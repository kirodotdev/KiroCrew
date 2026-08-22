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

import itertools
import logging
import time
from typing import Any, Callable, Optional

from kiro_crew.llm_helpers import ToolApprovalPolicy, provider_last_turn_usage, stream_and_collect
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Signature the runner expects: async (prompt, opts) -> result.
AgentFn = Callable[[str, dict], Any]

# Per-step tool-call ceiling. Generous enough for any realistic agent step,
# but prevents infinite tool loops from prompt injection.
_MAX_TURNS_PER_STEP = 200

# Stable identity used by the HookManager governance profile resolver for the
# host-authorized native workflow surface. Public workflows leave ``app`` empty.
WORKFLOW_APP_ID = "workflows"


async def _reject_unattended_tool(_event: Any) -> bool:
    """Native Crew has no interactive approver, so fail closed on TOOL_ALLOW."""
    return False


def _workflow_tool_gate_kwargs(
    *,
    native_crew: bool,
    key: str,
    source_session_key: str,
    agent: Optional[str],
    app: str,
) -> dict[str, Any]:
    """Return the tool-gate kwargs for one workflow agent turn.

    Dynamic/public workflows retain their historical AUTO_APPROVE behavior;
    native Crew execution is a separate host-authorized capability and must
    cross the HookManager boundary. If the dashboard has not initialized the
    global hook store, REJECT_ALL is deliberate fail-closed behavior — never
    silently degrade a native role to auto-approval.
    """
    if not native_crew:
        return {"approval_policy": ToolApprovalPolicy.AUTO_APPROVE}

    from kiro_crew.hooks import get_global_hook_store

    hooks = get_global_hook_store()
    return {
        "approval_policy": (
            ToolApprovalPolicy.HOOK_BASED
            if hooks is not None
            else ToolApprovalPolicy.REJECT_ALL
        ),
        "hooks": hooks,
        # Governance profiles bind to the originating dashboard session, while
        # the provider key remains an ephemeral per-role implementation detail.
        "session_key": source_session_key or key,
        "agent": agent or "",
        "app": app or WORKFLOW_APP_ID,
        "on_tool_approval": _reject_unattended_tool,
    }


def build_agent_fn(
    sessions: Any,
    *,
    run_id: str,
    default_agent: Optional[str] = None,
    default_model: Optional[str] = None,
    cwd: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    native_crew: bool = False,
    source_session_key: str = "",
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

    ``native_crew`` enables the host-authorized native Crew boundary. Its tool
    turns use the global HookManager when available and reject all tools when
    that store is absent. ``source_session_key`` and ``app`` identify the
    originating workflow surface to governance profiles.
    """

    # Per-run, 0-based ephemeral session index (not a module-global, so each run
    # restarts at :0 as the ``wf:{run_id}:{call_index}`` contract documents).
    counter = itertools.count()

    async def agent_fn(prompt: str, opts: dict) -> Any:
        # Per-call isolated session by default; caller-named session when session=.
        session = opts.get("session")
        ephemeral = session is None
        key = session or f"wf:{run_id}:{next(counter)}"

        provider, *_ = await sessions.get_or_create(
            key,
            agent=opts.get("agent") or default_agent,
            model=opts.get("model") or default_model,
            cwd=opts.get("cwd") or cwd,
            extra_env=extra_env,
        )
        # Wall clock for THIS agent turn only (not the whole workflow run):
        # acp leaves TurnUsage.duration_ms at 0, so without this the row's
        # duration_ms is a literal 0. Started after get_or_create so session
        # setup is not charged to the turn.
        _turn_t0 = time.monotonic()
        try:
            gate_kwargs = _workflow_tool_gate_kwargs(
                native_crew=native_crew,
                key=key,
                source_session_key=source_session_key,
                agent=opts.get("agent") or default_agent,
                app=app,
            )
            text = await stream_and_collect(
                provider,
                prompt,
                **gate_kwargs,
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
                        agent=(read_effective_agent(provider)
                               or opts.get("agent") or default_agent or ""),
                        context_used=_used,
                        context_window=_window,
                        elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                        model_source=provider,
                    )
                except Exception:
                    logger.debug("workflow usage row persist failed", exc_info=True)
            # Apply output redaction to prevent credential leakage
            # in workflow results stored in history or injected into parent chat.
            text, _ = redact_credentials(text)
            text, _ = redact_exfiltration_urls(text)
            return text
        finally:
            # Ephemeral per-call sessions are torn down; named sessions persist so
            # a stateful chain (session=) keeps its history across steps.
            if ephemeral:
                try:
                    sessions.release(key, cleanup=True)
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    pass

    return agent_fn
