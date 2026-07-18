"""Cron service for scheduling agent tasks.

Jobs are stored in the config directory (``~/.kirocrew/crons.json`` by default,
overridden by ``KIROCREW_HOME``) and executed by a background
asyncio timer.  Each job fires a callback (typically posting to Slack via ACP).

Cross-process safety: the CLI and gateway run as separate processes sharing
the same ``crons.json``.  All read-modify-write cycles use advisory file
locking (fcntl), and mtime-based ``_sync()``
detects external file changes
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
import json
import logging
import random
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
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
from kiro_crew.executors import subprocess_executor

logger = logging.getLogger(__name__)

# ── Constants ──

_DEFAULT_DIR = config_dir()
_CRONS_FILE = "crons.json"
_STORE_VERSION = 2
_MIN_INTERVAL_SECS = 60
_JOB_TIMEOUT_SECS = 1800  # 30 min per job
_TIMER_POLL_SECS = 30  # check for due cron-expr jobs
_AUTO_PAUSE_THRESHOLD = 5  # consecutive failures before a script/command cron auto-pauses
_REAPER_INTERVAL = 60  # seconds between reaper sweeps
_REAPER_RESET_TIMEOUT = 30.0  # max seconds for session reset in reaper
_MAX_SKIP_DATE_LOOKAHEAD = 52  # weekly cron × 1 year — cap iterations when advancing past skip_dates

# Jitter bounds (seconds) to spread job execution and avoid traffic spikes
_JITTER_HOURLY_MAX = 5 * 60    # 0–5 minutes for hourly jobs
_JITTER_DAILY_MAX = 59 * 60    # 0–59 minutes for daily jobs


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
    auto_paused: bool = False  # True when paused by execution after repeated failures; cleared on re-enable/success
    last_run_ts: float | None = None
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None
    created_ts: float = 0.0
    delete_after_run: bool = False
    last_result: str | None = None
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
    consecutive_failures: int = 0  # count of consecutive identical failures (incl. first alert)
    skip_dates: list[str] = field(default_factory=list)  # ISO dates to skip ["2026-04-06"]
    timezone: str = ""  # IANA timezone for skip evaluation
    persistent_session: bool = True  # False → fresh ephemeral session per run (Mesh-1026)
    minimal_context: bool = False  # True → skip memory/lessons/skills/history (Mesh-1632)
    hide_in_chat: bool = False  # True → don't create a dashboard chat slot; result still goes to history + Slack/bell
    model: str = ""  # per-job model override (canonical key or provider id); "" = inherit

    # When agent_sequence is set, it takes precedence over agent_id.
    # The execution logic runs agents in order; see Phase 3.
    agent_sequence: list[str] = field(default_factory=list)
    project_path: str = ""  # project root for project-scoped agent (empty = global agent)
    env: dict[str, str] = field(default_factory=dict)  # per-job environment variables
    timeout_secs: int = _JOB_TIMEOUT_SECS
    strict_schedule: bool = False  # when True, skip jitter and fire exactly on schedule
    script: str = ""  # Python callable path (module:func or file.py:func); bypasses LLM dispatch
    command: str = ""  # Shell command for direct execution; bypasses LLM dispatch
    timeout: int = 0  # script/command timeout in seconds (0 = use default: 30s script, 300s command)

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
        if self.consecutive_failures >= _AUTO_PAUSE_THRESHOLD and not self.auto_paused:
            self.enabled = False
            self.auto_paused = True
            self._audit_pause_change("auto_paused")

    def record_success(self) -> None:
        """Reset the failure counter and lift any execution auto-pause.

        A recovered job clears `auto_paused`; `enabled` is intentionally NOT set
        back to True here — a job the user paused (`user_paused`) must stay paused
        across a success, and re-enabling is the user's action (`enable_job`)."""
        self.consecutive_failures = 0
        if self.auto_paused:
            self.auto_paused = False
            self._audit_pause_change("auto_pause_cleared")


# ── Session-context helper (Mesh-1026) ──


def build_cron_session_context(job: CronJob) -> tuple[str, str]:
    """Compute (session_key, prompt) for one cron run.

    When ``job.persistent_session`` is True (default, legacy behaviour):
      - session_key is stable across runs: ``cron:{job.id}``
      - prompt prepends ``job.last_result`` so the agent has recent context

    When ``job.persistent_session`` is False (Mesh-1026 stateless mode):
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
            # Advance past any skip_dates
            for _ in range(_MAX_SKIP_DATE_LOOKAHEAD):
                nxt = cron.get_next(float)
                if not job.skip_dates:
                    return nxt
                local_date = datetime.fromtimestamp(nxt, tz=tz).strftime("%Y-%m-%d")
                if local_date not in job.skip_dates:
                    return nxt
            logger.warning("No valid next run within %d iterations for job %s (all dates skipped)", _MAX_SKIP_DATE_LOOKAHEAD, job.id)
            return None
    except Exception:
        logger.warning("Failed to compute next run for job %s", job.id, exc_info=True)
        return None
    return None


class CronService:
    """Background service for managing and executing scheduled jobs."""

    def __init__(
        self,
        base_dir: Path | None = None,
        on_job: Callable[[CronJob], Awaitable[str | None]] | None = None,
    ):
        self._dir = base_dir or _DEFAULT_DIR
        self._path = self._dir / _CRONS_FILE
        self._on_job = on_job
        self._jobs: list[CronJob] = []
        self._timer_task: asyncio.Task[None] | None = None
        self._running = False
        self._last_mtime: float = 0.0
        self._executing: set[str] = set()  # job IDs currently running
        self._running_tasks: dict[str, asyncio.Task[None]] = {}  # strong refs to prevent GC
        self._job_start_times: dict[str, float] = {}  # job ID → epoch start
        self._reaped_jobs: set[str] = set()  # job IDs killed by the reaper
        self._cancelled_jobs: set[str] = set()  # job IDs cancelled by the user
        self._job_jitter: dict[str, float] = {}  # job ID → jitter seconds applied
        self._job_run_meta: dict[str, tuple[float, str]] = {}  # job_id → (start_time, trigger)
        # Mesh-1026: job_id → active session_key for the in-flight run.
        # Populated by the dispatcher (gateway callback) so the reaper can
        # target per-run ephemeral keys when persistent_session=False.
        self._active_session_keys: dict[str, str] = {}
        self._sessions: SessionManager | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._push_refresh: Callable[[str], None] | None = None  # set externally
        _cfg = KiroCrewConfig.load().cron_history
        self._history = CronHistoryStore(
            base_dir=base_dir or _DEFAULT_DIR,
            cron_summary_cap=_cfg.cron_summary_cap,
            cron_trace_cap_kb=_cfg.cron_trace_cap_kb,
            cron_max_records_per_job=_cfg.cron_max_records_per_job,
            cron_max_index_records=_cfg.cron_max_index_records,
        )

    # ── Lifecycle ──

    async def start(self) -> None:
        """Load jobs and start the timer loop."""
        self._load()
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
            for job_id, started in list(self._job_start_times.items()):
                elapsed = now - started
                job = next((j for j in self._jobs if j.id == job_id), None)
                deadline = max(min(job.timeout_secs, 86400), _JOB_TIMEOUT_SECS) if job else _JOB_TIMEOUT_SECS
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

    async def _force_reap(self, job_id: str, elapsed: float, deadline: int = _JOB_TIMEOUT_SECS) -> None:
        """Kill a cron job's session process and cancel its task."""
        # Mesh-1026: use the active per-run session key if registered;
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
                self._sigkill_session(session_key)
            except Exception:
                logger.exception("Reaper: reset failed for cron %s, attempting SIGKILL", job_id)
                self._sigkill_session(session_key)

        # Cancel the asyncio task and clean up tracking state directly.
        # Don't rely on _run_job_isolated's finally — the reaper exists for
        # cases where the normal path is stuck (idempotent with finally).
        task = self._running_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        self._executing.discard(job_id)

        # Update job state and persist.
        by_id = {j.id: j for j in self._jobs}
        job = by_id.get(job_id)
        if job:
            job.last_status = "error"
            job.last_error = (
                f"Reaped after {int(elapsed)}s (exceeded {deadline}s deadline)"
            )
            job.last_run_ts = time.time()
            try:
                self._save()
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

    def _sigkill_session(self, session_key: str) -> None:
        """Best-effort SIGKILL when graceful reset hangs.

        Uses killpg to kill the entire process group, then sweeps
        escaped children in different PGIDs (MCP servers).
        """
        if not self._sessions:
            return
        try:
            # circular import: cron → acp.client → session → cron
            from kiro_crew.acp.client import (
                _get_child_pids,
                _get_start_time,
                _is_our_child,
                _kill_escaped_children,
                _read_basename,
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
            # PGIDs survive killpg.
            raw_children = getattr(client, "_child_pids", None)
            child_pids: dict = (
                dict(raw_children) if isinstance(raw_children, dict) else {}
            )
            for p in _get_child_pids(pid):
                if p not in child_pids:
                    child_pids[p] = (_get_start_time(p), _read_basename(p))
            # Validate PID hasn't been recycled before killing.
            original_start = getattr(client, "_start_time", None)
            if original_start is None:
                logger.debug("Reaper: PID %d already dead for %s", pid, session_key)
                _kill_escaped_children(child_pids)
                return
            if not _is_our_child(pid, expected_start=original_start):
                logger.warning("Reaper: PID %d recycled for %s, skipping killpg", pid, session_key)
                stored = dict(raw_children) if isinstance(raw_children, dict) else {}
                _kill_escaped_children(stored)
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
                # platform_compat.kill_process_tree).
                platform_compat.kill_process_tree(pid, platform_compat.SIGKILL)
            except ValueError:
                # Guard refused the pid outright (non-int/reserved) — nothing
                # safe to signal.
                logger.error("Reaper: kill guard refused pid %r for %s", pid, session_key)
            except (ProcessLookupError, OSError):
                try:
                    platform_compat.kill_pid(pid, platform_compat.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            _kill_escaped_children(child_pids)
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
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), self._sigkill_session, session_key
                )
            except Exception:
                logger.exception("Cancel: reset failed for cron %s, attempting SIGKILL", job_id)
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), self._sigkill_session, session_key
                )

        # 3. Cancel the asyncio task and clean up tracking state directly
        # (idempotent with _run_job_isolated's finally).
        task = self._running_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        self._executing.discard(job_id)

        # 4. Update job state, persist, and record history.
        if job:
            job.last_status = "error"
            job.last_error = f"Cancelled by user after {int(elapsed)}s"
            job.last_run_ts = time.time()
            try:
                self._save()
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
    ) -> CronJob:
        """Add a new job. Provide one of ``every_secs``, ``at_ts``, or ``cron_expr``."""
        valid_approval_modes = ("", "auto")
        if approval_mode not in valid_approval_modes:
            raise ValueError(f"Invalid approval_mode: {approval_mode!r}")
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

        job = CronJob(
            id=uuid.uuid4().hex[:8],
            name=name,
            message=message,
            schedule=schedule,
            channel=channel,
            thread_ts=thread_ts,
            enabled=True,
            created_ts=time.time(),
            delete_after_run=delete_after_run,
            created_by=created_by,
            approval_mode=approval_mode,
        )
        with self._file_lock():
            self._sync()
            self._jobs.append(job)
            self._save()
        self._arm_timer()
        logger.info("Added cron job '%s' (%s)", name, job.id)
        return job

    def update_job(self, job_id: str, **kwargs: Any) -> CronJob | None:
        """Update fields on an existing job. Returns updated job or None if not found.

        Accepted kwargs: name, message, every_secs, cron_expr, agent_id, channel,
        approval_mode, silent, skip_dates, timezone, thread_ts, model.
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
                if "name" in kwargs and kwargs["name"]:
                    job.name = kwargs["name"]
                if "message" in kwargs and kwargs["message"]:
                    job.message = kwargs["message"]
                if "agent_id" in kwargs:
                    job.agent_id = kwargs["agent_id"] or ""
                if "project_path" in kwargs:
                    job.project_path = kwargs["project_path"] or ""
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
                if "model" in kwargs:
                    job.model = str(kwargs["model"] or "").strip()

                # Schedule changes (already validated above)
                if "cron_expr" in kwargs and kwargs["cron_expr"]:
                    job.schedule = CronSchedule(kind="cron", cron_expr=kwargs["cron_expr"])
                elif "every_secs" in kwargs and kwargs["every_secs"]:
                    job.schedule = CronSchedule(kind="every", every_secs=int(kwargs["every_secs"]))
                self._save()
                self._arm_timer()
                logger.info("Updated cron job %s", job_id)
                return job
        return None

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        with self._file_lock():
            self._sync()
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.id != job_id]
            if len(self._jobs) < before:
                self._save()
                self._arm_timer()
                logger.info("Removed cron job %s", job_id)
                return True
        return False

    def enable_job(self, job_id: str, enabled: bool = True) -> bool:
        """Enable or disable a job by ID."""
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
                    self._arm_timer()
                    logger.info("%s cron job %s", "Enabled" if enabled else "Disabled", job_id)
                    return True
        return False

    def ack_job(self, job_id: str, summary: str) -> bool:
        """Acknowledge a cron notification — stores summary for future context."""
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
        """Remove the most recent acked item from a cron job."""
        with self._file_lock():
            self._sync()
            for job in self._jobs:
                if job.id == job_id and job.acked_items:
                    job.acked_items.pop()
                    self._save()
                    return True
        return False

    # ── Active session tracking (Mesh-1026) ──

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
        self._sync()
        job = None
        for j in self._jobs:
            if j.id == job_id:
                job = j
                break
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
        """List jobs, optionally including disabled ones."""
        self._sync()
        if include_disabled:
            return list(self._jobs)
        return [j for j in self._jobs if j.enabled]

    def get_job(self, job_id: str) -> CronJob | None:
        """Find a job by its id. Returns the CronJob, or None if not found."""
        self._sync()
        for job in self._jobs:
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
        return min(delay, _TIMER_POLL_SECS)

    def _arm_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
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

    async def _on_timer(self) -> None:
        """Fire due jobs as independent tasks (non-blocking)."""
        with self._file_lock():
            self._sync()
            now = time.time()
            due = [
                j
                for j in self._jobs
                if j.enabled and j.id not in self._executing and self._is_due(j, now)
            ]

        if not due:
            return

        # Fire each job independently — one hung job never blocks others.
        for j in due:
            self._executing.add(j.id)
            self._job_run_meta.setdefault(j.id, (time.time(), "scheduled"))
            task = asyncio.create_task(self._run_job_isolated(j))
            self._running_tasks[j.id] = task

    async def _run_job_isolated(self, job: CronJob) -> None:
        """Execute a single job and merge results back to disk."""
        meta = self._job_run_meta.get(job.id)
        started_at = meta[0] if meta else time.time()
        trigger = meta[1] if meta else "scheduled"
        self._job_start_times[job.id] = started_at
        # Apply jitter to spread execution unless strict_schedule is set or manual
        jitter = self._compute_jitter(job) if trigger != "manual" else 0
        self._job_jitter[job.id] = jitter
        if jitter > 0:
            logger.debug("Cron: applying %.0fs jitter to job '%s'", jitter, job.name)
            await asyncio.sleep(jitter)
        exec_started_at = time.time()
        # Notify dashboard that the job has started executing so the live
        # is_running badge appears without a manual reload (upstream a5326708).
        try:
            if self._push_refresh:
                self._push_refresh("crons")
        except Exception:
            logger.debug("push_refresh failed on job start", exc_info=True)
        try:
            await self._execute_with_timeout(job)
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
                try:
                    self._merge_job_result(job)
                except Exception:
                    logger.exception("Failed to merge result for job '%s'", job.name)
                # Record history
                try:
                    status = "success" if job.last_status == "ok" else "failure"
                    record = CronRunRecord(
                        job_id=job.id,
                        trigger=trigger,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=int((finished_at - exec_started_at) * 1000),
                        status=status,
                        summary=(job.last_result or job.last_error or "")[:200],
                        trace=job.last_result or "",
                        error=job.last_error or "",
                    )
                    await self._history.append(record)
                    if self._push_refresh:
                        self._push_refresh("cron_history")
                except Exception:
                    logger.exception("Failed to record history for job '%s'", job.name)

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
        try:
            await asyncio.wait_for(self._execute(job), timeout=timeout)
        except asyncio.TimeoutError:
            # NB: Timeout bypasses _cron_callback's except block entirely —
            # which also means it bypasses all Slack notification logic. From
            # the user's perspective, timeouts are silent (log + dashboard
            # status update only). Adding a timeout Slack alert is a separate
            # feature and is intentionally out of scope for failure dedup.
            # Clear failure dedup state so a subsequent real error isn't
            # suppressed as a dup of the pre-timeout failure.
            job.last_status = "error"
            job.last_error = f"Timed out after {timeout}s"
            job.last_run_ts = time.time()
            job.last_failure_hash = ""
            job.last_failure_at = 0.0
            job.consecutive_failures = 0
            logger.error("Cron job '%s' timed out after %ds", job.name, timeout)

    async def _execute(self, job: CronJob) -> None:
        """Run the job callback and update runtime fields (last_run_ts, last_status)."""
        logger.info("Cron: executing '%s' (%s)", job.name, job.id)
        # Reset status for this run so a prior run's "error" can't leak into an
        # "ok" decision below.
        job.last_status = None
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
        except Exception as exc:
            job.last_status = "error"
            job.last_error = str(exc)
            logger.error("Cron job '%s' failed: %s", job.name, exc)

        job.last_run_ts = time.time()

        # One-shot "at" jobs without delete_after_run: disable instead of delete
        if job.schedule.kind == "at" and not job.delete_after_run:
            job.enabled = False

    def _merge_job_result(self, job: CronJob) -> None:
        """Merge a single job's runtime state back to disk."""
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
                if job.schedule.kind == "at" and not job.delete_after_run:
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
            if job.delete_after_run:
                self._jobs = [j for j in self._jobs if j.id != job.id]
            self._save()

    # ── Persistence ──

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Cross-process advisory lock on the cron store.

        Uses fcntl.flock for cross-process locking.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        lock = self._dir / ".crons.lock"
        fd = lock.open("w")
        try:
            with platform_compat.file_lock(fd.fileno(), exclusive=True):
                yield
        finally:
            fd.close()

    def _sync(self) -> None:
        """Reload from disk if the file was modified externally."""
        if not self._path.exists():
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if mtime > self._last_mtime:
            logger.info("Cron file changed externally, reloading")
            self._load()

    def _load(self) -> None:
        """Deserialize jobs from crons.json and record mtime for sync tracking."""
        if not self._path.exists():
            self._jobs = []
            self._last_mtime = 0.0
            return
        try:
            self._last_mtime = self._path.stat().st_mtime
            data = json.loads(self._path.read_text())
            self._jobs = [
                CronJob(
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
                    # user_paused/auto_paused fall back to legacy !enabled for
                    # stores written before either field existed.
                    enabled=not j.get("user_paused", not j.get("enabled", True)) and not j.get("auto_paused", False),
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
                    model=j.get("model", ""),
                    agent_sequence=j.get("agent_sequence", []),
                    env=j.get("env", {}),
                    timeout_secs=j.get("timeout_secs", _JOB_TIMEOUT_SECS),
                    strict_schedule=j.get("strict_schedule", False),
                    script=j.get("script", ""),
                    command=j.get("command", ""),
                    timeout=j.get("timeout", 0),
                )
                for j in data.get("jobs", [])
            ]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load cron store: %s", exc)
            self._jobs = []
            self._last_mtime = 0.0

        # Restore timers for active jobs loaded from disk
        if self._running:
            restored = sum(1 for j in self._jobs if j.enabled)
            if restored:
                self._arm_timer()
                logger.info("Restored %d cron timer(s) from disk", restored)

    def _save(self) -> None:
        """Atomic write (tmp → rename) and update mtime tracking."""
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
        # Atomic write: unique tmp → rename (Mesh-100)
        # Deferred import to avoid circular dependency (pre-existing)
        from kiro_crew.atomic_write import atomic_write

        atomic_write(self._path, json.dumps(data, indent=2))
        self._last_mtime = self._path.stat().st_mtime
