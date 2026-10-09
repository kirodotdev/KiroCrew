from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from kiro_crew.history import ConversationLog
from kiro_crew.personal_insights.insights_source import _minimize_text, safe_title

OWNER_TEXT_CAP = 420
OWNER_TEXT_MAX_TURNS = 10
MIN_ROWS = 3
SESSION_PREFIX = "dashboard_chat-"

_CORRECTION_RE = re.compile(
    r"(?i)\b(no[,.!]|wrong|not what i|that'?s not|stop|why (?:are|is|did|would)|seriously|"
    r"are you kidding|wtf|omg|taking so long|too long|again\?|i said|i asked)"
)
_TOOL_ERROR_RE = re.compile(
    r"(?i)(\berror\b|denied|blocked|exit status: [1-9]|traceback|failed|timed out|stalled)"
)
_TOOL_NAME_RE = re.compile(r"^🔧 (?:Running|Loading tool): @?([\w./:-]+)")


@dataclass
class SessionFacts:
    key: str
    title: str
    started: str
    ended: str
    duration_minutes: int
    owner_turns: int
    assistant_turns: int
    tool_calls: int
    tool_errors: int
    stalls: int
    subagent_dispatches: int
    correction_markers: int
    tool_names: list[tuple[str, int]] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    owner_digest: list[str] = field(default_factory=list)
    minimization: dict[str, int] = field(default_factory=dict)

    def compact(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "started": self.started[:16],
            "duration_minutes": self.duration_minutes,
            "owner_turns": self.owner_turns,
            "assistant_turns": self.assistant_turns,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "stalls": self.stalls,
            "subagent_dispatches": self.subagent_dispatches,
            "correction_markers": self.correction_markers,
            "top_tools": [f"{n} x{c}" for n, c in self.tool_names[:8]],
            "owner_messages": self.owner_digest,
        }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _tool_name(row: dict[str, Any]) -> str | None:
    meta = row.get("meta") or {}
    name = meta.get("tool_name")
    if name and name != "tool_search":
        return str(name)
    match = _TOOL_NAME_RE.match(str(row.get("content", "")))
    if match:
        candidate = match.group(1)
        if candidate.startswith("Loading"):
            return None
        return candidate.rsplit("/", 1)[-1]
    return None


def extract_facts(
    log: ConversationLog, key: str, catalog_row: dict[str, Any]
) -> SessionFacts | None:
    rows = log.read_messages(key)
    if len(rows) < MIN_ROWS:
        return None
    owner_turns = 0
    assistant_turns = 0
    tool_calls = 0
    tool_errors = 0
    stalls = 0
    dispatches = 0
    corrections = 0
    names: Counter[str] = Counter()
    servers: set[str] = set()
    digest: list[str] = []
    classes: Counter[str] = Counter()
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is not None:
            first_ts = first_ts or ts
            last_ts = ts
        role = row.get("role")
        content = str(row.get("content", ""))
        if role == "user":
            owner_turns += 1
            if _CORRECTION_RE.search(content):
                corrections += 1
            if len(digest) < OWNER_TEXT_MAX_TURNS:
                minimized, receipt = _minimize_text(content)
                for cls, count in receipt.classes:
                    classes[cls] += count
                digest.append(minimized.strip()[:OWNER_TEXT_CAP])
        elif role == "assistant":
            assistant_turns += 1
        elif role == "tool":
            meta = row.get("meta") or {}
            if "Loading tool" in content:
                continue
            tool_calls += 1
            name = _tool_name(row)
            if name:
                names[name] += 1
                if name in ("spawn_run", "spawn_sub_agents", "spawn_continue"):
                    dispatches += 1
            if meta.get("mcp_server"):
                servers.add(str(meta["mcp_server"]))
            output = str(meta.get("output") or "")[:400]
            if _TOOL_ERROR_RE.search(output):
                tool_errors += 1
        elif role == "error":
            stalls += 1
    if owner_turns == 0:
        return None
    duration = 0
    if first_ts and last_ts:
        duration = max(0, int((last_ts - first_ts).total_seconds() // 60))
    return SessionFacts(
        key=key,
        title=safe_title(catalog_row.get("title"), "dashboard", key),
        started=first_ts.isoformat() if first_ts else "",
        ended=last_ts.isoformat() if last_ts else "",
        duration_minutes=duration,
        owner_turns=owner_turns,
        assistant_turns=assistant_turns,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        stalls=stalls,
        subagent_dispatches=dispatches,
        correction_markers=corrections,
        tool_names=names.most_common(12),
        mcp_servers=sorted(servers),
        owner_digest=digest,
        minimization=dict(classes),
    )


def select_sessions(
    log: ConversationLog, *, days: int, max_sessions: int
) -> tuple[list[dict[str, Any]], int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    rows = [
        row
        for row in log.list_sessions()
        if str(row.get("key", "")).startswith(SESSION_PREFIX)
        and float(row.get("modified") or 0) >= cutoff
        and row.get("memory_mode", "persistent") == "persistent"
    ]
    rows.sort(key=lambda row: float(row.get("modified") or 0), reverse=True)
    return rows[:max_sessions], len(rows)


def aggregate(facts: list[SessionFacts]) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    servers: Counter[str] = Counter()
    for item in facts:
        for name, count in item.tool_names:
            tools[name] += count
        for server in item.mcp_servers:
            servers[server] += 1
    return {
        "sessions": len(facts),
        "owner_turns": sum(f.owner_turns for f in facts),
        "assistant_turns": sum(f.assistant_turns for f in facts),
        "tool_calls": sum(f.tool_calls for f in facts),
        "tool_errors": sum(f.tool_errors for f in facts),
        "stalls": sum(f.stalls for f in facts),
        "subagent_dispatches": sum(f.subagent_dispatches for f in facts),
        "sessions_with_corrections": sum(1 for f in facts if f.correction_markers),
        "correction_markers": sum(f.correction_markers for f in facts),
        "active_minutes": sum(f.duration_minutes for f in facts),
        "top_tools": tools.most_common(12),
        "mcp_servers": servers.most_common(10),
        "longest_sessions": sorted(((f.duration_minutes, f.key) for f in facts), reverse=True)[:5],
    }
