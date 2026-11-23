"""A Slack channel's Crew agent binding, resolved for the session it starts.

``slack.channels.<id>.agent`` may name a Kiro Crew agent record (a
``config.agents`` key) rather than a kiro-cli agent mode. kiro-cli only starts
a mode, so a chat turn that hands the record's NAME to ``get_or_create`` asks
for a mode that does not exist and runs in a per-session scratch directory
instead of the record's workspace. The dashboard and the cron path
(``gateway_runtime/cron_fire._resolve_cron_agent``) already collapse the name:
the record's ``kiro_agent`` goes to kiro-cli as ``agent``, the name itself goes
as ``crew_agent`` so ``prepare_runtime`` keeps the member identity it already
resolved by name, and the session runs in the record's workspace directory.
This module is that same collapse for the Slack chat turn.

Only a name that IS a ``config.agents`` key is collapsed. Anything else -- a
plain kiro agent mode such as ``kirocrew-lite`` -- returns ``None`` and the
caller passes it through unchanged, exactly as before. The binding itself is
not new: the channel already names the record and the session already resolved
its member identity from that name; this only stops the mode and the working
directory from disagreeing with it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelCrew:
    """What a channel-bound Crew agent record starts a session with."""

    #: The kiro-cli agent mode the record runs (``agents.<name>.kiro_agent``).
    kiro_agent: str
    #: The record's own name, passed as ``crew_agent`` so member identity holds.
    crew_agent: str
    #: The record's workspace directory, anchored by the one placement rule.
    cwd: str

    def allocation_kwargs(self) -> dict[str, Any]:
        """The ``get_or_create`` keyword arguments this binding supplies."""
        return {"agent": self.kiro_agent, "crew_agent": self.crew_agent, "cwd": self.cwd}


def resolve_channel_crew(name: str | None) -> ChannelCrew | None:
    """Resolve a channel's agent *name* to its Crew record's mode and workspace.

    Blocking (a config read when no live snapshot exists): call it off the loop.
    Returns ``None`` for an empty name, a name that is not a ``config.agents``
    key, or any failure to read the record, so the caller keeps its current
    behaviour rather than starting a session under a guess.
    """
    if not name:
        return None
    try:
        from kiro_crew.config import KiroCrewConfig, live
        from kiro_crew.config.loader import resolve_agent_bindings
        from kiro_crew.config.loader_locations import usable_workspace_dir_from_entry

        # The last APPLIED reload first, so a record created at runtime is seen
        # without a gateway restart (the cron resolver reads the same way).
        cfg = live.snapshot() or KiroCrewConfig.load()
        record = (getattr(cfg, "agents", None) or {}).get(name)
        if record is None:
            return None
        # validate_memory_files=False: only the mode is read here; the session's
        # own execution-context resolution validates its store later.
        bindings = resolve_agent_bindings(cfg, name, validate_memory_files=False)
        kiro_agent = bindings.kiro_agent or ""
        if not kiro_agent:
            return None
        # bindings.workspace_dir is the RAW configured value (the shipped default
        # is the relative "workspace"); as a provider cwd that would resolve
        # against the gateway process directory. workspace_dir_from_entry anchors
        # a relative dir under the data home and an unmapped name to the base
        # workspace directory, never another workspace's.
        ws_entry = (getattr(cfg, "workspaces", None) or {}).get(record.workspace or "")
        # A folder that is missing or sensitive falls back to the base workspace.
        cwd = str(usable_workspace_dir_from_entry(ws_entry, record.workspace or ""))
        return ChannelCrew(kiro_agent=kiro_agent, crew_agent=name, cwd=cwd)
    except Exception:
        logger.debug("slack channel agent %r: crew record not resolvable", name, exc_info=True)
        return None


def context_template(name: str | None, crew: ChannelCrew | None = None) -> str | None:
    """The kiro-cli template the context builder is given for a turn run as *name*.

    ``ContextBuilder.build_message(agent=...)`` takes the EXECUTION TEMPLATE, the
    same value the dashboard passes: it locates the agent spec, its prompt and
    skill mapping, and a private crewmate's essentials read that template's
    documents and refuse the turn when no such template exists. A Crew record's
    name is not a template, so a record is collapsed to its ``kiro_agent``,
    whether it reached the turn as the channel binding (*crew*, already
    resolved) or as a dashboard-linked session's thread agent. Any other name
    passes through unchanged. Blocking when *crew* does not already cover
    *name*: call it off the loop.
    """
    if crew is not None and crew.crew_agent == name:
        return crew.kiro_agent
    record = resolve_channel_crew(name)
    return record.kiro_agent if record is not None else name


def crew_bots() -> dict[str, str]:
    """Every Crew Bot, a ``config.agents`` key, mapped to its display name ("" if none).

    The Bots a person may put a Slack thread on (``!bot``, ``/kirocrew bot``).
    Blocking like :func:`resolve_channel_crew`; an unreadable config lists none.
    """
    try:
        from kiro_crew.config import KiroCrewConfig, live

        cfg = live.snapshot() or KiroCrewConfig.load()
        return {
            key: str(getattr(record, "display_name", "") or "")
            for key, record in (getattr(cfg, "agents", None) or {}).items()
        }
    except Exception:
        logger.debug("slack bot list: config not readable", exc_info=True)
        return {}


def resolve_bot_name(typed: str) -> str | None:
    """The ``config.agents`` key *typed* names, or ``None``.

    An exact key wins; otherwise a key or display name that differs only in case.
    Blocking: call it off the loop.
    """
    bots = crew_bots()
    if typed in bots:
        return typed
    folded = typed.casefold()
    for key, label in bots.items():
        if folded and folded in (key.casefold(), label.casefold()):
            return key
    return None


def bots_text() -> str:
    """The Bots as one Slack line: `` `key` (Display name), ...``. Blocking."""
    bots = crew_bots()
    if not bots:
        return "(none found)"
    return ", ".join(
        f"`{key}`" + (f" ({label})" if label and label != key else "")
        for key, label in bots.items()
    )


async def bind_bot_memory(
    session_key: str, bot: str | None, sessions: Any, conversation_log: Any
) -> None:
    """Bind a thread's memory to Bot *bot* through the dashboard's own selection seam.

    A Bot with a named memory store (private v2, or a legacy v1 name) gets that store
    published as the thread's execution record, so ``session_store_for_turn`` reads the
    Bot's memory and not Global. The seam is the dashboard's (``pin_private_agent_store``
    then ``_record_explicit_agent_selection``, as ``slack/code_channels/binding.py`` runs
    them), so its fences hold: a thread that already has history, or belongs to another
    Bot, refuses a private Bot. ``bot=None`` is a reset. A thread bound to a named store
    keeps it (a binding is never lifted), so a reset or a switch to a Bot with another
    store refuses. A Bot on the Global store binds nothing (Global is its store, and a
    record would outlive ``!bot off`` and ``!ta``); hydration reads its name from the
    thread's ``agent`` field instead. Raises
    ``UnknownMemoryStore`` with the reason; the caller shows it. Nothing is published on
    a refusal, so the caller binds after it holds the thread's override slot.
    """
    from kiro_crew.config import KiroCrewConfig, live
    from kiro_crew.config.loader import resolve_agent_bindings
    from kiro_crew.dashboard.chat_handlers import (
        _record_explicit_agent_selection,
        pin_private_agent_store,
    )
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, UnknownMemoryStore

    # The last APPLIED reload first, as ``resolve_channel_crew`` reads it.
    cfg = live.snapshot()
    if cfg is None:
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
    refusal = await memory_binding_refusal(session_key, bot, cfg)
    if refusal:
        raise UnknownMemoryStore(refusal)
    target = getattr(cfg.agents.get(bot or ""), "memory_store", "") or ""
    store = "" if target == DEFAULT_MEMORY_STORE else target
    if bot is None or not store:
        return
    bindings = await asyncio.to_thread(
        resolve_agent_bindings, cfg, bot, None, validate_memory_files=False, selection_kind="member"
    )
    # The seam reads only ``sessions`` and ``conversation_log`` off the dashboard state.
    state: Any = SimpleNamespace(sessions=sessions, conversation_log=conversation_log)
    await pin_private_agent_store(state, session_key, bot, cfg)
    await _record_explicit_agent_selection(session_key, bot, bindings, config=cfg)


async def memory_binding_refusal(
    session_key: str, agent: str | None, cfg: Any = None
) -> str | None:
    """Why this thread may not move to *agent* (``None`` is a reset), or ``None``.

    A thread whose execution record names a store other than Global (``!bot`` bound it to
    a Bot's own memory) keeps that store for good, so it may move only to an agent whose
    ``config.agents`` record names the same store. Every thread-agent change asks this
    first: ``!bot <name>``, ``!bot off``, ``!ta <name>``, ``!ta off`` and ``/agent <name>``.
    The record is durable, so the answer is the same after a restart.
    """
    from kiro_crew.config import KiroCrewConfig, live
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

    prior = await asyncio.to_thread(read_session_execution, session_key)
    prior_store = prior.store.legacy_name if prior is not None else ""
    if prior is None or not prior_store:
        return None
    if cfg is None:
        cfg = live.snapshot() or await asyncio.to_thread(KiroCrewConfig.load)
    target = getattr((getattr(cfg, "agents", None) or {}).get(agent or ""), "memory_store", "")
    if (target or "") not in ("", DEFAULT_MEMORY_STORE) and target == prior_store:
        return None
    return (
        f"This thread's memory belongs to Bot {prior.selection_name or prior_store!r}. "
        "Start a new thread to use another Bot or none."
    )
