"""Crew's KAS workflow parsing, driven by a LIVE Kiro Agent run.

``test/fixtures/acp_frames/kas/workflow-run-live.jsonl`` is a real KAS session
(``scripts/capture_kas_workflow.py``): a two-step workspace recipe listed, created,
invoked, a step note to the parent, a step that parked for input and was answered
on its own session, the run's end, then load / inspect / list. It is the capture
``ACP_BACKENDS_NATIVE_WORKFLOWS`` cites. Every assertion here reads the frames KAS
actually sent through the same parsers the router uses, so a KAS wire change that
breaks Crew fails here rather than in a person's run view.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp import kas_wire
from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.mirror import RunMirror

CAPTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "kas" / "workflow-run-live.jsonl"


def _clock() -> float:
    return 1_800_000_000.0


def _frames() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines()]
    return lines[0]["_meta"], lines[1:]


def _results(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f["result"] for f in frames if "method" not in f and isinstance(f.get("result"), dict)]


def _result_with(frames: list[dict[str, Any]], *keys: str) -> dict[str, Any]:
    found = [r for r in _results(frames) if all(k in r for k in keys)]
    assert found, keys
    return found[0]


def _notifications(frames: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    return [f["params"] for f in frames if f.get("method") == method and "id" not in f]


def _node(row: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in row["nodes"]:
        if node["id"] == node_id:
            return node
    raise AssertionError(node_id)


@pytest.fixture(scope="module")
def capture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _frames()


def test_the_capture_is_a_live_kas_engine_recording(capture) -> None:
    meta, frames = capture
    assert (meta["backend"], meta["recorded"]) == ("kas", "live")
    assert meta["agent_version"] and meta["kiro_cli_version"]
    init = frames[0]["result"]
    # KAS's own handshake: no agentInfo, the extension list the router gates on.
    assert "agentInfo" not in init
    served = wire.extension_methods(init["agentCapabilities"])
    assert served is not None
    # Every verb the router sends to launch, read and list a run is advertised.
    for method in (
        wire.METHOD_LIST_RECIPES,
        wire.METHOD_NEW,
        wire.METHOD_INVOKE,
        wire.METHOD_LOAD,
        wire.METHOD_INSPECT,
        wire.METHOD_LIST,
    ):
        assert wire.method_served(method, served), method


def test_session_new_echoes_the_workflows_setting(capture) -> None:
    _meta, frames = capture
    answer = _result_with(frames, "sessionId", "_meta")
    # attach_workflow_settings sent _meta.kiro.settings.workflows.enabled: true.
    assert answer["_meta"]["workflowsEnabled"] is True


def test_list_recipes_reads_the_workspace_recipe_and_the_bundled_ones(capture) -> None:
    _meta, frames = capture
    recipes = wire.parse_recipes(_result_with(frames, "recipes"))
    by_name = {r.name: r for r in recipes}
    ours = by_name["kirocrew-capture"]
    assert ours.launchable and not ours.read_only
    assert ours.scope("/work") == wire.RECIPE_SCOPE_WORKSPACE
    assert [n.id for n in ours.plan] == ["note", "ask"]
    bundled = [r for r in recipes if r.source and r.source.startswith(wire.BUNDLED_SOURCE_PREFIX)]
    assert bundled and all(r.read_only for r in bundled)


def test_every_workflow_notification_kas_sent_is_one_the_sink_claims(capture) -> None:
    _meta, frames = capture
    sent = {
        f["method"]
        for f in frames
        if isinstance(f.get("method"), str) and f["method"].startswith("_kiro/workflow/")
    }
    assert sent and sent <= wire.NOTIFICATION_METHODS
    assert {
        wire.NOTIFY_RUN_START,
        wire.NOTIFY_NODE_START,
        wire.NOTIFY_NODE_COMPLETE,
        wire.NOTIFY_NODE_PAUSED,
        wire.NOTIFY_PAUSED,
        wire.NOTIFY_RUN_COMPLETE,
    } <= sent


def _fold(mirror: RunMirror, frames: list[dict[str, Any]], until: int | None = None) -> str:
    """Fold the frames in the order the router meets them; returns the run id."""
    created = _result_with(frames, "workflowId", "initialState")
    workflow_id = created["workflowId"]
    mirror.apply_load({"workflowId": workflow_id, "state": created["initialState"]})
    for frame in frames[:until]:
        method = frame.get("method")
        if method in wire.RUN_NOTIFICATIONS:
            mirror.apply_notification(method, frame["params"])
        elif "method" not in frame and isinstance(frame.get("result"), dict):
            result = frame["result"]
            if "state" in result and "workflowId" in result:
                mirror.apply_load(result)
    return workflow_id


def test_the_parked_step_reads_as_awaiting_a_reply(capture) -> None:
    _meta, frames = capture
    first_end = next(
        i
        for i, f in enumerate(frames)
        if f.get("method") == wire.NOTIFY_RUN_COMPLETE and f["params"]["status"] == "paused"
    )
    mirror = RunMirror(clock=_clock)
    workflow_id = _fold(mirror, frames, first_end + 1)
    row = mirror.row(workflow_id)
    assert row is not None and row["status"] == wire.STATUS_PAUSED
    assert _node(row, "note")["status"] == wire.STATUS_COMPLETED
    assert _node(row, "ask")["status"] == wire.STATUS_PAUSED
    ask_session = mirror.step_session(workflow_id, "ask")
    assert ask_session is not None
    assert mirror.run_for_step_session(ask_session) == (workflow_id, "ask")
    # The reply is a session/prompt on the step's own session (NODE_ACTION_REPLY).
    assert mirror.step_awaits_reply(workflow_id, ask_session)


def test_a_resumed_run_keeps_the_steps_it_already_finished(capture) -> None:
    """KAS re-sends run_start (with a status-free nodeTree) when a parked step resumes.

    Folding it as a restart from scratch would show the finished first step as
    pending again for the rest of the run; only a run that had ended starts over.
    """
    _meta, frames = capture
    starts = [i for i, f in enumerate(frames) if f.get("method") == wire.NOTIFY_RUN_START]
    assert len(starts) == 2, "the capture holds the resume's run_start"
    assert "status" not in json.dumps(frames[starts[1]]["params"]["nodeTree"])
    mirror = RunMirror(clock=_clock)
    workflow_id = _fold(mirror, frames, starts[1] + 1)
    row = mirror.row(workflow_id)
    assert row is not None and row["status"] == wire.STATUS_RUNNING
    assert _node(row, "note")["status"] == wire.STATUS_COMPLETED


def test_the_whole_run_folds_to_a_completed_row(capture) -> None:
    _meta, frames = capture
    mirror = RunMirror(clock=_clock)
    workflow_id = _fold(mirror, frames)
    row = mirror.row(workflow_id)
    assert row is not None and row["status"] == wire.STATUS_COMPLETED
    assert [n["status"] for n in row["nodes"]] == [wire.STATUS_COMPLETED] * 2
    loaded = _result_with(frames, "workflowId", "state", "stepSessions")
    assert {s["nodeId"] for s in loaded["stepSessions"]} == {"note", "ask"}
    runs = wire.parse_run_list(_result_with(frames, "runs"))
    assert [(r.workflow_id, r.status) for r in runs] == [(workflow_id, wire.STATUS_COMPLETED)]


def test_a_restart_of_the_finished_run_still_starts_over(capture) -> None:
    """The same run_start on an ENDED run is a restart from scratch (KAS ``invoke``)."""
    _meta, frames = capture
    mirror = RunMirror(clock=_clock)
    workflow_id = _fold(mirror, frames)
    start = _notifications(frames, wire.NOTIFY_RUN_START)[0]
    mirror.apply_notification(wire.NOTIFY_RUN_START, start)
    row = mirror.row(workflow_id)
    assert row is not None and row["status"] == wire.STATUS_RUNNING
    assert [n["status"] for n in row["nodes"]] == [wire.NODE_STATUS_PENDING] * 2


def test_step_notes_reach_the_parent_with_their_severities(capture) -> None:
    _meta, frames = capture
    parent = _result_with(frames, "sessionId", "_meta")["sessionId"]
    notes = _notifications(frames, wire.NOTIFY_SESSION_NOTIFY)
    assert notes and all(n["sessionId"] == parent for n in notes)
    assert all(n.get("workflowId") and n.get("nodeId") for n in notes)
    severities = {n["severity"] for n in notes}
    # A step's note (info), its success signal, and the warning that parked it.
    assert {"info", "success", "warning"} <= severities
    assert "warning" in wire.WAKING_NOTE_SEVERITIES and "success" not in wire.WAKING_NOTE_SEVERITIES


def test_the_parent_turn_kas_starts_itself_has_both_boundaries(capture) -> None:
    _meta, frames = capture
    parent = _result_with(frames, "sessionId", "_meta")["sessionId"]
    kinds = [
        boundary[0]
        for f in frames
        if f.get("method") == "session/update" and f["params"].get("sessionId") == parent
        for boundary in [kas_wire.turn_boundary(f["params"]["update"])]
        if boundary is not None
    ]
    # A waking note opens a parent turn no client prompted; it ends on turn_end
    # (the capture stops once one such turn has ended, so a later one may be open).
    assert kinds[:2] == [kas_wire.KIND_TURN_START, kas_wire.KIND_TURN_END]
