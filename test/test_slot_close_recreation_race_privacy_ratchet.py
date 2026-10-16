"""The ``memory_mode`` ratchet a slot-close hand-over keeps on a shared transcript.

A restricted tail draining under a persistent replacement tightens its line, live slot,
key marker and execution carrier, and rolls them back only when no private row landed;
the app-render tool rows tighten the same way; the carrier compare-and-swap retries; and
a save over an unreadable or corrupt metadata line fails closed. The close-vs-recreate
guards themselves live in ``test_slot_close_recreation_race.py``; the shared fakes in
``slot_close_recreation_race_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path

import pytest
from chat_test_helpers import _make_state
from slot_close_recreation_race_helpers import _no_nudge_service  # noqa: F401
from slot_close_recreation_race_helpers import (
    HKEY,
    NAME,
    RKEY,
    _arm_running_turn,
    _disk_contents,
    _publish_metadata,
    _reached,
    _Req,
    _slot_with_committed_and_uncommitted_rows,
)
from turn_harness import Do, SlotSpec, TurnContext, TurnScript, run_turn

from kiro_crew import execution_context
from kiro_crew import history as history_mod
from kiro_crew import mcp_apps_render
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.dashboard import chat_runner, chat_summary
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.history import is_incognito_transcript
from kiro_crew.memory_stores import UnknownMemoryStore


def _summary_cfg():
    """A config with the session summary ON, so its gate is decided by the slot."""
    from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig

    cfg = KiroCrewConfig()
    cfg.session_summary = SessionSummaryConfig(enabled=True)
    return cfg


def _floor_after_disk_tail(state, slot) -> None:
    """Tell a fresh same-key *slot* the newest ``ts`` already in its transcript file.

    A slot learns the file's tail only at its own first save boundary
    (``note_disk_tail``), so a replacement's FIRST row is stamped from the bare
    clock. On a host whose clock ticks coarsely (Windows advances it in ~15.6 ms
    steps) that stamp can tie the rows the dismissed original committed a moment
    earlier, and the save's two-way ``ts`` merge keeps the window's line first on
    an exact tie -- so the replacement's row would read back AHEAD of rows that
    were written before it. These tests assert file order to prove every row
    survived, not to probe that merge, so the replacement is told the on-disk
    tail up front, the same floor its first save would have recorded.
    """
    slot.note_disk_tail(*(m.get("ts") for m in state.conversation_log.read_messages(HKEY)))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_delete_handover_of_a_restricted_tail_tightens_a_persistent_line(
    tmp_path, caplog, monkeypatch, mode
) -> None:
    """A restricted tail draining under a persistent replacement's line tightens it.

    The rows-only drain defers ``memory_mode`` to the line the replacement
    published, and that field is what every learning reader gates on. Writing
    the restricted original's tail under a line that says persistent would make
    consolidation, the history tools and the summary treat private content as
    ordinary; refusing the write would lose the reply the user was watching, with
    no retry path (the slot is popped). The line is a ratchet any writer may
    tighten, so the drain folds the retained mode in and lands the rows under the
    stricter value -- the same file the other race order reaches, where the
    original's committed line ratchets the replacement's own save (see
    ``test_delete_handover_of_a_restricted_tail_lands_under_the_ratcheted_line``).
    A restricted line names no store, so the replacement's store name goes with
    the loosening; its title and folder are not the drain's to touch and stay.

    The line is not the only thing that must tighten. The replacement is LIVE
    and has already bound a persistent execution -- a durable record on the same
    line, which ``read_session_execution`` answers from -- and the readers that
    gate on the live slot rather than the line (the session summary, the export)
    read ``slot.memory_mode`` and then the whole transcript from disk. So the
    drain tightens the replacement slot itself before the rows are written, the
    save folds the carried execution record with the line, and the summary gate
    refuses the replacement afterwards: the private rows are on disk only once
    every reader of that file, by line or by slot, already sees them as restricted.

    The original committed NOTHING before the close, so the persistent line is
    the replacement's alone -- the only shape in which the drain meets a looser
    line. The replacement's first row is floored after the original's rows so the
    file order asserted below is a property of the write sequence and not of the
    host clock's resolution.
    """
    state = _make_state(tmp_path)
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "TAIL-1")
    original.append("assistant", "TAIL-2")
    original.drain()
    assert not state.conversation_log._path(HKEY).exists()

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)
    caplog.set_level(logging.INFO)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement is not original
    assert replacement.memory_mode == "persistent"
    replacement.note_disk_tail(*(m.get("ts") for m in original.messages))
    replacement.append("user", "REPLACEMENT-1")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    # The replacement's turn-start binding: a persistent DURABLE carrier on the line.
    persistent = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "kirocrew",
    )
    await asyncio.to_thread(execution_context.bind_session_execution, HKEY, persistent)
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == "persistent"
    assert "memory_store" in meta, "the persistent binding wrote no store field to drop"
    assert meta[execution_context.EXECUTION_CONTEXT_KEY]["memory_mode"] == "persistent"
    assert execution_context.read_session_execution(HKEY).memory_mode == "persistent"
    assert not chat_summary._should_summarize(
        _summary_cfg(), replacement, 4, force=True
    ), "the persistent replacement was not summarizable before the drain"
    release.set()
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

    assert resp.status == 200, "the restricted tail was refused instead of landing"
    assert state._slots.get(NAME) is replacement
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the drain filed private rows under a persistent line"
    assert "memory_store" not in meta, "the tightened line still names a store"
    assert meta.get("title") == "REPLACEMENT TITLE"
    assert meta.get("folder_id") == "f-r"
    assert _disk_contents(state) == ["TAIL-1", "TAIL-2", "REPLACEMENT-1"]
    # The live holder of the file is restricted too, not just the line.
    assert replacement.memory_mode == mode, "the live replacement still reads persistent"
    assert HKEY in state._restricted_keys, "the key-scoped marker was not re-derived"
    record = meta[execution_context.EXECUTION_CONTEXT_KEY]
    assert record["memory_mode"] == mode, "the carried execution record still says persistent"
    assert record["store"] == {"store_id": "default", "member_id": None}, "the identity moved"
    assert execution_context.read_session_execution(HKEY).memory_mode == mode
    assert (
        chat_summary._should_summarize(_summary_cfg(), replacement, 4, force=True) == "memory_mode"
    ), "the summary would still read the private rows off disk for the replacement"
    messages = [record.getMessage() for record in caplog.records]
    assert not any(
        "unpersisted row(s) were not written" in message for message in messages
    ), "the drain reported rows lost that it wrote"
    assert any(
        f"tightened another holder's persistent line to {mode}" in message for message in messages
    ), "the save did not report the tightening"

    # The live replacement's own next save folds the tightened line back in: the
    # ratchet holds, the store stays off the line, and every row is still there.
    replacement.append("user", "REPLACEMENT-2")
    replacement.drain()
    assert await handlers.save_slot_off_loop(state, replacement, best_effort=False)
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the replacement's full save loosened the line"
    assert "memory_store" not in meta
    assert _disk_contents(state) == ["TAIL-1", "TAIL-2", "REPLACEMENT-1", "REPLACEMENT-2"]


@pytest.mark.asyncio
async def test_tool_result_rows_tighten_a_same_transcript_replacement(
    tmp_path, monkeypatch
) -> None:
    """The app-render rows-only writer protects a replacement before its write."""
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("tool", "PRIVATE TOOL RESULT")
    original.drain()
    state._slots.pop(NAME)

    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT")
    replacement.drain()
    assert await handlers.save_slot_off_loop(state, replacement, best_effort=False)
    persistent = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "kirocrew",
    )
    await asyncio.to_thread(execution_context.bind_session_execution, HKEY, persistent)
    assert replacement.memory_mode == "persistent"
    assert execution_context.read_session_execution(HKEY).memory_mode == "persistent"

    assert await chat_runner._persist_tool_result_rows(state, original)

    meta = state.conversation_log.get_metadata(HKEY)
    assert (
        replacement.memory_mode == "temporary"
    ), "the live replacement stayed looser while private tool rows reached disk"
    assert HKEY in state._restricted_keys, "the replacement's key marker was not re-derived"
    assert meta.get("memory_mode") == "temporary", "the tool rows landed under a looser line"
    assert meta[execution_context.EXECUTION_CONTEXT_KEY]["memory_mode"] == "temporary"
    assert execution_context.read_session_execution(HKEY).memory_mode == "temporary"


@pytest.mark.asyncio
async def test_the_app_render_path_writes_its_tool_rows_through_the_privacy_wrapper(
    tmp_path, monkeypatch
) -> None:
    """Through the real ``_run_chat``: the MCP-app render persists its tool rows
    only after a same-transcript replacement has been tightened.

    A restricted turn is popped mid-stream and a same-key, persistent slot takes
    its place -- sharing its transcript. When the render claims the app, its
    rows-only write must already find the replacement temporary and its key
    marked restricted: during a write that lands first, live-slot gates (summary,
    memory, app 403 checks) would derive from the private rows. The end state
    converges either way (the save's own callback tightens right after the
    write), so the probe is taken at the moment of the write.
    """
    spool = tmp_path / "mcp-apps"
    spool.mkdir()
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", str(spool))
    sid = uuid.uuid4().hex
    (spool / f"{sid}.json").write_text(
        json.dumps(
            {
                "schema": mcp_apps_render.SPOOL_SCHEMA_VERSION,
                "server": "excalidraw",
                "tool": "create_view",
                "html": "<h1>x</h1>",
                "session_key": HKEY,
            }
        ),
        encoding="utf-8",
    )
    held: dict = {}
    writes: list[tuple[bool, str, bool]] = []
    real_save = chat_runner.save_slot_off_loop

    async def _probe(state, slot, *args, **kwargs):
        if kwargs.get("rows_only"):
            live = state._slots[NAME]
            writes.append(
                (live is held["replacement"], live.memory_mode, HKEY in state._restricted_keys)
            )
        return await real_save(state, slot, *args, **kwargs)

    monkeypatch.setattr(chat_runner, "save_slot_off_loop", _probe)

    def _replace_mid_turn(ctx: TurnContext) -> None:
        # After preparation, so the turn's own binding checks have passed.
        ctx.state._slots.pop(NAME)
        held["replacement"] = ctx.state.get_or_create_slot(NAME)

    record = await run_turn(
        TurnScript(
            events=[
                Do(_replace_mid_turn),
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id="tc-app-1",
                    title="create_view",
                    tool_name="create_view",
                    mcp_server_name="excalidraw",
                ),
                AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id="tc-app-1",
                    tool_output=f"Done. [kirocrew-mcp-app:{sid}]",
                    tool_final=True,
                ),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="There you go."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        ),
        slot=SlotSpec(key=NAME, memory_mode="temporary"),
    )
    assert record.frames("mcp_app_render"), "the render path never ran"
    assert writes == [(True, "temporary", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_delete_handover_of_a_restricted_tail_lands_under_the_ratcheted_line(
    tmp_path, mode
) -> None:
    """A restricted original's committed line ratchets the replacement, so its tail lands.

    The persistent replacement publishes over a line the restricted original
    committed; the fold keeps that line's mode, so the drain finds no looser line
    to refuse and the tail is written under the mode the rows were spoken in.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "PERSISTED-1")
    original.append("assistant", "PERSISTED-2")
    original.drain()
    assert await handlers.save_slot_off_loop(state, original, best_effort=False)
    original.append("user", "TAIL-3")
    original.append("assistant", "TAIL-4")
    original.drain()

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == mode
    release.set()
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the replacement's publish loosened the line"
    assert not meta.get("memory_store")
    assert meta.get("title") == "REPLACEMENT TITLE"
    assert _disk_contents(state) == ["PERSISTED-1", "PERSISTED-2", "TAIL-3", "TAIL-4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["close", "tool"])
@pytest.mark.parametrize("failure", ["declined", "raised"])
async def test_failed_handover_restores_replacement_when_no_private_rows_landed(
    tmp_path, monkeypatch, caplog, caller, failure
) -> None:
    """A persistent line proves a failed hand-over wrote no private rows."""
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "persistent"

    async def _failed_save(*_args, **_kwargs):
        if failure == "raised":
            raise OSError("disk wedged")
        return False

    target = handlers if caller == "close" else chat_runner
    monkeypatch.setattr(target, "save_slot_off_loop", _failed_save)
    caplog.set_level(logging.WARNING)

    if caller == "close":
        result = await handlers._persist_handover_tail(state, NAME, original)
        assert result.rows_committed is False
    elif failure == "raised":
        with pytest.raises(OSError, match="disk wedged"):
            await chat_runner._persist_tool_result_rows(state, original)
    else:
        assert await chat_runner._persist_tool_result_rows(state, original) is False

    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "persistent"
    assert (
        replacement.memory_mode == "persistent"
    ), "the failed save left the replacement restricted"
    assert (
        RKEY not in state._restricted_keys
    ), "the failed save left the replacement marked restricted"
    if caller == "close":
        messages = [record.getMessage() for record in caplog.records]
        expected = "could not be written" if failure == "raised" else "were not written"
        assert any(expected in message for message in messages)


@pytest.mark.asyncio
async def test_failed_handover_does_not_undo_a_concurrent_title_fold(tmp_path, monkeypatch) -> None:
    """A title writer ordered after the read leaves its pending mode before unlock."""
    from kiro_crew.dashboard import chat_persistence, chat_utils

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")
    tightening = chat_utils.tighten_replacement_to_restricted_original(state, NAME, original)
    assert tightening is not None

    log = state.conversation_log
    real_hold = log.derivation_hold

    @contextlib.contextmanager
    def _title_fold_after_read(stems):
        with real_hold(stems):
            yield
        fields = {"title": "CONCURRENT TITLE"}

        def _fold(meta):
            fields["memory_mode"] = execution_context.stricter_memory_mode(
                execution_context.canonical_memory_mode(meta.get("memory_mode")),
                "temporary",
            )
            return True

        assert log.update_metadata_if(
            HKEY,
            fields,
            _fold,
            after_commit_under_lock=lambda: chat_persistence._record_pending_memory_mode(
                replacement, fields["memory_mode"]
            ),
        )

    monkeypatch.setattr(log, "derivation_hold", _title_fold_after_read)

    assert not await chat_utils.restore_replacement_if_handover_did_not_land(
        state, NAME, tightening, HKEY
    )
    assert log.get_metadata(HKEY).get("memory_mode") == "temporary"
    assert log.get_metadata(HKEY).get("title") == "CONCURRENT TITLE"
    assert replacement.memory_mode == "temporary"
    assert RKEY in state._restricted_keys
    assert (
        execution_context.read_live_session_execution(chat_utils.effective_session_key(replacement))
        == tightening.tightened_execution
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "busy"])
async def test_failed_handover_keeps_replacement_restricted_when_line_unverifiable(
    tmp_path, monkeypatch, failure
) -> None:
    """An unreadable line or unavailable transcript lock cannot authorize rollback."""
    from kiro_crew.dashboard import chat_utils

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")
    tightening = chat_utils.tighten_replacement_to_restricted_original(state, NAME, original)
    assert tightening is not None

    if failure == "unreadable":
        monkeypatch.setattr(state.conversation_log, "get_metadata_status", lambda _key: ({}, False))
    else:

        @contextlib.contextmanager
        def _busy(_stems):
            raise history_mod.TranscriptBusy("held by writer")
            yield

        monkeypatch.setattr(state.conversation_log, "derivation_hold", _busy)

    assert not await chat_utils.restore_replacement_if_handover_did_not_land(
        state, NAME, tightening, HKEY
    )
    assert replacement.memory_mode == "temporary"
    assert RKEY in state._restricted_keys
    assert (
        execution_context.read_live_session_execution(chat_utils.effective_session_key(replacement))
        == tightening.tightened_execution
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["close", "tool"])
async def test_failed_handover_keeps_replacement_restricted_when_atomic_write_landed(
    tmp_path, monkeypatch, caller
) -> None:
    """A tightened line proves the atomic rows-and-metadata replacement landed."""
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE TAIL")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    replacement.append("user", "REPLACEMENT ROW")
    replacement.drain()
    await _publish_metadata(state, replacement, title="REPLACEMENT", folder="f-r")

    target = handlers if caller == "close" else chat_runner
    real_save = target.save_slot_off_loop

    async def _land_then_decline(*args, **kwargs):
        assert await real_save(*args, **kwargs)
        return False

    monkeypatch.setattr(target, "save_slot_off_loop", _land_then_decline)

    if caller == "close":
        result = await handlers._persist_handover_tail(state, NAME, original)
        assert result.rows_committed is False
    else:
        assert await chat_runner._persist_tool_result_rows(state, original) is False

    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "temporary"
    assert replacement.memory_mode == "temporary", "the landed rows lost their live restriction"
    assert RKEY in state._restricted_keys, "the landed rows lost their restricted marker"


def test_tightening_retries_carrier_cas_before_mutating_replacement(tmp_path, monkeypatch) -> None:
    """One carrier rebind is retried before the slot and marker become restricted."""
    from kiro_crew.dashboard import chat_utils as chat_utils_mod

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE-1")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    state._restricted_keys.discard(RKEY)
    skey = chat_utils_mod.effective_session_key(replacement)
    initial = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "initial-template",
    )
    rebound = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "rebound-template",
    )
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = initial

    real_tighten = chat_utils_mod.tighten_live_session_execution
    calls = 0

    def _rebind_once(session_key, memory_mode, *, expected=...):
        nonlocal calls
        calls += 1
        if calls == 1:
            with execution_context._EXECUTION_LOCK:
                execution_context._LIVE_EXECUTIONS[execution_context._live_key(session_key)] = (
                    rebound
                )
        return real_tighten(session_key, memory_mode, expected=expected)

    monkeypatch.setattr(chat_utils_mod, "tighten_live_session_execution", _rebind_once)
    try:
        tightening = chat_utils_mod.tighten_replacement_to_restricted_original(
            state, NAME, original
        )

        assert calls == 2, "the carrier conflict was not retried exactly once"
        assert tightening is not None
        assert tightening.previous_execution == rebound, "the retry kept the stale carrier witness"
        assert replacement.memory_mode == "temporary"
        assert RKEY in state._restricted_keys
        tightened = execution_context.read_live_session_execution(skey)
        assert tightened is not None
        assert tightened.memory_mode == "temporary"
        assert tightened.template_id == rebound.template_id, "the retry replaced carrier identity"
    finally:
        execution_context.clear_session_execution(skey)


def test_second_carrier_cas_failure_leaves_replacement_unmodified(tmp_path, monkeypatch) -> None:
    """Two carrier rebinds propagate without partially tightening slot-owned state."""
    from kiro_crew.dashboard import chat_utils as chat_utils_mod

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE-1")
    original.drain()
    state._slots.pop(NAME)
    replacement = state.get_or_create_slot(NAME)
    state._restricted_keys.discard(RKEY)
    skey = chat_utils_mod.effective_session_key(replacement)
    initial = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "initial-template",
    )
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = initial

    attempted_expected = []

    def _always_fail(_session_key, _memory_mode, *, expected=...):
        attempted_expected.append(expected)
        raise UnknownMemoryStore(
            "Execution memory is unavailable: session changed during privacy tightening; "
            "Global was not used"
        )

    monkeypatch.setattr(chat_utils_mod, "tighten_live_session_execution", _always_fail)
    try:
        with pytest.raises(UnknownMemoryStore, match="session changed during privacy tightening"):
            chat_utils_mod.tighten_replacement_to_restricted_original(state, NAME, original)

        assert attempted_expected == [initial, initial], "the carrier conflict was not retried once"
        assert (
            replacement.memory_mode == "persistent"
        ), "the failed CAS partially tightened the slot"
        assert RKEY not in state._restricted_keys, "the failed CAS partially tightened the marker"
        assert execution_context.read_live_session_execution(skey) == initial
    finally:
        execution_context.clear_session_execution(skey)


@pytest.mark.asyncio
async def test_delete_handover_saves_tail_after_second_carrier_cas_failure(
    tmp_path, monkeypatch
) -> None:
    """A CAS refusal cannot skip the rows-only save that owns the popped tail."""
    from kiro_crew.dashboard import chat_utils as chat_utils_mod

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="temporary")
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME)
    skey = chat_utils_mod.effective_session_key(replacement)
    initial = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "replacement-template",
    )
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = initial

    calls = 0

    def _always_fail(_session_key, _memory_mode, *, expected=...):
        nonlocal calls
        calls += 1
        raise UnknownMemoryStore(
            "Execution memory is unavailable: session changed during privacy tightening; "
            "Global was not used"
        )

    monkeypatch.setattr(chat_utils_mod, "tighten_live_session_execution", _always_fail)
    try:
        release.set()
        # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
        resp = await close

        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}
        assert calls == 2
        assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2"]
        assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "temporary"
    finally:
        execution_context.clear_session_execution(skey)


@pytest.mark.asyncio
async def test_tightening_a_live_replacement_keeps_and_rolls_back_its_carrier(tmp_path):
    """The hand-over tightening must not evict a live member-bound carrier.

    Clearing the replacement's live execution would leave the rest of its turn
    with no execution to check the store binding against (the check skips on
    ``None`` -- the fail-OPEN direction for a private silo) and the next turn's
    fold with nothing to fold into. The carrier is tightened in place instead:
    same identity, stricter mode, and a failed write restores that generation.
    """
    from kiro_crew.dashboard.chat_utils import (
        effective_session_key,
        restore_replacement_if_handover_did_not_land,
        slot_history_key,
        tighten_replacement_to_restricted_original,
    )

    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="incognito")
    original.append("user", "PRIVATE-1")
    original.drain()
    state._slots.pop(NAME)  # popped as a close does, still referenced here
    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    skey = effective_session_key(replacement)
    live = execution_context.ExecutionContext(
        "id-alice",
        execution_context.MemoryStoreRef("member-alice", "id-alice"),
        "member",
        "shared-template",
        "incognito",
    )
    # A live carrier for the replacement (as a restricted member binding leaves).
    with execution_context._EXECUTION_LOCK:
        execution_context._LIVE_EXECUTIONS[execution_context._live_key(skey)] = live
    try:
        # Model the original being stricter than the live carrier says.
        original.memory_mode = "temporary"
        tightening = tighten_replacement_to_restricted_original(state, NAME, original)

        assert replacement.memory_mode == "temporary"
        after = execution_context.read_live_session_execution(skey)
        assert after is not None, "the live carrier was cleared instead of tightened"
        assert after.memory_mode == "temporary"
        assert (after.member_id, after.store) == (live.member_id, live.store), "the identity moved"

        assert await restore_replacement_if_handover_did_not_land(
            state, NAME, tightening, slot_history_key(original)
        )
        assert replacement.memory_mode == "persistent"
        assert RKEY not in state._restricted_keys
        assert execution_context.read_live_session_execution(skey) == live
    finally:
        execution_context.clear_session_execution(skey)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_save_over_an_unreadable_line_is_deferred_not_folded_as_persistent(
    tmp_path, monkeypatch, mode
) -> None:
    """The ratchet must not fold a line it could not read.

    A restricted original's committed line, then a persistent recreate on the
    freed key whose first save meets a transient read failure on that line
    (``get_metadata_status`` -> ``({}, False)``). An empty dict folds as
    ``persistent``, so without the guard this save would relabel the restricted
    file persistent and stamp a store name on it -- the one write the ratchet
    exists to prevent. The save must fail CLOSED like the identity check does:
    raise, leaving ``_dirty`` armed so the flush retries once the read clears.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "PRIVATE-1")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == mode

    replacement = state.get_or_create_slot(NAME)
    replacement.memory_store = "coding"
    _floor_after_disk_tail(state, replacement)
    replacement.append("user", "REPLACEMENT-2")
    replacement.drain()

    log = state.conversation_log
    real_status = type(log).get_metadata_status
    monkeypatch.setattr(
        type(log),
        "get_metadata_status",
        lambda self, key: ({}, False) if key == HKEY else real_status(self, key),
    )
    with pytest.raises(OSError, match="deferred for retry"):
        await handlers.save_slot_off_loop(state, replacement, best_effort=False)

    monkeypatch.setattr(type(log), "get_metadata_status", real_status)
    meta = log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the unreadable read relabeled the line"
    assert "memory_store" not in meta
    assert _disk_contents(state) == ["PRIVATE-1"], "rows were written past a refused save"


def _corrupt_first_line(state) -> None:
    """Truncate the transcript's metadata line to bytes that are not JSON.

    The shape a torn write leaves: the line still opens like a metadata line
    (``"_type"`` and all) but never closes, so it is JSON to no parser and will
    not become one on a retry.
    """
    path = state.conversation_log._path(HKEY)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines and lines[0].startswith('{"_type": "metadata"')
    lines[0] = lines[0][: len('{"_type": "metadata", "cre')] + "\n"
    path.write_text("".join(lines), encoding="utf-8")
    # The damage lands from outside this process (a torn write, a disk fault),
    # so the reader's identity cache -- keyed on mtime, which a coarse clock
    # can tie with the save a moment earlier -- must not answer for it here.
    state.conversation_log._invalidate_cache(HKEY)


@pytest.mark.asyncio
async def test_a_save_over_a_corrupt_line_rewrites_it_strictest_and_lands_the_rows(
    tmp_path,
) -> None:
    """A permanently corrupt first line must not wedge every later save.

    Deferring is right for a TRANSIENT read failure, but a first line that is
    not JSON never becomes readable, so a save that kept deferring would never
    persist another row, ``closed`` would never land and the tab would
    resurrect on every restart. The full save rewrites the line instead -- the
    rows after it survive, ``closed`` can follow -- and stamps the STRICTEST
    mode with no store: the line's real contract is unknowable and the ratchet
    forbids relabelling it looser.
    """
    from kiro_crew.execution_context import STRICTEST_MEMORY_MODE
    from kiro_crew.history import METADATA_LINE_CORRUPT, METADATA_LINE_READABLE

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME)
    slot.memory_store = "coding"
    slot.append("user", "ROW-1")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    identity = log.get_metadata(HKEY)["created_at"]
    assert slot._disk_meta_created_at == identity, "the slot must have observed its line"

    _corrupt_first_line(state)
    assert log.metadata_line_state(HKEY) == METADATA_LINE_CORRUPT
    assert log.get_metadata_status(HKEY) == ({}, False)

    slot.append("assistant", "ROW-2")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    assert log.metadata_line_state(HKEY) == METADATA_LINE_READABLE
    meta = log.get_metadata(HKEY)
    assert meta.get("_type") == "metadata", "the corrupt line was not rewritten"
    assert meta.get("memory_mode") == STRICTEST_MEMORY_MODE
    assert "memory_store" not in meta, "a restricted line names no store"
    assert meta.get("created_at") == identity, "the full save restores the slot's identity"
    assert _disk_contents(state) == ["ROW-1", "ROW-2"], "the rows did not land"
    assert slot.memory_mode == STRICTEST_MEMORY_MODE, "the live slot must follow the line"

    assert await handlers.save_slot_off_loop(state, slot, closed=True, best_effort=False) is True
    assert log.get_metadata(HKEY).get("closed") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_transient_read_failure_still_defers_the_save(tmp_path, monkeypatch, mode) -> None:
    """Only a CORRUPT line is rewritten; an OSError on the read stays deferred.

    The reader answers ``transient`` after its bounded retries, the save raises,
    ``_dirty`` stays armed and no row is written; once the read clears the next
    attempt lands the rows under the line's own (folded) mode, untouched.
    """
    from kiro_crew import history_projection

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME, memory_mode=mode)
    slot.append("user", "ROW-1")
    slot.drain()
    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    path = log._path(HKEY)

    real_open = open
    failing = {"on": True}

    def _flaky_open(file, *args, **kwargs):
        if failing["on"] and Path(file) == path:
            raise OSError("device not ready")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(history_projection, "open", _flaky_open, raising=False)
    monkeypatch.setattr(history_mod, "_METADATA_READ_RETRY_SECS", 0.0)
    slot.append("assistant", "ROW-2")
    slot.drain()
    slot._dirty = True
    assert await handlers.save_slot_off_loop(state, slot, best_effort=True) is True
    assert slot._dirty is True, "a deferred save must leave the retry armed"
    failing["on"] = False
    assert _disk_contents(state) == ["ROW-1"], "rows were written past a deferred save"
    assert log.get_metadata(HKEY).get("memory_mode") == mode

    assert await handlers.save_slot_off_loop(state, slot, best_effort=False) is True
    assert _disk_contents(state) == ["ROW-1", "ROW-2"]
    assert log.get_metadata(HKEY).get("memory_mode") == mode, "the retry relabeled the line"


def test_a_corrupt_line_withholds_derivation(tmp_path) -> None:
    """Readers keep failing closed on a corrupt line: no rows are derived from it."""
    from kiro_crew.history import transcript_withholds_derivation

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "ROW-1")
    slot.drain()
    assert _save_slot_to_history(state, slot, force=True) is True
    assert transcript_withholds_derivation(log, HKEY) is False

    _corrupt_first_line(state)
    assert transcript_withholds_derivation(log, HKEY) is True
    with pytest.raises(history_mod.TranscriptWithheld):
        log.derive_messages(HKEY)


def test_a_metadata_merge_over_a_corrupt_line_heals_it_strictest(tmp_path, monkeypatch) -> None:
    """``update_metadata_if`` rewrites a corrupt line and defers a transient one.

    The empty-window merge and the title upsert reach the line through it, so a
    corrupt line must not wedge ``closed`` for an empty tab either. The healed
    line keeps the rows, carries the strictest mode whatever the fields say, no
    store, and no minted ``created_at``.
    """
    from kiro_crew import history_projection
    from kiro_crew.execution_context import STRICTEST_MEMORY_MODE

    state = _make_state(tmp_path)
    log = state.conversation_log
    slot = state.get_or_create_slot(NAME)
    slot.append("user", "ROW-1")
    slot.drain()
    assert _save_slot_to_history(state, slot, force=True) is True
    _corrupt_first_line(state)

    seen: list[dict] = []
    applied = log.update_metadata_if(
        HKEY,
        {"closed": True, "memory_mode": "persistent", "memory_store": "coding"},
        lambda meta: seen.append(dict(meta)) or True,
    )
    assert applied is True
    assert seen == [{"memory_mode": STRICTEST_MEMORY_MODE}], "the guard saw the rebuilt line"
    meta = log.get_metadata(HKEY)
    assert meta.get("closed") is True
    assert meta.get("memory_mode") == STRICTEST_MEMORY_MODE, "fields relabeled a healed line"
    assert "memory_store" not in meta
    assert "created_at" not in meta, "a healed line must not mint an identity"
    assert _disk_contents(state) == ["ROW-1"]

    monkeypatch.setattr(
        type(log),
        "_read_metadata_state",
        lambda self, key: ({}, history_projection.METADATA_LINE_TRANSIENT),
    )
    assert log.update_metadata_if(HKEY, {"title": "T"}, lambda meta: True) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_same_key_persistent_recreate_cannot_relabel_a_restricted_line(
    tmp_path, mode
) -> None:
    """The on-disk ``memory_mode`` is a ratchet: a later writer can only tighten it.

    A restricted slot commits rows and closes cleanly. Its file stays, and
    ``get_or_create_slot`` hands the freed key to a persistent slot whose saves
    rebuild the metadata line from their own state. Without the fold, the first
    such save would write ``memory_mode: persistent`` plus a store name over the
    committed private rows, and the consolidator and the MCP history tools --
    which read only that line -- would learn from them. Both writers are driven:
    the empty-window merge a newborn's metadata route takes, and the full save
    that carries the replacement's first row.
    """
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode=mode)
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    assert NAME not in state._slots
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == mode
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2"]

    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    replacement.memory_store = "coding"
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the empty-window merge relabeled the line"
    assert not meta.get("memory_store"), "the merge named a store on a restricted line"
    assert meta.get("title") == "REPLACEMENT TITLE"

    _floor_after_disk_tail(state, replacement)
    replacement.append("user", "REPLACEMENT-3")
    replacement.drain()
    assert await handlers.save_slot_off_loop(state, replacement, best_effort=False)
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == mode, "the full save relabeled the line"
    assert "memory_store" not in meta, "the full save named a store on a restricted line"
    assert replacement.memory_mode == mode, "the full save left the live replacement persistent"
    assert HKEY in state._restricted_keys
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2", "REPLACEMENT-3"]


@pytest.mark.asyncio
async def test_mixed_case_restricted_line_survives_persistent_recreate(tmp_path) -> None:
    """Case canonicalisation keeps hand-edited restricted metadata restrictive."""
    state = _make_state(tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="incognito")
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200
    assert NAME not in state._slots

    await asyncio.to_thread(
        state.conversation_log.update_metadata,
        HKEY,
        {"memory_mode": "Incognito"},
    )
    line_meta = state.conversation_log.get_metadata(HKEY)
    assert line_meta.get("memory_mode") == "Incognito"
    assert is_incognito_transcript(line_meta.get("memory_mode"))

    replacement = state.get_or_create_slot(NAME)
    replacement.memory_store = "coding"
    _floor_after_disk_tail(state, replacement)
    replacement.append("user", "REPLACEMENT-3")
    replacement.drain()
    assert await handlers.save_slot_off_loop(
        state,
        replacement,
        force=True,
        best_effort=False,
    )

    meta = state.conversation_log.get_metadata(HKEY)
    assert (
        meta.get("memory_mode") == "incognito"
    ), "the mixed-case restricted line was relabeled persistent"
    assert "memory_store" not in meta, "the full save named a store on a restricted line"
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2", "REPLACEMENT-3"]


@pytest.mark.asyncio
async def test_save_canonicalises_a_rehydrated_slot_mode(tmp_path) -> None:
    """A raw mixed-case mode restored onto a slot remains saveable and restricted."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(NAME)
    slot.memory_mode = "Incognito"
    slot.memory_store = "coding"
    slot.append("user", "PRIVATE-1")
    slot.drain()

    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)

    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == "incognito"
    assert "memory_store" not in meta
    assert _disk_contents(state) == ["PRIVATE-1"]


@pytest.mark.asyncio
async def test_persistent_turn_binding_after_restricted_recreate_keeps_line_and_rows(
    tmp_path, monkeypatch
) -> None:
    state = _make_state(tmp_path)
    monkeypatch.setattr(history_mod, "_sessions_dir", lambda: tmp_path)
    original = state.get_or_create_slot(NAME, memory_mode="incognito")
    original.append("user", "PRIVATE-1")
    original.append("assistant", "PRIVATE-2")
    original.drain()
    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
    assert resp.status == 200

    replacement = state.get_or_create_slot(NAME)
    assert replacement.memory_mode == "persistent"
    persistent = execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        "template",
        "kirocrew",
    )
    await asyncio.to_thread(execution_context.bind_session_execution, HKEY, persistent)

    meta = state.conversation_log.get_metadata(HKEY)
    assert meta["memory_mode"] == "incognito"
    assert "memory_store" not in meta
    assert execution_context.EXECUTION_CONTEXT_KEY not in meta
    live = execution_context.read_live_session_execution(HKEY)
    assert live is not None
    assert live.memory_mode == "incognito"
    assert _disk_contents(state) == ["PRIVATE-1", "PRIVATE-2"]


@pytest.mark.asyncio
async def test_delete_handover_of_a_persistent_tail_keeps_a_restricted_line(tmp_path) -> None:
    """The reverse direction commits: the line's stricter mode stays, rows land."""
    state = _make_state(tmp_path)
    original = await _slot_with_committed_and_uncommitted_rows(state)

    entered = asyncio.Event()
    release = asyncio.Event()
    _arm_running_turn(original, entered, release)

    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await _reached(entered, close, seam="the shielded task-cancel wait")
    replacement = state.get_or_create_slot(NAME, memory_mode="incognito")
    assert replacement is not original
    await _publish_metadata(state, replacement, title="REPLACEMENT TITLE", folder="f-r")
    assert state.conversation_log.get_metadata(HKEY).get("memory_mode") == "incognito"
    release.set()
    # flake-ok: the close task ends once the test sets release; moved verbatim, a bounded await is tracked for a de-flake pass
    resp = await close

    assert resp.status == 200
    meta = state.conversation_log.get_metadata(HKEY)
    assert meta.get("memory_mode") == "incognito", "the drain loosened the replacement's mode"
    assert _disk_contents(state) == ["PERSISTED-1", "PERSISTED-2", "TAIL-3", "TAIL-4"]
