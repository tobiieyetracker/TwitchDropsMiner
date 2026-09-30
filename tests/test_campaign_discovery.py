from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio

from campaign_discovery import (
    merge_channel_campaign,
    merge_inventory_campaign,
    normalize_available_campaign,
)
from inventory import DropsCampaign
from twitch import Twitch


START = "2020-01-01T00:00:00Z"
END = "2099-01-01T00:00:00Z"
GAME = {"id": "263490", "name": "Rust", "displayName": "Rust"}


def campaign(drop_id, *, drop_name=None):
    return {
        "id": "campaign-1",
        "name": "Rust campaign",
        "startAt": START,
        "endAt": END,
        "game": {"id": "263490", "name": "Rust"},
        "timeBasedDrops": [{
            "id": drop_id,
            "name": drop_name or drop_id,
            "requiredMinutesWatched": 60,
            "benefitEdges": [{"benefit": {
                "id": f"benefit-{drop_id}",
                "name": f"Reward {drop_id}",
                "imageAssetURL": "https://example.invalid/reward.png",
            }}],
        }],
    }


def normalize(raw, channel_id, login):
    return normalize_available_campaign(
        raw,
        channel_id=channel_id,
        channel_login=login,
        game=GAME,
        observed_at="2026-10-01T00:00:00Z",
    )


def test_candidate_keeps_unknown_account_state_and_channel_provenance():
    item = normalize(campaign("drop-a"), 11, "streamer_a")

    assert item["status"] == "ACTIVE"
    assert "self" not in item
    assert item["_discovery"]["coverage"] == "partial_channel_scan"
    assert item["_discovery"]["sources"] == [{
        "channel_id": "11",
        "channel_login": "streamer_a",
        "observed_at": "2026-10-01T00:00:00Z",
    }]
    assert item["_discovery"]["drop_sources"]["drop-a"][0]["channel_login"] == "streamer_a"
    assert item["timeBasedDrops"][0]["benefitEdges"][0]["benefit"]["distributionType"] == "UNKNOWN"


def test_same_campaign_merges_drops_by_drop_id_and_preserves_conflicts():
    first = normalize(campaign("drop-a", drop_name="Reward A"), 11, "streamer_a")
    second = normalize(campaign("drop-b"), 22, "streamer_b")
    conflicting = normalize(campaign("drop-a", drop_name="Different A"), 33, "streamer_c")

    merged = merge_channel_campaign(first, second)
    merged = merge_channel_campaign(merged, conflicting)

    assert [drop["id"] for drop in merged["timeBasedDrops"]] == ["drop-a", "drop-b"]
    assert {source["channel_login"] for source in merged["_discovery"]["sources"]} == {
        "streamer_a", "streamer_b", "streamer_c",
    }
    assert {source["channel_login"] for source in merged["_discovery"]["drop_sources"]["drop-a"]} == {
        "streamer_a", "streamer_c",
    }
    assert merged["timeBasedDrops"][0]["name"] == "Reward A"
    assert any(
        conflict["drop_id"] == "drop-a" and conflict["field"] == "name"
        for conflict in merged["_discovery"]["conflicts"]
    )


def test_inventory_fields_override_candidate_without_losing_candidate_drops():
    candidate = merge_channel_campaign(
        normalize(campaign("drop-a"), 11, "streamer_a"),
        normalize(campaign("drop-b"), 22, "streamer_b"),
    )
    inventory = {
        "id": "campaign-1",
        "name": "Rust campaign",
        "startAt": START,
        "endAt": END,
        "status": "ACTIVE",
        "game": {"id": "263490", "name": "Rust", "boxArtURL": "https://example.invalid/rust.jpg"},
        "self": {"isAccountConnected": True},
        "accountLinkURL": "https://example.invalid/link",
        "allow": {"isEnabled": True, "channels": [{"id": "11", "name": "streamer_a"}]},
        "timeBasedDrops": [{
            "id": "drop-a", "name": "drop-a", "requiredMinutesWatched": 60,
            "benefitEdges": [], "self": {"currentMinutesWatched": 7},
        }],
    }

    merged = merge_inventory_campaign(inventory, candidate)

    drops = {drop["id"]: drop for drop in merged["timeBasedDrops"]}
    assert merged["self"]["isAccountConnected"] is True
    assert merged["allow"]["channels"][0]["name"] == "streamer_a"
    assert drops["drop-a"]["self"]["currentMinutesWatched"] == 7
    assert "drop-b" in drops
    assert len(merged["_discovery"]["sources"]) == 2


def test_null_inventory_fields_do_not_erase_valid_channel_data():
    candidate = normalize(campaign("drop-a"), 11, "streamer_a")
    inventory = {
        "id": "campaign-1",
        "game": None,
        "self": None,
        "allow": None,
        "accountLinkURL": None,
        "timeBasedDrops": [
            {"id": "drop-a", "self": None},
            {
                "id": "inventory-only-drop", "name": "Inventory drop",
                "requiredMinutesWatched": 30, "benefitEdges": [],
                "preconditionDrops": [], "self": None,
            },
        ],
    }

    merged = merge_inventory_campaign(inventory, candidate)
    model = DropsCampaign(
        SimpleNamespace(settings=SimpleNamespace(enable_badges_emotes=False)), merged, {}
    )

    assert merged["game"]["id"] == "263490"
    assert merged["allow"] == {"isEnabled": False, "channels": []}
    assert model.linked is None
    assert model.get_drop("drop-a") is not None
    assert model.get_drop("inventory-only-drop") is not None
    assert "self" not in next(
        drop for drop in merged["timeBasedDrops"] if drop["id"] == "inventory-only-drop"
    )


def test_channel_first_reader_queries_configured_game_and_keeps_per_drop_sources():
    channel_a = SimpleNamespace(id=11, _login="streamer_a", game=SimpleNamespace(id=263490, name="Rust"))
    channel_b = SimpleNamespace(id=22, _login="streamer_b", game=SimpleNamespace(id=263490, name="Rust"))
    client = Twitch.__new__(Twitch)
    client._get_live_streams_by_slug = AsyncMock(return_value=[channel_a, channel_b])
    client.gql_request = AsyncMock(side_effect=[
        {"data": {"channel": {"id": "11", "viewerDropCampaigns": [campaign("drop-a")]}}},
        {"data": {"channel": {"id": "22", "viewerDropCampaigns": [campaign("drop-b")]}}},
    ])

    candidates, attempted, unavailable = asyncio.run(
        client._discover_campaigns_from_channels(["Rust"])
    )

    client._get_live_streams_by_slug.assert_awaited_once_with(
        "rust", limit=10, drops_enabled=True
    )
    assert attempted == 2
    assert unavailable == 0
    assert list(candidates) == ["campaign-1"]
    merged = candidates["campaign-1"]
    assert {drop["id"] for drop in merged["timeBasedDrops"]} == {"drop-a", "drop-b"}

    fake_twitch = SimpleNamespace(settings=SimpleNamespace(enable_badges_emotes=False))
    model = DropsCampaign(fake_twitch, merged, {})
    ch_a = SimpleNamespace(id=11, game=model.game)
    ch_b = SimpleNamespace(id=22, game=model.game)
    assert model.linked is None
    assert model.eligible is True  # candidate may be attempted, but is not labeled linked
    assert model.get_drop("drop-a").can_earn(ch_a)
    assert not model.get_drop("drop-a").can_earn(ch_b)
    assert model.get_drop("drop-b").can_earn(ch_b)
    assert not model.get_drop("drop-b").can_earn(ch_a)
    assert model.first_drop_for(ch_a).id == "drop-a"
    assert model.first_drop_for(ch_b).id == "drop-b"


def test_exact_channel_scan_resolves_rainbow6_even_when_not_in_directory():
    client = Twitch.__new__(Twitch)
    client.gui = SimpleNamespace(channels=SimpleNamespace())
    client.gql_request = AsyncMock(side_effect=[
        [{"data": {"user": {
            "id": "9001", "login": "rainbow6", "displayName": "Rainbow Six",
            "stream": None,
        }}}],
        {"data": {"channel": {
            "id": "9001", "viewerDropCampaigns": [campaign("rainbow6-drop")],
        }}},
    ])

    candidates, attempted, unavailable = asyncio.run(
        client._discover_campaigns_from_channels([], ["rainbow6"])
    )

    assert attempted == 1
    assert unavailable == 0
    assert candidates["campaign-1"]["_discovery"]["sources"][0]["channel_login"] == "rainbow6"
    operations = [
        call.args[0][0] if isinstance(call.args[0], list) else call.args[0]
        for call in client.gql_request.call_args_list
    ]
    assert [operation["operationName"] for operation in operations] == [
        "VideoPlayerStreamInfoOverlayChannel",
        "DropsHighlightService_AvailableDrops",
    ]


def test_game_directory_null_game_returns_no_channels_without_aborting_scan():
    client = Twitch.__new__(Twitch)
    client.gql_request = AsyncMock(return_value={"data": {"game": None}})

    streams = asyncio.run(
        client._get_live_streams_by_slug("missing-game-slug", limit=10, drops_enabled=True)
    )

    assert streams == []


def test_null_channel_availability_is_unknown_and_does_not_abort_other_candidates():
    from gql_recovery import CampaignAvailabilityUnknown, CampaignAccessError

    channels = [
        SimpleNamespace(id=11, _login="streamer_a", game=SimpleNamespace(id=263490, name="Rust")),
        SimpleNamespace(id=22, _login="streamer_b", game=SimpleNamespace(id=263490, name="Rust")),
    ]
    client = Twitch.__new__(Twitch)
    client.gui = SimpleNamespace(channels=SimpleNamespace())
    client._get_live_streams_by_slug = AsyncMock(return_value=channels)
    client.gql_request = AsyncMock(side_effect=[
        CampaignAvailabilityUnknown(
            "Twitch returned null for AvailableDrops.viewerDropCampaigns. "
            "Channel drop availability is unknown; this is not an empty campaign list."
        ),
        {"data": {"channel": {
            "id": "22", "viewerDropCampaigns": [campaign("drop-b")],
        }}},
    ])

    candidates, attempted, unavailable = asyncio.run(
        client._discover_campaigns_from_channels(["Rust"])
    )

    assert list(candidates) == ["campaign-1"]
    assert candidates["campaign-1"]["_discovery"]["sources"][0]["channel_login"] == "streamer_b"
    assert attempted == 2
    assert unavailable == 1


def test_integrity_challenge_stops_channel_scan():
    from gql_recovery import CampaignAccessError
    import pytest

    channel = SimpleNamespace(
        id=11, _login="streamer_a", game=SimpleNamespace(id=263490, name="Rust"),
    )
    client = Twitch.__new__(Twitch)
    client.gui = SimpleNamespace(channels=SimpleNamespace())
    client._get_live_streams_by_slug = AsyncMock(return_value=[channel])
    client.gql_request = AsyncMock(side_effect=CampaignAccessError(
        "Twitch requires a web integrity session for campaign discovery."
    ))

    with pytest.raises(CampaignAccessError, match="integrity session"):
        asyncio.run(client._discover_campaigns_from_channels(["Rust"]))


def test_observed_channels_are_added_as_watch_candidates_without_claiming_an_acl():
    raw = normalize(campaign("drop-a"), 11, "streamer_a")
    twitch = Twitch.__new__(Twitch)
    twitch.gui = SimpleNamespace(channels=SimpleNamespace())
    model = DropsCampaign(
        SimpleNamespace(settings=SimpleNamespace(enable_badges_emotes=False)), raw, {}
    )

    channels = twitch._campaign_source_channels([model])

    assert len(channels) == 1
    channel = next(iter(channels))
    assert (channel.id, channel._login, channel.acl_based) == (11, "streamer_a", False)
    assert model.allowed_channels == []
