"""The CI read-budget check: an over-budget gateway read at an unlisted site fails a test.

Real store, real reads: a unit past K records is written through ``CrewLog`` and read
whole from a product call site, and :func:`check_read_budget` is called directly. The
fixture resets the budget on both sides, so the floor's own check (on under
``KIROCREW_CREW_LOG_READ_BUDGET=raise``) never sees this file's deliberate reads.
"""

from __future__ import annotations

from pathlib import Path

import conftest_floor_read_budget as floor
import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import emit_crew
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_kernel, read_budget

SLOT = "chat-readbudget-ci"
UNIT = "acp-big"
LISTED = "crew_log/emit_crew.py _newest_dispatch_for"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setenv(read_budget.MODE_ENV, read_budget.MODE_REPORT)
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


def _unit(unit_id: str, records: int) -> CrewLog:
    """A unit of *records* ``ledger/recorded`` entries, reopened for reading."""
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="o", agent="a", slot=SLOT)
    for at in range(0, records, 1000):
        batch = range(at, min(records, at + 1000))
        handle.append_many(
            [{"type": "ledger/recorded", "data": {"slot": SLOT, "goal": f"g{i}"}} for i in batch],
            src="gateway",
        )
    handle.release_ownership()
    return CrewLog.open(lg.KIND_SESSION, unit_id)


def _product_whole_read() -> None:
    """A whole-unit read from a product call site, on a gateway process."""
    handle = _unit(UNIT, 2 * read_budget.K)
    read_budget.mark_gateway_process()
    assert emit_crew._newest_dispatch_for(handle, "item-1", 1) is None
    assert any(floor._site_of(frame)[0] == LISTED for _, frame in read_budget._warned)


def test_an_unlisted_product_site_over_budget_fails_the_check_naming_it(_floor_monkeypatch):
    _floor_monkeypatch.setattr(floor, "KNOWN_OVER_BUDGET_SITES", {})
    _product_whole_read()
    with pytest.raises(pytest.fail.Exception) as failed:
        floor.check_read_budget()
    message = str(failed.value)
    assert LISTED in message and "fold=iter_from" in message
    assert "KNOWN_OVER_BUDGET_SITES" in message and "caller=" in message
    assert not read_budget._warned  # the check resets the budget, pass or fail


def test_a_listed_product_site_passes_the_check():
    assert LISTED in floor.KNOWN_OVER_BUDGET_SITES
    _product_whole_read()
    floor.check_read_budget()
    assert not read_budget._warned


def test_a_tests_own_read_passes_the_check_even_through_the_fold_kernel(_floor_monkeypatch):
    _floor_monkeypatch.setattr(floor, "KNOWN_OVER_BUDGET_SITES", {})
    handle = _unit(UNIT, 2 * read_budget.K)
    read_budget.mark_gateway_process()
    assert len(list(handle.iter_from(1))) == 2 * read_budget.K
    projection_kernel.advance(projection_kernel.initial("status"), handle.iter_from(1))
    assert {f.split(":")[0] for _, f in read_budget._warned} == {
        "test_crew_log_read_budget_ci_check.py"
    }
    floor.check_read_budget()


def test_the_check_is_on_exactly_when_the_switch_is(request):
    assert ("_crew_log_read_budget_check" in request.fixturenames) is floor._READ_BUDGET_RAISE


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("test_x.py", True),
        ("conftest_floor_tmp.py", True),
        ("apps/builtins/issue_radar/tests/test_x.py", True),
        ("crew_log_core_helpers.py", True),
        ("cli.py", False),
        ("heapq.py", False),
        ("crew_log/read.py", False),
        ("apps/builtins/issue_radar/backend/crew_store_log.py", False),
    ],
)
def test_a_site_path_is_a_test_read_only_when_it_names_test_code(path, is_test):
    assert floor._is_test_read(path) is is_test


def test_every_listed_site_names_a_live_function_and_a_reason():
    package = Path(read_budget.__file__).resolve().parents[1]
    for site, reason in floor.KNOWN_OVER_BUDGET_SITES.items():
        path, _, func = site.partition(" ")
        assert reason and not floor._is_test_read(path), site
        source = (package / path).read_text(encoding="utf-8")
        # The one listed generator expression is the one inside ``iter_events``.
        owner = "iter_events" if func == "<genexpr>" else func
        assert f"def {owner}(" in source, site
