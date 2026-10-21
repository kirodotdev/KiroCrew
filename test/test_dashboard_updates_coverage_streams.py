"""The log level endpoint, the log handlers and the two long-lived stream writers.

``POST /api/logs/level`` refuses a bad body or level, applies a valid one and persists it
best-effort, and its getter reports the live level. The per-connection queue handler and
the always-on ring handler never raise into ``logging``; the ring fans out to WebSocket
subscribers, a dead subscriber is dropped, and installing the ring handler is idempotent.
``GET /api/logs`` replays the ring's tail, then streams live entries and a keepalive while
idle. The dashboard SSE endpoint gives each note type its own event name and withholds row
metadata and pin ranks from a reader that is not the dashboard user. Neither stream leaves a
logging handler or an SSE queue behind when the client hangs up or the request is
cancelled, and a source scan pins that both chat-message doors build the frame through
the shared serialiser. The check, the auto-update switch and the changelog cache live in
``test_dashboard_updates_coverage.py``, the apply in
``test_dashboard_updates_coverage_apply.py``, and the shared fixture and request stub in
``dashboard_updates_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_module_state,
    _request,
)

from kiro_crew.dashboard.handlers import updates
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

        Its `emit()` runs on arbitrary threads while `set_state` captured a loop
        ONCE, so any caller attaching the state from a non-loop context latches
        None permanently and every later fan-out is dropped with nothing
        surfaced. Today's only production caller runs inside `start_dashboard`,
        so the capture did succeed -- this pins the shape, not a live bug.
        Resolving through the state at emit time removes the latch entirely.
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

        ``call_soon_threadsafe`` then raises, and losing the ring entry over a
        subscriber that cannot be reached would blind the Logs page during
        exactly the shutdown a reader wants to see.

        The send coroutine is built BEFORE the scheduling call, so this path also
        abandons it un-awaited -- harmless (nothing has run yet) but the reason
        for the warning filter above.
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
        # flake-ok: pre-existing unbounded await of the cancelled logs stream, moved verbatim; tracked for a de-flake pass
        await task

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
        real_wait_for = asyncio.wait_for

        async def _no_waiting(awaitable, timeout=None):
            if timeout == 30:
                awaitable.close()  # nothing has run yet; close it rather than leak it
                # flake-ok: pre-existing 10 ms pause inside the stubbed wait_for timeout, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms pause inside the stubbed wait_for, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.01)
                raise asyncio.TimeoutError
            return await real_wait_for(awaitable, timeout)

        # flake-ok: pre-existing process-wide asyncio.wait_for stub for the keepalive branch, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(asyncio, "wait_for", _no_waiting)

        task = asyncio.ensure_future(updates.api_logs(_request()))
        await async_wait_until(
            lambda: b": keepalive\n\n" in writes, describe=lambda: f"writes={writes!r}"
        )
        task.cancel()
        # flake-ok: pre-existing unbounded await of the cancelled logs stream, moved verbatim; tracked for a de-flake pass
        await task

        assert b": keepalive\n\n" in writes


def test_neither_chat_message_door_builds_the_frame_by_hand() -> None:
    """Both delivery doors must route through the shared serialiser.

    `_broadcast()` feeds ONE note to two doors — the WS arm in `state_parts/slots_push.py` and
    the SSE arm in `handlers/updates_streams.py`. While each built the frame by hand
    they could disagree silently, which is exactly how `meta` can go missing on
    the SSE side while the WS side carries it.

    Asserted on the SOURCE, not on a payload, because that is the only form
    that catches the regression this guards: a payload comparison calling
    `chat_message_frame` directly still passes after a door is re-inlined, since
    it never exercises that door's own construction.

    Scoped to each door's own `chat_message` BRANCH rather than the whole file.
    A file-wide scan cannot work here: `chat_message_frame` is *defined* in
    `state_parts/transcript_rows.py`, so its own `"slot": note[...]` body reads as a hand-built frame
    and its `def` line satisfies a naive "calls the helper" check.
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
        # flake-ok: pre-existing real-clock stamp that marks the status-counts cache fresh, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(sc, "_counts_cache_ts", time.monotonic())
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

        `_broadcast()` fans the raw note out to EVERY registered SSE queue with
        no per-app filtering, so unlike the WS door there is no downstream gate
        to drop a row an app may not see. `meta` carries tool/LLM content
        (`tool_input`, a live `oauth_url`, `approval_id`), so including it here
        would expose it to any app token granted this route regardless of its
        `slots:*` scope — the same class of leak that put public-repo status
        onto this same endpoint. The metadata therefore rides only the door that
        filters.
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
        # flake-ok: pre-existing unbounded await of the cancelled dashboard stream, moved verbatim; tracked for a de-flake pass
        resp = await task

        assert isinstance(resp, web.StreamResponse)
        req.app["state"].unregister_sse.assert_called_once()
