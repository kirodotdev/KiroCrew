"""The write half of the Windows sharing-violation window (issue #4331).

#4331 fixed the READ half: ``CrewStore._load`` now reads through
``read_bytes_with_retry``. Its PR called out the other half explicitly --
*"this store's write side still calls ``tmp.replace()`` directly rather than
``replace_with_retry``, so it remains exposed to the other half of the same
window"* -- and left it as a separate change. This is that change.

On Windows the atomic rename raises ``PermissionError`` while ANY other handle
is open on either path: an indexer, an AV scanner, or this store's own
concurrent reader. POSIX permits it, which is why the class only ever surfaces
on the ``Backend Tests (Windows)`` matrix and on Windows hosts.

Two kinds of test here, deliberately:

* ``windows_sim.replace_sharing_violation`` drives the fault on any OS. Because
  it patches ``os.replace`` and CPython 3.10's ``pathlib`` holds a CAPTURED
  reference to that function, the pre-fix ``tmp.replace()`` was not reachable by
  it at all -- so these assert the fault was OBSERVED (``state["n"]``), not just
  that the write succeeded. Without that assertion they would pass vacuously on
  the unfixed tree, exactly the trap #4331's own read-side test documents.
* One test uses a REAL open handle on a REAL Windows host, no simulator, and is
  skipped elsewhere. That is the only evidence here that the OS behaves as
  described rather than as emulated.
"""

from __future__ import annotations

import json
import threading

import pytest
from windows_sim import replace_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the bounded retry loop instant; attempt COUNT is what these pin."""
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


@pytest.fixture
def _windows(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)


def _store(tmp_path):
    """A CrewStore with only the write machinery, no disk-reading __init__."""
    from kiro_crew.crew_chat import CrewStore

    store = CrewStore.__new__(CrewStore)
    store.dir = tmp_path
    store._seq_lock = threading.Lock()
    store._io_locks_guard = threading.Lock()
    store._io_locks = {}
    store._write_seq = {}
    store._written_seq = {}
    store._pending_writes = set()
    return store


class TestCrewStoreSaveSurvivesAContendedReplace:
    def test_a_contended_write_retries_and_the_payload_lands(self, tmp_path, _windows):
        """The product path: one faulted rename, then one that succeeds."""
        store = _store(tmp_path)

        with replace_sharing_violation(match="queue.json", times=1) as state:
            store._save("queue.json", [{"id": "q1"}])

        assert json.loads((tmp_path / "queue.json").read_text(encoding="utf-8")) == [{"id": "q1"}]
        assert state["n"] == 2, (
            "the simulator must have FAULTED the store's own rename -- n == 0 "
            "means the write still goes through pathlib's captured os.replace "
            "and the retry was never on its path"
        )

    def test_a_lost_rename_does_not_advance_the_written_sequence(self, tmp_path, _windows):
        """A permanently contended file must raise AND stay retryable.

        ``_written_seq`` is what makes newest-wins work; advancing it for a
        rename that never landed would record a lost write as durable and let
        the next snapshot skip itself as stale.
        """
        store = _store(tmp_path)

        with replace_sharing_violation(match="queue.json", times=10_000) as state:
            with pytest.raises(PermissionError):
                store._save("queue.json", [{"id": "q1"}])

        assert state["n"] == aw._REPLACE_MAX_ATTEMPTS, "the retry must be bounded"
        assert store._written_seq.get("queue.json", 0) == 0
        assert not (tmp_path / "queue.json").exists()

    def test_posix_permission_errors_are_not_slept_over(self, tmp_path, monkeypatch):
        """On POSIX the OS permits replacing an open file, so a
        ``PermissionError`` is a REAL access fault — retrying delays an honest
        failure. This is the non-vacuity proof for the platform gate: the same
        simulator settings recover in the first test and must not here."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        store = _store(tmp_path)

        with replace_sharing_violation(match="queue.json", times=1) as state:
            with pytest.raises(PermissionError):
                store._save("queue.json", [{"id": "q1"}])

        assert state["n"] == 1, "it must not have tried a second time"


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="POSIX permits replacing a file that another handle holds open",
)
def test_a_real_windows_reader_no_longer_defeats_the_store_write(tmp_path, monkeypatch):
    """No simulator: a real open handle on the destination, on a real Windows
    host. This is the only test here that evidences the OS behaviour itself.

    Deterministic without wall-clock: the handle is released from inside the
    retry's own backoff, so attempt 1 fails against a genuinely locked file and
    attempt 2 succeeds against a genuinely free one.
    """
    store = _store(tmp_path)
    target = tmp_path / "queue.json"
    target.write_text(json.dumps([{"id": "old"}]), encoding="utf-8")

    handle = open(target, "rb")
    releases = {"n": 0}

    class _ReleaseOnBackoff:
        """Stands in for the ``time`` module inside ``atomic_write`` only."""

        @staticmethod
        def sleep(_seconds):
            releases["n"] += 1
            if not handle.closed:
                handle.close()

    monkeypatch.setattr(aw, "time", _ReleaseOnBackoff)
    try:
        store._save("queue.json", [{"id": "new"}])
    finally:
        if not handle.closed:
            handle.close()

    assert json.loads(target.read_text(encoding="utf-8")) == [{"id": "new"}]
    assert releases["n"] == 1, "exactly one backoff, i.e. the first rename really failed"
    assert store._written_seq["queue.json"] == 1
