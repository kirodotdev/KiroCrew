"""The gateway's crew-log read budget: records decoded per call against delta + K.

Real store, real folds: units are written through ``CrewLog`` and read through the slot
fold the dashboard uses, so what is counted is what ``iter_from`` really decoded.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, checkpoint
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import read_budget
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SLOT = "chat-readbudget"
FOLD = "ledger"
SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.delenv(read_budget.MODE_ENV, raising=False)
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    read_budget.reset_for_tests()
    yield
    read_budget.reset_for_tests()
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _unit(unit_id: str, records: int, *, start: int = 0) -> None:
    """Append *records* ``ledger/recorded`` entries to *unit_id*, creating it first."""
    if CrewLog.exists(lg.KIND_SESSION, unit_id):
        handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    else:
        handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="o", agent="a", slot=SLOT)
    for at in range(0, records, 1000):
        batch = range(start + at, start + min(records, at + 1000))
        handle.append_many(
            [{"type": "ledger/recorded", "data": {"slot": SLOT, "goal": f"g{i}"}} for i in batch],
            src="gateway",
        )
    handle.release_ownership()


def _settled(monkeypatch) -> "list[tuple[str, int, int, bool]]":
    """Every call the budget settles, as ``(fold, records, below, cold)``."""
    seen: "list[tuple[str, int, int, bool]]" = []
    real = read_budget._settle

    def spy(tally: read_budget.Tally) -> None:
        seen.append((tally.fold, tally.records, tally.below, tally.cold))
        real(tally)

    monkeypatch.setattr(read_budget, "_settle", spy)
    return seen


def _forget_everything() -> None:
    """Drop the warm cell and the savepoint, so the next read folds cold."""
    crew_log.forget_slot_folds()
    handle = CrewLog.open(lg.KIND_SESSION, "acp-big")
    checkpoint.discard_slot(handle, (FOLD,))
    handle.release_ownership()


def board_reader(units: "list[str]") -> Any:
    """A gateway caller of the cold-fold API: the call site a warning must name."""
    return crew_log.fold_slot_warm_revised(FOLD, units, slot=SLOT)[0]


def test_k_is_the_savepoint_interval():
    assert read_budget.K == checkpoint.MIN_ADVANCE_ENTRIES


def test_a_cold_fold_on_a_gateway_thread_is_reported_with_its_call_site(caplog):
    _unit("acp-big", 3 * read_budget.K)
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        board_reader(["acp-big"])
    warnings = [r.getMessage() for r in caplog.records if "over budget" in r.getMessage()]
    assert len(warnings) == 1
    line = warnings[0]
    assert f"slot={SLOT}" in line and f"fold={FOLD}" in line
    assert f"records={3 * read_budget.K}" in line and "delta=0" in line and "cold=True" in line
    assert "test_crew_log_read_budget.py:" in line and "board_reader" in line
    counts = read_budget.snapshot()
    assert counts["over_budget"] == 1 and counts["cold_over_budget"] == 1
    assert counts["max_records"] == 3 * read_budget.K and counts["mode"] == "report"


def test_the_same_call_site_warns_once_and_counts_every_time(caplog):
    _unit("acp-big", 2 * read_budget.K)
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        board_reader(["acp-big"])
        _forget_everything()  # so the second pass is cold again too
        board_reader(["acp-big"])
    assert sum("over budget" in r.getMessage() for r in caplog.records) == 1
    assert read_budget.snapshot()["over_budget"] == 2


@pytest.mark.parametrize("scale", [1, 4, 16, 64])
def test_warm_and_restart_reads_stay_within_delta_plus_k_at_every_size(scale, monkeypatch):
    size = 40 * scale
    delta = 25
    _unit("acp-old", size)
    _unit("acp-new", size)
    units = ["acp-old", "acp-new"]
    read_budget.mark_gateway_process()
    seen = _settled(monkeypatch)
    assert board_reader(units).last_seq == size
    assert seen[-1] == (FOLD, 2 * size, 0, True)
    cold_counted = 1 if 2 * size > read_budget.K else 0
    assert read_budget.snapshot()["over_budget"] == cold_counted

    _unit("acp-new", delta, start=size)
    assert board_reader(units).last_seq == size + delta
    _fold, records, below, is_cold = seen[-1]
    # A warm read decodes its delta and the one record at its resume point.
    assert not is_cold and records - below == delta and below <= 1

    crew_log.forget_slot_folds()  # a restart: only the savepoint (if one was owed) is left
    _unit("acp-new", delta, start=size + delta)
    resumed = board_reader(units)
    _fold, records, below, is_cold = seen[-1]
    if is_cold:  # no savepoint yet: only a slot below K folds cold again
        assert records <= read_budget.K
    else:
        assert records - below == 2 * delta and below <= 1
    crew_log.forget_slot_folds()
    read_budget.reset_for_tests()
    expected = board_reader(units)  # read again, uncounted
    assert (resumed.last_seq, resumed.state) == (expected.last_seq, expected.state)
    assert read_budget.snapshot()["over_budget"] == 0


def test_an_iter_from_with_no_resume_point_counts_what_it_skipped(caplog):
    _unit("acp-tail", 4 * read_budget.K)
    read_budget.mark_gateway_process()
    handle = CrewLog.open(lg.KIND_SESSION, "acp-tail")
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        tail = list(handle.iter_from(handle.last_seq))
    handle.release_ownership()
    assert len(tail) == 1
    (line,) = [r.getMessage() for r in caplog.records if "over budget" in r.getMessage()]
    assert "fold=iter_from unit=acp-tail" in line and "delta=1" in line
    assert f"records={4 * read_budget.K}" in line


def test_one_call_site_walking_many_units_warns_once(caplog):
    for at in range(3):
        _unit(f"acp-tail-{at}", 2 * read_budget.K)
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        for at in range(3):
            handle = CrewLog.open(lg.KIND_SESSION, f"acp-tail-{at}")
            list(handle.iter_from(handle.last_seq))
            handle.release_ownership()
    assert sum("over budget" in r.getMessage() for r in caplog.records) == 1
    assert read_budget.snapshot()["over_budget"] == 3


def test_nothing_is_counted_outside_the_gateway_or_when_off(monkeypatch):
    _unit("acp-big", 2 * read_budget.K)
    board_reader(["acp-big"])  # this process never marked itself
    assert read_budget.snapshot() == {
        "over_budget": 0,
        "cold_over_budget": 0,
        "max_records": 0,
        "mode": "off",
        "k": read_budget.K,
    }
    crew_log.forget_slot_folds()
    read_budget.mark_gateway_process()
    monkeypatch.setenv(read_budget.MODE_ENV, "off")
    board_reader(["acp-big"])
    assert read_budget.snapshot()["over_budget"] == 0


def test_a_child_process_backfill_is_not_counted(tmp_path):
    """A spawned child (a fresh interpreter, as every gateway child is) folds cold uncounted."""
    _unit("acp-big", 3 * read_budget.K)
    read_budget.mark_gateway_process()
    child = (
        "import json\n"
        "from kiro_crew.crew_log import projection, read_budget\n"
        f"cp = projection.fold_slot_warm_revised({FOLD!r}, ['acp-big'], slot={SLOT!r})[0]\n"
        "print(json.dumps({'seq': cp.last_seq, 'counts': read_budget.snapshot()}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC.parent) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", child],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    reply = json.loads(done.stdout.strip().splitlines()[-1])
    assert reply["seq"] == 3 * read_budget.K
    assert reply["counts"]["over_budget"] == 0 and reply["counts"]["mode"] == "off"
    assert "over budget" not in done.stderr
    assert read_budget.snapshot()["over_budget"] == 0


# --------------------------------------------------------------------------- #
# Ratchet: gateway callers of the cold-fold API. The count may only go down.
# --------------------------------------------------------------------------- #

#: Calls that can fold a slot from its first record: the default (``cold=True``) form of
#: the slot fold readers, and the always-cold helpers.
_COLD_FOLD_API = frozenset(
    {
        "read_slot_projection",
        "read_slot_state",
        "read_tree_projection",
        "fold_slot_warm",
        "fold_slot_warm_revised",
        "fold_slot",
        "fold_slot_checkpoint",
        "fold_tree_checkpoint",
        "_fold_slot_cold",
    }
)
#: The API's own modules, which define it and delegate inside it.
_API_MODULES = frozenset({"crew_log/projection_slot_memo.py", "crew_log/projection_readers.py"})
#: Functions that run only in their own child process, which never marks itself as the
#: gateway, so a cold fold inside one is not the gateway's. Per function, never per file:
#: the rest of such a module runs in the gateway and stays counted.
_CHILD_ONLY_FUNCTIONS: "dict[str, frozenset[str]]" = {
    "crew_log/slot_backfill_child.py": frozenset({"_stage"}),
}

#: Today's callers, per file. Lower a number when a caller moves to ``cold=False`` or a
#: child process; never raise one or add a file.
PINNED_COLD_FOLD_CALLERS: "dict[str, int]" = {
    "apps/builtins/issue_radar/backend/crew_store_log.py": 1,
    "crew_log/slot_prime.py": 1,
    "dashboard/card_lifecycle.py": 2,
    "dashboard/handlers/agent_panel_dashboard.py": 2,
    "dashboard/handlers/agent_panel_read.py": 2,
    "dashboard/handlers/crew_log.py": 3,
    "dashboard/handlers/member_dashboard.py": 1,
    "dashboard/handlers/member_dashboard_fields.py": 1,
    "dashboard/handlers/usage_context.py": 1,
    "dashboard/handlers/work_ledger_board.py": 1,
    "dashboard/session_control_parts/session_status.py": 1,
    "dashboard_agentic.py": 1,
    "session_ledger.py": 1,
    "work_ledger.py": 1,
}


def _passes_cold_false(call: ast.Call) -> bool:
    return any(
        kw.arg == "cold" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in call.keywords
    )


def _cold_fold_callers() -> "Counter[str]":
    found: "Counter[str]" = Counter()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _API_MODULES or "/_vendor/" in f"/{rel}":
            continue
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in _COLD_FOLD_API):
            continue
        calls = _cold_calls_in(rel, source)
        if calls:
            found[rel] += calls
    return found


def _cold_calls_in(rel: str, source: str) -> int:
    """Cold-fold API calls in *source*, less those inside *rel*'s child-only functions."""
    child_only = _CHILD_ONLY_FUNCTIONS.get(rel, frozenset())
    count = 0

    def visit(node: ast.AST, enclosing: str) -> None:
        nonlocal count
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = node.name
        if isinstance(node, ast.Call) and enclosing not in child_only:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _COLD_FOLD_API and not _passes_cold_false(node):
                count += 1
        for child in ast.iter_child_nodes(node):
            visit(child, enclosing)

    visit(ast.parse(source), "")
    return count


def test_only_a_child_only_function_is_exempt_from_the_ratchet():
    source = (
        "def backfill(slot, name):\n"
        "    return projection.read_slot_projection(slot, name)\n"
        "def _stage(home, slot, name):\n"
        "    projection.read_slot_projection(slot, name)\n"
    )
    assert _cold_calls_in("crew_log/slot_backfill_child.py", source) == 1
    assert _cold_calls_in("crew_log/other.py", source) == 2


def test_gateway_callers_of_the_cold_fold_api_only_go_down():
    found = _cold_fold_callers()
    grown = {rel: n for rel, n in found.items() if n > PINNED_COLD_FOLD_CALLERS.get(rel, 0)}
    assert not grown, (
        f"new gateway callers of the cold-fold API: {grown}. Read with cold=False and "
        "answer 'not ready' (crew_log/slot_prime.py), or fold in a child process."
    )
    stale = {rel: n for rel, n in PINNED_COLD_FOLD_CALLERS.items() if found.get(rel, 0) < n}
    assert not stale, f"callers moved off the cold-fold API: lower these pins to {dict(found)}"
