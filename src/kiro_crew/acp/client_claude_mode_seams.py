"""Claude requested-mode adapters for existing ``AcpClient`` seams.

The shared session start, frame reader, model switch and readiness path stay unchanged.
``ClaudeAcpClient`` binds these wrappers over their existing seams to apply a requested
``auto`` after the startup model, keep the start drain's idle window, follow mode reports,
move a requested-``auto`` session out of an approving mode before a live model switch
(any other session is switched in the mode it is in, as on main), restore a start-drain
report before the first prompt, stop a settled session that reports an approving mode
before its reader can dispatch an agent-started turn, and restore asking mode before a
prompt.

Each function here whose first parameter is ``self`` is a ``ClaudeAcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_confirmed_mode``, ``_claude_mode_restore_due``,
``_claude_mode_settling``, ``_claude_mode_watched``, ``_claude_policy_deny_due``,
``_claude_policy_stop_is_policy``, ``_claude_policy_stop_process``,
``_claude_start_response``.

Instance fields read: none.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from kiro_crew.acp import client_model_selection as _client_model_selection
from kiro_crew.acp import client_start as _client_start
from kiro_crew.acp import client_transport as _client_transport
from kiro_crew.acp.client_timeouts import _DRAIN_DURATION, _DRAIN_IDLE_EXIT, _READ_TIMEOUT
from kiro_crew.acp.types import JsonRpcMessage

if TYPE_CHECKING:
    from kiro_crew.acp.client import ClaudeAcpClient

logger = logging.getLogger("kiro_crew.acp.client")


def _forget_confirmed_mode(self: ClaudeAcpClient) -> None:
    """Drop what an earlier session confirmed before this client opens another."""
    self._claude_confirmed_mode = None
    self._claude_mode_watched = False
    self._claude_mode_restore_due = False
    self._claude_mode_settling = False
    self._claude_policy_deny_due = False
    self._claude_policy_stop_process = None
    self._claude_policy_stop_is_policy = False
    self._claude_start_response = None


def _initialize_params(self: ClaudeAcpClient) -> dict[str, Any]:
    """Build initialize params after forgetting the previous session's mode.

    ``_initialize_session`` calls this existing seam before ``session/load`` or
    ``session/new``. Clearing the follower here means a replayed mode report from
    the session being loaded cannot be attributed to the session it replaces.
    """
    self._forget_confirmed_mode()
    return _client_start._initialize_params(self)


@contextlib.asynccontextmanager
async def _ended_on_requested_mode_cancel(
    client: ClaudeAcpClient,
) -> AsyncIterator[None]:
    """End a start cancelled while its requested mode remains unsettled."""
    try:
        yield
    except asyncio.CancelledError:
        try:
            await client._cleanup_failed_live_spawn()
        except Exception:
            logger.warning("ACP requested-mode cleanup after a cancel failed", exc_info=True)
        finally:
            client._reset_state()
        raise


async def _drain_notifications(
    self: ClaudeAcpClient,
    duration: float = _DRAIN_DURATION,
    idle_exit: float = _DRAIN_IDLE_EXIT,
    *,
    until_declared: bool = False,
) -> None:
    """Apply requested ``auto``, drain its reports, and restore asking mode.

    The shared start calls this existing seam after its startup model. A response
    retained by :meth:`_pin_starting_mode` therefore applies ``auto`` in the right
    order without adding a step to any other client's construction path. While a
    requested mode is unsettled, the drain keeps its normal idle window even though
    the shared Claude path passes ``idle_exit=0.0``; a cancellation ends the session
    because the adapter may have changed mode without reporting where it landed.
    """
    response = getattr(self, "_claude_start_response", None)
    if response is None or not self._follows_requested_mode:
        self._claude_start_response = None
        await _client_transport._drain_notifications(
            self,
            duration=duration,
            idle_exit=idle_exit,
            until_declared=until_declared,
        )
        return
    async with _ended_on_requested_mode_cancel(self):
        await self._apply_claude_requested_mode(response)
        await _client_transport._drain_notifications(
            self,
            duration=duration,
            idle_exit=_DRAIN_IDLE_EXIT,
            until_declared=until_declared,
        )
        await self._restore_claude_asking_mode_if_due()
    self._claude_start_response = None
    self._claude_mode_settling = False


async def _read_message(
    self: ClaudeAcpClient,
    timeout: float = _READ_TIMEOUT,
    *,
    check_cancel: bool = True,
) -> JsonRpcMessage | None:
    """Read one frame and follow any mode report it carries."""
    msg = await _client_transport._read_message(self, timeout=timeout, check_cancel=check_cancel)
    if msg is not None and self._follows_requested_mode:
        stop = self._note_claude_mode_report(msg)
        if stop is not None:
            # The reader must not consume an agent-started turn frame after the
            # approving report. Shield the stop because its process teardown cancels
            # the idle reader that may be awaiting it.
            await asyncio.shield(stop)
    return msg


async def _push_model_config_option(self: ClaudeAcpClient, model_id: str, *, strict: bool) -> str:
    """Before an explicit live model switch, leave a requested-``auto`` session's approving mode.

    Only a session that follows its requested mode (:attr:`_follows_requested_mode`)
    is moved first. Any other claude session, one in a mode it inherited from
    ``~/.claude`` or a resumed transcript included, reaches the shared push with no
    extra call and keeps its mode, as on main.
    """
    if strict and self._follows_requested_mode:
        await self._leave_claude_approving_mode_for_model_switch()
    return await _client_model_selection._push_model_config_option(self, model_id, strict=strict)


async def ensure_ready(self: ClaudeAcpClient) -> None:
    """Ready the session, then restore asking mode before the caller may prompt.

    A policy stop can land while the restore is waiting for its answer. That prompt
    then fails with the process-exit error, and its next attempt starts a fresh
    session under the denying ceiling.
    """
    await _client_start.ensure_ready(self)
    await self._restore_claude_asking_mode_if_due()
