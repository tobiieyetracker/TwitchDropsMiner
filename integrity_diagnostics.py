"""Correlate observed integrity issuance and use without exporting credentials.

This is a passive diagnostic. A matching token or identity does not establish
that Twitch accepts that token, and does not change the probe's success gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from web_session import WEB_CLIENT_ID


IDENTITY_HEADERS = (
    "authorization", "client-id", "x-device-id", "client-session-id",
    "client-version", "user-agent",
)
TIME_FIELDS = ("started_ms", "response_ms", "finished_ms", "failed_ms")


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity(headers: dict[str, str]) -> dict[str, str]:
    # Do not retain Cookie, SDK headers, proxy credentials or complete headers.
    return {
        name: value for name in IDENTITY_HEADERS
        if (value := _nonempty_string(headers.get(name))) is not None
    }


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    request_id = metadata.get("request_id")
    if type(request_id) is int and request_id >= 0:
        result["request_id"] = request_id
    for name in TIME_FIELDS:
        value = metadata.get(name)
        if value is None or (
            type(value) in (int, float) and isfinite(value) and value >= 0
        ):
            result[name] = value
    return result


@dataclass
class _Observation:
    output: dict[str, Any]
    identity: dict[str, str]
    token: str | None
    # Keep the reference: requestfinished may arrive after response parsing.
    metadata: dict[str, Any]


class IntegrityAudit:
    """Keep secrets in memory until all available responses can be correlated."""

    def __init__(self, expected_token: str):
        self._expected_token = expected_token
        self._issuances: list[_Observation] = []
        self._dashboards: list[_Observation] = []
        self._closed = False

    def observe_issuance(
        self, summary: dict[str, Any], headers: dict[str, str], body: Any,
        metadata: dict[str, Any],
    ) -> None:
        if self._closed:
            return
        identity = _identity(headers)
        summary["oauth_matches"] = identity.get("authorization") == f"OAuth {self._expected_token}"
        summary["web_client_matches"] = identity.get("client-id") == WEB_CLIENT_ID
        token = _nonempty_string(body.get("token")) if isinstance(body, dict) else None
        self._issuances.append(_Observation(summary, identity, token, metadata))

    def observe_dashboard(
        self, state: dict[str, Any], headers: dict[str, str], metadata: dict[str, Any],
    ) -> None:
        if self._closed:
            return
        self._dashboards.append(_Observation(
            state, _identity(headers), _nonempty_string(headers.get("client-integrity")),
            metadata,
        ))

    def finalize(self) -> None:
        """Resolve by token value, not parser completion order, then erase secrets."""
        if self._closed:
            return
        self._closed = True
        try:
            for observation in (*self._issuances, *self._dashboards):
                observation.output.update(_safe_metadata(observation.metadata))
            for dashboard in self._dashboards:
                matches = []
                if dashboard.token is not None:
                    for issuance in self._issuances:
                        if issuance.token != dashboard.token:
                            continue
                        issued_at = issuance.output.get("response_ms")
                        used_at = dashboard.output.get("started_ms")
                        # Equal rounded timestamps cannot establish event order.
                        before = (
                            issued_at < used_at
                            if issued_at is not None and used_at is not None and issued_at != used_at
                            else None
                        )
                        matches.append({
                            "issuance_request_id": issuance.output.get("request_id"),
                            "response_before_dashboard": before,
                            "identity_matches": {
                                name: (
                                    issuance.identity[name] == dashboard.identity[name]
                                    if name in issuance.identity and name in dashboard.identity else None
                                )
                                for name in IDENTITY_HEADERS
                            },
                        })
                dashboard.output["integrity_token_observation"] = (
                    "absent" if dashboard.token is None else "matched" if matches else "unobserved"
                )
                dashboard.output["integrity_matches"] = matches
        finally:
            self._expected_token = ""
            self._issuances.clear()
            self._dashboards.clear()
