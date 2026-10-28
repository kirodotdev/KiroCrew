"""The in-memory mirror of KAS workflow runs.

KAS is the only durable store for runs. This mirror holds one row per run, built
from three kinds of input, and every dashboard surface reads its rows:

- ``_kiro/workflow/list`` summaries (boot, reconnect, a project scan),
- ``_kiro/workflow/load`` / ``inspect`` answers (the full node tree),
- lifecycle notifications as they stream.

It is a pure reducer: no I/O and no locks. The owner calls it from one event
loop and publishes :meth:`RunMirror.row` after each change it reports. Inputs
can arrive in any order (a notification before the list that names its run, a
stale summary after a newer notification), so each input only moves what it
actually knows and never erases what it does not carry.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from kiro_crew.kas_workflows import wire

#: Terminal runs kept per mirror. Live runs are never evicted; past runs beyond
#: this are dropped oldest-first and stay listable from KAS on demand.
MAX_TERMINAL_RUNS = 200
#: Node instances kept per run (a long loop unrolls one body per iteration).
MAX_PATHS_PER_RUN = 2000
#: Project scopes whose last list membership is remembered (gap 19).
MAX_LIST_SCOPES = 64
#: Longest KAS text kept on a node's retry-wait line or a plan update reason.
MAX_REASON_CHARS = 1000

logger = logging.getLogger(__name__)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Run:
    workflow_id: str
    status: str
    name: str
    workflow_name: str | None = None
    parent_session_id: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    updated_at: str | None = None
    pause_pending: bool = False
    pause_reason: str | None = None
    #: KAS's structured park target (an exhausted loop), its ``pauseDetail``
    #: class, and whether a person paused or stopped the run.
    pause_info: wire.PauseInfo | None = None
    pause_class: str | None = None
    user_stopped: bool = False
    attention: tuple[wire.Attention, ...] = ()
    plan: tuple[wire.NodeDescriptor, ...] = ()
    node_status: dict[str, str] = field(default_factory=dict)
    node_session: dict[str, str] = field(default_factory=dict)
    node_iteration: dict[str, int] = field(default_factory=dict)
    #: Repeat nodes: the budget after a grant (``extendedMaxIterations``), by id.
    node_loop_max: dict[str, int] = field(default_factory=dict)
    #: Per-step model / effort KAS resolved at run time (NodeState), when known.
    node_model: dict[str, str] = field(default_factory=dict)
    node_effort: dict[str, str] = field(default_factory=dict)
    node_reason: dict[str, str] = field(default_factory=dict)
    #: Every node INSTANCE by its root-relative runtime path ("a/iter-1/b"):
    #: ``{nodeId, status?, sessionId?, reason?}``. A loop body's node recurs once
    #: per iteration under one bare id; the path tells the instances apart.
    path_state: dict[str, dict[str, str]] = field(default_factory=dict)
    queued_steps: tuple[wire.NodeDescriptor, ...] = ()
    #: Queued plan replacements KAS applied, and the latest resolution:
    #: ``(outcome, reason, at)``.
    plan_revision: int = 0
    last_plan_update: tuple[str, str | None, str] | None = None
    #: Steps waiting out KAS's transient-retry backoff, by bare id:
    #: ``(reason, since)``. The step stays ``running`` meanwhile.
    node_retry_wait: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Per-step start / end stamps (ISO), by bare id (latest instance).
    node_started: dict[str, str] = field(default_factory=dict)
    node_ended: dict[str, str] = field(default_factory=dict)
    #: Watch nodes: the last poll's ``(outcome, at)``, by bare id.
    node_watch: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    #: KAS's own step counts from the latest list row or notification that
    #: carried them; None while unknown and once the run is terminal.
    progress: wire.RunProgress | None = None
    revision: int = 0


class RunMirror:
    """One row per KAS run, keyed by ``workflowId``."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._runs: dict[str, _Run] = {}
        self._clock = clock
        #: The run ids each list scope (a project) last returned, for
        #: :meth:`apply_scoped_list`.
        self._scope_members: dict[str, frozenset[str]] = {}

    # ── Reading ──

    def __contains__(self, workflow_id: object) -> bool:
        return workflow_id in self._runs

    def __len__(self) -> int:
        return len(self._runs)

    def status(self, workflow_id: str) -> str | None:
        run = self._runs.get(workflow_id)
        return run.status if run else None

    def step_session(self, workflow_id: str, node_id: str) -> str | None:
        """The ACP session one step of a known run runs in, once it has started."""
        run = self._runs.get(workflow_id)
        return run.node_session.get(node_id) if run else None

    def step_session_at(self, workflow_id: str, path: str) -> str | None:
        """The session of the node INSTANCE at root-relative runtime *path*."""
        run = self._runs.get(workflow_id)
        entry = run.path_state.get(path.strip(wire.NODE_PATH_SEP)) if run else None
        return entry.get("sessionId") if entry else None

    def path_of_step_session(self, workflow_id: str, session_id: str) -> str | None:
        """The runtime path of the node instance that runs in *session_id*, if known."""
        run = self._runs.get(workflow_id)
        if run is None:
            return None
        for path, entry in run.path_state.items():
            if entry.get("sessionId") == session_id:
                return path
        return None

    def node_state_at(self, workflow_id: str, path: str) -> Mapping[str, str] | None:
        """The recorded state of the node instance at *path* (a copy), if known."""
        run = self._runs.get(workflow_id)
        entry = run.path_state.get(path.strip(wire.NODE_PATH_SEP)) if run else None
        return dict(entry) if entry else None

    def step_awaits_reply(self, workflow_id: str, session_id: str) -> bool:
        """Whether the step in *session_id* is parked for the person's next message.

        KAS lists such a wait as a ``question`` attention entry with no
        ``toolCallId`` (a ``warning`` signal, an interactive step's plain reply,
        an unmet completion gate); a paused step instance counts too. A question
        WITH a tool call id is a pending request, answered on that request, not
        by a message.
        """
        run = self._runs.get(workflow_id)
        if run is None or run.status in wire.TERMINAL_RUN_STATUSES:
            return False
        for entry in run.attention:
            if (
                entry.kind == wire.ATTENTION_QUESTION
                and entry.tool_call_id is None
                and entry.session_id == session_id
            ):
                return True
        for state in run.path_state.values():
            if state.get("sessionId") == session_id:
                return state.get("status") == wire.STATUS_PAUSED
        node_id = next((n for n, s in run.node_session.items() if s == session_id), None)
        return node_id is not None and run.node_status.get(node_id) == wire.STATUS_PAUSED

    def has_plan(self, workflow_id: str) -> bool:
        """Whether the run's step tree is known (from a load, inspect or creation)."""
        run = self._runs.get(workflow_id)
        return bool(run and run.plan)

    def pause_info(self, workflow_id: str) -> wire.PauseInfo | None:
        """KAS's structured park target for a paused run (an exhausted loop), if any."""
        run = self._runs.get(workflow_id)
        if run is None or run.status != wire.STATUS_PAUSED:
            return None
        return run.pause_info

    def instance_status(self, workflow_id: str, node_id: str, path: str | None) -> str | None:
        """The status of one node instance: by *path* when given, else by bare id."""
        run = self._runs.get(workflow_id)
        if run is None:
            return None
        if path:
            entry = run.path_state.get(path.strip(wire.NODE_PATH_SEP))
            return entry.get("status") if entry else None
        return run.node_status.get(node_id)

    def node_ids_at(self, workflow_id: str, path: str) -> str | None:
        """The bare node id the instance at *path* belongs to, if known."""
        run = self._runs.get(workflow_id)
        entry = run.path_state.get(path.strip(wire.NODE_PATH_SEP)) if run else None
        return entry.get("nodeId") if entry else None

    def has_failed_nodes(self, workflow_id: str) -> bool:
        run = self._runs.get(workflow_id)
        return run is not None and _has_failed_nodes(run)

    def parent_of(self, workflow_id: str) -> str | None:
        run = self._runs.get(workflow_id)
        return run.parent_session_id if run else None

    def run_for_step_session(self, session_id: str) -> tuple[str, str] | None:
        """The (workflowId, nodeId) whose step runs in *session_id*, if known."""
        for run in self._runs.values():
            for node_id, sid in run.node_session.items():
                if sid == session_id:
                    return run.workflow_id, node_id
        return None

    def row(self, workflow_id: str, *, plan_waiting: bool = False) -> dict[str, Any] | None:
        """The full row payload every surface renders, or None for an unknown run.

        *plan_waiting*: the router holds a plan approval for this run, so a
        paused run reads as waiting for that approval.
        """
        run = self._runs.get(workflow_id)
        return _row_payload(run, plan_waiting=plan_waiting) if run else None

    def rows(self, *, parent_session_id: str | None = None) -> list[dict[str, Any]]:
        """Every row, in display order, optionally only one chat's runs.

        Order: runs needing attention, then other live runs, newest first, then
        terminal runs by when they ended.
        """
        runs = [
            run
            for run in self._runs.values()
            if parent_session_id is None or run.parent_session_id == parent_session_id
        ]
        runs.sort(key=_recency, reverse=True)
        runs.sort(key=_group)
        return [_row_payload(run) for run in runs]

    # ── Writing ──

    def forget(self, workflow_id: str) -> bool:
        """Drop a run (it was deleted in KAS). True when one was held."""
        return self._runs.pop(workflow_id, None) is not None

    def apply_summaries(self, summaries: Iterable[wire.RunSummary]) -> list[str]:
        """Fold ``list`` rows in; returns the ids whose row changed."""
        changed = [s.workflow_id for s in summaries if self._apply_summary(s)]
        self._evict()
        return changed

    def apply_scoped_list(
        self,
        scope: str,
        summaries: Iterable[wire.RunSummary],
        *,
        keep: Callable[[str], bool] = lambda _wid: False,
    ) -> tuple[list[str], list[str]]:
        """Fold one scope's (a project's) FULL ``list`` in, treating it as membership.

        KAS's list for a scope names every run in it, so a held run that an
        earlier list of the SAME scope returned and this one omits was deleted
        elsewhere (the IDE, another process) and is forgotten -- unless *keep*
        says this gateway still drives it (a list read from disk can lag a run
        this process just created). A run never listed under this scope is left
        alone: absence from one project's list says nothing about another's.
        Returns ``(changed, forgotten)`` ids.
        """
        items = list(summaries)
        changed = self.apply_summaries(items)
        listed = frozenset(s.workflow_id for s in items)
        before = self._scope_members.get(scope, frozenset())
        forgotten = [wid for wid in sorted(before - listed) if wid in self._runs and not keep(wid)]
        for wid in forgotten:
            self._runs.pop(wid, None)
        self._scope_members.pop(scope, None)
        self._scope_members[scope] = listed
        while len(self._scope_members) > MAX_LIST_SCOPES:
            self._scope_members.pop(next(iter(self._scope_members)))
        return changed, forgotten

    def apply_load(self, response: Any) -> str | None:
        """Fold a ``load`` or ``inspect`` answer in; returns its id when it changed."""
        body = wire.as_mapping(response)
        if body is None:
            return None
        workflow_id = wire.as_str(body.get("workflowId"))
        state = wire.as_mapping(body.get("state")) or wire.as_mapping(body.get("finalState"))
        if workflow_id is None or state is None:
            return None
        status = wire.as_str(state.get("status"))
        if status not in wire.RUN_STATUSES:
            return None
        run = self._ensure(workflow_id, status=status, name=_state_name(state, workflow_id))
        before = _fingerprint(run)
        self._apply_state(run, state)
        plan = wire.parse_plan(body.get("nodePlan"))
        if plan:
            run.plan = plan
        for raw in wire.as_list(body.get("stepSessions")):
            item = wire.as_mapping(raw)
            node_id = wire.as_str(item.get("nodeId")) if item else None
            session_id = wire.as_str(item.get("sessionId")) if item else None
            if item and node_id and session_id:
                run.node_session[node_id] = session_id
                _note_path(run, _event_path(run, item), node_id=node_id, session_id=session_id)
        return self._touched(run, before)

    def apply_notification(self, method: str, params: Any) -> str | None:
        """Fold one lifecycle notification in; returns its id when the row changed.

        An unknown method, or one naming no run, is ignored.
        """
        if method not in wire.RUN_NOTIFICATIONS:
            return None
        body = wire.as_mapping(params)
        workflow_id = wire.as_str(body.get("workflowId")) if body else None
        if body is None or workflow_id is None:
            return None
        if method == wire.NOTIFY_RUN_START:
            name = wire.as_str(body.get("workflowName")) or workflow_id
            run = self._ensure(workflow_id, status=wire.STATUS_RUNNING, name=name)
        else:
            existing = self._runs.get(workflow_id)
            if existing is None:
                # A notification for a run this mirror has not listed yet. Hold
                # a placeholder so the stream is not lost; the next list or load
                # fills in its name and creation time.
                existing = self._ensure(workflow_id, status=wire.STATUS_RUNNING, name=workflow_id)
            run = existing
        before = _fingerprint(run)
        now = _iso(self._clock())
        run.updated_at = now
        parent = wire.as_str(body.get("parentSessionId"))
        if parent:
            _set_parent(run, parent)
        if "attention" in body:
            run.attention = wire.parse_attention(body.get("attention"))
        handler = _HANDLERS[method]
        handler(run, body, now)
        progress = wire.parse_run_progress(body)
        if progress is not None:
            run.progress = progress
        changed = self._touched(run, before)
        if run.status in wire.TERMINAL_RUN_STATUSES:
            self._evict()
        return changed

    # ── Internals ──

    def _ensure(self, workflow_id: str, *, status: str, name: str) -> _Run:
        run = self._runs.get(workflow_id)
        if run is None:
            # revision -1 marks a row nobody has published yet; _touched reports
            # it as changed even when the input carried nothing beyond its id.
            run = _Run(workflow_id=workflow_id, status=status, name=name, revision=-1)
            self._runs[workflow_id] = run
        return run

    def _apply_summary(self, summary: wire.RunSummary) -> bool:
        run = self._ensure(summary.workflow_id, status=summary.status, name=summary.name)
        before = _fingerprint(run)
        run.name = summary.name
        run.workflow_name = summary.workflow_name or run.workflow_name
        _set_parent(run, summary.parent_session_id)
        run.created_at = summary.created_at or run.created_at
        run.started_at = summary.started_at or run.started_at
        if _stale_live(run, summary.status):
            # A list read before the run's end, applied after it. Only a
            # lifecycle event (``run_start``) brings a finished run back.
            return self._touched(run, before) is not None
        run.status = summary.status
        run.ended_at = summary.ended_at or run.ended_at
        run.updated_at = summary.updated_at or run.updated_at
        run.pause_pending = summary.pause_pending and summary.status == wire.STATUS_RUNNING
        run.attention = summary.attention
        if summary.progress is not None:
            run.progress = summary.progress
        if summary.status != wire.STATUS_PAUSED:
            run.pause_info = None
        elif summary.pause_kind and summary.pause_node_id:
            # A list row flattens ``pause`` to kind + node; keep the budget an
            # inspect or a ``paused`` event already gave for the same park.
            held = run.pause_info
            if held is None or held.node_id != summary.pause_node_id:
                run.pause_info = wire.PauseInfo(summary.pause_kind, summary.pause_node_id)
        return self._touched(run, before) is not None

    def _apply_state(self, run: _Run, state: Mapping[str, Any]) -> None:
        run.name = _state_name(state, run.name)
        run.workflow_name = wire.as_str(state.get("workflowName")) or run.workflow_name
        _set_parent(run, wire.as_str(state.get("parentSessionId")))
        run.created_at = wire.as_str(state.get("createdAt")) or run.created_at
        if _stale_live(run, wire.as_str(state.get("status"))):
            # An inspect/load snapshot taken before the run's end: its node
            # statuses and waits are older than the end the mirror holds.
            return
        run.status = wire.as_str(state.get("status")) or run.status
        run.pause_reason = wire.as_str(state.get("pauseReason"))
        _apply_pause_fields(run, state)
        run.pause_pending = (
            wire.as_mapping(state.get("pausePending")) is not None
            and run.status == wire.STATUS_RUNNING
        )
        if "attention" in state:
            run.attention = wire.parse_attention(state.get("attention"))
        root = wire.as_mapping(state.get("root"))
        if root is not None:
            _walk_node_state(run, root, depth=0)
            # WorkflowState carries no run-level start/end: the root node's are
            # the run's, which is what an elapsed time and "ended" sort need.
            run.started_at = wire.as_str(root.get("startedAt")) or run.started_at
            if run.status in wire.TERMINAL_RUN_STATUSES:
                run.ended_at = wire.as_str(root.get("endedAt")) or run.ended_at

    def _touched(self, run: _Run, before: tuple) -> str | None:
        if run.status not in wire.TERMINAL_RUN_STATUSES:
            run.ended_at = None
        else:
            # KAS counts only running or paused runs; a finished run has none.
            run.progress = None
        if _fingerprint(run) == before and run.revision >= 0:
            return None
        run.revision += 1
        return run.workflow_id

    def _evict(self) -> None:
        terminal = [r for r in self._runs.values() if r.status in wire.TERMINAL_RUN_STATUSES]
        if len(terminal) <= MAX_TERMINAL_RUNS:
            return
        terminal.sort(key=lambda r: r.ended_at or r.updated_at or r.created_at or "")
        for run in terminal[: len(terminal) - MAX_TERMINAL_RUNS]:
            self._runs.pop(run.workflow_id, None)


# ── Notification handlers ──


def _on_run_start(run: _Run, body: Mapping[str, Any], now: str) -> None:
    # KAS emits run_start on EVERY entry into its run loop, a resume included (a
    # parked step's reply, a grant, a person's resume), and its nodeTree carries no
    # node statuses (test/fixtures/acp_frames/kas/workflow-run-live.jsonl: the
    # second run_start follows the reply to the parked step). Only a run that had
    # ENDED starts over from scratch, so only then are its node states dropped.
    restarted = run.status in wire.TERMINAL_RUN_STATUSES
    run.status = wire.STATUS_RUNNING
    run.workflow_name = wire.as_str(body.get("workflowName")) or run.workflow_name
    run.started_at = run.started_at or now
    run.created_at = run.created_at or now
    run.ended_at = None
    run.pause_pending = False
    run.pause_reason = None
    _clear_pause(run)
    plan = wire.parse_plan(body.get("nodeTree"))
    if plan:
        run.plan = plan
    if plan and restarted:
        # A restart from scratch re-sends the tree; every node is pending again.
        run.node_status.clear()
        run.node_reason.clear()
        run.node_iteration.clear()
        run.node_loop_max.clear()
        run.node_retry_wait.clear()
        run.node_started.clear()
        run.node_ended.clear()
        run.node_watch.clear()
        run.path_state.clear()


def _on_node_start(run: _Run, body: Mapping[str, Any], now: str) -> None:
    node_id = wire.as_str(body.get("nodeId"))
    if node_id is None:
        return
    path = _event_path(run, body)
    # A node_start that ends a retry-wait backoff re-drives the SAME attempt:
    # its start stamp stays. Any other start is a new attempt.
    resumed = node_id in run.node_retry_wait or (
        run.node_status.get(node_id) == wire.STATUS_RUNNING and node_id in run.node_started
    )
    run.node_retry_wait.pop(node_id, None)
    run.node_status[node_id] = wire.STATUS_RUNNING
    run.node_reason.pop(node_id, None)
    if not resumed:
        run.node_started[node_id] = wire.as_str(body.get("startedAt")) or now
    run.node_ended.pop(node_id, None)
    session_id = wire.as_str(body.get("sessionId"))
    if session_id:
        run.node_session[node_id] = session_id
    _note_path(
        run,
        path,
        node_id=node_id,
        status=wire.STATUS_RUNNING,
        session_id=session_id,
        clear_reason=True,
    )
    entry = run.path_state.get(wire.NODE_PATH_SEP.join(path))
    if entry is not None:
        had_wait = entry.pop("retryWait", None) is not None
        entry.pop("retrySince", None)
        if "startedAt" not in entry or not (had_wait or resumed):
            entry["startedAt"] = wire.as_str(body.get("startedAt")) or now
        entry.pop("endedAt", None)
    iteration = wire.as_count(body.get("iteration"))
    if iteration is not None:
        run.node_iteration[node_id] = iteration
    if run.status == wire.STATUS_PAUSED and not run.pause_pending:
        run.status = wire.STATUS_RUNNING
    if run.status == wire.STATUS_RUNNING:
        _clear_pause(run)


def _on_node_complete(run: _Run, body: Mapping[str, Any], now: str) -> None:
    node_id = wire.as_str(body.get("nodeId"))
    status = wire.as_str(body.get("status"))
    if node_id is None or status not in wire.NODE_STATUSES:
        return
    run.node_status[node_id] = status
    run.node_retry_wait.pop(node_id, None)
    ended = wire.as_str(body.get("endedAt")) or now
    run.node_ended[node_id] = ended
    reason = wire.as_str(body.get("failureReason"))
    if reason:
        run.node_reason[node_id] = reason
    else:
        run.node_reason.pop(node_id, None)
    path = _event_path(run, body)
    _note_path(
        run,
        path,
        node_id=node_id,
        status=status,
        reason=reason,
        clear_reason=True,
    )
    _settle_path(run, path, ended=ended)


def _on_node_paused(run: _Run, body: Mapping[str, Any], now: str) -> None:
    node_id = wire.as_str(body.get("nodeId"))
    if node_id is None:
        return
    reason = wire.as_str(body.get("reason"))
    path = _event_path(run, body)
    if wire.as_str(body.get("kind")) == wire.NODE_PAUSED_RETRY_WAIT:
        # INFORMATIONAL: KAS waits out a transient-error backoff and keeps
        # driving the step. Its status stays running (no Complete / Fail /
        # Reply on a live step); only the backoff line is recorded.
        text = (reason or "")[:MAX_REASON_CHARS]
        run.node_retry_wait[node_id] = (text, now)
        run.node_status.setdefault(node_id, wire.STATUS_RUNNING)
        _note_path(run, path, node_id=node_id)
        entry = run.path_state.get(wire.NODE_PATH_SEP.join(path))
        if entry is not None:
            entry.setdefault("status", wire.STATUS_RUNNING)
            entry["retryWait"] = text
            entry["retrySince"] = now
        return
    run.node_retry_wait.pop(node_id, None)
    run.node_status[node_id] = wire.STATUS_PAUSED
    if reason:
        run.node_reason[node_id] = reason
    _note_path(run, path, node_id=node_id, status=wire.STATUS_PAUSED, reason=reason)
    _settle_path(run, path, ended=None)


def _on_loop_iteration(run: _Run, body: Mapping[str, Any], now: str) -> None:
    loop_id = wire.as_str(body.get("loopId"))
    iteration = wire.as_count(body.get("iteration"))
    if loop_id and iteration is not None:
        run.node_iteration[loop_id] = iteration


def _on_watch_poll(run: _Run, body: Mapping[str, Any], now: str) -> None:
    node_id = wire.as_str(body.get("nodeId"))
    if not node_id:
        return
    run.node_status.setdefault(node_id, wire.STATUS_RUNNING)
    outcome = wire.as_str(body.get("outcome"))
    if outcome in wire.WATCH_OUTCOMES:
        at = wire.as_str(body.get("at")) or now
        run.node_watch[node_id] = (outcome, at)
        path = _event_path(run, body)
        _note_path(run, path, node_id=node_id)
        entry = run.path_state.get(wire.NODE_PATH_SEP.join(path))
        if entry is not None:
            entry["watchOutcome"] = outcome
            entry["watchAt"] = at


def _on_paused(run: _Run, body: Mapping[str, Any], now: str) -> None:
    run.status = wire.STATUS_PAUSED
    run.pause_pending = False
    run.pause_reason = wire.as_str(body.get("pauseReason"))
    run.pause_info = wire.parse_pause_info(body.get("pause"))
    run.pause_class = wire.pause_detail_class(body.get("pauseDetail"))
    # A paused event names its initiator only for an attributed pause; a step a
    # person stopped is stamped on the state, which the ``run_complete`` that
    # follows every park carries (``finalState.stopInitiator``).
    run.user_stopped = wire.as_str(body.get("initiator")) == wire.INITIATOR_USER


def _on_run_complete(run: _Run, body: Mapping[str, Any], now: str) -> None:
    status = wire.as_str(body.get("status"))
    if status not in wire.RUN_STATUSES:
        return
    run.status = status
    run.pause_pending = False
    final = wire.as_mapping(body.get("finalState"))
    if final is not None:
        root = wire.as_mapping(final.get("root"))
        if root is not None:
            _walk_node_state(run, root, depth=0)
        run.pause_reason = wire.as_str(final.get("pauseReason"))
        _apply_pause_fields(run, final)
    if status in wire.TERMINAL_RUN_STATUSES:
        run.ended_at = now
        run.attention = ()
        run.node_retry_wait.clear()
        for entry in run.path_state.values():
            entry.pop("retryWait", None)
            entry.pop("retrySince", None)
        _clear_pause(run)


def _on_steps_queued(run: _Run, body: Mapping[str, Any], now: str) -> None:
    pending = wire.parse_plan(body.get("pendingSteps"))
    resolution = wire.as_mapping(body.get("resolution"))
    outcome = wire.as_str(resolution.get("outcome")) if resolution else None
    if outcome in wire.PLAN_UPDATE_OUTCOMES:
        assert resolution is not None and outcome is not None
        reason = wire.as_str(resolution.get("reason"))
        run.last_plan_update = (outcome, reason[:MAX_REASON_CHARS] if reason else None, now)
    if outcome == wire.PLAN_UPDATE_APPLIED:
        if pending:
            # replace_remaining rewrites only the not-yet-run tail of the top level.
            started = tuple(node for node in run.plan if _subtree_started(run, node))
            run.plan = started + pending
        run.queued_steps = ()
        run.plan_revision += 1
    elif outcome in wire.PLAN_UPDATE_OUTCOMES:
        run.queued_steps = ()
    else:
        run.queued_steps = pending


_HANDLERS: dict[str, Callable[[_Run, Mapping[str, Any], str], None]] = {
    wire.NOTIFY_RUN_START: _on_run_start,
    wire.NOTIFY_NODE_START: _on_node_start,
    wire.NOTIFY_NODE_COMPLETE: _on_node_complete,
    wire.NOTIFY_NODE_PAUSED: _on_node_paused,
    wire.NOTIFY_LOOP_ITERATION: _on_loop_iteration,
    wire.NOTIFY_WATCH_POLL: _on_watch_poll,
    wire.NOTIFY_PAUSED: _on_paused,
    wire.NOTIFY_RUN_COMPLETE: _on_run_complete,
    wire.NOTIFY_STEPS_QUEUED: _on_steps_queued,
}


# ── Helpers ──


def _set_parent(run: _Run, parent: str | None) -> None:
    """Record a run's parent session; once known it is fixed (gap 29).

    KAS fixes a run's parent at ``_kiro/workflow/new``. An input naming a
    DIFFERENT parent later is a corrupt or colliding event, and following it
    would silently move the run (and its approvals and wake turns) to another
    chat, so it is ignored and logged.
    """
    if not parent:
        return
    if run.parent_session_id is None:
        run.parent_session_id = parent
    elif run.parent_session_id != parent:
        logger.warning(
            "workflow run %s: ignored a conflicting parent session (kept the first one)",
            run.workflow_id,
        )


def _stale_live(run: _Run, incoming: str | None) -> bool:
    """Whether a snapshot's *incoming* status would move a finished run back to live.

    Lists and inspect/load answers can be read before a run's end and applied
    after it. Only a lifecycle event (KAS's ``run_start``, sent on every
    resume, retry and restart) may bring a finished run back (gap 2).
    """
    return (
        run.status in wire.TERMINAL_RUN_STATUSES
        and incoming in wire.RUN_STATUSES
        and incoming not in wire.TERMINAL_RUN_STATUSES
    )


def _settle_path(run: _Run, path: tuple[str, ...], *, ended: str | None) -> None:
    """Stamp a node instance's end (or clear it for a park) and drop its backoff line."""
    entry = run.path_state.get(wire.NODE_PATH_SEP.join(path)) if path else None
    if entry is None:
        return
    entry.pop("retryWait", None)
    entry.pop("retrySince", None)
    if ended:
        entry["endedAt"] = ended
    else:
        entry.pop("endedAt", None)


def _apply_pause_fields(run: _Run, state: Mapping[str, Any]) -> None:
    """Copy a ``WorkflowState``'s park fields: ``pause``, ``pauseDetail``, the stop's initiator."""
    run.pause_info = wire.parse_pause_info(state.get("pause"))
    run.pause_class = wire.pause_detail_class(state.get("pauseDetail"))
    pending = wire.as_mapping(state.get("pausePending"))
    run.user_stopped = wire.as_str(state.get("stopInitiator")) == wire.INITIATOR_USER or (
        pending is not None and wire.as_str(pending.get("initiator")) == wire.INITIATOR_USER
    )


def _clear_pause(run: _Run) -> None:
    run.pause_info = None
    run.pause_class = None
    run.user_stopped = False


def _state_name(state: Mapping[str, Any], fallback: str) -> str:
    return (
        wire.as_str(state.get("runLabel"))
        or wire.as_str(state.get("name"))
        or wire.as_str(state.get("workflowName"))
        or fallback
    )


def _walk_node_state(
    run: _Run,
    node: Mapping[str, Any],
    *,
    depth: int,
    path: tuple[str, ...] = (),
    parent_type: str | None = None,
) -> None:
    """Copy a ``WorkflowState.root`` tree's per-node status into *run*.

    Bare-id maps keep the latest instance of a node (a loop body's last
    iteration); :attr:`_Run.path_state` keeps every instance by its runtime
    path, whose repeat segment is ``iter-<n>`` (KAS ``pathSegmentOf``).
    """
    if depth > wire.MAX_PLAN_DEPTH:
        return
    node_id = wire.as_str(node.get("id")) or wire.as_str(node.get("nodeId"))
    status = wire.as_str(node.get("status"))
    if node_id and status in wire.NODE_STATUSES:
        run.node_status[node_id] = status
    session_id = wire.as_str(node.get("sessionId"))
    if node_id and session_id:
        run.node_session[node_id] = session_id
    model = wire.as_str(node.get("modelId"))
    if node_id and model and model != "auto":
        run.node_model[node_id] = model
    effort = wire.as_str(node.get("effortLevel"))
    if node_id and effort:
        run.node_effort[node_id] = effort
    loop_max = wire.as_count(node.get("extendedMaxIterations"))
    if node_id and loop_max:
        run.node_loop_max[node_id] = loop_max
    started = wire.as_str(node.get("startedAt"))
    ended = wire.as_str(node.get("endedAt"))
    if node_id and depth > 0:
        if started:
            run.node_started[node_id] = started
        if ended and status not in (wire.STATUS_RUNNING, wire.STATUS_PAUSED):
            run.node_ended[node_id] = ended
        elif status in (wire.STATUS_RUNNING, wire.STATUS_PAUSED):
            run.node_ended.pop(node_id, None)
    iteration = wire.as_count(node.get("iteration"))
    here = path
    if depth > 0 and node_id:
        # The synthetic root (depth 0) contributes no segment.
        segment = (
            f"{wire.ITERATION_SEGMENT_PREFIX}{iteration}"
            if parent_type == "repeat" and iteration is not None
            else node_id
        )
        here = path + (segment,)
        _note_path(
            run,
            here,
            node_id=node_id,
            status=status if status in wire.NODE_STATUSES else None,
            session_id=session_id,
            reason=wire.as_str(node.get("failureReason")),
        )
        if loop_max and wire.NODE_PATH_SEP.join(here) in run.path_state:
            run.path_state[wire.NODE_PATH_SEP.join(here)]["loopMax"] = str(loop_max)
        entry = run.path_state.get(wire.NODE_PATH_SEP.join(here))
        if entry is not None:
            if started:
                entry["startedAt"] = started
            if ended and status not in (wire.STATUS_RUNNING, wire.STATUS_PAUSED):
                entry["endedAt"] = ended
            elif status in (wire.STATUS_RUNNING, wire.STATUS_PAUSED):
                entry.pop("endedAt", None)
    node_type = wire.as_str(node.get("type"))
    for key in ("children", "steps", "branches"):
        for child in wire.as_list(node.get(key)):
            item = wire.as_mapping(child)
            if item is not None:
                _walk_node_state(run, item, depth=depth + 1, path=here, parent_type=node_type)


def _note_path(
    run: _Run,
    path: tuple[str, ...],
    *,
    node_id: str,
    status: str | None = None,
    session_id: str | None = None,
    reason: str | None = None,
    clear_reason: bool = False,
) -> None:
    """Record one node instance's state under its runtime path."""
    key = wire.NODE_PATH_SEP.join(path)
    if not path or (key not in run.path_state and len(run.path_state) >= MAX_PATHS_PER_RUN):
        return
    entry = run.path_state.setdefault(key, {"nodeId": node_id})
    if status is not None:
        entry["status"] = status
    if session_id:
        entry["sessionId"] = session_id
    if reason:
        entry["reason"] = reason
    elif clear_reason:
        entry.pop("reason", None)


def _event_path(run: _Run, body: Mapping[str, Any]) -> tuple[str, ...]:
    """A lifecycle event's ``nodePath``, relative to the run's root (head dropped)."""
    path = wire.node_path(body.get("nodePath"))
    if path and path[0] == run.workflow_id:
        path = path[1:]
    return path


def _subtree_started(run: _Run, node: wire.NodeDescriptor) -> bool:
    if run.node_status.get(node.id, wire.NODE_STATUS_PENDING) != wire.NODE_STATUS_PENDING:
        return True
    return any(_subtree_started(run, child) for child in node.children)


def _fingerprint(run: _Run) -> tuple:
    return (
        run.status,
        run.name,
        run.workflow_name,
        run.parent_session_id,
        run.created_at,
        run.started_at,
        run.ended_at,
        run.pause_pending,
        run.pause_reason,
        run.pause_info,
        run.pause_class,
        run.user_stopped,
        run.attention,
        run.plan,
        tuple(sorted(run.node_status.items())),
        tuple(sorted(run.node_session.items())),
        tuple(sorted(run.node_iteration.items())),
        tuple(sorted(run.node_loop_max.items())),
        tuple(sorted(run.node_reason.items())),
        tuple(sorted((k, tuple(sorted(v.items()))) for k, v in run.path_state.items())),
        run.queued_steps,
        run.progress,
        run.plan_revision,
        run.last_plan_update,
        tuple(sorted(run.node_retry_wait.items())),
        tuple(sorted(run.node_started.items())),
        tuple(sorted(run.node_ended.items())),
        tuple(sorted(run.node_watch.items())),
    )


def _group(run: _Run) -> int:
    """0 = needs someone (the row's ``needsAttention``), 1 = other live runs, 2 = terminal."""
    if run.status in wire.TERMINAL_RUN_STATUSES:
        return 2
    pause = _pause_payload(run, False)
    kind = wire.attention_kind(run.status, run.attention, pause["reason"] if pause else None)
    return 0 if kind is not None else 1


def _recency(run: _Run) -> str:
    """ISO stamps sort lexically; a terminal run sorts by when it ended."""
    terminal = run.status in wire.TERMINAL_RUN_STATUSES
    return (run.ended_at if terminal else None) or run.created_at or run.updated_at or ""


def _progress(run: _Run) -> tuple[int, int] | None:
    steps = list(wire.iter_steps(run.plan))
    if not steps:
        return None
    done = sum(1 for s in steps if run.node_status.get(s.id) in wire.NODE_DONE_STATUSES)
    return done, len(steps)


def _node_payload(
    run: _Run,
    node: wire.NodeDescriptor,
    parent_path: tuple[str, ...] = (),
    *,
    in_iteration: bool = False,
) -> dict[str, Any]:
    """One plan node as a surface draws it.

    ``path`` is the node's root-relative runtime path. Outside a loop a node
    has one instance, so its bare-id state is its own. Inside a repeat
    iteration (*in_iteration*) the bare id names every iteration's instance,
    so only the path's own state is used. A repeat lists its ``iterations``,
    each with the body's nodes as that iteration ran them.
    """
    path = parent_path + (node.id,)
    entry = run.path_state.get(wire.NODE_PATH_SEP.join(path), {})
    if in_iteration:
        status = entry.get("status", wire.NODE_STATUS_PENDING)
        session_id = entry.get("sessionId")
        reason = entry.get("reason")
        iteration = None
        started_at = entry.get("startedAt")
        ended_at = entry.get("endedAt")
        retry = (entry["retryWait"], entry.get("retrySince")) if "retryWait" in entry else None
        watch = (entry["watchOutcome"], entry.get("watchAt")) if "watchOutcome" in entry else None
    else:
        status = run.node_status.get(node.id, wire.NODE_STATUS_PENDING)
        session_id = entry.get("sessionId") or run.node_session.get(node.id)
        reason = run.node_reason.get(node.id)
        iteration = run.node_iteration.get(node.id)
        started_at = entry.get("startedAt") or run.node_started.get(node.id)
        ended_at = entry.get("endedAt") or run.node_ended.get(node.id)
        retry = run.node_retry_wait.get(node.id)
        watch = run.node_watch.get(node.id)
    if status in (wire.STATUS_RUNNING, wire.STATUS_PAUSED, wire.NODE_STATUS_PENDING):
        ended_at = None
    if status != wire.STATUS_RUNNING or run.status in wire.TERMINAL_RUN_STATUSES:
        retry = None
    payload: dict[str, Any] = {
        "id": node.id,
        "path": wire.NODE_PATH_SEP.join(path),
        "type": node.type,
        "agent": node.agent,
        "label": node.label or node.id,
        "maxIterations": node.max_iterations,
        "status": status,
        "sessionId": session_id,
        "iteration": iteration,
        "reason": reason,
        "model": run.node_model.get(node.id) or node.model,
        "effort": run.node_effort.get(node.id) or node.effort,
        "include": node.include_payload(),
        "startedAt": started_at,
        "endedAt": ended_at,
        "retryWait": {"reason": retry[0], "since": retry[1]} if retry else None,
        "watchOutcome": (
            {"outcome": watch[0], "at": watch[1]} if watch and node.type == "watch" else None
        ),
        "waitingFor": _waiting_for(run, node.id, path, in_iteration=in_iteration),
        "replacementPending": bool(
            not parent_path and run.queued_steps and not _subtree_started(run, node)
        ),
        "actions": _node_actions(run, node, status, session_id),
        "children": (
            []
            if node.type == "watch"
            else [
                _node_payload(run, child, path, in_iteration=in_iteration)
                for child in node.children
            ]
        ),
    }
    if node.type == "repeat":
        iterations = [
            {
                "iteration": n,
                "path": wire.NODE_PATH_SEP.join(path + (segment,)),
                "status": _iteration_status(run, path + (segment,)),
                "children": [
                    _node_payload(run, child, path + (segment,), in_iteration=True)
                    for child in node.children
                ],
            }
            for n, segment in _iterations_of(run, path)
        ]
        payload["iterations"] = iterations
        payload["loop"] = _loop_payload(run, node, entry, iterations, in_iteration=in_iteration)
    return payload


def _loop_payload(
    run: _Run,
    node: wire.NodeDescriptor,
    entry: Mapping[str, str],
    iterations: list[dict[str, Any]],
    *,
    in_iteration: bool,
) -> dict[str, int] | None:
    """A repeat's ``{done, max}``: passes finished and the budget now in force.

    The budget is KAS's effective one: a grant's ``extendedMaxIterations``,
    else the declared ``maxIterations``. While the run is parked on this loop's
    exhausted budget, KAS's own ``pause`` numbers win.
    """
    pause = run.pause_info
    if (
        pause is not None
        and pause.kind == wire.PAUSE_KIND_MAX_ITERATIONS
        and pause.node_id == node.id
        and pause.max_iterations is not None
        and run.status == wire.STATUS_PAUSED
    ):
        done = pause.iterations_run if pause.iterations_run is not None else pause.max_iterations
        return {"done": done, "max": pause.max_iterations}
    raw_max = entry.get("loopMax")
    budget = int(raw_max) if raw_max and raw_max.isdigit() else None
    if budget is None and not in_iteration:
        budget = run.node_loop_max.get(node.id)
    if budget is None:
        budget = node.max_iterations
    if budget is None:
        return None
    done = sum(1 for it in iterations if it["status"] in wire.NODE_DONE_STATUSES)
    return {"done": done, "max": budget}


def _node_actions(
    run: _Run, node: wire.NodeDescriptor, status: str, session_id: str | None
) -> list[str]:
    """What a person may do to this node instance now (KAS's own state rules).

    - The repeat the run is parked on at ``maxIterations``: ``grant`` more
      passes (``resume`` + ``extendRepeat``) or ``complete`` it ("end the loop
      and continue", ``update_status`` + ``nodeId``; KAS accepts ``completed``
      only there, so no ``fail``).
    - A paused step: ``complete`` / ``fail`` (``update_status`` + ``nodeId``),
      and ``reply`` (a prompt on its session re-drives it).
    - A running step with a session: ``cancel`` (``session/cancel`` on the step's
      session parks that one step; siblings keep running).
    """
    if run.status in wire.TERMINAL_RUN_STATUSES:
        return []
    if node.type == "repeat":
        pause = run.pause_info
        if (
            run.status == wire.STATUS_PAUSED
            and status == wire.STATUS_PAUSED
            and pause is not None
            and pause.kind == wire.PAUSE_KIND_MAX_ITERATIONS
            and pause.node_id == node.id
        ):
            return [wire.NODE_ACTION_GRANT, wire.NODE_ACTION_COMPLETE]
        return []
    if node.type != wire.NODE_TYPE_STEP:
        return []
    if status == wire.STATUS_PAUSED:
        actions = [wire.NODE_ACTION_COMPLETE, wire.NODE_ACTION_FAIL]
        if session_id:
            actions.append(wire.NODE_ACTION_REPLY)
        return actions
    if status == wire.STATUS_RUNNING and session_id:
        return [wire.NODE_ACTION_CANCEL]
    return []


def _iterations_of(run: _Run, repeat_path: tuple[str, ...]) -> list[tuple[int, str]]:
    """The iterations of the repeat at *repeat_path* the run has reached, in order."""
    prefix = (
        wire.NODE_PATH_SEP.join(repeat_path) + wire.NODE_PATH_SEP + wire.ITERATION_SEGMENT_PREFIX
    )
    seen: set[int] = set()
    for key in run.path_state:
        if key.startswith(prefix):
            head = key[len(prefix) :].split(wire.NODE_PATH_SEP, 1)[0]
            if head.isdigit():
                seen.add(int(head))
    return [(n, f"{wire.ITERATION_SEGMENT_PREFIX}{n}") for n in sorted(seen)]


def _iteration_status(run: _Run, iteration_path: tuple[str, ...]) -> str:
    """An iteration's own status, else rolled up from the instances under it."""
    key = wire.NODE_PATH_SEP.join(iteration_path)
    own = run.path_state.get(key, {}).get("status")
    if own:
        return own
    statuses = [
        v.get("status") for k, v in run.path_state.items() if k.startswith(key + wire.NODE_PATH_SEP)
    ]
    statuses = [s for s in statuses if s]
    for wanted in (
        wire.STATUS_RUNNING,
        wire.STATUS_PAUSED,
        wire.STATUS_FAILED,
        wire.STATUS_ABORTED,
    ):
        if wanted in statuses:
            return wanted
    if statuses and all(s in wire.NODE_DONE_STATUSES for s in statuses):
        return wire.STATUS_COMPLETED
    return wire.NODE_STATUS_PENDING


def _blocking(run: _Run) -> dict[str, Any] | None:
    """The first wait, labelled for its row: ``<step> · <reason>``."""
    if not run.attention:
        return None
    first = run.attention[0]
    label = next(
        (s.label or s.agent or s.id for s in wire.iter_steps(run.plan) if s.id == first.node_id),
        first.node_id,
    )
    return {
        "nodeId": first.node_id,
        "label": label,
        "kind": first.kind,
        "sessionId": first.session_id or run.node_session.get(first.node_id),
        "toolCallId": first.tool_call_id,
    }


def _waiting_for(
    run: _Run, node_id: str, path: tuple[str, ...], *, in_iteration: bool
) -> str | None:
    """``approval`` / ``input`` when an ``attention`` entry names this node instance."""
    if run.status in wire.TERMINAL_RUN_STATUSES:
        return None
    key = wire.NODE_PATH_SEP.join(path)
    found: str | None = None
    for item in run.attention:
        where = item.node_path
        if where and where[0] == run.workflow_id:
            where = where[1:]
        if where:
            hit = wire.NODE_PATH_SEP.join(where) == key
        else:
            hit = item.node_id == node_id and not in_iteration
        if not hit:
            continue
        if item.kind == wire.ATTENTION_QUESTION:
            return "input"
        found = "approval"
    return found


def _active_steps(run: _Run) -> list[dict[str, Any]]:
    """Running step INSTANCES, by runtime path; none on a finished run (gap 23)."""
    if run.status in wire.TERMINAL_RUN_STATUSES:
        return []
    steps = {s.id: s for s in wire.iter_steps(run.plan)}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, entry in run.path_state.items():
        node = steps.get(entry.get("nodeId", ""))
        if node is None or entry.get("status") != wire.STATUS_RUNNING:
            continue
        seen.add(node.id)
        out.append(
            {
                "nodeId": node.id,
                "label": node.label or node.agent or node.id,
                "sessionId": entry.get("sessionId") or run.node_session.get(node.id),
                "path": path,
            }
        )
    for node in steps.values():
        # A step known only by bare id (a notification without ``nodePath``).
        if node.id not in seen and run.node_status.get(node.id) == wire.STATUS_RUNNING:
            out.append(
                {
                    "nodeId": node.id,
                    "label": node.label or node.agent or node.id,
                    "sessionId": run.node_session.get(node.id),
                    "path": node.id,
                }
            )
    return out


def _has_failed_nodes(run: _Run) -> bool:
    failed = (wire.STATUS_FAILED, wire.STATUS_ABORTED)
    return any(s in failed for s in run.node_status.values()) or any(
        entry.get("status") in failed for entry in run.path_state.values()
    )


def _pause_payload(run: _Run, plan_waiting: bool) -> dict[str, Any] | None:
    # After a restart KAS keeps exactly one open request on a paused run: a
    # top-level step's plan approval (every other request's wait is dropped on
    # load), so a permission wait on a paused run is that plan.
    plan_wait = plan_waiting or any(
        a.kind == wire.ATTENTION_PERMISSION and a.tool_call_id for a in run.attention
    )
    return wire.classify_pause(
        run.status,
        pause=run.pause_info,
        detail_class=run.pause_class,
        pause_reason=run.pause_reason,
        user_stopped=run.user_stopped,
        attention=run.attention,
        plan_waiting=plan_wait,
    )


def _row_payload(run: _Run, *, plan_waiting: bool = False) -> dict[str, Any]:
    pause = _pause_payload(run, plan_waiting)
    controls = wire.run_controls(
        run.status,
        pause_pending=run.pause_pending,
        attention=run.attention,
        pause_reason=pause["reason"] if pause else None,
    )
    progress = None if run.status in wire.TERMINAL_RUN_STATUSES else _progress(run)
    active = _active_steps(run)
    plan_update = run.last_plan_update
    return {
        "workflowId": run.workflow_id,
        "name": run.name,
        "workflowName": run.workflow_name,
        "status": run.status,
        "parentSessionId": run.parent_session_id,
        "createdAt": run.created_at,
        "startedAt": run.started_at,
        "endedAt": run.ended_at,
        "updatedAt": run.updated_at,
        "pausePending": run.pause_pending,
        "pauseReason": run.pause_reason,
        "pause": pause,
        "actions": wire.run_actions(
            run.status,
            pause_pending=run.pause_pending,
            loop_exhausted=bool(pause and pause["reason"] == wire.PAUSE_LOOP_EXHAUSTED),
            has_failed_nodes=_has_failed_nodes(run),
            # *plan_waiting* is the router's own word that it holds the plan
            # card; the attention-based guess in _pause_payload is not.
            plan_card_held=bool(
                plan_waiting and pause and pause["reason"] == wire.PAUSE_PLAN_APPROVAL
            ),
        ),
        "completedSteps": progress[0] if progress else None,
        "totalSteps": progress[1] if progress else None,
        # KAS's own counts (``WorkflowRunProgress``); every key null when unknown.
        **(run.progress.to_payload() if run.progress else wire.UNKNOWN_RUN_PROGRESS),
        "activeSteps": active,
        "stepsKnown": bool(run.plan),
        "attention": [item.to_payload() for item in run.attention],
        "blocking": _blocking(run),
        "controls": controls.to_payload(),
        "nodes": [_node_payload(run, node) for node in run.plan],
        "queuedSteps": [node.to_payload() for node in run.queued_steps],
        "planRevision": run.plan_revision,
        "lastPlanUpdate": (
            {"outcome": plan_update[0], "reason": plan_update[1], "at": plan_update[2]}
            if plan_update
            else None
        ),
        "revision": run.revision,
    }
