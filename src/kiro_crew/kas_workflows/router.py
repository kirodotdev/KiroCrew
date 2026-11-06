"""The gateway's router for KAS's own workflow engine.

Every run has a VISIBLE parent: the dashboard chat that launched it. KAS requires
a parent session for every run and, when the run ends, wakes that parent with a
model turn ("A workflow you launched … completed"). With a real chat as the
parent that turn is the run's summary, in a conversation the user can continue,
instead of a hidden turn nobody reads.

So a run is started from INSIDE its chat's turn pipeline, on the chat's own KAS
process (:func:`launch_from_turn`): the dashboard opens a chat, registers a
pending launch here (:meth:`KasWorkflowRouter.prepare_launch`) and sends the
chat a launch message; the turn that message starts claims the chat's session,
and this module sends ``_kiro/workflow/new`` + ``invoke`` on that process with
the chat's ACP session as the parent. No model turn runs for the launch itself.

When the run ends, KAS's wake turn on the chat's session is started by KAS, not
by a prompt, so nothing in the chat would read it. On the run's ``run_complete``
the router sends the chat an OBSERVE message (:meth:`KasWorkflowRouter.prepare_observe`,
single-use like a launch's); that turn claims the session and, instead of
prompting, reads the engine's turn through the ordinary chat-turn pipeline
(:func:`observe_from_turn`, :meth:`~kiro_crew.acp.session_handle.AcpSessionHandle.observe_turn`),
so its reply shows and its approvals follow the chat's mode.

Every KAS runtime carries a sink bound to this router
(:func:`kiro_crew.acp.workflow_sink.sink_for`), so whichever process a run lives
on, its notifications reach the one :class:`~kiro_crew.kas_workflows.mirror.RunMirror`
every surface reads, and its steps' permission prompts reach the dashboard.

Step approvals fail closed. A request is held for a person only after its
command (if it has one) passed the operator's deny rules
(:mod:`kiro_crew.kas_workflows.policy`); a deny match, a full queue, a dead
runtime or a step this router cannot place is answered with the request's own
reject option.

A step that passed that screen follows the approval mode of the chat that owns
its run, exactly as one of that chat's own tool calls would: under Trust (or a
process-wide override) it is allowed, under Trust reads only a read-only shell
command is, and under Normal it waits for a person. An auto-approval selects the
request's ALLOW-ONCE option, never an always-allow one, so nothing persists in
KAS past the call; it also passes the same name-grant check every Crew
auto-approve tier runs, and it is written to the security event log. A step
whose chat is unknown (no turn has named it yet) waits for a person.

Reads that need no parent (the recipe catalog, a project's run history) go to
any live KAS process, or to a session-less query process spawned for them. A run
is never ``load``-ed or resumed from a process that does not own it: each KAS
process keeps its own in-memory run registry, and two processes driving one run
is the failure this rule exists to prevent. The one exception is boot recovery's
ownership check (:meth:`KasWorkflowRouter.recover_on_boot`), which asks the query
process to ``load`` a run it has never held: KAS serves that from disk,
read-only, registering and claiming nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from kiro_crew._facade_bindings import own_methods
from kiro_crew.kas_workflows import asks, policy
from kiro_crew.kas_workflows import router_controls as _r_controls
from kiro_crew.kas_workflows import router_reads as _r_reads
from kiro_crew.kas_workflows import router_recovery as _r_recovery
from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.mirror import (
    MAX_LIVE_RUNS,
    MAX_RUN_ID_CHARS,
    MAX_TERMINAL_RUNS,
    RunMirror,
)

# Every name router_base defines, so ``router.<name>`` keeps resolving for the
# callers and tests that read it there.
from kiro_crew.kas_workflows.router_base import (  # noqa: F401
    _LAUNCH_RE,
    _MAX_DISPLAY_CHARS,
    _MAX_PLAN_CHARS,
    _NOTE_VERBS,
    _OBSERVE_RE,
    _OBSERVE_VERBS,
    _OFFERED_KINDS,
    _RECONNECT_RE,
    _VERBS,
    BOOT_RECOVERY_CONCURRENCY,
    BOOT_RECOVERY_MAX_CHATS,
    BOOT_RECOVERY_SLACK_SECS,
    BOOT_RECOVERY_TURN_SECS,
    CONTROL_METHODS,
    LAUNCH_TTL_SECS,
    LEDGER_MAX_RUNS,
    MAX_DEFINITION_BYTES,
    MAX_GRANT_SESSIONS,
    MAX_GRANTS_PER_STEP,
    MAX_OBSERVED_RUNS,
    MAX_PATHS_PER_DEFINITION,
    MAX_PENDING_APPROVALS,
    MAX_PENDING_LAUNCHES,
    MAX_PENDING_OBSERVES,
    MAX_PROMPT_CHARS,
    METHOD_REQUEST_PERMISSION,
    METHOD_USER_INPUT,
    MODE_NORMAL,
    MODE_TRUST,
    MODE_TRUST_READS,
    OWNER_ABANDON_SECS,
    RECONNECT_INTERVAL_SECS,
    RECONNECT_MAX_TRIES,
    RECONNECT_REASON,
    STALE_RUNNING_PAUSE_REASON,
    DashboardHooks,
    KasRuntime,
    KasWorkflowError,
    ModeReader,
    PendingApproval,
    PendingLaunch,
    RuntimeFactory,
    StepNotStarted,
    TurnStarter,
    _alive,
    _allow_once,
    _audit_auto_approval,
    _duration_secs,
    _gate_node_actions,
    _is_plan_approval,
    _kept_plan_waits,
    _name_grant_refusal,
    _node_label,
    _now_iso,
    _offered_options,
    _option_grant,
    _parse_iso,
    _plan_meta,
    _project,
    _read_definition,
    _read_only,
    _reject_result,
    _RuntimeSink,
    _step_grant_key,
    _strip_node_actions,
    _tool_call_text,
    session_binding,
)

logger = logging.getLogger(__name__)


#: Chats whose KAS session id the router remembers for adopting model-started runs.
MAX_PARENT_SESSIONS = 1024

#: Runs and step sessions the router keeps a route for: no more than the mirror
#: holds (:meth:`KasWorkflowRouter._prune_routes`).
MAX_ROUTED_RUNS = MAX_LIVE_RUNS + MAX_TERMINAL_RUNS
MAX_ROUTED_STEPS = 4 * MAX_ROUTED_RUNS

#: Inputs a launch may carry, and the longest launch field (cwd, label, model,
#: effort, an input's name or value). Refused past either, never cut.
MAX_LAUNCH_INPUTS = 64
_MAX_LAUNCH_FIELD_CHARS = 4096


def _redact(text: str) -> str:
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text[: _MAX_DISPLAY_CHARS * 2])[:_MAX_DISPLAY_CHARS]


def _redact_plan(text: str) -> str:
    """A plan's text for the card: redacted, and capped higher than a title."""
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text[: _MAX_PLAN_CHARS * 2])[:_MAX_PLAN_CHARS]


class KasWorkflowRouter:
    """Routes KAS workflow traffic for the dashboard. See the module docstring."""

    def __init__(
        self,
        query_runtime_factory: RuntimeFactory,
        *,
        mirror: RunMirror | None = None,
        screen: Callable[[str], str | None] = policy.deny_reason,
        clock: Callable[[], float] = time.time,
        max_pending: int = MAX_PENDING_APPROVALS,
        request_timeout: float = 30.0,
        ledger_path: Path | None = None,
        name_grant: Callable[[str], Awaitable[Any]] = _name_grant_refusal,
        read_only: Callable[[str], bool] = _read_only,
        audit: Callable[..., None] = _audit_auto_approval,
    ) -> None:
        self._query_factory = query_runtime_factory
        self.mirror = mirror or RunMirror(clock=clock)
        self._screen = screen
        self._name_grant = name_grant
        self._read_only = read_only
        self._audit = audit
        self._clock = clock
        self._max_pending = max_pending
        self._timeout = request_timeout
        #: Every runtime with a sink, for reads that need no parent.
        self._runtimes: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._query_runtime: KasRuntime | None = None
        self._query_lock = asyncio.Lock()
        #: Which process drives each run (in memory: a process is not durable).
        self._run_runtime: dict[str, "weakref.ReferenceType[Any]"] = {}
        #: Which chat parents each run the dashboard launched, ON DISK, so a
        #: gateway restart still knows where to reconnect it:
        #: ``{workflowId: {"slot", "cwd", "createdAt", "ended", "workspace"}}``.
        self._ledger_path = ledger_path
        self._ledger: dict[str, dict[str, Any]] = {}
        self._ledger_loaded = ledger_path is None
        self._ledger_lock = asyncio.Lock()
        #: Serializes ledger WRITES: each save snapshots the ledger inside it, so
        #: two saves can never land on disk in reverse order (an older snapshot
        #: overwriting a newer adopt / end).
        self._ledger_save_lock = asyncio.Lock()
        #: Step sessions with a person's reply on its way to KAS right now.
        self._replies_in_flight: set[str] = set()
        self._reconnects: dict[str, tuple[str, float]] = {}
        self._reconnect_tries: dict[str, tuple[int, float]] = {}
        #: Which process each step session lives on (from its node_start).
        self._step_runtime: dict[str, "weakref.ReferenceType[Any]"] = {}
        #: Which chat each parent ACP session belongs to, learned on that chat's
        #: turns, so a run its model starts (KAS's run_workflow) is adopted.
        self._parent_slot: dict[str, str] = {}
        #: Each chat's approval-mode reader, from its turns (note_chat_session).
        self._slot_mode: dict[str, ModeReader] = {}
        #: Each chat's workspace, from its turns, stamped on the runs it starts.
        self._slot_workspace: dict[str, str] = {}
        #: The chat mode each held approval was last decided under (recheck_pending).
        self._decided_under: dict[str, str | None] = {}
        #: Runs whose step tree was read with ``inspect`` (or is being read).
        self._inspecting: set[str] = set()
        self._inspected: set[str] = set()
        self._launches: dict[str, PendingLaunch] = {}
        #: Observe turns issued and not yet taken:
        #: ``{token: (slot_key, createdAt, parentSessionId)}``.
        self._observes: dict[str, tuple[str, float, str]] = {}
        #: Runs whose end already scheduled an observe turn (insertion-ordered).
        self._observed_runs: dict[str, None] = {}
        self._turn_starter: TurnStarter | None = None
        self._pending: dict[str, PendingApproval] = {}
        #: Step questions (``user_input``) held for a person. Never answered by
        #: an approval mode: picking an answer is not an allow.
        self._questions: dict[str, asks.PendingQuestion] = {}
        #: Each chat's current (runtime, ACP session id), from its turns, so a
        #: finished run can be retried or run again on its chat's live process.
        self._slot_binding: dict[str, tuple["weakref.ReferenceType[Any]", str]] = {}
        self._tasks: set[asyncio.Task] = set()
        #: Dashboard side effects: bell notes and transcript marker rows.
        self._hooks = DashboardHooks()
        #: Run ends that already got their outcome marker row (insertion-ordered).
        self._marked_ends: dict[str, None] = {}
        #: Crew "always allow in this step" grants: step session -> grant keys.
        self._step_grants: dict[str, set[str]] = {}
        #: Bumped on every change a surface would render; a cheap poll key.
        self.revision = 0
        #: Bumped on every ``recipes_changed`` push (the recipe list's poll key).
        self.recipes_revision = 0

    # ── Sinks (run ON a runtime's reader loop: never block) ──

    def sink_for(self, runtime: Any) -> _RuntimeSink:
        self._runtimes.add(runtime)
        return _RuntimeSink(self, runtime)

    def _route_run(self, workflow_id: str, runtime: "weakref.ReferenceType[Any]") -> None:
        """Route *workflow_id* to *runtime*; every write to ``_run_runtime`` goes here."""
        self._run_runtime[workflow_id] = runtime
        if len(self._run_runtime) > MAX_ROUTED_RUNS:
            self._prune_routes()

    def _route_step(self, session_id: str, runtime: "weakref.ReferenceType[Any]") -> None:
        """Route step session *session_id* to *runtime*; every ``_step_runtime`` write goes here."""
        self._step_runtime[session_id] = runtime
        if len(self._step_runtime) > MAX_ROUTED_STEPS:
            self._prune_routes()

    def _prune_routes(self) -> None:
        """Drop the routes of runs and step sessions the mirror does not hold.

        A run this router launched and has not seen end keeps its route, since
        KAS may not have reported it yet. Past that, the oldest routes go first.
        shortcut: oldest-first can drop a live step's route when a single run
        holds more step sessions than :data:`MAX_ROUTED_STEPS`; its next ask is
        then refused as unclaimed, never answered for the person. Raise the bound
        if that shows up in practice.
        """
        launched = {w for w, e in self._ledger.items() if not e.get("ended")}
        for workflow_id in [w for w in self._run_runtime if w not in self.mirror]:
            if workflow_id not in launched:
                self._run_runtime.pop(workflow_id, None)
        held_steps = self.mirror.step_session_ids()
        for session_id in [s for s in self._step_runtime if s not in held_steps]:
            self._step_runtime.pop(session_id, None)
        while len(self._run_runtime) > MAX_ROUTED_RUNS:
            self._run_runtime.pop(next(iter(self._run_runtime)))
        while len(self._step_runtime) > MAX_ROUTED_STEPS:
            self._step_runtime.pop(next(iter(self._step_runtime)))

    def _on_notification(
        self, runtime: "weakref.ReferenceType[Any]", method: str, params: dict[str, Any]
    ) -> None:
        if method == wire.NOTIFY_RECIPES_CHANGED:
            self.revision += 1
            self.recipes_revision += 1
            return
        workflow_id = wire.as_str(params.get("workflowId"))
        parent_before = self.mirror.parent_of(workflow_id) if workflow_id else None
        held_before = len(self.mirror)
        if self.mirror.apply_notification(method, params) is not None:
            self.revision += 1
        if len(self.mirror) < held_before:
            # The mirror evicted ended runs: their routes go with them.
            self._prune_routes()
        # Routed only once the mirror holds the run: a run it refused (the live
        # cap) or evicted is not routed either, so the routes stay as bounded as
        # the mirror (:meth:`_route_run`).
        if workflow_id and workflow_id in self.mirror:
            # The process a run's notifications arrive on is the one driving it;
            # this also adopts a run a chat's model started on its own. A run's
            # parent is fixed once known (gap 29), so a conflicting
            # ``parentSessionId`` never adopts it into another chat.
            self._route_run(workflow_id, runtime)
            if workflow_id not in self._ledger:
                parent = parent_before or wire.as_str(params.get("parentSessionId"))
                slot_key = self._parent_slot.get(parent or "")
                if slot_key is not None:
                    self._spawn(self._adopt(workflow_id, slot_key))
        if method == wire.NOTIFY_NODE_START:
            step = wire.as_str(params.get("sessionId"))
            if step and self.mirror.run_for_step_session(step) is not None:
                self._route_step(step, runtime)
        if workflow_id and method == wire.NOTIFY_RUN_START:
            # Running again (resume, retry, restart): its next end is a new one.
            self._marked_ends.pop(workflow_id, None)
        if workflow_id and method == wire.NOTIFY_RUN_COMPLETE:
            self._mark_outcome(workflow_id, params)
            self._schedule_observe(workflow_id, params)
        if workflow_id and self.mirror.status(workflow_id) in wire.TERMINAL_RUN_STATUSES:
            self._note_ended(workflow_id)
        if (
            workflow_id
            and workflow_id in self.mirror
            and workflow_id not in self._inspecting
            and len(self._inspecting) < MAX_ROUTED_RUNS
            and self._serves(runtime(), wire.METHOD_INSPECT)
        ):
            # Only a run the mirror holds is read, and no more reads are in
            # flight than the mirror holds runs.
            # A run this router did not create (a chat model's run_workflow) is
            # known only from its lifecycle events, and ``run_start`` carries the
            # recipe name but not the run's display label (``runLabel``) -- nor,
            # on older KAS, the step tree. So a newly seen run is read once, and
            # the run's end once more to settle every step. Both reads use
            # ``inspect``, KAS's read-only snapshot, on the process driving the
            # run -- never ``load``, which would take it over.
            terminal = method == wire.NOTIFY_RUN_COMPLETE
            if terminal or workflow_id not in self._inspected:
                self._inspecting.add(workflow_id)
                self._spawn(self._inspect(runtime, workflow_id))

    async def _inspect(self, runtime: "weakref.ReferenceType[Any]", workflow_id: str) -> None:
        """Fold KAS's read-only snapshot of one run in. Best effort, once per need."""
        try:
            live = _alive(runtime)
            if live is None:
                return
            response = await self._call(live, wire.METHOD_INSPECT, {"workflowId": workflow_id})
            if self.mirror.apply_load(response) is not None:
                self.revision += 1
        except Exception:  # noqa: BLE001 -- a missing tree costs a picture, not a run
            logger.debug("workflow inspect failed for %s", workflow_id, exc_info=True)
        finally:
            self._inspecting.discard(workflow_id)
            self._inspected.add(workflow_id)
            while len(self._inspected) > LEDGER_MAX_RUNS:
                self._inspected.pop()

    def _note_ended(self, workflow_id: str) -> None:
        entry = self._ledger.get(workflow_id)
        if entry is None or entry.get("ended"):
            return
        entry["ended"] = True
        self._spawn(self._save_ledger())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Dashboard side effects (gaps 6, 10) ──

    def set_dashboard_hooks(self, hooks: DashboardHooks | None) -> None:
        """Install the bell-note and marker-row hooks (:func:`dashboard_hooks`)."""
        self._hooks = hooks or DashboardHooks()

    @staticmethod
    def _serves(runtime: Any, method: str) -> bool:
        """Whether *runtime* advertised *method* (a legacy agent serves everything)."""
        caps = getattr(runtime, "_agent_capabilities", None) if runtime is not None else None
        return wire.method_served(method, wire.extension_methods(caps))

    def _require(self, runtime: Any, method: str, verb: str) -> None:
        """Refuse a verb the driving Kiro Agent does not serve, in words (gap 20)."""
        if not self._serves(runtime, method):
            raise KasWorkflowError(f"This Kiro Agent version does not support {verb}.")

    def _slot_for_run(self, workflow_id: str, parent: str | None = None) -> str | None:
        """The chat a run belongs to: by its parent session, else by the ledger."""
        parent = parent or self.mirror.parent_of(workflow_id)
        return (self._parent_slot.get(parent) if parent else None) or self.slot_of(workflow_id)

    def _write_marker(self, slot_key: str | None, event: Mapping[str, Any]) -> bool:
        writer = self._hooks.append_marker
        if writer is None or not slot_key:
            return False
        from kiro_crew.messaging.workflow_markers import event_line

        try:
            return bool(writer(slot_key, event_line(event), {"kas_event": dict(event)}))
        except Exception:  # noqa: BLE001 -- a lost row costs the line, not the run
            logger.warning("KAS workflow marker row not written for %s", slot_key, exc_info=True)
            return False

    def _mark_outcome(self, workflow_id: str, params: Mapping[str, Any]) -> None:
        """Write the run's outcome row in its chat, once per run end (gap 10).

        Written whether or not an observe turn follows (the chat may be busy,
        or KAS may not wake it), so every end leaves one line.
        """
        if workflow_id in self._marked_ends:
            return
        status = self.mirror.status(workflow_id)
        if status not in wire.TERMINAL_RUN_STATUSES:
            return
        row = self.mirror.row(workflow_id) or {}
        final = wire.as_mapping(params.get("finalState")) or {}
        parent = wire.as_str(params.get("parentSessionId")) or wire.as_str(
            final.get("parentSessionId")
        )
        event = {
            "type": "outcome",
            "workflowId": workflow_id,
            "name": _redact(str(row.get("name") or workflow_id))[:200],
            "status": status,
            "at": row.get("endedAt"),
            "durationSecs": _duration_secs(row.get("startedAt"), row.get("endedAt")),
        }
        self._marked_ends[workflow_id] = None
        while len(self._marked_ends) > MAX_OBSERVED_RUNS:
            self._marked_ends.pop(next(iter(self._marked_ends)))
        self._write_marker(self._slot_for_run(workflow_id, parent), event)

    def _mark_note(self, workflow_id: str, params: Mapping[str, Any], slot_key: str) -> None:
        """Write a step's note to the run's parent as a row in its chat (gap 10)."""
        node_id = wire.as_str(params.get("nodeId"))
        label = None
        row = self.mirror.row(workflow_id) or {}
        if node_id:
            label = _node_label(row.get("nodes") or [], node_id) or node_id
        message = wire.as_str(params.get("message")) or ""
        self._write_marker(
            slot_key,
            {
                "type": "note",
                "workflowId": workflow_id,
                "name": _redact(str(row.get("name") or workflow_id))[:200],
                "severity": wire.as_str(params.get("severity")) or "info",
                "message": _redact(message)[:_MAX_DISPLAY_CHARS],
                "nodeId": node_id,
                "step": _redact(label)[:200] if label else None,
                "agent": wire.as_str(params.get("agentName")),
                "at": _now_iso(self._clock()),
            },
        )

    def _announce(self, key: str, ask: str, workflow_id: str, node_id: str) -> None:
        """Publish one bell note for an ask held for a person (gap 6). Never raises."""
        notify = self._hooks.notify
        slot_key = self._slot_for_run(workflow_id)
        if notify is None or slot_key is None:
            return
        row = self.mirror.row(workflow_id) or {}
        run_name = _redact(str(row.get("name") or workflow_id))[:120]
        step = _redact(_node_label(row.get("nodes") or [], node_id) or node_id)[:120]
        if ask == "question":
            title = f"{run_name} · {step} has a question for you"
        elif ask == "plan":
            title = f"{run_name} · {step}: the plan is waiting for your approval"
        else:
            title = f"{run_name} · {step} is waiting for your approval"
        body = "A workflow step is waiting for you. Open the chat to answer it."
        try:
            notify(
                key,
                slot_key,
                title,
                body,
                {"workflowId": workflow_id, "nodeId": node_id, "ask": ask},
            )
        except Exception:  # noqa: BLE001 -- a lost note costs the signal, not the ask
            logger.warning("KAS workflow ask notification not published", exc_info=True)

    def _retire(self, key: str) -> None:
        """Mark an ask's bell note read: it was answered, withdrawn or lost."""
        retire = self._hooks.retire
        if retire is None:
            return
        try:
            retire(key)
        except Exception:  # noqa: BLE001
            logger.debug("KAS workflow ask notification not retired", exc_info=True)

    def _claims_step(
        self, runtime: "weakref.ReferenceType[Any]", session_id: str, method: str
    ) -> bool:
        if method not in (METHOD_REQUEST_PERMISSION, METHOD_USER_INPUT):
            return False
        owner = self._step_runtime.get(session_id)
        return (
            owner is not None
            and owner() is runtime()
            and (self.mirror.run_for_step_session(session_id) is not None)
        )

    def _on_step_request(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        params: dict[str, Any],
        method: str = METHOD_REQUEST_PERMISSION,
    ) -> "asyncio.Task[Any]":
        task = asyncio.create_task(self._admit(runtime, request_id, session_id, params, method))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _reject_unheld(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        params: dict[str, Any],
        owner: tuple[str, str] | None,
        reason: str,
        command: str | None = None,
    ) -> None:
        """Refuse a step's permission request without holding it, and audit why.

        Every fail-closed refusal (no owning run, a deny rule, nothing to pick,
        a full queue) is a permission decision Crew made on the person's behalf,
        so it leaves the same SEL record an auto-approval does. The audit is
        best effort: a refusal that cannot be logged is still sent, because the
        safe answer must never wait on the log.
        """
        tool_call = wire.as_mapping(params.get("toolCall")) or {}
        title = wire.as_str(tool_call.get("title")) or "Tool call"
        workflow_id, node_id = owner if owner is not None else (None, None)
        try:
            await asyncio.to_thread(
                self._audit,
                session_key=(self.slot_of(workflow_id) if workflow_id else None) or "kas-workflow",
                agent="kas-workflow",
                source="dashboard",
                tool_name=_redact(command or title),
                tool_kind="workflow_step",
                outcome="auto_rejected",
                request_id=str(request_id),
                metadata={"reason": _redact(reason), "workflowId": workflow_id, "nodeId": node_id},
            )
        except Exception:  # noqa: BLE001 -- the refusal goes out regardless
            logger.warning("workflow step refusal not logged", exc_info=True)
        await self._send(runtime, request_id, _reject_result(params))

    async def _admit(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        params: dict[str, Any],
        method: str = METHOD_REQUEST_PERMISSION,
    ) -> None:
        if isinstance(request_id, str) and len(request_id) > asks.MAX_ID_CHARS:
            # A request id is answered back whole, so one over the cap is
            # answered now and never held.
            if method == METHOD_USER_INPUT:
                await self._send(runtime, request_id, asks.dismiss_result_for(method))
            else:
                await self._reject_unheld(runtime, request_id, params, None, "request id too long")
            return
        owner = self.mirror.run_for_step_session(session_id)
        if method == METHOD_USER_INPUT:
            # A structured question: nothing runs, so no deny-rule screen and
            # no approval mode; it waits for the person (or is dismissed).
            if owner is None:
                await self._send(runtime, request_id, asks.dismiss_result_for(method))
                return
            await self._hold_question(runtime, request_id, session_id, owner, params, method)
            return
        if owner is None:
            await self._reject_unheld(runtime, request_id, params, None, "no run owns this step")
            return
        resolved = policy.resolve_step_command(params)
        command = resolved.text
        if command is not None:
            try:
                reason = await asyncio.to_thread(self._screen, command)
            except Exception:  # noqa: BLE001 -- a screen that cannot run refuses
                logger.warning("workflow step screen failed; rejecting", exc_info=True)
                reason = "the deny-rule screen could not run"
            if reason:
                logger.warning(
                    "workflow step command refused by deny rules (run %s, step %s)",
                    owner[0],
                    owner[1],
                )
                await self._reject_unheld(
                    runtime, request_id, params, owner, f"deny rule: {reason}", command
                )
                return
        options = _offered_options(params)
        if not options:
            await self._reject_unheld(runtime, request_id, params, owner, "no option offered")
            return
        tool_call = wire.as_mapping(params.get("toolCall")) or {}
        full_title = wire.as_str(tool_call.get("title")) or "Tool call"
        # Kept capped; a grant is keyed on the whole title, so an over-long one
        # gets none (``_step_grant_key``) rather than a key it shares.
        title = full_title[:_MAX_DISPLAY_CHARS]
        # A plan handoff is the person approving what the run will do next, so
        # it always waits for them, whatever the chat's mode.
        is_plan = _is_plan_approval(params)
        if not is_plan and asks.is_question_request(params):
            # A question waits for the person's answer under every mode.
            await self._hold_question(runtime, request_id, session_id, owner, params)
            return
        # A command it cannot read is held for a person: no grant can match it
        # and no approval mode answers it.
        hold = is_plan or resolved.unreadable
        grant_key = None if hold else _step_grant_key(params, command, full_title)
        if grant_key is not None and await self._apply_step_grant(
            runtime, request_id, session_id, owner, command, options, title, grant_key
        ):
            return
        if not hold and await self._auto_approve(
            runtime, request_id, owner, command, options, title
        ):
            return
        if len(self._pending) >= self._max_pending:
            await self._reject_unheld(
                runtime, request_id, params, owner, "too many asks waiting", command
            )
            return
        plan_text = _tool_call_text(tool_call) if is_plan else None
        approval = PendingApproval(
            key=uuid.uuid4().hex,
            request_id=request_id,
            session_id=session_id,
            workflow_id=owner[0],
            node_id=owner[1],
            title=_redact(title),
            command=_redact(command) if command else None,
            options=options,
            plan=_plan_meta(params),
            created_at=self._clock(),
            runtime=runtime,
            raw_command=command,
            raw_title=title,
            is_plan=is_plan,
            command_unreadable=resolved.unreadable,
            plan_text=_redact_plan(plan_text) if plan_text else None,
            grant_key=grant_key,
        )
        self._pending[approval.key] = approval
        self._decided_under[approval.key] = self._current_mode(owner[0])
        self.revision += 1
        self._announce(approval.key, "plan" if is_plan else "approval", owner[0], owner[1])

    async def recheck_pending(self) -> int:
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
            mode = self._current_mode(approval.workflow_id)
            if mode == self._decided_under.get(key):
                continue  # nothing changed since this one was last decided
            self._decided_under[key] = mode
            self._pending.pop(key, None)
            ok = False
            try:
                ok = await self._auto_approve(
                    approval.runtime,
                    approval.request_id,
                    (approval.workflow_id, approval.node_id),
                    approval.raw_command,
                    approval.options,
                    approval.raw_title or approval.title,
                )
            finally:
                if ok:
                    answered += 1
                    self.revision += 1
                    self._decided_under.pop(key, None)
                    self._retire(key)
                elif key not in self._pending:
                    self._pending[key] = approval
        return answered

    def _current_mode(self, workflow_id: str) -> str | None:
        """The owning chat's approval mode now, or None when it cannot be read."""
        slot_key = self.slot_of(workflow_id)
        reader = self._slot_mode.get(slot_key) if slot_key else None
        if reader is None:
            return None
        try:
            return reader()[0]
        except Exception:  # noqa: BLE001
            return None

    async def _auto_approve(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        owner: tuple[str, str],
        command: str | None,
        options: tuple[Mapping[str, str], ...],
        title: str,
    ) -> bool:
        """Answer a screened step under its chat's approval mode, if that mode allows it.

        True when the request was answered here; False leaves it for a person.
        Every doubt -- an unknown chat, a reader that raises, no allow-once
        option, a command the name-grant check refuses, a log write that fails
        -- lands on False, so the cost of a wrong guess is one more prompt.
        """
        workflow_id, node_id = owner
        slot_key = self.slot_of(workflow_id)
        reader = self._slot_mode.get(slot_key) if slot_key else None
        if reader is None:
            return False
        try:
            mode, reason = reader()
        except Exception:  # noqa: BLE001 -- an unreadable mode asks
            logger.debug("chat approval mode unreadable for %s", slot_key, exc_info=True)
            return False
        if mode == MODE_TRUST:
            pass
        elif mode == MODE_TRUST_READS:
            if not command or not self._read_only(command):
                return False
            reason = MODE_TRUST_READS
        else:
            return False
        option_id = _allow_once(options)
        if option_id is None:
            return False
        if command:
            try:
                refusal = await self._name_grant(command)
            except Exception:  # noqa: BLE001 -- a check that cannot run asks
                logger.debug("name-grant check failed for a workflow step", exc_info=True)
                return False
            if refusal is not None:
                logger.info("workflow step not auto-approved: the name-grant check refused it")
                return False
        assert slot_key is not None  # a reader was found under it
        try:
            await asyncio.to_thread(
                self._audit,
                session_key=slot_key,
                agent="kas-workflow",
                source="dashboard",
                tool_name=_redact(command or title),
                tool_kind="workflow_step",
                outcome="auto_approved",
                request_id=str(request_id),
                metadata={"reason": reason, "workflowId": workflow_id, "nodeId": node_id},
                # Audit-or-ask: only a critical write raises when the log fails.
                critical=True,
            )
        except Exception:  # noqa: BLE001 -- an approval that cannot be logged asks
            logger.warning("workflow step auto-approval not logged; asking", exc_info=True)
            return False
        return await self._send(
            runtime, request_id, {"outcome": {"outcome": "selected", "optionId": option_id}}
        )

    async def _apply_step_grant(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        owner: tuple[str, str],
        command: str | None,
        options: tuple[Mapping[str, str], ...],
        title: str,
        grant_key: str,
    ) -> bool:
        """Answer a request the person already "always allowed" in this step (gap 21).

        The grant is Crew's, not KAS's: it covers exactly this step session and
        this command (or tool). The request has passed the deny-rule screen; a
        command also passes the name-grant check, and every use is written to
        the security event log. Any doubt asks the person again.
        """
        if grant_key not in self._step_grants.get(session_id, set()):
            return False
        option_id = _allow_once(options)
        if option_id is None:
            return False
        if command:
            try:
                refusal = await self._name_grant(command)
            except Exception:  # noqa: BLE001 -- a check that cannot run asks
                return False
            if refusal is not None:
                return False
        workflow_id, node_id = owner
        try:
            await asyncio.to_thread(
                self._audit,
                session_key=self.slot_of(workflow_id) or "kas-workflow",
                agent="kas-workflow",
                source="dashboard",
                tool_name=_redact(command or title),
                tool_kind="workflow_step",
                outcome="auto_approved",
                request_id=str(request_id),
                metadata={"reason": "step_always", "workflowId": workflow_id, "nodeId": node_id},
                critical=True,
            )
        except Exception:  # noqa: BLE001 -- an approval that cannot be logged asks
            logger.warning("workflow step grant use not logged; asking", exc_info=True)
            return False
        return await self._send(
            runtime, request_id, {"outcome": {"outcome": "selected", "optionId": option_id}}
        )

    def _record_step_grant(self, session_id: str, grant_key: str) -> None:
        keys = self._step_grants.pop(session_id, set())
        if len(keys) < MAX_GRANTS_PER_STEP:
            keys.add(grant_key)
        self._step_grants[session_id] = keys
        while len(self._step_grants) > MAX_GRANT_SESSIONS:
            self._step_grants.pop(next(iter(self._step_grants)))

    async def _send(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        result: dict[str, Any],
    ) -> bool:
        live = _alive(runtime)
        if live is None:
            return False
        try:
            await live.send_response(request_id, result)
        except Exception:  # noqa: BLE001 -- a dead runtime fails every wait anyway
            logger.debug("workflow step answer not written", exc_info=True)
            return False
        return True

    # ── Approvals ──

    def _prune_dead_approvals(self) -> None:
        dead = [k for k, a in self._pending.items() if _alive(a.runtime) is None]
        for key in dead:
            self._pending.pop(key, None)
            self._decided_under.pop(key, None)
            self._retire(key)
        gone = [k for k, q in self._questions.items() if _alive(q.runtime) is None]
        for key in gone:
            self._questions.pop(key, None)
            self._retire(key)
        if dead or gone:
            self.revision += 1

    def approvals(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        self._prune_dead_approvals()
        items = [
            a for a in self._pending.values() if workflow_id is None or a.workflow_id == workflow_id
        ]
        items.sort(key=lambda a: a.created_at)
        return [a.to_payload() for a in items]

    async def answer(self, key: str, option_id: str) -> None:
        """Answer a held approval with one of the options it offered."""
        approval = self._pending.get(key)
        if approval is None:
            raise KasWorkflowError("That approval is no longer waiting.")
        if option_id not in {o["optionId"] for o in approval.options}:
            raise KasWorkflowError("That option was not offered for this approval.")
        kind = next((o.get("kind") for o in approval.options if o["optionId"] == option_id), None)
        sent_id = option_id
        if kind == "allow_always" and not approval.is_plan:
            # Crew's Trust model (gap 21): KAS is answered allow-ONCE, so no rule
            # persists in KAS that would skip Crew's deny-rule screen, and Crew
            # remembers the grant for this step session only.
            # A command it could not read gets no grant at all: this one call
            # is allowed, and the next one asks again.
            once = _allow_once(approval.options)
            if once is not None and (approval.grant_key or approval.command_unreadable):
                sent_id = once
                if approval.grant_key:
                    self._record_step_grant(approval.session_id, approval.grant_key)
        self._pending.pop(key, None)
        self._decided_under.pop(key, None)
        self.revision += 1
        self._retire(key)
        sent = await self._send(
            approval.runtime,
            approval.request_id,
            {"outcome": {"outcome": "selected", "optionId": sent_id}},
        )
        if not sent:
            raise KasWorkflowError("The step's Kiro Agent session is gone; nothing was answered.")

    # ── Questions ──

    async def _hold_question(
        self,
        runtime: "weakref.ReferenceType[Any]",
        request_id: int | str,
        session_id: str,
        owner: tuple[str, str],
        params: Mapping[str, Any],
        method: str = METHOD_REQUEST_PERMISSION,
    ) -> None:
        """Hold a step's question for a person, or decline it when it cannot be held.

        A question that cannot wait (a full queue, no answer to offer) is
        answered "no answer" in its request's own shape (``cancelled`` on the
        legacy bridge; on ``_kiro/userInput`` an ``answered`` whose text says no
        person saw it): never a pick made on the person's behalf.
        """
        if len(self._pending) + len(self._questions) >= self._max_pending:
            await self._send(runtime, request_id, asks.dismiss_result_for(method))
            return
        build = (
            asks.question_from_user_input
            if method == METHOD_USER_INPUT
            else asks.question_from_request
        )
        question = build(
            params,
            key=uuid.uuid4().hex,
            request_id=request_id,
            session_id=session_id,
            owner=owner,
            created_at=self._clock(),
            runtime=runtime,
            path=self.mirror.path_of_step_session(owner[0], session_id),
            slot=self.slot_of(owner[0]),
        )
        if question is None:
            await self._send(runtime, request_id, asks.dismiss_result_for(method))
            return
        self._questions[question.key] = question
        self.revision += 1
        self._announce(question.key, "question", owner[0], owner[1])

    def questions(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        """Every held step question, oldest first (``/state`` ``questions``)."""
        self._prune_dead_approvals()
        items = [
            q
            for q in self._questions.values()
            if workflow_id is None or q.workflow_id == workflow_id
        ]
        items.sort(key=lambda q: q.created_at)
        return [q.to_payload() for q in items]

    async def answer_question(
        self, key: str, answers: Mapping[str, Any] | None, *, dismiss: bool = False
    ) -> None:
        """Answer a held question with one offered option, or dismiss it.

        The answer goes back on the question's own request: the picked option
        (KAS hands its label to the step as the answer) or, dismissed, "no
        answer" (``cancelled`` on the legacy bridge; on ``_kiro/userInput`` text
        saying the person declined, never read as yes).
        """
        question = self._questions.get(key)
        if question is None:
            raise KasWorkflowError("That question is no longer waiting.")
        if dismiss:
            result: dict[str, Any] = question.dismiss_result(person=True)
        else:
            try:
                result = question.answer_result(answers or {})
            except ValueError as exc:
                raise KasWorkflowError(str(exc).capitalize() + ".") from exc
        self._questions.pop(key, None)
        self.revision += 1
        self._retire(key)
        if not await self._send(question.runtime, question.request_id, result):
            raise KasWorkflowError("The step's Kiro Agent session is gone; nothing was answered.")

    async def _withdraw_session_asks(self, session_id: str) -> None:
        """Answer ``cancelled`` to every ask held for *session_id* (its turn was stopped).

        ACP asks a client to answer every open permission request of a session
        it cancels; KAS reads ``cancelled`` as a refusal or a skipped question.
        """
        held: list[tuple[Any, int | str, dict[str, Any]]] = []
        cancelled: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
        for key in [k for k, a in self._pending.items() if a.session_id == session_id]:
            approval = self._pending.pop(key)
            self._decided_under.pop(key, None)
            self._retire(key)
            held.append((approval.runtime, approval.request_id, cancelled))
        for key in [k for k, q in self._questions.items() if q.session_id == session_id]:
            question = self._questions.pop(key)
            self._retire(key)
            held.append((question.runtime, question.request_id, question.dismiss_result()))
        for runtime, request_id, result in held:
            await self._send(runtime, request_id, result)
        if held:
            self.revision += 1

    # ── Ledger, reconnecting and boot recovery: ``kiro_crew.kas_workflows.router_recovery`` ──
    # ``reattach`` stays here: the lines it returns are redacted where they are built.
    ensure_ledger = _r_recovery.ensure_ledger
    _save_ledger = _r_recovery._save_ledger
    _undriven_live = _r_recovery._undriven_live
    prepare_reconnect = _r_recovery.prepare_reconnect
    _take_reconnect = _r_recovery._take_reconnect
    _step_cwd = _r_recovery._step_cwd
    _reask_plans = _r_recovery._reask_plans
    schedule_reattach = _r_recovery.schedule_reattach
    due_reconnects = _r_recovery.due_reconnects
    reset_reconnect_tries = _r_recovery.reset_reconnect_tries
    _owner_verdict = _r_recovery._owner_verdict
    recover_on_boot = _r_recovery.recover_on_boot

    def slot_of(self, workflow_id: str) -> str | None:
        entry = self._ledger.get(workflow_id)
        return entry["slot"] if entry else None

    # ── Chat-started runs ──

    def note_chat_session(
        self,
        slot_key: str,
        provider: Any,
        mode: ModeReader | None = None,
        *,
        workspace: str | None = None,
    ) -> None:
        """Remember which chat *provider*'s KAS session belongs to, and its mode.

        Called on every chat turn, before the prompt goes out, so the
        notifications of a run that turn's model starts already find their chat.
        *mode* reads the chat's approval mode at the moment a step asks, so a
        mode change in the picker reaches the next step without another turn.
        *workspace* is stamped on the ledger entry of a run this chat starts, so
        boot recovery never reattaches it to a chat of another workspace.
        Cheap and synchronous: dict writes, or nothing off Kiro Agent.
        """
        if workspace is not None:
            self._slot_workspace.pop(slot_key, None)
            self._slot_workspace[slot_key] = workspace
            while len(self._slot_workspace) > MAX_PARENT_SESSIONS:
                self._slot_workspace.pop(next(iter(self._slot_workspace)))
        if mode is not None:
            self._slot_mode.pop(slot_key, None)
            self._slot_mode[slot_key] = mode
            while len(self._slot_mode) > MAX_PARENT_SESSIONS:
                self._slot_mode.pop(next(iter(self._slot_mode)))
        binding = session_binding(provider)
        if binding is None:
            return
        session_id = binding[1]
        self._slot_binding.pop(slot_key, None)
        self._slot_binding[slot_key] = (weakref.ref(binding[0]), session_id)
        while len(self._slot_binding) > MAX_PARENT_SESSIONS:
            self._slot_binding.pop(next(iter(self._slot_binding)))
        if self._parent_slot.get(session_id) == slot_key:
            return
        self._parent_slot.pop(session_id, None)
        self._parent_slot[session_id] = slot_key
        while len(self._parent_slot) > MAX_PARENT_SESSIONS:
            self._parent_slot.pop(next(iter(self._parent_slot)))

    async def _adopt(self, workflow_id: str, slot_key: str) -> None:
        """Record a run a chat's model started as that chat's, like a launched one."""
        await self.ensure_ledger()
        if workflow_id in self._ledger:
            return
        self._ledger[workflow_id] = {
            "slot": slot_key,
            "cwd": "",
            "createdAt": self._clock(),
            "ended": self.mirror.status(workflow_id) in wire.TERMINAL_RUN_STATUSES,
            "workspace": self._slot_workspace.get(slot_key, ""),
        }
        self.revision += 1
        await self._save_ledger()

    # ── Launching ──

    def _expire_launches(self) -> None:
        cutoff = self._clock() - LAUNCH_TTL_SECS
        for token in [t for t, la in self._launches.items() if la.created_at < cutoff]:
            self._launches.pop(token, None)

    def prepare_launch(
        self,
        slot_key: str,
        cwd: str,
        source: str,
        inputs: Mapping[str, str],
        *,
        run_label: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> PendingLaunch:
        """Register a run for *slot_key*'s next launch turn to start.

        *model* / *effort* become the run's default for its steps (a step's own
        override still wins; KAS ``_kiro/workflow/new`` ``modelId`` /
        ``effortLevel``).
        """
        if not source:
            raise KasWorkflowError("A workflow source is required.")
        if len(source) > MAX_DEFINITION_BYTES:
            raise KasWorkflowError("The workflow source is too long.")
        fields = (cwd, run_label or "", model or "", effort or "")
        if any(len(text) > _MAX_LAUNCH_FIELD_CHARS for text in fields):
            raise KasWorkflowError("A workflow launch field is too long.")
        if len(inputs) > MAX_LAUNCH_INPUTS or any(
            len(str(k)) > _MAX_LAUNCH_FIELD_CHARS or len(str(v)) > _MAX_LAUNCH_FIELD_CHARS
            for k, v in inputs.items()
        ):
            raise KasWorkflowError("The workflow inputs are too many or too long.")
        self._expire_launches()
        if len(self._launches) >= MAX_PENDING_LAUNCHES:
            raise KasWorkflowError("Too many workflow launches are waiting to start.")
        launch = PendingLaunch(
            token=secrets.token_hex(16),
            slot_key=slot_key,
            cwd=cwd,
            source=source,
            inputs=dict(inputs),
            run_label=run_label,
            created_at=self._clock(),
            model=model or None,
            effort=effort or None,
        )
        self._launches[launch.token] = launch
        return launch

    @staticmethod
    def launch_token(message: str) -> str | None:
        """The token of a launch message, or None for any other message."""
        match = _LAUNCH_RE.match(message or "")
        return match.group(1) if match else None

    @staticmethod
    def reconnect_token(message: str) -> str | None:
        """The token of a reconnect message, or None for any other message."""
        match = _RECONNECT_RE.match(message or "")
        return match.group(1) if match else None

    @classmethod
    def is_turn_action(cls, message: str) -> bool:
        """Whether *message* is a launch or reconnect the chat turn answers itself."""
        return cls.launch_token(message) is not None or cls.reconnect_token(message) is not None

    def take_launch(self, slot_key: str, token: str) -> PendingLaunch | None:
        """The launch *token* names, consumed, only when it was issued for *slot_key*."""
        self._expire_launches()
        launch = self._launches.get(token)
        if launch is None or launch.slot_key != slot_key:
            return None
        self._launches.pop(token, None)
        return launch

    async def start_in_session(
        self, runtime: Any, session_id: str, launch: PendingLaunch
    ) -> dict[str, Any]:
        """Create and start *launch*'s run with *session_id* as its parent."""
        try:
            params = wire.new_run_params(
                source=launch.source,
                parent_session_id=session_id,
                inputs=launch.inputs,
                run_label=launch.run_label,
                model_id=launch.model,
                effort_level=launch.effort,
            )
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        created = await self._call(runtime, wire.METHOD_NEW, params)
        workflow_id = wire.as_str(created.get("workflowId"))
        if workflow_id is None:
            raise KasWorkflowError("Kiro Agent created no run.")
        if len(workflow_id) > MAX_RUN_ID_CHARS:
            raise KasWorkflowError("Kiro Agent named the run by an id too long to keep.")
        self._route_run(workflow_id, weakref.ref(runtime))
        await self.ensure_ledger()
        self._ledger[workflow_id] = {
            "slot": launch.slot_key,
            "cwd": launch.cwd,
            "createdAt": self._clock(),
            "ended": False,
            "workspace": self._slot_workspace.get(launch.slot_key, ""),
        }
        await self._save_ledger()
        state = wire.as_mapping(created.get("initialState"))
        if state is not None:
            self.mirror.apply_load({"workflowId": workflow_id, "state": state})
        await self._call(runtime, wire.METHOD_INVOKE, {"workflowId": workflow_id})
        self.revision += 1
        return self._row(workflow_id) or {"workflowId": workflow_id}

    # ── Observing a run's end ──
    #
    # When a run ends, KAS starts a model turn on the run's parent session by
    # itself ("A workflow you launched ... completed") -- but only when that
    # session is idle; a busy one gets the nudge in its steering buffer for its
    # current turn instead. No client sent that turn's prompt, so without a
    # reader its text is never shown and its permission requests are never
    # answered. The chat gets a short OBSERVE turn for it: the chat-turn pipeline
    # reads the engine's turn as its own (approvals under the chat's mode, text,
    # stats), sending nothing (:meth:`AcpSessionHandle.observe_turn`).

    def set_turn_starter(self, starter: TurnStarter | None) -> None:
        """Install how a chat is sent an observe turn (the dashboard's chat slots)."""
        self._turn_starter = starter

    def _expire_observes(self) -> None:
        cutoff = self._clock() - LAUNCH_TTL_SECS
        for token in [t for t, held in self._observes.items() if held[1] < cutoff]:
            self._observes.pop(token, None)

    def prepare_observe(self, slot_key: str, name: str, status: str, parent_session_id: str) -> str:
        """A single-use observe message for *slot_key*'s next turn.

        *parent_session_id* is the session KAS wakes; the turn reads it only if
        the chat's claimed session is still that one (:func:`observe_from_turn`).
        """
        self._expire_observes()
        while len(self._observes) >= MAX_PENDING_OBSERVES:
            self._observes.pop(next(iter(self._observes)))
        token = secrets.token_hex(16)
        self._observes[token] = (slot_key, self._clock(), parent_session_id)
        # The row the chat shows for this turn. Characters the chat would expand
        # (``$skill``, ``@prompt``) or that end the code span are dropped.
        label = re.sub(r"[`$@\n\r]", "", _redact(name)).strip()[:200] or "workflow"
        verb = status if status in _OBSERVE_VERBS else "ended"
        return f"[Workflow `{label}` {verb}] Kiro Agent's reply follows (observe {token})"

    @staticmethod
    def observe_token(message: str) -> str | None:
        """The token of an observe message, or None for any other message."""
        match = _OBSERVE_RE.match(message or "")
        return match.group(1) if match else None

    def take_observe(self, slot_key: str, token: str) -> str | None:
        """Consume *token* and return the session it watches, only for *slot_key*."""
        self._expire_observes()
        held = self._observes.get(token)
        if held is None or held[0] != slot_key:
            return None
        self._observes.pop(token, None)
        return held[2]

    def _schedule_observe(self, workflow_id: str, params: Mapping[str, Any]) -> None:
        """On a run's end, send its parent chat one observe turn. Never blocks.

        Only for a run whose parent session is a chat this router has seen a turn
        on (:meth:`note_chat_session`): that is the session KAS wakes. Once per
        run. The turn itself starts on the next loop pass, off the reader's frame.
        """
        if self._turn_starter is None or workflow_id in self._observed_runs:
            return
        status = wire.as_str(params.get("status")) or self.mirror.status(workflow_id)
        if status not in wire.TERMINAL_RUN_STATUSES:
            return
        final = wire.as_mapping(params.get("finalState")) or {}
        parent = (
            wire.as_str(params.get("parentSessionId"))
            or wire.as_str(final.get("parentSessionId"))
            or self.mirror.parent_of(workflow_id)
        )
        if not self._queue_observe(workflow_id, parent, status or "", final):
            return
        self._observed_runs[workflow_id] = None
        while len(self._observed_runs) > MAX_OBSERVED_RUNS:
            self._observed_runs.pop(next(iter(self._observed_runs)))

    def _on_watched_notification(self, method: str, params: Mapping[str, Any]) -> None:
        """A step's note to its run's parent: send the chat an observe turn.

        KAS delivers a step's ``send_message`` (and the runtime's own note on a
        step's behalf, such as a repeat pausing at its iteration limit) to the
        run's parent; a ``warning``, an ``error`` or an ``info`` wakes that
        parent when it is idle, with a model turn no client sent -- the same
        unread turn as a run's end. Only notes addressed to the run's parent
        are read here; a parent's note to one of its steps wakes the step.
        """
        if method != wire.NOTIFY_SESSION_NOTIFY:
            return
        workflow_id = wire.as_str(params.get("workflowId"))
        target = wire.as_str(params.get("sessionId"))
        severity = wire.as_str(params.get("severity")) or "info"
        if not workflow_id or not target:
            return
        parent = self.mirror.parent_of(workflow_id)
        if parent is None:
            # A run not mirrored yet (a restart): the note's target is the
            # parent only if it is a chat this router knows the run under.
            ledger_slot = self.slot_of(workflow_id)
            if ledger_slot is None or self._parent_slot.get(target) not in (None, ledger_slot):
                return
            parent = target
        if parent != target:
            return
        # Every note to the parent leaves a row in its chat, whatever its
        # severity and whether or not the chat is busy (gap 10).
        slot_key = self._slot_for_run(workflow_id, parent)
        if slot_key is not None:
            self._mark_note(workflow_id, params, slot_key)
        if self._turn_starter is None or severity not in wire.WAKING_NOTE_SEVERITIES:
            return
        self._queue_observe(workflow_id, parent, _NOTE_VERBS[severity], {})

    def _queue_observe(
        self, workflow_id: str, parent: str | None, verb: str, final: Mapping[str, Any]
    ) -> bool:
        """Queue one observe turn on *workflow_id*'s chat for *parent*'s woken turn.

        Only for a run whose parent session is a chat this router has seen a
        turn on (:meth:`note_chat_session`), or one the ledger names. Returns
        whether a turn was queued. Never blocks.
        """
        starter = self._turn_starter
        if starter is None or not parent or len(parent) > asks.MAX_ID_CHARS:
            return False
        slot_key = self._parent_slot.get(parent)
        if slot_key is None:
            # After a gateway restart the in-memory map is empty until the chat
            # has a turn, but the ledger (on disk) still names the run's chat.
            # The observe turn itself re-checks that the chat's claimed session
            # IS the one KAS woke, so a wrong guess shows a notice, not a read.
            slot_key = self.slot_of(workflow_id)
        if slot_key is None:
            return False
        row = self.mirror.row(workflow_id) or {}
        name = wire.as_str(final.get("workflowName")) or str(row.get("name") or workflow_id)
        message = self.prepare_observe(slot_key, name, verb, parent)
        token = self.observe_token(message)
        try:
            asyncio.get_running_loop().call_soon(
                self._start_observe, starter, slot_key, message, token
            )
        except RuntimeError:  # no loop: nothing could read the turn anyway
            self._observes.pop(token or "", None)
            return False
        return True

    def _start_observe(
        self, starter: TurnStarter, slot_key: str, message: str, token: str | None
    ) -> None:
        try:
            started = bool(starter(slot_key, message))
        except Exception:  # noqa: BLE001 -- a chat that cannot take it loses only the view
            logger.warning("KAS workflow observe turn not started for %s", slot_key, exc_info=True)
            started = False
        if not started:
            self._observes.pop(token or "", None)

    # ── Reconnecting ──

    async def reattach(self, slot_key: str, runtime: Any, session_id: str) -> list[str]:
        """Re-attach *slot_key*'s unfinished runs to the chat's live process.

        Each run is ``load``-ed on this process, which is read-only for a run it
        does not own: KAS reconciles a dead owner's run to paused with
        :data:`STALE_RUNNING_PAUSE_REASON` and never steals a live owner's. A
        run paused for that reason is resumed here, which is where KAS hands
        this process ownership; a run paused for any other reason is attached
        and left paused; a run still running belongs to a live process
        elsewhere and is left for a later try. Returns one line per run.
        """
        await self.ensure_ledger()
        lines: list[str] = []
        ref = weakref.ref(runtime)
        for wid in self._undriven_live(slot_key):
            try:
                loaded = await self._call(runtime, wire.METHOD_LOAD, {"workflowId": wid})
            except KasWorkflowError as exc:
                lines.append(f"`{wid}`: could not be loaded ({_redact(str(exc))[:200]})")
                continue
            if self.mirror.apply_load(loaded) is not None:
                self.revision += 1
            for raw in wire.as_list(loaded.get("stepSessions")):
                item = wire.as_mapping(raw)
                step = wire.as_str(item.get("sessionId")) if item else None
                if step:
                    self._route_step(step, ref)
            state = wire.as_mapping(loaded.get("state")) or {}
            status = wire.as_str(state.get("status"))
            name = self.mirror.row(wid) or {}
            label = f"**{_redact(str(name.get('name') or wid))}**"
            if status in wire.TERMINAL_RUN_STATUSES:
                self._note_ended(wid)
                lines.append(f"{label} had already finished ({status}).")
            elif status == wire.STATUS_PAUSED and (plan_steps := _kept_plan_waits(state)):
                # KAS keeps a top-level step's open plan approval across an agent
                # restart and asks it again on that step session's next
                # ``session/load``, but only while the run stays paused on it:
                # resuming would move the step on and cancel the plan. So the
                # step session is loaded here (its request then reaches this
                # router) and the run is left paused; Allow resumes it in KAS.
                self._route_run(wid, ref)
                reasked = await self._reask_plans(runtime, wid, plan_steps)
                lines.append(
                    f"{label} is waiting for its plan approval again."
                    if reasked
                    else f"{label} is reconnected and stays paused."
                )
            elif status == wire.STATUS_PAUSED and (
                wire.as_str(state.get("pauseReason")) == STALE_RUNNING_PAUSE_REASON
            ):
                await self._call(
                    runtime, wire.METHOD_RESUME, wire.user_control_params(wid, RECONNECT_REASON)
                )
                self._route_run(wid, ref)
                lines.append(f"{label} was interrupted by a Kiro Agent restart and is resuming.")
            elif status == wire.STATUS_PAUSED:
                self._route_run(wid, ref)
                lines.append(f"{label} is reconnected and stays paused.")
            else:
                lines.append(
                    f"{label} is still held by another Kiro Agent process; "
                    "it will be retried shortly."
                )
        if any(_alive(self._run_runtime.get(w)) is runtime for w in self._ledger):
            self._reconnect_tries.pop(slot_key, None)
        self.revision += 1
        return lines

    # ── Requests and reads: ``kiro_crew.kas_workflows.router_reads`` ──
    # ``recipe_detail`` stays here: its step prompts are redacted where they are read.
    _call = _r_reads._call
    _reader = _r_reads._reader
    _row = _r_reads._row
    run_row = _r_reads.run_row
    rows = _r_reads.rows
    recipes = _r_reads.recipes
    runs = _r_reads.runs
    _forget_deleted = _r_reads._forget_deleted
    step_transcript = _r_reads.step_transcript
    chat_runs = _r_reads.chat_runs
    run = _r_reads.run

    async def recipe_detail(self, cwd: str, source: str) -> dict[str, Any]:
        """One recipe with its steps' prompt text, for the recipe detail view (gap 27).

        *source* must be a recipe KAS lists for *cwd* right now, so no caller
        can name a file to read. Only a ``.workflow.json`` file is read (the
        definition format Crew can parse without a YAML dependency), capped in
        size; a bundled or YAML recipe answers ``definitionAvailable: false``.
        Prompts are redacted and capped.
        """
        runtime = await self._reader()
        project = _project(cwd)
        response = await self._call(
            runtime, wire.METHOD_LIST_RECIPES, {"workspacePaths": [project]}
        )
        recipe = next(
            (r for r in wire.parse_recipes(response) if r.source and r.source == source), None
        )
        if recipe is None:
            raise KasWorkflowError("That recipe is not listed for this project.")
        steps: list[dict[str, Any]] = []
        available = False
        path = recipe.source or ""
        if not recipe.read_only and path.endswith(".workflow.json") and os.path.isabs(path):
            definition = await asyncio.to_thread(_read_definition, path)
            if definition is not None:
                available = True
                steps = _definition_steps(definition)
        return {
            "recipe": recipe.to_payload(project),
            "steps": steps,
            "definitionAvailable": available,
        }

    # ── Run, step and recovery controls: ``kiro_crew.kas_workflows.router_controls`` ──
    #
    # Each maps one dashboard action onto the KAS verb that does it, on the
    # process that drives the run. KAS is the authority on every state rule: a
    # refusal comes back as KasWorkflowError (409) with KAS's own reason.
    # ``end_step`` stays here: the text it returns is redacted where it is built.

    _NOT_CONNECTED = (
        "This run's chat is not connected to Kiro Agent right now. "
        "Open the run's chat, then try again."
    )

    control = _r_controls.control
    _driver = _r_controls._driver
    _finished_runtime = _r_controls._finished_runtime
    _redriven = _r_controls._redriven
    retry = _r_controls.retry
    restart = _r_controls.restart
    grant = _r_controls.grant
    cancel_step = _r_controls.cancel_step
    reply_to_step = _r_controls.reply_to_step
    _send_step_reply = _r_controls._send_step_reply

    async def end_step(
        self,
        workflow_id: str,
        node_id: str,
        status: str,
        reason: str | None = None,
        path: str | None = None,
    ) -> str:
        """End a paused step with *status*, or end an exhausted loop and continue.

        KAS ``update`` ``update_status`` with ``nodeId``: a paused step takes
        ``completed`` or ``failed``; the repeat the run is parked on at its limit
        takes ``completed`` only. KAS answers ``updated: false`` with its reason
        for anything else, which is raised here.
        """
        if path:
            known = self.mirror.node_ids_at(workflow_id, path)
            if known is not None and known != node_id:
                raise KasWorkflowError("That path is not this step.")
        try:
            params = wire.end_step_params(workflow_id, node_id, status, reason)
        except ValueError as exc:
            raise KasWorkflowError(str(exc)) from exc
        driver = self._driver(workflow_id)
        self._require(driver, wire.METHOD_UPDATE, "ending a step")
        response = await self._call(driver, wire.METHOD_UPDATE, params)
        message = wire.as_str(response.get("message")) or ""
        if response.get("updated") is not True:
            raise KasWorkflowError(_redact(message) or "Kiro Agent did not end that step.")
        self.revision += 1
        return _redact(message)

    async def close(self) -> None:
        """Stop the query process; chat processes belong to their sessions."""
        for task in list(self._tasks):
            task.cancel()
        async with self._query_lock:
            runtime, self._query_runtime = self._query_runtime, None
        if runtime is not None:
            try:
                await runtime.kill(expected=True, reason="kas workflow query runtime shutdown")
            except Exception:  # noqa: BLE001
                logger.debug("kas workflow query runtime kill failed", exc_info=True)
        self._pending.clear()
        self._questions.clear()
        self._launches.clear()
        self._observes.clear()


#: The modules whose functions :class:`KasWorkflowRouter` binds as its own methods.
_MEMBER_OWNERS = frozenset({_r_controls.__name__, _r_reads.__name__, _r_recovery.__name__})
own_methods(KasWorkflowRouter, _MEMBER_OWNERS)


def _definition_steps(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every step of a recipe definition with its agent and prompt text, in plan order."""
    out: list[dict[str, Any]] = []

    def walk(nodes: Any, depth: int) -> None:
        if depth > wire.MAX_PLAN_DEPTH or len(out) >= MAX_PATHS_PER_DEFINITION:
            return
        for raw in wire.as_list(nodes):
            node = wire.as_mapping(raw)
            if node is None:
                continue
            node_id = wire.as_str(node.get("id")) or wire.as_str(node.get("nodeId"))
            if wire.as_str(node.get("type")) == wire.NODE_TYPE_STEP and node_id:
                prompt = wire.as_str(node.get("prompt"))
                out.append(
                    {
                        "nodeId": node_id,
                        "agent": wire.as_str(node.get("agent")),
                        "prompt": (
                            _redact_plan(prompt[: MAX_PROMPT_CHARS * 2])[:MAX_PROMPT_CHARS]
                            if prompt
                            else None
                        ),
                    }
                )
            for key in ("steps", "children", "branches"):
                walk(node.get(key), depth + 1)

    walk(definition.get("steps"), 0)
    return out


async def launch_from_turn(slot_key: str, message: str, provider: Any) -> str | None:
    """Start the launch *message* names on the chat's own session.

    Called by the chat turn once it has claimed the slot's session. Returns the
    reply the chat shows in place of a model turn, or None when *message* is not
    a launch message (the turn then proceeds normally).
    """
    router = get_router()
    reconnect = router.reconnect_token(message)
    if reconnect is not None:
        return await _reconnect_from_turn(router, slot_key, reconnect, provider)
    token = router.launch_token(message)
    if token is None:
        return None
    launch = router.take_launch(slot_key, token)
    if launch is None:
        return "That workflow launch has expired or belongs to another chat; nothing was started."
    binding = session_binding(provider)
    if binding is None:
        return (
            "This chat is not running on Kiro Agent, so it cannot start a Kiro "
            "Workflow. Switch the agent to Kiro Agent and launch it again."
        )
    runtime, session_id = binding
    try:
        row = await router.start_in_session(runtime, session_id, launch)
    except KasWorkflowError as exc:
        return f"The workflow could not start: {_redact(str(exc))}"
    name = row.get("name") or launch.source
    return (
        f"Started workflow **{_redact(str(name))}** (run `{row.get('workflowId')}`). "
        "Its steps run in Kiro Agent; step approvals appear in the run view. "
        "Kiro Agent posts a summary here when the run ends."
    )


def is_observe_turn(message: str) -> bool:
    """Whether *message* has the shape of an observe turn (its token unchecked)."""
    return KasWorkflowRouter.observe_token(message) is not None


def observe_from_turn(slot_key: str, message: str, provider: Any, *, is_new: bool) -> str | None:
    """Authorize the observe turn *message* names, on the chat's claimed session.

    Called by the chat turn once it has claimed the slot's session. Returns None
    when the turn should go on to read the engine's turn
    (:meth:`AcpSessionHandle.observe_turn`), or the reply the chat shows in its
    place: the token is spent, foreign or expired; the chat is not on Kiro Agent;
    or the claimed session is not the one KAS woke (it was started afresh for
    this turn, or the chat has moved to another session since the run began),
    so nothing it could read is that turn. The token is consumed either way.
    """
    router = get_router()
    token = router.observe_token(message)
    if token is None:
        return "That is not a workflow summary request; nothing was read."
    watched = router.take_observe(slot_key, token)
    if watched is None:
        return (
            "That workflow summary request has expired or belongs to another chat; "
            "nothing was read."
        )
    binding = session_binding(provider)
    if binding is None:
        return "This chat is not running on Kiro Agent, so there is no Kiro Agent reply to show."
    if is_new or binding[1] != watched:
        return (
            "Kiro Agent replied to the run's end on a session this chat is no longer "
            "connected to, so its reply cannot be shown here."
        )
    return None


async def _reconnect_from_turn(
    router: KasWorkflowRouter, slot_key: str, token: str, provider: Any
) -> str:
    if not router._take_reconnect(slot_key, token):
        return "That reconnect request has expired or belongs to another chat; nothing was done."
    binding = session_binding(provider)
    if binding is None:
        return "This chat is not running on Kiro Agent, so its workflow runs cannot reconnect."
    try:
        lines = await router.reattach(slot_key, *binding)
    except KasWorkflowError as exc:
        return f"Reconnecting failed: {_redact(str(exc))}"
    if not lines:
        return "No unfinished workflow runs needed reconnecting."
    return "\n".join(f"- {line}" for line in lines)


async def _spawn_query_runtime() -> KasRuntime:
    """A session-less KAS process for catalog and history reads."""
    from kiro_crew.agent_sdk.kas_workflows_driver import spawn_query_runtime
    from kiro_crew.config import KiroCrewConfig

    config = await asyncio.to_thread(KiroCrewConfig.load)
    runtime: KasRuntime = await spawn_query_runtime(getattr(config.agent, "sandbox", "auto"))
    return runtime


_ROUTER: KasWorkflowRouter | None = None
#: The dashboard's observe-turn starter, applied to the router whenever it exists.
_TURN_STARTER: TurnStarter | None = None
#: The dashboard's bell-note and marker-row hooks, likewise.
_HOOKS: DashboardHooks | None = None


def install_dashboard_hooks(hooks: DashboardHooks | None) -> None:
    """Install the dashboard hooks without creating the router (see :func:`dashboard_hooks`)."""
    global _HOOKS
    _HOOKS = hooks
    if _ROUTER is not None:
        _ROUTER.set_dashboard_hooks(hooks)


def dashboard_hooks(state: Any) -> DashboardHooks:
    """The hooks a dashboard *state* gives the router: bell notes and marker rows.

    - A held step ask becomes one ``system.approval`` note (critical, never
      muted) that opens its chat; once the ask is gone the note is marked read.
    - A run's outcome and its steps' notes become one ``notice`` row each in
      the run's chat, when that chat is open.
    """
    from urllib.parse import quote

    def notify(key: str, slot_key: str, title: str, body: str, meta: Mapping[str, Any]) -> None:
        state.notify(
            "approval",
            title,
            body,
            meta={**dict(meta), "slot": slot_key, "kas_ask": key},
            url=f"/chat?slot={quote(slot_key, safe='')}",
            channel="system.approval",
            publisher_session=slot_key,
        )

    def retire(key: str) -> None:
        stamps = [
            note.get("ts")
            for note in list(getattr(state, "_notification_log", []) or [])
            if note.get("kas_ask") == key and not note.get("acked")
        ]
        for ts in stamps:
            if isinstance(ts, str) and ts:
                try:
                    asyncio.get_running_loop().create_task(state.ack_notification(ts))
                except RuntimeError:  # no loop: nothing is watching the bell either
                    return

    def append_marker(slot_key: str, content: str, meta: Mapping[str, Any]) -> bool:
        slot = state.get_slot(slot_key)
        if slot is None:
            return False
        from kiro_crew.dashboard.state import append_and_surface

        append_and_surface(state, slot, "notice", content, "msg msg-info", meta=dict(meta))
        return True

    return DashboardHooks(notify=notify, retire=retire, append_marker=append_marker)


def install_turn_starter(starter: TurnStarter | None) -> None:
    """Install how chats are sent observe turns, without creating the router.

    The dashboard calls this at startup; the router picks it up when it is first
    created (a gateway that never uses KAS workflows never creates one).
    """
    global _TURN_STARTER
    _TURN_STARTER = starter
    if _ROUTER is not None:
        _ROUTER.set_turn_starter(starter)


def get_router() -> KasWorkflowRouter:
    """The gateway's one router, created on first use."""
    global _ROUTER
    if _ROUTER is None:
        from kiro_crew.config.paths import config_dir

        _ROUTER = KasWorkflowRouter(
            _spawn_query_runtime, ledger_path=config_dir() / "kas_workflows" / "runs.json"
        )
        _ROUTER.set_turn_starter(_TURN_STARTER)
        _ROUTER.set_dashboard_hooks(_HOOKS)
    return _ROUTER


async def close_router() -> None:
    global _ROUTER
    router, _ROUTER = _ROUTER, None
    if router is not None:
        await router.close()


__all__ = [
    "CONTROL_METHODS",
    "DashboardHooks",
    "KasWorkflowError",
    "KasWorkflowRouter",
    "PendingLaunch",
    "close_router",
    "dashboard_hooks",
    "get_router",
    "install_dashboard_hooks",
    "install_turn_starter",
    "is_observe_turn",
    "launch_from_turn",
    "observe_from_turn",
    "STALE_RUNNING_PAUSE_REASON",
    "session_binding",
]
