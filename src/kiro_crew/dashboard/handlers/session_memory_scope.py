"""``GET /api/spawn/session-memory`` -- the CALLER's own memory footprint, numbers only.

The Sessions panel's ``/api/sessions/memory`` lists every session on the host with
its title, agent, key and pid. That is an owner surface: an agent must not read
which other chats exist or what they are called. This route serves an agent the
same measurement for its OWN scope and nothing else:

- its own session row (or, for a sub-agent, its own run), and
- the runs it spawned, as one aggregate.

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

import math
import time
from collections.abc import Iterable, Mapping
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import member_request_scope
from kiro_crew.dashboard.session_memory import SessionMemorySampler
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.session_allocation_parts.companions import subagent_runtime_row_key

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
    payload = await _host_sample(request.app["state"])
    return web.json_response(caller_memory_view(payload, scope.session))
