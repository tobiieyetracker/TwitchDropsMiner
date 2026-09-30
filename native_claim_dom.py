"""Read-only, fail-closed DOM targeting for one Inventory-backed native claim.

The page does not expose drop IDs on reward cards. Bind the campaign by its
official link, then one uniquely attributable benefit's name and image. A
separate request guard must still enforce the real server-issued instance ID.
"""
from __future__ import annotations

from time import monotonic
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from watch_check_state import WatchCheckError, _field, _identifier, check_envelope


# Read operations found in OperationDefinition ASTs in the already saved
# twitch-drops-root.js and twitch-assets/*.js (2026-09-30). No live requests.
# PlaybackAccessToken_Template additionally comes from the raw query string in
# twitch-campaign-page.html's inline bootstrap (line 1, character offset 13412),
# not an AST: it selects only streamPlaybackAccessToken/videoPlaybackAccessToken.
# Unknown persisted operations are not assumed to be reads just because their
# name differs from ClaimDrop; the browser request guard uses this allowlist.
WEBSITE_READ_OPERATIONS = frozenset({
    "ActiveGoals", "AdContextChannelIDQuery", "AdRequestHandling",
    "Ads_Components_AdManager_User", "AllGoals", "AvailableEmotesForChannelPaginated",
    "BatchGetWatchStreaks", "BitsBalance", "BitsConfigContext_Channel",
    "BitsConfigContext_Global", "BitsConfigContext_SharedChatChannels", "Bits_BuyCard_Offers",
    "BonusBitsChannelBanner_EligibleRewards", "BulkActiveHypeTrainStatusesQuery", "BulkAllActiveHypeTrainStatusesQuery",
    "CanCreateClip", "ChannelClipCore", "ChannelCollaborationEligibilityQuery",
    "ChannelCollectionCore", "ChannelPage_SubscribeButton_User", "ChannelRoot_AboutPanel",
    "ChannelShell", "ChannelSkins", "ChannelSubscribeRedirect",
    "ChannelVideoCore", "ClipShareOverlay", "CollectionSideBar",
    "CollectionTopBar", "ComscoreStreamingQuery", "Consent",
    "ContentClassificationContext", "ContentClassificationContextStreamPubsub", "ContentPolicyPropertiesQuery",
    "CoreActionsCurrentUser", "CoreAuthCurrentUser", "Core_Services_Spade_CurrentUser",
    "Core_Services_Spade_Video", "CostreamingCurrentUser", "CostreamingDiscoveryContextQuery",
    "CostreamingInfo", "CreatorWalletPromoCallout", "CreatorWalletPromoEligibility",
    "DVRAvailabilityQuery", "DVRVideoIDQuery", "DirectoryGameRedirect",
    "DiscoveryPreferenceQuery", "DropsInventoryRewardGroupStatus", "EmotesForChannelFollowStatus",
    "ExtensionPanel_Conditions_BitsBalance", "ExtensionsForChannel", "ExtensionsInfoBalloon",
    "ExtensionsNotificationBitsBalance", "ExtensionsOverlay", "ExtensionsUIContext_ChannelID",
    "FeedInteractionHook_GetClipBySlug", "FetchAdsService_FetchAds", "FollowButton_FollowEvent_User",
    "FollowButton_User", "FollowPanelOverlay", "GetCreatorPromotion",
    "GetHypeTrainExecution", "GetIDFromLogin", "GetUserID",
    "GuestStarBatchCollaborationQuery", "HappeningNowSettings", "Inventory",
    "LiveNotificationsToggle_User", "LiveShoppingProductDetails", "LiveShoppingProductSummaries",
    "LiveStreamTime", "NielsenContentMetadata", "OfflineBannerOverlay",
    "OfflineEmbedVODAndSchedule", "PartnerPlusPublicQuery", "PlaybackAccessToken",
    "PlaybackAccessToken_Template",
    "PlayerTrackingContextQuery", "PrefetchPlaybackAccessToken", "PreviewContentOverlayQuery",
    "PrimeLinkConnectQuery", "PrimeLinking_CurrentUser", "Prime_Current_User",
    "Prime_PrimeOfferList_PrimeOffers_Eligibility", "Prime_PrimeOffers_CurrentUser", "Prime_PrimeOffers_PrimeOfferIds_Eligibility",
    "ProductConsent", "PurchaseOrderContextGetPurchaseOrder", "PurchaseOrderSuccessSnackbar",
    "RecoveryVODsByChannel", "ReportMenuItem", "RewardCodeModal",
    "SDAWrapperPremiumEventDVRQuery", "SearchTray_SearchSuggestions", "SettingsNotificationsPage_User_Portal",
    "Settings_ChannelClipsSettings", "ShareClipRenderStatus", "SharedChatSession",
    "SideNav", "SponsorBannerChannelContext", "SponsorBannerEligibility",
    "StoryPreviewsWithOrder", "StreamRefetchManager", "StreamTagsTrackingChannel",
    "Sub_Analytics", "SubscribedContext", "SuspendedWatchStreaks",
    "SyncedSettingsEmoteAnimations", "TopNav_CurrentUser", "TrackingManager_RequestInfo",
    "TurboAwarenessBanner_ChannelSharedPromotions", "TurboProductInformation", "UseGetUserLogin",
    "UseLive", "UseLiveBroadcast", "UserCanPrimeSubscribe",
    "UserMenuCurrentUser", "UserSelfFollowingNotificationsSettingsState", "VODMidrollManager",
    "VODPreviewOverlay", "VideoAccessToken_Clip", "VideoAccessToken_Collection",
    "VideoAdBanner", "VideoAdRequestDecline", "VideoPlayerClipPostplayRecommendationsOverlay",
    "VideoPlayerClipPostplayRecommendationsOverlay__recs", "VideoPlayerClipsButtonBroadcaster", "VideoPlayerMediaSessionManager",
    "VideoPlayerOfflineRecommendationsOverlay", "VideoPlayerPixelAnalyticsUrls", "VideoPlayerPremiumContentOverlayChannel",
    "VideoPlayerSettingsWithClipMetadata", "VideoPlayerStatusOverlayChannel", "VideoPlayerStreamInfoOverlayChannel",
    "VideoPlayerStreamInfoOverlayClip", "VideoPlayerStreamInfoOverlayVOD", "VideoPlayerStreamMetadata",
    "VideoPlayerSubscriberVODOverlayVideoQuery", "VideoPlayerVODPostplayRecommendations", "VideoPlayer_AgeGateOverlayBroadcaster",
    "VideoPlayer_ChapterSelectButtonVideo", "VideoPlayer_CollectionContent", "VideoPlayer_CollectionManager",
    "VideoPlayer_MutedSegmentsAlertOverlay", "VideoPlayer_VODSeekbar", "VideoPlayer_VODSeekbarPreviewVideo",
    "VideoPlayer_VideoSourceManager", "VideoPlayer_ViewCount", "VideoPreviewOverlay",
    "VideoShareBox_CollectionTrackingMeta", "VideoShareBox_TrackingVideoContext", "ViewerDropsDashboard",
    "ViewerRewardDropInventory", "Whispers_Tracking_CurrentUser", "WinbackReminder",
    "queryUserViewedVideo",
})

WEB = "https://www.twitch.tv"
IMAGES = "img.inventory-drop-image"
INFO = ".inventory-campaign-info"


def _image_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise WatchCheckError("native_benefit_image_invalid") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WatchCheckError("native_benefit_image_invalid")
    return value


def benefits_for_target(body, target) -> list[dict[str, str]]:
    """Return public benefit labels that cannot identify another campaign drop.

The caller has already bound the current Inventory user to the original journal.
Missing fields are unknown; an explicit null benefitEdges means no rendered card,
matching the downloaded page's nullable-edge handling.
"""
    check_envelope(body)
    data = _field(body, "data", "data", dict)
    user = _field(data, "currentUser", "user", dict)
    if not user:
        raise WatchCheckError("user_empty")
    inventory = _field(user, "inventory", "inventory", dict)
    campaigns = _field(inventory, "dropCampaignsInProgress", "campaigns", list)
    found = []
    campaign_ids = set()
    for campaign in campaigns:
        if type(campaign) is not dict:
            raise WatchCheckError("campaign_invalid")
        campaign_id = _identifier(campaign, "id", "campaign_id")
        if campaign_id in campaign_ids:
            raise WatchCheckError("campaign_duplicate")
        campaign_ids.add(campaign_id)
        if campaign_id == target.campaign_id:
            found.append(campaign)
    if len(found) != 1:
        raise WatchCheckError("native_target_campaign_missing")
    drops = _field(found[0], "timeBasedDrops", "time_based_drops", list)
    drop_ids, selected, others = set(), [], []
    for drop in drops:
        if type(drop) is not dict:
            raise WatchCheckError("drop_invalid")
        drop_id = _identifier(drop, "id", "drop_id")
        if drop_id in drop_ids:
            raise WatchCheckError("target_drop_duplicate")
        drop_ids.add(drop_id)
        if "benefitEdges" not in drop:
            raise WatchCheckError("native_benefit_edges_missing")
        edges = drop["benefitEdges"]
        if edges is None:
            continue
        if type(edges) is not list:
            raise WatchCheckError("native_benefit_edges_invalid")
        for edge in edges:
            if type(edge) is not dict:
                raise WatchCheckError("native_benefit_edge_invalid")
            benefit = _field(edge, "benefit", "native_benefit", dict)
            _identifier(benefit, "id", "native_benefit_id")
            name = _identifier(benefit, "name", "native_benefit_name")
            image = _image_url(_identifier(benefit, "imageAssetURL", "native_benefit_image"))
            (selected if drop_id == target.drop_id else others).append((name, image))
    if target.drop_id not in drop_ids:
        raise WatchCheckError("native_target_drop_missing")
    names = {name for name, _ in others}
    images = {image for _, image in others}
    unique = sorted({(name, image) for name, image in selected if name not in names and image not in images})
    if not unique:
        raise WatchCheckError("native_target_benefits_not_unique")
    return [{"name": name, "image": image} for name, image in unique]


def _css_string(value: str) -> str:
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\a ").replace("\r", "\\d ").replace("\f", "\\c ")) + '"'


def _is_campaign_link(href, campaign_id: str) -> bool:
    if type(href) is not str:
        return False
    try:
        parsed = urlsplit(urljoin(WEB, href))
        return (
            parsed.scheme == "https" and parsed.netloc == "www.twitch.tv"
            and parsed.path == "/drops/campaigns" and not parsed.fragment
            and parse_qs(parsed.query) == {"dropID": [campaign_id]}
        )
    except (TypeError, ValueError):
        return False


async def _card_button(image, name: str, wait_deadline: float):
    current = image
    # The saved card has only a few wrappers; never walk indefinitely or accept
    # the entire campaign/page as a reward card after the expected DOM changes.
    for _ in range(16):
        current = current.locator("..")
        if await current.count() != 1 or await current.locator(INFO).count():
            return None
        image_count = await current.locator(IMAGES).count()
        button = current.get_by_role("button", name="Claim Now", exact=True)
        button_count = await button.count()
        if image_count > 1 or button_count > 1:
            return None
        if image_count == 1 and await current.get_by_text(name, exact=True).count():
            # A response callback can precede React's DOM commit. Wait only
            # inside an already uniquely identified benefit card, sharing one
            # five-second budget across all benefits of this same target drop.
            remaining = wait_deadline - monotonic()
            if remaining > 0:
                try:
                    await button.first.wait_for(state="visible", timeout=max(1, int(remaining * 1000)))
                except Exception:
                    return None
            if await button.count() == 1 and await button.is_visible() and await button.is_enabled():
                return button
            return None
    return None


async def locate_claim_button(page, target, benefits):
    """Return one real native button locator; this function never clicks it.

The context must use English locale. No DOM insertion, event dispatch, React
introspection, page function invocation, or broad text-only button selection.
"""
    try:
        relative = "/drops/campaigns?dropID=" + quote(target.campaign_id, safe="")
        expected_link = page.locator(
            f"{INFO} a[href={_css_string(relative)}], "
            f"{INFO} a[href={_css_string(WEB + relative)}]"
        )
        try:
            await expected_link.first.wait_for(state="attached", timeout=10000)
        except Exception:
            raise WatchCheckError("native_campaign_link_not_unique") from None
        anchors = page.locator(f"{INFO} a[href]")
        matches = []
        for index in range(await anchors.count()):
            href = await anchors.nth(index).get_attribute("href")
            if _is_campaign_link(href, target.campaign_id):
                matches.append(href)
        if len(matches) != 1:
            raise WatchCheckError("native_campaign_link_not_unique")
        anchor = page.locator(f"{INFO} a[href={_css_string(matches[0])}]")
        if await anchor.count() != 1:
            raise WatchCheckError("native_campaign_link_not_unique")
        if (await anchor.inner_text()).strip() != target.name:
            raise WatchCheckError("native_campaign_name_mismatch")
        info = anchor.locator(
            'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), '
            '" inventory-campaign-info ")][1]'
        )
        campaign = info.locator("..")
        if await campaign.count() != 1 or await campaign.locator(INFO).count() != 1:
            raise WatchCheckError("native_campaign_container_invalid")
        images = campaign.locator(IMAGES)
        button_wait_deadline = monotonic() + 5
        for benefit in benefits:
            if (type(benefit) is not dict or set(benefit) != {"name", "image"}
                or type(benefit["name"]) is not str or not benefit["name"].strip()
                or type(benefit["image"]) is not str):
                raise WatchCheckError("native_benefits_invalid")
            raw_sources = []
            for index in range(await images.count()):
                source = await images.nth(index).get_attribute("src")
                if isinstance(source, str) and urljoin(WEB, source) == benefit["image"]:
                    raw_sources.append(source)
            if len(raw_sources) != 1:
                continue
            image = campaign.locator(f"{IMAGES}[src={_css_string(raw_sources[0])}]")
            if await image.count() != 1:
                continue
            button = await _card_button(image, benefit["name"], button_wait_deadline)
            if button is not None:
                return button
        raise WatchCheckError("native_target_button_not_unique")
    except WatchCheckError:
        raise
    except Exception:
        # Playwright exception strings can include page text and request details.
        raise WatchCheckError("native_dom_unavailable") from None
