"""History consolidation and auto-skill extraction.

The transcript facade owns persistence and locking.  This module is the import
path of the asynchronous consolidation workflow. The ``kiro_crew.history`` seams
that tests and embedding applications replace at runtime (``sel``,
``stream_and_collect``, ``stream_and_collect_json``, ``metadata_dedupe_verdict``)
are read at call time by the ``_facade_*`` helpers in its owners.

``HistoryConsolidator`` is defined here with its constructor, live reconfiguration,
retry accounting and every scheduling entry point. The stages of one pass live in
sibling owners, and the class binds each of their methods under its own name:

* ``history_consolidation_span`` -- which transcript span a pass prompts and how its
  attempt ends;
* ``history_consolidation_lease`` -- the cross-process lease that excludes a second
  pass over one session, and the fence every durable write of a pass takes on it;
* ``history_consolidation_memory`` -- the memory a pass reads into its prompt and writes
  back;
* ``history_consolidation_skills`` -- auto-skill detection, dedupe, staging and
  publication;
* ``history_consolidation_pass`` -- one pass end to end and the LLM turn it runs on.

This module stays the import path and the patch surface. It installs
:mod:`kiro_crew._facade_mirror`, so a patch of ``kiro_crew.history_consolidation.<name>``
reaches every owner that reads the name. A moved method is not a module name: patch it on
the class (``patch.object(HistoryConsolidator, "_consolidate", ...)``), as before the
split; a module-level patch of its name raises. An owner never imports this module at run time
and imports only the owners listed before it in ``_OWNERS``. New code for a stage goes to
that stage's owner; a new ``HistoryConsolidator`` method whose body lives in an owner gets
one binding line in the class, under that owner's header.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import importlib as _importlib
import json
import logging
import math
import os
import re
import sys as _sys
import time as _time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew import _facade_mirror
from kiro_crew import history_consolidation_lease as _lease
from kiro_crew import history_consolidation_memory as _memory
from kiro_crew import history_consolidation_pass as _pass
from kiro_crew import history_consolidation_skills as _skills
from kiro_crew import platform_compat
from kiro_crew._facade_bindings import own_methods
from kiro_crew.config import live
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.frontmatter import SKILL_UPDATE, frontmatter_value
from kiro_crew.history_consolidation_skills import _session_touched_sensitive
from kiro_crew.history_consolidation_span import (
    AttemptedSpan,
    _LeaseLostMidRun,
    _no_pass_ran,
    _persistence_disabled,
    _PersistenceDisabledMidRun,
    _RunCommitState,
)
from kiro_crew.history_projection import DISPLAY_ONLY_ROLES
from kiro_crew.image_refs import strip_image_refs
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_INSTRUCTION,
    LESSON_APPLIES_ON_TOPIC,
    authored_lesson_applies,
    extracted_lesson_applies,
)
from kiro_crew.llm_helpers import (
    ToolApprovalPolicy,
    background_turn,
)
from kiro_crew.project_scope import scope_is_admissible
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.skills import AUTO_SKILL_MAX_PROCEDURE_CHARS, AutoSkillProvenance, ClaimRefusal
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
    _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
)
from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

if TYPE_CHECKING:
    from kiro_crew.history import CacheIdentity, ConversationLog
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
# Seeded sessions (restored from disk after a restart) examined per idle sweep:
# a large backlog drains a few per heartbeat instead of all at once.
_SEEDED_PER_SWEEP = 3
# Oldest transcript the restart seed backfills: older stranded work is left to a
# manual ``kirocrew consolidate``, so one boot cannot bill a long-idle backlog.
_SEED_MAX_AGE_SECS = 7 * 86400


class _CountClaim:
    """One transcript's off-loop read, held from claim until its thread is done."""

    __slots__ = ("name", "reader")

    def __init__(self, name: str) -> None:
        self.name = name
        #: The worker-thread read; the claim only needs to know when it is done.
        self.reader: asyncio.Future[Any] | None = None


#: The owner modules whose functions :class:`HistoryConsolidator` binds as its members.
_MEMBER_OWNERS = frozenset(
    {
        "kiro_crew.history_consolidation_lease",
        "kiro_crew.history_consolidation_memory",
        "kiro_crew.history_consolidation_pass",
        "kiro_crew.history_consolidation_skills",
    }
)


class HistoryConsolidator:
    """Summarize old messages into structured memory via LLM.

    Two consolidation paths:
    - Preferences/projects: triggered by message count (30 messages)
    - Daily history: triggered by idle time (3h default) or end of day
    """

    # ── One consolidation pass (history_consolidation_pass) ──
    _consolidate = _pass._consolidate
    _call_llm = _pass._call_llm

    # ── Auto-skill extraction (history_consolidation_skills) ──
    _run_skill_detection = _skills._run_skill_detection
    _dedupe_candidate = _skills._dedupe_candidate
    _dedupe_judge = _skills._dedupe_judge
    _merge_skill_update = _skills._merge_skill_update
    _skill_publication_guard = _skills._skill_publication_guard
    _stage_skill_update = _skills._stage_skill_update
    _process_auto_skills = _skills._process_auto_skills

    # ── Cross-process consolidation lease (history_consolidation_lease) ──
    _acquire_consolidation_lease = _lease._acquire_consolidation_lease
    _renew_consolidation_lease = _lease._renew_consolidation_lease
    _heartbeat_consolidation_lease = _lease._heartbeat_consolidation_lease
    _release_consolidation_lease = _lease._release_consolidation_lease
    _lease_is_current = _lease._lease_is_current
    _lease_fence = _lease._lease_fence
    _advance_history_marker_now = _lease._advance_history_marker_now
    _advance_history_marker_fenced = _lease._advance_history_marker_fenced
    _mark_consolidated_fenced = _lease._mark_consolidated_fenced

    # ── Memory a pass reads and writes (history_consolidation_memory) ──
    _gated_lesson_scope = _memory._gated_lesson_scope
    _lesson_tier = _memory._lesson_tier
    _lesson_delete_decision = _memory._lesson_delete_decision
    _save_lessons = _memory._save_lessons
    _write_structured_memory = _memory._write_structured_memory

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
        # Transcript files whose rows are being counted off the loop, keyed by
        # file name like _busy so alias spellings of one transcript share one
        # claim. See _claim_count and _release_count.
        self._counting: dict[str, _CountClaim] = {}
        # Called as ``on_abandoned(key, message_count, reason, char_count=0,
        # last_slice=False, retired=False)`` when a span is abandoned, so the
        # owner can tell the user. ``message_count`` is the whole messages
        # given up; ``char_count`` is non-zero only when the abandon dropped ONE
        # budget-sized slice of an over-budget head message (sub-chunking)
        # rather than whole messages, so the notice can name the size of a
        # partial-message loss. ``last_slice`` is True when that dropped slice
        # was the message's final one (the marker has moved past the whole
        # message, so there is no next slice to resume from). ``retired`` is True
        # when the span was dropped DELIBERATELY by the per-head slice ceiling (a
        # cost cap) rather than after repeated failures, so the notice can avoid
        # failure/underlying-error wording. Without the callback the only trace
        # of the dropped span is a WARNING in a rotating log. Set by the gateway
        # once its notification surface exists; None skips it.
        self.on_abandoned: Callable[..., None] | None = None
        # Track last activity per session for idle-based history consolidation
        self._last_activity: dict[str, float] = {}
        # ``_last_activity`` lives in memory only, so after a restart a session
        # nobody touches again would never be idle-checked. The first idle sweep
        # seeds it once from the transcripts on disk (see _seed_last_activity).
        self._activity_seeded = False
        self._seeded_keys: set[str] = set()
        self._seed_task: "asyncio.Task[None] | None" = None
        self._history_consolidated: dict[str, float] = {}  # key → last history consolidation time
        # key → the transcript stamp at its latest idle read that found nothing left to
        # consolidate; check_idle_sessions skips the key while the stamp is unchanged.
        # _forget_key drops it with the key, so it never holds more than _last_activity.
        self._idle_clean: dict[str, tuple[CacheIdentity, int]] = {}
        # Separate offset for prefs-only consolidation (doesn't advance main offset)
        self._prefs_offset: dict[str, int] = {}
        # Session length at the last skill-detection pass, so an unchanged
        # (rotation_generation, message_count) at the last skill-detection
        # pass, so an unchanged session isn't re-judged on every history
        # consolidation — while a rotation (which bumps the generation and
        # swaps the window's content) still forces a fresh pass.
        self._last_skillgen_marker: dict[str, tuple[int, int]] = {}
        # Every tunable above is a copy of skills.* / memory.* config, so a write to
        # config.json reaches them only through reconfigure(). Held on self because
        # the watcher holds the owner weakly.
        self._config_sub = live.watch_object(
            self,
            "skills",
            "memory.history_idle_hours",
            "memory.migrated",
            name="HistoryConsolidator",
        )

    def reconfigure(self, cfg: object) -> None:
        """Push the live ``skills.*`` and consolidation settings onto this instance.

        These only ever gate the NEXT consolidation pass or the next auto-skill
        judgement, so swapping them mid-flight cannot corrupt work already running:
        a pass that has already read a threshold finishes on the old value and the
        next one uses the new one.
        """
        skills = getattr(cfg, "skills")
        memory = getattr(cfg, "memory")
        self._history_idle_secs = float(getattr(memory, "history_idle_hours")) * 3600
        self._migrated = bool(getattr(memory, "migrated"))
        self._auto_skills_enabled = bool(getattr(skills, "auto_create_from_sessions"))
        self._auto_refine_enabled = bool(getattr(skills, "auto_refine_on_deviation"))
        self._auto_min_tool_calls = int(getattr(skills, "auto_min_tool_calls"))
        self._auto_similarity_threshold = float(getattr(skills, "auto_similarity_threshold"))
        self._approval_required = bool(getattr(skills, "approval_required"))
        self._max_auto_skills = int(getattr(skills, "max_auto_skills"))
        self._stale_after_days = int(getattr(skills, "stale_after_days"))
        self._archive_after_days = int(getattr(skills, "archive_after_days"))
        self._generate_scripts = bool(getattr(skills, "generate_scripts"))
        self._judge_model = str(getattr(skills, "judge_model"))

    @property
    def _logger(self) -> logging.Logger:
        """Keep the pre-extraction ``kiro_crew.history`` logger category."""
        return _HISTORY_LOGGER

    @contextlib.contextmanager
    def _publication_hold_checked(self, key: str, commit_state: _RunCommitState | None = None):
        """Acquire one publication hold and gate the run's first commit."""
        state = commit_state or _RunCommitState()
        with self._log.publication_hold(key):
            # The hold's transcript lock also fences the write on the run's
            # lease: the token is re-read under that lock, so a pass whose
            # renewals failed past the ceiling while a peer claimed the span
            # refuses the write instead of doubling it.
            if state.lease_token is not None and not self._lease_is_current(key, state.lease_token):
                raise _LeaseLostMidRun(key)
            if not state.committed and _persistence_disabled():
                raise _PersistenceDisabledMidRun
            yield state

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

    def _inside_retry_deadline(self, key: str, now: float | None = None) -> bool:
        """True while *key*'s backoff deadline is still ahead: one metadata read.

        The scheduling short-circuit the turn check, the idle sweep and the
        worker-thread count share before any count runs. It reads only the
        deadline, which survives a new span (the cap may not), so it never
        refuses a key that :meth:`retry_eligible` with the counted total would
        admit. The gate inside ``_consolidate`` remains the enforcement.
        """
        return (_time.time() if now is None else now) < self._log.consolidation_retry_state(key)[1]

    async def _advance_history_marker(
        self, key: str, span: AttemptedSpan, lease: str | None = None
    ) -> None:
        """Make durable the progress a finished history span represents.

        The one place that decides, from the span alone, whether a pass advanced
        the whole-message ``last_consolidated`` marker or only the sub-message
        offset INSIDE an over-budget head — used identically on the success path
        and the abandon-at-cap path, since both end a slice and hand the next
        pass the slice after it.

        A span is a NON-FINAL sub-slice when its prompt advanced the sub-offset
        (``sub_prompted > sub_offset``) but did not reach the end of the head
        (``prompted`` still at ``offset``, i.e. the message marker was told not to
        move). That case advances the durable sub-offset to ``sub_prompted`` so
        the next pass resumes at the next slice; every other case — a final slice
        (``prompted`` moved past the whole chunk prefix, head plus any
        display-only rows carried with it) or an ordinary message-aligned
        chunk — advances ``last_consolidated`` to ``prompted`` and clears the
        sub-offset. Both run off the event loop (blocking file IO).

        *lease* fences the write on this pass's acquisition token (see
        ``_lease_fence``); a lost lease raises ``_LeaseLostMidRun``.
        """
        await asyncio.to_thread(self._advance_history_marker_fenced, key, span, lease)

    async def _note_failed_attempt(
        self, key: str, span: AttemptedSpan, reason: str, lease: str | None = None
    ) -> bool:
        """Charge one attempt for a billed turn that never reached the marker.

        Called only once the prompt has actually reached the provider, so a
        pre-dispatch failure (no session manager, kiro-cli failing to start) and a
        cheap pre-call failure (snapshot, metadata read) both keep their free
        retry. At the attempt cap the durable marker is written anyway and the span
        is abandoned with a warning: the alternative is re-billing this failure
        indefinitely.

        *span* is the pre-turn snapshot identity (see :class:`AttemptedSpan`), used
        both to stamp the charge and to place the abandon marker, so the marker
        cannot be written for a span other than the one the cap was reached on.
        The abandon marker lands at ``span.prompted``, NOT ``span.total``: only
        the prompted prefix was ever put in front of a provider, and marking the
        tail behind it would retire messages no model has read. That tail is left
        unconsolidated and is picked up by the next pass, which charges its own
        attempts against it.

        *lease* fences the charge AND the abandon marker on this pass's
        acquisition token, inside ONE transcript-lock hold. A stale holder whose
        lease a peer took over must not charge the peer's span: a fifth charge
        recorded before the ownership check would leave the span at the cap
        with the abandon marker refused, and ``retry_eligible`` would then reject
        the peer's span even after its backoff expired. Holding the fence across
        both writes also means no takeover can land between the charge and the
        marker. Returns ``False`` when the lease was lost and nothing was
        charged (the caller reports the pass as refused), ``True`` otherwise —
        including when the bookkeeping itself failed, which is only logged.
        """

        def _charge() -> tuple[int, float, Exception | None]:
            with self._lease_fence(key, lease):
                attempts, retry_at = self._log.record_consolidation_failure(
                    key,
                    _CONSOLIDATION_BACKOFF_BASE_SECS,
                    _CONSOLIDATION_BACKOFF_MAX_SECS,
                    span,
                )
                marker_error: Exception | None = None
                if attempts >= _CONSOLIDATION_MAX_ATTEMPTS:
                    try:
                        self._advance_history_marker_now(key, span)
                    except Exception as exc:  # reported below, with the abandon
                        marker_error = exc
                return attempts, retry_at, marker_error

        try:
            attempts, retry_at, marker_error = await asyncio.to_thread(_charge)
        except _LeaseLostMidRun:
            # A peer owns this span now: the charge and any abandon marker are
            # its to write. Nothing is recorded and no abandon notice fires.
            self._logger.warning(
                "Not charging the failed consolidation for %s (%s): another process "
                "holds the lease",
                key,
                reason,
            )
            return False
        except Exception:
            # Without a persisted count the sweep cannot back off, so say so
            # loudly — but never let bookkeeping mask the original failure.
            self._logger.warning(
                "Could not persist consolidation retry state for %s", key, exc_info=True
            )
            return True
        if attempts < 1:
            # The session was deleted mid-consolidation, so nothing was recorded
            # and there is no span left to abandon.
            return True
        if attempts < _CONSOLIDATION_MAX_ATTEMPTS:
            self._logger.warning(
                "Consolidation attempt %d/%d failed for %s (%s); " "next attempt in %.0fs",
                attempts,
                _CONSOLIDATION_MAX_ATTEMPTS,
                key,
                reason,
                max(0.0, retry_at - _time.time()),
            )
            return True
        # A non-final sub-slice abandon drops ONE budget-sized slice of an
        # over-budget head, not whole messages: the message marker stays put and
        # the next pass tries the next slice. Report the slice's character span so
        # the warning and the user notice name the real size of the loss rather
        # than "0 messages".
        # A sub-slice abandon drops ONE budget-sized slice of an over-budget
        # head, not whole messages. It is identified by the slice having prompted
        # some of the head's content (``sub_prompted > sub_offset``) — true for
        # the final slice as much as a non-final one. Reading ``prompted <=
        # offset`` as well would exclude the FINAL slice (whose ``prompted``
        # advances past the whole chunk prefix), so abandoning the last slice of
        # an over-budget head would report ``0`` characters and the user notice
        # would miscount it as whole messages even though earlier slices were
        # extracted. Report the slice's character span whenever any was prompted.
        is_sub_slice_abandon = span.sub_prompted > span.sub_offset
        slice_chars = span.sub_prompted - span.sub_offset if is_sub_slice_abandon else 0
        # The FINAL slice of an over-budget head advances the marker past the
        # whole chunk prefix (``prompted > offset``), so there is no "next
        # slice" to resume from — the message is now fully consolidated. A
        # non-final slice leaves the marker put (``prompted == offset``) and the
        # next pass continues from the slice after this one. Only the latter may
        # say so.
        is_last_slice = is_sub_slice_abandon and span.prompted > span.offset
        if is_sub_slice_abandon and not is_last_slice:
            self._logger.warning(
                "Abandoning consolidation for %s after %d failed attempts (%s): "
                "giving up one %d-character slice of an over-budget message "
                "WITHOUT a memory pass; the next pass continues from the slice "
                "after it",
                key,
                attempts,
                reason,
                slice_chars,
            )
        elif is_sub_slice_abandon:
            self._logger.warning(
                "Abandoning consolidation for %s after %d failed attempts (%s): "
                "giving up the last %d-character slice of an over-budget message "
                "WITHOUT a memory pass; the message is now fully consolidated",
                key,
                attempts,
                reason,
                slice_chars,
            )
        else:
            self._logger.warning(
                "Abandoning consolidation for %s after %d failed attempts (%s): "
                "marking %d messages consolidated WITHOUT a memory pass, so this "
                "span's history/preferences/lessons are not extracted",
                key,
                attempts,
                reason,
                span.prompted - span.offset,
            )
        if marker_error is not None:
            # The count stays at the cap, so retry_eligible() keeps refusing —
            # the span stops spending even though the marker is missing.
            self._logger.warning(
                "Could not mark abandoned consolidation for %s",
                key,
                exc_info=(type(marker_error), marker_error, marker_error.__traceback__),
            )
        callback = self.on_abandoned
        if callback is not None:
            try:
                callback(
                    key,
                    span.prompted - span.offset,
                    reason,
                    char_count=slice_chars,
                    last_slice=is_last_slice,
                )
            except Exception:
                self._logger.debug(
                    "Abandoned-consolidation callback failed for %s", key, exc_info=True
                )
        return True

    async def _note_environment_failure(self, key: str, reason: str) -> None:
        """Arm the backoff for a consolidation turn that spent nothing.

        Deliberately does NOT touch the attempt cap. A pre-dispatch failure, or a
        turn the provider sent nothing back for and billed nothing, spends
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

    def _busy(self, key: str) -> bool:
        """True when *key*, or any other key naming the same transcript file, is running."""
        target = self._log._path(key).name
        return any(self._log._path(running).name == target for running in self._running)

    def _transcript_stamp(self, key: str) -> tuple[CacheIdentity, int] | None:
        """Return *key*'s transcript stamp from a stat with no read, or ``None``.

        The pair ``_read_messages`` checks before it serves a cached parse: the file's
        stat identity (mtime, ctime, size and inode, so a rewrite that restores the
        mtime still shows) and the key's invalidation generation. While it is unchanged
        a read would return the same rows. ``None`` when the file cannot be stat'ed, so
        the caller reads instead of skipping. A transcript that does not exist raises
        ``FileNotFoundError``: the session was deleted, which is not a failed stat.
        """
        try:
            identity = self._log._cache_identity(self._log._path(key).stat())
        except FileNotFoundError:
            raise
        except OSError:
            return None
        return identity, self._log._cache_gen(key)

    def _forget_key(self, key: str) -> None:
        """Stop tracking *key* in the idle sweep, dropping every record held for it."""
        self._last_activity.pop(key, None)
        self._seeded_keys.discard(key)
        self._history_consolidated.pop(key, None)
        self._idle_clean.pop(key, None)

    def _scan_unconsolidated_transcripts(self) -> dict[str, float]:
        """Map each transcript's LIVE session key to its file mtime, for unconsolidated tails.

        Blocking file IO: run off the event loop. The live key, never the filename
        stem: ``_consolidate`` keys its member-memory receipt on the session key,
        so a stem would miss a receipt committed before the restart and publish
        the span twice. A stem whose live key cannot be recovered exactly (the
        ``:`` fold is not reversible) is skipped. Only persistent transcripts qualify: ``_consolidate`` refuses every other
        mode, and a refusal sets no throttle, so a private one would be
        re-dispatched on every sweep. A thread's privacy can live only in the
        session map (its header stamp may have failed), so a map-flagged
        transcript is skipped too, and with no map to ask nothing is seeded.
        """
        from kiro_crew.messaging.privacy_mode import conv_state_map  # deferred like its own

        found: dict[str, float] = {}
        session_map = conv_state_map(self._sessions)
        if session_map is None:
            return found
        private = {self._log._path(key).stem for key in session_map.privacy_flagged_entries()}
        for path in sorted(self._log._dir.glob("*.jsonl")):
            if path.is_symlink() or path.stem.startswith(("memory-consolidation", "subagent_")):
                continue
            if path.stem in private:
                continue
            stem = path.stem
            key = (
                "dashboard:" + stem[len("dashboard_") :]
                if stem.startswith("dashboard_")
                else session_map.channel_key_for_stem(stem)
            )
            try:
                if not key or self._log._path(key).name != path.name:
                    continue
                mtime = path.stat().st_mtime
                if mtime < _time.time() - _SEED_MAX_AGE_SECS:
                    continue
                mode = self._log.get_metadata(key).get("memory_mode") or "persistent"
                if mode == "persistent" and self._log.unconsolidated_count(key) > 0:
                    found[key] = mtime
            except Exception:
                _HISTORY_LOGGER.debug("idle seed skipped %s", path.name, exc_info=True)
        return found

    async def _seed_last_activity(self) -> None:
        """Seed ``_last_activity`` once from disk; a live key always wins."""
        from kiro_crew.history import _safe_key  # circular: history re-exports this module

        try:
            found = await asyncio.to_thread(self._scan_unconsolidated_transcripts)
        except Exception:
            _HISTORY_LOGGER.warning("idle seed scan failed", exc_info=True)
            return
        tracked = {_safe_key(key) for key in self._last_activity}
        for key, mtime in found.items():
            if _safe_key(key) not in tracked:
                self._last_activity[key] = mtime
                self._seeded_keys.add(key)

    def maybe_consolidate(self, key: str) -> None:
        """Fire preferences/projects consolidation if message threshold exceeded.

        Called on the event loop after every completed turn, so it never reads
        the transcript itself: the turn that just ended moved the file, the
        message cache misses, and counting rows there would parse the whole
        file on the loop. The count runs in a worker thread through
        :meth:`_count_off_loop`, one at a time per transcript. A turn that ends
        while a count is in flight starts nothing: the next turn's count
        includes its rows, and the idle and session-end passes extract
        preferences and projects from the same tail.
        """
        self._last_activity[key] = _time.time()
        self._seeded_keys.discard(key)  # a key with live activity is not a restored seed
        if _persistence_disabled():
            return
        if self._busy(key):
            return
        # A key inside its retry deadline would be refused after the count, so
        # it schedules nothing: one metadata read on the loop, as the idle sweep
        # does. _count_rows repeats the check for a deadline armed meanwhile.
        if self._inside_retry_deadline(key):
            return
        claim = self._claim_count(key)
        if claim is not None:
            self._start_counted(claim, self._threshold_pass(key, claim))

    async def _threshold_pass(self, key: str, claim: _CountClaim) -> None:
        """Count *key*'s rows off the loop, then run a prefs pass at the threshold."""
        try:
            counted = await self._count_off_loop(key, claim, floor=self._prefs_offset.get(key, 0))
            if counted is None:
                return
            total = counted[0]
            # Re-checked after the await: an idle or session-end pass may have
            # claimed this transcript, or finished one, while the count ran.
            if self._busy(key):
                return
            if total - self._prefs_offset.get(key, 0) < _CONSOLIDATION_THRESHOLD:
                return
            # Cheap pre-check mirroring the other automatic entry points. During
            # a backoff window every message past the threshold would otherwise
            # start a pass whose snapshot takes the per-file lock (the same one
            # appends contend on) and reads the transcript, only to be refused by
            # the gate inside _consolidate(). retry_eligible costs one
            # metadata-line read and no transcript read; the inner gate remains
            # the enforcement backstop.
            if not self.retry_eligible(key, message_count=total):
                return
            # No await between the busy check above and this claim.
            self._running.add(key)
        finally:
            self._release_count(claim)
        try:
            result = await self._consolidate(key, include_history=False)
        except Exception:
            # _consolidate has already logged it and released its claim; a
            # failed pass leaves the offset alone so the window is retried.
            return
        # A refusal ran no pass over the window. Advancing the offset anyway
        # would mark the window consolidated, so once the backoff expires the
        # threshold test skips it until a whole new threshold of messages
        # accumulates — silently dropping its preference/project extraction.
        # A cancellation propagates and leaves the offset alone too.
        if not _no_pass_ran(result):
            self._prefs_offset[key] = total

    def _claim_count(self, key: str) -> _CountClaim | None:
        """Claim *key*'s transcript for one off-loop count; None when one is in flight."""
        name = self._log._path(key).name
        if name in self._counting:
            return None
        claim = self._counting[name] = _CountClaim(name)
        return claim

    def _start_counted(self, claim: _CountClaim, work: Awaitable[None]) -> None:
        """Run *work*, which owns *claim*, as a tracked task."""
        t = asyncio.ensure_future(work)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        # A task cancelled before its first step never reaches its own release.
        t.add_done_callback(lambda _t: self._release_count(claim))

    def _release_count(self, claim: _CountClaim) -> None:
        """Drop *claim* now, or once its worker thread stops reading.

        A cancelled caller cannot stop the thread, so the claim stays until the
        thread is done; otherwise the next turn would start a second read of the
        same file beside it. Compared by identity, so a late release never drops
        a newer claim on the same transcript.
        """
        if self._counting.get(claim.name) is not claim:
            return
        if claim.reader is not None and not claim.reader.done():
            claim.reader.add_done_callback(lambda _f: self._release_count(claim))
            return
        del self._counting[claim.name]

    async def _count_off_loop(
        self, key: str, claim: _CountClaim, *, floor: int | None
    ) -> tuple[int, int] | None:
        """``(total, total - floor)`` for *key*, counted in a worker thread.

        The one row count the per-turn check and the idle sweep share. It runs
        in a worker thread, shielded: cancelling the caller leaves the thread
        to finish, and *claim* covers it until then. *floor* None means the
        transcript's consolidated offset. None means nothing was counted: the
        key is inside its retry deadline, or the read failed (logged as a
        warning), so the check is skipped.
        """
        claim.reader = asyncio.ensure_future(asyncio.to_thread(self._count_rows, key, floor))
        try:
            return await asyncio.shield(claim.reader)
        except Exception:
            self._logger.warning(
                "Could not count messages for %s; skipping this check", key, exc_info=True
            )
            return None

    def _count_rows(self, key: str, floor: int | None) -> tuple[int, int] | None:
        """Worker thread: count *key*'s rows one line at a time.

        A key inside its retry deadline is refused by ``retry_eligible`` after
        the count anyway, so it is not counted. Both callers skip such a key on
        the loop first; this repeats the check for a deadline armed after that
        and before the thread ran. The count is
        ``ConversationLog._count_messages``, the streaming count that shares
        ``message_row`` with the full read, so it materializes nothing and
        holds the GIL for one line at a time. It never takes the per-file
        writer lock, so an event-loop writer never waits out the count.
        """
        if self._inside_retry_deadline(key):
            return None
        if floor is None:
            floor = self._log.consolidated_offset(key)
        total = self._log._count_messages(key, hold_writer_lock=False)
        return total, max(0, total - floor)

    async def _idle_pass(
        self, key: str, claim: _CountClaim, now: float, stamp: tuple[CacheIdentity, int] | None
    ) -> None:
        """Count an idle *key* off the loop, then run its history pass if one is due.

        *stamp* is the transcript stamp taken before the count. A count that finds
        nothing left records it, so later sweeps skip the key until a row moves
        it. A count that finds rows drops it. A skipped count (retry deadline,
        failed read) records nothing, so the next sweep counts again.
        """
        try:
            counted = await self._count_off_loop(key, claim, floor=None)
            if counted is None:
                return
            total, unconsolidated = counted
            if stamp is not None:
                if unconsolidated < 1:
                    self._idle_clean[key] = stamp
                else:
                    self._idle_clean.pop(key, None)
            if (
                unconsolidated < 1
                or now - self._history_consolidated.get(key, 0) < self._history_idle_secs
                or self._busy(key)
                # Durable backoff with the counted total, so a span that grew
                # past its charged extent gets its fresh budget. The in-memory
                # throttle is set only when a pass ends without an exception and
                # is lost on restart, so it alone cannot stop a repeatedly
                # failing span from re-billing an LLM turn every tick.
                or not self.retry_eligible(key, now, message_count=total)
            ):
                return
            self._running.add(key)
        finally:
            self._release_count(claim)
        try:
            result = await self._consolidate(key, include_history=True)
        except Exception:
            return  # logged by _consolidate; no throttle, so the next sweep retries
        # A refusal is not a completed pass; setting the throttle for it would
        # delay the retry past the backoff deadline.
        if not _no_pass_ran(result):
            self._history_consolidated[key] = now

    def check_idle_sessions(self) -> None:
        """Check all tracked sessions for idle-based history consolidation."""
        if _persistence_disabled():
            return
        if not self._activity_seeded:
            with contextlib.suppress(RuntimeError):  # no running loop: seed later
                self._seed_task = asyncio.get_running_loop().create_task(self._seed_last_activity())
                self._activity_seeded = True
        now = _time.time()
        seeded_budget = _SEEDED_PER_SWEEP
        for key, last in list(self._last_activity.items()):
            if now - last < self._history_idle_secs:
                continue
            seeded = key in self._seeded_keys
            if seeded:
                if seeded_budget < 1:
                    continue
                seeded_budget -= 1
                # Re-queue at the end so a seed that keeps being skipped cannot starve the rest.
                self._last_activity[key] = self._last_activity.pop(key)
            # A key whose last count found nothing left is skipped while its transcript's
            # stamp is unchanged, so a finished session costs a stat, not a read, per
            # sweep. The stamp is taken here, before the off-loop count, so a row that
            # lands in between makes the next sweep count again. The key stays tracked:
            # turns that skip ``maybe_consolidate`` (monitor wakes, recoveries, cancelled
            # turns) and other processes append rows too, and the stamp sees all of them.
            # A key whose transcript is gone was deleted: it is forgotten once no pass
            # and no off-loop count for it is running, because each records its result
            # for the key when it ends.
            try:
                stamp = None if seeded else self._transcript_stamp(key)
            except FileNotFoundError:
                if not self._busy(key) and self._log._path(key).name not in self._counting:
                    self._forget_key(key)
                continue
            if stamp is not None and self._idle_clean.get(key) == stamp:
                continue
            throttled = now - self._history_consolidated.get(key, 0) < self._history_idle_secs
            if throttled or self._busy(key):
                continue
            if not seeded:
                # Never counted here on the loop: _idle_pass counts off the loop
                # and re-checks all of this. A key still inside its retry
                # deadline is skipped first, for one metadata read.
                if self._inside_retry_deadline(key, now):
                    continue
                claim = self._claim_count(key)
                if claim is not None:
                    self._start_counted(claim, self._idle_pass(key, claim, now, stamp))
                continue
            # A seed's transcript may be cold and large, so it is never read here:
            # ``_consolidate`` snapshots it off the loop and enforces the backoff.
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
                    and not _no_pass_ran(fut.result())
                ):
                    self._history_consolidated[k] = ts
                elif k in self._seeded_keys and not fut.cancelled() and fut.exception() is None:
                    self._history_consolidated[k] = ts  # refused seed: retried once per idle window

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
        if self._busy(key):
            return
        if _persistence_disabled():
            return
        # Idle expiry reaches this on the event loop long after the parsed
        # transcript cache has dropped the entry, so the eligibility reads below
        # are cold full parses. They run in _session_end_pass's worker thread;
        # the loop only pays for a stat and a metadata-line read.
        if not self._log._path(key).exists():
            return
        # This path consults no time-based throttle at all — every session expiry
        # for the same key fires a fresh consolidation — so the durable backoff
        # stands between a repeatedly failing span and one billed LLM turn per
        # expiry. A key inside its deadline is skipped here, before a task is
        # scheduled; _session_end_pass re-checks with the counted total.
        if self._inside_retry_deadline(key):
            self._logger.info(
                "consolidate_session skipped for %s: consolidation retry backoff", key
            )
            return
        # One off-loop read per transcript, shared with the idle sweep and the
        # per-turn check. A read already in flight belongs to one of those or to
        # an earlier session end; the idle sweep keeps the key tracked and
        # consolidates its history once the read has finished.
        claim = self._claim_count(key)
        if claim is None:
            self._logger.info("consolidate_session deferred for %s: transcript being read", key)
            return
        self._start_counted(claim, self._session_end_pass(key, claim))

    def _session_end_eligibility(self, key: str) -> tuple[int, int, bool]:
        """Worker thread: ``(total, unconsolidated, sensitive)`` for *key*.

        One cold parse at most: the sensitive scan reuses the entry the count
        just published. ``sensitive`` is only computed when there is a tail.
        """
        total, unconsolidated = self._log.consolidation_counts(key)
        if unconsolidated < 1:
            return total, unconsolidated, False
        return total, unconsolidated, _session_touched_sensitive(self._log._read_messages(key))

    async def _session_end_pass(self, key: str, claim: _CountClaim) -> None:
        """Check *key*'s eligibility off the loop, then run its history pass.

        The read is shielded and *claim* covers it until the thread stops, as
        in :meth:`_count_off_loop`.
        """
        try:
            claim.reader = asyncio.ensure_future(
                asyncio.to_thread(self._session_end_eligibility, key)
            )
            try:
                total, unconsolidated, sensitive = await asyncio.shield(claim.reader)
            except Exception:
                self._logger.warning(
                    "consolidate_session could not read %s; skipping", key, exc_info=True
                )
                return
            if unconsolidated < 1:
                return
            # Re-checked after the await: another entry point may have claimed
            # this transcript while the read ran.
            if self._busy(key):
                return
            if not self.retry_eligible(key, message_count=total):
                self._logger.info(
                    "consolidate_session skipped for %s: consolidation retry backoff", key
                )
                return
            if sensitive:
                self._logger.info("consolidate_session skipped for %s: sensitive session", key)
                return
            # No await between the busy check above and this claim.
            self._running.add(key)
        finally:
            self._release_count(claim)
        try:
            result = await self._consolidate(key, include_history=True)
        except Exception as exc:
            self._logger.warning("consolidate_session failed for %s: %s", key, exc)
            return
        finally:
            self._running.discard(key)
        # A refusal is not a completed pass; leave the throttle unset.
        if not _no_pass_ran(result):
            self._history_consolidated[key] = _time.time()

    async def consolidate_now(self, key: str) -> bool:
        """Consolidate a session synchronously (blocking), draining the tail.

        Unlike consolidate_session() which is fire-and-forget, this awaits
        completion. Used by the CLI command.

        Passes repeat until the tail is drained. One pass renders at most
        :data:`_CONSOLIDATION_PROMPT_BUDGET_CHARS` (see
        :func:`_consolidation_chunk`), and the CLI process exits when this
        returns — there is no idle sweep behind it to pick up a remainder the
        way there is for every in-gateway entry point. A single pass would
        therefore report a tail larger than the budget as fully consolidated
        while most of it was never read.

        The loop stops on the first pass that consolidates nothing, not only on
        an empty tail: a refusal, an unreadable transcript, or a span that the
        marker cannot advance over all leave the count where it was, and
        repeating them is an infinite loop rather than progress.

        Returns ``False`` when the first pass recorded no completed pass —
        retry backoff, another process holding the consolidation lease, or a
        source change after billing — so the CLI can report the skip instead of
        a false success, and ``True`` for every other outcome (including the
        nothing-to-do and sensitive-session skips, which were already reported
        as done). A partial drain that then stalls returns ``True``: work did
        happen, and the caller reports the remainder from its own count rather
        than from this flag.

        Safety: the sensitive-session check runs before the first pass and
        again before every later one, because a live session can append a
        sensitive tool event between passes and the drain would otherwise
        prompt a tail the first check never saw. It is not enforced inside
        _consolidate(): the idle sweep deliberately consolidates a sensitive
        session for memory. The consolidation retry backoff is re-checked in
        _consolidate(), and _run_skill_detection() re-checks the sensitive
        guard over its own window.
        """
        remaining = self._log.unconsolidated_count(key)
        if remaining < 1:
            return True
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            self._logger.info("consolidate_now skipped for %s: sensitive session", key)
            return True
        first_pass = True
        while remaining > 0:
            if not first_pass:
                # The drain is the one place a pass prompts a tail the pre-check
                # above never saw: a live session keeps appending between passes,
                # so the same whole-session check runs again before each later
                # pass. It is not moved into _consolidate, where the idle sweep
                # deliberately consolidates a sensitive session for memory and
                # suppresses only skill synthesis.
                messages = await asyncio.to_thread(self._log._read_messages, key)
                if _session_touched_sensitive(messages):
                    self._logger.info(
                        "consolidate_now stopped for %s: session turned sensitive mid-drain",
                        key,
                    )
                    return True
            outcome = await self._consolidate(key, include_history=True)
            if _no_pass_ran(outcome):
                return not first_pass
            after = self._log.unconsolidated_count(key)
            if after >= remaining:
                if after > 0:
                    self._logger.warning(
                        "consolidate_now made no progress on %s: %d message(s) "
                        "still unconsolidated",
                        key,
                        after,
                    )
                return True
            remaining = after
            first_pass = False
        return True


own_methods(HistoryConsolidator, _MEMBER_OWNERS)
#: BASE imports this module binds for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    AUTO_SKILL_MAX_PROCEDURE_CHARS,
    AutoSkillProvenance,
    ClaimRefusal,
    DISPLAY_ONLY_ROLES,
    Iterator,
    LESSON_APPLIES_INSTRUCTION,
    LESSON_APPLIES_ON_TOPIC,
    NamedTuple,
    SKILL_UPDATE,
    ToolApprovalPolicy,
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
    _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
    authored_lesson_applies,
    background_turn,
    copy,
    datetime,
    extracted_lesson_applies,
    frontmatter_value,
    hashlib,
    is_warm_task_brief_message,
    json,
    math,
    os,
    platform_compat,
    re,
    redact_credentials,
    redact_exfiltration_urls,
    run_in_embed_pool,
    scope_is_admissible,
    strip_image_refs,
    timezone,
    uuid,
    validate_skill_script,
)
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.history_consolidation_span",
    "kiro_crew.history_consolidation_lease",
    "kiro_crew.history_consolidation_memory",
    "kiro_crew.history_consolidation_skills",
    "kiro_crew.history_consolidation_pass",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
#: The one-module file's names: everything the family binds except the methods the class
#: binds from its owners, which were never module names, so a facade patch of one fails.
_SURFACE = frozenset().union(globals(), *(vars(_sys.modules[o]) for o in _OWNERS)) - {
    n
    for n, m in vars(HistoryConsolidator).items()
    if getattr(m, "__module__", None) in _MEMBER_OWNERS
}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=(), surface=_SURFACE).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.history_consolidation_lease import (  # noqa: F401
        _CONSOLIDATION_LEASE_CEILING_SECS,
        _CONSOLIDATION_LEASE_RENEW_SECS,
        _LEASE_INCARNATION,
        _LEASE_MONOTONIC,
        _LEASE_PID,
        _LEASE_START,
        _LEASE_TOKEN,
        _lease_holder_is_live,
    )
    from kiro_crew.history_consolidation_memory import (  # noqa: F401
        _EMBED_BUDGET_SECS_PER_PASS,
        _INHERIT_GLOBAL,
        _LESSON_PASSTHROUGH_FIELDS,
        _PLACEHOLDER_BODIES,
        _SEMANTIC_OMISSION_NOTICE,
        _SEMANTIC_SNAPSHOT_PAGE,
        _bounded_semantic_table,
        _BoundedTable,
        _EmbedBudget,
        _InheritGlobal,
        _is_plausible_memory_file,
        _LessonDeleteDecision,
        _recency,
        _reroute_lesson_semantic_keys,
        _semantic_snapshot_in_pages,
        _session_facets,
        _withhold_unseen_deletes,
    )
    from kiro_crew.history_consolidation_pass import (  # noqa: F401
        _facade_stream_and_collect_json,
    )
    from kiro_crew.history_consolidation_skills import (  # noqa: F401
        _SENSITIVE_TOOL_PATTERNS,
        _SKILL_DETECTION_WINDOW,
        _TOOL_ROLES,
        _count_tool_call_messages,
        _facade_metadata_dedupe_verdict,
        _facade_sel,
        _facade_stream_and_collect,
        _frontmatter_value,
        _merge_trigger_lists,
        _strip_code_fence,
        _strip_skill_frontmatter,
    )
    from kiro_crew.history_consolidation_span import (  # noqa: F401
        _CONSOLIDATION_BUSY,
        _CONSOLIDATION_MAX_HEAD_SLICES,
        _CONSOLIDATION_META_KEYS,
        _CONSOLIDATION_PROMPT_BUDGET_CHARS,
        _CONSOLIDATION_REFUSED,
        _CONSOLIDATION_SUBOFFSET_META_KEYS,
        _consolidation_chunk,
        _consolidation_head_content,
        _ConsolidationNotDispatched,
        _ConsolidationRefusedSentinel,
        _ConsolidationReplyUnusable,
        _fmt_message,
        _head_exceeds_budget,
        _head_slice_ceiling_reached,
        _lesson_extraction_disabled,
        _prompt_rows,
        _slice_boundary,
        _slice_head_for_budget,
    )
