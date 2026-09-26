"""The optimal weekly lineup: computed read-only, then applied via verified swaps."""

import asyncio
import math
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
from .props import (
    PropEstimate,
    WeeklyProps,
    _props_for_player,
    _week_props,
)
from .team import _FLEX_ELIGIBLE, _POINTS_FIELDS, _full_name, _points_field

_SCORING = {field: variant for variant, field in _POINTS_FIELDS.items()}
_STAT_FIELDS = {
    "passing_yards": "pass_yd",
    "passing_touchdowns": "pass_td",
    "rushing_yards": "rush_yd",
    "rushing_touchdowns": "rush_td",
    "receiving_yards": "rec_yd",
    "receiving_receptions": "rec",
    "receiving_touchdowns": "rec_td",
    "pass_int": "pass_int",
    "fum_lost": "fum_lost",
}
_DEFAULT_WEIGHTS = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "rec": 0.0,
    "pass_int": 0.0,
    "fum_lost": 0.0,
}


def _stat_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _weight_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _projection_stat(projection: object, stat: str) -> float | None:
    if not isinstance(projection, dict):
        return None
    field = _STAT_FIELDS.get(stat)
    return _stat_number(projection.get(field)) if field is not None else None


def _book_stat(props: dict[str, PropEstimate], stat: str) -> float | None:
    estimate = props.get(stat)
    if estimate is None:
        return None
    value = _stat_number(estimate["value"])
    if value is None:
        return None
    if estimate["kind"] == "line":
        return value
    if "touchdowns" in stat and value <= 1:
        # An anytime-TD price is P(at least one). Allow a small chance of a
        # second score without treating the price as a multi-TD over/under.
        return value + 0.15 * value * value
    return None


def _book_or_projection(book: float | None, projection: object, stat: str) -> float:
    SLEEPER_PENALTY = 0.80  # if the books aren't confident enough to set the line on something lets temper Sleeper's confidence
    sleeper_sprojected_stat = (
        _projection_stat(projection, stat) or 0.0
    ) * SLEEPER_PENALTY
    return book if book is not None else sleeper_sprojected_stat


def _stat_or_projection(
    props: dict[str, PropEstimate], projection: object, stat: str
) -> float:
    return _book_or_projection(_book_stat(props, stat), projection, stat)


def _touchdown_points(
    props: dict[str, PropEstimate],
    projection: object,
    weights: dict[str, float],
    default_rush_share: float,
) -> float:
    rush_book = _book_stat(props, "rushing_touchdowns")
    rec_book = _book_stat(props, "receiving_touchdowns")
    if rush_book is not None and rec_book is not None:
        return rush_book * weights["rush_td"] + rec_book * weights["rec_td"]
    overall = _book_stat(props, "touchdowns")
    if overall is not None:
        # A total-TD line and a position-specific anytime probability cannot
        # be subtracted: neither is an expected count of mutually exclusive TDs.
        rush = _projection_stat(projection, "rushing_touchdowns") or 0.0
        receiving = _projection_stat(projection, "receiving_touchdowns") or 0.0
        rush_share = (
            rush / (rush + receiving) if rush + receiving else default_rush_share
        )
        return overall * (
            rush_share * weights["rush_td"] + (1 - rush_share) * weights["rec_td"]
        )
    return (
        _book_or_projection(rush_book, projection, "rushing_touchdowns")
        * weights["rush_td"]
        + _book_or_projection(rec_book, projection, "receiving_touchdowns")
        * weights["rec_td"]
    )


def _scoring_weights(scoring_settings: object, scoring: str | None) -> dict[str, float]:
    settings = scoring_settings if isinstance(scoring_settings, dict) else {}
    resolved = _DEFAULT_WEIGHTS.copy()
    for field in resolved:
        value = _weight_number(settings.get(field))
        if value is not None:
            resolved[field] = value
    if scoring is not None:
        resolved["rec"] = {"std": 0.0, "half_ppr": 0.5, "ppr": 1.0}[scoring]
    return resolved


def _reception_bonus(scoring_settings: object, position: str) -> float:
    if not isinstance(scoring_settings, dict):
        return 0.0
    return _weight_number(scoring_settings.get(f"bonus_rec_{position.lower()}")) or 0.0


def _qb_prop_score(
    props: dict[str, PropEstimate],
    projection: object,
    scoring_settings: object,
    scoring: str | None,
) -> float | None:
    """Book-informed QB fantasy points from passing and rushing props."""
    passing_yards = _book_stat(props, "passing_yards")
    passing_tds = _book_stat(props, "passing_touchdowns")
    rushing_yards = _book_stat(props, "rushing_yards")
    rushing_tds = _book_stat(props, "rushing_touchdowns")
    weights = _scoring_weights(scoring_settings, scoring)
    if not any(
        value is not None and weights[field] != 0
        for value, field in (
            (passing_yards, "pass_yd"),
            (passing_tds, "pass_td"),
            (rushing_yards, "rush_yd"),
            (rushing_tds, "rush_td"),
            (_book_stat(props, "receiving_touchdowns"), "rec_td"),
            (_book_stat(props, "touchdowns"), "rush_td"),
            (_book_stat(props, "touchdowns"), "rec_td"),
        )
    ):
        return None
    return (
        _book_or_projection(passing_yards, projection, "passing_yards")
        * weights["pass_yd"]
        + _book_or_projection(passing_tds, projection, "passing_touchdowns")
        * weights["pass_td"]
        + _book_or_projection(rushing_yards, projection, "rushing_yards")
        * weights["rush_yd"]
        + _touchdown_points(props, projection, weights, default_rush_share=1)
        + (_projection_stat(projection, "pass_int") or 0.0) * weights["pass_int"]
        + (_projection_stat(projection, "fum_lost") or 0.0) * weights["fum_lost"]
    )


def _wr_te_prop_score(
    position: str,
    props: dict[str, PropEstimate],
    projection: object,
    scoring_settings: object,
    scoring: str | None,
) -> float | None:
    """Book-informed WR/TE fantasy points from receiving and scoring props."""
    weights = _scoring_weights(scoring_settings, scoring)
    reception_weight = weights["rec"] + _reception_bonus(scoring_settings, position)
    if not any(
        _book_stat(props, stat) is not None and weight != 0
        for stat, weight in (
            ("receiving_yards", weights["rec_yd"]),
            ("rushing_yards", weights["rush_yd"]),
            ("receiving_receptions", reception_weight),
            ("touchdowns", weights["rush_td"] or weights["rec_td"]),
            ("rushing_touchdowns", weights["rush_td"]),
            ("receiving_touchdowns", weights["rec_td"]),
        )
    ):
        return None
    return (
        _stat_or_projection(props, projection, "receiving_yards") * weights["rec_yd"]
        + _stat_or_projection(props, projection, "rushing_yards") * weights["rush_yd"]
        + _stat_or_projection(props, projection, "receiving_receptions")
        * reception_weight
        + _touchdown_points(props, projection, weights, default_rush_share=0)
        + (_projection_stat(projection, "fum_lost") or 0.0) * weights["fum_lost"]
    )


def _rb_prop_score(
    props: dict[str, PropEstimate],
    projection: object,
    scoring_settings: object,
    scoring: str | None,
) -> float | None:
    """Book-informed RB fantasy points from rushing, receiving, and TD props."""
    weights = _scoring_weights(scoring_settings, scoring)
    reception_weight = weights["rec"] + _reception_bonus(scoring_settings, "RB")
    if not any(
        _book_stat(props, stat) is not None and weight != 0
        for stat, weight in (
            ("rushing_yards", weights["rush_yd"]),
            ("receiving_yards", weights["rec_yd"]),
            ("receiving_receptions", reception_weight),
            ("touchdowns", weights["rush_td"] or weights["rec_td"]),
            ("rushing_touchdowns", weights["rush_td"]),
            ("receiving_touchdowns", weights["rec_td"]),
        )
    ):
        return None
    return (
        _stat_or_projection(props, projection, "rushing_yards") * weights["rush_yd"]
        + _stat_or_projection(props, projection, "receiving_yards") * weights["rec_yd"]
        + _stat_or_projection(props, projection, "receiving_receptions")
        * reception_weight
        + _touchdown_points(props, projection, weights, default_rush_share=0.5)
        + (_projection_stat(projection, "fum_lost") or 0.0) * weights["fum_lost"]
    )


def _composite_prop_score(
    position: str,
    props: dict[str, PropEstimate],
    projection: object,
    scoring_settings: object,
    scoring: str | None,
) -> float | None:
    """Score relevant props, or None when the position has no book market."""
    if position == "QB":
        return _qb_prop_score(props, projection, scoring_settings, scoring)
    if position == "RB":
        return _rb_prop_score(props, projection, scoring_settings, scoring)
    if position in {"WR", "TE"}:
        return _wr_te_prop_score(position, props, projection, scoring_settings, scoring)
    return None


def _eligible(positions: frozenset[str], slot: str) -> bool:
    """Whether a player with these fantasy positions may occupy the slot."""
    return slot in positions or bool(positions & _FLEX_ELIGIBLE.get(slot, set()))


def _rank(row: dict) -> tuple:
    """Book-informed score first, then Sleeper projection, with nulls last."""
    prop_score = row["prop_score"]
    points = row["projected_points"]
    return (
        prop_score is None,
        -prop_score if prop_score is not None else 0.0,
        points is None,
        -points if points is not None else 0.0,
        row["full_name"],
        row["player_id"],
    )


def _candidates(
    roster: dict,
    players: dict,
    projections: dict,
    points_field: str,
    markets: WeeklyProps,
    scoring_settings: object,
    scoring: str | None,
) -> list[dict]:
    """Rostered players who may start, best prop score first.

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
            raise RuntimeError(  # noqa: TRY004
                f"Sleeper has no player record for {player_id}."
            )
        full_name = _full_name(record, player_id)
        position = record.get("position")
        team = record.get("team")
        props = _props_for_player(
            markets, full_name, team if isinstance(team, str) else None
        )
        rows.append(
            {
                "player_id": player_id,
                "full_name": full_name,
                "team": team.upper() if isinstance(team, str) else None,
                "positions": frozenset(_fantasy_positions(record, player_id)),
                "prop_score": _composite_prop_score(
                    position if isinstance(position, str) else "",
                    props,
                    projections.get(player_id),
                    scoring_settings,
                    scoring,
                ),
                "projected_points": _projected_points(
                    projections.get(player_id), player_id, points_field
                ),
            }
        )
    return sorted(rows, key=_rank)


def _select(candidates: list[dict], slots: list[str]) -> list[dict | None]:
    """The highest-ranked legal starters for the slots, one per slot.

    Candidates are seated best rank first, and a player is kept only
    when everyone chosen so far can still occupy distinct slots. This greedy
    basis of the slot-assignment matroid picks the best ranked set; prop scores
    take priority over Sleeper projections. Free base-position slots are tried
    before flex slots, so the arrangement usually keeps flex slots for the
    overflow.
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


def _target_with_locks(
    candidates: list[dict],
    slots: list[str],
    starters: list[str],
    started_teams: frozenset[str],
) -> list[dict | None]:
    """Keep starters in games that have begun, then optimize open slots."""
    by_id = {row["player_id"]: row for row in candidates}
    locked = {
        j: by_id[player_id]
        for j, player_id in enumerate(starters)
        if player_id in by_id and by_id[player_id]["team"] in started_teams
    }
    open_indices = [j for j in range(len(slots)) if j not in locked]
    open_candidates = [row for row in candidates if row["team"] not in started_teams]
    open_slots = [slots[j] for j in open_indices]
    open_starters = [starters[j] for j in open_indices]
    chosen = [row for row in _select(open_candidates, open_slots) if row is not None]
    assigned = iter(_assign(chosen, open_slots, open_starters))
    return [locked[j] if j in locked else next(assigned) for j in range(len(slots))]


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
    """Report the highest-ranked legal lineup for one week.

    The return value contains `league_id`, `user_id`, `week`, `scoring`, one
    `lineup` entry per active slot in league order, and
    `total_projected_points`. Each entry holds the slot's name and the chosen
    player's `player_id`, `full_name`, `positions`, `prop_score`, and
    `projected_points`; slots no rostered player can fill hold `None` instead.
    `prop_score` is an approximate league-scored fantasy point total derived
    from available bookmaker lines, with Sleeper stat projections filling
    missing markets; it is `None` without a relevant prop. Players rank first
    by prop score (nulls last), then Sleeper projected points. The returned
    `total_projected_points` always sums Sleeper projections. `week` defaults
    to Sleeper's current editable lineup week (the same week
    `set_optimal_lineup` applies) and may be any
    week from 1 to 18; `scoring` selects the points variant like `team`.
    Past weeks use Sleeper projections alone because pregame markets have
    closed and historical odds require a paid SportsGameOdds plan.
    For the editable week, selected current starters keep their slots where
    possible; starters whose games have begun remain in their slots.
    Sleeper's public API and SportsGameOdds are read; no browser opens and no
    lineup changes.
    """
    async with httpx.AsyncClient() as client:
        state = await _get(client, "state/nfl")
        if not isinstance(state, dict):
            raise RuntimeError(  # noqa: TRY004
                "Sleeper did not return the current NFL state."
            )
        if week is None:
            week = _editable_week(state)
            editable_week = week
        else:
            try:
                editable_week = _editable_week(state)
            except (RuntimeError, ValueError):
                editable_week = None
        snapshot = (
            await _snapshot(client, league_id, user_id)
            if week == editable_week
            else await _team_snapshot(client, league_id, user_id, week)
        )
        if snapshot["week"] != week:
            raise RuntimeError(
                "Sleeper's editable week changed; retry the lineup read."
            )
        league = snapshot["league"]
        points_field = _points_field(league.get("scoring_settings"), scoring)
        markets: WeeklyProps
        started_teams: frozenset[str]
        if editable_week is not None and week < editable_week:
            # Past games have no pregame markets, and historical odds require
            # a paid SGO plan. Sleeper projections still rank these weeks.
            players, projections = await asyncio.gather(
                _players(client),
                _projections(client, snapshot["season"], snapshot["week"]),
            )
            markets, started_teams = {}, frozenset()
        else:
            players, projections, slate = await asyncio.gather(
                _players(client),
                _projections(client, snapshot["season"], snapshot["week"]),
                _week_props(client, snapshot["season"], snapshot["week"]),
            )
            markets, started_teams = slate
        slots = _lineup_slots(league)
        candidates = _candidates(
            snapshot["roster"],
            players,
            projections,
            points_field,
            markets,
            league.get("scoring_settings"),
            scoring,
        )
        starters = snapshot.get("starters")
        chosen = (
            _target_with_locks(candidates, slots, starters, started_teams)
            if starters is not None
            else _select(candidates, slots)
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
                        "prop_score": None,
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
                    "prop_score": player["prop_score"],
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

    The same prop-first ranking as `optimal_lineup` is used. Starters whose
    games have begun remain in their current slots, and other players from
    started games cannot enter. The current lineup is then transformed through
    the verified `swaps` helper:
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
        players, projections, slate = await asyncio.gather(
            _players(client),
            _projections(client, snapshot["season"], week),
            _week_props(client, snapshot["season"], week),
        )
        markets, started_teams = slate
        candidates = _candidates(
            snapshot["roster"],
            players,
            projections,
            points_field,
            markets,
            snapshot["league"].get("scoring_settings"),
            scoring,
        )
        # Candidates contain everything needed from these league-wide datasets.
        # Release them before Chromium adds its own browser and renderer memory.
        del players, projections
        target = _target_with_locks(
            candidates, slots, snapshot["starters"], started_teams
        )
        plan = _plan_swaps(
            snapshot["starters"],
            [player["player_id"] if player is not None else None for player in target],
        )
        if plan:
            _, latest_started_teams = await _week_props(
                client, snapshot["season"], week
            )
            if latest_started_teams != started_teams:
                raise RuntimeError(
                    "An NFL game began while planning the lineup; retry with a fresh slate."
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

        async def check_kickoff() -> None:
            _, latest_started = await _week_props(client, snapshot["season"], week)
            if latest_started != started_teams:
                raise RuntimeError(
                    "An NFL game began during the lineup exchanges; inspect Sleeper "
                    "and retry with a fresh slate."
                )

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
            before_exchange=check_kickoff,
        )
        result["swaps"] = [
            {"player_in": entering, "player_out": leaving} for entering, leaving in plan
        ]
        result["starters_after"] = batch["starters_after"]
        return result
