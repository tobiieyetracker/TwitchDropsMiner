import asyncio
import json
from http.cookies import SimpleCookie

import aiohttp
import pytest

import check_web_claim as candidate
from check_channel_watch import WEB
from constants import GQL_QUERIES
from finish_drop_journal import FinishJournal
from inventory import BaseDrop
from test_channel_watch_check import Clock, Response, TOKEN
from test_finish_channel_drop import CLAIM_ID, CLAIM_OP, Network
from web_claim_journal import WebClaimJournal
from watch_check_state import WatchCheckError


def scenario(monkeypatch, tmp_path, *, configure=None, original=True, original_confirmed=False,
             prior_candidate=False, original_user="42", journal_failure=False):
    async def run():
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["auth-token"] = TOKEN
        cookie["auth-token"]["domain"] = ".twitch.tv"
        cookie["auth-token"]["path"] = "/"
        jar.update_cookies(cookie, WEB)
        cookie_file = tmp_path / "cookies.jar"
        jar.save(cookie_file)
        cookie_bytes = cookie_file.read_bytes()
        state_dir = tmp_path / "state"
        original_path = state_dir / "journal.json"
        if original and not original_path.exists():
            with FinishJournal(state_dir) as journal:
                journal.record_attempt(original_user, "campaign", "Test campaign", "drop")
                if original_confirmed:
                    journal.confirm()
        original_bytes = original_path.read_bytes() if original_path.exists() else None
        if prior_candidate:
            with WebClaimJournal(state_dir) as journal:
                journal.record_attempt(original_user, "campaign", "Test campaign", "drop")
        clock = Clock()
        network = Network(clock)
        network.minutes = 60
        if configure:
            configure(network)
        if journal_failure:
            def fail(*args):
                raise WatchCheckError("fixture_journal_write_failed")
            monkeypatch.setattr(WebClaimJournal, "record_attempt", fail)
        monkeypatch.setattr(aiohttp, "ClientSession", network.session)
        code, report = await candidate.check(cookie_file, "Test campaign", state_dir=state_dir, clock=clock)
        assert cookie_file.read_bytes() == cookie_bytes
        assert (original_path.read_bytes() if original_path.exists() else None) == original_bytes
        assert all(s.closed for s in network.sessions)
        encoded = json.dumps(report)
        assert TOKEN not in encoded and CLAIM_ID not in encoded and '"42"' not in encoded
        candidate_path = state_dir / "web-query-claim-v1.json"
        saved = json.loads(candidate_path.read_text()) if candidate_path.exists() else None
        if saved:
            assert TOKEN not in json.dumps(saved) and CLAIM_ID not in json.dumps(saved)
        assert not network.sends
        return code, report, network, saved
    return asyncio.run(run())


def test_one_candidate_uses_real_base_drop_and_only_changes_wire_hash(monkeypatch, tmp_path):
    original_claim = BaseDrop._claim
    invocations = []
    async def claim(self):
        invocations.append(self)
        return await original_claim(self)
    monkeypatch.setattr(BaseDrop, "_claim", claim)
    code, report, network, saved = scenario(monkeypatch, tmp_path)
    assert code == 0 and report["state"] == "claim_confirmed"
    assert len(invocations) == len(network.claims) == 1
    actual = network.claims[0]
    assert actual == candidate.WEB_CLAIM_QUERY.with_variables({"input": {"dropInstanceID": CLAIM_ID}})
    old = GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}})
    assert actual["operationName"] == old["operationName"] and actual["variables"] == old["variables"]
    assert actual["extensions"]["persistedQuery"]["sha256Hash"] != old["extensions"]["persistedQuery"]["sha256Hash"]
    assert GQL_QUERIES["ClaimDrop"]["extensions"]["persistedQuery"]["sha256Hash"].startswith("a455deea")
    assert 1 <= len(network.calls) <= candidate.MAX_REQUESTS
    assert len(network.sessions) == 1 and saved["outcome"] == "confirmed"
    assert all(r["auth_cookie_matches"] is True and r["web_ua_matches"] is True for r in report["requests"])
    assert all(r["integrity_header_present"] is False for r in report["requests"])
    assert report["inventory_checks"][-1]["drops"][0]["is_claimed"] is True


@pytest.mark.parametrize("original,confirmed,error", [
    (False, False, "web_claim_original_missing"),
    (True, True, "web_claim_original_already_confirmed"),
])
def test_original_attempt_is_required_before_network(monkeypatch, tmp_path, original, confirmed, error):
    code, report, network, saved = scenario(monkeypatch, tmp_path, original=original, original_confirmed=confirmed)
    assert code == 1 and report["error"] == error and not network.calls and not network.sessions
    assert saved is None


def test_account_binding_stops_before_inventory(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, original_user="43")
    assert code == 1 and report["error"] == "web_claim_original_account_mismatch"
    assert len(network.calls) == 1 and not network.claims and saved is None


@pytest.mark.parametrize("minutes,claim_id,error", [
    (59, CLAIM_ID, "target_progress_incomplete"),
    (60, None, "target_claim_id_unavailable"),
    (60, "", "target_claim_id_unavailable"),
])
def test_no_candidate_without_real_server_readiness(monkeypatch, tmp_path, minutes, claim_id, error):
    def configure(n):
        n.minutes, n.claim_id = minutes, claim_id
    code, report, network, saved = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and report["error"] == error and len(network.calls) == 2
    assert not network.claims and saved is None


def test_already_claimed_is_read_only_and_creates_no_candidate_record(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path,
                                          configure=lambda n: setattr(n, "initial_claimed", True))
    assert code == 0 and report["state"] == "already_claimed" and len(network.calls) == 2
    assert not network.claims and saved is None


@pytest.mark.parametrize("failure,error", [
    (Response({"extensions": {"challenge": {"type": "integrity", "private": TOKEN}}}), "gql_challenge"),
    (Response({"extensions": {"challenge": {"type": "unrecognized-private-type"}}}), "gql_challenge"),
    (Response({}, 429), "http_rate_limited"),
    (Response({}, 302), "http_redirect_stopped"),
    (asyncio.TimeoutError(TOKEN), "TimeoutError"),
])
def test_challenge_and_transport_failures_stop_without_replay(monkeypatch, tmp_path, failure, error):
    def configure(n):
        n.responses[CLAIM_OP] = failure
    code, report, network, saved = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and report["state"] == "claim_unconfirmed" and report["error"] == error
    assert len(network.claims) == 1 and saved["outcome"] == "attempted"
    assert network.calls[-1][3]["json"]["operationName"] == CLAIM_OP
    assert not any(r["phase"] == "claim_confirmation" for r in report["requests"])
    assert "unrecognized-private-type" not in json.dumps(report)
    if error == "gql_challenge":
        assert report["claim"]["response_challenge"]["type"] in {"integrity", "other"}


@pytest.mark.parametrize("claimed", [False, True])
def test_existing_candidate_can_only_reconcile(monkeypatch, tmp_path, claimed):
    code, report, network, saved = scenario(monkeypatch, tmp_path, prior_candidate=True,
                                          configure=lambda n: setattr(n, "initial_claimed", claimed))
    assert code == (0 if claimed else 1) and report["mode"] == "web_query_claim_reconcile"
    assert report["claim"]["previous_attempt"] is True and not network.claims
    assert len(network.calls) == 2 and report["limits"]["requests"] == 2
    assert saved["outcome"] == ("confirmed" if claimed else "attempted")


def test_lost_candidate_response_does_not_enable_another_candidate(monkeypatch, tmp_path):
    def configure(n):
        n.responses[CLAIM_OP] = asyncio.TimeoutError()
    code, report, first, saved = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and len(first.claims) == 1 and saved["outcome"] == "attempted"
    code, report, second, saved = scenario(monkeypatch, tmp_path)
    assert code == 1 and report["claim"]["previous_attempt"] is True
    assert len(second.calls) == 2 and not second.claims and saved["outcome"] == "attempted"


def test_candidate_journal_failure_prevents_mutation(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, journal_failure=True)
    assert code == 1 and report["error"] == "fixture_journal_write_failed"
    assert not network.claims and saved is None


def test_accepted_response_still_needs_inventory_confirmation(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path,
                                          configure=lambda n: setattr(n, "confirm", False))
    assert code == 1 and report["state"] == "claim_unconfirmed" and saved["outcome"] == "attempted"
    assert len(network.claims) == 1 and len(network.calls) == 8


def test_direct_transport_and_unarmed_gql_cannot_submit_claim():
    async def run():
        client = candidate.WebClaimClient({"requests": []}, None, Clock(), 240)
        payload = candidate.WEB_CLAIM_QUERY.with_variables({"input": {"dropInstanceID": CLAIM_ID}})
        with pytest.raises(WatchCheckError, match="web_claim_request_not_allowed"):
            async with client.request("POST", candidate.GQL, json=payload):
                pytest.fail("direct claim must not open")
        with pytest.raises(WatchCheckError, match="claim_not_authorized"):
            await client.gql_request(GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}}))
        with pytest.raises(WatchCheckError, match="web_claim_request_not_allowed"):
            async with client.request("POST", "https://spade.twitch.tv/track", data={}):
                pytest.fail("watching must not open")
        assert client._session is None
    asyncio.run(run())


def test_total_budget_also_applies_to_open_validation(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path,
                                          configure=lambda n: setattr(n, "slow_open", True))
    assert code == 1 and report["error"] == "total_time_exhausted"
    assert len(network.calls) == 1 and not network.claims and saved is None
