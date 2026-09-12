"""Turn agent coding activity into WakaTime heartbeats (the send side).

The read side (stats, export, the productivity view) lives elsewhere in this
package. This module is the producer: a completed agent turn that ran a
filesystem-write or shell tool is coding activity, and it enqueues one
heartbeat whose entity is the session's project directory.

Design constraints, all load-bearing:

- **Off the turn hot path.** ``note_coding_activity`` never awaits the network
  and never raises into the caller. It appends to a bounded in-process buffer
  and, at most once per flush interval, drains that buffer on a fire-and-forget
  task. A turn's latency never depends on WakaTime being reachable.
- **Fire-and-forget.** A send failure drops the batch. Heartbeats are activity
  telemetry, not durable data: a retry storm against a down backend would cost
  more than the lost signal is worth, and WakaTime dedupes by two-minute
  buckets so a dropped heartbeat in a busy window is usually redundant anyway.
- **Opt-in, separately from reading.** Sending your activity outward is a
  bigger privacy step than reading your own stats, so it is gated on its own
  ``config.wakatime.send_heartbeats`` flag (default off) AND on
  ``wakatime.enabled``. Off by default means zero cost and zero send.
- **Project label only.** The heartbeat entity is the project directory's
  basename, never a file path, prompt text, or anything outside the project
  root.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import time
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.wakatime.client import MAX_HEARTBEATS_PER_REQUEST
from kiro_crew.wakatime.service import build_client

logger = logging.getLogger(__name__)

#: Tool names that count as coding activity. A read-only tool (grep, fs_read)
#: is not, on its own, coding: the turn only produces a heartbeat when it
#: mutated the workspace or ran a command. Mirrors the filesystem-write and
#: shell families in ``acp.kas_permissions``.
CODING_TOOL_NAMES = frozenset(
    {
        "fs_write",
        "fs_append",
        "str_replace",
        "write",
        "execute_bash",
        "execute_pwsh",
        "control_bash_process",
    }
)

#: Minimum seconds between flushes. WakaTime buckets activity into two-minute
#: windows, so flushing more often than that sends redundant rows.
_FLUSH_INTERVAL_SECONDS = 120.0

#: Hard cap on the buffer. A backend that stays unreachable must not let the
#: buffer grow without bound; past this we drop the oldest rows.
_MAX_BUFFERED = 200

_buffer: list[dict[str, Any]] = []
_last_flush_monotonic = 0.0
_delayed_flush_armed = False


def is_coding_tool(tool_name: str) -> bool:
    """True when a tool NAME is a known filesystem-write or shell tool."""
    return tool_name in CODING_TOOL_NAMES


def is_coding_event(tool_name: str, tool_kind: str, is_shell: bool) -> bool:
    """True when a tool dispatch counts as coding activity.

    Prefer the trusted resolved signals over the name: ``is_shell`` is set by
    the shell classifier, and ``tool_kind`` is the provider-normalized kind
    (``execute`` for a command, ``edit`` for a file write). The name check is a
    fallback, because a Claude-style frame can carry an empty ``tool_name`` and
    some editors expose a mutating tool under a name not in the set.
    """
    if is_shell:
        return True
    if tool_kind in ("execute", "edit"):
        return True
    return is_coding_tool(tool_name)


def line_changes_from_file_changes(file_changes: Any) -> int:
    """Net lines added or removed across a turn's file changes.

    ``file_changes`` is the per-turn list Kiro Crew accumulates for the diff
    chips: dicts with a ``content`` before-snapshot and, once resolved, an
    ``after``. The count is the symmetric line difference (added + removed) via
    difflib, summed over every changed file. Entries without a resolved
    ``after`` contribute nothing rather than a guessed number, so the value is
    a floor, never fabricated. A malformed entry is skipped and the valid
    entries are still counted; a non-list input returns 0. This feeds a
    heartbeat, never a decision.
    """
    if not isinstance(file_changes, list) or not file_changes:
        return 0
    total = 0
    for fc in file_changes:
        if not isinstance(fc, dict):
            continue
        before = fc.get("content") or fc.get("before") or ""
        after = fc.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            continue
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "replace":
                total += (i2 - i1) + (j2 - j1)
            elif tag == "delete":
                total += i2 - i1
            elif tag == "insert":
                total += j2 - j1
    return total


def _entity_for_project(project: str | None) -> str:
    """The heartbeat entity: the project directory basename, or a stable label.

    Never the absolute path — that can carry a home directory and a username.
    The basename is agent/user-selected, so it passes through the same outbound
    redactors as every other externally-sent string before it can leave for
    WakaTime: a directory whose name is itself credential- or URL-shaped is
    scrubbed rather than POSTed verbatim.
    """
    if not project:
        return "kirocrew-session"
    base = os.path.basename(os.path.normpath(project))
    if not base:
        return "kirocrew-session"
    base, _ = redact_exfiltration_urls(base)
    base, _ = redact_credentials(base)
    return base or "kirocrew-session"


def _make_heartbeat(
    project: str | None,
    *,
    ai_input_tokens: int = 0,
    ai_output_tokens: int = 0,
    ai_line_changes: int = 0,
) -> dict[str, Any]:
    """Build one AI-coding heartbeat.

    Kiro Crew is a GenAI agent, so activity is logged in WakaTime's ``ai coding``
    category and carries the AI-activity fields (tokens, line changes) that
    populate the AI Percent / AI Prompts views. The editor and OS are resolved
    by WakaTime from the request User-Agent, not from a body field, so they are
    set on the client rather than here.

    No session identifier is sent: the session key encodes the surface, agent,
    and chat scope, and a 1:1 messaging DM's scope segment is the peer's own
    platform id, so transmitting it would egress a third party's identifier to
    an external service. The AI-coding category and the aggregate token and
    line-change deltas carry the attribution WakaTime needs without one.

    Every AI field is dropped when zero/empty: the WakaTime schema treats them
    as nullable, and sending a zero would dilute per-session averages.
    """
    hb: dict[str, Any] = {
        "entity": _entity_for_project(project),
        "type": "app",
        "category": "ai coding",
        "time": time.time(),
        "is_write": True,
    }
    if project:
        hb["project"] = _entity_for_project(project)
    if ai_input_tokens > 0:
        hb["ai_input_tokens"] = ai_input_tokens
    if ai_output_tokens > 0:
        hb["ai_output_tokens"] = ai_output_tokens
    if ai_line_changes > 0:
        hb["ai_line_changes"] = ai_line_changes
    return hb


def note_coding_activity(
    project: str | None,
    *,
    ai_input_tokens: int = 0,
    ai_output_tokens: int = 0,
    ai_line_changes: int = 0,
    config: KiroCrewConfig | None = None,
) -> None:
    """Record that a coding turn just completed. Never raises, never awaits.

    A no-op unless both ``wakatime.enabled`` and ``wakatime.send_heartbeats``
    are set. Enqueues one AI-coding heartbeat carrying the turn's real token
    counts and net line-change delta, and triggers a flush when the interval
    has elapsed. The counts come from the turn that just ran, so they are
    genuine ``since the last heartbeat`` deltas the way WakaTime expects.
    """
    try:
        cfg = config or KiroCrewConfig.load()
        if not cfg.wakatime.enabled or not cfg.wakatime.send_heartbeats:
            return
        _buffer.append(
            _make_heartbeat(
                project,
                ai_input_tokens=max(0, int(ai_input_tokens or 0)),
                ai_output_tokens=max(0, int(ai_output_tokens or 0)),
                ai_line_changes=max(0, int(ai_line_changes or 0)),
            )
        )
        if len(_buffer) > _MAX_BUFFERED:
            del _buffer[:-_MAX_BUFFERED]
        _maybe_schedule_flush()
    except Exception:
        # Producing a heartbeat must never disturb the turn that produced it.
        logger.debug("wakatime: note_coding_activity failed", exc_info=True)


def _maybe_schedule_flush() -> None:
    global _last_flush_monotonic, _delayed_flush_armed
    now = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    remaining = _FLUSH_INTERVAL_SECONDS - (now - _last_flush_monotonic)
    if remaining > 0:
        # Within the interval: instead of dropping this row until the NEXT
        # activity happens to cross the interval, arm a one-shot delayed flush
        # for the remainder. Without this a final coding turn inside the window
        # would leave its heartbeat buffered and unsent on exit. Only one timer
        # is armed at a time; it drains whatever is buffered when it fires.
        if not _delayed_flush_armed:
            _delayed_flush_armed = True
            loop.call_later(remaining, _fire_delayed_flush)
        return
    _last_flush_monotonic = now
    batch = _buffer[:]
    _buffer.clear()
    loop.create_task(_flush(batch))


def _fire_delayed_flush() -> None:
    global _last_flush_monotonic, _delayed_flush_armed
    _delayed_flush_armed = False
    _last_flush_monotonic = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    batch = _buffer[:]
    _buffer.clear()
    if batch:
        loop.create_task(_flush(batch))


async def _flush(batch: list[dict[str, Any]]) -> None:
    """Send a batch, chunked to the API cap. Drops on any failure.

    Config is reloaded and the opt-in re-checked HERE, not captured when the
    flush was scheduled: a delayed flush can fire up to the interval later, and
    a user who disabled sending or changed the endpoint in that window must not
    have telemetry sent (possibly to the former endpoint). The config load and
    client construction both touch disk/vault, so they run off the event loop.
    """
    if not batch:
        return
    client = None
    try:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if not (cfg.wakatime.enabled and cfg.wakatime.send_heartbeats):
            return
        client = await asyncio.to_thread(build_client, cfg)
        if client is None:
            return
        for start in range(0, len(batch), MAX_HEARTBEATS_PER_REQUEST):
            chunk = batch[start : start + MAX_HEARTBEATS_PER_REQUEST]
            await client.send_heartbeats(chunk)
    except Exception:
        logger.debug("wakatime: heartbeat flush failed, dropping batch", exc_info=True)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("wakatime: client close failed", exc_info=True)


def _reset_for_tests() -> None:
    """Clear module state between tests."""
    global _last_flush_monotonic, _delayed_flush_armed
    _buffer.clear()
    _last_flush_monotonic = 0.0
    _delayed_flush_armed = False
