"""A shared-runtime session's warm-pool claim and the per-turn re-push of it.

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller. The claim helpers are read off ``acp.session_provider`` at
call time, so a patch of them there reaches these methods.

Instance fields written: ``_channel_id``, ``_session_crew_agent``, ``_session_key``.

Instance fields read: ``_channel_id``, ``_handle``, ``_runtime``, ``_session_key``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import WatchdogSettings
    from kiro_crew.acp.session_provider import AcpSessionProvider


def rekey(
    self: AcpSessionProvider,
    session_key: str,
    channel_id: str | None = None,
    crew_agent: str = "",
    watchdog: WatchdogSettings | None = None,
) -> None:
    """Re-key for a different session on warm-pool claim (parity with
    AcpClient.rekey). session_allocation_parts/get_or_create.py calls
    provider.client.rekey(...) on claim; when the pooled provider is
    kiro-shared, provider.client is THIS class, so a missing rekey() would
    AttributeError on claim. Stores
    the correlation keys and refreshes runtime activity so the just-claimed
    process is not idle-reaped.

    ``crew_agent`` is the claiming session's canonical crew identity: the
    pooled runtime was spawned before any crew claimed it, so both the
    runtime default (future sessions, e.g. new_conversation) and the live
    handle's watchdog snapshot are rebound here — the identity travels
    with the session, not the pool key. Empty means "no crew" and rebinds
    to the globals, so a recycled runtime never carries a previous crew's
    windows. ``watchdog`` is the pre-resolved snapshot from the async
    caller (resolved off-loop); None makes rebind load it synchronously."""
    from kiro_crew.acp import session_provider as _sp

    self._session_key = session_key
    self._channel_id = channel_id
    self._runtime._crew_agent = crew_agent
    # This session's own identity too: new_conversation passes it, so a pooled
    # provider built before the claim must not keep the empty one it was made with.
    self._session_crew_agent = crew_agent
    self._handle.rebind_watchdog(crew_agent, settings=watchdog)
    self._handle.bind_session_key(session_key)
    self._runtime._last_activity = time.monotonic()
    # Parity with AcpClient.rekey: the handle's prompt stats describe the
    # session this runtime served BEFORE the handoff; leaking them lets
    # check_context_usage() compact the new, empty session.
    self._handle.last_prompt_stats.reset_context_state()
    # Credits held between turns belong to the session served before the
    # claim; the claiming session's first turn must not bill them.
    self._handle._between_turn_credits = 0.0
    # Claim-push: re-target this session's MCP stub connections under the
    # shared runtime's PID to the claiming session (see AcpClient.rekey for
    # the rationale). Fire-and-forget; no-ops without a gateway socket.
    #
    # Named by the handle's stub token, so the claim reaches THIS session's
    # stubs and leaves every sibling session on the same runtime alone — a
    # subagent's stubs must not be re-pointed at the slot that claimed the
    # runtime. Empty (the gateway injected no stubs, or an older session)
    # falls back to the PID-wide re-target this always did.
    _sp.schedule_claim(
        self._runtime._mcp_gateway_socket,
        self._runtime.pid,
        session_key,
        channel_id,
        getattr(self._handle, "stub_session_token", ""),
    )
    # Re-point this session's SIGNED token mapping at the claiming session, for
    # the reason AcpClient.rekey states: the token survives the rekey so the
    # file is what moves. It matters more here — this runtime hosts several
    # sessions at once, so the mapping is the only channel that can tell them
    # apart without a daemon. Fire-and-forget; offloads its own file I/O.
    _sp.schedule_session_token_publish(getattr(self._handle, "stub_session_token", ""), session_key)


def reclaim(self: AcpSessionProvider) -> None:
    """Re-push this session's claim (parity with AcpClient.reclaim).

    The shared runtime makes this the case that matters: its stubs belong to
    several sessions at once, so the claim must name THIS session's token —
    and after a gatewayd respawn every one of those tokens is unbound, which
    gatewayd refuses rather than resolving from the shared process tree.
    Called at the start of every turn by the shared identity publisher.
    """
    from kiro_crew.acp import session_provider as _sp

    token = getattr(self._handle, "stub_session_token", "")
    if not token:
        return
    _sp.schedule_claim(
        self._runtime._mcp_gateway_socket,
        self._runtime.pid,
        self._session_key,
        self._channel_id,
        token,
    )
