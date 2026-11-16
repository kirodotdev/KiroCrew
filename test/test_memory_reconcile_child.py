"""The FULL memory metadata pass runs in a child process, with an in-process fallback."""

import logging
import multiprocessing
import shutil
import threading

import pytest

from kiro_crew import memory_reconcile_child as child
from kiro_crew import memory_record_metadata as meta
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.testing.wait import default_timeout, wait_until
from kiro_crew.vector_memory import VectorMemoryStore

#: How long the parent waits for a child that never exits, before terminating it.
_HANG_TIMEOUT_SECS = 1.0


@pytest.fixture
def path(tmp_path):
    return tmp_path / "memory.db"


def _open(path):
    store = VectorMemoryStore(db_path=path, embedding_dim=2)
    store.init()
    return store


def _upgraded_store(path, rows=30):
    """A populated store with no marker, as after an upgrade."""
    store = _open(path)
    for i in range(rows):
        store.set_semantic(f"user.k{i}", f"v{i}", 1, "user_explicit")
    store.close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM memory_reconcile_state")
    db.execute("DELETE FROM memory_record_meta")
    db.commit()
    db.close()


def _snapshot(path):
    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT record_id, content_hash FROM memory_record_meta ORDER BY record_id"
        ).fetchall()
        marker = db.execute(
            "SELECT relation, version, layout FROM memory_reconcile_state ORDER BY relation"
        ).fetchall()
        revisions = db.execute(
            "SELECT record_id, operation, source, status FROM memory_revisions ORDER BY record_id, id"
        ).fetchall()
        return rows, marker, revisions
    finally:
        db.close()


def test_full_pass_in_a_real_child_matches_the_in_process_pass(path, tmp_path):
    other = tmp_path / "other.db"
    _upgraded_store(path)
    shutil.copyfile(path, other)  # same rows, so the hashes can match
    assert child.run_full_pass_in_child(str(path)) is True
    db = sqlite3.connect(other)
    with db:
        meta.reconcile(db, full=True)
    db.close()
    rows, marker, revisions = _snapshot(path)
    assert rows and marker and revisions
    assert (rows, marker, revisions) == _snapshot(other)


def test_the_full_pass_finishes_while_the_parent_runs_a_busy_thread(path):
    _upgraded_store(path)
    stop = threading.Event()
    counter = [0]

    def spin():
        while not stop.is_set():
            counter[0] += 1

    thread = threading.Thread(target=spin)
    thread.start()
    try:
        assert child.run_full_pass_in_child(str(path)) is True
        seen = counter[0]
        # the GIL-bound loop is still making progress
        wait_until(lambda: counter[0] > seen, describe=lambda: f"counter={counter[0]}")
    finally:
        stop.set()
        thread.join(timeout=default_timeout())
    assert not thread.is_alive()
    assert _snapshot(path)[1]


def test_a_store_open_boots_through_the_child_and_marks_it(path, monkeypatch):
    _upgraded_store(path)
    calls = []
    real = child.run_full_pass_in_child

    def spy(db_path, **kwargs):
        calls.append(db_path)
        return real(db_path, **kwargs)

    monkeypatch.setattr(child, "run_full_pass_in_child", spy)
    _open(path).close()
    assert calls == [str(path)]
    assert _snapshot(path)[1]
    _open(path).close()
    assert len(calls) == 1  # marked: the second boot stays in process


def test_a_crashing_child_falls_back_in_process_with_one_warning(path, monkeypatch, caplog):
    _upgraded_store(path)
    monkeypatch.setattr(child, "run_child", _die)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        _open(path).close()
    warnings = [r for r in caplog.records if r.name == child.logger.name]
    assert len(warnings) == 1 and "exited with code" in warnings[0].getMessage()
    assert _snapshot(path)[1]  # the in-process pass installed the marker


def _die(db_path, conn=None):
    raise SystemExit(3)


def test_a_child_that_cannot_start_falls_back_in_process(path, monkeypatch, caplog):
    _upgraded_store(path)

    def refuse(*_a, **_k):
        raise OSError("no processes")

    monkeypatch.setattr(child.multiprocessing, "get_context", refuse)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        _open(path).close()
    assert [r.getMessage() for r in caplog.records if r.name == child.logger.name] == [
        "Memory reconcile child could not start (OSError: no processes); running the pass in process"
    ]
    assert _snapshot(path)[1]


def test_a_timed_out_child_is_terminated_before_the_fallback(path, monkeypatch, caplog):
    _upgraded_store(path)
    monkeypatch.setattr(child, "run_child", _hang)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        done = child.run_full_pass_in_child(
            str(path), timeout=_HANG_TIMEOUT_SECS
        )  # flake-ok: child never exits
    assert done is False
    assert "timed out" in caplog.records[-1].getMessage()
    assert not [p for p in __import__("multiprocessing").active_children() if "reconcile" in p.name]


def _hang(db_path, conn=None):
    threading.Event().wait()


def test_a_busy_write_lock_takes_the_busy_path_not_the_fallback(path, caplog):
    _upgraded_store(path)
    # Rows already carry metadata, so the child only reads; the marker waits.
    _open_rows_marked(path)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with caplog.at_level(logging.WARNING, logger=child.logger.name):
            assert child.run_full_pass_in_child(str(path)) is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not [r for r in caplog.records if r.name == child.logger.name]
    assert _snapshot(path)[1] == []  # no marker: the next start repeats the pass


def _open_rows_marked(path):
    """Give the rows their metadata without installing the marker."""
    db = sqlite3.connect(path)
    with db:
        meta.reconcile(db, full=True)
        db.execute("DELETE FROM memory_reconcile_state")
    db.close()


def test_the_passes_own_warnings_reach_the_parent_log(path, caplog):
    _upgraded_store(path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO episodic_memories (id, text, created_at) VALUES ('', 'b', '2026-01-01')"
    )
    db.commit()
    db.close()
    with caplog.at_level("WARNING", logger=meta.__name__):
        assert child.run_full_pass_in_child(str(path)) is True
    assert "skipped 1 episodic_memories row(s) with no id" in caplog.text


def test_run_child_reconciles_and_sends_the_collected_warnings(path):
    """The spawn target, called in this process so that coverage sees it."""
    _upgraded_store(path)
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO episodic_memories (id, text, created_at) VALUES ('', 'b', '2026-01-01')"
    )
    db.commit()
    db.close()
    receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
    child.run_child(str(path), sender)
    sender.close()
    assert receiver.recv() == ["Memory reconcile skipped 1 episodic_memories row(s) with no id"]
    assert _snapshot(path)[1]  # the marker is written
    assert not [
        h for h in logging.getLogger(meta.__name__).handlers if isinstance(h, child._Collect)
    ]


def test_run_child_without_a_pipe_still_reconciles(path):
    _upgraded_store(path)
    child.run_child(str(path))
    assert _snapshot(path)[1]


def test_run_child_sends_what_it_collected_even_when_the_pass_raises(path, monkeypatch):
    _upgraded_store(path)

    def boom(db, *, full):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(meta, "reconcile", boom)
    receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
    with pytest.raises(sqlite3.OperationalError):
        child.run_child(str(path), sender)
    sender.close()
    assert receiver.recv() == []


class _FakeProcess:
    """A stand-in child: `alive` is the is_alive() answers in order, then False."""

    def __init__(self, exitcode=0, alive=(), send=None):
        self.exitcode = exitcode
        self.alive = list(alive)
        self.calls = []

    def start(self):
        self.calls.append("start")

    def join(self, timeout=None):
        self.calls.append("join")

    def is_alive(self):
        return self.alive.pop(0) if self.alive else False

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


class _HeldSender:
    """A write end the parent's close() cannot shut, as if the child still held it."""

    def __init__(self, sender):
        self.sender = sender

    def close(self):
        pass


class _FakeContext:
    """`messages` is what the child sent (None: nothing, so the pipe closes on EOF);
    `hold` keeps the write end open, as a child that is still running does."""

    def __init__(self, process, *, messages=None, hold=False):
        self.process = process
        self.messages = messages
        self.hold = hold

    def Pipe(self, duplex=False):  # noqa: N802 - mirrors multiprocessing's API
        receiver, sender = multiprocessing.Pipe(duplex=False)
        if self.messages is not None:
            sender.send(self.messages)
        return receiver, (_HeldSender(sender) if self.hold else sender)

    def Process(self, **_kwargs):  # noqa: N802
        return self.process


def _fake_child(monkeypatch, process, **kwargs):
    monkeypatch.setattr(
        child.multiprocessing, "get_context", lambda _method: _FakeContext(process, **kwargs)
    )


def test_a_child_that_exits_non_zero_reports_its_exit_code(path, monkeypatch, caplog):
    _fake_child(monkeypatch, _FakeProcess(exitcode=7), messages=[])
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        assert child.run_full_pass_in_child(str(path)) is False
    assert [r.getMessage() for r in caplog.records if r.name == child.logger.name] == [
        "Memory reconcile child exited with code 7; running the pass in process"
    ]


def test_a_child_that_ignores_terminate_is_killed_then_joined(path, monkeypatch, caplog):
    process = _FakeProcess(alive=[True])  # still alive after terminate and the grace join
    _fake_child(monkeypatch, process, hold=True)  # nothing is ever sent
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        done = child.run_full_pass_in_child(
            str(path), timeout=_HANG_TIMEOUT_SECS
        )  # flake-ok: a fake process, no real wait
    assert done is False
    assert process.calls == ["start", "terminate", "join", "kill", "join"]
    assert "timed out" in caplog.records[-1].getMessage()


def test_a_child_that_sent_its_result_but_does_not_exit_is_stopped(path, monkeypatch, caplog):
    process = _FakeProcess(alive=[True, True])  # alive after the grace join, and after terminate
    _fake_child(monkeypatch, process, messages=[])
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        assert child.run_full_pass_in_child(str(path)) is False
    assert process.calls == ["start", "join", "terminate", "join", "kill", "join"]
    assert "did not exit after finishing" in caplog.records[-1].getMessage()


def test_a_child_that_exits_cleanly_without_its_message_falls_back(path, monkeypatch, caplog):
    _fake_child(monkeypatch, _FakeProcess())  # exit 0, the pipe closed with nothing sent
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        assert child.run_full_pass_in_child(str(path)) is False
    (record,) = [r for r in caplog.records if r.name == child.logger.name]
    assert "exited without a result" in record.getMessage()


class _BrokenReceiver:
    """The read end as Windows shows a pipe whose writer exited: BrokenPipeError, not EOFError.

    `raises_in` names the call that raises: PipeConnection.poll() peeks the pipe and
    raises it too, before recv() is reached.
    """

    def __init__(self, raises_in):
        self.raises_in = raises_in

    def _broken(self):
        raise BrokenPipeError(109, "The pipe has been ended")

    def poll(self, timeout=None):
        if self.raises_in == "poll":
            self._broken()
        return True

    def recv(self):
        self._broken()


@pytest.mark.parametrize("raises_in", ["poll", "recv"])
def test_a_broken_pipe_is_a_missing_result_not_a_failed_start(path, monkeypatch, caplog, raises_in):
    context = _FakeContext(_FakeProcess())
    monkeypatch.setattr(
        context,
        "Pipe",
        lambda duplex=False: (_BrokenReceiver(raises_in), multiprocessing.Pipe(False)[1]),
    )
    monkeypatch.setattr(child.multiprocessing, "get_context", lambda _method: context)
    with caplog.at_level(logging.WARNING, logger=child.logger.name):
        assert child.run_full_pass_in_child(str(path)) is False
    (record,) = [r for r in caplog.records if r.name == child.logger.name]
    assert "exited without a result" in record.getMessage()


def test_a_clean_child_message_is_logged_by_the_parent(path, monkeypatch, caplog):
    _fake_child(monkeypatch, _FakeProcess(), messages=["Memory reconcile skipped 2 rows"])
    with caplog.at_level(logging.WARNING, logger=meta.__name__):
        assert child.run_full_pass_in_child(str(path)) is True
    assert "Memory reconcile skipped 2 rows" in caplog.text


def _chatty(db_path, conn=None):
    """A child whose message is far larger than any OS pipe buffer."""
    conn.send([f"warning {i} " + "x" * 1000 for i in range(200)])


def test_a_child_sending_more_than_a_pipe_buffer_finishes_and_is_logged(path, monkeypatch, caplog):
    monkeypatch.setattr(child, "run_child", _chatty)
    with caplog.at_level(logging.WARNING, logger=meta.__name__):
        assert child.run_full_pass_in_child(str(path), timeout=default_timeout()) is True
    assert len([r for r in caplog.records if r.name == meta.__name__]) == 200


def test_run_child_relays_the_first_messages_and_counts_the_rest(path, monkeypatch):
    _upgraded_store(path)

    def noisy(db, *, full):
        for i in range(child._MAX_RELAYED + 70):
            meta.logger.warning("warning %d", i)

    monkeypatch.setattr(meta, "reconcile", noisy)
    receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
    child.run_child(str(path), sender)
    sender.close()
    relayed = receiver.recv()
    assert len(relayed) == child._MAX_RELAYED + 1
    assert relayed[0] == "warning 0" and relayed[-1] == "... and 70 more"
