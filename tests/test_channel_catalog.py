"""Offline transport/response tests: no Twitch requests or real credentials."""
import asyncio
import json
import ssl
from contextlib import asynccontextmanager
from copy import deepcopy
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import check_channel_catalog as catalog
from constants import ClientType, GQL_QUERIES
from watch_check_state import WatchCheckError

TOKEN = "fixture-private-oauth"
PROXY = "http://proxy-user:fixture-private-proxy@proxy.invalid:8080"
VALIDATE = {"client_id": ClientType.WEB.CLIENT_ID, "user_id": "42"}
STREAM = {"data": {"user": {"id": "123", "stream": {"id": "456"},
          "broadcastSettings": {"game": {"id": "7", "name": "Test game"}}}}}
CANDIDATE = {"id": "campaign", "name": "Test campaign", "timeBasedDrops": [{
    "id": "drop", "name": "Reward", "requiredMinutesWatched": 60,
    "startAt": "2026-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z"}]}
AVAILABLE = {"data": {"channel": {"id": "123", "viewerDropCampaigns": [CANDIDATE]}}}
INVENTORY = {"data": {"currentUser": {"id": "42", "inventory": {"dropCampaignsInProgress": []}}}}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    async def json(self):
        return deepcopy(self.body)


class Network:
    def __init__(self, responses, close_error=False):
        self.responses, self.close_error = responses, close_error
        self.calls, self.sessions = [], []

    def session(self, **settings):
        network = self

        class Session:
            def __init__(self):
                self.cookie_jar, self.closed = settings["cookie_jar"], False

            async def close(self):
                self.closed = True
                if network.close_error:
                    raise RuntimeError("fixture-private-cleanup")

            @asynccontextmanager
            async def request(self, method, url, **kwargs):
                network.calls.append((method, str(url), kwargs))
                assert kwargs["allow_redirects"] is False
                assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
                assert kwargs["ssl"].check_hostname is True
                assert kwargs["proxy"] == PROXY
                headers = {**settings["headers"], **kwargs.get("headers", {})}
                cookies = self.cookie_jar.filter_cookies(url)
                if cookies:
                    headers["Cookie"] = cookies.output(header="", sep=";").strip()
                for trace in settings["trace_configs"]:
                    for handler in trace.on_request_headers_sent:
                        await handler(self, SimpleNamespace(trace_request_ctx=kwargs["trace_request_ctx"]),
                                      SimpleNamespace(headers=headers))
                response = network.responses[len(network.calls) - 1]
                if isinstance(response, Exception):
                    raise response
                yield response if isinstance(response, Response) else Response(response)

        session = Session()
        self.sessions.append(session)
        return session


def scenario(monkeypatch, tmp_path, *, replacement=None, close_error=False):
    async def run():
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["auth-token"] = TOKEN
        cookie["auth-token"]["domain"] = ".twitch.tv"
        cookie["auth-token"]["path"] = "/"
        jar.update_cookies(cookie, catalog.watch_probe.WEB)
        cookie_file = tmp_path / "cookies.jar"
        jar.save(cookie_file)
        original = cookie_file.read_bytes()
        responses = deepcopy([VALIDATE, STREAM, AVAILABLE, INVENTORY])
        if replacement is not None:
            index, value = replacement
            responses[index] = value
        network = Network(responses, close_error)
        monkeypatch.setattr(catalog.watch_probe.aiohttp, "ClientSession", network.session)
        code, report = await catalog.check(cookie_file, "hJune", PROXY)
        assert cookie_file.read_bytes() == original
        assert network.sessions and all(session.closed for session in network.sessions)
        text = json.dumps(report)
        for secret in (TOKEN, PROXY, "fixture-private-proxy", "fixture-private-error",
                       "fixture-private-cleanup", "fixture-private-instance"):
            assert secret not in text
        return code, report, network
    return asyncio.run(run())


def test_success_uses_only_four_exact_requests_with_verified_tls(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path)
    assert code == 0 and report["state"] == "passed"
    assert report["coverage"] == "channels" and report["all_campaigns_verified"] is False
    assert report["new_vs_inventory"]["new_ids"] == ["campaign"]
    assert report["inventory"]["same_account"] is True
    assert [(method, url) for method, url, _ in network.calls] == [
        ("GET", str(catalog.watch_probe.VALIDATE)),
        *[("POST", str(catalog.watch_probe.GQL))] * 3]
    assert [call[2]["json"]["operationName"] for call in network.calls[1:]] == [
        GQL_QUERIES[key]["operationName"] for key in ("GetStreamInfo", "AvailableDrops", "Inventory")]
    assert len({id(call[2]["ssl"]) for call in network.calls}) == 1
    assert all(request["oauth_matches"] and request["auth_cookie_matches"] for request in report["requests"])
    assert all(request["web_client_matches"] for request in report["requests"][1:])


@pytest.mark.parametrize("value,state", [(None, "null"), ({}, "error"), (False, "error")])
def test_unknown_catalog_stops_before_inventory(monkeypatch, tmp_path, value, state):
    body = deepcopy(AVAILABLE)
    body["data"]["channel"]["viewerDropCampaigns"] = value
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(2, body))
    assert code == 1 and report["campaigns_state"] == state
    assert report["campaign_count"] is report["unique_campaign_count"] is None
    assert report["candidates"] is None and len(network.calls) == 3
    assert report["new_vs_inventory"]["comparable"] is False
    assert report["new_vs_inventory"]["new_count"] is None


def test_missing_catalog_is_unknown_and_real_empty_is_valid(monkeypatch, tmp_path):
    missing = {"data": {"channel": {"id": "123"}}}
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(2, missing))
    assert code == 1 and report["campaigns_state"] == "missing" and len(network.calls) == 3
    empty = {"data": {"channel": {"id": "123", "viewerDropCampaigns": []}}}
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(2, empty))
    assert code == 0 and report["campaign_count"] == 0 and len(network.calls) == 4
    assert report["new_vs_inventory"]["new_ids"] == []


@pytest.mark.parametrize("value", [None, {}, [None], [{}], [{"id": ""}],
                                   [{"id": "drop"}, {"id": "drop"}]])
def test_bad_drop_structure_preserved_and_stops_inventory(monkeypatch, tmp_path, value):
    body = deepcopy(AVAILABLE)
    body["data"]["channel"]["viewerDropCampaigns"][0]["timeBasedDrops"] = value
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(2, body))
    assert code == 1 and report["error"] == "catalog_entries_invalid"
    assert report["unique_campaign_count"] is None
    assert report["observed_unique_campaign_count"] == 1 and len(network.calls) == 3
    candidate = report["candidates"][0]
    if value is None or isinstance(value, dict):
        assert candidate["drops"] is None and candidate["drop_count"] is None
    else:
        assert candidate["invalid_drop_entries"] > 0


@pytest.mark.parametrize("value", [None, {}, [None], [{}], [{"id": ""}],
                                   [{"id": "campaign"}, {"id": "campaign"}]])
def test_bad_inventory_never_produces_new_ids(monkeypatch, tmp_path, value):
    body = deepcopy(INVENTORY)
    body["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"] = value
    code, report, _ = scenario(monkeypatch, tmp_path, replacement=(3, body))
    assert code == 1 and report["state"] == "failed"
    assert report["inventory"]["in_progress_count"] is None
    assert report["new_vs_inventory"]["new_ids"] is None
    assert report["new_vs_inventory"]["comparable"] is False


@pytest.mark.parametrize("user", [None, {}, {"inventory": {"dropCampaignsInProgress": []}},
                                   {"id": "99", "inventory": {"dropCampaignsInProgress": []}}])
def test_inventory_identity_must_be_present_and_match(monkeypatch, tmp_path, user):
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(3, {"data": {"currentUser": user}}))
    assert code == 1 and len(network.calls) == 4
    assert report["new_vs_inventory"]["comparable"] is False


@pytest.mark.parametrize("body", [{}, {"client_id": "wrong", "user_id": "42"},
                                  {"client_id": ClientType.WEB.CLIENT_ID}])
def test_invalid_identity_stops_before_gql(monkeypatch, tmp_path, body):
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(0, body))
    assert code == 1 and len(network.calls) == 1 and report["web_token_valid"] is False


@pytest.mark.parametrize("kind", ["missing", "invalid", "empty"])
def test_unknown_stream_is_not_recorded_as_offline(monkeypatch, tmp_path, kind):
    body = deepcopy(STREAM)
    if kind == "missing":
        del body["data"]["user"]["stream"]
    else:
        body["data"]["user"]["stream"] = [] if kind == "invalid" else {}
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(1, body))
    assert code == 1 and len(network.calls) == 2 and "channel" not in report


@pytest.mark.parametrize("response", [Response({}, 429), Response({}, 302),
    {"extensions": {"challenge": {"type": "integrity", "token": TOKEN}}},
    {"errors": [{"message": TOKEN}]}, aiohttp.ClientConnectionError("fixture-private-error")])
def test_transport_and_challenge_stop_without_followup(monkeypatch, tmp_path, response):
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(2, response))
    assert code == 1 and len(network.calls) == 3 and report["inventory"] is None
    assert report["new_vs_inventory"]["comparable"] is False


def test_wrong_channel_stops_before_inventory(monkeypatch, tmp_path):
    body = deepcopy(AVAILABLE)
    body["data"]["channel"]["id"] = "999"
    code, report, network = scenario(monkeypatch, tmp_path, replacement=(2, body))
    assert code == 1 and report["error"] == "available_channel_mismatch" and len(network.calls) == 3


def test_close_failure_cannot_return_passed(monkeypatch, tmp_path):
    code, report, _ = scenario(monkeypatch, tmp_path, close_error=True)
    assert code == 1 and report["state"] == "failed" and report["cleanup_error"] == "RuntimeError"


def test_public_candidate_fields_do_not_copy_sensitive_state(monkeypatch, tmp_path):
    body = deepcopy(AVAILABLE)
    item = body["data"]["channel"]["viewerDropCampaigns"][0]
    item.update({"self": {"dropInstanceID": "fixture-private-instance"}, "headers": {"OAuth": TOKEN},
                 "endAt": TOKEN, "auth-token": TOKEN})
    code, report, _ = scenario(monkeypatch, tmp_path, replacement=(2, body))
    assert code == 0 and report["candidates"][0]["account_link_state"] == "unknown"
    assert report["candidates"][0]["ends_at"] == "unknown" and "allow" not in report["candidates"][0]


def test_endpoint_and_spoofed_mutations_rejected_before_transport(monkeypatch):
    async def run():
        client = catalog.CatalogClient({"requests": []}, None)
        get_session = AsyncMock()
        monkeypatch.setattr(client, "get_session", get_session)
        for method, endpoint in (("POST", "https://spade.twitch.tv/track"),
                ("POST", str(catalog.watch_probe.VALIDATE)), ("GET", str(catalog.watch_probe.GQL)),
                ("GET", str(catalog.watch_probe.VALIDATE) + "?token=x")):
            with pytest.raises(WatchCheckError):
                async with client.request(method, endpoint):
                    pytest.fail("unexpected transport")
        for change in ({"query": "mutation Inventory { claim }"}, {"extensions": {}},
                       {"variables": {"fetchRewardCampaigns": True}}):
            operation = deepcopy(GQL_QUERIES["Inventory"])
            operation.update(change)
            with pytest.raises(WatchCheckError, match="operation_not_allowed"):
                async with client.request("POST", catalog.watch_probe.GQL, json=operation):
                    pytest.fail("unexpected transport")
        get_session.assert_not_awaited()
    asyncio.run(run())


def test_budget_enforced_before_fifth_request(monkeypatch):
    async def run():
        client = catalog.CatalogClient({"requests": [{}, {}, {}, {}]}, None)
        get_session = AsyncMock()
        monkeypatch.setattr(client, "get_session", get_session)
        with pytest.raises(WatchCheckError, match="request_budget_exhausted"):
            async with client.request("GET", catalog.watch_probe.VALIDATE):
                pytest.fail("unexpected transport")
        get_session.assert_not_awaited()
    asyncio.run(run())


def test_self_check_passes():
    assert catalog.self_check()["case_count"] == 12


def test_allowlist_and_request_budget():
    assert catalog.CATALOG_READS == {GQL_QUERIES[key]["operationName"] for key in (
        "GetStreamInfo", "AvailableDrops", "Inventory")}
    assert catalog.CatalogClient.REQUEST_BUDGET == 4
