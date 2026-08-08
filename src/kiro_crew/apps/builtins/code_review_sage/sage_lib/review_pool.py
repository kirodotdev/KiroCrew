#!/usr/bin/env python3
"""Review executor — one shared, batch-scoped ``AcpRuntime``, one session per task.

Every code review multiplexes onto a SINGLE kiro-cli subprocess (``AcpRuntime``)
rather than a pool of per-worker ``AcpClient`` processes. Design:

  * **One runtime per batch** — lazily ``spawn()``ed on the first task of a batch
    and ``kill()``ed when the batch drains (see ``_BatchRuntimeHolder``). One
    subprocess serves all concurrent reviews; teardown reclaims all memory.
  * **One session per task** — each dispatch (gate / deep / follow-up / post)
    gets its own ``AcpSessionHandle`` (distinct ``sessionId``), ``destroy()``ed on
    completion, so one review never leaks context into another. No process respawn.
  * **Bounded concurrency** — a semaphore of width ``review.max_concurrent``
    (default 5, ceiling ``MAX_CONCURRENT_CEIL`` = 30) caps in-flight sessions.
    Because it is a single process, raising concurrency (e.g. to review all open
    PRs) costs sessions, not subprocesses.
  * **Auto-approval + audit** — the reviewer runs the ``gh`` CLI + shell, so each
    tool permission is auto-approved; and because the runtime layer has no
    ``audit_source``, the pool emits its own per-tool SEL audit.

These sessions are created directly on the runtime — NOT via the gateway's
``/api/spawn`` / ``SubagentManager`` path — so they never produce an agent card,
a ``:lock:`` approval prompt, a Slack relay, or a 30-minute reaper slot. The
review runs silently.

The executor is async; the (synchronous, threaded) review driver bridges to it
via ``asyncio.run_coroutine_threadsafe`` on the gateway event loop, and brackets
each run with ``begin_batch()`` / ``end_batch()``. See ``backend/routes.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

# The app root holds ``sage_lib/``; put it on sys.path so ``from sage_lib import store``
# resolves on import (mirrors the sys.path setup in sibling ``review_driver.py``).
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

try:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    )
except ImportError:  # pragma: no cover - standalone / test fallback
    AcpRuntime = None  # type: ignore[assignment,misc]
    EVENT_TEXT_CHUNK = "text_chunk"  # type: ignore[assignment]
    EVENT_TOOL_CALL = "tool_call"  # type: ignore[assignment]
    EVENT_PERMISSION_REQUEST = "permission_request"  # type: ignore[assignment]
    EVENT_COMPLETE = "complete"  # type: ignore[assignment]
    STOP_REASON_STALE_RECOVER = "stale_recover"  # type: ignore[assignment]
    STOP_REASON_TOOL_STALL = "error: tool stall"  # type: ignore[assignment]

try:  # agents dir resolver — honors KIRO_HOME so a pod reads its own specs
    from kiro_crew.config.paths import kiro_agents_dir
except Exception:  # pragma: no cover - standalone / test fallback
    # Deliberately NOT a hard-coded ~/.kiro/agents fallback: a second spelling of
    # that path is what this PR removes. Unavailable means "cannot look a spec
    # up", and both call sites already degrade to their documented defaults.
    kiro_agents_dir = None  # type: ignore[assignment]

try:  # SEL audit — the runtime layer has no audit_source, so the pool emits its own
    from kiro_crew.sel import sel as _sel
except Exception:  # pragma: no cover - standalone / test fallback
    _sel = None  # type: ignore[assignment]

from sage_lib import store  # noqa: E402

logger = logging.getLogger(__name__)


def _is_abnormal_stop(reason: str) -> bool:
    """True when an EVENT_COMPLETE stop_reason means the turn did NOT finish
    cleanly — timeout, stale-recovery, tool-stall, or any ``error: *`` (see
    acp/session_handle.py / acp/types.py). A review that ended this way must be
    reported as a failure, not silently returned as partial success (which would
    wrongly mark the PR reviewed / post incomplete findings). The known abnormal
    reason CONSTANTS are matched explicitly (not just by an ``error`` prefix) so a
    future rename of a reason string can't silently reclassify it as success."""
    r = (reason or "").strip().lower()
    if not r:
        return False
    if r in (str(STOP_REASON_TOOL_STALL).lower(),
             str(STOP_REASON_STALE_RECOVER).lower(), "timeout"):
        return True
    return r.startswith("error")


# ── Tunables (resource limits live here for easy future updates) ──
MAX_CONCURRENT = 5        # default max reviews running at once (config: review.max_concurrent)
MAX_CONCURRENT_CEIL = 30  # hard ceiling — "review all" can raise concurrency up to here
MAX_STARTING = 2          # (legacy) retained for back-compat stats; single runtime has no cold-start throttle
DEFAULT_TASK_TIMEOUT = 5400.0   # 90 min per review turn. The single-pass review
#   is ONE heavier turn (design + all code dimensions) that replaces up to 5 old
#   turns, so the old 30-min cap force-killed legitimately-working large-PR reviews
#   (stop_reason='timeout'). 90 min gives real headroom while staying well under the
#   runtime's 2h (_DEFAULT_PROMPT_TIMEOUT) default that chat/subagent turns use.
REVIEW_AGENT = "code-review-sage-reviewer"  # dedicated lean reviewer agent (shell-
#   enabled so it can run the `gh` CLI to fetch/post GitHub PR reviews). The per-task
#   prompt loads the `sage-review` skill on top of it.
_FALLBACK_AGENT = "kirocrew"     # default agent when the reviewer agent isn't installed

# Reasoning/thinking effort for the review workers. Empty string = "no explicit
# override; inherit the model/provider default" (the config default), rather than
# pinning "max". A user can still choose a concrete level in the app settings.
_DEFAULT_EFFORT = ""
# The reviewer inherits the SYSTEM default model (config.DEFAULT_MODEL, e.g.
# "auto") rather than a pinned model. This constant is the fallback used only
# when the agent config is missing/unreadable.
try:
    from kiro_crew.config.loader import DEFAULT_MODEL as _SYSTEM_DEFAULT_MODEL
except Exception:  # pragma: no cover - defensive (config import cost/cycle)
    _SYSTEM_DEFAULT_MODEL = "auto"
_DEFAULT_REVIEW_MODEL = _SYSTEM_DEFAULT_MODEL

# Valid concrete effort levels — sourced from kiro_crew.effort (single source of
# truth), not a hardcoded list. "" (inherit default) is handled separately.
try:
    from kiro_crew.effort import EFFORT_LEVELS as VALID_EFFORTS
except Exception:  # pragma: no cover - defensive
    VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _get_review_settings() -> dict:
    """Read user-configured model and effort from config.json → review section.
    Returns {"model": str|None, "effort": str}. None model = use agent default;
    "" effort = inherit the model/provider default."""
    try:
        cfg = store.load_config()
        review = cfg.get("review", {})
        model = review.get("model") or None  # None/"" → agent default
        effort = review.get("effort", _DEFAULT_EFFORT)
        if effort and effort not in VALID_EFFORTS:  # "" is valid (= inherit)
            effort = _DEFAULT_EFFORT
        return {"model": model, "effort": effort}
    except Exception:
        return {"model": None, "effort": _DEFAULT_EFFORT}


def effective_max_concurrent() -> int:
    """Configured max concurrent reviews (config: ``review.max_concurrent``).

    Because all reviews multiplex onto a single shared ``AcpRuntime`` (one
    subprocess), concurrency is a plain semaphore width rather than a bound on
    process count. Defaults to ``MAX_CONCURRENT`` (5) and is clamped to
    ``[1, MAX_CONCURRENT_CEIL]`` so a "review all open PRs" batch can fan out (up
    to 30) without an operator editing code, but can never be set unbounded."""
    try:
        cfg = store.load_config()
        val = int((cfg.get("review") or {}).get("max_concurrent", MAX_CONCURRENT))
    except Exception:
        val = MAX_CONCURRENT
    return max(1, min(val, MAX_CONCURRENT_CEIL))


# Back-compat alias: code that references REVIEW_EFFORT gets the default.
REVIEW_EFFORT = _DEFAULT_EFFORT


def _resolve_review_agent(preferred: str = REVIEW_AGENT) -> str:
    """Use the dedicated reviewer agent if it's installed, else fall back to the
    `kirocrew` agent. GitHub posting runs the `gh` CLI, so the chosen agent needs
    shell access; review reasoning still runs on the fallback so a missing
    reviewer agent degrades gracefully rather than failing."""
    if kiro_agents_dir is None:  # pragma: no cover - standalone fallback
        return _FALLBACK_AGENT
    try:
        if (kiro_agents_dir() / f"{preferred}.json").is_file():
            return preferred
    except Exception:
        pass
    return _FALLBACK_AGENT


def _review_work_dir() -> Optional[str]:
    """Working directory for a review worker = the installed app root, so the
    gate/deep prompts' RELATIVE paths (`sage_lib/pipeline.py`, `data/results/<id>.json`)
    resolve to exactly where the driver reads/writes. Without this the worker's
    default cwd (<config_dir>/workspace) sends the result record to the wrong dir
    and the driver sees "gate produced no verdict". Falls back to the AcpClient
    default if the app root can't be resolved."""
    try:
        return str(store.app_root())
    except Exception:
        try:
            return str(store.crew_home() / "apps" / "code-review-sage")
        except Exception:
            return None


def _reviewer_model(agent: str) -> str:
    """The model the review *agent* runs. Resolution order:
    1. The user-configured model in config.json (review.model) — explicit override.
    2. The model pinned on the agent's json (~/.kiro/agents/<agent>.json).
    3. The dedicated reviewer's default model.
    kiro-cli applies effort via a per-model cli.json overlay, so the overlay MUST be
    keyed on the model the agent actually runs."""
    cfg_model = _get_review_settings().get("model")
    if isinstance(cfg_model, str) and cfg_model:
        return cfg_model
    if kiro_agents_dir is None:  # pragma: no cover - standalone fallback
        return _DEFAULT_REVIEW_MODEL
    try:
        cfg = json.loads(
            (kiro_agents_dir() / f"{agent}.json").read_text(encoding="utf-8"))
        model = cfg.get("model")
        if isinstance(model, str) and model:
            return model
    except Exception:
        pass
    return _DEFAULT_REVIEW_MODEL


def reviewer_info() -> dict:
    """Resolved reviewer identity for display in the dashboard: the agent in use,
    the model it actually runs (user override → agent default → fallback), and the
    thinking effort level (user-configured) applied to both review phases."""
    agent = _resolve_review_agent()
    settings = _get_review_settings()
    return {"agent": agent, "model": _reviewer_model(agent),
            "effort": settings.get("effort", _DEFAULT_EFFORT),
            "model_source": "config" if settings.get("model") else "agent-default"}


def _write_effort_overlay(work_dir: str, model: str, effort: str = REVIEW_EFFORT) -> None:
    """Make the kiro-cli pool worker run at ``effort`` thinking depth.

    kiro-cli reads a WORKSPACE cli.json overlay at ``<work_dir>/.kiro/settings/cli.json``
    on session/new; workspace settings override the global ``~/.kiro/settings/cli.json``,
    so this is scoped to the review worker's cwd (the app root) and NEVER changes the
    user's own interactive sessions. Schema (canonical impl:
    ``kiro_crew/providers/acp.py:_write_cli_overlay``)::

        {"chat.modelDefaults": {"<model>": {"output_config": {"effort": "<level>"}}}}

    Inlined here (stdlib-only) so the app stays self-contained and the unit test is
    hermetic. Merge-safe + idempotent; best-effort (logs and continues on error so a
    bad overlay write never breaks a review)."""
    try:
        settings_dir = Path(work_dir) / ".kiro" / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)
        cli_json = settings_dir / "cli.json"
        try:
            existing = json.loads(cli_json.read_text(encoding="utf-8")) if cli_json.exists() else {}
        except (json.JSONDecodeError, OSError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        defaults = existing.get("chat.modelDefaults")
        if not isinstance(defaults, dict):
            defaults = {}
        model_cfg = defaults.get(model)
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        output_cfg = model_cfg.get("output_config")
        if not isinstance(output_cfg, dict):
            output_cfg = {}
        output_cfg["effort"] = effort
        model_cfg["output_config"] = output_cfg
        defaults[model] = model_cfg
        existing["chat.modelDefaults"] = defaults
        cli_json.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("could not write review effort overlay (work_dir=%s)", work_dir, exc_info=True)


class _BatchRuntimeHolder:
    """Owns the ONE shared ``AcpRuntime`` for a review batch.

    Lifecycle is tied to the batch via a run-level reference count (``_batches``),
    NOT to individual sessions — the driver dispatches several tasks per PR (gate,
    deep, follow-ups, post), each its own short-lived ``AcpSessionHandle``, with
    momentary zero-session gaps between them; killing on a zero-session window
    would respawn the subprocess on every task. Instead:

      * ``begin_batch()`` (0->1 runs) lazily ``spawn()``s one runtime.
      * every task multiplexes its own session onto that runtime.
      * ``end_batch()`` decrements; when it drains to 0 the whole runtime is
        ``kill()``ed — one subprocess dies and reclaims all RSS (the "no per-turn
        compaction" caveat is bounded to a single batch this way).

    Concurrent/overlapping runs share the runtime and keep it alive until the last
    ``end_batch()``. All state is guarded by ``_lock``.
    """

    def __init__(self, agent: str, work_dir: Optional[str]) -> None:
        self._agent = agent
        self._work_dir = work_dir
        self._runtime: Optional["AcpRuntime"] = None
        self._batches = 0
        self._lock = asyncio.Lock()

    async def begin_batch(self) -> None:
        async with self._lock:
            # Ensure the runtime FIRST, then count the batch. If the spawn raises
            # (unimportable AcpRuntime / transient kiro-cli launch failure), we must
            # NOT leave _batches incremented — otherwise it can never drain back to
            # 0 and the shared subprocess would never be killed (RSS leak that
            # defeats the batch-scoped lifecycle).
            await self._ensure_runtime_locked()
            self._batches += 1

    async def end_batch(self) -> None:
        async with self._lock:
            self._batches = max(0, self._batches - 1)
            rt = None
            if self._batches == 0:
                rt, self._runtime = self._runtime, None
        if rt is not None:          # kill outside the lock (SIGTERM->SIGKILL can block)
            await self._kill(rt)

    async def acquire(self) -> "AcpRuntime":
        """Return the live shared runtime, spawning/self-healing if needed."""
        async with self._lock:
            return await self._ensure_runtime_locked()

    async def force_shutdown(self) -> None:
        """Tear the runtime down regardless of batch count (app disable / standalone)."""
        async with self._lock:
            self._batches = 0
            rt, self._runtime = self._runtime, None
        if rt is not None:
            await self._kill(rt)

    async def _ensure_runtime_locked(self) -> "AcpRuntime":
        rt = self._runtime
        if rt is not None and rt.is_alive():
            return rt
        if rt is not None:                          # reap a dead one first
            await self._kill(rt)
        if AcpRuntime is None:
            raise RuntimeError("AcpRuntime unavailable (kiro_crew.acp.runtime not importable)")
        # Effort overlay is read by kiro-cli at each session/new, so it must be on
        # disk before spawn. Keyed on the resolved model (config override -> agent
        # default). Best-effort — a bad overlay never blocks the review.
        if self._work_dir:
            try:
                _write_effort_overlay(
                    self._work_dir, _reviewer_model(self._agent),
                    _get_review_settings().get("effort", _DEFAULT_EFFORT))
            except Exception:
                logger.debug("could not write review effort overlay", exc_info=True)
        # work_dir + sandbox_mode="auto" mirror the old AcpClient worker: the
        # OS sandbox scrubs credential paths/env for this LLM-directed subprocess
        # (GitHub fetch/post run via the `gh` CLI's own auth; the worker only
        # writes data/results and runs `python3 sage_lib/pipeline.py`).
        rt = AcpRuntime(agent=self._agent, work_dir=self._work_dir, sandbox_mode="auto")
        await rt.spawn()
        self._runtime = rt
        logger.info("code-review-sage runtime spawned (agent=%s, cwd=%s)",
                    self._agent, self._work_dir)
        return rt

    async def _kill(self, rt: "AcpRuntime") -> None:
        try:
            await rt.kill()
        except Exception:
            logger.debug("code-review-sage runtime kill error", exc_info=True)

    def stats(self) -> dict:
        rt = self._runtime
        alive = bool(rt is not None and rt.is_alive())
        active = 0
        if rt is not None:
            try:
                active = len(getattr(rt, "_session_queues", {}) or {})
            except Exception:
                active = 0
        return {"runtime_alive": alive, "active_sessions": active, "batches": self._batches}


class ReviewPool:
    """Code Review Sage's review executor — **one shared, batch-scoped
    ``AcpRuntime``** multiplexing one ``AcpSessionHandle`` per task.

    Replaces the former pool of ``AcpClient`` subprocesses (one process per
    worker + a full process respawn between CRs). Now a single kiro-cli subprocess
    serves every concurrent review; per-task isolation is a distinct ``sessionId``
    (created + ``destroy()``ed per task, so no context leaks between reviews), and
    the whole runtime is torn down when the batch drains (see ``_BatchRuntimeHolder``).

    Concurrency is a plain semaphore over session-handles (``review.max_concurrent``,
    default 5, ceiling 30) — independent of subprocess count. These sessions are
    created directly on the runtime, NOT via ``/api/spawn``, so they produce no
    agent card, ``:lock:`` prompt, Slack relay, or reaper slot; the review runs
    silently. The runtime layer has no ``audit_source``, so this class emits the
    per-tool SEL audit itself (parity with the old ``AcpClient`` worker).
    """

    def __init__(
        self,
        max_workers: Optional[int] = None,
        agent: Optional[str] = None,
        work_dir: Optional[str] = None,
        # accepted-and-ignored for back-compat with older callers/tests:
        max_starting: Optional[int] = None,
        worker_factory: Optional[object] = None,
    ) -> None:
        self._agent = _resolve_review_agent(agent or REVIEW_AGENT)
        self._work_dir = work_dir if work_dir is not None else _review_work_dir()
        # Auto mode = no explicit max_workers -> the semaphore tracks the live
        # review.max_concurrent config (resized per batch). An explicit value
        # (tests / standalone) is honored verbatim and never resized.
        self._auto_max = not max_workers
        self._max = int(max_workers) if max_workers else effective_max_concurrent()
        self._max = max(1, min(self._max, MAX_CONCURRENT_CEIL))
        self._sema = asyncio.Semaphore(self._max)
        self._holder = _BatchRuntimeHolder(self._agent, self._work_dir)
        self._closed = False

    async def begin_batch(self) -> None:
        """Open a review batch — lazily spawns the shared runtime (0->1 runs)."""
        if self._closed:
            raise RuntimeError("ReviewPool is shut down")
        # Pick up a changed review.max_concurrent for the NEW batch. In-flight
        # sends keep the semaphore object they already entered; new sends use the
        # resized one. This makes a settings change take effect without a restart.
        # Only in auto mode — an explicit max_workers is honored verbatim.
        if self._auto_max:
            eff = effective_max_concurrent()
            if eff != self._max:
                self._max = eff
                self._sema = asyncio.Semaphore(eff)
        await self._holder.begin_batch()

    async def end_batch(self) -> None:
        """Close a review batch — kills the runtime once the last batch drains."""
        await self._holder.end_batch()

    async def send(self, task: str, timeout: float = DEFAULT_TASK_TIMEOUT,
                   on_activity: Callable[[str, int], None] | None = None) -> str:
        """Run one review task on its own session of the shared runtime and return
        the final assistant text. Auto-approves every tool permission (the reviewer
        runs the `gh` CLI + shell) and emits a per-tool SEL audit. The session is
        always ``destroy()``ed on completion so its context never leaks."""
        if self._closed:
            raise RuntimeError("ReviewPool is shut down")
        async with self._sema:
            runtime = await self._holder.acquire()
            handle = None
            try:
                # agent=None -> inherit the runtime's agent (spawned with --agent);
                # cwd=app root so relative prompt paths + the effort overlay resolve.
                handle = await runtime.create_session(cwd=self._work_dir, agent=None)
                gen = handle.prompt(task, timeout=timeout)
                parts: list[str] = []
                stop_reason = ""
                steps = 0
                try:
                    async for ev in gen:
                        kind = getattr(ev, "kind", None)
                        if kind == EVENT_TEXT_CHUNK:
                            parts.append(getattr(ev, "text", "") or "")
                        elif kind == EVENT_TOOL_CALL:
                            await self._audit_tool(handle, ev)
                            steps += 1
                            if on_activity is not None:
                                try:
                                    on_activity(
                                        str(getattr(ev, "title", "") or ""), steps)
                                except Exception:
                                    logger.debug("activity callback failed",
                                                 exc_info=True)
                        elif kind == EVENT_PERMISSION_REQUEST:
                            # Auto-approve (the reviewer needs `gh` + shell) AND record
                            # the permission DECISION in the security ledger, tagged with
                            # its request id. The blocking backend-security-controls rule
                            # requires every permission decision to emit an SEL event, and
                            # the EVENT_TOOL_CALL audit carries no decision/request id.
                            req_id = getattr(ev, "request_id", "")
                            try:
                                await handle.approve_tool(req_id)
                            except Exception:
                                logger.debug("tool approve failed", exc_info=True)
                            else:
                                await self._audit_tool(
                                    handle, ev, request_id=req_id,
                                    outcome="auto_approved")
                        elif kind == EVENT_COMPLETE:
                            stop_reason = getattr(ev, "stop_reason", "") or ""
                            break
                finally:
                    # Deterministically close the async generator instead of leaving
                    # it suspended-until-GC after the EVENT_COMPLETE break. prompt()
                    # is typed AsyncIterator (no aclose in the protocol) but is an
                    # async generator at runtime — close it if it supports it.
                    aclose = getattr(gen, "aclose", None)
                    if aclose is not None:
                        await aclose()
                # An abnormal completion (timeout / tool-stall / stale-recovery /
                # error:*) means the review did NOT finish — surface it as a failure
                # so make_sync_dispatch reports ok=False and the driver never marks
                # the PR reviewed or posts on partial output.
                if _is_abnormal_stop(stop_reason):
                    raise RuntimeError(
                        f"review turn ended abnormally (stop_reason={stop_reason!r})")
                return "".join(parts)
            finally:
                if handle is not None:
                    try:
                        await handle.destroy()
                    except Exception:
                        logger.debug("session destroy error", exc_info=True)

    async def _audit_tool(self, handle: object, ev: object, *,
                          request_id: object = None,
                          outcome: str = "auto_approved") -> None:
        """Emit a per-tool SEL audit (the runtime layer has no ``audit_source``,
        so without this the reviewer's tool calls would never reach the security log
        — parity with ``AcpClient._maybe_audit_tool_call``). Best-effort + bounded:
        offloaded to a thread with a timeout so a hung SEL backend can never stall a
        review turn, and a failure never breaks tool dispatch.

        Called on two events:
          * ``EVENT_TOOL_CALL`` — records the observed tool invocation.
          * ``EVENT_PERMISSION_REQUEST`` — records the permission DECISION itself
            (the approve, tagged with its ``request_id``). The blocking
            ``backend-security-controls`` rule requires EVERY permission decision to
            emit an SEL event, and the tool_call audit carries no decision/request id.
        """
        if _sel is None:
            return
        rid = "" if request_id is None else str(request_id)
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: _sel().log_tool_invocation(
                        session_key=getattr(handle, "session_id", "") or "",
                        agent=self._agent,
                        source="subagent",
                        tool_name=getattr(ev, "title", None) or "unknown",
                        tool_kind=getattr(ev, "tool_kind", None) or "",
                        outcome=outcome,
                        request_id=rid,
                    ),
                ),
                timeout=5.0,
            )
        except Exception:
            logger.warning("code-review-sage SEL audit failed", exc_info=True)

    def stats(self) -> dict:
        """Live occupancy for the dashboard. Keeps the legacy key names
        (``workers``/``idle``/``busy``/``max``/``starting_max``) so the existing UI
        keeps working, and adds the runtime-model keys."""
        h = self._holder.stats()
        active = h["active_sessions"]
        return {
            "workers": active, "idle": 0, "busy": active,
            "max": self._max, "starting_max": MAX_STARTING,
            "runtime_alive": h["runtime_alive"], "active_sessions": active,
            "batches": h["batches"],
        }

    async def shutdown(self) -> None:
        """Force-tear-down the runtime (app disable / gateway shutdown / standalone)."""
        self._closed = True
        await self._holder.force_shutdown()


# ── Process-wide singleton (owned by the gateway backend) ──
_POOL: Optional[ReviewPool] = None


def get_pool() -> ReviewPool:
    """Lazily create and return the process-wide review pool."""
    global _POOL
    if _POOL is None or _POOL._closed:
        _POOL = ReviewPool()
    return _POOL


async def shutdown_pool() -> None:
    """Tear down the singleton pool (called on app disable / gateway shutdown)."""
    global _POOL
    if _POOL is not None:
        await _POOL.shutdown()
        _POOL = None


def pool_stats() -> dict:
    """Live occupancy for the dashboard. Safe to call from a status handler —
    returns zeros (no lazy creation) when the pool hasn't started yet."""
    if _POOL is None:
        # No pool yet (before the first review) — report the static default cap.
        # Avoid effective_max_concurrent() here: it reads config.json, and this
        # runs synchronously on the gateway event loop from the /runs handler.
        return {"workers": 0, "idle": 0, "busy": 0,
                "max": MAX_CONCURRENT, "starting_max": MAX_STARTING,
                "runtime_alive": False, "active_sessions": 0, "batches": 0}
    return _POOL.stats()


# Bridge type the driver expects: a sync callable (task, timeout) -> result dict,
# optionally taking an ``on_activity`` reporter for the reviewer's tool stream.
DispatchFn = Callable[..., dict]


def make_sync_dispatch(
    loop: asyncio.AbstractEventLoop,
    pool: ReviewPool,
    default_timeout: float = DEFAULT_TASK_TIMEOUT,
) -> DispatchFn:
    """Build a synchronous ``(task, timeout) -> {ok, output, error}`` dispatch that
    bridges the threaded review driver to the async ``pool`` running on ``loop``.

    The driver fans changes out across worker threads and calls this synchronously;
    each call schedules ``pool.send`` on the gateway event loop and blocks the
    calling thread until the worker's turn finishes (the result record is on disk).
    Never raises — failures come back in the ``error`` field so the driver's phase
    switch can react deterministically."""

    def dispatch(task: str, timeout: float = default_timeout,
                 on_activity: Callable[[str, int], None] | None = None) -> dict:
        try:
            # The callback fires on the gateway loop's thread while the driver's
            # worker thread blocks here; the progress writer it feeds is lock-
            # guarded and copy-on-write, so that crossing is safe.
            fut = asyncio.run_coroutine_threadsafe(
                pool.send(task, timeout=timeout, on_activity=on_activity), loop)
            # Give the bridge a little headroom past the task timeout so the
            # pool's own timeout fires first with a cleaner error.
            out = fut.result(timeout=timeout + 60)
            return {"ok": True, "output": out, "error": ""}
        except Exception as e:
            return {"ok": False, "output": "", "error": str(e)}

    return dispatch
