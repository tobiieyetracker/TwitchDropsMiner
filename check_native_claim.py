"""Claim one journal-bound reward through Twitch's own inventory page.

This is a bounded validation path, not a background miner. The page owns its
normal integrity flow. No dashboard success, generated token, replacement SDK,
or Python mutation is involved. An existing native attempt is read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import ssl
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from browser_cookie_import import load_browser_cookies
from check_campaign_auth import (
    BrowserCapture, ProbeFailure, check_imported_cookies, shutdown_browser,
    stop_on_rate_limit,
)
from constants import GQL_QUERIES
from finish_channel_drop import (
    DEFAULT_STATE, FinishClient, inventory_identity, read_inventory, reconcile_previous,
)
from finish_drop_state import refresh_target
from native_claim_dom import WEBSITE_READ_OPERATIONS, benefits_for_target, locate_claim_button
from native_claim_state import NativeClaimGate, NativeClaimJournal
from watch_check_state import WatchCheckError, summarize_challenge
from web_session import GQL_URL, WEB_CLIENT_ID, HEADER_NAMES, browser_proxy_settings


INVENTORY_URL = "https://www.twitch.tv/drops/inventory"
INTEGRITY_URL = "https://gql.twitch.tv/integrity"
CLAIM_OPERATION = "DropsPage_ClaimDropRewards"
MAX_SECONDS = 180
MAX_PYTHON_REQUESTS = 8
IDENTITY_HEADERS = tuple(name.lower() for name in HEADER_NAMES)


def safe_error(error):
    if isinstance(error, WatchCheckError):
        return error.code
    if isinstance(error, ProbeFailure):
        return str(error)
    return type(error).__name__


def connection_diagnostic(error):
    """Bounded exception types and source locations; never messages or locals."""
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 4:
        seen.add(id(error))
        frames, trace = [], error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            filename = Path(code.co_filename).name
            function = code.co_name
            frames.append({
                "file": filename if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename) else "unknown",
                "function": function if re.fullmatch(r"[A-Za-z0-9_<>]{1,80}", function) else "unknown",
                "line": trace.tb_lineno,
            })
            trace = trace.tb_next
        result.append({"type": type(error).__name__, "frames": frames[-6:]})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return result


def operations_in(request):
    value = request.post_data_json
    operations = value if isinstance(value, list) else [value]
    if not operations or not all(isinstance(op, dict) for op in operations):
        raise WatchCheckError("native_gql_body_invalid")
    return operations


def is_claim(operation):
    # Guard every reward-claim spelling, including ones the saved site bundle
    # did not use. Only the exact approved operation is allowed by the gate.
    name = operation.get("operationName", "")
    return name not in WEBSITE_READ_OPERATIONS and "claim" in str(name).lower()


def matching_headers(headers, token, device):
    headers = {key.lower(): value for key, value in headers.items()}
    if (not all(isinstance(headers.get(key), str) and headers[key] for key in IDENTITY_HEADERS)
            or headers["authorization"] != f"OAuth {token}"
            or headers["client-id"] != WEB_CLIENT_ID
            or not device or headers["x-device-id"] != device):
        raise WatchCheckError("native_website_identity_mismatch")
    return {key: headers[key] for key in IDENTITY_HEADERS}


class InventoryClient(FinishClient):
    """Python can only validate the account and read Inventory."""

    def __init__(self, report, proxy, clock, deadline):
        super().__init__(report, proxy, clock, deadline, reconcile_only=True)
        self.request_limit = MAX_PYTHON_REQUESTS
        # Build after main enables system trust, matching the already-working
        # campaign probe. aiohttp's import-time context may predate that change.
        self._tls_context = ssl.create_default_context()

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        kwargs["ssl"] = self._tls_context
        try:
            async with super().request(method, url, **kwargs) as response:
                yield response
        except Exception as error:
            self.report["request_failure"] = connection_diagnostic(error)
            raise


class NativeCapture(BrowserCapture):
    def __init__(self, token, user_id, report, target, device):
        super().__init__(token, user_id, report, expected_device=device)
        self.target, self.device = target, device
        self.inventory_ready = asyncio.Event()
        self.claim_done = asyncio.Event()
        self.first_response_ready = asyncio.Event()
        self.issuance_ready = asyncio.Event()
        self.fatal = asyncio.Event()
        self.gate = None
        self.website_headers = None
        self.benefits = None
        self.inventory_observed_at = None
        self.claim_requests = set()
        self.response_times = {}
        self.armed = False
        report.update(website_inventory=[], native_claim_responses=[], blocked_claims=0,
                      blocked_operations=[])

    def fail(self, error):
        self.report.setdefault("native_error", safe_error(error))
        self.armed = False
        if self.gate is not None:
            self.gate.observe_failure()
        self.fatal.set()

    def response(self, response):
        if not self.closed and response.url in (GQL_URL, INTEGRITY_URL):
            # Older Windows Python monotonic clocks can have a 15 ms tick.
            # Keep distinct response events ordered without rounding them into
            # one tick and falsely rejecting a prompt official recovery.
            self.response_times[response.request] = time.perf_counter()
        super().response(response)

    async def guarded(self, awaitable, timeout=None):
        """Wake immediately for 429 or a target/identity/transport failure."""
        async def run():
            operation = asyncio.create_task(awaitable)
            failed = asyncio.create_task(self.fatal.wait())
            try:
                await asyncio.wait({operation, failed}, return_when=asyncio.FIRST_COMPLETED)
                if self.fatal.is_set():
                    raise WatchCheckError(self.report["native_error"])
                return await operation
            finally:
                for task in (operation, failed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(operation, failed, return_exceptions=True)
        return await stop_on_rate_limit(run(), self, timeout)

    async def matching_issuance(self, headers):
        while True:
            self.issuance_ready.clear()
            if self.gate.recovery_evidence_ready(headers):
                return
            await self.issuance_ready.wait()

    async def route(self, route, request):
        if self.closed or self.rate_limited.is_set() or self.fatal.is_set():
            await route.abort()
            return
        try:
            if request.method == "OPTIONS":
                await route.continue_()
                return
            operations = operations_in(request)
            claims = [op for op in operations if is_claim(op)]
            # Raw mutations are never needed by this persisted-query flow.
            if any("query" in op and "mutation" in str(op["query"]).lower() for op in operations):
                raise WatchCheckError("native_raw_mutation_blocked")
            if not claims and any(op.get("operationName") not in WEBSITE_READ_OPERATIONS for op in operations):
                # A persisted operation's name alone does not imply a read.
                # Do not let unrelated/unknown mutations run during this test.
                names = self.report["blocked_operations"]
                for op in operations:
                    name = op.get("operationName")
                    if name not in WEBSITE_READ_OPERATIONS:
                        label = name if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,99}", name) else "unknown"
                        if label not in names and len(names) < 30:
                            names.append(label)
                await route.abort()
                return
            if claims:
                if not self.armed or self.gate is None or len(operations) != 1:
                    raise WatchCheckError("native_unarmed_or_batched_claim")
                headers = await request.all_headers()
                if self.claim_requests:
                    # Playwright callbacks may parse the first response later
                    # than the webpage sees it. Never guess its result.
                    await self.guarded(self.first_response_ready.wait(), 5)
                    await self.guarded(self.matching_issuance(headers), 5)
                self.check_rate_limit()
                if self.fatal.is_set():
                    raise WatchCheckError(self.report["native_error"])
                self.gate.authorize_request(claims[0], headers)
                self.claim_requests.add(request)
                self.report["claim"]["attempted"] = True
                self.report["claim"]["wire_requests_reserved"] = len(self.claim_requests)
                self.report["state"] = "claim_unconfirmed"
            # Continue the browser's original transport, payload, and headers.
            # route.fetch() would replace that transport with an API request.
            await route.continue_()
        except Exception as error:
            self.report["blocked_claims"] += 1
            self.fail(error)
            await route.abort()

    def resource_failure(self, request, *, status=None):
        super().resource_failure(request, status=status)
        if request in self.claim_requests and not self.closed:
            self.first_response_ready.set()
            self.fail(WatchCheckError("native_claim_transport_unknown"))

    async def inspect(self, response, summary=None):
        # Reuse passive SDK/network reporting. It cannot submit GQL requests.
        await super().inspect(response, summary)
        if self.closed or self.rate_limited.is_set():
            return
        try:
            if response.url == INTEGRITY_URL and response.status == 200 and self.gate is not None:
                body = await response.json()
                headers = await response.request.all_headers()
                if isinstance(body, dict) and isinstance(body.get("token"), str):
                    if self.gate.observe_issuance(body, headers, http_status=response.status,
                                                 received_at=self.response_times.get(response.request)):
                        self.issuance_ready.set()
            if response.url != GQL_URL:
                return
            operations = operations_in(response.request)
            if not any(op.get("operationName") == "Inventory" or is_claim(op) for op in operations):
                return
            if response.status != 200:
                raise WatchCheckError("native_relevant_gql_http_error")
            body = await response.json()
            replies = body if isinstance(body, list) else [body]
            if len(operations) != len(replies):
                raise WatchCheckError("native_gql_batch_shape")
            headers = await response.request.all_headers()
            for operation, reply in zip(operations, replies):
                if is_claim(operation):
                    if response.request not in self.claim_requests or self.gate is None:
                        raise WatchCheckError("native_untracked_claim_response")
                    self.report["native_claim_responses"].append({
                        "http_status": response.status,
                        "response_challenge": summarize_challenge(reply),
                        "integrity_header_present": bool({k.lower(): v for k, v in headers.items()}.get("client-integrity")),
                    })
                    recovery = self.gate.observe_response(
                        reply, http_status=response.status,
                        received_at=self.response_times.get(response.request),
                    )
                    self.first_response_ready.set()
                    if not recovery or len(self.claim_requests) >= 2:
                        self.claim_done.set()
                elif operation.get("operationName") == "Inventory":
                    entry = {"http_status": response.status, "response_challenge": summarize_challenge(reply)}
                    self.report["website_inventory"].append(entry)
                    # A challenged Inventory may recover through the official
                    # page. It is never accepted as the pre-click baseline.
                    if entry["response_challenge"]["present"]:
                        continue
                    inventory_identity(reply, self.user_id)
                    identity = matching_headers(headers, self.token, self.device)
                    current = refresh_target(reply, self.user_id, self.target)
                    if current is None:
                        raise WatchCheckError("native_target_missing_from_website")
                    entry.update(user_matches=True, target=current.public_dict())
                    if self.inventory_ready.is_set():
                        if identity != self.website_headers:
                            raise WatchCheckError("native_website_identity_changed")
                        continue
                    self.website_headers = identity
                    self.target = current
                    if not current.is_claimed:
                        current.require_claim_id()
                        self.benefits = benefits_for_target(reply, current)
                    self.inventory_observed_at = self.response_times.get(response.request)
                    if self.inventory_observed_at is None:
                        raise WatchCheckError("native_inventory_observation_time_missing")
                    self.inventory_ready.set()
        except Exception as error:
            if response.request in self.claim_requests:
                self.first_response_ready.set()
            self.fail(error)

    async def close(self):
        try:
            await super().close()
        finally:
            if self.gate is not None:
                self.report["claim"]["gate"] = self.gate.summary
                self.gate.clear()
                self.gate = None
            self.website_headers = None
            self.benefits = None
            self.claim_requests.clear()
            self.response_times.clear()
            self.target = None
            self.device = None


async def run_browser(client, journal, imported, target, channel, proxy, seconds):
    from playwright.async_api import async_playwright

    report = client.report
    capture = NativeCapture(imported.auth_token, str(client._auth_state.user_id), report,
                            target, imported.unique_id)
    options = browser_proxy_settings(proxy) if proxy else None
    report["proxy_auth"] = bool(options and "username" in options)
    try:
        async with async_playwright() as runtime:
            launch = {"headless": False}
            if channel != "chromium":
                launch["channel"] = channel
            if options:
                launch["proxy"] = options
            client.phase = "native_browser_launch"
            browser = await runtime.chromium.launch(**launch)
            try:
                report["browser_version"] = browser.version
                context = await browser.new_context(locale="en-US", service_workers="block")
                await context.add_cookies(imported.cookies)
                report["cookie_import"].update(check_imported_cookies(
                    await context.cookies(INVENTORY_URL), imported.auth_token, imported.unique_id,
                ))
                context.on("request", capture.network.request)
                context.on("response", capture.response)
                context.on("requestfinished", capture.network.finished)
                context.on("requestfailed", capture.resource_failure)
                await context.route(GQL_URL, capture.route)
                page = await context.new_page()
                page.on("pageerror", capture.page_error)
                client.phase = "native_inventory_page"
                deadline = time.monotonic() + seconds
                await capture.guarded(page.goto(INVENTORY_URL, wait_until="domcontentloaded",
                                                timeout=seconds * 1000), seconds)
                await capture.guarded(capture.inventory_ready.wait(), max(0, deadline - time.monotonic()))
                if capture.target.is_claimed:
                    report["state"] = "already_claimed"
                    return capture
                button = await capture.guarded(
                    locate_claim_button(page, capture.target, capture.benefits),
                    max(0, deadline - time.monotonic()),
                )
                # Re-read the server immediately before enabling the page's
                # mutation. The gate also checks age of the website baseline.
                await capture.guarded(client.validate_identity(), client.remaining())
                latest = await capture.guarded(read_inventory(client, "native_preclick", target=capture.target),
                                               client.remaining())
                if latest is None:
                    raise WatchCheckError("native_preclick_target_missing")
                if latest.is_claimed:
                    report["state"] = "already_claimed"
                    return capture
                latest.require_claim_id()
                if latest.account_link_state is not True:
                    raise WatchCheckError("native_account_link_not_confirmed")
                report["target"] = latest.public_dict()
                capture.gate = NativeClaimGate(
                    journal, str(client._auth_state.user_id), latest,
                    inventory_observed_at=capture.inventory_observed_at,
                    expected_headers=capture.website_headers,
                    clock=time.perf_counter,
                )
                capture.armed = True
                client.phase = "native_claim_click"
                report["claim"]["click_attempted"] = True
                await capture.guarded(button.click(timeout=5000), 6)
                client.phase = "native_claim_response"
                await capture.guarded(capture.claim_done.wait(), min(45, client.remaining()))
            finally:
                capture.armed = False
                capture.network.freeze()
                close_task = asyncio.create_task(shutdown_browser(browser, capture))
                try:
                    await capture.close()
                finally:
                    await close_task
    except Exception as error:
        report["browser_error"] = safe_error(error)
    finally:
        await capture.close()
    return capture


async def check(cookie_file, campaign_name, proxy=None, state_dir=DEFAULT_STATE, channel="chrome", seconds=60,
                *, python_proxy=None):
    report = {"state": "failed", "mode": "native_inventory_claim", "requests": [],
              "inventory_checks": [], "current_checks": [], "watch_sends": 0,
              "claim": {"attempted": False, "previous_attempt": False, "confirmed": False},
              "limits": {"total_seconds": MAX_SECONDS, "python_requests": MAX_PYTHON_REQUESTS,
                         "browser_navigations": 1, "native_clicks": 1, "claim_wire_requests": 2}}
    report["separate_python_proxy"] = python_proxy is not None
    clock = SimpleNamespace(time=time.monotonic, sleep=asyncio.sleep)
    start = clock.time()
    # Muse's relay exists because Chromium cannot reach the same platform
    # upstream directly. Python already can; it need not depend on that relay.
    client = InventoryClient(report, python_proxy if python_proxy is not None else proxy,
                             clock, start + MAX_SECONDS)
    try:
        if not 10 <= seconds <= 90 or not campaign_name.strip():
            raise WatchCheckError("native_parameters_invalid")
        with NativeClaimJournal(state_dir) as journal:
            original, _ = journal.originals()
            saved = journal.read()
            if original["campaign_name"] != campaign_name:
                raise WatchCheckError("native_original_target_mismatch")
            async def run():
                client.phase = "native_identity"
                await client.open(cookie_file)
                if str(client._auth_state.user_id) != original["user_id"]:
                    raise WatchCheckError("native_original_account_mismatch")
                if saved is not None:
                    report["mode"] = "native_inventory_reconcile"
                    await reconcile_previous(client, journal, saved, campaign_name, original["drop_id"])
                    return
                target = await read_inventory(client, "native_preflight", campaign_name=campaign_name,
                                              drop_id=original["drop_id"])
                if target.campaign_id != original["campaign_id"]:
                    raise WatchCheckError("native_original_target_mismatch")
                report["target"] = target.public_dict()
                if target.is_claimed:
                    report["state"] = "already_claimed"
                    return
                target.require_claim_id()
                if target.account_link_state is not True:
                    raise WatchCheckError("native_account_link_not_confirmed")
                imported = load_browser_cookies(cookie_file)
                if imported.auth_token != client.token or not imported.unique_id:
                    raise WatchCheckError("native_cookie_identity_mismatch")
                report["cookie_import"] = dict(imported.summary)
                capture = await run_browser(client, journal, imported, target, channel, proxy, seconds)
                # A 429 always stops new requests. Retain the attempt for a later
                # explicit read-only reconciliation; do not auto-resubmit.
                if capture.rate_limited.is_set():
                    raise WatchCheckError("native_rate_limited")
                if report.get("browser_shutdown", {}).get("state") != "closed":
                    raise WatchCheckError("native_browser_shutdown_unconfirmed")
                if not report["claim"]["attempted"]:
                    if report["state"] != "already_claimed":
                        raise WatchCheckError(report.get("browser_error", "native_no_claim_sent"))
                    return
                # Even a timeout/challenge/error may follow a committed mutation.
                # Close the browser before these reads so it cannot replay later.
                for index in range(3):
                    if index:
                        await clock.sleep(min(15, client.remaining()))
                    latest = await read_inventory(client, "native_claim_confirmation", target=target)
                    if latest is not None and latest.is_claimed:
                        journal.confirm()
                        report["state"] = "claim_confirmed"
                        report["claim"].update(confirmed=True, confirmation_source="Inventory.self.isClaimed")
                        return
                raise WatchCheckError("native_claim_not_confirmed_by_inventory")
            await asyncio.wait_for(run(), client.remaining())
    except Exception as error:
        report["error"] = safe_error(error)
        if report["state"] == "already_claimed":
            report["state"] = "failed"
    finally:
        report["phase"] = client.phase
        try:
            await client.close()
        except Exception as error:
            report["error"] = safe_error(error)
            report["state"] = "failed"
        report["elapsed_seconds"] = round(clock.time() - start, 3)
    return (0 if report["state"] in {"claim_confirmed", "already_claimed"} else 1), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", type=Path, required=True)
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chrome")
    parser.add_argument("--proxy-env")
    parser.add_argument("--python-proxy-env", help="Optional original upstream proxy for Python reads; browser keeps --proxy-env")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    python_proxy = os.environ.get(args.python_proxy_env) if args.python_proxy_env else None
    if args.python_proxy_env and not python_proxy:
        print(json.dumps({"state": "failed", "error": "python_proxy_environment_missing"}))
        return 1
    import truststore
    truststore.inject_into_ssl()
    code, report = asyncio.run(check(args.cookie_file, args.campaign_name, proxy,
                                    args.state_dir, args.channel, args.seconds,
                                    python_proxy=python_proxy))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
