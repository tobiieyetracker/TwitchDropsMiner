"""Recovery for the challenge envelope used by Twitch's web GraphQL client."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from exceptions import GQLException


Json = dict[str, Any]
Payload = Json | list[Json]
CAMPAIGN_PATHS = {
    "ViewerDropsDashboard": ("currentUser", "dropCampaigns"),
    "DropCampaignDetails": ("user", "dropCampaign"),
}
# Never replay a successful mutation when another operation in its batch is challenged.
READ_OPERATIONS = frozenset({
    *CAMPAIGN_PATHS, "Inventory", "DropCurrentSessionContext",
    "DropsHighlightService_AvailableDrops", "DirectoryPage_Game",
    "VideoPlayerStreamInfoOverlayChannel", "ChannelPointsContext",
    "PlaybackAccessToken", "DirectoryGameRedirect", "OnsiteNotifications_ListNotifications",
})


class CampaignAccessError(GQLException):
    pass


def _as_list(payload: Payload, count: int) -> list[Json]:
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != count or not all(isinstance(item, dict) for item in items):
        raise GQLException("Twitch returned an unexpected GraphQL batch response")
    return items


def challenge_type(operation: Json, response: Json) -> str | None:
    extensions = response.get("extensions") or {}
    challenge = extensions.get("challenge") or {}
    if challenge.get("type"):
        return str(challenge["type"])
    errors = response.get("errors") or []
    for error in errors:
        code = (error.get("extensions") or {}).get("code")
        message = str(error.get("message", "")).casefold()
        if code == "IntegrityCheckFailed" or message in {
            "failed integrity check", "integritycheckfailed",
        }:
            return "integrity"
    # App clients can get a null field without an error or a challenge envelope.
    # An absent/unauthenticated parent and unrelated errors are different failures.
    path = CAMPAIGN_PATHS.get(operation.get("operationName"))
    if path and not errors:
        parent = (response.get("data") or {}).get(path[0])
        if isinstance(parent, dict) and path[1] in parent and parent[path[1]] is None:
            return "integrity"
    return None


def _validate_available_drops(response: Json) -> None:
    data = response.get("data")
    if not isinstance(data, dict) or "channel" not in data:
        raise CampaignAccessError("Twitch's AvailableDrops response schema has changed")
    channel = data["channel"]
    if channel is None:
        raise CampaignAccessError(
            "Twitch returned no channel for AvailableDrops. "
            "Channel drop availability is unknown."
        )
    if not isinstance(channel, dict) or "viewerDropCampaigns" not in channel:
        raise CampaignAccessError("Twitch's AvailableDrops response schema has changed")
    campaigns = channel["viewerDropCampaigns"]
    if campaigns is None:
        raise CampaignAccessError(
            "Twitch returned null for AvailableDrops.viewerDropCampaigns. "
            "Channel drop availability is unknown; this is not an empty campaign list."
        )
    if not isinstance(campaigns, list):
        raise CampaignAccessError("Twitch's AvailableDrops response schema has changed")


def validate_campaign_response(operation: Json, response: Json) -> None:
    if operation.get("operationName") == "DropsHighlightService_AvailableDrops":
        # Unlike dashboard nulls, these nulls are not evidence of an integrity challenge.
        _validate_available_drops(response)
        return
    path = CAMPAIGN_PATHS.get(operation.get("operationName"))
    if not path:
        return
    parent = (response.get("data") or {}).get(path[0])
    if not isinstance(parent, dict):
        raise CampaignAccessError(
            "Twitch returned no user for the campaign query. Check the login session."
        )
    if path[1] not in parent:
        raise CampaignAccessError("Twitch's campaign response schema has changed")
    value = parent[path[1]]
    if value is None:
        raise CampaignAccessError(
            "Twitch did not return campaign data. The query may require a valid "
            "web integrity session; this is not an empty campaign list. "
            "Use --browser-auth, or complete the check in the open Twitch browser."
        )
    expected_type = list if path[1] == "dropCampaigns" else dict
    if not isinstance(value, expected_type):
        raise CampaignAccessError("Twitch's campaign response schema has changed")


async def recover_challenges(
    operations: Payload,
    response: Payload,
    *,
    refresh: Callable[[], Awaitable[None]] | None,
    replay: Callable[[list[Json]], Awaitable[Payload]],
) -> Payload:
    """Refresh once and replay only challenged reads, preserving batch positions."""
    ops = operations if isinstance(operations, list) else [operations]
    responses = _as_list(response, len(ops))
    pending: list[int] = []
    for index, (operation, item) in enumerate(zip(ops, responses)):
        kind = challenge_type(operation, item)
        if kind is None:
            continue
        if kind != "integrity":
            raise CampaignAccessError(
                "Twitch requires an interactive check. Complete it in the Twitch browser "
                "and retry; this check cannot be retried automatically."
            )
        if operation.get("operationName") not in READ_OPERATIONS:
            raise CampaignAccessError("Twitch challenged a write operation; it was not replayed")
        pending.append(index)
    if not pending:
        return response
    if refresh is None:
        raise CampaignAccessError(
            "Twitch requires a web integrity session for campaign discovery. "
            "Start the miner with --browser-auth; changing the query hash or "
            "switching to SMARTBOX does not provide that session."
        )
    await refresh()
    retried = _as_list(await replay([ops[index] for index in pending]), len(pending))
    merged = list(responses)
    for index, item in zip(pending, retried):
        if challenge_type(ops[index], item) is not None:
            raise CampaignAccessError(
                "Twitch still rejected the campaign query after refreshing integrity. "
                "Check the open Twitch browser; no further automatic replay was attempted."
            )
        merged[index] = item
    return merged if isinstance(operations, list) else merged[0]
