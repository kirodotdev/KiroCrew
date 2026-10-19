"""Shared support for the `test_crew_log_emit*.py` files of the crew log emitter tests.

Holds the autouse fixture that gives every test its own data home with the log switched
on, the shared session ids and openers, readers of the log's entries, and the drain,
ownership, retention and event-building helpers the family's files share. Each file of the
family imports what it needs from here, the autouse fixture included.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import crew_log_path, crew_log_root, eager, emit
from kiro_crew.crew_log.lease import LEASE_FILE
from kiro_crew.platform_compat import file_lock

SESSION = "acp-sess-0001"

#: Lost-run ceiling for a shutdown drain that has to release a batch parked behind a
#: long backoff. The drain clamps that backoff to its first pacing slice, which is
#: ``timeout / DEFAULT_LIMITS.max_write_attempts`` after the call, so a passing run waits
#: one slice and then does its real store writes. Measured on the Windows shards at a 3 s
#: window: 0.56-0.59 s per call, of which 0.5 s is that slice, and one loaded runner
#: still held a batch in flight past 3 s. At 20 s the slice costs 3.3 s and the writes get
#: about 16.7 s, far beyond any passing run measured, and the ceiling stays under half of
#: the suite's ``--timeout=120``. It ends a wedged drain; it never decides a passing one.
SHUTDOWN_DRAIN_CEILING_SECS = 20.0


def _assert_drained_for_shutdown(what: str) -> None:
    """Drain under the lost-run ceiling and name what was still owed if it expires."""
    # flake-ok: elapsed only names the wait in the failure message; drain_for_shutdown's own timeout bounds it
    started = time.monotonic()
    drained = emit.drain_for_shutdown(timeout=SHUTDOWN_DRAIN_CEILING_SECS)
    elapsed = time.monotonic() - started
    assert drained, (
        f"{what}: the shutdown drain was not complete after {elapsed:.1f}s "
        f"(ceiling {SHUTDOWN_DRAIN_CEILING_SECS:.0f}s); "
        f"{emit.buffered_writes()} append(s) still buffered, {emit.dropped_writes()} dropped"
    )


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no module of this family calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one.

    The retry backoff is also flattened to zero here. What the retention tests
    assert is the ORDER and the OUTCOME of a bounded retry -- which entry lands,
    which is dropped, what the counter reads -- and none of that is a claim about
    how long the writer waits. Leaving the real schedule in would make each of them
    sit through up to 1.55s of sleeping on a single writer thread and then assert
    through a fixed timeout, which is a stopwatch race the moment the host is busy:
    the same three tests passed locally and on Windows while failing a loaded Linux
    shard. Zeroed, they wait only on the writer's own barrier.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    # Stop the writer BEFORE pytest removes tmp_path. The writer runs on its own
    # thread and creates the crew log root on demand, so one still in flight here can
    # recreate the directory tree pytest has just deleted -- leaving a stray home
    # behind and, worse, making a later test's failure depend on the previous test's
    # timing. `drain_for_shutdown` returns once nothing is buffered and no batch is
    # claimed, which is exactly the condition for "no thread will touch this path
    # again"; `reset_caches` then drops the handles that point into it.
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


def _log_path(session_id: str = SESSION) -> Path:
    """Ask the storage library where it puts things; never pin its layout."""
    return crew_log_path("session", session_id)


def _remove_as_retention_does(remove: Callable[[], object]) -> None:
    """Run *remove* with the eager folder settled and held between batches.

    Retention removes a unit inside ``eager.paused()``, so no fold has the unit
    open while its files go. A test that removes a crew log's files itself must do
    the same: the folder reads the unit on its own thread after every entry, and a
    fold that listed the segments before a bare removal then recreates the unit's
    lock file inside the directory being removed (``Directory not empty`` on POSIX,
    ``WinError 32`` on Windows for a segment it holds open).
    """
    assert eager.drain(timeout=10.0), "the eager folder never settled"
    with eager.paused() as held:
        assert held, "the eager folder could not be held between batches"
        remove()


def _store_root() -> Path:
    return crew_log_root("session")


def _entries(session_id: str = SESSION) -> list[dict]:
    """Every line, header first, exactly as the emitter left it."""
    path = _log_path(session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    """The entries after the header line."""
    return _entries(session_id)[1:]


def _open_session() -> None:
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )


def _typed(turn: int = 1, text: str = "hello", **kw) -> None:
    emit.on_message_received(SESSION, turn, text=text, **kw)


@contextlib.contextmanager
def _owned_elsewhere(session_id: str = SESSION):
    """Hold one session log's write ownership from another descriptor.

    An advisory lock belongs to an open file description, so a descriptor of our
    own contends exactly as a second gateway's would. Non-blocking, so a lock this
    process already holds is reported here instead of becoming a wait.
    """
    path = lg.crew_log_dir(lg.KIND_SESSION, session_id) / LEASE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+") as handle:
        with file_lock(handle.fileno(), exclusive=True, required=True, wait=False):
            yield


def _can_take_ownership(session_id: str = SESSION) -> bool:
    """Whether another owner could take this crew log's write lock right now.

    False means this process still owns the log. Non-blocking, so a held lock is
    an answer here rather than a wait.
    """
    try:
        with _owned_elsewhere(session_id):
            return True
    except OSError:
        return False


def _tool_result_event(output: str | None):
    from kiro_crew.acp._dispatch import parse_session_update
    from kiro_crew.acp.types import EVENT_TOOL_RESULT

    raw_output = {"items": [] if output is None else [{"Text": output}]}
    events = parse_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-measured",
            "status": "completed",
            "rawOutput": raw_output,
        }
    )
    results = [event for event in events if event.kind == EVENT_TOOL_RESULT]
    assert len(results) == 1
    return results[0]


#: The successor id a superseded-session test opens after ``SESSION``. A restart
#: cold-starts the slot's ACP session under a new id, which is why the successor
#: gets a crew log of its own rather than re-attaching to this one.
SUCCESSOR = "acp-sess-0002"


def _open_successor(**kwargs) -> None:
    """Open ``SUCCESSOR`` on the same slot ``_open_session`` uses."""
    emit.on_session_opened(
        SUCCESSOR,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
        **kwargs,
    )
