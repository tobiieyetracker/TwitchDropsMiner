import copy
import json

import pytest

from watch_check_state import WatchCheckError, snapshot_current, snapshot_inventory


USER_ID = "account-private-id"
CAMPAIGN_ID = "campaign-private-id"
DROP_ID = "target-drop"
TARGETS = {DROP_ID}


def current_body(session=None, *, user_id=USER_ID):
    user = {"dropCurrentSession": session}
    if user_id is not None:
        user["id"] = user_id
    return {"data": {"currentUser": user}}


def inventory_body(campaigns=None, *, user_id=USER_ID):
    user = {"inventory": {"dropCampaignsInProgress": [] if campaigns is None else campaigns}}
    if user_id is not None:
        user["id"] = user_id
    return {"data": {"currentUser": user}}


def target_campaign():
    return {
        "id": CAMPAIGN_ID,
        "timeBasedDrops": [{
            "id": DROP_ID,
            "requiredMinutesWatched": 60,
            "self": {"currentMinutesWatched": 7, "isClaimed": False},
        }],
    }


def read_inventory(body):
    return snapshot_inventory(body, USER_ID, CAMPAIGN_ID, TARGETS)


def read_current(body):
    return snapshot_current(body, USER_ID, TARGETS)


def assert_code(reader, body, code):
    with pytest.raises(WatchCheckError) as error:
        reader(body)
    assert error.value.code == code
    assert str(error.value) == code


@pytest.mark.parametrize("reader", [read_current, read_inventory])
@pytest.mark.parametrize("body,code", [
    (None, "response_invalid"),
    ([], "response_invalid"),
    ({}, "data_missing"),
    ({"data": None}, "data_null"),
    ({"data": []}, "data_invalid"),
    ({"data": {}}, "user_missing"),
    ({"data": {"currentUser": None}}, "user_null"),
    ({"data": {"currentUser": []}}, "user_invalid"),
    ({"data": {"currentUser": {}}}, "user_empty"),
])
def test_invalid_user_never_becomes_no_session(reader, body, code):
    assert_code(reader, body, code)


@pytest.mark.parametrize("reader,make_body", [
    (read_current, current_body), (read_inventory, inventory_body),
])
def test_identity_match_and_id_not_returned(reader, make_body):
    assert reader(make_body())["user_matches"] is True
    assert reader(make_body(user_id=None))["user_matches"] is None
    assert_code(reader, make_body(user_id="different-user"), "user_id_mismatch")


@pytest.mark.parametrize("reader,make_body", [
    (read_current, current_body), (read_inventory, inventory_body),
])
@pytest.mark.parametrize("value,code", [
    (None, "user_id_null"), (3, "user_id_invalid"),
    (True, "user_id_invalid"), ("", "user_id_invalid"),
])
def test_returned_user_id_must_be_valid(reader, make_body, value, code):
    body = make_body()
    body["data"]["currentUser"]["id"] = value
    assert_code(reader, body, code)


@pytest.mark.parametrize("reader,make_body", [
    (read_current, current_body), (read_inventory, inventory_body),
])
@pytest.mark.parametrize("envelope,code", [
    ({"errors": [{"message": "secret-token"}]}, "gql_errors"),
    ({"errors": None}, "gql_errors_invalid"),
    ({"errors": {}}, "gql_errors_invalid"),
    ({"extensions": {"challenge": {"type": "integrity", "token": "secret-token"}}},
     "gql_challenge"),
    ({"extensions": {"challenge": {"type": "other"}}}, "gql_challenge"),
    ({"extensions": {"challenge": {}}}, "gql_challenge"),
    ({"extensions": []}, "extensions_invalid"),
])
def test_errors_and_challenges_win_over_valid_data(reader, make_body, envelope, code):
    body = make_body()
    body.update(envelope)
    assert_code(reader, body, code)


@pytest.mark.parametrize("reader,make_body", [
    (read_current, current_body), (read_inventory, inventory_body),
])
def test_empty_error_list_and_absent_challenge(reader, make_body):
    body = make_body()
    body.update({"errors": [], "extensions": {"challenge": None}})
    assert reader(body)["user_present"] is True


def test_genuine_null_current_session_is_not_zero_progress():
    assert read_current(current_body()) == {
        "user_present": True, "user_matches": True,
        "session_state": "none", "minutes": None,
    }


def test_current_session_has_actual_positive_progress():
    body = current_body({"dropID": DROP_ID, "currentMinutesWatched": 7})
    assert read_current(body) == {
        "user_present": True, "user_matches": True,
        "session_state": "present", "drop_id": DROP_ID,
        "minutes": 7, "target_drop": True,
    }


def test_current_session_for_other_drop_is_preserved():
    snapshot = read_current(current_body({"dropID": "other", "currentMinutesWatched": 0}))
    assert snapshot["drop_id"] == "other"
    assert snapshot["target_drop"] is False
    assert snapshot["minutes"] == 0


def test_current_session_missing_is_not_none():
    body = current_body()
    del body["data"]["currentUser"]["dropCurrentSession"]
    assert_code(read_current, body, "current_session_missing")


@pytest.mark.parametrize("session,code", [
    ([], "current_session_invalid"),
    ({}, "current_drop_id_missing"),
    ({"dropID": None}, "current_drop_id_null"),
    ({"dropID": ""}, "current_drop_id_invalid"),
    ({"dropID": DROP_ID}, "current_minutes_missing"),
    ({"dropID": DROP_ID, "currentMinutesWatched": None}, "current_minutes_null"),
    ({"dropID": DROP_ID, "currentMinutesWatched": -1}, "current_minutes_invalid"),
    ({"dropID": DROP_ID, "currentMinutesWatched": True}, "current_minutes_invalid"),
    ({"dropID": DROP_ID, "currentMinutesWatched": "3"}, "current_minutes_invalid"),
    ({"dropID": DROP_ID, "currentMinutesWatched": 3.0}, "current_minutes_invalid"),
])
def test_current_fields_are_not_coerced(session, code):
    assert_code(read_current, current_body(session), code)


def test_genuine_empty_inventory_is_valid_absence():
    assert read_inventory(inventory_body()) == {
        "user_present": True, "user_matches": True,
        "campaigns_state": "list", "target_state": "absent", "drops": [],
    }


@pytest.mark.parametrize("field,value,code", [
    ("inventory", None, "inventory_null"),
    ("inventory", [], "inventory_invalid"),
    ("dropCampaignsInProgress", None, "campaigns_null"),
    ("dropCampaignsInProgress", {}, "campaigns_invalid"),
])
def test_null_inventory_fields_never_become_empty(field, value, code):
    body = inventory_body()
    user = body["data"]["currentUser"]
    parent = user if field == "inventory" else user["inventory"]
    parent[field] = value
    assert_code(read_inventory, body, code)


@pytest.mark.parametrize("field,code", [
    ("inventory", "inventory_missing"),
    ("dropCampaignsInProgress", "campaigns_missing"),
])
def test_missing_inventory_fields_never_become_empty(field, code):
    body = inventory_body()
    user = body["data"]["currentUser"]
    parent = user if field == "inventory" else user["inventory"]
    del parent[field]
    assert_code(read_inventory, body, code)


def test_target_inventory_keeps_server_values_and_does_not_mutate_input():
    body = inventory_body([target_campaign()])
    before = copy.deepcopy(body)
    assert read_inventory(body) == {
        "user_present": True, "user_matches": True,
        "campaigns_state": "list", "target_state": "present",
        "drops": [{"drop_id": DROP_ID, "minutes": 7,
                   "required_minutes": 60, "is_claimed": False}],
    }
    assert body == before


def test_claimed_flag_and_zero_are_actual_server_values():
    campaign = target_campaign()
    campaign["timeBasedDrops"][0]["self"] = {
        "currentMinutesWatched": 0, "isClaimed": True,
    }
    drop = read_inventory(inventory_body([campaign]))["drops"][0]
    assert drop["minutes"] == 0
    assert drop["is_claimed"] is True


@pytest.mark.parametrize("value,code", [
    (None, "drop_self_null"), ([], "drop_self_invalid"),
    ({}, "drop_minutes_missing"),
    ({"currentMinutesWatched": None, "isClaimed": False}, "drop_minutes_null"),
    ({"currentMinutesWatched": True, "isClaimed": False}, "drop_minutes_invalid"),
    ({"currentMinutesWatched": -1, "isClaimed": False}, "drop_minutes_invalid"),
    ({"currentMinutesWatched": "7", "isClaimed": False}, "drop_minutes_invalid"),
    ({"currentMinutesWatched": 7}, "drop_is_claimed_missing"),
    ({"currentMinutesWatched": 7, "isClaimed": None}, "drop_is_claimed_null"),
    ({"currentMinutesWatched": 7, "isClaimed": 0}, "drop_is_claimed_invalid"),
])
def test_drop_self_must_contain_real_state(value, code):
    campaign = target_campaign()
    campaign["timeBasedDrops"][0]["self"] = value
    assert_code(read_inventory, inventory_body([campaign]), code)


@pytest.mark.parametrize("field,code", [
    ("self", "drop_self_missing"),
    ("requiredMinutesWatched", "drop_required_minutes_missing"),
])
def test_drop_missing_state_is_not_synthesized(field, code):
    campaign = target_campaign()
    del campaign["timeBasedDrops"][0][field]
    assert_code(read_inventory, inventory_body([campaign]), code)


@pytest.mark.parametrize("value,code", [
    (None, "drop_required_minutes_null"), (-1, "drop_required_minutes_invalid"),
    (True, "drop_required_minutes_invalid"), ("60", "drop_required_minutes_invalid"),
])
def test_required_minutes_type_is_strict(value, code):
    campaign = target_campaign()
    campaign["timeBasedDrops"][0]["requiredMinutesWatched"] = value
    assert_code(read_inventory, inventory_body([campaign]), code)


@pytest.mark.parametrize("campaigns,code", [
    ([None], "campaign_invalid"),
    ([{}], "campaign_id_missing"),
    ([{"id": None}], "campaign_id_null"),
    ([{"id": ""}], "campaign_id_invalid"),
    ([{"id": CAMPAIGN_ID}], "time_based_drops_missing"),
    ([{"id": CAMPAIGN_ID, "timeBasedDrops": None}], "time_based_drops_null"),
    ([{"id": CAMPAIGN_ID, "timeBasedDrops": {}}], "time_based_drops_invalid"),
    ([{"id": CAMPAIGN_ID, "timeBasedDrops": [None]}], "drop_invalid"),
    ([{"id": CAMPAIGN_ID, "timeBasedDrops": [{}]}], "drop_id_missing"),
])
def test_campaign_and_drop_shapes(campaigns, code):
    assert_code(read_inventory, inventory_body(campaigns), code)


def test_other_campaigns_and_drops_do_not_require_own_state():
    campaign = target_campaign()
    campaign["timeBasedDrops"].append({"id": "other-drop", "self": None})
    body = inventory_body([{"id": "other-campaign", "timeBasedDrops": None}, campaign])
    assert len(read_inventory(body)["drops"]) == 1
    absent = read_inventory(inventory_body([{"id": "other-campaign"}]))
    assert absent["target_state"] == "absent"


def test_target_campaign_without_target_drop_is_not_given_zero_minutes():
    campaign = {"id": CAMPAIGN_ID, "timeBasedDrops": [{"id": "other-drop"}]}
    snapshot = read_inventory(inventory_body([campaign]))
    assert snapshot["target_state"] == "present"
    assert snapshot["drops"] == []


def test_duplicate_target_rows_are_ambiguous():
    campaign = target_campaign()
    assert_code(read_inventory, inventory_body([campaign, campaign]), "target_campaign_duplicate")
    campaign["timeBasedDrops"].append(copy.deepcopy(campaign["timeBasedDrops"][0]))
    assert_code(read_inventory, inventory_body([campaign]), "target_drop_duplicate")


@pytest.mark.parametrize("reader,body", [
    (read_current, current_body({"dropID": DROP_ID, "currentMinutesWatched": 7})),
    (read_inventory, inventory_body([target_campaign()])),
])
def test_snapshot_exports_only_allowlisted_state(reader, body):
    body["access_token"] = "secret-token"
    body["data"]["currentUser"]["login"] = "private-login"
    serialized = json.dumps(reader(body))
    for secret in (USER_ID, CAMPAIGN_ID, "secret-token", "private-login"):
        assert secret not in serialized
