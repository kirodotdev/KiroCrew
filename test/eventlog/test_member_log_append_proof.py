"""An append may skip the re-read only when the file is provably what is held.

``MemberLog.append`` adds the event it wrote to the events it holds instead of
parsing the whole log again. That is only right while the file is exactly the
events held followed by that event. Each test here puts another writer into one
window of the append -- inserting a record, or rewriting one -- and requires the
instance to end up holding what a fresh load of the file holds.

"Another writer" is a second ``MemberLog`` or a direct edit of the file, the way
the suites next to this one model a second process; one test drives a real
second OS process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from eventlog_helpers import _isolated_home  # noqa: F401 - autouse fixture

from kiro_crew.crew_log.store import CrewLog
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

HISTORY = 40


def _wait_for_a_later_timestamp(path) -> None:
    """Return once a write made now is stamped later than *path* was.

    The kernel stamps files on a coarse clock, so an edit made in the same tick as
    the file's last write leaves every timestamp equal. The tests below edit a file
    right after writing it and need that edit to show, so they wait on the signal
    itself: a probe beside the file is rewritten until its modification time passes
    both of the file's own times. The modification time is the one read from the
    probe because Windows reports a file's creation time as its change time.
    """
    stamped = path.stat()
    held = max(stamped.st_mtime_ns, stamped.st_ctime_ns)
    probe = path.with_name("clock-probe")

    def stamped_later() -> bool:
        probe.write_bytes(b"")
        return probe.stat().st_mtime_ns > held

    wait_until(stamped_later)
    probe.unlink()


def _record(i: int, project: str | None = None) -> dict:
    return {"ts": i, "via": "cli", "project": project or f"p{i:02d}"}


def _seeded(slug: str) -> MemberLog:
    log = MemberLog(slug)
    log.create(slug.title())
    for i in range(1, HISTORY + 1):
        log.append(types.ACTIVITY_RECORD, _record(i))
    log.load()
    return log


def _assert_holds_what_the_file_holds(log: MemberLog) -> None:
    fresh = MemberLog(log.slug)
    fresh.load()
    assert log.events == fresh.events
    assert log.retained_from == fresh.retained_from


def _in_the_write_window(log: MemberLog, monkeypatch, intrude) -> None:
    """Run *intrude* after the append sampled the file and before it writes."""
    real = log._append_through_contention

    def write(*args, **kwargs):
        intrude()
        return real(*args, **kwargs)

    monkeypatch.setattr(log, "_append_through_contention", write)


def _right_after_the_write(log: MemberLog, monkeypatch, intrude) -> None:
    """Run *intrude* after the append wrote and before it checks the file."""
    real = log._append_through_contention

    def write(*args, **kwargs):
        entry = real(*args, **kwargs)
        intrude()
        return entry

    monkeypatch.setattr(log, "_append_through_contention", write)


def _edit_line(log: MemberLog, seq: int, old: bytes, new: bytes) -> None:
    """Rewrite record *seq* in place, keeping every other byte and the file's size."""
    assert len(old) == len(new)
    lines = log.path.read_bytes().split(b"\n")
    assert old in lines[seq], lines[seq]
    lines[seq] = lines[seq].replace(old, new)
    with open(log.path, "r+b") as fh:  # same inode, no truncate
        fh.write(b"\n".join(lines))


# -- another writer inserts a record ---------------------------------------- #


def test_a_record_inserted_before_the_append_is_not_skipped():
    log = _seeded("alice")
    MemberLog("alice").append(types.ACTIVITY_RECORD, _record(98, "theirs"))

    ours = log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    assert ours["seq"] == HISTORY + 2
    _assert_holds_what_the_file_holds(log)


def test_a_record_inserted_in_the_write_window_is_not_skipped(monkeypatch):
    log = _seeded("bob")
    other = MemberLog("bob")
    _in_the_write_window(
        log, monkeypatch, lambda: other.append(types.ACTIVITY_RECORD, _record(98, "theirs"))
    )

    ours = log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    assert ours["seq"] == HISTORY + 2
    _assert_holds_what_the_file_holds(log)


def test_a_record_inserted_right_after_the_write_is_not_skipped(monkeypatch):
    log = _seeded("carol")
    other = MemberLog("carol")
    _right_after_the_write(
        log, monkeypatch, lambda: other.append(types.ACTIVITY_RECORD, _record(98, "theirs"))
    )

    log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    _assert_holds_what_the_file_holds(log)


def test_a_record_inserted_after_the_file_was_checked_is_seen_by_the_next_read(monkeypatch):
    """The stamp must not vouch for a record that landed after the check."""
    log = _seeded("dave")
    other = MemberLog("dave")
    real = CrewLog.tail_records
    once = {"done": False}

    def tail_then_a_commit(self, count):
        found = real(self, count)
        if not once["done"]:
            once["done"] = True
            other.append(types.ACTIVITY_RECORD, _record(98, "theirs"))
        return found

    monkeypatch.setattr(CrewLog, "tail_records", tail_then_a_commit)

    log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    assert once["done"], "the append did not check the file's tail"
    log.refresh_if_changed()
    _assert_holds_what_the_file_holds(log)


def test_a_record_inserted_by_another_os_process_in_the_write_window(monkeypatch, tmp_path):
    log = _seeded("erin")
    script = tmp_path / "other_process.py"
    script.write_text(
        textwrap.dedent("""
            import sys
            from kiro_crew.eventlog import types
            from kiro_crew.eventlog.log import MemberLog

            MemberLog(sys.argv[1]).append(
                types.ACTIVITY_RECORD, {"ts": 98, "via": "cli", "project": "theirs"}
            )
            """),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    def other_process() -> None:
        subprocess.run(
            [sys.executable, str(script), "erin"],
            env=env,
            cwd=tmp_path,
            check=True,
            timeout=FALLBACK_TIMEOUT_SECS,
        )

    _in_the_write_window(log, monkeypatch, other_process)

    ours = log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    assert ours["seq"] == HISTORY + 2
    _assert_holds_what_the_file_holds(log)
    assert [e["data"]["project"] for e in log.events[-2:]] == ["theirs", "ours"]


# -- another writer rewrites a record --------------------------------------- #


def test_an_older_record_rewritten_before_the_append(monkeypatch):
    log = _seeded("faye")
    _wait_for_a_later_timestamp(log.path)
    _edit_line(log, 5, b'"p05"', b'"q05"')

    log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    _assert_holds_what_the_file_holds(log)
    assert log.events[4]["data"]["project"] == "q05"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows reports a file's creation time as its change time, so nothing shows this rewrite",
)
def test_an_older_record_rewritten_with_its_modification_time_put_back():
    """Size and mtime as loaded: only the change time gives the rewrite away."""
    log = _seeded("gina")
    loaded = log.path.stat()
    _wait_for_a_later_timestamp(log.path)
    _edit_line(log, 5, b'"p05"', b'"q05"')
    os.utime(log.path, ns=(loaded.st_atime_ns, loaded.st_mtime_ns))
    now = log.path.stat()
    assert (now.st_size, now.st_mtime_ns) == (loaded.st_size, loaded.st_mtime_ns)

    log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    _assert_holds_what_the_file_holds(log)
    assert log.events[4]["data"]["project"] == "q05"


def test_the_file_swapped_for_another_of_the_same_size_and_modification_time():
    log = _seeded("hana")
    loaded = log.path.stat()
    _wait_for_a_later_timestamp(log.path)
    swapped = log.path.with_name("swapped.jsonl")
    swapped.write_bytes(log.path.read_bytes().replace(b'"p05"', b'"q05"'))
    os.utime(swapped, ns=(loaded.st_atime_ns, loaded.st_mtime_ns))
    os.replace(swapped, log.path)
    now = log.path.stat()
    assert (now.st_size, now.st_mtime_ns) == (loaded.st_size, loaded.st_mtime_ns)
    assert now.st_ino != loaded.st_ino

    log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    _assert_holds_what_the_file_holds(log)
    assert log.events[4]["data"]["project"] == "q05"


def test_an_older_record_removed_in_the_write_window(monkeypatch):
    """The newest record held is untouched; only the offsets moved."""
    log = _seeded("iris")

    def remove_record_five() -> None:
        lines = log.path.read_bytes().split(b"\n")
        del lines[5]
        log.path.write_bytes(b"\n".join(lines))

    _in_the_write_window(log, monkeypatch, remove_record_five)

    ours = log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    assert ours["seq"] == HISTORY + 1, "the tail seq did not move, so seq alone proves nothing"
    _assert_holds_what_the_file_holds(log)
    assert 5 not in [e["seq"] for e in log.events]


def test_the_newest_record_held_rewritten_in_the_write_window(monkeypatch):
    """Same seq, same length, other content: the size and the offsets all agree."""
    log = _seeded("jade")
    _in_the_write_window(log, monkeypatch, lambda: _edit_line(log, HISTORY, b'"p40"', b'"q40"'))

    ours = log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    assert ours["seq"] == HISTORY + 1
    _assert_holds_what_the_file_holds(log)
    assert log.events[-2]["data"]["project"] == "q40"


def test_our_own_record_replaced_right_after_the_write(monkeypatch):
    """Another record sits where ours was written, with the seq ours had."""
    log = _seeded("kate")
    size_before = log.path.stat().st_size
    other = MemberLog("kate")

    def replace_ours() -> None:
        with open(log.path, "r+b") as fh:
            fh.truncate(size_before)
        other.append(types.ACTIVITY_RECORD, _record(98, "theirs"))

    _right_after_the_write(log, monkeypatch, replace_ours)

    ours = log.append(types.ACTIVITY_RECORD, _record(99, "ours"))

    _assert_holds_what_the_file_holds(log)
    assert log.events[-1]["seq"] == ours["seq"]
    assert log.events[-1]["data"]["project"] == "theirs"


# -- control ---------------------------------------------------------------- #


def test_with_no_other_writer_the_append_is_adopted_without_a_reload(monkeypatch):
    """CONTROL: every test above would pass if the append simply always reloaded."""
    log = _seeded("lena")
    reloads = {"n": 0}
    real = MemberLog._ensure_loaded

    def counting(self):
        if not self._loaded:
            reloads["n"] += 1
        return real(self)

    monkeypatch.setattr(MemberLog, "_ensure_loaded", counting)

    for i in range(3):
        log.append(types.ACTIVITY_RECORD, _record(90 + i, "ours"))

    assert reloads["n"] == 0, "an undisturbed append fell back to a reload"
    _assert_holds_what_the_file_holds(log)
