"""HTTP routes over the durable task queue (``taskq``) and its capacity view.

The dashboard's "Tasks & capacity" panel and ``kirocrew doctor``-style probes
read here. Four routes:

* ``GET /api/tasks`` -- rows from the task store, filterable by ``state`` and
  ``lane``, capped by ``limit``.
* ``GET /api/tasks/summary`` -- queue depth by state, the oldest wait, the
  effective caps per lane against the user's ceiling (from
  ``resource_status.adaptive_state()`` and the session-health cap sources),
  the degrade reason, per-slot wait reasons and recovery attempts.
* ``GET /api/tasks/{task_id}`` -- one row plus its ``task_events`` tail.
* ``POST /api/tasks/{task_id}`` -- an action on a PARKED runner row:
  ``{"action": "answer_input", "answer": "..."}`` delivers the operator's
  answer to a ``waiting_input`` TaskRunner / workflow row (never auto-answered
  anywhere else); ``{"action": "cancel_wait"}`` ends the wait of a row that
  holds no live runtime without an answer (the row is ``cancelled``, the parked
  coroutine resumes and stops). A row whose runtime is live (``starting`` /
  ``running``) has no wait to end and is refused 409 ``not_waiting``: ending
  one here would leave the executor running against a ``cancelled`` row, the
  orphan the route below exists to avoid. Both refusals are the STORE's, over
  the generation the handler's read returned and over the one state the action
  answers (``TaskStore.cancel(only_from=)``,
  ``TaskStore.wake_wait(only_from=)``), so a row a concurrent admission takes
  live -- or moves into another wait, where an answer to the question that is
  over would end the wrong one -- between the read and the write is refused too,
  with the same code and the row's fresh state.
* ``POST /api/tasks/{task_id}/cancel`` -- cancel through the owner that can
  actually stop the work: subagent rows go through ``SubagentManager.cancel``
  (``_unqueue`` / ``taskq_cancel_queued`` for a row that never started,
  ``_force_reap`` → ``taskq_settle`` for a live one, children first); a
  TaskRunner step or workflow agent-call row that is parked (queued, deferred
  or in a wait) is ended through ``RunnerAdmission.cancel_wait``, and one whose
  runtime is live cancels its run (``TaskRunner.cancel`` /
  ``WorkflowService.cancel``) -- a step is one unit of its run, there is no
  smaller lever. The handler never writes the store for a live row itself: a
  bare ``store.cancel`` would leave a live runtime running against a row that
  says ``cancelled``.

Every store STRING these routes serve is scrubbed on the way out
(:func:`_scrub_prose`): a row's wait reason and its event details carry a
failing tool's, subagent's or dependency adapter's own text, which the store
keeps verbatim so the row stays diagnosable, and a credential or an
exfiltration URL in it must not reach a browser. Identifiers are not prose and
are served as stored -- an id, a lane, a session key and a lease owner are what
the panel keys its rows by, and rewriting one would break the match.

Every store read runs in ``asyncio.to_thread`` (SQLite on the request path is
blocking I/O), and the list and detail routes serialize their rows THERE too:
redacting a full page costs about as much as reading it, and the loop those
requests land on is the one every session's turn shares. Auth is the
dashboard's token middleware, like every sibling
handler in this package -- there is no per-route check here, and the routes
are NOT on the internal-path lists, so an MCP caller with only the internal
secret is refused the same way it is for ``/api/sessions/health``.

The lane spelling is ``taskq.lanes`` (RFC Q5): the row's stored ``lane``
column when the accept path filed it, else derived -- cron and hook roots and
automation session keys share one ``system`` lane; every other root is keyed
by its own session key.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from kiro_crew import resource_status
from kiro_crew.dashboard import session_health
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.recovery.ladder import default_ladder
from kiro_crew.security import redact_and_truncate
from kiro_crew.taskq import lanes as taskq_lanes
from kiro_crew.taskq import model as taskq_model
from kiro_crew.taskq.adapters import runner as taskq_runner
from kiro_crew.taskq.store import TaskStore

logger = logging.getLogger(__name__)

#: Default and ceiling for ``?limit=``: the panel shows a page, not the queue.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000
#: Events returned with one row. The store keeps every transition; a detail view
#: wants the recent tail, and a runaway retry loop must not turn one GET into a
#: megabyte.
_EVENTS_TAIL = 200
#: Bound on the ``retry_wait`` rows the summary merges into the active view
#: (they are parked, so ``active_rows`` does not return them). It bounds the ROW
#: LIST and the attempts summed over it, never a count: every ``depth`` number
#: is counted off the uncapped ``count_by_state()``, so no two numbers in one
#: response can disagree about how many rows there are.
_LANE_SCAN_LIMIT = 5000
#: Ceiling on ONE store string served here. Every writer bounds what it files
#: (a terminal ``error`` and a ``WaitRecord`` reason at 500 chars, an answered
#: input at 2000), so this is the bound for a value no writer bounded -- never a
#: second truncation of one they did.
_MAX_PROSE_CHARS = 4000

LANE_SYSTEM = taskq_lanes.SYSTEM_LANE
_SYSTEM_KINDS = frozenset({taskq_model.KIND_CRON, taskq_model.KIND_HOOK})

#: Rows the summary counts as recovering. ``retry_wait`` is BOTH this and
#: queued: it waits for capacity like a queued row and is a row being retried,
#: and ``depth`` is a set of views over the same rows, not a partition of them.
_RECOVERY_STATES = frozenset({taskq_model.RECOVERING, taskq_model.RETRY_WAIT})


def lane_of(record: Any) -> str:
    """The fairness lane a task row belongs to.

    The stored ``lane`` column wins (the accept path resolves a nested row to
    its root's lane there); a row from before the column, or one filed by a
    writer that left it empty, derives: cron/hook kinds are ``system``, and
    the session key maps through :func:`taskq.lanes.lane_key_for`.
    """
    stored = str(getattr(record, "lane", "") or "")
    if stored:
        return stored
    kind = str(getattr(record, "kind", "") or "")
    if kind in _SYSTEM_KINDS:
        return LANE_SYSTEM
    return taskq_lanes.lane_key_for(str(getattr(record, "session_key", "") or ""))


def _store_of(state: Any) -> TaskStore | None:
    """The open task store, or ``None`` when this gateway has none.

    Same read path as ``/api/sessions/health``: the subagent manager owns the
    store (``SubagentManager._taskq``). Anything that is not a real
    ``TaskStore`` (a ``MagicMock`` state in much of the suite answers a mock
    here) reads as "no store" rather than as a phantom queue of mocks.
    """
    store = getattr(getattr(state, "subagents", None), "_taskq", None)
    return store if isinstance(store, TaskStore) else None


def _store_now(store: Any) -> float:
    """The store's clock (injected in tests) so ages match the rows' stamps."""
    now = getattr(store, "now", None)
    try:
        return float(now()) if callable(now) else time.time()
    except Exception:
        return time.time()


def _scrub_prose(text: str) -> str:
    """One store string, safe to serve.

    The store keeps a tool's or subagent's failure text VERBATIM -- that is
    what makes a row diagnosable after the process that failed is gone -- so
    the redaction belongs on the way OUT, on every surface that serves it.
    :func:`security.redact_and_truncate` is the composition the sibling
    surfaces spell by hand (``subagent.py``'s ``_redact``, the pair in
    ``taskrunner._execute_tasks``): exfiltration URLs FIRST, then credentials.
    That order is load-bearing rather than incidental -- a URL whose query IS
    the credential is the shape the URL rewriter exists for, and it replaces
    the whole URL, DESTINATION included, while credentials-first leaves
    ``?token=[REDACTED: credential]`` behind: nothing suspicious remains in
    it, so the host the agent was posting to survives. The truncation likewise
    runs over the redacted text, never a slice of the raw one.
    """
    return redact_and_truncate(text, _MAX_PROSE_CHARS)


def _scrub_map(data: Mapping[str, Any]) -> dict[str, Any]:
    """:func:`_scrub_value` over every value of one payload mapping."""
    return {str(k): _scrub_value(v) for k, v in data.items()}


def _scrub_value(value: Any) -> Any:
    """*value* with every STRING leaf scrubbed, walking dicts and lists.

    Event data is whatever a writer filed -- an adapter's signal dict, a list
    of details -- so a credential nested two levels down is the same leak as
    one at the top. Non-string leaves (numbers, bools, ``None``) pass through
    unchanged: no redaction pattern can match them, and rewriting one would
    change an attempt count or an instant the panel reads. Mapping KEYS pass
    through too -- they are field names the writers spell in code, never store
    prose, and truncating one could collapse two fields into a single field.
    """
    if isinstance(value, str):
        return _scrub_prose(value)
    if isinstance(value, Mapping):
        return _scrub_map(value)
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    return value


def _wait_payload(wait: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ``WaitRecord`` fields a reader needs, without the whole dict.

    Scrubbed as a whole: ``reason`` is built from a dependency adapter's
    ``detail`` (a GitHub stderr tail, an HTTP body, an exception string), and a
    ``tool_call_id`` is the provider's, so neither is text this gateway wrote.
    """
    if not isinstance(wait, dict):
        return None
    cond = wait.get("resume_condition")
    cond = cond if isinstance(cond, dict) else {}
    return _scrub_map(
        {
            "reason": str(wait.get("reason") or ""),
            "since": wait.get("since"),
            "deadline_at": wait.get("deadline_at"),
            "resume_kind": str(cond.get("kind") or ""),
            "dependency_scope": wait.get("dependency_scope"),
            "cancel_semantics": str(wait.get("cancel_semantics") or ""),
            "tool_call_id": str(wait.get("tool_call_id") or ""),
        }
    )


def row_payload(record: Any, *, now: float | None = None) -> dict[str, Any]:
    """One task row as the API spells it."""
    now = time.time() if now is None else now
    updated = float(getattr(record, "updated_at", 0.0) or 0.0)
    created = float(getattr(record, "created_at", 0.0) or 0.0)
    wait = _wait_payload(getattr(record, "wait", None))
    state = str(getattr(record, "state", "") or "")
    # Age of the current condition: a wait counts from its own start, anything
    # else from the last state write, and a row never written counts from birth.
    since = created
    if wait and wait.get("since"):
        since = float(wait["since"])
    elif updated:
        since = updated
    return {
        "id": str(getattr(record, "id", "") or ""),
        "kind": str(getattr(record, "kind", "") or ""),
        "state": state,
        "lane": lane_of(record),
        "session_key": str(getattr(record, "session_key", "") or ""),
        "parent_id": getattr(record, "parent_id", None),
        "root_id": str(getattr(record, "root_id", "") or ""),
        "attempts": int(getattr(record, "attempts", 0) or 0),
        "generation": int(getattr(record, "generation", 0) or 0),
        "next_run_at": getattr(record, "next_run_at", None),
        "deadline_at": getattr(record, "deadline_at", None),
        "lease_owner": getattr(record, "lease_owner", None),
        "lease_expires_at": getattr(record, "lease_expires_at", None),
        "wait": wait,
        "wait_reason": (wait or {}).get("reason") or None,
        "wait_since": (wait or {}).get("since"),
        "wait_deadline_at": (wait or {}).get("deadline_at"),
        "age_secs": max(0.0, now - since) if since else 0.0,
        "created_at": created,
        "updated_at": updated,
        "terminal": state in taskq_model.TERMINAL,
    }


def _event_payload(event: Any) -> dict[str, Any]:
    """One ``task_events`` row as the API spells it.

    ``data`` is scrubbed (:func:`_scrub_value`): a transition's detail carries
    the failing tool's or subagent's error text verbatim from the store, and a
    dependency signal's detail carries an external command's output. ``kind`` is
    a closed set of event names the store's own writers spell, and ``seq`` /
    ``ts`` are numbers, so neither is prose to redact.
    """
    data = getattr(event, "data", None)
    return {
        "seq": int(getattr(event, "seq", 0) or 0),
        "ts": float(getattr(event, "ts", 0.0) or 0.0),
        "kind": str(getattr(event, "kind", "") or ""),
        "data": _scrub_map(data) if isinstance(data, Mapping) else {},
    }


def _parse_limit(raw: str | None) -> int | None:
    """``?limit=`` as an int in ``[1, _MAX_LIMIT]``; ``None`` for a bad value."""
    if raw is None or raw == "":
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return min(value, _MAX_LIMIT)


def _list_rows_sync(
    store: Any, *, state: str | None, lane: str | None, limit: int
) -> list[dict[str, Any]]:
    """Blocking store read for ``GET /api/tasks``, WITH its serialization.

    The lane filter is the store's own ``lane=`` predicate (every row carries
    its lane: derived at accept, backfilled by the schema v3 migration).

    The payloads are built here rather than back on the loop: redacting a full
    page of rows costs about as much as reading them, and a ``limit=1000``
    request would spend all of that on the loop every session's turn shares.
    """
    if lane is None:
        rows = list(store.list_rows(state=state, limit=limit))
    else:
        rows = list(store.list_rows(state=state, lane=lane, limit=limit))
    now = _store_now(store)
    return [row_payload(r, now=now) for r in rows]


async def api_tasks_list(request: web.Request) -> web.Response:
    """GET /api/tasks?state=&lane=&limit= -- task rows, oldest first."""
    state: DashboardState = request.app["state"]
    task_state = request.query.get("state") or None
    if task_state is not None and task_state not in taskq_model.STATES:
        return web.json_response(
            {
                "error": "unknown task state",
                "code": "bad_state",
                "states": sorted(taskq_model.STATES),
            },
            status=400,
        )
    lane = request.query.get("lane") or None
    limit = _parse_limit(request.query.get("limit"))
    if limit is None:
        return web.json_response(
            {"error": f"limit must be an integer in [1, {_MAX_LIMIT}]", "code": "bad_limit"},
            status=400,
        )
    store = _store_of(state)
    if store is None:
        return web.json_response({"available": False, "tasks": [], "count": 0})
    try:
        payload = await asyncio.to_thread(
            _list_rows_sync, store, state=task_state, lane=lane, limit=limit
        )
    except Exception:
        logger.warning("task store list failed", exc_info=True)
        return web.json_response({"available": False, "tasks": [], "count": 0})
    return web.json_response(
        {
            "available": True,
            "tasks": payload,
            "count": len(payload),
            "limit": limit,
            "filters": {"state": task_state, "lane": lane},
        }
    )


def _detail_sync(store: Any, task_id: str) -> dict[str, Any] | None:
    """Blocking read and serialization for ``GET /api/tasks/{id}``; None for a
    row that does not exist.

    The events tail is redacted here for :func:`_list_rows_sync`'s reason: a
    full tail is a couple of hundred strings through the redaction table, and
    the thread is already paid for.
    """
    record = store.get(task_id)
    if record is None:
        return None
    events = list(store.events(task_id, limit=_EVENTS_TAIL))
    return {
        "task": row_payload(record, now=_store_now(store)),
        "events": [_event_payload(e) for e in events],
    }


async def api_task_detail(request: web.Request) -> web.Response:
    """GET /api/tasks/{task_id} -- one row plus its events tail."""
    state: DashboardState = request.app["state"]
    task_id = request.match_info["task_id"]
    store = _store_of(state)
    if store is None:
        return web.json_response(
            {"error": "task store unavailable", "code": "unavailable"}, status=404
        )
    try:
        detail = await asyncio.to_thread(_detail_sync, store, task_id)
    except Exception:
        logger.warning("task store read failed for %s", task_id, exc_info=True)
        return web.json_response(
            {"error": "task store unavailable", "code": "unavailable"}, status=503
        )
    if detail is None:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    return web.json_response(detail)


async def api_task_cancel(request: web.Request) -> web.Response:
    """POST /api/tasks/{task_id}/cancel -- cancel through the admission cascade.

    ``SubagentManager.cancel`` is the one entry every cancellation takes
    (dashboard stop button, ``spawn_release``, parent cancel), so a task
    cancelled here gets the same neutral ``stopped`` terminal, the same
    children-first cascade and the same SEL record as one stopped from the
    activity card. Only subagent rows have a live cancel adapter today; a
    workflow or TaskRunner row that the manager does not know answers 409 with
    the row's current state rather than pretending it stopped.
    """
    state: DashboardState = request.app["state"]
    task_id = request.match_info["task_id"]
    store = _store_of(state)
    manager = getattr(state, "subagents", None)
    if store is None or manager is None:
        return web.json_response(
            {"error": "task store unavailable", "code": "unavailable"}, status=404
        )
    try:
        record = await asyncio.to_thread(store.get, task_id)
    except Exception:
        logger.warning("task store read failed for %s", task_id, exc_info=True)
        return web.json_response(
            {"error": "task store unavailable", "code": "unavailable"}, status=503
        )
    if record is None:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    if record.state in taskq_model.TERMINAL:
        return web.json_response(
            {"ok": False, "cancelled": False, "code": "terminal", "task": row_payload(record)},
            status=409,
        )
    cancelled = False
    try:
        runner_verdict = await _runner_cancel(state, record)
        if runner_verdict is not None:
            cancelled = runner_verdict
        else:
            cancelled = bool(await manager.cancel(task_id))
    except Exception:
        logger.warning("task cancel failed for %s", task_id, exc_info=True)
        return web.json_response(
            {"ok": False, "cancelled": False, "code": "cancel_failed"}, status=500
        )
    try:
        after = await asyncio.to_thread(store.get, task_id)
    except Exception:
        after = None
    now = _store_now(store)
    payload = row_payload(after if after is not None else record, now=now)
    if not cancelled and payload["state"] not in taskq_model.TERMINAL:
        # No adapter took the cancel and the row is still live: say so.
        return web.json_response(
            {"ok": False, "cancelled": False, "code": "no_cancel_adapter", "task": payload},
            status=409,
        )
    return web.json_response({"ok": True, "cancelled": True, "task": payload})


def _runner_admission_of(state: Any) -> Any:
    """The shared ``RunnerAdmission`` (TaskRunner + workflows), or None."""
    runner = getattr(state, "task_runner", None)
    admission = getattr(runner, "task_admission", None) if runner is not None else None
    if admission is None:
        service = getattr(state, "workflow_service", None)
        admission = getattr(service, "_task_admission", None) if service is not None else None
    return admission


def _is_runner_row(record: Any) -> bool:
    return str(getattr(record, "kind", "")) in taskq_runner.RUNNER_RECOVERY_ADAPTERS or (
        taskq_runner.owner_of(str(getattr(record, "id", ""))) is not None
    )


async def _runner_cancel(state: Any, record: Any) -> bool | None:
    """Cancel adapter for TaskRunner / workflow rows; None when not one.

    A parked row (queued, deferred, any wait) ends through the admission's
    ``cancel_wait`` -- the store row is ``cancelled`` and the parked coroutine
    resumes to see it. A row whose runtime is live (``starting``, ``running``)
    cancels its owning run, the only lever that stops the live session; the
    run's own exit path settles the row.

    ``record`` is a row read before this call, so the state it carries routes
    the cancel but never authorizes it: the parked branch hands the row's
    generation to ``cancel_wait``, which cancels only a still-parked row, and a
    False here is a row that went live under the read. The operator gets the
    fresh state and the live lever, never a ``cancelled`` row over a live run.
    """
    if not _is_runner_row(record):
        return None
    admission = _runner_admission_of(state)
    task_id = str(record.id)
    if record.state not in taskq_model.EXECUTING:
        if admission is None:
            return False
        return bool(
            await asyncio.to_thread(
                admission.cancel_wait,
                task_id,
                reason="cancelled from /api/tasks",
                generation=record.generation,
            )
        )
    owner = taskq_runner.owner_of(task_id)
    if owner is None:
        return False
    owner_kind, run_id = owner
    if owner_kind == taskq_runner.OWNER_TASKRUNNER:
        runner = getattr(state, "task_runner", None)
        if runner is None or not hasattr(runner, "cancel"):
            return False
        runner.cancel(run_id, exact=True)
        return True
    service = getattr(state, "workflow_service", None)
    if service is None or not hasattr(service, "cancel"):
        return False
    return bool(await service.cancel(run_id))


_TASK_ACTIONS = frozenset({"answer_input", "cancel_wait"})


async def api_task_action(request: web.Request) -> web.Response:
    """POST /api/tasks/{task_id} -- ``answer_input`` / ``cancel_wait`` on a runner row.

    ``answer_input`` needs a ``waiting_input`` row and a non-empty ``answer``;
    the answer is bound to that row (and through it to the step's attempt), so
    a late answer after the wait ended is refused with 409 rather than replayed.
    ``cancel_wait`` ends the wait of a row that holds no live runtime; a row
    whose runtime is live is 409 ``not_waiting``, because the wait-level lever
    would cancel the row and leave the step or agent call executing under it.
    Stopping THAT is ``POST /api/tasks/{task_id}/cancel``, which routes through
    the owning run.

    Both state tests below read a row over the wire and are PRE-CHECKS: they
    save a pointless write and name the refusal in the row's own vocabulary, but
    the fence is the writer's, under the generation this read returned and over
    the one state the action answers -- ``TaskStore.cancel(only_from=PARKED)``
    for ``cancel_wait``, ``wake_wait(only_from=waiting_input)`` for
    ``answer_input``. A row that goes live, or into a DIFFERENT wait, between the
    read and the write is refused by the STORE and answers the same 409 the
    pre-check would have, from the state the row carries then -- never 200 for a
    cancel, or an answer, that did not happen.
    """
    state: DashboardState = request.app["state"]
    task_id = request.match_info["task_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    action = str(body.get("action", "") or "")
    if action not in _TASK_ACTIONS:
        return web.json_response(
            {"error": "unknown action", "code": "bad_action", "actions": sorted(_TASK_ACTIONS)},
            status=400,
        )
    store = _store_of(state)
    if store is None:
        return web.json_response(
            {"error": "task store unavailable", "code": "unavailable"}, status=404
        )
    try:
        record = await asyncio.to_thread(store.get, task_id)
    except Exception:
        logger.warning("task store read failed for %s", task_id, exc_info=True)
        return web.json_response(
            {"error": "task store unavailable", "code": "unavailable"}, status=503
        )
    if record is None:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    if not _is_runner_row(record):
        return web.json_response(
            {"error": "not a runner row", "code": "no_input_adapter", "task": row_payload(record)},
            status=409,
        )
    admission = _runner_admission_of(state)
    if admission is None:
        return web.json_response(
            {"error": "runner admission not attached", "code": "no_input_adapter"}, status=409
        )
    if action == "answer_input":
        answer = str(body.get("answer", "") or "")
        if not answer.strip():
            return web.json_response(
                {"error": "answer is required", "code": "answer_required"}, status=400
            )
        # Terminal first, matching ``_refused_code``: a settled row is not "a wait
        # that is over", and testing WAITING_INPUT alone would name the same row
        # differently depending on whether it settled before this read or during
        # the window after it.
        if record.state in taskq_model.TERMINAL:
            return web.json_response(
                {"ok": False, "code": "terminal", "task": row_payload(record)}, status=409
            )
        if record.state != taskq_model.WAITING_INPUT:
            return web.json_response(
                {"ok": False, "code": "not_waiting_input", "task": row_payload(record)},
                status=409,
            )
        ok = bool(
            await asyncio.to_thread(
                admission.answer_input,
                task_id,
                answer,
                generation=record.generation,
            )
        )
    else:
        if record.state in taskq_model.TERMINAL:
            return web.json_response(
                {"ok": False, "code": "terminal", "task": row_payload(record)}, status=409
            )
        if record.state in taskq_model.EXECUTING:
            return web.json_response(
                {"ok": False, "code": "not_waiting", "task": row_payload(record)}, status=409
            )
        ok = bool(
            await asyncio.to_thread(
                admission.cancel_wait,
                task_id,
                reason="wait cancelled from /api/tasks",
                generation=record.generation,
            )
        )
    try:
        after = await asyncio.to_thread(store.get, task_id)
    except Exception:
        after = None
    payload = row_payload(after if after is not None else record, now=_store_now(store))
    if not ok:
        return web.json_response(
            {
                "ok": False,
                "code": _refused_code(action, str(payload["state"])),
                "action": action,
                "task": payload,
            },
            status=409,
        )
    return web.json_response({"ok": True, "action": action, "task": payload})


def _refused_code(action: str, state: str) -> str:
    """Why the WRITE refused an action the pre-check let through.

    The refusal comes from the row's state, so it is named in the same
    vocabulary the pre-check uses -- an operator who lost the race reads the
    same code as one who arrived a moment later. ``action_failed`` is kept for a
    refusal the state does NOT explain (the store was unavailable, or the row
    moved on under a new generation while still parked): naming a state reason
    there would be a guess.
    """
    if state in taskq_model.TERMINAL:
        return "terminal"
    if action == "cancel_wait":
        return "not_waiting" if state in taskq_model.EXECUTING else "action_failed"
    return "not_waiting_input" if state != taskq_model.WAITING_INPUT else "action_failed"


# ── summary ──────────────────────────────────────────────────────────────────


def _lane_caps(health: dict[str, Any], adaptive: dict[str, Any] | None) -> dict[str, Any]:
    """Effective cap vs the user's ceiling per lane.

    ``session_health`` publishes whatever cap sources registered (the subagent
    manager's live cap, the controller's when wired); the controller's own
    state carries the ceiling and the spawn-gate capacity. Merged by lane name
    so the panel renders one row per lane whatever the wiring on this build.
    """
    caps: dict[str, dict[str, Any]] = {}
    for lane, entry in (health.get("effective_caps") or {}).items():
        if isinstance(entry, dict):
            caps[str(lane)] = dict(entry)
    if adaptive:
        sub = caps.setdefault("subagents", {})
        if adaptive.get("effective_exec_cap") is not None:
            sub.setdefault("effective", adaptive.get("effective_exec_cap"))
        if adaptive.get("exec_ceiling") is not None:
            sub["user_max"] = adaptive.get("exec_ceiling")
        if adaptive.get("spawn_gate_capacity") is not None:
            caps["spawn_gate"] = {
                **caps.get("spawn_gate", {}),
                "effective": adaptive.get("spawn_gate_capacity"),
                "user_max": adaptive.get("gate_ceiling"),
            }
    return caps


def _degrade_reason(health: dict[str, Any], adaptive: dict[str, Any] | None) -> str | None:
    """The pressure reason: the health monitor's, else the controller's own."""
    reason = health.get("degrade_reason")
    if reason:
        return str(reason)
    if not adaptive:
        return None
    last = adaptive.get("last")
    last = last if isinstance(last, dict) else {}
    if adaptive.get("paused"):
        return str(last.get("reason") or "paused")
    if adaptive.get("probing"):
        return str(last.get("reason") or "probing")
    if last.get("action") in ("decrease", "pause"):
        return str(last.get("reason") or last.get("action"))
    return None


def _summary_sync(store: Any, snapshot: Any, monitor: Any) -> dict[str, Any]:
    """Blocking half of ``GET /api/tasks/summary``."""
    wall_now = time.time()
    now = _store_now(store) if store is not None else wall_now
    health = monitor.compute(snapshot, taskq=store, include_log_scan=False)
    adaptive = resource_status.adaptive_state()
    by_state: dict[str, int] = {}
    oldest = 0.0
    recovery_rows: list[dict[str, Any]] = []
    waiting_rows: list[dict[str, Any]] = []
    available = False
    if store is not None:
        try:
            by_state = {str(k): int(v) for k, v in store.count_by_state().items()}
            oldest = float(store.oldest_wait_secs())
            # ``retry_wait`` holds no runtime, so it is not an ACTIVE row; it
            # is still a task being recovered, and the panel lists it as one.
            rows = list(store.active_rows()) + list(
                store.list_rows(state=taskq_model.RETRY_WAIT, limit=_LANE_SCAN_LIMIT)
            )
            for rec in rows:
                payload = row_payload(rec, now=now)
                if rec.state in taskq_model.WAITING:
                    waiting_rows.append(payload)
                elif rec.state in _RECOVERY_STATES:
                    recovery_rows.append(payload)
            available = True
        except Exception:
            logger.debug("task store read failed in summary", exc_info=True)
    # THE queued set, read from ``session_health`` rather than restated: the
    # depth chip and ``/api/sessions/health``'s queue count are the same claim
    # about the same rows, and an operator comparing the two panels is entitled
    # to the same number.
    queued_total = sum(by_state.get(s, 0) for s in session_health.TASK_QUEUED_STATES)
    # Counted off ``by_state`` for the same reason, and NOT off ``recovery_rows``:
    # that list is a PAGE (``_LANE_SCAN_LIMIT``) while ``by_state`` counts every
    # row, and the card subtracts ``by_state.retry_wait`` from ``queued`` to get
    # "waiting for a slot" -- so a ``recovering`` counted off the page would leave
    # every ``retry_wait`` row past the page in NEITHER number, which is the one
    # way these two counters can disagree in a single response.
    recovering_total = sum(by_state.get(s, 0) for s in _RECOVERY_STATES)
    slots: list[dict[str, Any]] = []
    for key, entry in (health.get("slots") or {}).items():
        if not isinstance(entry, dict):
            continue
        slots.append(
            {
                "key": str(key),
                "classification": str(entry.get("classification") or ""),
                "age_secs": float(entry.get("age_secs") or 0.0),
                "evidence": list(entry.get("evidence") or []),
            }
        )
    slot_recovering = [s for s in health.get("recovering") or [] if s.get("kind") == "slot"]
    return {
        "generated_at": wall_now,
        "available": available,
        "depth": {
            "by_state": by_state,
            "queued": queued_total,
            "waiting": len(waiting_rows),
            "recovering": recovering_total,
            "running": int(by_state.get(taskq_model.RUNNING, 0)),
            "total": sum(by_state.values()),
        },
        "oldest_wait_secs": oldest,
        "lanes": _lane_caps(health, adaptive),
        "degrade_reason": _degrade_reason(health, adaptive),
        "adaptive": adaptive,
        "slots": slots,
        "waiting": waiting_rows,
        "recovering": {
            "tasks": recovery_rows,
            "task_attempts": sum(r["attempts"] for r in recovery_rows),
            "slots": slot_recovering,
            "ladder": default_ladder().table(),
        },
        "stalled": health.get("stalled") or {},
        "counts": health.get("counts") or {},
        "stall_after_secs": health.get("stall_after_secs"),
    }


async def api_tasks_summary(request: web.Request) -> web.Response:
    """GET /api/tasks/summary -- depth, oldest wait, caps per lane, degrade reason."""
    state: DashboardState = request.app["state"]
    store = _store_of(state)
    # Snapshot on the loop (it walks live slot objects), compute off it.
    snapshot = session_health.snapshot_state(state)
    monitor = session_health.default_monitor()
    try:
        payload = await asyncio.to_thread(_summary_sync, store, snapshot, monitor)
    except Exception:
        logger.warning("tasks summary computation failed", exc_info=True)
        payload = {
            "generated_at": time.time(),
            "available": False,
            "depth": {
                "by_state": {},
                "queued": 0,
                "waiting": 0,
                "recovering": 0,
                "running": 0,
                "total": 0,
            },
            "oldest_wait_secs": 0.0,
            "lanes": {},
            "degrade_reason": None,
            "adaptive": None,
            "slots": [],
            "waiting": [],
            "recovering": {"tasks": [], "task_attempts": 0, "slots": [], "ladder": []},
            "stalled": {},
            "counts": {},
            "stall_after_secs": None,
        }
    return web.json_response(payload)


__all__ = [
    "LANE_SYSTEM",
    "api_task_cancel",
    "api_task_detail",
    "api_tasks_list",
    "api_tasks_summary",
    "lane_of",
    "row_payload",
]
