# sleeper-sdk

A small async Python library with `login`, `enroll_passkey`, `check_auth`,
`swap`, `team`, `optimal_lineup`, and `set_optimal_lineup`. It reads Sleeper's
public API, reports your roster's eligible slots and weekly projections,
computes the highest-projected legal lineup, and uses your authenticated
browser session to exchange two players' exact lineup slots. Python 3.11 or
newer is required; development is pinned to Python 3.13.7. The only runtime
dependencies are `httpx` and `playwright`.

## Setup

```sh
uv sync --locked
uv run playwright install chromium
```

## Sign in

`await login()` opens visible Chromium. Sign in normally on
Sleeper and finish any verification yourself. The SDK does not collect your
password. It saves browser authentication, including IndexedDB, then closes
Chromium completely and verifies the same account in a separate launch.

In a notebook, use `await` directly:

```python
from sleeper_sdk import login

await login()
```

In a script, use `asyncio.run`:

```python
import asyncio
from sleeper_sdk import login

if __name__ == "__main__":
    asyncio.run(login())
```

The default snapshot path is `~/.sleeper-sdk/auth.json`. To customize it, import
`Path` from `pathlib` and pass `auth_path=Path(...)` to both functions. Choose a
dedicated directory outside repositories, with no symlink path components.
The directory must be private (`0700`) and the snapshot is stored with mode
`0600`; existing shared directories are not chmodded. This storage handling
uses POSIX filesystem features and has been exercised on macOS.

Treat the snapshot like a password and keep it out of version control. Sessions
can expire; run `login` again with the same path when sign-in is required, or
explicitly configure passkey recovery as described below.

## Enroll an unattended passkey

The SDK can use a dedicated software passkey to establish a new Sleeper session.
It uses Playwright 1.62 or newer's virtual authenticator. The human must initiate
registration in Sleeper; generating a local key alone does not register it.

1. Run `login` and finish normal sign-in and verification yourself.
2. Run enrollment for that account:

   ```sh
   uv run python examples/use_sdk.py enroll-passkey --user-id YOUR_USER_ID
   ```

3. In the dedicated browser at **Settings → Account**, click **ADD PASSKEY**.
   Complete any account verification yourself. The software authenticator handles
   the new credential; do not create an unrelated passkey in your usual browser.
4. Leave the browser open. The helper waits up to ten minutes, confirms that
   Sleeper lists the new credential, closes the browser completely, then proves
   login to the same account in a fresh headless browser with no saved session.
   Only successful proof permits saving the credential and reporting success.

The default credential file is `~/.sleeper-sdk/passkey.json`. It is separate from
`auth.json`, carries the account ID, and uses the same private-directory and
atomic-write protections. It is **not encrypted by the SDK** and grants account
login access, not just permission to modify lineups. Keep both files outside Git
and exclude them from logs, traces, and shared artifacts.

Enrollment refuses to overwrite an existing passkey file. To replace a passkey,
enroll to a new `--passkey-path`, verify it, update callers to use that path, then
remove the old credential yourself in Sleeper's account settings. If enrollment
fails after Sleeper added a credential but before a file was saved, remove that
unsuccessful credential there before retrying. Keep your personal login methods.

Python callers can use
`await enroll_passkey(user_id=..., auth_path=..., passkey_path=...)`.
Both paths have the defaults above; CLI path options go **before** the subcommand.

## Check authentication and enable recovery

```sh
# Verify and refresh the current session; no lineup page is opened.
uv run python examples/use_sdk.py check-auth --user-id YOUR_USER_ID

# Enable one passkey recovery attempt when the session is missing or logged out.
uv run python examples/use_sdk.py --passkey-path ~/.sleeper-sdk/passkey.json check-auth --user-id YOUR_USER_ID

# Prove a fresh login without loading saved session state into the browser.
uv run python examples/use_sdk.py --passkey-path ~/.sleeper-sdk/passkey.json check-auth --user-id YOUR_USER_ID --fresh
```

`check_auth` returns `AuthStatus`, a typed dictionary containing `user_id` and
`recovered`. The command prints this as JSON and exits zero on success; failures
produce a credential-free error on stderr and a nonzero exit code. Checks are
headless by default; use `--visible` to show the browser, or `headless=False` in
Python. `--fresh` requires a passkey and preserves the old session file unless
authentication and account verification succeed. Malformed or unsafe files are
errors, even during a fresh check.

Opt in to recovery on a lineup exchange by passing
`passkey_path=Path.home() / ".sleeper-sdk" / "passkey.json"` to `swap`, or by
supplying the CLI's `--passkey-path` before `swap`. Existing callers without a
passkey path retain session-only authentication. A configured missing, malformed,
or wrong-account passkey is rejected even if a session is currently valid.

Recovery happens before any lineup interaction and makes only one login attempt.
Network failures or unrecognized page changes are not treated as proof of logout.
An additional human challenge, revoked credential, or failed identity check stops
the operation. A swap with an uncertain result is never replayed automatically.

This SDK does not select a host, schedule checks, or send notifications. Before
relying on unattended operation, securely provision the files on your chosen host
and run a fresh check under its actual runtime account after a reboot. Sleeper
can revoke credentials or change its login flow, so access is not guaranteed
indefinitely.

## Exchange two lineup slots

```python
from sleeper_sdk import swap

result = await swap(
    league_id="YOUR_LEAGUE_ID",
    player_a_id="FIRST_PLAYER_ID",
    player_b_id="SECOND_PLAYER_ID",
    user_id="YOUR_USER_ID",
)
```

In a script, wrap the awaited call in an async function and run it with
`asyncio.run`. The first three arguments may also be positional; `user_id`,
`auth_path`, `passkey_path`, and `headless` are keyword-only. Use real Sleeper IDs for a roster
you own or co-own. Either player argument order works. At least one player must
be a starter; eligible starter-to-starter and starter-to-bench exchanges are
supported. IR, taxi, and two-bench-player moves are rejected.

`headless=False` is the default; `headless=True` hides the browser. The SDK
checks the signed-in account, roster, current week, and exact slot eligibility
before a single exchange. Success requires the complete ordered starter list,
including every unaffected slot, to match both the reloaded UI and public API.
The returned dictionary contains `league_id`, `roster_id`, `week`,
`starters_before`, and `starters_after`.

Weekly matchup starters are the normal API source. For a renewed pre-draft
dynasty with an entirely empty current-week matchup list, the SDK explicitly
selects roster starters instead. The initially selected source stays fixed
through verification; an API failure never triggers a source change. The UI's
displayed week must match the API's current display week.

A failed verification may occur after Sleeper has saved a change. Inspect your
lineup before taking further action. The SDK does not automatically retry a
mutation or roll it back.

The example opens no browser and changes no lineup when imported or run without
a subcommand; the no-argument invocation only prints help:

```sh
uv run python examples/use_sdk.py
uv run python examples/use_sdk.py login
uv run python examples/use_sdk.py swap --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID --player-a-id FIRST_PLAYER_ID --player-b-id SECOND_PLAYER_ID
uv run python examples/use_sdk.py team --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID [--week 1] [--scoring half_ppr]
```

## Read your team

```python
from sleeper_sdk import team

report = await team(
    league_id="YOUR_LEAGUE_ID",
    user_id="YOUR_USER_ID",
)
```

The league ID may be positional; `user_id` is keyword-only. `week` and
`scoring` are optional: `week` defaults to Sleeper's current displayed week and
may be any week from 1 to 18 of the league's current season, and `scoring`
selects the projected-points variant, detected from the league's reception
scoring (`std`, `half_ppr`, or `ppr`) by default. Leagues with other custom
scoring must pass `scoring` explicitly.

The report is one dictionary per rostered player, in Sleeper's roster order:

```python
{
    "player_id": "6770",
    "full_name": "Joe Burrow",
    "slots": ["QB", "BN"],
    "current": "QB",
    "projected_points": 22.4,
}
```

`slots` lists every active lineup slot the player may occupy, deduplicated in
league order: their base position plus `FLEX`, `REC_FLEX`, `WRRB_FLEX`,
`SUPER_FLEX`, and `IDP_FLEX` where eligible, always followed by `BN`, since
any rostered player may sit on the bench. `current` is the player's slot in
the requested week's lineup: an active slot for starters, otherwise `IR`,
`TX`, or `BN`. It is `None` when Sleeper has no lineup for that week yet,
for example a future week. `projected_points` is `None` when
Sleeper has no projection for that player and week, for example a bye. Team
defenses carry their ID-like record instead of a personal name.

The call reads only Sleeper's public API; no browser opens and no lineup
changes. Weekly projections come from an undocumented Sleeper endpoint that
can change without notice.

## Optimize your lineup

```python
from sleeper_sdk import optimal_lineup, set_optimal_lineup

lineup = await optimal_lineup(
    league_id="YOUR_LEAGUE_ID",
    user_id="YOUR_USER_ID",
)
```

`optimal_lineup` is read-only like `team`: it reports the highest-projected
legal lineup for one week. The arguments match `team`'s (`league_id` and
`user_id` may be positional; `week` and `scoring` are optional keywords), but
`week` defaults to Sleeper's current editable lineup week, the same week
`set_optimal_lineup` applies, rather than the displayed week. The result
holds `league_id`, `user_id`, `week`, `scoring`,
`total_projected_points`, and one `lineup` entry per active slot in league
order:

```python
{
    "slot": "FLEX",
    "player_id": "11632",
    "full_name": "Malik Nabers",
    "positions": ["WR"],
    "projected_points": 11.2,
}
```

Positions and flex slots are chosen together so that no legal lineup
projects more total points; players without a projection, for example on
bye, start only when a slot would otherwise be empty, and slots no rostered
player can legally fill hold `None` values. IR and taxi players are never
selected. The call reads only Sleeper's public API.

`set_optimal_lineup` computes the same optimum and applies it through the
verified `swap` helper, one bench player and one starter at a time:

```python
result = await set_optimal_lineup(
    league_id="YOUR_LEAGUE_ID",
    user_id="YOUR_USER_ID",
    headless=True,
)
```

`user_id`, `scoring`, `auth_path`, `passkey_path`, and `headless` are
keyword-only, exactly as for `swap`; supplying `passkey_path` enables one
unattended login attempt before lineup work. Starters already in their
optimal slot are left alone, and no browser opens at all when the lineup is
already optimal. The return value contains `league_id`, `roster_id`,
`week`, `scoring`, the ordered `swaps` applied as `player_in` and
`player_out`, `starters_before`, `starters_after`, and
`total_projected_points`. Every exchange is verified before the next one
begins; if one fails, or the verified lineup stops following the plan because
something else changed it, nothing further is applied and the error reports
the progress. Inspect Sleeper before retrying, just as for `swap`. A lineup with
an empty starting slot cannot be optimized this way, because a
player-to-player swap cannot fill an empty slot; fill it in Sleeper first.

Both are available as commands and recipes:

```sh
uv run python examples/use_sdk.py optimal-lineup --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID [--week 1] [--scoring half_ppr]
uv run python examples/use_sdk.py set-optimal-lineup --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID [--headless]
just optimal-lineup
just set-optimal-lineup
```

## Scope

The browser controls target Sleeper's current desktop Classic NFL Team page,
for the current season and supported editable week. Best Ball and other sports
are unsupported. UI changes can require selector updates. There are no batch
operations or scheduler; `set_optimal_lineup` applies one verified exchange at
a time. `team` and `optimal_lineup` are the read-only helpers, and their
projections come from an undocumented endpoint that can change without
notice. Public API requests are read-only; all lineup changes go through
Sleeper's UI.

Only one `swap` can run at a time in a process. Login, enrollment, checks, and
swaps also hold a nonblocking cross-process lock associated with `auth_path`.
Use the same auth path for all operations on an account; different paths and
manual lineup edits are not coordinated. Leave the private `.lock` file in place
between runs. The read/check/click sequence is not atomic with other actors.

## Layout

- `src/sleeper_sdk/__init__.py`: public helpers and the `AuthStatus` result type.
- `auth.py`: interactive sign-in, passkey enrollment/recovery, and private storage.
- `_api.py`: private public-API reads and validation for swaps and team reports.
- `lineup.py`: one UI exchange and complete ordered verification.
- `team.py`: read-only roster report of eligible slots and weekly projections.
- `optimize.py`: optimal-lineup selection, swap planning, and application.
- `examples/use_sdk.py`: explicit opt-in script commands.

## Manual validation — 2026-09-06

- `uv sync --locked` and Chromium installation passed. The unchanged example
  printed help without opening a browser.
- Public `login` completed normal visible sign-in and restored the same account
  after closing all Chromium windows and launching a separate browser. Default
  snapshot directory/file permissions were `0700`/`0600`.
- Identical player IDs, an outside-roster ID, two bench IDs, a missing snapshot,
  malformed snapshot JSON, and a well-formed but unauthenticated empty snapshot
  were rejected with useful errors.
- An incompatible destination was rejected after authenticated UI selection,
  before any target click. The full 11-slot API starter list was unchanged.
- A visible starter/bench exchange persisted after UI reload, with all other
  ten starter slots unchanged. The public call returned verification uncertainty
  because the API's CDN still served the old lineup; it did not retry or roll
  back the exchange. After adding fresh queries to the same read-only endpoints,
  verification passed against both the complete reloaded UI and fresh API in a
  separate browser, without clicking any position controls. A successful
  end-to-end public `swap` return after that fix has not yet been exercised.
- The inspected pre-draft dynasty had no weekly matchups and displayed week 1;
  its roster source was selected explicitly throughout verification.
- An expired saved session, locked-player rejection, and a headless
  mutation have not been exercised.

## Manual validation — 2026-09-07

- The read-only `team` report was run against the pre-draft dynasty league in
  the justfile: all 20 rostered players appeared in roster order with
  deduplicated league slots ending in `BN`; the half-PPR variant was
  auto-detected and matched Sleeper's `pts_half_ppr`; defenses fell back to
  their team names; each starter's `current` slot matched the week-1 lineup
  (for example Maye at `QB` and Smith at `IDP_FLEX`) and the bench showed
  `BN`; an explicit week outside 1–18 was rejected with a clear error.
  `ruff format` and pyright passed.

Authentication changes were checked separately:

- The new headless `check-auth` command verified the configured account using
  its existing session and returned `recovered: false`.
- A simultaneous check was rejected while enrollment held the cross-process
  session lock. Missing/malformed snapshots, unsafe permissions, missing or
  malformed configured keys, wrong-account key records, and conflicting paths
  failed before browser interaction. A fresh check without a key was rejected.
- The human registered a dedicated software passkey through **ADD PASSKEY**.
  Enrollment verified the account in a separate headless browser with no saved
  session, then saved both private files with mode `0600`.
- A separate `check-auth --fresh` process loaded the saved passkey and returned
  `recovered: true`. The next normal check reused its session and returned
  `recovered: false`.
- Using private temporary session files, both a missing snapshot and an empty
  logged-out snapshot recovered successfully with `recovered: true`.
- No lineup was changed for these checks. Reboot validation on the eventual
  unattended host and long-duration operation remain unverified.

## References

- [Sleeper API](https://docs.sleeper.com/): read-only league, roster, matchup,
  user, and NFL-state endpoints.
- [Playwright authentication](https://playwright.dev/python/docs/auth): saving
  and reusing browser state.
- [Playwright credentials](https://playwright.dev/python/docs/api/class-credentials):
  software passkey registration and fresh-browser restoration.
- [Playwright actionability](https://playwright.dev/python/docs/actionability):
  locator readiness checks before interaction.
- [uv projects](https://docs.astral.sh/uv/guides/projects/): project environments,
  lockfiles, and running commands.
