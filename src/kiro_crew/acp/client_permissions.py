"""Answer an ``AcpClient`` session's permission requests and enforce its tool restrictions.

The permission event and its answer (approve, reject, the auto-approve identity gate),
the zero-tools and spec-disabled refusals, identity-drift refusals, the pi, DeepSeek and
goose-mode tripwires, and the routing read-back, each with its SEL record.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_drain_mode_reports``, ``_mcp_notifications``,
``_mcp_report_frame_floor``, ``_permission_gate_events``, ``_permission_options``,
``_pi_gate_asked_ids``, ``_pi_gate_denied_ids``, ``_pi_gate_request_tool``,
``_routing_answer_floor``, ``_turn_permission_denied``.

Instance fields read: ``_acp_config_options``, ``_agent``,
``_copilot_ambiguous_mcp_calls``, ``_deepseek_gate_nonce``,
``_deepseek_read_only_issue``, ``_pi_gate_nonce``, ``_session_id``, ``_session_key``,
``_side_read_only``, ``_spec_denied_tools``, ``_tool_call_diff_path``,
``_tool_call_harness_tool_name``, ``_tool_call_identity_unreadable``,
``_tool_call_input_redacted``, ``_tool_call_inputs``, ``_tool_call_is_shell``,
``_tool_call_mcp_server``, ``_tool_call_params``, ``_tool_call_tool_name``,
``_tool_call_unclassified``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from kiro_crew import acp_tool_gate, permission_floor
from kiro_crew import sel as sel_module
from kiro_crew.acp._dispatch import (
    ACP_BACKENDS_META_IDENTITY,
    _loggable_request_id,
    build_permission_event,
    gate_envelope,
)
from kiro_crew.acp._dispatch import identified_mcp_call as _identified_mcp_call
from kiro_crew.acp._dispatch import is_mcp_tool_approval as _is_mcp_tool_approval
from kiro_crew.acp._dispatch import (
    meta_builtin_server_names,
    parse_session_modes,
)
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness.copilot import COPILOT_IDENTITY_OVERFLOW
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    JSONRPC_METHOD_NOT_FOUND,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    UPDATE_CURRENT_MODE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    session_config_drift_issue,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.trust_patterns import GrantDeadline, GrantEnded, grant_deadline_guard

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


# No CODEX_PATH constant: the adapter ships a compatible Codex binary as an npm
# dependency and reads CODEX_PATH itself only to run a DIFFERENT one. An operator
# who sets it reaches the child through the ambient environment copy, so naming it
# here would imply a wiring that does not exist (its claude counterpart,
# CLAUDE_CODE_EXECUTABLE, IS explicitly forwarded — the asymmetry is deliberate).


# The adapter release the gate contract was OBSERVED on: the frame corpus
# (``test/fixtures/acp_frames/pi``) records pi-acp forwarding the extension's
# confirm dialog as ``session/request_permission`` and honouring
# ``PI_ACP_PI_COMMAND`` at exactly this version. Neither link is pinned by the
# read-back, so a session on another release logs the drift by name at
# ``initialize`` -- the tripwire still catches a call that ran unasked, this
# names the likely reason before it does. A warning, not a refusal: a newer
# adapter is the common case and the tripwire is the control.
PI_ACP_VERIFIED_VERSION = "0.0.34"
# Versions already named this process, so a gateway on a newer adapter says so
# once, not on every session.
_pi_adapter_versions_noted: set[str] = set()
# goose's VERIFIED RANGE (the note beside ``ACP_BACKEND_GOOSE`` in
# ``agent_sdk/backends.py``) is 1.50.x. Of the three wire facts it names, the
# ``current_mode_update`` emission the mid-session tripwire rests on is the one that
# fails OPEN: a release that stopped emitting it would narrow enforcement back to the
# open/restore read-back with no frame saying so. ``agentInfo.version`` on the
# ``initialize`` reply is the one signal before the first prompt, so a release outside
# the range is named there. The trailing dot keeps ``1.500.x`` outside it. A warning,
# not a refusal: the two closed facts still hold on any release, and a newer goose is
# the common case.
GOOSE_VERIFIED_VERSION_PREFIX = "1.50."
# Same once-per-process memo as the pi note above.
_goose_versions_noted: set[str] = set()


def _scrub_observed(value: object) -> object:
    """Scrub a string that came out of the operator's own harness config.

    It travels into a refusal that reaches the dashboard and the chat card, so it
    goes through the same two scrubs every other backend-sourced string in this
    module does. A rule map or an agent name can spell anything -- a URL, a
    secret-shaped literal -- and a refusal is not a reason to publish it.
    Non-strings pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


# ── Dynamic Config from ACP ──


async def _apply_session_permission_routing(self: AcpClient) -> None:
    """Make a SESSION_CONFIG harness actually ask, or refuse to run it.

    Called ONLY for a ``SESSION_CONFIG`` harness -- the caller tests that, so
    the Kiro path never reaches this method (harness-parity H13).

    Two outcomes, and each is a different verdict on purpose:

    * the option was not advertised -> INDETERMINATE, because Kiro Crew cannot
      tell what the adapter will do, and "cannot tell" must not read as armed;
    * the write was rejected -> BYPASSED, an observed failure rather than an
      unknown.

    Only the enforced mechanisms refuse; ``enforce_runtime_routing`` owns that
    decision, so the scope lives in one place instead of being re-derived here.
    """
    backend = self.backend
    option_id, value = acp_tool_gate.permission_config_for(backend)
    issue = acp_tool_gate.session_config_issue(backend, self._acp_config_options)
    if issue:
        # Not advertised: INDETERMINATE, never BYPASSED. The adapter may well
        # ask anyway; Kiro Crew simply has no evidence, and the enforcement
        # treats the two identically while the message stays honest.
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

    if backend in ACP_BACKEND_FORBIDDEN_MODES:
        # Read the write back rather than trusting "no error": the response carries
        # the session's configOptions, and a harness whose higher-ranked permission
        # source wins could accept the write and still report the option on.
        drift = session_config_drift_issue(
            backend,
            {"sessionUpdate": "config_option_update", "configOptions": self._acp_config_options},
        )
        if drift:
            try:
                acp_tool_gate.enforce_runtime_routing(
                    backend,
                    f"after Kiro Crew's write, {drift}",
                    verdict=acp_tool_gate.Verdict.BYPASSED,
                    remedy=acp_tool_gate.remediation_for(backend),
                )
            except acp_tool_gate.ToolGateUnroutable as gate_exc:
                raise AcpToolGateUnroutable(str(gate_exc)) from None
            return
        # Frames buffered while the write was in flight can still name the value the
        # session was restored in (measured -- a restored ``allow_all on`` is reported
        # once more just before the write's own answer). The answer supersedes them;
        # everything read after it is live and is checked.
        self._routing_answer_floor = len(self._mcp_notifications)

    logger.info(
        "ACP permission route armed: %s=%s (%s)",
        option_id,
        value,
        acp_tool_gate.label_for(backend),
    )


def _verify_goose_routing(self: AcpClient, resp: dict) -> None:
    """Read this harness's OWN resolved mode back off the session response.

    This is the half that makes the routing VERIFIED rather than seeded, and on
    this harness it costs no second child: the mode goose resolved is reported in
    the ``modes`` block of the very response that opens or restores the session,
    so what comes back is the mode the session will actually use and it arrives on
    the session's own connection. A precedence change in a future release
    therefore surfaces as a refusal here rather than as a session that silently
    stops asking.

    Called on BOTH paths, and the restore path is the one that needs it most: the
    environment seed governs a session goose CREATES, but a session goose RESTORES
    carries the mode it was last left in, and ``session/set_mode`` accepts the
    auto-approving one. So a session resumed after any actor moved it would come
    back permissive, and only this read can tell.

    What it does NOT establish is that the harness HONOURS the mode per tool call;
    that is the harness's own contract and no client-side read can prove it. The
    scope is the precondition, and the frame corpus carries the observation of the
    emission.

    Raises ``AcpToolGateUnroutable`` when the required mode is not in force.
    """
    if not self._is_goose:
        return
    _ids, current_mode, advertised = parse_session_modes(resp)
    # An omitted modes block is INDETERMINATE rather than a pass: this harness
    # always reports one, so its absence means the response is not the shape this
    # read was verified against and no claim can be made from it.
    observed = current_mode if advertised else ""
    issue = acp_tool_gate.seeded_setting_issue(self.backend, _scrub_observed(observed))
    if not issue:
        return
    try:
        acp_tool_gate.enforce_runtime_routing(
            self.backend,
            issue,
            remedy=acp_tool_gate.remediation_for(self.backend),
        )
    except acp_tool_gate.ToolGateUnroutable as exc:
        # Translated to the ACP-layer type like the sibling refusal sites, and
        # that is not cosmetic: ``ensure_ready`` catches ``AcpToolGateUnroutable``,
        # so a bare gate exception would escape both of its handlers and skip
        # ``_cleanup_failed_live_spawn`` -- leaving the refusal untyped and the
        # failed spawn unreaped.
        raise AcpToolGateUnroutable(str(exc)) from None


async def approve_tool(
    self: AcpClient,
    request_id: str | int,
    option_id: str | None = None,
    *,
    always: bool = False,
    deadline: GrantDeadline | None = None,
    withdraw_if: Callable[[], bool] | None = None,
) -> bool:
    """Approve a pending session/request_permission.

    ``option_id`` overrides the auto-resolved id when provided. Otherwise
    the recorded options for ``request_id`` are consulted — picking the
    "always" variant if ``always=True``, else the "once" variant. This
    keeps kiro-cli ("allow_once"/"allow_always") and claude-agent-acp
    ("allow"/"allow_always") working without caller knowledge.

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
    the lock it is read before ``deadline``.
    """
    # An instance allocated without ``__init__`` may keep no event map until
    # the builder creates it; read it as empty, so it is judged like any
    # other client: an unrecorded id is refused.
    gate_events = getattr(self, "_permission_gate_events", None)
    gate_event = gate_events.pop(request_id, None) if gate_events is not None else None
    # No recorded event means no request this transport built, so there
    # is nothing the floor could judge: refuse rather than approve unjudged.
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
        _restore_withdrawn(self, request_id, gate_event, None, None)
        raise permission_floor.ApprovalWithdrawn
    # An approved call may complete; forget the envelope mapping so the map
    # stays bounded by the calls still awaiting an answer.
    pi_gate_tool = getattr(self, "_pi_gate_request_tool", {}).pop(str(request_id), None)
    resolved_id = option_id
    recorded: dict[str, str] | None = None
    # Read before the pop below: the Copilot downgrade needs the offered ids.
    offered = (self._permission_options.get(request_id) or {}) if self._is_copilot else {}
    if resolved_id is None:
        recorded = self._permission_options.pop(request_id, None)
        # A recorded entry may carry only a "reject" id (a request that
        # advertised a reject option but no allow option), so use .get and
        # fall back to the canonical allow id rather than KeyError-ing.
        resolved_id = (recorded or {}).get("always" if always else "once")
        if resolved_id is None:
            resolved_id = OPTION_ALLOW_ALWAYS if always else OPTION_ALLOW_ONCE
    if self._is_copilot and resolved_id in (OPTION_ALLOW_ALWAYS, offered.get("always")):
        # Copilot CLI's allow-always stops it asking about that tool for the rest
        # of the session, so every later call -- each later file of a patch
        # included -- would run with no permission frame for Crew's gates to
        # judge. Crew keeps its own approval memory; Copilot is answered once,
        # with its own once id, or the canonical one when the frame recorded
        # allow-always in that slot too.
        once = offered.get("once")
        resolved_id = once if once and once != offered.get("always") else OPTION_ALLOW_ONCE
    # The wait for the write lock is the last await before the answer goes
    # out, so the withdrawal is read again under that lock.
    recheck: dict[str, Callable[[], None]] = {}
    if withdraw_if is not None:
        recheck["before_write"] = lambda: permission_floor.raise_if_withdrawn(withdraw_if)
    if deadline is not None:
        grant_check = grant_deadline_guard(deadline, request_id)

        def before_write() -> None:
            # The withdrawal first (no-op without one), then the grant.
            permission_floor.raise_if_withdrawn(withdraw_if)
            grant_check()

        recheck["before_write"] = before_write
    try:
        await self._send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": resolved_id}},
            **recheck,
        )
    except GrantEnded:
        # Nothing was written: put the request back exactly as it was.
        if gate_events is not None and gate_event is not None:
            gate_events[request_id] = gate_event
        if recorded is not None:
            self._permission_options[request_id] = recorded
        if pi_gate_tool is not None:
            self._pi_gate_request_tool[str(request_id)] = pi_gate_tool
        raise
    except permission_floor.ApprovalWithdrawn:
        _restore_withdrawn(self, request_id, gate_event, recorded, pi_gate_tool)
        raise
    return True


def _restore_withdrawn(
    client: AcpClient,
    request_id: str | int,
    gate_event: AcpEvent | None,
    recorded: dict[str, str] | None,
    pi_tool: str | None,
) -> None:
    """Put back what a withdrawn approval took, so the request is pending as before."""
    if gate_event is not None:
        client._permission_gate_events[request_id] = gate_event
    if recorded is not None:
        client._permission_options[request_id] = recorded
    if pi_tool is not None:
        client._pi_gate_request_tool[str(request_id)] = pi_tool


def _note_pi_gate_denied(self: AcpClient, request_id: str | int) -> None:
    """Remember that the host DENIED the gate-extension dialog for this request.

    pi honours the extension's ``{block: true}`` by not running the call; that
    link is the adapter's and the harness's, not Kiro Crew's, so it is guarded
    in band like the other two: a denied call that later reports ``completed``
    trips :meth:`_tripwire_pi_gate`. No-op for a request no envelope named.
    """
    tool_call_id = getattr(self, "_pi_gate_request_tool", {}).pop(str(request_id), None)
    if tool_call_id:
        self._pi_gate_denied_ids.add(tool_call_id)


async def reject_tool(self: AcpClient, request_id: str | int) -> None:
    """Reject a pending session/request_permission.

    Prefers a clean ``selected`` reject using the reject optionId the agent
    advertised (claude-agent-acp offers ``reject`` → behavior:"deny",
    surfacing a clear "permission denied" rather than the cryptic
    "Tool use aborted" the adapter throws on a ``cancelled`` outcome).
    Falls back to ``cancelled`` when no reject option was advertised
    (kiro-cli), which kiro handles as an ordinary rejection.
    """
    recorded = self._permission_options.pop(request_id, None)
    getattr(self, "_permission_gate_events", {}).pop(request_id, None)
    self._note_pi_gate_denied(request_id)
    # Read by the concurrent-prompt steer settle: this denial may end the turn
    # before a steer sent into it is read.
    self._turn_permission_denied = True
    reject_id = recorded.get("reject") if recorded else None
    if reject_id:
        await self._send_response(
            request_id, {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}}
        )
    else:
        # Last resort, and not a per-tool signal: kiro-cli maps a
        # `cancelled` outcome to cancelling the TURN, so every later tool
        # call in it resolves as denied without prompting. Say so
        # where an operator will find it — the silent cascade is the bug
        # report's whole complaint.
        logger.warning(
            "reject_tool: no deny option advertised for req=%s; answering "
            "'cancelled', which the backend may treat as cancelling the "
            "remainder of the turn's tool calls",
            _loggable_request_id(request_id),
        )
        await self._send_response(request_id, {"outcome": {"outcome": OUTCOME_CANCELLED}})


async def _handle_permission(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Auto-approve tool permissions.

    When this session judges its requests (:attr:`_judges_permission_requests`),
    the refusals run first, because this site answers the request with no
    consumer's gate in between and a restriction must hold on every path that
    answers: the zero-tool refusal (every request, when the spec declares
    ``"tools": []``), then a switched-off tool, then a harness identity that is
    absent or names a server this session never mounted. And because there is no human here,
    "unidentified" cannot fall toward asking: on a session with a deny set, an MCP
    tool approval (the adapter marks one with ``_meta.is_mcp_tool_approval``)
    whose call this client cannot identify is REFUSED rather than approved blind.
    On a session that judges nothing, nothing is checked here and the answer is
    the plain approve it always was: the event is still built, so approve_tool's
    security floor can judge the request. Its advertised allow option ids stay
    unrecorded, while its reject id is kept so a floor refusal answers with the
    advertised reject option.
    """
    event = self._build_permission_event(msg)
    if event is None:
        if msg.id is not None:
            await self._send_error(msg.id, -32600, "invalid request id")
        return
    if not self._judges_permission_requests:
        options = getattr(self, "_permission_options", {})
        recorded = options.pop(event.request_id, None)
        reject_id = (recorded or {}).get("reject")
        if reject_id:
            options[event.request_id] = {"reject": reject_id}
    if self._judges_permission_requests:
        if await self._deny_zero_tools(event):
            return
        if await self._deny_spec_disabled_tool(event):
            return
        if await self._refuse_identity_drift(event):
            return
        if (
            self._spec_denied_tools
            and _is_mcp_tool_approval(msg, event)
            and not _identified_mcp_call(event)
        ):
            logger.warning(
                "session MCP: refusing an MCP tool approval this client cannot identify "
                "on an auto-approve path -- the spec switches tools off on this session "
                "and an unidentified call cannot be checked against it [session=%s]",
                self._session_id,
            )
            # A permission decision, so it reaches the SEL like the identified
            # refusal does; the tool name is the one thing this record cannot say.
            self._audit_spec_restriction(
                tool_name="mcp__unidentified",
                outcome="denied",
                reason="spec_disabled_tool_unidentified_call",
            )
            await self.reject_tool(event.request_id)
            return
    request_id = msg.id if msg.id is not None else ""
    reason = await asyncio.to_thread(
        permission_floor.refusal_for,
        event,
        session_key=self._session_key or "",
        agent=self._agent,
        security_only=False,
    )
    if reason is not None:
        logger.warning(
            "auto-approve identity gate rejected req=%s: %s",
            _loggable_request_id(request_id),
            permission_floor.loggable_reason(reason),
        )
        await asyncio.to_thread(
            permission_floor.audit_refusal, event, reason, request_id=request_id
        )
        await self.reject_tool(request_id)
        return

    params = msg.params or {}
    tool_call = params.get("toolCall", {})
    title = tool_call.get("title", "unknown")
    logger.info("Auto-approving tool: %s", title)

    # A False means the floor inside approve_tool refused, audited and
    # rejected the call itself; this path has nothing further to record.
    if not await self.approve_tool(request_id):
        return


async def _refuse_identity_drift(self: AcpClient, event: AcpEvent) -> bool:
    """Refuse a request whose harness identity is absent or names an unmounted server.

    The two refusals the identity channel earns, run from ONE place on every site
    that answers a permission request, and independent of the deny set: they are
    facts about the channel, and goose -- the one member today -- carries an empty
    deny set by ruling, so a refusal that waited for one never ran on a real goose
    session. Gated on ``ACP_BACKENDS_META_IDENTITY`` up front so no other backend
    gains a call; each predicate below keeps its own narrower scope as well.

    Copilot CLI publishes no identity channel; its MCP identity is recovered from the
    ``tool_call`` title (``_recover_copilot_mcp_identity``), and a title two placed
    servers could both have produced is refused here, before any gate reads it.

    Returns True when the request was refused (rejected on the wire and audited),
    False when it was left to the caller. A False means "not refused BY THIS".
    """
    ambiguous: set[str] = getattr(self, "_copilot_ambiguous_mcp_calls", set())
    if self._is_copilot and (
        COPILOT_IDENTITY_OVERFLOW in ambiguous
        or (event.tool_call_id and event.tool_call_id in ambiguous)
    ):
        logger.warning(
            "session MCP: refusing a Copilot CLI tool approval whose tool_call title "
            "more than one placed server could have produced, so the call's MCP "
            "server cannot be named and its governance cannot be applied [session=%s]",
            self._session_id,
        )
        self._audit_spec_restriction(
            tool_name="mcp__ambiguous__unknown",
            outcome="denied",
            reason="spec_disabled_tool_ambiguous_server",
        )
        await self.reject_tool(event.request_id)
        return True
    if self.backend not in ACP_BACKENDS_META_IDENTITY:
        return False
    if self._unclassified_tool_call(event):
        logger.warning(
            "session MCP: refusing a tool approval no channel classified -- the "
            "tool_call frame carried neither an ACP kind nor a harness _meta identity, "
            "so a command cannot be told from a served tool and no gate can be applied "
            "to it [session=%s]",
            self._session_id,
        )
        self._audit_spec_restriction(
            tool_name="tool__unclassified",
            outcome="denied",
            reason="spec_disabled_tool_unclassified_call",
        )
        await self.reject_tool(event.request_id)
        return True
    if self._foreign_mcp_identity(event):
        logger.warning(
            "session MCP: refusing an approval whose trusted identity names server "
            "%r, which Crew never placed on this session's array and which is not "
            "the harness's own extension -- a drifted builtin would otherwise pass "
            "as MCP-served and skip the command tier [session=%s]",
            event.mcp_server_name,
            self._session_id,
        )
        self._audit_spec_restriction(
            tool_name=f"mcp__{event.mcp_server_name}__{event.tool_name or 'unknown'}",
            outcome="denied",
            reason="spec_disabled_tool_foreign_server",
        )
        await self.reject_tool(event.request_id)
        return True
    return False


def _foreign_mcp_identity(self: AcpClient, event: AcpEvent) -> bool:
    """True when a trusted identity names a server this session never mounted.

    The fail-closed classification covers a channel that is ABSENT. This covers a
    channel that has DRIFTED: a harness release that renames its builtin extension
    would carry a trusted identity naming a server Crew never placed on the array,
    and the readers would file it as an MCP-served tool -- not a command, so the
    command-deny tier is skipped, and not in any deny set, so it is approved. Crew
    knows the whole server universe of a session it hands the array to: the names it
    placed there, plus the harness's own builtin extension. Anything else is refused.

    Scoped to ``ACP_BACKENDS_SESSION_MCP_ARRAY``, the backends whose array Crew
    builds and can therefore enumerate; kiro-cli's servers reach it through the agent
    file and are not enumerated here. Only a TRUSTED identity is judged -- the
    permission payload's own prose names whatever it likes and is never read.
    """
    if self.backend not in ACP_BACKENDS_SESSION_MCP_ARRAY:
        return False
    if not (event.mcp_identity_trusted and event.mcp_server_name):
        return False
    placed = {
        element.get("name")
        for element in self._session_mcp_servers()
        if isinstance(element, dict) and isinstance(element.get("name"), str)
    }
    return event.mcp_server_name not in placed | meta_builtin_server_names()


def _unclassified_tool_call(self: AcpClient, event: AcpEvent) -> bool:
    """True when the preceding ``tool_call`` frame classified this call as NOTHING.

    Fail-closed, and deliberately narrow. A call whose identity IS recoverable is
    left to the checks below this one: :func:`_identified_mcp_call` answers for a
    served tool, and a resolved shell classification answers for a command. What
    this catches is the third case -- a frame that said neither, whose permission
    request carries no ``kind`` either, and which therefore reaches an answering site
    with no fact any gate can be evaluated against: not a deny set, and not the
    command-deny tier, which sees a command only when something classified it as one.

    Keyed on the cache rather than on ``shell_classified``: that property is False on
    MCP frames from harnesses that omit ``kind``, which are identified and must keep
    their existing path.
    """
    if not event.tool_call_id:
        # No correlation id, so nothing was cached about the call and this check
        # cannot claim the frame said nothing. The MCP refusal below still applies.
        return False
    # A refusal needs POSITIVE evidence that the tool_call frame classified nothing.
    # A minimal construction with no cache never gathered that evidence, and a
    # tool_call this session never saw is not evidence either, so both answer False
    # and leave the call to the MCP checks. Same compatibility shape as the caches in
    # ``_build_permission_event``.
    if self.backend not in ACP_BACKENDS_META_IDENTITY:
        # A harness that publishes no identity channel never opted into this, and the
        # evidence that it classifies every tool class is a handful of recorded frames
        # -- enough to show a ``kind`` present, not enough to prove none is ever
        # missing. Refusing on that would turn a gap in Crew's evidence into a denial
        # of the harness's own legitimate call.
        return False
    recorded = getattr(self, "_tool_call_unclassified", None)
    if not isinstance(recorded, dict) or not recorded.get(event.tool_call_id, False):
        return False
    return _identified_mcp_call(event) is None


def _tripwire_spec_disabled_tool(self: AcpClient, result: AcpEvent) -> None:
    """Make a switched-off tool that RAN loud, whatever let it run.

    The refusal in :meth:`_deny_spec_disabled_tool` fires only on a permission
    request, and whether codex sends one for a given call is the adapter's
    behaviour, read from its source rather than measured here. This is the
    in-band check that does not depend on it: the result frame for a completed
    call carries the same ``toolCallId`` the ``tool_call`` frame cached its
    ``rawInput = {server, tool}`` under, so a completed call whose pair is in the
    deny set is detectable from Crew's own side of the wire. It is a TRIPWIRE,
    not enforcement -- the call has already run -- so it logs at WARNING and
    audits as a security-relevant observation, which turns an adapter release
    that stopped prompting from a silent drift into a red line in the log and
    the SEL. Cheap (two dict lookups) and a no-op with an empty deny set.
    """
    if not self._spec_denied_tools or not result.tool_final:
        return
    params = self._tool_call_params.get(result.tool_call_id or "")
    if not isinstance(params, dict):
        return
    server, tool = params.get("server"), params.get("tool")
    if not (isinstance(server, str) and isinstance(tool, str)):
        return
    if (server, tool) not in self._spec_denied_tools:
        return
    logger.warning(
        "session MCP: a call to %r on %r COMPLETED although the agent spec switches it off; "
        "the backend ran it without asking permission, so the per-call refusal never saw "
        "it -- check the adapter's approval behaviour [session=%s]",
        tool,
        server,
        self._session_id,
    )
    self._audit_spec_restriction(
        tool_name=f"mcp__{server}__{tool}",
        outcome="ran_despite_spec_disable",
        reason="spec_disabled_tool_completed",
    )


def _note_pi_adapter_version(self: AcpClient) -> None:
    """Log a pi adapter release other than the one the gate contract was observed on.

    Complements :meth:`_tripwire_pi_gate`: the tripwire fires AFTER a call ran
    unasked; this names the adapter drift that most plausibly causes it, at the
    handshake, before any tool call. Unknown (empty) is logged too, as the
    contract was observed on a release that reports itself. Once per process
    per version: the operator is told which release they are on, not reminded.
    """
    if not self._is_pi:
        return
    version = self.agent_version
    if version == PI_ACP_VERIFIED_VERSION or version in _pi_adapter_versions_noted:
        return
    _pi_adapter_versions_noted.add(version)
    logger.warning(
        "pi adapter %s reports version %r; the gate extension contract (dialog "
        "forwarding, %s) was verified on %s. The in-band tripwire still guards every "
        "call; if it trips, this is the first place to look.",
        pi_mod.PI_ACP_BIN,
        version or "unknown",
        pi_mod._ENV_PI_ACP_PI_COMMAND,
        PI_ACP_VERIFIED_VERSION,
    )


def _note_goose_version(self: AcpClient) -> None:
    """Log a goose release outside the verified range, at the handshake.

    Complements :meth:`_tripwire_goose_mode` the way :meth:`_note_pi_adapter_version`
    complements the pi tripwire. The two wire facts that fail closed (the mode
    read-back, the ``_meta.goose.toolCall`` identity) announce their own loss as
    refusals; the ``current_mode_update`` emission the tripwire fires on fails open,
    so its loss on a newer release would be silent. This names the release the
    session actually RUNS -- ``agentInfo.version`` off ``initialize``, not the file on
    disk -- once per process per version, so the operator knows where to look if a
    session that left the required mode is later found still running.
    """
    if not self._is_goose:
        return
    version = self.agent_version
    if version.startswith(GOOSE_VERIFIED_VERSION_PREFIX) or version in _goose_versions_noted:
        return
    _goose_versions_noted.add(version)
    logger.warning(
        "goose reports version %r; the routing contract (mode read-back, "
        "_meta.goose.toolCall identity, mid-session current_mode_update) was verified "
        "on %sx. The read-back and the identity check still refuse on their own; the "
        "mid-session mode tripwire rests on an emission this release may not make.",
        version or "unknown",
        GOOSE_VERIFIED_VERSION_PREFIX,
    )


async def _tripwire_goose_mode(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Refuse to continue a goose session that reports leaving the required mode.

    The read-back on ``session/new`` and ``session/load`` proves the mode the session
    STARTED in. It does not hold the session there: ``session/set_mode`` accepts the
    auto-approving mode, goose's own slash commands can move it, and 1.50.1 reports
    every such move as a ``current_mode_update`` on the session's own connection --
    captured live in ``session-load-live.jsonl``, where one names ``auto`` after a
    ``set_mode``. In ``auto`` goose asks about nothing, so no permission request
    reaches Crew to refuse; the notification is the last frame Crew sees before the
    harness starts saying yes on its own. So it is treated exactly as the read-back
    treats the same mode on open: the harness is stopped and the turn fails with the
    gate's own reason. Every frame that is not this one, or that names the required
    mode (goose re-emits the current mode on load), is a no-op. Gated on the same
    predicate as the read-back, so the kiro path gains no call (harness-parity H13).
    """
    if not self._is_goose:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_CURRENT_MODE:
        return
    mode_id = update.get("currentModeId")
    observed = mode_id if isinstance(mode_id, str) else ""
    issue = acp_tool_gate.seeded_setting_issue(self.backend, _scrub_observed(observed))
    if not issue:
        return
    logger.error(
        "goose routing: session reported mode %r mid-session, which is not the "
        "required one; stopping the harness before it approves on its own [session=%s]",
        _scrub_observed(observed),
        self._session_id,
    )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name="session/set_mode",
            tool_kind="other",
            outcome="denied",
            metadata={
                "reason": "goose_mode_tripwire",
                "backend": self.backend,
                "mode": _scrub_observed(observed),
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("goose routing: audit of the mode tripwire failed", exc_info=True)
    await self._kill_process(force=True)
    try:
        acp_tool_gate.enforce_runtime_routing(
            self.backend,
            issue,
            remedy=acp_tool_gate.remediation_for(self.backend),
        )
    except acp_tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from None


async def _tripwire_permission_state(self: AcpClient, msg: JsonRpcMessage) -> None:
    """The one frame-path check that a harness's own permission state has not moved.

    Each enforced harness whose asking a session-level state can turn off names that
    state in exactly one place: goose its seeded mode (``_tripwire_goose_mode``), a
    session-config harness with a ``ACP_BACKEND_FORBIDDEN_MODES`` row its permission
    option and forbidden modes (``_tripwire_session_config``). Both are membership
    tests, so every other backend -- kiro-cli included -- reads the frame and returns.
    """
    if self._is_goose:
        await self._tripwire_goose_mode(msg)
    elif self.backend in ACP_BACKEND_FORBIDDEN_MODES:
        await self._tripwire_session_config(msg)


async def _tripwire_session_config(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Stop a session-config-routed harness that reports leaving its routed state.

    The routing write (``_apply_session_permission_routing``) proves the permission
    option's value when the session opens; it cannot hold the session there if the
    harness moves itself. Copilot CLI 1.0.100 reports such a move as a
    ``config_option_update`` (``allow_all`` on) or a ``current_mode_update``
    (``autopilot``) on the session's own connection, so either is treated as the
    open-time check treats the same state: the harness is stopped and the turn fails
    with the gate's reason. A table lookup first (``ACP_BACKEND_FORBIDDEN_MODES``),
    so a harness outside it -- kiro-cli included -- gains no work (harness-parity H13).
    """
    if self.backend not in ACP_BACKEND_FORBIDDEN_MODES:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    issue = session_config_drift_issue(self.backend, params.get("update"))
    if not issue:
        return
    logger.error(
        "%s routing: %s; stopping the harness before it approves on its own [session=%s]",
        self.backend,
        issue,
        self._session_id,
    )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name="session/set_config_option",
            tool_kind="other",
            outcome="denied",
            metadata={
                "reason": "session_config_tripwire",
                "backend": self.backend,
                "issue": _scrub_observed(issue),
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("session-config tripwire: audit failed", exc_info=True)
    await self._kill_process(force=True)
    try:
        acp_tool_gate.enforce_runtime_routing(
            self.backend,
            issue,
            remedy=acp_tool_gate.remediation_for(self.backend),
        )
    except acp_tool_gate.ToolGateUnroutable as exc:
        raise AcpToolGateUnroutable(str(exc)) from None


def _drop_superseded_config_frames(self: AcpClient) -> None:
    """Before the start's drain, drop the config reports the routing answer superseded.

    The drain applies every ``config_option_update`` it finds, buffered or live. One
    buffered while the routing write was in flight still names the state the
    session was restored in, and applying it would put that stale value back over
    the write's own answer. Only those frames -- config and mode reports from before
    the answer -- are dropped; every other buffered frame (an OAuth request, an MCP
    server's status) is left for the drain, and everything read after the answer is
    applied and then checked (``_refuse_session_config_drift``). Called only for a
    member of ``ACP_BACKEND_FORBIDDEN_MODES`` (the caller tests that, H13).
    """
    floor = min(getattr(self, "_routing_answer_floor", 0), len(self._mcp_notifications))
    # Only config reports: the routing write moves ``allow_all`` and not the mode,
    # so a mode report from before its answer is still the session's mode.
    superseded = {"config_option_update"}

    def _is_superseded(msg: JsonRpcMessage) -> bool:
        update = msg.params.get("update") if isinstance(msg.params, dict) else None
        return isinstance(update, dict) and update.get("sessionUpdate") in superseded

    head = self._mcp_notifications[:floor]
    kept = [msg for msg in head if not _is_superseded(msg)]
    # The MCP report's own floor indexes into the same buffer; it falls by the
    # frames removed below it so it still marks the same frame.
    report_floor = getattr(self, "_mcp_report_frame_floor", 0)
    removed_below = sum(1 for msg in head[:report_floor] if _is_superseded(msg))
    self._mcp_notifications[:floor] = kept
    self._mcp_report_frame_floor = max(0, report_floor - removed_below)
    self._routing_answer_floor = 0


async def _refuse_session_config_drift(self: AcpClient) -> None:
    """Before a prompt, stop a harness whose last known state left its routed values.

    The in-turn readers run :func:`_tripwire_session_config` on each frame they
    read. Frames that arrive while a request outside a turn is waited on (an effort
    or model write, a compaction wait) are only buffered, and a config write's
    answer replaces the cached options without the reader seeing it -- and Copilot
    CLI reports an ``allow_all`` move only on the NEXT config write. So the cached
    options and every frame buffered since the start's drain are read here, before
    the next prompt can run a tool. Called only for a member (the caller tests it).
    """
    pending = [
        msg.params.get("update")
        for msg in self._mcp_notifications
        if isinstance(msg.params, dict) and isinstance(msg.params.get("update"), dict)
    ]
    # Mode reports the start's drain read (it applies none and clears its buffer).
    pending.extend(self._drain_mode_reports)
    self._drain_mode_reports = []
    current = {"sessionUpdate": "config_option_update", "configOptions": self._acp_config_options}
    for update in [*pending, current]:
        issue = session_config_drift_issue(self.backend, update)
        if issue:
            await self._tripwire_session_config(
                JsonRpcMessage(
                    method="session/update",
                    params={"sessionId": self._session_id, "update": update},
                )
            )


async def _tripwire_pi_gate(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Refuse to continue a gated session whose harness ran a tool the gate never saw.

    The read-back proves the gate extension LOADED; that the adapter then forwards
    its dialog for every call is the adapter's behaviour, read from its source and
    observed in the corpus, not something a client-side read can pin across
    upgrades. This is the in-band check that does not depend on it: every call the
    extension asked about left its ``toolCallId`` in the envelope, so a
    ``tool_call_update`` reaching ``completed`` for an id the gate never asked about
    is a call that ran with none of Crew's controls consulted -- whichever link
    failed (the adapter stopped honouring its own command override, stopped
    forwarding dialogs, or the extension was bypassed). The call has already run,
    so this cannot undo it; what it can do is make sure it is the LAST one: the
    harness is killed and the turn fails with a reason that names the bypass.

    Two ways to trip, one per unpinned link: a completed call the gate never
    asked about (the adapter stopped running the launcher or forwarding dialogs)
    and a completed call the host DENIED (pi stopped honouring the extension's
    block). A ``failed`` update does not trip: the harness rejects a call against
    its own schema before extension handlers run, and a call the gate DENIED is
    expected to fail -- read off the source, not assumed: pi-agent-core turns a
    handler's ``{block: true}`` into an error tool result (``agent-loop.js``,
    ``beforeResult.block`` -> ``createErrorToolResult`` with ``isError: true``)
    and pi-acp maps ``isError`` to ``status: "failed"`` on the
    ``tool_execution_end`` it forwards, so a denial reported ``completed`` is a
    harness that did not block. No-op on every session without a gate extension.

    An id's gate state lives exactly as long as its call: the terminal update,
    ``completed`` or ``failed``, consumes both the ask and any deny recorded for
    that id, because harness ids recur within a session and an approval left
    behind would vouch for a later call that merely reused the id.

    Armed for BOTH gate-extension harnesses, on the same grounds and with the
    same state. What a read-back proves is that the gate loaded in the child it
    booted; that the harness then asks for every call is its own behaviour across
    upgrades, which no client-side read pins. For the DeepSeek Harness this carries
    extra weight, because its load marker is written by the gate itself in a child
    the read-back booted: this is the check that speaks for the SESSION's own
    child, so a session whose real child composed no gate cannot complete a single
    tool call unnoticed.
    """
    if not (getattr(self, "_pi_gate_nonce", "") or getattr(self, "_deepseek_gate_nonce", "")):
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
        return
    status = update.get("status")
    if status not in ("completed", "failed"):
        return
    tool_call_id = update.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return
    # The id's gate state is CONSUMED at its terminal frame, whichever way the
    # call ended, and the verdict below is read from what was consumed. Nothing
    # makes a tool-call id unique for the life of a session (see
    # ``_note_pi_gate_asked``): a provider that mints per-response ids repeats
    # ``call_0`` on every response, so an approval that outlived its call would
    # vouch for the NEXT call wearing the same id -- one that ran with no fresh
    # ask -- and this check would wave it through. Consumed here, a reused id
    # has to earn its own ask again or it is an unasked call. CAPTURED rather
    # than asserted: ``test/fixtures/acp_frames/deepseek/tool-call-id-reuse-
    # live.jsonl`` is real dsh-acp driven by a model minting ``call_0`` in two
    # consecutive turns of one session -- the harness forwards the provider's
    # id as its ``toolCallId``, so the corpus carries ``call_0`` asked about
    # twice and ``completed`` twice, one terminal frame per call. Sound because
    # both harnesses emit exactly ONE terminal update per call: dsh-acp maps one
    # tool-result event to one ``completed``/``failed`` (``toolResultUpdate``),
    # pi-acp maps ``tool_execution_end`` the same way, and that capture shows
    # the second terminal for an id arriving only with the id's second call.
    # ``in_progress`` frames are not terminal and leave the state alone.
    asked = tool_call_id in self._pi_gate_asked_ids
    denied = tool_call_id in self._pi_gate_denied_ids
    self._pi_gate_asked_ids.discard(tool_call_id)
    self._pi_gate_denied_ids.discard(tool_call_id)
    if status != "completed":
        return
    if denied:
        # Third link: pi did not honour the extension's block. The call the host
        # refused ran anyway, which is the outcome the whole chain exists to
        # prevent, so it ends the session the same way an unasked call does.
        outcome, what = "ran_despite_deny", "after Kiro Crew's gate DENIED it"
    elif not asked:
        outcome, what = "ran_without_gate", "without asking Kiro Crew's gate"
    else:
        return
    logger.error(
        "%s gate: tool call %s COMPLETED %s; the session's tool calls are no longer "
        "governed by Kiro Crew's gate, so the harness is being stopped [session=%s]",
        acp_tool_gate.label_for(self.backend),
        tool_call_id,
        what,
        self._session_id,
    )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name=tool_call_id,
            tool_kind="other",
            outcome=outcome,
            metadata={"reason": "pi_gate_tripwire", "backend": self.backend},
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("pi gate: audit of the tripwire failed", exc_info=True)
    await self._kill_process(force=True)
    # The remedy is per harness because the unpinned link differs: pi's is the
    # adapter that must keep running Crew's launcher and forwarding dialogs, and
    # this harness's is the plugin composition its own launcher performs.
    if self._is_deepseek:
        remedy = (
            "Start a new chat, and if it recurs check the harness version: its "
            "launcher must keep applying the --patch overlay that composes the "
            "gate plugin, and its tools core must keep resolving an `ask` through "
            "its approval service."
        )
    else:
        remedy = (
            "Start a new chat, and if it recurs check the pi-acp and pi versions: "
            "the adapter must run the command named by PI_ACP_PI_COMMAND and forward "
            "extension dialogs, and pi must honour an extension's block."
        )
    raise AcpToolGateUnroutable(
        f"{acp_tool_gate.label_for(self.backend)} ran tool call {tool_call_id} {what}, "
        "so the gate extension is no longer in force for this session; the harness was "
        f"stopped. {remedy}"
    )


def _audit_spec_restriction(
    self: AcpClient, *, tool_name: str, outcome: str, reason: str, tool_kind: str = "mcp"
) -> None:
    """One SEL record shape for every decision the spec's per-tool restrictions drive.

    Four sites emit it -- the identified refusal, the unidentified-approval
    refusal on the auto-approve path, the completed-call tripwire, and the
    zero-tools refusal (whose ``tool_kind`` may be a harness-native call, not an
    MCP one) -- and a permission decision that reaches the log but not the SEL is
    one an operator cannot find later. Best-effort like every other ACP-layer
    audit: a failure to write the record must not change the decision it
    records.
    """
    try:
        # Resolved through the MODULE at call time: the file-scope ``sel`` name
        # is bound once at import, so only this attribute lookup sees an emitter
        # a test substituted on ``kiro_crew.sel``.
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name=tool_name,
            tool_kind=tool_kind,
            outcome=outcome,
            metadata={"reason": reason, "backend": self.backend},
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("session MCP: audit of a spec-restriction decision failed", exc_info=True)


async def _deny_zero_tools(self: AcpClient, event: AcpEvent) -> bool:
    """Refuse EVERY permission request when the agent spec declares ``tools: []``.

    The one refusal in this file that is not scoped to MCP calls: kiro-cli/KAS
    honour ``"tools": []`` as a total ban natively, including the harness's own
    built-in tools, but every mirrored backend (claude/opencode/goose) only
    turns ``tools`` into an MCP-server allowlist -- a native Bash/Read/Write call
    never reaches that allowlist at all, and would otherwise sail through
    ``approve_tool`` unconditionally. ``_spec_zero_tools`` is set from the same
    spec parse ``_spec_denied_tools`` is (:data:`SessionProjection.zero_tools`),
    so a spec edited between the two parses cannot ban tools in one and not the
    other.

    Runs before :meth:`_deny_spec_disabled_tool`: a zero-tool spec has nothing to
    deny per-tool (there is nothing it allows), so the narrower check would be a
    silent no-op on exactly the sessions this one exists for.

    Returns True when the request was refused (rejected on the wire and
    audited), False when :attr:`_enforces_zero_tool_ban` is not set and the caller
    decides.
    """
    if not self._enforces_zero_tool_ban:
        return False
    name = event.wire_title or event.title or "unknown"
    logger.warning(
        'session permission: refusing %r -- the agent spec declares "tools": [] '
        "and this backend does not honour that as a native-tool ban, so it is "
        "enforced at the permission request [session=%s]",
        name,
        self._session_id,
    )
    self._audit_spec_restriction(
        tool_name=name, outcome="denied", reason="spec_zero_tools", tool_kind="native"
    )
    await self.reject_tool(event.request_id)
    return True


async def _deny_spec_disabled_tool(self: AcpClient, event: AcpEvent) -> bool:
    """Refuse a codex permission request for a tool the agent spec switched off.

    The deny channel codex does not have on the wire, supplied at the one point
    this transport does offer: codex asks ``session/request_permission`` for an
    MCP tool call, and this answers it with the adapter's own reject option
    before anything runs. Returns True when the request was answered here, so
    the caller neither yields it to a consumer nor approves it.

    Identity comes from the PRECEDING ``tool_call`` frame, never from the
    permission payload: codex-acp emits the MCP call as ``rawInput = {server,
    tool, arguments}`` (``createMcpRawInput``), the client caches that dict by
    ``toolCallId``, and the permission event carries it as ``raw_tool_params``
    with ``raw_params_trusted`` set only when it came from that cache. Both
    fields are the adapter's resolution of WHICH server and tool run -- the
    model chooses a tool, it does not get to mis-report which one -- and a deny
    can only ever deny, so no further provenance is needed. The permission
    frame's own ``rawInput`` is a different shape (``serverName`` and a prose
    description) and is not consulted.

    Server names in the deny set are spelled as codex registers them, because
    ``rawInput.server`` is the registered spelling; the mirror folds them
    (:func:`~kiro_crew.providers.mirrors.codex.codex_projection`).

    A False here means "not refused BY THIS", nothing more: a cache miss, an
    untrusted params source, or a pair not in the set all return False and the
    caller decides. On the event-yielding path that is the ordinary gate and its
    human; on the auto-approve path, which has no human, ``_handle_permission``
    refuses an unidentified MCP approval itself rather than approve it blind.
    Codex's reject option for an MCP tool approval is ``cancel``, which codex-rs
    handles as a skip of THAT call with an error result to the model
    (``ReviewDecision::Abort`` in ``handle_mcp_tool_call`` ->
    ``notify_mcp_tool_call_skip``), not a turn abort. Audited as a denied
    invocation like kiro-cli's own security filter, because it is a permission
    decision a user should be able to find later.
    """
    if not self._spec_denied_tools:
        return False
    identity = _identified_mcp_call(event)
    if identity is None or identity not in self._spec_denied_tools:
        return False
    server, tool = identity
    logger.warning(
        "codex session MCP: refusing %r on %r -- the agent spec's disabledTools "
        "switches it off, and this transport has no wire channel for that "
        "restriction, so it is honoured at the permission request [session=%s]",
        tool,
        server,
        self._session_id,
    )
    self._audit_spec_restriction(
        tool_name=f"mcp__{server}__{tool}", outcome="denied", reason="spec_disabled_tool"
    )
    await self.reject_tool(event.request_id)
    return True


async def _reject_unknown_server_request(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Answer an unrecognized server→client request with -32601.

    Kiro Crew implements only ``session/request_permission`` as an inbound
    server request. Any other request (e.g. ``fs/read_text_file``,
    ``terminal/create``) has no handler, but JSON-RPC requires a response or
    the agent blocks forever. Reply ``Method not found`` so it fails fast.
    """
    if msg.id is None:
        return
    logger.warning(
        "ACP: rejecting unknown server request: method=%s id=%s",
        _loggable_request_id(msg.method),
        _loggable_request_id(msg.id),
    )
    await self._send_error(msg.id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {msg.method}")


def _note_pi_gate_asked(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Remember the tool call a gate-extension permission frame is asking about.

    The tripwire (:meth:`_tripwire_pi_gate`) trusts a completed call only when
    the gate asked about it first, so this must run on EVERY path that answers a
    permission frame -- the streaming dispatch, which builds the event
    unconditionally, and the auto-approve site, which builds one only under a
    spec deny set. Idempotent, and a no-op without a session nonce.

    Two shapes, one tripwire. pi's adapter can only forward a generic confirm
    DIALOG, so the real call rides inside it as the envelope Crew's extension
    wrote and the id is read back out of that. The DeepSeek Harness emits an
    ordinary ACP permission frame that names the call directly, so its id is read
    off ``toolCall.toolCallId``. Deliberately the same method rather than a second
    one per harness: the state it feeds and the refusal it arms are shared, so a
    future fix to either lands in both.

    A fresh ask is a fresh verdict, so any earlier DENY for the same id is dropped
    here. Nothing makes a tool-call id unique for the life of a session: the id is
    the harness's own, and an OpenAI-compatible provider that mints per-response
    ids (``call_0``, ``call_1``) repeats them on every response -- captured live
    in ``test/fixtures/acp_frames/deepseek/tool-call-id-reuse-live.jsonl``, where
    dsh-acp emits ``call_0`` for two consecutive turns of one session, each with
    its own permission frame and its own terminal update. Without the discard a single denial would arm
    :meth:`_tripwire_pi_gate` permanently, so the next APPROVED call reusing that
    id would report ``completed`` and be killed as ``ran_despite_deny`` -- a
    healthy session ended on stale state; a new denial re-arms it through
    :meth:`_note_pi_gate_denied`. Rejected alternative: turn-scoping this state by
    clearing ``_pi_gate_asked_ids`` at the prompt boundary, because a ``completed``
    frame arriving after that boundary would then read as ``ran_without_gate`` and
    kill a healthy session for the symmetric reason. What scopes the state instead
    is the call itself: the tripwire consumes an id's ask and deny at that call's
    terminal frame, so a reused id must be asked about afresh before its next
    ``completed`` is trusted. The third link is untouched: a denied id that
    completes with NO fresh frame still trips ``ran_despite_deny``.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    tool_call = params.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    tool_call_id = ""
    pi_nonce = getattr(self, "_pi_gate_nonce", "")
    if pi_nonce:
        envelope = gate_envelope(tool_call, pi_nonce)
        if envelope is not None and envelope["toolCallId"]:
            tool_call_id = envelope["toolCallId"]
    elif getattr(self, "_deepseek_gate_nonce", ""):
        candidate = tool_call.get("toolCallId")
        if isinstance(candidate, str):
            tool_call_id = candidate
    if not tool_call_id:
        return
    self._pi_gate_asked_ids.add(tool_call_id)
    self._pi_gate_denied_ids.discard(tool_call_id)
    if msg.id is not None:
        self._pi_gate_request_tool[str(msg.id)] = tool_call_id


def _build_permission_event(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """Build one permission event through the transport-shared parser.

    The legacy direct client owns the same provenance caches as the shared
    runtime. Routing them through one parser keeps a cached ``False`` shell
    classification distinguishable from a cache miss and preserves cached
    raw parameters across repeated permission frames for the same tool call.
    """
    restore = None
    if self._is_copilot:
        self._confirm_copilot_mcp_identity(msg)
        restore = self._seed_copilot_edit_targets(msg)
    # Compat-shaped like the caches below: an instance built without ``__init__``
    # has no nonce, and no nonce means no dialog is ever read as a gate envelope.
    _gate_nonce = getattr(self, "_pi_gate_nonce", "")
    try:
        event, recorded = build_permission_event(
            msg,
            tool_input_cache=self._tool_call_inputs,
            # ``AcpClient`` predates this same-key provenance cache.  Normal
            # instances initialize it in __init__, while legacy/minimal
            # constructions (including embedders that allocate with __new__)
            # may not.  The shared builder treats a missing map/key as
            # redacted/unknown, so this compatibility fallback stays
            # fail-closed for durable trust instead of inventing provenance.
            tool_input_redacted_cache=getattr(self, "_tool_call_input_redacted", None),
            shell_cache=self._tool_call_is_shell,
            raw_params_cache=self._tool_call_params,
            # Same compatibility shape as the redaction map above.
            diff_path_cache=getattr(self, "_tool_call_diff_path", None),
            mcp_server_name_cache=self._tool_call_mcp_server,
            tool_name_cache=self._tool_call_tool_name,
            harness_tool_name_cache=getattr(self, "_tool_call_harness_tool_name", None),
            # Handed only on the KAS harness, the same gate as the kind carry (H13).
            identity_unreadable_cache=(
                getattr(self, "_tool_call_identity_unreadable", None)
                if self.backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL
                else None
            ),
            # Set only for a session running Kiro Crew's gate extension, whose
            # dialogs carry the nonce this spawn issued; ``None`` everywhere else,
            # so no other harness's permission frame is ever read as an envelope.
            # Same compatibility shape as the maps above: an instance built without
            # ``__init__`` has no nonce, and no nonce means no envelope is trusted.
            gate_envelope_nonce=_gate_nonce or None,
            kas_consent_meta=self.backend == ACP_BACKEND_KAS,
            harness_backend=self.backend,
            harness_mcp_servers=self._harness_mcp_server_names(),
            side_read_only=getattr(self, "_side_read_only", False),
            deepseek_read_only_issue=getattr(
                self, "_deepseek_read_only_issue", "the harness composition was not read back"
            ),
        )
    finally:
        # The event holds its own copy of the seeded targets; none is retained.
        if restore is not None:
            self._restore_copilot_edit_seed(restore)
    if event is None:
        return None
    if recorded is not None:
        self._permission_options[event.request_id] = recorded
    # Created here when absent, so a client allocated without ``__init__``
    # that builds an event still records it for approve_tool's floor.
    _gate_events = getattr(self, "_permission_gate_events", None)
    if _gate_events is None:
        _gate_events = self._permission_gate_events = {}
    _gate_events[event.request_id] = event
    self._note_pi_gate_asked(msg)
    logger.info(
        "Permission requested for tool: %s (req=%s)",
        event.title,
        _loggable_request_id(event.request_id),
    )
    if logger.isEnabledFor(logging.DEBUG):
        params = msg.params if isinstance(msg.params, dict) else {}
        tool_call = params.get("toolCall", {})
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        redacted_payload = repr(tool_call)
        redacted_payload, _ = redact_exfiltration_urls(redacted_payload)
        redacted_payload, _ = redact_credentials(redacted_payload)
        logger.debug("Permission toolCall payload: %s", redacted_payload)
    return event
