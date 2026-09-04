"""History consolidation and auto-skill extraction.

The transcript facade owns persistence and locking.  This module owns the
asynchronous consolidation workflow and resolves the few facade-level seams
that tests and embedding applications intentionally replace at runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time as _time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.frontmatter import SKILL_UPDATE, frontmatter_value
from kiro_crew.llm_helpers import (
    ToolApprovalPolicy,
    background_turn,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS, AutoSkillProvenance
from kiro_crew.skills_dedupe import (
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
)
from kiro_crew.skills_script_validator import validate_skill_script
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.session import SessionManager
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.vector_memory import VectorMemoryStore


_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")

_CONSOLIDATION_THRESHOLD = 30
_CONSOLIDATION_MAX_ATTEMPTS = 5
_CONSOLIDATION_BACKOFF_BASE_SECS = 900.0
_CONSOLIDATION_BACKOFF_MAX_SECS = 86400.0
_SKILL_DETECTION_WINDOW = 200

_CONSOLIDATION_META_KEYS: frozenset[str] = frozenset(
    {
        "consolidation_attempts",
        "consolidation_retry_at",
        "consolidation_env_failures",
        "consolidation_attempts_generation",
        "consolidation_attempts_offset",
        "consolidation_attempts_count",
    }
)


class _ConsolidationRefusedSentinel:
    """A retry gate refused a span without running a consolidation pass."""

    __slots__ = ()


_CONSOLIDATION_REFUSED = _ConsolidationRefusedSentinel()


class AttemptedSpan(NamedTuple):
    """Identity of the transcript span a billed consolidation turn covered."""

    total: int
    generation: int
    offset: int


class _ConsolidationNotDispatched(Exception):
    """A consolidation prompt never reached the provider."""


def _fmt_message(message: dict) -> str:
    """Render one transcript message for a consolidation prompt."""
    tools = f" [tools: {', '.join(message['tools'])}]" if message.get("tools") else ""
    return (
        f"[{message.get('ts', '?')[:16]}] {message['role'].upper()}"
        f"{tools}: {message['content']}"
    )


_PLACEHOLDER_BODIES = frozenset(
    {
        "unchanged",
        "no change",
        "no changes",
        "no change needed",
        "no changes needed",
        "no changes required",
        "no update",
        "no updates",
        "no update needed",
        "no updates needed",
        "nothing changed",
        "nothing to update",
        "nothing to change",
        "none",
        "n/a",
        "na",
        "empty",
        "same",
        "same as before",
        "as before",
        "see above",
        "content unchanged",
        "file unchanged",
    }
)


def _is_plausible_memory_file(content: str, header: str) -> bool:
    """Refuse placeholder text before it overwrites a complete memory file."""
    first_line, _, body = content.strip().partition("\n")
    if first_line.strip() != header:
        return False
    normalized = body.strip().lower().strip(" \t\"'`*_~.,!()[]")
    return normalized not in _PLACEHOLDER_BODIES


def _facade_sel() -> Any:
    from kiro_crew import history as history_facade

    return history_facade.sel()


def _facade_stream_and_collect(*args: Any, **kwargs: Any) -> Awaitable[str | None]:
    from kiro_crew import history as history_facade

    return history_facade.stream_and_collect(*args, **kwargs)


def _facade_stream_and_collect_json(*args: Any, **kwargs: Any) -> Awaitable[dict | None]:
    from kiro_crew import history as history_facade

    return history_facade.stream_and_collect_json(*args, **kwargs)


def _facade_metadata_dedupe_verdict(
    candidate: dict,
    existing: list[dict],
    judge: Callable[[str], str],
) -> tuple[str, str | None]:
    from kiro_crew import history as history_facade

    return history_facade.metadata_dedupe_verdict(candidate, existing, judge)


# ── Module-level helpers for auto skill eligibility ──
#
# Kept at module level so they're trivially unit-testable without
# instantiating HistoryConsolidator.

# Canonical tool titles that indicate a read targeting a sensitive path.
# Supplements is_sensitive_path() and is_sensitive_bash_command() which
# handle the actual runtime blocking — this is a second-layer defense
# that refuses to extract a skill if the session tried to access a
# sensitive path, even when the attempt was denied at hook time.
_SENSITIVE_TOOL_PATTERNS: tuple[str, ...] = (
    ".aws/",
    ".ssh/",
    ".gnupg/",
    ".gpg/",
    ".docker/config",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # Kiro Crew's own credential file. The data home moved to ~/.kiro/crew, so the
    # LIVE secret is ~/.kiro/crew/.env; cover the pre-move legacy home too
    # (substring match, so bare "/.env"-suffixed forms).
    ".kiro/crew/.env",
    ".kirocrew/.env",
    "169.254.169.254",  # IMDS
)


_TOOL_ROLES: frozenset[str] = frozenset({"tool", "tool_call", "tool_result"})


def _frontmatter_value(text: str | None, key: str) -> str:
    """Return *key*'s frontmatter value from a SKILL.md body, or "".

    Values resolve the way ``SkillsLoader._parse_frontmatter`` resolves them:
    only a column-0 key is a field, and a bare block-scalar indicator
    (``>``/``|``, optionally chomped) folds the indented lines that follow.
    The auto-skill update path carries the live skill's ``description`` and
    ``triggers`` through this reader into a staged candidate that overwrites
    the live skill on approval — reading the indicator verbatim would collapse
    a block-scalar description to ``""`` and inject a bogus ``>`` trigger on
    that round-trip. The grammar (plus the leading-whitespace opener
    tolerance, verbatim plain values, and first-duplicate-wins lookup) is
    pinned as ``frontmatter.SKILL_UPDATE``.
    """
    if not text:
        return ""
    return frontmatter_value(text, key, SKILL_UPDATE)


def _merge_trigger_lists(live: str, candidate: str, *, cap: int = 12) -> str:
    """Union two comma-separated trigger lists, live first, case-insensitively
    deduped and capped.

    Triggers are the skill's ACTIVATION surface. An update proposes triggers for
    the new requirement only, so replacing the live list would stop the skill
    firing on every phrasing it already answered — a silent regression the diff
    shows but nobody reads as a behavior change. Union instead, and cap so
    repeated updates cannot grow the list without bound.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for raw in (live or "").split(",") + (candidate or "").split(","):
        t = re.sub(r"\s+", " ", raw).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        merged.append(t)
        if len(merged) >= cap:
            break
    return ", ".join(merged)


def _strip_skill_frontmatter(text: str | None) -> str:
    """Return *text* with a leading ``---`` frontmatter block removed.

    A skill body read off disk carries its frontmatter header; only the prose
    below it may be fed to (or accepted from) the update-merge turn, because
    ``stage_skill_candidate`` re-emits frontmatter of its own. Text without a
    leading block is returned unchanged (stripped). A fence LOCATOR, not a
    field parser — deliberately outside ``kiro_crew.frontmatter``; editing
    its grammar means revisiting ``_frontmatter_value``'s dialect too. Like
    that dialect's fence, an optional carriage return before each fence
    newline is tolerated, so the locator strips exactly the block the field
    parser reads.
    """
    if not text:
        return ""
    m = re.match(r"^\s*---\r?\n.*?\r?\n---\r?\n?(.*)$", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _strip_code_fence(text: str) -> str:
    """Unwrap a single outer ```/```markdown fence, if the model emitted one."""
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    lines = s.split("\n")
    if len(lines) < 2:
        return s
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _count_tool_call_messages(messages: list[dict]) -> int:
    """Count messages that represent tool invocations under either schema.

    Two recording formats exist:
    - Legacy (Slack pipeline): assistant messages carry a ``tools`` list field.
    - Dashboard pipeline: separate messages with ``role`` in {"tool", "tool_call",
      "tool_result"} and the tool name embedded in ``content``.

    A message matching EITHER condition counts once (no double-counting).
    """
    count = 0
    for msg in messages:
        tools = msg.get("tools")
        if isinstance(tools, list) and tools:
            count += 1
        elif msg.get("role") in _TOOL_ROLES:
            count += 1
    return count


def _session_touched_sensitive(messages: list[dict]) -> bool:
    """Return True if any tool call in the session referenced a sensitive path.

    Checks both recording schemas:
    - Legacy: substring match over each entry in ``msg["tools"]`` list.
    - Dashboard: substring match over ``content`` when ``role`` indicates a tool event.

    Designed to be conservative — a false positive just means we skip
    auto-creation for this session.
    """
    for msg in messages:
        # Legacy schema: tools list on assistant messages
        tools = msg.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, str):
                    continue
                lower = tool.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
        # Dashboard schema: role="tool" with tool info in content
        if msg.get("role") in _TOOL_ROLES:
            content = msg.get("content", "")
            if isinstance(content, str):
                lower = content.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
    return False


class HistoryConsolidator:
    """Summarize old messages into structured memory via LLM.

    Two consolidation paths:
    - Preferences/projects: triggered by message count (30 messages)
    - Daily history: triggered by idle time (3h default) or end of day
    """

    def __init__(
        self,
        log: ConversationLog,
        memory: MemoryStore,
        sessions: SessionManager | None = None,
        lesson_store: LessonStore | None = None,
        history_idle_secs: float = 3 * 3600,
        vector_store: "VectorMemoryStore | None" = None,
        migrated: bool = False,
        # ── Auto skill creation ──
        # All-default so callers unaware of this feature continue to work.
        skills_loader: "SkillsLoader | None" = None,
        auto_skills_enabled: bool = False,
        auto_refine_enabled: bool = False,
        auto_min_tool_calls: int = 5,
        auto_similarity_threshold: float = 0.85,
        # ── Staged approval + lifecycle (v2) ──
        approval_required: bool = True,
        max_auto_skills: int = 100,
        stale_after_days: int = 30,
        archive_after_days: int = 90,
        generate_scripts: bool = True,
        judge_model: str = "",
    ) -> None:
        self._log = log
        self._memory = memory
        self._sessions = sessions
        self._lesson_store = lesson_store
        self._history_idle_secs = history_idle_secs
        self._vector_store = vector_store
        self._migrated = migrated
        self._skills_loader = skills_loader
        self._auto_skills_enabled = auto_skills_enabled
        self._auto_refine_enabled = auto_refine_enabled
        self._auto_min_tool_calls = auto_min_tool_calls
        self._auto_similarity_threshold = auto_similarity_threshold
        self._approval_required = approval_required
        self._max_auto_skills = max_auto_skills
        self._stale_after_days = stale_after_days
        self._archive_after_days = archive_after_days
        self._generate_scripts = generate_scripts
        self._judge_model = judge_model
        # Captured on the first _consolidate (the gateway loop) so the sync,
        # thread-offloaded _process_auto_skills can bridge the async dedupe
        # judge back onto the loop. Throttle guards the autonomous lifecycle.
        self._event_loop: "asyncio.AbstractEventLoop | None" = None
        self._last_lifecycle: float = 0.0
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # Track last activity per session for idle-based history consolidation
        self._last_activity: dict[str, float] = {}
        self._history_consolidated: dict[str, float] = {}  # key → last history consolidation time
        # Separate offset for prefs-only consolidation (doesn't advance main offset)
        self._prefs_offset: dict[str, int] = {}
        # Session length at the last skill-detection pass, so an unchanged
        # (rotation_generation, message_count) at the last skill-detection
        # pass, so an unchanged session isn't re-judged on every history
        # consolidation — while a rotation (which bumps the generation and
        # swaps the window's content) still forces a fresh pass.
        self._last_skillgen_marker: dict[str, tuple[int, int]] = {}

    @property
    def _logger(self) -> logging.Logger:
        """Keep the pre-extraction ``kiro_crew.history`` logger category."""
        return _HISTORY_LOGGER

    def retry_eligible(
        self, key: str, now: float | None = None, message_count: int | None = None
    ) -> bool:
        """True when *key* may spend a billed consolidation turn right now.

        Every automatic entry point consults this so a span whose consolidation
        keeps failing backs off instead of re-billing an LLM turn on each sweep,
        and _consolidate() itself enforces it as the final gate, so an entry
        point without a pre-check of its own still cannot bypass the backoff.
        A span at :data:`_CONSOLIDATION_MAX_ATTEMPTS` is refused: the abandon path
        normally writes the marker (which also clears the accounting), so reaching
        here at the cap means even that write failed, and refusing keeps a broken
        span from spending forever.

        That refusal covers the SPAN, not the session. The cap is scoped to the
        content it measured, so a rotation or new messages release it with a fresh
        bounded budget (see
        :meth:`ConversationLog._attempts_describe_current_span`) — otherwise one
        transient marker-write failure would stop this session from ever
        consolidating again.

        Costs one metadata-line read and NO transcript read: this runs on the
        gateway event loop (heartbeat sweep, expiry, dashboard trigger), where a
        synchronous full-file read would stall every other gateway task on a large
        transcript. *message_count* is the transcript's current total, which every
        automatic caller already holds from its own
        :meth:`ConversationLog.consolidation_counts` call; omitting it skips the
        extent test and keeps the cap.
        """
        attempts, retry_at = self._log.consolidation_retry_state(key, message_count)
        if attempts >= _CONSOLIDATION_MAX_ATTEMPTS:
            return False
        return (_time.time() if now is None else now) >= retry_at

    async def _note_failed_attempt(self, key: str, span: AttemptedSpan, reason: str) -> None:
        """Charge one attempt for a billed turn that never reached the marker.

        Called only once the prompt has actually reached the provider, so a
        pre-dispatch failure (no session manager, kiro-cli failing to start) and a
        cheap pre-call failure (snapshot, metadata read) both keep their free
        retry. At the attempt cap the durable marker is written anyway and the span
        is abandoned with a warning: the alternative is re-billing this failure
        indefinitely.

        *span* is the pre-turn snapshot identity (see :class:`AttemptedSpan`), used
        both to stamp the charge and to place the abandon marker — the same values
        for both, so the marker cannot be written for a span other than the one the
        cap was reached on.
        """
        try:
            attempts, retry_at = await asyncio.to_thread(
                self._log.record_consolidation_failure,
                key,
                _CONSOLIDATION_BACKOFF_BASE_SECS,
                _CONSOLIDATION_BACKOFF_MAX_SECS,
                span,
            )
        except Exception:
            # Without a persisted count the sweep cannot back off, so say so
            # loudly — but never let bookkeeping mask the original failure.
            self._logger.warning(
                "Could not persist consolidation retry state for %s", key, exc_info=True
            )
            return
        if attempts < 1:
            # The session was deleted mid-consolidation, so nothing was recorded
            # and there is no span left to abandon.
            return
        if attempts < _CONSOLIDATION_MAX_ATTEMPTS:
            self._logger.warning(
                "Consolidation attempt %d/%d failed for %s (%s); " "next attempt in %.0fs",
                attempts,
                _CONSOLIDATION_MAX_ATTEMPTS,
                key,
                reason,
                max(0.0, retry_at - _time.time()),
            )
            return
        self._logger.warning(
            "Abandoning consolidation for %s after %d failed attempts (%s): "
            "marking %d messages consolidated WITHOUT a memory pass, so this "
            "span's history/preferences/lessons are not extracted",
            key,
            attempts,
            reason,
            span.total,
        )
        try:
            await asyncio.to_thread(self._log.mark_consolidated, key, span.total, span.generation)
        except Exception:
            # The count stays at the cap, so retry_eligible() keeps refusing —
            # the span stops spending even though the marker is missing.
            self._logger.warning(
                "Could not mark abandoned consolidation for %s", key, exc_info=True
            )

    async def _note_environment_failure(self, key: str, reason: str) -> None:
        """Arm the backoff for a consolidation that never reached the provider.

        Deliberately does NOT touch the attempt cap. A pre-dispatch failure spends
        nothing, so abandoning the span over one would write the durable marker
        over messages no LLM has ever read — losing a memory pass to a broken
        kiro-cli install rather than to a genuinely unprocessable span. The
        environment counter only widens the retry interval, so a permanently broken
        host settles at the backoff ceiling instead of re-attempting every tick.
        """
        try:
            failures, retry_at = await asyncio.to_thread(
                self._log.record_consolidation_environment_failure,
                key,
                _CONSOLIDATION_BACKOFF_BASE_SECS,
                _CONSOLIDATION_BACKOFF_MAX_SECS,
            )
        except Exception:
            self._logger.warning(
                "Could not persist consolidation environment backoff for %s",
                key,
                exc_info=True,
            )
            return
        if failures < 1:
            return
        self._logger.warning(
            "Consolidation for %s could not reach the LLM (%s; environment "
            "failure #%d, nothing billed); retrying in %.0fs without consuming "
            "the attempt budget",
            key,
            reason,
            failures,
            max(0.0, retry_at - _time.time()),
        )

    def maybe_consolidate(self, key: str) -> None:
        """Fire preferences/projects consolidation if message threshold exceeded."""
        self._last_activity[key] = _time.time()
        if key in self._running:
            return
        total = len(self._log._read_messages(key))
        prefs_off = self._prefs_offset.get(key, 0)
        if total - prefs_off < _CONSOLIDATION_THRESHOLD:
            return
        # Cheap pre-check mirroring the other automatic entry points. This
        # runs on every user turn, so during a backoff window every message
        # past the threshold would otherwise schedule a task whose snapshot
        # takes the per-file lock (the same one appends contend on) and reads
        # the transcript, only to be refused by the gate inside _consolidate().
        # retry_eligible costs one metadata-line read and no transcript read;
        # the inner gate remains the enforcement backstop.
        if not self.retry_eligible(key, message_count=total):
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=False))
        self._tasks.add(t)

        def _on_done(fut: asyncio.Task, k: str = key, off: int = total) -> None:  # type: ignore[type-arg]
            self._tasks.discard(fut)
            if (
                not fut.cancelled()
                and fut.exception() is None
                # A refusal ran no pass over the window. Advancing the offset
                # anyway would mark the window consolidated, so once the
                # backoff expires the threshold test skips it until a whole new
                # threshold of messages accumulates — silently dropping its
                # preference/project extraction.
                and fut.result() is not _CONSOLIDATION_REFUSED
            ):
                self._prefs_offset[k] = off

        t.add_done_callback(_on_done)

    def check_idle_sessions(self) -> None:
        """Check all tracked sessions for idle-based history consolidation."""
        now = _time.time()
        for key, last in list(self._last_activity.items()):
            if now - last < self._history_idle_secs:
                continue
            total, unconsolidated = self._log.consolidation_counts(key)
            if (
                unconsolidated < 1
                or now - self._history_consolidated.get(key, 0) < self._history_idle_secs
                or key in self._running
                # Durable backoff, checked last so it only costs a metadata read
                # once the cheap conditions pass. The in-memory throttle above is
                # set only when the task ends without an exception and is lost on
                # restart, so it alone cannot stop a repeatedly failing span from
                # re-billing an LLM turn every tick. *total* comes from the read
                # above, so the check adds no transcript read on the loop.
                or not self.retry_eligible(key, now, message_count=total)
            ):
                continue
            self._running.add(key)
            captured_now = now
            t = asyncio.create_task(self._consolidate(key, include_history=True))
            self._tasks.add(t)

            def _on_idle_done(
                fut: asyncio.Task,  # type: ignore[type-arg]
                k: str = key,
                ts: float = captured_now,
            ) -> None:
                self._tasks.discard(fut)
                if (
                    not fut.cancelled()
                    and fut.exception() is None
                    # A refusal is not a completed pass; setting the throttle
                    # for it would delay the retry past the backoff deadline.
                    and fut.result() is not _CONSOLIDATION_REFUSED
                ):
                    self._history_consolidated[k] = ts

            t.add_done_callback(_on_idle_done)

    def consolidate_session(self, key: str) -> None:
        """Trigger history consolidation for *key* (fire-and-forget).

        Used by session-end hooks (dashboard close, Slack end, idle expiry)
        and the ``kirocrew consolidate`` CLI command.  Skips if the session
        is already being consolidated, has no unconsolidated messages, or is
        inside the durable consolidation retry backoff.

        Safety: skill detection (_run_skill_detection) re-checks
        _session_touched_sensitive() over its window before proposing anything,
        so sensitive sessions never produce skills regardless of entry point.
        """
        if key in self._running:
            return
        total, unconsolidated = self._log.consolidation_counts(key)
        if unconsolidated < 1:
            return
        # This path consults no time-based throttle at all — every session expiry
        # for the same key fires a fresh consolidation — so the durable backoff
        # stands between a repeatedly failing span and one billed LLM turn per
        # expiry. Checked here (as well as inside _consolidate()) so the skip is
        # logged before a task is ever scheduled.
        if not self.retry_eligible(key, message_count=total):
            self._logger.info(
                "consolidate_session skipped for %s: consolidation retry backoff", key
            )
            return
        # Short-circuit sensitive sessions before scheduling a task
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            self._logger.info("consolidate_session skipped for %s: sensitive session", key)
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=True))
        self._tasks.add(t)

        def _on_done(
            fut: asyncio.Task,  # type: ignore[type-arg]
            k: str = key,
        ) -> None:
            self._tasks.discard(fut)
            self._running.discard(k)
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is None:
                # A refusal is not a completed pass; leave the throttle unset.
                if fut.result() is not _CONSOLIDATION_REFUSED:
                    self._history_consolidated[k] = _time.time()
            else:
                self._logger.warning("consolidate_session failed for %s: %s", k, exc)

        t.add_done_callback(_on_done)

    async def consolidate_now(self, key: str) -> bool:
        """Consolidate a session synchronously (blocking).

        Unlike consolidate_session() which is fire-and-forget, this awaits
        completion. Used by the CLI command.

        Returns ``False`` when the consolidation retry backoff refused the
        span — so the CLI can report the skip instead of a false success —
        and ``True`` for every other completion (including the nothing-to-do
        and sensitive-session skips, which were already reported as done).

        Safety: defense-in-depth — the consolidation retry backoff is also
        checked inside _consolidate(), and _run_skill_detection() re-checks
        the sensitive-session guard over its own window.
        """
        if self._log.unconsolidated_count(key) < 1:
            return True
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            self._logger.info("consolidate_now skipped for %s: sensitive session", key)
            return True
        outcome = await self._consolidate(key, include_history=True)
        return outcome is not _CONSOLIDATION_REFUSED

    async def _consolidate(
        self, key: str, include_history: bool = True
    ) -> _ConsolidationRefusedSentinel | None:
        """Run LLM consolidation for a session.

        Returns :data:`_CONSOLIDATION_REFUSED` when the retry-eligibility gate
        refuses the span; every other completion returns ``None``.
        """
        # Capture the gateway loop so the thread-offloaded _process_auto_skills
        # can schedule the async dedupe judge back onto it.
        self._event_loop = asyncio.get_running_loop()
        # Flipped once the prompt actually reaches the provider, which is what
        # makes a failure expensive: everything before that point is free to
        # retry, everything after costs a turn that produced nothing durable.
        billed = False
        total = 0
        generation_at_snapshot = 0
        # The span identity any failure charge is stamped with. Rebuilt from the
        # snapshot below; the zero value only ever reaches a charge if the snapshot
        # itself raised, and that path is not billed.
        attempted = AttemptedSpan(0, 0, 0)
        try:
            # Atomically snapshot the unconsolidated tail, the total message
            # count (the absolute offset handed to mark_consolidated below), and
            # the rotation generation under ONE lock hold. Reading them as
            # separate calls let an append trigger a rotation between them,
            # pairing a pre-rotation offset with a post-rotation generation —
            # mark_consolidated would then see matching generations and apply
            # the stale offset (retained-count fallback misses it too), silently
            # dropping messages from extraction. Offloaded to a worker thread:
            # _consolidate runs on the gateway event loop and _locked/file IO is
            # blocking (same rationale as the mark_consolidated offload below).
            (
                unconsolidated,
                total,
                generation_at_snapshot,
            ) = await asyncio.to_thread(self._log.snapshot_for_consolidation, key)
            if not unconsolidated:
                return None
            # Retry-eligibility choke point: every entry point funnels through
            # this function, so a span inside its durable backoff is refused
            # here — before anything that can bill a provider turn — even if a
            # caller carries no pre-check of its own (a future entry point, or
            # a pre-check that raced the backoff being recorded). Callers keep
            # their cheaper pre-checks as scheduling short-circuits and UX (the
            # idle sweep's per-tick skip, maybe_consolidate's per-turn skip,
            # the dashboard trigger's 429); this gate is the enforcement that
            # holds when a new entry point forgets one. The count comes
            # from the atomic snapshot above — the same consistent read the
            # rest of this function uses — and retry_eligible costs one
            # metadata-line read, so no second transcript read lands on the
            # event loop. The refusal returns a sentinel rather than raising:
            # the finally block still releases self._running and the callers'
            # done-callbacks run normally (so the key is never stranded), while
            # the sentinel lets those callbacks tell a refusal from a completed
            # pass and leave their bookkeeping untouched.
            if not self.retry_eligible(key, message_count=total):
                self._logger.info("_consolidate refused for %s: consolidation retry backoff", key)
                return _CONSOLIDATION_REFUSED
            # Freeze the whole span identity from that one snapshot. The offset is
            # derived rather than returned because the snapshot slices at it
            # (``messages[offset:]``), so the subtraction is exact and comes from
            # the same lock hold — no second read that a concurrent rotation could
            # land between. A failure charge stamped with these values describes
            # what the turn attempted even if the file changed underneath it.
            attempted = AttemptedSpan(
                total=total,
                generation=generation_at_snapshot,
                offset=total - len(unconsolidated),
            )

            # Resolve workspace-scoped memory from session metadata
            meta = self._log.get_metadata(key)
            ws_name = meta.get("workspace")
            if ws_name:
                from kiro_crew.context import ContextBuilder

                memory = ContextBuilder.get_memory_for(ws_name)
            else:
                memory = self._memory

            conversation = "\n".join(_fmt_message(m) for m in unconsolidated)

            current_prefs = memory.read_preferences()
            current_projects = memory.read_projects()

            # Build prompt keys dynamically based on consolidation type
            keys: list[str] = []
            if include_history:
                keys.append(
                    '"history_entry": A concise paragraph (2-5 sentences) summarizing '
                    "what happened. Use local time [YYYY-MM-DD HH:MM]. Focus on "
                    "decisions, outcomes, facts. Use user's real name if known."
                )

            # Structured memory extraction (when vector store is available)
            has_vector = self._vector_store is not None
            if has_vector and self._vector_store is not None:
                # Offload: the fetch serializes on the store's _db_lock (#1947),
                # and this coroutine runs on the gateway event loop — a worker
                # holding the lock (backfill's FAISS rebuild, reconcile's bulk
                # UPDATEs) would otherwise block the whole loop here.
                current_semantic = await asyncio.to_thread(self._vector_store.get_all_semantic)

                def _prompt_value(e: dict) -> object:
                    # A lesson row stores a mapping; the consolidation model
                    # should read the rule prose, not a JSON envelope whose
                    # field names dilute the instruction it is weighing.
                    if str(e.get("key", "")).startswith("lesson."):
                        from kiro_crew.vector_memory import _lesson_display_text

                        try:
                            decoded = json.loads(e["value_json"])
                        except Exception:
                            return e["value_json"]
                        return _lesson_display_text(decoded) or e["value_json"]
                    return e["value_json"]

                semantic_json = (
                    json.dumps(
                        [
                            {
                                "key": e["key"],
                                "value_json": _prompt_value(e),
                                "confidence": e["confidence"],
                            }
                            for e in current_semantic
                        ],
                        indent=1,
                    )
                    if current_semantic
                    else "[]"
                )
                keys.append(
                    '"semantic": Array of structured facts to remember long-term. '
                    'Each: {"key": "<dotted.key>", "value": <json_value>, "confidence": 0.0-1.0, '
                    '"delete": false}. '
                    "Rules: keys must start with pref.*, project.*, or user.* "
                    "(e.g. pref.color, user.favorite_language, project.name). "
                    "confidence 1.0 = user stated, 0.8-0.9 = clearly implied, <0.8 = uncertain (rejected). "
                    "value must be a JSON primitive (string, number, boolean) — NOT objects or arrays. "
                    "IMPORTANT: Check existing semantic memory above. If a key already covers "
                    "the same topic, UPDATE that key instead of creating a new one. "
                    "Do NOT create near-duplicate keys (e.g. project.x.approach AND project.x.refined). "
                    'To DELETE a stale/invalidated key, set "delete": true (e.g. pet died → delete '
                    "user.pet.name; project cancelled → delete project.x.status). "
                    f"Max {_MAX_SEMANTIC_PER_CONSOLIDATION} items."
                )
                keys.append(
                    '"episodic": Array of conversation fragments worth remembering. '
                    'Each: {"text": "...", "tags": ["tag1"], "importance": 0.0-1.0}. '
                    "Rules: text 10-2000 chars, factual. importance 0.9+ = critical, "
                    "0.7-0.9 = useful, 0.5-0.7 = minor. Skip greetings/small talk. "
                    f"Max {_MAX_EPISODIC_PER_CONSOLIDATION} items. "
                    "IMPORTANT: Do NOT write simple key-value facts here that belong in semantic "
                    "(e.g. 'Favorite color: blue'). Episodic is for events, decisions, and context "
                    "— not for duplicating semantic facts."
                )

            # Markdown memory (backward compat when not migrated)
            if not self._migrated:
                keys.append(
                    '"preferences_update": The COMPLETE updated preferences file, '
                    "included ONLY if the file needs changes. Merge duplicates, keep "
                    "only newest if contradicted, remove stale one-off observations. "
                    "Keep '# User Preferences' header. If nothing changed, OMIT this "
                    "key entirely — never echo the file back and never answer with a "
                    "placeholder word like 'unchanged': the value overwrites the file, "
                    "so when present it must be the full file body."
                )
                keys.append(
                    '"projects_update": The COMPLETE updated projects file, included '
                    "ONLY if the file needs changes. Only active projects, remove "
                    "stale entries, update facts. Keep '# Active Projects' header. "
                    "If nothing changed, OMIT this key entirely — never echo the file "
                    "back and never answer with a placeholder word like 'unchanged': "
                    "the value overwrites the file, so when present it must be the "
                    "full file body."
                )

            if include_history:
                keys.append(
                    '"lessons": Array of corrections the user taught '
                    '(e.g. "no, do X", "always Y", "never Z"). '
                    'Each: {"rule": "...", "negative": "...", "category": "tool|preference|knowledge"}. '
                    "Empty [] if no corrections. Skip general preferences. "
                    f"Max {_MAX_LESSONS_PER_CONSOLIDATION} items. "
                    "IMPORTANT: Only extract lessons that the user did NOT explicitly ask "
                    "to remember (those are already saved via learn_add). Only extract "
                    "implicit corrections the user made without saying 'remember'."
                )

            # ── Auto skill detection ──
            # Skill detection runs as its OWN pass (below, after the memory
            # writes) over a wider last-N window of the full session — not the
            # incremental history tail — so a reusable procedure that spans the
            # whole session is judged as a unit. It is therefore intentionally
            # absent from this consolidation prompt's keys.

            numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(keys))
            prompt_parts = [
                "You are a memory consolidation agent. Process this conversation "
                f"and return a JSON object with these keys:\n\n{numbered}",
            ]
            if has_vector:
                prompt_parts.append(f"\n\n## Current Semantic Memory\n{semantic_json}")
            if not self._migrated:
                prompt_parts.append(f"\n\n## Current Preferences\n{current_prefs or '(empty)'}")
                prompt_parts.append(f"\n\n## Current Projects\n{current_projects or '(empty)'}")
            prompt_parts.append(f"\n\n## Conversation to Process\n{conversation}")
            prompt_parts.append("\n\nRespond with ONLY valid JSON, no markdown fences.")
            prompt = "".join(prompt_parts)

            try:
                result = await self._call_llm(prompt)
            except _ConsolidationNotDispatched as exc:
                # Nothing was sent, so nothing was billed. Charging this to the
                # attempt cap would let a handful of environment failures abandon
                # the span — writing the durable marker over messages no LLM has
                # ever read, which is the exact false-abandonment this accounting
                # exists to prevent. Arm the backoff only, so a broken host retries
                # on a widening interval instead of on every 60s tick.
                if include_history:
                    await self._note_environment_failure(key, str(exc))
                return None
            billed = True
            if not result:
                # The turn reached the provider and produced nothing usable, so it
                # was spent while the marker below stays unwritten. Returning
                # silently would look like success to the done-callbacks, setting
                # the in-memory throttle while the durable count still says
                # unconsolidated: the span re-bills a full turn every idle window,
                # and immediately after every restart. Charge the attempt.
                if include_history:
                    await self._note_failed_attempt(key, attempted, "empty LLM result")
                return None

            if entry := result.get("history_entry"):
                # Offloaded to a worker thread: append_history takes a blocking
                # advisory file lock (cross-process) and does synchronous file
                # IO, and _consolidate runs on the event loop thread (fired via
                # asyncio.create_task). Running it inline would let cross-process
                # lock contention stall the whole gateway loop.
                await run_in_embed_pool(memory.append_history, entry)
                self._logger.info("Consolidated %d messages for %s", len(unconsolidated), key)

            # Structured memory writes (Phase 2/3). Offloaded to a worker thread:
            # _write_structured_memory embeds each item via a blocking urllib call
            # to the in-process embedder, and _consolidate runs on the event loop thread (fired via
            # asyncio.create_task). Running it inline stalls the whole gateway loop
            # if the embedding endpoint is slow/hung (heartbeats, Slack, dashboard).
            if self._vector_store:
                await run_in_embed_pool(self._write_structured_memory, result, key)

            # Markdown writes (backward compat — skip if migrated). Each value
            # replaces the whole file, so a non-file answer (e.g. the literal
            # word "unchanged") must be discarded, not written: once written it
            # re-enters the next prompt as the file's current content and primes
            # every later pass to repeat it (see _is_plausible_memory_file).
            if not self._migrated:
                if prefs := result.get("preferences_update"):
                    if not _is_plausible_memory_file(prefs, "# User Preferences"):
                        self._logger.warning(
                            "Discarding implausible preferences_update from "
                            "consolidation (missing '# User Preferences' header "
                            "or placeholder body; %d chars)",
                            len(prefs),
                        )
                    elif prefs.strip() != current_prefs.strip():
                        # Offloaded like append_history above (blocking file
                        # I/O on the event loop thread). expected_baseline is
                        # the compare-and-swap guard: this whole-file result
                        # was merged from current_prefs, read BEFORE the
                        # minutes-long LLM call — if a dashboard Save landed
                        # in that window, writing would silently revert it,
                        # so the store skips the stale write instead.
                        wrote = await run_in_embed_pool(
                            lambda: memory.write_preferences(prefs, expected_baseline=current_prefs)
                        )
                        if not wrote:
                            self._logger.info(
                                "Consolidated preferences for %s discarded: file "
                                "changed during consolidation",
                                key,
                            )

                if projects := result.get("projects_update"):
                    if not _is_plausible_memory_file(projects, "# Active Projects"):
                        self._logger.warning(
                            "Discarding implausible projects_update from "
                            "consolidation (missing '# Active Projects' header "
                            "or placeholder body; %d chars)",
                            len(projects),
                        )
                    elif projects.strip() != current_projects.strip():
                        wrote = await run_in_embed_pool(
                            lambda: memory.write_projects(
                                projects, expected_baseline=current_projects
                            )
                        )
                        if not wrote:
                            self._logger.info(
                                "Consolidated projects for %s discarded: file "
                                "changed during consolidation",
                                key,
                            )

            # Lesson extraction: _save_lessons calls write_lesson which embeds
            # each rule (+ up to 5 lazy backfills) via blocking urllib to Ollama.
            # Same rationale as _write_structured_memory above — must offload.
            if (self._lesson_store or self._vector_store) and (
                raw_lessons := result.get("lessons")
            ):
                await run_in_embed_pool(self._save_lessons, raw_lessons)

            # Auto skill detection — a SEPARATE LLM pass over the full-session
            # window (see _run_skill_detection), not the incremental tail. Runs
            # only on history consolidation, guarded by flag + loader; failures
            # are logged, never fatal.
            if include_history and self._auto_skills_enabled and self._skills_loader is not None:
                try:
                    await self._run_skill_detection(key)
                except Exception:
                    self._logger.warning("Auto-skill detection failed for %s", key, exc_info=True)

            # Autonomous lifecycle: age-based archival must run even when this
            # pass created/approved no skill, otherwise skills never age out on
            # their own (create/approve were the only triggers). Consolidation is
            # the existing idle/periodic path; throttle to at most once/hour
            # across all sessions so frequent consolidations don't rescan the set.
            if self._skills_loader is not None and (_time.time() - self._last_lifecycle) > 3600:
                self._last_lifecycle = _time.time()
                try:
                    await asyncio.to_thread(
                        self._skills_loader.run_skill_lifecycle,
                        max_auto_skills=self._max_auto_skills,
                        stale_after_days=self._stale_after_days,
                        archive_after_days=self._archive_after_days,
                    )
                except Exception:
                    self._logger.debug("Periodic skill lifecycle pass failed", exc_info=True)

            # Only advance the consolidated offset for history consolidation.
            # Prefs-only consolidation uses a separate in-memory offset.
            # mark_consolidated does a synchronous, fsync-backed rewrite of the
            # whole transcript (up to a couple of MB) behind the per-file lock.
            # _consolidate runs on the gateway event loop (fired via
            # asyncio.create_task), so offload the blocking rewrite to a worker
            # thread — otherwise a slow filesystem freezes the loop (heartbeats,
            # Slack, dashboard). Same rationale as the offloads above.
            if include_history:
                await asyncio.to_thread(
                    self._log.mark_consolidated,
                    key,
                    total,
                    generation_at_snapshot,
                )

        except Exception:
            self._logger.exception("Consolidation failed for %s", key)
            # Anything raised between the LLM call and mark_consolidated (memory
            # writes, lesson writes, the marker write itself) re-raises, so the
            # idle sweep's done-callback never sets its throttle and all of its
            # skip conditions are false again on the next 60s tick. Charging the
            # attempt here is what converts that tight loop into backoff.
            if billed and include_history:
                await self._note_failed_attempt(key, attempted, "exception after the LLM call")
            raise
        finally:
            self._running.discard(key)
        return None

    async def _run_skill_detection(self, key: str) -> None:
        """Detect a reusable skill from the FULL session (bounded window).

        Unlike history/semantic/lesson extraction — which correctly runs on the
        incremental unconsolidated tail — skill detection judges the last
        ``_SKILL_DETECTION_WINDOW`` messages of the WHOLE session, decoupled
        from the consolidation offset. A reusable procedure usually spans a
        session rather than the slice since the last consolidation, so a
        tail-only view systematically misses skills in any session consolidated
        more than once. The skill need only be demonstrated by PART of the
        window; the pass does not have to cover the whole session.

        Runs as its own LLM call so the consolidation prompt stays tail-scoped
        (widening THAT prompt would re-summarize already-consolidated messages
        into duplicate history/semantic entries). A per-session
        (rotation_generation, count) guard skips re-running when nothing new has
        been appended since the last pass, yet still forces a fresh pass after a
        transcript rotation (which swaps the window's content); genuine repeats
        are still caught by the dedupe verdict in ``_process_auto_skills``.

        The prompt gates on RECURRENCE, not effort. A session can be long,
        difficult, and rich in tool calls while still being one-off — a single
        bug's fix, a one-time audit of one component, a probe answering a
        question that is now answered — and the tool-call floor
        (``auto_min_tool_calls``) cannot tell those apart from a repeatable
        method. So the prompt makes the model name the future session and the
        DIFFERENT target that would reuse the procedure, and return null when
        the only honest answer reuses this session's own artifact. It also
        prefers null under uncertainty: an unreusable candidate is not free,
        because it spends the human's review attention on every later proposal.
        """
        if self._skills_loader is None:
            return
        all_messages = await asyncio.to_thread(self._log._read_messages, key)
        if not all_messages:
            return
        # Key the guard on (rotation generation, message count), NOT count
        # alone. The transcript rotates at _SESSION_MAX_BYTES / _SESSION_KEEP_LINES:
        # a rotation bumps rotation_generation and replaces the window with fresh
        # messages even when the resulting count matches a prior value, so a
        # count-only guard would wrongly treat a rotated session as unchanged and
        # never propose its skill. Comparing the pair re-detects after any
        # rotation while still skipping a genuinely unchanged session.
        generation = await asyncio.to_thread(
            lambda: int(self._log._read_metadata(key).get("rotation_generation", 0) or 0)
        )
        marker = (generation, len(all_messages))
        if self._last_skillgen_marker.get(key) == marker:
            return
        window = all_messages[-_SKILL_DETECTION_WINDOW:]
        if _count_tool_call_messages(window) < self._auto_min_tool_calls:
            return
        if _session_touched_sensitive(window):
            return

        scripts_field = ""
        if self._generate_scripts:
            scripts_field = (
                ', "scripts": (optional array, part of THIS new_skill '
                "object) ONLY when the procedure includes a "
                "DETERMINISTIC, always-identical step sequence worth "
                "running verbatim (a fixed command chain, a set API "
                "sequence, a predictable file transform). Each item: "
                '{"filename": "<name>.py", "language": "python", '
                '"content": "<self-contained Python, no network to '
                "unknown hosts, no credential access, no destructive "
                'commands, <=4KB>"}. Python ONLY (must run on Windows). '
                "Omit for judgment-based / context-dependent procedures. "
                "Scripts always require human approval"
            )
        skill_keys = [
            '"new_skill": Object or null. Return an object ONLY if this '
            "session demonstrated a procedure that will RECUR — one a future "
            "session, working on a DIFFERENT target, would run again "
            "substantially unchanged (e.g. a repeatable debugging method for a "
            "class of error, a fixed command/API sequence, a verification "
            "technique). The procedure may be demonstrated by only PART of the "
            "excerpt below — you do NOT need to cover the whole session. "
            "Shape: "
            '{"slug": "<kebab-case-4-to-60-chars>", '
            '"description": "<=150 chars, starts with verb>", '
            '"triggers": "<3-8 comma-separated keywords/phrases>", '
            '"procedure_md": "<concise markdown body with '
            "## When to use / ## Steps / ## Gotchas sections, "
            '<=8000 chars>"' + scripts_field + "}. "
            "## The recurrence test (apply BEFORE returning an object)\n"
            "Name the future session that would load this skill and the "
            "DIFFERENT target it would run against. If the only honest answer "
            "reuses this session's specific artifact — this bug, this file, "
            "this component, this one question — the procedure does not recur "
            "and you MUST return null. Effort is not evidence of recurrence: a "
            "long, many-step, genuinely difficult session is still one-off if "
            "its steps were chosen for one target.\n"
            "Return null for: a task done once and now finished (a specific "
            "bug's fix, a one-time audit/trace of one component, a migration, "
            "a probe run to answer a question that is now answered); a design "
            "or planning discussion; a narrative of what happened in this "
            "session; a procedure whose steps only make sense against the "
            "exact artifact at hand; a trivial or single-shot answer; a "
            "one-off failure with no reusable takeaway; anything touching "
            "sensitive paths. Prefer null when uncertain — an unreusable "
            "candidate costs the user review effort on every future proposal, "
            "so silence is cheaper than a plausible-looking one-off. "
            "Do NOT include absolute paths, credentials, tokens, or user PII "
            "in the procedure body."
        ]
        if self._auto_refine_enabled:
            skill_keys.append(
                '"refined_skill": Object or null. If an existing '
                '"auto/..." skill was loaded during this session AND '
                "the agent found a better procedure than the one "
                "documented in that skill, return: "
                '{"name": "auto/<existing-slug>", '
                '"description": "<updated>", "triggers": "<updated>", '
                '"procedure_md": "<refined markdown>"}. Return null '
                "if nothing was refined. Do not fabricate refinements."
            )
        numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(skill_keys))
        conversation = "\n".join(_fmt_message(m) for m in window)
        prompt = (
            "You are a skill-extraction agent. Review this session excerpt and "
            "return a JSON object with these keys:\n\n"
            + numbered
            + "\n\n## Session excerpt\n"
            + conversation
            + "\n\nRespond with ONLY valid JSON, no markdown fences."
        )
        try:
            result = await self._call_llm(prompt)
        except _ConsolidationNotDispatched:
            # Skill detection is best-effort and owns no retry accounting, so an
            # unreachable provider is simply no detection this pass. The marker
            # below is still recorded, matching the existing failed-turn path.
            result = None
        # Record the (generation, count) marker regardless of outcome so an
        # unchanged session isn't re-evaluated on every subsequent
        # consolidation, but a rotation still forces a fresh pass.
        self._last_skillgen_marker[key] = marker
        if not result:
            return
        # Log the verdict, not just the proposals. The prompt's default is null,
        # so silence is the common outcome, and the staging log in
        # ``_process_auto_skills`` only fires when a candidate is produced --
        # which would leave the queue showing the false-POSITIVE rate while the
        # false-negative rate had no signal at all.
        self._logger.debug(
            "Skill detection verdict for %s: %s",
            key,
            "candidate proposed" if result.get("new_skill") else "no recurring procedure",
        )
        # _event_loop was captured by our caller (_consolidate) so the
        # thread-offloaded dedupe judge can marshal back onto the gateway loop.
        await asyncio.to_thread(self._process_auto_skills, result, key)

    def _save_lessons(self, raw: object) -> None:
        """Save extracted lessons from consolidation result."""
        if not isinstance(raw, list):
            return

        # Cap like semantic/episodic: each write_lesson can perform up to 6
        # blocking embeds, so an uncapped LLM lessons array would occupy a
        # worker thread for minutes.
        max_lessons = _MAX_LESSONS_PER_CONSOLIDATION
        if len(raw) > max_lessons:
            self._logger.warning(
                "Consolidation returned %d lessons; capping to %d",
                len(raw),
                max_lessons,
            )
            raw = raw[:max_lessons]

        # Prefer vector store (dedup-aware) over JSONL
        if self._vector_store:
            count = 0
            for item in raw:
                if isinstance(item, dict) and item.get("rule"):
                    ok = self._vector_store.write_lesson(
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        source="consolidation",
                    )
                    if ok:
                        count += 1
            if count:
                self._logger.info("Extracted %d lesson(s) from chat (vector store)", count)
            return

        if not self._lesson_store:
            return
        from datetime import timezone as _tz

        from kiro_crew.learn import Lesson

        count = 0
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                self._lesson_store.save(
                    Lesson(
                        ts=datetime.now(tz=_tz.utc).isoformat(),
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                    )
                )
                count += 1
        if count:
            self._logger.info("Extracted %d lesson(s) from chat", count)

    def _write_structured_memory(self, result: dict, key: str) -> None:
        """Write semantic + episodic entries from consolidation result."""
        if not self._vector_store:
            return
        source = f"consolidation:{key}"

        # Semantic entries
        semantic_items = result.get("semantic")
        if isinstance(semantic_items, list):
            written = 0
            deleted = 0
            skipped = 0
            refused = 0
            for item in semantic_items[:_MAX_SEMANTIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or "key" not in item:
                    continue
                # Handle deletion of stale keys
                if item.get("delete"):
                    if self._vector_store.delete_semantic(item["key"], source):
                        deleted += 1
                    continue
                if "value" not in item or item["value"] is None:
                    # Counted and logged here because this path returns before set_semantic, so
                    # the VALUE_EMPTY reject event never fires for the omission that motivated it.
                    skipped += 1
                    self._logger.warning(
                        "Semantic consolidation skipped %r: item carries no value", item["key"]
                    )
                    continue
                conf = float(item.get("confidence", 0.5))
                # Confidence 1.0 means user explicitly stated it — escalate source
                # so it can overwrite previous user_explicit entries
                item_source = "user_explicit" if conf >= 1.0 else source
                err = self._vector_store.set_semantic(
                    key=item["key"],
                    value=item["value"],
                    confidence=conf,
                    source=item_source,
                )
                if err is None:
                    written += 1
                else:
                    # Counted apart from `skipped`: several reject causes reach here and only
                    # VALUE_EMPTY is a missing value, so a shared label names the wrong cause.
                    reject_code, _reason = err
                    refused += 1
                    self._logger.warning(
                        "Semantic consolidation refused %r: %s", item["key"], reject_code.value
                    )
            if written or deleted or skipped or refused:
                self._logger.info(
                    "Semantic consolidation: %d written, %d deleted, %d skipped (no value), "
                    "%d refused",
                    written,
                    deleted,
                    skipped,
                    refused,
                )

        # Episodic entries
        episodic_items = result.get("episodic")
        if isinstance(episodic_items, list):
            written = 0
            for item in episodic_items[:_MAX_EPISODIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or "text" not in item:
                    continue
                ep_ok = self._vector_store.write_episodic(
                    text=item["text"],
                    conversation_id=key,
                    tags=item.get("tags", []),
                    importance=float(item.get("importance", 0.5)),
                    source=source,
                )
                if ep_ok:
                    written += 1
            if written:
                self._logger.info("Wrote %d episodic entries from consolidation", written)

    def _dedupe_candidate(
        self, slug: str, description: str, triggers: str
    ) -> "tuple[str, str | None]":
        """Classify a candidate against existing auto-skills.

        Returns ``(verdict, key)`` where ``verdict`` is one of ``VERDICT_NEW``
        (stage as a new candidate), ``VERDICT_DUP`` (drop — pure re-detection),
        or ``VERDICT_UPDATE`` (stage a pending update to ``key``). ``key`` is the
        matched/target existing-skill key for DUP/UPDATE, else ``None``.

        Primary: a single tri-state metadata-judge call comparing the candidate
        against ALL existing auto-skills at once (bounded set, no embeddings).
        Lexical ``find_similar`` runs as a fallback when the judge is unavailable
        (no ``judge_model``, no captured event loop, or no existing skills) AND
        as a safety net when the judge returns ``VERDICT_NEW`` — so a judge
        *failure* (which fails open to "new") can't silently skip dedup and let
        a near-identical skill through. A lexical hit is treated as a DUP.
        """
        loader = self._skills_loader
        if loader is None:
            return (VERDICT_NEW, None)
        existing = list(loader.list_auto_skills())
        # Include already-staged (pending) candidates so repeated sessions don't
        # queue a duplicate of something still awaiting review (list_auto_skills
        # only enumerates LIVE skills — .pending is pruned from discovery).
        try:
            for p in loader.list_pending_skills():
                existing.append(
                    {
                        "key": f"auto/{p.get('slug', '')}",
                        "description": p.get("description", ""),
                        "triggers": p.get("triggers", ""),
                    }
                )
        except Exception:
            pass
        loop = self._event_loop

        def _lexical() -> "tuple[str, str | None]":
            hit = loader.find_similar(description, threshold=self._auto_similarity_threshold)
            return (VERDICT_DUP, hit) if hit else (VERDICT_NEW, None)

        if self._judge_model and existing and loop is not None:

            def _judge_fn(prompt: str) -> str:
                try:
                    fut = asyncio.run_coroutine_threadsafe(self._dedupe_judge(prompt), loop)
                    return fut.result(timeout=60) or ""
                except Exception:
                    return ""

            candidate = {
                "key": f"auto/{slug}",
                "description": description,
                "triggers": triggers,
            }
            verdict, key = _facade_metadata_dedupe_verdict(candidate, existing, _judge_fn)
            # VERDICT_NEW means "new" OR a judge error (the verdict API fails open
            # to new). Either way, confirm with the cheap lexical check before
            # concluding the candidate is unique.
            if verdict == VERDICT_NEW:
                return _lexical()
            return (verdict, key)
        return _lexical()

    async def _dedupe_judge(self, prompt: str) -> str:
        """One cheap metadata-dedupe judge turn on the shared background session.
        Runs on that session's existing (lite / haiku-class) model — no per-turn
        ``set_model`` switch, because the ``BACKGROUND_KEY`` session is shared
        with consolidation and a switch would leak the judge model into later
        turns when recycling doesn't fire. Fail-open (returns "" on any error)."""
        if not self._sessions:
            return ""
        try:
            async with background_turn(
                self._sessions, task="skill_dedupe", agent="kirocrew-lite"
            ) as client:
                text = await _facade_stream_and_collect(
                    client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
            return text or ""
        except Exception:
            self._logger.debug("Skill dedupe judge failed", exc_info=True)
            return ""

    async def _merge_skill_update(
        self, live_body: str, description: str, triggers: str, procedure_md: str
    ) -> "str | None":
        """Merge an existing live skill body with a new candidate into ONE
        updated markdown body — a single text turn on the shared background
        session. Mirrors ``_dedupe_judge`` exactly. Fail-open (returns ``None`` on
        any error) so the caller can fall back to a plain replacement proposal."""
        if not self._sessions:
            return None
        prompt = (
            "You are updating an existing auto-generated agent skill with a newly "
            "learned requirement. Merge the EXISTING skill body and the NEW "
            "requirement into ONE updated markdown skill body — fold the new "
            "requirement in, do NOT blindly replace the existing content. Keep "
            "the '## When to use', '## Steps', and '## Gotchas' sections. Keep "
            "the result under 8000 characters. Output ONLY the updated markdown "
            "body — no preamble, no explanation, no code fences.\n\n"
            f"EXISTING skill body:\n{live_body}\n\n"
            f"NEW requirement — description: {description}\n"
            f"NEW requirement — triggers: {triggers}\n"
            f"NEW requirement — procedure:\n{procedure_md}\n"
        )
        try:
            async with background_turn(
                self._sessions, task="skill_merge", agent="kirocrew-lite"
            ) as client:
                text = await _facade_stream_and_collect(
                    client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
            return text or None
        except Exception:
            self._logger.debug("Skill update merge failed", exc_info=True)
            return None

    def _stage_skill_update(
        self,
        *,
        key: str,
        target_key: str,
        description: str,
        triggers: str,
        procedure_md: str,
        scripts: "list[dict] | None" = None,
    ) -> None:
        """Stage a pending UPDATE candidate for an existing auto-skill.

        (a) read the target's current live body; (b) LLM-merge it with the new
        requirement (bridged from this worker thread onto the captured loop,
        90s, fail-open); (c) use the redacted merge as the proposed body, else
        fall back to the candidate's own procedure (also on oversize); (d) stage
        under ``<target-slug>-update`` with ``kind='update'`` metadata; (e) SEL
        audit with outcome ``staged_update``."""
        loader = self._skills_loader
        if loader is None:
            return

        def _redact(text: object) -> str:
            if not isinstance(text, str):
                return ""
            safe, _ = redact_exfiltration_urls(text)
            safe, _ = redact_credentials(safe)
            return safe

        target_slug = target_key.split("/", 1)[-1]
        # Capture the base version BEFORE reading the body it describes. The merge
        # turn below can take up to 90s, and an approval landing in that window
        # advances live — sampling the version afterwards would record the NEW
        # version against a body merged from the OLD one, and
        # ``approve_pending_update``'s staleness guard would then see base ==
        # current and let the stale body overwrite the intervening update. Reading
        # it first fails safe in the other direction: if live advances after this
        # point the recorded base is behind, the guard fires, and the candidate is
        # refused rather than silently applied.
        try:
            base_version = loader.get_auto_skill_version(target_key)
        except Exception:
            base_version = 1
        try:
            live_body = loader.read_auto_skill_body(target_key)
        except Exception:
            live_body = None
        if not live_body:
            # ``_dedupe_candidate`` deliberately includes already-PENDING
            # candidates in the judge's ``existing`` set (so repeated sessions
            # don't queue duplicates), which means the judge can answer
            # ``UPDATE auto/<pending-slug>`` — a target that is not live.
            # ``approve_pending_update`` requires a live target, so staging that
            # would queue a candidate the user can never approve. Drop it
            # instead, audited so the loss is visible.
            self._logger.info(
                "Skill update skipped: target '%s' is not a live auto skill",
                target_key,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="rejected",
                metadata={"target": target_key, "reason": "target_not_live"},
            )
            return
        # ``read_auto_skill_body`` returns the FULL SKILL.md (frontmatter
        # included). Only the prose body may be merged: ``stage_skill_candidate``
        # re-wraps the result in its own frontmatter, so feeding the header in
        # invites the merge to echo it back and nest a second ``---`` block
        # inside the procedure.
        # Redact before the merge prompt. The read path already refuses symlinks
        # into credential storage, but a credential can also be typed straight
        # INTO a skill body via the dashboard editor — that file legitimately
        # lives in the skills tree, so no path guard catches it. The candidate's
        # own description/triggers/procedure are redacted upstream; this was the
        # one input reaching the model raw. (Redaction also runs on the merge
        # OUTPUT, which is too late to protect the prompt.)
        live_prose = _redact(_strip_skill_frontmatter(live_body))

        merged: "str | None" = None
        if live_prose and self._event_loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._merge_skill_update(live_prose, description, triggers, procedure_md),
                    self._event_loop,
                )
                merged = fut.result(timeout=90)
            except Exception:
                merged = None

        used_merge = False
        body = procedure_md
        if merged:
            # Defensive sanitize: the prompt forbids fences/frontmatter, but a
            # model may still emit them — strip both so the staged candidate's
            # procedure is pure markdown prose.
            red = _redact(_strip_skill_frontmatter(_strip_code_fence(merged)))
            if red and len(red) <= AUTO_SKILL_MAX_PROCEDURE_CHARS:
                body = red
                used_merge = True

        provenance = AutoSkillProvenance(session_key=key, created_at=AutoSkillProvenance.now_iso())
        # The slug pattern caps at 64 chars, and our own generation prompt permits
        # up to 60, so `<target>-update` can overflow and be REJECTED by staging —
        # silently dropping the learning, because consolidation advances its
        # message offset regardless of candidate outcome. Reserve room for
        # "-update" (7) plus the "-2".."-50" collision suffix (3).
        _update_slug = f"{target_slug[:54].rstrip('-')}-update"
        # Approval writes the candidate's frontmatter over the live skill, so the
        # candidate must carry the MERGED metadata, not just its own. The body is
        # merged by the LLM turn above; description/triggers were not, and the
        # candidate only proposes triggers for the NEW requirement — replacing the
        # live list would stop the skill activating on everything it already
        # answered. Union the triggers and keep the live description when the
        # candidate did not supply one.
        _live_triggers = _frontmatter_value(live_body, "triggers")
        _live_description = _frontmatter_value(live_body, "description")
        _staged_triggers = _merge_trigger_lists(_live_triggers, triggers)
        _staged_description = description or _live_description
        name = loader.stage_skill_candidate(
            _update_slug,
            description=_staged_description,
            triggers=_staged_triggers,
            procedure_md=body,
            provenance=provenance,
            scripts=scripts or None,
            kind="update",
            target=target_key,
            base_version=base_version,
        )
        if name:
            self._logger.info(
                "Staged skill update %s (target %s) from session %s",
                name,
                target_key,
                key,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="staged_update",
                metadata={
                    "name": name,
                    "target": target_key,
                    "base_version": base_version,
                    "merged": used_merge,
                },
            )
        else:
            self._logger.info("Skill update staging rejected for target '%s'", target_key)
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="rejected",
                metadata={"slug": _update_slug, "reason": "creation_failed"},
            )

    def _process_auto_skills(self, result: dict, key: str) -> None:
        """Extract + write auto-generated skills from the consolidation result.

        Handles both ``new_skill`` and ``refined_skill`` result keys.  Each
        is validated, redacted via ``security.redact_*``, then deduped
        against existing skills (for new creation) before being written
        through ``SkillsLoader``.  Every successful write emits a SEL audit
        event via ``_facade_sel().log_tool_invocation``.
        """
        if self._skills_loader is None:
            return

        def _redact(text: object) -> str:
            """Run the same two-pass redaction used for Slack/dashboard output."""
            if not isinstance(text, str):
                return ""
            safe, _ = redact_exfiltration_urls(text)
            safe, _ = redact_credentials(safe)
            return safe

        # Create path
        new_skill = result.get("new_skill")
        if isinstance(new_skill, dict):
            slug = str(new_skill.get("slug", "")).strip()
            description = _redact(new_skill.get("description", ""))
            triggers = _redact(new_skill.get("triggers", ""))
            procedure_md = _redact(new_skill.get("procedure_md", ""))
            # Extract + statically validate any generated scripts. Scripts are
            # redacted, then each is checked by the always-on static validator;
            # only individually-clean scripts survive. A script-bearing
            # candidate ALWAYS routes to approval (never auto-published).
            valid_scripts: list[dict] = []
            scripts_supplied = False
            if self._generate_scripts:
                raw_scripts = new_skill.get("scripts")
                if isinstance(raw_scripts, list) and raw_scripts:
                    scripts_supplied = True
                    for s in raw_scripts:
                        if not isinstance(s, dict):
                            continue
                        fn = _redact(s.get("filename", "")).strip()
                        body = _redact(s.get("content", ""))
                        ok, _findings = validate_skill_script(fn, body)
                        if ok:
                            valid_scripts.append({"filename": fn, "content": body})
                        else:
                            self._logger.info(
                                "Auto-skill script %r rejected by validator: %s",
                                fn,
                                "; ".join(_findings),
                            )
            if not (slug and description and procedure_md):
                # Required fields missing (or stripped empty by redaction).
                # Audit the rejection so operators can see that a create
                # attempt happened but lacked the minimum inputs.
                self._logger.info(
                    "Auto-skill create skipped: empty slug/description/procedure "
                    "after redaction (slug=%r)",
                    slug,
                )
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_create",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={
                        "slug": slug or "(empty)",
                        "reason": "empty_after_redaction",
                    },
                )
            else:
                verdict, target = self._dedupe_candidate(slug, description, triggers)
                # ``_dedupe_candidate`` deliberately shows the judge already-PENDING
                # candidates too (so repeat sessions don't queue duplicates), which
                # means an UPDATE verdict can name a target that is not LIVE. Such a
                # target cannot be updated — but the requirement is genuinely new
                # relative to the live skill set, and consolidation advances its
                # message offset regardless, so dropping it would lose the learning
                # for good. Downgrade to a NEW candidate instead: it only overlaps
                # another *proposal*, which the human reviews side by side anyway.
                if verdict == VERDICT_UPDATE and target:
                    try:
                        _target_is_live = (
                            self._skills_loader.read_auto_skill_body(target) is not None
                        )
                    except Exception:
                        _target_is_live = False
                    if not _target_is_live:
                        self._logger.info(
                            "Auto-skill UPDATE target '%s' is not live (pending candidate); "
                            "staging '%s' as a new candidate instead of dropping it",
                            target,
                            slug,
                        )
                        verdict = VERDICT_NEW
                if verdict == VERDICT_DUP:
                    self._logger.info(
                        "Auto-skill synthesis skipped: '%s' overlaps existing skill '%s'",
                        slug,
                        target,
                    )
                    _facade_sel().log_tool_invocation(
                        session_key=key,
                        tool_name="auto_skill_create",
                        tool_kind="skills",
                        outcome="rejected",
                        metadata={
                            "slug": slug,
                            "reason": "similar_exists",
                            "existing": target,
                        },
                    )
                elif verdict == VERDICT_UPDATE and target:
                    # Same skill, new requirements worth folding in — stage a
                    # pending UPDATE candidate rather than dropping the learning.
                    self._stage_skill_update(
                        key=key,
                        target_key=target,
                        description=description,
                        triggers=triggers,
                        procedure_md=procedure_md,
                        scripts=valid_scripts or None,
                    )
                else:
                    provenance = AutoSkillProvenance(
                        session_key=key,
                        created_at=AutoSkillProvenance.now_iso(),
                    )
                    if self._approval_required or valid_scripts or scripts_supplied:
                        # Stage for human review — nothing goes live unattended,
                        # and any candidate that SUPPLIED scripts ALWAYS stages
                        # (even if every script was rejected by the validator, so
                        # a script-bearing candidate can never auto-publish as a
                        # prose-only skill).
                        name = self._skills_loader.stage_skill_candidate(
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                            scripts=valid_scripts or None,
                        )
                        if name:
                            self._logger.info(
                                "Staged skill candidate %s from session %s", name, key
                            )
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="staged",
                                metadata={"name": name, "scripts": len(valid_scripts)},
                            )
                        else:
                            self._logger.info("Skill staging rejected for slug '%s'", slug)
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="rejected",
                                metadata={"slug": slug, "reason": "creation_failed"},
                            )
                    else:
                        name = self._skills_loader.create_auto_skill(
                            slug,
                            description=description,
                            triggers=triggers,
                            procedure_md=procedure_md,
                            provenance=provenance,
                        )
                        if name:
                            self._logger.info("Auto-created skill %s from session %s", name, key)
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="invoked",
                                metadata={"name": name},
                            )
                            # Bound the live auto-skill set after a live create
                            # (auto-approve path). Best-effort; never break
                            # consolidation on a lifecycle hiccup.
                            try:
                                self._skills_loader.run_skill_lifecycle(
                                    max_auto_skills=self._max_auto_skills,
                                    stale_after_days=self._stale_after_days,
                                    archive_after_days=self._archive_after_days,
                                )
                            except Exception:  # pragma: no cover - defensive
                                self._logger.debug("Skill lifecycle pass failed", exc_info=True)
                        else:
                            # create_auto_skill returned None: invalid slug,
                            # oversized procedure, or directory already exists.
                            # Audit the rejection so operators can see why.
                            self._logger.info(
                                "Auto-skill creation rejected for slug '%s' (creation_failed)",
                                slug,
                            )
                            _facade_sel().log_tool_invocation(
                                session_key=key,
                                tool_name="auto_skill_create",
                                tool_kind="skills",
                                outcome="rejected",
                                metadata={
                                    "slug": slug,
                                    "reason": "creation_failed",
                                },
                            )
        else:
            # Eligible session ran the skill-gen prompt, but the model returned
            # no new-skill candidate. Emit a lightweight audit trail so
            # operators can distinguish "asked, model declined" from "never
            # asked" — previously this branch left no SEL event or log line,
            # making it impossible to tell from the audit log whether skill
            # generation was ever attempted during a consolidation.
            self._logger.info(
                "Auto-skill: model proposed no skill candidate for session %s",
                key,
            )
            _facade_sel().log_tool_invocation(
                session_key=key,
                tool_name="auto_skill_create",
                tool_kind="skills",
                outcome="skipped",
                metadata={"reason": "no_candidate_proposed"},
            )

        # Refine path (only if explicitly enabled)
        if not self._auto_refine_enabled:
            return
        refined = result.get("refined_skill")
        if isinstance(refined, dict):
            name = str(refined.get("name", "")).strip()
            if not self._skills_loader.is_auto_generated(name):
                self._logger.info("Auto-skill refine rejected for %s: not in auto namespace", name)
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "not_auto_namespace"},
                )
                return
            description = _redact(refined.get("description", ""))
            triggers = _redact(refined.get("triggers", ""))
            procedure_md = _redact(refined.get("procedure_md", ""))
            if not description or not procedure_md:
                self._logger.info(
                    "Auto-skill refine skipped for %s: empty description/procedure "
                    "after redaction",
                    name,
                )
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "empty_after_redaction"},
                )
                return
            provenance = AutoSkillProvenance(
                session_key=key,
                created_at=AutoSkillProvenance.now_iso(),
                refined_at=AutoSkillProvenance.now_iso(),
            )
            ok = self._skills_loader.update_auto_skill(
                name,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            if ok:
                self._logger.info("Auto-refined skill %s from session %s", name, key)
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="invoked",
                    metadata={"name": name},
                )
            else:
                # update_auto_skill returned False: oversized procedure,
                # file missing, or other internal rejection.  Audit it so
                # operators can trace why a refine was proposed but not
                # applied.
                self._logger.info("Auto-skill refine rejected for %s (update_failed)", name)
                _facade_sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "update_failed"},
                )

    async def _call_llm(self, prompt: str) -> dict | None:
        """Call LLM for consolidation via the persistent background session.

        Uses the shared background kiro-cli process (no spawn/teardown cost).
        Returns the parsed JSON dict, or ``None`` when the turn reached the
        provider but produced nothing usable (a failed or unparsable answer).

        Raises :class:`_ConsolidationNotDispatched` when the prompt never reached
        the provider at all — no session manager, or the background session could
        not be acquired because kiro-cli is missing, not logged in, or failing to
        start. That case is signalled separately rather than folded into ``None``
        because the two cost different things: a spent turn costs money and must
        consume the caller's retry budget, while a prompt that was never sent costs
        nothing and must not, or a broken host would abandon spans it never read.
        An exception (rather than a flag beside the result) is used so a caller
        cannot silently drop the distinction.

        Once ``stream_and_collect_json`` is entered the prompt counts as sent: a
        failure inside it may still have been billed, so it returns ``None`` and is
        charged rather than risk an unbounded retry loop over real spend.
        """
        if not self._sessions:
            self._logger.warning("LLM consolidation skipped — no session manager")
            raise _ConsolidationNotDispatched("no session manager")

        # Timing instrumentation: measure both the wait to acquire the shared
        # `_bg` session (queue contention behind other `_bg` consumers like
        # chat_nav link-preview) and the LLM turn itself. Logged at DEBUG:
        # silent in normal operation, surfaced only when log_level is raised
        # to investigate a consolidation stall.
        t_start = _time.monotonic()
        async with contextlib.AsyncExitStack() as stack:
            try:
                client = await stack.enter_async_context(
                    background_turn(self._sessions, task="consolidation", agent="kirocrew-lite")
                )
            except Exception as exc:
                self._logger.warning(
                    "Consolidation could not acquire the background session "
                    "after %.1fs — nothing was sent",
                    _time.monotonic() - t_start,
                    exc_info=True,
                )
                raise _ConsolidationNotDispatched("background session unavailable") from exc
            t_acquired = _time.monotonic()
            wait_s = t_acquired - t_start
            # Reject all tools: this is a text/JSON-only generation turn. kiro
            # scopes the kirocrew-lite session to tools:[] via set_mode, but the
            # Claude Code backend skips set_mode and injects the full
            # kirocrew-core/cron toolset — without REJECT_ALL a background
            # consolidation turn could fire side-effecting tools (send_message,
            # learn_add, spawn_run). REJECT_ALL keeps both providers tool-free.
            try:
                result = await _facade_stream_and_collect_json(
                    client,
                    prompt,
                    approval_policy=ToolApprovalPolicy.REJECT_ALL,
                    model_fallback=True,
                )
            except Exception:
                self._logger.warning(
                    "LLM consolidation turn failed after %.1fs",
                    _time.monotonic() - t_start,
                    exc_info=True,
                )
                return None
            turn_s = _time.monotonic() - t_acquired
            self._logger.debug(
                "Consolidation LLM turn: wait=%.1fs turn=%.1fs total=%.1fs ok=%s",
                wait_s,
                turn_s,
                _time.monotonic() - t_start,
                result is not None,
            )
            return result
        # Reached only if the exit stack suppresses an exception. The prompt was
        # already sent by then, so the turn may have been billed: report it as a
        # spent-but-unusable result rather than a non-dispatch, which would hand
        # the caller a free retry it has not earned.
        return None
