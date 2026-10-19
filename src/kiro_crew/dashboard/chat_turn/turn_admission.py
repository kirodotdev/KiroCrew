"""Whether a dashboard turn may run on the session it is bound to: the current binding and its
requirement, the owner check and the dispatch lock's release."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import _MemoryUnavailable, effective_session_key
    from kiro_crew.dashboard.chat_turn.app_agent_binding import _app_agent_start_dirs


def _current_binding(turn) -> tuple:
    slot = turn.slot
    return (
        slot.agent,
        slot.project,
        slot.workspace,
        slot.memory_store,
        slot._memory_assignment_from_history,
        effective_session_key(slot),
        slot.agent_kind,
    )


def _require_current_binding(turn, *, project_may_be_staged: bool = False) -> None:
    # ``project_may_be_staged``: once the session is registered the
    # project route may stage a pick for the NEXT turn (it writes
    # slot.project and arms the deferred reset). Checks made after that
    # point compare every other binding field and leave the project
    # out, because this turn runs on its start-of-turn project and the
    # staged pick is not a change to it.
    registered_slot = turn.registered_slot
    slot = turn.slot
    state = turn.state
    current = _current_binding(turn)
    expected = turn.selected_binding
    if project_may_be_staged:
        current = current[:1] + current[2:]
        expected = expected[:1] + expected[2:]
    if (registered_slot is slot and state._slots.get(slot.key) is not slot) or current != expected:
        raise _MemoryUnavailable(
            "memory_unavailable: conversation binding changed during preparation; " "retry the turn"
        )


def _check_owner(turn) -> str | None:
    # Only kiro-cli and KAS launch the app's named spec; checked first,
    # since the name check below means nothing on another backend.
    from kiro_crew.apps.manager import app_agent_backend_refusal, app_command_agent_refusal

    _owner_host_allowed = turn._owner_host_allowed
    _owner_project = turn._owner_project
    owner_agent = turn.owner_agent
    owner_app = turn.owner_app
    session_key = turn.session_key
    state = turn.state
    if not _owner_host_allowed:
        return app_agent_backend_refusal(owner_app)
    # Both halves touch the filesystem (the work-dir root, the specs).
    dirs = _app_agent_start_dirs(state, session_key, _owner_project)
    return app_command_agent_refusal(owner_app, owner_agent, *dirs)


def _release_dispatch_lock(turn) -> None:
    _dispatch_lock = turn._dispatch_lock
    if turn._dispatch_lock_held:
        turn._dispatch_lock_held = False
        _dispatch_lock.release()
