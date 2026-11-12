"""Start a fresh ``AcpClient`` conversation on the live adapter: ``new_conversation``.

The cheap reset a reused workflow-pool worker and a history consolidation pass ask for.
Only a member of ``ACP_BACKENDS_CLIENT_NEW_CONVERSATION`` has it; every other backend on
its own client raises ``NotImplementedError`` and is reset by kill and respawn.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_background_launches``, ``_between_turn_credits``,
``_buffer``, ``_crew_bridge_snapshots``, ``_last_activity``, ``_mcp_notifications``,
``_resumed``, ``_session_id``.

Instance fields read: ``_session_mcp_snapshot``, ``last_prompt_stats``.

The stale compaction-status state is cleared through
:mod:`kiro_crew.acp.stale_compaction`, which owns those fields.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.acp import kas_env_launch
from kiro_crew.acp._dispatch import BackgroundLaunchRecord
from kiro_crew.acp.client_timeouts import _READ_TIMEOUT
from kiro_crew.acp.stale_compaction import clear_stale_compaction_state
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.acp.types import METHOD_SESSION_CLOSE
from kiro_crew.agent import require_unchanged_derived_spec
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_CLIENT_NEW_CONVERSATION,
    POLICY_ID_BY_BACKEND,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

logger = logging.getLogger("kiro_crew.acp.client")


async def new_conversation(self: AcpClient) -> None:
    """Replace this session with a fresh one on the SAME adapter process.

    Skips the spawn and the ``initialize`` handshake a kill-and-respawn pays. The
    settings seed and the ``mcpServers`` array are prepared again off the loop, as a
    launch prepares them. Then ``session/close`` ends the old session and its Claude Code
    process, and what it left buffered is dropped, so this transport never reads two
    sessions as one. The new ``session/new`` is set up as a cold start sets one up:
    the same ``_meta`` and array, the starting-mode pin with its project-deny
    re-read, the derived-spec check, the MCP mount check, the member projection and
    the model.

    Any failure after the gate ends the process and resets the client to its
    not-started state, on a cancel too, so no half-set-up session survives; the
    caller's kill-and-respawn fallback then starts a fresh one. That includes a
    seed that stops governing the settings file: a file replaced since the last
    session is left out and the asking mode pinned only on a fresh client.
    """
    if self.backend not in ACP_BACKENDS_CLIENT_NEW_CONVERSATION:
        backend = POLICY_ID_BY_BACKEND.get(self.backend, self.backend)
        raise NotImplementedError(
            f"the {backend!r} backend has no fresh-conversation reset on its live "
            "process; it is reset by kill and respawn"
        )
    old_sid = self._session_id
    if not old_sid or not self._is_process_alive():
        raise AcpProcessDied("no live session to start a fresh conversation beside")
    # The old session's Connection bridges serve it until ``session/close``; the
    # fresh array projects its own, so only those are left on the client.
    old_bridges, self._crew_bridge_snapshots = self._crew_bridge_snapshots, []
    try:
        governed = self._permission_surface_governed
        await self._seed_session_settings()
        if governed and not self._permission_surface_governed:
            # Crew's settings file was replaced since the last session. Only a fresh
            # client leaves such a file out of the setting sources and pins the
            # asking mode, so this reset gives way to the cold start.
            raise AcpError("the session's settings file was replaced; starting it afresh")
        await self._prepare_claude_side_read_denies()
        await self._prepare_session_mcp()
        req_id = await self._send_request(METHOD_SESSION_CLOSE, {"sessionId": old_sid})
        await self._wait_for_response(req_id, timeout=_READ_TIMEOUT, method=METHOD_SESSION_CLOSE)
        if old_bridges:
            await asyncio.to_thread(kas_env_launch.discard_batch, [], old_bridges)
            old_bridges = []
        self._buffer.clear()
        self._mcp_notifications.clear()
        resp = await self._new_session_following_substitution()
        # Read before the re-seed below, which drops the projection it describes.
        sent_snapshot = self._session_mcp_snapshot
        new_sid = resp.get("sessionId")
        if not new_sid:
            raise AcpError("session/new returned no sessionId for the fresh conversation")
        self._session_id = new_sid
        self._capture_available_models(resp)
        if self._uses_advertised_model_selection:
            await self._persist_advertised_models_if_changed()
            await self._reseed_after_capture()
        self._store_session_config(resp)
        await self._pin_claude_starting_mode(resp)
        await asyncio.to_thread(require_unchanged_derived_spec, sent_snapshot)
        self._start_mount_check()
        await self.confirm_member_projection()
        await self._apply_startup_model()
        await self._drain_notifications()
    except BaseException:
        # Not yet closed: the process end in ``_reset_state`` removes them.
        self._crew_bridge_snapshots += old_bridges
        try:
            await self._cleanup_failed_live_spawn()
        except Exception:
            logger.warning("ACP fresh-conversation cleanup failed", exc_info=True)
        finally:
            self._reset_state()
        raise
    self.last_prompt_stats.reset_context_state()
    self.last_prompt_stats.begin_cost_session(new_sid)
    self._between_turn_credits = 0.0
    self._background_launches = BackgroundLaunchRecord()
    clear_stale_compaction_state(self)
    # The fresh conversation holds none of the pictures the old one was sent.
    self._forget_sent_images()
    self._resumed = False
    self._last_activity = time.monotonic()
    logger.info("ACP session %s replaced by a fresh conversation %s", old_sid, new_sid)
