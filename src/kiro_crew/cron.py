"""Cron service for scheduling agent tasks.

Jobs are stored in the config directory (``~/.kiro/crew/crons.json`` by default,
overridden by ``KIROCREW_HOME``) and executed by a background
asyncio timer.  Each job fires a callback (typically delivering the result to
the dashboard and, when configured, the owner's Slack DM).

Cross-process safety: the CLI and gateway run as separate processes sharing
the same ``crons.json``.  All read-modify-write cycles use advisory file
locking (fcntl), and a content-digest ``_sync()`` detects external file changes
before every mutation.  Job execution releases the lock so long-running jobs
don't block the CLI.

Jobs are created via MCP tools (``cron_add``) or the CLI (``kirocrew cron add``).

Supports three schedule types:
- ``every`` — recurring interval (min 60s)
- ``at`` — one-shot at a unix timestamp
- ``cron`` — standard cron expression (min hour dom month dow)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

try:
    from cron_descriptor import Options, get_description  # type: ignore[import-untyped]
except ImportError:
    Options = None  # type: ignore[assignment,misc]
    get_description = None  # type: ignore[assignment]
from croniter import croniter  # type: ignore[import-untyped]

from kiro_crew import cron_script, platform_compat, sel, shutdown_event
from kiro_crew.config.loader import KiroCrewConfig, config_dir, data_home
from kiro_crew.constants import env_flag_enabled
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
from kiro_crew.executors import subprocess_executor
from kiro_crew.resource_status import admission_check
from kiro_crew.validation import MAX_CRON_MESSAGE

logger = logging.getLogger(__name__)

# ── Constants ──

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_DEFAULT_DIR: Path | None = None


def _default_dir() -> Path:
    """Cron data directory, resolved against the live data home."""
    return _DEFAULT_DIR if _DEFAULT_DIR is not None else data_home()


_CRONS_FILE = "crons.json"

# ``$skill`` token pattern (mirrors skills._DOLLAR_SKILL_PATTERN; duplicated
# here to avoid a cron<->skills import cycle).
_SKILL_TOKEN_RE = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)")


def referenced_skill_names() -> set[str]:
    """Skill slugs referenced via ``$skill`` tokens in any cron job's message.

    Read-only + best-effort: reads ``crons.json`` directly (so it needs no
    running scheduler) and returns an empty set on any error. The skill
    lifecycle uses this to exempt cron-referenced skills from eviction — a job
    that says ``$deploy-helper`` keeps ``auto/deploy-helper`` from being
    archived out from under it. Returns both the raw token and its last path
    segment so callers can match either a full key or a bare slug.
    """
    out: set[str] = set()
    try:
        path = config_dir() / _CRONS_FILE
        if not path.exists():
            return out
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            msg = j.get("message") or ""
            for m in _SKILL_TOKEN_RE.finditer(msg):
                tok = m.group(1)
                if any(c.isalpha() for c in tok):
                    out.add(tok)
                    out.add(tok.split("/")[-1])
    except Exception:
        return set()
    return out


_STORE_VERSION = 2
_MIN_INTERVAL_SECS = 60
_JOB_TIMEOUT_SECS = 1800  # 30 min per job
# Margin the per-wake budget must leave above a command/script subprocess
# timeout: the wake deadline cancels only the executor FUTURE (threads are
# not interruptible), so a budget shorter than the subprocess bound leaves
# the subprocess running while the guards clear and later wakes duplicate it.
_SUBPROC_CLEANUP_ALLOWANCE_SECS = 5
_TIMER_POLL_SECS = 30  # check for due cron-expr jobs
_AUTO_PAUSE_THRESHOLD = 5  # consecutive failures before a script/command cron auto-pauses
_REAPER_INTERVAL = 60  # seconds between reaper sweeps
_REAPER_RESET_TIMEOUT = 30.0  # max seconds for session reset in reaper
# Bound skip_date advancement by a WALL-CLOCK horizon rather than an iteration
# count. An iteration cap couples the bound to schedule granularity: sized for a
# weekly cron (old 52) it broke daily crons; re-sized for daily it would then
# break sub-daily (e.g. a */5 cron does 288 fires/day and would exhaust a
# daily-sized cap within days). Bounding by wall-clock time removes the coupling
# entirely — a daily cron and a */5 cron both simply look ~2 years ahead for the
# next non-skipped fire. A large absolute iteration ceiling remains ONLY as an
# anti-infinite-loop safety net for a pathological all-skipped sub-minute
# schedule; realistic skip_dates lists are short (hand-entered) and exit far
# sooner, so the horizon is the binding constraint in every practical case.
_MAX_SKIP_DATE_HORIZON_SECS = 2 * 365 * 24 * 3600  # ~2 years of look-ahead
_MAX_SKIP_DATE_LOOKAHEAD = 500_000  # absolute safety ceiling (anti-infinite-loop)

# Bounded non-blocking acquire for the cron-store advisory lock (see
# CronService._file_lock). The spin never parks the event loop in an
# uninterruptible kernel wait; it fails fast after the timeout instead.
_FILE_LOCK_TIMEOUT_SECS = 10.0  # max wall-time to wait for the store lock
_FILE_LOCK_POLL_SECS = 0.02  # sleep between non-blocking acquire attempts


class CronStoreBusy(TimeoutError):
    """Raised when a cron-store mutator cannot acquire the store lock in time.

    This is the DEFINED failure contract of the store mutators (:meth:`add_job`,
    :meth:`update_job`, :meth:`remove_job`, :meth:`enable_job`, :meth:`ack_job`,
    :meth:`unack_job` and their ``*_async`` variants): under sustained lock
    contention they raise this instead of blocking forever. It subclasses
    :class:`TimeoutError` so the existing ``except TimeoutError`` guards (the
    reaper sweep, the timer tick, the read-path degrade) keep catching it, while
    giving the public scheduling boundaries a named, greppable type to translate
    into a clean *retryable* error — HTTP 409 at the dashboard handlers, a
    structured ``Error:`` string at the MCP tools, a "store busy, try again"
    reply at the Slack surfaces — rather than surfacing an opaque 500 / tool
    crash. Contention is transient (a large atomic save on network storage, the
    CLI process, or the off-loop batch-remove worker holding the lock), so the
    correct caller response is to retry, not to fail permanently.
    """


# ── Loop-safety guard ───────────────────────────────────────────────────────
# The store lock (``CronService._file_lock``) must NEVER be acquired on a thread
# that has a running asyncio event loop: the bounded ``time.sleep`` spin would
# park that loop under contention. The invariant is upheld structurally —
# loop-resident callers use the ``*_async`` mutators (which ``asyncio.to_thread``
# the lock+save) and the synchronous ``CronSDK`` facade offloads to a worker
# thread when a loop is running — but conventions drift as new writers are
# added. ``_file_lock`` therefore MACHINE-ENFORCES the rule: on entry it detects
# a running loop on the current thread and, when strict mode is enabled, RAISES
# so a regression is caught in CI rather than silently re-freezing the loop.
#
# Gating mirrors the repo's other strict rails (e.g. KIROCREW_STRICT_ON_LOOP_
# PERSIST): OFF by default it degrades to a throttled warning (so no production
# path is broken by an unforeseen legitimate on-loop caller, and existing tests
# that seed jobs via the sync mutators from an async body keep passing); the CI
# loop-safety regression test flips it ON to prove the guard fires and that the
# sanctioned async / offloaded-sync paths do NOT trip it. Operators can export
# KIROCREW_STRICT_LOOP_SAFETY=1 to escalate the warning to a hard failure fleet-
# wide.
_STRICT_LOOP_SAFETY_ENV = "KIROCREW_STRICT_LOOP_SAFETY"
_loop_safety_warned = False


class CronLoopSafetyError(RuntimeError):
    """Raised when the cron store lock is acquired on a running event loop.

    Signals a loop-park hazard: a synchronous ``_file_lock`` acquisition on a
    thread with a live asyncio loop would block that loop in the bounded lock
    spin under contention (the ``no-blocking-call-on-event-loop`` class this
    module exists to eliminate). The fix is to use the ``*_async`` mutator
    variant (``add_job_async`` et al.), or — from the synchronous ``CronSDK``
    facade — to let it offload to a worker thread. Only raised under strict
    mode (``KIROCREW_STRICT_LOOP_SAFETY``); otherwise the guard warns.
    """


# Jitter bounds (seconds) to spread job execution and avoid traffic spikes
_JITTER_HOURLY_MAX = 5 * 60  # 0–5 minutes for hourly jobs
_JITTER_DAILY_MAX = 59 * 60  # 0–59 minutes for daily jobs


# ── Types ──


@dataclass
class CronSchedule:
    """Schedule definition — ``every``, ``at``, or ``cron``."""

    kind: str  # "every" | "at" | "cron"
    every_secs: int | None = None
    at_ts: float | None = None
    cron_expr: str | None = None  # "min hour dom month dow"


@dataclass
class CronJob:
    """A scheduled job."""

    id: str
    name: str
    message: str
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    channel: str | None = None
    thread_ts: str | None = None
    enabled: bool = True
    user_paused: bool = False  # True when explicitly paused by user; never mutated by execution
    auto_paused: bool = (
        False  # True when paused by execution after repeated failures; cleared on re-enable/success
    )
    last_run_ts: float | None = None
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None
    created_ts: float = 0.0
    delete_after_run: bool = False
    # Runtime-only (never serialized): set by the gateway when THIS run was
    # refused by the fire-time governance gate. A denied run is a policy
    # state, not a completed run: a one-shot delete_after_run job is RETAINED
    # instead of deleted, and a denied "at" job is parked DISABLED (a past-due
    # at-job left enabled would be due again on every timer tick — a
    # zero-delay refire loop) so an operator can re-enable it after a policy
    # loosening. Recurring jobs need neither: they wait for their next slot.
    # Reset at the start of every run.
    fire_time_denied: bool = False
    last_result: str | None = None
    # Runtime-only (never serialized): True once THIS run produced a result
    # via set_run_result(). For AGENT jobs ``last_result`` is a cross-run
    # context-carry field that result-less runs deliberately leave in place
    # for the next run's prompt dedup, so the history recorder needs this
    # marker — not the value — to decide attribution. Command and script
    # jobs instead clear it on every result-less exit: the prompt built for
    # them is discarded (the command branch never reads it, the script
    # branch reassigns the variable), so a carried-over value could only
    # ever misreport a finished run's result. Identity/equality checks on
    # the string cannot do that job: CPython interns equal literals and caches
    # single-character latin-1 strings, so a run re-producing the same text
    # is indistinguishable from a run that produced nothing. Reset at the
    # start of every run by _run_job_isolated.
    result_produced: bool = False
    # Runtime-only, reset by _execute_with_timeout at the start of every run:
    # True once THIS run's failure has been counted via record_failure(). The
    # timeout handler consults it so a run that already recorded its failure
    # (e.g. a delivery-path exception) and then overran its deadline during
    # cleanup is counted once, not twice.
    failure_recorded: bool = False
    context_enabled: bool = False
    agent_id: str = ""
    approval_mode: str = ""  # "" (default/hook-based) | "auto" (auto-approve all tools)
    acked_items: list[str] = field(default_factory=list)
    created_by: str = ""  # Slack user ID of the creator (for DM fallback)
    silent: bool = False  # suppress auto-delivery; agent sends via send_message
    session_key: str = ""  # session that created this job (for scoped removal)
    last_posted_hash: str = ""  # hash of last result posted to Slack (dedup)
    consecutive_dupes: int = 0  # count of suppressed duplicate results
    last_posted_at: float = 0.0  # epoch when last Slack post was delivered (dedup reminder)
    last_failure_hash: str = ""  # hash of last failure notification (dedup crashes)
    last_failure_at: float = 0.0  # epoch of last failure Slack alert (dedup reminder)
    consecutive_failures: int = 0  # consecutive failed runs (any error); drives auto-pause
    skip_dates: list[str] = field(default_factory=list)  # ISO dates to skip ["2026-04-06"]
    timezone: str = ""  # IANA timezone for skip evaluation
    persistent_session: bool = True  # False → fresh ephemeral session per run
    minimal_context: bool = False  # True → skip memory/lessons/skills/history
    hide_in_chat: bool = (
        False  # True → don't create a dashboard chat slot; result still goes to history + Slack/bell
    )
    # Cron folder grouping; "" = unfiled. CONTRACT for all consumers
    # (Schedule UI, calendar, CLI, MCP): an id that does not match a folder
    # in cron_folders.json MUST be treated as ungrouped — folder deletion
    # clears assignments only best-effort, so dangling ids are expected and
    # benign (they self-heal on the job's next folder move).
    folder_id: str = ""
    model: str = ""  # per-job model override (canonical key or provider id); "" = inherit

    # When agent_sequence is set, it takes precedence over agent_id.
    # The execution logic runs agents in order; see Phase 3.
    agent_sequence: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # per-job environment variables
    timeout_secs: int = _JOB_TIMEOUT_SECS
    strict_schedule: bool = False  # when True, skip jitter and fire exactly on schedule
    script: str = ""  # Python callable path (module:func or file.py:func); bypasses LLM dispatch
    command: str = ""  # Shell command for direct execution; bypasses LLM dispatch
    timeout: int = (
        0  # script/command timeout in seconds (0 = use default: 30s script, 300s command)
    )

    def set_run_result(self, value: str) -> None:
        """Record a result produced by the CURRENT run.

        Sole write path for executor callbacks: pairs the ``last_result``
        assignment with the runtime-only ``result_produced`` marker so the
        history recorder can attribute the value to this run. Direct
        ``last_result`` assignment stays reserved for store merge and
        deserialization paths, which restore prior state rather than
        produce a new result.
        """
        self.last_result = value
        self.result_produced = True

    def clear_carried_result(self) -> None:
        """Drop a PREVIOUS run's result when this run produced none.

        Result-less command/script exits must not display the last run's
        output beside this run's status. Guarded on ``result_produced`` so a
        run that produced and delivered a result and then failed during
        cleanup keeps it. Assigns directly rather than via set_run_result()
        so a cleared field is never marked as produced by this run.
        """
        if not self.result_produced:
            self.last_result = ""

    def _audit_pause_change(self, outcome: str) -> None:
        """Emit a SEL audit event for an auto-pause permission transition.

        Auto-pausing revokes a job's ability to execute (and clearing it restores
        that ability), so the transition is a permission decision that must be
        auditable per the security-controls guideline. Best-effort — an audit
        write failure must never mask the failure/success bookkeeping that drives
        the pause itself; the tool-invocation error paths already log the run
        outcome separately."""
        try:
            sel.sel().log_tool_invocation(
                session_key=f"cron:{self.id}",
                tool_name=self.script or self.command or "cron_job",
                tool_kind="cron_auto_pause",
                outcome=outcome,
                metadata={"job_id": self.id, "consecutive_failures": self.consecutive_failures},
            )
        except Exception:
            logger.debug("SEL logging failed in cron auto-pause transition", exc_info=True)

    def record_failure(self) -> None:
        """Count one consecutive failure and auto-pause once the threshold is hit.

        Auto-pause is execution-owned: it sets both `enabled` (so the in-memory
        scheduler stops firing immediately) and `auto_paused` (the durable reason,
        distinct from a user pause), so the pause survives a reload. Single-sourced
        here so the many script/command failure branches can't drift on how a pause
        is recorded — mirroring how the effective-enabled derivation reads it back.
        """
        self.consecutive_failures += 1
        self.failure_recorded = True
        if self.consecutive_failures >= _AUTO_PAUSE_THRESHOLD and not self.auto_paused:
            self.enabled = False
            self.auto_paused = True
            self._audit_pause_change("auto_paused")

    def record_success(self) -> None:
        """Reset the failure counter and lift any execution auto-pause.

        Clearing an auto-pause also re-enables the job, because ``enabled`` is
        not independent state: :func:`_job_enabled` reconstructs it on load as
        ``not user_paused and not auto_paused``. Leaving ``enabled`` False after
        clearing ``auto_paused`` therefore produces a job that is paused in
        memory and enabled on disk — it stays stopped until the next restart
        silently resumes it, which is the surprise a manual "Run Now" on an
        auto-paused job used to create.

        A job the user paused stays paused: ``user_paused`` is never mutated by
        execution, so it is the discriminator here, and re-enabling THAT is the
        user's action (``enable_job``).
        """
        self.consecutive_failures = 0
        if self.auto_paused:
            self.auto_paused = False
            if not self.user_paused:
                self.enabled = True
            self._audit_pause_change("auto_pause_cleared")


# ── Session-context helper ──


def build_cron_session_context(job: CronJob) -> tuple[str, str]:
    """Compute (session_key, prompt) for one cron run.

    When ``job.persistent_session`` is True (default, legacy behaviour):
      - session_key is stable across runs: ``cron:{job.id}``
      - prompt prepends ``job.last_result`` so the agent has recent context

    When ``job.persistent_session`` is False:
      - session_key is unique per call: ``cron:{job.id}:{uuid}``
        → each run opens a fresh agent session; no context accumulation
      - prompt is the bare ``job.message`` — no last_result injection
        (accumulated state is the other half of the bug)

    The key prefix ``cron:{job.id}`` is preserved in both modes so the
    reaper's existing session-matching logic continues to work.

    This is a pure function — all side effects (session creation, Slack
    delivery, acked_items handling) happen in the caller. Keep it that way
    so it stays trivially unit-testable.
    """
    if job.persistent_session:
        msg = job.message
        if job.last_result:
            last = job.last_result
            if job.minimal_context and len(last) > 2000:
                last = "[truncated]…" + last[-2000:]
            msg = (
                "[Previous run result — do NOT repeat the same content]\n"
                f"{last}\n"
                "[End of previous run result]\n\n"
                f"{msg}"
            )
        return f"cron:{job.id}", msg

    # Stateless: fresh key, bare message.
    run_id = uuid.uuid4().hex[:8]
    return f"cron:{job.id}:{run_id}", job.message


# ── Cron expression matching (via croniter) ──


def cron_expr_matches(expr: str, dt: datetime) -> bool:
    """Check if ``dt`` matches a 5-field cron expression (min hour dom month dow)."""
    try:
        return croniter.match(expr, dt)
    except (ValueError, KeyError):
        return False


def validate_cron_expr(expr: str) -> bool:
    """Return True if ``expr`` is a syntactically valid 5-field cron expression."""
    return croniter.is_valid(expr)


# ── Service ──


def _humanize_cron(expr: str, tz_name: str = "") -> str:
    """Convert a 5-field cron expression to human-readable string with timezone."""
    if get_description is None:
        return expr
    opts = Options()
    opts.use_24hour_time_format = False
    try:
        desc = get_description(expr, opts)
    except Exception:
        return expr

    # Timezone-aware display: evaluate the cron expression in the job's
    # timezone (matching compute_next_run_ts) and display the local time.
    parts = expr.split()
    if tz_name and len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
        try:
            tz = ZoneInfo(tz_name)
            # Evaluate in job timezone, same as the scheduler does
            base = datetime.now(tz)
            next_local = croniter(expr, base).get_next(datetime).astimezone(tz)
            local_time = platform_compat.strftime(next_local, "%-I:%M %p %Z")
            # cron_descriptor produces UTC-based text; replace the time portion
            utc_base = datetime.now(timezone.utc)
            next_as_utc = croniter(expr, utc_base).get_next(datetime)
            utc_time = platform_compat.strftime(next_as_utc, "%-I:%M %p")
            utc_time_padded = next_as_utc.strftime("%I:%M %p")
            result = desc.replace(f"At {utc_time}", f"At {local_time}")
            if result == desc:
                result = desc.replace(f"At {utc_time_padded}", f"At {local_time}")
            if result == desc:
                # Fallback: prepend local time if replacement failed
                result = f"At {local_time}, {desc.removeprefix('At ')}"
            return result
        except Exception:
            pass

    return desc


def format_schedule(schedule: CronSchedule, tz_name: str = "") -> str:
    """Human-readable schedule description."""
    # Fallback: read timezone from config (callers in loops should pass tz_name)
    if not tz_name:
        try:
            tz_name = KiroCrewConfig.load().timezone
        except Exception:
            pass
    if schedule.kind == "cron" and schedule.cron_expr:
        return _humanize_cron(schedule.cron_expr, tz_name)
    if schedule.kind == "every" and schedule.every_secs:
        secs = schedule.every_secs
        if secs >= 3600:
            return f"every {secs // 3600}h"
        return f"every {secs}s"
    if schedule.kind == "at" and schedule.at_ts:
        tz = ZoneInfo(tz_name) if tz_name else None
        if tz:
            now = datetime.now(tz)
            dt = datetime.fromtimestamp(schedule.at_ts, tz)
        else:
            now = datetime.now().astimezone()
            dt = datetime.fromtimestamp(schedule.at_ts).astimezone()
        if dt.date() == now.date():
            return f"at {dt:%I:%M %p %Z}"
        return f"at {dt:%I:%M %p %Z}, {platform_compat.strftime(dt, '%b %-d')}"
    return schedule.kind


def is_valid_timezone(tz_name: str) -> bool:
    """Return True if ``tz_name`` is a resolvable IANA timezone key.

    Validates via the ``ZoneInfo`` constructor -- a single targeted, cached
    lookup -- rather than ``available_timezones()``, which recursively walks
    the entire tzdata tree and opens many files on every call. Because this
    runs on callers reachable from the async event loop (dashboard cron PATCH
    -> CronService.update_job), the cheap constructor path avoids blocking the
    gateway (see ``no-blocking-call-on-event-loop``). ``ZoneInfo`` raises
    ``ZoneInfoNotFoundError`` for unknown keys and ``ValueError`` for malformed
    ones (e.g. absolute paths, ``..``); both are treated as invalid.
    """
    if not tz_name:
        return False
    try:
        ZoneInfo(tz_name)
    except Exception:
        return False
    return True


def is_valid_skip_date(value: object) -> bool:
    """Return True iff ``value`` is a strict, zero-padded ``YYYY-MM-DD`` date.

    ``datetime.strptime(s, "%Y-%m-%d")`` accepts non-padded inputs such as
    ``"2026-1-1"``: they parse fine, but fire-time skip matching compares
    against a zero-padded rendering (``"2026-01-01"``), so the intended skip
    silently never matches and the job runs on a date the user told it to
    skip -- with no error anywhere. Requiring the parsed value to round-trip
    exactly back to ``%Y-%m-%d`` rejects non-padded (and calendar-invalid)
    inputs at every persistence path, independent of the running Python
    version's ``date.fromisoformat`` leniency.
    """
    s = str(value)
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except (ValueError, TypeError):
        return False


def get_local_tz() -> tuple[str, ZoneInfo]:
    """Return (tz_name, ZoneInfo) from config, falling back to UTC."""
    try:
        tz_name = KiroCrewConfig.load().timezone or "UTC"
        return tz_name, ZoneInfo(tz_name)
    except Exception:
        logger.warning(
            "Failed to load timezone from config, falling back to UTC",
            exc_info=True,
        )
        return "UTC", ZoneInfo("UTC")


def _job_tz(job: CronJob) -> ZoneInfo:
    """Return the job's timezone, falling back to config then UTC."""
    try:
        tz_name = job.timezone or KiroCrewConfig.load().timezone or "UTC"
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("Failed to resolve timezone for job %s, using UTC", job.id, exc_info=True)
        return ZoneInfo("UTC")


def compute_next_run_ts(job: CronJob, now: float | None = None) -> float | None:
    """Return the next fire time as a UTC epoch, or ``None`` if unknown."""
    try:
        if not job.enabled:
            return None
        sched = job.schedule
        now = now if now is not None else time.time()
        if sched.kind == "every" and sched.every_secs is not None:
            last = job.last_run_ts if job.last_run_ts is not None else job.created_ts
            if last is None:
                return None
            nxt = last + sched.every_secs
            return nxt if nxt > now else now
        if sched.kind == "at" and sched.at_ts is not None:
            return sched.at_ts if sched.at_ts > now else None
        if sched.kind == "cron" and sched.cron_expr is not None:
            # croniter interprets cron_expr in base's timezone; get_next(float) returns UTC epoch
            tz = _job_tz(job)
            base = datetime.fromtimestamp(now, tz=tz)
            cron = croniter(sched.cron_expr, base)
            # Advance past any skip_dates, bounded by a wall-clock horizon so the
            # bound does not depend on schedule granularity (a daily and a */5
            # cron both look ~2 years ahead). The iteration count is only a hard
            # safety ceiling against a pathological all-skipped sub-minute config.
            horizon = now + _MAX_SKIP_DATE_HORIZON_SECS
            for _ in range(_MAX_SKIP_DATE_LOOKAHEAD):
                nxt = cron.get_next(float)
                if not job.skip_dates:
                    return nxt
                if nxt > horizon:
                    logger.warning(
                        "No valid next run within ~2y horizon for job %s (all dates skipped)",
                        job.id,
                    )
                    return None
                local_date = datetime.fromtimestamp(nxt, tz=tz).strftime("%Y-%m-%d")
                if local_date not in job.skip_dates:
                    return nxt
            logger.warning(
                "No valid next run within %d-iteration safety cap for job %s (all dates skipped)",
                _MAX_SKIP_DATE_LOOKAHEAD,
                job.id,
            )
            return None
    except Exception:
        logger.warning("Failed to compute next run for job %s", job.id, exc_info=True)
        return None
    return None


def _record_is_enabled(j: dict[str, Any]) -> bool:
    """Single owner for the effective-enabled predicate of a serialized job.

    A job is enabled when it is neither user-paused nor auto-paused, with the
    legacy ``!enabled`` fallback for stores written before those fields existed.
    Both ``_load`` (the scheduler deserialization path) and
    ``count_enabled_from_disk`` (the off-thread dashboard count) MUST route
    through here so the semantics have exactly one implementation and cannot
    drift when a future pause-state change lands in only one reader.
    """
    user_paused = j.get("user_paused", not j.get("enabled", True))
    auto_paused = j.get("auto_paused", False)
    return not user_paused and not auto_paused


def _job_from_record(j: dict[str, Any]) -> CronJob:
    """Build one :class:`CronJob` from its serialized record.

    Raises ``KeyError``/``TypeError``/``AttributeError`` when the record is
    malformed (missing required keys, or not shaped like a job object at all).
    The caller (:meth:`CronService._load`) isolates that failure to THIS entry
    — one bad record must never discard the rest of the store (#4664).
    """
    return CronJob(
        id=j["id"],
        name=j["name"],
        message=j["message"],
        schedule=CronSchedule(
            kind=j["schedule"]["kind"],
            every_secs=j["schedule"].get("every_secs"),
            at_ts=j["schedule"].get("at_ts"),
            cron_expr=j["schedule"].get("cron_expr"),
        ),
        channel=j.get("channel"),
        thread_ts=j.get("thread_ts"),
        # Effective enabled is derived from the two "reasons a job is
        # off": an explicit user pause and an execution auto-pause
        # (repeated failures). Deriving it — rather than trusting the
        # stored `enabled` — is what makes an auto-pause survive a
        # restart: the failing run sets auto_paused=True, and a
        # recurring job's `enabled` is otherwise never persisted, so a
        # naive `enabled` read would resurrect the job on reload.
        # The predicate (incl. the legacy !enabled fallback) has one
        # owner, `_record_is_enabled`, shared with
        # count_enabled_from_disk so the two readers cannot drift.
        enabled=_record_is_enabled(j),
        user_paused=j.get("user_paused", not j.get("enabled", True)),
        auto_paused=j.get("auto_paused", False),
        last_run_ts=j.get("last_run_ts"),
        last_status=j.get("last_status"),
        last_error=j.get("last_error"),
        created_ts=j.get("created_ts", 0.0),
        delete_after_run=j.get("delete_after_run", False),
        last_result=j.get("last_result"),
        context_enabled=j.get("context_enabled", False),
        agent_id=j.get("agent_id", ""),
        approval_mode=j.get("approval_mode", ""),
        acked_items=j.get("acked_items", []),
        created_by=j.get("created_by", ""),
        silent=j.get("silent", False),
        session_key=j.get("session_key", ""),
        last_posted_hash=j.get("last_posted_hash", ""),
        consecutive_dupes=j.get("consecutive_dupes", 0),
        last_posted_at=j.get("last_posted_at", 0.0),
        last_failure_hash=j.get("last_failure_hash", ""),
        last_failure_at=j.get("last_failure_at", 0.0),
        consecutive_failures=j.get("consecutive_failures", 0),
        skip_dates=j.get("skip_dates", []),
        timezone=j.get("timezone", ""),
        persistent_session=j.get("persistent_session", True),
        minimal_context=j.get("minimal_context", False),
        hide_in_chat=j.get("hide_in_chat", False),
        folder_id=j.get("folder_id", ""),
        model=j.get("model", ""),
        agent_sequence=j.get("agent_sequence", []),
        env=j.get("env", {}),
        timeout_secs=j.get("timeout_secs", _JOB_TIMEOUT_SECS),
        strict_schedule=j.get("strict_schedule", False),
        script=j.get("script", ""),
        command=j.get("command", ""),
        timeout=j.get("timeout", 0),
    )


class CronService:
    """Background service for managing and executing scheduled jobs."""

    def __init__(
        self,
        base_dir: Path | None = None,
        on_job: Callable[[CronJob], Awaitable[str | None]] | None = None,
        *,
        _defer_initial_load: bool = False,
    ):
        self._dir = base_dir if base_dir is not None else _default_dir()
        self._path = self._dir / _CRONS_FILE
        self._on_job = on_job
        self._jobs: list[CronJob] = []
        self._timer_task: asyncio.Task[None] | None = None
        # True only for the span of an in-flight _on_timer() dispatch pass
        # (set/cleared there). _arm_timer() checks this to avoid cancelling
        # self._timer_task out from under a sweep that hasn't finished
        # spawning its due jobs yet — see _arm_timer's guard for the failure
        # mode this prevents.
        self._on_timer_running = False
        self._running = False
        # The event loop this service is bound to, captured in create()/start()
        # (the gateway's loop). _arm_timer() uses it to re-arm the timer THREAD-
        # SAFELY when it is reached OFF the loop — inside an asyncio.to_thread
        # worker running a locked core whose _sync()->_load() wants to re-arm —
        # by handing the arm back to the loop via loop.call_soon_threadsafe(
        # self._arm_timer). Arming is therefore an IN-SERVICE guarantee owned by
        # CronService: no caller (mutator, app hook, SDK, or route) has to
        # remember to drain a deferred arm, so no off-loop mutation path can
        # silently leave the timer un-armed (the "scheduled job never fires"
        # failure class this module exists to prevent). Stays None in genuinely
        # loop-less processes (CLI, MCP server, apps SDK, tests), where there is
        # no scheduler loop to arm.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_mtime: float = 0.0
        # Fingerprint of the store as last LOADED, used by _sync to decide
        # whether the on-disk file changed. mtime alone is insufficient: on
        # filesystems with coarse (1s) mtime granularity — or simply two writes
        # within the same clock tick — a second external write lands with an
        # EQUAL st_mtime, so the old `mtime > self._last_mtime` check skipped
        # the reload and silently dropped that update. A (mtime_ns, size) tuple
        # improves on that but still collides when an external write preserves
        # BOTH the coarse timestamp and the byte length (e.g. renaming a job to
        # an equal-length name), which would again drop the update and let the
        # next _save overwrite it. The authoritative signal is therefore a
        # content DIGEST derived from the same bytes we parse; mtime_ns/size are
        # retained for diagnostics. _save refreshes all three so we never reload
        # our own write.
        self._last_mtime_ns: int = 0
        self._last_size: int = -1
        self._last_digest: bytes = b""
        self._executing: set[str] = set()  # job IDs currently running
        self._running_tasks: dict[str, asyncio.Task[None]] = {}  # strong refs to prevent GC
        self._job_start_times: dict[str, float] = {}  # job ID → epoch start
        self._reaped_jobs: set[str] = set()  # job IDs killed by the reaper
        self._cancelled_jobs: set[str] = set()  # job IDs cancelled by the user
        self._job_jitter: dict[str, float] = {}  # job ID → jitter seconds applied
        self._job_run_meta: dict[str, tuple[float, str]] = {}  # job_id → (start_time, trigger)
        # Job IDs whose one-shot (delete_after_run / Done) removal was DEFERRED
        # because remove_job_async hit a contended store (CronStoreBusy). The
        # timer tick drains these under the store lock in a worker thread (see
        # defer_removal / _drain_pending_removals_locked / _tick_scan_locked) so
        # a completed one-shot is always
        # eventually removed and can never re-fire in the meantime.
        self._pending_removals: set[str] = set()
        # True while a critical-posture episode is deferring scheduled
        # firings (see _on_timer). Log-throttle state only: the INFO line
        # fires once per deferral episode, not once per deferred tick.
        self._admission_deferring: bool = False
        self._admission_last_log: float = 0.0
        # job_id → active session_key for the in-flight run.
        # Populated by the dispatcher (gateway callback) so the reaper can
        # target per-run ephemeral keys when persistent_session=False.
        self._active_session_keys: dict[str, str] = {}
        self._sessions: SessionManager | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._push_refresh: Callable[[str], None] | None = None  # set externally
        _cfg = KiroCrewConfig.load().cron_history
        self._history = CronHistoryStore(
            base_dir=base_dir if base_dir is not None else _default_dir(),
            cron_summary_cap=_cfg.cron_summary_cap,
            cron_trace_cap_kb=_cfg.cron_trace_cap_kb,
            cron_max_records_per_job=_cfg.cron_max_records_per_job,
            cron_max_index_records=_cfg.cron_max_index_records,
        )
        # Populate the in-memory snapshot from disk once at construction.
        # The read paths (list_jobs / get_job) are CACHE-ONLY — they perform no
        # filesystem I/O on the hot event-loop path (see list_jobs). They used
        # to lazily _load() on first read via _sync(); loop-less callers that
        # construct a service and read immediately without start() (the MCP and
        # CLI processes, tests) relied on that. An initial load here restores
        # the "a fresh service reflects on-disk state" invariant without
        # putting any I/O back on the gateway's hot read path (the gateway
        # constructs its service once at startup, off any hot loop, and
        # start() reloads anyway). No timer is armed: _running is still False.
        #
        # BUT the initial _load() itself read_bytes()+blake2b-hashes the WHOLE
        # crons.json — synchronous filesystem I/O. For genuinely-sync, loop-less
        # processes (CLI, MCP server, apps SDK, tests) that is fine: there is no
        # event loop to park. The async gateway, however, constructs its
        # CronService INSIDE its running startup coroutine, so a plain
        # constructor _load() would block the sole event loop (chat, WS, timers,
        # heartbeat) on that read — violating no-blocking-call-on-event-loop.
        # Loop contexts therefore MUST construct via the async factory
        # CronService.create(), which passes _defer_initial_load=True (skipping
        # the load here) and instead runs _load() in a worker thread via
        # asyncio.to_thread. Enforced mechanically by
        # test_cron_locking_regression.py::TestConstructionLoadOffLoop.
        if not _defer_initial_load:
            self._load()

    # ── Lifecycle ──

    @classmethod
    async def create(
        cls,
        base_dir: Path | None = None,
        on_job: Callable[[CronJob], Awaitable[str | None]] | None = None,
    ) -> "CronService":
        """Async factory for event-loop contexts (the gateway).

        Equivalent to ``CronService(...)`` but SAFE to call from a running
        event loop: the plain constructor performs its initial ``_load()`` —
        a whole-file ``read_bytes()`` + blake2b hash of ``crons.json`` —
        synchronously, which would block the sole gateway loop (chat, WS,
        timers, heartbeat) during async startup. This factory constructs with
        ``_defer_initial_load=True`` (so the constructor does no store I/O) and
        then runs that initial ``_load()`` in a worker thread via
        ``asyncio.to_thread``. ``_running`` is still ``False`` at this point, so
        ``_load()`` arms no timer — running it off-loop is safe.

        Genuinely-sync, loop-less processes (CLI, MCP server, apps SDK, tests)
        must keep using the plain constructor, which loads inline.
        """
        self = cls(base_dir=base_dir, on_job=on_job, _defer_initial_load=True)
        # Bind to the gateway loop so off-loop mutation paths (async mutators'
        # worker cores, app-hook/SDK calls offloaded via asyncio.to_thread) can
        # re-arm the timer thread-safely — see _arm_timer / __init__ _loop.
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._load)
        return self

    async def start(self) -> None:
        """Load jobs and start the timer loop.

        ``_load()`` is offloaded to a worker thread (``asyncio.to_thread``):
        ``start()`` is always awaited on the gateway event loop, and the load
        does a whole-file read+hash of ``crons.json`` — synchronous filesystem
        I/O that must never run on the loop. ``_running`` is still ``False``
        here, so the load arms no timer; ``_arm_timer()`` is called explicitly
        on the loop afterwards.
        """
        # Bind to the running loop (idempotent if create() already did) so any
        # off-loop re-arm during this service's lifetime self-heals to it.
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._load)
        self._running = True
        await self._history.rotate_all()
        self._arm_timer()
        logger.info("Cron service started with %d jobs", len(self._jobs))

    async def stop(self) -> None:
        """Stop the timer loop and cancel running jobs."""
        self._running = False
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        for task in self._running_tasks.values():
            task.cancel()
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
            self._running_tasks.clear()

    # ── Reaper ──

    def start_reaper(self, sessions: SessionManager) -> None:
        """Start the periodic reaper loop.  Call once after the event loop is running."""
        self._sessions = sessions
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        """Periodically force-kill cron jobs that exceed the timeout.

        Defense-in-depth: catches cases where ``asyncio.wait_for`` in
        ``_execute_with_timeout`` fails to fire (event-loop saturation,
        orphaned tasks).
        """
        while True:
            await asyncio.sleep(_REAPER_INTERVAL)
            now = time.time()
            # Snapshot the job list CACHE-ONLY — no store lock, no _sync, no
            # disk I/O on the loop (same rationale as list_jobs/get_job). The
            # batch-remove worker (remove_jobs → asyncio.to_thread) builds a
            # NEW list and swaps self._jobs by an atomic reference assignment,
            # so this comprehension iterates one coherent list object (either
            # the pre- or post-swap list, never a half-rebuilt one) and can
            # never tear. The reaper only needs the in-memory view to map
            # running task ids → timeouts; cross-process freshness is
            # irrelevant to force-killing a locally-running task.
            jobs_by_id = {j.id: j for j in self._jobs}
            for job_id, started in list(self._job_start_times.items()):
                elapsed = now - started
                job = jobs_by_id.get(job_id)
                deadline = (
                    max(min(job.timeout_secs, 86400), _JOB_TIMEOUT_SECS)
                    if job
                    else _JOB_TIMEOUT_SECS
                )
                jitter_allowance = self._job_jitter.get(job_id, 0.0)
                if elapsed <= deadline + jitter_allowance:
                    continue
                task = self._running_tasks.get(job_id)
                if task and task.done():
                    # Normal timeout path already completed; just clean up tracking.
                    self._job_start_times.pop(job_id, None)
                    continue
                logger.warning(
                    "Reaper: cron job %s exceeded %ds (ran %.0fs), force-killing",
                    job_id,
                    deadline,
                    elapsed,
                )
                try:
                    await self._force_reap(job_id, elapsed, deadline)
                except Exception:
                    logger.exception("Reaper: failed to reap cron job %s", job_id)

    async def _force_reap(
        self, job_id: str, elapsed: float, deadline: int = _JOB_TIMEOUT_SECS
    ) -> None:
        """Kill a cron job's session process and cancel its task."""
        # use the active per-run session key if registered;
        # fall back to the stable key for persistent or legacy callers.
        session_key = self._active_session_keys.get(job_id) or f"cron:{job_id}"
        self._reaped_jobs.add(job_id)
        meta = self._job_run_meta.pop(job_id, None)
        reap_started_at = meta[0] if meta else time.time() - elapsed
        reap_trigger = meta[1] if meta else "scheduled"
        self._job_start_times.pop(job_id, None)  # prevent repeated reaping
        # Kill the session process first.
        if self._sessions:
            try:
                await asyncio.wait_for(
                    self._sessions.reset(session_key), timeout=_REAPER_RESET_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("Reaper: reset hung for cron %s, attempting SIGKILL", job_id)
                await self._sigkill_session(session_key)
            except Exception:
                logger.exception("Reaper: reset failed for cron %s, attempting SIGKILL", job_id)
                await self._sigkill_session(session_key)

        # Cancel the asyncio task and clean up tracking state directly.
        # Don't rely on _run_job_isolated's finally — the reaper exists for
        # cases where the normal path is stuck (idempotent with finally).
        task = self._running_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        self._executing.discard(job_id)

        # Update job state and persist. The persist goes through the locked
        # worker-thread merge helper (offloaded via asyncio.to_thread) — NOT a
        # bare on-loop self._save() — so it re-syncs under the store lock and
        # cannot clobber a concurrent add/update worker's just-written job
        # list, and its bounded lock spin never parks the event loop this
        # coroutine runs on. See _merge_terminal_state_locked.
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job:
            last_error = f"Reaped after {int(elapsed)}s (exceeded {deadline}s deadline)"
            last_run_ts = time.time()
            # Reflect into the in-memory snapshot for the history record below
            # and any immediate reader; the authoritative persist is the locked
            # merge, which re-derives the disk copy after _sync().
            job.last_status = "error"
            job.last_error = last_error
            job.last_run_ts = last_run_ts
            try:
                await asyncio.to_thread(
                    self._merge_terminal_state_locked,
                    job_id,
                    last_status="error",
                    last_error=last_error,
                    last_run_ts=last_run_ts,
                )
            except Exception:
                logger.exception("Reaper: failed to persist state for cron %s", job_id)
            # Record timeout in history
            try:
                record = CronRunRecord(
                    job_id=job_id,
                    trigger=reap_trigger,
                    started_at=reap_started_at,
                    finished_at=time.time(),
                    duration_ms=int(elapsed * 1000),
                    status="timeout",
                    summary=job.last_error or "",
                    error=job.last_error or "",
                )
                await self._history.append(record)
                if self._push_refresh:
                    self._push_refresh("cron_history")
            except Exception:
                logger.exception("Reaper: failed to record history for cron %s", job_id)

        # SEL audit.
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key,
                source="cron",
                tool_name="reaper_force_kill",
                outcome="reaped",
                metadata={
                    "job_id": job_id,
                    "session_key": session_key,
                    "elapsed": int(elapsed),
                },
            )
        except Exception:
            logger.exception("Reaper: SEL audit failed for cron %s", job_id)

    async def _sigkill_session(self, session_key: str) -> None:
        """Best-effort SIGKILL when graceful reset hangs.

        Uses killpg to kill the entire process group, then sweeps
        escaped children in different PGIDs (MCP servers).

        Async so the Windows ``taskkill`` spawn offloads to
        :func:`kiro_crew.executors.subprocess_executor` via
        :func:`platform_compat.kill_process_tree_async` / ``kill_pid_async``
        instead of blocking the reaper loop's event loop for the duration of
        ``taskkill.exe``. The child-tree probe helpers
        (``_get_child_pids`` / ``_get_start_time`` / ``_read_basename``) also
        shell out to ``ps`` / ``pgrep`` on macOS, so they are offloaded to the
        same executor.
        """
        if not self._sessions:
            return
        try:
            # circular import: cron → acp.client → session → cron
            from kiro_crew.acp.client import (
                _capture_child_records,
                _get_child_pids,
                _is_our_child,
                _kill_escaped_children,
            )

            session = self._sessions._sessions.get(session_key)
            if not session:
                logger.warning("Reaper: no session found for %s", session_key)
                return
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            pid = raw_pid if isinstance(raw_pid, int) and raw_pid > 1 else None
            if not pid:
                logger.warning("Reaper: no usable PID (%r) for %s", raw_pid, session_key)
                return
            # Snapshot child tree before killing — children in different
            # PGIDs survive killpg. The macOS pgrep/ps spawns happen on the
            # subprocess_executor so the loop keeps ticking.
            loop = asyncio.get_running_loop()
            raw_children = getattr(client, "_child_pids", None)
            child_pids: dict = dict(raw_children) if isinstance(raw_children, dict) else {}
            fresh = await loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
            new_pids = [p for p in fresh if p not in child_pids]
            if new_pids:
                child_pids.update(
                    await loop.run_in_executor(
                        subprocess_executor(), _capture_child_records, new_pids
                    )
                )
            # Validate PID hasn't been recycled before killing.
            original_start = getattr(client, "_start_time", None)
            if original_start is None:
                logger.debug("Reaper: PID %d already dead for %s", pid, session_key)
                await loop.run_in_executor(
                    subprocess_executor(), _kill_escaped_children, child_pids
                )
                return
            if not await loop.run_in_executor(
                subprocess_executor(), _is_our_child, pid, original_start
            ):
                logger.warning("Reaper: PID %d recycled for %s, skipping killpg", pid, session_key)
                stored = dict(raw_children) if isinstance(raw_children, dict) else {}
                await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, stored)
                return
            # Kill the entire process group first
            logger.warning(
                "Reaper: killpg for PID %d (%d children) for %s",
                pid,
                len(child_pids),
                session_key,
            )
            try:
                # killpg(getpgid) on POSIX, taskkill /T on Windows — routed
                # through platform_compat, whose POSIX path carries the
                # broadcast guard (refuses pgid<=1 / own group; see
                # platform_compat.kill_process_tree). Async variant offloads
                # Windows taskkill to subprocess_executor so the reaper loop
                # never blocks the event loop on taskkill.exe.
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
            except ValueError:
                # Guard refused the pid outright (non-int/reserved) — nothing
                # safe to signal.
                logger.error("Reaper: kill guard refused pid %r for %s", pid, session_key)
            except (ProcessLookupError, OSError):
                try:
                    await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
        except Exception:
            logger.exception("Reaper: SIGKILL failed for %s", session_key)

    # ── User-initiated cancellation ──

    async def cancel(self, job_id: str) -> bool:
        """Cancel a running cron execution (user-initiated).

        Kills the sandboxed subprocess (script/command crons) or the kiro-cli
        session (agent crons), cancels the asyncio task, records a
        ``cancelled`` history entry, and leaves ``consecutive_failures``
        untouched. Returns True when a running execution was found.
        """
        if job_id not in self._executing:
            return False
        logger.info("Cancel: user-initiated cancellation of cron job %s", job_id)
        self._cancelled_jobs.add(job_id)
        meta = self._job_run_meta.pop(job_id, None)
        started_at = meta[0] if meta else self._job_start_times.get(job_id, time.time())
        trigger = meta[1] if meta else "scheduled"
        elapsed = time.time() - started_at
        self._job_start_times.pop(job_id, None)
        self._job_jitter.pop(job_id, None)

        job = next((j for j in self._jobs if j.id == job_id), None)

        # 1. Script/command crons: SIGTERM the sandboxed subprocess group.
        # Offloaded: kill_running_process performs blocking kernel calls.
        killed_proc = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), cron_script.kill_running_process, job_id
        )

        # 2. Agent crons: kill the kiro-cli session (mirrors _force_reap).
        session_key = self._active_session_keys.get(job_id) or f"cron:{job_id}"
        is_agent_job = job is None or not (job.script or job.command)
        if self._sessions and is_agent_job and not killed_proc:
            try:
                await asyncio.wait_for(
                    self._sessions.reset(session_key), timeout=_REAPER_RESET_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("Cancel: reset hung for cron %s, attempting SIGKILL", job_id)
                await self._sigkill_session(session_key)
            except Exception:
                logger.exception("Cancel: reset failed for cron %s, attempting SIGKILL", job_id)
                await self._sigkill_session(session_key)

        # 3. Cancel the asyncio task and clean up tracking state directly
        # (idempotent with _run_job_isolated's finally).
        task = self._running_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        self._executing.discard(job_id)

        # 4. Update job state, persist, and record history. The persist goes
        # through the locked worker-thread merge helper (offloaded via
        # asyncio.to_thread) — NOT a bare on-loop self._save() — so it re-syncs
        # under the store lock and cannot clobber a concurrent add/update
        # worker; the bounded spin never parks this loop-side coroutine.
        if job:
            last_error = f"Cancelled by user after {int(elapsed)}s"
            last_run_ts = time.time()
            # In-memory snapshot for the history record / immediate readers;
            # the locked merge is authoritative.
            job.last_status = "error"
            job.last_error = last_error
            job.last_run_ts = last_run_ts
            try:
                await asyncio.to_thread(
                    self._merge_terminal_state_locked,
                    job_id,
                    last_status="error",
                    last_error=last_error,
                    last_run_ts=last_run_ts,
                )
            except Exception:
                logger.exception("Cancel: failed to persist state for cron %s", job_id)
            try:
                record = CronRunRecord(
                    job_id=job_id,
                    trigger=trigger,
                    started_at=started_at,
                    finished_at=time.time(),
                    duration_ms=int(elapsed * 1000),
                    status="cancelled",
                    summary=job.last_error or "",
                    error=job.last_error or "",
                )
                await self._history.append(record)
                if self._push_refresh:
                    self._push_refresh("cron_history")
            except Exception:
                logger.exception("Cancel: failed to record history for cron %s", job_id)
        if self._push_refresh:
            self._push_refresh("crons")

        # SEL audit.
        try:
            sel.sel().log_tool_invocation(
                session_key=session_key,
                source="cron",
                tool_name="cron_cancel",
                outcome="cancelled",
                metadata={
                    "job_id": job_id,
                    "session_key": session_key,
                    "elapsed": int(elapsed),
                    "killed_subprocess": killed_proc,
                },
            )
        except Exception:
            logger.exception("Cancel: SEL audit failed for cron %s", job_id)
        return True

    # ── Public API ──

    def add_job(
        self,
        name: str,
        message: str,
        every_secs: int | None = None,
        at_ts: float | None = None,
        cron_expr: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        delete_after_run: bool = False,
        created_by: str = "",
        approval_mode: str = "",
        enabled: bool = True,
        agent_id: str = "",
        model: str = "",
        silent: bool = False,
        timezone: str = "",
        skip_dates: list[str] | None = None,
        strict_schedule: bool = False,
        hide_in_chat: bool = False,
        folder_id: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        session_key: str = "",
        minimal_context: bool = False,
        timeout: int = 0,
        timeout_secs: int = 0,
    ) -> CronJob:
        """Add a new job. Provide one of ``every_secs``, ``at_ts``, or ``cron_expr``.

        ``enabled=False`` creates the job already paused (``user_paused=True``,
        mirroring :meth:`enable_job`) so the paused state is part of the FIRST
        persist — never an enabled-then-paused two-save window that a crash or
        a concurrent reader of the store could capture as enabled.

        ``timezone``/``skip_dates`` are validated HERE, at the persistence
        owner, and folded into the job before its single ``_save()`` -- so no
        caller can strand a half-populated or invalid job on disk, and every
        create path (MCP, apps SDK, dashboard, CLI) shares one check. This
        consolidates **every** first-save field
        (``agent_id``/``model``/``silent``/``strict_schedule``/``hide_in_chat``,
        ``command``/``script``/``agent_sequence``/``env``/``persistent_session``)
        into the same single locked build+persist, totalizing over all fields
        into the same single locked build+persist, totalizing over all fields
        the "fully-formed on first save" invariant. The MCP create path folds
        ``session_key``/``minimal_context``/``timeout`` here too, replacing its
        former create-then-mutate plus second unlocked ``_save()``.

        Synchronous variant: the lock+save runs INLINE and so must only be
        called from a loop-less context (CLI / MCP server process / a worker
        thread) — the ``_file_lock`` loop-safety guard rejects it on a running
        event loop. On the gateway loop use :meth:`add_job_async`. Accepts the
        same full field set as :meth:`add_job_async` so a caller (e.g. the
        synchronous ``CronSDK`` facade) can persist a fully-formed, owner-tagged
        job in the single locked transaction with no follow-up unlocked
        ``_save()``.
        """
        job = self._build_job(
            name,
            message,
            every_secs=every_secs,
            at_ts=at_ts,
            cron_expr=cron_expr,
            channel=channel,
            thread_ts=thread_ts,
            delete_after_run=delete_after_run,
            created_by=created_by,
            approval_mode=approval_mode,
            enabled=enabled,
            agent_id=agent_id,
            model=model,
            silent=silent,
            timezone=timezone,
            skip_dates=skip_dates,
            strict_schedule=strict_schedule,
            hide_in_chat=hide_in_chat,
            folder_id=folder_id,
            command=command,
            script=script,
            agent_sequence=agent_sequence,
            env=env,
            persistent_session=persistent_session,
            session_key=session_key,
            minimal_context=minimal_context,
            timeout=timeout,
            timeout_secs=timeout_secs,
        )
        self._persist_add_locked(job)
        self._arm_timer()
        logger.info("Added cron job '%s' (%s)", name, job.id)
        return job

    def add_job_if_absent(
        self,
        predicate: Callable[[CronJob], bool],
        **kwargs: Any,
    ) -> CronJob | None:
        """Build and persist a job only when no current store entry matches."""
        job = self._build_job(**kwargs)
        if not self._persist_add_if_absent_locked(predicate, job):
            return None
        self._arm_timer()
        return job

    async def add_job_if_absent_async(
        self,
        predicate: Callable[[CronJob], bool],
        **kwargs: Any,
    ) -> CronJob | None:
        """Event-loop-native :meth:`add_job_if_absent`.

        Mirrors :meth:`add_job_async`: the job is built on-loop, the
        lock/sync/check/append/save core runs in a worker thread so the
        bounded ``_file_lock`` spin never parks the gateway loop, and timer
        arming stays on-loop. The absence check and the append happen under
        ONE store lock after a fresh ``_sync()``, so two concurrent
        registrars (e.g. a CLI enable racing gateway boot) cannot both
        observe the name as absent and persist duplicates. Returns None when
        a matching job already exists.
        """
        job = self._build_job(**kwargs)
        persisted = await asyncio.to_thread(
            self._persist_add_if_absent_locked, predicate, job
        )
        if not persisted:
            return None
        self._arm_timer()
        logger.info("Added cron job '%s' (%s) [if-absent]", job.name, job.id)
        return job

    def _persist_add_if_absent_locked(
        self,
        predicate: Callable[[CronJob], bool],
        job: CronJob,
    ) -> bool:
        """Lock/reload/check/append/save — the atomic add-if-absent disk core.

        Like :meth:`_persist_add_locked` (no timer work, thread-safe, raises
        :class:`CronStoreBusy` on sustained contention) but the existence
        check happens INSIDE the same lock, after ``_sync()`` refreshed the
        in-memory view — closing the snapshot-then-append TOCTOU. Returns
        False when an existing job matches ``predicate``.
        """
        with self._file_lock():
            self._sync()
            if any(predicate(existing) for existing in self._jobs):
                return False
            self._jobs.append(job)
            self._save()
        return True

    def _build_job(
        self,
        name: str,
        message: str,
        every_secs: int | None = None,
        at_ts: float | None = None,
        cron_expr: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        delete_after_run: bool = False,
        created_by: str = "",
        approval_mode: str = "",
        enabled: bool = True,
        agent_id: str = "",
        model: str = "",
        silent: bool = False,
        timezone: str = "",
        skip_dates: list[str] | None = None,
        strict_schedule: bool = False,
        hide_in_chat: bool = False,
        folder_id: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        session_key: str = "",
        minimal_context: bool = False,
        timeout: int = 0,
        timeout_secs: int = 0,
    ) -> CronJob:
        """Validate inputs and construct the :class:`CronJob` (no I/O, no lock).

        Shared by :meth:`add_job` and :meth:`add_job_async` so both perform
        identical validation on the event loop before any disk work. Raises
        ``ValueError`` on an invalid schedule or approval mode.

        ``timeout_secs`` is the per-wake execution budget (the
        ``asyncio.wait_for`` deadline in ``_execute_with_timeout``); ``0`` means
        the ``_JOB_TIMEOUT_SECS`` default. Distinct from ``timeout``, which
        bounds only script/command subprocesses.

        The optional presentation/routing fields (``agent_id``, ``model``,
        ``silent``, ``timezone``, ``strict_schedule``, ``hide_in_chat``) are set
        here so the job is persisted **fully-formed** in the single locked
        transaction. This closes a create-then-mutate-then-unlocked-``_save``
        window (two concurrent creates could otherwise interleave at the
        ``await`` and the unlocked save could clobber the other request's job).
        """
        valid_approval_modes = ("", "auto")
        if approval_mode not in valid_approval_modes:
            raise ValueError(f"Invalid approval_mode: {approval_mode!r}")
        # Message cap enforced HERE, at the persistence owner, for the same
        # reason as timezone/skip_dates below: every create path (MCP, apps
        # SDK, dashboard, CLI) shares one check, so no surface can admit a
        # message the others would reject. Uses the cron-specific cap, not
        # MAX_MEDIUM_STRING — a cron message is a task prompt (see
        # validation.MAX_CRON_MESSAGE). Type-checked first: len() succeeds on
        # a list, and a non-str message would be persisted into crons.json and
        # only blow up at fire time inside _build_prompt.
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        if len(message) > MAX_CRON_MESSAGE:
            raise ValueError(f"message exceeds max length {MAX_CRON_MESSAGE}")
        if timeout_secs and not 1 <= int(timeout_secs) <= 86400:
            raise ValueError(f"timeout_secs must be within 1..86400, got {timeout_secs}")
        if timeout_secs and (command or script):
            if timeout:
                _eff_sub = int(timeout)
            elif script:
                _eff_sub = 30
            else:
                _eff_sub = 300
            if int(timeout_secs) < _eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS:
                raise ValueError(
                    "timeout_secs (wake budget) must cover the command/script "
                    f"subprocess timeout plus cleanup: need >= "
                    f"{_eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS}, got {timeout_secs}. "
                    "A shorter wake budget cancels only the executor future — "
                    "the subprocess keeps running while the next wake launches "
                    "a duplicate."
                )
        if timezone and not is_valid_timezone(timezone):
            raise ValueError(f"Invalid timezone: {timezone!r}")
        skip_dates = skip_dates or []
        for _d in skip_dates:
            if not is_valid_skip_date(_d):
                raise ValueError(f"Invalid skip_date: {_d!r} (expected YYYY-MM-DD)")
        if cron_expr:
            if not validate_cron_expr(cron_expr):
                raise ValueError(f"Invalid cron expression: {cron_expr}")
            schedule = CronSchedule(kind="cron", cron_expr=cron_expr)
        elif every_secs:
            schedule = CronSchedule(kind="every", every_secs=max(every_secs, _MIN_INTERVAL_SECS))
        elif at_ts:
            schedule = CronSchedule(kind="at", at_ts=at_ts)
        else:
            raise ValueError("Must provide every_secs, at_ts, or cron_expr")

        return CronJob(
            id=uuid.uuid4().hex[:8],
            name=name,
            message=message,
            schedule=schedule,
            channel=channel,
            thread_ts=thread_ts,
            enabled=enabled,
            user_paused=not enabled,
            created_ts=time.time(),
            delete_after_run=delete_after_run,
            created_by=created_by,
            approval_mode=approval_mode,
            agent_id=agent_id,
            model=str(model or "").strip(),
            silent=silent,
            timezone=timezone,
            skip_dates=skip_dates,
            strict_schedule=strict_schedule,
            hide_in_chat=hide_in_chat,
            folder_id=folder_id,
            command=command,
            script=script,
            agent_sequence=list(agent_sequence) if agent_sequence else [],
            env=dict(env) if env else {},
            persistent_session=persistent_session,
            session_key=session_key,
            minimal_context=minimal_context,
            timeout=timeout,
            timeout_secs=int(timeout_secs) if timeout_secs else _JOB_TIMEOUT_SECS,
        )

    def _persist_add_locked(self, job: CronJob) -> None:
        """Lock/reload/append/save for a new job — the thread-safe disk core.

        Does NO timer work (``_arm_timer`` needs the event loop), so
        :meth:`add_job_async` can run it in an executor thread. Raises
        :class:`CronStoreBusy` if the store lock stays contended past the
        timeout. Mirrors the :meth:`_remove_jobs_locked` batch precedent.
        """
        with self._file_lock():
            self._sync()
            self._jobs.append(job)
            self._save()

    async def add_job_async(
        self,
        name: str,
        message: str,
        every_secs: int | None = None,
        at_ts: float | None = None,
        cron_expr: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        delete_after_run: bool = False,
        created_by: str = "",
        approval_mode: str = "",
        enabled: bool = True,
        agent_id: str = "",
        model: str = "",
        silent: bool = False,
        timezone: str = "",
        skip_dates: list[str] | None = None,
        strict_schedule: bool = False,
        hide_in_chat: bool = False,
        folder_id: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        session_key: str = "",
        minimal_context: bool = False,
        timeout: int = 0,
        timeout_secs: int = 0,
    ) -> CronJob:
        """Event-loop-safe :meth:`add_job`: the lock+save runs off the loop.

        The gateway's aiohttp/Slack handlers run on the sole asyncio event loop;
        calling the sync :meth:`add_job` there parks the loop in the bounded lock
        spin under contention. This builds+validates on the loop (no I/O),
        offloads the lock+persist to a worker thread via ``asyncio.to_thread``
        (the disk core is thread-safe — flock on separate fds mutually excludes
        in-process too), then re-arms the timer back on the loop. Raises
        :class:`CronStoreBusy` (retryable) on sustained contention; the public
        boundaries translate it to a clean 409 / structured error.

        Optional presentation/routing fields (``agent_id``, ``model``,
        ``silent``, ``timezone``, ``strict_schedule``, ``hide_in_chat``) are
        applied during the single locked build+persist so callers never need a
        follow-up unlocked ``_save()`` (which could race a concurrent create and
        drop a job).
        """
        job = self._build_job(
            name,
            message,
            every_secs=every_secs,
            at_ts=at_ts,
            cron_expr=cron_expr,
            channel=channel,
            thread_ts=thread_ts,
            delete_after_run=delete_after_run,
            created_by=created_by,
            approval_mode=approval_mode,
            enabled=enabled,
            agent_id=agent_id,
            model=model,
            silent=silent,
            timezone=timezone,
            skip_dates=skip_dates,
            strict_schedule=strict_schedule,
            hide_in_chat=hide_in_chat,
            folder_id=folder_id,
            command=command,
            script=script,
            agent_sequence=agent_sequence,
            env=env,
            persistent_session=persistent_session,
            session_key=session_key,
            minimal_context=minimal_context,
            timeout=timeout,
            timeout_secs=timeout_secs,
        )
        await asyncio.to_thread(self._persist_add_locked, job)
        self._arm_timer()
        logger.info("Added cron job '%s' (%s)", name, job.id)
        return job

    def update_job(self, job_id: str, **kwargs: Any) -> CronJob | None:
        """Update fields on an existing job. Returns updated job or None if not found.

        Accepted kwargs: name, message, every_secs, cron_expr, agent_id, channel,
        approval_mode, silent, skip_dates, timezone, thread_ts, model,
        timeout_secs (per-wake execution budget, 1..86400).

        Raises :class:`CronStoreBusy` if the store lock is contended past the
        timeout; see :meth:`update_job_async` for the event-loop-safe variant.
        """
        job = self._update_job_locked(job_id, **kwargs)
        if job is not None:
            self._arm_timer()
        return job

    async def update_job_async(self, job_id: str, **kwargs: Any) -> CronJob | None:
        """Event-loop-safe :meth:`update_job`: the lock+save runs off the loop.

        Offloads the lock/reload/mutate/save core to a worker thread, then
        re-arms the timer on the loop. Raises :class:`CronStoreBusy` (retryable)
        on sustained contention.
        """
        job = await asyncio.to_thread(self._update_job_locked_kw, job_id, kwargs)
        if job is not None:
            self._arm_timer()
        return job

    def _update_job_locked_kw(self, job_id: str, kwargs: dict[str, Any]) -> CronJob | None:
        """``asyncio.to_thread`` shim so kwargs cross the thread boundary as a dict."""
        return self._update_job_locked(job_id, **kwargs)

    def _update_job_locked(self, job_id: str, **kwargs: Any) -> CronJob | None:
        """Lock/reload/mutate/save core of :meth:`update_job` (no timer work).

        Returns the updated job, or ``None`` when the id is absent. Raises
        :class:`CronStoreBusy` on lock contention and ``ValueError`` on invalid
        input. Safe to run in an executor thread (does no ``_arm_timer``).
        """
        with self._file_lock():
            self._sync()
            for job in self._jobs:
                if job.id != job_id:
                    continue
                # Validate approval_mode if provided
                if "approval_mode" in kwargs:
                    valid_approval_modes = ("", "auto")
                    if kwargs["approval_mode"] not in valid_approval_modes:
                        raise ValueError(f"Invalid approval_mode: {kwargs['approval_mode']!r}")
                # Validate before any mutations
                if "message" in kwargs and kwargs["message"]:
                    # Same chokepoint rationale as _build_job: every update
                    # surface (MCP, dashboard PATCH, CLI) funnels here. Type
                    # first — len() succeeds on a list, which would then be
                    # persisted and only raise at fire time in _build_prompt.
                    if not isinstance(kwargs["message"], str):
                        raise ValueError("message must be a string")
                    if len(kwargs["message"]) > MAX_CRON_MESSAGE:
                        raise ValueError(f"message exceeds max length {MAX_CRON_MESSAGE}")
                if (
                    "cron_expr" in kwargs
                    and kwargs["cron_expr"]
                    and "every_secs" in kwargs
                    and kwargs["every_secs"]
                ):
                    raise ValueError("Cannot specify both cron_expr and every_secs")
                if "cron_expr" in kwargs and kwargs["cron_expr"]:
                    if not validate_cron_expr(kwargs["cron_expr"]):
                        raise ValueError(f"Invalid cron expression: {kwargs['cron_expr']}")
                if "every_secs" in kwargs and kwargs["every_secs"]:
                    try:
                        val = int(kwargs["every_secs"])
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"Invalid interval: {kwargs['every_secs']}") from e
                    if val < _MIN_INTERVAL_SECS:
                        raise ValueError(f"Interval must be >= {_MIN_INTERVAL_SECS}s, got {val}")
                # Calendar-validity of timezone / skip_dates, validated at the
                # persistence owner so EVERY caller (MCP cron_add/cron_update,
                # dashboard, CLI) is covered by one check rather than each
                # write path re-implementing it. The schema regex only checks
                # the YYYY-MM-DD shape, not that the date exists -- so
                # skip_dates=["2026-02-30"] would otherwise persist silently
                # and the skip would never match at fire time.
                if "timezone" in kwargs and kwargs["timezone"]:
                    if not is_valid_timezone(kwargs["timezone"]):
                        raise ValueError(f"Invalid timezone: {kwargs['timezone']!r}")
                if "skip_dates" in kwargs and kwargs["skip_dates"]:
                    for _d in kwargs["skip_dates"]:
                        if not is_valid_skip_date(_d):
                            raise ValueError(f"Invalid skip_date: {_d!r} (expected YYYY-MM-DD)")
                # Per-wake budget and subprocess timeout: validated HERE, in
                # the pre-mutation section with every other check, so a
                # rejected update cannot leave earlier field mutations (name,
                # message, ...) stranded on the in-memory job for a later
                # save to persist. Assignments happen below with the rest.
                _tsecs: int | None = None
                if "timeout_secs" in kwargs and kwargs["timeout_secs"] is not None:
                    try:
                        _tsecs = int(kwargs["timeout_secs"])
                    except (ValueError, TypeError) as e:
                        raise ValueError(
                            f"Invalid timeout_secs: {kwargs['timeout_secs']!r}"
                        ) from e
                    if not 1 <= _tsecs <= 86400:
                        raise ValueError(
                            f"timeout_secs must be within 1..86400, got {_tsecs}"
                        )
                # Script/command subprocess timeout. MCP cron_update passes this
                # field, so a branch has to consume it here — otherwise the
                # update is accepted and silently dropped.
                _tsub: int | None = None
                if "timeout" in kwargs and kwargs["timeout"] is not None:
                    try:
                        _tsub = int(kwargs["timeout"])
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"Invalid timeout: {kwargs['timeout']!r}") from e
                    if not 0 <= _tsub <= 86400:
                        raise ValueError(f"timeout must be within 0..86400, got {_tsub}")
                # Cross-field: the wake budget must cover the subprocess bound
                # plus cleanup, evaluated on the POST-update effective values —
                # the wake deadline cancels only the executor future, so a
                # shorter budget leaves the subprocess running while later
                # wakes launch duplicates.
                if job.command or job.script:
                    _eff_secs = _tsecs if _tsecs is not None else job.timeout_secs
                    _eff_sub_new = _tsub if _tsub is not None else job.timeout
                    if _eff_sub_new:
                        _eff_sub = int(_eff_sub_new)
                    elif job.script:
                        _eff_sub = 30
                    else:
                        _eff_sub = 300
                    if (_tsecs is not None or _tsub is not None) and _eff_secs < (
                        _eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS
                    ):
                        raise ValueError(
                            "timeout_secs (wake budget) must cover the "
                            "command/script subprocess timeout plus cleanup: "
                            f"need >= {_eff_sub + _SUBPROC_CLEANUP_ALLOWANCE_SECS}, "
                            f"got {_eff_secs}"
                        )
                if "name" in kwargs and kwargs["name"]:
                    job.name = kwargs["name"]
                if "message" in kwargs and kwargs["message"]:
                    job.message = kwargs["message"]
                if "agent_id" in kwargs:
                    job.agent_id = kwargs["agent_id"] or ""
                if "channel" in kwargs:
                    job.channel = kwargs["channel"] or None
                if "approval_mode" in kwargs:
                    job.approval_mode = kwargs["approval_mode"] or ""
                if "silent" in kwargs:
                    job.silent = bool(kwargs["silent"])
                if "skip_dates" in kwargs:
                    job.skip_dates = kwargs["skip_dates"] or []
                if "timezone" in kwargs:
                    job.timezone = kwargs["timezone"] or ""
                if "strict_schedule" in kwargs:
                    job.strict_schedule = bool(kwargs["strict_schedule"])
                if "persistent_session" in kwargs:
                    job.persistent_session = bool(kwargs["persistent_session"])
                if "minimal_context" in kwargs:
                    job.minimal_context = bool(kwargs["minimal_context"])
                if "hide_in_chat" in kwargs:
                    job.hide_in_chat = bool(kwargs["hide_in_chat"])
                if "folder_id" in kwargs:
                    job.folder_id = kwargs["folder_id"] or ""
                if "model" in kwargs:
                    job.model = str(kwargs["model"] or "").strip()
                # Per-wake budget (the asyncio.wait_for deadline in
                # _execute_with_timeout). Distinct from ``timeout``, which
                # bounds only script/command subprocesses. Until this branch
                # existed the field was read, clamped and persisted but
                # unreachable through every public writer — a job could only
                # ever carry the creation-time default (Phase 0, Intervention
                # 2: the operator had to edit the store under _file_lock by
                # hand to keep an agent alive).
                if _tsecs is not None:
                    job.timeout_secs = _tsecs
                if _tsub is not None:
                    job.timeout = _tsub

                # Schedule changes (already validated above)
                if "cron_expr" in kwargs and kwargs["cron_expr"]:
                    job.schedule = CronSchedule(kind="cron", cron_expr=kwargs["cron_expr"])
                elif "every_secs" in kwargs and kwargs["every_secs"]:
                    job.schedule = CronSchedule(kind="every", every_secs=int(kwargs["every_secs"]))
                self._save()
                logger.info("Updated cron job %s", job_id)
                return job
        return None

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID.

        Raises :class:`CronStoreBusy` on lock contention; see
        :meth:`remove_job_async` for the event-loop-safe variant.
        """
        ok = self._remove_job_locked(job_id)
        if ok:
            self._arm_timer()
        return ok

    async def remove_job_async(self, job_id: str) -> bool:
        """Event-loop-safe :meth:`remove_job`: the lock+save runs off the loop.

        Raises :class:`CronStoreBusy` (retryable) on sustained contention.
        """
        ok = await asyncio.to_thread(self._remove_job_locked, job_id)
        if ok:
            self._arm_timer()
        return ok

    def defer_removal(self, job_id: str) -> None:
        """Queue a one-shot job for removal on the next timer tick.

        Called on the event loop when an immediate :meth:`remove_job_async` for
        a completed ``delete_after_run`` / Done job raised :class:`CronStoreBusy`
        (the store lock stayed contended past the timeout). There is otherwise
        no caller to retry a fire-and-forget removal, so without this the
        finished job would linger ENABLED with its recurring schedule and
        re-fire on the next tick — duplicate execution and a duplicate
        user-visible notification.

        Two-layer guarantee:

        * **Immediate** — the job is disabled IN MEMORY right now so the very
          next :meth:`_on_timer` due-scan skips it (covers the window where the
          store is unchanged and ``_sync`` does not reload).
        * **Durable** — the id is recorded so :meth:`_drain_pending_removals_locked`,
          invoked from the timer tick's worker-thread transaction while it
          already holds the store lock (:meth:`_tick_scan_locked`), deletes it
          from disk. The drain runs BEFORE the due-scan, so even a
          ``_sync`` reload that re-enables the job (``enabled`` is derived from
          persisted pause flags, not the removal intent) cannot let it fire.

        Idempotent and cheap; safe to call for an id already queued.
        """
        for job in self._jobs:
            if job.id == job_id:
                job.enabled = False
                break
        self._pending_removals.add(job_id)

    def _drain_pending_removals_locked(self) -> None:
        """Delete jobs queued via :meth:`defer_removal`. MUST hold the store lock.

        Called from :meth:`_tick_scan_locked` (the timer tick's worker-thread
        transaction) inside its ``_file_lock`` block, so the delete+save is
        serialized against every other
        mutator exactly like the other locked cores. Removes only the queued
        ids still present after the tick's ``_sync``; saves once iff something
        was actually removed (an all-missing queue never rewrites the file).
        An id no longer present was already removed elsewhere, so dropping it
        is correct.

        Cross-thread safety: this drain runs in the timer tick's WORKER thread
        while :meth:`defer_removal` adds ids from the EVENT-LOOP thread. The
        queue is claimed with a single-bytecode tuple swap
        (``pending, self._pending_removals = self._pending_removals, set()``),
        which is atomic under the GIL. A concurrent ``defer_removal`` add
        therefore lands EITHER in ``pending`` (drained now) OR in the fresh
        replacement set (drained next tick) — it can never fall into the gap
        between a read and a reset and be silently erased. ``present`` is
        computed AFTER the swap so the intersection sees the post-swap job
        list, and the in-memory disable performed by ``defer_removal`` keeps
        even an id deferred to the next tick from re-firing meanwhile.
        """
        if not self._pending_removals:
            return
        # Atomic claim-and-reset (see docstring) — do NOT split into a read
        # (``& present``) followed by ``.clear()``; an id added between those
        # two steps would be erased without ever being deleted from disk, so
        # the completed one-shot would re-fire and re-notify.
        pending, self._pending_removals = self._pending_removals, set()
        present = {j.id for j in self._jobs}
        to_remove = pending & present
        if not to_remove:
            return
        self._jobs = [j for j in self._jobs if j.id not in to_remove]
        self._save()
        for jid in to_remove:
            logger.info("Removed deferred one-shot cron job %s", jid)

    def _remove_job_locked(self, job_id: str) -> bool:
        """Lock/reload/mutate/save core of :meth:`remove_job` (no timer work)."""
        with self._file_lock():
            self._sync()
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.id != job_id]
            if len(self._jobs) < before:
                self._save()
                logger.info("Removed cron job %s", job_id)
                return True
        return False

    def _remove_jobs_locked(self, job_ids: list[str]) -> tuple[list[str], list[str]]:
        """Sync core of :meth:`remove_jobs` — lock/reload/mutate/save only.

        Deliberately does NO timer work so it is safe to run in an executor
        thread (``_arm_timer`` needs the event loop). Cross-thread safety:
        every other store mutation also takes ``_file_lock`` — flock on
        separate fds mutually excludes within the process too — so a
        concurrent loop-side mutation blocks until this completes.
        """
        removed: list[str] = []
        missing: list[str] = []
        with self._file_lock():
            self._sync()
            present = {j.id for j in self._jobs}
            targets = set()
            for jid in job_ids:
                if jid in present:
                    removed.append(jid)
                    targets.add(jid)
                else:
                    missing.append(jid)
            if targets:
                self._jobs = [j for j in self._jobs if j.id not in targets]
                self._save()
                logger.info("Removed %d cron job(s) in batch", len(targets))
        return removed, missing

    async def remove_jobs(self, job_ids: list[str]) -> tuple[list[str], list[str]]:
        """Remove many jobs under ONE lock/reload/save, off the event loop.

        Returns ``(removed_ids, missing_ids)`` preserving input order. Looping
        :meth:`remove_job` per id would pay the file-lock + reload +
        full-serialize + atomic-write cost PER id on the event loop — with up to
        500 ids that starves every other gateway task (and on slow/network
        storage even one save can stall). The disk work
        runs in a worker thread; only ``_arm_timer`` (asyncio.create_task)
        runs back on the loop, and only when something was actually removed.
        """
        removed, missing = await asyncio.to_thread(self._remove_jobs_locked, list(job_ids))
        if removed:
            self._arm_timer()
        return removed, missing

    def remove_jobs_sync(self, job_ids: list[str]) -> tuple[list[str], list[str]]:
        """Synchronous sibling of :meth:`remove_jobs` — ONE atomic locked batch.

        Removes every id in ``job_ids`` under a SINGLE :meth:`_remove_jobs_locked`
        lock/reload/save transaction (not a per-id loop), so a contended store
        either removes them all or removes none and raises :class:`CronStoreBusy`
        — there is no partial-removal state that could leave some jobs orphaned
        and still enabled. Returns ``(removed_ids, missing_ids)``.

        Synchronous: only for loop-less callers / the offloaded ``CronSDK``
        facade (the ``_file_lock`` loop-safety guard rejects it on a running
        loop). On the loop use :meth:`remove_jobs`.
        """
        removed, missing = self._remove_jobs_locked(list(job_ids))
        if removed:
            self._arm_timer()
        return removed, missing

    def _remove_jobs_by_owner_locked(self, owner_prefix: str) -> list[str]:
        """Select AND remove every job owned by ``owner_prefix`` under ONE lock.

        Sync core of :meth:`remove_jobs_by_owner` — lock/reload/select/mutate/
        save only, no timer work (so it is safe in an executor thread;
        ``_arm_timer`` needs the event loop). The critical property over
        passing in a pre-computed id list: the ownership SELECTION happens
        AFTER the in-lock ``_sync()`` reload, against the authoritative on-disk
        state — not against a possibly-stale in-memory/cache snapshot taken
        before the lock. A job created by this owner in another process since
        the last cache refresh is therefore still seen and removed, closing the
        cross-process orphan window where a cache-only ``list_jobs()`` id
        snapshot would miss it and leave it ENABLED after the app is deleted.

        All-or-nothing within the single ``_file_lock`` transaction: a contended
        store raises :class:`CronStoreBusy` before any mutation. Returns the
        list of removed ids.
        """
        removed: list[str] = []
        with self._file_lock():
            self._sync()
            removed = [j.id for j in self._jobs if getattr(j, "created_by", "") == owner_prefix]
            if removed:
                targets = set(removed)
                self._jobs = [j for j in self._jobs if j.id not in targets]
                self._save()
                logger.info("Removed %d cron job(s) owned by %s", len(removed), owner_prefix)
        return removed

    async def remove_jobs_by_owner(self, owner_prefix: str) -> list[str]:
        """Remove every job owned by ``owner_prefix`` under ONE lock, off-loop.

        Selects and removes in a SINGLE :meth:`_remove_jobs_by_owner_locked`
        lock/reload/select/save transaction — the owner scan runs against the
        in-lock reloaded on-disk state, so a job another process created for
        this owner since the last cache refresh is still removed (no
        cross-process orphan window). All-or-nothing; propagates
        :class:`CronStoreBusy` on a contended store. The disk work runs in a
        worker thread; only ``_arm_timer`` runs back on the loop, and only when
        something was actually removed. Returns the removed ids.
        """
        removed = await asyncio.to_thread(self._remove_jobs_by_owner_locked, owner_prefix)
        if removed:
            self._arm_timer()
        return removed

    def remove_jobs_by_owner_sync(self, owner_prefix: str) -> list[str]:
        """Synchronous sibling of :meth:`remove_jobs_by_owner` — ONE atomic
        locked select+remove batch.

        Selects and removes every job whose ``created_by == owner_prefix``
        under a SINGLE :meth:`_remove_jobs_by_owner_locked` transaction (the
        owner scan runs on the in-lock reloaded state, so a cross-process
        creation is still caught). All-or-nothing; raises
        :class:`CronStoreBusy` on a contended store.

        Synchronous: only for loop-less callers / the offloaded ``CronSDK``
        facade (the ``_file_lock`` loop-safety guard rejects it on a running
        loop). On the loop use :meth:`remove_jobs_by_owner`. Returns the removed
        ids.
        """
        removed = self._remove_jobs_by_owner_locked(owner_prefix)
        if removed:
            self._arm_timer()
        return removed

    def enable_job(self, job_id: str, enabled: bool = True) -> bool:
        """Enable or disable a job by ID.

        Raises :class:`CronStoreBusy` on lock contention; see
        :meth:`enable_job_async` for the event-loop-safe variant.
        """
        ok = self._enable_job_locked(job_id, enabled)
        if ok:
            self._arm_timer()
        return ok

    async def enable_job_async(self, job_id: str, enabled: bool = True) -> bool:
        """Event-loop-safe :meth:`enable_job`: the lock+save runs off the loop.

        Raises :class:`CronStoreBusy` (retryable) on sustained contention.
        """
        ok = await asyncio.to_thread(self._enable_job_locked, job_id, enabled)
        if ok:
            self._arm_timer()
        return ok

    def _enable_job_locked(self, job_id: str, enabled: bool = True) -> bool:
        """Lock/reload/mutate/save core of :meth:`enable_job` (no timer work)."""
        with self._file_lock():
            self._sync()
            for job in self._jobs:
                if job.id == job_id:
                    job.user_paused = not enabled
                    job.enabled = enabled
                    # Re-enabling clears an execution auto-pause; without this a
                    # job auto-paused after failures would be re-derived as
                    # disabled on the next reload despite the explicit resume.
                    if enabled and job.auto_paused:
                        job.auto_paused = False
                        # Reset the counter too: the user re-enabled expecting a
                        # fresh set of attempts. Left at the threshold, the very
                        # next failure would immediately re-auto-pause the job
                        # (consecutive_failures already >= threshold). Mirrors
                        # record_success, which resets the counter on recovery.
                        job.consecutive_failures = 0
                        # A user resume that lifts an auto-pause restores execute
                        # permission — audit it like the auto-pause transition.
                        job._audit_pause_change("auto_pause_cleared")
                    self._save()
                    logger.info("%s cron job %s", "Enabled" if enabled else "Disabled", job_id)
                    return True
        return False

    def ack_job(self, job_id: str, summary: str) -> bool:
        """Acknowledge a cron notification — stores summary for future context.

        Raises :class:`CronStoreBusy` on lock contention; see
        :meth:`ack_job_async` for the event-loop-safe variant.
        """
        return self._ack_job_locked(job_id, summary)

    async def ack_job_async(self, job_id: str, summary: str) -> bool:
        """Event-loop-safe :meth:`ack_job`: the lock+save runs off the loop.

        Raises :class:`CronStoreBusy` (retryable) on sustained contention.
        """
        ok = await asyncio.to_thread(self._ack_job_locked, job_id, summary)
        # ack itself changes no schedule. If the worker's _sync() reloaded an
        # external change, its _load() re-armed the timer thread-safely via the
        # bound loop (see _arm_timer) — no drain needed here.
        return ok

    def _ack_job_locked(self, job_id: str, summary: str) -> bool:
        """Lock/reload/mutate/save core of :meth:`ack_job`."""
        with self._file_lock():
            self._sync()
            for job in self._jobs:
                if job.id == job_id:
                    job.acked_items.append(summary[:500])
                    # Keep only last 20 acks
                    job.acked_items = job.acked_items[-20:]
                    self._save()
                    return True
        return False

    def unack_job(self, job_id: str) -> bool:
        """Remove the most recent acked item from a cron job.

        Raises :class:`CronStoreBusy` on lock contention; see
        :meth:`unack_job_async` for the event-loop-safe variant.
        """
        return self._unack_job_locked(job_id)

    async def unack_job_async(self, job_id: str) -> bool:
        """Event-loop-safe :meth:`unack_job`: the lock+save runs off the loop.

        Raises :class:`CronStoreBusy` (retryable) on sustained contention.
        """
        ok = await asyncio.to_thread(self._unack_job_locked, job_id)
        # See ack_job_async: any external-change re-arm self-heals in the worker.
        return ok

    def _unack_job_locked(self, job_id: str) -> bool:
        """Lock/reload/mutate/save core of :meth:`unack_job`."""
        with self._file_lock():
            self._sync()
            for job in self._jobs:
                if job.id == job_id and job.acked_items:
                    job.acked_items.pop()
                    self._save()
                    return True
        return False

    # ── Active session tracking ──

    def register_active_session_key(self, job_id: str, session_key: str) -> None:
        """Record the session key used by the current run of ``job_id``.

        The dispatcher calls this at the start of each run. The reaper reads
        it when force-killing a timed-out job. Overwrites any existing entry
        for the same job_id (prior run already ended or was reaped).
        """
        self._active_session_keys[job_id] = session_key

    def clear_active_session_key(self, job_id: str) -> None:
        """Clear the active session key for ``job_id``.

        Called by the dispatcher in its finally/cleanup path so the reaper
        falls back to the stable key for the next (not yet started) run.
        """
        self._active_session_keys.pop(job_id, None)

    def get_active_session_key(self, job_id: str) -> str | None:
        """Return the active session key for ``job_id``, or None if unregistered."""
        return self._active_session_keys.get(job_id)

    def get_history(self) -> CronHistoryStore:
        """Public accessor for the history store."""
        return self._history

    def is_running(self, job_id: str) -> bool:
        """Return whether a job is currently executing."""
        return job_id in self._executing

    def running_since(self, job_id: str) -> float | None:
        """Return the epoch start time of a running job, or None."""
        return self._job_start_times.get(job_id)

    def set_refresh_callback(self, cb: Any) -> None:
        """Set the dashboard refresh callback."""
        self._push_refresh = cb

    async def run_job(self, job_id: str) -> bool:
        """Manually trigger a job via _run_job_isolated (records history)."""
        # Refresh the store off the loop, then resolve + claim on the loop.
        #
        # The locked _sync() + snapshot runs in a worker thread (_synced_snapshot
        # via asyncio.to_thread) so a manual trigger never pays the whole-file
        # read_bytes() + blake2b hash of crons.json on the event loop.
        # _executing / _job_run_meta are loop-owned, so the find + claim stays
        # on the loop, with NO await between the snapshot read and the claim so
        # it is atomic against every other loop task (the timer due-scan).
        #
        # One residual, benign race remains against the batch-remove worker: it
        # may delete the job on its own thread in the instant between our
        # snapshot and our spawn, so a manual run can execute a just-removed job
        # ONCE (non-destructive — it is not persisted, and the next scan won't
        # see it). The batch-remove worker holds the SAME flock, so a lock-held
        # claim could not observe a delete mid-way regardless. Degrades to the
        # in-memory snapshot under lock contention.
        snapshot = await asyncio.to_thread(self._synced_snapshot, True)
        job = next((j for j in snapshot if j.id == job_id), None)
        if not job:
            return False
        if job.id in self._executing:
            return False
        self._job_run_meta[job.id] = (time.time(), "manual")
        self._executing.add(job.id)
        task = asyncio.create_task(self._run_job_isolated(job))
        self._running_tasks[job.id] = task
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise  # outer coroutine was cancelled, propagate
        finally:
            if task.done():
                self._executing.discard(job.id)
                self._running_tasks.pop(job.id, None)
        return True

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List jobs from the in-memory snapshot — CACHE-ONLY, never touches disk.

        This is a hot path: it is called directly on the gateway event loop by
        the dashboard WebSocket status push, the dashboard REST handlers, the
        Slack handlers, the apps SDK, and MCP tools. It performs NO filesystem
        I/O — no lock-file open, no ``read_bytes()``, no digest hash — so a
        large ``crons.json`` can never freeze the loop with synchronous I/O
        (the ``no-blocking-call-on-event-loop`` rule). ``list(self._jobs)`` is
        never torn: CPython swaps the list reference atomically.

        Cross-process freshness is maintained OFF the loop: the timer tick
        (``_on_timer``, every ≤``_TIMER_POLL_SECS``) and every mutator
        ``_sync()`` the in-memory snapshot under the store lock, so an external
        write is picked up within one poll interval. Callers that need to
        observe a cross-process write *immediately* use :meth:`list_jobs_async`,
        which offloads a locked ``_sync()`` + snapshot to a worker thread.
        """
        return self._snapshot(list(self._jobs), include_disabled)

    def count_enabled_from_disk(self) -> int:
        """Count enabled jobs by reading ``crons.json`` directly — thread-safe.

        Unlike :meth:`list_jobs` (which calls ``_sync()`` → ``_load()`` →
        ``_arm_timer()``), this performs ONLY a read-only file parse. It never
        mutates loop-owned state (``self._jobs``, ``self._last_mtime``) and
        never touches the asyncio timer, so it is safe to invoke from a worker
        thread via ``asyncio.to_thread``.

        This exists specifically for the dashboard WS status pusher, which needs
        an enabled-job count off the event loop: routing ``list_jobs`` through a
        worker thread would run ``_arm_timer()`` (which calls
        ``asyncio.create_task``) with no running loop in that thread, raising
        ``RuntimeError`` — and because ``_arm_timer`` cancels the existing timer
        first, that would silently stop all scheduled jobs until restart.

        Enabled semantics come from the shared ``_record_is_enabled`` predicate
        (the single owner used by ``_load`` too): a job is enabled when it is
        neither user-paused nor auto-paused (with the legacy ``!enabled``
        fallback for stores written before those fields existed). A slightly stale count is
        acceptable here — the caller caches it and the atomic tmp→rename write
        in ``_save`` guarantees a concurrent read sees a whole file, never a
        partial one.
        """
        if not self._path.exists():
            return 0
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        records = data.get("jobs", []) if isinstance(data, dict) else None
        if not isinstance(records, list):
            # Mirrors _load's document guard (#4674): a parseable non-object
            # or a non-list jobs value ({"jobs": null}) would raise an
            # uncaught AttributeError/TypeError past the handler above and
            # kill the WS status pusher calling this reader.
            return 0
        count = 0
        for j in records:
            # Same skip decision as _load (#4664): a record _job_from_record
            # rejects is not a schedulable job, so it must not be counted —
            # and a non-dict entry would otherwise crash _record_is_enabled
            # with an AttributeError this method's handler does not catch,
            # silently killing the WS status pusher that calls this reader.
            try:
                _job_from_record(j)
            except (KeyError, TypeError, AttributeError):
                continue
            if _record_is_enabled(j):
                count += 1
        return count

    def get_job(self, job_id: str) -> CronJob | None:
        """Find a job by its id in the in-memory snapshot — CACHE-ONLY, no disk I/O.

        See :meth:`list_jobs` for the cache-only rationale and the
        off-loop freshness contract. Use :meth:`get_job_async` when a
        guaranteed cross-process-fresh read is required.
        """
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    @staticmethod
    def _snapshot(jobs: list[CronJob], include_disabled: bool) -> list[CronJob]:
        """Filter a job snapshot by the ``include_disabled`` flag."""
        if include_disabled:
            return jobs
        return [j for j in jobs if j.enabled]

    def _synced_snapshot(self, include_disabled: bool) -> list[CronJob]:
        """Refresh from disk under the store lock, then snapshot. WORKER-THREAD ONLY.

        Runs the blocking read/hash/parse + bounded lock spin OFF the event
        loop (via :meth:`list_jobs_async` / :meth:`get_job_async` /
        :meth:`run_job` -> ``asyncio.to_thread``). Degrades to the current
        in-memory snapshot if the store is too contended to lock, so a read
        never raises :class:`CronStoreBusy` into a caller. A worker-thread
        ``_sync()`` may reach ``_arm_timer``, which hands the (re)arm back to
        the bound event loop thread-safely (see :meth:`_arm_timer`), so no
        caller-side drain is required.
        """
        try:
            with self._file_lock():
                self._sync()
        except CronStoreBusy:
            pass  # too contended for a guaranteed-fresh read — use the cache
        return self._snapshot(list(self._jobs), include_disabled)

    async def list_jobs_async(self, include_disabled: bool = False) -> list[CronJob]:
        """Freshness-guaranteed :meth:`list_jobs`: offloads a locked sync to a worker.

        For the rare loop-side caller that must observe a write made by another
        process (CLI/MCP) *right now* rather than within the ≤``_TIMER_POLL_SECS``
        timer refresh. The read/hash/parse and the bounded lock spin run in an
        ``asyncio.to_thread`` worker so the event loop is never blocked; the
        deferred timer arm (if the worker's ``_sync()`` reloaded an external
        change) is drained back on the loop.
        """
        jobs = await asyncio.to_thread(self._synced_snapshot, include_disabled)
        return jobs

    async def get_job_async(self, job_id: str) -> CronJob | None:
        """Freshness-guaranteed :meth:`get_job` — see :meth:`list_jobs_async`."""
        jobs = await asyncio.to_thread(self._synced_snapshot, True)
        for job in jobs:
            if job.id == job_id:
                return job
        return None

    def status(self) -> dict[str, Any]:
        """Service status summary."""
        return {
            "running": self._running,
            "jobs": len(self._jobs),
            "enabled": sum(1 for j in self._jobs if j.enabled),
        }

    # ── Timer ──

    def _next_wake_secs(self) -> float | None:
        """Compute seconds until the next job should fire."""
        now = time.time()
        delays: list[float] = []
        for job in self._jobs:
            if not job.enabled or job.id in self._executing:
                continue
            if job.schedule.kind == "every" and job.schedule.every_secs:
                last = job.last_run_ts or job.created_ts
                next_run = last + job.schedule.every_secs
                delays.append(max(0.0, next_run - now))
            elif job.schedule.kind == "at" and job.schedule.at_ts:
                delays.append(max(0.0, job.schedule.at_ts - now))
            elif job.schedule.kind == "cron":
                # Poll every _TIMER_POLL_SECS for cron expressions
                delays.append(_TIMER_POLL_SECS)
        return min(delays) if delays else None

    def _effective_delay(self) -> float:
        """Compute the actual timer delay, capped at poll interval.

        Ensures the timer always wakes within _TIMER_POLL_SECS to _sync()
        externally-added jobs, even when the next job is far in the future.
        """
        delay = self._next_wake_secs()
        if delay is None:
            return _TIMER_POLL_SECS
        if self._admission_deferring and delay < _TIMER_POLL_SECS:
            # A critical-posture episode leaves deferred ``every``/``at``
            # jobs overdue (``last_run_ts`` untouched by design), which
            # would otherwise re-arm the timer at zero delay — a busy loop
            # of scans and admission probes on a host already under memory
            # pressure. Back off to the poll cadence: the episode is
            # re-evaluated (and deferred jobs fire) within one poll of
            # posture recovery.
            return _TIMER_POLL_SECS
        return min(delay, _TIMER_POLL_SECS)

    def _arm_timer(self) -> None:
        # Re-arming creates/cancels asyncio tasks, which is only legal on the
        # event loop thread. When this is reached OFF the loop — a locked core
        # running in an asyncio.to_thread worker whose _sync()->_load() wants to
        # re-arm, an app-hook/SDK mutation offloaded via asyncio.to_thread, or a
        # purely synchronous (CLI/test) context — there is no running loop here.
        # Creating a task would raise RuntimeError, and a blind cancel could
        # stop the existing timer WITHOUT rearming it, silently halting every
        # scheduled job. So off-loop we cancel/create nothing and instead hand
        # the arm back to the bound event loop (captured in create()/start())
        # via loop.call_soon_threadsafe(self._arm_timer): the arm then runs ON
        # the loop and (re)arms for real. Arming is thus owned by the service —
        # no caller has to remember a drain step. In a genuinely loop-less
        # process (self._loop is None) there is no scheduler to arm.
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            bound = self._loop
            if bound is not None and not bound.is_closed():
                bound.call_soon_threadsafe(self._arm_timer)
            return
        current = asyncio.current_task()
        # Never cancel the timer task if we ARE that task. The tick's own
        # `finally` re-arms while the tick coroutine is still executing, so a
        # blind `self._timer_task.cancel()` there fires a CancelledError back
        # into the running tick — aborting the in-flight `_on_timer` dispatch
        # (dropping any due jobs not yet spawned) and leaving a half-processed
        # sweep. We skip the cancel in that self-referential case and simply
        # create the replacement task below; the finishing tick exits normally.
        # A *different* caller rescheduling while the tick merely waits on
        # shutdown_event still cancels correctly (current is not the timer task).
        #
        # A DIFFERENT hazard, same root cause, needs a second guard: a job's
        # own completion handler calls _arm_timer() (see _run_job_isolated) to
        # re-arm promptly instead of waiting out the rest of the poll cap, but
        # that call runs on the JOB's task, not the timer's — so `current is
        # not self._timer_task` above is true even while _on_timer is still
        # mid-sweep (yielded at its own to_thread scan). Cancelling here would
        # abort that sweep exactly as the self-referential case above
        # describes, just reached from a different caller. Neither cancelling
        # NOR creating a replacement task is safe in that window (creating one
        # too would leave two timer tasks alive and double-fire the next
        # tick), so this arm is dropped entirely: _on_timer's own tick already
        # unconditionally re-arms in its `finally` once the sweep completes
        # (by which point the completed job is no longer in self._executing),
        # so the delay this caller wanted still gets picked up, just a moment
        # later rather than being computed twice.
        if self._on_timer_running and current is not self._timer_task:
            return
        if self._timer_task and not self._timer_task.done() and self._timer_task is not current:
            self._timer_task.cancel()
        if not self._running:
            return
        delay = self._effective_delay()

        logger.debug("Cron: next timer in %.1fs", delay)

        async def _tick() -> None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
                return  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # normal wake-up
            if self._running:
                try:
                    await self._on_timer()
                except Exception:
                    logger.exception("Cron timer error — will re-arm")
                finally:
                    # Always re-arm, even after errors
                    if self._running:
                        self._arm_timer()

        self._timer_task = asyncio.create_task(_tick())

    def _tick_scan_locked(self) -> list[CronJob]:
        """Locked store refresh + deferred-removal drain + snapshot. WORKER-THREAD ONLY.

        The timer tick's blocking work — the bounded ``_file_lock`` spin, the
        ``_sync()`` that ``read_bytes()`` + blake2b-hashes the WHOLE
        ``crons.json``, and the deferred one-shot delete+save — runs here so it
        can be offloaded off the event loop via ``asyncio.to_thread`` (see
        :meth:`_on_timer`). Returns a snapshot of the current jobs; the loop
        then runs the mutation-free, ``_executing``-aware due-scan against it.

        Drains deferred removals BEFORE snapshotting so a completed
        ``delete_after_run`` job whose immediate removal hit a busy store (see
        :meth:`defer_removal`) is deleted here and can never appear due — even
        though the ``_sync`` above may have re-derived it as enabled. If the
        store is too contended to lock this tick, degrades to the in-memory
        snapshot without draining (the next tick retries; ``defer_removal``'s
        in-memory disable keeps a completed one-shot from re-firing meanwhile).
        A worker-thread ``_sync()`` reload may reach ``_arm_timer``, which hands
        the (re)arm back to the bound event loop thread-safely (see
        :meth:`_arm_timer`) — no caller-side drain is required.
        """
        try:
            with self._file_lock():
                self._sync()
                self._drain_pending_removals_locked()
        except CronStoreBusy:
            logger.debug("Cron timer tick: store busy, using in-memory snapshot")
        return list(self._jobs)

    async def _on_timer(self) -> None:
        """Fire due jobs as independent tasks (non-blocking).

        The locked store refresh + deferred-removal drain + snapshot is
        offloaded to a worker thread (:meth:`_tick_scan_locked`) so a large or
        slow ``crons.json`` can never freeze the gateway loop with the
        ``_sync()`` ``read_bytes()`` + blake2b hash on every tick (the
        ``no-blocking-call-on-event-loop`` rule). The mutation-free due-scan —
        which reads loop-owned ``self._executing`` — then runs on the loop
        against the returned snapshot.

        Brackets the whole body with ``self._on_timer_running`` so a job
        completing during either ``to_thread`` await below (the scan, and the
        admission check) cannot have its ``_arm_timer()`` call cancel
        ``self._timer_task`` out from under this sweep — see ``_arm_timer``.
        """
        self._on_timer_running = True
        try:
            snapshot = await asyncio.to_thread(self._tick_scan_locked)
            now = time.time()
            due = [
                j
                for j in snapshot
                if j.enabled and j.id not in self._executing and self._is_due(j, now)
            ]

            # An empty due-scan can only end the tick when no deferral episode is
            # in progress: the recovery log (below) must still fire on a quiet
            # tick, otherwise an episode that ends during a lull is never closed.
            if not due and not self._admission_deferring:
                return

            # Posture-gated admission: while host memory is CRITICAL, defer this
            # tick's ``every``/``at`` firings instead of admitting more work onto
            # a host that cannot absorb it. The verdict is computed off-loop
            # (config + procfs reads must not stall the event loop). Deferral is
            # deliberately STATELESS and only applies to schedule kinds that stay
            # due on their own (``last_run_ts`` untouched, so a deferred job fires
            # on the first admitted tick). A cron-expression job is only due while
            # its expression matches the current minute, so it cannot be deferred
            # statelessly: an in-memory catch-up marker loses the occurrence on
            # gateway restart, and dropping it silently loses the occurrence
            # outright — so cron-expression jobs run normally even under critical
            # posture (persisted deferral markers are a possible follow-up).
            # Manual runs (run_job / cron_trigger) never pass through this scan
            # and are not deferred. Fails open on unknown posture. The INFO log
            # fires once per deferral episode and re-fires every 15 minutes so a
            # long suspension stays diagnosable.
            decision = await asyncio.to_thread(admission_check)

            # The admission await yielded the loop, so the due snapshot may be
            # stale: a manual run (run_job / cron_trigger) can have claimed — or
            # even completed — a job meanwhile, and a job can have been edited,
            # disabled, or queued for removal. Rebuild the due list from the LIVE
            # job objects and re-run the due check BEFORE the deferral partition
            # below: classifying by the stale snapshot's schedule kind would let
            # an interval job edited into a matching cron expression during the
            # await be deferred-and-dropped (its occurrence lost), and dispatching
            # the snapshot object would execute a stale definition. An id-only
            # check would double-fire a job whose manual run already finished.
            # The re-check deliberately reuses the scan-time ``now``: a live
            # ``last_run_ts`` advanced by a finished manual run still fails it
            # (interval math for ``every``/``at``, the same-minute guard for cron
            # expressions), while a minute boundary crossed during the await
            # cannot drop a cron-expression occurrence that was genuinely due at
            # scan time.
            live_by_id = {j.id: j for j in self._jobs if j.enabled}
            due = [
                live_by_id[j.id]
                for j in due
                if j.id in live_by_id
                and j.id not in self._executing
                and j.id not in self._pending_removals
                and self._is_due(live_by_id[j.id], now)
            ]

            if not decision.admitted:
                deferred = [j for j in due if j.schedule.kind != "cron"]
                due = [j for j in due if j.schedule.kind == "cron"]
                now_mono = time.monotonic()
                if not self._admission_deferring:
                    self._admission_deferring = True
                    self._admission_last_log = now_mono
                    logger.info(
                        "Cron: deferring interval/one-shot firings (%d deferred "
                        "this tick; cron-expression jobs run normally) — %s "
                        "(re-logged every 15 min while the episode lasts)",
                        len(deferred),
                        decision.reason,
                    )
                elif now_mono - self._admission_last_log >= 900.0:
                    self._admission_last_log = now_mono
                    logger.info(
                        "Cron: STILL deferring interval/one-shot firings (%d "
                        "deferred this tick) — %s",
                        len(deferred),
                        decision.reason,
                    )
                else:
                    logger.debug(
                        "Cron: still deferring %d scheduled job(s)", len(deferred)
                    )
            elif self._admission_deferring:
                self._admission_deferring = False
                logger.info(
                    "Cron: memory posture recovered — resuming scheduled firings"
                )

            if not due:
                return

            # Fire each job independently — one hung job never blocks others.
            for j in due:
                self._executing.add(j.id)
                self._job_run_meta.setdefault(j.id, (time.time(), "scheduled"))
                task = asyncio.create_task(self._run_job_isolated(j))
                self._running_tasks[j.id] = task
        finally:
            self._on_timer_running = False

    async def _run_job_isolated(self, job: CronJob) -> None:
        """Execute a single job and merge results back to disk."""
        meta = self._job_run_meta.get(job.id)
        started_at = meta[0] if meta else time.time()
        trigger = meta[1] if meta else "scheduled"
        self._job_start_times[job.id] = started_at
        # Apply jitter to spread execution unless strict_schedule is set or manual
        jitter = self._compute_jitter(job) if trigger != "manual" else 0
        self._job_jitter[job.id] = jitter
        # Provisional; refined once the jitter sleep completes. Only read on
        # the history path, which a cancelled-during-jitter run never reaches.
        exec_started_at = started_at
        # ``last_result`` is a cross-run context-carry field for AGENT jobs
        # (see build_cron_session_context): result-less runs leave the
        # previous value in place so the next run's prompt keeps its dedup
        # context. Command and script jobs have theirs cleared once in the
        # finally below, because the prompt built for them is never dispatched. The history recorder in
        # the finally block must NOT attribute that carried-over value to
        # THIS run, so clear the freshness marker here; executor callbacks
        # set it via CronJob.set_run_result() when the run actually produces
        # a result. (String identity/equality can't stand in for the marker:
        # CPython interns equal literals and caches single-char strings, so
        # a run re-producing the previous text looks identical to one that
        # produced nothing.)
        job.result_produced = False
        being_cancelled = False
        try:
            # The jitter sleep MUST live inside this try: hourly/daily jobs
            # sleep up to 59 min here, and a user cancel() during that window
            # raises CancelledError at the sleep — if that happened BEFORE the
            # try, the finally below would never run, leaking the
            # _cancelled_jobs marker (and the rest of the bookkeeping) so the
            # job's NEXT run would see the stale marker and silently drop its
            # real result as "cancelled".
            if jitter > 0:
                logger.debug("Cron: applying %.0fs jitter to job '%s'", jitter, job.name)
                await asyncio.sleep(jitter)
            exec_started_at = time.time()
            # Notify dashboard that the job has started executing so the live
            # is_running badge appears without a manual reload.
            try:
                if self._push_refresh:
                    self._push_refresh("crons")
            except Exception:
                logger.debug("push_refresh failed on job start", exc_info=True)
            await self._execute_with_timeout(job)
        except asyncio.CancelledError:
            # stop() cancels this task WITHOUT marking _cancelled_jobs, so the
            # finally must know not to clear the last completed run's result.
            being_cancelled = True
            raise
        finally:
            finished_at = time.time()
            self._job_start_times.pop(job.id, None)
            self._job_jitter.pop(job.id, None)
            self._job_run_meta.pop(job.id, None)
            reaped = job.id in self._reaped_jobs
            self._reaped_jobs.discard(job.id)
            cancelled = job.id in self._cancelled_jobs
            self._cancelled_jobs.discard(job.id)
            self._executing.discard(job.id)
            self._running_tasks.pop(job.id, None)
            # Notify dashboard that the job has finished (clears the badge).
            try:
                if self._push_refresh:
                    self._push_refresh("crons")
            except Exception:
                logger.debug("push_refresh failed on job end", exc_info=True)
            # For 'every' jobs, use started_at to prevent cumulative drift
            if not reaped and not cancelled and job.schedule.kind == "every":
                job.last_run_ts = started_at
            if not reaped and not cancelled:
                # One clear per result-less run. Scattering it over exit sites is
                # what let the fire-time deny and script Skip paths keep a result.
                if (job.command or job.script) and not being_cancelled:
                    job.clear_carried_result()
                try:
                    # Offload the lock+sync+save merge to a worker thread:
                    # _merge_job_result enters the bounded sync _file_lock,
                    # whose spin does time.sleep(poll) for up to
                    # _FILE_LOCK_TIMEOUT_SECS under contention. Calling it
                    # directly here — on the gateway event loop, since
                    # _run_job_isolated is a loop task — would park the whole
                    # loop (chat, heartbeat, timer) for that window. to_thread
                    # is safe for the same reason the batch-remove path uses it
                    # (flock on separate fds mutually excludes in-process too,
                    # and the self._jobs reassignment is an atomic reference
                    # swap). CronStoreBusy (a TimeoutError) on sustained
                    # contention is caught below and logged — the merge is
                    # best-effort and the next run / reaper re-persists.
                    await asyncio.to_thread(self._merge_job_result, job)
                except Exception:
                    logger.exception("Failed to merge result for job '%s'", job.name)
                # Record history
                try:
                    status = "success" if job.last_status == "ok" else "failure"
                    # Attribute last_result to this run only if the run
                    # actually produced it (set_run_result sets the marker).
                    # Reading it unconditionally recorded the PREVIOUS run's
                    # result as this run's summary/trace whenever the run
                    # ended without producing one (observed in the wild: a
                    # timed-out run's history row carried the prior success's
                    # summary verbatim — fabricated history on a
                    # status=failure record).
                    run_result = job.last_result if job.result_produced else None
                    record = CronRunRecord(
                        job_id=job.id,
                        trigger=trigger,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=int((finished_at - exec_started_at) * 1000),
                        status=status,
                        summary=(run_result or job.last_error or "")[:200],
                        trace=run_result or "",
                        error=job.last_error or "",
                    )
                    await self._history.append(record)
                    if self._push_refresh:
                        self._push_refresh("cron_history")
                except Exception:
                    logger.exception("Failed to record history for job '%s'", job.name)
            # Re-arm now rather than waiting for whatever wake was already
            # armed: a job that ran for most of its interval was invisible to
            # every _next_wake_secs() computed while self._executing held it
            # (see _next_wake_secs), so the armed delay can be stale by up to
            # _TIMER_POLL_SECS by the time this job becomes due again. Placed
            # at the very end, after last_run_ts/history are settled, so the
            # delay this computes reflects this run's actual outcome. Safe to
            # call unconditionally (also when reaped/cancelled, or when
            # nothing changed): _arm_timer() itself no-ops when the service
            # isn't running, and the self._on_timer_running guard there
            # covers the one case where this job's own completion happens to
            # race an in-flight dispatch sweep.
            if self._running:
                self._arm_timer()

    @staticmethod
    def _compute_jitter(job: CronJob) -> float:
        """Return random jitter seconds based on schedule frequency.

        - strict_schedule=True or one-shot 'at' jobs: no jitter
        - Sub-hourly (every < 3600s or cron with /, , or * in minute field): no jitter
        - Hourly (every 3600–86399s or cron firing hourly): 0–5 min
        - Daily (every >= 86400s or cron firing daily): 0–59 min
        - Unrecognized cron patterns (fallback): 0–5 min
        """
        if job.strict_schedule:
            return 0.0
        sched = job.schedule
        if sched.kind == "at":
            return 0.0  # one-shot jobs fire at exact time
        if sched.kind == "every" and sched.every_secs:
            if sched.every_secs >= 86400:
                return random.uniform(0, _JITTER_DAILY_MAX)
            elif sched.every_secs >= 3600:
                return random.uniform(0, _JITTER_HOURLY_MAX)
            else:
                return 0.0  # sub-hourly jobs shouldn't be jittered
        if sched.kind == "cron" and sched.cron_expr:
            parts = sched.cron_expr.split()
            if len(parts) == 5:
                # Sub-hourly cron (minute field has / or , or is wildcard): no jitter
                if "/" in parts[0] or "," in parts[0] or parts[0] == "*":
                    return 0.0
                # Single literal hour (e.g., "0 3 * * *") = truly daily/weekly
                if parts[1].isdigit():
                    return random.uniform(0, _JITTER_DAILY_MAX)
                # Multi-hour patterns (*/2, 1,13) or wildcard = hourly jitter
                if parts[1] != "*":
                    return random.uniform(0, _JITTER_HOURLY_MAX)
            return random.uniform(0, _JITTER_HOURLY_MAX)
        return 0.0

    @staticmethod
    def _is_due(job: CronJob, now: float) -> bool:
        if job.schedule.kind == "every" and job.schedule.every_secs:
            last = job.last_run_ts or job.created_ts
            if now < last + job.schedule.every_secs:
                return False
        elif job.schedule.kind == "at" and job.schedule.at_ts:
            if now < job.schedule.at_ts:
                return False
        elif job.schedule.kind == "cron" and job.schedule.cron_expr:
            tz = _job_tz(job)
            dt = datetime.fromtimestamp(now, tz=tz)
            if not cron_expr_matches(job.schedule.cron_expr, dt):
                return False
            # Don't re-fire within the same UTC minute (immune to DST ambiguity)
            if job.last_run_ts and int(job.last_run_ts) // 60 == int(now) // 60:
                return False
        else:
            return False
        # Skip dates check (evaluated in job's local timezone, applies to all schedule types)
        if job.skip_dates:
            local_date = datetime.fromtimestamp(now, _job_tz(job)).strftime("%Y-%m-%d")
            if local_date in job.skip_dates:
                return False
        return True

    async def _execute_with_timeout(self, job: CronJob) -> None:
        """Execute a job with a timeout guard."""
        timeout = job.timeout_secs if 1 <= job.timeout_secs <= 86400 else _JOB_TIMEOUT_SECS
        # Fresh run: no failure counted yet. The timeout handler below reads
        # this to avoid double-counting a run that already recorded its
        # failure and then overran the deadline during cleanup.
        job.failure_recorded = False
        try:
            await asyncio.wait_for(self._execute(job), timeout=timeout)
        except asyncio.TimeoutError:
            # NB: Timeout bypasses _cron_callback's except block entirely —
            # which also means it bypasses all Slack notification logic. Adding
            # a timeout Slack alert is a separate feature and is intentionally
            # out of scope here.
            # Clear failure dedup state so a subsequent real error isn't
            # suppressed as a dup of the pre-timeout failure, but STILL count
            # the timeout toward the auto-pause threshold: a job that times out
            # on every run must eventually auto-pause instead of running forever
            # with zero user signal.
            job.last_status = "error"
            job.last_error = f"Timed out after {timeout}s"
            job.last_run_ts = time.time()
            job.last_failure_hash = ""
            job.last_failure_at = 0.0
            # Skip the count when this run already recorded its failure (a
            # delivery-path exception followed by cleanup overrunning the
            # deadline): one failed run is one failure, whichever handler
            # observes it last.
            if not job.failure_recorded:
                job.record_failure()
            logger.error("Cron job '%s' timed out after %ds", job.name, timeout)

    async def _execute(self, job: CronJob) -> None:
        """Run the job callback and update runtime fields (last_run_ts, last_status)."""
        logger.info("Cron: executing '%s' (%s)", job.name, job.id)
        # Reset status for this run so a prior run's "error" can't leak into an
        # "ok" decision below. Same for the fire-time denial marker.
        job.last_status = None
        job.fire_time_denied = False
        try:
            if self._on_job:
                await self._on_job(job)
            # Only mark "ok" if the callback did not itself report failure. The
            # command/script paths return NORMALLY and signal failure by mutating
            # the shared job (last_status="error"); only the LLM path raises.
            # Overwriting unconditionally with "ok" destroyed that error before
            # the history recorder and _merge_job_result read it, mis-reporting
            # failed command/script runs as successful on the dashboard and in
            # cron_list.
            if job.last_status != "error":
                job.last_status = "ok"
                job.last_error = None
                # Reset the auto-pause budget: without this, CronService-run
                # jobs count failures monotonically (record_failure fires on
                # the error/timeout paths but nothing ever reset the counter
                # here), so any job accumulating _AUTO_PAUSE_THRESHOLD
                # transient failures over its LIFETIME — successes in
                # between notwithstanding — silently auto-paused. Guarded by
                # the "error" check above so the deliberately-neutral paths
                # (governance/fire-time denials, which set last_status =
                # "error" without counting a failure) stay neutral: a policy
                # denial neither spends nor refills the budget. Callback
                # paths that already called record_success() are unaffected
                # (resetting 0 to 0 is idempotent). The _cancelled_jobs
                # check closes a cancel race: cancel() kills the sandboxed
                # subprocess BEFORE task.cancel(), and the gateway's
                # cancelled branch returns None without setting last_status,
                # so a callback returning in that window would otherwise
                # reach this branch — and cancel() documents that it leaves
                # consecutive_failures untouched.
                if job.id not in self._cancelled_jobs:
                    job.record_success()
        except Exception as exc:
            job.last_status = "error"
            job.last_error = str(exc)
            logger.error("Cron job '%s' failed: %s", job.name, exc)

        job.last_run_ts = time.time()

        # One-shot "at" jobs: disable after the run. A fire-time-DENIED at-job
        # is disabled too — its due time has passed, so leaving it enabled
        # would make it due on EVERY timer tick (a zero-delay refire loop that
        # floods audit/history until resource exhaustion). Parking it disabled
        # (instead of deleting — including the delete_after_run shape, which
        # the merge below retains) keeps it discoverable so an operator can
        # re-enable it after a policy loosening. Recurring jobs are untouched:
        # they simply wait for their next scheduled slot and resume on their
        # own when policy loosens.
        if job.schedule.kind == "at" and (not job.delete_after_run or job.fire_time_denied):
            job.enabled = False

    def _merge_job_result(self, job: CronJob) -> None:
        """Merge a single job's runtime state back to disk.

        Enters the bounded sync :meth:`_file_lock` (which spins with
        ``time.sleep`` under contention) and may raise :class:`CronStoreBusy`.
        MUST NOT be called directly on the gateway event loop — its sole
        loop-side caller, :meth:`_run_job_isolated`, offloads it via
        ``asyncio.to_thread`` so the spin never parks the loop. Sync/CLI
        contexts with no running loop may call it directly.
        """
        with self._file_lock():
            self._sync()
            by_id = {j.id: j for j in self._jobs}
            if job.id in by_id:
                by_id[job.id].last_run_ts = job.last_run_ts
                by_id[job.id].last_status = job.last_status
                by_id[job.id].last_error = job.last_error
                # Only propagate enabled=False for one-shot at-jobs that fired.
                # Never overwrite enabled for recurring jobs — user_paused is the
                # sole authority for user-controlled pause/resume state.
                # Propagate the fired/parked disable for at-jobs — including a
                # fire-time-DENIED one (parked disabled instead of deleted so
                # it cannot refire every tick yet stays re-enableable).
                if job.schedule.kind == "at" and (not job.delete_after_run or job.fire_time_denied):
                    by_id[job.id].enabled = job.enabled
                    by_id[job.id].user_paused = not job.enabled
                # auto_paused is execution-owned (repeated-failure auto-pause and
                # its reset on success), so propagate it for every job — unlike
                # `enabled`, which must not be clobbered for recurring jobs. Also
                # reflect it into the disk copy's derived `enabled` so the next
                # reader sees the pause before a reload re-derives it.
                by_id[job.id].auto_paused = job.auto_paused
                if job.auto_paused and not by_id[job.id].user_paused:
                    by_id[job.id].enabled = False
                by_id[job.id].last_result = job.last_result
                by_id[job.id].last_posted_hash = job.last_posted_hash
                by_id[job.id].consecutive_dupes = job.consecutive_dupes
                by_id[job.id].last_posted_at = job.last_posted_at
                by_id[job.id].last_failure_hash = job.last_failure_hash
                by_id[job.id].last_failure_at = job.last_failure_at
                by_id[job.id].consecutive_failures = job.consecutive_failures
            # A fire-time-DENIED run is a policy refusal, not a completed run:
            # deleting the one-shot here would make the documented
            # resume-on-policy-loosening semantic impossible for at-jobs.
            if job.delete_after_run and not job.fire_time_denied:
                self._jobs = [j for j in self._jobs if j.id != job.id]
            self._save()

    def _merge_terminal_state_locked(
        self,
        job_id: str,
        *,
        last_status: str,
        last_error: str,
        last_run_ts: float,
    ) -> None:
        """Persist a job's terminal runtime state under the store lock.

        Used for the reaper timeout (:meth:`_force_reap`) and user cancel
        (:meth:`cancel`) paths. Mutating the in-memory job and calling a bare,
        unlocked ``self._save()`` directly on the event loop would open a
        lost-update race: between a concurrent
        ``add_job_async``/``update_job_async`` worker's ``_sync`` and its
        ``_save``, the unlocked save would re-serialize a stale ``self._jobs``
        and silently drop the just-added/updated job from ``crons.json``.

        WORKER-THREAD ONLY. Mirrors :meth:`_merge_job_result`: enters the
        bounded sync :meth:`_file_lock` (whose spin does ``time.sleep`` and may
        raise :class:`CronStoreBusy`), ``_sync()``s FIRST so any concurrent
        worker's persisted job list is reloaded, then applies the terminal
        fields to the disk copy and ``_save()``s — the whole read-modify-write
        is one lock transaction. Both loop-side callers offload it via
        ``asyncio.to_thread`` so the spin never parks the gateway loop. A
        missing id (removed meanwhile) is a no-op.
        """
        with self._file_lock():
            self._sync()
            by_id = {j.id: j for j in self._jobs}
            target = by_id.get(job_id)
            if target is None:
                return
            target.last_status = last_status
            target.last_error = last_error
            target.last_run_ts = last_run_ts
            self._save()

    # ── Persistence ──

    @staticmethod
    def _guard_off_event_loop() -> None:
        """Enforce that the store lock is never acquired on a running loop.

        Detects a running asyncio event loop on the CURRENT thread — the
        loop-park hazard. Under strict mode (``KIROCREW_STRICT_LOOP_SAFETY``)
        it raises :class:`CronLoopSafetyError`; otherwise it emits a single
        throttled warning so an unforeseen legitimate caller is never broken in
        production while the signal is still surfaced. Sanctioned loop-resident
        paths never reach here on the loop thread: the ``*_async`` mutators run
        the lock in an ``asyncio.to_thread`` worker, and the synchronous
        :class:`~kiro_crew.apps.cron_sdk.CronSDK` facade offloads to a worker
        thread when a loop is running — in both cases this executes on a worker
        with no running loop, so the guard passes.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # loop-less thread/process — safe, the intended sync path
        if env_flag_enabled(_STRICT_LOOP_SAFETY_ENV):
            raise CronLoopSafetyError(
                "CronService store lock acquired on a thread with a running "
                "event loop — use the *_async mutator variant (add_job_async, "
                "remove_job_async, …) or the offloaded CronSDK facade instead "
                "of the synchronous mutator on the loop."
            )
        global _loop_safety_warned
        if not _loop_safety_warned:
            _loop_safety_warned = True
            logger.warning(
                "CronService store lock acquired on the event loop thread — "
                "this can park the loop under contention. Use the *_async "
                "mutator variants. Set %s=1 to make this a hard failure.",
                _STRICT_LOOP_SAFETY_ENV,
            )

    @contextmanager
    def _file_lock(
        self, *, timeout: float = _FILE_LOCK_TIMEOUT_SECS, poll: float = _FILE_LOCK_POLL_SECS
    ) -> Iterator[None]:
        """Cross-process advisory lock on the cron store.

        Acquires the lock with a NON-BLOCKING ``try_acquire_lock`` in a bounded
        spin instead of a blocking ``fcntl.flock(LOCK_EX)``. A blocking flock
        parks the calling thread in an uninterruptible kernel wait for as long
        as another holder keeps the lock — and every store *mutator*
        (:meth:`add_job`, :meth:`update_job`, :meth:`remove_job`,
        :meth:`enable_job`, …) takes this lock directly on the gateway's
        asyncio event loop. A single slow holder (a large atomic save on
        network storage, the CLI process, or the off-loop batch-remove worker)
        would therefore freeze the ENTIRE event loop — every unrelated session,
        timer, and reaper — until it released.

        The non-blocking spin polls with a short ``time.sleep`` between
        attempts (releasing the GIL so worker threads make progress) and raises
        :class:`CronStoreBusy` (a :class:`TimeoutError` subclass) after
        ``timeout`` rather than blocking indefinitely. flock on separate open
        descriptions mutually excludes within a single process too, so this
        still serializes the loop-side mutators against the ``asyncio.to_thread``
        batch-remove and mutator workers.

        The loop-resident mutator boundaries do NOT call this directly on the
        event loop — they use the ``*_async`` mutator variants (``add_job_async``
        et al.), which offload this lock+save to a worker thread and translate a
        raised :class:`CronStoreBusy` into a clean retryable error. The bounded
        sync path here still serves the CLI/MCP server processes (no event loop
        to park) and remains a strict improvement over the old unbounded flock.

        The ``no-blocking-call-on-event-loop`` invariant is MACHINE-ENFORCED:
        :meth:`_guard_off_event_loop` raises :class:`CronLoopSafetyError` (strict
        mode) or warns (default) if this is entered on a thread with a running
        asyncio loop — so a future writer that calls a sync mutator on the loop
        is caught rather than silently re-freezing it.
        """
        self._guard_off_event_loop()
        self._dir.mkdir(parents=True, exist_ok=True)
        lock = self._dir / ".crons.lock"
        fd = lock.open("w")
        deadline = time.monotonic() + timeout
        try:
            while not platform_compat.try_acquire_lock(fd.fileno(), exclusive=True):
                if time.monotonic() >= deadline:
                    raise CronStoreBusy(f"Could not acquire cron store lock within {timeout:g}s")
                time.sleep(poll)
            try:
                yield
            finally:
                platform_compat.release_lock(fd.fileno())
        finally:
            fd.close()

    def _record_fingerprint(self) -> None:
        """Snapshot the store file's fingerprint as the last-loaded state.

        Called after a successful load and after a save so :meth:`_sync` treats
        the current on-disk contents as already in memory and only reloads on a
        genuine external change. Records a content digest (authoritative) plus
        the (mtime_ns, size) tuple (diagnostic) from the bytes now on disk.
        """
        try:
            st = self._path.stat()
            raw = self._path.read_bytes()
        except OSError:
            self._reset_fingerprint()
            return
        self._last_mtime = st.st_mtime
        self._last_mtime_ns = st.st_mtime_ns
        self._last_size = st.st_size
        self._last_digest = hashlib.blake2b(raw, digest_size=16).digest()

    def _reset_fingerprint(self) -> None:
        """Clear the fingerprint so the next :meth:`_sync` forces a reload."""
        self._last_mtime = 0.0
        self._last_mtime_ns = 0
        self._last_size = -1
        self._last_digest = b""

    def _sync(self) -> None:
        """Reload from disk if the file changed externally.

        Compares a content DIGEST rather than only ``(mtime_ns, size)``: an
        external atomic write can preserve both the coarse timestamp and the
        byte length while changing content (e.g. renaming a job to an
        equal-length name), which an mtime/size fingerprint misses — the stale
        in-memory state would then be re-saved over the external change, losing
        it. The bytes read here are the same bytes :meth:`_load` parses when a
        reload is needed, so the file is read at most once per changed sync.

        ─────────────────────────────────────────────────────────────────────
        EXHAUSTIVE AUDIT — every ``_sync()`` caller and raw store-``read``
        site in this module, classified by whether it can run on the gateway
        event loop. INVARIANT: **no ``_sync()`` / whole-file ``read_bytes()`` +
        hash ever runs on the loop.** All blocking store I/O is either in a
        worker thread (``asyncio.to_thread``) or in a loop-less process
        (CLI / MCP). Enforced mechanically by
        ``test_cron_locking_regression.py::TestReadPathsLocked`` (on-loop reads
        AND the timer tick must not touch the store on the loop).

        ``_sync()`` callers
          • _persist_add_locked / _update_job_locked_kw / _remove_job_locked /
            _remove_jobs_locked / _enable_job_locked / _ack_job_locked /
            _unack_job_locked  → OFF-LOOP: reached from the loop only via their
            ``*_async`` wrappers, which ``await asyncio.to_thread(...)``; also
            called directly by loop-less CLI/MCP/app-SDK processes.
          • _synced_snapshot  → OFF-LOOP (worker): the body of
            list_jobs_async / get_job_async / run_job's offloaded refresh.
          • _tick_scan_locked  → OFF-LOOP (worker): the timer tick's
            (``_on_timer``) offloaded lock+sync+drain+snapshot transaction.
          • _merge_job_result  → OFF-LOOP on the gateway (``_run_job_isolated``
            calls it via ``asyncio.to_thread``); loop-less CLI/MCP may call it
            directly.
          • run_job  → now OFF-LOOP: its former on-loop ``_sync()`` moved into
            the ``_synced_snapshot`` offload; the ``_executing`` claim stays on
            the loop and does NO store I/O.

        Raw store ``read_bytes()`` sites
          • _record_fingerprint (post-load/save) / _sync / _load  → all reached
            only through the OFF-LOOP ``_sync()`` callers above (or a loop-less
            process). None on the loop.
          • initial ``_load()`` (construction / ``start()``)  → the plain
            constructor loads INLINE (loop-less CLI/MCP/apps-SDK/tests only —
            no loop to park). Loop contexts (the gateway) build via the async
            factory ``CronService.create()``, which sets
            ``_defer_initial_load=True`` and runs ``_load()`` via
            ``asyncio.to_thread``; ``start()`` likewise offloads its ``_load()``.
            ``_running`` is False during both, so neither arms a timer off-loop.
            OFF-LOOP on the gateway.

        Cache-only (NO store I/O at all — never lock, read, or hash)
          • list_jobs / get_job  → on-loop hot paths; return the atomically-
            swapped in-memory snapshot.
          • _reaper_loop's ``jobs_by_id`` snapshot  → on-loop; cache-only,
            same atomic-reference-swap rationale as list_jobs.
        ─────────────────────────────────────────────────────────────────────
        """
        if not self._path.exists():
            return
        try:
            raw = self._path.read_bytes()
        except OSError:
            return
        if hashlib.blake2b(raw, digest_size=16).digest() != self._last_digest:
            logger.info("Cron file changed externally, reloading")
            self._load(_preread=raw)

    def _load(self, _preread: bytes | None = None) -> None:
        """Deserialize jobs from crons.json and record the fingerprint.

        ``_preread`` lets :meth:`_sync` hand in the bytes it already read for
        the change check so the file is not read twice for one reload.
        """
        if not self._path.exists():
            self._jobs = []
            self._reset_fingerprint()
            return
        try:
            st = self._path.stat()
            raw = _preread if _preread is not None else self._path.read_bytes()
            data = json.loads(raw)
            records = data.get("jobs", []) if isinstance(data, dict) else None
            if not isinstance(records, list):
                # A document that parses but is not an object holding a jobs
                # LIST (top-level [], a scalar, {"jobs": null}) cannot yield
                # any job — same salvage story as unparseable JSON (there is
                # nothing to keep), and without this guard data.get() / the
                # loop below would raise an uncaught AttributeError/TypeError
                # into _sync and gateway startup (#4674).
                logger.warning(
                    "Failed to load cron store: document is not an object with a jobs list"
                )
                self._jobs = []
                self._reset_fingerprint()
                return
            # Per-entry isolation (#4664): one malformed or legacy record must
            # not discard the whole registry. Each record is built in its own
            # try block; a bad one is warned about and skipped, and every
            # well-formed job survives. The whole-store reset below is reserved
            # for a genuinely unparseable file (json.JSONDecodeError), where
            # there is nothing to salvage.
            jobs: list[CronJob] = []
            for j in records:
                try:
                    jobs.append(_job_from_record(j))
                except (KeyError, TypeError, AttributeError) as entry_exc:
                    entry_id = (
                        j.get("id", "<missing id>") if isinstance(j, dict) else "<not an object>"
                    )
                    logger.warning(
                        "Skipping malformed cron job entry (id=%r): %r; "
                        "the entry will be dropped from the store on the next write",
                        entry_id,
                        entry_exc,
                    )
            self._jobs = jobs
            # Fingerprint from the stat taken BEFORE the read: if a writer
            # replaced the file between our stat and read we may have loaded the
            # newer content under an older fingerprint, which only costs one
            # redundant reload on the next _sync — never a lost update. The
            # digest is taken from the exact bytes we parsed so _sync compares
            # like for like.
            self._last_mtime = st.st_mtime
            self._last_mtime_ns = st.st_mtime_ns
            self._last_size = st.st_size
            self._last_digest = hashlib.blake2b(raw, digest_size=16).digest()
        except json.JSONDecodeError as exc:
            logger.warning("Failed to load cron store: %s", exc)
            self._jobs = []
            self._reset_fingerprint()

        # Restore timers for active jobs loaded from disk
        if self._running:
            restored = sum(1 for j in self._jobs if j.enabled)
            if restored:
                self._arm_timer()
                logger.info("Restored %d cron timer(s) from disk", restored)

    def _save(self) -> None:
        """Atomic write (tmp → rename) and update mtime tracking.

        WRITE-PATH AUDIT — every ``_save()`` call site and every structural
        ``self._jobs`` mutation, each classified locked/unlocked and
        on-loop/off-loop. INVARIANT: every writer holds :meth:`_file_lock` and
        is reached from the gateway event loop ONLY via ``asyncio.to_thread``
        (or runs in a genuinely loop-less CLI/MCP process). No bare on-loop
        ``_save()`` remains. Keep this table in sync when adding a writer.

        ============================  ==========  ================================
        Writer (method)               Locked?     Loop entry
        ============================  ==========  ================================
        _persist_add_locked           _file_lock  add_job_async → to_thread; sync CLI/MCP
        _update_job_locked            _file_lock  update_job_async → to_thread; sync CLI/MCP
        _remove_job_locked            _file_lock  remove_job_async → to_thread; sync CLI/MCP
        _remove_jobs_locked           _file_lock  remove_jobs → to_thread
        _enable_job_locked            _file_lock  enable_job_async → to_thread; sync CLI/MCP
        _ack_job_locked               _file_lock  ack_job_async → to_thread; sync
        _unack_job_locked             _file_lock  unack_job_async → to_thread; sync
        _merge_job_result             _file_lock  _run_job_isolated → to_thread; sync
        _merge_terminal_state_locked  _file_lock  _force_reap / cancel → to_thread
        _drain_pending_removals_locked  (caller)  _tick_scan_locked holds _file_lock (→ to_thread)
        _load (self._jobs = …)          (caller)  _sync() under _file_lock; else construction/start off-loop
        ============================  ==========  ================================

        In-memory-only job field writes that DON'T call ``_save()`` and are
        persisted later under lock: ``defer_removal`` (sets ``enabled=False``
        so the next due-scan skips it; the durable delete happens in the locked
        ``_drain_pending_removals_locked``), and the pre-persist snapshot writes
        in ``_force_reap``/``cancel`` (authoritative persist is the offloaded
        ``_merge_terminal_state_locked``).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": _STORE_VERSION,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "message": j.message,
                    "schedule": asdict(j.schedule),
                    "channel": j.channel,
                    "thread_ts": j.thread_ts,
                    "enabled": j.enabled,
                    "user_paused": j.user_paused,
                    "auto_paused": j.auto_paused,
                    "last_run_ts": j.last_run_ts,
                    "last_status": j.last_status,
                    "last_error": j.last_error,
                    "created_ts": j.created_ts,
                    "delete_after_run": j.delete_after_run,
                    "last_result": j.last_result,
                    "context_enabled": j.context_enabled,
                    "agent_id": j.agent_id,
                    "approval_mode": j.approval_mode,
                    "acked_items": j.acked_items,
                    "created_by": j.created_by,
                    "silent": j.silent,
                    "session_key": j.session_key,
                    "last_posted_hash": j.last_posted_hash,
                    "consecutive_dupes": j.consecutive_dupes,
                    "last_posted_at": j.last_posted_at,
                    "last_failure_hash": j.last_failure_hash,
                    "last_failure_at": j.last_failure_at,
                    "consecutive_failures": j.consecutive_failures,
                    "skip_dates": j.skip_dates,
                    "timezone": j.timezone,
                    "persistent_session": j.persistent_session,
                    "minimal_context": j.minimal_context,
                    "hide_in_chat": j.hide_in_chat,
                    "folder_id": j.folder_id,
                    "model": j.model,
                    "agent_sequence": j.agent_sequence,
                    "env": j.env,
                    "timeout_secs": j.timeout_secs,
                    "strict_schedule": j.strict_schedule,
                    "script": j.script,
                    "command": j.command,
                    "timeout": j.timeout,
                }
                for j in self._jobs
            ],
        }
        # Atomic write: unique tmp → rename
        # Deferred import to avoid circular dependency (pre-existing)
        from kiro_crew.atomic_write import atomic_write

        atomic_write(self._path, json.dumps(data, indent=2))
        # Refresh the (mtime_ns, size) fingerprint so _sync recognizes this as
        # our own write and does not reload it back over the in-memory state.
        self._record_fingerprint()
