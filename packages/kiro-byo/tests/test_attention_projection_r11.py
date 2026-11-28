"""R11: DSH-inspired single attention shortlist, no second task owner."""
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.work_trace import work_trace_snapshot


def test_attention_is_bounded_deduplicated_and_read_only(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    for idx in range(7):
        store.create_run(f"s-{idx}", metadata={
            "host_work_link": {"taskId": f"task-{idx}", "role": "worker"}})
        store.create_run(f"second-{idx}", metadata={
            "host_work_link": {"taskId": f"task-{idx}", "role": "worker"}})
    output = work_trace_snapshot(store=store)
    attention = output["attention"]
    assert attention["totalInWindow"] == 7
    assert attention["hasMore"] is True
    assert len(attention["items"]) == 5
    assert len({i["taskId"] for i in attention["items"]}) == 5
    assert attention["taskCompletionAuthoritative"] is False
    assert all(set(i) == {"kind", "taskId", "executionSignal", "updatedAt", "runCount"} for i in attention["items"])


def test_unlinked_attention_deduplicates_sessions_never_invents_task_id(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    for _ in range(3):
        store.create_run("crew-worker-s1", metadata={"crew_projected_agent_id":"kirocrew-mate-worker"})
    result = work_trace_snapshot(store=store)
    assert result["tasks"] == []
    assert result["attention"]["totalInWindow"] == 1
    item = result["attention"]["items"][0]
    assert item["kind"] == "unlinked_crew_session"
    assert item["sessionId"] == "crew-worker-s1"
    assert "taskId" not in item and "assignmentId" not in item


def test_attention_is_empty_when_only_completed_runs(tmp_path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("s", metadata={"host_work_link":{"taskId":"task-1","role":"worker"}})
    store.finish_run(run["run_id"], status="completed")
    result = work_trace_snapshot(store=store)
    assert result["attention"]["totalInWindow"] == 0
    assert result["attention"]["items"] == []
