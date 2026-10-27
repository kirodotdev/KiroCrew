#!/usr/bin/env python3
"""Convert optional ACP tap frames into AgentCore-shaped chat/workflow spans.

  invoke_agent   one per completed ACP prompt (a turn): stop reason, assistant text, token usage
    chat         one aggregate model span per turn, with token usage (not per model request)
    execute_tool one per toolCallId: name, arguments, output, exit code, success, status

Workflow agents: a finished Crew workflow run record (read from
KIRO_OTEL_WORKFLOW_RUNS_DIR) is joined to ACP sessions and hook records by
explicit workflow, agent, session, turn, and tool-call IDs, giving workflow ->
phase -> invoke_agent <agent label> -> execute_tool. Timestamps set span bounds;
they do not decide attribution.

The prompt text is never in the frames (one text block mixes the user's request with Crew's wrapper). The
turn's input comes from Crew's UserPromptSubmit hook, so spans are marked gen_ai.task.input.available=false.
Use one source for tool spans, this or the hook runner, or the traces will duplicate.

Usage: acp-to-spans.py [--frames FILE] [--since MINUTES] [--service NAME] [--all-sessions] [--force]
Env:   KIRO_OTEL_ENDPOINT, KIRO_OTEL_DRY_RUN_FILE (see hook-runner-v2), KIRO_OTEL_STATE_DIR
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import importlib.util
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "hookv2", HERE.parent / "hook-runner-v2" / "otel-crew-hook-v2.py"
)
assert _spec and _spec.loader
v2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v2)
v2.SERVICE_NAME = os.environ.get("KIRO_OTEL_SERVICE_NAME", "kiro-linux-acp")
attr, span, safe_text = v2.attr, v2.span, v2.safe_text

home = Path.home()
MAX_EMITTED_TURNS = 5_000
MAX_EMITTED_RUNS = 1_000
# Keep the newest N exact IDs per ledger. Duplicates older than this window may
# be re-emitted; unseen events are never discarded due to timestamp ordering.
# --force bypasses retained-ID suppression for operator-requested re-export.
_REPLAY_LIMITS = {"emitted": MAX_EMITTED_TURNS, "runs": MAX_EMITTED_RUNS}
args = None
STATE = None
state = {"emitted": [], "runs": []}


def make_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", default=str(home / ".kiro/otel-traces/acp-frames.jsonl"))
    parser.add_argument(
        "--prompts",
        default=str(home / ".kiro/otel-traces/acp-prompts.jsonl"),
        help="prompt text recorded by the tap with ACP_TAP_PROMPTS=1",
    )
    parser.add_argument("--since", type=float, default=240)
    parser.add_argument(
        "--session-map",
        default=str(Path(os.environ.get("KIROCREW_HOME") or home) / "session_map.json"),
    )
    parser.add_argument(
        "--runs-dir",
        default=os.environ.get("KIRO_OTEL_WORKFLOW_RUNS_DIR", str(home / "workflows/runs")),
    )
    parser.add_argument("--state", default=None)
    parser.add_argument(
        "--all-sessions",
        action="store_true",
        help="also emit housekeeping sessions (title generation and similar)",
    )
    parser.add_argument(
        "--grace",
        type=float,
        default=60,
        help="seconds to wait for the v2 turn root before emitting a standalone turn",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-emit what the state file says was already emitted"
    )
    return parser


def load_frames():
    p = Path(args.frames)
    if not p.exists():
        sys.exit(f"no frames at {p}")
    cut = (time.time() - args.since * 60) * 1e9
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return [r for r in rows if int(r["ts_ns"]) >= cut]


def drop_replay(rows):
    """Frames between session/load and its response are replayed history, not live work."""
    replay, open_ = set(), {}
    for i, r in enumerate(rows):
        if r.get("method") == "session/load" and r["dir"] == "client_to_agent":
            open_[(r["pid"], r["id"])] = i
        elif r["kind"] == "response" and (r["pid"], r.get("id")) in open_:
            replay.update(range(open_.pop((r["pid"], r["id"])), i + 1))
    return [r for i, r in enumerate(rows) if i not in replay]


def resolve_session_key(event, session_map=None):
    """Resolve root parent, own session, legacy ID, then old ACP-map fallback."""
    effective, _, _, _ = v2.session_identity(event)
    if effective:
        return effective
    session_map = session_map or {}
    acp_session_id = event.get("sid")
    return session_map.get(acp_session_id) if acp_session_id else None


def producer_session_attributes(event):
    _, own, parent, _ = v2.session_identity(event)
    return v2.session_attributes(own, parent)


def replay_is_suppressed(state, ledger, identity, force=False, known_ids=None):
    """Suppress only exact IDs still present in the bounded replay window."""
    if force:
        return False
    if ledger not in _REPLAY_LIMITS:
        raise ValueError(f"unknown replay ledger: {ledger}")
    identities = known_ids if known_ids is not None else state.setdefault(ledger, [])
    return identity in identities


def prune_replay_ledger(state, ledger, limit=None):
    """Retain only the newest exact IDs and discard earlier schema fields."""
    if ledger not in _REPLAY_LIMITS:
        raise ValueError(f"unknown replay ledger: {ledger}")
    maximum = _REPLAY_LIMITS[ledger] if limit is None else max(1, int(limit))
    identities = state.setdefault(ledger, [])
    state[ledger] = identities[-maximum:]
    state.pop(f"{ledger}_timestamps", None)
    state.pop(f"{ledger}_through_ns", None)


def remember_replay_id(state, ledger, identity, limit=None):
    """Move an exact ID to the newest position in the bounded replay window."""
    if ledger not in _REPLAY_LIMITS:
        raise ValueError(f"unknown replay ledger: {ledger}")
    maximum = _REPLAY_LIMITS[ledger] if limit is None else max(1, int(limit))
    identities = state.setdefault(ledger, [])
    if identity in identities:
        identities.remove(identity)
    identities.append(identity)
    prune_replay_ledger(state, ledger, maximum)


def mark_workflow_turns_used(run, turns, used):
    """Keep exact run/agent/session workflow turns from falling into chat export."""
    run_id = run.get("run_id")
    if not run_id:
        return
    agent_ids = {
        event.get("data", {}).get("agent_id")
        for event in run.get("events", [])
        if event.get("type") == "agent_started"
    }
    sessions = {
        event.get("data", {}).get("agent_id"): event.get("data", {}).get("acp_session_id")
        for event in run.get("events", [])
        if event.get("type") == "agent_session_bound"
    }
    for turn in turns:
        agent_id = turn.get("agent_id")
        expected_session = sessions.get(agent_id)
        if (
            turn.get("workflow_run_id") == run_id
            and agent_id in agent_ids
            and (not expected_session or turn.get("sid") == expected_session)
        ):
            used.add(id(turn))


def build_turns(rows, include_incomplete=False):
    """One turn per completed session/prompt: its frames between request and response."""
    turns, open_ = [], {}
    for r in rows:
        sid = r.get("ids", {}).get("sessionId")
        if r.get("method") == "session/prompt" and r["dir"] == "client_to_agent" and sid:
            ids = r.get("ids", {})
            open_[(r["pid"], r["id"])] = {
                "sid": sid,
                "pid": r["pid"],
                "request_id": r["id"],
                "workflow_run_id": ids.get("workflow_run_id"),
                "agent_id": ids.get("agent_id"),
                "session_key": ids.get("session_key") or ids.get("sessionKey"),
                "parent_session_key": ids.get("parent_session_key") or ids.get("parentSessionKey"),
                "session_id": ids.get("session_id"),
                "turn_id": ids.get("turn_id"),
                "hook_trace_id": ids.get("hook_trace_id"),
                "hook_span_id": ids.get("hook_span_id"),
                "start": int(r["ts_ns"]),
                "frames": [],
                "usage": {},
            }
        elif (
            r["kind"] == "response"
            and r["dir"] == "agent_to_client"
            and (r["pid"], r.get("id")) in open_
        ):
            t = open_.pop((r["pid"], r["id"]))
            t["end"] = int(r["ts_ns"])
            t["stop"] = r.get("ids", {}).get("stopReason")
            t["usage"] = r.get("nums", {})
            t["error"] = r.get("error")
            t["kiro"] = {k: v for k, v in t.get("kiro", {}).items() if v}
            turns.append(t)
        else:
            ids = r.get("ids", {})
            matched = []
            for t in open_.values():
                exact_turn = t.get("turn_id") and ids.get("turn_id") == t["turn_id"]
                exact_workflow_agent = (
                    t.get("workflow_run_id")
                    and t.get("agent_id")
                    and ids.get("workflow_run_id") == t["workflow_run_id"]
                    and ids.get("agent_id") == t["agent_id"]
                )
                if exact_turn or exact_workflow_agent:
                    t["frames"].append(r)
                    matched.append(t)
                    if r.get("method") == "_kiro.dev/metadata" and r.get(
                        "nums"
                    ):  # Kiro CLI reports credits and duration, not tokens
                        k = t.setdefault("kiro", {})
                        k["credits"] = k.get("credits", 0) + r["nums"].get("meteringCredits", 0)
                        k["duration_ms"] = max(
                            k.get("duration_ms", 0), r["nums"].get("turnDurationMs", 0)
                        )
            if matched:
                continue

            # Ordinary ACP clients often identify notifications only by process
            # and session. Associate those frames only when exactly one
            # metadata-free prompt for that session is in flight. Explicit
            # correlation metadata remains authoritative, and ambiguous frames
            # stay unassigned instead of being guessed by time or tool shape.
            correlation_keys = ("turn_id", "workflow_run_id", "agent_id")
            if any(ids.get(key) for key in correlation_keys):
                continue
            candidates = [
                t for t in open_.values()
                if t["pid"] == r["pid"]
                and t["sid"] == ids.get("sessionId")
                and not any(t.get(key) for key in correlation_keys)
            ]
            if len(candidates) == 1:
                candidates[0]["frames"].append(r)
    if include_incomplete:
        for turn in open_.values():
            turn["incomplete"] = True
            turn["end"] = max([turn["start"], *[int(r["ts_ns"]) for r in turn["frames"]]])
            turn["stop"] = "incomplete"
            turns.append(turn)
    return turns


def attach_prompts(turns):
    """Attach prompt text using the exact JSON-RPC request ID and process."""
    rows = []
    try:
        for line in Path(args.prompts).read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        return
    by_request = {
        (r.get("pid"), str(r.get("requestId"))): r
        for r in rows
        if r.get("requestId") is not None and r.get("marker") and r.get("text")
    }
    for t in turns:
        prompt = by_request.get((t["pid"], str(t.get("request_id"))))
        if prompt:
            t["prompt"] = prompt["text"]


def kiro_result(raw):
    """Kiro CLI shell output: {"items":[{"Json":{"exit_status":"exit status: 1","stdout":...,"stderr":...}}]}. None for any other shape."""
    try:
        items = json.loads(raw).get("items")
        j = items[0]["Json"]
        if "exit_status" not in j and "stdout" not in j:
            return None
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        return None
    m = re.search(r"-?\d+", str(j.get("exit_status", "")))
    text = (j.get("stdout") or "") + (("\n[stderr] " + j["stderr"]) if j.get("stderr") else "")
    return {"text": text, "exit_code": int(m.group()) if m else None}


def tool_calls(frames):
    calls = collections.OrderedDict()
    perms = collections.Counter()
    for r in frames:
        tid = r.get("ids", {}).get("toolCallId")
        if not tid:
            continue
        if r.get("method") == "session/request_permission":
            perms[tid] += 1
            continue
        if r.get("update") not in ("tool_call", "tool_call_update"):
            continue
        c = calls.setdefault(
            tid, {"id": tid, "frames": [], "out": [], "input": None, "result": None}
        )
        c["frames"].append(r)
        ids, ct = r.get("ids", {}), r.get("content") or {}
        if ids.get("status"):
            c["status"] = ids["status"]
        for k in ("name", "kind", "server", "tool", "exit_code", "signal"):
            if ids.get(k) not in (None, ""):
                c[k] = ids[k]
        if ct.get("input") and c["input"] is None:
            c["input"] = ct["input"]
        if ct.get("title"):
            c["title"] = ct["title"]
        if ct.get("output_delta"):
            c["out"].append(ct["output_delta"])
        if ct.get("output") and c["result"] is None:
            c["result"] = ct["output"]
            kr = kiro_result(ct["output"])
            if kr:
                c["result"] = kr["text"]
                if kr.get("exit_code") is not None and "exit_code" not in c:
                    c["exit_code"] = kr["exit_code"]
        if (
            "update.rawOutput.error" in r.get("fields", {})
            and r["fields"]["update.rawOutput.error"] != "null"
        ):
            c["error"] = True
    for tid, c in calls.items():
        c["perms"] = perms.get(tid, 0)
    return list(calls.values())


def assistant_text(frames):
    msgs = collections.OrderedDict()
    for r in frames:
        if r.get("update") == "agent_message_chunk":
            mid = r.get("ids", {}).get("messageId", "?")
            t = (r.get("content") or {}).get("text")
            if t:
                msgs[mid] = msgs.get(mid, "") + t
    return (list(msgs.values())[-1] if msgs else None), len(msgs)


def tool_span(c, sid_key, trace_id, parent):
    name = (
        f"mcp.{c['server']}.{c['tool']}"
        if c.get("server") and c.get("tool")
        else (c.get("name") or c.get("kind") or "tool")
    )
    out = (
        "".join(c["out"]) if c["out"] else c["result"]
    )  # the terminal output stream is the real output; rawOutput is the fallback
    start = int(c["frames"][0]["ts_ns"])
    end = max(start, int(c.get("end_ns") or c["frames"][-1]["ts_ns"]))
    status = c.get("status", "unknown")
    exit_code = c.get("exit_code")
    if exit_code is not None:
        success = "true" if str(exit_code) == "0" else "false"
    elif status == "completed" and not c.get("error"):
        success = "true"
    elif status in {"failed", "cancelled"} or c.get("error"):
        success = "false"
    else:
        success = "unknown"
    attrs = [
        attr("session.id", sid_key),
        attr("gen_ai.conversation.id", sid_key),
        attr("gen_ai.operation.name", "execute_tool"),
        attr("gen_ai.tool.name", name),
        attr("tool.name", name),
        attr("gen_ai.tool.call.id", c["id"]),
        attr("tool.status", status),
        attr("tool.success", success),
        attr("crew.source", "acp_tap"),
        attr("crew.permission_requests", c.get("perms", 0), "intValue"),
    ]
    if c.get("session_key"):
        attrs.append(attr("crew.session_key", c["session_key"]))
    if c.get("parent_session_key"):
        attrs.append(attr("crew.parent_session_key", c["parent_session_key"]))
    if c.get("workflow_run_id"):
        attrs.append(attr("workflow.run_id", c["workflow_run_id"]))
    if c.get("agent_id"):
        attrs.append(attr("workflow.agent_id", c["agent_id"]))
    if c.get("workflow_attribution"):
        attrs.append(attr("crew.attribution", c["workflow_attribution"]))
    if c.get("partial"):
        attrs.append(attr("tool.partial", True, "boolValue"))
    if exit_code is not None:
        attrs.append(attr("tool.exit_code", exit_code))
    if c["input"]:
        text, trunc = safe_text(c["input"])
        attrs += [
            attr("tool.input", text),
            attr("gen_ai.tool.call.arguments", text),
            attr("tool.input.truncated", trunc, "boolValue"),
        ]
    if out is not None:
        text, trunc = safe_text(out)
        attrs += [
            attr("tool.output", text),
            attr("gen_ai.tool.call.result", text),
            attr("tool.output.truncated", trunc, "boolValue"),
        ]
    attrs.append(attr("tool.result.available", bool(out), "boolValue"))
    return span(
        trace_id,
        secrets.token_hex(8),
        f"execute_tool {name}",
        start,
        end,
        attrs,
        parent,
        sid_key,
        2 if success == "false" else 0,
    )


USAGE = {
    "inputTokens": "gen_ai.usage.input_tokens",
    "outputTokens": "gen_ai.usage.output_tokens",
    "cachedInputTokens": "gen_ai.usage.cached_input_tokens",
    "cachedReadTokens": "gen_ai.usage.cache_read_tokens",
    "reasoningOutputTokens": "gen_ai.usage.reasoning_tokens",
    "totalTokens": "gen_ai.usage.total_tokens",
}


def turn_spans(
    t,
    sid_key,
    trace_id,
    parent,
    agent_name=None,
    name="invoke_agent",
    root=None,
    include_tool_spans=True,
):
    """root: span ID of an existing v2 turn root. Then no invoke_agent span is made; chat and tools hang off the root."""
    text, n_msgs = assistant_text(t["frames"])
    root_id = root or secrets.token_hex(8)
    attrs = [
        attr("session.id", sid_key),
        attr("gen_ai.conversation.id", sid_key),
        attr("gen_ai.operation.name", "invoke_agent"),
        attr("gen_ai.task.input.available", bool(t.get("prompt")), "boolValue"),
        attr("crew.source", "acp_tap"),
        attr("gen_ai.response.finish_reasons", t.get("stop") or "unknown"),
        attr("crew.assistant_messages", n_msgs, "intValue"),
        attr("acp.session_id", t["sid"]),
    ]
    if t.get("workflow_run_id"):
        attrs.append(attr("workflow.run_id", t["workflow_run_id"]))
    if t.get("agent_id"):
        attrs.append(attr("workflow.agent_id", t["agent_id"]))
    if t.get("turn_id"):
        attrs.append(attr("crew.turn_id", t["turn_id"]))
    if t.get("session_key"):
        attrs.append(attr("crew.session_key", t["session_key"]))
    if t.get("parent_session_key"):
        attrs.append(attr("crew.parent_session_key", t["parent_session_key"]))
    if t.get("session_id"):
        attrs.append(attr("crew.legacy_session_id", t["session_id"]))
    if t.get("workflow_attribution"):
        attrs.append(attr("crew.attribution", t["workflow_attribution"]))
    if t.get("prompt"):
        pin, ptr = safe_text(t["prompt"])
        attrs += [
            attr("gen_ai.task.input", pin),
            attr("gen_ai.task.input.truncated", ptr, "boolValue"),
        ]
    if text is not None:
        out, trunc = safe_text(text)
        attrs += [
            attr("gen_ai.task.output", out),
            attr("gen_ai.task.output.truncated", trunc, "boolValue"),
        ]
    if agent_name:
        attrs.append(attr("gen_ai.agent.name", agent_name))
    for k, ak in USAGE.items():
        if k in t["usage"]:
            attrs.append(attr(ak, int(t["usage"][k]), "intValue"))
    out_spans = (
        []
        if root
        else [
            span(
                trace_id,
                root_id,
                name,
                t["start"],
                t["end"],
                attrs,
                parent,
                sid_key,
                2 if t.get("error") else 0,
            )
        ]
    )
    if t["usage"]:
        cattrs = [
            attr("session.id", sid_key),
            attr("gen_ai.operation.name", "chat"),
            attr("crew.chat.aggregate", True, "boolValue"),
            attr("crew.source", "acp_tap"),
        ] + [attr(ak, int(t["usage"][k]), "intValue") for k, ak in USAGE.items() if k in t["usage"]]
        cattrs += producer_session_attributes(t)
        if t.get("workflow_run_id"):
            cattrs.append(attr("workflow.run_id", t["workflow_run_id"]))
        if t.get("agent_id"):
            cattrs.append(attr("workflow.agent_id", t["agent_id"]))
        out_spans.append(
            span(
                trace_id,
                secrets.token_hex(8),
                "chat",
                t["start"],
                t["end"],
                cattrs,
                root_id,
                sid_key,
            )
        )
    if t.get("kiro") and not t["usage"]:
        k = t["kiro"]
        kattrs = [
            attr("session.id", sid_key),
            attr("gen_ai.operation.name", "chat"),
            attr("crew.chat.aggregate", True, "boolValue"),
            attr("crew.source", "acp_tap"),
            attr("kiro.credits", float(k.get("credits", 0)), "doubleValue"),
            attr("kiro.turn_duration_ms", int(k.get("duration_ms", 0)), "intValue"),
        ]
        kattrs += producer_session_attributes(t)
        if t.get("workflow_run_id"):
            kattrs.append(attr("workflow.run_id", t["workflow_run_id"]))
        if t.get("agent_id"):
            kattrs.append(attr("workflow.agent_id", t["agent_id"]))
        out_spans.append(
            span(
                trace_id,
                secrets.token_hex(8),
                "chat",
                t["start"],
                t["end"],
                kattrs,
                root_id,
                sid_key,
            )
        )
    if include_tool_spans:
        for c in tool_calls(t["frames"]):
            c["workflow_run_id"] = t.get("workflow_run_id")
            c["agent_id"] = t.get("agent_id")
            c["workflow_attribution"] = t.get("workflow_attribution")
            c["session_key"] = t.get("session_key")
            c["parent_session_key"] = t.get("parent_session_key")
            if t.get("incomplete") and c.get("status") not in {"completed", "failed"}:
                c["status"] = "cancelled" if t.get("stop") == "cancelled" else "failed"
                c["partial"] = True
                c["end_ns"] = t["end"]
            out_spans.append(tool_span(c, sid_key, trace_id, root_id))
    return out_spans


def iso_ns(iso):
    return int(datetime.datetime.fromisoformat(iso).timestamp() * 1e9)


def wf_io(run, nested, outcome="finished"):
    """Convert workflow input/result fields, omitting output when no successful result exists."""
    inp, itr = safe_text(v2.workflow_command(run))
    res = run.get("result") if run.get("result") is not None else run.get("error")
    out, otr = safe_text(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False))
    if nested:
        attrs = [attr("workflow.input", inp)]
        if outcome == "finished":
            attrs.append(attr("workflow.result", out))
        return attrs
    attrs = [
        attr("gen_ai.operation.name", "invoke_agent"),
        attr("gen_ai.task.input", inp),
        attr("gen_ai.task.input.truncated", itr, "boolValue"),
    ]
    if outcome == "finished":
        attrs += [
            attr("gen_ai.task.output", out),
            attr("gen_ai.task.output.truncated", otr, "boolValue"),
        ]
    return attrs


def workflow_spans(run, turns, used, orphans, used_orphans, hook_turns):
    ev = run["events"]
    starts = [e for e in ev if e["type"] == "agent_started"]
    terminal_events = [
        e for e in ev if e["type"] in {"run_finished", "run_failed", "run_cancelled"}
    ]
    if not terminal_events:
        return []
    sk = resolve_session_key(run) or "unknown"
    started = next((e for e in ev if e["type"] == "run_started"), ev[0])
    terminal = terminal_events[-1]
    t0 = iso_ns(started["ts"])
    t1 = iso_ns(terminal["ts"])
    outcome = {
        "run_finished": "finished",
        "run_failed": "failed",
        "run_cancelled": "cancelled",
    }[terminal["type"]]
    fin = {e["data"]["agent_id"]: e for e in ev if e["type"] == "agent_finished"}
    acp_bindings = {
        e["data"]["agent_id"]: e["data"]["acp_session_id"]
        for e in ev
        if e["type"] == "agent_session_bound"
    }
    # Prompt frames carry workflow_run_id and agent_id from KiroCrew. Session
    # identity alone is insufficient when named ACP sessions are reused.
    picked = {}
    run_id = run["run_id"]
    for e in starts:
        agent_key = e["data"]["agent_id"]
        expected_session = acp_bindings.get(agent_key)
        matches = [
            t
            for t in turns
            if t.get("workflow_run_id") == run_id
            and t.get("agent_id") == agent_key
            and (not expected_session or t.get("sid") == expected_session)
            and (not t.get("incomplete") or outcome in {"cancelled", "failed"})
        ]
        if not matches and expected_session:
            identity_matches = [
                t
                for t in turns
                if t.get("workflow_run_id") == run_id and t.get("agent_id") == agent_key
            ]
            v2.append_jsonl(
                "correlation-errors.jsonl",
                {
                    "reason": (
                        "workflow_agent_session_conflict"
                        if expected_session and identity_matches
                        else "missing_workflow_agent_turn"
                    ),
                    "workflow_run_id": run_id,
                    "agent_id": agent_key,
                    "expected_acp_session_id": expected_session,
                    "observed_acp_session_ids": sorted(
                        {t.get("sid") for t in identity_matches if t.get("sid")}
                    ),
                },
            )
        picked[agent_key] = matches
        used.update(id(t) for t in matches)
        for turn in matches:
            if turn.get("incomplete"):
                turn["end"] = t1
                turn["stop"] = outcome
            turn["workflow_run_id"] = run_id
            turn["agent_id"] = agent_key
            turn["workflow_attribution"] = "explicit_ids"
    # Hook turns carry the workflow run ID. Parent and completion relationships
    # are joined only through that producer ID.
    related_turns = [h for h in hook_turns if h.get("workflow_run_id") == run_id]
    parent_turn = next((h for h in related_turns if not h.get("workflow")), None)
    completion = next((h for h in related_turns if h.get("workflow")), None)
    launch_call_id = None
    if parent_turn is None:
        launches = []
        for turn in turns:
            for call in tool_calls(turn.get("frames", [])):
                if call.get("name") != "workflow_run":
                    continue
                try:
                    output = json.loads(call.get("result") or "null")
                except (TypeError, ValueError):
                    continue
                if not isinstance(output, dict) or not isinstance(output.get("items"), list):
                    continue
                texts = []
                for item in output["items"]:
                    payload = item.get("Json") if isinstance(item, dict) else None
                    blocks = payload.get("content") if isinstance(payload, dict) else None
                    if isinstance(blocks, list):
                        texts.extend(
                            block["text"] for block in blocks
                            if isinstance(block, dict) and block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                        )
                if not any(text.startswith(f"Started workflow run `{run_id}` ") for text in texts):
                    continue
                hook = match_v2(turn, sk, hook_turns)
                if hook is None and resolve_session_key(turn) == sk:
                    trace, root = turn.get("hook_trace_id"), turn.get("hook_span_id")
                    if (isinstance(trace, str) and re.fullmatch(r"[0-9a-f]{32}", trace)
                            and isinstance(root, str) and re.fullmatch(r"[0-9a-f]{16}", root)):
                        hook = {"trace_id": trace, "span_id": root, "session": sk}
                if hook is not None:
                    launches.append((hook, call["id"]))
        if len(launches) == 1:
            parent_turn, launch_call_id = launches[0]
        elif len(launches) > 1:
            v2.append_jsonl("correlation-errors.jsonl", {
                "reason": "ambiguous_workflow_launch", "workflow_run_id": run_id,
                "tool_call_ids": [call_id for _, call_id in launches],
            })
    # A background workflow can run between the initiating turn and its later
    # completion turn. When there is no active parent, reuse the completion
    # turn's trace ID so the workflow root and phases appear in the chat trace.
    trace_id = (
        parent_turn["trace_id"]
        if parent_turn
        else (completion["trace_id"] if completion else secrets.token_hex(16))
    )
    run_span = secrets.token_hex(8)
    run_attrs = [
        attr("session.id", sk),
        attr("gen_ai.conversation.id", sk),
        attr("workflow.run_id", run["run_id"]),
        attr("workflow.name", run.get("name", "")),
        attr("workflow.status", outcome),
        attr("crew.source", "acp_tap"),
    ] + wf_io(run, bool(parent_turn), outcome)
    run_attrs += producer_session_attributes(run)
    if launch_call_id:
        run_attrs.append(attr("workflow.launch_tool_call_id", launch_call_id))
    script_hash = started.get("data", {}).get("script_hash")
    if script_hash:
        run_attrs.append(attr("workflow.script.sha256", script_hash))
    terminal_data = terminal.get("data", {})
    if outcome == "failed":
        error, _ = safe_text(terminal_data.get("error", run.get("error", "")))
        run_attrs += [attr("error.type", "workflow.run_failed"), attr("error.message", error)]
        if terminal_data.get("where"):
            run_attrs.append(attr("workflow.failure.where", terminal_data["where"]))
    elif outcome == "cancelled":
        reason, _ = safe_text(terminal_data.get("reason", ""))
        run_attrs.append(attr("workflow.cancel.reason", reason))
    run_attrs.append(attr("crew.attribution", "explicit_ids"))
    if completion:
        run_attrs.append(attr("workflow.completion_trace_id", completion["trace_id"]))
    out = [
        span(
            trace_id,
            run_span,
            "workflow " + run.get("name", run["run_id"]),
            t0,
            t1,
            run_attrs,
            parent_turn["span_id"] if parent_turn else None,
            sk,
            2 if outcome == "failed" else 0,
        )
    ]
    outcome_attrs = [
        attr("session.id", sk),
        attr("workflow.run_id", run["run_id"]),
        attr("workflow.outcome", outcome),
        attr("workflow.status", outcome),
    ] + producer_session_attributes(run)
    if outcome == "failed":
        error, _ = safe_text(terminal_data.get("error", run.get("error", "")))
        outcome_attrs += [attr("error.type", "workflow.run_failed"), attr("error.message", error)]
        if terminal_data.get("where"):
            outcome_attrs.append(attr("workflow.failure.where", terminal_data["where"]))
    elif outcome == "cancelled":
        reason, _ = safe_text(terminal_data.get("reason", ""))
        outcome_attrs.append(attr("workflow.cancel.reason", reason))
    out.append(
        span(
            trace_id,
            secrets.token_hex(8),
            "workflow outcome " + outcome,
            t1,
            t1 + 1,
            outcome_attrs,
            run_span,
            sk,
            2 if outcome == "failed" else 0,
        )
    )
    phases = [e for e in ev if e["type"] == "phase_started"]
    phase_id = {}
    for i, ph in enumerate(phases):
        end = iso_ns(phases[i + 1]["ts"]) if i + 1 < len(phases) else t1
        pid = secrets.token_hex(8)
        phase_id[ph["data"].get("title")] = pid
        out.append(
            span(
                trace_id,
                pid,
                "phase " + str(ph["data"].get("title")),
                iso_ns(ph["ts"]),
                end,
                [
                    attr("session.id", sk),
                    attr("workflow.run_id", run_id),
                    attr("workflow.phase", ph["data"].get("title")),
                ]
                + producer_session_attributes(run),
                run_span,
                sk,
            )
        )
    fin = {e["data"]["agent_id"]: e for e in ev if e["type"] == "agent_finished"}
    for e in starts:
        d = e["data"]
        label = str(d.get("label", d["agent_id"]))
        agent_parent = phase_id.get(d.get("phase"), run_span)
        acp_turns = picked.get(d["agent_id"], [])
        a0 = iso_ns(e["ts"])
        finished = fin.get(d["agent_id"])
        a1 = iso_ns(finished["ts"]) if finished else t1
        agent_id = secrets.token_hex(8)
        aattrs = [
            attr("session.id", sk),
            attr("gen_ai.conversation.id", sk),
            attr("gen_ai.operation.name", "invoke_agent"),
            attr("gen_ai.agent.name", label),
            attr("workflow.run_id", run_id),
            attr("workflow.agent_id", d["agent_id"]),
            attr("workflow.phase", d.get("phase", "")),
            attr("gen_ai.task.input.available", False, "boolValue"),
            attr("crew.source", "hook_journal"),
        ] + producer_session_attributes(run)
        aattrs.append(attr("crew.attribution", "explicit_ids"))
        if acp_bindings.get(d["agent_id"]):
            aattrs.append(attr("acp.session_id", acp_bindings[d["agent_id"]]))
        if finished:
            summary = finished.get("data", {}).get("result_summary")
            if summary is not None:
                aattrs.append(attr("gen_ai.task.output", summary))
            if "ok" in finished.get("data", {}):
                aattrs.append(attr("workflow.agent_ok", finished["data"]["ok"]))
            agent_status = "finished" if finished.get("data", {}).get("ok", True) else "failed"
            agent_span_status = 0 if agent_status == "finished" else 2
        else:
            agent_status = "cancelled" if outcome == "cancelled" else "interrupted"
            agent_span_status = 2 if outcome == "failed" else 0
        aattrs.append(attr("workflow.agent.status", agent_status))
        out.append(
            span(
                trace_id,
                agent_id,
                "invoke_agent " + label,
                a0,
                a1,
                aattrs,
                agent_parent,
                sk,
                agent_span_status,
            )
        )
        for acp_turn in acp_turns:
            out += turn_spans(
                acp_turn,
                sk,
                trace_id,
                agent_parent,
                agent_name=label,
                name="invoke_agent " + label,
                root=agent_id,
            )
        agent_tool_order = 0
        for orphan in orphans:
            if orphan["id"] in used_orphans:
                continue
            explicit_orphan_match = (
                orphan.get("workflow_run_id") == run_id and orphan.get("agent_id") == d["agent_id"]
            )
            if not explicit_orphan_match:
                continue
            used_orphans.add(orphan["id"])
            name = orphan.get("name") or "unknown"
            try:
                tool_input = json.loads(orphan.get("input") or "null")
            except (TypeError, ValueError):
                tool_input = None
            if isinstance(tool_input, dict) and tool_input.get("server") and tool_input.get("tool"):
                name = f"mcp.{tool_input['server']}.{tool_input['tool']}"
            elif isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
                name = "shell"
            agent_tool_order += 1
            tattrs = [
                attr("session.id", sk),
                attr("gen_ai.conversation.id", sk),
                attr("gen_ai.operation.name", "execute_tool"),
                attr("gen_ai.tool.name", name),
                attr("tool.name", name),
                attr("gen_ai.tool.call.id", orphan.get("tool_call_id") or orphan["id"]),
                attr("workflow.tool.order", agent_tool_order, "intValue"),
                attr("crew.attribution", "explicit_ids"),
                attr("workflow.run_id", run_id),
                attr("workflow.agent_id", d["agent_id"]),
                attr("tool.success", "unknown"),
                attr("tool.result.available", "result" in orphan, "boolValue"),
                attr("crew.source", "hook_journal"),
            ]
            tattrs += producer_session_attributes(orphan)
            if orphan.get("input"):
                tattrs += [
                    attr("tool.input", orphan["input"]),
                    attr("gen_ai.tool.call.arguments", orphan["input"]),
                ]
            if orphan.get("result") is not None:
                tattrs += [
                    attr("tool.output", orphan["result"]),
                    attr("gen_ai.tool.call.result", orphan["result"]),
                ]
            out.append(
                span(
                    trace_id,
                    secrets.token_hex(8),
                    "execute_tool " + name,
                    int(orphan["start_ns"]),
                    int(orphan.get("end_ns") or orphan["start_ns"]),
                    tattrs,
                    agent_id,
                    sk,
                )
            )
    return out


def v2_turns():
    """Closed hook turns, written by the v2 runner at Stop: session key, trace ID, root span ID, start and end."""
    out = []
    try:
        for line in (v2.STATE_ROOT / "turns.jsonl").read_text().splitlines()[-500:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def match_v2(t, sid_key, hook_turns):
    """Match an ACP turn to its hook turn by exact producer identity."""
    turn_id = t.get("turn_id")
    if turn_id:
        matches = [h for h in hook_turns if h.get("session") == sid_key and h.get("turn_id") == turn_id]
    else:
        trace_id, span_id = t.get("hook_trace_id"), t.get("hook_span_id")
        if not (isinstance(trace_id, str) and isinstance(span_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", trace_id)
                and re.fullmatch(r"[0-9a-f]{16}", span_id)):
            return None
        matches = [h for h in hook_turns if h.get("session") == sid_key
                   and h.get("trace_id") == trace_id and h.get("span_id") == span_id]
    return matches[0] if len(matches) == 1 else None


def main(argv=None):
    global args, STATE, state
    args = make_parser().parse_args(argv)
    STATE = Path(args.state) if args.state else v2.STATE_ROOT / "acp-spans-state.json"
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else {"emitted": [], "runs": []}
    )
    state.setdefault("emitted", [])
    state.setdefault("runs", [])
    # One pass at a time: Stop hooks can start several converters close together.
    import fcntl

    v2.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (v2.STATE_ROOT / ".acp-lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            run()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run():
    prune_replay_ledger(state, "emitted")
    prune_replay_ledger(state, "runs")
    known_turn_ids = set(state["emitted"])
    known_run_ids = set(state["runs"])
    hook_turns = v2_turns()
    rows = drop_replay(load_frames())
    turns = build_turns(rows, include_incomplete=True)
    attach_prompts(turns)
    sm = {}
    try:
        for key, v in json.loads(Path(args.session_map).read_text()).items():
            if isinstance(v, dict) and v.get("sid"):
                sm[v["sid"]] = key
    except (OSError, ValueError):
        pass
    spans, used, used_orphans = [], set(), set()
    orphan_by_id = {}
    try:
        for line in (v2.STATE_ROOT / "orphan-tools.jsonl").read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            orphan_by_id[record["id"]] = {**orphan_by_id.get(record["id"], {}), **record}
    except OSError:
        pass
    workflow_orphans = sorted(orphan_by_id.values(), key=lambda r: int(r.get("start_ns", 0)))
    # 1. workflow runs
    # Invoker-created run records are shared through --runs-dir. Dashboard
    # workflows may be stored in KiroCrew's runtime directory, so scan those
    # locations too (the runtime path differs between KiroCrew releases).
    run_dirs = {Path(args.runs_dir), home / "workflows/runs", home / ".kiro/crew/workflows/runs"}
    kirocrew_home = os.environ.get("KIROCREW_HOME")
    if kirocrew_home:
        run_dirs.add(Path(kirocrew_home).expanduser() / "workflows/runs")
    run_files = {path for run_dir in run_dirs for path in glob.glob(str(run_dir / "wf_*.json"))}
    for f in sorted(run_files, key=lambda p: Path(p).stat().st_mtime)[-16:]:
        try:
            run = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        # Ignore legacy/minimal run records that are not full workflow event
        # journals; one such file must not abort ACP conversion for chat turns.
        if not isinstance(run, dict) or not isinstance(run.get("events"), list):
            continue
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        mark_workflow_turns_used(run, turns, used)
        terminal = [
            e for e in run["events"] if e["type"] in {"run_finished", "run_failed", "run_cancelled"}
        ]
        terminal_ns = iso_ns(terminal[-1]["ts"]) if terminal else None
        if replay_is_suppressed(state, "runs", run_id, args.force, known_run_ids):
            continue
        sk = resolve_session_key(run)
        if terminal:
            completion = next(
                (
                    h
                    for h in hook_turns
                    if h.get("session") == sk and h.get("workflow_run_id") == run.get("run_id")
                ),
                None,
            )
            if completion is None and time.time_ns() - terminal_ns < args.grace * 1e9:
                continue  # the completion turn is not closed yet; look again on the next pass
        got = workflow_spans(run, turns, used, workflow_orphans, used_orphans, hook_turns)
        if got:
            spans += got
            remember_replay_id(state, "runs", run_id)
            known_run_ids.add(run_id)
    # 2. ordinary chat turns
    for t in turns:
        if id(t) in used:
            continue
        if t.get("incomplete"):
            continue
        if t.get("workflow_run_id") and not args.force and t["workflow_run_id"] in known_run_ids:
            continue
        key = (
            f"{t['sid']}@{t.get('turn_id') or (str(t.get('pid')) + ':' + str(t.get('request_id')))}"
        )
        if replay_is_suppressed(state, "emitted", key, args.force, known_turn_ids):
            continue
        sid_key = resolve_session_key(t, sm)
        if sid_key is None and not args.all_sessions:
            continue
        if sid_key and not t.get("turn_id"):
            v2.append_jsonl(
                "correlation-errors.jsonl",
                {
                    "reason": "missing_turn_id",
                    "acp_session_id": t["sid"],
                    "request_id": t.get("request_id"),
                    "pid": t.get("pid"),
                },
            )
        h = match_v2(t, sid_key, hook_turns) if sid_key else None
        if h:
            # With hook tool emission disabled, ACP owns tool spans for ordinary
            # dashboard chats as well as workflow-agent turns.
            turn_output = turn_spans(
                t,
                sid_key,
                h["trace_id"],
                None,
                root=h["span_id"],
                include_tool_spans=v2.HOOK_TOOLS == "off",
            )
        elif sid_key and time.time_ns() - t["end"] < args.grace * 1e9:
            continue  # the Stop hook may not have closed the turn yet; look again on the next pass
        else:
            turn_output = turn_spans(t, sid_key or f"acp:{t['sid']}", secrets.token_hex(16), None)
        spans += turn_output
        remember_replay_id(state, "emitted", key)
        known_turn_ids.add(key)
    v2.export(spans)
    prune_replay_ledger(state, "emitted")
    prune_replay_ledger(state, "runs")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")
    n_turn = sum(1 for s in spans if s["name"].startswith("invoke_agent"))
    n_tool = sum(1 for s in spans if s["name"].startswith("execute_tool"))
    n_workflow = sum(
        1
        for s in spans
        if s["name"].startswith("workflow ") and not s["name"].startswith("workflow outcome ")
    )
    print(
        f"{len(spans)} spans: {n_turn} invoke_agent, {n_tool} execute_tool, "
        f"{sum(1 for s in spans if s['name'] == 'chat')} chat; "
        f"{len(turns)} completed turns seen, workflow runs emitted this pass: {n_workflow}"
    )


if __name__ == "__main__":
    main()
