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

# Whole-plan watchdog. ``stage_timeout_seconds`` bounds one stage; a plan with
# many stages multiplies it, so a 10-stage plan at the 30-minute default can run
# for hours unattended. This is the ceiling for the WHOLE run, checked at each
# stage boundary, with a single warning once the run passes
# ``PLAN_WARN_FRACTION`` of it so the user can intervene before the cut.
PLAN_WARN_FRACTION = 0.75

# Fallback per-stage budget for a tracker built without one. The authoritative
# value is ``orchestrator.stage_timeout_seconds``; this is only what the tracker
# runs on between construction and the config load.
DEFAULT_STAGE_TIMEOUT = 1800


def _human_secs(seconds: int) -> str:
    """Render a second count as ``30m`` / ``1m30s`` / ``45s``."""
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h{m}m" if m else f"{h}h"
    if seconds >= 60:
        m, rem = divmod(seconds, 60)
        return f"{m}m{rem}s" if rem else f"{m}m"
    return f"{seconds}s"


class OrchestrationTracker:
    """Track failures and rounds per orchestrated session.

    Enforces hard limits that the LLM prompt cannot override.
    """

    def __init__(self, stage_timeout_seconds: int | None = None) -> None:
        # ``None`` means "not provided -- load it from config", which is a
        # different state from an explicit value that happens to equal the
        # default. The stage loop reads ``budgets_unset`` to decide whether it
        # owes this tracker a config load, so a tracker built without budgets
        # (the Slack gateway creates one lazily when a subagent result lands) gets
        # one, while a caller that passed a budget keeps exactly what it passed.
        self._budgets_unset: bool = stage_timeout_seconds is None
        self._task_failures: dict[str, int] = {}  # task_key → failure count
        self._stage_rounds: dict[int, int] = {}  # stage_num → round count
        self._stage_escalations: dict[int, int] = {}  # stage_num → escalation count
        self._stage_results: dict[int, str] = {}  # stage_num → result file path
        self.stopped: bool = False
        self._stage_timeout: int = (
            DEFAULT_STAGE_TIMEOUT if stage_timeout_seconds is None else stage_timeout_seconds
        )
        self._stage_start: float = 0.0  # set when stage begins
        # Whole-plan watchdog. Monotonic deliberately: it measures how long THIS
        # process has been running the plan, which is all it ever has to measure --
        # a plan does not outlive its process (nothing persists the plan shape, so
        # a restart ends the plan; see the module spec's Limitations).
        self._plan_timeout: int = 0  # 0 = disabled
        self._plan_start: float = 0.0  # set when the plan's first stage is entered
        self._plan_warned: bool = False  # the 75% notice fires once per plan

    def stop(self) -> None:
        """User requested stop after escalation."""
        self.stopped = True

    @property
    def has_escalated(self) -> bool:
        """True if any task hit failure limit or any stage hit round limit."""
        return any(v >= MAX_TASK_FAILURES for v in self._task_failures.values()) or any(
            v >= MAX_STAGE_ROUNDS for v in self._stage_rounds.values()
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
        # Only ever STARTS the stage clock, never restarts it: the clock belongs
        # to the stage, and `start_stage` owns the reset. The old `rounds == 1`
        # arm was that reset, from when entering a stage and recording a round
        # were the same call; it is unreachable now, because the only caller that
        # introduces a new stage key is the loop, which enters through
        # `start_stage`. The Slack handler records against `current_stage`, an
        # existing key. Left as a floor for a tracker whose first round arrives
        # with no stage entry at all (the lazily created Slack-only tracker) and
        # after `reset_after_guidance` zeroes it.
        if not self._stage_start:
            self._stage_start = time.monotonic()
        # The whole-plan clock starts with the plan's first round and is never
        # restarted by a later one: it is the budget for the RUN, not for a
        # stage, so re-arming it here would make every stage boundary refresh
        # the ceiling the watchdog is supposed to enforce.
        if not self._plan_start:
            self._plan_start = time.monotonic()
        return self._stage_rounds[stage] >= MAX_STAGE_ROUNDS

    def start_stage(self, stage: int) -> None:
        """Mark *stage* as the one now executing, without spending a round.

        A round is one SPAWN WAVE -- the unit the subagent-completion handler
        records, and the unit ``MAX_STAGE_ROUNDS`` budgets, because that is what
        the orchestrator prompt promises the model ("max 3 rounds per stage").
        Entering a stage is not a wave.

        The stage loop used to enter through :meth:`record_round`, called for its
        side effects rather than its count -- harmless while nothing on that path
        consulted the cap. Once the cap IS consulted there (issue #1783), that
        tick spent a third of the stage's budget before any subagent had run: a
        dashboard stage was cut after two waves, while the same stage driven from
        the Slack handler -- which has no stage loop and so no entry tick -- got
        three. Stricter than the prompt promises AND inconsistent between paths.

        So the side effects live here and the counting stays in
        :meth:`record_round`. Registering the stage at zero rounds is what makes
        :attr:`current_stage` (the highest key) point at it, which is what the
        Slack handler then records against and what the loop resumes from.
        """
        self._stage_rounds.setdefault(stage, 0)
        # Unconditional: this is the per-STAGE clock, so entering stage 2 must not
        # inherit stage 1's elapsed time.
        self._stage_start = time.monotonic()
        # The whole-plan clock starts with the plan's first stage and is never
        # restarted: it is the budget for the RUN, so re-arming it here would let
        # every stage boundary refresh the ceiling the watchdog enforces.
        if not self._plan_start:
            self._plan_start = time.monotonic()

    def round_limit_reached(self, stage: int) -> bool:
        """True when *stage* has spent its whole round budget.

        The same reading ``record_round`` returns, available without recording
        another round -- the stage loop needs it again after a stage's subagent
        wave finishes, because the rounds those waves consume are recorded by the
        subagent-completion handler on this same tracker, not by the loop.
        """
        return self._stage_rounds.get(stage, 0) >= MAX_STAGE_ROUNDS

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

    @stage_timeout_seconds.setter
    def stage_timeout_seconds(self, seconds: int) -> None:
        """Set the per-stage budget after construction.

        The orchestrator builds its tracker before it knows the configured
        value: reading the config blocks, so it is loaded on a worker, and a
        plan cancel arriving during that load needs a tracker already published
        to stop. The tracker therefore starts on the default above and is
        adjusted here once the load returns.

        Safe at that point, and only at that point: the budget is consulted by
        ``is_stage_timed_out`` and by callers deriving a wait from it, all of
        which run per stage, and ``_stage_start`` is still 0 until the first
        round is recorded. Changing it mid-stage would move a deadline the
        current stage is already being measured against, so callers must not.
        """
        self._stage_timeout = seconds

    @property
    def timeout_human(self) -> str:
        """Human-friendly timeout string, e.g. '30m' or '1m30s'."""
        return _human_secs(self._stage_timeout)

    # ── Whole-plan watchdog ──

    @property
    def budgets_unset(self) -> bool:
        """True while this tracker has never had its configured budgets applied.

        The stage loop used to gate its config load on "did I just create this
        tracker", which meant a tracker it did NOT create -- the one the Slack
        gateway creates lazily when a subagent result lands on a slot the loop has
        not reached -- ran the whole plan on constructor defaults: the plan
        watchdog disabled at 0 and the stage budget at
        :data:`DEFAULT_STAGE_TIMEOUT` regardless of config. Asking the tracker
        instead makes the answer independent of how the loop obtained it.
        """
        return self._budgets_unset

    def mark_budgets_loaded(self) -> None:
        """Record that a config load has been attempted for this tracker.

        Called even when the load RAISED: the fallback is the documented
        behaviour of a failed load, and re-attempting it at every later Go would
        turn one bad config read into one per stage gate.
        """
        self._budgets_unset = False

    @property
    def max_plan_duration_seconds(self) -> int:
        """Configured ceiling for the whole plan in seconds (0 = disabled)."""
        return self._plan_timeout

    @max_plan_duration_seconds.setter
    def max_plan_duration_seconds(self, seconds: int) -> None:
        """Set the whole-plan budget after construction.

        Same seam, and same reason, as ``stage_timeout_seconds``: the tracker is
        published before the orchestrator config load returns, so the configured
        value is applied here once it is known. Safe only before the first round
        is recorded -- ``_plan_start`` is 0 until then, so no deadline the run is
        already being measured against can move.
        """
        self._plan_timeout = seconds

    @property
    def plan_elapsed_seconds(self) -> int:
        """Seconds since the plan's first round, or 0 before it started."""
        if not self._plan_start:
            return 0
        return int(time.monotonic() - self._plan_start)

    def is_plan_timed_out(self) -> bool:
        """True once the whole plan has outrun ``max_plan_duration_seconds``."""
        if not self._plan_start or not self._plan_timeout:
            return False
        return (time.monotonic() - self._plan_start) > self._plan_timeout

    def plan_warning_due(self) -> bool:
        """True exactly once, at the first check past ``PLAN_WARN_FRACTION``.

        Latches on read: the caller emits the notice, and a plan whose remaining
        stages each re-check must not re-announce it at every boundary.
        """
        if self._plan_warned or not self._plan_start or not self._plan_timeout:
            return False
        if (time.monotonic() - self._plan_start) < self._plan_timeout * PLAN_WARN_FRACTION:
            return False
        self._plan_warned = True
        return True

    @property
    def plan_timeout_human(self) -> str:
        """Human-friendly whole-plan budget, e.g. '2h'."""
        return _human_secs(self._plan_timeout)

    @property
    def plan_elapsed_human(self) -> str:
        """Human-friendly elapsed plan time, e.g. '1h30m'."""
        return _human_secs(self.plan_elapsed_seconds)

    # ── Stage ledger reads ──

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
    r"(?:^|\n)\s*(?:Phase|Step|Stage|Part)\s+\d+\s*[:\-—]" r"|(?:^|\n)\s*\d+\.\s+\*\*[A-Z]",
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


async def rephrase_plan(
    text: str, issues: list[str], client: Any, *, might_not_be_plan: bool = False
) -> str | None:
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
