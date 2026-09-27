# sleeper-sdk

A small async Python library with `login`, `enroll_passkey`, `check_auth`,
`swap`, `swaps`, `team`, `team_props`, `optimal_lineup`, and `set_optimal_lineup`. It reads
Sleeper's public API, reports your roster's eligible slots and weekly
projections, ranks legal lineups using SportsGameOdds player props and Sleeper
projections, and uses your authenticated browser session to exchange players'
exact lineup slots. Python
3.11 or newer is required; development is pinned to Python 3.13.7. The only
runtime dependencies are `httpx` and `playwright`.

ESPN support is isolated in `sleeper_sdk.espn`; the existing Sleeper functions
and commands retain their behavior. ESPN uses the same prop scoring and legal
slot-assignment helpers with ESPN's weekly projections and league settings.

## Setup

```sh
uv sync --locked
uv run playwright install chromium
```

## ESPN league

The included commands target league `107649966`, team `10`:

```sh
just espn-login
just espn-check-auth
just espn-optimal-lineup
just espn-set-optimal-lineup
```

`espn-login` opens a dedicated Chromium window. Complete sign-in and any
verification yourself. The SDK verifies that the account owns the requested
team before saving the `SWID` and `espn_s2` session cookies to
`~/.sleeper-sdk/espn.json`, using the existing private-directory and atomic-write
helpers (directory `0700`, file `0600`). The SDK does not collect your password.
These cookies are the persistent credential; they can expire or be revoked.
Run login again when authentication expires. ESPN software passkey recovery is
not implemented.

Both optimization commands require `SPORTSGAMEODDS_API_KEY`, as the Sleeper
optimizer does. The read-only command reports the best legal lineup using
bookmaker props, ESPN stat projections to fill missing markets, and ESPN's
native projected fantasy totals as the secondary ranking. The scoring weights
and slot counts come from your league, including PPR and its RB/WR/TE flex.
`total_projected_points` sums ESPN projections rather than the prop ranking
score. ESPN player IDs are independent of Sleeper player IDs.

```python
from sleeper_sdk.espn import login, optimal_lineup, set_optimal_lineup

await login(107649966, 10)  # Interactive, once per saved session.
report = await optimal_lineup(107649966, 10)  # Read-only.
result = await set_optimal_lineup(107649966, 10)  # Changes the lineup.
```

These functions accept `season=` and `auth_path=`; the read-only optimizer also
accepts `week=`. The season defaults to the current NFL season and the week to
ESPN's current editable scoring period. For other leagues, use
`uv run python examples/use_espn.py COMMAND --league-id ID --team-id ID`.
Place a custom `--auth-path` before the command. Keep credentials outside
repositories and shared artifacts.

Past-week reports use ESPN's historical roster and projections without pregame
props. Position-specific overrides on the core offensive scoring stats fall
back to native fantasy projections for the affected players. Current-week
optimization requires individual-game lineup locks and a reliable ESPN schedule.

The setter preserves locked starters and excludes locked bench players, using
ESPN's NFL schedule and native lock information independently of bookmaker
coverage. IR players remain untouched. It rechecks ownership, league settings,
week, roster, and locks before submitting one lineup transaction, then verifies
every rostered player's slot from a fresh ESPN response. Duplicate slots such
as the two RB positions are equivalent in ESPN. An already-optimal lineup makes
no write request. A failed verification can mean the change applied; inspect
ESPN before retrying. The setter never automatically retries a submission.

ESPN's API is undocumented; response and transaction formats can change.
The implementation references the [espn-api stat mappings](https://github.com/cwendt94/espn-api/blob/master/espn_api/football/constant.py)
and a [captured ESPN lineup transaction](https://github.com/tlo1216/espn-fantasy-mcp#the-lineup-move-payload).

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

The default credential file is `~/.sleeper-sdk/passkey.json`, alongside the
browser session at `~/.sleeper-sdk/auth.json`. Both files must have mode `0600`
inside a private directory (`0700`) outside repositories, including when using
custom paths. The credential
carries the account ID and uses atomic-write protections. It is **not encrypted
by the SDK** and grants account login access, not just permission to modify
lineups. Keep both files out of Git and exclude them from logs, traces, and
shared artifacts.

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
`passkey_path=Path.home() / ".sleeper-sdk" / "passkey.json"` to `swap` or
`swaps`, or by supplying the CLI's `--passkey-path` before `swap`, `swaps`, or
`set-optimal-lineup`. Existing callers without a passkey path retain
session-only authentication. A configured missing, malformed, or wrong-account
passkey is rejected even if a session is currently valid.

Recovery happens before any lineup interaction and makes only one login attempt.
Network failures or unrecognized page changes are not treated as proof of logout.
An additional human challenge, revoked credential, or failed identity check stops
the operation. A swap with an uncertain result is never replayed automatically.

This SDK does not select a host, schedule checks, or send notifications. Before
relying on unattended operation, securely provision the files on your chosen host
and run a fresh check under its actual runtime account after a reboot. Sleeper
can revoke credentials or change its login flow, so access is not guaranteed
indefinitely.

### Render cron secret files

Upload these two Render Secret Files:

| Local file | Secret File name | Launcher source |
| --- | --- | --- |
| `~/.sleeper-sdk/passkey.json` | `passkey.json` | `/etc/secrets/passkey.json` |
| `~/.sleeper-sdk/espn.json` | `espn.json` | `/etc/secrets/espn.json` |

Use this cron command after installing dependencies and Chromium during the build:

```sh
sh examples/render_cron.sh --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID
```

The existing command arguments configure Sleeper. ESPN defaults to league
`107649966`, team `10`; override them with Render environment variables
`ESPN_LEAGUE_ID` and `ESPN_TEAM_ID` when needed.

The launcher copies both mounted secrets into a new private directory under the
runtime user's home, with mode `0600`, before running either optimizer. Sleeper
runs first and ESPN second; an optimizer failure still allows the other to run,
and any failure makes the job exit nonzero. Each platform prints its name before
running. Temporary credentials and the Sleeper session are removed on exit;
mounted secrets are unchanged. The runtime user's home must be writable.

Sleeper recovers authentication through its passkey as needed. When ESPN's
cookies expire, run `just espn-login` locally and replace the Render `espn.json`
Secret File with the refreshed file.

Configure `SPORTSGAMEODDS_API_KEY` as a Render environment variable. See
[Render secret files](https://render.com/docs/configure-environment-variables#secret-files).

### Cron memory sizing

Use Render's **2 GB / 1 CPU** cron plan as the initial capacity after a 512 MiB
out-of-memory failure. This is a sizing recommendation, not a measured Linux
minimum. The job runs Python, Playwright's Node driver, and Chromium's browser
and renderer processes. It uses one browser session and one page for the batch;
an already-optimal lineup opens no browser.

On September 26, 2026, a local macOS read-only profile fetched the actual weekly
data, opened the authenticated roster, reloaded it once, and saved a temporary
session. It did not submit lineup changes. Sampling the sum of process RSS every
200 ms produced these approximate peaks:

| Scenario | Sampled peak |
| --- | ---: |
| Python planning data loaded (across runs) | 165–216 MiB |
| Fresh passkey login and roster, planning data retained | 1,297 MiB |
| Saved session and roster, planning data released | 1,251 MiB |
| Same saved-session setup, images/fonts/media blocked experimentally | 1,050 MiB |

Summed RSS can count shared pages more than once, and macOS memory compression
and browser builds differ from Linux. These figures support an initial capacity
estimate; they do not establish Render's exact minimum or a guaranteed asset
blocking reduction. The asset-blocking experiment is not enabled in the SDK.

The optimizer releases the full player catalog and projections once roster
candidates have been computed, before starting the browser. The cron launcher
prints `Container lifetime peak memory: ... MiB` on exit when Linux exposes a
cgroup peak counter. This includes browser subprocesses and kernel-accounted
memory; it is not Python's memory alone. The counter covers the container's
lifetime, and a container-wide OOM kill can prevent the final log from appearing.

After deploying, use a successful run that actually opens the browser to measure
capacity. Compare its peak with Render's Metrics page, and budget roughly 30%
headroom above the highest peak across several representative runs. A no-swap
run is not sufficient to size the browser workload. See
[Render metrics](https://render.com/docs/service-metrics) and
[cron pricing](https://render.com/pricing).

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

To apply several exchanges without reopening the browser between them, use
`swaps` with the pairs in order. The whole chain is validated against your
roster before any browser opens. Each pair then goes through the same
per-exchange checks and verification as `swap`, and the first failure stops
the batch with a report of how many exchanges completed:

```python
from sleeper_sdk import swaps

result = await swaps(
    league_id="YOUR_LEAGUE_ID",
    exchanges=[("FIRST_PLAYER_ID", "SECOND_PLAYER_ID"), ("THIRD_ID", "FOURTH_ID")],
    user_id="YOUR_USER_ID",
)
```

The return value adds the applied `swaps` as `player_a_id`/`player_b_id` pairs,
in order, to `swap`'s fields. An empty exchange list opens no browser and
reports the current lineup.

The example opens no browser and changes no lineup when imported or run without
a subcommand; the no-argument invocation only prints help:

```sh
uv run python examples/use_sdk.py
uv run python examples/use_sdk.py login
uv run python examples/use_sdk.py swap --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID --player-a-id FIRST_PLAYER_ID --player-b-id SECOND_PLAYER_ID
uv run python examples/use_sdk.py swaps --league-id YOUR_LEAGUE_ID --user-id YOUR_USER_ID --exchange FIRST_PLAYER_ID:SECOND_PLAYER_ID --exchange THIRD_ID:FOURTH_ID
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

## Read player props

```python
from sleeper_sdk.props import player_props

props = await player_props("Joe Burrow", season="2026", week=3, team="CIN")
```

The result maps available full-game stat names, such as `passing_yards`, to
`value`, `kind`, `source`, and `books`. A `line` is a bookmaker over/under threshold;
`probability` is the implied chance of at least one touchdown from yes odds.
No matching pregame market returns an empty dictionary. This uses the same
weekly slate cache as lineup optimization.

Run `just team-props` for every player on the configured roster, or call
`await team_props(league_id, user_id)` from Python. The default week is the
current editable week; `--week` and `--scoring` are available with the CLI.
Set `SPORTSGAMEODDS_API_KEY` in the environment or `.env` first. Each stat has
separate fields, so a Sleeper fallback never appears as a bookmaker prop:

```json
"receiving_yards": {
  "prop": null,
  "sleeper_projection": 72.4
}
```

When available, `prop` contains the estimate and its sportsbook IDs in
`books`; `sleeper_projection` remains visible alongside it. For `book_median`,
the listed books supplied the lines in the median. For SportsGameOdds
`book_over_under`, `fair_odds`, and `book_odds` consensus values, they are
available books on that market; the API does not identify the exact consensus
contributors. An empty list means no individual book could be identified.
`pass_int` and `fum_lost` are Sleeper-only stats. `touchdowns` is an overall
book market; its scoring fallback uses the separate rushing and receiving
touchdown projections. The report includes every rostered player, including
positions without supported player-prop markets. Past weeks show Sleeper
projections with `prop: null` because historical pregame markets are not
available on the SportsGameOdds free tier.

## Optimize your lineup

```python
from sleeper_sdk import optimal_lineup, set_optimal_lineup

lineup = await optimal_lineup(
    league_id="YOUR_LEAGUE_ID",
    user_id="YOUR_USER_ID",
)
```

`optimal_lineup` is read-only like `team`: it reports the highest-ranked
legal lineup for one week. Set `SPORTSGAMEODDS_API_KEY` in the environment or
in a `.env` file in the working directory before calling either lineup
optimizer. The
arguments match `team`'s (`league_id` and `user_id` may be positional; `week`
and `scoring` are optional keywords), but `week` defaults to Sleeper's current
editable lineup week, the same week
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
    "prop_score": 14.7,
    "projected_points": 11.2,
}
```

`prop_score` approximates fantasy points from available bookmaker over/under
lines and touchdown odds, with Sleeper stat projections filling missing
markets. League scoring weights are used when available; reception scoring
defaults to the league setting, includes position reception bonuses, and can
be selected with `scoring`. Players
rank first by `prop_score` (missing scores last), then by Sleeper
`projected_points` (missing projections last). The returned
`total_projected_points` sums Sleeper projections, so it is not the ranking
score. Past weeks use Sleeper projections alone; their pregame odds are not
available on the SportsGameOdds free tier. Positions and flex slots are chosen
together for a legal lineup; slots
no rostered player can fill hold `None` values. IR and taxi players are never
selected. In the active week, starters whose games have begun stay in their
slots, and other players from started games cannot enter. Selected current
starters keep their slots where possible. The call reads
Sleeper and SportsGameOdds; the latter fetches one
weekly NFL slate and caches it for ten minutes to limit free-tier usage.
The cache expires at kickoff if a game starts sooner.

`set_optimal_lineup` uses the same ranking while keeping starters whose games
have begun in their current slots. Players from started games cannot enter
from the bench. It applies the resulting lineup through the verified `swaps`
helper in one browser session, one bench player and one starter at a time:

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
`total_projected_points` (the Sleeper projection sum). Every exchange is
verified before the next one begins; if one fails, or the verified lineup
stops following the plan because something else changed it, nothing further
is applied and the error reports
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
are unsupported. UI changes can require selector updates. There is no
scheduler; `swaps` and `set_optimal_lineup` apply one verified exchange at a
time within a single browser session. `team` and `optimal_lineup` are the
read-only helpers, and their projections come from an undocumented endpoint
that can change without notice. Public API requests are read-only; all lineup
changes go through Sleeper's UI.

Only one `swap` or `swaps` batch can run at a time in a process. Login, enrollment, checks, and
swaps also hold a nonblocking cross-process lock associated with `auth_path`.
Use the same auth path for all operations on an account; different paths and
manual lineup edits are not coordinated. Leave the private `.lock` file in place
between runs. The read/check/click sequence is not atomic with other actors.

## Layout

- `src/sleeper_sdk/__init__.py`: public helpers and the `AuthStatus` result type.
- `auth.py`: interactive sign-in, passkey enrollment/recovery, and private storage.
- `_api.py`: private public-API reads and validation for swaps and team reports.
- `lineup.py`: verified UI exchanges, one at a time or batched in one session.
- `team.py`: read-only roster report of eligible slots and weekly projections.
- `prop_report.py`: read-only roster props and projection provenance.
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
