"""The opencode session contract ``OpencodeAcpClient`` keeps.

opencode can ask permission for a shell call before it sends the update that carries
the command, can open a session on its own hosted model when the configured one is not
served, and reads no agent spec of Crew's, so its session MCP array is the only channel
onto its tools. This module holds the session's answer to each: it lets the
command-bearing update reach the security cache before the permission, refuses a
session that fell back to the hosted model before its first prompt, and hands out the
session's MCP array.

Each function here whose first parameter is ``self`` is an ``OpencodeAcpClient`` method:
the class binds it by name, so it reads and writes the session's own state on the
instance, and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_buffer``, ``_resolved_model_id``, ``_tool_call_is_shell``,
``_tool_call_params``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kiro_crew.acp.client_timeouts import _OPENCODE_PERMISSION_REFINEMENT_TIMEOUT
from kiro_crew.acp.no_model_provider import (
    AcpNoModelProvider,
    is_opencode_hosted_fallback,
    opencode_hosted_fallback_message,
)
from kiro_crew.acp.types import METHOD_SESSION_UPDATE, JsonRpcMessage
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import OpencodeAcpClient


async def _defer_shell_permission(
    self: OpencodeAcpClient, msg: JsonRpcMessage, remaining: float
) -> bool:
    """Queue a matching command update before its OpenCode shell permission.

    The turn loop's ``_defer_shell_permission`` hook itself, so the frame it reads is
    read under the prompt loop's claim on stdout.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    tool_call = params.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or self._tool_call_is_shell.get(tool_call_id) is not True:
        return False
    cached = self._tool_call_params.get(tool_call_id, {})
    if isinstance(cached, dict) and cached.get("command"):
        return False

    # OpenCode can ask permission before its command-bearing update. Read
    # only the next frame to preserve unrelated event order. The request's
    # own rawInput must never fill the trusted security cache.
    following = await self._read_message(
        timeout=min(_OPENCODE_PERMISSION_REFINEMENT_TIMEOUT, remaining)
    )
    if following is None:
        return False

    follow_params = following.params if isinstance(following.params, dict) else {}
    update = follow_params.get("update")
    update = update if isinstance(update, dict) else {}
    raw_input = update.get("rawInput")
    buffer_capacity = self._buffer.maxlen
    if not (
        following.is_method(METHOD_SESSION_UPDATE)
        and isinstance(params.get("sessionId"), str)
        and follow_params.get("sessionId") == params.get("sessionId")
        and update.get("sessionUpdate") == "tool_call_update"
        and update.get("toolCallId") == tool_call_id
        and update.get("status") == "in_progress"
        and isinstance(raw_input, dict)
        and isinstance(raw_input.get("command"), str)
        and raw_input["command"]
        and not update.get("content")
        and not update.get("rawOutput")
        and (buffer_capacity is None or len(self._buffer) <= buffer_capacity - 2)
    ):
        self._buffer.appendleft(following)
        return False

    # The normal dispatcher caches the command before building permission.
    self._buffer.appendleft(msg)
    self._buffer.appendleft(following)
    return True


def _refuse_opencode_hosted_fallback(
    current: str, advertised: Sequence[str], config_model: str
) -> None:
    """Raise :class:`AcpNoModelProvider` when opencode fell back to its hosted model.

    Before the first prompt; see :func:`is_opencode_hosted_fallback` for the test.
    """
    if not is_opencode_hosted_fallback(current, advertised, config_model):
        return
    shown, _ = redact_exfiltration_urls(current)
    shown, _ = redact_credentials(shown)
    raise AcpNoModelProvider(opencode_hosted_fallback_message(shown))


def _opencode_session_mcp_servers(self: OpencodeAcpClient) -> list:
    """MCP server array passed to an opencode ``session/new`` / ``session/load``.

    The opencode twin of
    :meth:`kiro_crew.acp.harness.codex.CodexHarness.session_mcp_servers`, and it
    must stay non-empty: ``opencode acp`` reads no ``~/.kiro/agents/<name>.json``,
    so without this a session has none of Crew's tools (no ``spawn_run``,
    ``cron_add``, ``send_message``).

    No transport filter, unlike codex: ``opencode acp`` accepts ``http`` and
    ``sse`` elements, so ``drop_unadvertised_transports`` would only remove
    servers it would mount.
    ``test/test_opencode_session_mcp.py::test_real_opencode_acp_accepts_the_crew_stdio_element``
    pins this against a real ``opencode acp``.

    Unlike codex, a malformed element (no ``command``, or a non-array ``env``)
    fails the WHOLE ``session/new`` with ``-32602``.
    ``acp.session_mcp.acp_server_element`` returns ``None`` for an entry with
    neither ``command`` nor ``url`` and stringifies what it cannot type, so one
    bad spec line costs only that server.

    The translation lives in :mod:`kiro_crew.providers.mirrors.opencode`. Keep
    this method as the seam: an edition may override it, and a capability-set
    test at the call site would skip that override. In-memory only: the spawn
    path warms ``_session_mcp_cache`` off the loop (harness-parity H13).
    """
    return self._session_mcp_servers()


# The ``AcpClient`` hooks ``OpencodeAcpClient`` fills, each the opencode step above.


def _harness_session_mcp_servers(self: OpencodeAcpClient) -> list:
    """The servers an opencode session's MCP array carries ahead of the shared entries."""
    return self._opencode_session_mcp_servers()


def _refuse_harness_default_model(self: OpencodeAcpClient) -> None:
    """The refusal of a session left on opencode's hosted fall-back model."""
    _refuse_opencode_hosted_fallback(
        self._resolved_model_id or "",
        self._advertised_model_ids(),
        self._harness_read_back().config_model,
    )
