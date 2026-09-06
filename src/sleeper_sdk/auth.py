"""Interactive Sleeper login and private, reusable browser sessions."""

from __future__ import annotations

import json
import math
import os
import stat
import warnings
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

DEFAULT_AUTH_PATH = Path.home() / ".sleeper-sdk" / "auth.json"
_LOGIN_AGAIN = "Run await sleeper_sdk.login() again (with the same auth_path, if customized)."
_PROFILE_URL = "https://sleeper.com/settings/profile"


@contextmanager
def _auth_directory(auth_path: str | Path, *, create: bool = False) -> Iterator[tuple[Path, int]]:
    """Open a private directory without following any path-component symlinks."""
    path = Path(os.path.abspath(Path(auth_path).expanduser()))
    if path == path.parent or path.is_relative_to(Path(__file__).resolve().parent):
        raise RuntimeError("Choose an auth_path outside the SDK and all repositories.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            try:
                os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("Keep authentication outside repositories; use the default auth_path.")
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
            raise RuntimeError("Keep authentication outside repositories; use the default auth_path.")
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
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


def _validate_storage_state(state: Any) -> dict[str, Any]:
    """Check the browser-state shape without examining or displaying credentials."""
    valid = (
        isinstance(state, dict)
        and isinstance(state.get("cookies"), list)
        and isinstance(state.get("origins"), list)
    )
    if valid:
        for cookie in state["cookies"]:
            if not (
                isinstance(cookie, dict)
                and all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path"))
                and type(cookie.get("expires")) in (int, float)
                and math.isfinite(cookie["expires"])
                and all(isinstance(cookie.get(key), bool) for key in ("httpOnly", "secure"))
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
                    isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str)
                    for item in origin["localStorage"]
                )
                and isinstance(origin.get("indexedDB", []), list)
            ):
                valid = False
                break
    if not valid:
        raise RuntimeError("Saved authentication is malformed. " + _LOGIN_AGAIN)
    return state


def _load_storage_state(auth_path: str | Path = DEFAULT_AUTH_PATH) -> dict[str, Any]:
    with _auth_directory(auth_path) as (path, directory_fd):
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            raise RuntimeError("No saved Sleeper authentication. " + _LOGIN_AGAIN) from None
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            _check_auth_file(os.fstat(source.fileno()))
            try:
                state = json.load(source)
            except (ValueError, UnicodeError):
                raise RuntimeError("Saved authentication is malformed. " + _LOGIN_AGAIN) from None
    return _validate_storage_state(state)


async def _save_storage_state(context: BrowserContext, auth_path: str | Path = DEFAULT_AUTH_PATH) -> None:
    state = await context.storage_state(indexed_db=True)
    with _auth_directory(auth_path, create=True) as (path, directory_fd):
        try:
            _check_auth_file(os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False))
        except FileNotFoundError:
            pass
        temporary = f".{path.name}.{uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(state, target, separators=(",", ":"), allow_nan=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


async def _authenticated_user_id(page: Page, *, timeout: float = 30_000, navigate: bool = True) -> str:
    """Verify the account's own profile, then resolve its visible username publicly."""
    try:
        if navigate:
            await page.goto(_PROFILE_URL, wait_until="domcontentloaded", timeout=timeout)
        await page.get_by_role("heading", name="Profile Settings", level=2, exact=True).wait_for(timeout=timeout)
        username = (await page.get_by_role("textbox", name="Username", exact=True).input_value(timeout=timeout)).strip()
        own_name = (await page.get_by_role("button", name="Account menu", exact=True).inner_text(timeout=timeout)).strip()
    except PlaywrightError:
        raise RuntimeError("Sleeper did not show an authenticated account profile. " + _LOGIN_AGAIN) from None
    location = urlsplit(page.url)
    if location.scheme != "https" or location.netloc != "sleeper.com" or location.path != "/settings/profile":
        raise RuntimeError("Sleeper redirected away from the authenticated account profile. " + _LOGIN_AGAIN)
    if not username or username.casefold() != own_name.casefold():
        raise RuntimeError("Could not verify the signed-in account's username. " + _LOGIN_AGAIN)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"https://api.sleeper.app/v1/user/{quote(username, safe='')}")
            response.raise_for_status()
            user = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("Could not resolve the signed-in Sleeper account through the public API. Try again.") from None
    if not (
        isinstance(user, dict)
        and isinstance(user.get("user_id"), str)
        and user["user_id"].isdecimal()
        and isinstance(user.get("username"), str)
        and user["username"].casefold() == username.casefold()
    ):
        raise RuntimeError("Sleeper's account profile and public user record did not match. " + _LOGIN_AGAIN)
    return user["user_id"]


async def _launch_browser(playwright: Playwright, *, headless: bool) -> Browser:
    try:
        return await playwright.chromium.launch(headless=headless)
    except PlaywrightError:
        raise RuntimeError(
            "Could not start Chromium. Run `uv run playwright install chromium`; "
            "interactive login also requires a desktop display."
        ) from None


async def login(*, auth_path: str | Path = DEFAULT_AUTH_PATH) -> None:
    """Sign in normally in visible Chromium and verify saved login in a fresh browser.

    Complete Sleeper's login and any verification prompts yourself. The function
    waits up to ten minutes for your account profile; passwords are never read.
    """
    with _auth_directory(auth_path, create=True):
        pass
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright, headless=False)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(_PROFILE_URL, wait_until="domcontentloaded")
            if not await page.get_by_role("dialog", name="Sign In - Sleeper", exact=True).is_visible():
                await page.get_by_role("button", name="Log in", exact=True).click()
            user_id = await _authenticated_user_id(page, timeout=600_000, navigate=False)
            await _save_storage_state(context, auth_path)
        except PlaywrightError:
            raise RuntimeError("Interactive Sleeper login could not complete. " + _LOGIN_AGAIN) from None
        finally:
            # A new context in this process is not sufficient persistence proof.
            await browser.close()

        browser = await _launch_browser(playwright, headless=False)
        try:
            context = await browser.new_context(storage_state=_load_storage_state(auth_path))
            page = await context.new_page()
            if await _authenticated_user_id(page) != user_id:
                raise RuntimeError("Saved authentication restored a different Sleeper account. " + _LOGIN_AGAIN)
        except PlaywrightError:
            raise RuntimeError("Saved Sleeper login did not survive a fresh browser. " + _LOGIN_AGAIN) from None
        finally:
            await browser.close()


@asynccontextmanager
async def _session(
    auth_path: str | Path = DEFAULT_AUTH_PATH, *, headless: bool = True
) -> AsyncIterator[tuple[Page, str]]:
    """Yield a verified session; refreshing storage must never change an operation's outcome."""
    state = _load_storage_state(auth_path)
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright, headless=headless)
        try:
            try:
                context = await browser.new_context(storage_state=state)
                page = await context.new_page()
            except PlaywrightError:
                raise RuntimeError("Saved Sleeper authentication could not be restored. " + _LOGIN_AGAIN) from None
            user_id = await _authenticated_user_id(page)
            yield page, user_id
            # Only refresh after success, and only while the same account is active.
            try:
                if await _authenticated_user_id(page) != user_id:
                    raise RuntimeError("The signed-in account changed.")
                if _load_storage_state(auth_path) != state:
                    raise RuntimeError("Saved authentication changed during the operation.")
                await _save_storage_state(context, auth_path)
            except Exception:
                with suppress(Exception):
                    warnings.warn(
                        "The operation finished, but saved authentication could not be refreshed. " + _LOGIN_AGAIN,
                        RuntimeWarning,
                        stacklevel=2,
                    )
        finally:
            with suppress(PlaywrightError):
                await browser.close()
