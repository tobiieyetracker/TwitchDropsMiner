"""Channel-scoped public campaign discovery: read-only candidate listing.

Coverage: channels only. This is NOT a full-campaign fix; the report always
states coverage="channels" and all_campaigns_verified=false.

Read sequence (fixed request budget, no retries, no concurrency):
  1. identity validation (id.twitch.tv/oauth2/validate)
  2. GetStreamInfo (channel id, stream state, broadcast game)
  3. AvailableDrops (public campaign candidates for that channel)
  4. at most one Inventory read, same-account verified, for ID diffing

Never: watch events, claims, Dashboard/Details operations, browser usage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import ssl
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from yarl import URL

import check_channel_watch as watch_probe
from constants import GQL_QUERIES
from twitch import Twitch
from watch_check_state import (
    WatchCheckError, check_envelope, summarize_challenge,
)

UNKNOWN = "unknown"

# Pure discovery: only the three read operations. Dashboard, Details,
# CurrentDrop, claims and anything else are rejected before any request.
CATALOG_READS = {GQL_QUERIES[key]["operationName"] for key in (
    "GetStreamInfo", "AvailableDrops", "Inventory",
)}


def _str_or_unknown(value):
    return value if isinstance(value, str) and value.strip() and len(value) <= 500 else UNKNOWN


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value) is not None


def _timestamp_or_unknown(value):
    if not isinstance(value, str) or len(value) > 50:
        return UNKNOWN
    try:
        return value if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo else UNKNOWN
    except ValueError:
        return UNKNOWN


def _field_state(parent, key, expected):
    if not isinstance(parent, dict):
        return "error", None
    if key not in parent:
        return "missing", None
    value = parent[key]
    if value is None:
        return "null", None
    if not isinstance(value, expected):
        return "error", None
    return "list" if expected is list else "object", value


def connection_diagnostic(error):
    """Bounded exception types/source locations, without messages or locals."""
    result, seen = [], set()
    while error is not None and id(error) not in seen and len(result) < 4:
        seen.add(id(error))
        frames, trace = [], error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            filename, function = Path(code.co_filename).name, code.co_name
            frames.append({
                "file": filename if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename) else "unknown",
                "function": function if re.fullmatch(r"[A-Za-z0-9_<>]{1,80}", function) else "unknown",
                "line": trace.tb_lineno,
            })
            trace = trace.tb_next
        result.append({"type": type(error).__name__, "frames": frames[-6:]})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return result


def validate_operation(operation):
    """Match the fixed persisted read, not just a caller-controlled name."""
    if not isinstance(operation, dict):
        raise WatchCheckError("operation_not_allowed")
    name, variables = operation.get("operationName"), operation.get("variables")
    if not isinstance(variables, dict):
        raise WatchCheckError("operation_not_allowed")
    if name == GQL_QUERIES["GetStreamInfo"]["operationName"]:
        key = "GetStreamInfo"
        valid = set(variables) == {"channel"} and isinstance(variables["channel"], str) and re.fullmatch(r"[A-Za-z0-9_]{1,25}", variables["channel"])
    elif name == GQL_QUERIES["AvailableDrops"]["operationName"]:
        key = "AvailableDrops"
        valid = set(variables) == {"channelID"} and isinstance(variables["channelID"], str) and variables["channelID"].isascii() and variables["channelID"].isdigit()
    elif name == GQL_QUERIES["Inventory"]["operationName"]:
        key = "Inventory"
        valid = set(variables) == {"fetchRewardCampaigns"} and variables["fetchRewardCampaigns"] is False
    else:
        raise WatchCheckError("operation_not_allowed")
    if not valid or operation != GQL_QUERIES[key].with_variables(variables):
        raise WatchCheckError("operation_not_allowed")


class CatalogClient(watch_probe.WatchClient):
    """WatchClient with a fixed budget and catalog-only operations."""

    REQUEST_BUDGET = 4

    def __init__(self, report: dict, proxy: str | None):
        super().__init__(report, proxy)
        self.request_limit = self.REQUEST_BUDGET
        # Construct after main installs system trust. aiohttp's cached context
        # can have been created before truststore was enabled.
        self._tls_context = ssl.create_default_context()

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        target = URL(url)
        if (method, str(target)) not in {
            ("GET", str(watch_probe.VALIDATE)), ("POST", str(watch_probe.GQL)),
        } or "params" in kwargs or "data" in kwargs:
            raise WatchCheckError("unexpected_endpoint")
        if method == "POST":
            validate_operation(kwargs.get("json"))
        elif "json" in kwargs:
            raise WatchCheckError("unexpected_request_body")
        kwargs["ssl"] = self._tls_context
        try:
            async with super().request(method, target, **kwargs) as response:
                yield response
        except Exception as error:
            self.report["request_failure"] = connection_diagnostic(error)
            raise

    async def gql_request(self, operation):
        validate_operation(operation)
        # Real miner header/transport builder, single attempt, no retry loop.
        body, _ = await Twitch._gql_request_once(self, operation)
        self.report["requests"][-1]["response_challenge"] = summarize_challenge(body)
        # Any populated challenge is a stop condition.
        check_envelope(body)
        # Deliberately NOT calling validate_campaign_response: a null/missing
        # viewerDropCampaigns is recorded as a state, not raised as an error.
        return body


def parse_stream_info(body):
    """Return (channel_id, stream_live, game{id,name}). Offline is observed."""
    state, data = _field_state(body, "data", dict)
    if state != "object":
        raise WatchCheckError("stream_data_" + state)
    state, user = _field_state(data, "user", dict)
    if state == "null":
        raise WatchCheckError("channel_not_found")
    if state != "object":
        raise WatchCheckError("stream_user_" + state)
    channel_id = user.get("id")
    if not isinstance(channel_id, str) or not channel_id.isascii() or not channel_id.isdigit():
        raise WatchCheckError("channel_id_invalid")
    state, stream = _field_state(user, "stream", dict)
    if state not in {"object", "null"}:
        raise WatchCheckError("stream_state_" + state)
    if state == "object" and not _identifier(stream.get("id")):
        raise WatchCheckError("stream_id_invalid")
    live = state == "object"
    settings = user.get("broadcastSettings")
    game = settings.get("game") if isinstance(settings, dict) else None
    game = game if isinstance(game, dict) else {}
    return channel_id, live, {
        "id": _str_or_unknown(game.get("id")),
        "name": _str_or_unknown(game.get("name")),
    }


def parse_candidate(item):
    """Public fields only. Missing fields stay unknown.

    Never fabricate campaign start/end times from drop times, never generate
    self.isAccountConnected=true, never build allow.channels or DropsCampaign
    objects here.
    """
    if not isinstance(item, dict):
        return None
    cid = item.get("id")
    if not _identifier(cid):
        return None
    game = item.get("game") if isinstance(item.get("game"), dict) else {}
    self_data = item.get("self") if isinstance(item.get("self"), dict) else {}
    linked = self_data.get("isAccountConnected")
    linked = linked if type(linked) is bool else UNKNOWN
    drops_state, raw_drops = _field_state(item, "timeBasedDrops", list)
    drops = [] if drops_state == "list" else None
    invalid_drops, seen = 0, set()
    if drops_state == "list":
        for drop in raw_drops:
            if not isinstance(drop, dict) or not _identifier(drop.get("id")):
                invalid_drops += 1
                continue
            did = drop["id"]
            if did in seen:
                invalid_drops += 1
                continue
            seen.add(did)
            minutes = drop.get("requiredMinutesWatched")
            minutes = minutes if type(minutes) is int and minutes >= 0 else UNKNOWN
            drops.append({
                "id": did,
                "name": _str_or_unknown(drop.get("name")),
                "required_minutes": minutes,
                "starts_at": _timestamp_or_unknown(drop.get("startAt")),
                "ends_at": _timestamp_or_unknown(drop.get("endAt")),
            })
    return {
        "id": cid,
        "name": _str_or_unknown(item.get("name")),
        "game": {
            "id": _str_or_unknown(game.get("id")),
            "name": _str_or_unknown(game.get("name")),
        },
        # Campaign-level times only; unknown is not empty and is not derived.
        "starts_at": _timestamp_or_unknown(item.get("startAt")),
        "ends_at": _timestamp_or_unknown(item.get("endAt")),
        "account_link_state": linked,
        "drops": drops,
        "drops_state": drops_state,
        "drop_count": len(raw_drops) if drops_state == "list" else None,
        "invalid_drop_entries": invalid_drops if drops_state == "list" else None,
        "source": "AvailableDrops",
    }


def parse_catalog(body, channel_id):
    """Return (campaigns_state, raw_count, candidates, duplicate_count, invalid_count)."""
    for key in ("data", "channel"):
        state, body = _field_state(body, key, dict)
        if state != "object":
            return state, None, None, None, None
    channel = body
    if channel.get("id") != channel_id:
        raise WatchCheckError("available_channel_mismatch")
    state, campaigns = _field_state(channel, "viewerDropCampaigns", list)
    if state != "list":
        return state, None, None, None, None
    candidates, seen, duplicates, invalid = [], set(), 0, 0
    for item in campaigns:
        candidate = parse_candidate(item)
        if candidate is None:
            invalid += 1
            continue
        if candidate["drops_state"] != "list" or candidate["invalid_drop_entries"]:
            invalid += 1
        if candidate["id"] in seen:
            duplicates += 1
            continue
        seen.add(candidate["id"])
        candidates.append(candidate)
    return "list", len(campaigns), candidates, duplicates, invalid


def parse_inventory(body, user_id, catalog_ids):
    """Return (state, same_account, in_progress_ids, comparable, new_ids).

    Candidate and inventory user-state are kept separate; no DropsCampaign is
    constructed. A null inventory means the diff is unknown, not empty.
    """
    for key in ("data", "currentUser"):
        state, body = _field_state(body, key, dict)
        if state != "object":
            return state, UNKNOWN, None, False, None
    user = body
    same_account = UNKNOWN
    if "id" not in user:
        return "missing", UNKNOWN, None, False, None
    returned = user["id"]
    if not isinstance(returned, str) or not returned.isascii() or not returned.isdigit():
        return "error", UNKNOWN, None, False, None
    if returned != user_id:
        raise WatchCheckError("inventory_account_mismatch")
    same_account = True
    state, inventory = _field_state(user, "inventory", dict)
    if state != "object":
        return state, same_account, None, False, None
    state, campaigns = _field_state(inventory, "dropCampaignsInProgress", list)
    if state != "list":
        return state, same_account, None, False, None
    ids = []
    for campaign in campaigns:
        if not isinstance(campaign, dict) or not _identifier(campaign.get("id")):
            return "error", same_account, None, False, None
        ids.append(campaign["id"])
    if len(ids) != len(set(ids)):
        return "error", same_account, None, False, None
    return "list", same_account, ids, True, sorted(set(catalog_ids) - set(ids))


async def check(cookie_file: Path, channel_login: str, proxy: str | None = None):
    report = {
        "state": "failed", "mode": "channel_catalog",
        "script": "check_channel_catalog.py",
        "coverage": "channels", "all_campaigns_verified": False,
        "coverage_note": ("Channel-scoped candidate discovery only; "
                          "not a full-campaign fix."),
        "channel_login": None,
        "cookie_loaded": False, "web_token_valid": False,
        "campaigns_state": None, "campaign_count": None,
        "unique_campaign_count": None, "observed_unique_campaign_count": None,
        "duplicate_campaigns": None, "invalid_campaign_entries": None, "candidates": None,
        "inventory": None, "new_vs_inventory": {
            "comparable": False, "new_ids": None, "new_count": None,
            "new_sample_names": None,
        },
        "requests": [], "rate_limits": [],
        "request_budget": CatalogClient.REQUEST_BUDGET,
    }
    client = CatalogClient(report, proxy)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,25}", channel_login):
            raise WatchCheckError("invalid_channel_login")
        report["channel_login"] = channel_login.lower()
        await client.open(cookie_file)  # identity validated inside
        user_id = str(client._auth_state.user_id)
        client.phase = "stream_info"
        info = await client.gql_request(
            GQL_QUERIES["GetStreamInfo"].with_variables({"channel": channel_login.lower()}))
        channel_id, live, game = parse_stream_info(info)
        report["channel"] = {
            "id": channel_id, "login": channel_login.lower(),
            "stream_live": live, "stream_game": game,
        }
        client.phase = "available_drops"
        available = await client.gql_request(
            GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel_id)}))
        campaigns_state, raw_count, candidates, dups, invalid = parse_catalog(
            available, channel_id)
        report.update(
            campaigns_state=campaigns_state, campaign_count=raw_count,
            unique_campaign_count=len(candidates) if candidates is not None and not invalid else None,
            observed_unique_campaign_count=len(candidates) if candidates is not None else None,
            duplicate_campaigns=dups,
            invalid_campaign_entries=invalid, candidates=candidates)
        if campaigns_state != "list":
            raise WatchCheckError("catalog_" + campaigns_state)
        if invalid:
            raise WatchCheckError("catalog_entries_invalid")
        catalog_ids = [c["id"] for c in candidates]
        # At most one same-account Inventory read.
        client.phase = "inventory"
        inventory_body = await client.gql_request(GQL_QUERIES["Inventory"])
        inv_state, same_account, inv_ids, comparable, new_ids = parse_inventory(
            inventory_body, user_id, catalog_ids)
        names = {c["id"]: c["name"] for c in candidates}
        report["inventory"] = {
            "state": inv_state, "same_account": same_account,
            "in_progress_count": len(inv_ids) if inv_ids is not None else None,
            "in_progress_ids": inv_ids,
        }
        if inv_state != "list" or not comparable:
            raise WatchCheckError("inventory_" + inv_state)
        report["new_vs_inventory"] = {
            "comparable": comparable, "new_ids": new_ids,
            "new_count": len(new_ids),
            "new_sample_names": [names.get(i, UNKNOWN) for i in new_ids[:3]],
        }
        report["state"] = "passed"
    except WatchCheckError as exc:
        report["error"] = exc.code  # codes only; no credential/URL text
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        report["phase"] = client.phase
        report["rate_limits"] = [
            r for r in report["requests"] if r.get("http_status") == 429]
        try:
            await client.close()
        except Exception as exc:
            report["state"] = "failed"
            report["cleanup_error"] = type(exc).__name__
    return (0 if report["state"] == "passed" else 1), report


def _assert(cond, label):
    if not cond:
        raise AssertionError(label)


def self_check():
    """Offline verification. No network, no cookies, no Twitch requests."""
    results = []

    def case(label, fn):
        fn()
        results.append(label)

    # 1. list with duplicates: dedupe, keep first, source preserved
    def t_dedup():
        body = {"data": {"channel": {"id": "1", "viewerDropCampaigns": [
            {"id": "c1", "name": "Alpha", "game": {"id": "g1", "name": "G1"},
             "startAt": "2026-01-01T00:00:00Z", "endAt": "2026-02-01T00:00:00Z",
             "self": {"isAccountConnected": True},
             "timeBasedDrops": [{"id": "d1", "name": "Drop1",
                                 "requiredMinutesWatched": 60,
                                 "startAt": "2026-01-01T00:00:00Z",
                                 "endAt": "2026-02-01T00:00:00Z"}]},
            {"id": "c1", "name": "Alpha-dup", "game": {"id": "g1", "name": "G1"},
             "timeBasedDrops": []},
            {"id": "c2", "name": "Beta", "timeBasedDrops": "nope"},
        ]}}}
        state, raw, cands, dups, invalid = parse_catalog(body, "1")
        _assert(state == "list", "dedup state")
        _assert(raw == 3 and len(cands) == 2, "dedup counts")
        _assert(dups == 1 and invalid == 1, "dup/invalid counts")
        _assert(cands[0]["name"] == "Alpha", "keep first")
        _assert(cands[0]["source"] == "AvailableDrops", "source kept")
        _assert(cands[0]["account_link_state"] is True, "observed link bool kept")
        _assert(cands[1]["drops"] is None and cands[1]["drops_state"] == "error",
                "non-list drops remain unknown")
    case("dedup_keeps_first_and_source", t_dedup)

    # 2. null campaigns -> state null, not empty, not error
    def t_null():
        body = {"data": {"channel": {"id": "1", "viewerDropCampaigns": None}}}
        state, raw, cands, _, _ = parse_catalog(body, "1")
        _assert(state == "null" and raw is None and cands is None, "null state")
    case("null_is_not_empty", t_null)

    # 3. missing key -> state missing
    def t_missing():
        for b in ({"data": {}}, {"data": {"channel": {"id": "1"}}},
                  {"data": {"channel": None}}, {"other": 1}):
            state, _, cands, _, _ = parse_catalog(b, "1")
            _assert(state in ("missing", "null") and cands is None, "missing or null")
    case("missing_key_is_missing", t_missing)

    # 4. unknown is not empty; no fabricated self/ACL; no fabricated times
    def t_unknown():
        body = {"data": {"channel": {"id": "1", "viewerDropCampaigns": [
            {"id": "c9",
             "timeBasedDrops": [{"id": "d9", "requiredMinutesWatched": 0,
                                "startAt": "2026-01-01T00:00:00Z",
                                "endAt": "2026-02-01T00:00:00Z"}]},
        ]}}}
        _, _, cands, _, _ = parse_catalog(body, "1")
        c = cands[0]
        _assert(c["name"] == UNKNOWN, "name unknown")
        _assert(c["game"]["id"] == UNKNOWN, "game unknown")
        _assert(c["account_link_state"] == UNKNOWN, "no fabricated self.isAccountConnected")
        _assert(c["starts_at"] == UNKNOWN and c["ends_at"] == UNKNOWN,
                "campaign times not derived from drops")
        _assert(c["drops"][0]["required_minutes"] == 0, "zero minutes kept as 0")
        _assert("allow" not in json.dumps(c).lower(), "no ACL fabrications")
    case("unknown_not_empty_no_fabrication", t_unknown)

    # 5. invalid entries skipped, not crashed on
    def t_invalid():
        body = {"data": {"channel": {"id": "1", "viewerDropCampaigns": [
            None, "x", {"id": ""}, {"id": "ok", "name": "Fine", "timeBasedDrops": []},
        ]}}}
        state, raw, cands, _, invalid = parse_catalog(body, "1")
        _assert(state == "list" and len(cands) == 1 and invalid == 3, "invalid skipped")
    case("invalid_entries_skipped", t_invalid)

    # 6. offline streamer: observed, still parseable; null broadcastSettings
    def t_offline():
        cid, live, game = parse_stream_info(
            {"data": {"user": {"id": "42", "stream": None, "broadcastSettings": None}}})
        _assert(cid == "42" and live is False, "offline observed")
        _assert(game["id"] == UNKNOWN, "game unknown when offline")
    case("offline_stream_recorded", t_offline)

    # 7. channel not found -> hard stop code
    def t_not_found():
        try:
            parse_stream_info({"data": {"user": None}})
            raise SystemExit("should have raised")
        except WatchCheckError as exc:
            _assert(exc.code == "channel_not_found", "not found code")
    case("channel_not_found_stops", t_not_found)

    # 8. inventory identity mismatch -> stop
    def t_mismatch():
        body = {"data": {"currentUser": {"id": "999",
                                        "inventory": {"dropCampaignsInProgress": []}}}}
        try:
            parse_inventory(body, "123", [])
            raise SystemExit("should have raised")
        except WatchCheckError as exc:
            _assert(exc.code == "inventory_account_mismatch", "mismatch code")
    case("inventory_identity_mismatch_stops", t_mismatch)

    # 9. inventory null -> diff unknown, not empty
    def t_inv_null():
        for key, val in (("currentUser", None), ("inventory", None),
                         ("dropCampaignsInProgress", None)):
            if key == "currentUser":
                body = {"data": {"currentUser": None}}
            elif key == "inventory":
                body = {"data": {"currentUser": {"id": "7", "inventory": None}}}
            else:
                body = {"data": {"currentUser": {"id": "7",
                              "inventory": {"dropCampaignsInProgress": None}}}}
            state, same, ids, comparable, new_ids = parse_inventory(body, "7", ["c1"])
            _assert(state == "null" and comparable is False and new_ids is None,
                    f"inventory null {key}")
    case("inventory_null_diff_unknown", t_inv_null)

    # 10. diff only when same account verified
    def t_diff():
        body = {"data": {"currentUser": {"id": "7", "inventory": {
            "dropCampaignsInProgress": [{"id": "c1"}, {"id": "c3"}]}}}}
        state, same, ids, comparable, new_ids = parse_inventory(body, "7", ["c1", "c2"])
        _assert(state == "list" and same is True, "same account")
        _assert(comparable and new_ids == ["c2"], "new ids vs inventory")
        body2 = {"data": {"currentUser": {"inventory": {
            "dropCampaignsInProgress": [{"id": "c1"}]}}}}
        _, same2, _, comparable2, new2 = parse_inventory(body2, "7", ["c1", "c2"])
        _assert(same2 == UNKNOWN and comparable2 is False and new2 is None,
                "no id -> not comparable")
    case("diff_only_when_same_account", t_diff)

    # 11. operation allowlist rejects everything outside the three reads
    async def t_allowlist():
        client = CatalogClient({"requests": []}, None)
        for op in ("ViewerDropsDashboard", "DropsCampaignDetails",
                   "DropCurrentSessionContext", "ClaimDrop", "SendWatch"):
            try:
                await client.gql_request({"operationName": op})
                raise SystemExit(f"allowed {op}")
            except WatchCheckError as exc:
                _assert(exc.code == "operation_not_allowed", f"reject {op}")
        _assert(client.request_limit == 4, "budget is fixed at 4")
    case("allowlist_rejects_non_catalog_ops", lambda: asyncio.run(t_allowlist()))

    # 12. pure discovery: no watch/claim/dashboard/details code paths.
    # Scan only the implementation (before self_check); the test's own
    # string literals below must not count as code paths.
    def t_pure():
        src = Path(__file__).read_text().split("def self_check", 1)[0]
        for token in ("send_watch", "ClaimDrop", "ViewerDropsDashboard",
                      "DropsCampaignDetails", "DropCurrentSessionContext",
                      "PlaybackAccessToken", "allow_channels", "DropsCampaign("):
            _assert(token not in src, f"forbidden token {token}")
    case("pure_discovery_no_watch_claim_dashboard", t_pure)

    return {"self_check": "passed", "cases": results, "case_count": len(results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", type=Path, default=Path("cookies.jar"))
    parser.add_argument("--channel-login", default="hJune")
    parser.add_argument("--proxy-env", help="Name of the current proxy environment variable")
    parser.add_argument("--self-check", action="store_true",
                        help="Run offline assertions only; no network, no cookies")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.self_check:
        try:
            print(json.dumps(self_check(), ensure_ascii=False, indent=2))
            return 0
        except (AssertionError, SystemExit) as exc:
            print(json.dumps({"self_check": "failed", "error": str(exc)}))
            return 1
    # Fresh verified SSL, same approach as the earlier successful probes.
    import truststore
    truststore.inject_into_ssl()
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    try:
        code, report = asyncio.run(asyncio.wait_for(
            check(args.cookie_file, args.channel_login, proxy), timeout=240))
    except asyncio.TimeoutError:
        print(json.dumps({"state": "failed", "error": "probe_timeout"}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
