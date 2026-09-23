"""Read-only roster view of sportsbook props and Sleeper stat projections."""

import asyncio
import json
from datetime import UTC, datetime

import anyio
import httpx

from ._api import (
    _editable_week,
    _get,
    _players,
    _projected_points,
    _projections,
    _team_snapshot,
)
from .optimize import _projection_stat
from .props import _props_for_player, _week_props
from .team import _full_name, _points_field

_REPORT_STATS = {
    "QB": (
        "passing_yards",
        "passing_touchdowns",
        "rushing_yards",
        "rushing_touchdowns",
        "receiving_touchdowns",
        "touchdowns",
        "pass_int",
        "fum_lost",
    ),
    "RB": (
        "rushing_yards",
        "receiving_yards",
        "receiving_receptions",
        "rushing_touchdowns",
        "receiving_touchdowns",
        "touchdowns",
        "fum_lost",
    ),
    "WR": (
        "receiving_yards",
        "receiving_receptions",
        "rushing_yards",
        "rushing_touchdowns",
        "receiving_touchdowns",
        "touchdowns",
        "fum_lost",
    ),
    "TE": (
        "receiving_yards",
        "receiving_receptions",
        "rushing_yards",
        "rushing_touchdowns",
        "receiving_touchdowns",
        "touchdowns",
        "fum_lost",
    ),
}


async def team_props(
    league_id: str,
    user_id: str,
    *,
    week: int | None = None,
    scoring: str | None = None,
) -> dict:
    """Show each rostered player's props beside the corresponding projections.

    Missing bookmaker props are explicitly ``None``. Sleeper stat projections
    stay in a separate field even when a book prop is present. The default is
    the current editable week, matching ``optimal_lineup``. Past weeks use
    Sleeper projections alone because pregame markets are unavailable.
    """
    async with httpx.AsyncClient() as client:
        state = await _get(client, "state/nfl")
        if not isinstance(state, dict):
            raise RuntimeError(  # noqa: TRY004
                "Sleeper did not return the current NFL state."
            )
        editable_week = _editable_week(state)
        snapshot = await _team_snapshot(
            client, league_id, user_id, editable_week if week is None else week
        )
        season, selected_week = snapshot["season"], snapshot["week"]
        if selected_week < editable_week:
            players, projections = await asyncio.gather(
                _players(client), _projections(client, season, selected_week)
            )
            markets = {}
        else:
            players, projections, slate = await asyncio.gather(
                _players(client),
                _projections(client, season, selected_week),
                _week_props(client, season, selected_week),
            )
            markets, _ = slate

    points_field = _points_field(snapshot["league"].get("scoring_settings"), scoring)
    report = []
    for player_id in snapshot["roster"]["players"]:
        record = players.get(player_id)
        if not isinstance(record, dict):
            raise RuntimeError(  # noqa: TRY004
                f"Sleeper has no player record for {player_id}."
            )
        full_name = _full_name(record, player_id)
        team = record.get("team")
        raw_position = record.get("position")
        position = raw_position if isinstance(raw_position, str) else None
        props = _props_for_player(
            markets, full_name, team if isinstance(team, str) else None
        )
        projection = projections.get(player_id)
        stats = dict.fromkeys((*_REPORT_STATS.get(position or "", ()), *sorted(props)))
        report.append(
            {
                "player_id": player_id,
                "full_name": full_name,
                "team": team if isinstance(team, str) else None,
                "position": position,
                "projected_points": _projected_points(
                    projection, player_id, points_field
                ),
                "stats": {
                    stat: {
                        "prop": props.get(stat),
                        "sleeper_projection": _projection_stat(projection, stat),
                    }
                    for stat in stats
                },
            }
        )
    out = {
        "league_id": league_id,
        "user_id": user_id,
        "season": season,
        "week": selected_week,
        "scoring": points_field.removeprefix("pts_"),
        "players": report,
    }

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    async with await anyio.open_file(f"data/props_{today}.json", "w") as f:
        await f.write(json.dumps(out, indent=4))

    return out
