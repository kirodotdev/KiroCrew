"""The dashboard session a code channel runs in.

Every code channel is bound to ONE ordinary dashboard session: it appears in
the sidebar (filed in the configured default folder), runs the configured
agent / model / reasoning effort, and can be opened, read and typed into like
any other chat. Slack is a second window onto it: a message in the channel is
delivered to the session, and the session's replies are mirrored back into the
channel TOP-LEVEL (a code channel is one session, never a thread).

**What the session starts with** (:func:`resolve_session_spec`)

========== =================================================================
field      first that is set wins
========== =================================================================
agent      explicit default agent > the default folder's ``default_agent``
           (nearest ancestor that declares one) > Slack's default agent
model      explicit default model > the agent's own
effort     explicit default reasoning effort > the agent's own
folder     the default folder when it still exists > top level
project    the folder's project directory (nearest ancestor) > the origin
           thread's bound project (``!project``) > none (dashboard default)
========== =================================================================

**How Slack reaches it.** The session's Slack link is recorded against the ts
of the kickoff message this gateway posts when it binds the channel (the
binding's ``anchor_ts``), because the link index is keyed by a ts. An inbound
message in the channel is routed to the session the anchor names
(:func:`kiro_crew.slack.handler.maybe_route_linked_thread`),
and :func:`mirror_post_thread` makes every mirrored post omit ``thread_ts``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack.code_channels.settings import (
    CodeChannelDefaults,
    code_channels_on,
    find_folder,
    live_config,
)
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionSpec:
    """Resolved settings for a code channel's new dashboard session."""

    agent: str = ""
    model: str = ""
    reasoning_effort: str = ""
    folder_id: str = ""
    project_dir: str = ""


class BindingUnavailable(Exception):
    """No dashboard session could be created (no dashboard, store failure...)."""


def _folder_by_id(folders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(f.get("id") or ""): f for f in folders if isinstance(f, dict)}


def folder_default_agent(folders: list[dict[str, Any]], folder_id: str) -> str:
    """The nearest ``default_agent`` up *folder_id*'s ancestor chain ("" if none).

    The same walk the dashboard's agent picker makes (``folderAgent.ts``), so a
    code channel filed in a folder starts on the agent a chat opened there
    would.
    """
    by_id = _folder_by_id(folders)
    seen: set[str] = set()
    current = folder_id
    while current and current not in seen:
        seen.add(current)
        folder = by_id.get(current)
        if folder is None:
            return ""
        agent = folder.get("default_agent")
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
        current = str(folder.get("parent_id") or "")
    return ""


def _slack_default_agent(cfg: Any) -> str:
    agent = getattr(getattr(cfg, "agent", None), "default_agent", "")
    return agent.strip() if isinstance(agent, str) else ""


def _origin_project(origin_thread_ts: str) -> str:
    """The project the origin Slack thread was bound to with ``!project``."""
    if not origin_thread_ts:
        return ""
    try:
        from kiro_crew.messaging.link import canonical_key
        from kiro_crew.slack.handler import _thread_projects

        return str(_thread_projects.get(canonical_key(origin_thread_ts)) or "")
    except Exception:
        logger.debug("origin thread project unavailable", exc_info=True)
        return ""


async def resolve_session_spec(
    state: Any,
    cfg: Any,
    defaults: CodeChannelDefaults,
    *,
    origin_thread_ts: str = "",
) -> SessionSpec:
    """Apply the resolution table in the module docstring."""
    folders: list[dict[str, Any]] = []
    if state is not None and hasattr(state, "read_folders"):
        folders = await state.read_folders(lambda fs: [dict(f) for f in fs if isinstance(f, dict)])
    found = find_folder(folders, defaults.folder)
    folder_id = str(found.get("id") or "") if found else ""
    if defaults.folder and not folder_id:
        logger.info(
            "code channel default folder %s does not exist; filing at top level",
            defaults.folder,
        )
    agent = defaults.agent or folder_default_agent(folders, folder_id) or _slack_default_agent(cfg)
    project = ""
    if folder_id:
        from kiro_crew.dashboard.chat_folders import resolve_folder_project_dir_off_loop

        project, error = await resolve_folder_project_dir_off_loop(folders, folder_id)
        if error:
            logger.info("code channel folder project unusable (%s); ignoring it", error)
            project = ""
    if not project:
        project = _origin_project(origin_thread_ts)
    return SessionSpec(
        agent=agent,
        model=defaults.model,
        reasoning_effort=defaults.reasoning_effort,
        folder_id=folder_id,
        project_dir=project,
    )


async def create_bound_session(
    state: Any,
    spec: SessionSpec,
    *,
    title: str,
    channel_id: str,
    anchor_ts: str,
) -> Any:
    """Create the dashboard session for a code channel and link it to Slack.

    Mirrors the owner path of ``POST /api/chat/slots`` (agent stamp, binding
    resolution, explicit selection record, folder filing, persist at birth),
    then records the Slack link against *anchor_ts*. Returns the slot.
    """
    if state is None or not hasattr(state, "get_or_create_slot"):
        raise BindingUnavailable("no dashboard")
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.loader import _workspace_name_for_dir, resolve_agent_bindings

    # The chat_api owners run composed into chat_handlers' namespace; reach
    # them through it, as every route does.
    from kiro_crew.dashboard.chat_handlers import (
        _default_template_stamp,
        _record_explicit_agent_selection,
        _unhide_folder,
        pin_private_agent_store,
    )
    from kiro_crew.dashboard.chat_utils_slots import (
        _sync_dashboard_slots,
        effective_session_key,
        slot_history_key,
    )
    from kiro_crew.dashboard.slot_persistence import metadata_codec
    from kiro_crew.effort import is_valid_effort

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    agent, agent_kind = spec.agent, ""
    if not agent:
        agent = await asyncio.to_thread(_default_template_stamp, cfg)
        agent_kind = "template"
    project = spec.project_dir
    bindings = await asyncio.to_thread(
        resolve_agent_bindings,
        cfg,
        agent,
        project or None,
        validate_memory_files=False,
        selection_kind=agent_kind,
    )
    workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
    if not project:
        # The same fallback a new dashboard chat gets: the configured default
        # project when it is usable, else the workspace directory.
        import os

        from kiro_crew.config.loader import default_project_dir
        from kiro_crew.security import is_sensitive_path

        configured = getattr(getattr(cfg, "dashboard", None), "default_project", "")
        if isinstance(configured, str) and configured:
            resolved = os.path.realpath(os.path.expanduser(configured))
            if os.path.isdir(resolved) and not is_sensitive_path(resolved):
                project = resolved
        if not project:
            project = await asyncio.to_thread(default_project_dir, workspace)

    with state.suspend_slots_push():
        slot = state.get_or_create_slot(None, agent=agent, workspace=workspace, model=spec.model)
        if agent_kind:
            slot.agent_kind = agent_kind
        slot.title = title
        slot._titled = True
        slot._title_origin = "user"
        slot.project = project
        if spec.model:
            slot._model_pick_gen += 1
        if spec.reasoning_effort and is_valid_effort(spec.reasoning_effort):
            slot.reasoning_effort = spec.reasoning_effort
        if spec.folder_id:
            slot.folder_id = spec.folder_id
            if not await _unhide_folder(state, spec.folder_id):
                slot.folder_id = ""
        key = effective_session_key(slot)
        assigned_store = (
            ""
            if agent_kind == "template"
            else await pin_private_agent_store(state, key, agent, cfg, memory_mode=slot.memory_mode)
        )
        chosen = await asyncio.to_thread(
            resolve_agent_bindings,
            cfg,
            slot.agent,
            slot.project or None,
            validate_memory_files=False,
            selection_kind=agent_kind,
        )
        slot.agent_kind = chosen.selection_kind
        await _record_explicit_agent_selection(
            key, slot.agent, chosen, config=cfg, memory_mode=slot.memory_mode
        )
        slot.memory_store = assigned_store or bindings.memory_store_name
        _sync_dashboard_slots(state)
        log = getattr(state, "conversation_log", None)
        if log is not None:
            line = {
                "_type": "metadata",
                **metadata_codec.encode(
                    slot,
                    folds=metadata_codec.SaveFolds(
                        memory_mode=getattr(slot, "memory_mode", "persistent"),
                        created_at=slot.created_at,
                        tab_id=getattr(slot, "_tab_id", None),
                    ),
                ),
            }
            history_key = slot_history_key(slot)
            await asyncio.to_thread(
                log.update_metadata_if, history_key, line, lambda meta: not meta
            )
        state.link_slack(slot.key, anchor_ts, channel_id)
        state.push_slots_update()
    return slot


def bound_slot(state: Any, binding: Binding) -> Any | None:
    """The live slot *binding* names, via the Slack link index."""
    if state is None or not hasattr(state, "get_linked_slot"):
        return None
    return state.get_linked_slot(binding.anchor_ts)


def active_binding(channel_id: str | None, thread_ts: str | None = None) -> Binding | None:
    """The binding for *channel_id* while the feature is on, else None.

    *thread_ts*, when given, must be the binding's anchor: a session linked to
    some OTHER thread of the same channel is not the code channel's session.
    """
    if not channel_id:
        return None
    binding = store().get_binding(channel_id)
    if binding is None:
        return None
    if thread_ts is not None and thread_ts != binding.anchor_ts:
        return None
    if not code_channels_on(live_config()):
        return None
    return binding


def mirror_post_thread(channel_id: str | None, thread_ts: str | None) -> str | None:
    """The ``thread_ts`` a dashboard-to-Slack mirror post uses.

    ``None`` (post top-level) for a bound code channel's own session while the
    feature is on; *thread_ts* unchanged everywhere else, so with the toggle
    off -- or for any ordinary linked thread -- mirroring is exactly as before.
    """
    if active_binding(channel_id, thread_ts) is not None:
        return None
    return thread_ts


async def notify_turn_start(
    channel_id: str | None, thread_ts: str | None, session_key: str, *, source: str
) -> bool:
    """Fire turn-start hooks for a bound channel; True when it is one."""
    binding = active_binding(channel_id, thread_ts)
    if binding is None:
        return False
    from kiro_crew.slack.code_channels.hooks import TurnEvent, fire_turn_start

    await fire_turn_start(binding, TurnEvent(binding.channel_id, session_key, source=source))
    return True


async def notify_turn_end(
    channel_id: str | None, thread_ts: str | None, session_key: str, *, source: str, outcome: str
) -> None:
    """Fire turn-end hooks for a bound channel (no-op elsewhere)."""
    if not channel_id:
        return
    # Read without the toggle: a turn that STARTED with the feature on must
    # still settle its status if the toggle flips mid-turn.
    binding = store().get_binding(channel_id)
    if binding is None or (thread_ts is not None and thread_ts != binding.anchor_ts):
        return
    from kiro_crew.slack.code_channels.hooks import TurnEvent, fire_turn_end

    await fire_turn_end(
        binding, TurnEvent(binding.channel_id, session_key, source=source, outcome=outcome)
    )
