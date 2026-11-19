"""A KAS workflow step's own conversation, read from KAS's session store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.kas_workflows import transcript, wire
from kiro_crew.kas_workflows.router import KasWorkflowRouter, StepNotStarted
from kiro_crew.testing.links import make_dir_link

SID = "sess_81c4db41-9212-49ba-a7d3-2f09908a77b5"
WID = "wf_4d59ddebfbc27732"


@pytest.fixture(autouse=True)
def _plain_redaction(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.platform.redact_via_context", lambda text: text.replace("SECRET", "[redacted]")
    )


def _session(root: Path, records: list[dict], *, workflow_id: str = WID, sid: str = SID) -> Path:
    directory = root / "b069624b1d504c50" / sid
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(
        json.dumps(
            {
                "id": sid,
                "title": "investigate-kiro-docs · investigate",
                "agentMode": "wf-planner",
                "modelId": "claude-opus-5.5",
                "status": "idle",
                "createdAt": "2026-10-16T16:49:22.434Z",
                "lastModifiedAt": "2026-10-16T16:58:52.366Z",
                "_meta": {
                    "kiro": {"workflow": {"workflowId": workflow_id, "nodeId": "investigate"}}
                },
            }
        )
    )
    with (directory / "messages.jsonl").open("w") as fh:
        for n, payload in enumerate(records):
            fh.write(json.dumps({"id": f"r{n}", "timestamp": f"t{n}", "payload": payload}) + "\n")
    return directory


# The record shapes below are copied from a real KAS 2.21.1 step session.
RECORDS = [
    {"type": "user", "content": "You are running a READ-ONLY investigation."},
    {"type": "turn_start", "executionId": "e"},
    {"type": "steering_inclusion", "documents": ["file:///x.md"]},
    {"type": "assistant", "content": "I'll start by ", "operationType": "Reasoning"},
    {"type": "assistant", "content": "cloning the repo.", "operationType": "Reasoning"},
    {"type": "pending_interaction", "interactionType": "tool_approval", "toolCallId": "c1"},
    {
        "type": "tool_call",
        "toolCallId": "c1",
        "toolName": "execute_bash",
        "args": {"command": "gh repo clone x SECRET"},
        "status": "running",
        "kind": "execute",
    },
    {"type": "interaction_resolved", "toolCallId": "c1", "outcome": "selected"},
    {
        "type": "tool_call",
        "toolCallId": "c1",
        "toolName": "execute_bash",
        "args": {"command": "gh repo clone x"},
        "status": "completed",
        "kind": "execute",
    },
    {"type": "tool_result", "toolCallId": "c1", "content": "Cloning into 'x'..."},
    {
        "type": "tool_call",
        "toolCallId": "c2",
        "toolName": "read_file",
        "title": "Read File",
        "args": {"path": "/r/lib/routes.ts", "offset": 0},
        "status": "approved",
        "kind": "read",
    },
    {"type": "tool_result", "toolCallId": "c2", "content": "export const routes = []"},
    {"type": "assistant", "content": "Found the routes.", "operationType": "Say"},
    {"type": "session_metadata", "key": "contextUsage", "value": {}},
    {
        "type": "usage_summary",
        "promptTurnSummaries": [
            {"unit": "credit", "usage": 27.177},
            {"unit": "credit", "usage": 1.5},
        ],
    },
    {"type": "turn_end", "stopReason": "end_turn"},
    "not a dict",
]


def test_a_real_step_log_becomes_dashboard_entries(tmp_path):
    _session(tmp_path, RECORDS)
    out = transcript.read_step_transcript(WID, "investigate", SID, root=tmp_path)
    kinds = [(e["kind"], e.get("reasoning")) for e in out["entries"]]
    assert kinds == [
        ("user", None),
        ("assistant", True),
        ("tool", None),
        ("tool", None),
        ("assistant", False),
    ]
    user, thought, clone, read, say = out["entries"]
    assert thought["text"] == "I'll start by cloning the repo.", "adjacent chunks of one kind merge"
    assert clone["status"] == "completed" and clone["output"] == "Cloning into 'x'..."
    assert clone["input"] == "gh repo clone x [redacted]", "text is redacted before it leaves"
    assert clone["toolKind"] == "execute" and clone["name"] == "execute_bash"
    assert read["title"] == "Read File" and read["input"] == "/r/lib/routes.ts"
    assert read["status"] == "completed", "an approved read with a result reads as done"
    assert out["credits"] == pytest.approx(28.677)
    assert out["status"] == "idle" and out["agent"] == "wf-planner" and out["truncated"] is False


def test_a_step_mid_turn_reads_running_and_its_waiting_tool(tmp_path):
    _session(
        tmp_path,
        [
            {"type": "user", "content": [{"type": "text", "text": "go"}]},
            {"type": "turn_start"},
            {
                "type": "tool_call",
                "toolCallId": "c1",
                "toolName": "execute_bash",
                "args": {"command": "ls"},
                "status": "running",
            },
            {"type": "pending_interaction", "toolCallId": "c1"},
        ],
    )
    out = transcript.read_step_transcript(WID, "investigate", SID, root=tmp_path)
    assert out["status"] == "running"
    assert out["entries"][0]["text"] == "go"
    assert out["entries"][1]["status"] == "waiting_approval"
    assert out["credits"] is None


def test_entries_are_capped_newest_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "MAX_ENTRIES", 3)
    _session(tmp_path, [{"type": "user", "content": f"m{n}"} for n in range(5)])
    out = transcript.read_step_transcript(WID, "n", SID, root=tmp_path)
    assert [e["text"] for e in out["entries"]] == ["m2", "m3", "m4"] and out["truncated"]


@pytest.mark.parametrize(
    "sid", ["../etc", "sess_x", "", SID + "/..", "sess_81C4DB41-9212-49ba-a7d3-2f09908a77b5"]
)
def test_only_a_kas_session_id_is_ever_located(tmp_path, sid):
    with pytest.raises(transcript.StepTranscriptError):
        transcript.locate(sid, root=tmp_path)


def test_a_session_of_another_run_is_refused(tmp_path):
    _session(tmp_path, RECORDS, workflow_id="wf_0000000000000000")
    with pytest.raises(transcript.StepTranscriptError):
        transcript.read_step_transcript(WID, "investigate", SID, root=tmp_path)


def test_a_symlinked_session_outside_the_root_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    _session(outside, RECORDS)
    root = tmp_path / "root"
    (root / "bucket").mkdir(parents=True)
    make_dir_link(root / "bucket" / SID, outside / "b069624b1d504c50" / SID)
    with pytest.raises(transcript.StepTranscriptError):
        transcript.locate(SID, root=root)


# ── Through the router ──


class _Runtime:
    acp_backend = "kas"

    def __init__(self, answer=None):
        self.answer = answer or {}
        self.calls: list[str] = []

    def is_alive(self):
        return True

    async def extension_request(self, method, params, *, timeout=30.0):
        self.calls.append(method)
        return self.answer


def _router(runtime):
    async def factory():
        return runtime

    return KasWorkflowRouter(factory, screen=lambda _c: None)


@pytest.mark.asyncio
async def test_router_serves_a_known_steps_chat_by_its_mirrored_session(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "_sessions_root", lambda: tmp_path)
    _session(tmp_path, RECORDS)
    rt = _Runtime()
    router = _router(rt)
    sink = router.sink_for(rt)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": WID, "workflowName": "x"})
    sink.on_notification(
        wire.NOTIFY_NODE_START, {"workflowId": WID, "nodeId": "investigate", "sessionId": SID}
    )
    out = await router.step_transcript(WID, "investigate")
    assert out["sessionId"] == SID and len(out["entries"]) == 5


@pytest.mark.asyncio
async def test_router_serves_one_loop_iteration_by_its_path(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "_sessions_root", lambda: tmp_path)
    _session(tmp_path, RECORDS)
    rt = _Runtime()
    router = _router(rt)
    sink = router.sink_for(rt)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": WID, "workflowName": "x"})
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {
            "workflowId": WID,
            "nodeId": "investigate",
            "nodePath": [WID, "loop", "iter-0", "investigate"],
            "sessionId": SID,
        },
    )
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {
            "workflowId": WID,
            "nodeId": "investigate",
            "nodePath": [WID, "loop", "iter-1", "investigate"],
            "sessionId": "later-iteration",
        },
    )
    # The first iteration is still reachable by its path, though the bare id
    # now names the second.
    out = await router.step_transcript(WID, "investigate", "loop/iter-0/investigate")
    assert out["sessionId"] == SID
    with pytest.raises(StepNotStarted):
        await router.step_transcript(WID, "investigate", "loop/iter-5/investigate")


@pytest.mark.asyncio
async def test_router_refuses_unknown_runs_and_unstarted_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "_sessions_root", lambda: tmp_path)
    rt = _Runtime()
    router = _router(rt)
    with pytest.raises(Exception, match="not known"):
        await router.step_transcript(WID, "investigate")
    router.sink_for(rt).on_notification(
        wire.NOTIFY_RUN_START, {"workflowId": WID, "workflowName": "x"}
    )
    with pytest.raises(StepNotStarted):
        await router.step_transcript(WID, "investigate")
    assert wire.METHOD_INSPECT in rt.calls, "an unmirrored step session is looked up read-only"
    assert wire.METHOD_LOAD not in rt.calls


# ── A chat's runs after a gateway restart ──


@pytest.mark.asyncio
async def test_a_chats_runs_come_back_from_the_ledger_read_only(tmp_path):
    ledger = tmp_path / "runs.json"
    ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "runs": {
                    WID: {"slot": "chat-6", "cwd": "", "createdAt": 2.0, "ended": True},
                    "wf_1111111111111111": {"slot": "chat-7", "cwd": "", "createdAt": 1.0},
                },
            }
        )
    )
    rt = _Runtime(
        {
            "workflowId": WID,
            "state": {"status": "completed", "workflowName": "investigate"},
            "nodePlan": [{"id": "investigate", "type": "step", "label": "Investigate"}],
        }
    )

    async def factory():
        return rt

    router = KasWorkflowRouter(factory, screen=lambda _c: None, ledger_path=ledger)
    rows = await router.chat_runs("chat-6")
    assert [r["workflowId"] for r in rows] == [WID]
    assert rows[0]["slotKey"] == "chat-6" and rows[0]["status"] == "completed"
    assert rt.calls == [wire.METHOD_INSPECT], "inspect only; never load"
    # Read once: a second open of the panel costs no call.
    await router.chat_runs("chat-6")
    assert rt.calls == [wire.METHOD_INSPECT]
    assert await router.chat_runs("chat-unknown") == []


def _straddling(cap: int) -> str:
    """A secret that starts just before *cap*: a cut-then-redact shows its prefix."""
    return "x" * (cap - 3) + "SECRET"


@pytest.mark.parametrize(
    "field,record_or_meta,where",
    [
        ("toolName", "record", lambda out: out["entries"][0]["name"]),
        ("toolName-as-title", "record", lambda out: out["entries"][0]["title"]),
        ("kind", "record", lambda out: out["entries"][0]["toolKind"]),
        ("status", "record", lambda out: out["entries"][0]["status"]),
        ("toolCallId", "record", lambda out: out["entries"][0]["toolCallId"]),
        ("agentMode", "meta", lambda out: out["agent"]),
        ("modelId", "meta", lambda out: out["model"]),
        ("title", "meta", lambda out: out["title"]),
    ],
)
def test_every_shown_string_is_neutralized_redacted_then_capped(
    tmp_path, field, record_or_meta, where
):
    value = _straddling(transcript._MAX_LINE) + "\u202e\n"
    call = {"type": "tool_call", "toolCallId": "c1", "toolName": "t", "status": "running"}
    if record_or_meta == "record":
        key = "toolName" if field == "toolName-as-title" else field
        call[key] = value
    directory = _session(tmp_path, [call])
    if record_or_meta == "meta":
        meta = json.loads((directory / "session.json").read_text())
        meta[field] = value
        (directory / "session.json").write_text(json.dumps(meta))
    shown = where(transcript.read_step_transcript(WID, "investigate", SID, root=tmp_path))
    assert "SEC" not in shown and "\u202e" not in shown and "\n" not in shown, field


def test_a_steps_text_keeps_its_lines_but_loses_bidi_overrides(tmp_path):
    _session(tmp_path, [{"type": "user", "content": "line one\nline\u202e two"}])
    [user] = transcript.read_step_transcript(WID, "investigate", SID, root=tmp_path)["entries"]
    assert user["text"] == "line one\nline  two"


def test_a_secret_split_across_streamed_chunks_is_redacted_as_one(tmp_path, monkeypatch):
    import re as _re

    # A bearer-like pattern: once the first half is replaced, the tag ends the
    # match, so a second pass over the merged text cannot reach the second half.
    monkeypatch.setattr(
        "kiro_crew.platform.redact_via_context",
        lambda text: _re.sub(r"key=[A-Za-z0-9]+", "key=[redacted]", text),
    )
    _session(
        tmp_path,
        [
            {"type": "assistant", "content": "the key=SEC"},
            {"type": "assistant", "content": "RET2, done"},
        ],
    )
    [say] = transcript.read_step_transcript(WID, "investigate", SID, root=tmp_path)["entries"]
    assert say["text"] == "the key=[redacted], done"
