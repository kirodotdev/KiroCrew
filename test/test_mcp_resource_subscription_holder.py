"""The resource-subscription holder keeps a session's subscriptions up (RFC Phase 2).

``rfc-mcp-resource-subscriptions.md`` §4.1-§4.4, §4.6 and §4.7. A real gatewayd
runs in-process against ``fake_pool_mcp_server.py`` in subscription mode; the
session's kiro-cli stub is a real stub process claimed with its token, and the
holder is the one :class:`ResourceSubscriptionService` opens from this process.
The scripted update is sent by a third connection, of another session, calling
the fake server's ``emit_update`` tool -- the server does not filter, the
gateway routes it to subscribers.

Pinned here, against the RFC's Phase 2 exit criteria:

* the update reaches the holder after the kiro-cli stub detaches, after the
  backend process is killed, and after the daemon restarts (and after this
  process restarts, from the store);
* a launch the operator's approval does not admit pauses the subscription
  with a reason;
* a subscribe that would pin one more backend than ``max_pinned_backends`` is
  refused with a reason, and so is a 21st subscription for one session;
* ``describe-session`` hands out no token and no ``stub_uuid``, and closing a
  session removes its holders and record.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fake_pool_mcp_server import recorded
from mcp_resource_subscription_helpers import (
    INIT_PARAMS,
    RawConnection,
    StubProcess,
    register_payload_like_the_stub,
    running_daemon,
    running_stub,
)

from kiro_crew import platform_compat as pc
from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
from kiro_crew.mcp_gateway import resource_subscriptions as rs
from kiro_crew.mcp_gateway.claim import mint_stub_session_token, send_claim
from kiro_crew.mcp_gateway.resource_holder import (
    HANDSHAKE_TIMEOUT_SECS,
    HolderNotLive,
    HolderPaused,
    HolderSpec,
    ResourceHolder,
    _Connection,
    describe_session,
    holder_register_frame,
)
from kiro_crew.mcp_gateway.resource_subscription_store import (
    RETAINED_CONTAINER_MAX_BYTES,
    STORE_DIR_LEAF,
    STORE_LEAF,
    HolderRecord,
    ResourceSubscriptionStore,
    StoreUnreadable,
)
from kiro_crew.mcp_gateway.resource_subscriptions import (
    EVENT_MAY_HAVE_CHANGED,
    EVENT_PAUSED,
    EVENT_UPDATED,
    MAX_SUBSCRIPTIONS_PER_SESSION,
    EventStream,
    RecordingSink,
    ResourceEvent,
    ResourceSubscriptionService,
    SubscribeRefused,
)
from kiro_crew.testing.wait import async_wait_until, await_until, finished

pytestmark = [
    pytest.mark.xdist_group("mcp_gateway"),
    pytest.mark.skipif(pc.IS_WINDOWS, reason="claims resolve the peer's ancestry via SO_PEERCRED"),
]

SESSION = "dashboard:held-session"
CHANNEL = "C_HELD"
URI = "crux://reviews/CR-1"
TRIGGER = "dashboard:update-sender"
_HANG_SECS = 30.0  # only turns a hang into a named failure; the signal decides


@pytest.fixture
def paths(tmp_path: Path, short_sock_dir: Path) -> dict[str, Path]:
    work = tmp_path / "ws"
    work.mkdir()
    other_work = tmp_path / "ws-other"
    other_work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return {
        "sock": short_sock_dir / "gw.sock",
        "work": work,
        "other_work": other_work,
        "home": home,
        "launches": tmp_path / "launches.txt",
        "subscribes": tmp_path / "subscribes.txt",
        "callers": tmp_path / "callers.txt",
        "store": tmp_path / "data" / STORE_DIR_LEAF / STORE_LEAF,
    }


def _daemon(monkeypatch: pytest.MonkeyPatch, p: dict[str, Path], **kwargs: Any) -> Any:
    return running_daemon(
        monkeypatch, p["sock"], p["work"], p["launches"], p["subscribes"], p["callers"], **kwargs
    )


def _service(
    p: dict[str, Path], sink: RecordingSink, *, enabled: bool = True, cap: int = 16
) -> ResourceSubscriptionService:
    settings = McpResourceSubscriptionsConfig(enabled=enabled, max_pinned_backends=cap)
    return ResourceSubscriptionService(
        store=ResourceSubscriptionStore(p["store"]),
        socket_path=lambda: p["sock"],
        sink=sink,
        settings=lambda: settings,
        ping_interval_secs=0.2,
        reconnect_delay_secs=0.05,
    )


async def _kiro_cli_stub(
    stack: AsyncExitStack,
    p: dict[str, Path],
    session_key: str = SESSION,
    work: Path | None = None,
) -> StubProcess:
    token = mint_stub_session_token()
    await send_claim(str(p["sock"]), os.getpid(), session_key, CHANNEL, token)
    return await stack.enter_async_context(
        running_stub(p["sock"], work or p["work"], p["home"], session_token=token)
    )


async def _update_sender(stack: AsyncExitStack, p: dict[str, Path]) -> RawConnection:
    """Another session's connection on the same backend, to script updates with."""
    spec = HolderSpec(
        session_key=TRIGGER,
        channel_id="",
        server="fake",
        register=register_payload_like_the_stub(p["sock"], p["work"]),
        initialize={},
    )
    conn = await RawConnection.register(p["sock"], holder_register_frame(spec))
    stack.push_async_callback(conn.close)
    assert "result" in await conn.request(1, "initialize", INIT_PARAMS)
    return conn


class _Ids:
    next_id = 100


async def _send_update(sender: RawConnection, uri: str = URI) -> None:
    """``emit_update`` until a call lands; a respawn answers one with a retry error."""
    for _ in range(5):
        _Ids.next_id += 1
        reply = await sender.request(
            _Ids.next_id, "tools/call", {"name": "emit_update", "arguments": {"uri": uri}}
        )
        if "result" in reply:
            return
    raise AssertionError(f"emit_update never landed: {reply!r}")


async def _update_reaches(sink: RecordingSink, sender: RawConnection, uri: str = URI) -> None:
    before = len(sink.of(EVENT_UPDATED, uri))
    await _send_update(sender, uri)
    await async_wait_until(
        lambda: len(sink.of(EVENT_UPDATED, uri)) > before,
        describe=lambda: f"no update for {uri}; events: {sink.events!r}",
    )


def _backend_pids(daemon: Any) -> list[int]:
    return [b.pid for b in daemon.backends() if b.pid is not None and b.is_alive]


@pytest.mark.asyncio
async def test_describe_session_hands_out_the_stubs_register_without_a_token(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    async with _daemon(monkeypatch, paths), AsyncExitStack() as stack:
        kiro = await _kiro_cli_stub(stack, paths)
        try:
            described = await describe_session(paths["sock"], SESSION, "fake")
            assert described["type"] == "session-described", described
            assert described["channel_id"] == CHANNEL
            assert described["private_backend"] is False
            assert "stub_session_token" not in described["register"]
            assert "stub_uuid" not in described["register"]
            assert described["register"]["server_name"] == "fake"
            assert described["initialize"]["params"] == INIT_PARAMS

            missing = await describe_session(paths["sock"], "dashboard:nobody", "fake")
            assert missing["type"] == "session-undescribed"
            assert "no live, initialized 'fake' stub" in missing["reason"]
        finally:
            await kiro.close()


@pytest.mark.asyncio
async def test_the_update_reaches_the_holder_after_the_kiro_cli_stub_detaches(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Exit criterion (1), with the shared grant held for the holder alone."""
    sink = RecordingSink()
    service = _service(paths, sink)
    async with _daemon(monkeypatch, paths) as daemon, AsyncExitStack() as stack:
        await service.start()
        stack.push_async_callback(service.close)
        kiro = await _kiro_cli_stub(stack, paths)
        assert "result" in await kiro.request(2, "resources/subscribe", {"uri": URI})
        assert "result" in await service.subscribe(SESSION, "fake", URI)
        sender = await _update_sender(stack, paths)
        await _update_reaches(sink, sender)

        await kiro.close()
        (backend,) = daemon.backends()
        holder = service.holder(SESSION, "fake")
        assert holder is not None
        await async_wait_until(
            lambda: len(backend._stub_inboxes) == 2,
            describe=lambda: f"stubs still attached: {list(backend._stub_inboxes)!r}",
        )
        await _update_reaches(sink, sender)
        assert [
            line for line in recorded(paths["subscribes"]) if line.startswith("unsubscribe")
        ] == []
        assert sink.of(EVENT_MAY_HAVE_CHANGED) == []
        assert len(recorded(paths["launches"])) == 1


@pytest.mark.asyncio
async def test_the_update_reaches_the_holder_after_the_backend_is_killed(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Exit criterion (2): the ping notices, the daemon respawns and replays."""
    sink = RecordingSink()
    service = _service(paths, sink)
    async with _daemon(monkeypatch, paths) as daemon, AsyncExitStack() as stack:
        await service.start()
        stack.push_async_callback(service.close)
        kiro = await _kiro_cli_stub(stack, paths)
        assert "result" in await service.subscribe(SESSION, "fake", URI)
        await kiro.close()
        (pid,) = _backend_pids(daemon)
        os.kill(pid, signal.SIGKILL)

        await async_wait_until(
            lambda: sink.of(EVENT_MAY_HAVE_CHANGED, URI),
            describe=lambda: f"no gap reported; events: {sink.events!r}",
        )
        assert len(recorded(paths["launches"])) == 2
        sender = await _update_sender(stack, paths)
        await _update_reaches(sink, sender)


@pytest.mark.asyncio
async def test_the_update_reaches_the_holder_after_the_daemon_restarts(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Exit criterion (3). The holder is the first client of the new backend, so it
    initializes it itself (§4.2); then this process restarts too, from the store."""
    sink = RecordingSink()
    service = _service(paths, sink)
    try:
        async with _daemon(monkeypatch, paths), AsyncExitStack() as stack:
            await service.start()
            kiro = await _kiro_cli_stub(stack, paths)
            assert "result" in await service.subscribe(SESSION, "fake", URI)
            await kiro.close()

        async with _daemon(monkeypatch, paths) as daemon, AsyncExitStack() as stack:
            await async_wait_until(
                lambda: sink.of(EVENT_MAY_HAVE_CHANGED, URI),
                describe=lambda: f"no gap reported; events: {sink.events!r}",
            )
            assert len(daemon.backends()) == 1
            sender = await _update_sender(stack, paths)
            await _update_reaches(sink, sender)

            await service.close()
            restarted_sink = RecordingSink()
            restarted = _service(paths, restarted_sink)
            stack.push_async_callback(restarted.close)
            await restarted.start()
            await async_wait_until(
                lambda: restarted_sink.of(EVENT_MAY_HAVE_CHANGED, URI),
                describe=lambda: f"no gap after resume; events: {restarted_sink.events!r}",
            )
            await _update_reaches(restarted_sink, sender)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_a_changed_launch_approval_pauses_the_subscription_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    approved = [True]
    sink = RecordingSink()
    service = _service(paths, sink)
    async with (
        _daemon(monkeypatch, paths, approved=lambda: approved[0]) as daemon,
        AsyncExitStack() as stack,
    ):
        try:
            await service.start()
            kiro = await _kiro_cli_stub(stack, paths)
            assert "result" in await service.subscribe(SESSION, "fake", URI)
            await kiro.close()
            assert service.pinned_backends()

            approved[0] = False
            (pid,) = _backend_pids(daemon)
            os.kill(pid, signal.SIGKILL)
            await async_wait_until(
                lambda: sink.of(EVENT_PAUSED, URI),
                describe=lambda: f"never paused; events: {sink.events!r}",
            )
            (paused,) = sink.of(EVENT_PAUSED, URI)
            assert "could not start 'fake'" in paused.reason
            assert service.holder(SESSION, "fake") is None
            assert service.pinned_backends() == set()
            await async_wait_until(
                lambda: [r.paused_reason for r in ResourceSubscriptionStore(paths["store"]).load()]
                == [paused.reason],
                off_loop=True,
                describe=lambda: f"store: {ResourceSubscriptionStore(paths['store']).load()!r}",
            )
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_a_subscribe_that_would_pin_one_backend_too_many_is_refused(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """The cap counts backends: a second session on the same pool key pins nothing."""
    sink = RecordingSink()
    service = _service(paths, sink, cap=1)
    async with _daemon(monkeypatch, paths), AsyncExitStack() as stack:
        await service.start()
        stack.push_async_callback(service.close)
        for session_key, work in (
            (SESSION, paths["work"]),
            ("dashboard:same-key", paths["work"]),
            ("dashboard:other-key", paths["other_work"]),
        ):
            await _kiro_cli_stub(stack, paths, session_key, work)

        assert "result" in await service.subscribe(SESSION, "fake", URI)
        assert "result" in await service.subscribe("dashboard:same-key", "fake", URI)
        assert len(service.pinned_backends()) == 1
        # Both holders ride the one backend, each under a stub_uuid of its own.
        sender = await _update_sender(stack, paths)
        await _send_update(sender)
        await async_wait_until(
            lambda: {e.session_key for e in sink.of(EVENT_UPDATED, URI)}
            == {SESSION, "dashboard:same-key"},
            describe=lambda: f"updates reached: {sink.events!r}",
        )
        assert sink.of(EVENT_MAY_HAVE_CHANGED) == []
        with pytest.raises(SubscribeRefused, match="would pin 2 MCP backends.*allows 1"):
            await service.subscribe("dashboard:other-key", "fake", URI)
        assert service.holder("dashboard:other-key", "fake") is None
        assert [r.session_key for r in ResourceSubscriptionStore(paths["store"]).load()] == [
            SESSION,
            "dashboard:same-key",
        ]


@pytest.mark.asyncio
async def test_a_session_holds_at_most_twenty_subscriptions(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    sink = RecordingSink()
    service = _service(paths, sink)
    async with _daemon(monkeypatch, paths), AsyncExitStack() as stack:
        await service.start()
        stack.push_async_callback(service.close)
        await _kiro_cli_stub(stack, paths)
        uris = [f"crux://reviews/CR-{n}" for n in range(MAX_SUBSCRIPTIONS_PER_SESSION)]
        for uri in uris:
            assert "result" in await service.subscribe(SESSION, "fake", uri)
        # Subscribing a held URI again is not a new subscription.
        assert "result" in await service.subscribe(SESSION, "fake", uris[0])
        with pytest.raises(SubscribeRefused, match="already holds 20 resource subscriptions"):
            await service.subscribe(SESSION, "fake", "crux://reviews/CR-one-more")


@pytest.mark.asyncio
async def test_closing_the_session_removes_its_holder_and_record(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """§4.7 through the process-wide handle the slot close and the trash call."""
    sink = RecordingSink()
    service = _service(paths, sink)
    async with _daemon(monkeypatch, paths), AsyncExitStack() as stack:
        rs.set_active_service(service)
        try:
            await service.start()
            kiro = await _kiro_cli_stub(stack, paths)
            assert "result" in await service.subscribe(SESSION, "fake", URI)
            await kiro.close()

            assert await rs.retire_session("dashboard:somebody-else") == 0
            assert service.holder(SESSION, "fake") is not None
            assert await rs.retire_session(SESSION) == 1
            assert service.holder(SESSION, "fake") is None
            assert ResourceSubscriptionStore(paths["store"]).load() == []
            # The holder was the last to hold the grant, so leaving releases it.
            await async_wait_until(
                lambda: f"unsubscribe {URI} {SESSION}" in recorded(paths["subscribes"]),
                off_loop=True,
                describe=lambda: f"server saw: {recorded(paths['subscribes'])!r}",
            )
        finally:
            rs.set_active_service(None)
            await service.close()


def _record(session_key: str, server: str = "fake", uris: tuple[str, ...] = (URI,)) -> HolderRecord:
    return HolderRecord(
        session_key=session_key,
        channel_id=CHANNEL,
        server=server,
        register={"server_name": server, "poolable": True},
        initialize={"method": "initialize", "params": INIT_PARAMS},
        uris=list(uris),
    )


@pytest.mark.asyncio
async def test_retiring_without_a_running_service_edits_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session closed while the broker is down must not come back with it."""
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(SESSION))
    store.put(_record("slack:C1:1700000000.000100"))
    store.put(_record("dashboard:kept"))
    monkeypatch.setattr(rs, "default_store", lambda: store)
    rs.set_active_service(None)

    assert await rs.retire_session(SESSION) == 1
    from kiro_crew.history_files import transcript_stems

    trashed = transcript_stems("slack:C1:1700000000.000100")
    assert await rs.retire_sessions_with_stems(trashed) == 1
    assert [r.session_key for r in store.load()] == ["dashboard:kept"]


@pytest.mark.asyncio
async def test_the_switch_off_refuses_and_resumes_nothing(tmp_path: Path) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subs.json")
    store.put(_record(SESSION))
    settings = McpResourceSubscriptionsConfig()
    assert settings.enabled is False and settings.max_pinned_backends == 16
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=RecordingSink(), settings=lambda: settings
    )
    await service.start()
    assert service.holder(SESSION, "fake") is None
    with pytest.raises(SubscribeRefused, match="resource subscriptions are off"):
        await service.subscribe(SESSION, "fake", URI)
    assert [r.session_key for r in service.records()] == [SESSION]


@pytest.mark.asyncio
async def test_an_unreadable_store_refuses_every_subscribe(tmp_path: Path) -> None:
    path = tmp_path / "subs.json"
    path.write_text("{not json", encoding="utf-8")
    store = ResourceSubscriptionStore(path)
    with pytest.raises(StoreUnreadable):
        store.load()
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=RecordingSink(), settings=lambda: settings
    )
    await service.start()
    with pytest.raises(SubscribeRefused, match="store is unreadable"):
        await service.subscribe(SESSION, "fake", URI)
    assert path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.asyncio
async def test_the_event_stream_evicts_the_oldest_when_full() -> None:
    stream = EventStream(capacity=2)
    events = [ResourceEvent(EVENT_UPDATED, SESSION, "fake", f"u{n}") for n in range(3)]
    for event in events:
        stream(event)
    assert stream.dropped == 1
    assert [await stream.next(), await stream.next()] == events[1:]
    waiting = asyncio.ensure_future(stream.next())
    assert not waiting.done()
    stream(events[0])
    assert await asyncio.wait_for(waiting, _HANG_SECS) == events[0]


def test_the_config_section_defaults_and_coercion() -> None:
    from kiro_crew.config.section_builders import _build_mcp_gateway_config

    assert _build_mcp_gateway_config({}).resource_subscriptions == McpResourceSubscriptionsConfig(
        enabled=False, max_pinned_backends=16
    )
    built = _build_mcp_gateway_config(
        {"resource_subscriptions": {"enabled": True, "max_pinned_backends": -3}}
    ).resource_subscriptions
    assert built == McpResourceSubscriptionsConfig(enabled=True, max_pinned_backends=0)
    not_true = _build_mcp_gateway_config({"resource_subscriptions": {"enabled": "yes"}})
    assert not_true.resource_subscriptions.enabled is False
    assert (
        _build_mcp_gateway_config({"resource_subscriptions": "on"}).resource_subscriptions
        == McpResourceSubscriptionsConfig()
    )


class _GapListener:
    def __init__(self) -> None:
        self.gaps: list[list[str]] = []

    def on_update(self, uri: str) -> None:
        del uri

    def on_gap(self, uris: list[str]) -> None:
        self.gaps.append(uris)

    async def on_paused(self, reason: str, uris: list[str]) -> bool:
        del reason, uris
        return True

    async def on_refused(self, uri: str, reason: str) -> bool:
        del uri, reason
        return True


class _ScriptedConnection:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.timeouts: list[float] = []
        self.closed = False
        self.writer = SimpleNamespace(close=lambda: setattr(self, "closed", True))
        self.pending: dict[str, Any] = {}

    def fail_pending(self, exc: BaseException) -> None:
        del exc

    async def request(
        self, method: str, params: Any = None, *, timeout: float = 30.0
    ) -> dict[str, Any]:
        del params
        self.sent.append(method)
        self.timeouts.append(timeout)
        return self.replies.pop(0)


def _error(message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "x", "error": {"code": -32000, "message": message}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", ["backend gone: exited", "backend restarted mid-call, retry: x"]
)
async def test_a_lost_backend_ping_answer_ends_the_session_after_one_request(
    message: str,
) -> None:
    listener = _GapListener()
    spec = HolderSpec(session_key=SESSION, channel_id="", server="fake", register={}, initialize={})
    probe_timeout = HANDSHAKE_TIMEOUT_SECS + 1
    holder = ResourceHolder(
        spec, lambda: None, listener, uris=(URI,), probe_timeout_secs=probe_timeout
    )
    conn = _ScriptedConnection(_error(message), _error(message))
    holder._live.set()
    with pytest.raises(ConnectionError, match="daemon lost-backend answer"):
        await holder._probe_backend(conn)  # type: ignore[arg-type]
    assert conn.sent == ["ping"]
    assert conn.timeouts == [probe_timeout]
    assert conn.closed and not holder.is_live
    with pytest.raises(HolderNotLive):
        await holder.subscribe(URI)
    assert listener.gaps == []


class _MemoryHolder:
    def __init__(self, uris: tuple[str, ...]) -> None:
        self._uris = list(uris)
        self.closed = False
        self.unsubscribed: list[str] = []

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    async def subscribe(self, uri: str) -> dict[str, Any]:
        if uri not in self._uris:
            self._uris.append(uri)
        return {"jsonrpc": "2.0", "id": "x", "result": {}}

    async def unsubscribe(self, uri: str) -> None:
        if uri in self._uris:
            self._uris.remove(uri)
        self.unsubscribed.append(uri)

    async def close(self) -> None:
        self.closed = True


def _record_with_pool_key(
    session_key: str, *, work_dir: str = "/work/shared", uris: tuple[str, ...] = (URI,)
) -> HolderRecord:
    return HolderRecord(
        session_key=session_key,
        channel_id=CHANNEL,
        server="fake",
        register={
            "server_name": "fake",
            "poolable": True,
            "command_args_hash": "args",
            "effective_env_hash": "env",
            "work_dir": work_dir,
            "binary_version": "v1",
            "os_uid": 1000,
        },
        initialize={"method": "initialize", "params": INIT_PARAMS},
        uris=list(uris),
    )


def _unit_service(
    store: ResourceSubscriptionStore, *, cap: int = 16
) -> ResourceSubscriptionService:
    settings = McpResourceSubscriptionsConfig(enabled=True, max_pinned_backends=cap)
    return ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=RecordingSink(), settings=lambda: settings
    )


@pytest.mark.asyncio
async def test_failed_writes_leave_each_published_transition_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_put(_record: HolderRecord) -> None:
        raise OSError("disk full")

    def fail_remove(_session_key: str, _server: str = "") -> int:
        raise OSError("disk full")

    subscribe_store = ResourceSubscriptionStore(tmp_path / "subscribe.json")
    subscribe_service = _unit_service(subscribe_store)
    subscribe_record = _record_with_pool_key(SESSION)
    subscribe_holder = _MemoryHolder((URI,))
    subscribe_service._records[subscribe_record.key] = subscribe_record
    subscribe_service._holders[subscribe_record.key] = subscribe_holder  # type: ignore[assignment]
    monkeypatch.setattr(subscribe_store, "put", fail_put)
    with pytest.raises(SubscribeRefused, match="could not persist"):
        await subscribe_service.subscribe(SESSION, "fake", "crux://reviews/CR-2")
    assert subscribe_service.records() == [subscribe_record]
    assert subscribe_holder.uris == [URI]
    assert subscribe_holder.unsubscribed == ["crux://reviews/CR-2"]

    unsubscribe_store = ResourceSubscriptionStore(tmp_path / "unsubscribe.json")
    unsubscribe_service = _unit_service(unsubscribe_store)
    unsubscribe_record = _record_with_pool_key(SESSION, uris=(URI, "crux://reviews/CR-2"))
    unsubscribe_holder = _MemoryHolder(tuple(unsubscribe_record.uris))
    unsubscribe_service._records[unsubscribe_record.key] = unsubscribe_record
    unsubscribe_service._holders[unsubscribe_record.key] = unsubscribe_holder  # type: ignore[assignment]
    monkeypatch.setattr(unsubscribe_store, "put", fail_put)
    assert await unsubscribe_service.unsubscribe(SESSION, "fake", URI) is False
    assert unsubscribe_service.records() == [unsubscribe_record]
    assert unsubscribe_holder.uris == unsubscribe_record.uris
    assert unsubscribe_holder.unsubscribed == []

    removal_store = ResourceSubscriptionStore(tmp_path / "remove.json")
    removal_service = _unit_service(removal_store)
    removal_record = _record_with_pool_key(SESSION)
    removal_holder = _MemoryHolder((URI,))
    removal_service._records[removal_record.key] = removal_record
    removal_service._holders[removal_record.key] = removal_holder  # type: ignore[assignment]
    monkeypatch.setattr(removal_store, "remove", fail_remove)
    with pytest.raises(OSError, match="disk full"):
        await removal_service.remove_session(SESSION)
    # The session is closed whatever the disk says: its holder and records go,
    # and the failed removal is remembered rather than retried by the caller.
    assert removal_service.records() == []
    assert removal_service.holder(SESSION, "fake") is None
    assert removal_holder.closed is True
    assert SESSION in rs._UNRETIRED
    rs._UNRETIRED.discard(SESSION)

    pause_store = ResourceSubscriptionStore(tmp_path / "pause-failure.json")
    pause_sink = RecordingSink()
    settings = McpResourceSubscriptionsConfig(enabled=True)
    pause_service = ResourceSubscriptionService(
        store=pause_store,
        socket_path=lambda: None,
        sink=pause_sink,
        settings=lambda: settings,
    )
    pause_record = _record_with_pool_key(SESSION)
    pause_holder = _MemoryHolder((URI,))
    pause_service._records[pause_record.key] = pause_record
    pause_service._holders[pause_record.key] = pause_holder  # type: ignore[assignment]
    monkeypatch.setattr(pause_store, "put", fail_put)
    assert await pause_service._holder_paused(pause_record.key, "backend gone", [URI]) is False
    assert await pause_service._uri_refused(pause_record.key, URI, "server refused") is False
    assert pause_service.records() == [pause_record]
    assert pause_service.holder(SESSION, "fake") is pause_holder
    assert pause_sink.events == []


@pytest.mark.asyncio
async def test_a_holder_pause_is_persisted_before_memory_and_event_publication(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _GatedStore(ResourceSubscriptionStore):
        def put(self, record: HolderRecord) -> None:
            entered.set()
            assert release.wait(5), "test did not release the persistence gate"
            super().put(record)

    store = _GatedStore(tmp_path / "pause.json")
    sink = RecordingSink()
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=sink, settings=lambda: settings
    )
    record = _record_with_pool_key(SESSION)
    holder = _MemoryHolder((URI,))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]

    transition = asyncio.create_task(
        service._holder_paused(record.key, "launch approval changed", [URI])
    )
    try:
        await async_wait_until(entered.is_set, describe=lambda: "pause write did not start")
        assert service.records() == [record]
        assert service.holder(SESSION, "fake") is holder
        assert sink.events == []
    finally:
        release.set()
    await asyncio.wait_for(transition, _HANG_SECS)

    (paused,) = service.records()
    assert paused.paused_reason == "launch approval changed"
    assert service.holder(SESSION, "fake") is None
    assert sink.of(EVENT_PAUSED, URI) == [
        ResourceEvent(EVENT_PAUSED, SESSION, "fake", URI, "launch approval changed")
    ]


@pytest.mark.asyncio
async def test_retirement_never_checks_the_store_path_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    class _PathThatMustNotBeChecked:
        def exists(self) -> bool:
            raise AssertionError("Path.exists ran on the event loop")

    class _Store:
        path = _PathThatMustNotBeChecked()

        def load(self) -> list[HolderRecord]:
            calls.append(("load", threading.get_ident()))
            return [_record("slack:C1:1700000000.000100")]

        def remove(self, session_key: str, server: str = "") -> int:
            del session_key, server
            calls.append(("remove", threading.get_ident()))
            return 1

    store = _Store()
    monkeypatch.setattr(rs, "default_store", lambda: store)
    rs.set_active_service(None)
    assert await rs.retire_session(SESSION) == 1
    from kiro_crew.history_files import transcript_stems

    assert await rs.retire_sessions_with_stems(transcript_stems("slack:C1:1700000000.000100")) == 1
    assert [name for name, _thread in calls] == ["remove", "load", "remove"]
    assert all(thread_id != loop_thread for _name, thread_id in calls)


@pytest.mark.asyncio
async def test_start_restores_only_distinct_pins_admitted_by_the_current_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "restore-cap.json")
    first = _record_with_pool_key("dashboard:first")
    shared = _record_with_pool_key("dashboard:shared")
    excess = _record_with_pool_key("dashboard:excess", work_dir="/work/other")
    for record in (first, shared, excess):
        store.put(record)
    service = _unit_service(store, cap=1)
    started: list[str] = []

    def start_holder(record: HolderRecord, **_kwargs: Any) -> _MemoryHolder:
        started.append(record.session_key)
        return _MemoryHolder(tuple(record.uris))

    monkeypatch.setattr(service, "_start_holder", start_holder)
    await service.start()

    assert started == ["dashboard:first", "dashboard:shared"]
    records = {record.session_key: record for record in service.records()}
    assert records["dashboard:first"].paused_reason == ""
    assert records["dashboard:shared"].paused_reason == ""
    assert "max_pinned_backends allows (1)" in records["dashboard:excess"].paused_reason
    stored = {record.session_key: record for record in store.load()}
    assert stored["dashboard:excess"].paused_reason == records["dashboard:excess"].paused_reason


@pytest.mark.asyncio
async def test_a_timed_out_ping_ends_the_session_instead_of_silencing_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Writer:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    listener = _GapListener()
    spec = HolderSpec(session_key=SESSION, channel_id="", server="fake", register={}, initialize={})
    holder = ResourceHolder(spec, lambda: None, listener, uris=(URI,), ping_interval_secs=0)
    writer = _Writer()
    pump_cancelled = asyncio.Event()

    async def block_pump(_conn: Any) -> None:
        try:
            await finished(asyncio.Event().wait(), "cancelled holder pump")
        finally:
            pump_cancelled.set()

    async def timeout_ping(_conn: Any) -> None:
        raise asyncio.TimeoutError("cold backend spawn")

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.resource_holder.transport.connect",
        AsyncMock(return_value=(MagicMock(), writer)),
    )
    monkeypatch.setattr(holder, "_handshake", AsyncMock())
    monkeypatch.setattr(holder, "_initialize", AsyncMock())
    monkeypatch.setattr(holder, "_resubscribe", AsyncMock())
    monkeypatch.setattr(holder, "_pump", block_pump)
    monkeypatch.setattr(holder, "_ping_loop", timeout_ping)

    session = asyncio.create_task(holder._session(Path("unused")))
    # Wait on what the assertions read: the writer is closed before the session
    # finishes its teardown, so its flag says nothing about the task.
    await async_wait_until(
        session.done,
        timeout=5,
        describe=lambda: "timed-out ping kept session open",
    )
    assert writer.closed
    with pytest.raises(asyncio.TimeoutError, match="cold backend spawn"):
        await await_until(session, what="timed-out holder session did not finish")
    await asyncio.wait_for(pump_cancelled.wait(), _HANG_SECS)


def test_describe_session_never_stores_a_token_or_stub_uuid_it_was_given() -> None:
    """Defence in depth: the identity path pops the token first today, and a
    describe-session reply must stay token-free if that ever changes."""
    from kiro_crew.mcp_gateway.daemon import session_describe

    class _Conn:
        register: Any = None
        private_backend = False

    conn = _Conn()
    session_describe.track_live_conn(
        conn,  # type: ignore[arg-type]
        {"server_name": "fake", "stub_uuid": "u-1", "stub_session_token": "secret"},
        private_backend=True,
    )
    try:
        assert conn.register == {"server_name": "fake"}
        assert conn.private_backend is True
    finally:
        session_describe.forget_live_conn(conn)  # type: ignore[arg-type]


# -- R2 repairs -----------------------------------------------------------------

URI_2 = "crux://reviews/CR-2"
URI_3 = "crux://reviews/CR-3"


class _GatedSubscribeHolder(_MemoryHolder):
    """A holder whose subscribe waits until the test lets it answer."""

    def __init__(self, uris: tuple[str, ...]) -> None:
        super().__init__(uris)
        self.subscribing = asyncio.Event()
        self.answer = asyncio.Event()

    async def subscribe(self, uri: str) -> dict[str, Any]:
        self.subscribing.set()
        await await_until(self.answer, what="gated subscribe was not released")
        return await super().subscribe(uri)


@pytest.mark.asyncio
async def test_a_refusal_landing_while_subscribe_waits_is_kept(tmp_path: Path) -> None:
    store = ResourceSubscriptionStore(tmp_path / "subscribe-race.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION, uris=(URI, URI_2))
    store.put(record)
    holder = _GatedSubscribeHolder((URI, URI_2))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]

    subscribe = asyncio.create_task(service.subscribe(SESSION, "fake", URI_3))
    try:
        await asyncio.wait_for(holder.subscribing.wait(), _HANG_SECS)
        # The holder's re-subscribe of a carried URI is refused meanwhile.
        assert await asyncio.wait_for(service._uri_refused(record.key, URI_2, "gone"), _HANG_SECS)
        holder._uris.remove(URI_2)
    finally:
        holder.answer.set()
    await await_until(subscribe, what="gated subscribe did not finish")

    for kept in (service.records()[0], store.load()[0]):
        assert kept.paused_uris == {URI_2: "gone"}
        assert kept.uris == [URI, URI_3]


@pytest.mark.asyncio
async def test_a_refusal_landing_while_unsubscribe_writes_is_kept(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _GatedStore(ResourceSubscriptionStore):
        def put(self, record: HolderRecord) -> None:
            entered.set()
            assert release.wait(5), "test did not release the persistence gate"
            super().put(record)

    store = _GatedStore(tmp_path / "unsubscribe-race.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION, uris=(URI, URI_2, URI_3))
    service._records[record.key] = record
    service._holders[record.key] = _MemoryHolder(tuple(record.uris))  # type: ignore[assignment]

    unsubscribe = asyncio.create_task(service.unsubscribe(SESSION, "fake", URI_3))
    refusal_started = asyncio.Event()

    async def refuse() -> bool:
        refusal_started.set()
        return await service._uri_refused(record.key, URI_2, "gone")

    try:
        await async_wait_until(entered.is_set, describe=lambda: "unsubscribe write did not start")
        refusal = asyncio.create_task(refuse())
        await asyncio.wait_for(refusal_started.wait(), _HANG_SECS)
    finally:
        release.set()
    assert await asyncio.wait_for(unsubscribe, _HANG_SECS) is True
    assert await asyncio.wait_for(refusal, _HANG_SECS) is True

    for kept in (service.records()[0], store.load()[0]):
        assert kept.uris == [URI]
        assert kept.paused_uris == {URI_2: "gone"}


@pytest.mark.asyncio
async def test_unsubscribing_the_last_live_uri_drops_the_holder(tmp_path: Path) -> None:
    store = ResourceSubscriptionStore(tmp_path / "last-live.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION)
    record.paused_uris = {URI_2: "refused"}
    holder = _MemoryHolder((URI,))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]

    assert await service.unsubscribe(SESSION, "fake", URI) is True

    assert holder.closed is True
    assert service.holder(SESSION, "fake") is None
    assert service.pinned_backends() == set()
    (kept,) = service.records()
    assert kept.uris == [] and kept.paused_uris == {URI_2: "refused"}


class _ReasonListener(_GapListener):
    def __init__(self) -> None:
        super().__init__()
        self.paused: list[str] = []
        self.refused: list[tuple[str, str]] = []

    async def on_paused(self, reason: str, uris: list[str]) -> bool:
        del uris
        self.paused.append(reason)
        return True

    async def on_refused(self, uri: str, reason: str) -> bool:
        self.refused.append((uri, reason))
        return True


def _bare_holder(listener: Any, **kwargs: Any) -> ResourceHolder:
    spec = HolderSpec(session_key=SESSION, channel_id="", server="fake", register={}, initialize={})
    return ResourceHolder(spec, lambda: Path("unused"), listener, **kwargs)


@pytest.mark.asyncio
async def test_a_server_authored_reason_is_cut_to_the_bound_and_marked() -> None:
    from kiro_crew.mcp_gateway.resource_holder import REASON_CUT_MARKER, REASON_MAX_CHARS

    listener = _ReasonListener()
    holder = _bare_holder(listener, uris=(URI,))
    assert await holder._pause("x" * (REASON_MAX_CHARS * 4)) is True
    await holder._resubscribe(_ScriptedConnection(_error("y" * (REASON_MAX_CHARS * 4))))  # type: ignore[arg-type]

    (paused,) = listener.paused
    ((refused_uri, refused),) = listener.refused
    assert refused_uri == URI
    for reason in (paused, refused, holder.paused_reason):
        assert len(reason) == REASON_MAX_CHARS
        assert reason.endswith(REASON_CUT_MARKER)
    assert holder.uris == []


@pytest.mark.asyncio
async def test_describe_session_reads_a_frame_larger_than_the_default_limit(
    short_sock_dir: Path,
) -> None:
    from kiro_crew.mcp_gateway.transport import DEFAULT_READ_LIMIT

    big = "r" * (DEFAULT_READ_LIMIT * 2)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b'{"type": "session-described", "reason": "' + big.encode() + b'"}\n')
        await writer.drain()
        writer.close()

    sock = short_sock_dir / "big.sock"
    server = await asyncio.start_unix_server(answer, path=str(sock))
    try:
        reply = await asyncio.wait_for(describe_session(sock, SESSION, "fake"), _HANG_SECS)
    finally:
        server.close()
        await server.wait_closed()
    assert reply["reason"] == big


@pytest.mark.asyncio
async def test_a_holder_session_connects_with_the_daemons_frame_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES

    limits: list[int] = []

    async def connect(_path: Any, *, limit: int = 0) -> Any:
        limits.append(limit)
        raise ConnectionRefusedError("no daemon")

    monkeypatch.setattr("kiro_crew.mcp_gateway.resource_holder.transport.connect", connect)
    holder = _bare_holder(_GapListener())
    with pytest.raises(ConnectionRefusedError):
        await asyncio.wait_for(holder._session(Path("unused")), _HANG_SECS)
    assert limits == [READ_BUFFER_LIMIT_BYTES]


@pytest.mark.asyncio
async def test_a_malformed_server_message_does_not_end_the_holder() -> None:
    listener = _ReasonListener()
    holder = _bare_holder(listener, uris=(URI,))

    class _Conn:
        pending: dict[str, Any] = {}

    for params in (["not", "a", "dict"], "text", 7):
        await holder._dispatch(
            _Conn(),  # type: ignore[arg-type]
            {"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": params},
        )
    for result in (["not", "a", "dict"], {"capabilities": "x"}, {"capabilities": {"resources": 1}}):
        with pytest.raises(HolderPaused, match="does not declare resources.subscribe"):
            await holder._initialize(
                _ScriptedConnection({"jsonrpc": "2.0", "id": "x", "result": result})  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_an_unexpected_session_error_pauses_the_holder_with_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _ReasonListener()
    holder = _bare_holder(listener, uris=(URI,), reconnect_delay_secs=0)

    async def explode(_path: Path) -> None:
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(holder, "_session", explode)
    holder.start()
    try:
        with pytest.raises(HolderPaused, match="AttributeError"):
            await holder.wait_ready(5)
    finally:
        await holder.close()
    (reason,) = listener.paused
    assert "AttributeError" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("awaiting_subscribe", [False, True])
async def test_a_holder_whose_every_uri_was_refused_does_not_stay_live(
    monkeypatch: pytest.MonkeyPatch, awaiting_subscribe: bool
) -> None:
    class _Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    holder = _bare_holder(
        _GapListener(), uris=(URI,), awaiting_subscribe=awaiting_subscribe, ping_interval_secs=60
    )

    async def refuse_all(_conn: Any) -> None:
        holder._uris.clear()

    async def block(_conn: Any) -> None:
        await finished(asyncio.Event().wait(), "cancelled holder pump")

    monkeypatch.setattr(
        "kiro_crew.mcp_gateway.resource_holder.transport.connect",
        AsyncMock(return_value=(MagicMock(), _Writer())),
    )
    monkeypatch.setattr(holder, "_handshake", AsyncMock())
    monkeypatch.setattr(holder, "_initialize", AsyncMock())
    monkeypatch.setattr(holder, "_resubscribe", refuse_all)
    monkeypatch.setattr(holder, "_pump", block)

    session = asyncio.create_task(holder._session(Path("unused")))
    try:
        if awaiting_subscribe:
            # Started for a subscribe that is about to land: it goes live.
            await async_wait_until(lambda: holder.is_live, describe=lambda: "never went live")
        else:
            with pytest.raises(HolderPaused, match="refused every subscription"):
                await asyncio.wait_for(session, _HANG_SECS)
    finally:
        session.cancel()
        await await_until(
            asyncio.gather(session, return_exceptions=True),
            what="cancelled holder session did not finish",
        )


# -- R3 repairs -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_unretired_sessions() -> Any:
    """``_UNRETIRED`` and ``_LATE_RETIRES`` are process-wide; no test sees another's."""
    rs._UNRETIRED.clear()
    rs._LATE_RETIRES.clear()
    yield
    rs._UNRETIRED.clear()
    rs._LATE_RETIRES.clear()


class _StartedHolder(_MemoryHolder):
    """The holder ``subscribe`` starts: live with *uris*, its new URI answer chosen by *failure*."""

    service: ResourceSubscriptionService

    def __init__(self, uris: tuple[str, ...], failure: str = "") -> None:
        super().__init__(uris)
        self.failure = failure

    async def wait_ready(self, timeout: float) -> None:
        del timeout

    async def subscribe(self, uri: str) -> dict[str, Any]:
        self.paused_during_start = self.service.paused(SESSION, "fake", URI)
        if self.failure == "error":
            return _error("no such resource")
        if self.failure == "not_live":
            raise HolderNotLive("the holder has no live gateway connection")
        return await super().subscribe(uri)


def _starting_service(
    store: ResourceSubscriptionStore, holder: _StartedHolder, monkeypatch: pytest.MonkeyPatch
) -> ResourceSubscriptionService:
    """A service whose ``subscribe`` describes a fresh record and starts *holder*."""
    service = _unit_service(store)
    holder.service = service

    async def describe(session_key: str, server: str) -> HolderRecord:
        del server
        return _record_with_pool_key(session_key, uris=())

    def start_holder(record: HolderRecord, **_kwargs: Any) -> _MemoryHolder:
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start_holder)
    return service


def _paused_record() -> HolderRecord:
    record = _record_with_pool_key(SESSION, uris=(URI,))
    record.paused_reason = "backend gone"
    record.paused_uris = {URI_2: "refused once"}
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["accepted", "error", "not_live"])
async def test_a_started_holder_keeps_its_carried_uris_on_disk_when_the_new_uri_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "carried.json")
    paused = _paused_record()
    store.put(paused)
    holder = _StartedHolder((URI, URI_2), failure)
    service = _starting_service(store, holder, monkeypatch)
    service._records[paused.key] = paused

    if failure == "accepted":
        assert "result" in await service.subscribe(SESSION, "fake", URI_3)
    elif failure == "error":
        assert "error" in await service.subscribe(SESSION, "fake", URI_3)
    else:
        with pytest.raises(SubscribeRefused, match="not live"):
            await service.subscribe(SESSION, "fake", URI_3)

    assert holder.paused_during_start is True
    assert service.holder(SESSION, "fake") is holder and holder.closed is False
    for kept in (service.records()[0], store.load()[0]):
        assert kept.paused_reason == ""
        assert kept.paused_uris == {}
        assert kept.uris == [URI, URI_2] + ([URI_3] if failure == "accepted" else [])


@pytest.mark.asyncio
async def test_a_started_holder_goes_when_its_carried_uris_cannot_be_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "carried-unwritable.json")
    paused = _paused_record()
    store.put(paused)
    holder = _StartedHolder((URI, URI_2), "error")
    service = _starting_service(store, holder, monkeypatch)
    service._records[paused.key] = paused

    def fail_put(_record: HolderRecord) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "put", fail_put)
    assert "error" in await service.subscribe(SESSION, "fake", URI_3)

    assert holder.closed is True
    assert service.holder(SESSION, "fake") is None
    assert service.records() == [paused]


class _GatedPutStore(ResourceSubscriptionStore):
    """A store whose ``put`` waits in its worker thread until the test releases it."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def put(self, record: HolderRecord) -> None:
        self.entered.set()
        assert self.release.wait(5), "test did not release the persistence gate"
        super().put(record)


@pytest.mark.asyncio
async def test_a_pause_landing_during_an_unsubscribe_keeps_the_uri_gone(tmp_path: Path) -> None:
    store = _GatedPutStore(tmp_path / "pause-unsubscribe.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION, uris=(URI, URI_2))
    holder = _MemoryHolder(tuple(record.uris))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]

    unsubscribe = asyncio.create_task(service.unsubscribe(SESSION, "fake", URI))
    try:
        await async_wait_until(
            store.entered.is_set, describe=lambda: "unsubscribe write did not start"
        )
        # The holder read its URIs before the unsubscribe landed.
        pause = asyncio.create_task(
            service._holder_paused(record.key, "backend gone", [URI, URI_2], holder=holder)
        )
    finally:
        store.release.set()
    assert await asyncio.wait_for(unsubscribe, _HANG_SECS) is True
    assert await asyncio.wait_for(pause, _HANG_SECS) is True

    for kept in (service.records()[0], store.load()[0]):
        assert kept.uris == [URI_2]
        assert kept.paused_reason == "backend gone"
    sink = service.sink
    assert isinstance(sink, RecordingSink)
    assert sink.of(EVENT_PAUSED) == [
        ResourceEvent(EVENT_PAUSED, SESSION, "fake", URI_2, "backend gone")
    ]


@pytest.mark.asyncio
async def test_a_pause_never_forgets_a_holder_that_replaced_the_reporter(
    tmp_path: Path,
) -> None:
    store = _GatedPutStore(tmp_path / "pause-replaced.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION)
    reporter, successor = _MemoryHolder((URI,)), _MemoryHolder((URI,))
    service._records[record.key] = record
    service._holders[record.key] = reporter  # type: ignore[assignment]

    pause = asyncio.create_task(
        service._holder_paused(record.key, "backend gone", [URI], holder=reporter)
    )
    try:
        await async_wait_until(store.entered.is_set, describe=lambda: "pause write did not start")
        service._holders[record.key] = successor  # type: ignore[assignment]
    finally:
        store.release.set()
    await asyncio.wait_for(pause, _HANG_SECS)

    assert service.holder(SESSION, "fake") is successor


@pytest.mark.asyncio
async def test_a_refusal_of_a_uri_already_unsubscribed_changes_nothing(tmp_path: Path) -> None:
    store = ResourceSubscriptionStore(tmp_path / "refusal-gone.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION)
    store.put(record)
    holder = _MemoryHolder((URI,))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]

    assert await service._uri_refused(record.key, URI_2, "refused", holder=holder) is True

    assert service.records() == [record]
    assert store.load() == [record]
    sink = service.sink
    assert isinstance(sink, RecordingSink) and sink.events == []


@pytest.mark.asyncio
async def test_a_refusal_for_a_uri_unsubscribed_meanwhile_does_not_end_the_resubscribe() -> None:
    class _UnsubscribingListener(_ReasonListener):
        holder: Any = None

        async def on_refused(self, uri: str, reason: str) -> bool:
            # The unsubscribe lands while the refusal is being recorded.
            await self.holder.unsubscribe(uri)
            return await super().on_refused(uri, reason)

    listener = _UnsubscribingListener()
    holder = _bare_holder(listener, uris=(URI, URI_2))
    listener.holder = holder
    ok = {"jsonrpc": "2.0", "id": "x", "result": {}}

    await holder._resubscribe(_ScriptedConnection(_error("gone"), ok))  # type: ignore[arg-type]

    assert holder.uris == [URI_2]


@pytest.mark.asyncio
async def test_a_failed_removal_drops_the_holder_and_is_never_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "unremovable.json")
    record = _record_with_pool_key(SESSION)
    store.put(record)
    failing = [True]
    real_remove = store.remove

    def remove(session_key: str, server: str = "") -> int:
        if failing[0]:
            raise OSError("disk full")
        return real_remove(session_key, server)

    monkeypatch.setattr(store, "remove", remove)
    service = _unit_service(store)
    holder = _MemoryHolder((URI,))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]
    rs.set_active_service(service)
    try:
        assert await rs.retire_session(SESSION) == 0
    finally:
        rs.set_active_service(None)

    # Closed is closed: nothing keeps the backend pinned or the grant in use.
    assert holder.closed is True
    assert service.holder(SESSION, "fake") is None
    assert service.records() == [] and service.pinned_backends() == set()
    assert store.load() == [record]

    started: list[tuple[str, str]] = []

    def start_holder(started_record: HolderRecord, **_kwargs: Any) -> _MemoryHolder:
        started.append(started_record.key)
        return _MemoryHolder(tuple(started_record.uris))

    # A broker restart while the disk still refuses writes restores nothing of it.
    restarted = _unit_service(store)
    monkeypatch.setattr(restarted, "_start_holder", start_holder)
    await restarted.start()
    assert restarted.records() == [] and started == []

    # Once the disk takes writes again, the next start removes it for good.
    failing[0] = False
    recovered = _unit_service(store)
    monkeypatch.setattr(recovered, "_start_holder", start_holder)
    await recovered.start()
    assert recovered.records() == [] and started == []
    assert store.load() == []
    assert SESSION not in rs._UNRETIRED


@pytest.mark.asyncio
async def test_a_session_subscribing_again_settles_its_failed_removal_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "resubscribe.json")
    stale = _record_with_pool_key(SESSION)
    stale.server = "other"
    store.put(stale)
    rs._UNRETIRED.add(SESSION)
    service = _starting_service(store, _StartedHolder(()), monkeypatch)

    await service.subscribe(SESSION, "fake", URI_2)

    assert [(r.server, r.uris) for r in store.load()] == [("fake", [URI_2])]
    assert SESSION not in rs._UNRETIRED


@pytest.mark.asyncio
async def test_a_uri_longer_than_the_bound_is_refused_before_anything_keeps_it(
    tmp_path: Path,
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "long-uri.json")
    service = _unit_service(store)
    record = _record_with_pool_key(SESSION)
    holder = _MemoryHolder((URI,))
    service._records[record.key] = record
    service._holders[record.key] = holder  # type: ignore[assignment]
    at_bound = "crux://" + "x" * (rs.RESOURCE_URI_MAX_LEN - len("crux://"))

    with pytest.raises(SubscribeRefused, match=f"at most {rs.RESOURCE_URI_MAX_LEN} characters"):
        await service.subscribe(SESSION, "fake", at_bound + "x")
    assert holder.uris == [URI] and service.records() == [record]
    assert not store.path.exists()

    await service.subscribe(SESSION, "fake", at_bound)
    assert service.records()[0].uris == [URI, at_bound]


def test_turning_the_switch_on_needs_a_restart() -> None:
    """The broker decides at boot whether the service runs at all."""
    from kiro_crew.config.schema import requires_restart

    assert requires_restart("mcp_gateway.resource_subscriptions.enabled")
    assert not requires_restart("mcp_gateway.resource_subscriptions.max_pinned_backends")


@pytest.mark.asyncio
async def test_retiring_with_no_store_file_takes_no_lock_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    store = ResourceSubscriptionStore(data / rs.STORE_LEAF)
    monkeypatch.setattr(rs, "default_store", lambda: store)
    rs.set_active_service(None)

    assert await rs.retire_session(SESSION) == 0
    assert await rs.retire_sessions_with_stems({"dashboard_held-session"}) == 0
    assert store.load() == []
    assert not data.exists()


# -- R4 repairs -----------------------------------------------------------------


class _ObservedLock(asyncio.Lock):
    """An ``asyncio.Lock`` that counts how many times it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    async def acquire(self) -> bool:
        self.asked += 1
        return await super().acquire()


class _GatedStore(ResourceSubscriptionStore):
    """``load`` and the first ``remove`` wait in their worker thread for the test."""

    def __init__(self, path: Path, *, remove_fails: bool = False) -> None:
        super().__init__(path)
        self.remove_fails = remove_fails
        self.load_entered = threading.Event()
        self.load_release = threading.Event()
        self.remove_entered = threading.Event()
        self.remove_release = threading.Event()

    def load(self) -> list[HolderRecord]:
        self.load_entered.set()
        assert self.load_release.wait(5), "test did not release the load"
        return super().load()

    def remove(self, session_key: str, server: str = "") -> int:
        if not self.remove_entered.is_set():
            self.remove_entered.set()
            assert self.remove_release.wait(5), "test did not release the remove"
        if self.remove_fails:
            raise OSError("disk full")
        return super().remove(session_key, server)


def _restoring_service(
    store: ResourceSubscriptionStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[ResourceSubscriptionService, list[_MemoryHolder], _ObservedLock]:
    service = _unit_service(store)
    lock = _ObservedLock()
    service._lock = lock
    started: list[_MemoryHolder] = []

    def start_holder(record: HolderRecord, **_kwargs: Any) -> _MemoryHolder:
        holder = _MemoryHolder(tuple(record.uris))
        service._holders[record.key] = holder  # type: ignore[assignment]
        started.append(holder)
        return holder

    monkeypatch.setattr(service, "_start_holder", start_holder)
    return service, started, lock


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_fails", [True, False])
async def test_a_retire_arriving_while_start_loads_is_never_undone_by_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove_fails: bool
) -> None:
    """The retire queues behind the whole start, so it removes what the start restored."""
    store = _GatedStore(tmp_path / "start-race.json", remove_fails=remove_fails)
    ResourceSubscriptionStore(store.path).put(_record_with_pool_key(SESSION))
    service, started, lock = _restoring_service(store, monkeypatch)

    start = asyncio.create_task(service.start())
    await async_wait_until(store.load_entered.is_set, describe=lambda: "start never loaded")
    retire = asyncio.create_task(service.remove_session(SESSION))
    # Unfixed, the retire takes the lock at once and reaches the store; fixed, it
    # queues behind the start. Either way, go on only once it has done one of them.
    await async_wait_until(
        lambda: store.remove_entered.is_set() or lock.asked >= 2,
        describe=lambda: "the retire neither reached the store nor queued",
    )
    store.load_release.set()
    # The start has loaded and filtered by now, or has finished.
    await async_wait_until(
        lambda: start.done() or lock.asked >= 2,
        describe=lambda: "the start neither finished nor queued",
    )
    store.remove_release.set()
    await asyncio.wait_for(start, _HANG_SECS)
    results = await asyncio.wait_for(asyncio.gather(retire, return_exceptions=True), _HANG_SECS)

    assert service.records() == []
    assert service.holder(SESSION, "fake") is None
    assert all(holder.closed for holder in started)
    if remove_fails:
        assert isinstance(results[0], OSError)
        assert SESSION in rs._UNRETIRED
    else:
        assert results == [1]
        assert ResourceSubscriptionStore(store.path).load() == []


@pytest.mark.asyncio
async def test_a_subscribe_during_starts_retry_keeps_its_new_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start's retry of a failed removal must not wipe a subscribe that settled it."""
    store = _GatedStore(tmp_path / "retry-race.json")
    stale = _record_with_pool_key(SESSION)
    stale.server = "other"
    ResourceSubscriptionStore(store.path).put(stale)
    rs._UNRETIRED.add(SESSION)
    store.load_release.set()
    service = _starting_service(store, _StartedHolder(()), monkeypatch)
    lock = _ObservedLock()
    service._lock = lock

    start = asyncio.create_task(service.start())
    await async_wait_until(store.remove_entered.is_set, describe=lambda: "start never retried")
    subscribe = asyncio.create_task(service.subscribe(SESSION, "fake", URI_2))
    await async_wait_until(
        lambda: subscribe.done() or lock.asked >= 2,
        describe=lambda: "the subscribe neither finished nor queued",
    )
    store.remove_release.set()
    await asyncio.wait_for(start, _HANG_SECS)
    await asyncio.wait_for(subscribe, _HANG_SECS)

    assert [(r.server, r.uris) for r in ResourceSubscriptionStore(store.path).load()] == [
        ("fake", [URI_2])
    ]
    assert SESSION not in rs._UNRETIRED


@pytest.mark.asyncio
async def test_a_trash_while_start_loads_prunes_what_the_start_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.history_files import transcript_stems

    store = _GatedStore(tmp_path / "trash-race.json")
    ResourceSubscriptionStore(store.path).put(_record_with_pool_key(SESSION))
    ResourceSubscriptionStore(store.path).put(_record_with_pool_key("dashboard:kept"))
    store.remove_entered.set()  # no gate on the removal itself
    service, started, lock = _restoring_service(store, monkeypatch)
    rs.set_active_service(service)
    try:
        start = asyncio.create_task(service.start())
        await async_wait_until(store.load_entered.is_set, describe=lambda: "start never loaded")
        trash = asyncio.create_task(rs.retire_sessions_with_stems(transcript_stems(SESSION)))
        await async_wait_until(
            lambda: trash.done() or lock.asked >= 2,
            describe=lambda: "the trash neither finished nor queued",
        )
        store.load_release.set()
        await asyncio.wait_for(start, _HANG_SECS)
        assert await asyncio.wait_for(trash, _HANG_SECS) == 1
    finally:
        rs.set_active_service(None)

    assert [r.session_key for r in service.records()] == ["dashboard:kept"]
    assert [r.session_key for r in ResourceSubscriptionStore(store.path).load()] == [
        "dashboard:kept"
    ]
    assert service.holder(SESSION, "fake") is None
    assert [holder.closed for holder in started].count(True) == 1


@pytest.mark.asyncio
async def test_a_trash_against_an_unreadable_store_says_it_could_not_check(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    path = tmp_path / "unreadable.json"
    path.write_text("{not json", encoding="utf-8")
    service = _unit_service(ResourceSubscriptionStore(path))
    await service.start()
    caplog.set_level(logging.ERROR)
    rs.set_active_service(service)
    try:
        assert await rs.retire_sessions_with_stems({"dashboard_held-session"}) == 0
    finally:
        rs.set_active_service(None)

    assert any(
        r.levelno == logging.ERROR and "trashed sessions could not be checked" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_pid_claim_never_retargets_a_holder_attributed_inline(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """RFC §4.10: the holder's own Register keeps it out of every pid-indexed claim.

    The holder frame from :func:`holder_register_frame` carries the caller inline,
    no ``ancestor_pids`` and no ``stub_session_token``. A claim on this process's
    pid for ANOTHER session -- tokenless, then tokened -- must leave the holder's
    calls recorded under the holder's own session. A tokenless stub under the
    same pid is the positive control: the tokenless claim does retarget it.
    """
    other = "dashboard:another-session"
    async with _daemon(monkeypatch, paths), AsyncExitStack() as stack:
        spec = HolderSpec(
            session_key=SESSION,
            channel_id=CHANNEL,
            server="fake",
            register=register_payload_like_the_stub(paths["sock"], paths["work"]),
            initialize={},
        )
        holder = await RawConnection.register(paths["sock"], holder_register_frame(spec))
        stack.push_async_callback(holder.close)
        bystander = await stack.enter_async_context(
            running_stub(paths["sock"], paths["work"], paths["home"])
        )
        assert "result" in await holder.request(1, "initialize", INIT_PARAMS)

        async def caller_of(conn: Any, req_id: int) -> str:
            assert "result" in await conn.request(req_id, "tools/call", {"name": "noop"})
            return recorded(paths["callers"])[-1]

        assert await caller_of(holder, 2) == SESSION

        await send_claim(str(paths["sock"]), os.getpid(), other, CHANNEL)
        assert await caller_of(bystander, 3) == other
        assert await caller_of(holder, 4) == SESSION

        await send_claim(str(paths["sock"]), os.getpid(), other, CHANNEL, mint_stub_session_token())
        assert await caller_of(holder, 5) == SESSION


# -- R4a repairs ----------------------------------------------------------------


def test_the_store_lives_inside_one_hidden_write_protected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import sandbox_leaves, sandbox_precreate
    from kiro_crew.security import paths_lists

    monkeypatch.setattr("kiro_crew.config.paths.data_home", lambda: tmp_path)
    store = rs.default_store()

    assert store.path == tmp_path / STORE_DIR_LEAF / STORE_LEAF
    assert STORE_DIR_LEAF in paths_lists._CREW_SECRET_LEAVES
    assert STORE_DIR_LEAF in sandbox_leaves._CREW_HIDDEN_LEAVES
    assert STORE_DIR_LEAF in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    # A secret leaf is the read+write keystone: agents can neither read nor write under it.
    for prefix in paths_lists._CREW_HOME_PREFIXES:
        assert f"{prefix}/{STORE_DIR_LEAF}" in paths_lists._SENSITIVE_HOME_DIRS


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_an_update_in_the_subscribe_responses_socket_write_is_delivered(
    short_sock_dir: Path, refused: bool
) -> None:
    updates: list[str] = []
    release_server = asyncio.Event()

    class _Listener(_GapListener):
        def on_update(self, uri: str) -> None:
            updates.append(uri)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        if refused:
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32000, "message": "refused"},
            }
        else:
            response = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
        update = {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": URI},
        }
        writer.write((json.dumps(response) + "\n" + json.dumps(update) + "\n").encode())
        await writer.drain()
        await await_until(release_server, what="test server was not released")
        writer.close()
        await writer.wait_closed()

    socket_path = short_sock_dir / "immediate-update.sock"
    server = await asyncio.start_unix_server(answer, path=str(socket_path))
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    conn = _Connection(reader, writer)
    holder = _bare_holder(_Listener())
    holder._conn = conn
    holder._live.set()
    pump = asyncio.create_task(holder._pump(conn))
    try:
        reply = await asyncio.wait_for(holder.subscribe(URI), _HANG_SECS)
        release_server.set()
        await asyncio.wait_for(pump, _HANG_SECS)
        if refused:
            assert "error" in reply
            assert updates == []
            assert holder.uris == []
        else:
            assert "result" in reply
            assert updates == [URI]
            assert holder.uris == [URI]
    finally:
        release_server.set()
        writer.close()
        await writer.wait_closed()
        await await_until(
            asyncio.gather(pump, return_exceptions=True),
            what="cancelled holder pump did not finish",
        )
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_pending_update_is_discarded_when_the_connection_is_lost() -> None:
    updates: list[str] = []

    class _Listener(_GapListener):
        def on_update(self, uri: str) -> None:
            updates.append(uri)

    holder = _bare_holder(_Listener())

    class _LostConnection:
        pending: dict[str, Any] = {}

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "resources/subscribe" and params == {"uri": URI}
            await holder._dispatch(
                self,  # type: ignore[arg-type]
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/resources/updated",
                    "params": {"uri": URI},
                },
            )
            raise ConnectionError("socket closed")

    holder._conn = _LostConnection()  # type: ignore[assignment]
    holder._live.set()
    with pytest.raises(ConnectionError, match="socket closed"):
        await holder.subscribe(URI)

    assert updates == []
    assert holder.uris == []
    assert holder._pending_uris == set()
    assert holder._held_updates == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_fails", [False, True])
async def test_start_never_restores_a_closed_dashboard_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    remove_fails: bool,
) -> None:
    import logging

    store = ResourceSubscriptionStore(tmp_path / "closed.json")
    stored = _record_with_pool_key(SESSION)
    store.put(stored)
    real_remove = store.remove
    if remove_fails:

        def refuse_remove(session_key: str, server: str = "") -> int:
            del session_key, server
            raise OSError("disk full")

        monkeypatch.setattr(store, "remove", refuse_remove)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: None,
        sink=RecordingSink(),
        settings=lambda: McpResourceSubscriptionsConfig(enabled=True),
        session_is_closed=lambda session_key: session_key == SESSION,
    )
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service,
        "_start_holder",
        lambda record, **_kwargs: started.append(record.key),
    )
    caplog.set_level(logging.ERROR)

    await service.start()

    assert service.records() == []
    assert started == []
    if remove_fails:
        assert any(
            record.levelno == logging.ERROR
            and "could not remove closed session" in record.getMessage()
            for record in caplog.records
        )
        assert ResourceSubscriptionStore(store.path).load() == [stored]
    else:
        assert real_remove(SESSION) == 0
        assert ResourceSubscriptionStore(store.path).load() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["register", "initialize"])
async def test_describe_session_refuses_an_oversized_retained_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    reply = {
        "type": "session-described",
        "channel_id": CHANNEL,
        "private_backend": False,
        "register": {"server_name": "fake"},
        "initialize": {"method": "initialize"},
    }
    reply[field_name] = {"payload": "x" * RETAINED_CONTAINER_MAX_BYTES}
    service = _unit_service(ResourceSubscriptionStore(tmp_path / "describe.json"))
    service._socket_path = lambda: Path("unused")
    monkeypatch.setattr(rs, "describe_session", AsyncMock(return_value=reply))

    with pytest.raises(SubscribeRefused, match="retained-container limit") as refused:
        await service._describe(SESSION, "fake")

    assert service.records() == [] and refused.value.code == rs.REFUSED_SERVER_NOT_ATTACHED
    assert not service._store.path.exists()


@pytest.mark.parametrize("field_name", ["register", "initialize"])
def test_store_drops_an_oversized_retained_container_with_a_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    field_name: str,
) -> None:
    import logging

    store = ResourceSubscriptionStore(tmp_path / "oversized.json")
    valid = _record_with_pool_key("dashboard:valid")
    oversized = _record_with_pool_key("dashboard:oversized").to_json()
    oversized[field_name] = {"payload": "x" * RETAINED_CONTAINER_MAX_BYTES}
    store.path.write_text(
        json.dumps({"version": 1, "holders": [valid.to_json(), oversized]}),
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING)

    assert store.load() == [valid]
    assert any(
        field_name in record.getMessage() and "exceeds" in record.getMessage()
        for record in caplog.records
    )


def test_store_and_service_share_the_backend_uri_bound(tmp_path: Path) -> None:
    from kiro_crew.mcp_gateway.backend_leases import RESOURCE_URI_MAX_LEN

    assert rs.RESOURCE_URI_MAX_LEN == RESOURCE_URI_MAX_LEN
    store = ResourceSubscriptionStore(tmp_path / "overlong-uri.json")
    overlong = _record_with_pool_key(
        SESSION,
        uris=("resource://" + "x" * RESOURCE_URI_MAX_LEN,),
    )
    store.path.write_text(
        json.dumps({"version": 1, "holders": [overlong.to_json()]}),
        encoding="utf-8",
    )
    assert store.load() == []


@pytest.mark.asyncio
async def test_the_session_cap_counts_live_and_paused_uris_together(tmp_path: Path) -> None:
    store = ResourceSubscriptionStore(tmp_path / "combined-cap.json")
    service = _unit_service(store)
    record = _record_with_pool_key(
        SESSION,
        uris=tuple(f"resource://live/{index}" for index in range(10)),
    )
    record.paused_uris = {f"resource://paused/{index}": "refused" for index in range(10)}
    service._records[record.key] = record
    service._holders[record.key] = _MemoryHolder(tuple(record.uris))  # type: ignore[assignment]

    assert service._session_total(SESSION, besides=("fake", "resource://new")) == 20
    with pytest.raises(SubscribeRefused, match="already holds 20 resource subscriptions"):
        await service.subscribe(SESSION, "fake", "resource://new")
