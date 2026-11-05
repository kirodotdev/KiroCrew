"""Relaunching a live ``AcpClient`` process and loading the same session in it.

Some harnesses read a setting once, at launch, and give a running process no way to
take a new value. For a harness that also keeps its own session records
(``session/load`` restores the conversation in a fresh process), the change can
still reach a live chat: end this process at a turn boundary, start a new one that
reads the setting as it is now, and load the same session id there.

:func:`relaunch_and_reload` is that step, and the only one here that touches a
process. It reuses the stalled-child replacement ``ensure_ready`` already runs for a
session the harness disposed (:mod:`kiro_crew.acp.disposed_session`): mark the
client stalled, keep its session id as the resume id, and call ``ensure_ready``,
which kills the process, spawns a replacement and ``session/load``-s the id.

The rest is the MCP use of it, for a member of
:data:`~kiro_crew.agent_sdk.backends.ACP_BACKENDS_MCP_RELAUNCH_RELOAD` (Strands: its
MCP servers come from the ``--mcp-config`` file written at each launch). A dashboard
MCP write that skips the all-sessions reset marks the client
(:func:`mark_mcp_config_changed`); the provider applies the mark before its next
prompt (:func:`apply_pending_mcp_relaunch`, called by ``AcpProvider`` after
``ensure_ready``), when no turn is running. A spawn that starts after the mark
already reads the new set, so the harness's launch clears the mark
(``_mcp_relaunch_pending``) right before it reads the agent spec, and a mark landing
after that read survives for the next boundary.

The functions take the client as their first argument; nothing here is bound on the
class, as for :mod:`kiro_crew.acp.disposed_session`.

Instance fields written: ``_mcp_relaunch_pending``, ``_relaunch_owed_session``,
``_resume_session_id``, ``_session_id``, ``_stdin_stalled``.

Instance fields read: ``_resumed``, ``_session_id``, ``_turn_lock``, ``backend``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MCP_RELAUNCH_RELOAD

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

__all__ = [
    "RELAUNCH_FAILED_TEXT",
    "SessionNotReloadedError",
    "apply_pending_mcp_relaunch",
    "mark_mcp_config_changed",
    "mcp_config_relaunch_reload",
    "relaunch_and_reload",
]

logger = logging.getLogger(__name__)


class SessionNotReloadedError(AcpError):
    """The relaunched process opened a different session than the one it was given."""


#: What a turn fails with when a relaunch could not reopen this chat. The load stays
#: owed, so the next message tries again; a chat whose saved session is gone keeps
#: failing, so the text names the way out.
RELAUNCH_FAILED_TEXT = (
    "Kiro Crew restarted the agent but could not reopen this chat. Send your message "
    "again to retry; if this keeps happening, start a new chat."
)


async def relaunch_and_reload(client: AcpClient) -> None:
    """End *client*'s process and load its session in a fresh one.

    Call only at a turn boundary, with a live process that holds a session. Returns
    once the replacement serves the same session id. Raises what ``ensure_ready``
    raises, and :exc:`SessionNotReloadedError` when the replacement opened a different session
    (``ensure_ready`` falls back to ``session/new`` when a load fails): a fresh
    session is not this conversation. Either way the client is left stalled with
    the session id as its resume id and as ``_relaunch_owed_session``: the next
    ``ensure_ready`` replaces the process again, and :func:`apply_pending_mcp_relaunch`
    refuses every turn until a replacement serves that id, so no turn is prompted
    into another conversation.
    """
    session_id = client._session_id
    if not session_id:
        raise ValueError("relaunch_and_reload needs a client that holds a session")
    client._stdin_stalled = True
    client._resume_session_id = session_id
    try:
        await client.ensure_ready()
    except BaseException:
        _owe_the_load(client, session_id)
        raise
    if not client._resumed or client._session_id != session_id:
        logger.warning(
            "%s: session %s did not load in the relaunched process (got %s)",
            client.backend,
            session_id,
            client._session_id,
        )
        _owe_the_load(client, session_id)
        raise SessionNotReloadedError(
            f"session {session_id} did not load in the relaunched process"
        )
    client._relaunch_owed_session = ""
    _reclaim(client)


def _reclaim(client: AcpClient) -> None:
    """Bind the stub token to the replacement process before the next prompt.

    The turn's identity publisher claimed the token for the process that was
    running when the turn was admitted, and the relaunch replaced it, so the
    gateway would refuse the new process's stubs until a later turn. The same
    re-push the KAS re-injection makes after its load
    (``acp/session_provider_mcp_reinject.py``). Never fails the turn.
    """
    try:
        client.reclaim()
    except Exception:
        logger.debug("%s: stub re-claim after relaunch failed", client.backend, exc_info=True)


def _owe_the_load(client: AcpClient, session_id: str) -> None:
    """Leave the next ``ensure_ready`` a replacement that loads *session_id*, and
    mark the load owed until one does.

    ``_session_id`` is pointed back at *session_id* too. A fallback ``session/new``
    may have left a fresh id there, and every reader of the chat's session (the
    shutdown sweep that saves it to the session map, ``AcpProvider.session_id``)
    must keep naming the real conversation. The client is stalled, so the next
    ``ensure_ready`` kills that fallback process before anything is prompted.
    """
    client._resume_session_id = session_id
    client._stdin_stalled = True
    client._relaunch_owed_session = session_id
    client._session_id = session_id


def _settle_owed_load(client: AcpClient) -> None:
    """Refuse the turn while a relaunch still owes this chat its session.

    Called after ``ensure_ready``: a replacement that loaded the owed id settles the
    debt; one that opened any other session (``ensure_ready``'s fallback to
    ``session/new``) is replaced again before the next turn, and this one is refused.
    """
    owed = getattr(client, "_relaunch_owed_session", "")
    if not owed:
        return
    if client._resumed and client._session_id == owed:
        client._relaunch_owed_session = ""
        _reclaim(client)
        return
    logger.warning(
        "%s: session %s is still owed after a relaunch (got %s); refusing the turn",
        client.backend,
        owed,
        client._session_id,
    )
    _owe_the_load(client, owed)
    raise AcpError(RELAUNCH_FAILED_TEXT, transient=False, user_worded=True)


def mcp_config_relaunch_reload(client: AcpClient) -> bool:
    """Whether a dashboard MCP write reaches this client's session by a relaunch.

    Membership only (harness-parity H6). True before the first spawn as well: a mark
    set then is cleared by that spawn, which reads the new set itself.
    """
    return client.backend in ACP_BACKENDS_MCP_RELAUNCH_RELOAD


def mark_mcp_config_changed(client: AcpClient) -> None:
    """Note that the MCP set this client launched with is behind the config.

    A flag, applied by :func:`apply_pending_mcp_relaunch`. A client outside the set
    is reset by the writer instead, so it keeps no mark.
    """
    if mcp_config_relaunch_reload(client):
        client._mcp_relaunch_pending = True


async def apply_pending_mcp_relaunch(client: AcpClient) -> None:
    """Relaunch *client* and load its session if an MCP change is pending.

    Called before a prompt, after ``ensure_ready``, for every direct client. With
    no mark and no owed load (every harness outside the set, and a member with
    nothing pending) it returns before reading anything else. It does nothing
    while a turn holds the turn lock (never mid-turn: the work waits for the next
    boundary). Then a load an earlier relaunch still owes is settled or the turn
    is refused (:func:`_settle_owed_load`), and a mark with no live process
    holding a session is left for the next spawn, which reads the new set. The mark is consumed before the relaunch is awaited, so a write
    landing during it marks the client again and the next boundary applies it. A
    replacement that opened another session, or a failure that is not an
    ``AcpError``, refuses this turn with :data:`RELAUNCH_FAILED_TEXT`. Any other
    ``AcpError`` (a spawn refusal that names its own remedy, a process that died)
    reaches the caller unchanged, as it would from an ordinary ``ensure_ready``;
    the load stays owed either way.
    """
    pending = getattr(client, "_mcp_relaunch_pending", False) is True
    owed = getattr(client, "_relaunch_owed_session", "")
    if not pending and not owed:
        return
    if client._turn_lock.locked():
        return
    _settle_owed_load(client)
    if not pending:
        return
    if not client._is_process_alive() or not client._session_id:
        return
    client._mcp_relaunch_pending = False
    session_id = client._session_id
    logger.info(
        "%s session %s: MCP config changed; relaunching before this turn",
        client.backend,
        session_id,
    )
    try:
        await relaunch_and_reload(client)
    except SessionNotReloadedError as exc:
        raise AcpError(RELAUNCH_FAILED_TEXT, transient=False, user_worded=True) from exc
    except AcpError:
        logger.warning(
            "%s session %s: relaunch for the MCP change was refused",
            client.backend,
            session_id,
            exc_info=True,
        )
        raise
    except Exception as exc:
        logger.warning(
            "%s session %s: relaunch for the MCP change failed",
            client.backend,
            session_id,
            exc_info=True,
        )
        raise AcpError(RELAUNCH_FAILED_TEXT, transient=False, user_worded=True) from exc
    logger.info("%s session %s: relaunched; the new MCP set is live", client.backend, session_id)
