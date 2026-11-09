"""A slot fold's cold backfill in a child process, staged and admitted by the gateway.

The child is a real ``python -m kiro_crew.crew_log.slot_backfill_child`` over real crew
logs in a temp home. The gateway side is this process: it admits the staged savepoint
under the unit's lease and resumes from it, and every way the staged file can be wrong
falls back to what the gateway did before.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import sys
import time
from dataclasses import replace
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_slot_memo as psm
from kiro_crew.crew_log import slot_backfill_child as child
from kiro_crew.crew_log import slot_prime
from kiro_crew.crew_log.checkpoint import slot_checkpoint_path
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SLOT = "chat-backfill"
FOLD = "work"
UNITS = ("acp-backfill-1", "acp-backfill-2")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.delenv(child.SWITCH_ENV, raising=False)
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    child.reset_for_tests()
    yield
    child.reset_for_tests()
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _append(handle: CrewLog, item: str) -> None:
    handle.append(
        "work/recorded",
        {
            "slot": SLOT,
            "actor": "conductor",
            "by": SLOT,
            "action": "create",
            "item_id": item,
            "title": f"item {item}",
        },
        src="gateway",
    )


def _slot(entries: int = 200) -> None:
    for unit in UNITS:
        handle = CrewLog.create(lg.KIND_SESSION, unit, owner="o", agent="kirocrew", slot=SLOT)
        for nth in range(entries):
            _append(handle, f"{unit}-{nth}")
        del handle
    gc.collect()


def _grow(entries: int) -> None:
    handle = CrewLog.open(lg.KIND_SESSION, UNITS[-1])
    for nth in range(entries):
        _append(handle, f"late-{nth}")
    del handle
    gc.collect()


def _value(folded: Any) -> str:
    return json.dumps(getattr(folded, "value", folded), sort_keys=True, default=str)


def _no_savepoint() -> None:
    """Drop the gateway's memo and every slot savepoint, so a read has nothing to resume."""
    crew_log.forget_slot_folds()
    for unit in UNITS:
        slot_checkpoint_path(lg.KIND_SESSION, unit, FOLD).unlink(missing_ok=True)


def _cold_value() -> str:
    """What the gateway's own cold fold answers for the files as they are now."""
    _no_savepoint()
    try:
        return _value(crew_log.read_slot_projection(SLOT, FOLD))
    finally:
        _no_savepoint()


def _staged() -> "os.PathLike[str]":
    return child.staged_path(UNITS[-1], FOLD)


def _stage_with_a_real_child() -> None:
    child.enable()
    assert child.backfill(SLOT, FOLD)
    assert os.path.isfile(_staged())


def _warm_read() -> str:
    return _value(crew_log.read_slot_projection(SLOT, FOLD, cold=False))


# --------------------------------------------------------------------------- #
# the real child, end to end
# --------------------------------------------------------------------------- #


def test_a_real_child_stages_a_savepoint_the_gateway_admits_and_resumes(monkeypatch):
    _slot()
    expected = _cold_value()
    _stage_with_a_real_child()
    colds: list[str] = []
    real_cold = psm._fold_slot_cold
    monkeypatch.setattr(
        psm, "_fold_slot_cold", lambda *a, **k: colds.append(a[0]) or real_cold(*a, **k)
    )

    assert _warm_read() == expected
    assert colds == [], "the gateway folded cold after all"
    assert not os.path.exists(_staged())
    assert slot_checkpoint_path(lg.KIND_SESSION, UNITS[-1], FOLD).is_file()


def test_the_prime_uses_the_child_and_never_folds_cold_on_its_own_thread(monkeypatch):
    _slot()
    expected = _cold_value()
    child.enable()
    colds: list[str] = []
    real_cold = psm._fold_slot_cold
    monkeypatch.setattr(
        psm, "_fold_slot_cold", lambda *a, **k: colds.append(a[0]) or real_cold(*a, **k)
    )
    published: list[Any] = []
    monkeypatch.setattr(
        crew_log_eager, "_publish_fold", lambda slot, name, value: published.append(value)
    )

    slot_prime.schedule(SLOT, FOLD, for_reader=True)
    assert slot_prime.drain(timeout=FALLBACK_TIMEOUT_SECS)
    slot_prime.reset_for_tests()

    assert colds == []
    assert [_value(value) for value in published] == [expected]


def test_a_unit_that_grew_after_the_child_resumes_to_the_cold_value():
    _slot()
    _stage_with_a_real_child()
    _grow(40)
    warm = _warm_read()
    assert warm == _cold_value()


# --------------------------------------------------------------------------- #
# a staged file the gateway must refuse
# --------------------------------------------------------------------------- #


def test_a_staged_file_for_another_unit_list_is_refused():
    _slot()
    _stage_with_a_real_child()
    # The slot gains a unit: the staged identity block names the old list.
    third = CrewLog.create(
        lg.KIND_SESSION, "acp-backfill-3", owner="o", agent="kirocrew", slot=SLOT
    )
    _append(third, "new-unit")
    del third
    gc.collect()
    with pytest.raises(psm.SlotFoldNotReady):
        crew_log.read_slot_projection(SLOT, FOLD, cold=False)
    # Staged at the old newest unit, so never moved into the new one; the sweep collects it.
    assert os.path.exists(_staged())
    assert not slot_checkpoint_path(lg.KIND_SESSION, "acp-backfill-3", FOLD).exists()


def test_a_staged_file_of_another_state_version_is_refused():
    _slot()
    _stage_with_a_real_child()
    payload = json.loads(open(_staged(), encoding="utf-8").read())
    payload["state_version"] = "not-this-build"
    with open(_staged(), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
    with pytest.raises(psm.SlotFoldNotReady):
        crew_log.read_slot_projection(SLOT, FOLD, cold=False)
    # Admitted, then refused by the resume's own checks: not skipped before them.
    assert not os.path.exists(_staged())


def test_a_cut_after_the_child_is_refused():
    from kiro_crew.crew_log.store_cuts import _close_cut, _open_cut

    _slot()
    _stage_with_a_real_child()
    directory = lg.crew_log_path(lg.KIND_SESSION, UNITS[-1]).parent
    _open_cut(directory)
    _close_cut(directory)
    with pytest.raises(psm.SlotFoldNotReady):
        crew_log.read_slot_projection(SLOT, FOLD, cold=False)
    # Admitted, then refused by the resume's own checks: not skipped before them.
    assert not os.path.exists(_staged())


def test_a_staged_file_for_a_removed_unit_is_deleted_not_admitted():
    _slot()
    _stage_with_a_real_child()
    from kiro_crew.crew_log.store import remove_unit

    crew_log_emit.reset_caches()
    gc.collect()
    remove_unit(lg.KIND_SESSION, UNITS[-1], guard=lambda _path: True)
    assert child.admit(FOLD, UNITS[-1]) is False
    assert not os.path.exists(_staged())


def test_a_stale_staged_file_is_swept_before_the_next_child(tmp_path):
    _slot()
    stale = _staged()
    os.makedirs(os.path.dirname(stale), exist_ok=True)
    with open(stale, "w", encoding="utf-8") as handle:
        handle.write("{}")
    old = time.time() - child.STALE_STAGED_SECS - 60
    os.utime(stale, (old, old))
    child.enable()
    child.backfill(SLOT, FOLD, argv=[sys.executable, "-c", "raise SystemExit(3)"])
    assert not os.path.exists(stale)


# --------------------------------------------------------------------------- #
# the child failing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv, timeout",
    [
        ([sys.executable, "-c", "raise SystemExit(3)"], 60.0),
        ([sys.executable, "-c", "print('not json')"], 60.0),
        ([sys.executable, "-c", "import time; time.sleep(30)"], 0),
        (["/nonexistent/python-for-a-test"], 60.0),
    ],
    ids=["crashes", "bad-reply", "times-out", "cannot-start"],
)
def test_a_failed_child_answers_false_and_the_prime_folds_in_the_gateway(
    monkeypatch, argv, timeout
):
    _slot()
    expected = _cold_value()
    child.enable()
    assert child.backfill(SLOT, FOLD, argv=argv, timeout=timeout) is False
    monkeypatch.setattr(child, "backfill", lambda *a, **k: False)
    assert _value(slot_prime._read_backfilled(SLOT, FOLD)) == expected


def test_three_failures_in_a_row_turn_the_child_off_with_one_warning(caplog):
    _slot()
    child.enable()
    crash = [sys.executable, "-c", "raise SystemExit(3)"]
    with caplog.at_level(logging.INFO, logger=child.__name__):
        for _ in range(4):
            child.backfill(SLOT, FOLD, argv=crash)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "3 times in a row" in warnings[0].getMessage()
    assert child.usable() is False


def test_only_one_child_runs_at_a_time():
    _slot()
    child.enable()
    assert child._running.acquire(blocking=False)
    try:
        assert (
            child.backfill(SLOT, FOLD, argv=[sys.executable, "-c", "print('{\"ok\":true}')"])
            is False
        )
    finally:
        child._running.release()


# --------------------------------------------------------------------------- #
# off
# --------------------------------------------------------------------------- #


def test_off_by_default_and_by_switch(monkeypatch):
    ran = [sys.executable, "-c", "print('{\"ok\":true}')"]
    assert child.backfill(SLOT, FOLD, argv=ran) is False  # never enabled
    child.enable()
    monkeypatch.setenv(child.SWITCH_ENV, "0")
    assert child.usable() is False
    assert child.backfill(SLOT, FOLD, argv=ran) is False
    _slot()
    expected = _cold_value()
    # Off: the prime's read is the plain cold read, as before the child existed.
    assert _value(slot_prime._read_backfilled(SLOT, FOLD)) == expected


def test_the_gateway_read_after_a_backfill_stays_within_the_read_budget():
    from kiro_crew.crew_log import read_budget

    _slot()
    _stage_with_a_real_child()
    read_budget.reset_for_tests()
    read_budget.mark_gateway_process()
    try:
        _warm_read()
        counts = read_budget.snapshot()
        # The same slot folded cold on this thread is over budget: what the child spared.
        _no_savepoint()
        crew_log.read_slot_projection(SLOT, FOLD)
        cold_counts = read_budget.snapshot()
    finally:
        read_budget.reset_for_tests()
    assert counts["over_budget"] == 0 and counts["cold_over_budget"] == 0, counts
    assert cold_counts["cold_over_budget"] == 1, cold_counts


def test_an_oversize_cell_spawns_no_child_and_folds_once(monkeypatch):
    _slot()
    # A ceiling the work cell is over: the gateway's own fold records the refusal.
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, "1024")
    crew_log.read_slot_projection(SLOT, FOLD)
    assert crew_log.slot_fold_over_ceiling(SLOT, FOLD)
    for unit in UNITS:  # the refusal stays remembered; only the files go
        slot_checkpoint_path(lg.KIND_SESSION, unit, FOLD).unlink(missing_ok=True)
    child.enable()
    spawned: list[Any] = []
    monkeypatch.setattr(child.subprocess, "run", lambda *a, **k: spawned.append(a))
    colds: list[str] = []
    real_cold = psm._fold_slot_cold
    monkeypatch.setattr(
        psm, "_fold_slot_cold", lambda *a, **k: colds.append(a[0]) or real_cold(*a, **k)
    )

    slot_prime._read_backfilled(SLOT, FOLD)

    assert spawned == []
    assert colds == [FOLD]


def test_a_child_that_staged_nothing_is_not_a_failure():
    _slot()
    child.enable()
    nothing = [sys.executable, "-c", 'print(\'{"ok": true, "staged": false}\')']
    for _ in range(4):
        assert child.backfill(SLOT, FOLD, argv=nothing) is False
    assert child.usable() is True
    old_shape = [sys.executable, "-c", "print('{\"ok\": true}')"]
    assert child.backfill(SLOT, FOLD, argv=old_shape) is False


def _count_spawns(monkeypatch) -> "list[Any]":
    spawned: list[Any] = []
    real_run = child.subprocess.run
    monkeypatch.setattr(
        child.subprocess, "run", lambda *a, **k: spawned.append(a) or real_run(*a, **k)
    )
    return spawned


def test_a_slot_under_the_savepoint_threshold_spawns_no_child_and_folds_once(monkeypatch):
    from kiro_crew.crew_log.checkpoint import MIN_ADVANCE_ENTRIES

    _slot(entries=(MIN_ADVANCE_ENTRIES - 10) // len(UNITS))
    child.enable()
    spawned = _count_spawns(monkeypatch)
    colds: list[str] = []
    real_cold = psm._fold_slot_cold
    monkeypatch.setattr(
        psm, "_fold_slot_cold", lambda *a, **k: colds.append(a[0]) or real_cold(*a, **k)
    )

    slot_prime._read_backfilled(SLOT, FOLD)

    assert spawned == []
    assert colds == [FOLD]


def test_a_slot_with_an_unknown_mark_spawns_no_child(monkeypatch):
    _slot()
    child.enable()
    spawned = _count_spawns(monkeypatch)
    real_marks = psm._unit_marks
    monkeypatch.setattr(
        psm,
        "_unit_marks",
        lambda units, home: tuple(
            replace(mark, origin=None) if n == 0 else mark
            for n, mark in enumerate(real_marks(units, home))
        ),
    )
    assert child.backfill(SLOT, FOLD) is False
    assert spawned == []


def test_a_slot_at_the_threshold_still_spawns_and_resumes(monkeypatch):
    _slot()
    expected = _cold_value()
    child.enable()
    spawned = _count_spawns(monkeypatch)
    assert child.backfill(SLOT, FOLD) is True
    assert len(spawned) == 1
    assert _warm_read() == expected


# --------------------------------------------------------------------------- #
# the child's own code, in this process
# --------------------------------------------------------------------------- #


@pytest.fixture
def _child_mode_restored(monkeypatch):
    from kiro_crew.crew_log import checkpoint

    # _stage sets both; the monkeypatch puts them back after the test.
    monkeypatch.setattr(checkpoint, "slot_save_dir", None)
    monkeypatch.setenv("KIROCREW_HOME", os.environ["KIROCREW_HOME"])


@pytest.mark.parametrize(
    "raw", [b"not json", b"[1, 2]", b'{"home": "h", "slot": "s"}'], ids=["text", "list", "short"]
)
def test_a_bad_request_is_refused(raw):
    assert child._parse_request(raw) is None


def test_a_good_request_is_read():
    raw = json.dumps({"home": "h", "slot": "s", "fold": "work"}).encode()
    assert child._parse_request(raw) == ("h", "s", "work")


@pytest.mark.parametrize("args, raw", [(["extra"], b"{}"), ([], b"not json")])
def test_main_answers_usage_for_a_bad_call(monkeypatch, args, raw):
    import io

    monkeypatch.setattr(child.sys, "stdin", type("_In", (), {"buffer": io.BytesIO(raw)})())
    assert child.main(args) == 2


def test_stage_in_this_process_stages_the_savepoint(_child_mode_restored):
    _slot()
    assert child._stage(os.environ["KIROCREW_HOME"], SLOT, FOLD) is True
    assert os.path.isfile(_staged())


def test_stage_in_this_process_reports_nothing_for_a_slot_that_earns_no_savepoint(
    _child_mode_restored,
):
    _slot(entries=10)
    assert child._stage(os.environ["KIROCREW_HOME"], SLOT, FOLD) is False
    assert not os.path.exists(_staged())


def test_a_staging_directory_that_cannot_be_made_stages_nothing(monkeypatch):
    from kiro_crew import owner_only_files

    def refuse(_path):
        raise OSError("read-only")

    monkeypatch.setattr(owner_only_files, "ensure_directory", refuse)
    handle = type("_Handle", (), {"id": UNITS[0]})()
    assert child._staging_dir_for(handle) is None


def test_the_stamps_and_the_sweep_survive_a_missing_or_odd_staging_root():
    _slot()
    assert child._staged_stamps(FOLD) == {}  # no staging root yet
    root = child.staging_root()
    root.mkdir(parents=True, mode=0o700)
    (root / "stray-file").write_text("x")
    (root / "empty-unit").mkdir(mode=0o700)
    assert child._staged_stamps(FOLD) == {}
    child._sweep_stale()  # a stray file at the root does not raise


def test_admit_answers_false_when_the_unit_dir_or_the_move_fails(monkeypatch):
    from kiro_crew.crew_log import checkpoint

    _slot()
    _stage_with_a_real_child()
    monkeypatch.setattr(checkpoint, "_ensure_dir", lambda *_a, **_k: None)
    assert child.admit(FOLD, UNITS[-1]) is False
    assert os.path.exists(_staged())

    def broken(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(checkpoint, "_ensure_dir", broken)
    assert child.admit(FOLD, UNITS[-1]) is False


def test_admit_is_off_in_the_child(monkeypatch):
    from kiro_crew.crew_log import checkpoint

    monkeypatch.setattr(checkpoint, "slot_save_dir", lambda _handle: None)
    assert child.admit(FOLD, UNITS[-1]) is False
