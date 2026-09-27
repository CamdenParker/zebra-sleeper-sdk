"""Read-only ESPN roster props beside native weekly stat projections."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx

from ..optimize import _projection_stat
from ..prop_report import _REPORT_STATS
from ..props import WeeklyProps, _props_for_player, _week_props
from ._api import ESPNClient, editable_week, projected_stats, scoring_settings
from .auth import DEFAULT_AUTH_PATH
from .lineup import _roster, _schedule
from .optimize import _POSITIONS


async def team_props(
    league_id: int,
    team_id: int,
    *,
    season: int | None = None,
    week: int | None = None,
    auth_path: Path = DEFAULT_AUTH_PATH,
) -> dict:
    """Show every rostered player's props, source books, and ESPN projections.

    Missing props or projections are ``None``. Bench and reserve players are
    included. Past weeks use ESPN's historical roster and native projections
    without requesting pregame props. This report submits no lineup changes
    and writes no files.
    """
    if week is not None and (type(week) is not int or not 1 <= week <= 18):
        raise ValueError("week must be between 1 and 18.")
    async with ESPNClient(
        league_id, team_id, season=season, auth_path=auth_path
    ) as client:
        snapshot = await client.league()
        current_week = editable_week(snapshot)
        selected_week = current_week if week is None else week
        if selected_week != current_week:
            snapshot = await client.league(selected_week)
        if selected_week < current_week:
            schedule = await client.pro_schedule(selected_week)
            markets: WeeklyProps = {}
        else:
            async with httpx.AsyncClient() as props_client:
                schedule, slate = await asyncio.gather(
                    client.pro_schedule(selected_week),
                    _week_props(props_client, str(snapshot["seasonId"]), selected_week),
                )
                markets = slate[0]

        teams = _schedule(schedule, selected_week, require_kickoffs=False)
        report = []
        for entry in _roster(snapshot, team_id):
            player = entry["playerPoolEntry"]["player"]
            if player["id"] != entry["playerId"]:
                raise RuntimeError("ESPN returned inconsistent roster player IDs.")
            team = teams.get(player["proTeamId"], {}).get("abbreviation")
            position = _POSITIONS.get(player["defaultPositionId"])
            projection, points = projected_stats(
                player, selected_week, season=snapshot["seasonId"]
            )
            props = _props_for_player(markets, player["fullName"], team)
            stats = dict.fromkeys(
                (*_REPORT_STATS.get(position or "", ()), *sorted(props))
            )
            report.append(
                {
                    "player_id": entry["playerId"],
                    "full_name": player["fullName"],
                    "team": team,
                    "position": position,
                    "projected_points": points,
                    "stats": {
                        stat: {
                            "prop": props.get(stat),
                            "espn_projection": _projection_stat(projection, stat),
                        }
                        for stat in stats
                    },
                }
            )
        out = {
            "league_id": snapshot["id"],
            "team_id": team_id,
            "season": snapshot["seasonId"],
            "week": selected_week,
            "scoring": "ppr" if scoring_settings(snapshot)["rec"] == 1 else "league",
            "players": report,
        }

        today = datetime.now(UTC).strftime("%Y-%m-%d")

        async with await anyio.open_file(f"data/espn_props_{today}.json", "w") as f:
            await f.write(json.dumps(out, indent=4))

        return out
