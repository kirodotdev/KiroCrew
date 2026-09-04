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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Iterator

from kiro_crew import irq, platform_compat, probes, shutdown_event
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.loader import config_dir, data_home
from kiro_crew.config.paths import legacy_home
from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.monitoring.decision import decide_monitor, monitor_budget_reason
from kiro_crew.monitoring.models import (
    MONITOR_BUSY_RETRY_SECS,
    MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS,
    MONITOR_STATE_VERSION,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_COMPLETION_UNAVAILABLE,
    MONITOR_STOP_INVALID_RECORD,
    MONITOR_STOP_SESSION_CLOSE,
    MONITOR_STOP_SESSION_UNAVAILABLE,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    MONITOR_STOP_USER,
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    monitor_state_from_dict,
    monitor_state_to_dict,
    quarantine_monitor_state,
)
from kiro_crew.probes import targets
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult

logger = logging.getLogger(__name__)

#: ``stopped_reason`` for a loop whose watched subject finished (a merged or
#: closed pull request). Distinct from the bound reasons because there is nothing
#: left to SERVICE, not because it went well: only a merge is recorded as a
#: success, while a pull request closed without merging is recorded as blocked and
#: still needs a decision. What the two share -- and what this reason means -- is
#: that re-arming would poll a dead subject, so a revival check that treated it as
#: a cap would bring back a watch with nothing to watch.
MONITOR_TERMINAL_REASON = "monitor_terminal"

#: Ticks the gate is bypassed for after each wake, so a woken agent gets a
#: second turn to finish. One, because the cost is paid per wake and a second
#: free turn buys progress the probe cannot observe; raising it multiplies the
#: cost of every wake, and lowering it to zero reintroduces the stall.
_WAKE_FOLLOWUP_TICKS = 1

#: Consecutive quiet observations after which a gated loop is delivered anyway.
#:
#: This is what makes "gating slows an act-on-quiet loop" true instead of
#: "gating silences it". Ten keeps the great majority of the saving (nine ticks
#: in ten cost nothing) while bounding how long any loop can go undelivered to
#: ten intervals -- under an hour on the 300s interval agents actually use.
#: Lower wastes the saving on loops that had nothing to do; higher starts to
#: look like silence to whoever armed the watch.
_MAX_QUIET_STREAK = 10

_NUDGES_FILE = "autonudge.json"
_STORE_VERSION = 1
_MIN_IDLE_SECS = 15
_MAX_IDLE_SECS = 86400  # 24h
# Re-arm delay after a skipped/failed fire so a busy slot or a transient fire
# error can't silently orphan the loop. The delay escalates exponentially per
# consecutive failure (base << streak) up to _REARM_MAX_BACKOFF_SECS, and is
# always capped by the loop's idle_secs, so a permanently-wedged callback backs
# off to a slow poll instead of hammering every base interval.
_REARM_BACKOFF_SECS = 15
_REARM_MAX_BACKOFF_SECS = 300  # 5m ceiling for the escalated re-arm delay
_REARM_BACKOFF_MAX_SHIFT = 16  # clamp the 2**shift exponent
_MONITOR_RETRY_BACKOFF_SECS = 15
_MONITOR_RETRY_MAX_BACKOFF_SECS = 300

# Re-arm delay when a loop's deadline has already passed while a user turn was
# in flight. Small but non-zero: firing the instant the user's turn ends would
# race their follow-up message; a short beat leaves room for notify_user_input
# to cancel the pending fire again if they are still actively conversing.
_OVERDUE_REARM_SECS = 10

# Persisted source category for a deliberate ``autonudge_stop`` directive.
# The caller's free-form explanation is intentionally not stored: it is
# model-authored text and the watchdog only needs the deterministic source.
AUTONUDGE_STOP_REASON = "autonudge_stop"

# Persisted reason for a loop stopped because one of its cycles could not obtain
# tool approval. Named separately from the other bounds because its remedy is
# different in kind: the cap and the budget are raised, this one needs an
# authorization the loop cannot grant itself.
APPROVAL_STALL_REASON = "approval_stalled"


class NudgeAdmissionRefused(RuntimeError):
    """The session authorized for an arm disappeared before its commit point."""


_TERMINAL_BOUND_REASONS = frozenset({"cycle_cap", "runtime_budget", APPROVAL_STALL_REASON})

#: Stops the SYSTEM imposed on a legacy loop, which a directive re-arm may
#: therefore displace: a lapsed approval, a spent bound, a finished subject.
#: Everything else — a manual pause (empty reason), a research tombstone
#: (``AUTONUDGE_STOP_REASON``, consumed by the auto_research watchdog to tell
#: deliberate completion from crash cleanup), and any reason this version does
#: not know — is evidence some consumer may read, so it fails CLOSED to
#: preserved.
_REPLACEABLE_LOOP_STOP_REASONS = _TERMINAL_BOUND_REASONS | {MONITOR_TERMINAL_REASON}


def _stopped_row_is_replaceable(loop: "NudgeLoop") -> bool:
    """Whether a directive re-arm (``replace_stopped``) may displace this row.

    Callers have already established the row is INACTIVE. The split is by who
    recorded the stop: a stop the system imposed (bound expiry, approval
    stall, terminal subject, crash retirement) is automatically re-armable,
    while a stop a person or an app recorded — ``USER_STOP``, session-close
    retention, a manual pause, a research tombstone — is retained evidence.
    Unknown outcomes and reasons are treated as evidence (fail closed).
    """
    state = loop.monitor
    if state is not None and state.outcome is not None:
        if str(state.stopped_reason or "") == MONITOR_STOP_INVALID_RECORD:
            # A quarantined malformed record is an inspection artifact of a
            # store defect, not a system-imposed stop: _load() synthesized its
            # BLOCKED outcome precisely to retain the raw payload for a human.
            # The ruling's fail-closed principle covers it — evidence, never
            # replaceable.
            return False
        return state.outcome in (
            MonitorOutcome.BUDGET,
            MonitorOutcome.SUCCESS,
            MonitorOutcome.BLOCKED,
            # System-imposed too: a vanished or undeliverable subject
            # (dispatch failure, shadow NOT_FOUND). No consumer authored it,
            # so refusing re-creates the deadlock this predicate exists to end.
            MonitorOutcome.TARGET_UNAVAILABLE,
        )
    return (loop.stopped_reason or "") in _REPLACEABLE_LOOP_STOP_REASONS


# Namespaced session-key prefixes that identify messaging-channel sessions
# (as opposed to bare dashboard chat-slot keys). Channel-bound loops have no
# dashboard turn-lifecycle hooks (notify_turn_complete / notify_user_input),
# so they run on a fixed interval instead of an idle timer: the timer re-arms
# itself right after every delivered fire.
#
# This mirrors ``messaging.link.CHANNEL_SESSION_NAMESPACES``, spelled out here
# rather than derived from it, for two independent reasons:
#
# 1. IMPORT WEIGHT. ``autonudge`` is imported at module scope by ``mcp_core``
#    (i.e. by every MCP server process) and by the dashboard chat layer, and it
#    depends only on config/security/platform_compat today. Naming
#    ``kiro_crew.messaging.link`` runs ``messaging/__init__``, which pulls the
#    driver/renderer/transport layer and, transitively, the ACP client, agent,
#    hooks, artifacts, metrics and sqlite — measured at 48 additional
#    ``kiro_crew`` modules to obtain one tuple of string literals.
# 2. THIS IS A KEY-SHAPE QUESTION, NOT A LIVE-CAPABILITY ONE. ``is_channel_key``
#    selects the RE-ARM STRATEGY and the expiry-notification metadata, so it has
#    to answer identically whether or not the transport happens to be registered
#    at this instant. Deriving it from a runtime ``supports_proactive_send``
#    lookup fails toward the WRONG branch: a loop whose transport is momentarily
#    absent would read as a dashboard slot, so ``_run_fire_cycle`` would stop
#    self-re-arming it — and nothing else ever will, since
#    ``notify_turn_complete`` never fires for a channel key — while the expiry
#    notice would synthesize a ``dashboard:<namespace>:<id>`` jump link pointing
#    at no slot.
#
# Membership therefore does NOT assert deliverability; it asserts "this key names
# a conversation rather than a chat slot". Whether a nudge can actually be
# delivered stays with the fail-closed ladder in ``dashboard/chat_runner.py``
# (``_resolve_channel_target``: governance, then a REGISTERED transport, then
# ``supports_proactive_send``), which logs its reason and degrades to a no-op.
# So a namespace is listed even when nothing can currently be delivered to it,
# and the two clearest cases are both here: ``whatsapp`` has no transport package
# in this fork at all, and ``feishu`` ships one that declares
# ``supports_proactive_send=False`` (its renderer only replies to an inbound
# message id, so a nudge cycle has nowhere to put the answer). Both still classify
# as channel keys, because the alternative is worse than a refusal: an unlisted key
# is read as a dashboard slot and silently stops being re-armed, whereas a listed
# one reaches the ladder and is refused with a logged reason. Being listed is
# likewise not an arming permission — that is ``binding_key_for``, which is
# narrower still and gated on an ownership check and a fire route.
_CHANNEL_KEY_PREFIXES = (
    "slack:",
    "discord:",
    "telegram:",
    "wecom:",
    "whatsapp:",
    "webex:",
    "teams:",
    "weixin:",
    "imessage:",
    "feishu:",
    "unified:",
)


def is_channel_key(key: str) -> bool:
    """True when *key* names a messaging-channel session (``slack:<ts>``,
    ``discord:{agent}:direct:{user}`` ...) rather than a dashboard chat slot.

    A CLASSIFICATION, not a permission: see :data:`_CHANNEL_KEY_PREFIXES` for why
    the set is spelled out, and why membership says nothing about whether a nudge
    can be delivered. Callers asking "may this session be armed?" want
    :func:`binding_key_for` instead.
    """
    return key.startswith(_CHANNEL_KEY_PREFIXES)


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


def _maintenance_lock(base_dir: Path) -> asyncio.Lock:
    """Per-event-loop lock serializing store maintenance with service startup."""
    loop = asyncio.get_running_loop()
    path_key = os.path.normcase(os.path.abspath(str(base_dir)))
    return _MAINTENANCE_LOCKS.setdefault((loop, path_key), asyncio.Lock())


async def _cancel_and_drain_tasks(*tasks: asyncio.Task[Any]) -> bool:
    """Cancel child tasks without letting repeated cancellation abort cleanup."""
    for task in tasks:
        task.cancel()
    drain = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
    interrupted = False
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            interrupted = True
    drain.result()
    return interrupted


def get_instance() -> "AutoNudgeService | None":
    return _INSTANCE


def _current_task_or_none() -> "asyncio.Task[Any] | None":
    """:func:`asyncio.current_task`, or ``None`` when no loop is running.

    ``current_task`` raises ``RuntimeError: no running event loop`` outside a loop, and
    ``stop()`` is reached from SYNCHRONOUS callers — the gateway's shutdown path and test
    teardown — where nothing is running. There, no task can be "the current" one, which is
    the answer this returns rather than an exception the caller would have to know about.
    """
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@dataclass
class NudgeLoop:
    """A single auto-nudge loop bound to one session.

    ``slot_key`` is the binding key: either a bare dashboard chat-slot key
    (e.g. ``chat-1-1721...``, idle-timer driven via notify_turn_complete) or a
    namespaced messaging-channel session key (e.g. ``slack:<thread_ts>``,
    ``discord:{agent}:direct:{user_id}``), which runs on a fixed interval.
    The field keeps its historical name for store/REST/WS compatibility.
    """

    id: str
    slot_key: str
    message: str
    idle_secs: int = 60
    max_cycles: int = 0  # 0 = unlimited
    cycle_count: int = 0
    active: bool = True
    last_fire_ts: float = 0.0
    created_ts: float = 0.0
    stop_sentinel_path: str = ""  # optional absolute path; if present loop halts
    # Wall-clock budget in seconds, measured from ``created_ts`` (0 = unlimited).
    # A cycle cap alone cannot bound COST: a loop whose turns are slow or whose
    # idle gap is long can run for days within its cycle budget. Anchoring on
    # the persisted ``created_ts`` (not arm time) makes the budget restart-proof
    # — a gateway restart re-arms the loop but never resets its clock.
    max_runtime_secs: int = 0
    #: Whether this loop may be observation-gated. Defaults to FALSE, which is what
    #: a record stored before this field existed decodes to.
    #:
    #: THE PRINCIPLE, stated once because four review rounds circled it: gating is
    #: the state that can silently stop work -- a gated loop whose subject is merged
    #: or closed DEACTIVATES -- so every uncertainty resolves to UNGATED, and only an
    #: explicit boolean true gates. An absent key is a loop nobody chose to gate,
    #: usually a generic goal loop that predates the feature; a corrupt value is not
    #: a decision either. Being wrong in this direction costs a turn per interval,
    #: which is what today already costs. Being wrong the other way stops a
    #: recurring task because its instruction happened to mention a pull request.
    #:
    #: Persisted because the opt-out has to SURVIVE. The instruction is the target,
    #: so editing it re-infers the subject; without a remembered decision an
    #: explicitly ungated loop would be silently re-gated by the next wording
    #: change -- exactly the harm the opt-out exists to prevent, arriving through
    #: the documented way to revise a loop.
    gate: bool = False
    # WHY the loop was last deactivated: "" (active / never stopped),
    # "manual" (user pause / any caller that didn't say otherwise),
    # "autonudge_stop" (deliberate directive), "cycle_cap",
    # "runtime_budget", or "approval_stalled" (set by _timer's terminal
    # bounds).
    # Persisted so revival logic can distinguish a manual pause from a bound
    # expiry — elapsed wall-clock keeps growing after a manual pause, so
    # WITHOUT this record a paused loop whose budget has since elapsed is
    # indistinguishable from a budget-stopped one, and a budget raise would
    # resume unattended execution against the user's explicit pause.
    stopped_reason: str = ""
    # Evidence that a cycle in this loop's session asked for tool approval and
    # nobody answered within the window. Set by ``notify_approval_stalled`` and
    # consumed by ``_timer`` as a terminal condition on the NEXT wake, which is
    # the whole point: the loop stops on proof that it could not act, never on a
    # prediction that it might not be able to. A loop whose turns only touch
    # auto-approved tools never reaches an interactive wait, so it can never be
    # flagged here — that is what keeps a working read-only loop running instead
    # of needing a "does this loop need approval?" guess.
    # Persisted, because the condition that produced it (a lapsed grant) usually
    # outlives a restart; cleared on every revival so a re-granted loop is not
    # stopped by stale evidence.
    approval_stalled: bool = False
    # Absolute wall-clock deadline for the next fire (0 = unset: the next arm
    # starts a fresh full countdown). This is what makes the countdown
    # deadline-preserving — user turns cancel the pending timer TASK but never
    # touch this field, so the schedule survives an active conversation.
    # Cleared on every delivered fire (the next cycle is measured from the
    # nudge turn's END, whose timestamp is only known at notify_turn_complete).
    # Every assignment is persisted: add/update/fire bookkeeping write it
    # inline, and turn-lifecycle arms schedule a supervised background write,
    # so a restart resumes the countdown. A lost background write degrades to
    # a fresh full countdown after restart, never a lost or premature fire.
    next_due_ts: float = 0.0
    # Optional observation/controller state. ``gate=True`` records belong to the
    # prompt path; controller-owned records carry state with ``gate=False``.
    monitor: MonitorState | None = None
    # Optional SHORT stand-in for ``message`` in the VISIBLE dashboard
    # transcript row. Empty (the default) means the row is byte-identical to
    # what it has always been, so no existing loop changes behaviour.
    #
    # ``message`` serves two consumers with opposite needs. The model needs the
    # whole instruction re-delivered every cycle — that is the guarantee the
    # nudge exists to provide. A person reading the transcript needs only "a
    # nudge happened", yet gets the same multi-KB payload appended per cycle:
    # measured on one long-running loop, 44 nudge rows of ~7.9KB were 51.8% of
    # the entire 671,900-char session file.
    #
    # The PROMPT is never affected by this field (see
    # ``GatewayOrchestrator._fire_dashboard_nudge``): shortening the model's
    # copy would delete real instruction, which is the opposite of the point.
    # Scoped to the dashboard transcript row — channel-bound loops
    # (``slack:``/``discord:``/``webex:``) deliver the nudge as the turn's own
    # input and have no separate display surface to shorten.
    #
    # Appended LAST rather than placed beside ``message`` so a persisted store
    # written by this version still loads on a build that predates the field:
    # ``_load`` filters unknown keys, so a downgrade degrades to the verbose
    # display instead of raising.
    banner: str = ""


def is_structured_monitor_loop(loop: NudgeLoop) -> bool:
    """Distinguish controller records from prompt loops carrying probe state."""
    return getattr(loop, "monitor", None) is not None and not getattr(loop, "gate", False)


class MonitorUpdateConflict(ValueError):
    """A structured mutation would break active action correlation."""


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


def runtime_budget_exceeded(loop: "NudgeLoop", now: float | None = None) -> bool:
    """True when *loop* has a wall-clock budget and it is spent.

    Single source of truth shared by ``_timer`` (enforcement) and the expiry
    notifier (wording), so the two can never disagree on WHY a loop stopped.
    A loop with no ``created_ts`` (a malformed/legacy store entry) never
    trips the budget — there is no anchor to measure from, and guessing one
    could kill a healthy loop on its first cycle after an upgrade.
    """
    if not loop.max_runtime_secs or not loop.created_ts:
        return False
    return (now if now is not None else time.time()) - loop.created_ts >= loop.max_runtime_secs


@contextmanager
def _locked_file(path: Path, mode: str) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if "r" in mode and not path.exists():
        path.write_text(json.dumps({"version": _STORE_VERSION, "loops": []}))
    # "r" -> "r+": Windows msvcrt.locking requires WRITE access on the fd — a
    # read-only handle fails with EACCES, which platform_compat.file_lock
    # swallows (best-effort), silently degrading the reader's lock to a no-op
    # and letting a concurrent _save race the read (same fix as
    # apps/bridges.py:_mcp_lock). The shared/exclusive decision keys off the
    # ORIGINAL mode so a reader still requests a shared lock.
    exclusive = "w" in mode or "+" in mode
    if mode == "r":
        mode = "r+"
    with open(path, mode, encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=exclusive):
            yield fh


def infer_monitor(message: str, now: float) -> MonitorState | None:
    """Build a monitor for *message*'s subject, or ``None`` to stay ungated.

    ``None`` is the common, safe answer: a loop watching something with no probe
    -- a deployment, a ticket, a file -- keeps exactly the behaviour it had
    before this feature existed. Only a message that names ONE observable
    subject becomes a gated monitor.

    Public because the ARMING SURFACE has to report this same decision in its
    acknowledgement, and the reasons it can answer ``None`` are not all in
    :func:`targets.infer` -- a subject that will not form a valid monitor is
    another. An ack that re-derived the answer from the target alone could claim
    a gate the loop never got, which is the one thing a disclosure must not do.
    One function, one answer.

    Budgets are left at their defaults and are NOT enforced on this path. The
    default cap is 8 agent turns, and real babysit loops run for dozens of
    cycles, so enforcing it here would stop working watches early -- a
    regression wearing a budget's clothing. Enforcement belongs with the
    decision controller that owns the rest of the budget vocabulary, and is
    deliberately not smuggled in behind a token saving.
    """
    target = targets.infer(message)
    if target is None:
        return None
    try:
        return MonitorState(
            kind=target.kind,
            target=target.subject,
            objective="review_ready",
            created_ts=now,
        )
    except ValueError:
        # A subject that cannot form a valid monitor is not a reason to refuse
        # the loop the caller asked for. Arm it ungated.
        logger.warning("AutoNudge: inferred target %r rejected by MonitorState", target.subject)
        return None


class AutoNudgeService:
    """Manages reactive per-slot nudge loops with restart-survival."""

    def __init__(
        self,
        base_dir: Path | None = None,
        on_fire: Callable[[NudgeLoop], Awaitable[bool]] | None = None,
        on_monitor_tick: Callable[[NudgeLoop], Awaitable[None]] | None = None,
    ) -> None:
        self._base_dir = base_dir or config_dir()
        self._path = self._base_dir / _NUDGES_FILE
        self._on_fire = on_fire
        self._on_monitor_tick = on_monitor_tick
        self._loops: dict[str, NudgeLoop] = {}
        self._timers: dict[str, asyncio.Task] = {}
        # Loop ids whose re-arm was requested while their fire window was open.
        # Applied when the window closes (see _timer): a dashboard turn can
        # complete while the firing task is still persisting, and honouring the
        # hook immediately would cancel that task mid-persist.
        self._rearm_pending: set[str] = set()
        # Loop ids removed from memory whose durable state write has not yet
        # succeeded. A caller may retry remove(id) after the first write fails;
        # an arbitrary unknown id remains a no-op.
        self._pending_removals: set[str] = set()
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
        # Strong refs to in-flight shielded add() tasks: keeps a detached
        # mutation supervised (no GC, failures logged) even when every awaiting
        # caller was cancelled. Discarded on completion.
        self._inflight_adds: set = set()
        # Runtime turn-start evidence for the narrow window between a channel
        # accepting a claimed wake and the controller persisting DISPATCHED.
        # One monitor can own only one claim, so the loop id maps directly to
        # its accepted fingerprint. Durable delivery state remains authoritative
        # after the dispatcher returns or the process restarts.
        self._accepted_monitor_turns: dict[str, str] = {}
        self._observers: list[Callable[[str, NudgeLoop | None], None]] = []
        self._lock = asyncio.Lock()

    # ── Persistence ──

    def _load(self) -> None:
        """Read the store and repair each entry. BLOCKING — see ``start()``.

        Does file I/O (locked read) and, via ``repair_sentinel_path``, realpath
        resolution that can stall on an unavailable network mount, so callers on
        the event loop MUST offload this (``no-blocking-call-on-event-loop``).
        """
        with _locked_file(self._path, "r") as fh:
            data = json.load(fh)
        for raw in data.get("loops", []):
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
                # FALSE. Round 8 normalised it to True on the grounds that reading
                # corrupt data as an opt-out would ungate loops nobody chose to
                # ungate. That had the asymmetry backwards: gating is the state that
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
                loop = NudgeLoop(**loop_values)
                if "monitor" in raw:
                    monitor_raw = raw["monitor"]
                    monitor_quarantined = False
                    try:
                        loop.monitor = monitor_state_from_dict(monitor_raw)
                    except (TypeError, ValueError):
                        monitor_quarantined = True
                        loop.monitor = quarantine_monitor_state(monitor_raw)
                        loop.active = False
                        loop.next_due_ts = 0.0
                        self._store_dirty = True
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
                if isinstance(loop.message, str) and loop.message:
                    scrubbed_msg, _ = redact_exfiltration_urls(loop.message)
                    scrubbed_msg, _ = redact_credentials(scrubbed_msg)
                    if scrubbed_msg != loop.message:
                        loop.message = scrubbed_msg
                        self._store_dirty = True
            except Exception:
                logger.warning("AutoNudge: skipping malformed loop entry: %r", raw, exc_info=True)
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
                    loop.active = False
                self._store_dirty = True
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

    @classmethod
    @asynccontextmanager
    async def maintenance_service(
        cls, base_dir: Path | None = None
    ) -> AsyncIterator["_AutoNudgeMaintenanceView"]:
        """Yield one authoritative store view, serialized with startup and peers."""
        selected_dir = base_dir or data_home()
        async with _maintenance_lock(selected_dir):
            live = _INSTANCE
            if live is not None and live._base_dir == selected_dir:
                view = _AutoNudgeMaintenanceView(live)
                try:
                    yield view
                finally:
                    view._release()
                return
            offline = await cls.load_for_maintenance(base_dir=selected_dir)
            view = _AutoNudgeMaintenanceView(offline)
            try:
                yield view
            finally:
                view._release()

    def _serialize_state(self) -> dict:
        """Snapshot the store payload ON THE CALLER'S THREAD.

        Loop state is mutated only under the service lock on the event loop, so
        the serialization must happen there too — a worker thread iterating
        ``self._loops`` concurrently with a mutation would race. The returned
        payload is immutable-by-convention and safe to hand to an executor.
        """
        return {
            "version": _STORE_VERSION,
            "loops": [self._serialize_loop(lp) for lp in self._loops.values()],
        }

    @staticmethod
    def _serialize_loop(loop: NudgeLoop) -> dict[str, Any]:
        payload = asdict(loop)
        if loop.monitor is None:
            # Preserve the legacy wire shape instead of eagerly migrating every
            # record the next time an unrelated loop is saved.
            payload.pop("monitor", None)
        else:
            payload["monitor"] = monitor_state_to_dict(loop.monitor)
        return payload

    def _write_state(self, payload: dict) -> None:
        # Atomic write: serialize to a temp file in the same dir, fsync, then
        # replace onto the target path. Eliminates the truncate-before-
        # flock race that plain open(path, "w") has — readers always see either
        # the old complete file or the new complete file, never a partial one.
        # The rename goes through replace_with_retry because on Windows it can
        # fail with PermissionError while another handle is transiently open on
        # the fresh temp file (indexer / AV), which loses the write (issue #1105).
        # Blocking (fsync) — async callers offload this to an executor.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            replace_with_retry(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

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
                    self._arm_from_deadline(loop)
            global _INSTANCE
            _INSTANCE = self
        logger.info("AutoNudge started")

    def stop(self) -> None:
        # Through _cancel_timer, not a bare t.cancel() loop: shutdown is the likeliest
        # moment for a timer's loop to be closing already, and one cancellation policy
        # means this path inherits both of its guards instead of restating neither.
        # It pops as it goes, so iterate over a snapshot of the keys.
        for loop_id in list(self._timers):
            self._cancel_timer(loop_id)
        self._timers.clear()
        self._accepted_monitor_turns.clear()
        self._maintenance_quiescing.clear()
        self._maintenance_quiesce_events.clear()
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None

    # ── Loop CRUD ──

    def _begin_maintenance_quiesce(self, loop_id: str) -> None:
        self._maintenance_quiescing.add(loop_id)
        self._maintenance_quiesce_events.setdefault(loop_id, asyncio.Event()).set()

    def _end_maintenance_quiesce(self, loop_id: str) -> None:
        self._maintenance_quiescing.discard(loop_id)
        self._maintenance_quiesce_events.pop(loop_id, None)

    async def _acquire_mutation_lock(self, loop_id: str) -> asyncio.Lock | None:
        """Acquire the store mutex unless cleanup claims this loop first."""
        if loop_id in self._maintenance_quiescing:
            return None
        lock = _maintenance_lock(self._base_dir)
        event = self._maintenance_quiesce_events.setdefault(loop_id, asyncio.Event())
        acquire_task = asyncio.create_task(lock.acquire())
        quiesce_task = asyncio.create_task(event.wait())
        try:
            done, _pending = await asyncio.wait(
                {acquire_task, quiesce_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            await _cancel_and_drain_tasks(acquire_task, quiesce_task)
            if acquire_task.done() and not acquire_task.cancelled():
                lock.release()
            raise
        if acquire_task in done:
            interrupted = await _cancel_and_drain_tasks(quiesce_task)
            if interrupted:
                lock.release()
                raise asyncio.CancelledError()
            if loop_id not in self._maintenance_quiescing:
                return lock
            lock.release()
            return None
        interrupted = await _cancel_and_drain_tasks(acquire_task)
        if acquire_task.done() and not acquire_task.cancelled():
            lock.release()
        if interrupted:
            raise asyncio.CancelledError()
        return None

    async def add(
        self,
        slot_key: str,
        message: str,
        idle_secs: int = 60,
        max_cycles: int = 0,
        stop_sentinel_path: str = "",
        max_runtime_secs: int = 0,
        banner: str = "",
        admission_check: Callable[[], bool] | None = None,
        # UNGATED by default, and the default lives at the ARMING SURFACES instead.
        # The evidence for gating is about monitor_start -- a babysit loop whose work
        # IS the pull request. This service also arms loops whose work is not: a goal
        # loop, an app's own timer. Defaulting to gated here inferred a monitor from
        # any message that merely MENTIONED one PR, which throttles such a loop and,
        # if that PR is already merged, deactivates it before its first turn.
        gate: bool = False,
        replace_existing: bool = True,
        replace_stopped: bool = False,
    ) -> NudgeLoop:
        # CANCELLATION SAFETY: the mutate+persist runs as a SHIELDED task. If
        # the awaiting caller is cancelled mid-write, a bare await would release
        # ``_lock`` while the executor write is still running — a subsequent
        # add/update could persist newer state first and then be clobbered by
        # this operation's stale snapshot (lost update after restart). Shielding
        # keeps the inner task (and the lock) alive until the write completes,
        # so writes remain strictly serialized; the cancelled caller still sees
        # CancelledError, with the arm possibly landed (same "mutation may have
        # already landed" semantics as other cancellation-uncertain mutations).
        # The inner task is retained in ``_inflight_adds`` (discarded when done)
        # so it stays SUPERVISED — strongly referenced and completion-logged —
        # even if every awaiting caller has been cancelled.
        inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
            self._add_locked(
                slot_key,
                message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                stop_sentinel_path=stop_sentinel_path,
                max_runtime_secs=max_runtime_secs,
                banner=banner,
                admission_check=admission_check,
                gate=gate,
                replace_existing=replace_existing,
                replace_stopped=replace_stopped,
            )
        )
        self._inflight_adds.add(inner)

        def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("AutoNudge: detached add() failed", exc_info=t.exception())

        inner.add_done_callback(_finish)
        return await asyncio.shield(inner)

    async def add_monitor(
        self,
        *,
        slot_key: str,
        kind: str,
        target: str,
        objective: str,
        cadence_secs: int,
        budgets: MonitorBudgets,
        wake_instructions: str = "",
        now: float | None = None,
        replace_existing: bool = True,
        replace_stopped: bool = False,
        expected_existing_monitor_id: str | None = None,
        expected_existing_config_generation: int | None = None,
        admission_check: Callable[[], bool] | None = None,
    ) -> NudgeLoop:
        """Create one durable structured record without legacy prompt routing."""
        inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
            self._add_monitor_locked(
                slot_key=slot_key,
                kind=kind,
                target=target,
                objective=objective,
                cadence_secs=cadence_secs,
                budgets=budgets,
                wake_instructions=wake_instructions,
                now=now,
                replace_existing=replace_existing,
                replace_stopped=replace_stopped,
                expected_existing_monitor_id=expected_existing_monitor_id,
                expected_existing_config_generation=expected_existing_config_generation,
                admission_check=admission_check,
            )
        )
        self._inflight_adds.add(inner)

        def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("detached structured monitor add failed", exc_info=t.exception())

        inner.add_done_callback(_finish)
        return await asyncio.shield(inner)

    async def _add_monitor_locked(
        self,
        *,
        slot_key: str,
        kind: str,
        target: str,
        objective: str,
        cadence_secs: int,
        budgets: MonitorBudgets,
        wake_instructions: str,
        now: float | None,
        replace_existing: bool,
        replace_stopped: bool = False,
        expected_existing_monitor_id: str | None,
        expected_existing_config_generation: int | None,
        admission_check: Callable[[], bool] | None,
    ) -> NudgeLoop:
        created = time.time() if now is None else now
        cadence = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(cadence_secs)))
        async with _maintenance_lock(self._base_dir):
            async with self._lock:
                if admission_check is not None and not admission_check():
                    raise NudgeAdmissionRefused("session changed before monitor arm committed")
                existing = self._find_by_slot(slot_key)
                if expected_existing_monitor_id is not None:
                    existing_monitor = existing.monitor if existing is not None else None
                    if (
                        existing is None
                        or existing.id != expected_existing_monitor_id
                        or existing_monitor is None
                        or existing_monitor.config_generation != expected_existing_config_generation
                    ):
                        raise MonitorUpdateConflict("monitor changed before restart")
                if existing:
                    # Same split as the legacy add: create-only refuses ANY
                    # record unless the caller opted into ``replace_stopped``,
                    # which under the owner's ruling displaces only
                    # SYSTEM-imposed stops (a spent bound, a finished subject).
                    # A consumer-recorded stop — including a USER_STOP record
                    # retained by monitor_stop — is preserved; the dashboard
                    # restart route (conditional replace) is the sanctioned way
                    # to succeed one. Dashboard creates never opt in, so their
                    # any-record 409 keeps retained evidence intact. The
                    # wake-in-flight guard below still covers a terminal record
                    # that owns an accepted, uncompleted wake.
                    if not replace_existing and (existing.active or not replace_stopped):
                        raise MonitorUpdateConflict("session already has an automation")
                    existing_monitor = existing.monitor
                    if (
                        not replace_existing
                        and existing_monitor is not None
                        and existing_monitor.version != MONITOR_STATE_VERSION
                    ):
                        # Same rule as the legacy add: a future-version record
                        # is a newer gateway's state, retained inactive across a
                        # downgrade on purpose — never deletable by this one.
                        raise MonitorUpdateConflict(
                            "the session's stopped automation was written by a newer "
                            "gateway and cannot be replaced by this one"
                        )
                    if (
                        not replace_existing
                        and replace_stopped
                        and not _stopped_row_is_replaceable(existing)
                    ):
                        # Same owner ruling as the legacy add: consumer-recorded
                        # stops (USER_STOP, SESSION_CLOSE, tombstones) are
                        # evidence, never displaced by a re-arm.
                        raise MonitorUpdateConflict(
                            "the session's stopped automation is retained as evidence "
                            "and is not replaceable by a re-arm; its owner must clear "
                            "it first"
                        )
                    if existing_monitor is not None and existing_monitor.wake_in_flight:
                        raise MonitorUpdateConflict(
                            "existing monitor cannot be replaced while a wake is in flight"
                        )
                due = created + cadence
                monitor = MonitorState(
                    kind=kind,
                    target=target,
                    objective=objective,
                    created_ts=created,
                    budgets=budgets,
                    cadence_secs=cadence,
                    wake_instructions=wake_instructions,
                    next_probe_at=due,
                )
                loop = NudgeLoop(
                    id=uuid.uuid4().hex[:8],
                    slot_key=slot_key,
                    message="",
                    idle_secs=cadence,
                    created_ts=created,
                    next_due_ts=due,
                    monitor=monitor,
                )
                replacement_payload = {
                    "version": _STORE_VERSION,
                    "loops": [
                        self._serialize_loop(candidate)
                        for candidate in self._loops.values()
                        if existing is None or candidate.id != existing.id
                    ]
                    + [self._serialize_loop(loop)],
                }
                await self._write_monitor_snapshot_locked(replacement_payload)
                if existing is not None:
                    self.remove_sync(existing.id, persist=False)
                self._loops[loop.id] = loop
                if self._on_monitor_tick is not None:
                    self._arm_from_deadline(loop)
        self._emit("added", loop)
        return loop

    async def _add_locked(
        self,
        slot_key: str,
        message: str,
        *,
        idle_secs: int,
        max_cycles: int,
        stop_sentinel_path: str,
        max_runtime_secs: int = 0,
        banner: str = "",
        admission_check: Callable[[], bool] | None = None,
        gate: bool = False,
        replace_existing: bool = True,
        replace_stopped: bool = False,
    ) -> NudgeLoop:
        async with _maintenance_lock(self._base_dir):
            return await self._add_unserialized(
                slot_key,
                message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                stop_sentinel_path=stop_sentinel_path,
                max_runtime_secs=max_runtime_secs,
                banner=banner,
                admission_check=admission_check,
                gate=gate,
                replace_existing=replace_existing,
                replace_stopped=replace_stopped,
            )

    async def _add_unserialized(
        self,
        slot_key: str,
        message: str,
        *,
        idle_secs: int,
        max_cycles: int,
        stop_sentinel_path: str,
        max_runtime_secs: int = 0,
        banner: str = "",
        admission_check: Callable[[], bool] | None = None,
        gate: bool = False,
        replace_existing: bool = True,
        replace_stopped: bool = False,
    ) -> NudgeLoop:
        idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
        async with self._lock:
            if admission_check is not None and not admission_check():
                raise NudgeAdmissionRefused("session changed before nudge arm committed")
            # One loop per slot — replace any existing loop on this slot.
            # persist=False: the offloaded write below persists the combined
            # removal+add atomically, avoiding a duplicate blocking save here.
            existing = self._find_by_slot(slot_key)
            if existing:
                # Create-only (``replace_existing=False``) refuses ANY existing
                # record by default — the dashboard REST creates depend on that:
                # their documented contract is a 409 that never discards a
                # retained inspection record. ``replace_stopped`` is the
                # directive re-arm path's explicit opt-in to narrow the refusal
                # to ACTIVE records, because a retained INACTIVE row
                # (approval-stalled, capped, budget-spent, or a terminal record
                # kept for inspection) otherwise deadlocks the session's only
                # re-arm: monitor_update's approval-stall refusal names
                # monitor_start as the remedy. The wake-in-flight guard below
                # still runs for the replaced-inactive case, so a terminal
                # record whose accepted wake is awaiting completion evidence
                # keeps its own refusal rather than having its correlation
                # orphaned by a replacement.
                if not replace_existing and (existing.active or not replace_stopped):
                    raise MonitorUpdateConflict("session already has an automation")
                existing_monitor = existing.monitor
                if (
                    not replace_existing
                    and existing_monitor is not None
                    and existing_monitor.version != MONITOR_STATE_VERSION
                ):
                    # A future-version record belongs to the newer gateway that
                    # wrote it: _load() retains it inactive so an upgrade can
                    # resume the watch, and the retarget path refuses to touch
                    # it for the same reason. A stopped-replacement here would
                    # destroy state this gateway cannot even read. Checked
                    # BEFORE the evidence allowlist so the version message —
                    # the actionable one — wins for such records.
                    raise MonitorUpdateConflict(
                        "the session's stopped automation was written by a newer "
                        "gateway and cannot be replaced by this one"
                    )
                if (
                    not replace_existing
                    and replace_stopped
                    and not _stopped_row_is_replaceable(existing)
                ):
                    # Owner ruling (option A): only system-imposed stops are
                    # re-armable. A stop recorded FOR a consumer — a research
                    # tombstone, a manual pause, a user stop, session-close
                    # retention — is evidence, and an unknown reason is
                    # treated as evidence too.
                    raise MonitorUpdateConflict(
                        "the session's stopped automation is retained as evidence "
                        f"(stop reason: {existing.stopped_reason or 'manual'!s}) and is "
                        "not replaceable by a re-arm; its owner must clear it first"
                    )
                if existing_monitor is not None and existing_monitor.wake_in_flight:
                    raise MonitorUpdateConflict(
                        "existing monitor cannot be replaced while a wake is in flight"
                    )
                self.remove_sync(existing.id, persist=False, emit=False)
            now = time.time()
            loop = NudgeLoop(
                id=uuid.uuid4().hex[:8],
                slot_key=slot_key,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max(0, int(max_cycles)),
                created_ts=now,
                stop_sentinel_path=stop_sentinel_path,
                max_runtime_secs=max(0, int(max_runtime_secs)),
                # Anchor the first deadline at arm time (set BEFORE the
                # snapshot below so it persists): the countdown starts the
                # moment the loop is armed, and user turns from here on only
                # defer delivery, never restart it.
                next_due_ts=now + idle_secs,
                # The SUBJECT is decided HERE, from the instruction the caller
                # already wrote -- no target, kind or enable flag is ever passed.
                # WHETHER to look for one is the ``gate`` argument above, which the
                # arming surfaces set and this service defaults to False; saying
                # "rather than from a parameter" was true before that default moved
                # and is not any more. What has never been a parameter, and is the
                # point, is the subject: every earlier attempt at this saving
                # shipped as an opt-in and measured zero adoption -- the switch
                # existed, the agent arming the loop was mid-task, and nothing made
                # it worth its five steps. There is no SUBJECT parameter to forget
                # here: whatever the caller already wrote is where the target comes
                # from, on every surface. Gating itself is no longer inherited by
                # construction, though -- that claim was true before the default
                # moved and is not now. Each arming surface chooses: monitor_start's
                # directive gates by default, the generic REST route does not.
                #
                # ``gate=False`` is the one escape, and it is an opt-OUT of a
                # default that lives at the ARMING SURFACE: monitor_start's own
                # directive gates unless told otherwise, while this service and the
                # generic REST route default to ungated -- they also arm loops whose
                # work is not a pull request. So it cannot repeat the zero-adoption
                # failure on the babysit path, which is the path the evidence is
                # about. The escape exists because a loop whose duty is to act WHILE
                # its subject is quiet is invisible to an observation of that
                # subject; keying that only on the wording of the instruction made a
                # cadence contract depend on prose.
                monitor=infer_monitor(message, now) if gate else None,
                gate=gate,
                banner=banner,
            )
            self._loops[loop.id] = loop
            # Persist WITHOUT blocking the event loop (no-blocking-call rule:
            # _write_state fsyncs, and a wedged disk must not freeze the
            # gateway). Snapshot under the lock (mutation safety), write on a
            # worker thread, and await it so a persistence failure still
            # propagates to the caller before the loop is reported armed.
            payload = self._serialize_state()
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
            except BaseException:
                self._loops.pop(loop.id, None)
                if existing is not None:
                    self._loops[existing.id] = existing
                    if existing.active:
                        self._arm_from_deadline(existing)
                raise
            self._arm_from_deadline(loop)
            if existing is not None:
                self._emit("removed", existing)
        self._emit("added", loop)
        logger.info("AutoNudge: added loop %s on slot %s (idle=%ds)", loop.id, slot_key, idle_secs)
        return loop

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

    async def update(
        self,
        loop_id: str,
        *,
        message: str | None = None,
        idle_secs: int | None = None,
        max_cycles: int | None = None,
        active: bool | None = None,
        max_runtime_secs: int | None = None,
        stopped_reason: str | None = None,
        banner: str | None = None,
    ) -> NudgeLoop | None:
        # CANCELLATION SAFETY: same contract as add(). The mutate+persist runs
        # as a SHIELDED, supervised task so a caller cancelled mid-write cannot
        # release ``_lock`` while the executor write is still in flight — which
        # would let a later write land first and then be clobbered by this
        # operation's stale snapshot (lost update after restart).
        inner: "asyncio.Task[NudgeLoop | None]" = asyncio.ensure_future(
            self._update_locked(
                loop_id,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                active=active,
                max_runtime_secs=max_runtime_secs,
                stopped_reason=stopped_reason,
                banner=banner,
            )
        )
        self._inflight_adds.add(inner)

        def _finish(t: "asyncio.Task[NudgeLoop | None]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("AutoNudge: detached update() failed", exc_info=t.exception())

        inner.add_done_callback(_finish)
        return await asyncio.shield(inner)

    async def deactivate_and_wait(self, loop_id: str) -> bool:
        """Persistently pause a loop and wait for its current timer to quiesce.

        ``update(active=False)`` deliberately does not cancel a timer already
        inside its fire callback because channel turns run inline there.  A
        cleanup caller has a different need: it must know that a dashboard fire
        can no longer materialize a slot after the caller takes its snapshot.
        The loop remains durably present and inactive until the caller removes
        it, so a timeout or process exit leaves a restart-visible recovery
        marker instead of losing the orphan's only identity.
        """
        timer_before = self._timers.get(loop_id)
        loop = await self.update(loop_id, active=False)
        if loop is None:
            return False
        # A turn-complete notification can replace the timer while update()
        # waits to acquire and persist the inactive state. Once update returns,
        # active=False prevents any further replacement, so both tasks close
        # the final slot-publication window.
        timer_after = self._timers.get(loop_id)
        current = asyncio.current_task()
        for timer in {timer_before, timer_after}:
            if timer is not None and timer is not current and not timer.done():
                await asyncio.shield(timer)
        return True

    async def _deactivate_and_wait_unserialized(self, loop_id: str) -> bool:
        """Quiesce a loop while the caller owns the maintenance transaction."""
        self._begin_maintenance_quiesce(loop_id)
        timer_before = self._timers.get(loop_id)
        inner = asyncio.create_task(self._update_unserialized(loop_id, active=False))
        try:
            loop = await asyncio.shield(inner)
        except asyncio.CancelledError:
            # maintenance_service() must not release its transaction while the
            # executor-backed write can still commit a stale snapshot.
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    continue
            inner.result()
            raise
        if loop is None:
            return False
        timer_after = self._timers.get(loop_id)
        current = asyncio.current_task()
        for timer in {timer_before, timer_after}:
            if timer is not None and timer is not current and not timer.done():
                await asyncio.shield(timer)
        return True

    async def _update_locked(
        self,
        loop_id: str,
        *,
        message: str | None = None,
        idle_secs: int | None = None,
        max_cycles: int | None = None,
        active: bool | None = None,
        max_runtime_secs: int | None = None,
        stopped_reason: str | None = None,
        banner: str | None = None,
    ) -> NudgeLoop | None:
        lock = await self._acquire_mutation_lock(loop_id)
        if lock is None:
            return None
        try:
            return await self._update_unserialized(
                loop_id,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                active=active,
                max_runtime_secs=max_runtime_secs,
                stopped_reason=stopped_reason,
                banner=banner,
            )
        finally:
            lock.release()

    async def _update_unserialized(
        self,
        loop_id: str,
        *,
        message: str | None = None,
        idle_secs: int | None = None,
        max_cycles: int | None = None,
        active: bool | None = None,
        max_runtime_secs: int | None = None,
        stopped_reason: str | None = None,
        banner: str | None = None,
    ) -> NudgeLoop | None:
        async with self._lock:
            loop = self._loops.get(loop_id)
            if not loop:
                return None
            if is_structured_monitor_loop(loop):
                # Generic update owns only legacy prompt loops. Reject before
                # touching even one shared scheduling field so a non-HTTP
                # caller cannot bypass structured policy.
                return loop
            # Keep typed nested values intact. ``asdict`` recursively converts
            # MonitorState to a plain dict, which is not a valid rollback value.
            previous = {item.name: getattr(loop, item.name) for item in fields(loop)}
            # Set only if a retarget takes this loop's pending wake claim, so the
            # rollback below restores exactly what it removed and nothing else.
            claim_discarded_for_retarget = False
            floor_discarded_for_retarget = False
            was_active = loop.active
            if message is not None:
                retarget = message != loop.message
                loop.message = message
                if retarget:
                    # The instruction IS the target, so a changed instruction can
                    # change the subject. Re-infer, or the loop keeps polling the
                    # pull request it was armed on: the new subject is never
                    # watched, and the old one merging would retire the loop while
                    # the work it was retargeted to sits unobserved.
                    #
                    # An unchanged subject keeps its existing monitor rather than a
                    # fresh one -- refining the wording of an instruction about the
                    # same PR is the common use of this path, and rebuilding would
                    # discard the metering counters and the follow-up allowance for
                    # no reason. A message that no longer names one subject clears
                    # the monitor, which returns the loop to a plain timer.
                    #
                    # A loop armed with gate=False is never re-inferred here. Its
                    # caller said the cadence matters, and re-gating it because the
                    # wording changed would revoke that through the documented way
                    # to revise a loop -- silently, since an ungated loop and a
                    # re-gated one look identical until the turns stop arriving.
                    inferred = infer_monitor(message, time.time()) if loop.gate else None
                    current = loop.monitor
                    # The stored spelling is a canonical shorthand and cannot
                    # express a HOST, so kind and target alone would call an edit
                    # from an enterprise shorthand to the same public slug
                    # "unchanged" and keep polling the wrong server. This is the
                    # third of the three places that comparison had to reach; the
                    # other two are the post-poll binding and the dedupe identity.
                    old_probe = targets.infer(str(previous.get("message") or ""))
                    new_probe = targets.infer(message)
                    same_host = (old_probe.host_key if old_probe else None) == (
                        new_probe.host_key if new_probe else None
                    )
                    same_subject = (
                        inferred is not None
                        and current is not None
                        and current.kind == inferred.kind
                        and current.target == inferred.target
                        and same_host
                    )
                    if not same_subject:
                        if current is not None and current.version != MONITOR_STATE_VERSION:
                            # A FUTURE version cannot be interpreted here, so it must
                            # not be REPLACED here either. The revival guard below
                            # already refuses to touch such a record, on the grounds
                            # that the stored intent belongs to the newer gateway that
                            # wrote it -- but that guard runs after this assignment,
                            # so a downgraded gateway destroyed the payload before the
                            # rule protecting it ever applied. Same rule, second
                            # surface: leave the record alone and let the message
                            # change without rebinding the watch.
                            logger.info(
                                "AutoNudge: loop %s carries a monitor from version %d, so "
                                "its retarget is refused rather than overwriting state "
                                "this gateway cannot read",
                                loop.id,
                                current.version,
                            )
                        else:
                            # A wake claimed for the OLD subject must not be spent on
                            # the new one. The claim is keyed by loop id, so without
                            # this the in-flight turn's delivery charges a wake to a
                            # monitor that has observed nothing, and grants it a
                            # follow-up allowance it never earned. Remembered so the
                            # persistence rollback below can hand it back if this
                            # retarget never lands.
                            claim_discarded_for_retarget = loop.id in self._pending_monitor_wake
                            self._pending_monitor_wake.discard(loop.id)
                            # And the floor claim, for the identical reason. I added
                            # that second claim one round ago and wrote on the pull
                            # request that two hand-written claim sets with two release
                            # points would go wrong at the third site; this IS that
                            # site, missed by the same change that predicted it. Third
                            # time a claim has been released in one set and forgotten
                            # in another.
                            floor_discarded_for_retarget = loop.id in self._pending_floor_tick
                            self._pending_floor_tick.discard(loop.id)
                            loop.monitor = inferred
            if banner is not None:
                # Display-only, so no deadline or timer consequence — unlike
                # ``idle_secs`` below, quieting a running loop must not restart
                # its countdown. "" clears it back to the verbose default.
                loop.banner = banner
            interval_changed = False
            if idle_secs is not None:
                new_idle = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
                interval_changed = new_idle != loop.idle_secs
                loop.idle_secs = new_idle
            if max_cycles is not None:
                loop.max_cycles = max(0, int(max_cycles))
            if max_runtime_secs is not None:
                loop.max_runtime_secs = max(0, int(max_runtime_secs))
            if active is not None:
                if (
                    active
                    and loop.monitor is not None
                    and loop.monitor.version != MONITOR_STATE_VERSION
                ):
                    # A FUTURE version cannot be interpreted here. IGNORE the
                    # flag -- deliberately without touching ``loop.active`` --
                    # because the stored intent belongs to the newer gateway that
                    # wrote it and will resume it. Forcing it off would let an
                    # older process silently retire a watch it cannot even read.
                    pass
                elif active and loop.monitor is not None and loop.monitor.outcome is not None:
                    # A monitor with an outcome is finished -- its subject merged,
                    # or its budget is spent. Reviving it would fire ungated
                    # prompts at a settled subject, because the tick gate
                    # declines to observe a monitor that already has an outcome.
                    #
                    # A current, unsettled monitor falls through to the ordinary
                    # activation below: its delivery is gated per tick, so
                    # resuming it cannot inject an ungated prompt. Spelling both
                    # refusals out separately matters -- collapsing them into one
                    # branch on ``loop.monitor is not None`` swallowed the
                    # revival entirely, leaving the loop neither refused nor
                    # activated.
                    loop.active = False
                    loop.next_due_ts = 0.0
                # TERMINAL-TRANSITION ATOMICITY: a bound-tagged deactivation
                # (stopped_reason supplied — the _timer's cycle_cap /
                # runtime_budget paths) must never OVERWRITE a deactivation
                # that landed first. The race: user pauses right after the
                # timer detects expiry — the pause persists "manual" and
                # cancels the timer, but the timer's already-inflight shielded
                # update would stamp "runtime_budget" over it, making the loop
                # budget-revivable against an explicit pause. Both transitions
                # serialize on _lock, so re-checking here closes the race: the
                # bound's deactivation degrades to a no-op when the loop is
                # already inactive. The reverse order is already safe — a
                # manual pause overwriting a bound tag only ever NARROWS
                # revivability ("manual" never auto-revives).
                elif (
                    not active
                    and stopped_reason is None
                    and loop.stopped_reason == AUTONUDGE_STOP_REASON
                ):
                    # A reasonless repeat of an already-inactive state is not a
                    # new stop transition. Preserve source-owned completion
                    # evidence until its Research Lab watchdog consumes it;
                    # dashboard retries and unrelated patches must not turn a
                    # deliberate stop into a revivable manual pause.
                    logger.info(
                        "AutoNudge: loop %s retains its source stop reason on "
                        "reasonless inactive update",
                        loop.id,
                    )
                elif stopped_reason in _TERMINAL_BOUND_REASONS and not active and not loop.active:
                    logger.info(
                        "AutoNudge: loop %s already deactivated (%s) — %s bound "
                        "not overwriting it",
                        loop.id,
                        loop.stopped_reason or "manual",
                        stopped_reason,
                    )
                else:
                    loop.active = bool(active)
                    # Record WHY on every deactivation and clear it on every
                    # revival, so the store always reflects the LAST transition.
                    # ``stopped_reason`` is an internal caller parameter (_timer's
                    # terminal bounds pass "cycle_cap"/"runtime_budget"); external
                    # deactivations (REST pause, deactivate-mid-fire) default to
                    # "manual", which the revive logic never auto-resumes.
                    if loop.active:
                        loop.stopped_reason = ""
                        # Spent only by an actual REVIVAL, hence ``not
                        # was_active``. A still-active loop also receives
                        # ``active=True`` from an ordinary settings save (the
                        # goal popover sends it on every edit), and treating
                        # that as an answer would erase evidence recorded
                        # moments earlier and let one more doomed cycle fire.
                        # Keeping it costs at most a resumable stop the operator
                        # can undo; dropping it costs a wasted cycle and the
                        # silence this stop exists to end.
                        if not was_active:
                            loop.approval_stalled = False
                    else:
                        loop.stopped_reason = stopped_reason or "manual"
            revived = loop.active and not was_active
            # Deadline bookkeeping (BEFORE the snapshot below so it persists):
            # an interval change restarts an EXISTING countdown at the new
            # interval — the old deadline encodes the old cadence and honouring
            # it would make the new setting take a full stale cycle to apply.
            # Any other patch (message edit, cap raise) keeps the deadline, so
            # a monitor_update refining the instruction never delays the next
            # check. Deactivation clears it — a paused loop holds no schedule.
            # A deadline that is ALREADY cleared (a delivered fire whose turn
            # is still running — nudge turns commonly call monitor_update)
            # stays cleared: the turn's END anchors the next full countdown
            # via notify_turn_complete, and assigning here would start the
            # interval mid-turn, so a turn longer than the interval would be
            # followed by a spurious overdue fire instead of a full cycle.
            if not loop.active:
                loop.next_due_ts = 0.0
            elif interval_changed and loop.next_due_ts > 0:
                loop.next_due_ts = time.time() + loop.idle_secs
            # Persist WITHOUT blocking the event loop — _write_state fsyncs, and
            # a wedged disk must not freeze chat/heartbeat/liveness. Snapshot
            # under THIS lock hold (mutation safety + serialization vs the
            # post-fire write) and await the offloaded write so a persistence
            # failure still reaches the caller. Same contract as _add_locked.
            payload = self._serialize_state()
            claim_was_held = claim_discarded_for_retarget
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
            except BaseException:
                for field_name, value in previous.items():
                    setattr(loop, field_name, value)
                if claim_was_held:
                    # The retarget above dropped this loop's pending wake claim,
                    # because a claim earned by the OLD subject must not be spent on
                    # the new one. If the write then fails the retarget did not
                    # happen -- so the claim belongs to the loop again, and leaving
                    # it discarded costs the delivered wake its accounting and its
                    # follow-up turn. Rolling back the fields but not this is the
                    # same incomplete-restore defect as the terminal transition's.
                    self._pending_monitor_wake.add(loop.id)
                if floor_discarded_for_retarget:
                    # Same restore, same reason. This is the fourth hand-written
                    # restore of a per-loop claim in this file, and the review has now
                    # found a claim missing from one of them three separate times --
                    # which is the argument for one transition object with one restore
                    # rather than a fifth.
                    self._pending_floor_tick.add(loop.id)
                raise
            # Re-arm the timer with the new settings — but NEVER while its
            # callback is mid-fire. Cancelling a firing timer cancels the
            # in-flight turn itself (channel loops run the turn inline in
            # _on_fire), destroying the response and the cycle accounting. A
            # firing timer re-arms itself on every exit path anyway (backoff
            # re-arm when undelivered, self-re-arm for channel keys,
            # notify_turn_complete for dashboard slots), and each of those reads
            # the freshly-updated idle_secs/active, so the new settings still
            # take effect on the next cycle.
            if loop.id in self._firing:
                logger.info(
                    "AutoNudge: loop %s updated mid-fire — deferring re-arm to the "
                    "running timer so the in-flight turn is not cancelled",
                    loop.id,
                )
            else:
                self._cancel_timer(loop_id)
                # Arm only when a schedule exists (deadline set) or this update
                # REVIVED the loop (fresh full countdown for a paused loop —
                # nothing else will arm it). An active loop with a cleared
                # deadline is a delivered fire whose turn is still running;
                # notify_turn_complete owns its next arm (see the deadline
                # bookkeeping above), so arming here would anchor the interval
                # mid-turn.
                if loop.active and (loop.next_due_ts > 0 or revived):
                    self._arm_from_deadline(loop)
        self._emit("updated", loop)
        return loop

    def remove_sync(
        self, loop_id: str, *, persist: bool = True, emit: bool = True
    ) -> NudgeLoop | None:
        """Remove a loop. ``persist=False`` skips the blocking save — used by
        async callers that snapshot+offload the write themselves right after."""
        loop = self._loops.pop(loop_id, None)
        if loop is None:
            return None
        self._cancel_timer(loop_id)
        self._rearm_fail_count.pop(loop_id, None)
        self._rearm_pending.discard(loop_id)
        self._accepted_monitor_turns.pop(loop_id, None)
        if persist:
            self._save()
        if emit:
            self._emit("removed", loop)
        return loop

    async def remove(self, loop_id: str) -> None:
        lock = await self._acquire_mutation_lock(loop_id)
        if lock is None:
            return
        try:
            await self._remove_unserialized(loop_id)
        finally:
            lock.release()

    async def remove_by_slot(self, slot_key: str) -> NudgeLoop | None:
        """Retire the current slot generation inside one maintenance transaction."""
        async with _maintenance_lock(self._base_dir):
            loop = self._find_by_slot(slot_key)
            if loop is None:
                return None
            if is_structured_monitor_loop(loop):
                await self.retire_monitor_for_session_close(loop.id)
            else:
                await self._remove_unserialized(loop.id)
            return loop

    async def _remove_unserialized(self, loop_id: str) -> None:
        async with self._lock:
            existed = loop_id in self._loops
            if not existed and loop_id not in self._pending_removals:
                return
            # Remove in-memory but SKIP the blocking save: _save() -> _write_state
            # fsyncs, and a wedged disk must not freeze the event loop. Snapshot
            # under THIS lock hold (serialization vs the post-fire write). Keep
            # the removal INLINE (not a separate task) so _cancel_timer's
            # "never cancel the current task" self-guard still applies when
            # _timer removes its own loop.
            removed_loop: NudgeLoop | None = None
            if existed:
                removed_loop = self.remove_sync(loop_id, persist=False, emit=False)
                self._pending_removals.add(loop_id)
            payload = self._serialize_state()
            fut = asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)

            def _restore_failed_removal() -> None:
                self._pending_removals.discard(loop_id)
                if removed_loop is None:
                    return
                self._loops[loop_id] = removed_loop
                if removed_loop.active:
                    self._arm_from_deadline(removed_loop)

            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                # Caller cancelled mid-write: the executor thread can't be
                # cancelled and is still fsyncing. shield re-raised on us
                # immediately, so DRAIN the write to completion before this
                # `async with` exits and releases _lock — otherwise a waiter
                # (add()/update()/_persist_locked) could acquire the lock and
                # race a second os.replace(), clobbering newer state with this
                # stale removal snapshot ("lost update after restart"). Then
                # propagate the cancellation.
                while not fut.done():
                    try:
                        await asyncio.shield(fut)
                    except asyncio.CancelledError:
                        continue
                try:
                    fut.result()
                except Exception:
                    _restore_failed_removal()
                    raise
                self._pending_removals.discard(loop_id)
                if removed_loop is not None:
                    self._emit("removed", removed_loop)
                raise
            except Exception:
                # Persistence is the commit point. Restore the live row (and
                # its timer when it was active) so an immediate retry can still
                # see the same loop the durable store retained.
                _restore_failed_removal()
                raise
            else:
                self._pending_removals.discard(loop_id)
                if removed_loop is not None:
                    self._emit("removed", removed_loop)

    def get_by_id(self, loop_id: str) -> NudgeLoop | None:
        """The loop with this id, or ``None``.

        Public because the update authorizer holds only an opaque ``loop_id`` and
        must resolve it to a slot key to decide whether a banner is supported
        there. An accessor rather than reaching into ``_loops`` from another
        module, matching ``get_by_slot``/``list_all``. Returns the LIVE object,
        not a copy; callers here only read from it.
        """
        return self._loops.get(loop_id)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return self._find_by_slot(slot_key)

    def list_all(self) -> list[NudgeLoop]:
        return list(self._loops.values())

    def _monitor_snapshot_with_replacement(
        self,
        loop: NudgeLoop,
        replacement: NudgeLoop,
    ) -> dict:
        """Serialize one staged monitor replacement without changing live state."""
        return {
            "version": _STORE_VERSION,
            "loops": [
                self._serialize_loop(replacement if candidate.id == loop.id else candidate)
                for candidate in self._loops.values()
            ],
        }

    def _apply_staged_monitor(self, loop: NudgeLoop, staged: NudgeLoop) -> None:
        """Publish a durable staged transition while preserving live object identity."""
        state = loop.monitor
        staged_state = staged.monitor
        if state is None or staged_state is None:
            raise ValueError("structured monitor replacement requires monitor state")
        for loop_field in fields(NudgeLoop):
            if loop_field.name != "monitor":
                setattr(loop, loop_field.name, deepcopy(getattr(staged, loop_field.name)))
        for state_field in fields(MonitorState):
            setattr(
                state,
                state_field.name,
                deepcopy(getattr(staged_state, state_field.name)),
            )

    async def _persist_staged_monitor_locked(
        self,
        loop: NudgeLoop,
        staged: NudgeLoop,
    ) -> None:
        """Persist a complete replacement before publishing it to live readers."""
        payload = self._monitor_snapshot_with_replacement(loop, staged)
        try:
            await self._write_monitor_snapshot_locked(payload)
        except asyncio.CancelledError:
            # The snapshot writer propagates cancellation only after draining
            # the executor write. Publish the state that is already durable
            # before preserving the caller's cancellation.
            self._apply_staged_monitor(loop, staged)
            raise
        self._apply_staged_monitor(loop, staged)

    async def apply_monitor_probe(
        self,
        monitor_id: str,
        result: GitHubPullRequestProbeResult,
        *,
        now: float,
        config_generation: int,
    ) -> MonitorDecision:
        """Persist one probe decision and any wake claim as one transition."""
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if loop is None or state is None or not loop.active or state.outcome is not None:
                return MonitorDecision.STOP_BLOCKED
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            if state.config_generation != config_generation:
                self._set_monitor_deadline(staged, now + staged_state.cadence_secs)
                decision = MonitorDecision.NO_CHANGE
            elif state.wake_in_flight:
                return MonitorDecision.NO_CHANGE
            else:
                decision = decide_monitor(staged_state, result.observation, now=now)
                staged_state.probe_count += 1
                staged_state.last_probe_at = now
                staged_state.last_decision = decision
                observation = result.observation
                provider_error = (
                    observation.provider_error or observation.supplemental_provider_error
                )
                if provider_error is not None:
                    staged_state.provider_error_count += 1
                    staged_state.consecutive_provider_errors += 1
                    staged_state.last_provider_error = provider_error
                else:
                    staged_state.consecutive_provider_errors = 0
                    staged_state.last_provider_error = None
                if observation.status is not MonitorObservationStatus.PROVIDER_ERROR:
                    staged_state.last_observation = deepcopy(result.canonical)
                    staged_state.last_fingerprint = observation.fingerprint
                    staged_state.last_observed_at = now

                if decision in {MonitorDecision.NO_CHANGE, MonitorDecision.RECORD_ONLY}:
                    self._set_monitor_deadline(staged, now + staged_state.cadence_secs)
                elif decision is MonitorDecision.RETRY_PROVIDER:
                    shift = max(0, staged_state.consecutive_provider_errors - 1)
                    retry = min(
                        _MONITOR_RETRY_MAX_BACKOFF_SECS,
                        _MONITOR_RETRY_BACKOFF_SECS * (2 ** min(shift, _REARM_BACKOFF_MAX_SHIFT)),
                        staged_state.cadence_secs,
                    )
                    self._set_monitor_deadline(staged, now + retry)
                elif decision is MonitorDecision.WAKE_ACTIONABLE:
                    staged_state.last_wake_fingerprint = observation.fingerprint
                    staged_state.last_wake_reason_code = observation.reason_code
                    staged_state.wake_in_flight = True
                    staged_state.wake_delivery = None
                    self._set_monitor_deadline(staged, 0.0)
                elif decision is MonitorDecision.STOP_BUDGET:
                    reason = monitor_budget_reason(staged_state, now=now)
                    self._apply_monitor_budget_stop(staged, reason, stopped_at=now)
                else:
                    staged.active = False
                    self._set_monitor_deadline(staged, 0.0)
                    staged_state.outcome = (
                        MonitorOutcome.SUCCESS
                        if decision is MonitorDecision.STOP_SUCCESS
                        else MonitorOutcome.BLOCKED
                    )
                    staged_state.stopped_reason = observation.reason_code or "monitor_blocked"
                    staged_state.stopped_at = now
            # Every probe advances durable inspection state and the next
            # deadline. Persist before publishing so a restart cannot restore
            # an overdue schedule and repeat an unchanged probe early.
            await self._persist_staged_monitor_locked(loop, staged)
            if not loop.active:
                self._sync_terminal_completion_timer(loop)
        self._emit("updated", loop)
        return decision

    def _set_monitor_deadline(self, loop: NudgeLoop, deadline: float) -> None:
        """Write the scheduler authority and inspection mirror together."""
        loop.next_due_ts = deadline
        if loop.monitor is not None:
            loop.monitor.next_probe_at = deadline

    async def stop_monitor(
        self,
        monitor_id: str,
        *,
        now: float | None = None,
        user_reason: str = "",
    ) -> NudgeLoop | None:
        """Retain a structured record with a durable user-stop outcome."""
        stopped_at = time.time() if now is None else now
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if loop is None or state is None:
                return None
            if state.outcome is not None:
                return loop
            stopped = deepcopy(loop)
            self._apply_monitor_user_stop(stopped, stopped_at=stopped_at)
            assert stopped.monitor is not None
            stopped.monitor.user_stop_reason = user_reason
            # Keep the live state and timer untouched until the terminal
            # snapshot is durable. A failed write must leave memory matching
            # the still-active record on disk so restart cannot resurrect work
            # the current process already considers stopped.
            await self._persist_staged_monitor_locked(loop, stopped)
            self._sync_terminal_completion_timer(loop)
        self._emit("updated", loop)
        return loop

    async def retire_monitor_for_session_close(
        self, monitor_id: str, *, now: float | None = None
    ) -> NudgeLoop | None:
        """Retain a terminal session-close record while disarming its timer."""
        stopped_at = time.time() if now is None else now
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if loop is None or state is None:
                return None
            if state.outcome is not None:
                return loop
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            staged.active = False
            self._retain_accepted_terminal_completion(staged, stopped_at=stopped_at)
            staged_state.outcome = MonitorOutcome.SESSION_CLOSE
            staged_state.stopped_reason = MONITOR_STOP_SESSION_CLOSE
            staged_state.stopped_at = stopped_at
            await self._persist_staged_monitor_locked(loop, staged)
            self._sync_terminal_completion_timer(loop)
        self._emit("updated", loop)
        return loop

    async def restore_monitor_after_failed_session_close(
        self,
        monitor_id: str,
        *,
        now: float | None = None,
        admission_check: Callable[[], bool] | None = None,
    ) -> NudgeLoop | None:
        """Rollback only the close-owned terminal transition after close failure."""
        restored_at = time.time() if now is None else now
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if loop is None or state is None or state.outcome is not MonitorOutcome.SESSION_CLOSE:
                return None
            if admission_check is not None and not admission_check():
                return None
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            staged.active = True
            staged_state.outcome = None
            staged_state.stopped_reason = ""
            staged_state.stopped_at = 0.0
            if (
                staged_state.wake_in_flight
                and staged_state.wake_delivery is MonitorDispatchResult.DISPATCHED
                and staged_state.completion_evidence_deadline > 0
            ):
                deadline = staged_state.completion_evidence_deadline
            elif staged_state.wake_in_flight:
                staged_state.wake_delivery = MonitorDispatchResult.BUSY
                staged_state.completion_evidence_deadline = 0.0
                deadline = restored_at + min(
                    MONITOR_BUSY_RETRY_SECS,
                    staged_state.cadence_secs,
                )
            else:
                deadline = restored_at + staged_state.cadence_secs
            self._set_monitor_deadline(staged, deadline)
            await self._persist_staged_monitor_locked(loop, staged)
            if self._on_monitor_tick is not None:
                self._arm_from_deadline(loop)
        self._emit("updated", loop)
        return loop

    async def update_monitor(
        self,
        monitor_id: str,
        *,
        target: str | None = None,
        objective: str | None = None,
        cadence_secs: int | None = None,
        budgets: MonitorBudgets | None = None,
        budget_patch: dict[str, int] | None = None,
        wake_instructions: str | None = None,
    ) -> NudgeLoop | None:
        """Patch an active structured record without implicit revival."""
        if budgets is not None and budget_patch is not None:
            raise ValueError("budgets and budget_patch are mutually exclusive")
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if loop is None or state is None or state.outcome is not None:
                return None
            reset_baseline = (target is not None and target != state.target) or (
                objective is not None and objective != state.objective
            )
            if reset_baseline and state.wake_in_flight:
                raise MonitorUpdateConflict(
                    "target or objective cannot change while a wake is in flight"
                )
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            if target is not None:
                staged_state.target = target
            if objective is not None:
                staged_state.objective = objective
            if cadence_secs is not None:
                cadence = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(cadence_secs)))
                staged_state.cadence_secs = cadence
                staged.idle_secs = cadence
                if staged.active and not staged_state.wake_in_flight and staged.next_due_ts > 0:
                    self._set_monitor_deadline(staged, time.time() + cadence)
            if budget_patch is not None:
                budget_fields = {
                    "max_runtime_secs",
                    "max_agent_turns",
                    "max_tokens",
                    "max_provider_errors",
                }
                unknown = set(budget_patch) - budget_fields
                if unknown:
                    raise ValueError(
                        "unknown structured monitor budget fields: " + ", ".join(sorted(unknown))
                    )
                values = {field: getattr(staged_state.budgets, field) for field in budget_fields}
                values.update(budget_patch)
                staged_state.budgets = MonitorBudgets(**values)
            elif budgets is not None:
                staged_state.budgets = budgets
            if wake_instructions is not None:
                staged_state.wake_instructions = wake_instructions
            if reset_baseline:
                staged_state.config_generation += 1
                staged_state.last_observation = {}
                staged_state.last_fingerprint = ""
                staged_state.last_observed_at = 0.0
                staged_state.last_decision = None
                staged_state.last_wake_fingerprint = ""
                staged_state.last_wake_reason_code = ""
                staged_state.wake_in_flight = False
                staged_state.wake_delivery = None
                staged_state.completion_evidence_deadline = 0.0
                staged_state.last_completion_fingerprint = ""
                staged_state.consecutive_provider_errors = 0
                staged_state.last_provider_error = None
            await self._persist_staged_monitor_locked(loop, staged)
            if loop.active and not state.wake_in_flight and loop.id not in self._firing:
                self._arm_from_deadline(loop)
        self._emit("updated", loop)
        return loop

    async def mark_monitor_action_in_flight(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Persist the dispatch claim for one actionable fingerprint."""
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("fingerprint must be a non-empty string")
        checked_at = time.time() if now is None else now
        if (
            isinstance(checked_at, bool)
            or not isinstance(checked_at, (int, float))
            or not math.isfinite(checked_at)
            or checked_at < 0
        ):
            raise ValueError("now must be a finite non-negative number")
        dispatched = False
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if (
                loop is None
                or state is None
                or not loop.active
                or state.outcome is not None
                or state.wake_in_flight
                or state.last_wake_fingerprint == fingerprint
            ):
                return False
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            reason = monitor_budget_reason(staged_state, now=checked_at)
            if reason:
                self._apply_monitor_budget_stop(staged, reason, stopped_at=checked_at)
            else:
                staged_state.last_wake_fingerprint = fingerprint
                staged_state.wake_in_flight = True
                staged_state.wake_delivery = None
                dispatched = True
            await self._persist_staged_monitor_locked(loop, staged)
            if not loop.active:
                self._sync_terminal_completion_timer(loop)
        self._emit("updated", loop)
        return dispatched

    async def record_monitor_turn_completion(
        self,
        completion: MonitorActionCompletion,
    ) -> None:
        """Charge one correlated, completed action turn exactly once."""
        async with self._lock:
            if self._accepted_monitor_turns.get(completion.monitor_id) == completion.fingerprint:
                self._accepted_monitor_turns.pop(completion.monitor_id, None)
            loop = self._loops.get(completion.monitor_id)
            state = loop.monitor if loop is not None else None
            if (
                loop is None
                or state is None
                or not state.wake_in_flight
                or state.last_wake_fingerprint != completion.fingerprint
            ):
                return
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            disposition = (
                MonitorActionDisposition.APPROVAL_STALL
                if staged.approval_stalled
                else completion.disposition
            )
            if staged_state.wake_delivery is not MonitorDispatchResult.DISPATCHED:
                staged_state.wake_count += 1
            staged_state.wake_in_flight = False
            staged_state.wake_delivery = None
            staged_state.completion_evidence_deadline = 0.0
            staged_state.last_completion_fingerprint = completion.fingerprint
            staged_state.last_completion_disposition = disposition
            staged_state.last_completed_at = completion.completed_ts
            staged_state.agent_turns += 1
            if completion.input_tokens is None or completion.output_tokens is None:
                staged_state.token_usage_known = False
            if completion.input_tokens is not None:
                staged_state.input_tokens += completion.input_tokens
            if completion.output_tokens is not None:
                staged_state.output_tokens += completion.output_tokens
            reason = monitor_budget_reason(staged_state, now=completion.completed_ts)
            if staged_state.outcome is not None:
                self._set_monitor_deadline(staged, 0.0)
            elif reason:
                self._apply_monitor_budget_stop(
                    staged,
                    reason,
                    stopped_at=completion.completed_ts,
                )
            elif (
                disposition is MonitorActionDisposition.APPROVAL_STALL
                and staged_state.outcome is None
            ):
                staged.active = False
                self._set_monitor_deadline(staged, 0.0)
                staged_state.outcome = MonitorOutcome.BLOCKED
                staged_state.stopped_reason = MONITOR_STOP_APPROVAL_STALL
                staged_state.stopped_at = completion.completed_ts
            elif staged.active and staged_state.outcome is None:
                self._set_monitor_deadline(
                    staged,
                    completion.completed_ts + staged_state.cadence_secs,
                )
            await self._persist_staged_monitor_locked(loop, staged)
            if not loop.active:
                self._sync_terminal_completion_timer(loop)
            if loop.active and state.outcome is None:
                if loop.id in self._firing:
                    self._rearm_pending.add(loop.id)
                else:
                    self._arm_from_deadline(loop)
        self._emit("updated", loop)

    def _apply_monitor_budget_stop(
        self,
        loop: NudgeLoop,
        reason: str,
        *,
        stopped_at: float,
    ) -> None:
        """Apply budget-stop fields without changing the live timer registry."""
        state = loop.monitor
        if state is None:
            return
        loop.active = False
        self._retain_accepted_terminal_completion(loop, stopped_at=stopped_at)
        state.outcome = MonitorOutcome.BUDGET
        state.stopped_reason = reason
        state.stopped_at = stopped_at

    def _apply_monitor_user_stop(self, loop: NudgeLoop, *, stopped_at: float) -> None:
        """Apply a user stop after its replacement snapshot is durable."""
        state = loop.monitor
        if state is None:
            return
        loop.active = False
        self._retain_accepted_terminal_completion(loop, stopped_at=stopped_at)
        state.outcome = MonitorOutcome.USER_STOP
        state.stopped_reason = MONITOR_STOP_USER
        state.stopped_at = stopped_at

    def _retain_accepted_terminal_completion(
        self,
        loop: NudgeLoop,
        *,
        stopped_at: float,
    ) -> None:
        """Bound an accepted terminal claim until completion or evidence expiry."""
        state = loop.monitor
        if state is None:
            return
        accepted = self._accepted_monitor_turns.get(loop.id) == state.last_wake_fingerprint
        if not accepted:
            state.wake_in_flight = False
            state.wake_delivery = None
            state.completion_evidence_deadline = 0.0
            self._set_monitor_deadline(loop, 0.0)
            return
        deadline = state.completion_evidence_deadline
        if deadline <= stopped_at:
            deadline = stopped_at + MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS
            state.completion_evidence_deadline = deadline
        self._set_monitor_deadline(loop, deadline)

    def _waits_for_terminal_completion(self, loop: NudgeLoop) -> bool:
        """Whether a terminal row still owns a finite accepted-turn correlation."""
        state = loop.monitor
        return bool(
            state is not None
            and state.outcome
            in {
                MonitorOutcome.BUDGET,
                MonitorOutcome.SESSION_CLOSE,
                MonitorOutcome.USER_STOP,
            }
            and state.wake_in_flight
            and state.completion_evidence_deadline > 0
        )

    def _sync_terminal_completion_timer(self, loop: NudgeLoop) -> None:
        """Keep only the timer needed to expire an accepted terminal claim."""
        if self._waits_for_terminal_completion(loop):
            if loop.id in self._firing:
                self._rearm_pending.add(loop.id)
            else:
                self._arm_from_deadline(loop)
            return
        self._cancel_timer(loop.id)

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

    async def record_monitor_dispatch_failure(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float | None = None,
    ) -> None:
        """Retire an acknowledged wake when its session cannot accept it."""
        async with self._lock:
            if self._accepted_monitor_turns.get(monitor_id) == fingerprint:
                self._accepted_monitor_turns.pop(monitor_id, None)
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if (
                loop is None
                or state is None
                or state.outcome is not None
                or not state.wake_in_flight
                or state.last_wake_fingerprint != fingerprint
            ):
                return
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            staged_state.wake_in_flight = False
            staged_state.completion_evidence_deadline = 0.0
            staged_state.wake_delivery = MonitorDispatchResult.UNAVAILABLE
            staged.active = False
            staged_state.outcome = MonitorOutcome.TARGET_UNAVAILABLE
            staged_state.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
            staged_state.stopped_at = time.time() if now is None else now
            self._set_monitor_deadline(staged, 0.0)
            await self._persist_staged_monitor_locked(loop, staged)
            self._cancel_timer(loop.id)
        self._emit("updated", loop)

    async def monitor_dispatch_is_authorized(
        self,
        monitor_id: str,
        fingerprint: str,
    ) -> bool:
        """Revalidate a persisted claim immediately before transport handoff."""
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            return bool(
                loop is not None
                and state is not None
                and loop.active
                and state.outcome is None
                and state.wake_in_flight
                and state.last_wake_fingerprint == fingerprint
                and state.wake_delivery is not MonitorDispatchResult.DISPATCHED
            )

    def mark_monitor_turn_accepted(self, monitor_id: str, fingerprint: str) -> None:
        """Remember a claimed wake that crossed a channel's provider boundary."""
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is not None
            and state is not None
            and loop.active
            and state.outcome is None
            and state.wake_in_flight
            and state.last_wake_fingerprint == fingerprint
        ):
            self._accepted_monitor_turns[monitor_id] = fingerprint

    async def record_monitor_dispatch_busy(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float,
    ) -> None:
        """Retry one claimed wake after ordinary session concurrency clears."""
        async with self._lock:
            if self._accepted_monitor_turns.get(monitor_id) == fingerprint:
                self._accepted_monitor_turns.pop(monitor_id, None)
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if (
                loop is None
                or state is None
                or not loop.active
                or not state.wake_in_flight
                or state.last_wake_fingerprint != fingerprint
            ):
                return
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            reason = monitor_budget_reason(staged_state, now=now)
            if reason:
                self._apply_monitor_budget_stop(staged, reason, stopped_at=now)
            else:
                staged_state.wake_delivery = MonitorDispatchResult.BUSY
                staged_state.completion_evidence_deadline = 0.0
                self._set_monitor_deadline(
                    staged,
                    now + min(MONITOR_BUSY_RETRY_SECS, staged_state.cadence_secs),
                )
            await self._persist_staged_monitor_locked(loop, staged)
            if not loop.active:
                self._sync_terminal_completion_timer(loop)
            if loop.active:
                if loop.id in self._firing:
                    self._rearm_pending.add(loop.id)
                else:
                    self._arm_from_deadline(loop)
        self._emit("updated", loop)

    async def record_monitor_dispatched(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float,
    ) -> None:
        """Persist the finite window for authoritative completion evidence."""
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if (
                loop is None
                or state is None
                or not loop.active
                or not state.wake_in_flight
                or state.last_wake_fingerprint != fingerprint
            ):
                return
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            deadline = now + MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS
            if staged_state.wake_delivery is not MonitorDispatchResult.DISPATCHED:
                staged_state.wake_count += 1
            staged_state.wake_delivery = MonitorDispatchResult.DISPATCHED
            staged_state.completion_evidence_deadline = deadline
            self._set_monitor_deadline(staged, deadline)
            await self._persist_staged_monitor_locked(loop, staged)
            if loop.id in self._firing:
                self._rearm_pending.add(loop.id)
            else:
                self._arm_from_deadline(loop)
        self._emit("updated", loop)

    async def record_monitor_completion_evidence_unavailable(
        self,
        monitor_id: str,
        fingerprint: str,
        *,
        now: float,
    ) -> None:
        """Fail closed when an accepted wake never reports raw completion."""
        async with self._lock:
            loop = self._loops.get(monitor_id)
            state = loop.monitor if loop is not None else None
            if (
                loop is None
                or state is None
                or not state.wake_in_flight
                or state.last_wake_fingerprint != fingerprint
                or state.completion_evidence_deadline <= 0
                or now < state.completion_evidence_deadline
            ):
                return
            terminal = state.outcome is not None
            if terminal and state.outcome not in {
                MonitorOutcome.BUDGET,
                MonitorOutcome.SESSION_CLOSE,
                MonitorOutcome.USER_STOP,
            }:
                return
            if self._accepted_monitor_turns.get(monitor_id) == fingerprint:
                self._accepted_monitor_turns.pop(monitor_id, None)
            staged = deepcopy(loop)
            staged_state = staged.monitor
            assert staged_state is not None
            staged_state.wake_in_flight = False
            staged_state.wake_delivery = None
            staged_state.completion_evidence_deadline = 0.0
            if not terminal:
                staged.active = False
                staged_state.outcome = MonitorOutcome.BLOCKED
                staged_state.stopped_reason = MONITOR_STOP_COMPLETION_UNAVAILABLE
                staged_state.stopped_at = now
            self._set_monitor_deadline(staged, 0.0)
            await self._persist_staged_monitor_locked(loop, staged)
            self._cancel_timer(loop.id)
        self._emit("updated", loop)

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

    # ── Reactive arming ──

    def notify_approval_stalled(self, slot_key: str) -> None:
        """Record that a tool approval in *slot_key* went unanswered.

        Called from the approval path when a prompt times out with no decision.
        That is the only evidence available that an unattended loop can no longer
        act, and it is evidence rather than inference: an auto-approved tool
        never reaches the interactive wait, so this is unreachable for a loop
        whose cycles only touch read-only tools.

        Records the fact and returns. The STOP is left to ``_timer``, which
        already owns every terminal decision and evaluates them serialized before
        a fire — stopping from here would mean cancelling a timer that may be
        mid-fire (the one thing the fire-window contracts forbid, since it kills
        the in-flight turn) and racing the very turn that produced the evidence.
        Deferring costs the cycle already in flight and saves every later one.

        The evidence is slot-level, not cycle-level: an unanswered prompt in an
        attended tab counts too. That is the conservative direction — the loop
        deactivates inspectable and restartable with a notice naming the remedy,
        and a person who was merely away resumes it — whereas the alternative
        needs a reliable "is this turn a nudge cycle?" test, which the fire
        window does not provide for dashboard slots (their turn outlives it).
        """
        loop = self._find_by_slot(slot_key)
        if not loop or not loop.active or loop.approval_stalled:
            return
        loop.approval_stalled = True
        logger.warning(
            "AutoNudge: a tool approval went unanswered in loop %s's session — "
            "it will stop instead of firing another cycle",
            loop.id,
        )
        self._persist_soon()

    def notify_turn_complete(self, slot_key: str) -> None:
        """Called by gateway after HOOK_EVENT_STOP — resume the countdown for this slot.

        Re-arms toward the loop's persistent deadline (``_arm_from_deadline``),
        NOT with a fresh full interval: after a user turn the timer picks up
        the remaining time (or fires shortly after, if the deadline passed
        mid-turn), while the first turn-complete after a delivered fire — the
        nudge turn's own end — finds the deadline cleared and starts the next
        full cycle. DEFERS while the loop's own timer task is mid-fire:
        ``_arm_timer`` cancels the existing task, and during the fire window
        that task may be parked on ``_persist_locked()`` writing the delivered
        cycle. Cancelling it there loses the ``cycle_count`` bump and lets the
        loop run extra cycles after a restart. The deferred re-arm is applied
        when the window closes.
        """
        loop = self._find_by_slot(slot_key)
        if not loop or not loop.active:
            return
        if loop.id in self._firing:
            self._rearm_pending.add(loop.id)
            return
        self._arm_from_deadline(loop)

    def notify_user_input(self, slot_key: str) -> None:
        """Called when user sends a message — cancel the pending nudge task.

        Cancelling the TASK defers delivery until the user's turn ends (a
        nudge must never race a human turn); the loop's ``next_due_ts`` is
        untouched, so the schedule itself survives — ``notify_turn_complete``
        resumes the same countdown rather than restarting the full interval.

        While the loop is mid-fire this must NOT cancel the timer: that task may
        be parked on ``_persist_locked()`` writing the delivered cycle, and
        cancelling it there abandons an in-flight executor write whose stale
        payload can later overwrite a newer update/delete (state resurrected
        after a restart). User priority is still honoured — the deferred re-arm
        is dropped, so no further nudge is scheduled from this cycle.
        """
        loop = self._find_by_slot(slot_key)
        if not loop:
            return
        if loop.id in self._firing:
            self._rearm_pending.discard(loop.id)
            logger.info(
                "AutoNudge: user input during loop %s's fire window — dropped the "
                "deferred re-arm instead of cancelling mid-persist",
                loop.id,
            )
            return
        self._cancel_timer(loop.id)

    def _cancel_timer(self, loop_id: str, *, drop_claims: bool = True) -> None:
        """Retire one loop's timer task. The single cancellation policy.

        Two conditions make a cancel wrong rather than merely redundant, and both are
        stated here so no caller has to remember either:

        * **The currently running timer task** (a self-re-arm from inside ``_timer``) is
          about to return on its own, and cancelling it would inject a spurious
          ``CancelledError`` into the finishing task.
        * **A task whose event loop has already closed.** ``Task.cancel`` schedules the
          cancellation through ``loop.call_soon``, which raises ``RuntimeError: Event loop
          is closed`` — so this raises out of ``remove``/``remove_sync`` and the dashboard
          handler above it answers 500. The service is a process-global singleton, so its
          ``_timers`` outlive the loop that created them whenever one loop is replaced by
          another: the gateway's own shutdown, and every test that drives a handler after
          an earlier test's loop closed. Asked positively (``get_loop().is_closed()``)
          rather than by catching the ``RuntimeError``, because a closed loop is the one
          state where cancelling is a NO-OP by definition — the task can never run again —
          and catching would also swallow a genuine scheduling fault.

        The closed-loop question is asked FIRST because it needs no running loop of its
        own, and ``stop()`` reaches here from synchronous callers (gateway shutdown, test
        teardown) where ``asyncio.current_task()`` would raise instead of answering — hence
        :func:`_current_task_or_none`.
        """
        t = self._timers.pop(loop_id, None)
        if t is None or t.done():
            return
        # Closed-loop check FIRST: it needs no running loop of its own, so a dead timer is
        # retired even from a synchronous caller.
        if t.get_loop().is_closed():
            logger.debug(
                "AutoNudge: dropped loop %s's timer without cancelling — its event loop "
                "has closed, so the task can no longer run",
                loop_id,
            )
            return
        if t is _current_task_or_none():
            return
        t.cancel()
        if not drop_claims:
            # Replacing a timer is not cancelling a cycle. ``_arm_timer`` cancels before
            # it creates, so every ordinary re-arm came through here -- including the
            # backoff re-arm on the refused-fire path, which erased the claim that same
            # path had re-owed one statement earlier. The accounting fix was defeated by
            # the cleanup meant to protect it.
            return
        # A cancelled CYCLE drops its claim. Without this the id stays in the claim set
        # and the loop's next delivered fire -- a fallback, a floor tick -- inherits it
        # and is charged as a wake as well, counting one delivered turn under two
        # counters. That trade is deliberate: an undelivered observation is lost rather
        # than attributed to a turn that did not carry it.
        self._pending_monitor_wake.discard(loop_id)
        self._pending_floor_tick.discard(loop_id)

    def _arm_timer(self, loop: NudgeLoop, delay: float | None = None) -> None:
        self._cancel_timer(loop.id, drop_claims=False)
        self._timers[loop.id] = asyncio.create_task(self._timer(loop, delay))

    async def _deactivate_unwired_monitor(self, loop_id: str) -> None:
        """Retain but disarm a structured record when no controller is wired."""
        async with self._lock:
            loop = self._loops.get(loop_id)
            if loop is None or loop.monitor is None:
                return
            staged = deepcopy(loop)
            staged.active = False
            assert staged.monitor is not None
            staged.monitor.wake_in_flight = False
            staged.monitor.wake_delivery = None
            staged.monitor.completion_evidence_deadline = 0.0
            staged.monitor.outcome = MonitorOutcome.BLOCKED
            staged.monitor.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
            staged.monitor.stopped_at = time.time()
            self._set_monitor_deadline(staged, 0.0)
            await self._persist_staged_monitor_locked(loop, staged)
            self._cancel_timer(loop.id)
        self._emit("updated", loop)

    def _arm_from_deadline(self, loop: NudgeLoop) -> None:
        """(Re)arm the timer toward the loop's persistent deadline.

        The countdown anchors on ``next_due_ts`` instead of restarting at the
        full interval on every arm, so user turns in the bound session defer a
        pending fire without pushing the schedule back. An unset deadline (0 —
        a just-delivered fire, a legacy store entry) starts a fresh full
        countdown from now, and the assignment is persisted through a
        supervised background write so a restart resumes this countdown
        rather than restarting the interval. A deadline still in the future
        resumes with exactly the remaining time; only one already in the past
        fires after a short beat (``_OVERDUE_REARM_SECS``) rather than
        instantly, so a user mid-conversation keeps deferring it simply by
        sending another message. The delay is capped at ``idle_secs`` so a
        clock jump can never park the timer beyond one full interval.

        A monitor loop arms through this same path and on the same deadline. Its
        cadence is the interval the user already set, not a second clock on the
        monitor record: two clocks for one countdown would have to be kept
        agreed, and the one the user can see is the one they set. What differs
        for a monitor is not WHEN the timer wakes but what the wake costs -- the
        probe gate in :meth:`_timer` decides whether that tick spends a turn.

        ONE monitor is refused a timer outright: a record whose ``version`` this
        gateway does not implement. Such a record belongs to a newer gateway
        (a downgrade or a rollback read its store), and this controller cannot
        interpret its policy -- so arming it would run the loop under a policy
        nobody here understands, which for the pre-gate code path means
        injecting the raw message every interval with no decision at all. The
        refusal is deliberately made HERE, on the arm, rather than by rewriting
        the record: the stored ``active`` intent belongs to the gateway that
        wrote it and must survive the downgrade so an upgrade resumes the watch.
        Inertness is the local consequence, not a change of intent.
        """
        monitor = loop.monitor
        if monitor is not None and monitor.version != MONITOR_STATE_VERSION:
            logger.info(
                "AutoNudge: not arming loop %s -- its monitor record is version %s and "
                "this gateway implements %s",
                loop.id,
                monitor.version,
                MONITOR_STATE_VERSION,
            )
            return
        now = time.time()
        if loop.next_due_ts <= 0:
            loop.next_due_ts = now + loop.idle_secs
            if loop.monitor is not None:
                loop.monitor.next_probe_at = loop.next_due_ts
            self._persist_soon()
        remaining = loop.next_due_ts - now
        if remaining <= 0:
            delay = float(_OVERDUE_REARM_SECS)
        else:
            delay = min(remaining, float(loop.idle_secs))
        self._arm_timer(loop, delay=delay)

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

    async def _monitor_tick_is_quiet(self, loop: NudgeLoop) -> bool:
        """Observe this loop's subject cheaply; say whether to skip the turn.

        Returns True only when the tick is DEFINITELY not worth a model turn.
        Every other case -- no monitor, no probe for that subject kind, an
        un-inferable target, a probe defect, a kernel that reached no verdict --
        returns False so the caller fires exactly as it does today.

        The asymmetry is the whole safety argument. A wrongly-QUIET tick is
        silence: the loop stops waking and the work it was watching stalls with
        nothing on screen to say why. A wrongly-spent tick costs one turn, which
        is what every tick costs today. So every uncertain path resolves toward
        spending, and only a positive "nothing happened" from the kernel skips.

        The kernel call is offloaded to a thread because observing runs ``gh``
        as a subprocess with a 25s timeout. On the event loop that would freeze
        chat, the channel transports and the liveness probes for as long as one
        slow GitHub call takes.
        """
        monitor = loop.monitor
        if monitor is None or monitor.outcome is not None:
            return False
        if not loop.gate:
            # An opt-out that only SOME paths honour is worse than no opt-out. This
            # check exists because the two can now disagree: a record stored with a
            # monitor but no ``gate`` key -- one armed while the default was True,
            # or upgraded from an earlier build of this branch -- decodes to
            # ``gate=False`` with its monitor intact. Reading only the monitor would
            # poll such a loop anyway and let a terminal verdict DEACTIVATE it,
            # which is exactly the harm the opt-out exists to prevent. The stored
            # decision wins over the presence of the object.
            return False
        # A wake buys the agent one more turn, unconditionally and BEFORE any
        # observation. The probe watches the subject, not the agent: a turn that
        # was woken and has not pushed yet leaves the subject unchanged, so
        # observing here would read "nothing happened" and starve work already in
        # progress. Bounded on purpose -- one tick per wake, spent whether or not
        # it was needed -- because the alternative designs both fail worse: an
        # unbounded allowance driven by a completion signal disables gating
        # entirely on any surface where that signal never arrives, and no
        # allowance at all lets a watch go silent while holding half-finished
        # work. Costing one turn per wake is the cheap failure.
        if monitor.followup_ticks > 0 and not monitor.terminal_pending:
            # NOT while a terminal turn is owed. The allowance exists to protect work
            # already in progress, which is why it skips observation -- but a subject
            # with terminal debt is FINISHED, so there is no in-progress work to
            # protect, and the retry's correctness depends on it still being finished.
            # Skipping the poll here is what let a REOPENED pull request keep its stale
            # debt: the clearing added for that case lives after the poll, so the
            # bypass jumped straight over it and the retried delivery settled a
            # terminal state that no longer held. Re-observing costs one probe call on
            # a path that is already firing a turn.
            monitor.followup_ticks -= 1
            self._persist_soon()
            logger.debug("AutoNudge: loop %s spending a post-wake follow-up tick", loop.id)
            return False
        probe = probes.build(monitor.kind)
        if probe is None:
            return False
        # Derive the probe's config from the LOOP'S OWN INSTRUCTION, then check
        # the subject it yields against the stored monitor.
        #
        # Not from ``monitor.target``: that is the CANONICAL subject
        # ("owner/name#123"), a shorthand, and a shorthand deliberately carries no
        # host -- so re-inferring from it would discard the github.com pin that a
        # URL-armed watch is entitled to, and on a machine configured for an
        # enterprise server the probe would resolve the slug there. A
        # same-numbered enterprise pull request being merged would then falsely
        # terminate a live public watch.
        #
        # The instruction is the only place the original spelling survives, and
        # storing the host a second time would put one fact in two places that can
        # disagree. So infer from the message and REQUIRE the result to name the
        # subject the monitor is bound to; a mismatch means the two have drifted
        # apart, which is not something to resolve by guessing -- fire instead, the
        # same direction every other uncertain path takes.
        target = targets.infer(loop.message)
        if target is None or (target.kind, target.subject) != (monitor.kind, monitor.target):
            if target is not None:
                logger.info(
                    "AutoNudge: loop %s instruction names %s but its monitor is bound to "
                    "%s -- firing instead of observing",
                    loop.id,
                    target.subject,
                    monitor.target,
                )
            return False
        # Captured BEFORE the poll, which awaits: this is what the verdict is
        # about, and it is checked again afterwards. The derived CONFIG is part of
        # it, not just the subject: the stored target is a shorthand, so an
        # instruction edited from an enterprise shorthand to the same public URL
        # leaves kind and target identical while changing which SERVER is being
        # observed. Comparing only the subject would let a verdict about one host
        # settle a watch that now means the other.
        binding = (monitor.kind, monitor.target, target.message)
        # The dedupe memory is keyed on this identity, so it must move when the
        # subject's host does -- otherwise a retargeted watch inherits
        # observations made against a different server and suppresses the first
        # real signal from the new one. Only this driver's identity changes; the
        # cron path keeps the one its persisted state was written under.
        identity = f"{loop.id}:{target.host_key}"
        if monitor.poll_in_flight:
            # A previous poll was interrupted after the kernel may already have
            # committed "reported" for what it saw. That observation reached
            # nobody, and re-observing now would read the same state as unchanged,
            # so this tick must not trust a quiet verdict -- it fires. The flag is
            # cleared first so the doubt is consumed once rather than latching.
            monitor.poll_in_flight = False
            monitor.gate_fallbacks += 1
            self._persist_soon()
            logger.info(
                "AutoNudge: loop %s had a poll interrupted -- firing rather than "
                "trusting a fresh observation of the same state",
                loop.id,
            )
            return False
        # Durable BEFORE the probe runs, because the case it protects against is
        # this coroutine never resuming. ``_persist_soon`` would not do: a
        # scheduled write does not survive the shutdown that causes the problem.
        monitor.poll_in_flight = True
        try:
            # ``_write_monitor_snapshot_locked`` under ``_lock``, NOT
            # ``_persist_locked``: that one releases ``_lock`` if the awaiting task
            # is cancelled while the executor write is still in flight, so a
            # pause or retarget landing here could have this stale snapshot
            # overwrite the newer state it just wrote. The settlements already use
            # the non-releasing writer; these marker writes were left behind.
            async with self._lock:
                await self._write_monitor_snapshot_locked()
        except Exception:
            # Could not record the doubt, so do not incur it: fire this tick
            # rather than run a probe whose interruption would be invisible.
            monitor.poll_in_flight = False
            logger.warning(
                "AutoNudge: could not record the in-flight marker for loop %s -- "
                "firing instead of polling",
                loop.id,
                exc_info=True,
            )
            return False
        try:
            verdict = await asyncio.get_running_loop().run_in_executor(
                None, lambda: irq.poll(identity, target.message, probe)
            )
        except Exception:
            monitor.poll_in_flight = False
            logger.warning(
                "AutoNudge: probe gate raised for loop %s — firing as usual",
                loop.id,
                exc_info=True,
            )
            return False
        if verdict.outcome is not irq.Outcome.WAKE:
            # The doubt is discharged when the thing it protects has happened -- and
            # for a WAKE that is DELIVERY, not the poll returning. The kernel has
            # already committed "reported" for what it saw, so if this process dies
            # between here and the turn landing, a fresh observation reads the same
            # state as unchanged and the signal is gone until the streak floor. The
            # in-process refusal is covered by ``followup_ticks``; a DEATH is covered
            # only by this marker outliving the fire, so a wake keeps it set and the
            # fire cycle clears it where the wake claim is consumed.
            #
            # The asymmetry is deliberate: the SET must be durable because it guards
            # against a death, while a CLEAR may ride the debounced write -- losing a
            # clear costs one unnecessary fire, the direction this design resolves
            # toward anyway.
            monitor.poll_in_flight = False

        # The poll above is a real await -- it runs ``gh`` in a thread for up to
        # 25 seconds -- so the loop can be RETARGETED while it is in flight:
        # ``update(message=...)`` rebinds the monitor to a different pull request,
        # or clears it. Acting on this verdict now would apply an observation of
        # the OLD subject to the new one, and the terminal branch would deactivate
        # a watch that had just been pointed at a live pull request. Compare the
        # binding, not the object: a retarget mutates the same MonitorState.
        fresh = targets.infer(loop.message)
        current_binding = (
            (monitor.kind, monitor.target, fresh.message) if fresh is not None else None
        )
        if loop.monitor is not monitor or current_binding != binding:
            # The verdict is thrown away, so no wake is owed and the doubt has
            # nothing left to protect. Clear it, or the next tick would fire a
            # second time on a discharged suspicion and count a phantom fallback.
            monitor.poll_in_flight = False
            logger.info(
                "AutoNudge: loop %s was retargeted while its probe was in flight -- "
                "discarding the stale verdict and firing as usual",
                loop.id,
            )
            return False

        if verdict.outcome is irq.Outcome.TERMINAL:
            # The subject is finished (a merged or closed pull request). Stop the
            # loop rather than firing: there is nothing left to service, and one
            # more turn would only rediscover that. ``expired`` is the existing
            # channel for "this loop stopped rather than the agent finishing",
            # and the emitted payload carries ``stopped_reason``, which is what
            # distinguishes a merged subject from a spent bound.
            #
            # Deliberately NOT counted as a wake: no turn is delivered here. A
            # terminal observation that incremented ``wakes`` would report a turn
            # that never ran, in the very counters this change exists to make
            # trustworthy.
            # Record the finish ON THE MONITOR, not only on the loop. Without
            # this the record reads as merely paused, and the generic resume path
            # -- the goal popover's Save, which is allowed to revive a current
            # unsettled monitor -- would re-arm the watch onto a subject that is
            # already merged. It would then observe TERMINAL, deactivate, and be
            # revivable again: a loop that cannot be told apart from a working
            # one. Reaching the end of the thing you were watching is a SUCCESS,
            # so the outcome says so rather than borrowing a bound's vocabulary.
            # ONE transition, committed once. Four rounds of review landed on this
            # hunk and each earlier shape had a gap: announcing before the write
            # promised a finish the record did not have; announcing after it was
            # swallowed by ``update``'s cancel reaching this very task; and doing
            # both left TWO await points, so a write failure killed the timer with
            # the loop still active, and a retarget landing between them let an old
            # verdict deactivate a subject that had never been observed.
            #
            # So there is no ``update`` call here at all. The marks go on in memory
            # with no await between them, one durable write commits them, and the
            # deactivation is simply ``active = False`` -- which the re-arm guard
            # below already honours, making the timer cancel unnecessary rather
            # than merely deferred.
            # Reaching the end of the thing you were watching is not automatically
            # a success. A MERGED subject is; one CLOSED WITHOUT MERGING ended on a
            # question -- reopen or abandon -- and recording SUCCESS there tells the
            # user "no action needed" about the one case that needs them most. The
            # probe distinguishes the two, so this reads its KEYS rather than its
            # prose, which would break the first time that wording is edited. No
            # key at all (an unusable target) is also not a success.
            merged = "merged" in verdict.keys
            # EVERY field this transition writes has to be in here. The loop's own
            # ``stopped_reason`` is written alongside the monitor's, and leaving it
            # out of the rollback left a live loop tagged as terminated -- which the
            # fallback delivery would then persist.
            restore = (
                monitor.outcome,
                monitor.stopped_reason,
                monitor.stopped_at,
                loop.active,
                loop.stopped_reason,
            )
            logger.info(
                "AutoNudge: loop %s subject reached a terminal state (%s)",
                loop.id,
                ",".join(verdict.keys) or "unattributed",
            )
            # SERIALIZED against ``update``. Round 13 removed this path's own second
            # await; this closes the other side of the same race, which is
            # ``update``'s. That method takes the MAINTENANCE lock (not ``_lock``)
            # and awaits inside it, so a retarget could pass its precheck, yield,
            # let this branch settle the OLD subject with ``active = False``, and
            # then bind the NEW subject onto that inactive loop -- a fresh watch
            # that never ticks. Holding the same lock across revalidate, mutate and
            # persist is what makes the two mutually exclusive; ``_lock`` alone
            # would not, because that is not the lock ``update`` contends for.
            #
            # Lock ORDER matches ``update``'s (maintenance, then ``_lock`` for the
            # write) so the two cannot deadlock against each other.
            # A CHANNEL loop is told by a delivered TURN, not by the dashboard
            # notification -- so for one the settlement must not be committed yet.
            # Committing it means an inactive loop, and if that final fire is
            # refused (a busy thread, the ordinary case) nothing re-arms and the
            # news is lost: exactly the silent ending the previous round added this
            # delivery to prevent. So mark what is OWED, durably, and settle only
            # once the turn has landed. No outcome is recorded in the meantime, so a
            # restart in this window finds a plain live loop rather than one tagged
            # as finished and refused revival.
            if is_channel_key(loop.slot_key):
                if not monitor.terminal_pending:
                    monitor.terminal_pending = "success" if merged else "blocked"
                    try:
                        # Same writer as the settlements, for the same reason: a
                        # cancelled ``_persist_locked`` releases ``_lock`` mid-write.
                        async with self._lock:
                            await self._write_monitor_snapshot_locked()
                    except Exception:
                        monitor.terminal_pending = ""
                        logger.exception(
                            "AutoNudge: could not record the owed terminal turn for %s",
                            loop.id,
                        )
                return False
            settle_lock = await self._acquire_mutation_lock(loop.id)
            if settle_lock is None:
                # Maintenance has claimed this loop. Not ours to settle: fire, and
                # the next tick will observe the same terminal state.
                return False
            try:
                # Re-read under the lock. The checks before it were made while a
                # retarget could still land.
                fresh_under_lock = targets.infer(loop.message)
                if (
                    loop.monitor is not monitor
                    or loop.id not in self._loops
                    or (
                        (monitor.kind, monitor.target, fresh_under_lock.message)
                        if fresh_under_lock is not None
                        else None
                    )
                    != binding
                ):
                    logger.info(
                        "AutoNudge: loop %s changed before its terminal settlement -- firing",
                        loop.id,
                    )
                    return False
                monitor.outcome = MonitorOutcome.SUCCESS if merged else MonitorOutcome.BLOCKED
                monitor.stopped_reason = MONITOR_TERMINAL_REASON
                monitor.stopped_at = time.time()
                loop.stopped_reason = MONITOR_TERMINAL_REASON
                loop.active = False
                try:
                    async with self._lock:
                        await self._write_monitor_snapshot_locked()
                except asyncio.CancelledError:
                    # The writer drains its executor write before propagating
                    # cancellation, so by HERE the settlement is already committed
                    # -- and on restart the loop reads as settled, so nothing would
                    # ever notify. Tell the user now, then preserve the
                    # cancellation. Same shape as ``_apply_staged_monitor``'s.
                    self._emit("expired", loop)
                    raise
                except Exception:
                    # A failed write must not take the watch down with it. Undo the
                    # marks and fire: the loop stays watchable, the user gets a
                    # turn, and the next tick observes the same terminal state and
                    # tries again. Letting this raise would kill the timer task
                    # with the loop still active in memory and on disk -- a dead
                    # watch that looks exactly like a calm one, which is the
                    # failure mode this whole change exists to remove.
                    (
                        monitor.outcome,
                        monitor.stopped_reason,
                        monitor.stopped_at,
                        loop.active,
                        loop.stopped_reason,
                    ) = restore
                    logger.exception(
                        "AutoNudge: could not persist the terminal transition for %s -- "
                        "keeping the watch alive and firing instead",
                        loop.id,
                    )
                    return False
            finally:
                settle_lock.release()
            self._emit("expired", loop)
            return True

        if monitor.terminal_pending and verdict.outcome in (
            irq.Outcome.QUIET,
            irq.Outcome.WAKE,
        ):
            # The subject came BACK. A channel loop defers its settlement as a durable
            # debt because only a delivered turn can carry the news, and that debt
            # outlives the observation that created it -- so a closed PR that is
            # REOPENED while the final fire is still owed would have its next
            # delivered turn claim the stale debt and deactivate a watch whose
            # subject is live again. Nothing else cleared it: the marker was written
            # once and read at settlement, which is the same absence-shaped defect
            # this review has now found twelve times.
            #
            # Only a TRUSTWORTHY observation clears it. A FALLBACK means the subject
            # was NOT observed (a failed fetch, a probe defect), and letting an
            # unobserved tick erase real debt would lose the terminal news for good --
            # the opposite of the invariant that failure resolves toward spending.
            monitor.terminal_pending = ""
            try:
                async with self._lock:
                    await self._write_monitor_snapshot_locked()
            except Exception:
                # NOT rolled back -- and there is deliberately no saved copy to roll
                # back TO. Round 31 restored the debt here to keep memory and disk in
                # agreement, which is the right instinct almost everywhere and the
                # wrong one here: a trustworthy live observation has just DISPROVED
                # the debt, so restoring it lets the next delivered turn settle a
                # terminal state that no longer holds and silently stop a watch whose
                # subject is alive. The divergence is safe in exactly one direction --
                # memory saying "no debt" keeps the watch running, and if the process
                # restarts before the write lands, the disk's stale debt comes back and
                # the tick RE-OBSERVES it (an outstanding debt no longer spends the
                # observation-free follow-up tick), which clears it again. So the
                # failure path converges instead of stopping work.
                logger.exception(
                    "AutoNudge: could not persist the cleared terminal debt for %s -- "
                    "keeping it cleared in memory so a live subject is not settled",
                    loop.id,
                )

        if verdict.outcome is irq.Outcome.QUIET:
            monitor.quiet_ticks += 1
            monitor.quiet_streak += 1
            monitor.last_observed_at = time.time()
            if monitor.quiet_streak >= _MAX_QUIET_STREAK:
                # Floor reached: deliver anyway. The gate can only see the
                # SUBJECT, and a loop whose duty is to act while the subject is
                # quiet -- refresh a heartbeat, chase a silent reviewer, rebase
                # onto a moving base -- is invisible to it and would otherwise
                # never be delivered again. Inference cannot read that intent out
                # of the wording, so the honest answer is not to guess it but to
                # bound how long any loop can go undelivered.
                monitor.quiet_streak = 0
                # NOT charged here, for the same reason the WAKE branch is not: this
                # tick has decided to deliver but has not delivered, and the fire can
                # still be refused by a busy slot. Charging now would report a turn
                # that never ran, and since the honest free-tick figure is
                # ``quiet_ticks`` minus ``floor_ticks``, an over-counted floor
                # UNDERSTATES the saving -- the safe direction, but still a wrong
                # number in the one artifact this PR exists to produce.
                #
                # GPT's prescribed remedy was to revert this counter until it could be
                # charged after delivery. Declined: the counter is what separates a
                # quiet verdict from a free tick, so deleting it would remove the
                # subtraction that makes the metering honest. The substance -- charge
                # only on confirmed delivery -- is adopted instead, by the mechanism
                # the wake charge already uses.
                #
                # Two counters now carry the same owed-charge shape through the same
                # fire cycle. They belong in one structure; that is the collapse
                # recommended on the pull request rather than a third set later.
                self._pending_floor_tick.add(loop.id)
                logger.info(
                    "AutoNudge: loop %s hit the quiet-streak floor after %d quiet ticks",
                    loop.id,
                    _MAX_QUIET_STREAK,
                )
                # Persisted AFTER the reset, not before it. Today the earlier
                # call would have captured this anyway, because the write is a
                # detached task that cannot run until this block yields -- but
                # that is an accident of the persist being deferred, and a
                # restart reading a streak that was never reset would deliver one
                # extra turn and under-count the floor. Ordering it explicitly
                # costs nothing and does not depend on that.
                self._persist_soon()
                return False
            self._persist_soon()
            logger.debug("AutoNudge: loop %s quiet tick (%s)", loop.id, verdict.body)
            return True

        # WAKE, and FALLBACK, both spend a turn. FALLBACK is counted separately
        # from a wake so the metering cannot flatter itself: a gate that never
        # works would otherwise read as a busy, well-used watch.
        monitor.quiet_streak = 0
        if verdict.outcome is irq.Outcome.WAKE:
            # NOT charged here. A wake is a DELIVERED turn, and this tick has not
            # delivered one yet -- the fire that follows can still be refused (a
            # busy slot, a callback error, a loop deactivated mid-flight). Charging
            # now would report a turn that never ran and would hand out the
            # follow-up allowance for it, so the next tick would skip its
            # observation to protect work that was never started. The charge is
            # claimed at the one point delivery is confirmed, in
            # :meth:`_run_fire_cycle`. A process that dies in between charges
            # nothing, which is the right direction: never invent a turn.
            self._pending_monitor_wake.add(loop.id)
        else:
            # A fallback is an OBSERVATION outcome, not a delivery, so it is
            # counted here where it happened.
            monitor.gate_fallbacks += 1
        monitor.last_observed_at = time.time()
        self._persist_soon()
        return False

    async def _timer(self, loop: NudgeLoop, delay: float | None = None) -> None:
        try:
            await asyncio.sleep(loop.idle_secs if delay is None else delay)
        except asyncio.CancelledError:
            return
        if shutdown_event.is_set():
            return
        if is_structured_monitor_loop(loop):
            assert loop.monitor is not None
            waiting_for_terminal_completion = self._waits_for_terminal_completion(loop)
            if not loop.active and not waiting_for_terminal_completion:
                return
            if self._on_monitor_tick is None:
                if waiting_for_terminal_completion:
                    await self.record_monitor_completion_evidence_unavailable(
                        loop.id,
                        loop.monitor.last_wake_fingerprint,
                        now=time.time(),
                    )
                    return
                # A structured record must never fall through to legacy prompt
                # delivery when its typed controller is unavailable.
                await self._deactivate_unwired_monitor(loop.id)
                return
            self._firing.add(loop.id)
            try:
                await self._on_monitor_tick(loop)
            except Exception:
                logger.exception("structured monitor tick failed for %s", loop.id)
            finally:
                self._firing.discard(loop.id)
                self._rearm_pending.discard(loop.id)
                if (
                    (loop.active or self._waits_for_terminal_completion(loop))
                    and loop.id in self._loops
                    and loop.next_due_ts > 0
                ):
                    self._arm_from_deadline(loop)
            return
        # Kill switch: sentinel file present?
        if loop.stop_sentinel_path and Path(loop.stop_sentinel_path).exists():
            logger.info("AutoNudge: stop sentinel found for %s — removing loop", loop.id)
            await self.remove(loop.id)
            return
        # Cycle cap reached?
        if loop.max_cycles and loop.cycle_count >= loop.max_cycles:
            logger.info("AutoNudge: loop %s reached max_cycles — deactivating", loop.id)
            await self.update(loop.id, active=False, stopped_reason="cycle_cap")
            # Signal the cap. Reaching max_cycles is NOT a successful finish —
            # the loop ran out of cycles with its goal possibly unmet — yet the
            # only trace used to be this log line plus an ``updated`` event
            # indistinguishable from a user pressing Stop. A capped-out babysit
            # was therefore impossible to tell apart from the agent stopping on
            # its own. ``expired`` is emitted so an observer can raise a
            # notification the user actually sees.
            #
            # Emitted AFTER update() (which already persisted active=False and
            # emitted ``updated``), so a subscriber handling ``expired`` always
            # observes the loop in its final deactivated state. Deliberately a
            # NEW event kind rather than overloading ``updated``: the many
            # benign updates (message edits, manual pause) must not notify.
            self._emit("expired", loop)
            return
        # Wall-clock budget spent? Checked AFTER the cycle cap (both exhausted
        # → the cap wins, keeping historical wording) and BEFORE the fire, so
        # a spent budget never buys one more unattended turn. Same terminal
        # treatment as the cap: deactivate (inspectable/restartable, not
        # removed) and emit ``expired`` so the existing observer raises a
        # user-visible notification — a budget that stops a loop silently
        # would be indistinguishable from the agent stopping on its own.
        if runtime_budget_exceeded(loop):
            logger.info(
                "AutoNudge: loop %s exceeded max_runtime_secs=%d — deactivating",
                loop.id,
                loop.max_runtime_secs,
            )
            await self.update(loop.id, active=False, stopped_reason="runtime_budget")
            self._emit("expired", loop)
            return
        # Proved unable to act? Checked LAST, so a loop that is also out of
        # cycles or budget still reports the bound it historically would have. This one is reactive by construction: it fires only on recorded
        # evidence that a cycle's approval went unanswered (see
        # ``notify_approval_stalled``), never on a reading of whether a grant
        # happens to be in force — a loop that only ever calls auto-approved
        # tools needs no grant, and stopping it would turn a working
        # configuration into a stopped one.
        #
        # Same terminal treatment as the other bounds: deactivate rather than
        # remove, so the loop stays inspectable and can be resumed once the
        # operator restores the authorization it cannot obtain for itself, and
        # emit ``expired`` so the notifier tells them it stopped rather than
        # finished. Without this the loop keeps waking, dispatching, being
        # declined and spending its cap on cycles that were never able to work.
        if loop.approval_stalled:
            logger.info(
                "AutoNudge: loop %s cannot obtain tool approval — deactivating "
                "instead of firing cycle %d",
                loop.id,
                loop.cycle_count + 1,
            )
            await self.update(loop.id, active=False, stopped_reason=APPROVAL_STALL_REASON)
            self._emit("expired", loop)
            return
        # Fire. Update state only if the callback reports actual delivery —
        # otherwise skipped nudges (e.g. slot mid-turn) inflate cycle_count and
        # prematurely trip max_cycles. Missing callback → nothing to deliver.
        if self._on_fire is None:
            return
        # Probe gate. For a monitor loop, decide whether this tick is worth a
        # turn BEFORE spending one: a quiet tick returns here having cost one
        # bounded subprocess and no model call at all, which is the entire
        # saving this path exists for. A loop with no monitor -- and a monitor
        # whose subject has no probe, or whose probe failed -- falls straight
        # through to the unchanged legacy fire, so the absence of a gate can
        # never be the reason a loop goes silent.
        #
        # This moves what ``max_cycles`` bounds. ``cycle_count`` only advances on
        # a DELIVERED fire, so for a gated loop the cap counts delivered TURNS
        # rather than ticks. Not "wakes": a floor delivery, a fallback and a
        # follow-up are all delivered turns that advance it, and only quiet ticks
        # are free. Calling it wakes would undercount what the number actually
        # bounds, which is what the user pays for. A watch can still sit on a pull
        # request for days inside a small cap, which is the intended reading of the
        # number, and monitor_start's own description says so at the arming surface.
        if await self._monitor_tick_is_quiet(loop):
            # A quiet tick MUST re-arm itself. Nothing else will: the delivered
            # paths re-arm through notify_turn_complete (dashboard slots) or
            # through the fire cycle's own exit (channel keys), and a quiet tick
            # reaches neither. Returning here without arming would make the FIRST
            # quiet observation the last one the watch ever makes -- the exact
            # silent failure this gate is otherwise built to avoid, and invisible
            # from outside because a dead watch and a calm one look identical.
            #
            # Self-re-arm from inside the running timer is the supported pattern
            # (see _cancel_timer, which refuses to cancel the current task), and
            # is what the delivered path's own `finally` already does.
            #
            # ONLY while the loop is still live, though. "Do not spend a turn" and
            # "keep watching" are different answers, and the terminal verdict
            # returns the first while having just deactivated the loop: re-arming
            # on that would poll a merged pull request forever and re-emit its
            # expiry notification on every tick. Registration is checked too, so
            # a loop removed during the observation is not resurrected by its own
            # in-flight tick.
            if loop.active and loop.id in self._loops:
                loop.next_due_ts = time.time() + loop.idle_secs
                self._persist_soon()
                self._arm_from_deadline(loop)
            return
        self._firing.add(loop.id)
        try:
            await self._run_fire_cycle(loop)
        finally:
            self._firing.discard(loop.id)
            # A re-arm requested DURING the fire window (a dashboard turn that
            # completed while we were still persisting) was deferred rather than
            # applied, because applying it would have cancelled this very task
            # mid-persist. Apply it now that the window is closed — dropping it
            # would leave a dashboard loop with no armed timer at all, since the
            # delivered path relies on notify_turn_complete for those slots.
            if loop.id in self._rearm_pending:
                self._rearm_pending.discard(loop.id)
                if loop.active and loop.id in self._loops:
                    self._arm_from_deadline(loop)

    async def _terminal_still_holds(self, loop: NudgeLoop, monitor: MonitorState) -> bool:
        """Re-observe the subject and report whether the OWED terminal still holds.

        Used only where a settlement is about to deactivate a loop, because that is the
        one action here that stops work silently. The answer is deliberately asymmetric:
        only a fresh terminal that CARRIES THE SAME CLASSIFICATION returns True, so an
        unobservable subject -- a failed fetch, a probe defect, a binding that no longer
        resolves -- keeps the watch alive rather than letting an absence of evidence
        retire it.

        Matching the classification matters as much as matching the outcome. A pull
        request can be closed, reopened and MERGED inside one channel turn, and a
        revalidation that accepted any terminal would then settle the merge under the
        stale "blocked" marker and announce an unmerged close. When the two disagree the
        owed terminal is simply gone: this returns False, the debt is dropped, and the
        next tick records the real one with the right classification.

        Reuses the tick's probe machinery, and the same ``"merged" in keys`` rule the
        tick's own terminal branch applies, instead of adding a marker to remember what
        was already delivered. This review has paid for a defect at an existing site for
        each new piece of per-loop state, so re-asking is the cheaper way to answer.
        """
        target = targets.infer(loop.message)
        probe = probes.build(monitor.kind)
        if target is None or probe is None:
            # Cannot re-check, so cannot confirm. Keep the loop alive.
            return False
        # A DISTINCT identity, because this call throws its verdict away. ``identity``
        # is the kernel's dedupe key -- ``poll``'s own contract says it "replaces the
        # cron job id in the state digest, so two drivers watching one subject keep
        # independent dedupe memories" -- so sharing the tick's key would let this
        # re-read consume the tick's credit: a reopened subject with a fresh comment
        # would be marked reported here, and then the next real tick would read it as
        # unchanged and go quiet until the streak floor. A poll whose answer is
        # discarded must not be able to swallow a signal, so it observes on its own
        # memory and leaves the tick's untouched.
        identity = f"{loop.id}:{target.host_key}:terminal-recheck"
        try:
            verdict = await asyncio.get_running_loop().run_in_executor(
                None, lambda: irq.poll(identity, target.message, probe)
            )
        except Exception:
            logger.warning(
                "AutoNudge: could not revalidate the terminal verdict for %s -- keeping "
                "the watch alive rather than settling on a stale observation",
                loop.id,
                exc_info=True,
            )
            return False
        if verdict.outcome is not irq.Outcome.TERMINAL:
            return False
        fresh = "success" if "merged" in verdict.keys else "blocked"
        if fresh != monitor.terminal_pending:
            logger.info(
                "AutoNudge: loop %s owed a %s settlement but now observes %s -- dropping "
                "the owed one rather than announcing the wrong ending",
                loop.id,
                monitor.terminal_pending,
                fresh,
            )
            return False
        return True

    async def _run_fire_cycle(self, loop: NudgeLoop) -> None:
        """Fire once, then persist bookkeeping and decide the re-arm.

        Runs entirely inside the caller's ``_firing`` window so a concurrent
        ``update()`` never cancels this task between delivery and persistence.
        """
        if self._on_fire is None:
            return
        # Mark the fire window so a concurrent update() defers its re-arm
        # instead of cancelling this task mid-turn (see update()). The window
        # stays open through the post-delivery bookkeeping and the re-arm
        # decision, NOT just the callback: clearing it the moment _on_fire
        # returned let a waiting update() cancel this task while it was parked
        # on _persist_locked(), so the delivered cycle was never written and the
        # loop could run extra cycles after a restart. _run_fire_cycle owns the
        # window; this method is the body.
        try:
            delivered = await self._on_fire(loop)
        except Exception:
            delivered = False
            # Full traceback only on the first failure of a streak; subsequent
            # failures stay at debug so a permanently-wedged callback can't spam
            # a traceback every re-arm.
            if self._rearm_fail_count.get(loop.id, 0) == 0:
                logger.exception("AutoNudge fire callback failed for %s", loop.id)
            else:
                logger.debug(
                    "AutoNudge fire still failing for %s (streak=%d)",
                    loop.id,
                    self._rearm_fail_count.get(loop.id, 0) + 1,
                )
        claimed_wake = loop.id in self._pending_monitor_wake
        self._pending_monitor_wake.discard(loop.id)
        claimed_floor = loop.id in self._pending_floor_tick
        self._pending_floor_tick.discard(loop.id)
        if claimed_wake and loop.monitor is not None:
            # Delivery is settled either way now -- landed or refused -- so the doubt
            # the wake carried across the fire is discharged here. A debounced write
            # is enough: this process survived, and a lost clear only costs one extra
            # fire on the next tick.
            loop.monitor.poll_in_flight = False
            self._persist_soon()
        if not delivered and loop.monitor is not None and loop.id in self._loops:
            # A REFUSED fire must not consume the tick that earned it. Two credits
            # are at stake and both are spent by the time we get here: a claimed
            # wake, and a follow-up allowance the gate decremented to let this
            # tick through. Neither can be recovered by simply re-arming, because
            # the kernel has already DEDUPED the observation this fire was
            # carrying -- the next tick would look at an unchanged subject, judge
            # it quiet, and the signal would not come back until the streak floor.
            # A busy slot is ordinary (the user is typing), so that is a routine
            # path to losing a real wake.
            #
            # Granting one gate-free tick makes the next tick RETRY the delivery
            # instead of re-observing. Set rather than incremented, so a
            # permanently refusing callback cannot accumulate an unbounded
            # bypass; the existing per-failure backoff bounds how fast it retries.
            loop.monitor.followup_ticks = _WAKE_FOLLOWUP_TICKS
            if claimed_wake:
                # And keep the wake OWED. The claim was discarded above on the
                # assumption that reaching here meant the wake had been accounted
                # for, but a refused fire accounts for nothing: the next tick takes
                # the observation-free bypass, DELIVERS the turn, and finds no claim
                # to charge -- so a wake that really happened and really woke the
                # agent was missing from ``wakes`` entirely. That counter is the one
                # artifact this PR exists to make trustworthy, and omitting a
                # delivered wake makes the saving look BETTER than it is, which is
                # the same dishonesty as the zero nothing surfaced before.
                #
                # Re-adding cannot double-charge: the charge happens once, at the
                # single point delivery is confirmed, and the claim is discarded
                # there. A retry that is refused again re-owes it, which is correct
                # and bounded by the same backoff that bounds the retry itself.
                self._pending_monitor_wake.add(loop.id)
            if claimed_floor:
                # Same reasoning: a refused floor delivery spent nothing, so the charge
                # stays owed rather than being recorded or dropped.
                self._pending_floor_tick.add(loop.id)
            self._persist_soon()
        if delivered:
            # BEFORE the settlement below, not after. That block carries a comment
            # forbidding an early RETURN precisely so this bookkeeping still runs --
            # but a re-raised ``CancelledError`` leaves by the same door a return
            # would, and the terminal write deliberately DRAINS before propagating,
            # so the loop would be committed as finished while the turn that carried
            # the news went uncounted. Recording a delivery that has already happened
            # cannot be wrong; deferring it past a re-raise can.
            self._rearm_fail_count.pop(loop.id, None)
            loop.cycle_count += 1
            loop.last_fire_ts = time.time()
        if delivered and loop.monitor is not None and loop.monitor.terminal_pending:
            # The owed turn landed, so the watch can be closed now -- and only now.
            # Until this point the loop stayed live on purpose, so a refused fire
            # would re-arm and retry rather than leave the channel unaware. The
            # probe re-raises a terminal state on every tick (it is not deduped),
            # which is what makes that retry converge.
            # SERIALIZED, like the gate's own settlement. ``update`` takes the
            # MAINTENANCE lock and awaits inside it, so without holding that same
            # lock a retarget could land between the read and the write here and
            # have its new subject deactivated by the old subject's finish. This is
            # the site the previous round named as still open; closing it needs the
            # lock, not merely the ordering fix that round shipped.
            #
            # Safe to take here: this runs after ``_on_fire`` has returned, and the
            # timer that called us does not hold the lock -- the same evidence that
            # lets the gate's settlement take it.
            settle_lock = await self._acquire_mutation_lock(loop.id)
            # No early RETURN in here: the rest of this fire cycle still has to
            # charge the wake and run its re-arm bookkeeping. Skipping that to bail
            # out of a settlement would trade one defect for another.
            if settle_lock is not None:
                try:
                    # Re-read under the lock, and re-check that the monitor is still
                    # THERE. Waiting for the lock is an await, so a retarget can
                    # clear ``loop.monitor`` to None in that gap -- and dereferencing
                    # it then raises out of the fire cycle, which leaves the newly
                    # retargeted loop active with no timer: a watch that never ticks
                    # again. The earlier checks covered the debt and the
                    # registration but not the object itself.
                    monitor = loop.monitor
                    pending = monitor.terminal_pending if monitor is not None else ""
                    settle_now = bool(monitor is not None and pending and loop.id in self._loops)
                    if settle_now and monitor is not None:
                        if not await self._terminal_still_holds(loop, monitor):
                            # The subject came back while the turn was being delivered.
                            # Every earlier guard for a reopened subject lives on the
                            # NEXT TICK -- the debt clearing added in round 31, the
                            # forced re-observation added in round 34 -- and this
                            # settlement runs before any tick can happen, so the window
                            # between the terminal observation and the turn landing had
                            # no evidence in it at all. A channel turn runs inline and
                            # can take minutes, which is long enough for a pull request
                            # to be reopened.
                            #
                            # Settling is the one action that STOPS work, so it needs a
                            # CONFIRMED terminal rather than merely an unrefuted one:
                            # anything else -- reopened, or simply unobservable -- leaves
                            # the watch alive. Failure resolves toward spending, here as
                            # everywhere else in this file.
                            #
                            # SKIPPED, not returned from. This block's own comment forbids
                            # an early exit because the rest of the fire cycle still has
                            # to run, and round 35 was that exact rule being broken by a
                            # re-raise leaving through the same door.
                            monitor.terminal_pending = ""
                            self._persist_soon()
                            logger.info(
                                "AutoNudge: loop %s had its subject come back while the "
                                "final turn was delivered -- dropping the owed settlement "
                                "and keeping the watch alive",
                                loop.id,
                            )
                            settle_now = False
                    if settle_now and monitor is not None:
                        restore = (
                            pending,
                            monitor.outcome,
                            monitor.stopped_reason,
                            monitor.stopped_at,
                            loop.active,
                            loop.stopped_reason,
                        )
                        monitor.terminal_pending = ""
                        monitor.outcome = (
                            MonitorOutcome.SUCCESS
                            if pending == "success"
                            else MonitorOutcome.BLOCKED
                        )
                        monitor.stopped_reason = MONITOR_TERMINAL_REASON
                        monitor.stopped_at = time.time()
                        loop.stopped_reason = MONITOR_TERMINAL_REASON
                        loop.active = False
                        # PERSIST BEFORE ANNOUNCING -- the same rule the gate's own
                        # settlement follows. This site was added two rounds later
                        # and did not inherit it: the delivered path does reach a
                        # write further down, but it is AFTER the emit, so a failed
                        # write left memory reporting a finish while the record
                        # still said active-and-owed, and the restart would deliver
                        # the final turn a second time.
                        try:
                            async with self._lock:
                                await self._write_monitor_snapshot_locked()
                        except asyncio.CancelledError:
                            # Committed before the cancellation propagates, so the
                            # user must hear it now or never -- a restart reads the
                            # loop as settled and no longer owes a turn.
                            self._emit("expired", loop)
                            raise
                        except Exception:
                            (
                                monitor.terminal_pending,
                                monitor.outcome,
                                monitor.stopped_reason,
                                monitor.stopped_at,
                                loop.active,
                                loop.stopped_reason,
                            ) = restore
                            logger.exception(
                                "AutoNudge: could not persist the delivered terminal "
                                "settlement for %s -- leaving the watch live so it "
                                "retries",
                                loop.id,
                            )
                        else:
                            self._emit("expired", loop)
                finally:
                    settle_lock.release()
        if claimed_wake and delivered and loop.monitor is not None:
            # The turn happened, so it is a wake, and only now does the agent own
            # work the probe cannot see -- which is what the follow-up allowance
            # protects. A refused fire falls through here uncharged.
            #
            # No persist call of its own: the delivered path below reaches
            # ``await self._persist_locked()`` with no await in between, so these
            # counters are already in the state that write serialises -- and that
            # write is the stronger one, since it holds the lock and cannot be
            # clobbered by a concurrent update()'s snapshot.
            loop.monitor.wakes += 1
            loop.monitor.followup_ticks = _WAKE_FOLLOWUP_TICKS
        if delivered and claimed_floor and loop.monitor is not None:
            # The floor's turn happened. No follow-up allowance goes with it: the floor
            # exists to break a silence, not to protect work the agent had already
            # started, so there is nothing in progress for a bypassed tick to shield.
            loop.monitor.floor_ticks += 1
        if not delivered:
            # If the fire path already removed the loop (e.g. slot missing →
            # remove()), do NOT resurrect it with a fresh timer — that would
            # orphan-poll forever. Clear the streak and stop.
            if loop.id not in self._loops:
                self._rearm_fail_count.pop(loop.id, None)
                return
            # A concurrent update() may have DEACTIVATED this loop while the
            # callback was in flight; that update deliberately deferred the
            # cancel to avoid killing the turn, so the failure path must honour
            # the pause instead of re-arming. Otherwise "stop the loop" during a
            # cycle whose delivery then fails silently resumes unattended tool
            # execution.
            if not loop.active:
                logger.info(
                    "AutoNudge: loop %s was deactivated mid-fire — not re-arming",
                    loop.id,
                )
                self._rearm_fail_count.pop(loop.id, None)
                return
            # Slot was busy mid-turn, or the fire callback errored. Do NOT end
            # the loop — re-arm so it self-heals and never depends solely on the
            # external notify_turn_complete hook (skipped on a slot's error/
            # timeout/cancel exit paths). Escalate the delay per consecutive
            # failure so a never-delivering loop backs off to a slow poll
            # instead of hammering, capped by idle_secs and _REARM_MAX_BACKOFF.
            n = self._rearm_fail_count.get(loop.id, 0) + 1
            self._rearm_fail_count[loop.id] = n
            shift = min(n - 1, _REARM_BACKOFF_MAX_SHIFT)
            backoff = min(
                _REARM_BACKOFF_SECS * (2**shift),
                _REARM_MAX_BACKOFF_SECS,
                loop.idle_secs,
            )
            self._arm_timer(loop, delay=backoff)
            return
        # Delivered — the failure streak and the turn accounting were already
        # recorded above, before the terminal settlement could re-raise past them.
        loop.next_due_ts = 0.0
        # Persist through the shared locked+offloaded path so this bookkeeping
        # cannot be clobbered by a concurrent update()'s snapshot (and so the
        # fsync stays off the event loop).
        await self._persist_locked()
        self._emit("fired", loop)
        # POST-DELIVERY budget check: the budget gates when turns START, so a
        # slow in-flight turn can overshoot it (bounded by the transport's
        # per-turn ceiling, constants.CHAT_TURN_TIMEOUT — this service must
        # not cancel a running turn; see the mid-fire contracts above). But
        # once the turn HAS finished, a spent budget must take effect NOW —
        # deactivating here instead of on the next idle timer closes the
        # window where notify_turn_complete arms another full idle cycle for
        # a loop that is already over budget.
        if runtime_budget_exceeded(loop) and loop.active and loop.id in self._loops:
            logger.info(
                "AutoNudge: loop %s exceeded max_runtime_secs=%d during its turn "
                "— deactivating post-delivery",
                loop.id,
                loop.max_runtime_secs,
            )
            await self._update_unserialized(loop.id, active=False, stopped_reason="runtime_budget")
            self._emit("expired", loop)
            return
        # Channel-bound loops (Slack/Discord/...) have no dashboard
        # turn-lifecycle hook to re-arm them (notify_turn_complete never fires
        # for these keys), so they self-re-arm on a fixed interval. The fire
        # callback runs the turn inline, so the next fire lands idle_secs
        # after the previous turn finished; the busy-skip + backoff above
        # handles any overlap.
        if is_channel_key(loop.slot_key) and loop.active and loop.id in self._loops:
            self._arm_from_deadline(loop)


class _AutoNudgeMaintenanceView:
    """Store operations that are safe inside ``maintenance_service``'s lock."""

    def __init__(self, service: AutoNudgeService) -> None:
        self._service = service
        self._quiescing: set[str] = set()

    def _release(self) -> None:
        for loop_id in self._quiescing:
            self._service._end_maintenance_quiesce(loop_id)
        self._quiescing.clear()

    def list_all(self) -> list[NudgeLoop]:
        return self._service.list_all()

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return self._service.get_by_slot(slot_key)

    async def deactivate_and_wait(self, loop_id: str) -> bool:
        self._quiescing.add(loop_id)
        quiesced = await self._service._deactivate_and_wait_unserialized(loop_id)
        if quiesced:
            return True
        else:
            self._service._end_maintenance_quiesce(loop_id)
            self._quiescing.discard(loop_id)
            return False

    async def remove(self, loop_id: str) -> None:
        await self._service._remove_unserialized(loop_id)
        self._service._end_maintenance_quiesce(loop_id)
        self._quiescing.discard(loop_id)
