"""Regression tests for the xdist INTERNALERROR terminal report (issue #2803).

When a second pytest-timeout worker kill lands in one ``--dist loadgroup``
shard, xdist's loadscope scheduler can die with an INTERNALERROR at exit 3
and no ``short test summary info`` section -- the whole shard's report is
erased. ``test/conftest.py`` preserves the report by recording each crashed
worker (``pytest_testnodedown``) and its in-flight test
(``pytest_handlecrashitem``) and replaying them from ``pytest_internalerror``.

The real crash needs two timeout kills in the same shard and is a flake, so
these tests drive the hooks directly with synthetic state instead of trying
to reproduce it. They fail on base with ImportError: the hooks did not exist.
"""

from __future__ import annotations

import pytest

from conftest import (
    _crash_victims,
    _crashed_workers,
    _format_abandoned_run_report,
    _reset_xdist_crash_state,
    pytest_handlecrashitem,
    pytest_internalerror,
    pytest_testnodedown,
)


class _FakeGateway:
    def __init__(self, gw_id: str) -> None:
        self.id = gw_id


class _FakeNode:
    def __init__(self, gw_id: str) -> None:
        self.gateway = _FakeGateway(gw_id)


@pytest.fixture(autouse=True)
def _clean_crash_state():
    """Isolate the module-level crash record from other tests in this worker."""
    _reset_xdist_crash_state()
    yield
    _reset_xdist_crash_state()


def test_internalerror_report_names_victims_and_replacement_count(capsys):
    """The abandoned-run report names every victim and the replacement count."""
    pytest_testnodedown(_FakeNode("gw1"), "worker gw1 crashed (pytest-timeout)")
    pytest_handlecrashitem("test/test_slow_a.py::test_hangs", report=None, sched=None)
    pytest_testnodedown(_FakeNode("gw2"), "worker gw2 crashed (pytest-timeout)")
    pytest_handlecrashitem("test/test_slow_b.py::test_also_hangs", report=None, sched=None)

    result = pytest_internalerror(excrepr="INTERNALERROR> KeyError: gw2", excinfo=None)

    # MUST NOT swallow the failure: returning True would suppress pytest's own
    # INTERNALERROR traceback. The hook only adds output.
    assert result is None
    err = capsys.readouterr().err
    assert "ABANDONED: INTERNALERROR after 2 crashed-worker replacements" in err
    assert "gw1" in err
    assert "gw2" in err
    assert "test/test_slow_a.py::test_hangs" in err
    assert "test/test_slow_b.py::test_also_hangs" in err


def test_internalerror_without_worker_crashes_is_silent(capsys):
    """A non-xdist internal error must be reported exactly as before."""
    result = pytest_internalerror(excrepr="INTERNALERROR> boom", excinfo=None)

    assert result is None
    # Substring check, not exact equality: unrelated stderr noise (GC-time
    # asyncio warnings, first-use library warnings) must not flake this test.
    assert "ABANDONED" not in capsys.readouterr().err


def test_traceback_error_is_summarized_by_its_last_line():
    """A multi-line remote traceback yields its exception line, not the
    constant 'Traceback (most recent call last):' header."""
    traceback_text = (
        "Traceback (most recent call last):\n"
        '  File "dsession.py", line 273, in worker_collectionfinish\n'
        "    self.sched.schedule()\n"
        "KeyError: <WorkerController gw5>\n"
    )
    report = _format_abandoned_run_report([("gw5", traceback_text)], [])

    assert "gw5: KeyError: <WorkerController gw5>" in report
    assert "gw5: Traceback" not in report


def test_clean_worker_shutdown_is_not_recorded():
    """``pytest_testnodedown`` with error=None (normal exit) records nothing."""
    pytest_testnodedown(_FakeNode("gw0"), None)

    assert _crashed_workers == []
    assert _crash_victims == []


def test_singular_replacement_count_and_missing_victim():
    """One crash reads 'replacement' (singular); no victim gets an explicit line."""
    report = _format_abandoned_run_report([("gw3", "worker gw3 crashed")], [])

    assert "after 1 crashed-worker replacement" in report
    assert "replacements" not in report
    assert "No in-flight test was recorded" in report


def test_node_without_gateway_id_is_still_recorded():
    """A node whose gateway id cannot be read still yields a report entry."""
    pytest_testnodedown(object(), "worker crashed hard")

    assert _crashed_workers == [("<unknown worker>", "worker crashed hard")]
