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
gateway wires them at startup), so importing this module has no side effects.
It is an optional adapter the gateway supplies as ``agent_fn``, not an engine layer.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any, Callable, Optional

from kiro_crew.acp_backends import (
    backend_from_stored,
    selectable_backends,
    selectable_stored_backend_values,
)
from kiro_crew.agent_sdk.drivers.acp_vocab import STOP_REASON_CANCEL_UNACKED, STOP_REASON_CANCELLED
from kiro_crew.agent_sdk.mcp_problems import UNATTENDED_MCP_WAIT_SECS, wait_for_declared_mcp
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort
from kiro_crew.llm_helpers import ToolApprovalPolicy, provider_last_turn_usage, stream_and_collect
from kiro_crew.security import redact
from kiro_crew.workflows import WorkflowStepInterrupted

logger = logging.getLogger(__name__)

# Signature the runner expects: async (prompt, opts) -> result.
AgentFn = Callable[[str, dict], Any]

# Per-step tool-call ceiling. Generous enough for any realistic agent step,
# but prevents infinite tool loops from prompt injection.
_MAX_TURNS_PER_STEP = 200

#: Stop reasons of a turn something outside the step ended: a session cancel
#: (the gateway stopping, a session reset) or a cancel the backend never acked.
#: A workflow never cancels its own step's turn -- a run cancel or a per-task
#: timeout cancels the awaiting task instead -- so text collected before one of
#: these is a cut-off answer, not the step's result.
_INTERRUPTED_STOP_REASONS = frozenset({STOP_REASON_CANCELLED, STOP_REASON_CANCEL_UNACKED})


class StepStopWatch:
    """Records a step turn's stop reason; pass :meth:`on_complete` to ``stream_and_collect``.

    Shared by both executors. :meth:`raise_if_interrupted` raises
    :class:`WorkflowStepInterrupted` for an ``_INTERRUPTED_STOP_REASONS`` stop, so
    the step fails instead of recording the cut-off text as its result.
    """

    def __init__(self) -> None:
        self.stop_reason = ""

    def on_complete(self, event: Any) -> None:
        self.stop_reason = str(getattr(event, "stop_reason", "") or "")

    def raise_if_interrupted(self) -> None:
        if self.stop_reason in _INTERRUPTED_STOP_REASONS:
            raise WorkflowStepInterrupted(
                f"agent turn was stopped before it finished ({self.stop_reason})"
            )


def step_text(refusals: list[str], text: str, *, has_schema: bool) -> str:
    """A step's text, led by its directive refusals unless the step has a ``schema=``.

    A ``schema=`` step's text is parsed as JSON, and a refusal line ahead of a
    scalar reply would fail that parse and spend the step's re-asks. There the
    refusal stays in its SEL ``denied`` row and the step's log line only.
    """
    if not refusals:
        return text
    if has_schema:
        logger.info("workflow step: refused %d person-only directive(s)", len(refusals))
        return text
    return "\n\n".join([*refusals, text])


class WorkflowSpawnRefused(Exception):
    """A ``ctx.agent()`` step the spawn policy, the cwd allowlist or its backend refuses."""


class WorkflowEffortInvalid(ValueError):
    """A ``ctx.agent(effort=...)`` value that is not a reasoning-effort level."""


def step_effort_kwargs(opts: dict) -> dict[str, str]:
    """The ``get_or_create`` kwargs that carry a step's ``effort=``, or ``{}``.

    An unset effort adds nothing, so the session resolves its effort exactly as
    a step that never named one. A set one must be a level from the shared
    vocabulary: anything else raises :class:`WorkflowEffortInvalid` and fails the
    step before a session is allocated, instead of running it at a default the
    script did not ask for.
    """
    effort = opts.get("effort")
    if effort is None or effort == "":
        return {}
    if not is_valid_effort(effort):
        raise WorkflowEffortInvalid(
            f"invalid effort {str(effort)[:64]!r}: expected one of {', '.join(EFFORT_LEVELS)}"
        )
    return {"reasoning_effort_override": effort}


def step_backend_kwargs(opts: dict) -> dict[str, str]:
    """The ``get_or_create`` kwargs that carry a step's ``backend=``, or ``{}``.

    Named as ``spawn_run(backend=)`` and ``config.json`` name a backend (``"kiro"``
    is Kiro CLI). Unset (``None`` or ``""``) adds nothing. A name this build cannot
    select (unknown, or denied by policy) raises :class:`WorkflowSpawnRefused`
    before a session is allocated: the provider factory would quietly move it to
    the floor backend instead.
    """
    name = opts.get("backend")
    backend = backend_from_stored(name)
    if backend is None:
        return {}
    if not isinstance(backend, str) or backend not in selectable_backends():
        raise WorkflowSpawnRefused(
            f"backend {name!r} is not available; choose one of "
            f"{selectable_stored_backend_values()}"
        )
    return {"backend_override": backend}


def step_session_name(opts: dict) -> Optional[str]:
    """The step's ``session=`` name, scoped to its ``backend=`` when it names one.

    A live session cannot change harness, so the same name on two backends must be
    two sessions. A step that names no backend keeps its name, and so its key.
    """
    session, backend = opts.get("session"), opts.get("backend")
    return f"{session}@{backend}" if session is not None and backend else session


async def apply_step_effort(provider: Any, level: Optional[str]) -> None:
    """Push ``level`` onto a live session through ``provider.change_effort``.

    ``get_or_create`` applies ``reasoning_effort_override`` only when it starts a
    session, so a session that is already live needs the step's effort pushed.
    Nothing is pushed for an unset level or a provider without ``change_effort``.
    A level the session does not advertise (``accepts_effort_level`` returns
    False) is skipped, as a cold start drops it. A raise from ``change_effort``
    propagates to the caller.
    """
    change_effort = getattr(provider, "change_effort", None)
    if not level or change_effort is None:
        return
    accepts = getattr(provider, "accepts_effort_level", None)
    if accepts is not None and not accepts(level):
        return
    await change_effort(level)


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
    # Function-local despite the advisory ``top-level-imports`` rule: importing
    # ``kiro_crew.subagent`` adds 133 modules and ~104ms to this module's import,
    # and ``kiro_crew.subagent`` imports ``kiro_crew.slack.format`` while
    # ``slack/gateway.py`` imports ``workflows/service.py`` (which imports this
    # module); that chain is acyclic only while ``slack/__init__`` stays lazy.
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


async def persist_step_usage_row(
    *,
    slot_key: str,
    model: str,
    provider: Any,
    agent: str,
    elapsed_ms: int,
) -> None:
    """Write ONE per-turn workflow usage row through the existing usage writer.

    Shared by both executors (``build_agent_fn`` and ``agent_pool``'s pooled and
    overflow legs). ``slot_key`` is the step session's real key (``wf:``,
    ``wf-pool:`` or ``wf-unpooled:``). ``provider`` doubles as ``model_source`` so
    the writer can name the model that ran when the caller resolved none.

    Best-effort: it never breaks the run, but each guard is narrow so one failing
    read cannot silently drop the whole row. The import is function-local because
    kiro_crew.dashboard.handlers.usage pulls in the slack handler chain and can
    raise ImportError under some import orders; that is a wiring bug, so it warns.
    """
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
        return
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
            slot_key,
            model,
            provider_last_turn_usage(provider),
            provider="acp",
            surface="workflow",
            agent=read_effective_agent(provider) or agent,
            context_used=_used,
            context_window=_window,
            elapsed_ms=elapsed_ms,
            model_source=provider,
            trigger="workflow",
        )
    except Exception:
        logger.debug("workflow usage row persist failed", exc_info=True)


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
        # Effort and spawn gates FIRST: a refused step must allocate no session,
        # touch no memory scope and consume no session index.
        effort_kwargs = step_effort_kwargs(opts)
        backend_kwargs = step_backend_kwargs(opts)
        step_cwd = await vet_step_spawn(
            session_key=session_key,
            agent=opts.get("agent") or default_agent,
            cwd=opts.get("cwd"),
            app=app,
        )
        # Per-call isolated session by default; caller-named session when session=.
        session = step_session_name(opts)
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
            **effort_kwargs,
            **backend_kwargs,
        )
        try:
            if is_new:
                # Nobody can reconnect a slow MCP server for a step, so its first
                # prompt waits (bounded) for the agent spec's servers to report.
                await wait_for_declared_mcp(provider, UNATTENDED_MCP_WAIT_SECS)
            elif session is not None:
                # A live named session keeps the effort it started with; push
                # this step's effort onto it before the turn is sent.
                await apply_step_effort(provider, effort_kwargs.get("reasoning_effort_override"))
            # Wall clock for THIS agent turn only (not the whole workflow run):
            # acp leaves TurnUsage.duration_ms at 0, so without this the row's
            # duration_ms is a literal 0. Started after the session is ready and
            # the effort push so session setup is not charged to the turn.
            _turn_t0 = time.monotonic()
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
            # A step is never a person's turn and no session applier reads its
            # stream, so a person-only directive it calls is refused out loud.
            from kiro_crew.dashboard.session_directive_apply import (
                person_only_directive_refuser,
            )

            stop = StepStopWatch()
            refusals: list[str] = []
            text = await stream_and_collect(
                provider,
                prompt,
                approval_policy=ToolApprovalPolicy.AUTO_APPROVE,
                max_turns=_MAX_TURNS_PER_STEP,
                on_complete=stop.on_complete,
                on_event=person_only_directive_refuser(key, refusals),
            )
            text = step_text(refusals, text, has_schema=opts.get("schema") is not None)
            # ── Per-turn usage row: attribute workflow spend. ──
            # One shared writer for both executors (see persist_step_usage_row).
            # Written before the stop check: a turn cut off from outside still
            # spent what it spent.
            await persist_step_usage_row(
                slot_key=key,
                model=opts.get("model") or default_model or "",
                provider=provider,
                agent=opts.get("agent") or default_agent or "",
                elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
            )
            stop.raise_if_interrupted()
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
