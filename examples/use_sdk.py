"""Opt-in examples: no browser opens and no lineup changes without a command."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sleeper_sdk import check_auth, enroll_passkey, login, swap, team
from sleeper_sdk.auth import DEFAULT_PASSKEY_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--auth-path",
        type=Path,
        default=Path.home() / ".sleeper-sdk" / "auth.json",
        help="Private browser authentication snapshot path.",
    )
    commands = parser.add_subparsers(dest="command")
    parser.add_argument(
        "--passkey-path",
        type=Path,
        help="Private software passkey file; explicitly enables unattended recovery.",
    )
    commands.add_parser("login", help="Open a browser and sign in normally.")
    enroll_command = commands.add_parser(
        "enroll-passkey", help="Register a dedicated passkey with your assistance."
    )
    enroll_command.add_argument("--user-id", required=True)
    check_command = commands.add_parser(
        "check-auth", help="Verify authentication without changing a lineup."
    )
    check_command.add_argument("--user-id", required=True)
    check_command.add_argument(
        "--fresh",
        action="store_true",
        help="Prove passkey login without saved session state.",
    )
    check_command.add_argument("--visible", action="store_true")
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
    elif args.command == "enroll-passkey":
        print(
            "Opening Sleeper Account Settings. Click ADD PASSKEY and complete any "
            "verification yourself. Wait for the separate headless login proof; "
            "do not close the browser. Enrollment waits up to ten minutes.",
            file=sys.stderr,
            flush=True,
        )
        asyncio.run(
            enroll_passkey(
                user_id=args.user_id,
                auth_path=args.auth_path,
                passkey_path=args.passkey_path or DEFAULT_PASSKEY_PATH,
            )
        )
        print(json.dumps({"user_id": args.user_id, "enrolled": True}))
    elif args.command == "check-auth":
        result = asyncio.run(
            check_auth(
                user_id=args.user_id,
                auth_path=args.auth_path,
                passkey_path=args.passkey_path,
                fresh=args.fresh,
                headless=not args.visible,
            )
        )
        print(json.dumps(result))
    elif args.command == "swap":
        result = asyncio.run(
            swap(
                args.league_id,
                args.player_a_id,
                args.player_b_id,
                user_id=args.user_id,
                auth_path=args.auth_path,
                passkey_path=args.passkey_path,
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
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
