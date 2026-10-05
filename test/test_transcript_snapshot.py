"""A consistent transcript snapshot, as its three readers observe it.

The fork, the transfer bundle and the bounded slot-detail page each read a live
slot's durable rows off the event loop and pair them with window rows that are not
on disk yet. Every scenario here drives one of them over a real ``ConversationLog``
under ``tmp_path`` and lands a slot mutation INSIDE the threaded read through
``_ReadHook``, which runs it on the event loop while the worker thread waits for it.
Nothing sleeps: the read cannot return before the mutation has run, so each retry
and each refusal is reached on the same call every run.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Callable

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import session_transfer as st
from kiro_crew.dashboard import transcript_snapshot
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.slot_persistence import write_guards
from kiro_crew.dashboard.transcript_snapshot import (
    FORK,
    PAGE,
    SNAPSHOT_ATTEMPTS,
    TRANSFER,
    RetryRead,
    SlotView,
    SnapshotUnstable,
    read_consistent_transcript,
)

#: Bounds the worker thread's wait for its mutation to finish on the loop. A
#: mutation is a handful of attribute writes, so this is a hang guard only.
_MUTATION_TIMEOUT_S = 30


class _ReadHook:
    """Count calls to one ``ConversationLog`` read and mutate the slot inside them.

    The real read runs first, so its result predates the mutation; the mutation
    then runs on the event loop, and only after it finishes does the read return
    to its caller. That is the interleaving a periodic flush, an append or a
    rewind produces when it lands while the snapshot is off the loop. *on_loop*
    counts reads made on the event loop's own thread, which a reader must never
    make: a blocking read there stalls every task, the heartbeat included.

    Build it on the event loop, inside the test coroutine.
    """

    def __init__(self, monkeypatch, log, method: str, loop: asyncio.AbstractEventLoop):
        self.calls = 0
        self.on_loop = 0
        self._loop = loop
        self._loop_thread = threading.get_ident()
        self._by_call: dict[int, Callable[[], None]] = {}
        self._every: Callable[[int], None] | None = None
        original = getattr(log, method)

        def _read(*args, **kwargs):
            self.calls += 1
            if threading.get_ident() == self._loop_thread:
                self.on_loop += 1
            result = original(*args, **kwargs)
            mutation = self._mutation_for(self.calls)
            if mutation is not None:
                asyncio.run_coroutine_threadsafe(_on_loop(mutation), self._loop).result(
                    timeout=_MUTATION_TIMEOUT_S
                )
            return result

        monkeypatch.setattr(log, method, _read)

    def on_call(self, n: int, mutation: Callable[[], None]) -> None:
        self._by_call[n] = mutation

    def on_every_call(self, mutation: Callable[[int], None]) -> None:
        self._every = mutation

    def _mutation_for(self, n: int) -> Callable[[], None] | None:
        if n in self._by_call:
            return self._by_call[n]
        every = self._every
        if every is not None:
            return lambda: every(n)
        return None


async def _on_loop(mutation: Callable[[], None]) -> None:
    mutation()


def _persisted_slot(state, name: str, contents: list[str]):
    """A slot whose window is saved and clean, alternating user and assistant rows."""
    slot = state.get_or_create_slot(name)
    for i, text in enumerate(contents):
        role = "user" if i % 2 == 0 else "assistant"
        slot.append(role, text, "msg msg-u" if role == "user" else "msg msg-a")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    slot._dirty = False
    return slot


def _contents(rows) -> list[str]:
    return [m.get("content", "") for m in rows if m.get("role") in ("user", "assistant")]


def _disk(state, key: str) -> list[str]:
    return _contents(state.conversation_log.read_messages_chained(key))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


async def _fork(state, name: str) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{name}/fork", json={"prompt": "forked"})
        return resp.status, await resp.json()


def _child(state, payload: dict) -> list[str]:
    return _contents(state._slots[payload["key"]].messages)


# ── the fork ─────────────────────────────────────────────────────────────────


class TestForkSnapshot:
    @pytest.mark.asyncio
    async def test_the_unpersisted_tail_is_merged_once(self, state, monkeypatch):
        slot = _persisted_slot(state, "f-tail", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        status, payload = await _fork(state, "f-tail")
        assert (hook.calls, hook.on_loop) == (1, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "u2"]

    @pytest.mark.asyncio
    async def test_a_flush_landing_during_the_read_is_re_read_not_duplicated(
        self, state, monkeypatch
    ):
        slot = _persisted_slot(state, "f-flush", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: state.flush_slot_now(slot))

        status, payload = await _fork(state, "f-flush")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "u2"]

    @pytest.mark.asyncio
    async def test_a_message_landing_during_the_read_reaches_the_fork(self, state, monkeypatch):
        _persisted_slot(state, "f-late", ["u1", "a1"])
        slot = state._slots["f-late"]
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: slot.append("user", "late", "msg msg-u"))

        status, payload = await _fork(state, "f-late")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "late"]

    @pytest.mark.asyncio
    async def test_a_frozen_prefix_move_during_the_read_forces_a_re_read(self, state, monkeypatch):
        slot = _persisted_slot(state, "f-older", ["u1", "a1", "u2"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        def _move_prefix() -> None:
            slot._disk_older_count += 1

        hook.on_call(1, _move_prefix)

        status, payload = await _fork(state, "f-older")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "u2"]

    @pytest.mark.asyncio
    async def test_a_save_completing_during_the_read_is_re_read(self, state, monkeypatch):
        """Only ``_dirty`` moves: an in-place edit whose save lands under the read."""
        slot = _persisted_slot(state, "f-edit", ["u1", "a1-old"])
        slot.messages[1]["content"] = "a1-new"
        slot._dirty = True
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: state.flush_slot_now(slot))

        status, payload = await _fork(state, "f-edit")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1-new"]

    @pytest.mark.asyncio
    async def test_a_pending_rewrite_is_saved_before_the_read(self, state, monkeypatch):
        slot = _persisted_slot(state, "f-rewind", ["u1", "a1", "u2", "a2", "u3", "a3"])
        del slot.messages[4:]
        slot._resumed_count = 0
        slot._pending_rewrite = True
        slot._dirty = True
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        status, payload = await _fork(state, "f-rewind")
        assert (hook.calls, hook.on_loop) == (1, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "u2", "a2"]
        assert slot._pending_rewrite is False
        assert _disk(state, "dashboard:f-rewind") == ["u1", "a1", "u2", "a2"]

    @pytest.mark.asyncio
    async def test_a_rewind_landing_during_the_read_is_saved_then_re_read(self, state, monkeypatch):
        slot = _persisted_slot(state, "f-midrewind", ["u1", "a1", "u2", "a2"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        def _rewind() -> None:
            del slot.messages[2:]
            slot._resumed_count = 0
            slot._pending_rewrite = True
            slot._dirty = True

        hook.on_call(1, _rewind)

        status, payload = await _fork(state, "f-midrewind")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1"]
        assert _disk(state, "dashboard:f-midrewind") == ["u1", "a1"]

    @pytest.mark.asyncio
    async def test_a_pending_rewrite_of_a_deleted_session_refuses_the_fork(
        self, state, monkeypatch
    ):
        slot = _persisted_slot(state, "f-gone", ["u1", "a1", "u2", "a2"])
        assert state.conversation_log.delete_session("dashboard:f-gone") is True
        del slot.messages[2:]
        slot._pending_rewrite = True
        slot._dirty = True
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        status, payload = await _fork(state, "f-gone")
        assert (hook.calls, hook.on_loop) == (0, 0)

        assert status == 409, payload
        assert payload == {
            "error": "the source session could not be saved; retry the fork",
            "code": "fork_source_deleted",
        }

    @pytest.mark.asyncio
    async def test_a_slot_that_never_settles_is_refused_after_the_budget(self, state, monkeypatch):
        slot = _persisted_slot(state, "f-churn", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )
        hook.on_every_call(lambda n: slot.append("user", f"late-{n}", "msg msg-u"))

        status, payload = await _fork(state, "f-churn")
        assert (hook.calls, hook.on_loop) == (4, 0)

        assert status == 503, payload
        assert payload == {
            "error": "the source session is being written to; please retry",
            "code": "fork_snapshot_unstable",
        }

    @pytest.mark.asyncio
    async def test_a_capped_restore_merges_from_the_restore_point_without_saving(
        self, state, monkeypatch
    ):
        rows = [f"r{i}" for i in range(10)]
        slot = _persisted_slot(state, "f-capped", rows)
        del slot.messages[:6]
        slot._resumed_count = len(slot.messages)
        slot.append("user", "new1", "msg msg-u")
        slot.append("assistant", "new2", "msg msg-a")
        slot._disk_window_len = 10
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        status, payload = await _fork(state, "f-capped")
        assert (hook.calls, hook.on_loop) == (1, 0)

        assert status == 200, payload
        assert _child(state, payload) == rows + ["new1", "new2"]
        assert _disk(state, "dashboard:f-capped") == rows
        assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_a_boundary_ahead_of_a_shrunken_window_is_flushed_then_re_read(
        self, state, monkeypatch
    ):
        slot = _persisted_slot(state, "f-ahead", ["u1", "a1", "u2"])
        slot.append("assistant", "a2", "msg msg-a")
        slot._disk_window_len = len(slot.messages) + 1
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained",
            asyncio.get_running_loop(),
        )

        status, payload = await _fork(state, "f-ahead")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "u2", "a2"]
        assert _disk(state, "dashboard:f-ahead") == ["u1", "a1", "u2", "a2"]


# ── the transfer bundle ──────────────────────────────────────────────────────


def _bundle_contents(bundle) -> list[str]:
    return [m["content"] for m in bundle["messages"]]


class TestTransferSnapshot:
    @pytest.mark.asyncio
    async def test_a_dirty_slot_is_flushed_and_bundled_from_disk(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-dirty", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        bundle = await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (1, 0)

        assert _bundle_contents(bundle) == ["u1", "a1", "u2"]
        assert _disk(state, "dashboard:t-dirty") == ["u1", "a1", "u2"]

    @pytest.mark.asyncio
    async def test_a_message_landing_during_the_read_is_re_read(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-late", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: slot.append("user", "late", "msg msg-u"))

        bundle = await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert _bundle_contents(bundle) == ["u1", "a1", "late"]

    @pytest.mark.asyncio
    async def test_a_flush_landing_during_the_read_is_re_read(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-flush", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        def _append_and_flush() -> None:
            slot.append("user", "late", "msg msg-u")
            state.flush_slot_now(slot)

        hook.on_call(1, _append_and_flush)

        bundle = await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert _bundle_contents(bundle) == ["u1", "a1", "late"]

    @pytest.mark.asyncio
    async def test_a_pending_rewrite_is_refused_before_any_read(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-rewind", ["u1", "a1"])
        slot._pending_rewrite = True
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (0, 0)

        assert str(raised.value) == "a pending rewrite means the on-disk transcript is stale"

    @pytest.mark.asyncio
    async def test_a_rewind_landing_during_the_read_is_refused(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-midrewind", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        def _rewind() -> None:
            slot._pending_rewrite = True

        hook.on_call(1, _rewind)

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (1, 0)

        assert str(raised.value) == "a pending rewrite means the on-disk transcript is stale"

    @pytest.mark.asyncio
    async def test_a_boundary_ahead_of_the_window_is_refused(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-ahead", ["u1", "a1"])
        slot._disk_window_len = len(slot.messages) + 1
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (0, 0)

        assert str(raised.value) == (
            "the persisted boundary is ahead of the resident window (a flush landed mid-stream)"
        )

    @pytest.mark.asyncio
    async def test_a_deleted_session_is_refused(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-gone", ["u1", "a1"])
        assert state.conversation_log.delete_session("dashboard:t-gone") is True
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (0, 0)

        assert str(raised.value) == "the session was permanently deleted"

    @pytest.mark.asyncio
    async def test_a_dirty_slot_whose_session_was_deleted_is_refused_by_its_flush(
        self, state, monkeypatch
    ):
        slot = _persisted_slot(state, "t-flushgone", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        assert state.conversation_log.delete_session("dashboard:t-flushgone") is True
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (0, 0)

        assert str(raised.value) == "the session was permanently deleted"

    @pytest.mark.asyncio
    async def test_a_delete_landing_during_the_read_is_refused(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-middelete", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: state.conversation_log.delete_session("dashboard:t-middelete"))

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (1, 0)

        assert str(raised.value) == "the session was permanently deleted"

    @pytest.mark.asyncio
    async def test_a_slot_that_never_settles_is_refused_after_the_budget(self, state, monkeypatch):
        slot = _persisted_slot(state, "t-churn", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )
        hook.on_every_call(lambda n: slot.append("user", f"late-{n}", "msg msg-u"))

        with pytest.raises(st.SnapshotUnstable) as raised:
            await st.build_transfer_bundle_async(state, slot, origin="here")
        assert (hook.calls, hook.on_loop) == (4, 0)

        assert str(raised.value) == "transcript snapshot did not settle in 4 attempts"

    @pytest.mark.asyncio
    async def test_a_retried_bundle_releases_the_layer_b_snapshot_it_will_not_ship(
        self, state, monkeypatch, tmp_path
    ):
        """A spent attempt's staged Layer B copy is removed; only the shipped one stays."""
        sid = "dddddddd-1111-2222-3333-444444444444"
        sessions = tmp_path / "kiro" / "sessions" / "cli"
        sessions.mkdir(parents=True)
        (sessions / f"{sid}.json").write_text(json.dumps({"session_id": sid}), encoding="utf-8")
        (sessions / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
        monkeypatch.setattr(state.sessions, "resumable_sid", lambda _key: sid)
        slot = _persisted_slot(state, "t-layerb", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: slot.append("user", "late", "msg msg-u"))

        bundle = await st.build_transfer_bundle_async(state, slot, origin="here")
        try:
            assert hook.calls == 2
            shipped = bundle["layer_b"]["events"].path
            assert sorted(st._egress_tmp_dir().iterdir()) == [shipped]
        finally:
            st.release_bundle_files(bundle)


# ── the bounded slot-detail page ─────────────────────────────────────────────


async def _page(state, name: str, limit: int = 50) -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{name}?limit={limit}")
        assert resp.status == 200, await resp.text()
        return await resp.json()


class TestPageSnapshot:
    @pytest.mark.asyncio
    async def test_a_page_pairs_disk_with_the_unflushed_window(self, state, monkeypatch):
        slot = _persisted_slot(state, "p-tail", ["u1", "a1", "u2"])
        slot.append("assistant", "a2", "msg msg-a")
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained_page",
            asyncio.get_running_loop(),
        )

        page = await _page(state, "p-tail")
        assert (hook.calls, hook.on_loop) == (2, 0)

        assert _contents(page["messages"]) == ["u1", "a1", "u2", "a2"]
        assert (page["total"], page["has_more"]) == (4, False)

    @pytest.mark.asyncio
    async def test_a_message_landing_during_the_read_recomposes_the_page(self, state, monkeypatch):
        slot = _persisted_slot(state, "p-late", ["u1", "a1", "u2"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained_page",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: slot.append("assistant", "late", "msg msg-a"))

        page = await _page(state, "p-late")
        assert (hook.calls, hook.on_loop) == (4, 0)

        assert _contents(page["messages"]) == ["u1", "a1", "u2", "late"]

    @pytest.mark.asyncio
    async def test_a_foreign_append_during_the_read_recomposes_the_page(self, state, monkeypatch):
        _persisted_slot(state, "p-foreign", ["u1", "a1", "u2"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained_page",
            asyncio.get_running_loop(),
        )
        hook.on_call(
            1,
            lambda: state.conversation_log.append("dashboard:p-foreign", "assistant", "foreign"),
        )

        page = await _page(state, "p-foreign")
        assert (hook.calls, hook.on_loop) == (4, 0)

        assert _contents(page["messages"]) == ["u1", "a1", "u2", "foreign"]

    @pytest.mark.asyncio
    async def test_a_slot_that_never_settles_is_served_by_the_full_reader(self, state, monkeypatch):
        slot = _persisted_slot(state, "p-churn", ["u1", "a1", "u2"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "read_messages_chained_page",
            asyncio.get_running_loop(),
        )
        hook.on_every_call(lambda n: slot.append("assistant", f"late-{n}", "msg msg-a"))

        page = await _page(state, "p-churn")
        assert (hook.calls, hook.on_loop) == (8, 0)

        assert _contents(page["messages"]) == [
            "u1",
            "a1",
            "u2",
            "late-1",
            "late-2",
            "late-3",
            "late-4",
            "late-5",
            "late-6",
            "late-7",
            "late-8",
        ]

    @pytest.mark.asyncio
    async def test_a_read_failure_that_is_also_a_value_error_is_answered_not_retried(
        self, state, monkeypatch
    ):
        """``io.UnsupportedOperation`` is an OSError AND a ValueError: the 503 wins."""
        _persisted_slot(state, "p-unsupported", ["u1", "a1"])
        log = state.conversation_log
        reads = []

        def _unsupported(*_args, **_kwargs):
            reads.append(1)
            raise io.UnsupportedOperation("not seekable")

        monkeypatch.setattr(log, "read_messages_chained_page", _unsupported)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/p-unsupported?limit=10")
            body = await resp.json()

        assert (resp.status, body.get("code"), len(reads)) == (503, "history_corpus_unreadable", 1)


class TestTheLogLines:
    """The lines an operator greps keep their logger, level and text."""

    @pytest.mark.asyncio
    async def test_a_spent_fork_attempt_logs_its_retry(self, state, monkeypatch, caplog):
        slot = _persisted_slot(state, "l-fork", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch, state.conversation_log, "read_messages_chained", asyncio.get_running_loop()
        )
        hook.on_call(1, lambda: slot.append("user", "late", "msg msg-u"))

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.dashboard.chat_fork"):
            await _fork(state, "l-fork")

        assert [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.name == "kiro_crew.dashboard.chat_fork" and "transcript read" in r.getMessage()
        ] == [
            (logging.DEBUG, "chat_fork: slot=l-fork changed during the transcript read; retrying")
        ]

    @pytest.mark.asyncio
    async def test_a_spent_transfer_attempt_logs_its_retry(self, state, monkeypatch, caplog):
        slot = _persisted_slot(state, "l-xfer", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )
        hook.on_call(1, lambda: slot.append("user", "late", "msg msg-u"))

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.dashboard.session_transfer"):
            await st.build_transfer_bundle_async(state, slot, origin="here")

        assert [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.name == "kiro_crew.dashboard.session_transfer"
            and "transcript read" in r.getMessage()
        ] == [
            (
                logging.DEBUG,
                "session_transfer: slot l-xfer flushed during the transcript read; retrying",
            )
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("during_read", "line"),
        [
            (
                False,
                "session_transfer: slot=l-gone belongs to a permanently deleted session; "
                "refusing the transfer",
            ),
            (
                True,
                "session_transfer: slot=l-gone was permanently deleted during bundle assembly; "
                "refusing the transfer",
            ),
        ],
        ids=["before-the-read", "during-the-read"],
    )
    async def test_a_deleted_session_logs_its_refusal(
        self, state, monkeypatch, caplog, during_read, line
    ):
        slot = _persisted_slot(state, "l-gone", ["u1", "a1"])
        hook = _ReadHook(
            monkeypatch,
            state.conversation_log,
            "derive_messages_chained_with_keys",
            asyncio.get_running_loop(),
        )
        if during_read:
            hook.on_call(1, lambda: state.conversation_log.delete_session("dashboard:l-gone"))
        else:
            assert state.conversation_log.delete_session("dashboard:l-gone") is True

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.session_transfer"):
            with pytest.raises(st.SnapshotUnstable):
                await st.build_transfer_bundle_async(state, slot, origin="here")

        assert [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.name == "kiro_crew.dashboard.session_transfer"
        ] == [(logging.WARNING, line)]

    @pytest.mark.asyncio
    async def test_a_page_whose_read_keeps_failing_warns_once_then_falls_back(
        self, state, monkeypatch, caplog
    ):
        _persisted_slot(state, "l-page", ["u1", "a1"])

        def _transient(*_args, **_kwargs):
            raise OSError("transient")

        monkeypatch.setattr(state.conversation_log, "read_messages_chained_page", _transient)
        handlers_logger = "kiro_crew.dashboard.chat_handlers"

        with caplog.at_level(logging.DEBUG, logger=handlers_logger):
            page = await _page(state, "l-page", limit=10)

        assert _contents(page["messages"]) == ["u1", "a1"]
        key = "dashboard:l-page"
        assert [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.name == handlers_logger and "bounded slot history read failed" in r.getMessage()
        ] == [
            (logging.DEBUG, f"bounded slot history read failed for {key} (attempt 1/4); retrying"),
            (logging.DEBUG, f"bounded slot history read failed for {key} (attempt 2/4); retrying"),
            (logging.DEBUG, f"bounded slot history read failed for {key} (attempt 3/4); retrying"),
            (
                logging.WARNING,
                f"bounded slot history read failed for {key} (attempt 4/4); "
                "falling back to the full reader",
            ),
        ]


# ── the interface: read_consistent_transcript ────────────────────────────────
#
# The read callable IS the seam, so these tests mutate the slot inside it, on the
# loop, between the observation and the re-check -- exactly where a concurrent
# writer lands -- and need no thread at all.


class _Reader:
    """A read callable that records each attempt's view and lands *mutations* in it."""

    def __init__(self, *mutations: Callable[[], None] | None, result: object = "rows"):
        self.views: list[SlotView] = []
        self._mutations = list(mutations)
        self._result = result

    async def __call__(self, view: SlotView):
        self.views.append(view)
        if len(self.views) <= len(self._mutations):
            mutation = self._mutations[len(self.views) - 1]
            if mutation is not None:
                mutation()
        return self._result


class _Saves:
    """``persist`` / ``rewrite`` callables that record calls and can land a mutation."""

    def __init__(self, during: Callable[[], None] | None = None, *, then=None):
        self.calls = 0
        self._during = during
        self._then = then

    async def __call__(self):
        self.calls += 1
        if self.calls == 1 and self._during is not None:
            self._during()
        if self._then is not None:
            self._then()


def _bump(slot, field: str) -> Callable[[], None]:
    def _move() -> None:
        if field == "boundary":
            # A flush completing: the boundary advances inside the window.
            slot._disk_window_len += 1
        elif field == "generation":
            slot._dirty_gen += 1
        elif field == "length":
            slot.messages.append({"role": "user", "content": "more", "ts": ""})
        elif field == "older":
            slot._disk_older_count += 1
        elif field == "durable_older":
            slot._disk_older_durable_count += 1
        elif field == "dirty":
            slot._dirty_flag = not slot._dirty_flag
        else:
            raise AssertionError(field)

    return _move


_ALL_FIELDS = ("boundary", "generation", "length", "older", "durable_older", "dirty")


class TestTheObservation:
    """The witness is the observation the tail was cut from, not a second read of it.

    A worker-thread save stores ``_disk_window_len`` with no regard for the event
    loop, so it can land between two synchronous statements of an attempt. The
    store is run synchronously right after the real observation to land it at that
    point deterministically.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("store", ["save", "flush"])
    async def test_a_save_landing_right_after_the_observation_is_re_read(
        self, state, monkeypatch, store
    ):
        slot = _persisted_slot(state, f"o-{store}", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        real_observe = transcript_snapshot._observe
        observed: list[int] = []

        def _observe_then_store(slot_, purpose, attempt):
            view = real_observe(slot_, purpose, attempt)
            observed.append(attempt)
            if purpose is FORK and attempt == 1:
                if store == "save":
                    _save_slot_to_history(state, slot_, force=True)
                else:
                    state.flush_slot_now(slot_)
            return view

        monkeypatch.setattr(transcript_snapshot, "_observe", _observe_then_store)

        status, payload = await _fork(state, f"o-{store}")

        assert status == 200, payload
        assert _child(state, payload) == ["u1", "a1", "u2"]
        assert observed == [1, 2]


class TestTheBudget:
    def test_one_budget_is_shared_with_the_saves_own_pair_retries(self):
        assert SNAPSHOT_ATTEMPTS == write_guards._FLUSH_SNAPSHOT_RETRIES == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize("purpose", [FORK, TRANSFER, PAGE], ids=["fork", "transfer", "page"])
    async def test_a_slot_that_never_settles_spends_every_attempt_then_refuses(
        self, state, purpose
    ):
        slot = _persisted_slot(state, "i-churn", ["u1", "a1"])
        reader = _Reader(*[_bump(slot, "generation")] * SNAPSHOT_ATTEMPTS)

        with pytest.raises(SnapshotUnstable) as raised:
            await read_consistent_transcript(
                state, slot, purpose, reader, persist=_Saves(), rewrite=_Saves()
            )

        assert str(raised.value) == "transcript snapshot did not settle in 4 attempts"
        assert [v.attempt for v in reader.views] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_a_read_that_asks_to_go_again_spends_its_attempt(self, state):
        slot = _persisted_slot(state, "i-again", ["u1", "a1"])
        asked = []

        async def _read(view):
            asked.append(view.attempt)
            if view.attempt < 3:
                raise RetryRead
            return "page"

        snapshot = await read_consistent_transcript(state, slot, PAGE, _read)

        assert (snapshot.result, asked) == ("page", [1, 2, 3])


class TestTheWitness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("purpose", "held"),
        [
            (FORK, {"boundary", "generation", "length", "older", "dirty"}),
            (TRANSFER, {"generation", "boundary", "length"}),
            (PAGE, {"generation", "older", "durable_older"}),
        ],
        ids=["fork", "transfer", "page"],
    )
    @pytest.mark.parametrize("field", _ALL_FIELDS)
    async def test_only_the_purposes_own_fields_must_hold_still(self, state, purpose, held, field):
        slot = _persisted_slot(state, f"i-{field}", ["u1", "a1", "u2"])
        # One row past the boundary, so a flush can advance it within the window.
        slot._disk_window_len = 2
        reader = _Reader(_bump(slot, field))

        snapshot = await read_consistent_transcript(
            state, slot, purpose, reader, persist=_Saves(), rewrite=_Saves()
        )

        assert len(reader.views) == (2 if field in held else 1)
        assert snapshot.view is reader.views[-1]

    @pytest.mark.asyncio
    async def test_a_result_the_snapshot_will_not_return_is_discarded(self, state):
        slot = _persisted_slot(state, "i-discard", ["u1", "a1"])
        reader = _Reader(_bump(slot, "length"), result=object())
        discarded = []

        snapshot = await read_consistent_transcript(
            state, slot, TRANSFER, reader, persist=_Saves(), discard=discarded.append
        )

        assert discarded == [snapshot.result]


class TestTheTail:
    @pytest.mark.asyncio
    async def test_the_tail_is_the_window_past_the_persisted_boundary(self, state):
        slot = _persisted_slot(state, "i-tail", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")

        snapshot = await read_consistent_transcript(
            state, slot, FORK, _Reader(), persist=_Saves(), rewrite=_Saves()
        )

        assert _contents(snapshot.tail) == ["u2"]
        assert snapshot.disk_holds_unrepresented is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("purpose", [FORK, TRANSFER], ids=["fork", "transfer"])
    async def test_a_clean_window_past_the_boundary_is_still_owed(self, state, purpose):
        """The boundary alone decides: a flush that cleared ``_dirty`` owes the rest."""
        slot = _persisted_slot(state, "i-clean-tail", ["u1", "a1", "u2"])
        slot._disk_window_len = 2

        snapshot = await read_consistent_transcript(
            state, slot, purpose, _Reader(), persist=_Saves(), rewrite=_Saves()
        )

        assert _contents(snapshot.tail) == ["u2"]

    @pytest.mark.asyncio
    async def test_the_page_takes_no_tail(self, state):
        slot = _persisted_slot(state, "i-notail", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")

        snapshot = await read_consistent_transcript(state, slot, PAGE, _Reader())

        assert snapshot.tail == []

    @pytest.mark.asyncio
    async def test_a_boundary_ahead_of_a_clean_window_has_nothing_owed(self, state):
        slot = _persisted_slot(state, "i-clean-ahead", ["u1", "a1"])
        slot._disk_window_len = 5

        snapshot = await read_consistent_transcript(
            state, slot, FORK, _Reader(), persist=_Saves(), rewrite=_Saves()
        )

        assert snapshot.tail == []

    @pytest.mark.asyncio
    async def test_disk_longer_than_the_counters_is_a_capped_restore(self, state):
        slot = _persisted_slot(state, "i-capped", ["u1", "a1"])
        slot._resumed_count = 1
        slot.append("user", "u2", "msg msg-u")
        slot._disk_window_len = 9
        persist = _Saves()
        rows = [{"role": "user", "content": f"r{i}"} for i in range(5)]

        snapshot = await read_consistent_transcript(
            state, slot, FORK, _Reader(result=rows), persist=persist, rewrite=_Saves()
        )

        assert snapshot.disk_holds_unrepresented is True
        assert _contents(snapshot.tail) == ["a1", "u2"]
        assert persist.calls == 0

    @pytest.mark.asyncio
    async def test_counters_that_agree_with_disk_are_re_synced_then_re_read(self, state):
        slot = _persisted_slot(state, "i-resync", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        slot._disk_window_len = 9

        def _resynced() -> None:
            slot._disk_window_len = len(slot.messages)

        persist = _Saves(then=_resynced)
        rows = [{"role": "user", "content": "r0"}]
        reader = _Reader(result=rows)
        discarded: list = []

        snapshot = await read_consistent_transcript(
            state, slot, FORK, reader, persist=persist, rewrite=_Saves(), discard=discarded.append
        )

        assert (persist.calls, len(reader.views)) == (1, 2)
        assert snapshot.tail == [] and snapshot.disk_holds_unrepresented is False
        # The first read was spent on the re-sync, so it is released, not returned.
        assert discarded == [rows]


class TestAPendingRewrite:
    @pytest.mark.asyncio
    async def test_the_fork_saves_it_and_reads_after(self, state):
        slot = _persisted_slot(state, "i-save", ["u1", "a1"])
        slot._pending_rewrite = True

        def _saved() -> None:
            slot._pending_rewrite = False

        rewrite = _Saves(then=_saved)
        reader = _Reader()

        await read_consistent_transcript(
            state, slot, FORK, reader, persist=_Saves(), rewrite=rewrite
        )

        assert (rewrite.calls, [v.attempt for v in reader.views]) == (1, [2])

    @pytest.mark.asyncio
    async def test_a_rewind_landing_inside_the_save_is_saved_again(self, state):
        """The save clears the flag it did not take; the generation is the witness."""
        slot = _persisted_slot(state, "i-carry", ["u1", "a1"])
        slot._pending_rewrite = True

        def _rewind_inside() -> None:
            slot._dirty = True

        def _saved() -> None:
            slot._pending_rewrite = False

        rewrite = _Saves(_rewind_inside, then=_saved)
        reader = _Reader()

        await read_consistent_transcript(
            state, slot, FORK, reader, persist=_Saves(), rewrite=rewrite
        )

        assert (rewrite.calls, [v.attempt for v in reader.views]) == (2, [3])

    @pytest.mark.asyncio
    async def test_one_landing_during_the_fork_read_is_saved_on_the_next_attempt(self, state):
        slot = _persisted_slot(state, "i-midsave", ["u1", "a1"])

        def _rewind() -> None:
            slot._pending_rewrite = True

        def _saved() -> None:
            slot._pending_rewrite = False

        rewrite = _Saves(then=_saved)
        reader = _Reader(_rewind)

        await read_consistent_transcript(
            state, slot, FORK, reader, persist=_Saves(), rewrite=rewrite
        )

        assert (rewrite.calls, [v.attempt for v in reader.views]) == (1, [1, 3])

    @pytest.mark.asyncio
    async def test_the_transfer_refuses_it_before_and_after_the_read(self, state):
        slot = _persisted_slot(state, "i-refuse", ["u1", "a1"])
        slot._pending_rewrite = True
        reader = _Reader()

        with pytest.raises(SnapshotUnstable, match="pending rewrite"):
            await read_consistent_transcript(state, slot, TRANSFER, reader, persist=_Saves())
        assert reader.views == []

        slot._pending_rewrite = False
        discarded = []

        def _rewind() -> None:
            slot._pending_rewrite = True

        with pytest.raises(SnapshotUnstable, match="pending rewrite"):
            await read_consistent_transcript(
                state,
                slot,
                TRANSFER,
                _Reader(_rewind, result="bundle"),
                persist=_Saves(),
                discard=discarded.append,
            )
        assert discarded == ["bundle"]

    @pytest.mark.asyncio
    async def test_the_page_reads_through_it(self, state):
        slot = _persisted_slot(state, "i-through", ["u1", "a1"])
        slot._pending_rewrite = True
        slot._disk_window_len = 9

        snapshot = await read_consistent_transcript(state, slot, PAGE, _Reader(result="page"))

        assert snapshot.result == "page"


class TestTheTransferRules:
    @pytest.mark.asyncio
    async def test_a_dirty_slot_is_persisted_before_every_read(self, state):
        slot = _persisted_slot(state, "i-flush", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")

        def _clean() -> None:
            slot._dirty = False

        persist = _Saves(then=_clean)
        reader = _Reader()

        await read_consistent_transcript(state, slot, TRANSFER, reader, persist=persist)

        assert (persist.calls, len(reader.views)) == (1, 1)

    @pytest.mark.asyncio
    async def test_an_edit_landing_inside_the_flush_spends_the_attempt(self, state):
        slot = _persisted_slot(state, "i-reflush", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")
        flushes = []

        async def _persist() -> None:
            flushes.append(slot._dirty_gen)
            # The first flush has an edit land inside it (a new mark moves the
            # generation); the second commits and clears the flag.
            slot._dirty = len(flushes) == 1

        reader = _Reader()

        await read_consistent_transcript(state, slot, TRANSFER, reader, persist=_persist)

        assert (len(flushes), [v.attempt for v in reader.views]) == (2, [2])

    @pytest.mark.asyncio
    async def test_a_rewind_landing_inside_the_flush_is_refused_before_the_read(self, state):
        slot = _persisted_slot(state, "i-flushrewind", ["u1", "a1"])
        slot.append("user", "u2", "msg msg-u")

        async def _persist_while_a_rewind_lands() -> None:
            slot._pending_rewrite = True
            slot._dirty_flag = False

        reader = _Reader()

        with pytest.raises(SnapshotUnstable, match="pending rewrite"):
            await read_consistent_transcript(
                state, slot, TRANSFER, reader, persist=_persist_while_a_rewind_lands
            )

        assert reader.views == []

    @pytest.mark.asyncio
    async def test_a_boundary_ahead_is_refused_before_any_read(self, state):
        slot = _persisted_slot(state, "i-ahead", ["u1", "a1"])
        slot._disk_window_len = 9
        reader = _Reader()

        with pytest.raises(SnapshotUnstable, match="boundary is ahead"):
            await read_consistent_transcript(state, slot, TRANSFER, reader, persist=_Saves())

        assert reader.views == []

    @pytest.mark.asyncio
    async def test_a_deleted_session_is_refused_before_and_after_the_read(self, state):
        slot = _persisted_slot(state, "i-deleted", ["u1", "a1"])
        discarded = []

        def _delete() -> None:
            assert state.conversation_log.delete_session("dashboard:i-deleted") is True

        with pytest.raises(SnapshotUnstable, match="permanently deleted"):
            await read_consistent_transcript(
                state,
                slot,
                TRANSFER,
                _Reader(_delete, result="bundle"),
                persist=_Saves(),
                discard=discarded.append,
            )
        assert discarded == ["bundle"]

        reader = _Reader()
        with pytest.raises(SnapshotUnstable, match="permanently deleted"):
            await read_consistent_transcript(state, slot, TRANSFER, reader, persist=_Saves())
        assert reader.views == []

    @pytest.mark.asyncio
    async def test_the_fork_and_the_page_leave_a_deleted_session_to_their_caller(self, state):
        slot = _persisted_slot(state, "i-notprobed", ["u1", "a1"])
        assert state.conversation_log.delete_session("dashboard:i-notprobed") is True

        for purpose in (FORK, PAGE):
            snapshot = await read_consistent_transcript(
                state, slot, purpose, _Reader(result="x"), persist=_Saves(), rewrite=_Saves()
            )
            assert snapshot.result == "x"


class TestTheCallables:
    @pytest.mark.asyncio
    async def test_what_a_callable_raises_reaches_the_caller_unchanged(self, state):
        slot = _persisted_slot(state, "i-raise", ["u1", "a1"])
        slot._pending_rewrite = True

        class _Refused(Exception):
            pass

        async def _rewrite() -> None:
            raise _Refused("mine")

        with pytest.raises(_Refused, match="mine"):
            await read_consistent_transcript(
                state, slot, FORK, _Reader(), persist=_Saves(), rewrite=_rewrite
            )

    @pytest.mark.asyncio
    async def test_a_purpose_names_the_callables_it_needs(self, state):
        slot = _persisted_slot(state, "i-needs", ["u1", "a1"])

        with pytest.raises(TypeError, match="rewrite"):
            await read_consistent_transcript(state, slot, FORK, _Reader(), persist=_Saves())
        with pytest.raises(TypeError, match="persist"):
            await read_consistent_transcript(state, slot, TRANSFER, _Reader())
        assert (await read_consistent_transcript(state, slot, PAGE, _Reader())).result == "rows"
