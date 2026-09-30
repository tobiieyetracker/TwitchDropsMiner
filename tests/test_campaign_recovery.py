import asyncio
from unittest.mock import AsyncMock

import pytest

from gql_recovery import (
    CampaignAccessError, challenge_type, recover_challenges, validate_campaign_response,
)
from exceptions import GQLException


DASHBOARD = {"operationName": "ViewerDropsDashboard"}
DETAILS = {"operationName": "DropCampaignDetails", "variables": {"dropID": "new"}}
CHALLENGE = {"extensions": {"challenge": {"type": "integrity"}}}
EMPTY = {"data": {"currentUser": {"id": "42", "dropCampaigns": []}}}
NULL = {"data": {"currentUser": {"id": "42", "dropCampaigns": None}}}


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("response", [
    CHALLENGE,
    NULL,
    {"errors": [{"message": "failed integrity check"}]},
    {"errors": [{"extensions": {"code": "IntegrityCheckFailed"}}]},
])
def test_web_challenge_and_silent_app_denial_are_recovered(response):
    refresh, replay = AsyncMock(), AsyncMock(return_value=[EMPTY])
    result = run(recover_challenges(DASHBOARD, response, refresh=refresh, replay=replay))
    assert result == EMPTY
    refresh.assert_awaited_once()
    replay.assert_awaited_once_with([DASHBOARD])


def test_real_empty_list_needs_no_refresh_and_is_valid():
    refresh, replay = AsyncMock(), AsyncMock()
    assert run(recover_challenges(DASHBOARD, EMPTY, refresh=refresh, replay=replay)) == EMPTY
    validate_campaign_response(DASHBOARD, EMPTY)
    refresh.assert_not_awaited()
    replay.assert_not_awaited()


def test_null_is_not_reported_as_no_campaigns_without_web_session():
    replay = AsyncMock()
    with pytest.raises(CampaignAccessError, match="--browser-auth"):
        run(recover_challenges(DASHBOARD, NULL, refresh=None, replay=replay))
    replay.assert_not_awaited()


def test_batch_does_not_repeat_successful_claim_or_reorder_responses():
    claim = {"operationName": "DropsPage_ClaimDropRewards"}
    claimed = {"data": {"claimDropRewards": {"status": "ELIGIBLE_FOR_ALL"}}}
    detail = {"data": {"user": {"dropCampaign": {"id": "new"}}}}
    original = [claimed, CHALLENGE, EMPTY]
    replay = AsyncMock(return_value=[detail])
    result = run(recover_challenges(
        [claim, DETAILS, DASHBOARD], original, refresh=AsyncMock(), replay=replay,
    ))
    assert result == [claimed, detail, EMPTY]
    assert original[1] is CHALLENGE
    replay.assert_awaited_once_with([DETAILS])


def test_permanent_challenge_retries_once():
    refresh, replay = AsyncMock(), AsyncMock(return_value=[NULL])
    with pytest.raises(CampaignAccessError, match="no further"):
        run(recover_challenges(DASHBOARD, CHALLENGE, refresh=refresh, replay=replay))
    refresh.assert_awaited_once()
    replay.assert_awaited_once()


@pytest.mark.parametrize("response", [
    {"data": {"currentUser": None}},
    {"data": {"currentUser": {"id": "42"}}},
    {"data": {"currentUser": {"dropCampaigns": {}}}},
])
def test_missing_user_or_changed_schema_is_not_an_empty_inventory(response):
    assert challenge_type(DASHBOARD, response) is None
    with pytest.raises(CampaignAccessError):
        validate_campaign_response(DASHBOARD, response)


def test_details_null_is_also_detected():
    response = {"data": {"user": {"dropCampaign": None}}}
    assert challenge_type(DETAILS, response) == "integrity"
    with pytest.raises(CampaignAccessError):
        validate_campaign_response(DETAILS, response)


def test_unrelated_server_error_is_not_treated_as_integrity():
    response = {**NULL, "errors": [{"message": "service unavailable"}]}
    assert challenge_type(DASHBOARD, response) is None


@pytest.mark.parametrize("operation,response", [
    (DASHBOARD, {"extensions": {"challenge": {"type": "challenge-gates"}}}),
    ({"operationName": "DropsPage_ClaimDropRewards"}, CHALLENGE),
])
def test_interactive_challenges_and_writes_are_not_automatically_replayed(operation, response):
    refresh, replay = AsyncMock(), AsyncMock()
    with pytest.raises(CampaignAccessError):
        run(recover_challenges(operation, response, refresh=refresh, replay=replay))
    refresh.assert_not_awaited()
    replay.assert_not_awaited()


def test_short_batch_response_cannot_silently_drop_campaigns():
    with pytest.raises(GQLException, match="batch"):
        run(recover_challenges([DASHBOARD, DETAILS], [EMPTY], refresh=None, replay=AsyncMock()))


def test_retry_response_must_match_requested_batch():
    with pytest.raises(GQLException, match="batch"):
        run(recover_challenges(
            [DASHBOARD, DETAILS], [CHALLENGE, CHALLENGE],
            refresh=AsyncMock(), replay=AsyncMock(return_value=[EMPTY]),
        ))
