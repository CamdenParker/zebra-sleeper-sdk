"""ESPN-specific authentication and weekly lineup optimization."""

from .auth import check_auth, login
from .optimize import optimal_lineup, set_optimal_lineup

__all__ = ["check_auth", "login", "optimal_lineup", "set_optimal_lineup"]
