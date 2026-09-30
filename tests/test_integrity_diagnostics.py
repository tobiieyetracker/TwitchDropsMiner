import json
from copy import deepcopy

import pytest

from integrity_diagnostics import IDENTITY_HEADERS, IntegrityAudit, WEB_CLIENT_ID


OAUTH = "fixture-oauth-secret"
TOKEN_A = "fixture-integrity-a-secret"
TOKEN_B = "fixture-integrity-b-secret"
IDENTITY = {
    "authorization": f"OAuth {OAUTH}", "client-id": WEB_CLIENT_ID,
    "x-device-id": "fixture-device-secret", "client-session-id": "fixture-session-secret",
    "client-version": "fixture-version-secret", "user-agent": "fixture-browser-secret",
    "cookie": "fixture-cookie-secret", "proxy-authorization": "fixture-proxy-secret",
}


def metadata(request_id, *, started_ms=1, response_ms=2):
    return {
        "request_id": request_id, "started_ms": started_ms, "response_ms": response_ms,
        "finished_ms": None, "failed_ms": None,
    }


def test_old_token_matches_its_issuance_instead_of_most_recent_response():
    audit = IntegrityAudit(OAUTH)
    first, second, dashboard = {}, {}, {}
    audit.observe_issuance(first, IDENTITY, {"token": TOKEN_A}, metadata(1))
    audit.observe_issuance(second, IDENTITY, {"token": TOKEN_B}, metadata(2, response_ms=8))
    audit.observe_dashboard(
        dashboard, {**IDENTITY, "client-integrity": TOKEN_A}, metadata(3, started_ms=10),
    )
    audit.finalize()
    assert first["oauth_matches"] is True and first["web_client_matches"] is True
    assert dashboard["integrity_token_observation"] == "matched"
    assert dashboard["integrity_matches"] == [{
        "issuance_request_id": 1, "response_before_dashboard": True,
        "identity_matches": dict.fromkeys(IDENTITY_HEADERS, True),
    }]


def test_late_body_parsing_and_lifecycle_updates_are_correlated_at_finalize():
    audit = IntegrityAudit(OAUTH)
    dashboard, issuance = {}, {}
    source = metadata(1, response_ms=5)
    audit.observe_dashboard(
        dashboard, {**IDENTITY, "client-integrity": TOKEN_A}, metadata(2, started_ms=10),
    )
    # The response arrived first; its body was parsed after the dashboard body.
    audit.observe_issuance(issuance, IDENTITY, {"token": TOKEN_A}, source)
    source["finished_ms"] = 12
    audit.finalize()
    assert issuance["finished_ms"] == 12
    assert dashboard["integrity_matches"][0]["issuance_request_id"] == 1
    assert dashboard["integrity_matches"][0]["response_before_dashboard"] is True


def test_same_token_from_multiple_responses_keeps_all_possible_sources():
    audit = IntegrityAudit(OAUTH)
    dashboard = {}
    for request_id, response_ms in ((1, 5), (2, 15), (3, None), (4, 10)):
        audit.observe_issuance({}, IDENTITY, {"token": TOKEN_A}, metadata(request_id, response_ms=response_ms))
    audit.observe_dashboard(
        dashboard, {**IDENTITY, "client-integrity": TOKEN_A}, metadata(5, started_ms=10),
    )
    audit.finalize()
    matches = dashboard["integrity_matches"]
    assert [match["issuance_request_id"] for match in matches] == [1, 2, 3, 4]
    assert [match["response_before_dashboard"] for match in matches] == [True, False, None, None]


@pytest.mark.parametrize("token, expected", [(TOKEN_B, "unobserved"), (None, "absent"), ("", "absent")])
def test_unknown_and_absent_integrity_are_distinct(token, expected):
    audit = IntegrityAudit(OAUTH)
    dashboard = {}
    audit.observe_issuance({}, IDENTITY, {"token": TOKEN_A}, metadata(1))
    headers = {**IDENTITY, "client-integrity": token} if token is not None else IDENTITY
    audit.observe_dashboard(dashboard, headers, metadata(2))
    audit.finalize()
    assert dashboard["integrity_token_observation"] == expected
    assert dashboard["integrity_matches"] == []


def test_identity_mismatches_and_missing_headers_are_reported_per_field():
    audit = IntegrityAudit(OAUTH)
    issuance, dashboard = {}, {}
    issuer_headers = {**IDENTITY, "authorization": "OAuth another-fixture-secret", "client-id": "other-client"}
    issuer_headers.pop("client-session-id")
    headers = {**IDENTITY, "client-integrity": TOKEN_A, "x-device-id": "different-fixture-secret"}
    headers.pop("client-version")
    audit.observe_issuance(issuance, issuer_headers, {"token": TOKEN_A}, metadata(1))
    audit.observe_dashboard(dashboard, headers, metadata(2))
    audit.finalize()
    assert issuance["oauth_matches"] is False and issuance["web_client_matches"] is False
    assert dashboard["integrity_matches"][0]["identity_matches"] == {
        "authorization": False, "client-id": False, "x-device-id": False,
        "client-session-id": None, "client-version": None, "user-agent": True,
    }


@pytest.mark.parametrize("body", [None, [], {}, {"token": ""}, {"token": 42}, {"token": [TOKEN_A]}])
def test_only_nonempty_string_tokens_can_be_correlated(body):
    audit = IntegrityAudit(OAUTH)
    dashboard = {}
    audit.observe_issuance({}, IDENTITY, body, metadata(1))
    audit.observe_dashboard(dashboard, {**IDENTITY, "client-integrity": TOKEN_A}, metadata(2))
    audit.finalize()
    assert dashboard["integrity_token_observation"] == "unobserved"


def test_finalize_is_idempotent_clears_secrets_and_exports_only_safe_metadata():
    audit = IntegrityAudit(OAUTH)
    issuance, dashboard = {}, {}
    source = {**metadata(1), "url": "https://example.invalid/?secret=fixture-url-secret", "token": TOKEN_B}
    bad_times = {**metadata(2), "started_ms": "fixture-time-secret", "failed_ms": float("nan")}
    audit.observe_issuance(issuance, IDENTITY, {"token": TOKEN_A}, source)
    audit.observe_dashboard(dashboard, {**IDENTITY, "client-integrity": TOKEN_A}, bad_times)
    audit.finalize()
    assert audit._expected_token == ""
    assert audit._issuances == [] and audit._dashboards == []
    assert "url" not in issuance and "token" not in issuance
    assert "started_ms" not in dashboard and "failed_ms" not in dashboard
    first_result = deepcopy([issuance, dashboard])
    source["finished_ms"] = 100
    audit.observe_issuance(issuance, IDENTITY, {"token": TOKEN_B}, metadata(3))
    audit.observe_dashboard(dashboard, {**IDENTITY, "client-integrity": TOKEN_B}, metadata(4))
    audit.finalize()
    assert [issuance, dashboard] == first_result
    assert audit._issuances == [] and audit._dashboards == []
    output = json.dumps([issuance, dashboard, vars(audit)])
    assert "fixture-" not in output
    assert TOKEN_A not in output and TOKEN_B not in output and OAUTH not in output
    assert "cookie" not in output and "proxy-authorization" not in output
