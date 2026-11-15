"""The goose session contract ``GooseAcpClient`` keeps.

goose resolves its permission mode from ``GOOSE_MODE`` in its environment (seeded by
:mod:`kiro_crew.acp.harness.goose`), reports the mode it resolved in the ``modes``
block of the ``session/new`` and ``session/load`` answers, and reports a later move
as a ``current_mode_update``. This module reads the mode back off both answers,
stops a session that reports leaving the required mode, names a goose release
outside the verified range at the handshake, and hands out the session's MCP array.

Each function here whose first parameter is ``self`` is a ``GooseAcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_session_id``, ``_session_key``, ``_side_read_only``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew import sel as sel_module
from kiro_crew.acp.harness import goose as goose_harness
from kiro_crew.acp.types import JsonRpcMessage

if TYPE_CHECKING:
    from kiro_crew.acp.client import GooseAcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


# goose's VERIFIED RANGE (the note beside ``ACP_BACKEND_GOOSE`` in
# ``agent_sdk/backends.py``) is 1.50.x. Of the three wire facts it names, the
# ``current_mode_update`` emission the mid-session tripwire rests on is the one that
# fails OPEN: a release that stopped emitting it would narrow enforcement back to the
# open/restore read-back with no frame saying so. ``agentInfo.version`` on the
# ``initialize`` reply is the one signal before the first prompt, so a release outside
# the range is named there. The trailing dot keeps ``1.500.x`` outside it. A warning,
# not a refusal: the two closed facts still hold on any release, and a newer goose is
# the common case.
GOOSE_VERIFIED_VERSION_PREFIX = "1.50."
# Versions already named this process, so a gateway on a newer goose says so once,
# not on every session.
_goose_versions_noted: set[str] = set()


def _verify_goose_routing(self: GooseAcpClient, resp: dict) -> None:
    """Read this harness's OWN resolved mode back off the session response.

    This is the half that makes the routing VERIFIED rather than seeded, and on
    this harness it costs no second child: the mode goose resolved is reported in
    the ``modes`` block of the very response that opens or restores the session,
    so what comes back is the mode the session will actually use and it arrives on
    the session's own connection. A precedence change in a future release
    therefore surfaces as a refusal here rather than as a session that silently
    stops asking.

    Called on BOTH paths, and the restore path is the one that needs it most: the
    environment seed governs a session goose CREATES, but a session goose RESTORES
    carries the mode it was last left in, and ``session/set_mode`` accepts the
    auto-approving one. So a session resumed after any actor moved it would come
    back permissive, and only this read can tell.

    What it does NOT establish is that the harness HONOURS the mode per tool call;
    that is the harness's own contract and no client-side read can prove it. The
    scope is the precondition, and the frame corpus carries the observation of the
    emission.

    Raises ``AcpToolGateUnroutable`` when the required mode is not in force.
    """
    # The read and the refusal are the goose harness's, shared with the
    # shared-runtime driver (``GooseHarness.after_session_start``). An omitted
    # modes block is INDETERMINATE rather than a pass: this harness always
    # reports one, so its absence means the response is not the shape this read
    # was verified against and no claim can be made from it. The refusal is the
    # ACP-layer type ``ensure_ready`` catches, so a failed spawn is reaped.
    issue = goose_harness.session_mode_issue(resp)
    if issue:
        goose_harness.refuse_routing(issue)


def _note_goose_version(self: GooseAcpClient) -> None:
    """Log a goose release outside the verified range, at the handshake.

    Complements :meth:`_tripwire_goose_mode` the way ``_note_pi_adapter_version``
    complements the pi tripwire. The two wire facts that fail closed (the mode
    read-back, the ``_meta.goose.toolCall`` identity) announce their own loss as
    refusals; the ``current_mode_update`` emission the tripwire fires on fails open,
    so its loss on a newer release would be silent. This names the release the
    session actually RUNS -- ``agentInfo.version`` off ``initialize``, not the file on
    disk -- once per process per version, so the operator knows where to look if a
    session that left the required mode is later found still running.
    """
    version = self.agent_version
    if version.startswith(GOOSE_VERIFIED_VERSION_PREFIX) or version in _goose_versions_noted:
        return
    _goose_versions_noted.add(version)
    logger.warning(
        "goose reports version %r; the routing contract (mode read-back, "
        "_meta.goose.toolCall identity, mid-session current_mode_update) was verified "
        "on %sx. The read-back and the identity check still refuse on their own; the "
        "mid-session mode tripwire rests on an emission this release may not make.",
        version or "unknown",
        GOOSE_VERIFIED_VERSION_PREFIX,
    )


async def _tripwire_goose_mode(self: GooseAcpClient, msg: JsonRpcMessage) -> None:
    """Refuse to continue a goose session that reports leaving the required mode.

    The read-back on ``session/new`` and ``session/load`` proves the mode the session
    STARTED in. It does not hold the session there: ``session/set_mode`` accepts the
    auto-approving mode, goose's own slash commands can move it, and 1.50.1 reports
    every such move as a ``current_mode_update`` on the session's own connection --
    captured live in ``session-load-live.jsonl``, where one names ``auto`` after a
    ``set_mode``. In ``auto`` goose asks about nothing, so no permission request
    reaches Crew to refuse; the notification is the last frame Crew sees before the
    harness starts saying yes on its own. So it is treated exactly as the read-back
    treats the same mode on open: the harness is stopped and the turn fails with the
    gate's own reason. Every frame that is not this one, or that names the required
    mode (goose re-emits the current mode on load), is a no-op.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    issue, observed = goose_harness.mode_update_issue(params.get("update"))
    if not issue:
        return
    logger.error(
        "goose routing: session reported mode %r mid-session, which is not the "
        "required one; stopping the harness before it approves on its own [session=%s]",
        observed,
        self._session_id,
    )
    try:
        sel_module.sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name="session/set_mode",
            tool_kind="other",
            outcome="denied",
            metadata={
                "reason": "goose_mode_tripwire",
                "backend": self.backend,
                "mode": observed,
            },
        )
    except Exception:  # pragma: no cover - audit is best-effort
        logger.debug("goose routing: audit of the mode tripwire failed", exc_info=True)
    await self._kill_process(force=True)
    goose_harness.refuse_routing(issue)


def _goose_session_mcp_servers(self: GooseAcpClient) -> list:
    """MCP server array passed to a goose ``session/new`` / ``session/load``.

    The goose twin of ``OpencodeAcpClient._opencode_session_mcp_servers``, and it must stay
    non-empty: ``goose acp`` reads no ``~/.kiro/agents/<name>.json``, so without
    this a goose session has none of Crew's tools (no ``spawn_run``,
    ``cron_add``, ``send_message``). ``_pooled_mcp_servers`` answers ``[]``
    for backends in ``MIRRORS``, so pooled stubs come from here too.

    No transport filter: goose 1.50.1 mounts the stdio element (measured round
    trip through ``initialize``, ``notifications/initialized``, ``tools/list``
    and ``tools/call``).

    An element whose command cannot start does not fail ``session/new``; it is
    dropped, and the drop is unobservable: goose sends no per-server MCP
    notification (so ``_mcp_timeout_progress`` has nothing to read), and
    ``available_commands_update`` is identical either way.
    ``test/fixtures/acp_frames/goose/mcp-stdio-mount-live.jsonl`` pins the
    round trip so a release that stops mounting goes red in the corpus. This is
    accepted because a dropped mount loses functionality, not a gate: permission
    routing on this harness rides on the seeded mode and its read-back, not on
    MCP, so every tool call still reaches ``HookManager.on_tool_call``. The
    follow-up is a shared broker-side check that the child sends
    ``initialize`` within N seconds of ``session/new`` for every member of
    ``ACP_BACKENDS_SESSION_MCP_ARRAY``.

    The translation lives in :mod:`kiro_crew.providers.mirrors.goose`. Keep this
    method as the seam: an edition may override it, and a capability-set test
    at the call site would skip that override. In-memory only: the spawn path
    warms ``_session_mcp_cache`` off the loop (harness-parity H13).

    The count is logged here because it is the only greppable fact a degraded
    goose session leaves; it says what was offered, not what mounted.
    """
    if getattr(self, "_side_read_only", False):
        # A Side Chat session mounts no MCP server, as on claude: READ_ONLY
        # refuses every MCP call, and a server named after goose's builtin
        # extension would replace its read tools.
        return []
    servers = self._session_mcp_servers()
    logger.info(
        "goose session MCP: %d server(s) placed on the session array; this harness "
        "reports neither a mount nor a drop per server [session=%s]",
        len(servers),
        self._session_id,
    )
    return servers


# The ``AcpClient`` hooks ``GooseAcpClient`` fills, each the goose step above.


def _harness_session_mcp_servers(self: GooseAcpClient) -> list:
    """The servers a goose session's MCP array carries ahead of the shared entries."""
    return self._goose_session_mcp_servers()


def _verify_session_mode(self: GooseAcpClient, resp: dict) -> None:
    """The mode read-back the session start runs on a ``session/new`` or restore answer."""
    self._verify_goose_routing(resp)


async def _tripwire_session_mode(self: GooseAcpClient, msg: JsonRpcMessage) -> None:
    """The mid-session mode tripwire the frame path runs on every session update."""
    await self._tripwire_goose_mode(msg)


def _note_harness_adapter_version(self: GooseAcpClient) -> None:
    """The handshake's note of a goose release outside the verified range."""
    self._note_goose_version()
