#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
umask 077
auth_dir="$(mktemp -d "$HOME/.sleeper-cron.XXXXXX")"
trap 'rm -rf -- "$auth_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Render controls the mounted file's permissions and ownership. Load a private copy.
install -m 600 /etc/secrets/passkey.json "$auth_dir/passkey.json"
uv run --no-sync python examples/use_sdk.py \
    --auth-path "$auth_dir/auth.json" --passkey-path "$auth_dir/passkey.json" \
    set-optimal-lineup --headless "$@"
