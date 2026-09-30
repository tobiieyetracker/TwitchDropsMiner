"""Read trusted aiohttp cookie files without changing cookie scope or lifetime.

Only the returned ``summary`` is suitable for a diagnostic report. Cookie values,
including the two convenient identity fields, must remain in memory. This module
reads aiohttp's JSON and legacy pickle formats directly: calling update_cookies
on a saved Max-Age would restart its lifetime at import time.
"""
from __future__ import annotations

import io
import json
import pickle
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from http.cookies import Morsel, SimpleCookie
from math import isfinite
from pathlib import Path
from typing import Any

from aiohttp import CookieJar


TWITCH_DOMAINS = ("twitch.tv", "twitchcdn.net", "jtvnw.net")
WEBSITE_HOST = "www.twitch.tv"
WEBSITE_PATH = "/drops/campaigns"


class BrowserCookieError(Exception):
    """A fixed error code; never include filenames or cookie contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BrowserCookieSnapshot:
    cookies: list[dict[str, Any]] = field(repr=False)
    auth_token: str = field(repr=False)
    unique_id: str | None = field(repr=False)
    summary: dict[str, int | bool]


class _CookieUnpickler(pickle._Unpickler):
    """Allow only the types used by aiohttp 3.x's legacy CookieJar.save()."""

    _types = {
        ("http.cookies", "SimpleCookie"): SimpleCookie,
        ("http.cookies", "Morsel"): Morsel,
        ("collections", "defaultdict"): defaultdict,
        ("builtins", "dict"): dict,
        ("builtins", "tuple"): tuple,
        ("builtins", "set"): set,
        ("builtins", "frozenset"): frozenset,
    }

    def find_class(self, module: str, name: str):
        try:
            return self._types[module, name]
        except KeyError:
            raise BrowserCookieError("cookie_file_format_invalid") from None


def _domain(value: Any) -> str:
    if not isinstance(value, str):
        raise BrowserCookieError("cookie_file_format_invalid")
    normalized = value.lower().removeprefix(".")
    if normalized and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", normalized):
        raise BrowserCookieError("cookie_domain_invalid")
    if ".." in normalized:
        raise BrowserCookieError("cookie_domain_invalid")
    return normalized


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _path_matches(request_path: str, cookie_path: str) -> bool:
    return request_path == cookie_path or (
        request_path.startswith(cookie_path)
        and (cookie_path.endswith("/") or request_path[len(cookie_path):].startswith("/"))
    )


def _entries(data: Any, *, legacy: bool):
    if not isinstance(data, dict):
        raise BrowserCookieError("cookie_file_format_invalid")
    for key, group in data.items():
        if legacy:
            if not isinstance(key, tuple) or len(key) != 2:
                raise BrowserCookieError("cookie_file_format_invalid")
            domain, bucket_path = key
        else:
            if not isinstance(key, str) or "|" not in key:
                raise BrowserCookieError("cookie_file_format_invalid")
            domain, bucket_path = key.split("|", 1)
        domain = _domain(domain)
        if not isinstance(bucket_path, str) or not isinstance(group, dict):
            raise BrowserCookieError("cookie_file_format_invalid")
        for name, raw in group.items():
            if not isinstance(name, str):
                raise BrowserCookieError("cookie_file_format_invalid")
            if legacy:
                if not isinstance(raw, Morsel):
                    raise BrowserCookieError("cookie_file_format_invalid")
                item = dict(raw)
                item.update(key=raw.key, value=raw.value)
            else:
                if not isinstance(raw, dict):
                    raise BrowserCookieError("cookie_file_format_invalid")
                item = raw
            yield domain, bucket_path, name, item


def _expiry(item: dict[str, Any], now: float) -> tuple[str, float | None]:
    absolute = item.get("expires_timestamp")
    if absolute is not None:
        if type(absolute) not in (int, float) or not isfinite(absolute):
            raise BrowserCookieError("cookie_expiry_invalid")
        return ("expired", None) if absolute <= now else ("valid", absolute)
    # Its creation time was not persisted in legacy files. In particular, even
    # a positive Max-Age must not become a fresh browser session or a fresh TTL.
    if item.get("max-age"):
        try:
            age = int(item["max-age"])
        except (TypeError, ValueError, OverflowError):
            raise BrowserCookieError("cookie_expiry_invalid") from None
        return ("expired" if age <= 0 else "unknown", None)
    expires = item.get("expires")
    if expires:
        if not isinstance(expires, str):
            raise BrowserCookieError("cookie_expiry_invalid")
        try:
            absolute = CookieJar._parse_date(expires)
        except (ValueError, OverflowError):
            absolute = None
        if absolute is None:
            raise BrowserCookieError("cookie_expiry_invalid")
        return ("expired", None) if absolute <= now else ("valid", absolute)
    return "valid", None


def _flag(item: dict[str, Any], key: str) -> bool:
    value = item.get(key, "")
    if value != "" and type(value) is not bool:
        raise BrowserCookieError("cookie_attributes_invalid")
    return bool(value)


def _scope_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    left, right = first["domain"], second["domain"]
    left_host, right_host = left.lstrip("."), right.lstrip(".")
    domains_overlap = (
        left_host == right_host
        or (left.startswith(".") and _domain_matches(right_host, left_host))
        or (right.startswith(".") and _domain_matches(left_host, right_host))
    )
    return domains_overlap and (
        _path_matches(first["path"], second["path"])
        or _path_matches(second["path"], first["path"])
    )


def _website_value(cookies: list[dict[str, Any]], name: str) -> str | None:
    values = {
        item["value"] for item in cookies if item["name"] == name
        and (item["domain"] == WEBSITE_HOST or (
            item["domain"].startswith(".")
            and _domain_matches(WEBSITE_HOST, item["domain"][1:])
        )) and _path_matches(WEBSITE_PATH, item["path"])
    }
    if len(values) > 1:
        raise BrowserCookieError("cookie_identity_ambiguous")
    return next(iter(values), None)


def _load_browser_cookies(path: Path | str, *, now: float | None = None) -> BrowserCookieSnapshot:
    """Read a trusted aiohttp jar; never save it or return unrelated cookies.

    JSON is the metadata-aware aiohttp format (missing host_only means a domain
    cookie). Legacy pickle lacks host-only and absolute Max-Age metadata. Only
    legacy scopes covering www.twitch.tv are imported, narrowed to that exact
    host; cookies whose original lifetime cannot be recovered are skipped.
    """
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not isfinite(now):
        raise BrowserCookieError("cookie_clock_invalid")
    try:
        raw = Path(path).read_bytes()
    except (OSError, TypeError, ValueError):
        raise BrowserCookieError("cookie_file_unreadable") from None
    try:
        data = json.loads(raw)
        legacy = False
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            data = _CookieUnpickler(io.BytesIO(raw)).load()
        except Exception:
            raise BrowserCookieError("cookie_file_format_invalid") from None
        legacy = True
    summary: dict[str, int | bool] = {
        "source_cookie_count": 0, "imported_cookie_count": 0,
        "skipped_unrelated_count": 0, "skipped_legacy_scope_count": 0,
        "skipped_expired_count": 0, "skipped_unknown_expiry_count": 0,
        "legacy_scope_narrowed_count": 0, "deduplicated_count": 0,
        "host_only_cookie_count": 0, "domain_cookie_count": 0,
        "legacy_format": legacy,
    }
    cookies: list[dict[str, Any]] = []
    for domain, bucket_path, name, item in _entries(data, legacy=legacy):
        summary["source_cookie_count"] += 1
        if not domain or not any(_domain_matches(domain, root) for root in TWITCH_DOMAINS):
            summary["skipped_unrelated_count"] += 1
            continue
        if not name or item.get("key") != name or not isinstance(item.get("value"), str):
            raise BrowserCookieError("cookie_file_format_invalid")
        if _domain(item.get("domain", "")) != domain:
            raise BrowserCookieError("cookie_domain_mismatch")
        cookie_path = item.get("path", "")
        if not isinstance(cookie_path, str) or not cookie_path.startswith("/"):
            raise BrowserCookieError("cookie_path_invalid")
        if bucket_path not in (cookie_path, cookie_path.rstrip("/")):
            raise BrowserCookieError("cookie_path_mismatch")
        state, expires = _expiry(item, now)
        if state != "valid":
            key = "skipped_expired_count" if state == "expired" else "skipped_unknown_expiry_count"
            summary[key] += 1
            continue
        if legacy:
            if not _domain_matches(WEBSITE_HOST, domain):
                summary["skipped_legacy_scope_count"] += 1
                continue
            domain, host_only = WEBSITE_HOST, True
            summary["legacy_scope_narrowed_count"] += 1
        else:
            host_only = item.get("host_only", False)
            if type(host_only) is not bool:
                raise BrowserCookieError("cookie_attributes_invalid")
        cookie = {
            "name": name, "value": item["value"],
            # Playwright uses a leading dot for subdomain coverage; a plain
            # domain preserves exact-host scope while still allowing a path.
            "domain": domain if host_only else "." + domain,
            "path": cookie_path, "secure": _flag(item, "secure"),
            "httpOnly": _flag(item, "httponly"),
        }
        same_site = item.get("samesite", "")
        if not isinstance(same_site, str):
            raise BrowserCookieError("cookie_attributes_invalid")
        if same_site:
            mapping = {"strict": "Strict", "lax": "Lax", "none": "None"}
            if same_site.lower() not in mapping:
                raise BrowserCookieError("cookie_attributes_invalid")
            cookie["sameSite"] = mapping[same_site.lower()]
        if expires is not None:
            cookie["expires"] = expires
        for previous in cookies:
            if previous["name"] != name or not _scope_overlap(previous, cookie):
                continue
            if previous["value"] != cookie["value"]:
                raise BrowserCookieError("cookie_identity_ambiguous")
            if (previous["domain"], previous["path"]) == (cookie["domain"], cookie["path"]):
                if previous != cookie:
                    raise BrowserCookieError("cookie_attributes_ambiguous")
                summary["deduplicated_count"] += 1
                break
        else:
            cookies.append(cookie)
    auth_token = _website_value(cookies, "auth-token")
    if not auth_token:
        raise BrowserCookieError("cookie_has_no_web_auth_token")
    unique_id = _website_value(cookies, "unique_id")
    summary.update(
        imported_cookie_count=len(cookies),
        host_only_cookie_count=sum(not item["domain"].startswith(".") for item in cookies),
        domain_cookie_count=sum(item["domain"].startswith(".") for item in cookies),
        auth_token_present=True, unique_id_present=bool(unique_id),
    )
    return BrowserCookieSnapshot(cookies, auth_token, unique_id, summary)


def load_browser_cookies(path: Path | str, *, now: float | None = None) -> BrowserCookieSnapshot:
    """Return private browser cookies plus a safe summary; leave the file untouched."""
    try:
        return _load_browser_cookies(path, now=now)
    except BrowserCookieError:
        raise
    except Exception:
        # Corrupt pickle objects and parser failures must not leak their repr,
        # filenames, or values when the caller writes its diagnostic report.
        raise BrowserCookieError("cookie_file_format_invalid") from None
