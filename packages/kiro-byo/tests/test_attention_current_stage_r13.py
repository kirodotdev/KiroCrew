"""R13: obsolete ordinary errors stop consuming human attention.

Still never turn the BYO evidence projection into an authoritative Crew task
state, and never hide unresolved external side effects.
"""
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.work_trace import work_trace_snapshot


def _new_run(store, session, task=None):
    meta = ({"host_work_link": {"taskId": task, "role": "worker"}}
            if task else {"crew_projected_agent_id": "kirocrew-mate-worker"})
    return store.create_run(session, metadata=meta)


def test_later_safe_success_does_not_leave_historical_failure_attention(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    old = _new_run(store, "crew-session", "task-1")
    store.finish_run(old["run_id"], status="failed")
    retry = _new_run(store, "crew-session", "task-1")
    store.finish_run(retry["run_id"], status="completed")
    output = work_trace_snapshot(store=store)
    assert output["attention"]["totalInWindow"] == 0
    assert output["attention"]["selectionPolicy"] == "latest_run_except_unresolved_side_effects"
    task = output["tasks"][0]
    assert task["executionSignal"] == "completed_run"
    assert [row["status"] for row in task["runs"]] == ["completed", "failed"]
    assert task["requiresReview"] is False
    assert output["summary"]["taskCompletionAuthoritative"] is False


def test_active_retry_suppresses_old_nonmutating_failure(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    first = _new_run(store, "crew-session", "task-1")
    store.finish_run(first["run_id"], status="failed")
    new = store.create_run("crew-session", metadata={"host_work_link": {"taskId": "task-1", "role": "worker"}}, lease_owner="owner", lease_ttl=120)
    output = work_trace_snapshot(store=store)
    assert output["tasks"][0]["executionSignal"] == "working"
    assert output["attention"]["items"] == []


def test_unresolved_side_effect_remains_attention_after_newer_success(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    unsafe = _new_run(store, "crew-session", "task-1")
    store.checkpoint(unsafe["run_id"], name="tool_started", data={"side_effect_risk": True})
    store.finish_run(unsafe["run_id"], status="failed")
    latest = _new_run(store, "crew-session", "task-1")
    store.finish_run(latest["run_id"], status="completed")
    output = work_trace_snapshot(store=store)
    assert output["attention"]["items"][0]["executionSignal"] == "unknown_side_effect"
    assert output["tasks"][0]["executionSignal"] == "unknown_side_effect"


def test_unlinked_session_suppresses_old_failure_without_fabricating_task(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    old = _new_run(store, "crew-session")
    store.finish_run(old["run_id"], status="failed")
    new = _new_run(store, "crew-session")
    store.finish_run(new["run_id"], status="completed")
    output = work_trace_snapshot(store=store)
    assert output["attention"]["items"] == []
    assert len(output["unlinkedCrewRuns"]) == 2
    assert output["tasks"] == []


def test_unlinked_unsafe_history_remains_visible_after_success(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    unsafe = _new_run(store, "crew-session")
    store.checkpoint(unsafe["run_id"], name="tool_started", data={"side_effect_risk": True})
    store.finish_run(unsafe["run_id"], status="failed")
    safe = _new_run(store, "crew-session")
    store.finish_run(safe["run_id"], status="completed")
    attention = work_trace_snapshot(store=store)["attention"]
    assert attention["totalInWindow"] == 1
    assert attention["items"][0]["kind"] == "unlinked_crew_session"
    assert attention["items"][0]["executionSignal"] == "unknown_side_effect"
