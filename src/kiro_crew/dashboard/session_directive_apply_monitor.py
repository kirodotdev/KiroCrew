"""The monitor directives: start, watch, update and stop a loop, and stop autonudge.

The loop binding a directive acts on, the refusals of a stale wake's arm or stop, and the
monitor start, watch, update and stop paths with their structured forms. A new monitor
directive goes here. Its function-local imports are deliberate; the facade's module docstring
says why.

Served through :mod:`kiro_crew.dashboard.session_directive_apply`, which callers import
and tests patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to
this module, and this module never imports it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiro_crew.apps.builtins.auto_research.session_keys import is_owned_research_slot
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    AUTONUDGE_STOP_REASON,
    CONSECUTIVE_FAILURE_REASON,
    MONITOR_TERMINAL_REASON,
    STOP_SENTINEL_REASON,
    is_channel_key,
)
from kiro_crew.autonudge_judge import screen_phrase
from kiro_crew.dashboard.session_directive_apply_outcome import (
    _describe_interval,
    _describe_next_wake,
    _DirectiveDenied,
    _surface_arm_success,
)

logger = logging.getLogger("kiro_crew.dashboard.session_directive_apply")


# ── autonudge trio ──────────────────────────────────────────────────────────


def _binding(session_key: str) -> str | None:
    from kiro_crew.autonudge import binding_key_for

    return binding_key_for(session_key)


def _structured_binding(session_key: str) -> str | None:
    from kiro_crew.autonudge import structured_monitor_binding_key_for

    return structured_monitor_binding_key_for(session_key)


def _refuse_stale_wake_arm(
    session_key: str,
    wake_loop_id: str,
    *,
    require_current: bool = False,
) -> None:
    """Refuse stale-wake arms and mutations.

    A delivered wake turn can outlive the Stop that ended its loop: a
    prompt-loop Stop REMOVES the row, and a structured stop retains it as a
    ``USER_STOP`` record. The wake carries its loop id
    (``_fire_dashboard_nudge`` passes it with the self-wake mark), so the row
    that fired this turn is read back here:

    * row gone -> the loop was removed under the turn (a Stop, a session close):
      refused, nothing may be created in its place;
    * row present but stopped by a person -- a manual pause, a user stop, an
      empty reason, anything ``_stopped_row_is_replaceable`` fails closed on
      -> refused, the retained record is evidence;
    * row present and active, or deactivated by its own cycle cap, runtime
      budget, terminal subject, dropped sentinel or over-policy stored budget
      -> admitted. The arm then meets the create-only and ``replace_stopped``
      rules unchanged, so an active row still answers 409 and only a
      system-imposed stop is displaced.

    A wake that names no loop id (a caller outside the fire path) falls back to
    the session's bound row, which answers the same three ways.
    """
    from kiro_crew.autonudge import _stopped_row_is_replaceable, get_instance

    svc = get_instance()
    if svc is None:
        # The applier refuses a disabled host on its own, with its own wording.
        return
    if wake_loop_id:
        row = svc.get_by_id(wake_loop_id)
    else:
        binding = _binding(session_key)
        row = svc.get_by_slot(binding) if binding else None
    if require_current:
        binding = _binding(session_key)
        current = svc.get_by_slot(binding) if binding else None
        if current is None or getattr(current, "id", "") != wake_loop_id:
            raise _DirectiveDenied(
                "Monitor NOT changed: the loop that delivered this wake is no longer "
                "this session's monitor; a new user request is required."
            )
    if row is None:
        raise _DirectiveDenied(
            "Monitor NOT armed: the loop that delivered this wake was stopped or "
            "removed, so this turn may not start a replacement. A new user request "
            "is required."
        )
    if not getattr(row, "active", False) and not _stopped_row_is_replaceable(row):
        raise _DirectiveDenied(
            "Monitor NOT armed: the loop that delivered this wake was stopped by a "
            "person and is retained, so this turn may not start a replacement. A new "
            "user request is required."
        )


def _refuse_stale_wake_stop(session_key: str, wake_loop_id: str) -> None:
    """Refuse a wake-delivered stop that would not end the wake's own live loop.

    Two checks. Identity: the loop bound to this session must be the one that
    delivered this turn, or a stale wake would end a loop a person armed after
    it. Retention: a row a person paused or stopped while this wake was still
    running (``"manual"``, an empty reason, anything
    ``_stopped_row_is_replaceable`` fails closed on) is retained evidence, and a
    legacy stop REMOVES its row, so the wake may not delete it. A row the system
    deactivated (cycle cap, runtime budget, terminal subject) still passes;
    ending it is the stop applier's decision.
    """
    from kiro_crew.autonudge import _stopped_row_is_replaceable, get_instance

    svc = get_instance()
    if svc is None:
        # The applier answers a disabled host on its own, with its own wording.
        return
    binding = _binding(session_key)
    current = svc.get_by_slot(binding) if binding else None
    if current is None or getattr(current, "id", "") != wake_loop_id:
        raise _DirectiveDenied(
            "Monitor NOT stopped: the loop that delivered this wake is no longer "
            "this session's monitor, so this turn may not stop the loop that "
            "replaced it. A new user request is required."
        )
    if not getattr(current, "active", False) and not _stopped_row_is_replaceable(current):
        raise _DirectiveDenied(
            "Monitor NOT stopped: the loop that delivered this wake was paused or "
            "stopped by a person and is retained, so this turn may not remove it. "
            "A new user request is required."
        )


_STOP_NOT_APPLIED = (
    "Monitor NOT stopped: the monitor service did not apply the stop (it is "
    "paused for maintenance), so the loop may still be running. Retry the stop."
)


class _StaleWakeStopGuard:
    """Re-take a stale-wake stop's identity and retention decision under the write lock.

    ``_refuse_stale_wake_stop`` answers early and lock-free; this guard is the
    authoritative re-check. The service calls it on the live row inside the same
    ``_lock`` hold that mutates it and reads only the boolean. When the row is
    missing, the service calls ``on_absent`` in that same hold instead, so the
    stop can tell a row that is simply gone (its goal already holds) from one a
    concurrent arm REPLACED (the replacement is still running). A write that
    reached neither never took the lock, and the stop may not claim success.
    """

    def __init__(self, svc: Any, binding: str, wake_loop_id: str) -> None:
        self._svc = svc
        self._binding = binding
        self._wake_loop_id = wake_loop_id
        #: ``""`` until a check fails; then ``"current"`` or ``"person"``.
        self.refusal = ""
        #: Set when the service found no row under the lock and nothing replaced it.
        self.gone = False

    def on_absent(self) -> None:
        """Called by the service under ``_lock`` when the wake's row is missing."""
        current = self._svc.get_by_slot(self._binding)
        if current is not None and getattr(current, "id", "") != self._wake_loop_id:
            self.refusal = "current"
            return
        self.gone = True

    def raise_unless_stopped(self, applied: bool) -> None:
        """Raise unless the write applied or the row was confirmed gone under the lock."""
        if applied:
            return
        if self.refusal:
            self.raise_refusal()
        if self.gone:
            return
        raise _DirectiveDenied(_STOP_NOT_APPLIED)

    def __call__(self, row: Any) -> bool:
        from kiro_crew.autonudge import _stopped_row_is_replaceable

        current = self._svc.get_by_slot(self._binding)
        if (
            getattr(row, "id", "") != self._wake_loop_id
            or getattr(row, "slot_key", "") != self._binding
            or current is None
            or getattr(current, "id", "") != self._wake_loop_id
        ):
            self.refusal = "current"
            return False
        if not getattr(row, "active", False) and not _stopped_row_is_replaceable(row):
            self.refusal = "person"
            return False
        return True

    def raise_refusal(self) -> None:
        """Refuse the stop for the check that failed. Callers raise only when one did."""
        if self.refusal == "person":
            raise _DirectiveDenied(
                "Monitor NOT stopped: the loop that delivered this wake was paused or "
                "stopped by a person and is retained, so this turn may not remove it. "
                "A new user request is required."
            )
        raise _DirectiveDenied(
            "Monitor NOT stopped: the loop that delivered this wake is no longer "
            "this session's monitor, so this turn may not stop the loop that "
            "replaced it. A new user request is required."
        )


async def _monitor_start(
    state: Any,
    session_key: str,
    args: dict[str, Any],
    *,
    slot: Any = None,
    self_arm_ok: bool = False,
    producer_is_channel: bool,
) -> str:
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge
    from kiro_crew.monitoring.models import MonitorCreationSurface

    svc = get_instance()
    # Not-applied paths RAISE so the wrapper audits them as denied — a plain
    # return here would be derived as ``success`` and corrupt the SEL chain
    # for an effect that never happened (the loop was not armed).
    if svc is None:
        raise _DirectiveDenied("Monitor loop NOT armed: auto-nudge is disabled on this host.")
    binding = _binding(session_key)
    if not binding:
        raise _DirectiveDenied("monitor_start is not supported from this session type.")
    idle_secs = int(args.get("idle_secs") or 300)
    max_cycles = int(args.get("max_cycles") or 0)
    max_runtime_secs = int(args.get("max_runtime_secs") or 0)
    # Absent means gated, matching the tool's default: a directive written before
    # the flag existed must not read as an opt-out.
    raw_gate = args.get("gate")
    gate = True if raw_gate is None else bool(raw_gate)
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=binding,
        message=str(args.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path="",
        max_runtime_secs=max_runtime_secs,
        # Every kwarg here is named explicitly -- there is no ``**args`` splat --
        # so a field the tool accepts but this call omits is silently dropped
        # rather than erroring. The authorizer owns the cap and both redaction
        # passes, so nothing is validated twice by routing through it.
        banner=str(args.get("banner") or ""),
        # Named explicitly for the reason the comment above gives: this call has no
        # splat, so a brief the tool accepted and this line omitted would be dropped
        # without a word -- the loop would arm with no judge and nothing would say so.
        judge=args.get("judge") if isinstance(args.get("judge"), dict) else None,
        # Named explicitly for the reason the two lines above are: this call has no
        # splat, so a watch the tool accepted and this line omitted would validate
        # cleanly and then be discarded -- the caller told its request was valid and
        # handed an ordinary timer, which is the exact contract the schema's own
        # absence-pin was holding the field back to avoid.
        watch=str(args.get("watch") or ""),
        source="mcp-directive",
        caller="session-directive",
        gate=gate,
        replace_existing=False,
        # The directive re-arm is the one path allowed to displace a retained
        # STOPPED row: monitor_update's approval-stall refusal names
        # monitor_start as the remedy, so refusing here deadlocks it.
        replace_stopped=True,
        # SELF-ARM provenance: this consumer applies the directive to the exact
        # session whose turn produced it (the facade's docstring), so the binding IS
        # the initiator -- for the two producers ``apply_session_directive``
        # admits (a human-started turn, or this slot's own loop wake). A cron
        # injection, a sub-agent sharing the slot or an app-driven turn runs in
        # the same session without being it, and a loop such a turn armed would
        # be the outsider's loop wearing the member's key; those pass "".
        initiator_slot_key=binding if self_arm_ok else "",
        creation_surface=(
            MonitorCreationSurface.CHANNEL
            if producer_is_channel
            else MonitorCreationSurface.DASHBOARD
        ),
    )
    if error is not None:
        # The authorizer already audited its own refusal; the wrapper's record
        # for THIS directive must agree (denied), not overwrite it as success.
        # The status rides along so the reader can tell a 409 refusal (mode,
        # existing automation) from a 404 (session gone) or 503 (audit down).
        raise _DirectiveDenied(f"Failed to start monitor loop: {error} [status {status}]")
    cap = f", stopping after {max_cycles} cycles" if max_cycles else ", with NO cycle cap"
    if max_runtime_secs:
        cap += f", runtime budget {max_runtime_secs}s"
    # Read the cadence off the ARMED loop, not off the request. This surface knows
    # something the MCP tool's own ack has to infer: whether a monitor was actually
    # attached. Reporting "re-injects every {idle_secs}s" for a gated loop is untrue --
    # a quiet tick spends no turn at all -- and this applier defaults ``gate`` to True
    # a few lines above, so the unconditional promise was wrong for its own default.
    armed_monitor = getattr(loop, "monitor", None)
    if armed_monitor is not None and getattr(loop, "gate", False):
        cadence = (
            f"observing {armed_monitor.target} every {idle_secs}s and re-injecting the "
            "message only when the tick needs you -- "
            f"{screen_phrase()}, and a raised wake lands up to about one "
            "interval after the tick that saw it"
        )
    else:
        cadence = f"the message re-injects every {idle_secs}s"
    loop_id = str(getattr(loop, "id", "?"))
    first_wake = _describe_next_wake(loop)
    _surface_arm_success(
        state,
        slot,
        f"loop {loop_id} · every {_describe_interval(idle_secs)} · "
        + ("no cycle cap" if not max_cycles else f"{max_cycles}-cycle cap")
        + (f" · {first_wake}" if first_wake else ""),
    )
    return (
        f"Monitor loop {loop_id} started on this session: {cadence} "
        f"(user messages defer a due fire "
        f"to their turn's end without restarting the countdown){cap}"
        + (f"; {first_wake}" if first_wake else "")
        + ". End your turn now — the loop wakes you. Call autonudge_stop when the "
        "exit condition is met."
    )


async def _monitor_watch(
    state: Any,
    session_key: str,
    args: dict[str, Any],
    *,
    slot: Any = None,
    self_arm_ok: bool = False,
    producer_is_channel: bool,
) -> str:
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge
    from kiro_crew.monitoring.models import (
        MonitorBudgets,
        MonitorCreationSurface,
        MonitorState,
    )

    svc = get_instance()
    if svc is None:
        raise _DirectiveDenied("Structured monitor NOT armed: auto-nudge is disabled on this host.")
    binding = _structured_binding(session_key)
    if not binding:
        raise _DirectiveDenied("monitor_watch is not supported from this session type.")
    budgets = MonitorBudgets(
        max_runtime_secs=int(args["max_runtime_secs"]),
        max_agent_turns=int(args["max_agent_turns"]),
        max_tokens=int(args["max_tokens"]),
        max_provider_errors=int(args["max_provider_errors"]),
    )
    monitor = MonitorState(
        kind=str(args["kind"]),
        target=str(args["target"]),
        objective=str(args["objective"]),
        created_ts=time.time(),
        budgets=budgets,
        cadence_secs=int(args["cadence_secs"]),
        wake_instructions=str(args.get("wake_instructions") or ""),
    )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=binding,
        message=monitor.wake_instructions or "structured monitor",
        idle_secs=monitor.cadence_secs,
        max_cycles=0,
        max_runtime_secs=monitor.budgets.max_runtime_secs,
        source="mcp-directive",
        caller="session-directive",
        replace_existing=False,
        # Same opt-in as _monitor_start: a monitor stopped and retained for
        # inspection must not block this session's next directive arm.
        replace_stopped=True,
        monitor=monitor,
        # Self-arm provenance, same rule as _monitor_start: human-started turns only.
        initiator_slot_key=binding if self_arm_ok else "",
        creation_surface=(
            MonitorCreationSurface.CHANNEL
            if producer_is_channel
            else MonitorCreationSurface.DASHBOARD
        ),
    )
    if error is not None:
        raise _DirectiveDenied(f"Failed to start structured monitor: {error} [status {status}]")
    if loop is None:
        raise _DirectiveDenied(
            "Failed to start structured monitor: no monitor record was returned."
        )
    first_probe = _describe_next_wake(loop, verb="first probe")
    _surface_arm_success(
        state,
        slot,
        f"structured monitor {loop.id} on {monitor.target} · every "
        f"{_describe_interval(monitor.cadence_secs)}"
        + (f" · {first_probe}" if first_probe else ""),
    )
    return f"Structured monitor {loop.id} started on this session" + (
        f"; {first_probe}." if first_probe else "."
    )


async def _monitor_update(
    state: Any,
    session_key: str,
    args: dict[str, Any],
    *,
    self_arm_ok: bool = False,
    allow_rearm: bool = True,
    producer_is_channel: bool = False,
) -> str:
    from kiro_crew.autonudge import get_instance, is_structured_monitor_loop
    from kiro_crew.autonudge_authz import (
        _EXTERNAL_ARM_REFUSED_MODES,
        authorize_and_update_nudge,
        external_arm_refusal,
        is_self_arm,
    )
    from kiro_crew.autonudge_service.model import runtime_elapsed

    svc = get_instance()
    # Not-applied paths raise (audited denied) — see _monitor_start.
    if svc is None:
        raise _DirectiveDenied("Cannot update monitor loop: auto-nudge is disabled on this host.")
    binding = _binding(session_key)
    if not binding:
        raise _DirectiveDenied("monitor_update is not supported from this session type.")
    loop = svc.get_by_slot(binding)
    if not loop:
        raise _DirectiveDenied("No active monitor loop on this session to update.")
    if not allow_rearm and not loop.active:
        # SELF-WAKE REVIVAL: a delivered wake can outlive a Stop, so a stopped
        # loop is NOT a wake's to revive -- with one exception the bound-revival
        # rule below already owns. A legacy loop that deactivated on its OWN
        # cycle cap or runtime budget (the ``_timer`` stamps exactly these two
        # persisted reasons) was never stopped by a person, and a member's
        # final-cycle wake raising that bound is the loop managing its own
        # patrol lifecycle unattended. Everything else fails closed:
        # a manual pause, a user stop, a structured record (terminal by
        # contract, re-armed only by an explicit new watch), and an EMPTY
        # reason (a pre-field pause or a torn write, which the cycle-count
        # heuristic below may still read as a cap stop for a HUMAN turn but
        # which a wake must not trust). The revival itself still runs through
        # the bound-revival decision below, so a wake that does not raise the
        # stopping bound is refused there like any other patch.
        reason = str(getattr(loop, "stopped_reason", "") or "")
        self_wake_revivable = not is_structured_monitor_loop(loop) and reason in {
            "cycle_cap",
            "runtime_budget",
        }
        if not self_wake_revivable:
            raise _DirectiveDenied(
                "Monitor NOT rearmed: this loop stopped; a new user request is required."
            )
    # Read HERE, not at the write: ``get_by_slot`` hands back the LIVE row, so a token read
    # at the call site would already carry a concurrent rotation and never fail the compare.
    baseline_token = str(getattr(loop, "goal_token", "") or "")
    patch = dict(args.get("patch") or {})
    if is_structured_monitor_loop(loop):
        if _structured_binding(session_key) != binding:
            raise _DirectiveDenied("monitor_update is not supported from this session type.")
        return await _structured_monitor_update(
            state,
            svc,
            loop,
            patch,
            initiator=binding if self_arm_ok else "",
            producer_is_channel=producer_is_channel,
        )
    structured_only = sorted(
        set(patch)
        & {
            "target",
            "objective",
            "max_agent_turns",
            "max_tokens",
            "max_provider_errors",
            "wake_instructions",
        }
    )
    if structured_only:
        raise _DirectiveDenied(
            "monitor_update cannot apply structured fields to a legacy loop: "
            + ", ".join(structured_only)
        )
    cycle_count = int(getattr(loop, "cycle_count", 0) or 0)
    current_cap = int(getattr(loop, "max_cycles", 0) or 0)
    new_cap = patch.get("max_cycles", current_cap)
    # Capped-loop guard: a cap at/below the delivered count deactivates the loop
    # without another fire — refuse rather than promise a wake that never comes.
    if not (new_cap == 0 or new_cap > cycle_count):
        raise _DirectiveDenied(
            f"monitor_update: max_cycles={new_cap} is at or below this loop's "
            f"delivered cycle count ({cycle_count}), so it would deactivate "
            "without firing again. Pass a larger cap, or 0 for unlimited."
        )
    # Spent-budget guard, same shape as the cycle-cap one: a runtime budget
    # at/below the loop's elapsed age deactivates it on the next timer without
    # another fire — refuse rather than promise a wake that never comes. The age
    # is the timer's own reading (``runtime_elapsed``), so the two cannot disagree.
    if "max_runtime_secs" in patch:
        new_budget = int(patch["max_runtime_secs"] or 0)
        created_ts = float(getattr(loop, "created_ts", 0.0) or 0.0)
        elapsed = int(runtime_elapsed(loop, created_ts)) if created_ts else 0
        if new_budget and created_ts and elapsed >= new_budget:
            raise _DirectiveDenied(
                f"monitor_update: max_runtime_secs={new_budget} is at or below "
                f"this loop's elapsed runtime ({elapsed}s since it was armed), "
                "so it would deactivate without firing again. Pass a larger "
                "budget, or 0 for unlimited."
            )
    revived = False
    # Paused-loop protection: never silently resume unattended execution as a
    # side effect of a metadata edit — revive ONLY a loop stopped by one of its
    # own terminal bounds whose stopping bound is actually being raised. Keyed
    # on the PERSISTED ``stopped_reason`` recorded at deactivation time: the
    # cycle-count heuristic stays only as a legacy fallback for stores written
    # before the field existed, and the budget side has NO heuristic at all —
    # elapsed time keeps growing after a manual pause, so "budget looks spent"
    # cannot distinguish a pause from an expiry: a budget raise must never
    # resume a loop the user paused.
    if not getattr(loop, "active", True):
        reason = str(getattr(loop, "stopped_reason", "") or "")
        stopped_at_cap = reason == "cycle_cap" or (
            not reason and current_cap > 0 and cycle_count >= current_cap
        )
        raising_cap = "max_cycles" in patch and (new_cap == 0 or new_cap > current_cap)
        stopped_at_budget = reason == "runtime_budget"
        # A budget-raise passed the spent-budget guard above, so any budget in
        # the patch here is beyond the loop's elapsed age (or 0 = unlimited).
        raising_budget = "max_runtime_secs" in patch
        # A TERMINAL subject outranks every bound, and an OWED terminal turn counts
        # as one -- the same precedence the expiry notice states, read from the same
        # two fields, so the agent-facing and user-facing endings cannot disagree.
        #
        # A channel-bound loop does not settle on observation: the probe records the
        # owed final turn in ``monitor.terminal_pending`` and leaves the loop active
        # with no ``outcome``. If that turn is refused (a busy thread, the ordinary
        # case) and the retry finds a bound spent, the loop deactivates tagged with
        # that bound before the settlement that would promote the debt ever runs.
        # Reading ``stopped_reason`` alone then contradicts a fact already durably on
        # disk, and here it does more than mis-word a notice: a patch that also
        # raises the bound REVIVES the loop, re-arming a watch on a subject that has
        # already merged -- the wasted fresh loop this branch exists to prevent.
        #
        # Expressed ONCE, as a term in the revival decision itself, rather than as a
        # guard per branch: a per-branch guard loses this precedence as soon as a
        # new bound is added ahead of it. A turn that already LANDED keeps the same
        # fact in ``terminal_delivered`` until its settlement runs, so both count.
        monitor = getattr(loop, "monitor", None)
        owed = (
            str(
                getattr(monitor, "terminal_pending", "")
                or getattr(monitor, "terminal_delivered", "")
                or ""
            )
            if monitor
            else ""
        )
        terminal = reason == MONITOR_TERMINAL_REASON or bool(owed)
        # A settled outcome wins; the debt is the fallback that keeps the
        # merged-vs-closed distinction available before the settlement lands. Both
        # speak the same vocabulary (``success``/``blocked``, matching
        # ``MonitorOutcome``), so one reading covers either source.
        settled = getattr(monitor, "outcome", None) if monitor else None
        decided = str(getattr(settled, "value", settled) or owed or "")
        revivable = not terminal and (
            (stopped_at_cap and raising_cap) or (stopped_at_budget and raising_budget)
        )
        if revivable:
            patch["active"] = True
            revived = True
        else:
            # Name the bound that actually stopped the loop, so the remedy in
            # the message is the one that will work.
            if terminal:
                if decided == "success":
                    bound = (
                        "its subject already merged, so the watch is over and there is "
                        "nothing left to observe; raising a bound buys cycles with no "
                        "work in them, so arm monitor_start again only for a NEW subject"
                    )
                else:
                    bound = (
                        "its subject was closed without merging, so re-arming would only "
                        "re-observe that; the open question is whether to reopen the "
                        "subject or abandon the goal, and neither is a bound you can raise"
                    )
            elif stopped_at_budget:
                bound = (
                    f"its {int(getattr(loop, 'max_runtime_secs', 0) or 0)}s runtime "
                    "budget (awake time while the gateway runs; a restart counts "
                    "the time since created_ts on the wall clock) ran out; "
                    "raise max_runtime_secs above the loop's age "
                    "(or pass 0)"
                )
            elif stopped_at_cap:
                bound = "it hit its cycle cap; raise max_cycles above the cap (or pass 0)"
            elif reason == APPROVAL_STALL_REASON:
                # No revival affordance on purpose: raising a bound does not
                # restore an authorization, so this stays in the deny path — but
                # with the remedy that actually works, since the generic
                # "paused manually" wording would send the user to ask a human
                # who already answered by letting the grant lapse.
                bound = (
                    "a tool it needed went unanswered at the approval prompt; "
                    "re-enable auto-approve, then re-arm it with monitor_start"
                )
            elif reason == CONSECUTIVE_FAILURE_REASON:
                # Not a cap and not a human pause: several of its own cycles
                # reached a session and died in a row. Raising a bound does not
                # fix the fault, so this stays in the deny path with the remedy
                # that works — look at the error, fix the cause, re-arm; a cycle
                # that completes clears the streak.
                bound = (
                    "several of its cycles reached a model session and then died "
                    "in a row (a backend error, a persistent tool failure or a "
                    "timeout); look at the session for the error, fix the cause, "
                    "then re-arm it with monitor_start"
                )
            elif reason == STOP_SENTINEL_REASON:
                # Finished, not paused: the stop file is how the agent reports the
                # goal reached, and the service refuses to revive such a loop
                # whatever the patch. Said here so the remedy is a NEW goal rather
                # than a bound to raise or a user to ask.
                bound = (
                    "its stop file was created, so the goal is finished and there "
                    "is nothing to resume; arm monitor_start again only for a NEW goal"
                )
            else:
                bound = "it was paused manually; ask the user, or use monitor_start"
            raise _DirectiveDenied(
                f"Monitor loop {loop.id} is PAUSED (cycle {cycle_count}"
                + (f" of {current_cap}" if current_cap else ", no cap")
                + f"). monitor_update will not resume it as a side effect: {bound}."
            )
    # CREW/MEMBER GATE for the legacy loop, the twin of the one
    # ``authorize_and_update_monitor`` applies to a structured monitor. The
    # legacy chokepoint ``authorize_and_update_nudge`` holds an opaque loop id
    # and no session identity (its REST caller is user-token gated), so the mode
    # rule has to be applied HERE, where the provenance lives: ``message`` is
    # the instruction every future wake executes, and before this PR a
    # crew/member slot could hold no loop at all, so a revision of one is a
    # NEW surface. Only the session's own turn (``self_arm_ok``) may revise it;
    # a cron injection, an app-driven turn or a sub-agent sharing the slot is
    # refused with the same reason the arm path gives. Same predicate as the
    # arm path (``is_self_arm``) so the two never drift apart. The mode is read
    # off the live slot; a binding with no live slot has no mode to refuse on
    # and keeps the pre-existing behaviour (the store's own miss handling).
    if not is_channel_key(binding):
        current = (getattr(state, "_slots", None) or {}).get(binding)
        mode = str(getattr(current, "mode", ""))
        if mode in _EXTERNAL_ARM_REFUSED_MODES and not is_self_arm(
            binding, binding if self_arm_ok else ""
        ):
            raise _DirectiveDenied(f"Failed to update monitor loop: {external_arm_refusal(mode)}")
    _new_loop, error, _status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop.id,
        message=patch.get("message"),
        idle_secs=patch.get("idle_secs"),
        max_cycles=patch.get("max_cycles"),
        active=patch.get("active"),
        max_runtime_secs=patch.get("max_runtime_secs"),
        # ``.get`` returns None when the key is absent, which the authorizer reads
        # as "leave unchanged", while an explicit "" reaches it as a clear -- the
        # distinction the handler preserved by keeping a blank banner in the patch.
        banner=patch.get("banner"),
        # Absent leaves the brief alone; ``{}`` clears it. Same absent-vs-explicit
        # distinction as ``banner`` above, preserved by the tool surface.
        judge=patch.get("judge"),
        # Absent leaves the loop's subject alone. There is no clear request to pass on:
        # the tool drops a blank rather than forwarding one, so anything that arrives
        # here names the watch to arm.
        watch=patch.get("watch"),
        # A message write with NO baseline SKIPS the stale check rather than failing it, so
        # hand it the token read above -- scoped to the message case, as the handler's 409 is.
        expect_fingerprint=(baseline_token if patch.get("message") is not None else None),
        source="mcp-directive",
        caller="session-directive",
    )
    if error is not None:
        # The authorizer already audited its own refusal; agree with it.
        raise _DirectiveDenied(f"Failed to update monitor loop: {error}")
    fields = ", ".join(sorted(k for k in patch if k != "active"))
    return f"Monitor loop {loop.id} updated on this session ({fields})." + (
        " The stopped loop has been re-armed." if revived else ""
    )


def _no_loop_message(svc: Any, binding: str) -> str:
    """The result for ``autonudge_stop`` when this session resolves no loop.

    ``get_by_slot`` resolves only the loop bound to the CALLING session's
    binding key, so its miss covers two states that a caller cannot otherwise
    tell apart: no loop exists anywhere (an idempotent success — the goal
    already holds), or a loop is running under a different slot key and is
    simply unreachable from here (nothing was stopped). Counting the service's
    active loops separates them.

    Reports a COUNT and never a loop id or slot key. The stop tool exposes no
    loop-id parameter precisely so a session cannot target another session's
    loop; naming other sessions' loops here would hand the model the
    identifiers that schema withholds. Cross-session enumeration stays on the
    token-authed dashboard API. A count is all this branch needs, because the
    caller's question is whether ITS OWN stop took effect. The keys themselves
    go to the log instead, which no model reads.
    """
    active = [lp for lp in svc.list_all() if getattr(lp, "active", True)]
    if not active:
        return "No active auto-nudge loop on this session — nothing to stop."
    # SERVER-SIDE ONLY, and the reason this branch logs at all: a miss has two
    # candidate causes — a slot-key spelling the binding lookup does not model,
    # or an arming path that registered a key this session later resolves
    # differently — and they are distinguishable only from the caller's binding
    # next to the keys the store actually holds. A slot key can carry a channel
    # or user identifier, so the pair stays out of the return value and out of
    # every user-facing string.
    logger.warning(
        "AutoNudge: stop resolved no loop for binding %r; active loop slot keys: %s",
        binding,
        ", ".join(sorted(repr(getattr(lp, "slot_key", "")) for lp in active)),
    )
    return (
        "NOTHING WAS STOPPED. No auto-nudge loop is bound to this session "
        f"(binding: {binding}), but {len(active)} auto-nudge loop(s) are running on "
        "other sessions. A loop can only be stopped from the session it is bound "
        "to, so this call could not reach them."
    )


async def _structured_monitor_update(
    state: Any,
    svc: Any,
    loop: Any,
    patch: dict[str, Any],
    *,
    initiator: str = "",
    producer_is_channel: bool = False,
) -> str:
    from kiro_crew.autonudge_authz import authorize_and_update_monitor
    from kiro_crew.dashboard.handlers.source_providers import ensure_gitlab_hosts_loaded
    from kiro_crew.monitoring.models import MonitorCreationSurface
    from kiro_crew.monitoring.targets import normalize_pull_request_target

    # ``banner`` is a message-loop-only field (a structured monitor shows its
    # objective as the transcript row), so it belongs with the legacy fields the
    # structured path refuses. Without it here, ``monitor_update`` would accept a
    # banner into the patch, drop it, and report success -- a silent no-op.
    #
    # ``judge`` is the same class and reaches this path the same way: the schema
    # offers it on EVERY ``monitor_update``, so arming a structured monitor and then
    # sending a brief is two ordinary steps. A structured monitor is probe-first and
    # holds no brief, so the field has nowhere to go here -- and an owner who is told
    # their criterion was armed, while every tick keeps firing on the typed probe
    # alone, has no way to discover that from the acknowledgement.
    legacy_only = sorted(set(patch) & {"message", "max_cycles", "active", "banner", "judge"})
    if legacy_only:
        raise _DirectiveDenied(
            "monitor_update cannot apply legacy fields to a structured monitor: "
            + ", ".join(legacy_only)
        )
    monitor_state = loop.monitor
    if monitor_state is None:
        raise _DirectiveDenied("No structured monitor on this session to update.")
    structured: dict[str, Any] = {}
    if "target" in patch:
        try:
            gitlab_hosts = await ensure_gitlab_hosts_loaded()
            target = normalize_pull_request_target(
                monitor_state.kind,
                str(patch["target"]),
                gitlab_hosts=tuple(gitlab_hosts),
            )
            structured["target"] = target
            if producer_is_channel and target != monitor_state.target:
                structured["creation_surface"] = MonitorCreationSurface.CHANNEL
        except ValueError as exc:
            raise _DirectiveDenied(str(exc)) from exc
    if "objective" in patch:
        structured["objective"] = str(patch["objective"])
    if "idle_secs" in patch:
        structured["cadence_secs"] = int(patch["idle_secs"])
    if "wake_instructions" in patch:
        structured["wake_instructions"] = str(patch["wake_instructions"])
    budget_fields = {
        "max_runtime_secs",
        "max_agent_turns",
        "max_tokens",
        "max_provider_errors",
    }
    if budget_fields & set(patch):
        values = {field: int(patch[field]) for field in budget_fields if field in patch}
        # ``max_agent_turns`` alone admits 0, its unlimited sentinel; the other
        # three budgets are what keep an unlimited wake count affordable, so a
        # zero there would leave the watch with no cost ceiling.
        unbounded = {"max_agent_turns"}
        if any(value < (0 if field in unbounded else 1) for field, value in values.items()):
            raise _DirectiveDenied(
                "structured monitor budgets must be positive, "
                "except max_agent_turns where 0 means unlimited"
            )
        structured["budget_patch"] = values
    updated, error, _status = await authorize_and_update_monitor(
        svc=svc,
        state=state,
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch=structured,
        source="mcp-directive",
        caller="session-directive",
        # The SESSION'S OWN binding, handed down by _monitor_update -- never the
        # loop's key echoed back, which would make the self-arm test trivially
        # true at this site. The authorizer compares it against the loop's slot.
        initiator_slot_key=initiator,
    )
    if error is not None:
        raise _DirectiveDenied(f"Failed to update structured monitor: {error}")
    if updated is None:
        raise _DirectiveDenied(
            "Failed to update structured monitor: no monitor record was returned."
        )
    return f"Structured monitor {updated.id} updated on this session."


def _structured_stop_reason(args: dict[str, Any]) -> str:
    from kiro_crew.monitoring.models import MAX_MONITOR_STOP_REASON_CHARS
    from kiro_crew.security import redact_and_truncate

    return redact_and_truncate(
        str(args.get("reason") or "").strip(),
        max_chars=MAX_MONITOR_STOP_REASON_CHARS,
    )


async def _stop_resolved_loop(
    slot: Any,
    svc: Any,
    binding: str,
    loop: Any,
    args: dict[str, Any],
    *,
    wake_loop_id: str = "",
) -> str:
    """Stop the loop bound to this session, whatever shape it holds.

    The single implementation shared by both stop entry points, so the two can
    never route the same loop differently. Both ``autonudge_stop`` and
    ``monitor_stop`` resolve the general binding, fetch the loop, and hand it
    here; the shape test and the routing live in one place.

    Routing is asymmetric because the data model is. A structured monitor goes
    through ``authorize_and_stop_monitor``, which RETAINS a terminal record for
    later inspection. A legacy loop has no such record: a research-owned slot is
    deactivated with a tombstone reason a Research Lab consumer reads, and every
    other legacy loop is REMOVED, leaving no row behind. So a stop of a legacy
    loop cannot be inspected afterward -- ``monitor_inspect`` reports it as not
    armed; only the WARNING stop line in ``gateway.log`` says why it went (the
    agent's own explanation rides along on the removal path only). Callers that
    need a retained terminal record must be watching a structured monitor.
    """
    from kiro_crew.autonudge import is_structured_monitor_loop

    loop_id = loop.id
    reason = _structured_stop_reason(args)
    structured = is_structured_monitor_loop(loop)
    if structured:
        from kiro_crew.autonudge_authz import authorize_and_stop_monitor

        _loop, error, _status = await authorize_and_stop_monitor(
            svc=svc,
            loop_id=loop_id,
            session_key=loop.slot_key,
            source="mcp-directive",
            caller="session-directive",
            user_reason=reason,
        )
        if error is not None:
            raise _DirectiveDenied(f"Failed to stop structured monitor: {error}")
        return (
            f"Structured monitor {loop_id} stopped and retained for inspection"
            + (f" (reason: {reason})" if reason else "")
            + ". No further monitor wakes will fire."
        )
    # Research Lab consumes a persisted stop record to distinguish deliberate
    # completion from unreachable-session cleanup. The canonical name is not
    # ownership evidence: users may give an ordinary dashboard slot the same
    # shape, while the slot's persisted app provenance cannot be user-selected.
    # Ordinary dashboard/channel monitors have no tombstone consumer, so retain
    # their historical removal behavior instead of leaving a paused loop. The
    # SESSION'S binding names the slot, not the loop's own slot_key: they are the
    # same for a bound loop, and the binding is the identity the ownership check
    # reads.
    # Both legacy writes take the guard: the tombstone is read by Research Lab
    # as a deliberate finish, so stamping it over a person's pause would settle
    # the campaign exactly as deleting the row would lose it.
    # Only a RECORDED outcome decides. A refusal refuses. A row the service found
    # missing under the lock is success only if nothing replaced it (the guard's
    # ``on_absent`` checks the slot in that same hold); a replacement refuses,
    # because it is still running. A write that reached neither never took the
    # lock (maintenance quiesce) and may not be reported as a stop.
    guard = _StaleWakeStopGuard(svc, binding, wake_loop_id) if wake_loop_id else None
    # A guard-less stop (a person's own request) has no identity to re-check, but
    # it still may not report a write that never happened, nor a stop of a loop
    # a concurrent arm replaced: it too learns under the lock what the slot holds.
    absent_outcome: list[str] = []

    def _mark_absent() -> None:
        current = svc.get_by_slot(binding)
        replaced = current is not None and getattr(current, "id", "") != loop_id
        absent_outcome.append("replaced" if replaced else "gone")

    on_absent = guard.on_absent if guard is not None else _mark_absent
    if is_owned_research_slot(binding, str(getattr(slot, "_app", "") or "")):
        update_kwargs: dict[str, Any] = {
            "active": False,
            "stopped_reason": AUTONUDGE_STOP_REASON,
            "on_absent": on_absent,
        }
        if guard is not None:
            update_kwargs["precondition"] = guard
        applied = await svc.update(loop_id, **update_kwargs) is not None
    else:
        # The removal leaves no row, so the agent's own reason travels in the WARNING
        # stop line instead (autonudge_stop_log); without it a self-stop is "removed".
        remove_kwargs: dict[str, Any] = {
            "stop_reason": AUTONUDGE_STOP_REASON,
            "stop_detail": reason,
            "on_absent": on_absent,
        }
        if guard is not None:
            remove_kwargs["precondition"] = guard
        applied = bool(await svc.remove(loop_id, **remove_kwargs))
    if guard is not None:
        guard.raise_unless_stopped(applied)
    elif not applied and absent_outcome != ["gone"]:
        if absent_outcome == ["replaced"]:
            raise _DirectiveDenied(
                "Monitor NOT stopped: this session's monitor was replaced by a new "
                "loop while the stop was waiting, and the new loop is still running. "
                "Stop it again to end the new loop."
            )
        raise _DirectiveDenied(_STOP_NOT_APPLIED)
    return (
        f"Auto-nudge loop {loop_id} stopped on this session"
        + (f" (reason: {reason})" if reason else "")
        + ". No further nudges will fire."
    )


async def _monitor_stop(
    slot: Any,
    session_key: str,
    args: dict[str, Any],
    *,
    wake_loop_id: str = "",
) -> str:
    from kiro_crew.autonudge import get_instance

    svc = get_instance()
    if svc is None:
        raise _DirectiveDenied("Monitor was not stopped: auto-nudge is disabled on this host.")
    # The GENERAL binding, so a legacy loop resolves here too. A stop that
    # answered only for a structured monitor is a no-op on the loop shape most
    # sessions actually run, and a session that armed a timer loop and called
    # monitor_stop would believe it ended while it kept firing.
    binding = _binding(session_key)
    if not binding:
        raise _DirectiveDenied("monitor_stop is not supported from this session type.")
    loop = svc.get_by_slot(binding)
    if not loop:
        return _no_loop_message(svc, binding)
    return await _stop_resolved_loop(
        slot,
        svc,
        binding,
        loop,
        args,
        wake_loop_id=wake_loop_id,
    )


async def _autonudge_stop(
    slot: Any,
    session_key: str,
    args: dict[str, Any],
    *,
    wake_loop_id: str = "",
) -> str:
    from kiro_crew.autonudge import get_instance

    svc = get_instance()
    # "Nothing to stop" is an IDEMPOTENT success — the goal (no loop running on
    # this session) already holds — so the disabled-service and no-loop paths
    # keep returning; a binding miss that is NOT that state is separated in
    # ``_no_loop_message``. The unsupported-session path is a refusal like its
    # siblings: the caller asked for an effect this session can never carry.
    if svc is None:
        return "No auto-nudge loop to stop (auto-nudge is disabled on this host)."
    binding = _binding(session_key)
    if not binding:
        raise _DirectiveDenied("autonudge_stop is not supported from this session type.")
    loop = svc.get_by_slot(binding)
    if not loop:
        return _no_loop_message(svc, binding)
    return await _stop_resolved_loop(
        slot,
        svc,
        binding,
        loop,
        args,
        wake_loop_id=wake_loop_id,
    )
