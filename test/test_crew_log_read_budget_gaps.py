"""The read budget on the readers that are not slot folds: page, get, the raw prefix
walks, session folds, a walk from the top of a unit, and resolve.

Real store, real readers: units are written through ``CrewLog`` and read through the
methods gateway code calls, so what is counted is what the store really walked.
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

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, checkpoint
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import read_budget
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.crew_log.schema import KIND_MEMBER, Ref
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SLOT = "chat-readbudget-gaps"
BIG = 8 * read_budget.K
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
    crew_log.forget_session_folds()
    read_budget.reset_for_tests()
    yield
    read_budget.reset_for_tests()
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()


def _unit(unit_id: str, records: int, *, start: int = 0, kind: str = lg.KIND_SESSION) -> None:
    """Append *records* entries to *unit_id*, creating it first."""
    if CrewLog.exists(kind, unit_id):
        handle = CrewLog.open(kind, unit_id)
    elif kind == KIND_MEMBER:
        handle = CrewLog.create(kind, unit_id, name="Ivy")
    else:
        handle = CrewLog.create(kind, unit_id, owner="o", agent="a", slot=SLOT)
    for at in range(0, records, 1000):
        batch = range(start + at, start + min(records, at + 1000))
        if kind == KIND_MEMBER:
            items = [{"type": "slot/opened", "data": {"slot_key": f"s{i}"}} for i in batch]
        else:
            items = [
                {"type": "ledger/recorded", "data": {"slot": SLOT, "goal": f"g{i}"}} for i in batch
            ]
        handle.append_many(items, src="gateway")
    handle.release_ownership()


def _warnings(caplog) -> "list[str]":
    return [r.getMessage() for r in caplog.records if "over budget" in r.getMessage()]


def _open(unit_id: str, kind: str = lg.KIND_SESSION) -> CrewLog:
    return CrewLog.open(kind, unit_id)


def test_a_page_of_a_long_unit_is_reported_and_a_short_one_is_not(caplog):
    _unit("acp-page", BIG)
    _unit("acp-short", 100)
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        for unit_id in ("acp-page", "acp-short"):
            handle = _open(unit_id)
            assert len(handle.page(None, 50).entries) == 50
            handle.release_ownership()
    (line,) = _warnings(caplog)
    assert "fold=page unit=acp-page" in line and f"records={BIG}" in line and "delta=51" in line
    assert "test_crew_log_read_budget_gaps.py:" in line


def test_a_get_near_the_end_is_reported_with_delta_one(caplog):
    _unit("acp-get", BIG)
    read_budget.mark_gateway_process()
    handle = _open("acp-get")
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        assert handle.get(BIG - 1).seq == BIG - 1
        assert handle.get(3).seq == 3
    handle.release_ownership()
    (line,) = _warnings(caplog)
    assert "fold=get unit=acp-get" in line and f"records={BIG - 1}" in line and "delta=1" in line


def test_a_digest_witness_walks_the_prefix_and_is_reported_and_a_cut_witness_is_not(caplog):
    _unit("acp-cuts", BIG)
    _unit("acp-digest", BIG)
    (lg.crew_log_path(lg.KIND_SESSION, "acp-digest").parent / crew_log_store._CUTS_FILE).unlink()
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        for unit_id in ("acp-cuts", "acp-digest"):
            handle = _open(unit_id)
            witness = checkpoint.fold_witness(handle, BIG)
            assert witness is not None and checkpoint.witness_unchanged(handle, witness)
            handle.release_ownership()
            if unit_id == "acp-cuts":
                assert _warnings(caplog) == [], "a cut-count witness reads no prefix"
    lines = _warnings(caplog)
    assert any("fold=records_through unit=acp-digest" in x and "delta=0" in x for x in lines)
    assert any("fold=digest unit=acp-digest" in x and f"records={BIG}" in x for x in lines)


def test_a_cold_session_fold_is_reported_and_a_resume_from_its_savepoint_is_not(
    caplog, monkeypatch
):
    _unit("acp-session", BIG)
    read_budget.mark_gateway_process()
    seen: "list[tuple[str, int, int, bool]]" = []
    real = read_budget._settle

    def spy(tally: read_budget.Tally) -> None:
        seen.append((tally.fold, tally.records, tally.below, tally.cold))
        real(tally)

    monkeypatch.setattr(read_budget, "_settle", spy)
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        cold = crew_log.fold_session("acp-session", ("status",))
        assert seen[-1][0] == "session" and seen[-1][3] is True and seen[-1][1] >= BIG
        _unit("acp-session", 25, start=BIG)
        crew_log.forget_session_folds()
        warm = crew_log.fold_session("acp-session", ("status",))
    fold, records, below, is_cold = seen[-1]
    assert fold == "session" and not is_cold and records - below == 25 and below <= 1
    assert warm.last_seq == cold.last_seq + 25
    (line,) = _warnings(caplog)
    assert "slot=acp-session fold=session" in line and "cold=True" in line


def test_a_session_resume_that_still_starts_at_record_one_is_cold(caplog):
    """One requested fold has no savepoint, so the walk starts at record 1: cold.

    ``status`` resumes from its savepoint and ``tools`` has none, so the shared floor
    is 0 and the pass reads the whole unit. Resuming ``status`` alone starts above 0.
    """
    _unit("acp-partial", BIG)
    crew_log.fold_session("acp-partial", ("status",))  # writes the status savepoint
    crew_log.forget_session_folds()
    _unit("acp-partial", 25, start=BIG)
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        crew_log.fold_session("acp-partial", ("status",))
        assert _warnings(caplog) == [], "a resume above record 1 reads its delta"
        crew_log.forget_session_folds()
        crew_log.fold_session("acp-partial", ("status", "tools"))
    (line,) = _warnings(caplog)
    assert "slot=acp-partial fold=session" in line and "cold=True" in line and "delta=0" in line


def test_a_walk_from_the_top_of_a_member_log_is_cold_and_a_short_one_is_not(caplog):
    _unit("ivy", BIG, kind=KIND_MEMBER)
    _unit("acp-short", 100)
    read_budget.mark_gateway_process()
    with caplog.at_level(logging.WARNING, logger=read_budget.__name__):
        assert len(list(MemberLog("ivy").iter_events())) == BIG
        short = _open("acp-short")
        assert len(list(short.iter_from(1))) == 100
        short.release_ownership()
    lines = _warnings(caplog)
    # Two call sites in the member log each walk it whole: the load and iter_events.
    assert len(lines) == 2 and all(
        "fold=iter_from unit=ivy" in x and "delta=0" in x and "cold=True" in x for x in lines
    )
    assert read_budget.snapshot()["cold_over_budget"] == 2


def test_resolve_reads_only_up_to_the_cited_span(monkeypatch):
    _unit("acp-cited", BIG)
    read_budget.mark_gateway_process()
    handle = _open("acp-cited")
    seen: "list[int]" = []
    real = CrewLog._iter_segments_located

    def counting(self, *args, **kwargs):
        for item in real(self, *args, **kwargs):
            seen.append(item[0].seq)
            yield item

    monkeypatch.setattr(CrewLog, "_iter_segments_located", counting)
    outcome = handle.resolve(Ref(unit=lg.KIND_SESSION, id="acp-cited", from_seq=2, to_seq=4))
    handle.release_ownership()
    assert [entry.seq for entry in outcome.entries] == [2, 3, 4]
    assert max(seen) == 5, "the walk stops at the first record past the span"


def test_child_process_reads_of_every_kind_are_not_counted():
    _unit("acp-big", BIG)
    _unit("acp-nocuts", BIG)
    (lg.crew_log_path(lg.KIND_SESSION, "acp-nocuts").parent / crew_log_store._CUTS_FILE).unlink()
    read_budget.mark_gateway_process()
    child = (
        "import json\n"
        "from kiro_crew import crew_log as lg\n"
        "from kiro_crew.crew_log import CrewLog, checkpoint, projection, read_budget\n"
        "h = CrewLog.open(lg.KIND_SESSION, 'acp-big')\n"
        "h.page(None, 10); h.get(5); list(h.iter_from(1))\n"
        "projection.fold_session('acp-big', ('status',))\n"
        "d = CrewLog.open(lg.KIND_SESSION, 'acp-nocuts')\n"
        f"checkpoint.witness_unchanged(d, checkpoint.fold_witness(d, {BIG}))\n"
        "print(json.dumps(read_budget.snapshot()))\n"
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
    counts = json.loads(done.stdout.strip().splitlines()[-1])
    assert counts["over_budget"] == 0 and counts["mode"] == "off"
    assert "over budget" not in done.stderr
    assert read_budget.snapshot()["over_budget"] == 0


# --------------------------------------------------------------------------- #
# Ratchet: callers of the whole-unit read primitives. The count may only go down.
# --------------------------------------------------------------------------- #

#: Today's callers, per file. Lower a number when a caller moves to a bounded read;
#: never raise one or add a file.
PINNED_WHOLE_UNIT_CALLERS: "dict[str, int]" = {
    "crew_log/emit_session.py": 1,
    "crew_log/projection_readers.py": 2,
    "crew_log/projection_work.py": 1,
    "crew_log/read.py": 1,
    "dashboard_templates/instance.py": 1,
    "eventlog/log.py": 4,
}


def _whole_unit_read(call: ast.Call) -> bool:
    """``.page(``, ``.thread_page(``, ``<log or handle>.get(<...seq>)``, ``iter_from(1``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in ("page", "thread_page"):
        return True
    if func.attr == "iter_from":
        first = call.args[0] if call.args else None
        seq_kw = next((kw.value for kw in call.keywords if kw.arg == "seq"), None)
        start = first if first is not None else seq_kw
        return start is None or (isinstance(start, ast.Constant) and start.value == 1)
    if func.attr == "get" and len(call.args) == 1:
        owner = ast.unparse(func.value).lower()
        arg = ast.unparse(call.args[0])
        return ("log" in owner or "handle" in owner) and arg.endswith("seq")
    return False


def _whole_unit_callers() -> "Counter[str]":
    found: "Counter[str]" = Counter()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "crew_log/store_handle.py" or "/_vendor/" in f"/{rel}":
            continue
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in ("page(", "iter_from", ".get(")):
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and _whole_unit_read(node):
                found[rel] += 1
    return found


def test_callers_of_the_whole_unit_reads_only_go_down():
    found = _whole_unit_callers()
    grown = {rel: n for rel, n in found.items() if n > PINNED_WHOLE_UNIT_CALLERS.get(rel, 0)}
    assert not grown, (
        f"new callers of a whole-unit crew-log read: {grown}. Pass a resume point, read "
        "a bounded tail, or fold in a child process."
    )
    stale = {rel: n for rel, n in PINNED_WHOLE_UNIT_CALLERS.items() if found.get(rel, 0) < n}
    assert not stale, f"callers moved off a whole-unit read: lower these pins to {dict(found)}"
