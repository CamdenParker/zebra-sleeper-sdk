"""ESPN login, read-only optimization, and explicitly requested lineup updates."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sleeper_sdk.espn import check_auth, login, optimal_lineup, set_optimal_lineup
from sleeper_sdk.espn.auth import DEFAULT_AUTH_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-path", type=Path, default=DEFAULT_AUTH_PATH)
    commands = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("login", "Open ESPN and save verified authentication after you sign in."),
        ("check-auth", "Verify saved authentication and team ownership."),
        ("optimal-lineup", "Print the optimal lineup without making changes."),
        ("set-optimal-lineup", "Compute, apply, and verify the optimal lineup."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--league-id", type=int, required=True)
        command.add_argument("--team-id", type=int, required=True)
        command.add_argument("--season", type=int)
        if name == "optimal-lineup":
            command.add_argument("--week", type=int)
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    functions = {
        "login": login,
        "check-auth": check_auth,
        "optimal-lineup": optimal_lineup,
        "set-optimal-lineup": set_optimal_lineup,
    }
    kwargs = {"season": args.season, "auth_path": args.auth_path}
    if args.command == "optimal-lineup":
        kwargs["week"] = args.week
    result = asyncio.run(
        functions[args.command](args.league_id, args.team_id, **kwargs)
    )
    print(
        json.dumps(result if result is not None else {"authenticated": True}, indent=2)
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
