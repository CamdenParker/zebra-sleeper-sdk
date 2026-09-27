"""One guarded ESPN lineup transaction, followed by full-roster verification."""

import asyncio
import math
import time
from collections import Counter
from threading import Lock

from ._api import ESPNClient, editable_week

_LINEUP_LOCK = Lock()
_INACTIVE = frozenset({20, 21, 88})


def _roster(snapshot: dict, team_id: int) -> list[dict]:
    teams = [team for team in snapshot["teams"] if team["id"] == team_id]
    if len(teams) != 1:
        raise RuntimeError("ESPN did not return one unique requested team.")
    entries = teams[0]["roster"]["entries"]
    ids = [entry["playerId"] for entry in entries]
    current = snapshot["scoringPeriodId"] == editable_week(snapshot)
    if (
        not entries
        or any(type(player) is not int for player in ids)
        or len(ids) != len(set(ids))
        or any(type(entry["lineupSlotId"]) is not int for entry in entries)
        or any(
            current and entry["playerPoolEntry"].get("onTeamId", team_id) != team_id
            for entry in entries
        )
    ):
        raise RuntimeError("ESPN returned an empty or ambiguous roster.")
    return entries


def _assignments(snapshot: dict, team_id: int) -> dict[int, int]:
    return {
        entry["playerId"]: entry["lineupSlotId"] for entry in _roster(snapshot, team_id)
    }


def _snapshot_key(snapshot: dict, team_id: int, *, lineup: bool = True) -> tuple:
    roster = []
    for entry in _roster(snapshot, team_id):
        pool = entry["playerPoolEntry"]
        player = pool["player"]
        roster.append(
            (
                entry["playerId"],
                entry["lineupSlotId"] if lineup else None,
                entry.get("lineupLocked") if lineup else None,
                pool.get("lineupLocked") if lineup else None,
                pool.get("rosterLocked") if lineup else None,
                entry.get("injuryStatus"),
                entry.get("pendingTransactionIds"),
                player["id"],
                player["proTeamId"],
                sorted(player["eligibleSlots"]),
            )
        )
    scoring = snapshot["settings"]["scoringSettings"]
    return (
        snapshot["id"],
        snapshot["seasonId"],
        snapshot["scoringPeriodId"],
        editable_week(snapshot),
        {
            field: snapshot["status"].get(field)
            for field in (
                "isActive",
                "isExpired",
                "isToBeDeleted",
                "currentMatchupPeriod",
                "transactionScoringPeriod",
                "firstScoringPeriod",
                "finalScoringPeriod",
            )
        },
        snapshot["settings"]["rosterSettings"],
        {field: value for field, value in scoring.items() if field != "scoringItems"},
        [
            {
                field: value
                for field, value in item.items()
                if field not in {"leagueRanking", "leagueTotal"}
            }
            for item in scoring["scoringItems"]
        ],
        sorted(
            (team["id"], sorted(team.get("owners", []))) for team in snapshot["teams"]
        ),
        sorted(roster),
    )


def _validate_slots(snapshot: dict, assignments: dict[int, int]) -> None:
    settings = snapshot["settings"]["rosterSettings"]
    counts = settings["lineupSlotCounts"]
    if any(
        count > counts.get(str(slot), 0)
        for slot, count in Counter(assignments.values()).items()
        if not (slot == 20 and settings.get("isBenchUnlimited") is True)
    ):
        raise ValueError("The ESPN roster exceeds its configured slot counts.")


def _schedule(
    schedule: dict, week: int, *, require_kickoffs: bool = True
) -> dict[int, dict]:
    """Resolve ESPN kickoffs independently of available bookmaker markets."""
    result = {}
    now = time.time()
    for team in schedule["settings"]["proTeams"]:
        if team["id"] == 0:
            continue
        games = team.get("proGamesByScoringPeriod", {}).get(str(week), [])
        bye = team.get("byeWeek") == week
        dates = [game.get("date") for game in games]
        if (
            require_kickoffs
            and not bye
            and (
                not dates
                or any(
                    type(date) not in {int, float}
                    or not math.isfinite(date)
                    or date <= 0
                    for date in dates
                )
                or any(
                    game.get("startTimeTBD") is True
                    or game.get("validForLocking") is False
                    for game in games
                )
            )
        ):
            raise RuntimeError(
                f"ESPN has no reliable kickoff for NFL team {team['id']} in week {week}."
            )
        kickoff = min(dates) / 1000 if require_kickoffs and dates else None
        result[team["id"]] = {
            "abbreviation": team.get("abbrev"),
            "locked": kickoff is not None and kickoff <= now,
        }
    return result


def _locked(snapshot: dict, team_id: int, schedule: dict) -> frozenset[int]:
    settings = snapshot["settings"]["rosterSettings"]
    if settings.get("lineupLocktimeType") != "INDIVIDUAL_GAME":
        raise RuntimeError(
            "ESPN lineup optimization requires individual-game lineup locks."
        )
    games = _schedule(schedule, editable_week(snapshot))
    locked = set()
    for entry in _roster(snapshot, team_id):
        if (
            entry.get("lineupLocked") is True
            or entry["playerPoolEntry"].get("lineupLocked") is True
        ):
            locked.add(entry["playerId"])
        pro_team = entry["playerPoolEntry"]["player"]["proTeamId"]
        if pro_team == 0:
            continue
        if pro_team not in games:
            raise RuntimeError(f"ESPN has no schedule for NFL team {pro_team}.")
        if games[pro_team]["locked"]:
            locked.add(entry["playerId"])
    return frozenset(locked)


async def _apply_lineup(
    client: ESPNClient,
    baseline: dict,
    target: dict[int, int],
    locked: frozenset[int],
) -> dict:
    """Submit at most once; a failed verification never triggers a retry."""
    if not _LINEUP_LOCK.acquire(blocking=False):
        raise RuntimeError("Another ESPN lineup update is running in this process.")
    try:
        team_id = client.team_id
        if baseline["scoringPeriodId"] != editable_week(baseline):
            raise ValueError(
                "ESPN lineup changes require the current editable week's roster."
            )
        before = _assignments(baseline, team_id)
        if target.keys() != before.keys():
            raise ValueError("The planned lineup must contain every rostered player.")
        for entry in _roster(baseline, team_id):
            player_id = entry["playerId"]
            destination = target[player_id]
            if destination != before[player_id] and (
                player_id in locked
                or before[player_id] in _INACTIVE - {20}
                or destination
                not in entry["playerPoolEntry"]["player"]["eligibleSlots"]
            ):
                raise ValueError("The planned ESPN move is locked or ineligible.")
        _validate_slots(baseline, target)
        items = [
            {
                "type": "LINEUP",
                "playerId": player,
                "fromLineupSlotId": slot,
                "toLineupSlotId": target[player],
            }
            for player, slot in sorted(before.items())
            if slot != target[player]
        ]
        if items:
            week = editable_week(baseline)
            fresh, schedule = await asyncio.gather(
                client.league(), client.pro_schedule(week)
            )
            await client.verify_owner(fresh)
            if (
                _snapshot_key(fresh, team_id) != _snapshot_key(baseline, team_id)
                or _locked(fresh, team_id, schedule) != locked
            ):
                raise RuntimeError(
                    "The ESPN league, week, roster, or locks changed; nothing was submitted."
                )
            try:
                await client.post_lineup(items, week)
                verified = await client.league()
                await client.verify_owner(verified)
                if (
                    verified["id"] != baseline["id"]
                    or verified["seasonId"] != baseline["seasonId"]
                    or editable_week(verified) != week
                    or _assignments(verified, team_id) != target
                    or _snapshot_key(verified, team_id, lineup=False)
                    != _snapshot_key(baseline, team_id, lineup=False)
                ):
                    raise RuntimeError(
                        "ESPN's complete roster differs from the submitted lineup."
                    )
            except Exception as exc:
                raise RuntimeError(
                    "The ESPN lineup may have applied, but could not be verified. Inspect ESPN before retrying; no automatic retry was attempted."
                ) from exc
        return {
            "moves": items,
            "assignments_before": before,
            "assignments_after": target,
        }
    finally:
        _LINEUP_LOCK.release()
