"""Where a chat's terminal may run: the refusal of a local shell for a chat bound to
a session environment, the argv and environment of a shell in its remote workspace, and
the retirement of local shells once the chat is bound.

An owner of :mod:`kiro_crew.dashboard.handlers.terminal`: callers and tests import and
patch these names through that facade, which mirrors a patch into every module of the
family that holds the name.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard.handlers.terminal_lifecycle import _kill_session, _sess_alive
from kiro_crew.dashboard.handlers.terminal_session import _sel, _TerminalSession
from kiro_crew.dashboard.handlers.terminal_transport import _close_terminal_ws_bounded

# The chat's placement is known and this shell would run on the wrong machine:
# the client parks the tab. A placement that could not be READ (a Coder call or
# the choices store failed) gets the other code, which the client redials like a
# dropped socket, so a brief outage never strands a live terminal tab.
_WRONG_MACHINE = "terminal_wrong_machine"
_PLACEMENT_UNAVAILABLE = "terminal_placement_unavailable"
_CHAT_NOT_LOADED = "This chat is not loaded yet, so no local shell was opened"


async def _remote_terminal_argv(
    request: web.Request, slot_name: str
) -> tuple[list[str] | None, str, str]:
    """``(argv, "", "")`` for a shell in ``slot_name``'s remote workspace, else
    ``(None, why, error_code)``."""
    from kiro_crew.coder.session_browse import NotRemote, remote_terminal_argv
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    state = request.app.get("state")
    slots = getattr(state, "_slots", None) or {}
    slot = slots.get(slot_name) if isinstance(slots, dict) else None
    if slot is None:
        # Not loaded yet (a restart restores chats after the server is up) or
        # gone: either way no shell, and a tab that redials finds out which.
        return None, "Unknown chat for this terminal", _PLACEMENT_UNAVAILABLE
    try:
        return await remote_terminal_argv(effective_session_key(slot)), "", ""
    except NotRemote:
        return None, "This chat runs on this machine, not a remote environment", _WRONG_MACHINE
    except SessionEnvironmentUnavailable as exc:
        return None, f"The remote workspace is not available: {exc}", _PLACEMENT_UNAVAILABLE


def _remote_pty_env(extra: dict[str, str]) -> dict[str, str]:
    """The environment of a workspace shell's local ``coder ssh``: the same one every
    other ``coder ssh`` uses (no gateway secret, no inherited ``CODER_*`` override),
    so the shell signs in with the stored login that verified the workspace, plus
    the terminal's own *extra*."""
    from kiro_crew.coder.remote_worker import _scrubbed_env

    env = _scrubbed_env()
    env.update(extra)
    return env


async def _local_terminal_refusal(
    request: web.Request, slot_name: str, *, reattach: _TerminalSession | None = None
) -> tuple[str, str]:
    """``(why, error_code)`` when a LOCAL shell opened for chat ``slot_name`` is
    refused, else ``("", "")``.

    The gateway decides the chat's placement itself rather than trusting the
    client to ask for a remote shell: a tab opened before the chat was bound,
    or after a missed placement read, must not run on the gateway. A slot the
    gateway does not hold yet (a tab redialing while a restart restores its
    chats) opens no local shell either: its placement is unknown, so the tab is
    told to redial, as ``_remote_terminal_argv`` does for the same case.

    ``reattach`` is the live local session a reconnect would take over. When
    its chat is gone (closed or deleted), the reconnect goes through: that
    shell already runs here, and a chat that does not exist cannot be bound
    elsewhere, so refusing would only strand its running processes.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.session_environment import (
        SessionEnvironmentUnavailable,
        _choices,
        session_environments_enabled,
    )

    # Flag off: every session runs here, and the choices are never read.
    if not session_environments_enabled():
        return "", ""
    state = request.app.get("state")
    slots = getattr(state, "_slots", None) or {}
    slot = slots.get(slot_name) if isinstance(slots, dict) else None
    if slot is None:
        if (
            reattach is not None
            and not reattach.remote
            and reattach.chat_slot == slot_name
            and _sess_alive(reattach)
        ):
            return "", ""
        return _CHAT_NOT_LOADED, _PLACEMENT_UNAVAILABLE
    try:
        profile = await asyncio.to_thread(_choices().get, effective_session_key(slot))
    except SessionEnvironmentUnavailable:
        # Still no local shell (fail closed), but the tab may redial.
        return (
            "Could not read where this chat runs, so no local shell was opened",
            _PLACEMENT_UNAVAILABLE,
        )
    if profile is not None:
        return (
            "This chat runs on a remote environment, so this shell would open on the"
            " wrong machine. Close this tab and open a new terminal",
            _WRONG_MACHINE,
        )
    return "", ""


def _session_environments_on() -> bool:
    from kiro_crew.session_environment import session_environments_enabled

    return session_environments_enabled()


def _chat_gone(request: web.Request, slot_name: str) -> bool:
    """Whether the gateway holds no chat named ``slot_name``."""
    slots = getattr(request.app.get("state"), "_slots", None) or {}
    return not isinstance(slots, dict) or slots.get(slot_name) is None


async def retire_local_shells_of_chat(
    registry: dict,
    slot_name: str,
    *,
    error_message: str = (
        "This chat now runs on a remote environment, so this shell on this"
        " machine was closed. Open a new terminal"
    ),
    error_code: str = _WRONG_MACHINE,
) -> int:
    """Close every LOCAL shell opened for chat ``slot_name``; return how many.

    Called once the chat is bound to a session environment: its commands now
    belong on that machine, and a connected socket is never re-checked, so a tab
    whose placement read is still cached could keep typing into the gateway.
    Each socket gets the wrong-machine code, which parks the tab, and a redial
    meets ``_local_terminal_refusal``. A shell still opening when the chat is
    bound is caught by the opener's own re-check once it publishes.
    """
    retired = 0
    for sid, sess in list(registry.items()):
        if sess is None or sess.remote or sess.chat_slot != slot_name:
            continue
        if registry.get(sid) is not sess:
            continue
        del registry[sid]
        ws = sess.ws
        if ws is not None and not ws.closed:
            await _close_terminal_ws_bounded(ws, error_message=error_message, error_code=error_code)
        await _kill_session(sess)
        _sel().log_api_access(
            caller="gateway",
            operation="terminal.session.retire",
            outcome="ok",
            source="dashboard",
            resources=f"session={sid},chat_bound=1",
        )
        retired += 1
    return retired
