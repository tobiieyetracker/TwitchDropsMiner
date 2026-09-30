"""Offline boundary tests: real journal/Python reads, simulated website transport."""
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

import check_native_claim as native
from check_channel_watch import WEB
from constants import GQL_QUERIES
from finish_drop_journal import FinishJournal
from native_claim_state import NativeClaimJournal
from test_channel_watch_check import Clock, TOKEN
from test_finish_channel_drop import CLAIM_ID, Network
from web_claim_journal import WebClaimJournal


DEVICE = "fixture-native-private-device"
INTEGRITY = "fixture-native-private-integrity"
HEADERS = {
    "Authorization": f"OAuth {TOKEN}", "Client-Id": native.WEB_CLIENT_ID,
    "User-Agent": "fixture-browser", "X-Device-Id": DEVICE,
    "Client-Session-Id": "fixture-private-session", "Client-Version": "fixture-version",
}
CLAIM = dict(GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}}))
SUCCESS = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
CHALLENGE = {"data": {"claimDropRewards": None}, "extensions": {"challenge": {"type": "integrity"}}}


class Request:
    def __init__(self, operation=None, headers=None, url=native.GQL_URL):
        self.post_data_json = deepcopy(operation)
        self.url, self.method, self.resource_type = url, "POST", "fetch"
        self.failure = None
        self.all_headers = AsyncMock(return_value=deepcopy(HEADERS if headers is None else headers))


def response(request, body, status=200):
    parser = AsyncMock(side_effect=body) if isinstance(body, Exception) else AsyncMock(return_value=body)
    return SimpleNamespace(request=request, url=request.url, status=status, headers={}, json=parser)


class Website:
    """Browser API boundary; all application-side reads and journal calls stay real."""
    version = "fixture-browser"

    def __init__(self, network, state_dir, behavior):
        self.network, self.state_dir, self.behavior = network, state_dir, behavior
        self.events, self.wire, self.aborted = {}, [], []
        self.closed, self.close_calls, self.clicks, self.navigations = False, 0, 0, 0
        self.capture = None

    async def launch(self, **kwargs):
        self.launch_options = deepcopy(kwargs)
        assert {key: value for key, value in kwargs.items() if key != "proxy"} == {
            "headless": False, "channel": "chrome",
        }
        return self

    async def new_context(self, **kwargs):
        assert kwargs == {"locale": "en-US", "service_workers": "block"}
        return self

    async def add_cookies(self, cookies):
        self.imported = deepcopy(cookies)

    async def cookies(self, url):
        assert url == native.INVENTORY_URL
        return self.imported

    def on(self, event, callback):
        self.events[event] = callback
        if event == "response":
            self.capture = callback.__self__

    async def route(self, url, callback):
        assert url == native.GQL_URL
        self.route_handler = callback

    async def new_page(self):
        return self

    async def emit(self, request, body, status=200):
        self.events["request"](request)
        route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        if request.url == native.GQL_URL:
            await self.route_handler(route, request)
            if route.abort.await_count:
                self.aborted.append(request)
                return False
            route.continue_.assert_awaited_once_with()
            if native.is_claim(request.post_data_json):
                # Durable reservation precedes the actual browser send.
                record = json.loads((self.state_dir / "native-inventory-claim-v1.json").read_text())
                assert record["requests_reserved"] == len(self.wire) + 1
                self.wire.append(request)
                if status == 200 and body != CHALLENGE and self.behavior != "success_not_committed":
                    self.network.initial_claimed = True
        self.events["response"](response(request, body, status))
        await asyncio.gather(*list(self.capture.tasks))
        self.events["requestfinished"](request)
        return True

    async def goto(self, url, **kwargs):
        assert url == native.INVENTORY_URL
        self.navigations += 1
        if self.behavior == "navigation_timeout":
            raise asyncio.TimeoutError(TOKEN)
        if self.behavior == "429_before":
            await self.emit(Request(url="https://k.twitchcdn.net/private-document"), {}, 429)
            return
        if self.behavior == "claimed_during_navigation_close_failure":
            self.network.initial_claimed = True
        inventory = self.network.inventory()
        if self.behavior == "wrong_website_user":
            inventory["data"]["currentUser"]["id"] = "43"
        await self.emit(Request(dict(GQL_QUERIES["Inventory"])), inventory)

    async def click(self, **kwargs):
        self.clicks += 1
        claim, headers = deepcopy(CLAIM), deepcopy(HEADERS)
        if self.behavior == "wrong_claim_identity":
            headers["Authorization"] = "OAuth fixture-wrong-account-secret"
        if self.behavior == "wrong_claim_target":
            claim["variables"]["input"]["dropInstanceID"] = "fixture-wrong-instance"
        if self.behavior == "raw_mutation":
            claim = {"operationName": "Other", "query": "mutation { sensitiveAction }"}
        if self.behavior == "batched_claim":
            claim = [claim, dict(GQL_QUERIES["Inventory"])]
        if self.behavior == "429_after":
            await self.emit(Request(claim, headers), {}, 429)
            return
        if self.behavior == "official_recovery":
            assert await self.emit(Request(claim, headers), CHALLENGE)
            await self.emit(Request(headers=headers, url=native.INTEGRITY_URL), {
                "token": INTEGRITY, "expiration": (time.time() + 300) * 1000,
            })
            headers["Client-Integrity"] = INTEGRITY
        reply = ValueError(TOKEN) if self.behavior == "unknown_response" else SUCCESS
        await self.emit(Request(claim, headers), reply)
        if self.behavior == "unknown_response":
            # A webpage's speculative duplicate must not be replayed by the probe.
            await self.emit(Request(claim, headers), SUCCESS)

    async def close(self):
        self.close_calls += 1
        if self.behavior.endswith("close_failure"):
            raise RuntimeError(TOKEN)
        self.closed = True


def scenario(monkeypatch, tmp_path, *, behavior="success", previous=False, claimed=False,
             browser_proxy=None, python_proxy=None):
    async def run():
        real_sleep = asyncio.sleep

        async def no_confirmation_delay(seconds):
            await real_sleep(0 if seconds >= 1 else seconds)

        monkeypatch.setattr(asyncio, "sleep", no_confirmation_delay)
        state_dir = tmp_path / "state"
        with FinishJournal(state_dir) as journal:
            journal.record_attempt("42", "campaign", "Test campaign", "drop")
        with WebClaimJournal(state_dir) as journal:
            journal.record_attempt("42", "campaign", "Test campaign", "drop")
        originals = {name: (state_dir / name).read_bytes() for name in ("journal.json", "web-query-claim-v1.json")}
        if previous:
            with NativeClaimJournal(state_dir) as journal:
                journal.record_attempt("42", "campaign", "Test campaign", "drop")

        jar = aiohttp.CookieJar()
        cookies = SimpleCookie()
        for name, value in (("auth-token", TOKEN), ("unique_id", DEVICE)):
            cookies[name] = value
            cookies[name]["domain"], cookies[name]["path"] = ".twitch.tv", "/"
        jar.update_cookies(cookies, WEB)
        cookie_file = tmp_path / "cookies.jar"
        jar.save(cookie_file)
        cookie_bytes = cookie_file.read_bytes()
        network = Network(Clock())
        network.minutes, network.initial_claimed = 60, claimed
        base_inventory = network.inventory

        def inventory():
            body = base_inventory()
            campaign = body["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]
            campaign["self"] = {"isAccountConnected": True}
            campaign["timeBasedDrops"][0]["self"]["hasPreconditionsMet"] = True
            return body

        network.inventory = inventory
        site = Website(network, state_dir, behavior)
        monkeypatch.setattr(aiohttp, "ClientSession", network.session)
        import playwright.async_api

        @asynccontextmanager
        async def runtime():
            yield SimpleNamespace(chromium=SimpleNamespace(launch=site.launch))

        monkeypatch.setattr(playwright.async_api, "async_playwright", runtime)
        monkeypatch.setattr(native, "benefits_for_target", lambda body, target: ("reward",))
        monkeypatch.setattr(native, "locate_claim_button", AsyncMock(return_value=site))
        original_read = native.read_inventory

        async def read(client, label, **kwargs):
            if label == "native_claim_confirmation":
                assert site.closed, "Read-only confirmation cannot race a still-live website mutation"
            return await original_read(client, label, **kwargs)

        monkeypatch.setattr(native, "read_inventory", read)
        code, report = await native.check(cookie_file, "Test campaign", proxy=browser_proxy,
                                          python_proxy=python_proxy, state_dir=state_dir)
        assert cookie_file.read_bytes() == cookie_bytes
        assert all((state_dir / name).read_bytes() == content for name, content in originals.items())
        assert not network.claims and not network.sends, "Python must remain Inventory-only"
        assert all(session.closed for session in network.sessions)
        encoded = json.dumps(report)
        assert all(secret not in encoded for secret in (
            TOKEN, DEVICE, INTEGRITY, CLAIM_ID, "fixture-wrong-account-secret", "fixture-wrong-instance",
            "fixture-private-session",
        ))
        record_path = state_dir / "native-inventory-claim-v1.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        if record:
            assert CLAIM_ID not in json.dumps(record) and TOKEN not in json.dumps(record)
        return code, report, site, network, record

    return asyncio.run(run())


def test_native_click_requires_same_target_inventory_confirmation(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(monkeypatch, tmp_path)
    assert code == 0 and report["state"] == "claim_confirmed"
    assert report["claim"]["confirmation_source"] == "Inventory.self.isClaimed"
    assert report["inventory_checks"][-1]["drops"][0]["is_claimed"] is True
    assert site.navigations == site.clicks == len(site.wire) == site.close_calls == 1
    assert record["outcome"] == "confirmed" and record["requests_reserved"] == 1
    assert report["browser_shutdown"]["state"] == "closed"
    assert site.capture.token == "" and site.capture.website_headers is None


def test_page_owned_integrity_recovery_needs_observed_issuance(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior="official_recovery")
    assert code == 0 and report["claim"]["confirmed"] is True, {
        key: report.get(key) for key in ("error", "browser_error", "native_error", "claim", "integrity_responses")
    }
    assert site.clicks == 1 and len(site.wire) == record["requests_reserved"] == 2
    assert site.wire[0].post_data_json == site.wire[1].post_data_json == CLAIM
    assert len(report["native_claim_responses"]) == 2
    assert report["native_claim_responses"][0]["response_challenge"] == {"present": True, "type": "integrity"}


@pytest.mark.parametrize("claimed", [False, True])
def test_prior_native_attempt_only_reads_inventory(monkeypatch, tmp_path, claimed):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, previous=True, claimed=claimed)
    assert code == (0 if claimed else 1)
    assert report["mode"] == "native_inventory_reconcile"
    assert report["claim"]["previous_attempt"] and not report["claim"]["attempted"]
    assert site.capture is None and site.clicks == 0
    assert len(network.calls) == 2
    assert record["requests_reserved"] == 1


@pytest.mark.parametrize("when", ["before", "after"])
def test_429_blocks_all_followup_mutations_and_python_confirmation(monkeypatch, tmp_path, when):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior=f"429_{when}")
    assert code == 1 and report["error"] == "native_rate_limited"
    assert site.closed and site.close_calls == 1
    assert len(site.wire) == (0 if when == "before" else 1)
    assert not any(item["checkpoint"] == "native_claim_confirmation" for item in report["inventory_checks"])
    assert (record is None) == (when == "before")


def test_unknown_claim_response_never_replays_but_can_reconcile_after_close(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior="unknown_response")
    assert code == 0 and report["state"] == "claim_confirmed"
    assert report["browser_error"] == "ValueError"
    assert len(site.wire) == 1 and len(site.aborted) == 1
    assert site.closed and record["requests_reserved"] == 1


@pytest.mark.parametrize("behavior,error", [
    ("wrong_website_user", "inventory_identity_not_confirmed"),
    ("wrong_claim_identity", "native_claim_identity_mismatch"),
    ("wrong_claim_target", "native_claim_target_mismatch"),
    ("raw_mutation", "native_raw_mutation_blocked"),
    ("batched_claim", "native_unarmed_or_batched_claim"),
])
def test_invalid_account_target_or_mutation_shape_cannot_reach_wire(monkeypatch, tmp_path, behavior, error):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior=behavior)
    assert code == 1 and report["error"] == error
    assert not site.wire and record is None and site.closed
    assert report["claim"]["attempted"] is False


def test_navigation_timeout_closes_browser_without_attempt_or_confirmation(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior="navigation_timeout")
    assert code == 1 and report["error"] == "TimeoutError"
    assert site.closed and site.close_calls == 1 and record is None
    assert site.clicks == 0 and len(network.calls) == 2


def test_close_failure_leaves_claim_unknown_and_does_not_start_confirmation(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior="close_failure")
    assert code == 1 and report["error"] == "native_browser_shutdown_unconfirmed"
    assert report["browser_shutdown"]["state"] == "failed"
    assert not site.closed and record["outcome"] == "attempted"
    assert len(site.wire) == 1
    assert not any(item["checkpoint"] == "native_claim_confirmation" for item in report["inventory_checks"])


def test_already_claimed_does_not_hide_browser_cleanup_failure(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(
        monkeypatch, tmp_path, behavior="claimed_during_navigation_close_failure",
    )
    assert code == 1 and report["error"] == "native_browser_shutdown_unconfirmed"
    assert report["browser_shutdown"]["state"] == "failed"
    assert not site.wire and record is None


def test_successful_mutation_response_alone_cannot_confirm_claim(monkeypatch, tmp_path):
    code, report, site, network, record = scenario(monkeypatch, tmp_path, behavior="success_not_committed")
    assert code == 1 and report["error"] == "native_claim_not_confirmed_by_inventory"
    assert site.closed and len(site.wire) == 1 and record["outcome"] == "attempted"
    assert report["claim"]["confirmed"] is False
    confirmations = [item for item in report["inventory_checks"] if item["checkpoint"] == "native_claim_confirmation"]
    assert len(confirmations) == 3
    assert all(item["drops"][0]["is_claimed"] is False for item in confirmations)


def test_unknown_persisted_operation_is_blocked_without_stopping_inventory_reads():
    async def run():
        report = {}
        capture = native.NativeCapture(TOKEN, "42", report, None, DEVICE)
        unknown = Request({"operationName": "UnreviewedMutation", "variables": {},
                           "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "a" * 64}}})
        denied = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        await capture.route(denied, unknown)
        denied.abort.assert_awaited_once_with()
        assert not denied.continue_.await_count and not capture.fatal.is_set()
        allowed = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        await capture.route(allowed, Request(dict(GQL_QUERIES["Inventory"])))
        allowed.continue_.assert_awaited_once_with()
        assert not allowed.abort.await_count
        await capture.close()

    asyncio.run(run())


def test_guard_timeout_cancels_pending_work():
    async def run():
        capture = native.NativeCapture(TOKEN, "42", {}, None, DEVICE)
        cancelled = asyncio.Event()

        async def pending():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(asyncio.TimeoutError):
            await capture.guarded(pending(), 0.01)
        assert cancelled.is_set()
        await capture.close()

    asyncio.run(run())


def test_browser_relay_and_python_upstream_are_separate_and_redacted(monkeypatch, tmp_path):
    browser_proxy = "http://fixture-browser-user:fixture-browser-password@127.0.0.1:32123"
    python_proxy = "http://fixture-python-user:fixture-python-password@platform.proxy.invalid:3128"
    code, report, site, network, record = scenario(
        monkeypatch, tmp_path, browser_proxy=browser_proxy, python_proxy=python_proxy,
    )
    assert code == 0 and report["state"] == "claim_confirmed"
    assert network.calls and all(call[3]["proxy"] == python_proxy for call in network.calls)
    assert site.launch_options["proxy"] == {
        "server": "http://127.0.0.1:32123",
        "username": "fixture-browser-user", "password": "fixture-browser-password",
    }
    assert report["separate_python_proxy"] is True and report["proxy_auth"] is True
    output = json.dumps(report)
    assert all(value not in output for value in (
        browser_proxy, python_proxy, "fixture-browser-user", "fixture-browser-password",
        "fixture-python-user", "fixture-python-password", "platform.proxy.invalid", "32123",
    ))


def test_missing_python_proxy_environment_stops_before_network(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", [
        "check_native_claim.py", "--cookie-file", str(tmp_path / "not-read.jar"),
        "--campaign-name", "Test campaign", "--proxy-env", "NATIVE_TEST_BROWSER_PROXY",
        "--python-proxy-env", "NATIVE_TEST_PYTHON_PROXY",
    ])
    monkeypatch.setenv("NATIVE_TEST_BROWSER_PROXY", "http://fixture-proxy-secret@127.0.0.1:32123")
    monkeypatch.delenv("NATIVE_TEST_PYTHON_PROXY", raising=False)
    check, sessions = AsyncMock(), Mock()
    monkeypatch.setattr(native, "check", check)
    monkeypatch.setattr(aiohttp, "ClientSession", sessions)
    logging_level = native.logging.root.manager.disable
    try:
        code = native.main()
    finally:
        native.logging.disable(logging_level)
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "state": "failed", "error": "python_proxy_environment_missing",
    }
    check.assert_not_called()
    sessions.assert_not_called()
