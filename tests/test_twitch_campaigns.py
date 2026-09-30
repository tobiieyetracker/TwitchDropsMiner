import asyncio
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import twitch as twitch_module

from constants import ClientInfo, ClientType, GQL_QUERIES
from gql_recovery import CampaignAccessError
from twitch import Twitch, _AuthState
from utils import RateLimiter
from web_session import WebCredentials
from campaign_discovery import normalize_available_campaign


async def passthrough(coro):
    return await coro


def bare_client():
    client = Twitch.__new__(Twitch)
    client.settings = SimpleNamespace(check_campaigns=False)
    client._client_type = ClientType.ANDROID_APP
    client.gui = SimpleNamespace(
        coro_unless_closed=passthrough, status=SimpleNamespace(update=Mock()),
        inv=SimpleNamespace(clear=Mock(), add_campaign=AsyncMock()), close_requested=False,
        login=SimpleNamespace(update=Mock()),
        help=SimpleNamespace(_invalidate_button=SimpleNamespace(config=Mock())),
        start=Mock(), print=Mock(),
    )
    client._web_session = SimpleNamespace(refresh_integrity=AsyncMock())
    return client


def channel_campaign_candidate():
    raw = {
        "id": "channel-campaign", "name": "Channel candidate",
        "startAt": "2020-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z",
        "game": {"id": "263490", "name": "Rust"},
        "timeBasedDrops": [{
            "id": "channel-drop", "name": "Channel drop",
            "requiredMinutesWatched": 60,
            "benefitEdges": [{"benefit": {
                "id": "reward", "name": "Reward", "imageAssetURL": "",
            }}],
        }],
    }
    return normalize_available_campaign(
        raw, channel_id="123", channel_login="rainbow6",
        game={"id": "263490", "name": "Rust"}, observed_at="2026-10-01T00:00:00Z",
    )


def test_explicit_campaign_channels_are_normalized_and_deduplicated():
    client = Twitch.__new__(Twitch)
    client.settings = SimpleNamespace(campaign_channel=[
        " Rainbow6 ", "@rainbow6", "", "@Ubisoft", None,
    ])

    assert client._campaign_discovery_channels() == ["rainbow6", "ubisoft"]


@pytest.mark.parametrize("smartbox", [False, True], ids=["dashboard-fallback", "smartbox"])
def test_campaign_discovery_falls_back_to_configured_channel_scan(smartbox, capsys):
    client = bare_client()
    client.settings = SimpleNamespace(
        dump=False, enable_badges_emotes=False, check_campaigns=True,
        priority=["Rust"], campaign_game=[],
    )
    client._drops, client._campaigns, client.inventory = {}, {}, []
    client._mnt_triggers, client._mnt_task = deque(), None
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    client._client_type = ClientType.SMARTBOX if smartbox else ClientType.ANDROID_APP
    client._discover_campaigns_from_channels = AsyncMock(
        return_value=({"channel-campaign": channel_campaign_candidate()}, 1, 0)
    )
    operations = []

    async def send(operation):
        operations.append(operation["operationName"])
        if operation["operationName"] == "Inventory":
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [], "gameEventDrops": [],
            }}}}
        raise CampaignAccessError("dashboard denied")

    client.gql_request = AsyncMock(side_effect=send)
    asyncio.run(client.fetch_inventory())

    if smartbox:
        client._discover_campaigns_from_channels.assert_awaited_once_with(
            ["Rust"], [], availability_client=None
        )
    else:
        client._discover_campaigns_from_channels.assert_awaited_once_with(["Rust"], [])
    assert [campaign.id for campaign in client.inventory] == ["channel-campaign"]
    assert client.inventory[0].linked is None
    assert client.inventory[0].discovery_channel_logins == ["rainbow6"]
    assert "partial" in capsys.readouterr().out
    if smartbox:
        assert operations == ["Inventory"]
    else:
        assert operations == ["Inventory", "ViewerDropsDashboard"]


def test_smartbox_uses_verified_web_cookie_only_for_available_drops(monkeypatch):
    client = bare_client()
    client.settings = SimpleNamespace(
        dump=False, enable_badges_emotes=False, check_campaigns=True,
        priority=["Rust"], campaign_game=[], campaign_channel=["rainbow6"],
        campaign_web_cookie_file="cookies.jar", proxy=None,
    )
    client._drops, client._campaigns, client.inventory = {}, {}, []
    client._mnt_triggers, client._mnt_task = deque(), None
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    client._auth_state = SimpleNamespace(user_id=42)
    client._client_type = ClientType.SMARTBOX
    client._qgl_limiter = RateLimiter(capacity=5, window=1)
    client.print = Mock()
    client.gql_request = AsyncMock(return_value={"data": {"currentUser": {"inventory": {
        "dropCampaignsInProgress": [], "gameEventDrops": [],
    }}}})
    source_constructed = {}

    class FakeWebCampaignSource:
        def __init__(self, cookie_file, *, proxy, gql_limiter):
            source_constructed["instance"] = self
            source_constructed.update(
                cookie_file=cookie_file, proxy=proxy, gql_limiter=gql_limiter
            )

        async def open(self, *, expected_user_id):
            source_constructed["expected_user_id"] = expected_user_id

        async def close(self):
            source_constructed["closed"] = True

    monkeypatch.setattr(twitch_module, "WebCampaignSource", FakeWebCampaignSource)
    client._discover_campaigns_from_channels = AsyncMock(
        return_value=({"channel-campaign": channel_campaign_candidate()}, 1, 0)
    )
    client.gui.print = Mock()

    asyncio.run(client.fetch_inventory())

    assert source_constructed["cookie_file"] == "cookies.jar"
    assert source_constructed["expected_user_id"] == 42
    assert source_constructed["closed"] is True
    client._discover_campaigns_from_channels.assert_awaited_once_with(
        ["Rust"], ["rainbow6"], availability_client=source_constructed["instance"]
    )
    assert [call.args[0]["operationName"] for call in client.gql_request.await_args_list] == [
        "Inventory",
    ]
    assert any("same-account WEB cookie" in str(call.args) for call in client.print.call_args_list)


def test_dashboard_failure_without_configured_games_remains_an_error():
    client = bare_client()
    client.settings = SimpleNamespace(
        dump=False, enable_badges_emotes=False, check_campaigns=False, priority=[],
    )
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))

    async def send(operation):
        if operation["operationName"] == "Inventory":
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [], "gameEventDrops": [],
            }}}}
        raise CampaignAccessError("dashboard denied")

    client.gql_request = AsyncMock(side_effect=send)
    client._discover_campaigns_from_channels = AsyncMock()
    with pytest.raises(CampaignAccessError, match="dashboard denied"):
        asyncio.run(client.fetch_inventory())
    client._discover_campaigns_from_channels.assert_not_awaited()


@pytest.mark.parametrize("check_campaigns", [False, True])
def test_new_campaign_reaches_inventory_after_challenged_dashboard(check_campaigns, capsys):
    client = bare_client()
    client.settings = SimpleNamespace(
        dump=False, enable_badges_emotes=False, check_campaigns=check_campaigns,
    )
    client._drops, client._campaigns, client.inventory = {}, {}, []
    client._mnt_triggers, client._mnt_task = deque(), None
    client._maintenance_task = AsyncMock()
    client.get_auth = AsyncMock(return_value=SimpleNamespace(user_id=42))
    campaign = {
        "id": "new-campaign", "name": "Previously undiscovered", "status": "ACTIVE",
        "game": {"id": "1", "name": "Test game", "boxArtURL": "https://example.com/game.jpg"},
        "self": {"isAccountConnected": True}, "accountLinkURL": "https://example.com/link",
        "startAt": "2020-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z",
        "allow": {"isEnabled": False, "channels": []},
        "timeBasedDrops": [{
            "id": "new-drop", "name": "New drop", "preconditionDrops": [],
            "startAt": "2020-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z",
            "requiredMinutesWatched": 60, "benefitEdges": [],
        }],
    }
    calls = []

    async def send(ops):
        calls.append(ops)
        if not isinstance(ops, list) and ops["operationName"] == "Inventory":
            return {"data": {"currentUser": {"inventory": {
                "dropCampaignsInProgress": [], "gameEventDrops": [],
            }}}}, "old-integrity"
        if not isinstance(ops, list):
            return {"extensions": {"challenge": {"type": "integrity"}}}, "old-integrity"
        if ops[0]["operationName"] == "ViewerDropsDashboard":
            return [{"data": {"currentUser": {"dropCampaigns": [
                {"id": "new-campaign", "status": "ACTIVE"},
            ]}}}], "new-integrity"
        assert ops[0]["variables"] == {"channelLogin": "42", "dropID": "new-campaign"}
        return [{"data": {"user": {"dropCampaign": campaign}}}], "new-integrity"

    client._gql_request_once = send

    async def scenario():
        await client.fetch_inventory()
        if client._mnt_task is not None:
            await client._mnt_task
    asyncio.run(scenario())
    assert [c.id for c in client.inventory] == ["new-campaign"]
    assert client._drops["new-drop"].current_minutes == 0
    assert client._campaigns["new-campaign"].linked is True
    client.gui.inv.add_campaign.assert_awaited_once()
    client._web_session.refresh_integrity.assert_awaited_once_with(rejected_token="old-integrity")
    assert len(calls) == 4
    if check_campaigns:
        assert client._mnt_task is None
        client._maintenance_task.assert_not_awaited()
        assert "0 in progress, 1 on dashboard, 1 newly discovered" in capsys.readouterr().out
    else:
        client._maintenance_task.assert_awaited_once()


def test_original_transport_reports_silent_denial_without_typeerror():
    client = bare_client()
    client._web_session = None
    client._gql_request_once = AsyncMock(return_value=({
        "data": {"currentUser": {"dropCampaigns": None}},
    }, None))
    with pytest.raises(CampaignAccessError, match="--browser-auth"):
        asyncio.run(client.gql_request(GQL_QUERIES["Campaigns"]))


def test_web_auth_uses_one_consistent_client_and_never_enters_device_login():
    client = bare_client()
    credentials = WebCredentials(42, {
        "Authorization": "OAuth web-test", "Client-Id": ClientType.WEB.CLIENT_ID,
        "User-Agent": "actual-browser", "X-Device-Id": "actual-device",
        "Client-Session-Id": "actual-session",
    })
    client._web_session.start = AsyncMock(return_value=credentials)
    auth = _AuthState(client)
    auth._oauth_login = AsyncMock()
    asyncio.run(auth.validate())
    assert auth.access_token == "web-test" and auth.user_id == 42
    headers = auth.headers(user_agent=client._client_type.USER_AGENT, gql=True)
    assert all(headers[name] == value for name, value in credentials.headers.items())
    auth._oauth_login.assert_not_awaited()


def test_check_mode_cannot_start_watching_or_claiming():
    client = bare_client()
    client.settings = SimpleNamespace(check_campaigns=True)
    client.get_auth = AsyncMock()
    client.fetch_inventory = AsyncMock()
    client.inventory = [object(), object()]
    client._in_progress_campaign_ids = {"in-progress"}
    client.inventory = [
        SimpleNamespace(
            id="in-progress", name="Existing", game=SimpleNamespace(name="Game"),
            linked=True, drops=[], discovery_channel_logins=[],
        ),
        SimpleNamespace(
            id="new-campaign", name="New", game=SimpleNamespace(name="Game"),
            linked=None,
            drops=[SimpleNamespace(name="Drop", id="drop-id", required_minutes=0)],
            discovery_channel_logins=["rainbow6"],
        ),
    ]
    client.print = Mock()
    client.websocket = SimpleNamespace(start=AsyncMock())
    client._watch_loop = AsyncMock()
    asyncio.run(client._run())
    client.fetch_inventory.assert_awaited_once()
    client.websocket.start.assert_not_awaited()
    client._watch_loop.assert_not_awaited()
    output = "\n".join(str(call.args[0]) for call in client.print.call_args_list)
    assert "New campaigns beyond Inventory: 1." in output
    assert "New [new-campaign]" in output and "drop-id" in output


def test_transport_supplies_matching_browser_headers():
    client = bare_client()
    client._client_type = ClientInfo(ClientType.WEB.CLIENT_URL, ClientType.WEB.CLIENT_ID, "browser")
    auth = _AuthState(client)
    auth.access_token, auth.device_id, auth.session_id = "test-auth", "device", "session"
    client.get_auth = AsyncMock(return_value=auth)
    client._web_session.headers = AsyncMock(return_value={
        "Client-Integrity": "test-integrity", "Client-Version": "test-version",
    })
    captured = {}

    @asynccontextmanager
    async def limiter():
        yield

    @asynccontextmanager
    async def request(method, url, **kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(json=AsyncMock(return_value={"data": {"ok": True}}))

    client._qgl_limiter, client.request = limiter(), request
    result, token = asyncio.run(client._gql_request_once(GQL_QUERIES["Campaigns"]))
    assert token == "test-integrity"
    assert captured["headers"]["Authorization"] == "OAuth test-auth"
    assert captured["headers"]["X-Device-Id"] == "device"
    assert captured["headers"]["Client-Id"] == ClientType.WEB.CLIENT_ID
    assert result == {"data": {"ok": True}}
