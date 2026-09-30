import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import check_campaign_auth as probe
from integrity_diagnostics import IDENTITY_HEADERS


OAUTH = "fixture-oauth-secret"
INTEGRITY = "fixture-integrity-secret"
HEADERS = {
    "Authorization": f"OAuth {OAUTH}", "Client-Id": probe.WEB_CLIENT_ID,
    "X-Device-Id": "fixture-device-secret", "Client-Session-Id": "fixture-session-secret",
    "Client-Version": "fixture-version-secret", "User-Agent": "fixture-browser-secret",
    "Client-Integrity": INTEGRITY, "Cookie": "fixture-cookie-secret",
}
QUERY = {
    "operationName": "ViewerDropsDashboard", "variables": {"fetchRewardCampaigns": False},
    "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "a" * 64}},
}
DENIED = {
    "data": {"currentUser": {"id": "42", "dropCampaigns": None}},
    "extensions": {"challenge": {"type": "integrity"}},
    "errors": [{"message": "failed integrity check"}],
}


def response(body, *, url=probe.GQL_URL, status=200, headers=HEADERS, reply_headers=None):
    return SimpleNamespace(
        url=url, status=status, headers=reply_headers or {}, json=AsyncMock(return_value=body),
        request=SimpleNamespace(
            url=url, method="POST", resource_type="fetch", failure=None,
            post_data_json=QUERY, all_headers=AsyncMock(return_value=headers),
        ),
    )


def capture_with_clock():
    report = {}
    capture = probe.BrowserCapture(OAUTH, "42", report)
    ticks = iter(range(1, 1000))
    capture.network.elapsed_ms = lambda: next(ticks)
    return capture, report


def receive(capture, reply):
    capture.network.request(reply.request)
    capture.response(reply)


def assert_redacted(report):
    assert "fixture-" not in json.dumps(report)


def test_response_callbacks_match_late_issuance_body_and_restore_request_order():
    async def scenario():
        capture, report = capture_with_clock()
        issuance_gate, dashboard_gate = asyncio.Event(), asyncio.Event()
        issuance = response({"token": INTEGRITY}, url="https://gql.twitch.tv/integrity")
        first_dashboard, second_dashboard = response(DENIED), response(DENIED)

        async def delayed_issuance():
            await issuance_gate.wait()
            return {"token": INTEGRITY}

        async def delayed_dashboard():
            await dashboard_gate.wait()
            return DENIED

        issuance.json = delayed_issuance
        first_dashboard.json = delayed_dashboard
        receive(capture, issuance)
        receive(capture, first_dashboard)
        receive(capture, second_dashboard)
        await asyncio.sleep(0)
        assert [item["request_id"] for item in report["dashboard_responses"]] == [3]
        dashboard_gate.set()
        issuance_gate.set()
        await asyncio.gather(*list(capture.tasks))
        for reply in (issuance, first_dashboard, second_dashboard):
            capture.network.finished(reply.request)
        await capture.close()
        assert [item["request_id"] for item in report["dashboard_responses"]] == [2, 3]
        for dashboard in report["dashboard_responses"]:
            assert dashboard["integrity_token_observation"] == "matched"
            assert dashboard["integrity_matches"] == [{
                "issuance_request_id": 1, "response_before_dashboard": True,
                "identity_matches": dict.fromkeys(IDENTITY_HEADERS, True),
            }]
            assert dashboard["finished_ms"] > dashboard["response_ms"] > dashboard["started_ms"]
        assert report["integrity_responses"][0]["finished_ms"] == report["integrity_requests"][0]["finished_ms"]
        assert capture.token == "" and not capture.network.requests and not capture.tasks
        assert_redacted(report)

    asyncio.run(scenario())


@pytest.mark.parametrize("parser_started", [False, True])
def test_cancelled_body_never_reports_that_server_returned_no_token(parser_started):
    async def scenario():
        capture, report = capture_with_clock()
        entered = asyncio.Event()

        async def blocked_body():
            entered.set()
            await asyncio.Event().wait()

        reply = response({}, url="https://gql.twitch.tv/integrity")
        reply.json = blocked_body
        receive(capture, reply)
        if parser_started:
            await asyncio.wait_for(entered.wait(), 1)
        capture.network.finished(reply.request)
        await capture.close()
        summary = report["integrity_responses"][0]
        assert summary["body_state"] == "cancelled"
        assert summary["headers_state"] == "cancelled"
        assert summary["token_returned"] is None
        assert summary["finished_ms"] == report["integrity_requests"][0]["finished_ms"]
        assert summary["finished_ms"] is not None
        assert not capture.tasks
        assert_redacted(report)

    asyncio.run(scenario())


def test_parsed_token_still_matches_when_issuance_header_capture_is_cancelled():
    async def scenario():
        capture, report = capture_with_clock()
        headers_entered = asyncio.Event()

        async def blocked_headers():
            headers_entered.set()
            await asyncio.Event().wait()

        issuance = response({"token": INTEGRITY}, url="https://gql.twitch.tv/integrity")
        issuance.request.all_headers = blocked_headers
        receive(capture, issuance)
        await asyncio.wait_for(headers_entered.wait(), 1)
        dashboard = response(DENIED)
        receive(capture, dashboard)
        await asyncio.sleep(0)
        capture.network.finished(issuance.request)
        capture.network.finished(dashboard.request)
        await capture.close()
        summary = report["integrity_responses"][0]
        assert summary["body_state"] == "parsed" and summary["token_returned"] is True
        assert summary["headers_state"] == "cancelled"
        assert summary["oauth_matches"] is None and summary["web_client_matches"] is None
        assert report["dashboard_responses"][0]["integrity_matches"] == [{
            "issuance_request_id": 1, "response_before_dashboard": True,
            "identity_matches": dict.fromkeys(IDENTITY_HEADERS, None),
        }]
        assert_redacted(report)

    asyncio.run(scenario())


def test_sdk_rate_limit_interrupts_pending_navigation_and_cancels_it():
    async def scenario():
        capture, report = capture_with_clock()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def navigation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        waiting = asyncio.create_task(probe.stop_on_rate_limit(navigation(), capture))
        await asyncio.wait_for(started.wait(), 1)
        limited = response(
            {}, url="https://k.twitchcdn.net/fixture-path-secret?token=fixture-query-secret", status=429,
            reply_headers={"retry-after": "120", "set-cookie": "fixture-cookie-secret"},
        )
        limited.request.resource_type = "document"
        receive(capture, limited)
        with pytest.raises(probe.ProbeFailure, match="^relevant_rate_limit$"):
            await asyncio.wait_for(waiting, 1)
        assert cancelled.is_set()
        assert report["rate_limits"][0]["retry_after_seconds"] == 120
        assert report["rate_limits"][0]["host"] == "k.twitchcdn.net"
        assert capture.control is None
        await capture.close()
        assert_redacted(report)

    asyncio.run(scenario())


def test_rate_limit_wins_when_operation_success_races_same_event():
    async def scenario():
        capture, report = capture_with_clock()

        async def simultaneous():
            capture.ready.set()
            receive(capture, response({}, status=429, reply_headers={"retry-after": "fixture-header-secret"}))
            return "would-have-succeeded"

        with pytest.raises(probe.ProbeFailure, match="^relevant_rate_limit$"):
            await probe.stop_on_rate_limit(simultaneous(), capture)
        assert capture.ready.is_set() and capture.rate_limited.is_set()
        assert report["rate_limits"][0]["retry_after_seconds"] is None
        await capture.close()
        assert_redacted(report)

    asyncio.run(scenario())


def test_existing_rate_limit_prevents_operation_from_starting():
    async def scenario():
        capture, report = capture_with_clock()
        capture.rate_limited.set()
        entered = False

        async def would_issue_request():
            nonlocal entered
            entered = True

        with pytest.raises(probe.ProbeFailure, match="^relevant_rate_limit$"):
            await probe.stop_on_rate_limit(would_issue_request(), capture)
        assert not entered
        await capture.close()
        assert_redacted(report)

    asyncio.run(scenario())


def test_rate_limit_arriving_during_cleanup_takes_priority_over_operation_error():
    async def scenario():
        capture, report = capture_with_clock()

        async def operation_error():
            raise ValueError("fixture-error-secret")

        async def wait_for_limit():
            try:
                await asyncio.Event().wait()
            finally:
                # A browser response arrives while wait-task cancellation is settled.
                receive(capture, response({}, status=429))

        capture.rate_limited.wait = wait_for_limit
        with pytest.raises(probe.ProbeFailure, match="^relevant_rate_limit$"):
            await probe.stop_on_rate_limit(operation_error(), capture)
        await capture.close()
        assert_redacted(report)

    asyncio.run(scenario())


def test_unrelated_rate_limit_does_not_stop_dashboard_observation():
    async def scenario():
        capture, report = capture_with_clock()
        receive(capture, response({}, status=429, url="https://unrelated.invalid/fixture-secret"))
        assert not capture.rate_limited.is_set()
        assert await probe.stop_on_rate_limit(asyncio.sleep(0, result="ok"), capture) == "ok"
        await capture.close()
        assert report["rate_limits"] == []
        assert_redacted(report)

    asyncio.run(scenario())


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), (" 120 ", 120), ("0", 0), ("-1", None), ("+1", None),
    ("1.5", None), ("NaN", None), ("999999999999", None), ("fixture-header-secret", None),
    ("Thu, 01 Jan 2026 00:01:00 GMT", 60), ("Wed, 31 Dec 2025 23:59:59 GMT", 0),
    ("Thu, 01 Jan 2026 00:01:00", None),
])
def test_retry_after_retains_only_valid_delay(value, expected):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert probe.retry_after_seconds(value, now=now) == expected
