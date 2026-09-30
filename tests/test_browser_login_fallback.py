import asyncio
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from constants import ClientType
from twitch import Twitch, _AuthState
from web_session import WebCredentials


async def passthrough(coro):
    return await coro


def make_client():
    client = Twitch.__new__(Twitch)
    client._client_type = ClientType.ANDROID_APP
    client._web_session = None
    client._session = None
    client.settings = SimpleNamespace(proxy=None)
    client.gui = SimpleNamespace(
        coro_unless_closed=passthrough,
        login=SimpleNamespace(update=Mock(), ask_for_browser_login=AsyncMock()),
        help=SimpleNamespace(_invalidate_button=SimpleNamespace(config=Mock())),
    )
    client.print = Mock()
    return client


def credentials():
    return WebCredentials(789, {
        "Authorization": "OAuth web-token",
        "Client-Id": ClientType.WEB.CLIENT_ID,
        "User-Agent": "Chrome test agent",
        "X-Device-Id": "web-device",
        "Client-Session-Id": "web-session",
    })


def browser_session():
    session = SimpleNamespace(start=AsyncMock(return_value=credentials()))
    return session


def install_device_response(client):
    seen = []

    @asynccontextmanager
    async def request(method, url, **kwargs):
        seen.append((method, str(url)))
        yield SimpleNamespace(json=AsyncMock(return_value={}))

    client.request = request
    return seen


def test_missing_device_code_waits_for_login_then_opens_chrome():
    client = make_client()
    seen = install_device_response(client)
    auth = _AuthState(client)
    auth.device_id = "device-client"

    web_session = browser_session()
    with patch("twitch.TwitchWebSession", return_value=web_session) as make_web:
        token = asyncio.run(auth._oauth_login())

    assert token == "web-token"
    assert seen == [("POST", "https://id.twitch.tv/oauth2/device")]
    client.gui.login.ask_for_browser_login.assert_awaited_once()
    make_web.assert_called_once_with(channel="chrome", proxy="", notify=client.print)
    web_session.start.assert_awaited_once()
    assert client._client_type.CLIENT_ID == ClientType.WEB.CLIENT_ID
    assert auth.user_id == 789
    assert auth._logged_in.is_set()


def test_click_login_fallback_does_not_save_web_token_to_device_cookie_jar():
    client = make_client()
    seen = install_device_response(client)
    cookie_jar = Mock()
    cookie_jar.filter_cookies.return_value = SimpleCookie()
    client._session = SimpleNamespace(cookie_jar=cookie_jar)
    client.get_session = AsyncMock(return_value=client._session)
    auth = _AuthState(client)
    auth.device_id = "device-client"
    web_session = browser_session()

    with patch("twitch.TwitchWebSession", return_value=web_session):
        asyncio.run(auth.validate())

    assert seen == [("POST", "https://id.twitch.tv/oauth2/device")]
    cookie_jar.clear.assert_called_once()
    cookie_jar.save.assert_not_called()
    assert not cookie_jar.filter_cookies.return_value
    assert auth.access_token == "web-token"
    assert auth._logged_in.is_set()
