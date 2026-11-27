"""Workflow steps on their launching KAS chat's own process.

A dedicated ``ctx.agent()`` step starts a process of its own (``wf:`` / ``wf-pool:``
keys through ``SessionManager.get_or_create``): an engine start and a full MCP fleet
even for an empty step. A KAS chat's process can host sessions besides its own: that
is how its ``spawn_run`` sub-agents run (``acp.kas_subagent_sharing``,
``subagent_manager/run_start.py``), and the engine runs one copy of an MCP server
for every session of the process that uses the same server settings in the same
folder. So a step of a run launched from such a chat opens a session there
(``SessionManager.open_task_session(runtime=...)``) and starts only its own Kiro
Crew servers, under the same switch, ``agent.session_sharing`` (default on).

A step keeps a dedicated process when any of these holds:

- ``agent.session_sharing`` is off;
- the run has no launching chat, or that chat's provider is not live;
- the chat's process does not qualify for sub-agents
  (``is_session_sharing_eligible``: on KAS, ``runtime_hosts_subagents``: close
  advertised, engine at or above the chat-sharing floor, no machine mode, no
  looser sandbox tier, no chat lease shared with other chats);
- the chat is not on KAS (kiro-cli and codex steps are unchanged), or a dedicated
  start of the step would now get another backend (``agent.acp_backend`` changed
  since the chat started);
- the step names an ``effort``, a ``backend`` or a ``session=``, its agent has
  a configured session effort, or it resolves to a crew: only a dedicated start
  applies those;
- the run carries an environment pin (``extra_env``), a crew member's context, or
  a memory mode other than persistent;
- the step's ``cwd`` is not the chat's own folder, or the chat's process carries
  an AWS profile (``aws.project_profiles``) a dedicated step would not get;
- ``KAS_SHARED_NODES_PER_PARENT`` steps already sit on that process.

A step on the chat's process runs in the chat's folder when it names no ``cwd``.
A model the step (or the run) names is put on its own session, which KAS
applies per session (``ACP_BACKENDS_MODEL_PER_SESSION``). Any failure to open
the shared session falls back to the dedicated start.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Most workflow steps placed on one chat's KAS process at once; the next one
#: takes a process of its own. The same bound ``spawn_run`` children have
#: (``subagent_manager.run_start.KAS_SHARED_CHILDREN_PER_PARENT``), counted
#: separately: a run's own concurrency is 4 per run.
KAS_SHARED_NODES_PER_PARENT = 8

#: Step session key -> launching chat key, for every step placed on (or being
#: placed on) a chat's process. Reserved before the first await, so racing
#: steps cannot both take the last place.
_placed: dict[str, str] = {}

#: The model value that names no model (``acp.runtime_models.DEFAULT_MODEL``).
_AUTO_MODEL = "auto"

#: The agent a dedicated start with no agent runs: the ACP client's own default
#: (``acp.client_start.CLIENT_NAME``), which ``get_or_create`` reaches for a
#: stateless ``wf:`` / ``wf-pool:`` key because those bypass the warm pool.
_DEDICATED_DEFAULT_AGENT = "kirocrew"


def placed_count(parent_session_key: str) -> int:
    """How many steps sit on *parent_session_key*'s process now."""
    return sum(1 for parent in _placed.values() if parent == parent_session_key)


def is_placed(key: str) -> bool:
    """Whether step session *key* sits on its chat's process."""
    return key in _placed


def forget(key: str) -> None:
    """Free *key*'s place on its chat's process (no-op for a dedicated step).

    A key whose cancelled open is still in flight keeps its place: the teardown
    that ends the session that open creates frees it (``_end_when_opened``).
    """
    if key not in _closing:
        _placed.pop(key, None)


def _host_runtime(sessions: Any, parent_session_key: str) -> Any:
    """The KAS runtime the chat *parent_session_key* runs on, or None."""
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    get_provider = getattr(sessions, "get_provider", None)
    eligible = getattr(sessions, "is_session_sharing_eligible", None)
    if get_provider is None or eligible is None:
        return None
    if eligible(parent_session_key) is not True:
        return None
    provider = get_provider(parent_session_key)
    inner = getattr(provider, "_client", None)
    runtime = getattr(inner, "_runtime", None)
    return runtime if getattr(runtime, "acp_backend", None) == ACP_BACKEND_KAS else None


def _dedicated_start_is_kas(cfg: Any, key: str) -> bool:
    """Whether ``create_provider_factory`` would select KAS for a step *key* now.

    The step names no backend (a ``backend=`` step never reaches this), so this is
    the factory's own gate with no override: the crewmate pin, the member route,
    then ``agent.acp_backend``.
    """
    from kiro_crew.acp_backends import ACP_BACKEND_KAS
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import select_provider_backend

    backend = select_provider_backend(
        key,
        cfg.agent.member_acp_backend,
        cfg.agent.acp_backend,
        crew_backend=crewmate_backend_pin(cfg, key),
    )
    return backend == ACP_BACKEND_KAS


def shared_host(
    sessions: Any,
    parent_session_key: str,
    *,
    agent: Optional[str],
    cwd: Optional[str],
    extra_env: Optional[dict[str, str]] = None,
    effort_kwargs: Optional[dict] = None,
    backend_kwargs: Optional[dict] = None,
    named: bool = False,
    memory_scope: Any = None,
    cfg: Any = None,
    step_key: str = "",
) -> Any:
    """The chat process a step may open its session on, or None for a dedicated start.

    *cfg* is a config the caller loaded off the event loop. Synchronous and
    side-effect free; never raises (any doubt, a missing *cfg* included, answers
    None). See the module docstring for the terms.
    """
    try:
        if not parent_session_key or named or effort_kwargs or backend_kwargs or extra_env:
            return None
        if memory_scope is not None:
            if getattr(memory_scope, "memory_mode", "persistent") != "persistent":
                return None
            execution = getattr(memory_scope, "execution_context", None)
            if getattr(execution, "member_id", None) is not None:
                return None
        if cfg is None or not cfg.agent.session_sharing:
            return None
        runtime = _host_runtime(sessions, parent_session_key)
        if runtime is None:
            return None
        # The backend a dedicated start of this step would get now, through the
        # provider factory's own gate; a step whose backend is not the host's KAS
        # (agent.acp_backend changed since the chat started, say) keeps that start.
        if not _dedicated_start_is_kas(cfg, step_key):
            return None
        # A step that resolves to a crew carries that crew's own identity (model,
        # effort, watchdog caps, member documents), which only its dedicated
        # start applies in full; it keeps that start.
        from kiro_crew.config.loader import resolve_crew_identity

        # Both the step's own agent and the default the shared opener names for an
        # unnamed step (``_DEDICATED_DEFAULT_AGENT``) must resolve to no crew.
        if resolve_crew_identity(cfg, agent or None, None) or resolve_crew_identity(
            cfg, agent or _DEDICATED_DEFAULT_AGENT, None
        ):
            return None
        # Resolved on the agent the dedicated start would resolve it on.
        if cfg.resolve_session_effort(agent or None) != "":
            return None
        if cwd:
            work_dir = getattr(runtime, "_work_dir", None)
            if work_dir is None or os.path.normpath(str(cwd)) != os.path.normpath(str(work_dir)):
                return None
        differs = getattr(sessions, "aws_profile_differs_from_parent", None)
        if differs is None or differs(parent_session_key, None) is not False:
            return None
        if placed_count(parent_session_key) >= KAS_SHARED_NODES_PER_PARENT:
            logger.info(
                "workflow shared nodes: %d steps already on %s's process; "
                "this one takes its own",
                KAS_SHARED_NODES_PER_PARENT,
                parent_session_key,
            )
            return None
        return runtime
    except Exception:  # noqa: BLE001 - any doubt keeps the dedicated process
        logger.debug("workflow shared nodes: host check failed", exc_info=True)
        return None


#: Teardowns of shared opens their step was cancelled during; held so they finish.
_teardowns: set[asyncio.Task] = set()

#: Keys whose cancelled open is still in flight; their place stays taken.
_closing: set[str] = set()


def _end_when_opened(sessions: Any, key: str, opening: "asyncio.Future[Any]") -> None:
    """End the session *opening* creates for a cancelled step, then free its place."""

    async def _end() -> None:
        try:
            await opening
        except BaseException:  # noqa: BLE001 - no session was created
            return
        try:
            sessions.release(key, cleanup=True)
            await sessions.destroy(key)
        except Exception:
            logger.debug("workflow step %s: teardown after a cancelled open failed", key)

    async def _end_then_free() -> None:
        try:
            await _end()
        finally:
            _closing.discard(key)
            _placed.pop(key, None)

    _closing.add(key)
    task = asyncio.ensure_future(_end_then_free())
    _teardowns.add(task)
    task.add_done_callback(_teardowns.discard)


async def _load_config() -> Any:
    """The config, read off the event loop; None when it cannot be read."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:  # noqa: BLE001 - an unreadable config keeps the dedicated start
        logger.debug("workflow shared nodes: config unreadable", exc_info=True)
        return None


async def _apply_model(
    runtime: Any, provider: Any, agent: Optional[str], model: Optional[str], key: str
):
    """Put the step's session on the model a dedicated start would pick. Never raises.

    KAS's agent projection carries no model, so Crew's own choice is applied to the
    session: the step's or run's model, else the session-model ladder a dedicated
    ``get_or_create`` start resolves (``session._session_model``: crew
    pin, template, app, global, agent file). Either is spelled for the backend by
    ``acp_effective_model``. An unpinned result keeps what ``open_task_session``
    gave the session (kiro-cli's ``chat.defaultModel``).
    """
    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.agent_sdk.drivers.acp import apply_configured_model
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import _session_model

    backend = runtime.acp_backend

    def _resolve() -> str:
        cfg = KiroCrewConfig.load()
        chosen = model or _session_model(cfg, agent, session_key=key) or None
        if not chosen or chosen == _AUTO_MODEL:
            return ""
        return cfg.acp_effective_model(
            agent or None, chosen, namespace=capabilities_for(backend).model_id_namespace
        )

    pin = ""
    try:
        pin = await asyncio.to_thread(_resolve)
        if pin and pin != _AUTO_MODEL:
            await apply_configured_model(provider._handle, pin, backend=backend)
    except Exception:
        logger.warning(
            "workflow step: could not apply model %s to its shared session",
            pin or model,
            exc_info=True,
        )


async def acquire_step_session(
    sessions: Any,
    key: str,
    *,
    parent_session_key: str,
    agent: Optional[str],
    model: Optional[str],
    cwd: Optional[str],
    extra_env: Optional[dict[str, str]] = None,
    effort_kwargs: Optional[dict] = None,
    backend_kwargs: Optional[dict] = None,
    named: bool = False,
    memory_scope: Any = None,
) -> tuple[Any, bool, bool]:
    """A step's session: on its chat's KAS process when it may, else a dedicated one.

    Returns ``get_or_create``'s ``(provider, is_new, resumed)``, holding the
    session's turn lease either way. A shared step's place is freed by
    :func:`forget` when the caller tears the session down.
    """
    # A re-acquire of the same key (a pool worker's hard reset) decides afresh.
    forget(key)
    effort_kwargs = effort_kwargs or {}
    backend_kwargs = backend_kwargs or {}
    cfg = await _load_config()
    runtime = shared_host(
        sessions,
        parent_session_key,
        agent=agent,
        cwd=cwd,
        extra_env=extra_env,
        effort_kwargs=effort_kwargs,
        backend_kwargs=backend_kwargs,
        named=named,
        memory_scope=memory_scope,
        cfg=cfg,
        step_key=key,
    )
    # No await between the cap check above and this reservation.
    if runtime is not None:
        # The agent a dedicated start with no agent runs, never the chat's own:
        # the session opener would fall back to the runtime's.
        step_agent = agent or _DEDICATED_DEFAULT_AGENT
        _placed[key] = parent_session_key
        opening = asyncio.ensure_future(
            sessions.open_task_session(
                parent_session_key, key, agent=step_agent, cwd=cwd, runtime=runtime
            )
        )
        try:
            provider, is_new, resumed = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The open keeps running to its answer, so the session it creates is
            # owned and ended; its place is held until then.
            _end_when_opened(sessions, key, opening)
            raise
        except Exception:
            forget(key)
            logger.warning(
                "workflow step %s: shared session on %s's process failed; "
                "starting a process of its own",
                key,
                parent_session_key,
                exc_info=True,
            )
        else:
            if getattr(provider, "_runtime", None) is runtime:
                try:
                    if is_new:
                        # The model ladder runs on the step's own agent (None when
                        # unnamed), as the dedicated start's does.
                        await _apply_model(runtime, provider, agent, model, key)
                except BaseException:
                    # Cancelled while the session is held and before the caller's
                    # teardown owns it: end it here so neither it nor its place leaks.
                    forget(key)
                    try:
                        sessions.release(key, cleanup=True)
                        await sessions.destroy(key)
                    except Exception:
                        logger.debug("workflow step %s: teardown after a cancel failed", key)
                    raise
                logger.info(
                    "workflow step %s on %s's KAS process (pid %s)",
                    key,
                    parent_session_key,
                    getattr(runtime, "pid", None),
                )
                return provider, is_new, resumed
            # ``open_task_session`` took its own dedicated path (a restricted
            # memory mode, a project AWS profile, a capability revision, a
            # workspace binding): that start carried no model or env, so it is
            # replaced by the dedicated start this step would have had.
            forget(key)
            try:
                sessions.release(key, cleanup=True)
                await sessions.destroy(key)
            except Exception:
                logger.debug("workflow step %s: replacing a fallback start failed", key)
    return await sessions.get_or_create(
        key,
        agent=agent,
        model=model,
        cwd=cwd,
        extra_env=extra_env,
        **effort_kwargs,
        **backend_kwargs,
    )
