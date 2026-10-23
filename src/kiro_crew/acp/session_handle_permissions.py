"""Permission requests and in-band refusals.

The permission event, approve and reject, the spec-disabled deny and tripwire, the
unidentifiable, unreadable and unretained MCP identity refusals, the reject audit, and
session permission routing.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_audit_tasks``, ``_permission_gate_events``,
``_permission_options``.

Instance fields read: ``_config_options``, ``_runtime``, ``_session_id``,
``_tool_call_cache_overflow``, ``_tool_call_diff_path``,
``_tool_call_harness_tool_name``, ``_tool_call_input_redacted``, ``_tool_call_inputs``,
``_tool_call_is_shell``, ``_tool_call_mcp_server``, ``_tool_call_raw_params``,
``_tool_call_tool_name``, ``mirror_used``, ``spec_denied_tools``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from kiro_crew import acp_tool_gate, permission_floor
from kiro_crew.acp._dispatch import (
    TOOL_CALL_CACHE_MAX_ENTRIES,
    TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
    build_permission_event,
    identified_mcp_call,
    is_mcp_tool_approval,
    redact_text,
    scope_overflowed,
    scoped_tool_cache_key,
)
from kiro_crew.acp.client import AcpError, AcpToolGateUnroutable, _loggable_request_id
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter
from kiro_crew.session_environment import WORKSPACE_SERVER_NAME
from kiro_crew.trust_patterns import GrantDeadline, GrantEnded, grant_deadline_guard

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# ── Tool Approval ──


async def approve_tool(
    self: AcpSessionHandle,
    request_id: str | int,
    option_id: str | None = None,
    *,
    deadline: GrantDeadline | None = None,
    withdraw_if: Callable[[], bool] | None = None,
) -> bool:
    """Approve a pending permission request.

    ``option_id`` overrides the auto-resolved id when provided. Otherwise the
    optionIds the agent advertised (recorded by build_permission_event) are
    consulted — picking the "always" variant when the caller asked for the
    "allow_always" id, else the "once" variant. Falls back to the kiro
    literals when nothing was recorded. This keeps kiro-cli
    ("allow_once"/"allow_always") and claude-agent-acp ("allow"/"allow_always")
    working without the caller knowing the backend.

    Every approval first passes the security floor
    (:mod:`kiro_crew.permission_floor`): a request the deny floor or the
    sensitive-path checks refuse is REJECTED here, whichever consumer asked
    to approve it and whether or not that consumer consulted the gate.

    ``deadline`` is set only by an approval a timed grant backs. It is
    checked under the write lock, right before the answer is written: if it
    has passed, nothing is written, the request stays pending as it was, and
    :class:`~kiro_crew.trust_patterns.GrantEnded` tells the caller to ask
    the person instead. Without it the approval is sent as before.

    *withdraw_if* is read after the floor's thread hop and under the write
    lock right before the answer is written; once it holds, nothing is
    written, the request's records are put back, and
    :class:`~kiro_crew.permission_floor.ApprovalWithdrawn` is raised. Under
    the lock it is read ahead of the runtime's liveness check, which
    ``deadline`` follows.
    """
    gate_event = self._permission_gate_events.pop(request_id, None)
    # No recorded event means no request this transport built, so there is
    # nothing the floor could judge: refuse rather than approve unjudged.
    if gate_event is None:
        reason: str | None = permission_floor.REASON_NO_EVENT
    else:
        reason = await asyncio.to_thread(permission_floor.refusal_for, gate_event)
    if reason is not None:
        logger.warning(
            "approve_tool: security floor rejected req=%s: %s",
            _loggable_request_id(request_id),
            permission_floor.loggable_reason(reason),
        )
        await asyncio.to_thread(
            permission_floor.audit_refusal, gate_event, reason, request_id=request_id
        )
        await self.reject_tool(request_id)
        return False
    if withdraw_if is not None and withdraw_if():
        # The floor's thread hop is an await: read again before anything else
        # is taken from the request's records.
        self._restore_withdrawn(request_id, gate_event, None)
        raise permission_floor.ApprovalWithdrawn
    resolved_id = option_id
    recorded = self._permission_options.pop(request_id, None)
    if deadline is None:
        # Answered, so the turn is not waiting on a human. Also closes the
        # human-wait segment of the park so an observer does not attribute the
        # person's thinking time to the consumer (see _end_human_wait).
        self._end_human_wait()
    if recorded:
        if resolved_id is None:
            resolved_id = recorded.get("once") or recorded.get("always")
        elif resolved_id == OPTION_ALLOW_ALWAYS:
            resolved_id = recorded.get("always") or recorded.get("once") or resolved_id
        elif resolved_id == OPTION_ALLOW_ONCE:
            resolved_id = recorded.get("once") or resolved_id
    if resolved_id is None:
        resolved_id = OPTION_ALLOW_ONCE
    # The wait for the write lock is the last await before the answer goes
    # out, so the runtime reads the withdrawal again under that lock, ahead
    # of its own liveness check, and the grant's deadline after it: a runtime
    # that died during the wait reports the death, not an ended grant.
    recheck: dict[str, Callable[[], None]] = {}
    if withdraw_if is not None:
        recheck["withdrawal_check"] = lambda: permission_floor.raise_if_withdrawn(withdraw_if)
    if deadline is not None:
        recheck["before_write"] = grant_deadline_guard(deadline, request_id)
    try:
        await self._runtime.send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": resolved_id}},
            **recheck,
        )
    except GrantEnded:
        # Nothing was written: put the request back exactly as it was.
        if gate_event is not None:
            self._permission_gate_events[request_id] = gate_event
        if recorded is not None:
            self._permission_options[request_id] = recorded
        raise
    except permission_floor.ApprovalWithdrawn:
        self._restore_withdrawn(request_id, gate_event, recorded)
        raise
    if deadline is not None:
        # Answered only now: until the write the request was still pending.
        self._end_human_wait()
    return True


def _restore_withdrawn(
    self: AcpSessionHandle,
    request_id: str | int,
    gate_event: AcpEvent | None,
    recorded: dict[str, str] | None,
) -> None:
    """Put back what a withdrawn approval took, so the request is pending as before."""
    if gate_event is not None:
        self._permission_gate_events[request_id] = gate_event
    if recorded is not None:
        self._permission_options[request_id] = recorded


async def reject_tool(self: AcpSessionHandle, request_id: str | int) -> None:
    """Reject a pending permission request.

    Prefers a clean ``selected`` reject using the reject optionId the agent
    advertised (claude-agent-acp offers ``reject`` → behavior:"deny",
    surfacing a clear "permission denied" rather than the cryptic "Tool use
    aborted" the adapter throws on a ``cancelled`` outcome). Falls back to
    ``cancelled`` when no deny-shaped option was advertised — NOT a per-tool
    signal: kiro-cli maps it to cancelling the TURN, auto-denying every
    later tool call in it without prompting.
    """
    recorded = self._permission_options.pop(request_id, None)
    self._permission_gate_events.pop(request_id, None)
    # Answered (see approve_tool) — a rejection ends the human wait too.
    self._end_human_wait()
    self._note_steering_denial()
    reject_id = recorded.get("reject") if recorded else None
    if reject_id:
        await self._runtime.send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}},
        )
    else:
        # Same last-resort warning as AcpClient.reject_tool — this is the
        # second of the two ``cancelled`` fallback sites, and the cascade
        # it can trigger is otherwise silent.
        logger.warning(
            "reject_tool: no deny option advertised for req=%s; answering "
            "'cancelled', which the backend may treat as cancelling the "
            "remainder of the turn's tool calls",
            _loggable_request_id(request_id),
        )
        await self._runtime.send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )


async def _deny_spec_disabled_tool(self: AcpSessionHandle, event: AcpEvent) -> bool:
    """Refuse a call the agent spec switched off. True when it was refused.

    The mirrored counterpart of ``AcpClient._deny_spec_disabled_tool``, and the
    identity read is the SAME function on both drivers
    (:func:`~kiro_crew.acp._dispatch.identified_mcp_call`) rather than a second
    copy of it -- a driver that read identity its own way would be a restriction
    that holds on one transport and not the other.

    Both drivers refuse on the same grounds: the pair is in this session's deny
    set, proved from a channel the model cannot reach (the preceding
    ``tool_call`` frame's own ``server``/``tool``, or the ``_meta`` identity the
    harness published). A False means "not refused BY THIS", nothing more -- an
    unidentifiable call is left to the consumer's own gate, exactly as it is on
    the client's event-yielding path.

    Refused HERE rather than after the yield, because a switched-off tool is not
    a decision to offer anyone: a consumer that auto-approves on hooks or trust
    would answer it without a human, and a human offered the choice is being
    asked to re-decide something the spec already settled.

    The audit is recorded first and runs off the loop, so it can neither delay
    the refusal nor be lost when the (bounded) reject write fails.
    """
    if not self.spec_denied_tools:
        return False
    identity = identified_mcp_call(event)
    if identity is None or identity not in self.spec_denied_tools:
        return False
    server, tool = identity
    logger.warning(
        "session MCP: refusing %r on %r -- the agent spec switches it off and this "
        "transport has no wire channel for that restriction, so it is honoured at "
        "the permission request [session=%s]",
        tool,
        server,
        self._session_id,
    )
    self._audit_handle_reject(
        event.request_id,
        f"mcp__{server}__{tool}",
        "spec_disabled_tool",
        sub_session_id=event.sub_session_id or "",
    )
    await self.reject_tool(event.request_id)
    return True


def _tripwire_spec_disabled_tool(
    self: AcpSessionHandle, result: AcpEvent, msg: JsonRpcMessage
) -> None:
    """Make a switched-off tool that RAN loud, whatever let it run.

    The two refusals above fire on a permission REQUEST, and whether codex sends
    one for a given call is the adapter's behaviour -- read from its source, not
    measured here. This is the in-band check that does not depend on it: the
    result frame for a completed call carries the same ``toolCallId`` the
    ``tool_call`` frame cached its ``rawInput = {server, tool}`` under, so a
    completed call whose pair is in the deny set is detectable from Crew's own
    side of the wire -- read under the SAME origin-scoped key the ``tool_call``
    frame wrote it under, because a bare id would miss on every session and read as
    "this call carries no identity", which is the silent direction.

    A TRIPWIRE, not enforcement -- the call has already run -- so it logs at
    WARNING and audits as a security-relevant observation. That turns an adapter
    release which stopped prompting from a silent drift into a red line in the
    log and the SEL. Cheap (two dict lookups) and a no-op with an empty deny set,
    which is every host that needs no projection.

    The same authoring as ``AcpClient._tripwire_spec_disabled_tool``, because the
    deny set has three readers on that driver and a transport carrying only two
    of them is a restriction whose failure is invisible on one side.
    """
    if not self.spec_denied_tools or not result.tool_final:
        return
    scope = str((msg.params or {}).get("sessionId") or self._session_id)
    params = self._tool_call_raw_params.get(scoped_tool_cache_key(scope, result.tool_call_id or ""))
    if not isinstance(params, dict):
        return
    server, tool = params.get("server"), params.get("tool")
    if not (isinstance(server, str) and isinstance(tool, str)):
        return
    if (server, tool) not in self.spec_denied_tools:
        return
    logger.warning(
        "session MCP: a call to %r on %r COMPLETED although the agent spec switches it "
        "off; the backend ran it without asking permission, so the per-call refusal "
        "never saw it -- check the adapter's approval behaviour [session=%s]",
        tool,
        server,
        self._session_id,
    )
    self._audit_handle_reject(
        None,
        f"mcp__{server}__{tool}",
        "spec_disabled_tool_completed",
        outcome="ran_despite_spec_disable",
    )


async def _refuse_unidentifiable_mcp_approval(
    self: AcpSessionHandle, msg: JsonRpcMessage, event: AcpEvent
) -> bool:
    """Refuse an MCP approval this session cannot check against its deny set.

    ``AcpClient`` carries this refusal on ``_handle_permission`` alone, because
    that is its site with no human. This handle has no second site: the event it
    yields is what a consumer reads, and a consumer auto-approves by hook glob,
    by ``auto_approve_tools`` pattern and in trust mode. So "unidentified" cannot
    fall toward asking here either -- on a session whose spec switched tools off,
    an MCP tool approval whose call cannot be identified is REFUSED.

    Reachable rather than theoretical: codex marks its STANDALONE approval with
    ``_meta.is_mcp_tool_approval`` too, and a standalone one has no preceding
    ``tool_call`` frame, so nothing cached its ``(server, tool)``. Left to the
    consumer, that is a switched-off tool running on an auto-approve.

    Narrow by construction. It fires only on a session that HAS a deny set, which
    is a mirrored host whose agent spec switched something off; every other
    session reads one falsy attribute and returns. The tool name is the one thing
    this record cannot say, so it is audited as ``mcp__unidentified``.
    """
    if not self.spec_denied_tools:
        return False
    if not is_mcp_tool_approval(msg, event) or identified_mcp_call(event) is not None:
        return False
    logger.warning(
        "session MCP: refusing an MCP tool approval this session cannot identify -- "
        "the agent spec switches tools off here and an unidentified call cannot be "
        "checked against that, while a consumer may auto-approve it [session=%s]",
        self._session_id,
    )
    self._audit_handle_reject(
        event.request_id,
        "mcp__unidentified",
        "spec_disabled_tool_unidentified_call",
        sub_session_id=event.sub_session_id or "",
    )
    await self.reject_tool(event.request_id)
    return True


async def _refuse_unreadable_mcp_identity(self: AcpSessionHandle, event: AcpEvent) -> bool:
    """Refuse a permission request whose MCP identity was PRESENT but unreadable.

    The tool_call frame (or this permission frame's own ``mcpTool.identity``)
    named a server or tool in a spelling the MCP-name bound refused -- longer
    than what Crew's own tool surface admits. That is not a call that names
    no tool: it is a call whose exact per-tool deny cannot be checked, and
    left to a consumer it is judged under an EMPTY identity, where a
    ``@server/tool`` rule does not bind and the title-keyed grant loop may
    approve it. So it is refused here, before any consumer sees it, on every
    harness alike (a fact about the frame, not about a backend), and audited
    as ``mcp_identity_unreadable``. A frame that names nothing is untouched.
    """
    if not event.mcp_identity_unreadable:
        return False
    logger.warning(
        "session MCP: refusing a permission request whose MCP identity is unreadable -- "
        "the frame named a server or tool longer than the tool surface admits, so the "
        "per-tool rules it is under cannot be checked and a consumer would judge it "
        "under no identity [session=%s]",
        self._session_id,
    )
    self._audit_handle_reject(
        event.request_id,
        "mcp__unreadable",
        "mcp_identity_unreadable",
        sub_session_id=event.sub_session_id or "",
    )
    await self.reject_tool(event.request_id)
    return True


async def _refuse_unretained_provenance(
    self: AcpSessionHandle, msg: JsonRpcMessage, event: AcpEvent
) -> bool:
    """Refuse a permission request for a tool call whose provenance the
    per-turn caches never retained.

    ``_dispatch.admit_tool_call_cache_entry`` refuses a tool_call frame past
    the shared count bound or over the payload bound and records the emitting
    scope in ``_tool_call_cache_overflow``. The permission request for that
    call then misses every cache: no trusted params, no shell classification,
    no identity, no kind. Left to a consumer, that request is judged by its
    title alone -- and under AUTO_APPROVE the title tier and the permission
    floor are the only tiers left, so a mounted KAS ``delete_file`` of a
    write-protected config flooded past the cap would stop being refused by
    the write-plane target gate. So a request that carries NO retained
    provenance from a scope that overflowed this turn is REFUSED here, before
    any consumer sees it; the bound narrows what is provable, never what is
    permitted. A request whose id WAS retained (any cache hit) is untouched,
    as is every request from a scope the bound never refused.
    """
    _params = msg.params if isinstance(msg.params, dict) else {}
    scope = str(_params.get("sessionId") or self._session_id)
    if not scope_overflowed(self._tool_call_cache_overflow, scope):
        return False
    if event.raw_params_trusted or event.shell_classified or event.mcp_identity_trusted:
        return False
    # The bounds are named here as well as at the admission site, so the
    # operator reading the refusal alone knows which limit a call can meet:
    # more than the count of distinct calls in flight, or one call whose
    # rendered input exceeds the payload bound (a write that large is
    # refused for its size, not its target).
    logger.warning(
        "refusing a permission request whose tool call the per-turn provenance caches did "
        "not retain (cache bound reached this turn: more than %d distinct calls in flight, "
        "or a call whose input exceeds %d chars): kind, identity and target are unknown, "
        "so it cannot be judged [session=%s]",
        TOOL_CALL_CACHE_MAX_ENTRIES,
        TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
        self._session_id,
    )
    self._audit_handle_reject(
        event.request_id,
        event.title or "",
        "provenance_cache_overflow",
        sub_session_id=event.sub_session_id or "",
    )
    await self.reject_tool(event.request_id)
    return True


def _audit_handle_reject(
    self: AcpSessionHandle,
    request_id: str | int | None,
    title: str,
    error: str,
    sub_session_id: str = "",
    outcome: str = "denied",
) -> None:
    """SEL-audit a permission decision this handle made ITSELF.

    ``outcome`` is a parameter because one caller is not a rejection:
    :meth:`_tripwire_spec_disabled_tool` records that a switched-off call RAN, and
    writing that down as ``denied`` would put a false record in the SEL -- the one
    place an operator goes to find out what happened. Every other caller takes the
    default.

    The fail-close fidelity gate and the pre-turn drain answer requests
    that never reach a consumer, so no consumer-side audit fires — every
    permission decision must still leave a SEL record (repo convention;
    the runtime's unregistered-session auto-reject does the same).
    Off-loop (``asyncio.to_thread``) and BEFORE the reject goes on the
    wire: sel() may do blocking filesystem work on first use, so the audit
    never delays the answer, and the reject is a bounded write that can
    fail -- the decision must leave its SEL record whether or not the wire
    accepted it (the same audit-first ordering chat_runner's deny paths
    keep). Title is backend/LLM-authored: bounded then redacted before it
    is stored.
    """
    safe_title = redact_text(str(title)[:4096])[:120] if title else "<unknown>"
    rid = request_id if isinstance(request_id, (str, int, float)) else ""
    # Hang-resilience series: handle-owned denials (fail-close fidelity
    # gate, pre-turn drain). CHILD-origin only — the pre-turn drain also
    # answers abandoned PARENT-turn requests (sub_session_id empty), and
    # counting those would corrupt the child-denial series.
    if sub_session_id:
        emit_counter(
            CHILD_PERMISSION_DENIED,
            {"surface": "session_handle", "reason": error},
        )

    def _audit() -> None:
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                # Child rejections carry the CHILD's id in the key —
                # attributing them only to the parent would erase the
                # traceability the audit exists to provide.
                session_key=(
                    f"acp:{self._session_id}:{sub_session_id}"
                    if sub_session_id
                    else f"acp:{self._session_id}"
                ),
                agent="kirocrew",
                source="acp_session_handle",
                tool_name=safe_title,
                outcome=outcome,
                request_id=rid,
                error=error,
            )
        except Exception:
            logger.exception("SEL audit for handle-rejected permission failed")

    audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
    self._audit_tasks.add(audit_task)
    audit_task.add_done_callback(self._audit_tasks.discard)


async def apply_session_permission_routing(self: AcpSessionHandle) -> None:
    """Make a ``SESSION_CONFIG`` harness actually ask, or refuse to run it.

    Self-gating on the harness's routing mechanism, so it is inert for every
    harness whose privileged tools are made to ask some other way: kiro-cli
    and the KAS relay both route through the agent spec
    (``Routing.AGENT_SPEC``), so the body below never runs for them and
    neither backend sends an extra write.

    Two outcomes, and each is a different verdict on purpose:

    * the option was not advertised -> ``INDETERMINATE``, because Crew cannot
      tell what the adapter will do, and "cannot tell" must not read as armed;
    * the write was rejected -> ``BYPASSED``, an observed failure rather than
      missing evidence.

    Only the enforced mechanisms refuse, and ``enforce_runtime_routing`` owns
    that decision, so the scope is not re-derived here.
    """
    backend = self._runtime.acp_backend
    if acp_tool_gate.routing_for(backend) is not acp_tool_gate.Routing.SESSION_CONFIG:
        return
    if self.mirror_used and self._runtime._member_projection_needs_confirmation(self.mirror_used):
        await self._runtime._confirm_member_projection(self)
    option_id, value = acp_tool_gate.permission_config_for(backend)
    issue = acp_tool_gate.session_config_issue(backend, self._config_options)
    if issue:
        # Not advertised: INDETERMINATE, never BYPASSED. The adapter may well
        # ask anyway; Crew simply has no evidence, and the enforcement treats
        # the two identically while the message stays honest.
        try:
            acp_tool_gate.enforce_runtime_routing(
                backend,
                issue,
                verdict=acp_tool_gate.Verdict.INDETERMINATE,
                remedy=acp_tool_gate.remediation_for(backend),
            )
        except acp_tool_gate.ToolGateUnroutable as exc:
            raise AcpToolGateUnroutable(str(exc)) from None
        return

    try:
        await self.set_config_option(option_id, value)
    except AcpError as exc:
        # The option was advertised and the write still failed, so this is an
        # observed bypass rather than missing evidence.
        try:
            acp_tool_gate.enforce_runtime_routing(
                backend,
                "the adapter rejected its required session permission configuration",
                verdict=acp_tool_gate.Verdict.BYPASSED,
                remedy=acp_tool_gate.remediation_for(backend),
            )
        except acp_tool_gate.ToolGateUnroutable as gate_exc:
            raise AcpToolGateUnroutable(str(gate_exc)) from exc
        return

    logger.info(
        "ACP permission route armed: %s=%s (%s)",
        option_id,
        value,
        acp_tool_gate.label_for(backend),
    )


def _remote_workdir(remote_workspace: object) -> str:
    """The guest workdir of an attached remote workspace; empty when unreadable, which
    leaves its tools unmapped rather than anchored somewhere else."""
    workdir = remote_workspace.get("workdir") if isinstance(remote_workspace, dict) else None
    return workdir if isinstance(workdir, str) else ""


def _build_permission_event(self: AcpSessionHandle, msg: JsonRpcMessage) -> AcpEvent | None:
    """Build an AcpEvent for a permission request via the shared parser.

    Delegates to _dispatch.build_permission_event so this transport reads the
    kiro/claude payload shape (toolCall-nested title/kind/toolCallId, option
    normalization, is_shell from the trusted tool_call cache) identically to
    AcpClient. Records the advertised optionIds so approve/reject echo them.
    """
    _perm_params = msg.params if isinstance(msg.params, dict) else {}
    _remote = getattr(self._runtime, "_remote_workspace", None)
    event, recorded = build_permission_event(
        msg,
        tool_input_cache=self._tool_call_inputs,
        tool_input_redacted_cache=self._tool_call_input_redacted,
        shell_cache=self._tool_call_is_shell,
        raw_params_cache=self._tool_call_raw_params,
        diff_path_cache=self._tool_call_diff_path,
        tool_kind_cache=self._tool_kind_cache_for_wire(),
        mcp_server_name_cache=self._tool_call_mcp_server,
        tool_name_cache=self._tool_call_tool_name,
        harness_tool_name_cache=self._tool_call_harness_tool_name,
        identity_unreadable_cache=self._identity_unreadable_cache_for_wire(),
        # ORIGIN-BOUND provenance: cache entries are keyed by the
        # emitting frame's sessionId, so a child cannot replay a consumed
        # parent toolCallId to inherit trusted params for a different
        # operation, while same-origin repeat frames still resolve.
        cache_scope=str(_perm_params.get("sessionId") or self._session_id),
        kas_consent_meta=self._runtime.acp_backend == ACP_BACKEND_KAS,
        harness_backend=self._runtime.acp_backend,
        # Set only on a runtime that attached a remote workspace server, with the
        # guest workdir its relative path arguments resolve in.
        remote_workspace_server=(WORKSPACE_SERVER_NAME if _remote is not None else ""),
        remote_workspace_root=_remote_workdir(_remote),
    )
    if event is None:
        return None
    if recorded is not None and event.request_id != "":
        self._permission_options[event.request_id] = recorded
    if event.request_id != "":
        self._permission_gate_events[event.request_id] = event
    # A frame the runtime routed here for a backend-internal subagent
    # carries the CHILD's sessionId, not this handle's. Mark the origin so
    # the policy consumer can tell reduced-fidelity requests apart. Child
    # tool_call/refinement frames ARE routed into the caches above (same
    # parser as slot-owned frames), so a well-behaved child carries full
    # structured context; the low-fidelity downgrade applies only when the
    # provenance flags say the context never arrived (frame race, drop).
    frame_sid = str((msg.params or {}).get("sessionId") or "")
    if frame_sid and frame_sid != self._session_id:
        event.sub_session_id = frame_sid
    return event
