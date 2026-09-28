"""Production ``agent_fn`` for workflow ``ctx.agent()`` calls.

The runner takes an injected ``agent_fn(prompt, opts) -> result`` so it stays
testable with stubs and never spawns ``kiro-cli`` in tests. This module builds the
REAL one: it runs each workflow agent step through an actual model via the same
core primitive everything else uses — ``llm_helpers.stream_and_collect`` over a
provider from ``SessionManager``.

Execution model (matches the frozen contract in workflows/__init__.py):

* **default (subagent semantics):** each ``ctx.agent()`` call gets its OWN fresh,
  isolated session — keyed ``wf:{run_id}:{call_index}`` — so parallel calls don't
  share conversational state. The session is released (and reset) after the call.
* **``session=<key>`` (stateful):** the call reuses a caller-named session so a
  chain of steps shares context.

Structured output (``schema=``) is handled upstream by the runner via
``schema.run_with_schema`` — which calls this ``agent_fn`` as its text producer —
so this module only needs to return the model's text.

Kept out of the hot import path: ``SessionManager`` etc. are passed IN (the
gateway wires them at startup), so this module imports only ``llm_helpers`` types
and has no side effects on import. It is NOT in the F1 engine layering graph
(``dsl→context→runner``); it's an optional production adapter the gateway supplies
as ``agent_fn``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Callable, Optional

from kiro_crew.llm_helpers import ToolApprovalPolicy, provider_last_turn_usage, stream_and_collect
from kiro_crew.security import redact

logger = logging.getLogger(__name__)

# Signature the runner expects: async (prompt, opts) -> result.
AgentFn = Callable[[str, dict], Any]

# Per-step tool-call ceiling. Generous enough for any realistic agent step,
# but prevents infinite tool loops from prompt injection.
_MAX_TURNS_PER_STEP = 200


class WorkflowSpawnRefused(Exception):
    """A ``ctx.agent()`` step the spawn policy or the cwd allowlist refuses."""


async def vet_step_spawn(
    *, session_key: str, agent: Optional[str], cwd: Optional[str], app: str = ""
) -> Optional[str]:
    """Run the two spawn gates on one ``ctx.agent()`` step; return the cwd to launch in.

    A ``ctx.agent()`` step allocates a child agent session, so it is a spawn and it
    answers to the gates every other spawn entrypoint answers to: the
    ``capabilities.spawn`` ceiling of the PARENT surface, and the
    ``agent.subagent_cwd_allowed_roots`` allowlist for a caller-named launch
    directory. Both helpers get the arguments the subagent admission gate passes
    them (``gate.py`` declares ``agent: str = ""``/``app: str = ""``, so an unnamed
    agent is the empty string there and here); nothing new is decided here.

    ``session_key`` is the run's originating session (the parent surface the
    profile is resolved against). ``app`` is the calling app's identity, which binds
    that app's OWN profile -- precedence #1 in ``resolve_active_scope`` and the
    argument the admission gate threads; without it only the policy ceiling and the
    surface-bound profile apply, so an app-bound profile would be skipped. A run
    with no app identity passes ``""``, exactly as a non-app spawn does. ``cwd`` is
    the SCRIPT-supplied ``ctx.agent(cwd=)`` value; a run-level pin wired by the host
    is host configuration and is not re-checked. Raises ``WorkflowSpawnRefused``.

    A step that names no directory reaches NO ``await`` here, deliberately. A
    coroutine that returns without awaiting never yields to the loop, so the
    default path keeps the exact scheduling it had before this gate existed.
    Suspending unconditionally instead reorders the pooled fan-out: four gathered
    ``ctx.agent()`` calls each complete their hop before the next reaches the
    pool, so one warm worker serves all four and
    ``test_workflows_agent_pool.py::test_concurrent_calls_get_distinct_sessions``
    sees one session where it requires four.

    The governance decision therefore runs INLINE, as it does at every other
    chokepoint. It is the same call the synchronous PreToolUse host gate makes on
    every tool call, and ``ProfileStore._ensure_fresh`` documents that path as
    event-loop-reachable by design and never blocking: warm stores serve the
    pinned snapshot and a caller that cannot take the reload lock does not wait.
    Its directory fingerprint measured 57us at 5 profile files and 1.12ms at 200.
    The cwd leg IS offloaded -- a config load plus ``realpath``/``isdir``, the same
    work ``spawn_warm`` offloads -- and only when a directory was actually named.
    """
    # Function-local, deliberately, against the ``top-level-imports`` rule
    # (advisory). Two measured reasons, so a later reader can re-judge it rather
    # than guess: importing ``kiro_crew.subagent`` here adds 133 modules and
    # ~104ms to importing this module, which is the "kept out of the hot import
    # path" contract at the top of this file; and this module sits UNDER
    # ``workflows/service.py``, which ``slack/gateway.py`` imports, while
    # ``kiro_crew.subagent`` imports ``kiro_crew.slack.format`` -- that chain is
    # acyclic only while ``slack/__init__`` stays lazy, so a module-scope import
    # would make this file depend on that staying true.
    from kiro_crew.subagent import _vet_spawn_governance, validate_cwd

    denial = _vet_spawn_governance(session_key, agent or "", app=app)
    if denial:
        logger.warning("workflow agent step refused by governance: %s", denial)
        raise WorkflowSpawnRefused(f"spawn refused by governance: {denial}")
    if not cwd:
        return cwd

    def _validated() -> "tuple[str, str]":
        from kiro_crew.config.loader import KiroCrewConfig

        try:
            allowed_roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
        except Exception:
            # Fail closed, as the admission gate does: an unreadable config must
            # not re-enable an override an operator disabled with an empty list.
            allowed_roots = []
        return validate_cwd(cwd, allowed_roots)

    resolved, cwd_err = await asyncio.to_thread(_validated)
    if cwd_err:
        logger.warning("workflow agent step refused: invalid cwd")
        raise WorkflowSpawnRefused(f"spawn refused: {cwd_err}")
    return resolved


def build_agent_fn(
    sessions: Any,
    *,
    run_id: str,
    default_agent: Optional[str] = None,
    default_model: Optional[str] = None,
    cwd: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    memory_scope: Any = None,
    context_builder: Any = None,
    session_key: str = "",
    app: str = "",
) -> AgentFn:
    """Return an ``agent_fn`` that runs each workflow agent step through a model.

    ``sessions`` is a ``SessionManager``-like object exposing
    ``async get_or_create(key, *, agent, model, cwd, ...) -> (provider, *_)`` and
    ``release(key, *, cleanup=True)``. Injected so tests can pass a fake.

    ``extra_env`` is a run-level environment pin threaded into every spawned
    session, mirroring ``default_agent``/``default_model``/``cwd``. It is a
    per-run pin rather than a per-call override because ``WorkflowContext.agent()``
    exposes no ``env=`` parameter (that Protocol is frozen).
    """

    # Per-run, 0-based ephemeral session index (not a module-global, so each run
    # restarts at :0 as the ``wf:{run_id}:{call_index}`` contract documents).
    counter = itertools.count()

    async def agent_fn(prompt: str, opts: dict) -> Any:
        # Spawn gates FIRST: a refused step must allocate no session, touch no
        # memory scope and consume no session index.
        step_cwd = await vet_step_spawn(
            session_key=session_key,
            agent=opts.get("agent") or default_agent,
            cwd=opts.get("cwd"),
            app=app,
        )
        # Per-call isolated session by default; caller-named session when session=.
        session = opts.get("session")
        ephemeral = session is None
        key = session or f"wf:{run_id}:{next(counter)}"
        if memory_scope is not None:
            if session is not None:
                key = memory_scope.worker_key(f"named:{session}")
            await memory_scope.prepare(context_builder, key)

        provider, is_new, _resumed = await sessions.get_or_create(
            key,
            agent=opts.get("agent") or default_agent,
            model=opts.get("model") or default_model,
            cwd=step_cwd or cwd,
            extra_env=extra_env,
        )
        # Wall clock for THIS agent turn only (not the whole workflow run):
        # acp leaves TurnUsage.duration_ms at 0, so without this the row's
        # duration_ms is a literal 0. Started after get_or_create so session
        # setup is not charged to the turn.
        _turn_t0 = time.monotonic()
        try:
            from kiro_crew.messaging.identity import publish_turn_identity

            await publish_turn_identity(sessions, key)
            if memory_scope is not None:
                prompt = await memory_scope.prompt(
                    context_builder,
                    key,
                    prompt,
                    is_new=is_new,
                    provider=provider,
                    resumed=_resumed,
                    agent=opts.get("agent") or default_agent,
                    cwd=step_cwd or cwd,
                )
            text = await stream_and_collect(
                provider,
                prompt,
                approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
                max_turns=_MAX_TURNS_PER_STEP,
            )
            # ── Per-turn usage row: attribute workflow spend. ──
            # Best-effort analytics that must never break the workflow run — but
            # the guards are deliberately NARROW. A single wide try around the
            # import + context read + persist would swallow ANY of them at
            # debug, so one import failure or a context-read failure would
            # silently drop the ENTIRE row (the workflow surface writing zero
            # rows).
            #
            # The import stays function-local on purpose: kiro_crew.dashboard.
            # handlers.usage pulls in the slack handler chain, so a module-scope
            # import can raise ImportError under some import orders. A genuine
            # failure here is a real wiring bug, so surface it (warning) instead
            # of hiding it — while still not aborting the run.
            try:
                from kiro_crew.dashboard.handlers.usage import (
                    persist_token_record_async,
                    read_context_tokens,
                    read_effective_agent,
                )
            except ImportError:
                logger.warning(
                    "workflow usage row skipped: usage handlers unimportable",
                    exc_info=True,
                )
            else:
                # Context occupancy is enrichment only. Guard it on its OWN so a
                # read failure degrades to (0, 0) instead of taking the row down.
                try:
                    _used, _window = read_context_tokens(provider)
                except Exception:
                    logger.debug("workflow context-token read failed", exc_info=True)
                    _used, _window = 0, 0
                # Only the persist stays in a best-effort try: a write failure
                # must not break the workflow run, but nothing else hides here.
                try:
                    await persist_token_record_async(
                        key,
                        opts.get("model") or default_model or "",
                        provider_last_turn_usage(provider),
                        provider="acp",
                        surface="workflow",
                        agent=(
                            read_effective_agent(provider)
                            or opts.get("agent")
                            or default_agent
                            or ""
                        ),
                        context_used=_used,
                        context_window=_window,
                        elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                        model_source=provider,
                    )
                except Exception:
                    logger.debug("workflow usage row persist failed", exc_info=True)
            # Apply canonical output redaction to prevent credential or
            # exfiltration-URL leakage into workflow results stored in history
            # or injected into parent chat.
            text = redact(text)
            if memory_scope is not None:
                await memory_scope.validate()
            return text
        finally:
            # Every successful acquire owns a lease, including stateful calls.
            # Returning it without cleanup keeps the named provider and history.
            try:
                sessions.release(key, cleanup=ephemeral)
                if ephemeral and memory_scope is not None:
                    await sessions.destroy(key)
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                logger.warning("workflow session lease release failed", exc_info=True)

    return agent_fn
