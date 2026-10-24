"""Applying a dashboard MCP config change to a LIVE wire-projected session.

On a host in ``ACP_BACKENDS_MCP_WIRE_REINJECT`` (KAS) the session's MCP servers
travel inside the projected agent (``_meta.kiro.customAgents``) and the engine
registers that batch once, so no file watcher and no re-sent ``set_mode`` can
change a running session's set. What the engine does take -- measured, see the
set's own note in :mod:`kiro_crew.agent_sdk.backends_mcp` -- is ``session/close``
on the resident session, ``session/load`` of the SAME id carrying the new batch,
and ``session/set_mode``: it replays the conversation it kept and the next turn
runs with the new tools. ``AcpRuntime.load_session`` already performs the load
and activation with every guard the first load had (projection freshness, the
managed-stub hoist, the readiness wait, mode refusal), so re-injection is one
evict and one load, done at the session's own next turn boundary.

The dashboard writer marks the provider (:meth:`mark_mcp_config_changed`) in the
same breath as it skips the all-sessions reset; the mark is applied the next time
the provider is asked to run a turn (:meth:`apply_pending_mcp_reinjection`, called
from ``stream`` under the shared-turn claim, so no turn is in flight on the
session). Never mid-turn: ``session/close`` on a session with a running prompt
would cut it.

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller. The runtime side -- the record-keeping teardown scope and
the eviction -- is ``acp/runtime_session_registry.py``.

Instance fields written: ``_handle``, ``_mcp_reinject_pending``.

Instance fields read: ``_channel_id``, ``_handle``, ``_runtime``, ``_session_key``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.acp.client import DEFAULT_MODEL, AcpError
from kiro_crew.acp.types import ACP_BACKENDS_MCP_WIRE_REINJECT, ACP_BACKENDS_MEMBER_DISPATCH

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider

#: The provider's logger: a re-injection is part of that session's story.
logger = logging.getLogger("kiro_crew.acp.session_provider")


def mcp_config_wire_reinject(self: AcpSessionProvider) -> bool:
    """Whether this LIVE session takes a re-sent projection at its next turn.

    Membership (harness-parity H6 -- opt-in per harness, never inferred) on a
    session that exists: this provider is constructed around a handle, so there
    is always a session to re-load. The placeholder client a session starts
    from is not this class and answers the ``LLMProvider`` default, False.
    """
    return self._runtime.acp_backend in ACP_BACKENDS_MCP_WIRE_REINJECT


def mark_mcp_config_changed(self: AcpSessionProvider) -> None:
    """Note that this session's projection is behind the MCP config on disk.

    A flag, applied at the next turn boundary. Kept on the PROVIDER rather than
    the handle because the apply replaces the handle. A second mark landing
    while one is pending collapses into it (the load reads the config as it is
    then, which covers both writes); one landing while an apply is already
    running survives that apply, because the apply consumes its mark before it
    awaits anything (:func:`apply_pending_mcp_reinjection`).
    """
    if not self.mcp_config_wire_reinject:
        # A host that does not re-inject is reset by the writer instead; a mark
        # left set here would make the first turn on it attempt a verb the host
        # was never measured to take.
        return
    self._mcp_reinject_pending = True


async def apply_pending_mcp_reinjection(self: AcpSessionProvider) -> None:
    """Re-send this session's projection if a config change is pending.

    Called under the shared-turn claim, before the prompt. No-op when nothing is
    pending. Otherwise: evict the resident session (``session/close``, record
    kept), then ``AcpRuntime.load_session`` on the same id, which re-reads the
    agent spec, re-projects the batch, re-declares the broker stubs and
    activates the mode exactly as a resume does. The returned handle replaces
    this provider's, and the session's gateway claim is re-pushed for the stubs
    the load relaunched.

    The previous non-default model is re-applied, as ``new_conversation`` does:
    the engine restores the session's own model on load, but a slot pinned to a
    non-default one must not depend on that. It is the LAST step, after the
    handle swap, so a cancel during that round trip leaves a usable session.

    The mark is CONSUMED before the first await and never cleared after one: a
    write landing while the load runs marks the provider again, and that mark
    survives this apply, so the next boundary re-loads from the spec as it is
    then. On failure the consumed mark is restored, so the load is retried at
    the next boundary (a ``session/close`` of an id the host does not hold is
    answered as an error this path ignores, so a half-done apply converges), and
    the turn that found the failure is refused with an ``AcpError`` naming the
    one recovery -- a session restart -- rather than run on a session the engine
    may have already dropped.

    The load runs inside ``AcpRuntime.keep_record_on_teardown``: its failure arms
    (an activation refusal, a stale spec, a timed-out ``set_mode``) end the
    session through ``terminate_session``, whose KAS verb deletes the stored
    record -- the conversation this apply exists to keep. Inside the scope that
    teardown is the record-keeping eviction instead.
    """
    if not getattr(self, "_mcp_reinject_pending", False):
        return
    old = self._handle
    sid = old.session_id
    runtime = self._runtime
    if not runtime.is_alive():
        # The resident copy died with the process; the next start re-projects.
        self._mcp_reinject_pending = False
        return
    # Consumed here, before any await: see the docstring.
    self._mcp_reinject_pending = False
    # The same rule as the provider layer's ``_member_session_key``: a member DM
    # on a dispatch-capable backend re-mounts its dispatch server on load, and
    # this load must carry what the first one carried or the thread loses its
    # tools mid-conversation. Resolved here because the provider layer sits
    # above this module.
    member_session_key = ""
    if runtime.acp_backend in ACP_BACKENDS_MEMBER_DISPATCH and self._session_key:
        from kiro_crew.members import is_member_session_key

        if is_member_session_key(self._session_key):
            member_session_key = self._session_key
    agent = old.active_agent or runtime._agent or None
    logger.info(
        "session %s: MCP config changed; re-sending its projection before this turn",
        sid,
    )
    try:
        with runtime.keep_record_on_teardown(sid):
            await runtime.evict_session(sid)
            new_handle = await runtime.load_session(
                "",
                sid,
                cwd=old._bound_cwd or None,
                agent=agent,
                crew_agent=old._crew_agent,
                member_session_key=member_session_key,
                session_key=self._session_key,
                channel_id=self._channel_id or "",
            )
    except BaseException as exc:
        # Restored on EVERY exit that is not a loaded session, a cancel included,
        # so the change is applied at the next boundary rather than lost.
        self._mcp_reinject_pending = True
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "session %s: re-sending its projection failed; the change stays pending",
            sid,
            exc_info=True,
        )
        raise AcpError(
            "The MCP change could not be applied to this chat yet; "
            "send again, or restart the session to apply it."
        ) from exc
    # The handle is swapped BEFORE anything else is awaited: the old one's queue is
    # unregistered, so a cancel landing between here and the model re-apply would
    # otherwise leave the provider prompting a session whose replies route to a
    # queue nobody reads. The per-session facts the load cannot read back off the
    # wire travel with the swap.
    new_handle.keep_transcript = old.keep_transcript
    new_handle.memory_mode = old.memory_mode
    # Set by this turn's admission on the OLD handle before the apply ran; a new
    # handle defaults to the fail-closed False, which would auto-reject a routed
    # child permission request the dashboard can present.
    new_handle.child_fidelity_aware = old.child_fidelity_aware
    self._handle = new_handle
    # The load relaunched this session's stubs with a fresh token nothing has
    # named yet; claim it against the session this provider serves.
    try:
        self.reclaim()
    except Exception:
        logger.debug("re-injection: stub re-claim failed", exc_info=True)
    logger.info("session %s: projection re-sent; the new MCP set is live", sid)
    prior_model = old.model
    if isinstance(prior_model, str) and prior_model and prior_model != DEFAULT_MODEL:
        # Last, and after the swap, so a failure or a cancel here costs the pinned
        # model for this session (the engine restored its own) and nothing else.
        try:
            await new_handle.set_model(prior_model)
        except Exception:
            logger.warning(
                "session %s: re-applying model %s after re-injection failed; "
                "the session runs the model the engine restored",
                sid,
                prior_model,
                exc_info=True,
            )
