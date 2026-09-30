import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from check_browser_proxy import check, error_code


def test_browser_error_output_keeps_only_the_network_code():
    assert error_code(RuntimeError(
        "page.goto: net::ERR_EMPTY_RESPONSE using http://alice:secret@proxy.example"
    )) == "ERR_EMPTY_RESPONSE"
    assert error_code(ValueError("alice:secret")) == "ValueError"


@pytest.mark.parametrize("failure", [False, True])
def test_proxy_check_uses_anonymous_browser_and_reports_failure(monkeypatch, capsys, failure):
    import playwright.async_api

    page = SimpleNamespace(goto=AsyncMock(side_effect=[
        RuntimeError("net::ERR_EMPTY_RESPONSE secret") if failure else SimpleNamespace(status=200),
        SimpleNamespace(status=200),
    ]))
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(
        version="test-browser", new_context=AsyncMock(return_value=context), close=AsyncMock(),
    )
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))

    class Manager:
        async def __aenter__(self):
            return runtime

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(playwright.async_api, "async_playwright", Manager)
    assert asyncio.run(check("chrome", "http://alice:secret@proxy.example:3128")) == int(failure)
    runtime.chromium.launch.assert_awaited_once_with(
        headless=False, channel="chrome",
        proxy={"server": "http://proxy.example:3128", "username": "alice", "password": "secret"},
    )
    browser.new_context.assert_awaited_once_with()
    browser.close.assert_awaited_once()
    assert page.goto.await_count == 2
    output = capsys.readouterr().out
    assert "alice" not in output and "secret" not in output and "proxy.example" not in output
    for line in output.splitlines():
        assert isinstance(json.loads(line), dict)
