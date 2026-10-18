"""A released KAS session keeps its record for ``session.kas_delete_delay_secs``.

On KAS the teardown verb, ``_kiro/session/delete``, frees the session and removes
its record in one step. An engine that advertises ``sessionCapabilities.close``
also has ``session/close``, which frees the session and keeps the record. When a
delay is configured, a task-runner session and a reused workflow worker's previous
conversation end with the close, an entry under ``released-sessions/`` schedules
the delete, and the session cleanup loop sends it once it is due.

Measured against KAS 0.66.22 and 0.66.26 through kiro-cli 2.27.1 and 2.29.0: the
close keeps ``session.json`` and ``messages.jsonl``, a later prompt makes the
engine hydrate the session from disk, a restarted process replays the turn, and a
delete sent later by a process started in another directory removes the record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import released_sessions, sandbox_launch_mark
from kiro_crew.acp.harness import _HARNESSES, harness_for
from kiro_crew.acp.runtime import _TERMINATE_TIMEOUT, AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    METHOD_KAS_SESSION_DELETE,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_TERMINATE,
)
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

#: What KAS 0.66.22 and 0.66.26 answer in ``initialize``.
_CLOSE_ADVERTISED = {
    "loadSession": True,
    "sessionCapabilities": {"list": {}, "close": {}, "delete": {}, "fork": {}},
}
#: What KAS 0.63.3 answered (``test/fixtures/acp_frames/kas/session.jsonl``).
_BEFORE_CLOSE = {"loadSession": True, "sessionCapabilities": {"list": {}, "fork": {}}}

_SID = "sess_3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"


def _runtime(tmp_path, backend: str, capabilities: dict | None = None) -> AcpRuntime:
    runtime = AcpRuntime(
        work_dir=tmp_path / f"ws-{backend or 'kiro'}",
        sandbox_mode="off",
        acp_backend=backend,
    )
    runtime._process = object()  # type: ignore[assignment]
    runtime._agent_capabilities = dict(capabilities or {})
    return runtime


def _sent(send: AsyncMock) -> list[str]:
    return [c.args[0] for c in send.await_args_list]


def _entries() -> list[dict]:
    directory = released_sessions.released_sessions_dir()
    if not directory.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]


class TestTheReleaseVerbIsTheHarnesssAnswer:
    def test_kas_closes_when_the_engine_advertises_close(self):
        policy = harness_for(ACP_BACKEND_KAS).release(_CLOSE_ADVERTISED)
        assert policy is not None
        assert policy.method == METHOD_SESSION_CLOSE
        assert policy.notification is False

    @pytest.mark.parametrize(
        "capabilities",
        [
            _BEFORE_CLOSE,
            {},
            None,
            {"sessionCapabilities": {"close": True}},
            {"sessionCapabilities": "close"},
        ],
        ids=["engine-before-close", "empty", "no-handshake", "close-not-an-object", "malformed"],
    )
    def test_kas_refuses_without_the_advertised_verb(self, capabilities):
        assert harness_for(ACP_BACKEND_KAS).release(capabilities) is None

    @pytest.mark.parametrize(
        "backend", sorted(b for b in _HARNESSES if b != ACP_BACKEND_KAS), ids=lambda b: b or "kiro"
    )
    def test_other_hosts_have_no_release(self, backend):
        """Their teardown already keeps the record, so there is nothing to defer."""
        assert harness_for(backend).release(_CLOSE_ADVERTISED) is None

    def test_the_teardown_is_still_the_delete(self):
        assert harness_for(ACP_BACKEND_KAS).teardown.method == METHOD_KAS_SESSION_DELETE


class TestTheRuntimeReleasesAndDeletesLater:
    @pytest.mark.asyncio
    async def test_release_sends_close_and_unregisters(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS, _CLOSE_ADVERTISED)
        runtime._session_queues[_SID] = asyncio.Queue()
        send = AsyncMock(return_value={})
        monkeypatch.setattr(runtime, "_send_and_await", send)

        assert runtime.can_release() is True
        assert await runtime.release_session(_SID) is True
        send.assert_awaited_once_with(
            METHOD_SESSION_CLOSE, {"sessionId": _SID}, timeout=_TERMINATE_TIMEOUT
        )
        assert _SID not in runtime._session_queues

    @pytest.mark.asyncio
    async def test_an_engine_before_close_releases_nothing(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS, _BEFORE_CLOSE)
        runtime._session_queues[_SID] = asyncio.Queue()
        send = AsyncMock(return_value={})
        monkeypatch.setattr(runtime, "_send_and_await", send)

        assert runtime.can_release() is False
        assert await runtime.release_session(_SID) is False
        send.assert_not_awaited()
        assert _SID not in runtime._session_queues

    @pytest.mark.asyncio
    async def test_a_failed_close_is_reported(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS, _CLOSE_ADVERTISED)
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=RuntimeError("no")))
        assert await runtime.release_session(_SID) is False

    @pytest.mark.asyncio
    async def test_a_record_is_deleted_by_id_on_any_process(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        send = AsyncMock(return_value={"success": True})
        monkeypatch.setattr(runtime, "_send_and_await", send)

        assert await runtime.delete_session_record(_SID) is True
        send.assert_awaited_once_with(
            METHOD_KAS_SESSION_DELETE, {"sessionId": _SID}, timeout=_TERMINATE_TIMEOUT
        )

    @pytest.mark.asyncio
    async def test_a_failed_or_impossible_delete_is_reported(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=RuntimeError("no")))
        assert await runtime.delete_session_record(_SID) is False
        runtime._dead = True
        assert await runtime.delete_session_record(_SID) is False


class TestDestroy:
    """``AcpSessionHandle.destroy`` with and without a delay."""

    def _handle(self, tmp_path, monkeypatch, *, backend=ACP_BACKEND_KAS, caps=_CLOSE_ADVERTISED):
        # A suite run from a shell inside an agent's sandbox inherits the marker.
        monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER, raising=False)
        runtime = _runtime(tmp_path, backend, caps)
        send = AsyncMock(return_value={})
        monkeypatch.setattr(runtime, "_send_and_await", send)
        handle = AcpSessionHandle(_SID, asyncio.Queue(), runtime)
        runtime._session_queues[_SID] = handle._queue
        return handle, runtime, send

    @pytest.mark.asyncio
    async def test_a_delay_closes_now_and_schedules_the_delete(self, tmp_path, monkeypatch):
        handle, runtime, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0

        await handle.destroy()

        assert _sent(send) == [METHOD_SESSION_CLOSE]
        [entry] = _entries()
        assert entry["session_id"] == _SID
        assert entry["backend"] == ACP_BACKEND_KAS
        assert entry["delete_at"] - entry["released_at"] == pytest.approx(1800.0)
        assert _SID not in runtime._session_queues

    @pytest.mark.asyncio
    async def test_the_entry_is_stored_before_the_close_is_sent(self, tmp_path, monkeypatch):
        """An exit between the two must leave an entry, not a record nothing deletes."""
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0
        seen: list[list[str]] = []

        async def answer(method, *_a, **_k):
            seen.append([e["session_id"] for e in _entries()])
            return {}

        send.side_effect = answer

        await handle.destroy()

        assert seen == [[_SID]]

    @pytest.mark.asyncio
    async def test_no_delay_deletes_at_once(self, tmp_path, monkeypatch):
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)

        await handle.destroy()

        assert _sent(send) == [METHOD_KAS_SESSION_DELETE]
        assert _entries() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
    async def test_a_private_session_is_deleted_at_once_whatever_the_delay(
        self, tmp_path, monkeypatch, memory_mode
    ):
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0
        handle.memory_mode = memory_mode

        await handle.destroy()

        assert _sent(send) == [METHOD_KAS_SESSION_DELETE]
        assert _entries() == []

    @pytest.mark.asyncio
    async def test_a_process_the_sandbox_launched_is_deleted_at_once(self, tmp_path, monkeypatch):
        """Its ``released-sessions/`` is masked, so an entry written there would be lost."""
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0
        # What ``cli.main`` records before it drops ``KIROCREW_SANDBOX_ACTIVE``.
        monkeypatch.setattr(sandbox_launch_mark, "_launched_in_sandbox", True)

        await handle.destroy()

        assert _sent(send) == [METHOD_KAS_SESSION_DELETE]
        assert _entries() == []

    @pytest.mark.asyncio
    async def test_an_engine_before_close_is_deleted_at_once(self, tmp_path, monkeypatch):
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch, caps=_BEFORE_CLOSE)
        handle.delete_delay_secs = 1800.0

        await handle.destroy()

        assert _sent(send) == [METHOD_KAS_SESSION_DELETE]
        assert _entries() == []

    @pytest.mark.asyncio
    async def test_a_failed_close_deletes_at_once_and_keeps_the_entry(self, tmp_path, monkeypatch):
        """The delete is best-effort too, so the entry stays until one is confirmed."""
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0

        async def answer(method, *_a, **_k):
            if method == METHOD_SESSION_CLOSE:
                raise RuntimeError("engine refused the close")
            return {}

        send.side_effect = answer

        await handle.destroy()

        assert _sent(send) == [METHOD_SESSION_CLOSE, METHOD_KAS_SESSION_DELETE]
        assert [e["session_id"] for e in _entries()] == [_SID]

    @pytest.mark.asyncio
    async def test_a_runtime_that_died_leaves_the_entry_for_the_sweep(self, tmp_path, monkeypatch):
        """Neither the close nor the delete can be sent, and the record is still on disk."""
        handle, runtime, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0
        runtime._dead = True

        await handle.destroy()

        send.assert_not_awaited()
        assert [e["session_id"] for e in _entries()] == [_SID]
        assert _SID not in runtime._session_queues

    @pytest.mark.asyncio
    async def test_a_cancelled_destroy_still_closes_the_session(self, tmp_path, monkeypatch):
        """Cancellation is when teardown runs. A cancel while the entry is written must
        not leave the entry on disk and the session resident on the engine."""
        handle, runtime, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0
        entered, go_on = threading.Event(), threading.Event()
        schedule = released_sessions.schedule

        def slow_schedule(*args, **kwargs):
            entered.set()
            go_on.wait(default_timeout())
            schedule(*args, **kwargs)

        monkeypatch.setattr(released_sessions, "schedule", slow_schedule)

        destroying = asyncio.create_task(handle.destroy())
        try:
            await async_wait_until(entered.is_set, describe=lambda: "schedule not entered")
            destroying.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(destroying, what="the cancelled destroy")
        finally:
            go_on.set()
            if not destroying.done():
                destroying.cancel()
            await await_until(
                asyncio.gather(destroying, return_exceptions=True), what="the destroy settling"
            )
            if handle._release_task is not None:
                await await_until(
                    asyncio.gather(handle._release_task, return_exceptions=True),
                    what="the release task",
                )

        assert _sent(send) == [METHOD_SESSION_CLOSE]
        assert [e["session_id"] for e in _entries()] == [_SID]
        assert _SID not in runtime._session_queues

    @pytest.mark.asyncio
    async def test_a_second_destroy_does_not_release_again(self, tmp_path, monkeypatch):
        """Releasing again would write the entry with a later due time while a sweep may
        already be deleting the record by the first one."""
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0

        await handle.destroy()
        [first] = _entries()
        await handle.destroy()

        assert _sent(send) == [METHOD_SESSION_CLOSE]
        assert _entries() == [first]

    @pytest.mark.asyncio
    async def test_an_entry_that_cannot_be_stored_deletes_at_once(self, tmp_path, monkeypatch):
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch)
        handle.delete_delay_secs = 1800.0

        def refuse(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(released_sessions, "schedule", refuse)

        await handle.destroy()

        assert _sent(send) == [METHOD_KAS_SESSION_DELETE]

    @pytest.mark.asyncio
    async def test_kiro_ignores_the_delay(self, tmp_path, monkeypatch):
        handle, _runtime_, send = self._handle(tmp_path, monkeypatch, backend="")
        handle.delete_delay_secs = 1800.0

        await handle.destroy()

        assert _sent(send) == [METHOD_SESSION_TERMINATE]
        assert _entries() == []


class TestTheStore:
    def test_an_entry_is_due_only_once_its_time_has_come(self):
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=60, now=1000.0)
        assert released_sessions.due(now=1059.0) == []
        [entry] = released_sessions.due(now=1060.0)
        assert entry == released_sessions.ReleasedSession(_SID, ACP_BACKEND_KAS, 1060.0)
        released_sessions.forget(_SID)
        assert released_sessions.due(now=9999.0) == []

    @pytest.mark.parametrize("bad", ["../escape", ".hidden", "a/b", "", "x" * 200])
    def test_an_id_that_is_not_a_safe_file_name_is_refused(self, bad):
        with pytest.raises(ValueError):
            released_sessions.schedule(bad, backend=ACP_BACKEND_KAS, delay_secs=60)

    def test_entries_that_cannot_be_trusted_are_skipped_and_kept(self):
        directory = released_sessions.released_sessions_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.json").write_text("{not json")
        other = "sess_5b8e2d47-1c9a-4f3e-8d6b-2e7f9a0c4b1d"
        (directory / f"{other}.json").write_text(
            json.dumps({"session_id": _SID, "backend": ACP_BACKEND_KAS, "delete_at": 0})
        )
        assert released_sessions.due(now=9999.0) == []
        assert sorted(p.name for p in directory.iterdir()) == ["broken.json", f"{other}.json"]


class _BgHandle:
    def __init__(self, runtime):
        self._runtime = runtime
        self.destroyed = False

    async def destroy(self):
        self.destroyed = True


def _manager(runtime):
    handle = _BgHandle(runtime)
    return SimpleNamespace(get_bg_session=AsyncMock(return_value=handle)), handle


_NOW = 1_000_000.0


class TestTheSweep:
    @pytest.fixture(autouse=True)
    def _frozen_clock(self, _floor_monkeypatch):
        # The module's own clock, so whether an entry is due never depends on the wall
        # clock moving between two calls.
        _floor_monkeypatch.setattr(released_sessions, "time", SimpleNamespace(time=lambda: _NOW))

    @pytest.mark.asyncio
    async def test_due_records_are_deleted_and_their_entries_dropped(self, tmp_path, monkeypatch):
        later = "sess_8c1d4e6f-2a3b-4c5d-9e8f-7a6b5c4d3e2f"
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0)
        released_sessions.schedule(later, backend=ACP_BACKEND_KAS, delay_secs=3600)
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        send = AsyncMock(return_value={"success": True})
        monkeypatch.setattr(runtime, "_send_and_await", send)
        manager, handle = _manager(runtime)

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 1

        send.assert_awaited_once_with(
            METHOD_KAS_SESSION_DELETE, {"sessionId": _SID}, timeout=_TERMINATE_TIMEOUT
        )
        assert [e["session_id"] for e in _entries()] == [later]
        assert handle.destroyed is True

    @pytest.mark.asyncio
    async def test_a_failed_delete_keeps_the_entry_for_the_next_tick(self, tmp_path, monkeypatch):
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0)
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=RuntimeError("no")))
        manager, handle = _manager(runtime)

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 0

        assert [e["session_id"] for e in _entries()] == [_SID]
        assert handle.destroyed is True

    @pytest.mark.asyncio
    async def test_another_backend_leaves_the_entries_waiting(self, tmp_path):
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0)
        manager, _handle = _manager(_runtime(tmp_path, ""))

        assert await released_sessions.sweep(manager, "") == 0

        manager.get_bg_session.assert_not_awaited()
        assert [e["session_id"] for e in _entries()] == [_SID]

    @pytest.mark.asyncio
    async def test_a_runtime_on_another_backend_is_not_sent_the_delete(self, tmp_path, monkeypatch):
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0)
        runtime = _runtime(tmp_path, "")
        send = AsyncMock(return_value={})
        monkeypatch.setattr(runtime, "_send_and_await", send)
        manager, handle = _manager(runtime)

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 0

        send.assert_not_awaited()
        assert handle.destroyed is True
        assert [e["session_id"] for e in _entries()] == [_SID]

    @pytest.mark.asyncio
    async def test_nothing_due_needs_no_background_session(self, tmp_path):
        manager, _handle = _manager(_runtime(tmp_path, ACP_BACKEND_KAS))

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 0

        manager.get_bg_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_tick_sends_at_most_fifty(self, tmp_path, monkeypatch):
        """A backlog drains over several ticks instead of holding one."""
        for i in range(51):
            sid = f"sess_4d7e9f20-6b1a-4c3d-8e5f-{i:06d}abcdef"
            released_sessions.schedule(sid, backend=ACP_BACKEND_KAS, delay_secs=0)
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(return_value={"success": True}))
        manager, _handle = _manager(runtime)

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 50
        assert len(_entries()) == 1
        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 1
        assert _entries() == []

    @pytest.mark.asyncio
    async def test_an_engine_that_stops_answering_holds_a_tick_only_for_the_budget(
        self, tmp_path, monkeypatch
    ):
        """Each unanswered delete waits the teardown timeout, and the hooks after this
        one in the tick wait with it, so sending stops once the budget is spent."""
        for i in range(released_sessions._SWEEP_BATCH):
            sid = f"sess_4d7e9f20-6b1a-4c3d-8e5f-{i:06d}abcdef"
            released_sessions.schedule(sid, backend=ACP_BACKEND_KAS, delay_secs=0)
        clock = [0.0]
        monkeypatch.setattr(released_sessions, "_monotonic", lambda: clock[0])
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)

        async def unanswered(*_a, **_k):
            clock[0] += _TERMINATE_TIMEOUT
            raise asyncio.TimeoutError

        send = AsyncMock(side_effect=unanswered)
        monkeypatch.setattr(runtime, "_send_and_await", send)
        manager, handle = _manager(runtime)

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 0

        budget = released_sessions._SWEEP_BUDGET_SECS
        assert send.await_count == math.ceil(budget / _TERMINATE_TIMEOUT)
        assert len(_entries()) == released_sessions._SWEEP_BATCH
        assert handle.destroyed is True

    @pytest.mark.asyncio
    async def test_a_refused_delete_does_not_hold_back_the_rest(self, tmp_path, monkeypatch):
        """The batch is earliest first, so an entry the engine keeps refusing must not
        stop the ones after it on every tick."""
        refused = "sess_a9e3c7b1-5d2f-4a8e-b6c4-0f1e2d3c4b5a"
        released_sessions.schedule(refused, backend=ACP_BACKEND_KAS, delay_secs=0, now=_NOW - 60)
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0)
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)

        async def answer(_method, params, **_k):
            if params["sessionId"] == refused:
                raise RuntimeError("refused")
            return {"success": True}

        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=answer))
        manager, _handle = _manager(runtime)

        assert await released_sessions.sweep(manager, ACP_BACKEND_KAS) == 1

        assert [e["session_id"] for e in _entries()] == [refused]

    @pytest.mark.asyncio
    async def test_records_left_on_a_backend_no_longer_selected_are_reported_once(
        self, tmp_path, caplog
    ):
        """They are deleted only while their backend is selected, so after a lasting
        switch the operator is told, once, how to delete them."""
        stale = _NOW - released_sessions._STRANDED_GRACE_SECS - 1
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0, now=stale)
        manager, _handle = _manager(_runtime(tmp_path, ""))

        with caplog.at_level(logging.WARNING, logger=released_sessions.__name__):
            assert await released_sessions.sweep(manager, "") == 0
            assert await released_sessions.sweep(manager, "") == 0

        reports = [r.getMessage() for r in caplog.records if r.name == released_sessions.__name__]
        assert len(reports) == 1
        assert f"1 record(s) released on the {ACP_BACKEND_KAS} backend" in reports[0]
        assert f"kirocrew config set agent.acp_backend {ACP_BACKEND_KAS}" in reports[0]
        assert [e["session_id"] for e in _entries()] == [_SID]

    @pytest.mark.asyncio
    async def test_a_short_switch_away_is_not_reported(self, tmp_path, caplog):
        recent = _NOW - released_sessions._STRANDED_GRACE_SECS + 60
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0, now=recent)
        manager, _handle = _manager(_runtime(tmp_path, ""))

        with caplog.at_level(logging.WARNING, logger=released_sessions.__name__):
            assert await released_sessions.sweep(manager, "") == 0

        assert [r for r in caplog.records if r.name == released_sessions.__name__] == []

    @pytest.mark.asyncio
    async def test_an_entry_written_again_during_the_sweep_is_kept(self, tmp_path, monkeypatch):
        """The sweep drops only the entry it read, never one written after its reading."""
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=0)
        runtime = _runtime(tmp_path, ACP_BACKEND_KAS)

        async def answer(*_a, **_k):
            released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=60)
            return {"success": True}

        monkeypatch.setattr(runtime, "_send_and_await", AsyncMock(side_effect=answer))
        manager, _handle = _manager(runtime)

        await released_sessions.sweep(manager, ACP_BACKEND_KAS)

        [entry] = _entries()
        assert entry["delete_at"] == _NOW + 60


class TestTheStoreIsProtected:
    """An entry has the gateway delete the record it names, so it is registered
    in the four places ``panel-dismissals`` is. Nothing in the code fails when one
    is missing -- it just leaves the store writable -- so each is pinned by name."""

    def test_the_leaf_is_a_direct_child_of_the_data_home(self):
        from kiro_crew.config.paths import data_home

        directory = released_sessions.released_sessions_dir()
        assert directory.parent == data_home()
        assert "trust" not in directory.parts

    def test_the_leaf_is_on_the_file_gates_sensitive_floor(self):
        from kiro_crew.security import paths as security_paths

        floor = security_paths._CREW_SECRET_LEAVES
        assert "panel-dismissals" in floor, "not the floor list this test means to read"
        assert released_sessions._RELEASED_SESSIONS_LEAF in floor

    def test_the_leaf_is_bind_masked_precreated_and_refuses_a_link(self):
        from kiro_crew import sandbox

        leaf = released_sessions._RELEASED_SESSIONS_LEAF
        assert leaf in sandbox._CREW_HIDDEN_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert leaf in sandbox._CREW_NO_ALIAS_LEAVES


class TestTheCleanupLoopRunsTheSweep:
    """The sweep is a cleanup-loop hook, and the loop otherwise starts only with the
    first ordinary allocation."""

    @staticmethod
    def _manager():
        from kiro_crew.session import SessionManager

        return SessionManager.__new__(SessionManager)

    @staticmethod
    def _hook(manager):
        watchdog = manager._cleanup_boundary().state.watchdog
        [hook] = [h for h in watchdog._hooks if h.name == "released_sessions"]
        return hook

    @pytest.mark.asyncio
    async def test_a_cleanup_tick_runs_the_sweep(self, monkeypatch):
        manager = self._manager()
        manager._cfg = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_KAS))
        sweep = AsyncMock(return_value=0)
        monkeypatch.setattr(released_sessions, "sweep", sweep)

        await self._hook(manager).run()

        sweep.assert_awaited_once_with(manager, ACP_BACKEND_KAS)

    @pytest.mark.asyncio
    async def test_nothing_is_swept_while_admission_is_closed(self, monkeypatch):
        manager = self._manager()
        manager._cfg = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_KAS))
        manager._closing = True
        sweep = AsyncMock(return_value=0)
        monkeypatch.setattr(released_sessions, "sweep", sweep)

        await self._hook(manager).run()

        sweep.assert_not_awaited()

    def _starting(self):
        manager = self._manager()
        manager._pool = SimpleNamespace(start_pool=AsyncMock())
        manager._background_tasks = set()
        manager._ensure_cleanup_task = MagicMock()
        return manager

    @pytest.mark.asyncio
    async def test_a_new_process_starts_the_loop_for_waiting_entries(self):
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=60)
        manager = self._starting()

        await manager.start_pool()

        manager._ensure_cleanup_task.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_non_blocking_start_leaves_the_check_to_a_background_task(self):
        """A non-blocking start returns before its housekeeping, so the check runs
        after it, in a task the manager tracks."""
        released_sessions.schedule(_SID, backend=ACP_BACKEND_KAS, delay_secs=60)
        manager = self._starting()

        await manager.start_pool(blocking=False)

        manager._ensure_cleanup_task.assert_not_called()
        [task] = manager._background_tasks
        await await_until(task, what="the check for waiting deletes")
        manager._ensure_cleanup_task.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_without_waiting_entries_the_first_allocation_starts_it(self):
        manager = self._starting()

        await manager.start_pool()

        manager._ensure_cleanup_task.assert_not_called()
