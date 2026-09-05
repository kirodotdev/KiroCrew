"""Apply a decoded session directive against the consumer's OWN session.

Called from ``dashboard/chat_runner.py``'s ``EVENT_TOOL_RESULT`` handler — the
shared turn loop for every dashboard-driven surface (dashboard, Slack mirror,
taskrunner, …) — and from ``messaging/driver.py``'s ``TurnDriver`` directive
consumer, which covers the standalone channel transports (Telegram, Discord,
standalone Slack, iMessage, Teams, Webex, WeCom, Weixin). The caller supplies
the AUTHORITATIVE ``session_key`` for the turn, so a stateless tool's directive
is applied to the exact session that produced it. Effects run IN-PROCESS via
the same cores the HTTP endpoints call (no loopback HTTP, no user-token dance):
the consumer is the authoritative session, so cross-session misattribution is
unrepresentable.

``slot`` is the dashboard chat slot when the caller has one (chat_runner) and
``None`` for a channel turn (TurnDriver). A missing slot NEVER weakens a
boundary: the dashboard-only directives are refused outright for a slot-less
caller (they act on a slot, so there is nothing to apply them to);
``set_project`` — user-surface-gated rather than dashboard-only, though its
effect targets the slot — is likewise refused when the turn
holds no slot; and the monitor trio only reads ``slot`` through fail-safe
``getattr``.

Every branch returns a human-readable confirmation string and NEVER raises into
the runner. NOTE: gateway-off (the default), the MODEL already received the
tool's OWN return over the MCP pipe; this string is recorded on KiroCrew's
transcript / WS / hook surfaces, it does NOT replace the model's tool result.
That is why the tool bodies phrase their own message to not over-claim an effect
this consumer applies (and may refuse) after the fact.

IMPORTS ARE DELIBERATELY FUNCTION-LOCAL here, except for the shared session and
Research ownership contracts plus the immutable ``AUTONUDGE_STOP_REASON``
constant. ``sel`` is a genuine cycle
(``sel`` -> config -> apps -> dashboard, and chat_runner imports this module
before it imports sel). The rest (autonudge, autonudge_authz, chat_utils,
security, chat_handlers, chat_persistence, chat_tags, chat_tag_grants) are
deferred on purpose: they keep this module cheap to
import from the turn loop's import graph, and they resolve the symbol at CALL
time so patching the SOURCE module is what tests (and any runtime override)
actually observe — a module-scope ``from X import name`` would freeze a stale
binding and silently bypass it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from kiro_crew.apps.builtins.auto_research.session_keys import (
    is_owned_research_slot,
)
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    AUTONUDGE_STOP_REASON,
    MONITOR_TERMINAL_REASON,
)
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.session_surface import has_dashboard_surface

logger = logging.getLogger(__name__)

# Card directives require a connected dashboard surface. ``set_project`` is
# admitted by the user-surface provenance gate below, then separately requires
# the current turn to own the slot it would mutate.
_DASHBOARD_ONLY_DIRECTIVES = frozenset({"suggest_followup", "ask_question"})
_USER_SURFACE_DIRECTIVES = frozenset({"set_project", "reset_conversation", "chat_tag"})


def _has_user_surface(session_key: str) -> bool:
    """Return whether *session_key* names a user-facing conversation."""
    return has_dashboard_surface(session_key) or is_channel_session_key(session_key)


class _DirectiveDenied(Exception):
    """Raised by an applier when the directive is REFUSED — a permission
    decision (e.g. a sensitive-path block), an unsupported session type, or an
    authorizer refusal. Audited as ``outcome="denied"`` by the wrapper. The
    distinction from a plain returned string matters for the SEL chain: every
    path where the effect was NOT applied must never audit ``success``."""


def _audit(session_key: str, kind: str, outcome: str) -> None:
    """Emit a SEL tool-invocation event for one directive application.

    AUTOSDE ``backend-security-controls`` requires every tool invocation AND
    permission decision to emit a SEL event — the effect now runs here (not in
    the tool body or an HTTP endpoint), so the audit must too. Best-effort: a
    telemetry failure must never break the turn.
    """
    try:
        # Local import: kiro_crew.sel transitively pulls config -> apps ->
        # dashboard, which cycles with this dashboard-side module at import time
        # (chat_runner imports this module before it imports sel).
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=session_key, source="mcp-directive", tool_name=kind, outcome=outcome
        )
    except Exception:
        logger.debug("session-directive SEL audit failed", exc_info=True)


async def apply_session_directive(
    state: Any,
    slot: Any,
    session_key: str,
    kind: str,
    args: dict[str, Any],
    *,
    producer_is_user_facing: bool = False,
) -> str:
    """Apply directive *kind* with *args* to *slot*/*session_key*; return a
    confirmation string for the model. Fail-soft: any error is returned as a
    readable message, never raised. Every path emits a SEL audit event.
    ``slot`` is ``None`` for a channel (TurnDriver) caller — see the module
    docstring."""
    if kind in _DASHBOARD_ONLY_DIRECTIVES and (
        slot is None or not has_dashboard_surface(session_key)
    ):
        # These two act on a dashboard chat SLOT (its follow-up card, its
        # question card), so the boundary is whether an open tab exists to
        # receive the effect — not where the conversation started. A
        # channel-born session displayed in a tab qualifies; a cron, sub-agent
        # or otherwise tabless caller does not, and must not address a card
        # nothing will render. A slot-less caller (a channel transport's
        # TurnDriver) is refused for the same reason even when a tab happens to
        # be open: the effect targets the SLOT, and this turn does not hold
        # one. The consumer is the only layer that knows the authoritative
        # session, so the check belongs HERE.
        _audit(session_key, kind, "denied")
        return (
            f"Error: {kind} only works from a dashboard chat session "
            f"(this turn is {session_key!r}). Nothing was changed."
        )
    if kind in _USER_SURFACE_DIRECTIVES and slot is None:
        # set_project mutates the SLOT (its project and session CWD). A
        # slot-less caller — a channel transport's TurnDriver — holds no slot
        # for the effect to land on, so refuse it as a decision here: letting
        # it fall through would crash `_set_project` on the missing slot and
        # the fail-soft wrapper would audit "error" for what is a permission
        # boundary. Slot-bearing callers continue to the provenance and
        # user-surface gate below.
        _audit(session_key, kind, "denied")
        return (
            f"Error: {kind} targets this turn's chat slot, and this turn "
            f"holds none (this turn is {session_key!r}). Nothing was changed."
        )
    if kind in _USER_SURFACE_DIRECTIVES and (
        not producer_is_user_facing or not _has_user_surface(session_key)
    ):
        # A cron turn can run on a user's slot and a sub-agent can share its
        # parent's slot. Positive admission prevents either from silently
        # retargeting the user's project/CWD.
        _audit(session_key, kind, "denied")
        return (
            f"Error: {kind} only works from a user-facing session (dashboard "
            f"or a messaging channel); headless callers such as cron jobs and "
            f"sub-agents are refused (this turn is {session_key!r}). "
            "Nothing was changed."
        )
    try:
        if kind == "monitor_start":
            result = await _monitor_start(state, session_key, args)
        elif kind == "monitor_watch":
            result = await _monitor_watch(state, session_key, args)
        elif kind == "monitor_update":
            result = await _monitor_update(state, session_key, args)
        elif kind == "monitor_stop":
            result = await _monitor_stop(session_key, args)
        elif kind == "autonudge_stop":
            result = await _autonudge_stop(slot, session_key, args)
        elif kind == "set_project":
            result = await _set_project(state, slot, args)
        elif kind == "reset_conversation":
            result = await _reset_conversation(slot, session_key, args)
        elif kind == "chat_tag":
            result = await _apply_chat_tag(state, slot, session_key, args)
        elif kind == "suggest_followup":
            result = await _suggest_followup(state, slot, args)
        elif kind == "ask_question":
            result = await _ask_question(state, slot, args)
        else:
            _audit(session_key, kind, "error")
            return f"Error: unknown session directive {kind!r}."
    except _DirectiveDenied as exc:
        _audit(session_key, kind, "denied")
        logger.warning(
            "session-directive DENIED at apply for session_key=%r kind=%r: %s",
            session_key,
            kind,
            exc,
        )
        return str(exc)
    except Exception as exc:  # never propagate into the turn loop
        logger.warning("apply_session_directive(%s) failed", kind, exc_info=True)
        _audit(session_key, kind, "error")
        return f"Error applying {kind}: {exc}"
    # Some appliers RETURN a readable failure instead of raising (an invalid
    # project dir, an absent loop, no attached client), so a blanket "success"
    # would falsely mark those in the SEL chain. Derive the outcome from the
    # result the same way call_tool_with_logging does (an "Error:" prefix ==
    # failed), keeping the audit truthful for the failure paths too.
    _audit(session_key, kind, "error" if result.startswith("Error:") else "success")
    return result


# ── autonudge trio ──────────────────────────────────────────────────────────


def _binding(session_key: str) -> str | None:
    from kiro_crew.autonudge import binding_key_for

    return binding_key_for(session_key)


def _structured_binding(session_key: str) -> str | None:
    from kiro_crew.autonudge import structured_monitor_binding_key_for

    return structured_monitor_binding_key_for(session_key)


async def _monitor_start(state: Any, session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge

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
    loop, error, _status = await authorize_and_add_nudge(
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
        source="mcp-directive",
        caller="session-directive",
        gate=gate,
        replace_existing=False,
        # The directive re-arm is the one path allowed to displace a retained
        # STOPPED row: monitor_update's approval-stall refusal names
        # monitor_start as the remedy, so refusing here deadlocks it.
        replace_stopped=True,
    )
    if error is not None:
        # The authorizer already audited its own refusal; the wrapper's record
        # for THIS directive must agree (denied), not overwrite it as success.
        raise _DirectiveDenied(f"Failed to start monitor loop: {error}")
    cap = f", stopping after {max_cycles} cycles" if max_cycles else ", with NO cycle cap"
    if max_runtime_secs:
        cap += f", wall-clock budget {max_runtime_secs}s"
    # Read the cadence off the ARMED loop, not off the request. This surface knows
    # something the MCP tool's own ack has to infer: whether a monitor was actually
    # attached. Reporting "re-injects every {idle_secs}s" for a gated loop is untrue --
    # a quiet tick spends no turn at all -- and this applier defaults ``gate`` to True
    # a few lines above, so the unconditional promise was wrong for its own default.
    armed_monitor = getattr(loop, "monitor", None)
    if armed_monitor is not None and getattr(loop, "gate", False):
        cadence = (
            f"observing {armed_monitor.target} every {idle_secs}s and re-injecting the "
            "message only when it changes, so quiet cycles cost no turn"
        )
    else:
        cadence = f"the message re-injects every {idle_secs}s"
    return (
        f"Monitor loop {getattr(loop, 'id', '?')} started on this session: {cadence} "
        f"(user messages defer a due fire "
        f"to their turn's end without restarting the countdown){cap}. "
        "End your turn now — the loop wakes you. Call autonudge_stop when the "
        "exit condition is met."
    )


async def _monitor_watch(state: Any, session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_authz import authorize_and_add_nudge
    from kiro_crew.monitoring.models import MonitorBudgets, MonitorState

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
    loop, error, _status = await authorize_and_add_nudge(
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
    )
    if error is not None:
        raise _DirectiveDenied(f"Failed to start structured monitor: {error}")
    if loop is None:
        raise _DirectiveDenied(
            "Failed to start structured monitor: no monitor record was returned."
        )
    return f"Structured monitor {loop.id} started on this session."


async def _monitor_update(state: Any, session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance, is_structured_monitor_loop
    from kiro_crew.autonudge_authz import authorize_and_update_nudge

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
    patch = dict(args.get("patch") or {})
    if is_structured_monitor_loop(loop):
        if _structured_binding(session_key) != binding:
            raise _DirectiveDenied("monitor_update is not supported from this session type.")
        return await _structured_monitor_update(state, svc, loop, patch)
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
    # Spent-budget guard, same shape as the cycle-cap one: a wall-clock budget
    # at/below the loop's elapsed age deactivates it on the next timer without
    # another fire — refuse rather than promise a wake that never comes.
    if "max_runtime_secs" in patch:
        new_budget = int(patch["max_runtime_secs"] or 0)
        created_ts = float(getattr(loop, "created_ts", 0.0) or 0.0)
        elapsed = int(time.time() - created_ts) if created_ts else 0
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
    # cannot distinguish a pause from an expiry (GPT review on #2116: a
    # budget raise must never resume a loop the user paused).
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
        # guard per branch: the notice next door lost this same precedence three
        # times because each new bound was added ahead of it.
        monitor = getattr(loop, "monitor", None)
        owed = str(getattr(monitor, "terminal_pending", "") or "") if monitor else ""
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
                    f"its {int(getattr(loop, 'max_runtime_secs', 0) or 0)}s wall-clock "
                    "budget ran out; raise max_runtime_secs above the loop's age "
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
            else:
                bound = "it was paused manually; ask the user, or use monitor_start"
            raise _DirectiveDenied(
                f"Monitor loop {loop.id} is PAUSED (cycle {cycle_count}"
                + (f" of {current_cap}" if current_cap else ", no cap")
                + f"). monitor_update will not resume it as a side effect: {bound}."
            )
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


async def _structured_monitor_update(state: Any, svc: Any, loop: Any, patch: dict[str, Any]) -> str:
    from kiro_crew.autonudge_authz import authorize_and_update_monitor

    # ``banner`` is a message-loop-only field (a structured monitor shows its
    # objective as the transcript row), so it belongs with the legacy fields the
    # structured path refuses. Without it here, ``monitor_update`` accepted a
    # banner into the patch, dropped it, and reported success -- a silent no-op.
    legacy_only = sorted(set(patch) & {"message", "max_cycles", "active", "banner"})
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
        structured["target"] = str(patch["target"])
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
        if any(value <= 0 for value in values.values()):
            raise _DirectiveDenied("structured monitor budgets must be positive")
        structured["budget_patch"] = values
    updated, error, _status = await authorize_and_update_monitor(
        svc=svc,
        state=state,
        loop_id=loop.id,
        session_key=loop.slot_key,
        patch=structured,
        source="mcp-directive",
        caller="session-directive",
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


async def _monitor_stop(session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance, is_structured_monitor_loop
    from kiro_crew.autonudge_authz import authorize_and_stop_monitor

    svc = get_instance()
    if svc is None:
        raise _DirectiveDenied("Monitor was not stopped: auto-nudge is disabled on this host.")
    binding = _structured_binding(session_key)
    if not binding:
        raise _DirectiveDenied("monitor_stop is not supported from this session type.")
    loop = svc.get_by_slot(binding)
    if loop is None or not is_structured_monitor_loop(loop):
        return "No structured monitor to stop on this session."
    stopped, error, _status = await authorize_and_stop_monitor(
        svc=svc,
        loop_id=loop.id,
        session_key=loop.slot_key,
        source="mcp-directive",
        caller="session-directive",
        user_reason=_structured_stop_reason(args),
    )
    if error is not None:
        raise _DirectiveDenied(f"Failed to stop structured monitor: {error}")
    if stopped is None:
        raise _DirectiveDenied("Failed to stop structured monitor: no monitor record was returned.")
    return f"Structured monitor {stopped.id} stopped and retained for inspection."


async def _autonudge_stop(slot: Any, session_key: str, args: dict[str, Any]) -> str:
    from kiro_crew.autonudge import get_instance, is_structured_monitor_loop

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
    loop_id = loop.id
    reason = _structured_stop_reason(args)
    # Research Lab consumes a persisted stop record to distinguish deliberate
    # completion from unreachable-session cleanup. The canonical name is not
    # ownership evidence: users may give an ordinary dashboard slot the same
    # shape, while the slot's persisted app provenance cannot be user-selected.
    # Ordinary dashboard/channel monitors have no tombstone consumer, so retain
    # their historical removal behavior instead of leaving a paused loop.
    if is_structured_monitor_loop(loop):
        from kiro_crew.autonudge_authz import authorize_and_stop_monitor

        _loop, error, _status = await authorize_and_stop_monitor(
            svc=svc,
            loop_id=loop_id,
            session_key=loop.slot_key,
            source="mcp-directive",
            caller="autonudge-stop-compat",
            user_reason=_structured_stop_reason(args),
        )
        if error is not None:
            raise _DirectiveDenied(f"Failed to stop structured monitor: {error}")
    elif is_owned_research_slot(binding, str(getattr(slot, "_app", "") or "")):
        await svc.update(loop_id, active=False, stopped_reason=AUTONUDGE_STOP_REASON)
    else:
        await svc.remove(loop_id)
    return (
        f"Auto-nudge loop {loop_id} stopped on this session"
        + (f" (reason: {reason})" if reason else "")
        + ". No further nudges will fire."
    )


# ── slot-targeted effects (the dashboard-only pair + set_project) ────────────


async def _set_project(state: Any, slot: Any, args: dict[str, Any]) -> str:
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.sandbox import voice_runtime_workspace_conflict
    from kiro_crew.security import is_sensitive_path

    clear = bool(args.get("clear"))
    project = str(args.get("project") or "").strip()
    old_project = getattr(slot, "project", "") or ""
    if clear or not project:
        slot.project = ""
        if old_project:
            slot._pending_reset_history_key = effective_session_key(slot)
        _push(state)
        return "Project cleared. The next message cold-starts with no project scope."
    expanded = os.path.expanduser(project)

    def _validate() -> tuple[str, bool, bool]:
        """Resolve + classify the path on a worker thread.

        `realpath`/`isdir` touch the filesystem, so a network-mounted project
        path would stall chat, heartbeat and liveness if resolved on the event
        loop (no-blocking-call-on-event-loop). Returns
        (realpath, sensitive, is_dir); the sensitive check runs on BOTH the
        pre-resolution and resolved forms — the pre-check keeps a sensitive
        path from being probed at all, the post-check catches symlink/".."
        evasion.
        """
        if is_sensitive_path(expanded):
            return "", True, False
        rp_ = os.path.realpath(expanded)
        if is_sensitive_path(rp_):
            return rp_, True, False
        return rp_, False, os.path.isdir(rp_)

    rp, sensitive, is_dir = await asyncio.to_thread(_validate)
    if sensitive:
        # Permission decision — raise so the wrapper audits it as denied.
        raise _DirectiveDenied("Error: access denied (sensitive path).")
    if not is_dir:
        return f"Error: not a directory: {rp}"
    # #7392 pre-flight, mirrored from the HTTP project endpoint: this directive
    # is the OTHER user/agent-driven moment of choice that sets slot.project
    # (set_project MCP routes here in-process, never through the endpoint), so
    # without this check the overlap refusal would still land at spawn time,
    # after the bad folder was committed. Same helper, same message; off the
    # loop because it stats the runtime paths.
    overlap = await asyncio.to_thread(voice_runtime_workspace_conflict, rp)
    if overlap is not None:
        return f"Error: {overlap}"
    slot.project = rp
    if rp != old_project:
        slot._pending_reset_history_key = effective_session_key(slot)
        try:
            from kiro_crew.dashboard.chat_handlers import _save_recent_project

            # Offload the recent-projects file IO (mkdir + read + atomic write)
            # off the event loop — the HTTP endpoint this replaced did the same.
            await asyncio.to_thread(_save_recent_project, rp)
        except Exception:
            logger.debug("save recent project failed", exc_info=True)
    _push(state)
    return (
        f"Project set to {rp}. The session cold-starts with the new CWD and "
        "project-level .kiro/steering on the next message."
    )


async def _reset_conversation(slot: Any, session_key: str, args: dict[str, Any]) -> str:
    """Queue a conversation discard for this slot's next turn boundary.

    Deferred rather than applied here because the caller is mid-turn: a discard
    is a full provider teardown, and the immediate route
    (``POST /api/chat/slots/{slot}/reset-conversation``) refuses a busy slot for
    exactly that reason. Queuing is what makes the effect reachable from inside
    the turn that wants it — the flag is consumed at a later turn boundary.

    Queues the *session_key* THIS TURN runs on, captured by the caller, rather
    than re-resolving it from the slot. A slot's ``linked_session_key`` is
    mutable: a cron or workflow injection can rebind the live slot between the
    turn that asked for the reset and the consume that applies it, so a
    slot-resolved key would discard whatever conversation the slot points at by
    then and leave the one the caller meant untouched. The key is the caller's,
    not the slot's.

    Only the model's memory is dropped. The slot stays open, the session-map
    entry keeps its channel linkage, and the transcript is untouched on disk and
    in the tab: the record is the user's, the context was the conversation's.
    """
    slot._pending_discard_conversation_key = session_key
    return (
        "Conversation reset queued. It lands at a turn boundary — normally the "
        "end of this turn, later if a turn is still in flight on the session or "
        "sub-agents are running, queued, or delivering a result. The next "
        "message after it lands starts with no memory of this conversation. The "
        "transcript is untouched — earlier messages stay visible in the tab and "
        "on disk."
    )


async def _apply_chat_tag(state: Any, slot: Any, session_key: str, args: dict[str, Any]) -> str:
    """Apply a ``chat_tag`` directive to THIS turn's slot.

    Mirrors the ``PUT /api/chat/slots/{slot}/tags`` write sequence
    (chat_tags.api_chat_slot_tags): hold the tags write lock across
    resolve→validate→assign→persist, read ``slot.tags`` FRESH inside the lock
    (a concurrent folder/board edit landing mid-apply is the stale-read bug
    class), and push a slots update after persisting.

    Enforces the per-tag agent policy (chat_tags.agent_tag_policy). Named
    refusals surface as the directive's result string: ``tag_policy_denied:<id>``
    (not agent-writable for the requested op) and ``unknown_tag:<id>``. A no-op
    (the session already carries exactly the requested state/labels) is audited
    as ``no_op`` but answers with the current tag list ("No change. ...") — the
    documented READ path.
    On success the result includes the session's RESULTING tag names — this is
    also the agent's tag READ path.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_tag_grants import refresh_cache
    from kiro_crew.dashboard.chat_tags import (
        agent_tag_grant,
        agent_tag_policy,
        tags_write_lock,
        validate_folder_tag_ids,
    )

    # Identity FIRST, before any suspension point: this directive was
    # authorized against the conversation that produced it, and the grant
    # refresh below is an await — a concurrent rebind landing inside it must
    # not let the capture bind the REBOUND transcript, or the in-lock recheck
    # compares the moved key against itself and passes (GPT review finding).
    from kiro_crew.dashboard.chat_utils import slot_history_key

    authorized_history_key = slot_history_key(slot)

    # Pull the grants store read+parse off the event loop ONCE; the sync
    # resolutions below then serve from the cache (one stat syscall each —
    # the skill-trust reader's documented trade-off). GPT finding: the full
    # json read must not run on the gateway loop.
    await asyncio.to_thread(refresh_cache)

    set_state = str(args.get("set_state") or "").strip()
    add_ids = [str(t) for t in (args.get("add") or [])]
    remove_ids = [str(t) for t in (args.get("remove") or [])]

    def _sel_self_tag(outcome: str, resources: str = "") -> None:
        try:
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller="mcp-directive",
                operation="chat.self_tag",
                outcome=outcome,
                source="mcp-directive",
                resources=resources,
            )
        except Exception:
            logger.debug("chat_tag SEL api_access audit failed", exc_info=True)

    # Capture the transcript identity of the TURN's slot BEFORE any awaited
    # work: this directive was authorized against the conversation that
    # produced it, and a rebind landing while we wait on the tags lock (or
    # during the persist) must not let the mutation follow the slot to a
    # different transcript. Mirrors the locked_history_key discipline in the
    # chat_handlers metadata endpoints. ``authorized_history_key`` was
    # captured at FUNCTION ENTRY, before the grant-refresh await — capturing
    # it here would already be past that suspension point.

    async with tags_write_lock(state):
        # The slot may have been rebound while we awaited the lock: the write
        # below would target the NEW transcript while this directive's
        # authorization names the old one. Refuse rather than follow.
        if slot_history_key(slot) != authorized_history_key:
            _sel_self_tag("denied", "session_rebound")
            return "Error: session_rebound"
        # Live vocabulary, resolved INSIDE the lock. Map lowercased id AND
        # lowercased display name -> tag dict so requests resolve
        # case-insensitively by either handle (maintainer audit ask from
        # #3469's closure: a user-created status tag has a uuid id, so
        # name resolution is what keeps it reachable). Names are indexed
        # first and ids second, so an id always wins a collision — ids are
        # the authoritative handle.
        vocab_by_lower: dict[str, dict[str, Any]] = {}
        for t in state._tags:
            tname = t.get("name")
            if isinstance(tname, str) and tname.strip():
                vocab_by_lower.setdefault(tname.lower(), t)
        for t in state._tags:
            tid = t.get("id")
            if isinstance(tid, str):
                vocab_by_lower[tid.lower()] = t

        def _resolve(requested: str) -> dict[str, Any] | None:
            return vocab_by_lower.get(requested.lower())

        def _available() -> str:
            names = [str(t.get("name") or t.get("id")) for t in state._tags if isinstance(t, dict)]
            return ", ".join(n for n in names if n)

        # Validate every requested id exists BEFORE any mutation, so a bad id in
        # a multi-tag call changes nothing.
        for requested in ([set_state] if set_state else []) + add_ids + remove_ids:
            if _resolve(requested) is None:
                _sel_self_tag("denied", requested)
                return (
                    f"Error: unknown_tag:{requested}. No tag named '{requested}' "
                    f"found (case-insensitive, by id or name). "
                    f"Available: {_available()}"
                )

        # Workflow-state tags are mutually exclusive, and `set_state` is the only
        # verb carrying the peer-strip that upholds that invariant. A state id
        # smuggled through `add` would append WITHOUT stripping peers, persisting
        # two exclusive states — refuse and teach the boundary instead. Status-
        # ness here (and at every authorization decision below) is the GRANT
        # STORE's recorded bit, not the tag dict's own field: tags.json is
        # agent-writable, so a forged ``status`` must not re-route which verbs
        # apply or which peers get stripped.
        for requested in add_ids:
            if agent_tag_grant(_resolve(requested))[1]:  # type: ignore[arg-type]
                _sel_self_tag("denied", requested)
                return f"Error: status_tag_requires_set_state:{requested}"

        # Policy: `add` needs add-only or add-remove; `remove` and the implicit
        # removal inside `set_state` need add-remove.
        for requested in add_ids:
            policy = agent_tag_policy(_resolve(requested))  # type: ignore[arg-type]
            if policy not in ("add-only", "add-remove"):
                _sel_self_tag("denied", requested)
                return f"Error: tag_policy_denied:{requested}"
        for requested in remove_ids:
            policy = agent_tag_policy(_resolve(requested))  # type: ignore[arg-type]
            if policy != "add-remove":
                _sel_self_tag("denied", requested)
                return f"Error: tag_policy_denied:{requested}"
        if set_state:
            state_tag = _resolve(set_state)
            # Pre-validation above guarantees every requested id resolves;
            # narrow explicitly for the type checker.
            assert state_tag is not None
            # `set_state` is the workflow-state verb: the requested tag must BE
            # a workflow state, or the peer-strip below would strip real states
            # in exchange for a plain label. One store read answers both the
            # status question and the policy question so the two cannot be
            # satisfied by different sources.
            state_policy, state_is_status = agent_tag_grant(state_tag)
            if not state_is_status:
                _sel_self_tag("denied", set_state)
                return f"Error: not_a_status_tag:{set_state}"
            if state_policy != "add-remove":
                _sel_self_tag("denied", set_state)
                return f"Error: tag_policy_denied:{set_state}"
            state_canonical_id = str(state_tag["id"])
            # `set_state=X, remove=[X]` in one call would add X then remove it,
            # leaving the session with NO workflow state — the exact outcome
            # set_state exists to prevent. Refuse the contradictory call.
            for requested in remove_ids:
                if _resolve(requested)["id"] == state_canonical_id:  # type: ignore[index]
                    _sel_self_tag("denied", requested)
                    return f"Error: set_state_conflicts_with_remove:{state_canonical_id}"

        # FRESH read of the slot's current tags inside the lock.
        current: list[str] = list(getattr(slot, "tags", None) or [])
        new_tags: list[str] = list(current)

        def _add(canonical_id: str) -> None:
            if canonical_id not in new_tags:
                new_tags.append(canonical_id)

        def _remove(canonical_id: str) -> None:
            if canonical_id in new_tags:
                new_tags.remove(canonical_id)

        if set_state:
            state_id = _resolve(set_state)["id"]  # type: ignore[index]
            # Mutual exclusivity: strip every OTHER workflow-state tag (any tag
            # carrying status: True), keyed on the LIVE vocabulary rather than a
            # hardcoded id list, then add the requested one. A removed peer that
            # is human-only must NOT be silently stripped — refuse instead.
            for existing in list(new_tags):
                et = _resolve(existing)
                if et is None:
                    continue
                et_policy, et_is_status = agent_tag_grant(et)
                if et_is_status and et["id"] != state_id:
                    if et_policy != "add-remove":
                        _sel_self_tag("denied", et["id"])
                        return f"Error: tag_policy_denied:{et['id']}"
                    _remove(et["id"])
            _add(state_id)

        for requested in add_ids:
            _add(_resolve(requested)["id"])  # type: ignore[index]
        for requested in remove_ids:
            _remove(_resolve(requested)["id"])  # type: ignore[index]

        if new_tags == current:
            # The documented READ path: a no-op change is how a caller asks
            # for its current tags, so answer with them instead of a bare
            # error (GPT r8 finding: the doc promised this and the code
            # returned "Error: no_op" without the list). Still audited as
            # no mutation.
            _sel_self_tag("denied", "no_op")
            # String ids only: a malformed (e.g. list-valued) ``id`` loaded
            # from tags.json is unhashable and would raise here (GPT review
            # finding); such entries resolve via the ``tid`` fallback instead.
            name_by_id = {
                t.get("id"): (t.get("name") or t.get("id"))
                for t in state._tags
                if isinstance(t.get("id"), str)
            }
            names = [str(name_by_id.get(tid, tid)) for tid in current]
            shown = ", ".join(names) if names else "(none)"
            return f"No change. This session currently carries: {shown}."

        # Pin the persist to the transcript captured at TURN ENTRY (before the
        # lock wait): a slot rebind landing during the awaited save would
        # otherwise deliver this agent's tag mutation to a conversation it
        # never touched. On a refused save, roll the in-memory slot back and
        # mark it dirty so the periodic flush reconverges the durable record
        # to the (restored) live state.
        prior_tags = list(current)
        slot.tags = validate_folder_tag_ids(new_tags, state)
        applied = await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        )
        if not applied:
            slot.tags = prior_tags
            try:
                slot._dirty = True
            except Exception:
                logger.debug("chat_tag rollback dirty-mark failed", exc_info=True)
            _sel_self_tag("denied", "session_rebound")
            return "Error: session_rebound"

        # GPT r9 blocking (live-alias overwrite): a SECOND live slot bound to
        # the same transcript still holds the pre-update tags in memory, and
        # its next dirty flush would persist those stale tags over the update
        # we just committed. Mirror the applied tags onto every live alias
        # inside this same lock, so any later flush of an alias writes the
        # same (current) state instead of losing it.
        for other in state._slots.values():
            if other is slot:
                continue
            try:
                if slot_history_key(other) == authorized_history_key:
                    other.tags = list(slot.tags)
            except Exception:
                logger.debug("chat_tag alias tag mirror failed", exc_info=True)

        # GPT review finding (queued stale flush): a dirty ALIAS flush already
        # in flight may have captured the pre-update tags BEFORE the mirror
        # above ran, and its write can land after the confirmed save — a
        # restart would then restore the stale tags. Re-save once AFTER the
        # mirror: this write serializes behind any such queued flush on the
        # same transcript, so the committed state is what lands last. Memory
        # is already correct either way, so a failed re-save just marks the
        # slot dirty and the periodic flush reconverges from mirrored memory.
        try:
            resealed = await save_slot_off_loop(
                state, slot, force=True, expected_history_key=authorized_history_key
            )
            if not resealed:
                slot._dirty = True
        except Exception:
            logger.debug("chat_tag post-mirror re-save failed", exc_info=True)
            try:
                slot._dirty = True
            except Exception:
                pass

    _push(state)
    _sel_self_tag("allowed", ",".join(slot.tags))

    # Resulting tag NAMES for the model (the READ path). Fall back to ids for
    # any tag whose vocabulary entry lacks a name. String ids only: a
    # malformed (e.g. list-valued) ``id`` loaded from tags.json is unhashable
    # and would raise here AFTER the mutation committed, reporting failure on
    # a persisted change (GPT review finding).
    name_by_id = {
        t.get("id"): (t.get("name") or t.get("id"))
        for t in state._tags
        if isinstance(t.get("id"), str)
    }
    names = [str(name_by_id.get(tid, tid)) for tid in slot.tags]
    shown = ", ".join(names) if names else "(none)"
    return f"Board tags updated. This session now carries: {shown}."


async def _suggest_followup(state: Any, slot: Any, args: dict[str, Any]) -> str:
    from kiro_crew.dashboard.chat_handlers import _redact_followup_item

    items = [_redact_followup_item(i) for i in (args.get("items") or [])]
    if not items:
        return "No follow-up items to show."
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return "Follow-up card could not be delivered (no owner channel)."
    clients = int(
        await deliver("followup_card", {"slot": slot.key, "items": items, "ts": time.time()})
    )
    if clients == 0:
        return (
            "Follow-up card prepared, but no dashboard client is attached — "
            "restate the follow-ups in your reply text so they are not lost."
        )
    if not getattr(slot, "project", ""):
        # The card renders "Start in new worktree" DISABLED when the slot has no
        # project directory (FollowUpCard.tsx gates on projectDir), and this
        # confirmation is the model's only window into that: without it the
        # agent recommends the worktree route in sessions where it can never
        # work — Research Lab worker slots, for one, are created unscoped
        # (auto_research/handlers.py) — and steers the user into a dead button.
        return (
            "Follow-up card shown below the composer. Note: this session has no "
            "project directory, so the card's 'Start in new worktree' button is "
            "disabled. Point the user at 'Add to this session' instead, or "
            "suggest they scope a project first (the composer's Project chip)."
        )
    return "Follow-up card shown below the composer."


async def _ask_question(state: Any, slot: Any, args: dict[str, Any]) -> str:
    """Post a NON-BLOCKING question card to this session's slot. The card
    carries no ask_id, so the frontend submit sends the answers as an ordinary
    next message that resumes the session — the agent must END its turn now."""
    post = getattr(state, "post_question_card", None)
    if post is None:
        return "Question card could not be delivered (no card channel)."
    clients = int(await post(slot.key, args.get("questions") or []))
    if clients == 0:
        return (
            "Question posted, but no dashboard client is attached to see it — "
            "ask in plain text and end your turn instead."
        )
    return (
        "Question card shown in this session. End your turn now — the user's "
        "answer will arrive as your next message; do not re-ask or guess."
    )


def _push(state: Any) -> None:
    push = getattr(state, "push_slots_update", None)
    if push is not None:
        try:
            push()
        except Exception:
            logger.debug("push_slots_update failed", exc_info=True)
