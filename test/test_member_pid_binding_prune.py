"""Aged per-pid member-memory binding records are collected.

``<crew home>/member-memory-bindings/pids/`` holds one record per agent process,
left behind by a routing path outside this version. Nothing deletes them and no
sweep collects them, so they accumulate for the install's life --
measured on an operator host at 165,975 files spanning nine days and 24 MB of
directory inode, of which 165,816 named no live process.

These tests pin the collector and, more importantly, the two conditions that
make it safe: a record goes only when it is BOTH aged out AND names no live
process, because a pid number can be recycled in either direction.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kiro_crew import member_memory_auth as mma

DAY = 24 * 3600.0


@pytest.fixture
def pid_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "member-memory-bindings" / "pids"
    root.mkdir(parents=True)
    monkeypatch.setattr(mma, "config_dir", lambda: tmp_path)
    return root


def _record(pid_dir: Path, name: str, *, age_secs: float) -> Path:
    path = pid_dir / name
    path.write_text("{}", encoding="utf-8")
    stamp = time.time() - age_secs
    os.utime(path, (stamp, stamp))
    return path


def _no_pid_is_alive(monkeypatch) -> None:
    monkeypatch.setattr(mma.platform_compat, "pid_exists", lambda _pid: False)


def _every_pid_is_alive(monkeypatch) -> None:
    monkeypatch.setattr(mma.platform_compat, "pid_exists", lambda _pid: True)


def test_an_aged_record_for_a_dead_pid_is_removed(pid_dir, monkeypatch):
    _no_pid_is_alive(monkeypatch)
    plain = _record(pid_dir, "111.json", age_secs=3 * DAY)
    namespaced = _record(pid_dir, "222.namespace.json", age_secs=3 * DAY)

    assert mma.prune_legacy_member_pid_bindings() == 2

    assert not plain.exists()
    assert not namespaced.exists()


def test_a_young_record_is_kept_even_when_its_pid_is_dead(pid_dir, monkeypatch):
    """Age alone is not the condition: a just-written record must survive."""
    _no_pid_is_alive(monkeypatch)
    fresh = _record(pid_dir, "333.json", age_secs=60.0)

    assert mma.prune_legacy_member_pid_bindings() == 0

    assert fresh.exists()


def test_an_aged_record_whose_pid_is_alive_is_kept(pid_dir, monkeypatch):
    """The pid-reuse guard. The number may now name an unrelated live process,
    and deleting its record on age alone would retract a live binding."""
    _every_pid_is_alive(monkeypatch)
    aged = _record(pid_dir, "444.json", age_secs=9 * DAY)

    assert mma.prune_legacy_member_pid_bindings() == 0

    assert aged.exists()


def test_an_unrecognised_name_is_left_alone(pid_dir, monkeypatch):
    """This sweep owns exactly the record shape it can attribute to a pid; a
    directory the operator or a later version put something else in is not its
    to empty."""
    _no_pid_is_alive(monkeypatch)
    other = _record(pid_dir, "README.txt", age_secs=9 * DAY)
    nested = _record(pid_dir, "notapid.json", age_secs=9 * DAY)

    assert mma.prune_legacy_member_pid_bindings() == 0

    assert other.exists()
    assert nested.exists()


def _every_record_is_aged(monkeypatch) -> None:
    """Move the module's clock forward so the age gate admits every record.

    Used where the age gate is NOT the property under test. Preferred over
    ``os.utime(..., follow_symlinks=False)``, which raises
    ``NotImplementedError`` on Windows (``os.utime`` there has no
    ``follow_symlinks`` support), and over a zero ``min_age_secs``, whose cutoff
    lands on ``time.time()`` itself and races a just-created file's mtime on a
    coarse-timestamp filesystem.
    """

    class _Clock:
        @staticmethod
        def time() -> float:
            return time.time() + 30 * DAY

    monkeypatch.setattr(mma, "time", _Clock)


def test_a_symlink_is_never_followed_or_removed(pid_dir, tmp_path, monkeypatch):
    """The directory is same-uid agent-writable, so a planted link must not
    redirect the age reading or the deletion.

    The clock is moved past every record's age so the symlink guard is the ONLY
    thing left that can save the link -- otherwise the link would be retained by
    the age gate and the test would pass for the wrong reason.
    """
    _no_pid_is_alive(monkeypatch)
    _every_record_is_aged(monkeypatch)
    target = tmp_path / "outside.json"
    target.write_text("keep me", encoding="utf-8")
    link = pid_dir / "555.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without links
        pytest.skip("symlinks unavailable on this host")

    assert mma.prune_legacy_member_pid_bindings() == 0

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep me"


def test_the_pass_is_budgeted(pid_dir, monkeypatch):
    """A host that accumulated for weeks must not become one six-figure unlink
    run inside a single maintenance task; the backlog drains over passes."""
    _no_pid_is_alive(monkeypatch)
    for pid in range(600, 610):
        _record(pid_dir, f"{pid}.json", age_secs=9 * DAY)

    assert mma.prune_legacy_member_pid_bindings(budget=4) == 4
    assert len(list(pid_dir.iterdir())) == 6

    assert mma.prune_legacy_member_pid_bindings(budget=100) == 6
    assert list(pid_dir.iterdir()) == []


def test_a_missing_directory_is_not_an_error(tmp_path, monkeypatch):
    """The normal state on an install that never ran the path that wrote it."""
    monkeypatch.setattr(mma, "config_dir", lambda: tmp_path / "nope")

    assert mma.prune_legacy_member_pid_bindings() == 0


def test_the_age_threshold_is_configurable(pid_dir, monkeypatch):
    _no_pid_is_alive(monkeypatch)
    hour_old = _record(pid_dir, "777.json", age_secs=3600.0)

    assert mma.prune_legacy_member_pid_bindings() == 0
    assert mma.prune_legacy_member_pid_bindings(min_age_secs=60.0) == 1

    assert not hour_old.exists()
