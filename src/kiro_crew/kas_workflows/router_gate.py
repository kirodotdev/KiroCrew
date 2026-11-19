"""The KAS workflow router's step judgement: one identity, the chat turn's checks.

Every decision on a workflow step -- admission, a Trust or Trust reads
auto-approve, an "Always" grant's use, the mode-change sweep, a plan handoff and
a person's answer -- is made on ONE identity event, derived the way a chat turn
derives it for the same engine (:mod:`kiro_crew.kas_workflows.step_identity`),
and runs the checks a chat turn runs for it: the deny-rule screen on its command,
the title and tool-input screen, the tool gate (``HookManager.on_tool_call``),
the PreToolUse hooks and a moved Bot's held rules (:mod:`kiro_crew.kas_workflows.
policy`). An identity that cannot be derived refuses.

Each function here whose first parameter is ``self`` is a
:class:`~kiro_crew.kas_workflows.router.KasWorkflowRouter` method the class binds
by name. Nothing here redacts: every text that leaves is built in ``router.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Mapping

from kiro_crew.kas_workflows import router_answers as _r_answers
from kiro_crew.kas_workflows import router_base
from kiro_crew.kas_workflows.router_base import PendingApproval

if TYPE_CHECKING:
    from kiro_crew.kas_workflows.router import KasWorkflowRouter

logger = logging.getLogger("kiro_crew.kas_workflows.router")

_SESSION_UPDATE = "session/update"


def _observe_step_update(
    self: KasWorkflowRouter, method: str, params: Mapping[str, Any], runtime: Any = None
) -> None:
    """Feed a step session's ``session/update`` to the identity caches. On the reader loop.

    Only a frame from the runtime that drives that step (the one its ``node_start``
    came on, as ``_claims_step`` requires of its requests) is read.
    """
    if method != _SESSION_UPDATE or runtime is None:
        return
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return
    # The O(1) route check first: this runs on every chat's streamed chunk too.
    owner = self._step_runtime.get(session_id)
    if owner is None or owner() is None or owner() is not runtime():
        return
    if self.mirror.run_for_step_session(session_id) is None:
        return
    # A turn_start here never clears a Stop: it may be the stopped turn's,
    # still buffered on the pipe (only Crew's re-drive clears it).
    self._step_calls.observe(session_id, params.get("update"))


def _identity_now(
    self: KasWorkflowRouter, params: Mapping[str, Any], *, plan: bool = False
) -> tuple[Any, str | None]:
    """The step's identity event from the caches as they stand: ``(event, refusal)``.

    Synchronous, so the router reads it in frame order, when the request
    arrives. A builder that raises refuses.
    """
    try:
        return self._identity(params, plan=plan)
    except Exception:  # noqa: BLE001 -- an identity that cannot be read refuses
        logger.warning("workflow step identity failed; rejecting", exc_info=True)
        return None, "the step's identity could not be read"


async def _step_refusal(
    self: KasWorkflowRouter,
    params: Mapping[str, Any] | None,
    owner: tuple[str, str],
    *,
    plan: bool = False,
    event: Any = None,
    fire_hooks: bool = True,
    identity: tuple[Any, str | None] | None = None,
) -> tuple[str | None, bool, Any]:
    """Judge a step's request: ``(refusal, held, identity event)``.

    The identity first (*identity*, read when the request arrived; else built
    here from the reader's caches, unless *event* is the one admission built),
    then the input screen, then the tool
    gate. A refusal rejects the request; ``held`` keeps it for a person. A check
    that raises refuses.
    """
    if event is None:
        if params is None:
            return "identity: the step's request was not kept", False, None
        event, why = identity if identity is not None else _identity_now(self, params, plan=plan)
        if event is None:
            return f"identity: {why}", False, None
    try:
        reason = await asyncio.to_thread(self._input_screen, event, params)
    except Exception:  # noqa: BLE001 -- a screen that cannot run refuses
        logger.warning("workflow step input screen failed; rejecting", exc_info=True)
        reason = "the tool-input screen could not run"
    if reason:
        return f"input screen: {reason}", False, event
    try:
        verdict = await self._gate(event, self.slot_of(owner[0]) or "", fire_hooks=fire_hooks)
    except Exception:  # noqa: BLE001 -- a gate that cannot run refuses
        logger.warning("workflow step tool gate failed; rejecting", exc_info=True)
        return "tool gate: the tool gate could not run", False, event
    if verdict.refusal:
        return f"tool gate: {verdict.refusal}", False, event
    return None, verdict.held, event


async def _recheck_refusal(
    self: KasWorkflowRouter, approval: PendingApproval
) -> tuple[str | None, bool]:
    """Every admission check again on a held step, before the sweep or a person allows it.

    The operator's rules may have changed since it was held. The identity event
    admission built is judged again (its caches may be gone by now); the
    PreToolUse hooks are not fired a second time. A held step that kept no event
    cannot be judged, and refuses.
    """
    if approval.gate_event is None:
        return "identity: the step's identity was not kept", False
    command = approval.raw_command
    if command is not None:
        try:
            reason = await asyncio.to_thread(self._screen, command)
        except Exception:  # noqa: BLE001 -- a screen that cannot run refuses
            logger.warning("workflow step screen failed; rejecting", exc_info=True)
            reason = "the deny-rule screen could not run"
        if reason:
            return f"deny rule: {reason}", False
    refusal, held, _event = await self._step_refusal(
        approval.raw_params,
        (approval.workflow_id, approval.node_id),
        plan=approval.is_plan,
        event=approval.gate_event,
        fire_hooks=False,
    )
    return refusal, held


_event_command = router_base._event_command


async def _names_granted(self: KasWorkflowRouter, command: str | None, event: Any) -> bool:
    """The name-grant check on every command the step names: the request's and its event's.

    False when either is refused or a check cannot run (an auto-approve then asks).
    """
    for text in {c for c in (command, _event_command(event)) if c}:
        try:
            if await self._name_grant(text) is not None:
                return False
        except Exception:  # noqa: BLE001 -- a check that cannot run asks
            logger.debug("name-grant check failed for a workflow step", exc_info=True)
            return False
    return True


_NOTE_SEVERITIES = frozenset({"info", "success", "warning", "error"})


def _note_severity(value: Any) -> str:
    """A step note's severity, one of the known values (else ``info``)."""
    return value if isinstance(value, str) and value in _NOTE_SEVERITIES else "info"


def _held_params(approval: PendingApproval) -> dict[str, Any]:
    """The request shape a held step's refusal is built from: its title and options."""
    return {
        **dict(approval.raw_params or {}),
        "toolCall": {"title": approval.title},
        "options": [dict(o) for o in approval.options],
    }


async def _audit_person_answer(
    self: KasWorkflowRouter,
    approval: PendingApproval,
    allowed: bool,
    option_id: str,
    tool_name: str,
    node_id: str | None = None,
) -> None:
    """Write a person's answer to the security event log, as the chat path does.

    *tool_name* and *option_id* are already redacted and capped (``router.py``
    builds them: every redactor call
    stays in the module the security posture registry names). An allow is
    audit-or-ask: a log that cannot be written raises, and the caller leaves the
    step held. A reject is sent whatever the log does.
    """
    fields = {
        "session_key": self.slot_of(approval.workflow_id) or "kas-workflow",
        "agent": "kas-workflow",
        "source": "dashboard",
        "tool_name": tool_name,
        "tool_kind": "workflow_step",
        "outcome": "approved" if allowed else "rejected",
        "request_id": _r_answers.log_id(approval.request_id),
        "metadata": {
            "reason": "person",
            "optionId": option_id,
            "workflowId": approval.workflow_id,
            "nodeId": node_id,
        },
    }
    if allowed:
        await asyncio.to_thread(self._audit, critical=True, **fields)
        return
    try:
        await asyncio.to_thread(self._audit, **fields)
    except Exception:  # noqa: BLE001 -- a reject goes out regardless
        logger.warning("workflow step rejection not logged", exc_info=True)


def _current_mode(self: KasWorkflowRouter, workflow_id: str) -> str | None:
    """The owning chat's approval mode now, or None when it cannot be read."""
    slot_key = self.slot_of(workflow_id)
    reader = self._slot_mode.get(slot_key) if slot_key else None
    if reader is None:
        return None
    try:
        return reader()[0]
    except Exception:  # noqa: BLE001
        return None


def _record_step_grant(self: KasWorkflowRouter, session_id: str, grant_key: str) -> None:
    """Record a person's "Always" for *grant_key* in this step session, bounded."""
    from kiro_crew.kas_workflows.router_base import MAX_GRANT_SESSIONS, MAX_GRANTS_PER_STEP

    keys = self._step_grants.pop(session_id, set())
    if len(keys) < MAX_GRANTS_PER_STEP:
        keys.add(grant_key)
    self._step_grants[session_id] = keys
    while len(self._step_grants) > MAX_GRANT_SESSIONS:
        self._step_grants.pop(next(iter(self._step_grants)))


#: Longest identity-event title a held step keeps for its re-checks. The builder
#: redacts but does not cap a title, so a longer one keeps no event, and the
#: step's sweep or a person's allow then refuses it (``_recheck_refusal``).
MAX_RETAINED_TITLE_CHARS = 64 * 1024


def _retained_event(event: Any) -> Any:
    """The part of a held step's identity event its re-checks read, bounded; or None.

    Re-checks read the identity (title, kind, tool name, MCP pair, shell flags),
    the tool input and the raw params; never the offered options (the held
    ``PendingApproval.options`` is their bounded copy), the stated purpose or the
    wire title, so those are dropped. Every other string field is measured, not
    cut: one over :data:`MAX_RETAINED_TITLE_CHARS`, or an input or params over the
    tool-call cache's own bound (``TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS``), keeps
    nothing.
    """
    import dataclasses
    import json

    from kiro_crew.kas_workflows.step_identity import payload_bound

    PAYLOAD = payload_bound()

    if event is None:
        return None
    kept = dataclasses.replace(event, options=[], tool_purpose="", wire_title="", text="")
    for spec in dataclasses.fields(kept):
        value = getattr(kept, spec.name)
        limit = PAYLOAD if spec.name == "tool_input" else MAX_RETAINED_TITLE_CHARS
        if isinstance(value, str) and len(value) > limit:
            return None
    params = kept.raw_tool_params
    try:
        if params is not None and len(json.dumps(params, default=str)) > PAYLOAD:
            return None
    except (TypeError, ValueError):
        return None
    return kept


async def recheck_pending(self: KasWorkflowRouter) -> int:
    """Re-decide held approvals under their chats' CURRENT approval mode.

    A step that asked while its chat was in Normal waits for a person. If
    the person then switches the chat to Trust, that held step must follow
    the switch like every later step does, not keep waiting. Called from the
    dashboard's state poll, so a mode change lands within one poll. Returns
    how many were answered here.
    """
    answered = 0
    for stale in [k for k in self._decided_under if k not in self._pending]:
        self._decided_under.pop(stale, None)
    for key in list(self._pending):
        approval = self._pending.get(key)
        if approval is None or approval.is_plan or approval.command_unreadable:
            continue
        if approval.gate_held or key in self._answering:
            continue  # a held rule covers it, or a person is answering it
        mode = self._current_mode(approval.workflow_id)
        if mode == self._decided_under.get(key):
            continue  # nothing changed since this one was last decided
        self._decided_under[key] = mode
        refusal, held = await self._recheck_refusal(approval)
        if self._pending.get(key) is not approval or key in self._answering:
            # Answered, or a person began answering it, meanwhile: not
            # decided here, so a later sweep may still decide it.
            self._decided_under.pop(key, None)
            continue
        if held and not refusal:
            continue
        if refusal:
            self._pending.pop(key, None)
            self._decided_under.pop(key, None)
            self.revision += 1
            self._retire(key)
            await self._reject_unheld(
                approval.claim,
                _held_params(approval),
                (approval.workflow_id, approval.node_id),
                refusal,
                approval.raw_command,
            )
            continue
        self._pending.pop(key, None)
        self._sweeping[key] = approval
        ok = False
        try:
            # Its answer goes out only under its own claim (router_answers).
            ok = await self._auto_approve(
                approval.claim,
                (approval.workflow_id, approval.node_id),
                approval.raw_command,
                approval.options,
                approval.title,  # redacted before its cap, as every audit's is
                approval.gate_event,
            )
        finally:
            # Gone from _sweeping: its session's asks were withdrawn meanwhile.
            withdrawn = self._sweeping.pop(key, None) is None
            if ok:
                answered += 1
                self.revision += 1
                self._decided_under.pop(key, None)
                self._retire(key)
            elif not withdrawn and key not in self._pending:
                self._pending[key] = approval
    return answered
