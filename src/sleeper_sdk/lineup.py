"""Verified lineup exchanges through Sleeper's roster UI, one session per batch."""

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, expect

from ._api import _expected_starters, _snapshot, _starters
from .auth import DEFAULT_AUTH_PATH, _session

_SWAP_LOCK = Lock()
_UI_TIMEOUT_MS = 15_000
_VERIFY_TIMEOUT = 55.0
_API_POLL_INTERVAL = 0.75

# Sleeper's desktop roster DOM, inspected on the authenticated Team page.
# A first position click selects a row; only the second position click submits.
_ROSTER = ".team-roster"
_ROWS = ".team-roster-item"
_POSITION = ".link-button.cell-position"
_SLOT = ".league-slot-position-square"
_AVATAR = ".avatar-player"
_PLAYER_META = ".cell-player-meta"
_WEEK = ".week-selector-dropdown .label .text"
_ACCOUNT = ".nav-profile-item"
_ACCOUNT_NAME = ".name"
_PROFILE_USERNAME = "Username"
_PRE_CLICK_FAILURE = (
    "Sleeper's authenticated page or roster controls could not be verified before the swap. "
    "No target click was attempted. Refresh Sleeper and confirm that the roster is editable."
)


def _snapshot_key(snapshot: dict) -> tuple:
    """Fields that must not change between choosing and submitting an exchange."""
    league, roster = snapshot["league"], snapshot["roster"]
    return (
        snapshot["roster_id"],
        snapshot["week"],
        snapshot["source"],
        snapshot["starters"],
        snapshot["slots"],
        league.get("league_id"),
        league.get("season"),
        league.get("season_type"),
        league.get("sport"),
        league.get("status"),
        league.get("settings"),
        roster.get("owner_id"),
        sorted(roster.get("co_owners") or []),
        sorted(roster["players"]),
        sorted(roster.get("reserve") or []),
        sorted(roster.get("taxi") or []),
    )


async def _account_name(page) -> str:
    account = page.locator(_ACCOUNT)
    await account.wait_for(state="visible", timeout=_UI_TIMEOUT_MS)
    username = account.locator(_ACCOUNT_NAME)
    await username.wait_for(state="attached", timeout=_UI_TIMEOUT_MS)
    name = (await username.text_content(timeout=_UI_TIMEOUT_MS) or "").strip()
    if await account.count() != 1 or await username.count() != 1 or not name:
        raise RuntimeError(
            "Sleeper's signed-in account cannot be identified in the page."
        )
    return name


async def _read_ui(page, league_id: str, account_name: str) -> dict:
    location = urlparse(page.url)
    if (
        location.scheme != "https"
        or location.netloc != "sleeper.com"
        or (location.path.rstrip("/") != f"/leagues/{league_id}/team")
    ):
        raise RuntimeError(
            "The browser is no longer on the requested league's Team page."
        )
    if await _account_name(page) != account_name:
        raise RuntimeError(
            "Sleeper's signed-in account changed; no further exchange is allowed."
        )
    roster = page.locator(_ROSTER)
    await roster.wait_for(state="visible", timeout=_UI_TIMEOUT_MS)
    if await roster.count() != 1:
        raise RuntimeError("Sleeper displayed an ambiguous roster.")
    await roster.locator(_ROWS).first.wait_for(state="visible", timeout=_UI_TIMEOUT_MS)
    week = roster.locator(_WEEK)
    await week.wait_for(state="visible", timeout=_UI_TIMEOUT_MS)
    week_text = (await week.inner_text(timeout=_UI_TIMEOUT_MS)).strip()
    match = re.fullmatch(r"Wk\. (\d+)", week_text)
    if await week.count() != 1 or match is None:
        raise RuntimeError("Sleeper's displayed lineup week cannot be identified.")
    rows = await roster.locator(_ROWS).evaluate_all(
        """(rows, s) => rows.map(row => {
            const controls = row.querySelectorAll(s.position);
            const badges = row.querySelectorAll(s.slot);
            const avatars = row.querySelectorAll(s.avatar);
            const metadata = row.querySelectorAll(s.metadata);
            return {
                classes: [...row.classList],
                controls: controls.length,
                label: controls[0]?.getAttribute('aria-label'),
                badges: badges.length,
                slotClasses: badges[0] ? [...badges[0].classList] : [],
                avatars: [...avatars].map(img => img.getAttribute('aria-label')),
                empty: metadata.length === 1 && metadata[0].textContent.trim() === 'Empty'
            };
        })""",
        {
            "position": _POSITION,
            "slot": _SLOT,
            "avatar": _AVATAR,
            "metadata": _PLAYER_META,
        },
    )
    if not rows:
        raise RuntimeError("Sleeper did not render any roster rows.")
    for row in rows:
        if row["controls"] != 1 or row["badges"] != 1:
            raise RuntimeError("Sleeper's roster row controls are ambiguous.")
        slot_classes = [
            value
            for value in row["slotClasses"]
            if value != "league-slot-position-square"
        ]
        if len(slot_classes) != 1:
            raise RuntimeError("Sleeper's roster slot cannot be identified.")
        row["slot"] = slot_classes[0].upper()
        if row["empty"] and not row["avatars"]:
            row["player_id"] = "0"
        elif len(row["avatars"]) == 1 and isinstance(row["avatars"][0], str):
            player = re.fullmatch(r"nfl Player (\S+)", row["avatars"][0])
            if player is None or row["empty"]:
                raise RuntimeError(
                    "Sleeper's player ID cannot be identified in a roster row."
                )
            row["player_id"] = player[1]
        else:
            raise RuntimeError(
                "Sleeper displayed an ambiguous player image in a roster row."
            )
    return {"week": int(match[1]), "rows": rows}


def _ui_starters(ui: dict) -> list[str]:
    return [
        row["player_id"] for row in ui["rows"] if row["slot"] not in {"BN", "IR", "TX"}
    ]


def _assert_ui(
    ui: dict, snapshot: dict, starters: list[str], *, selected: str | None = None
) -> None:
    rows, roster = ui["rows"], snapshot["roster"]
    count = len(snapshot["slots"])
    if ui["week"] != snapshot["week"]:
        raise RuntimeError(
            "The displayed week differs from Sleeper's current lineup week."
        )
    if (
        [row["slot"] for row in rows[:count]] != snapshot["slots"]
        or any(row["slot"] not in {"BN", "IR", "TX"} for row in rows[count:])
        or _ui_starters(ui) != starters
    ):
        raise RuntimeError(
            "The full ordered starter lineup in Sleeper does not match the API."
        )
    occupied = [row["player_id"] for row in rows if row["player_id"] != "0"]
    if len(occupied) != len(set(occupied)) or set(occupied) != set(roster["players"]):
        raise RuntimeError(
            "Sleeper's displayed roster does not match the authenticated user's roster."
        )
    for slot, key in (("IR", "reserve"), ("TX", "taxi")):
        actual = {
            row["player_id"]
            for row in rows
            if row["slot"] == slot and row["player_id"] != "0"
        }
        if actual != set(roster.get(key) or []):
            raise RuntimeError(
                "Sleeper's displayed IR or taxi squad differs from the API."
            )
    selected_ids = [row["player_id"] for row in rows if "selected" in row["classes"]]
    if selected_ids != ([] if selected is None else [selected]):
        raise RuntimeError("Sleeper's selected exchange row changed or is ambiguous.")


def _ui_player(ui: dict, player_id: str) -> dict:
    rows = [row for row in ui["rows"] if row["player_id"] == player_id]
    if len(rows) != 1:
        raise RuntimeError("The requested player has no unique roster row in Sleeper.")
    return rows[0]


def _row_locator(
    page,
    player_id: str,
    *,
    unselected: bool = False,
    selected_source: str | None = None,
):
    roster = page.locator(_ROSTER)
    if unselected:
        roster = roster.filter(has_not=page.locator(f"{_ROWS}.selected"))
    if selected_source is not None:
        source = page.locator(f"{_ROWS}.selected").filter(
            has=page.get_by_label(f"nfl Player {selected_source}", exact=True),
        )
        roster = roster.filter(has=source)
    rows = (
        _ROWS
        if selected_source is None
        else f"{_ROWS}.valid:not(.invalid):not(.selected)"
    )
    return roster.locator(rows).filter(
        has=page.get_by_label(f"nfl Player {player_id}", exact=True)
    )


def _assert_eligible(ui: dict, source: str, target: str) -> None:
    for player in (source, target):
        row = _ui_player(ui, player)
        if (
            "valid" not in row["classes"]
            or "invalid" in row["classes"]
            or not isinstance(row["label"], str)
            or not row["label"].startswith(f"Slot {row['slot']} - ")
        ):
            raise ValueError(
                "Sleeper does not allow this direct exchange; a player may be locked or ineligible."
            )


def _validate_pair(player_a_id: str, player_b_id: str) -> None:
    """Check the ID shapes of one exchange before any browser opens."""
    for player in (player_a_id, player_b_id):
        if (
            not isinstance(player, str)
            or not player
            or player == "0"
            or player != player.strip()
        ):
            raise ValueError(
                "Player IDs must be nonempty strings, not names or empty-slot IDs."
            )
    if player_a_id == player_b_id:
        raise ValueError("Choose two different players to swap.")


async def _read_account_name(page) -> str:
    """Resolve the signed-in username from Sleeper's own profile field."""
    account_name = (
        await page.get_by_role(
            "textbox",
            name=_PROFILE_USERNAME,
            exact=True,
        ).input_value(timeout=_UI_TIMEOUT_MS)
    ).strip()
    if not account_name:
        raise RuntimeError(
            "Sleeper's own profile did not expose the authenticated username."
        )
    return account_name


async def _verify(
    page,
    client,
    league_id: str,
    account_name: str,
    snapshot: dict,
    expected: list[str],
    click_error,
) -> list[str]:
    """Read after the sole submit attempt, including when that click timed out."""
    last_ui = last_api = None
    cause = click_error

    async def read_reloaded_ui() -> bool:
        nonlocal last_ui, cause
        try:
            await page.reload(wait_until="domcontentloaded", timeout=_UI_TIMEOUT_MS)
            ui = await _read_ui(page, league_id, account_name)
            last_ui = _ui_starters(ui)
            _assert_ui(ui, snapshot, expected)
            return True
        except Exception as exc:
            cause = cause or exc
            return False

    try:
        async with asyncio.timeout(_VERIFY_TIMEOUT):
            try:
                # Sleeper clears selection after the save response. Let that finish
                # before a reload can interrupt an in-flight request.
                await expect(
                    page.locator(_ROSTER).locator(f"{_ROWS}.selected")
                ).to_have_count(
                    0,
                    timeout=_UI_TIMEOUT_MS,
                )
            except Exception as exc:
                cause = cause or exc
            await read_reloaded_ui()
            while True:
                try:
                    last_api = await _starters(
                        client,
                        league_id,
                        snapshot["roster_id"],
                        snapshot["week"],
                        source=snapshot["source"],
                    )
                    if last_api == expected and await read_reloaded_ui():
                        return last_api
                except Exception as exc:
                    cause = cause or exc
                # This is bounded API propagation polling, not a browser readiness delay.
                await asyncio.sleep(_API_POLL_INTERVAL)
    except TimeoutError as exc:
        cause = cause or exc
    raise RuntimeError(
        "The swap may already have applied, but its complete lineup could not be verified. "
        "Do not retry automatically. "
        f"Last UI starters: {last_ui!r}; last API starters: {last_api!r}."
    ) from cause


async def _exchange_once(
    page,
    client: httpx.AsyncClient,
    league_id: str,
    user_id: str,
    account_name: str,
    snapshot: dict,
    current: list[str],
    expected: list[str],
    player_a_id: str,
    player_b_id: str,
) -> list[str]:
    """Run one verified exchange inside an open team session.

    `current` is the lineup the previous exchange left behind and `expected`
    the lineup this one must produce. `current` is checked against a fresh API
    read immediately before the submit click, so drift caused by another actor
    aborts before anything is clicked. Returns the verified starters.
    """
    view = {**snapshot, "starters": current}
    source = player_a_id if player_a_id in current else player_b_id
    target = player_b_id if source == player_a_id else player_a_id
    submit_attempted = False
    try:
        ui = await _read_ui(page, league_id, account_name)
        _assert_ui(ui, snapshot, current)
        source_data = _ui_player(ui, source)
        if not isinstance(source_data["label"], str) or not source_data[
            "label"
        ].startswith(f"Slot {source_data['slot']} - "):
            raise ValueError(
                "Sleeper has locked or disabled the requested starter's position."
            )
        source_row = _row_locator(page, source)
        if await source_row.count() != 1:
            raise RuntimeError("The requested starter's position control is ambiguous.")
        # Selecting the source only highlights exchange options; it does not submit.
        await (
            _row_locator(page, source, unselected=True)
            .locator(_POSITION)
            .click(timeout=_UI_TIMEOUT_MS)
        )
        await expect(source_row).to_have_class(
            re.compile(r"\bselected\b"), timeout=_UI_TIMEOUT_MS
        )
        ui = await _read_ui(page, league_id, account_name)
        _assert_ui(ui, snapshot, current, selected=source)
        _assert_eligible(ui, source, target)

        fresh = await _snapshot(client, league_id, user_id)
        if _snapshot_key(fresh) != _snapshot_key(view):
            raise RuntimeError(
                "The league, week, roster, or lineup changed before the exchange; refresh Sleeper."
            )
        # Re-read the full UI and eligibility immediately before the sole submit.
        ui = await _read_ui(page, league_id, account_name)
        _assert_ui(ui, fresh, current, selected=source)
        _assert_eligible(ui, source, target)
        target_row = _row_locator(page, target, selected_source=source)
        if await target_row.count() != 1:
            raise RuntimeError("The requested exchange target is ambiguous.")
        click_error = None
        submit_attempted = True
        try:
            await target_row.locator(_POSITION).click(timeout=_UI_TIMEOUT_MS)
        except Exception as exc:
            click_error = exc
        return await _verify(
            page,
            client,
            league_id,
            account_name,
            snapshot,
            expected,
            click_error,
        )
    except (PlaywrightError, AssertionError) as exc:
        if submit_attempted:
            raise RuntimeError(
                "The swap may already have applied, but the browser could not verify it. "
                "Inspect Sleeper before trying again. "
                f"Verified starters before submission: {current!r}."
            ) from exc
        raise RuntimeError(_PRE_CLICK_FAILURE) from exc


@asynccontextmanager
async def _open_team(
    league_id: str,
    user_id: str,
    auth_path: Path,
    passkey_path: Path | None,
    headless: bool,
) -> AsyncIterator[tuple[Page, str]]:
    """Enter the session, verify the account, and land on the league's Team page."""
    async with _session(
        auth_path, headless=headless, user_id=user_id, passkey_path=passkey_path
    ) as (page, authenticated_user_id):
        if authenticated_user_id != user_id:
            raise ValueError(
                "The saved Sleeper account does not match user_id. Run login for the intended account."
            )
        account_name = await _read_account_name(page)
        await page.goto(
            f"https://sleeper.com/leagues/{league_id}/team",
            wait_until="domcontentloaded",
            timeout=_UI_TIMEOUT_MS,
        )
        yield page, account_name


async def swap(
    league_id: str,
    player_a_id: str,
    player_b_id: str,
    *,
    user_id: str,
    auth_path: Path = DEFAULT_AUTH_PATH,
    passkey_path: Path | None = None,
    headless: bool = False,
) -> dict:
    """Exchange two rostered players once, then verify every ordered starter slot.

    At least one player must be a starter. Sleeper decides whether the exact
    exchange is legal. Concurrent calls in this process are rejected. A failure
    after the target click may mean the exchange applied; never retry it blindly.
    Supplying passkey_path enables one unattended login attempt before lineup work.
    For several exchanges in one browser session, use `swaps`.
    """
    _validate_pair(player_a_id, player_b_id)
    result = await _apply_swaps(
        league_id,
        [(player_a_id, player_b_id)],
        user_id=user_id,
        auth_path=auth_path,
        passkey_path=passkey_path,
        headless=headless,
    )
    del result["swaps"]
    return result


async def swaps(
    league_id: str,
    exchanges: Sequence[tuple[str, str]],
    *,
    user_id: str,
    auth_path: Path = DEFAULT_AUTH_PATH,
    passkey_path: Path | None = None,
    headless: bool = False,
) -> dict:
    """Exchange several player pairs in one browser session, verifying each one.

    Every pair is applied in order through the same checks as `swap`: the full
    ordered lineup is asserted in the UI before the submit click, the click is
    checked against a fresh API read, and the complete lineup is verified in
    both the reloaded UI and the API before the next exchange begins. The
    returned dictionary contains `league_id`, `roster_id`, `week`,
    `starters_before`, `starters_after`, and the applied `swaps` as
    `player_a_id`/`player_b_id` pairs in order. The whole chain is checked
    against the current roster before any browser opens, so a pair that would
    be illegal after the earlier ones is rejected with nothing applied. An
    empty exchange list opens no browser and reports the current lineup. If an
    exchange fails, nothing
    further is applied and the error reports how many exchanges completed;
    inspect Sleeper before trying again, since a failure after a submit click
    may mean that exchange applied. Concurrent calls in this process are
    rejected. Supplying passkey_path enables one unattended login attempt
    before lineup work.
    """
    pairs: list[tuple[str, str]] = []
    for exchange in exchanges:
        if not isinstance(exchange, (tuple, list)) or len(exchange) != 2:
            raise ValueError(
                "Each exchange must be a pair of player IDs, for example ('A', 'B')."
            )
        player_a_id, player_b_id = exchange
        _validate_pair(player_a_id, player_b_id)
        pairs.append((player_a_id, player_b_id))
    return await _apply_swaps(
        league_id,
        pairs,
        user_id=user_id,
        auth_path=auth_path,
        passkey_path=passkey_path,
        headless=headless,
    )


def _plan_lineups(snapshot: dict, pairs: list[tuple[str, str]]) -> list[list[str]]:
    """The lineup each exchange must produce, validated before any browser opens."""
    lineups: list[list[str]] = []
    current = snapshot["starters"]
    for player_a_id, player_b_id in pairs:
        current = _expected_starters(
            {**snapshot, "starters": current}, player_a_id, player_b_id
        )
        lineups.append(current)
    return lineups


async def _apply_swaps(
    league_id: str,
    pairs: list[tuple[str, str]],
    *,
    user_id: str,
    auth_path: Path,
    passkey_path: Path | None,
    headless: bool,
    baseline: dict | None = None,
) -> dict:
    """Apply shape-checked pairs in order within one session.

    The whole chain is validated against the current lineup before a browser
    opens, so a pair that would become illegal partway through is rejected
    with nothing applied. A `baseline` snapshot, when given, must still match
    the current one; this keeps a caller's plan from running against a lineup
    it was not computed for.
    """
    if not _SWAP_LOCK.acquire(blocking=False):
        raise RuntimeError("Another swap is already running in this process.")
    try:
        async with httpx.AsyncClient() as client:
            snapshot = await _snapshot(client, league_id, user_id)
            if baseline is not None and _snapshot_key(snapshot) != _snapshot_key(
                baseline
            ):
                raise RuntimeError(
                    "The league, week, roster, or lineup changed after the exchanges "
                    "were planned; nothing was applied."
                )
            before = list(snapshot["starters"])
            lineups = _plan_lineups(snapshot, pairs)
            current = before
            completed: list[dict] = []
            if pairs:
                async with _open_team(
                    league_id, user_id, auth_path, passkey_path, headless
                ) as (page, account_name):
                    for (player_a_id, player_b_id), expected in zip(pairs, lineups):
                        try:
                            current = await _exchange_once(
                                page,
                                client,
                                league_id,
                                user_id,
                                account_name,
                                snapshot,
                                current,
                                expected,
                                player_a_id,
                                player_b_id,
                            )
                        except Exception as exc:
                            if len(pairs) == 1:
                                raise
                            raise RuntimeError(
                                f"The batch stopped after {len(completed)} of {len(pairs)} "
                                f"exchanges; inspect Sleeper before trying again. {exc}"
                            ) from exc
                        completed.append(
                            {"player_a_id": player_a_id, "player_b_id": player_b_id}
                        )
            return {
                "league_id": league_id,
                "roster_id": snapshot["roster_id"],
                "week": snapshot["week"],
                "starters_before": before,
                "starters_after": list(current),
                "swaps": completed,
            }
    except (PlaywrightError, AssertionError) as exc:
        # Exchanges convert their own browser errors, so these precede any click.
        raise RuntimeError(_PRE_CLICK_FAILURE) from exc
    finally:
        _SWAP_LOCK.release()
