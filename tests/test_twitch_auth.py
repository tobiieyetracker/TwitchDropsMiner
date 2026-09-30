import asyncio
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest

import twitch
from constants import ClientType
from exceptions import LoginException
from twitch import Twitch, _AuthState
from web_session import WebCredentials


@pytest.fixture
def cookie_path(monkeypatch, tmp_path):
    path = tmp_path / "cookies.jar"
    monkeypatch.setattr(twitch, "COOKIES_PATH", path)
    return path


def saved_client(cookie_path, responses):
    """Use real cookie persistence and shutdown, with no network or Tk window."""
    jar = aiohttp.CookieJar()
    jar.update_cookies(
        {"auth-token": "saved-test-token", "unique_id": "saved-device"},
        ClientType.ANDROID_APP.CLIENT_URL,
    )
    jar.save(cookie_path)

    client = Twitch.__new__(Twitch)
    client.settings = SimpleNamespace(connection_quality=1)
    client.gui = SimpleNamespace(
        login=SimpleNamespace(update=Mock()),
        help=SimpleNamespace(_invalidate_button=SimpleNamespace(config=Mock())),
    )
    client._client_type = ClientType.ANDROID_APP
    client._session = client._web_session = None
    client._auth_state = _AuthState(client)
    client._auth_state._oauth_login = AsyncMock(return_value="renewed-test-token")
    client.stop_watching = Mock()
    client._watching_task = client._mnt_task = None
    client.websocket = SimpleNamespace(stop=AsyncMock())
    client._drops, client.channels = {}, {}
    client.inventory, client.wanted_games = [], []
    client._mnt_triggers = deque()
    validations = iter(responses)
    client.validation_headers = []

    @asynccontextmanager
    async def request(method, url, **kwargs):
        assert method == "GET"
        if url == ClientType.ANDROID_APP.CLIENT_URL:
            session = await client.get_session()
            # A normal page response can change cookies before token validation.
            session.cookie_jar.update_cookies({"unique_id": "new-device"}, url)
            yield SimpleNamespace(text=AsyncMock(return_value=""))
        else:
            assert url == "https://id.twitch.tv/oauth2/validate"
            client.validation_headers.append(kwargs["headers"])
            status, data = next(validations)
            yield SimpleNamespace(status=status, json=AsyncMock(return_value=data))

    client.request = request
    return client


def valid_token(client_id=ClientType.ANDROID_APP.CLIENT_ID):
    return 200, {"client_id": client_id, "user_id": "42"}


@pytest.mark.parametrize("issuer", ["other-test-client"])
def test_valid_foreign_cookie_survives_validation_retry_and_shutdown(cookie_path, issuer, caplog):
    caplog.set_level("INFO", logger="TwitchDrops")

    async def scenario():
        client = saved_client(cookie_path, [valid_token(issuer)] * 2)
        original = cookie_path.read_bytes()
        auth = client._auth_state
        try:
            for _ in range(2):
                with pytest.raises(LoginException, match="different client") as error:
                    await auth.validate()
                assert "preserved" in str(error.value)
                assert "does not import cookies.jar" in str(error.value)
                assert "saved-test-token" not in str(error.value)
                assert cookie_path.read_bytes() == original
                assert not auth._logged_in.is_set()
                assert not auth._hasattrs("access_token")
                assert not auth._hasattrs("user_id")
            jar = (await client.get_session()).cookie_jar
            assert jar.filter_cookies(client._client_type.CLIENT_URL)["auth-token"].value == (
                "saved-test-token"
            )
            auth._oauth_login.assert_not_awaited()
        finally:
            await client.shutdown()
        assert cookie_path.read_bytes() == original
        assert len(client.validation_headers) == 2

    asyncio.run(scenario())
    assert "saved-test-token" not in caplog.text


def test_saved_web_token_offers_chrome_without_overwriting_cookies(cookie_path):
    async def passthrough(coro):
        return await coro

    async def scenario():
        client = saved_client(cookie_path, [valid_token(ClientType.WEB.CLIENT_ID)])
        original = cookie_path.read_bytes()
        client.settings.proxy = None
        client.print = Mock()
        client.gui.coro_unless_closed = passthrough
        client.gui.login.ask_for_browser_login = AsyncMock()
        web_credentials = WebCredentials(789, {
            "Authorization": "OAuth web-test-token",
            "Client-Id": ClientType.WEB.CLIENT_ID,
            "User-Agent": "Chrome test agent",
            "X-Device-Id": "web-device",
            "Client-Session-Id": "web-session",
        })
        web_session = SimpleNamespace(
            start=AsyncMock(return_value=web_credentials), close=AsyncMock(),
        )
        try:
            with patch("twitch.TwitchWebSession", return_value=web_session) as make_web:
                await client._auth_state.validate()
            make_web.assert_called_once_with(channel="chrome", proxy="", notify=client.print)
            web_session.start.assert_awaited_once()
            client.gui.login.ask_for_browser_login.assert_awaited_once()
            assert client._auth_state.user_id == 789
            assert client._auth_state._logged_in.is_set()
            assert client._client_type.CLIENT_ID == ClientType.WEB.CLIENT_ID
            assert cookie_path.read_bytes() == original
        finally:
            await client.shutdown()
        assert cookie_path.read_bytes() == original

    asyncio.run(scenario())


def test_web_session_invalidation_preserves_the_device_cookie_file(cookie_path):
    async def scenario():
        client = saved_client(cookie_path, [])
        original = cookie_path.read_bytes()
        web_session = SimpleNamespace(invalidate=Mock())
        client._web_session = web_session

        client._auth_state.invalidate(delete_cookies=True)

        web_session.invalidate.assert_called_once_with()
        assert cookie_path.read_bytes() == original

    asyncio.run(scenario())


def test_matching_saved_login_is_used_and_cookies_still_saved_at_shutdown(cookie_path):
    async def scenario():
        client = saved_client(cookie_path, [valid_token()])
        try:
            await client._auth_state.validate()
            assert client._auth_state.user_id == 42
            assert client._auth_state._logged_in.is_set()
            client._auth_state._oauth_login.assert_not_awaited()
            session = await client.get_session()
            session.cookie_jar.update_cookies(
                {"test-after-login": "saved"}, client._client_type.CLIENT_URL,
            )
        finally:
            await client.shutdown()
        restored = aiohttp.CookieJar()
        restored.load(cookie_path)
        cookie = restored.filter_cookies(client._client_type.CLIENT_URL)
        assert cookie["auth-token"].value == "saved-test-token"
        assert cookie["persistent"].value == "42"
        assert cookie["test-after-login"].value == "saved"

    asyncio.run(scenario())


def test_expired_cookie_can_be_replaced_by_valid_device_login(cookie_path):
    async def scenario():
        client = saved_client(cookie_path, [(401, {}), valid_token()])
        try:
            await client._auth_state.validate()
            client._auth_state._oauth_login.assert_awaited_once()
            assert client._auth_state._logged_in.is_set()
            assert client.validation_headers == [
                {"Authorization": "OAuth saved-test-token"},
                {"Authorization": "OAuth renewed-test-token"},
            ]
        finally:
            await client.shutdown()
        restored = aiohttp.CookieJar()
        restored.load(cookie_path)
        assert restored.filter_cookies(client._client_type.CLIENT_URL)["auth-token"].value == (
            "renewed-test-token"
        )

    asyncio.run(scenario())


def test_failed_device_login_does_not_overwrite_cookie_during_cleanup(cookie_path):
    async def scenario():
        client = saved_client(cookie_path, [(401, {}), (401, {})])
        original = cookie_path.read_bytes()
        try:
            with pytest.raises(LoginException, match="could not validate"):
                await client._auth_state.validate()
            client._auth_state._oauth_login.assert_awaited_once()
            assert not client._auth_state._logged_in.is_set()
        finally:
            await client.shutdown()
        assert cookie_path.read_bytes() == original

    asyncio.run(scenario())


def test_explicit_logout_still_deletes_cookie_and_shutdown_does_not_recreate_it(cookie_path):
    async def scenario():
        client = saved_client(cookie_path, [valid_token()])
        try:
            await client._auth_state.validate()
            client._auth_state.invalidate(delete_cookies=True)
            assert not cookie_path.exists()
            assert not client._auth_state._logged_in.is_set()
            assert len((await client.get_session()).cookie_jar) == 0
        finally:
            await client.shutdown()
        assert not cookie_path.exists()

    asyncio.run(scenario())


def test_browser_mode_neither_loads_nor_saves_existing_cookie(cookie_path):
    async def scenario():
        client = saved_client(cookie_path, [])
        original = cookie_path.read_bytes()
        client._web_session = SimpleNamespace(close=AsyncMock())
        client._auth_state._logged_in.set()
        try:
            session = await client.get_session()
            assert len(session.cookie_jar) == 0
            session.cookie_jar.update_cookies(
                {"auth-token": "browser-test-token"}, ClientType.WEB.CLIENT_URL,
            )
        finally:
            await client.shutdown()
        assert cookie_path.read_bytes() == original
        client._web_session.close.assert_awaited_once()

    asyncio.run(scenario())
