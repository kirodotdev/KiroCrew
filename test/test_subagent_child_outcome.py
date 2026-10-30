"""A sub-agent's ending is one ``ChildOutcome``, made final by one ``_finish``.

``ChildOutcome`` names five endings and folds each onto the three-way ``outcome``
every reader already uses, so the fold is pinned exactly: a reader of ``outcome``
must see what it saw before the finer vocabulary existed. ``_finish`` is the one
place the terminal report settles, records and flips, in that order, and the
``subagent_done`` frame built after it carries the finer ending.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_manager.terminal import TerminalCoordinator
from kiro_crew.subagent_parts.child_outcome import ChildOutcome

# (record fields, fine ending, coarse outcome the record had before ChildOutcome)
_SHAPES = [
    ({}, ChildOutcome.COMPLETED, "completed"),
    ({"partial": True}, ChildOutcome.PARTIAL, "completed"),
    ({"user_stopped": True}, ChildOutcome.INTERRUPTED_BY_PARENT, "stopped"),
    (
        {"user_stopped": True, "partial": True, "error": "kill not confirmed"},
        ChildOutcome.INTERRUPTED_BY_PARENT,
        "stopped",
    ),
    ({"error": "boom"}, ChildOutcome.FAILED, "failed"),
    ({"error": "boom", "partial": True}, ChildOutcome.FAILED, "failed"),
    (
        {"error": "Reaped after 10s", "reaped": True, "_reaper_wrote_error": True},
        ChildOutcome.REAPED,
        "failed",
    ),
    # The run recorded its own error first; a reap tearing it down later sets
    # ``reaped`` but did not write the error.
    ({"error": "boom", "reaped": True}, ChildOutcome.FAILED, "failed"),
]


@pytest.mark.parametrize(("fields", "fine", "coarse"), _SHAPES)
def test_each_record_shape_has_one_ending_and_keeps_its_three_way_outcome(fields, fine, coarse):
    info = SubagentInfo(id="c0ffee", task="t", **fields)
    assert info.child_outcome is fine
    assert info.outcome == coarse
    assert fine.coarse == coarse


def test_every_ending_folds_onto_the_panel_vocabulary():
    from kiro_crew.subagent_persistence import _PANEL_OUTCOMES

    assert {o.coarse for o in ChildOutcome} == set(_PANEL_OUTCOMES)
    assert [o.value for o in ChildOutcome] == [
        "completed",
        "partial",
        "interrupted_by_parent",
        "reaped",
        "failed",
    ]


def test_a_reaped_run_without_an_error_is_not_called_reaped():
    """A neutral stop's reap sets ``reaped`` and writes no error: not the reaper's ending."""
    info = SubagentInfo(id="c0ffee", task="t", reaped=True)
    assert info.child_outcome is ChildOutcome.COMPLETED


def test_the_reap_ending_names_the_reaper_only_when_it_writes_the_error(monkeypatch):
    """``_record_reap_ending`` marks the error as the reaper's only when it wrote it."""
    from kiro_crew import subagent
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    _record_reap_ending_impl = RunEventCoordinator._record_reap_ending_impl
    monkeypatch.setattr(
        subagent, "Stats", lambda: SimpleNamespace(inc_subagent_failed=lambda: None)
    )

    manager = SimpleNamespace(
        arm_report_in_flight=lambda _info: None,
        _write_tombstone=lambda *_a, **_k: None,
    )
    events = SimpleNamespace(_manager=manager)

    reaped = SubagentInfo(id="c0ffee", task="t", reaped=True)
    reaped._reap_reason = "reaped"
    _record_reap_ending_impl(events, reaped, "the runtime was torn down")
    assert reaped.child_outcome is ChildOutcome.REAPED

    own = SubagentInfo(id="c0ffef", task="t", reaped=True, error="the run's own error")
    own._reap_reason = "reaped"
    _record_reap_ending_impl(events, own, "the runtime was torn down")
    assert own.child_outcome is ChildOutcome.FAILED


def _coordinator(calls: list[str]) -> SimpleNamespace:
    coordinator = SimpleNamespace(
        _record_crew_log_terminal=lambda info: calls.append(f"record done={info.done}"),
        _record_terminal_metric=lambda info: calls.append(f"metric done={info.done}"),
        _manager=SimpleNamespace(
            arm_report_in_flight=lambda info: calls.append(f"arm done={info.done}"),
            consume_report_hold=lambda _batch, _id: None,
        ),
    )
    coordinator._finish = lambda info: TerminalCoordinator._finish(coordinator, info)
    return coordinator


def test_finish_settles_then_records_then_flips():
    calls: list[str] = []
    info = SubagentInfo(id="c0ffee", task="t")
    info._credit_accounting = SimpleNamespace(settle=lambda: calls.append("settle"))

    TerminalCoordinator._finish(_coordinator(calls), info)

    assert calls == ["settle", "record done=False", "metric done=False", "arm done=True"]
    assert info.done is True


def test_the_report_finishes_once_before_it_broadcasts():
    calls: list[str] = []
    info = SubagentInfo(id="c0ffee", task="t", partial=True)
    coordinator = _coordinator(calls)

    async def _guarded(_info, **_kw):
        calls.append(f"broadcast {_info.child_outcome.value} done={_info.done}")
        return True

    coordinator._report_terminal_guarded_impl = _guarded

    assert asyncio.run(
        TerminalCoordinator._report_terminal_impl(
            coordinator,
            info,
            source="test",
            injection_timeout_reason="",
            mark_delivered_on_success=False,
        )
    )
    assert calls == [
        "record done=False",
        "metric done=False",
        "arm done=True",
        "broadcast partial done=True",
    ]


def test_the_done_frame_carries_the_fine_ending_beside_the_coarse_one():
    events: list[tuple[str, dict]] = []

    async def _fire_event(etype, _info, data):
        events.append((etype, data))

    coordinator = SimpleNamespace(
        _manager=SimpleNamespace(_fire_event=_fire_event, _on_done=None),
    )
    info = SubagentInfo(
        id="c0ffee", task="t", error="Reaped after 10s", reaped=True, _reaper_wrote_error=True
    )

    assert asyncio.run(
        TerminalCoordinator._report_terminal_guarded_impl(
            coordinator,
            info,
            source="test",
            injection_timeout_reason="",
            mark_delivered_on_success=False,
        )
    )
    ((etype, data),) = events
    assert etype == "subagent_done"
    assert data["outcome"] == "failed"
    assert data["child_outcome"] == "reaped"
