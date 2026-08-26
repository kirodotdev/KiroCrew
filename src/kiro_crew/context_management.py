"""Context management for sub-agent results and session workspaces.

Enforces size limits on disk files, memory buffers, and session history
to prevent unbounded growth during multi-agent orchestration.

All limits are centralized here so they can be tuned in one place.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

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
# RESULT_FILE_MAX_BYTES above) until cleanup removes it after delivery.
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

# ── Orchestration guards ────────────────────────────────────────────

# Max consecutive failures on the same sub-task before forcing user escalation.
MAX_TASK_FAILURES = 3
MAX_STAGE_ROUNDS = 3
MAX_STAGE_ESCALATIONS = 2  # after 2 escalations (= 9 rounds), force-fail


class OrchestrationTracker:
    """Track failures and rounds per orchestrated session.

    Enforces hard limits that the LLM prompt cannot override.
    """

    def __init__(self, stage_timeout_seconds: int = 1800) -> None:
        self._task_failures: dict[str, int] = {}  # task_key → failure count
        self._stage_rounds: dict[int, int] = {}  # stage_num → round count
        self._stage_escalations: dict[int, int] = {}  # stage_num → escalation count
        self._stage_results: dict[int, str] = {}  # stage_num → result file path
        self.stopped: bool = False
        self._stage_timeout: int = stage_timeout_seconds
        self._stage_start: float = 0.0  # set when stage begins

    def stop(self) -> None:
        """User requested stop after escalation."""
        self.stopped = True

    @property
    def has_escalated(self) -> bool:
        """True if any task hit failure limit or any stage hit round limit."""
        return (
            any(v >= MAX_TASK_FAILURES for v in self._task_failures.values())
            or any(v >= MAX_STAGE_ROUNDS for v in self._stage_rounds.values())
        )

    def reset_after_guidance(self) -> None:
        """Reset round counters after user provides guidance. Increments escalation count."""
        for stage, rounds in self._stage_rounds.items():
            if rounds >= MAX_STAGE_ROUNDS:
                self._stage_escalations[stage] = self._stage_escalations.get(stage, 0) + 1
                self._stage_rounds[stage] = 0
        # Also reset task failures so user guidance gets a fresh start
        self._task_failures.clear()
        self._stage_start = 0.0  # reset timeout clock for next stage

    def is_force_failed(self, stage: int) -> bool:
        """True if stage has exhausted all escalations (2 escalations = 9 rounds)."""
        return self._stage_escalations.get(stage, 0) >= MAX_STAGE_ESCALATIONS

    def record_failure(self, task_key: str) -> bool:
        """Record a failure. Returns True if limit reached (must escalate)."""
        self._task_failures[task_key] = self._task_failures.get(task_key, 0) + 1
        return self._task_failures[task_key] >= MAX_TASK_FAILURES

    def record_success(self, task_key: str) -> None:
        """Reset failure count for a task."""
        self._task_failures.pop(task_key, None)

    def failure_count(self, task_key: str) -> int:
        return self._task_failures.get(task_key, 0)

    def record_round(self, stage: int) -> bool:
        """Record a spawn round for a stage. Returns True if limit reached."""
        self._stage_rounds[stage] = self._stage_rounds.get(stage, 0) + 1
        if self._stage_rounds[stage] == 1 or not self._stage_start:
            self._stage_start = time.monotonic()
        return self._stage_rounds[stage] >= MAX_STAGE_ROUNDS

    def is_stage_timed_out(self) -> bool:
        """True if current stage has exceeded the timeout."""
        if not self._stage_start or not self._stage_timeout:
            return False
        return (time.monotonic() - self._stage_start) > self._stage_timeout

    @property
    def stage_timeout_seconds(self) -> int:
        """Configured per-stage timeout in seconds (0 = disabled).

        Public accessor for callers that need the raw budget -- e.g. the
        orchestrator's ``asyncio.wait_for`` around a stage turn and its
        subagent-wait poll cap, both of which derive from this value.
        """
        return self._stage_timeout

    @property
    def timeout_human(self) -> str:
        """Human-friendly timeout string, e.g. '30m' or '1m30s'."""
        s = self._stage_timeout
        if s >= 60:
            m, rem = divmod(s, 60)
            return f"{m}m{rem}s" if rem else f"{m}m"
        return f"{s}s"

    def round_count(self, stage: int) -> int:
        return self._stage_rounds.get(stage, 0)

    @property
    def current_stage(self) -> int:
        return max(self._stage_rounds.keys(), default=1)

    # ── Python-controlled stage loop helpers ──

    def record_stage_result(self, stage_num: int, result_path: str) -> None:
        """Record that *stage_num* (1-based) completed with result at *result_path*."""
        self._stage_results[stage_num] = result_path

    def status_summary(self, current: int, total: int, titles: list[str]) -> str:
        """Build a compact plan status block.

        *current* is 0-based index of the stage about to execute.
        """
        lines: list[str] = []
        for i in range(total):
            t = titles[i] if i < len(titles) else ""
            label = f"Stage {i + 1}: {t}" if t else f"Stage {i + 1}"
            if i < current:
                lines.append(f"  ✅ {label} — completed")
            elif i == current:
                lines.append(f"  ▶️ {label} — execute now")
            else:
                lines.append(f"  ⬜ {label} — pending")
        return "\n".join(lines)


# ── Plan format validation ──────────────────────────────────────────

_PLAN_HEADER_RE = re.compile(r"📋\s*Plan for:", re.IGNORECASE)
_STAGE_RE = re.compile(r"^Stage\s+(\d+)\s*:", re.MULTILINE | re.IGNORECASE)
_STAGE_TITLE_RE = re.compile(r"^Stage\s+(\d+)\s*:\s*(.*)", re.MULTILINE | re.IGNORECASE)
_PLAN_GOAL_RE = re.compile(r"📋\s*Plan for:\s*\"?(.+?)\"?\s*$", re.MULTILINE | re.IGNORECASE)
_OPTION_RE = re.compile(r"\[OPTION:\s*Go\s*\|.*Cancel\s*\]")


def extract_plan_metadata(text: str) -> tuple[list[str], str, list[list[str]]]:
    """Extract stage titles, goal, and descriptions from plan text.

    Returns (titles, goal, descriptions) where titles[i] is Stage i+1's title
    and descriptions[i] is a list of bullet-point tasks for that stage.
    """
    pairs = _STAGE_TITLE_RE.findall(text)
    max_stage = max((int(n) for n, _ in pairs), default=0)
    titles = [""] * max_stage
    for num_str, title in pairs:
        idx = int(num_str) - 1
        if 0 <= idx < max_stage:
            titles[idx] = title.strip()
    goal_m = _PLAN_GOAL_RE.search(text)
    goal = goal_m.group(1).strip() if goal_m else ""
    # Extract bullet points under each stage heading
    descriptions: list[list[str]] = [[] for _ in range(max_stage)]
    lines = text.splitlines()
    current_stage = -1
    for line in lines:
        m = _STAGE_TITLE_RE.match(line)
        if m:
            current_stage = int(m.group(1)) - 1
            continue
        stripped = line.strip()
        if current_stage >= 0 and current_stage < max_stage and stripped.startswith("- "):
            descriptions[current_stage].append(stripped)
        elif stripped and not stripped.startswith("-") and current_stage >= 0:
            # Non-bullet, non-empty line ends bullet collection for this stage
            current_stage = -1
    return titles, goal, descriptions


PLAN_TEMPLATE = """\
📋 Plan for: "<task description>"

Stage 1: <Title>
  - <task>
  - <task>

Stage 2: <Title>
  - <task>

Stage N: Verification
  - <verification task>

[OPTION: Go | Go All | Cancel]"""


# Loose pre-filter: catches plan-like text cheaply. False positives are
# handled by rephrase_plan(might_not_be_plan=True) which asks the LLM.
_PLAN_LIKE_RE = re.compile(
    r"(?:^|\n)\s*(?:Phase|Step|Stage|Part)\s+\d+\s*[:\-—]"
    r"|(?:^|\n)\s*\d+\.\s+\*\*[A-Z]",
    re.IGNORECASE,
)


def looks_like_plan(text: str) -> bool:
    """Cheap heuristic: does the text look like it might be a plan?

    Intentionally loose — false positives are caught downstream by the
    LLM-based rephrase which can reject non-plans.
    """
    return len(_PLAN_LIKE_RE.findall(text)) >= 2


_GO_ALL_RE = re.compile(r"\[OPTION:\s*Go\s*\|\s*Cancel\s*\]")


def ensure_go_all_option(text: str) -> str:
    """Patch [OPTION: Go | Cancel] → [OPTION: Go | Go All | Cancel]."""
    return _GO_ALL_RE.sub("[OPTION: Go | Go All | Cancel]", text)


def validate_plan_format(text: str) -> tuple[bool, bool, list[str]]:
    """Check if text contains a plan and whether it follows the expected format.

    Returns (has_plan, valid, issues).
    """
    if not _PLAN_HEADER_RE.search(text):
        return False, False, []
    issues: list[str] = []
    stages = _STAGE_RE.findall(text)
    if not stages:
        issues.append("No 'Stage N:' lines found")
    else:
        nums = [int(s) for s in stages]
        if nums != list(range(1, len(nums) + 1)):
            issues.append(f"Stages not sequential: {nums}")
    if not _OPTION_RE.search(text):
        issues.append("Missing [OPTION: Go | Go All | Cancel] footer")
    return True, len(issues) == 0, issues


async def rephrase_plan(text: str, issues: list[str], client: Any, *, might_not_be_plan: bool = False) -> str | None:
    """Ask the LLM to reformat a malformed plan. Returns fixed text or None.

    When *might_not_be_plan* is True, the LLM is instructed to return the
    input unchanged (prefixed with ``NOT_A_PLAN:``) if it is not an
    execution plan.
    """
    from kiro_crew.llm_helpers import stream_and_collect

    if might_not_be_plan:
        prompt = (
            "First, decide: is the following text an execution plan with "
            "actionable steps the user wants to carry out?\n"
            "- If NO (e.g. it is an analysis, summary, explanation, or general "
            "response), return ONLY the string 'NOT_A_PLAN'\n"
            "- If YES, reformat it to match this template:\n\n"
            f"{PLAN_TEMPLATE}\n\n"
            f"Issues to fix: {', '.join(issues)}\n"
            "Keep all original stage content. Number stages from 1. "
            "End with [OPTION: Go | Go All | Cancel]. Return ONLY the result.\n\n"
            f"Text:\n{text}"
        )
    else:
        prompt = (
            "Reformat the following plan to match this exact template:\n\n"
            f"{PLAN_TEMPLATE}\n\n"
            f"Issues to fix: {', '.join(issues)}\n\n"
            "Rules:\n"
            "- Keep all original stage content and tasks\n"
            "- Number stages sequentially starting from 1\n"
            "- End with [OPTION: Go | Go All | Cancel]\n"
            "- Return ONLY the reformatted plan, nothing else\n\n"
            f"Plan to reformat:\n{text}"
        )
    try:
        result = await stream_and_collect(client, prompt)
        if not result:
            return None
        if might_not_be_plan and result.strip().startswith("NOT_A_PLAN"):
            return None
        return result
    except Exception:
        logger.warning("Plan rephrase failed", exc_info=True)
        return None


def strip_plan_markers(text: str) -> str:
    """Remove plan structure markers, leaving content as plain text."""
    text = _PLAN_HEADER_RE.sub("", text)
    text = _STAGE_RE.sub("", text)
    text = _OPTION_RE.sub("", text)
    return text.strip()


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

    The full untruncated transcript stays in
    ``~/.kiro/crew/subagents/<id>/result.txt`` until the completion event is
    delivered to the parent session, after which it is cleaned up by
    ``subagent.py`` (see ``delete_agent_folder``). Use the ``spawn_status``
    MCP tool to read it before delivery completes.
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
