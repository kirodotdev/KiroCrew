"""The security floor every ACP permission approval passes through.

Each consumer of a permission request (dashboard, Slack, channels, cron,
subagents, ...) is expected to consult ``HookManager.on_tool_call`` and refuse
a ``TOOL_DENY`` before it approves. That is a per-consumer duty, and a consumer
that forgets it approves a call the deny floor refuses everywhere else.

The two transports that can send an ``allow`` answer -- ``AcpClient`` and
``SessionHandle`` -- call :func:`refusal_for` inside ``approve_tool``, so the
always-on security tiers (the denied-command floor, the sensitive-path read and
write checks, the unverifiable-shell refusal) hold for every consumer, whether
or not it wired the gate.

Only a SECURITY deny refuses at the transport. The governance ceiling is a
POLICY deny that depends on the calling surface's identity (session, agent,
app), which a transport does not know, so asking it here with an empty identity
could refuse a call the consumer's own identity-aware check allowed. Governance
stays the consumer's job; the transport floor is the part that needs no
identity. A consumer that has an identity (the Agent-Channels loop) calls the
same :func:`refusal_for` with it and ``security_only=False``, so there is one
gate-consultation body for both.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from kiro_crew import hooks as hooks_mod
from kiro_crew import sel as sel_mod
from kiro_crew.config import KiroCrewConfig
from kiro_crew.hooks import hook_gate_kwargs

logger = logging.getLogger(__name__)

#: SEL outcome for an approval the transport floor turned into a rejection.
OUTCOME_REJECTED_TRANSPORT_FLOOR = "rejected_transport_floor"

#: SEL outcome for an allow the transport sent on its auto-approve path.
OUTCOME_AUTO_APPROVED_TRANSPORT = "auto_approved"

#: Refusal for an approval of a request id the transport recorded no event for.
REASON_NO_EVENT = "Blocked: no permission request was recorded for this id"

#: Refusal when the gate itself cannot be built or consulted.
REASON_GATE_UNAVAILABLE = "Blocked: the tool security gate could not be consulted"


def refusal_for(
    event: Any,
    *,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    security_only: bool = True,
) -> str | None:
    """The gate's refusal reason for approving ``event``, or ``None`` to allow.

    ``event`` is the permission event built for this request. The gate is built
    from the live config exactly as the KAS hook gate builds it
    (``kas_wire._gate_hook_command``), so the user's denied-command opt-outs in
    the keystone file apply here the same as on every consumer.

    With ``security_only=True`` (the transports) only a SECURITY deny refuses,
    and the consultation is not counted: it is a second look at a request the
    consumer already counted. With ``security_only=False`` (a consumer that
    passes its own ``session_key``, ``agent`` and ``app``, so the governance
    ceiling can apply) any ``TOOL_DENY`` refuses and the consultation is counted as that
    consumer's own gate decision.

    Synchronous: it reads config, the keystone file and governance state, so
    callers run it off the event loop. Fails CLOSED: a gate that cannot be built
    or consulted refuses the approval rather than letting an unjudged call
    through.
    """
    try:
        manager = hooks_mod.HookManager(
            hooks_mod.hooks_config_from_config_dict(KiroCrewConfig.load().hooks)
        )
        counting = hooks_mod.uncounted_gate() if security_only else contextlib.nullcontext()
        with counting:
            decision = manager.on_tool_call(
                getattr(event, "title", "") or "",
                session_key=session_key,
                agent=agent,
                app=app,
                **hook_gate_kwargs(event),
            )
    except Exception:
        logger.warning("tool gate could not be consulted for a permission request", exc_info=True)
        return REASON_GATE_UNAVAILABLE
    if decision.action != hooks_mod.TOOL_DENY:
        return None
    if security_only and not decision.security_deny:
        return None
    return decision.reason or "Blocked by security policy"


def audit_refusal(event: Any, reason: str, *, request_id: str | int = "") -> None:
    """Record the refusal where operators read tool decisions. Best-effort."""
    try:
        sel_mod.sel().log_tool_invocation(
            session_key="",
            source="transport_floor",
            tool_name=sel_mod._redact_and_clip(getattr(event, "title", "") or ""),
            tool_kind=sel_mod._redact_and_clip(getattr(event, "tool_kind", "") or ""),
            outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
            request_id=getattr(event, "request_id", "") or request_id,
            metadata={
                "reason": sel_mod._redact_and_clip(reason),
                "mechanism": "always_deny_transport",
            },
        )
    except Exception:
        logger.debug("transport floor SEL audit failed", exc_info=True)


def audit_auto_approval(event: Any, *, request_id: str | int = "") -> None:
    """Record an allow this transport sent on its own auto-approve path. Best-effort."""
    try:
        sel_mod.sel().log_tool_invocation(
            session_key="",
            source="transport_auto_approve",
            tool_name=sel_mod._redact_and_clip(getattr(event, "title", "") or ""),
            tool_kind=sel_mod._redact_and_clip(getattr(event, "tool_kind", "") or ""),
            outcome=OUTCOME_AUTO_APPROVED_TRANSPORT,
            request_id=getattr(event, "request_id", "") or request_id,
            metadata={"mechanism": "transport_auto_approve"},
        )
    except Exception:
        logger.debug("transport auto-approval SEL audit failed", exc_info=True)
