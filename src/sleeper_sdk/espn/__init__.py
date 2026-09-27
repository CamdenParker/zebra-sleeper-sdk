"""ESPN-specific authentication and weekly lineup optimization."""

from .auth import check_auth, login
from .optimize import optimal_lineup, set_optimal_lineup
from .prop_report import team_props

__all__ = ["check_auth", "login", "optimal_lineup", "set_optimal_lineup", "team_props"]
