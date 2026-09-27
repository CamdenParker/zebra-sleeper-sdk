"""ESPN reads, projection translation, and authenticated roster transactions."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import httpx

from .auth import (
    DEFAULT_AUTH_PATH,
    _AccessDenied,
    _identity,
    _load_cookies,
    _NotOwner,
    _validate_cookies,
    _validate_ids,
)

_READ_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
_WRITE_URL = "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons"
_STAT_FIELDS = {
    3: "pass_yd",
    4: "pass_td",
    20: "pass_int",
    24: "rush_yd",
    25: "rush_td",
    41: "rec",
    42: "rec_yd",
    43: "rec_td",
    53: "rec",
    72: "fum_lost",
}
_SCORING_FIELDS = {stat_id: (field, 1) for stat_id, field in _STAT_FIELDS.items()}
for _field, _ids in (
    ("pass_yd", (5, 6, 7, 8, 9, 10)),
    ("rush_yd", (27, 28, 29, 30, 31, 32)),
    ("rec_yd", (47, 48, 49, 50, 51, 52)),
):
    _SCORING_FIELDS.update(
        (stat_id, (_field, divisor))
        for stat_id, divisor in zip(_ids, (5, 10, 20, 25, 50, 100), strict=True)
    )
_SCORING_FIELDS.update({54: ("rec", 5), 55: ("rec", 10)})


def _season(season: int | None) -> int:
    today = datetime.now(UTC).date()
    return season if season is not None else today.year - (today.month < 3)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def editable_week(snapshot: dict) -> int:
    """ESPN's editable scoring period, checked against the league status."""
    status = snapshot.get("status")
    if not isinstance(status, dict):
        raise RuntimeError("ESPN returned malformed league status.")  # noqa: TRY004
    week = status.get("transactionScoringPeriod", snapshot.get("scoringPeriodId"))
    if type(week) is not int or not 1 <= week <= 18:
        raise RuntimeError(
            "ESPN's current editable NFL week cannot be determined safely."
        )
    return week


def projected_stats(
    player: dict, week: int, *, season: int | None = None
) -> tuple[dict[str, float], float | None]:
    """Translate one weekly ESPN projection into the shared stat vocabulary."""
    records = player.get("stats", [])
    if not isinstance(records, list) or any(
        not isinstance(row, dict) for row in records
    ):
        raise RuntimeError("ESPN returned malformed player projections.")
    matches = [
        row
        for row in records
        if row.get("statSourceId") == 1
        and row.get("statSplitTypeId") == 1
        and row.get("scoringPeriodId") == week
        and row.get("seasonId") == _season(season)
    ]
    if not matches:
        return {}, None
    if len(matches) != 1 or not isinstance(matches[0].get("stats"), dict):
        raise RuntimeError("ESPN returned ambiguous weekly player projections.")
    source = matches[0]
    stats = {}
    for stat_id, field in _STAT_FIELDS.items():
        value = source["stats"].get(str(stat_id))
        if value is not None:
            number = _number(value)
            if number is None or number < 0:
                raise RuntimeError("ESPN returned an invalid projected statistic.")
            # Receptions have two ESPN IDs; weekly player stats use 41.
            if field not in stats or stat_id != 53:
                stats[field] = number
    points = source.get("appliedTotal")
    if points is not None and _number(points) is None:
        raise RuntimeError("ESPN returned invalid projected fantasy points.")
    return stats, _number(points)


def scoring_settings(snapshot: dict) -> dict[str, float]:
    """Translate additive scoring weights; omitted ESPN stats score zero."""
    items = snapshot.get("settings", {}).get("scoringSettings", {}).get("scoringItems")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError("ESPN returned malformed scoring settings.")
    result = dict.fromkeys(_STAT_FIELDS.values(), 0.0)
    seen = set()
    for item in items:
        stat_id = item.get("statId")
        points = _number(item.get("points"))
        if type(stat_id) is not int or points is None:
            raise RuntimeError("ESPN returned an invalid scoring weight.")
        if stat_id in seen:
            raise RuntimeError("ESPN returned duplicate scoring weights.")
        seen.add(stat_id)
        mapped = _SCORING_FIELDS.get(stat_id)
        if mapped is not None:
            field, divisor = mapped
            result[field] += points / divisor
    return result


class ESPNClient:
    """An isolated authenticated session scoped to one ESPN league and team."""

    def __init__(
        self,
        league_id: int,
        team_id: int,
        *,
        season: int | None = None,
        auth_path: Path = DEFAULT_AUTH_PATH,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.season = _season(season)
        _validate_ids(league_id, team_id, self.season)
        self.league_id, self.team_id = league_id, team_id
        self._cookies = (
            _validate_cookies(cookies)
            if cookies is not None
            else _load_cookies(auth_path)
        )
        jar = httpx.Cookies()
        for name, value in self._cookies.items():
            jar.set(name, value, domain=".espn.com", path="/")
        self._client = httpx.AsyncClient(
            cookies=jar,
            timeout=httpx.Timeout(20, connect=10),
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Origin": "https://fantasy.espn.com",
                "Referer": f"https://fantasy.espn.com/football/team?leagueId={league_id}&teamId={team_id}",
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    def _url(self, *, write: bool = False) -> str:
        base = _WRITE_URL if write else _READ_URL
        return f"{base}/{self.season}/segments/0/leagues/{self.league_id}"

    async def _get(self, url: str, params: list[tuple[str, str]]) -> dict:
        response = await self._client.get(
            url, params=[*params, ("_espn_sdk_read", str(time.time_ns()))]
        )
        if response.status_code in (401, 403):
            raise _AccessDenied(
                "ESPN denied access. Run ESPN login again for this league."
            )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("ESPN returned an invalid API response.")  # noqa: TRY004
        return value

    async def league(
        self, week: int | None = None, *, views: tuple[str, ...] | None = None
    ) -> dict:
        if week is not None and (type(week) is not int or not 1 <= week <= 18):
            raise ValueError("week must be an integer between 1 and 18.")
        params = [
            ("view", view)
            for view in views
            or ("mSettings", "mRoster", "mTeam", "mStatus", "mMatchup")
        ]
        if week is not None:
            params.append(("scoringPeriodId", str(week)))
        value = await self._get(self._url(), params)
        if value.get("id") != self.league_id or value.get("seasonId") != self.season:
            raise RuntimeError("ESPN returned a different league or season.")
        return value

    async def pro_schedule(self, week: int) -> dict:
        if type(week) is not int or not 1 <= week <= 18:
            raise ValueError("week must be an integer between 1 and 18.")
        return await self._get(
            f"{_READ_URL}/{self.season}", [("view", "proTeamSchedules_wl")]
        )

    async def verify_owner(self, snapshot: dict) -> None:
        if (
            snapshot.get("id") != self.league_id
            or snapshot.get("seasonId") != self.season
        ):
            raise RuntimeError("ESPN ownership cannot be verified for this league.")
        teams = snapshot.get("teams")
        if not isinstance(teams, list) or any(
            not isinstance(team, dict) for team in teams
        ):
            raise RuntimeError("ESPN returned malformed team ownership.")
        matches = [team for team in teams if team.get("id") == self.team_id]
        if len(matches) != 1 or not isinstance(matches[0].get("owners"), list):
            raise RuntimeError(
                "ESPN has no unique requested team with owner information."
            )
        owners = {_identity(owner) for owner in matches[0]["owners"]}
        if _identity(self._cookies["SWID"]) not in owners:
            raise _NotOwner("The signed-in ESPN account does not own this team.")

    async def post_lineup(self, items: list[dict], scoring_period_id: int) -> dict:
        """Submit once; the guarded lineup caller owns fresh roster and lock checks."""
        if type(scoring_period_id) is not int or not 1 <= scoring_period_id <= 18:
            raise ValueError("scoring_period_id must be an integer between 1 and 18.")
        if not items or any(
            not isinstance(item, dict)
            or item.get("type") != "LINEUP"
            or any(
                type(item.get(field)) is not int
                for field in ("playerId", "fromLineupSlotId", "toLineupSlotId")
            )
            for item in items
        ):
            raise ValueError("ESPN roster transactions require valid LINEUP items.")
        response = await self._client.post(
            self._url(write=True) + "/transactions/",
            json={
                "isLeagueManager": False,
                "teamId": self.team_id,
                "type": "ROSTER",
                "memberId": self._cookies["SWID"],
                "scoringPeriodId": scoring_period_id,
                "executionType": "EXECUTE",
                "items": items,
            },
        )
        if response.status_code in (401, 403):
            raise RuntimeError("ESPN denied the lineup change; authenticate again.")
        response.raise_for_status()
        # Transaction bodies can echo memberId (SWID); expose no response body.
        # The caller verifies the actual roster rather than trusting an acknowledgement.
        return {"status_code": response.status_code}
