"""Warm session pool for workflow ``ctx.agent()`` calls — kills per-call cold-start.

Background: the default ``agent_fn`` (``agent_exec.build_agent_fn``) gives every
``ctx.agent()`` call its own fresh ``SessionManager`` session keyed
``wf:{run_id}:{i}`` and tears it down (``release(cleanup=True)``) afterwards. Each
call therefore pays a FULL cold start — subprocess spawn + ACP ``initialize`` +
``session/new`` (the MCP-toolset + system-prompt load, the dominant cost per
cold-start profiling). An 8-agent run = 8 cold starts.

This module reuses the generic, already-tested :class:`kiro_crew.acp.worker_pool.WorkerPool`
engine to keep a small set of WARM workflow sessions alive and reuse them across
calls. Semantics that make this correct:

  * **Isolation preserved** — the engine hands each *concurrent* task a DISTINCT
    worker (its own live session), so parallel ``ctx.agent()`` calls never share
    conversational state, exactly like the per-call-session model they replace.
  * **Warm reuse** — a *sequential* task reuses an idle worker after a cheap
    clean-slate reset (``provider.new_conversation()`` — fresh ``session/new`` on
    the same live process, skipping spawn + ``initialize``). No cold start.
  * **Self-healing** — a worker whose process died is retired and replaced.
  * **Bounded** — at most ``max_workers`` live sessions; startup throttled to
    ``max_starting`` concurrent spawns (matches the workflow concurrency cap).

Each worker owns a stable SessionManager key ``wf-pool:{run_id}:{worker_id}`` so
the manager's own warm per-key fast path returns it instantly on reuse.

Kept dependency-light: ``SessionManager`` and ``stream_and_collect`` are injected,
so this is unit-testable with fakes and never spawns kiro-cli in tests.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Callable, Optional

from kiro_crew.acp.worker_pool import WorkerPool
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Per-step tool-call ceiling — shared with the per-call path (agent_exec) so a
# single edit retunes both; a hand-duplicated copy here would silently diverge
# the pooled vs fallback ceilings (no test pins them equal).
from kiro_crew.workflows.agent_exec import _MAX_TURNS_PER_STEP

logger = logging.getLogger(__name__)


async def _run_step(provider: Any, prompt: str, *, timeout: Optional[float] = None) -> str:
    """Stream one workflow agent step through ``provider`` and redact its output.

    Single source of truth for the per-step contract shared by the pooled worker
    and the ``session=`` bypass path: AUTO_APPROVE policy, the shared
    ``_MAX_TURNS_PER_STEP`` tool-call ceiling, an optional per-task ``timeout``
    (via ``asyncio.wait_for`` — the pool passes its per-task bound here so a
    wedged turn is terminated instead of holding a permit until the run ceiling),
    and the credential + exfiltration-URL output redaction pair (parity with
    ``agent_exec`` — prevents credential leakage into workflow results stored in
    history / injected into parent chat).
    """
    coro = stream_and_collect(
        provider,
        prompt,
        approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
        max_turns=_MAX_TURNS_PER_STEP,
    )
    text = await (asyncio.wait_for(coro, timeout) if timeout is not None else coro)
    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    return text


class _WorkflowSessionWorker:
    """A warm ``SessionManager`` session reused across workflow agent steps.

    Implements the :class:`kiro_crew.acp.worker_pool.PoolWorker` protocol. One
    worker == one long-lived session keyed ``wf-pool:{run_id}:{worker_id}``.
    """

    def __init__(
        self,
        sessions: Any,
        *,
        key: str,
        agent: Optional[str],
        model: Optional[str],
        cwd: Optional[str],
    ) -> None:
        self._sessions = sessions
        self._key = key
        self._agent = agent
        self._model = model
        self._cwd = cwd
        self._provider: Any = None

    async def start(self) -> None:
        """Cold-start THIS worker's session once (the only cold start it pays)."""
        provider, *_ = await self._sessions.get_or_create(
            self._key,
            agent=self._agent,
            model=self._model,
            cwd=self._cwd,
        )
        self._provider = provider

    async def send_message(self, prompt: str, timeout: float = 1800.0) -> str:
        if self._provider is None:
            await self.start()
        # Honor the pool's per-task timeout: a wedged turn is terminated here
        # instead of holding a _task_sema permit until the run-level ceiling.
        return await _run_step(self._provider, prompt, timeout=timeout)

    async def reset(self) -> None:
        """Cheap clean slate before REUSE — fresh conversation on the warm process.

        Uses ``provider.new_conversation()`` when the backend exposes it (kiro +
        claude ACP both do), which skips the expensive spawn+initialize. If it is
        unavailable or fails, fall back to a hard ``SessionManager.reset`` so a
        reused worker can NEVER carry prior-task context into the next task
        (correctness over speed on the fallback path)."""
        prov = self._provider
        new_conv = getattr(prov, "new_conversation", None) if prov is not None else None
        if new_conv is not None:
            try:
                await new_conv()
                return
            except Exception:
                logger.debug(
                    "workflow pool: new_conversation reset failed for %s, "
                    "falling back to hard reset",
                    self._key,
                    exc_info=True,
                )
        # Fallback: hard reset (kill+respawn) via the manager, then re-acquire.
        try:
            await self._sessions.reset(self._key)
        except Exception:
            logger.debug("workflow pool: hard reset failed for %s", self._key, exc_info=True)
        self._provider = None
        await self.start()

    async def shutdown(self) -> None:
        # A pooled worker owns an ephemeral ``wf-pool:`` session that can never
        # resume, so tear it down for real: release the turn semaphore, then
        # ``destroy()`` (kills the provider process + deletes the session_map).
        # ``release(cleanup=True)`` alone would NOT reap it — its file-cleanup
        # branch only fires for ``subagent:`` keys — leaking the warm kiro-cli
        # process across runs.
        try:
            self._sessions.release(self._key)
        except Exception:
            logger.debug("workflow pool: release failed for %s", self._key, exc_info=True)
        try:
            await self._sessions.destroy(self._key)
        except Exception:
            logger.debug("workflow pool: destroy failed for %s", self._key, exc_info=True)
        self._provider = None

    def is_alive(self) -> bool:
        prov = self._provider
        if prov is None:
            return False
        checker = getattr(prov, "is_process_alive", None) or getattr(prov, "is_alive", None)
        try:
            return bool(checker()) if checker else True
        except Exception as exc:
            logger.debug(
                "workflow pool: is_alive check failed for %s: %s",
                self._key,
                type(exc).__name__,
            )
            return False


def build_pooled_agent_fn(
    sessions: Any,
    *,
    run_id: str,
    default_agent: Optional[str] = None,
    default_model: Optional[str] = None,
    cwd: Optional[str] = None,
    max_workers: int = 4,
    max_starting: int = 2,
    max_identities: int = 8,
) -> "tuple[Callable[[str, dict], Any], _AggregatePool]":
    """Return ``(agent_fn, pool)`` where ``agent_fn`` reuses WARM sessions.

    Drop-in replacement for ``agent_exec.build_agent_fn`` for the DEFAULT
    (subagent-semantics) path: each ``ctx.agent()`` call runs on a pooled warm
    session instead of a fresh cold-started one. A ``session=<key>`` (stateful)
    call still gets its own dedicated session via ``get_or_create`` so a chain
    keeps its history — pooling only covers the ephemeral default path.

    The caller owns ``pool`` and MUST ``await pool.shutdown()`` when the run ends
    (e.g. in the runner's finally / on_done) so the warm sessions are released.
    Per-call ``ctx.agent(agent=/model=/cwd=)`` overrides are honored: each
    distinct ``(agent, model, cwd)`` identity gets its OWN warm sub-pool (so a
    multi-specialist fan-out still gets warm reuse per specialist, and a worker
    built for one identity never serves a call that asked for another). Calls
    with no override share the default sub-pool. ``pool.shutdown()`` tears down
    every sub-pool.
    """
    worker_ids = itertools.count()

    def _make_pool(
        agent: Optional[str], model: Optional[str], work_dir: Optional[str]
    ) -> WorkerPool:
        def _factory() -> _WorkflowSessionWorker:
            wid = next(worker_ids)
            return _WorkflowSessionWorker(
                sessions,
                key=f"wf-pool:{run_id}:{wid}",
                agent=agent,
                model=model,
                cwd=work_dir,
            )

        return WorkerPool(
            _factory,
            max_workers=max(1, max_workers),
            max_starting=max(1, max_starting),
            name=f"wf-pool:{run_id}",
        )

    # Default sub-pool (no per-call override) + a registry of identity-keyed
    # sub-pools created on demand. ``pool`` (the default) is returned to the
    # caller for shutdown; it delegates to every sub-pool via _AggregatePool.
    default_pool = _make_pool(default_agent, default_model, cwd)
    subpools: dict[tuple[Optional[str], Optional[str], Optional[str]], WorkerPool] = {}

    def _pool_for(
        agent: Optional[str], model: Optional[str], work_dir: Optional[str]
    ) -> Optional[WorkerPool]:
        # ``agent=None`` on the call means "use the run default" — identical to
        # build_agent_fn's ``opts.get("agent") or default_agent``. Resolve first
        # so a call that explicitly asks for the default reuses the default pool.
        eff_agent = agent or default_agent
        eff_model = model or default_model
        eff_cwd = work_dir or cwd
        if (eff_agent, eff_model, eff_cwd) == (default_agent, default_model, cwd):
            return default_pool
        key = (eff_agent, eff_model, eff_cwd)
        sp = subpools.get(key)
        if sp is not None:
            return sp
        # Aggregate bound: each identity sub-pool holds up to ``max_workers``
        # live kiro-cli processes, so an unbounded number of distinct identities
        # (a run can make up to 1000 calls, each with a unique model string)
        # would blow past the run-wide worker cap and exhaust memory / fds.
        # Once ``max_identities`` distinct sub-pools exist, a new identity gets
        # NO pool (returns None) and the caller runs it on the unpooled
        # create-run-destroy path — so total live workers stay bounded at
        # ``(max_identities + 1) * max_workers``.
        if len(subpools) >= max(1, max_identities):
            return None
        sp = _make_pool(eff_agent, eff_model, eff_cwd)
        subpools[key] = sp
        return sp

    _unpooled = itertools.count()

    async def _run_unpooled(prompt: str, opts: dict) -> Any:
        # Fallback when the identity cap is hit: a one-shot ephemeral session,
        # torn down after the call so it never lingers. No warm reuse, but
        # bounded — this is the overflow valve, not the common path.
        key = f"wf-unpooled:{run_id}:{next(_unpooled)}"
        provider, *_ = await sessions.get_or_create(
            key,
            agent=opts.get("agent") or default_agent,
            model=opts.get("model") or default_model,
            cwd=opts.get("cwd") or cwd,
        )
        try:
            return await _run_step(provider, prompt)
        finally:
            try:
                await sessions.destroy(key)
            except Exception:
                logger.debug("workflow pool: unpooled session teardown failed", exc_info=True)

    async def agent_fn(prompt: str, opts: dict) -> Any:
        # Stateful ``session=`` calls bypass the pool: they need a stable, named
        # session that persists across steps, not a reset-between-uses worker.
        named = opts.get("session")
        if named is not None:
            provider, *_ = await sessions.get_or_create(
                named,
                agent=opts.get("agent") or default_agent,
                model=opts.get("model") or default_model,
                cwd=opts.get("cwd") or cwd,
            )
            return await _run_step(provider, prompt)
        # Ephemeral default path: run on a warm worker from the sub-pool matching
        # this call's (agent, model, cwd) — honoring per-call overrides exactly as
        # the per-call-session model (build_agent_fn) it replaces did.
        target = _pool_for(opts.get("agent"), opts.get("model"), opts.get("cwd"))
        if target is None:
            # Identity cap reached — run unpooled (bounded overflow valve).
            return await _run_unpooled(prompt, opts)
        return await target.send(prompt)

    pool = _AggregatePool(default_pool, subpools)
    return agent_fn, pool


class _AggregatePool:
    """Owns the default sub-pool + the on-demand identity-keyed sub-pools, and
    shuts them all down together. Returned to the caller as ``pool`` so a single
    ``await pool.shutdown()`` releases every warm session across all identities.
    """

    def __init__(
        self,
        default_pool: WorkerPool,
        subpools: dict[tuple[Optional[str], Optional[str], Optional[str]], WorkerPool],
    ) -> None:
        self._default = default_pool
        self._subpools = subpools

    async def shutdown(self) -> None:
        # Snapshot: sub-pools may be added concurrently while a run is still live.
        pools = [self._default, *list(self._subpools.values())]
        for p in pools:
            try:
                await p.shutdown()
            except Exception:
                logger.debug("workflow pool: sub-pool shutdown failed", exc_info=True)
