"""One CDP connection to the browser, shared by the Harness daemon and the gateway.

The gateway holds the ONLY websocket to the browser. Harness's daemon connects to
this relay as if the relay were the browser: it resolves ``BU_CDP_URL`` with an HTTP
``GET /json/version`` (served here) and opens the ``webSocketDebuggerUrl`` the answer
names (served here too). Everything the daemon sends is forwarded unchanged except
its request ids, which are renumbered so several clients never collide and so the
relay's own requests -- ids from :data:`RELAY_ID_BASE` up -- stay disjoint.

Why one connection: on the user's own Chrome (144+), every CDP connection raises an
"Allow remote debugging?" sheet. One connection is one click, which lasts until
Chrome or the gateway restarts.

Why private sessions: the relay attaches its own flat sessions (:meth:`CdpRelay.attach`)
for the screencast and for takeover input. Every reply and event on such a session is
delivered to the attacher's callback and NEVER to a daemon client, so frames do not
enter Harness's shared event buffer (``drain_events`` empties it on every read).

Why a tab ledger: the relay records a target the moment the daemon's
``Target.createTarget`` returns, whether or not the page then loads, so session end
can close exactly the tabs the agent opened and never the person's. Each such target
also gets a relay-owned session with focus emulation on: without it a tab behind
another tab paints no frames and Harness's input calls on it hang.

Loopback only, and a secret on every path: the listener binds ``127.0.0.1:0`` and
every route it serves carries a per-start capability (:attr:`CdpRelay.url` is
``http://127.0.0.1:<port>/<secret>``). The sandbox does not fence loopback, so a
process the agent runs can find the port by probing; without the secret it gets a
404 from ``/json/version`` and from the websocket path alike, and the secret is
given to the daemon's environment and to nothing else. Harness resolves
``BU_CDP_URL`` with ``GET <base>/json/version``, so the prefix rides along unchanged.

Two upstreams: a websocket URL (the user's own Chrome, its ``DevToolsActivePort``
file; Kiro's Chromium on Windows) or a :class:`PipeUpstream` over Chromium's
``--remote-debugging-pipe`` (fds 3 and 4, ``\\0``-terminated JSON), which opens no
port at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import itertools
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any, Protocol

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

#: First id the relay uses for its own CDP requests. Daemon requests are renumbered
#: from 1, so the two ranges cannot meet in any realistic session.
RELAY_ID_BASE = 1_000_000_000

#: How long one relay-owned CDP request may wait for its reply.
SEND_TIMEOUT_SECS = 15.0

#: The websocket path prefix the relay serves to the daemon, followed by the per-start
#: secret. Chrome's own path shape, so a client that pattern-matches on
#: ``/devtools/browser/`` keeps working.
RELAY_WS_PREFIX = "/devtools/browser/"

#: Bytes of entropy in the per-start secret (URL-safe base64, 32 characters).
_SECRET_BYTES = 24

#: Longest ``\0``-terminated message the pipe upstream reads: a screencast frame or
#: a DOM snapshot can be several MiB; Chrome has no documented ceiling.
PIPE_MESSAGE_LIMIT = 256 * 1024 * 1024
_PIPE_TERMINATOR = b"\0"

#: The loopback address every relay socket binds and every relay URL names.
LOOPBACK_HOST = "127.0.0.1"

#: ``/json/version`` fields the daemon reads. Harness takes only
#: ``webSocketDebuggerUrl``; the rest keep the shape Chrome answers.
_VERSION_BROWSER = "KiroCrew/cdp-relay"
_VERSION_PROTOCOL = "1.3"

#: No cap on a websocket message: a screencast frame or a DOM snapshot can exceed
#: aiohttp's 4 MiB default, and both ends are loopback.
_UNLIMITED_MESSAGE = 0

#: Grace for the websocket close handshake when the relay shuts down.
_CLOSE_TIMEOUT_SECS = 5.0

EventCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class CdpError(RuntimeError):
    """The browser answered a relay-owned request with a CDP ``error``."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__(f"{method}: {error.get('message', error)}")
        self.method = method
        self.error = error


class RelayClosed(RuntimeError):
    """The upstream connection is gone; no request can be answered."""


class Upstream(Protocol):
    """The browser end of the relay: text messages in both directions, then close."""

    async def send(self, raw: str) -> None: ...

    def messages(self) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...


class _WsUpstream:
    """A CDP websocket (the user's Chrome; Kiro's Chromium on Windows)."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, session: aiohttp.ClientSession):
        self._ws = ws
        self._session = session

    async def send(self, raw: str) -> None:
        await self._ws.send_str(raw)

    async def messages(self) -> AsyncIterator[str]:
        async for msg in self._ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                yield msg.data

    async def close(self) -> None:
        await _close_quietly(self._ws)
        await self._session.close()


class PipeUpstream:
    """Chromium's ``--remote-debugging-pipe``: fd 3 into the browser, fd 4 out of it.

    Each message is JSON followed by one ``\\0``. *reader* and *writer* are the
    gateway's ends of the two pipes; the browser holds the other two.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, raw: str) -> None:
        self._writer.write(raw.encode("utf-8") + _PIPE_TERMINATOR)
        await self._writer.drain()

    async def messages(self) -> AsyncIterator[str]:
        while True:
            try:
                chunk = await self._reader.readuntil(_PIPE_TERMINATOR)
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError:
                logger.warning(
                    "relay: pipe message over %d bytes; dropping upstream", PIPE_MESSAGE_LIMIT
                )
                return
            yield chunk[:-1].decode("utf-8", errors="replace")

    async def close(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), _CLOSE_TIMEOUT_SECS)
        except Exception:  # noqa: BLE001 - the browser end may already be gone
            pass


async def pipe_upstream_from_fds(read_fd: int, write_fd: int) -> PipeUpstream:
    """Wrap the gateway's ends of the two debugging pipes (POSIX only).

    *read_fd* is where the browser's fd 4 writes; *write_fd* feeds the browser's
    fd 3. Both are owned by the returned upstream from here on.
    """
    import os

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=PIPE_MESSAGE_LIMIT)
    # Wrapped before the first await, so each fd has exactly one owner from here:
    # its file object, then the transport that adopts that same object. Closing the
    # objects on failure is idempotent and never closes a reused fd number.
    files: list[Any] = []
    try:
        files.append(os.fdopen(read_fd, "rb", buffering=0))
        files.append(os.fdopen(write_fd, "wb", buffering=0))
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), files[0]
        )
        files.append(read_transport)
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, files[1]
        )
    except BaseException:
        for owned in reversed(files):
            with contextlib.suppress(Exception):
                owned.close()
        if len(files) < 2:
            # fdopen itself failed: close the raw fds nothing wrapped yet.
            for fd in (read_fd, write_fd)[len(files) :]:
                with contextlib.suppress(OSError):
                    os.close(fd)
        raise
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    return PipeUpstream(reader, writer)


class CdpRelay:
    """The gateway's one CDP connection, served to the daemon and tapped by the gateway.

    One instance per browser connection. :meth:`start` connects upstream and starts
    the loopback server; :meth:`close` tears both down. The ``agent_targets`` ledger
    and the relay-owned sessions live for the connection's lifetime.
    """

    def __init__(self) -> None:
        self.url = ""
        self.ws_url = ""
        self.upstream_url = ""
        #: Called with each target id the daemon creates, as soon as the create
        #: returns. The stream lane hooks its screencast here.
        self.on_target_created: Callable[[str], Awaitable[None]] | None = None
        #: Called once when the upstream connection closes for any reason.
        self.on_upstream_closed: Callable[[], Awaitable[None]] | None = None
        self._secret = secrets.token_urlsafe(_SECRET_BYTES)
        self._up: Upstream | None = None
        self._up_lock: asyncio.Lock | None = None
        self._runner: web.AppRunner | None = None
        self._reader: asyncio.Task[None] | None = None
        self._clients: set[web.WebSocketResponse] = set()
        self._down_ids = itertools.count(1)
        self._routes: dict[int, tuple[web.WebSocketResponse, Any]] = {}
        self._create_calls: set[int] = set()
        self._ids = itertools.count(RELAY_ID_BASE)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._own_sessions: dict[str, EventCallback] = {}
        #: Child sessions a relay-owned session auto-attached (out-of-process iframes).
        self._children: dict[str, set[str]] = {}
        #: Child sessions dropped with their parent: Chrome's late detach events for
        #: them are swallowed, never broadcast to a daemon client.
        self._closing: set[str] = set()
        self._adopted: dict[str, str] = {}
        self._agent_targets: list[str] = []
        self._closed = False
        self._torn_down = False
        self.forwarded_up = 0
        self.forwarded_down = 0

    # ── lifecycle ──

    async def start(self, upstream: str | Upstream) -> str:
        """Connect to the browser and serve the relay; return the daemon's HTTP URL.

        *upstream* is a CDP websocket URL or an already-open :class:`PipeUpstream`.
        A URL blocks for as long as the browser holds the handshake -- on the
        user's Chrome that is until the person clicks Allow, with no deadline on
        purpose: a deadline would drop the sheet and the retry would raise a
        second one.

        The returned URL carries the per-start secret; it goes into the daemon's
        environment and nowhere else.
        """
        self._up_lock = asyncio.Lock()
        if isinstance(upstream, str):
            self.upstream_url = upstream
            self._up = await _connect_ws(upstream)
        else:
            self.upstream_url = "pipe"
            self._up = upstream
        app = web.Application(client_max_size=_UNLIMITED_MESSAGE)
        app.router.add_get("/{token}/json/version", self._serve_version)
        app.router.add_get("/{token}/json", self._serve_version)
        app.router.add_get(RELAY_WS_PREFIX + "{token}", self._serve_client)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, LOOPBACK_HOST, 0)
        await site.start()
        port = _site_port(site)
        self.url = f"http://{LOOPBACK_HOST}:{port}/{self._secret}"
        self.ws_url = f"ws://{LOOPBACK_HOST}:{port}{RELAY_WS_PREFIX}{self._secret}"
        self._reader = asyncio.create_task(self._read_upstream(), name="cdp-relay-upstream")
        return self.url

    def _authorized(self, request: web.Request) -> bool:
        token = request.match_info.get("token", "")
        return hmac.compare_digest(token.encode("utf-8"), self._secret.encode("utf-8"))

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Stop serving, drop every client, close the upstream connection. Idempotent.

        ``closed`` alone does not short-circuit this: the upstream reader marks the
        relay closed when the browser hangs up, and the listener and client
        session still need releasing afterwards.
        """
        if self._torn_down:
            return
        self._torn_down = True
        self._closed = True
        self._fail_pending(RelayClosed("relay closed"))
        for ws in list(self._clients):
            await _close_quietly(ws)
        self._clients.clear()
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
        if self._up is not None:
            try:
                await self._up.close()
            except Exception:  # noqa: BLE001 - teardown of a connection already failing
                pass
        if self._runner is not None:
            await self._runner.cleanup()
        self._up = None
        self._runner = None

    # ── the relay's own requests ──

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        quiet: bool = False,
    ) -> dict[str, Any]:
        """One relay-owned CDP request; the ``result`` of its reply.

        Raises :class:`CdpError` on a CDP error reply and :class:`RelayClosed` when
        the upstream is gone. With *quiet* a CDP error answers ``{}`` instead:
        for the late ``Page.screencastFrameAck`` a stopped screencast still
        emits, where the error is expected and carries nothing.
        """
        if self._closed or self._up is None:
            raise RelayClosed("relay is not connected")
        mid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        body: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            body["sessionId"] = session_id
        try:
            await self._send_upstream(json.dumps(body))
            reply = await asyncio.wait_for(fut, SEND_TIMEOUT_SECS)
        finally:
            self._pending.pop(mid, None)
        if "error" in reply:
            if quiet:
                return {}
            raise CdpError(method, reply["error"])
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    async def attach(self, target_id: str, on_event: EventCallback) -> str:
        """Attach a relay-owned flat session to *target_id*; events go to *on_event*.

        The session id is returned. Nothing on it ever reaches a daemon client.
        """
        res = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = str(res["sessionId"])
        self._own_sessions[sid] = on_event
        return sid

    async def detach(self, session_id: str) -> None:
        """Detach a relay-owned session. A session already gone is not an error."""
        self._own_sessions.pop(session_id, None)
        self._drop_children(session_id)
        await self.send("Target.detachFromTarget", {"sessionId": session_id}, quiet=True)

    def _track_child(self, parent: str, msg: dict[str, Any]) -> None:
        """A relay-owned session auto-attached a child (an out-of-process iframe).

        Registered BEFORE the event reaches the owner's callback, and synchronously,
        so the child's own replies and events are never forwarded to a daemon
        client: they belong to whoever owns the parent. Detach drops it again.
        """
        method = msg.get("method")
        if method not in ("Target.attachedToTarget", "Target.detachedFromTarget"):
            return
        child = (msg.get("params") or {}).get("sessionId")
        if not isinstance(child, str) or not child:
            return
        if method == "Target.attachedToTarget":
            self._own_sessions[child] = self._own_sessions[parent]
            self._children.setdefault(parent, set()).add(child)
        else:
            self._own_sessions.pop(child, None)
            self._children.get(parent, set()).discard(child)
            self._drop_children(child)

    def _drop_children(self, session_id: str) -> None:
        for child in self._children.pop(session_id, set()):
            self._own_sessions.pop(child, None)
            if len(self._closing) < 4096:
                self._closing.add(child)
            self._drop_children(child)

    def owns_session(self, session_id: str) -> bool:
        return session_id in self._own_sessions

    # ── the tab ledger ──

    def agent_targets(self) -> list[str]:
        """Targets the daemon created, in creation order, still believed open."""
        return list(self._agent_targets)

    def forget_target(self, target_id: str) -> None:
        """Drop *target_id* from the ledger (the stream lane saw it destroyed)."""
        if target_id in self._agent_targets:
            self._agent_targets.remove(target_id)
        self._adopted.pop(target_id, None)

    async def close_agent_targets(self) -> None:
        """Close every ledgered tab. The person's own tabs are never in the ledger."""
        for target_id in self.agent_targets():
            try:
                await self.send("Target.closeTarget", {"targetId": target_id}, quiet=True)
            except RelayClosed:
                break
            self.forget_target(target_id)

    async def _adopt(self, target_id: str) -> None:
        """Hold a focus-emulating session on an agent tab so it keeps rendering."""
        if target_id in self._adopted or self._closed:
            return
        try:
            sid = await self.attach(target_id, _ignore_event)
            self._adopted[target_id] = sid
            await self.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        except (CdpError, RelayClosed, asyncio.TimeoutError) as exc:
            logger.debug("relay: focus emulation on %s not enabled: %s", target_id, exc)

    # ── the daemon side ──

    async def _serve_version(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPNotFound()
        return web.json_response(
            {
                "Browser": _VERSION_BROWSER,
                "Protocol-Version": _VERSION_PROTOCOL,
                "webSocketDebuggerUrl": self.ws_url,
            }
        )

    async def _serve_client(self, request: web.Request) -> web.StreamResponse:
        # ``autoping=True`` so aiohttp answers the daemon's websocket keepalive
        # pings with pongs itself (the daemon's ``cdp_use`` client drops the
        # connection on an unanswered ping); with ``autoping=False`` aiohttp hands
        # the ping to this read loop, which forwards only TEXT. ``heartbeat`` stays
        # unset: the relay sends no pings of its own.
        if not self._authorized(request):
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse(max_msg_size=_UNLIMITED_MESSAGE, autoping=True)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                await self._forward_client_message(ws, msg.data)
        finally:
            self._clients.discard(ws)
            self._routes = {gid: r for gid, r in self._routes.items() if r[0] is not ws}
        return ws

    async def _forward_client_message(self, ws: web.WebSocketResponse, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "id" in msg:
            gid = next(self._down_ids)
            self._routes[gid] = (ws, msg["id"])
            if msg.get("method") == "Target.createTarget":
                self._create_calls.add(gid)
            msg["id"] = gid
            raw = json.dumps(msg)
        if self._up is None or self._closed:
            return
        await self._send_upstream(raw)
        self.forwarded_up += 1

    # ── the browser side ──

    async def _send_upstream(self, raw: str) -> None:
        if self._up is None or self._up_lock is None:
            raise RelayClosed("relay is not connected")
        async with self._up_lock:
            await self._up.send(raw)

    async def _read_upstream(self) -> None:
        assert self._up is not None
        try:
            async for raw in self._up.messages():
                await self._dispatch_upstream(raw)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the connection is reported, not re-raised
            logger.debug("relay: upstream reader ended", exc_info=True)
        finally:
            if not self._closed:
                self._closed = True
                self._fail_pending(RelayClosed("browser connection closed"))
                for ws in list(self._clients):
                    await _close_quietly(ws)
                if self.on_upstream_closed is not None:
                    try:
                        await self.on_upstream_closed()
                    except Exception:  # noqa: BLE001 - a listener must not kill the reader
                        logger.debug("relay: on_upstream_closed failed", exc_info=True)

    async def _dispatch_upstream(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        mid = msg.get("id")
        if isinstance(mid, int) and mid >= RELAY_ID_BASE:
            fut = self._pending.get(mid)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        sid = msg.get("sessionId")
        if isinstance(sid, str) and sid in self._own_sessions:
            if "method" in msg:
                self._track_child(sid, msg)
                asyncio.create_task(self._own_sessions[sid](msg))
            return
        if isinstance(mid, int):
            await self._route_reply(mid, msg)
            return
        if self._closing:
            params = msg.get("params") or {}
            if sid in self._closing or params.get("sessionId") in self._closing:
                closed = params.get("sessionId") or sid
                if msg.get("method") == "Target.detachedFromTarget" and isinstance(closed, str):
                    self._closing.discard(closed)
                return
        self._observe_event(msg)
        for ws in list(self._clients):
            try:
                await ws.send_str(raw)
            except Exception:  # noqa: BLE001 - a gone client is dropped on its own loop
                pass
        self.forwarded_down += 1

    async def _route_reply(self, gid: int, msg: dict[str, Any]) -> None:
        route = self._routes.pop(gid, None)
        if gid in self._create_calls:
            self._create_calls.discard(gid)
            tid = (msg.get("result") or {}).get("targetId")
            if isinstance(tid, str) and tid:
                await self._record_agent_target(tid)
        if route is None:
            return
        ws, original = route
        msg["id"] = original
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:  # noqa: BLE001 - a gone client is dropped on its own loop
            return
        self.forwarded_down += 1

    async def _record_agent_target(self, target_id: str) -> None:
        if target_id not in self._agent_targets:
            self._agent_targets.append(target_id)
        asyncio.create_task(self._adopt(target_id))
        if self.on_target_created is not None:
            try:
                await self.on_target_created(target_id)
            except Exception:  # noqa: BLE001 - a listener must not stall the reply
                logger.debug("relay: on_target_created failed", exc_info=True)

    def _observe_event(self, msg: dict[str, Any]) -> None:
        """Keep the ledger honest when the browser reports a tab gone."""
        if msg.get("method") != "Target.targetDestroyed":
            return
        tid = (msg.get("params") or {}).get("targetId")
        if isinstance(tid, str):
            self.forget_target(tid)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


async def _ignore_event(_msg: dict[str, Any]) -> None:
    return None


async def _connect_ws(upstream_ws_url: str) -> _WsUpstream:
    # No total or connect timeout: the Allow sheet has none either.
    timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None)
    session = aiohttp.ClientSession(timeout=timeout)
    try:
        ws = await session.ws_connect(
            upstream_ws_url,
            max_msg_size=_UNLIMITED_MESSAGE,
            autoping=False,
            heartbeat=None,
        )
    except Exception:
        await session.close()
        raise
    return _WsUpstream(ws, session)


async def _close_quietly(ws: web.WebSocketResponse | aiohttp.ClientWebSocketResponse) -> None:
    try:
        await asyncio.wait_for(ws.close(), _CLOSE_TIMEOUT_SECS)
    except Exception:  # noqa: BLE001 - teardown of a connection already failing
        pass


def _site_port(site: web.TCPSite) -> int:
    server: Any = site._server  # aiohttp exposes the bound sockets only here
    assert server is not None and server.sockets
    return int(server.sockets[0].getsockname()[1])
