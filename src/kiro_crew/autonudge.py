"""Auto-nudge service — reactive same-session self-prompting loop.

Each active loop is bound to a dashboard chat slot. When the slot's turn
completes (``HOOK_EVENT_STOP``), we arm a timer toward the loop's persistent
deadline (``next_due_ts``). If the deadline elapses with no new user input,
we inject the configured nudge message as the next turn into the same slot.

The countdown is DEADLINE-PRESERVING: a user message cancels the pending fire
(a nudge must never race a human turn) but does not push the deadline back —
when the user's turn ends, the timer resumes toward the same ``next_due_ts``,
firing shortly after the turn if the deadline already passed. Only the loop's
own delivered cycles start a fresh full interval (measured from the nudge
turn's end). Without this, a session chatted in more often than ``idle_secs``
starves its loop forever: every message restarted the full interval, so a
30-minute babysit loop in an active conversation never fired at all.

State is persisted to ``~/.kiro/crew/autonudge.json`` (fcntl-locked, atomic
write). On gateway restart, active loops are reloaded and timers re-armed.

The browser observes the loop through the normal chat stream path — nudges
appear as user-style messages tagged ``[auto-nudge cycle N]`` so they are
visually distinct from human input.

Feature-flagged via env ``KIROCREW_AUTONUDGE`` (on by default; set to ``0`` to disable).

The service's responsibilities live in :mod:`kiro_crew.autonudge_service`, one owner
per module: the loop record and its vocabulary, monitor inference, the durable store,
the maintenance transaction, the timer lifecycle, the probe gate, the wake judge's
tick, the fire cycle, the loop transactions, the structured-monitor transitions, and
the loader, which vets and scrubs every stored row at the trust boundary and keeps the
scrub helpers every other entrance uses too.
:class:`AutoNudgeService` is defined here and composes them. It keeps the live loop
registry and the coordination state those owners share, its lifecycle and singleton,
the observer hook and the persistence entry points. The owners' methods are bound on
the class by name, in one table at the end of its body.

This module is also the subsystem's import and patch surface: every name it defined
before those owners moved out still resolves here as the same object its owner holds,
and so does every public name it imported from the rest of the package and every
other import callers and tests read off it. The names moved code reads through this
module on each call -- so a patch here reaches it -- are
``_OVERDUE_REARM_SECS``, ``_RECONCILE_INTERVAL_SECS``, ``_DRAIN_CANCEL_GRACE_SECS``,
``replace_with_retry``, ``fsync_dir``, ``scrubbed_judge_spec``, ``_bounded_judge_verdict``,
``repair_sentinel_path``, ``_INSTANCE``, ``_MAINTENANCE_LOCKS`` and
``_MUTATION_LOCK_OWNERS``. Every other name the owners read is their own global, which
a patch here does not reach.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time  # noqa: F401 -- read off this module by callers and tests
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew import irq  # noqa: F401 -- read off this module by callers and tests
from kiro_crew import platform_compat  # noqa: F401 -- re-exported
from kiro_crew import probes  # noqa: F401 -- re-exported
from kiro_crew import shutdown_event  # noqa: F401 -- re-exported
from kiro_crew import autonudge_stop_log, validation  # noqa: F401 -- re-exported
from kiro_crew.atomic_write import (  # noqa: F401 -- read off this module by callers and tests
    fsync_dir,
    replace_with_retry,
)
from kiro_crew.autonudge_service import firing as _firing
from kiro_crew.autonudge_service import gate as _gate
from kiro_crew.autonudge_service import judge_tick as _judge_tick
from kiro_crew.autonudge_service import maintenance as _maintenance
from kiro_crew.autonudge_service import monitor_records as _monitor_records
from kiro_crew.autonudge_service import mutations as _mutations
from kiro_crew.autonudge_service import store_loader as _store_loader
from kiro_crew.autonudge_service import timers as _timers
from kiro_crew.autonudge_service.gate import (  # noqa: F401 -- re-exported
    _JUDGE_QUIET_STREAK_FLOOR_DEFAULT,
    _MAX_QUIET_STREAK,
    _WAKE_FOLLOWUP_TICKS,
)
from kiro_crew.autonudge_service.maintenance import (  # noqa: F401 -- re-exported
    _DRAIN_CANCEL_GRACE_SECS,
    _assert_mutation_lock_owned,
    _AutoNudgeMaintenanceView,
    _cancel_and_drain_tasks,
    _claim_mutation_lock,
    _DurabilityDrain,
    _maintenance_lock,
    _release_mutation_lock,
    _unclaim_mutation_lock,
)
from kiro_crew.autonudge_service.model import (  # noqa: F401 -- re-exported
    _CHANNEL_KEY_PREFIXES,
    _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
    _MAX_IDLE_SECS,
    _MIN_IDLE_SECS,
    _REPLACEABLE_LOOP_STOP_REASONS,
    _START_FAILURE_BACKOFF_AFTER,
    _START_FAILURE_STANDDOWN_AFTER,
    _TERMINAL_BOUND_REASONS,
    APPROVAL_STALL_REASON,
    AUTONUDGE_STOP_REASON,
    CONSECUTIVE_FAILURE_REASON,
    CYCLE_CAP_REASON,
    FINISHED_LOOP_REASONS,
    LOOP_BOUND_FIELDS,
    MANUAL_STOP_REASON,
    MODEL_REFUSED_REASON,
    MONITOR_TERMINAL_REASON,
    NUDGE_RENEW_DUE_SHARE,
    RUNTIME_BUDGET_REASON,
    SENTINEL_DROPPED_REASON,
    SERVICE_SHUTTING_DOWN_MESSAGE,
    SESSION_START_FAILURE_REASON,
    STOP_SENTINEL_REASON,
    STRUCTURAL_TERMINAL_REASON,
    AutoNudgeStaleBaseline,
    AutoNudgeStoreUnvetted,
    MonitorUpdateConflict,
    NudgeAdmissionReason,
    NudgeAdmissionRefused,
    NudgeLoop,
    _MutationAdmission,
    _positive_number,
    _stopped_row_is_replaceable,
    _TransactionSubmissions,
    is_channel_key,
    is_structured_monitor_loop,
    new_goal_token,
    nudge_cycle_header,
    reason_in,
    runtime_budget_exceeded,
    stored_bound,
    terminal_notification_delivery_matches,
)
from kiro_crew.autonudge_service.store import (  # noqa: F401 -- re-exported
    _NUDGES_FILE,
    _QUARANTINE_FILE,
    _STORE_VERSION,
    LoopStore,
    _locked_file,
    _quarantine_row_key,
    _rows_or_empty,
)
from kiro_crew.autonudge_service.store_loader import (  # noqa: F401 -- re-exported
    _JUDGE_MAX_CRITERION_CHARS,
    _JUDGE_MAX_CURSORS,
    _JUDGE_MAX_OUTCOME_CHARS,
    _JUDGE_MAX_TARGET_CHARS,
    _JUDGE_MAX_TARGETS,
    ADDRESSING_FIELDS,
    _addressing_value_unsafe_why,
    _bounded_judge_cursors,
    _bounded_judge_pr_seen,
    _bounded_judge_recent,
    _bounded_judge_spec,
    _bounded_judge_verdict,
    _is_torn_deactivation,
    _repair_number,
    redact_store_value,
    scrub_loop_text,
    scrubbed_judge_spec,
)
from kiro_crew.autonudge_service.subject import (  # noqa: F401 -- re-exported
    _JUDGE_PR_SEEN_DIGEST_CHARS,
    _JUDGE_PR_SEEN_REMARKS,
    _PR_FACTS_TICK_KEYS,
    _judge_pr_targets,
    _pr_facts_digest,
    _pr_observation_of,
    infer_monitor,
    infer_subject,
    loop_subject,
    read_session_texts,
)
from kiro_crew.autonudge_service.timers import (  # noqa: F401 -- re-exported
    _MONITOR_RETRY_BACKOFF_SECS,
    _MONITOR_RETRY_MAX_BACKOFF_SECS,
    _OVERDUE_REARM_SECS,
    _REARM_BACKOFF_MAX_SHIFT,
    _REARM_BACKOFF_SECS,
    _REARM_MAX_BACKOFF_SECS,
    _RECONCILE_INTERVAL_SECS,
    _current_task_or_none,
    _resolve_beat,
)
from kiro_crew.config.loader import data_home  # noqa: F401 -- re-exported
from kiro_crew.config.loader import config_dir
from kiro_crew.config.paths import legacy_home
from kiro_crew.constants import MAX_BANNER_CHARS  # noqa: F401 -- re-exported
from kiro_crew.monitoring.decision import (  # noqa: F401 -- re-exported
    decide_monitor,
    monitor_budget_reason,
    monitor_stall_reason,
    stamp_monitor_alerted,
)
from kiro_crew.monitoring.github_provider_errors import (  # noqa: F401 -- re-exported
    is_unattempted_probe,
)
from kiro_crew.monitoring.limits import validate_runtime_secs  # noqa: F401 -- re-exported
from kiro_crew.monitoring.models import (  # noqa: F401 -- re-exported
    MONITOR_BUSY_RETRY_SECS,
    MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS,
    MONITOR_STATE_VERSION,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_COMPLETION_UNAVAILABLE,
    MONITOR_STOP_SESSION_CLOSE,
    MONITOR_STOP_SESSION_UNAVAILABLE,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    MONITOR_STOP_USER,
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorCreationSurface,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorProbeResult,
    MonitorState,
    MonitorVerdict,
    monitor_state_from_dict,
    monitor_state_to_dict,
    quarantine_monitor_state,
    retained_outcome_blocks_rearm,
)
from kiro_crew.monitoring.registry import (  # noqa: F401 -- re-exported
    REVIEW_READY,
    kind_supports_objective,
)
from kiro_crew.platform import PlatformCompositionError  # noqa: F401 -- re-exported
from kiro_crew.platform import redact_log_via_context  # noqa: F401 -- re-exported
from kiro_crew.platform import redact_via_context  # noqa: F401 -- re-exported
from kiro_crew.probes import targets  # noqa: F401 -- re-exported
from kiro_crew.security import redact_credentials  # noqa: F401 -- re-exported
from kiro_crew.security import redact_exfiltration_urls  # noqa: F401 -- re-exported
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)


def binding_key_for(session_key: str) -> str | None:
    """Map a session key to its AutoNudge binding (slot) key, or ``None`` if the
    session is not nudge-able.

    ``dashboard:chat-N-TS`` → bare slot key ``chat-N-TS`` (the autonudge layer
    keys dashboard loops on the bare slot key); ``slack:``/``discord:``/``webex:``
    session keys pass through unchanged (channel-bound loops). Anything else
    (``cron:``, ``hook:``, ``subagent:`` ...) is not a nudge-able session.

    Single source of truth shared by the ``monitor_start`` MCP tool and the
    workflow ``ctx.nudge`` port so both agree on what "nudge-able" means.

    NARROWER THAN :data:`_CHANNEL_KEY_PREFIXES` ON PURPOSE, and for a different
    reason than that tuple's own exclusions. ``is_channel_key`` classifies a key's
    SHAPE; this function answers whether an arm request can be honoured, which
    additionally requires an ownership check in ``autonudge_authz`` and a fire
    route in the gateway's ``_fire`` dispatcher — implemented for ``slack:``,
    ``discord:`` and ``webex:`` only. Passing a namespace through ahead of those two would
    arm a loop that is denied at the chokepoint (or removed on its first fire
    with "unsupported channel key"), which is strictly worse than refusing it
    here: a clean "not supported from this session type" instead of a loop that
    appears to exist and then dies. Widen this set only together with the
    matching ownership check and fire route.
    """
    if not session_key:
        return None
    if session_key.startswith("dashboard:"):
        return session_key.split(":", 1)[1]
    if session_key.startswith(("slack:", "discord:", "webex:")):
        return session_key
    return None


def structured_monitor_binding_key_for(session_key: str) -> str | None:
    """Return a binding only when structured wake delivery is supported.

    Legacy prompt loops have a Webex fire adapter. Structured monitors require
    typed dispatch and completion correlation, which currently exist only for
    dashboard, Slack, and Discord sessions.
    """
    binding = binding_key_for(session_key)
    if binding is None or binding.startswith("webex:"):
        return None
    return binding


def enabled() -> bool:
    """Feature flag — on by default. Set ``KIROCREW_AUTONUDGE=0`` to disable."""
    return os.environ.get("KIROCREW_AUTONUDGE", "1").lower() not in ("0", "false", "no")


def repair_sentinel_path(raw: str) -> str:
    """Re-home a persisted ``stop_sentinel_path`` onto the CURRENT data home.

    The kill-switch path is resolved once at arm time (``resolve_stop_sentinel``,
    which builds it under ``workspace_dir_for(...)`` → normally
    ``config_dir()/workspace``) and then persisted verbatim in the loop store.
    That store survives the one-time ``~/.kirocrew`` → ``~/.kiro/crew`` data-home
    migration (``config/paths.py``) and is re-armed on the next ``start()``, so a
    loop armed BEFORE the move comes back pointing at a directory that no longer
    exists. ``_timer`` only ever tests ``Path(stop_sentinel_path).exists()``, so
    such a loop has a DEAD kill switch: a sentinel written at the freshly
    resolved (current-home) path is never seen, and the only remaining stops are
    ``max_cycles`` and an explicit remove.

    Three transformations, in order:

    1. **Pass through a path already under the CURRENT home.** Checked FIRST,
       because ``KIROCREW_HOME`` may legally point *inside* the legacy root
       (e.g. ``~/.kirocrew/dev``). Such a path is lexically under
       ``~/.kirocrew`` yet already live and correct; re-homing it would produce
       ``~/.kirocrew/dev/dev/workspace/…``, persist that, and — since the
       rewrite is not idempotent — append another segment every boot, disabling
       a WORKING kill switch with the very code meant to repair dead ones.
    2. **Re-home a STRANDED legacy-rooted path.** A path under ``~/.kirocrew``
       is rewritten onto the resolved current home. The migration relocated the
       whole tree wholesale, so the tail after the home prefix is still correct.
       Gated on the sentinel's directory no longer existing: an absolute
       ``workspaces.<name>.dir`` may legitimately live inside that tree (and the
       legacy root can survive as debris), and rewriting a live path would move
       a working kill switch outside its configured workspace and persist that.
       Skipped when the current home IS the legacy home (``KIROCREW_HOME``
       pointing there, or the migration's fall-back-to-legacy path) — there the
       persisted path is already live. Both sides are normalized LEXICALLY
       (``os.path.normpath``, no filesystem access) before the containment
       test, so an unnormalized value like ``~/.kirocrew/../workspace/STOP``
       is not mistaken for a legacy-contained path and rewritten elsewhere.
    3. **Re-apply the arm-time sensitivity refusal.** ``authorize_and_add_nudge``
       refuses a sensitive ``stop_sentinel_path`` at arm time, but the denylist
       can widen between releases and the persisted value outlives the original
       check. A path that is sensitive NOW is dropped to ``""`` (no sentinel)
       rather than kept, so the service never stats an attacker- or
       credential-adjacent location on a timer. The check itself FAILS CLOSED:
       if ``is_sensitive_path`` raises, the path is dropped rather than trusted,
       because an unvalidated path is exactly what this step exists to reject.

    Returns the (possibly rewritten) path, or ``""`` to mean "no sentinel".
    Non-``str`` input (a malformed store where ``stop_sentinel_path`` is a
    number or list) yields ``""`` instead of raising — this runs inside
    ``_load()`` during ``start()``, so an exception here would abort gateway
    startup entirely.

    Deliberately does NOT require the path to live under the data home: an
    absolute ``workspaces.<name>.dir`` is a legitimate configuration, and
    clearing those would break working kill switches.

    BLOCKING: performs no filesystem I/O itself, but ``is_sensitive_path``
    resolves realpaths, which can block on an unavailable network mount.
    ``start()`` therefore runs the whole load+repair phase in an executor.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    path = raw.strip()
    try:
        legacy = legacy_home()
        current = config_dir()
        candidate = Path(path).expanduser()
        # Lexical normalization only — never touch the filesystem here.
        norm_candidate = Path(os.path.normpath(str(candidate)))
        norm_legacy = Path(os.path.normpath(str(legacy)))
        norm_current = Path(os.path.normpath(str(current)))
        if norm_candidate.is_relative_to(norm_current):
            # Already live under the current home (including a nested
            # KIROCREW_HOME inside the legacy root) — nothing to re-home.
            pass
        elif norm_current != norm_legacy and norm_candidate.is_relative_to(norm_legacy):
            # Re-home ONLY when the legacy directory the sentinel lives in is
            # gone. A path under ``~/.kirocrew`` is not necessarily a migration
            # casualty: ``workspaces.<name>.dir`` may legitimately be configured
            # as an absolute path inside that tree (and the legacy root can
            # survive the migration as debris, which `kirocrew doctor` reports).
            # Rewriting a still-existing directory's sentinel would move a
            # WORKING kill switch outside its configured workspace and persist
            # that. The migration deletes the tree it moved, so "parent no
            # longer exists" is what distinguishes a stranded path from a live
            # one. A dead path stays dead either way, so the existence probe
            # only ever prevents damage.
            if norm_candidate.parent.exists():
                logger.debug(
                    "AutoNudge: keeping legacy-rooted sentinel %s — its directory "
                    "still exists, so it is a live configured path, not a "
                    "migration leftover",
                    path,
                )
            else:
                rehomed = norm_current / norm_candidate.relative_to(norm_legacy)
                logger.info(
                    "AutoNudge: re-homed stop sentinel from legacy data home: %s → %s",
                    path,
                    rehomed,
                )
                path = str(rehomed)
    except Exception:  # noqa: BLE001 - a repair failure must never block startup
        logger.warning("AutoNudge: could not re-home sentinel %r", raw, exc_info=True)
    try:
        sensitive = is_sensitive_path(path)
    except Exception:  # noqa: BLE001 - fail closed: unvalidated ⇒ untrusted
        logger.warning(
            "AutoNudge: sensitivity re-check failed for %r — dropping the sentinel",
            path,
            exc_info=True,
        )
        return ""
    if sensitive:
        logger.warning(
            "AutoNudge: dropping stop sentinel %r — path is now sensitive; "
            "the loop will be deactivated rather than left unstoppable by file",
            path,
        )
        return ""
    return path


# Module-level singleton so hooks in chat.py / messaging.py can notify the
# service without needing a reference to the gateway. Set by AutoNudgeService
# on start(); cleared on stop().
_INSTANCE: "AutoNudgeService | None" = None
_MAINTENANCE_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}
_MUTATION_LOCK_OWNERS: dict[asyncio.Lock, asyncio.Task[Any]] = {}


def get_instance() -> "AutoNudgeService | None":
    return _INSTANCE


def release_approval_hold_for(slot_key: str | None, *, why: str) -> None:
    """End *slot_key*'s approval hold, if its loop has one. Best-effort.

    The one call the approval paths make once a person answers a prompt. It
    schedules ``AutoNudgeService.release_approval_hold`` (which awaits its own
    durable write) and returns at once. A monitoring convenience must never change
    how that answer is applied, so a missing service, an empty key or a fault here
    is swallowed.
    """
    try:
        svc = _INSTANCE
        if svc is not None and slot_key:
            _timers._schedule_release(svc, slot_key, why=why)
    except Exception:
        logger.debug("autonudge.release_approval_hold failed", exc_info=True)


class AutoNudgeService:
    """Manages reactive per-slot nudge loops with restart-survival."""

    def __init__(
        self,
        base_dir: Path | None = None,
        on_fire: Callable[[NudgeLoop], Awaitable[bool]] | None = None,
        on_monitor_tick: Callable[[NudgeLoop], Awaitable[None]] | None = None,
        collect_judge_evidence: Callable[[NudgeLoop], Awaitable[Any]] | None = None,
        emit_judge_notice: Callable[[NudgeLoop, str], Awaitable[None]] | None = None,
        worker_running: Callable[[str], bool] | None = None,
        worker_closed: Callable[[str], bool] | None = None,
    ) -> None:
        self._base_dir = base_dir or config_dir()
        # The durable store's state and file protocol (see autonudge_service.store).
        self._store = LoopStore(self._base_dir)
        self._on_fire = on_fire
        self._on_monitor_tick = on_monitor_tick
        #: Reads the wake judge's evidence for one loop. Injected rather than called
        #: directly because authorizing a session read needs ``DashboardState``,
        #: which this service does not hold -- the same reason ``on_fire`` and
        #: ``owner_session_id`` are closures the gateway supplies. ``None`` means this
        #: build collects nothing, so every judge verdict is a fail-open FALLBACK and
        #: the tick behaves exactly as it does today.
        self._collect_judge_evidence = collect_judge_evidence
        #: Writes ONE transcript notice row on the owning session, so a verdict that
        #: spent no turn is still visible to whoever is reading the tab. Injected for
        #: the same reason the reader above is: appending a row needs
        #: ``DashboardState``. ``None`` means this build renders no notice, which
        #: costs the verdict nothing -- it is already on the loop record and in the
        #: decisions log.
        self._emit_judge_notice = emit_judge_notice
        #: ``session_key -> that slot has a turn in flight``. Injected because the
        #: slot table is the dashboard's, and this service is constructed with
        #: callbacks rather than a handle on it. Only the work-ledger probe reads
        #: it, to answer the "not running" half of the staleness conjunction; when
        #: it is absent every worker reads as idle, which can only make that probe
        #: louder, never quieter. See :mod:`kiro_crew.probes.work_ledger`.
        self._worker_running_resolver = worker_running
        #: ``session_key -> that slot is GONE``. Injected for the same reason the
        #: liveness resolver above is, and read by the same probe -- but it answers a
        #: different question: a closed worker is stale AT ONCE, where an idle one waits
        #: out the staleness window. Absent means "not closed", which keeps the window.
        self._worker_closed_resolver = worker_closed
        self._loops: dict[str, NudgeLoop] = {}
        self._timers: dict[str, asyncio.Task] = {}
        # Loop ids whose re-arm was requested while their fire window was open.
        # Applied when the window closes (see _timer): a dashboard turn can
        # complete while the firing task is still persisting, and honouring the
        # hook immediately would cancel that task mid-persist.
        self._rearm_pending: set[str] = set()
        #: Loop ids whose PULL-FORWARD was refused because the loop was mid-fire, so the
        #: deferred re-arm must run at delay zero rather than toward the loop's own
        #: deadline. A plain ``_rearm_pending`` entry re-arms from the deadline, which for
        #: a conductor patrolling on an hours-long cadence would turn a worker's report
        #: into an hours-long wait -- exactly the delay the crew-log wake removes. Kept
        #: BESIDE that set rather than replacing it, because the two say different things
        #: about the same window ("resume the countdown" and "run now"), and released at
        #: the one site that applies it (``_run_fire_cycle``'s tail) plus the removal
        #: path, so a claim cannot outlive its loop.
        self._pulled_forward: set[str] = set()
        #: Loop ids whose ARMED timer was set by a worker's push and has not started.
        #: Moved to ``_pushed_running`` when that tick begins, and dropped by every
        #: other arm, so it always describes the timer actually armed. A push landing
        #: while it is set buys nothing new: the armed tick has not read the ledger yet.
        self._pushed_ticks: set[str] = set()
        #: Loop ids whose RUNNING tick was armed by a worker's push. Such a tick goes
        #: through the probe gate rather than spending the post-wake follow-up (the
        #: free follow-up belongs to the loop's own cadence), and a quiet answer keeps
        #: the loop's earlier deadline rather than pushing it out. Reset at every tick.
        self._pushed_running: set[str] = set()
        #: ``loop id -> failure streak`` a work-ledger wake was delivered at, so the
        #: gate can retry that wake if its own turn fails; see
        #: ``gate._spend_ledger_wake_retry``. Released with the loop.
        self._ledger_wake_streak: dict[str, int] = {}
        #: ``loop id -> ledger fingerprint`` read by the running tick's person-wait
        #: check ("" when not every open item waits on a person). The delivered turn
        #: copies it to ``ledger_seen_fp``; see ``_holds_for_person_wait``.
        self._ledger_tick_fp: dict[str, str] = {}
        #: ``loop id -> item id -> wall-clock times`` of the pull-forwards that item
        #: bought its conductor in the last hour, and the ``(loop id, item id)`` pairs
        #: whose cap has already been logged in the current window. Read and written by
        #: ``conductor_wake`` on the event loop; released with the loop.
        self._pull_forward_counts: dict[str, dict[str, list[float]]] = {}
        self._pull_forward_capped: set[tuple[str, str]] = set()
        # Loop ids whose CURRENT tick observed a wake but has not yet had its fire
        # confirmed. Transient on purpose: it is a claim about a turn in flight,
        # so a restart must forget it rather than charge a turn that never ran.
        #: A MAP, not a set, and the value is the kernel's own wake text: which
        #: items moved and how, as ``irq`` composed it from the probe's briefs.
        #: A GATED loop's fire path builds its prompt from the loop's own message
        #: and carries no envelope, so the verdict was the only holder of that text
        #: and it went out of scope at the gate -- which is why a delivered
        #: work-ledger wake said only that a cycle had happened.
        #:
        #: Carried ON the claim rather than in a second table beside it, because
        #: the two have one lifetime: a claim released without delivering must not
        #: leave text behind for whatever fire comes next, and this file already
        #: records what two claim tables with separate release points cost. Every
        #: site that releases the claim therefore releases the text by doing so.
        #:
        #: The text is worker-attributed prose about the ledger, so the in-memory
        #: lifetime is a boundary and not only a restart choice: the one place it
        #: is entitled to appear is the conductor's own turn, and writing it beside
        #: the watch state would put it outside the store's identity-gated read
        #: path -- exactly what ``ledger_wake.wake_brief`` keeps out of there.
        self._pending_monitor_wake: dict[str, str] = {}
        #: A quiet-streak floor tick that has decided to deliver but not yet
        #: delivered. Same shape and same reason as the wake claim above: the charge
        #: belongs at the single point delivery is confirmed, never at the decision.
        self._pending_floor_tick: set[str] = set()
        #: Consecutive indeterminate re-probes of a delivered terminal, per loop.
        #: In memory on purpose: it only decides when the settlement-only branch
        #: hands the tick back to ordinary observation, and the delivered marker
        #: survives that hand-off, so a restart resetting it buys a few more quiet
        #: re-probes and never a second terminal turn.
        self._terminal_reprobe_unknowns: dict[str, int] = {}
        # Loop ids whose timer task is CURRENTLY inside its ``_on_fire`` await.
        # ``update()`` must not cancel such a timer: for channel-bound loops the
        # fire callback runs the unattended turn INLINE, so cancelling it kills
        # the in-flight turn and loses its transcript and cycle bookkeeping.
        self._firing: set[str] = set()
        # Loop ids whose delivered-terminal ``holds`` snapshot is being written.
        # A manual fire in this window must refuse rather than arm a timer that
        # publishing the staged stop immediately retires.
        self._terminal_settlement_writes: set[str] = set()
        # Loop ids owned by an administrative cleanup. Public mutations on the
        # same firing loop must not wait for the maintenance mutex: the cleanup
        # is waiting for that timer to finish, so waiting would invert the lock.
        # They instead observe a missing/no-op mutation while cleanup retains
        # the durable row until the dependent worker has been archived.
        self._maintenance_quiescing: set[str] = set()
        self._maintenance_quiesce_events: dict[str, asyncio.Event] = {}
        # Set by _load() when persisted state is repaired in memory so start()
        # flushes the correction before any loop can re-arm.
        self._store_dirty = False
        # Consecutive non-delivery count per loop (drives escalating re-arm
        # backoff + once-per-streak failure logging). Not persisted; resets on
        # a delivered fire, on removal, and on restart.
        self._rearm_fail_count: dict[str, int] = {}
        # Which start-failure streak value each loop has already paid a deferral
        # for, so one wake per failure is deferred and the next one fires. Without
        # it the streak -- which only grows on a DELIVERED cycle -- freezes below
        # the stand-down threshold and the loop polls at the backoff interval for
        # good. Not persisted: a restart re-arms on a fresh full interval anyway,
        # so the worst a lost entry costs is one extra deferral.
        self._start_failure_deferred: dict[str, int] = {}
        # Closed synchronously at shutdown entry, before any drain awaits. New
        # callers are refused after closure; a lease captured before closure
        # remains valid for one persistence submission in this generation.
        self._accepting_mutations = True
        self._mutation_generation = 0
        # A timer callback admitted before closure keeps using the same
        # generation lease for the writes its own task makes, including the
        # bookkeeping a delivery cancelled by shutdown performs on its way out.
        # A task it spawns sees the lease but is not its owner, so it does not
        # inherit it; ordinary public mutations still receive a
        # single-persistence lease from `_admit_mutation`.
        self._mutation_admission: contextvars.ContextVar[_MutationAdmission | None] = (
            contextvars.ContextVar(
                f"autonudge_mutation_admission_{id(self)}",
                default=None,
            )
        )
        # A remove-class transaction runs in a detached child, but timer
        # cancellation must still recognize the callback task that owns it as
        # self-cancellation. Set only in that child's copied context.
        self._admitted_transaction_owner: contextvars.ContextVar[asyncio.Task[Any] | None] = (
            contextvars.ContextVar(
                f"autonudge_admitted_transaction_owner_{id(self)}",
                default=None,
            )
        )
        # The writes one admitted transaction has started, carried in the same
        # child context. The caller's cancellation hold reads it, so a detached
        # write under the callback's shared lease, which runs in its own context,
        # cannot pass for this transaction's submission.
        self._admitted_transaction_submissions: contextvars.ContextVar[
            _TransactionSubmissions | None
        ] = contextvars.ContextVar(
            f"autonudge_admitted_transaction_submissions_{id(self)}",
            default=None,
        )
        # Strong refs to owned mutation tasks and owner-completion futures:
        # keep detached mutations supervised (no GC, failures logged) and let
        # shutdown map a drained section or transaction back to its timer callback
        # without waiting on that callback's whole lifetime. Discarded on completion.
        self._inflight_adds: set[asyncio.Future[Any]] = set()
        # Timer tasks past their sleep. Shutdown cancels them like dormant timers,
        # including one whose own loop was removed mid-callback and so has left
        # ``_timers``; only a callback identified by a registered drain-owner
        # completion in ``_inflight_adds`` is drained instead.
        self._running_callbacks: set[asyncio.Task[Any]] = set()
        # Executor-backed writes outlive cancellation of their asyncio waiter.
        # Track the raw futures separately so shutdown can prove the durable
        # boundary before the store path is released.
        self._inflight_persistence: set[asyncio.Future[Any]] = set()
        # Structured replacements whose prior row must keep its protected trust
        # until the caller completes a second durable authorization step. The
        # monitor snapshot and the protected trust record are separate files, so
        # the authorizer either commits this entry after activating the new grant
        # or rolls the monitor snapshot back to it.
        self._deferred_monitor_replacements: dict[str, tuple[NudgeLoop | None, NudgeLoop, bool]] = (
            {}
        )
        # Runtime turn-start evidence for the narrow window between a channel
        # accepting a claimed wake and the controller persisting DISPATCHED.
        # One monitor can own only one claim, so the loop id maps directly to
        # its accepted fingerprint. Durable delivery state remains authoritative
        # after the dispatcher returns or the process restarts.
        self._accepted_monitor_turns: dict[str, str] = {}
        # Runtime authority for a completion emitted by the exact task whose
        # dashboard/channel turn crossed the provider-entry boundary. The owner
        # task stays out of the mutation drain, so a stop never waits on its
        # turn; the entry outlives shutdown until that task ends, and the write
        # its completion makes is drained like any other admitted write.
        self._accepted_monitor_completion_authorities: dict[
            tuple[str, str], tuple[asyncio.Task[Any], _MutationAdmission]
        ] = {}
        # The periodic reconciler task (see _reconcile_forever). Owned by
        # start()/stop(); None while the service is not running.
        self._reconciler: asyncio.Task | None = None
        # Loop ids the previous reconciler pass found eligible-and-unarmed.
        # A rescue requires membership here AND a second eligible observation
        # (see _reconcile_once); notify_user_input clears a slot's candidacy,
        # so any sign of life restarts the two-pass clock. Not persisted --
        # after a restart, start() re-arms every active loop anyway.
        self._reconcile_candidates: set[str] = set()
        # One lock per structured monitor orders its durable patch and any
        # credential continuation as one transaction. The map lookup is protected
        # by ``_lock``; callers then acquire this lock before re-entering ``_lock``.
        # Registered users keep an entry stable across a concurrent row removal.
        self._monitor_mutation_locks: dict[str, asyncio.Lock] = {}
        self._monitor_mutation_lock_users: dict[str, int] = {}
        self._observers: list[Callable[[str, NudgeLoop | None], None]] = []
        self._lock = asyncio.Lock()

    @property
    def accepting_mutations(self) -> bool:
        """Whether this service generation admits a new public mutation."""
        return self._accepting_mutations

    @property
    def _path(self) -> Path:
        """The store file's path, which callers and tests read off the service.

        Read from the composed store on every access rather than copied at
        construction, so the loader reads the file the store writes even after
        the store's path is reassigned.
        """
        return self._store.path

    # ── Persistence ──

    def _admit_mutation(
        self,
        *,
        allow_multiple_persistence: bool = False,
    ) -> _MutationAdmission:
        """Capture mutation authority before the caller reaches its first await."""
        current = _current_task_or_none()
        inherited = self._mutation_admission.get()
        if inherited is not None and inherited.owner_task is current:
            return inherited
        if not self._accepting_mutations:
            raise NudgeAdmissionRefused(
                SERVICE_SHUTTING_DOWN_MESSAGE,
                reason=NudgeAdmissionReason.SERVICE_SHUTTING_DOWN,
            )
        return _MutationAdmission(
            self._mutation_generation,
            owner_task=current,
            allow_multiple_persistence=allow_multiple_persistence,
        )

    def _effective_admission(
        self,
        admission: _MutationAdmission | None,
    ) -> _MutationAdmission | None:
        """The explicit lease, else the one the current timer task inherited."""
        if admission is not None:
            return admission
        inherited = self._mutation_admission.get()
        if inherited is None or inherited.owner_task is not _current_task_or_none():
            return None
        return inherited

    def _mutation_allowed(self, admission: _MutationAdmission | None) -> bool:
        effective = self._effective_admission(admission)
        if effective is None:
            return self._accepting_mutations
        return effective.generation == self._mutation_generation

    def _start_persistence(
        self,
        payload: dict,
        *,
        admission: _MutationAdmission | None = None,
    ) -> asyncio.Future[None]:
        """Start one off-loop write under live or already-admitted authority.

        The single submission point for an executor-backed store write, so the
        raw future is registered where ``shutdown()`` drains it even when every
        asyncio waiter of the write was cancelled.
        """
        effective = self._effective_admission(admission)
        if not self._mutation_allowed(effective):
            raise NudgeAdmissionRefused(
                SERVICE_SHUTTING_DOWN_MESSAGE,
                reason=NudgeAdmissionReason.SERVICE_SHUTTING_DOWN,
            )
        if (
            effective is not None
            and effective.persistence_submitted
            and not effective.allow_multiple_persistence
        ):
            raise NudgeAdmissionRefused(
                "mutation persistence lease is already consumed",
                reason=NudgeAdmissionReason.PERSISTENCE_LEASE_CONSUMED,
            )
        future = asyncio.get_running_loop().run_in_executor(
            None,
            self._write_state,
            payload,
        )
        if effective is not None:
            effective.persistence_submitted = True
        submissions = self._admitted_transaction_submissions.get()
        if submissions is not None:
            submissions.count += 1
        self._inflight_persistence.add(future)
        future.add_done_callback(self._inflight_persistence.discard)
        return future

    @classmethod
    async def load_for_maintenance(cls, base_dir: Path | None = None) -> "AutoNudgeService":
        """Load the durable store without arming timers or publishing a singleton.

        Administrative cleanup still needs to see old loops when AutoNudge is
        disabled.  Reusing the service's locked parser keeps that recovery on
        the same schema and persistence protocol as normal startup, while the
        absence of ``start()`` guarantees that reading the store cannot fire a
        loop as a side effect.
        """
        service = cls(base_dir=base_dir)
        await asyncio.get_running_loop().run_in_executor(None, service._load)
        return service

    def _serialize_state(self) -> dict:
        """Snapshot the store payload ON THE CALLER'S THREAD.

        Loop state is mutated only under the service lock on the event loop, so
        the serialization must happen there too — a worker thread iterating
        ``self._loops`` concurrently with a mutation would race. The returned
        payload is immutable-by-convention and safe to hand to an executor.
        Loops are built from ``self._loops`` alone, so a row ``_load`` declined is
        absent from ``loops``. An unusable addressing field is NOT dropped: it is held
        in the ``autonudge.quarantine.json`` sidecar, which this payload does not carry
        and this write does not touch, so the entry the operator was warned about is
        still there to repair. It is kept out of ``loops`` because addressing fields are
        served unscrubbed. A row that could not be PARSED is carried here verbatim from
        ``self._store.unparsed_rows``: it arms nothing, but any cause that flags the store dirty would
        otherwise make this write delete a neighbour nobody chose to remove.

        A whole-store refusal covers three causes, and ``_write_state`` honours all of
        them by refusing rather than persisting a payload that is empty because nothing
        could be vetted: a credential policy this host declares but cannot compose, a
        ``loops`` value that is present but not a list, and a quarantine sidecar that
        could not be read. An unusable addressing field is quarantined per row instead,
        so it is never refused wholesale.

        DELIBERATE, and the alternative named: held rows could instead be re-emitted into
        this payload's own ``loops`` and never armed, which would delete the sidecar and its
        whole ordering apparatus -- and with it the cross-process race its lock now covers.
        Not taken here because a build PREDATING the ``quarantined`` key (see
        ``_QUARANTINE_FILE``) reads this payload back with no held-row concept, so an
        embedded row is an ordinary loop to it and it ARMS the addressing field this
        quarantine refuses. Serving is NOT the reason: a held row is absent from ``_loops``,
        so no client frame can carry it, exactly as ``self._store.unparsed_rows`` rides this list
        unserved. Revisiting it means accepting that downgrade-arming risk.
        """
        payload: dict[str, Any] = {
            "version": _STORE_VERSION,
            "loops": self._serialized_loops(),
        }
        return payload

    def _serialized_loops(
        self,
        *,
        replace: dict[str, NudgeLoop] | None = None,
        skip: set[str] | None = None,
        extra: list[NudgeLoop] | None = None,
    ) -> list[Any]:
        """Every store payload's ``loops`` list: the store's rows around the live loops.

        Each live loop goes through ``self._serialize_loop``, so a patch on the service
        shapes every stored row, as it did when this builder lived on the service.
        """
        return self._store.serialized_loops(
            self._loops, self._serialize_loop, replace=replace, skip=skip, extra=extra
        )

    def _write_state(self, payload: dict) -> None:
        """Commit one payload through the store; every service write goes through here."""
        self._store.write_state(payload)

    def _save(self) -> None:
        self._write_state(self._serialize_state())

    # ── Observer hook (for WS broadcasts) ──

    def subscribe(self, cb: Callable[[str, NudgeLoop | None], None]) -> None:
        self._observers.append(cb)

    def _emit(self, event: str, loop: NudgeLoop | None) -> None:
        # A detached admitted child carries its owner in ``_admitted_transaction_owner``
        # for its own timer cancellations only, its submission token in
        # ``_admitted_transaction_submissions`` for its own writes only, and a monitor
        # update's post-commit handle for its own credential continuation only. An
        # observer runs in the emitter's context and a task it creates copies that
        # context, so all three are cleared for the observers: their follow-up must not
        # pass for the owner's callback, a write it starts must not count as the
        # transaction's own, and nothing it starts may reach the update's continuation.
        owner_token = self._admitted_transaction_owner.set(None)
        submissions_token = self._admitted_transaction_submissions.set(None)
        post_commit_token = _monitor_records._MONITOR_UPDATE_POST_COMMIT.set(None)
        try:
            for cb in self._observers:
                try:
                    cb(event, loop)
                except Exception:
                    logger.warning("AutoNudge observer failed", exc_info=True)
        finally:
            _monitor_records._MONITOR_UPDATE_POST_COMMIT.reset(post_commit_token)
            self._admitted_transaction_submissions.reset(submissions_token)
            self._admitted_transaction_owner.reset(owner_token)

    # ── Lifecycle ──

    async def start(self) -> None:
        if not enabled():
            logger.info("AutoNudge disabled (KIROCREW_AUTONUDGE is 0/false/no)")
            return
        # A restart after shutdown() opens a NEW generation, so a lease captured
        # before the previous closure cannot write into this one.
        if not self._accepting_mutations:
            self._mutation_generation += 1
        self._accepting_mutations = True
        # This lock spans load, repair, timer arming and singleton publication.
        # Disabled-mode maintenance that got here first finishes its whole
        # read/modify/write transaction before startup loads; maintenance that
        # arrives later sees this live service rather than a stale private copy.
        async with _maintenance_lock(self._base_dir):
            # Load + repair OFF the event loop: the locked read is file I/O and
            # repair_sentinel_path's sensitivity check resolves realpaths.
            await asyncio.get_running_loop().run_in_executor(None, self._load)
            if self._store_dirty:
                try:
                    await self._persist_locked()
                    self._store_dirty = False
                except Exception:  # noqa: BLE001 - in-memory repair still applies
                    logger.warning(
                        "AutoNudge: could not persist loaded-state repair", exc_info=True
                    )
            for loop in self._loops.values():
                if loop.active:
                    # A work-ledger loop resumes at delay ZERO instead of toward its
                    # persisted deadline, and only this kind does. The crew-log wake is a
                    # push off an in-process queue, so every push in flight when this
                    # process died is gone -- the entry is durable, the notification was
                    # not. For a pull-request watch that costs nothing (the next poll
                    # reads the same pull request), but a conductor's whole point in
                    # setting an hours-long cadence is that the push carries the news, so
                    # a restart would hide a worker's report for those hours. One tick
                    # per such loop at boot replays them all, because the probe reads the
                    # ledger ITSELF: whatever landed while the process was down is in the
                    # fold, and a tick that finds nothing actionable answers quiet and
                    # spends no turn. That is also why there is no replay log -- the
                    # store is the record, and re-reading it is the replay.
                    if self._observes_work_ledger(loop):
                        # Marked as a pushed tick for the same reason a worker's push
                        # is: the gate's post-wake follow-up allowance skips the
                        # probe, and a replay that took it would spend an unattended
                        # turn without reading the ledger -- the opposite of why the
                        # replay exists. A pushed tick always goes through the probe.
                        # Marked AFTER arming: _arm_timer clears the mark it replaces.
                        self._arm_timer(loop, delay=0.0)
                        self._pushed_ticks.add(loop.id)
                    else:
                        self._arm_from_deadline(loop)
            global _INSTANCE
            _INSTANCE = self
        # The crew-log bus subscriptions that pull a work-ledger loop forward when its
        # board's ``work`` fold advances: one keyed subscription per watched board,
        # following this service's loop table. Installed HERE because this service owns
        # the loops it fires -- the bus's rule is that a consumer subscribes where its
        # state exists. Function-local import to keep ``conductor_wake`` off this
        # module's import graph: it imports ``autonudge`` back (for ``get_instance``),
        # and a module-level import here would close that cycle.
        from kiro_crew import conductor_wake

        conductor_wake.install(self)
        # The reconciler is the timer-driven backstop for a loop stranded
        # active-but-unarmed (see _reconcile_forever). Spawned outside the
        # maintenance lock: it takes no locks of its own and its first pass is
        # a full interval away, so nothing it reads can race the load above.
        # ``done()`` alone is not enough: a task whose event loop closed
        # without cancellation is not done, yet can never run again -- the
        # same singleton-outlives-its-loop scenario _cancel_timer documents.
        # Without the closed-loop clause a start() under a fresh loop would
        # silently decline to spawn and run with no backstop.
        if (
            self._reconciler is None
            or self._reconciler.done()
            or self._reconciler.get_loop().is_closed()
        ):
            self._reconciler = asyncio.create_task(self._reconcile_forever())
        logger.info("AutoNudge started")

    def stop(self, *, preserve_admitted: bool = False) -> None:
        # Retire the reconciler first so a pass cannot re-arm a timer this
        # method is about to cancel. Same closed-loop guard as _cancel_timer:
        # stop() runs from synchronous shutdown paths where the task's loop
        # may already be gone, and cancelling through a closed loop raises.
        t = self._reconciler
        self._reconciler = None
        if t is not None and not t.done() and not t.get_loop().is_closed():
            t.cancel()
        # Through _cancel_timer, not a bare t.cancel() loop: shutdown is the likeliest
        # moment for a timer's loop to be closing already, and one cancellation policy
        # means this path inherits both of its guards instead of restating neither.
        # A timer identified by a drain-owner completion is inside bookkeeping
        # or a detached admitted transaction. Async shutdown detaches and drains
        # that boundary; synchronous stop keeps its historical cancel-all behavior.
        drained_callback_owners = _timers._drain_registered_owners(self)
        for loop_id in list(self._timers):
            timer = self._timers.get(loop_id)
            if preserve_admitted and timer in drained_callback_owners:
                self._timers.pop(loop_id, None)
                continue
            self._cancel_timer(loop_id)
        self._timers.clear()
        if preserve_admitted:
            return
        self._reconcile_candidates.clear()
        self._terminal_reprobe_unknowns.clear()
        self._accepted_monitor_turns.clear()
        self._accepted_monitor_completion_authorities.clear()
        self._maintenance_quiescing.clear()
        self._maintenance_quiesce_events.clear()
        self._unpublish()

    def _unpublish(self) -> None:
        """Withdraw the singleton, so no late caller reaches this service."""
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None
            # The crew-log subscriptions and the boards they filled are bounded by this
            # service's loop table; with the table gone they have nothing to fire, so
            # they are disposed with it. A later start() joins them again.
            from kiro_crew import conductor_wake

            conductor_wake.dispose_all()

    async def shutdown(self) -> None:
        """Close admission, cancel every timer outside drained bookkeeping, drain the rest.

        A running callback is cancelled exactly like a dormant timer, so a gateway stop
        never waits on a delivery, a probe or a judge call. The exception is a callback
        owning a registered bookkeeping section or detached admitted transaction: that
        boundary is drained with the other owners instead, without extending the drain
        through the callback's remaining lifetime. A terminal settlement's provider
        re-probe runs outside every registered section, so it is cancelled with its
        callback: the delivered marker persisted before it stays owed, and the next
        start settles it without another turn.
        """
        self._accepting_mutations = False
        current = _current_task_or_none()
        armed = {task for task in self._timers.values() if isinstance(task, asyncio.Task)}
        all_controls = {
            task
            for task in (self._reconciler, *armed, *self._running_callbacks)
            if isinstance(task, asyncio.Task)
            and task is not current
            and not task.done()
            and not task.get_loop().is_closed()
        }
        # A callback whose own loop was removed mid-callback has left the timer table,
        # so ``stop()`` cannot reach it; it is cancelled here on the same terms. A
        # registered callback is represented by its drain-owner completion future
        # instead of by the task's whole remaining lifetime.
        drained_callback_owners = _timers._drain_registered_owners(self)
        controls = all_controls - drained_callback_owners
        unarmed_callbacks = {task for task in controls - armed if task in self._running_callbacks}
        self.stop(preserve_admitted=True)
        for task in unarmed_callbacks:
            task.cancel()
        drain = _DurabilityDrain()
        settled = await drain.settle(*controls)

        # Public mutations and timer callbacks share the same drain registry.
        # Re-snapshot until no task/future remains: an admitted section may start
        # its raw executor write after shutdown takes the first snapshot. The
        # loop ends early only when the drain's post-cancellation window has
        # closed -- re-snapshotting the same wedged write would otherwise grant
        # it a fresh window on every pass.
        pending: set[asyncio.Future[Any]] = set()
        while settled:
            mutations = {
                task for task in self._inflight_adds if task is not current and not task.done()
            }
            writes = {future for future in self._inflight_persistence if not future.done()}
            pending = {*mutations, *writes}
            if not pending:
                break
            settled = await drain.settle(*pending)

        if not settled:
            # Not a lost write: the executor thread keeps running and the store
            # write is atomic, so the file holds the new state or the previous
            # complete one. What is lost is the wait, which the supervisor's kill
            # would have ended less gracefully.
            logger.warning(
                "AutoNudge shutdown stopped waiting on %d owned task(s)/write(s) still "
                "pending %.1fs after cancellation; each store write is atomic",
                sum(1 for future in {*controls, *pending} if not future.done()),
                _DRAIN_CANCEL_GRACE_SECS,
            )

        # Filter only settled entries in place. Pending admitted tasks must stay
        # registered for their done callbacks and any work still reading this set.
        self._inflight_adds.difference_update({task for task in self._inflight_adds if task.done()})
        self._inflight_persistence.difference_update(
            {future for future in self._inflight_persistence if future.done()}
        )
        if settled:
            # Callback bookkeeping may release the runtime claim sets only after
            # every admitted owner and write has settled. Finalize here, before
            # callers close the stores and sessions those callbacks use, but keep
            # the closed service published until the gateway has closed its
            # dashboard. Slot retirement in that window must reach this service
            # and fail loudly, not mistake an absent singleton for no loop.
            # Accepted-turn authorities stay: each ends with its owner task, which
            # a stop does not wait on, so a completion that turn emits while the
            # gateway closes sessions still persists under its captured lease.
            self._reconcile_candidates.clear()
            self._accepted_monitor_turns.clear()
            self._maintenance_quiescing.clear()
            self._maintenance_quiesce_events.clear()
        # Work the drain stopped waiting on may still read the claim sets, so an
        # unsettled drain leaves those sets intact. Both outcomes keep the
        # singleton and its crew-log subscriptions published; gateway teardown
        # withdraws them only after the dashboard runner has stopped serving.
        if drain.interrupted:
            raise asyncio.CancelledError()

    async def _persist_locked(
        self,
        *,
        admission: _MutationAdmission | None = None,
    ) -> None:
        """Snapshot under the service lock and write on a worker thread.

        The SINGLE async persistence path for post-arm mutations. Two properties
        matter and both were violated before:

        * **Serialization.** Every writer must snapshot while holding
          ``_lock``; otherwise a writer that snapshots, releases, and then
          writes can land a STALE payload on top of a newer one (e.g. a
          concurrent ``update()`` overwriting the post-fire ``cycle_count`` /
          ``active`` bookkeeping, which then resurrects obsolete state after a
          restart).
        * **Non-blocking.** ``_write_state`` fsyncs, so it must never run on the
          event loop.
        """
        async with self._lock:
            payload = self._serialize_state()
            await asyncio.shield(self._start_persistence(payload, admission=admission))

    def get_by_id(self, loop_id: str) -> NudgeLoop | None:
        """The loop with this id, or ``None``.

        Public because ``autonudge_authz`` needs it twice: to resolve an opaque
        ``loop_id`` to a slot key when deciding whether a banner is supported there,
        and to read the CURRENT message when deciding whether a submitted one is
        merely the scrubbed projection it served. An accessor rather than reaching
        into ``_loops`` from another module, matching ``get_by_slot``/``list_all``.
        Returns the LIVE object, not a copy; callers here only read from it.
        """
        return self._loops.get(loop_id)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return self._find_by_slot(slot_key)

    def list_all(self) -> list[NudgeLoop]:
        return list(self._loops.values())

    def pending_wake_briefs(self, loop_id: str) -> str:
        """The wake text owed to *loop_id*'s next delivered cycle, or ``""``.

        An accessor rather than a reach into ``_pending_monitor_wake``, matching
        ``get_by_id`` / ``get_by_slot``: the fire path lives in the gateway, which is
        another module, and the one thing it needs there is a read.

        A READ, not a take. The text stays owed until delivery is confirmed, for the
        same reason the claim carrying it does -- a fire the slot refuses delivered
        nothing, and taking the text here would leave the retry with a turn that says
        only that a cycle happened.
        """
        return self._pending_monitor_wake.get(loop_id, "")

    async def _write_monitor_snapshot_locked(
        self,
        payload: dict | None = None,
        *,
        admission: _MutationAdmission | None = None,
    ) -> None:
        """Persist a monitor transition without releasing ``_lock`` mid-write."""
        if payload is None:
            payload = self._serialize_state()
        future = self._start_persistence(payload, admission=admission)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                # Executor writes cannot be cancelled. Absorb every
                # cancellation until the write settles so the caller's lock
                # scope cannot release around an older snapshot.
                cancelled = True
        future.result()
        if cancelled:
            # Propagate cancellation only after the executor result has been
            # observed while the caller still owns the lock.
            raise asyncio.CancelledError

    def _find_by_slot(self, slot_key: str) -> NudgeLoop | None:
        """The loop bound to *slot_key*, which may be a binding key OR a tab name.

        A channel-born conversation is bound under its channel session key
        (``slack:<ts>``) — the fire path needs that key to route the turn — but
        its dashboard tab knows itself only by slot NAME
        (``slack_<ts>``: the same key folded to the filename charset). The
        turn-lifecycle hooks and the tab's own loop lookups pass that name, so
        matching it here is what keeps one loop addressable from both sides
        instead of invisible from the dashboard.

        Exact match wins; the fold is a fallback, and is computed with the
        dashboard's own normalizer so no second derivation of the name exists.
        """
        for lp in self._loops.values():
            if lp.slot_key == slot_key:
                return lp
        if not slot_key or is_channel_key(slot_key):
            return None
        # Lazy: autonudge is imported BY the dashboard chat layer.
        from kiro_crew.dashboard.state import _normalize_slot_key

        for lp in self._loops.values():
            if is_channel_key(lp.slot_key) and _normalize_slot_key(lp.slot_key) == slot_key:
                return lp
        return None

    def _persist_soon(self) -> None:
        """Schedule a supervised background persist of loop state.

        For sync callers (the turn-lifecycle hooks) that assign a fresh
        deadline and cannot await ``_persist_locked`` themselves. Detached but
        supervised — strong ref in ``_inflight_adds`` plus failure logging —
        so the assignment reaches the store and a restart resumes the
        countdown. A lost write degrades to a fresh full countdown after
        restart, never a premature or dropped fire. A live caller without an
        inherited lease captures one single-write admission before the task is
        created, so closure cannot overtake queued lifecycle persistence. After
        shutdown closes admission the persist is skipped unless the caller is an
        admitted timer callback, whose lease the detached write inherits
        explicitly. A task created inside an admitted transaction clears only
        that transaction's
        submission token, so its write cannot count as the child's own.
        """
        admission = self._effective_admission(None)
        if admission is None:
            if not self._accepting_mutations:
                return
            admission = self._admit_mutation()
        submissions_token = self._admitted_transaction_submissions.set(None)
        try:
            task = asyncio.create_task(self._persist_locked(admission=admission))
        finally:
            self._admitted_transaction_submissions.reset(submissions_token)
        self._inflight_adds.add(task)

        def _finish(t: "asyncio.Task[None]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("AutoNudge: deadline persist failed", exc_info=t.exception())

        task.add_done_callback(_finish)

    def _worker_running(self, session_key: str) -> bool:
        """Whether *session_key*'s slot has a turn in flight.

        False when no resolver was injected, which is the direction that cannot
        lose a signal: the work-ledger probe uses this for the "not running" half
        of the staleness conjunction, so an unknown liveness produces a stall wake
        the conductor may not have needed (one turn) rather than silence about a
        worker that stopped without reporting (the task). A resolver that raises is
        treated the same way -- a slot-table read must not kill a tick.
        """
        resolver = self._worker_running_resolver
        if resolver is None or not session_key:
            return False
        try:
            return bool(resolver(session_key))
        except Exception:  # pragma: no cover - a liveness read must not fail a tick
            logger.debug("AutoNudge: worker liveness read failed for %s", session_key)
            return False

    def _worker_looping(self, session_key: str) -> bool:
        """Whether *session_key*'s own session holds an active loop that will wake it.

        Answered from this service's own loop table, so no resolver is injected: the
        logic is ``ledger_wake.worker_looping``, which the dashboard's ``stale`` flag
        reads too, and an unreadable answer is False -- the direction that cannot
        suppress a stall wake.
        """
        from kiro_crew import ledger_wake

        return ledger_wake.worker_looping(self, session_key)

    def _observes_work_ledger(self, loop: NudgeLoop) -> bool:
        """Whether *loop*'s monitor is a work-ledger watch THIS gateway may arm.

        A predicate rather than an inline comparison because the answer decides a
        STARTUP behaviour (resume now, not at the deadline) and the reason is specific to
        this kind: its news arrives by an in-process push that a restart loses, where
        every other kind's arrives by the probe's own poll.

        The VERSION is part of the question, not a separate guard, and leaving it out was
        a reachable hole rather than a theoretical one. ``_arm_from_deadline`` refuses a
        monitor record whose ``version`` this gateway does not implement, because such a
        record belongs to a newer gateway and running the loop would deliver an unattended
        turn under a policy nothing here can interpret. A work-ledger watch is a
        ``gate=True`` prompt loop, so ``is_structured_monitor_loop`` is False and ``_load``
        leaves such a row ACTIVE -- exactly the shape a zero-delay arm here would have
        bypassed that refusal for. Answering False sends the row to
        ``_arm_from_deadline``, which refuses it and logs why, so the refusal lives in one
        place rather than being restated here.

        The kind is compared against ``probes.WORK_LEDGER``, read off the module-level
        ``probes`` binding this facade already re-exports.
        """
        monitor = getattr(loop, "monitor", None)
        if monitor is None:
            return False
        if getattr(monitor, "version", None) != MONITOR_STATE_VERSION:
            return False
        return str(getattr(monitor, "kind", "")) == probes.WORK_LEDGER

    def _worker_closed(self, session_key: str) -> bool:
        """Whether *session_key*'s slot is gone.

        False when no resolver was injected, and here that is the direction that cannot
        INVENT a signal: this input removes the staleness window, so answering True
        without a slot table would flag every freshly created item. A resolver that
        raises is treated the same way, for the reason the liveness read is -- a
        slot-table read must not kill a tick.
        """
        resolver = self._worker_closed_resolver
        if resolver is None or not session_key:
            return False
        try:
            return bool(resolver(session_key))
        except Exception:  # pragma: no cover - a slot read must not fail a tick
            logger.debug("AutoNudge: worker close read failed for %s", session_key)
            return False

    # ── Owner methods ──
    # Each name below IS its owner's function, bound here by name: one
    # definition, reached through the instance, so a patch on the service reaches
    # every caller. test_autonudge_refactor_contract pins each binding.
    # autonudge_service.store
    _serialize_loop = staticmethod(LoopStore.serialize_loop)
    # autonudge_service.maintenance
    maintenance_service = classmethod(_maintenance.maintenance_service)
    _begin_maintenance_quiesce = _maintenance._begin_maintenance_quiesce
    _end_maintenance_quiesce = _maintenance._end_maintenance_quiesce
    _acquire_mutation_lock = _maintenance._acquire_mutation_lock
    deactivate_and_wait = _maintenance.deactivate_and_wait
    _deactivate_and_wait_unserialized = _maintenance._deactivate_and_wait_unserialized
    # autonudge_service.timers
    notify_approval_stalled = _timers.notify_approval_stalled
    release_approval_hold = _timers.release_approval_hold
    notify_cycle_start_failed = _timers.notify_cycle_start_failed
    notify_cycle_failed = _timers.notify_cycle_failed
    notify_cycle_landed = _timers.notify_cycle_landed
    notify_turn_complete = _timers.notify_turn_complete
    notify_user_input = _timers.notify_user_input
    drain_section = _timers.drain_section
    settle_admitted_writes = _timers.settle_admitted_writes
    _cancel_timer = _timers._cancel_timer
    _arm_timer = _timers._arm_timer
    _arm_from_deadline = _timers._arm_from_deadline
    _reconcile_forever = _timers._reconcile_forever
    _reconcile_once = _timers._reconcile_once
    # autonudge_service.gate
    _commit_judge_pr_seen = _gate._commit_judge_pr_seen
    _publish_pr_observation = _gate._publish_pr_observation
    _monitor_tick_is_quiet = _gate._monitor_tick_is_quiet
    _settle_delivered_terminal = _gate._settle_delivered_terminal
    _terminal_still_holds = _gate._terminal_still_holds
    # autonudge_service.judge_tick
    _judge_quiet_streak_floor = _judge_tick._judge_quiet_streak_floor
    _judge_tick_is_quiet = _judge_tick._judge_tick_is_quiet
    _record_judge_verdict = _judge_tick._record_judge_verdict
    _withdraw_judge_suppression = _judge_tick._withdraw_judge_suppression
    _confirm_judge_delivery = _judge_tick._confirm_judge_delivery
    _label_judge_delivery_locked = _judge_tick._label_judge_delivery_locked
    _append_judge_labels = _judge_tick._append_judge_labels
    _persist_judge_state = _judge_tick._persist_judge_state
    # autonudge_service.firing
    _timer = _firing._timer
    _run_timer_callback = _firing._run_timer_callback
    _extend_for_open_ledger = _firing._extend_for_open_ledger
    _holds_for_person_wait = _firing._holds_for_person_wait
    _run_fire_cycle = _firing._run_fire_cycle
    _settle_fire_cycle = _firing._settle_fire_cycle
    _finish_fire_cycle = _firing._finish_fire_cycle
    fire_now = _firing.fire_now
    # autonudge_service.mutations
    add = _mutations.add
    _mint_loop_id = _mutations._mint_loop_id
    _add_locked = _mutations._add_locked
    _add_unserialized = _mutations._add_unserialized
    update = _mutations.update
    _update_locked = _mutations._update_locked
    _update_unserialized = _mutations._update_unserialized
    remove_sync = _mutations.remove_sync
    _revoke_self_arm_for = _mutations._revoke_self_arm_for
    _revoke_self_arm = staticmethod(_mutations._revoke_self_arm)
    remove = _mutations.remove
    remove_by_slot = _mutations.remove_by_slot
    clear_terminal_monitor = _mutations.clear_terminal_monitor
    _remove_unserialized = _mutations._remove_unserialized
    _revoke_provider_credentials_before_removal = staticmethod(
        _mutations._revoke_provider_credentials_before_removal
    )
    _provider_credentials_authorized = staticmethod(_mutations._provider_credentials_authorized)
    _restore_provider_credentials = staticmethod(_mutations._restore_provider_credentials)
    # autonudge_service.monitor_records
    add_monitor = _monitor_records.add_monitor
    _add_monitor_locked = _monitor_records._add_monitor_locked
    commit_monitor_replacement = _monitor_records.commit_monitor_replacement
    rollback_monitor_replacement = _monitor_records.rollback_monitor_replacement
    _monitor_snapshot_with_replacement = _monitor_records._monitor_snapshot_with_replacement
    _apply_staged_monitor = _monitor_records._apply_staged_monitor
    _persist_staged_monitor_locked = _monitor_records._persist_staged_monitor_locked
    apply_monitor_probe = _monitor_records.apply_monitor_probe
    stop_monitor_if_budget_exhausted = _monitor_records.stop_monitor_if_budget_exhausted
    _set_monitor_deadline = _monitor_records._set_monitor_deadline
    stop_monitor = _monitor_records.stop_monitor
    _stop_monitor_admitted = _monitor_records._stop_monitor_admitted
    mark_terminal_notification_delivered = _monitor_records.mark_terminal_notification_delivered
    retire_monitor_for_session_close = _monitor_records.retire_monitor_for_session_close
    restore_monitor_after_failed_session_close = (
        _monitor_records.restore_monitor_after_failed_session_close
    )
    update_monitor = _monitor_records.update_monitor
    _update_monitor_admitted = _monitor_records._update_monitor_admitted
    rollback_monitor_update = _monitor_records.rollback_monitor_update
    mark_monitor_action_in_flight = _monitor_records.mark_monitor_action_in_flight
    record_monitor_turn_completion = _monitor_records.record_monitor_turn_completion
    _record_monitor_turn_completion_admitted = (
        _monitor_records._record_monitor_turn_completion_admitted
    )
    _apply_monitor_budget_stop = _monitor_records._apply_monitor_budget_stop
    _apply_monitor_user_stop = _monitor_records._apply_monitor_user_stop
    _retain_accepted_terminal_completion = _monitor_records._retain_accepted_terminal_completion
    _waits_for_terminal_completion = _monitor_records._waits_for_terminal_completion
    _sync_terminal_completion_timer = _monitor_records._sync_terminal_completion_timer
    record_monitor_dispatch_failure = _monitor_records.record_monitor_dispatch_failure
    monitor_dispatch_is_authorized = _monitor_records.monitor_dispatch_is_authorized
    mark_monitor_turn_accepted = _monitor_records.mark_monitor_turn_accepted
    record_monitor_dispatch_busy = _monitor_records.record_monitor_dispatch_busy
    record_monitor_dispatched = _monitor_records.record_monitor_dispatched
    record_monitor_completion_evidence_unavailable = (
        _monitor_records.record_monitor_completion_evidence_unavailable
    )
    _deactivate_unwired_monitor = _monitor_records._deactivate_unwired_monitor
    # autonudge_service.store_loader
    _load = _store_loader._load


# An owner function annotates ``self`` / ``cls`` as the service so mypy checks its
# body; once it is bound above, that annotation is dropped from the runtime object and
# from every function it wraps, so ``inspect.signature`` and ``typing.get_type_hints``
# read each method exactly as a method defined in this class body would.
for _member in vars(AutoNudgeService).values():
    _function = getattr(_member, "__func__", _member)
    while getattr(_function, "__module__", "").startswith("kiro_crew.autonudge_service."):
        _function.__annotations__.pop("self", None)
        _function.__annotations__.pop("cls", None)
        _function = getattr(_function, "__wrapped__", None)
del _member, _function
