"""Pins scripts/check_merge_queue_ruleset.py against real rules/branches/main shapes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_merge_queue_ruleset.py"
_spec = importlib.util.spec_from_file_location("check_merge_queue_ruleset", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# The shape `gh api repos/kirodotdev/KiroCrew/rules/branches/main` returns while
# the queue is not required: every rule main carries except `merge_queue`.
_LIVE_WITHOUT_QUEUE = [
    {"type": "repository_visibility", "ruleset_id": 5369255},
    {"type": "deletion", "ruleset_id": 20088190},
    {"type": "non_fast_forward", "ruleset_id": 20088190},
    {"type": "required_linear_history", "ruleset_id": 20088190},
    {
        "type": "pull_request",
        "ruleset_id": 20088190,
        "parameters": {"required_approving_review_count": 1},
    },
    {
        "type": "required_status_checks",
        "ruleset_id": 20088190,
        "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [{"context": "PR Readiness"}],
        },
    },
]

_MERGE_QUEUE = {
    "type": "merge_queue",
    "ruleset_id": 20088190,
    "parameters": {
        "max_entries_to_build": 40,
        "max_entries_to_merge": 1,
        "check_response_timeout_minutes": 180,
    },
}


def test_the_live_rules_without_a_queue_fail() -> None:
    ok, message = gate.verdict(_LIVE_WITHOUT_QUEUE)
    assert not ok
    assert "no merge_queue rule" in message


def test_strict_up_to_date_is_not_a_substitute_for_the_queue() -> None:
    rules = [dict(rule) for rule in _LIVE_WITHOUT_QUEUE]
    rules[-1] = {
        **rules[-1],
        "parameters": {**rules[-1]["parameters"], "strict_required_status_checks_policy": True},
    }
    assert gate.verdict(rules)[0] is False


def test_a_merge_queue_rule_passes() -> None:
    ok, message = gate.verdict([*_LIVE_WITHOUT_QUEUE, _MERGE_QUEUE])
    assert ok
    assert "20088190" in message


def test_a_queue_without_a_required_check_fails() -> None:
    rules = [rule for rule in _LIVE_WITHOUT_QUEUE if rule["type"] != "required_status_checks"]
    ok, message = gate.verdict([*rules, _MERGE_QUEUE])
    assert not ok
    assert "no required_status_checks" in message


def test_a_non_list_body_fails_closed() -> None:
    assert gate.verdict({"message": "Not Found"})[0] is False


def test_the_cli_exit_codes(tmp_path: Path) -> None:
    off = tmp_path / "off.json"
    off.write_text(json.dumps(_LIVE_WITHOUT_QUEUE), encoding="utf-8")
    on = tmp_path / "on.json"
    on.write_text(json.dumps([*_LIVE_WITHOUT_QUEUE, _MERGE_QUEUE]), encoding="utf-8")
    assert gate.main(["x", str(off)]) == 1
    assert gate.main(["x", str(on)]) == 0
    assert gate.main(["x", str(tmp_path / "missing.json")]) == 2


def test_the_variable_on_without_the_queue_names_the_skipped_push_run() -> None:
    ok, message = gate.verdict(_LIVE_WITHOUT_QUEUE, "true")
    assert not ok
    assert "MERGE_QUEUE_ENABLED is 'true'" in message
    assert "MERGE_QUEUE_ENABLED" not in gate.verdict(_LIVE_WITHOUT_QUEUE, "")[1]


def test_the_cli_reads_the_queue_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    off = tmp_path / "off.json"
    off.write_text(json.dumps(_LIVE_WITHOUT_QUEUE), encoding="utf-8")
    monkeypatch.setenv("MERGE_QUEUE_ENABLED", "true")
    assert gate.main(["x", str(off)]) == 1
    assert "MERGE_QUEUE_ENABLED is 'true'" in capsys.readouterr().out


_WORKFLOW = _SCRIPT.parent.parent / ".github" / "workflows" / "merge-queue-ruleset.yml"
_WATCH = _SCRIPT.parent.parent / ".github" / "workflows" / "scheduled-failure-watch.yml"


def test_the_workflow_runs_on_a_schedule_and_never_on_a_push_or_pr() -> None:
    triggers = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_the_workflow_runs_the_script_with_the_queue_variable() -> None:
    job = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["check"]
    assert job["if"] == "${{ github.repository == 'kirodotdev/KiroCrew' }}"
    step = job["steps"][-1]
    assert step["env"]["MERGE_QUEUE_ENABLED"] == "${{ vars.MERGE_QUEUE_ENABLED }}"
    assert "rules/branches/main" in step["run"]
    assert "scripts/check_merge_queue_ruleset.py" in step["run"]


def test_a_red_scheduled_run_reaches_the_failure_watch() -> None:
    name = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["name"]
    watched = yaml.safe_load(_WATCH.read_text(encoding="utf-8"))[True]["workflow_run"]["workflows"]
    assert name in watched
