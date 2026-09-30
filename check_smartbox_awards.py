"""Read-only reward-grant reconciliation for the existing SMARTBOX attempt.

Exactly token validation and the original Inventory query are allowed. Public
third-party metadata supplies benefit IDs, not account eligibility or proof that
this particular mutation caused the grant. No claim journal is changed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from check_smartbox_claim import SmartboxClaimClient
from constants import GQL_QUERIES
from finish_channel_drop import DEFAULT_STATE, inventory_identity
from smartbox_claim_journal import SmartboxClaimJournal
from watch_check_state import WatchCheckError, snapshot_inventory


MAX_SECONDS = 240
METADATA_PATH = Path(__file__).resolve().parent / "docs" / "campaign-discovery" / "rust-isles-ar-benefit.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value, code: str) -> datetime:
    if (type(value) is not str or len(value) > 64 or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value)):
        raise WatchCheckError(code)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise WatchCheckError(code) from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise WatchCheckError(code)
    return stamp.astimezone(timezone.utc)


def load_metadata(path: Path = METADATA_PATH) -> dict:
    # Only a repository-owned fixed file is accepted by the CLI; no URL, query,
    # target or instance-ID argument can broaden this two-request check.
    try:
        if path.stat().st_size > 32 * 1024:
            raise WatchCheckError("award_metadata_invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise WatchCheckError("award_metadata_unavailable") from None
    if type(value) is not dict:
        raise WatchCheckError("award_metadata_invalid")
    for key in ("campaign_id", "campaign_name", "drop_id"):
        if type(value.get(key)) is not str or not value[key].strip():
            raise WatchCheckError("award_metadata_invalid")
    ids = value.get("benefit_ids")
    if (type(ids) is not list or not ids or len(ids) > 100
            or any(type(item) is not str or not item.strip() for item in ids)
            or len(set(ids)) != len(ids)):
        raise WatchCheckError("award_metadata_invalid")
    return {"campaign_id": value["campaign_id"], "campaign_name": value["campaign_name"],
            "drop_id": value["drop_id"],
            "benefit_ids": list(ids)}


def field_state(parent, name, expected_type):
    if type(parent) is not dict:
        return "invalid", None
    if name not in parent:
        return "missing", None
    value = parent[name]
    if value is None:
        return "null", None
    if type(value) is not expected_type:
        return "invalid", None
    return "list" if expected_type is list else "object", value


def parse_awards(inventory, benefit_ids: list[str], attempted_at: datetime, checked_at: datetime) -> dict:
    """Match only target benefit IDs; do not emit other rewards or assume IDs.

The original miner's gameEventDrops[].id denotes the benefit. For the site's
connection shape only node.benefit.id is accepted; node.id is never substituted.
Missing pages or fields leave the grant unconfirmed, not absent or unclaimed.
"""
    result = {"source": None, "state": "missing", "expected_benefit_count": len(benefit_ids),
              "matched_benefits": None, "all_benefits_present": None,
              "all_awards_in_window": None, "has_more": None}
    if "gameEventDrops" in inventory:
        result["source"] = "Inventory.gameEventDrops"
        state, entries = field_state(inventory, "gameEventDrops", list)
        connection = False
    else:
        result["source"] = "Inventory.gameEventDropsConnection"
        state, conn = field_state(inventory, "gameEventDropsConnection", dict)
        if state != "object":
            result["state"] = state
            return result
        state, entries = field_state(conn, "edges", list)
        page_info = conn.get("pageInfo")
        if type(page_info) is dict and type(page_info.get("hasNextPage")) is bool:
            result["has_more"] = page_info["hasNextPage"]
        connection = True
    result["state"] = state
    if state != "list":
        return result
    expected, matches = set(benefit_ids), {}
    for entry in entries:
        if type(entry) is not dict:
            raise WatchCheckError("award_entry_invalid")
        if connection:
            state, node = field_state(entry, "node", dict)
            if state != "object":
                raise WatchCheckError("award_node_" + state)
            state, benefit = field_state(node, "benefit", dict)
            if state != "object":
                raise WatchCheckError("award_benefit_" + state)
            benefit_id = benefit.get("id")
        else:
            node, benefit_id = entry, entry.get("id")
        if type(benefit_id) is not str or not benefit_id.strip():
            raise WatchCheckError("award_benefit_id_invalid")
        if benefit_id not in expected:
            continue
        if benefit_id in matches:
            raise WatchCheckError("award_target_duplicate")
        awarded_at = parse_timestamp(node.get("lastAwardedAt"), "award_timestamp_invalid")
        matches[benefit_id] = {"benefit_id": benefit_id, "last_awarded_at": awarded_at.isoformat(),
                               "within_attempt_window": attempted_at <= awarded_at <= checked_at}
    result["matched_benefits"] = [matches[bid] for bid in benefit_ids if bid in matches]
    result["all_benefits_present"] = set(matches) == expected
    result["all_awards_in_window"] = (
        result["all_benefits_present"] and all(item["within_attempt_window"] for item in matches.values())
    )
    return result


async def experiment(client, original, prior, metadata):
    await client.open(client.cookie_file)
    user_id = str(client._auth_state.user_id)
    if user_id != original["user_id"] or user_id != prior["user_id"]:
        raise WatchCheckError("smartbox_awards_account_mismatch")
    client.report["same_account"] = True
    client.phase = "award_inventory"
    body = await client.gql_request(GQL_QUERIES["Inventory"])
    inventory_identity(body, user_id)
    checked_at = utc_now()
    attempted_at = parse_timestamp(prior["attempted_at"], "award_attempt_timestamp_invalid")
    if checked_at < attempted_at:
        raise WatchCheckError("award_check_precedes_attempt")
    client.report["checked_at"] = checked_at.isoformat()
    client.report["attempted_at"] = attempted_at.isoformat()
    client.report["state"] = "reward_grant_unconfirmed"
    try:
        client.report["inventory_target"] = snapshot_inventory(
            body, user_id, metadata["campaign_id"], {metadata["drop_id"]})
    except WatchCheckError as error:
        # In-progress state and grants are distinct server fields. Missing or
        # malformed progress is not an empty list and does not erase grants.
        client.report["inventory_target_error"] = error.code
    state, inventory = field_state(body["data"]["currentUser"], "inventory", dict)
    if state != "object":
        client.report["awards"] = {"source": "Inventory", "state": state,
                                   "matched_benefits": None, "all_benefits_present": None,
                                   "all_awards_in_window": None}
        return
    summary = parse_awards(inventory, metadata["benefit_ids"], attempted_at, checked_at)
    client.report["awards"] = summary
    if summary["all_benefits_present"] is True and summary["all_awards_in_window"] is True:
        client.report["state"] = "reward_grant_observed"


async def check(cookie_file, proxy=None, state_dir=DEFAULT_STATE, *, clock=None):
    report = {"state": "failed", "mode": "smartbox_awards_reconcile", "read_only": True,
              "cookie_loaded": False, "smartbox_token_valid": False, "same_account": None,
              "requests": [], "watch_sends": 0, "claim_attempted": False,
              "journal_modified": False, "inventory_target": None, "awards": None,
              "evidence_limits": {"benefit_mapping_source": "fixed_public_third_party_snapshot",
                                  "pre_claim_awards_baseline": False,
                                  "unique_mutation_attribution": False,
                                  "claim_isClaimed_confirmation": False},
              "limits": {"total_seconds": MAX_SECONDS, "requests": 2}}
    if clock is None:
        clock = SimpleNamespace(time=asyncio.get_running_loop().time, sleep=asyncio.sleep)
    start, client = clock.time(), None
    try:
        metadata = load_metadata()
        with SmartboxClaimJournal(state_dir) as journal:
            original, prior = journal.original(), journal.read()
            if prior is None:
                raise WatchCheckError("smartbox_awards_attempt_missing")
            for record in (original, prior):
                if any(record[key] != metadata[key] for key in ("campaign_id", "campaign_name", "drop_id")):
                    raise WatchCheckError("smartbox_awards_target_mismatch")
            report["target"] = metadata
            client = SmartboxClaimClient(report, proxy, clock, start + MAX_SECONDS, reconcile_only=True)
            client.cookie_file = cookie_file
            await asyncio.wait_for(experiment(client, original, prior, metadata), timeout=client.remaining())
    except WatchCheckError as error:
        report["error"] = error.code
    except Exception as error:
        report["error"] = type(error).__name__
    finally:
        report["phase"] = client.phase if client else "setup"
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except Exception as error:
                report["cleanup_error"] = type(error).__name__
        report["elapsed_seconds"] = round(clock.time() - start, 3)
    success = report["state"] == "reward_grant_observed" and "error" not in report and "cleanup_error" not in report
    if not success and report["state"] == "reward_grant_observed":
        report["state"] = "reward_grant_unconfirmed"
    return (0 if success else 1), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, required=True, help="Existing SMARTBOX CookieJar (read-only)")
    parser.add_argument("--proxy-env", help="Current proxy environment variable name")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    import truststore
    truststore.inject_into_ssl()
    code, report = asyncio.run(check(args.cookies, proxy, args.state_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
