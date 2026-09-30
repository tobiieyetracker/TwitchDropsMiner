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
        request=SimpleNamespace(post_data_json=operations, all_headers=AsyncMock(return_value=headers), url=url),
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
            failure="net::ERR_CERT_AUTHORITY_INVALID must-not-print",
        ))
        capture.page_error(RuntimeError("must-not-print"))
        await capture.inspect(response(
            {"token": INTEGRITY}, url="https://gql.twitch.tv/integrity",
        ))
        await capture.inspect(response(DENIED))
        assert report["resource_failures"][0] == {
            "host": "assets.twitch.tv", "http_status": None, "error": "ERR_CERT_AUTHORITY_INVALID",
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
        callbacks["response"](response(DENIED if case == "denied" else SUCCESS, headers=headers))
        await asyncio.sleep(0)

    page = SimpleNamespace(on=lambda event, callback: callbacks.update({event: callback}), goto=AsyncMock(side_effect=navigate))
    context = SimpleNamespace(
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
