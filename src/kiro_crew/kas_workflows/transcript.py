"""One KAS workflow step's own conversation, read from KAS's session store.

KAS opens a session per step and persists it as ``messages.jsonl`` beside a
``session.json`` under ``<kiro home>/sessions/<workspace bucket>/<session id>/``.
The dashboard's step pane (the IDE's "step chat") renders that conversation.

Read-only and bounded, by construction:

- The session id is never taken from a request. The router resolves it from a
  run it already mirrors (:meth:`RunMirror.step_session`), and this module only
  accepts the ``sess_<uuid>`` shape KAS mints, so no request can name a path.
- The located directory must sit directly under the sessions root, and its
  ``session.json`` must say it is a step of the same workflow, or nothing is read.
- Only the newest :data:`MAX_READ_BYTES` of the log are read, at most
  :data:`MAX_ENTRIES` entries are returned, and every text field is capped and
  passed through the credential redactor before it leaves the gateway.

The log format is KAS's own and unversioned, so parsing is tolerant: an entry of
an unknown type is skipped, a malformed line is skipped, and a missing field
reads as absent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: The session-id shape KAS mints for every session, step sessions included.
SESSION_ID_RE = re.compile(r"^sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

MAX_READ_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 400
MAX_TEXT_CHARS = 20_000
MAX_TOOL_INPUT_CHARS = 2_000
MAX_TOOL_OUTPUT_CHARS = 6_000
#: A one-line field (name, kind, status, title, timestamp) and an id.
_MAX_LINE = 200
_MAX_ID = 256


class StepTranscriptError(LookupError):
    """The step's conversation cannot be read: unknown, mismatched or unreadable."""


def _sessions_root() -> Path:
    from kiro_crew.config.paths import kiro_home

    return kiro_home() / "sessions"


def _clip(value: Any, limit: int, *, line: bool = False) -> str | None:
    """*value* as the step pane shows it: neutralized, redacted over a bounded
    window, then capped; None when empty.

    The same order and neutralizers as the router's and ``asks._shown``: a
    *line* field (a name, id, kind or status) loses every control and bidi
    character; text keeps its line breaks and tabs.
    """
    if not isinstance(value, str) or not value:
        return None
    from kiro_crew.kas_workflows.asks import _UNSAFE_RE, _UNSAFE_TEXT_RE
    from kiro_crew.platform import redact_via_context

    text = value if len(value) <= limit * 2 else value[: limit * 2]
    text = (_UNSAFE_RE if line else _UNSAFE_TEXT_RE).sub(" ", text)
    text = redact_via_context(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _text(content: Any) -> str:
    """User/assistant content: a string, or a list of ``{type:'text', text}`` parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _tool_input(payload: dict[str, Any]) -> str | None:
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    for key in ("command", "path", "query", "url", "pattern"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return _clip(value, MAX_TOOL_INPUT_CHARS)
    try:
        return _clip(json.dumps(args, ensure_ascii=False, sort_keys=True), MAX_TOOL_INPUT_CHARS)
    except (TypeError, ValueError):
        return None


def locate(session_id: str, *, root: Path | None = None) -> Path:
    """The directory of KAS session *session_id*, under the sessions root only."""
    if not SESSION_ID_RE.match(session_id or ""):
        raise StepTranscriptError("not a Kiro Agent session id")
    base = (root or _sessions_root()).resolve()
    for candidate in base.glob(f"*/{session_id}"):
        resolved = candidate.resolve()
        # One bucket level below the root, never anywhere a link points.
        if resolved.parent.parent == base and resolved.is_dir():
            return resolved
    raise StepTranscriptError("the step's session is not on disk")


def _read_tail(path: Path) -> tuple[list[str], bool]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        clipped = size > MAX_READ_BYTES
        if clipped:
            fh.seek(size - MAX_READ_BYTES)
        data = fh.read(MAX_READ_BYTES)
    lines = data.decode("utf-8", errors="replace").splitlines()
    if clipped and lines:
        lines = lines[1:]  # the first line was cut mid-record
    return lines, clipped


def read_step_transcript(
    workflow_id: str, node_id: str, session_id: str, *, root: Path | None = None
) -> dict[str, Any]:
    """The step's conversation as dashboard entries. Blocking: call off the loop."""
    directory = locate(session_id, root=root)
    meta: dict[str, Any] = {}
    try:
        loaded = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        meta = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        meta = {}
    workflow = (((meta.get("_meta") or {}).get("kiro") or {}).get("workflow")) or {}
    if not isinstance(workflow, dict) or workflow.get("workflowId") != workflow_id:
        raise StepTranscriptError("that session is not a step of this run")

    try:
        lines, clipped = _read_tail(directory / "messages.jsonl")
    except FileNotFoundError:
        lines, clipped = [], False
    except OSError as exc:
        raise StepTranscriptError("the step's conversation could not be read") from exc

    entries: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    credits = 0.0
    saw_credits = False
    running = False
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        rid = _clip(record.get("id"), _MAX_ID, line=True) or str(len(entries))
        ts = _clip(record.get("timestamp"), _MAX_LINE, line=True)
        if kind == "user":
            text = _clip(_text(payload.get("content")), MAX_TEXT_CHARS)
            if text:
                entries.append({"kind": "user", "id": rid, "ts": ts, "text": text})
        elif kind == "assistant":
            raw = _text(payload.get("content"))
            if not raw:
                continue
            reasoning = payload.get("operationType") == "Reasoning"
            last = entries[-1] if entries else None
            if last and last["kind"] == "assistant" and last["reasoning"] == reasoning:
                # The raw chunks are joined, and the whole is clipped once below:
                # a secret split across two chunks is redacted as one.
                last["_raw"] = (last["_raw"] + raw)[: MAX_TEXT_CHARS * 2]
                continue
            entry: dict[str, Any] = {"kind": "assistant", "id": rid, "ts": ts, "text": None}
            entry["reasoning"] = reasoning
            entry["_raw"] = raw[: MAX_TEXT_CHARS * 2]
            entries.append(entry)
        elif kind == "tool_call":
            call_id = payload.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                continue
            existing = tools.get(call_id)
            status = _clip(payload.get("status"), _MAX_LINE, line=True)
            if existing is not None:
                existing["status"] = status or existing["status"]
                continue
            name = _clip(payload.get("toolName"), _MAX_LINE, line=True) or "tool"
            new_tool: dict[str, Any] = {
                "kind": "tool",
                "id": rid,
                "ts": ts,
                "toolCallId": _clip(call_id, _MAX_ID, line=True),
                "name": name,
                "title": _clip(payload.get("title"), _MAX_LINE, line=True) or name,
                "toolKind": _clip(payload.get("kind"), _MAX_LINE, line=True),
                "status": status or "running",
                "input": _tool_input(payload),
                "output": None,
            }
            tools[call_id] = new_tool
            entries.append(new_tool)
        elif kind == "tool_result":
            tool = tools.get(str(payload.get("toolCallId")))
            if tool is not None:
                tool["output"] = _clip(_text(payload.get("content")), MAX_TOOL_OUTPUT_CHARS)
        elif kind == "pending_interaction":
            tool = tools.get(str(payload.get("toolCallId")))
            if tool is not None and tool["status"] in ("running", "pending"):
                tool["status"] = "waiting_approval"
        elif kind == "interaction_resolved":
            tool = tools.get(str(payload.get("toolCallId")))
            if tool is not None and tool["status"] == "waiting_approval":
                tool["status"] = "running"
        elif kind == "usage_summary":
            for summary in payload.get("promptTurnSummaries") or ():
                if isinstance(summary, dict) and summary.get("unit") == "credit":
                    usage = summary.get("usage")
                    if isinstance(usage, (int, float)):
                        credits += float(usage)
                        saw_credits = True
        elif kind == "turn_start":
            running = True
        elif kind == "turn_end":
            running = False

    for entry in entries:
        if "_raw" in entry:
            entry["text"] = _clip(entry.pop("_raw"), MAX_TEXT_CHARS)
    entries = [e for e in entries if e["kind"] != "assistant" or e["text"]]
    truncated = clipped or len(entries) > MAX_ENTRIES
    # KAS leaves an auto-approved read at ``approved`` after it ran; its result is
    # the proof it finished, so the row reads as done like every other tool.
    for tool in tools.values():
        if tool["status"] == "approved" and tool["output"] is not None:
            tool["status"] = "completed"
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]
    status = _clip(meta.get("status"), _MAX_LINE, line=True)
    return {
        "workflowId": workflow_id,
        "nodeId": node_id,
        "sessionId": session_id,
        "title": _clip(meta.get("title"), _MAX_LINE, line=True),
        "agent": _clip(meta.get("agentMode"), _MAX_LINE, line=True),
        "model": _clip(meta.get("modelId"), _MAX_LINE, line=True),
        "status": "running" if running else (status or "idle"),
        "credits": round(credits, 4) if saw_credits else None,
        "startedAt": _clip(meta.get("createdAt"), _MAX_LINE, line=True),
        "updatedAt": _clip(meta.get("lastModifiedAt"), _MAX_LINE, line=True),
        "entries": entries,
        "truncated": truncated,
    }


__all__ = [
    "MAX_ENTRIES",
    "SESSION_ID_RE",
    "StepTranscriptError",
    "locate",
    "read_step_transcript",
]
