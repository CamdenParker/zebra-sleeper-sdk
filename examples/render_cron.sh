#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
umask 077
auth_dir="$(mktemp -d "$HOME/.sleeper-cron.XXXXXX")"
cleanup() {
    run_status=$?
    # Linux accounts for the whole container, including Chromium's child processes.
    for peak_file in /sys/fs/cgroup/memory.peak /sys/fs/cgroup/memory/memory.max_usage_in_bytes; do
        if [ -r "$peak_file" ]; then
            awk '{printf "Container lifetime peak memory: %.1f MiB\n", $1 / 1048576}' "$peak_file" >&2 || :
            break
        fi
    done
    rm -rf -- "$auth_dir"
    exit "$run_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Render controls the mounted file's permissions and ownership. Load a private copy.
install -m 600 /etc/secrets/passkey.json "$auth_dir/passkey.json"
install -m 600 /etc/secrets/espn.json "$auth_dir/espn.json"
lineup_status=0
printf 'Running Sleeper lineup optimization...\n' >&2
uv run --no-sync python examples/use_sdk.py \
    --auth-path "$auth_dir/auth.json" --passkey-path "$auth_dir/passkey.json" \
    set-optimal-lineup --headless "$@" || lineup_status=$?
printf 'Running ESPN lineup optimization...\n' >&2
uv run --no-sync python examples/use_espn.py \
    --auth-path "$auth_dir/espn.json" \
    set-optimal-lineup --league-id "${ESPN_LEAGUE_ID:-107649966}" \
    --team-id "${ESPN_TEAM_ID:-10}" || {
    espn_status=$?
    if [ "$lineup_status" -eq 0 ]; then
        lineup_status=$espn_status
    fi
}
exit "$lineup_status"
