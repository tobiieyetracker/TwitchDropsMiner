import copy
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from finish_drop_state import refresh_target, select_target
from watch_check_state import WatchCheckError


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
USER = "test-user"
NAME = "Test campaign"
SECRET = "real-server-issued-private-instance"


def iso(value):
    return value.isoformat()


def body(*, minutes=8, claimed=False, claim_id=None):
    return {"data": {"currentUser": {
        "id": USER,
        "inventory": {"dropCampaignsInProgress": [{
            "id": "campaign", "name": NAME, "game": {"id": "game"},
            "startAt": iso(NOW - timedelta(days=1)),
            "endAt": iso(NOW + timedelta(days=1)),
            "self": None,
            "timeBasedDrops": [{
                "id": "drop", "requiredMinutesWatched": 60,
                "startAt": iso(NOW - timedelta(hours=12)),
                "endAt": iso(NOW + timedelta(hours=12)),
                "self": {"currentMinutesWatched": minutes, "isClaimed": claimed,
                         "dropInstanceID": claim_id},
            }],
        }]},
    }}}


def campaign(value):
    return value["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"][0]


def drop(value):
    return campaign(value)["timeBasedDrops"][0]


def select(value):
    return select_target(value, USER, NAME, now=NOW)


def code(expected, callback):
    with pytest.raises(WatchCheckError) as error:
        callback()
    assert error.value.code == expected
    assert str(error.value) == expected


def test_real_state_is_immutable_and_input_untouched():
    value = body()
    original = copy.deepcopy(value)
    target = select(value)
    assert target.minutes == 8 and target.required_minutes == 60
    assert target.starts_at == NOW - timedelta(hours=12)
    assert target.ends_at == NOW + timedelta(hours=12)
    assert target.campaign_ends_at == NOW + timedelta(days=1)
    assert target.account_link_state is None
    assert target.can_watch(NOW)
    assert not target.ready_to_claim
    assert value == original
    with pytest.raises(FrozenInstanceError):
        target.minutes = 60


def test_real_claim_id_never_appears_in_repr_or_report():
    target = select(body(minutes=60, claim_id=SECRET))
    assert target.ready_to_claim
    assert target.require_claim_id(NOW) == SECRET
    assert SECRET not in repr(target)
    report = target.public_dict()
    assert "claim_id" not in report
    assert SECRET not in json.dumps(report)
    assert report["claim_id_present"] is True


@pytest.mark.parametrize("value", [None, "", " \t"])
def test_unready_claim_id_is_not_synthesized(value):
    target = select(body(minutes=60, claim_id=value))
    assert not target.ready_to_claim
    code("target_claim_id_unavailable", lambda: target.require_claim_id(NOW))


def test_missing_claim_id_allowed_until_server_supplies_it():
    value = body(minutes=60)
    del drop(value)["self"]["dropInstanceID"]
    target = select(value)
    assert target.claim_id is None
    assert not target.ready_to_claim
    updated = refresh_target(body(minutes=60, claim_id=SECRET), USER, target, now=NOW)
    assert updated.require_claim_id(NOW) == SECRET


@pytest.mark.parametrize("value", [3, True, [], {}])
def test_invalid_claim_id_is_rejected(value):
    code("drop_claim_id_invalid", lambda: select(body(claim_id=value)))


def test_claim_id_alone_does_not_prove_ready():
    target = select(body(claim_id=SECRET))
    assert not target.ready_to_claim
    code("target_progress_incomplete", lambda: target.require_claim_id(NOW))


def test_already_claimed_keeps_actual_zero_minutes():
    target = select(body(minutes=0, claimed=True, claim_id=SECRET))
    assert target.is_claimed and target.minutes == 0
    assert not target.ready_to_claim and not target.can_watch(NOW)
    code("target_already_claimed", lambda: target.require_claim_id(NOW))


def test_claim_window_is_campaign_end_plus_24_hours_not_drop_end():
    target = select(body(minutes=60, claim_id=SECRET))
    assert not target.can_watch(NOW + timedelta(hours=13))
    assert target.require_claim_id(NOW + timedelta(hours=47)) == SECRET
    code("claim_window_ended", lambda: target.require_claim_id(NOW + timedelta(hours=48)))
    code("target_not_started", lambda: target.require_claim_id(NOW - timedelta(days=2)))


@pytest.mark.parametrize("value,expected", [
    (None, "user_null"), ({}, "user_empty"), ([], "user_invalid"),
    ({"inventory": {}}, "user_id_missing"),
    ({"id": "other"}, "user_id_mismatch"),
])
def test_identity_is_strict(value, expected):
    code(expected, lambda: select({"data": {"currentUser": value}}))


@pytest.mark.parametrize("location,key,value,expected", [
    ("self", "currentMinutesWatched", None, "drop_minutes_null"),
    ("self", "currentMinutesWatched", True, "drop_minutes_invalid"),
    ("self", "currentMinutesWatched", -1, "drop_minutes_invalid"),
    ("self", "currentMinutesWatched", "8", "drop_minutes_invalid"),
    ("self", "isClaimed", None, "drop_is_claimed_null"),
    ("self", "isClaimed", 0, "drop_is_claimed_invalid"),
    ("drop", "requiredMinutesWatched", 0, "drop_required_minutes_invalid"),
    ("drop", "requiredMinutesWatched", True, "drop_required_minutes_invalid"),
    ("drop", "requiredMinutesWatched", -1, "drop_required_minutes_invalid"),
    ("drop", "requiredMinutesWatched", "60", "drop_required_minutes_invalid"),
    ("drop", "self", None, "drop_self_null"),
    ("drop", "self", {}, "drop_minutes_missing"),
])
def test_progress_shape_is_not_coerced(location, key, value, expected):
    response = body()
    owner = drop(response)["self"] if location == "self" else drop(response)
    owner[key] = value
    code(expected, lambda: select(response))


@pytest.mark.parametrize("key", ["startAt", "endAt"])
@pytest.mark.parametrize("value", ["", "2026-09-30", "not-a-time", 3, None])
def test_dates_require_aware_valid_timestamps(key, value):
    response = body()
    campaign(response)[key] = value
    suffix = "start" if key == "startAt" else "end"
    kind = "null" if value is None else "invalid"
    code(f"campaign_{suffix}_{kind}", lambda: select(response))


def test_nonoverlapping_or_reversed_dates_are_rejected():
    response = body()
    drop(response)["startAt"] = iso(NOW + timedelta(days=2))
    drop(response)["endAt"] = iso(NOW + timedelta(days=3))
    code("target_time_range_invalid", lambda: select(response))


@pytest.mark.parametrize("linked", [True, False, None])
def test_account_link_is_only_server_state(linked):
    response = body()
    campaign(response)["self"] = {"isAccountConnected": linked}
    assert select(response).account_link_state is linked


def test_invalid_account_link_not_true_by_truthiness():
    response = body()
    campaign(response)["self"] = {"isAccountConnected": 1}
    code("account_link_state_invalid", lambda: select(response))


@pytest.mark.parametrize("met", [True, False, None])
def test_server_precondition_false_prevents_claim_without_synthesizing_true(met):
    response = body(minutes=60, claim_id=SECRET)
    drop(response)["self"]["hasPreconditionsMet"] = met
    target = select(response)
    assert target.preconditions_met is met
    if met is False:
        assert not target.ready_to_claim
        code("target_preconditions_not_met", lambda: target.require_claim_id(NOW))
    else:
        assert target.require_claim_id(NOW) == SECRET


def test_invalid_precondition_state_is_not_coerced():
    response = body()
    drop(response)["self"]["hasPreconditionsMet"] = 1
    code("drop_preconditions_invalid", lambda: select(response))


def test_campaign_name_must_be_unique_even_with_distinct_ids():
    response = body()
    second = copy.deepcopy(campaign(response))
    second["id"] = "second-campaign"
    response["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"].append(second)
    code("target_campaign_not_unique_or_present", lambda: select(response))


def test_duplicate_ids_are_rejected():
    response = body()
    response["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"].append(copy.deepcopy(campaign(response)))
    code("campaign_duplicate", lambda: select(response))
    response = body()
    campaign(response)["timeBasedDrops"].append(copy.deepcopy(drop(response)))
    code("target_drop_duplicate", lambda: select(response))


def test_unique_unclaimed_selection_and_explicit_selection():
    response = body()
    other = copy.deepcopy(drop(response))
    other["id"] = "other-drop"
    other["self"]["isClaimed"] = True
    campaign(response)["timeBasedDrops"].append(other)
    assert select(response).drop_id == "drop"
    chosen = select_target(response, USER, NAME, "other-drop", now=NOW)
    assert chosen.drop_id == "other-drop" and chosen.is_claimed
    other["self"]["isClaimed"] = False
    code("target_drop_ambiguous_or_absent", lambda: select(response))
    assert select_target(response, USER, NAME, "drop", now=NOW).drop_id == "drop"


def test_multiple_claimed_drops_still_require_explicit_choice():
    response = body(claimed=True)
    other = copy.deepcopy(drop(response))
    other["id"] = "other-drop"
    campaign(response)["timeBasedDrops"].append(other)
    code("target_drop_ambiguous_or_absent", lambda: select(response))


def test_refresh_absence_is_none_never_claimed_success():
    target = select(body())
    empty = body()
    empty["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"] = []
    assert refresh_target(empty, USER, target, now=NOW) is None
    missing_drop = body()
    campaign(missing_drop)["timeBasedDrops"] = []
    assert refresh_target(missing_drop, USER, target, now=NOW) is None


@pytest.mark.parametrize("value,expected", [(None, "campaigns_null"), ({}, "campaigns_invalid")])
def test_refresh_unknown_inventory_is_not_absence(value, expected):
    target = select(body())
    response = body()
    response["data"]["currentUser"]["inventory"]["dropCampaignsInProgress"] = value
    code(expected, lambda: refresh_target(response, USER, target, now=NOW))


@pytest.mark.parametrize("owner,key,value,expected", [
    ("campaign", "name", "Changed name", "target_name_changed"),
    ("campaign", "game", {"id": "other-game"}, "target_game_id_changed"),
    ("campaign", "id", "new-campaign-id", "target_campaign_identity_changed"),
    ("campaign", "endAt", iso(NOW + timedelta(days=2)), "target_campaign_ends_at_changed"),
    ("drop", "requiredMinutesWatched", 80, "target_required_minutes_changed"),
    ("drop", "endAt", iso(NOW + timedelta(hours=13)), "target_ends_at_changed"),
    ("drop", "startAt", iso(NOW - timedelta(hours=13)), "target_starts_at_changed"),
])
def test_refresh_changed_metadata_is_not_silently_accepted(owner, key, value, expected):
    target = select(body())
    response = body()
    (campaign(response) if owner == "campaign" else drop(response))[key] = value
    code(expected, lambda: refresh_target(response, USER, target, now=NOW))


def test_refresh_detects_changed_dates_even_when_intersection_is_unchanged():
    target = select(body())
    response = body()
    campaign(response)["startAt"] = iso(NOW - timedelta(days=2))
    code("target_time_bounds_changed", lambda: refresh_target(response, USER, target, now=NOW))


def test_refresh_never_retargets_a_new_drop_id():
    target = select(body())
    response = body()
    drop(response)["id"] = "different-drop"
    assert refresh_target(response, USER, target, now=NOW) is None


def test_refresh_accepts_positive_progress_and_claimed_zero():
    target = select(body())
    ready = refresh_target(body(minutes=60, claim_id=SECRET), USER, target, now=NOW)
    assert ready.ready_to_claim
    claimed = refresh_target(body(minutes=0, claimed=True, claim_id=SECRET), USER, ready, now=NOW)
    assert claimed.is_claimed and claimed.minutes == 0


def test_refresh_rejects_unclaimed_regression_and_claim_id_change():
    target = select(body(minutes=60, claim_id=SECRET))
    code("target_minutes_regressed", lambda: refresh_target(body(minutes=8), USER, target, now=NOW))
    code("target_claim_id_changed", lambda: refresh_target(
        body(minutes=60, claim_id="different-secret"), USER, target, now=NOW))
    claimed = select(body(claimed=True))
    code("target_claimed_state_regressed", lambda: refresh_target(body(), USER, claimed, now=NOW))


@pytest.mark.parametrize("envelope,expected", [
    ({"errors": [{"message": SECRET}]}, "gql_errors"),
    ({"extensions": {"challenge": {"type": "integrity"}}}, "gql_challenge"),
])
def test_errors_win_over_valid_state_in_both_paths(envelope, expected):
    target = select(body())
    response = body()
    response.update(envelope)
    code(expected, lambda: select(response))
    code(expected, lambda: refresh_target(response, USER, target, now=NOW))


def test_now_requires_timezone_and_selection_preserves_expired_state():
    code("now_invalid", lambda: select_target(body(), USER, NAME, now=datetime(2026, 9, 30)))
    target = select_target(body(claimed=True), USER, NAME, now=NOW + timedelta(days=5))
    assert target.is_claimed
