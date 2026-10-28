"""KAS workflow run mirror: folding lists, loads and notifications into rows."""

from __future__ import annotations

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.mirror import (
    MAX_PATHS_PER_RUN,
    MAX_REASON_CHARS,
    MAX_TERMINAL_RUNS,
    RunMirror,
)

_TREE = [
    {"type": "step", "id": "plan", "agent": "wf-planner"},
    {
        "type": "repeat",
        "id": "loop",
        "maxIterations": 3,
        "children": [
            {"type": "step", "id": "code", "agent": "wf-coder"},
            {"type": "step", "id": "review", "agent": "wf-code-reviewer"},
        ],
    },
]


def _clock() -> float:
    return 1_800_000_000.0


def _started(mirror: RunMirror, workflow_id: str = "wf_1") -> None:
    mirror.apply_notification(
        wire.NOTIFY_RUN_START,
        {
            "workflowId": workflow_id,
            "workflowName": "review-loop",
            "inputs": {},
            "nodeTree": _TREE,
            "parentSessionId": "chat-1",
        },
    )


def test_a_run_streams_from_start_to_completion() -> None:
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    row = mirror.row("wf_1")
    assert row is not None
    assert row["status"] == "running"
    assert row["parentSessionId"] == "chat-1"
    assert (row["completedSteps"], row["totalSteps"]) == (0, 3)
    assert row["controls"]["primary"] == "pause"

    mirror.apply_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "wf_1", "nodeId": "plan", "type": "step", "sessionId": "step-s1"},
    )
    row = mirror.row("wf_1")
    assert row is not None
    assert row["activeSteps"] == [
        {"nodeId": "plan", "label": "wf-planner", "sessionId": "step-s1", "path": "plan"}
    ]
    assert mirror.run_for_step_session("step-s1") == ("wf_1", "plan")

    mirror.apply_notification(
        wire.NOTIFY_NODE_COMPLETE, {"workflowId": "wf_1", "nodeId": "plan", "status": "completed"}
    )
    row = mirror.row("wf_1")
    assert row is not None and row["completedSteps"] == 1 and row["activeSteps"] == []

    mirror.apply_notification(
        wire.NOTIFY_RUN_COMPLETE, {"workflowId": "wf_1", "status": "completed", "finalState": {}}
    )
    row = mirror.row("wf_1")
    assert row is not None
    assert row["status"] == "completed"
    assert row["endedAt"] is not None
    # Progress is hidden on a terminal run.
    assert row["completedSteps"] is None and row["totalSteps"] is None
    assert row["controls"]["primary"] is None


def test_a_parked_step_is_paused_not_terminal() -> None:
    """``run_complete`` with ``paused`` means a step parked for input."""
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    mirror.apply_notification(
        wire.NOTIFY_RUN_COMPLETE, {"workflowId": "wf_1", "status": "paused", "finalState": {}}
    )
    row = mirror.row("wf_1")
    assert row is not None
    assert row["status"] == "paused" and row["endedAt"] is None
    assert row["controls"]["primary"] == "resume"


def test_attention_turns_the_primary_into_open_and_names_the_step() -> None:
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    mirror.apply_notification(
        wire.NOTIFY_NODE_START,
        {
            "workflowId": "wf_1",
            "nodeId": "code",
            "sessionId": "step-s2",
            "attention": [{"kind": "permission", "nodeId": "code", "sessionId": "step-s2"}],
        },
    )
    row = mirror.row("wf_1")
    assert row is not None
    # A tool approval on a RUNNING run: it still reads running and keeps Pause
    # and it needs the person.
    assert row["controls"]["primary"] == "pause"
    assert row["controls"]["display"] == "running"
    assert row["controls"]["needsAttention"] is True
    assert row["controls"]["attentionKind"] == "approval"
    assert row["blocking"]["label"] == "wf-coder"
    assert row["blocking"]["sessionId"] == "step-s2"
    # A later notification without the key leaves the wait in place; one that
    # carries an empty list clears it.
    mirror.apply_notification(
        wire.NOTIFY_LOOP_ITERATION, {"workflowId": "wf_1", "loopId": "loop", "iteration": 1}
    )
    assert mirror.row("wf_1")["blocking"] is not None  # type: ignore[index]
    mirror.apply_notification(
        wire.NOTIFY_NODE_COMPLETE,
        {"workflowId": "wf_1", "nodeId": "code", "status": "completed", "attention": []},
    )
    assert mirror.row("wf_1")["blocking"] is None  # type: ignore[index]


def test_a_notification_before_the_list_is_held_and_then_named() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_notification(wire.NOTIFY_NODE_START, {"workflowId": "wf_9", "nodeId": "x"})
    assert "wf_9" in mirror
    changed = mirror.apply_summaries(
        wire.parse_run_list(
            {
                "runs": [
                    {
                        "workflowId": "wf_9",
                        "status": "running",
                        "name": "Nightly",
                        "createdAt": "2026-10-14T10:00:00Z",
                    }
                ]
            }
        )
    )
    assert changed == ["wf_9"]
    row = mirror.row("wf_9")
    assert row is not None and row["name"] == "Nightly"


def test_an_unchanged_input_reports_no_change() -> None:
    mirror = RunMirror(clock=_clock)
    summaries = wire.parse_run_list({"runs": [{"workflowId": "wf_1", "status": "paused"}]})
    assert mirror.apply_summaries(summaries) == ["wf_1"]
    assert mirror.apply_summaries(summaries) == []
    assert mirror.apply_notification("_kiro/workflow/unknown", {"workflowId": "wf_1"}) is None
    assert mirror.apply_notification(wire.NOTIFY_PAUSED, {"no": "id"}) is None


def test_load_fills_node_status_and_step_sessions() -> None:
    mirror = RunMirror(clock=_clock)
    changed = mirror.apply_load(
        {
            "workflowId": "wf_2",
            "state": {
                "status": "paused",
                "runLabel": "Fix flaky test",
                "parentSessionId": "launcher-1",
                "pauseReason": "awaiting approval",
                "root": {
                    "id": "root",
                    "type": "sequence",
                    "status": "paused",
                    "children": [
                        {"id": "plan", "type": "step", "status": "completed"},
                        {
                            "id": "loop",
                            "type": "repeat",
                            "status": "paused",
                            "children": [
                                {"id": "code", "type": "step", "status": "paused"},
                            ],
                        },
                    ],
                },
            },
            "nodePlan": _TREE,
            "stepSessions": [{"nodeId": "code", "nodePath": "loop/code", "sessionId": "step-s7"}],
        }
    )
    assert changed == "wf_2"
    row = mirror.row("wf_2")
    assert row is not None
    assert row["name"] == "Fix flaky test"
    assert row["pauseReason"] == "awaiting approval"
    assert row["completedSteps"] == 1
    loop = row["nodes"][1]
    assert loop["children"][0]["status"] == "paused"
    assert loop["children"][0]["sessionId"] == "step-s7"


def _iteration(n: int, code: dict, review: dict) -> dict:
    return {
        "nodeId": f"loop#{n}",
        "type": "sequence",
        "iteration": n,
        "status": "completed" if review.get("status") == "completed" else "running",
        "children": [
            {"nodeId": "code", "type": "step", **code},
            {"nodeId": "review", "type": "step", **review},
        ],
    }


def test_a_loops_iterations_keep_each_instance_by_its_path() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_load(
        {
            "workflowId": "wf_3",
            "state": {
                "status": "running",
                "root": {
                    "nodeId": "wf_3",
                    "type": "sequence",
                    "status": "running",
                    "children": [
                        {
                            "nodeId": "plan",
                            "type": "step",
                            "status": "completed",
                            "sessionId": "s-plan",
                        },
                        {
                            "nodeId": "loop",
                            "type": "repeat",
                            "status": "running",
                            "children": [
                                _iteration(
                                    0,
                                    {"status": "completed", "sessionId": "s-code-0"},
                                    {"status": "completed", "sessionId": "s-review-0"},
                                ),
                                _iteration(
                                    1,
                                    {"status": "running", "sessionId": "s-code-1"},
                                    {"status": "pending"},
                                ),
                            ],
                        },
                    ],
                },
            },
            "nodePlan": _TREE,
        }
    )
    loop = mirror.row("wf_3")["nodes"][1]
    assert [i["iteration"] for i in loop["iterations"]] == [0, 1]
    first, second = loop["iterations"]
    assert first["path"] == "loop/iter-0" and first["status"] == "completed"
    assert [c["sessionId"] for c in first["children"]] == ["s-code-0", "s-review-0"]
    assert [c["path"] for c in first["children"]] == ["loop/iter-0/code", "loop/iter-0/review"]
    assert second["status"] == "running"
    assert [c["status"] for c in second["children"]] == ["running", "pending"]
    assert second["children"][1]["sessionId"] is None
    # The bare id still names the latest instance; the path names each one.
    assert mirror.step_session("wf_3", "code") == "s-code-1"
    assert mirror.step_session_at("wf_3", "loop/iter-0/code") == "s-code-0"
    assert mirror.path_of_step_session("wf_3", "s-review-0") == "loop/iter-0/review"
    assert mirror.row("wf_3")["nodes"][0]["path"] == "plan"

    # A live node_start for the next iteration lands under its own path,
    # with the workflowId head of its nodePath dropped.
    mirror.apply_notification(
        wire.NOTIFY_NODE_START,
        {
            "workflowId": "wf_3",
            "nodeId": "code",
            "nodePath": ["wf_3", "loop", "iter-2", "code"],
            "sessionId": "s-code-2",
            "iteration": 2,
        },
    )
    loop = mirror.row("wf_3")["nodes"][1]
    assert [i["iteration"] for i in loop["iterations"]] == [0, 1, 2]
    assert loop["iterations"][2]["children"][0]["sessionId"] == "s-code-2"
    assert mirror.step_session_at("wf_3", "loop/iter-1/code") == "s-code-1"
    # A run outside any loop has no iterations to list.
    assert "iterations" not in mirror.row("wf_3")["nodes"][0]


def test_replace_remaining_keeps_started_nodes_and_swaps_the_tail() -> None:
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    mirror.apply_notification(wire.NOTIFY_NODE_START, {"workflowId": "wf_1", "nodeId": "plan"})
    mirror.apply_notification(
        wire.NOTIFY_STEPS_QUEUED,
        {
            "workflowId": "wf_1",
            "pendingSteps": [{"type": "step", "id": "ship", "agent": "wf-pr-submitter"}],
        },
    )
    row = mirror.row("wf_1")
    assert row is not None and [n["id"] for n in row["queuedSteps"]] == ["ship"]
    mirror.apply_notification(
        wire.NOTIFY_STEPS_QUEUED,
        {
            "workflowId": "wf_1",
            "pendingSteps": [{"type": "step", "id": "ship", "agent": "wf-pr-submitter"}],
            "resolution": {"outcome": "applied"},
        },
    )
    row = mirror.row("wf_1")
    assert row is not None
    assert [n["id"] for n in row["nodes"]] == ["plan", "ship"]
    assert row["queuedSteps"] == []


def test_rows_put_waiting_runs_first_and_filter_by_chat() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_summaries(
        wire.parse_run_list(
            {
                "runs": [
                    {
                        "workflowId": "old",
                        "status": "completed",
                        "endedAt": "2026-10-01T00:00:00Z",
                        "parentSessionId": "a",
                    },
                    {
                        "workflowId": "new",
                        "status": "completed",
                        "endedAt": "2026-10-02T00:00:00Z",
                        "parentSessionId": "b",
                    },
                    {
                        "workflowId": "live",
                        "status": "running",
                        "createdAt": "2026-10-03T00:00:00Z",
                        "parentSessionId": "a",
                    },
                    {
                        "workflowId": "ask",
                        "status": "running",
                        "createdAt": "2026-09-01T00:00:00Z",
                        "parentSessionId": "a",
                        "attention": [{"kind": "question", "nodeId": "n"}],
                    },
                ]
            }
        )
    )
    assert [r["workflowId"] for r in mirror.rows()] == ["ask", "live", "new", "old"]
    assert [r["workflowId"] for r in mirror.rows(parent_session_id="a")] == ["ask", "live", "old"]


def test_terminal_runs_are_capped_and_live_runs_are_never_evicted() -> None:
    mirror = RunMirror(clock=_clock)
    runs = [
        {
            "workflowId": f"t{i:04d}",
            "status": "completed",
            "endedAt": f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}Z",
        }
        for i in range(MAX_TERMINAL_RUNS + 5)
    ]
    runs.append({"workflowId": "live", "status": "running"})
    mirror.apply_summaries(wire.parse_run_list({"runs": runs}))
    assert "live" in mirror
    assert len(mirror) == MAX_TERMINAL_RUNS + 1
    assert "t0000" not in mirror and f"t{MAX_TERMINAL_RUNS + 4:04d}" in mirror


def test_forget_drops_a_deleted_run() -> None:
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    assert mirror.forget("wf_1") is True
    assert mirror.row("wf_1") is None
    assert mirror.forget("wf_1") is False


def test_a_loaded_runs_start_and_end_come_from_its_root_node():
    """KAS's WorkflowState has no run-level times; the root node's are the run's."""
    mirror = RunMirror(clock=_clock)
    mirror.apply_load(
        {
            "workflowId": "w9",
            "state": {
                "status": "completed",
                "workflowName": "investigate",
                "createdAt": "2026-10-16T16:49:22.236Z",
                "root": {
                    "nodeId": "w9",
                    "type": "sequence",
                    "status": "completed",
                    "startedAt": "2026-10-16T16:49:22.301Z",
                    "endedAt": "2026-10-16T16:58:52.366Z",
                    "steps": [
                        {
                            "nodeId": "investigate",
                            "type": "step",
                            "status": "completed",
                            "modelId": "claude-opus-5",
                            "effortLevel": "high",
                        }
                    ],
                },
            },
            "nodePlan": [{"nodeId": "investigate", "type": "step", "agentName": "wf-planner"}],
        }
    )
    row = mirror.row("w9")
    assert row is not None
    assert row["startedAt"] == "2026-10-16T16:49:22.301Z"
    assert row["endedAt"] == "2026-10-16T16:58:52.366Z"
    assert [n["id"] for n in row["nodes"]] == ["investigate"]
    assert (row["nodes"][0]["model"], row["nodes"][0]["effort"]) == ("claude-opus-5", "high")


# ── Kiro Agent's own step counts (``WorkflowRunProgress``) ──

_UNKNOWN = {
    "stepsCompleted": None,
    "stepsTotal": None,
    "currentNodeId": None,
    "waitingOnWatch": None,
}


def _progress(row: dict | None) -> dict:
    assert row is not None
    return {key: row[key] for key in wire.RUN_PROGRESS_KEYS}


def test_kas_step_counts_absent_stay_absent() -> None:
    # An older bundled kiro-cli sends none of the four fields: the row says
    # unknown (null) rather than inventing a count, and our own count stays.
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    mirror.apply_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "wf_1", "nodeId": "plan", "type": "step", "sessionId": "s1"},
    )
    mirror.apply_summaries(
        wire.parse_run_list({"runs": [{"workflowId": "wf_1", "status": "running", "name": "r"}]})
    )
    row = mirror.row("wf_1")
    assert _progress(row) == _UNKNOWN
    assert row is not None and (row["completedSteps"], row["totalSteps"]) == (0, 3)


def test_kas_step_counts_from_every_lifecycle_notification() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_notification(
        wire.NOTIFY_RUN_START,
        {
            "workflowId": "wf_1",
            "workflowName": "review-loop",
            "inputs": {},
            "nodeTree": _TREE,
            "stepsCompleted": 0,
            "stepsTotal": 3,
            "currentNodeId": "plan",
            "waitingOnWatch": False,
        },
    )
    assert _progress(mirror.row("wf_1")) == {
        "stepsCompleted": 0,
        "stepsTotal": 3,
        "currentNodeId": "plan",
        "waitingOnWatch": False,
    }
    # A later event is a newer snapshot: KAS counts loop passes it has
    # materialized, so the total grows, and the current node moves on.
    changed = mirror.apply_notification(
        wire.NOTIFY_LOOP_ITERATION,
        {
            "workflowId": "wf_1",
            "loopId": "loop",
            "iteration": 2,
            "stepsCompleted": 3,
            "stepsTotal": 5,
            "currentNodeId": "code",
            "waitingOnWatch": False,
        },
    )
    assert changed == "wf_1"
    assert _progress(mirror.row("wf_1"))["stepsTotal"] == 5
    # An event with none of the fields (a replayed one) leaves the last snapshot.
    mirror.apply_notification(
        wire.NOTIFY_NODE_START, {"workflowId": "wf_1", "nodeId": "code", "type": "step"}
    )
    assert _progress(mirror.row("wf_1"))["stepsCompleted"] == 3
    # A snapshot without currentNodeId means no node qualifies right now.
    mirror.apply_notification(
        wire.NOTIFY_WATCH_POLL,
        {
            "workflowId": "wf_1",
            "nodeId": "ci",
            "stepsCompleted": 4,
            "stepsTotal": 5,
            "waitingOnWatch": True,
        },
    )
    assert _progress(mirror.row("wf_1")) == {
        "stepsCompleted": 4,
        "stepsTotal": 5,
        "currentNodeId": None,
        "waitingOnWatch": True,
    }


def test_kas_step_counts_from_a_list_row() -> None:
    mirror = RunMirror(clock=_clock)
    mirror.apply_summaries(
        wire.parse_run_list(
            {
                "runs": [
                    {
                        "workflowId": "wf_1",
                        "status": "paused",
                        "name": "r",
                        "stepsCompleted": 2,
                        "stepsTotal": 5,
                        "currentNodeId": "watch-ci",
                        "waitingOnWatch": True,
                    }
                ]
            }
        )
    )
    assert _progress(mirror.row("wf_1")) == {
        "stepsCompleted": 2,
        "stepsTotal": 5,
        "currentNodeId": "watch-ci",
        "waitingOnWatch": True,
    }


def test_kas_step_counts_clear_when_the_run_goes_terminal() -> None:
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    mirror.apply_notification(
        wire.NOTIFY_NODE_COMPLETE,
        {
            "workflowId": "wf_1",
            "nodeId": "plan",
            "status": "completed",
            "stepsCompleted": 1,
            "stepsTotal": 3,
            "currentNodeId": "code",
            "waitingOnWatch": False,
        },
    )
    assert _progress(mirror.row("wf_1"))["stepsTotal"] == 3
    # KAS's terminal run_complete carries no counts; a finished run has none.
    mirror.apply_notification(
        wire.NOTIFY_RUN_COMPLETE, {"workflowId": "wf_1", "status": "completed"}
    )
    assert _progress(mirror.row("wf_1")) == _UNKNOWN
    # Nor does a terminal list row bring stale ones back.
    mirror.apply_summaries(
        wire.parse_run_list(
            {
                "runs": [
                    {
                        "workflowId": "wf_1",
                        "status": "completed",
                        "name": "r",
                        "stepsCompleted": 3,
                        "stepsTotal": 3,
                    }
                ]
            }
        )
    )
    assert _progress(mirror.row("wf_1")) == _UNKNOWN


def test_kas_step_counts_ignore_malformed_values() -> None:
    progress = wire.parse_run_progress(
        {"stepsCompleted": -1, "stepsTotal": True, "currentNodeId": 7, "waitingOnWatch": "yes"}
    )
    assert progress is not None
    assert progress.to_payload() == {
        "stepsCompleted": None,
        "stepsTotal": None,
        "currentNodeId": None,
        "waitingOnWatch": False,
    }
    assert wire.parse_run_progress({"workflowId": "wf_1"}) is None


def test_every_kept_kas_text_is_cut_to_the_reason_cap() -> None:
    """A failure, park or pause reason and a run's names are stored at most MAX_REASON_CHARS long."""
    long = "x" * (MAX_REASON_CHARS * 5)
    mirror = RunMirror(clock=_clock)
    mirror.apply_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "wf_1", "workflowName": long, "nodeTree": _TREE},
    )
    mirror.apply_notification(
        wire.NOTIFY_NODE_COMPLETE,
        {
            "workflowId": "wf_1",
            "nodeId": "plan",
            "nodePath": ["wf_1", "plan"],
            "status": "failed",
            "failureReason": long,
        },
    )
    mirror.apply_notification(
        wire.NOTIFY_NODE_PAUSED,
        {"workflowId": "wf_1", "nodeId": "code", "nodePath": ["wf_1", "code"], "reason": long},
    )
    mirror.apply_notification(wire.NOTIFY_PAUSED, {"workflowId": "wf_1", "pauseReason": long})
    row = mirror.row("wf_1")
    assert row is not None
    assert row["workflowName"] == "x" * MAX_REASON_CHARS
    assert row["pauseReason"] == "x" * MAX_REASON_CHARS
    run = mirror._runs["wf_1"]
    assert run.node_reason == {"plan": "x" * MAX_REASON_CHARS, "code": "x" * MAX_REASON_CHARS}
    assert {k: v["reason"] for k, v in run.path_state.items()} == run.node_reason

    mirror.apply_load(
        {
            "workflowId": "wf_2",
            "state": {
                "status": "failed",
                "runLabel": long,
                "pauseReason": long,
                "root": {
                    "id": "root",
                    "children": [{"id": "plan", "status": "failed", "failureReason": long}],
                },
            },
        }
    )
    loaded = mirror._runs["wf_2"]
    assert loaded.name == "x" * MAX_REASON_CHARS
    assert loaded.pause_reason == "x" * MAX_REASON_CHARS
    assert loaded.path_state["plan"]["reason"] == "x" * MAX_REASON_CHARS


def test_per_id_maps_stop_taking_new_node_ids_at_the_cap() -> None:
    """Past MAX_PATHS_PER_RUN distinct ids a new id is not recorded; a held id still updates."""
    mirror = RunMirror(clock=_clock)
    _started(mirror)
    for i in range(MAX_PATHS_PER_RUN + 50):
        mirror.apply_notification(
            wire.NOTIFY_NODE_COMPLETE,
            {"workflowId": "wf_1", "nodeId": f"n{i}", "status": "failed", "failureReason": "r"},
        )
    run = mirror._runs["wf_1"]
    assert len(run.node_status) == MAX_PATHS_PER_RUN
    assert len(run.node_reason) == MAX_PATHS_PER_RUN
    assert f"n{MAX_PATHS_PER_RUN}" not in run.node_status
    mirror.apply_notification(
        wire.NOTIFY_NODE_START, {"workflowId": "wf_1", "nodeId": "n0", "sessionId": "s0"}
    )
    assert run.node_status["n0"] == wire.STATUS_RUNNING and run.node_session["n0"] == "s0"
