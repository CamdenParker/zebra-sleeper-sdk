"""Pregame NFL player props from SportsGameOdds.

The free tier counts returned events, so one weekly slate is shared across
players and cached for ten minutes. Missing props are normal; request failures
raise instead of masquerading as an empty slate.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Literal, TypedDict

import httpx

_URL = "https://api.sportsgameodds.com/v2/events"
_STATS = frozenset(
    {
        "passing_yards",
        "passing_touchdowns",
        "rushing_yards",
        "rushing_touchdowns",
        "receiving_yards",
        "receiving_receptions",
        "receiving_touchdowns",
        "touchdowns",
    }
)
_CACHE_SECONDS = 600
_MAX_PAGES = 3  # Bound requests when the API returns an unexpected slate.
_CACHE: dict[tuple[str, int], tuple[float, WeeklyProps, frozenset[str]]] = {}
_FETCH_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


class PropEstimate(TypedDict):
    """A bookmaker proxy for a player's expected full-game stat.

    ``kind='line'`` is the posted over/under threshold. ``probability`` is
    the implied chance of at least one touchdown, used when no useful O/U is
    posted. It is not an expected number of touchdowns.
    """

    value: float
    kind: Literal["line", "probability"]
    source: Literal["book_over_under", "book_median", "fair_odds", "book_odds"]


class PlayerMarket(TypedDict):
    name: str
    team: str | None
    props: dict[str, PropEstimate]


WeeklyProps = dict[str, PlayerMarket]  # SportsGameOdds playerID -> market


def _api_key() -> str:
    key = os.environ.get("SPORTSGAMEODDS_API_KEY", "").strip()
    if key:
        return key
    for env_path in dict.fromkeys(
        (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env")
    ):
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                name, separator, value = line.removeprefix("export ").partition("=")
                if separator and name.strip() == "SPORTSGAMEODDS_API_KEY":
                    key = value.strip().strip("\"'")
                    if key:
                        return key
    raise RuntimeError("SPORTSGAMEODDS_API_KEY is not configured.")


def _week_bounds(season: str, week: int) -> tuple[datetime, datetime]:
    if (
        not re.fullmatch(r"\d{4}", season)
        or type(week) is not int
        or not 1 <= week <= 18
    ):
        raise ValueError("season must be a four-digit year and week must be 1–18.")
    # A Wednesday-noon UTC boundary includes US Wednesday evening games and
    # Tuesday night reschedules without attributing them to the prior week.
    september_first = datetime(int(season), 9, 1, tzinfo=UTC)
    labor_day = september_first + timedelta(days=(-september_first.weekday()) % 7)
    start = labor_day + timedelta(days=2 + 7 * (week - 1), hours=12)
    return start, start + timedelta(days=7)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _implied_probability(value: object) -> float | None:
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return None
    try:
        price = float(value)
    except ValueError:
        return None
    if not math.isfinite(price) or price == 0:
        return None
    return 100 / (price + 100) if price > 0 else -price / (100 - price)


def _active_books(odd: dict) -> list[dict]:
    by_bookmaker = odd.get("byBookmaker")
    if not isinstance(by_bookmaker, dict):
        return []
    return [
        book
        for book in by_bookmaker.values()
        if isinstance(book, dict) and book.get("available") is True
    ]


def _estimate(odd: dict) -> PropEstimate | None:
    books = _active_books(odd)
    if odd.get("started") or odd.get("ended") or odd.get("cancelled") or not books:
        return None
    bet_type = odd.get("betTypeID")
    if bet_type == "ou" and odd.get("sideID") in ("over", "under"):
        lines = [
            value
            for book in books
            if (value := _number(book.get("overUnder"))) is not None
        ]
        if lines:
            return {
                "value": float(median(lines)),
                "kind": "line",
                "source": "book_median",
            }
        line = _number(odd.get("bookOverUnder"))
        if line is not None:
            return {"value": line, "kind": "line", "source": "book_over_under"}
    elif bet_type == "yn" and odd.get("sideID") == "yes":
        if odd.get("fairOddsAvailable") is True:
            probability = _implied_probability(odd.get("fairOdds"))
            if probability is not None:
                return {
                    "value": probability,
                    "kind": "probability",
                    "source": "fair_odds",
                }
        probability = _implied_probability(odd.get("bookOdds"))
        if probability is not None:
            return {"value": probability, "kind": "probability", "source": "book_odds"}
    return None


def _event_time(event: dict) -> datetime | None:
    status = event.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("startsAt"), str):
        return None
    try:
        start = datetime.fromisoformat(status["startsAt"])
    except ValueError:
        return None
    return start if start.tzinfo is not None else None


def _team_short(event: dict, team_id: object) -> str | None:
    teams = event.get("teams")
    if not isinstance(teams, dict):
        return None
    for team in teams.values():
        if isinstance(team, dict) and team.get("teamID") == team_id:
            names = team.get("names")
            if isinstance(names, dict) and isinstance(names.get("short"), str):
                return _team_code(names["short"])
    return None


def _team_code(code: str) -> str:
    """Use Sleeper's abbreviation for the Rams, which SGO calls LA."""
    code = code.upper()
    return "LAR" if code == "LA" else code


def _event_team_codes(event: dict) -> set[str]:
    teams = event.get("teams")
    if not isinstance(teams, dict):
        return set()
    codes = set()
    for team in teams.values():
        if not isinstance(team, dict):
            continue
        names = team.get("names")
        short = names.get("short") if isinstance(names, dict) else None
        if isinstance(short, str):
            codes.add(_team_code(short))
    return codes


def _add_event_props(markets: WeeklyProps, event: dict) -> None:
    players, odds = event.get("players"), event.get("odds")
    if not isinstance(players, dict) or not isinstance(odds, dict):
        return
    for odd in odds.values():
        if not isinstance(odd, dict):
            continue
        player_id = odd.get("playerID") or odd.get("statEntityID")
        stat = odd.get("statID")
        if (
            not isinstance(player_id, str)
            or not isinstance(stat, str)
            or stat not in _STATS
            or odd.get("periodID") != "game"
        ):
            continue
        player = players.get(player_id)
        if not isinstance(player, dict) or not isinstance(player.get("name"), str):
            continue
        estimate = _estimate(odd)
        if estimate is None:
            continue
        market = markets.setdefault(
            player_id,
            {
                "name": player["name"],
                "team": _team_short(event, player.get("teamID")),
                "props": {},
            },
        )
        current = market["props"].get(stat)
        # An anytime-TD price is more informative than a TD threshold,
        # including a possible alternate line above 0.5.
        if current is None or (
            stat.endswith("touchdowns")
            and estimate["kind"] == "probability"
            and current["kind"] == "line"
        ):
            market["props"][stat] = estimate


async def _fetch_week_props(
    client: httpx.AsyncClient, season: str, week: int
) -> tuple[WeeklyProps, frozenset[str]]:
    """Fetch a weekly NFL slate once, retaining current pregame player markets.

    The date filter keeps the number of billable event objects small. Every
    event is checked locally; incomplete pagination raises rather than
    returning a partial week. Started teams are returned for lineup locks.
    Cache entries expire at the next kickoff, if sooner than ten minutes.
    """
    start, end = _week_bounds(season, week)
    cache_key = (season, week)
    cached = _CACHE.get(cache_key)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1], cached[2]

    key = _api_key()
    markets: WeeklyProps = {}
    started_teams: set[str] = set()
    next_kickoff: datetime | None = None
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, str | int] = {
            "leagueID": "NFL",
            "startsAfter": (start - timedelta(seconds=1)).isoformat(),
            "startsBefore": end.isoformat(),
            "limit": 25,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            response = await client.get(
                _URL,
                params=params,
                headers={"x-api-key": key},
                timeout=httpx.Timeout(20.0, connect=10.0),
            )
            if response.status_code == 404 and cursor is not None:
                break
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(
                "SportsGameOdds player props could not be fetched."
            ) from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError(
                "SportsGameOdds returned an unsuccessful props response."
            )
        events = payload.get("data")
        if not isinstance(events, list):
            raise RuntimeError(  # noqa: TRY004
                "SportsGameOdds returned invalid event data."
            )
        for event in events:
            if not isinstance(event, dict) or event.get("leagueID") != "NFL":
                continue
            event_start = _event_time(event)
            status = event.get("status")
            if (
                event_start is None
                or not start <= event_start < end
                or not isinstance(status, dict)
                or status.get("cancelled")
            ):
                continue
            now = datetime.now(UTC)
            started = any(
                status.get(flag)
                for flag in ("started", "ended", "completed", "finalized")
            ) or (event_start <= now and not status.get("delayed"))
            if started:
                started_teams.update(_event_team_codes(event))
            else:
                retry_at = (
                    now + timedelta(seconds=60)
                    if status.get("delayed") and event_start <= now
                    else event_start
                )
                if next_kickoff is None or retry_at < next_kickoff:
                    next_kickoff = retry_at
                _add_event_props(markets, event)
        cursor_value = payload.get("nextCursor")
        if not cursor_value:
            break
        if not isinstance(cursor_value, str) or cursor_value == cursor:
            raise RuntimeError("SportsGameOdds returned an invalid pagination cursor.")
        cursor = cursor_value
    else:
        raise RuntimeError(
            "SportsGameOdds returned too many event pages for the free-tier props fetch."
        )
    lifetime = _CACHE_SECONDS
    if next_kickoff is not None:
        lifetime = min(
            lifetime, max(0.0, (next_kickoff - datetime.now(UTC)).total_seconds())
        )
    locked = frozenset(started_teams)
    _CACHE[cache_key] = (time.monotonic() + lifetime, markets, locked)
    return markets, locked


async def _week_props(
    client: httpx.AsyncClient, season: str, week: int
) -> tuple[WeeklyProps, frozenset[str]]:
    """Share a completed or in-flight weekly slate across concurrent callers."""
    cache_key = (season, week)
    cached = _CACHE.get(cache_key)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1], cached[2]
    lock = _FETCH_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        return await _fetch_week_props(client, season, week)


def _normalized_name(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).casefold()
    words = re.findall(r"[a-z0-9]+", folded)
    while words and words[-1] in {"jr", "sr", "ii", "iii", "iv"}:
        words.pop()
    return " ".join(words)


def _props_for_player(
    markets: WeeklyProps, full_name: str, team: str | None = None
) -> dict[str, PropEstimate]:
    """Resolve a Sleeper player by name and, when needed, team abbreviation.

    Ambiguous or unmatched names return no props rather than borrowing another
    player's odds. Pass ``record['team']`` from Sleeper when available.
    """
    name = _normalized_name(full_name)
    matches = [
        market
        for market in markets.values()
        if _normalized_name(market["name"]) == name
    ]
    if team:
        matches = [market for market in matches if market["team"] == _team_code(team)]
    return matches[0]["props"].copy() if len(matches) == 1 else {}


async def player_props(
    full_name: str, *, season: str, week: int, team: str | None = None
) -> dict[str, PropEstimate]:
    """Return available full-game props for one NFL player in a given week.

    Each value says whether it is a bookmaker O/U line or an anytime-TD
    probability. Empty means no suitable pregame market matched this player.
    SportsGameOdds errors raise ``RuntimeError``. The API key is read from
    ``SPORTSGAMEODDS_API_KEY`` or ``.env`` in the working directory (falling
    back to the source project's root for editable installs).
    """
    async with httpx.AsyncClient() as client:
        markets, _ = await _week_props(client, season, week)
    return _props_for_player(markets, full_name, team)
