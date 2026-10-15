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

:class:`CronService` is defined here and composes two sets of owners. The modules
of :mod:`kiro_crew.cron_service` hold what the service is built from (schedule
evaluation, the job record, run identity, run claims, run deadlines, the store format
and lock, field validation, the serviceless store readers, cron folders). The modules
of :mod:`kiro_crew.cron_service.service` hold its behaviour: each member there is a
module-level function the class binds by name, so the service's state stays on the
instance. The class body keeps that state (``__init__``), the ``_claims`` view, the
loop-safety guard, and the store-lock cores whose log lines carry code-scanning alerts
keyed by this file's path.

This module is also the subsystem's import and patch surface: every function,
class and constant it defined before those owners moved out still resolves here
as the same object, and so does every public name it imported from the rest of
the package. An owner reads a name tests patch here, and a name this module
defines, through this module on each call (``from kiro_crew import cron as seams``),
so a patch here reaches it; every other name an owner reads is its own global,
which a patch here does not reach.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import random  # noqa: F401 -- patch seam: tests patch kiro_crew.cron.random.uniform
import threading
import time
from datetime import datetime  # noqa: F401 -- patch seam the owners read through here
from pathlib import Path
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
)

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

from kiro_crew import platform_compat  # noqa: F401 -- re-exported
from kiro_crew import (  # noqa: F401 -- re-exported
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
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord  # noqa: F401 -- re-exported
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
    interruption_note,
    pause_changed_since_start,
    run_parks_one_shot,
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
from kiro_crew.cron_service.service import job_ownership as _job_ownership
from kiro_crew.cron_service.service import job_removal as _job_removal
from kiro_crew.cron_service.service import job_writes as _job_writes
from kiro_crew.cron_service.service import run_execution as _run_execution
from kiro_crew.cron_service.service import run_kill as _run_kill
from kiro_crew.cron_service.service import run_merge as _run_merge
from kiro_crew.cron_service.service import run_registry as _run_registry
from kiro_crew.cron_service.service import run_teardown as _run_teardown
from kiro_crew.cron_service.service import service_lifecycle as _service_lifecycle
from kiro_crew.cron_service.service import stall_breaker as _stall_breaker
from kiro_crew.cron_service.service import store_sync as _store_sync
from kiro_crew.cron_service.service import wake_timer as _wake_timer
from kiro_crew.cron_service.store import (  # noqa: F401 -- re-exported
    _CRONS_FILE,
    _FILE_LOCK_POLL_SECS,
    _FILE_LOCK_TIMEOUT_SECS,
    _STORE_VERSION,
    CronPendingMismatch,
    CronStoreBusy,
    CronStoreUnreadable,
    StoreDocument,
    UnknownFields,
    _is_loadable_record,
    _is_representable_number,
    _job_from_record,
    _read_job_records,
    _record_is_enabled,
    _record_user_paused,
    cron_store_lock,
    decode_jobs,
    decode_store,
    encode_store,
    quarantine_copies,
    quarantine_unreadable_store,
    store_digest,
)
from kiro_crew.executors import cron_gate_budget  # noqa: F401 -- re-exported
from kiro_crew.executors import subprocess_executor  # noqa: F401 -- re-exported
from kiro_crew.metrics.events import CRON_FIRES, emit_counter  # noqa: F401 -- re-exported
from kiro_crew.process_identity import (  # noqa: F401 -- re-exported
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
    refresh_live_process_tree,
    release_teardown_lease,
    spawn_in_flight,
    teardown_barriers,
    teardown_capture,
    with_kill_failure,
)
from kiro_crew.resource_status import admission_check  # noqa: F401 -- re-exported
from kiro_crew.runtime_ownership import authorize_runtime_kill  # noqa: F401 -- re-exported
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
# dashboard/handlers/usage_store.py is the reference implementation.
_DEFAULT_DIR: Path | None = None


def _default_dir() -> Path:
    """Cron data directory, resolved against the live data home."""
    return _DEFAULT_DIR if _DEFAULT_DIR is not None else data_home()


# The doctor's three store readers live here. `kirocrew doctor`'s facade test
# pins `unhealthy_jobs_from_disk` and `job_pause_state_from_disk` as defined in
# `kiro_crew.cron`; this third one sits beside them. It lists the copies
# gateway startup moved only after `crons.json` failed JSON decoding or decoded
# to something other than an object holding a `jobs` list. Each copy is an unchanged rename under
# `cron-history/quarantine`, which is behind the file-tool fence and the
# sandbox mask. The dashboard notice and doctor's Cron Jobs section name the
# copy and the restore sequence: fix it, stop Kiro Crew, move it back, then
# start Kiro Crew again.
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


class CronPauseNotSaved(RuntimeError):
    """Raised by :meth:`CronService.cancel` when a cancelled one-shot's pause did not reach the store.

    The run is cancelled and its history entry written; only the park failed to
    land, and nothing retries the save later. The message describes the job as the
    cancel last saw it, after the merge (see :func:`_cancelled_park_report`).
    """


# Reasons ``_merge_terminal_state_locked`` gives for saving no park.
_PARK_JOB_REMOVED = "the job is no longer in the cron store"
_PARK_NEWER_RUN = "the cron store already holds a newer run of the job"
_PARK_NOT_ONE_SHOT = "the job is no longer a one-shot in the cron store"
_PARK_NOT_SAVED = "The run was cancelled, but its pause could not be saved"
_ON_SCHEDULE_MESSAGE = (
    f"{_PARK_NOT_SAVED}: a newer run of the job was recorded meanwhile, so the job is back"
    " on its schedule now. Pause or delete it if it must not run again."
)
_RECURRING_MESSAGE = (
    f"{_PARK_NOT_SAVED}: the job was changed to a recurring schedule meanwhile and runs on"
    " that schedule now. Pause or delete it if it must not run again."
)


def _cancelled_park_report(
    job: CronJob | None, reason: str | None, saved: str | None = None
) -> tuple[str, str | None]:
    """The history note and the message for a cancelled one-shot's park.

    ``job`` is the job as ``cancel`` sees it after the merge, and ``reason`` the
    merge's answer (None: a save holding the park returned). Every end of the
    merge is worded here, from that job and not from the reason, so the text
    is true whatever wrote the store meanwhile. A message of None means nothing
    is left to pause, and ``cancel`` returns True: a failed park answers that
    only on ``saved``, what the store as saved shows when it shows the job
    cannot run again (``CronService._saved_job_state``).
    """
    if reason is None:
        return "", None
    if saved == "absent":
        return "; the job was removed from the cron store meanwhile", None
    note = f"; the pause could not be saved ({reason})"
    if saved == "paused":
        return f"{note}, and the job is paused", None
    if job is not None and job.enabled:
        if job.schedule.kind == "at":
            return f"{note}, so the job is back on its schedule now", _ON_SCHEDULE_MESSAGE
        return f"{note}, so the job runs on its recurring schedule now", _RECURRING_MESSAGE
    # Held off the schedule in this process only, or gone from memory, and not
    # shown paused or absent by the saved store: a reload or a restart can bring
    # it back enabled, so it may run again.
    subject = "this one-shot" if job is None or job.schedule.kind == "at" else "the job"
    return f"{note}, so {subject} may run again", (
        f"{_PARK_NOT_SAVED}, so {subject} may run again once the cron store is reloaded or"
        " the gateway restarts. Delete it if it must not run again."
    )


# ── Service ──


class CronService:
    """Background service for managing and executing scheduled jobs."""

    # What a NEWER build added to the store this service last loaded: the version
    # it wrote, its unknown top-level keys, and each loaded job's unknown keys.
    # _save writes them back (encode_store), so a rollback does not delete them.
    # Class-level defaults are this build's own empty document; every _load
    # replaces them whole on the instance, and nothing edits them in place.
    _store_version: int = _STORE_VERSION
    _store_extra: Mapping[str, Any] = MappingProxyType({})
    _unknown_fields: Mapping[str, UnknownFields] = MappingProxyType({})

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
        # Completed runs whose result merge has not been saved yet, the newest
        # run per job id (see _hold_run_record). The guard covers the dict and
        # every apply of a held record: the loop holds, worker threads re-apply
        # and release, and a hold and a re-apply never interleave on one job.
        self._held_run_records: dict[str, CronJob] = {}
        self._held_run_records_guard = threading.Lock()
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
        # The gateway's failure alert for a run its deadline ended (set_timeout_alert),
        # and the alerts in flight: the loop keeps only weak references to tasks.
        self._timeout_alert: Callable[[CronJob, str], Coroutine[Any, Any, None]] | None = None
        self._timeout_alerts: set[asyncio.Task[None]] = set()
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

    # Starting and stopping the service: ``kiro_crew.cron_service.service.service_lifecycle``.
    create = classmethod(_service_lifecycle.create)
    _quarantine_unreadable_locked = _service_lifecycle._quarantine_unreadable_locked
    start = _service_lifecycle.start
    stop = _service_lifecycle.stop
    # The reaper and cancel: ``kiro_crew.cron_service.service.run_teardown``.
    start_reaper = _run_teardown.start_reaper
    _reaper_loop = _run_teardown._reaper_loop
    _force_reap = _run_teardown._force_reap
    cancel = _run_teardown.cancel
    # Ending a run's processes and sessions: ``kiro_crew.cron_service.service.run_kill``.
    _session_process_handles = _run_kill._session_process_handles
    _sessions_under = _run_kill._sessions_under
    _handles_of = staticmethod(_run_kill._handles_of)
    _end_run_processes = _run_kill._end_run_processes
    _fence_run_keys = _run_kill._fence_run_keys
    _end_run_sessions = _run_kill._end_run_sessions
    _spawn_in_flight = _run_kill._spawn_in_flight
    _reset_and_kill_once = _run_kill._reset_and_kill_once
    _sigkill_sessions = _run_kill._sigkill_sessions
    _sigkill_session = _run_kill._sigkill_session
    # Adding and changing jobs: ``kiro_crew.cron_service.service.job_writes``.
    add_job = _job_writes.add_job
    add_job_if_absent = _job_writes.add_job_if_absent
    add_job_if_absent_async = _job_writes.add_job_if_absent_async
    add_managed_job = _job_writes.add_managed_job
    add_managed_job_async = _job_writes.add_managed_job_async
    _persist_add_if_absent_locked = _job_writes._persist_add_if_absent_locked
    _drop_owner_if_parent_gone = _job_writes._drop_owner_if_parent_gone
    _persist_add_locked = _job_writes._persist_add_locked
    add_job_async = _job_writes.add_job_async
    update_job = _job_writes.update_job
    update_job_async = _job_writes.update_job_async
    _update_job_locked_kw = _job_writes._update_job_locked_kw
    enable_job = _job_writes.enable_job
    enable_job_async = _job_writes.enable_job_async
    ack_job = _job_writes.ack_job
    ack_job_async = _job_writes.ack_job_async
    _ack_job_locked = _job_writes._ack_job_locked
    unack_job = _job_writes.unack_job
    unack_job_async = _job_writes.unack_job_async
    _unack_job_locked = _job_writes._unack_job_locked
    # Removing jobs: ``kiro_crew.cron_service.service.job_removal``.
    remove_job = _job_removal.remove_job
    remove_job_async = _job_removal.remove_job_async
    remove_job_if_async = _job_removal.remove_job_if_async
    _audit_refused_removal = _job_removal._audit_refused_removal
    defer_removal = _job_removal.defer_removal
    _drain_pending_removals_locked = _job_removal._drain_pending_removals_locked
    _bump_grant_epochs_for = _job_removal._bump_grant_epochs_for
    _remove_job_rows = _job_removal._remove_job_rows
    _release_children_of_removed = _job_removal._release_children_of_removed
    _remove_jobs_locked = _job_removal._remove_jobs_locked
    remove_jobs = _job_removal.remove_jobs
    _audit_requested_batch_removal = _job_removal._audit_requested_batch_removal
    remove_jobs_sync = _job_removal.remove_jobs_sync
    _remove_jobs_by_owner_locked = _job_removal._remove_jobs_by_owner_locked
    remove_jobs_by_owner = _job_removal.remove_jobs_by_owner
    remove_jobs_by_owner_sync = _job_removal.remove_jobs_by_owner_sync
    # Who owns a job: ``kiro_crew.cron_service.service.job_ownership``.
    adopt_job = _job_ownership.adopt_job
    _adopt_job_locked = _job_ownership._adopt_job_locked
    _release_jobs_owned_by_locked = _job_ownership._release_jobs_owned_by_locked
    release_jobs_owned_by = _job_ownership.release_jobs_owned_by
    _owner_keys_locked = _job_ownership._owner_keys_locked
    _chat_folder_ids_locked = _job_ownership._chat_folder_ids_locked
    chat_folder_ids_async = _job_ownership.chat_folder_ids_async
    owner_keys_async = _job_ownership.owner_keys_async
    # The runs in flight: ``kiro_crew.cron_service.service.run_registry``.
    register_active_session_key = _run_registry.register_active_session_key
    clear_active_session_key = _run_registry.clear_active_session_key
    _run_session_keys = _run_registry._run_session_keys
    active_session_keys = _run_registry.active_session_keys
    get_history = _run_registry.get_history
    is_running = _run_registry.is_running
    _claim_run = _run_registry._claim_run
    attach_run_task = _run_registry.attach_run_task
    discard_finished_run = _run_registry.discard_finished_run
    running_since = _run_registry.running_since
    set_refresh_callback = _run_registry.set_refresh_callback
    set_timeout_alert = _run_registry.set_timeout_alert
    run_job = _run_registry.run_job
    _run_claimed_manual = _run_registry._run_claimed_manual
    # When the scheduler wakes: ``kiro_crew.cron_service.service.wake_timer``.
    _next_wake_secs = _wake_timer._next_wake_secs
    _effective_delay = _wake_timer._effective_delay
    _arm_timer = _wake_timer._arm_timer
    _tick_scan_locked = _wake_timer._tick_scan_locked
    _on_timer = _wake_timer._on_timer
    # Running one job: ``kiro_crew.cron_service.service.run_execution``.
    _run_job_isolated = _run_execution._run_job_isolated
    _execute_with_timeout = _run_execution._execute_with_timeout
    _alert_timeout = _run_execution._alert_timeout
    _execute = _run_execution._execute
    # Landing a run's result: ``kiro_crew.cron_service.service.run_merge``.
    _hold_run_record = _run_merge._hold_run_record
    _apply_held_run_record = staticmethod(_run_merge._apply_held_run_record)
    _release_held_run_record = _run_merge._release_held_run_record
    _reapply_held_run_records = _run_merge._reapply_held_run_records
    _apply_held_run_records_to = _run_merge._apply_held_run_records_to
    _save_held_run_records_locked = _run_merge._save_held_run_records_locked
    _flush_held_run_records = _run_merge._flush_held_run_records
    _merge_job_result = _run_merge._merge_job_result
    _merge_terminal_state_locked = _run_merge._merge_terminal_state_locked
    _saved_job_state = _run_merge._saved_job_state
    # The loop-stall breaker: ``kiro_crew.cron_service.service.stall_breaker``.
    _apply_loop_stall_breaker = _stall_breaker._apply_loop_stall_breaker
    _loop_stall_breaker_verdict = _stall_breaker._loop_stall_breaker_verdict
    _pause_for_loop_stall = _stall_breaker._pause_for_loop_stall
    # The service's copy of the cron store: ``kiro_crew.cron_service.service.store_sync``.
    list_jobs = _store_sync.list_jobs
    count_enabled_from_disk = _store_sync.count_enabled_from_disk
    get_job = _store_sync.get_job
    _snapshot = staticmethod(_store_sync._snapshot)
    _synced_snapshot = _store_sync._synced_snapshot
    list_jobs_async = _store_sync.list_jobs_async
    get_job_async = _store_sync.get_job_async
    status = _store_sync.status
    _file_lock = _store_sync._file_lock
    _record_fingerprint = _store_sync._record_fingerprint
    _keep_store_document = _store_sync._keep_store_document
    _forget_store_document = _store_sync._forget_store_document
    _reset_fingerprint = _store_sync._reset_fingerprint
    _sync = _store_sync._sync
    _load = _store_sync._load
    _unreadable_error = _store_sync._unreadable_error
    raise_if_store_unreadable = _store_sync.raise_if_store_unreadable
    _sync_for_write = _store_sync._sync_for_write
    _save = _store_sync._save

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
        # The app bridge's manifest stamp: its re-sync computes WHICH fields to
        # send from the stamp it read off a loop-side snapshot, several awaited
        # steps before this lock. A stamp that moved meanwhile means another
        # registrar already applied a manifest to this job, and fields computed
        # against the old one would overwrite whatever has been written since.
        expect_stamp = kwargs.pop("expect_app_manifest_digest", None)
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
        # Not a field: the session making this write, recorded on the
        # auto-pause-limit audit row (see CronJob.audit_auto_pause_limit_change).
        audit_actor = kwargs.pop("audit_actor", "") or ""
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
                if expect_stamp is not None and job.app_manifest_digest != expect_stamp:
                    raise CronPendingMismatch("manifest stamp changed concurrently")
                # The cached record is published to every reader the moment it
                # is mutated (``list_jobs`` hands out these very objects), so a
                # save that fails must put it back: otherwise the cache says the
                # update landed while crons.json still holds the old values, and
                # a caller that compares the two -- the app bridge's manifest
                # stamp -- stops retrying a write that never happened. Restored
                # onto the SAME object (identity is what readers hold), and the
                # reload-on-change in ``_sync_for_write`` cannot do this, since a
                # failed save changes nothing on disk.
                previous_limit = job.auto_pause_after_failures
                before = copy.copy(job)
                apply_job_update(job, kwargs, chat_folder_out)
                try:
                    self._save()
                except BaseException:
                    job.__dict__.update(before.__dict__)
                    raise
                job.audit_auto_pause_limit_change(previous_limit, audit_actor)
                logger.info("Updated cron job %s", job_id)
                return job
        return None

    def _delete_history_of(self, job_ids: list[str]) -> None:
        """Delete the run history of removed jobs. WORKER-THREAD or loop-less only.

        Best-effort, like the audit: the jobs are already gone from the store,
        so a history failure is logged and never fails the removal.
        """
        for job_id in job_ids:
            try:
                self._history.delete_job_history_sync(job_id)
            except Exception:
                logger.warning(
                    "History cleanup failed for cron %s (job already removed)",
                    job_id,
                    exc_info=True,
                )

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

    def _remove_job_if_locked(
        self,
        job_id: str,
        expected_revision: str,
        revision_of: Callable[["CronJob"], str],
    ) -> str:
        """Lock/reload/compare/mutate/save core of :meth:`remove_job_if_async`.

        An exact ``revision_of``/``expected_revision`` match proceeds to the same
        remove-rows + cascade + save the unconditional path uses; a live job that
        differs is left untouched (``"changed"``), a missing one ``"not_found"``.
        """
        with self._file_lock():
            self._sync_for_write()
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None:
                return "not_found"
            if revision_of(job) != expected_revision:
                return "changed"
            # Same ordering as _remove_job_locked: the grant-epoch bump reads
            # live rows and must precede the row filter.
            self._bump_grant_epochs_for({job_id})
            restore = self._remove_job_rows({job_id})
            try:
                self._save()
            except BaseException:
                for child, previous_owner in restore:
                    child.session_key = previous_owner
                self._reset_fingerprint()
                raise
            logger.info("Removed cron job %s", job_id)
            return "removed"

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

    #: Validate the inputs and construct the job, no I/O and no lock
    #: (:func:`~kiro_crew.cron_service.fields.build_job`). Every add path builds
    #: through it, so each create surface validates identically before disk work.
    _build_job = staticmethod(build_job)
    #: Random jitter seconds for a scheduled run of a job
    #: (:func:`~kiro_crew.cron_service.schedule.compute_jitter`).
    _compute_jitter = staticmethod(compute_jitter)
    #: Whether a job is due at an epoch (:func:`~kiro_crew.cron_service.schedule.is_due`).
    _is_due = staticmethod(is_due)

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
