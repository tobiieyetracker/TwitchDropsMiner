"""Resume one server-known drop, attempt one claim, and verify Inventory.

This bounded Muse diagnostic uses the proven Python session and original miner
watch/claim methods. It neither discovers all campaigns nor installs a daemon.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import aiohttp

from channel import Channel, Stream
from check_channel_watch import GQL, VALIDATE, WatchClient, WatchWindowEnded, stream_user, target_campaign
from constants import GQL_QUERIES, WATCH_INTERVAL
from finish_drop_journal import FinishJournal
from finish_drop_state import refresh_target, select_target
from inventory import BaseDrop
from twitch import Twitch
from watch_check_state import (
    WatchCheckError, check_envelope, snapshot_current, snapshot_inventory, summarize_challenge,
)


MAX_SECONDS = 5400
MAX_REQUESTS = 180
CHECK_INTERVAL = 300
STALL_SECONDS = 900
FINISH_RESERVE = 120
CONFIRM_INTERVAL = 15
DEFAULT_STATE = Path.home() / ".local" / "state" / "twitchdropsminer" / "finish-drop"


class FinishClient(WatchClient):
    claim_query = GQL_QUERIES["ClaimDrop"]

    def __init__(self, report, proxy, clock, deadline, *, reconcile_only=False):
        super().__init__(report, proxy)
        self.clock, self.deadline = clock, deadline
        self.reconcile_only = reconcile_only
        self.request_limit = 2 if reconcile_only else MAX_REQUESTS
        self._claim_authorization = None
        self._claim_used = False

    def remaining(self):
        remaining = self.deadline - self.clock.time()
        if remaining <= 0:
            raise WatchCheckError("total_time_exhausted")
        return remaining

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        if self.reconcile_only and not (
            (method == "GET" and str(url) == str(VALIDATE))
            or (method == "POST" and str(url) == str(GQL) and kwargs.get("json") == GQL_QUERIES["Inventory"])
        ):
            raise WatchCheckError("reconcile_request_not_allowed")
        kwargs["timeout"] = aiohttp.ClientTimeout(total=min(20, self.remaining()))
        async with super().request(method, url, **kwargs) as response:
            yield response
        # Response-body parsing is part of the budget, too.
        self.remaining()

    async def gql_request(self, operation):
        if self.reconcile_only and operation != GQL_QUERIES["Inventory"]:
            raise WatchCheckError("reconcile_operation_not_allowed")
        if not isinstance(operation, dict) or operation.get("operationName") != GQL_QUERIES["ClaimDrop"]["operationName"]:
            return await super().gql_request(operation)
        expected = GQL_QUERIES["ClaimDrop"].with_variables(
            {"input": {"dropInstanceID": self._claim_authorization}}
        )
        if self._claim_used or self._claim_authorization is None or operation != expected:
            raise WatchCheckError("claim_not_authorized")
        wire_operation = self.claim_query.with_variables(
            {"input": {"dropInstanceID": self._claim_authorization}}
        )
        # Consume before the first await. Neither transport nor _claim retries.
        self._claim_used = True
        self._claim_authorization = None
        body, _ = await Twitch._gql_request_once(self, wire_operation)
        self.report["claim"]["response_challenge"] = summarize_challenge(body)
        self.report["requests"][-1]["response_challenge"] = self.report["claim"]["response_challenge"]
        check_envelope(body)
        return body


def inventory_identity(body, user_id):
    """Mutation-capable runs require an explicitly matching Inventory user ID."""
    check_envelope(body)
    data = body.get("data")
    user = data.get("currentUser") if isinstance(data, dict) else None
    if not isinstance(user, dict) or user.get("id") != user_id:
        raise WatchCheckError("inventory_identity_not_confirmed")


async def read_inventory(client, label, *, target=None, campaign_name=None, drop_id=None):
    client.phase = label
    body = await client.gql_request(GQL_QUERIES["Inventory"])
    user_id = str(client._auth_state.user_id)
    inventory_identity(body, user_id)
    result = (refresh_target(body, user_id, target) if target is not None
              else select_target(body, user_id, campaign_name, drop_id))
    reference = result or target
    client.report["inventory_checks"].append({
        "checkpoint": label,
        **snapshot_inventory(body, user_id, reference.campaign_id, {reference.drop_id}),
        "claim_id_present": bool(result and result.claim_id and result.claim_id.strip()),
    })
    return result


async def read_current(client, channel, target, label):
    client.phase = label
    body = await client.gql_request(GQL_QUERIES["CurrentDrop"].with_variables({"channelID": str(channel.id)}))
    state = snapshot_current(body, str(client._auth_state.user_id), {target.drop_id})
    client.report["current_checks"].append({"checkpoint": label, **state})
    return state


def observed_minutes(target, current):
    value = target.minutes
    if current.get("session_state") == "present" and current.get("target_drop"):
        value = max(value, current["minutes"])
    return value


def claimed_result(client, journal, *, previous=False):
    journal.confirm()
    client.report["claim"]["confirmed"] = True
    client.report["state"] = "claim_confirmed"
    client.report["claim"]["confirmation_source"] = "Inventory.self.isClaimed"
    client.report["claim"]["previous_attempt"] = previous


async def reconcile_previous(client, journal, saved, campaign_name, drop_id):
    if (saved["user_id"] != str(client._auth_state.user_id)
        or saved["campaign_name"] != campaign_name
        or (drop_id is not None and saved["drop_id"] != drop_id)):
        raise WatchCheckError("journal_target_or_account_mismatch")
    client.report["claim"]["previous_attempt"] = True
    client.report["state"] = "claim_unconfirmed"
    client.phase = "reconcile_previous"
    body = await client.gql_request(GQL_QUERIES["Inventory"])
    user_id = str(client._auth_state.user_id)
    inventory_identity(body, user_id)
    state = snapshot_inventory(body, user_id, saved["campaign_id"], {saved["drop_id"]})
    client.report["inventory_checks"].append({"checkpoint": client.phase, **state})
    if len(state["drops"]) == 1 and state["drops"][0]["is_claimed"] is True:
        claimed_result(client, journal, previous=True)
    else:
        client.report["error"] = "previous_claim_not_confirmed"


async def claim_and_confirm(client, journal, target):
    # Refresh both identity and Inventory immediately before using an instance ID.
    client.phase = "claim_validation"
    await client.validate_identity()
    latest = await read_inventory(client, "claim_inventory", target=target)
    if latest is None:
        raise WatchCheckError("target_disappeared_before_claim")
    if latest.is_claimed:
        client.report["state"] = "already_claimed"
        return
    if latest.account_link_state is False:
        raise WatchCheckError("account_link_not_confirmed")
    claim_id = latest.require_claim_id()
    client.remaining()
    if len(client.report["requests"]) >= client.request_limit:
        raise WatchCheckError("request_budget_exhausted")
    # Persist before mutation; a crash or lost response must never enable replay.
    journal.record_attempt(str(client._auth_state.user_id), latest.campaign_id,
                           latest.name, latest.drop_id)
    client.report["state"] = "claim_unconfirmed"
    client.report["claim"]["attempted"] = True
    client.phase = "claim"
    client._claim_authorization = claim_id
    drop = BaseDrop.__new__(BaseDrop)
    drop._twitch = client
    drop.campaign = SimpleNamespace(ends_at=latest.campaign_ends_at)
    drop.claim_id, drop.is_claimed, drop.id = claim_id, False, latest.drop_id
    accepted = await drop._claim()
    client.report["claim"]["miner_response_accepted"] = accepted
    # A false/unknown result still might have committed. Read, never replay.
    for index in range(3):
        if index:
            await client.clock.sleep(min(CONFIRM_INTERVAL, client.remaining()))
        confirmed = await read_inventory(client, "claim_confirmation", target=latest)
        if confirmed is not None and confirmed.is_claimed:
            claimed_result(client, journal)
            return
    client.report["error"] = "claim_not_confirmed_by_inventory"


async def finish_if_ready(client, journal, target):
    if target.is_claimed:
        client.report["state"] = "already_claimed"
        return True
    if target.account_link_state is False:
        raise WatchCheckError("account_link_not_confirmed")
    if target.preconditions_met is False:
        raise WatchCheckError("target_preconditions_not_met")
    if target.minutes < target.required_minutes:
        return False
    # Full minutes with a missing instance ID is not permission to invent one.
    # Stop watching and permit two delayed Inventory reads for server propagation.
    for index in range(3):
        if target.ready_to_claim:
            await claim_and_confirm(client, journal, target)
            return True
        if index == 2:
            raise WatchCheckError("server_claim_id_not_ready")
        await client.clock.sleep(min(CONFIRM_INTERVAL, client.remaining()))
        latest = await read_inventory(client, "waiting_for_claim_id", target=target)
        if latest is None:
            raise WatchCheckError("target_disappeared_before_claim")
        if latest.is_claimed:
            client.report["state"] = "already_claimed"
            return True
        if latest.minutes < latest.required_minutes:
            raise WatchCheckError("claim_progress_regressed")
        target = latest
    return True


async def experiment(client, journal, channel_login, campaign_name, drop_id, linked_confirmed):
    saved = journal.read()
    if saved is not None:
        await reconcile_previous(client, journal, saved, campaign_name, drop_id)
        return
    if client.reconcile_only:
        raise WatchCheckError("reconcile_journal_missing")
    target = await read_inventory(client, "baseline", campaign_name=campaign_name, drop_id=drop_id)
    client.report["target"] = target.public_dict()
    if await finish_if_ready(client, journal, target):
        return
    if not target.can_watch():
        raise WatchCheckError("target_not_active")
    client.phase = "preflight"
    query = GQL_QUERIES["GetStreamInfo"].with_variables({"channel": channel_login})
    user = stream_user(await client.gql_request(query))
    channel = Channel(client, id=user["id"], login=channel_login)
    client.settings.available_drops_check = True
    channel._stream = Stream.from_get_stream(channel, user)
    available = await client.gql_request(GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel.id)}))
    if str(available["data"]["channel"].get("id")) != str(channel.id):
        raise WatchCheckError("available_channel_mismatch")
    available_report = {}
    campaign_id, drop_ids, ends = target_campaign(available, campaign_name, str(channel.game.id), available_report)
    if campaign_id != target.campaign_id or target.drop_id not in drop_ids or str(channel.game.id) != target.game_id:
        raise WatchCheckError("channel_target_mismatch")
    links = [target.account_link_state, available_report["target"]["account_link_state"]]
    if False in links or (True not in links and not linked_confirmed):
        raise WatchCheckError("account_link_not_confirmed")
    client.report["link_evidence"] = "server" if True in links else "operator_confirmed_connections"
    current = await read_current(client, channel, target, "baseline")
    clock = client.clock
    high_water, last_progress = observed_minutes(target, current), clock.time()
    next_check, next_send = clock.time() + CHECK_INTERVAL, clock.time()
    client.watch_deadline = client.deadline - FINISH_RESERVE
    client.watch_clock, client.watch_ends = clock, min(ends, target.ends_at)
    while clock.time() < client.watch_deadline:
        if not target.can_watch() or datetime.now(timezone.utc) >= client.watch_ends:
            raise WatchCheckError("target_not_active")
        if clock.time() >= next_check:
            client.phase = "periodic_validation"
            await client.validate_identity()
            new_user = stream_user(await client.gql_request(query))
            if (str(new_user["id"]) != str(channel.id)
                or new_user["stream"]["id"] != user["stream"]["id"]
                or str(new_user["broadcastSettings"]["game"]["id"]) != target.game_id):
                raise WatchCheckError("stream_changed")
            current = await read_current(client, channel, target, "periodic")
            latest = await read_inventory(client, "periodic", target=target)
            if latest is None:
                raise WatchCheckError("target_disappeared_during_watch")
            target = latest
            if await finish_if_ready(client, journal, target):
                return
            observed = observed_minutes(target, current)
            if observed > high_water:
                high_water, last_progress = observed, clock.time()
            next_check = clock.time() + CHECK_INTERVAL
        if clock.time() - last_progress >= STALL_SECONDS:
            raise WatchCheckError("server_progress_stalled")
        if clock.time() >= client.watch_deadline:
            break
        if clock.time() >= next_send:
            client.phase = "watch"
            try:
                if not await channel.send_watch():
                    raise WatchCheckError("watch_transport_not_204")
            except WatchWindowEnded:
                break
            client.report["watch_sends"] += 1
            next_send = clock.time() + WATCH_INTERVAL.total_seconds()
        wake = min(next_send, next_check, last_progress + STALL_SECONDS, client.watch_deadline)
        await clock.sleep(max(0, wake - clock.time()))
    client.report["watch_stop_reason"] = "watch_window_ended"
    client.phase = "final_validation"
    await client.validate_identity()
    latest = await read_inventory(client, "final", target=target)
    if latest is None:
        raise WatchCheckError("target_disappeared_during_watch")
    if not await finish_if_ready(client, journal, latest):
        client.report["state"] = "incomplete"
        client.report["error"] = "time_limit_before_claim_ready"


async def check(cookie_file, channel, campaign, proxy=None, linked_confirmed=False,
                seconds=MAX_SECONDS, state_dir=DEFAULT_STATE, drop_id=None, *, clock=None,
                reconcile_only=False):
    budget = min(seconds, 120) if reconcile_only else seconds
    report = {"state": "failed", "mode": "reconcile_claim" if reconcile_only else "finish_one_drop", "cookie_loaded": False,
              "web_token_valid": False, "requests": [], "inventory_checks": [],
              "current_checks": [], "watch_sends": 0,
              "claim": {"attempted": False, "previous_attempt": False, "confirmed": False},
              "limits": {"total_seconds": budget, "requests": 2 if reconcile_only else MAX_REQUESTS,
                         **({} if reconcile_only else {"stall_seconds": STALL_SECONDS,
                             "watch_finish_reserve_seconds": FINISH_RESERVE})}}
    if clock is None:
        clock = SimpleNamespace(time=asyncio.get_running_loop().time, sleep=asyncio.sleep)
    start = clock.time()
    client = FinishClient(report, proxy, clock, start + budget, reconcile_only=reconcile_only)
    try:
        if (type(seconds) is not int or not (1 if reconcile_only else 180) <= seconds <= MAX_SECONDS
            or not re.fullmatch(r"[A-Za-z0-9_]{1,25}", channel) or not campaign.strip()):
            raise WatchCheckError("invalid_check_parameters")
        # Held across network work; concurrent instances fail before opening cookies.
        with FinishJournal(state_dir) as journal:
            saved = journal.read()  # fail on corrupt state before making a network request
            if reconcile_only and saved is None:
                raise WatchCheckError("reconcile_journal_missing")
            async def run():
                await client.open(cookie_file)
                await experiment(client, journal, channel.lower(), campaign, drop_id, linked_confirmed)
            await asyncio.wait_for(run(), timeout=client.remaining())
    except WatchCheckError as exc:
        report["error"] = exc.code
    except Exception as exc:
        report["error"] = type(exc).__name__  # no exception body, response, URL, or credentials
    finally:
        report["phase"] = client.phase
        report["elapsed_seconds"] = round(clock.time() - start, 3)
        await client.close()
    return (0 if report["state"] in {"claim_confirmed", "already_claimed"} else 1), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, default=Path("cookies.jar"))
    parser.add_argument("--channel", required=True)
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--drop-id", help="Exact public drop ID, only needed when the campaign is ambiguous")
    parser.add_argument("--proxy-env", help="Current proxy environment variable name, never a credential")
    parser.add_argument("--linked-confirmed", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--reconcile-only", action="store_true",
                        help="Require the existing attempt journal; only validate identity and read Inventory")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    code, report = asyncio.run(check(args.cookies, args.channel, args.campaign_name, proxy,
                                     args.linked_confirmed,
                                     args.max_seconds, args.state_dir, args.drop_id,
                                     reconcile_only=args.reconcile_only))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
