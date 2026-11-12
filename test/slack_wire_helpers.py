"""A real Slack client wired to a loopback fake of the Slack Web API.

``slack_wire`` builds a :class:`RealSlackClient` whose ``slack_sdk``
``AsyncWebClient`` posts to an aiohttp server on ``127.0.0.1`` with a
kernel-chosen port. Every request the SDK serialises lands in
``wire.calls`` as a :class:`WireCall` holding the Web API method and the
arguments Slack would have received, decoded from the JSON or form body.

A test therefore reads what went over the wire (``channel``, ``blocks``,
``thread_ts``) after the product's own client code and the SDK have run,
instead of reading the arguments a mock was called with. Only the network
is fake: ``RealSlackClient`` and ``slack_sdk`` run unchanged.

The server answers ``{"ok": true}`` plus a per-method body (a ``ts`` for
posts, a channel for ``conversations.open``). ``wire.respond(method, body)``
overrides one method's answer, and ``wire.fail(method, error)`` makes it
answer ``{"ok": false, "error": ...}``, which the SDK raises as
``SlackApiError``.

Name lookups are pinned to loopback while the wire is open, so a wrong
``base_url`` fails the test instead of reaching slack.com.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl

import aiohttp
import pytest
from aiohttp import web

from kiro_crew.slack import team_ids
from kiro_crew.slack.client import RealSlackClient

# Bound at import so a test that stubs ``aiohttp.ClientSession`` (for a
# ``response_url`` post) still leaves the client a real session to the wire.
_REAL_CLIENT_SESSION = aiohttp.ClientSession

# Built from parts so no full token-shaped literal sits in the source.
_TEST_TOKEN = "xoxb-" + "wire-" + "test"

_DEFAULT_BODIES: dict[str, dict[str, Any]] = {
    "auth.test": {"user_id": "UBOT", "team_id": "T0WIRE"},
    "conversations.open": {"channel": {"id": "D0WIRE"}},
    "conversations.info": {"channel": {"id": "C0WIRE", "is_im": False}},
    "users.info": {"user": {"id": "U0WIRE", "name": "wire", "profile": {}}},
    "conversations.history": {"messages": [], "has_more": False},
    "conversations.replies": {"messages": [], "has_more": False},
    "pins.list": {"items": []},
    "views.open": {"view": {"id": "V0WIRE"}},
    "views.update": {"view": {"id": "V0WIRE"}},
}


@dataclass
class WireCall:
    """One Web API request as Slack would have received it."""

    method: str
    args: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.args.get(key, default)


@dataclass
class SlackWire:
    """The loopback server, the client pointed at it, and what it received."""

    client: RealSlackClient
    url: str
    calls: list[WireCall] = field(default_factory=list)
    _bodies: dict[str, dict[str, Any]] = field(default_factory=dict)
    _ts: int = 0

    def respond(self, method: str, body: dict[str, Any]) -> None:
        """Answer *method* with ``{"ok": true, **body}`` from now on."""
        self._bodies[method] = {"ok": True, **body}

    def fail(self, method: str, error: str) -> None:
        """Answer *method* with a Slack error, which the SDK raises."""
        self._bodies[method] = {"ok": False, "error": error}

    def sent(self, method: str) -> list[WireCall]:
        """Every request for *method*, in arrival order."""
        return [call for call in self.calls if call.method == method]

    def posts(self) -> list[WireCall]:
        """Every ``chat.postMessage`` request, in arrival order."""
        return self.sent("chat.postMessage")

    def _answer(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        if method in self._bodies:
            return self._bodies[method]
        body: dict[str, Any] = {"ok": True, **_DEFAULT_BODIES.get(method, {})}
        if method.startswith("chat."):
            self._ts += 1
            body.setdefault("ts", args.get("ts") or f"1700000000.{self._ts:06d}")
            body.setdefault("channel", args.get("channel", ""))
        return body


def _decode(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8") if raw else ""
    if not text:
        return {}
    if "json" in content_type:
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else {"_body": loaded}
    args: dict[str, Any] = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        # The SDK sends list/dict arguments (blocks, attachments) as JSON text in a form body.
        if value[:1] in "[{":
            with contextlib.suppress(ValueError):
                args[key] = json.loads(value)
                continue
        args[key] = value
    return args


@contextlib.asynccontextmanager
async def slack_wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SlackWire]:
    """Yield a :class:`SlackWire`; the server and client close on exit."""
    # The client registers its channel-team cache as a module global; keep that inside the test.
    monkeypatch.setattr(team_ids, "_store", None)
    real_getaddrinfo = socket.getaddrinfo

    def _loopback_only(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in ("127.0.0.1", "localhost"):
            raise OSError(f"slack_wire: refused name lookup for {host!r} (loopback only)")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _loopback_only)

    wire_ref: list[SlackWire] = []

    async def _handle(request: web.Request) -> web.Response:
        method = request.match_info["method"]
        args = _decode(await request.read(), request.headers.get("Content-Type", ""))
        args.update({k: v for k, v in request.query.items() if k not in args})
        wire = wire_ref[0]
        wire.calls.append(WireCall(method=method, args=args))
        return web.json_response(wire._answer(method, args))

    app = web.Application()
    app.router.add_route("*", "/api/{method}", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    client = RealSlackClient(_TEST_TOKEN)
    url = f"http://127.0.0.1:{port}"
    client._web.base_url = f"{url}/api/"
    # The SDK sets its own ClientTimeout on every request, so the session needs none.
    session = _REAL_CLIENT_SESSION()
    client._web.session = session
    wire = SlackWire(client=client, url=url)
    wire_ref.append(wire)
    try:
        yield wire
    finally:
        await asyncio.wait_for(session.close(), timeout=30)
        await asyncio.wait_for(runner.cleanup(), timeout=30)
