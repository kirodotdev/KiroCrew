"""Which agent an app-owned dashboard slot runs as: whether the app's binding applies, whether
the turn's backend may run the app's own agent, retiring and recovering the app agent's
session, the except arm for an app agent that is not loaded
(``_on_app_agent_not_loaded``), and the refusal of an app's session that would run as a
Bot (``app_session_selects_bot``, ``_refuse_app_bot_turn``)."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.capabilities import SessionCapabilities
from kiro_crew.dashboard.chat_utils import APP_AGENT_REFUSED_KIND
from kiro_crew.dashboard.slot_ownership import (
    BOT_IDENTITY_DENIED,
    _record_isolation_denial,
    selects_bot_identity,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        KiroCrewConfig,
        ResolvedBindings,
        Routing,
        _ChatSlot,
        _MemoryUnavailable,
        effective_session_key,
        logger,
        resolve_agent_bindings,
        resolve_session_agent_bindings,
        routing_for,
        select_provider_backend,
        subprocess_executor,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

#: The row a turn ends on when an app's session would run as a Bot.
APP_BOT_TURN_REFUSAL = (
    "This session belongs to an app, and an app's session cannot run as a Bot. "
    "Start a new session on one of the app's own agents; a template that shares a "
    'Bot\'s name is picked with agent_kind "template".'
)


def app_session_selects_bot(slot: "_ChatSlot", bindings: "ResolvedBindings | None") -> bool:
    """Whether a turn on app-owned *slot* would run as a Bot.

    The owner approves the paths an app may call (``permissions.api``), never a
    Bot: owning a session gives an app no claim on a Bot's memory store or on
    its chat, with the Permissions that chat takes. So an app's session never
    runs on what is a Bot's own (``slot_ownership.selects_bot_identity``),
    whether the Bot's name is only stamped on the slot (no execution record yet)
    or an execution record already names the Bot or runs on its store. The same
    rule ``apps.cron_sdk`` and ``apps.spawn_sdk`` hold: an app does not reach a
    Bot by naming it. A session that is not an app's is never judged here.
    """
    return bool(getattr(slot, "_app", "")) and selects_bot_identity(bindings)


def _refuse_app_bot_turn(slot: "_ChatSlot", bindings: "ResolvedBindings | None") -> bool:
    """End the turn on app-owned *slot* when it would run as a Bot; True when refused.

    The caller returns before it attaches a memory store or writes an execution
    record, so a refused turn binds nothing of the Bot's to the app's session.
    """
    if not app_session_selects_bot(slot, bindings):
        return False
    # Says why, since the person may have made the pick themselves: the owner's
    # own switch on an app's tab is accepted by the route and ends here.
    logger.warning(
        "refusing the turn on slot %s: the session belongs to app %r and its agent %r "
        "selects a Bot's own memory store or chat (%s); an app's session never runs as "
        "a Bot, whoever picked the agent",
        slot.key,
        slot._app,
        slot.agent,
        (
            "the session's execution record names it"
            if getattr(bindings, "execution_context", None) is not None
            else "the name is stamped on the slot, with no execution record yet"
        ),
    )
    _record_isolation_denial(
        slot._app, "chat_runner.turn_admission", f"slot={slot.key}", BOT_IDENTITY_DENIED
    )
    # shortcut: English only; give it a gateway-notice token when an app can be
    # granted a Bot and this row starts naming the missing grant.
    slot.append("error", APP_BOT_TURN_REFUSAL, "msg msg-err", meta={"kind": APP_AGENT_REFUSED_KIND})
    return True


def app_agent_binding_applies(slot: "_ChatSlot") -> bool:
    """Whether *slot*'s app-agent binding still names the agent it runs as.

    The binding is bound to the agent name (``<app>/<agent>``): a session
    switched to another agent keeps the field, and both the per-turn check in
    ``_run_chat`` and the eager-spawn opt-out apply only while the session runs
    as the bound agent. One predicate for both, so they cannot drift.
    """
    owner_app, _, owner_agent = (slot.app_agent_owner or "").partition("/")
    return bool(owner_app and owner_agent and slot.agent == owner_agent)


async def _retire_app_agent_session(
    state: "DashboardState", slot: "_ChatSlot", session_key: str
) -> None:
    """Cancel *slot*'s pending eager start and remove any session it registered.

    Called when an app-owned turn is refused: whatever is registered under
    *session_key* was resolved without the ownership check and must not serve a
    later turn.
    """
    eager = getattr(slot, "_eager_spawn_task", None)
    if eager is not None and not eager.done():
        eager.cancel()
        await asyncio.gather(eager, return_exceptions=True)
    sessions = getattr(state, "sessions", None)
    if sessions is not None and sessions.has_session(session_key):
        await sessions.remove(session_key)


async def _recover_app_agent_binding(
    cfg: "KiroCrewConfig", slot: "_ChatSlot", *, project: str | None
) -> ResolvedBindings:
    """Re-register an app-owned slot's resources from source, then re-resolve.

    The last recovery rung for an app slot whose agent stayed unresolved after
    the snapshot rescan: the spec was never materialized even though the source
    is intact (a plain gateway restart re-materializes every ENABLED app via
    ``reconcile_enabled_app_resources``, so this is what avoids that restart and
    heals mid-turn). Uses ``register_app`` — not the narrower
    ``refresh_app_agents`` — so the app's MCP servers are registered BEFORE its
    agents; re-materializing only the agent would inline an empty server map and
    recreate an agent whose own tool refs dangle (dispatches, tools never mount).
    Gated on ``is_app_enabled`` held under ``app_lifecycle_lock`` so a concurrent
    disable/uninstall cannot race recovery into reactivating a deregistered
    agent; a disabled app is left to fail loud. A recovery failure only logs —
    the re-resolve below then simply returns the still-cold bindings. Returns the
    freshly resolved bindings; the caller reassigns its own locals from them.
    """
    # Local imports mirror server.py's reconcile import to avoid a top-level
    # apps<->dashboard cycle.
    from kiro_crew.apps.bridges import register_app
    from kiro_crew.apps.manager import app_lifecycle_lock, is_app_enabled

    try:
        loop = asyncio.get_running_loop()
        async with app_lifecycle_lock(slot._app):
            if await loop.run_in_executor(subprocess_executor(), is_app_enabled, slot._app):
                # register_app runs in an executor thread that cannot be
                # cancelled. Shield the await so cancelling THIS coroutine (e.g.
                # an eager-spawn task being cancelled) does NOT release the
                # lifecycle lock while that thread is still writing — a concurrent
                # disable could otherwise acquire the lock, deregister the app,
                # and have the still-running thread republish a now-disabled
                # agent. On cancel, wait for the thread to finish before letting
                # the lock release, then propagate the cancellation.
                fut = loop.run_in_executor(subprocess_executor(), register_app, slot._app)
                try:
                    await asyncio.shield(fut)
                except asyncio.CancelledError:
                    await fut
                    raise
    except Exception:  # noqa: BLE001 — a recovery failure only costs the fail-loud
        logger.warning(
            "Failed to re-register app resources from source for app slot %s",
            slot.key,
            exc_info=True,
        )
    selected_agent = slot.agent
    bindings = await asyncio.to_thread(
        functools.partial(resolve_session_agent_bindings, agent_kind=slot.agent_kind),
        resolve_agent_bindings,
        cfg,
        effective_session_key(slot),
        selected_agent or None,
        project,
    )
    if slot.agent != selected_agent:
        raise _MemoryUnavailable(
            "memory_unavailable: agent changed during recovery; retry the turn"
        )
    return bindings


def _provider_backend(provider: Any) -> str | None:
    """The backend id *provider*'s own capabilities name, or None when it names none.

    A provider that carries no real :class:`SessionCapabilities` -- a wrapper, a
    test double, a future adapter -- names no backend, and every caller treats
    that as refused rather than as the default id.
    """
    caps = getattr(provider, "capabilities", None)
    if not isinstance(caps, SessionCapabilities):
        return None
    return caps.backend


def _app_agent_host_allowed(provider: Any) -> bool:
    """Whether *provider*'s backend launches its agent from the agent spec.

    An app's own agent runs only where the spec is what the harness mounts
    (``Routing.AGENT_SPEC``); any other backend would run the app-authored
    prompt without the app's spec. A provider naming no backend is refused.
    """
    backend = _provider_backend(provider)
    return backend is not None and routing_for(backend) is Routing.AGENT_SPEC


def _launched_copy_names(provider: Any) -> set[str]:
    """Every launch-copy name the layers of *provider* say kiro-cli was asked for.

    The allocation stamps it on the provider (``app_launch_agent``) and the Kiro
    runtime keeps it (``_launch_agent``); the agent's own name stays on ``_agent``.
    """
    from kiro_crew.wrapper_chain import wrapper_chain

    names: set[str] = set()
    for node in wrapper_chain(provider):
        for attr in ("app_launch_agent", "_launch_agent"):
            name = getattr(node, attr, None)
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _runs_app_agent_launch(provider: Any, launch_name: str) -> bool:
    """Whether kiro-cli behind *provider* was asked for *launch_name* and nothing else.

    Fail closed: a provider that names no launch copy cannot show it runs the
    copy, so it does not.
    """
    return bool(launch_name) and _launched_copy_names(provider) == {launch_name}


async def _app_agent_turn_backend(
    state: "DashboardState", session_key: str, cfg: "KiroCrewConfig | None"
) -> str | None:
    """The backend the turn about to be claimed for *session_key* would run on.

    A live provider registered under the key is the one the claim reuses, so its
    own identity answers (None when it names none). Otherwise the claim allocates
    through the same member-aware selector this reads. The claimed provider is
    checked again after the claim; this read only spares a refused turn its cold
    start and decides whether the turn launches the app's copy.
    """
    sessions = state.sessions
    provider = sessions.get_provider(session_key)
    if provider is not None and await sessions.is_provider_alive(session_key) is True:
        return _provider_backend(provider)
    if cfg is None:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    from kiro_crew.crewmate_harness import crewmate_backend_pin
    from kiro_crew.members import session_backend_pick

    pick = session_backend_pick(session_key)
    return select_provider_backend(
        session_key,
        cfg.agent.member_acp_backend,
        cfg.agent.acp_backend,
        backend_override=pick,
        crew_backend=crewmate_backend_pin(cfg, session_key) if pick is None else None,
    )


async def _on_app_agent_not_loaded(turn: _ChatTurn, exc) -> None:
    """The slot's app agent is not loaded."""
    slot = turn.slot
    # An app-owned slot whose agent never materialized, even after the
    # self-heal warm. Deliberately terminal: running the default agent here is
    # the exact silent substitution this guards against. Surface the naming
    # card through the same ``slot.append("error", ...)`` path as every other
    # terminal turn error, but do NOT record a session failure — nothing
    # failed to run, the agent simply is not loaded yet, and the user's next
    # send (once the warm lands) should start clean.
    logger.warning("App agent not loaded for slot %s: %s", slot.key, exc)
    # Stays English: the row's whole text is the exception's own message, so a
    # token would carry it verbatim and localize nothing of ours.
    slot.append("error", str(exc), "msg msg-err")
    _crew_log_error = type(exc).__name__
    turn._crew_log_error = _crew_log_error
