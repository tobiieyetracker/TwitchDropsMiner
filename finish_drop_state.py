"""Strict Inventory state for one bounded finish-and-claim experiment.

Only ``Target.public_dict()`` is intended for reports.  The server-issued claim
identifier stays in memory and is deliberately omitted from repr and reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from watch_check_state import (
    WatchCheckError, _current_user, _field, _identifier, snapshot_inventory,
)


def _now(value: datetime | None) -> datetime:
    result = datetime.now(timezone.utc) if value is None else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise WatchCheckError("now_invalid")
    return result.astimezone(timezone.utc)


def _time(parent: dict, key: str, code: str) -> datetime:
    value = _field(parent, key, code, str)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise WatchCheckError(f"{code}_invalid") from None


def _claim_id(own: dict) -> str | None:
    value = own.get("dropInstanceID")
    if value is not None and type(value) is not str:
        raise WatchCheckError("drop_claim_id_invalid")
    # Empty server values are preserved as unready, never invented or repaired.
    return value


@dataclass(frozen=True)
class Target:
    campaign_id: str
    name: str
    drop_id: str
    game_id: str
    required_minutes: int
    minutes: int
    is_claimed: bool
    claim_id: str | None = field(repr=False)
    starts_at: datetime
    ends_at: datetime
    campaign_ends_at: datetime
    account_link_state: bool | None
    preconditions_met: bool | None
    source_time_bounds: tuple[datetime, datetime, datetime, datetime] = field(repr=False)

    @property
    def ready_to_claim(self) -> bool:
        """Whether server fields are ready; use require_claim_id for time checks."""
        return (
            not self.is_claimed
            and self.required_minutes > 0
            and self.minutes >= self.required_minutes
            and self.preconditions_met is not False
            and isinstance(self.claim_id, str)
            and bool(self.claim_id.strip())
        )

    def can_watch(self, now: datetime | None = None) -> bool:
        return not self.is_claimed and self.starts_at <= _now(now) < self.ends_at

    def require_claim_id(self, now: datetime | None = None) -> str:
        stamp = _now(now)
        if self.is_claimed:
            raise WatchCheckError("target_already_claimed")
        if stamp < self.starts_at:
            raise WatchCheckError("target_not_started")
        if stamp >= self.campaign_ends_at + timedelta(hours=24):
            raise WatchCheckError("claim_window_ended")
        if self.required_minutes <= 0 or self.minutes < self.required_minutes:
            raise WatchCheckError("target_progress_incomplete")
        if self.preconditions_met is False:
            raise WatchCheckError("target_preconditions_not_met")
        if not isinstance(self.claim_id, str) or not self.claim_id.strip():
            raise WatchCheckError("target_claim_id_unavailable")
        return self.claim_id

    def public_dict(self) -> dict:
        """A bounded report view; never serialize this dataclass with asdict()."""
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.name,
            "drop_id": self.drop_id,
            "game_id": self.game_id,
            "required_minutes": self.required_minutes,
            "minutes": self.minutes,
            "is_claimed": self.is_claimed,
            "claim_id_present": isinstance(self.claim_id, str) and bool(self.claim_id.strip()),
            "ready_to_claim": self.ready_to_claim,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "campaign_ends_at": self.campaign_ends_at.isoformat(),
            "account_link_state": self.account_link_state,
            "preconditions_met": self.preconditions_met,
        }


def _campaigns(body: Any, user_id: str) -> list[dict]:
    user, matches = _current_user(body, user_id)
    if matches is not True:
        # This path can submit a mutation, so an unreturned user ID is insufficient.
        raise WatchCheckError("user_id_missing")
    inventory = _field(user, "inventory", "inventory", dict)
    campaigns = _field(inventory, "dropCampaignsInProgress", "campaigns", list)
    seen = set()
    for campaign in campaigns:
        if type(campaign) is not dict:
            raise WatchCheckError("campaign_invalid")
        identity = _identifier(campaign, "id", "campaign_id")
        if identity in seen:
            raise WatchCheckError("campaign_duplicate")
        seen.add(identity)
        _identifier(campaign, "name", "campaign_name")
    return campaigns


def _drops(campaign: dict) -> list[dict]:
    drops = _field(campaign, "timeBasedDrops", "time_based_drops", list)
    seen = set()
    for drop in drops:
        if type(drop) is not dict:
            raise WatchCheckError("drop_invalid")
        identity = _identifier(drop, "id", "drop_id")
        if identity in seen:
            raise WatchCheckError("target_drop_duplicate")
        seen.add(identity)
    return drops


def _target(body: Any, user_id: str, campaign: dict, drop: dict) -> Target:
    campaign_id, drop_id = campaign["id"], drop["id"]
    snapshot = snapshot_inventory(body, user_id, campaign_id, {drop_id})
    own_snapshot = snapshot["drops"][0]
    if own_snapshot["required_minutes"] <= 0:
        raise WatchCheckError("drop_required_minutes_invalid")
    game = _field(campaign, "game", "campaign_game", dict)
    game_id = _identifier(game, "id", "campaign_game_id")
    campaign_start = _time(campaign, "startAt", "campaign_start")
    campaign_end = _time(campaign, "endAt", "campaign_end")
    drop_start = _time(drop, "startAt", "drop_start")
    drop_end = _time(drop, "endAt", "drop_end")
    starts, ends = max(campaign_start, drop_start), min(campaign_end, drop_end)
    if campaign_start >= campaign_end or drop_start >= drop_end or starts >= ends:
        raise WatchCheckError("target_time_range_invalid")
    own_campaign = campaign.get("self")
    linked = None
    if own_campaign is not None:
        if type(own_campaign) is not dict:
            raise WatchCheckError("campaign_self_invalid")
        linked = own_campaign.get("isAccountConnected")
        if linked is not None and type(linked) is not bool:
            raise WatchCheckError("account_link_state_invalid")
    preconditions_met = drop["self"].get("hasPreconditionsMet")
    if preconditions_met is not None and type(preconditions_met) is not bool:
        raise WatchCheckError("drop_preconditions_invalid")
    return Target(
        campaign_id=campaign_id, name=campaign["name"], drop_id=drop_id,
        game_id=game_id, required_minutes=own_snapshot["required_minutes"],
        minutes=own_snapshot["minutes"], is_claimed=own_snapshot["is_claimed"],
        claim_id=_claim_id(drop["self"]), starts_at=starts, ends_at=ends,
        campaign_ends_at=campaign_end, account_link_state=linked,
        preconditions_met=preconditions_met,
        source_time_bounds=(campaign_start, campaign_end, drop_start, drop_end),
    )


def select_target(
    body: Any, user_id: str, campaign_name: str, drop_id: str | None = None,
    now: datetime | None = None,
) -> Target:
    """Select one real Inventory drop; no missing state becomes zero or eligible."""
    _now(now)
    if type(campaign_name) is not str or not campaign_name.strip():
        raise WatchCheckError("target_campaign_name_invalid")
    if drop_id is not None and (type(drop_id) is not str or not drop_id.strip()):
        raise WatchCheckError("target_drop_id_invalid")
    campaigns = [item for item in _campaigns(body, user_id) if item["name"] == campaign_name]
    if len(campaigns) != 1:
        raise WatchCheckError("target_campaign_not_unique_or_present")
    campaign = campaigns[0]
    drops = _drops(campaign)
    if drop_id is not None:
        matches = [drop for drop in drops if drop["id"] == drop_id]
        if len(matches) != 1:
            raise WatchCheckError("target_drop_not_present")
        return _target(body, user_id, campaign, matches[0])
    # Validate every candidate's server state before choosing the only unclaimed
    # one. An unknown candidate cannot safely be treated as already claimed.
    parsed = [_target(body, user_id, campaign, drop) for drop in drops]
    unclaimed = [item for item in parsed if not item.is_claimed]
    if len(unclaimed) == 1:
        return unclaimed[0]
    if len(parsed) == 1:
        return parsed[0]
    raise WatchCheckError("target_drop_ambiguous_or_absent")


def refresh_target(
    body: Any, user_id: str, target: Target, now: datetime | None = None,
) -> Target | None:
    """Refresh the exact IDs, with genuine disappearance remaining unverified."""
    _now(now)
    campaigns = _campaigns(body, user_id)
    matches = [item for item in campaigns if item["id"] == target.campaign_id]
    if not matches:
        if any(item["name"] == target.name for item in campaigns):
            raise WatchCheckError("target_campaign_identity_changed")
        return None
    campaign = matches[0]
    matches = [drop for drop in _drops(campaign) if drop["id"] == target.drop_id]
    if not matches:
        return None
    updated = _target(body, user_id, campaign, matches[0])
    for attr in (
        "campaign_id", "name", "drop_id", "game_id", "required_minutes",
        "starts_at", "ends_at", "campaign_ends_at",
    ):
        if getattr(updated, attr) != getattr(target, attr):
            raise WatchCheckError(f"target_{attr}_changed")
    if updated.source_time_bounds != target.source_time_bounds:
        raise WatchCheckError("target_time_bounds_changed")
    if target.is_claimed and not updated.is_claimed:
        raise WatchCheckError("target_claimed_state_regressed")
    if not updated.is_claimed and updated.minutes < target.minutes:
        raise WatchCheckError("target_minutes_regressed")
    if (
        isinstance(target.claim_id, str) and target.claim_id.strip()
        and isinstance(updated.claim_id, str) and updated.claim_id.strip()
        and updated.claim_id != target.claim_id
    ):
        raise WatchCheckError("target_claim_id_changed")
    return updated
