"""The optimal weekly lineup: computed read-only, then applied via verified swaps."""

import asyncio
from pathlib import Path

import httpx

from ._api import (
    _editable_week,
    _fantasy_positions,
    _get,
    _lineup_slots,
    _players,
    _projected_points,
    _projections,
    _snapshot,
    _team_snapshot,
)
from .auth import DEFAULT_AUTH_PATH
from .lineup import _apply_swaps
from .team import _FLEX_ELIGIBLE, _POINTS_FIELDS, _full_name, _points_field

_SCORING = {field: variant for variant, field in _POINTS_FIELDS.items()}


def _eligible(positions: frozenset[str], slot: str) -> bool:
    """Whether a player with these fantasy positions may occupy the slot."""
    return slot in positions or bool(positions & _FLEX_ELIGIBLE.get(slot, set()))


def _rank(row: dict) -> tuple:
    """Candidate order: points descending, players without a projection last."""
    points = row["projected_points"]
    return (
        points is None,
        -points if points is not None else 0.0,
        row["full_name"],
        row["player_id"],
    )


def _candidates(
    roster: dict, players: dict, projections: dict, points_field: str
) -> list[dict]:
    """Rostered players who may start, best projection first.

    IR and taxi players are excluded: Sleeper rejects lineups that include
    them, and the swap helper refuses to move them.
    """
    inactive = set(roster.get("reserve") or []) | set(roster.get("taxi") or [])
    rows = []
    for player_id in roster["players"]:
        if player_id in inactive:
            continue
        record = players.get(player_id)
        if not isinstance(record, dict):
            raise RuntimeError(f"Sleeper has no player record for {player_id}.")
        rows.append(
            {
                "player_id": player_id,
                "full_name": _full_name(record, player_id),
                "positions": frozenset(_fantasy_positions(record, player_id)),
                "projected_points": _projected_points(
                    projections.get(player_id), player_id, points_field
                ),
            }
        )
    return sorted(rows, key=_rank)


def _select(candidates: list[dict], slots: list[str]) -> list[dict | None]:
    """The highest-projected legal starters for the slots, one per slot.

    Candidates are seated best projection first, and a player is kept only
    when everyone chosen so far can still occupy distinct slots. This greedy
    basis of the slot-assignment matroid maximizes the lineup's total
    projected points; players without a projection start only when a slot
    would otherwise be empty. Free base-position slots are tried before flex
    slots, so the arrangement usually keeps flex slots for the overflow.
    """
    order = sorted(range(len(slots)), key=lambda j: (slots[j] in _FLEX_ELIGIBLE, j))
    seated: dict[int, dict] = {}

    def seat(player: dict, visited: set[int]) -> bool:
        for j in order:
            if (
                j not in visited
                and j not in seated
                and _eligible(player["positions"], slots[j])
            ):
                seated[j] = player
                return True
        for j in order:
            if j not in visited and _eligible(player["positions"], slots[j]):
                visited.add(j)
                if seat(seated[j], visited):
                    seated[j] = player
                    return True
        return False

    for player in sorted(candidates, key=_rank):
        seat(player, set())
    return [seated.get(j) for j in range(len(slots))]


def _assign(
    chosen: list[dict], slots: list[str], starters: list[str]
) -> list[dict | None]:
    """Seat the chosen players, keeping current starters in their own slots.

    Current starters are seated first, each in his own slot; the remaining
    chosen players take free slots, base positions before flex slots, and
    displace a seated player only when no free eligible slot remains. Fewer
    moved starters means fewer exchanges, though the count is not minimized.
    """
    current = {player_id: j for j, player_id in enumerate(starters) if player_id != "0"}
    order = sorted(range(len(slots)), key=lambda j: (slots[j] in _FLEX_ELIGIBLE, j))
    seated: dict[int, dict] = {}

    def seat(player: dict, visited: set[int]) -> bool:
        own = current.get(player["player_id"])
        priority = order if own is None else [own, *order]
        for j in priority:
            if (
                j not in visited
                and j not in seated
                and _eligible(player["positions"], slots[j])
            ):
                seated[j] = player
                return True
        for j in priority:
            if j not in visited and _eligible(player["positions"], slots[j]):
                visited.add(j)
                if seat(seated[j], visited):
                    seated[j] = player
                    return True
        return False

    for player in sorted(
        chosen,
        key=lambda row: (
            row["player_id"] not in current,
            current.get(row["player_id"], len(slots)),
            _rank(row),
        ),
    ):
        if not seat(player, set()):
            raise RuntimeError(
                "A chosen player cannot be seated in any lineup slot; refresh Sleeper."
            )
    return [seated.get(j) for j in range(len(slots))]


def _plan_swaps(starters: list[str], target: list[str | None]) -> list[tuple[str, str]]:
    """The swaps that transform the starters into the target, in order.

    Each exchange starts one bench player in the slot he is targeted for and
    benches that slot's current player. A benched player who belongs in the
    target lineup then starts in his own target slot, until the chain benches
    a player who is not part of it. Every player lands in a slot he is
    eligible for, so Sleeper accepts every exchange.
    """
    target_slot = {player: j for j, player in enumerate(target) if player is not None}
    current = list(starters)
    swaps: list[tuple[str, str]] = []
    for entering in target:
        if entering is None or entering in starters:
            continue
        player, slot, chain = entering, target_slot[entering], set()
        while True:
            if slot in chain:
                raise RuntimeError(
                    "The swap plan would revisit a slot; nothing was applied."
                )
            chain.add(slot)
            leaving = current[slot]
            if leaving == "0":
                raise ValueError(
                    "The lineup has an empty starting slot that a player-to-player "
                    "swap cannot fill; fill it in Sleeper first."
                )
            swaps.append((player, leaving))
            current[slot] = player
            if leaving not in target_slot:
                break
            player, slot = leaving, target_slot[leaving]
    if any(
        current[j] != ("0" if wanted is None else wanted)
        for j, wanted in enumerate(target)
    ):
        # Reachable only when a current starter is no longer eligible for his
        # own slot, which a bench-for-starter exchange cannot repair.
        raise RuntimeError(
            "Player-to-player swaps cannot reach the optimal lineup from the current "
            "starters; nothing was applied. Correct the lineup in Sleeper first."
        )
    return swaps


def _total(chosen: list[dict | None]) -> float:
    return sum(
        player["projected_points"] or 0.0 for player in chosen if player is not None
    )


def _scoring(scoring: str | None, points_field: str) -> str:
    return scoring if scoring is not None else _SCORING[points_field]


async def optimal_lineup(
    league_id: str,
    user_id: str,
    *,
    week: int | None = None,
    scoring: str | None = None,
) -> dict:
    """Report the highest-projected legal lineup for one week.

    The return value contains `league_id`, `user_id`, `week`, `scoring`, one
    `lineup` entry per active slot in league order, and
    `total_projected_points`. Each entry holds the slot's name and the chosen
    player's `player_id`, `full_name`, `positions`, and `projected_points`;
    slots no rostered player can fill hold `None` instead. Positions and flex
    slots are filled together so no legal lineup projects more total points;
    players without a projection, for example on bye, start only when a slot
    would otherwise be empty. `week` defaults to Sleeper's current editable
    lineup week (the same week `set_optimal_lineup` applies) and may be any
    week from 1 to 18; `scoring` selects the points variant like `team`.
    Only Sleeper's public API is read; no browser opens and no lineup changes.
    """
    async with httpx.AsyncClient() as client:
        if week is None:
            state = await _get(client, "state/nfl")
            if not isinstance(state, dict):
                raise RuntimeError("Sleeper did not return the current NFL state.")
            week = _editable_week(state)
        snapshot = await _team_snapshot(client, league_id, user_id, week)
        league = snapshot["league"]
        points_field = _points_field(league.get("scoring_settings"), scoring)
        players, projections = await asyncio.gather(
            _players(client),
            _projections(client, snapshot["season"], snapshot["week"]),
        )
        slots = _lineup_slots(league)
        chosen = _select(
            _candidates(snapshot["roster"], players, projections, points_field), slots
        )
        lineup = []
        for slot, player in zip(slots, chosen):
            if player is None:
                lineup.append(
                    {
                        "slot": slot,
                        "player_id": None,
                        "full_name": None,
                        "positions": None,
                        "projected_points": None,
                    }
                )
                continue
            lineup.append(
                {
                    "slot": slot,
                    "player_id": player["player_id"],
                    "full_name": player["full_name"],
                    "positions": sorted(player["positions"]),
                    "projected_points": player["projected_points"],
                }
            )
        return {
            "league_id": league_id,
            "user_id": user_id,
            "week": snapshot["week"],
            "scoring": _scoring(scoring, points_field),
            "lineup": lineup,
            "total_projected_points": _total(chosen),
        }


async def set_optimal_lineup(
    league_id: str,
    *,
    user_id: str,
    scoring: str | None = None,
    auth_path: Path = DEFAULT_AUTH_PATH,
    passkey_path: Path | None = None,
    headless: bool = False,
) -> dict:
    """Compute the optimal lineup for the current editable week and apply it.

    The optimal lineup is computed exactly as `optimal_lineup` does, then the
    current lineup is transformed into it through the verified `swaps` helper:
    one browser session, one bench player and one starter at a time, each
    exchange verified before the next begins. Starters who already occupy
    their optimal slot are left alone; when the lineup is already optimal no
    browser opens. The return value contains `league_id`, `roster_id`, `week`,
    `scoring`, the ordered `swaps` applied as `player_in` and `player_out`,
    `starters_before`, `starters_after`, and the lineup's
    `total_projected_points`. If the lineup changes between planning and the
    first exchange, nothing is applied; if an exchange fails or something else
    changes the lineup mid-way, nothing further is applied and the error
    reports how many exchanges completed. A lineup with an empty
    starting slot cannot be optimized this way, because a player-to-player
    swap cannot fill an empty slot. Supplying `passkey_path` enables one
    unattended login attempt before lineup work.
    """
    async with httpx.AsyncClient() as client:
        snapshot = await _snapshot(client, league_id, user_id)
        week = snapshot["week"]
        slots = snapshot["slots"]
        points_field = _points_field(
            snapshot["league"].get("scoring_settings"), scoring
        )
        players, projections = await asyncio.gather(
            _players(client),
            _projections(client, snapshot["season"], week),
        )
        chosen = [
            player
            for player in _select(
                _candidates(snapshot["roster"], players, projections, points_field),
                slots,
            )
            if player is not None
        ]
        target = _assign(chosen, slots, snapshot["starters"])
        plan = _plan_swaps(
            snapshot["starters"],
            [player["player_id"] if player is not None else None for player in target],
        )
        before = list(snapshot["starters"])
        result = {
            "league_id": league_id,
            "roster_id": snapshot["roster_id"],
            "week": week,
            "scoring": _scoring(scoring, points_field),
            "swaps": [],
            "starters_before": before,
            "starters_after": list(before),
            "total_projected_points": _total(target),
        }
        if not plan:
            return result
        # The planning snapshot is the baseline, so a lineup changed since the
        # plan was computed is rejected before any browser opens.
        batch = await _apply_swaps(
            league_id,
            plan,
            user_id=user_id,
            auth_path=auth_path,
            passkey_path=passkey_path,
            headless=headless,
            baseline=snapshot,
        )
        result["swaps"] = [
            {"player_in": entering, "player_out": leaving} for entering, leaving in plan
        ]
        result["starters_after"] = batch["starters_after"]
        return result
