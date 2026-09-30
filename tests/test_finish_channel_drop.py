import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from http.cookies import SimpleCookie
from types import SimpleNamespace

import aiohttp
import pytest

import finish_channel_drop as finish
from channel import Channel
from check_channel_watch import WEB, VALIDATE
from constants import ClientType, GQL_QUERIES
from finish_drop_journal import FinishJournal
from inventory import BaseDrop
from test_channel_watch_check import Clock, Response, STREAM, CAMPAIGN, TOKEN
from watch_check_state import WatchCheckError


CLAIM_ID = "fixture-private-instance-id"
CLAIM_OP = GQL_QUERIES["ClaimDrop"]["operationName"]


class Network:
    def __init__(self, clock):
        self.clock = clock
        self.calls, self.sends, self.claims, self.sessions = [], [], [], []
        self.minutes, self.required = 8, 60
        self.initial_claimed = False
        self.confirm = True
        self.disappear = False
        self.claim_id = CLAIM_ID
        self.claim_status = "ELIGIBLE_FOR_ALL"
        self.progress = True
        self.current_full = False
        self.changed_stream = False
        self.changed_game = False
        self.changed_user = False
        self.stream_reads = 0
        self.validate_reads = 0
        self.responses = {}
        self.slow_open = False
        self.slow_page = False
        self.slow_periodic = False

    def inventory(self):
        claimed = self.initial_claimed or bool(self.claims and self.confirm)
        minutes = 0 if claimed else min(self.required, self.minutes + (len(self.sends) if self.progress else 0))
        campaign = deepcopy(CAMPAIGN)
        campaign["startAt"] = "2020-01-01T00:00:00Z"
        drop = campaign["timeBasedDrops"][0]
        drop["requiredMinutesWatched"] = self.required
        drop["self"] = {"currentMinutesWatched": minutes, "isClaimed": claimed,
                        "dropInstanceID": None if claimed else self.claim_id}
        return {"data": {"currentUser": {"id": "42", "inventory": {
            "dropCampaignsInProgress": [] if self.claims and self.disappear else [campaign],
        }}}}

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
                assert options["allow_redirects"] is False and "ssl" not in options
                network.calls.append((network.clock.time(), method, str(url), options))
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
                if key == CLAIM_OP:
                    network.claims.append(deepcopy(options["json"]))
                response = network.responses.get(key)
                if isinstance(response, Exception):
                    raise response
                if response is not None:
                    yield response
                elif url == VALIDATE:
                    network.validate_reads += 1
                    if network.slow_open:
                        network.clock.now += 5401
                    yield Response({"client_id": ClientType.WEB.CLIENT_ID,
                                    "user_id": "43" if network.changed_user and network.validate_reads > 1 else "42"})
                elif key == "Inventory":
                    yield Response(network.inventory())
                elif key == CLAIM_OP:
                    yield Response({"data": {"claimDropRewards": {"status": network.claim_status}}})
                elif key == "VideoPlayerStreamInfoOverlayChannel":
                    network.stream_reads += 1
                    body = deepcopy(STREAM)
                    if network.stream_reads > 1:
                        if network.changed_stream:
                            body["data"]["user"]["stream"]["id"] = "new-stream"
                        if network.changed_game:
                            body["data"]["user"]["broadcastSettings"]["game"]["id"] = "new-game"
                        if network.slow_periodic:
                            network.clock.now += 100
                    yield Response(body)
                elif key == "DropsHighlightService_AvailableDrops":
                    yield Response({"data": {"channel": {"id": "123", "viewerDropCampaigns": [CAMPAIGN]}}})
                elif key == "DropCurrentSessionContext":
                    minutes = network.required if network.current_full else network.minutes + (len(network.sends) if network.progress else 0)
                    yield Response({"data": {"currentUser": {"id": "42", "dropCurrentSession": {
                        "dropID": "drop", "currentMinutesWatched": minutes,
                    }}}})
                elif url.host == "www.twitch.tv":
                    if network.slow_page:
                        network.clock.now += 800
                    yield Response('"spade_url":"https://spade.twitch.tv/track"')
                elif url.host == "spade.twitch.tv":
                    network.sends.append(network.clock.time())
                    assert "headers" not in options
                    yield Response({}, 204)
                else:
                    raise AssertionError("unexpected_request")

        session = Session()
        self.sessions.append(session)
        return session


def scenario(monkeypatch, tmp_path, *, configure=None, seconds=5400, previous=False,
             journal_failure=False, locked=False, reconcile_only=False):
    async def run():
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["auth-token"] = TOKEN
        cookie["auth-token"]["domain"] = ".twitch.tv"
        cookie["auth-token"]["path"] = "/"
        jar.update_cookies(cookie, WEB)
        cookie_file = tmp_path / "cookies.jar"
        jar.save(cookie_file)
        original = cookie_file.read_bytes()
        clock = Clock()
        network = Network(clock)
        if configure:
            configure(network)
        state_dir = tmp_path / "state"
        if previous:
            with FinishJournal(state_dir) as journal:
                journal.record_attempt("42", "campaign", "Test campaign", "drop")
        if journal_failure:
            def fail(*args):
                raise WatchCheckError("fixture_journal_write_failure")
            monkeypatch.setattr(FinishJournal, "record_attempt", fail)
        monkeypatch.setattr(aiohttp, "ClientSession", network.session)
        async def check():
            return await finish.check(cookie_file, "example", "Test campaign", linked_confirmed=True,
                                      seconds=seconds, state_dir=state_dir, clock=clock,
                                      reconcile_only=reconcile_only)
        if locked:
            with FinishJournal(state_dir):
                code, report = await check()
        else:
            code, report = await check()
        assert cookie_file.read_bytes() == original
        assert all(s.closed for s in network.sessions)
        output = json.dumps(report)
        assert TOKEN not in output and CLAIM_ID not in output and '"42"' not in output
        journal_file = state_dir / "journal.json"
        saved = json.loads(journal_file.read_text()) if journal_file.exists() else None
        if saved:
            assert TOKEN not in json.dumps(saved) and CLAIM_ID not in json.dumps(saved)
        return code, report, network, saved
    return asyncio.run(run())


def test_resume_real_miner_watch_and_claim_confirmed_after_server_reset(monkeypatch, tmp_path):
    watched, claimed = [], []
    original_watch, original_claim = Channel.send_watch, BaseDrop._claim
    async def watch(self):
        watched.append(self)
        return await original_watch(self)
    async def claim(self):
        claimed.append(self)
        return await original_claim(self)
    monkeypatch.setattr(Channel, "send_watch", watch)
    monkeypatch.setattr(BaseDrop, "_claim", claim)
    code, report, network, journal = scenario(monkeypatch, tmp_path)
    assert code == 0 and report["state"] == "claim_confirmed"
    assert report["target"]["minutes"] == 8 and report["target"]["required_minutes"] == 60
    assert len(watched) == len(network.sends) >= 52 and len(claimed) == len(network.claims) == 1
    assert network.claims[0] == GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}})
    assert journal["outcome"] == "confirmed"
    assert report["inventory_checks"][-1]["drops"][0]["minutes"] == 0
    assert report["inventory_checks"][-1]["drops"][0]["is_claimed"] is True
    assert 40 < len(network.calls) <= finish.MAX_REQUESTS
    assert all(b - a >= 59 for a, b in zip(network.sends, network.sends[1:]))
    assert len(network.sessions) == 1
    assert all(r["auth_cookie_matches"] is True for r in report["requests"])
    assert not any(r["phase"] == "watch" for r in report["requests"][next(i for i, r in enumerate(report["requests"]) if r["phase"] == "claim"):])


@pytest.mark.parametrize("already", [False, True])
def test_ready_or_claimed_needs_no_channel_requests(monkeypatch, tmp_path, already):
    def configure(n):
        n.minutes, n.initial_claimed = 60, already
        n.responses["VideoPlayerStreamInfoOverlayChannel"] = Response({"data": {"user": {"stream": None}}})
    code, report, network, saved = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 0 and report["state"] == ("already_claimed" if already else "claim_confirmed")
    assert not network.sends and network.stream_reads == 0
    assert len(network.claims) == (0 if already else 1)
    assert (saved is None) == already


@pytest.mark.parametrize("claim_id", [None, "", "   "])
def test_full_minutes_without_real_claim_id_never_mutates(monkeypatch, tmp_path, claim_id):
    def configure(n):
        n.minutes, n.claim_id = 60, claim_id
    code, report, network, journal = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and report["error"] == "server_claim_id_not_ready"
    assert not network.claims and not network.sends and journal is None


def test_current_drop_full_never_authorizes_claim(monkeypatch, tmp_path):
    def configure(n):
        n.progress, n.current_full = False, True
    code, report, network, journal = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and report["error"] == "server_progress_stalled"
    assert not network.claims and journal is None
    assert report["elapsed_seconds"] == 900


@pytest.mark.parametrize("failure,expected", [
    (Response({}, 429), "http_rate_limited"),
    (Response({}, 302), "http_redirect_stopped"),
    (Response({"extensions": {"challenge": {"type": "integrity"}}}), "gql_challenge"),
    (asyncio.TimeoutError(TOKEN), "TimeoutError"),
])
def test_failed_claim_is_not_replayed_or_followed_by_network(monkeypatch, tmp_path, failure, expected):
    def configure(n):
        n.minutes = 60
        n.responses[CLAIM_OP] = failure
    code, report, network, journal = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and report["state"] == "claim_unconfirmed" and report["error"] == expected
    assert len(network.claims) == 1 and network.calls[-1][3]["json"]["operationName"] == CLAIM_OP
    assert journal["outcome"] == "attempted"
    assert not any(r["phase"] == "claim_confirmation" for r in report["requests"])
    assert report["inventory_checks"][-1]["checkpoint"] == "claim_inventory"
    if expected == "gql_challenge":
        assert report["claim"]["response_challenge"] == {"present": True, "type": "integrity"}
        assert report["requests"][-1]["integrity_header_present"] is False


@pytest.mark.parametrize("confirm,disappear,status", [
    (False, False, "ELIGIBLE_FOR_ALL"),
    (False, True, "ELIGIBLE_FOR_ALL"),
    (True, False, "UNKNOWN"),
])
def test_only_inventory_claimed_confirms_mutation(monkeypatch, tmp_path, confirm, disappear, status):
    def configure(n):
        n.minutes, n.confirm, n.disappear, n.claim_status = 60, confirm, disappear, status
    code, report, network, journal = scenario(monkeypatch, tmp_path, configure=configure)
    assert len(network.claims) == 1
    assert report["state"] == ("claim_confirmed" if confirm else "claim_unconfirmed")
    assert code == (0 if confirm else 1)
    assert journal["outcome"] == ("confirmed" if confirm else "attempted")


@pytest.mark.parametrize("claimed", [False, True])
def test_existing_attempt_allows_only_read_only_reconciliation(monkeypatch, tmp_path, claimed):
    code, report, network, journal = scenario(monkeypatch, tmp_path, previous=True,
                                            configure=lambda n: setattr(n, "initial_claimed", claimed))
    assert code == (0 if claimed else 1)
    assert len(network.calls) == 2 and not network.sends and not network.claims
    assert report["claim"]["previous_attempt"] is True and report["claim"]["attempted"] is False
    assert journal["outcome"] == ("confirmed" if claimed else "attempted")


def test_lost_claim_response_survives_restart_without_replay(monkeypatch, tmp_path):
    def configure(n):
        n.minutes = 60
        n.responses[CLAIM_OP] = asyncio.TimeoutError(TOKEN)
    code, report, first, saved = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and len(first.claims) == 1 and saved["outcome"] == "attempted"
    code, report, second, saved = scenario(monkeypatch, tmp_path,
                                          configure=lambda n: setattr(n, "initial_claimed", True))
    assert code == 0 and report["state"] == "claim_confirmed"
    assert report["claim"]["previous_attempt"] is True and not second.claims and not second.sends
    assert len(second.calls) == 2 and saved["outcome"] == "confirmed"


@pytest.mark.parametrize("condition", ["isAccountConnected", "hasPreconditionsMet"])
def test_explicit_false_eligibility_stops_ready_drop(monkeypatch, tmp_path, condition):
    def configure(n):
        n.minutes = 60
        original = n.inventory
        def inventory():
            body = original()
            campaign = body["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]
            if condition == "isAccountConnected":
                campaign["self"] = {condition: False}
            else:
                campaign["timeBasedDrops"][0]["self"][condition] = False
            return body
        n.inventory = inventory
    code, report, network, saved = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and not network.claims and not network.sends and saved is None
    assert report["error"] in {"account_link_not_confirmed", "target_preconditions_not_met"}


def test_journal_failure_prevents_claim(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, journal_failure=True,
                                          configure=lambda n: setattr(n, "minutes", 60))
    assert code == 1 and report["error"] == "fixture_journal_write_failure"
    assert not network.claims and saved is None


def test_lock_conflict_stops_before_opening_network(monkeypatch, tmp_path):
    code, report, network, _ = scenario(monkeypatch, tmp_path, locked=True)
    assert code == 1 and report["error"] == "finish_journal_locked"
    assert not network.sessions and not network.calls


@pytest.mark.parametrize("field,error", [
    ("changed_stream", "stream_changed"), ("changed_game", "stream_changed"),
    ("changed_user", "validation_user_changed"),
])
def test_periodic_identity_changes_stop_without_claim(monkeypatch, tmp_path, field, error):
    code, report, network, saved = scenario(monkeypatch, tmp_path, configure=lambda n: setattr(n, field, True))
    assert code == 1 and report["error"] == error and report["elapsed_seconds"] == 300
    assert not network.claims and saved is None


def test_total_deadline_includes_cookie_open_and_validation(monkeypatch, tmp_path):
    code, report, network, _ = scenario(monkeypatch, tmp_path, configure=lambda n: setattr(n, "slow_open", True))
    assert code == 1 and report["error"] == "total_time_exhausted"
    assert len(network.calls) == 1 and not network.sends


@pytest.mark.parametrize("field", ["slow_page", "slow_periodic", None])
def test_finish_reserve_has_no_watch_posts_or_busy_loop(monkeypatch, tmp_path, field):
    def configure(n):
        if field:
            setattr(n, field, True)
    code, report, network, saved = scenario(monkeypatch, tmp_path, seconds=500, configure=configure)
    assert code == 1 and not network.claims and saved is None
    assert all(at < 380 for at in network.sends)
    assert report["state"] in {"incomplete", "failed"}
    assert report["elapsed_seconds"] <= 800


def test_request_budget_is_hard_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(finish, "MAX_REQUESTS", 7)
    code, report, network, saved = scenario(monkeypatch, tmp_path)
    assert code == 1 and report["error"] == "request_budget_exhausted"
    assert len(network.calls) == 7 and not network.claims and saved is None


def test_unarmed_claim_operation_is_rejected():
    async def run():
        clock = Clock()
        client = finish.FinishClient({"requests": []}, None, clock, 300)
        with pytest.raises(WatchCheckError, match="claim_not_authorized"):
            await client.gql_request(GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}}))
    asyncio.run(run())


@pytest.mark.parametrize("claimed", [False, True])
def test_explicit_reconcile_has_two_reads_and_no_watching_or_claiming(monkeypatch, tmp_path, claimed):
    code, report, network, saved = scenario(
        monkeypatch, tmp_path, previous=True, reconcile_only=True,
        configure=lambda n: setattr(n, "initial_claimed", claimed),
    )
    assert code == (0 if claimed else 1) and report["mode"] == "reconcile_claim"
    assert report["limits"] == {"total_seconds": 120, "requests": 2}
    assert len(network.calls) == 2 and not network.sends and not network.claims
    assert report["claim"]["previous_attempt"] is True
    assert saved["outcome"] == ("confirmed" if claimed else "attempted")


def test_explicit_reconcile_missing_journal_never_opens_network(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, reconcile_only=True)
    assert code == 1 and report["error"] == "reconcile_journal_missing"
    assert not network.sessions and not network.calls and saved is None


def test_reconcile_120_second_budget_is_valid(monkeypatch, tmp_path):
    _, report, network, _ = scenario(monkeypatch, tmp_path, previous=True, reconcile_only=True, seconds=120)
    assert report["error"] == "previous_claim_not_confirmed" and len(network.calls) == 2


def test_reconcile_stops_on_inventory_challenge_without_retry(monkeypatch, tmp_path):
    def configure(n):
        n.responses["Inventory"] = Response({"extensions": {"challenge": {"type": "integrity", "secret": TOKEN}}})
    code, report, network, saved = scenario(monkeypatch, tmp_path, previous=True, reconcile_only=True, configure=configure)
    assert code == 1 and report["state"] == "claim_unconfirmed" and report["error"] == "gql_challenge"
    assert len(network.calls) == 2 and not network.claims and saved["outcome"] == "attempted"
    assert report["requests"][-1]["response_challenge"] == {"present": True, "type": "integrity"}


def test_reconcile_guards_block_even_an_armed_mutation_or_watch_request():
    async def run():
        client = finish.FinishClient({"requests": []}, None, Clock(), 120, reconcile_only=True)
        client._claim_authorization = CLAIM_ID
        with pytest.raises(WatchCheckError, match="reconcile_operation_not_allowed"):
            await client.gql_request(GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}}))
        with pytest.raises(WatchCheckError, match="reconcile_request_not_allowed"):
            async with client.request("POST", "https://spade.twitch.tv/track", data={}):
                pytest.fail("watch transport must not open")
        assert client._session is None
    asyncio.run(run())
