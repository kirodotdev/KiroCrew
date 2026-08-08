"""Dashboard shared state — ChatSlot and DashboardState."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Coroutine, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew.acp.types import STOP_REASON_CANCELLED
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import DASHBOARD_PORT, config_dir
from kiro_crew.constants import (
    OPTIONS_RE_LINE,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.dashboard.chat_compaction_notice import deliver_channel_compaction_notice
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.history import latest_transcript_ts, monotonic_transcript_ts
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    ChannelLink,
    channel_namespace_of,
    is_channel_session_key,
)
from kiro_crew.notifications.bus import (
    NotificationBus,
    NotificationValidationError,
    normalize_note,
    payload_from_legacy,
)
from kiro_crew.notifications.rate_limit import AppRateLimiter
from kiro_crew.notifications.settings import ChannelSettings
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.release_channel import channel as _release_channel_of_build
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )
    from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog  # noqa: F401
    from kiro_crew.messaging.transport import MessagingTransport  # noqa: F401
    from kiro_crew.power import SleepInhibitor  # noqa: F401

logger = logging.getLogger(__name__)

_CHANNEL_ID_PREFIX_RE = re.compile(r"^([a-z][a-z0-9_-]*):(.*)$", re.IGNORECASE)
_CHANNEL_LABELS = {
    "slack": "Slack",
    "discord": "Discord DM",
    "telegram": "Telegram",
    "teams": "Microsoft Teams",
    "webex": "Webex",
    "wecom": "WeCom",
    "weixin": "WeChat",
}


def _split_namespaced_channel_id(channel_id: str | None) -> tuple[str, str] | None:
    """Return ``(channel_type, target)`` for a ``<type>:<target>`` id."""
    if not channel_id:
        return None
    match = _CHANNEL_ID_PREFIX_RE.match(channel_id)
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def _is_genuine_slack_link(thread_ts: str | None, channel_id: str | None) -> bool:
    """True only for a complete Slack link, never another channel's legacy id."""
    namespaced = _split_namespaced_channel_id(channel_id)
    return bool(
        thread_ts and channel_id and (namespaced is None or namespaced[0] == SLACK_NAMESPACE)
    )


def _link_label(channel_type: str) -> str:
    """Human label for a known channel; preserve unknown types verbatim."""
    return _CHANNEL_LABELS.get(channel_type, channel_type)


def _redacted_link_target(target: str | None) -> str:
    """Return a non-sensitive tail hint, never a raw conversation id."""
    if not target:
        return "…"
    safe, _ = redact_exfiltration_urls(target)
    safe, _ = redact_credentials(safe)
    if safe != target:
        return "…redacted"
    if len(safe) <= 6:
        return f"…{safe[-2:]}" if len(safe) > 2 else "…"
    return f"…{safe[-6:]}"


# Native kiro-cli subagent reconnect policy. The slot state, writer, and replay
# path all import these bounds so retention cannot drift between modules.
NATIVE_SUBAGENT_OUTPUT_TAIL = 40_000
NATIVE_SUBAGENT_OUTPUT_HARD = 80_000
NATIVE_SUBAGENT_DONE_RESULT_CAP = 8_000
NATIVE_SUBAGENT_DONE_TRUNC_MARKER = "…(earlier output truncated)\n"
NATIVE_SUBAGENT_TERMINAL_KEEP = 50
NATIVE_SUBAGENT_TERMINAL_TTL_SECS = 3600.0


def native_subagent_output_tail(chunks: list[str], limit: int = NATIVE_SUBAGENT_OUTPUT_TAIL) -> str:
    """Join only the trailing ``limit`` characters of native-card output."""
    if limit <= 0:
        return ""
    collected: list[str] = []
    total = 0
    for chunk in reversed(chunks):
        collected.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    collected.reverse()
    return "".join(collected)[-limit:]


# Running build's git (branch, short_commit). Resolved ONCE by the CLI gateway
# entrypoint via set_build_info() — AFTER KIROCREW_PROJECT_DIR is detected and
# BEFORE asyncio.run() starts the loop. Deliberately NOT resolved at import time:
# under systemd the entrypoint imports this module before main() detects the
# project dir, so an import-time git_build_info() would see no project dir and the
# lru_cache would then pin ("", "") forever. DashboardState (built on the loop) and
# status_snapshot() only READ this global — they never call git_build_info() — so
# no subprocess ever runs on the event loop.
_build_info: tuple[str, str] = ("", "")


# Auto-minted dashboard slot keys share the shape "<prefix>-<N>-<ts>" where
# <prefix> is chat (the only auto-mint prefix in this fork), <N> is the
# monotonic _slot_counter, and <ts> is a unix second. Minting and index-parsing
# both go through these helpers so the format lives in exactly one place — a
# future change to the key shape can't silently desync the minter from
# reseed_slot_counter() (which would let the post-restart tab<->session
# collision quietly return).
def _mint_slot_key(prefix: str, counter: int, ts: int) -> str:
    """Build an auto-minted slot key of the canonical ``<prefix>-<N>-<ts>`` shape."""
    return f"{prefix}-{counter}-{ts}"


def _slot_index_from_key(key: str) -> int | None:
    """Return the ``<N>`` index from a ``<prefix>-<N>-<ts>`` slot key, else None.

    Non-auto-minted keys (Slack sessions, ascii-sanitized display names) don't
    match the shape and return ``None``. The ``isascii()`` guard keeps a stray
    unicode-digit char (``str.isdigit()`` is True for e.g. superscripts, but
    ``int()`` would raise) from aborting boot-time reseeding.
    """
    parts = key.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isascii() and parts[1].isdigit():
        return int(parts[1])
    return None


def set_build_info(info: tuple[str, str]) -> None:
    """Record the running build's ``(branch, short_commit)`` for status payloads.

    Called once from the CLI gateway entrypoint (sync, pre-loop, post-detection).
    Defaults to ``("", "")`` for non-git / packaged installs, which the frontend
    renders by omitting the build-info rows.
    """
    global _build_info
    _build_info = info


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks.

    Shared by gateway._deliver_result and chat.py queue-drain paths.
    Short-circuits on cancelled tasks (task.exception() would raise CancelledError).
    Exception message is redacted to avoid leaking credentials/URLs to log sinks.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            redacted_tb, _ = redact_credentials(tb)
            redacted_tb, _ = redact_exfiltration_urls(redacted_tb)
            logger.error("Background task failed:\n%s", redacted_tb)
        except Exception as redaction_err:
            # Include the redaction failure class so bugs in the redactor are visible,
            # without logging the raw traceback (which defeats the redaction contract).
            logger.error(
                "Background task failed (redaction error %s): %s",
                type(redaction_err).__name__,
                type(exc).__name__,
            )


# ── Read-only bash command classification ──

_READ_ONLY_BASH_PREFIXES: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "wc",
    "which",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "diff",
    "pwd",
    "echo",
    "date",
    "whoami",
    "hostname",
    "uname",
    "readlink",
    "realpath",
    "basename",
    "dirname",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "git tag",
    "git remote",
    "git rev-parse",
    "git describe",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git blame",
    "brazil ws show",
    "brazil ws list",
    "brazil workspace show",
    "brazil workspace list",
    "brazil versionset print",
    "brazil versionset show",
    "brazil-path",
    "python --version",
    "python3 --version",
    "node --version",
    "java -version",
    "javac -version",
)

_READ_ONLY_PIPE_RE = re.compile(
    r"^\s*(grep|egrep|fgrep|head|tail|wc|sort|uniq|cut|less|more|cat)\b"
)

# Reject redirections and command substitutions — conservative.
_UNSAFE_SHELL_RE = re.compile(r">|`|\$\(|<\(|(?<!&)&(?!&)")

# Discard-only redirect idioms that are read-only despite containing '>'/'&':
# `2>/dev/null`, `>/dev/null`, `&>/dev/null`, `2>>/dev/null`, and `2>&1`.
# These sink or merge output, never writing a real file, so they must be
# stripped before _UNSAFE_SHELL_RE — otherwise every `find … 2>/dev/null`
# falls through to an interactive prompt. A redirect to any real path
# (e.g. `cmd > out.txt`) still trips _UNSAFE_SHELL_RE and stays unsafe.
# The `(?![\w./-])` guard pins the match to the literal device `/dev/null`:
# without it, `>/dev/nullx` or `>/dev/null/../etc/passwd` would be scrubbed as
# a sink, smuggling a real-file write past the unsafe-shell check.
_DEVNULL_REDIR_RE = re.compile(r"(?:\d*>>?|&>)\s*/dev/null(?![\w./-])|\d*>&\d+")


def _classify_bash(cmd: str) -> str:
    """Single source of truth for read-only bash classification.

    Returns "" when the command is read-only, otherwise a human-readable
    reason it was rejected. :func:`is_read_only_bash` and
    :func:`unsafe_bash_reason` both delegate here so the two can never
    diverge — the invariant "reason is non-empty iff not read-only" holds
    by construction rather than by parallel maintenance. Deny-by-default.
    """
    if not cmd.strip():
        return "empty command"
    # Strip discard-only redirects (output sinks / stderr-merge) before the
    # unsafe-shell check; they are read-only but contain '>' / '&'.
    scrubbed = _DEVNULL_REDIR_RE.sub(" ", cmd)
    if _UNSAFE_SHELL_RE.search(scrubbed):
        return "unsafe shell pattern (redirect, command/process substitution, or backgrounding)"
    parts = re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd.strip())
    for part in parts:
        if not part.strip():
            continue
        pipe_parts = [p.strip() for p in part.split("|") if p.strip()]
        if not pipe_parts:
            return "unsafe shell pattern"
        first = pipe_parts[0].strip().lower()
        if not (
            first.endswith("--help")
            or first.endswith("--version")
            or any(first == p or first.startswith(p + " ") for p in _READ_ONLY_BASH_PREFIXES)
        ):
            base = first.split()[0] if first.split() else first
            return f"command '{base}' is not on the read-only allowlist"
        for target in pipe_parts[1:]:
            if not _READ_ONLY_PIPE_RE.match(target):
                tgt = target.split()[0] if target.split() else target
                return f"pipe target '{tgt}' is not a read-only filter"
    return ""


def is_read_only_bash(cmd: str) -> bool:
    """Check if a bash command is read-only. Deny-by-default."""
    return _classify_bash(cmd) == ""


def unsafe_bash_reason(cmd: str) -> str:
    """Human-readable reason a bash command failed read-only classification.

    Used to make rejection messages specific ("unsafe shell pattern …")
    instead of the generic adapter default ("User refused permission to run
    tool"). Returns "" when the command IS read-only (no reason to reject on
    safety grounds).
    """
    return _classify_bash(cmd)


# ── Shared helpers ──


def parse_cls_meta(cls_val: str) -> dict | None:
    """Parse a JSON-encoded ``cls`` string into a meta dict.

    Returns the parsed dict (with ``tool_input`` sanitized) or ``None``
    if ``cls_val`` is not valid JSON or not a dict.  Used by both
    ``_prepare_messages`` (HTTP history) and ``_broadcast_chat_message``
    (live WS push) so the frontend sees an identical ``meta`` structure.
    """
    if not cls_val:
        return None
    try:
        meta = json.loads(cls_val)
        if not isinstance(meta, dict):
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    # Defence-in-depth: sanitize LLM-controlled content at every read boundary
    if isinstance(meta.get("tool_input"), str):
        sanitized, _ = redact_exfiltration_urls(meta["tool_input"])
        sanitized, _ = redact_credentials(sanitized)
        meta["tool_input"] = sanitized

    # Normalize: backend stores as request_id, frontend expects approval_id
    if "request_id" in meta and "approval_id" not in meta:
        meta["approval_id"] = meta.pop("request_id")

    return meta


def _mark_permission_resolved(
    messages: list[dict],
    request_id: str,
    decision: str,
    *,
    only_if_pending: bool = False,
) -> bool:
    """Persist a resolved decision into a permission message's cls JSON.

    Returns True when a permission message was written. Callers holding the
    owning slot MUST set ``slot._dirty = True`` on a True return — the periodic
    flush skips non-dirty slots, so an unflagged in-place mutation can be lost
    on restart and the card comes back as an unanswerable orphan.

    ``only_if_pending`` leaves an already-resolved message untouched (and
    returns False). Use it for backstop callers that must not clobber a richer
    decision already recorded by the primary resolver — e.g. "trust"/"yolo",
    which the UI renders as "Trusted — auto-approving future calls" and would
    otherwise be flattened to a bare "approved".
    """
    for msg in reversed(messages):
        if msg.get("role") == "permission":
            try:
                cls = json.loads(msg.get("cls", "{}"))
                if not isinstance(cls, dict):
                    # Valid JSON but not an object — cannot carry "resolved".
                    # Mirrors parse_cls_meta() / _sweep_stale_permissions().
                    continue
                if cls.get("request_id") == request_id:
                    if only_if_pending and "resolved" in cls:
                        return False
                    cls["resolved"] = decision
                    msg["cls"] = json.dumps(cls)
                    return True
            except (json.JSONDecodeError, TypeError):
                pass
    return False


# ── Constants ──


_DEFAULT_PORT = DASHBOARD_PORT
_SSE_INTERVAL_SECS = 5
_NOTIFICATIONS_FILE = "notifications.jsonl"
_MAX_PERSISTED_NOTIFICATIONS = 200
_AUTO_COMPACT_NOTICE = "🔄 Auto-compacted at {pct:.0f}%."
_AUTO_COMPACT_FAILED_NOTICE = (
    "⚠ Auto-compact failed at {pct:.0f}% — will retry after cooldown. "
    "You can run `/compact` manually."
)
_SESSION_RECYCLED_NOTICE = (
    "♻️ This session was recycled by the watchdog ({reason}). "
    "Conversation history is preserved — your next message starts a fresh process."
)
_MAX_SLOT_MESSAGES = 10000  # Keep all messages — virtual scrolling handles performance

#: Roles that exist only on the wire: appended so a reader/flush can see them,
#: never broadcast as a `chat_message` and never persisted (the mirror of
#: ``chat_persistence._TRANSIENT_ROLES`` minus the rows that ARE broadcast).
#: They get no ``meta.mid`` — see ``_ChatSlot.append``.
_WIRE_ONLY_ROLES = frozenset({"chunk", "done", "streaming"})
_MAX_SOURCE_LINKS_PER_SLOT = 64
# How many source links each slot payload actually serializes (the sidebar
# renders at most this many chips). Shared with the periodic check-status
# refresh so the driver and the serializer cannot drift.
_SERIALIZED_SOURCE_LINKS_PER_SLOT = 3


def _budgeted_source_links(links: list[dict]) -> list[dict]:
    """Apply the sidebar chip budget PER KIND, changes first.

    Pull requests and issues each get their own
    ``_SERIALIZED_SOURCE_LINKS_PER_SLOT`` allowance. A single shared budget
    sliced before the kind filter would let three mentioned issues crowd every
    PR chip out of the sidebar -- and, because the check-status refresh reads
    the same slice, would also stop scheduling that PR's CI status updates.
    Budgeting per kind keeps pre-existing pull-request behaviour unchanged and
    makes issues purely additive.
    """
    changes = [link for link in links if link.get("kind", "change") == "change"]
    issues = [link for link in links if link.get("kind", "change") == "issue"]
    return changes[:_SERIALIZED_SOURCE_LINKS_PER_SLOT] + issues[:_SERIALIZED_SOURCE_LINKS_PER_SLOT]


_NON_DURABLE_SOURCE_LINK_ROLES = frozenset({"chunk", "done", "streaming", "queued", "permission"})
# FIFO ceiling on a slot's pending-context queue (app-kit context inject +
# Slack thread backfill). Shared so the two eviction sites cannot drift.
_MAX_PENDING_CONTEXT = 50

# Bare chat-N label matcher used by DashboardState.resolve_slot() for prefix fallback.
# Gates the prefix lookup to prevent broad matches (e.g. bare "chat" binding to any slot).
_CHAT_N_RE = re.compile(r"chat-\d+")

# Display label for a chat slot that has no real title yet — shown in the UI
# instead of the internal ``chat-N-<ts>`` key (which is an identifier, not a
# name). Applied at the serialization boundary (``_ChatSlot.display_title``),
# so a brand-new empty session, the pre-send window, and the pre-LLM window all
# read the same. The LLM auto-title / fallback replace it with a real title.
NEW_SESSION_TITLE = "New Session…"

# Matches a slot-key *identifier* used as a title (both the stripped
# ``chat-N-<ts>`` and the resumed ``dashboard_chat-N-<ts>`` forms). An untitled
# slot whose title is still such an identifier should display as
# NEW_SESSION_TITLE, not the raw key. Real titles never match this.
_SLOT_KEY_TITLE_RE = re.compile(r"(?:dashboard_)?chat-\d+-\d+$")

# Cron notification wrapper format — used by handlers.py (create), chat.py (detect), ChatPage.tsx (render)
CRON_NOTIFY_PREFIX = "[Cron notification from "
CRON_NOTIFY_END = "[End of cron notification]"
CRON_NOTIFY_RE = re.compile(rf'^{re.escape(CRON_NOTIFY_PREFIX)}"(.*)"\]')
# Both sub-agent markers, for the checks that must treat either shape as a system
# injection. Pass this straight to ``str.startswith`` (it accepts a tuple) instead
# of listing the prefixes per call site: the batch marker is a SIBLING of the
# per-agent one rather than an extension of it, so a per-prefix check written
# against one silently misses the other, and a third shape would miss both.
SUBAGENT_COMPLETION_PREFIXES = (
    SUBAGENT_COMPLETION_PREFIX,
    SUBAGENT_BATCH_COMPLETION_PREFIX,
)
# One-shot synthesis turn fired after ALL sub-agents in a fan-out complete and
# each result has been processed in its own turn (see gateway._subagent_done arm
# + chat_runner drain/idle branch). Its visible reply is the consolidated,
# user-facing summary. Rendered as an "inject" message (not a user bubble); the
# prefix marks it as a synthetic continuation so it is NOT mirrored to linked
# surfaces (Slack/Telegram) as though the user typed it.
SUBAGENT_SYNTHESIS_PREFIX = "[SYSTEM] Sub-agent synthesis:"
SUBAGENT_SYNTHESIS_PROMPT = (
    f"{SUBAGENT_SYNTHESIS_PREFIX} all sub-agents you spawned have completed and each result was "
    "processed above. Produce a single consolidated synthesis as your reply for the user: "
    "(1) restate the original goal you spawned the sub-agents for, (2) synthesize the combined "
    "findings across all of them (do not just repeat each result in turn), and (3) give concrete "
    "recommended next actions or decisions. This is the user-facing deliverable — keep it clear "
    "and actionable."
)
# Synthetic continuation injected after a recoverable tool refusal (host-gate
# policy deny or the read-only bash gate) ended a turn early. Carries the
# refusal reason back to the model so it can adapt instead of stalling for the
# user. Rendered as an "inject" message (not a user bubble) and never mirrored
# to a linked Slack thread as user input.
REFUSAL_RECOVERY_PREFIX = "[Tool refusal — automatic recovery]"
# Synthetic continuation injected after a genuinely-wedged (stale) turn was
# detected + reset. Tells the model its previous turn was interrupted by a
# system stall — NOT the user — and to resume from its last committed step
# rather than restart. Rendered as an "inject" message (not a user bubble) and
# never mirrored to a linked Slack thread as user input.
STALE_RECOVERY_PREFIX = "[Stalled turn — automatic recovery]"
# Synthetic continuation injected after the per-session watchdog judged an
# in-flight tool dead/stuck and cancelled the session. Unlike the legacy path
# (which re-queued the ORIGINAL user message verbatim — restarting the whole
# task from scratch), this hands the model the stall context so it can check
# partial results and continue. Rendered as an "inject" message (not a user
# bubble) and never mirrored to a linked Slack thread as user input.
TOOL_STALL_RECOVERY_PREFIX = "[Tool stall — automatic recovery]"
# Prefix on the runner-injected CONTINUE that resumes a turn cut short by a
# transient backend 5xx after tokens/tools had already streamed. The body lives
# in chat_utils as _POSTTOKEN_RECOVER_MSG; the prefix is here so all five
# recovery markers share one home and the frontend has one list to mirror.
POSTTOKEN_RECOVERY_PREFIX = "[Interrupted turn — automatic recovery]"
# Prefix on the runner-injected nudge that breaks a repeated empty-generation
# pattern (the model returned no output twice). Body: _EMPTY_AUTO_CONTINUE_MSG.
EMPTY_RESPONSE_RECOVERY_PREFIX = "[Empty response — automatic recovery]"
# Prefix on the continuation injected when the USER pressed Continue on an
# interrupted turn. Body: _MANUAL_RESUME_MSG in chat_utils. Named into the
# *_RECOVERY_PREFIX family because test_recovery_card_prefixes.py keys the
# cross-language drift guard on that suffix — a marker outside the family is
# invisible to it, and the card would silently render machine prose as a bubble.
# The VALUE is what carries the user-facing meaning, and it deliberately does NOT
# say "automatic recovery" like the five above: a person pressed the button, and
# the card must not claim the system recovered by itself.
MANUAL_RESUME_RECOVERY_PREFIX = "[Continue — requested by the user]"


def should_queue_refusal_recovery(
    refusal_reasons: list, stopping: bool, needs_reset: bool, stop_reason: str
) -> bool:
    """Decide whether to auto-queue a refusal-recovery prompt after a turn.

    Returns False (skip recovery) when:
    - No refusals occurred
    - A stop is still in progress
    - A session reset is already re-queuing
    - The turn was cancelled by the user (not a policy block)
    """
    return bool(
        refusal_reasons
        and not stopping
        and not needs_reset
        and stop_reason != STOP_REASON_CANCELLED
    )


def build_refusal_recovery_prompt(refusals: list[tuple[str, str]]) -> str:
    """Build the body of an automatic continuation after a recoverable tool refusal.

    When a tool call is refused for a recoverable, system-side reason — a
    host-gate policy deny or the read-only bash safety gate — kiro-cli ends the
    turn early with an attribution-free "tool uses were interrupted" marker. The
    refusal reason is otherwise surfaced only to the dashboard pill and the SEL
    audit log, never to the model, so the agent stalls and waits for the user.

    ``refusals`` is a list of ``(tool_title, reason)`` tuples recorded during the
    turn (already redacted by the caller). The returned text hands those reasons
    back to the model and frames the block as a system policy decision — NOT a
    user cancellation — so the agent can adapt (an allowed alternative, a
    different tool) or stop on its own with a reason. The caller prepends
    :data:`REFUSAL_RECOVERY_PREFIX`. Returns "" if there is nothing to recover.

    Lives here (a leaf module that owns the prefix) rather than in context.py so
    chat_runner can import it at module top without a circular import. There is
    deliberately no retry cap: the model decides when to stop, and the user's
    Stop button remains the hard breaker.
    """
    if not refusals:
        return ""
    lines = [
        "One or more tool calls in your previous turn were blocked by a Kiro Crew "
        "safety policy, which ended the turn early. This was NOT a user action — "
        "do not treat it as a cancellation or interruption by the user.",
        "",
        "Blocked:",
    ]
    for title, reason in refusals:
        lines.append(f"  - {title}: {reason}" if reason else f"  - {title}")
    lines += [
        "",
        "Decide how to proceed: use an allowed alternative (for a shell command, "
        "a read-only variant), a different tool, or — if the block is correct and "
        "you genuinely cannot proceed — say so and stop. Otherwise continue the "
        "task where you left off.",
    ]
    return "\n".join(lines)


def build_stale_recovery_prompt() -> str:
    """Body of the continuation injected after an auto-recovered stalled turn.

    A previous turn wedged: the ACP layer detected a genuinely stale turn (total
    stdout+stderr silence past the timeout), probed it via ``session/cancel``, got
    no ack, and the dashboard reset the session. The prior work already committed
    to the conversation is restored by ``session/load`` resume; this nudge tells
    the model to CONTINUE from that last committed step rather than restart the
    task from scratch. The caller prepends :data:`STALE_RECOVERY_PREFIX`. Framed
    as a system stall — NOT a user cancellation — so the agent doesn't stop.
    """
    return (
        "Your previous turn was interrupted by a system stall and has been "
        "automatically recovered. This was NOT a user action — do not treat it "
        "as a cancellation or interruption by the user. The work you already "
        "completed is preserved in the conversation above. Continue from where "
        "you left off and finish the task; do not restart it or repeat steps "
        "that already succeeded."
    )


# Shell output-redirection target, e.g. `> build.log` / `>> build.log`. The
# character class excludes `&` so fd-dup forms (`2>&1`, `>&2`) self-exclude.
_REDIRECT_TARGET_RE = re.compile(r">>?\s*([^\s;|&]+)")


def extract_log_redirect_target(command: str) -> str:
    """The first real file a shell command redirects output into, or "".

    Used by the tool-stall recovery nudge: when a long command redirected its
    output (long commands typically redirect, e.g. ``> build.log 2>&1``), the model
    should inspect that file's tail instead of blindly re-running the command.
    ``/dev/null`` and fd-dups (``2>&1``) are ignored.
    """
    for m in _REDIRECT_TARGET_RE.finditer(command or ""):
        target = m.group(1).strip("\"'")
        if not target or target == "/dev/null":
            continue
        return target
    return ""


def build_tool_stall_recovery_prompt(
    tool_title: str,
    idle_secs: int,
    command: str = "",
    stuck_input: bool = False,
) -> str:
    """Body of the continuation injected after a watchdog tool-stall cancel.

    The per-session watchdog judged an in-flight tool dead (its process exited
    without a result frame), stuck on interactive input, or opaque past the
    UNKNOWN budget, and cancelled the session's turn. This nudge is a SYSTEM
    action — NOT a user cancellation — and replaces the legacy behavior of
    re-queuing the original user message verbatim (which restarted the entire
    task and re-ran the very command that stalled). The caller prepends
    :data:`TOOL_STALL_RECOVERY_PREFIX`.
    """
    idle_mins = max(1, round(idle_secs / 60))
    tool_label = tool_title or "a tool call"
    lines = [
        f"Your previous turn stalled: {tool_label} produced no response for "
        f"~{idle_mins} minute(s) and the turn was ended by a Kiro Crew watchdog. "
        "This was NOT a user action — do not treat it as a cancellation or "
        "interruption by the user.",
        "",
        "Before doing anything else, check whether the tool actually completed "
        "or left partial results — do NOT blindly re-run the whole task or "
        "repeat steps that already succeeded.",
    ]
    log_target = extract_log_redirect_target(command)
    if log_target:
        lines += [
            "",
            f"The command's output was redirected to `{log_target}` — inspect it "
            "with tail (last ~50 lines); do NOT cat the whole file.",
        ]
    if stuck_input:
        lines += [
            "",
            "The command appeared to be waiting for interactive input it will "
            "never receive. Re-run it non-interactively (e.g. with -y, "
            "--no-input, or </dev/null) instead of repeating it as-is.",
        ]
    lines += [
        "",
        "Then continue the task from where you left off.",
    ]
    return "\n".join(lines)


# [OPTIONS: a | b | c] — the marker ends a LINE here, so use the MULTILINE/
# single-line canonical parser. Defined once in constants.py (shared with
# slack/format.py and the renderer surfaces) so the ReDoS-hardened grammar can
# never drift between copies; see OPTIONS_RE_LINE for the full rationale
# (tempered body, ``\n`` exclusion under MULTILINE). Per-choice whitespace is
# stripped by the caller; dashboard pills and Slack buttons parse OPTIONS
# identically because they share this exact object.
_OPTIONS_RE = OPTIONS_RE_LINE


def _redact(text: str) -> str:
    """Sanitise LLM output before surfacing to dashboard."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _parse_options(text: str) -> list[str]:
    """Extract pipe-separated choices from the LAST [OPTIONS: A | B | C] in text."""
    matches = _OPTIONS_RE.findall(text)
    if not matches:
        return []
    parts = [p.strip() for p in matches[-1].split("|")]
    return [p for p in parts if p]


VALID_MEMORY_MODES = ("persistent", "incognito", "temporary")


def _ascii_slot_key(name: str) -> str:
    """Return *name* with any character outside printable ASCII replaced by ``-``.

     A slot key becomes the session key (``dashboard:{slot.key}``) that
     kirocrew-core sends as the ``X-Session-Key`` HTTP header on every gateway
     call. Header values are latin-1 per RFC 7230, so a non-latin-1 char (e.g.
     an em-dash from a title-derived slot name) would abort every tool call
    . ASCII control characters (notably CR/LF) are excluded too, so
     a name can never inject into or split the header. Idempotent;
     printable-ASCII names — including the auto-generated ``chat-N-<ts>`` keys —
     are returned unchanged. (Path-separator/traversal containment for keys later
     used as filesystem paths is enforced separately at the persistence layer.)
    """
    return re.sub(r"[^\x20-\x7e]", "-", name)


# Characters that survive the history layer's ``_safe_key()`` filename fold
# (``re.sub(r"[^\w\-.]", "_", key)``). ``re.ASCII`` pins ``\w`` to
# ``[a-zA-Z0-9_]`` — the input is already ASCII-folded, so this matches what
# ``_safe_key`` produces byte-for-byte.
_SLOT_KEY_FILENAME_UNSAFE_RE = re.compile(r"[^\w\-.]", flags=re.ASCII)


def _normalize_slot_key(name: str) -> str:
    """Return *name* folded to the exact charset of a persisted session filename.

    Guarantees the invariant a restart depends on: for any input,
    ``_safe_key(_history_key_for(key))`` == ``f"dashboard_{key}"`` — i.e. the
    slot key equals its JSONL filename stem minus the ``dashboard_`` prefix.

    Three steps compose: strip a ``dashboard:``/``dashboard_`` transport
    prefix (a full session key or filename stem sometimes reaches slot-name
    positions; ``_history_key_for`` strips the same prefixes when building the
    history key, so such names already share one transcript with their bare
    form and must share one slot), then :func:`_ascii_slot_key` (header
    safety), then a filename fold using the same character class as
    ``history._safe_key``.

    Without the filename fold, a display-style slot name (e.g.
    ``Artifact: My Doc`` from the artifact iterate flow) diverges from its
    sanitized filename stem. After a gateway restart, ``restore_open_slots``
    rehydrates the raw key from ``open_slots.json`` while
    ``restore_recent_sessions`` derives a second slot from the filename stem —
    the dedup guards compare mismatched strings, so the user sees two
    identical sidebar sessions backed by one transcript, and the next
    ``_persist_open_slots`` flush cements both keys. Idempotent;
    auto-generated ``chat-N-<ts>`` keys are returned unchanged.
    """
    if name.startswith("dashboard:"):
        name = name[len("dashboard:") :]
    while name.startswith("dashboard_"):
        name = name[len("dashboard_") :]
    return _SLOT_KEY_FILENAME_UNSAFE_RE.sub("_", _ascii_slot_key(name))


class _ChatSlot:
    """Independent chat session that runs server-side."""

    __slots__ = (
        "_source_links_cache",
        "_source_links_revision",
        "key",
        "title",
        "agent",
        "model",
        "reasoning_effort",
        "mode",
        "workspace",
        "project",
        "created_at",
        "messages",
        "total_messages",
        "task",
        "event",
        "_pending",
        "_queue",
        "_approval_futures",
        "_trust",
        "_trust_reads",
        "_trusted_patterns",
        "_titled",
        "_title_in_flight",
        "_title_retry_pending",
        "_artifact",
        "_resumed_count",
        "_todo",
        "_on_message",
        "_has_reader",
        "_stop_state",
        "_stop_event_id",
        "_stop_escalated_card_id",
        "_pending_reset_history_key",
        "_dirty_flag",
        "_dirty_gen",
        "_orch_tracker",
        "_auto_run",
        "_in_stage_execution",
        "_last_turn_auth_required",
        "_recovery_chat_triggered",
        "_stage_titles",
        "_stage_descriptions",
        "_plan_goal",
        "_slack_linked",
        "_slack_channel",
        "_slack_thread_ts",
        "channel_origin",
        "folder_id",
        "_folder_changed",
        "_folder_suggested",
        "pinned",
        "tags",
        "_pending_subagent_failures",
        "_pending_synthesis",
        "_synthesis_inflight",
        "_subagent_deliveries_inflight",
        "_subagents_inline_collected",
        "_recovery_retrigger_count",
        "_prompt_busy_retries",
        "_acp_pipe_death_retries",
        "_stale_recovery_retries",
        "_tool_stall_retries",
        "_transient_5xx_retries",
        "_posttoken_retry_used",
        "_empty_response_retries",
        "_batch_rejected",
        "_compaction_fail_streak",
        "_compaction_fail_cooldown_until",
        "color_index",
        "color_theme",
        "theme_consent",
        "theme_consent_sha",
        "memory_mode",
        "_ephemeral",
        "_pending_context",
        "_app",
        "_pending_variants",
        "_lock",
        "forked_from",
        "_fork_lock",
        "_tab_id",
        "_channel_window_mtime",
        "_disk_older_count",
        "_disk_window_len",
        "_disk_tail_ts",
        "_frozen_prefix_cache",
        "_pending_rewrite",
        "_file_changes",
        "linked_session_key",
        "_side",
        "_acp_client",
        "_steer_segment_cut",
        "_native_subagent_tracker",
        "_native_subagent_output",
        "_pending_steers",
    )

    def __init__(
        self,
        key: str,
        title: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str = "persistent",
        ephemeral: bool = False,
    ) -> None:
        self.key = key
        self.title = title or key
        self.agent = agent
        self.model = model
        # Reasoning effort: "" = provider default, else one of low/medium/high/max.
        # Currently consumed by an alternate ACP backend (--effort flag); ACP wired later.
        self.reasoning_effort: str = ""
        # "" = default chat, "orchestrator" = orchestrated chat
        self.mode = mode
        self.workspace = workspace
        self.project: str = ""
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.messages: list[dict[str, Any]] = []
        # (content revision, links) cache for the sidebar PR chips scan.
        self._source_links_revision = 0
        self._source_links_cache: tuple[tuple[int, int], list[dict]] | None = None
        self.total_messages: int = 0  # lifetime count (survives trimming)
        self.task: asyncio.Task | None = None  # type: ignore[type-arg]
        self.event = asyncio.Event()
        self._pending: list[dict[str, str]] = []
        self._queue: list[dict[str, str]] = []  # [{"id": uuid, "content": str}, ...]
        self._approval_futures: dict[str, asyncio.Future[str]] = {}  # type: ignore[type-arg]
        self._trust: bool = False  # auto-approve tools for this slot
        self._trust_reads: bool = False  # auto-approve read-only bash commands
        self._trusted_patterns: set[str] = set()  # session-scoped fnmatch globs
        self._titled: bool = False  # True once a title has been assigned
        # Guards against concurrent LLM auto-title attempts (on-send trigger vs
        # the end-of-turn chat_done trigger racing on the same slot).
        self._title_in_flight: bool = False
        # Records a chat_done retry that arrived during the on-send attempt.
        self._title_retry_pending: bool = False
        # Artifact companion binding: set when this slot is a
        # companion chat session for an artifact (slug). At most one
        # non-archived slot per slug by convention — the frontend flow
        # maintains the invariant (archive-then-create); the backend accepts
        # any valid slug and does not enforce uniqueness. This IS serialized
        # (to_dict) and persisted (history meta) — the dashboard resolves the
        # active binding from the slots snapshot, and the binding must survive
        # gateway restarts.
        self._artifact: str = ""
        self._resumed_count: int = 0  # messages loaded from history on resume
        # Agent-authored TODO list, replaced wholesale from each todo_list tool
        # result (every command echoes the full list, so there is nothing to
        # merge). Shape: {description: str, tasks: [{id, text, completed}]}.
        # None = the agent has never used its todo tool in this slot, which the
        # UI renders as "no pill" rather than "an empty list".
        self._todo: dict[str, Any] | None = None
        # Callback for broadcasting messages via global SSE
        self._on_message: object | None = None  # Callable[[str, dict], None] | None
        self._has_reader: bool = False  # True when HTTP SSE stream is draining
        self._stop_state: str = "idle"  # 'idle' | 'soft_pending' | 'killing'
        self._stop_event_id: str | None = None  # transcript message id for in-flight stop
        # Id of the stop card the user escalated to a hard kill, or None. Kept
        # separate from `_stop_state` because turn teardown resets that back to
        # "idle" (see the `_stopping` setter below), which would erase the
        # escalation and let a late cooperative ack relabel the card as a clean
        # stop. Holds an id rather than a bool so the marker cannot leak onto a
        # later card: a boolean left set would make the NEXT card's cooperative
        # ack defer to a hard callback that never fires, stranding it at
        # "stopping". Every card has a fresh uuid, so a stale id simply stops
        # matching and no card-open path has to remember to clear it.
        self._stop_escalated_card_id: str | None = None
        # Set by api_chat_slot_project; consumed in _run_chat instead of
        # inline because the endpoint can be reached from inside the kiro-cli
        # process group via the set_project MCP tool.
        self._pending_reset_history_key: str | None = None
        self._dirty_flag: bool = False  # True when messages changed since last flush
        # Bumped by the _dirty setter on every True. Lets the periodic flush tell
        # "the True I started this save under" from "a NEW True set during it".
        self._dirty_gen: int = 0
        self._orch_tracker: Any = None  # OrchestrationTracker, set by gateway
        self._auto_run: bool = False  # "Go All" — skip stage gates
        # True only while _stage_loop is driving a stage-execution turn. Gates
        # the end-of-turn plan detector so a stage turn whose output happens to
        # contain plan-like text cannot re-arm / re-count the plan (which
        # corrupted the stage total and produced "Stage N of M" over-runs).
        # It ALSO gates mid-plan message handling: while set, api_chat queues a
        # user message (chip card) even when slot.task is momentarily idle between
        # stages, and _start_next_queued_turn HOLDS user messages (recovery/system
        # still drain) until the plan ends — so autopilot reuses the normal-chat
        # queue/chip path without changing slot.task / slot.running semantics.
        self._in_stage_execution: bool = False
        # Set by _run_chat's teardown to that turn's ACP auth-required outcome, so
        # the orchestrator _stage_loop can mirror the "hold the queue for
        # post-login resume" guard on its end-of-plan handoff (a signed-out CLI
        # must not pop the held follow-up into another auth failure).
        self._last_turn_auth_required: bool = False
        self._recovery_chat_triggered: bool = False  # guard against concurrent failure recovery
        self._stage_titles: list[str] = []  # stage titles extracted from plan
        self._stage_descriptions: list[list[str]] = []  # bullet points per stage
        self._plan_goal: str = ""  # goal from 📋 Plan for: header
        self._slack_linked: bool = False  # True when linked to a Slack thread
        self._slack_channel: str = ""
        self._slack_thread_ts: str = ""
        self.folder_id: str = ""  # project folder assignment
        self._folder_changed: bool = False  # re-inject [FOLDER] breadcrumb next turn after move
        # One-shot claim for the post-titling folder suggestion (see
        # chat_folder_suggest.maybe_suggest_folder). In-memory only: a restored
        # slot is already titled, so the suggestion hook never re-fires for it
        # and a reset flag cannot produce a second card.
        self._folder_suggested: bool = False
        self.pinned: bool = False  # pinned to top of sidebar
        self.tags: list[str] = []  # assigned tag ids (see DashboardState._tags)
        self._pending_subagent_failures: list[str] = []
        # Fix 2 (B1): armed by gateway when the LAST sub-agent of a fan-out
        # completes; consumed once by chat_runner's drain/idle branch to fire a
        # single post-fan-out synthesis turn. Cleared if a user message drains
        # first (user takes over).
        self._pending_synthesis: bool = False
        # True while chat_runner owns the one readiness-wait/synthesis task.
        # Kept separate from _pending_synthesis so readiness loss does not
        # consume the one-shot request or permit duplicate waiters.
        self._synthesis_inflight: bool = False
        # Fix 2 (B1) race guard: number of sub-agent completion deliveries
        # currently in flight for this slot (incremented in gateway._subagent_done
        # from entry until the completion is queued/launched). The synthesis
        # fire-gate requires this to be 0 so a concurrently-finishing sibling
        # can't let an earlier turn fire synthesis before its result lands.
        self._subagent_deliveries_inflight: int = 0
        # IDs of sub-agents whose results were already delivered inline via the
        # blocking spawn_sub_agents MCP tool.  _subagent_done skips injection
        # for these to prevent a duplicate turn that clobbers [OPTIONS:] buttons.
        self._subagents_inline_collected: set[str] = set()
        self._recovery_retrigger_count: int = 0
        self._prompt_busy_retries: int = 0
        self._acp_pipe_death_retries: int = 0
        # Auto-recovery of a genuinely-wedged (stale) turn: bumped when the ACP
        # layer signals STOP_REASON_STALE_RECOVER; bounded (3) so a permanently
        # broken session surfaces "start a new chat" instead of looping. Reset on
        # a completed turn (alongside the other retry budgets).
        self._stale_recovery_retries: int = 0
        # Tool-stall recovery: bumped when the ACP layer ends a turn with
        # STOP_REASON_TOOL_STALL. A SEPARATE budget from pipe-death (the legacy
        # path charged stalls against _acp_pipe_death_retries and re-queued the
        # original message verbatim — one false positive burned the whole
        # session budget). Bounded (3); reset on a completed turn.
        self._tool_stall_retries: int = 0
        # Transient backend 5xx (InternalServerError / DispatchFailure /
        # ConnectionReset) retries on the interactive stream path. Distinct
        # budget from prompt-busy / pipe-death; reset on a completed turn.
        self._transient_5xx_retries: int = 0
        # One-shot guard for the post-token (text-only) transient retry: a turn
        # that has already streamed answer tokens may be re-prompted at most
        # ONCE on a transient 5xx (and only when no tool call fired). Reset on a
        # completed turn alongside _transient_5xx_retries.
        self._posttoken_retry_used: bool = False
        self._empty_response_retries: int = 0
        self._batch_rejected: bool = False
        # Per-turn compaction-status failure tracking (Mesh compaction-spam
        # fix). Distinct from SessionManager._compact_cooldown_until, which
        # only gates the *proactive* session-level auto-compact trigger —
        # this gates the per-turn EVENT_COMPACTION_STATUS notice path in
        # chat_runner, which previously had no backoff at all and could
        # append one near-identical "Compaction failed: unknown error"
        # message per turn indefinitely.
        self._compaction_fail_streak: int = 0
        self._compaction_fail_cooldown_until: float = 0.0
        self.color_index: int | None = None
        self.color_theme: str = ""
        # Explicit user consent for the active INSTALLED theme's experience
        # layer (persona injection is gated on this; fail-closed default).
        self.theme_consent: bool = False
        # Content-bound persona consent: sha256 hex of the installed pack's
        # persona text the user granted in the consent modal. Persona injection
        # requires this to match the persona read from disk (fail-closed None).
        self.theme_consent_sha: str | None = None
        if memory_mode not in VALID_MEMORY_MODES:
            raise ValueError(
                f"invalid memory_mode {memory_mode!r}, must be one of {VALID_MEMORY_MODES}"
            )
        self.memory_mode: str = memory_mode
        self._ephemeral: bool = ephemeral  # Incognito mode: no memory writes
        self._pending_context: list[dict[str, Any]] = []
        self._app: str = ""  # App identity tag (App Kit §5.2)
        # Regenerate feature: variants pending attachment to next finalized assistant message
        self._pending_variants: list[dict] = []
        self._lock = asyncio.Lock()
        self.forked_from: str | None = None  # parent slot key if this is a fork
        self._fork_lock: asyncio.Lock = asyncio.Lock()  # serialises concurrent forks on this slot
        self._tab_id: str = ""  # permanent tab identity for cross-restart session chaining
        # Transcript mtime the in-memory window was last brought up to date
        # against. Only meaningful for a slot bound to a channel session, whose
        # transcript the channel also writes to (see channel_slots).
        self._channel_window_mtime: float = 0.0
        self._disk_older_count: int = (
            0  # count of disk messages OLDER than in-memory window (stable, set at restore/resume)
        )
        # Count of in-memory window messages the LAST save persisted to disk
        # (the on-disk window region). Trimming may only fold a leading window
        # message into the frozen prefix once it is known to be on disk; this
        # watermark is what makes the #8 trim credit safe. It is NOT a fragile
        # "what to append" counter — saves always re-serialize the WHOLE window.
        self._disk_window_len: int = 0
        # The newest ``ts`` seen on disk at the last save, INCLUDING rows this
        # slot never observed. A subagent, cron, or CLI appending to a session a
        # live tab also has open writes rows that ``_save_slot_to_history``
        # preserves as "foreign" without ever folding them into ``messages`` --
        # so the window is not a superset of the file, and flooring the next
        # append on the window tail alone can tie a foreign row's timestamp.
        # Cached rather than read per append: consulting the file here would put
        # a stat plus a bounded read on the event loop, which AUTOSDE's
        # no-blocking-call-on-event-loop rule forbids. Refreshed at the save
        # boundary, where the lock is already held and the foreign lines are
        # already parsed.
        self._disk_tail_ts: str | None = None        # Cached frozen-prefix bytes for the append-safe save model.
        # The session file is FROZEN-PREFIX (the first _disk_older_count on-disk
        # message lines, OLDER than the in-memory window) + a fresh re-serialize
        # of the whole window. The prefix is never rewritten, so a restart that
        # loaded only a recent window can no longer destroy older history. This
        # caches the prefix bytes keyed by (path-mtime, path-size,
        # _disk_older_count) so a 5s flush is O(window), not O(file). The
        # (mtime, size) pair also doubles as the "did another process write this
        # file since we last saved?" signal that gates the cross-process
        # foreign-append merge. See chat_persistence._save_*.
        self._frozen_prefix_cache: tuple[float, int, int, str, list[str]] | None = None
        # Set by rewind/regenerate after they TRUNCATE the window. While set,
        # _save_slot_to_history takes the archive-safe rewrite path so the
        # dropped tail is archived — even if the inline rewrite save failed:
        # the next 5s flush then retries the rewrite instead of silently
        # overwriting (the default save skips archiving). Cleared on a
        # successful rewrite save.
        self._pending_rewrite: bool = False
        self._file_changes: list[dict[str, str]] = (
            []
        )  # [{path, content}] before-snapshots accumulated per turn for file-chip diffs
        self.linked_session_key: str = ""  # when set, _run_chat uses this as session key
        # True only when this slot was created to DISPLAY a conversation that
        # already lives in a channel transcript (the reconciler surfacing a
        # thread, a restore, a History resume). It is what separates such a tab
        # from a dashboard slot that merely happens to be NAMED like one --
        # a filename-shaped name is not provenance, and inferring it from the
        # name would let `POST /api/chat/slots` with a colliding `slack_<ts>`
        # name write a fresh conversation into an existing thread's transcript.
        self.channel_origin: bool = False
        self._side: SideState | None = None
        # Live inner AcpClient for the in-flight turn, published by _run_chat at
        # turn start and cleared in its finally. Lets a concurrent request (the
        # dashboard steer handler) reach the running session's client to inject
        # a mid-turn steer. None when idle.
        self._acp_client = None
        # Sync callable published by _run_chat alongside _acp_client (cleared in
        # the same finally): flushes the turn's accumulated text as a finalized
        # assistant segment NOW. The steer handler calls it right BEFORE
        # persisting the steer user message, so the transcript order is
        # [assistant(pre-steer), user(steer), assistant(post-steer)] — matching
        # what the client rendered live — instead of the whole segment landing
        # BELOW the steer bubble at end-of-turn (and stranding the pre-steer
        # chunk entries above it, which _flush_segment's trailing-run walk could
        # then never reclaim). None when idle.
        self._steer_segment_cut: Callable[[], None] | None = None
        # Native kiro-cli subagents run inside the parent ACP turn. Keep their
        # live and terminal state on the slot so reconnects can hydrate cards.
        self._native_subagent_tracker: dict[str, dict[str, Any]] = {}
        self._native_subagent_output: dict[str, list[str]] = {}
        # Mid-turn steers handed to the backend but not yet confirmed consumed
        # (no steering_consumed / EVENT_STEER_CONSUMED echo yet). Appended by
        # the dashboard steer handler BEFORE the steer RPC's await (so a turn
        # dying mid-write still sees it), settled by _run_chat when the
        # consumed echo arrives (matched against the echo's snapshot text),
        # and — the point of the mechanism — REQUEUED as ordinary queue cards
        # by _run_chat's finally when the turn dies first (stall-cancel, user
        # STOP, error). Without this, a steer swallowed by a dying turn
        # vanished with no trace (see the requeue site).
        self._pending_steers: list[str] = []

    @property
    def _dirty(self) -> bool:
        """True while this slot holds state not yet confirmed on disk.

        Deliberately a property so that ``_dirty_gen`` is bumped centrally by the
        ~20 existing ``slot._dirty = True`` sites without editing any of them.

        Two independent readers depend on this staying True for the WHOLE
        duration of a save, not just until the save starts:

        * ``chat_fork`` treats it as "unpersisted in-memory state exists". A False
          read makes it skip both the in-memory tail append and the durable
          pre-fork save, so it forks from stale disk and the new session silently
          omits the newest messages.
        * ``_save_slot_to_history``'s resumed-slot no-op guard skips when
          ``_resumed_count > 0 and len(window) <= _resumed_count and not _dirty``;
          its comment states the assumption directly — "a dirty slot whose length
          merely equals the resumed count still falls through ... otherwise an
          in-place edit after resume would never reach disk."

        So the periodic flush must NOT clear this early to protect itself against
        clobbering a concurrent mark. It compares ``_dirty_gen`` instead.
        """
        return self._dirty_flag

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._dirty_flag = value
        if value:
            # Monotonic: only ever advances, so a wrapped-around compare is
            # impossible and a missed bump can only cause an extra (harmless)
            # flush, never a skipped one.
            self._dirty_gen += 1

    @property
    def _plan_stage_count(self) -> int:
        return len(self._stage_titles)

    @property
    def _stopping(self) -> bool:
        return self._stop_state != "idle"

    @_stopping.setter
    def _stopping(self, value: bool) -> None:
        self._stop_state = "soft_pending" if value else "idle"

    def set_todo(self, todo: dict[str, Any] | None) -> bool:
        """Replace the slot's TODO snapshot. Returns True when it changed.

        The return value gates the live websocket push so an unchanged list —
        common, because a single turn can echo the same snapshot on several
        tool results — does not fan a redundant broadcast out to every socket.
        """
        normalised: dict[str, Any] | None = None
        if isinstance(todo, dict):
            tasks = todo.get("tasks")
            normalised = {
                "description": str(todo.get("description") or ""),
                "tasks": list(tasks) if isinstance(tasks, list) else [],
            }
        if normalised == self._todo:
            return False
        self._todo = normalised
        return True

    def todo_payload(self) -> dict[str, Any] | None:
        """The serialized TODO snapshot with server-derived progress counts.

        ``completed``/``total`` are computed here rather than in the browser so
        the pill's "N of M" cannot drift from the list it labels. ``current`` is
        the first not-completed task's text — kiro-cli's todo model is a plain
        ``completed`` boolean with NO in-progress state, so "current task" is
        this derivation, not something the agent reports.
        """
        if self._todo is None:
            return None
        tasks = [t for t in self._todo.get("tasks", []) if isinstance(t, dict)]
        completed = sum(1 for t in tasks if t.get("completed"))
        current = next((str(t.get("text") or "") for t in tasks if not t.get("completed")), "")
        return {
            "description": self._todo.get("description", ""),
            "tasks": tasks,
            "completed": completed,
            "total": len(tasks),
            "current": current,
        }

    def note_disk_tail(self, *candidates: str | None) -> None:
        """Record the newest ``ts`` known to be ON DISK for this session.

        The save boundary is the only place a slot can learn about a row it never
        observed (see ``_disk_tail_ts``), so it calls this with whatever it just
        wrote -- foreign rows included. Keeping the update here rather than
        assigning the attribute from the persistence module means the **monotone**
        rule lives with the field it guards: the floor may only ever move FORWARD.
        A save that moved it backwards would re-open the same-``ts`` tie the floor
        exists to prevent, and unparseable candidates are skipped rather than
        ranked (``latest_transcript_ts``), so one corrupt row cannot capture it.
        """
        self._disk_tail_ts = latest_transcript_ts(self._disk_tail_ts, *candidates)

    def append(
        self,
        role: str,
        content: str,
        cls: str = "",
        ts: str = "",
        *,
        broadcast: bool = True,
        broadcast_user: bool = False,
        meta: dict | None = None,
    ) -> None:
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "cls": cls,
            # This window is re-serialized into the SAME transcript file that
            # ConversationLog.append writes, so it owes the reader the same
            # ordering guarantee: strictly after the row before it, even when
            # the clock does not tick between two appends. An explicit *ts*
            # (a row replayed from a channel transcript) is preserved verbatim
            # -- rewriting it would reorder the replay it came from.
            #
            # The floor is the later of the window tail and the last on-disk tail
            # this slot was told about, because the window is NOT a superset of
            # the file: a row written by another process is preserved as a
            # foreign line without entering ``messages``, so flooring on the
            # window alone leaves it un-ordered-against. Both candidates are
            # in-process reads -- no file I/O on the event loop.
            "ts": ts
            or monotonic_transcript_ts(
                latest_transcript_ts(
                    self.messages[-1].get("ts") if self.messages else None,
                    self._disk_tail_ts,
                ),
                datetime.now(timezone.utc),
            ),
        }
        if meta:
            msg["meta"] = meta
        # Stamp a per-row delivery identity. A client sees the SAME row through
        # two doors — the slot-detail HTTP rebuild and the live `chat_message`
        # broadcast — and must be able to tell "this row again" from "another row
        # that happens to look identical". `ts` cannot answer that: a coarse OS
        # clock stamps two rows appended in the same tick identically (the same
        # collision mergePreservedClientTs already guards), and content cannot
        # either, since two identical messages are legitimate. So identity is an
        # explicit id, minted once here, carried on the message dict, and thus
        # present on every path that ships it: persisted by _build_message_entry,
        # restored with the rest of `meta`, broadcast as `payload["meta"]`, and
        # returned by _prepare_messages.
        #
        # Random rather than a per-slot counter deliberately: a counter rebased
        # after a restore could reissue an id a restored row already holds, and a
        # colliding id makes a client DROP a real message. There is no such
        # failure mode for a random id.
        #
        # A caller-supplied `mid` (a row replayed from disk) is preserved — the
        # id must survive the round trip or a post-restart redelivery of that row
        # would not be recognisable.
        #
        # Skipped for the wire-only roles: `chunk` is appended once per streamed
        # token and `done`/`streaming` are internal markers. None of them is ever
        # broadcast as a `chat_message` (the broadcast below excludes them) or
        # persisted (`_TRANSIENT_ROLES`), so an id would buy nothing and cost a
        # uuid4 plus a dict on the hottest path in the runner.
        if role not in _WIRE_ONLY_ROLES and not (
            isinstance(msg.get("meta"), dict) and msg["meta"].get("mid")
        ):
            existing = msg.get("meta")
            msg["meta"] = {
                **(existing if isinstance(existing, dict) else {}),
                "mid": f"m-{uuid.uuid4().hex[:16]}",
            }
        self.messages.append(msg)
        self.invalidate_source_links()
        self.total_messages += 1
        self._dirty = True
        self._pending.append(msg)
        self.event.set()
        # Broadcast via global SSE when no HTTP stream reader is active
        # Skip: chunk (too noisy), done (internal). A "user" row is skipped by
        # DEFAULT because the composer that submitted it already rendered it
        # optimistically -- but that is only true of a message typed in this
        # dashboard. A row replayed from a CHANNEL transcript was typed in
        # Slack, so nothing rendered it here; those callers pass
        # ``broadcast_user=True`` or the message stays invisible until a full
        # transcript reload, arriving AFTER the reply it came before.
        if (
            broadcast
            and self._on_message
            and role not in ("chunk", "done")
            and (role != "user" or broadcast_user)
            and not self._has_reader
        ):
            self._on_message(self.key, msg)  # type: ignore[operator]
        # Trim old messages to bound memory usage
        if len(self.messages) > _MAX_SLOT_MESSAGES:
            excess = len(self.messages) - _MAX_SLOT_MESSAGES
            del self.messages[:excess]
            self._resumed_count = max(0, self._resumed_count - excess)
            # A trimmed leading window message may only join the frozen prefix
            # once it is actually on disk. Credit _disk_older_count only
            # for the persisted portion; the unpersisted overflow (should not
            # happen between 5s flushes) is logged rather than silently counted
            # as on-disk, which would have stranded those turns.
            persisted_trim = min(excess, self._disk_window_len)
            self._disk_older_count += persisted_trim
            self._disk_window_len = max(0, self._disk_window_len - excess)
            if persisted_trim < excess:
                logger.warning(
                    "Slot %s trimmed %d messages not yet flushed to disk; "
                    "they will not be recoverable from history",
                    self.key,
                    excess - persisted_trim,
                )
            # The frozen prefix grew → its cached bytes are stale.
            self._frozen_prefix_cache = None

    def drain(self) -> list[dict[str, str]]:
        """Return and clear pending messages."""
        out = self._pending[:]
        self._pending.clear()
        self.event.clear()
        return out

    def mark_permission_resolved(self, approval_id: str, decision: str = "approved") -> None:
        """Update stored permission message cls JSON with resolved flag."""
        for m in self.messages:
            if m.get("role") == "permission":
                try:
                    cls_data = json.loads(m.get("cls", ""))
                    if isinstance(cls_data, dict) and cls_data.get("request_id") == approval_id:
                        cls_data["resolved"] = decision
                        m["cls"] = json.dumps(cls_data)
                        return
                except (json.JSONDecodeError, TypeError):
                    pass

    def update_message(
        self,
        ts: str,
        *,
        content: str | None = None,
        meta: dict | None = None,
    ) -> dict | None:
        """Replace fields on a previously-appended message identified by ts.

        ``meta`` replaces the whole meta dict (so callers can also remove keys);
        pass ``None`` to leave it untouched. Returns the mutated message or None.
        """
        if not ts:
            return None
        for m in self.messages:
            if m.get("ts") == ts:
                if content is not None:
                    m["content"] = content
                    self.invalidate_source_links()
                if meta is not None:
                    m["meta"] = meta
                self._dirty = True
                return m
        return None

    # ── Queue helpers (dict-based queue items) ──

    def queue_append(self, content: str, kind: str = "") -> str:
        """Append a message to the queue. Returns the generated queue ID.

        ``kind`` is a structural origin tag (e.g. ``"synthetic_recovery"`` for
        runner-injected recovery instructions). Classification by metadata —
        not by content equality — survives queue transformations and cannot
        collide with user-typed text that happens to match an internal string.
        Empty string = plain user/system content (default).
        """
        qid = uuid.uuid4().hex[:12]
        self._queue.append({"id": qid, "content": content, "kind": kind})
        return qid

    def queue_insert(self, index: int, content: str, kind: str = "") -> str:
        """Insert a message at a specific queue position. Returns the queue ID.

        See :meth:`queue_append` for the ``kind`` structural origin tag.
        """
        qid = uuid.uuid4().hex[:12]
        self._queue.insert(index, {"id": qid, "content": content, "kind": kind})
        return qid

    def queue_pop(self, index: int = 0) -> dict[str, str]:
        """Pop a queue item by index. Returns {"id": ..., "content": ...}."""
        return self._queue.pop(index)

    def queue_remove_by_id(self, queue_id: str) -> str | None:
        """Remove a queue item by ID. Returns the content or None if not found."""
        for i, item in enumerate(self._queue):
            if item["id"] == queue_id:
                del self._queue[i]
                return item["content"]
        return None

    def queue_edit_by_id(self, queue_id: str, content: str) -> bool:
        """Replace the content of a queue item by ID. Returns True if found.

        Order is preserved — only the content of the matching item changes.
        """
        for item in self._queue:
            if item["id"] == queue_id:
                item["content"] = content
                return True
        return False

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def queue_depth(self) -> int:
        """Number of prompts currently queued behind the active turn."""
        return len(self._queue)

    @property
    def is_restricted(self) -> bool:
        """True when memory writes (consolidation, lessons) are blocked."""
        return self.memory_mode != "persistent"

    @property
    def blocks_reads(self) -> bool:
        """True when memory-context injection into this session is blocked."""
        return self.memory_mode == "temporary"

    def enqueue_or_run_prompt(
        self,
        prompt: str,
        run_chat_coro: Callable[[DashboardState, _ChatSlot, str], Coroutine[Any, Any, None]],
        state: DashboardState,
    ) -> bool:
        """Queue *prompt* if busy, otherwise start an agent turn.

        Encapsulates the queue-vs-run decision so callers don't need to
        touch ``_queue``, ``task``, or ``_background_tasks`` directly.
        Always registers :func:`_log_task_exception` to prevent silent failures.

        Returns ``True`` if the prompt started an agent turn, ``False`` if
        it was queued. Lets callers gate UI-visible side-effects (notifications,
        SSE pushes) on whether the prompt actually ran.

        Concurrency: the check (``self.running``) and mutation (``self.task = ...``)
        run synchronously on the asyncio event loop with no ``await`` between them,
        so two concurrent callers targeting the same slot cannot both observe
        ``running == False`` within a single loop iteration.
        """
        if self.running:
            self.queue_append(prompt)
            return False
        self.append("user", prompt, "msg msg-u")
        task = asyncio.create_task(run_chat_coro(state, self, prompt))
        self.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        task.add_done_callback(_log_task_exception)
        return True

    @property
    def display_title(self) -> str:
        """Title for UI display. Shows ``NEW_SESSION_TITLE`` while the slot is
        still on its untouched default key (untitled) — covering brand-new
        empty sessions and the window before the LLM title lands — otherwise
        the real title. Slots with a meaningful non-key title (plan, cron,
        fork, slack) are unaffected since their title != key.
        """
        if not self._titled and (
            not self.title or self.title == self.key or _SLOT_KEY_TITLE_RE.match(self.title)
        ):
            return NEW_SESSION_TITLE
        return self.title

    def invalidate_source_links(self) -> None:
        """Mark cached sidebar PR/MR/issue links stale after message-content mutation."""
        self._source_links_revision += 1

    def _pr_source_links(self) -> list[dict]:
        """PR/MR/issue links found in this slot's messages, for sidebar wayfinding chips.

        Linear scan (no regex backtracking) validated by the source-provider
        URL parser and cached behind an explicit content revision.

        Each entry carries a ``kind`` discriminator (``"change"`` for a pull or
        merge request, ``"issue"`` for an issue). Readers that only handle pull
        requests -- the chip-status cache and every path that reaches ``gh pr
        view`` -- must filter on it.
        """
        # Local import: handlers.source_providers does not import state, but
        # keep the dependency lazy to stay out of module-load ordering.
        from kiro_crew.dashboard.handlers.source_providers import (
            gitlab_hosts_generation,
            parse_source_url,
        )

        # The self-managed GitLab allowlist is part of the cache key, not just the
        # message revision: this runs synchronously and can execute BEFORE the
        # first off-loop allowlist load, in which case a self-managed URL is
        # rejected against the cold (empty) snapshot. Without the generation, that
        # rejection would stay memoized until the next message mutation and the
        # chip would be missing even after the allowlist loaded.
        cache_key = (self._source_links_revision, gitlab_hosts_generation())
        if self._source_links_cache and self._source_links_cache[0] == cache_key:
            return self._source_links_cache[1]

        stop_chars = set(" \t\n<>()[]{}\"'")
        found: dict[str, dict] = {}
        for msg in self.messages:
            if len(found) >= _MAX_SOURCE_LINKS_PER_SLOT:
                break
            if not isinstance(msg, dict) or msg.get("role") in _NON_DURABLE_SOURCE_LINK_ROLES:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or "https://" not in content:
                continue
            idx = 0
            while len(found) < _MAX_SOURCE_LINKS_PER_SLOT:
                idx = content.find("https://", idx)
                if idx == -1:
                    break
                end = idx
                while end < len(content) and content[end] not in stop_chars:
                    end += 1
                # Also strip markdown emphasis (**bold**, *italic*, `code`,
                # _underscore_, ~~strike~~): agent messages routinely wrap PR
                # URLs in emphasis and a trailing "**" fails the numeric tail
                # check. Valid PR/MR URLs end in a number, so these chars can
                # never belong to a legitimate link tail.
                candidate = content[idx:end].rstrip(".,!?;:*_~`")
                idx = end
                if (
                    "/pull/" not in candidate
                    and "/merge_requests/" not in candidate
                    and "/issues/" not in candidate
                ):
                    continue
                try:
                    ref = parse_source_url(candidate)
                except ValueError:
                    continue
                if ref.url not in found:
                    found[ref.url] = {
                        "provider": ref.provider,
                        "number": ref.number,
                        "url": ref.url,
                        # "change" or "issue". Absent on the wire means
                        # "change" for older payloads, so the frontend defaults
                        # rather than requires it.
                        "kind": ref.kind,
                    }
        links = list(found.values())
        self._source_links_cache = (cache_key, links)
        return links

    def to_dict(self, *, include_check_status: bool = False) -> dict:
        last_ts = self.messages[-1].get("ts", "") if self.messages else ""
        # Single reverse scan for last_msg, options, and last_activity_ts.
        last_msg = ""
        has_options = False
        options: list[str] = []
        prompt_preview = ""
        last_conv_role = ""
        last_activity_ts = ""
        found_conv = False
        for m in reversed(self.messages):
            role = m.get("role")
            # Compute meta/compaction flag once for both guards below
            msg_meta = m.get("meta") or {}
            is_compaction = role == "assistant" and msg_meta.get("kind") == "compaction"
            # Capture last_activity_ts from the most recent actionable message
            if (
                not last_activity_ts
                and role in ("tool_call", "tool_result", "assistant")
                and not is_compaction
            ):
                last_activity_ts = m.get("ts") or ""
            # Capture the last conversational message (role/options once, and
            # the newest non-empty preview). Skip compaction
            # notices: assistant-role system messages tagged
            # meta.kind == "compaction" — the auto-compact notice
            # ("Auto-compacted at N%.", _AUTO_COMPACT_NOTICE) and the
            # /compact result banner (chat_utils._append_compaction_notice).
            # This keeps the sidebar showing the last real message and mirrors
            # the frontend's deriveFollowUpOptions skip so preview/options
            # stay consistent.
            if role in ("user", "assistant") and not is_compaction:
                txt = m.get("content") or ""
                if txt:
                    if not found_conv:
                        found_conv = True
                        last_conv_role = role
                        if role == "assistant":
                            options = _parse_options(txt)
                            has_options = bool(options)
                            if has_options:
                                stripped = _redact(_OPTIONS_RE.sub("", txt).strip())
                                prompt_preview = (
                                    stripped[:240] + "…" if len(stripped) > 240 else stripped
                                )
                    if not last_msg:
                        # Preview is plain text in a one-line truncate div —
                        # strip markdown so raw markers (**, ```, links) don't
                        # leak into the sidebar. Options/prompt keep raw text.
                        # ORDER MATTERS: strip BEFORE redacting — markdown
                        # markers inside a secret (e.g. AKIA**…**) would split
                        # the credential signature past the scanner, and
                        # stripping afterwards would rejoin the fragments into
                        # a valid credential in the broadcast preview.
                        # A message that is ONLY stripped syntax (e.g. just an
                        # [OPTIONS:] block or a --- rule) yields '' — keep
                        # scanning older messages for a visible preview, like
                        # history.last_message_preview does, so live and
                        # archived previews stay consistent.
                        redacted = _redact(strip_markdown_preview(txt))
                        last_msg = (redacted[:80] + "…") if len(redacted) > 80 else redacted
            if found_conv and last_msg and last_activity_ts:
                break
        pending_approval = any(not f.done() for f in self._approval_futures.values())
        # waiting_for_input: turn ended (not running), no options, no approval,
        # and the last conversational message is from the assistant (not user).
        waiting_for_input = (
            not self.running
            and not has_options
            and not pending_approval
            and bool(self.messages)
            and last_conv_role == "assistant"
        )
        # If an approval is pending, surface the tool metadata from the most
        # recent unresolved permission message so the Board can show inline
        # Approve/Trust/Reject buttons without a second API call.
        #
        # LANE ASSIGNMENT NOTE: The frontend's inferLane() uses the boolean
        # `pending_approval` field (not `pending_approval_info`) to assign
        # sessions to the "Needs Approval" lane. `pending_approval_info` is
        # supplementary UI metadata (tool name, input, kind) for rendering
        # inline action buttons — it does NOT drive lane placement.
        pending_approval_info: dict[str, str] | None = None
        if pending_approval:
            for m in reversed(self.messages):
                if m.get("role") != "permission":
                    continue
                meta = parse_cls_meta(m.get("cls") or "") or {}
                if meta.get("resolved"):
                    continue
                pending_approval_info = {
                    "tool": _redact(m.get("content") or ""),
                    "tool_input": _redact(meta.get("tool_input", "")),
                    "tool_kind": _redact(meta.get("tool_kind", "")),
                    "request_id": _redact(meta.get("approval_id", meta.get("request_id", ""))),
                }
                break
        source_links = self._pr_source_links()
        return {
            "key": self.key,
            "title": _redact(self.display_title),
            "agent": self.agent,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "mode": self.mode,
            # Forward-compat alias of `mode` for the frontend's surface
            # registry. Today every slot's surface is identical to its mode
            # (default chat -> "", autopilot -> "orchestrator"), but emitting
            # a distinct field lets a future backend split the two — e.g. a
            # mode that introduces new behavior without claiming its own nav
            # destination, or two modes that share a destination — without
            # another wire-format change. The frontend reads
            # `slot.surface ?? slot.mode` for back-compat.
            "surface": self.mode,
            "workspace": self.workspace,
            "project": self.project,
            # Artifact companion binding. Flows into GET
            # /api/chat/slots and the WS `slots` snapshot — the frontend
            # resolves the active bound session for an artifact from here.
            "artifact": self._artifact,
            "messages": len(self.messages),
            "running": self.running,
            "orchestrating": self._in_stage_execution,
            "queue_depth": self.queue_depth,
            "stopping": self._stopping,
            "pending_approval": pending_approval,
            "pending_approval_info": pending_approval_info,
            "last_activity_ts": last_activity_ts,
            "waiting_for_input": waiting_for_input,
            "stop_state": self._stop_state,
            "created": self.created_at,
            "last_ts": last_ts,
            "last_message": last_msg,
            "source_links": [
                {
                    **link,
                    # The chip-status cache is pull-request-only: it holds a
                    # {ci, state} projection of a PR/MR lifecycle. Consulting it
                    # for an issue would key on a URL it never stores -- and if a
                    # PR and an issue ever normalized to the same key, the issue
                    # chip would inherit the PR's CI glyph. Gate on kind.
                    **(
                        (_cached_check_status(link["url"]) or {})
                        if include_check_status and link.get("kind", "change") == "change"
                        else {}
                    ),
                }
                for link in _budgeted_source_links(source_links)
            ],
            "source_links_total": len(source_links),
            # Agent TODO list. Absent-vs-empty is load-bearing: None means the
            # agent never used its todo tool (no pill), [] means it cleared the
            # list. Serialized here — the single dict feeding BOTH
            # /api/chat/slots (cold load) and the WS `slots` snapshot — so the
            # pill survives reconnect without a separate rehydration path.
            "todo": self.todo_payload(),
            "has_options": has_options,
            "options": [_redact(o) for o in options],
            "prompt_preview": prompt_preview,
            "trust": self._trust,
            "trust_reads": self._trust_reads,
            "trusted_patterns_count": len(self._trusted_patterns),
            "slack_linked": self._slack_linked,
            "slack_channel": self._slack_channel,
            "slack_thread_ts": self._slack_thread_ts,
            "folder_id": self.folder_id,
            "pinned": self.pinned,
            "tags": list(self.tags),
            "color_index": self.color_index,
            "color_theme": self.color_theme,
            "theme_consent": self.theme_consent,
            "theme_consent_sha": self.theme_consent_sha,
            "memory_mode": self.memory_mode,
            "forked_from": self.forked_from,
            "linked_session_key": self.linked_session_key,
            "app": self._app,
        }


class DashboardState:
    """Shared state injected into all handlers via ``app["state"]``."""

    # Class-level defaults, NOT just __init__ assignments. push_slots_update and
    # _persist_open_slots read these on every call, and a partially-constructed
    # state built with DashboardState.__new__(DashboardState) — the pattern used
    # by several endpoint test suites, which set only the attributes the handler
    # under test touches — never runs __init__. Without a class default those
    # reads raise AttributeError. __init__ still assigns per-instance values
    # below; these only supply the "nothing suspended, not restoring" baseline.
    _slots_push_suspend: int = 0
    _slots_push_pending: bool = False
    restoring_open_slots: bool = False

    def __init__(
        self,
        sessions: SessionManager,
        crons: CronService,
        lessons: LessonStore,
        start_time: float,
        subagents: SubagentManager | None = None,
        context_builder: ContextBuilder | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        task_runner: TaskRunner | None = None,
        slack_client: Any = None,
        owner_id: str = "",
    ):
        self.sessions = sessions
        self.crons = crons
        self.lessons = lessons
        self.start_time = start_time
        # Published only at the final boot-to-ready boundary in server.py.
        # The socket binds earlier, so /api/ready can truthfully return 503
        # while session restoration, channel relaunch, and tunnel setup finish.
        self.ready: bool = False
        # Wired by server.py after the gateway-owned prerequisite service is
        # constructed. The central chat runner reads this latch so every turn
        # entry path is protected, including task/workflow continuations.
        self.kiro_prerequisite_service: Any = None
        self.subagents = subagents
        self.channel_manager: Any = None  # lazy-init in server.py
        self.tunnel_manager: Any = None  # lazy-init in server.py (TunnelManager)
        self.instances_manager: Any = None  # lazy-init in server.py (SshTunnelManager)
        self.instances_registry: Any = None  # lazy-init in server.py (InstancesRegistry)
        # MCP gateway control plane — wired by GatewayOrchestrator AFTER
        # dashboard init (the broker starts before dashboard_state exists).
        # Read by the /api/mcp-gateway/* handlers off request.app['state'].
        self._mcp_gateway_manager: Any = None  # GatewayManager | None
        self._mcp_gateway_apply: Any = None  # async (enabled: bool) -> dict
        self._mcp_gateway_apply_poolable: Any = None  # async () -> dict
        # Secretary subsystem removed; kept as permanent None for apps/routes.py
        # builtin-service restart lookup (getattr-based, no-op when None).
        self._secretary_restart: Any = None  # restart callback (always None — service removed)
        self.workflow_service: Any = None  # lazy-init in server.py (WorkflowService, M6)
        self.context_builder = context_builder
        self.conversation_log = conversation_log
        self.consolidator = consolidator
        self.task_runner = task_runner
        self.slack_client = slack_client
        # True only when the Slack socket-mode connect actually succeeded this
        # session. slack_client being set proves tokens existed at boot, not
        # that they are valid — the gateway records the real outcome after
        # _connect_slack(). Read by the Slack settings status badge.
        self.slack_socket_connected: bool = False
        # Short reason from the failed connect attempt (e.g. "invalid_auth"),
        # empty when connected or never attempted. Read by the settings badge.
        self.slack_connect_error: str = ""
        # True once the Discord channel's Gateway WebSocket transport started
        # this session (set by maybe_start_discord). Read by the Discord
        # settings status badge.
        self.discord_connected: bool = False
        # Short reason when the Discord channel failed to start, empty when
        # running or never attempted. Read by the settings badge.
        self.discord_connect_error: str = ""
        # True once the Telegram channel's long-polling transport started this
        # session (set by maybe_start_telegram). Read by the Telegram settings
        # status badge.
        self.telegram_connected: bool = False
        # Short reason when the Telegram channel failed to start, empty when
        # running or never attempted. Read by the settings badge.
        self.telegram_connect_error: str = ""
        # True only while the Webex device WebSocket is connected + authorized
        # this session (kept truthful by WebexClient.on_state_change). Read by
        # the Webex settings status badge.
        self.webex_connected: bool = False
        # Short reason from the most recent Webex connection failure, empty
        # when connected or never attempted. Read by the settings badge.
        self.webex_connect_error: str = ""
        # True only while the WeCom (企业微信) channel's WebSocket is connected
        # + subscribed (kept live by WeComClient.on_status, wired in
        # maybe_start_wecom). Read by the WeCom settings status badge.
        self.wecom_connected: bool = False
        # Short reason from the most recent WeCom connection failure (connect
        # error, immediate close on bad credentials, or server kick), empty
        # when connected or never attempted. Read by the settings badge.
        self.wecom_connect_error: str = ""
        # True only while the Teams channel's credentials validated this
        # session (kept truthful by TeamsClient.on_state_change). Read by the
        # Teams settings status badge.
        self.teams_connected: bool = False
        # Short reason from the most recent Teams credential/connection failure,
        # empty when connected or never attempted. Read by the settings badge.
        self.teams_connect_error: str = ""
        # Late-bound inbound webhook handler for the Teams channel. The route
        # POST /api/messaging/teams is registered at app-build time (aiohttp
        # freezes routes at startup); maybe_start_teams sets this to the built
        # client's on_activity once credentials are present. None => 503.
        self.teams_on_activity: Any = None
        # True only while the Weixin (personal WeChat over iLink) channel's
        # long-poll loop is running (set in maybe_start_weixin). Read by the
        # WeChat settings status badge — a credential present at boot is NOT
        # enough to report "connected".
        self.weixin_connected: bool = False
        # Short reason from the most recent Weixin start failure, empty when
        # connected or never attempted. Read by the settings badge.
        self.weixin_connect_error: str = ""
        # Live channel transports (Telegram/WeCom/...) for channel-neutral
        # cross-surface mirror delivery — registered at boot by each channel's
        # gateway via ``register_channel_transport``. Slack keeps its dedicated
        # ``slack_client`` above (rich streaming mirror), so it is not stored here.
        self.channel_transports: dict[str, "MessagingTransport"] = {}
        self.owner_id = owner_id
        self._owner_hash: str | None = None
        # Branch+commit are resolved once by the CLI entrypoint (set_build_info,
        # pre-loop, post-detection); status_snapshot() reads this attribute so
        # subprocess never runs on the event loop. See module-level _build_info.
        self._build_info: tuple[str, str] = _build_info
        self.messages_received = 0
        # Broadcast: each SSE client gets its own queue; _notify_event wakes all
        self._sse_queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._notify_event = asyncio.Event()
        # Depth + pending flag for suspend_slots_push(); see that method.
        self._slots_push_suspend = 0
        self._slots_push_pending = False
        # True while the startup open-tab restore is in flight. Suppresses the
        # open_slots.json snapshot so a periodic flush cannot overwrite the file
        # being restored from with a half-populated slot set — see
        # _persist_open_slots.
        self.restoring_open_slots = False
        self._notification_log: list[dict[str, Any]] = _load_notifications()
        self._unread_count: int = 0
        # Notification bus (schema v2) — notify() adapts legacy calls onto it;
        # _deliver_note is the delivery sink (log, count, broadcast, persist).
        self.notification_bus = NotificationBus(sink=self._deliver_note)
        # Future of the most recent delivery-sink persist job (None when the
        # last persist ran inline). The app push handler awaits it to give a
        # durability guarantee; legacy producers ignore it (best-effort).
        self.last_notification_persist: asyncio.Future[bool] | None = None
        # Per-app push rate limiter (RFC Phase 2). State-owned (not a module
        # global) so its lifecycle matches the gateway instance and tests get
        # isolation for free.
        self.notification_rate_limiter = AppRateLimiter()
        # Per-channel user settings (RFC Phase 3): mute + priority override,
        # applied at the delivery sink so the bus stays pure.
        self.notification_channel_settings = ChannelSettings()
        self._slots: dict[str, _ChatSlot] = {}
        self._slack_to_slot: dict[str, str] = {}  # Slack session_key → slot name
        self._slot_counter = 0
        # slot key → last context-meter reading, for seeding the bar when a
        # session is reopened after its ACP session is gone. Readings this
        # process took live here immediately; `_loaded` tracks whether the
        # file written by an earlier process has been merged in yet, and
        # `_dirty` whether the off-loop flush still owes a write. The map is
        # touched from the event loop (broadcast/read), the flush executor,
        # and the shutdown thread, so EVERY access — including the flags —
        # holds `_context_snapshots_lock`. File IO happens outside the lock:
        # the flush serializes under it, writes without it.
        self._context_snapshots: dict[str, dict] = {}
        self._context_snapshots_loaded = False
        self._context_snapshots_dirty = False
        self._context_snapshots_lock = threading.Lock()
        # Serializes whole flushes (dirty-check through file write). Two flush
        # paths exist — the periodic executor pass and the shutdown save — and
        # the data lock above deliberately excludes the file write, so without
        # this an overlapping pair can land writes out of order: the slower
        # flush writes an OLDER serialization last, rolling the file back, and
        # the already-cleared dirty flag means nothing corrects it until a new
        # reading arrives. Only flush threads contend here; the event loop
        # never acquires it.
        self._context_snapshots_flush_lock = threading.Lock()
        self._folders: list[dict[str, Any]] = []  # project folder definitions
        self._cron_folders: list[dict[str, Any]] = []  # cron job folder groupings
        # Tag vocabulary: list of {id, name, color, order}. User-managed.
        self._tags: list[dict[str, Any]] = []
        # Sidebar columns — flat list of {id, name, tag_ids, mode, order, include_untagged}
        self._tag_boards: list[dict[str, Any]] = []
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self.no_crons: bool = False  # --no-crons flag: cron execution disabled
        self._hook_store: Any = None  # Lazy-init ScriptHookStore
        # Task refine state (background LLM spec generation)
        self._refine_status: str = "idle"  # idle, running, done, error, cancelled
        self._refine_text: str = ""
        self._refine_error: str = ""
        self._terminal_sessions: dict[str, Any] = {}  # PTY sessions for CLI panel
        self._terminal_reaper: asyncio.Task | None = None  # type: ignore[type-arg]
        self._terminal_title_poller: asyncio.Task | None = None  # type: ignore[type-arg]
        # Background reconciler that surfaces channel-originated sessions
        # (slack:<ts>, discord:…) as chat slots. Held to prevent GC.
        self._channel_slot_reconciler: asyncio.Task | None = None  # type: ignore[type-arg]
        self._loop_heartbeat: asyncio.Task | None = None  # type: ignore[type-arg]
        # Off-loop event-loop stall watchdog; armed under the real gateway
        # entrypoint (faulthandler enabled) and stopped on shutdown. Annotated
        # here so the assignment in start_dashboard type-checks under mypy strict.
        self._loop_watchdog: "LoopStallWatchdog | None" = None
        # Prevent-sleep inhibitor + its poll task. Held to prevent GC and
        # released/cancelled on shutdown; annotated here so the assignments in
        # start_dashboard type-check under mypy.
        self._sleep_inhibitor: "SleepInhibitor | None" = None
        self._prevent_sleep_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Knowledge Library
        self._knowledge_store: "KnowledgeStore | None" = None  # Lazy-initialized on first access
        self._knowledge_watcher: asyncio.Task | None = None  # type: ignore[type-arg]
        # Slack channel name resolver (lazy-initialized on first /api/slack/channels hit)
        self._channel_resolver: Any = None
        self._refine_input: str = ""
        self._refine_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._refine_session_key: str = ""
        # slack_client is set via constructor param above; gateway may override later
        self._refine_answer_future: asyncio.Future | None = None  # type: ignore[type-arg]
        # WebSocket clients (multiplexed real-time connection)
        self._ws_clients: list[web.WebSocketResponse] = []
        self._owner_ws_clients: set[web.WebSocketResponse] = set()
        self._ws_log_subscribers: set[web.WebSocketResponse] = set()
        self._ws_subagent_subscribers: set[web.WebSocketResponse] = set()
        # Pending tool approvals: id → asyncio.Future[bool]
        self._pending_approvals: dict[str, dict] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        # Pending agent questions (ask_question MCP tool): ask_id → payload /
        # Future[dict]. Distinct from _approval_futures because the resolution
        # value is the user's answer map, not an allow/deny boolean, and the
        # question card is addressed to one slot rather than the whole gateway.
        self._pending_questions: dict[str, dict] = {}
        self._question_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        self._flush_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Update progress tracking (shared across all connected clients)
        self._update_progress: dict[str, str] | None = None  # {step, detail}
        # Restricted (incognito/temporary): session keys with memory writes disabled
        self._restricted_keys: set[str] = set()
        # Ephemeral: session keys with no memory writes at all
        self._ephemeral_keys: set[str] = set()
        # Per-project file index registry (shared across slots)
        from kiro_crew.dashboard.file_index import FileIndexRegistry

        self.file_indexes = FileIndexRegistry()

    def register_channel_transport(self, transport: "MessagingTransport") -> None:
        """Register a live channel transport for cross-surface mirror delivery.

        Called by each channel's gateway at boot, keyed by ``channel_type`` so
        the dashboard turn path can resolve the transport for a session's
        outbound mirror link and deliver a reply via ``send_message``.
        """
        ct = getattr(transport, "channel_type", "")
        if transport is not None and ct:
            self.channel_transports[ct] = transport
            dispatcher = getattr(transport, "dispatcher", None)
            if dispatcher is not None:
                dispatcher.dashboard_state = self

    def get_channel_transport(self, channel_type: str) -> "MessagingTransport | None":
        """Return the registered transport for *channel_type*, or None."""
        return self.channel_transports.get(channel_type)

    def wire_session_compact_callback(self) -> None:
        """Register the dashboard's compaction callback on the session manager."""

        async def _on_compacted(key: str, pct: float, *, success: bool) -> None:
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            slot_key = dashboard_slot_key(key)
            if slot_key:
                # A channel-born session with an open tab is readable on BOTH
                # surfaces, and the user may be looking at either one, so both
                # get the notice: silently summarized history is the confusing
                # outcome this notice exists to prevent.
                if is_channel_session_key(key):
                    await self._notify_channel_compaction(key, pct, success=success)
            else:
                # No tab to append to, so the notice would be dropped and the
                # user would see summarized history with no explanation. Route
                # it to its own conversation instead.
                await self._notify_channel_compaction(key, pct, success=success)
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            template = _AUTO_COMPACT_NOTICE if success else _AUTO_COMPACT_FAILED_NOTICE
            message = template.format(pct=pct)
            try:
                # Tag kind="compaction" so this proactive auto-compact notice
                # (fired at session.autocompact_pct) is skipped by the dashboard's
                # follow-up [OPTIONS:] backward scan — same invariant as
                # chat_utils._append_compaction_notice. meta.kind covers history
                # reload; slot.append carries the meta on the live broadcast too.
                # (Routing through the chat_utils chokepoint would create a
                # state<->chat_utils import cycle; the notice is a hardcoded
                # template with no LLM content, so its redaction pass is moot.)
                slot.append("assistant", message, "msg msg-a", meta={"kind": "compaction"})
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append compact notice to slot %s", slot_key
                )
            if success:
                # Reset the context bar — successful compact dropped usage.
                # reset lets the frontend drop its stored token counts too
                # (the "X / Y tokens" tooltip), which no longer describe the
                # compacted session.
                try:
                    self.broadcast_context_usage(
                        slot_key, {"slot": slot_key, "pct": 0.0, "reset": True}
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Failed to broadcast context_usage for slot %s", slot_key
                    )

        self.sessions.set_compact_callback(_on_compacted)

    async def _notify_channel_compaction(self, key: str, pct: float, *, success: bool) -> None:
        """Deliver the auto-compact notice to a channel-originated session.

        Isolated from the dashboard leg: a channel that is unreachable, ungoverned
        or unregistered must not turn a successful compaction into an exception on
        the session manager's background task.
        """
        try:
            await deliver_channel_compaction_notice(self, key, pct, success=success)
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to deliver channel compact notice for %s", key
            )

    def wire_session_recycle_callback(self) -> None:
        """Register the dashboard's recycle-notification callback.

        Fired when the watchdog recycles a session (e.g. RSS threshold). Posts a
        notice into the slot so the user understands why their session reset.
        """

        async def _on_recycled(key: str, *, reason: str) -> None:
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            # A channel-born session's key is the channel's own even while its
            # tab is open, so ask which tab displays it rather than reading the
            # key's prefix — otherwise that tab resets with no explanation.
            slot_key = dashboard_slot_key(key)
            if not slot_key:
                return
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            message = _SESSION_RECYCLED_NOTICE.format(reason=reason)
            try:
                # Tag kind="compaction" so the dashboard's follow-up [OPTIONS:]
                # backward scan skips this proactive system notice, matching the
                # auto-compact notice invariant.
                slot.append("assistant", message, "msg msg-a", meta={"kind": "compaction"})
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append recycle notice to slot %s", slot_key
                )

        self.sessions.set_recycle_callback(_on_recycled)

    def _count_lessons(self) -> int:
        """Count lessons from JSONL store + vector store (if enabled)."""
        count = len(self.lessons.load_all())
        if self.context_builder:
            vs = self.context_builder.memory.vector_store
            if vs:
                count += len(vs.get_lessons())
        return count

    def status_snapshot(
        self,
        *,
        cron_jobs: int | None = None,
        lessons: int | None = None,
        update_available: bool = False,
        update_self_updatable: bool = False,
        update_checked: bool = False,
        update_command: str = "",
    ) -> dict[str, Any]:
        """Core status fields shared by /api/status, SSE, and WebSocket pushes."""
        uptime = int(time.time() - self.start_time)
        branch, commit = self._build_info
        return {
            "uptime": _fmt_duration(uptime),
            "start_time": self.start_time,
            "sessions": self.sessions.count,
            "messages": self.messages_received,
            "cron_jobs": cron_jobs if cron_jobs is not None else len(self.crons.list_jobs()),
            "lessons": lessons if lessons is not None else self._count_lessons(),
            "subagents": self.subagents.count if self.subagents else 0,
            "update_available": update_available,
            # Can THIS install replace its own code? Only a git checkout can
            # (``POST /api/update`` is git fetch + reset). Shipped alongside the
            # availability flag so the dashboard can offer an Update button that
            # will actually work, instead of one that 409s on a wheel install —
            # it must not have to run a fresh check just to learn the layout.
            "update_self_updatable": update_self_updatable,
            # Did a check ever reach a verdict? Without this the UI cannot tell
            # "checked and current" from "never checked", and painting a green
            # "Up to date" pill next to a red "couldn't check" line is the exact
            # half-truth the update-check contract exists to prevent.
            "update_checked": update_checked,
            # The upgrade command for an install that cannot replace itself, so the
            # 12-hourly BACKGROUND check can light the nav badge and still land the
            # user on something actionable. Deriving it only from a manual check
            # left the badge pointing at an Update button that 409s.
            "update_command": update_command,
            "no_crons": self.no_crons,
            "branch": branch,
            "commit": commit,
            # Which release lane these bytes came from: "nightly", "insider" or
            # "stable". Shipped as a RESOLVED ANSWER rather than leaving the
            # dashboard to parse `version` itself, because the rule is not
            # obvious (the same release is stamped as SemVer for desktop and
            # PEP 440 for wheels, and neither PEP 440 prerelease spelling
            # contains a `-`) and a frontend mirror of it would drift silently.
            # The dashboard uses this to give prerelease users an obvious way to
            # report a bug; see release_channel.py for the full rule.
            "release_channel": _release_channel_of_build(),
            # True when the gateway has wired up a live Slack client (Socket Mode
            # connected). None in pure-dashboard mode or when Slack is disabled.
            "slack_connected": self.slack_client is not None,
            # Governance enforcement health: "active" (enforcing),
            # "disabled" (permissive default / not restricting), "degraded" (a
            # fail-closed trip, integrity mismatch, or unverified policy this
            # session), or "unknown" (policy not yet loaded).  Pure in-memory read.
            "governance": _governance_status(),
        }

    _APPROVAL_TIMEOUT = 7200  # 2 hours — triggers pause (not skip/fail) via deny path
    # Background sources (cron, heartbeat, taskrunner) have no human responder, so
    # waiting the full human window would burn 2h on every unattended approval. They
    # wait only this short window and then deny-fast, letting the turn proceed/fail
    # rather than hang.
    _BACKGROUND_APPROVAL_TIMEOUT_SECS = 180  # 3 minutes — deny-fast for unattended runs
    # Agent questions block a live MCP tool call, so the ceiling is bounded by
    # how long the agent transport will hold that call open — far shorter than
    # the 2h approval window. Callers pick a value inside these bounds.
    _QUESTION_TIMEOUT_DEFAULT = 300  # 5 minutes
    # Hard ceiling set by the ACP tool-stall watchdog, NOT by the `wait` tool.
    # `acp/client.py::_TOOL_STALL_TIMEOUT` is 600s and is armed once a tool call
    # is dispatched; a blocked ask_question emits no progress frames, so a window
    # at or beyond 600s lets the watchdog declare the turn dead and kill it —
    # after which an answer has no turn left to return to. 540s keeps a 60s
    # margin below the watchdog. `wait` can afford 1800s because it is a
    # different mechanism; copying that number here was the bug.
    _QUESTION_TIMEOUT_MAX = 540  # 9 minutes — 60s under the 600s tool-stall watchdog
    _FLUSH_INTERVAL = 5  # seconds between dirty-slot flushes

    _log = logging.getLogger(__name__)

    @property
    def knowledge_store(self):  # type: ignore[override]
        """Lazy-init KnowledgeStore on first access."""
        if self._knowledge_store is None:
            db_dir = os.path.join(str(config_dir()), "workspace", "knowledge")
            os.makedirs(db_dir, exist_ok=True)
            self._knowledge_store = KnowledgeStore(os.path.join(db_dir, "knowledge.db"))
        return self._knowledge_store

    def enable_yolo(self, *, from_config: bool = False) -> None:
        """Activate safety override (delegates to safety_override module)."""
        source = "config" if from_config else "dashboard"
        safety_override().activate(source)

    def disable_yolo(self) -> None:
        """Deactivate safety override (delegates to safety_override module)."""
        safety_override().deactivate("dashboard")

    def is_yolo_active(self) -> bool:
        """Return whether safety override is active (delegates to safety_override module)."""
        return safety_override().is_active()

    @property
    def _yolo(self) -> bool:
        """Backward-compat property for code reading _yolo directly."""
        return safety_override().is_active()

    @_yolo.setter
    def _yolo(self, value: bool) -> None:
        """Backward-compat setter for tests that assign state._yolo = True/False."""
        if value:
            safety_override().activate("dashboard")
        else:
            safety_override().deactivate("dashboard")

    async def request_approval(
        self,
        approval_id: str,
        source: str,
        tool: str,
        *,
        tool_input: str = "",
        tool_purpose: str = "",
        slot: str = "",
        is_background: bool = False,
    ) -> bool:
        """Request interactive approval. Returns True if approved, False if rejected/timeout.

        ``is_background`` marks an unattended source (cron, heartbeat, taskrunner)
        with no human responder. Those wait only ``_BACKGROUND_APPROVAL_TIMEOUT_SECS``
        and then deny-fast, instead of burning the full 2h human window.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._approval_futures[approval_id] = fut

        # Sanitize LLM-sourced fields before broadcasting to dashboard clients
        safe_tool, _ = redact_exfiltration_urls(tool)
        safe_tool, _ = redact_credentials(safe_tool)
        safe_input, _ = redact_exfiltration_urls(tool_input)
        safe_input, _ = redact_credentials(safe_input)
        safe_purpose, _ = redact_exfiltration_urls(tool_purpose)
        safe_purpose, _ = redact_credentials(safe_purpose)

        self._pending_approvals[approval_id] = {
            "id": approval_id,
            "source": source,
            "tool": safe_tool,
            "tool_input": safe_input,
            "tool_purpose": safe_purpose,
            "slot": slot,
            "ts": time.time(),
        }
        self.broadcast_ws("approval", self._pending_approvals[approval_id])
        # Background sources have no human present — deny-fast on a short window
        # instead of pausing for the full 2h human window.
        timeout = (
            self._BACKGROUND_APPROVAL_TIMEOUT_SECS if is_background else self._APPROVAL_TIMEOUT
        )
        try:
            # Timeout triggers deny → which pauses the run (not skip/fail) for
            # interactive sources. This prevents indefinite hangs if notifications
            # are lost or user disconnects, while still allowing the user to resume
            # later. The run pauses gracefully rather than silently proceeding or
            # permanently failing.
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            # Deny-by-default on shutdown/cancellation
            return False
        finally:
            self._pending_approvals.pop(approval_id, None)
            self._approval_futures.pop(approval_id, None)

    def _audit_and_broadcast_approval(
        self, session_key: str, approval_id: str, approved: bool
    ) -> None:
        """Emit SEL audit event and broadcast WS notification for an approval decision."""
        try:
            sel().log_tool_invocation(
                session_key=session_key,
                tool_name="approval_decision",
                outcome="approved" if approved else "rejected",
                request_id=approval_id,
                source="dashboard",
            )
        except Exception:
            self._log.warning("SEL audit failed for approval resolution", exc_info=True)
        try:
            self.broadcast_ws("approval_resolved", {"id": approval_id, "approved": approved})
        except Exception:
            self._log.warning("WS broadcast failed for approval resolution", exc_info=True)

    def resolve_state_approval(self, approval_id: str, approved: bool) -> bool:
        """Resolve ONLY a state-level (background: cron/subagent/gateway) approval.

        Does NOT scan slot-level futures — so it carries no cross-slot authority.
        Callers that have already located the owning slot under a session-identity
        guard (e.g. the dashboard slot-approve handler's fallback) MUST use this
        rather than :meth:`resolve_approval`: a bare id-match slot scan would let a
        request-id collision resolve an unrelated slot's pending tool, bypassing
        the owner's session-identity check. Returns False if no state-level future
        owns ``approval_id``.
        """
        fut = self._approval_futures.get(approval_id)
        if fut and not fut.done():
            fut.set_result(approved)
            self._audit_and_broadcast_approval("state", approval_id, approved)
            return True
        return False

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        """Resolve a pending approval. Returns False if not found.

        State-level futures receive ``bool`` (consumed by gateway, which converts to str).
        Slot-level futures receive ``str`` ("approved"/"rejected", consumed by channel.py).

        This scans slot-level futures by bare id-match with NO session-identity
        check, so it is safe only for callers that legitimately own the id
        (native gateway / Slack click / session-scoped handler). A caller that
        addresses one slot but may hold a colliding id from another MUST use
        :meth:`resolve_state_approval` instead (see the slot-approve handler).
        """
        decision = "approved" if approved else "rejected"
        if self.resolve_state_approval(approval_id, approved):
            return True
        # Also check slot-level approval futures (chat tool approvals)
        for slot in self._slots.values():
            fut = slot._approval_futures.get(approval_id)
            if fut and not fut.done():
                fut.set_result(decision)
                if _mark_permission_resolved(slot.messages, approval_id, decision):
                    # The periodic flush skips non-dirty slots; without this the
                    # in-place mutation can be lost and the answered card comes
                    # back on reload with a future that no longer exists.
                    slot._dirty = True
                self._audit_and_broadcast_approval(slot.key, approval_id, approved)
                self.push_slots_update()
                return True
        return False

    def _redact_questions(self, questions: list[dict]) -> list[dict]:
        """Redact model-authored question/option text (URLs, credentials) and
        reject any pair that collapses to identical text after redaction — the
        answer map is keyed by the rendered text, so an indistinguishable
        question or option is unanswerable. Shared by request_question (the
        blocking HTTP round-trip) and post_question_card (the stateless card)."""
        safe_questions: list[dict] = []
        seen_redacted: set[str] = set()
        for q in questions:
            sq = dict(q)
            for field in ("question", "header"):
                val, _ = redact_exfiltration_urls(str(sq.get(field) or ""))
                val, _ = redact_credentials(val)
                sq[field] = val
            norm = " ".join(str(sq.get("question") or "").split()).casefold()
            if norm in seen_redacted:
                raise ValueError(
                    "questions collapse to identical text after redaction; "
                    "rephrase so each question is distinguishable"
                )
            seen_redacted.add(norm)
            safe_opts: list[dict] = []
            seen_redacted_labels: set[str] = set()
            for o in sq.get("options") or []:
                so = dict(o)
                for field in ("label", "description"):
                    val, _ = redact_exfiltration_urls(str(so.get(field) or ""))
                    val, _ = redact_credentials(val)
                    so[field] = val
                norm_label = " ".join(str(so.get("label") or "").split()).casefold()
                if norm_label in seen_redacted_labels:
                    raise ValueError(
                        "option labels collapse to identical text after redaction; "
                        "rephrase so every option is distinguishable"
                    )
                seen_redacted_labels.add(norm_label)
                safe_opts.append(so)
            sq["options"] = safe_opts
            safe_questions.append(sq)
        return safe_questions

    async def post_question_card(self, slot_key: str, questions: list[dict]) -> int:
        """Broadcast a NON-BLOCKING question card (no ``ask_id``) to *slot_key*'s
        owner clients; return the number delivered.

        Unlike :meth:`request_question`, this registers no future and awaits no
        answer: the frontend renders a legacy (ask_id-less) card whose submit
        sends the answers as an ordinary chat message, so the agent resumes in a
        fresh turn (#755 stateless ``ask_question``) rather than blocking. Shares
        :meth:`_redact_questions` (may raise ``ValueError`` on a post-redaction
        collapse). Owner-only, same grounds as request_question's broadcast."""
        safe_questions = self._redact_questions(questions)
        payload = {"slot": slot_key, "questions": safe_questions, "ts": time.time()}
        return int(await self.deliver_ws_owners("question_card", payload))

    async def request_question(
        self,
        ask_id: str,
        slot_key: str,
        questions: list[dict],
        timeout: int | None = None,
    ) -> dict[str, str] | None:
        """Ask the dashboard user a multiple-choice question and block for the answer.

        Broadcasts a ``question_card`` carrying ``ask_id`` and awaits the
        matching :meth:`resolve_question` call. Returns the user's answer map
        (``{question: answer}``), or ``None`` when the wait timed out, the
        caller was cancelled, or the user dismissed the card.

        ``questions`` MUST already have passed
        :func:`kiro_crew.validation.validate_ask_user_question` — this method
        redacts but does not re-shape the payload.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, str] | None] = loop.create_future()

        # Redact model-authored text (URLs/credentials) before it is rendered,
        # rejecting post-redaction collapses. Shared with post_question_card.
        safe_questions = self._redact_questions(questions)

        payload = {
            "ask_id": ask_id,
            "slot": slot_key,
            "questions": safe_questions,
            "ts": time.time(),
        }
        self._pending_questions[ask_id] = payload
        # Registered only now that the payload is known-good: an early raise
        # above must not leave an orphan future nothing will ever resolve.
        self._question_futures[ask_id] = fut
        # Owner-only: the payload carries the model-authored question text and
        # options addressed to the dashboard owner. A plain broadcast_ws would
        # also deliver it to non-owner sessions, which would defeat the
        # owner-gating on the HTTP endpoints.
        self.broadcast_ws_owners("question_card", payload)

        window = timeout if timeout is not None else self._QUESTION_TIMEOUT_DEFAULT
        window = max(1, min(int(window), self._QUESTION_TIMEOUT_MAX))
        try:
            return await asyncio.wait_for(fut, timeout=window)
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            return None
        finally:
            self._pending_questions.pop(ask_id, None)
            self._question_futures.pop(ask_id, None)
            # Tell every owner client to drop the card — otherwise a timed-out
            # or cancelled question stays clickable and submitting it 404s.
            # Owner-scoped to match the card broadcast: a non-owner never
            # received the card, so it has nothing to drop.
            try:
                self.broadcast_ws_owners("question_card_resolved", {"ask_id": ask_id})
            except Exception:
                self._log.warning("WS broadcast failed for question resolution", exc_info=True)

    def resolve_question(self, ask_id: str, answers: dict[str, str] | None) -> bool:
        """Resolve a pending agent question. Returns False when no such question.

        ``answers`` of ``None`` means the user dismissed the card without
        answering; the blocked caller then sees the same result as a timeout.
        """
        fut = self._question_futures.get(ask_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answers)
        return True

    def cancel_questions_for_slot(self, slot_key: str) -> int:
        """Unblock every question pending on ``slot_key``. Returns how many.

        Called when a slot's turn is stopped or reset so a blocked ask_question
        cannot outlive the turn that issued it and strand its MCP call.
        """
        stale = [aid for aid, p in self._pending_questions.items() if p.get("slot") == slot_key]
        cancelled = 0
        for aid in stale:
            if self.resolve_question(aid, None):
                cancelled += 1
        return cancelled

    def start_flush_loop(self) -> None:
        """Start background loop that flushes dirty slots to disk every 5s."""
        if self._flush_task is None:
            self._flush_task = asyncio.ensure_future(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Periodically save dirty slots so a crash loses at most 5s of chat."""
        from kiro_crew import shutdown_event

        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=self._FLUSH_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.get_running_loop().run_in_executor(None, self._flush_dirty_slots)

    def _flush_dirty_slots(self) -> None:
        """Write any slot with new messages to its JSONL file."""
        if not self.conversation_log:
            return
        from kiro_crew.dashboard.chat import _save_slot_to_history

        for slot in list(self._slots.values()):
            if not slot._dirty or not slot.messages:
                continue
            # Clear the dirty bit only if NOTHING re-marked the slot while this
            # save was running. This runs on an executor thread and the event loop
            # keeps mutating the slot underneath it, so a plain post-save
            # `_dirty = False` would overwrite a mark set DURING the save (e.g.
            # _flush_file_changes attaching file_changes) — the stale snapshot
            # would be the last thing written and every later pass would skip the
            # slot, so the late mutation would never reach disk.
            #
            # The generation compare is used instead of consuming the bit up front
            # because `_dirty` must stay True for the whole save: `chat_fork` reads
            # it as "unpersisted state exists" (a False read makes it fork from
            # stale disk), and `_save_slot_to_history`'s resumed-slot no-op guard
            # is written assuming a dirty slot still reads dirty during the save.
            # See the `_dirty` property for both contracts.
            gen = slot._dirty_gen
            try:
                _save_slot_to_history(self, slot)
            except Exception:
                # Leave _dirty set so the next 5s pass retries.
                logger.warning("Flush failed for slot %s", slot.key, exc_info=True)
            else:
                if slot._dirty_gen == gen:
                    slot._dirty = False
        # Snapshot the live tab set so a gateway restart can restore exactly
        # the tabs the user had open, regardless of last-message age. Without
        # this, restore_recent_sessions only brings back sessions whose
        # JSONL file was written within `restore_window_minutes` (default
        # 30) — long-running tabs that haven't seen a new message in 30 min
        # would silently drop to History on every restart. This file is
        # cheap (~one short string per tab) and overwritten on every flush.
        self._persist_open_slots()
        # Same off-loop flush, same reason: a context-meter reading is recorded
        # on the loop (pure dict write) and the file IO happens here.
        self._persist_context_snapshots()

    def _persist_open_slots(self) -> None:
        """Atomically write the current open-slot keys to <config_dir>/open_slots.json.

        The file shape is intentionally minimal:
            {"keys": ["chat-1-...", "chat-2-..."], "ts": 1234567890.0}

        Path resolves through ``config_dir()`` so the snapshot lives next to
        every other dashboard persistence file and honors ``KIROCREW_HOME``
        — non-default homes (dev/test instances) restore from their own file
        instead of bleeding through ``~/.kiro/crew``.

        Restored on startup by ``restore_open_slots`` in chat_persistence.
        Failures are logged at debug level — losing the snapshot only
        degrades restore behaviour back to the legacy 30-min mtime window,
        it never breaks the gateway.

        NO-OP while ``restoring_open_slots`` is set. The startup restore yields to
        the event loop between tabs, and ``start_flush_loop()`` is already running
        by then (every 5s), so without this guard a flush lands mid-restore and
        snapshots a PARTIAL slot set over the very file being restored from —
        measured 77 tabs collapsing to 70. A kill in that window would drop the
        un-restored tabs from the sidebar permanently. Whatever is on disk is
        already the authoritative set we are loading, so skipping is always safe.
        """
        if self.restoring_open_slots:
            logger.debug("open_slots snapshot skipped: restore in progress")
            return
        try:
            path = config_dir() / "open_slots.json"
            # Only snapshot persistent-memory slots. Incognito/temporary tabs
            # are ephemeral by contract ("closes when I'm done", no
            # consolidation/lessons); persisting their keys would resurrect
            # them on every restart indefinitely. Filter on the canonical
            # "persistent" memory_mode so any non-default mode (incognito,
            # temporary, future variants) is excluded.
            keys = [
                name
                for name, slot in list(self._slots.items())
                if getattr(slot, "memory_mode", "persistent") == "persistent"
            ]
            payload = json.dumps({"keys": keys, "ts": time.time()})
            # Use the canonical atomic_write helper, not a deterministic
            # ".json.tmp" name — _persist_open_slots can run concurrently from
            # two threads (the periodic _flush_dirty_slots executor every 5s
            # and the shutdown thread via save_all_slots_to_history). A shared
            # fixed temp file would hit an ENOENT race between the two writers;
            # atomic_write uses tempfile.mkstemp for unique names so they can't
            # collide. mode=0o600 because open_slots.json holds session
            # identifier keys — default umask perms (0o644) are too permissive.
            atomic_write(path, payload, mode=0o600)
        except Exception:
            logger.debug("Failed to persist open_slots.json", exc_info=True)

    def notify(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        meta: dict | None = None,
        url: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Push a notification to ALL connected SSE clients and persist to disk.

        Legacy adapter over the notification bus (see
        docs/request-for-change/rfc-local-notification-bus.md): builds a
        schema-v2 payload (source="system", channel="system.<kind>") and pushes
        it through :class:`NotificationBus`, which validates and hands the
        enriched note back to :meth:`_deliver_note`.

        ``url`` (a dashboard-internal path that renders the detail panel's Open
        button) and ``actions`` (up to four labelled navigation capsules on the
        feed row) must be passed HERE, not inside ``meta``: the bus's meta merge
        skips both names so ``meta`` cannot smuggle an unvalidated deep link,
        so a ``meta={"url": ...}`` caller produces a note with no navigation at
        all. Both are validated by the payload; an invalid one -- wrong type or
        an off-dashboard path -- is dropped with a warning, so the never-raises
        contract is preserved. That holds only because
        :meth:`NotificationPayload.validate` turns BOTH bad values and bad types
        into :class:`NotificationValidationError`; the payload build is inside
        the guarded block so a future field that validates on construction
        cannot reopen the hole either.
        """
        try:
            payload = payload_from_legacy(kind, title, body, meta, url=url, actions=actions)
            self.notification_bus.push(payload)
        except NotificationValidationError:
            # Legacy callers never validated inputs; keep the old
            # never-raises contract and log instead.
            logger.warning("Dropped invalid notification (kind=%s)", kind, exc_info=True)

    def _deliver_note(self, note: dict[str, Any]) -> None:
        """Delivery sink for the notification bus: log, count, broadcast, persist.

        Central redaction point: notes can carry LLM-derived content (agent
        results, cron summaries — including flat-merged meta values and
        nested structures like action labels), so every string value is
        scanned recursively before reaching any external surface (SSE
        clients, JSONL on disk). Most callers already redact at the call
        site; this is defense-in-depth ahead of Phase 2 app producers.
        """
        for key, value in note.items():
            if key != "ts":
                note[key] = _redact_note_value(value)
        # Per-channel user settings (RFC Phase 3): mute stamps silenced=True
        # + forces passive; priority override replaces the effective priority.
        # Applied before append/broadcast so SSE clients and disk both see
        # the user's view.
        self.notification_channel_settings.apply(note)
        # RFC Phase 5: lazily sweep expired passive rows on every delivery
        # (the log is capped at a few hundred rows, so the scan is cheap).
        # Disk catches up on the next full rewrite (ack/delete/clear paths).
        sweep_expired_notifications(self._notification_log)
        self._notification_log.append(note)
        # Bound the in-memory list: only the disk load
        # path capped it before, so sustained live deliveries grew the list
        # without limit — and the per-delivery sweep above scans it, making
        # delivery O(N²) over time. Same cap as the persisted file; oldest
        # rows drop first (the file trim keeps disk consistent).
        if len(self._notification_log) > _MAX_PERSISTED_NOTIFICATIONS:
            del self._notification_log[: len(self._notification_log) - _MAX_PERSISTED_NOTIFICATIONS]
        # Badge counts attention-worthy rows only (RFC Phase 3: passive rows
        # -- including muted-channel notes -- are excluded).
        if note.get("priority") != "passive":
            self._unread_count += 1
        self._broadcast(note)
        # Persistence does blocking file I/O (append + possible trim). The
        # bus sink is now externally drivable (Phase 2 app producers), so on
        # a running event loop the write is offloaded to a dedicated
        # single-worker executor (FIFO keeps on-disk order = delivery order).
        # A snapshot copy is handed off because the in-memory note can be
        # mutated afterwards on the loop (e.g. ack sets note["acked"]).
        # The future is stashed so callers that need durability (the app
        # push endpoint) can await it and read the success bool; legacy
        # system producers stay fire-and-forget (best-effort history).
        # Without a running loop (unit tests, sync callers) persist inline.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _persist_notification(note)
            self.last_notification_persist = None
        else:
            self.last_notification_persist = loop.run_in_executor(
                _notification_io_executor(), _persist_notification, dict(note)
            )

    def register_sse(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new SSE client and return its dedicated queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._sse_queues.append(q)
        return q

    def unregister_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE client queue on disconnect."""
        try:
            self._sse_queues.remove(q)
        except ValueError:
            pass

    def mark_notifications_read(self) -> None:
        """Reset unread counter (called when client opens notification panel)."""
        self._unread_count = 0

    async def _rewrite_notifications_async(self) -> None:
        """Rewrite the notifications file on the I/O executor and await it.

        All disk mutations (appends from ``_deliver_note`` and rewrites from
        delete/ack/clear) go through the same single-worker executor, so they
        execute strictly in submission order — a rewrite submitted after an
        append can never be overtaken by it (no resurrection of deleted
        rows). Awaiting makes the mutation durable before the HTTP response
        returns. A shallow per-row snapshot is handed off because rows are
        mutated on the loop (e.g. ack flags).
        """
        snapshot = [dict(n) for n in self._notification_log]
        await asyncio.get_running_loop().run_in_executor(
            _notification_io_executor(), _rewrite_notifications, snapshot
        )

    async def delete_notification(self, ts: str) -> bool:
        """Remove a single notification by timestamp and persist to disk."""
        before = len(self._notification_log)
        self._notification_log = [n for n in self._notification_log if n.get("ts") != ts]
        removed = len(self._notification_log) < before
        if removed:
            await self._rewrite_notifications_async()
        return removed

    async def ack_notification(self, ts: str) -> bool:
        """Mark a notification as acknowledged and persist."""
        for n in self._notification_log:
            if n.get("ts") == ts:
                n["acked"] = True
                await self._rewrite_notifications_async()
                self.broadcast_ws("notification_ack", {"ts": ts})
                return True
        return False

    async def unack_notification(self, ts: str) -> bool:
        """Mark a notification as unread and persist."""
        for n in self._notification_log:
            if n.get("ts") == ts:
                n["acked"] = False
                await self._rewrite_notifications_async()
                self.broadcast_ws("notification_unack", {"ts": ts})
                return True
        return False

    async def clear_notifications(self) -> None:
        """Remove all notifications from memory and disk."""
        self._notification_log.clear()
        self._unread_count = 0
        await self._rewrite_notifications_async()

    def get_slot(self, name: str) -> _ChatSlot | None:
        """Look up a slot by name without creating it. Returns None if absent."""
        return self._slots.get(name)

    def spend_slot_by_session(self) -> dict[str, str]:
        """Map each live slot's SESSION key to the SLOT key its spend is filed under.

        Per-turn usage is persisted under ``slot.key``
        (``chat_runner.persist_token_record_async``), while a session is addressed
        by :func:`effective_session_key`. For an ordinary dashboard slot those are
        the same string modulo the ``dashboard:`` prefix, so a prefix rule is
        enough. For a slot bound to a channel or cron conversation they are
        UNRELATED: the turns run under ``linked_session_key`` while the spend rows
        still carry the dashboard slot key, so a consumer joining spend by session
        key finds nothing and renders "unknown" for a session that did spend.

        This is the reverse index that closes that gap. It lives here because
        DashboardState owns the slots and the identity rule; a consumer rebuilding
        it would be a second owner of the rule, which is how the two sides drifted
        apart in the first place.
        """
        # Local import: chat_utils imports FROM state at module level, so a
        # top-level import here is a cycle. state.py already defers
        # `dashboard_slot_key` the same way.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        out: dict[str, str] = {}
        for slot in list(self._slots.values()):
            try:
                session_key = effective_session_key(slot)
            except Exception:  # pragma: no cover - defensive; a slot mid-teardown
                continue
            if session_key:
                out[session_key] = slot.key
        return out

    def native_subagent_snapshots(
        self,
        terminal_limit: int = NATIVE_SUBAGENT_TERMINAL_KEEP,
        ttl_secs: float = NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    ) -> list[dict[str, object]]:
        """Return bounded native running and terminal cards for WS replay.

        DashboardState owns the slot record shape. The WebSocket layer consumes
        these transport-ready snapshots without reaching into private slot data.
        """
        now = time.time()
        running: list[dict[str, object]] = []
        done: list[dict[str, object]] = []
        for slot in list(self._slots.values()):
            output = slot._native_subagent_output
            for info in list(slot._native_subagent_tracker.values()):
                card_id = str(info.get("id") or "")
                if not card_id:
                    continue
                base: dict[str, object] = {
                    "id": card_id,
                    "slot": slot.key,
                    "task": str(info.get("task") or ""),
                    "agent": str(info.get("agent") or ""),
                }
                if info.get("done"):
                    done_at = float(info.get("done_at") or 0.0)
                    if done_at and (now - done_at) > ttl_secs:
                        continue
                    done.append(
                        {
                            **base,
                            "done": True,
                            "elapsed": float(info.get("elapsed") or 0.0),
                            "error": info.get("error"),
                            "stopped": bool(info.get("stopped")),
                            "outcome": (
                                "stopped"
                                if info.get("stopped")
                                else ("failed" if info.get("error") else "completed")
                            ),
                            "result": str(info.get("result") or ""),
                            "done_at": done_at,
                        }
                    )
                else:
                    running.append(
                        {
                            **base,
                            "done": False,
                            "streaming": native_subagent_output_tail(output.get(card_id, [])),
                            "last_tool": str(info.get("last_tool") or ""),
                            "started": float(info.get("started") or now),
                        }
                    )
        if terminal_limit >= 0 and len(done) > terminal_limit:

            def snapshot_done_at(snapshot: dict[str, object]) -> float:
                value = snapshot.get("done_at")
                return float(value) if isinstance(value, (int, float)) else 0.0

            done.sort(key=snapshot_done_at, reverse=True)
            done = done[:terminal_limit]
        return running + done

    def has_slot(self, name: str) -> bool:
        """Check if a slot exists by name."""
        return name in self._slots

    def get_linked_slot(self, session_key: str) -> "_ChatSlot | None":
        """Look up a dashboard slot linked to a Slack thread. Cleans up stale mappings."""
        slot_key = self._slack_to_slot.get(session_key)
        if not slot_key:
            return None
        slot = self._slots.get(slot_key)
        if not slot or not slot._slack_linked or slot._slack_thread_ts != session_key:
            self._slack_to_slot.pop(session_key, None)
            return None
        return slot

    def resolve_slot(self, name: str) -> _ChatSlot | None:
        """Like :meth:`get_slot`, but also resolves bare ``chat-N`` labels.

        Falls back to a prefix match so ``chat-2`` resolves to
        ``chat-2-<timestamp>`` when no exact match exists. The fallback is
        gated to names matching ``chat-\\d+`` to prevent broad-prefix
        collisions (e.g. a bare ``chat`` binding to any ``chat-*`` slot).

        Tie-break: when multiple slots share the same ``chat-N-`` prefix
        (e.g. a stale slot re-created by a resume/restart alongside the live
        one), return the slot with the largest trailing ``<timestamp>`` — the
        newest. Iteration-order tie-break previously returned whichever slot
        happened to be first in the dict, which could route a ``chat-N``
        message to a long-closed slot after a restart.

        Use this from trusted delivery paths (heartbeat, cron) where the
        caller wants short-label addressing. Do NOT use from HTTP handlers
        that pass the resolved name to key-derivation functions
        (e.g. ``_history_key_for``) — those require the full slot key.
        """
        slot = self._slots.get(name)
        if slot is not None:
            return slot
        if not _CHAT_N_RE.fullmatch(name):
            return None
        prefix = name + "-"
        best_ts = -1
        best_slot: _ChatSlot | None = None
        for key, s in self._slots.items():
            if not key.startswith(prefix):
                continue
            tail = key[len(prefix) :]
            try:
                ts = int(tail)
            except ValueError:
                ts = -1
            # Prefer the newest timestamp; on a genuine tie keep the first seen.
            if best_slot is None or ts > best_ts:
                best_ts, best_slot = ts, s
        return best_slot

    def link_slack(self, slot_name: str, thread_ts: str, channel_id: str) -> None:
        """Update a slot's Slack link state and persist to SessionStore."""
        slot = self._slots.get(slot_name)
        if not slot:
            return
        # Remove stale mapping if slot was previously linked to a different thread
        old_ts = slot._slack_thread_ts
        if old_ts and old_ts != thread_ts:
            self._slack_to_slot.pop(old_ts, None)
        # Clear persisted link of old slot if this thread was previously owned by another slot
        old_owner = self._slack_to_slot.get(thread_ts)
        if old_owner and old_owner != slot_name:
            old_slot = self._slots.get(old_owner)
            if old_slot:
                old_slot._slack_linked = False
                old_slot._slack_thread_ts = ""
                old_slot._slack_channel = ""
            if self.sessions:
                from kiro_crew.dashboard.chat_utils import (
                    _history_key_for,
                    effective_session_key,
                )

                # The previous owner's slot may already be gone; fall back to
                # deriving its key from the name in that case.
                old_key = (
                    effective_session_key(old_slot)
                    if old_slot
                    else _history_key_for(old_owner)
                )
                self.sessions.set_slack_link(old_key, "", "")
        slot._slack_linked = True
        slot._slack_channel = channel_id
        slot._slack_thread_ts = thread_ts
        self._slack_to_slot[thread_ts] = slot_name
        # Persist so link survives gateway restarts
        if self.sessions:
            from kiro_crew.dashboard.chat_utils import effective_session_key

            self.sessions.set_slack_link(
                effective_session_key(slot), thread_ts, channel_id
            )
        self.push_slots_update()

    def get_or_create_slot(
        self,
        name: str | None = None,
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str | None = None,
        ephemeral: bool | None = None,
        app: str = "",
        linked_session_key: str = "",
        channel_origin: bool = False,
    ) -> _ChatSlot:
        """Return existing slot or create a new one.

        *linked_session_key* binds a new slot to the session its conversation
        actually runs on (a channel thread, a cron job). It must be supplied
        here rather than assigned afterwards: the Slack-link hydration below
        reads the persisted link off the slot's effective session key, so a
        binding applied later would hydrate against the wrong key and leave a
        channel-born tab looking unlinked.
        """
        requested_name = ""
        if name:
            # Slot keys flow into the session key (``dashboard:{slot.key}``)
            # that kirocrew-core sends as the ``X-Session-Key`` HTTP header
            # (latin-1 per RFC 7230) AND into the persisted JSONL
            # filename via the history layer's lossy ``_safe_key()`` fold.
            # Normalize to the filename charset *before* the lookup so the key
            # is header-, filesystem-, and restore-round-trip-safe: the key now
            # equals its filename stem, so the two restart restore paths
            # (open_slots.json replay vs filename-stem walk) converge on one
            # slot instead of duplicating the session in the sidebar.
            requested_name = name
            name = _normalize_slot_key(name)
            if not name:
                # Degenerate input (e.g. a bare "dashboard:" prefix) — fall
                # through to an auto-generated key without title seeding.
                requested_name = ""
        if name and name in self._slots:
            existing = self._slots[name]
            if memory_mode is not None and memory_mode != existing.memory_mode:
                raise ValueError(
                    f"Slot {name!r} already exists with memory_mode={existing.memory_mode!r}"
                )
            return existing
        if not name:
            self._slot_counter += 1
            ts = int(time.time())
            name = _mint_slot_key("chat", self._slot_counter, ts)
        slot = _ChatSlot(
            name,
            agent=agent,
            workspace=workspace,
            model=model,
            mode=mode,
            memory_mode=memory_mode or "persistent",
        )
        if requested_name and requested_name != name:
            # The caller asked for a human-readable name (e.g. "Artifact: My
            # Doc"); the key had to be folded, but the pretty form makes a
            # better initial title than the "New session" placeholder. Titles
            # are dashboard-surfaced, so apply the same redaction as explicit
            # title pinning in api_chat_slot_create. ``_titled`` stays False —
            # auto-title and explicit pinning can still override.
            pretty_title, _ = redact_exfiltration_urls(requested_name)
            pretty_title, _ = redact_credentials(pretty_title)
            slot.title = pretty_title
        slot._tab_id = uuid.uuid4().hex[:12]
        slot._on_message = self._broadcast_chat_message
        slot._app = app
        if memory_mode and memory_mode != "persistent":
            self._restricted_keys.add(f"dashboard:{name}")
        if ephemeral:
            self._ephemeral_keys.add(f"dashboard:{name}")
        # Hydrate only a complete, genuine Slack link. Other transports still
        # write their namespaced origin id through the legacy channel field;
        # those are projected separately via ``links`` and must never make the
        # destructive Slack actions appear.
        if channel_origin:
            # Additive: never cleared, because get_or_create_slot also returns
            # EXISTING slots and a later plain call must not downgrade a tab
            # that a channel path already claimed.
            slot.channel_origin = True
        if linked_session_key:
            slot.linked_session_key = linked_session_key
        elif self.sessions:
            # No caller-supplied binding, but a channel-stem name means this slot
            # displays a conversation that runs on the channel's own session.
            # Resolving it HERE rather than in each caller is what makes the
            # binding correct by construction: the History resume path builds the
            # slot without one, and an unbound channel tab silently answers from a
            # dashboard-only session whose replies never reach the thread.
            #
            # Only ever adopts a key the session map actually holds, so a slot
            # whose name merely looks channel-shaped stays unbound. Validated the
            # same way ``surface_channel_session`` validates its own argument:
            # only a real channel key may become a binding, so a malformed map
            # answer leaves the slot unbound (a supported state) rather than
            # routing the user's replies to a session no channel reads.
            if is_channel_session_key(name):
                resolved = self.sessions.channel_key_for_stem(name)
                if isinstance(resolved, str) and is_channel_session_key(resolved):
                    slot.linked_session_key = resolved
        try:
            if self.sessions:
                from kiro_crew.dashboard.chat_utils import effective_session_key

                _ts, _ch = self.sessions.get_slack_link(effective_session_key(slot))
                slot._slack_linked = _is_genuine_slack_link(_ts, _ch)
                if slot._slack_linked:
                    namespaced = _split_namespaced_channel_id(_ch)
                    slot._slack_channel = namespaced[1] if namespaced else (_ch or "")
                    slot._slack_thread_ts = _ts or ""
                    # Rebuild the thread -> slot index too, not just the fields:
                    # inbound replies resolve through the index, so restoring
                    # the fields alone leaves a mirrored session delivering to
                    # Slack but not back to its tab after a restart.
                    #
                    # Index ONLY a genuine mirror-OUT. A channel-born session's
                    # ``slack_thread_ts`` is a SELF-reference -- the thread the
                    # session lives IN, not one it mirrors TO -- and indexing
                    # that would make every inbound Slack message resolve to a
                    # "linked" slot and run through the dashboard chat runner
                    # instead of the Slack transport, silently changing the
                    # execution engine and approval semantics of all Slack
                    # traffic.
                    #
                    # Both tests are load-bearing and neither is a name
                    # heuristic. A channel slot whose stem RESOLVED is caught by
                    # ``linked_session_key``; one whose stem did NOT resolve
                    # (leaving that field empty) is caught by comparing the link
                    # against the slot's own filename stem, because a channel
                    # slot is named for the very thread it lives in. A dashboard
                    # slot that merely happens to be named ``slack_...`` matches
                    # neither test and is still indexed.
                    from kiro_crew.history import _safe_key
                    from kiro_crew.messaging.link import canonical_key

                    _self_ref = False
                    if _ts:
                        _self_ref = _safe_key(canonical_key(_ts)) == name
                    if _ts and not slot.linked_session_key and not _self_ref:
                        self._slack_to_slot[_ts] = name
        except Exception:
            pass
        self._slots[name] = slot
        self.push_slots_update()
        return slot

    def reseed_slot_counter(self) -> None:
        """Advance ``_slot_counter`` past the highest index among live slots.

        ``__init__`` resets ``_slot_counter`` to 0 on every gateway boot, but
        the startup restore paths (``restore_open_slots`` then
        ``restore_recent_sessions``) rehydrate the user's tabs under their
        original ``chat-<N>-<ts>`` keys without touching the counter. The first
        new slot minted after a restart would then re-use a low index
        (``chat-1-...``) that collides with an already-restored tab holding that
        same index, scrambling the frontend's tab -> session binding so a
        restored tab loads the wrong session.

        Called once after the restore paths run in ``start_dashboard``. Parses
        the ``<prefix>-<N>-<ts>`` slot keys and seeds the counter to the max
        observed index so subsequent auto-minted slots always get fresh,
        collision-proof indices. Monotonic: only ever advances the counter,
        never lowers it, so it is safe to call regardless of restore order.
        """
        max_idx = self._slot_counter
        for name in self._slots:
            # Parse via the shared helper so this stays in lock-step with the
            # key minter (_mint_slot_key). Custom keys return None and skip.
            idx = _slot_index_from_key(name)
            if idx is not None and idx > max_idx:
                max_idx = idx
        if max_idx != self._slot_counter:
            # Symmetric with restore_recent_sessions' "Restored %d session(s)"
            # log so a future recurrence of the collision is observable.
            logger.info(
                "Reseeded slot counter %d -> %d past highest restored slot index",
                self._slot_counter,
                max_idx,
            )
        self._slot_counter = max_idx

    def _broadcast_chat_message(self, slot_key: str, msg: dict) -> None:
        """Push a chat message to all SSE clients via the global stream."""
        payload: dict[str, Any] = {
            "_type": "chat_message",
            "slot": slot_key,
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
            "ts": msg.get("ts", ""),
        }
        # Include cls for backward compatibility
        cls_val = msg.get("cls", "")
        if cls_val:
            payload["cls"] = cls_val
            # Parse cls as JSON to send structured meta field for new frontend
            meta = parse_cls_meta(cls_val)
            if meta is not None:
                payload["meta"] = meta
        # Also include direct meta (e.g. tool_call_id on tool messages).
        #
        # Deliberately NOT redacted here, unlike the `cls` branch above (which is
        # sanitised by parse_cls_meta). Two reasons, both load-bearing:
        #
        # 1. This is the LIVE oauth banner's egress path. _emit_mcp_oauth_request
        #    appends the banner with a real `oauth_url`, already gated by
        #    _oauth_url_contains_credential — a gate that deliberately exempts OAuth
        #    params from the query-length / base64 heuristics because those
        #    "would reject every real OAuth URL". Running _redact_meta_for_role here
        #    would blank a genuine Google/GitHub consent URL and break the user's
        #    ability to authorize an MCP server.
        # 2. chat_utils imports from this module, so importing the redactors the
        #    other way would be a cycle.
        #
        # What makes that safe: live tool meta is redacted at source (_tool_meta),
        # and a DISK-LOADED message reaches this path only when the caller opts
        # in per-role. Both restore loops pass broadcast=False, and the ONE
        # exception is refresh_channel_window, which replays a channel
        # transcript's tail and passes broadcast_user=True so a message typed in
        # Slack renders at all (nothing rendered it optimistically here). That
        # exception cannot carry unredacted meta: ConversationLog.append writes
        # only role/content/ts/source_thread/source_user for such a row -- no
        # meta dict -- so the arm below never fires for it, and the row's
        # content is human-typed, which is deliberately raw at every other
        # boundary too. The invariant is pinned by
        # test_rehydrate_does_not_broadcast_replayed_messages and
        # test_restore_recent_sessions_does_not_broadcast_either. Do not relax
        # it further without re-checking that meta is still absent.
        direct_meta = msg.get("meta")
        if direct_meta and isinstance(direct_meta, dict):
            payload["meta"] = {**(payload.get("meta") or {}), **direct_meta}
        self._broadcast(payload)

    # ── Folder persistence ──

    _FOLDERS_FILE = "folders.json"
    _TAGS_FILE = "tags.json"
    _TAG_BOARDS_FILE = "tag_boards.json"

    # Seed vocabulary created on first run when tags.json is missing or empty.
    # status=True tags are mutually-exclusive workflow states. Drag-between-columns
    # strips all status tags from a card and applies the destination column's
    # status tag. Non-status tags survive the drag.
    _DEFAULT_TAGS: list[dict[str, Any]] = [
        {"id": "planned", "name": "Planned", "color": "#6b7280", "order": 0, "status": True},
        {"id": "todo", "name": "ToDo", "color": "#3b82f6", "order": 1, "status": True},
        {
            "id": "implementation",
            "name": "Implementation",
            "color": "#8b5cf6",
            "order": 2,
            "status": True,
        },
        {"id": "review", "name": "Review", "color": "#f59e0b", "order": 3, "status": True},
        {"id": "done", "name": "Done", "color": "#10b981", "order": 4, "status": True},
    ]

    def load_folders(self) -> None:
        """Load folder definitions from disk."""
        path = config_dir() / self._FOLDERS_FILE
        try:
            if path.exists():
                self._folders = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load folders", exc_info=True)

    def save_folders(self) -> None:
        """Persist folder definitions to disk (atomic write)."""
        path = config_dir() / self._FOLDERS_FILE
        self._atomic_write_json(path, self._folders)

    _CRON_FOLDERS_FILE = "cron_folders.json"

    def load_cron_folders(self) -> None:
        """Load cron folder definitions from disk.

        Validates the loaded shape: the file must contain a JSON array of
        folder objects. Anything else (a hand-edited ``{}``, a string, or
        malformed entries) is discarded with a warning instead of being
        assigned verbatim — a non-list value would flow to the frontend
        and crash grouping (``folders.map is not a function``).
        """
        path = config_dir() / self._CRON_FOLDERS_FILE
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, list):
                    logger.warning(
                        "Ignoring %s: expected a JSON array, got %s",
                        self._CRON_FOLDERS_FILE,
                        type(loaded).__name__,
                    )
                    return
                valid = [
                    f
                    for f in loaded
                    if isinstance(f, dict)
                    and isinstance(f.get("id"), str)
                    and f.get("id")
                    and isinstance(f.get("name"), str)
                    and f.get("name")
                    and isinstance(f.get("order"), (int, float))
                    and not isinstance(f.get("order"), bool)
                ]
                if len(valid) != len(loaded):
                    logger.warning(
                        "Dropped %d malformed entr(ies) while loading %s",
                        len(loaded) - len(valid),
                        self._CRON_FOLDERS_FILE,
                    )
                self._cron_folders = valid
        except Exception:
            logger.warning("Failed to load cron folders", exc_info=True)

    def save_cron_folders(self) -> None:
        """Persist cron folder definitions to disk (atomic write).

        Raises on I/O failure so callers can surface a 500 to the client
        rather than silently losing the write.
        """
        path = config_dir() / self._CRON_FOLDERS_FILE
        self._atomic_write_json_strict(path, self._cron_folders)

    def create_cron_folder(self, name: str, folder_id: str) -> dict:
        """Create a new cron folder and persist.

        Returns the created folder dict. Raises on persistence failure
        (callers should surface a 500); in-memory state is rolled back.
        """
        order = max((f["order"] for f in self._cron_folders), default=-1) + 1
        folder = {"id": folder_id, "name": name, "order": order}
        self._cron_folders.append(folder)
        try:
            self.save_cron_folders()
        except Exception:
            self._cron_folders.pop()
            raise
        return folder

    def rename_cron_folder(self, folder_id: str, name: str) -> dict | None:
        """Rename a cron folder and persist.

        Returns the updated folder dict, or None if folder_id not found.
        Raises on persistence failure (callers should surface a 500);
        original name is restored on failure.
        """
        for folder in self._cron_folders:
            if folder["id"] == folder_id:
                old_name = folder["name"]
                folder["name"] = name
                try:
                    self.save_cron_folders()
                except Exception:
                    folder["name"] = old_name
                    raise
                return folder
        return None

    def delete_cron_folder(self, folder_id: str) -> bool:
        """Remove a cron folder and clear its assignment on all jobs.

        Returns True if the folder existed, False otherwise.
        Raises on persistence failure (callers should surface a 500).

        Ordering: the folder removal is the single authoritative write —
        it is removed from memory and persisted FIRST (rolled back in
        memory if the save fails, keeping memory consistent with disk).
        Job ``folder_id`` clears happen afterwards as best-effort cleanup:
        a dangling ``folder_id`` is benign (grouping renders unknown ids
        in the Ungrouped bucket, and a job's next folder move overwrites
        it), so a crash or per-job failure between writes can never strand
        jobs in a half-deleted state — the folder is either fully present
        or fully gone.
        """
        if not any(f["id"] == folder_id for f in self._cron_folders):
            return False
        # Remove the folder definition and persist — the one write that
        # decides whether the delete happened.
        snapshot = list(self._cron_folders)
        self._cron_folders = [f for f in self._cron_folders if f["id"] != folder_id]
        try:
            self.save_cron_folders()
        except Exception:
            self._cron_folders = snapshot
            raise
        # Best-effort: clear the now-dangling folder_id on affected jobs.
        # Failures are logged and tolerated — consumers treat an unknown
        # folder_id as ungrouped, so a leftover id has no user-visible
        # effect and self-heals on the job's next folder assignment.
        for job in self.crons.list_jobs(include_disabled=True):
            if job.folder_id == folder_id:
                try:
                    self.crons.update_job(job.id, folder_id="")
                except Exception:
                    logger.warning(
                        "Failed to clear folder_id on job %s after folder delete "
                        "(benign: unknown ids render as ungrouped)",
                        job.id,
                        exc_info=True,
                    )
        return True

    def folder_breadcrumb(self, folder_id: str, sep: str = " › ") -> str:
        """Render a folder's ancestry root→leaf as a breadcrumb string.

        Walks the ``parent_id`` chain up to the root, then joins names with
        *sep*. Cycle-safe (a visited set bounds the walk) and tolerant of
        dangling ``parent_id`` references. Returns "" for an empty or unknown
        folder id.
        """
        if not folder_id:
            return ""
        # load_folders() does no id-filtering (unlike load_tags), so a legacy or
        # corrupt folders.json may contain dicts lacking an "id" key. Skip those
        # rather than letting a hard index raise KeyError mid-walk — the docstring
        # promises tolerance of dangling references and "" for an unknown id.
        by_id = {f["id"]: f for f in self._folders if isinstance(f, dict) and f.get("id")}
        names: list[str] = []
        seen: set[str] = set()
        fid = folder_id
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            folder = by_id[fid]
            names.append(str(folder.get("name", "")))
            fid = str(folder.get("parent_id") or "")
        names.reverse()
        return sep.join(n for n in names if n)

    def load_tags(self) -> None:
        """Load tag vocabulary and sidebar columns from disk; seed defaults if missing.

        Only seed when ``tags.json`` does not exist. An explicitly-empty file
        is left as-is (so a user who deletes every tag stays at zero tags
        across restarts), and a parse failure is left untouched (so a
        transient I/O error never silently overwrites saved data).
        """
        tags_path = config_dir() / self._TAGS_FILE
        file_existed = tags_path.exists()
        try:
            if file_existed:
                raw = json.loads(tags_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._tags = [t for t in raw if isinstance(t, dict) and t.get("id")]
        except Exception:
            logger.warning("Failed to load tags", exc_info=True)
            # Treat a parse error like a present file: do not re-seed.
            file_existed = True
        # Back-fill the status flag for legacy tags saved before the field existed.
        # The 5 seed ids are canonical status tags; everything else defaults to False.
        seed_ids = {t["id"] for t in self._DEFAULT_TAGS}
        mutated = False
        for t in self._tags:
            if "status" not in t:
                t["status"] = t.get("id") in seed_ids
                mutated = True
        if not file_existed and not self._tags:
            # Fresh install (no tags.json on disk) — seed the default vocabulary.
            self._tags = [dict(t) for t in self._DEFAULT_TAGS]
            mutated = True
        if mutated:
            self.save_tags()

        # Column layout: flat list of {id, name, tag_ids, mode, order}.
        # Empty list = single implicit "all sessions" column (legacy UX).
        columns_path = config_dir() / self._TAG_BOARDS_FILE
        try:
            if columns_path.exists():
                raw = json.loads(columns_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._tag_boards = [c for c in raw if isinstance(c, dict) and c.get("id")]
        except Exception:
            logger.warning("Failed to load sidebar columns", exc_info=True)

    def save_tags(self) -> None:
        """Persist tag vocabulary to disk (atomic write)."""
        self._atomic_write_json(config_dir() / self._TAGS_FILE, self._tags)

    def save_tag_boards(self) -> None:
        """Persist sidebar column layout to disk (atomic write)."""
        self._atomic_write_json(config_dir() / self._TAG_BOARDS_FILE, self._tag_boards)

    @staticmethod
    def _atomic_write_json_strict(path: Path, data: Any) -> None:
        """Atomic JSON write that RAISES on failure (no swallowing).

        Used by persistence helpers where the caller needs to know about
        write failures (e.g. to return HTTP 500).
        """
        payload = json.dumps(data).encode()
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            # fdopen takes ownership of fd; file-object write() guarantees
            # the full buffer is written or an exception is raised (a bare
            # os.write may return a short count silently, which would let
            # os.replace() install truncated JSON).
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        """Atomic JSON write used by folder/tag persistence helpers.

        Delegates to _atomic_write_json_strict but swallows errors (logs a
        warning instead of raising).
        """
        try:
            DashboardState._atomic_write_json_strict(path, data)
        except Exception:
            logger.warning("Failed to write %s", path.name, exc_info=True)

    def source_link_urls(self) -> list[str]:
        """URLs of the sidebar-visible PR/MR chips across all slots.

        Only the links each slot actually serializes (the first
        ``_SERIALIZED_SOURCE_LINKS_PER_SLOT``) are returned — these are the
        chips whose check status the periodic owner-WS refresh keeps fresh.
        Reads the per-slot revision cache, so this is cheap to call on a timer.

        Issue links are excluded: the check-status path reaches ``gh pr view``
        and has no meaning for an issue.
        """
        urls: list[str] = []
        for s in self._slots.values():
            urls.extend(
                link["url"]
                for link in _budgeted_source_links(s._pr_source_links())
                if link.get("kind", "change") == "change"
            )
        return urls

    def source_link_urls_for_slot(self, key: str) -> list[str]:
        """Sidebar-visible PR/MR chip URLs for one slot (same cap and kind filter)."""
        slot = self._slots.get(key)
        if slot is None:
            return []
        return [
            link["url"]
            for link in _budgeted_source_links(slot._pr_source_links())
            if link.get("kind", "change") == "change"
        ]

    def push_source_status(self, delta: dict) -> None:
        """Push a single PR/MR status delta to owner websockets only.

        Chip status is credential-backed provider data, so this never reaches
        non-owner or app-token clients. Fire-and-forget: the panel's own poll
        remains the safety net if a client misses the event.
        """
        if not self._owner_ws_clients:
            return
        self._send_ws_owners(json.dumps({"type": "source_status", "data": delta}))

    def refresh_slot_source_status(self, key: str) -> None:
        """Re-read this slot's PR/MR status now — called at agent turn boundaries.

        A turn that just ran ``gh pr create``, pushed a revision, or drove a
        review round is exactly when a PR's lifecycle moved, and nothing else in
        the system invalidates the status caches on that event: the chips would
        wait out the periodic rotation and the detail panel would not refetch at
        all. Owner-gated (status is credential-backed, and with no owner window
        open there is nobody to render it, so no provider subprocess is spawned)
        and rate-floored inside ``request_check_refresh_now``.
        """
        if not self._owner_ws_clients:
            return
        try:
            urls = self.source_link_urls_for_slot(key)
            if not urls:
                return
            from kiro_crew.dashboard.handlers.source_providers import (
                request_check_refresh_now,
            )

            request_check_refresh_now(urls, self.push_slots_update)
        except Exception:
            logger.debug("turn-boundary source status refresh failed", exc_info=True)

    def _channel_link_is_live(self, link: ChannelLink) -> bool:
        """Is a proactive-capable transport registered for this channel?

        Deliberately an IN-MEMORY check only. This runs per linked slot inside
        ``serialize_slots``, which sits on the ``push_slots_update`` websocket
        broadcast path, so it must not touch the filesystem: the full governed
        ladder (``chat_runner._resolve_channel_target``) calls
        ``governance_permits``, which walks the profile directory (``iterdir`` +
        ``stat``, with a possible reload) — a slow filesystem there would block
        the event loop on every push and can drive watchdog restarts.

        Governance stays enforced at the async SEND boundary (
        ``_resolve_mirror_target`` in the turn path and in the mirror-link
        reminder handler). A link may therefore read ``live: true`` here and
        still be refused at send time; that asymmetry is deliberate and safe —
        the menu affordance is optimistic, the side effect is gated.
        """
        if link.channel_type == SLACK_NAMESPACE or not link.channel_id:
            return False
        transport = self.get_channel_transport(link.channel_type)
        if transport is None:
            return False
        return bool(
            getattr(
                getattr(transport, "capabilities", None),
                "supports_proactive_send",
                False,
            )
        )

    def _slot_links(self, slot: _ChatSlot) -> tuple[list[dict[str, Any]], bool, str, str]:
        """Build the redacted channel-neutral link projection for one slot."""
        # circular import: chat imports state at module scope.
        from kiro_crew.dashboard.chat_utils import effective_session_key

        session_key = effective_session_key(slot)
        mirror: ChannelLink | None = None
        persisted_ts: str | None = None
        persisted_channel: str | None = None
        try:
            candidate = self.sessions.get_mirror_link(session_key)
            if isinstance(candidate, ChannelLink):
                mirror = candidate
        except Exception:
            pass
        try:
            raw_ts, raw_channel = self.sessions.get_slack_link(session_key)
            persisted_ts = raw_ts if isinstance(raw_ts, str) else None
            persisted_channel = raw_channel if isinstance(raw_channel, str) else None
        except Exception:
            pass

        # Prefer persisted values, but retain explicit in-memory Slack links in
        # tests and during the short interval before persistence is observable.
        slack_ts = persisted_ts or slot._slack_thread_ts
        slack_channel = persisted_channel or slot._slack_channel
        namespaced_origin = _split_namespaced_channel_id(persisted_channel)
        genuine_slack = _is_genuine_slack_link(slack_ts, slack_channel)
        # A Slack-BORN session's ``slack_thread_ts`` names the thread it LIVES
        # in, not a mirror target somewhere else: the Slack inbound handler
        # writes it every turn as the thread registry that routes replies back.
        # That makes it a self-reference, and the sidebar already draws an origin
        # glyph from the slot key -- so surfacing it as an outbound mirror badges
        # one conversation twice and offers a session its own origin thread as a
        # releasable mirror. A Slack-born session that genuinely mirrors to a
        # DIFFERENT thread still carries a different ts, so it is unaffected.
        slack_origin_self_link = (
            channel_namespace_of(session_key) == SLACK_NAMESPACE
            and bool(slack_ts)
            and session_key.endswith(slack_ts)
        )
        links: list[dict[str, Any]] = []

        def append_link(link: ChannelLink, direction: str) -> None:
            channel_type = (link.channel_type or "").lower()
            if not channel_type:
                return
            channel_id = link.channel_id or ""
            nested = _split_namespaced_channel_id(channel_id)
            if nested and nested[0] == channel_type:
                channel_id = nested[1]
            normalized = ChannelLink(channel_type, channel_id, link.thread_id)
            links.append(
                {
                    "channel": channel_type,
                    "label": _link_label(channel_type),
                    "target": _redacted_link_target(channel_id),
                    "direction": direction,
                    "live": self._channel_link_is_live(normalized),
                }
            )

        # Non-Slack transports currently leak their home conversation through
        # slack_channel_id. Surface that as a read-only origin, never a Slack
        # mirror. This prefix sniff is intentionally defensive for unknown
        # future channel types too.
        if namespaced_origin and namespaced_origin[0] != SLACK_NAMESPACE:
            append_link(
                ChannelLink(namespaced_origin[0], namespaced_origin[1]),
                "origin",
            )

        if mirror is not None:
            if mirror.channel_type == SLACK_NAMESPACE:
                # get_mirror_link synthesizes Slack for the legacy fields. If
                # those fields actually hold a namespaced non-Slack origin, the
                # origin above is the only truthful representation.
                if not namespaced_origin and genuine_slack and not slack_origin_self_link:
                    append_link(
                        ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                        "out",
                    )
            else:
                # A resume binding (set by an in-channel `!sessions` pick) routes
                # BOTH ways: this session's replies go to that channel AND
                # messages from it are delivered back here. That is a materially
                # different thing for the user to see and release than an
                # outbound-only `!link` mirror, so it gets its own direction
                # rather than being flattened into "out". Slack is excluded by
                # the branch above — it carries inbound on its own thread index
                # and never sets the marker.
                inbound = False
                try:
                    inbound = bool(self.sessions.mirror_accepts_inbound(session_key))
                except Exception:
                    # Older/stubbed SessionManagers may not expose the accessor;
                    # degrade to the outbound reading rather than dropping the link.
                    inbound = False
                append_link(mirror, "both" if inbound else "out")
        elif genuine_slack and not slack_origin_self_link:
            # Defensive fallback for SessionManager test doubles or older
            # implementations that expose get_slack_link but not get_mirror_link.
            append_link(
                ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                "out",
            )

        if genuine_slack and not slack_origin_self_link:
            slack_namespace = _split_namespaced_channel_id(slack_channel)
            visible_slack_channel = slack_namespace[1] if slack_namespace else (slack_channel or "")
            return links, True, visible_slack_channel, slack_ts or ""
        return links, False, "", ""

    def serialize_slot(
        self, slot: _ChatSlot, *, include_check_status: bool = False
    ) -> dict[str, Any]:
        """Serialize one slot with state-backed channel-link metadata."""
        payload = slot.to_dict(include_check_status=include_check_status)
        links, slack_linked, slack_channel, slack_thread_ts = self._slot_links(slot)
        payload.update(
            {
                "links": links,
                "slack_linked": slack_linked,
                "slack_channel": slack_channel,
                "slack_thread_ts": slack_thread_ts,
            }
        )
        return payload

    def serialize_slots(self, *, include_check_status: bool = False) -> list:
        """Serialize slots, optionally including owner-only provider status.

        ``subagents_running`` remains available to every authenticated caller.
        Credential-backed ``ci`` and ``state`` fields are omitted unless an
        authenticated owner boundary explicitly opts in.
        """
        out = []
        subs = getattr(self, "subagents", None)
        for s in self._slots.values():
            d = self.serialize_slot(s, include_check_status=include_check_status)
            d["subagents_running"] = bool(subs and subs.running_agents_for(f"dashboard:{s.key}"))
            out.append(d)
        return out

    @contextlib.contextmanager
    def suspend_slots_push(self) -> "Iterator[None]":
        """Coalesce every ``push_slots_update()`` inside the block into one at exit.

        ``get_or_create_slot`` broadcasts the FULL slot list on each call, so a bulk
        restore of N tabs serializes 1+2+…+N slots — O(N²) ``to_dict``/redaction
        work for intermediate states no client will ever render (measured ~1.3s at
        N=77, and it grows quadratically). Wrap the restore, emit one broadcast.

        Depth-counted so nested use is safe (an inner block must not flush early),
        and ``@contextmanager``'s try/finally unwinds the depth even if the body
        raises. Only flushes if something actually asked to push.
        """
        self._slots_push_suspend += 1
        try:
            yield
        finally:
            self._slots_push_suspend -= 1
            if self._slots_push_suspend == 0 and self._slots_push_pending:
                self._slots_push_pending = False
                self.push_slots_update()

    def push_slots_update(self) -> None:
        """Push slots, keeping provider status confined to owner websockets."""
        from kiro_crew.dashboard.handlers.source_providers import (
            gitlab_hosts_generation,
        )

        if self._slots_push_suspend:
            # Inside suspend_slots_push(); remember that a push is owed and let the
            # outermost block emit a single coalesced broadcast on exit.
            self._slots_push_pending = True
            return

        yolo_active = self.is_yolo_active()  # expire first if needed
        slots_data = self.serialize_slots()
        mgr = getattr(self, "channel_manager", None)
        ch_trusted = bool(mgr and any(ch.trusted for ch in mgr._channels.values()))
        # Piggyback the allowlist generation so clients invalidate the cached
        # ['dashboardConfig'] query only when the GitLab-hosts allowlist actually
        # changed -- an event-driven refresh that replaces a constant 30s poll
        # (which multiplied audit-log writes across every same-key observer).
        self._broadcast(
            {
                "_type": "slots",
                "_slots_list": slots_data,
                "_yolo": yolo_active,
                "slots": json.dumps(slots_data),
                "channelTrusted": ch_trusted,
                "gitlabHostsGeneration": gitlab_hosts_generation(),
            }
        )
        owner_ws_clients = getattr(self, "_owner_ws_clients", None)
        if owner_ws_clients:
            owner_slots = self.serialize_slots(include_check_status=True)
            self._send_ws_owners(
                json.dumps(
                    {
                        "type": "slots",
                        "data": owner_slots,
                        "yolo": yolo_active,
                        "channelTrusted": ch_trusted,
                    }
                )
            )

    def push_slot_title(self, key: str, title: str, *, full: bool = True) -> None:
        """Push a targeted title update for a single slot.

        By default also pushes a full slots update so the sidebar reflects the
        new title without callers needing to do both. Pass ``full=False`` for
        high-frequency streaming partials (word-by-word title reveal) to send
        only the lightweight ``slot_title`` event; finalize with a ``full=True``
        call once.
        """
        self._broadcast({"_type": "slot_title", "key": key, "title": title})
        if full:
            self.push_slots_update()

    def push_artifact_update(self, slug: str, version: int, *, deleted: bool = False) -> None:
        """Broadcast an artifact content change to all connected clients.

        Emitted from the artifact mutation funnel (create / content update /
        revert / pull-latest / relocate / delete) so every open dashboard
        window — main, popouts, companion chat panels — can invalidate its
        artifact queries immediately instead of waiting for the 30s react-query
        staleness window. Fire-and-forget, best-effort: the
        staleness window remains the safety net if a client misses the event.
        """
        self._broadcast(
            {
                "_type": "artifact_update",
                "slug": slug,
                "version": version,
                "deleted": deleted,
            }
        )

    def push_refresh(self, *kinds: str) -> None:
        """Push a lightweight refresh hint for specific data types.

        The frontend receives ``event: refresh`` with ``data: kind1,kind2``
        and fetches fresh data only for those types.  This replaces blind
        polling — the server tells the client *when* to refresh, not the
        client guessing on a timer.

        Supported kinds: ``crons``, ``lessons``, ``agents``, ``history``,
        ``taskrunner``.
        """
        self._broadcast({"_type": "refresh", "kinds": ",".join(kinds)})

    def push_update_progress(self, step: str, detail: str = "") -> None:
        """Broadcast an update progress event to all connected clients.

        ``step`` is a short machine-readable phase name (e.g. ``pulling``,
        ``syncing``, ``building``, ``installing``, ``restarting``, ``failed``).
        ``detail`` is an optional human-readable message.
        """
        self._update_progress = {"step": step, "detail": detail}
        self._broadcast(
            {
                "_type": "update_progress",
                "step": step,
                "detail": detail,
            }
        )

    def clear_update_progress(self) -> None:
        """Reset update progress (e.g. after cancel or completion)."""
        self._update_progress = None

    def _broadcast(self, note: dict[str, Any]) -> None:
        """Send a message to all connected SSE and WS clients."""
        for q in self._sse_queues:
            try:
                q.put_nowait(note)
            except asyncio.QueueFull:
                pass
        self._notify_event.set()
        # WS broadcast — translate internal _type to WS message format
        if self._ws_clients:
            msg_type = note.get("_type", "notification")
            if msg_type == "slots":
                slots_list = note.get("_slots_list") or json.loads(note["slots"])
                ws_msg = json.dumps(
                    {
                        "type": "slots",
                        "data": slots_list,
                        "yolo": note.get("_yolo", False),
                        "channelTrusted": note.get("channelTrusted", False),
                        # Forwarded explicitly: this envelope is rebuilt key-by-key,
                        # so anything not named here is silently dropped. The client
                        # invalidates its cached dashboard config when this changes.
                        "gitlabHostsGeneration": note.get("gitlabHostsGeneration"),
                    }
                )
            elif msg_type == "slot_title":
                ws_msg = json.dumps(
                    {"type": "slot_title", "data": {"key": note["key"], "title": note["title"]}}
                )
            elif msg_type == "refresh":
                ws_msg = json.dumps(
                    {"type": "refresh", "data": {"kinds": note["kinds"].split(",")}}
                )
            elif msg_type == "update_progress":
                ws_msg = json.dumps(
                    {
                        "type": "update_progress",
                        "data": {"step": note["step"], "detail": note.get("detail", "")},
                    }
                )
            elif msg_type == "artifact_update":
                # Typed envelope (not the generic `notification` fallback) so
                # useWebSocket and future consumers get a self-documenting
                # event: {slug, version, deleted}.
                ws_msg = json.dumps(
                    {
                        "type": "artifact_update",
                        "data": {
                            "slug": note["slug"],
                            "version": note.get("version", 0),
                            "deleted": note.get("deleted", False),
                        },
                    }
                )
            elif msg_type == "chat_message":
                chat_data: dict[str, Any] = {
                    "slot": note["slot"],
                    "role": note["role"],
                    "content": note["content"],
                    "ts": note.get("ts", ""),
                }
                # Include cls for messages with metadata (e.g. permission with tool_input)
                if note.get("cls"):
                    chat_data["cls"] = note["cls"]
                if note.get("meta"):
                    chat_data["meta"] = note["meta"]
                ws_msg = json.dumps({"type": "chat_message", "data": chat_data})
            else:
                ws_msg = json.dumps({"type": "notification", "data": note})
            self._send_ws_all(ws_msg)

    def _spawn_ws_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        """Fire-and-forget a WS send while retaining a strong task reference.

        ``asyncio.ensure_future(...)`` without keeping the returned task lets the
        event loop hold only a weak reference, so the task can be garbage-collected
        mid-send — silently dropping the websocket message (a lost dashboard update).
        Track it in ``_background_tasks`` (the existing pattern in this module) and
        discard on completion so the reference is held for the task's lifetime.
        """
        task = asyncio.ensure_future(ws.send_str(msg))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_ws_send_done)

    def _on_ws_send_done(self, task: asyncio.Task) -> None:
        """Discard the finished WS-send task and surface any failure.

        A failed ``ws.send_str`` (e.g. ``ConnectionResetError`` when a client
        disconnects mid-send) is otherwise swallowed silently — the task stores the
        exception, nobody reads it, and it's GC'd with the task — leaving operators
        blind to send failures under burst load. Log at DEBUG since peer disconnects
        are routine and expected, not errors.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("WS send failed (client likely disconnected): %s", exc)

    def _send_ws_all(self, msg: str) -> None:
        """Send a pre-serialized JSON string to all WS clients."""
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                self._spawn_ws_send(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._remove_ws(ws)

    def _send_ws_owners(self, msg: str) -> None:
        """Send a pre-serialized message only to owner-authenticated clients."""
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._owner_ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                self._spawn_ws_send(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._remove_ws(ws)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        """Send a typed message to all WS clients (not SSE)."""
        if not self._ws_clients:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        self._send_ws_all(msg)

    def broadcast_context_usage(self, slot_key: str, payload: dict) -> None:
        """Broadcast one ``context_usage`` frame AND record it as the slot's snapshot.

        The SINGLE writer for context-meter state. Every producer of a
        ``context_usage`` frame routes through here so the broadcast and the
        stored snapshot cannot drift: the meter is otherwise turn-scoped only,
        and reopening a session whose ACP process has expired (idle timeout or a
        gateway restart) leaves the bar at 0% until the next turn because
        nothing on the open path carries usage.

        ``payload`` is the frame as broadcast (``{slot, pct, used_tokens?,
        window_tokens?, reset?}``). The snapshot mirrors it plus the slot's
        model, which the read side compares to decide whether the reading still
        describes the session (see ``_context_snapshot_fields``). ``pct`` is
        the load-bearing field and is stored on its own when that is all the
        frame carries: kiro-cli commonly reports a percentage with no
        ``usage_update``, so requiring token counts here would leave the
        majority of sessions with nothing to restore. A post-compaction frame
        legitimately stores ``pct: 0`` — that IS the new truth, not an absence.

        Storage is a small sidecar map, NOT the session's metadata line:
        ``ConversationLog.update_metadata`` reads and rewrites the WHOLE
        transcript to edit its first line, so paying that per turn would scale
        a turn's I/O with transcript size (tens of MB on a long session) while
        holding the cross-process lock. The sidecar is O(open slots).
        """
        self.broadcast_ws("context_usage", payload)
        slot = self.get_slot(slot_key)
        if slot is None:
            return
        # Ephemeral tabs (incognito/temporary) leave no memory behind by
        # contract — same filter as _persist_open_slots.
        if getattr(slot, "memory_mode", "persistent") != "persistent":
            return
        pct = payload.get("pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            return
        snapshot: dict[str, Any] = {"pct": pct, "model": slot.model}
        window = payload.get("window_tokens") or 0
        if window:
            snapshot["window_tokens"] = window
            snapshot["used_tokens"] = payload.get("used_tokens", 0)
        with self._context_snapshots_lock:
            if self._context_snapshots.get(slot_key) == snapshot:
                return  # unchanged — nothing for the next flush to write
            self._context_snapshots[slot_key] = snapshot
            self._context_snapshots_dirty = True

    def ensure_context_snapshots_loaded(self) -> None:
        """Merge the on-disk snapshot file into the in-memory map. BLOCKING.

        Only readings taken by an EARLIER process need the file; anything this
        process recorded is already in memory. So the merge never overwrites a
        live entry — disk fills gaps, memory wins ties.

        The loaded flag flips only AFTER the merge is in the map, under the
        lock, so a concurrent flush can never observe ``loaded`` while the
        disk entries are still in flight — that ordering is what stops the
        flush from writing a memory-only view over readings it has not merged
        yet. Two concurrent loaders may both read the file; the second merge
        is a no-op because ties keep the in-memory value.

        Blocking by design and therefore never called from the event loop: the
        async slot-detail handler reaches it through ``asyncio.to_thread`` and
        the flush paths reach it from their executors. A missing or corrupt
        file leaves the map as-is; a lost snapshot only degrades the reopen case
        back to an empty bar.
        """
        with self._context_snapshots_lock:
            if self._context_snapshots_loaded:
                return
        try:
            raw = json.loads((config_dir() / "context_snapshots.json").read_text())
        except FileNotFoundError:
            raw = {}
        except Exception:
            logger.debug("context_snapshots.json unreadable; starting empty", exc_info=True)
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        with self._context_snapshots_lock:
            if self._context_snapshots_loaded:
                return
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, dict):
                    self._context_snapshots.setdefault(key, value)
            self._context_snapshots_loaded = True

    def context_snapshot_for(self, slot_key: str) -> dict | None:
        """Return a copy of the recorded reading for ``slot_key``, or ``None``.

        The read seam for the slot-detail handler: hands out a copy under the
        lock so the caller never holds a reference into the shared map.
        """
        with self._context_snapshots_lock:
            snapshot = self._context_snapshots.get(slot_key)
            return dict(snapshot) if isinstance(snapshot, dict) else None

    def _persist_context_snapshots(self) -> None:
        """Write the snapshot map to ``<config_dir>/context_snapshots.json``. BLOCKING.

        Called from ``_flush_dirty_slots`` (the flush loop's executor pass) and
        from the shutdown save in ``chat_persistence`` — the same off-loop
        paths ``_persist_open_slots`` uses, and for the same reason: a home
        directory on slow or network-backed storage can stall the write, and
        one stalled write on the event loop freezes every chat turn and the
        liveness heartbeat.

        The data lock is held for the in-memory work only — the dirty check,
        the disk merge, the prune, and serialization — never across the file
        write, so a stalled disk cannot block the event loop's writers. The
        flush lock then serializes whole flushes against each other: without
        it, the periodic and shutdown flushes can overlap and the slower one
        lands an OLDER serialization last, rolling the file back with the
        dirty flag already cleared. ANY
        failure re-arms the dirty flag and is swallowed: the flush loop treats
        a raising callee as fatal, and losing every future flush over one
        failed write would be a far worse trade than retrying in 5s. The prune
        reads ``self._slots`` from a worker thread the way
        ``_flush_dirty_slots`` and ``_persist_open_slots`` already do; if the
        loop resizes it mid-iteration the raise lands in the same retry path.

        Entries for slots that no longer exist are dropped on the way out, so a
        deleted session cannot leave its usage behind and the file stays bounded
        by the number of open slots.

        NO-OP while ``restoring_open_slots`` is set — the same guard
        ``_persist_open_slots`` carries, for the same reason: the startup
        restore yields to the event loop between tabs, so mid-restore
        ``self._slots`` holds only the tabs restored so far, and the prune
        would read that partial set as "deleted sessions" and permanently drop
        the readings of every tab still waiting to be restored. Skipping is
        always safe: the dirty flag stays set, so the first flush after the
        restore completes writes everything.
        """
        if self.restoring_open_slots:
            logger.debug("context snapshot flush skipped: restore in progress")
            return
        with self._context_snapshots_lock:
            if not self._context_snapshots_dirty:
                return
        self.ensure_context_snapshots_loaded()
        # _context_snapshots_flush_lock makes the serialize→write pair atomic
        # against the OTHER flush path, so a slower flush cannot land an older
        # serialization after a newer one and roll the file back.
        with self._context_snapshots_flush_lock:
            try:
                with self._context_snapshots_lock:
                    self._context_snapshots_dirty = False
                    live_keys = set(self._slots)
                    for key in [k for k in self._context_snapshots if k not in live_keys]:
                        del self._context_snapshots[key]
                    payload = json.dumps(self._context_snapshots)
                atomic_write(config_dir() / "context_snapshots.json", payload, mode=0o600)
            except Exception:
                logger.debug("Failed to persist context_snapshots.json", exc_info=True)
                with self._context_snapshots_lock:
                    self._context_snapshots_dirty = True

    async def deliver_ws_owners(self, msg_type: str, data: object) -> int:
        """Send a typed message ONLY to owner clients; return how many sends COMPLETED.

        Use this instead of :meth:`broadcast_ws` for payloads scoped to the
        dashboard user rather than to every subscriber — an app credential can
        open ``/api/ws`` and lands in ``_ws_clients``, so an all-clients broadcast
        of user-scoped content crosses the App Kit boundary.

        The return value is the count of sends that actually completed, for
        callers whose response reports delivery. A socket count is not a delivery count: the
        fire-and-forget path returns before any ``send_str`` runs, so a client
        that disconnects between the count and the send yields a failed send that
        was already reported as success. For an ephemeral, broadcast-only payload
        (nothing is stored server-side to re-deliver) that false success is the
        whole failure mode — the caller is told the user saw a card that was
        dropped on the floor.

        Sends run concurrently and failures are absorbed per socket: one dead
        peer must not hide a successful delivery to another window. Sockets that
        are already ``closed``, and those whose send raised, are removed here —
        the same cleanup the non-awaiting path performs.
        """
        targets = [ws for ws in list(self._owner_ws_clients) if not ws.closed]
        if not targets:
            return 0
        msg = json.dumps({"type": msg_type, "data": data})
        results = await asyncio.gather(
            *(ws.send_str(msg) for ws in targets), return_exceptions=True
        )
        delivered = 0
        for ws, result in zip(targets, results):
            if isinstance(result, BaseException):
                logger.debug("Owner WS send failed (client likely disconnected): %s", result)
                self._remove_ws(ws)
            else:
                delivered += 1
        for ws in list(self._owner_ws_clients):
            if ws.closed:
                self._remove_ws(ws)
        return delivered

    def broadcast_ws_owners(self, msg_type: str, data: object) -> None:
        """Send a typed message to OWNER-authorized WS clients only.

        For payloads that carry capability material (e.g. the MCP Apps
        ``mcp_app_render`` frame, which delivers the app's ``callback_secret``)
        — a non-owner or guest socket must never receive them.
        """
        if not getattr(self, "_owner_ws_clients", None):
            return
        msg = json.dumps({"type": msg_type, "data": data})
        self._send_ws_owners(msg)

    def ws_client_count(self) -> int:
        """Number of connected dashboard WS clients (live subscribers)."""
        return len(self._ws_clients)

    def broadcast_browser_event(self, event_type: str, data: dict) -> None:
        """Broadcast a browser activity event to all connected WS clients.

        Redacts string values to prevent credential leakage.
        """
        safe_data: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str):
                v, _ = redact_credentials(v)
                v, _ = redact_exfiltration_urls(v)
            safe_data[k] = v
        payload: dict[str, Any] = {"type": "browser_event", "event": event_type, "ts": time.time()}
        for k, v in safe_data.items():
            if k not in ("type", "event", "ts"):
                payload[k] = v
        self.broadcast_ws("browser_event", payload)

    def register_ws(self, ws: web.WebSocketResponse, *, owner: bool = False) -> None:
        """Register a WebSocket client and its owner authorization state."""
        self._ws_clients.append(ws)
        if owner:
            self._owner_ws_clients.add(ws)

    def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WebSocket client on disconnect."""
        self._remove_ws(ws)

    def _remove_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WS client from all subscriber lists."""
        try:
            self._ws_clients.remove(ws)
        except ValueError:
            pass
        self._owner_ws_clients.discard(ws)
        self._ws_log_subscribers.discard(ws)
        self._ws_subagent_subscribers.discard(ws)

    def subscribe_logs(self, ws: web.WebSocketResponse) -> None:
        """Subscribe a WS client to log events."""
        self._ws_log_subscribers.add(ws)

    def unsubscribe_logs(self, ws: web.WebSocketResponse) -> None:
        """Unsubscribe a WS client from log events."""
        self._ws_log_subscribers.discard(ws)

    def subscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._ws_subagent_subscribers.add(ws)

    def unsubscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._ws_subagent_subscribers.discard(ws)

    def broadcast_ws_subagent_subscribers(self, msg_type: str, data: object) -> None:
        """Send to subagent-subscribed clients only (for heavy chunk data)."""
        if not self._ws_subagent_subscribers:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_subagent_subscribers):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                self._spawn_ws_send(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._remove_ws(ws)

    async def close_all_ws(self) -> None:
        """Close all WebSocket connections (called on shutdown)."""
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_clients.clear()
        self._owner_ws_clients.clear()
        self._ws_log_subscribers.clear()
        self._ws_subagent_subscribers.clear()


# ── Notification persistence ──


def _redact_note_value(value: Any) -> Any:
    """Recursively redact every string inside a notification note value.

    Notes carry LLM-derived content in nested structures too (e.g. the
    ``actions`` field is a list of dicts whose ``label`` values may be
    model output), so redaction must descend into lists and dicts rather
    than only scanning top-level strings.
    """
    if isinstance(value, str):
        if not value:
            return value
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, list):
        return [_redact_note_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_note_value(item) for key, item in value.items()}
    return value


def _notifications_path() -> Path:
    """Path to the notifications JSONL file."""
    return config_dir() / _NOTIFICATIONS_FILE


def _note_ts_epoch(note: dict[str, Any]) -> float | None:
    """Best-effort epoch seconds for a note's ``ts`` (ISO string or epoch str)."""
    ts = note.get("ts")
    if ts is None:
        return None
    try:
        parsed = float(ts)
        # float() of a numeric STRING beyond float range (e.g. "-1e999")
        # returns inf/-inf without raising — a -inf epoch would make every
        # TTL comparison read "expired" and the sweep would destroy the row,
        # violating the never-destroy-on-ambiguity rule.
        # NaN likewise carries no ordering meaning. Treat both as
        # unparseable (note kept).
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, OverflowError):
        # OverflowError: float() of a JSON integer beyond float range (e.g.
        # 10**400) raises rather than returning inf — one poison row must
        # not abort the whole sweep.
        pass
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except (ValueError, OverflowError, OSError):
        # .timestamp() raises OverflowError/OSError (not just ValueError) for
        # platform-unrepresentable datetimes -- pre-epoch or far-future ISO
        # strings, most acutely on Windows. Treat them as unparseable (note
        # kept) rather than letting the error escape the sweep: at load time
        # that escape would hit _load_notifications' blanket handler, empty
        # the history, and the next mutation would persist the loss.
        return None


def sweep_expired_notifications(log: list[dict[str, Any]], *, now: float | None = None) -> int:
    """Remove expired PASSIVE notes in place (RFC Phase 5 TTL sweeper).

    A note expires when it is passive, carries a positive integer ``ttl``
    (seconds), and ``ts + ttl`` is in the past. Only passive notes sweep —
    critical/default history has recall value and stays until the user acts.
    Notes with unparseable timestamps are kept (never destroy on ambiguity).
    Returns the number of rows removed.
    """
    now = time.time() if now is None else now
    kept: list[dict[str, Any]] = []
    removed = 0
    try:
        for note in log:
            ttl = note.get("ttl")
            epoch = _note_ts_epoch(note)
            if (
                note.get("priority") == "passive"
                and isinstance(ttl, int)
                and not isinstance(ttl, bool)  # bool is an int subclass
                and ttl > 0
                and epoch is not None
                # ttl < now - epoch (not epoch + ttl < now): adding an
                # arbitrarily large int TTL to a float epoch raises
                # OverflowError, and the sweep-wide guard would abort the
                # whole sweep. int-vs-float comparison never overflows.
                and ttl < now - epoch
            ):
                removed += 1
                continue
            kept.append(note)
    except Exception:
        # The sweep is an optimization -- it must NEVER cost data. A poison
        # row escaping here at load time would hit _load_notifications'
        # blanket handler and empty the entire history (persisted on the
        # next mutation); in _deliver_note it would break every delivery.
        logger.warning("Notification TTL sweep aborted", exc_info=True)
        return 0
    if removed:
        log[:] = kept
    return removed


def _load_notifications() -> list[dict[str, Any]]:
    """Load persisted notifications from disk (newest last)."""
    path = _notifications_path()
    if not path.exists():
        return []
    try:
        entries: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = normalize_note(json.loads(line))
                # Redact at load: rows written before delivery-time redaction
                # existed may carry unredacted LLM-derived content; they are
                # served to SSE clients straight from this list.
                for key, value in parsed.items():
                    if key != "ts":
                        parsed[key] = _redact_note_value(value)
                entries.append(parsed)
            except Exception:  # noqa: BLE001 — skip the bad row, not the whole file
                # normalize_note/_redact_note_value can raise on valid-JSON
                # rows with unexpected shapes (e.g. a top-level array); keep
                # the per-line skip semantics instead of losing all history
                # to the outer except.
                logger.debug("Skipping malformed notification row", exc_info=True)
                continue
        # RFC Phase 5: drop expired passive rows BEFORE the recency cap.
        # Sweeping after truncation loses data: with more than N rows on
        # disk, newer expired-passive rows would displace older LIVE rows
        # during truncation, and the next full rewrite would delete those
        # live rows permanently. Disk rewrites lazily on
        # the next mutation; the in-memory view is authoritative for serving.
        sweep_expired_notifications(entries)
        # Keep only the most recent N live rows
        entries = entries[-_MAX_PERSISTED_NOTIFICATIONS:]
        return entries
    except Exception:
        logger.debug("Failed to load notifications", exc_info=True)
        return []


# Notification file I/O runs exclusively on this single-worker executor when
# an event loop is running: appends (from the delivery sink) and rewrites
# (from delete/ack/clear) execute strictly in submission order, so no lock is
# needed and the loop never blocks on file I/O.
_notification_io_pool: concurrent.futures.ThreadPoolExecutor | None = None


def _notification_io_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Lazily create the single-worker executor for notification persistence."""
    global _notification_io_pool
    if _notification_io_pool is None:
        _notification_io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="notif-io"
        )
    return _notification_io_pool


def _persist_notification(note: dict[str, str]) -> bool:
    """Append a single notification to the JSONL file on disk.

    Returns True on success. Failures are swallowed (legacy system producers
    are explicitly best-effort — history is a cache, delivery is the
    broadcast) but reported via the return value so callers that need
    durability (the app push endpoint) can surface them.
    """
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(note) + "\n")
        # Trim if file grows too large (keep last N lines)
        _maybe_trim_notifications(path)
        return True
    except Exception:
        logger.debug("Failed to persist notification", exc_info=True)
        return False


def _rewrite_notifications(notifications: list[dict[str, str]]) -> None:
    """Rewrite the entire notifications file from the in-memory list."""
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(n) + "\n" for n in notifications[-_MAX_PERSISTED_NOTIFICATIONS:]]
        path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        logger.debug("Failed to rewrite notifications file", exc_info=True)


def _maybe_trim_notifications(path: Path) -> None:
    """Trim the notifications file if it exceeds 2x the max.

    Expired passive rows are discarded BEFORE the recency cap — the same
    displacement hazard as the load path: trimming the
    raw tail first would retain newer expired-passive rows while deleting
    older LIVE rows, permanently losing history after the next load-time
    sweep. Unparseable lines are kept (never destroy on ambiguity).
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) <= _MAX_PERSISTED_NOTIFICATIONS * 2:
            return
        keep: list[str] = []
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                keep.append(line)
                continue
            if isinstance(row, dict) and sweep_expired_notifications([row]) == 1:
                continue  # expired passive row -- drop before the cap
            keep.append(line)
        kept = keep[-_MAX_PERSISTED_NOTIFICATIONS:]
        path.write_text("".join(kept), encoding="utf-8")
    except Exception:
        pass


def _fmt_duration(secs: int) -> str:
    """Format seconds as human-readable duration."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"


def _governance_status() -> str:
    """Governance health for the status snapshot (never raises)."""
    try:
        from kiro_crew.platform.governance_health import governance_status

        return governance_status()
    except Exception:
        return "unknown"


def _cached_check_status(url: str) -> dict | None:
    """Lazy wrapper so state.py has no import-time dep on the handler module."""
    from kiro_crew.dashboard.handlers.source_providers import get_cached_check_status

    return get_cached_check_status(url)
