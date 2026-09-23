"""Read-only report of every rostered player's slots and weekly projections."""

import asyncio

import httpx

from ._api import (
    _fantasy_positions,
    _lineup_slots,
    _players,
    _projected_points,
    _projections,
    _team_snapshot,
    _team_starters,
)

_FLEX_ELIGIBLE = {
    "FLEX": {"RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB"},
}
_POINTS_FIELDS = {"std": "pts_std", "half_ppr": "pts_half_ppr", "ppr": "pts_ppr"}
_REC_VARIANTS = {1.0: "ppr", 0.5: "half_ppr", 0.0: "std"}


def _eligible_slots(positions: set[str], slots: list[str]) -> list[str]:
    """Active lineup slots a player may occupy, in league order without repeats."""
    eligible = []
    for slot in slots:
        if slot not in eligible and (
            slot in positions or positions & _FLEX_ELIGIBLE.get(slot, set())
        ):
            eligible.append(slot)
    return eligible


def _points_field(scoring_settings: object, scoring: str | None) -> str:
    """Resolve the projected-points field matching the league's scoring."""
    if scoring is not None:
        if scoring not in _POINTS_FIELDS:
            raise ValueError("scoring must be one of 'std', 'half_ppr', or 'ppr'.")
        return _POINTS_FIELDS[scoring]
    rec = (
        scoring_settings.get("rec") if isinstance(scoring_settings, dict) else None
    ) or 0
    variant = _REC_VARIANTS.get(rec)
    if variant is None:
        raise ValueError(
            "The league's scoring is neither standard, half-PPR, nor full PPR; "
            "pass scoring='std', 'half_ppr', or 'ppr'."
        )
    return _POINTS_FIELDS[variant]


def _full_name(record: dict, player_id: str) -> str:
    for name in (
        record.get("full_name"),
        " ".join(
            part
            for part in (record.get("first_name"), record.get("last_name"))
            if isinstance(part, str) and part
        ),
    ):
        if isinstance(name, str) and name.strip():
            return name
    return player_id


async def team(
    league_id: str,
    user_id: str,
    *,
    week: int | None = None,
    scoring: str | None = None,
) -> list[dict]:
    """Report every player on your roster with their eligible slots and projections.

    One dictionary per rostered player, in Sleeper's roster order: `player_id`,
    `full_name` (falling back to the ID for defenses without names), `slots`
    ending in `BN`, `current` (the player's slot in the requested week's
    lineup: an active slot for starters, otherwise `IR`, `TX`, or `BN`, and
    None when Sleeper has no lineup for that week yet, for example a future
    week), and `projected_points` for the week (None when Sleeper has no
    projection, for example a bye). `week` defaults to Sleeper's current
    displayed week and may be any week from 1 to 18. `scoring` selects the
    points variant; by default it is detected from the league's reception
    scoring. Only Sleeper's public API is read; no browser opens and no
    lineup changes.
    """
    async with httpx.AsyncClient() as client:
        snapshot = await _team_snapshot(client, league_id, user_id, week)
        starters, players, projections = await asyncio.gather(
            _team_starters(
                client,
                league_id,
                snapshot["league"],
                snapshot["roster"],
                snapshot["roster_id"],
                snapshot["week"],
            ),
            _players(client),
            _projections(client, snapshot["season"], snapshot["week"]),
        )
        league, roster = snapshot["league"], snapshot["roster"]
        slots = list(dict.fromkeys(_lineup_slots(league)))
        points_field = _points_field(league.get("scoring_settings"), scoring)
        current: dict[str, str] = {}
        if starters is not None:
            lineup_slots = _lineup_slots(league)
            if len(starters) != len(lineup_slots):
                raise RuntimeError(
                    "The week's lineup does not match the league's lineup slots."
                )
            for slot, player_id in zip(lineup_slots, starters):
                if player_id != "0":
                    current[player_id] = slot
            if not set(current) <= set(roster["players"]):
                raise RuntimeError(
                    "The week's lineup includes a player who is not on the roster."
                )
        report = []
        for player_id in roster["players"]:
            record = players.get(player_id)
            if not isinstance(record, dict):
                raise RuntimeError(  # noqa: TRY004
                    f"Sleeper has no player record for {player_id}."
                )
            positions = _fantasy_positions(record, player_id)
            points = _projected_points(
                projections.get(player_id), player_id, points_field
            )
            slot = current.get(player_id)
            if slot is None and starters is not None:
                if player_id in snapshot["reserve"]:
                    slot = "IR"
                elif player_id in snapshot["taxi"]:
                    slot = "TX"
                else:
                    slot = "BN"
            report.append(
                {
                    "player_id": player_id,
                    "full_name": _full_name(record, player_id),
                    "slots": [*_eligible_slots(set(positions), slots), "BN"],
                    "current": slot,
                    "projected_points": points,
                }
            )
        return report
