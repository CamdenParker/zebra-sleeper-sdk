league_id := "1312207808812322816"
user_id := "997952604384608256"
burrow_id := "6770"
maye_id := "11564"
dowdle_id := "7021"

# List commands without changing the lineup.
default:
    @just --list

# Exchange Burrow and Maye; running twice restores their original positions.
swap-burrow-maye:
    uv run python examples/use_sdk.py swap --league-id {{league_id}} --user-id {{user_id}} --player-a-id {{burrow_id}} --player-b-id {{maye_id}}

# Expect rejection: Dowdle cannot replace Maye at QB (two bench players also reject).
swap-dowdle-maye:
    uv run python examples/use_sdk.py swap --league-id {{league_id}} --user-id {{user_id}} --player-a-id {{dowdle_id}} --player-b-id {{maye_id}}
