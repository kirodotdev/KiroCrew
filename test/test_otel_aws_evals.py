"""Contract tests for the optional AWS-evals chat/workflow exporters."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = ROOT / "scripts/otel/aws-evals"
HOOK_PATH = ADAPTER_ROOT / "hook-runner-v2/otel-crew-hook-v2.py"
INSTALLER_PATH = HOOK_PATH.with_name("install-v2.py")
CONVERTER_PATH = ADAPTER_ROOT / "acp-tap/acp-to-spans.py"
SHIM_BUILDER_PATH = ADAPTER_ROOT / "acp-tap/make-shim.sh"
KIRO_SHIM_BUILDER_PATH = ADAPTER_ROOT / "acp-tap/make-kiro-shim.sh"
TAP_SCRIPT_PATH = ADAPTER_ROOT / "acp-tap/acp-tap.js"


def load_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_OTEL_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("KIRO_OTEL_STATE_DIR", str(tmp_path / "state"))
    spec = importlib.util.spec_from_file_location("aws_eval_hook_under_test", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_converter(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_OTEL_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("KIRO_OTEL_STATE_DIR", str(tmp_path / "state"))
    return load_module(CONVERTER_PATH, "aws_eval_acp_converter_under_test")


def attribute(span: dict, name: str):
    return next(
        (
            next(iter(entry["value"].values()), None)
            for entry in span.get("attributes", [])
            if entry.get("key") == name
        ),
        None,
    )


def test_hook_uses_root_only_session_key(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    spans = hook.handle(
        {"hook_event_name": "UserPromptSubmit", "event_id": "prompt-1", "session_key": "chat-root"},
        10,
    )

    assert attribute(spans[0], "session.id") == "chat-root"


def test_hook_selects_agentcore_parent_and_keeps_both_session_keys(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    parent = "dashboard:agentcore-eval-root-1"

    spans = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-2",
            "session_key": "agent-slot-1",
            "parent_session_key": parent,
        },
        20,
    )

    assert attribute(spans[0], "session.id") == parent
    assert attribute(spans[0], "crew.session_key") == "agent-slot-1"
    assert attribute(spans[0], "crew.parent_session_key") == parent


def test_hook_ignores_non_root_parent_when_own_session_key_exists(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    spans = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-3",
            "session_key": "own-chat",
            "parent_session_key": "dashboard:ordinary-parent",
        },
        30,
    )

    assert attribute(spans[0], "session.id") == "own-chat"


def test_hook_falls_back_to_legacy_session_id(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    spans = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-4",
            "session_id": "legacy-chat",
            "parent_session_key": "dashboard:ordinary-parent",
        },
        40,
    )

    assert attribute(spans[0], "session.id") == "legacy-chat"


def test_hook_correlation_key_retains_own_and_parent_session_identity(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    base = {"tool_call_id": "call-1", "workflow_run_id": "run-1", "agent_id": "agent-1"}

    original = hook.correlation_key(
        {**base, "session_key": "own-a", "parent_session_key": "dashboard:agentcore-eval-a"}
    )
    other_own = hook.correlation_key(
        {**base, "session_key": "own-b", "parent_session_key": "dashboard:agentcore-eval-a"}
    )
    other_parent = hook.correlation_key(
        {**base, "session_key": "own-a", "parent_session_key": "dashboard:agentcore-eval-b"}
    )

    assert original != other_own
    assert original != other_parent


def test_hook_ignores_duplicate_event_delivery(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.handle(
        {"hook_event_name": "UserPromptSubmit", "event_id": "prompt", "session_key": "chat-1"},
        10,
    )
    pre = {
        "hook_event_name": "PreToolUse",
        "event_id": "pre-1",
        "tool_call_id": "call-1",
        "session_key": "chat-1",
        "tool_name": "ReadFile",
    }
    hook.handle(pre, 20)
    hook.handle(pre, 21)

    spans = hook.handle(
        {
            "hook_event_name": "PostToolUse",
            "event_id": "post-1",
            "tool_call_id": "call-1",
            "session_key": "chat-1",
            "tool_name": "ReadFile",
            "tool_response": {"output": "done"},
        },
        30,
    )

    assert len(spans) == 1
    assert attribute(spans[0], "crew.pre_events") == "1"


def test_hook_quarantines_tool_event_without_call_id(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)

    assert (
        hook.handle(
            {"hook_event_name": "PreToolUse", "event_id": "missing-call", "tool_name": "ReadFile"},
            50,
        )
        == []
    )
    errors = [
        json.loads(line)
        for line in (tmp_path / "state" / "correlation-errors.jsonl").read_text().splitlines()
    ]

    assert errors[0]["missing_ids"] == "tool_call_id"


def test_hook_does_not_infer_tool_pair_from_time_name_or_input(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.ORPHANS = "emit"
    common = {
        "session_key": "chat-1",
        "tool_name": "RunCommand",
        "tool_input": {"command": "same"},
        "timestamp": "same-time",
    }
    hook.handle(
        {**common, "hook_event_name": "PreToolUse", "event_id": "pre", "tool_call_id": "call-a"},
        60,
    )

    spans = hook.handle(
        {
            **common,
            "hook_event_name": "PostToolUse",
            "event_id": "post",
            "tool_call_id": "call-b",
            "tool_response": {"output": "not paired"},
        },
        60,
    )
    errors = [
        json.loads(line)
        for line in (tmp_path / "state" / "correlation-errors.jsonl").read_text().splitlines()
    ]

    assert spans == []
    assert errors[0]["missing_ids"] == "matching_pre_tool_event"


def test_hook_bounds_pending_tool_state(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_PENDING_CALLS = 1
    common = {"session_key": "chat-1", "tool_name": "ReadFile"}
    hook.handle(
        {**common, "hook_event_name": "PreToolUse", "event_id": "pre-a", "tool_call_id": "call-a"},
        70,
    )
    hook.handle(
        {**common, "hook_event_name": "PreToolUse", "event_id": "pre-b", "tool_call_id": "call-b"},
        71,
    )

    assert [item["tool_call_id"] for item in hook.load_state()["pending"]] == ["call-b"]


def test_hook_prunes_idle_sessions_without_replaying_retained_events(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_SESSION_STATES = 2
    first_prompt = {
        "hook_event_name": "UserPromptSubmit",
        "event_id": "prompt-a",
        "session_key": "chat-a",
    }
    for suffix, session in (("a", "chat-a"), ("b", "chat-b")):
        hook.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "event_id": f"prompt-{suffix}",
                "session_key": session,
            },
            80,
        )
        hook.handle(
            {"hook_event_name": "Stop", "event_id": f"stop-{suffix}", "session_key": session},
            90,
        )
    hook.handle(
        {"hook_event_name": "UserPromptSubmit", "event_id": "prompt-c", "session_key": "chat-c"},
        100,
    )

    state = hook.load_state()
    assert set(state["sessions"]) == {"chat-b", "chat-c"}
    assert hook.handle(first_prompt, 110) == []
    assert set(hook.load_state()["sessions"]) == {"chat-b", "chat-c"}


def test_hook_does_not_evict_open_session_at_capacity(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_SESSION_STATES = 1
    hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-active",
            "session_key": "active",
        },
        120,
    )

    ignored = hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "prompt-overflow",
            "session_key": "overflow",
        },
        130,
    )

    state = hook.load_state()
    assert ignored == []
    assert set(state["sessions"]) == {"active"}
    assert state["sessions"]["active"]["turn"] is not None


def test_hook_capacity_rejection_leaves_event_retryable(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_SESSION_STATES = 1
    hook.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "event_id": "active-prompt",
            "session_key": "active",
        },
        170,
    )
    overflow_prompt = {
        "hook_event_name": "UserPromptSubmit",
        "event_id": "overflow-prompt",
        "session_key": "overflow",
    }

    assert hook.handle(overflow_prompt, 180) == []
    hook.handle(
        {"hook_event_name": "Stop", "event_id": "active-stop", "session_key": "active"},
        190,
    )
    retry_spans = hook.handle(overflow_prompt, 200)

    assert any(span["name"] == "user.prompt" for span in retry_spans)
    assert set(hook.load_state()["sessions"]) == {"overflow"}


def test_hook_caps_agent_spans_per_turn_with_root_fallback(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_AGENTS_PER_TURN = 2
    hook.handle(
        {"hook_event_name": "UserPromptSubmit", "event_id": "prompt", "session_key": "chat-1"},
        140,
    )
    for suffix, role in (("a", "worker-a"), ("b", "worker-b"), ("c", "worker-c")):
        hook.handle(
            {
                "hook_event_name": "PreToolUse",
                "event_id": f"pre-{suffix}",
                "tool_call_id": f"call-{suffix}",
                "session_key": "chat-1",
                "subagent_id": f"agent-{suffix}",
                "agent_role": role,
                "tool_name": "ReadFile",
            },
            150,
        )

    spans = hook.handle(
        {"hook_event_name": "Stop", "event_id": "stop", "session_key": "chat-1"},
        160,
    )
    agent_names = {span["name"] for span in spans if span["name"].startswith("invoke_agent ")}
    tool_spans = [span for span in spans if span["name"].startswith("execute_tool ")]
    root_span = next(span for span in spans if span["name"] == "invoke_agent")
    overflow_tool = next(
        span for span in tool_spans if attribute(span, "gen_ai.tool.call.id") == "call-c"
    )

    assert agent_names == {"invoke_agent worker-a", "invoke_agent worker-b"}
    assert len(tool_spans) == 3
    assert overflow_tool["parentSpanId"] == root_span["spanId"]


def test_hook_migrates_oversized_open_turn_agent_state(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    hook.MAX_AGENTS_PER_TURN = 2
    hook.save_state(
        {
            "sessions": {
                "chat-1": {
                    "trace_id": "a" * 32,
                    "turn": {
                        "trace_id": "a" * 32,
                        "span_id": "b" * 16,
                        "start_ns": 1,
                        "prompt": "prompt",
                        "agents": {
                            f"agent-{suffix}": {
                                "span_id": f"{suffix}" * 16,
                                "role": f"worker-{suffix}",
                                "start_ns": suffix_index,
                                "last_ns": suffix_index,
                            }
                            for suffix_index, suffix in enumerate(("a", "b", "c"), 1)
                        },
                    },
                }
            },
            "pending": [],
        }
    )

    spans = hook.handle(
        {"hook_event_name": "Stop", "event_id": "stop", "session_key": "chat-1"},
        10,
    )
    agent_names = {span["name"] for span in spans if span["name"].startswith("invoke_agent ")}

    assert agent_names == {"invoke_agent worker-a", "invoke_agent worker-b"}


def test_workflow_turn_joins_by_run_agent_and_acp_session(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    stamp = "2026-10-21T10:00:00+00:00"
    run = {
        "run_id": "run-1",
        "session_key": "dashboard:agentcore-eval-root-1",
        "name": "research",
        "args": {},
        "result": "done",
        "events": [
            {"type": "run_started", "ts": stamp, "data": {}},
            {
                "type": "agent_started",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "label": "worker"},
            },
            {
                "type": "agent_session_bound",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "acp_session_id": "acp-1"},
            },
            {"type": "agent_finished", "ts": stamp, "data": {"agent_id": "agent-1", "ok": True}},
            {"type": "run_finished", "ts": stamp, "data": {}},
        ],
    }
    turns = [
        {
            "sid": "acp-1",
            "workflow_run_id": "run-1",
            "agent_id": "agent-1",
            "start": 1,
            "end": 2,
            "frames": [
                {
                    "ids": {"toolCallId": "matched"},
                    "update": "tool_call",
                    "ts_ns": "1",
                    "content": {"input": "same"},
                }
            ],
            "usage": {},
        },
        {
            "sid": "acp-other",
            "workflow_run_id": "run-1",
            "agent_id": "agent-1",
            "start": 3,
            "end": 4,
            "frames": [
                {
                    "ids": {"toolCallId": "wrong-session"},
                    "update": "tool_call",
                    "ts_ns": "3",
                    "content": {"input": "same"},
                }
            ],
            "usage": {},
        },
        {
            "sid": "acp-1",
            "workflow_run_id": "run-1",
            "agent_id": "agent-other",
            "start": 5,
            "end": 6,
            "frames": [
                {
                    "ids": {"toolCallId": "wrong-agent"},
                    "update": "tool_call",
                    "ts_ns": "5",
                    "content": {"input": "same"},
                }
            ],
            "usage": {},
        },
    ]

    spans = converter.workflow_spans(run, turns, set(), [], set(), [])
    tool_ids = {
        attribute(span, "gen_ai.tool.call.id")
        for span in spans
        if span["name"].startswith("execute_tool ")
    }

    assert tool_ids == {"matched"}


def test_workflow_turn_does_not_join_by_timestamp_name_or_input(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    stamp = "2026-10-21T10:00:00+00:00"
    run = {
        "run_id": "run-1",
        "session_key": "chat-1",
        "name": "same-name",
        "args": {},
        "events": [
            {"type": "run_started", "ts": stamp, "data": {}},
            {
                "type": "agent_started",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "label": "same-name"},
            },
            {
                "type": "agent_session_bound",
                "ts": stamp,
                "data": {"agent_id": "agent-1", "acp_session_id": "acp-1"},
            },
            {"type": "run_finished", "ts": stamp, "data": {}},
        ],
    }
    unrelated = {
        "sid": "acp-1",
        "workflow_run_id": "other-run",
        "agent_id": "agent-1",
        "start": 1,
        "end": 2,
        "frames": [
            {
                "ids": {"toolCallId": "unrelated"},
                "update": "tool_call",
                "ts_ns": "1",
                "content": {"name": "same-name", "input": "same"},
            }
        ],
        "usage": {},
    }

    spans = converter.workflow_spans(run, [unrelated], set(), [], set(), [])

    assert not any(span["name"].startswith("execute_tool ") for span in spans)


@pytest.mark.parametrize("ledger", ["emitted", "runs"], ids=["turns", "workflow-runs"])
def test_converter_bounds_exact_id_replay_window(monkeypatch, tmp_path, ledger):
    converter = load_converter(monkeypatch, tmp_path)
    state = {}
    for number in range(1, 5):
        converter.remember_replay_id(state, ledger, f"id-{number}", limit=2)

    assert state[ledger] == ["id-3", "id-4"]
    assert converter.replay_is_suppressed(state, ledger, "id-4")
    assert not converter.replay_is_suppressed(state, ledger, "id-1")
    assert not converter.replay_is_suppressed(state, ledger, "unseen-late-id")
    assert not converter.replay_is_suppressed(state, ledger, "id-1", force=True)


def test_converter_allows_replay_after_turn_id_eviction(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_OTEL_DRY_RUN_FILE", str(tmp_path / "otlp.jsonl"))
    converter = load_converter(monkeypatch, tmp_path)
    monkeypatch.setitem(converter._REPLAY_LIMITS, "emitted", 1)
    frames = tmp_path / "frames.jsonl"
    frame_rows = []
    for request_id, turn_id, start_ns, end_ns in (
        (1, "turn-a", 100, 200),
        (2, "turn-b", 300, 400),
    ):
        frame_rows.extend(
            [
                {
                    "pid": 11,
                    "id": request_id,
                    "method": "session/prompt",
                    "dir": "client_to_agent",
                    "kind": "request",
                    "ids": {
                        "sessionId": "acp-1",
                        "session_key": "chat-1",
                        "turn_id": turn_id,
                    },
                    "ts_ns": str(start_ns),
                },
                {
                    "pid": 11,
                    "id": request_id,
                    "dir": "agent_to_client",
                    "kind": "response",
                    "ids": {"sessionId": "acp-1"},
                    "nums": {},
                    "ts_ns": str(end_ns),
                },
            ]
        )
    frames.write_text("".join(json.dumps(row) + "\n" for row in frame_rows), encoding="utf-8")
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text("", encoding="utf-8")
    session_map = tmp_path / "session-map.json"
    session_map.write_text("{}", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    state_path = tmp_path / "emitted-state.json"
    args = [
        "--frames",
        str(frames),
        "--prompts",
        str(prompts),
        "--session-map",
        str(session_map),
        "--runs-dir",
        str(runs_dir),
        "--state",
        str(state_path),
        "--since",
        "1000000000",
        "--grace",
        "0",
    ]

    converter.main(args)
    first_export = (tmp_path / "otlp.jsonl").read_text(encoding="utf-8").splitlines()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    converter.main(args)
    exports = (tmp_path / "otlp.jsonl").read_text(encoding="utf-8").splitlines()

    assert len(persisted["emitted"]) == 1
    assert "emitted_through_ns" not in persisted
    assert len(exports) == 2
    assert len(json.loads(exports[0])["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 2
    assert len(json.loads(exports[1])["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 1
    assert exports[0] == first_export[0]


def test_converter_emits_unseen_late_turn_despite_legacy_watermark(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_OTEL_DRY_RUN_FILE", str(tmp_path / "otlp.jsonl"))
    converter = load_converter(monkeypatch, tmp_path)
    frames = tmp_path / "frames.jsonl"
    rows = [
        {
            "pid": 12,
            "id": 1,
            "method": "session/prompt",
            "dir": "client_to_agent",
            "kind": "request",
            "ids": {"sessionId": "acp-late", "session_key": "chat-late", "turn_id": "late"},
            "ts_ns": "100",
        },
        {
            "pid": 12,
            "id": 1,
            "dir": "agent_to_client",
            "kind": "response",
            "ids": {"sessionId": "acp-late"},
            "nums": {},
            "ts_ns": "400",
        },
    ]
    frames.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text("", encoding="utf-8")
    session_map = tmp_path / "session-map.json"
    session_map.write_text("{}", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    state_path = tmp_path / "emitted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "emitted": ["acp-earlier@turn-a", "acp-later@turn-b"],
                "emitted_timestamps": {"acp-earlier@turn-a": 100, "acp-later@turn-b": 500},
                "runs": [],
                "emitted_through_ns": 500,
                "runs_through_ns": 500,
            }
        ),
        encoding="utf-8",
    )

    converter.main(
        [
            "--frames",
            str(frames),
            "--prompts",
            str(prompts),
            "--session-map",
            str(session_map),
            "--runs-dir",
            str(runs_dir),
            "--state",
            str(state_path),
            "--since",
            "1000000000",
            "--grace",
            "0",
        ]
    )
    otlp_path = tmp_path / "otlp.jsonl"
    payloads = (
        [json.loads(line) for line in otlp_path.read_text(encoding="utf-8").splitlines()]
        if otlp_path.exists()
        else []
    )
    spans = [
        span
        for payload in payloads
        for resource in payload["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    migrated_state = json.loads(state_path.read_text(encoding="utf-8"))

    assert any(span["name"] == "invoke_agent" for span in spans)
    assert "emitted_through_ns" not in migrated_state
    assert "runs_through_ns" not in migrated_state


def test_converter_does_not_reexport_suppressed_workflow_turn_as_chat(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_OTEL_DRY_RUN_FILE", str(tmp_path / "otlp.jsonl"))
    converter = load_converter(monkeypatch, tmp_path)
    frames = tmp_path / "frames.jsonl"
    rows = [
        {
            "pid": 13,
            "id": 7,
            "method": "session/prompt",
            "dir": "client_to_agent",
            "kind": "request",
            "ids": {
                "sessionId": "acp-workflow",
                "session_key": "chat-workflow",
                "workflow_run_id": "run-1",
                "agent_id": "agent-1",
                "turn_id": "turn-1",
            },
            "ts_ns": "100",
        },
        {
            "pid": 13,
            "id": 7,
            "dir": "agent_to_client",
            "kind": "response",
            "ids": {"sessionId": "acp-workflow"},
            "nums": {},
            "ts_ns": "200",
        },
    ]
    frames.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    prompts = tmp_path / "prompts.jsonl"
    prompts.write_text("", encoding="utf-8")
    session_map = tmp_path / "session-map.json"
    session_map.write_text("{}", encoding="utf-8")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run = {
        "run_id": "run-1",
        "session_key": "chat-workflow",
        "name": "workflow",
        "args": {},
        "result": "done",
        "events": [
            {"type": "run_started", "ts": "2020-01-01T00:00:00+00:00", "data": {}},
            {
                "type": "agent_started",
                "ts": "2020-01-01T00:00:00+00:00",
                "data": {"agent_id": "agent-1", "label": "worker"},
            },
            {
                "type": "agent_session_bound",
                "ts": "2020-01-01T00:00:00+00:00",
                "data": {"agent_id": "agent-1", "acp_session_id": "acp-workflow"},
            },
            {
                "type": "agent_finished",
                "ts": "2020-01-01T00:00:00+00:00",
                "data": {"agent_id": "agent-1", "ok": True},
            },
            {"type": "run_finished", "ts": "2020-01-01T00:00:01+00:00", "data": {}},
        ],
    }
    (runs_dir / "wf_run-1.json").write_text(json.dumps(run), encoding="utf-8")
    args = [
        "--frames",
        str(frames),
        "--prompts",
        str(prompts),
        "--session-map",
        str(session_map),
        "--runs-dir",
        str(runs_dir),
        "--state",
        str(tmp_path / "emitted-state.json"),
        "--since",
        "1000000000",
        "--grace",
        "0",
    ]

    converter.main(args)
    first_export = (tmp_path / "otlp.jsonl").read_text(encoding="utf-8")
    (runs_dir / "wf_run-1.json").unlink()
    converter.main(args)

    assert (tmp_path / "otlp.jsonl").read_text(encoding="utf-8") == first_export


def test_suppressed_workflow_run_does_not_reexport_its_acp_turn_as_chat(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    run = {
        "run_id": "run-old",
        "events": [
            {"type": "agent_started", "data": {"agent_id": "agent-1"}},
            {
                "type": "agent_session_bound",
                "data": {"agent_id": "agent-1", "acp_session_id": "acp-1"},
            },
        ],
    }
    turn = {
        "workflow_run_id": "run-old",
        "agent_id": "agent-1",
        "sid": "acp-1",
    }
    unrelated = {
        "workflow_run_id": "run-old",
        "agent_id": "agent-1",
        "sid": "acp-other",
    }
    used = set()

    converter.mark_workflow_turns_used(run, [turn, unrelated], used)

    assert id(turn) in used
    assert id(unrelated) not in used


def test_acp_session_resolution_uses_agentcore_parent_root(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)

    session = converter.resolve_session_key(
        {
            "sid": "acp-1",
            "session_key": "agent-slot-1",
            "parent_session_key": "dashboard:agentcore-eval-root-1",
        },
        {"acp-1": "mapped-chat"},
    )

    assert session == "dashboard:agentcore-eval-root-1"


def test_acp_session_resolution_ignores_non_root_parent(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)

    session = converter.resolve_session_key(
        {
            "sid": "acp-1",
            "session_key": "own-chat",
            "parent_session_key": "dashboard:ordinary-parent",
        },
        {"acp-1": "mapped-chat"},
    )

    assert session == "own-chat"


def test_acp_session_resolution_uses_legacy_id_before_session_map(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)

    session = converter.resolve_session_key(
        {"sid": "acp-1", "session_id": "legacy-chat"},
        {"acp-1": "mapped-chat"},
    )

    assert session == "legacy-chat"


def test_acp_chat_span_keeps_own_and_parent_session_attributes(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    turn = {
        "sid": "acp-1",
        "session_key": "agent-slot-1",
        "parent_session_key": "dashboard:agentcore-eval-root-1",
        "start": 1,
        "end": 2,
        "frames": [],
        "usage": {"inputTokens": 1},
    }

    spans = converter.turn_spans(
        turn,
        converter.resolve_session_key(turn),
        "a" * 32,
        None,
    )
    chat = next(span for span in spans if span["name"] == "chat")

    assert attribute(chat, "session.id") == "dashboard:agentcore-eval-root-1"
    assert attribute(chat, "crew.session_key") == "agent-slot-1"
    assert attribute(chat, "crew.parent_session_key") == "dashboard:agentcore-eval-root-1"


def test_exporter_redacts_and_truncates_content():
    hook = load_module(HOOK_PATH, "aws_eval_redaction_under_test")
    value, truncated = hook.safe_text("password=hunter2 " + ("x" * 9000))

    assert value.startswith("password=[REDACTED]")
    assert len(value) == 8192
    assert truncated is True


def test_hook_export_failure_is_fail_open(monkeypatch, tmp_path):
    hook = load_module(HOOK_PATH, "aws_eval_export_failure_under_test")
    monkeypatch.setattr(hook, "DRY_RUN_FILE", None)
    monkeypatch.setattr(hook, "TRACE_ROOT", tmp_path)
    monkeypatch.setattr(
        hook.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    hook.export([hook.span("a" * 32, "b" * 16, "test", 1, 2, [])])


def test_export_outbox_retries_original_span_ids_on_empty_pass(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "DRY_RUN_FILE", None)
    with monkeypatch.context() as offline:
        offline.setattr(hook.urllib.request, "urlopen",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("offline")))
        hook.export([hook.span("a" * 32, "b" * 16, "test", 1, 2, [])])
    pending = list((hook.TRACE_ROOT / "export-outbox").glob("*.json"))
    assert len(pending) == 1
    original = pending[0].read_bytes()
    received = []

    def send(request, **_kwargs):
        received.append(request.data)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(hook.urllib.request, "urlopen", send)
    hook.export([])
    hook.export([])
    assert received == [original]
    assert not list((hook.TRACE_ROOT / "export-outbox").glob("*.json"))
    stored = json.loads(original)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert stored["traceId"] == "a" * 32
    assert stored["spanId"] == "b" * 16


def test_export_outbox_retains_partial_rejections_and_dry_run_does_not_drain(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch, tmp_path)
    monkeypatch.setattr(hook, "DRY_RUN_FILE", None)
    monkeypatch.setattr(hook.urllib.request, "urlopen",
                        lambda *_a, **_kw: io.BytesIO(b'{"partialSuccess":{"rejectedSpans":"1"}}'))
    hook.export([hook.span("a" * 32, "b" * 16, "test", 1, 2, [])])
    assert len(list((hook.TRACE_ROOT / "export-outbox").glob("*.json"))) == 1
    monkeypatch.setattr(hook, "DRY_RUN_FILE", str(tmp_path / "dry.jsonl"))

    def unexpected_send(*_args, **_kwargs):
        pytest.fail("dry-run must not drain live exports")

    monkeypatch.setattr(hook.urllib.request, "urlopen", unexpected_send)
    hook.export([])
    assert len(list((hook.TRACE_ROOT / "export-outbox").glob("*.json"))) == 1


def test_cancelled_workflow_keeps_unfinished_prompt_tool(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    rows = [
        {"pid": 1, "id": 2, "method": "session/prompt", "dir": "client_to_agent",
         "ids": {"sessionId": "session", "workflow_run_id": "wf_test", "agent_id": "a0",
                 "turn_id": "wf_test:a0"}, "ts_ns": "1793106000000000000"},
        {"pid": 1, "kind": "notification", "dir": "agent_to_client",
         "ids": {"workflow_run_id": "wf_test", "agent_id": "a0", "toolCallId": "call",
                 "name": "shell", "status": "in_progress"},
         "update": "tool_call", "content": {"input": '{"command":"sleep 20"}'},
         "ts_ns": "1793106001000000000"},
    ]
    assert converter.build_turns(rows) == []
    turns = converter.build_turns(rows, include_incomplete=True)
    run = {"run_id": "wf_test", "name": "test", "session_key": "dashboard:test", "events": [
        {"type": "run_started", "ts": "2026-10-27T13:00:00+00:00", "data": {}},
        {"type": "agent_started", "ts": "2026-10-27T13:00:00+00:00", "data": {"agent_id": "a0"}},
        {"type": "agent_session_bound", "ts": "2026-10-27T13:00:00+00:00",
         "data": {"agent_id": "a0", "acp_session_id": "session"}},
        {"type": "run_cancelled", "ts": "2026-10-27T13:00:10+00:00", "data": {}},
    ]}
    spans = converter.workflow_spans(run, turns, set(), [], set(), [])
    tool = next(s for s in spans if s["name"] == "execute_tool shell")
    agent = next(s for s in spans if s["name"].startswith("invoke_agent "))
    assert tool["traceId"] == agent["traceId"]
    assert tool["parentSpanId"] == agent["spanId"]
    assert attribute(tool, "tool.partial") is True
    assert attribute(tool, "tool.status") == "cancelled"
    assert attribute(tool, "tool.result.available") is False
    assert tool["status"]["code"] == 2
    assert tool["endTimeUnixNano"] == "1793106010000000000"


def test_workflow_launch_joins_exact_returned_run_id_and_hook_ids(monkeypatch, tmp_path):
    converter = load_converter(monkeypatch, tmp_path)
    run = {"run_id": "wf_test", "name": "test", "session_key": "dashboard:test", "events": [
        {"type": "run_started", "ts": "2026-10-27T13:00:00+00:00", "data": {}},
        {"type": "run_finished", "ts": "2026-10-27T13:00:10+00:00", "data": {}},
    ]}
    output = json.dumps({"items": [{"Json": {"content": [
        {"type": "text", "text": "Started workflow run `wf_test` (name: test)."}
    ]}}]})
    turn = {"session_key": "dashboard:test", "hook_trace_id": "a" * 32,
            "hook_span_id": "b" * 16, "frames": [
        {"ids": {"toolCallId": "launch", "name": "workflow_run", "status": "completed"},
         "update": "tool_call", "content": {"output": output}, "ts_ns": "1"},
    ]}
    spans = converter.workflow_spans(run, [turn], set(), [], set(), [])
    assert spans[0]["traceId"] == "a" * 32
    assert spans[0]["parentSpanId"] == "b" * 16
    assert attribute(spans[0], "workflow.launch_tool_call_id") == "launch"
    turn["frames"][0]["ids"]["name"] = "workflow_result"
    spans = converter.workflow_spans(run, [turn], set(), [], set(), [])
    assert "parentSpanId" not in spans[0]


def test_installer_is_import_safe_and_requires_explicit_action(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    original = '{"hooks":[{"id":"custom","enabled":true}]}'
    hooks_path.write_text(original, encoding="utf-8")
    installer = load_module(INSTALLER_PATH, "aws_eval_installer_under_test")

    try:
        installer.main([])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("installer without an action should show usage")

    assert hooks_path.read_text(encoding="utf-8") == original


def test_installer_backups_and_restores_hook_configuration(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    original = {
        "hooks": [
            {"id": "otel-legacy", "enabled": True, "command": "legacy"},
            {"id": "custom", "enabled": True, "command": "custom"},
        ]
    }
    hooks_path.write_text(json.dumps(original), encoding="utf-8")
    installer = load_module(INSTALLER_PATH, "aws_eval_installer_actions_under_test")

    installer.main(["enable", "--hooks", str(hooks_path)])
    enabled = json.loads(hooks_path.read_text(encoding="utf-8"))
    enable_backups = list(tmp_path.glob("hooks.json.before-v2-enable-*.bak"))
    assert len(enable_backups) == 1
    assert json.loads(enable_backups[0].read_text(encoding="utf-8")) == original
    assert next(h for h in enabled["hooks"] if h["id"] == "otel-legacy")["enabled"] is False
    assert any(h["id"] == "otelv2-userpromptsubmit" for h in enabled["hooks"])

    installer.main(["disable", "--hooks", str(hooks_path)])
    restored = json.loads(hooks_path.read_text(encoding="utf-8"))
    disable_backups = list(tmp_path.glob("hooks.json.before-v2-disable-*.bak"))
    assert len(disable_backups) == 1
    assert json.loads(disable_backups[0].read_text(encoding="utf-8")) == enabled
    assert restored["hooks"] == original["hooks"]


def test_installer_repeated_actions_restore_original_enabled_states(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    original = {
        "hooks": [
            {"id": "otel-enabled", "enabled": True, "command": "one"},
            {"id": "otel-disabled", "enabled": False, "command": "two"},
            {"id": "otel-default", "command": "default"},
            {"id": "custom", "enabled": True, "command": "custom"},
        ]
    }
    hooks_path.write_text(json.dumps(original), encoding="utf-8")
    installer = load_module(INSTALLER_PATH, "aws_eval_installer_repeated_actions_under_test")

    installer.main(["enable", "--hooks", str(hooks_path)])
    installer.main(["enable", "--hooks", str(hooks_path)])
    assert len(list(tmp_path.glob("hooks.json.before-v2-enable-*.bak"))) == 1
    installer.main(["disable", "--hooks", str(hooks_path)])
    installer.main(["disable", "--hooks", str(hooks_path)])
    restored = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert restored["hooks"] == original["hooks"]


def test_installer_repairs_partial_v2_hooks_preserving_original_backup_and_user_hooks(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    original = {
        "hooks": [
            {"id": "otel-legacy", "enabled": True, "command": "legacy"},
            {"id": "custom", "enabled": True, "command": "before"},
        ]
    }
    hooks_path.write_text(json.dumps(original), encoding="utf-8")
    installer = load_module(INSTALLER_PATH, "aws_eval_installer_partial_install_under_test")
    installer.main(["enable", "--hooks", str(hooks_path)])
    partial = json.loads(hooks_path.read_text(encoding="utf-8"))
    partial["hooks"] = [
        {**hook, "command": "stale", "enabled": False} if hook["id"] == "otelv2-stop" else hook
        for hook in partial["hooks"]
        if hook["id"] != "otelv2-userpromptsubmit"
    ]
    next(hook for hook in partial["hooks"] if hook["id"] == "custom")["command"] = "operator edit"
    hooks_path.write_text(json.dumps(partial), encoding="utf-8")

    installer.main(["enable", "--hooks", str(hooks_path)])
    repaired = json.loads(hooks_path.read_text(encoding="utf-8"))
    v2_hooks = [hook for hook in repaired["hooks"] if hook["id"].startswith("otelv2-")]

    assert {hook["id"] for hook in v2_hooks} == {
        "otelv2-agentspawn",
        "otelv2-userpromptsubmit",
        "otelv2-pretooluse",
        "otelv2-posttooluse",
        "otelv2-stop",
    }
    assert all(hook["enabled"] is True for hook in v2_hooks)
    assert (
        next(hook for hook in repaired["hooks"] if hook["id"] == "custom")["command"]
        == "operator edit"
    )
    assert len(list(tmp_path.glob("hooks.json.before-v2-enable-*.bak"))) == 1
    assert len(list(tmp_path.glob("hooks.json.before-v2-repair-*.bak"))) == 1

    installer.main(["disable", "--hooks", str(hooks_path)])
    restored = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert (
        next(hook for hook in restored["hooks"] if hook["id"] == "otel-legacy")["enabled"] is True
    )
    assert (
        next(hook for hook in restored["hooks"] if hook["id"] == "custom")["command"]
        == "operator edit"
    )
    assert not any(hook["id"].startswith("otelv2-") for hook in restored["hooks"])


def run_packaged_tap(
    tmp_path,
    *,
    env_overrides=None,
    raw_input="tool payload",
    prompt_text="password=prompt-secret",
    prompt_messages=None,
):
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the optional ACP tap"
    shim = tmp_path / "shim" / "@agentclientprotocol" / "codex-acp" / "dist" / "index.js"
    build_env = os.environ.copy()
    build_env["HOME"] = str(tmp_path / "home")
    build_env["PATH"] = os.pathsep.join(
        (str(Path(node).parent), "/usr/bin", "/bin", build_env.get("PATH", ""))
    )
    (tmp_path / "home").mkdir()
    subprocess.run(
        ["/bin/sh", str(SHIM_BUILDER_PATH), str(shim)],
        cwd=tmp_path,
        env=build_env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    fake_adapter = tmp_path / "fake-acp-adapter.py"
    fake_adapter.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "sys.stdin.buffer.read()\n"
        "for update in [\n"
        " {'sessionUpdate':'tool_call','toolCallId':'call-1','rawInput':os.environ.get('ACP_FAKE_INPUT', 'tool payload'),"
        "  'rawOutput':'password=adapter-secret','title':'run'},\n"
        " {'sessionUpdate':'agent_message_chunk','content':{'text':'assistant text'}},\n"
        "]: print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':"
        "{'sessionId':'acp-1','update':update}}), flush=True)\n"
        "print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'sessionId':'acp-old'}}), flush=True)\n"
        "print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':"
        "{'sessionId':'acp-old','update':{'sessionUpdate':'agent_message_chunk',"
        "'content':{'text':'context probe'}}}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_adapter.chmod(0o700)
    output = tmp_path / "frames.jsonl"
    prompts = tmp_path / "prompts.jsonl"
    env = build_env.copy()
    env.update(
        {
            "ACP_TAP_REAL_BIN": str(fake_adapter),
            "ACP_TAP_OUT": str(output),
            "ACP_TAP_PROMPTS_OUT": str(prompts),
            "ACP_FAKE_INPUT": raw_input,
        }
    )
    if env_overrides:
        env.update(env_overrides)
    if prompt_messages is None:
        prompt_messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/prompt",
                "params": {
                    "sessionId": "acp-1",
                    "prompt": [
                        {"text": f"[CURRENT USER REQUEST -- respond to this]\n{prompt_text}"}
                    ],
                },
            }
        ]
    request = "".join(json.dumps(message) + "\n" for message in prompt_messages)
    command = [node, str(shim)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(request, timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        raise
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    frames = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    prompt_rows = (
        [json.loads(line) for line in prompts.read_text(encoding="utf-8").splitlines()]
        if prompts.exists()
        else []
    )
    return frames, prompt_rows, result


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_tap_carries_active_hook_turn_identity_into_unannotated_acp_frames(tmp_path):
    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    (crew_home / "session_map.json").write_text(
        json.dumps({"dashboard:chat-one": {"sid": "acp-1"}}), encoding="utf-8"
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({
            "sessions": {
                "dashboard:chat-one": {
                    "turn": {"turn_id": "turn-one", "session_key": "dashboard:chat-one"}
                }
            }
        }),
        encoding="utf-8",
    )

    frames, _, _ = run_packaged_tap(
        tmp_path,
        env_overrides={
            "KIROCREW_HOME": str(crew_home),
            "KIRO_OTEL_STATE_DIR": str(state_dir),
        },
    )

    prompt = next(frame for frame in frames if frame.get("method") == "session/prompt")
    tool = next(frame for frame in frames if frame.get("update") == "tool_call")
    assert prompt["ids"].get("turn_id") == "turn-one"
    assert prompt["ids"].get("session_key") == "dashboard:chat-one"
    assert tool["ids"].get("turn_id") == "turn-one"


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_tap_carries_active_hook_span_identity_without_turn_id(tmp_path):
    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    (crew_home / "session_map.json").write_text(
        json.dumps({"dashboard:chat-one": {"sid": "acp-1"}}), encoding="utf-8"
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({
            "sessions": {
                "dashboard:chat-one": {
                    "turn": {
                        "trace_id": "a" * 32,
                        "span_id": "b" * 16,
                        "session_key": "dashboard:chat-one",
                    }
                }
            }
        }),
        encoding="utf-8",
    )

    frames, _, _ = run_packaged_tap(
        tmp_path,
        env_overrides={
            "KIROCREW_HOME": str(crew_home),
            "KIRO_OTEL_STATE_DIR": str(state_dir),
        },
    )

    prompt = next(frame for frame in frames if frame.get("method") == "session/prompt")
    tool = next(frame for frame in frames if frame.get("update") == "tool_call")
    assert prompt["ids"].get("hook_trace_id") == "a" * 32
    assert prompt["ids"].get("hook_span_id") == "b" * 16
    assert tool["ids"].get("hook_span_id") == "b" * 16


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_tap_does_not_join_ambiguous_acp_session_owners(tmp_path):
    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    (crew_home / "session_map.json").write_text(
        json.dumps({
            "dashboard:chat-one": {"sid": "acp-1"},
            "dashboard:chat-two": {"sid": "acp-1"},
        }),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({
            "sessions": {
                "dashboard:chat-one": {
                    "turn": {"turn_id": "turn-one", "session_key": "dashboard:chat-one"}
                },
                "dashboard:chat-two": {
                    "turn": {"turn_id": "turn-two", "session_key": "dashboard:chat-two"}
                },
            }
        }),
        encoding="utf-8",
    )

    frames, _, _ = run_packaged_tap(
        tmp_path,
        env_overrides={
            "KIROCREW_HOME": str(crew_home),
            "KIRO_OTEL_STATE_DIR": str(state_dir),
        },
    )

    prompt = next(frame for frame in frames if frame.get("method") == "session/prompt")
    tool = next(frame for frame in frames if frame.get("update") == "tool_call")
    assert "turn_id" not in prompt["ids"]
    assert "turn_id" not in tool["ids"]


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_packaged_shim_captures_redacted_bounded_content_by_default(tmp_path):
    frames, prompts, _ = run_packaged_tap(
        tmp_path,
        raw_input="password=tool-secret " + ("x" * 9000),
    )
    tool_content = next(frame["content"] for frame in frames if frame.get("update") == "tool_call")
    prompt_content = prompts[0]["text"]

    assert len(tool_content["input"]) == 8192
    assert "password=[REDACTED]" in tool_content["input"]
    assert "adapter-secret" not in tool_content["output"]
    assert "password=[REDACTED]" in tool_content["output"]
    assert prompt_content == "password=[REDACTED]"
    assert "tool-secret" not in json.dumps(frames)


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_packaged_shim_never_records_credential_environment_values(tmp_path):
    api_key = "api-key-value-that-must-not-be-recorded"
    access_key = "access-key-value-that-must-not-be-recorded"
    frames, _, _ = run_packaged_tap(
        tmp_path,
        env_overrides={
            "KIROCREW_API_KEY": api_key,
            "KIROCREW_AWS_ACCESS_KEY_ID": access_key,
        },
    )
    start = next(frame for frame in frames if frame.get("kind") == "process_start")

    assert "env" not in start
    assert "KIROCREW_API_KEY" in start["env_names"]
    assert "KIROCREW_AWS_ACCESS_KEY_ID" in start["env_names"]
    assert api_key not in json.dumps(frames)
    assert access_key not in json.dumps(frames)


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_prompt_capture_is_bounded_to_8192_even_with_larger_override(tmp_path):
    frames, prompts, _ = run_packaged_tap(
        tmp_path,
        env_overrides={"ACP_TAP_MAX_PROMPT": "16000"},
        prompt_text="p" * 9000,
    )

    assert len(prompts[0]["text"]) == 8192
    assert prompts[0]["truncated"] is True


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_content_opt_out_does_not_disable_prompt_capture(tmp_path):
    frames, prompts, _ = run_packaged_tap(tmp_path, env_overrides={"ACP_TAP_CONTENT": "0"})

    assert all("content" not in frame for frame in frames if frame.get("update"))
    assert prompts[0]["text"] == "password=[REDACTED]"


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_prompt_opt_out_does_not_disable_content_capture(tmp_path):
    frames, prompts, _ = run_packaged_tap(tmp_path, env_overrides={"ACP_TAP_PROMPTS": "0"})

    assert any(frame.get("content", {}).get("input") for frame in frames)
    assert prompts == []


@pytest.mark.skipif(os.name != "posix", reason="the Kiro shim builder is POSIX shell")
def test_kiro_shim_builder_does_not_require_gnu_readlink(tmp_path):
    adapter_dir = tmp_path / "acp-tap"
    adapter_dir.mkdir()
    builder = adapter_dir / "make-kiro-shim.sh"
    builder.write_bytes(KIRO_SHIM_BUILDER_PATH.read_bytes())
    (adapter_dir / "acp-tap.js").write_bytes(TAP_SCRIPT_PATH.read_bytes())
    fake_cli = tmp_path / "fake-kiro"
    fake_cli.write_text(f"#!{sys.executable}\npass\n", encoding="utf-8")
    fake_cli.chmod(0o700)
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    for name in ("node", "dirname", "basename", "mkdir", "chmod", "cat"):
        executable = shutil.which(name)
        assert executable
        (tool_dir / name).symlink_to(executable)
    env = {"HOME": str(tmp_path / "home"), "PATH": str(tool_dir)}
    (tmp_path / "home").mkdir()

    subprocess.run(
        ["/bin/sh", str(builder), str(fake_cli)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    wrapper = (adapter_dir / "shim/kiro-cli").read_text(encoding="utf-8")

    assert "readlink" not in wrapper
    assert f'ACP_TAP_REAL_BIN="{fake_cli}"' in wrapper


@pytest.mark.skipif(os.name != "posix", reason="the packaged shell shim is POSIX-only")
def test_tap_bounds_unanswered_prompt_context_maps(tmp_path):
    prompts = [
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {"sessionId": session, "workflow_run_id": f"run-{request_id}"},
        }
        for request_id, session in ((1, "acp-old"), (2, "acp-middle"), (3, "acp-new"))
    ]
    frames, _, _ = run_packaged_tap(
        tmp_path,
        env_overrides={"ACP_TAP_MAX_PROMPT_CONTEXTS": "2"},
        prompt_messages=prompts,
    )
    response = next(frame for frame in frames if frame.get("kind") == "response")
    old_session_update = next(
        frame
        for frame in frames
        if frame.get("method") == "session/update"
        and frame.get("ids", {}).get("sessionId") == "acp-old"
    )

    assert "workflow_run_id" not in response["ids"]
    assert "workflow_run_id" not in old_session_update["ids"]
