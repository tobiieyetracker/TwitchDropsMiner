import asyncio
import json
import ssl
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from yarl import URL

import check_campaign_auth as probe


TOKEN = "fixture-oauth-secret"
INTEGRITY = "fixture-integrity-secret"
QUERY = {
    "operationName": "ViewerDropsDashboard", "variables": {"fetchRewardCampaigns": False},
    "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "a" * 64}},
}
HEADERS = {
    "Authorization": f"OAuth {TOKEN}", "Client-Id": probe.WEB_CLIENT_ID,
    "X-Device-Id": "fixture-device", "Client-Session-Id": "fixture-session",
    "Client-Version": "fixture-version", "User-Agent": "fixture-browser",
    "Client-Integrity": INTEGRITY, "Cookie": "must-not-copy", "Proxy-Authorization": "must-not-copy",
}
SUCCESS = {"data": {"currentUser": {"id": "42", "dropCampaigns": [{"id": "campaign"}]}}}
DENIED = {
    "data": {"currentUser": {"id": "42", "dropCampaigns": None}},
    "errors": [{"message": "failed integrity check"}],
    "extensions": {"challenge": {"type": "integrity"}},
}


def response(body, *, operations=QUERY, headers=HEADERS, status=200, url=probe.GQL_URL):
    return SimpleNamespace(
        url=url, status=status, json=AsyncMock(return_value=body),
        request=SimpleNamespace(
            post_data_json=operations, all_headers=AsyncMock(return_value=headers), url=url,
            method="POST", resource_type="fetch", failure=None,
        ),
    )


def test_null_without_errors_is_not_reported_as_proven_integrity_failure():
    state = probe.dashboard_state({"data": {"currentUser": {"id": "42", "dropCampaigns": None}}}, "42")
    assert state["campaigns_state"] == "null"
    assert state["integrity_failure"] is False
    assert not probe.accepted({"http_status": 200, **state})


def test_empty_dashboard_is_a_success_but_wrong_user_is_not():
    body = {"data": {"currentUser": {"id": "42", "dropCampaigns": []}}}
    state = {"http_status": 200, **probe.dashboard_state(body, "42")}
    assert probe.accepted(state) and state["campaign_count"] == 0
    assert not probe.accepted({"http_status": 200, **probe.dashboard_state(body, "43")})


def test_capture_reports_resource_and_integrity_failures_without_secrets():
    async def scenario():
        report = {}
        capture = probe.BrowserCapture(TOKEN, "42", report)
        capture.resource_failure(SimpleNamespace(
            url="https://assets.twitch.tv/secret?token=must-not-print",
            failure="net::ERR_CERT_AUTHORITY_INVALID must-not-print", resource_type="script",
        ))
        capture.page_error(RuntimeError("must-not-print"))
        await capture.inspect(response(
            {"token": INTEGRITY}, url="https://gql.twitch.tv/integrity",
        ))
        await capture.inspect(response(DENIED))
        assert report["resource_failures"][0] == {
            "host": "assets.twitch.tv", "http_status": None, "error": "ERR_CERT_AUTHORITY_INVALID",
            "resource_type": "script", "role": "page_resource",
        }
        assert report["integrity_responses"] == [{"http_status": 200, "token_returned": True}]
        invalid_json = response({}, status=503, url="https://gql.twitch.tv/integrity")
        invalid_json.json.side_effect = ValueError("must-not-print")
        await capture.inspect(invalid_json)
        assert report["integrity_responses"][-1] == {
            "http_status": 503, "token_returned": False, "parse_error": "ValueError",
        }
        assert report["page_errors"] == 1
        assert report["dashboard_responses"][0]["integrity_failure"] is True
        assert not capture.ready.is_set()
        assert capture.control is None
        output = json.dumps(report)
        assert all(secret not in output for secret in (TOKEN, INTEGRITY, "must-not-print", "fixture-device"))
        await capture.close()

    asyncio.run(scenario())


def test_capture_extracts_only_dashboard_from_batch_with_mutation():
    async def scenario():
        capture = probe.BrowserCapture(TOKEN, "42", {})
        await capture.inspect(response(
            [{"data": {"claimDrop": "success"}}, SUCCESS],
            operations=[{"operationName": "DropsPage_ClaimDropRewards", "variables": {}}, QUERY],
        ))
        assert capture.ready.is_set()
        query, headers = capture.control
        assert query == QUERY
        assert headers["client-integrity"] == INTEGRITY
        assert "cookie" not in headers and "proxy-authorization" not in headers
        await capture.close()
        assert capture.control is None and capture.token == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["none", "pending", "failed", "partial", "finished"])
def test_integrity_lifecycle_distinguishes_no_request_from_no_response(phase):
    report = {"integrity_responses": []}
    observation = probe.NetworkObservation(report)
    reply = response({}, url="https://gql.twitch.tv/integrity?must-not-print")
    if phase != "none":
        observation.request(reply.request)
    if phase in {"partial", "finished"}:
        observation.response(reply)
    if phase == "finished":
        observation.finished(reply.request)
    if phase == "failed":
        reply.request.failure = "net::ERR_CERT_AUTHORITY_INVALID must-not-print"
        observation.failed(reply.request)
    observation.freeze()
    counts = report["integrity_network"]
    assert counts["requests"] == int(phase != "none")
    assert counts["request_methods"] == ({} if phase == "none" else {"POST": 1})
    assert counts["responses"] == int(phase in {"partial", "finished"})
    assert counts["awaiting_response"] == int(phase == "pending")
    assert counts["awaiting_body"] == int(phase == "partial")
    assert counts["finished"] == int(phase == "finished")
    assert counts["failed"] == int(phase == "failed")
    # The old response-body list can be empty in all of these different states.
    assert report["integrity_responses"] == []
    snapshot = deepcopy(report)
    observation.finished(reply.request)
    observation.freeze()
    assert report == snapshot
    assert "must-not-print" not in json.dumps(report)


def test_failure_summary_retains_sdk_script_after_twenty_image_errors():
    async def scenario():
        report = {}
        capture = probe.BrowserCapture(TOKEN, "42", report)
        for _ in range(25):
            request = SimpleNamespace(
                url="https://static-cdn.jtvnw.net/fixture-secret.png?token=must-not-print",
                resource_type="image", failure="net::ERR_CERT_AUTHORITY_INVALID",
            )
            capture.network.request(request)
            capture.resource_failure(request)
        sdk = SimpleNamespace(
            url="https://k.twitchcdn.net/fixture-secret/p.js?token=must-not-print",
            resource_type="script", failure="net::ERR_TIMED_OUT",
        )
        capture.network.request(sdk)
        capture.resource_failure(sdk)
        await capture.close()
        assert len(report["resource_failures"]) == 20
        groups = {item["host"]: item for item in report["network_summary"]}
        assert groups["static-cdn.jtvnw.net"]["resource_type"] == "image"
        assert groups["static-cdn.jtvnw.net"]["failed"] == 25
        assert groups["k.twitchcdn.net"]["resource_type"] == "script"
        assert groups["k.twitchcdn.net"]["role"] == "integrity_sdk_script"
        assert groups["k.twitchcdn.net"]["network_errors"] == {"ERR_TIMED_OUT": 1}
        assert groups["k.twitchcdn.net"]["outstanding"] == 0
        assert "must-not-print" not in json.dumps(report) and "fixture-secret" not in json.dumps(report)

    asyncio.run(scenario())


def test_integrity_headers_are_counted_even_when_response_body_hangs():
    async def scenario():
        report = {}
        capture = probe.BrowserCapture(TOKEN, "42", report)

        async def unfinished_body():
            await asyncio.Event().wait()

        reply = response({}, url="https://gql.twitch.tv/integrity")
        reply.json = unfinished_body
        capture.network.request(reply.request)
        capture.response(reply)
        await asyncio.sleep(0)
        await capture.close()
        assert not capture.tasks
        assert report["integrity_network"]["requests"] == 1
        assert report["integrity_network"]["responses"] == 1
        assert report["integrity_network"]["awaiting_body"] == 1
        assert report["integrity_requests"][0]["http_status"] == 200
        assert report["integrity_requests"][0]["state"] == "awaiting_body"
        before = deepcopy(report)
        await capture.close()
        assert report == before

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["name", "hash", "raw_query", "variables"])
def test_unsupported_payloads_cannot_be_used_for_python_control(mutation):
    query = deepcopy(QUERY)
    if mutation == "name":
        query["operationName"] = "DropsPage_ClaimDropRewards"
    elif mutation == "hash":
        query["extensions"]["persistedQuery"]["sha256Hash"] = "bad"
    elif mutation == "raw_query":
        query["query"] = "mutation { doSomething }"
    else:
        query["variables"] = []
    assert probe.persisted_dashboard(query) is None


@pytest.mark.parametrize("case", ["success", "denied", "wrong_oauth", "wrong_issuer", "changed_login", "python_denied", "launch_failed"])
def test_standalone_probe_uses_normal_browser_and_preserves_cookie(monkeypatch, tmp_path, case):
    import playwright.async_api

    callbacks = {}
    headers = {**HEADERS, "Authorization": "OAuth wrong-fixture-token"} if case == "wrong_oauth" else HEADERS

    async def navigate(*args, **kwargs):
        reply = response(DENIED if case == "denied" else SUCCESS, headers=headers)
        callbacks["request"](reply.request)
        callbacks["response"](reply)
        callbacks["requestfinished"](reply.request)
        await asyncio.sleep(0)

    page = SimpleNamespace(
        on=lambda event, callback: callbacks.update({event: callback}), goto=AsyncMock(side_effect=navigate),
        evaluate=AsyncMock(return_value={"sdk_global_present": False, "sdk_ready": None}),
    )
    context = SimpleNamespace(
        on=lambda event, callback: callbacks.update({event: callback}),
        add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page),
        cookies=AsyncMock(return_value=[{"name": "auth-token", "value": "changed" if case == "changed_login" else TOKEN}]),
    )
    browser = SimpleNamespace(
        version="fixture-browser", new_context=AsyncMock(return_value=context), close=AsyncMock(),
    )
    launch = AsyncMock(return_value=browser)
    if case == "launch_failed":
        launch.side_effect = RuntimeError("launch failed with fixture-proxy-password")
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    class Manager:
        async def __aenter__(self):
            return runtime

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(playwright.async_api, "async_playwright", Manager)
    post_calls = []
    proxy = "http://fixture-user:fixture-proxy-password@proxy.example:3128"

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @asynccontextmanager
        async def get(self, url, **kwargs):
            assert url == probe.VALIDATE_URL and kwargs["headers"] == {"Authorization": f"OAuth {TOKEN}"}
            assert kwargs["proxy"] == proxy and kwargs["allow_redirects"] is False
            assert kwargs["ssl"].check_hostname and kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
            yield response({"client_id": "other" if case == "wrong_issuer" else probe.WEB_CLIENT_ID, "user_id": "42"})

        @asynccontextmanager
        async def post(self, url, **kwargs):
            post_calls.append((url, kwargs))
            yield response(DENIED if case == "python_denied" else SUCCESS)

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: Http())

    async def scenario():
        path = tmp_path / "cookies.jar"
        jar = aiohttp.CookieJar()
        jar.update_cookies({"auth-token": TOKEN}, URL("https://www.twitch.tv/"))
        jar.save(path)
        original = path.read_bytes()
        status, report = await probe.check(path, "chrome", proxy, 0.05)
        assert status == (0 if case == "success" else 1)
        assert path.read_bytes() == original
        if case == "wrong_issuer":
            launch.assert_not_awaited()
        else:
            launch.assert_awaited_once_with(
                headless=False, channel="chrome",
                proxy={"server": "http://proxy.example:3128", "username": "fixture-user", "password": "fixture-proxy-password"},
            )
        if case not in {"wrong_issuer", "launch_failed"}:
            browser.new_context.assert_awaited_once_with()
            context.add_cookies.assert_awaited_once_with([{
                "name": "auth-token", "value": TOKEN, "url": "https://www.twitch.tv/",
                "secure": True, "sameSite": "Lax",
            }])
            browser.close.assert_awaited_once()
            page.evaluate.assert_awaited_once_with(probe.PAGE_STATUS)
            assert report["network_summary"][0]["started"] == 1
            assert report["network_summary"][0]["finished"] == 1
            assert report["integrity_network"]["requests"] == 0
        if case in {"success", "python_denied"}:
            assert len(post_calls) == 1
            url, options = post_calls[0]
            assert url == probe.GQL_URL and options["json"] == QUERY
            assert options["headers"]["authorization"] == f"OAuth {TOKEN}"
            assert options["headers"]["client-integrity"] == INTEGRITY
            assert options["allow_redirects"] is False
            assert options["ssl"].check_hostname and options["ssl"].verify_mode == ssl.CERT_REQUIRED
            assert "cookie" not in options["headers"] and "proxy-authorization" not in options["headers"]
        else:
            assert not post_calls
        if case == "denied":
            assert report["dashboard_responses"][0]["integrity_failure"] is True
        if case == "wrong_oauth":
            assert report["dashboard_responses"][0]["oauth_matches"] is False
        output = json.dumps(report)
        assert all(secret not in output for secret in (TOKEN, INTEGRITY, "fixture-proxy-password", "fixture-device"))

    asyncio.run(scenario())
