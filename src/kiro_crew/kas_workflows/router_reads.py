"""The KAS workflow router's requests and reads: rows, recipes, run lists, step transcripts.

Each function here whose first parameter is ``self`` is a
:class:`~kiro_crew.kas_workflows.router.KasWorkflowRouter` method: the class binds it
by name (``run = _r_reads.run``), so it reads and writes the router's own state, and a
patch of the method on the class or on an instance reaches every caller. Nothing here
redacts: text a person sees is redacted in ``router.py``, the module the security
posture registry names as its sink.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.kas_workflows import wire
from kiro_crew.kas_workflows.router_base import (
    LEDGER_MAX_RUNS,
    KasWorkflowError,
    StepNotStarted,
    _alive,
    _gate_node_actions,
    _project,
    _strip_node_actions,
)

if TYPE_CHECKING:
    from kiro_crew.kas_workflows.router import KasWorkflowRouter

#: The router's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.kas_workflows.router")


async def _call(
    self: KasWorkflowRouter, runtime: Any, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await runtime.extension_request(method, params, timeout=self._timeout)
    except Exception as exc:
        short = method.rsplit("/", 1)[-1]
        raise KasWorkflowError(f"Kiro Agent refused {short}: {exc}") from exc


async def _reader(self: KasWorkflowRouter) -> Any:
    """A live KAS process for parentless reads, spawning one if none is up."""
    for runtime in list(self._runtimes):
        if runtime.is_alive():
            return runtime
    async with self._query_lock:
        runtime = self._query_runtime
        if runtime is not None and runtime.is_alive():
            return runtime
        try:
            runtime = await self._query_factory()
        except Exception as exc:
            raise KasWorkflowError(f"Kiro Agent could not start: {exc}") from exc
        self._query_runtime = runtime
        return runtime


def _row(self: KasWorkflowRouter, workflow_id: str) -> dict[str, Any] | None:
    plan_waiting = any(a.is_plan and a.workflow_id == workflow_id for a in self._pending.values())
    row = self.mirror.row(workflow_id, plan_waiting=plan_waiting)
    if row is None:
        return None
    driven = _alive(self._run_runtime.get(workflow_id)) is not None
    server = (
        _alive(self._run_runtime.get(workflow_id))
        if driven
        else (
            self._finished_runtime(workflow_id)
            if row.get("status") in wire.TERMINAL_RUN_STATUSES
            else None
        )
    )
    reachable = server is not None
    if not reachable:
        # KAS would accept these, but only on the process that holds the
        # run: until the run's chat reconnects nothing here can send them.
        # A finished run may still be deleted from any process.
        row["actions"] = [a for a in row.get("actions", []) if a == wire.RUN_ACTION_DELETE]
        _strip_node_actions(row.get("nodes") or [])
    else:
        # Only what the serving Kiro Agent advertised (gap 20).
        methods = wire.extension_methods(getattr(server, "_agent_capabilities", None))
        if methods is not None:
            row["actions"] = wire.gate_actions(list(row.get("actions", [])), methods)
            _gate_node_actions(row.get("nodes") or [], methods)
    return {**row, "slotKey": self.slot_of(workflow_id), "driven": driven}


def run_row(self: KasWorkflowRouter, workflow_id: str) -> dict[str, Any] | None:
    """One run's row as every surface reads it (actions gated on its driver)."""
    return self._row(workflow_id)


def rows(self: KasWorkflowRouter) -> list[dict[str, Any]]:
    out = []
    for row in self.mirror.rows():
        wid = row.get("workflowId")
        if isinstance(wid, str):
            out.append(self._row(wid) or row)
    return out


async def recipes(self: KasWorkflowRouter, cwd: str) -> list[dict[str, Any]]:
    runtime = await self._reader()
    project = _project(cwd)
    response = await self._call(runtime, wire.METHOD_LIST_RECIPES, {"workspacePaths": [project]})
    return [r.to_payload(project) for r in wire.parse_recipes(response)]


async def runs(self: KasWorkflowRouter, cwd: str) -> list[dict[str, Any]]:
    """Every run in *cwd*'s bucket, from disk. Nothing is loaded or resumed.

    The answer is the project's whole run list, so a run an earlier list of
    this project returned and this one omits was deleted elsewhere (the IDE,
    another process) and is dropped here too (gap 19), unless a process of
    this gateway still drives it.
    """
    runtime = await self._reader()
    project = _project(cwd)
    response = await self._call(runtime, wire.METHOD_LIST, {"workspacePaths": [project]})
    summaries = wire.parse_run_list(response)
    changed, forgotten = self.mirror.apply_scoped_list(
        project,
        summaries,
        keep=lambda wid: _alive(self._run_runtime.get(wid)) is not None,
    )
    if changed or forgotten:
        self.revision += 1
    await self.ensure_ledger()
    for wid in forgotten:
        self._forget_deleted(wid)
    for summary in summaries:
        if summary.status in wire.TERMINAL_RUN_STATUSES:
            self._note_ended(summary.workflow_id)
    ids = {s.workflow_id for s in summaries}
    return [row for row in self.rows() if row.get("workflowId") in ids]


def _forget_deleted(self: KasWorkflowRouter, workflow_id: str) -> None:
    """A run deleted outside this gateway: drop what is held for it here."""
    self._run_runtime.pop(workflow_id, None)
    self._inspected.discard(workflow_id)
    for key in [k for k, a in self._pending.items() if a.workflow_id == workflow_id]:
        self._pending.pop(key, None)
        self._decided_under.pop(key, None)
        self._retire(key)
    for key in [k for k, q in self._questions.items() if q.workflow_id == workflow_id]:
        self._questions.pop(key, None)
        self._retire(key)
    # Kept in the ledger (it names the chat), but never chased by a reconnect.
    self._note_ended(workflow_id)


async def step_transcript(
    self: KasWorkflowRouter, workflow_id: str, node_id: str, path: str | None = None
) -> dict[str, Any]:
    """One step's own conversation (the IDE's step chat), read-only.

    *path* (root-relative runtime path, e.g. ``loop/iter-1/code``) picks one
    instance of a node a loop runs more than once; without it the latest
    instance of *node_id* is read.

    Only a step of a run this router knows (mirrored, or in the ledger) is
    served, and its session id comes from KAS's own run state, never from the
    caller. A run whose step sessions are not mirrored yet (after a gateway
    restart) is read once with ``inspect`` first. Raises
    :class:`KasWorkflowError` when the step has not started.
    """
    from kiro_crew.kas_workflows.transcript import StepTranscriptError, read_step_transcript

    await self.ensure_ledger()
    if self.mirror.status(workflow_id) is None and workflow_id not in self._ledger:
        raise KasWorkflowError("That workflow run is not known here.")

    def lookup() -> str | None:
        # A path names one instance (a loop body's iteration); a bare id
        # names the latest instance, which is all a path-less caller means.
        if path:
            return self.mirror.step_session_at(workflow_id, path)
        return self.mirror.step_session(workflow_id, node_id)

    session_id = lookup()
    reader = await self._reader() if session_id is None else None
    if session_id is None and self._serves(reader, wire.METHOD_INSPECT):
        try:
            response = await self._call(reader, wire.METHOD_INSPECT, {"workflowId": workflow_id})
        except KasWorkflowError:
            response = None
        if response is not None and self.mirror.apply_load(response) is not None:
            self.revision += 1
        session_id = lookup()
    if session_id is None:
        raise StepNotStarted("This step has not started.")
    try:
        return await asyncio.to_thread(read_step_transcript, workflow_id, node_id, session_id)
    except StepTranscriptError as exc:
        raise StepNotStarted(str(exc)) from exc


async def chat_runs(
    self: KasWorkflowRouter, slot_key: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Every run *slot_key*'s chat parents, reloaded read-only when not mirrored.

    The mirror is memory: after a gateway restart it holds only runs that
    have spoken since. The ledger survives the restart, so a chat's own
    runs are read back from it with ``inspect`` (KAS's read-only snapshot,
    which falls back to disk for a run no process holds) -- the newest
    *limit* of them, once each. Never ``load``: that takes a run over.
    """
    await self.ensure_ledger()
    mine = sorted(
        (w for w, e in self._ledger.items() if e["slot"] == slot_key),
        key=lambda w: self._ledger[w]["createdAt"],
        reverse=True,
    )[:limit]
    missing = [w for w in mine if w not in self._inspected and not self.mirror.has_plan(w)]
    reader = await self._reader() if missing else None
    if missing and self._serves(reader, wire.METHOD_INSPECT):
        for workflow_id in missing:
            self._inspected.add(workflow_id)
            while len(self._inspected) > LEDGER_MAX_RUNS:
                self._inspected.pop()
            try:
                response = await self._call(
                    reader, wire.METHOD_INSPECT, {"workflowId": workflow_id}
                )
            except KasWorkflowError:
                logger.debug("chat run %s could not be inspected", workflow_id)
                continue
            if self.mirror.apply_load(response) is not None:
                self.revision += 1
            if self.mirror.status(workflow_id) in wire.TERMINAL_RUN_STATUSES:
                self._note_ended(workflow_id)
    return [row for row in (self._row(w) for w in mine) if row is not None]


def run(self: KasWorkflowRouter, workflow_id: str) -> dict[str, Any]:
    row = self._row(workflow_id)
    if row is None:
        raise KasWorkflowError("No run with that id is known; list its project first.")
    return {
        **row,
        "approvals": self.approvals(workflow_id),
        "questions": self.questions(workflow_id),
    }
