import asyncio
from hashlib import sha256

import aiohttp
import pytest
from yarl import URL

import campaign_web_source as web_source_module
from campaign_web_source import WebCampaignSource, WebCampaignSourceError
from constants import ClientType, GQL_QUERIES
from gql_recovery import CampaignAccessError, CampaignAvailabilityUnknown
from utils import RateLimiter


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(*self.responses.pop(0))

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse(*self.responses.pop(0))

    async def close(self):
        self.closed = True


def web_cookie_file(tmp_path):
    path = tmp_path / "cookies.jar"

    async def save_cookie_jar():
        jar = aiohttp.CookieJar()
        jar.update_cookies(
            {"auth-token": "web-token-test-value", "unique_id": "device-test-value"},
            URL(ClientType.WEB.CLIENT_URL),
        )
        jar.save(path)

    asyncio.run(save_cookie_jar())
    return path


def source(path):
    return WebCampaignSource(path, proxy=None, gql_limiter=RateLimiter(capacity=5, window=1))


def test_web_source_uses_same_account_read_only_available_drops(tmp_path, monkeypatch):
    path = web_cookie_file(tmp_path)
    original_hash = sha256(path.read_bytes()).hexdigest()
    validation = FakeSession([(200, {
        "client_id": ClientType.WEB.CLIENT_ID, "user_id": "42",
    })])
    gql = FakeSession([(200, {"data": {"channel": {
        "id": "123", "viewerDropCampaigns": [],
    }}})])
    sessions = iter([validation, gql])
    session_args = []

    def make_session(**kwargs):
        session_args.append(kwargs)
        return next(sessions)

    monkeypatch.setattr(web_source_module.aiohttp, "ClientSession", make_session)
    client = source(path)
    operation = GQL_QUERIES["AvailableDrops"].with_variables({"channelID": "123"})

    async def scenario():
        await client.open(expected_user_id=42)
        response = await client.gql_request(operation)
        await client.close()
        return response

    response = asyncio.run(scenario())

    assert response["data"]["channel"]["viewerDropCampaigns"] == []
    assert len(validation.calls) == 1
    assert isinstance(session_args[0]["cookie_jar"], aiohttp.DummyCookieJar)
    assert len(gql.calls) == 1
    headers = gql.calls[0][2]["headers"]
    assert headers["Client-Id"] == ClientType.WEB.CLIENT_ID
    assert headers["Authorization"] == "OAuth web-token-test-value"
    assert headers["X-Device-Id"] == "device-test-value"
    assert "Client-Integrity" not in headers
    assert path.exists() and sha256(path.read_bytes()).hexdigest() == original_hash
    assert validation.closed and gql.closed


def test_web_source_refuses_a_different_account_before_gql(tmp_path, monkeypatch):
    path = web_cookie_file(tmp_path)
    validation = FakeSession([(200, {
        "client_id": ClientType.WEB.CLIENT_ID, "user_id": "43",
    })])
    created = []

    def make_session(**_kwargs):
        created.append(True)
        return validation

    monkeypatch.setattr(web_source_module.aiohttp, "ClientSession", make_session)
    client = source(path)

    with pytest.raises(WebCampaignSourceError) as error:
        asyncio.run(client.open(expected_user_id=42))

    assert error.value.code == "web_token_account_mismatch"
    assert client.user_id is None
    assert len(created) == 1
    assert validation.closed


def test_web_source_stops_on_integrity_challenge_without_replay(tmp_path, monkeypatch):
    path = web_cookie_file(tmp_path)
    validation = FakeSession([(200, {
        "client_id": ClientType.WEB.CLIENT_ID, "user_id": "42",
    })])
    gql = FakeSession([(200, {"data": {"channel": {
        "id": "123", "viewerDropCampaigns": None,
    }}, "extensions": {"challenge": {"type": "integrity"}}})])
    sessions = iter([validation, gql])
    monkeypatch.setattr(web_source_module.aiohttp, "ClientSession", lambda **_kwargs: next(sessions))
    client = source(path)
    operation = GQL_QUERIES["AvailableDrops"].with_variables({"channelID": "123"})

    async def scenario():
        await client.open(expected_user_id=42)
        with pytest.raises(CampaignAccessError, match="integrity session"):
            await client.gql_request(operation)
        await client.close()

    asyncio.run(scenario())
    assert len(gql.calls) == 1
    assert len(gql.responses) == 0


def test_web_source_preserves_null_as_unknown_not_empty(tmp_path, monkeypatch):
    path = web_cookie_file(tmp_path)
    validation = FakeSession([(200, {
        "client_id": ClientType.WEB.CLIENT_ID, "user_id": "42",
    })])
    gql = FakeSession([(200, {"data": {"channel": {
        "id": "123", "viewerDropCampaigns": None,
    }}})])
    sessions = iter([validation, gql])
    monkeypatch.setattr(web_source_module.aiohttp, "ClientSession", lambda **_kwargs: next(sessions))
    client = source(path)
    operation = GQL_QUERIES["AvailableDrops"].with_variables({"channelID": "123"})

    async def scenario():
        await client.open(expected_user_id=42)
        with pytest.raises(CampaignAvailabilityUnknown):
            await client.gql_request(operation)
        await client.close()

    asyncio.run(scenario())
    assert len(gql.calls) == 1


def test_web_source_rejects_non_available_drops_operations(tmp_path, monkeypatch):
    path = web_cookie_file(tmp_path)
    validation = FakeSession([(200, {
        "client_id": ClientType.WEB.CLIENT_ID, "user_id": "42",
    })])
    gql = FakeSession([])
    sessions = iter([validation, gql])
    monkeypatch.setattr(web_source_module.aiohttp, "ClientSession", lambda **_kwargs: next(sessions))
    client = source(path)

    async def scenario():
        await client.open(expected_user_id=42)
        with pytest.raises(CampaignAccessError, match="only permits"):
            await client.gql_request(GQL_QUERIES["Inventory"])
        await client.close()

    asyncio.run(scenario())
    assert gql.calls == []
