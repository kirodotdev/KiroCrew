"""Apply a decoded session directive against the consumer's OWN session.

Called from ``_on_tool_result`` (``dashboard/chat_turn/tool_results.py``), a phase of the
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

import importlib as _importlib
import logging
import sys as _sys
from typing import Any

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.session_directive_apply_monitor import (
    _autonudge_stop,
    _monitor_start,
    _monitor_stop,
    _monitor_update,
    _monitor_watch,
    _refuse_stale_wake_arm,
    _refuse_stale_wake_stop,
)
from kiro_crew.dashboard.session_directive_apply_outcome import (
    _ARMING_DIRECTIVES,
    _DASHBOARD_ONLY_DIRECTIVES,
    _REVISION_DIRECTIVES,
    _STOP_DIRECTIVES,
    _USER_ORIGIN_DIRECTIVES,
    _USER_SURFACE_DIRECTIVES,
    REVISION_REFUSAL_NOTICE_PREFIX,
    STOP_REFUSAL_NOTICE_PREFIX,
    DirectiveOutcome,
    _DirectiveDenied,
    _has_user_surface,
    _surface_arm_refusal,
)
from kiro_crew.dashboard.session_directive_apply_slot import (
    _apply_chat_tag,
    _nothing_to_do,
    _refuse_unvouched_wake_reset,
    _reset_conversation,
    _set_project,
    _suggest_followup,
)
from kiro_crew.session_surface import has_dashboard_surface

logger = logging.getLogger("kiro_crew.dashboard.session_directive_apply")


def _audit(session_key: str, kind: str, outcome: str) -> None:
    """Emit a SEL tool-invocation event for one directive application.

    AUTOSDE ``backend-security-controls`` requires every tool invocation AND
    permission decision to emit a SEL event — the effect runs here (not in the
    tool body or an HTTP endpoint), so the audit does too. Best-effort: a
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
    producer_is_self_wake: bool = False,
    producer_is_channel: bool = False,
    producer_wake_loop_id: str = "",
    producer_turn_actor: str = "",
) -> str:
    """Apply directive *kind*; return the confirmation TEXT only.

    The string-returning entry every existing consumer calls. A consumer that
    owns an empty-response verdict (the dashboard runner) calls
    :func:`apply_session_directive_outcome` instead and reads the structured
    ``ends_turn`` signal; everything about the application is identical.
    """
    outcome = await apply_session_directive_outcome(
        state,
        slot,
        session_key,
        kind,
        args,
        producer_is_user_facing=producer_is_user_facing,
        producer_is_self_wake=producer_is_self_wake,
        producer_is_channel=producer_is_channel,
        producer_wake_loop_id=producer_wake_loop_id,
        producer_turn_actor=producer_turn_actor,
    )
    return outcome.text


def _outcome(result: "str | DirectiveOutcome") -> DirectiveOutcome:
    """Normalise an applier's return. Only the terminal appliers return a
    ``DirectiveOutcome``; a returned failure text never ends the turn, whatever
    the applier claimed, so the audit and the signal cannot disagree."""
    if isinstance(result, DirectiveOutcome):
        if result.text.startswith("Error:"):
            return DirectiveOutcome(result.text, False)
        return result
    return DirectiveOutcome(result, False)


async def apply_session_directive_outcome(
    state: Any,
    slot: Any,
    session_key: str,
    kind: str,
    args: dict[str, Any],
    *,
    producer_is_user_facing: bool = False,
    producer_is_self_wake: bool = False,
    producer_is_channel: bool = False,
    producer_wake_loop_id: str = "",
    producer_turn_actor: str = "",
) -> DirectiveOutcome:
    """Apply directive *kind* with *args* to *slot*/*session_key*; return the
    structured :class:`DirectiveOutcome` (confirmation text for the model plus
    the terminal-turn signal). Fail-soft: any error is returned as a
    readable message, never raised. Every path emits a SEL audit event.
    ``slot`` is ``None`` for a channel (TurnDriver) caller — see the module
    docstring. ``producer_wake_loop_id`` names the loop whose delivered wake
    this turn is (set with ``producer_is_self_wake`` by ``_fire_dashboard_nudge``);
    the arming gate reads that row to tell a live wake from a stale one."""
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
        return DirectiveOutcome(
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
        return DirectiveOutcome(
            f"Error: {kind} targets this turn's chat slot, and this turn "
            f"holds none (this turn is {session_key!r}). Nothing was changed."
        )
    # The one non-human producer a user-surface directive admits: a conductor's
    # patrol wake dropping its own chat at a round close. Admission here only
    # skips the human-turn refusal below; ``_refuse_unvouched_wake_reset``
    # decides it, before anything is queued.
    wake_reset = bool(
        kind == "reset_conversation"
        and producer_is_self_wake
        and not producer_is_user_facing
        and _has_user_surface(session_key)
    )
    if (
        kind in _USER_ORIGIN_DIRECTIVES
        and not wake_reset
        and (not producer_is_user_facing or not _has_user_surface(session_key))
    ):
        # A cron turn can run on a user's slot and a sub-agent can share its
        # parent's slot, so both inherit a surface they never opened.
        _audit(session_key, kind, "denied")
        if kind in _USER_SURFACE_DIRECTIVES:
            # Unchanged wording: extending the set must not silently reword an
            # unrelated tool's refusal.
            return DirectiveOutcome(
                f"Error: {kind} only works from a user-facing session (dashboard "
                f"or a messaging channel); headless callers such as cron jobs and "
                f"sub-agents are refused (this turn is {session_key!r}). "
                "Nothing was changed."
            )
        return DirectiveOutcome(
            f"Error: {kind} only works from a turn a person sent; cron jobs, "
            f"sub-agents, task-runner turns and this session's own wakes are "
            f"refused even when this session has an open dashboard tab "
            f"(this turn is {session_key!r}). Nothing was changed."
        )
    # SELF-ARM PROVENANCE: which turns count as "the session's own" for the
    # crew/member rule. Two producers, each named explicitly: a turn a HUMAN
    # started in this session (the same authenticated-human flag the
    # set_project / reset_conversation gate uses), and the delivered wake of a
    # loop bound to this very slot (marked by ``_fire_dashboard_nudge``) -- a
    # member's loop firing on the member's slot is the member keeping itself
    # awake, so the re-arm or revision it issues from inside that cycle is its
    # own act -- with one exception, ``_refuse_stale_wake_arm``: a delivered
    # wake can outlive a person's Stop, and that stale turn must not create a
    # replacement automation. Reviving a loop a person stopped needs a new user
    # request; a wake may revive a loop its own cycle cap or runtime budget
    # deactivated, by raising that bound (``_monitor_update``).
    # Everything else -- a cron injection, an app-driven turn, a
    # sub-agent sharing the slot -- carries neither mark and is refused. This is
    # NOT the user-surface gate above: ``set_project`` / ``chat_tag`` stay
    # human-only, a wake must never retarget the slot's project, and
    # ``reset_conversation`` admits a wake only through
    # ``_refuse_unvouched_wake_reset``.
    self_arm_ok = bool(producer_is_user_facing or producer_is_self_wake)
    try:
        if producer_is_self_wake and kind in _ARMING_DIRECTIVES:
            _refuse_stale_wake_arm(session_key, producer_wake_loop_id)
        elif producer_is_self_wake and producer_wake_loop_id and kind in _REVISION_DIRECTIVES:
            _refuse_stale_wake_arm(
                session_key,
                producer_wake_loop_id,
                require_current=True,
            )
        elif producer_is_self_wake and producer_wake_loop_id and kind in _STOP_DIRECTIVES:
            _refuse_stale_wake_stop(session_key, producer_wake_loop_id)
        result: str | DirectiveOutcome
        if kind == "monitor_start":
            result = await _monitor_start(
                state,
                session_key,
                args,
                slot=slot,
                self_arm_ok=self_arm_ok,
                producer_is_channel=producer_is_channel,
            )
        elif kind == "monitor_watch":
            result = await _monitor_watch(
                state,
                session_key,
                args,
                slot=slot,
                self_arm_ok=self_arm_ok,
                producer_is_channel=producer_is_channel,
            )
        elif kind == "monitor_update":
            result = await _monitor_update(
                state,
                session_key,
                args,
                self_arm_ok=self_arm_ok,
                producer_is_channel=producer_is_channel,
                allow_rearm=not producer_is_self_wake,
            )
        elif kind == "monitor_stop":
            result = await _monitor_stop(
                slot,
                session_key,
                args,
                wake_loop_id=producer_wake_loop_id if producer_is_self_wake else "",
            )
        elif kind == "autonudge_stop":
            result = await _autonudge_stop(
                slot,
                session_key,
                args,
                wake_loop_id=producer_wake_loop_id if producer_is_self_wake else "",
            )
        elif kind == "set_project":
            result = await _set_project(state, slot, args)
        elif kind == "reset_conversation":
            if wake_reset:
                await _refuse_unvouched_wake_reset(state, slot, session_key, producer_wake_loop_id)
            result = await _reset_conversation(slot, session_key, args, from_wake=wake_reset)
            if wake_reset:
                logger.warning(
                    "session-directive: patrol wake reset the conversation for "
                    "session_key=%r loop_id=%r",
                    session_key,
                    producer_wake_loop_id,
                )
        elif kind == "chat_tag":
            result = await _apply_chat_tag(state, slot, session_key, args)
        elif kind == "suggest_followup":
            result = await _suggest_followup(state, slot, args)
        elif kind == "nothing_to_do":
            result = _nothing_to_do(
                args,
                producer_is_user_facing=producer_is_user_facing,
                producer_is_self_wake=producer_is_self_wake,
                producer_is_channel=producer_is_channel,
            )
        else:
            _audit(session_key, kind, "error")
            return DirectiveOutcome(f"Error: unknown session directive {kind!r}.")
    except _DirectiveDenied as exc:
        reason = str(exc)
        if not self_arm_ok and kind in _ARMING_DIRECTIVES | _REVISION_DIRECTIVES:
            # The directive targets the session that produced it, so an "outside
            # the session" refusal here was earned by the turn's origin.
            reason = _origin_arm_refusal(reason, producer_turn_actor)
        _audit(session_key, kind, "denied")
        logger.warning(
            "session-directive DENIED at apply for session_key=%r kind=%r: %s",
            session_key,
            kind,
            reason,
        )
        if kind in _ARMING_DIRECTIVES:
            _surface_arm_refusal(
                state,
                slot,
                kind,
                reason,
                session_key=session_key,
                producer_is_self_wake=producer_is_self_wake,
                producer_wake_loop_id=producer_wake_loop_id,
            )
        elif kind in _REVISION_DIRECTIVES:
            _surface_arm_refusal(
                state,
                slot,
                kind,
                reason,
                prefix=REVISION_REFUSAL_NOTICE_PREFIX,
                session_key=session_key,
                producer_is_self_wake=producer_is_self_wake,
                producer_wake_loop_id=producer_wake_loop_id,
            )
        elif kind in _STOP_DIRECTIVES:
            _surface_arm_refusal(
                state,
                slot,
                kind,
                reason,
                prefix=STOP_REFUSAL_NOTICE_PREFIX,
                session_key=session_key,
                producer_is_self_wake=producer_is_self_wake,
                producer_wake_loop_id=producer_wake_loop_id,
            )
        return DirectiveOutcome(reason)
    except Exception as exc:  # never propagate into the turn loop
        logger.warning("apply_session_directive(%s) failed", kind, exc_info=True)
        _audit(session_key, kind, "error")
        return DirectiveOutcome(f"Error applying {kind}: {exc}")
    # Some appliers RETURN a readable failure instead of raising (an invalid
    # project dir, an absent loop, no attached client), so a blanket "success"
    # would falsely mark those in the SEL chain. Derive the outcome from the
    # result the same way call_tool_with_logging does (an "Error:" prefix ==
    # failed), keeping the audit truthful for the failure paths too.
    outcome = _outcome(result)
    _audit(session_key, kind, "error" if outcome.text.startswith("Error:") else "success")
    return outcome


_TURN_ORIGINS = {"subagent": "a sub-agent result", "cron": "a cron job", "app": "an app"}


def _origin_arm_refusal(reason: str, turn_actor: str) -> str:
    """Reword the crew/member outside-arm refusal for a turn that is not the session's own.

    Text only: who may arm (``autonudge_authz.is_self_arm``) is unchanged."""
    from kiro_crew.autonudge_authz import _EXTERNAL_ARM_REFUSED_MODES, external_arm_refusal

    origin = _TURN_ORIGINS.get(turn_actor, "something other than the user")
    for mode in sorted(_EXTERNAL_ARM_REFUSED_MODES):
        reason = reason.replace(
            external_arm_refusal(mode),
            # The notice row shows this to the person, so it speaks to them.
            f"this turn was started by {origin}, so it cannot start or change a loop. "
            "To do it, send the request yourself in this chat.",
        )
    return reason


from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.session_directive_apply_monitor import (  # noqa: F401
        _STOP_NOT_APPLIED,
        _binding,
        _no_loop_message,
        _StaleWakeStopGuard,
        _stop_resolved_loop,
        _structured_binding,
        _structured_monitor_update,
        _structured_stop_reason,
    )
    from kiro_crew.dashboard.session_directive_apply_outcome import (  # noqa: F401
        ARM_REFUSAL_NOTICE_KIND,
        ARM_REFUSAL_NOTICE_PREFIX,
        ARM_SUCCESS_NOTICE_PREFIX,
        QUIET_END_OUTCOME_PREFIX,
        REFUSAL_SELF_WAKE_META_KEY,
        REFUSAL_WAKE_LOOP_ID_META_KEY,
        TERMINAL_DIRECTIVES,
        _deliver_refusal_to_crewmate,
        _describe_interval,
        _describe_next_wake,
        _surface_arm_success,
    )
    from kiro_crew.dashboard.session_directive_apply_slot import (  # noqa: F401
        QUIET_END_REFUSED_USER_TURN,
        _push,
        _slot_has_pending_interaction,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.apps.builtins.auto_research.session_keys import (  # noqa: F401
        is_owned_research_slot,
    )
    from kiro_crew.autonudge import (  # noqa: F401
        APPROVAL_STALL_REASON,
        AUTONUDGE_STOP_REASON,
        CONSECUTIVE_FAILURE_REASON,
        MONITOR_TERMINAL_REASON,
        STOP_SENTINEL_REASON,
        is_channel_key,
    )
    from kiro_crew.autonudge_judge import (  # noqa: F401
        screen_phrase,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        is_channel_session_key,
    )

# This module is the facade of directive application: every name is reached through it
# as ``kiro_crew.dashboard.session_directive_apply.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module,
# and they log under its name. The dispatcher above stays here. New work goes to the
# owner of its responsibility:
#
#   ``session_directive_apply_outcome`` -- the outcome, the directive families, the notices
#   ``session_directive_apply_monitor`` -- the monitor and autonudge directives
#   ``session_directive_apply_slot`` -- the slot-targeted directives and the quiet end
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.session_directive_apply_outcome",
    "kiro_crew.dashboard.session_directive_apply_monitor",
    "kiro_crew.dashboard.session_directive_apply_slot",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
