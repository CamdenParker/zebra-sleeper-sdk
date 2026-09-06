"""Private reads from Sleeper's documented, unauthenticated API."""

import asyncio
import time

import httpx


_BASE_URL = "https://api.sleeper.app/v1"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_SLOTS = {
    "QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "REC_FLEX",
    "WRRB_FLEX", "K", "DEF", "DL", "LB", "DB", "IDP_FLEX",
}


async def _get(client: httpx.AsyncClient, path: str):
    # Observed CDN responses can cache lineups for five minutes. A unique query
    # forces a fresh read; Cache-Control alone does not bypass that cache.
    response = await client.get(
        f"{_BASE_URL}/{path}",
        params={"_sleeper_sdk_read": str(time.time_ns())},
        headers={"Cache-Control": "no-cache"},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _player_ids(value, field: str, *, empty_slots: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(player, str) or not player or player != player.strip()
        for player in value
    ):
        raise RuntimeError(f"Sleeper returned an invalid {field} list.")
    occupied = [player for player in value if not empty_slots or player != "0"]
    if len(occupied) != len(set(occupied)) or (not empty_slots and "0" in value):
        raise RuntimeError(f"Sleeper returned duplicate or invalid IDs in {field}.")
    return list(value)


def _lineup(rows, roster_id: int, source: str) -> list[str]:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"Sleeper did not return valid {source} lineup data.")
    matches = [row for row in rows if row.get("roster_id") == roster_id]
    if len(matches) != 1:
        raise RuntimeError(f"The roster has no unique {source} lineup for the current week.")
    return _player_ids(matches[0].get("starters"), f"{source} starters", empty_slots=True)


async def _starters(
    client: httpx.AsyncClient, league_id: str, roster_id: int, week: int,
    *, source: str = "matchup",
) -> list[str]:
    """Read the initially selected lineup source without switching on failure."""
    if source not in ("matchup", "roster"):
        raise ValueError("Unknown lineup source.")
    resource = "rosters" if source == "roster" else f"matchups/{week}"
    rows = await _get(client, f"league/{league_id}/{resource}")
    return _lineup(rows, roster_id, source)


async def _snapshot(
    client: httpx.AsyncClient, league_id: str, user_id: str
) -> dict:
    """Resolve the user's current NFL lineup, rejecting unsupported/ambiguous data."""
    if not isinstance(league_id, str) or not league_id.isascii() or not league_id.isdigit():
        raise ValueError("league_id must be a numerical Sleeper ID string.")
    if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit():
        raise ValueError("user_id must be a numerical Sleeper ID string.")
    league, rosters, state = await asyncio.gather(
        _get(client, f"league/{league_id}"),
        _get(client, f"league/{league_id}/rosters"),
        _get(client, "state/nfl"),
    )
    if not isinstance(league, dict) or league.get("league_id") != league_id:
        raise RuntimeError("Sleeper did not return the requested league.")
    if not isinstance(state, dict):
        raise RuntimeError("Sleeper did not return the current NFL state.")
    if (
        league.get("sport") != "nfl"
        or league.get("season_type") != "regular"
    ):
        raise ValueError("Only NFL regular-season leagues are supported.")
    settings = league.get("settings")
    if (
        not isinstance(settings, dict)
        or settings.get("best_ball", 0) != 0
        or settings.get("type") not in (0, 1, 2)
    ):
        raise ValueError("Only Classic redraft, keeper, and dynasty lineups are supported.")
    # A renewed dynasty can retain an editable roster before its rookie draft.
    if league.get("status") != "in_season" and not (
        league.get("status") == "pre_draft" and settings.get("type") == 2
    ):
        raise ValueError("The league must be active, or a renewed pre-draft dynasty.")
    season = state.get("season")
    if not isinstance(season, str) or not season.isdigit() or league.get("season") != season:
        raise ValueError("The league must belong to the current NFL season.")

    # display_week is the week shown in Sleeper's UI, and can advance before week.
    week, nfl_week = state.get("display_week"), state.get("week")
    if type(week) is not int or not 1 <= week <= 18:
        raise RuntimeError("Sleeper has no supported current lineup week.")
    season_type = state.get("season_type")
    if season_type == "pre":
        if week != 1:
            raise ValueError("Only the week 1 lineup is supported before the NFL season.")
    elif (
        season_type != "regular" or type(nfl_week) is not int
        or not 1 <= nfl_week <= 18 or week not in (nfl_week, nfl_week + 1)
    ):
        raise ValueError("The current editable NFL week cannot be determined safely.")

    positions = league.get("roster_positions")
    if not isinstance(positions, list) or any(
        not isinstance(position, str) or position not in _SLOTS | {"BN"}
        for position in positions
    ):
        raise ValueError("The league has an unsupported roster slot format.")
    slots = [position for position in positions if position != "BN"]
    if not slots:
        raise ValueError("The league has no active lineup slots.")
    if not isinstance(rosters, list) or any(not isinstance(row, dict) for row in rosters):
        raise RuntimeError("Sleeper did not return league rosters.")
    owned = []
    for row in rosters:
        co_owners = row.get("co_owners")
        if co_owners is None:
            co_owners = []
        if not isinstance(co_owners, list) or any(not isinstance(owner, str) for owner in co_owners):
            raise RuntimeError("Sleeper returned invalid roster co-owner data.")
        if row.get("owner_id") == user_id or user_id in co_owners:
            owned.append(row)
    if len(owned) != 1:
        raise ValueError("The logged-in user must own or co-own exactly one roster in the league.")
    roster = owned[0]
    roster_id = roster.get("roster_id")
    if type(roster_id) is not int or roster_id < 1:
        raise RuntimeError("Sleeper returned an invalid roster ID.")
    players = _player_ids(roster.get("players"), "roster players")
    reserve = _player_ids([] if roster.get("reserve") is None else roster["reserve"], "reserve players")
    taxi = _player_ids([] if roster.get("taxi") is None else roster["taxi"], "taxi players")
    matchups = await _get(client, f"league/{league_id}/matchups/{week}")
    source = "matchup"
    # Sleeper's web UI uses the retained roster before a dynasty rookie draft.
    # Select this source explicitly, only while no weekly matchups exist at all.
    if matchups == [] and league.get("status") == "pre_draft" and settings.get("type") == 2:
        source = "roster"
    starters = _lineup(rosters if source == "roster" else matchups, roster_id, source)
    active = set(starters) - {"0"}
    if len(starters) != len(slots) or not active <= set(players):
        raise RuntimeError("The weekly lineup does not match the current roster and slots.")
    if active & (set(reserve) | set(taxi)):
        raise RuntimeError("The weekly lineup includes an IR or taxi player; refresh Sleeper first.")
    return {
        "league": league,
        "roster": roster,
        "roster_id": roster_id,
        "week": week,
        "starters": starters,
        "slots": slots,
        "source": source,
    }


def _expected_starters(snapshot: dict, a: str, b: str) -> list[str]:
    """Validate a same-roster swap and preserve every unaffected lineup slot."""
    for player in (a, b):
        if not isinstance(player, str) or not player or player == "0" or player != player.strip():
            raise ValueError("Player IDs must be nonempty strings, not names or empty-slot IDs.")
    if a == b:
        raise ValueError("Choose two different players to swap.")
    roster = snapshot["roster"]
    players = roster["players"]
    if a not in players or b not in players:
        raise ValueError("Both players must belong to the logged-in user's roster.")
    inactive = (roster.get("reserve") or []) + (roster.get("taxi") or [])
    if a in inactive or b in inactive:
        raise ValueError("IR and taxi squad moves are not supported.")
    starters = snapshot["starters"]
    if a not in starters and b not in starters:
        raise ValueError("At least one player must occupy an active lineup slot.")
    return [b if player == a else a if player == b else player for player in starters]
