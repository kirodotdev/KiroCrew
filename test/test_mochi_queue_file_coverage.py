"""``mochi/queue_file.py``: timestamp parsing, read validation and update paths other suites skip.

Pure functions over parsed JSON. ``now_ms`` is pinned in every test, and offsets are whole seconds
so the float round trip through ``_epoch_ms`` stays exact.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf

NOW = 1_800_000_000_000  # epoch milliseconds, on a whole second


def _task(task_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": task_id, **extra}


def _ids(tasks: list[dict[str, Any]]) -> list[str]:
    return [t["id"] for t in tasks]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-01T00:00:00Z", 1_767_225_600_000),
        ("2026-01-01T00:00:00z", 1_767_225_600_000),
        ("2026-01-01T01:00:00+01:00", 1_767_225_600_000),
        ("2026-01-01T00:00:00", 1_767_225_600_000),  # a bare date-time is read as UTC
        ("   ", None),
        ("not a date", None),
        (12345, None),
    ],
)
def test_epoch_ms_reads_iso_text_and_everything_else_as_unparseable(
    value: Any, expected: Any
) -> None:
    assert qf._epoch_ms(value) == expected


def test_read_queue_returns_a_valid_queue_unchanged(tmp_path: Path) -> None:
    queue = {"tasks": [_task("a")], "planned_at": "x", "planned_until": "y"}
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(queue), encoding="utf-8")
    assert qf.read_queue(path) == queue


@pytest.mark.parametrize(
    ("text", "warning"),
    [
        ("{not json", "Failed to read"),
        ('["a list"]', "missing tasks array"),
        ('{"tasks": [], "planned_at": 1, "planned_until": "y"}', "missing timestamps"),
        ('{"tasks": [], "planned_at": "x"}', "missing timestamps"),
    ],
)
def test_read_queue_rejects_a_bad_file_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str, warning: str
) -> None:
    path = tmp_path / "queue.json"
    path.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert qf.read_queue(path) is None
    assert warning in caplog.text


def test_cleanup_drops_stale_and_unparseable_done_tasks_and_keeps_the_rest() -> None:
    queue = {
        "mood": "calm",
        "tasks": [
            _task("open"),
            _task("untimed", done=True),
            _task("stale", done=True, executed_at=qf._iso(NOW - qf.DEFAULT_TTL_MS - 1000)),
            _task("fresh", done=True, executed_at=qf._iso(NOW - 1000)),
            _task("garbled", done=True, executed_at="some time yesterday"),
        ],
    }
    out = qf.cleanup_done_tasks(queue, now_ms=NOW)
    assert _ids(out["tasks"]) == ["open", "untimed", "fresh"]
    assert out["mood"] == "calm"
    assert len(queue["tasks"]) == 5  # the input is not mutated


def test_get_executable_tasks_returns_due_open_tasks_only() -> None:
    queue = {
        "tasks": [
            _task("due", execute_after=qf._iso(NOW - 1000)),
            _task("exactly-now", execute_after=qf._iso(NOW)),
            _task("later", execute_after=qf._iso(NOW + 1000)),
            _task("done", done=True, execute_after=qf._iso(NOW - 1000)),
            _task("garbled", execute_after="soon"),
            _task("missing"),
        ]
    }
    assert _ids(qf.get_executable_tasks(queue, NOW)) == ["due", "exactly-now"]


@pytest.mark.parametrize(
    ("queue", "route"),
    [
        (None, "plan"),
        ({"needs_replan": True, "planned_until": qf._iso(NOW + 3_600_000)}, "replan"),
        ({"planned_until": qf._iso(NOW + qf.PLAN_AHEAD_MS - 60_000)}, "plan"),
        ({"planned_until": qf._iso(NOW + qf.PLAN_AHEAD_MS + 60_000)}, "execute"),
        ({"planned_until": "garbled"}, "execute"),  # NaN < threshold is false
    ],
)
def test_route_poll_chooses_plan_replan_or_execute(queue: Any, route: str) -> None:
    assert qf.route_poll(queue, NOW) == route


def test_full_replace_keeps_the_first_two_agent_tasks_and_warns() -> None:
    tasks = [
        _task("a1", requires_agent=True),
        _task("n1"),
        _task("a2", requires_agent=True),
        _task("a3", requires_agent=True),
        _task("n2", requires_agent="yes"),  # truthy but not True: not counted against the budget
    ]
    result = qf.apply_update({"tasks": []}, {"full_replace": {"tasks": tasks}}, now_ms=NOW)
    assert _ids(result["queue"]["tasks"]) == ["a1", "n1", "a2", "n2"]
    assert result["warning"] == (
        "Agent task budget exceeded: 3 agent tasks found, truncated to 2. Excess tasks were removed."
    )


@pytest.mark.parametrize("until", ["garbled", qf._iso(NOW + 60_000)])
def test_full_replace_moves_a_planned_until_that_is_unparseable_or_under_five_minutes(
    until: str,
) -> None:
    result = qf.apply_update(
        {"tasks": []}, {"full_replace": {"tasks": [], "planned_until": until}}, now_ms=NOW
    )
    corrected = qf._iso(NOW + 3_600_000)
    assert result["queue"]["planned_until"] == corrected
    assert (
        result["warning"]
        == f"planned_until was in the past or too short ({until}), corrected to {corrected}."
    )


def test_full_replace_carries_over_open_reminders_the_new_plan_lacks() -> None:
    old = {
        "tasks": [
            _task("r1", category="reminder"),
            _task("r2", category="reminder", done=True),
            _task("r3", category="reminder"),
            _task("tip", category="tip"),
        ]
    }
    new = {"tasks": [_task("r3", category="reminder"), _task("n")]}
    result = qf.apply_update(old, {"full_replace": new}, now_ms=NOW)
    assert _ids(result["queue"]["tasks"]) == ["r3", "n", "r1"]
    assert "warning" not in result


def test_incremental_update_adds_removes_then_modifies_and_skips_unknown_ids() -> None:
    queue = {"mood": "calm", "tasks": [_task("a", title="A"), _task("b", title="B")]}
    params = {
        "tasks_to_add": [_task("c", title="C")],
        "tasks_to_remove": ["a"],
        "tasks_to_modify": [{"id": "b", "title": "B2"}, {"id": "ghost"}, {"title": "no id"}],
        "mood": "excited",
        "narrative": None,
        "needs_replan": True,
    }
    updated = qf.apply_update(queue, params, now_ms=NOW)["queue"]
    assert updated["tasks"] == [{"id": "b", "title": "B2"}, {"id": "c", "title": "C"}]
    assert (updated["mood"], updated["needs_replan"]) == ("excited", True)
    assert "narrative" not in updated  # None leaves the key alone
    assert queue["tasks"][0] == {"id": "a", "title": "A"}  # the input queue is not mutated
