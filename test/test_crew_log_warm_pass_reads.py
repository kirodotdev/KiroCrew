"""What one warm session pass costs in file opens and data-home resolutions.

A warm ``fold_session_warm`` pass is paid by every eager wake and every panel read, so
its fixed cost is pinned exactly. The opens left on the idle path each serve a check
the fold needs: the tail scan and header read of ``CrewLog.open``, the header re-read
after the pass that catches a log recreated onto a recycled inode, and the extent
check. The kind root is still checked on every call; only the data home's own
resolution is shared across the pass.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log import CrewLog, bus, eager, emit
from kiro_crew.crew_log import projection as crew_log

SESSION = "s-warm-pass-reads"

_counting = {"on": False, "opens": 0}


def _audit(event: str, args: tuple) -> None:
    if not _counting["on"] or event != "open" or not args:
        return
    target = args[0]
    if isinstance(target, (str, bytes, os.PathLike)) and os.fsdecode(target).endswith("log.jsonl"):
        _counting["opens"] += 1


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()
    yield
    _counting["on"] = False
    eager.stop_for_tests()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()


def _turn(handle: CrewLog, turn: int) -> None:
    handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src="gateway")


def _measured(monkeypatch, before=None) -> tuple[int, int]:
    """(opens of log.jsonl, resolutions of the data home) for one warm pass."""
    home = data_home()
    resolves = [0]
    real_resolve = Path.resolve

    def counted(self, *args, **kwargs):
        if self == home:
            resolves[0] += 1
        return real_resolve(self, *args, **kwargs)

    if before is not None:
        before()
    monkeypatch.setattr(Path, "resolve", counted)
    _counting["opens"] = 0
    _counting["on"] = True
    try:
        crew_log.fold_session_warm(SESSION)
    finally:
        _counting["on"] = False
        monkeypatch.setattr(Path, "resolve", real_resolve)
    return _counting["opens"], resolves[0]


def _warm_unit() -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, SESSION, owner="raymond", agent="kirocrew")
    for turn in range(3):
        _turn(handle, turn)
    crew_log.fold_session_warm(SESSION)
    crew_log.fold_session_warm(SESSION)
    return handle


def test_a_warm_pass_with_nothing_new_opens_the_log_four_times_and_resolves_home_once(
    monkeypatch,
):
    _warm_unit()
    assert _measured(monkeypatch) == (4, 1)


def test_a_warm_pass_over_new_entries_resolves_home_once(monkeypatch):
    handle = _warm_unit()
    assert _measured(monkeypatch, before=lambda: _turn(handle, 9)) == (9, 1)


def test_the_warm_pass_still_folds_the_new_entries():
    handle = _warm_unit()
    seq_before = crew_log.fold_session_warm(SESSION).bundle.last_seq
    _turn(handle, 9)
    assert crew_log.fold_session_warm(SESSION).bundle.last_seq == seq_before + 1
