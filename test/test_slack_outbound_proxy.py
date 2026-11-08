"""Slack requests made outside slack_sdk reach Slack through the configured proxy.

slack_sdk's web client already sends its API calls through ``HTTPS_PROXY``. A button
reply posted to a ``response_url`` and a file download open their own aiohttp session,
so on a host whose only way out is the proxy they failed while the channel still worked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import aiohttp
import pytest

from kiro_crew.slack.client import RealSlackClient
from kiro_crew.slack.interactions_approvals import _post_review_auth_error
from kiro_crew.slack.interactions_context import ack_button

#: Every variable ``urllib.request.getproxies`` reads, cleared so the host's own cannot leak in.
_PROXY_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
)
_PROXY = "http://proxy.example:3128"


class _Body:
    async def iter_chunked(self, _size: int) -> Any:
        yield b"x"


class _Resp:
    status = 200
    content = _Body()

    def raise_for_status(self) -> None:
        return None

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Session:
    def __init__(self, proxies: list[str | None]) -> None:
        self._proxies = proxies

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self._proxies.append(kwargs.get("proxy"))
        return _Resp()

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        self._proxies.append(kwargs.get("proxy"))
        return _Resp()

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def _button_reply(tmp_path: Path) -> None:
    await ack_button({"response_url": "https://hooks.slack.com/actions/T1/x"}, "C1", "1.1")


async def _review_denial(tmp_path: Path) -> None:
    await _post_review_auth_error("https://hooks.slack.com/actions/T1/y")


async def _file_download(tmp_path: Path) -> None:
    client = RealSlackClient.__new__(RealSlackClient)
    client._web = SimpleNamespace(token="xoxb-test")
    await client.download_file("https://files.slack.com/x.png", str(tmp_path / "out"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send",
    [_button_reply, _review_denial, _file_download],
    ids=["reply", "review-reply", "download"],
)
async def test_slack_request_outside_the_sdk_uses_the_env_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    send: Callable[[Path], Awaitable[None]],
) -> None:
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    proxies: list[str | None] = []
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Session(proxies))

    await send(tmp_path)

    assert proxies == [_PROXY]
