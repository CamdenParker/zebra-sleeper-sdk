"""Opt-in examples: no browser opens and no lineup changes without a command."""

import argparse
import asyncio
import json
from pathlib import Path

from sleeper_sdk import login, swap, team


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--auth-path",
        type=Path,
        default=Path.home() / ".sleeper-sdk" / "auth.json",
        help="Private browser authentication snapshot path.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("login", help="Open a browser and sign in normally.")
    swap_command = commands.add_parser("swap", help="Exchange two lineup slots.")
    swap_command.add_argument("--league-id", required=True)
    swap_command.add_argument("--user-id", required=True)
    swap_command.add_argument("--player-a-id", required=True)
    swap_command.add_argument("--player-b-id", required=True)
    swap_command.add_argument("--headless", action="store_true")
    team_command = commands.add_parser(
        "team", help="Report rostered players' slots and weekly projections."
    )
    team_command.add_argument("--league-id", required=True)
    team_command.add_argument("--user-id", required=True)
    team_command.add_argument("--week", type=int)
    team_command.add_argument("--scoring", choices=("std", "half_ppr", "ppr"))
    args = parser.parse_args()

    if args.command == "login":
        asyncio.run(login(auth_path=args.auth_path))
    elif args.command == "swap":
        result = asyncio.run(
            swap(
                args.league_id,
                args.player_a_id,
                args.player_b_id,
                user_id=args.user_id,
                auth_path=args.auth_path,
                headless=args.headless,
            )
        )
        print(json.dumps(result, indent=2))
    elif args.command == "team":
        result = asyncio.run(
            team(
                args.league_id,
                args.user_id,
                week=args.week,
                scoring=args.scoring,
            )
        )
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
