#!/usr/bin/env python3
"""Optional KiroCrew hook adapter: chat/tool events to AgentCore-shaped OTLP spans.

Requires producer `event_id` and `tool_call_id` values for tool-event identity.
It pairs start and result events by scoped IDs, retains unmatched records with
their explicit workflow/agent/ACP IDs, and quarantines records that lack an
applicable ID. Timestamps set span bounds and pending-call expiry only.

It also:
  * closes an open turn if a new prompt arrives (overlap);
  * optional one-trace-per-session (KIRO_OTEL_TRACE_SCOPE=session);
  * agent spans when the payload carries subagent_id.

Standard library only. Never prints to stdout. Always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINT = os.environ.get("KIRO_OTEL_ENDPOINT", "http://127.0.0.1:14318").rstrip("/")
SERVICE_NAME = os.environ.get("KIRO_OTEL_SERVICE_NAME", "kiro-linux")
TRACE_ROOT = Path(os.environ.get("KIRO_OTEL_TRACE_DIR", Path.home() / ".kiro/otel-traces"))
STATE_ROOT = Path(os.environ.get("KIRO_OTEL_STATE_DIR", TRACE_ROOT / "state-v2"))
TRACE_SCOPE = os.environ.get("KIRO_OTEL_TRACE_SCOPE", "turn")  # turn | session
ORPHANS = os.environ.get("KIRO_OTEL_ORPHANS", "journal")  # journal | emit
ACP_CONVERTER = os.environ.get(
    "KIRO_OTEL_ACP_CONVERTER", ""
)  # path to acp-to-spans.py; run in the background after each Stop
ACP_DELAY = os.environ.get("KIRO_OTEL_ACP_DELAY", "3")
HOOK_TOOLS = os.environ.get(
    "KIRO_OTEL_HOOK_TOOLS", "on"
)  # on | off | chat: chat emits only keyed chat-tool spans
DRY_RUN_FILE = os.environ.get("KIRO_OTEL_DRY_RUN_FILE")
MAX_VALUE = 8192
MAX_PENDING_CALLS = 5_000
MAX_SESSION_STATES = 5_000
MAX_AGENTS_PER_TURN = 500
# Deduplication is exact for the most recent 10,000 distinct producer event IDs.
# IDs evicted beyond this finite replay window may be accepted as new deliveries.
MAX_SEEN_EVENT_IDS = 10_000
GENERIC_NAMES = {"", "unknown", "terminal", "tool"}

_REDACTIONS = (
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", re.I), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(bearer\s+)[A-Za-z0-9._~+/-]+=*", re.I), r"\1[REDACTED]"),
    (
        re.compile(r"\b(password|passwd|token|secret|api[_-]?key)\s*([:=])\s*([^\s,;\"}]+)", re.I),
        r"\1\2[REDACTED]",
    ),
)


def safe_text(value: Any, limit: int = MAX_VALUE) -> tuple[str, bool]:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text[:limit], len(text) > limit


def get(event: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in event and event[name] is not None:
            return event[name]
    return default


def attr(key: str, value: Any, kind: str = "stringValue") -> dict:
    if kind == "boolValue":
        value = bool(value)
    elif kind == "doubleValue":
        value = float(value)
    elif kind == "intValue":
        value = str(int(value))
    else:
        value = str(value)
    return {"key": key, "value": {kind: value}}


def span(
    trace_id, span_id, name, start_ns, end_ns, attrs, parent=None, session=None, status=0
) -> dict:
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(int(start_ns)),
        "endTimeUnixNano": str(max(int(start_ns), int(end_ns))),
        "attributes": attrs,
        "status": {"code": status},
    }
    if parent:
        s["parentSpanId"] = parent
    s["_session"] = session or "unknown"
    return s


def export(spans: list[dict]) -> None:
    by_session: dict[str, list[dict]] = {}
    for s in spans:
        by_session.setdefault(s.pop("_session", "unknown"), []).append(s)
    envelopes = []
    for session, group in by_session.items():
        envelope = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            attr("service.name", SERVICE_NAME),
                            attr("session.id", session),
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "opentelemetry.instrumentation.kirocrew_hooks"},
                            "spans": group,
                        }
                    ],
                }
            ]
        }
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if DRY_RUN_FILE:
            with open(DRY_RUN_FILE, "ab") as stream:
                stream.write(body + b"\n")
            continue
        envelopes.append(body)
    if DRY_RUN_FILE:
        return
    import fcntl

    outbox = TRACE_ROOT / "export-outbox"
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (outbox / ".lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            for body in envelopes:
                fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=outbox)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, outbox / f"{time.time_ns()}-{secrets.token_hex(8)}.json")
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            directory = os.open(outbox, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
                for pending in sorted(outbox.glob("*.json")):
                    request = urllib.request.Request(
                        f"{ENDPOINT}/v1/traces", data=pending.read_bytes(), method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=2) as response:
                            result = json.loads(response.read() or b"{}")
                        if int(result.get("partialSuccess", {}).get("rejectedSpans", 0)):
                            log_error(f"OTLP export rejected spans; retained {pending.name}: {result}")
                            break
                    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                        log_error(f"OTLP export failed; retained {pending.name}: {exc}")
                        break
                    pending.unlink()
                    os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def log_error(message: str) -> None:
    try:
        TRACE_ROOT.mkdir(parents=True, exist_ok=True)
        with (TRACE_ROOT / "hook-errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} v2 {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        return json.loads((STATE_ROOT / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sessions": {}, "pending": []}


def save_state(state: dict) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".state-", dir=STATE_ROOT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, separators=(",", ":"))
        os.replace(temp, STATE_ROOT / "state.json")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def append_jsonl(name: str, record: dict) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (STATE_ROOT / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- helpers
def norm_name(raw: Any, tool_input: Any) -> str:
    name = str(raw or "").strip()
    if name.startswith("Running: "):
        name = name[9:]
    if (
        name.lower() in GENERIC_NAMES
        and isinstance(tool_input, dict)
        and tool_input.get("server")
        and tool_input.get("tool")
    ):
        return f"mcp.{tool_input['server']}.{tool_input['tool']}"
    return name or "unknown"


def is_generic(name: str) -> bool:
    return name.strip().lower() in GENERIC_NAMES


AGENTCORE_ROOT_PREFIX = "dashboard:agentcore-eval-"


def session_identity(event: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Return effective, own, parent, and legacy session IDs from the event."""
    own = get(event, "session_key", "sessionKey")
    parent = get(event, "parent_session_key", "parentSessionKey")
    legacy = get(event, "session_id", "sessionId")
    if not isinstance(own, str):
        own = None
    if not isinstance(parent, str):
        parent = None
    if not isinstance(legacy, str):
        legacy = None
    effective = parent if parent and parent.startswith(AGENTCORE_ROOT_PREFIX) else own or legacy
    return effective, own, parent, legacy


def session_attributes(own: str | None, parent: str | None) -> list[dict]:
    attrs = []
    if own:
        attrs.append(attr("crew.session_key", own))
    if parent:
        attrs.append(attr("crew.parent_session_key", parent))
    return attrs


def correlation_key(event: dict) -> str:
    """Key a logical call from producer IDs only; never from time, name, or input."""
    call_id = get(event, "tool_call_id", "toolCallId")
    _, own_session_key, parent_session_key, legacy_session_id = session_identity(event)
    context = [
        get(event, "acp_session_id", "acpSessionId"),
        get(event, "workflow_run_id", "workflowRunId"),
        get(event, "agent_id", "agentId"),
        own_session_key,
        parent_session_key,
        legacy_session_id,
        get(event, "turn_id", "turnId"),
    ]
    return json.dumps([*context, call_id], separators=(",", ":"))


def quarantine_event(event: dict, reason: str) -> None:
    record = {
        "event_id": get(event, "event_id", "eventId"),
        "tool_call_id": get(event, "tool_call_id", "toolCallId"),
        "hook_event_name": get(event, "hook_event_name", "trigger"),
        "missing_ids": reason,
        "identity_conflict": reason == "conflicting_event_identity",
        "workflow_run_id": get(event, "workflow_run_id", "workflowRunId"),
        "agent_id": get(event, "agent_id", "agentId"),
        "acp_session_id": get(event, "acp_session_id", "acpSessionId"),
        "parent_session_key": get(event, "parent_session_key", "parentSessionKey"),
        "session_key": get(event, "session_key", "sessionKey"),
    }
    append_jsonl("correlation-errors.jsonl", record)
    log_error(f"event correlation rejected: {reason}; event_id={record['event_id']!r}")


def forget_event_identity(state: dict, event_id: Any) -> None:
    """Do not consume dedup capacity for an event rejected by session admission."""
    if not isinstance(event_id, str) or not event_id:
        return
    seen = state.get("seen_event_ids", [])
    try:
        seen.remove(event_id)
    except ValueError:
        pass
    state.get("seen_event_signatures", {}).pop(event_id, None)


def tool_output(event: dict) -> Any:
    response = get(event, "tool_response", "toolResponse", "tool_result", "toolResult", "result")
    if isinstance(response, dict):
        for key in ("output", "result", "content", "text"):
            if response.get(key) is not None:
                return response[key]
    return response


def outcome(event: dict) -> tuple[str, str]:
    response = get(event, "tool_response", "toolResponse", "tool_result", "toolResult", "result")
    for value in (response, event):
        if not isinstance(value, dict):
            continue
        for key in ("success", "succeeded", "ok", "is_success"):
            if isinstance(value.get(key), bool):
                return ("true", "success") if value[key] else ("false", "error")
        for key in ("is_error", "isError"):
            if isinstance(value.get(key), bool):
                return ("false", "error") if value[key] else ("true", "success")
        for key in ("error", "exception", "error_message"):
            if value.get(key):
                return "false", "error"
    if isinstance(response, str) and re.match(
        r"\s*(error\b|failed\b|exception\b|no such file\b)", response, re.I
    ):
        return "false", "error"
    return "unknown", "unknown"


def prune_sessions(
    state: dict, limit: int = MAX_SESSION_STATES, *, migrate_over_limit: bool = False
) -> None:
    """Drop least-recent idle sessions, pinning live state except during migration."""
    sessions = state.setdefault("sessions", {})
    for entry in sessions.values():
        entry.pop("last_seen_ns", None)
        turn = entry.get("turn")
        agents = turn.get("agents") if isinstance(turn, dict) else None
        if isinstance(agents, dict) and len(agents) > MAX_AGENTS_PER_TURN:
            turn["agents"] = dict(list(agents.items())[:MAX_AGENTS_PER_TURN])
            turn["agent_limit_logged"] = True
            log_error("trimmed over-limit agent span state in an open turn")
    pending_sessions = {item.get("session") for item in state.get("pending", [])}
    idle = [
        sid
        for sid, entry in sessions.items()
        if entry.get("turn") is None and sid not in pending_sessions
    ]
    while len(sessions) > limit and idle:
        sid = idle.pop(0)
        sessions.pop(sid, None)
    if migrate_over_limit and len(sessions) > limit:
        remaining = list(sessions)
        while len(sessions) > limit and remaining:
            sessions.pop(remaining.pop(0), None)
        log_error("pruned over-limit session state while migrating to bounded state")


def session_entry(state: dict, sid: str) -> dict | None:
    sessions = state.setdefault("sessions", {})
    entry = sessions.get(sid)
    if entry is None:
        prune_sessions(state, max(0, MAX_SESSION_STATES - 1))
        if len(sessions) >= MAX_SESSION_STATES:
            log_error("session state limit reached; ignoring new session")
            return None
        entry = {"trace_id": None, "turn": None}
        sessions[sid] = entry
    else:
        sessions.pop(sid)
        sessions[sid] = entry
    return entry


# ---------------------------------------------------------------- emission
def tool_span(
    p: dict,
    sid: str | None,
    trace_id: str,
    parent: str | None,
    attribution: str,
    reason: str,
    response_event: dict | None,
    end_ns: int,
) -> dict:
    name = p["name"]
    attrs = [
        attr("session.id", sid or "unknown"),
        attr("gen_ai.conversation.id", sid or "unknown"),
        attr("gen_ai.operation.name", "execute_tool"),
        attr("gen_ai.tool.name", name),
        attr("tool.name", name),
        attr("crew.attribution", attribution),
        attr("crew.pre_events", p.get("dup", 1), "intValue"),
        attr("crew.end_reason", reason),
    ]
    attrs += session_attributes(p.get("session_key"), p.get("parent_session_key"))
    for key, attr_key in (
        ("tool_call_id", "gen_ai.tool.call.id"),
        ("workflow_run_id", "workflow.run_id"),
        ("agent_id", "workflow.agent_id"),
        ("acp_session_id", "acp.session_id"),
        ("turn_id", "crew.turn_id"),
        ("event_id", "crew.event.id"),
        ("result_event_id", "crew.result_event.id"),
    ):
        if p.get(key):
            attrs.append(attr(attr_key, p[key]))
    if isinstance(p.get("input"), str) and p["input"]:
        attrs += [
            attr("tool.input", p["input"]),
            attr("gen_ai.tool.call.arguments", p["input"]),
            attr("tool.input.truncated", p.get("input_truncated", False), "boolValue"),
        ]
    if response_event is not None:
        out, truncated = safe_text(
            tool_output(response_event) if tool_output(response_event) is not None else ""
        )
        success, status = outcome(response_event)
        attrs += [
            attr("tool.output", out),
            attr("gen_ai.tool.call.result", out),
            attr("tool.output.truncated", truncated, "boolValue"),
            attr("tool.success", success),
            attr("tool.outcome", status),
            attr("tool.result.available", bool(out), "boolValue"),
        ]
    else:
        attrs += [
            attr("tool.success", "unknown"),
            attr("tool.outcome", "unknown"),
            attr("tool.result.available", False, "boolValue"),
        ]
    if p.get("agent_role"):
        attrs.append(attr("gen_ai.agent.name", p["agent_role"]))
    return span(
        trace_id, p["span_id"], f"execute_tool {name}", p["start_ns"], end_ns, attrs, parent, sid
    )


def emit_tool(
    state: dict, p: dict, response_event: dict | None, end_ns: int, spans: list[dict], reason: str
) -> None:
    sid = p.get("session")
    attribution = "payload" if sid else "none"
    turn = (state["sessions"].get(sid) or {}).get("turn") if sid else None
    if turn is None:
        # Preserve unmatched calls with explicit identities for the workflow
        # converter; it must never recover ownership from timestamps.
        record = {
            k: p.get(k)
            for k in (
                "id",
                "start_ns",
                "name",
                "input",
                "input_truncated",
                "dup",
                "event_id",
                "tool_call_id",
                "workflow_run_id",
                "agent_id",
                "acp_session_id",
                "turn_id",
                "result_event_id",
                "event_ids",
            )
        }
        record.update(end_ns=end_ns, reason=reason, session=sid)
        if response_event is not None:
            out, _ = safe_text(tool_output(response_event) or "")
            record["result"] = out
        append_jsonl("orphan-tools.jsonl", record)
        if ORPHANS != "emit":
            return
        spans.append(
            tool_span(
                p, sid, secrets.token_hex(16), None, attribution, reason, response_event, end_ns
            )
        )
        return
    parent = turn["span_id"]
    agent = p.get("agent_id")
    if agent and agent in turn["agents"]:
        parent = turn["agents"][agent]["span_id"]
        turn["agents"][agent]["last_ns"] = end_ns
    spans.append(
        tool_span(p, sid, turn["trace_id"], parent, attribution, reason, response_event, end_ns)
    )


RUNS_DIR = Path(os.environ.get("KIRO_OTEL_RUNS_DIR", Path.home() / "workflows/runs"))


def workflow_command(run: dict) -> str:
    """The request that started a workflow run, rebuilt from its record: /workflow <name> <args>."""
    a = run.get("args")
    vals = " ".join(str(v) for v in a.values()) if isinstance(a, dict) else str(a or "")
    return f"/workflow {run.get('name', '')} {vals}".strip()


def find_run(run_id: str) -> dict | None:
    """Find a workflow record only by its explicit run ID."""
    try:
        files = RUNS_DIR.glob("wf_*.json")
    except OSError:
        return None
    for f in files:
        try:
            run = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if run.get("run_id") == run_id:
            return run
    return None


def close_turn(
    state: dict,
    sid: str,
    now: int,
    spans: list[dict],
    assistant: str,
    assistant_truncated: bool,
    how: str,
) -> None:
    sess = state["sessions"].get(sid)
    turn = sess.get("turn") if sess else None
    if not turn:
        return
    remaining = [p for p in state["pending"] if p.get("session") == sid]
    for p in remaining:
        emit_tool(state, p, None, now, spans, "flushed_at_" + how)
    state["pending"] = [p for p in state["pending"] if p not in remaining]
    for key, agent in turn["agents"].items():
        spans.append(
            span(
                turn["trace_id"],
                agent["span_id"],
                "invoke_agent " + (agent.get("role") or key),
                agent["start_ns"],
                agent.get("last_ns", now),
                [
                    attr("session.id", sid),
                    attr("gen_ai.conversation.id", sid),
                    attr("gen_ai.operation.name", "invoke_agent"),
                    attr("gen_ai.agent.name", agent.get("role") or key),
                    attr("crew.subagent_id", key),
                    attr("crew.agent.lifecycle_events", False, "boolValue"),
                ]
                + session_attributes(turn.get("session_key"), turn.get("parent_session_key")),
                turn["span_id"],
                sid,
            )
        )
    attrs = [
        attr("session.id", sid),
        attr("gen_ai.conversation.id", sid),
        attr("gen_ai.operation.name", "invoke_agent"),
        attr("gen_ai.task.input", turn["prompt"]),
        attr("agentcore.invocation.user_prompt", turn["prompt"]),
        attr("gen_ai.task.input.truncated", turn.get("prompt_truncated", False), "boolValue"),
        attr("gen_ai.task.output", assistant),
        attr("agentcore.invocation.agent_response", assistant),
        attr("gen_ai.task.output.truncated", assistant_truncated, "boolValue"),
        attr("crew.turn.closed_by", how),
        attr("crew.trace_scope", TRACE_SCOPE),
    ]
    attrs += session_attributes(turn.get("session_key"), turn.get("parent_session_key"))
    if turn.get("workflow_run_id"):
        attrs.append(attr("workflow.run_id", turn["workflow_run_id"]))
    if turn.get("turn_id"):
        attrs.append(attr("crew.turn_id", turn["turn_id"]))
    m = re.match(r"\[Workflow `([^`]+)` finished", turn["prompt"] or "")
    wf_name = None
    if m:
        # Crew's own message after a workflow run, not something the user typed. The real request is the /workflow
        # command, which fires no prompt hook, so it comes from the run record. The wrapper text is kept as an attribute.
        wf_name = m.group(1)
        attrs += [
            attr("crew.turn.synthetic", True, "boolValue"),
            attr("crew.workflow.name", wf_name),
            attr("crew.turn.wrapper_prompt", turn["prompt"]),
        ]
        run_id = turn.get("workflow_run_id")
        run = find_run(run_id) if run_id else None
        if run:
            cmd, ctr = safe_text(workflow_command(run))
            attrs = [
                x
                for x in attrs
                if x["key"]
                not in (
                    "gen_ai.task.input",
                    "agentcore.invocation.user_prompt",
                    "gen_ai.task.input.truncated",
                )
            ]
            attrs += [
                attr("gen_ai.task.input", cmd),
                attr("agentcore.invocation.user_prompt", cmd),
                attr("gen_ai.task.input.truncated", ctr, "boolValue"),
                attr("crew.workflow.run_id", run.get("run_id", "")),
            ]
    spans.append(
        span(
            turn["trace_id"],
            turn["span_id"],
            "invoke_agent",
            turn["start_ns"],
            now,
            attrs,
            None,
            sid,
        )
    )
    spans.append(
        span(
            turn["trace_id"],
            secrets.token_hex(8),
            "turn.complete",
            now,
            now + 10_000_000,
            [attr("session.id", sid)]
            + session_attributes(turn.get("session_key"), turn.get("parent_session_key")),
            turn["span_id"],
            sid,
        )
    )
    append_jsonl(
        "turns.jsonl",
        {
            "session": sid,
            "session_key": turn.get("session_key"),
            "parent_session_key": turn.get("parent_session_key"),
            "trace_id": turn["trace_id"],
            "span_id": turn["span_id"],
            "start_ns": turn["start_ns"],
            "end_ns": now,
            "workflow": wf_name,
            "workflow_run_id": turn.get("workflow_run_id"),
            "turn_id": turn.get("turn_id"),
        },
    )
    sess["turn"] = None


# ---------------------------------------------------------------- events
def handle(event: dict, now: int) -> list[dict]:
    spans: list[dict] = []
    state = load_state()
    prune_sessions(state, MAX_SESSION_STATES, migrate_over_limit=True)
    name = re.sub(r"[^a-z]", "", str(get(event, "hook_event_name", "trigger", default="")).lower())
    sid, own_session_key, parent_session_key, _legacy_session_id = session_identity(event)
    subagent = get(event, "agent_id", "agentId", "subagent_id", "subagentId")
    role = get(event, "agent_role", "agentRole")
    event_id = get(event, "event_id", "eventId")
    is_tool_event = name in {"pretooluse", "posttooluse"}
    if is_tool_event and (not isinstance(event_id, str) or not event_id):
        quarantine_event(event, "event_id")
        save_state(state)
        return spans
    if is_tool_event and not isinstance(get(event, "tool_call_id", "toolCallId"), str):
        quarantine_event(event, "tool_call_id")
        save_state(state)
        return spans

    # Suppress duplicates inside a fixed 10,000-distinct-ID window. IDs evicted
    # beyond that window may be accepted as new deliveries; never infer replay
    # identity from timestamps, names, or content.
    seen = state.setdefault("seen_event_ids", [])
    signatures = state.setdefault("seen_event_signatures", {})
    signature = {
        "hook_event_name": get(event, "hook_event_name", "trigger"),
        "tool_call_id": get(event, "tool_call_id", "toolCallId"),
        "parent_session_key": get(event, "parent_session_key", "parentSessionKey"),
        "session_key": get(event, "session_key", "sessionKey"),
        "evaluation_session_id": get(event, "evaluation_session_id", "evaluationSessionId"),
        "workflow_run_id": get(event, "workflow_run_id", "workflowRunId"),
        "agent_id": get(event, "agent_id", "agentId"),
        "acp_session_id": get(event, "acp_session_id", "acpSessionId"),
        "turn_id": get(event, "turn_id", "turnId"),
    }
    if event_id and event_id in seen:
        previous_signature = signatures.get(event_id)
        if previous_signature is not None and previous_signature != signature:
            quarantine_event(event, "conflicting_event_identity")
        save_state(state)
        return []
    if event_id:
        seen.append(event_id)
        signatures[event_id] = signature
        expired = seen[:-MAX_SEEN_EVENT_IDS]
        del seen[:-MAX_SEEN_EVENT_IDS]
        for old_event_id in expired:
            signatures.pop(old_event_id, None)

    if sid and sid in state["sessions"]:
        state["sessions"][sid] = state["sessions"].pop(sid)

    if is_tool_event and not get(event, "tool_call_id", "toolCallId"):
        quarantine_event(event, "tool_call_id")
        save_state(state)
        return spans

    # Discard pending state from pre-contract versions. It has no producer ID,
    # so no later event can be paired safely.
    legacy_pending = [
        p for p in state["pending"] if not p.get("tool_call_id") or not p.get("correlation_key")
    ]
    for p in legacy_pending:
        log_error(f"discarding legacy pending tool without correlation IDs: {p.get('id')!r}")
    state["pending"] = [p for p in state["pending"] if p not in legacy_pending]

    if name in {"agentspawn", "sessionstart"}:
        if sid:
            if session_entry(state, sid) is None:
                forget_event_identity(state, event_id)
                save_state(state)
                return spans

    elif name in {"userpromptsubmit", "promptsubmit"}:
        sid = sid or "unknown"
        sess = session_entry(state, sid)
        if sess is None:
            forget_event_identity(state, event_id)
            save_state(state)
            return spans
        if sess.get("turn"):
            close_turn(state, sid, now, spans, "", False, "next_prompt")
        prompt, truncated = safe_text(get(event, "prompt", "user_prompt", default=""))
        if TRACE_SCOPE == "session":
            sess["trace_id"] = sess.get("trace_id") or secrets.token_hex(16)
            trace_id = sess["trace_id"]
        else:
            trace_id = secrets.token_hex(16)
        turn = {
            "trace_id": trace_id,
            "span_id": secrets.token_hex(8),
            "start_ns": now,
            "prompt": prompt,
            "prompt_truncated": truncated,
            "agents": {},
            "session_key": own_session_key,
            "parent_session_key": parent_session_key,
            "workflow_run_id": get(event, "workflow_run_id", "workflowRunId"),
            "evaluation_session_id": get(event, "evaluation_session_id", "evaluationSessionId"),
            "turn_id": get(event, "turn_id", "turnId"),
        }
        sess["turn"] = turn
        spans.append(
            span(
                trace_id,
                secrets.token_hex(8),
                "user.prompt",
                now,
                now + 10_000_000,
                [
                    attr("session.id", sid),
                    attr("gen_ai.conversation.id", sid),
                    attr("prompt.preview", prompt),
                    attr("prompt.preview.truncated", truncated, "boolValue"),
                ]
                + session_attributes(own_session_key, parent_session_key),
                turn["span_id"],
                sid,
            )
        )

    elif name == "pretooluse":
        tool_input = get(event, "tool_input", "toolInput")
        tname = norm_name(
            get(event, "tool_name", "toolName"),
            tool_input if isinstance(tool_input, dict) else None,
        )
        text, truncated = safe_text(tool_input) if tool_input is not None else ("", False)
        key = correlation_key(event)
        merged = next((p for p in state["pending"] if p.get("correlation_key") == key), None)
        if merged is not None:
            merged["dup"] = merged.get("dup", 1) + 1
            merged_events = merged.setdefault("event_ids", [])
            if len(merged_events) < 32:
                merged_events.append(event_id)
        else:
            p = {
                "id": secrets.token_hex(6),
                "correlation_key": key,
                "start_ns": now,
                "name": tname,
                "input": text,
                "input_truncated": truncated,
                "session": sid,
                "session_key": own_session_key,
                "parent_session_key": parent_session_key,
                "dup": 1,
                "span_id": secrets.token_hex(8),
                "agent_id": subagent,
                "agent_role": role,
                "event_id": event_id,
                "event_ids": [event_id],
                "tool_call_id": get(event, "tool_call_id", "toolCallId"),
                "workflow_run_id": get(event, "workflow_run_id", "workflowRunId"),
                "acp_session_id": get(event, "acp_session_id", "acpSessionId"),
                "turn_id": get(event, "turn_id", "turnId"),
            }
            state["pending"].append(p)
            if len(state["pending"]) > MAX_PENDING_CALLS:
                dropped = state["pending"].pop(0)
                log_error(
                    "pending tool-call limit reached; retained record without result "
                    f"tool_call_id={dropped.get('tool_call_id')!r}"
                )
        if subagent:
            turn = (state["sessions"].get(sid) or {}).get("turn") if sid else None
            if turn is not None:
                agents = turn["agents"]
                if subagent in agents:
                    agents[subagent]["last_ns"] = now
                elif len(agents) < MAX_AGENTS_PER_TURN:
                    agents[subagent] = {
                        "span_id": secrets.token_hex(8),
                        "role": role,
                        "start_ns": now,
                        "last_ns": now,
                    }
                elif not turn.get("agent_limit_logged"):
                    log_error("per-turn agent span limit reached; overflow tools use the turn root")
                    turn["agent_limit_logged"] = True

    elif name == "posttooluse":
        key = correlation_key(event)
        pick = next((p for p in state["pending"] if p.get("correlation_key") == key), None)
        if pick is not None:
            pick["result_event_id"] = event_id
            state["pending"].remove(pick)
            emit_tool(state, pick, event, now, spans, "post_tool_use")
        else:
            quarantine_event(event, "matching_pre_tool_event")

    elif name in {"stop", "agentstop"}:
        if sid:
            text, truncated = safe_text(
                get(event, "assistant_text", "assistant_response", default="")
            )
            close_turn(state, sid, now, spans, text, truncated, "stop")

    save_state(state)
    return spans


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        event = json.loads(raw)
        if not isinstance(event, dict):
            return 0
        import fcntl

        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        with (STATE_ROOT / ".lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                spans = handle(event, int(os.environ.get("KIRO_OTEL_NOW_NS") or time.time_ns()))
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        if HOOK_TOOLS == "off":
            spans = [s for s in spans if not s["name"].startswith("execute_tool")]
        elif HOOK_TOOLS == "chat":
            # In ACP mode, hooks own ordinary dashboard/chat tools while ACP
            # remains authoritative for workflow-agent tools. Hook calls carry
            # their explicit IDs into the workflow journal.
            spans = [
                s
                for s in spans
                if not s["name"].startswith("execute_tool")
                or any(
                    a.get("key") == "crew.attribution"
                    and a.get("value", {}).get("stringValue") == "payload"
                    for a in s.get("attributes", [])
                )
            ]
        export(spans)
        if ACP_CONVERTER and str(event.get("hook_event_name", "")).lower() in {"stop", "agentstop"}:
            # Detached, so the hook still returns at once. The delay lets the adapter's last frames reach the tap's log.
            subprocess.Popen(
                ["sh", "-c", 'sleep "$1"; exec python3 "$2"', "sh", ACP_DELAY, ACP_CONVERTER],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as exc:  # telemetry must never block Crew
        log_error(f"hook failed: {exc!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
