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
tick, the fire cycle, the loop transactions and the structured-monitor transitions.
:class:`AutoNudgeService` is defined here and composes them. It keeps the live loop
registry and the coordination state those owners share, its lifecycle and singleton,
the observer hook, the persistence entry points, and the loader: ``_load`` vets and
scrubs every stored row at the trust boundary, so it stays with the scrub helpers
every other entrance uses too. The owners' methods are bound on the class by name,
in one table at the end of its body.

This module is also the subsystem's import and patch surface: every name it defined
before those owners moved out still resolves here as the same object its owner holds,
and so does every public name it imported from the rest of the package and every
other import callers and tests read off it. The names moved code reads through this
module on each call -- so a patch here reaches it -- are
``_OVERDUE_REARM_SECS``, ``_RECONCILE_INTERVAL_SECS``, ``replace_with_retry``,
``fsync_dir``, ``scrubbed_judge_spec``, ``_INSTANCE``, ``_MAINTENANCE_LOCKS`` and
``_MUTATION_LOCK_OWNERS``. Every other name the owners read is their own global, which
a patch here does not reach.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew import irq  # noqa: F401 -- read off this module by callers and tests
from kiro_crew import platform_compat  # noqa: F401 -- re-exported
from kiro_crew import probes  # noqa: F401 -- re-exported
from kiro_crew import shutdown_event  # noqa: F401 -- re-exported
from kiro_crew import autonudge_stop_log, validation
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
from kiro_crew.autonudge_service import timers as _timers
from kiro_crew.autonudge_service.gate import (  # noqa: F401 -- re-exported
    _JUDGE_QUIET_STREAK_FLOOR_DEFAULT,
    _MAX_QUIET_STREAK,
    _WAKE_FOLLOWUP_TICKS,
)
from kiro_crew.autonudge_service.maintenance import (  # noqa: F401 -- re-exported
    _assert_mutation_lock_owned,
    _AutoNudgeMaintenanceView,
    _cancel_and_drain_tasks,
    _claim_mutation_lock,
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
    MANUAL_STOP_REASON,
    MONITOR_TERMINAL_REASON,
    NUDGE_RENEW_DUE_SHARE,
    RUNTIME_BUDGET_REASON,
    SENTINEL_DROPPED_REASON,
    SESSION_START_FAILURE_REASON,
    STRUCTURAL_TERMINAL_REASON,
    AutoNudgeStaleBaseline,
    AutoNudgeStoreUnvetted,
    GoalUpdateConflict,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    NudgeLoop,
    _positive_number,
    _stopped_row_is_replaceable,
    is_channel_key,
    is_structured_monitor_loop,
    new_goal_token,
    nudge_cycle_header,
    runtime_budget_exceeded,
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
from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.goal import (
    GoalState,
    continuation_message,
)
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
from kiro_crew.platform import PlatformCompositionError, redact_log_via_context, redact_via_context
from kiro_crew.probes import targets  # noqa: F401 -- re-exported
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)


#: Bounds on what a PERSISTED judge brief may hold once loaded. The store is
#: agent-writable, so these are what make the loader's retention bounded by this
#: code rather than by the file: an oversized nested map would otherwise be kept in
#: memory and rewritten on every persist for the life of the loop.
#: The brief's shape bounds have ONE spelling, in ``validation``, because that is
#: where the arming surface refuses an oversized brief. A copied literal here would
#: be a second number to keep in step, and the two would disagree silently: the
#: arming call would accept a brief this loader then trimmed.
_JUDGE_MAX_TARGETS = validation.MAX_JUDGE_TARGETS
_JUDGE_MAX_TARGET_CHARS = validation.MAX_JUDGE_TARGET_CHARS
_JUDGE_MAX_CRITERION_CHARS = validation.MAX_JUDGE_CRITERION_CHARS
_JUDGE_MAX_CURSORS = 16
_JUDGE_MAX_OUTCOME_CHARS = 32


def _bounded_judge_spec(raw: object, loop_id: object = None) -> dict:
    """A stored judge brief rebuilt from allowlisted keys with clipped values.

    Rebuilt rather than validated in place, so a key this code does not know is
    dropped instead of retained: that is what makes the size of what the loader
    keeps a property of this function rather than of the file it read.
    """
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            logger.warning("AutoNudge: loop %s stored a non-object judge; ignoring it", loop_id)
        return {}
    out: dict = {}
    if validation.judge_is_off(raw):
        # The opt-out, and the one key this loader keeps that the arming validator
        # refuses from a caller: an owner spells it ``judge: false``, which normalises
        # to this marker, and the loader has to reload what was stored. Returned on its
        # own -- an opted-out loop has no criteria to carry, so pairing the marker with
        # a brief would describe a state no arming call can produce.
        return {validation.JUDGE_OFF_KEY: True}
    targets = raw.get("targets")  # noqa: F811 -- a local, not the probes.targets module
    if isinstance(targets, (list, tuple)):
        clean = [t for t in targets if isinstance(t, str) and t.strip()][:_JUDGE_MAX_TARGETS]
        if clean:
            out["targets"] = clean
    for key in ("wake_when", "quiet_when"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return scrubbed_judge_spec(out)


def scrubbed_judge_spec(spec: dict) -> dict:
    """A judge brief with every operator-authored string scrubbed, then clipped.

    The brief is free text somebody writes at arm time, and it reaches two places a
    credential must not: the loop store on disk, and the loop serialization the
    dashboard reads. Bounding a SHAPE is not scrubbing a VALUE -- an allowlist of keys
    with a length limit keeps a criterion naming a bearer token verbatim, merely
    shorter -- so the scrub is its own step and this is where it lives.

    Called at EVERY entrance, not once at the store boundary, because the entrances are
    independent: a brief that arrives on a tool call is serialized to the dashboard
    without passing the decode path, so a scrub applied only on decode covers the one
    entrance that already survived a round trip.

    Scrub BEFORE clip, in that order: a redaction marker can be longer than the secret
    it replaces, so clipping first lets a scrubbed value land over the bound.

    ``targets`` are scrubbed on the same rule. A clean forge URL passes
    ``redact_via_context`` unchanged, so no watch can be broken by this, and a target
    this alters is one carrying the credential the scrub exists for.
    """
    out: dict = dict(spec)
    for key in ("wake_when", "quiet_when"):
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = scrub_loop_text(value)[:_JUDGE_MAX_CRITERION_CHARS]
    targets = out.get("targets")  # noqa: F811 -- a local, not the probes.targets module
    if isinstance(targets, (list, tuple)):
        out["targets"] = [
            (scrub_loop_text(t)[:_JUDGE_MAX_TARGET_CHARS] if isinstance(t, str) and t else t)
            for t in targets
        ]
    return out


def _bounded_judge_cursors(raw: object) -> dict:
    """Stored read cursors, keyed by bounded target name with whole-number values."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        if len(out) >= _JUDGE_MAX_CURSORS:
            break
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        out[key[:_JUDGE_MAX_TARGET_CHARS]] = value
    return out


def _bounded_judge_pr_seen(raw: object) -> dict:
    """The stored pull-request baseline, reduced to a fixed-width digest and capped ids.

    The write path clips this value, but a clip on the way out constrains only what THIS
    build wrote: the store is writable by an auto-approved agent shell, so the bound that
    matters is the one the loader applies to whatever the file actually holds. Unknown
    keys are dropped rather than carried, so a row that grew a field is not retained and
    re-serialized for the life of the loop.

    A rejected or over-long id list costs at most a repeat delivery -- an id absent from
    the baseline reads as new again, which fires -- so every uncertain case here
    resolves by keeping less.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    digest = raw.get("digest")
    if isinstance(digest, str) and digest.strip():
        out["digest"] = digest.strip()[:_JUDGE_PR_SEEN_DIGEST_CHARS]
    ids: list[str] = []
    stored_ids = raw.get("remarks")
    for ident in stored_ids if isinstance(stored_ids, list) else []:
        if len(ids) >= _JUDGE_PR_SEEN_REMARKS:
            break
        if isinstance(ident, str) and ident.strip():
            ids.append(ident.strip()[:_JUDGE_MAX_TARGET_CHARS])
    out["remarks"] = ids
    return out


def _bounded_judge_verdict(raw: object) -> dict:
    """The stored previous verdict, reduced to its three bounded fields."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    outcome = raw.get("outcome")
    if isinstance(outcome, str) and outcome:
        out["outcome"] = outcome[:_JUDGE_MAX_OUTCOME_CHARS]
    for key in ("evidence_items", "at"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if key == "evidence_items":
            # Local to keep the decisions graph off the gateway boot path.
            from kiro_crew.decisions.points import nudge_wake as point_nudge_wake

            # Bounded at BOTH ends: the ceiling keeps the state inside its character
            # budget, and the floor matters just as much, because this store is
            # agent-writable and a 4,000-digit negative is as long on the wire as a
            # positive one. A count below zero is not a count, so it reads as none.
            if isinstance(value, int):
                out[key] = max(0, min(value, point_nudge_wake.MAX_EVIDENCE_ITEMS))
            elif math.isfinite(value):
                out[key] = max(0, min(value, point_nudge_wake.MAX_EVIDENCE_ITEMS))
            continue
        try:
            finite = math.isfinite(float(value))
        except OverflowError:
            finite = False
        if finite:
            out[key] = value
    return out


def _bounded_judge_recent(raw: object) -> list:
    """The stored labelled verdict history, each row rebuilt from allowlisted keys.

    Rebuilt rather than filtered, so a row an operator hand-edited into the store
    cannot carry text into the next request: the point screens what it sends, and this
    is the matching refusal on the way IN. A row naming no outcome is dropped -- it
    labels nothing and would only dilute the hit rate the judge reads.

    The newest rows are kept when the stored list is longer than the window, because a
    file written by a build with a wider window must not push this one's window into
    the past.

    An UNLABELLED delivered row is marked ``forfeited`` here, which is the one field
    this function adds rather than carrying across. Such a row belongs to a turn this
    process never saw: the fire went out, then the gateway stopped before the turn
    completed, so no ``owner_acted`` can ever be read for it, and the next turn on that
    slot -- quite possibly the owner's own -- would be labelled in its place. The flag
    refuses the label while KEEPING ``delivered``, so the row still closes its own label
    cycle and the suppressions behind it are not credited to a later delivery. A
    re-owed delivery gets a newer undecided row; the fire path stamps that row while
    this boundary remains unchanged.
    """
    # The local import keeps the decisions graph off the gateway boot path.
    from kiro_crew.autonudge_judge import MAX_STORED_VERDICTS, MAX_VERDICT_ID_CHARS

    if not isinstance(raw, list):
        return []
    out: list = []
    discarded = 0
    for index, item in enumerate(reversed(raw)):
        if len(out) >= MAX_STORED_VERDICTS:
            discarded = len(raw) - index
            break
        if not isinstance(item, dict):
            continue
        row = _bounded_judge_verdict(item)
        if "outcome" not in row:
            continue
        delivered = item.get("delivered") is True
        suppressed = item.get("suppressed") is True
        owner_acted = item.get("owner_acted")
        if delivered and isinstance(owner_acted, bool):
            row["owner_acted"] = owner_acted
        missed = item.get("missed")
        if suppressed and isinstance(missed, bool):
            row["missed"] = missed
        for key in ("suppressed", "answered"):
            value = item.get(key)
            if isinstance(value, bool):
                row[key] = value
        if delivered:
            row["delivered"] = True
            if "owner_acted" not in row:
                row["forfeited"] = True
        row_id = item.get("id")
        if isinstance(row_id, str) and row_id.strip():
            row["id"] = row_id.strip()[:MAX_VERDICT_ID_CHARS]
        out.append(row)
    if discarded:
        logger.debug(
            "AutoNudge: discarded %d older judge verdict history row(s) beyond the %d-row cap",
            discarded,
            MAX_STORED_VERDICTS,
        )
    out.reverse()
    return out


#: Fields a client ADDRESSES a row by, so the REST scrub exempts them -- which is
#: only safe because ``_load`` refuses a row whose value here is credential-shaped.
ADDRESSING_FIELDS = frozenset({"id", "slot_key"})


def _addressing_value_unsafe_why(got: object) -> str:
    """Why the load guard holds a row aside for THIS addressing value, or ``""``.

    ONE definition, shared by the guard that refuses a row and the matcher that decides a
    repair superseded it. Two copies could disagree, and a matcher with a laxer notion of
    unsafe would retire a held row against a loop the guard never accepted.
    """
    if not isinstance(got, str):
        return "is not a string"
    if not got.isprintable():
        return "contains a non-printable character"
    if redact_via_context(got) != got:
        return "is credential-shaped"
    return ""


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


def scrub_loop_text(value: Any) -> Any:
    """Credential-scrub one serialized ``NudgeLoop`` field value.

    ``None`` passes through untouched, because ``str(None)`` would turn an absent value
    into a message that reads like content. Everything else is scrubbed through
    ``platform.redact_via_context``, coerced with ``str()`` first when not already a
    string -- coerced rather than blanked so the operator can still see the bad row.
    """
    if value is None:
        return value
    if isinstance(value, str):
        if not value:
            return value
        return redact_via_context(value)
    return redact_via_context(str(value))


def redact_store_value(value: object) -> str:
    """Render a store-sourced value safe for a log line in this module.

    ``repr`` supplies the ESCAPE: a store value can carry a newline or an ANSI
    sequence, and a raw ``%r``/``%s`` would let it forge a second log record.

    The SCRUB is delegated to ``redact_log_via_context``, which already owns exactly
    this contract -- context-aware redaction for a log line that must not raise. That
    matters because several callers sit inside ``except`` arms whose documented job is
    to never raise.
    """
    return redact_log_via_context(repr(value))


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


def _is_torn_deactivation(loop: NudgeLoop) -> bool:
    """Whether a persisted row is inactive without any stop having been recorded.

    Every deactivation this service performs leaves one of two marks: a
    non-empty ``stopped_reason`` on the loop (``update`` defaults to
    ``"manual"``; the timer bounds, the terminal settlement and the
    sentinel-drop repair write theirs) or a terminal ``outcome`` on a monitor
    record — and all of them clear ``next_due_ts``. A row that is inactive with
    NEITHER mark and a deadline still set was therefore not stopped by this
    service; ``_load`` resumes it. A monitor whose wake is in flight is left to
    the claim-recovery branches, which own that state.

    The row must still carry its kill-switch path. A sentinel-drop repair that
    predates ``SENTINEL_DROPPED_REASON`` persisted this very shape MINUS the
    sentinel (it blanked the path and recorded nothing), and that stop was a
    fail-closed refusal to run without a kill switch -- so an empty sentinel is
    the one persisted discriminator between a torn write and that refusal, and
    the ambiguous shape stays inactive. A loop armed without a sentinel in the
    first place therefore is not resumed here either; the dashboard revives it.
    """
    if loop.active or loop.stopped_reason or loop.next_due_ts <= 0:
        return False
    if not loop.stop_sentinel_path:
        return False
    state = loop.monitor
    if state is None:
        return True
    return (
        state.version == MONITOR_STATE_VERSION
        and state.outcome is None
        and not state.wake_in_flight
    )


def _repair_number(
    value: Any, *, lo: float, fallback: float, hi: float | None = None
) -> tuple[float, bool]:
    """Coerce a persisted numeric field to a FINITE value within [lo, hi].

    Returns ``(repaired_value, was_repaired)``. Non-numeric, non-finite
    (``1e309`` parses to ``inf``, which json.dump would emit as invalid
    ``Infinity``), and out-of-range inputs all repair rather than raise, so a
    corrupt store entry can never abort gateway startup or poison the JSON
    the REST/WS surface emits.
    """
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: JSON integers are arbitrary-precision, so a persisted
        # 10**400 converts to float by raising rather than returning inf —
        # without this arm the error would escape to _load()'s per-entry
        # handler, which SKIPS the loop and lets the next persist delete it.
        return fallback, True
    if math.isnan(num) or math.isinf(num):
        return fallback, True
    clamped = max(lo, num) if hi is None else max(lo, min(hi, num))
    return clamped, clamped != num


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
        #: ``loop id -> item id -> wall-clock times`` of the pull-forwards that item
        #: bought its conductor in the last hour, and the ``(loop id, item id)`` pairs
        #: whose cap has already been logged in the current window. Read and written by
        #: ``conductor_wake`` on the event loop; released with the loop.
        self._pull_forward_counts: dict[str, dict[str, list[float]]] = {}
        self._pull_forward_capped: set[tuple[str, str]] = set()
        # Loop ids whose CURRENT tick observed a wake but has not yet had its fire
        # confirmed. Transient on purpose: it is a claim about a turn in flight,
        # so a restart must forget it rather than charge a turn that never ran.
        self._pending_monitor_wake: set[str] = set()
        #: A quiet-streak floor tick that has decided to deliver but not yet
        #: delivered. Same shape and same reason as the wake claim above: the charge
        #: belongs at the single point delivery is confirmed, never at the decision.
        self._pending_floor_tick: set[str] = set()
        # Loop ids whose timer task is CURRENTLY inside its ``_on_fire`` await.
        # ``update()`` must not cancel such a timer: for channel-bound loops the
        # fire callback runs the unattended turn INLINE, so cancelling it kills
        # the in-flight turn and loses its transcript and cycle bookkeeping.
        self._firing: set[str] = set()
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
        # Strong refs to in-flight shielded add() tasks: keeps a detached
        # mutation supervised (no GC, failures logged) even when every awaiting
        # caller was cancelled. Discarded on completion.
        self._inflight_adds: set = set()
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
        # The periodic reconciler task (see _reconcile_forever). Owned by
        # start()/stop(); None while the service is not running.
        self._reconciler: asyncio.Task | None = None
        # Loop ids the previous reconciler pass found eligible-and-unarmed.
        # A rescue requires membership here AND a second eligible observation
        # (see _reconcile_once); notify_user_input clears a slot's candidacy,
        # so any sign of life restarts the two-pass clock. Not persisted --
        # after a restart, start() re-arms every active loop anyway.
        self._reconcile_candidates: set[str] = set()
        self._observers: list[Callable[[str, NudgeLoop | None], None]] = []
        self._lock = asyncio.Lock()

    @property
    def _path(self) -> Path:
        """The store file's path, which callers and tests read off the service.

        Read from the composed store on every access rather than copied at
        construction, so the loader reads the file the store writes even after
        the store's path is reassigned.
        """
        return self._store.path

    # ── Persistence ──

    def _load(self) -> None:
        """Read the store and repair each entry. BLOCKING — see ``start()``.

        Does file I/O (locked read) and, via ``repair_sentinel_path``, realpath
        resolution that can stall on an unavailable network mount, so callers on
        the event loop MUST offload this (``no-blocking-call-on-event-loop``).
        """
        with _locked_file(self._path, "r") as fh:
            data = json.load(fh)
        # Reset per load: a re-read must not inherit a refusal from a prior one.
        self._store.load_refused = False
        # Prior quarantine is re-read and kept HELD: repairing the offending field is
        # not enough on its own, because the sidecar must not be the only durable copy.
        self._store.quarantined = []
        # And the ownership set with it: it is the LICENCE to remove, so a key surviving a
        # pass the row did not lets compaction delete a row a peer wrote afterwards.
        self._store.sidecar_seen = set()
        # Re-read per load: a row repaired between loads must stop being carried.
        self._store.unparsed_rows = []
        # No turn is in flight across a load, so no claim can be owed.
        self._store.delivering_claim = {}
        # Re-derived below from each row's own marker, never carried across a load.
        self._store.undelivered_claim = set()
        # The sidecar is the SINGLE durable location. Held-aside rows are deliberately
        # not embedded in the store too -- two copies of one state can disagree.
        prior_quarantined = self._store.read_quarantine_sidecar()
        # Arming while writes are refused is worse than arming nothing: a delivered cycle
        # cannot persist its counter, so a restart re-fires it past its own cycle cap.
        if self._store.load_refused:
            logger.warning(
                "AutoNudge: arming no loops — the quarantine sidecar at %s could not be "
                "read, so a delivered cycle could not record itself. Fix the file and "
                "restart.",
                self._store.quarantine_path,
            )
            return
        # Probe the ACTIVE credential policy ONCE, before the row loop: inside it the
        # per-row ``except`` swallows a composition failure and misreports the defect.
        try:
            redact_via_context("")
        except PlatformCompositionError:
            self._store.load_refused = True
            logger.error(
                "AutoNudge: refusing to arm any loop — this host declares a credential "
                "policy it could not compose, so a persisted addressing field cannot be "
                "vetted. The store is left untouched (writes are refused while this "
                "holds); fix the host and restart.",
                exc_info=True,
            )
            return
        # The list guard below cannot see a NON-DICT root: ``"loops" in []`` is False, so
        # a hand-edited ``[]`` or bare number reached the row loop and aborted boot.
        if not isinstance(data, dict):
            self._store.load_refused = True
            logger.error(
                "AutoNudge: refusing to arm any loop — the store at %s holds %s at its "
                "root instead of an object, so no row can be read. Writes are refused (a "
                "write would delete them); fix the file and restart.",
                self._path,
                type(data).__name__,
            )
            return
        # PRESENT-BUT-NOT-A-LIST is corruption, not an empty store: read as empty, the
        # next mutation replaces the file and deletes every row it held. ABSENT is legal.
        if "loops" in data and not isinstance(data["loops"], list):
            self._store.load_refused = True
            logger.error(
                "AutoNudge: refusing to arm any loop — the store at %s carries %s under "
                "'loops' instead of a list, so its rows cannot be enumerated. Writes are "
                "refused (a write would delete them); fix the file and restart.",
                self._path,
                type(data["loops"]).__name__,
            )
            return
        store_rows = _rows_or_empty(data.get("loops"))
        # HELD, NEVER ARMED: arming a held-aside row made the sidecar the only durable
        # copy of a live row, so a failed compaction plus a delete left it to re-arm.
        for raw in prior_quarantined:
            self._store.quarantined.append(deepcopy(raw))
            # Accounted for by THIS instance, so compaction may later drop it once repaired.
            # A row a peer adds after this read stays outside the set, and so survives.
            self._store.sidecar_seen.add(_quarantine_row_key(raw))
            logger.warning(
                "autonudge: not arming held-aside loop %s -- held rows are kept for "
                "repair, never armed; move the repaired row into %s to arm it",
                redact_store_value(raw.get("id") if isinstance(raw, dict) else None),
                self._path,
            )
        for raw in store_rows:
            try:
                loop_values = {
                    key: raw[key]
                    for key in raw
                    if key in NudgeLoop.__dataclass_fields__ and key != "monitor"
                }
                # ``gate`` decides whether a loop may be observation-gated, and a
                # stored value that is not a bool is not a decision: the STRING
                # "false" is truthy, so passing it through would gate a loop that
                # asked not to be. Normalise it here, at the boundary, rather than
                # hardening each read site.
                # PRESENT-AND-NOT-A-BOOL, which includes ``null``, normalised to
                # FALSE. Normalising it to True instead -- on the grounds that reading
                # corrupt data as an opt-out would ungate loops nobody chose to
                # ungate -- has the asymmetry backwards: gating is the state that
                # can silently STOP a loop, so an unreadable value must resolve to
                # ungated -- costing a turn per interval, which is today's cost --
                # rather than to gated, which can deactivate a recurring task whose
                # instruction merely mentioned a pull request. Only an explicit
                # boolean true gates.
                if "gate" in loop_values and not isinstance(loop_values["gate"], bool):
                    logger.warning(
                        "AutoNudge: loop %s stored a non-boolean gate (%r); leaving it ungated",
                        raw.get("id"),
                        loop_values["gate"],
                    )
                    loop_values["gate"] = False
                # The judge fields come out of the same agent-writable store, so they
                # are normalised HERE rather than at each read site, for the reason
                # ``gate`` is: a stored ``"judge": "yes"`` would otherwise reach a
                # collector as a string, and a non-numeric streak would defeat the
                # floor that bounds how long a judge may keep a loop quiet. Every
                # unreadable value resolves to the shape that behaves as today.
                #
                # Bounded, not merely type-checked. A row in this store can be written
                # by an auto-approved agent shell, so an oversized nested map would be
                # RETAINED and re-serialized on every persist -- unbounded memory and
                # write work for the life of the loop. Each map is rebuilt from
                # allowlisted keys with clipped values, so what the loader keeps is
                # bounded by this code rather than by whatever the file holds.
                if "judge" in loop_values:
                    loop_values["judge"] = _bounded_judge_spec(loop_values["judge"], raw.get("id"))
                if "judge_cursors" in loop_values:
                    loop_values["judge_cursors"] = _bounded_judge_cursors(
                        loop_values["judge_cursors"]
                    )
                if "judge_last_verdict" in loop_values:
                    loop_values["judge_last_verdict"] = _bounded_judge_verdict(
                        loop_values["judge_last_verdict"]
                    )
                if "judge_recent_verdicts" in loop_values:
                    loop_values["judge_recent_verdicts"] = _bounded_judge_recent(
                        loop_values["judge_recent_verdicts"]
                    )
                if "judge_pr_seen" in loop_values:
                    loop_values["judge_pr_seen"] = _bounded_judge_pr_seen(
                        loop_values["judge_pr_seen"]
                    )
                # Only an explicit boolean true owes a turn. This one resolves the other
                # way from ``gate``: a corrupt value here costs at most one extra fire,
                # while reading it as owed on every load would make a loop fire forever
                # without ever consulting its judge.
                if "judge_wake_pending" in loop_values and not isinstance(
                    loop_values["judge_wake_pending"], bool
                ):
                    logger.warning(
                        "AutoNudge: loop %s stored a non-boolean judge_wake_pending (%r); "
                        "treating the wake as delivered",
                        raw.get("id"),
                        loop_values["judge_wake_pending"],
                    )
                    loop_values["judge_wake_pending"] = False
                if "judge_quiet_streak" in loop_values:
                    _streak = loop_values["judge_quiet_streak"]
                    if isinstance(_streak, bool) or not isinstance(_streak, int) or _streak < 0:
                        logger.warning(
                            "AutoNudge: loop %s stored a non-count judge streak; resetting it",
                            raw.get("id"),
                        )
                        loop_values["judge_quiet_streak"] = 0
                    else:
                        loop_values["judge_quiet_streak"] = min(_streak, _MAX_QUIET_STREAK)
                # ``self_armed`` is the ONE bit that relaxes the crew/member
                # fire-time guard, and this store is agent-writable. A persisted
                # non-boolean (the string "false" is truthy) must therefore
                # normalise to the REFUSING value, exactly as ``gate`` above
                # normalises to its safe value: only an explicit boolean True
                # admits, and the fire-time check compares ``is True`` besides.
                if "self_armed" in loop_values and not isinstance(loop_values["self_armed"], bool):
                    logger.warning(
                        "AutoNudge: loop %s stored a non-boolean self_armed (%s); "
                        "treating it as externally armed",
                        redact_store_value(raw.get("id")),
                        redact_store_value(loop_values["self_armed"]),
                    )
                    loop_values["self_armed"] = False
                # ``config_generation`` is agent-writable persisted data and is
                # used in arithmetic (``+= 1``) and an equality fence. A stored
                # ``null``, string or negative would raise mid-mutation (a partial
                # update + HTTP 500) or corrupt the fence, so normalise it at the
                # boundary to a non-negative int, exactly like ``gate`` above.
                # Absent -> the dataclass default 0. An unreadable value resets to
                # 0, which only makes a captured pre-existing verdict's generation
                # not match (a missed stop, the safe direction), never a wrong stop.
                if "config_generation" in loop_values:
                    _cg = loop_values["config_generation"]
                    if not isinstance(_cg, int) or isinstance(_cg, bool) or _cg < 0:
                        logger.warning(
                            "AutoNudge: loop %s stored a non-int/negative "
                            "config_generation (%r); resetting to 0",
                            raw.get("id"),
                            _cg,
                        )
                        loop_values["config_generation"] = 0
                if loop_values.get("goal") is not None:
                    stored_goal = loop_values["goal"]
                    loop_values["goal"] = GoalState.from_dict(stored_goal)
                    if any(
                        stored_goal[name] != getattr(loop_values["goal"], name)
                        for name in ("objective", "criteria", "progress", "evidence")
                        if name in stored_goal
                    ):
                        self._store_dirty = True
                loop = NudgeLoop(**loop_values)
                # Rotated on EVERY load: a human may have hand-edited the goal while we
                # were down, so a pre-restart token must not authorise overwriting it.
                loop.goal_token = new_goal_token()
                # Owed in BOTH arms: a cap applied later is refused against ``cycle_count``, so
                # committing an unconfirmed cycle is not inert even on an uncapped loop.
                inflight = raw.get("inflight_cycle")
                if isinstance(inflight, bool):
                    # ``isinstance(True, int)`` is TRUE, so a stored boolean would read as
                    # cycle 1 and spend a phantom cycle that no turn ever claimed.
                    logger.warning(
                        "AutoNudge: loop %s stored a non-integer cycle claim (%s); ignoring it",
                        redact_store_value(raw.get("id")),
                        redact_store_value(inflight),
                    )
                    inflight = None
                    self._store_dirty = True
                if isinstance(inflight, int) and inflight > loop.cycle_count:
                    # The claim reaches disk BEFORE the turn it claims, so an unresolved one
                    # cannot say whether the reader already received that turn.
                    logger.warning(
                        "AutoNudge: loop %s was interrupted mid-delivery on cycle %d — it is "
                        "owed, not spent, and is held stopped until re-activated",
                        redact_store_value(raw.get("id")),
                        inflight,
                    )
                    self._store.unreconciled_claim[loop.id] = inflight
                    if raw.get("inflight_undelivered") is True:
                        # The writer knew this turn never went out, so the charge is not owed.
                        self._store.undelivered_claim.add(loop.id)
                    loop.active = False
                    # A reason the UI can render: without it a routine restart mid-fire
                    # surfaced as an unexplained pause with only a log line behind it.
                    loop.stopped_reason = "interrupted_cycle"
                    self._store_dirty = True
                # TRUST BOUNDARY: the REST scrub exempts these fields, so a credential
                # placed in one reaches every client verbatim. REFUSED, never scrubbed.
                unsafe_field = None
                unsafe_why = ""
                for name in sorted(ADDRESSING_FIELDS):
                    why = _addressing_value_unsafe_why(getattr(loop, name, None))
                    if why:
                        unsafe_field, unsafe_why = name, why
                        break
                if unsafe_field is not None:
                    logger.warning(
                        "AutoNudge: refusing loop %s — its %s %s and addressing fields are "
                        "served unscrubbed; fix the store entry",
                        redact_store_value(loop.id),
                        unsafe_field,
                        unsafe_why,
                    )
                    # QUARANTINE rather than refuse the store: siblings keep running while
                    # the offending row is held on disk for repair instead of deleted.
                    key = _quarantine_row_key(raw)
                    if key not in {_quarantine_row_key(r) for r in self._store.quarantined}:
                        self._store.quarantined.append(deepcopy(raw))
                    self._store_dirty = True
                    continue
                if "monitor" in raw:
                    monitor_raw = raw["monitor"]
                    monitor_quarantined = False
                    try:
                        loop.monitor = monitor_state_from_dict(monitor_raw)
                    except (TypeError, ValueError):
                        monitor_quarantined = True
                        quarantine_needs_rewrite = loop.active or loop.next_due_ts != 0.0
                        loop.monitor = quarantine_monitor_state(monitor_raw)
                        loop.active = False
                        loop.next_due_ts = 0.0
                        self._store_dirty = self._store_dirty or quarantine_needs_rewrite
                        logger.warning(
                            "AutoNudge: quarantined malformed monitor record for loop %s",
                            loop.id,
                            exc_info=True,
                        )
                    if (
                        "gate" not in raw
                        and loop.monitor.version == MONITOR_STATE_VERSION
                        and not monitor_quarantined
                    ):
                        # Records from the pre-gate prompt path can carry inferred
                        # observation state without an explicit decision to gate.
                        # Migrate them to a plain ungated loop so later saves and
                        # restarts cannot mistake the inert payload for a typed
                        # controller record.
                        loop.monitor = None
                        self._store_dirty = True
                    if loop.monitor is None:
                        pass
                    elif loop.monitor.version != MONITOR_STATE_VERSION:
                        # An older controller cannot safely interpret a newer
                        # policy. The stored ``active`` intent is deliberately
                        # left alone -- it belongs to the gateway that wrote it
                        # and must survive a downgrade so an upgrade resumes the
                        # legacy watch. A structured controller record, however,
                        # is exposed through control surfaces that must agree it
                        # cannot be scheduled, so retire only that record shape.
                        loop.monitor.outcome = MonitorOutcome.BLOCKED
                        loop.monitor.stopped_reason = MONITOR_STOP_UNSUPPORTED_VERSION
                        if is_structured_monitor_loop(loop):
                            if loop.active or loop.next_due_ts:
                                self._store_dirty = True
                            loop.active = False
                            loop.next_due_ts = 0.0
                    elif loop.monitor.outcome is not None:
                        # A terminal record is inspectable, never schedulable,
                        # even when a hand-edited store contradicts itself.
                        if loop.active or loop.monitor.wake_in_flight or loop.next_due_ts:
                            self._store_dirty = True
                        loop.active = False
                        loop.monitor.wake_in_flight = False
                        loop.monitor.completion_evidence_deadline = 0.0
                        loop.next_due_ts = 0.0
                    elif is_structured_monitor_loop(loop) and loop.monitor.wake_in_flight:
                        if (
                            loop.monitor.wake_delivery is None
                            and loop.monitor.completion_evidence_deadline > 0
                        ):
                            # A legacy snapshot can carry the accepted evidence
                            # deadline without the later typed delivery marker.
                            # Recover it as dispatched so the finite expiry path
                            # owns the claim instead of leaving it immortal.
                            loop.monitor.wake_delivery = MonitorDispatchResult.DISPATCHED
                            self._store_dirty = True
                        if (
                            loop.monitor.wake_delivery is MonitorDispatchResult.BUSY
                            and loop.next_due_ts > 0
                        ):
                            # BUSY proves no action turn started. Resume the
                            # already-claimed wake at its persisted retry instead
                            # of treating the intentionally empty evidence
                            # deadline as an ambiguous accepted dispatch.
                            if loop.monitor.next_probe_at != loop.next_due_ts:
                                loop.monitor.next_probe_at = loop.next_due_ts
                                self._store_dirty = True
                        elif loop.monitor.completion_evidence_deadline <= 0:
                            # A persisted claim with no accepted-dispatch
                            # deadline may have died on either side of handoff.
                            # Retire it without charging or redispatching.
                            loop.monitor.wake_in_flight = False
                            if loop.monitor.outcome is None:
                                loop.monitor.outcome = MonitorOutcome.BLOCKED
                                loop.monitor.stopped_reason = MONITOR_STOP_COMPLETION_UNAVAILABLE
                            loop.active = False
                            loop.next_due_ts = 0.0
                            self._store_dirty = True
                        elif loop.next_due_ts != loop.monitor.completion_evidence_deadline:
                            loop.next_due_ts = loop.monitor.completion_evidence_deadline
                            loop.monitor.next_probe_at = loop.next_due_ts
                            self._store_dirty = True
                    elif (
                        is_structured_monitor_loop(loop)
                        and loop.active
                        and self._on_monitor_tick is None
                        and self._on_fire is not None
                    ):
                        # Structured monitor delivery belongs to the controller,
                        # which is intentionally not wired in this substrate.
                        # Deactivate rather than allowing the legacy timer to
                        # inject the prompt before a typed decision is made.
                        loop.active = False
                        loop.monitor.outcome = MonitorOutcome.BLOCKED
                        loop.monitor.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
                        loop.monitor.stopped_at = time.time()
                        loop.next_due_ts = 0.0
                        self._store_dirty = True
                    # A current, un-claimed, unsettled monitor keeps its active
                    # intent and re-arms like any other loop. It used to be
                    # deactivated here because delivery had no gate and the
                    # legacy timer would have injected a prompt without a
                    # decision; the gate in _monitor_tick_is_quiet now makes that
                    # decision on every tick, so surviving a restart is correct
                    # rather than a hazard. Deactivating instead would end every
                    # watch at the next gateway restart -- silently, since a
                    # stopped watch and a quiet one look identical from outside.
                # Re-home / re-validate the persisted kill-switch path. A loop
                # armed before the data-home move would otherwise be re-armed
                # with a sentinel path nothing can ever create (see
                # repair_sentinel_path). INSIDE the per-entry try: a malformed
                # store entry must be skipped, never abort start() and take the
                # gateway offline.
                repaired = repair_sentinel_path(loop.stop_sentinel_path)
                # Same fail-open posture for the numeric timer fields: they
                # drive arithmetic at arm time (``start()`` →
                # ``_arm_from_deadline``) and are emitted as JSON by the
                # REST/WS surface, so both must be finite and in range. A
                # hand-edited or foreign-written store degrades per-field —
                # never a startup abort (TypeError on a string interval) and
                # never non-standard JSON output (a 1e309 deadline parses to
                # ``inf``, which json.dump emits as invalid ``Infinity``).
                loop.next_due_ts, due_repaired = _repair_number(
                    loop.next_due_ts, lo=0.0, fallback=0.0
                )
                idle_num, idle_repaired = _repair_number(
                    loop.idle_secs,
                    lo=float(_MIN_IDLE_SECS),
                    hi=float(_MAX_IDLE_SECS),
                    fallback=float(_MIN_IDLE_SECS),
                )
                loop.idle_secs = int(idle_num)
                # ``consecutive_start_failures`` is compared with ``>=`` on every
                # wake, and this store is agent-writable, so a persisted string or
                # ``null`` would raise ``TypeError`` inside ``_timer`` and the
                # active automation would silently never fire again -- and the
                # malformed value survives every reload. Normalised at the
                # boundary for the same reason ``gate``, ``self_armed`` and
                # ``config_generation`` are, rather than by hardening the one
                # comparison.
                streak_num, streak_repaired = _repair_number(
                    loop.consecutive_start_failures, lo=0.0, fallback=0.0
                )
                loop.consecutive_start_failures = int(streak_num)
                if streak_repaired:
                    self._store_dirty = True
                # The two counters the timer reads on every wake, for the same
                # reason: ``cycle_count`` meets ``>=`` against the cap and
                # ``created_ts`` is subtracted from the clock, so a persisted
                # string or ``null`` in either raises inside ``_timer`` and
                # dead-ends the loop the same way. Repaired to 0 -- a count of
                # nothing run, and the anchor every budget reader already treats
                # as "nothing to measure from" -- rather than a guess that could
                # stop a healthy loop. The user's resume preserves the
                # breakpoint, so this boundary is the one place they are
                # repaired. The BOUNDS are deliberately left as stored: a
                # malformed cap or budget repaired to 0 would quietly remove a
                # cost limit the user typed, and persist that.
                count_num, count_repaired = _repair_number(loop.cycle_count, lo=0.0, fallback=0.0)
                loop.cycle_count = int(count_num)
                loop.created_ts, created_repaired = _repair_number(
                    loop.created_ts, lo=0.0, fallback=0.0
                )
                # ``consecutive_failed_cycles`` is compared with ``>=`` on every
                # wake too, for the same agent-writable-store reason, so it is
                # normalised at the boundary alongside its siblings above.
                failed_num, failed_repaired = _repair_number(
                    loop.consecutive_failed_cycles, lo=0.0, fallback=0.0
                )
                loop.consecutive_failed_cycles = int(failed_num)
                if count_repaired or created_repaired or failed_repaired:
                    self._store_dirty = True
                if (
                    loop.monitor is not None
                    and loop.monitor.version == MONITOR_STATE_VERSION
                    and loop.monitor.next_probe_at != loop.next_due_ts
                ):
                    # NudgeLoop owns the restart schedule; the monitor field is
                    # its atomically-persisted inspection mirror.
                    loop.monitor.next_probe_at = loop.next_due_ts
                    self._store_dirty = True
                if due_repaired or idle_repaired:
                    self._store_dirty = True
                notification_stopped_at, notification_time_repaired = _repair_number(
                    loop.terminal_notification_stopped_at,
                    lo=0.0,
                    fallback=0.0,
                )
                loop.terminal_notification_stopped_at = notification_stopped_at
                notification_outcome = loop.terminal_notification_outcome
                valid_notification_outcomes = {item.value for item in MonitorOutcome}
                if (
                    not isinstance(notification_outcome, str)
                    or notification_outcome not in valid_notification_outcomes
                ):
                    if (
                        notification_outcome
                        or notification_stopped_at
                        or notification_time_repaired
                    ):
                        self._store_dirty = True
                    loop.terminal_notification_outcome = ""
                    loop.terminal_notification_stopped_at = 0.0
                elif notification_time_repaired:
                    self._store_dirty = True
                if (
                    loop.monitor is not None
                    and loop.monitor.outcome is not None
                    and loop.monitor.terminal_notification_delivered
                    and not loop.terminal_notification_outcome
                ):
                    loop.terminal_notification_outcome = loop.monitor.outcome.value
                    loop.terminal_notification_stopped_at = loop.monitor.stopped_at
                    self._store_dirty = True
                # ``banner`` is display-only, but it is ``.strip()``ed on the
                # fire path, so a non-string value there raises AttributeError
                # and the loop rearms forever without ever delivering. Normalize
                # it here for the same reason ``repair_sentinel_path`` opens with
                # an isinstance check: both are persisted STRING fields read
                # straight out of parsed JSON, where the dataclass annotation is
                # not enforced. Repaired-and-persisted rather than merely
                # tolerated, so a hand-edited store is corrected once instead of
                # silently suppressing the banner on every boot.
                if not isinstance(loop.banner, str):
                    logger.warning(
                        "AutoNudge: loop %s had a non-string banner (type %s) — treating it "
                        "as absent; the transcript row falls back to the full message",
                        loop.id,
                        type(loop.banner).__name__,
                    )
                    loop.banner = ""
                    self._store_dirty = True
                elif loop.banner:
                    # SCRUB a persisted string banner, redacting the FULL value
                    # BEFORE any cap slice. A banner reaches the store through
                    # producers that skip the authorized write paths — a
                    # hand-edited ``autonudge.json``, a direct agent ``svc.add``,
                    # or a banner persisted before this scrub existed — and the
                    # loop is served RAW by ``GET /api/autonudge`` (``_serialize``
                    # is ``asdict``), broadcast to every dashboard client, and
                    # replayed by the fire path, so an unscrubbed credential here
                    # reaches the browser after a restart. Same two passes the
                    # write path uses, redaction FIRST so a secret straddling the
                    # cap is masked WHOLE — slicing first would leave a raw prefix
                    # the scanner cannot match. An over-cap banner — measured
                    # BEFORE redaction OR after (redaction can shrink an
                    # exfiltration URL below the cap, or grow a credential above
                    # it) — is then BLANKED (absent), matching the promise the cap
                    # makes elsewhere: a value the authorized write path would have
                    # rejected is not invented back by keeping a shrunk remnant,
                    # and the row falls back to the full message.
                    scrubbed, _ = redact_exfiltration_urls(loop.banner)
                    scrubbed, _ = redact_credentials(scrubbed)
                    if len(loop.banner) > MAX_BANNER_CHARS or len(scrubbed) > MAX_BANNER_CHARS:
                        scrubbed = ""
                    if scrubbed != loop.banner:
                        loop.banner = scrubbed
                        self._store_dirty = True
                # SCRUB the persisted ``message`` on load — same rationale as the
                # banner above and the same two redaction passes. The store is
                # writable out-of-band (a hand-edited ``autonudge.json`` or a
                # direct ``svc.add``) and served RAW by ``GET /api/autonudge``,
                # so a credential that reached the store bypassing the authorized
                # write path — which already scrubs ``message`` — would otherwise
                # be broadcast to every dashboard client after a restart. Unlike
                # the banner this is redaction ONLY, never blank-on-length:
                # ``message`` is the payload the model receives and has no
                # fallback row, and its 8000-char limit is a write-path concern.
                if loop.goal is not None:
                    # Typed goal data owns this derived prompt, including after an offline edit.
                    canonical_message = continuation_message(loop.goal)
                    if loop.message != canonical_message:
                        loop.message = canonical_message
                        self._store_dirty = True
                    if loop.goal.status == "suggested":
                        # A stored proposal cannot authorize its own first wake.
                        if loop.active is not False or loop.next_due_ts != 0.0:
                            self._store_dirty = True
                        loop.active = False
                        loop.next_due_ts = 0.0
                elif isinstance(loop.message, str) and loop.message:
                    scrubbed_msg, _ = redact_exfiltration_urls(loop.message)
                    scrubbed_msg, _ = redact_credentials(scrubbed_msg)
                    if scrubbed_msg != loop.message:
                        loop.message = scrubbed_msg
                        self._store_dirty = True
                if _is_torn_deactivation(loop):
                    # INACTIVE, NO stop reason, deadline still LIVE. No stop path
                    # of this service produces that shape: ``update`` clears the
                    # deadline and records a reason on every deactivation, the
                    # timer bounds and the terminal paths record theirs, and the
                    # repairs above zero the deadline when they retire a record.
                    # The row was flipped by a write outside the stop paths (a
                    # store migrated between hosts or edited by hand) and, left
                    # alone, it is a loop that nobody stopped yet nothing will
                    # ever arm again — every babysit silently dead after one
                    # restart, with the re-arm refused on top. Resume it: the
                    # schedule the user set is still on the row, and so is its
                    # kill switch (the predicate requires the sentinel path).
                    logger.warning(
                        "AutoNudge: loop %s was inactive with no stop reason and a live "
                        "deadline — resuming it; a paused loop records a reason",
                        loop.id,
                    )
                    loop.active = True
                    self._store_dirty = True
            except Exception:
                logger.warning("AutoNudge: skipping malformed loop entry: %r", raw, exc_info=True)
                # Without this, a sibling flagging the store dirty makes the rewrite delete
                # this row permanently -- the warning above is then its only other trace.
                self._store.unparsed_rows.append(raw)
                continue
            self._loops[loop.id] = loop
            if repaired != loop.stop_sentinel_path:
                dropped = bool(loop.stop_sentinel_path) and not repaired
                loop.stop_sentinel_path = repaired
                if dropped:
                    # FAIL CLOSED, matching the arm-time contract:
                    # authorize_and_add_nudge REFUSES to arm a loop whose
                    # sentinel is sensitive, so a persisted loop whose sentinel
                    # has become sensitive must not be re-armed with no kill
                    # switch at all. Deactivating leaves it inspectable and
                    # restartable rather than silently unstoppable-by-file.
                    logger.warning(
                        "AutoNudge: deactivating loop %s — its stop sentinel was dropped",
                        loop.id,
                    )
                    # Record WHY and clear the schedule, like every other stop:
                    # a reasonless inactive row with a live deadline is the
                    # torn-write shape ``_is_torn_deactivation`` resumes on the
                    # next boot, which would undo this refusal. Only a row THIS
                    # branch deactivates gets the stamp: a row already paused
                    # keeps the reason its own stop recorded (a manual pause
                    # relabelled ``sentinel_dropped`` would become re-armable).
                    if loop.active:
                        loop.active = False
                        loop.stopped_reason = SENTINEL_DROPPED_REASON
                    loop.next_due_ts = 0.0
                    if loop.monitor is not None:
                        loop.monitor.next_probe_at = 0.0
                self._store_dirty = True
        # Stop-record baseline: rows ACTIVE on disk, taken before repair so a loop this
        # load deactivates (a cycle interrupted by the restart) is recorded when the
        # repair persists -- but only rows this load ACCEPTED. A held-aside or unparsed
        # row is not served, so it must not be reported as removed either.
        with self._store.commit_lock:
            self._store.committed_active = {
                loop_id: summary
                for loop_id, summary in autonudge_stop_log.active_summaries(store_rows).items()
                if loop_id in self._loops
            }
        logger.info("AutoNudge: loaded %d loops", len(self._loops))

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
        for cb in self._observers:
            try:
                cb(event, loop)
            except Exception:
                logger.warning("AutoNudge observer failed", exc_info=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        if not enabled():
            logger.info("AutoNudge disabled (KIROCREW_AUTONUDGE not set)")
            return
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

    def stop(self) -> None:
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
        # It pops as it goes, so iterate over a snapshot of the keys.
        for loop_id in list(self._timers):
            self._cancel_timer(loop_id)
        self._timers.clear()
        self._reconcile_candidates.clear()
        self._accepted_monitor_turns.clear()
        self._maintenance_quiescing.clear()
        self._maintenance_quiesce_events.clear()
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None
            # The crew-log subscriptions and the boards they filled are bounded by this
            # service's loop table; with the table gone they have nothing to fire, so
            # they are disposed with it. A later start() joins them again.
            from kiro_crew import conductor_wake

            conductor_wake.dispose_all()

    async def _persist_locked(self) -> None:
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
            await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)

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

    async def _write_monitor_snapshot_locked(self, payload: dict | None = None) -> None:
        """Persist a monitor transition without releasing ``_lock`` mid-write."""
        if payload is None:
            payload = self._serialize_state()
        future = asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
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
        restart, never a premature or dropped fire.
        """
        task = asyncio.create_task(self._persist_locked())
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
    notify_cycle_start_failed = _timers.notify_cycle_start_failed
    notify_cycle_failed = _timers.notify_cycle_failed
    notify_cycle_landed = _timers.notify_cycle_landed
    notify_turn_complete = _timers.notify_turn_complete
    notify_user_input = _timers.notify_user_input
    _cancel_timer = _timers._cancel_timer
    _arm_timer = _timers._arm_timer
    _arm_from_deadline = _timers._arm_from_deadline
    _reconcile_forever = _timers._reconcile_forever
    _reconcile_once = _timers._reconcile_once
    # autonudge_service.gate
    _commit_judge_pr_seen = _gate._commit_judge_pr_seen
    _publish_pr_observation = _gate._publish_pr_observation
    _monitor_tick_is_quiet = _gate._monitor_tick_is_quiet
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
    _run_fire_cycle = _firing._run_fire_cycle
    fire_now = _firing.fire_now
    # autonudge_service.mutations
    pause_goal = _mutations.pause_goal
    pause_goals_by_slot = _mutations.pause_goals_by_slot
    _supervise_goal_pause = _mutations._supervise_goal_pause
    _pause_goals_by_slot_locked = _mutations._pause_goals_by_slot_locked
    _pause_goal_locked = _mutations._pause_goal_locked
    _pause_goal_unserialized = _mutations._pause_goal_unserialized
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
    restore_goal_after_failed_session_close = _mutations.restore_goal_after_failed_session_close
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
    mark_terminal_notification_delivered = _monitor_records.mark_terminal_notification_delivered
    retire_monitor_for_session_close = _monitor_records.retire_monitor_for_session_close
    restore_monitor_after_failed_session_close = (
        _monitor_records.restore_monitor_after_failed_session_close
    )
    update_monitor = _monitor_records.update_monitor
    rollback_monitor_update = _monitor_records.rollback_monitor_update
    mark_monitor_action_in_flight = _monitor_records.mark_monitor_action_in_flight
    record_monitor_turn_completion = _monitor_records.record_monitor_turn_completion
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
