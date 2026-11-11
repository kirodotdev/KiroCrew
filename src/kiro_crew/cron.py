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
    bound_script_runs_from_disk,
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
    LOCAL_TIMEZONE,
    _compute_next_run_ts_raw,
    _humanize_cron,
    _job_tz,
    _next_cron_boundary_ts,
    compute_jitter,
    compute_next_run_ts,
    cron_expr_matches,
    default_timezone_name,
    format_schedule,
    get_local_tz,
    is_due,
    is_local_timezone,
    is_valid_skip_date,
    is_valid_timezone,
    job_follows_host,
    job_zone_name,
    never_fires_warning,
    next_wake_secs,
    parse_time_string,
    resolve_timezone_name,
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
    _record_parked_by_run,
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

    A falsy key is an ownerless row (the CLI and Schedule page manage those), not
    an input error. A non-string value is malformed data but gets the same ``""``:
    removal cleanup shares this parser, and raising there would abort after the
    row left memory, so a later save would commit a removal the caller saw fail.
    Guarding here keeps ONE key parser for :func:`cron_owner_matches` and the
    liveness checks.
    """
    if not isinstance(session_key, str) or not session_key.startswith("cron:"):
        return ""
    return session_key.split(":")[1] if len(session_key.split(":")) > 1 else ""


def cron_owner_matches(job_owner: str, target: str) -> bool:
    """Whether ``job_owner`` names the same principal as ``target``.

    THE one place an owner-key spelling is compared for release. One run may
    present ``cron:<job id>``, ``cron:<job id>:<run id>`` or
    ``cron:<job id>:<agent>``, and a job it creates carries whichever it
    presented, so plain ``==`` would silently skip a child with a longer spelling.
    Parses through :func:`cron_job_id_from_session_key` so release and the MCP
    surface agree on what a ``cron:`` key means.

    Non-cron owners compare exactly: they have one spelling each, and loosening
    them would let one session's key reach another's jobs. The MCP ownership gate
    (``_owned_by``) stays exact equality; this only decides what a RETIRED
    principal's cleanup may release.
    """
    if job_owner == target:
        return True
    owner_principal = cron_job_id_from_session_key(job_owner)
    return bool(owner_principal) and owner_principal == cron_job_id_from_session_key(target)


# Resolved per call, never at import: an import-time binding freezes the data
# home and defeats pod isolation, the legacy-home migration and test isolation.
# None = live home; tests monkeypatch this override. See config.md "Data Home".
_DEFAULT_DIR: Path | None = None


def _default_dir() -> Path:
    """Cron data directory, resolved against the live data home."""
    return _DEFAULT_DIR if _DEFAULT_DIR is not None else data_home()


# The doctor's three store readers live here: its facade test pins
# `unhealthy_jobs_from_disk` and `job_pause_state_from_disk` to `kiro_crew.cron`.
# This one lists the copies startup moved after `crons.json` failed to decode or
# held no `jobs` list. Each is an unchanged rename under `cron-history/quarantine`,
# behind the file-tool fence and the sandbox mask.
def cron_store_quarantine_copies() -> tuple[Path, list[Path]]:
    """The cron store's path and every copy gateway startup moved aside, for doctor."""
    store = config_dir() / _CRONS_FILE
    return store, quarantine_copies(store)


def unhealthy_jobs_from_disk() -> tuple[list[tuple[str, str]], list[tuple[str, str]], bool]:
    """Return ``(auto_paused, errored, loadable)`` for the doctor's cron check.

    The first two are ``(id, name)`` pairs needing attention. ``loadable`` comes
    from the same read: an unloadable store gives two empty buckets, which look
    like a healthy empty store, so without it a stopped scheduler would pass.

    Reads ``crons.json`` directly, so ``kirocrew doctor`` can speak while the
    gateway is wedged. Never raises (via :func:`_read_job_records`): a missing,
    unreadable or malformed store, or a malformed record, reports "nothing
    found", because a host with a corrupt store most needs the other diagnostics.

    The buckets are disjoint. An auto-paused job almost always has
    ``last_status="error"`` too; auto-pause wins, since re-triggering a paused job
    does not un-pause it. A user-paused job is in NEITHER bucket: a deliberate
    pause is not a health signal. Both pause flags can be set at once (enabling
    is what clears ``auto_paused``), and the user pause wins. A one-shot its own
    run parked is not a user pause (``_record_parked_by_run``) and is scanned.
    """
    auto_paused: list[tuple[str, str]] = []
    errored: list[tuple[str, str]] = []
    records, loadable = _read_job_records(config_dir() / _CRONS_FILE)
    for j in records:
        if not _is_loadable_record(j):
            # A record the scheduler cannot build would get a hint for a job that
            # never runs, and a non-empty bucket would hide the unloadable-store
            # report behind it. The predicate that clears `loadable` decides here.
            continue
        entry = (str(j.get("id") or "no-id"), str(j.get("name") or "(unnamed)"))
        if _record_user_paused(j) and not _record_parked_by_run(j):
            # A fired one-shot is parked with the user-pause flags, but its run
            # stamped `last_run_ts`: that park is the run's, so its error counts.
            continue
        if j.get("auto_paused", False):
            auto_paused.append(entry)
        elif j.get("last_status") == "error":
            # Neither pause flag is set here: still-scheduled failures only.
            errored.append(entry)
    return (auto_paused, errored, loadable)


def job_pause_state_from_disk(job_id: str) -> str | None:
    """``"parked after its one-shot run"`` / ``"paused by the user"`` /
    ``"auto-paused"`` / ``"enabled"`` for *job_id*, or None when the store has
    no such job.

    A sibling of :func:`unhealthy_jobs_from_disk` for the doctor's stall
    attribution: once a dump is attributed to a job, the next question is
    whether that job is still scheduled to run again, answered from the store
    directly so it holds when the gateway is down.
    """
    records, _loadable = _read_job_records(config_dir() / _CRONS_FILE)
    for j in records:
        if str(j.get("id") or "") != job_id:
            continue
        if _record_parked_by_run(j):
            return "parked after its one-shot run"
        if _record_user_paused(j):
            return "paused by the user"
        if j.get("auto_paused", False):
            return "auto-paused"
        return "enabled"
    return None


_REAPER_INTERVAL = 60  # seconds between reaper sweeps
_REAPER_RESET_TIMEOUT = 30.0  # max seconds for session reset in reaper
#: How many ROUNDS of key-ending a reap or cancel runs over a run
#: (``_end_run_sessions``). Each key gets ONE reset-then-kill pass
#: (``_end_run_processes``) under its ENDING FENCE (``SessionManager.ending_key``),
#: held until the terminal record and audit are written, so no session lands
#: under a fenced key. A sequential run can still register a NEW key mid-round
#: (its task is cancelled only after the passes), so keys are re-read after each
#: round. Bounded so a run that keeps registering cannot hold the reaper: a key
#: registered after the last round is a kill failure, audited ``failed``. Each
#: round spends at most ``_REAPER_RESET_TIMEOUT`` plus the kill's own bounds.
_ENDING_ROUNDS = 2
#: How long ``stop()`` waits for the notices of the runs it interrupted
#: (``set_interrupt_alert``) before it returns. Bounded because it spends the
#: gateway's graceful-shutdown budget; the bell note comes first, so a slow
#: Slack or channel post is what a cut loses.
_INTERRUPT_ALERT_WAIT_SECS = 3.0
#: How long a planned in-app restart waits for ``stop()``
#: (``stop_cron_before_restart``): the runs' unwind, then the notices' own
#: ``_INTERRUPT_ALERT_WAIT_SECS``, so it is above that bound.
_RESTART_STOP_SECS = 5.0


async def stop_cron_before_restart(crons: CronService | None) -> None:
    """Stop the gateway's cron service for a planned in-app restart. Never raises.

    The dashboard Restart (and update apply), the gateway's update restart and
    the Slack ``/kirocrew restart`` end in an exec or a hard exit, so the
    gateway's ``_shutdown``, where ``stop()`` runs on a SIGTERM, never runs. A
    run in flight would end with the process image: no history row, no notice
    (``set_interrupt_alert``), and, as an exec keeps the pid, an in-flight marker
    whose writer still reads as alive. Each of those restarts calls this past
    its last refusal and before its ``close_all()``, the order ``_shutdown``
    takes. Bounded by ``_RESTART_STOP_SECS``: a stop still running then is
    cancelled, not awaited, and the restart goes on. ``None`` is a no-op.
    """
    if crons is None:
        return
    try:
        stop = asyncio.ensure_future(crons.stop())
    except Exception:
        logger.warning("Cron: could not stop the service before the restart", exc_info=True)
        return
    done, _ = await asyncio.wait({stop}, timeout=_RESTART_STOP_SECS)
    if stop not in done:
        stop.cancel()
        logger.warning(
            "Cron: the stop before the restart ran past %.0fs; restarting anyway",
            _RESTART_STOP_SECS,
        )
    elif not stop.cancelled() and stop.exception() is not None:
        logger.warning("Cron: the stop before the restart failed", exc_info=stop.exception())


async def _sleep_out_jitter(jitter: float) -> None:
    """Wait ``jitter`` seconds before a scheduled run, ending on the wall clock too.

    Jitter spreads the WALL-CLOCK fire time. A bare ``asyncio.sleep`` uses
    ``time.monotonic()``, which on macOS stops while the host sleeps, so a job
    that fires in a brief DarkWake would stay "Running" for hours of wall time.

    The wait ends at whichever deadline comes FIRST: the wall-clock one (a resumed
    host starts within one ``_JITTER_WALL_SLICE_SECS`` slice), or the monotonic
    one (a backward clock step cannot stretch the wait). ``cancel()`` lands at the
    slice's ``asyncio.sleep``.
    """
    wall_deadline = time.time() + jitter
    mono_deadline = time.monotonic() + jitter
    while True:
        remaining = min(wall_deadline - time.time(), mono_deadline - time.monotonic())
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _JITTER_WALL_SLICE_SECS))


# Loop-safety guard.
# The store lock (``CronService._file_lock``) must NEVER be acquired on a thread
# with a running asyncio loop: its bounded ``time.sleep`` spin would park the loop.
# Loop callers use the ``*_async`` mutators and ``CronSDK`` offloads to a worker
# thread; ``_file_lock`` enforces it anyway, since new writers drift. Off by
# default it logs a throttled warning; KIROCREW_STRICT_LOOP_SAFETY=1 (set by the
# CI regression test) makes it raise.
_STRICT_LOOP_SAFETY_ENV = "KIROCREW_STRICT_LOOP_SAFETY"
_loop_safety_warned = False


class CronLoopSafetyError(RuntimeError):
    """Raised when the cron store lock is acquired on a running event loop.

    The lock spin would block that loop under contention. Use the ``*_async``
    mutators (``add_job_async`` et al.) or let ``CronSDK`` offload to a worker
    thread. Raised only under ``KIROCREW_STRICT_LOOP_SAFETY``; otherwise it warns.
    """


class CronPauseNotSaved(RuntimeError):
    """Raised by :meth:`CronService.cancel` when a cancelled one-shot's pause did not reach the store.

    The run is cancelled and its history entry written; only the park failed to
    land. A scheduled run keeps its terminal record for the next store tick, but
    this error still reports that no durable write has returned yet. The message
    describes the job as the cancel last saw it, after the merge (see
    :func:`_cancelled_park_report`).
    """


# Reasons ``_merge_terminal_state_locked`` gives for saving no park.
_PARK_JOB_REMOVED = "the job is no longer in the cron store"
_PARK_NEWER_RUN = "the cron store already holds a newer run of the job"
_PARK_NOT_ONE_SHOT = "the job is no longer a one-shot in the cron store"
_PARK_NEWER_CHOICE = "the job was paused or resumed after the cancel"
_PARK_NOT_SAVED = "The run was cancelled, but its pause could not be saved"
_ON_SCHEDULE_MESSAGE = (
    f"{_PARK_NOT_SAVED}: a newer run of the job was recorded meanwhile, so the job is back"
    " on its schedule now. Pause or delete it if it must not run again."
)
_RESUMED_MESSAGE = (
    "The run was cancelled, but the job was resumed meanwhile, so it is back on its"
    " schedule now. Pause or delete it if it must not run again."
)
_RECURRING_MESSAGE = (
    f"{_PARK_NOT_SAVED}: the job was changed to a recurring schedule meanwhile and runs on"
    " that schedule now. Pause or delete it if it must not run again."
)


def _cancelled_park_report(
    job: CronJob | None,
    reason: str | None,
    saved: str | None = None,
    *,
    held: bool = False,
) -> tuple[str, str | None]:
    """The history note and the message for a cancelled one-shot's park.

    ``job`` is the job as ``cancel`` sees it after the merge, and ``reason`` the
    merge's answer (None: a save holding the park returned). Every end of the
    merge is worded here, from that job and not from the reason, so the text
    is true whatever wrote the store meanwhile. A message of None means nothing
    is left to pause, and ``cancel`` returns True: a failed park answers that
    only on ``saved``, what the store as saved shows when it shows the job
    cannot run again (``CronService._saved_job_state``). ``held`` says this
    process still holds the cancel's terminal record for a later store write
    (a scheduled run): a reload re-applies it, so only a restart before that
    write lands can run the job again.
    """
    if reason is None:
        return "", None
    if reason == _PARK_NEWER_CHOICE and job is not None:
        # The store saved the user's later choice, so it is the job's state.
        if job.enabled:
            return "; the job was resumed meanwhile, so it is back on its schedule now", (
                _RESUMED_MESSAGE
            )
        return "; the job was paused meanwhile", None
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
    if held:
        return f"{note}, so {subject} may run again after a restart", (
            f"{_PARK_NOT_SAVED}. It is held off the schedule until a later cron store"
            f" write saves the pause, so {subject} may run again only if the gateway"
            " restarts first. Delete it if it must not run again."
        )
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
    # The jobs[] objects the last _load could build no job from and kept
    # (StoreDocument.skipped). _save writes them back after the jobs.
    _skipped_records: tuple[Any, ...] = ()

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
        # True during an _on_timer() dispatch pass, so _arm_timer() does not
        # cancel self._timer_task before the sweep has spawned its due jobs.
        self._on_timer_running = False
        self._running = False
        # The gateway loop, captured in create()/start(). When _arm_timer() is
        # reached off the loop (a to_thread worker whose _sync()->_load() re-arms),
        # it hands the arm back via loop.call_soon_threadsafe, so no off-loop
        # mutation path can leave the timer un-armed. None in loop-less processes
        # (CLI, MCP server, apps SDK, tests).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_mtime: float = 0.0
        # Fingerprint of the store as last LOADED, for _sync's change check. The
        # authority is a content DIGEST of the parsed bytes: mtime and size can
        # both match after an external write (coarse mtime, an equal-length
        # rename), and the next _save would overwrite it. mtime_ns/size are kept
        # for diagnostics. _save refreshes all three so we never reload our write.
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
        # One-shot removals deferred on CronStoreBusy. The timer tick drains them
        # under the store lock (_drain_pending_removals_locked), so a completed
        # one-shot is eventually removed and never re-fires meanwhile.
        self._pending_removals: set[str] = set()
        # The schedule that fired, for each queued ``delete_after_run`` consume:
        # the drain keeps a row given a new schedule since. An entry lives until
        # the drain resolves its id, independently of the held run records.
        self._fired_schedules: dict[str, CronSchedule] = {}
        # Completed runs whose result merge has not been saved yet, the newest
        # run per job id (see _hold_run_record). The guard covers the dict and
        # every apply of a held record: the loop holds, worker threads re-apply
        # and release, and a hold and a re-apply never interleave on one job.
        self._held_run_records: dict[str, CronJob] = {}
        # The held records that are a scheduled cancel or reap's terminal
        # settlement, by job id (the same object as in _held_run_records). Their
        # park is not on disk until a save lands it, so they leave the listed
        # job's ``user_paused`` as persisted (_apply_held_run_record).
        self._held_settlements: dict[str, CronJob] = {}
        self._held_run_records_guard = threading.Lock()
        # True while a critical-posture episode is deferring scheduled
        # firings (see _on_timer). Log-throttle state only: the INFO line
        # fires once per deferral episode, not once per deferred tick.
        self._admission_deferring: bool = False
        self._admission_last_log: float = 0.0
        # The previous timer tick's clock, which bounds the cron minutes a late
        # tick catches up (_on_timer). None until this process's first tick.
        self._last_tick_ts: float | None = None
        # job_id -> ordered live session keys, each attributed to the run
        # (_RunClaim) that registered it. One job can hold several: an older
        # stateless run kept alive for pending sub-agents, or one key per agent
        # in a sequential run. Keeping only the newest would let a reap record
        # ``reaped`` while an earlier agent's session survives. The attribution
        # lets a reap end all of its run's keys (newest first, by insertion
        # order) and leave an older run's alone. Re-registering is idempotent:
        # one SessionManager reset retires the whole key.
        self._active_session_lock = threading.Lock()
        self._active_session_keys: dict[str, dict[str, _RunClaim | None]] = {}
        # (job id, key) pairs whose registering run ended its turn on the key
        # (``finish_session_key``): retired by the last sub-agent's cleanup even
        # while that run's claim is live.
        self._finished_session_keys: set[tuple[str, str]] = set()
        self._sessions: SessionManager | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._push_refresh: Callable[[str], None] | None = None  # set externally
        # Creates a missing Schedule-page folder by name and returns
        # ``(folder_id, error)``. Set by the dashboard once its socket is bound
        # (``wire_app_cron_folder_creator``), so the app cron bridge can file a
        # job in the folder its manifest names on install. None during gateway
        # startup and in processes that do not own ``cron_folders.json``.
        self.folder_creator: Callable[[str], Awaitable[tuple[str, str | None]]] | None = None
        # The gateway's failure alert for a run its deadline ended (set_timeout_alert),
        # and the alerts in flight: the loop keeps only weak references to tasks.
        self._timeout_alert: Callable[[CronJob, str], Coroutine[Any, Any, None]] | None = None
        self._timeout_alerts: set[asyncio.Task[None]] = set()
        # The gateway's notice for a run stop() cut short (set_interrupt_alert),
        # and the notices in flight, which stop() waits for before it returns.
        self._interrupt_alert: Callable[[CronJob, str], Coroutine[Any, Any, None]] | None = None
        self._interrupt_alerts: set[asyncio.Task[None]] = set()
        _cfg = KiroCrewConfig.load().cron_history
        _history_dir = base_dir if base_dir is not None else _default_dir()
        # History is BEST-EFFORT: a raise here would take down the whole cron
        # subsystem. The store never raises (_prepare_dir resolves usability,
        # _degrade absorbs later failures), so there is deliberately no
        # try/except. Its directory setup is blocking I/O, deferred on the same
        # condition as _load() below so create() runs both off the loop.
        self._history = CronHistoryStore(
            base_dir=_history_dir,
            cron_summary_cap=_cfg.cron_summary_cap,
            cron_trace_cap_kb=_cfg.cron_trace_cap_kb,
            cron_max_records_per_job=_cfg.cron_max_records_per_job,
            cron_max_index_records=_cfg.cron_max_index_records,
            _defer_prepare=_defer_initial_load,
        )
        # Load once at construction: the read paths (list_jobs / get_job) are
        # CACHE-ONLY, and loop-less callers (CLI, MCP, tests) read without
        # start(). No timer is armed: _running is still False. The load reads and
        # hashes all of crons.json, which would block a running loop, so loop
        # contexts MUST use CronService.create() (it defers this load to a worker
        # thread). Enforced by
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
    retire_finished_session_key = _run_registry.retire_finished_session_key
    finish_session_key = _run_registry.finish_session_key
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
    set_interrupt_alert = _run_registry.set_interrupt_alert
    trigger_run = _run_registry.trigger_run
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
    _alert_interrupt = _run_execution._alert_interrupt
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
    _merge_terminal_state_with_retry = _run_merge._merge_terminal_state_with_retry
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
        # Optional caller-owned OUT dict, filled with
        # ``{"chat_folder_was": <prior folder, possibly "">}`` when the update
        # changes ``chat_folder_id``. The dashboard needs the prior value read
        # atomically with the write; a field on the shared ``CronJob`` would be
        # clobbered by concurrent callers, a caller's own dict is not.
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
                # ``list_jobs`` hands out these very objects, so a failed save must
                # restore them (on the SAME object), or the app bridge's manifest
                # stamp would stop retrying a write that never landed. Only this
                # update's fields are restored, and only while they still hold
                # its value: a run in flight writes its own fields (failure count,
                # pause, last status) onto this object without the store lock.
                # Hence the private copy: it shows which fields this update set.
                previous_limit = job.auto_pause_after_failures
                staged = copy.copy(job)
                prior = dict(staged.__dict__)
                apply_job_update(staged, kwargs, chat_folder_out)
                edits = {
                    name: value
                    for name, value in staged.__dict__.items()
                    if name not in prior or prior[name] is not value
                }
                for name, value in edits.items():
                    setattr(job, name, value)
                try:
                    self._save()
                except BaseException:
                    for name, value in edits.items():
                        if name not in job.__dict__ or job.__dict__[name] is not value:
                            continue
                        if name in prior:
                            setattr(job, name, prior[name])
                        else:
                            delattr(job, name)
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
                    # Only a reload can put the removed rows back, so drop the
                    # fingerprint to force one; otherwise the next _save() would
                    # persist the removal this caller was told had failed.
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
                    # A request that moves the job's pause state is ordered after
                    # every terminal record frozen before it, so a pending record
                    # keeps this choice (``_apply_held_run_record``). So is a
                    # resume of a one-shot: another process can hold a cancelled
                    # run's park for it that this store never received, and the
                    # resume is the user's answer to it. Any other request that
                    # changes nothing records nothing, and its save leaves the
                    # store's bytes as they were.
                    if (
                        (job.enabled, job.user_paused) != (enabled, not enabled)
                        or (enabled and job.auto_paused)
                        or (enabled and job.schedule.kind == "at")
                    ):
                        job.user_pause_generation += 1
                    job.user_paused = not enabled
                    job.user_pause_requested = not enabled
                    job.enabled = enabled
                    # Re-enabling clears an execution auto-pause; without this a
                    # job auto-paused after failures would be re-derived as
                    # disabled on the next reload despite the explicit resume.
                    if enabled and job.auto_paused:
                        job.auto_paused = False
                        # Reset the counter too, or the next failure would
                        # re-auto-pause at once (as record_success does).
                        job.consecutive_failures = 0
                        # A user resume that lifts an auto-pause restores execute
                        # permission — audit it like the auto-pause transition.
                        job._audit_pause_change("auto_pause_cleared")
                    try:
                        self._save()
                    except BaseException:
                        # A run in flight also writes these pause fields, so
                        # neither restoring nor keeping them is right; reload
                        # instead. A run's write that must outlive a reload is a
                        # held record or settlement (re-applied by ``_load``) or
                        # a pending removal. The fingerprint is dropped first so
                        # a reload that cannot run here happens on the next
                        # ``_sync``; an unreadable store latches ``_load_failed``,
                        # which refuses every save until a load replaces it.
                        self._reset_fingerprint()
                        try:
                            self._sync()
                        except Exception:
                            logger.error(
                                "Cron: reload after a failed pause/resume save did not run",
                                exc_info=True,
                            )
                        else:
                            if self._load_failed:
                                logger.error(
                                    "Cron: store unreadable after a failed pause/resume save;"
                                    " writes are refused until it reloads"
                                )
                        raise
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

        Under ``KIROCREW_STRICT_LOOP_SAFETY`` it raises :class:`CronLoopSafetyError`;
        otherwise it warns once, so an unforeseen caller is not broken in
        production. The ``*_async`` mutators and
        :class:`~kiro_crew.apps.cron_sdk.CronSDK` run the lock on a worker thread
        with no loop, so they pass.
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
