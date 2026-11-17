"""The kiro-cli log reader tool: what it advertises and what it does.

``schemas()`` returns the ADVERTISEMENT half; ``HANDLERS`` maps the name to the
function that runs it. Both halves live here so the contract and behavior are
read together, and ``test_mcp_tool_registry`` fails if one arrives without the
other (see ``skills.py`` — this module follows the same template).

WHY THIS TOOL EXISTS: kiro-cli's identity store is fenced (it also holds SSO
tokens), and the command gate is a deliberately coarse, verb-independent path
matcher — so the agent driving kiro-cli has no sanctioned way to read kiro-cli's
own logs when the backend rejects a turn. This is that
sanctioned channel: ONE reviewed code path that reads only kiro-cli's mcp/lsp
PROTOCOL logs and runs every byte through the shared redaction stack
(``redact_credentials`` / ``redact_exfiltration_urls`` plus the diagnostics
collector's ``_EXTRA_REDACTIONS`` for the bearer/Authorization/mc_token shapes
those two miss) before returning it. It does NOT widen the gate and does NOT
take a path carve-out on the fence: redaction is what makes returning the bytes
safe.

SCOPE IS THE OTHER HALF OF THAT SAFETY. ``kiro-chat.log`` and the session
transcripts are deliberately NOT read. Neither is scoped to the calling
session: the chat log is one shared host file or, where Kiro Crew pins it into
a kiro-cli's scratch dir, one file per PROCESS, and one process can serve
several sessions. The redaction stack is a CREDENTIAL pass that does not narrow
prose — an agent-callable read would hand session A's private conversation to
session B.
The mcp/lsp protocol logs are what actually explain a rejected turn, so the
tool stays there. See ``diagnostics.read_kiro_cli_logs``.

Handlers reach shared plumbing as attributes of ``mcp_core`` (``mcp_core.sel``,
``mcp_core.require_strict_session_key``) so a test that rebinds one still intercepts —
an attribute lookup resolves at CALL time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew import diagnostics, mcp_core
from kiro_crew.mcp_tools.catalog.logs import (  # noqa: F401
    schemas,
)
from kiro_crew.validation import KIRO_CLI_LOGS_SCHEMA, validate_tool_args


def kiro_cli_logs(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, KIRO_CLI_LOGS_SCHEMA)
    tail = args.get("tail")
    if tail is not None:
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = None
    since = str(args.get("since", "") or "").strip() or None

    session_key, refusal = mcp_core.require_strict_session_key(
        "Error: kiro-cli logs require an established session."
    )
    if refusal:
        return refusal

    try:
        # diagnostics.read_kiro_cli_logs reads only log files and scrubs every
        # byte through the shared redaction stack before returning.
        out = diagnostics.read_kiro_cli_logs(tail=tail, since=since)
    except Exception as exc:  # pragma: no cover — defensive
        mcp_core.sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="kiro_cli_logs",
            tool_kind="read",
            outcome="error",
            metadata={"error": type(exc).__name__},
        )
        # "Error:" prefix is load-bearing: the shared call_tool_with_logging
        # wrapper classifies a result by result.startswith("Error:").
        return f"Error: kiro_cli_logs failed: {type(exc).__name__}: {exc}"

    mcp_core.sel().log_tool_invocation(
        session_key=session_key,
        source="mcp",
        tool_name="kiro_cli_logs",
        tool_kind="read",
        outcome="success",
        metadata={"tail": tail, "since_set": since is not None},
    )
    return out


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "kiro_cli_logs": kiro_cli_logs,
}
