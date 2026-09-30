"""Read-only WEB Cookie, website dashboard and Python transport diagnostic; no Tk."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

from check_browser_proxy import error_code
from web_session import GQL_URL, WEB_URL, WEB_CLIENT_ID, HEADER_NAMES, browser_proxy_settings


VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
READ_HEADERS = {name.lower() for name in (*HEADER_NAMES, "Client-Integrity", "Origin", "Referer")}
KNOWN_HOSTS = {"www.twitch.tv", "gql.twitch.tv", "id.twitch.tv", "assets.twitch.tv"}


class ProbeFailure(Exception):
    """Only fixed diagnostic codes, never server messages or credentials."""


def read_token(path: Path) -> str:
    # The explicitly selected file is the user's trusted local aiohttp cookie jar.
    # Load only; never save it or copy it into a browser profile on disk.
    jar = aiohttp.CookieJar()
    jar.load(path)
    cookie = jar.filter_cookies(URL(WEB_URL)).get("auth-token")
    if cookie is None or not cookie.value:
        raise ProbeFailure("cookie_has_no_twitch_auth_token")
    return cookie.value


def dashboard_state(body: Any, user_id: str) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    data = body.get("data")
    user = data.get("currentUser") if isinstance(data, dict) else None
    user = user if isinstance(user, dict) else {}
    campaigns = user.get("dropCampaigns")
    state = (
        "missing" if "dropCampaigns" not in user else
        "null" if campaigns is None else "list" if isinstance(campaigns, list) else "unexpected"
    )
    extensions = body.get("extensions")
    challenge = extensions.get("challenge") if isinstance(extensions, dict) else None
    kind = challenge.get("type") if isinstance(challenge, dict) else None
    errors = body.get("errors")
    integrity_error = False
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        info = error.get("extensions")
        integrity_error |= (
            isinstance(info, dict) and info.get("code") == "IntegrityCheckFailed"
        ) or str(error.get("message", "")).casefold() in {
            "failed integrity check", "integritycheckfailed",
        }
    return {
        "user_present": bool(user),
        "user_matches": str(user["id"]) == user_id if "id" in user else None,
        "campaigns_state": state,
        "campaign_count": len(campaigns) if isinstance(campaigns, list) else None,
        "errors_present": bool(errors),
        "challenge": "integrity" if kind == "integrity" else "other" if challenge else None,
        "integrity_failure": integrity_error or kind == "integrity",
    }


def accepted(state: dict[str, Any]) -> bool:
    return (
        state["http_status"] == 200 and state["user_matches"] is True
        and state["campaigns_state"] == "list" and not state["errors_present"]
        and state["challenge"] is None
    )


def persisted_dashboard(operation: Any) -> dict[str, Any] | None:
    if not isinstance(operation, dict) or operation.get("operationName") != "ViewerDropsDashboard":
        return None
    extensions = operation.get("extensions")
    query = extensions.get("persistedQuery") if isinstance(extensions, dict) else None
    if (
        not isinstance(query, dict) or query.get("version") != 1
        or not isinstance(query.get("sha256Hash"), str)
        or not re.fullmatch(r"[a-fA-F0-9]{64}", query["sha256Hash"])
        or not isinstance(operation.get("variables", {}), dict)
        or "query" in operation
    ):
        return None
    # Copy only this read, never other operations in the original request batch.
    return {
        "operationName": "ViewerDropsDashboard", "variables": operation.get("variables", {}),
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": query["sha256Hash"]}},
    }


class BrowserCapture:
    def __init__(self, token: str, user_id: str, report: dict[str, Any]):
        self.token, self.user_id, self.report = token, user_id, report
        self.ready = asyncio.Event()
        self.control: tuple[dict[str, Any], dict[str, str]] | None = None
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        report.update(dashboard_responses=[], integrity_responses=[], resource_failures=[], page_errors=0)

    def response(self, response: Any) -> None:
        if not self.closed:
            task = asyncio.create_task(self.inspect(response))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def resource_failure(self, request: Any, *, status: int | None = None) -> None:
        if self.closed or len(self.report["resource_failures"]) >= 20:
            return
        host = urlsplit(request.url).hostname
        # Keep no URLs, query strings, console messages or request headers.
        self.report["resource_failures"].append({
            "host": host if host in KNOWN_HOSTS else "other",
            "http_status": status,
            "error": error_code(RuntimeError(request.failure or "")) if status is None else None,
        })

    def page_error(self, _error: Any) -> None:
        if not self.closed:
            self.report["page_errors"] += 1

    async def inspect(self, response: Any) -> None:
        try:
            if response.status >= 400:
                self.resource_failure(response.request, status=response.status)
            if response.url == "https://gql.twitch.tv/integrity":
                summary = {"http_status": response.status, "token_returned": False}
                if len(self.report["integrity_responses"]) < 20:
                    self.report["integrity_responses"].append(summary)
                try:
                    body = await response.json()
                    summary["token_returned"] = isinstance(body, dict) and bool(body.get("token"))
                except Exception as error:
                    summary["parse_error"] = error_code(error)
            if response.url != GQL_URL:
                return
            operations = response.request.post_data_json
            operations = operations if isinstance(operations, list) else [operations]
            if not any(persisted_dashboard(op) for op in operations):
                return
            body = await response.json()
            replies = body if isinstance(body, list) else [body]
            if len(operations) != len(replies):
                raise ProbeFailure("unexpected_gql_batch_shape")
            headers = {key.lower(): value for key, value in (await response.request.all_headers()).items()}
            for operation, reply in zip(operations, replies):
                payload = persisted_dashboard(operation)
                if payload is None:
                    continue
                state = {
                    "http_status": response.status, **dashboard_state(reply, self.user_id),
                    "oauth_matches": headers.get("authorization") == f"OAuth {self.token}",
                    "web_client_matches": headers.get("client-id") == WEB_CLIENT_ID,
                    "integrity_header_present": bool(headers.get("client-integrity")),
                    "device_header_present": bool(headers.get("x-device-id")),
                }
                if len(self.report["dashboard_responses"]) < 20:
                    self.report["dashboard_responses"].append(state)
                if (
                    self.control is None and accepted(state) and state["oauth_matches"]
                    and state["web_client_matches"] and state["device_header_present"]
                    and headers.get("user-agent")
                ):
                    self.control = payload, {key: value for key, value in headers.items() if key in READ_HEADERS}
                    self.ready.set()
        except Exception as error:
            self.report["capture_error"] = str(error) if isinstance(error, ProbeFailure) else error_code(error)

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.control = None
        self.token = ""


async def check(cookie_file: Path, channel: str, proxy: str | None, seconds: int) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {"state": "failed", "phase": "cookie_load"}
    try:
        token = read_token(cookie_file)
        proxy_options = browser_proxy_settings(proxy) if proxy else None
        # Build after main() enables system trust; aiohttp may cache an SSL
        # context at import time. Verification and hostname checks stay enabled.
        tls = ssl.create_default_context()
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()) as http:
            report["phase"] = "token_validation"
            async with http.get(
                VALIDATE_URL, headers={"Authorization": f"OAuth {token}"}, proxy=proxy,
                allow_redirects=False, ssl=tls,
            ) as response:
                report["validation_http_status"] = response.status
                if response.status != 200:
                    raise ProbeFailure("token_validation_failed")
                identity = await response.json()
            if not isinstance(identity, dict) or identity.get("client_id") != WEB_CLIENT_ID:
                raise ProbeFailure("web_issued_token_required")
            user_id = str(identity.get("user_id", ""))
            if not user_id.isdecimal():
                raise ProbeFailure("validation_has_no_user_id")
            report["web_token_valid"] = True

            from playwright.async_api import async_playwright
            capture = BrowserCapture(token, user_id, report)
            try:
                async with async_playwright() as runtime:
                    report["phase"] = "browser_launch"
                    launch: dict[str, Any] = {"headless": False}
                    if channel != "chromium":
                        launch["channel"] = channel
                    if proxy_options:
                        launch["proxy"] = proxy_options
                    browser = await runtime.chromium.launch(**launch)
                    try:
                        report["browser_version"] = browser.version
                        report["proxy_auth"] = bool(proxy_options and "username" in proxy_options)
                        context = await browser.new_context()
                        await context.add_cookies([{
                            "name": "auth-token", "value": token, "url": "https://www.twitch.tv/",
                            "secure": True, "sameSite": "Lax",
                        }])
                        page = await context.new_page()
                        page.on("response", capture.response)
                        page.on("requestfailed", capture.resource_failure)
                        page.on("pageerror", capture.page_error)
                        report["phase"] = "website_dashboard"
                        deadline = asyncio.get_running_loop().time() + seconds
                        await page.goto(WEB_URL, wait_until="domcontentloaded", timeout=seconds * 1000)
                        try:
                            await asyncio.wait_for(
                                capture.ready.wait(), max(0, deadline - asyncio.get_running_loop().time()),
                            )
                        except asyncio.TimeoutError:
                            raise ProbeFailure("no_accepted_website_dashboard") from None
                        # Ensure the imported login did not change before the control read.
                        cookies = await context.cookies("https://www.twitch.tv")
                        if next((c["value"] for c in cookies if c["name"] == "auth-token"), None) != token:
                            raise ProbeFailure("browser_login_changed")
                        assert capture.control is not None
                        payload, headers = capture.control
                        report["phase"] = "python_dashboard"
                        async with http.post(
                            GQL_URL, json=payload, headers=headers, proxy=proxy,
                            allow_redirects=False, ssl=tls,
                        ) as response:
                            result = {"http_status": response.status, **dashboard_state(await response.json(), user_id)}
                        report["python_dashboard"] = result
                        if not accepted(result):
                            raise ProbeFailure("python_dashboard_not_accepted")
                        report.update(state="passed", phase="complete")
                    finally:
                        await capture.close()
                        await browser.close()
            finally:
                await capture.close()
        return 0, report
    except Exception as error:
        report["state"] = "failed"
        report["error"] = str(error) if isinstance(error, ProbeFailure) else error_code(error)
        return 1, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", type=Path, required=True, help="Trusted local miner cookies.jar; read only")
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chrome")
    parser.add_argument("--proxy-env", help="Read the current proxy URL from this environment variable")
    parser.add_argument("--seconds", type=int, default=60, help="Website observation limit (10-300 seconds)")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 300:
        parser.error("--seconds must be between 10 and 300")
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        parser.error("The selected proxy environment variable is empty or unset")
    import truststore
    truststore.inject_into_ssl()
    status, report = asyncio.run(check(args.cookie_file.expanduser(), args.channel, proxy, args.seconds))
    print(json.dumps(report, indent=2), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
