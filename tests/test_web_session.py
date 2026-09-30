import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import web_session
from exceptions import LoginException
from web_session import TwitchWebSession, WebCredentials, credentials_from_response


HEADERS = {
    "Authorization": "OAuth test-auth-token",
    "Client-Id": web_session.WEB_CLIENT_ID,
    "User-Agent": "Test browser",
    "X-Device-Id": "test-device",
    "Client-Version": "test-build",
}


@pytest.mark.parametrize(("url", "expected"), [
    ("proxy.example:8080", {"server": "http://proxy.example:8080"}),
    ("https://proxy.example/", {"server": "https://proxy.example"}),
    ("http://alice:secret@proxy.example:3128", {
        "server": "http://proxy.example:3128", "username": "alice", "password": "secret",
    }),
    ("http://name%40tenant:p%3Aa%2Fs%25%2B@proxy.example:3128", {
        "server": "http://proxy.example:3128", "username": "name@tenant", "password": "p:a/s%+",
    }),
    ("http://alice:secret@[2001:db8::1]:8080", {
        "server": "http://[2001:db8::1]:8080", "username": "alice", "password": "secret",
    }),
    ("socks5://127.0.0.1:1080", {"server": "socks5://127.0.0.1:1080"}),
])
def test_browser_proxy_preserves_protocol_address_and_separate_credentials(url, expected):
    assert web_session.browser_proxy_settings(url) == expected


@pytest.mark.parametrize("url", [
    "http://alice:secret@proxy.example:bad-port",
    "http://alice:secret@[invalid-ipv6]:8080",
    "http://alice:secret@proxy.example/path",
    "ftp://alice:secret@proxy.example:21",
    "http://alice:secret@",
    "socks5://alice:secret@proxy.example:1080",
])
def test_invalid_browser_proxy_errors_do_not_expose_credentials(url):
    with pytest.raises(LoginException) as error:
        web_session.browser_proxy_settings(url)
    assert "secret" not in str(error.value)
    assert "alice" not in str(error.value)
    assert "proxy.example" not in str(error.value)


def session(monkeypatch):
    monkeypatch.setattr(web_session, "time", lambda: 1000)
    result = TwitchWebSession(notify=Mock())
    result._credentials = WebCredentials(42, dict(HEADERS))
    result._page = SimpleNamespace(
        url=web_session.WEB_URL, is_closed=lambda: False,
        evaluate=AsyncMock(return_value={
            "status": 200, "data": {"token": "test-integrity", "expiration": 2000000},
        }),
    )
    result._context = SimpleNamespace(cookies=AsyncMock(return_value=[
        {"name": "auth-token", "value": "test-auth-token"},
    ]))
    return result


def test_credentials_belong_to_the_actual_authenticated_request():
    response = [{"data": {"currentUser": {"id": "42", "login": "test", "dropCampaigns": []}}}]
    credentials = credentials_from_response({**HEADERS, "Cookie": "not-copied"}, response)
    assert credentials.user_id == 42
    assert credentials.headers == HEADERS
    assert "test-auth-token" not in repr(credentials)
    assert credentials_from_response(HEADERS, {"data": {"currentUser": None}}) is None
    assert credentials_from_response({**HEADERS, "Client-Id": "another-client"}, response) is None


def test_cached_token_uses_browser_identity_and_refreshes_before_expiry(monkeypatch):
    client = session(monkeypatch)

    async def scenario():
        headers = await client.headers()
        assert headers == {**HEADERS, "Client-Integrity": "test-integrity"}
        assert client._refresh_at == 1900
        await client.headers()
        client._page.evaluate.assert_awaited_once()
        monkeypatch.setattr(web_session, "time", lambda: 1901)
        await client.headers()
        assert client._page.evaluate.await_count == 2
        # The browser, not a copied header, supplies User-Agent to the integrity endpoint.
        assert "User-Agent" not in client._page.evaluate.call_args.args[1]
    asyncio.run(scenario())


def test_concurrent_rejections_refresh_once(monkeypatch):
    client = session(monkeypatch)
    client._token, client._refresh_at = "rejected", 1900

    async def scenario():
        await asyncio.gather(*[
            client.refresh_integrity(rejected_token="rejected") for _ in range(20)
        ])
        client._page.evaluate.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    None, {"token": "", "expiration": 2000000},
    {"token": "expired", "expiration": 900000},
    {"token": "bad-expiry", "expiration": "2000000"},
])
def test_invalid_integrity_response_is_not_cached(monkeypatch, data):
    client = session(monkeypatch)
    client._page.evaluate.return_value = {"status": 403, "data": data}
    with pytest.raises(LoginException):
        asyncio.run(client.headers())
    assert client._token is None


def test_navigation_away_never_sends_credentials_into_another_origin(monkeypatch):
    client = session(monkeypatch)
    client._page.url = "https://example.com/"
    with pytest.raises(LoginException, match="Return"):
        asyncio.run(client.headers())
    client._page.evaluate.assert_not_awaited()


def test_changed_account_is_not_silently_used_for_claims(monkeypatch):
    client = session(monkeypatch)
    client._context.cookies.return_value = [{"name": "auth-token", "value": "another-account"}]
    with pytest.raises(LoginException, match="changed or expired"):
        asyncio.run(client.headers())
    client._page.evaluate.assert_not_awaited()


def test_account_change_in_a_response_invalidates_session(monkeypatch):
    client = session(monkeypatch)
    response = SimpleNamespace(
        request=SimpleNamespace(all_headers=AsyncMock(return_value=HEADERS)),
        json=AsyncMock(return_value={"data": {"currentUser": {"id": "99", "dropCampaigns": []}}}),
    )
    asyncio.run(client._capture_response(response))
    assert client._account_changed
    assert client._credentials.user_id == 42


def test_browser_close_cleans_up_tokens_and_pending_tasks(monkeypatch):
    client = session(monkeypatch)
    browser, runtime = SimpleNamespace(close=AsyncMock()), SimpleNamespace(stop=AsyncMock())
    client._browser, client._playwright = browser, runtime

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(600))
        client._response_tasks.add(task)
        await client.close()
        assert task.cancelled()
    asyncio.run(scenario())
    browser.close.assert_awaited_once()
    runtime.stop.assert_awaited_once()
    assert client._credentials is None and client._token is None


def test_error_details_cannot_leak_tokens(monkeypatch):
    client = session(monkeypatch)
    client._page.evaluate.side_effect = RuntimeError("request included secret-credential")
    with pytest.raises(LoginException) as error:
        asyncio.run(client.headers())
    assert "secret-credential" not in str(error.value)


@pytest.mark.parametrize("user", [
    {"id": "42"}, {"id": "42", "dropCampaigns": None},
])
def test_login_waits_for_dashboard_to_pass_the_websites_own_check(user):
    assert credentials_from_response(HEADERS, {"data": {"currentUser": user}}) is None


@pytest.mark.parametrize("proxy", ["", "http://alice:secret@proxy.example:3128"])
def test_normal_start_waits_for_a_successful_dashboard_without_saving_a_profile(monkeypatch, proxy):
    import playwright.async_api

    client = TwitchWebSession(proxy=proxy, notify=Mock())
    response = SimpleNamespace(
        request=SimpleNamespace(all_headers=AsyncMock(return_value=HEADERS)),
        json=AsyncMock(return_value={"data": {"currentUser": {"id": "42", "dropCampaigns": []}}}),
    )
    page = SimpleNamespace(url=web_session.WEB_URL, is_closed=lambda: False, on=Mock())

    async def navigate(*args, **kwargs):
        await client._capture_response(response)

    page.goto = AsyncMock(side_effect=navigate)
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        cookies=AsyncMock(return_value=[{"name": "auth-token", "value": "test-auth-token"}]),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    chromium = SimpleNamespace(launch=AsyncMock(return_value=browser), launch_persistent_context=Mock())
    runtime = SimpleNamespace(chromium=chromium, stop=AsyncMock())
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: SimpleNamespace(
        start=AsyncMock(return_value=runtime),
    ))

    async def scenario():
        credentials = await client.start()
        assert credentials.user_id == 42
        assert await client.start() is credentials
        await client.close()

    asyncio.run(scenario())
    chromium.launch.assert_awaited_once()
    assert chromium.launch.call_args.kwargs["headless"] is False
    if proxy:
        assert chromium.launch.call_args.kwargs["proxy"] == {
            "server": "http://proxy.example:3128", "username": "alice", "password": "secret",
        }
    else:
        assert "proxy" not in chromium.launch.call_args.kwargs
    chromium.launch_persistent_context.assert_not_called()
    browser.new_context.assert_awaited_once_with()
    runtime.stop.assert_awaited_once()


def test_cleanup_discards_credentials_even_if_browser_runtime_shutdown_fails(monkeypatch):
    client = session(monkeypatch)
    client._browser = SimpleNamespace(close=AsyncMock())
    client._playwright = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        asyncio.run(client.close())
    assert client._credentials is None and client._token is None


def test_non_ok_integrity_response_is_rejected_even_if_it_has_a_token(monkeypatch):
    client = session(monkeypatch)
    client._page.evaluate.return_value["status"] = 403
    with pytest.raises(LoginException):
        asyncio.run(client.headers())
    assert client._token is None
