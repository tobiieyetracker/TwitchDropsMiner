"""Helpers for merging campaign candidates observed through live channels.

Channel AvailableDrops is a useful fallback when the account dashboard is not
available, but it is not a complete campaign directory. Keep its provenance
and per-drop source channels separate from Twitch's account ACL.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


Json = dict[str, Any]


def _source(channel_id: int | str, login: str, observed_at: str) -> Json:
    return {
        "channel_id": str(channel_id),
        "channel_login": login,
        "observed_at": observed_at,
    }


def normalize_available_campaign(
    raw: Json,
    *,
    channel_id: int | str,
    channel_login: str,
    game: Json,
    observed_at: str,
) -> Json | None:
    """Return a DropsCampaign-compatible candidate without inventing user state."""
    campaign_id = raw.get("id")
    name = raw.get("name")
    starts_at = _normalize_time(raw.get("startAt"))
    ends_at = _normalize_time(raw.get("endAt"))
    drops = raw.get("timeBasedDrops")
    game_data = raw.get("game")
    if not (
        isinstance(campaign_id, str) and campaign_id
        and isinstance(name, str) and name
        and isinstance(drops, list)
        and isinstance(game, dict)
    ):
        return None

    merged_game = deepcopy(game_data) if isinstance(game_data, dict) else {}
    # AvailableDrops variants can omit part of the game object. The stream's
    # directory result supplies the authoritative game identity for this scan.
    for key in ("id", "name", "displayName", "boxArtURL", "slug"):
        if not merged_game.get(key) and game.get(key):
            merged_game[key] = game[key]
    if not merged_game.get("id") or not (merged_game.get("name") or merged_game.get("displayName")):
        return None
    merged_game["boxArtURL"] = merged_game.get("boxArtURL") or ""

    # Campaign-level bounds are absent from some AvailableDrops responses.
    # Build drops first so a missing campaign bound can be derived only from
    # actual, valid drop windows. Never substitute an arbitrary date.
    valid_drops: list[tuple[Json, str | None, str | None]] = []
    for raw_drop in drops:
        if not isinstance(raw_drop, dict):
            continue
        drop_id = raw_drop.get("id")
        drop_name = raw_drop.get("name")
        minutes = raw_drop.get("requiredMinutesWatched")
        edges = raw_drop.get("benefitEdges")
        if (
            not isinstance(drop_id, str) or not drop_id
            or not isinstance(drop_name, str) or not drop_name
            or not isinstance(minutes, int)
            or not isinstance(edges, list)
        ):
            continue
        drop = deepcopy(raw_drop)
        drop_start = _normalize_time(drop.get("startAt"))
        drop_end = _normalize_time(drop.get("endAt"))
        # If a campaign bound is missing, a drop with no corresponding bound
        # cannot safely inherit the value derived from another drop.
        if (starts_at is None and drop_start is None) or (ends_at is None and drop_end is None):
            continue
        effective_start = drop_start or starts_at
        effective_end = drop_end or ends_at
        if effective_start is None or effective_end is None or not _valid_window(
            effective_start, effective_end
        ):
            continue
        drop["startAt"] = effective_start
        drop["endAt"] = effective_end
        drop.setdefault("preconditionDrops", [])
        if not isinstance(drop.get("self"), dict):
            drop.pop("self", None)
        else:
            drop["self"].setdefault("dropInstanceID", None)
            drop["self"].setdefault("isClaimed", False)
            drop["self"].setdefault("currentMinutesWatched", 0)
        # Some AvailableDrops selections omit this optional field. Unknown is
        # preserved as UNKNOWN instead of treating the reward as a badge.
        for edge in drop["benefitEdges"]:
            if isinstance(edge, dict) and isinstance(edge.get("benefit"), dict):
                edge["benefit"].setdefault("distributionType", "UNKNOWN")
                edge["benefit"].setdefault("imageAssetURL", "")
        valid_drops.append((drop, drop_start, drop_end))
    if not valid_drops:
        return None

    derived_bounds: dict[str, str] = {}
    if starts_at is None:
        starts_at = min(drop_start for _, drop_start, _ in valid_drops if drop_start is not None)
        derived_bounds["startAt"] = "min(timeBasedDrops[].startAt)"
    if ends_at is None:
        ends_at = max(drop_end for _, _, drop_end in valid_drops if drop_end is not None)
        derived_bounds["endAt"] = "max(timeBasedDrops[].endAt)"
    if not _valid_window(starts_at, ends_at):
        return None

    campaign = deepcopy(raw)
    campaign["game"] = merged_game
    campaign["startAt"] = starts_at
    campaign["endAt"] = ends_at
    campaign.setdefault("accountLinkURL", "")
    # Absence of a self edge means unknown. It is not a negative or positive
    # account-link result.
    if not isinstance(campaign.get("self"), dict):
        campaign.pop("self", None)
    # Absence of allow data is not proof that all channels are allowed. The
    # observed channels below form a bounded operational candidate set only.
    if not isinstance(campaign.get("allow"), dict):
        campaign["allow"] = {"isEnabled": False, "channels": []}
    if not isinstance(campaign.get("status"), str):
        campaign["status"] = _window_status(starts_at, ends_at)
    else:
        campaign["status"] = campaign["status"].upper()

    normalized_drops: list[Json] = []
    for drop, drop_start, drop_end in valid_drops:
        # A bound already present on a drop stays authoritative. The fallback
        # is used only when the campaign supplied that bound.
        drop["startAt"] = drop_start or starts_at
        drop["endAt"] = drop_end or ends_at
        normalized_drops.append(drop)
    if not normalized_drops:
        return None
    campaign["timeBasedDrops"] = normalized_drops
    discovery = {
        "source": "DropsHighlightService_AvailableDrops",
        "sources": [_source(channel_id, channel_login, observed_at)],
        "drop_sources": {
            drop["id"]: [_source(channel_id, channel_login, observed_at)]
            for drop in normalized_drops
        },
        "conflicts": [],
        "coverage": "partial_channel_scan",
    }
    if derived_bounds:
        discovery["derived_campaign_window"] = {
            "source": "timeBasedDrops",
            "bounds": derived_bounds,
        }
    campaign["_discovery"] = discovery
    return campaign


def merge_channel_campaign(existing: Json, incoming: Json) -> Json:
    """Merge a repeated campaign ID, unioning drops/sources and recording conflicts."""
    result = deepcopy(existing)
    old_meta = result.setdefault("_discovery", {})
    new_meta = incoming.get("_discovery") or {}
    for source in new_meta.get("sources", []):
        if source not in old_meta.setdefault("sources", []):
            old_meta["sources"].append(deepcopy(source))

    old_drops = {drop.get("id"): drop for drop in result.get("timeBasedDrops", [])}
    for drop in incoming.get("timeBasedDrops", []):
        drop_id = drop.get("id")
        source_list = new_meta.get("drop_sources", {}).get(drop_id, [])
        old_meta.setdefault("drop_sources", {}).setdefault(drop_id, [])
        for source in source_list:
            if source not in old_meta["drop_sources"][drop_id]:
                old_meta["drop_sources"][drop_id].append(deepcopy(source))
        if drop_id not in old_drops:
            copied = deepcopy(drop)
            result.setdefault("timeBasedDrops", []).append(copied)
            old_drops[drop_id] = copied
            continue
        previous = old_drops[drop_id]
        # Do not silently replace conflicting data returned for the same
        # campaign/drop IDs by different channels. Keep the first payload and
        # preserve a compact conflict record for later review.
        for key, value in drop.items():
            if key.startswith("_") or key == "self":
                continue
            if key in previous and previous[key] != value:
                old_meta.setdefault("conflicts", []).append({
                    "drop_id": drop_id,
                    "field": key,
                    "kept": deepcopy(previous[key]),
                    "observed": deepcopy(value),
                    "source": deepcopy(source_list),
                })

    for key in ("name", "startAt", "endAt", "game"):
        if key in result and key in incoming and result[key] != incoming[key]:
            old_meta.setdefault("conflicts", []).append({
                "field": key,
                "kept": deepcopy(result[key]),
                "observed": deepcopy(incoming[key]),
                "source": deepcopy(new_meta.get("sources", [])),
            })
    return result


def merge_inventory_campaign(inventory: Json, candidate: Json) -> Json:
    """Merge account-authoritative Inventory fields into a channel candidate."""
    result = deepcopy(candidate)
    discovery = deepcopy(candidate.get("_discovery") or {})
    for key, value in inventory.items():
        if key.startswith("_") or key == "timeBasedDrops":
            continue
        if value is None:
            # A null optional field is unknown, not a reason to erase usable
            # campaign data already supplied by AvailableDrops.
            continue
        # Inventory is the source of truth for account-link state, campaign
        # status, allow-lists and account-visible campaign metadata.
        result[key] = deepcopy(value)
    candidate_drops = {drop.get("id"): drop for drop in result.get("timeBasedDrops", [])}
    for inventory_drop in inventory.get("timeBasedDrops", []) or []:
        drop_id = inventory_drop.get("id")
        if drop_id in candidate_drops:
            merged_drop = deepcopy(candidate_drops[drop_id])
            merged_drop.update({
                key: deepcopy(value)
                for key, value in inventory_drop.items()
                if value is not None
            })
            candidate_drops[drop_id].clear()
            candidate_drops[drop_id].update(merged_drop)
        else:
            copied_drop = deepcopy(inventory_drop)
            if copied_drop.get("self") is None:
                copied_drop.pop("self", None)
            result.setdefault("timeBasedDrops", []).append(copied_drop)
            candidate_drops[drop_id] = result["timeBasedDrops"][-1]
    result["_discovery"] = discovery
    return result


def _window_status(starts_at: str, ends_at: str) -> str:
    try:
        start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "UNKNOWN"
    now = datetime.now(timezone.utc)
    if now < start:
        return "UPCOMING"
    if now >= end:
        return "EXPIRED"
    return "ACTIVE"


def _valid_window(starts_at: str, ends_at: str) -> bool:
    try:
        start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    return start.tzinfo is not None and end.tzinfo is not None and start < end


def _normalize_time(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
