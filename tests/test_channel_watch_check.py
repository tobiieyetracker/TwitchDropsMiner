import asyncio
import json
from base64 import b64decode
from contextlib import asynccontextmanager
from copy import deepcopy
from http.cookies import SimpleCookie
from types import SimpleNamespace

import aiohttp
import pytest
from yarl import URL

import check_channel_watch as probe
from channel import Channel
from watch_check_state import WatchCheckError


TOKEN = "fixture-private-oauth"
STREAM = {"data": {"user": {
    "id": "123", "displayName": "Example",
    "stream": {"id": "456", "viewersCount": 10, "game": None},
    "broadcastSettings": {"game": {"id": "7", "name": "Test game"}, "title": "Live"},
}}}
CAMPAIGN = {
    "id": "campaign", "name": "Test campaign", "game": {"id": "7"}, "self": None,
    "endAt": "2099-01-01T00:00:00Z", "timeBasedDrops": [{
        "id": "drop", "startAt": "2020-01-01T00:00:00Z", "endAt": "2099-01-01T00:00:00Z",
        "requiredMinutesWatched": 60,
    }],
}


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    async def json(self):
        return deepcopy(self.body)

    async def text(self, **kwargs):
        return self.body


class Network:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.sends = []
        self.sessions = []
        self.responses = {}
        self.progress = True
        self.late_page = False
        self.late_midpoint = False
        self.page_timeout = False
        self.midpoint_timeout = False
        self.stream_reads = 0

    def session(self, **kwargs):
        network = self

        class Session:
            def __init__(self):
                self.headers = kwargs["headers"]
                self.cookie_jar = kwargs["cookie_jar"]
                self.closed = False

            async def close(self):
                self.closed = True

            @asynccontextmanager
            async def request(self, method, url, **options):
                assert options["allow_redirects"] is False
                assert "ssl" not in options
                network.calls.append((method, str(url), options))
                headers = {**self.headers, **options.get("headers", {})}
                cookies = self.cookie_jar.filter_cookies(url)
                if cookies:
                    headers["Cookie"] = cookies.output(header="", sep=";").strip()
                if "data" in options:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                for trace in kwargs["trace_configs"]:
                    for handler in trace.on_request_headers_sent:
                        await handler(self, SimpleNamespace(trace_request_ctx=options["trace_request_ctx"]),
                                      SimpleNamespace(headers=headers))
                key = options.get("json", {}).get("operationName", url.host)
                if key in network.responses:
                    yield network.responses[key]
                    return
                if url == probe.VALIDATE:
                    yield Response({"client_id": probe.ClientType.WEB.CLIENT_ID, "user_id": "42"})
                elif url.host == "www.twitch.tv":
                    if network.late_page:
                        network.clock.now = 61
                    if network.page_timeout:
                        network.clock.now = 61
                        raise asyncio.TimeoutError()
                    yield Response('"spade_url":"https://spade.twitch.tv/track"')
                elif url.host == "spade.twitch.tv":
                    assert "headers" not in options  # no copied GQL auth/integrity headers
                    network.sends.append((network.clock.time(), deepcopy(options["data"])))
                    yield Response({}, 204)
                elif key == "VideoPlayerStreamInfoOverlayChannel":
                    network.stream_reads += 1
                    if network.late_midpoint and network.stream_reads == 2:
                        network.clock.now = 61
                    if network.midpoint_timeout and network.stream_reads == 2:
                        network.clock.now = 61
                        raise asyncio.TimeoutError()
                    yield Response(STREAM)
                elif key == "DropsHighlightService_AvailableDrops":
                    yield Response({"data": {"channel": {"id": "123", "viewerDropCampaigns": [CAMPAIGN]}}})
                elif key in {"DropCurrentSessionContext", "Inventory"}:
                    minutes = 2 if network.progress and len(network.sends) >= 2 else None
                    if key == "DropCurrentSessionContext":
                        session = {"dropID": "drop", "currentMinutesWatched": minutes} if minutes else None
                        own = {"dropCurrentSession": session}
                    else:
                        campaigns = [{"id": "campaign", "timeBasedDrops": [{
                            "id": "drop", "requiredMinutesWatched": 60,
                            "self": {"isClaimed": False, "currentMinutesWatched": minutes},
                        }]}] if minutes else []
                        own = {"inventory": {"dropCampaignsInProgress": campaigns}}
                    yield Response({"data": {"currentUser": {"id": "42", **own}}})
                else:
                    raise AssertionError("Unexpected operation")

        session = Session()
        self.sessions.append(session)
        return session


def scenario(monkeypatch, tmp_path, *, watch=True, linked=True, seconds=60,
             configure=None, host_only=False, conflicting=False):
    async def run():
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["auth-token"] = TOKEN
        cookie["auth-token"]["path"] = "/"
        if not host_only:
            cookie["auth-token"]["domain"] = ".twitch.tv"
        jar.update_cookies(cookie, probe.WEB)
        if conflicting:
            jar.update_cookies({"auth-token": "other-private-token"}, URL("https://gql.twitch.tv"))
        cookie_file = tmp_path / "cookies.jar"
        jar.save(cookie_file)
        original = cookie_file.read_bytes()
        clock, original_experiment = Clock(), probe.experiment
        network = Network(clock)
        if configure:
            configure(network)

        async def fast_experiment(*args, **kwargs):
            return await original_experiment(*args, **kwargs, clock=clock)

        monkeypatch.setattr(probe.aiohttp, "ClientSession", network.session)
        monkeypatch.setattr(probe, "experiment", fast_experiment)
        code, report = await probe.check(cookie_file, "example", "Test campaign", watch=watch,
                                         linked_confirmed=linked, seconds=seconds)
        assert cookie_file.read_bytes() == original
        assert all(s.closed for s in network.sessions)
        output = json.dumps(report)
        assert TOKEN not in output and "other-private-token" not in output
        assert '"42"' not in output
        return code, report, network
    return asyncio.run(run())


def test_real_channel_method_uses_validated_ids_and_broadcast_settings(monkeypatch, tmp_path):
    original = Channel.send_watch
    calls = []

    async def traced(self):
        calls.append(self)
        return await original(self)

    monkeypatch.setattr(Channel, "send_watch", traced)
    code, report, network = scenario(monkeypatch, tmp_path)
    assert code == 0 and report["state"] == "progress_observed"
    assert len(calls) == len(network.sends) == 2
    payload = json.loads(b64decode(network.sends[0][1]["data"]))[0]["properties"]
    assert payload["user_id"] == 42 and payload["channel_id"] == "123"
    assert payload["broadcast_id"] == "456" and payload["game_id"] == "7"
    assert payload["game"] == "Test game"  # stream.game was deliberately null
    assert report["progress_evidence"][0]["before_minutes"] is None
    assert report["checkpoints"][0]["current"]["minutes"] is None
    assert len(network.sessions) == 1
    assert all(r["auth_cookie_matches"] and r["web_ua_matches"] for r in report["requests"])
    spade = [r for r in report["requests"] if r["host"] == "spade.twitch.tv"]
    assert all(not r["oauth_sent"] and r["content_type"] == "application/x-www-form-urlencoded" for r in spade)


def test_no_watch_without_explicit_option(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, watch=False, linked=False)
    assert code == 0 and report["state"] == "preflight_passed"
    assert network.sends == []


def test_unknown_link_stops_before_watch(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, linked=False)
    assert code == 1 and report["error"] == "account_link_not_confirmed"
    assert network.sends == []


def test_204_is_not_reported_as_progress(monkeypatch, tmp_path):
    code, report, _ = scenario(monkeypatch, tmp_path, configure=lambda n: setattr(n, "progress", False))
    assert code == 1 and report["state"] == "no_progress_observed"
    assert report["watch_sends"] == 2 and report["progress_evidence"] == []


@pytest.mark.parametrize("body,expected", [
    ({"data": {"currentUser": None}}, "user_null"),
    ({"data": {"currentUser": {"id": "different-user", "dropCurrentSession": None}}}, "user_id_mismatch"),
])
def test_invalid_progress_identity_never_starts_watch(monkeypatch, tmp_path, body, expected):
    code, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: n.responses.update({
        "DropCurrentSessionContext": Response(body),
    }))
    assert code == 1 and report["error"] == expected
    assert network.sends == []


@pytest.mark.parametrize("status,expected", [(429, "http_rate_limited"), (302, "http_redirect_stopped"), (503, "http_error")])
def test_spade_errors_stop_after_one_attempt(monkeypatch, tmp_path, status, expected):
    code, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: n.responses.update({
        "spade.twitch.tv": Response({}, status),
    }))
    assert code == 1 and report["error"] == expected
    assert sum(url.startswith("https://spade.") for _, url, _ in network.calls) == 1
    assert report["watch_sends"] == 0


@pytest.mark.parametrize("envelope,expected", [
    ({"extensions": {"challenge": {}}}, "gql_challenge"),
    ({"extensions": {"challenge": {"type": "integrity"}}}, "gql_challenge"),
    ({"extensions": []}, "extensions_invalid"),
    ({"errors": [{"message": TOKEN}]}, "gql_errors"),
])
def test_first_query_challenge_stops_without_browser_or_retry(monkeypatch, tmp_path, envelope, expected):
    code, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: n.responses.update({
        "VideoPlayerStreamInfoOverlayChannel": Response(envelope),
    }))
    assert code == 1 and report["error"] == expected
    assert len(network.calls) == 2 and network.sends == []


@pytest.mark.parametrize("late", ["late_midpoint", "late_page"])
def test_no_watch_post_after_deadline(monkeypatch, tmp_path, late):
    code, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: setattr(n, late, True))
    assert all(at < 60 for at, _ in network.sends)
    assert len(network.sends) == (1 if late == "late_midpoint" else 0)
    assert report["checkpoints"][-1]["checkpoint"] == "final"


def test_ten_minute_window_never_exceeds_ten_sends(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, seconds=600)
    assert code == 0 and report["watch_sends"] == 10
    assert all(b[0] - a[0] >= 59 for a, b in zip(network.sends, network.sends[1:]))


def test_watch_page_timing_out_at_deadline_permits_final_snapshot(monkeypatch, tmp_path):
    _, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: setattr(n, "page_timeout", True))
    assert network.sends == []
    assert report["watch_stop_reason"] == "watch_window_ended"
    assert report["checkpoints"][-1]["checkpoint"] == "final"


def test_midpoint_network_timeout_is_an_error_not_zero_progress(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: setattr(n, "midpoint_timeout", True))
    assert code == 1 and report["state"] == "failed" and report["error"] == "TimeoutError"
    assert report["phase"] == "midpoint" and len(network.sends) == 1
    assert report["checkpoints"][-1]["checkpoint"] == "baseline"


def test_host_only_cookie_is_not_copied_to_spade(monkeypatch, tmp_path):
    _, report, _ = scenario(monkeypatch, tmp_path, host_only=True)
    spade = [r for r in report["requests"] if r["host"] == "spade.twitch.tv"]
    assert all(not r["auth_cookie_sent"] and r["auth_cookie_matches"] is None for r in spade)


def test_conflicting_target_cookie_is_rejected_without_overwriting(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, conflicting=True)
    assert code == 1 and report["error"] == "target_auth_cookie_differs"
    assert len(network.calls) == 1 and network.sends == []


def test_missing_game_is_not_replaced_from_available_drops(monkeypatch, tmp_path):
    body = deepcopy(STREAM)
    body["data"]["user"]["broadcastSettings"]["game"] = None
    code, report, network = scenario(monkeypatch, tmp_path, configure=lambda n: n.responses.update({
        "VideoPlayerStreamInfoOverlayChannel": Response(body),
    }))
    assert code == 1 and report["error"] == "broadcast_settings_game_unknown"
    assert network.sends == []
