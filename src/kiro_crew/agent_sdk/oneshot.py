"""One-shot agent prompts for the Advisor reviewer, behind the SDK boundary.

The advisor's reviewer needs exactly three things from the agent backend:
spawn a runtime pinned to a packaged agent, keep its subprocess safe from the
orphan sweeper, and feed it one prompt collecting the text reply. Application
code may not import ``kiro_crew.acp`` directly (see
``scripts/check_agent_sdk_boundary.py``), so this module owns those three
operations inside the exempt SDK tree — the consolidation direction the
boundary gate exists to enable.

Deliberately minimal: no session reuse, no streaming surface. The session is
created, prompted once, and destroyed. Tool-call mediation is the one
exception: every ``permission_request`` is judged by the caller-supplied
``permission_gate`` (approve once or reject), every
tool call and decision leaves a typed SEL ``tool_invocation`` record, and an
approval is audit-or-deny -- if its record cannot be written, the request is
rejected rather than served untraced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)
from kiro_crew.sandbox import mcp_only_crew_leaf_targets
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

AgentRuntimeHandle = AcpRuntime
"""The runtime type the one-shot surface vends; the caller annotates with this
instead of importing the ACP layer itself."""

__all__ = [
    "AgentRuntimeHandle",
    "OneShotReply",
    "create_agent_runtime",
    "prompt_for_reply",
]


class OneShotAuditFailed(Exception):
    """An already-executed tool call could not be written to the audit trail:
    the prompt is abandoned rather than letting an unaudited call contribute."""


class OneShotCancelled(Exception):
    """``prompt_for_reply`` stopped before the prompt left the process: the
    caller's ``proceed`` predicate answered False after the session opened."""


class OneShotReply:
    """One prompt's outcome: text, terminal event, and the served model.

    The terminal event carries the backend's usage accounting; ``None`` when
    the stream ended without one (crash, cancel). ``model`` is the session's
    backend-resolved served model id (``""`` when unknown) -- callers
    attributing spend must prefer it over their configured model name, which
    can be empty (= inherit the gateway default).
    """

    __slots__ = ("text", "terminal", "model")

    def __init__(self, text: str, terminal: object | None, model: str = "") -> None:
        self.text = text
        self.terminal = terminal
        self.model = model


def create_agent_runtime(
    *,
    agent: str,
    work_dir: str | None,
    model: str | None = None,
) -> AcpRuntime:
    """Spawn an agent runtime pinned to a named agent spec.

    ``model`` selects the backend model for every session the runtime
    hosts; ``None`` keeps the runtime's own default resolution.

    The runtime runs under the STRICT sandbox tier. kiro-cli auto-approves its
    builtin reads (``fs_read``, ``grep``) without raising a permission request,
    so the OS sandbox's credential mask -- not the caller's permission gate --
    is what keeps an unattended reviewer's reads away from ``~/.ssh`` and
    friends; the primary's ``auto`` tier is not enough for a process that acts
    on injected checkpoints with no human in front of it.
    """
    # Crew-home leaves the tier leaves read-write for a primary's in-sandbox MCP
    # servers (the SEL trust root and key, the event log, the dashboard secret).
    # The reviewer runs no MCP server and its builtin reads auto-approve, so they
    # are hidden from its child.
    return AcpRuntime(
        agent=agent,
        work_dir=work_dir,
        sandbox_mode="strict",
        model=model,
        extra_hidden_dirs=tuple(mcp_only_crew_leaf_targets()),
    )


def _session_key(handle: object) -> str:
    """The one-shot session's audit identity for SEL tool records.

    Prefers the backend-assigned session id; falls back to a fixed surface
    marker so a handle without one still produces an attributable record.
    """
    sid = str(getattr(handle, "session_id", "") or "")
    return f"oneshot:{sid}" if sid else "oneshot:agent_sdk"


async def prompt_for_reply(
    runtime: AcpRuntime,
    *,
    cwd: str | None,
    prompt: str,
    permission_gate: Callable[[object], str],
    proceed: Callable[[], bool] | None = None,
) -> OneShotReply:
    """Feed one prompt to a fresh session; return text plus terminal event.

    Opens a session on the given runtime (agent inherited from the runtime
    spawn), collects text chunks to the terminal event, then destroys the
    session. The generator is closed explicitly so a break mid-stream never
    leaks the underlying request. The terminal event is returned so callers
    can account the turn's usage.

    ``permission_gate`` judges each ``permission_request``: it returns ``""``
    to approve the request ONCE, or a non-empty deny reason. The gate is
    required: this surface runs unattended, and an auto-approve would grant
    an escalation no policy engine has seen. A gate that raises is treated as
    a denial (fail closed).

    kiro-cli auto-approves its builtin reads (``fs_read``, ``grep``) and raises
    no permission request for them, so every observed ``tool_call`` is also
    recorded on the SEL trail as ``auto_approved`` -- unless a permission
    decision in this loop already covered that request id. Best-effort: the
    call has already run by the time the event arrives, so a failed audit is
    logged rather than raised (the approval path above it is audit-or-deny).
    """
    handle = await runtime.create_session(cwd=cwd, agent=None)
    try:
        # ``create_session`` is an await: an authorization true when the caller
        # checked can be revoked while it runs. Last check before the prompt
        # leaves the process; the session is destroyed on the way out.
        if proceed is not None and not proceed():
            raise OneShotCancelled("authorization revoked while the session opened")
        parts: list[str] = []
        terminal: object | None = None
        decided: set[str] = set()
        gen = handle.prompt(prompt)
        try:
            async for ev in gen:
                kind = getattr(ev, "kind", None)
                if kind == EVENT_TEXT_CHUNK:
                    parts.append(getattr(ev, "text", "") or "")
                elif kind == EVENT_TOOL_CALL:
                    call_id = str(getattr(ev, "request_id", "") or "")
                    if call_id and call_id in decided:
                        continue
                    _name = str(getattr(ev, "title", "") or "") or "<unknown>"
                    _kind = str(getattr(ev, "tool_kind", "") or "")
                    _skey_tc = _session_key(handle)

                    def _audit_observed() -> None:
                        sel().log_tool_invocation(
                            session_key=_skey_tc,
                            agent="agent_sdk.oneshot",
                            source="oneshot_tool_call",
                            tool_name=_name,
                            tool_kind=_kind,
                            outcome="auto_approved",
                            request_id=call_id,
                        )

                    try:
                        await asyncio.to_thread(_audit_observed)
                    except Exception as exc:
                        # The call has already run (a builtin read), so the audit
                        # cannot deny it; what it can do is stop an unaudited call
                        # from contributing: abandon the prompt (session destroyed
                        # in ``finally``) and let the caller record no reply.
                        logger.warning("one-shot tool-call SEL audit failed", exc_info=True)
                        raise OneShotAuditFailed(str(exc)) from exc
                elif kind == EVENT_PERMISSION_REQUEST:
                    # This surface runs unattended (no approval UI): an
                    # unanswered request stalls to timeout, and a blanket
                    # auto-approve would grant an escalation no policy engine
                    # has seen. The caller's gate is the policy engine; no
                    # gate, or a gate that raises, means reject. Decide, then
                    # audit BEFORE the wire write (a stalled pipe or a
                    # cancellation there must not leave the decision
                    # unaudited), then answer inline (a stdin write, cannot
                    # deadlock the read loop).
                    rid = getattr(ev, "request_id", "")
                    if rid:
                        decided.add(str(rid))
                    _tool_kind = str(getattr(ev, "tool_kind", "") or "")
                    try:
                        # The gate reads config and policy (file IO): run
                        # it in a worker thread, never on the event loop.
                        deny_reason = str(await asyncio.to_thread(permission_gate, ev) or "")
                    except Exception:  # a broken gate must DENY, not authorize
                        logger.warning(
                            "one-shot permission gate failed; denying (fail-closed)",
                            exc_info=True,
                        )
                        deny_reason = "permission_gate_error"
                    if deny_reason:
                        logger.warning(
                            "one-shot session rejected permission request id=%s " "(tool: %s): %s",
                            rid,
                            _tool_kind or "<unknown>",
                            deny_reason,
                        )
                    _skey = _session_key(handle)
                    if deny_reason:
                        # A denial's audit is best-effort: nothing runs either way.
                        try:
                            sel().log_tool_invocation(
                                session_key=_skey,
                                agent="agent_sdk.oneshot",
                                source="oneshot_permission_gate",
                                tool_name=_tool_kind or "<unknown>",
                                tool_kind=_tool_kind,
                                outcome="denied",
                                request_id=str(rid),
                                resources=f"reason={redact_text(deny_reason[:4096])[:200]}",
                            )
                        except Exception:
                            logger.debug("one-shot permission SEL audit failed", exc_info=True)
                    else:
                        # AUDIT-OR-DENY: an approval lets an unattended tool run,
                        # and the SEL record is the only trace that it did. The
                        # critical write is synchronous (a filesystem failure
                        # re-raises) and runs off the loop; if it cannot be
                        # written the request is REJECTED, not served untraced.
                        def _critical_audit() -> None:
                            sel().log_tool_invocation(
                                session_key=_skey,
                                agent="agent_sdk.oneshot",
                                source="oneshot_permission_gate",
                                tool_name=_tool_kind or "<unknown>",
                                tool_kind=_tool_kind,
                                outcome="approved",
                                request_id=str(rid),
                                resources="approve_once",
                                critical=True,
                            )

                        try:
                            await asyncio.to_thread(_critical_audit)
                        except Exception:
                            logger.warning(
                                "one-shot permission audit unwritable; rejecting id=%s "
                                "(audit-or-deny)",
                                rid,
                                exc_info=True,
                            )
                            deny_reason = "audit_unwritable"
                    try:
                        if deny_reason:
                            await handle.reject_tool(rid)
                        else:
                            await handle.approve_tool(rid)  # allow_once, never always
                    except Exception:
                        logger.debug("one-shot permission answer failed", exc_info=True)
                elif kind == EVENT_COMPLETE:
                    terminal = ev
                    break
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                await aclose()
        served = getattr(handle, "served_model", "") or ""
        return OneShotReply("".join(parts), terminal, model=str(served))
    finally:
        destroy = getattr(handle, "destroy", None)
        if destroy is not None:
            try:
                await destroy()
            except Exception:
                logger.debug("one-shot session destroy failed", exc_info=True)
