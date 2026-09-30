"""Bounded matching-SMARTBOX claim experiment; read-only unless --claim is set.

Uses an existing SMARTBOX-issued session and the original miner claim query.
No browser, integrity provider, watching, automatic login or mutation retries.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from check_channel_watch import GQL, VALIDATE
from constants import ClientType, GQL_QUERIES
from finish_channel_drop import (
    DEFAULT_STATE, FinishClient, claim_and_confirm, read_inventory, reconcile_previous,
)
from smartbox_claim_journal import SmartboxClaimJournal
from watch_check_state import WatchCheckError


MAX_SECONDS = 240
MAX_REQUESTS = 8


class SmartboxClaimClient(FinishClient):
    AUTH_CLIENT_TYPE = ClientType.SMARTBOX
    COOKIE_ORIGIN = ClientType.SMARTBOX.CLIENT_URL
    IDENTITY_LABEL = "smartbox"
    INVALID_TOKEN_ERROR = "not_a_valid_smartbox_token"
    MISSING_TOKEN_ERROR = "cookie_has_no_smartbox_auth_token"

    def __init__(self, report, proxy, clock, deadline, *, reconcile_only=False):
        super().__init__(report, proxy, clock, deadline, reconcile_only=reconcile_only)
        self.request_limit = 2 if reconcile_only else MAX_REQUESTS
        self._pending_wire_claim = None
        self._claim_http_used = False
        # main installs system trust first. Avoid aiohttp's context cached before
        # that initialization; verification stays enabled for every request.
        self._tls_context = ssl.create_default_context()

    async def gql_request(self, operation):
        if operation == GQL_QUERIES["Inventory"]:
            return await super().gql_request(operation)
        if (self.reconcile_only or self._claim_http_used
                or not isinstance(operation, dict)
                or operation.get("operationName") != self.claim_query["operationName"]):
            raise WatchCheckError("smartbox_claim_operation_not_allowed")
        self._pending_wire_claim = operation
        try:
            # FinishClient checks the complete original operation and authorized
            # fresh server instance, consuming that authorization before await.
            return await super().gql_request(operation)
        finally:
            self._pending_wire_claim = None

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        if any(key in kwargs for key in ("data", "params", "auth", "cookies")):
            raise WatchCheckError("smartbox_claim_request_not_allowed")
        is_validation = method == "GET" and str(url) == str(VALIDATE) and "json" not in kwargs
        is_gql = method == "POST" and str(url) == str(GQL)
        is_inventory = is_gql and kwargs.get("json") == GQL_QUERIES["Inventory"]
        is_claim = (
            is_gql and not self.reconcile_only and self._claim_used
            and not self._claim_http_used and self._pending_wire_claim is not None
            and kwargs.get("json") == self._pending_wire_claim
        )
        if not (is_validation or is_inventory or is_claim):
            raise WatchCheckError("smartbox_claim_request_not_allowed")
        headers = kwargs.get("headers", {})
        if headers.get("Authorization") != f"OAuth {self.token}" or "Client-Integrity" in headers:
            raise WatchCheckError("smartbox_claim_identity_headers_invalid")
        if is_gql and any(headers.get(key) != value for key, value in {
            "Client-Id": self.AUTH_CLIENT_TYPE.CLIENT_ID,
            "User-Agent": self.AUTH_CLIENT_TYPE.USER_AGENT,
            "Origin": str(self.COOKIE_ORIGIN), "Referer": str(self.COOKIE_ORIGIN),
        }.items()):
            raise WatchCheckError("smartbox_claim_identity_headers_invalid")
        if is_claim:
            self._claim_http_used = True
        kwargs["ssl"] = self._tls_context
        async with super().request(method, url, **kwargs) as response:
            yield response


async def experiment(client, journal, original, campaign_name, prior_candidate, *, claim=False):
    await client.open(client.cookie_file)
    if str(client._auth_state.user_id) != original["user_id"]:
        raise WatchCheckError("smartbox_claim_original_account_mismatch")
    if prior_candidate is not None:
        await reconcile_previous(client, journal, prior_candidate, campaign_name, original["drop_id"])
        return
    target = await read_inventory(client, "candidate_preflight", campaign_name=campaign_name,
                                  drop_id=original["drop_id"])
    if target.campaign_id != original["campaign_id"]:
        raise WatchCheckError("smartbox_claim_original_target_mismatch")
    client.report["target"] = target.public_dict()
    if target.is_claimed:
        client.report["state"] = "already_claimed"
        return
    if target.account_link_state is False:
        raise WatchCheckError("account_link_not_confirmed")
    target.require_claim_id()
    if not claim:
        client.report["state"] = "preflight_ready"
        return
    # Same miner method, fresh validation + Inventory, durable attempt record,
    # one mutation, and same-target Inventory confirmation. No hash substitution.
    await claim_and_confirm(client, journal, target)


async def check(cookie_file, campaign_name, proxy=None, state_dir=DEFAULT_STATE, *, clock=None, claim=False):
    report = {
        "state": "failed", "mode": "smartbox_claim_candidate" if claim else "smartbox_claim_preflight",
        "cookie_loaded": False, "smartbox_token_valid": False,
        "requests": [], "inventory_checks": [], "current_checks": [], "watch_sends": 0,
        "original_attempt_present": False,
        "claim": {"attempted": False, "previous_attempt": False, "confirmed": False},
        "query": {"operation_name": GQL_QUERIES["ClaimDrop"]["operationName"],
                  "sha256": GQL_QUERIES["ClaimDrop"]["extensions"]["persistedQuery"]["sha256Hash"],
                  "source": "original_miner_claim_query", "identity": "matching_smartbox"},
        "limits": {"total_seconds": MAX_SECONDS, "requests": MAX_REQUESTS if claim else 2},
    }
    if clock is None:
        clock = SimpleNamespace(time=asyncio.get_running_loop().time, sleep=asyncio.sleep)
    start, client = clock.time(), None
    try:
        with SmartboxClaimJournal(state_dir) as journal:
            original = journal.original()
            if original["campaign_name"] != campaign_name:
                raise WatchCheckError("smartbox_claim_original_target_mismatch")
            prior_candidate = journal.read()
            if prior_candidate is None and original["outcome"] != "attempted":
                raise WatchCheckError("smartbox_claim_original_already_confirmed")
            report["original_attempt_present"] = True
            read_only = prior_candidate is not None or not claim
            if prior_candidate is not None:
                report["mode"] = "smartbox_claim_reconcile"
            report["limits"]["requests"] = 2 if read_only else MAX_REQUESTS
            client = SmartboxClaimClient(report, proxy, clock, start + MAX_SECONDS,
                                        reconcile_only=read_only)
            client.cookie_file = cookie_file
            await asyncio.wait_for(
                experiment(client, journal, original, campaign_name, prior_candidate, claim=claim),
                timeout=client.remaining(),
            )
    except WatchCheckError as exc:
        report["error"] = exc.code
    except Exception as exc:
        report["error"] = type(exc).__name__  # never include credential-bearing exception text
    finally:
        report["phase"] = client.phase if client else "setup"
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except Exception as exc:
                report["cleanup_error"] = type(exc).__name__
        report["elapsed_seconds"] = round(clock.time() - start, 3)
    successful = report["state"] in {"preflight_ready", "claim_confirmed", "already_claimed"}
    if "error" in report or "cleanup_error" in report:
        if successful:
            report["state"] = "failed"
        successful = False
    return (0 if successful else 1), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, required=True, help="Existing SMARTBOX CookieJar (read-only)")
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--proxy-env", help="Current proxy environment variable name")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--claim", action="store_true", help="Allow one fixed candidate; existing attempt only reconciles")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    import truststore
    truststore.inject_into_ssl()
    code, report = asyncio.run(check(args.cookies, args.campaign_name, proxy, args.state_dir, claim=args.claim))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
