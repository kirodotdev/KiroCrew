"""Context management for sub-agent results and session workspaces.

Enforces size limits on disk files, memory buffers, and session history
to prevent unbounded growth during multi-agent runs.

All limits are centralized here so they can be tuned in one place.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

# ── Limits ──────────────────────────────────────────────────────────

# Per sub-agent result file: truncate after this many bytes.
RESULT_FILE_MAX_BYTES = 512_000  # 500 KB

# In-memory streaming_text buffer per sub-agent (for Activity Viewer).
STREAMING_TEXT_MAX_CHARS = 50_000  # ~50 KB

# Words to include in the completion notification summary.
# The LLM uses this to decide whether to read the full file.
# 50 words is enough for simple status; 200 words gives enough for planning.
RESULT_SUMMARY_WORDS = 200

# Default character cap for the completion event injected into the parent
# session. The full transcript stays in result.txt (capped by
# RESULT_FILE_MAX_BYTES above) for a retention window after delivery.
# Override per-installation via ``agent.completion_keep_chars`` in
# ``~/.kiro/crew/config.json``. Pair with ``agent.completion_keep`` to choose
# whether the head, tail, or both ends of the transcript are kept (see
# ``apply_completion_keep`` below).
COMPLETION_KEEP_DEFAULT_CHARS = 3000

# Session workspace: max total bytes across all result files.
SESSION_MAX_BYTES = 5_000_000  # 5 MB

# History JSONL: max entries kept.
HISTORY_MAX_ENTRIES = 500

# Session workspace: max age before cleanup (seconds).
SESSION_MAX_AGE_SECS = 86400 * 7  # 7 days

# Max completed sub-agents retained in SubagentManager._agents dict.
MAX_RETAINED_AGENTS = 50


def cap_result_file(path: Path) -> bool:
    """Truncate a result file if it exceeds RESULT_FILE_MAX_BYTES.

    Keeps the first 20% and last 80% of the budget to preserve
    the beginning (task context) and end (final output).
    Returns True if truncation occurred.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= RESULT_FILE_MAX_BYTES:
        return False

    head_budget = RESULT_FILE_MAX_BYTES // 5  # 20%
    tail_budget = RESULT_FILE_MAX_BYTES - head_budget - 100  # 80% minus marker

    content = path.read_text(encoding="utf-8", errors="replace")
    head = content[:head_budget]
    tail = content[-tail_budget:]
    marker = f"\n\n[...truncated {size - RESULT_FILE_MAX_BYTES:,} bytes...]\n\n"

    path.write_text(head + marker + tail, encoding="utf-8")
    logger.info("Truncated %s from %d to %d bytes", path.name, size, RESULT_FILE_MAX_BYTES)
    return True


def cap_streaming_text(text: str) -> str:
    """Truncate in-memory streaming_text if it exceeds the limit.

    Keeps the last STREAMING_TEXT_MAX_CHARS characters (most recent output).
    """
    if len(text) <= STREAMING_TEXT_MAX_CHARS:
        return text
    return "…(truncated)\n" + text[-STREAMING_TEXT_MAX_CHARS + 20 :]


# Marker inserted between head and tail when completion_keep="both".
_COMPLETION_BOTH_MARKER = "\n\n[...middle elided...]\n\n"


def apply_completion_keep(text: str, mode: str, max_chars: int) -> str:
    """Truncate completion-event text per ``mode`` and ``max_chars``.

    Three modes: ``head`` (first ``max_chars`` characters), ``tail`` (last
    ``max_chars``), ``both`` (head + middle marker + tail). ``max_chars``
    of ``0`` or less disables truncation.

    ``mode`` is validated at config load by ``_validated_completion_keep``
    in ``config/loader.py``; callers may rely on receiving one of
    ``head``/``tail``/``both``.

    The transcript stays in ``~/.kiro/crew/subagents/<id>/result.txt``, trimmed towards
    ``RESULT_FILE_MAX_BYTES``, for at least ``agent.subagent_result_ttl_secs`` (default
    3600) after the completion event reaches the parent: delivery writes a
    ``cause="delivered"`` tombstone instead of deleting the folder, and the reaper prunes
    a tombstoned folder once that window closes. Read it there with ``spawn_status``.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if mode == "tail":
        return text[-max_chars:]
    if mode == "both":
        marker_len = len(_COMPLETION_BOTH_MARKER)
        if max_chars <= marker_len + 2:
            return text[:max_chars]
        head_budget = (max_chars - marker_len) // 2
        tail_budget = max_chars - marker_len - head_budget
        return text[:head_budget] + _COMPLETION_BOTH_MARKER + text[-tail_budget:]
    return text[:max_chars]


def summarize_result(result: str, result_path: str, words: int = RESULT_SUMMARY_WORDS) -> str:
    """Build a completion-event body that points at the full transcript on disk.

    Emits a first+last ``words`` preview of *result* plus the ``result_path`` to
    the full (up to ``RESULT_FILE_MAX_BYTES``) transcript, and instructs the
    parent to read it on demand (``read`` with offset/limit, ``grep``, or the
    ``spawn_status`` MCP tool) instead of re-running the subagent.

    Used when the completion-event copy was truncated (``head``/``tail``/``both``
    dropped content) or for orchestrator-mode delivery, so the deliverable at the
    end of a long transcript is never silently lost. The preview reflects whatever
    end ``apply_completion_keep`` retained; the file is the source of truth.
    """
    tokens = (result or "").split()
    half = max(1, words // 2)
    if len(tokens) <= words:
        preview = " ".join(tokens)
    else:
        preview = (
            " ".join(tokens[:half])
            + "\n[...middle truncated — read the full transcript below...]\n"
            + " ".join(tokens[-half:])
        )
    size = ""
    try:
        size = f" ({os.path.getsize(result_path):,} bytes)"
    except OSError:
        pass
    return (
        f"Full transcript: {result_path}{size}\n"
        f"Preview (first+last {half} words):\n{preview}\n\n"
        f"The full result is on disk — read it on demand with the read tool "
        f"(offset/limit), grep the path above, or call "
        f"spawn_status(agent_id, offset=, limit=, grep=). Do NOT re-run the subagent."
    )


def cap_history(entries: list[dict]) -> list[dict]:
    """Keep only the last HISTORY_MAX_ENTRIES from a history list."""
    if len(entries) <= HISTORY_MAX_ENTRIES:
        return entries
    return entries[-HISTORY_MAX_ENTRIES:]


def check_session_budget(session_dir: Path) -> bool:
    """Check if a session workspace exceeds its total size budget.

    Returns True if over budget. Caller should stop writing new results.
    """
    total = sum(f.stat().st_size for f in session_dir.glob("agent-*.md") if f.is_file())
    return total > SESSION_MAX_BYTES


def evict_completed_agents(agents: dict, max_retained: int = MAX_RETAINED_AGENTS) -> int:
    """Remove oldest completed sub-agents from the agents dict.

    Returns number of evicted entries.
    """
    completed = [(k, v) for k, v in agents.items() if v.done]
    if len(completed) <= max_retained:
        return 0
    completed.sort(key=lambda x: x[1].started)
    to_evict = len(completed) - max_retained
    for k, _ in completed[:to_evict]:
        del agents[k]
    logger.info("Evicted %d completed sub-agents (kept %d)", to_evict, max_retained)
    return to_evict


def cleanup_stale_sessions() -> int:
    """Remove session workspace directories older than SESSION_MAX_AGE_SECS.

    Returns number of cleaned up sessions.
    """
    sessions_dir = config_dir() / "sessions"
    if not sessions_dir.exists():
        return 0
    now = time.time()
    cleaned = 0
    for d in sessions_dir.iterdir():
        if not d.is_dir():
            continue
        try:
            files = list(d.iterdir())
            mtime = max((f.stat().st_mtime for f in files), default=d.stat().st_mtime)
            if now - mtime > SESSION_MAX_AGE_SECS:
                shutil.rmtree(d, ignore_errors=True)
                cleaned += 1
        except OSError:
            continue
    if cleaned:
        logger.info("Cleaned up %d stale session workspaces", cleaned)
    return cleaned
