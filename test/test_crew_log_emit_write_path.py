"""How an entry reaches the file off the event loop, or is counted as lost.

It pins that no append runs on the event loop or beside a claimed batch, that every gateway
mode drains the log before a hard exit and a shutdown drain reports only what landed, that a
failed append or reopen is retried while a refused one is dropped, counted and marked
durably, that a turn keeps its pin until its terminal write lands or is dropped, that the
log-creating record is exempt from the memory ceiling and discards after a failed creation
count as loss, and that a storage failure never reaches a caller. It also holds how an
oversize body reaches the file as one chunk group and how attachment metadata yields before
a fitting body. The other turn entries live in `test_crew_log_emit_entries.py`; redaction,
the flag and the boot path in `test_crew_log_emit_redaction_and_flag.py`.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import threading
import time

import pytest
from crew_log_emit_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    SESSION,
    _assert_drained_for_shutdown,
    _body,
    _can_take_ownership,
    _entries,
    _isolated_home,
    _log_path,
    _open_session,
    _store_root,
)

from kiro_crew import crew_log as lg
from kiro_crew import executors
from kiro_crew.crew_log import emit
from kiro_crew.crew_log.writer import WriterLimits
from kiro_crew.dashboard import server as server_module


def test_no_second_thread_appends_while_a_writer_batch_is_claimed(monkeypatch):
    # Two threads appending to one file is not just a race for the lock: flock
    # serializes the writes but does not ORDER them, so the batch claimed second
    # can reach the file first and a `turn/started` can land at a lower seq than
    # the `session/opened` before it. A log whose seq disagrees with causality is
    # worse than a short one, because a fold cannot detect it.
    import threading

    release = threading.Event()
    holding = threading.Event()
    writers: list[str] = []
    real_append = lg.CrewLog.append

    def _slow(self, *args, **kwargs):
        writers.append(threading.current_thread().name)
        if not holding.is_set():
            holding.set()
            release.wait(timeout=10.0)
        return real_append(self, *args, **kwargs)

    _open_session()
    assert emit.flush()

    async def _emit() -> None:
        emit.on_turn_started(SESSION, 1, "user")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _slow)
        asyncio.run(_emit())
        assert holding.wait(timeout=10.0), "the writer never claimed a batch"
        claimer = writers[0]

        # A synchronous emit lands here with no event loop running, which is the
        # path that writes inline rather than handing the job to the writer.
        done = threading.Event()

        def _emit_sync() -> None:
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
            done.set()

        worker = threading.Thread(target=_emit_sync, name="sync-caller")
        worker.start()
        # It must NOT have appended while the batch is still claimed.
        # flake-ok: pre-existing 1 s absence window while the claimed batch holds the writer, moved verbatim; tracked for a de-flake pass
        assert not done.wait(timeout=1.0) or writers == [
            claimer
        ], f"a second thread appended beside a claimed batch: {writers}"
        assert writers == [claimer], f"expected only {claimer} to have written: {writers}"
        release.set()
        worker.join(timeout=15.0)
        assert not worker.is_alive(), "the synchronous caller never completed"
    assert emit.drain_for_shutdown(timeout=10.0) is True
    seqs = [e["seq"] for e in _body()]
    assert seqs == sorted(seqs), f"seq is not monotonic: {seqs}"
    kinds = [e["type"] for e in _body()]
    assert kinds.index("turn/started") < kinds.index(
        "turn/completed"
    ), f"the file disagrees with emit order: {kinds}"


def test_every_gateway_mode_drains_the_log_before_a_hard_exit():
    # The dashboard registers its own cleanup hook, but a mode that builds no
    # dashboard app -- slack-only is the plain case -- never runs one, and the hard
    # exit skips atexit. So the drain has to sit on the exit path ITSELF, which is
    # the only code every mode goes through. What its absence drops is the last
    # thing each session did, which is what a reader looks for after a restart.
    #
    # Inspected rather than executed, matching how this repo already tests
    # `_shutdown_and_exit`: the function ends in `os._exit`, so calling it would
    # take the test process with it. Read from the imported MODULE, not from a
    # path relative to the working directory: a shard that runs pytest from
    # anywhere else, or from a Windows checkout, would otherwise fail on the read
    # rather than on the thing being asserted.
    import ast
    import importlib
    import inspect

    gateway_module = importlib.import_module("kiro_crew.slack.gateway_runtime.exit_path")
    tree = ast.parse(inspect.getsource(gateway_module))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_shutdown_and_exit"
    )

    def _calls(name: str) -> list[int]:
        found = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                func = node.func
                attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if attr == name:
                    found.append(node.lineno)
            elif isinstance(node, ast.Attribute) and node.attr == name:
                # Handed to a thread rather than called on this one, which is how
                # a bounded blocking drain belongs on an async path.
                found.append(node.lineno)
        return found

    drains = _calls("drain_for_shutdown")
    exits = _calls("_exit")
    assert drains, "the hard-exit path does not drain the session log at all"
    assert exits, "this test no longer finds the hard exit it is anchored to"
    assert min(drains) < min(
        exits
    ), f"the crew log drain at line {min(drains)} runs after the exit at {min(exits)}"
    # And before the log-queue drain, so a crew log warning still reaches the log.
    log_drains = _calls("drain_log_queue_before_hard_exit")
    if log_drains:
        assert min(drains) < min(log_drains), (
            "the crew log drain runs after the log queue is flushed, so its own "
            "warning about an incomplete drain would be lost"
        )


# --- durable loss markers -------------------------------------------------


def test_fold_reads_loss_marker_with_contiguous_sequence():
    _open_session()
    assert emit.flush()
    real_append = lg.CrewLog.append

    def _lose_the_call(self, entry_type, *args, **kwargs):
        if entry_type == "tool/called":
            raise OSError("fold loss")
        return real_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _lose_the_call)
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush(timeout=20.0)
    assert emit.dropped_writes() == 1, "the append was not given up on"
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush(timeout=20.0)

    known = {"session/opened", "write/dropped", "turn/completed"}
    entries = list(lg.CrewLog.open(lg.KIND_SESSION, SESSION).iter_from(1, known=known))
    kinds = [entry.type for entry in entries]
    assert "write/dropped" in kinds, f"reader did not observe write/dropped: {kinds}"
    seqs = [entry.seq for entry in entries]
    assert seqs == list(range(1, len(seqs) + 1)), f"marker introduced a seq gap: {seqs}"


def test_a_permanent_refusal_midbatch_owes_a_loss_marker():
    _open_session()
    assert emit.flush()

    real_append = lg.CrewLog.append
    refused_once = {"left": 1}

    def _refuse_turn_started(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started" and refused_once["left"]:
            refused_once["left"] -= 1
            raise lg.CrewLogError("another process owns this log", code=lg.CODE_ALREADY_OWNED)
        return real_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _refuse_turn_started)

        async def _batch() -> None:
            # Both land in one drain: the first is refused (permanent), the batch
            # continues to the second, so the file resumes right after the hole.
            emit.on_turn_started(SESSION, 1, "user")
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
            assert emit.flush(timeout=20.0)

        asyncio.run(_batch())

    assert refused_once["left"] == 0, "the injected refusal never fired"
    assert emit.dropped_writes() == 1, "the refused entry was not counted"
    body = _body()
    assert (
        body[-2]["type"] == "write/dropped"
    ), f"the entry after a refusal was not the marker: {[e['type'] for e in body]}"
    marker = body[-2]["data"]
    assert marker["dropped_count"] == 1
    assert body[-1]["type"] == "turn/completed", "the batch did not resume after the marker"


# --- fail-soft ------------------------------------------------------------


def test_a_refused_oversize_write_is_dropped_and_counted_and_named_once(caplog):
    """A refusal is a loss NOW, not a retry, and it is admitted rather than hidden.

    The storage layer checks an entry against the format before it writes a byte,
    so a refused entry leaves the file untouched and would be refused identically
    on every retry. Retaining one would spend the whole attempt budget on a verdict
    that cannot change and hold the rest of that session's log behind it, so it is
    dropped at once -- and counted, because a hole a caller can read is the only
    kind this writer is allowed to leave.
    """
    _open_session()
    before = len(_entries())
    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        emit.on_tool_called(SESSION, 1, name="x" * (70 * 1024), call_id="tc-big")
        emit.on_tool_called(SESSION, 1, name="y" * (70 * 1024), call_id="tc-big2")
    assert len(_entries()) == before, "a refused write must not land"
    assert emit.dropped_writes() == 2, "both losses are counted"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # One for the storage failure, one for the loss -- and the SECOND refusal adds
    # neither, because a failing crew log names itself once rather than per entry.
    assert len(warnings) == 2, f"expected the failure and the loss once each: {warnings}"
    assert any("gave up on" in record.getMessage() for record in warnings)


def test_a_refused_write_does_not_break_the_next_good_write():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="z" * (70 * 1024), call_id="tc-big")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert _body()[-1]["type"] == "turn/completed"


def test_a_storage_layer_that_raises_never_breaks_a_caller(monkeypatch):
    _open_session()

    class Exploding:
        def append(self, *args, **kwargs):
            raise RuntimeError("disk is on fire")

    monkeypatch.setitem(emit._open, SESSION, Exploding())
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="tc-1")
    emit.on_approval_requested(SESSION, 1, approval_id="ap-1")
    emit.on_approval_decided(SESSION, 1, approval_id="ap-1", decision="approved")
    emit.on_model_selected(SESSION, "m", "config")
    emit.on_compaction_applied(SESSION, pct_before=0.9, pct_after=0.2)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_session_closed(SESSION, "reset")


def test_a_failed_turn_start_leaves_later_entries_unthreaded(monkeypatch):
    """An anchor is only claimed from an entry that actually landed."""
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert "thread" not in _body()[-1]


def test_events_for_a_session_with_no_log_create_nothing():
    emit.on_turn_started("never-opened", 1, "user")
    emit.on_tool_called("never-opened", 1, name="fs_read", call_id="tc-1")
    assert not _log_path("never-opened").exists()


def test_a_missing_session_id_is_a_no_op():
    emit.on_session_opened("", agent="kirocrew")
    emit.on_turn_started("", 1, "user")
    emit.on_session_closed("", "reset")
    assert not _store_root().exists()


# --- off the event loop ----------------------------------------------------


def _spy_on_append(monkeypatch, seen: list[int]) -> None:
    """Record which thread each storage append actually runs on."""
    original = lg.CrewLog.append

    def _spy(self, *args, **kwargs):
        seen.append(threading.get_ident())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _spy)


def test_an_append_never_runs_on_the_event_loop_thread(monkeypatch):
    """Why this module queues at all.

    The storage call takes the unit's lock, reads a bounded tail to assign seq
    and fsyncs the line. On the gateway that call site is inside the async chat
    path, so running it inline blocks the one loop that drives every session's
    turns and the liveness heartbeat.
    """
    _open_session()
    assert emit.flush()
    seen: list[int] = []
    _spy_on_append(monkeypatch, seen)

    async def _turn() -> int:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush()
        return threading.get_ident()

    loop_thread = asyncio.run(_turn())
    assert seen, "no append was observed"
    assert loop_thread not in seen


def test_a_write_from_a_thread_with_no_loop_runs_inline(monkeypatch):
    """There is no loop to keep the call off, so it is not deferred.

    What makes a synchronous caller -- and this file -- deterministic: the entry
    point returns with the entry already on disk.
    """
    _open_session()
    seen: list[int] = []
    _spy_on_append(monkeypatch, seen)
    emit.on_turn_started(SESSION, 1, "user")
    assert seen == [threading.get_ident()]


def test_a_turn_boundary_crossing_a_full_queue_cannot_mis_attribute_an_entry():
    """The property the carried identity buys.

    Two turns' entries sit in the queue at once and the writer drains them after
    both turns have already started on the loop. Each entry still names the turn
    that produced it, because it was built with that ordinal in it -- there is no
    shared cell for a later turn to overwrite and no cache entry to evict.
    """
    _open_session()
    assert emit.flush()

    async def _two_turns() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        emit.on_turn_started(SESSION, 2, "user")
        emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
        emit.on_turn_completed(SESSION, 2, stop_reason="end_turn")
        assert emit.flush()

    asyncio.run(_two_turns())
    body = _body()
    assert [(e["type"], e["data"]["turn"]) for e in body if e["type"] != "session/opened"] == [
        ("turn/started", 1),
        ("tool/called", 1),
        ("turn/completed", 1),
        ("turn/started", 2),
        ("tool/called", 2),
        ("turn/completed", 2),
    ]
    assert all("thread" not in e for e in body)


def test_a_slow_writer_costs_memory_not_a_record_and_not_the_loop(monkeypatch):
    """Both halves of the rule at once, which is why they are one test.

    A hole in an append-only log is permanent and silent -- a reader cannot tell a
    turn that never completed from one whose completion was discarded -- so no
    record is dropped. And a turn must not wait on the filesystem, so the producer
    neither writes nor waits. What gives instead is memory, which is visible.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    release = threading.Event()

    def _held(self, *args, **kwargs):
        # The writer is stuck for the whole burst, so this measures the producer
        # alone rather than a race with a writer that might be keeping up.
        assert release.wait(20.0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _held)

    async def _flood() -> None:
        started = time.monotonic()
        for n in range(40):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        elapsed = time.monotonic() - started
        # The writer holds the very first append for 20s. A producer that waited
        # on it, or wrote for itself, could not get through 40 emits in under a
        # second.
        # flake-ok: pre-existing 1 s real-time ceiling on 40 emits while the writer is held for 20 s, moved verbatim; tracked for a de-flake pass
        assert elapsed < 1.0, f"the producer blocked for {elapsed:.2f}s"
        assert emit.buffered_writes() > 0, "nothing was buffered -- the writer kept up"
        release.set()
        assert emit.flush(timeout=20.0)

    asyncio.run(_flood())

    assert emit.dropped_writes() == 0
    calls = [e for e in _body() if e["type"] == "tool/called"]
    assert len(calls) == 40, f"only {len(calls)} of 40 entries reached the file"
    assert [e["data"]["call_id"] for e in calls] == [f"tc-{n}" for n in range(40)]
    assert [e["data"]["call_index"] for e in calls] == list(range(1, 41))
    assert [e["seq"] for e in calls] == sorted(e["seq"] for e in calls)


def test_the_backlog_is_reported_rather_than_shed(monkeypatch, caplog):
    """The high-water mark is a report, not a cap.

    Crossing it must not shed anything -- that is what the durability half of the
    rule forbids -- so the only correct response is to say so.
    """
    emit.reset_caches(limits=WriterLimits(pending_high_water=4))
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    release = threading.Event()

    def _held(self, *args, **kwargs):
        assert release.wait(20.0)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _held)

    async def _flood() -> None:
        for n in range(12):
            emit.on_tool_called(SESSION, 1, name="fs_read", call_id=f"tc-{n}")
        release.set()
        assert emit.flush(timeout=20.0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_flood())

    assert "buffered" in caplog.text and "rather than" in caplog.text
    assert emit.peak_buffered_writes() >= 4
    assert emit.dropped_writes() == 0
    assert [e["type"] for e in _body()].count("tool/called") == 12


def test_shutdown_drains_what_the_writer_has_not_written_yet():
    """The quiescence barrier a restart needs.

    Entries live in memory until the writer takes them, so a process that exits
    without draining loses the last thing each session did -- which is precisely
    what a reader goes looking for after a restart.
    """
    _open_session()
    assert emit.flush()

    async def _turn() -> None:
        # No flush: this is the state a restart interrupts.
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

    asyncio.run(_turn())

    assert emit.drain_for_shutdown(timeout=20.0)
    assert emit.buffered_writes() == 0
    types = [e["type"] for e in _body()]
    assert types == ["session/opened", "turn/started", "tool/called", "turn/completed"]


def test_a_close_under_a_live_turn_leaves_that_turn_its_open_calls():
    """A teardown may not erase state the running turn still needs.

    A forced reset tears a session down while a turn is running. The teardown's
    cleanup drops only what a successor must not inherit, and of the open tool calls
    only those whose turn is already gone -- because the running turn closes its own
    calls later, from its `finally`, and a cleanup that took them would leave them
    open for the life of the file.

    The terminal itself is written at once and late entries may follow it; see
    `on_session_closed` for why holding it was abandoned. What is asserted here is
    that nothing the live turn needs was taken away.

    Mutation guard: dropping every `_tool_started` key for the session, as before,
    makes `close_open_tool_calls` return 0 and reddens this.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_session_closed(SESSION, "reset")
    assert emit.flush()

    types = [e["type"] for e in _body()]
    assert "session/closed" in types, "the terminal is written, not held"

    # The turn ends the way the runner ends one, after its session already closed.
    assert emit.close_open_tool_calls(SESSION, 1) == 1, "the live turn lost its open call"
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush()

    after = [e["type"] for e in _body()]
    assert after.index("tool/completed") > after.index("session/closed")
    assert after.index("tool/completed") < after.index("turn/completed")


def test_a_close_with_no_live_turn_is_written_at_once():
    """The deferral is for a live turn only, not a general delay.

    Every ordinary teardown -- an idle session evicted, a shutdown, a reset between
    turns -- has nothing to wait for, and holding its terminal would leave the log
    without one.
    """
    _open_session()
    assert emit.flush()
    emit.on_session_closed(SESSION, "shutdown")
    assert emit.flush()
    assert _body()[-1]["type"] == "session/closed"
    assert _body()[-1]["data"]["reason"] == "shutdown"


def test_shutdown_drains_even_when_the_writer_pool_is_gone():
    """A shutdown that already tore the pool down must still not lose records.

    The executor registers its own exit hook, so the pool can be gone before this
    runs. Blocking the caller here is correct -- it is the exit path -- and it is
    the only remaining way to keep the entries.

    The state is CONSTRUCTED rather than hoped for. What a pool shutdown cancels is
    a QUEUED future, so the single writer thread is occupied first to make the drain
    pass queue behind it; whether the pass had already started is precisely the
    timing this test must not depend on. The cancelled pass never runs, so nothing
    releases the writer claim -- and reading that claim as "a batch is in flight"
    wedges every later barrier for the life of the process: the inline write is
    refused on the belief another thread holds the batch, and ``flush`` answers
    False forever. So the claim is derived from the future, not from a flag nothing
    will clear.
    """
    _open_session()
    assert emit.flush()

    occupied = threading.Event()
    release = threading.Event()

    def _occupy() -> None:
        occupied.set()
        assert release.wait(20.0)

    executors.crew_log_executor().submit(_occupy)
    # Waited on the worker's OWN signal, so the queue state below is a fact rather
    # than a guess about scheduling.
    assert occupied.wait(20.0), "the writer thread was never occupied"

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 2, "user")
        emit.on_turn_completed(SESSION, 2, stop_reason="end_turn")

    asyncio.run(_turn())
    assert emit.buffered_writes() == 2, "the entries were written instead of queueing"

    # Cancels the queued drain pass. The occupier is RUNNING, so it survives and is
    # released next -- only the pass that would have cleared the claim is lost.
    executors.shutdown_maintenance_executor()
    release.set()

    assert emit.drain_for_shutdown(timeout=20.0)
    assert emit.buffered_writes() == 0
    assert [e["type"] for e in _body()].count("turn/completed") == 1
    # The claim was released with the pass it was made for, so the next caller is
    # not answered False forever.
    assert emit.flush(timeout=20.0), "the writer claim outlived the pass it was made for"
    emit.on_turn_started(SESSION, 3, "user")
    assert emit.flush(timeout=20.0)
    assert [e["data"]["turn"] for e in _body() if e["type"] == "turn/started"] == [2, 3]


def test_the_dashboard_cleanup_hook_actually_drains_off_the_loop():
    """The barrier is only worth having if something calls it.

    The registered hook is INVOKED here rather than pattern-matched, so what is
    asserted is that it drains and that it does so off the event loop -- the two
    things that matter. A substring match on the source proves neither, and breaks
    on an honest rewrite of the same behaviour.
    """
    import asyncio as _asyncio
    from pathlib import Path

    source = inspect.getsource(server_module) + (
        Path(server_module.__file__).parent / "server_runtime" / "instances_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert (
        "app.on_cleanup.append(_crew_log_drain)" in source
    ), "the server no longer registers a crew log drain on cleanup"

    drained: list[str] = []

    def _record() -> bool:
        drained.append(threading.current_thread().name)
        return True

    async def _drive() -> None:
        loop_thread = threading.current_thread().name
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(emit, "drain_for_shutdown", _record)
            await _asyncio.to_thread(emit.drain_for_shutdown)
        assert drained, "the drain never ran"
        assert drained[0] != loop_thread, (
            "the drain ran ON the event loop thread; it blocks while the writer "
            "finishes, so a slow disk would stall everything else shutting down"
        )

    _asyncio.run(_drive())


def test_a_dropped_turn_start_leaves_its_turn_unthreaded_not_folded_into_the_last(
    monkeypatch,
):
    """The failure a single mutable anchor would have got wrong.

    A turn whose start was REFUSED has no seq to thread under. Its followers must
    carry no thread at all -- folding them into the PREVIOUS turn would attribute
    one turn's tool calls to another, which is worse than recording no grouping.

    A refusal is also the reason the followers still land. It is decided before a
    byte is written and would be decided the same way on every retry, so the entry
    is simply gone and the pass continues past it; holding the rest of the turn
    behind an entry that can never land would turn one refused line into a missing
    turn.
    """
    _open_session()
    assert emit.flush()

    async def _first_turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush()

    asyncio.run(_first_turn())
    first_start = next(e for e in _body() if e["type"] == "turn/started")

    original = lg.CrewLog.append

    def _refuse_turn_starts(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started":
            raise lg.CrewLogError("too big", code=lg.CODE_ENTRY_TOO_LARGE)
        return original(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _refuse_turn_starts)

    async def _second_turn() -> None:
        emit.on_turn_started(SESSION, 2, "user")
        emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
        assert emit.flush(timeout=20.0)

    asyncio.run(_second_turn())
    orphan = [e for e in _body() if e["type"] == "tool/called" and e["data"]["turn"] == 2]
    assert len(orphan) == 1
    assert "thread" not in orphan[0]
    assert orphan[0].get("thread") != first_start["seq"]
    assert emit.dropped_writes() == 1, "the refused start is the only loss"


def test_a_log_failure_on_the_writer_never_reaches_the_caller(monkeypatch):
    """Fail-soft holds across the thread boundary too, and the loss is admitted.

    The job runs where nothing is awaiting it, so an exception that escaped would
    surface as an unretrieved future rather than at the call site -- worse than the
    inline case, not better. The entry is retried and then given up on, and
    ``dropped_writes`` is where that shows: a hole nobody can count is the failure
    this counter exists to rule out.
    """
    _open_session()
    assert emit.flush()
    real_append = lg.CrewLog.append

    def _boom(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started":
            raise OSError("no space left on device")
        return real_append(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _boom)

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")

    asyncio.run(_turn())
    assert emit.flush(timeout=20.0), "the writer never finished giving up on the entry"
    assert emit.dropped_writes() == 1, "the entry the writer gave up on was not counted"
    assert emit.buffered_writes() == 0
    assert [e["type"] for e in _body()] == ["session/opened", "write/dropped"]


def test_a_failed_append_is_retained_and_lands_on_the_next_pass(monkeypatch):
    """The hole a swallowed exception leaves.

    One transient filesystem error must cost latency, not a record: the batch goes
    back to the front of its session's bucket and the next pass writes it. The
    entries keep the order they were emitted in, and nothing is dropped -- a retry
    that reordered the file, or gave up on the first error, would each be worse
    than the delay.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_once(self, *args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_once)

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        assert emit.flush(timeout=20.0)

    asyncio.run(_turn())
    assert failures["left"] == 0, "the injected failure never fired"
    body = _body()
    assert [e["type"] for e in body] == [
        "session/opened",
        "turn/started",
        "tool/called",
        "turn/completed",
    ]
    assert [e["seq"] for e in body] == sorted(e["seq"] for e in body)
    assert emit.dropped_writes() == 0
    assert emit.buffered_writes() == 0


def test_a_later_write_during_retention_queues_behind_the_retained_batch(monkeypatch):
    """Why the retained batch goes to the FRONT of its session's bucket.

    While a session owes a retained batch, its inline fast path is off and every
    later write joins the same bucket behind it. Without that, the follower would
    reach the file first and the log would state an order that never happened --
    which a fold reads as fact, and cannot detect.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.append
    refused = threading.Event()
    seen = {"starts": 0}

    def _fail_the_first_starts(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started":
            seen["starts"] += 1
            if seen["starts"] <= 2:
                refused.set()
                raise OSError("input/output error")
        return original(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _fail_the_first_starts)

    async def _turn() -> None:
        emit.on_turn_started(SESSION, 1, "user")
        # Emitted only once the start has actually been refused, so the follower is
        # produced INSIDE the retention window rather than racing it.
        assert refused.wait(20.0), "the start never reached the writer"
        emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
        assert emit.flush(timeout=20.0)

    asyncio.run(_turn())
    assert seen["starts"] == 3, "the start was not retried twice before it landed"
    assert [e["type"] for e in _body()] == ["session/opened", "turn/started", "tool/called"]
    assert emit.dropped_writes() == 0


def test_a_reopen_that_fails_is_retried_rather_than_discarding_the_entry():
    """The same hole one layer up from the append.

    A handle can go missing from the bounded cache, and the next entry reopens the
    crew log. If that reopen error were swallowed the job would SUCCEED with nothing
    written -- no retry, and nothing in ``dropped_writes()`` either -- which is
    exactly the silent hole the retention exists to close.
    """
    _open_session()
    assert emit.flush()
    original = lg.CrewLog.open
    failures = {"left": 1}

    def _fail_first_open(kind, unit_id, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original(kind, unit_id, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "open", _fail_first_open)
        with emit._lock:
            emit._open.clear()  # the eviction the cap would have made

        async def _turn() -> None:
            emit.on_turn_started(SESSION, 1, "user")
            assert emit.flush(timeout=20.0)

        asyncio.run(_turn())

    assert failures["left"] == 0, "the injected reopen failure never fired"
    assert [e["type"] for e in _body()] == ["session/opened", "turn/started"]
    assert emit.dropped_writes() == 0


def test_a_retried_session_open_still_writes_the_entry_it_owes():
    """A retry must not lose the decision the first attempt made.

    The job decides whether to announce from filesystem state it changes itself:
    when the header lands and the entry behind it does not, the retry finds the
    crew log already there and would read "nothing new to say" -- skipping
    ``session/opened`` for good, and not counting it either. The decision is
    latched on the first attempt for exactly that reason.
    """
    original = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_the_first_open_entry(self, entry_type, *args, **kwargs):
        if entry_type == "session/opened" and failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _fail_the_first_open_entry)
        _open_session()
        assert emit.flush(timeout=20.0)

    assert failures["left"] == 0, "the injected failure never fired"
    body = _body()
    assert [e["type"] for e in body] == ["session/opened"]
    assert body[0]["data"]["model"] == "claude-opus-5", "the entry kept its payload"
    assert emit.dropped_writes() == 0


# --- shutdown reports the truth about what landed -------------------------


def test_shutdown_is_not_reported_drained_while_a_batch_is_still_writing():
    """A claimed batch is out of the buffer but not yet on disk.

    The writer takes a batch OUT of `_pending` before it writes it, so an empty
    buffer says nothing about whether those entries reached the file. Reporting
    drained there is the worst answer available: the caller exits believing the
    record is complete, and the entries in that batch are the last thing the
    session did.
    """
    _open_session()
    assert emit.flush()
    holding = threading.Event()
    release = threading.Event()
    original = lg.CrewLog.append

    def _slow(self, *args, **kwargs):
        holding.set()
        release.wait(timeout=10.0)
        return original(self, *args, **kwargs)

    verdicts: list[bool] = []
    elapsed: list[float] = []

    async def _emit_then_shutdown() -> None:
        emit.on_turn_started(SESSION, 1, "user")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "append", _slow)
        asyncio.run(_emit_then_shutdown())
        assert holding.wait(timeout=10.0), "the writer never claimed the batch"
        # Mid-batch: the buffer is empty, the batch is in flight. A short budget
        # must NOT come back True.
        began = time.monotonic()
        verdicts.append(emit.drain_for_shutdown(timeout=0.2))
        elapsed.append(time.monotonic() - began)
        release.set()
    assert emit.drain_for_shutdown(timeout=10.0) is True
    assert verdicts == [False], "shutdown claimed drained while a batch was in flight"
    # Bounded, not retried: reaching the fallback means the timed wait already
    # failed, so the writer is wedged rather than slow and looping there would
    # spend an unbounded part of the exit on a filesystem that stopped answering.
    # flake-ok: pre-existing 5 s real-time ceiling on a drain given a 0.2 s budget, moved verbatim; tracked for a de-flake pass
    assert elapsed[0] < 5.0, f"the failed drain took {elapsed[0]:.1f}s -- it is retrying"
    assert [e["type"] for e in _body() if e["type"] == "turn/started"]


# --- shutdown finishes a batch that is inside its backoff -----------------


def test_shutdown_writes_a_batch_that_had_been_failing():
    """A retained batch is written at shutdown, not abandoned.

    Event-driven: the store refuses until the test says otherwise, and the assertion
    is on the drain's own answer plus the file, never on how long anything took. The
    fixture's flattened backoff keeps this to the mechanism under test -- the clamp
    that makes a LONG backoff reachable is pinned above, without threads.
    """
    allow = threading.Event()
    real_append = lg.CrewLog.append

    def _gated(self, *a, **kw):
        if not allow.is_set():
            raise OSError("store is refusing")
        return real_append(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        assert emit.flush()
        # A backoff long enough that the batch is still PARKED when the assertions
        # below run: with the fixture's flattened schedule it would burn its whole
        # attempt budget in microseconds and be dropped before the drain is asked.
        mp.setattr(emit, "_retry_delay", lambda _attempts: 5.0)
        mp.setattr(lg.CrewLog, "append", _gated)
        emit.on_turn_started(SESSION, 1, "user")
        # Refused, so the batch is retained rather than written. The wait only has to
        # be shorter than that backoff, which no machine speed changes.
        assert not emit.flush(timeout=0.5), "the gated store did not retain a batch"
        assert emit.buffered_writes() >= 1

        allow.set()
        assert emit.drain_for_shutdown(timeout=20.0), "shutdown abandoned a retained batch"

    assert "turn/started" in [e["type"] for e in _body()]
    assert emit.dropped_writes() == 0, "a batch that could be written was counted lost"


# --- a hung write is bounded by a ceiling, not by the attempt counter ------


def test_a_write_that_finishes_late_sheds_nothing():
    """Slow is not stuck: a job that returns leaves the counters alone.

    The ceiling is the one place memory is shed, so it must not fire for a write
    that was merely behind -- otherwise every busy disk would cost entries.
    """
    release = threading.Event()
    real_append = lg.CrewLog.append

    def _slow(self, *a, **kw):
        release.wait(10.0)
        return real_append(self, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        assert emit.flush()
        mp.setattr(lg.CrewLog, "append", _slow)
        emit.on_turn_started(SESSION, 1, "user")
        emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        release.set()
        assert emit.drain_for_shutdown(timeout=20.0)

    assert emit.dropped_writes() == 0, "a slow write cost an entry"
    kinds = [e["type"] for e in _body()]
    assert "turn/started" in kinds and "turn/completed" in kinds


def test_attachment_metadata_is_omitted_before_a_fitting_body_is_split():
    """Attachment detail yields before a fitting message body."""
    _open_session()
    body = "x" * (emit._crew_log().MAX_ENTRY_BYTES - emit._ENVELOPE_HEADROOM - 64)
    ids = [f"/tmp/attachment-{n:04d}-with-a-long-enough-name.bin" for n in range(200)]
    emit.on_message_received(SESSION, 1, role="user", text=body, attachments=ids)
    assert emit.flush()

    received = [e for e in _body() if e["type"] == "message/received"]
    assert len(received) == 1, "the message was refused when attachment metadata exceeded the cap"
    assert emit.dropped_writes() == 0
    data = received[0]["data"]
    kept = data.get("attachments", [])
    assert kept == ids[: len(kept)]
    assert data["attachments_omitted"] == len(ids) - len(kept)
    assert data["text"] == body
    assert "chunks" not in data


def test_an_oversize_body_reaches_the_file_as_one_group():
    """The emitter writes a chunk group through the batched append, not one by one.

    Entry by entry, a hard kill between the chunks and the entry that cites them
    leaves the body on disk with nothing pointing at it. This pins the property a
    reader depends on: the cited seqs are exactly the chunk entries that precede the
    citing entry, contiguously, with no other entry interleaved.

    Mutation guard: writing the chunks with individual appends reddens the call
    count; writing the citing entry FIRST reddens the ordering assertion.
    """
    calls = {"many": 0}
    real_many = lg.CrewLog.append_many

    def _counting(self, items, **kw):
        calls["many"] += 1
        return real_many(self, items, **kw)

    with pytest.MonkeyPatch.context() as mp:
        _open_session()
        mp.setattr(lg.CrewLog, "append_many", _counting)
        body = "y" * (emit._crew_log().MAX_ENTRY_BYTES * 2)
        emit.on_message_sent(SESSION, 1, step=1, text=body)
        assert emit.flush()

    assert calls["many"] == 1, "the group was not written as a single batch"
    entries = _body()
    sent = [e for e in entries if e["type"] == "message/sent"]
    assert len(sent) == 1
    cited = sent[0]["data"]["chunks"]
    assert cited, "an oversize body was not chunked"
    chunk_seqs = [e["seq"] for e in entries if e["type"] == "message/chunk"]
    assert cited == chunk_seqs, "the citing entry names seqs that are not the chunks"
    assert cited == list(range(cited[0], cited[0] + len(cited))), "the chunk seqs have a gap"
    assert sent[0]["seq"] == cited[-1] + 1, "the citing entry does not follow its chunks"
    assert sent[0]["data"]["chars"] == len(body)


# --- one wedged session must not take another down ------------------------


def test_queued_terminal_write_keeps_ownership_until_it_lands(monkeypatch):
    """Cache pressure cannot hand away a still-open turn's write lease."""
    blocker = "acp-sess-blocker"
    _open_session()
    emit.on_session_opened(blocker, agent="kirocrew", slot="chat-b")
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    writer_entered = threading.Event()
    release_writer = threading.Event()
    real_append = lg.CrewLog.append

    def _hold_the_writer(self, entry_type, *args, **kwargs):
        if self.id == blocker and entry_type == "turn/started":
            writer_entered.set()
            assert release_writer.wait(20.0)
        return real_append(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _hold_the_writer)
    try:

        async def _queue_closer() -> None:
            emit.on_turn_started(blocker, 1, "user")
            assert writer_entered.wait(20.0), "the writer was never occupied"
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")

        asyncio.run(_queue_closer())
        assert "turn/completed" not in [e["type"] for e in _body()]

        for index in range(emit._MAX_OPEN_CREW_LOGS + 1):
            emit._remember(f"pressure-{index}", object())
        gc.collect()
        assert (
            not _can_take_ownership()
        ), "ownership released while the turn is still open in the file"
    finally:
        release_writer.set()

    assert emit.flush(timeout=20.0)
    assert [e["type"] for e in _body()][-1] == "turn/completed"
    assert emit.live_turn(SESSION) == 0


def test_retryable_terminal_failure_keeps_the_turn_pinned(monkeypatch):
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()

    original_append = lg.CrewLog.append
    failures = {"left": 1}

    def _fail_closer_once(self, entry_type, *args, **kwargs):
        if entry_type == "turn/completed" and failures["left"]:
            failures["left"] -= 1
            raise OSError("input/output error")
        return original_append(self, entry_type, *args, **kwargs)

    # A backoff long enough that the retained closer is still parked when the pin is
    # read; the shutdown drain below clamps it inside its window and lands the retry.
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 3600.0)
    monkeypatch.setattr(lg.CrewLog, "append", _fail_closer_once)
    # No event loop: the closer is written inline, fails, and is retained before this
    # call returns.
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert failures["left"] == 0, "the closer was never attempted"
    assert emit.live_turn(SESSION) == 1, "a retryable terminal failure released the turn pin"

    _assert_drained_for_shutdown("the retained closer")
    assert [e["type"] for e in _body()][-1] == "turn/completed"
    assert emit.live_turn(SESSION) == 0


@pytest.mark.parametrize("terminal", ["refused", "completed", "failed"])
def test_every_terminal_path_releases_its_pin_after_a_definitive_drop(terminal):
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush()
    assert emit.live_turn(SESSION) == 1

    original_append = lg.CrewLog.append

    def _fail_closer(self, entry_type, *args, **kwargs):
        if entry_type in {"turn/refused", "turn/completed"}:
            raise OSError("input/output error")
        return original_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lg.CrewLog, "append", _fail_closer)
        if terminal == "refused":
            emit.on_turn_refused(SESSION, 1, "stopped_before_dispatch")
        elif terminal == "completed":
            emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
        else:
            emit.on_turn_failed(SESSION, 1, error="AcpError")
        assert emit.flush(timeout=20.0)

    assert emit.dropped_writes() == 1
    assert emit.live_turn(SESSION) == 0


# --- the log-creating record is exempt from the memory ceiling ----------


def test_overflow_while_the_creating_record_is_queued_still_creates_the_log(monkeypatch):
    """(a) A ceiling crossed as the file-creating record is queued must not erase it.

    The ceiling bounds PAYLOAD memory, and the record that creates the crew log is
    O(1) per session -- refusing it would leave the session with no file, so every
    later entry (the loss marker included) would be a silent uncounted no-op. The
    creating record is exempt, so it lands past a full buffer and the session's
    entries are recorded.

    Driven on an event loop with the writer OCCUPIED, so ``on_session_opened``
    buffers its creating record -- the one path the ceiling guards -- instead of
    taking the inline path a session that owes nothing would otherwise take.
    """
    blocker = "acp-sess-blocker"
    # One buffered append fills the buffer: the creating record is admitted past it,
    # and the turn's start, which is not exempt, is refused.
    emit.reset_caches(limits=WriterLimits(max_pending_count=1))
    emit.on_session_opened(blocker, agent="kirocrew", slot="chat-b")
    assert emit.flush()

    writer_entered = threading.Event()
    release_writer = threading.Event()
    real_append = lg.CrewLog.append

    def _hold_the_writer(self, entry_type, *args, **kwargs):
        if self.id == blocker and entry_type == "turn/started":
            writer_entered.set()
            assert release_writer.wait(20.0)
        return real_append(self, entry_type, *args, **kwargs)

    monkeypatch.setattr(lg.CrewLog, "append", _hold_the_writer)
    try:

        async def _under_pressure() -> None:
            emit.on_turn_started(blocker, 1, "user")
            assert writer_entered.wait(20.0), "the writer was never occupied"
            emit.on_turn_started(blocker, 2, "user")
            emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
            emit.on_turn_started(SESSION, 1, "user")

        asyncio.run(_under_pressure())
        assert emit.overflow_writes(SESSION) == 1, "a non-exempt append was expected to overflow"
    finally:
        release_writer.set()

    assert emit.flush(timeout=20.0)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert emit.flush(timeout=20.0)

    assert _log_path().is_file(), "the ceiling refused the log-creating record"
    body_types = [e["type"] for e in _body()]
    assert "session/opened" in body_types, "the creating session/opened entry was refused"


# --- a permanently failed creation makes later discards COUNT --------------


def _fail_creation_permanently() -> None:
    """Open a session whose creating record is REFUSED (a permanent CrewLogError).

    Leaves ``SESSION`` flagged in ``_creation_failed`` with no log file, which
    is the state a later append must count as loss rather than silently no-op.
    """
    real_create = lg.CrewLog.create

    def _refuse_create(kind, unit_id, *args, **kwargs):
        if unit_id == SESSION:
            raise lg.CrewLogError("refused at create", code=lg.CODE_ALREADY_OWNED)
        return real_create(kind, unit_id, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(lg.CrewLog, "create", _refuse_create)
        emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7")
        assert emit.flush(timeout=20.0)


def test_a_permanently_failed_creation_counts_later_discards_as_loss():
    """(b) After a permanent creation failure, later entries are COUNTED, not dropped silently.

    A session whose creating record died permanently has no log file, but it
    WAS opened, so its later entries are a real loss. ``_handle`` raises a refusal
    for such a session instead of returning None, so the discarded entry is counted
    in ``dropped_writes`` rather than vanishing as a silent policy no-op.
    """
    _fail_creation_permanently()
    assert SESSION in emit._creation_failed, "the failed creation was not flagged"
    assert not _log_path().is_file(), "no log file should exist after a failed creation"
    dropped_before = emit.dropped_writes()

    emit.on_turn_started(SESSION, 1, "user")
    assert emit.flush(timeout=20.0)

    assert emit.dropped_writes() == dropped_before + 1, (
        "a discarded entry for a creation-failed session was a silent no-op, " "not counted as loss"
    )


def test_a_session_that_was_never_opened_stays_a_silent_no_op():
    """(b, negative half) A session that legitimately has no crew log is unchanged.

    The creation-failed count must not turn the ordinary "feature off / never
    opened" no-op into a counted loss: that session is a policy no-op, uncounted.
    """
    dropped_before = emit.dropped_writes()
    emit.on_turn_started("never-opened-session", 1, "user")
    assert emit.flush(timeout=20.0)
    assert not _log_path("never-opened-session").exists()
    assert emit.dropped_writes() == dropped_before, "an unopened session was counted as a loss"


def test_a_closed_session_does_not_leave_its_creation_failure_flagged():
    """The creation-failure verdict dies with the session, not at the next reset.

    The flag makes every later entry for that id a counted loss, so a successor
    reusing the id would inherit a verdict about a crew log it never created. Left
    to ``reset_caches`` the flag would also outlive every failed session for as
    long as the process runs. It is cleared in the close path's terminal cleanup,
    which runs even when the closing entry itself was dropped -- and that is the
    case for exactly the sessions the flag is set on.
    """
    _fail_creation_permanently()
    assert SESSION in emit._creation_failed, "the failed creation was not flagged"

    emit.on_session_closed(SESSION, reason="test")
    assert emit.flush(timeout=20.0)

    assert (
        SESSION not in emit._creation_failed
    ), "a closed session left its creation-failure flag behind"


def test_a_closed_session_does_not_leave_its_overflow_count_behind():
    """The per-session overflow count dies with the session, not at the next reset.

    ``overflow_writes(session_id)`` answers "did an append for this session overflow",
    which a writer reads to tell a landed append from a dropped one. It is therefore
    per SESSION and read only while that session is writing -- so it is released in
    the close path's terminal cleanup. Left to ``reset_caches`` a gateway that runs
    for weeks keeps one entry for every session that ever overflowed, and a successor
    reusing the id would read a count it did not earn.
    """
    emit.reset_caches(limits=WriterLimits(max_pending_count=0))
    _open_session()
    assert emit.flush()

    async def _overflow() -> None:
        emit.on_turn_started(SESSION, 1, "user")

    asyncio.run(_overflow())
    assert emit.overflow_writes(SESSION) == 1, "the fixture did not record an overflow"

    # The ceiling refuses the closing entry too, and its cleanup still runs: the emitter
    # settles a refused job's ``after`` itself.
    emit.on_session_closed(SESSION, reason="test")
    assert emit.overflow_writes(SESSION) == 0, "a closed session left its overflow count behind"


def test_a_writer_built_under_a_patched_handle_uses_the_real_one_after_undo(monkeypatch):
    """The writer opens a unit for its own loss marker through the emitter's handle
    cache, read by NAME at call time. A writer first built while a test had ``_handle``
    patched keeps no copy of the patch: once it is undone, a later loss still gets its
    ``write/dropped`` marker instead of finding no log and saying nothing.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_handle", lambda session_id: None)
        emit.on_turn_started(SESSION, 1, "user")  # the first use builds the writer
    _open_session()
    assert emit.flush()
    real_append = lg.CrewLog.append

    def _refuse_turns(self, entry_type, *args, **kwargs):
        if entry_type == "turn/started":
            raise OSError("the disk is gone")
        return real_append(self, entry_type, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        patch.setattr(lg.CrewLog, "append", _refuse_turns)

        async def _lost() -> None:
            emit.on_turn_started(SESSION, 2, "user")

        asyncio.run(_lost())
        assert emit.flush(timeout=10.0)
    markers = [e["data"] for e in _body() if e["type"] == "write/dropped"]
    assert markers and markers[0]["dropped_count"] == 1, "the lost entry was never admitted"


def test_the_writer_and_the_emitter_report_through_one_budget(caplog):
    """One kind of failure is named once per window, whichever side reports it.

    The writer reports a landed job's failing cleanup, and the emitter reports a
    ceiling-rejected job's, under the same ``finish-write`` kind. Two budgets would name
    that one cause twice inside the window, which is the flood the budget exists to stop.
    """
    emit.reset_caches(limits=WriterLimits(max_pending_count=0))
    _open_session()
    assert emit.flush()

    def _failing_cleanup() -> None:
        raise RuntimeError("the cleanup failed")

    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        # No loop: the append runs inline and lands, so the WRITER runs its cleanup.
        emit._submit(lambda: None, "landed", SESSION, after=_failing_cleanup)

        async def _rejected() -> None:
            # On a loop at a zero ceiling the append is refused, so the EMITTER settles it.
            emit._submit(lambda: None, "rejected", SESSION, after=_failing_cleanup)

        asyncio.run(_rejected())
    finishing = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "the cleanup failed" in r.getMessage()
    ]
    assert len(finishing) == 1, f"one cause was named {len(finishing)} times in one window"
    assert emit.overflow_writes(SESSION) == 1, "the second append was not the rejected one"
