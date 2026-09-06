# sleeper-sdk

A small async Python library with two public functions: `login` and `swap`.
It reads Sleeper's public API and uses your authenticated browser session to
exchange two players' exact lineup slots. Python 3.11 or newer is required;
development is pinned to Python 3.13.7. The only runtime dependencies are
`httpx` and `playwright`.

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
can expire; run `login` again with the same path when sign-in is required.

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
`auth_path`, and `headless` are keyword-only. Use real Sleeper IDs for a roster
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
```

## Scope

The browser controls target Sleeper's current desktop Classic NFL Team page,
for the current season and supported editable week. Best Ball and other sports
are unsupported. UI changes can require selector updates. There are no public
read helpers, batch operations, scheduler, or optimizer. Public API requests
are read-only; all lineup changes go through Sleeper's UI.

Only one `swap` can run at a time in a process. There is no cross-process lock;
concurrent login, other SDK processes, and manual lineup edits are unsupported.
The read/check/click sequence is not atomic with other actors.

## Layout

- `src/sleeper_sdk/__init__.py`: exports only `login` and `swap`.
- `auth.py`: interactive sign-in, private snapshots, and verified sessions.
- `_api.py`: private public-API reads and lineup validation.
- `lineup.py`: one UI exchange and complete ordered verification.
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

## References

- [Sleeper API](https://docs.sleeper.com/): read-only league, roster, matchup,
  user, and NFL-state endpoints.
- [Playwright authentication](https://playwright.dev/python/docs/auth): saving
  and reusing browser state.
- [Playwright actionability](https://playwright.dev/python/docs/actionability):
  locator readiness checks before interaction.
- [uv projects](https://docs.astral.sh/uv/guides/projects/): project environments,
  lockfiles, and running commands.
