"""Resource-subscription holder and service failure paths for RFC Phase 2."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.config.integration_sections import McpResourceSubscriptionsConfig
from kiro_crew.mcp_gateway import resource_subscriptions as rs
from kiro_crew.mcp_gateway.daemon import session_describe
from kiro_crew.mcp_gateway.resource_holder import (
    RESOURCE_HOLDER_FIELD,
    HolderPaused,
    HolderSpec,
    ResourceHolder,
    _Connection,
)
from kiro_crew.mcp_gateway.resource_subscription_store import (
    RETAINED_CONTAINER_MAX_BYTES,
    HolderRecord,
    ResourceSubscriptionStore,
)
from kiro_crew.mcp_gateway.resource_subscriptions import (
    EVENT_MAY_HAVE_CHANGED,
    EVENT_UPDATED,
    RecordingSink,
    ResourceSubscriptionService,
    SubscribeRefused,
)
from kiro_crew.testing.wait import async_wait_until, await_until, finished

_HANG_SECS = 30.0  # only turns a hang into a named failure; the signal decides

SESSION = "dashboard:failure-paths"
URI = "crux://reviews/CR-1"
URI_2 = "crux://reviews/CR-2"
_ACCESS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_SIGNATURE = "a" * 64
_CREDENTIAL_URI = (
    f"https://example.com/resource?X-Amz-Credential={_ACCESS_KEY}%2Fscope"
    f"&X-Amz-Signature={_SIGNATURE}"
)
_USERINFO_URI = f"https://user:{_ACCESS_KEY}@example.com/resource"
_UNTRUSTED_TEXT = f"server refused {_CREDENTIAL_URI} {_USERINFO_URI}\nforged log line"


@pytest.fixture(autouse=True)
def _no_process_wide_state() -> Any:
    """``_UNRETIRED``, ``_LATE_RETIRES`` and the live-connection table are process-wide."""
    rs._UNRETIRED.clear()
    rs._LATE_RETIRES.clear()
    session_describe._LIVE_CONNS.clear()
    yield
    rs._UNRETIRED.clear()
    rs._LATE_RETIRES.clear()
    session_describe._LIVE_CONNS.clear()


class _Conn:
    """The ``_StubConn`` attributes ``session_describe`` reads and writes."""

    def __init__(self) -> None:
        self.caller = SimpleNamespace(session_key=SESSION, channel_id="C1")
        self.register: Optional[dict[str, Any]] = None
        self.captured_init: Optional[dict[str, Any]] = None
        self.private_backend = False
        self.describe_refusal = ""
        self.oversized_server = ""


def _oversized() -> dict[str, Any]:
    return {"payload": "x" * RETAINED_CONTAINER_MAX_BYTES}


def _describe() -> dict[str, Any]:
    return session_describe.describe_session_reply(
        {"type": "describe-session", "session_key": SESSION, "server": "fake"}
    )


@pytest.mark.parametrize("field_name", ["register", "initialize"])
def test_the_daemon_never_copies_or_keeps_an_oversized_container(
    monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    copied: list[Any] = []
    real_deepcopy = session_describe.copy.deepcopy

    def recording_deepcopy(value: Any, *args: Any) -> Any:
        copied.append(value)
        return real_deepcopy(value, *args)

    monkeypatch.setattr(session_describe.copy, "deepcopy", recording_deepcopy)
    conn = _Conn()
    register: dict[str, Any] = {"server_name": "fake", "stub_uuid": "u-1"}
    initialize: dict[str, Any] = {"method": "initialize", "params": {}}
    if field_name == "register":
        register.update(_oversized())
    else:
        initialize["params"] = _oversized()

    session_describe.track_live_conn(conn, register, private_backend=False)  # type: ignore[arg-type]
    session_describe.track_initialize(conn, initialize)  # type: ignore[arg-type]
    reply = _describe()

    oversized = register if field_name == "register" else initialize
    assert all(value is not oversized for value in copied)
    if field_name == "register":
        assert conn.register is None
    else:
        assert conn.captured_init is None
    assert reply["type"] == "session-undescribed"
    assert "retained-container limit" in reply["reason"]


def test_a_container_within_the_bound_is_still_described() -> None:
    conn = _Conn()
    register = {"server_name": "fake", "stub_uuid": "u-1"}
    session_describe.track_live_conn(conn, register, private_backend=False)  # type: ignore[arg-type]
    session_describe.track_initialize(conn, {"method": "initialize"})  # type: ignore[arg-type]

    reply = _describe()

    assert reply["type"] == "session-described"
    assert reply["register"] == {"server_name": "fake"}
    assert reply["initialize"] == {"method": "initialize"}


class _Listener:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.refused: list[str] = []

    def on_update(self, uri: str) -> None:
        self.updates.append(uri)

    def on_gap(self, uris: list[str]) -> None:
        del uris

    async def on_paused(self, reason: str, uris: list[str]) -> bool:
        del reason, uris
        return True

    async def on_refused(self, uri: str, reason: str) -> bool:
        del reason
        self.refused.append(uri)
        return True


def _holder(listener: _Listener, uris: tuple[str, ...] = ()) -> ResourceHolder:
    spec = HolderSpec(session_key=SESSION, channel_id="", server="fake", register={}, initialize={})
    return ResourceHolder(spec, lambda: Path("unused"), listener, uris=uris)


async def _never_answers(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # Reads every request and answers none, until the holder hangs up.
    while await reader.readline():
        pass
    writer.close()
    await writer.wait_closed()


async def _answers_once(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    reply: dict[str, Any],
) -> None:
    request = json.loads((await reader.readline()).decode("utf-8"))
    answer = {**reply, "jsonrpc": "2.0", "id": request["id"]}
    writer.write(json.dumps(answer).encode("utf-8") + b"\n")
    await writer.drain()
    await reader.read()
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
@pytest.mark.parametrize("method", ["subscribe", "unsubscribe"])
async def test_a_timed_out_request_ends_the_holders_connection(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    socket_path = short_sock_dir / f"timeout-{method}.sock"
    server = await asyncio.start_unix_server(_never_answers, path=str(socket_path))
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    conn = _Connection(reader, writer)

    async def time_out(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise asyncio.TimeoutError()

    monkeypatch.setattr(conn, "request", time_out)
    holder = _holder(_Listener(), uris=(URI,) if method == "unsubscribe" else ())
    holder._conn = conn
    holder._live.set()
    pump = asyncio.create_task(holder._pump(conn))
    try:
        if method == "subscribe":
            with pytest.raises(asyncio.TimeoutError):
                await holder.subscribe(URI)
        else:
            await holder.unsubscribe(URI)
        # The read loop sees end of file, which is what ends the session.
        await asyncio.wait_for(pump, _HANG_SECS)
        assert holder.uris == []
    finally:
        writer.close()
        await await_until(
            asyncio.gather(pump, return_exceptions=True),
            what="cancelled holder pump did not finish",
        )
        server.close()
        await server.wait_closed()


class _Replies:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.writer = SimpleNamespace(close=lambda: None)

    async def request(self, method: str, params: Any = None, **_kwargs: Any) -> dict[str, Any]:
        del method, params
        return self.replies.pop(0)

    def fail_pending(self, _exc: BaseException) -> None:
        pass


def _error(message: str, code: int = -32000) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "x", "error": {"code": code, "message": message}}


def _update(uri: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "notifications/resources/updated",
        "params": {"uri": uri},
    }


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
@pytest.mark.parametrize(
    ("operation", "reply", "expected_reason"),
    [
        ("subscribe", {}, "subscribe reply had no result"),
        ("resubscribe", {}, "the subscribe without a verdict"),
        ("unsubscribe", {}, "unsubscribe reply had no result"),
        ("unsubscribe", _error(_UNTRUSTED_TEXT), "unsubscribe was refused"),
        ("unsubscribe", _error("backend restarted mid-call, retry: x"), "lost-backend answer"),
    ],
    ids=[
        "subscribe_missing_result",
        "resubscribe_missing_result",
        "unsubscribe_missing_result",
        "unsubscribe_refused",
        "unsubscribe_restarted",
    ],
)
async def test_only_an_explicit_result_is_a_successful_subscription_verdict(
    short_sock_dir: Path,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    reply: dict[str, Any],
    expected_reason: str,
) -> None:
    socket_path = short_sock_dir / f"verdict-{operation}-{len(reply)}.sock"

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _answers_once(reader, writer, reply)

    server = await asyncio.start_unix_server(answer, path=str(socket_path))
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    conn = _Connection(reader, writer)
    holder = _holder(_Listener(), uris=(URI,) if operation != "subscribe" else ())
    holder._conn = conn
    holder._live.set()
    pump = asyncio.create_task(holder._pump(conn))
    caplog.set_level(logging.INFO, logger="kiro_crew.mcp_gateway.resource_holder")
    try:
        if operation == "subscribe":
            with pytest.raises(ConnectionError, match="did not contain a result"):
                await holder.subscribe(URI)
        elif operation == "resubscribe":
            # The server's own answer, so the URI pauses; it was the only one.
            with pytest.raises(HolderPaused, match="refused every subscription"):
                await holder._resubscribe(conn)
        else:
            await holder.unsubscribe(URI)

        if operation != "resubscribe":
            await asyncio.wait_for(pump, _HANG_SECS)
            messages = [record.getMessage() for record in caplog.records]
            assert any(expected_reason in message for message in messages)
            assert all(_ACCESS_KEY not in message for message in messages)
            assert all(_SIGNATURE not in message for message in messages)
            assert all("user:" + _ACCESS_KEY not in message for message in messages)
            assert all("\n" not in message for message in messages)
        assert holder.uris == []
    finally:
        writer.close()
        await await_until(
            asyncio.gather(pump, return_exceptions=True),
            what="cancelled holder pump did not finish",
        )
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_untrusted_holder_and_service_log_fields_are_sanitized(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    listener = _Listener()
    holder = ResourceHolder(
        HolderSpec(
            session_key=_CREDENTIAL_URI,
            channel_id="",
            server=_USERINFO_URI,
            register={},
            initialize={},
        ),
        lambda: Path("unused"),
        listener,
    )
    writer = SimpleNamespace(close=lambda: None)
    conn = _Connection(asyncio.StreamReader(), writer)  # type: ignore[arg-type]
    caplog.set_level(logging.INFO)

    holder._end_session(conn, _UNTRUSTED_TEXT)
    assert await holder._pause(_UNTRUSTED_TEXT)
    assert holder.paused_reason == _UNTRUSTED_TEXT

    failed: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    failed.set_exception(OSError(_UNTRUSTED_TEXT))
    rs._log_late_retire_failure(failed, _CREDENTIAL_URI)

    # A file name can hold a newline only where the filesystem allows one; the
    # credential in the name is checked everywhere.
    separator = "_" if pc.IS_WINDOWS else "\n"
    store_path = tmp_path / f"store-{_ACCESS_KEY}{separator}forged.json"
    store_path.write_text(json.dumps({"version": 1, "holders": [None]}))
    assert ResourceSubscriptionStore(store_path).load() == []

    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert all(_ACCESS_KEY not in message for message in messages)
    assert all(_SIGNATURE not in message for message in messages)
    assert all("user:" + _ACCESS_KEY not in message for message in messages)
    assert all("\n" not in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "message", "code", "lost_backend"),
    [
        (operation, message, -32000, True)
        for operation in ("initialize", "resubscribe", "subscribe")
        for message in (
            "backend gone: exited",
            "backend restarted mid-call, retry: x",
            "the MCP server was replaced and its tool set changed (drift)",
        )
    ]
    + [
        ("initialize", "upstream said: backend gone: maintenance", -32000, False),
        ("resubscribe", "backend gone: this server's own words", -32001, False),
        ("subscribe", "no such resource", -32000, False),
    ],
)
async def test_holder_requests_distinguish_backend_loss_from_server_refusals(
    operation: str, message: str, code: int, lost_backend: bool
) -> None:
    listener = _Listener()
    holder = _holder(listener, uris=(URI,) if operation == "resubscribe" else ())
    conn = _Replies(_error(message, code))
    holder._conn = conn  # type: ignore[assignment]
    holder._live.set()
    request = (
        holder.subscribe(URI)
        if operation == "subscribe"
        else getattr(holder, f"_{operation}")(conn)
    )
    if lost_backend:
        with pytest.raises(ConnectionError, match="daemon lost-backend answer"):
            await request
    elif operation == "initialize":
        with pytest.raises(HolderPaused, match="refused initialize"):
            await request
    else:
        assert await request == (None if operation == "resubscribe" else _error(message, code))
    assert holder.is_live is not lost_backend
    assert listener.refused == ([] if lost_backend or operation != "resubscribe" else [URI])
    assert holder.uris == ([URI] if lost_backend and operation == "resubscribe" else [])


def _record(uris: tuple[str, ...] = (URI,)) -> HolderRecord:
    return HolderRecord(
        session_key=SESSION,
        channel_id="C1",
        server="fake",
        register={"server_name": "fake"},
        initialize={"method": "initialize"},
        uris=list(uris),
    )


class _AcceptingHolder:
    """A live holder whose subscribe the server accepts, with a scripted side effect."""

    def __init__(self, uris: tuple[str, ...]) -> None:
        self._uris = list(uris)
        self.paused_reason = ""
        self.paused_code = ""
        self.unsubscribed: list[str] = []
        self.on_accept: Any = None

    @property
    def uris(self) -> list[str]:
        return list(self._uris)

    async def subscribe(self, uri: str) -> dict[str, Any]:
        self._uris.append(uri)
        if self.on_accept is not None:
            await self.on_accept(uri)
        return {"jsonrpc": "2.0", "id": "x", "result": {}}

    async def unsubscribe(self, uri: str) -> None:
        if uri in self._uris:
            self._uris.remove(uri)
        self.unsubscribed.append(uri)

    async def close(self) -> None:
        pass


def _service_with_holder(
    store: ResourceSubscriptionStore, sink: Any
) -> tuple[ResourceSubscriptionService, _AcceptingHolder, Any]:
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=sink, settings=lambda: settings
    )
    record = _record()
    store.put(record)
    service._records[record.key] = record
    holder = _AcceptingHolder((URI,))
    service._holders[record.key] = holder  # type: ignore[assignment]
    listener = rs._Listener(service, record.key)
    listener.holder = holder  # type: ignore[assignment]
    return service, holder, listener


@pytest.mark.asyncio
async def test_a_holder_that_paused_after_the_server_accepted_refuses_the_subscribe(
    tmp_path: Path,
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "paused.json")
    service, holder, listener = _service_with_holder(store, RecordingSink())

    async def pause(_uri: str) -> None:
        assert await listener.on_paused("backend refused", [URI])

    holder.on_accept = pause

    with pytest.raises(SubscribeRefused, match="paused: backend refused") as refused:
        await service.subscribe(SESSION, "fake", URI_2)
    assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE

    (stored,) = store.load()
    assert stored.paused_reason == "backend refused"
    assert stored.uris == [URI]
    assert service.records() == [stored]


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
@pytest.mark.parametrize(
    ("arrivals", "held_kind"),
    [
        (("update",), EVENT_UPDATED),
        (("gap",), EVENT_MAY_HAVE_CHANGED),
        (("gap", "update"), EVENT_MAY_HAVE_CHANGED),
    ],
    ids=["update", "gap", "gap_then_update"],
)
async def test_an_event_for_a_subscribe_is_published_only_once_it_is_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrivals: tuple[str, ...],
    held_kind: str,
    save_fails: bool,
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "held-event.json")
    published: list[tuple[str, str, bool]] = []

    def sink(event: rs.ResourceEvent) -> None:
        # Persist before publish: the event's subscription is on disk already.
        saved = any(event.uri in record.uris for record in store.load())
        published.append((event.kind, event.uri, saved))

    service, holder, listener = _service_with_holder(store, sink)

    async def events_with_the_reply(uri: str) -> None:
        for arrival in arrivals:
            if arrival == "update":
                listener.on_update(uri)
            else:
                # A reconnect reports a gap for every URI the holder tracks.
                listener.on_gap([URI, uri])

    holder.on_accept = events_with_the_reply
    # The gap for the URI saved before this subscribe is not held.
    saved_uri_gap = [(EVENT_MAY_HAVE_CHANGED, URI, True)] if "gap" in arrivals else []
    if save_fails:

        def refuse_put(_record: HolderRecord) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "put", refuse_put)
        with pytest.raises(SubscribeRefused, match="could not persist"):
            await service.subscribe(SESSION, "fake", URI_2)
        assert published == saved_uri_gap
        assert holder.unsubscribed == [URI_2]
    else:
        await service.subscribe(SESSION, "fake", URI_2)
        assert published == saved_uri_gap + [(held_kind, URI_2, True)]
    assert service._unsaved == {}


@pytest.mark.asyncio
async def test_an_update_for_an_already_saved_uri_is_published_at_once(tmp_path: Path) -> None:
    store = ResourceSubscriptionStore(tmp_path / "saved-update.json")
    sink = RecordingSink()
    service, _holder_, listener = _service_with_holder(store, sink)

    listener.on_update(URI)

    assert [event.uri for event in sink.of(EVENT_UPDATED)] == [URI]
    assert service._unsaved == {}


@pytest.mark.asyncio
async def test_unsubscribe_with_a_stale_expected_id_keeps_the_replacement(
    tmp_path: Path,
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "stale-id.json")
    sink = RecordingSink()
    service, holder, _listener = _service_with_holder(store, sink)
    replacement_id = "rsub_replacement"
    record = service.records()[0]
    record.subscriptions[URI] = rs.SubscriptionMeta(id=replacement_id, instruction="replacement")
    store.put(record)

    removed = await service.unsubscribe(
        SESSION,
        "fake",
        URI,
        expected_subscription_id="rsub_original",
    )

    assert removed is False
    assert service.subscription(SESSION, "fake", URI) == rs.SubscriptionMeta(
        id=replacement_id, instruction="replacement"
    )
    assert store.load() == service.records()
    assert holder.unsubscribed == []
    assert sink.of(rs.EVENT_REMOVED) == []


class _BlockingStore(ResourceSubscriptionStore):
    """A store whose next selected mutation stays in its worker thread."""

    def __init__(self, path: Path, method: str) -> None:
        super().__init__(path)
        self.method = method
        self.armed = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def _block(self, method: str) -> None:
        if self.armed and self.method == method:
            self.entered.set()
            assert self.release.wait(5), "the test did not release the store write"

    def put(self, record: HolderRecord) -> None:
        self._block("put")
        super().put(record)

    def remove(self, session_key: str, server: Optional[str] = None) -> int:
        self._block("remove")
        return super().remove(session_key, server)


async def _wait_for_thread_event(event: threading.Event) -> None:
    await async_wait_until(event.is_set, describe=lambda: "the store write did not start")


@pytest.mark.asyncio
async def test_cancelling_a_subscribe_store_write_finishes_memory_and_disk_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _BlockingStore(tmp_path / "cancelled-subscribe.json", "put")
    service, holder, _listener = _service_with_holder(store, RecordingSink())
    store.armed = True
    cancellation_seen = asyncio.Event()
    real_shield = asyncio.shield

    async def observe_cancellation(awaitable: Any) -> Any:
        try:
            return await real_shield(awaitable)
        except asyncio.CancelledError:
            cancellation_seen.set()
            raise

    monkeypatch.setattr(asyncio, "shield", observe_cancellation)
    subscribing = asyncio.create_task(service.subscribe(SESSION, "fake", URI_2))
    try:
        await _wait_for_thread_event(store.entered)
        assert not subscribing.done(), "subscribe finished before cancellation could be observed"
        subscribing.cancel()
        await await_until(cancellation_seen, what="subscribe cancellation was not observed")
        subscribing.cancel()
        assert not subscribing.done()
    finally:
        store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await await_until(subscribing, what="cancelled subscribe did not finish")
    assert service.records() == store.load()
    assert service.subscription(SESSION, "fake", URI_2) is not None
    assert await service.unsubscribe(SESSION, "fake", URI_2) is True
    assert holder.unsubscribed == [URI_2]
    assert service.records() == store.load()


@pytest.mark.asyncio
async def test_cancelling_an_unsubscribe_store_write_finishes_memory_and_disk_first(
    tmp_path: Path,
) -> None:
    store = _BlockingStore(tmp_path / "cancelled-unsubscribe.json", "put")
    service, holder, _listener = _service_with_holder(store, RecordingSink())
    record = service.records()[0]
    record.uris.append(URI_2)
    record.subscriptions[URI_2] = rs.SubscriptionMeta(id="rsub_second")
    store.put(record)
    holder._uris.append(URI_2)
    store.armed = True

    unsubscribing = asyncio.create_task(service.unsubscribe(SESSION, "fake", URI_2))
    try:
        await _wait_for_thread_event(store.entered)
        unsubscribing.cancel()
    finally:
        store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await await_until(unsubscribing, what="cancelled unsubscribe did not finish")
    assert service.records() == store.load()
    assert service.subscription(SESSION, "fake", URI_2) is None
    assert holder.unsubscribed == [URI_2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "extra"),
    [("put", (URI_2,)), ("remove", ())],
    ids=["a-uri-is-left", "the-last-uri"],
)
async def test_a_removal_the_store_cannot_take_refuses_and_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    extra: tuple[str, ...],
) -> None:
    """A full or read-only store keeps the subscription whole, in memory and on disk."""
    store = ResourceSubscriptionStore(tmp_path / "removal-refused.json")
    sink = RecordingSink()
    service, holder, _listener = _service_with_holder(store, sink)
    record = service.records()[0]
    record.subscriptions[URI] = rs.SubscriptionMeta(id="rsub_first")
    for uri in extra:
        record.uris.append(uri)
        record.subscriptions[uri] = rs.SubscriptionMeta(id="rsub_second")
        holder._uris.append(uri)
    store.put(record)
    whole = store.load()

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(store, failing, refuse)
    caplog.set_level(logging.ERROR)

    assert await service.unsubscribe(SESSION, "fake", URI) is False

    assert service.records() == whole
    assert store.load() == whole
    assert service.subscription(SESSION, "fake", URI) is not None
    assert sink.of(rs.EVENT_REMOVED) == []
    assert holder.unsubscribed == []
    assert holder.uris == [URI, *extra]
    assert service._holders[(SESSION, "fake")] is holder
    assert [r.getMessage() for r in caplog.records if "did not commit" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_late_retire_failure_at_the_cancellation_boundary_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def retire() -> int:
        await await_until(gate, what="late retire gate was not released")
        raise OSError("disk full")

    caller = asyncio.create_task(rs.retire_within_bound(retire(), SESSION, timeout=60))
    await asyncio.sleep(0)
    caplog.set_level(logging.ERROR)
    # One step: the removal fails in the same loop pass that cancels the
    # caller, so the failure is both seen by the caller and queued for the
    # done callback.
    gate.set_result(None)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(caller, what="cancelled retire did not finish")
    for _ in range(3):
        await asyncio.sleep(0)

    failures = [
        record
        for record in caplog.records
        if "removal failed after the wait" in record.getMessage()
    ]
    assert len(failures) == 1
    assert rs._LATE_RETIRES == set()


def test_a_container_exactly_at_the_bound_is_kept() -> None:
    # Measured on compact JSON, so this initialize is exactly the bound.
    overhead = len(json.dumps({"p": ""}, separators=(",", ":")))
    payload = "x" * (RETAINED_CONTAINER_MAX_BYTES - overhead)
    conn = _Conn()
    register = {"server_name": "fake", "p": ""}
    session_describe.track_live_conn(conn, register, private_backend=False)  # type: ignore[arg-type]
    session_describe.track_initialize(conn, {"p": payload})  # type: ignore[arg-type]
    assert conn.captured_init == {"p": payload}


#: What ``os.fsdecode`` makes of a POSIX work directory holding a byte that is not UTF-8.
_UNDECODABLE_PATH = "/home/u/caf\udce9"


def test_a_lone_surrogate_in_a_register_or_initialize_is_measured_not_raised() -> None:
    conn = _Conn()
    register = {"server_name": "fake", "stub_uuid": "u-1", "work_dir": _UNDECODABLE_PATH}
    initialize = {"method": "initialize", "params": {"rootUri": _UNDECODABLE_PATH}}

    session_describe.track_live_conn(conn, register, private_backend=False)  # type: ignore[arg-type]
    session_describe.track_initialize(conn, initialize)  # type: ignore[arg-type]
    reply = _describe()

    assert reply["type"] == "session-described", reply
    assert reply["register"] == {"server_name": "fake", "work_dir": _UNDECODABLE_PATH}
    assert reply["initialize"] == initialize


def test_a_register_whose_retained_form_is_exactly_at_the_bound_is_described() -> None:
    retained_overhead = len(json.dumps({"server_name": "fake", "p": ""}, separators=(",", ":")))
    payload = "x" * (RETAINED_CONTAINER_MAX_BYTES - retained_overhead)
    # The token fields are not kept, so they do not count against the bound.
    register = {
        "server_name": "fake",
        "p": payload,
        "stub_uuid": "u-1",
        "stub_session_token": "t" * 64,
    }
    conn = _Conn()
    session_describe.track_live_conn(conn, register, private_backend=False)  # type: ignore[arg-type]
    session_describe.track_initialize(conn, {"method": "initialize"})  # type: ignore[arg-type]

    reply = _describe()

    assert reply["type"] == "session-described", reply.get("reason")
    assert reply["register"] == {"server_name": "fake", "p": payload}


def test_an_oversized_holder_connection_does_not_block_describing_the_stub() -> None:
    stub = _Conn()
    session_describe.track_live_conn(  # type: ignore[arg-type]
        stub, {"server_name": "fake", "stub_uuid": "u-stub"}, private_backend=False
    )
    session_describe.track_initialize(stub, {"method": "initialize"})  # type: ignore[arg-type]
    holder_conn = _Conn()
    oversized_holder = {"server_name": "fake", "stub_uuid": "u-holder", **_oversized()}
    oversized_holder[RESOURCE_HOLDER_FIELD] = True
    session_describe.track_live_conn(  # type: ignore[arg-type]
        holder_conn, oversized_holder, private_backend=False
    )

    reply = _describe()

    assert holder_conn.register is None
    assert reply["type"] == "session-described", reply.get("reason")
    assert reply["register"] == {"server_name": "fake"}


def _daemon_paths(tmp_path: Path, short_sock_dir: Path, name: str) -> dict[str, Path]:
    work = tmp_path / "ws"
    work.mkdir()
    return {
        "sock": short_sock_dir / f"{name}.sock",
        "work": work,
        "launches": tmp_path / "launches.txt",
        "subscribes": tmp_path / "subscribes.txt",
        "callers": tmp_path / "callers.txt",
    }


def _conn_tracked(stub_uuid: str) -> bool:
    from kiro_crew.mcp_gateway import gatewayd as gw

    in_index = any(
        conn.stub_uuid == stub_uuid for conns in gw._CONN_INDEX.values() for conn in conns
    )
    probed = any(probe.stub_uuid == stub_uuid for probe in gw._STUB_PROBES)
    return in_index or probed


@pytest.mark.asyncio
@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.skipif(pc.IS_WINDOWS, reason="claims resolve the peer's ancestry via SO_PEERCRED")
async def test_a_lone_surrogate_register_and_initialize_are_served_with_the_feature_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_sock_dir: Path
) -> None:
    from mcp_resource_subscription_helpers import (
        INIT_PARAMS,
        RawConnection,
        register_payload_like_the_stub,
        running_daemon,
    )

    p = _daemon_paths(tmp_path, short_sock_dir, "surrogate")
    async with running_daemon(
        monkeypatch, p["sock"], p["work"], p["launches"], p["subscribes"], p["callers"]
    ):
        register = register_payload_like_the_stub(p["sock"], p["work"])
        register["undecodable_work_dir"] = _UNDECODABLE_PATH
        conn = await RawConnection.register(p["sock"], register)
        try:
            params = {**INIT_PARAMS, "clientInfo": {"name": _UNDECODABLE_PATH, "version": "0"}}
            reply = await asyncio.wait_for(conn.request(1, "initialize", params), 30)
            assert "result" in reply, reply
        finally:
            await conn.close()


@pytest.mark.asyncio
@pytest.mark.xdist_group("mcp_gateway")
@pytest.mark.skipif(pc.IS_WINDOWS, reason="claims resolve the peer's ancestry via SO_PEERCRED")
async def test_a_raise_inside_connection_tracking_still_tears_the_connection_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_sock_dir: Path
) -> None:
    from mcp_resource_subscription_helpers import register_payload_like_the_stub, running_daemon

    from kiro_crew.mcp_gateway import transport
    from kiro_crew.testing.wait import async_wait_until

    def tracking_fails(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("tracking failed")

    monkeypatch.setattr(session_describe, "track_live_conn", tracking_fails)
    p = _daemon_paths(tmp_path, short_sock_dir, "trackraise")
    async with running_daemon(
        monkeypatch, p["sock"], p["work"], p["launches"], p["subscribes"], p["callers"]
    ):
        register = register_payload_like_the_stub(p["sock"], p["work"])
        register["stub_uuid"] = stub_uuid = "r6b-tracking-raise"
        assert register.get("ancestor_pids"), "the Register must reach the claim index"
        reader, writer = await transport.connect(p["sock"])
        try:
            writer.write((json.dumps(register) + "\n").encode("utf-8"))
            await writer.drain()
            registered = json.loads(await asyncio.wait_for(reader.readline(), 30))
            assert registered.get("type") == "registered", registered
            # The handler raised; the daemon ends the connection.
            assert await asyncio.wait_for(reader.read(), 30) == b""
            await async_wait_until(
                lambda: not _conn_tracked(stub_uuid),
                timeout=10,
                describe=lambda: "the connection stayed in the claim index or probe table",
            )
        finally:
            writer.close()


class _RecordingListener(_Listener):
    def __init__(self) -> None:
        super().__init__()
        self.paused: list[str] = []

    async def on_paused(self, reason: str, uris: list[str]) -> bool:
        del uris
        self.paused.append(reason)
        return True


class _ScriptedDaemon:
    """A Unix socket that answers each holder connection's handshake as scripted.

    ``ensure_replies[i]`` answers the i-th connection's ``ensure_backend``; the
    last one repeats. Behind a ``ready`` it serves ``initialize`` and answers each
    ``resources/subscribe`` with ``subscribe_reply(uri)``, then writes ``pushed[uri]``.
    ``before_registered`` and ``before_ready`` are written ahead of the
    ``registered`` and ``ensure_backend`` answers, as the daemon's sweeper can.
    """

    _INIT_OK = {"result": {"capabilities": {"resources": {"subscribe": True}}}}

    def __init__(
        self,
        ensure_replies: list[dict[str, Any]],
        subscribe_reply: Any = None,
        *,
        initialize_replies: tuple[dict[str, Any], ...] = (),
        before_registered: tuple[dict[str, Any], ...] = (),
        before_ready: tuple[dict[str, Any], ...] = (),
        pushed: Optional[dict[str, tuple[dict[str, Any], ...]]] = None,
    ) -> None:
        self.ensure_replies = ensure_replies
        self.subscribe_reply = subscribe_reply or (lambda _uri: {"result": {}})
        self.inits = list(initialize_replies)
        self.before_registered = before_registered
        self.before_ready = before_ready
        self.pushed = pushed or {}
        self.connections = 0
        self.subscribes: list[str] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        index = self.connections
        self.connections += 1
        try:
            await reader.readline()
            for frame in self.before_registered:
                await self._send(writer, frame)
            await self._send(writer, {"type": "registered"})
            await reader.readline()
            for frame in self.before_ready:
                await self._send(writer, frame)
            ensure = self.ensure_replies[min(index, len(self.ensure_replies) - 1)]
            await self._send(writer, ensure)
            if ensure.get("type") != "ready":
                return
            while line := await reader.readline():
                msg = json.loads(line)
                if msg.get("id") is None:
                    continue
                if msg.get("method") == "initialize":
                    reply: dict[str, Any] = self.inits.pop(0) if self.inits else self._INIT_OK
                elif msg.get("method") == "resources/subscribe":
                    uri = msg["params"]["uri"]
                    self.subscribes.append(uri)
                    reply = self.subscribe_reply(uri)
                else:
                    reply = {"result": {}}
                await self._send(writer, {**reply, "jsonrpc": "2.0", "id": msg["id"]})
                if msg.get("method") == "resources/subscribe":
                    for frame in self.pushed.get(msg["params"]["uri"], ()):
                        await self._send(writer, frame)
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()


class _SteppedClock:
    """Stands in for the holder's ``asyncio``: records each reconnect wait, takes none."""

    #: The holder's ping interval in these tests; waited for real, it never fires.
    PING = 3600.0

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        if delay >= self.PING:
            await asyncio.sleep(delay)
            return
        self.waits.append(delay)
        await asyncio.sleep(0)


def _running_holder(
    socket_path: Path, listener: _Listener, uris: tuple[str, ...]
) -> ResourceHolder:
    spec = HolderSpec(session_key=SESSION, channel_id="", server="fake", register={}, initialize={})
    holder = ResourceHolder(
        spec,
        lambda: socket_path,
        listener,
        uris=uris,
        resumed=True,
        ping_interval_secs=_SteppedClock.PING,
        reconnect_delay_secs=0.01,
    )
    holder.start()
    return holder


def test_the_holder_reads_the_daemons_capacity_class() -> None:
    from kiro_crew.mcp_gateway import resource_holder
    from kiro_crew.mcp_gateway.daemon import admission_protocol

    assert resource_holder._REJECT_CLASS_CAPACITY == admission_protocol.REJECT_CLASS_CAPACITY


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
@pytest.mark.parametrize("initialize_replies", [(), (_error("backend gone: exited"),)])
async def test_a_capacity_refusal_on_reconnect_waits_and_reconnects_without_pausing(
    short_sock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialize_replies: tuple[dict[str, Any], ...],
) -> None:
    from kiro_crew.mcp_gateway import resource_holder
    from kiro_crew.mcp_gateway.daemon.admission_protocol import _capacity_rejection

    clock = _SteppedClock()
    monkeypatch.setattr(resource_holder, "asyncio", clock)
    capacity = _capacity_rejection(7).frame("the host spawn budget is exhausted")
    daemon = _ScriptedDaemon([capacity, {"type": "ready"}], initialize_replies=initialize_replies)
    socket_path = short_sock_dir / "capacity.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _RecordingListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        await holder.wait_ready(10)
        assert (listener.paused, holder.paused_reason) == ([], "")
        assert daemon.connections == 2 + bool(initialize_replies)
        assert clock.waits and clock.waits[0] >= 7
        assert daemon.subscribes == [URI]
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_wait_that_runs_out_at_capacity_names_the_capacity_refusal(
    short_sock_dir: Path,
) -> None:
    from kiro_crew.mcp_gateway.daemon.admission_protocol import _capacity_rejection

    capacity = _capacity_rejection(60).frame("the host spawn budget is exhausted")
    daemon = _ScriptedDaemon([capacity])
    socket_path = short_sock_dir / "full.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    holder = _running_holder(socket_path, _RecordingListener(), (URI,))
    try:
        await async_wait_until(
            lambda: "at capacity" in holder._last_connect_failure,
            describe=lambda: "the holder did not record the capacity refusal",
        )
        with pytest.raises(HolderPaused, match="at capacity.*spawn budget is exhausted"):
            await holder.wait_ready(0)
        assert daemon.connections == 1
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_compat_refusal_of_the_backend_still_pauses_the_holder(
    short_sock_dir: Path,
) -> None:
    compat = {"type": "rejected", "class": "compat", "reason": "no mapping", "fallback": True}
    daemon = _ScriptedDaemon([compat, {"type": "ready"}])
    socket_path = short_sock_dir / "compat.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _RecordingListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        with pytest.raises(HolderPaused, match="could not start"):
            await holder.wait_ready(10)
        assert len(listener.paused) == 1 and "no mapping" in listener.paused[0]
        assert daemon.connections == 1
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_resubscribe_never_given_a_verdict_pauses_that_uri_after_one_reconnect(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import resource_holder

    monkeypatch.setattr(resource_holder, "asyncio", _SteppedClock())

    def no_verdict_for_uri(uri: str) -> dict[str, Any]:
        return {} if uri == URI else {"result": {}}

    daemon = _ScriptedDaemon([{"type": "ready"}], no_verdict_for_uri)
    socket_path = short_sock_dir / "noverdict.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _RecordingListener()
    holder = _running_holder(socket_path, listener, (URI, URI_2))
    try:
        await holder.wait_ready(10)
        assert listener.refused == [URI]
        assert listener.paused == []
        assert holder.uris == [URI_2]
        assert daemon.connections == 2
        assert daemon.subscribes == [URI, URI_2]
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


class _FreshHolder(_AcceptingHolder):
    """A holder ``subscribe`` starts with no earlier record; ready at once."""

    async def wait_ready(self, timeout: float) -> None:
        del timeout


@pytest.mark.asyncio
async def test_a_new_holder_pausing_after_the_accept_leaves_memory_equal_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResourceSubscriptionStore(tmp_path / "fresh-pause.json")
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=RecordingSink(), settings=lambda: settings
    )

    async def describe(_session_key: str, _server: str) -> HolderRecord:
        return _record(uris=())

    def start_holder(record: HolderRecord, **_kwargs: Any) -> _FreshHolder:
        holder = _FreshHolder(())
        listener = rs._Listener(service, record.key)
        listener.holder = holder  # type: ignore[assignment]
        service._holders[record.key] = holder  # type: ignore[assignment]

        async def pause(_uri: str) -> None:
            assert await listener.on_paused("backend refused", holder.uris)

        holder.on_accept = pause
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start_holder)

    with pytest.raises(SubscribeRefused, match="paused: backend refused"):
        await service.subscribe(SESSION, "fake", URI)

    assert store.load() == []
    assert service.records() == []
    assert service._holders == {}


class _GatedDaemon:
    """One holder connection whose re-subscribe of ``gated`` is answered only on release.

    Records every subscribe and unsubscribe, and whether the holder was live when
    each unsubscribe arrived.
    """

    def __init__(self, gated: str) -> None:
        self.gated = gated
        self.holding = asyncio.Event()
        self.release = asyncio.Event()
        self.subscribes: list[str] = []
        self.unsubscribes: list[tuple[str, bool]] = []
        self.holder: Optional[ResourceHolder] = None

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            await _ScriptedDaemon._send(writer, {"type": "registered"})
            await reader.readline()
            await _ScriptedDaemon._send(writer, {"type": "ready"})
            while line := await reader.readline():
                msg = json.loads(line)
                if msg.get("id") is None:
                    continue
                method = msg.get("method")
                reply: dict[str, Any] = {"result": {}}
                if method == "initialize":
                    reply = {"result": {"capabilities": {"resources": {"subscribe": True}}}}
                elif method == "resources/subscribe":
                    uri = msg["params"]["uri"]
                    self.subscribes.append(uri)
                    if uri == self.gated:
                        self.holding.set()
                        await await_until(self.release, what="gated daemon was not released")
                elif method == "resources/unsubscribe":
                    assert self.holder is not None
                    self.unsubscribes.append((msg["params"]["uri"], self.holder.is_live))
                await _ScriptedDaemon._send(writer, {"jsonrpc": "2.0", "id": msg["id"], **reply})
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
@pytest.mark.parametrize(
    ("gated", "dropped"),
    [(URI, URI_2), (URI_2, URI)],
    ids=["dropped_before_the_loop_reaches_it", "dropped_after_its_resubscribe_was_accepted"],
)
async def test_an_unsubscribe_during_a_resubscribe_leaves_the_server_unsubscribed(
    short_sock_dir: Path, gated: str, dropped: str
) -> None:
    daemon = _GatedDaemon(gated)
    socket_path = short_sock_dir / "gated.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    holder = _running_holder(socket_path, _RecordingListener(), (URI, URI_2))
    daemon.holder = holder
    try:
        await asyncio.wait_for(daemon.holding.wait(), _HANG_SECS)
        await holder.unsubscribe(dropped)
        daemon.release.set()
        await holder.wait_ready(10)

        kept = gated
        assert holder.uris == [kept]
        if dropped == URI_2:
            # Never subscribed on this connection, so there is nothing to release.
            assert daemon.subscribes == [URI]
            assert daemon.unsubscribes == []
        else:
            assert daemon.subscribes == [URI, URI_2]
            assert daemon.unsubscribes == [(URI, False)]
    finally:
        daemon.release.set()
        await holder.close()
        server.close()
        await server.wait_closed()


def _capacity_frame(retry_after: Any) -> dict[str, Any]:
    return {
        "type": "rejected",
        "class": "capacity",
        "reason": "the backend is unavailable",
        "retry_after_secs": retry_after,
    }


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (60, 60.0),
        (float("nan"), 0.01),
        (float("inf"), 0.01),
        (-5, 0.01),
        (1e9, "ceiling"),
    ],
    ids=["sixty", "nan", "infinity", "negative", "huge"],
)
async def test_a_capacity_refusal_waits_as_long_as_it_asks_up_to_a_ceiling(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch, retry_after: Any, expected: Any
) -> None:
    from kiro_crew.mcp_gateway import resource_holder

    clock = _SteppedClock()
    monkeypatch.setattr(resource_holder, "asyncio", clock)
    daemon = _ScriptedDaemon([_capacity_frame(retry_after), {"type": "ready"}])
    socket_path = short_sock_dir / "retry-after.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    holder = _running_holder(socket_path, _RecordingListener(), (URI,))
    try:
        await holder.wait_ready(10)
        if expected == "ceiling":
            expected = resource_holder.CAPACITY_RETRY_MAX_SECS
        assert clock.waits[0] == expected
        assert daemon.connections == 2
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_first_subscribe_on_a_full_host_is_refused_at_once(
    tmp_path: Path, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _ScriptedDaemon([_capacity_frame(60), {"type": "ready"}])
    socket_path = short_sock_dir / "first-full.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    store = ResourceSubscriptionStore(tmp_path / "first-full.json")
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store,
        socket_path=lambda: socket_path,
        sink=RecordingSink(),
        settings=lambda: settings,
    )

    async def describe(_session_key: str, _server: str) -> HolderRecord:
        return _record(uris=())

    monkeypatch.setattr(service, "_describe", describe)
    try:
        # The ready wait is 120 s; a refusal that ends it must come far sooner.
        with pytest.raises(SubscribeRefused, match="at capacity.*backend is unavailable"):
            await asyncio.wait_for(service.subscribe(SESSION, "fake", URI), _HANG_SECS)
        assert daemon.connections == 1
        assert store.load() == []
        assert service.records() == []
        assert service._holders == {}
    finally:
        await service.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_wait_that_ends_with_the_gateway_down_says_so() -> None:
    calls = 0

    def socket_path() -> Optional[Path]:
        # A failed connect first, then no daemon at all.
        nonlocal calls
        calls += 1
        return Path("/nonexistent/kirocrew-holder.sock") if calls == 1 else None

    spec = HolderSpec(session_key=SESSION, channel_id="", server="fake", register={}, initialize={})
    holder = ResourceHolder(
        spec, socket_path, _RecordingListener(), uris=(URI,), reconnect_delay_secs=0.01
    )
    holder.start()
    try:
        await async_wait_until(
            lambda: calls >= 2 and holder._last_connect_failure == "the MCP gateway is not running",
            describe=lambda: "the holder did not retry after the gateway stopped",
        )
        with pytest.raises(HolderPaused) as raised:
            await holder.wait_ready(0)
        assert str(raised.value).endswith(": the MCP gateway is not running")
    finally:
        await holder.close()


class _RecordingWriter:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.frames.append(json.loads(data))

    async def drain(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_server_ping_is_answered_with_an_empty_result() -> None:
    holder = _holder(_Listener())
    writer = _RecordingWriter()
    conn = _Connection(asyncio.StreamReader(), writer)  # type: ignore[arg-type]

    await holder._dispatch(conn, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
    await holder._dispatch(conn, {"jsonrpc": "2.0", "id": 8, "method": "roots/list"})

    assert writer.frames[0] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert writer.frames[1]["id"] == 8 and writer.frames[1]["error"]["code"] == -32601


class _CancelledSubscribeHolder:
    def __init__(self, cancellation_point: str) -> None:
        self.cancellation_point = cancellation_point
        self.entered = asyncio.Event()
        self.closed = False

    @property
    def uris(self) -> list[str]:
        return []

    async def wait_ready(self, timeout: float) -> None:
        del timeout
        if self.cancellation_point == "ready":
            self.entered.set()
            await finished(asyncio.Event().wait(), "cancelled holder readiness")

    async def subscribe(self, uri: str) -> dict[str, Any]:
        del uri
        self.entered.set()
        await finished(asyncio.Event().wait(), "cancelled holder subscribe")
        raise AssertionError("the blocked subscribe unexpectedly resumed")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation_point", ["ready", "subscribe"])
async def test_cancelling_a_subscribe_rolls_back_the_started_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation_point: str
) -> None:
    store = ResourceSubscriptionStore(tmp_path / f"cancel-{cancellation_point}.json")
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: None, sink=RecordingSink(), settings=lambda: settings
    )
    holder = _CancelledSubscribeHolder(cancellation_point)

    async def describe(_session_key: str, _server: str) -> HolderRecord:
        return _record(uris=())

    def start_holder(record: HolderRecord, **_kwargs: Any) -> _CancelledSubscribeHolder:
        service._holders[record.key] = holder  # type: ignore[assignment]
        return holder

    monkeypatch.setattr(service, "_describe", describe)
    monkeypatch.setattr(service, "_start_holder", start_holder)
    subscribing = asyncio.create_task(service.subscribe(SESSION, "fake", URI))
    await asyncio.wait_for(holder.entered.wait(), _HANG_SECS)
    subscribing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await await_until(subscribing, what="cancelled holder subscribe did not finish")

    assert holder.closed is True
    assert service._holders == {}
    assert service.records() == store.load() == []


class _CancellationDaemon:
    """A daemon that holds one armed request after reading its frame."""

    def __init__(self, method: str, uri: Optional[str]) -> None:
        self.method = method
        self.uri = uri
        self.armed = asyncio.Event()
        self.request_read = asyncio.Event()
        self.release = asyncio.Event()
        self.response_sent = asyncio.Event()
        self.connections = 0
        self.subscribes: list[tuple[int, str]] = []
        self.unsubscribes: list[tuple[int, str]] = []
        self.reply: Any = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._held = False
        self._held_task: Optional[asyncio.Task[None]] = None

    async def _answer(
        self,
        writer: asyncio.StreamWriter,
        connection: int,
        msg: dict[str, Any],
        method: str,
        uri: Optional[str],
        held_request: bool,
    ) -> None:
        if held_request:
            await await_until(self.release, what="gated daemon was not released")
        if self.reply is not None:
            reply = self.reply(connection, method, uri, held_request)
        elif method == "initialize":
            reply = {"result": {"capabilities": {"resources": {"subscribe": True}}}}
        else:
            reply = {"result": {}}
        await _ScriptedDaemon._send(writer, {"jsonrpc": "2.0", "id": msg["id"], **reply})
        if held_request:
            self.response_sent.set()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = self.connections
        self.connections += 1
        self.writer = writer
        try:
            await reader.readline()
            await _ScriptedDaemon._send(writer, {"type": "registered"})
            await reader.readline()
            await _ScriptedDaemon._send(writer, {"type": "ready"})
            while line := await reader.readline():
                msg = json.loads(line)
                if msg.get("id") is None:
                    continue
                method = msg.get("method")
                uri = msg.get("params", {}).get("uri")
                if method == "resources/subscribe":
                    self.subscribes.append((connection, uri))
                elif method == "resources/unsubscribe":
                    self.unsubscribes.append((connection, uri))
                held_request = (
                    not self._held
                    and self.armed.is_set()
                    and method == self.method
                    and uri == self.uri
                )
                if held_request:
                    self._held = True
                    self.request_read.set()
                    self._held_task = asyncio.create_task(
                        self._answer(writer, connection, msg, method, uri, True)
                    )
                else:
                    await self._answer(writer, connection, msg, method, uri, False)
        except (OSError, ConnectionError):
            pass
        finally:
            if self._held_task is not None and not self._held_task.done():
                self._held_task.cancel()
                await asyncio.gather(self._held_task, return_exceptions=True)
            writer.close()


class _GapListener(_Listener):
    def __init__(self) -> None:
        super().__init__()
        self.gaps: list[list[str]] = []
        self.gap = asyncio.Event()

    def on_gap(self, uris: list[str]) -> None:
        self.gaps.append(uris)
        self.gap.set()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_cancelling_a_new_subscribe_reconnects_from_tracked_uris_only(
    short_sock_dir: Path,
) -> None:
    daemon = _CancellationDaemon("resources/subscribe", URI)
    socket_path = short_sock_dir / "cancel-subscribe.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _GapListener()
    holder = _running_holder(socket_path, listener, ())
    try:
        await holder.wait_ready(10)
        listener.gap.clear()
        daemon.armed.set()
        request = asyncio.create_task(holder.subscribe(URI))
        await asyncio.wait_for(daemon.request_read.wait(), _HANG_SECS)
        request.cancel()

        with pytest.raises(asyncio.CancelledError):
            await await_until(request, what="cancelled holder request did not finish")

        assert holder.uris == []
        daemon.release.set()
        await asyncio.wait_for(listener.gap.wait(), _HANG_SECS)
        assert listener.gaps[-1] == []
        assert daemon.connections == 2
        assert daemon.subscribes == [(0, URI)]
    finally:
        daemon.release.set()
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_cancelling_a_resubscribe_keeps_the_live_connection(
    short_sock_dir: Path,
) -> None:
    daemon = _CancellationDaemon("resources/subscribe", URI)
    socket_path = short_sock_dir / "cancel-resubscribe.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _GapListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        await holder.wait_ready(10)
        daemon.armed.set()
        request = asyncio.create_task(holder.subscribe(URI))
        await asyncio.wait_for(daemon.request_read.wait(), _HANG_SECS)
        request.cancel()

        with pytest.raises(asyncio.CancelledError):
            await await_until(request, what="cancelled holder request did not finish")

        daemon.release.set()
        await asyncio.wait_for(daemon.response_sent.wait(), _HANG_SECS)
        assert holder.is_live
        assert holder.uris == [URI]
        assert daemon.connections == 1
        assert daemon.subscribes == [(0, URI), (0, URI)]
    finally:
        daemon.release.set()
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_cancelling_unsubscribe_reconnects_without_the_removed_uri(
    short_sock_dir: Path,
) -> None:
    daemon = _CancellationDaemon("resources/unsubscribe", URI)
    socket_path = short_sock_dir / "cancel-unsubscribe.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _GapListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        await holder.wait_ready(10)
        listener.gap.clear()
        daemon.armed.set()
        request = asyncio.create_task(holder.unsubscribe(URI))
        await asyncio.wait_for(daemon.request_read.wait(), _HANG_SECS)
        request.cancel()

        with pytest.raises(asyncio.CancelledError):
            await await_until(request, what="cancelled holder request did not finish")

        assert holder.uris == []
        daemon.release.set()
        await asyncio.wait_for(listener.gap.wait(), _HANG_SECS)
        assert listener.gaps[-1] == []
        assert daemon.connections == 2
        assert daemon.subscribes == [(0, URI)]
        assert daemon.unsubscribes == [(0, URI)]
    finally:
        daemon.release.set()
        await holder.close()
        server.close()
        await server.wait_closed()


_KEEPALIVE = {"type": "keepalive"}


def test_the_holder_skips_the_control_frames_the_session_stub_skips() -> None:
    from kiro_crew.mcp_gateway import resource_holder, stub_bridge
    from kiro_crew.mcp_gateway.daemon import wire

    assert resource_holder._DAEMON_CONTROL_FRAME_TYPES == {
        wire.STUB_KEEPALIVE_TYPE,
        stub_bridge._BRIDGE_KEEPALIVE_TYPE,
        stub_bridge._BRIDGE_PONG_TYPE,
        stub_bridge._SPAWN_QUEUED_TYPE,
    }


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_control_frames_during_the_handshake_are_skipped_not_a_pause(
    short_sock_dir: Path,
) -> None:
    daemon = _ScriptedDaemon(
        [{"type": "ready"}],
        before_registered=(_KEEPALIVE,),
        before_ready=(_KEEPALIVE, {"type": "pong"}, {"type": "queued", "position": 1}),
    )
    socket_path = short_sock_dir / "control-frames.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _RecordingListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        await holder.wait_ready(10)
        assert holder.is_live
        assert listener.paused == []
        assert holder.paused_reason == ""
        assert daemon.connections == 1
        assert daemon.subscribes == [URI]
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_an_unexpected_frame_after_a_keepalive_still_pauses_the_holder(
    short_sock_dir: Path,
) -> None:
    daemon = _ScriptedDaemon([{"type": "registered"}], before_ready=(_KEEPALIVE,))
    socket_path = short_sock_dir / "unexpected.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _RecordingListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        with pytest.raises(HolderPaused, match="could not start"):
            await holder.wait_ready(10)
        assert len(listener.paused) == 1 and "registered" in listener.paused[0]
        assert daemon.connections == 1
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


class _TickingLoop:
    """The running loop, except that each clock reading is one second later."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.now = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loop, name)

    def time(self) -> float:
        self.now += 1.0
        return self.now


class _TickingClock(_SteppedClock):
    """:class:`_SteppedClock` whose loop clock ticks on every reading."""

    def __init__(self) -> None:
        super().__init__()
        self.loop: Optional[_TickingLoop] = None

    def get_running_loop(self) -> _TickingLoop:
        if self.loop is None:
            self.loop = _TickingLoop(asyncio.get_running_loop())
        return self.loop


class _KeepalivesOnlyDaemon(_ScriptedDaemon):
    """Answers the first ``ensure_backend`` with keepalives alone, until the holder hangs up."""

    #: More keepalives than any handshake bound in these tests could skip.
    MAX_KEEPALIVES = 10_000

    def __init__(self) -> None:
        super().__init__([{"type": "ready"}])
        self.keepalives = 0

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connections:
            await super().serve(reader, writer)
            return
        self.connections += 1
        try:
            await reader.readline()
            await self._send(writer, {"type": "registered"})
            await reader.readline()
            while self.keepalives < self.MAX_KEEPALIVES:
                await self._send(writer, _KEEPALIVE)
                self.keepalives += 1
                await asyncio.sleep(0)
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_handshake_given_only_keepalives_times_out_into_a_reconnect(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import resource_holder

    clock = _TickingClock()
    monkeypatch.setattr(resource_holder, "asyncio", clock)
    monkeypatch.setattr(resource_holder, "HANDSHAKE_TIMEOUT_SECS", 5.0)
    daemon = _KeepalivesOnlyDaemon()
    socket_path = short_sock_dir / "keepalives-only.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _RecordingListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        await holder.wait_ready(10)
        assert listener.paused == []
        assert holder.paused_reason == ""
        assert daemon.connections == 2
        assert len(clock.waits) == 1
        assert 0 < daemon.keepalives < _KeepalivesOnlyDaemon.MAX_KEEPALIVES
        assert daemon.subscribes == [URI]
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


class _UpdateListener(_RecordingListener):
    def __init__(self) -> None:
        super().__init__()
        self.updated = asyncio.Event()

    def on_update(self, uri: str) -> None:
        super().on_update(uri)
        self.updated.set()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_keepalive_on_a_live_holder_is_ignored_and_updates_still_arrive(
    short_sock_dir: Path,
) -> None:
    updated = _update(URI)
    daemon = _ScriptedDaemon([{"type": "ready"}], pushed={URI_2: (_KEEPALIVE, updated)})
    socket_path = short_sock_dir / "live-keepalive.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    listener = _UpdateListener()
    holder = _running_holder(socket_path, listener, (URI,))
    try:
        await holder.wait_ready(10)
        assert listener.updates == []
        assert "result" in await holder.subscribe(URI_2)
        await asyncio.wait_for(listener.updated.wait(), _HANG_SECS)
        assert listener.updates == [URI]
        assert holder.is_live
        assert listener.paused == []
        assert daemon.connections == 1
    finally:
        await holder.close()
        server.close()
        await server.wait_closed()


async def _start_tracked_holder(
    tmp_path: Path,
    socket_path: Path,
    uris: tuple[str, ...],
    *,
    ping_interval_secs: float,
) -> tuple[ResourceSubscriptionService, ResourceHolder, RecordingSink]:
    store = ResourceSubscriptionStore(tmp_path / "restart-holder.json")
    sink = RecordingSink()
    settings = McpResourceSubscriptionsConfig(enabled=True)
    service = ResourceSubscriptionService(
        store=store, socket_path=lambda: socket_path, sink=sink, settings=lambda: settings
    )
    record = _record(uris)
    record.subscriptions = {uri: rs.SubscriptionMeta(id="rsub") for uri in uris}
    store.put(record)
    service._records[record.key] = record
    listener = rs._Listener(service, record.key)
    holder = ResourceHolder(
        HolderSpec(SESSION, "C1", "fake", {}, {}),
        lambda: socket_path,
        listener,
        uris=uris,
        ping_interval_secs=ping_interval_secs,
        reconnect_delay_secs=0.01,
    )
    listener.holder = holder
    service._holders[record.key] = holder
    holder.start()
    await holder.wait_ready(10)
    return service, holder, sink


def _restart_error() -> dict[str, Any]:
    return {"error": {"code": -32000, "message": "backend restarted mid-call, retry: ready"}}


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
@pytest.mark.parametrize("all_refused", [False, True], ids=["one_held", "all_refused"])
async def test_a_ping_restart_reconnects_before_reconciling_replacement_verdicts(
    tmp_path: Path,
    short_sock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    all_refused: bool,
) -> None:
    daemon = _CancellationDaemon("ping", None)

    def reply(_connection: int, method: str, uri: Optional[str], held: bool) -> dict[str, Any]:
        if method == "initialize":
            return {"result": {"capabilities": {"resources": {"subscribe": True}}}}
        if held:
            return _restart_error()
        if daemon.response_sent.is_set() and method == "resources/subscribe" and uri == URI:
            return {"error": {"code": -32001, "message": "launch approval changed"}}
        return {"result": {}}

    daemon.reply = reply
    socket_path = short_sock_dir / f"ping-restart-{all_refused}.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    uris = (URI,) if all_refused else (URI, URI_2)
    service, holder, sink = await _start_tracked_holder(
        tmp_path, socket_path, uris, ping_interval_secs=0.05
    )
    from kiro_crew.mcp_gateway import resource_holder

    clock = _SteppedClock()
    monkeypatch.setattr(resource_holder, "asyncio", clock)
    try:
        daemon.armed.set()
        await await_until(daemon.request_read, what="the restart ping was not read")
        daemon.release.set()
        await async_wait_until(
            lambda: (
                service.holder(SESSION, "fake") is None
                if all_refused
                else bool(sink.of(EVENT_MAY_HAVE_CHANGED))
            ),
            describe=lambda: "the replacement verdicts did not settle",
        )
        assert daemon.connections == 2
        assert clock.waits.count(0.01) == 1
        (stored,) = service.records()
        reason = "'fake' refused the subscribe: launch approval changed"
        assert stored.paused_uris == {URI: reason}
        assert [event.uri for event in sink.of(rs.EVENT_PAUSED)] == [URI]
        assert [event.uri for event in sink.of(EVENT_MAY_HAVE_CHANGED)] == (
            [] if all_refused else [URI_2]
        )
        if all_refused:
            assert holder.uris == [] and not holder.is_live
        else:
            assert holder.uris == [URI_2] and holder.is_live
            assert daemon.writer is not None
            await _ScriptedDaemon._send(daemon.writer, _update(URI_2))
            await async_wait_until(
                lambda: bool(sink.of(EVENT_UPDATED, URI_2)),
                describe=lambda: "the post-gap update did not reach the holder",
            )
    finally:
        daemon.release.set()
        await service.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.skipif(pc.IS_WINDOWS, reason="the fake daemon listens on a Unix socket")
async def test_a_ping_restart_fails_an_inflight_subscribe_before_refusal_commits(
    tmp_path: Path, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _CancellationDaemon("resources/subscribe", URI)

    def reply(_connection: int, method: str, _uri: Optional[str], held: bool) -> dict[str, Any]:
        if method == "initialize":
            return {"result": {"capabilities": {"resources": {"subscribe": True}}}}
        if method == "ping" and daemon.request_read.is_set():
            return _restart_error()
        if method == "resources/subscribe" and daemon.request_read.is_set() and not held:
            return {"error": {"code": -32001, "message": "launch approval changed"}}
        return {"result": {}}

    daemon.reply = reply
    socket_path = short_sock_dir / "subscribe-restart-race.sock"
    server = await asyncio.start_unix_server(daemon.serve, path=str(socket_path))
    service, holder, _sink = await _start_tracked_holder(
        tmp_path, socket_path, (URI,), ping_interval_secs=0.01
    )
    entered, release = threading.Event(), threading.Event()
    real_put = service._store.put

    def gated_put(record: HolderRecord) -> None:
        if record.paused_uris:
            entered.set()
            assert release.wait(5), "test did not release the refusal write"
        real_put(record)

    monkeypatch.setattr(service._store, "put", gated_put)
    try:
        daemon.armed.set()
        subscribing = asyncio.create_task(service.subscribe(SESSION, "fake", URI))
        await await_until(daemon.request_read, what="the explicit subscribe was not read")
        await async_wait_until(entered.is_set, describe=lambda: "the refusal write did not start")
        daemon.release.set()
        await async_wait_until(
            lambda: URI not in holder._pending_uris,
            describe=lambda: "the explicit subscribe did not settle at the holder",
        )
        release.set()
        with pytest.raises(SubscribeRefused) as refused:
            await await_until(subscribing, what="the explicit subscribe did not finish")
        assert refused.value.code == rs.REFUSED_GATEWAY_UNAVAILABLE
        await async_wait_until(
            lambda: service.holder(SESSION, "fake") is None,
            describe=lambda: "the all-refused holder did not pause",
        )
        (stored,) = service.records()
        assert stored.uris == [] and URI in stored.paused_uris
        assert holder.uris == []
    finally:
        daemon.release.set()
        release.set()
        await service.close()
        server.close()
        await server.wait_closed()
