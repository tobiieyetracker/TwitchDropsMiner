"""An opt-in, in-memory Twitch web session for authenticated GraphQL reads.

The user signs in on Twitch's own page. Its fetch implementation obtains integrity
tokens; we do not implement or replace Twitch's browser checks.
"""
from __future__ import annotations

import asyncio
import sys
from math import isfinite
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from exceptions import LoginException


GQL_URL = "https://gql.twitch.tv/gql"
WEB_URL = "https://www.twitch.tv/drops/campaigns"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
HEADER_NAMES = (
    "Authorization", "Client-Id", "User-Agent", "X-Device-Id",
    "Client-Session-Id", "Client-Version",
)


def browser_proxy_settings(proxy: str) -> dict[str, str]:
    """Translate the miner's proxy URL to Playwright's separate auth fields."""
    try:
        url = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        host, port = url.hostname, url.port
        if (
            url.scheme not in {"http", "https", "socks4", "socks5"}
            or not host or url.path not in {"", "/"} or url.query or url.fragment
        ):
            raise ValueError
        if ":" in host:
            host = f"[{host}]"
        server = f"{url.scheme}://{host}"
        if port is not None:
            server += f":{port}"
        options = {"server": server}
        if url.username is not None:
            if url.scheme in {"socks4", "socks5"}:
                raise LoginException(
                    "The browser does not support authenticated SOCKS proxies. "
                    "Use an HTTP/HTTPS proxy supported by your network."
                )
            options["username"] = unquote(url.username)
            options["password"] = unquote(url.password or "")
        return options
    except ValueError:
        # URL parser errors can contain the original credentials or address.
        raise LoginException(
            "Invalid browser proxy URL. Expected scheme://[user:password@]host:port."
        ) from None


# Called only inside the normal Twitch page, after the user has signed in.
# window.fetch is intentionally used so Twitch's own request handling runs.
FETCH_INTEGRITY = """async (headers) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await window.fetch('https://gql.twitch.tv/integrity', {
            method: 'POST', headers, signal: controller.signal,
        });
        return {status: response.status, data: response.ok ? await response.json() : null};
    } finally { clearTimeout(timer); }
}"""


@dataclass(frozen=True)
class WebCredentials:
    user_id: int
    headers: dict[str, str] = field(repr=False)

    @property
    def access_token(self) -> str:
        return self.headers["Authorization"][6:]


def credentials_from_response(
    headers: dict[str, str], response: Any,
) -> WebCredentials | None:
    """Bind credentials to a successful dashboard response from that exact request."""
    lower = {key.lower(): value for key, value in headers.items()}
    if lower.get("client-id") != WEB_CLIENT_ID:
        return None
    if not lower.get("authorization", "").startswith("OAuth ") or len(lower["authorization"]) <= 6:
        return None
    if not lower.get("x-device-id") or not lower.get("user-agent"):
        return None
    responses = response if isinstance(response, list) else [response]
    for item in responses:
        if not isinstance(item, dict):
            continue
        if item.get("errors") or (item.get("extensions") or {}).get("challenge"):
            continue
        current_user = (item.get("data") or {}).get("currentUser")
        if not isinstance(current_user, dict) or not isinstance(current_user.get("dropCampaigns"), list):
            continue
        user_id = str(current_user.get("id", ""))
        if user_id.isdecimal():
            selected = {name: lower[name.lower()] for name in HEADER_NAMES if name.lower() in lower}
            return WebCredentials(int(user_id), selected)
    return None


class TwitchWebSession:
    def __init__(
        self, *, channel: str | None = None, proxy: str = "",
        notify: Callable[[str], None] = print,
    ):
        self.channel = channel or ("msedge" if sys.platform == "win32" else "chrome")
        self.proxy = proxy
        self.notify = notify
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._credentials: WebCredentials | None = None
        self._token: str | None = None
        self._refresh_at = 0.0
        self._ready = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._response_tasks: set[asyncio.Task] = set()
        self._invalidated = False
        self._account_changed = False

    def invalidate(self) -> None:
        self._invalidated = True
        self._token = None
        self._refresh_at = 0.0

    def _on_response(self, response: Any) -> None:
        if response.url != GQL_URL:
            return
        task = asyncio.create_task(self._capture_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _capture_response(self, response: Any) -> None:
        try:
            credentials = credentials_from_response(
                await response.request.all_headers(), await response.json(),
            )
        except Exception:
            # Redirects, cancelled requests and non-JSON responses are not a login.
            return
        if credentials is None:
            return
        if self._credentials is not None:
            if credentials.user_id != self._credentials.user_id:
                self._account_changed = True
            # Do not silently change the identity used by an in-flight miner operation.
            return
        self._credentials = credentials
        self._ready.set()

    async def start(self) -> WebCredentials:
        async with self._start_lock:
            if self._invalidated:
                await self.close()
            if self._credentials is not None:
                await self._check_session()
                return self._credentials
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise LoginException(
                    "Browser authentication requires requirements-browser.txt. "
                    "Install it with: python -m pip install -r requirements-browser.txt"
                ) from None
            try:
                self._playwright = await async_playwright().start()
                launch: dict[str, Any] = {"headless": False}
                if self.channel != "chromium":
                    launch["channel"] = self.channel
                if self.proxy:
                    launch["proxy"] = browser_proxy_settings(self.proxy)
                self._browser = await self._playwright.chromium.launch(**launch)
                # Ephemeral context: no access to the user's default browser profile,
                # and no auth cookies, integrity token or passwords written to disk.
                self._context = await self._browser.new_context()
                self._page = await self._context.new_page()
                self._page.on("response", self._on_response)
                self.notify(
                    "Sign in to Twitch in the opened browser and open the Drops campaigns page. "
                    "Leave that tab open. "
                    "This session is not saved; campaign queries use this account."
                )
                await self._page.goto(WEB_URL, wait_until="domcontentloaded", timeout=60000)
                for _ in range(300):
                    if self._page.is_closed():
                        raise LoginException("The Twitch login browser was closed")
                    try:
                        await asyncio.wait_for(self._ready.wait(), timeout=1)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    raise LoginException("Twitch browser login timed out; restart and sign in again")
                await self._check_session()
                assert self._credentials is not None
                return self._credentials
            except BaseException as error:
                await self.close()
                if isinstance(error, (LoginException, asyncio.CancelledError)):
                    raise
                # Playwright errors can include request details; do not expose them.
                raise LoginException(
                    "Unable to open the Twitch web session. Check the browser installation "
                    "and network, or select --browser-channel chrome/msedge/chromium."
                ) from None

    async def _check_session(self) -> None:
        if self._invalidated or self._account_changed or self._credentials is None:
            raise LoginException("The Twitch web account changed; restart browser authentication")
        if self._page is None or self._page.is_closed():
            raise LoginException("Keep the Twitch authentication browser open while mining")
        location = urlsplit(self._page.url)
        if location.scheme != "https" or location.netloc != "www.twitch.tv":
            raise LoginException("Return the authentication tab to the Twitch campaigns page")
        cookies = await self._context.cookies("https://www.twitch.tv")
        token = next((item["value"] for item in cookies if item["name"] == "auth-token"), None)
        if token != self._credentials.access_token:
            raise LoginException("The Twitch web login changed or expired; restart browser authentication")

    async def refresh_integrity(self, rejected_token: str | None = None) -> None:
        async with self._refresh_lock:
            await self._check_session()
            # Coalesce concurrent challenges that were sent with the same old token.
            if self._token and time() < self._refresh_at and self._token != rejected_token:
                return
            assert self._credentials is not None
            headers = {
                key: value for key, value in self._credentials.headers.items()
                if key != "User-Agent"  # the browser supplies its actual User-Agent
            }
            headers["Client-Request-Id"] = uuid4().hex
            try:
                result = await self._page.evaluate(FETCH_INTEGRITY, headers)
            except Exception:
                raise LoginException(
                    "Could not refresh Twitch integrity. Check the open Twitch browser."
                ) from None
            data = result.get("data") if isinstance(result, dict) else None
            now = time()
            if (
                not isinstance(result, dict) or result.get("status") != 200
                or not isinstance(data, dict) or not isinstance(data.get("token"), str)
                or not data["token"] or not isinstance(data.get("expiration"), (int, float))
                or not isfinite(data["expiration"])
                or data["expiration"] / 1000 <= now
            ):
                raise LoginException("Twitch did not issue a valid integrity token; check the browser")
            await self._check_session()
            self._token = data["token"]
            self._refresh_at = now + 0.9 * (data["expiration"] / 1000 - now)

    async def headers(self) -> dict[str, str]:
        await self.refresh_integrity()
        assert self._credentials is not None and self._token is not None
        return {**self._credentials.headers, "Client-Integrity": self._token}

    async def close(self) -> None:
        tasks = list(self._response_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._response_tasks.clear()
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            try:
                if self._playwright is not None:
                    await self._playwright.stop()
            finally:
                self._playwright = self._browser = self._context = self._page = None
                self._credentials = self._token = None
                self._refresh_at = 0.0
                self._invalidated = self._account_changed = False
                self._ready.clear()
