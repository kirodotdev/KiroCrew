"""The CDP relay (``browser_harness/relay.py``) against a fake browser on port 0.

The fake answers every request with a reply that echoes the method and the id it
saw, hands out session ids on ``Target.attachToTarget`` and a target id on
``Target.createTarget``, records every ``Emulation.setFocusEmulationEnabled``, and
can push events of its own. No sleeps as barriers: every wait is on an event or a
bounded ``wait_for``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from kiro_crew.browser_harness import relay as relay_mod
from kiro_crew.browser_harness.relay import RELAY_ID_BASE, CdpError, CdpRelay, RelayClosed

#: A hang ceiling only: every wait below awaits a signal the relay emits.
_WAIT = 30.0


class FakeBrowser:
    """A CDP endpoint good enough for the relay's wire behaviour."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.focus_calls: list[str] = []
        self.sessions = 0
        self.created = 0
        self.context_ids: Iterator[str] = iter(())
        self.ws: web.WebSocketResponse | None = None
        self.connected = asyncio.Event()
        self.focus_seen = asyncio.Event()
        self.ws_url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/devtools/browser/fake", self._serve)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.ws_url = f"ws://127.0.0.1:{port}/devtools/browser/fake"
        return self.ws_url

    async def stop(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def emit(self, msg: dict[str, Any]) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps(msg))

    async def _serve(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        async for raw in ws:
            if raw.type is not aiohttp.WSMsgType.TEXT:
                continue
            msg = json.loads(raw.data)
            self.seen.append(msg)
            await ws.send_str(json.dumps(self._reply(msg)))
        return ws

    def _reply(self, msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        result: dict[str, Any] = {"echo": method, "seen_id": msg["id"]}
        if method == "Target.attachToTarget":
            self.sessions += 1
            result["sessionId"] = f"S{self.sessions}"
        elif method == "Target.createTarget":
            self.created += 1
            result["targetId"] = "T-agent" if self.created == 1 else f"T-agent{self.created}"
        elif method == "Emulation.setFocusEmulationEnabled":
            self.focus_calls.append(str(msg.get("sessionId")))
            self.focus_seen.set()
        elif method == "Target.createBrowserContext":
            result["browserContextId"] = next(self.context_ids)
        elif method == "Fail.me":
            return {"id": msg["id"], "error": {"code": -32000, "message": "nope"}}
        reply: dict[str, Any] = {"id": msg["id"], "result": result}
        if "sessionId" in msg:
            reply["sessionId"] = msg["sessionId"]
        return reply


@pytest_asyncio.fixture
async def browser() -> AsyncIterator[FakeBrowser]:
    fake = FakeBrowser()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest_asyncio.fixture
async def relay(browser: FakeBrowser) -> AsyncIterator[CdpRelay]:
    r = CdpRelay()
    await r.start(browser.ws_url)
    await asyncio.wait_for(browser.connected.wait(), _WAIT)
    try:
        yield r
    finally:
        await r.close()


async def _until(check: Any) -> None:
    """Await *check* turning true: the relay applies an event on its reader task."""

    async def _poll() -> None:
        while not check():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), _WAIT)


class Client:
    """A daemon stand-in: one websocket to the relay, with a mailbox."""

    def __init__(self, relay: CdpRelay) -> None:
        self.relay = relay
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Client:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.relay.ws_url, autoping=False)
        self._task = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._ws is not None and self._session is not None and self._task is not None
        await self._ws.close()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        await self._session.close()

    async def _pump(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await self.inbox.put(json.loads(msg.data))

    async def send(self, body: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send_str(json.dumps(body))

    async def recv(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.inbox.get(), _WAIT)


@pytest.mark.asyncio
async def test_json_version_names_the_relay_websocket(relay: CdpRelay) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{relay.url}/json/version") as resp:
            assert resp.status == 200
            body = await resp.json()
    assert body["webSocketDebuggerUrl"] == relay.ws_url
    assert relay.url.startswith("http://127.0.0.1:")
    assert relay.ws_url.startswith("ws://127.0.0.1:")


@pytest.mark.asyncio
async def test_without_the_secret_the_relay_is_a_404_on_every_path(relay: CdpRelay) -> None:
    """A local process that found the port learns nothing and forwards nothing.

    The secret rides in the URL the daemon gets (``/<secret>/json/version``,
    ``/devtools/browser/<secret>``); the bare Chrome-shaped paths, a wrong secret
    and a prefix of the right one are all refused before any CDP is forwarded.
    """
    base = relay.url.rsplit("/", 1)[0]
    secret = relay.url.rsplit("/", 1)[1]
    assert len(secret) >= 32
    wrong = "x" * len(secret)
    async with aiohttp.ClientSession() as session:
        for path in (
            "/json/version",
            "/json",
            "/json/list",
            f"/{wrong}/json/version",
            f"/{secret[:-1]}/json/version",
        ):
            async with session.get(base + path) as resp:
                assert resp.status == 404, path
        for ws_path in ("/devtools/browser/relay", f"/devtools/browser/{wrong}"):
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await session.ws_connect(base.replace("http", "ws", 1) + ws_path, autoping=False)
            assert err.value.status == 404, ws_path
    assert relay.forwarded_up == 0


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="--remote-debugging-pipe is POSIX-only: on Windows the supervisor launches "
    "Chromium on a loopback port (_launch_chromium_port) and never builds a pipe upstream",
)
@pytest.mark.asyncio
async def test_a_pipe_upstream_carries_nul_terminated_cdp_both_ways() -> None:
    """The ``--remote-debugging-pipe`` transport, with a coroutine as the browser.

    The relay writes requests ``\\0``-terminated on one pipe and reads replies the
    same way on the other; a relay-owned request round-trips and a daemon client
    over the secret URL gets its reply renumbered back, exactly as over a websocket.
    """
    import os

    to_browser_r, to_browser_w = os.pipe()
    from_browser_r, from_browser_w = os.pipe()
    loop = asyncio.get_running_loop()
    browser_in = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(browser_in), os.fdopen(to_browser_r, "rb", 0)
    )
    out_tr, out_pr = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(from_browser_w, "wb", 0)
    )
    browser_out = asyncio.StreamWriter(out_tr, out_pr, None, loop)
    seen: list[dict[str, Any]] = []

    async def fake_browser() -> None:
        while True:
            try:
                chunk = await browser_in.readuntil(b"\0")
            except asyncio.IncompleteReadError:
                return
            msg = json.loads(chunk[:-1])
            seen.append(msg)
            reply = {"id": msg["id"], "result": {"echo": msg["method"], "seen_id": msg["id"]}}
            browser_out.write(json.dumps(reply).encode() + b"\0")
            await browser_out.drain()

    pump = asyncio.create_task(fake_browser())
    r = CdpRelay()
    upstream = await relay_mod.pipe_upstream_from_fds(from_browser_r, to_browser_w)
    await r.start(upstream)
    try:
        res = await asyncio.wait_for(r.send("Browser.getVersion"), _WAIT)
        assert res["echo"] == "Browser.getVersion" and res["seen_id"] >= RELAY_ID_BASE
        async with Client(r) as c:
            await c.send({"id": 42, "method": "Page.enable"})
            reply = await c.recv()
        assert reply["id"] == 42 and reply["result"]["echo"] == "Page.enable"
        assert reply["result"]["seen_id"] == 1
        assert r.upstream_url == "pipe"
    finally:
        await r.close()
        browser_out.close()
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
    assert [m["method"] for m in seen] == [
        "Target.setDiscoverTargets",
        "Browser.getVersion",
        "Page.enable",
    ]


@pytest.mark.asyncio
async def test_two_clients_get_disjoint_upstream_ids_and_their_own_replies(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    async with Client(relay) as a, Client(relay) as b:
        await a.send({"id": 1, "method": "A.first"})
        await b.send({"id": 1, "method": "B.first"})
        reply_a = await a.recv()
        reply_b = await b.recv()
    assert reply_a["id"] == 1 and reply_a["result"]["echo"] == "A.first"
    assert reply_b["id"] == 1 and reply_b["result"]["echo"] == "B.first"
    upstream_ids = sorted(m["id"] for m in browser.seen if m["id"] < RELAY_ID_BASE)
    assert upstream_ids == [1, 2]
    assert {reply_a["result"]["seen_id"], reply_b["result"]["seen_id"]} == {1, 2}


@pytest.mark.asyncio
async def test_relay_requests_use_the_high_id_range_and_surface_cdp_errors(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    result = await relay.send("Browser.getVersion")
    assert result["echo"] == "Browser.getVersion"
    assert result["seen_id"] >= RELAY_ID_BASE
    with pytest.raises(CdpError):
        await relay.send("Fail.me")
    assert await relay.send("Fail.me", quiet=True) == {}


@pytest.mark.asyncio
async def test_relay_owned_session_events_never_reach_a_client(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    got: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_event(msg: dict[str, Any]) -> None:
        await got.put(msg)

    async with Client(relay) as client:
        sid = await relay.attach("T-user", on_event)
        assert relay.owns_session(sid)
        await browser.emit({"method": "Page.screencastFrame", "sessionId": sid, "params": {"n": 1}})
        frame = await asyncio.wait_for(got.get(), _WAIT)
        assert frame["params"] == {"n": 1}
        # A browser-level event still reaches the client, so ordering proves the
        # frame above was withheld rather than merely late.
        await browser.emit({"method": "Browser.downloadWillBegin", "params": {"x": 1}})
        seen = await client.recv()
        assert seen["method"] == "Browser.downloadWillBegin"
        assert client.inbox.empty()
        await relay.detach(sid)
        assert not relay.owns_session(sid)


@pytest.mark.asyncio
async def test_the_tab_ledger_records_at_the_create_reply_and_turns_focus_emulation_on(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    created: asyncio.Queue[str] = asyncio.Queue()

    async def on_created(target_id: str) -> None:
        await created.put(target_id)

    relay.on_target_created = on_created
    async with Client(relay) as client:
        await client.send(
            {"id": 7, "method": "Target.createTarget", "params": {"url": "about:blank"}}
        )
        reply = await client.recv()
        assert reply["id"] == 7 and reply["result"]["targetId"] == "T-agent"
        assert await asyncio.wait_for(created.get(), _WAIT) == "T-agent"
        assert relay.agent_targets() == ["T-agent"]
        # Adoption is a task: wait for the focus call to land upstream.
        await asyncio.wait_for(browser.focus_seen.wait(), _WAIT)
        assert browser.focus_calls == ["S1"]
        await browser.emit({"method": "Target.targetDestroyed", "params": {"targetId": "T-agent"}})
        await _until(lambda: relay.agent_targets() == [])


@pytest.mark.asyncio
async def test_a_create_context_opens_the_daemon_s_tab_in_that_context_only(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    # browser.isolation: per_session -- the tab a chat's op opens lands in its context;
    # one the daemon names a context for keeps it; with none set nothing is added.
    async with Client(relay) as client:
        relay.create_context = "CTX-a"
        await client.send({"id": 1, "method": "Target.createTarget", "params": {"url": "x"}})
        await client.recv()
        await client.send({"id": 2, "method": "Target.createTarget"})
        await client.recv()
        await client.send(
            {"id": 3, "method": "Target.createTarget", "params": {"browserContextId": "CTX-x"}}
        )
        await client.recv()
        await client.send({"id": 4, "method": "Page.navigate", "params": {"url": "x"}})
        await client.recv()
        relay.create_context = ""
        await client.send({"id": 5, "method": "Target.createTarget", "params": {"url": "x"}})
        await client.recv()
    # The relay's own discovery request at start is not one of the daemon's sends.
    skip = (
        "Target.attachToTarget",
        "Emulation.setFocusEmulationEnabled",
        "Target.setDiscoverTargets",
    )
    sent = [m.get("params") for m in browser.seen if m["method"] not in skip]
    assert sent == [
        {"url": "x", "browserContextId": "CTX-a"},
        {"browserContextId": "CTX-a"},
        {"browserContextId": "CTX-x"},
        {"url": "x"},
        {"url": "x"},
    ]
    # The ledger remembers each tab's context, so a chat is shown only the tabs of
    # the context it browses in; a destroyed tab is forgotten with it.
    assert [relay.context_of(t) for t in relay.agent_targets()] == ["CTX-a", "CTX-a", "CTX-x", ""]
    relay.forget_target("T-agent")
    assert relay.context_of("T-agent") == "" and relay._target_contexts.get("T-agent") is None


@pytest.mark.asyncio
async def test_a_session_context_is_made_once_disposed_on_detach_and_disposed_on_end(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    browser.context_ids = iter(["CTX-1", "CTX-2"])
    assert await relay.ensure_session_context("chat-a") == ("CTX-1", True)
    assert await relay.ensure_session_context("chat-a") == ("CTX-1", False)
    assert await relay.ensure_session_context("chat-b") == ("CTX-2", True)
    assert relay.session_context("chat-a") == "CTX-1" and relay.session_context("x") == ""
    assert relay.session_context_ids() == ["CTX-1", "CTX-2"]
    await relay.dispose_session_context("chat-a")
    await relay.dispose_session_context("chat-a")  # already gone: no second CDP call
    assert relay.session_context_ids() == ["CTX-2"]
    calls = [
        (m["method"], m["params"])
        for m in browser.seen
        if m["method"].startswith("Target.") and m["method"] != "Target.setDiscoverTargets"
    ]
    assert calls == [
        ("Target.createBrowserContext", {"disposeOnDetach": True}),
        ("Target.createBrowserContext", {"disposeOnDetach": True}),
        ("Target.disposeBrowserContext", {"browserContextId": "CTX-1"}),
    ]


@pytest.mark.asyncio
async def test_close_agent_targets_closes_only_ledgered_tabs(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    async with Client(relay) as client:
        await client.send({"id": 1, "method": "Target.createTarget", "params": {}})
        await client.recv()
    await relay.close_agent_targets()
    closed = [m["params"]["targetId"] for m in browser.seen if m["method"] == "Target.closeTarget"]
    assert closed == ["T-agent"]
    assert relay.agent_targets() == []


@pytest.mark.asyncio
async def test_a_client_that_disconnects_drops_its_routes(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    async with Client(relay) as client:
        await client.send({"id": 3, "method": "Slow.thing"})
        await client.recv()
    assert relay._routes == {}


@pytest.mark.asyncio
async def test_the_relay_answers_a_client_keepalive_ping_with_a_pong(relay: CdpRelay) -> None:
    """The daemon connects with the ``websockets`` library, which pings every 20 s and
    drops the connection if a pong does not come back within the cycle. The relay must
    answer that ping itself (``autoping=True``); the earlier ``autoping=False`` handed
    the ping to the forward loop, which forwards only TEXT, so the daemon reattached
    every ~20 s and every page-session command issued against the torn-down session hung.
    """
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(relay.ws_url, autoping=False) as ws:
            await ws.ping(b"beat")
            msg = await asyncio.wait_for(ws.receive(), _WAIT)
            assert msg.type is aiohttp.WSMsgType.PONG, msg.type


@pytest.mark.asyncio
async def test_closing_the_browser_closes_the_relay_and_fails_pending_sends(
    browser: FakeBrowser,
) -> None:
    closed = asyncio.Event()
    r = CdpRelay()

    async def on_closed() -> None:
        closed.set()

    await r.start(browser.ws_url)
    r.on_upstream_closed = on_closed
    await asyncio.wait_for(browser.connected.wait(), _WAIT)
    await browser.stop()
    await asyncio.wait_for(closed.wait(), _WAIT)
    assert r.closed
    with pytest.raises(RelayClosed):
        await r.send("Browser.getVersion")
    await r.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_frees_the_port(browser: FakeBrowser) -> None:
    r = CdpRelay()
    await r.start(browser.ws_url)
    await r.close()
    await r.close()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectionError):
            await session.get(f"{r.url}/json/version")


@pytest.mark.asyncio
async def test_start_turns_target_discovery_on_for_the_relay(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    discover = [m for m in browser.seen if m["method"] == "Target.setDiscoverTargets"]
    assert len(discover) == 1
    assert discover[0]["params"] == {"discover": True} and discover[0]["id"] >= RELAY_ID_BASE


async def _create_agent_tab(client: Client) -> None:
    await client.send({"id": 1, "method": "Target.createTarget", "params": {}})
    await client.recv()


def _created(target_id: str, opener: str | None, kind: str = "page") -> dict[str, Any]:
    info: dict[str, Any] = {"targetId": target_id, "type": kind, "url": "https://idp/login"}
    if opener is not None:
        info["openerId"] = opener
    return {"method": "Target.targetCreated", "params": {"targetInfo": info}}


@pytest.mark.asyncio
async def test_a_popup_an_agent_tab_opens_joins_the_ledger_with_its_opener(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    created: list[str] = []

    async def on_created(target_id: str) -> None:
        created.append(target_id)

    relay.on_target_created = on_created
    async with Client(relay) as client:
        await _create_agent_tab(client)
        await browser.emit(_created("T-popup", "T-agent"))
        await _until(lambda: "T-popup" in relay.agent_targets())
        assert relay.agent_targets() == ["T-agent", "T-popup"]
        assert relay.opener_of("T-popup") == "T-agent"
        assert created == ["T-agent", "T-popup"]
        # Focus emulation reaches the popup too, so a headless popup keeps painting.
        await _until(lambda: len(browser.focus_calls) == 2)
        await browser.emit({"method": "Target.targetDestroyed", "params": {"targetId": "T-popup"}})
        await _until(lambda: relay.agent_targets() == ["T-agent"])
        # The link outlives the popup: the route walks it back to the opener.
        assert relay.opener_of("T-popup") == "T-agent"
        assert client.inbox.empty()


def _emulation_sessions(browser: FakeBrowser) -> list[str]:
    return [
        str(m.get("sessionId")) for m in browser.seen if m["method"] in relay_mod.EMULATE_METHODS
    ]


@pytest.mark.asyncio
async def test_emulate_sends_every_override_on_the_one_session_the_tab_was_adopted_with(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    # Chrome keeps an override per session and a reset from another session leaves
    # it, so a set and its reset must share one session, even when the first call
    # races the adoption the create reply started.
    async with Client(relay) as client:
        await _create_agent_tab(client)
        dark = {"features": [{"name": "prefers-color-scheme", "value": "dark"}]}
        await relay.emulate("T-agent", "Emulation.setEmulatedMedia", dark)
        await relay.emulate("T-agent", "Emulation.setEmulatedMedia", {"features": []})
        await relay.emulate("T-agent", "Emulation.clearDeviceMetricsOverride", {})
    attaches = [m for m in browser.seen if m["method"] == "Target.attachToTarget"]
    assert len(attaches) == 1
    assert _emulation_sessions(browser) == ["S1", "S1", "S1"]
    await _until(lambda: browser.focus_calls == ["S1"])


@pytest.mark.asyncio
async def test_emulate_refuses_a_tab_outside_the_ledger_and_any_other_command(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    async with Client(relay) as client:
        await _create_agent_tab(client)
        with pytest.raises(CdpError):
            await relay.emulate("T-user", "Emulation.clearDeviceMetricsOverride", {})
        with pytest.raises(ValueError):
            await relay.emulate("T-agent", "Runtime.evaluate", {"expression": "1"})
    assert _emulation_sessions(browser) == []
    assert not [m for m in browser.seen if m.get("params", {}).get("targetId") == "T-user"]


@pytest.mark.asyncio
async def test_the_sign_in_guard_hears_the_tabs_session_before_the_daemon_hears_of_the_tab(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    adopted: list[tuple[str, str]] = []
    heard: list[tuple[str, str, str]] = []

    async def on_adopted(target_id: str, session_id: str) -> None:
        adopted.append((target_id, session_id))

    async def on_event(target_id: str, session_id: str, msg: dict[str, Any]) -> None:
        heard.append((target_id, session_id, str(msg.get("method"))))

    relay.on_tab_adopted, relay.on_tab_event = on_adopted, on_event
    async with Client(relay) as client:
        await client.send({"id": 1, "method": "Target.createTarget", "params": {}})
        await client.recv()
        # The reply was held until the guard had the tab's session.
        assert adopted == [("T-agent", "S1")]
        paused = {"sessionId": "S1", "method": "Fetch.requestPaused", "params": {"requestId": "R"}}
        await browser.emit(paused)
        await _until(lambda: heard == [("T-agent", "S1", "Fetch.requestPaused")])
        assert relay.adopted_sessions() == {"T-agent": "S1"}
        assert client.inbox.empty()


@pytest.mark.asyncio
async def test_a_browser_wide_fetch_pause_reaches_the_guard_and_never_a_client(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    # A popup's first request is paused on the browser target before the relay can
    # adopt the popup; that pause carries no session and is the guard's alone.
    heard: list[str] = []

    async def on_paused(msg: dict[str, Any]) -> None:
        heard.append(str((msg.get("params") or {}).get("requestId")))

    relay.on_browser_paused = on_paused
    async with Client(relay) as client:
        await browser.emit({"method": "Fetch.requestPaused", "params": {"requestId": "R9"}})
        await _until(lambda: heard == ["R9"])
        assert client.inbox.empty()


@pytest.mark.asyncio
async def test_the_persons_own_pages_and_non_pages_stay_out_of_the_ledger(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    async with Client(relay) as client:
        await _create_agent_tab(client)
        await browser.emit(_created("T-user-popup", "T-user"))
        await browser.emit(_created("T-user-tab", None))
        await browser.emit(_created("T-worker", "T-agent", kind="service_worker"))
        # A marker the relay forwards, so the three events above are known applied.
        await browser.emit({"method": "Browser.downloadWillBegin", "params": {}})
        assert (await client.recv())["method"] == "Browser.downloadWillBegin"
    assert relay.agent_targets() == ["T-agent"]
    assert relay.opener_of("T-user-popup") == ""


@pytest.mark.asyncio
async def test_discovery_events_reach_a_client_only_once_it_asks_for_them(
    relay: CdpRelay, browser: FakeBrowser
) -> None:
    async with Client(relay) as client:
        await browser.emit(_created("T-x", None))
        await browser.emit({"method": "Browser.downloadWillBegin", "params": {}})
        assert (await client.recv())["method"] == "Browser.downloadWillBegin"
        await client.send(
            {"id": 9, "method": "Target.setDiscoverTargets", "params": {"discover": True}}
        )
        assert (await client.recv())["id"] == 9
        await browser.emit(_created("T-y", None))
        assert (await client.recv())["method"] == "Target.targetCreated"


@pytest.mark.asyncio
async def test_the_opener_map_is_bounded_and_drops_the_oldest_link() -> None:
    r = CdpRelay()
    r._agent_targets.append("T-agent")

    async def _no_record(_tid: str, _context: str = "") -> None:
        return None

    r._record_agent_target = _no_record  # type: ignore[method-assign]
    for i in range(relay_mod._OPENER_CAP + 1):
        await r._observe_popup({"targetId": f"P{i}", "type": "page", "openerId": "T-agent"})
    assert len(r._openers) == relay_mod._OPENER_CAP
    assert r.opener_of("P0") == ""
    assert r.opener_of(f"P{relay_mod._OPENER_CAP}") == "T-agent"


@pytest.mark.asyncio
async def test_a_popup_is_ledgered_in_its_openers_browser_context() -> None:
    """Under per-chat isolation a window.open stays in the chat's context, so the
    chat's ``tabs`` (filtered by context) still shows it."""
    r = CdpRelay()
    r._agent_targets.append("T-agent")
    r._target_contexts["T-agent"] = "CTX-a"
    recorded: list[tuple[str, str]] = []

    async def _record(tid: str, context: str = "") -> None:
        recorded.append((tid, context))

    r._record_agent_target = _record  # type: ignore[method-assign]
    await r._observe_popup({"targetId": "P1", "type": "page", "openerId": "T-agent"})
    assert recorded == [("P1", "CTX-a")]


def test_every_relay_start_mints_its_own_secret() -> None:
    a, b = CdpRelay(), CdpRelay()
    assert a._secret != b._secret and len(a._secret) >= 32


def test_constants_pin_the_contract() -> None:
    assert relay_mod.RELAY_ID_BASE == 1_000_000_000
    assert relay_mod.LOOPBACK_HOST == "127.0.0.1"
