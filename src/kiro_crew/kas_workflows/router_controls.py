"""The KAS workflow router's run, step and recovery controls.

Each maps one dashboard action onto the KAS verb that does it, on the process that
drives the run. Each function here whose first parameter is ``self`` is a
:class:`~kiro_crew.kas_workflows.router.KasWorkflowRouter` method: the class binds it
by name (``retry = _r_controls.retry``), so it reads and writes the router's own state,
and a patch of the method on the class or on an instance reaches every caller. Nothing
here redacts: ``end_step``, whose reply carries KAS's text, stays in ``router.py``, the
module the security posture registry names as the sink.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.router_base import (
    _VERBS,
    CONTROL_METHODS,
    KasWorkflowError,
    StepNotStarted,
    _alive,
)

if TYPE_CHECKING:
    from kiro_crew.kas_workflows.router import KasWorkflowRouter


async def control(
    self: KasWorkflowRouter, workflow_id: str, action: str, reason: str | None = None
) -> None:
    method = CONTROL_METHODS.get(action)
    if method is None:
        raise KasWorkflowError(f"Unknown action {action!r}.")
    try:
        params = wire.user_control_params(workflow_id, reason)
    except ValueError as exc:
        raise KasWorkflowError(str(exc)) from exc
    runtime = _alive(self._run_runtime.get(workflow_id))
    if runtime is None:
        status = self.mirror.status(workflow_id)
        if action != "delete" or status not in wire.TERMINAL_RUN_STATUSES:
            raise KasWorkflowError(
                "This run's chat is not connected to Kiro Agent right now. "
                "Open the run's chat, then try again."
            )
        # A finished run has no driver to race; any process may delete it.
        runtime = await self._reader()
    self._require(runtime, method, _VERBS.get(action, action))
    await self._call(runtime, method, params)
    if action == "delete":
        self.mirror.forget(workflow_id)
        self._run_runtime.pop(workflow_id, None)
        if self._ledger.pop(workflow_id, None) is not None:
            await self._save_ledger()
        for key in [k for k, a in self._pending.items() if a.workflow_id == workflow_id]:
            self._pending.pop(key, None)
            self._retire(key)
        for key in [k for k, q in self._questions.items() if q.workflow_id == workflow_id]:
            self._questions.pop(key, None)
            self._retire(key)
    self.revision += 1


def _driver(self: KasWorkflowRouter, workflow_id: str) -> Any:
    runtime = _alive(self._run_runtime.get(workflow_id))
    if runtime is None:
        raise KasWorkflowError(self._NOT_CONNECTED)
    return runtime


def _finished_runtime(self: KasWorkflowRouter, workflow_id: str) -> Any:
    """The process that may re-drive a finished run, or None.

    Its last driver while that process lives; otherwise the process of the
    run's chat, but only while the chat's session is still the run's
    parent, since a step's permissions come from that session.
    """
    live = _alive(self._run_runtime.get(workflow_id))
    if live is not None:
        return live
    slot_key = self.slot_of(workflow_id)
    held = self._slot_binding.get(slot_key) if slot_key else None
    parent = self.mirror.parent_of(workflow_id)
    if held is None or parent is None or held[1] != parent:
        return None
    return _alive(held[0])


async def _redriven(self: KasWorkflowRouter, workflow_id: str, runtime: Any) -> None:
    """A finished run is running again on *runtime*: it is live in every record."""
    self._route_run(workflow_id, weakref.ref(runtime))
    self._observed_runs.pop(workflow_id, None)
    self._inspected.discard(workflow_id)
    entry = self._ledger.get(workflow_id)
    if entry is not None and entry.get("ended"):
        entry["ended"] = False
        await self._save_ledger()
    self.revision += 1


async def retry(
    self: KasWorkflowRouter, workflow_id: str, node_id: str | None = None, path: str | None = None
) -> list[str]:
    """Retry every failed/aborted step of a finished run, or one (KAS ``retry``).

    KAS resets the step(s) and their containers and drives the run again.
    It names a step by id alone and resolves the FIRST instance, so a step
    inside a loop iteration is retried only with all failed steps.
    """
    status = self.mirror.status(workflow_id)
    if status not in (wire.STATUS_FAILED, wire.STATUS_ABORTED):
        raise KasWorkflowError("Only a failed or stopped run can be retried.")
    if path:
        if any(
            seg.startswith(wire.ITERATION_SEGMENT_PREFIX)
            for seg in path.strip(wire.NODE_PATH_SEP).split(wire.NODE_PATH_SEP)
        ):
            raise KasWorkflowError(
                "A step inside a loop can only be retried together with every failed step."
            )
        known = self.mirror.node_ids_at(workflow_id, path)
        if known is not None and node_id is not None and known != node_id:
            raise KasWorkflowError("That path is not this step.")
        node_id = node_id or known
    if node_id is not None and self.mirror.instance_status(workflow_id, node_id, path) not in (
        wire.STATUS_FAILED,
        wire.STATUS_ABORTED,
    ):
        raise KasWorkflowError("Only a failed or stopped step can be retried.")
    runtime = self._finished_runtime(workflow_id)
    if runtime is None:
        raise KasWorkflowError(self._NOT_CONNECTED)
    try:
        params = wire.retry_params(workflow_id, node_id)
    except ValueError as exc:
        raise KasWorkflowError(str(exc)) from exc
    self._require(runtime, wire.METHOD_RETRY, "retry")
    response = await self._call(runtime, wire.METHOD_RETRY, params)
    await self._redriven(workflow_id, runtime)
    return [n for n in wire.as_list(response.get("retriedNodeIds")) if isinstance(n, str)]


async def restart(self: KasWorkflowRouter, workflow_id: str) -> None:
    """Run a finished run again from scratch (KAS ``invoke`` on a terminal run)."""
    if self.mirror.status(workflow_id) not in wire.TERMINAL_RUN_STATUSES:
        raise KasWorkflowError("Only a finished run can be run again.")
    runtime = self._finished_runtime(workflow_id)
    if runtime is None:
        raise KasWorkflowError(self._NOT_CONNECTED)
    self._require(runtime, wire.METHOD_INVOKE, "running a workflow again")
    await self._call(runtime, wire.METHOD_INVOKE, {"workflowId": workflow_id})
    await self._redriven(workflow_id, runtime)


async def grant(
    self: KasWorkflowRouter,
    workflow_id: str,
    node_id: str,
    iterations: int,
    reason: str | None = None,
) -> None:
    """Grant an exhausted loop more passes and drive it (KAS ``resume`` + ``extendRepeat``).

    Done iterations are kept; KAS re-checks the stop condition first and
    refuses a budget past its cap.
    """
    try:
        params = wire.extend_repeat_params(workflow_id, node_id, iterations, reason)
    except ValueError as exc:
        raise KasWorkflowError(str(exc)) from exc
    driver = self._driver(workflow_id)
    self._require(driver, wire.METHOD_RESUME, "granting more passes")
    await self._call(driver, wire.METHOD_RESUME, params)
    self.revision += 1


async def cancel_step(
    self: KasWorkflowRouter, workflow_id: str, node_id: str, path: str | None = None
) -> None:
    """Stop one running step (``session/cancel`` on its own session).

    KAS parks that step ``paused`` ("stopped by user"), keeps its session,
    and lets parallel siblings run on; the run settles paused once they
    finish, and Resume or a reply re-drives the step. Any ask still open on
    the step's session is answered ``cancelled``, as ACP requires.
    """
    if path:
        session_id = self.mirror.step_session_at(workflow_id, path)
    else:
        session_id = self.mirror.step_session(workflow_id, node_id)
    if session_id is None:
        raise StepNotStarted("This step has not started.")
    if self.mirror.instance_status(workflow_id, node_id, path) != wire.STATUS_RUNNING:
        raise KasWorkflowError("This step is not running.")
    runtime = self._driver(workflow_id)
    try:
        await runtime.send_notification(wire.METHOD_SESSION_CANCEL, {"sessionId": session_id})
    except Exception as exc:  # noqa: BLE001 -- a dead pipe is the run's state to report
        raise KasWorkflowError(f"Kiro Agent did not take the stop: {exc}") from exc
    await self._withdraw_session_asks(session_id)
    self.revision += 1


async def reply_to_step(
    self: KasWorkflowRouter, workflow_id: str, node_id: str, path: str | None, text: str
) -> None:
    """Send the person's *text* to a step parked for their next message.

    KAS has no special method for this: a ``session/prompt`` on the step's
    own session is recognised as the paused step's continuation, answered
    ``end_turn`` at once, and the step's turn runs under the workflow. Only
    a step KAS lists as waiting for a reply is sent one (a running step
    would take the prompt as an unrelated chat turn), and only on the
    process driving the run. A step session that process does not hold now
    (it restarted) is loaded first, as KAS's guide prescribes.
    """
    body = (text or "").strip()
    if not body:
        raise KasWorkflowError("Write a reply first.")
    if len(body) > wire.MAX_STEP_REPLY_CHARS:
        raise KasWorkflowError("That reply is too long.")
    if path:
        session_id = self.mirror.step_session_at(workflow_id, path)
    else:
        session_id = self.mirror.step_session(workflow_id, node_id)
    if session_id is None:
        raise StepNotStarted("This step has not started.")
    if not self.mirror.step_awaits_reply(workflow_id, session_id):
        raise KasWorkflowError("This step is not waiting for a reply.")
    runtime = _alive(self._run_runtime.get(workflow_id))
    if runtime is None:
        raise KasWorkflowError(
            "This run's chat is not connected to Kiro Agent right now. "
            "Open the run's chat, then try again."
        )
    # One reply per parked step at a time. The waiting check above reads the
    # mirror, which moves only when KAS reports the step running again, so a
    # second reply (a double send, another tab) would pass it too and reach
    # KAS as an unrelated chat turn on the step's session.
    if session_id in self._replies_in_flight:
        raise KasWorkflowError("A reply to this step is already being sent.")
    self._replies_in_flight.add(session_id)
    try:
        await self._send_step_reply(workflow_id, session_id, runtime, body)
    finally:
        self._replies_in_flight.discard(session_id)
    self.revision += 1


async def _send_step_reply(
    self: KasWorkflowRouter, workflow_id: str, session_id: str, runtime: Any, body: str
) -> None:
    prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": body}]}
    try:
        await self._call(runtime, wire.METHOD_SESSION_PROMPT, prompt)
    except KasWorkflowError:
        cwd = self._step_cwd(workflow_id, runtime)
        if not cwd:
            raise
        # A load replays the step's open requests (a plan approval is asked
        # again); they must reach this router, not the runtime's auto-reject.
        self._route_step(session_id, weakref.ref(runtime))
        await self._call(
            runtime,
            wire.METHOD_SESSION_LOAD,
            {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
        )
        await self._call(runtime, wire.METHOD_SESSION_PROMPT, prompt)
