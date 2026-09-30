"""Check browser proxy connectivity without Twitch login or miner startup."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re

from web_session import browser_proxy_settings


def error_code(error: Exception) -> str:
    # Never print raw Playwright errors, which may include proxy credentials.
    match = re.search(r"net::(ERR_[A-Z0-9_]+)", str(error))
    return match.group(1) if match else type(error).__name__


async def check(channel: str, proxy: str) -> int:
    from playwright.async_api import async_playwright

    options = browser_proxy_settings(proxy)
    async with async_playwright() as runtime:
        launch = {"headless": False, "proxy": options}
        if channel != "chromium":
            launch["channel"] = channel
        try:
            browser = await runtime.chromium.launch(**launch)
        except Exception as error:
            print(json.dumps({"phase": "launch", "error": error_code(error)}), flush=True)
            return 1
        try:
            print(json.dumps({
                "browser_version": browser.version,
                "proxy_scheme": options["server"].split(":", 1)[0],
                "proxy_auth": "username" in options,
            }), flush=True)
            context = await browser.new_context()
            page = await context.new_page()
            success = True
            for url in ("https://example.com/", "https://www.twitch.tv/drops/campaigns"):
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    status = response.status if response is not None else None
                    ok = status is not None and 200 <= status < 400
                    print(json.dumps({"url": url, "status": status, "ok": ok}), flush=True)
                    success = success and ok
                except Exception as error:
                    print(json.dumps({"url": url, "error": error_code(error)}), flush=True)
                    success = False
            return 0 if success else 1
        finally:
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chrome")
    parser.add_argument("--proxy-env", required=True, help="Name of an existing env var holding the proxy URL")
    args = parser.parse_args()
    proxy = os.environ.get(args.proxy_env)
    if not proxy:
        parser.error("The selected proxy environment variable is empty or unset")
    try:
        return asyncio.run(check(args.channel, proxy))
    except Exception as error:
        print(json.dumps({"phase": "setup_or_cleanup", "error": error_code(error)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
