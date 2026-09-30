"""One source-derived web-query candidate for an already reconciled drop.

The original claim journal is required and preserved. This experiment changes
only the persisted claim query definition, keeping the validated WEB session.
It is not an integrity provider or a general claim retry mechanism.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from check_channel_watch import GQL, VALIDATE
from constants import GQL_QUERIES, GQLPersistedQuery
from finish_channel_drop import (
    DEFAULT_STATE, FinishClient, claim_and_confirm, read_inventory, reconcile_previous,
)
from web_claim_journal import WebClaimJournal
from watch_check_state import WatchCheckError


# Derived offline from the saved official web document, Apollo transformation
# and bundled printer. Its acceptance by Twitch has NOT been established.
WEB_CLAIM_HASH = "3b8a08f5a35dc95d7de229dea731a106a9aa9fa2e84c8f693fd159943f273e4f"
WEB_CLAIM_QUERY = GQLPersistedQuery("DropsPage_ClaimDropRewards", WEB_CLAIM_HASH)
MAX_SECONDS = 240
MAX_REQUESTS = 8


class WebClaimClient(FinishClient):
    claim_query = WEB_CLAIM_QUERY

    def __init__(self, report, proxy, clock, deadline, *, reconcile_only=False):
        super().__init__(report, proxy, clock, deadline, reconcile_only=reconcile_only)
        self.request_limit = 2 if reconcile_only else MAX_REQUESTS
        self._pending_wire_claim = None
        self._claim_http_used = False

    async def gql_request(self, operation):
        if operation == GQL_QUERIES["Inventory"]:
            return await super().gql_request(operation)
        if not isinstance(operation, dict) or operation.get("operationName") != WEB_CLAIM_QUERY["operationName"]:
            raise WatchCheckError("web_claim_operation_not_allowed")
        if self.reconcile_only or self._claim_http_used:
            raise WatchCheckError("web_claim_operation_not_allowed")
        self._pending_wire_claim = self.claim_query.with_variables(operation.get("variables", {}))
        try:
            # The parent still validates the exact original BaseDrop operation
            # and in-memory authorized server instance ID before any request.
            return await super().gql_request(operation)
        finally:
            self._pending_wire_claim = None

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        is_read = (
            (method == "GET" and str(url) == str(VALIDATE))
            or (method == "POST" and str(url) == str(GQL) and kwargs.get("json") == GQL_QUERIES["Inventory"])
        )
        if not is_read:
            if not (
                not self.reconcile_only and self._claim_used and not self._claim_http_used
                and method == "POST" and str(url) == str(GQL)
                and self._pending_wire_claim is not None
                and kwargs.get("json") == self._pending_wire_claim
            ):
                raise WatchCheckError("web_claim_request_not_allowed")
            self._claim_http_used = True
        async with super().request(method, url, **kwargs) as response:
            yield response


async def experiment(client, journal, original, campaign_name, prior_candidate):
    await client.open(client.cookie_file)
    if str(client._auth_state.user_id) != original["user_id"]:
        raise WatchCheckError("web_claim_original_account_mismatch")
    if prior_candidate is not None:
        await reconcile_previous(client, journal, prior_candidate, campaign_name, original["drop_id"])
        return
    target = await read_inventory(client, "candidate_preflight", campaign_name=campaign_name,
                                  drop_id=original["drop_id"])
    if target.campaign_id != original["campaign_id"]:
        raise WatchCheckError("web_claim_original_target_mismatch")
    client.report["target"] = target.public_dict()
    if target.is_claimed:
        client.report["state"] = "already_claimed"
        return
    if target.account_link_state is False:
        raise WatchCheckError("account_link_not_confirmed")
    target.require_claim_id()  # never wait, watch, or invent an instance ID
    # Includes fresh validation and Inventory, then a durable candidate record,
    # one original BaseDrop._claim call, and bounded read-only confirmation.
    await claim_and_confirm(client, journal, target)


async def check(cookie_file, campaign_name, proxy=None, state_dir=DEFAULT_STATE, *, clock=None):
    report = {
        "state": "failed", "mode": "web_query_claim_candidate", "cookie_loaded": False,
        "web_token_valid": False, "requests": [], "inventory_checks": [], "current_checks": [],
        "watch_sends": 0, "original_attempt_present": False,
        "claim": {"attempted": False, "previous_attempt": False, "confirmed": False},
        "query": {"operation_name": WEB_CLAIM_QUERY["operationName"],
                  "source": "saved_official_web_bundle_offline_derivation",
                  "candidate_sha256": WEB_CLAIM_HASH,
                  "original_sha256": GQL_QUERIES["ClaimDrop"]["extensions"]["persistedQuery"]["sha256Hash"]},
        "limits": {"total_seconds": MAX_SECONDS, "requests": MAX_REQUESTS},
    }
    if clock is None:
        clock = SimpleNamespace(time=asyncio.get_running_loop().time, sleep=asyncio.sleep)
    start, client = clock.time(), None
    try:
        with WebClaimJournal(state_dir) as journal:
            original = journal.original()
            if original["campaign_name"] != campaign_name:
                raise WatchCheckError("web_claim_original_target_mismatch")
            prior_candidate = journal.read()
            if prior_candidate is None and original["outcome"] != "attempted":
                raise WatchCheckError("web_claim_original_already_confirmed")
            report["original_attempt_present"] = True
            if prior_candidate is not None:
                report["mode"] = "web_query_claim_reconcile"
                report["limits"]["requests"] = 2
            client = WebClaimClient(report, proxy, clock, start + MAX_SECONDS,
                                    reconcile_only=prior_candidate is not None)
            client.cookie_file = cookie_file
            await asyncio.wait_for(
                experiment(client, journal, original, campaign_name, prior_candidate),
                timeout=client.remaining(),
            )
    except WatchCheckError as exc:
        report["error"] = exc.code
    except Exception as exc:
        report["error"] = type(exc).__name__  # exception text can expose credentials
    finally:
        report["phase"] = client.phase if client else "setup"
        report["elapsed_seconds"] = round(clock.time() - start, 3)
        if client is not None:
            await client.close()
    return (0 if report["state"] in {"claim_confirmed", "already_claimed"} else 1), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, default=Path("cookies.jar"))
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--proxy-env", help="Current proxy environment variable name")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    code, report = asyncio.run(check(args.cookies, args.campaign_name, proxy, args.state_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
