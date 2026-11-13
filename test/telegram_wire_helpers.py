"""A real Telegram client wired to a loopback fake of the Bot API.

``telegram_wire`` starts an aiohttp server on ``127.0.0.1`` with a kernel-chosen
port and points the product's own ``TELEGRAM_API_BASE_URL`` seam at it, then
builds a real :class:`TelegramClient`. Every Bot API request the client sends
lands in ``wire.calls`` as a :class:`WireCall` holding the method and the
parameters Telegram would have received, decoded from the JSON or multipart body.

A test therefore reads what went over the wire (``chat_id``, ``text``,
``reply_markup``, ``message_thread_id``) after the product's client code has run,
instead of reading the arguments a mock was called with. Only the network is
fake: ``TelegramClient`` and its HTTP layer run unchanged.

The server answers ``{"ok": true, "result": ...}``: a message object with a
fresh ``message_id`` for send methods, ``true`` for the rest.
``wire.respond(method, result)`` overrides one method's result, and
``wire.fail(method, code, description)`` makes it answer
``{"ok": false, ...}``, which the client treats as a Bot API error.

Proxy variables are cleared and name lookups are pinned to loopback while the
wire is open, so a request that misses the fake fails the test instead of
reaching api.telegram.org.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp
import pytest
from aiohttp import web

from kiro_crew.telegram.client import TelegramClient

# Captured at import so an autouse stub of ``aiohttp.ClientSession`` in a test
# module cannot replace the session the client talks through.
_REAL_CLIENT_SESSION = aiohttp.ClientSession

# Built from parts so no full token-shaped literal sits in the source.
_TEST_TOKEN = "123456" + ":" + "wire-test-token"

_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")

_WAIT_CEILING_S = 30

_SEND_METHODS = frozenset(
    {
        "sendMessage",
        "sendPhoto",
        "sendDocument",
        "sendVoice",
        "sendAudio",
        "sendVideo",
        "editMessageText",
        "editMessageReplyMarkup",
    }
)


@dataclass
class WireCall:
    """One Bot API request as Telegram would have received it."""

    method: str
    params: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class TelegramWire:
    """The loopback server, the client pointed at it, and what it received."""

    client: TelegramClient
    url: str
    calls: list[WireCall] = field(default_factory=list)
    _answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    _message_id: int = 100

    def respond(self, method: str, result: Any) -> None:
        """Answer *method* with ``{"ok": true, "result": result}`` from now on."""
        self._answers[method] = {"ok": True, "result": result}

    def fail(self, method: str, code: int, description: str) -> None:
        """Answer *method* with a Bot API error."""
        self._answers[method] = {"ok": False, "error_code": code, "description": description}

    def sent(self, method: str) -> list[WireCall]:
        """Every request for *method*, in arrival order."""
        return [call for call in self.calls if call.method == method]

    def messages(self) -> list[WireCall]:
        """Every ``sendMessage`` request, in arrival order."""
        return self.sent("sendMessage")

    def _answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in self._answers:
            return self._answers[method]
        if method in _SEND_METHODS:
            self._message_id += 1
            message_id = params.get("message_id") or self._message_id
            chat = {"id": params.get("chat_id", 0), "type": "private"}
            return {
                "ok": True,
                "result": {"message_id": message_id, "chat": chat, "date": 1700000000},
            }
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "wire_bot"}}
        return {"ok": True, "result": True}


async def _decode(request: web.Request) -> dict[str, Any]:
    if "json" in request.headers.get("Content-Type", ""):
        raw = await request.read()
        loaded = json.loads(raw.decode("utf-8")) if raw else {}
        return loaded if isinstance(loaded, dict) else {"_body": loaded}
    params: dict[str, Any] = {}
    form = await request.post()
    for key, value in form.items():
        if isinstance(value, web.FileField):
            params[key] = {"filename": value.filename, "content_type": value.content_type}
            continue
        text = str(value)
        # Multipart sends structured fields (reply_markup, media) as JSON text.
        if text[:1] in "[{":
            with contextlib.suppress(ValueError):
                params[key] = json.loads(text)
                continue
        params[key] = text
    return params


@contextlib.asynccontextmanager
async def telegram_wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TelegramWire]:
    """Yield a :class:`TelegramWire`; the server and client close on exit."""
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    real_getaddrinfo = socket.getaddrinfo

    def _loopback_only(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in ("127.0.0.1", "localhost"):
            raise OSError(f"telegram_wire: refused name lookup for {host!r} (loopback only)")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _loopback_only)

    wire_ref: list[TelegramWire] = []

    async def _handle(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = await _decode(request)
        wire = wire_ref[0]
        wire.calls.append(WireCall(method=method, params=params))
        return web.json_response(wire._answer(method, params))

    app = web.Application()
    app.router.add_route("*", "/bot{token}/{method}", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("TELEGRAM_API_BASE_URL", url + "/bot{token}/{method}")
    client = TelegramClient(token=_TEST_TOKEN)
    session = _REAL_CLIENT_SESSION()
    client._session = session
    wire = TelegramWire(client=client, url=url)
    wire_ref.append(wire)
    try:
        yield wire
    finally:
        await asyncio.wait_for(session.close(), timeout=_WAIT_CEILING_S)
        await asyncio.wait_for(runner.cleanup(), timeout=_WAIT_CEILING_S)
