"""The dashboard session a code channel runs in.

Every code channel is bound to ONE ordinary dashboard session: it appears in
the sidebar (filed in the configured default folder), runs the configured
agent / model / reasoning effort, and can be opened, read and typed into like
any other chat. Slack is a second window onto it: a message in the channel is
delivered to the session, and the session's replies are mirrored back into the
channel TOP-LEVEL (a code channel is one session, never a thread).

**What the session starts with** (:func:`resolve_session_spec`): the order
:func:`kiro_crew.slack.features.resolve_code_channel_session` applies --
agent from the explicit default, else the default folder's ``default_agent``,
else Slack's default agent; model and effort explicit or the agent's own; the
default folder when it exists; project from the folder chain, else the origin
thread's ``!project``, else the dashboard default; approval mode explicit, else
``ask``. A ``trust_reads`` / ``trust`` mode is put on the new slot through
:func:`kiro_crew.dashboard.slot_approval_seed.seed_slot_approval` -- the same
per-slot flags and ``mode_change:<mode>`` SEL record the dashboard picker
writes -- once the session is bound, so the channel's first turn already runs
in it. The user can change it on the session afterwards; like every slot's
trust it is in-memory, so a gateway restart returns the session to asking.

**How Slack reaches it.** The session's Slack link is recorded against the ts
of the kickoff message this gateway posts when it binds the channel (the
binding's ``anchor_ts``), because the link index is keyed by a ts. An inbound
message in the channel is routed to the session the anchor names
(:func:`kiro_crew.slack.handler.maybe_route_linked_thread`),
and :func:`mirror_post_thread` makes every mirrored post omit ``thread_ts``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from kiro_crew.slack import features
from kiro_crew.slack.code_channels.store import Binding, store

logger = logging.getLogger(__name__)


def live_config(orch: Any | None = None) -> Any | None:
    """The gateway's live config (reload-aware), or None when it cannot be read."""
    try:
        from kiro_crew.slack.handler import slack_cfg

        return slack_cfg(orch)
    except Exception:
        logger.debug("code channels: live config unavailable", exc_info=True)
        return None


def code_channels_on(cfg: Any | None) -> bool:
    """The one gate: :func:`kiro_crew.slack.features.code_channels_enabled`.

    The setting is on AND the installed app can run code channels (scope
    granted, not latched). An unreadable config is off.
    """
    return cfg is not None and features.code_channels_enabled(cfg)


@dataclass(frozen=True)
class SessionSpec:
    """Resolved settings for a code channel's new dashboard session."""

    agent: str = ""
    #: ``"template"`` when *agent* is a shared template by name (the defaults
    #: picker's pick); ``""`` lets the binding resolve the name as usual.
    agent_kind: str = ""
    model: str = ""
    reasoning_effort: str = ""
    folder_id: str = ""
    project_dir: str = ""
    #: ``ask`` / ``trust_reads`` / ``trust``.
    approval_mode: str = features.CODE_CHANNEL_APPROVAL_ASK


#: The SEL caller a code channel's approval seed is recorded under.
APPROVAL_SEED_CALLER = "slack:code_channel"


class BindingUnavailable(Exception):
    """No dashboard session could be created (no dashboard, store failure...)."""


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


async def resolve_session_spec(state: Any, cfg: Any, *, origin_thread_ts: str = "") -> SessionSpec:
    """Apply :func:`kiro_crew.slack.features.resolve_code_channel_session`.

    The order is that function's; this only supplies its inputs (the live folder
    tree, Slack's default agent, the origin thread's project) and validates a
    folder project directory, which the pure resolver leaves to the session
    layer.
    """
    folders: list[dict[str, Any]] = []
    if state is not None and hasattr(state, "read_folders"):
        folders = await _read_folders(state)
    origin_project = _origin_project(origin_thread_ts)
    defaults = features.code_channel_defaults(cfg)
    if not defaults.folder and state is not None and hasattr(state, "read_folders"):
        # Zero-config: file into the "Code channels" folder, created on first
        # use (and again if the user deleted it) with the conductor as its
        # default agent. The resolution order is unchanged -- an explicit
        # default agent still wins over the folder's.
        try:
            auto_id = await ensure_folder_path(
                state, AUTO_FOLDER_PATH, default_agent=AUTO_FOLDER_AGENT
            )
        except BindingUnavailable as exc:
            # Two folders of that name: the settings page says so; the session
            # goes unfiled (the resolver's own rule).
            logger.info("code channel default folder not used: %s", exc)
        except Exception:
            logger.warning("code channel default folder could not be created", exc_info=True)
        else:
            folders = await _read_folders(state)
            defaults = dataclasses.replace(defaults, folder=auto_id)
    resolved = features.resolve_code_channel_session(
        defaults,
        folders,
        slack_agent=_slack_default_agent(cfg),
        origin_project_dir=origin_project,
    )
    if resolved.folder_missing:
        logger.info("code channel default folder does not exist; filing at top level")
    from kiro_crew.dashboard.slack_code_settings import default_agent_available

    if await default_agent_available(defaults.agent, resolved.project_dir) is False:
        # A saved default that is gone (its spec removed) is skipped, as the
        # settings preview shows: the folder's agent, then Slack's, runs instead.
        logger.info("code channel default agent %r no longer exists; inheriting", defaults.agent)
        resolved = features.resolve_code_channel_session(
            defaults,
            folders,
            slack_agent=_slack_default_agent(cfg),
            origin_project_dir=origin_project,
            default_agent_available=False,
        )
    folder_id = resolved.folder_id or ""
    project = resolved.project_dir or ""
    if resolved.project_source == "folder":
        from kiro_crew.dashboard.chat_folders import resolve_folder_project_dir_off_loop

        project, error = await resolve_folder_project_dir_off_loop(folders, folder_id)
        if error:
            logger.info("code channel folder project unusable (%s); ignoring it", error)
            project = origin_project
    return SessionSpec(
        agent=resolved.agent or "",
        # The defaults picker offers templates only: a same-named crewmate must
        # not take that pick. Folder and Slack defaults resolve as they do elsewhere.
        agent_kind="template" if resolved.agent_source == "default" else "",
        model=resolved.model or "",
        reasoning_effort=resolved.reasoning_effort or "",
        folder_id=folder_id,
        project_dir=project,
        approval_mode=resolved.approval_mode,
    )


async def create_bound_session(
    state: Any,
    spec: SessionSpec,
    *,
    title: str,
    channel_id: str,
    anchor_ts: str,
    before_publish: Callable[[Any], Awaitable[None]] | None = None,
) -> Any:
    """Create the dashboard session for a code channel and link it to Slack.

    Mirrors the owner path of ``POST /api/chat/slots`` (agent stamp, binding
    resolution, explicit selection record, folder filing, persist at birth),
    then records the Slack link against *anchor_ts*. Returns the slot. The
    caller holds *anchor_ts* (:func:`hold_anchor`) from posting it until this
    returns, so no other slot can be linked to it before the binding exists.
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
        save_slot_off_loop,
    )
    from kiro_crew.dashboard.chat_utils_slots import (
        _sync_dashboard_slots,
        effective_session_key,
        slot_history_key,
    )
    from kiro_crew.dashboard.slot_approval_seed import seed_slot_approval
    from kiro_crew.dashboard.slot_persistence import metadata_codec
    from kiro_crew.effort import is_valid_effort

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    agent, agent_kind = spec.agent, spec.agent_kind
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
        from kiro_crew.config.loader import default_project_dir

        configured = getattr(getattr(cfg, "dashboard", None), "default_project", "")
        if isinstance(configured, str) and configured:
            project = await asyncio.to_thread(_usable_project_dir, configured)
        if not project:
            project = await asyncio.to_thread(default_project_dir, workspace)

    if getattr(state, "conversation_log", None) is None:
        # No durable history means the session could not be persisted at birth.
        raise BindingUnavailable("session history unavailable")

    with state.suspend_slots_push():
        slot = state.get_or_create_slot(None, agent=agent, workspace=workspace, model=spec.model)
        try:
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
                else await pin_private_agent_store(
                    state, key, agent, cfg, memory_mode=slot.memory_mode
                )
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
            log = state.conversation_log
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
            # Persist at birth, before anything is linked or published. A first
            # write lands the full line; when the agent selection above already
            # wrote execution metadata to the same transcript, the forced save
            # merges the birth fields into it. Either must commit, or the slot is
            # taken back down and the bind fails -- a restart must not lose the
            # title, folder, project, model and effort the user was shown.
            persisted = await asyncio.to_thread(
                log.update_metadata_if, history_key, line, lambda meta: not meta
            )
            if not persisted:
                persisted = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=history_key,
                )
            if not persisted:
                raise BindingUnavailable("session metadata not saved")
        except BaseException:
            # Any failure before the link -- a refused or raising save, a
            # cancellation -- takes the still-idle newborn back down inside the
            # suspension, so nothing unbound is published and a retry starts
            # clean.
            _retract(state, slot, "", _sync_dashboard_slots)
            raise
        # The link writer's own body, without its ``batched_save`` wrapper: the
        # batch exists to make a HANDOFF (clear the old owner, claim for the new)
        # one write, and it writes the session map inline on the loop when it
        # ends. The anchor is a message this gateway just posted, so it has no
        # previous owner and this is a single ``set_slack_link``, which on the
        # loop is a debounced write in a worker. ``aflush`` then lands it off
        # the loop, and *before_publish* (the caller's binding write) runs,
        # before the session is published. A failure in any of the three
        # undoes the link and the slot, so no linked session without a binding
        # is left for a retry to duplicate.
        try:
            state._link_slack_persisted(slot, slot.key, anchor_ts, channel_id)
            if getattr(state, "sessions", None) is not None:
                await state.sessions.aflush()
            if before_publish is not None:
                await before_publish(slot)
        except BaseException:
            _retract(state, slot, anchor_ts, _sync_dashboard_slots)
            raise
        # Bound and recorded: only now put the slot on its configured posture, so
        # a retracted newborn never leaves a SEL record for a session that does
        # not exist, and the published slot is already in it. ``ask`` is the
        # starting posture and changes nothing. A channel the policy refuses
        # (narrowed during the awaits above) gets no raised posture.
        from kiro_crew.slack.code_channels import surface

        if (
            spec.approval_mode != features.CODE_CHANNEL_APPROVAL_ASK
            and surface.channel_policy_refusal(
                channel_id, operation="slack.code_channel.write", caller=APPROVAL_SEED_CALLER
            )
        ):
            logger.info("code channel %s: approval mode not seeded (channel policy)", channel_id)
        elif seed_slot_approval(state, slot, spec.approval_mode, caller=APPROVAL_SEED_CALLER):
            logger.info("code channel %s: session starts in %s", channel_id, spec.approval_mode)
        state.push_slots_update()
    return slot


def _retract(state: Any, slot: Any, anchor_ts: str, sync: Callable[[Any], None]) -> None:
    """Take an unpublished newborn back down: its Slack link, then the slot.

    Best effort past the first step: the in-memory link and slot are always
    dropped; a persisted link that cannot be cleared is logged, and a restart
    finds a link to a session that has no binding, which nothing routes to.
    """
    if anchor_ts and state._slack_to_slot.get(anchor_ts) == slot.key:
        state._slack_to_slot.pop(anchor_ts, None)
    if anchor_ts:
        slot._slack_linked = False
        slot._slack_thread_ts = ""
        slot._slack_channel = ""
        sessions = getattr(state, "sessions", None)
        if sessions is not None:
            from kiro_crew.dashboard.chat_utils_slots import effective_session_key

            try:
                sessions.clear_slack_link(effective_session_key(slot))
            except Exception:
                logger.warning("code channel: retracted session's link not cleared", exc_info=True)
    if not slot.running and state._slots.get(slot.key) is slot:
        state._slots.pop(slot.key, None)
        sync(state)


def _usable_project_dir(configured: str) -> str:
    """*configured* resolved, when it is an existing, non-sensitive directory.

    Filesystem calls: run it through ``asyncio.to_thread``.
    """
    import os

    from kiro_crew.security import is_sensitive_path

    resolved = os.path.realpath(os.path.expanduser(configured))
    if os.path.isdir(resolved) and not is_sensitive_path(resolved):
        return resolved
    return ""


#: The sidebar folder zero-config code channels file into, and its agent: the
#: resolver's own constants, so creation and resolution name the same folder.
AUTO_FOLDER_PATH = features.AUTO_FOLDER_NAME
AUTO_FOLDER_AGENT = features.AUTO_FOLDER_AGENT


async def _read_folders(state: Any) -> list[dict[str, Any]]:
    folders: list[dict[str, Any]] = await state.read_folders(
        lambda fs: [dict(f) for f in fs if isinstance(f, dict)]
    )
    return folders


def _child_folder(folders: list[dict[str, Any]], parent_id: str, name: str) -> str:
    """The one child of *parent_id* named *name*; "" when there is none.

    Two or more of that name are ambiguous, as the settings resolver
    (``features.resolve_code_channel_session``) reads them: filing into an
    arbitrary one would run the session with that folder's agent and project
    while the settings page previews an unfiled one, so this raises instead.
    """
    wanted = name.strip().casefold()
    matches = [
        str(folder["id"])
        for folder in folders
        if str(folder.get("parent_id") or "") == parent_id
        and str(folder.get("name") or "").strip().casefold() == wanted
        and folder.get("id")
    ]
    if len(matches) > 1:
        raise BindingUnavailable(f"{len(matches)} folders share the name {name!r}")
    return matches[0] if matches else ""


async def ensure_folder_path(state: Any, path: str, *, default_agent: str = "") -> str:
    """The id of the sidebar folder at *path* (``Parent/Child``), mkdir -p.

    Existing segments are reused by name (trimmed, case-folded, the rule the
    folder path walk uses); a missing one is created through the folder store's
    single create path, ``create_folder_record``, which validates it and writes
    it under the folders lock, and each creation is SEL-audited. *default_agent*
    is set on the last segment only, and only when that segment is created: a
    folder the user already has keeps its own default agent.
    """
    from kiro_crew.dashboard.chat_folders import FolderNameExistsError, create_folder_record
    from kiro_crew.sel import sel

    segments = [s.strip() for s in path.split(features.FOLDER_PATH_SEP) if s.strip()]
    if not segments:
        raise BindingUnavailable("empty folder path")
    parent = ""
    for index, name in enumerate(segments):
        existing = _child_folder(await _read_folders(state), parent, name)
        if existing:
            parent = existing
            continue
        last = index == len(segments) - 1
        try:
            folder = await create_folder_record(
                state,
                name=name,
                parent_id=parent,
                default_agent=default_agent if last else "",
                refuse_duplicate_name=True,
            )
        except FolderNameExistsError:
            # Created concurrently by someone else: use theirs.
            existing = _child_folder(await _read_folders(state), parent, name)
            if not existing:
                raise
            parent = existing
            continue
        parent = str(folder["id"])
        if not folder.get("reused"):
            state.push_slots_update()
            sel().log_api_access(
                caller="slack-code-channels",
                operation="folder_auto_create",
                outcome="allowed",
                source="slack",
                resources=parent,
            )
    return parent


def bound_slot(state: Any, binding: Binding) -> Any | None:
    """The live slot *binding* names, via the Slack link index."""
    if state is None or not hasattr(state, "get_linked_slot"):
        return None
    return state.get_linked_slot(binding.anchor_ts)


def channel_session(state: Any, binding: Binding) -> Any | None:
    """:func:`bound_slot`, only while it is still the session *binding* recorded.

    The binding store is the gateway's own record; the slot's routing is read
    back from its transcript, which an agent can rewrite. A change of the
    channel's agent or approval mode acts on the slot only while its key is the
    binding's ``session_key`` and its turns run on that same session: a code
    channel's session is never linked to another, so a ``linked_session_key``
    read back onto it names some other session and the change is refused.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key, session_key_for

    slot = bound_slot(state, binding)
    if slot is None or str(getattr(slot, "key", "") or "") != binding.session_key:
        return None
    if getattr(slot, "linked_session_key", ""):
        return None
    if effective_session_key(slot) != session_key_for(binding.session_key):
        return None
    return slot


def active_binding(channel_id: str | None, thread_ts: str | None = None) -> Binding | None:
    """The binding for *channel_id* while the feature is on, else None.

    *thread_ts*, when given, must be the binding's anchor: a session linked to
    some OTHER thread of the same channel is not the code channel's session.
    Called on the event loop for every linked dashboard turn: the gate is read
    first, and the store only when it is already loaded (never its cold load).
    """
    if not channel_id or not code_channels_on(live_config()):
        return None
    binding = store().get_binding_if_loaded(channel_id)
    if binding is None:
        return None
    if thread_ts is not None and thread_ts != binding.anchor_ts:
        return None
    return binding


#: Anchors of code channels being bound right now, ``anchor_ts -> holder``
#: (:func:`hold_anchor`), so the guard covers the window before the binding
#: exists.
_BINDING_ANCHORS: dict[str, str] = {}

_BIRTH_HOLD = "<code channel being bound>"


@contextlib.contextmanager
def hold_anchor(anchor_ts: str) -> Iterator[None]:
    """Hold a just-posted anchor while its code channel is bound.

    Entered with no await after the post returns, and held until the binding is
    stored (or the bind failed): any link of the anchor meanwhile is refused by
    :func:`check_anchor_handoff`. The binding session's own link writes it
    directly, past the guard.
    """
    held = bool(anchor_ts) and anchor_ts not in _BINDING_ANCHORS
    if held:
        _BINDING_ANCHORS[anchor_ts] = _BIRTH_HOLD
    try:
        yield
    finally:
        if held:
            _BINDING_ANCHORS.pop(anchor_ts, None)


#: Stores whose unreadable file was already warned about and audited, by id.
_UNREADABLE_REPORTED: set[int] = set()


class AnchorHandoffRefused(Exception):
    """A link refused by the code channel anchor guard: the thread is another
    slot's code channel anchor."""

    def __init__(self, thread_ts: str) -> None:
        super().__init__(f"code channel anchor guard refused: {thread_ts}")
        self.thread_ts = thread_ts


def anchor_owner_by_ts(thread_ts: str | None) -> str | None:
    """The slot key that owns *thread_ts* as its code channel's anchor, or None.

    Matched by timestamp alone, whatever channel a caller names: the link
    writer (``DashboardState.link_slack``) keys a thread by its ts, so the
    guard must too. A channel being bound right now answers from
    :data:`_BINDING_ANCHORS`. Snapshot-only: never loads, never takes the
    store's lock, so a cold store answers None.
    """
    if not thread_ts:
        return None
    pending = _BINDING_ANCHORS.get(thread_ts)
    if pending:
        return pending
    cc = store()
    if not cc.loaded:
        return None
    for binding in cc.bindings():
        if binding.anchor_ts == thread_ts and binding.session_key:
            return binding.session_key
    return None


async def load_store_for_links(operation: str) -> None:
    """Load the store off the loop before a link that names an existing thread.

    Idempotent and never raises. A store that failed to load is treated like
    one that cannot be read: code channels cannot route without it, so there
    is nothing to split, and the link proceeds with a warning and an audit row
    naming why. The guard then reads the unloaded store as "no binding".
    """
    cc = store()
    try:
        await asyncio.to_thread(cc.load)
    except Exception:
        logger.warning(
            "code channel store load failed; linking without its anchor check", exc_info=True
        )
        _audit_store_unavailable(operation, "the code channel store failed to load")
        return
    if cc.unreadable and id(cc) not in _UNREADABLE_REPORTED:
        _UNREADABLE_REPORTED.add(id(cc))
        logger.warning("code channel store unreadable; linking without its anchor check")
        _audit_store_unavailable(operation, "the code channel store could not be read")


def _audit_store_unavailable(operation: str, error: str) -> None:
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller="code_channels",
        operation=operation,
        outcome="allowed",
        source="code_channel_store_unavailable",
        error=error,
    )


def check_anchor_handoff(thread_ts: str | None, slot_key: str) -> None:
    """Raise :class:`AnchorHandoffRefused` unless *slot_key* may own *thread_ts*.

    The one chokepoint every public Slack-thread link passes through
    (``DashboardState.link_slack``, and the routes before their Slack writes).
    A code channel IS its binding's session (Stop and button answers follow the
    binding), so its anchor thread is never linked to another slot. Read
    without the feature toggle: a binding outlives a switch-off.

    Synchronous and snapshot-only (:func:`anchor_owner_by_ts`). Every caller
    that can name an EXISTING thread preloads the store off the loop
    (:func:`load_store_for_links`); a store that still did not load answers
    "no binding", as an unreadable one does. ``link_slack``'s other writers
    link a thread the gateway just posted, which no binding can hold.
    """
    owner = anchor_owner_by_ts(thread_ts)
    if owner is not None and owner != slot_key:
        raise AnchorHandoffRefused(thread_ts or "")


def mirror_post_thread(channel_id: str | None, thread_ts: str | None) -> str | None:
    """The ``thread_ts`` a dashboard-to-Slack mirror post uses.

    ``None`` (post top-level) for a bound code channel's own session while the
    feature is on; *thread_ts* unchanged everywhere else, so with the toggle
    off -- or for any ordinary linked thread -- mirroring is exactly as before.
    """
    if active_binding(channel_id, thread_ts) is not None:
        return None
    return thread_ts


async def notify_turn_prepare(state: Any, slot: Any) -> bool:
    """Prepare a DASHBOARD turn's code channel before the turn reads its bindings.

    Admission reads the session's project for the runtime it starts before the
    turn-start hooks run; the preparer that moves the project (the workspace,
    into the channel's worktree) runs here and is awaited, so THIS turn starts
    there. False when it could not be prepared: the caller does not run the
    turn rather than run it in the shared checkout. Any session that is not a
    code channel's costs one link lookup and answers True. Never raises
    (``CancelledError`` propagates).
    """
    try:
        from kiro_crew.dashboard.chat_utils_slots import effective_session_key

        key = effective_session_key(slot)
        thread_ts, channel_id = state.sessions.get_slack_link(key)
        binding = active_binding(channel_id, thread_ts) if channel_id and thread_ts else None
    except Exception:
        logger.debug("code channel: no binding lookup for a dashboard turn", exc_info=True)
        return True
    if binding is None:
        return True
    from kiro_crew.slack.code_channels.hooks import prepare_turn

    return await prepare_turn(binding)


async def notify_turn_start(
    channel_id: str | None,
    thread_ts: str | None,
    session_key: str,
    *,
    source: str,
    on_bound: Callable[[], None] | None = None,
    prompt: str = "",
) -> bool:
    """Fire turn-start hooks for a bound channel; True when it is one.

    *on_bound* runs once the channel is known to be bound and before any
    subscriber is awaited, so a caller records that it owes a turn end even
    when the turn is cancelled part way through the start subscribers.
    """
    binding = active_binding(channel_id, thread_ts)
    if binding is None or binding.archived_at:
        # An archived channel takes no turns: its session is a plain chat now.
        return False
    if on_bound is not None:
        on_bound()
    from kiro_crew.slack.code_channels.hooks import TurnEvent, fire_turn_start

    await fire_turn_start(
        binding, TurnEvent(binding.channel_id, session_key, source=source, prompt=prompt)
    )
    return True


async def notify_turn_end(
    channel_id: str | None, thread_ts: str | None, session_key: str, *, source: str, outcome: str
) -> None:
    """Fire turn-end hooks for a bound channel (no-op elsewhere)."""
    if not channel_id:
        return
    # Read without the toggle: a turn that STARTED with the feature on must
    # still settle its status if the toggle flips mid-turn. Never the cold load:
    # a turn that started on a bound channel found the store loaded already.
    binding = store().get_binding_if_loaded(channel_id)
    if binding is None or (thread_ts is not None and thread_ts != binding.anchor_ts):
        return
    from kiro_crew.slack.code_channels.hooks import TurnEvent, fire_turn_end

    await fire_turn_end(
        binding, TurnEvent(binding.channel_id, session_key, source=source, outcome=outcome)
    )
