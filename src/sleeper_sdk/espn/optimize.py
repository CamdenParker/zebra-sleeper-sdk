"""ESPN's native eligible slots and projections with shared prop-first ranking."""

import asyncio
from pathlib import Path

import httpx

from ..optimize import _assign, _composite_prop_score, _select, _total
from ..props import WeeklyProps, _props_for_player, _week_props
from ._api import (
    _SCORING_FIELDS,
    ESPNClient,
    editable_week,
    projected_stats,
    scoring_settings,
)
from .auth import DEFAULT_AUTH_PATH
from .lineup import (
    _INACTIVE,
    _apply_lineup,
    _assignments,
    _locked,
    _roster,
    _schedule,
    _validate_slots,
)

_POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
_SLOTS = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K", 23: "FLEX"}


def _slots(snapshot: dict) -> list[int]:
    counts = snapshot["settings"]["rosterSettings"]["lineupSlotCounts"]
    slots = []
    for slot, count in sorted(counts.items(), key=lambda item: int(item[0])):
        if type(count) is not int or count < 0:
            raise RuntimeError("ESPN returned invalid lineup slot counts.")
        if int(slot) not in _INACTIVE:
            slots.extend([int(slot)] * count)
    if not slots:
        raise RuntimeError("ESPN returned no starting lineup slots.")
    return slots


def _candidates(
    snapshot: dict, team_id: int, week: int, markets: WeeklyProps, games: dict
) -> list[dict]:
    scoring = scoring_settings(snapshot)
    rows = []
    for entry in _roster(snapshot, team_id):
        if entry["lineupSlotId"] in _INACTIVE - {20}:
            continue
        player = entry["playerPoolEntry"]["player"]
        if player["id"] != entry["playerId"]:
            raise RuntimeError("ESPN returned inconsistent roster player IDs.")
        projection, points = projected_stats(player, week, season=snapshot["seasonId"])
        team = games.get(player["proTeamId"], {}).get("abbreviation")
        position = _POSITIONS.get(player["defaultPositionId"], "")
        prop_scoring_supported = not any(
            item["statId"] in _SCORING_FIELDS
            and str(player["defaultPositionId"]) in item.get("pointsOverrides", {})
            for item in snapshot["settings"]["scoringSettings"]["scoringItems"]
        )
        prop_score = (
            _composite_prop_score(
                position,
                _props_for_player(markets, player["fullName"], team),
                projection,
                scoring,
                None,
            )
            if prop_scoring_supported
            else None
        )
        if prop_score is not None and points is not None:
            # Retain native bonuses and stats that shared prop scoring does not cover.
            covered = ["rush_yd", "rush_td", "rec_td", "fum_lost"]
            covered.extend(
                ["pass_yd", "pass_td", "pass_int"]
                if position == "QB"
                else ["rec_yd", "rec"]
            )
            prop_score += points - sum(
                projection.get(field, 0) * scoring[field] for field in covered
            )
        rows.append(
            {
                "player_id": entry["playerId"],
                "full_name": player["fullName"],
                # Native slot tokens preserve exact eligibility through shared helpers.
                "positions": frozenset(str(slot) for slot in player["eligibleSlots"]),
                "position": position,
                "prop_score": prop_score,
                "projected_points": points,
            }
        )
    return rows


async def _plan(
    client: ESPNClient, week: int | None = None
) -> tuple[dict, dict, frozenset[int]]:
    snapshot = await client.league()
    current_week = editable_week(snapshot)
    week = current_week if week is None else week
    if type(week) is not int or not 1 <= week <= 18:
        raise ValueError("week must be between 1 and 18.")
    if week != current_week:
        snapshot = await client.league(week)
    async with httpx.AsyncClient() as props_client:
        if week < current_week:
            schedule = await client.pro_schedule(week)
            markets: WeeklyProps = {}
        else:
            schedule, slate = await asyncio.gather(
                client.pro_schedule(week),
                _week_props(props_client, str(snapshot["seasonId"]), week),
            )
            markets = slate[0]
    games = _schedule(schedule, week, require_kickoffs=week == current_week)
    locked = (
        _locked(snapshot, client.team_id, schedule)
        if week == current_week
        else frozenset()
    )
    rows = _candidates(snapshot, client.team_id, week, markets, games)
    by_id = {row["player_id"]: row for row in rows}
    slots = _slots(snapshot)
    current = _assignments(snapshot, client.team_id)
    _validate_slots(snapshot, current)
    starters = []
    for slot in slots:
        occupied = sorted(
            player for player, assigned in current.items() if assigned == slot
        )
        index = sum(previous == slot for previous in slots[: len(starters)])
        starters.append(occupied[index] if index < len(occupied) else "0")
    fixed = {
        index: by_id[player]
        for index, player in enumerate(starters)
        if player in locked
    }
    open_indices = [index for index in range(len(slots)) if index not in fixed]
    open_slots = [str(slots[index]) for index in open_indices]
    selected = _select(
        [row for row in rows if row["player_id"] not in locked], open_slots
    )
    assigned = iter(
        _assign(
            [row for row in selected if row is not None],
            open_slots,
            [starters[index] for index in open_indices],
        )
    )
    chosen = [
        fixed[index] if index in fixed else next(assigned)
        for index in range(len(slots))
    ]
    lineup = [
        {
            "slot": _SLOTS.get(slot, f"ESPN slot {slot}"),
            "slot_id": slot,
            "player_id": row["player_id"] if row else None,
            "full_name": row["full_name"] if row else None,
            "position": row["position"] if row else None,
            "prop_score": row["prop_score"] if row else None,
            "projected_points": row["projected_points"] if row else None,
        }
        for slot, row in zip(slots, chosen)
    ]
    return (
        snapshot,
        {
            "league_id": snapshot["id"],
            "team_id": client.team_id,
            "season": snapshot["seasonId"],
            "week": week,
            "scoring": "ppr" if scoring_settings(snapshot)["rec"] == 1 else "league",
            "lineup": lineup,
            "total_projected_points": _total(chosen),
        },
        locked,
    )


async def optimal_lineup(
    league_id: int,
    team_id: int,
    *,
    season: int | None = None,
    week: int | None = None,
    auth_path: Path = DEFAULT_AUTH_PATH,
) -> dict:
    """Read the prop-first lineup using ESPN projections and league scoring.

    The editable week's locked players keep their native slots. Native eligible
    slot IDs control every assignment; unavailable prop markets use ESPN points
    as the shared ranking's fallback. Past weeks use ESPN's historical roster
    and projections without pregame props. No lineup transaction is submitted.
    """
    async with ESPNClient(
        league_id, team_id, season=season, auth_path=auth_path
    ) as client:
        _, result, _ = await _plan(client, week)
        return result


async def set_optimal_lineup(
    league_id: int,
    team_id: int,
    *,
    season: int | None = None,
    auth_path: Path = DEFAULT_AUTH_PATH,
) -> dict:
    """Apply the current week's lineup once, guarding drift and verifying all slots.

    Ownership, league settings, the complete roster, and ESPN kickoff locks are
    checked again immediately before submission. An uncertain result raises and
    must be inspected in ESPN before another attempt.
    """
    async with ESPNClient(
        league_id, team_id, season=season, auth_path=auth_path
    ) as client:
        snapshot, result, locked = await _plan(client)
        await client.verify_owner(snapshot)
        target = _assignments(snapshot, team_id)
        for player, slot in target.items():
            if slot not in _INACTIVE:
                target[player] = 20
        for row in result["lineup"]:
            if row["player_id"] is not None:
                target[row["player_id"]] = row["slot_id"]
        result.update(await _apply_lineup(client, snapshot, target, locked))
        return result
