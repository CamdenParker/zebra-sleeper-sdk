"""Interactive Sleeper login and private, reusable browser sessions."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import re
import stat
import warnings
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    StorageState,
    VirtualCredential,
    async_playwright,
    expect,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

DEFAULT_AUTH_PATH = Path.home() / ".sleeper-sdk" / "auth.json"
DEFAULT_PASSKEY_PATH = Path.cwd() / "passkey.json"
_LOGIN_AGAIN = (
    "Run await sleeper_sdk.login() again (with the same auth_path, if customized)."
)
_PROFILE_URL = "https://sleeper.com/settings/profile"
_ACCOUNT_URL = "https://sleeper.com/settings/account"


class AuthStatus(TypedDict):
    """Verified account and whether this call established a new session."""

    user_id: str
    recovered: bool


class _LoginRequired(RuntimeError):
    """Sleeper visibly requested sign-in, rather than merely failing to load."""


class _MissingAuth(RuntimeError):
    """A private file is absent; other storage failures must not trigger login."""


@contextmanager
def _auth_directory(
    auth_path: str | Path, *, create: bool = False, passkey: bool = False
) -> Iterator[tuple[Path, int]]:
    """Open a private directory without following any path-component symlinks."""
    path = Path(os.path.abspath(Path(auth_path).expanduser()))
    if path == path.parent or path.is_relative_to(Path(__file__).resolve().parent):
        raise RuntimeError("Choose an auth_path outside the SDK and all repositories.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path.anchor, flags)
    project_passkey = passkey and path == Path.cwd() / "passkey.json"
    try:
        for part in path.parent.parts[1:]:
            try:
                os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(
                    "Keep authentication outside repositories; passkeys may use "
                    "passkey.json directly at the repository root."
                )
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not (passkey and path.name == "passkey.json"):
                raise RuntimeError(
                    "Keep authentication outside repositories; passkeys may use "
                    "passkey.json directly at the repository root."
                )
            project_passkey = True
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or (
            not project_passkey and stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise RuntimeError(
                "The auth directory must be owned by you and private (0700). "
                "Choose a new dedicated directory; existing directories are never chmodded."
            )
        yield path, directory_fd
    except OSError:
        raise RuntimeError(
            "Cannot access the auth file safely. Use a private, writable directory outside "
            "repositories, with no symlinks. " + _LOGIN_AGAIN
        ) from None
    finally:
        os.close(directory_fd)


def _check_auth_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise RuntimeError(
            "The auth file must be a regular file without hard links, owned by you with mode 0600. "
            "Choose a new private auth_path and run login again."
        )


@contextmanager
def _auth_lock(auth_path: Path) -> Iterator[None]:
    """Keep one inode for the advisory lock, including between operations."""
    with _auth_directory(auth_path, create=True) as (path, directory_fd):
        fd = os.open(
            path.name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            _check_auth_file(os.fstat(fd))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Authentication is busy: another login, enrollment, check, or swap "
                    "is using this auth_path. Try again after it finishes."
                ) from None
            yield
        finally:
            os.close(fd)


def _validate_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdecimal():
        raise ValueError("user_id must be a numeric Sleeper account ID.")


def _validate_paths(auth_path: Path, passkey_path: Path | None) -> None:
    if passkey_path is None:
        return
    auth = Path(os.path.abspath(auth_path.expanduser()))
    key = Path(os.path.abspath(passkey_path.expanduser()))
    if key in (auth, auth.with_name(auth.name + ".lock")):
        raise ValueError("passkey_path must be separate from the session and its lock.")


def _validate_storage_state(state: Any) -> StorageState:
    """Check the browser-state shape without examining or displaying credentials."""
    valid = (
        isinstance(state, dict)
        and isinstance(state.get("cookies"), list)
        and isinstance(state.get("origins"), list)
        and not state.get("credentials")
    )
    if valid:
        for cookie in state["cookies"]:
            if not (
                isinstance(cookie, dict)
                and all(
                    isinstance(cookie.get(key), str)
                    for key in ("name", "value", "domain", "path")
                )
                and type(cookie.get("expires")) in (int, float)
                and math.isfinite(cookie["expires"])
                and all(
                    isinstance(cookie.get(key), bool) for key in ("httpOnly", "secure")
                )
                and cookie.get("sameSite") in ("Strict", "Lax", "None")
            ):
                valid = False
                break
        for origin in state["origins"]:
            if not (
                isinstance(origin, dict)
                and isinstance(origin.get("origin"), str)
                and isinstance(origin.get("localStorage"), list)
                and all(
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("value"), str)
                    for item in origin["localStorage"]
                )
                and isinstance(origin.get("indexedDB", []), list)
            ):
                valid = False
                break
    if not valid:
        raise RuntimeError("Saved authentication is malformed. " + _LOGIN_AGAIN)
    return cast(StorageState, state)


def _read_private_json(file_path: Path, *, label: str, passkey: bool = False) -> Any:
    with _auth_directory(file_path, passkey=passkey) as (path, directory_fd):
        try:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            raise _MissingAuth(f"No saved {label}. " + _LOGIN_AGAIN) from None
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            _check_auth_file(os.fstat(source.fileno()))
            try:
                return json.load(source)
            except (ValueError, UnicodeError):
                raise RuntimeError(f"Saved {label} is malformed.") from None


def _load_storage_state(auth_path: Path = DEFAULT_AUTH_PATH) -> StorageState:
    return _validate_storage_state(
        _read_private_json(auth_path, label="Sleeper authentication")
    )


def _write_private_json(
    file_path: Path, value: Any, *, overwrite: bool = True, passkey: bool = False
) -> None:
    with _auth_directory(file_path, create=True, passkey=passkey) as (
        path,
        directory_fd,
    ):
        try:
            _check_auth_file(
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            )
        except FileNotFoundError:
            pass
        else:
            if not overwrite:
                raise RuntimeError(
                    "A passkey file already exists; choose a new passkey_path."
                )
        temporary = f".{path.name}.{uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(value, target, separators=(",", ":"), allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            if overwrite:
                os.replace(
                    temporary,
                    path.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            else:
                # Atomic creation without replacing a concurrently created credential.
                try:
                    os.link(
                        temporary,
                        path.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise RuntimeError(
                        "A passkey file already exists; choose a new passkey_path."
                    ) from None
                os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


async def _save_storage_state(
    context: BrowserContext, auth_path: Path = DEFAULT_AUTH_PATH
) -> StorageState:
    state = await context.storage_state(indexed_db=True, credentials=False)
    _write_private_json(auth_path, state)
    return state


def _load_passkey(passkey_path: Path, user_id: str) -> VirtualCredential:
    try:
        record = _read_private_json(passkey_path, label="Sleeper passkey", passkey=True)
    except _MissingAuth:
        raise RuntimeError(
            "No saved Sleeper passkey. Run enroll_passkey() first or correct passkey_path."
        ) from None
    if (
        not isinstance(record, dict)
        or type(record.get("version")) is not int
        or record["version"] != 1
    ):
        raise RuntimeError("Saved passkey is malformed or uses an unsupported version.")
    if record.get("user_id") != user_id:
        raise ValueError("The passkey belongs to a different Sleeper account.")
    credential = record.get("credential")
    if not (
        isinstance(credential, dict)
        and credential.get("rpId") == "sleeper.com"
        and all(
            isinstance(credential.get(key), str) and credential[key]
            for key in ("id", "userHandle", "privateKey", "publicKey")
        )
    ):
        raise RuntimeError("Saved passkey is malformed or is not for sleeper.com.")
    return cast(VirtualCredential, credential)


async def _login_required(page: Page) -> bool:
    location = urlsplit(page.url)
    return (
        location.scheme == "https"
        and location.netloc == "sleeper.com"
        and location.path == "/"
        and (
            await page.get_by_role("button", name="Log in", exact=True).is_visible()
            or await page.get_by_role(
                "dialog", name="Sign In - Sleeper", exact=True
            ).is_visible()
        )
    )


async def _authenticated_user_id(
    page: Page, *, timeout: float = 30_000, navigate: bool = True
) -> str:
    """Verify the account's own profile, then resolve its visible username publicly."""
    try:
        if navigate:
            await page.goto(
                _PROFILE_URL, wait_until="domcontentloaded", timeout=timeout
            )
        await page.get_by_role(
            "heading", name="Profile Settings", level=2, exact=True
        ).wait_for(timeout=timeout)
        username = (
            await page.get_by_role("textbox", name="Username", exact=True).input_value(
                timeout=timeout
            )
        ).strip()
        own_name = (
            await page.get_by_role(
                "button", name="Account menu", exact=True
            ).inner_text(timeout=timeout)
        ).strip()
    except PlaywrightError:
        if await _login_required(page):
            raise _LoginRequired("Sleeper requires sign-in. " + _LOGIN_AGAIN) from None
        raise RuntimeError(
            "Sleeper did not show an authenticated account profile. " + _LOGIN_AGAIN
        ) from None
    location = urlsplit(page.url)
    if (
        location.scheme != "https"
        or location.netloc != "sleeper.com"
        or location.path != "/settings/profile"
    ):
        raise RuntimeError(
            "Sleeper redirected away from the authenticated account profile. "
            + _LOGIN_AGAIN
        )
    if not username or username.casefold() != own_name.casefold():
        raise RuntimeError(
            "Could not verify the signed-in account's username. " + _LOGIN_AGAIN
        )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://api.sleeper.app/v1/user/{quote(username, safe='')}"
            )
            response.raise_for_status()
            user = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError(
            "Could not resolve the signed-in Sleeper account through the public API. Try again."
        ) from None
    if not (
        isinstance(user, dict)
        and isinstance(user.get("user_id"), str)
        and user["user_id"].isdecimal()
        and isinstance(user.get("username"), str)
        and user["username"].casefold() == username.casefold()
    ):
        raise RuntimeError(
            "Sleeper's account profile and public user record did not match. "
            + _LOGIN_AGAIN
        )
    return user["user_id"]


async def _launch_browser(playwright: Playwright, *, headless: bool) -> Browser:
    try:
        return await playwright.chromium.launch(headless=headless)
    except PlaywrightError:
        raise RuntimeError(
            "Could not start Chromium. Run `uv run playwright install chromium`; "
            "interactive login also requires a desktop display."
        ) from None


async def login(*, auth_path: Path = DEFAULT_AUTH_PATH) -> None:
    """Sign in normally in visible Chromium and verify saved login in a fresh browser.

    Complete Sleeper's login and any verification prompts yourself. The function
    waits up to ten minutes for your account profile; passwords are never read.
    """
    with _auth_lock(auth_path):
        await _interactive_login(auth_path)


async def _interactive_login(auth_path: Path) -> None:
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright, headless=False)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(_PROFILE_URL, wait_until="domcontentloaded")
            if not await page.get_by_role(
                "dialog", name="Sign In - Sleeper", exact=True
            ).is_visible():
                await page.get_by_role("button", name="Log in", exact=True).click()
            user_id = await _authenticated_user_id(
                page, timeout=600_000, navigate=False
            )
            await _save_storage_state(context, auth_path)
        except PlaywrightError:
            raise RuntimeError(
                "Interactive Sleeper login could not complete. " + _LOGIN_AGAIN
            ) from None
        finally:
            # A new context in this process is not sufficient persistence proof.
            await browser.close()

        browser = await _launch_browser(playwright, headless=False)
        try:
            context = await browser.new_context(
                storage_state=_load_storage_state(auth_path)
            )
            page = await context.new_page()
            if await _authenticated_user_id(page) != user_id:
                raise RuntimeError(
                    "Saved authentication restored a different Sleeper account. "
                    + _LOGIN_AGAIN
                )
            await _save_storage_state(context, auth_path)
        except PlaywrightError:
            raise RuntimeError(
                "Saved Sleeper login did not survive a fresh browser. " + _LOGIN_AGAIN
            ) from None
        finally:
            await browser.close()


async def _passkey_login(
    browser: Browser, credential: VirtualCredential, user_id: str
) -> tuple[BrowserContext, Page]:
    """Prove possession to Sleeper in an empty context, then verify the account."""
    try:
        async with asyncio.timeout(90):
            context = await browser.new_context()
            await context.credentials.create(
                credential["rpId"],
                id=credential["id"],
                user_handle=credential["userHandle"],
                private_key=credential["privateKey"],
                public_key=credential["publicKey"],
            )
            await context.credentials.install()
            page = await context.new_page()
            await page.goto(_PROFILE_URL, wait_until="domcontentloaded")
            dialog = page.get_by_role("dialog", name="Sign In - Sleeper", exact=True)
            if not await dialog.is_visible():
                await page.get_by_role("button", name="Log in", exact=True).click()
            await dialog.get_by_role(
                "button", name="Sign in with passkey", exact=True
            ).click()
            actual_id = await _authenticated_user_id(
                page, navigate=False, timeout=60_000
            )
            if actual_id != user_id:
                raise ValueError("Passkey login restored a different Sleeper account.")
            return context, page
    except (PlaywrightError, TimeoutError, _LoginRequired):
        # Playwright errors can echo credential arguments; never expose their text.
        raise RuntimeError(
            "Passkey login could not finish without human assistance. Sleeper may "
            "require additional verification, the credential may be revoked, or its "
            "login page may have changed. No automatic retry was made. " + _LOGIN_AGAIN
        ) from None


async def enroll_passkey(
    *,
    user_id: str,
    auth_path: Path = DEFAULT_AUTH_PATH,
    passkey_path: Path = DEFAULT_PASSKEY_PATH,
) -> None:
    """Let the human register a software passkey, then prove a fresh headless login.

    Run login() first. In the visible Account Settings page, click ADD PASSKEY
    and complete any verification yourself. This waits up to ten minutes. A
    credential is saved only after a separate browser verifies the same account.
    """
    _validate_user_id(user_id)
    _validate_paths(auth_path, passkey_path)
    with _auth_lock(auth_path):
        with _auth_directory(passkey_path, create=True, passkey=True) as (
            path,
            directory_fd,
        ):
            try:
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(
                    "A passkey file already exists; choose a new passkey_path."
                )
        state = _load_storage_state(auth_path)
        async with async_playwright() as playwright:
            browser = await _launch_browser(playwright, headless=False)
            stage = "opening the account settings page"
            try:
                context = await browser.new_context(storage_state=state)
                await context.credentials.install()
                page = await context.new_page()
                if await _authenticated_user_id(page) != user_id:
                    raise ValueError("The saved session does not match user_id.")
                await page.goto(_ACCOUNT_URL, wait_until="domcontentloaded")
                await page.get_by_role(
                    "heading", name="Passkeys", exact=True
                ).wait_for()
                add_passkey = page.get_by_role("button", name="Add Passkey", exact=True)
                await add_passkey.wait_for()
                await add_passkey.scroll_into_view_if_needed()
                await page.bring_to_front()
                passkeys = page.get_by_role(
                    "button", name=re.compile(r"^Delete passkey ")
                )
                count_before = await passkeys.count()
                stage = "waiting for you to click ADD PASSKEY and complete registration"
                async with asyncio.timeout(600):
                    while True:
                        credentials = await context.credentials.get()
                        if credentials:
                            break
                        # Poll the authenticator while the human completes registration.
                        await asyncio.sleep(0.5)
                    if len(credentials) != 1 or credentials[0]["rpId"] != "sleeper.com":
                        raise RuntimeError(
                            "Registration produced an unexpected credential; enrollment stopped."
                        )
                    credential = credentials[0]
                    # A local key alone is not evidence that Sleeper registered it.
                    stage = (
                        "waiting for Sleeper to display the newly registered passkey"
                    )
                    await expect(passkeys).to_have_count(
                        count_before + 1, timeout=60_000
                    )
                    if await _authenticated_user_id(page) != user_id:
                        raise ValueError(
                            "The Sleeper account changed during enrollment."
                        )
            except (PlaywrightError, TimeoutError):
                raise RuntimeError(
                    f"Passkey enrollment stopped while {stage}. If Sleeper added a new passkey, "
                    "remove that unsuccessful enrollment in Account Settings before trying again."
                ) from None
            finally:
                with suppress(PlaywrightError):
                    await browser.close()

            browser = await _launch_browser(playwright, headless=True)
            try:
                context, _ = await _passkey_login(browser, credential, user_id)
                if _load_storage_state(auth_path) != state:
                    raise RuntimeError(
                        "Saved authentication changed during enrollment."
                    )
                _write_private_json(
                    passkey_path,
                    {"version": 1, "user_id": user_id, "credential": credential},
                    overwrite=False,
                    passkey=True,
                )
                await _save_storage_state(context, auth_path)
            except (RuntimeError, ValueError) as exc:
                raise RuntimeError(
                    f"Enrollment could not finish: {exc} "
                    "Do not rely on this credential yet. If no passkey file was saved, "
                    "remove the newly added passkey through Sleeper Account Settings."
                ) from None
            finally:
                with suppress(PlaywrightError):
                    await browser.close()


async def _restore_session(
    browser: Browser,
    state: StorageState | None,
    credential: VirtualCredential | None,
    user_id: str | None,
    *,
    fresh: bool,
) -> tuple[BrowserContext, Page, str, bool]:
    if state is not None and not fresh:
        try:
            context = await browser.new_context(storage_state=state)
            page = await context.new_page()
        except PlaywrightError:
            raise RuntimeError(
                "Saved browser authentication could not be restored."
            ) from None
        try:
            actual_id = await _authenticated_user_id(page)
        except _LoginRequired:
            if credential is None:
                raise
            await context.close()
        else:
            return context, page, actual_id, False
    if credential is None or user_id is None:
        raise RuntimeError("No passkey is configured for recovery. " + _LOGIN_AGAIN)
    context, page = await _passkey_login(browser, credential, user_id)
    return context, page, user_id, True


@asynccontextmanager
async def _verified_session(
    auth_path: Path,
    *,
    user_id: str | None,
    passkey_path: Path | None,
    headless: bool,
    fresh: bool = False,
    refresh_after: bool = True,
) -> AsyncIterator[tuple[Page, AuthStatus]]:
    if user_id is not None:
        _validate_user_id(user_id)
    _validate_paths(auth_path, passkey_path)
    if passkey_path is not None and user_id is None:
        raise ValueError("Passkey recovery requires the expected user_id.")
    if fresh and passkey_path is None:
        raise ValueError("A fresh authentication check requires passkey_path.")
    with _auth_lock(auth_path):
        try:
            state = _load_storage_state(auth_path)
        except _MissingAuth:
            if passkey_path is None:
                raise
            state = None
        credential = (
            _load_passkey(passkey_path, cast(str, user_id))
            if passkey_path is not None
            else None
        )
        async with async_playwright() as playwright:
            browser = await _launch_browser(playwright, headless=headless)
            try:
                context, page, actual_id, recovered = await _restore_session(
                    browser, state, credential, user_id, fresh=fresh
                )
                if user_id is not None and actual_id != user_id:
                    raise ValueError(
                        "The saved Sleeper account does not match user_id."
                    )
                # Persist recovery before any lineup action; failures here are safe to report.
                baseline = await _save_storage_state(context, auth_path)
                status: AuthStatus = {"user_id": actual_id, "recovered": recovered}
                yield page, status
                if refresh_after:
                    await _refresh_after_success(
                        context, page, auth_path, baseline, actual_id
                    )
            finally:
                with suppress(PlaywrightError):
                    await browser.close()


async def _refresh_after_success(
    context: BrowserContext,
    page: Page,
    auth_path: Path,
    baseline: StorageState,
    user_id: str,
) -> None:
    """Post-operation persistence must never change the reported mutation outcome."""
    try:
        if await _authenticated_user_id(page) != user_id:
            raise RuntimeError("The signed-in account changed.")
        if _load_storage_state(auth_path) != baseline:
            raise RuntimeError("Saved authentication changed during the operation.")
        await _save_storage_state(context, auth_path)
    except Exception:  # noqa: BLE001
        with suppress(Exception):
            warnings.warn(
                "The operation finished, but saved authentication could not be refreshed. "
                + _LOGIN_AGAIN,
                RuntimeWarning,
                stacklevel=2,
            )


async def check_auth(
    *,
    user_id: str,
    auth_path: Path = DEFAULT_AUTH_PATH,
    passkey_path: Path | None = None,
    fresh: bool = False,
    headless: bool = True,
) -> AuthStatus:
    """Verify and persist authentication without visiting or modifying a lineup.

    Supplying passkey_path enables one recovery attempt. fresh=True ignores
    saved browser state for login, preserving the file unless verification succeeds.
    """
    async with _verified_session(
        auth_path,
        user_id=user_id,
        passkey_path=passkey_path,
        headless=headless,
        fresh=fresh,
        refresh_after=False,
    ) as (_, status):
        return status


@asynccontextmanager
async def _session(
    auth_path: Path = DEFAULT_AUTH_PATH,
    *,
    headless: bool = True,
    user_id: str | None = None,
    passkey_path: Path | None = None,
) -> AsyncIterator[tuple[Page, str]]:
    async with _verified_session(
        auth_path,
        user_id=user_id,
        passkey_path=passkey_path,
        headless=headless,
    ) as (page, status):
        yield page, status["user_id"]
