league_id := "1312207808812322816"
user_id := "997952604384608256"
burrow_id := "6770"
maye_id := "11564"
dowdle_id := "7021"

# List commands without changing the lineup.
default:
    @just --list

# Clear the SDK's saved session; keep the passkey and other devices signed in.
logout:
    #!/usr/bin/env -S uv run python
    from sleeper_sdk.auth import DEFAULT_AUTH_PATH, _auth_lock

    with _auth_lock(DEFAULT_AUTH_PATH):
        DEFAULT_AUTH_PATH.unlink(missing_ok=True)
    print("SDK session cleared. Run just login-unattended to sign back in.")

# Prove headless passkey login from a fresh browser and save the recovered session.
login-unattended:
    uv run python examples/use_sdk.py --passkey-path "$HOME/.sleeper-sdk/passkey.json" check-auth --user-id {{ user_id }} --fresh

# Exchange Burrow and Maye; running twice restores their original positions.
swap-burrow-maye:
    uv run python examples/use_sdk.py swap --league-id {{ league_id }} --user-id {{ user_id }} --player-a-id {{ burrow_id }} --player-b-id {{ maye_id }}

# Expect rejection: Dowdle cannot replace Maye at QB (two bench players also reject).
swap-dowdle-maye:
    uv run python examples/use_sdk.py swap --league-id {{ league_id }} --user-id {{ user_id }} --player-a-id {{ dowdle_id }} --player-b-id {{ maye_id }}

# Read-only report of every rostered player's slots and weekly projections.
team:
    uv run python examples/use_sdk.py team --league-id {{ league_id }} --user-id {{ user_id }}

# Read-only roster props with source books and separate Sleeper projections.
team-props:
    uv run python examples/use_sdk.py team-props --league-id {{ league_id }} --user-id {{ user_id }}

# Print the highest-projected legal lineup for the current editable week.
optimal-lineup:
    uv run python examples/use_sdk.py optimal-lineup --league-id {{ league_id }} --user-id {{ user_id }}

# Compute the optimal lineup and apply it through verified swaps.
set-optimal-lineup *args:
    uv run python examples/use_sdk.py set-optimal-lineup --league-id {{ league_id }} --user-id {{ user_id }} {{ args }}
