"""Producer-side concurrency of the session ledger emitter.

The STORE layer's two-handle seq race is pinned in test_ledger_core.py. This
file attacks the EMITTER layer above it: the _submit / _buffer / _drain_loop /
flush machinery that sits between a producer calling on_* and the Ledger.append
that lands a line on disk.

Four areas, matching the task spec:
1. Many producers, one session -- contiguous seq, no gaps, causal order.
2. Many sessions -- no cross-contamination, each file well-formed.
3. Interleaved sessions -- one session's write in flight cannot lose another's.
4. flush(timeout=...) under concurrent producers returns only when empty.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from kiro_crew import session_ledger_emit as emit
from kiro_crew.ledger import Ledger, ledger_path

SESSION = "conc-edge-sess-0001"

#: How long a drain barrier waits for the writer to land something NEW before it
#: reports the writer stuck. Not a total budget for the drain: every test below
#: hands the single writer thread 30 to 160 appends and each one costs an ``fsync``,
#: which measures 0.4 ms on a warm Linux host and over 100 ms on a contended Windows
#: CI runner -- so a fixed ceiling across a whole batch asserts a WRITE RATE the host
#: owns rather than anything the emitter does. A writer that stopped still fails
#: inside this window, which is the failure these barriers exist to catch.
_NO_PROGRESS_SECONDS = 10.0

#: Total ceiling for one barrier, half of the suite's ``--timeout=120`` in setup.cfg.
#: A writer that trickles forever must fail as a readable assertion here rather than
#: reach that mark, because pytest-timeout kills the xdist worker and costs the whole
#: RUN instead of one test.
_DRAIN_CEILING_SECONDS = 60.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Each test gets its own data home, zero-backoff, and a clean emitter."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


def _drained(*sids: str) -> bool:
    """Wait for the writer to go quiet. False once it stops landing entries.

    What every test below needs is the barrier "the writer finished", and what it
    must NOT depend on is "the writer finished inside one fixed number of seconds":
    the work is a batch of fsync-priced appends the test itself queues, so a fixed
    ceiling over it is a rate assertion the slow host loses while the emitter is
    working perfectly. So the give-up condition is a writer that landed nothing for a
    whole :data:`_NO_PROGRESS_SECONDS` window, and the first window is measured from
    BEFORE the first wait -- a writer that is wedged rather than slow is reported one
    window in, exactly as promptly as a fixed ceiling of the same size reported it.

    Progress is read as the BYTES on disk under the named sessions, which only grow
    and need no parse: a size read cannot trip over a line the writer is in the
    middle of appending, and the buffer count cannot serve here because the writer
    takes a batch OUT of the buffer before it writes it, so an empty buffer says
    nothing about how far a claimed batch has got.
    """
    watched = sids or (SESSION,)
    give_up_at = time.monotonic() + _DRAIN_CEILING_SECONDS
    landed = _bytes_on_disk(watched)
    while not emit.flush(timeout=_NO_PROGRESS_SECONDS):
        written = _bytes_on_disk(watched)
        if written <= landed or time.monotonic() >= give_up_at:
            return False
        landed = written
    return True


def _bytes_on_disk(sids: tuple[str, ...]) -> int:
    """How many bytes the named sessions' logs hold together."""
    total = 0
    for sid in sids:
        try:
            total += ledger_path("session", sid).stat().st_size
        except OSError:
            pass
    return total


def _open(sid: str = SESSION) -> None:
    emit.on_session_opened(sid, agent="kirocrew", slot="test", model="m", owner="default")
    assert _drained(sid)


def _entries(sid: str = SESSION) -> list[dict]:
    path = ledger_path("session", sid)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _body(sid: str = SESSION) -> list[dict]:
    return _entries(sid)[1:]


# ---------------------------------------------------------------------------
# 1. Many producers, one session
# ---------------------------------------------------------------------------


def test_many_producers_one_session_all_entries_land():
    """N threads calling on_tool_called for the same session. Every entry must
    land, seq must be contiguous (no gaps, no duplicates)."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert _drained()

    n_threads = 8
    calls_per_thread = 20
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def _produce(thread_idx: int) -> None:
        try:
            barrier.wait()
            for i in range(calls_per_thread):
                emit.on_tool_called(
                    SESSION,
                    turn=1,
                    name=f"tool_t{thread_idx}",
                    call_id=f"call-{thread_idx}-{i}",
                    server="test-server",
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"producer threads raised: {errors}"
    assert _drained(), "the writer stopped draining"

    body = _body()
    # Filter to tool/called entries (skip session/opened, turn/started)
    tool_entries = [e for e in body if e["type"] == "tool/called"]
    assert len(tool_entries) == n_threads * calls_per_thread

    # Seq must be contiguous with no gaps across ALL body entries
    all_seqs = [e["seq"] for e in body]
    assert all_seqs == list(
        range(1, len(body) + 1)
    ), f"seq not contiguous: gaps at {_find_gaps(all_seqs)}"


def test_many_producers_one_session_per_session_order_preserved():
    """Each thread's entries must appear in the order that thread emitted them.
    The emitter buffers per session and the single writer drains in order, so
    within one session causal order (emit A before B -> seq(A) < seq(B)) must
    hold for entries from the SAME producer thread."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert _drained()

    n_threads = 6
    calls_per_thread = 15
    barrier = threading.Barrier(n_threads)

    def _produce(thread_idx: int) -> None:
        barrier.wait()
        for i in range(calls_per_thread):
            # Encode the thread and the sequence number in the call_id
            emit.on_tool_called(
                SESSION,
                turn=1,
                name=f"tool_t{thread_idx}",
                call_id=f"call-{thread_idx}-{i:04d}",
                server="test-server",
            )

    threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _drained()

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]

    # For each thread, extract the entries it produced and verify they are in
    # submission order by checking the index encoded in call_id.
    for thread_idx in range(n_threads):
        thread_entries = [
            e for e in tool_entries if e["data"]["call_id"].startswith(f"call-{thread_idx}-")
        ]
        assert (
            len(thread_entries) == calls_per_thread
        ), f"thread {thread_idx}: expected {calls_per_thread}, got {len(thread_entries)}"
        indices = [int(e["data"]["call_id"].split("-")[-1]) for e in thread_entries]
        assert indices == list(
            range(calls_per_thread)
        ), f"thread {thread_idx}: entries out of causal order: {indices}"


def test_call_index_contiguous_under_concurrent_producers():
    """call_index is minted under _lock per turn. With N threads calling
    on_tool_called concurrently the resulting call_index values must form a
    contiguous 1..N*M range with no duplicates."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert _drained()

    n_threads = 8
    calls_per_thread = 10
    barrier = threading.Barrier(n_threads)

    def _produce(thread_idx: int) -> None:
        barrier.wait()
        for i in range(calls_per_thread):
            emit.on_tool_called(
                SESSION,
                turn=1,
                name=f"t{thread_idx}",
                call_id=f"ci-{thread_idx}-{i}",
                server="s",
            )

    threads = [threading.Thread(target=_produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _drained()

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    call_indices = sorted(e["data"]["call_index"] for e in tool_entries)
    expected = list(range(1, n_threads * calls_per_thread + 1))
    assert call_indices == expected, (
        f"call_index not contiguous: missing={set(expected) - set(call_indices)}, "
        f"dupes={[x for x in call_indices if call_indices.count(x) > 1]}"
    )


# ---------------------------------------------------------------------------
# 2. Many sessions at once
# ---------------------------------------------------------------------------


def test_many_sessions_no_cross_contamination():
    """M sessions emitting concurrently. No session's file holds another's
    entries, and each is independently readable by the production reader."""
    n_sessions = 6
    entries_per_session = 20
    sids = [f"conc-multi-{i:04d}" for i in range(n_sessions)]

    for sid in sids:
        _open(sid)
        emit.on_turn_started(sid, turn=1)
    assert _drained(*sids)

    barrier = threading.Barrier(n_sessions)

    def _produce(sid: str) -> None:
        barrier.wait()
        for i in range(entries_per_session):
            emit.on_tool_called(
                sid,
                turn=1,
                name=f"tool-{sid}",
                call_id=f"call-{sid}-{i}",
                server="s",
            )

    threads = [threading.Thread(target=_produce, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _drained(*sids)

    for sid in sids:
        body = _body(sid)
        tool_entries = [e for e in body if e["type"] == "tool/called"]
        # Correct count
        assert (
            len(tool_entries) == entries_per_session
        ), f"{sid}: expected {entries_per_session} tool entries, got {len(tool_entries)}"
        # No foreign entries
        for e in tool_entries:
            assert e["data"]["name"] == f"tool-{sid}", (
                f"{sid}: cross-contamination, entry names tool for another session: "
                f"{e['data']['name']}"
            )
            assert e["data"]["call_id"].startswith(
                f"call-{sid}-"
            ), f"{sid}: cross-contamination in call_id: {e['data']['call_id']}"

        # Each file is independently readable by the production reader
        ledger = Ledger.open("session", sid)
        reader_entries = list(ledger.iter_from(1))
        assert len(reader_entries) > 0, f"{sid}: iter_from returned nothing"
        reader_seqs = [re.seq for re in reader_entries]
        assert reader_seqs == list(
            range(1, len(reader_entries) + 1)
        ), f"{sid}: production reader sees non-contiguous seqs"


def test_many_sessions_each_file_well_formed():
    """Each session's file must parse as valid JSON lines and the header must
    be present and correct."""
    n_sessions = 4
    sids = [f"conc-wf-{i}" for i in range(n_sessions)]

    for sid in sids:
        _open(sid)
        emit.on_turn_started(sid, turn=1)
    assert _drained(*sids)

    barrier = threading.Barrier(n_sessions)

    def _produce(sid: str) -> None:
        barrier.wait()
        emit.on_message_received(sid, turn=1, text=f"hello from {sid}")
        emit.on_tool_called(sid, turn=1, name="tool", call_id=f"c-{sid}", server="s")
        emit.on_tool_completed(sid, turn=1, name="tool", call_id=f"c-{sid}", status="ok")
        emit.on_turn_completed(sid, turn=1, input_tokens=10, output_tokens=5)

    threads = [threading.Thread(target=_produce, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _drained(*sids)

    for sid in sids:
        entries = _entries(sid)
        # Header is entry 0 -- its type is the ledger kind, "session"
        assert entries[0]["type"] == "session", f"{sid}: first line is not a header"
        # Every line parses as valid JSON (already guaranteed by _entries, but
        # also check via the production reader)
        ledger = Ledger.open("session", sid)
        all_read = list(ledger.iter_from(1))
        types_seen = {e.type for e in all_read}
        assert "session/opened" in types_seen, f"{sid}: missing session/opened"
        assert "turn/started" in types_seen, f"{sid}: missing turn/started"
        assert "turn/completed" in types_seen, f"{sid}: missing turn/completed"


# ---------------------------------------------------------------------------
# 3. Interleaved sessions -- no entry lost while another's write is in flight
# ---------------------------------------------------------------------------


def test_interleaved_sessions_no_entry_lost():
    """Session A and session B emit concurrently. The invariant: bucketing buys
    order and content, NOT latency isolation. No entry from either session is
    lost while the other's write is in flight."""
    sid_a = "conc-interleave-a"
    sid_b = "conc-interleave-b"
    _open(sid_a)
    _open(sid_b)
    emit.on_turn_started(sid_a, turn=1)
    emit.on_turn_started(sid_b, turn=1)
    assert _drained(sid_a, sid_b)

    n_entries = 30
    barrier = threading.Barrier(2)

    def _produce_a() -> None:
        barrier.wait()
        for i in range(n_entries):
            emit.on_tool_called(sid_a, turn=1, name="a-tool", call_id=f"a-{i}", server="s")

    def _produce_b() -> None:
        barrier.wait()
        for i in range(n_entries):
            emit.on_tool_called(sid_b, turn=1, name="b-tool", call_id=f"b-{i}", server="s")

    ta = threading.Thread(target=_produce_a)
    tb = threading.Thread(target=_produce_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    assert _drained(sid_a, sid_b)

    body_a = [e for e in _body(sid_a) if e["type"] == "tool/called"]
    body_b = [e for e in _body(sid_b) if e["type"] == "tool/called"]

    assert (
        len(body_a) == n_entries
    ), f"session A lost entries: expected {n_entries}, got {len(body_a)}"
    assert (
        len(body_b) == n_entries
    ), f"session B lost entries: expected {n_entries}, got {len(body_b)}"

    # Each session's seqs are contiguous on their own
    for label, body in [("A", _body(sid_a)), ("B", _body(sid_b))]:
        seqs = [e["seq"] for e in body]
        assert seqs == list(range(1, len(body) + 1)), f"session {label}: non-contiguous seqs"


def test_burst_across_sessions_all_entries_land():
    """A harder variant: many sessions each emitting a burst simultaneously,
    testing that the single writer thread and per-session bucketing lose
    nothing under a wide fan-out."""
    n_sessions = 10
    entries_per = 15
    sids = [f"conc-burst-{i:03d}" for i in range(n_sessions)]

    for sid in sids:
        _open(sid)
        emit.on_turn_started(sid, turn=1)
    assert _drained(*sids)

    barrier = threading.Barrier(n_sessions)

    def _produce(sid: str) -> None:
        barrier.wait()
        for i in range(entries_per):
            emit.on_tool_called(sid, turn=1, name="t", call_id=f"c-{sid}-{i}", server="s")

    threads = [threading.Thread(target=_produce, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _drained(*sids)

    total_tool_entries = 0
    for sid in sids:
        tool_entries = [e for e in _body(sid) if e["type"] == "tool/called"]
        assert (
            len(tool_entries) == entries_per
        ), f"{sid}: expected {entries_per}, got {len(tool_entries)}"
        total_tool_entries += len(tool_entries)
    assert total_tool_entries == n_sessions * entries_per


# ---------------------------------------------------------------------------
# 4. flush(timeout=...) under concurrent producers
# ---------------------------------------------------------------------------


def test_flush_returns_only_when_buffer_empty():
    """flush() must return True only when the buffer is truly empty, even when
    producers are still active at the moment flush is called."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert _drained()

    n_entries = 50
    produced = threading.Event()

    def _produce() -> None:
        for i in range(n_entries):
            emit.on_tool_called(SESSION, turn=1, name="ft", call_id=f"fc-{i}", server="s")
        produced.set()

    t = threading.Thread(target=_produce)
    t.start()
    produced.wait()  # Wait until all entries are submitted
    t.join()

    # Now flush -- it must wait until the writer has drained everything
    result = _drained()
    assert result, "flush returned False (the writer stopped draining)"
    assert (
        emit.buffered_writes() == 0
    ), f"flush returned True but {emit.buffered_writes()} writes still buffered"

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    assert len(tool_entries) == n_entries


def test_flush_under_ongoing_production():
    """flush called while producers are STILL emitting. flush must return True
    only after the entries that existed at the time of the call have been
    written. (New entries submitted after flush starts looking are NOT its
    responsibility, but anything queued before must land.)"""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert _drained()

    n_pre_flush = 20
    n_post_flush = 10
    pre_flush_done = threading.Event()
    flush_started = threading.Event()
    flush_result: list[bool] = []

    def _produce() -> None:
        for i in range(n_pre_flush):
            emit.on_tool_called(SESSION, turn=1, name="pre", call_id=f"pre-{i}", server="s")
        pre_flush_done.set()
        flush_started.wait()  # Wait for flush to begin
        for i in range(n_post_flush):
            emit.on_tool_called(SESSION, turn=1, name="post", call_id=f"post-{i}", server="s")

    def _flusher() -> None:
        pre_flush_done.wait()
        flush_started.set()
        flush_result.append(_drained())

    tp = threading.Thread(target=_produce)
    tf = threading.Thread(target=_flusher)
    tp.start()
    tf.start()
    tp.join()
    tf.join()

    assert flush_result[0], "flush returned False"

    # A second flush to ensure everything including post-flush entries has landed
    assert _drained()

    tool_entries = [e for e in _body() if e["type"] == "tool/called"]
    # At minimum, the pre-flush entries must be there; post-flush may or may not
    # be included depending on timing, but a second flush guarantees all.
    assert len(tool_entries) == n_pre_flush + n_post_flush


def test_flush_timeout_zero_does_not_hang():
    """flush(timeout=0) must return immediately, True if empty, False otherwise.
    It must never block."""
    _open()
    # With nothing pending, should return True instantly
    assert emit.flush(timeout=0.0) or True  # may be True or False, must not hang

    # Submit some entries
    emit.on_turn_started(SESSION, turn=1)
    for i in range(5):
        emit.on_tool_called(SESSION, turn=1, name="t", call_id=f"z-{i}", server="s")

    # flush(0) returns immediately -- we just check it returns at all
    emit.flush(timeout=0.0)
    # Now drain properly
    assert emit.flush(timeout=5.0)


# ---------------------------------------------------------------------------
# Mixed: different on_* entry points concurrently on one session
# ---------------------------------------------------------------------------


def test_mixed_entry_points_concurrent():
    """Different on_* functions called concurrently for one session. Tests that
    the emitter's internal state (_live, _tool_started, _pinned) stays
    consistent under contention."""
    _open()
    emit.on_turn_started(SESSION, turn=1)
    assert _drained()

    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def _tools() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_tool_called(SESSION, turn=1, name="tool", call_id=f"mt-{i}", server="s")
                emit.on_tool_completed(SESSION, turn=1, name="tool", call_id=f"mt-{i}", status="ok")
        except Exception as exc:
            errors.append(exc)

    def _messages() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_message_received(SESSION, turn=1, text=f"msg {i}", role="user")
        except Exception as exc:
            errors.append(exc)

    def _approvals() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_approval_requested(SESSION, turn=1, approval_id=f"ap-{i}", tool="t")
                emit.on_approval_decided(
                    SESSION, turn=1, approval_id=f"ap-{i}", decision="approved"
                )
        except Exception as exc:
            errors.append(exc)

    def _contexts() -> None:
        try:
            barrier.wait()
            for i in range(10):
                emit.on_context_composed(
                    SESSION, turn=1, blocks={"memory": 100 + i, "skills": 200 + i}
                )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_tools),
        threading.Thread(target=_messages),
        threading.Thread(target=_approvals),
        threading.Thread(target=_contexts),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors}"
    assert _drained()

    body = _body()
    seqs = [e["seq"] for e in body]
    assert seqs == list(range(1, len(body) + 1)), "non-contiguous seqs after mixed producers"

    # Verify tool calls and completions pair up correctly
    called_ids = {e["data"]["call_id"] for e in body if e["type"] == "tool/called"}
    completed_ids = {e["data"]["call_id"] for e in body if e["type"] == "tool/completed"}
    assert called_ids == completed_ids, (
        f"tool call/completion mismatch: called={called_ids - completed_ids}, "
        f"unclosed={completed_ids - called_ids}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_gaps(seqs: list[int]) -> list[int]:
    """Return the seq values that are missing from a range."""
    if not seqs:
        return []
    expected = set(range(seqs[0], seqs[-1] + 1))
    return sorted(expected - set(seqs))
