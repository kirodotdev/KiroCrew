"""The read half of the Windows sharing-violation window (issue #4331).

``CrewStore._load`` read with a bare ``read_text`` and mapped every ``OSError``
to a fatal ``RuntimeError``. On Windows a read raises ``PermissionError``
(``WinError 32``) while another handle holds the file open for write, and the
store's own builder admits two concurrent first messages for one slot both
build a store — so one can read ``queue.json`` while the other is replacing it.
The user gets a failed send that succeeds on retry, which reads as random.

POSIX permits that read, which is why #4142 could only reproduce it in a test.
``windows_sim.read_sharing_violation`` reproduces the fault deterministically on
any OS, so these drive the exact path a Windows host takes. They do NOT prove
the real OS behaviour end to end, only that the retry is wired, bounded, gated
to Windows, and gated off the event loop.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the bounded retry loop instant; attempt COUNT is what these pin."""
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


@pytest.fixture
def _windows(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)


def _seed(tmp_path, name: str = "queue.json"):
    path = tmp_path / name
    path.write_text(json.dumps([{"id": "q1"}]), encoding="utf-8")
    return path


class TestReadBytesWithRetry:
    def test_a_contended_read_retries_and_succeeds(self, tmp_path, _windows) -> None:
        path = _seed(tmp_path)

        with read_sharing_violation(match="queue.json", times=1) as state:
            data = aw.read_bytes_with_retry(path)

        assert json.loads(data.decode("utf-8")) == [{"id": "q1"}]
        assert state["n"] == 2, "one faulted read, then one that succeeded"

    def test_the_retry_is_bounded(self, tmp_path, _windows) -> None:
        """A permanently contended file must fail, not spin forever."""
        path = _seed(tmp_path)

        with read_sharing_violation(match="queue.json", times=10_000):
            with pytest.raises(PermissionError):
                aw.read_bytes_with_retry(path)

    def test_posix_permission_errors_are_not_slept_over(self, tmp_path, monkeypatch) -> None:
        """On POSIX the OS permits the read, so a PermissionError is a REAL
        access fault — retrying would just delay an honest failure."""
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        path = _seed(tmp_path)

        with read_sharing_violation(match="queue.json", times=1) as state:
            with pytest.raises(PermissionError):
                aw.read_bytes_with_retry(path)

        assert state["n"] == 1, "it must not have tried a second time"

    @pytest.mark.asyncio
    async def test_on_the_event_loop_it_does_not_sleep(self, tmp_path, _windows) -> None:
        """The retry sleeps, so a caller on the gateway loop gets the plain
        single-attempt semantics rather than pausing the one loop."""
        path = _seed(tmp_path)

        with read_sharing_violation(match="queue.json", times=1) as state:
            with pytest.raises(PermissionError):
                aw.read_bytes_with_retry(path)

        assert state["n"] == 1
        assert asyncio.get_running_loop() is not None

    def test_a_missing_file_is_not_retried(self, tmp_path, _windows) -> None:
        """Absence is not contention; sleeping cannot make the file appear."""
        with pytest.raises(FileNotFoundError):
            aw.read_bytes_with_retry(tmp_path / "absent.json")


class TestCrewStoreLoadSurvivesAContendedRead:
    def test_load_reads_through_the_retrying_helper(self, tmp_path, _windows, monkeypatch) -> None:
        """The product path from #4331: a concurrent first message for the same
        slot is replacing ``queue.json`` while this build reads it.

        Asserted as WIRING, deliberately. The pre-fix read was
        ``path.read_text(...)``, and none of the ``windows_sim`` faults reach it
        — ``read_sharing_violation`` patches ``Path.read_bytes`` and
        ``builtin_open_sharing_violation`` patches ``builtins.open``, while
        ``pathlib`` opens through its own ``io.open`` binding. A test that only
        wrapped the old call in a simulator would therefore pass on the unfixed
        tree while proving nothing. Reading bytes is part of the fix precisely
        because it puts the store on a path the existing simulator can fault.
        """
        from kiro_crew import crew_chat
        from kiro_crew.crew_chat import CrewStore

        seen: list[str] = []
        real = crew_chat.read_bytes_with_retry

        def _spy(path):
            seen.append(str(path))
            return real(path)

        monkeypatch.setattr(crew_chat, "read_bytes_with_retry", _spy)

        store = CrewStore.__new__(CrewStore)
        store.dir = tmp_path
        _seed(tmp_path)

        with read_sharing_violation(match="queue.json", times=1):
            assert store._load("queue.json") == [{"id": "q1"}]

        assert seen, "the store must read through the retrying helper, not read_text"

    def test_a_damaged_file_is_still_fatal(self, tmp_path, _windows) -> None:
        """The retry must not soften the guard that exists so a broken file is
        never read as an empty queue and saved back over the real one."""
        from kiro_crew.crew_chat import CrewStore

        store = CrewStore.__new__(CrewStore)
        store.dir = tmp_path
        (tmp_path / "queue.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(RuntimeError, match="unreadable"):
            store._load("queue.json")

    def test_a_missing_file_is_still_an_empty_queue(self, tmp_path, _windows) -> None:
        from kiro_crew.crew_chat import CrewStore

        store = CrewStore.__new__(CrewStore)
        store.dir = tmp_path

        assert store._load("queue.json") == []
