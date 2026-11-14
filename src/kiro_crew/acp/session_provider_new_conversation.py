"""A fresh conversation on the same warm runtime: ``AcpSessionProvider.new_conversation``.

Each function here whose first parameter is ``self`` is an ``AcpSessionProvider``
method: the class binds it by name, so it reads and writes the provider's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields written: ``_handle``, ``_resumed_flag``.

Instance fields read: ``_handle``, ``_runtime``, ``_session_key``,
``_shared_runtime``, ``kas_memory_mode``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.acp.client import DEFAULT_MODEL, AcpError, AcpProcessDied
from kiro_crew.acp.types import ACP_BACKENDS_SESSION_EVICTION

if TYPE_CHECKING:
    from kiro_crew.acp.session_provider import AcpSessionProvider

#: The provider's logger, so this method's records stay under the provider's name.
logger = logging.getLogger("kiro_crew.acp.session_provider")


async def new_conversation(
    self: AcpSessionProvider, *, member_session_key: str = "", channel_id: str = ""
) -> None:
    """Reset to a fresh conversation on the SAME warm runtime (kiro path).

    The cheap clean-slate reuse primitive a warm worker pool relies on for a
    backend on the shared runtime; a backend on its own ``AcpClient`` has
    ``AcpClient.new_conversation`` instead. Create a brand-new ``session/new``
    on the *already-running* kiro-cli process (paying only session/new + the
    MCP-init drain), then destroy the old session so its context/transcript
    and MCP children are freed on the shared process. This SKIPS the expensive
    parts of a cold start — subprocess spawn + the ACP ``initialize`` handshake
    — which is exactly what makes pooled reuse faster than teardown+respawn.

    If the runtime is dead there is nothing warm to reuse; the caller (pool)
    detects that via ``is_alive()``/``is_process_alive()`` and replaces the
    worker, so this raises rather than silently respawning a new process here
    (the runtime is owned by this provider's lifecycle, not recreated in place).

    Reuse is only cheap where the old session actually GOES AWAY, so this is
    gated on ``ACP_BACKENDS_SESSION_EVICTION``. The whole primitive rests on
    the ``old.destroy()`` below reclaiming the previous session's context and
    MCP children on the shared process; on a backend whose teardown does not
    evict, that call returns having freed nothing and each reset leaves one
    more resident session behind. Nothing downstream collects them: such a
    backend is off the eviction path by construction, and a recycle rule that
    measures a narrower scope than where the sessions live never sees the
    growth, so only the age ceiling ever reaps it. Refusing BEFORE creating
    anything is what makes this safe --
    ``WorkerPool.reset`` already treats an exception here as "no cheap path"
    and falls back to a hard ``SessionManager.reset``, which is slower but
    correct for every backend.
    """
    # A plain membership test, with no sentinel handling: kiro's own backend
    # id IS the empty string (``ACP_BACKEND_KIRO = ""``), so an unset
    # ``acp_backend`` is not a value awaiting resolution -- it already reads as
    # the member this set admits. Every other value, including one no harness
    # registered, is refused, which is the fail-closed direction.
    if self.backend not in ACP_BACKENDS_SESSION_EVICTION:
        # Raise before the fresh session/new: the caller's hard-reset
        # fallback is the correct path, and creating a session first would
        # leak the very session this refusal exists to prevent.
        raise AcpError(
            f"backend {self.backend!r} does not evict sessions on teardown, so "
            "warm conversation reuse would accumulate resident sessions -- "
            "use the hard-reset path instead"
        )
    if not self._runtime.is_alive():
        raise AcpProcessDied("Runtime is not alive — cannot start a new conversation")
    old = self._handle
    # Create the fresh session BEFORE destroying the old one so a failure
    # leaves the provider still pointing at a usable handle (no window where
    # self._handle references a terminated session).
    # A member DM's dispatch mount and a channel session's channel ride every
    # session start (``AcpProvider._member_session_key`` / ``_owning_channel_id``),
    # so a chat's fresh conversation carries the ones its cold start passed.
    new_handle = await self._runtime.create_session(
        cwd=self._runtime._work_dir,
        agent=self._runtime._agent or None,
        memory_mode=self.memory_mode,
        kas_memory_mode=self.kas_memory_mode,
        session_key=self._session_key,
        member_session_key=member_session_key,
        channel_id=channel_id,
    )
    # Re-apply the configured non-default model to the fresh session. A new
    # session/new reverts to the agent-config default model, so a warm worker
    # configured with a non-default model (via the cold-start set_model in
    # AcpProvider._start_kiro_runtime_impl, recorded on the old handle) would
    # silently run every reused task on the wrong model without this. Mirrors
    # that cold-start handshake: skip the "auto" sentinel (let kiro pick).
    #
    # If the re-apply FAILS we must NOT commit the fresh handle — a
    # wrong-model session would silently run every subsequent pooled step on
    # the default model. Tear the fresh session down and raise so the caller
    # (WorkerPool) performs its hard-reset fallback and the old, correctly
    # configured handle is not destroyed below.
    prior_model = getattr(old, "model", "")
    if isinstance(prior_model, str) and prior_model and prior_model != DEFAULT_MODEL:
        try:
            await new_handle.set_model(prior_model)
        except Exception as exc:
            logger.warning(
                "new_conversation: failed to re-apply model %s to fresh session; "
                "tearing it down and signalling reset",
                prior_model,
                exc_info=True,
            )
            try:
                await new_handle.destroy()
            except Exception:
                logger.debug(
                    "new_conversation: fresh-session teardown after model "
                    "re-apply failure also failed",
                    exc_info=True,
                )
            raise AcpError(f"failed to re-apply model {prior_model} to fresh session") from exc
    elif isinstance(inherited := getattr(old, "inherited_model", ""), str) and inherited:
        # An unpinned KAS session inherits kiro-cli's chat.defaultModel; the
        # fresh session/new is back on KAS's own default, so re-apply it the
        # same way. Not a pin, so a refusal leaves the default and is not fatal.
        try:
            await new_handle.set_model(inherited, inherit=True)
        except Exception:
            logger.warning(
                "new_conversation: failed to re-apply inherited model %s",
                inherited,
                exc_info=True,
            )
    # Accumulated across the swap: the old session's KAS launch snapshots are
    # removed by this provider's shutdown, like the new one's.
    new_handle.kas_launch_snapshots = [
        *getattr(old, "kas_launch_snapshots", []),
        *getattr(new_handle, "kas_launch_snapshots", []),
    ]
    # The fresh conversation is the same worker's, so its record waits as long as
    # the previous one's did.
    new_handle.delete_delay_secs = getattr(old, "delete_delay_secs", 0.0)
    # Still the session the keepalive route's registry holds, if it was.
    new_handle.keepalive_routed = getattr(old, "keepalive_routed", False) is True
    self._handle = new_handle
    old.mcp_session_report().hand_over(new_handle.mcp_session_report())  # its reader follows
    # The fresh session/new restored nothing: a session/load observation from
    # the old handle would make the next turn skip its history replay
    # (``turn_dispatch_gates``) and run on an empty conversation. Same as
    # ``AcpClient.new_conversation``; cleared only once the swap is committed.
    self._resumed_flag = False
    if getattr(self, "_shared_runtime", False):
        new_handle.chat_shared = True
    # The fresh session launched fresh stubs carrying a fresh token, and
    # nothing has named it: an unnamed token is refused, not resolved from
    # the shared runtime's tree. Claim it now, against the session this
    # provider already serves (empty on a worker no session has claimed yet,
    # where rekey() does the naming instead).
    self.reclaim()
    # Best-effort teardown of the old session on the shared process so its
    # context doesn't linger (RSS growth). Never let cleanup mask success.
    try:
        await old.destroy()
    except Exception:
        logger.debug("new_conversation: old session destroy failed", exc_info=True)
