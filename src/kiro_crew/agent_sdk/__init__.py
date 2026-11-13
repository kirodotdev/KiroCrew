"""The one import surface between application code and an agent backend.

Provider-neutral turn usage and stop-reason vocabulary live here first. The
boundary is enforced by ``scripts/check_agent_sdk_boundary.py`` and
``test/test_agent_sdk_boundary.py``, so application code cannot introduce new
direct dependencies on the backend packages while the rest of the SDK is built.

Which backends this build can serve, and every capability set a harness may
claim, live in :mod:`kiro_crew.agent_sdk.backends`; the per-session record a
consumer asks instead of naming a harness is
:class:`kiro_crew.agent_sdk.capabilities.SessionCapabilities`. What an operator
choosing BETWEEN harnesses is shown -- the same memberships projected as one card
per harness, with no per-harness prose -- is
:mod:`kiro_crew.agent_sdk.backend_cards`. Whether a harness's
tool calls reach the PreToolUse gate is :mod:`kiro_crew.agent_sdk.tool_gate`.

Machine-local backend readiness lives in :mod:`kiro_crew.agent_sdk.backend_install`.
How a harness signs in -- which store holds its entitlement, and the remedy to
print when it does not -- is declared once per harness in
:mod:`kiro_crew.agent_sdk.host_auth`, and the credential floor, the sandbox mask,
the doctor row and the backend panel all project from that one declaration.
Promptless structured command batches live in
:mod:`kiro_crew.agent_sdk.native_commands`; their ACP process lifecycle and
exception translation stay in the driver, and only plain data crosses upward.

Layering::

    consumers        dashboard/  slack/  discord/  telegram/  messaging/
                     session.py  subagent.py  apps/  cli_*.py  workflows/
                            |
                            |  import kiro_crew.agent_sdk for anything
                            |  provider-specific. A file ALREADY in the
                            |  boundary baseline keeps its direct ACP
                            |  imports; a file that is clean today is
                            |  REFUSED a new one by the gate
                            v
                     kiro_crew.agent_sdk          domain types, role protocols,
                                                  capabilities, supervisor
                            |
                            |  resolves drivers through a registry
                            v
                     kiro_crew.agent_sdk.drivers.acp
                     kiro_crew.agent_sdk.drivers.acp_vocab
                                                  the only modules INSIDE this
                                                  package that import
                                                  kiro_crew.acp: the driver
                                                  (call-time imports) and the
                                                  by-value vocabulary it exposes
                            v
                     kiro_crew.acp  (foundation)  wire, dialects, adapters,
                                                  session handles, worker pool

If the gate goes red you introduced a boundary violation: fix the import
direction rather than relaxing the rule, and never add or raise a line in
``.github/agent-sdk-boundary-baseline.txt`` to make it green.

Design of record, including what each later phase moves in here:
``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

# One name, because one name has a consumer: the dashboard handler that serves
# ``GET /api/acp-backends``. The record types and the per-id projection stay inside
# :mod:`kiro_crew.agent_sdk.backend_cards`, which is where the tests about them
# reach for them -- a facade export nobody imports is a promise this boundary would
# have to keep for no one.
from kiro_crew.agent_sdk.backend_cards import card_payload
from kiro_crew.agent_sdk.backend_install import (
    CACHE_TTL_SECONDS,
    COMPONENT_CLAUDE_ACP_ADAPTER,
    COMPONENT_CLAUDE_CODE_CLI,
    COMPONENT_KIRO_CLI,
    INSTALLED,
    MISSING,
    UNKNOWN,
    BackendInstallState,
    clear_probe_cache,
    forget_for_recheck,
    probe_backend,
    probe_backends,
)
from kiro_crew.agent_sdk.backends import (
    CODEX_CLI_ADAPTER_MISSING,
    CODEX_CLI_OVERRIDE_UNUSABLE,
    CODEX_CLI_UNRESOLVED,
)
from kiro_crew.agent_sdk.capabilities import (
    MODEL_NAMESPACE_ACP,
    UNKNOWN_BACKEND_CAPABILITIES,
    SessionCapabilities,
    capabilities_for,
    capabilities_of,
    capture_prompt_timeout,
)
from kiro_crew.agent_sdk.context import (
    CONTEXT_EVENT_AGENT_CHANGED,
    CONTEXT_EVENT_CLEAR,
    CONTEXT_EVENT_COMPACTION,
    CONTEXT_EVENT_COMPLETED,
    CONTEXT_EVENT_TEXT,
    CONTEXT_EVENT_TOOL,
    ContextPromptProvider,
    ContextStreamEvent,
)
from kiro_crew.agent_sdk.drivers.acp import (
    context_provider_of,
    finish_suspended_spawn,
    kas_engine_gap,
    kas_engine_start_verdict,
    kas_engine_unsupported_reason,
    kas_engine_verdict,
    kas_start_was_refused,
)
from kiro_crew.agent_sdk.host_auth import (
    UNKNOWN_AGENT_AUTH,
    AgentAuthDeclaration,
    AgentInteractiveLogin,
    declaration_for,
    entitlement_label,
    signs_in_separately,
)
from kiro_crew.agent_sdk.native_commands import NativeCommandBatch, run_kiro_native_commands

if TYPE_CHECKING:
    from collections.abc import Callable

    from kiro_crew.acp.client_spawn_env import CodexCliResolution
    from kiro_crew.acp.managed_agent_shadow import AgentNameClaim


def fits_tool_result(text: str, limit: int) -> bool:
    """Whether *text* stays within *limit* after the tool-row scrub, so it lands uncut."""
    # Imported lazily: kiro_crew.acp imports back into this package at load time.
    from kiro_crew.acp._dispatch import fits_tool_result as _fits

    return _fits(text, limit)


def tool_row_text(text: str) -> str:
    """*text* as the tool-row scrub leaves it, for a caller that must apply it before serializing."""
    # Imported lazily for the same reason as fits_tool_result.
    from kiro_crew.acp._dispatch import redact_text as _scrub

    return _scrub(text)


def agents_dir_competing_claim(
    agent: str, agents_dir: Path, own_file: Path
) -> AgentNameClaim | None:
    """A competing user-level claim or unverifiable scan, excluding the app's own file."""
    from kiro_crew.acp.managed_agent_shadow import agents_dir_competing_claim as _claim

    return _claim(agent, agents_dir, own_file)


def shell_command_from_tool_params(params: dict) -> str | None:
    """The shell command the tool gate scans in a tool call's *params*, or None."""
    from kiro_crew.acp.types import _command_from_tool_params as _command

    return _command(params)


def published_stderr_tail(
    lines: list[str], sep: str, last: int, redact: Callable[[str], str] | None = None
) -> str:
    """The *last* of a child's stderr *lines* joined with *sep*, or ``""``, or the
    withheld marker when ``acp/stderr_tail.py:safe_stderr_tail`` flags ANY of the
    lines: all of them are judged, so a secret straddling the cut is seen whole."""
    from kiro_crew.acp.stderr_tail import STDERR_WITHHELD, safe_stderr_tail

    judged = safe_stderr_tail(lines, "\n", redact=redact)
    return judged if judged in ("", STDERR_WITHHELD) else sep.join(lines[-last:])


def resolve_codex_cli() -> CodexCliResolution:
    """The ``codex`` CLI the codex-acp adapter would run, or why it cannot be found."""
    # Imported lazily: the ACP spawn module is heavy and imports back into this package.
    from kiro_crew.acp.client_spawn_env import resolve_codex_cli as _resolve

    return _resolve()


def session_start_concurrency() -> int:
    """The configured ``agent.session_start_concurrency`` snapshot (off-loop caller)."""
    from kiro_crew.acp.runtime_start import _resolve_session_start_concurrency as _resolve

    return _resolve()


def take_batch_guard_refusal(client: Any, request_id: Any) -> str | None:
    """The batch write guard's reason for the approval *client* just refused, taken once.

    The SDK surface for a consumer that writes its own audit row when
    ``approve_tool`` returns ``False`` and must tell a guard refusal from a
    security-floor one. Imported lazily (the ACP package imports back into this
    one at load time)."""
    from kiro_crew.acp.batch_guard import take_client_refusal

    return take_client_refusal(client, request_id)


def batch_guard_refusal_outcome(reason: str | None) -> tuple[str, dict[str, str] | None]:
    """The ``(outcome, metadata)`` an audit row should carry for a refused approval:
    the guard's own labels when *reason* is a kept guard reason, the transport-floor
    label otherwise."""
    from kiro_crew.acp.batch_guard import refusal_outcome

    return refusal_outcome(reason)


def mcp_call_arguments(event: Any, server: str, tool: str) -> dict[str, Any]:
    """The arguments of an MCP call to *server*/*tool*, read through the harness
    envelopes (``{tool_id, arguments}``, Codex's ``{server, tool, arguments}``) only
    when the envelope names that same tool. Imported lazily, as above."""
    from kiro_crew.acp.batch_guard import _mcp_arguments

    return _mcp_arguments(event, server, tool)


def watch_agent_turns(provider: object, callback: Callable[[], None] | None) -> bool:
    """Call *callback* when *provider*'s agent starts a turn between prompts; ``None`` stops it.

    False, with nothing registered, when *provider* is not an ACP session or its
    backend starts no such turn (``AcpProvider.watch_agent_turns``).
    """
    from kiro_crew.providers.acp import AcpProvider

    if not isinstance(provider, AcpProvider):
        return False
    return provider.watch_agent_turns(callback)


TURN_STOP_REASON_CANCELLED = "cancelled"
TURN_STOP_REASON_END_TURN = "end_turn"


class AgentTurnUsage(Protocol):
    """Provider-neutral token dimensions reported for one completed turn."""

    input_tokens: int
    output_tokens: int


__all__ = [
    "published_stderr_tail",
    "CODEX_CLI_ADAPTER_MISSING",
    "CODEX_CLI_OVERRIDE_UNUSABLE",
    "CODEX_CLI_UNRESOLVED",
    "resolve_codex_cli",
    "session_start_concurrency",
    "take_batch_guard_refusal",
    "batch_guard_refusal_outcome",
    "mcp_call_arguments",
    "watch_agent_turns",
    "tool_row_text",
    "agents_dir_competing_claim",
    "ContextPromptProvider",
    "ContextStreamEvent",
    "CONTEXT_EVENT_AGENT_CHANGED",
    "CONTEXT_EVENT_CLEAR",
    "CONTEXT_EVENT_COMPACTION",
    "CONTEXT_EVENT_COMPLETED",
    "CONTEXT_EVENT_TEXT",
    "CONTEXT_EVENT_TOOL",
    "context_provider_of",
    "AgentTurnUsage",
    "MODEL_NAMESPACE_ACP",
    "SessionCapabilities",
    "UNKNOWN_BACKEND_CAPABILITIES",
    "capabilities_for",
    "capabilities_of",
    "capture_prompt_timeout",
    "card_payload",
    "UNKNOWN_AGENT_AUTH",
    "AgentAuthDeclaration",
    "AgentInteractiveLogin",
    "declaration_for",
    "entitlement_label",
    "signs_in_separately",
    "CACHE_TTL_SECONDS",
    "COMPONENT_CLAUDE_ACP_ADAPTER",
    "COMPONENT_CLAUDE_CODE_CLI",
    "COMPONENT_KIRO_CLI",
    "INSTALLED",
    "MISSING",
    "UNKNOWN",
    "BackendInstallState",
    "NativeCommandBatch",
    "clear_probe_cache",
    "finish_suspended_spawn",
    "fits_tool_result",
    "forget_for_recheck",
    "kas_engine_gap",
    "kas_engine_start_verdict",
    "kas_engine_unsupported_reason",
    "kas_engine_verdict",
    "kas_start_was_refused",
    "probe_backend",
    "probe_backends",
    "run_kiro_native_commands",
    "TURN_STOP_REASON_CANCELLED",
    "TURN_STOP_REASON_END_TURN",
]
