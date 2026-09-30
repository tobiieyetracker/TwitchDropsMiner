"""The SMARTBOX candidate uses the real miner transport and claim implementation.

Only HTTP is simulated. The existing finish-drop inventory fixture is shared,
with a TV validation response and a wrapper that observes verified TLS settings.
"""
import asyncio
import json
import ssl
from contextlib import asynccontextmanager

import aiohttp
import pytest

import check_smartbox_claim as candidate
from check_channel_watch import GQL, VALIDATE, WEB
from constants import ClientType, GQL_QUERIES
from finish_drop_journal import FinishJournal
from inventory import BaseDrop
from smartbox_claim_journal import SmartboxClaimJournal
from test_channel_watch_check import Clock, Response, TOKEN
from test_finish_channel_drop import CLAIM_ID, CLAIM_OP, Network
from watch_check_state import WatchCheckError


TV = ClientType.SMARTBOX.CLIENT_URL
DEVICE = "fixture-private-device"


class TVNetwork(Network):
    def __init__(self, clock):
        super().__init__(clock)
        self.minutes = 60
        self.tls_contexts = []
        self.responses[VALIDATE.host] = Response({
            "client_id": ClientType.SMARTBOX.CLIENT_ID, "user_id": "42",
        })
        self.close_error = False

    def session(self, **kwargs):
        session = super().session(**kwargs)
        original_request, original_close = session.request, session.close

        @asynccontextmanager
        async def request(method, url, **options):
            # The older fixture forbids TLS overrides. Preserve its checks after
            # verifying that this candidate supplied its fresh verified context.
            tls = options.pop("ssl", None)
            assert isinstance(tls, ssl.SSLContext)
            assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
            self.tls_contexts.append(tls)
            async with original_request(method, url, **options) as response:
                yield response

        async def close():
            await original_close()
            if self.close_error:
                raise RuntimeError(TOKEN)

        session.request, session.close = request, close
        return session


def scenario(monkeypatch, tmp_path, *, claim=False, configure=None, original=True,
             original_user="42", original_confirmed=False, previous=False,
             cookie_origin=TV, conflicting_gql_cookie=False, journal_failure=False):
    async def run():
        jar = aiohttp.CookieJar()
        jar.update_cookies({"auth-token": TOKEN, "unique_id": DEVICE}, cookie_origin)
        if conflicting_gql_cookie:
            jar.update_cookies({"auth-token": "fixture-other-private-token"}, GQL)
        cookie_file = tmp_path / "cookies.jar.bak"
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
        # Preserve other candidates byte-for-byte; no credentials are involved.
        state_dir.mkdir(exist_ok=True)
        other_path = state_dir / "web-query-claim-v1.json"
        if not other_path.exists():
            other_path.write_text('{"existing":"preserve"}\n', encoding="utf-8")
        other_bytes = other_path.read_bytes()
        if previous:
            with SmartboxClaimJournal(state_dir) as journal:
                journal.record_attempt(original_user, "campaign", "Test campaign", "drop")
        clock, network = Clock(), None
        network = TVNetwork(clock)
        if configure:
            configure(network)
        if journal_failure:
            def fail(*args):
                raise WatchCheckError("fixture_journal_write_failed")
            monkeypatch.setattr(SmartboxClaimJournal, "record_attempt", fail)
        monkeypatch.setattr(aiohttp, "ClientSession", network.session)
        code, report = await candidate.check(
            cookie_file, "Test campaign", state_dir=state_dir, clock=clock, claim=claim,
        )
        assert cookie_file.read_bytes() == cookie_bytes
        assert (original_path.read_bytes() if original_path.exists() else None) == original_bytes
        assert other_path.read_bytes() == other_bytes
        assert all(session.closed for session in network.sessions)
        assert not network.sends and report["watch_sends"] == 0
        output = json.dumps(report)
        for secret in (TOKEN, CLAIM_ID, DEVICE, "fixture-other-private-token", '"42"'):
            assert secret not in output
        candidate_path = state_dir / "smartbox-claim-v1.json"
        saved = json.loads(candidate_path.read_text()) if candidate_path.exists() else None
        if saved:
            assert TOKEN not in json.dumps(saved) and CLAIM_ID not in json.dumps(saved)
        return code, report, network, saved
    return asyncio.run(run())


def test_default_preflight_only_validates_and_reads_inventory(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path)
    assert code == 0 and report["state"] == "preflight_ready"
    assert saved is None and not network.claims
    assert [(method, url) for _, method, url, _ in network.calls] == [
        ("GET", str(VALIDATE)), ("POST", str(GQL)),
    ]
    assert network.calls[1][3]["json"] == GQL_QUERIES["Inventory"]
    assert report["smartbox_token_valid"] is True
    assert report["target"]["account_link_state"] is None
    assert report["target"]["preconditions_met"] is None
    assert len({id(context) for context in network.tls_contexts}) == 1
    for request in report["requests"]:
        assert request["oauth_matches"] is True
        assert request["smartbox_ua_matches"] is True
        assert request["integrity_header_present"] is False
        # A TV host-only Cookie must not be widened to gql.twitch.tv.
        assert request["auth_cookie_sent"] is False
    assert report["requests"][1]["smartbox_client_matches"] is True


def test_claim_reuses_original_method_hash_and_fresh_inventory_instance(monkeypatch, tmp_path):
    original_claim, invocations = BaseDrop._claim, []
    async def claim(self):
        invocations.append(self)
        return await original_claim(self)
    monkeypatch.setattr(BaseDrop, "_claim", claim)
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True)
    assert code == 0 and report["state"] == "claim_confirmed"
    assert len(invocations) == len(network.claims) == 1
    assert network.claims[0] == GQL_QUERIES["ClaimDrop"].with_variables({
        "input": {"dropInstanceID": CLAIM_ID},
    })
    assert [(method, options.get("json", {}).get("operationName"))
            for _, method, _, options in network.calls[:5]] == [
        ("GET", None), ("POST", "Inventory"), ("GET", None),
        ("POST", "Inventory"), ("POST", CLAIM_OP),
    ]
    assert saved["outcome"] == "confirmed"
    assert report["inventory_checks"][-1]["drops"][0]["is_claimed"] is True
    assert 1 <= len(network.calls) <= 8
    assert len(network.sessions) == 1
    assert all(request["smartbox_client_matches"] is True
               for request in report["requests"] if request["method"] == "POST")


@pytest.mark.parametrize("issuer", [ClientType.WEB.CLIENT_ID, ClientType.ANDROID_APP.CLIENT_ID, None])
def test_wrong_issuer_stops_before_inventory(monkeypatch, tmp_path, issuer):
    def configure(network):
        network.responses[VALIDATE.host] = Response({"client_id": issuer, "user_id": "42"})
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and len(network.calls) == 1 and saved is None and not network.claims
    assert report["smartbox_token_valid"] is False


def test_account_must_match_original_attempt(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, original_user="43")
    assert code == 1 and len(network.calls) == 1 and saved is None and not network.claims


@pytest.mark.parametrize("original,confirmed", [(False, False), (True, True)])
def test_missing_or_confirmed_original_never_claims(monkeypatch, tmp_path, original, confirmed):
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True,
                                          original=original, original_confirmed=confirmed)
    assert not network.claims and saved is None


def test_wrong_cookie_origin_is_not_copied_to_tv(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, cookie_origin=WEB)
    assert code == 1 and not network.calls and saved is None


def test_conflicting_actual_gql_cookie_stops_before_gql(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, conflicting_gql_cookie=True)
    assert code == 1 and len(network.calls) == 1 and not network.claims and saved is None


@pytest.mark.parametrize("minutes,claim_id", [(59, CLAIM_ID), (60, None), (60, "")])
def test_real_readiness_required_before_candidate_attempt(monkeypatch, tmp_path, minutes, claim_id):
    def configure(network):
        network.minutes, network.claim_id = minutes, claim_id
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and not network.claims and saved is None and len(network.calls) == 2


@pytest.mark.parametrize("field", ["isAccountConnected", "hasPreconditionsMet"])
def test_explicit_false_eligibility_stops(monkeypatch, tmp_path, field):
    def configure(network):
        original_inventory = network.inventory
        def inventory():
            body = original_inventory()
            campaign = body["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]
            if field == "isAccountConnected":
                campaign["self"] = {field: False}
            else:
                campaign["timeBasedDrops"][0]["self"][field] = False
            return body
        network.inventory = inventory
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and not network.claims and saved is None


def test_campaign_id_must_match_original_even_if_name_matches(monkeypatch, tmp_path):
    def configure(network):
        original_inventory = network.inventory
        def inventory():
            body = original_inventory()
            body["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]["id"] = "different"
            return body
        network.inventory = inventory
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and len(network.calls) == 2 and not network.claims and saved is None


@pytest.mark.parametrize("change,error", [
    ("instance", "target_claim_id_changed"),
    ("campaign", "target_campaign_identity_changed"),
    ("minutes", "target_minutes_regressed"),
])
def test_latest_inventory_changes_stop_before_using_preflight_state(monkeypatch, tmp_path, change, error):
    def configure(network):
        original_inventory, reads = network.inventory, 0
        def inventory():
            nonlocal reads
            reads += 1
            body = original_inventory()
            if reads > 1:
                campaign = body["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]
                own = campaign["timeBasedDrops"][0]["self"]
                if change == "instance":
                    own["dropInstanceID"] = "fixture-new-private-instance"
                elif change == "campaign":
                    campaign["id"] = "replacement-campaign"
                else:
                    own["currentMinutesWatched"] = 59
            return body
        network.inventory = inventory
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and report["error"] == error
    assert len(network.calls) == 4 and not network.claims and saved is None
    assert "fixture-new-private-instance" not in json.dumps(report)


def test_account_change_at_submission_validation_stops(monkeypatch, tmp_path):
    def configure(network):
        class ChangingValidation(Response):
            calls = 0
            async def json(self):
                self.calls += 1
                return {"client_id": ClientType.SMARTBOX.CLIENT_ID,
                        "user_id": "42" if self.calls == 1 else "43"}
        network.responses[VALIDATE.host] = ChangingValidation({})
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and report["error"] == "validation_user_changed"
    assert len(network.calls) == 3 and not network.claims and saved is None


@pytest.mark.parametrize("claim", [False, True])
@pytest.mark.parametrize("claimed", [False, True])
def test_existing_candidate_is_always_read_only_reconciliation(monkeypatch, tmp_path, claim, claimed):
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=claim, previous=True,
        configure=lambda network: setattr(network, "initial_claimed", claimed))
    assert code == (0 if claimed else 1)
    assert report["claim"]["previous_attempt"] is True
    assert report["claim"]["attempted"] is False
    assert len(network.calls) == 2 and not network.claims
    assert saved["outcome"] == ("confirmed" if claimed else "attempted")


@pytest.mark.parametrize("failure", [
    Response({"extensions": {"challenge": {"type": "integrity", "private": TOKEN}}}),
    Response({}, 429), Response({}, 302), asyncio.TimeoutError(TOKEN),
])
def test_claim_failure_preserves_attempt_without_replay(monkeypatch, tmp_path, failure):
    def configure(network):
        network.responses[CLAIM_OP] = failure
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and report["state"] == "claim_unconfirmed"
    assert len(network.claims) == 1 and saved["outcome"] == "attempted"
    assert network.calls[-1][3]["json"]["operationName"] == CLAIM_OP
    # Restarting with --claim must use the existing record, never resubmit.
    code, report, restarted, saved = scenario(monkeypatch, tmp_path, claim=True)
    assert code == 1 and len(restarted.calls) == 2 and not restarted.claims


def test_journal_failure_prevents_mutation(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, journal_failure=True)
    assert code == 1 and not network.claims and saved is None


@pytest.mark.parametrize("disappear", [False, True])
def test_http_success_or_target_disappearance_is_not_claim_confirmation(monkeypatch, tmp_path, disappear):
    def configure(network):
        network.confirm, network.disappear = False, disappear
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True, configure=configure)
    assert code == 1 and report["state"] == "claim_unconfirmed"
    assert len(network.claims) == 1 and saved["outcome"] == "attempted"
    assert len(network.calls) == 8


def test_already_claimed_preflight_does_not_create_candidate(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path, claim=True,
        configure=lambda network: setattr(network, "initial_claimed", True))
    assert code == 0 and report["state"] == "already_claimed"
    assert len(network.calls) == 2 and not network.claims and saved is None


def test_cleanup_failure_is_reported_and_not_success(monkeypatch, tmp_path):
    code, report, network, saved = scenario(monkeypatch, tmp_path,
        configure=lambda network: setattr(network, "close_error", True))
    assert code == 1 and (report.get("cleanup_error") or report.get("error"))
    assert not network.claims and saved is None


def test_exact_operation_and_http_guards_reject_direct_calls():
    async def run():
        report = {"requests": [], "claim": {}}
        clock = Clock()
        client = candidate.SmartboxClaimClient(report, None, clock, 240)
        for operation in (GQL_QUERIES["Campaigns"], GQL_QUERIES["AvailableDrops"],
                          GQL_QUERIES["ClaimDrop"].with_variables({"input": {"dropInstanceID": CLAIM_ID}})):
            with pytest.raises(WatchCheckError):
                await client.gql_request(operation)
        async def forbidden(method, url, **kwargs):
            with pytest.raises(WatchCheckError):
                async with client.request(method, url, **kwargs):
                    pytest.fail("unauthorized HTTP reached transport")
        await forbidden("POST", GQL, json=GQL_QUERIES["AvailableDrops"])
        await forbidden("POST", GQL, json=GQL_QUERIES["ClaimDrop"].with_variables({
            "input": {"dropInstanceID": CLAIM_ID}}))
        await forbidden("POST", "https://gql.twitch.tv/integrity")
        assert not report["requests"]
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("Authorization", "OAuth fixture-wrong-token"),
    ("Client-Id", ClientType.WEB.CLIENT_ID),
    ("User-Agent", ClientType.WEB.USER_AGENT),
    ("Origin", str(WEB)), ("Referer", str(WEB)),
    ("Client-Integrity", "fixture-private-integrity"),
])
def test_http_identity_guard_rejects_mismatched_profile_before_transport(field, value):
    async def run():
        report = {"requests": [], "claim": {}}
        client = candidate.SmartboxClaimClient(report, None, Clock(), 240)
        client.token = TOKEN
        headers = {"Authorization": f"OAuth {TOKEN}", "Client-Id": ClientType.SMARTBOX.CLIENT_ID,
                   "User-Agent": ClientType.SMARTBOX.USER_AGENT, "Origin": str(TV), "Referer": str(TV)}
        headers[field] = value
        with pytest.raises(WatchCheckError, match="smartbox_claim_identity_headers_invalid"):
            async with client.request("POST", GQL, json=GQL_QUERIES["Inventory"], headers=headers):
                pytest.fail("invalid identity reached transport")
        assert not report["requests"]
    asyncio.run(run())
