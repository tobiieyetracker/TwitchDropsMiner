"""Read-only WEB identity used to discover channel campaigns for SMARTBOX runs.

The SMARTBOX account remains the miner's authoritative identity for inventory,
watching and claiming. This optional client reads only AvailableDrops with a
separately saved WEB token after validating that it belongs to the same account.
It never writes the cookie jar and never retries a challenged request.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiohttp

from constants import ClientType, GQL_QUERIES
from gql_recovery import CampaignAccessError, recover_challenges, validate_campaign_response
from utils import RateLimiter


Json = dict[str, Any]
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
GQL_URL = "https://gql.twitch.tv/gql"


class WebCampaignSourceError(Exception):
    """A non-secret error code for an unavailable optional WEB cookie source."""

    def __init__(self, code: str, *, fatal: bool = False):
        super().__init__(code)
        self.code = code
        self.fatal = fatal


class WebCampaignSource:
    """A single-account, read-only GQL client restricted to AvailableDrops."""

    def __init__(
        self,
        cookie_file: str | Path,
        *,
        proxy: str | None,
        gql_limiter: RateLimiter,
    ):
        self.cookie_file = Path(cookie_file).expanduser()
        self.proxy = proxy
        self.gql_limiter = gql_limiter
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self.user_id: int | None = None
        self._device_id: str | None = None

    async def open(self, *, expected_user_id: int) -> None:
        jar = aiohttp.CookieJar()
        try:
            jar.load(self.cookie_file)
            web_cookies = jar.filter_cookies(ClientType.WEB.CLIENT_URL)
        except Exception:
            raise WebCampaignSourceError("web_cookie_unreadable") from None

        token = web_cookies.get("auth-token")
        if token is None or not token.value:
            raise WebCampaignSourceError("web_cookie_missing")
        self._token = token.value
        device = web_cookies.get("unique_id")
        self._device_id = device.value if device is not None and device.value else None

        timeout = aiohttp.ClientTimeout(total=20, sock_connect=10)
        validation_session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": ClientType.WEB.USER_AGENT},
            timeout=timeout,
        )
        try:
            async with validation_session.get(
                VALIDATE_URL,
                headers={"Authorization": f"OAuth {self._token}"},
                proxy=self.proxy,
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    raise WebCampaignSourceError("web_validation_rate_limited", fatal=True)
                if response.status != 200:
                    raise WebCampaignSourceError("web_token_invalid")
                body = await response.json()
        except WebCampaignSourceError:
            raise
        except Exception:
            raise WebCampaignSourceError("web_validation_request_failed", fatal=True) from None
        finally:
            await validation_session.close()

        if not isinstance(body, dict) or body.get("client_id") != ClientType.WEB.CLIENT_ID:
            raise WebCampaignSourceError("cookie_is_not_web_auth")
        user_id = body.get("user_id")
        if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit():
            raise WebCampaignSourceError("web_token_user_missing")
        self.user_id = int(user_id)
        if self.user_id != expected_user_id:
            self.user_id = None
            raise WebCampaignSourceError("web_token_account_mismatch")

        self._session = aiohttp.ClientSession(
            cookie_jar=jar,
            headers={"User-Agent": ClientType.WEB.USER_AGENT},
            timeout=timeout,
        )

    async def gql_request(self, operation: Json) -> Json:
        expected_name = GQL_QUERIES["AvailableDrops"]["operationName"]
        variables = operation.get("variables") if isinstance(operation, dict) else None
        channel_id = variables.get("channelID") if isinstance(variables, dict) else None
        if (
            not isinstance(channel_id, str)
            or not channel_id.isascii()
            or not channel_id.isdigit()
            or operation.get("operationName") != expected_name
            or operation != GQL_QUERIES["AvailableDrops"].with_variables(
                {"channelID": channel_id}
            )
        ):
            raise CampaignAccessError(
                "The WEB campaign source only permits the persisted AvailableDrops read."
            )
        if self._session is None or self._token is None:
            raise CampaignAccessError("The WEB campaign source is not initialized.")

        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US",
            "Cache-Control": "no-cache",
            "Client-Id": ClientType.WEB.CLIENT_ID,
            "Origin": str(ClientType.WEB.CLIENT_URL),
            "Pragma": "no-cache",
            "Referer": str(ClientType.WEB.CLIENT_URL),
            "User-Agent": ClientType.WEB.USER_AGENT,
            "Authorization": f"OAuth {self._token}",
        }
        if self._device_id:
            headers["X-Device-Id"] = self._device_id

        try:
            async with self.gql_limiter:
                async with self._session.post(
                    GQL_URL,
                    json=operation,
                    headers=headers,
                    proxy=self.proxy,
                    allow_redirects=False,
                ) as response:
                    if response.status == 429:
                        raise CampaignAccessError(
                            "The WEB campaign source was rate limited; the scan stopped."
                        )
                    if response.status != 200:
                        raise CampaignAccessError(
                            "The WEB campaign source returned an unsuccessful HTTP status."
                        )
                    body = await response.json()
        except CampaignAccessError:
            raise
        except Exception:
            raise CampaignAccessError(
                "The WEB campaign source request failed; the scan stopped."
            ) from None

        async def reject_replay(_operations: list[Json]):
            raise CampaignAccessError(
                "The WEB campaign source received a challenge; it has no browser token to replay it."
            )

        if not isinstance(body, dict):
            raise CampaignAccessError("The WEB campaign source returned an unexpected response.")
        body = await recover_challenges(
            operation, body, refresh=None, replay=reject_replay
        )
        validate_campaign_response(operation, body)
        return body

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._token = None
        self._device_id = None
        self.user_id = None
