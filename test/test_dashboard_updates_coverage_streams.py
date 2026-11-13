"""The log level endpoint, the log handlers and the two long-lived stream writers.

Covers ``POST /api/logs/level``, the queue and ring log handlers, ``GET /api/logs`` and
the dashboard SSE endpoint, which withholds row metadata from a non-dashboard reader.
Neither stream leaves a handler or queue behind on hang-up. The check and apply live in
``test_dashboard_updates_coverage.py`` and ``test_dashboard_updates_coverage_apply.py``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_module_state,
    _request,
)

from kiro_crew.dashboard.handlers import updates, updates_streams
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until, await_until


class TestLogLevel:
    """``POST /api/logs/level`` and its getter."""

    @pytest.fixture(autouse=True)
    def _restore_level(self):
        root = logging.getLogger("kiro_crew")
        saved = root.level
        yield
        root.setLevel(saved)

    @pytest.mark.asyncio
    async def test_rejects_a_body_that_is_not_json(self):
        resp = await updates.api_log_level(_request(ValueError("bad json")))
        assert resp.status == 400

    @pytest.mark.parametrize("level", ["TRACE", "", "verbose", "CRITICAL"])
    @pytest.mark.asyncio
    async def test_rejects_a_level_outside_the_map(self, level):
        before = logging.getLogger("kiro_crew").level
        resp = await updates.api_log_level(_request({"level": level}))
        assert resp.status == 400
        # A rejected level must not have been applied on the way to the refusal.
        assert logging.getLogger("kiro_crew").level == before

    @pytest.mark.parametrize("level", [None, 10, ["DEBUG"]], ids=["null", "int", "list"])
    @pytest.mark.asyncio
    async def test_a_non_string_level_is_a_coded_400(self, level):
        before = logging.getLogger("kiro_crew").level
        resp = await updates.api_log_level(_request({"level": level}))
        assert resp.status == 400
        assert json.loads(resp.body.decode()) == {
            "error": "level must be a string",
            "code": "invalid_field_type",
        }
        assert logging.getLogger("kiro_crew").level == before

    @pytest.mark.asyncio
    async def test_applies_and_persists_a_valid_level_case_insensitively(self, monkeypatch):
        saved: list[str] = []

        def _fake_update_config_locked(*args, **kwargs):
            # The handler persists via a delta mutate through
            # update_config_locked; record what it wrote.
            doc = kwargs["mutate"]({})
            saved.append(doc["agent"]["log_level"])
            return doc

        monkeypatch.setattr(updates, "update_config_locked", _fake_update_config_locked)

        resp = await updates.api_log_level(_request({"level": "warning"}))

        assert resp.status == 200
        payload = json.loads(resp.body.decode())
        assert payload == {"ok": True, "level": "WARNING", "persisted": True}
        assert logging.getLogger("kiro_crew").level == logging.WARNING
        assert saved == ["WARNING"]

    @pytest.mark.asyncio
    async def test_a_failed_persist_still_applies_the_level_and_says_so(self, monkeypatch):
        """The runtime change is the point; persistence is best-effort.

        Reporting ``persisted: False`` rather than 500 keeps a read-only config
        from blocking a debugging session.
        """
        monkeypatch.setattr(
            updates,
            "update_config_locked",
            MagicMock(side_effect=OSError("read-only fs")),
        )

        resp = await updates.api_log_level(_request({"level": "DEBUG"}))

        payload = json.loads(resp.body.decode())
        assert payload["ok"] is True
        assert payload["persisted"] is False
        assert logging.getLogger("kiro_crew").level == logging.DEBUG

    @pytest.mark.asyncio
    async def test_the_getter_reports_the_live_level(self):
        logging.getLogger("kiro_crew").setLevel(logging.ERROR)
        resp = await updates.api_log_level_get(_request())
        assert json.loads(resp.body.decode()) == {"level": "ERROR"}


def _record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("kiro_crew.test", level, __file__, 1, msg, None, None)


class TestQueueLogHandler:
    """The per-connection SSE handler must never raise into ``logging``."""

    def test_formats_each_record_as_one_sse_payload(self):
        queue: asyncio.Queue[str] = asyncio.Queue()
        handler = updates._QueueLogHandler(queue)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

        handler.emit(_record("first", logging.WARNING))

        assert json.loads(queue.get_nowait()) == {
            "level": "WARNING",
            "msg": "WARNING first",
        }

    def test_a_full_queue_drops_the_entry_instead_of_raising(self):
        """``logging`` calls this from arbitrary code; raising would break callers."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        handler = updates._QueueLogHandler(queue)
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record("kept"))
        handler.emit(_record("dropped"))

        assert queue.qsize() == 1
        assert json.loads(queue.get_nowait())["msg"] == "kept"


class TestSafeWsSend:
    """A dead WebSocket subscriber is removed, not retried."""

    @pytest.mark.asyncio
    async def test_a_live_subscriber_is_kept(self):
        ws = MagicMock()
        ws.send_str = AsyncMock()
        state = MagicMock()
        state._ws_log_subscribers = {ws}

        await updates._safe_ws_send(ws, "payload", state)

        ws.send_str.assert_awaited_once_with("payload")
        assert state._ws_log_subscribers == {ws}

    @pytest.mark.asyncio
    async def test_a_dead_subscriber_is_discarded(self):
        ws = MagicMock()
        ws.send_str = AsyncMock(side_effect=ConnectionResetError("gone"))
        state = MagicMock()
        state._ws_log_subscribers = {ws}

        await updates._safe_ws_send(ws, "payload", state)

        assert state._ws_log_subscribers == set()


class TestRingLogHandler:
    """The always-on ring buffer plus its WebSocket fan-out."""

    def test_set_state_keeps_no_loop_of_its_own(self):
        """The handler defers to the dashboard's one serving loop.

        `emit()` runs on any thread; a loop captured once in `set_state` from a non-loop
        context would latch None and drop every fan-out, so it resolves at emit time.
        """
        handler = updates._RingLogHandler(collections.deque(maxlen=4))
        handler.set_state(MagicMock())
        assert not hasattr(handler, "_loop")

    def test_emit_appends_to_the_ring_and_honours_maxlen(self):
        ring: collections.deque[str] = collections.deque(maxlen=2)
        handler = updates._RingLogHandler(ring, 2)
        handler.setFormatter(logging.Formatter("%(message)s"))

        for text in ("one", "two", "three"):
            handler.emit(_record(text))

        assert [json.loads(entry)["msg"] for entry in ring] == ["two", "three"]

    def test_a_formatting_failure_is_swallowed(self):
        ring: collections.deque[str] = collections.deque(maxlen=2)
        handler = updates._RingLogHandler(ring)
        broken = MagicMock()
        broken.format.side_effect = ValueError("bad format string")
        handler.setFormatter(broken)

        handler.emit(_record())

        assert list(ring) == []

    @pytest.mark.asyncio
    async def test_emit_fans_out_to_every_websocket_subscriber(self):
        ring: collections.deque[str] = collections.deque(maxlen=4)
        handler = updates._RingLogHandler(ring)
        handler.setFormatter(logging.Formatter("%(message)s"))
        ws = MagicMock()
        ws.send_str = AsyncMock()
        state = MagicMock()
        state._ws_log_subscribers = {ws}
        state.serving_loop = asyncio.get_running_loop()
        handler.set_state(state)

        handler.emit(_record("broadcast me"))
        # The fan-out is scheduled via call_soon_threadsafe, so it lands on a
        # later loop iteration rather than inside emit().
        await async_wait_until(
            lambda: ws.send_str.await_count,
            describe=lambda: f"send_str awaits={ws.send_str.await_count}",
        )

        ws.send_str.assert_awaited_once()
        payload = json.loads(ws.send_str.await_args[0][0])
        assert payload == {"type": "log", "data": {"level": "INFO", "msg": "broadcast me"}}
        assert json.loads(ring[0])["msg"] == "broadcast me"

    def test_emit_with_no_subscribers_still_fills_the_ring(self):
        ring: collections.deque[str] = collections.deque(maxlen=4)
        handler = updates._RingLogHandler(ring)
        handler.setFormatter(logging.Formatter("%(message)s"))
        state = MagicMock()
        state._ws_log_subscribers = set()
        handler.set_state(state)

        handler.emit(_record("ring only"))

        assert json.loads(ring[0])["msg"] == "ring only"

    @pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
    def test_a_closed_loop_drops_the_fan_out_but_keeps_the_ring(self):
        """The gateway shuts its loop down while the handler stays attached.

        ``call_soon_threadsafe`` then raises; the ring entry must still be kept. The send
        coroutine is abandoned un-awaited, which is why the warning filter is above.
        """
        ring: collections.deque[str] = collections.deque(maxlen=4)
        handler = updates._RingLogHandler(ring)
        handler.setFormatter(logging.Formatter("%(message)s"))
        state = MagicMock()
        state._ws_log_subscribers = {MagicMock()}
        handler._state = state
        state.serving_loop = MagicMock()
        state.serving_loop.call_soon_threadsafe.side_effect = RuntimeError("event loop is closed")

        handler.emit(_record("during shutdown"))

        assert json.loads(ring[0])["msg"] == "during shutdown"

    def test_install_is_idempotent_and_attaches_exactly_one_handler(self, monkeypatch):
        root = logging.getLogger("kiro_crew")
        monkeypatch.setattr(updates, "_log_ring_handler_installed", False)
        monkeypatch.setattr(updates, "_log_ring_handler", None)
        before = list(root.handlers)
        try:
            first = updates.install_log_ring_handler()
            second = updates.install_log_ring_handler()
            assert first is not None
            # Second call is a no-op: two ring handlers would double every entry.
            assert second is first
            assert len([h for h in root.handlers if h not in before]) == 1
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)


def _stub_stream(monkeypatch, *, prepare_raises: BaseException | None = None) -> list[bytes]:
    """Capture what a handler writes without opening a socket."""
    writes: list[bytes] = []

    async def _prepare(self, request):  # noqa: ANN001 - stub mirrors aiohttp
        if prepare_raises is not None:
            raise prepare_raises
        return None

    async def _write(self, data):  # noqa: ANN001 - stub mirrors aiohttp
        writes.append(bytes(data))

    monkeypatch.setattr(web.StreamResponse, "prepare", _prepare)
    monkeypatch.setattr(web.StreamResponse, "write", _write)
    return writes


class TestLogsStream:
    """``GET /api/logs`` — ring replay, then a live tail."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _own_shutdown_event(self, monkeypatch):
        """The module binds ``shutdown_event`` at import, so patch it there."""
        event = asyncio.Event()
        monkeypatch.setattr(updates, "shutdown_event", event)
        return event

    @pytest.mark.asyncio
    async def test_a_client_that_hangs_up_during_prepare_is_not_an_error(self, monkeypatch):
        _stub_stream(monkeypatch, prepare_raises=ClientConnectionResetError("gone"))
        updates._log_ring.append(json.dumps({"level": "INFO", "msg": "x"}))
        resp = await updates.api_logs(_request(query={"lines": "10"}))
        assert isinstance(resp, web.StreamResponse)

    @pytest.mark.asyncio
    async def test_a_client_that_hangs_up_during_replay_is_not_an_error(self, monkeypatch):
        writes = _stub_stream(monkeypatch)

        async def _boom(self, data):  # noqa: ANN001 - stub mirrors aiohttp
            raise ConnectionResetError("gone")

        updates._log_ring.append(json.dumps({"level": "INFO", "msg": "x"}))
        monkeypatch.setattr(web.StreamResponse, "write", _boom)

        await updates.api_logs(_request(query={"lines": "5"}))

        assert writes == []
        # The queue handler is only installed AFTER the replay, so an abort here
        # must not leave one attached to the logger.
        assert not any(
            isinstance(h, updates._QueueLogHandler) for h in logging.getLogger("kiro_crew").handlers
        )

    @pytest.mark.parametrize("raw", ["not-a-number", "", "1e5"])
    @pytest.mark.asyncio
    async def test_an_unparseable_lines_param_falls_back_to_the_default(self, monkeypatch, raw):
        writes = _stub_stream(monkeypatch)
        for index in range(250):
            updates._log_ring.append(json.dumps({"level": "INFO", "msg": f"m{index}"}))
        event = updates.shutdown_event
        event.set()  # replay only, no live tail

        await updates.api_logs(_request(query={"lines": raw}))

        assert len(writes) == 200
        assert json.loads(writes[0].decode().removeprefix("data: ").strip())["msg"] == "m50"

    @pytest.mark.asyncio
    async def test_the_replay_is_capped_by_the_lines_param(self, monkeypatch):
        writes = _stub_stream(monkeypatch)
        for index in range(10):
            updates._log_ring.append(json.dumps({"level": "INFO", "msg": f"m{index}"}))
        updates.shutdown_event.set()

        await updates.api_logs(_request(query={"lines": "3"}))

        replayed = [json.loads(w.decode().removeprefix("data: ").strip())["msg"] for w in writes]
        # The TAIL of the ring, so the client sees the most recent history.
        assert replayed == ["m7", "m8", "m9"]

    @pytest.mark.asyncio
    async def test_live_entries_are_streamed_and_the_handler_is_removed_on_cancel(
        self, monkeypatch
    ):
        writes = _stub_stream(monkeypatch)
        root = logging.getLogger("kiro_crew")
        before = len(root.handlers)

        task = asyncio.ensure_future(updates.api_logs(_request(query={"lines": "1"})))
        await async_wait_until(
            lambda: any(isinstance(h, updates._QueueLogHandler) for h in root.handlers),
            describe=lambda: f"handlers={root.handlers!r}",
        )

        logging.getLogger("kiro_crew.stream").warning("live one")
        logging.getLogger("kiro_crew.stream").warning("live two")
        await async_wait_until(lambda: len(writes) >= 2, describe=lambda: f"writes={writes!r}")
        task.cancel()
        await await_until(task, what="the cancelled logs stream to return")

        payloads = [json.loads(w.decode().removeprefix("data: ").strip())["msg"] for w in writes]
        assert any("live one" in p for p in payloads)
        assert any("live two" in p for p in payloads)
        # The finally block must detach the per-connection handler, or every
        # disconnected client would keep formatting records forever.
        assert len(root.handlers) == before

    @pytest.mark.asyncio
    async def test_an_idle_stream_emits_a_keepalive_comment(self, monkeypatch):
        """A silent logger must not let an intermediary time the connection out.

        The real wait is 30s, so the wait itself is short-circuited rather than
        slept through.
        """
        writes = _stub_stream(monkeypatch)
        idle = asyncio.Event()  # set at cleanup: after the first keepalive the stream waits here
        real_handler = updates_streams._QueueLogHandler

        class _KeepaliveHandler(real_handler):
            """The stream's log handler, with the first read of its queue timing out at once.

            The stream waits ``wait_for(queue.get(), timeout=30)`` and writes a keepalive
            when that wait times out. The first read raises that ``TimeoutError`` at once; a
            later one parks on ``idle`` until the test cancels the stream.
            """

            def __init__(self, queue):
                super().__init__(queue)
                keepalive_due = [True]

                async def _get():
                    if keepalive_due:
                        keepalive_due.clear()
                        raise asyncio.TimeoutError
                    await idle.wait()
                    # Released at cleanup: end the stream as a cancel would, not with another timeout.
                    raise asyncio.CancelledError

                queue.get = _get

        # Through the facade: it reaches the streams module that builds the handler.
        monkeypatch.setattr(updates, "_QueueLogHandler", _KeepaliveHandler)

        try:
            task = asyncio.ensure_future(updates.api_logs(_request()))
            await async_wait_until(
                lambda: b": keepalive\n\n" in writes, describe=lambda: f"writes={writes!r}"
            )
            task.cancel()
            await await_until(task, what="the cancelled logs stream to return")
        finally:
            idle.set()

        assert b": keepalive\n\n" in writes


def test_neither_chat_message_door_builds_the_frame_by_hand() -> None:
    """Both delivery doors must route through the shared serialiser.

    `state_parts/slots_push.py` (WS) and `handlers/updates_streams.py` (SSE) must not
    hand-build frames. Asserted on SOURCE: a payload test calling `chat_message_frame`
    passes after a door is re-inlined. Scoped to each `chat_message` branch, since the
    helper's own definition reads as a hand-built frame.
    """
    from pathlib import Path

    import kiro_crew

    def _branch(src: str) -> str:
        """The `chat_message` arm's body, up to the next sibling arm."""
        start = src.index('elif msg_type == "chat_message":')
        rest = src[start + 1 :]
        ends = [
            i
            for i in (rest.find("\n            elif "), rest.find("\n                    elif "))
            if i != -1
        ]
        return rest[: min(ends)] if ends else rest

    root = Path(kiro_crew.__file__).parent
    for rel in ("dashboard/state_parts/slots_push.py", "dashboard/handlers/updates_streams.py"):
        branch = _branch((root / rel).read_text(encoding="utf-8"))
        assert "chat_message_frame(" in branch, (
            f"{rel}'s chat_message arm no longer calls chat_message_frame — a "
            f"door that builds the frame itself is how the two transports "
            f"drifted apart in #8045"
        )
        assert '"slot":' not in branch, (
            f"{rel}'s chat_message arm names the frame's keys inline instead of "
            f"delegating to chat_message_frame"
        )


class TestDashboardStream:
    """The dashboard SSE endpoint — one event name per queued note type."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _own_shutdown_event(self, monkeypatch):
        event = asyncio.Event()
        monkeypatch.setattr(updates, "shutdown_event", event)
        return event

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _warm_status_counts(self, monkeypatch):
        # The SSE `dashboard` frame routes its lesson/cron counts through the
        # shared status_counts cache now, not an inline status_snapshot call.
        # Seed it warm so the writer serves serializable counts without the
        # MagicMock state's count methods (which return non-serializable Mocks)
        # ever running.
        from kiro_crew.dashboard import status_counts as sc

        monkeypatch.setattr(sc, "_counts_cache", (0, 0))
        clock = ManualClock()
        clock.install(monkeypatch, sc)
        monkeypatch.setattr(sc, "_counts_cache_ts", clock.monotonic())
        monkeypatch.setattr(sc, "_counts_cache_failures", 0)
        monkeypatch.setattr(sc, "_counts_refresh_inflight", False)

    def _request_with_queue(
        self, notes: list[dict], *, is_dashboard_user: bool = True
    ) -> MagicMock:
        req = _request()
        # `api_stream` reads `request["is_dashboard_user"]` — the auth
        # middleware's POSITIVE signal — to decide whether row metadata may go
        # on this unfiltered queue. A bare MagicMock returns a truthy Mock from
        # `.get()`, which would make the app-token assertion below pass on mock
        # truthiness rather than on the flag, so wire it to a real mapping.
        req.get = {"is_dashboard_user": is_dashboard_user}.get
        queue: asyncio.Queue[dict] = asyncio.Queue()
        for note in notes:
            queue.put_nowait(note)
        state = req.app["state"]
        state.register_sse.return_value = queue
        state.status_snapshot.return_value = {"sessions": 0, "update_available": True}
        return req

    @pytest.mark.asyncio
    async def test_a_client_that_hangs_up_during_prepare_is_not_an_error(self, monkeypatch):
        _stub_stream(monkeypatch, prepare_raises=ConnectionResetError("gone"))
        req = self._request_with_queue([])
        resp = await updates.api_stream(req)
        assert isinstance(resp, web.StreamResponse)
        # No queue was registered, so none can be leaked.
        req.app["state"].unregister_sse.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_note_type_gets_its_own_sse_event_name(self, monkeypatch):
        writes = _stub_stream(monkeypatch)
        req = self._request_with_queue(
            [
                {"_type": "slots", "slots": "[]"},
                {"_type": "slot_title", "key": "s1", "title": "Renamed"},
                {"_type": "refresh", "kinds": "updating"},
                {"_type": "chat_message", "slot": "s1", "role": "assistant", "content": "hi"},
                {"kind": "toast", "text": "done"},
            ]
        )

        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(lambda: len(writes) >= 6, describe=lambda: f"writes={writes!r}")
        updates.shutdown_event.set()
        await await_until(task, what="the stream handler returning on shutdown")

        text = b"".join(writes).decode()
        assert "event: slots\ndata: []\n\n" in text
        assert '"title": "Renamed"' in text
        assert "event: refresh\ndata: updating\n\n" in text
        assert "event: chat_message" in text
        assert "event: notification" in text
        # The periodic dashboard frame carries the version and the update flag.
        dashboard = [w.decode() for w in writes if w.startswith(b"event: dashboard")]
        assert dashboard
        payload = json.loads(dashboard[0].split("data: ", 1)[1].strip())
        assert payload["update_available"] is True
        assert payload["version"] == updates._local_version
        # The per-client queue is always handed back, disconnect or not.
        req.app["state"].unregister_sse.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_dashboard_user", [True, False])
    async def test_only_a_dashboard_reader_gets_pin_ranks_on_sse(
        self, monkeypatch, is_dashboard_user
    ):
        """A rank is a place in the person's whole order; a scoped reader gets none."""
        writes = _stub_stream(monkeypatch)
        slots = json.dumps([{"key": "a", "pin_rank": 0}])
        req = self._request_with_queue(
            [{"_type": "slots", "slots": slots}], is_dashboard_user=is_dashboard_user
        )
        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(
            lambda: any(w.startswith(b"event: slots") for w in writes),
            describe=lambda: f"writes={writes!r}",
        )
        updates.shutdown_event.set()
        await await_until(task, what="the stream handler returning on shutdown")
        frames = [w.decode() for w in writes if w.startswith(b"event: slots")]
        assert frames, "no slots frame was written"
        rows = json.loads(frames[0].split("data: ", 1)[1].strip())
        assert ("pin_rank" in rows[0]) is is_dashboard_user

    @pytest.mark.asyncio
    async def test_the_sse_chat_message_frame_carries_the_rows_identity(self, monkeypatch):
        """`meta` (and `cls`) survive the SSE relay, as they do on the WebSocket.

        `meta.mid` is the row identity a client dedups on, so a relay that
        rebuilds a four-field subset publishes frames no consumer can recognise
        as a redelivery. Both doors read the SAME producer note, so they must
        not disagree about what a `chat_message` frame contains.
        """
        writes = _stub_stream(monkeypatch)
        req = self._request_with_queue(
            [
                {
                    "_type": "chat_message",
                    "slot": "s1",
                    "role": "assistant",
                    "content": "hi",
                    "ts": "2026-09-03T00:00:00Z",
                    "cls": '{"mid": "m-1"}',
                    "meta": {"mid": "m-1", "kind": "compaction"},
                }
            ]
        )

        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(
            lambda: any(w.startswith(b"event: chat_message") for w in writes),
            describe=lambda: f"writes={writes!r}",
        )
        updates.shutdown_event.set()
        await await_until(task, what="the stream handler returning on shutdown")

        frames = [w.decode() for w in writes if w.startswith(b"event: chat_message")]
        assert frames, "no chat_message frame was written"
        payload = json.loads(frames[0].split("data: ", 1)[1].strip())
        assert payload["meta"] == {"mid": "m-1", "kind": "compaction"}
        assert payload["cls"] == '{"mid": "m-1"}'
        # The pre-existing fields are unchanged — this widens the frame, it does
        # not reshape it.
        assert payload["slot"] == "s1"
        assert payload["role"] == "assistant"
        assert payload["content"] == "hi"
        assert payload["ts"] == "2026-09-03T00:00:00Z"

    @pytest.mark.asyncio
    async def test_a_chat_message_without_meta_gains_no_empty_keys(self, monkeypatch):
        """A note carrying no `cls`/`meta` still serialises to the bare frame.

        Guards the other direction of the passthrough: absent metadata must not
        become `null`/`{}` keys a consumer would have to special-case.
        """
        writes = _stub_stream(monkeypatch)
        req = self._request_with_queue(
            [{"_type": "chat_message", "slot": "s1", "role": "user", "content": "hi"}]
        )

        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(
            lambda: any(w.startswith(b"event: chat_message") for w in writes),
            describe=lambda: f"writes={writes!r}",
        )
        updates.shutdown_event.set()
        await await_until(task, what="the stream handler returning on shutdown")

        frames = [w.decode() for w in writes if w.startswith(b"event: chat_message")]
        assert frames, "no chat_message frame was written"
        payload = json.loads(frames[0].split("data: ", 1)[1].strip())
        assert "meta" not in payload
        assert "cls" not in payload
        assert payload == {"slot": "s1", "role": "user", "content": "hi", "ts": ""}

    @pytest.mark.asyncio
    async def test_an_app_token_stream_never_receives_row_metadata(self, monkeypatch):
        """An app token on `/api/stream` gets no `cls`/`meta`, whatever its scope.

        SSE fans out with no per-app filter, and `meta` carries `tool_input`, a live
        `oauth_url` and `approval_id`, so it rides only the WS door, which filters.
        """
        writes = _stub_stream(monkeypatch)
        req = self._request_with_queue(
            [
                {
                    "_type": "chat_message",
                    "slot": "s1",
                    "role": "assistant",
                    "content": "hi",
                    "ts": "2026-09-03T00:00:00Z",
                    "cls": '{"mid": "m-3", "tool_input": "secret"}',
                    "meta": {"mid": "m-3", "tool_input": "secret"},
                }
            ],
            is_dashboard_user=False,
        )

        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(
            lambda: any(w.startswith(b"event: chat_message") for w in writes),
            describe=lambda: f"writes={writes!r}",
        )
        updates.shutdown_event.set()
        await await_until(task, what="the stream handler returning on shutdown")

        frames = [w.decode() for w in writes if w.startswith(b"event: chat_message")]
        assert frames, "no chat_message frame was written"
        payload = json.loads(frames[0].split("data: ", 1)[1].strip())
        assert "meta" not in payload
        assert "cls" not in payload
        assert "secret" not in frames[0]
        # The pre-existing four-field projection is unchanged for an app token —
        # this withholds the new metadata, it does not newly restrict the frame.
        assert payload == {
            "slot": "s1",
            "role": "assistant",
            "content": "hi",
            "ts": "2026-09-03T00:00:00Z",
        }

    @pytest.mark.asyncio
    async def test_a_disconnect_mid_stream_unregisters_the_queue(self, monkeypatch):
        """A leaked queue keeps growing for a client that will never read it."""
        _stub_stream(monkeypatch)
        req = self._request_with_queue([])

        async def _boom(self, data):  # noqa: ANN001 - stub mirrors aiohttp
            raise ClientConnectionResetError("gone")

        monkeypatch.setattr(web.StreamResponse, "write", _boom)

        resp = await updates.api_stream(req)

        assert isinstance(resp, web.StreamResponse)
        req.app["state"].unregister_sse.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_queue_that_empties_mid_drain_ends_the_drain(self, monkeypatch):
        """``empty()`` then ``get_nowait()`` is not atomic under a second consumer.

        The defensive break is what keeps a raced drain from raising out of the
        stream and dropping the client.
        """
        _stub_stream(monkeypatch)
        req = _request()

        class _RacingQueue:
            def __init__(self) -> None:
                self.probes = 0

            def empty(self) -> bool:
                self.probes += 1
                return self.probes > 1

            def get_nowait(self) -> dict:
                raise asyncio.QueueEmpty

        queue = _RacingQueue()
        state = req.app["state"]
        state.register_sse.return_value = queue
        state.status_snapshot.return_value = {"sessions": 0}

        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(lambda: queue.probes > 1, describe=lambda: f"probes={queue.probes}")
        updates.shutdown_event.set()
        resp = await await_until(task, what="the stream handler returning on shutdown")

        assert isinstance(resp, web.StreamResponse)
        state.unregister_sse.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation_is_swallowed_and_still_unregisters(self, monkeypatch):
        _stub_stream(monkeypatch)
        req = self._request_with_queue([])

        task = asyncio.ensure_future(updates.api_stream(req))
        await async_wait_until(
            lambda: req.app["state"].register_sse.called,
            describe=lambda: "register_sse never called",
        )
        task.cancel()
        resp = await await_until(task, what="the cancelled dashboard stream to return")

        assert isinstance(resp, web.StreamResponse)
        req.app["state"].unregister_sse.assert_called_once()
