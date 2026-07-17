#!/usr/bin/env python3
"""Reusable review worker pool — long-lived ACP sessions, not per-CR spawns.

Mirrors the Knowledge Library's ``knowledge/llm_pool.py`` (a bounded pool of
warm ``AcpClient`` workers) but tuned for Code Review Sage:

  * **Lazy** — workers are created on demand, never eagerly. A pool that is
    never used spawns nothing.
  * **Bounded concurrency** — at most ``MAX_CONCURRENT`` tasks run at once
    (a task = one Phase-1 gate or one Phase-2 deep review for one change).
  * **Throttled startup** — at most ``MAX_STARTING`` workers spin up
    simultaneously, because ACP process launch + session handshake is the
    expensive part; a burst of CRs must not cold-start five processes at once.
  * **Clean slate per CR** — before a *reused* worker runs the next task its
    session is reset by respawning the ACP process (shutdown + start), since the
    OSS ``AcpClient`` has no in-process conversation reset, so one review never
    leaks context into another. A freshly-created worker is already clean, so the
    reset is skipped on first use.
  * **Self-healing** — a worker found dead on acquire (or one that raised
    during a task) is shut down and replaced; it never re-enters the idle set.

Crucially these workers are plain ``AcpClient`` sessions created directly — they
do NOT go through the gateway's ``/api/spawn`` / ``SubagentManager`` path, so
they never produce an agent card, a ``:lock:`` approval prompt, a Slack relay,
or a 30-minute reaper slot. The review runs silently.

The pool is async; the (synchronous, threaded) review driver bridges to it via
``asyncio.run_coroutine_threadsafe`` on the gateway event loop. See
``backend/routes.py`` for the wiring.
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
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
except ImportError:  # pragma: no cover - standalone / test fallback
    AcpClient = None  # type: ignore[assignment,misc]
    ACP_BACKEND_CLAUDE = "claude"  # type: ignore[assignment]

from sage_lib import store  # noqa: E402

# The reusable pool engine (provider-agnostic; see kiro_crew/acp/worker_pool.py).
# ReviewPool is now a thin adapter over it — the engine owns the pooling logic,
# this module owns the review-specific worker + tunables + singleton + bridge.
from kiro_crew.acp.worker_pool import PoolWorker as Worker  # noqa: E402,F401  (back-compat alias)
from kiro_crew.acp.worker_pool import WorkerFactory, WorkerPool  # noqa: E402,F401

logger = logging.getLogger(__name__)

# ── Tunables (resource limits live here for easy future updates) ──
MAX_CONCURRENT = 5        # max tasks running at once (== max live workers)
MAX_STARTING = 2          # max workers spinning up simultaneously
DEFAULT_TASK_TIMEOUT = 1800.0   # seconds per review task (gate or deep)
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


# Back-compat alias: code that references REVIEW_EFFORT gets the default.
REVIEW_EFFORT = _DEFAULT_EFFORT


def _resolve_review_agent(preferred: str = REVIEW_AGENT) -> str:
    """Use the dedicated reviewer agent if it's installed, else fall back to the
    `kirocrew` agent. GitHub posting runs the `gh` CLI, so the chosen agent needs
    shell access; review reasoning still runs on the fallback so a missing
    reviewer agent degrades gracefully rather than failing."""
    try:
        if (Path.home() / ".kiro" / "agents" / f"{preferred}.json").is_file():
            return preferred
    except Exception:
        pass
    return _FALLBACK_AGENT


def _review_work_dir() -> Optional[str]:
    """Working directory for a review worker = the installed app root, so the
    gate/deep prompts' RELATIVE paths (`sage_lib/pipeline.py`, `data/results/<id>.json`)
    resolve to exactly where the driver reads/writes. Without this the worker's
    default cwd (~/.kirocrew/workspace) sends the result record to the wrong dir
    and the driver sees "gate produced no verdict". Falls back to the AcpClient
    default if the app root can't be resolved."""
    try:
        return str(store.app_root())
    except Exception:
        try:
            base = os.environ.get("KIROCREW_HOME") or str(Path.home() / ".kirocrew")
            return str(Path(base) / "apps" / "code-review-sage")
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
    try:
        cfg = json.loads(
            (Path.home() / ".kiro" / "agents" / f"{agent}.json").read_text(encoding="utf-8"))
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


class AcpReviewWorker:
    """A warm ``AcpClient`` session running the review agent."""

    def __init__(self, agent: str = REVIEW_AGENT) -> None:
        self._client: Optional["AcpClient"] = None
        self._agent = agent

    async def start(self) -> None:
        if AcpClient is None:
            raise RuntimeError("AcpClient unavailable (kiro_crew.acp.client not importable)")
        agent = _resolve_review_agent(self._agent)
        work_dir = _review_work_dir()   # cwd = app root so relative prompt paths resolve
        settings = _get_review_settings()
        effort = settings.get("effort", _DEFAULT_EFFORT)
        # Run the worker at the user-configured thinking effort for BOTH phases.
        # kiro-cli (the default ACP backend for these workers) reads effort from a
        # per-model workspace cli.json overlay at session/new, so the overlay must
        # be on disk in `work_dir` BEFORE the process spawns. Keyed on the resolved
        # model (config override → agent default).
        if work_dir:
            _write_effort_overlay(work_dir, _reviewer_model(agent), effort)
        # sandbox_mode="auto" (the default): the OS-level sandbox bind-mounts empty
        # dirs over credential paths (.aws/.ssh/.midway/.env) and scrubs cred env
        # vars for this LLM-directed worker. Review workers need NO stored
        # credentials — GitHub fetch/post run via the `gh` CLI using gh's own
        # auth, and the worker only writes data/results and runs
        # `python3 sage_lib/pipeline.py` — so the sandbox is safe here and satisfies the
        # agent-subprocess sandboxing guideline. Degrades to a no-op if unprivileged
        # user namespaces are unavailable, so it never breaks the worker.
        self._client = AcpClient(
            agent=agent, sandbox_mode="auto", work_dir=work_dir, audit_source="subagent"
        )
        await self._client.ensure_ready()
        await self._apply_claude_effort()   # claude-backend fallback (kiro uses the overlay)
        logger.info("AcpReviewWorker ready (agent=%s, cwd=%s, effort=%s)",
                    agent, work_dir, effort)

    def pid(self) -> Optional[int]:
        """Underlying kiro-cli process PID (or None before start / after
        shutdown). The pool reads this to shield the worker from the gateway's
        periodic orphan sweep — a busy, unshielded worker is otherwise SIGKILLed
        mid-review as a false orphan ("ACP process exited (code=1)")."""
        pid = getattr(self._client, "_pid", None)
        return pid if isinstance(pid, int) and pid > 0 else None

    async def _apply_claude_effort(self) -> None:
        """Push the configured effort live on the CLAUDE backend only.

        The kiro-cli backend gets effort from the cli.json overlay written before
        spawn; pushing ``session/set_config_option`` on kiro would spam errors and
        reset the session. This branch only fires if a future build runs the pool on
        claude-agent-acp. Guarded + best-effort: a no-op when the backend exposes no
        ``effort`` selector, and never raises (effort is a quality knob, not a
        worker breaker)."""
        client = self._client
        if client is None:
            return
        effort = _get_review_settings().get("effort", _DEFAULT_EFFORT)
        try:
            if getattr(client, "backend", "") != ACP_BACKEND_CLAUDE:
                return
            if not client.supports_config_option("effort"):
                return
            await client.set_config_option("effort", effort)
        except Exception:
            logger.debug("could not push claude effort=%s", effort, exc_info=True)

    async def send_message(self, prompt: str, timeout: float = DEFAULT_TASK_TIMEOUT) -> str:
        if self._client is None or not self._client.is_ready:
            await self.start()
        assert self._client is not None
        return await self._client.send_message(prompt, timeout=timeout)

    async def reset(self) -> None:
        """Clean slate for the next change — guarantee per-change session
        isolation (the reviewer's core invariant). The OSS ``AcpClient`` exposes
        no in-process conversation reset, so tear the worker's client down and
        start a fresh one; ``start()`` re-writes the effort overlay and re-runs
        ``ensure_ready``, so the new session is fully re-initialized."""
        await self.shutdown()
        await self.start()

    async def shutdown(self) -> None:
        if self._client is not None:
            try:
                await self._client.shutdown()
            except Exception:
                logger.debug("AcpReviewWorker shutdown error", exc_info=True)
            self._client = None

    def is_alive(self) -> bool:
        return self._client is not None and self._client.is_process_alive()


class ReviewPool(WorkerPool):
    """Code Review Sage's worker pool — a thin adapter over the reusable
    :class:`kiro_crew.acp.worker_pool.WorkerPool` engine. The engine owns the
    pooling guarantees (lazy, bounded concurrency, startup throttle, self-healing,
    clean-slate reset on reuse); this subclass only supplies the review-specific
    defaults: an ``AcpReviewWorker`` factory and the review tunables.

    These workers are plain ``AcpClient`` sessions created directly — NOT
    ``/api/spawn`` sub-agents — so they produce no agent card, ``:lock:`` prompt,
    Slack relay, or reaper slot, and (via ``AcpReviewWorker``) are sweep-protected.
    """

    def __init__(
        self,
        max_workers: int = MAX_CONCURRENT,
        max_starting: int = MAX_STARTING,
        worker_factory: Optional[WorkerFactory] = None,
    ) -> None:
        super().__init__(
            worker_factory or (lambda: AcpReviewWorker()),
            max_workers=max_workers,
            max_starting=max_starting,
            default_timeout=DEFAULT_TASK_TIMEOUT,
            name="ReviewPool",
        )


# ── Process-wide singleton (owned by the gateway backend) ──
_POOL: Optional[ReviewPool] = None


def get_pool() -> ReviewPool:
    """Lazily create and return the process-wide review pool."""
    global _POOL
    if _POOL is None:
        _POOL = ReviewPool()
    return _POOL


async def shutdown_pool() -> None:
    """Tear down the singleton pool (called on app disable / gateway shutdown)."""
    global _POOL
    if _POOL is not None:
        await _POOL.shutdown()
        _POOL = None


def pool_stats() -> dict:
    """Live worker-pool occupancy for the dashboard. Safe to call from a status
    handler — returns zeros (no lazy creation) when the pool hasn't started yet."""
    if _POOL is None:
        return {"workers": 0, "idle": 0, "busy": 0,
                "max": MAX_CONCURRENT, "starting_max": MAX_STARTING}
    return _POOL.stats()


# Bridge type the driver expects: a sync callable (task, timeout) -> result dict.
DispatchFn = Callable[[str, float], dict]


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

    def dispatch(task: str, timeout: float = default_timeout) -> dict:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                pool.send(task, timeout=timeout), loop)
            # Give the bridge a little headroom past the task timeout so the
            # pool's own timeout fires first with a cleaner error.
            out = fut.result(timeout=timeout + 60)
            return {"ok": True, "output": out, "error": ""}
        except Exception as e:
            return {"ok": False, "output": "", "error": str(e)}

    return dispatch
