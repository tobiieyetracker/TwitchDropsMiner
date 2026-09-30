"""Bounded, no-GUI check using the miner's Channel/Stream and GQL transport.

Loads a trusted, explicitly selected aiohttp cookie jar without saving it. The
request adapter disables redirects/retries; it does not implement a new watch
payload, browser authentication, local progress estimates, or claims.
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
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from yarl import URL

from channel import Channel, Stream
from constants import ClientType, GQL_QUERIES, WATCH_INTERVAL
from gql_recovery import validate_campaign_response
from twitch import Twitch, _AuthState
from utils import RateLimiter
from watch_check_state import WatchCheckError, check_envelope, snapshot_current, snapshot_inventory


WEB = URL("https://www.twitch.tv")
GQL = URL("https://gql.twitch.tv/gql")
VALIDATE = URL("https://id.twitch.tv/oauth2/validate")
READS = {GQL_QUERIES[key]["operationName"] for key in (
    "GetStreamInfo", "AvailableDrops", "CurrentDrop", "Inventory",
)}


async def passthrough(coro):
    return await coro


class WatchWindowEnded(Exception):
    """Internal stop: final read-only snapshots are still permitted."""


class WatchClient(Twitch):
    def __init__(self, report: dict, proxy: str | None):
        # Do not construct Tk or enter the application's login/watch/claim loops.
        self.report = report
        self.settings = SimpleNamespace(proxy=proxy)
        self.gui = SimpleNamespace(channels=None, coro_unless_closed=passthrough)
        self._client_type = ClientType.WEB  # set BEFORE creating the shared Session
        self._web_session = None
        self._session = None
        self._auth_state = _AuthState(self)
        self._qgl_limiter = RateLimiter(capacity=5, window=1)
        self.token = None
        self.phase = "setup"
        self.watch_deadline = None

    async def open(self, cookie_file: Path):
        jar = aiohttp.CookieJar()
        jar.load(cookie_file)
        cookie = jar.filter_cookies(WEB).get("auth-token")
        if cookie is None or not cookie.value:
            raise WatchCheckError("cookie_has_no_web_auth_token")
        self.token = cookie.value
        trace = aiohttp.TraceConfig()
        trace.on_request_headers_sent.append(self.sent_headers)
        self._session = aiohttp.ClientSession(
            cookie_jar=jar, headers={"User-Agent": self._client_type.USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20, sock_connect=10),
            trace_configs=[trace],
        )
        self.report["cookie_loaded"] = True
        await self.validate_identity()
        auth = self._auth_state
        auth.access_token = self.token
        # Preserve an existing device cookie, without copying cookies across domains.
        device = jar.filter_cookies(WEB).get("unique_id")
        if device is not None:
            auth.device_id = device.value

    async def validate_identity(self):
        async with self.request("GET", VALIDATE, headers={"Authorization": f"OAuth {self.token}"}) as response:
            body = await response.json()
        if not isinstance(body, dict) or body.get("client_id") != ClientType.WEB.CLIENT_ID:
            raise WatchCheckError("not_a_valid_web_token")
        user_id = body.get("user_id")
        if not isinstance(user_id, str) or not user_id.isdigit():
            raise WatchCheckError("validation_user_missing")
        if hasattr(self._auth_state, "user_id") and self._auth_state.user_id != int(user_id):
            raise WatchCheckError("validation_user_changed")
        self._auth_state.user_id = int(user_id)
        self.report["web_token_valid"] = True

    async def get_auth(self):
        # Identity was validated above. The normal login method may save cookies
        # or open a browser, so it is deliberately outside this diagnostic.
        if not hasattr(self._auth_state, "access_token"):
            raise WatchCheckError("identity_not_initialized")
        return self._auth_state

    async def sent_headers(self, session, context, params):
        entry = context.trace_request_ctx
        headers = params.headers
        cookies = SimpleCookie()
        cookies.load(headers.get("Cookie", ""))
        entry.update(
            cookie_count=len(cookies), auth_cookie_sent="auth-token" in cookies,
            auth_cookie_matches=cookies["auth-token"].value == self.token
                if "auth-token" in cookies else None,
            oauth_sent="Authorization" in headers,
            oauth_matches=headers.get("Authorization") == f"OAuth {self.token}"
                if "Authorization" in headers else None,
            web_client_matches=headers.get("Client-Id") == ClientType.WEB.CLIENT_ID
                if "Client-Id" in headers else None,
            web_ua_matches=headers.get("User-Agent") == self._client_type.USER_AGENT,
            content_type=headers.get("Content-Type"),
        )

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        target = URL(url)
        if target.scheme != "https" or target.user or not target.host or not any(
            target.host == host or target.host.endswith("." + host)
            for host in ("twitch.tv", "twitchcdn.net", "jtvnw.net")
        ):
            raise WatchCheckError("unexpected_endpoint")
        if len(self.report["requests"]) >= 40:
            raise WatchCheckError("request_budget_exhausted")
        session = await self.get_session()
        current_cookie = session.cookie_jar.filter_cookies(WEB).get("auth-token")
        if current_cookie is None or current_cookie.value != self.token:
            raise WatchCheckError("auth_cookie_changed")
        target_cookie = session.cookie_jar.filter_cookies(target).get("auth-token")
        if target_cookie is not None and target_cookie.value != self.token:
            raise WatchCheckError("target_auth_cookie_differs")
        if self.phase == "watch":
            remaining = self.watch_deadline - self.watch_clock.time()
            if remaining <= 0 or datetime.now(timezone.utc) >= self.watch_ends:
                raise WatchWindowEnded()
            kwargs["timeout"] = aiohttp.ClientTimeout(total=min(20, remaining))
        entry = {"phase": self.phase, "method": method, "host": target.host,
                 "http_status": None, "redirected": False}
        self.report["requests"].append(entry)
        # One attempt, no redirect following and no TLS overrides. In particular,
        # never add GQL credentials to the Spade request.
        try:
            async with session.request(
                method, target, proxy=self.settings.proxy, allow_redirects=False,
                trace_request_ctx=entry, **kwargs,
            ) as response:
                entry["http_status"] = response.status
                if 300 <= response.status < 400:
                    entry["redirected"] = True
                    raise WatchCheckError("http_redirect_stopped")
                if response.status == 429:
                    raise WatchCheckError("http_rate_limited")
                if not 200 <= response.status < 300:
                    raise WatchCheckError("http_error")
                yield response
        except asyncio.TimeoutError:
            if self.phase == "watch" and self.watch_clock.time() >= self.watch_deadline:
                raise WatchWindowEnded() from None
            raise

    async def gql_request(self, operation):
        if not isinstance(operation, dict) or operation.get("operationName") not in READS:
            raise WatchCheckError("operation_not_allowed")
        # Use the actual miner header/transport builder, without its retry loop.
        body, _ = await Twitch._gql_request_once(self, operation)
        check_envelope(body)
        validate_campaign_response(operation, body)
        return body

    async def close(self):
        if self._session is not None:
            await self._session.close()
        # Never call Twitch.shutdown(): a normal shutdown can save cookies.


def stream_user(body):
    data = body.get("data") if isinstance(body, dict) else None
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, dict) or not isinstance(user.get("stream"), dict):
        raise WatchCheckError("channel_not_live")
    settings = user.get("broadcastSettings")
    game = settings.get("game") if isinstance(settings, dict) else None
    if not isinstance(game, dict) or not game.get("id") or not game.get("name"):
        raise WatchCheckError("broadcast_settings_game_unknown")
    return user


def target_campaign(body, name: str, game_id: str, report: dict):
    campaigns = body["data"]["channel"]["viewerDropCampaigns"]
    candidates = [item for item in campaigns if isinstance(item, dict) and item.get("name") == name]
    if len(candidates) != 1:
        raise WatchCheckError("target_campaign_not_unique_or_available")
    campaign = candidates[0]
    game = campaign.get("game")
    if not isinstance(game, dict) or str(game.get("id")) != game_id:
        raise WatchCheckError("campaign_stream_game_mismatch")
    now = datetime.now(timezone.utc)
    try:
        ends = datetime.fromisoformat(campaign["endAt"].replace("Z", "+00:00"))
        drops = campaign["timeBasedDrops"]
        active = [drop for drop in drops if (
            datetime.fromisoformat(drop["startAt"].replace("Z", "+00:00")) <= now
            < datetime.fromisoformat(drop["endAt"].replace("Z", "+00:00"))
        )]
        if ends <= now or not active:
            raise WatchCheckError("campaign_not_active")
        ends = min(ends, *(datetime.fromisoformat(drop["endAt"].replace("Z", "+00:00")) for drop in active))
        if not isinstance(campaign["id"], str) or not all(
            isinstance(drop["id"], str) and type(drop["requiredMinutesWatched"]) is int
            and drop["requiredMinutesWatched"] > 0 for drop in active
        ):
            raise WatchCheckError("campaign_schema_invalid")
    except (KeyError, TypeError, ValueError):
        raise WatchCheckError("campaign_schema_invalid") from None
    self_data = campaign.get("self")
    linked = self_data.get("isAccountConnected") if isinstance(self_data, dict) else None
    report["target"] = {
        "campaign_id": campaign["id"], "campaign_name": name,
        "account_link_state": linked if type(linked) is bool else None,
        "drop_ids": [drop["id"] for drop in active],
        "required_minutes": [drop["requiredMinutesWatched"] for drop in active],
        "game_source": "GetStreamInfo.broadcastSettings.game",
    }
    return campaign["id"], {drop["id"] for drop in active}, ends


async def snapshot(client, channel, campaign_id, drop_ids, label):
    client.phase = label
    user_id = str(client._auth_state.user_id)
    current = await client.gql_request(GQL_QUERIES["CurrentDrop"].with_variables({"channelID": str(channel.id)}))
    current_state = snapshot_current(current, user_id, drop_ids)
    inventory = await client.gql_request(GQL_QUERIES["Inventory"])
    inventory_state = snapshot_inventory(inventory, user_id, campaign_id, drop_ids)
    result = {"checkpoint": label, "current": current_state, "inventory": inventory_state}
    client.report["checkpoints"].append(result)
    return result


def progress_evidence(before, after):
    def values(checkpoint):
        result = {d["drop_id"]: d["minutes"] for d in checkpoint["inventory"]["drops"]}
        current = checkpoint["current"]
        if current.get("target_drop"):
            result[current["drop_id"]] = max(current["minutes"], result.get(current["drop_id"], 0))
        return result
    initial = values(before)
    return [
        {"drop_id": drop_id, "before_minutes": initial.get(drop_id), "after_minutes": minutes,
         "kind": "increase" if drop_id in initial else "new_positive_progress"}
        for drop_id, minutes in values(after).items()
        if minutes > 0 and (drop_id not in initial or minutes > initial[drop_id])
    ]


async def experiment(client, channel_login, campaign_name, watch, linked_confirmed, seconds, *, clock=None):
    client.phase = "preflight"
    query = GQL_QUERIES["GetStreamInfo"].with_variables({"channel": channel_login})
    user = stream_user(await client.gql_request(query))
    channel = Channel(client, id=user["id"], login=channel_login)
    # Use the real constructor, including its game source and cached watch payload.
    client.settings.available_drops_check = True
    channel._stream = Stream.from_get_stream(channel, user)
    available = await client.gql_request(GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(channel.id)}))
    if str(available["data"]["channel"].get("id")) != str(channel.id):
        raise WatchCheckError("available_channel_mismatch")
    campaign_id, drop_ids, ends = target_campaign(available, campaign_name, str(channel.game.id), client.report)
    baseline = await snapshot(client, channel, campaign_id, drop_ids, "baseline")
    if not watch:
        client.report["state"] = "preflight_passed"
        return
    linked = client.report["target"]["account_link_state"]
    if linked is False or (linked is None and not linked_confirmed):
        raise WatchCheckError("account_link_not_confirmed")
    client.report["link_evidence"] = "server" if linked else "operator_confirmed_connections"
    interval = WATCH_INTERVAL.total_seconds()
    if clock is None:
        clock = SimpleNamespace(time=asyncio.get_running_loop().time, sleep=asyncio.sleep)
    start = clock.time()
    deadline, middle, next_send = start + seconds, start + seconds / 2, start
    client.watch_deadline, client.watch_clock, client.watch_ends = deadline, clock, ends
    middle_done = False
    while clock.time() < deadline:
        now = clock.time()
        if datetime.now(timezone.utc) >= ends:
            raise WatchCheckError("campaign_ended")
        if not middle_done and now >= middle:
            client.phase = "midpoint"
            current_user = stream_user(await client.gql_request(query))
            if (current_user["stream"]["id"] != user["stream"]["id"]
                or str(current_user["broadcastSettings"]["game"]["id"]) != str(channel.game.id)):
                raise WatchCheckError("stream_changed")
            await snapshot(client, channel, campaign_id, drop_ids, "midpoint")
            middle_done = True
        if next_send <= clock.time() < deadline and client.report["watch_sends"] < 10:
            client.phase = "watch"
            # This is the repository method, not a rewritten Spade event sender.
            try:
                if not await channel.send_watch():
                    raise WatchCheckError("watch_transport_not_204")
            except WatchWindowEnded:
                client.report["watch_stop_reason"] = "watch_window_ended"
                break
            client.report["watch_sends"] += 1
            next_send = clock.time() + interval  # no catch-up bursts after slow requests
        wake = min(deadline, middle if not middle_done else deadline,
                   next_send if client.report["watch_sends"] < 10 else deadline)
        await clock.sleep(max(0, wake - clock.time()))
    final = await snapshot(client, channel, campaign_id, drop_ids, "final")
    client.phase = "final_validation"
    await client.validate_identity()
    client.report["progress_evidence"] = progress_evidence(baseline, final)
    client.report["state"] = "progress_observed" if client.report["progress_evidence"] else "no_progress_observed"


async def check(cookie_file, channel, campaign, proxy=None, watch=False, linked_confirmed=False, seconds=600):
    report = {"state": "failed", "mode": "watch" if watch else "read_only", "cookie_loaded": False,
              "web_token_valid": False, "requests": [], "checkpoints": [], "watch_sends": 0}
    client = WatchClient(report, proxy)
    try:
        if not 60 <= seconds <= 600 or not re.fullmatch(r"[A-Za-z0-9_]{1,25}", channel):
            raise WatchCheckError("invalid_check_parameters")
        await client.open(cookie_file)
        await asyncio.wait_for(
            experiment(client, channel.lower(), campaign, watch, linked_confirmed, seconds),
            timeout=seconds + 120,
        )
    except WatchCheckError as exc:
        report["error"] = exc.code
    except Exception as exc:
        report["error"] = type(exc).__name__  # exception text may contain credentials/URLs
    finally:
        report["phase"] = client.phase
        await client.close()
    return (0 if report["state"] in {"preflight_passed", "progress_observed"} else 1), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, default=Path("cookies.jar"))
    parser.add_argument("--channel", required=True)
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--proxy-env", help="Name of the current proxy environment variable")
    parser.add_argument("--watch", action="store_true", help="Send up to 10 real miner watch events")
    parser.add_argument("--linked-confirmed", action="store_true", help="Connections-page link was confirmed externally")
    parser.add_argument("--seconds", type=int, default=600, help="Watch window, 60..600 seconds")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    proxy = os.environ.get(args.proxy_env) if args.proxy_env else None
    if args.proxy_env and not proxy:
        print(json.dumps({"state": "failed", "error": "proxy_environment_missing"}))
        return 1
    code, report = asyncio.run(check(args.cookies, args.channel, args.campaign_name, proxy,
                                     args.watch, args.linked_confirmed, args.seconds))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
