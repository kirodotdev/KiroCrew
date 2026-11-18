"""``GET /api/spawn/session-memory`` -- the CALLER's own memory footprint, numbers only.

The Sessions panel's ``/api/sessions/memory`` lists every session on the host with
its title, agent, key and pid. That is an owner surface: an agent must not read
which other chats exist or what they are called. This route serves an agent the
same measurement for its OWN scope and nothing else:

- its own session row (or, for a sub-agent, its own run), and
- the runs it spawned, as one aggregate, and
- for a conductor, one row per worker bound to an open item on its own board,
  keyed by its own item id (:func:`caller_worker_rows`).

Every value served is a count, a size or a ``shared`` flag. No session key,
title, agent name, task text, run id or pid leaves this module, and a runtime shared
with other sessions reports only the caller's size share, so a caller cannot learn
the name, the number or the figures of anything outside its scope. The scope rule is the one the spawn
control routes apply (:func:`_run_belongs_to_caller`): a run belongs to the
session that started it, and a sub-agent owns its own run.

It sits under the ``/api/spawn`` prefix, which is on ``_MIXED_INTERNAL_API_PATHS``,
so a loopback ``X-Internal-Secret`` caller reaches it with no allowlist change. Only
such a caller is answered, and only once its ``X-Session-Key`` is attested
(:func:`member_request_scope`): a name the caller merely asserts scopes nothing. The
dashboard owner keeps the full panel at ``/api/sessions/memory``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from aiohttp import web

from kiro_crew import ledger_wake
from kiro_crew.dashboard.handlers._shared import member_request_scope
from kiro_crew.dashboard.session_memory import SessionMemorySampler
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.resource_status import temp_headroom
from kiro_crew.session_allocation_parts.companions import subagent_runtime_row_key

logger = logging.getLogger(__name__)

#: Its own sampler, not the Sessions panel's: ``sample`` appends to the panel's
#: rolling history and CPU baselines, and an agent polling on its own cadence must
#: not reshape the owner's graph.
_scope_sampler = SessionMemorySampler()

#: One host sample serves every caller for this long. ``resource_status`` is called
#: before heavy steps and fan-outs, so a wave of agents would otherwise each pay a
#: full ``/proc`` walk of every session tree on the shared subprocess pool.
_SAMPLE_REUSE_SECS = 5.0
_sample_lock = LoopBoundLock()
_last_sample: tuple[float, dict[str, object]] | None = None
#: The reuse window's clock, a module binding so a test can drive it.
_clock = time.monotonic

#: The per-runtime figures a row may carry. Each is a count or a size.
_ROW_FIELDS = ("rss_mb", "procs", "mcp")
_RUN_FIELDS = ("count", "unsampled", "shared", "rss_mb", "procs", "mcp")


def _number(value: object) -> float | int | None:
    """A non-negative finite number, or ``None``. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _row_figures(row: Mapping[str, object], *, shared: bool, measured: bool) -> dict[str, object]:
    """The numbers-only projection of one sampler row.

    An unmeasured row reports every figure as ``None`` (unmeasured), never as the
    0.0 a fresh run carries before its first sample. On a runtime shared with other
    sessions the size is already this caller's share, while the process and MCP
    counts describe the whole runtime, co-tenants included, so they are withheld:
    they are not the caller's own.
    """
    out: dict[str, object] = {name: None for name in _ROW_FIELDS}
    if measured:
        out["rss_mb"] = _number(row.get("rss_mb"))
        if not shared:
            out["procs"] = _number(row.get("procs"))
            out["mcp"] = _number(row.get("mcp"))
    out["shared"] = shared
    return out


def _session_row_shared(row: Mapping[str, object]) -> bool:
    sharers = _number(row.get("sharers"))
    return sharers is not None and sharers > 1


def _is_own_run(caller: str, row: Mapping[str, object]) -> bool:
    """Whether task *row* is the caller's own run.

    A continued run keeps ``subagent:<original id>`` as its key while its live run
    gets a new id, so the row's ``session_key`` is matched first.
    """
    run_id = row.get("id")
    session_key = row.get("session_key")
    if isinstance(session_key, str) and session_key:
        return session_key == caller
    return isinstance(run_id, str) and bool(run_id) and caller == f"subagent:{run_id}"


def _owns_run(caller: str, row: Mapping[str, object]) -> bool:
    """Whether *caller* started task *row* (the spawn control routes' rule)."""
    from kiro_crew.dashboard.handlers.messaging import _run_belongs_to_caller

    run_id = row.get("id")
    if not isinstance(run_id, str) or not run_id:
        return False
    return _run_belongs_to_caller(caller, run_id, row.get("parent"))


def caller_memory_view(payload: Mapping[str, Any], caller: str) -> dict[str, object]:
    """Project the sampler *payload* onto *caller*'s own scope, numbers only.

    ``self`` is the caller's own runtime: its session row, or for a sub-agent
    caller its own run. ``None`` when the caller has no live runtime of its own (a
    turn not running right now). ``subagent_runtime`` is the shared runtime the
    gateway keeps for the caller's sub-agents, when one is alive. ``runs`` totals
    the live runs the caller started: a run not yet measured is counted in
    ``unsampled`` and adds nothing to the sizes, and a run on a shared runtime is
    counted in ``shared`` and adds its size share but no process or MCP counts. A
    figure no run sent is ``None`` (unmeasured), never a 0 nobody measured. A
    run hosted inside ``subagent_runtime`` reports its share of that runtime, so the
    two overlap and are not to be added. The caller's own run is never counted as
    one of its runs.
    """
    if not caller:
        raise ValueError("caller is required")
    sessions = payload.get("sessions")
    tasks = payload.get("tasks")
    session_rows: Iterable[Any] = sessions if isinstance(sessions, list) else []
    task_rows: Iterable[Any] = tasks if isinstance(tasks, list) else []

    own: dict[str, object] | None = None
    companion: dict[str, object] | None = None
    companion_key = subagent_runtime_row_key(caller)
    for row in session_rows:
        if not isinstance(row, Mapping):
            continue
        if own is None and row.get("key") == caller:
            own = _row_figures(row, shared=_session_row_shared(row), measured=True)
        elif companion is None and row.get("key") == companion_key:
            companion = _row_figures(row, shared=_session_row_shared(row), measured=True)

    runs: dict[str, float] = dict.fromkeys(_RUN_FIELDS, 0)
    # The figures some run actually sent. A figure no run sent (every run unmeasured,
    # or every measured run shared, which sends no counts) is unmeasured, not 0.
    reported: set[str] = set()
    for row in task_rows:
        if not isinstance(row, Mapping):
            continue
        shared = row.get("shared") is True
        measured = row.get("sampled") is True
        if _is_own_run(caller, row):
            if own is None:
                own = _row_figures(row, shared=shared, measured=measured)
            continue
        if not _owns_run(caller, row):
            continue
        runs["count"] += 1
        if not measured:
            runs["unsampled"] += 1
            continue
        figures = _row_figures(row, shared=shared, measured=True)
        if shared:
            runs["shared"] += 1
        for name in _ROW_FIELDS:
            value = figures[name]
            if isinstance(value, (int, float)):
                runs[name] += value
                reported.add(name)
    view_runs: dict[str, object] = {name: int(runs[name]) for name in _RUN_FIELDS}
    view_runs["rss_mb"] = round(float(runs["rss_mb"]), 1)
    for name in _ROW_FIELDS:
        if name not in reported:
            view_runs[name] = None
    totals = payload.get("totals")
    upper = totals.get("rss_is_upper_bound") if isinstance(totals, Mapping) else None
    return {
        "self": own,
        "subagent_runtime": companion,
        "runs": view_runs,
        "rss_is_upper_bound": upper is not False,
    }


def _proc_cmdline(pid: int) -> bytes:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def _measures_as_test_runner(cmdline: bytes) -> bool:
    """The orphan sweep's structural test-runner match, plus an interpreter-prefixed
    console script (``python /venv/bin/pytest ...``). Measurement only: the sweep's
    own kill classifier is not widened."""
    from kiro_crew.session_pid_orphan_scan import _work_sweep_cmdline_is_test_runner

    # ``/proc`` ends argv with a NUL; a bare ``pytest`` would otherwise read as one
    # NUL-suffixed word.
    if _work_sweep_cmdline_is_test_runner(cmdline.rstrip(b"\0")):
        return True
    args = [a for a in cmdline.split(b"\x00") if a]
    return len(args) >= 2 and args[1].rsplit(b"/", 1)[-1] == b"pytest"


def _largest_test_tree_mb(pid: object) -> float | None:
    """RSS of the largest test-runner tree under *pid* (Linux), 0 when none, else ``None``.

    A tree is rooted at the topmost process the orphan sweep's structural
    classifier calls a test runner (``_work_sweep_cmdline_is_test_runner``), so a
    pytest run and its xdist workers are one tree. BLOCKING: walks ``/proc``.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or not sys.platform.startswith("linux"):
        return None
    from kiro_crew import platform_compat
    from kiro_crew.session_pid import _PAGE_SIZE, _read_rss_pages

    # The RSS ceiling's measuring map: a run the worker's shell left behind
    # (reparented to init, still in the runtime's session) is still its tree.
    children = platform_compat.proc_child_map(session_members=True)
    if children is None:
        return None

    def tree(root: int) -> set[int]:
        seen: set[int] = set()
        frontier = [root]
        while frontier:
            current = frontier.pop()
            if current not in seen:
                seen.add(current)
                frontier.extend(children.get(current, ()))
        return seen

    parent = {child: p for p, kids in children.items() for child in kids}
    below = tree(pid) - {pid}
    runners = {p for p in below if _measures_as_test_runner(_proc_cmdline(p))}
    roots = [p for p in runners if parent.get(p) not in runners]
    sizes = [sum(_read_rss_pages(q) for q in tree(root)) * _PAGE_SIZE / 2**20 for root in roots]
    return round(max(sizes, default=0.0), 1)


def _volume_free_pct(path: str) -> float | None:
    if not path:
        return None
    reading = temp_headroom(path)
    return None if reading is None else round(reading.free_space_pct, 1)


def caller_worker_rows(
    payload: Mapping[str, Any],
    caller: str,
    *,
    running: Callable[[str], bool],
    project_of: Callable[[str], str],
) -> list[dict[str, object]]:
    """One numbers-only row per open item on *caller*'s OWN board with a bound worker.

    rfc-host-memory-guard.md §5.3. A worker is listed only when its binding file
    names this board and this item (``read_binding``), so a session bound to
    another board never appears. Each row is keyed by the caller's own item id and
    carries sizes and flags only: no session key, title, path or pid. A remote
    item has no local tree and is skipped. Empty for a caller with no board.
    BLOCKING: reads the ledger, ``/proc`` and ``statvfs``.
    """
    from kiro_crew import work_ledger
    from kiro_crew.session_ledger import ledger_key

    board = ledger_key(caller)
    sessions = payload.get("sessions")
    by_key: dict[str, Mapping[str, Any]] = {}
    for row in sessions if isinstance(sessions, list) else []:
        if isinstance(row, Mapping) and isinstance(row.get("key"), str):
            by_key.setdefault(ledger_key(row["key"]), row)
    out: list[dict[str, object]] = []
    for item in work_ledger.list_work_items(board):
        worker = item.worker_session_key or ""
        if item.is_terminal or not worker or item.worker_instance_id:
            continue
        if work_ledger.read_binding(worker) != (board, item.item_id):
            continue
        row = by_key.get(ledger_key(worker))
        figures = (
            _row_figures(row, shared=_session_row_shared(row), measured=True)
            if row is not None
            else {name: None for name in _ROW_FIELDS}
        )
        out.append(
            {
                "item_id": item.item_id,
                "rss_mb": figures["rss_mb"],
                "procs": figures["procs"],
                # A shared runtime's tree is not the worker's own, so it is not walked.
                "test_mb": (
                    _largest_test_tree_mb(row.get("pid"))
                    if row is not None and not _session_row_shared(row)
                    else None
                ),
                "turn": "busy" if running(worker) else "idle",
                "volume_free_pct": _volume_free_pct(project_of(worker)),
            }
        )
    return out


def _slot_project(state: Any, session_key: str) -> str:
    getter = getattr(state, "get_slot", None)
    if not callable(getter):
        return ""
    for candidate in (session_key, f"dashboard_{session_key}"):
        slot = getter(candidate)
        if slot is not None:
            project = getattr(slot, "project", "")
            return project if isinstance(project, str) else ""
    return ""


async def _host_sample(state: Any) -> dict[str, object]:
    """One host sample, reused by every caller within ``_SAMPLE_REUSE_SECS``.

    Serialized: concurrent callers wait for the one sample in flight instead of
    each starting their own, which also keeps the sampler's CPU baselines from
    being written by two executor threads at once.
    """
    global _last_sample
    async with _sample_lock:
        now = _clock()
        if _last_sample is not None and now - _last_sample[0] < _SAMPLE_REUSE_SECS:
            return _last_sample[1]
        # ``get_slot`` is deliberately not passed: titles are never resolved here,
        # so none can reach the projection even by mistake.
        payload = await _scope_sampler.sample(state.sessions, getattr(state, "subagents", None))
        _last_sample = (_clock(), payload)
        return payload


async def api_spawn_session_memory(request: web.Request) -> web.Response:
    """GET /api/spawn/session-memory -- the attested caller's own footprint."""
    if request.get("internal_auth") is not True:
        return web.json_response(
            {
                "error": "This route answers an agent about its own session; "
                "the Sessions panel reads /api/sessions/memory.",
                "code": "agent_route",
            },
            status=403,
        )
    scope = await member_request_scope(request)
    if not scope.verified or not scope.session:
        return web.json_response(
            {
                "error": "The calling session could not be verified, so no scope can be shown.",
                "code": "caller_unverified",
            },
            status=409,
        )
    state = request.app["state"]
    payload = await _host_sample(state)
    view = caller_memory_view(payload, scope.session)
    try:
        workers = await asyncio.to_thread(
            caller_worker_rows,
            payload,
            scope.session,
            running=lambda key: ledger_wake.worker_running(state, key),
            project_of=lambda key: _slot_project(state, key),
        )
    except Exception:
        logger.debug("session-memory: worker rows could not be read", exc_info=True)
        workers = []
    if workers:
        view["workers"] = workers
    return web.json_response(view)
