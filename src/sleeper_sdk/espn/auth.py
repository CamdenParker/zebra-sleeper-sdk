"""Private ESPN cookie persistence and interactive sign-in."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import UUID

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from ..auth import (
    _auth_lock,
    _launch_browser,
    _MissingAuth,
    _read_private_json,
    _write_private_json,
)

DEFAULT_AUTH_PATH = Path.home() / ".sleeper-sdk" / "espn.json"


class _AccessDenied(RuntimeError):
    """Cookies are not yet usable for the requested league."""


class _NotOwner(RuntimeError):
    """The current cookies belong to a different team owner."""


def _validate_ids(league_id: int, team_id: int, season: int) -> None:
    if any(type(value) is not int or value <= 0 for value in (league_id, team_id)):
        raise ValueError("league_id and team_id must be positive integers.")
    if type(season) is not int or not 2000 <= season <= 2100:
        raise ValueError("season must be an integer between 2000 and 2100.")


def _identity(swid: object) -> str:
    if not isinstance(swid, str):
        raise RuntimeError("ESPN authentication has no valid account identity.")  # noqa: TRY004
    try:
        return str(UUID(swid.strip("{}")))
    except ValueError:
        raise RuntimeError(
            "ESPN authentication has no valid account identity."
        ) from None


def _validate_cookies(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(name), str)
        or not value[name]
        or any(ord(char) < 33 or ord(char) > 126 for char in value[name])
        or ";" in value[name]
        for name in ("SWID", "espn_s2")
    ):
        raise RuntimeError(
            "Saved ESPN authentication is malformed. Run ESPN login again."
        )
    _identity(value["SWID"])
    return {name: value[name] for name in ("SWID", "espn_s2")}


def _load_cookies(auth_path: Path = DEFAULT_AUTH_PATH) -> dict[str, str]:
    try:
        value = _read_private_json(Path(auth_path), label="ESPN authentication")
    except _MissingAuth:
        raise RuntimeError(
            "No saved ESPN authentication. Run ESPN login first."
        ) from None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RuntimeError(
            "Saved ESPN authentication is malformed. Run ESPN login again."
        )
    return _validate_cookies(value.get("cookies"))


async def login(
    league_id: int,
    team_id: int,
    *,
    season: int | None = None,
    auth_path: Path = DEFAULT_AUTH_PATH,
) -> None:
    """Open ESPN for sign-in; save only cookies verified to own the requested team."""
    from ._api import ESPNClient, _season

    season = _season(season)
    _validate_ids(league_id, team_id, season)
    auth_path = Path(auth_path)
    with _auth_lock(auth_path):
        async with async_playwright() as playwright:
            browser = await _launch_browser(playwright, headless=False)
            try:
                context = await browser.new_context()
                await context.new_page()
                await context.pages[0].goto(
                    f"https://fantasy.espn.com/football/team?leagueId={league_id}"
                    f"&teamId={team_id}&seasonId={season}",
                    wait_until="domcontentloaded",
                )
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    if not browser.is_connected() or not context.pages:
                        raise RuntimeError("ESPN login was closed before verification.")
                    cookies = {}
                    for cookie in await context.cookies("https://fantasy.espn.com"):
                        name, value = cookie.get("name"), cookie.get("value")
                        if (
                            name in ("SWID", "espn_s2")
                            and isinstance(name, str)
                            and isinstance(value, str)
                        ):
                            cookies[name] = value
                    if all(cookies.get(name) for name in ("SWID", "espn_s2")):
                        cookies = _validate_cookies(cookies)
                        try:
                            async with ESPNClient(
                                league_id, team_id, season=season, cookies=cookies
                            ) as client:
                                await client.verify_owner(await client.league())
                        except (_AccessDenied, _NotOwner):
                            await asyncio.sleep(2)
                            continue
                        _write_private_json(
                            auth_path, {"version": 1, "cookies": cookies}
                        )
                        return
                    await asyncio.sleep(1)
                raise RuntimeError(
                    "ESPN login timed out. Sign in with an owner of the requested team."
                )
            except PlaywrightError:
                raise RuntimeError(
                    "The ESPN login browser closed or could not load the team page."
                ) from None
            finally:
                await browser.close()


async def check_auth(
    league_id: int,
    team_id: int,
    *,
    season: int | None = None,
    auth_path: Path = DEFAULT_AUTH_PATH,
) -> dict:
    """Verify saved cookies and team ownership without returning credentials."""
    from ._api import ESPNClient

    async with ESPNClient(
        league_id, team_id, season=season, auth_path=auth_path
    ) as client:
        await client.verify_owner(await client.league())
    return {"league_id": league_id, "team_id": team_id, "season": client.season}
