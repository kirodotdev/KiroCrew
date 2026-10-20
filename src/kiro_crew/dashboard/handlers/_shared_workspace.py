"""The memory, lessons and skills a request works in, and the project a slot pins.

The dashboard's memory and the lesson and skill stores of the caller's workspace, and
the active project directory of the requesting slot that the spawn gate and the
skill routes resolve against.

Served through :mod:`kiro_crew.dashboard.handlers._shared`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.dashboard.state import DashboardState


def _get_memory(state: DashboardState):
    """Get MemoryStore from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.memory
    # Fallback: create standalone MemoryStore
    if not hasattr(state, "_standalone_memory"):
        from kiro_crew.memory import MemoryStore

        mem = MemoryStore()
        mem.init()
        state._standalone_memory = mem  # type: ignore[attr-defined]
    return state._standalone_memory  # type: ignore[attr-defined]


def _lesson_caller_slot_key(state: DashboardState, session_key: str) -> str:
    """Map a lesson caller's session key to its slot key, or ``""``.

    Delegates to :func:`session_control.caller_slot_key` for the shared
    resolution (history key, slot key, transcript stem) and adds ONLY a
    fallback that matches each slot's :func:`effective_session_key`, so a
    channel-born slot surfaced without a session-map binding -- whose turns run
    on ``dashboard:<slot>`` -- still resolves to its own workspace.

    That extra match belongs ONLY to the lesson read/write path, which is why
    it lives here rather than in ``caller_slot_key``: widening the latter would
    also change the ``authorize_target`` self-target guard that every
    session-control verb consults, which this bug does not touch.
    """
    if not session_key:
        return ""
    from kiro_crew.dashboard import session_control

    resolved = session_control.caller_slot_key(state, session_key)
    if resolved:
        return resolved
    # Lazy, like the other session_control imports in this module.
    from kiro_crew.dashboard.chat_utils import effective_session_key

    for slot in list(getattr(state, "_slots", {}).values()):
        try:
            if session_key == effective_session_key(slot):
                return slot.key
        except Exception:
            continue
    return ""


def _get_active_workspace(state: DashboardState, session_key: str = "") -> str:
    """Return the REQUESTING session's workspace, or 'default'.

    Resolution is per-session: *session_key* names the chat slot making the
    call, and that slot's own ``workspace`` is returned. Falling back to
    ``'default'`` is the ONLY fallback -- never to another session's workspace.

    A global "which slot is busiest" value is not a safe source for this: it
    would route every session's workspace-scoped lesson reads and writes into
    whichever workspace happens to be most active, crossing workspace
    boundaries.

    A caller with no session key (``session_key`` empty) or whose slot cannot
    be found (headless/standalone, or a slot that does not exist) resolves to
    ``'default'``. The slot is resolved through the lessons-local
    :func:`_lesson_caller_slot_key`, not ``session_control.caller_slot_key``, so
    the session-control self-target guard is unaffected by this path.
    """
    slot = state.get_slot(_lesson_caller_slot_key(state, session_key)) if session_key else None
    ws = getattr(slot, "workspace", None) if slot is not None else None
    if ws and ws != "default":
        return ws
    return "default"


def _get_lessons(state: DashboardState, workspace: str | None = None, *, session_key: str = ""):
    """Get LessonStore for a workspace. Falls back to the requesting session.

    *workspace* wins when the caller names one explicitly; otherwise the
    workspace is resolved from *session_key* (the requesting slot), never from a
    global heuristic. Both resolve to the global store ('default') when nothing
    names a non-default workspace.
    """
    ws = workspace or _get_active_workspace(state, session_key)
    if ws != "default" and state.context_builder:
        return state.context_builder.get_lessons_for(ws)
    return state.lessons


def _get_skills(state: DashboardState):
    """Get SkillsLoader from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.skills
    if not hasattr(state, "_standalone_skills"):
        from kiro_crew.skills import SkillsLoader

        skills = SkillsLoader(install_builtins=False)
        state._standalone_skills = skills  # type: ignore[attr-defined]
    return state._standalone_skills  # type: ignore[attr-defined]


def active_project_state(state: DashboardState, session_key: str = "") -> tuple[Path | None, str]:
    """Resolve the workspace project AND why it is absent when it is.

    Returns ``(project, state)`` where *state* is one of:

    * ``"set"`` — *project* is a real directory and workspace-scoped resources
      resolve against it;
    * ``"none"`` — no open chat slot names a project at all;
    * ``"ambiguous"`` — two or more slots name DIFFERENT projects and
      *session_key* did not single one out, so there is no defensible answer.

    :func:`active_project_dir` collapses the last two to ``None``, which is the
    right call for a resolver but not for a UI: "you have no project" and "your
    open chats disagree" need different words and different remedies, and a
    caller that cannot tell them apart has to guess. Callers that only need the
    path should keep using :func:`active_project_dir`.
    """
    project = _resolve_active_project(state, session_key)
    if project is not None:
        return project, "set"
    slots = getattr(state, "_slots", {}) or {}
    distinct = {str(p) for p in (_slot_project(s) for s in slots.values()) if p is not None}
    return None, "ambiguous" if len(distinct) > 1 else "none"


def _slot_project(slot: Any) -> Path | None:
    """The project a chat slot is bound to, if any.

    ``project_dir`` is accepted alongside ``project`` for slot-like objects that
    expose that name instead.
    """
    pd = getattr(slot, "project", None) or getattr(slot, "project_dir", None)
    if isinstance(pd, Path):
        return pd
    if isinstance(pd, str) and pd:
        return Path(pd)
    return None


def active_project_dir(state: DashboardState, session_key: str = "") -> Path | None:
    """Return the project directory that workspace-scoped resources resolve against.

    Workspace-scoped resources (``<project>/.kiro/skills``,
    ``<project>/.kiro/steering``) live under the directory the agent actually
    runs in, which the dashboard stores per chat slot as ``_ChatSlot.project``
    (set by ``PUT /api/chat/slots/{slot}/project`` and the ``set_project`` MCP
    tool).  ``project_dir`` is accepted as a fallback for slot-like objects that
    expose that name instead.

    Resolution is deterministic, in this order:

    1. the slot named by *session_key*, when it has a project;
    2. otherwise the single project shared by every slot that has one;
    3. otherwise ``None``.

    Step 3 matters for mutations: with two chats open on different projects
    there is no defensible "active" project for a settings page, and silently
    picking the first-inserted slot would create, overwrite or delete files in
    the wrong project.  Failing closed makes the caller surface the ambiguity
    instead — :func:`active_project_state` reports which of the two "no answer"
    cases produced the ``None``.

    Step 2 is what makes this the WRONG helper for a per-chat resource. It
    answers for a chat that has no project of its own, so a caller that must
    agree with what one chat will actually load — the skills catalog, and the
    consent grant that admits those skills — would resolve a directory that chat
    is not bound to. Those callers use :func:`requesting_slot_project` instead.
    Reach for this one only when the resource really is global.
    """
    return _resolve_active_project(state, session_key)


def requesting_slot_project(state: DashboardState, session_key: str = "") -> Path | None:
    """The project bound to THIS chat slot, with no cross-slot fallback.

    :func:`active_project_dir` answers "which project should a global surface
    act on", and falls back to the single project shared by the open slots.
    This answers the narrower question the skills loader asks: "which project
    is THIS chat bound to". ``SkillsLoader`` resolves project skills from
    ``_ChatSlot.project`` verbatim, so a caller that must agree with what the
    loader will actually load -- the catalog, and the consent grant that admits
    it -- has to ask the same question, not the broader one.

    Returns ``None`` when this slot has no project, which is a meaningful
    answer: there is no directory for this chat to list, trust, or load from.
    """
    slots = getattr(state, "_slots", {}) or {}
    if not session_key:
        return None
    slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
    slot = slots.get(slot_name)
    if slot is None:
        return None
    return _slot_project(slot)


def session_project_for_spawn_gate(state: DashboardState, session_key: str) -> list[Path]:
    """Every project directory *session_key*'s own kiro-cli may be running in.

    For the spawn gate's parent-spec read (``subagent.parent_project_dirs``),
    which applies the spec of EACH directory returned. A sub-agent parent
    (``subagent:<id>``) answers its run's cwd. A chat parent answers the cwd its
    LIVE session is bound to (``provider.cwd``) AND the project of the slot whose
    effective session key IS *session_key*: a ``set_project`` change only
    stages the slot's project and resets the runtime on the next message, so
    until then the running agent still loaded its spec from the old directory.
    Deliberately NOT :func:`active_project_dir`: its "the one project every slot
    shares" fallback names a directory this session may never have run in.
    Dict reads only (no I/O), so it is safe from the worker thread the gate's
    read runs on.
    """
    if not session_key:
        return []
    if session_key.startswith("subagent:"):
        manager = getattr(state, "subagents", None)
        agents = getattr(manager, "_agents", None) or {}
        info = agents.get(session_key.removeprefix("subagent:"))
        cwd = getattr(info, "cwd", "") if info is not None else ""
        return [Path(cwd)] if isinstance(cwd, str) and cwd else []
    found: list[Path] = []
    sessions = getattr(state, "sessions", None)
    get_provider = getattr(sessions, "get_provider", None)
    if callable(get_provider):
        live_cwd = getattr(get_provider(session_key), "cwd", "")
        if isinstance(live_cwd, str) and live_cwd:
            found.append(Path(live_cwd))
    from kiro_crew.dashboard.chat_utils import effective_session_key

    for slot in list((getattr(state, "_slots", {}) or {}).values()):
        try:
            if effective_session_key(slot) != session_key:
                continue
        except Exception:  # noqa: BLE001 - a slot-like object without a key is not this session
            continue
        staged = _slot_project(slot)
        if staged is not None and staged not in found:
            found.append(staged)
        break
    return found


def _resolve_active_project(state: DashboardState, session_key: str) -> Path | None:
    """The three-step resolution shared by the two public accessors."""
    slots = getattr(state, "_slots", {}) or {}

    if session_key:
        slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
        slot = slots.get(slot_name)
        if slot is not None:
            scoped = _slot_project(slot)
            if scoped is not None:
                return scoped
    distinct: dict[str, Path] = {}
    for slot in slots.values():
        proj = _slot_project(slot)
        if proj is not None:
            distinct[str(proj)] = proj
    if len(distinct) == 1:
        return next(iter(distinct.values()))
    return None
