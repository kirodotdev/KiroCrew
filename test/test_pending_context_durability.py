"""Durability of a slot's queued background context across a restart and a handover.

An entry POSTed to ``/api/chat/slots/{slot}/context`` is answered 200 and held in
``slot._pending_context`` until the next turn drains it into the prompt. Before this
the only copy lived in process memory, so a restart between the 200 and the drain
dropped acknowledged context with no row and no error -- and a rows-only handover
save, writing one slot's rows onto a line another live slot published, had no
disposition for the queue at all.

Pinned here, per layer: the ctxId minted at the acknowledging boundary; the full
save's write through the union and its clearing by absence; the foreign-line merge
that keeps BOTH holders' entries; and the restore, which re-validates a value that
is drained into a prompt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.slot_buffers import sanitize_restored_pending_context
from kiro_crew.dashboard.state import (
    _MAX_PENDING_CONTEXT,
    _MAX_PERSISTED_CONTEXT_BYTES,
    MAX_CONTEXT_CONTENT,
    DashboardState,
)
from kiro_crew.history import (
    ROWS_ONLY_DEFERRED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    ConversationLog,
    read_ctx_overflow,
)


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _slot_with_rows(state, name: str = "s1"):
    """A slot carrying a persisted user row, i.e. the shape a queue is held on."""
    slot = state.get_or_create_slot(name)
    slot.append("user", "the running turn")
    slot.drain()
    return slot


def _entry(ctx_id: str, content: str = "background", source: str = "app-kit") -> dict:
    return {
        "ctxId": ctx_id,
        "content": content,
        "source": source,
        "ephemeral": True,
        "injectedAt": 1.0,
    }


def _meta(state, name: str = "s1") -> dict:
    return state.conversation_log._read_metadata(f"dashboard:{name}")


def _ctx_ids(meta: dict) -> list[str]:
    return [e["ctxId"] for e in meta.get("pending_context", [])]


class TestIdentity:
    def test_the_boundary_mints_a_ctx_id(self) -> None:
        """Minted where the 200 is issued, not at save time.

        The union dedupes on it, so two holders writing the same line must agree on
        one entry's identity -- and a save-time mint would give the same entry a new
        id on every save, making every save's copy look like a new entry.
        """
        from kiro_crew.dashboard.chat_handlers import _build_pending_context_entry

        slot = MagicMock(_pending_context=[])
        first, err = _build_pending_context_entry(slot, "a", "app-kit", True, None)
        second, _ = _build_pending_context_entry(slot, "a", "app-kit", True, None)

        assert err is None
        assert isinstance(first, dict) and isinstance(second, dict)
        assert isinstance(first["ctxId"], str) and first["ctxId"]
        assert first["ctxId"] != second["ctxId"], "two acknowledged entries are not one entry"


class TestPersist:
    def test_the_full_save_persists_the_queue(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _slot_with_rows(state)
        slot._pending_context[:] = [_entry("c1")]

        _save_slot_to_history(state, slot, closed=False)

        assert _ctx_ids(_meta(state)) == ["c1"]

    def test_a_drained_queue_is_cleared_by_absence(self, tmp_path) -> None:
        """The key is owned, so the next save's omission must erase it.

        Carried forward instead, a restart would re-inject context the model was
        already given -- the drain has no other way to retract it.
        """
        state = _make_state(tmp_path)
        slot = _slot_with_rows(state)
        slot._pending_context[:] = [_entry("c1")]
        _save_slot_to_history(state, slot, closed=False)

        slot._pending_context.clear()
        _save_slot_to_history(state, slot, closed=False)

        assert "pending_context" not in _meta(state)


class TestMerge:
    def test_the_key_is_owned_and_merged_rather_than_deferred(self) -> None:
        # Owned so absence clears a drained queue; NOT deferred, because on a
        # foreign line either one-sided choice discards acknowledged entries.
        assert "pending_context" in SLOT_OWNED_META_KEYS
        assert "pending_context" not in ROWS_ONLY_DEFERRED_META_KEYS

    def test_rows_only_unions_both_holders_queues(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        holder = _slot_with_rows(state, "s1")
        holder._pending_context[:] = [_entry("held", "the holder's context")]
        _save_slot_to_history(state, holder, closed=False)

        # A popped slot writing its rows onto a line another slot published.
        popped = state.get_or_create_slot("s2")
        popped._tab_id = "otherslot"
        popped.linked_session_key = "dashboard:s1"
        popped.append("user", "handover row")
        popped.drain()
        popped._pending_context[:] = [_entry("popped", "the popped slot's context")]

        _save_slot_to_history(state, popped, rows_only=True)

        # Both, on-disk side first: neither holder's acknowledged context is lost.
        assert _ctx_ids(_meta(state)) == ["held", "popped"]

    def test_the_same_entry_from_both_sides_is_not_doubled(self, tmp_path) -> None:
        """Deduplicated by ``ctxId``, so a re-save cannot re-inject one entry twice."""
        state = _make_state(tmp_path)
        slot = _slot_with_rows(state)
        slot._pending_context[:] = [_entry("c1")]

        _save_slot_to_history(state, slot, closed=False)
        _save_slot_to_history(state, slot, closed=False)

        assert _ctx_ids(_meta(state)) == ["c1"]

    def test_the_empty_window_merge_unions_a_foreign_line_too(self, tmp_path) -> None:
        """The merge writes this key, so it needs the same rule as the full save.

        Mirroring live state onto a foreign line here would drop the holder's queue
        as surely as the full save would.
        """
        import asyncio

        from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

        state = _make_state(tmp_path)
        holder = _slot_with_rows(state, "s1")
        holder._pending_context[:] = [_entry("held")]
        _save_slot_to_history(state, holder, closed=False)

        rowless = state.get_or_create_slot("s2")
        rowless._tab_id = "otherslot"
        rowless.linked_session_key = "dashboard:s1"
        rowless._pending_context[:] = [_entry("arriving")]

        asyncio.run(save_slot_off_loop(state, rowless, force=True))

        assert _ctx_ids(_meta(state)) == ["held", "arriving"]


class TestFinalSaveSpills:
    def test_a_closing_save_spills_the_excess_instead_of_deferring_it(
        self, tmp_path, monkeypatch
    ) -> None:
        """A close is the last save, so a deferral has no later save to retry it.

        Over-budget entries therefore go to the sidecar. Asserting only that the
        metadata line stayed bounded would pass on a variant that dropped them.
        """
        from kiro_crew import history as h

        monkeypatch.setattr(h, "_SESSION_MAX_BYTES", 40_000, raising=True)
        monkeypatch.setattr(h, "_sessions_dir", lambda: tmp_path, raising=True)

        state = _make_state(tmp_path)
        slot = _slot_with_rows(state)
        slot._pending_context[:] = [
            _entry("fits", "f" * 100),
            _entry("over-1", "o" * 15_000),
            _entry("over-2", "o" * 15_000),
        ]

        _save_slot_to_history(state, slot, closed=True)

        on_line = _ctx_ids(_meta(state))
        spilled = [e["ctxId"] for e in read_ctx_overflow("dashboard:s1", tmp_path)]

        # Asserting only that the line stayed bounded would pass on a variant
        # that dropped the excess instead of spilling it.
        assert sorted([*on_line, *spilled]) == ["fits", "over-1", "over-2"]
        assert spilled, "the excess must be recoverable, not dropped"
        assert not set(on_line) & set(spilled), "an entry in both places is two copies"
        assert "fits" in on_line


class TestRestore:
    def test_restore_hands_the_queue_back(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = _slot_with_rows(state)
        slot._pending_context[:] = [_entry("c1", "survive the restart")]
        _save_slot_to_history(state, slot, closed=False)

        # A fresh process: same transcript, a slot that has never seen the entry.
        reborn = _make_state(tmp_path)
        revived = _rehydrate_slot_from_history(reborn, "s1")

        assert revived is not None
        assert [e["content"] for e in revived._pending_context] == ["survive the restart"]

    def test_a_tampered_value_cannot_reach_the_queue(self) -> None:
        """On-disk metadata is a trust boundary and this queue is drained into a prompt.

        Each case would raise or inject if it were trusted: a missing ``content``
        KeyErrors at drain, a non-finite ``injectedAt`` never expires, and an entry
        past the persistable bound cannot have been admitted by the endpoint, which
        caps content before answering 200.
        """
        tampered = [
            "not a dict",
            {"ctxId": "no-content", "source": "app-kit", "injectedAt": 1.0},
            {"ctxId": "nan", "content": "x", "source": "app-kit", "injectedAt": float("nan")},
            _entry("too-big", "x" * (MAX_CONTEXT_CONTENT + 1)),
        ]

        assert (
            sanitize_restored_pending_context(
                tampered,
                max_entries=_MAX_PENDING_CONTEXT,
                max_chars=MAX_CONTEXT_CONTENT,
                max_entry_bytes=_MAX_PERSISTED_CONTEXT_BYTES,
            )
            == []
        )

    def test_the_oldest_are_dropped_when_a_tampered_value_is_over_the_count(self) -> None:
        """Dropping the OLDEST matches the live enqueue's own eviction order.

        Dropping the newest instead would reorder what the next drain injects.
        """
        raw = [_entry(f"c{i}") for i in range(_MAX_PENDING_CONTEXT + 2)]

        kept = sanitize_restored_pending_context(
            raw,
            max_entries=_MAX_PENDING_CONTEXT,
            max_chars=MAX_CONTEXT_CONTENT,
            max_entry_bytes=_MAX_PERSISTED_CONTEXT_BYTES,
        )

        assert len(kept) == _MAX_PENDING_CONTEXT
        assert kept[0]["ctxId"] == "c2"
        assert kept[-1]["ctxId"] == f"c{_MAX_PENDING_CONTEXT + 1}"
