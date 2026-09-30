import copy
import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest

from native_claim_dom import WEBSITE_READ_OPERATIONS, benefits_for_target, locate_claim_button
from watch_check_state import WatchCheckError


TARGET = SimpleNamespace(campaign_id="campaign-1", drop_id="drop-1", name="Campaign")
IMAGE = "https://static-cdn.jtvnw.net/reward.png"


def test_inventory_bootstrap_reads_are_known_without_allowing_native_mutations():
    assert {
        "Inventory", "DropsInventoryRewardGroupStatus", "ViewerRewardDropInventory",
        "RewardCodeModal", "ViewerDropsDashboard", "CoreAuthCurrentUser",
        "CoreActionsCurrentUser", "TopNav_CurrentUser", "UserMenuCurrentUser",
        "PlaybackAccessToken_Template",
    } <= WEBSITE_READ_OPERATIONS
    assert WEBSITE_READ_OPERATIONS.isdisjoint({
        "DropsPage_ClaimDropRewards", "CoreUtilsSetLanguagePreference",
        "TOS_Banner_Update_Consent", "UpdateConsentMutation",
        "ChannelPage_SetSessionStatus", "BrowserPushNotifications_AddBrowserPushSubscription",
        "DiscoveryPreferenceMutation", "FollowButton_FollowUser", "FollowButton_UnfollowUser",
        "GlobalNotificationSettings_SetNotificationSetting",
        "GlobalNotificationSettings_setLiveNotificationsEnrollment",
        "LiveNotificationsToggle_ToggleNotifications", "PrimeSubscribe_SpendPrimeSubscriptionCredit",
        "PurchaseOrderContext_PurchaseOffer", "LinkPrimeAccount", "UnlinkAmazonConnection",
        "PreviewLiveShoppingOrders", "ConfirmLiveShoppingOrders", "placeOrder",
        "sendChatMessage", "incrementClipViewCount", "updateUserViewedVideo",
    })


def as_sync(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def edge(name="Reward", image=IMAGE, identity="benefit-1"):
    return {"benefit": {"id": identity, "name": name, "imageAssetURL": image}}


def body(extra_drops=None):
    return {"data": {"currentUser": {"id": "user", "inventory": {"dropCampaignsInProgress": [{
        "id": "campaign-1", "timeBasedDrops": [
            {"id": "drop-1", "benefitEdges": [edge()]}, *(extra_drops or []),
        ],
    }]}}}}


def campaign(value):
    return value["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]


def error_code(expected, callback):
    with pytest.raises(WatchCheckError) as error:
        callback()
    assert error.value.code == expected


def test_public_benefits_are_real_and_input_not_modified():
    value = body()
    original = copy.deepcopy(value)
    assert benefits_for_target(value, TARGET) == [{"name": "Reward", "image": IMAGE}]
    assert value == original


@pytest.mark.parametrize("other", [edge(image="https://cdn.test/other.png"), edge(name="Other")])
def test_other_drop_name_or_image_conflict_excludes_benefit(other):
    value = body([{"id": "drop-2", "benefitEdges": [other]}])
    error_code("native_target_benefits_not_unique", lambda: benefits_for_target(value, TARGET))


def test_multiple_target_benefits_choose_only_unambiguous_public_labels():
    value = body([{"id": "drop-2", "benefitEdges": [edge()]}])
    campaign(value)["timeBasedDrops"][0]["benefitEdges"].append(edge("Unique", "https://cdn.test/unique.png", "b2"))
    assert benefits_for_target(value, TARGET) == [{"name": "Unique", "image": "https://cdn.test/unique.png"}]


def test_same_target_duplicate_edges_do_not_duplicate_result():
    value = body()
    campaign(value)["timeBasedDrops"][0]["benefitEdges"].append(edge())
    assert len(benefits_for_target(value, TARGET)) == 1


@pytest.mark.parametrize("edges,expected", [(None, "native_target_benefits_not_unique"), ([], "native_target_benefits_not_unique"), ({}, "native_benefit_edges_invalid")])
def test_unknown_or_empty_edges_do_not_create_a_card(edges, expected):
    value = body()
    campaign(value)["timeBasedDrops"][0]["benefitEdges"] = edges
    error_code(expected, lambda: benefits_for_target(value, TARGET))


def test_nullable_other_drop_edges_have_no_rendered_conflict():
    assert benefits_for_target(body([{"id": "drop-2", "benefitEdges": None}]), TARGET)


def test_unknown_other_drop_fields_fail_closed():
    error_code("native_benefit_edges_missing", lambda: benefits_for_target(body([{"id": "drop-2"}]), TARGET))


@pytest.mark.parametrize("key,value,expected", [
    ("id", "", "native_benefit_id_invalid"),
    ("name", None, "native_benefit_name_null"),
    ("imageAssetURL", "javascript:alert(1)", "native_benefit_image_invalid"),
    ("imageAssetURL", "http://cdn.test/a", "native_benefit_image_invalid"),
    ("imageAssetURL", "https://[", "native_benefit_image_invalid"),
])
def test_benefit_fields_are_not_synthesized(key, value, expected):
    response = body()
    campaign(response)["timeBasedDrops"][0]["benefitEdges"][0]["benefit"][key] = value
    error_code(expected, lambda: benefits_for_target(response, TARGET))


def test_missing_target_or_duplicate_drop_rejected():
    response = body()
    campaign(response)["timeBasedDrops"][0]["id"] = "different"
    error_code("native_target_drop_missing", lambda: benefits_for_target(response, TARGET))
    response = body([{"id": "drop-1", "benefitEdges": [edge()]}])
    error_code("target_drop_duplicate", lambda: benefits_for_target(response, TARGET))


def test_authentication_and_challenge_errors_are_not_empty_benefits():
    error_code("user_null", lambda: benefits_for_target({"data": {"currentUser": None}}, TARGET))
    value = body()
    value["extensions"] = {"challenge": {"type": "integrity"}}
    error_code("gql_challenge", lambda: benefits_for_target(value, TARGET))


class Locator:
    """Small read-only locator model; deliberate DOM ambiguity remains observable."""
    def __init__(self, nodes):
        self.nodes = nodes

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return Locator([self.nodes[index]])

    @property
    def first(self):
        return Locator(self.nodes[:1])

    async def wait_for(self, *, state, timeout):
        assert state in {"attached", "visible"}
        assert 0 < timeout <= (10000 if state == "attached" else 5000)
        if not self.nodes or (state == "visible" and not self.nodes[0].get("visible", True)):
            raise RuntimeError("not ready")

    async def get_attribute(self, key):
        return self.nodes[0].get(key)

    async def inner_text(self):
        return self.nodes[0].get("text", "")

    async def is_visible(self):
        return self.nodes[0].get("visible", True)

    async def is_enabled(self):
        return self.nodes[0].get("enabled", True)

    def locator(self, selector):
        node = self.nodes[0]
        if selector == "..":
            return Locator([node["parent"]] if node.get("parent") else [])
        if selector.startswith("xpath="):
            return Locator([node["info"]])
        if selector == ".inventory-campaign-info":
            return Locator(node.get("infos", []))
        if selector == "img.inventory-drop-image":
            return Locator(node.get("images", []))
        if selector.startswith("img.inventory-drop-image[src="):
            source = selector.split('[src="', 1)[1][:-2]
            return Locator([image for image in node.get("images", []) if image.get("src") == source])
        raise AssertionError(selector)

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("button", "Claim Now", True)
        return Locator(self.nodes[0].get("buttons", []))

    def get_by_text(self, name, *, exact):
        assert exact is True
        return Locator([{}] if name in self.nodes[0].get("labels", []) else [])


class Page:
    def __init__(self, *, sources=None, buttons=1, label="Reward", href=None):
        self.button_nodes = [{"tag": "button"} for _ in range(buttons)]
        self.card = {"buttons": self.button_nodes, "labels": [label]}
        images = [{"src": source, "parent": self.card} for source in (sources or [IMAGE])]
        self.card["images"] = images
        self.info = {}
        self.container = {"images": images, "infos": [self.info]}
        self.info["parent"] = self.container
        self.card["parent"] = self.container
        self.anchors = [{"href": href or "/drops/campaigns?dropID=campaign-1", "text": "Campaign", "info": self.info}]

    def locator(self, selector):
        if selector == ".inventory-campaign-info a[href]":
            return Locator(self.anchors)
        if selector.startswith('.inventory-campaign-info a[href="'):
            hrefs = {item.split('[href="', 1)[1][:-2] for item in selector.split(", ")}
            return Locator([node for node in self.anchors if node["href"] in hrefs])
        raise AssertionError(selector)


@as_sync
async def test_locates_only_native_unique_card_button_without_clicking():
    page = Page()
    button = await locate_claim_button(page, TARGET, [{"name": "Reward", "image": IMAGE}])
    assert button.nodes == page.button_nodes


@as_sync
@pytest.mark.parametrize("href", ["https://www.twitch.tv/drops/campaigns?dropID=campaign-1", "/drops/campaigns?dropID=campaign-1"])
async def test_campaign_link_can_be_absolute_or_relative(href):
    assert await locate_claim_button(Page(href=href), TARGET, [{"name": "Reward", "image": IMAGE}])


@as_sync
@pytest.mark.parametrize("href", ["https://evil.test/drops/campaigns?dropID=campaign-1", "/drops/campaigns?dropID=other", "/drops/campaigns?dropID=campaign-1&dropID=other"])
async def test_wrong_campaign_or_foreign_link_never_matches(href):
    with pytest.raises(WatchCheckError, match="native_campaign_link_not_unique"):
        await locate_claim_button(Page(href=href), TARGET, [{"name": "Reward", "image": IMAGE}])


@as_sync
async def test_duplicate_campaign_links_fail_before_selecting_a_card():
    page = Page()
    page.anchors.append(dict(page.anchors[0]))
    with pytest.raises(WatchCheckError, match="native_campaign_link_not_unique"):
        await locate_claim_button(page, TARGET, [{"name": "Reward", "image": IMAGE}])


@as_sync
@pytest.mark.parametrize("kwargs", [{"sources": [IMAGE, IMAGE]}, {"buttons": 2}, {"buttons": 0}, {"label": "Different reward"}])
async def test_ambiguous_or_incomplete_card_does_not_match(kwargs):
    with pytest.raises(WatchCheckError, match="native_target_button_not_unique"):
        await locate_claim_button(Page(**kwargs), TARGET, [{"name": "Reward", "image": IMAGE}])


@as_sync
@pytest.mark.parametrize("flag", ["visible", "enabled"])
async def test_button_must_be_visible_and_enabled(flag):
    page = Page()
    page.button_nodes[0][flag] = False
    with pytest.raises(WatchCheckError, match="native_target_button_not_unique"):
        await locate_claim_button(page, TARGET, [{"name": "Reward", "image": IMAGE}])


@as_sync
async def test_can_skip_absent_benefit_for_another_unique_benefit_of_same_drop():
    page = Page()
    button = await locate_claim_button(page, TARGET, [
        {"name": "Absent", "image": "https://cdn.test/absent.png"},
        {"name": "Reward", "image": IMAGE},
    ])
    assert button.nodes == page.button_nodes


@as_sync
async def test_never_accepts_whole_campaign_as_a_reward_card():
    page = Page(label="Wrong card")
    page.container.update(buttons=page.button_nodes, labels=["Reward"])
    with pytest.raises(WatchCheckError, match="native_target_button_not_unique"):
        await locate_claim_button(page, TARGET, [{"name": "Reward", "image": IMAGE}])


@as_sync
async def test_dom_exception_text_is_not_exposed():
    class BrokenPage:
        def locator(self, selector):
            raise RuntimeError("private page content")
    with pytest.raises(WatchCheckError) as error:
        await locate_claim_button(BrokenPage(), TARGET, [{"name": "Reward", "image": IMAGE}])
    assert str(error.value) == "native_dom_unavailable"
