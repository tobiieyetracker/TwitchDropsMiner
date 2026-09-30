"""HTTP-level read-only award reconciliation; no mutation or journal updates."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

import check_smartbox_awards as awards
from check_channel_watch import GQL, VALIDATE
from constants import ClientType, GQL_QUERIES
from finish_drop_journal import FinishJournal
from smartbox_claim_journal import SmartboxClaimJournal
from test_channel_watch_check import Clock, Response, TOKEN
from test_smartbox_claim import DEVICE, TV, TVNetwork


BENEFIT = "target-public-benefit"
SECOND_BENEFIT = "second-target-public-benefit"
UNRELATED = "do-not-report-this-unrelated-reward"
METADATA = {"campaign_id": "campaign", "campaign_name": "Test campaign",
            "drop_id": "drop", "benefit_ids": [BENEFIT]}


def scenario(monkeypatch, tmp_path, *, shape="legacy", configure=None, metadata=None,
             original=True, prior=True, original_user="42", award_offset=30,
             candidate_confirmed=False):
    async def run():
        cookie_file = tmp_path / "cookies.jar.bak"
        jar = aiohttp.CookieJar()
        jar.update_cookies({"auth-token": TOKEN, "unique_id": DEVICE}, TV)
        jar.save(cookie_file)
        cookie_before = cookie_file.read_bytes()
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        if original:
            with FinishJournal(state_dir) as journal:
                journal.record_attempt(original_user, "campaign", "Test campaign", "drop")
        if prior and original:
            with SmartboxClaimJournal(state_dir) as journal:
                journal.record_attempt(original_user, "campaign", "Test campaign", "drop")
                if candidate_confirmed:
                    journal.confirm()
        paths = [state_dir / "journal.json", state_dir / "smartbox-claim-v1.json"]
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        prior_record = json.loads(before[paths[1]]) if before[paths[1]] else None
        attempted_at = (datetime.fromisoformat(prior_record["attempted_at"]) if prior_record
                        else datetime.now(timezone.utc))
        checked_at = attempted_at + timedelta(seconds=120)
        award_at = (attempted_at + timedelta(seconds=award_offset)).isoformat()
        monkeypatch.setattr(awards, "utc_now", lambda: checked_at)
        monkeypatch.setattr(awards, "load_metadata", lambda: dict(metadata or METADATA))
        clock, network = Clock(), None
        network = TVNetwork(clock)
        original_inventory = network.inventory
        def inventory():
            body = original_inventory()
            inv = body["data"]["currentUser"]["inventory"]
            inv["dropCampaignsInProgress"] = []
            entries = [{"id": BENEFIT, "lastAwardedAt": award_at},
                       {"id": UNRELATED, "lastAwardedAt": award_at}]
            if shape == "legacy":
                inv["gameEventDrops"] = entries
            else:
                inv["gameEventDropsConnection"] = {
                    "edges": [{"node": {"id": "not-a-benefit-id", "benefit": {"id": item["id"]},
                                         "lastAwardedAt": item["lastAwardedAt"]}} for item in entries],
                    "pageInfo": {"hasNextPage": True},
                }
            return body
        network.inventory = inventory
        if configure:
            configure(network, attempted_at, checked_at)
        monkeypatch.setattr(aiohttp, "ClientSession", network.session)
        # Runtime guards complement byte comparison: even confirming the same
        # record must never be part of this readonly entry point.
        def forbidden(*args, **kwargs):
            pytest.fail("read-only awards helper attempted a journal write")
        monkeypatch.setattr(SmartboxClaimJournal, "record_attempt", forbidden)
        monkeypatch.setattr(SmartboxClaimJournal, "confirm", forbidden)
        code, report = await awards.check(cookie_file, state_dir=state_dir, clock=clock)
        assert cookie_file.read_bytes() == cookie_before
        assert all((path.read_bytes() if path.exists() else None) == content for path, content in before.items())
        assert all(session.closed for session in network.sessions)
        assert not network.claims and not network.sends
        assert report["claim_attempted"] is False and report["journal_modified"] is False
        output = json.dumps(report)
        for secret in (TOKEN, DEVICE, UNRELATED, '"42"', "fixture-private-instance-id"):
            assert secret not in output
        assert report["state"] != "claim_confirmed"
        assert len(network.calls) <= 2
        assert all(call[3].get("json") == GQL_QUERIES["Inventory"]
                   for call in network.calls if call[1] == "POST")
        return code, report, network
    return asyncio.run(run())


def inventory_change(mutator):
    def configure(network, attempted_at, checked_at):
        original_inventory = network.inventory
        def inventory():
            body = original_inventory()
            mutator(body["data"]["currentUser"]["inventory"], attempted_at, checked_at)
            return body
        network.inventory = inventory
    return configure


@pytest.mark.parametrize("shape", ["legacy", "connection"])
def test_award_observed_with_absent_in_progress_target(monkeypatch, tmp_path, shape):
    code, report, network = scenario(monkeypatch, tmp_path, shape=shape)
    assert code == 0 and report["state"] == "reward_grant_observed"
    assert report["inventory_target"]["target_state"] == "absent"
    assert report["awards"]["all_benefits_present"] is True
    assert report["awards"]["all_awards_in_window"] is True
    assert len(report["awards"]["matched_benefits"]) == 1
    assert report["evidence_limits"]["pre_claim_awards_baseline"] is False
    assert report["evidence_limits"]["unique_mutation_attribution"] is False
    assert report["evidence_limits"]["claim_isClaimed_confirmation"] is False
    assert [(method, url) for _, method, url, _ in network.calls] == [
        ("GET", str(VALIDATE)), ("POST", str(GQL)),
    ]
    assert report["requests"][1]["smartbox_client_matches"] is True
    assert len(network.tls_contexts) == 2


@pytest.mark.parametrize("prior", [False, True])
def test_missing_original_or_candidate_stops_before_network(monkeypatch, tmp_path, prior):
    code, report, network = scenario(monkeypatch, tmp_path, original=not prior, prior=prior)
    assert code == 1 and not network.calls


@pytest.mark.parametrize("field", ["campaign_id", "campaign_name", "drop_id"])
def test_fixed_metadata_must_match_journals_before_network(monkeypatch, tmp_path, field):
    metadata = {**METADATA, field: "not-the-original-target"}
    code, report, network = scenario(monkeypatch, tmp_path, metadata=metadata)
    assert code == 1 and report["error"] == "smartbox_awards_target_mismatch" and not network.calls


def test_same_account_required_before_inventory(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, original_user="43")
    assert code == 1 and report["error"] == "smartbox_awards_account_mismatch"
    assert len(network.calls) == 1


def test_wrong_token_issuer_stops_before_inventory(monkeypatch, tmp_path):
    def configure(network, *_):
        network.responses[VALIDATE.host] = Response({"client_id": ClientType.WEB.CLIENT_ID, "user_id": "42"})
    code, report, network = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and len(network.calls) == 1


@pytest.mark.parametrize("offset", [-1, 121])
def test_old_or_future_award_does_not_confirm_this_window(monkeypatch, tmp_path, offset):
    code, report, network = scenario(monkeypatch, tmp_path, award_offset=offset)
    assert code == 1 and report["state"] == "reward_grant_unconfirmed"
    assert report["awards"]["all_benefits_present"] is True
    assert report["awards"]["all_awards_in_window"] is False


@pytest.mark.parametrize("offset", [0, 120])
def test_window_boundaries_are_inclusive(monkeypatch, tmp_path, offset):
    code, report, network = scenario(monkeypatch, tmp_path, award_offset=offset)
    assert code == 0 and report["state"] == "reward_grant_observed"


@pytest.mark.parametrize("timestamp", [None, "", "2026-10-01", "2026-10-01 00:00:00Z", "2026-99-99T00:00:00Z", "private-invalid-time"])
def test_missing_or_malformed_target_timestamp_is_unconfirmed(monkeypatch, tmp_path, timestamp):
    def mutate(inv, *_):
        inv["gameEventDrops"][0]["lastAwardedAt"] = timestamp
    code, report, network = scenario(monkeypatch, tmp_path, configure=inventory_change(mutate))
    assert code == 1 and report["state"] == "reward_grant_unconfirmed"
    assert report["error"] == "award_timestamp_invalid"
    assert "private-invalid-time" not in json.dumps(report)


@pytest.mark.parametrize("kind,expected", [("missing", "missing"), ("null", "null"), ("invalid", "invalid"), ("empty", "list")])
def test_unknown_award_list_is_not_reported_as_empty(monkeypatch, tmp_path, kind, expected):
    def mutate(inv, *_):
        if kind == "missing":
            del inv["gameEventDrops"]
        else:
            inv["gameEventDrops"] = {"null": None, "invalid": {}, "empty": []}[kind]
    code, report, network = scenario(monkeypatch, tmp_path, configure=inventory_change(mutate))
    assert code == 1 and report["state"] == "reward_grant_unconfirmed"
    assert report["awards"]["state"] == expected
    assert report["awards"]["matched_benefits"] == ([] if kind == "empty" else None)


def test_duplicate_target_benefit_is_not_silently_deduplicated(monkeypatch, tmp_path):
    def mutate(inv, *_):
        inv["gameEventDrops"].append(dict(inv["gameEventDrops"][0]))
    code, report, network = scenario(monkeypatch, tmp_path, configure=inventory_change(mutate))
    assert code == 1 and report["error"] == "award_target_duplicate"


def test_connection_node_id_is_never_substituted_for_benefit_id(monkeypatch, tmp_path):
    def mutate(inv, *_):
        node = inv["gameEventDropsConnection"]["edges"][0]["node"]
        node["id"] = BENEFIT
        del node["benefit"]
    code, report, network = scenario(monkeypatch, tmp_path, shape="connection", configure=inventory_change(mutate))
    assert code == 1 and report["error"] == "award_benefit_missing"


@pytest.mark.parametrize("include_second", [False, True])
def test_every_expected_benefit_must_be_awarded(monkeypatch, tmp_path, include_second):
    def mutate(inv, attempted, checked):
        if include_second:
            inv["gameEventDrops"].append({"id": SECOND_BENEFIT, "lastAwardedAt": checked.isoformat()})
    code, report, network = scenario(monkeypatch, tmp_path,
        metadata={**METADATA, "benefit_ids": [BENEFIT, SECOND_BENEFIT]}, configure=inventory_change(mutate))
    assert code == (0 if include_second else 1)
    assert report["awards"]["all_benefits_present"] is include_second


@pytest.mark.parametrize("progress", [None, {}, "missing"])
def test_in_progress_snapshot_unknown_does_not_hide_valid_awards(monkeypatch, tmp_path, progress):
    def mutate(inv, *_):
        if progress == "missing":
            del inv["dropCampaignsInProgress"]
        else:
            inv["dropCampaignsInProgress"] = progress
    code, report, network = scenario(monkeypatch, tmp_path, configure=inventory_change(mutate))
    assert code == 0 and report["state"] == "reward_grant_observed"
    assert report["inventory_target"] is None and "inventory_target_error" in report


@pytest.mark.parametrize("response", [Response({}, 429), Response({"extensions": {"challenge": {"type": "integrity"}}})])
def test_rate_limit_and_challenge_stop_without_extra_requests(monkeypatch, tmp_path, response):
    def configure(network, *_):
        network.responses["Inventory"] = response
    code, report, network = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and len(network.calls) == 2


def test_confirmed_candidate_remains_readonly(monkeypatch, tmp_path):
    code, report, network = scenario(monkeypatch, tmp_path, candidate_confirmed=True)
    assert code == 0 and len(network.calls) == 2


def test_cleanup_failure_prevents_success(monkeypatch, tmp_path):
    def configure(network, *_):
        network.close_error = True
    code, report, network = scenario(monkeypatch, tmp_path, configure=configure)
    assert code == 1 and report["state"] == "reward_grant_unconfirmed"
    assert report["cleanup_error"] == "RuntimeError"


@pytest.mark.parametrize("benefits", [[], [BENEFIT, BENEFIT], [None], None])
def test_invalid_fixed_metadata_is_rejected(tmp_path, benefits):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({**METADATA, "benefit_ids": benefits}))
    with pytest.raises(Exception, match="award_metadata_invalid"):
        awards.load_metadata(path)
