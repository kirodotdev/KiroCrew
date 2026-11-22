"""Discord, Webex and WeCom requests skip the forward proxy for a NO_PROXY host."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.discord.client import DiscordClient
from kiro_crew.webex.client import _API_BASE as _WEBEX_API_BASE
from kiro_crew.webex.client import WebexClient
from kiro_crew.wecom.client import WeComClient

_PROXY = "http://proxy.invalid:8080"
_CHANNEL_HOSTS = "discord.com,discord.gg,discordapp.com,webexapis.com,wbx2.com,weixin.qq.com"
_CASES = [("", _PROXY), (_CHANNEL_HOSTS, None)]


@pytest.fixture(autouse=True)
def _no_proxy_env(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "https_proxy", "no_proxy", "all_proxy"):
        _floor_monkeypatch.delenv(name, raising=False)
        _floor_monkeypatch.delenv(name.upper(), raising=False)


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def json(self, content_type: Any = None) -> dict:
        return {}


class _CM:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Session:
    """Records each call's URL and ``proxy``; a call with no scripted outcome fails."""

    closed = False

    def __init__(self, *outcomes: Any) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._outcomes = list(outcomes)

    def _record(self, url: str, kwargs: dict) -> _CM:
        self.calls.append((url, kwargs.get("proxy")))
        if self._outcomes:
            return _CM(self._outcomes.pop(0))
        return _CM(aiohttp.ClientError("stop"))

    def request(self, method: str, url: str, **kwargs: Any) -> _CM:
        return self._record(url, kwargs)

    def get(self, url: str, **kwargs: Any) -> _CM:
        return self._record(url, kwargs)

    def post(self, url: str, **kwargs: Any) -> _CM:
        return self._record(url, kwargs)

    def head(self, url: str, **kwargs: Any) -> _CM:
        return self._record(url, kwargs)

    def ws_connect(self, url: str, **kwargs: Any) -> _CM:
        return self._record(url, kwargs)

    async def close(self) -> None:
        self.closed = True


def _bind(client: Any, session: _Session, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ensure() -> _Session:
        return session

    monkeypatch.setattr(client, "_ensure_session", _ensure)


def _hosts(session: _Session) -> list[str]:
    return [url.split("/")[2] for url, _ in session.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(("no_proxy", "expected"), _CASES)
async def test_discord_paths_honour_no_proxy(monkeypatch, tmp_path, no_proxy, expected):
    monkeypatch.setenv("NO_PROXY", no_proxy)
    client = DiscordClient(token="t", proxy=_PROXY)
    session = _Session(_Resp(204))
    _bind(client, session, monkeypatch)

    await client._api("POST", "/channels/c1/typing", {})
    with pytest.raises(aiohttp.ClientError):
        await client._run_connection()
    with pytest.raises(aiohttp.ClientError):
        await client.download_attachment(
            "https://cdn.discordapp.com/attachments/1/2/a.png", str(tmp_path / "a.png")
        )

    assert _hosts(session) == ["discord.com", "gateway.discord.gg", "cdn.discordapp.com"]
    assert [proxy for _, proxy in session.calls] == [expected] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("no_proxy", "expected"), _CASES)
async def test_webex_paths_honour_no_proxy(monkeypatch, tmp_path, no_proxy, expected):
    monkeypatch.setenv("NO_PROXY", no_proxy)
    client = WebexClient(token="t", proxy=_PROXY)
    content = f"{_WEBEX_API_BASE}/contents/abc"
    # U2C discovery fails, device create answers 409, the device list fails.
    session = _Session(aiohttp.ClientError("u2c"), _Resp(409))
    _bind(client, session, monkeypatch)

    assert await client._get_websocket_url() == ""
    assert await client._api("GET", "/people/me", None) is None
    assert (
        await client.send_file("r1", "hi", data=b"x", filename="a.txt", mimetype="text/plain")
        is None
    )
    assert await client.head_content(content) == ("", "", 0)
    with pytest.raises(ValueError):
        await client.download_content(content, str(tmp_path / "a"))

    async def _me() -> bool:
        return True

    async def _ws_url() -> str:
        return "wss://mercury-connection-a.wbx2.com/v1/apps/wx2/registrations/x/messages"

    monkeypatch.setattr(client, "_fetch_me", _me)
    monkeypatch.setattr(client, "_get_websocket_url", _ws_url)
    with pytest.raises(aiohttp.ClientError):
        await client._connect_and_serve()

    assert _hosts(session) == [
        "u2c.wbx2.com",
        "wdm-a.wbx2.com",
        "wdm-a.wbx2.com",
        "webexapis.com",
        "webexapis.com",
        "webexapis.com",
        "webexapis.com",
        "mercury-connection-a.wbx2.com",
    ]
    assert [proxy for _, proxy in session.calls] == [expected] * 8


@pytest.mark.asyncio
@pytest.mark.parametrize(("no_proxy", "expected"), _CASES)
async def test_wecom_paths_honour_no_proxy(monkeypatch, no_proxy, expected):
    monkeypatch.setenv("NO_PROXY", no_proxy)
    client = WeComClient(
        bot_id="b", secret="s", ws_url="wss://openws.work.weixin.qq.com", proxy=_PROXY
    )
    session = _Session()
    client._session = session  # type: ignore[assignment]

    assert (
        await client.send_reply("https://qyapi.weixin.qq.com/cgi-bin/aibot/response", "hi") is False
    )
    with pytest.raises(aiohttp.ClientError):
        await client._connect_and_serve()

    assert _hosts(session) == ["qyapi.weixin.qq.com", "openws.work.weixin.qq.com"]
    assert [proxy for _, proxy in session.calls] == [expected] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("no_proxy", "via_proxy"), [("", True), ("127.0.0.1", False)])
async def test_a_real_session_goes_direct_for_a_no_proxy_host(monkeypatch, no_proxy, via_proxy):
    """Over loopback, with aiohttp's own session: a refusing proxy sees the reply
    unless ``NO_PROXY`` lists the destination, which then answers it directly."""
    seen: list[str] = []

    async def _proxy(request: web.Request) -> web.Response:
        seen.append("proxy")
        return web.json_response({"errcode": 1}, status=502)

    async def _destination(request: web.Request) -> web.Response:
        seen.append("destination")
        return web.json_response({"errcode": 0})

    proxy_app = web.Application()
    proxy_app.router.add_route("*", "/{tail:.*}", _proxy)
    destination_app = web.Application()
    destination_app.router.add_post("/reply", _destination)
    async with TestServer(proxy_app) as proxy, TestServer(destination_app) as destination:
        monkeypatch.setenv("NO_PROXY", no_proxy)
        client = WeComClient(
            bot_id="b", secret="s", ws_url="wss://openws.invalid", proxy=str(proxy.make_url(""))
        )
        delivered = await asyncio.wait_for(
            client.send_reply(str(destination.make_url("/reply")), "hi"), 30
        )

    assert seen == (["proxy"] if via_proxy else ["destination"])
    assert delivered is not via_proxy
