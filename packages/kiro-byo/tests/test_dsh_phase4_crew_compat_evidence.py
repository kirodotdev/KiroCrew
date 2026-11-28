"""KiroCrew 0.11 Mate-role visibility without manufacturing Task identity.

The 0.11.0 upstream service-agent IDs are source-defined in mate_worker.py.
A projected agent is evidence of the selected *profile*, not a ticket, grant,
or completed task. The legacy Crew ACP client need not send any extension.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_byo.crew_role_evidence import projected_crew_role
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.work_trace import work_trace_snapshot


@pytest.mark.parametrize("agent,role", [
    ("kirocrew-mate-worker", "worker"),
    ("kirocrew-mate-conductor", "conductor"),
    ("kirocrew-conductor", ""),
    ("mate worker", ""),
    ("kirocrew-mate-worker-other", ""),
    ("WORKER", ""),
    ("", ""),
    (None, ""),
])
def test_only_source_defined_mate_service_agents_are_classified(agent, role):
    assert projected_crew_role(agent) == role


def test_unmodified_crew_session_is_visible_but_never_assigned_fake_task(tmp_path):
    ledger = DurableRunStore(tmp_path / "runs")
    original = ledger.create_run("acp-session-a", metadata={
        "crew_projected_agent_id": "kirocrew-mate-worker",
        "model_id": "some-provider-model",
        "prompt": "Fix task-123 urgently",  # must never reverse-engineer task ID
        "password": "NEVER_LEAK",
    })
    out = work_trace_snapshot(store=ledger)
    assert out["tasks"] == []
    assert out["summary"]["linkedRunsInWindow"] == 0
    assert out["summary"]["unlinkedCrewRunsInWindow"] == 1
    assert len(out["unlinkedCrewRuns"]) == 1
    row = out["unlinkedCrewRuns"][0]
    assert row["sessionId"] == "acp-session-a"
    assert row["projectedRole"] == "worker"
    assert row["taskCorrelation"] == "not_provided_by_host"
    assert not any("taskId" in value or "assignmentId" in value for value in out["unlinkedCrewRuns"])
    assert "NEVER_LEAK" not in json.dumps(out)
    assert "Fix task-123" not in json.dumps(out)
    assert ledger.get(original["run_id"]) == original


def test_linked_worker_is_not_double_counted_as_unlinked(tmp_path):
    ledger = DurableRunStore(tmp_path / "runs")
    a = ledger.create_run("acp-session", metadata={
        "model_id": "a", "crew_projected_agent_id": "kirocrew-mate-worker",
        "host_work_link": {"taskId": "task-real", "assignmentId": "assignment-real", "role": "worker"},
    })
    ledger.create_run("acp-session", metadata={
        "model_id": "b", "crew_projected_agent_id": "kirocrew-mate-worker",
    })
    summary = work_trace_snapshot(store=ledger)
    assert summary["summary"]["linkedTasksInWindow"] == 1
    assert summary["summary"]["unlinkedCrewRunsInWindow"] == 1
    assert summary["tasks"][0]["runs"][0]["runId"] == a["run_id"]
    only = work_trace_snapshot(task_id="task-real", store=ledger)
    assert only["unlinkedCrewRuns"] == []
    assert only["summary"]["unlinkedCrewRunsInWindow"] == 0


def test_unknown_or_forged_metadata_does_not_get_crew_role(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    store.create_run("s1", metadata={"crew_projected_agent_id": "not-crew", "role": "worker"})
    store.create_run("s2", metadata={"role": "worker", "taskId": "task-guessed"})
    snapshot = work_trace_snapshot(store=store)
    assert snapshot["unlinkedCrewRuns"] == []
    assert snapshot["tasks"] == []


def test_bounded_unlinked_view_without_fake_task(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    for i in range(5):
        store.create_run(f"worker-{i}", metadata={"crew_projected_agent_id": "kirocrew-mate-conductor"})
    view = work_trace_snapshot(limit=2, store=store)
    assert len(view["unlinkedCrewRuns"]) == 2
    assert view["summary"]["unlinkedCrewRunsInWindow"] == 5
    assert view["summary"]["unlinkedHasMore"] is True
    assert all(x["projectedRole"] == "conductor" for x in view["unlinkedCrewRuns"])


def test_source_writes_projected_identity_but_not_authority_and_all_locales_present():
    root = Path(__file__).resolve().parents[1]
    # R29 moved durable persistence behind the TurnRunnerMixin seam.
    turn = (root / "src/kiro_byo/durable_run_lifecycle.py").read_text(encoding="utf-8")
    assert '"crew_projected_agent_id": projected_crew_agent_id(' in turn
    assert 'projected_crew_agent_id(' in turn
    assert '"host_work_link": dict(' in turn
    app = (root / "ui/index.mjs").read_text(encoding="utf-8")
    assert "work?.unlinkedCrewRuns" in app
    for lang in ("zh-CN", "zh-TW", "en", "ja", "ko"):
        messages = json.loads((root / "src/kiro_byo/locales" / (lang + ".json")).read_text(encoding="utf-8"))
        for name in ("work.unlinked_heading", "work.unlinked_note", "work.unlinked_empty", "work.profile_worker", "work.profile_conductor"):
            assert messages[name]


def test_actual_durable_run_uses_current_crew_agent_not_host_role_claim(tmp_path):
    from types import SimpleNamespace
    from kiro_byo.turn_runner import TurnRunnerMixin

    class Runner(TurnRunnerMixin):
        runtime_trace = None
        def trace(self, _event):
            pass

    runner = Runner()
    runner._kiro_durable_run_store = DurableRunStore(tmp_path / "runs")
    session = SimpleNamespace(
        session_id="crew-worker-session", events=[], agent=SimpleNamespace(agent_id="kirocrew-mate-worker"),
        host_work_link={}, active_agent_plan_id="", active_run_id="", active_run_step_id="", active_run_attempt_id="",
    )
    run_id = runner._begin_durable_run(session, None, selection="fixed", provider_id="p", model_id="m")
    assert run_id
    persisted = runner._kiro_durable_run_store.get(run_id)
    assert persisted["metadata"]["crew_projected_agent_id"] == "kirocrew-mate-worker"
    assert persisted["metadata"]["host_work_link"] == {}
    visible = work_trace_snapshot(store=runner._kiro_durable_run_store)
    assert visible["tasks"] == []
    assert visible["unlinkedCrewRuns"][0]["projectedRole"] == "worker"
    assert DurableRunStore.public_record(persisted)["crewProjectedRole"] == "worker"

    # The next run on an unrelated saved agent must not inherit the worker tag.
    session.agent = SimpleNamespace(agent_id="general-coding-agent")
    other = runner._begin_durable_run(session, None, selection="fixed", provider_id="p", model_id="m")
    record = runner._kiro_durable_run_store.get(other)
    assert record["metadata"]["crew_projected_agent_id"] == ""
    assert "crewProjectedRole" not in DurableRunStore.public_record(record)


def test_crew12_source_preflight_never_claims_host_task_bridge_or_desktop_e2e(tmp_path):
    import subprocess
    import sys
    import json
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/verify_crew12_integration.py"),
         "--crew-package", str(tmp_path / "no-upstream-crew-checkout")],
        capture_output=True, text=True, cwd=root,
    )
    assert result.returncode == 2
    facts = json.loads(result.stdout)
    assert facts["source_contract_status"] == "refused"
    assert facts["windows_e2e"] == "not_verified"
    assert facts["verified_by_upstream_source_archive"] is False
    assert facts["issues"]
