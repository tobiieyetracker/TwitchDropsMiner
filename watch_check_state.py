"""Strict, secret-free server snapshots for a bounded watch check.

These parsers deliberately do not turn missing users, null inventory fields, or
missing per-drop state into an empty inventory or zero watched minutes.
"""
from __future__ import annotations

from typing import Any


class WatchCheckError(Exception):
    """A stable diagnostic code without any response body or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _field(parent: dict, key: str, code: str, expected_type: type) -> Any:
    if key not in parent:
        raise WatchCheckError(f"{code}_missing")
    value = parent[key]
    if value is None:
        raise WatchCheckError(f"{code}_null")
    # bool is a subclass of int, but is never a valid minute count.
    if type(value) is not expected_type:
        raise WatchCheckError(f"{code}_invalid")
    return value


def _identifier(parent: dict, key: str, code: str) -> str:
    value = _field(parent, key, code, str)
    if not value.strip():
        raise WatchCheckError(f"{code}_invalid")
    return value


def _minutes(parent: dict, key: str, code: str) -> int:
    value = _field(parent, key, code, int)
    if value < 0:
        raise WatchCheckError(f"{code}_invalid")
    return value


def check_envelope(body: Any) -> None:
    if not isinstance(body, dict):
        raise WatchCheckError("response_invalid")
    if "extensions" in body:
        extensions = body["extensions"]
        if not isinstance(extensions, dict):
            raise WatchCheckError("extensions_invalid")
        # Any populated challenge is a stop condition, not only integrity.
        if "challenge" in extensions and extensions["challenge"] is not None:
            raise WatchCheckError("gql_challenge")
    if "errors" in body:
        errors = body["errors"]
        if not isinstance(errors, list):
            raise WatchCheckError("gql_errors_invalid")
        if errors:
            raise WatchCheckError("gql_errors")
    if "error" in body:
        raise WatchCheckError("gql_errors")


def _current_user(body: Any, user_id: str) -> tuple[dict, bool | None]:
    check_envelope(body)
    data = _field(body, "data", "data", dict)
    user = _field(data, "currentUser", "user", dict)
    if not user:
        raise WatchCheckError("user_empty")
    user_matches = None
    if "id" in user:
        returned_id = _identifier(user, "id", "user_id")
        if returned_id != user_id:
            raise WatchCheckError("user_id_mismatch")
        user_matches = True
    return user, user_matches


def snapshot_current(body: Any, user_id: str, target_drop_ids: set[str]) -> dict:
    """Return the actual CurrentDrop state, allowing a genuine null session."""
    user, user_matches = _current_user(body, user_id)
    if "dropCurrentSession" not in user:
        raise WatchCheckError("current_session_missing")
    session = user["dropCurrentSession"]
    snapshot = {"user_present": True, "user_matches": user_matches}
    if session is None:
        return {**snapshot, "session_state": "none", "minutes": None}
    if not isinstance(session, dict):
        raise WatchCheckError("current_session_invalid")
    drop_id = _identifier(session, "dropID", "current_drop_id")
    minutes = _minutes(session, "currentMinutesWatched", "current_minutes")
    return {
        **snapshot,
        "session_state": "present",
        "drop_id": drop_id,
        "minutes": minutes,
        "target_drop": drop_id in target_drop_ids,
    }


def snapshot_inventory(
    body: Any, user_id: str, campaign_id: str, target_drop_ids: set[str]
) -> dict:
    """Return only server state for requested drops in the target campaign."""
    user, user_matches = _current_user(body, user_id)
    inventory = _field(user, "inventory", "inventory", dict)
    campaigns = _field(
        inventory, "dropCampaignsInProgress", "campaigns", list
    )
    target = None
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            raise WatchCheckError("campaign_invalid")
        returned_id = _identifier(campaign, "id", "campaign_id")
        if returned_id == campaign_id:
            if target is not None:
                raise WatchCheckError("target_campaign_duplicate")
            target = campaign
    snapshot = {
        "user_present": True,
        "user_matches": user_matches,
        "campaigns_state": "list",
        "target_state": "absent" if target is None else "present",
        "drops": [],
    }
    if target is None:
        return snapshot
    drops = _field(target, "timeBasedDrops", "time_based_drops", list)
    seen = set()
    for drop in drops:
        if not isinstance(drop, dict):
            raise WatchCheckError("drop_invalid")
        drop_id = _identifier(drop, "id", "drop_id")
        if drop_id not in target_drop_ids:
            continue
        if drop_id in seen:
            raise WatchCheckError("target_drop_duplicate")
        seen.add(drop_id)
        own = _field(drop, "self", "drop_self", dict)
        minutes = _minutes(own, "currentMinutesWatched", "drop_minutes")
        is_claimed = _field(own, "isClaimed", "drop_is_claimed", bool)
        required = _minutes(drop, "requiredMinutesWatched", "drop_required_minutes")
        snapshot["drops"].append({
            "drop_id": drop_id,
            "minutes": minutes,
            "required_minutes": required,
            "is_claimed": is_claimed,
        })
    return snapshot
