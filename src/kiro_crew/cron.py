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

:class:`CronService` is defined here and composes the owners in
:mod:`kiro_crew.cron_service` (schedule evaluation, the job record, run identity,
run claims, run deadlines, the store format and lock, field validation, the
serviceless store readers, cron folders). It keeps the service's live state, its
run lifecycle, the reaper and ``cancel()`` teardown, and every store transaction.

This module is also the subsystem's import and patch surface: every function,
class and constant it defined before those owners moved out still resolves here
as the same object, and so does every public name it imported from the rest of
the package.
The names tests patch here that moved code reads -- ``datetime``,
``get_local_tz``, ``published_config_timezone``, ``cron_expr_matches``,
``config_dir``, ``_record_is_enabled``, ``sel`` and ``_JOB_TIMEOUT_SECS`` -- the
owners read through this module on each call, so a patch here reaches them. Every
other name the owners read is their own global, which a patch here does not reach.
"""

from __future__ import annotations

import asyncio
import logging
import random  # noqa: F401 -- patch seam: tests patch kiro_crew.cron.random.uniform
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime  # noqa: F401 -- patch seam the owners read through here
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Iterator,
)

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

from kiro_crew import platform_compat  # noqa: F401 -- re-exported
from kiro_crew import (
    cron_inflight,
    cron_script,
    sel,
    shutdown_event,
    stall_attribution,
)
from kiro_crew.config.loader import (  # noqa: F401 -- patch seams the owners read through here
    KiroCrewConfig,
    config_dir,
    data_home,
    published_config_timezone,
)
from kiro_crew.constants import env_flag_enabled
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord
from kiro_crew.cron_service.claims import (  # noqa: F401 -- re-exported
    RunClaims,
    _manual_run_refused,
    _RunClaim,
)
from kiro_crew.cron_service.execution import (  # noqa: F401 -- re-exported
    _SUBPROC_CLEANUP_ALLOWANCE_SECS,
    _gate_budget_allowance,
    _pool_queue_allowance,
    _vet_allowance,
    apply_run_record,
    close_run,
    effective_wake_budget,
)
from kiro_crew.cron_service.fields import (  # noqa: F401 -- re-exported
    _CHAT_FOLDER_NEEDS_PERSISTENT,
    _CRON_STRING_FIELD_CAPS,
    _MIN_INTERVAL_SECS,
    _validate_cron_string_fields,
    apply_job_update,
    build_job,
    validate_managed_by,
)
from kiro_crew.cron_service.folders import (  # noqa: F401 -- re-exported
    _CRON_FOLDERS_FILE,
    CronFolderLookup,
    _read_cron_folders,
    load_cron_folders,
    lookup_cron_folder_id,
)
from kiro_crew.cron_service.identity import (  # noqa: F401 -- re-exported
    agent_sequence_dispatches,
    bind_cron_memory,
    build_cron_session_context,
    cron_session_key_is_stable,
    resolve_cron_memory,
)
from kiro_crew.cron_service.model import (  # noqa: F401 -- re-exported
    _AUTO_PAUSE_THRESHOLD,
    _JOB_TIMEOUT_SECS,
    CronJob,
    CronSchedule,
)
from kiro_crew.cron_service.readers import (  # noqa: F401 -- re-exported
    _SKILL_TOKEN_RE,
    _captured_template_id,
    dispatched_agents_from_disk,
    enabled_count_from_disk,
    job_agent_names_from_disk,
    referenced_skill_names,
)
from kiro_crew.cron_service.schedule import (  # noqa: F401 -- re-exported
    _JITTER_DAILY_MAX,
    _JITTER_HOURLY_MAX,
    _JITTER_WALL_SLICE_SECS,
    _MAX_SKIP_DATE_HORIZON_SECS,
    _MAX_SKIP_DATE_LOOKAHEAD,
    _RE_IN_DURATION,
    _TIMER_POLL_SECS,
    _UNIT_SECS,
    _compute_next_run_ts_raw,
    _humanize_cron,
    _job_tz,
    _next_cron_boundary_ts,
    compute_jitter,
    compute_next_run_ts,
    cron_expr_matches,
    format_schedule,
    get_local_tz,
    is_due,
    is_valid_skip_date,
    is_valid_timezone,
    next_wake_secs,
    parse_time_string,
    validate_cron_expr,
)
from kiro_crew.cron_service.store import (  # noqa: F401 -- re-exported
    _CRONS_FILE,
    _FILE_LOCK_POLL_SECS,
    _FILE_LOCK_TIMEOUT_SECS,
    _STORE_VERSION,
    CronPendingMismatch,
    CronStoreBusy,
    CronStoreUnreadable,
    _is_loadable_record,
    _is_representable_number,
    _job_from_record,
    _read_job_records,
    _record_is_enabled,
    _record_user_paused,
    cron_store_lock,
    decode_jobs,
    encode_store,
    quarantine_copies,
    quarantine_unreadable_store,
    store_digest,
)
from kiro_crew.executors import cron_gate_budget  # noqa: F401 -- re-exported
from kiro_crew.executors import subprocess_executor
from kiro_crew.metrics.events import CRON_FIRES, emit_counter
from kiro_crew.process_identity import (
    ProcessHandle,
    add_handle,
    ending_fence,
    failure_name,
    join_failures,
    kill_each,
    kill_set,
    kill_verified_process,
    process_handle_of,
    process_survived_async,
    release_teardown_lease,
    spawn_in_flight,
    teardown_barriers,
    teardown_capture,
    with_kill_failure,
)
from kiro_crew.resource_status import admission_check
from kiro_crew.runtime_ownership import authorize_runtime_kill
from kiro_crew.validation import (  # noqa: F401 -- re-exported
    CHANNEL_MAX_LEN,
    MAX_CRON_MESSAGE,
    MAX_SHORT_STRING,
)

logger = logging.getLogger(__name__)


def cron_job_id_from_session_key(session_key: object) -> str:
    """The job id inside a ``cron:`` session key, or ``""`` for any other value.

    Every shape this repository mints is ``cron:<job_id>`` with an OPTIONAL third
    segment, and a job id is ``uuid4().hex[:8]`` so it never contains a colon --
    which is what makes taking the second segment exact rather than a guess.

    A FALSY key (``None`` or ``""``) is one of the "any other key" cases, not an
    input error: ``session_key`` is an optional caller-supplied field, so the
    falsy-skip at creation persists ``None`` on the row, and an ownerless row is
    the documented state the CLI and the Schedule page manage. A non-string JSON
    value is malformed persisted data, but it gets the same non-cron answer here:
    this parser is shared by removal cleanup, where calling ``.startswith`` on a
    list or mapping would abort after the target row was filtered from memory and
    let a later save silently commit a removal the caller saw fail. Answering
    ``""`` here is what every consumer already expects of a non-cron owner -- the
    same reading ``_owned_by`` gives an empty key (reaches nothing) and the release
    paths give one (``if not job.session_key: continue``). Guarding at this one
    boundary rather than at each call site keeps the "ONE key parser" invariant
    that :func:`cron_owner_matches` and the liveness checks depend on.
    """
    if not isinstance(session_key, str) or not session_key.startswith("cron:"):
        return ""
    return session_key.split(":")[1] if len(session_key.split(":")) > 1 else ""


def cron_owner_matches(job_owner: str, target: str) -> bool:
    """Whether ``job_owner`` names the same principal as ``target``.

    THE one place an owner-key spelling is compared for release. A cron run does
    not present a single spelling: ``build_cron_session_context`` mints
    ``cron:<job id>`` for a persistent job and ``cron:<job id>:<run id>`` for a
    stateless one, and the sequential-agent path mints
    ``cron:<job id>:<agent>`` — and a job that run creates is stamped with
    WHICHEVER of those the run happened to present. All of them name the same
    principal, so plain ``==`` silently misses a child stamped with a longer
    spelling than the caller holds: the release skips it, and because a match miss
    is not a release FAILURE nothing warns.

    Parses through :func:`cron_job_id_from_session_key` rather than its own
    splitter, so the release path and the MCP surface cannot drift on what a
    ``cron:`` key means.

    Non-cron owners (``dashboard:``, a channel key) compare exactly. They have
    ONE spelling each, and loosening them would let one session's key reach
    another's jobs.

    Deliberately NOT used by the MCP ownership gate (``_owned_by``), which stays
    exact equality: this decides which jobs a RETIRED principal's cleanup may
    release, not which jobs a live caller may read or write.
    """
    if job_owner == target:
        return True
    owner_principal = cron_job_id_from_session_key(job_owner)
    return bool(owner_principal) and owner_principal == cron_job_id_from_session_key(target)


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_DEFAULT_DIR: Path | None = None


def _default_dir() -> Path:
    """Cron data directory, resolved against the live data home."""
    return _DEFAULT_DIR if _DEFAULT_DIR is not None else data_home()


# The doctor's two store readers stay defined here: `kirocrew doctor`'s facade
# test pins `kiro_crew.cron` as the module that defines them.
def cron_store_quarantine_copies() -> tuple[Path, list[Path]]:
    """The cron store's path and every copy gateway startup moved aside, for doctor."""
    store = config_dir() / _CRONS_FILE
    return store, quarantine_copies(store)


def unhealthy_jobs_from_disk() -> tuple[list[tuple[str, str]], list[tuple[str, str]], bool]:
    """Return ``(auto_paused, errored, loadable)`` for the doctor's cron check.

    The first two are ``(id, name)`` pairs needing attention. ``loadable``
    rides the SAME read rather than a second one: a store the scheduler cannot
    load yields two empty buckets, which is indistinguishable from a healthy
    empty store in the pairs alone, so the caller needs the flag to avoid
    handing back a clean bill of health for a stopped scheduler.

    Read-only + best-effort, and a sibling of :func:`referenced_skill_names` for
    the same reason: it reads ``crons.json`` directly so it needs no running
    scheduler. ``kirocrew doctor`` is the caller, and a diagnostic whose purpose
    is to speak when the gateway is wedged must not depend on the gateway.

    Non-raising by contract, via :func:`_read_job_records`, which owns the
    read-parse-shape prologue this and the two other direct readers share: a
    missing file (every fresh install — no crons yet), unreadable bytes,
    invalid UTF-8, malformed JSON (including deeply nested input), a store not
    shaped like a job list, and individual malformed records all report
    "nothing found". The run on a host with a corrupt store is exactly the run
    that most needs the caller's other diagnostics, and must not get a
    traceback instead of them.

    The two buckets are disjoint and carry different remediation. A job only
    reaches ``auto_paused`` by failing repeatedly, so it almost always carries
    ``last_status="error"`` too; reporting it in both would give a caller
    contradictory advice (resume it vs. re-trigger it) for one job. Auto-pause
    wins because re-triggering a paused job does not un-pause it.

    A user-paused job appears in NEITHER bucket. ``user_paused`` is deliberately
    distinct from ``auto_paused``: a job the user paused on purpose is not a
    health signal, and a stale ``last_status`` from before they paused it is not
    either. Both flags can be set at once — :meth:`CronStore._enable_job_locked`
    clears ``auto_paused`` only when ENABLING, so pausing an already-auto-paused
    job leaves ``auto_paused`` true and adds ``user_paused`` — and the explicit
    user pause is the later, more specific instruction, so it wins.
    """
    auto_paused: list[tuple[str, str]] = []
    errored: list[tuple[str, str]] = []
    records, loadable = _read_job_records(config_dir() / _CRONS_FILE)
    for j in records:
        if not _is_loadable_record(j):
            # A record the SCHEDULER cannot build is not a job to advise about.
            # Classifying it anyway emits a resume/trigger hint for something
            # that will never run — and because a non-empty bucket outranks the
            # store report, it also HIDES the unloadable-store diagnostic behind
            # a phantom job. Skipping here keeps the two consistent: the same
            # predicate that clears `loadable` also decides what gets named.
            # Diagnostic-only; the runtime readers take `[0]` and are unaffected.
            continue
        entry = (str(j.get("id") or "no-id"), str(j.get("name") or "(unnamed)"))
        if _record_user_paused(j):
            # The user pause wins unconditionally, per the contract above. An
            # errored `at` record is NOT exempted: nothing in a serialized job
            # separates a pause the user asked for from the one execution writes
            # when it parks a fired at-job (both are enabled=False +
            # user_paused=True, and `fire_time_denied` is not persisted), so an
            # exemption cannot target only the execution case -- it also hands
            # back a hint for a job the user deliberately switched off.
            continue
        if j.get("auto_paused", False):
            auto_paused.append(entry)
        elif j.get("last_status") == "error":
            # _record_is_enabled is the shared predicate: reaching here means
            # neither pause flag is set, so this bucket is the still-scheduled
            # failures and cannot overlap the auto-paused one above.
            errored.append(entry)
    return (auto_paused, errored, loadable)


def job_pause_state_from_disk(job_id: str) -> str | None:
    """``"paused by the user"`` / ``"auto-paused"`` / ``"enabled"`` for *job_id*,
    or None when the store has no such job.

    A sibling of :func:`unhealthy_jobs_from_disk` for the doctor's stall
    attribution: once a dump is attributed to a job, the next question is
    whether that job is still scheduled to run again, answered from the store
    directly so it holds when the gateway is down.
    """
    records, _loadable = _read_job_records(config_dir() / _CRONS_FILE)
    for j in records:
        if str(j.get("id") or "") != job_id:
            continue
        if _record_user_paused(j):
            return "paused by the user"
        if j.get("auto_paused", False):
            return "auto-paused"
        return "enabled"
    return None


_REAPER_INTERVAL = 60  # seconds between reaper sweeps
_REAPER_RESET_TIMEOUT = 30.0  # max seconds for session reset in reaper
#: How many ROUNDS of key-ending a reap or cancel runs over a run
#: (``_end_run_sessions``): the first ends every key the run had registered,
#: each with ONE reset-then-kill pass (``_end_run_processes``) under the key's
#: ENDING FENCE (``SessionManager.ending_key``), held from before the pass until
#: the run's terminal record and audit are written -- a claim or cold start under
#: a fenced key is HELD at the door (it lands once the fence lifts, under the
#: recorded key), one already in flight is refused at registration with its
#: provider hard-killed there, so no session lands under a fenced key after the
#: pass and the pass is never repeated for a key. What CAN land is a NEW key: a
#: sequential run whose hung agent's session was reset moves on and registers
#: its next agent's key while the round runs (the run's task is cancelled only
#: after the passes), so after each round the run's keys are read again and a
#: key no round has ended gets the next round, fenced too. Bounded so a run that
#: keeps registering cannot hold the reaper: a key registered after the last
#: round is a named kill failure, audited ``failed``, never ``reaped``. Each
#: round spends at most ``_REAPER_RESET_TIMEOUT`` plus the kill's own bounds.
_ENDING_ROUNDS = 2


async def _sleep_out_jitter(jitter: float) -> None:
    """Wait ``jitter`` seconds before a scheduled run, ending on the wall clock too.

    Jitter is a spread on the WALL-CLOCK fire time ("start somewhere in the
    59 minutes after 09:00"). A bare ``asyncio.sleep(jitter)`` measures it on
    the event loop's clock, ``time.monotonic()``, which on macOS is
    ``mach_absolute_time`` and stops while the host sleeps. A daily job that
    fires in a two-second DarkWake therefore waits out its jitter in AWAKE
    seconds only: hours of wall time later it is still waiting, the dashboard
    shows it Running (the run holds its claim through the wait), it has
    written no in-flight marker or history row, and its next run has already
    moved to tomorrow.

    The wait ends at whichever deadline comes FIRST:

    - the wall-clock deadline, so suspended time counts and a resumed host
      starts the run within one ``_JITTER_WALL_SLICE_SECS`` slice;
    - the monotonic deadline, so a backward wall-clock step (NTP, a manual
      clock change) can never stretch the wait past the old behaviour.

    Cancellation is unchanged: a ``cancel()`` lands at the slice's
    ``asyncio.sleep`` exactly as it landed at the single sleep before.
    """
    wall_deadline = time.time() + jitter
    mono_deadline = time.monotonic() + jitter
    while True:
        remaining = min(wall_deadline - time.time(), mono_deadline - time.monotonic())
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _JITTER_WALL_SLICE_SECS))


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


# ── Service ──


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
        #: Where :meth:`create` moved an unparseable store, or None.
        self.quarantined_store: Path | None = None
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
        # Set when _load could not read the store, cleared on every load that
        # DID resolve (including a missing file and an honestly empty one).
        # _save consults it so a degraded-to-empty job list is never persisted
        # over a store that still holds records — see _save's refusal.
        self._load_failed: bool = False
        # Every job's run claim (its trigger and start stamp, the tracked task,
        # the in-flight marker token, the generation, the monotonic start the
        # reaper measures on and the jitter it allows for), the reaper's and
        # cancel()'s markers keyed to it, and the run generations handed out:
        # see RunClaims. Loop-owned: every claim, fence and release happens on
        # the event loop, await-free.
        self._runs = RunClaims()
        # Where the loop-stall breaker looks for crash dumps. None = the data
        # home's dump directory; tests point it at a temp dir.
        self._dumps_dir: Path | None = None
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
        # job_id → ordered exact live session keys, each attributed to the run
        # (the _RunClaim stored for the job when it was registered) that
        # registered it. A stateless run can leave its session alive for
        # pending subagents after the cron callback returns, while a newer run
        # of the same job registers another key; a sequential job registers one
        # key per agent inside ONE run and defers an earlier agent's reset while
        # its sub-agents are pending. Keeping only the newest key makes
        # ownership cleanup forget the older live runtime, and a reap or cancel
        # that ends only the newest key records ``reaped`` while an earlier
        # agent's session -- the same run's -- and its sub-agents survive. Dict
        # insertion order gives the reaper the run's keys newest first; the
        # attribution tells the run's own keys, all of which it ends, from an
        # older run's key kept alive for its pending sub-agents, which it leaves
        # alone; the full nested set protects every distinct live run. Duplicate
        # registration is idempotent: one successful SessionManager reset retires
        # the entire exact key, not one caller's reference to it.
        self._active_session_lock = threading.Lock()
        self._active_session_keys: dict[str, dict[str, _RunClaim | None]] = {}
        self._sessions: SessionManager | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._push_refresh: Callable[[str], None] | None = None  # set externally
        _cfg = KiroCrewConfig.load().cron_history
        _history_dir = base_dir if base_dir is not None else _default_dir()
        # Execution history is BEST-EFFORT and must never be load-bearing for
        # scheduling: a throw HERE would propagate out of CronService.__init__
        # and take the WHOLE cron subsystem with it — the gateway scheduler, MCP
        # cron_add/cron_list/cron_trigger and `kirocrew cron list` alike, none of
        # which need history to work. That guarantee lives in the store itself:
        # _prepare_dir resolves usability without raising and _degrade absorbs a
        # later failure, so there is deliberately NO try/except here. One would
        # guard a raise that cannot occur, and a reader would have to prove that
        # for themselves before trusting either layer.
        #
        # The store's directory setup is synchronous filesystem I/O, so it is
        # deferred on exactly the same condition as _load() below: a loop
        # context constructs via create(), which then runs both off the loop in
        # a worker thread. Without that, preparing the history directory would
        # stat/open on the gateway's sole event loop — the same
        # no-blocking-call-on-event-loop violation _defer_initial_load exists
        # to prevent.
        self._history = CronHistoryStore(
            base_dir=_history_dir,
            cron_summary_cap=_cfg.cron_summary_cap,
            cron_trace_cap_kb=_cfg.cron_trace_cap_kb,
            cron_max_records_per_job=_cfg.cron_max_records_per_job,
            cron_max_index_records=_cfg.cron_max_index_records,
            _defer_prepare=_defer_initial_load,
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
        *,
        quarantine_unreadable: bool = False,
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

        ``quarantine_unreadable`` is for gateway startup. A store that failed to
        load refuses every write, and startup writes to it (app cron cleanup), so
        a corrupt file stopped the gateway from serving at all. With the flag, an
        unparseable file is renamed aside -- never rewritten or deleted -- and the
        store reloads as missing: empty and writable. :attr:`quarantined_store`
        then names the copy so the caller can tell the user where the jobs are.
        """
        self = cls(base_dir=base_dir, on_job=on_job, _defer_initial_load=True)
        # Bind to the gateway loop so off-loop mutation paths (async mutators'
        # worker cores, app-hook/SDK calls offloaded via asyncio.to_thread) can
        # re-arm the timer thread-safely — see _arm_timer / __init__ _loop.
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._load)
        if quarantine_unreadable and self._load_failed:
            self.quarantined_store = await asyncio.to_thread(self._quarantine_unreadable_locked)
        # Resolve history usability off the loop too (deferred in __init__).
        await asyncio.to_thread(self._history.prepare)
        return self

    def _quarantine_unreadable_locked(self) -> Path | None:
        """Rename an unparseable store aside under the store lock and reload."""
        try:
            with self._file_lock():
                moved = quarantine_unreadable_store(self._path)
                if moved is not None:
                    self._load()
        except (CronStoreBusy, OSError):
            # The startup this exists to rescue must not fail on the lock itself
            # (busy, or an unwritable lock file): keep the refusing store as-is.
            logger.warning("Could not lock the cron store; leaving the unreadable store in place")
            return None
        if moved is not None:
            logger.warning(
                "Cron store %s could not be parsed; moved it to %s and started with an "
                "empty schedule. Run `kirocrew doctor` for how to restore it.",
                self._path,
                moved,
            )
        return moved

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
        # BEFORE the timer is armed: a job the previous gateway died running
        # has no last_run_ts for that run, so it is due again the moment the
        # timer fires. The breaker must have paused it by then or the boot
        # re-runs the crash.
        await asyncio.to_thread(self._apply_loop_stall_breaker)
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
        tasks = [claim.task for claim in self._claims.values() if claim.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._claims.clear()

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
            now_mono = time.monotonic()
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
            for job_id, claim in list(self._claims.items()):
                # The list is a snapshot for iteration order only; the job's
                # run is whatever the store holds NOW. An earlier job's reap in
                # this same sweep awaited (its session reset, the locked
                # persist, the history append), and in that window this job's
                # run can end and a replacement claim the job: the snapshot's
                # claim then holds a run that is gone, with stamps that put the
                # job far past its deadline. Measure and reap only the stored
                # claim -- a replacement is a different object, measured on its
                # own stamps by the next sweep. (The six-dict shape this
                # replaces read the monotonic and jitter stamps live per
                # iteration, so a replacement's fresh stamp made the sweep
                # continue; the claim gives the same guarantee by identity.)
                if self._claims.get(job_id) is not claim:
                    continue
                # A claim cancel() or an earlier reap has taken is theirs to
                # finish: they popped the run's stamps, in the shape this
                # replaces, exactly so a sweep landing inside their kill awaits
                # would not reap the run a second time.
                if claim.taken:
                    continue
                # A task that is done() while its claim is still here never
                # reached _run_job_isolated's finally (a run that ends normally
                # releases the claim there, before its task finishes), so the
                # job still reads as running to the due-scan, _next_wake_secs
                # and run_job. Release it on THIS sweep, not once the run's
                # deadline passes: that is at least _JOB_TIMEOUT_SECS and up to
                # a day away, and every scheduled fire until then is skipped.
                # A live task or no tracked task falls through to the timeout
                # backstop below unchanged.
                if self.discard_finished_run(job_id):
                    continue
                elapsed = now - claim.claimed_at
                # DECIDE and REPORT on the monotonic clock. A claim with no
                # monotonic stamp (a manual run still parked in its store
                # refresh, or a test that seeds only the epoch stamp) falls back
                # to the wall-clock elapsed, so the backstop never stops reaping
                # — it just cannot tell suspend time apart for that run.
                #
                # The reported duration is monotonic too, not wall-clock: a
                # backward wall-clock step during a >=30-min run would otherwise
                # render a negative "ran -Ns"/"Reaped after -Ns" in the log and
                # the persisted history. Monotonic elapsed is equally legible
                # ("seconds since start") and cannot go negative.
                started_mono = claim.started_monotonic
                elapsed_mono = now_mono - started_mono if started_mono is not None else elapsed
                job = jobs_by_id.get(job_id)
                deadline = (
                    max(min(job.timeout_secs, 86400), _JOB_TIMEOUT_SECS)
                    if job
                    else _JOB_TIMEOUT_SECS
                ) + (_pool_queue_allowance(job) + _gate_budget_allowance(job) + _vet_allowance(job))
                jitter_allowance = claim.jitter or 0.0
                if elapsed_mono <= deadline + jitter_allowance:
                    continue
                logger.warning(
                    "Reaper: cron job %s exceeded %ds (ran %.0fs), force-killing",
                    job_id,
                    deadline,
                    elapsed_mono,
                )
                try:
                    # Named for the claim measured above: the reap takes that
                    # claim or nothing (``RunClaims.take``'s ``expected``), so a
                    # claim that changed under this sweep is never killed on
                    # the snapshot's stamps.
                    await self._force_reap(job_id, elapsed_mono, deadline, claim=claim)
                except Exception:
                    logger.exception("Reaper: failed to reap cron job %s", job_id)

    async def _force_reap(
        self,
        job_id: str,
        elapsed: float,
        deadline: int = _JOB_TIMEOUT_SECS,
        *,
        claim: _RunClaim,
    ) -> None:
        """Kill the run ``claim`` stands for: its session process and its task.

        ``claim`` is the run the caller measured over its deadline -- the
        sweep, the only caller, passes the claim it iterated. The reap takes
        that claim or nothing (identity): a stored claim that is a different
        object is a replacement run the caller never measured, a claim that is
        gone was a run that ended on its own, and a claim already taken is a
        teardown someone else owns -- none is killed, and the job's session,
        which is the replacement's now, is left alone. There is no reap by
        job id alone: a kill needs a run to answer for, and the run is the
        claim.
        """
        taken = self._runs.take(job_id, expected=claim)
        if taken is None:
            # Either the stored claim is taken -- cancel() or an earlier reap
            # owns this run's teardown and is inside its kill awaits; its
            # claim is not this reap's to pop below, and its terminal row is
            # the only one owed (a second "Reaped" row here would draw a
            # generation that could outrank a replacement run's) -- or the
            # measured run is gone, or a replacement this reap was not named
            # for stands in its place. The sweep skips all three itself; this
            # covers a store that changed under the sweep's awaits.
            return
        # Flag the run this reap took: its finalizer reads the flag off its
        # own claim, so no other run of the job can see it.
        taken.reaped = True
        reap_started_at = taken.claimed_at
        reap_trigger = taken.trigger
        # The kill, then the finish -- in a ``finally``, so the claim this reap
        # took is finished however the kill ends. ``taken`` is a lock with no
        # other owner-death recovery: from the take on, by design, the run's
        # own fences fail, the sweep skips the claim and discard_finished_run
        # refuses it. A kill that raised would otherwise leave the claim taken
        # for the life of the gateway -- the sweep swallows this coroutine's
        # exception and skips the claim on every later sweep, and every manual
        # run of the job 409s. What can raise here is a cancellation (the
        # reaper task cancelled at shutdown); the ``Exception`` arm of the
        # handler is a net for a reset failure the inner handlers let through,
        # and today none does: they catch every ``Exception`` the reset raises,
        # and ``_sigkill_session`` raises nothing. What the SIGKILL could not
        # do it REPORTS instead -- the guard's refusal of the pid, an error
        # the kill raised, an error ahead of the signal -- as its result, so
        # a process group it left alive is never recorded as reaped: the
        # failure, raised or reported, is carried into the terminal record
        # below (the run's finally writes none for a taken run) and audited as
        # ``failed``; a raised one is re-raised after the record, so the sweep
        # still logs it.
        #
        # EVERY key the run registered is ended (``_run_session_keys``): a
        # sequential job holds one key per agent, and an earlier agent's session
        # is kept alive for its pending sub-agents while a later agent runs --
        # a reap that ends only the newest key records ``reaped`` while that
        # earlier session and its sub-agents survive. Each key is FENCED from
        # before its first pass until the record and the audit are written
        # (``SessionManager.ending_key``, see ``_end_run_processes``): a claim
        # or cold start under it meets a key that is either being ended --
        # held at the door -- or recorded, never one that is neither. The
        # fences lift however the block ends; a kill failure that escaped is
        # re-raised after the lift.
        with ExitStack() as fences:
            keys = self._fence_run_keys(job_id, taken, fences)
            # The run's newest key names the record and the audit; the audit's
            # metadata lists what was actually ENDED -- filled only by the kill
            # passes, so a service without a session manager and a kill that
            # raised report none.
            session_key = keys[0]
            ended: list[str] = []
            kill_failure: BaseException | None = None
            sigkill_failure: str | None = None
            try:
                # Kill the session processes first: reset each key and kill what
                # the reset could not stop -- for every session that lands under
                # it, bounded (see ``_end_run_processes``).
                if self._sessions:
                    sigkill_failure, ended = await self._end_run_sessions(
                        job_id, taken, keys, fences, who="Reaper"
                    )
            except (Exception, asyncio.CancelledError) as exc:
                # CancelledError too: the reaper task is cancelled at shutdown, and
                # this reap still owes the finish and the record before it lets
                # the cancellation through. GeneratorExit and the interpreter-exit
                # signals are not caught (awaiting after them is an error); the
                # finally still finishes the claim.
                kill_failure = exc
            finally:
                # Cancel the asyncio task and release the claim directly. Don't rely on
                # _run_job_isolated's finally — the reaper exists for cases where the
                # normal path is stuck (idempotent with finally).
                self._runs.finish_taken(job_id)
            # One name for the record and the audit: a failure that escaped the
            # kill (re-raised below) or one the SIGKILL reported.
            kill_failed = (
                failure_name(kill_failure) if kill_failure is not None else sigkill_failure
            )

            # Update job state and persist. The persist goes through the locked
            # worker-thread merge helper (offloaded via asyncio.to_thread) — NOT a
            # bare on-loop self._save() — so it re-syncs under the store lock and
            # cannot clobber a concurrent add/update worker's just-written job
            # list, and its bounded lock spin never parks the event loop this
            # coroutine runs on. See _merge_terminal_state_locked.
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job:
                last_error = f"Reaped after {int(elapsed)}s (exceeded {deadline}s deadline)"
                if kill_failed is not None:
                    # Bounded at retention (``MAX_ERROR_DETAIL_LEN``): the kill's
                    # reason is not this code's to size -- a Windows tree drain that
                    # failed carries one line per process, several handles' failures
                    # are joined -- and ``last_error`` is persisted and shown as is.
                    last_error = with_kill_failure(last_error, kill_failed)
                last_run_ts = time.time()
                # Drawn here, in the same loop step as the release above (no await
                # between them), so a replacement claim always draws a higher one
                # and this record can never land over that run's.
                generation = self._runs.next_generation(job)
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
                        run_generation=generation,
                        result_produced=taken.started_monotonic is not None and job.result_produced,
                        # A reaped run that started counts toward auto-pause, like
                        # the wait_for timeout arm; one that never started does not.
                        count_failure=taken.started_monotonic is not None
                        and not job.failure_recorded
                        and not job.run_never_started,
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
                    # Never ``reaped`` for a process group the kill left alive:
                    # the reap ended the run's record, not its processes.
                    outcome="reaped" if kill_failed is None else "failed",
                    metadata={
                        "job_id": job_id,
                        "session_key": session_key,
                        # The keys this reap actually ended, newest first --
                        # only those every session of which was answered; a key
                        # whose kill was refused or failed is not listed (it
                        # stays registered, named in last_error), and none is
                        # when the passes never ran or raised.
                        "session_keys": ended,
                        "elapsed": int(elapsed),
                    },
                )
            except Exception:
                logger.exception("Reaper: SEL audit failed for cron %s", job_id)
        if kill_failure is not None:
            raise kill_failure

    def _session_process_handles(self, session_key: str) -> list[ProcessHandle]:
        """Every process the key names at snapshot time, the torn-down one first.

        Read BEFORE the reset by ``_force_reap`` and ``cancel`` (see
        :class:`kiro_crew.process_identity.ProcessHandle` for why the reset itself loses it).

        A miss in the live map is not yet "no process". The run's OWN teardown
        reset -- the run body's ``finally`` in the gateway, or the deferred reset
        a late sub-agent completion triggers -- pops the session out of the map
        before the awaits that can hang, so a reap or cancel that arrives after
        that pop finds nothing under the key while that reset still holds the
        process. That is the ordinary shape of a run that hangs in its teardown,
        not a timing corner: the reaper's cancel of the run task is what ends the
        hung reset, and nothing else re-examines the process once the audit is
        written. The session manager keeps every popped session readable through
        ``tearing_down`` for exactly the life of its teardown (one entry per
        teardown in flight, released when it ends however it ends -- that cancel
        included), so a successor whose own reset popped it and hung beside the
        run's teardown is a candidate too.

        Two sessions can stand under one key at once: that torn-down one, and a
        live successor a cold start registered under the key during the
        teardown's awaits (a sub-agent completion delivering into the key). The
        run's process is the torn-down one, but the reap ends the KEY: it resets
        whatever is live under it too, so both are candidates, each verified and
        killed on its own handle -- preferring either alone would leave the
        other's process unrecorded (a torn-down run process abandoned mid-hung
        shutdown by the reap's cancel, or a live successor a completed reset
        failed to stop). Distinct process incarnations only: the same ``(pid,
        start id)`` under both is one handle, the same pid under two start ids
        is two (:meth:`_handles_of`). Empty when neither exists: nothing to kill.
        """
        if not self._sessions:
            return []
        return self._handles_of(self._sessions_under(session_key))

    def _sessions_under(self, session_key: str) -> list[tuple[Any, ProcessHandle]]:
        """The sessions the key names right now, each with its kill handle: every torn-down one first, in pop order, then the live one.

        The torn-down ones are the sessions a reset popped under the key and
        still holds (``SessionManager.tearing_down``): the run's OWN teardown's,
        and any successor's whose own reset popped it and hung while the first
        teardown still ran -- each is a process the reap must answer, and
        retaining the first alone hid the successor's behind a record that said
        ``reaped``. Their handle is the one the retention captured AT THE POP
        (:class:`kiro_crew.session_lifecycle.TornDown`), never a re-read of the
        session: the teardown that holds them clears the provider's pid in its
        own awaits (the ACP client's reset, after a kill it could not confirm,
        then hangs on the transport), so a handle read now says the session
        names no process while the process stands, and the reap that trusted it
        recorded ``reaped`` over it. The live one is whatever the map holds
        under the key -- a successor a cold start registered during those
        teardowns -- with its handle read now, before the reset pops it. Read
        twice by :meth:`_end_run_processes`: once for the snapshot the kill
        works from, and once after the pass to tell a session the pass has not
        handled (a registration that landed after the reset's pop) from the ones
        it has -- by identity, since a session is handled once its process was
        answered, whatever key it sits under now. An entry of another shape (a
        double's default return) is not a session.
        """
        if not self._sessions:
            return []
        tearing_down = getattr(self._sessions, "tearing_down", None)
        torn = tearing_down(session_key) if callable(tearing_down) else None
        pairs: list[tuple[Any, ProcessHandle]] = []
        if isinstance(torn, list):
            for entry in torn:
                session = getattr(entry, "session", None)
                handle = getattr(entry, "handle", None)
                if session is not None and isinstance(handle, ProcessHandle):
                    pairs.append((session, handle))
        live = self._sessions._sessions.get(session_key)
        if live:
            pairs.append((live, process_handle_of(live)))
        return pairs

    @staticmethod
    def _handles_of(pairs: list[tuple[Any, ProcessHandle]]) -> list[ProcessHandle]:
        """One kill handle per distinct process incarnation among ``pairs``, in order.

        A handle with no pid names no process (the session had never spawned,
        or its client was already reset, when the handle was read).
        Distinctness is the handle's own identity, ``(pid, start id)``: two
        sessions naming the same incarnation are one handle -- their child records
        and group merged (``process_identity.add_handle``), since the two readings
        were taken at different moments -- while the same pid under two start ids
        is two processes -- a successor the kernel handed a dead predecessor's
        number -- and both are kept. A pid alone is never the key.
        """
        handles: list[ProcessHandle] = []
        for _session, candidate in pairs:
            if candidate.pid is None:
                continue
            add_handle(handles, candidate)
        return handles

    async def _end_run_processes(self, session_key: str, *, job_id: str, who: str) -> str | None:
        """Reset ``session_key`` and kill what the reset could not stop -- for every session that lands under it, bounded; the caller holds the key's ending fence.

        Runs under the key's ENDING FENCE (``SessionManager.ending_key``), which
        the caller -- ``_force_reap``, ``cancel()`` -- raises synchronously
        before awaiting this (so the first snapshot below is read with the fence
        already up) and holds until the run's terminal record and audit are
        written. While it is up, a claim or a new allocation under the key is
        HELD at the door of ``get_or_create`` -- it lands once the fence lifts,
        under a key whose run is recorded, so a sub-agent completion that races
        the reap is delivered after the record, never dropped and never into a
        key that is neither being ended nor recorded -- and an allocation
        already in flight when it went up -- a late sub-agent completion
        cold-starting the parent key again, caught inside ``provider.start()``
        with nothing published yet that any snapshot could see -- is
        invalidated: refused at registration when its start returns, fence up
        or lifted, with the provider it started hard-killed there by the
        closing manager's own path, and its call re-allocated after the lift.
        Without the fence that start published after the passes, with its
        process and the completion's injection running on behind a record that
        said ``reaped``. A start still past its spawn door when the passes end
        (:meth:`_spawn_in_flight`) is therefore reported, not waited for (its
        start may take seconds, and the fence has already settled what happens
        to it): a named kill failure, so the audit says ``failed`` -- while a
        claim merely waiting behind the key, or a cold start still ahead of that
        door, has started nothing and is not named. Nothing here lifts the
        fence: a registration after the passes is a new life under the key, not
        the run's process, and it lands only once the record it follows is
        written.

        One pass (:meth:`_reset_and_kill_once`) answers the sessions it saw: the
        snapshot (:meth:`_sessions_under`) and the session the reset popped. It
        is the ONLY pass, because nothing can land under the key after the pop
        while the fence is up: every door into the live map either meets the
        fence -- ``get_or_create``, at its front door, again after its busy-turn
        wait and again at registration, for a cold start and a warm-pool claim
        alike -- or never registers under a cron key at all
        (``open_task_session`` publishes the task runner's own ``taskrunner:``
        keys and refuses a key being ended at its door; the background session
        publishes its own key), and every cron key is ``cron:``-prefixed. A
        second reset-and-kill pass for a registrant the fence let through had
        no trigger on that path and is gone. The key is still read once more
        after the pass: a session under it that the pass did not handle (by
        identity -- the sessions the snapshot and the pop capture named, kept
        referenced so the comparison holds) is a registration the fence did not
        stop -- a manager without the fence, or a door this reasoning missed --
        and is a NAMED kill failure: reported, never reset, never signalled, so
        the audit says ``failed``, not ``reaped``. Returns every failure,
        joined, or None once every session under the key was answered and no
        start is past its spawn door under it.
        """
        pairs = self._sessions_under(session_key)
        failure, popped = await self._reset_and_kill_once(
            session_key, pairs, job_id=job_id, who=who
        )
        handled = [session for session, _handle in pairs]
        handled.extend(session for session in popped if not any(session is s for s in handled))
        late = [
            (session, handle)
            for session, handle in self._sessions_under(session_key)
            if not any(session is s for s in handled)
        ]
        late_failure: str | None = None
        if late:
            # Not chased: whatever landed under the key past the fence is
            # reported, and the run is not recorded as reaped over it.
            named = ", ".join(
                f"pid {handle.pid}" if handle.pid is not None else "no process handle yet"
                for _session, handle in late
            )
            logger.error(
                "%s: a session registered under %s after the reset's pop for cron %s (%s); "
                "not reset, not signalled",
                who,
                session_key,
                job_id,
                named,
            )
            late_failure = (
                f"a session registered under the key after the reset's pop ({named}); "
                "not reset, not signalled"
            )
        return join_failures(
            failure, late_failure, self._spawn_in_flight(session_key, job_id=job_id, who=who)
        )

    def _fence_run_keys(self, job_id: str, run: _RunClaim, fences: ExitStack) -> list[str]:
        """Raise the ending fence on every key ``run`` holds, synchronously, and return them newest first.

        Called by ``_force_reap`` and ``cancel()`` on entry to their fenced
        block, before any await: each fence is up before that key's first
        snapshot is read (see :meth:`_end_run_processes`), and ``fences`` lifts
        them all when the block ends -- after the run's terminal record and
        audit. A run that registered no key (a persistent job before
        registration, a script or command job, a legacy caller) is ended on the
        job's stable key ``cron:<job id>``, which then also names its record.
        """
        keys = self._run_session_keys(job_id, run) or [f"cron:{job_id}"]
        for key in keys:
            fences.enter_context(ending_fence(self._sessions, key))
        return keys

    async def _end_run_sessions(
        self,
        job_id: str,
        run: _RunClaim,
        keys: list[str],
        fences: ExitStack,
        *,
        who: str,
    ) -> tuple[str | None, list[str]]:
        """End every key of the run -- ``keys`` first, then any the run registers meanwhile, bounded -- and return the joined failure and the keys actually ended.

        ``keys`` are the run's keys the caller fenced on entry
        (:meth:`_fence_run_keys`); each is reset and its processes killed through
        :meth:`_end_run_processes`, and a key every session of which was answered
        is retired from the registry (:meth:`clear_active_session_key`), as the
        gateway retires a key its own reset completed. The list returned is
        exactly those keys -- the ones whose ending returned no failure -- and
        is what the audit's ``session_keys`` says was ended: a key whose kill was
        refused or failed is NOT in it (it stays registered, and its failure is
        in the record's ``last_error``), and it is not ended a second time
        either, because a key any round has attempted is never read back as a
        late registration. The keys of a round are
        ended CONCURRENTLY (``asyncio.gather``): each key's ending is bounded on
        its own (one reset of ``_REAPER_RESET_TIMEOUT`` and verified kills), and
        the fences stay up until the record is written, so a run with many keys
        -- one per agent of a sequential job -- ended one after another would
        hold its fences for the SUM of those bounds, past
        ``session_allocation.ENDING_FENCE_WAIT_SECS``, and a completion held at
        the door would be refused for a teardown that was merely long; ended
        together, the fences are up for one key's bound -- per round
        ``_REAPER_RESET_TIMEOUT`` plus the kill's own bounds (under 40 s),
        ``_ENDING_ROUNDS`` rounds at most, so well inside the 180 s a held
        caller waits. A key whose ending RAISED (a cancellation of the
        reaper task at shutdown is what can) interrupts no other key's: every
        ending of the round completes before the first exception is re-raised
        to the caller, which records it as the run's kill failure and re-raises
        it after the record, as for one key. The run's
        task is still alive while its keys are ended -- the caller cancels it
        only after the passes -- so a sequential run whose hung agent's session
        is reset can move on and register its NEXT agent's key while an earlier
        key is still being ended: after the round the run's keys are read again,
        a key no round has attempted is fenced and ended too, and the chase is
        bounded by ``_ENDING_ROUNDS`` rounds. A key the run registers after
        the last round is a named kill failure -- reported, never reset, never
        signalled -- so the audit says ``failed``, not ``reaped``.
        """
        ended: list[str] = []
        attempted: list[str] = []
        failures: list[str | None] = []
        pending = list(keys)
        for round_no in range(1, _ENDING_ROUNDS + 1):
            results = await asyncio.gather(
                *(self._end_run_processes(key, job_id=job_id, who=who) for key in pending),
                return_exceptions=True,
            )
            raised: BaseException | None = None
            for key, result in zip(pending, results):
                attempted.append(key)
                if isinstance(result, BaseException):
                    if raised is None:
                        raised = result
                    else:
                        logger.error(
                            "%s: ending %s for cron %s also raised",
                            who,
                            key,
                            job_id,
                            exc_info=result,
                        )
                    continue
                failures.append(result)
                if result is None:
                    # Ended: every session under the key was answered. A key
                    # whose kill was refused or failed is neither listed as
                    # ended nor retired -- the record says ``failed`` over it.
                    ended.append(key)
                    self.clear_active_session_key(job_id, key)
            if raised is not None:
                # Every ending of the round is over (``return_exceptions``): no
                # sibling is left running under a fence about to lift.
                raise raised
            pending = [key for key in self._run_session_keys(job_id, run) if key not in attempted]
            if not pending:
                break
            if round_no == _ENDING_ROUNDS:
                # Not chased: whatever the run keeps registering is reported,
                # and the run is not recorded as reaped over it.
                named = ", ".join(pending)
                logger.error(
                    "%s: cron %s registered %s after %d rounds of resets; not reset, not signalled",
                    who,
                    job_id,
                    named,
                    _ENDING_ROUNDS,
                )
                failures.append(
                    f"the run registered {named} after {_ENDING_ROUNDS} rounds of resets; "
                    "not reset, not signalled"
                )
                break
            logger.warning(
                "%s: cron %s registered %s while its keys were being ended; "
                "ending it too (round %d of %d)",
                who,
                job_id,
                ", ".join(pending),
                round_no + 1,
                _ENDING_ROUNDS,
            )
            for key in pending:
                fences.enter_context(ending_fence(self._sessions, key))
        return join_failures(*failures), ended

    def _spawn_in_flight(self, session_key: str, *, job_id: str, who: str) -> str | None:
        """The named failure for a cold start past its spawn door under the fenced key, or None -- logged under the job.

        The read itself, and what it names, is
        :func:`kiro_crew.process_identity.spawn_in_flight` (the sub-agent
        manager's reap reads it the same way); this adds the cron job to the log.
        """
        failure = spawn_in_flight(self._sessions, session_key)
        if failure is not None:
            logger.error("%s: %s -- under %s for cron %s", who, failure, session_key, job_id)
        return failure

    async def _reset_and_kill_once(
        self, session_key: str, pairs: list[tuple[Any, ProcessHandle]], *, job_id: str, who: str
    ) -> tuple[str | None, list[Any]]:
        """One reset of the key, then the kill of what it could not stop; the failure and the popped sessions.

        ``pairs`` is the snapshot -- the session objects the key named before
        the reset, each with its kill handle: the one the retention captured at
        its pop for a torn-down session, the one read now for the live one --
        whose kill handles are taken FIRST: the reset pops the
        session from the map before it can hang, so a kill that looked the key
        up afterwards would find nothing and leave the process it names running
        (see :class:`kiro_crew.process_identity.ProcessHandle`). The snapshot is
        keyed by name, so the session the reset ACTUALLY pops is captured at the
        pop itself (:func:`kiro_crew.process_identity.teardown_capture`): a cold
        start can register a new session under the key between the snapshot and
        the pop, and that one is
        what a hung reset then holds. A reset that hung or failed gets every
        handle killed; a completed one -- True, or False for a key a concurrent
        reset had already popped (the common False is the run's OWN teardown
        reset, popped before the caller looked and hung since; the caller's
        ``RunClaims.finish_taken`` cancel is what ends it) -- is not proof the
        process is gone (its own shutdown can fail without raising out of it; a
        False one stopped nothing), so every handle is asked (pid + recorded
        start id, then the tree it led: ``process_survived``) and a process still
        standing gets the fallback, whose report is what the record says. A
        popped session with no handle is a failure, not nothing.
        """
        if not self._sessions:
            return None, []
        handles = self._handles_of(pairs)
        reset_kwargs, popped = teardown_capture(self._sessions, session_key)
        seen = [session for session, _handle in pairs]
        failure: str | None = None
        try:
            await asyncio.wait_for(
                # Same class for the reaper and cancel: the run is over, so its
                # conversation is over and its sub-agent runs end with it -- not
                # a recycle.
                self._sessions.reset(session_key, ends_conversation=True, **reset_kwargs),
                timeout=_REAPER_RESET_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("%s: reset hung for cron %s, attempting SIGKILL", who, job_id)
            targets, missing = kill_set(handles, popped, seen=seen)
            failure = join_failures(
                await self._sigkill_sessions(session_key, targets, who=who, popped=popped),
                missing,
            )
        except Exception:
            logger.exception("%s: reset failed for cron %s, attempting SIGKILL", who, job_id)
            targets, missing = kill_set(handles, popped, seen=seen)
            failure = join_failures(
                await self._sigkill_sessions(session_key, targets, who=who, popped=popped),
                missing,
            )
        else:
            targets, missing = kill_set(handles, popped, seen=seen)
            # Off the loop: the scan walks every recorded child.
            survivors = [handle for handle in targets if await process_survived_async(handle)]
            if survivors:
                logger.warning(
                    "%s: process survived the reset for cron %s, attempting SIGKILL", who, job_id
                )
                failure = await self._sigkill_sessions(
                    session_key, survivors, who=who, popped=popped
                )
            failure = join_failures(failure, missing)
        return failure, [session for session, _handle in popped]

    async def _sigkill_sessions(
        self,
        session_key: str,
        handles: list[ProcessHandle],
        *,
        who: str = "Reaper",
        popped: list[tuple[Any, ProcessHandle]] | None = None,
    ) -> str | None:
        """Kill every handle's process (:func:`kiro_crew.process_identity.kill_each`); the failures joined, or None.

        ``popped`` is the caller's captured pop, forwarded so each kill can release
        the lease of the session its own reset destroyed even when that reset was
        cancelled before ``provider.shutdown()`` -- the case where the manager's
        torn-down table has already been unwound and holds nothing.
        """
        return await kill_each(
            handles,
            lambda handle: self._sigkill_session(session_key, handle, who=who, popped=popped),
        )

    async def _sigkill_session(
        self,
        session_key: str,
        handle: ProcessHandle | None,
        *,
        who: str = "Reaper",
        popped: list[tuple[Any, ProcessHandle]] | None = None,
    ) -> str | None:
        """Best-effort SIGKILL when the graceful reset hangs, fails, or left the process standing.

        ``handle`` is one process handle the caller took before the reset
        (:meth:`_session_process_handles`), and it is the ONLY thing that names
        the process: the map is never consulted here (a session under the key
        now is a successor registered during the reset's awaits, verified and
        killed on its own handle by the caller), and ``session_key`` names the
        run in the log only, prefixed with ``who`` -- the caller's name. ``None``
        means no session was live, or being torn down, under the key before the
        reset: nothing to kill, not a failure. The kill itself -- the two
        start-id reads around the child walk, the group signal (by the group id
        retained while the leader was alive once the leader is gone), the
        pid-scoped fallback, the escaped-children sweep -- is
        :func:`kiro_crew.process_identity.kill_verified_process`, the one
        definition this method shares with the sub-agent manager's teardown
        paths. It never
        raises (the caller took the run's claim and must still finish it) and
        never swallows: it returns what stopped the kill, and the caller records
        it so the audit does not say the run was reaped over a process tree left
        alive.
        """
        if not self._sessions:
            return None  # no session manager: nothing to kill
        if handle is None:
            # Nothing to kill, not a failure: no session was live, or being
            # torn down, under the key before the reset, so the run has no
            # process group to answer for. The map is deliberately not read:
            # whatever it holds under the key now was registered after the
            # snapshot -- a successor, not this run's process.
            logger.warning("%s: no session found for %s", who, session_key)
            return None
        # Ownership, asked once before the escalation below starts. The gate answers
        # from two tables and refuses on either: a LEASE, held by the session that
        # owns the runtime, and a TENANCY, held by a party mid-flight on the process
        # without owning it. At cap=1 a session-sharing sub-agent takes no lease --
        # an acquisition cannot join an occupied runtime -- so the tenancy table is
        # what speaks for it, and the gate reads that too. A refusal for a tenant is
        # not this caller's mistake: the answer is to let the tenant finish, and its
        # own last release hands the orphan back for teardown.
        #
        # The run's OWN lease is released first, and that ordering is the whole
        # correctness of the gate here. A reset releases the lease inside
        # ``provider.shutdown()``, so every await before it -- the ended-record
        # write, the queue unlink, the child probes -- is a point where the
        # teardown can hang or raise and land on this path with the lease still
        # held. Asking the gate then would let the session being destroyed refuse
        # its own last-resort kill: the wedged process would survive, and its pid
        # would go on being refused by every sweep for the gateway's life while
        # the run recorded a false "leased by another tenant". The rule this
        # follows is the one the allocation path's own failure handler states --
        # release before the kill, or the cleanup refuses its own teardown.
        #
        # What survives the release is a lease held by a DIFFERENT tenant, which
        # is the only thing that may withhold the signal. A refusal is then
        # reported the same way a refused pid is: as the thing that stopped the
        # kill, so the run is never recorded as reaped over a process tree that is
        # still standing.
        # The captured pop goes with it: a reset this run abandoned on its timeout
        # has already unwound the scope that made the subject readable through
        # ``tearing_down``, and the pop the caller holds is then the only thing that
        # still names the session whose lease must go before the gate is asked.
        await release_teardown_lease(self._sessions, session_key, handle, who=who, popped=popped)
        handle_pid = getattr(handle, "pid", None)
        if isinstance(handle_pid, int) and not authorize_runtime_kill(
            handle_pid,
            reason=f"cron run teardown for {session_key}",
            caller="cron._sigkill_session",
        ):
            logger.warning(
                "%s: %s still leased by another tenant; not signalling it", who, session_key
            )
            return "runtime still leased by another tenant"
        # The client's child-tree probe, record capture and escaped-children sweep,
        # resolved through the session module at call time (circular import:
        # session → cron; and a test's patch of the client module is what the
        # sweep must run). Cron itself never reaches the ACP layer.
        from kiro_crew.session import child_process_helpers

        # The gate's answer above is separated from the first signal by the verified
        # kill's own start-id read, group resolution and descendant walk. The barrier
        # makes it current and shuts that window; a pid it does not grant has gained a
        # tenant, and the refusal is reported the same way a refused pid is.
        with teardown_barriers([handle_pid], who=who) as barriered:
            if isinstance(handle_pid, int) and not barriered:
                logger.warning(
                    "%s: %s gained a tenant after the gate allowed it; not signalling it",
                    who,
                    session_key,
                )
                return "a tenant claimed the runtime after the gate allowed it"
            return await kill_verified_process(
                handle, who=who, key=session_key, child_helpers=child_process_helpers()
            )

    # ── User-initiated cancellation ──

    async def cancel(self, job_id: str) -> bool:
        """Cancel a running cron execution (user-initiated).

        Kills the sandboxed subprocess (script/command crons) or the kiro-cli
        session (agent crons), cancels the asyncio task, records a
        ``cancelled`` history entry, and leaves ``consecutive_failures``
        untouched. Returns True when a running execution was found. A kill
        that raises still cancels the task, releases the run's claim and
        records the entry (its error names the failure), then propagates.
        """
        # A finished task is not a running execution, whatever the claim says.
        # Trusting the claim here would kill nothing, answer True, record a
        # "Cancelled by user after Ns" row for a run that ended long ago.
        # Release the leftovers and answer "not running" instead; a live
        # task is untouched and cancels below.
        self.discard_finished_run(job_id)
        if job_id not in self._claims:
            return False
        claim = self._runs.take(job_id)
        if claim is None:
            # The stored claim is already taken: another cancel() (a
            # double-clicked Cancel) or the reaper is inside its kill awaits
            # and owns this run's teardown. Carrying on would pop ITS claim at
            # step 3, ending the job's occupancy before that kill finished,
            # and write a second terminal row whose generation could outrank
            # a replacement run's. Nothing here is this caller's to cancel:
            # answer "not running", as the route does for an idle job.
            return False
        logger.info("Cancel: user-initiated cancellation of cron job %s", job_id)
        # Flag the run this cancel took (_RunClaim.cancelled): the flag lives
        # on this run's claim, so only this run's finalizer can read it.
        claim.cancelled = True
        started_at = claim.claimed_at
        trigger = claim.trigger
        elapsed = time.time() - started_at

        job = next((j for j in self._jobs if j.id == job_id), None)
        is_agent_job = job is None or not (job.script or job.command)

        # Steps 1-2 are the kill awaits; step 3 finishes the claim in a
        # ``finally``, so it runs however they end. ``taken`` is a lock with no
        # other owner-death recovery: from the take on, by design, the run's
        # own fences fail, the reaper sweep skips the claim and
        # discard_finished_run refuses it. A kill that raised would otherwise
        # leave the claim taken until restart: every manual run of the job 409s
        # and every scheduled fire is skipped. What can raise here: step 1's
        # executor hop (a pool shut down under it, a thread refused at
        # interpreter exit -- RuntimeError either way) and a cancellation of
        # this handler when the client disconnects. Step 2 raises nothing but
        # a cancellation: its inner handlers catch every ``Exception`` the
        # reset raises, and ``_sigkill_session`` raises nothing -- it REPORTS
        # a refused pid or a failed kill as its result instead, so a process
        # group it left alive is never recorded as cancelled. The failure,
        # raised or reported, is carried into the terminal record (the run's
        # finally writes none for a taken run) and audited as ``failed``; a
        # raised one is re-raised after the record, so the caller still learns
        # the kill failed.
        #
        # EVERY key the run registered is ended (``_run_session_keys``; a
        # sequential job holds one per agent, an earlier agent's kept alive for
        # its pending sub-agents), each FENCED from before step 1 until step 4's
        # record and the audit are written (``SessionManager.ending_key``, see
        # ``_end_run_processes``): a claim or cold start under it meets a key
        # that is either being ended -- held at the door -- or recorded, never
        # one that is neither. A script or command job's key names no session,
        # so the fence gates nothing for it. The fences lift however the block
        # ends; a kill failure that escaped is re-raised after the lift.
        killed_proc = False
        with ExitStack() as fences:
            keys = self._fence_run_keys(job_id, claim, fences)
            # The run's newest key names the record and the audit; the audit's
            # metadata lists what was actually ENDED -- filled only by step 2, so
            # a script or command job (whose key names no session and whose
            # step 2 is skipped) and a kill that raised report none.
            session_key = keys[0]
            ended: list[str] = []
            kill_failure: BaseException | None = None
            sigkill_failure: str | None = None
            try:
                # 1. Script/command crons: SIGTERM the sandboxed subprocess group.
                # Offloaded: kill_running_process performs blocking kernel calls.
                killed_proc = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), cron_script.kill_running_process, job_id
                )

                # 2. Agent crons: kill the kiro-cli sessions (mirrors _force_reap):
                # reset each of the run's keys and kill what the reset could not
                # stop, for every session that lands under it, bounded
                # (``_end_run_sessions``).
                if self._sessions and is_agent_job and not killed_proc:
                    sigkill_failure, ended = await self._end_run_sessions(
                        job_id, claim, keys, fences, who="Cancel"
                    )
            except (Exception, asyncio.CancelledError) as exc:
                # CancelledError too: aiohttp cancels the route's handler when the
                # client disconnects mid-cancel, and this coroutine still owes the
                # finish and the record before it lets the cancellation through.
                # GeneratorExit and the interpreter-exit signals are not caught
                # (awaiting after them is an error); the finally still finishes.
                kill_failure = exc
            finally:
                # 3. Cancel the asyncio task and release the claim directly
                # (idempotent with _run_job_isolated's finally).
                self._runs.finish_taken(job_id)
            # One name for the record and the audit: a failure that escaped the
            # kill (re-raised below) or one the SIGKILL reported.
            kill_failed = (
                failure_name(kill_failure) if kill_failure is not None else sigkill_failure
            )

            # 4. Update job state, persist, and record history. The persist goes
            # through the locked worker-thread merge helper (offloaded via
            # asyncio.to_thread) — NOT a bare on-loop self._save() — so it re-syncs
            # under the store lock and cannot clobber a concurrent add/update
            # worker; the bounded spin never parks this loop-side coroutine.
            if job:
                last_error = f"Cancelled by user after {int(elapsed)}s"
                if kill_failed is not None:
                    # Same bound as the reaper's (``with_kill_failure``).
                    last_error = with_kill_failure(last_error, kill_failed)
                last_run_ts = time.time()
                # Drawn here, in the same loop step as the release in step 3 (no
                # await between them), so a replacement claim always draws a higher
                # one and this record can never land over that run's.
                generation = self._runs.next_generation(job)
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
                        run_generation=generation,
                        result_produced=claim.started_monotonic is not None and job.result_produced,
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
                    # Never ``cancelled`` for a process group the kill left alive.
                    outcome="cancelled" if kill_failed is None else "failed",
                    metadata={
                        "job_id": job_id,
                        "session_key": session_key,
                        # The keys this cancel actually ended, newest first --
                        # only those every session of which was answered; a key
                        # whose kill was refused or failed is not listed (it
                        # stays registered, named in last_error). None for a
                        # script or command job, whose sessions step 2 never
                        # touches, or when step 2 raised.
                        "session_keys": ended,
                        "elapsed": int(elapsed),
                        # Named for what the return now MEANS, not for what it used
                        # to. kill_running_process returns True either because it
                        # signalled a live child OR because it recorded the cancel
                        # against a spawn still in flight, where there is no child to
                        # signal yet. Auditing that second case as
                        # "killed_subprocess" asserted a kill that never happened.
                        "cancellation_accepted": killed_proc,
                    },
                )
            except Exception:
                logger.exception("Cancel: SEL audit failed for cron %s", job_id)
        if kill_failure is not None:
            raise kill_failure
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
        member_id: str = "",
        model: str = "",
        silent: bool = False,
        timezone: str = "",
        skip_dates: list[str] | None = None,
        strict_schedule: bool = False,
        hide_in_chat: bool = False,
        folder_id: str = "",
        chat_folder_id: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        session_key: str = "",
        minimal_context: bool = False,
        timeout: int = 0,
        timeout_secs: int = 0,
        auto_pause_after_failures: int | None = None,
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
            member_id=member_id,
            model=model,
            silent=silent,
            timezone=timezone,
            skip_dates=skip_dates,
            strict_schedule=strict_schedule,
            hide_in_chat=hide_in_chat,
            folder_id=folder_id,
            chat_folder_id=chat_folder_id,
            command=command,
            script=script,
            agent_sequence=agent_sequence,
            env=env,
            persistent_session=persistent_session,
            session_key=session_key,
            minimal_context=minimal_context,
            timeout=timeout,
            timeout_secs=timeout_secs,
            auto_pause_after_failures=auto_pause_after_failures,
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
        persisted = await asyncio.to_thread(self._persist_add_if_absent_locked, predicate, job)
        if not persisted:
            return None
        self._arm_timer()
        logger.info("Added cron job '%s' (%s) [if-absent]", job.name, job.id)
        return job

    def add_managed_job(self, managed_by: str, **kwargs: Any) -> CronJob:
        """Create the job an installer owns under the key *managed_by*.

        *kwargs* are :meth:`add_job`'s fields and are validated by the same
        :func:`build_job` before any disk work. The key is unique across the
        store, and this is its only writer, so the uniqueness holds by
        construction: an existing job under the key is a refusal
        (``ValueError`` naming its id), checked INSIDE the store lock after
        ``_sync_for_write()`` so two installers racing the same key cannot both
        see it absent and persist two jobs.

        There is deliberately no replace-in-place. Every run-driven removal --
        a script that raises ``Done``, a one-shot consumed on completion, a
        deferred removal -- deletes its row BY ID with no record of which
        definition the run started from, so a replacement reusing the id would
        be deleted by the run it replaced. An installer re-installs with
        ``cron remove --managed-by KEY`` then ``cron add --managed-by KEY``: the
        new job gets a fresh id, so an in-flight run can only remove the dead
        one. Replace-in-place needs a definition-generation fence first.
        """
        validate_managed_by(managed_by)
        job = self._build_job(**kwargs)
        job.managed_by = managed_by
        # The shared locked check-then-append core: the key check runs after
        # the in-lock reload, so two racing installers cannot both persist.
        if not self._persist_add_if_absent_locked(lambda j: j.managed_by == managed_by, job):
            existing = ", ".join(
                j.id for j in self.list_jobs(include_disabled=True) if j.managed_by == managed_by
            )
            raise ValueError(
                f"a job is already managed by {managed_by!r} ({existing}); "
                f"remove it with 'cron remove --managed-by {managed_by}' first"
            )
        self._arm_timer()
        logger.info("Added managed cron job '%s' (%s) for %s", job.name, job.id, managed_by)
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

        Applies the same dead-parent guard as :meth:`_persist_add_locked`; see
        :meth:`_drop_owner_if_parent_gone`.
        """
        with self._file_lock():
            self._sync_for_write()
            if any(predicate(existing) for existing in self._jobs):
                return False
            bind_cron_memory(job)
            self._drop_owner_if_parent_gone(job)
            self._jobs.append(job)
            self._save()
        job.audit_auto_pause_limit_change(None)
        return True

    def _drop_owner_if_parent_gone(self, job: CronJob) -> None:
        """Blank a ``cron:`` owner whose parent is absent. MUST hold the store lock.

        Call after ``_sync_for_write()`` and before appending, so the decision is
        made against the authoritative reloaded store. This is what makes a child
        under a dead parent STRUCTURALLY impossible rather than merely cleaned up
        afterwards: the removal cascade
        (:meth:`_release_children_of_removed`) can only release children that
        existed when it scanned, and a run of the parent still in flight can call
        ``cron_add`` in the window between that scan and its own teardown — the
        new row would then be born stamped with a key no session can ever present
        again. Both halves resolve against the same in-lock reload, so whichever
        transaction lands second sees the other's write.

        Dropped rather than refused: the caller's request to schedule work is
        honoured and the row lands in the documented ownerless state the CLI and
        the Schedule page manage, which is the semantics every release path here
        uses. Refusing would lose the user's job over a race they did not cause.
        """
        principal = cron_job_id_from_session_key(job.session_key)
        if not principal or any(j.id == principal for j in self._jobs):
            return
        logger.warning(
            "Cron job %s created with owner %s whose cron no longer exists; "
            "storing it ownerless (manage from CLI or the Schedule page)",
            job.id,
            job.session_key,
        )
        job.session_key = ""

    #: Validate the inputs and construct the job, no I/O and no lock
    #: (:func:`~kiro_crew.cron_service.fields.build_job`). Every add path builds
    #: through it, so each create surface validates identically before disk work.
    _build_job = staticmethod(build_job)

    def _persist_add_locked(self, job: CronJob) -> None:
        """Lock/reload/append/save for a new job — the thread-safe disk core.

        Does NO timer work (``_arm_timer`` needs the event loop), so
        :meth:`add_job_async` can run it in an executor thread. Raises
        :class:`CronStoreBusy` if the store lock stays contended past the
        timeout. Mirrors the :meth:`_remove_jobs_locked` batch precedent.

        Applies the dead-parent guard before the append; see
        :meth:`_drop_owner_if_parent_gone`.
        """
        with self._file_lock():
            self._sync_for_write()
            bind_cron_memory(job)
            self._drop_owner_if_parent_gone(job)
            self._jobs.append(job)
            self._save()
        job.audit_auto_pause_limit_change(None)

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
        member_id: str = "",
        model: str = "",
        silent: bool = False,
        timezone: str = "",
        skip_dates: list[str] | None = None,
        strict_schedule: bool = False,
        hide_in_chat: bool = False,
        folder_id: str = "",
        chat_folder_id: str = "",
        command: str = "",
        script: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        session_key: str = "",
        minimal_context: bool = False,
        timeout: int = 0,
        timeout_secs: int = 0,
        auto_pause_after_failures: int | None = None,
        source_preset: str = "",
        source_template_prompt: str = "",
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
            member_id=member_id,
            model=model,
            silent=silent,
            timezone=timezone,
            skip_dates=skip_dates,
            strict_schedule=strict_schedule,
            hide_in_chat=hide_in_chat,
            folder_id=folder_id,
            chat_folder_id=chat_folder_id,
            command=command,
            script=script,
            agent_sequence=agent_sequence,
            env=env,
            persistent_session=persistent_session,
            session_key=session_key,
            minimal_context=minimal_context,
            timeout=timeout,
            timeout_secs=timeout_secs,
            auto_pause_after_failures=auto_pause_after_failures,
        )
        # Dashboard-only template provenance. Set on the freshly-built job
        # BEFORE the off-loop persist -- the object has no other reference yet,
        # so this is still a single fully-formed first save, not a
        # build-then-mutate-then-second-save. Kept off _build_job because only
        # this async path is ever called with them (the sync add_job, CLI, MCP
        # and apps SDK never carry a template), so threading them through the
        # shared constructor would be surface with no consumer.
        if source_preset:
            job.source_preset = source_preset
            job.source_template_prompt = source_template_prompt
        await asyncio.to_thread(self._persist_add_locked, job)
        self._arm_timer()
        logger.info("Added cron job '%s' (%s)", name, job.id)
        return job

    def update_job(self, job_id: str, **kwargs: Any) -> CronJob | None:
        """Update fields on an existing job. Returns updated job or None if not found.

        Accepted kwargs: name, message, every_secs, cron_expr, agent_id, channel,
        approval_mode, silent, skip_dates, timezone, thread_ts, model,
        timeout_secs (per-wake execution budget, 1..86400),
        auto_pause_after_failures (0..10000; 0 = never auto-pause).

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

        ``chat_folder_transition_out``: pass a dict to learn the folder
        ``chat_folder_id`` held before this call replaced it. Filled under the
        store's file lock, so it is atomic with the write; owned by the caller, so
        concurrent callers cannot see or overwrite each other's answer.
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
        # Preconditions, not fields: popped before the field gates below ever
        # see them. When present, the freshly reloaded (locked) record must
        # still carry exactly the pending request the caller decided on.
        expect_pending = kwargs.pop("expect_secret_env_pending", None)
        expect_pending_ts = kwargs.pop("expect_secret_env_pending_ts", None)
        # Same shape for the ACTIVE grant fields: the approval's compensating
        # restore names the just-promoted (dead) grant here, so a concurrent
        # revoke that already cleared the fields makes the restore a no-op
        # instead of resurrecting state the operator withdrew.
        expect_active = kwargs.pop("expect_secret_env", None)
        expect_active_pin = kwargs.pop("expect_secret_env_pin", None)
        # Optional OUT-parameter, owned by the caller: a dict this pass fills with
        # ``{"chat_folder_was": <prior folder, possibly "">}`` when the update
        # actually changes ``chat_folder_id``.
        #
        # An out-parameter rather than a field on the job, and the distinction is
        # the whole reason this exists. The dashboard must move the job's chat tab
        # out of its previous folder, and may only do so when the tab is still
        # sitting where this feature put it -- so it needs the PRIOR value, read
        # atomically with the write that replaces it. Reading it with a separate
        # query is a read-then-write race. Stamping it on the ``CronJob`` closes
        # that race and opens another: ``self._jobs`` holds ONE object per job, so
        # concurrent callers share the attribute and each one's answer is visible
        # to, and clobberable by, the others. A dict the caller allocated is seen
        # by that caller alone.
        chat_folder_out = kwargs.pop("chat_folder_transition_out", None)
        with self._file_lock():
            self._sync_for_write()
            for job in self._jobs:
                if job.id != job_id:
                    continue
                if "member_id" in kwargs and (kwargs["member_id"] or "") != job.member_id:
                    raise ValueError("member memory is fixed for this schedule; create a new job")
                if expect_pending is not None and job.secret_env_pending != expect_pending:
                    raise CronPendingMismatch("pending secret request changed")
                if expect_pending_ts is not None and job.secret_env_pending_ts != expect_pending_ts:
                    raise CronPendingMismatch("pending secret request was re-issued")
                if expect_active is not None and job.secret_env != expect_active:
                    raise CronPendingMismatch("active grant changed concurrently")
                if expect_active_pin is not None and job.secret_env_pin != expect_active_pin:
                    raise CronPendingMismatch("active grant pin changed concurrently")
                previous_limit = job.auto_pause_after_failures
                apply_job_update(job, kwargs, chat_folder_out)
                self._save()
                job.audit_auto_pause_limit_change(previous_limit)
                logger.info("Updated cron job %s", job_id)
                return job
        return None

    def remove_job(
        self,
        job_id: str,
        *,
        actor: str,
        source: str,
        one_shot_path: str | None = None,
    ) -> bool:
        """Remove a job by ID.

        ``actor`` and ``source`` are required so every caller-requested
        removal is attributable at this mutation seam. Automated one-shot
        callers additionally provide ``one_shot_path`` to retain their
        distinct audit outcome and path discriminator.

        Raises :class:`CronStoreBusy` on lock contention; see
        :meth:`remove_job_async` for the event-loop-safe variant.
        """
        ok = self._remove_job_locked(job_id)
        self._audit_requested_removal(
            job_id,
            removed=ok,
            actor=actor,
            source=source,
            one_shot_path=one_shot_path,
        )
        if ok:
            self._arm_timer()
        return ok

    async def remove_job_async(
        self,
        job_id: str,
        *,
        actor: str,
        source: str,
        one_shot_path: str | None = None,
    ) -> bool:
        """Event-loop-safe :meth:`remove_job`: the lock+save runs off the loop.

        Raises :class:`CronStoreBusy` (retryable) on sustained contention.
        """
        ok = await asyncio.to_thread(self._remove_job_locked, job_id)
        self._audit_requested_removal(
            job_id,
            removed=ok,
            actor=actor,
            source=source,
            one_shot_path=one_shot_path,
        )
        if ok:
            self._arm_timer()
        return ok

    def _audit_requested_removal(
        self,
        job_id: str,
        *,
        removed: bool,
        actor: str,
        source: str,
        one_shot_path: str | None = None,
    ) -> None:
        """Audit one removal after persistence and outside the store lock."""
        if one_shot_path is not None:
            if removed:
                self.audit_one_shot_removal(job_id, one_shot_path)
            return
        resources = f"job_id={job_id}"
        if not removed:
            resources += " reason=not_found"
        try:
            sel.sel().log_api_access(
                caller=actor,
                operation="cron.remove",
                outcome="allowed" if removed else "not_found",
                source=source,
                resources=resources,
            )
        except Exception:
            logger.warning("SEL audit for cron removal failed (job %s)", job_id, exc_info=True)

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

    def audit_one_shot_removal(self, job_id: str, path: str) -> None:
        """SEL-audit one automated one-shot removal. Call AFTER the store lock.

        An automated removal with no human caller is exactly the delete an
        operator cannot otherwise distinguish from data loss. Emits the same
        ``cron.remove`` shape as the caller-requested single-delete path
        (:meth:`_audit_requested_removal`, serving dashboard/MCP/CLI), with an
        automated-actor identity and a ``one_shot_completed`` outcome.
        ``source`` stays ``"cron"`` — the SEL spec treats ``source`` as a
        constrained identity vocabulary (it skips redaction on that promise),
        and ``"cron"`` is this module's established value — so the removal
        path rides in ``resources`` as a ``path=`` discriminator instead.
        Best-effort and exception-contained: the removal is already saved, so
        audit unavailability must never break the caller. Never call while
        holding ``_file_lock`` — the first ``sel()`` of a process constructs
        the log and must not extend the store-lock hold.
        """
        try:
            sel.sel().log_api_access(
                caller="cron",
                operation="cron.remove",
                outcome="one_shot_completed",
                source="cron",
                resources=f"job_id={job_id} path={path}",
            )
        except Exception:
            logger.warning(
                "SEL audit for one-shot cron removal failed (job %s)", job_id, exc_info=True
            )

    def _drain_pending_removals_locked(self) -> list[str]:
        """Delete jobs queued via :meth:`defer_removal`. MUST hold the store lock.

        Returns the ids actually removed (sorted, empty when nothing drained)
        so the caller can SEL-audit them after releasing the store lock.

        Called from :meth:`_tick_scan_locked` (the timer tick's worker-thread
        transaction) inside its ``_file_lock`` block, so the delete+save is
        serialized against every other
        mutator exactly like the other locked cores. Removes only the queued
        ids still present after the tick's ``_sync``; saves once iff something
        was actually removed (an all-missing queue never rewrites the file).
        An id no longer present was already removed elsewhere, so dropping it
        is correct -- but ONLY when the load succeeded. Under ``_load_failed``
        the list is unknown rather than empty, so this returns before claiming
        (see the guard below) instead of intersecting against nothing.

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
            return []
        if self._load_failed:
            # Return WITHOUT claiming. The claim below is a reset, and `present`
            # is built from `self._jobs`, which a failed load has emptied -- so
            # the intersection would be empty and the early return below would
            # drop the whole queue before ever reaching the `_save` its requeue
            # arm guards. Absence from an unloaded list means "unknown", not
            # "already removed", and dropping the intent lets the repaired store
            # re-run a completed one-shot and notify a second time.
            logger.warning("Deferred cron removals held: store unreadable, retrying next tick")
            return []
        # Atomic claim-and-reset (see docstring) — do NOT split into a read
        # (``& present``) followed by ``.clear()``; an id added between those
        # two steps would be erased without ever being deleted from disk, so
        # the completed one-shot would re-fire and re-notify.
        pending, self._pending_removals = self._pending_removals, set()
        present = {j.id for j in self._jobs}
        to_remove = pending & present
        if not to_remove:
            return []
        # BACKGROUND tick: a failed epoch bump must not crash the scan, but
        # it must also not let the delete proceed (the saved grant record
        # would be replayable once the epoch state heals). Requeue exactly
        # like the store-unreadable case and retry next tick.
        try:
            self._bump_grant_epochs_for(to_remove)
        except (OSError, ValueError):
            logger.warning("Deferred cron removals held: grant-epoch bump failed", exc_info=True)
            self._pending_removals |= pending
            return []
        # A Done()/delete_after_run job that self-removes retires its principal
        # just as a CLI remove does, so its children are released in the SAME
        # save (see _remove_job_rows).
        restore = self._remove_job_rows(to_remove)
        # BACKGROUND writer: this runs inside the due-scan, so an unreadable
        # store must not abort the tick and stop every other job. The deferred
        # delete simply stays pending until the store is readable again.
        try:
            self._save()
        except BaseException as exc:
            # EVERY save failure rolls back, not just CronStoreUnreadable. _save
            # also raises bare OSError (ENOSPC/EROFS/EIO out of atomic_write),
            # which a narrow `except CronStoreUnreadable` let past this block
            # entirely: the child owners stayed cleared in memory while disk still
            # named the old owner, the queue stayed empty, and the fingerprint
            # still matched the untouched file so no _sync would ever reload the
            # truth back. The next successful save then persisted the cleared
            # owners -- a silent release nothing asked for, from a removal that
            # never happened.
            for child, previous_owner in restore:
                child.session_key = previous_owner
            # REQUEUE, or the intent is lost outright. The claim above already
            # emptied the queue, so the comment's promise that the delete "stays
            # pending until the store is readable again" only holds if it is put
            # back: the next _sync reloads the job from the file that still holds
            # it, and a completed one-shot would run and notify a SECOND time.
            # Union rather than assignment -- a concurrent defer_removal may have
            # added to the fresh replacement set since the swap.
            self._pending_removals |= to_remove
            # self._jobs still has the removed rows filtered out, and only a
            # reload can put them back -- so drop the fingerprint to force one.
            # Without it the retry above is a promise nothing can keep: the next
            # drain intersects the requeued ids against a _jobs that omits them,
            # finds nothing to remove, and silently drops the intent.
            self._reset_fingerprint()
            if isinstance(exc, CronStoreUnreadable):
                logger.warning("Deferred cron removal not persisted: %s", exc)
                # Empty list, not a bare return: the caller SEL-audits what came
                # back, and nothing was durably removed.
                return []
            # Anything else is a real write fault, not the tolerated
            # store-unreadable case: surface it rather than reporting a quiet
            # no-op tick after the disk refused the write.
            raise
        for jid in to_remove:
            logger.info("Removed deferred one-shot cron job %s", jid)
        # SEL audit is the CALLER's job (post-lock): this method runs inside
        # the caller's ``_file_lock`` transaction, and the first ``sel()`` of a
        # process constructs the log (trust-dir + HMAC key read), which must
        # never extend the store-lock hold past the CronStoreBusy timeout.
        return sorted(to_remove)

    def _bump_grant_epochs_for(self, removed_ids: set[str]) -> None:
        """Kill the secret grants of jobs about to be deleted from the store.

        A deleted job's record (mapping + active pin) survives as
        agent-readable history, and the store file is agent-writable:
        without an epoch bump, re-creating the job from the saved record
        would let the runner verify the old pin and inject the secret
        again. Bumping BEFORE the store swap keeps the revoke fence's
        fail-closed direction — and a FAILED bump (unwritable/corrupt epoch
        state) raises so the caller ABORTS the delete: deleting while the
        old epoch is still live would leave the saved record replayable the
        moment the epoch state heals. Owner-driven removal paths propagate
        the error; background ticks catch it and requeue/skip the delete
        instead of crashing the scan.
        """
        # An id with a LIVE epoch entry must bump even when the record no
        # longer carries grant fields: the store is agent-writable, so an
        # agent can CLEAR the fields, delete the job, and replay the saved
        # mapping+pin into a re-created job — the pin was minted under the
        # still-committed epoch. An id with neither grant fields nor an
        # epoch entry never had an active pin minted (pins are HMAC-keyed
        # and only the approval path commits entries), so skipping it is
        # safe and keeps the epoch file bounded across one-shot job churn.
        epoch_ids = cron_script.grant_epoch_ids() if removed_ids else set()
        for j in self._jobs:
            if j.id in removed_ids and (j.secret_env or j.secret_env_pin or j.id in epoch_ids):
                cron_script.bump_grant_epoch(j.id)

    def _remove_job_locked(self, job_id: str) -> bool:
        """Lock/reload/mutate/save core of :meth:`remove_job` (no timer work).

        Removing a job also releases every job that job OWNS, in the same locked
        write — see :meth:`_release_children_of_removed`.
        """
        with self._file_lock():
            self._sync_for_write()
            # Bump UNCONDITIONALLY: grant_epoch_ids() raises on corrupt epoch
            # state, so removing even a missing id surfaces that corruption
            # instead of answering a quiet False. The bump reads live rows, so
            # it must precede the filter inside _remove_job_rows.
            self._bump_grant_epochs_for({job_id})
            if any(j.id == job_id for j in self._jobs):
                restore = self._remove_job_rows({job_id})
                try:
                    self._save()
                except BaseException:
                    for child, previous_owner in restore:
                        child.session_key = previous_owner
                    # The removed rows are still filtered out of self._jobs and only
                    # a reload can put them back, so drop the fingerprint to force
                    # one. Without it the cache is missing rows that are still on
                    # disk while the fingerprint says it agrees with disk: the next
                    # _save() skips the reload and persists the removal this caller
                    # was told had failed, completing the parent's removal while its
                    # children keep the ownership just rolled back.
                    self._reset_fingerprint()
                    raise
                logger.info("Removed cron job %s", job_id)
                return True
        return False

    def _remove_job_rows(self, removed_ids: set[str]) -> list[tuple[CronJob, str]]:
        """Filter ``removed_ids`` out of ``self._jobs`` and cascade the release.

        The ONLY sanctioned spelling of a structural remove. Every site that
        drops a job's row from ``self._jobs`` must route through this helper so
        the removal and the release of the removed jobs' children land in the
        SAME save -- a bare list-comprehension filter compiles and passes tests
        while silently skipping the cascade, stranding every child the removed
        cron owns. ``test_cron_remove_rows_structural.py`` fails any filter
        site that bypasses this helper.

        Same contract as :meth:`_release_children_of_removed`: IN-LOCK ONLY,
        after ``_sync_for_write()``; the caller must ``_save()`` afterwards and
        on save failure restore the returned ``(job, previous_owner)`` pairs
        (and reset the fingerprint where its path requires it). Grant-epoch
        bumps read the live rows, so callers bump BEFORE calling this.
        """
        self._jobs = [j for j in self._jobs if j.id not in removed_ids]
        return self._release_children_of_removed(removed_ids)

    def _release_children_of_removed(self, removed_ids: set[str]) -> list[tuple[CronJob, str]]:
        """Clear ownership on jobs whose cron principal is among ``removed_ids``.

        IN-LOCK ONLY: callers must already hold :meth:`_file_lock`, must have
        reloaded through ``_sync_for_write()``, must have filtered the removed
        rows out of ``self._jobs`` first (so a removed job cannot release
        itself), and must ``_save()`` afterwards — the point is that a removal
        and the release it implies land in ONE atomic write, never as two
        transactions a crash could split.

        Removing a cron retires its principal: ``cron:<job id>`` is presented
        only by runs of that job, so once the row is gone no session can ever
        present the key again and any job it created is manageable by nobody —
        ``cron_list`` omits it, ``cron_update``/``cron_remove`` answer "job not
        found", and it keeps firing. That is the same dead-owner state the
        history-delete funnel exists to prevent, and the funnel cannot cover it:
        it deliberately SKIPS a live cron principal, so a transcript deleted
        before the cron is removed leaves nothing behind to notice later.

        Released, not deleted or re-parented — the same semantics the delete
        funnel uses. A child is the user's own scheduled work; only its owner is
        gone, so it drops to the documented ownerless state the CLI and the
        Schedule page manage rather than a third state or a guessed new parent.

        Matches through :func:`cron_owner_matches`, the one matcher every release
        path shares, so a child stamped under a longer spelling
        (``cron:<parent>:<run id>``, ``cron:<parent>:<agent>``) is caught too.
        Returns ``(job, previous_owner)`` pairs so the caller can roll the cache
        back if its ``_save()`` fails.
        """
        if not removed_ids:
            return []
        targets = {f"cron:{job_id}" for job_id in removed_ids if job_id}
        restore: list[tuple[CronJob, str]] = []
        for job in self._jobs:
            if not job.session_key:
                continue
            if not any(cron_owner_matches(job.session_key, target) for target in targets):
                continue
            restore.append((job, job.session_key))
            job.session_key = ""
        if restore:
            logger.info(
                "Released %d cron job(s) whose owning cron was removed: %s",
                len(restore),
                ", ".join(sorted(job.id for job, _ in restore)),
            )
        return restore

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
            self._sync_for_write()
            present = {j.id for j in self._jobs}
            targets = set()
            for jid in job_ids:
                if jid in present:
                    removed.append(jid)
                    targets.add(jid)
                else:
                    missing.append(jid)
            if targets:
                self._bump_grant_epochs_for(targets)
                restore = self._remove_job_rows(targets)
                try:
                    self._save()
                except BaseException:
                    for child, previous_owner in restore:
                        child.session_key = previous_owner
                    # The removed rows are still filtered out of self._jobs and only
                    # a reload can put them back, so drop the fingerprint to force
                    # one. Without it the cache is missing rows that are still on
                    # disk while the fingerprint says it agrees with disk: the next
                    # _save() skips the reload and persists the removal this caller
                    # was told had failed, completing the parent's removal while its
                    # children keep the ownership just rolled back.
                    self._reset_fingerprint()
                    raise
                logger.info("Removed %d cron job(s) in batch", len(targets))
        return removed, missing

    async def remove_jobs(
        self, job_ids: list[str], *, actor: str, source: str
    ) -> tuple[list[str], list[str]]:
        """Remove many jobs under ONE lock/reload/save, off the event loop.

        ``actor`` and ``source`` are required so the completed batch is
        audited here after persistence, outside the store lock.

        Returns ``(removed_ids, missing_ids)`` preserving input order. Looping
        :meth:`remove_job` per id would pay the file-lock + reload +
        full-serialize + atomic-write cost PER id on the event loop — with up to
        500 ids that starves every other gateway task (and on slow/network
        storage even one save can stall). The disk work
        runs in a worker thread; only ``_arm_timer`` (asyncio.create_task)
        runs back on the loop, and only when something was actually removed.
        """
        requested = list(job_ids)
        removed, missing = await asyncio.to_thread(self._remove_jobs_locked, requested)
        self._audit_requested_batch_removal(requested, removed, missing, actor=actor, source=source)
        if removed:
            self._arm_timer()
        return removed, missing

    def _audit_requested_batch_removal(
        self,
        requested: list[str],
        removed: list[str],
        missing: list[str],
        *,
        actor: str,
        source: str,
    ) -> None:
        """Audit one caller-requested batch after persistence and off-lock."""
        try:
            sel.sel().log_api_access(
                caller=actor,
                operation="cron.batch_delete",
                outcome="ok" if removed else "failed",
                source=source,
                resources=f"requested={requested} deleted={removed} failed={missing}",
            )
        except Exception:
            logger.warning("SEL audit for cron batch removal failed", exc_info=True)

    def remove_jobs_sync(
        self, job_ids: list[str], *, actor: str, source: str
    ) -> tuple[list[str], list[str]]:
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
        requested = list(job_ids)
        removed, missing = self._remove_jobs_locked(requested)
        self._audit_requested_batch_removal(requested, removed, missing, actor=actor, source=source)
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

        Selects through :meth:`_sync_for_write`, not ``_sync()``, because an EMPTY
        owned set is not an authoritative one. ``_load`` degrades an unreadable
        store to an empty job list, so the selection below would answer zero for a
        reason unrelated to ownership, skip the ``if removed`` branch, never reach
        ``_save()`` -- the only raiser on this path -- and return ``[]``. Uninstall
        reads that as "this app owned nothing" and deletes the app while its
        still-ENABLED jobs remain on disk to resume once the store parses again.
        ``_sync_for_write`` refuses first. A missing or honestly empty store leaves
        ``_load_failed`` clear, so a fresh install still tears down silently.
        """
        removed: list[str] = []
        with self._file_lock():
            self._sync_for_write()
            removed = [j.id for j in self._jobs if getattr(j, "created_by", "") == owner_prefix]
            if removed:
                targets = set(removed)
                self._bump_grant_epochs_for(targets)
                restore = self._remove_job_rows(targets)
                try:
                    self._save()
                except BaseException:
                    for child, previous_owner in restore:
                        child.session_key = previous_owner
                    # The removed rows are still filtered out of self._jobs and only
                    # a reload can put them back, so drop the fingerprint to force
                    # one. Without it the cache is missing rows that are still on
                    # disk while the fingerprint says it agrees with disk: the next
                    # _save() skips the reload and persists the removal this caller
                    # was told had failed, completing the parent's removal while its
                    # children keep the ownership just rolled back.
                    self._reset_fingerprint()
                    raise
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

    def adopt_job(self, job_id: str, session_key: str) -> bool:
        """Point ``job_id`` at ``session_key`` as its originating chat session.

        ``session_key`` names the chat session a job belongs to, and it is ONE
        field with ONE meaning: every consumer reads it as the delivery target
        (``session="origin"`` resolution and script-result injection both strip
        the ``dashboard:`` prefix off it to get a slot). So adopting a job also
        makes its output arrive in that session -- that is what being the
        originating session IS, not a side effect the caller has to be warned
        about separately. Pass ``""`` to release the job back to the operator
        surfaces (CLI and the dashboard Schedule page), which is the state every
        job created outside a chat legitimately starts in.

        Deliberately NOT a branch in :meth:`_update_job_locked`: that path is
        reachable from MCP ``cron_update`` and the dashboard ``PATCH``, and a
        ``session_key`` branch there would hand both surfaces the power to
        repoint where any job delivers. Ownership is asserted by the operator,
        so the CLI -- the one surface that is not a session -- is its only
        writer.

        Returns ``False`` when the id is absent. Raises :class:`CronStoreBusy`
        on lock contention. Synchronous only: the CLI is its sole caller and has
        no event loop, so an async sibling would be dead code.
        """
        ok = self._adopt_job_locked(job_id, session_key)
        if ok:
            self._arm_timer()
        return ok

    def _adopt_job_locked(self, job_id: str, session_key: str) -> bool:
        """Lock/reload/mutate/save core of :meth:`adopt_job` (no timer work)."""
        with self._file_lock():
            self._sync_for_write()
            for job in self._jobs:
                if job.id == job_id:
                    job.session_key = session_key
                    self._save()
                    return True
        return False

    def _release_jobs_owned_by_locked(
        self,
        owner_keys: Collection[str],
    ) -> list[str]:
        """Select AND release every job owned by ``owner_keys`` under ONE lock.

        Sync core of :meth:`release_jobs_owned_by` — lock/reload/select/mutate/
        save only, no timer work (so it is safe in an executor thread;
        ``_arm_timer`` needs the event loop). Same shape, and the same critical
        property, as :meth:`_remove_jobs_by_owner_locked`: BOTH decisions this
        makes — which owner keys name a retired principal, and which jobs still
        carry one of them — happen AFTER the in-lock ``_sync_for_write()``
        reload, against the authoritative on-disk state rather than a snapshot
        taken before the lock.

        That is the whole reason this exists instead of a caller-side
        ``list_jobs()`` loop over :meth:`adopt_job`. ``list_jobs`` is cache-only
        with up to one timer-poll interval of cross-process staleness, and both
        decisions are wrong when read from it:

        * OWNER staleness — between a pre-lock snapshot and a per-id write,
          another surface (the CLI's ``cron adopt``, a cron-injected slot
          re-stamping its key) can hand the job to a DIFFERENT owner, and an
          unconditional per-id release would clear that new owner, silently
          unbinding a job from a session that legitimately owns it.
        * PRINCIPAL staleness — a ``cron:<job id>`` owner is only live while that
          job exists, so its jobs must be kept owned; but a CLI ``cron remove``
          inside the staleness window leaves the cache still listing the job, and
          a cached liveness check would call the dead principal live and skip
          releasing its children. Nothing re-runs the delete funnel, so those
          jobs strand permanently.

        Conditioning both on the reloaded state makes the release a
        compare-and-clear against current truth: a re-adopted job keeps its new
        owner, a job whose cron principal is still scheduled keeps its owner, and
        either is simply absent from the returned ids.

        Ownership is matched through :func:`cron_owner_matches`, not ``==``,
        because one cron principal is stamped under several spellings
        (``cron:<job id>``, ``cron:<job id>:<run id>``,
        ``cron:<job id>:<agent>``) depending on which run created the job. An
        exact comparison misses a child stamped with a longer spelling than the
        caller holds, and a match MISS is not a release failure, so nothing warns.

        The active exact-key registry is snapshotted only AFTER this transaction
        acquires ``_file_lock`` and reloads the store. The short in-process
        ``_active_session_lock`` makes that worker-thread read coherent with loop
        registrations. A run that registered and persisted a child before this
        lock was acquired is therefore visible in both snapshots; a run that
        registers later cannot persist its child until this transaction releases
        the same store lock. This closes the snapshot-to-lock TOCTOU window while
        keeping every live stateless run out of principal-wide matching.

        All-or-nothing within the single ``_file_lock`` transaction: a contended
        store raises :class:`CronStoreBusy` before any mutation, and a save that
        fails after the mutation puts every touched owner back before re-raising,
        so the cache never reports a release that is not on disk. Returns the ids
        actually released.

        Selects through :meth:`_sync_for_write`, not ``_sync()``, for the reason
        spelled out on :meth:`_remove_jobs_by_owner_locked`: ``_load`` degrades an
        unreadable store to an empty job list, so an ownership scan over it would
        answer "this session owned nothing", skip the save, and report a clean
        release while the still-stamped jobs sit on disk. ``_sync_for_write``
        refuses first, so the caller learns the release did not happen. It also
        fails the liveness read closed, which matters in the opposite direction:
        an empty job list would call every cron principal dead and release jobs a
        live cron still owns.
        """
        owners = {k for k in owner_keys if k}
        if not owners:
            return []
        released: list[str] = []
        with self._file_lock():
            self._sync_for_write()
            active_keys = self.active_session_keys()
            # Principal liveness, decided on the state the reload just brought
            # in. A cron whose row is still here AND whose key is stable across
            # runs presents that key again on every future run, so releasing the
            # jobs it created would leave a LIVE owner unable to list, update or
            # remove its own work.
            #
            # Existence alone is NOT enough. A stateless job mints
            # ``cron:<job id>:<uuid4>`` fresh per fire, so the key its last run
            # stamped on a child is already unpresentable even though the parent
            # row is still scheduled -- retaining that ownership strands the child
            # exactly as a removed parent would. ``cron_session_key_is_stable``
            # is the predicate that lives beside the mint sites, so this cannot
            # drift from what those sites actually produce; inferring it from the
            # key's SHAPE is wrong, because a durable sequential-agent key and an
            # ephemeral per-run key are both three segments.
            live_by_id = {job.id: job for job in self._jobs}
            targets: set[str] = set()
            for key in owners:
                principal = cron_job_id_from_session_key(key)
                parent = live_by_id.get(principal) if principal else None
                if parent is not None and cron_session_key_is_stable(parent):
                    continue
                targets.add(key)
            if not targets:
                return []
            # Previous owners are recorded so the cache can be put BACK if the
            # save fails. ``_save`` serializes ``self._jobs``, so the mutation
            # has to precede persistence -- but an unwritable store (ENOSPC, a
            # read-only volume) would then leave memory saying "ownerless" while
            # disk still names the old owner: every ownership decision in this
            # process reads the released state, the delete reports success, and
            # the old owner resurrects on the next restart. Rolling back keeps
            # the two agreeing on the only outcome that actually happened.
            restore: list[tuple[CronJob, str]] = []
            for job in self._jobs:
                if not job.session_key:
                    continue
                if isinstance(job.session_key, str) and job.session_key in active_keys:
                    continue
                if not any(cron_owner_matches(job.session_key, target) for target in targets):
                    continue
                restore.append((job, job.session_key))
                job.session_key = ""
                released.append(job.id)
            if released:
                try:
                    self._save()
                except BaseException:
                    for job, previous_owner in restore:
                        job.session_key = previous_owner
                    raise
                logger.info(
                    "Released %d cron job(s) owned by deleted session(s) %s",
                    len(released),
                    ", ".join(sorted(targets)),
                )
        return released

    async def release_jobs_owned_by(self, owner_keys: Collection[str]) -> list[str]:
        """Clear ``session_key`` on every job owned by a RETIRED ``owner_keys`` key.

        The batch, owner-conditioned sibling of ``adopt_job(job_id, "")``: it
        releases jobs back to the operator surfaces (CLI and the dashboard
        Schedule page) the way a single ``--release`` does, but resolves both
        principal liveness and current ownership INSIDE the store lock, on the
        freshly reloaded state — so neither decision can be made from a stale
        cache (see :meth:`_release_jobs_owned_by_locked`). Callers pass every
        candidate owner key and do no filtering of their own.

        One lock/reload/select/save transaction, all-or-nothing; propagates
        :class:`CronStoreBusy` on a contended store so the caller can retry
        rather than silently dropping the release. The worker snapshots the
        complete ordered set of active and deferred per-run keys only after it
        holds the store lock, so a newer stateless run cannot persist a child in
        the snapshot-to-lock gap and have that live owner released. Only
        ``_arm_timer`` runs back on the loop, and only when something was actually
        released. Returns the released ids.
        """
        released = await asyncio.to_thread(
            self._release_jobs_owned_by_locked,
            owner_keys,
        )
        if released:
            self._arm_timer()
        return released

    def _owner_keys_locked(self) -> set[str]:
        """Every non-empty ``session_key`` on disk, read under ONE lock. STRICT.

        The READ half of :meth:`_release_jobs_owned_by_locked`, with the same
        freshness guarantee and the same failure contract, for the caller that
        must decide WHICH owner keys to release before it can call the release at
        all — the history delete's store-side owner sweep, which recovers the
        exact owner key of a job whose transcript never recorded it.

        Deliberately NOT ``list_jobs``/``list_jobs_async``. Both answer a job list
        that is EMPTY or STALE in exactly the two states this scan has to
        distinguish from "nobody owns anything", and neither raises:

        * ``list_jobs`` is cache-only, up to one timer-poll interval behind a
          cross-process write — and the cross-process job is precisely what the
          sweep exists to find.
        * ``list_jobs_async`` locks and syncs, but degrades on BOTH store
          failures: :meth:`_synced_snapshot` swallows :class:`CronStoreBusy` and
          returns the cache, and it syncs through ``_sync()``, whose ``_load``
          turns an unreadable store into an empty job list. A contended or
          corrupt store therefore reads as "no owners" — indistinguishable from
          an honestly ownerless store, and the wrong answer for a caller about to
          destroy the last record of an ownership it could not see.

        So this raises instead of degrading: :class:`CronStoreBusy` out of
        :meth:`_file_lock` on sustained contention, :class:`CronStoreUnreadable`
        out of :meth:`_sync_for_write` on a store ``_load`` could not parse. The
        caller is expected to fail CLOSED on either — an unknown job set is not an
        empty one. ``_sync_for_write`` is the same reload the release uses and is
        chosen for the same reason spelled out there: ``_sync()`` alone would let
        an unreadable store answer "this session owned nothing".

        Read-only: no mutation, no ``_save``, so nothing to roll back. Returns
        owner keys, not jobs, because the scan's only question is which owner
        spellings exist — the release re-resolves ownership and liveness per job
        inside its own lock, and handing out ``CronJob`` objects would invite a
        caller-side decision on state that is stale the moment the lock drops.
        Includes jobs that are disabled or auto-paused: a paused job's owner is
        still stamped on disk and still strands when the session goes.
        """
        with self._file_lock():
            self._sync_for_write()
            return {owner for job in self._jobs if (owner := job.session_key)}

    def _chat_folder_ids_locked(self) -> set[str]:
        """Every non-empty ``chat_folder_id`` on disk, read under ONE lock. STRICT.

        For the folder cleanup, which must not delete a folder a saved job files
        its tab into: the job stores the folder's id, so a folder recreated by
        name would not bring the filing back. Same failure contract as
        :meth:`_owner_keys_locked` and for the same reason -- an unreadable or
        contended store is an unknown job set, not an empty one, so this raises
        :class:`CronStoreBusy` or :class:`CronStoreUnreadable` instead of
        answering "no folders". Includes disabled and auto-paused jobs.
        """
        with self._file_lock():
            self._sync_for_write()
            return {fid for job in self._jobs if (fid := job.chat_folder_id)}

    async def chat_folder_ids_async(self) -> set[str]:
        """Event-loop-safe :meth:`_chat_folder_ids_locked`."""
        return await asyncio.to_thread(self._chat_folder_ids_locked)

    async def owner_keys_async(self) -> set[str]:
        """Event-loop-safe :meth:`_owner_keys_locked` — the lock+read runs off the loop.

        Propagates :class:`CronStoreBusy` (retryable) and
        :class:`CronStoreUnreadable` (not) rather than degrading to a partial
        answer; see :meth:`_owner_keys_locked` for why a read this one is used for
        must fail loudly. No timer work: nothing is mutated, so there is no
        schedule change to arm.
        """
        return await asyncio.to_thread(self._owner_keys_locked)

    def enable_job(
        self, job_id: str, enabled: bool = True, *, expected_owner: str | None = None
    ) -> bool:
        """Enable or disable a job by ID.

        Raises :class:`CronStoreBusy` on lock contention; see
        :meth:`enable_job_async` for the event-loop-safe variant.
        """
        ok = (
            self._enable_job_locked(job_id, enabled, expected_owner=expected_owner)
            if expected_owner is not None
            else self._enable_job_locked(job_id, enabled)
        )
        if ok:
            self._arm_timer()
        return ok

    async def enable_job_async(
        self, job_id: str, enabled: bool = True, *, expected_owner: str | None = None
    ) -> bool:
        """Event-loop-safe :meth:`enable_job`: the lock+save runs off the loop.

        Raises :class:`CronStoreBusy` (retryable) on sustained contention.
        """
        ok = (
            await asyncio.to_thread(
                self._enable_job_locked, job_id, enabled, expected_owner=expected_owner
            )
            if expected_owner is not None
            else await asyncio.to_thread(self._enable_job_locked, job_id, enabled)
        )
        if ok:
            self._arm_timer()
        return ok

    def _enable_job_locked(
        self, job_id: str, enabled: bool = True, *, expected_owner: str | None = None
    ) -> bool:
        """Lock/reload/mutate/save core; app ownership is checked under the lock.

        An SDK-side cached lookup cannot authorize a mutation after another
        process has changed the store. Missing and foreign jobs both refuse
        when an expected owner is supplied; ordinary host calls retain False
        for a missing job.
        """
        with self._file_lock():
            self._sync_for_write()
            for job in self._jobs:
                if job.id == job_id:
                    if expected_owner is not None and job.created_by != expected_owner:
                        raise PermissionError("Cron job ownership violation")
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
            if expected_owner is not None:
                raise PermissionError("Cron job ownership violation")
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
            self._sync_for_write()
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
            self._sync_for_write()
            for job in self._jobs:
                if job.id == job_id and job.acked_items:
                    job.acked_items.pop()
                    self._save()
                    return True
        return False

    # ── Active session tracking ──

    def register_active_session_key(self, job_id: str, session_key: str) -> None:
        """Register one live exact key for ``job_id`` under the run that holds the job's claim.

        Several distinct keys may coexist: a finished stateless run can stay alive
        for pending subagents while a newer run starts, and a sequential job holds
        one key per agent inside one run. Each key is attributed to the run that
        registered it -- the ``_RunClaim`` stored for the job at that moment, the
        run the gateway's cron callback is executing under -- so a reap or cancel
        of that run ends every key it registered and no other run's
        (:meth:`_run_session_keys`). Pop-and-reinsert refreshes recency for
        reaper/cancel targeting and re-attributes a stable key to the run
        registering it now. Re-registering the same exact key is idempotent
        because all claimants share one SessionManager runtime.
        """
        # Loop-owned, read on the loop: both product registrants run inside the
        # cron callback, under the claim ``_run_job_isolated`` holds for the run.
        run = self._claims.get(job_id)
        with self._active_session_lock:
            keys = self._active_session_keys.setdefault(job_id, {})
            keys.pop(session_key, None)
            keys[session_key] = run

    def clear_active_session_key(self, job_id: str, session_key: str) -> None:
        """Retire the exact key whose SessionManager reset completed."""
        with self._active_session_lock:
            keys = self._active_session_keys.get(job_id)
            if not keys:
                return
            keys.pop(session_key, None)
            if not keys:
                self._active_session_keys.pop(job_id, None)

    def _run_session_keys(self, job_id: str, run: _RunClaim) -> list[str]:
        """Every live exact key ``run`` registered, newest first: the keys a reap or cancel of that run ends.

        A key another run registered -- a finished run's session kept alive for
        its pending sub-agents -- is not this run's and is left to its own end.
        A key registered under no claim (no product path registers outside the
        run callback; legacy callers and tests) belongs to whichever run of the
        job is ended next, as the newest key did before keys were attributed.
        Empty when the run registered none: the caller then ends the job's
        stable key.
        """
        with self._active_session_lock:
            keys = self._active_session_keys.get(job_id) or {}
            return [key for key, owner in reversed(keys.items()) if owner is run or owner is None]

    def active_session_keys(self) -> frozenset[str]:
        """Snapshot every exact live key across current and deferred cron runs."""
        with self._active_session_lock:
            return frozenset(
                session_key for keys in self._active_session_keys.values() for session_key in keys
            )

    def get_history(self) -> CronHistoryStore:
        """Public accessor for the history store."""
        return self._history

    def is_running(self, job_id: str) -> bool:
        """Return whether a job is currently executing."""
        return job_id in self._claims

    # ── Run claims ──
    #
    # A run's whole per-run state is one _RunClaim, stored, fenced, taken and
    # released by self._runs (RunClaims), so a field added to _RunClaim
    # inherits every fence; stop() alone drops them all, at shutdown.

    @property
    def _claims(self) -> dict[str, _RunClaim]:
        """Job id -> the claim of the run that occupies it (:attr:`RunClaims.claims`).

        Membership is "the job is running" for the due-scan, the next-wake
        computation, :meth:`run_job` and the manual-run route.
        """
        return self._runs.claims

    @_claims.setter
    def _claims(self, claims: dict[str, _RunClaim]) -> None:
        self._runs.claims = claims

    def _claim_run(self, job_id: str, trigger: str) -> _RunClaim:
        """Claim ``job_id`` for a new run and return the claim (:meth:`RunClaims.claim`).

        The dispatchers' one door: :meth:`run_job` and the due-scan call it in
        the loop step that found the job idle.
        """
        return self._runs.claim(job_id, trigger)

    def attach_run_task(self, job_id: str, task: asyncio.Task[Any]) -> None:
        """Track ``task`` as the run that :meth:`run_job` just claimed ``job_id`` for.

        The manual-run route wraps the coroutine ``run_job`` returns in a task
        on the same line and hands it in here, still await-free, so ``cancel()``
        can reach a run parked in its store refresh and ``stop()`` can await it.
        See :meth:`RunClaims.attach_task`.
        """
        self._runs.attach_task(job_id, task)

    def discard_finished_run(self, job_id: str) -> bool:
        """Drop the claim of a run whose task has already finished (:meth:`RunClaims.discard_finished`).

        Asked first by the three consumers that gate on "is a run in flight?" --
        the manual-run route before its 409, the reaper sweep before its
        deadline math, and :meth:`cancel` before its guard. Returns True when a
        stale claim was dropped.
        """
        return self._runs.discard_finished(job_id)

    def running_since(self, job_id: str) -> float | None:
        """Return the epoch start time of a running job, or None (:meth:`RunClaims.running_since`)."""
        return self._runs.running_since(job_id)

    def set_refresh_callback(self, cb: Any) -> None:
        """Set the dashboard refresh callback."""
        self._push_refresh = cb

    def run_job(self, job_id: str) -> Coroutine[Any, Any, bool]:
        """Manually trigger a job via _run_job_isolated (records history).

        A plain ``def`` that returns the coroutine, on purpose: the claim on the
        job -- its ``_RunClaim`` with ``trigger="manual"`` -- is taken HERE,
        synchronously, while the call expression is evaluated. It therefore
        exists before the caller's ``asyncio.create_task`` has scheduled
        anything and before the coroutine's first ``await``. The manual-run
        route answers 409 "already running" off the claim and hands the task it
        creates to ``attach_run_task``, and ``cancel()`` reads the same claim: a
        claim taken only inside the coroutine -- after the offloaded store
        refresh, up to a full lock spin later -- leaves a window in which Cancel
        answers "not running" about a run that Run calls "already running", and
        the run then executes anyway. Loop-only and await-free, so the
        check-and-claim stays atomic against the due-scan and a concurrent
        manual trigger; ``_run_claimed_manual`` releases the claim if the store
        does not hold the job. Every caller awaits or schedules the returned
        coroutine at once (the route wraps it in a task on the same line); one
        that dropped it would leave the job claimed.
        """
        if job_id in self._claims:
            return _manual_run_refused()
        claim = self._claim_run(job_id, "manual")
        return self._run_claimed_manual(job_id, claim)

    async def _run_claimed_manual(self, job_id: str, claim: _RunClaim) -> bool:
        """Body of :meth:`run_job`, entered with the claim already taken."""
        # Refresh the store off the loop, then resolve + spawn on the loop.
        #
        # The locked _sync() + snapshot runs in a worker thread (_synced_snapshot
        # via asyncio.to_thread) so a manual trigger never pays the whole-file
        # read_bytes() + blake2b hash of crons.json on the event loop.
        # _claims is loop-owned; the claim was taken on the loop before this
        # coroutine started, so a cancel() that lands while the refresh is in
        # flight finds the run and cancels THIS task.
        #
        # One residual, benign race remains against the batch-remove worker: it
        # may delete the job on its own thread in the instant between our
        # snapshot and our spawn, so a manual run can execute a just-removed job
        # ONCE (non-destructive — it is not persisted, and the next scan won't
        # see it). The batch-remove worker holds the SAME flock, so a lock-held
        # claim could not observe a delete mid-way regardless. Degrades to the
        # in-memory snapshot under lock contention.
        try:
            snapshot = await asyncio.to_thread(self._synced_snapshot, True)
        except BaseException:
            # No run will consume the claim: release it unless cancel() already
            # took it (the fence fails) or a newer claim has replaced it. A
            # cancel flag cancel() set goes with the claim.
            self._runs.release(job_id, claim)
            raise
        if not self._runs.holds(job_id, claim):
            # cancel() took the claim while the refresh was in flight and is
            # still tearing down: it takes the claim first and cancels the
            # tracked task only after its process-kill / session-reset awaits,
            # so this coroutine can resume inside that gap. Dispatching here
            # would start the very run cancel() is about to report cancelled.
            return False
        job = next((j for j in snapshot if j.id == job_id), None)
        if not job:
            self._runs.release(job_id, claim)
            return False
        task = asyncio.create_task(self._run_job_isolated(job, claim))
        claim.task = task
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise  # outer coroutine was cancelled, propagate
        finally:
            # Backstop for a run whose finally was cut short, idempotent with
            # it and fenced the same way: the wrapper resumes one loop
            # iteration after the run ends, and a claim that is not this
            # wrapper's by then belongs to a replacement run.
            if task.done():
                self._runs.release(job.id, claim)
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

        Unlike :meth:`list_jobs`, which serves the in-memory snapshot that the
        loop-side timer refreshes, this performs ONLY a read-only file parse. It
        never mutates loop-owned state (``self._jobs``, ``self._last_mtime``) and
        never touches the asyncio timer, so it is safe to invoke from a worker
        thread via ``asyncio.to_thread``.

        This exists specifically for the dashboard status count refresh, which
        needs an enabled-job count off the event loop that is current across
        processes: a job added by the CLI or an MCP tool reaches the snapshot
        only on the next timer tick, while this read sees it at once.

        Enabled semantics come from the shared ``_record_is_enabled`` predicate
        (the single owner used by ``_load`` too): a job is enabled when it is
        neither user-paused nor auto-paused (with the legacy ``!enabled``
        fallback for stores written before those fields existed). A slightly stale count is
        acceptable here — the caller caches it and the atomic tmp→rename write
        in ``_save`` guarantees a concurrent read sees a whole file, never a
        partial one.

        The read, parse and shape guards are :func:`_read_job_records`, shared
        with the other two direct readers. Routing through it is what keeps the
        WS status pusher alive on a corrupt store: catching only
        ``(OSError, json.JSONDecodeError)`` here would let invalid UTF-8
        (``UnicodeDecodeError``, from a bare locale-dependent ``read_text()``)
        and deeply nested JSON (``RecursionError``, a ``RuntimeError``) escape
        and kill the pusher. A count of 0 is the correct degrade: an
        unreadable store has no jobs anyone can schedule.

        The reduction itself lives in :func:`enabled_count_from_disk`, whose
        ``loadable`` half this method deliberately discards — the status pusher
        wants a number it can always render, not a fault to handle.
        """
        return enabled_count_from_disk(self._path)[0]

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
        """Compute seconds until the next job should fire (:func:`~kiro_crew.cron_service.schedule.next_wake_secs`)."""
        return next_wake_secs(self._jobs, self._claims, time.time())

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
        # (by which point the completed job has released its claim),
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
        then runs the mutation-free, claim-aware due-scan against it.

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
        drained: list[str] = []
        try:
            with self._file_lock():
                self._sync()
                drained = self._drain_pending_removals_locked()
        except CronStoreBusy:
            logger.debug("Cron timer tick: store busy, using in-memory snapshot")
        except OSError as exc:
            logger.warning("Cron timer tick: store write failed, using in-memory snapshot: %s", exc)
        # Post-lock on purpose: the emit must never extend the store-lock hold
        # (see audit_one_shot_removal). Still on this worker thread, so the
        # queue append cannot block the event loop either.
        for jid in drained:
            self.audit_one_shot_removal(jid, "cron_deferred_drain")
        return list(self._jobs)

    async def _on_timer(self) -> None:
        """Fire due jobs as independent tasks (non-blocking).

        The locked store refresh + deferred-removal drain + snapshot is
        offloaded to a worker thread (:meth:`_tick_scan_locked`) so a large or
        slow ``crons.json`` can never freeze the gateway loop with the
        ``_sync()`` ``read_bytes()`` + blake2b hash on every tick (the
        ``no-blocking-call-on-event-loop`` rule). The mutation-free due-scan —
        which reads loop-owned ``self._claims`` — then runs on the loop
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
                if j.enabled and j.id not in self._claims and self._is_due(j, now)
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
                and j.id not in self._claims
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
                    logger.debug("Cron: still deferring %d scheduled job(s)", len(deferred))
            elif self._admission_deferring:
                self._admission_deferring = False
                logger.info("Cron: memory posture recovered — resuming scheduled firings")

            if not due:
                return

            # Fire each job independently — one hung job never blocks others.
            # The claim taken here is handed to the task rather than read back
            # by it: see _run_job_isolated.
            for j in due:
                claim = self._claim_run(j.id, "scheduled")
                claim.task = asyncio.create_task(self._run_job_isolated(j, claim))
        finally:
            self._on_timer_running = False

    async def _run_job_isolated(self, job: CronJob, claim: _RunClaim) -> None:
        """Execute a single job and merge results back to disk.

        ``claim`` is the claim the dispatcher took for this run -- the object it
        stored in ``_claims`` and tracked this task on (``_on_timer``,
        ``_run_claimed_manual``) -- handed in rather than read back here. This
        first step runs at least one loop iteration after ``create_task``, and
        a ``cancel()`` in that gap takes the claim, marks the cancellation for
        it, and awaits the process kill BEFORE it cancels the task, so the task
        starts inside ``cancel()``: a claim read back from the store there would
        be one this run does not own, the finally's flag read would miss,
        and the run would be filed as a failure beside the cancelled row
        ``cancel()`` writes.
        """
        if not self._runs.holds(job.id, claim):
            # Taken before this run's first step: cancel() took the claim,
            # wrote the run's terminal row and is about to cancel this task.
            # Run nothing -- a stamp set now would sit on a claim that is no
            # longer this run's to release, and that finally would double the
            # row already written. The flags cancel() and the reaper set live
            # on this claim and go with it.
            return
        # This run's generation, drawn while it verifiably holds the claim;
        # the finally stamps it on the record it merges (see CronJob).
        claim.generation = self._runs.next_generation(job)
        started_at = claim.claimed_at
        trigger = claim.trigger
        # Provisional; refined once the jitter sleep completes. Only read on
        # the history path, which a cancelled-during-jitter run never reaches.
        exec_started_at = started_at
        being_cancelled = False
        marker_write: "asyncio.Future[None] | None" = None
        # Everything the finally below releases is claimed INSIDE the try. The
        # caller (_on_timer, run_job) has already stored the claim and tracked
        # this task on it, and the timer path never awaits the task, so that
        # finally is the only release the claim ever gets. Bookkeeping ahead of
        # the try -- the generation, the fire counter -- is outside that
        # protection: an exception there ends the task with the claim still
        # stored and nothing on this path left to release it; the job then
        # reads as running until the reaper sweep, the manual-run route or
        # cancel() meets the finished task (discard_finished_run), and every
        # scheduled fire and every manual run in between is skipped or refused
        # with 409.
        try:
            # Stamped here rather than derived from claimed_at: the two clocks
            # share no epoch, so the reaper's deadline is only meaningful
            # against a stamp taken on its own clock.
            claim.started_monotonic = time.monotonic()
            # One increment per execution, before the jitter sleep so a run
            # cancelled during jitter still counts as fired. ``kind`` is the
            # dispatch shape -- ``script`` and ``command`` bypass the model
            # entirely, so this is the split between jobs that cost tokens and
            # jobs that cost none.
            if job.script:
                kind = "script"
            elif job.command:
                kind = "command"
            else:
                kind = "agent"
            emit_counter(CRON_FIRES, {"kind": kind, "trigger": trigger})
            # Apply jitter to spread execution unless strict_schedule is set or manual
            jitter = self._compute_jitter(job) if trigger != "manual" else 0
            claim.jitter = jitter
            # ``last_result`` is a cross-run context-carry field for AGENT jobs
            # (see build_cron_session_context): result-less runs leave the
            # previous value in place so the next run's prompt keeps its dedup
            # context. Command and script jobs have theirs cleared once in the
            # finally below, because the prompt built for them is never
            # dispatched. The history recorder in the finally block must NOT
            # attribute that carried-over value to THIS run, so clear the
            # freshness marker here; executor callbacks set it via
            # CronJob.set_run_result() when the run actually produces a result.
            # (String identity/equality can't stand in for the marker: CPython
            # interns equal literals and caches single-char strings, so a run
            # re-producing the previous text looks identical to one that
            # produced nothing.)
            job.result_produced = False
            # The jitter sleep MUST live inside this try: hourly/daily jobs
            # sleep up to 59 min here, and a user cancel() during that window
            # raises CancelledError at the sleep — if that happened BEFORE the
            # try, the finally below would never run, leaking the rest of this
            # run's bookkeeping (the in-flight marker, the claim release).
            if jitter > 0:
                logger.debug("Cron: applying %.0fs jitter to job '%s'", jitter, job.name)
                await _sleep_out_jitter(jitter)
            exec_started_at = time.time()
            # The record a hard exit leaves behind. Every other trace of this
            # run (last_run_ts, the history row, status) is written in the
            # finally below, which an os._exit from the loop-stall watchdog
            # never reaches -- so without this file the store would show the
            # job as never fired, it would be due again on the next boot, and
            # nothing could say which job the dying gateway was running. Off
            # the loop like every other write on this path; best-effort. The
            # write is kept as a task so the finally below can wait for it: a
            # cancellation that lands mid-write must not let clear_marker run
            # before the worker publishes, or the marker it leaves behind would
            # read as an abandoned run on the next boot.
            # The in-flight marker is one file per RUN, named by the claim's
            # token, so the clear in the finally can only remove this run's own
            # marker -- never the one a replacement run wrote while this run's
            # cancellation was still unwinding (see cron_inflight.clear_marker).
            marker_write = asyncio.ensure_future(
                asyncio.to_thread(
                    cron_inflight.write_marker,
                    self._dir,
                    job.id,
                    job.name,
                    exec_started_at,
                    run=claim.marker_run,
                )
            )
            await asyncio.shield(marker_write)
            # Notify dashboard that the job has started executing so the live
            # is_running badge appears without a manual reload.
            try:
                if self._push_refresh:
                    self._push_refresh("crons")
            except Exception:
                logger.debug("push_refresh failed on job start", exc_info=True)
            await self._execute_with_timeout(job, claim)
        except asyncio.CancelledError:
            # stop() cancels this task WITHOUT setting claim.cancelled, so the
            # finally must know not to clear the last completed run's result.
            being_cancelled = True
            raise
        finally:
            finished_at = time.time()
            # The run ended by a path that runs finally, so it is no longer in
            # flight whatever its outcome. A marker that survives this is what a
            # hard exit looks like, so clear it first and unconditionally --
            # after the write that may still be publishing it, or a
            # cancellation mid-write would unlink nothing and leave the marker.
            try:
                if marker_write is not None and not marker_write.done():
                    await asyncio.shield(marker_write)
            except (asyncio.CancelledError, Exception):
                pass  # the write is best-effort; the clear below still runs
            try:
                # This run's file only (the token is in its name): a replacement
                # run accepted while this cancellation was unwinding -- a whole
                # session teardown for an agent job -- has its own marker, and
                # a clear by job id would have taken it, leaving a hard exit
                # during that run with no evidence for the breaker.
                await asyncio.to_thread(
                    cron_inflight.clear_marker, self._dir, job.id, claim.marker_run
                )
            except Exception:
                logger.debug("in-flight marker not cleared for %s", job.id, exc_info=True)
            # Read only THIS run's flags, off its own claim. cancel() and the
            # reaper flag the claim they took; a marker keyed by job id alone
            # would let this finalizer, stalled in the clear above while a
            # replacement run was accepted and cancelled, eat that run's
            # marker -- its finalizer then finds none and appends a failure
            # row after the cancelled row cancel() already wrote.
            reaped = claim.reaped
            cancelled = claim.cancelled
            # This run's terminal record, taken BEFORE the release below. The
            # job object is shared by every run of the job, and the release
            # lets a replacement start while the merge and history append are
            # still pending: that run's `_execute` resets last_status and the
            # result-produced flag on the same object, so a record read after
            # the release would file this run as a failure with no summary and
            # persist the replacement's blank status as this run's. The every-
            # job stamp move and the carried-result clear are this run's own
            # terminal writes, so they land here too, inside the claim.
            terminal: CronJob | None = None
            status = ""
            run_result: str | None = None
            if not reaped and not cancelled:
                terminal, status, run_result = close_run(
                    job,
                    started_at=started_at,
                    generation=claim.generation,
                    being_cancelled=being_cancelled,
                )
            # Release the claim only while it is still THIS run's (identity,
            # see RunClaims.holds). cancel() and the reaper take the run they
            # found before anything else can claim the job, and the marker
            # clear above is a full executor round trip: a manual Run accepted
            # in that gap holds a claim of its own that is not this run's to
            # remove. Popping it anyway drops that run at its own claim re-check
            # after the route answered "started", or, past the re-check, leaves
            # it running with Cancel answering 409 and a further Run accepted
            # beside it. A run that is still the claim holder (a normal
            # completion, or stop()'s cancel, which takes nothing) releases
            # everything here -- the one pop covers every field.
            self._runs.release(job.id, claim)
            # Notify dashboard that the job has finished (clears the badge).
            try:
                if self._push_refresh:
                    self._push_refresh("crons")
            except Exception:
                logger.debug("push_refresh failed on job end", exc_info=True)
            if terminal is not None:
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
                    await asyncio.to_thread(self._merge_job_result, terminal)
                except CronStoreBusy as exc:
                    # The lock was never won, so nothing was merged and the
                    # one-shot consume did not run. A delete_after_run at-job
                    # is still enabled (the consume is what stops it), so
                    # without the deferral it is due again on the next tick.
                    # Same owed rule as the consume in _merge_job_result.
                    logger.warning("Result merge for job '%s' skipped: %s", job.name, exc)
                    if terminal.delete_after_run and not (
                        terminal.fire_time_denied or terminal.run_never_started
                    ):
                        self.defer_removal(terminal.id)
                except Exception:
                    logger.exception("Failed to merge result for job '%s'", job.name)
                # Record history
                try:
                    record = CronRunRecord(
                        job_id=job.id,
                        trigger=trigger,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_ms=int((finished_at - exec_started_at) * 1000),
                        status=status,
                        # Uncut on purpose: CronHistoryStore.append is the one
                        # truncation site, applying the configured cap through
                        # truncate_summary, which keeps URLs and the outcome
                        # line. A slice here would cut ahead of both.
                        summary=run_result or terminal.last_error or "",
                        trace=run_result or "",
                        error=terminal.last_error or "",
                    )
                    await self._history.append(record)
                    if self._push_refresh:
                        self._push_refresh("cron_history")
                except Exception:
                    logger.exception("Failed to record history for job '%s'", job.name)
            # Re-arm now rather than waiting for whatever wake was already
            # armed: a job that ran for most of its interval was invisible to
            # every _next_wake_secs() computed while self._claims held it
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

    #: Random jitter seconds for a scheduled run of a job
    #: (:func:`~kiro_crew.cron_service.schedule.compute_jitter`).
    _compute_jitter = staticmethod(compute_jitter)
    #: Whether a job is due at an epoch (:func:`~kiro_crew.cron_service.schedule.is_due`).
    _is_due = staticmethod(is_due)

    async def _execute_with_timeout(self, job: CronJob, claim: _RunClaim | None = None) -> None:
        """Execute a job with a timeout guard.

        ``claim`` is the run's own claim, handed down so ``_execute`` can ask
        whether THIS run was cancelled (the markers are keyed by run, and
        ``cancel()`` has taken the stored claim by the time that question is
        asked); a direct call without one matches no marker.
        """
        timeout = effective_wake_budget(job)
        # The cron pool's QUEUE WAIT happens inside this deadline, so the wake
        # budget has to cover it as well as the execution.  Excluding queue wait
        # from the per-call `timeout=` kwarg (see run_in_cron_pool) is not enough
        # on its own: without the term below, a job still sitting in the pool
        # queue is killed here and reported as an execution overrun, which is the
        # exact misdiagnosis this whole change exists to remove.  Worse, a thread
        # cannot be interrupted -- so if a worker claimed the call as this
        # deadline fired, the subprocess runs on while the overlap guards clear
        # and the next wake duplicates its side effects.  That is the hazard
        # _SUBPROC_CLEANUP_ALLOWANCE_SECS was written for, and the queue wait is
        # a second term it never accounted for, and the fire-time gate's own bound
        # is a third -- it is awaited before the dispatch and inside this same
        # deadline, so _gate_budget_allowance covers it.  The CLAIM-time vet is a
        # fourth: the same vet again, inside the worker, ahead of the subprocess
        # it authorises -- so _vet_allowance covers it, and without that term a
        # widened inner backstop in the gateway is simply pre-empted here.
        # All three allowances are
        # shared with the reaper so the two deadlines cannot drift and pre-empt
        # one another.  Only command/script jobs go through the pool, so a
        # message job's budget is left exactly as set.
        deadline = (
            timeout + _pool_queue_allowance(job) + _gate_budget_allowance(job) + _vet_allowance(job)
        )
        # Fresh run: no failure counted yet. The timeout handler below reads
        # this to avoid double-counting a run that already recorded its
        # failure and then overran the deadline during cleanup.
        job.failure_recorded = False
        try:
            await asyncio.wait_for(self._execute(job, claim), timeout=deadline)
        except asyncio.TimeoutError:
            # NB: Timeout bypasses _cron_callback's except block entirely —
            # which also means it bypasses all Slack notification logic. Adding
            # a timeout Slack alert is a separate feature and is intentionally
            # out of scope here.
            # Clear failure dedup state so a subsequent real error isn't
            # suppressed as a dup of the pre-timeout failure, and count the
            # timeout toward the auto-pause threshold for a run that actually
            # DISPATCHED: a job that times out on every run must eventually
            # auto-pause instead of running forever with zero user signal.
            job.last_status = "error"
            job.last_error = f"Timed out after {deadline}s"
            job.last_run_ts = time.time()
            job.last_failure_hash = ""
            job.last_failure_at = 0.0
            # Skip the count when this run already recorded its failure (a
            # delivery-path exception followed by cleanup overrunning the
            # deadline): one failed run is one failure, whichever handler
            # observes it last.
            #
            # Skip it too when the payload NEVER STARTED. A stall that pushes
            # wall clock past the wake deadline while the fire-time gate is
            # still awaited cancels this coroutine AT that await, so no handler
            # inside the gate runs and the marker set before it survives -- and
            # the timeout lands here on a run that dispatched nothing. Counting
            # it would auto-pause at _AUTO_PAUSE_THRESHOLD, and a paused job
            # never fires again, so repeated event-loop saturation durably
            # disables a job that has not run a line. This is the same
            # discriminator the starvation, gate-deny and vet-overrun paths
            # already use: a state that PREVENTED the run is not a defect OF
            # the run. A genuine execution overrun still counts, because
            # _execute resets the marker to False before invoking the callback.
            if not job.failure_recorded and not job.run_never_started:
                job.record_failure()
            logger.error("Cron job '%s' timed out after %ds", job.name, deadline)

    async def _execute(self, job: CronJob, claim: _RunClaim | None = None) -> None:
        """Run the job callback and update runtime fields (last_run_ts, last_status).

        ``claim`` is this run's claim (see ``_execute_with_timeout``).
        """
        logger.info("Cron: executing '%s' (%s)", job.name, job.id)
        # Reset status for this run so a prior run's "error" can't leak into an
        # "ok" decision below. Same for the fire-time denial marker.
        job.last_status = None
        job.fire_time_denied = False
        job.run_never_started = False
        # Transient retries the callback took this run. The gateway callback only
        # INCREMENTS `_transient_attempts` (a runtime attribute on the live job);
        # this method is the one owner of reading it, clearing it and persisting
        # it -- see the stamp after `last_run_ts` below. The read-and-clear sits
        # in a `finally` so a cancelled or timed-out run (CancelledError skips
        # everything after the await) cannot leak a half-spent budget into the
        # next run's retry allowance.
        retries = 0
        try:
            if self._on_job:
                try:
                    await self._on_job(job)
                finally:
                    retries = int(getattr(job, "_transient_attempts", 0) or 0)
                    job._transient_attempts = 0  # type: ignore[attr-defined]
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
                # (resetting 0 to 0 is idempotent). The claim.cancelled
                # check closes a cancel race: cancel() kills the sandboxed
                # subprocess BEFORE task.cancel(), and the gateway's
                # cancelled branch returns None without setting last_status,
                # so a callback returning in that window would otherwise
                # reach this branch — and cancel() documents that it leaves
                # consecutive_failures untouched. Read off THIS run's claim:
                # cancel() has taken the stored claim by now, and a flag set
                # on another run of the job is not this run's.
                if claim is None or not claim.cancelled:
                    job.record_success()
        except Exception as exc:
            job.last_status = "error"
            job.last_error = str(exc)
            logger.error("Cron job '%s' failed: %s", job.name, exc)

        job.last_run_ts = time.time()
        # Retry telemetry is stamped HERE, with the `last_run_ts` it describes,
        # so the two can never disagree for a run that completed. Stamping it
        # anywhere inside the callback would bind it to the PREVIOUS run's
        # `last_run_ts` (this line has not run yet while the callback is
        # executing), and the Schedule page -- which shows the count only when
        # the two stamps match -- would never show it. A cancelled run never
        # reaches this line, so its own `last_run_ts` write leaves the pair
        # mismatched and the page shows no count rather than a stale one.
        job.last_retry_count = retries
        job.last_retry_run_ts = job.last_run_ts

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

        Fenced by run generation (``run_generation``, see :class:`CronJob`):
        the record's fields are applied only while its generation is not
        below the one the store holds, compared under the lock after the
        ``_sync()``. The finalizer calls this behind the release of its claim,
        so a replacement run can be accepted, complete and merge while this
        call is still waiting for the lock; an older record landing after it
        would revert status, error, result and the failure counter to a run
        that is not the last one. ONLY the field copies are fenced: the
        one-shot consume below is owed by this run's completion whatever
        landed since -- a replacement's terminal merge writes status fields and
        retires nothing -- and a stale record that skipped it would leave a
        consumed ``delete_after_run`` at-job enabled on disk, due again on
        every tick. The consume already tolerates the row being gone.
        """
        with self._file_lock():
            self._sync()
            by_id = {j.id: j for j in self._jobs}
            if job.id in by_id and job.run_generation < by_id[job.id].run_generation:
                logger.debug(
                    "Cron: not applying the record of an older run of '%s' (generation %d);"
                    " the store already holds generation %d",
                    job.name,
                    job.run_generation,
                    by_id[job.id].run_generation,
                )
            elif job.id in by_id:
                apply_run_record(by_id[job.id], job)
            # A fire-time-DENIED run is a policy refusal, not a completed run:
            # deleting the one-shot here would make the documented
            # resume-on-policy-loosening semantic impossible for at-jobs.
            # A run that never STARTED is the same story for a different reason --
            # every pool worker was busy for the whole queue budget -- so consuming
            # the one-shot would destroy scheduled work that never got a chance to
            # run. Only the delete is suppressed: unlike a policy denial this needs
            # no operator action, so the job stays enabled and simply retries.
            # TWO signals, because the queue and the audit ask different
            # questions and a corrupt store answers them differently.
            #   delete_owed      -- is a consume OWED by this path at all?
            #   removed_one_shot -- did this path actually remove a PRESENT job?
            # Deriving both from presence conflated them: `_load` degrades an
            # unreadable store to an empty job list WITHOUT raising, so presence
            # is exactly what a corrupt store destroys, and the deferred queue
            # below then never fired for a delete that was still owed on disk.
            delete_owed = job.delete_after_run and not (
                job.fire_time_denied or job.run_never_started
            )
            removed_one_shot = False
            restore: list[tuple[CronJob, str]] = []
            consumed_row = False
            if delete_owed:
                # Presence check keeps the audit honest: a Done-script one-shot
                # already removed by the gateway path leaves nothing to delete
                # here, and that path owns the audit record.
                removed_one_shot = job.id in by_id
                # BACKGROUND writer: a failed epoch bump must not crash the
                # run path, but the delete is skipped — the deferred drain
                # retries once the epoch state heals, never deleting a
                # still-live grant record. The job has ALREADY RUN, so the
                # held delete needs the same two-layer guard as
                # `defer_removal`: the run path deliberately leaves `enabled`
                # untouched for a delete_after_run at-job (the delete is what
                # stops it), so without a persisted pause the save below
                # writes it back live and every tick re-fires it until the
                # bump succeeds; and without the queue entry no later pass
                # ever retries the delete. `user_paused` is the persisted
                # spelling `enabled` is re-derived from on reload.
                try:
                    self._bump_grant_epochs_for({job.id})
                except (OSError, ValueError):
                    logger.warning(
                        "One-shot delete held for %s: grant-epoch bump failed",
                        job.id,
                        exc_info=True,
                    )
                    removed_one_shot = False
                    if job.id in by_id:
                        by_id[job.id].enabled = False
                        by_id[job.id].user_paused = True
                    self._pending_removals.add(job.id)
                else:
                    # Consuming the one-shot retires its principal cron:<job id>,
                    # so a child that job created is released in the SAME save --
                    # the fifth removal core alongside the four locked cores.
                    # _remove_job_rows filters the row out before releasing so
                    # the job cannot release itself; rolled back below if the
                    # save fails.
                    restore = self._remove_job_rows({job.id})
                    consumed_row = True
            # BACKGROUND writer: a job has already run, so an unreadable store
            # must not surface as a job-runner crash. The run result is lost,
            # which is strictly better than clobbering the store.
            try:
                self._save()
            except BaseException as exc:
                # EVERY save failure rolls back the child release, not just
                # CronStoreUnreadable: the release lives in memory only until the
                # save lands it, so a bare OSError (ENOSPC/EROFS/EIO out of
                # atomic_write) that left the cleared owners in place while disk
                # still named the old owner would let the next successful save
                # persist a release nothing asked for, from a consume that never
                # reached disk.
                if consumed_row:
                    for child, previous_owner in restore:
                        child.session_key = previous_owner
                    # The consumed row is still filtered out of self._jobs and
                    # only a reload can put it back, so drop the fingerprint to
                    # force one -- otherwise the next _save skips the reload and
                    # persists the removal this path was told had failed,
                    # completing the parent's removal while its children keep the
                    # ownership just rolled back.
                    self._reset_fingerprint()
                # Queue the consume for EVERY save failure, not just the
                # store-unreadable one: the fingerprint reset above forces a
                # reload, and the save never landed, so the disk copy is still
                # enabled. Without a queue entry the reloaded one-shot comes back
                # enabled and runs a second time on the next tick. Keyed on
                # delete_owed, NOT presence: an absent id proves nothing about
                # whether the delete is owed, and the drain intersects the queue
                # with what is present and drops the rest.
                if delete_owed:
                    self._pending_removals.add(job.id)
                if isinstance(exc, CronStoreUnreadable):
                    # Return WITHOUT auditing: the emit below records only a SAVED
                    # removal, and nothing was saved. Auditing here would file a
                    # removal record for a delete that never reached disk. The
                    # drain retries the queued consume once the store is readable.
                    logger.warning("Cron job result not persisted: %s", exc)
                    return
                # Anything else is a real write fault, not the tolerated
                # store-unreadable case: surface it rather than reporting a quiet
                # no-op after the disk refused the write.
                raise
        if removed_one_shot:
            # The delete_after_run consume is an automated removal with no
            # handler-level caller, so the emit lives with the removal.
            # AFTER the lock: only a saved removal is recorded, and
            # the sel call never extends the store-lock hold.
            self.audit_one_shot_removal(job.id, "cron_run_complete")

    def _merge_terminal_state_locked(
        self,
        job_id: str,
        *,
        last_status: str,
        last_error: str,
        last_run_ts: float,
        run_generation: int,
        result_produced: bool = False,
        count_failure: bool = False,
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

        Fenced by run generation like :meth:`_merge_job_result`:
        ``run_generation`` is the number the caller drew for the run this
        record is for, and the record is skipped (debug log, nothing saved)
        when the store already holds a higher one. Both callers release the
        run's claim before they offload this call, so a Run
        accepted in that gap can complete and merge first; applying the older
        record would persist that run's success as the cancellation or timeout
        that came before it. Returning early here skips nothing owed: unlike
        ``_merge_job_result`` this helper writes only the three status fields
        (plus clearing a command/script job's carried result, and the failure
        count when ``count_failure``), and the save after them has nothing to
        record once they are skipped.
        """
        with self._file_lock():
            self._sync()
            by_id = {j.id: j for j in self._jobs}
            target = by_id.get(job_id)
            if target is None:
                return
            if run_generation < target.run_generation:
                logger.debug(
                    "Cron: not applying the terminal record of an older run of '%s'"
                    " (generation %d); the store already holds generation %d",
                    target.name,
                    run_generation,
                    target.run_generation,
                )
                return
            target.run_generation = run_generation
            target.last_status = last_status
            target.last_error = last_error
            target.last_run_ts = last_run_ts
            # Counted on the disk copy under the lock, so the failure count and
            # any auto-pause it triggers persist with the terminal record.
            counted = (target.enabled, target.auto_paused, target.consecutive_failures)
            if count_failure:
                target.record_failure()
            # A command/script run that produced nothing must not show the
            # previous run's result beside this error. The caller passes the
            # flag because a reload in _sync() drops the runtime-only marker.
            # Agent jobs keep theirs on purpose as dedup context.
            if (target.command or target.script) and not result_produced:
                target.last_result = ""
            # BACKGROUND writer: reached from the reaper timeout and user
            # cancel. An unreadable store must not abort the reaper loop.
            try:
                self._save()
            except Exception as exc:
                # Keep scheduling as the disk says (like the loop-stall breaker):
                # a pause the store did not take must not stop the job in memory.
                target.enabled, target.auto_paused, target.consecutive_failures = counted
                if not isinstance(exc, CronStoreUnreadable):
                    raise
                logger.warning("Cron terminal state not persisted: %s", exc)

    # ── Loop-stall breaker ──

    def _apply_loop_stall_breaker(self) -> str | None:
        """Pause the job the previous gateway died running. WORKER-THREAD ONLY.

        The loop-stall watchdog hard-exits the gateway; the run in flight left an
        in-flight marker (:mod:`kiro_crew.cron_inflight`) and the dump names the
        PID. When the newest dump's wedged stack is a cron turn AND exactly one
        abandoned marker carries that PID, that job is the one whose input
        stalled the loop -- and left enabled it is due again as soon as the
        timer arms, which is the hourly crash loop a user reported. It is parked
        ``auto_paused`` with a ``last_error`` that says why and how to resume,
        audited like the failure-count auto-pause. Ambiguous evidence (several
        runs in flight, no marker, a non-cron surface) pauses nothing; the doctor
        prints the same attribution so the operator can decide.

        What the markers said is recorded (``cron_inflight.record_attribution``)
        BEFORE they are swept, because the doctor and the restart notification
        read the same evidence afterwards: an ambiguous verdict the breaker
        declined to act on must still reach the operator who can act on it.

        A dump is CLAIMED, and its markers swept, only once the breaker has
        reached a verdict that survives a restart. A pause the store refused to
        persist leaves the job enabled and still due, so claiming it would let the
        next boot -- the one whose store is readable again -- skip the job and
        re-run the crash the breaker exists to stop. That boot is the only one
        that retries, so it keeps both the claim and the evidence intact.
        Returns the paused job id, or None.

        Every failure here is swallowed. This is a safety net that runs BEFORE
        the timer arms, so a fault in it must cost at most the net: letting one
        propagate would fail ``start()`` and leave the operator with no scheduler
        at all, which is strictly worse than the crash loop it is trying to stop.
        """
        try:
            return self._loop_stall_breaker_verdict()
        except Exception:
            logger.warning("loop-stall breaker skipped after an unexpected failure", exc_info=True)
            return None

    def _loop_stall_breaker_verdict(self) -> str | None:
        """The breaker's body. See :meth:`_apply_loop_stall_breaker`, which owns
        the promise that nothing in here can fail the cron service's start."""
        try:
            attribution = stall_attribution.attribute_latest_stall(self._dir, self._dumps_dir)
        except Exception:
            logger.debug("loop-stall attribution failed; breaker skipped", exc_info=True)
            return None
        if attribution is None:
            cron_inflight.sweep_abandoned_markers(self._dir)
            return None
        if cron_inflight.read_claim(self._dir) == attribution.dump.name:
            # Settled on an earlier boot: its evidence has been recorded, and
            # re-pausing a job the operator resumed is what the claim prevents.
            cron_inflight.sweep_abandoned_markers(self._dir)
            return None
        recorded = True
        if attribution.candidates or attribution.unrelated_abandoned:
            recorded = cron_inflight.record_attribution(
                self._dir,
                attribution.dump.name,
                attribution.candidates,
                attribution.unrelated_abandoned,
            )
        paused: str | None = None
        if attribution.is_cron and attribution.job is not None:
            settled, paused = self._pause_for_loop_stall(attribution)
            if not settled:
                return None
        # Sweep only behind a written claim AND a readable record. With the claim
        # missing, the next boot would re-derive this verdict from the retained
        # markers (and the record), and the pause it reaches is idempotent: the
        # job's own ``last_error`` names the dump, which settles it even after
        # the operator has resumed the job. With the record missing, the
        # markers are the only copy of what the doctor has to show.
        if recorded and cron_inflight.write_claim(self._dir, attribution.dump.name):
            cron_inflight.sweep_abandoned_markers(self._dir)
        return paused

    def _pause_for_loop_stall(
        self, attribution: "stall_attribution.StallAttribution"
    ) -> tuple[bool, str | None]:
        """``(settled, paused job id)`` for the job *attribution* names.

        *settled* is False ONLY when the verdict could not be recorded -- an
        unreadable or unwritable store -- which is the one case a later boot must
        retry. A job that is already paused, or gone from the store, is settled
        with nothing paused: there is no action left for any boot to take.
        """
        marker = attribution.job
        if marker is None:  # pragma: no cover - the caller checks
            return (True, None)
        with self._file_lock():
            self._sync()
            if self._load_failed:
                logger.warning("loop-stall auto-pause deferred: cron store not readable")
                return (False, None)
            job = next((j for j in self._jobs if j.id == marker.job_id), None)
            if job is None or job.auto_paused or job.user_paused:
                return (True, None)
            if job.last_error and attribution.dump.name in job.last_error:
                # Paused for THIS dump on an earlier boot and since resumed by
                # the operator (resume keeps ``last_error``): the verdict was
                # recorded in the store itself, so a lost claim file cannot
                # turn a resume into a second pause.
                return (True, None)
            before = (
                job.enabled,
                job.auto_paused,
                job.last_status,
                job.last_run_ts,
                job.last_error,
            )
            job.enabled = False
            job.auto_paused = True
            job.last_status = "error"
            job.last_run_ts = marker.started_at
            job.last_error = (
                "Paused: the gateway was terminated by the loop-stall watchdog while this "
                f"job was running (crash dump {attribution.dump.name}). Inspect the command "
                "the run was about to execute, then resume with "
                f"`kirocrew cron resume {job.id}`."
            )
            try:
                self._save()
            except CronStoreUnreadable as exc:
                # The in-memory job must match the store it could not reach:
                # still enabled, still due, so this session schedules it as the
                # disk says and the next boot retries the pause.
                (
                    job.enabled,
                    job.auto_paused,
                    job.last_status,
                    job.last_run_ts,
                    job.last_error,
                ) = before
                logger.warning("loop-stall auto-pause not persisted: %s", exc)
                return (False, None)
            job._audit_pause_change("auto_paused_loop_stall")
        logger.error(
            "Cron job '%s' (%s) auto-paused: the previous gateway was hard-exited by the "
            "loop-stall watchdog while running it (%s). Resume with `kirocrew cron resume %s` "
            "once the cause is fixed.",
            job.name,
            job.id,
            attribution.dump.name,
            job.id,
        )
        return (True, job.id)

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
        with cron_store_lock(self._dir, timeout=timeout, poll=poll):
            yield

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
        self._last_digest = store_digest(raw)

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
            the ``_synced_snapshot`` offload; the claim stays on
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
            # Clear the refusal latch: a store that is GONE is not an unreadable
            # one, and _load's docstring already promises that a load which
            # resolves -- "including a missing file" -- leaves the store writable.
            # Returning bare left the latch set, so the one remediation this
            # refusal PRINTS (move the unreadable file aside) did nothing on a
            # live gateway: every later write kept failing until a restart. A
            # fresh CLI/MCP process was unaffected because it reconstructs.
            #
            # Deliberately NOT a call to _load(), even though its missing-file
            # branch clears this same flag: that branch also replaces _jobs with
            # an empty list, which discards in-memory jobs not yet persisted --
            # the reaper mutates a job and only then saves, so wiping first loses
            # the update and the save never happens (test_cron_reaper's
            # test_reaper_persists_state catches exactly that). Clearing the flag
            # is the whole of the defect; emptying the list is a separate
            # behaviour change and not one this needs.
            #
            # The fingerprint is left alone on purpose: the failed load that set
            # this latch already reset it, so a file that reappears mismatches the
            # cleared digest below and reloads normally.
            #
            # Clearing the latch re-opens _save(), so the snapshot it would write
            # has to be trustworthy. When the latch was SET, _jobs came from a
            # load that could not read the store -- it may predate an external
            # removal, and writing it back resurrects whatever that writer
            # deleted. _load's own missing-file branch empties the list for the
            # same reason; this branch bypasses _load, so it must do it too.
            # Conditioned on the latch, NOT unconditional: with the latch clear
            # this is the ordinary no-store path, where the reaper's in-memory
            # mutation is still waiting to be saved and wiping it would lose the
            # update (test_cron_reaper's test_reaper_persists_state).
            if self._load_failed:
                self._jobs = []
            self._load_failed = False
            return
        try:
            raw = self._path.read_bytes()
        except OSError:
            # LATCH, the same as _load's two failure paths do. This is the THIRD
            # way a read of the store can fail and the only one that never reaches
            # _load, so returning bare left _save()'s guard -- the one thing
            # standing between stale memory and the file -- open: a store that went
            # unreadable AFTER a good load (EIO, EACCES, a botched restore) was
            # overwritten from memory, discarding whatever it had come to hold.
            #
            # Still deliberately NOT un-latched: the store is unreadable here, so
            # keeping an existing refusal is correct and clearing it would suppress
            # a live fault rather than report it.
            #
            # _jobs is deliberately NOT emptied, unlike _load's paths: the
            # missing-file rationale above applies unchanged -- wiping the list
            # discards a mutation the reaper has made but not yet saved.
            #
            # The fingerprint has to be cleared WITH the latch, exactly as both
            # _load failure paths pair them. Left alone, a fault over an UNCHANGED
            # store leaves the tracked digest still matching the file, so the next
            # _sync sees no change, skips the _load that is the only thing that
            # clears this latch, and a store that is now perfectly healthy refuses
            # every write until the process restarts.
            self._reset_fingerprint()
            self._load_failed = True
            return
        if store_digest(raw) != self._last_digest:
            logger.info("Cron file changed externally, reloading")
            self._load(_preread=raw)

    def _load(self, _preread: bytes | None = None) -> None:
        """Deserialize jobs from crons.json and record the fingerprint.

        ``_preread`` lets :meth:`_sync` hand in the bytes it already read for
        the change check so the file is not read twice for one reload.

        Clears :attr:`_load_failed` on entry and re-raises it only on the paths
        that could not read the store, so a load that DOES resolve — including
        a missing file and an honestly empty one — leaves the store writable,
        and a store repaired between two loads heals itself.
        """
        self._load_failed = False
        if not self._path.exists():
            self._jobs = []
            self._reset_fingerprint()
            return
        try:
            st = self._path.stat()
            raw = _preread if _preread is not None else self._path.read_bytes()
            jobs = decode_jobs(raw)
            if jobs is None:
                # A document that parses but is not an object holding a jobs
                # LIST (top-level [], a scalar, {"jobs": null}) cannot yield
                # any job — same salvage story as unparseable JSON (there is
                # nothing to keep), and without this guard the per-entry build
                # would raise an uncaught AttributeError/TypeError into _sync
                # and gateway startup.
                logger.warning(
                    "Failed to load cron store: document is not an object with a jobs list"
                )
                self._jobs = []
                self._reset_fingerprint()
                self._load_failed = True
                return
            # One malformed or legacy record is warned about and skipped by
            # decode_jobs; every well-formed job survives, and the whole-store
            # reset below is reserved for a file that yields nothing parseable
            # at all, where there is nothing to salvage.
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
            self._last_digest = store_digest(raw)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            # Same class set as _read_job_records' json.loads guard, kept
            # spelled identically so the two cannot drift. A decode-error-only
            # handler here let two classes escape into _sync and gateway
            # startup: UnicodeDecodeError (invalid UTF-8 — a SIBLING subclass
            # of ValueError, not an ancestor of json.JSONDecodeError) and
            # RecursionError (deeply nested JSON — a RuntimeError, outside the
            # ValueError tree entirely). OSError covers the stat()/read_bytes()
            # above, which _sync already guards but the constructor's
            # _load() — and so gateway startup — does not. A genuinely absent
            # file never reaches here: the exists() check returns early, so a
            # fresh install still loads silently rather than warning.
            logger.warning("Failed to load cron store: %s", exc)
            self._jobs = []
            self._reset_fingerprint()
            self._load_failed = True

        # Restore timers for active jobs loaded from disk
        if self._running:
            restored = sum(1 for j in self._jobs if j.enabled)
            if restored:
                self._arm_timer()
                logger.info("Restored %d cron timer(s) from disk", restored)

    def _unreadable_error(self) -> CronStoreUnreadable:
        """The one wording for a refusal caused by an unreadable store.

        Built in a single place because THREE guards raise it — :meth:`_sync_for_write`
        before a mutation, :meth:`_save` at the disk boundary, and
        :meth:`raise_if_store_unreadable` for a caller that must refuse without
        attempting a write at all — and the message names the path plus the
        remediation that the CLI, dashboard, MCP and Slack boundaries surface
        verbatim. Two copies of that sentence would drift.
        """
        return CronStoreUnreadable(
            f"refusing to write cron store: the last load could not read {self._path}, "
            "so the in-memory job list is empty for that reason rather than because the "
            "store is empty. Move the unreadable file aside to start fresh."
        )

    def raise_if_store_unreadable(self) -> None:
        """Refuse if the last load could not read the store. NO I/O of its own.

        Exists because every other guard is on a WRITE, and a caller that decides
        whether to write by first comparing the loaded jobs against a desired state
        never gets that far: an unreadable store loads as an EMPTY list — :meth:`_load`
        warns, empties, latches ``_load_failed`` and RETURNS rather than raising, and
        :meth:`_synced_snapshot` only translates :class:`CronStoreBusy` — so there is
        no job to diverge, no mutation is attempted, and such a caller reports a
        successful no-op over a corrupt file. That is the quiet-versus-broken
        conflation, and it is invisible to :meth:`_sync_for_write`.

        Reads the latch only, so it is safe on the event loop and adds no second read
        after a :meth:`list_jobs_async` — which has just refreshed the latch under the
        store lock. Call it AFTER that read, or the answer is one poll stale.
        """
        if self._load_failed:
            raise self._unreadable_error()

    def _sync_for_write(self) -> None:
        """:meth:`_sync` for a MUTATING transaction — refuse BEFORE the mutation.

        Every user-facing mutator edits ``self._jobs`` and only then reaches
        ``_save()``, so refusing at the disk boundary alone left the caller told
        "rejected" while the mutation stayed in the in-memory list — a resumed job
        the timer can still fire, an ack already consumed, a removal already gone
        from the cache. All TEN user-facing writers in ``_save``'s audit table now
        route through here; the three BACKGROUND ones deliberately do not (below).
        That is reachable, not theoretical:
        :meth:`_tick_scan_locked` documents an in-memory-snapshot fallback for a
        contended lock — it skips ``_sync()`` and returns ``list(self._jobs)`` — so
        the due-scan could hand a refused job to the runner.

        Refusing up front rather than undoing afterwards is what makes that
        unrepresentable. ``_save()`` cannot roll back a mutation it never saw: its
        write-path audit lists roughly a dozen writers, each touching different
        fields, so a generic rollback there has nothing to key on and a partial one
        would be a fresh defect. The check sits after ``_sync()`` because
        ``_sync()`` is what sets ``_load_failed``.

        ``_save()`` keeps its own guard rather than delegating to this one: it is
        the backstop for any writer that does not come through here, and the three
        BACKGROUND writers depend on it firing at the disk boundary — each wraps
        only its ``_save()`` call, so an earlier raise would abort the reaper and
        the tick instead of degrading them.
        """
        self._sync()
        if self._load_failed:
            raise self._unreadable_error()

    def _save(self) -> None:
        """Atomic write (tmp → rename) and update mtime tracking.

        RAISES :exc:`CronStoreUnreadable` when the last :meth:`_load` could not
        read the store, instead of writing. ``_load`` degrades an unreadable
        store to an empty job list, which is indistinguishable HERE from an
        honestly empty one — and this method serialises ``self._jobs``
        wholesale, so one mutation after a failed load would persist that empty
        list over a store still holding records. Measured on the base handler
        alone (a ``json.JSONDecodeError`` store plus one ``add_job``), so this
        is not a hazard the widened ``_load`` guard introduced.

        It RAISES rather than returning quietly because a silent refusal is the
        same silence-shaped failure this change exists to break: the mutator
        would return success to the dashboard/CLI/MCP caller for a write that
        never happened. Every writer below reaches disk through this one
        method, so the single check covers all of them; the three BACKGROUND
        writers catch the error and degrade so a corrupt store cannot take down
        the reaper, the tick scan or the job runner. A missing store and an
        honestly empty one are NOT failures (``_load`` clears the flag for
        both), so a fresh install still writes.

        WRITE-PATH AUDIT — every ``_save()`` call site and every structural
        ``self._jobs`` mutation, each classified locked/unlocked and
        on-loop/off-loop. INVARIANT: every writer holds :meth:`_file_lock` and
        is reached from the gateway event loop ONLY via ``asyncio.to_thread``
        (or runs in a genuinely loop-less CLI/MCP process). No bare on-loop
        ``_save()`` remains. Keep this table in sync when adding a writer.
        Counted by EXECUTABLE call site: three other lines in this file mention
        ``self._save()`` in a comment or docstring and are not calls.

        ==============================  ==========  ======================================
        Writer (method)                 Locked?     Loop entry
        ==============================  ==========  ======================================
        _persist_add_locked             _file_lock  add_job_async → to_thread; sync CLI/MCP
        _persist_add_if_absent_locked   _file_lock  add_job_if_absent_async → to_thread; sync
        _update_job_locked              _file_lock  update_job_async → to_thread; sync CLI/MCP
        _remove_job_locked              _file_lock  remove_job_async → to_thread; sync CLI/MCP
        _remove_jobs_locked             _file_lock  remove_jobs → to_thread
        _remove_jobs_by_owner_locked    _file_lock  app/owner teardown → to_thread; sync
        _adopt_job_locked               _file_lock  adopt path → to_thread; sync
        _enable_job_locked              _file_lock  enable_job_async → to_thread; sync CLI/MCP
        _ack_job_locked                 _file_lock  ack_job_async → to_thread; sync
        _unack_job_locked               _file_lock  unack_job_async → to_thread; sync
        _merge_job_result               _file_lock  _run_job_isolated → to_thread; BACKGROUND
        _merge_terminal_state_locked    _file_lock  _force_reap / cancel → to_thread; BACKGROUND
        _drain_pending_removals_locked    (caller)  _tick_scan_locked holds _file_lock; BACKGROUND
        _load (self._jobs = …)            (caller)  _sync() under _file_lock; else construction/start
        ==============================  ==========  ======================================

        In-memory-only job field writes that DON'T call ``_save()`` and are
        persisted later under lock: ``defer_removal`` (sets ``enabled=False``
        so the next due-scan skips it; the durable delete happens in the locked
        ``_drain_pending_removals_locked``), and the pre-persist snapshot writes
        in ``_force_reap``/``cancel`` (authoritative persist is the offloaded
        ``_merge_terminal_state_locked``).
        """
        if self._load_failed:
            raise self._unreadable_error()
        self._dir.mkdir(parents=True, exist_ok=True)
        document = encode_store(self._jobs)
        # Atomic write: unique tmp → rename
        # Deferred import to avoid circular dependency (pre-existing)
        from kiro_crew.atomic_write import atomic_write

        atomic_write(self._path, document)
        # Refresh the (mtime_ns, size) fingerprint so _sync recognizes this as
        # our own write and does not reload it back over the in-memory state.
        self._record_fingerprint()
