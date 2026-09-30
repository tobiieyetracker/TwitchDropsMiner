import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from channel import Channel
from constants import GQL_QUERIES
from gql_recovery import CampaignAccessError
from twitch import Twitch


AVAILABLE = GQL_QUERIES["AvailableDrops"].with_variables({"channelID": "123"})


def available(campaigns):
    return {"data": {"channel": {"id": "123", "viewerDropCampaigns": campaigns}}}


async def passthrough(coro):
    return await coro


def client_with_transport(response):
    client = Twitch.__new__(Twitch)
    client.gui = SimpleNamespace(channels=Mock(), coro_unless_closed=passthrough)
    client.settings = SimpleNamespace(available_drops_check=True)
    client._campaigns = {}
    client._web_session = SimpleNamespace(refresh_integrity=AsyncMock())
    client._gql_request_once = AsyncMock(return_value=(response, None))
    return client


@pytest.mark.parametrize("campaigns", [[], [{"id": "known-campaign"}]])
def test_available_list_is_preserved_without_refresh(campaigns):
    response = available(campaigns)
    client = client_with_transport(response)
    assert asyncio.run(client.gql_request(AVAILABLE)) is response
    client._gql_request_once.assert_awaited_once()
    client._web_session.refresh_integrity.assert_not_awaited()


@pytest.mark.parametrize("response,message", [
    (available(None), "not an empty campaign list"),
    ({"data": {"channel": None}}, "no channel"),
    ({"data": {}}, "schema"),
    ({"data": []}, "schema"),
    ({"data": {"channel": {"id": "123"}}}, "schema"),
    ({"data": {"channel": []}}, "schema"),
    (available({}), "schema"),
    (available(False), "schema"),
])
def test_unknown_availability_is_not_inferred_to_be_integrity(response, message):
    client = client_with_transport(response)
    with pytest.raises(CampaignAccessError, match=message):
        asyncio.run(client.gql_request(AVAILABLE))
    client._gql_request_once.assert_awaited_once()
    client._web_session.refresh_integrity.assert_not_awaited()


def online_channel(response):
    client = client_with_transport(response)
    stream_info = {"data": {"user": {
        "id": "123", "displayName": "Example",
        "stream": {"id": "456", "viewersCount": 10},
        "broadcastSettings": {"game": {"id": "1", "name": "Example game"}, "title": "Live"},
    }}}

    async def send(operations):
        ops = operations if isinstance(operations, list) else [operations]
        replies = [
            response if op["operationName"] == AVAILABLE["operationName"] else stream_info
            for op in ops
        ]
        return (replies if isinstance(operations, list) else replies[0]), None

    client._gql_request_once = AsyncMock(side_effect=send)
    return client, Channel(client, id=123, login="example"), stream_info


async def check_channel(client, channel, bulk):
    if bulk:
        await client.bulk_check_online([channel])
        return channel._stream
    return await channel.get_stream()


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("response", [available(None), {"data": {"channel": None}}])
def test_channel_checks_do_not_convert_unknown_to_no_drops(response, bulk):
    client, channel, _ = online_channel(response)
    with patch.object(Channel, "_check_drops_enabled") as check:
        with pytest.raises(CampaignAccessError):
            asyncio.run(check_channel(client, channel, bulk))
        check.assert_not_called()
    assert channel._stream is None
    assert client._gql_request_once.await_count == 2
    client._web_session.refresh_integrity.assert_not_awaited()


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "batch"])
def test_channel_checks_propagate_explicit_integrity_failure(bulk):
    response = {"extensions": {"challenge": {"type": "integrity"}}}
    client, channel, _ = online_channel(response)
    client._web_session = None
    with pytest.raises(CampaignAccessError, match="integrity"):
        asyncio.run(check_channel(client, channel, bulk))
    assert channel._stream is None


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("campaigns", [[], [{"id": "known-campaign"}]])
def test_channel_checks_accept_real_lists(bulk, campaigns):
    client, channel, _ = online_channel(available(campaigns))
    client._campaigns["known-campaign"] = SimpleNamespace(can_earn=Mock(return_value=True))
    stream = asyncio.run(check_channel(client, channel, bulk))
    assert stream is not None and stream.drops_enabled is bool(campaigns)
    assert client._gql_request_once.await_count == 2


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("no_user", [False, True], ids=["offline", "unavailable-user"])
def test_no_available_drops_query_when_stream_is_unavailable(bulk, no_user):
    client, channel, stream_info = online_channel(available(None))
    if no_user:
        stream_info["data"]["user"] = None
    else:
        stream_info["data"]["user"]["stream"] = None
    assert asyncio.run(check_channel(client, channel, bulk)) is None
    client._gql_request_once.assert_awaited_once()


def test_bulk_missing_availability_is_not_defaulted_to_empty():
    response = available([])
    response["data"]["channel"]["id"] = "999"
    client, channel, _ = online_channel(response)
    with pytest.raises(CampaignAccessError, match="omitted drop availability"):
        asyncio.run(client.bulk_check_online([channel]))
    assert channel._stream is None


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "batch"])
def test_disabled_availability_check_keeps_existing_stream_behavior(bulk):
    client, channel, _ = online_channel(available(None))
    client.settings.available_drops_check = False
    stream = asyncio.run(check_channel(client, channel, bulk))
    assert stream is not None and stream.drops_enabled
    client._gql_request_once.assert_awaited_once()
