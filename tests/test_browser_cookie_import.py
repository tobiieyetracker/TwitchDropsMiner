import asyncio
import json
import pickle
from collections import defaultdict
from http.cookies import SimpleCookie

import aiohttp
import pytest
from yarl import URL

from browser_cookie_import import BrowserCookieError, load_browser_cookies


NOW = 1_800_000_000
TOKEN = "fixture-secret-oauth"
DEVICE = "fixture-secret-device"


def record(name, value, domain="www.twitch.tv", path="/", **attributes):
    return {
        "key": name, "value": value, "coded_value": value,
        "domain": domain, "path": path, **attributes,
    }


def save_json(tmp_path, records):
    groups = {}
    for item in records:
        groups.setdefault(item["domain"] + "|" + item["path"].rstrip("/"), {})[item["key"]] = item
    path = tmp_path / "cookies.jar"
    path.write_text(json.dumps(groups), encoding="utf-8")
    return path


def auth(**attributes):
    return record("auth-token", TOKEN, host_only=True, secure=True, **attributes)


def test_json_preserves_attributes_scope_absolute_deadline_and_identity(tmp_path):
    path = save_json(tmp_path, [
        auth(httponly=True, samesite="lax", expires_timestamp=NOW + 1800),
        record("unique_id", DEVICE, "twitch.tv", secure=True, samesite="None"),
        record("scoped", "scope-secret", "www.twitch.tv", "/drops/", host_only=True,
               secure=True, samesite="Strict"),
        record("api", "api-secret", "gql.twitch.tv", host_only=True),
        record("sdk", "sdk-secret", "k.twitchcdn.net", host_only=True),
        record("unrelated", "unrelated-secret", "example.com"),
    ])
    before = path.read_bytes()
    snapshot = load_browser_cookies(path, now=NOW)
    assert snapshot.auth_token == TOKEN and snapshot.unique_id == DEVICE
    items = {item["name"]: item for item in snapshot.cookies}
    assert items["auth-token"] == {
        "name": "auth-token", "value": TOKEN, "domain": "www.twitch.tv", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": NOW + 1800,
    }
    assert items["unique_id"]["domain"] == ".twitch.tv"
    assert items["unique_id"]["sameSite"] == "None"
    assert items["scoped"]["path"] == "/drops/"
    assert items["api"]["domain"] == "gql.twitch.tv"
    assert items["sdk"]["domain"] == "k.twitchcdn.net"
    assert "sameSite" not in items["api"] and "expires" not in items["api"]
    assert "unrelated" not in items
    assert snapshot.summary["imported_cookie_count"] == 5
    assert snapshot.summary["host_only_cookie_count"] == 4
    assert snapshot.summary["domain_cookie_count"] == 1
    assert snapshot.summary["skipped_unrelated_count"] == 1
    assert path.read_bytes() == before
    output = repr(snapshot) + json.dumps(snapshot.summary)
    assert all(secret not in output for secret in (TOKEN, DEVICE, "scope-secret", "api-secret", "sdk-secret"))
    assert all(type(value) in (int, bool) for value in snapshot.summary.values())


def test_actual_installed_aiohttp_save_format_is_supported_without_writing_back(tmp_path):
    path = tmp_path / "cookies.jar"

    async def write_fixture():
        jar = aiohttp.CookieJar()
        cookies = SimpleCookie()
        cookies["auth-token"] = TOKEN
        cookies["auth-token"]["secure"] = True
        cookies["auth-token"]["httponly"] = True
        cookies["unique_id"] = DEVICE
        jar.update_cookies(cookies, URL("https://www.twitch.tv/"))
        jar.save(path)

    asyncio.run(write_fixture())
    before = path.read_bytes()
    snapshot = load_browser_cookies(path)
    assert snapshot.auth_token == TOKEN and snapshot.unique_id == DEVICE
    assert all(item["domain"] == "www.twitch.tv" for item in snapshot.cookies)
    assert path.read_bytes() == before


def save_legacy(tmp_path, records):
    jar_data = defaultdict(SimpleCookie)
    for item in records:
        group = jar_data[item["domain"], item["path"]]
        group[item["key"]] = item["value"]
        for key, value in item.items():
            if key not in {"key", "value", "coded_value", "host_only", "expires_timestamp"}:
                group[item["key"]][key] = value
    path = tmp_path / "cookies.jar"
    path.write_bytes(pickle.dumps(jar_data, pickle.HIGHEST_PROTOCOL))
    return path


def test_legacy_pickle_narrows_unknown_host_scope_and_does_not_reset_max_age(tmp_path):
    path = save_legacy(tmp_path, [
        record("auth-token", TOKEN, "twitch.tv", secure=True),
        record("unique_id", DEVICE, "www.twitch.tv"),
        record("api", "must-not-move-to-www", "gql.twitch.tv"),
        record("future", "absolute-secret", "twitch.tv", "/drops", expires="Wed, 01 Jan 2031 00:00:00 GMT"),
        record("old", "expired-secret", "twitch.tv", expires="Wed, 01 Jan 2020 00:00:00 GMT"),
        record("relative", "age-secret", "twitch.tv", **{"max-age": "86400"}),
        record("relative_with_expires", "age-exp-secret", "twitch.tv",
               expires="Wed, 01 Jan 2031 00:00:00 GMT", **{"max-age": "86400"}),
    ])
    before = path.read_bytes()
    snapshot = load_browser_cookies(path, now=NOW)
    assert {item["name"] for item in snapshot.cookies} == {"auth-token", "unique_id", "future"}
    assert all(item["domain"] == "www.twitch.tv" for item in snapshot.cookies)
    assert next(item for item in snapshot.cookies if item["name"] == "future")["expires"] == 1_924_992_000
    assert snapshot.summary["legacy_format"] is True
    assert snapshot.summary["skipped_expired_count"] == 1
    assert snapshot.summary["skipped_unknown_expiry_count"] == 2
    assert snapshot.summary["skipped_legacy_scope_count"] == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("expired", [NOW - 1, NOW, 0, -1])
def test_expired_json_deadlines_never_become_session_cookies(tmp_path, expired):
    path = save_json(tmp_path, [auth(), record("old", "old-secret", expires_timestamp=expired)])
    snapshot = load_browser_cookies(path, now=NOW)
    assert len(snapshot.cookies) == 1 and snapshot.summary["skipped_expired_count"] == 1


def test_json_uses_saved_absolute_max_age_deadline_without_starting_new_ttl(tmp_path):
    path = save_json(tmp_path, [auth(), record(
        "relative", "secret", expires_timestamp=NOW + 5, **{"max-age": "999999"},
    )])
    snapshot = load_browser_cookies(path, now=NOW)
    assert snapshot.cookies[1]["expires"] == NOW + 5
    after_expiration = load_browser_cookies(path, now=NOW + 5)
    assert len(after_expiration.cookies) == 1


@pytest.mark.parametrize("attributes", [
    {"expires_timestamp": float("nan")}, {"expires_timestamp": True},
    {"expires_timestamp": "1900000000"}, {"expires": "not-a-date-secret"},
    {"max-age": "not-a-number-secret"},
])
def test_invalid_expiration_is_rejected_without_exposing_value(tmp_path, attributes):
    path = save_json(tmp_path, [auth(), record("bad", "private", **attributes)])
    with pytest.raises(BrowserCookieError, match="^cookie_expiry_invalid$") as error:
        load_browser_cookies(path, now=NOW)
    assert error.value.code == "cookie_expiry_invalid"


@pytest.mark.parametrize("other", [
    record("auth-token", "different-secret", "twitch.tv"),
    record("auth-token", "different-secret", "www.twitch.tv", "/drops", host_only=True),
])
def test_overlapping_identity_values_are_rejected_instead_of_order_selected(tmp_path, other):
    path = save_json(tmp_path, [auth(), other])
    with pytest.raises(BrowserCookieError, match="^cookie_identity_ambiguous$"):
        load_browser_cookies(path, now=NOW)


def test_identical_values_in_different_scopes_preserve_original_scopes(tmp_path):
    path = save_json(tmp_path, [auth(), record("auth-token", TOKEN, "twitch.tv")])
    snapshot = load_browser_cookies(path, now=NOW)
    assert snapshot.auth_token == TOKEN
    assert {item["domain"] for item in snapshot.cookies} == {"www.twitch.tv", ".twitch.tv"}


def test_legacy_scopes_collapsing_to_same_cookie_are_deduplicated(tmp_path):
    path = save_legacy(tmp_path, [record("auth-token", TOKEN, "twitch.tv"),
                                  record("auth-token", TOKEN, "www.twitch.tv")])
    snapshot = load_browser_cookies(path, now=NOW)
    assert len(snapshot.cookies) == 1 and snapshot.summary["deduplicated_count"] == 1


def test_legacy_scope_collapse_with_different_attributes_is_rejected(tmp_path):
    path = save_legacy(tmp_path, [record("auth-token", TOKEN, "twitch.tv", secure=True),
                                  record("auth-token", TOKEN, "www.twitch.tv")])
    with pytest.raises(BrowserCookieError, match="^cookie_attributes_ambiguous$"):
        load_browser_cookies(path, now=NOW)


def test_path_boundaries_and_disjoint_hosts_do_not_create_false_conflicts(tmp_path):
    path = save_json(tmp_path, [
        auth(), record("preference", "drops", path="/drops"),
        record("preference", "other", path="/drops-other"),
        record("same-name", "www-secret", host_only=True),
        record("same-name", "gql-secret", "gql.twitch.tv", host_only=True),
    ])
    snapshot = load_browser_cookies(path, now=NOW)
    assert len(snapshot.cookies) == 5


@pytest.mark.parametrize("domain", ["twitch.tv.example.com", "nottwitch.tv", "tv", "example.com", ""])
def test_unrelated_or_unscoped_cookie_cannot_leak_into_browser(tmp_path, domain):
    path = save_json(tmp_path, [auth(), record("secret", "never-import", domain)])
    snapshot = load_browser_cookies(path, now=NOW)
    assert len(snapshot.cookies) == 1 and snapshot.summary["skipped_unrelated_count"] == 1


@pytest.mark.parametrize("other", [
    record("auth-token", TOKEN, "gql.twitch.tv", host_only=True),
    record("auth-token", TOKEN, "twitch.tv", host_only=True),
    record("auth-token", TOKEN, path="/unrelated", host_only=True),
])
def test_identity_cookie_must_reach_the_actual_website_path(tmp_path, other):
    path = save_json(tmp_path, [other])
    with pytest.raises(BrowserCookieError, match="^cookie_has_no_web_auth_token$"):
        load_browser_cookies(path, now=NOW)


@pytest.mark.parametrize("attribute,value", [("samesite", "invalid-secret"), ("secure", "false"),
                                             ("host_only", "false")])
def test_invalid_cookie_attributes_fail_closed(tmp_path, attribute, value):
    item = auth()
    item[attribute] = value
    path = save_json(tmp_path, [item])
    with pytest.raises(BrowserCookieError, match="^cookie_attributes_invalid$"):
        load_browser_cookies(path, now=NOW)


def test_bucket_domain_and_cookie_domain_must_agree(tmp_path):
    path = save_json(tmp_path, [auth()])
    data = json.loads(path.read_text())
    data["www.twitch.tv|"]["auth-token"]["domain"] = "gql.twitch.tv"
    path.write_text(json.dumps(data))
    with pytest.raises(BrowserCookieError, match="^cookie_domain_mismatch$"):
        load_browser_cookies(path, now=NOW)


def test_legacy_unpickler_does_not_resolve_arbitrary_globals(tmp_path):
    path = tmp_path / "private-cookie-path"
    path.write_bytes(b"cos\nsystem\n(S'echo private-secret'\ntR.")
    with pytest.raises(BrowserCookieError, match="^cookie_file_format_invalid$") as error:
        load_browser_cookies(path, now=NOW)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("raw", [b"invalid-private-data", b"[]", b"null", b'{"invalid-private-key":{}}'])
def test_malformed_files_produce_only_fixed_errors(tmp_path, raw):
    path = tmp_path / "private-file-name"
    path.write_bytes(raw)
    with pytest.raises(BrowserCookieError, match="^cookie_file_format_invalid$"):
        load_browser_cookies(path, now=NOW)


def test_missing_file_error_does_not_contain_private_filename(tmp_path):
    with pytest.raises(BrowserCookieError, match="^cookie_file_unreadable$"):
        load_browser_cookies(tmp_path / "private-secret", now=NOW)
