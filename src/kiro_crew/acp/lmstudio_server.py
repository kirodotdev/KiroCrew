"""Owned, local-only ACP server for LM Studio — v2 WITH tool execution.

The KiroCrew gateway remains the ACP *client*.  This module is the small
adapter process it spawns for ``agent.acp_backend = "lmstudio"``.  Other than
the shared MCP bridge (acp/mcp_bridge.py, same as the OpenRouter harness —
H6: an adapter that reads the agent spec belongs in
ACP_BACKENDS_SESSION_MCP_ARRAY) it has no network route other than a loopback
OpenAI-compatible LM Studio endpoint.  That intentionally keeps it suitable
for bounded local work while preserving the caller's normal sandbox and
lifecycle controls.

v1 was chat-only: it never sent ``tools`` to LM Studio, and raised
"empty completion" whenever the loaded model correctly tried to call a tool —
so local sessions could talk but never act, and every real task died at the
first tool call.  v2 runs a real loop: prompt -> model -> (tool_calls?
execute locally -> feed results back) -> final text, with the same budgets as
the OpenRouter adapter (200 steps / 200 tool calls / 2h wall clock, all
env-overridable).

If the loaded model does not support tool calling (LM Studio rejects the
``tools`` parameter), the adapter transparently falls back to chat-only mode
for that session instead of erroring — a non-tool model still answers, it just
cannot act.

It is stdlib-only because it runs from the installed package under the same
interpreter as KiroCrew; installing a second Python runtime would make the
local path less reliable than the Kiro CLI dependency it replaces.

Companion modules
-----------------
This adapter is one half of a two-part direct-ACP story: an in-process agent
that speaks ACP over stdio and drives an OpenAI-compatible HTTP model.  Three
pieces of that story are provider-neutral and therefore shared rather than
copied, so a fix in one cannot silently diverge from the other:

* ``kiro_crew.acp.direct_toolkit`` — the generic OpenAI-compatible plumbing:
  the streaming tool runner (``iter_tool``), the dependency-free token
  estimator (``estimate_message_tokens``), the usage coerter
  (``usage_tokens``) and the history compactor (``compact_history``).
* ``kiro_crew.acp.mcp_bridge`` — bridging ``session/new`` mcpServers into model
  tools, with the identity and per-session policy checks that keep an MCP
  scope from widening.
* ``kiro_crew.acp.stdio_permission`` / ``kiro_crew.acp.tool_fs`` — the
  permission relay back to the KiroCrew client and the path-normalized,
  symlink-pinned file operations.

Each must be present in the tree (or an equivalent supplied) for this module
to import: they are named here rather than inlined so that the transport stays
one implementation shared by every local adapter instead of N drifting copies.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Iterator

from kiro_crew.acp import mcp_bridge
from kiro_crew.acp.direct_toolkit import (
    MAX_MODEL_CONTEXT_WINDOW,
    MAX_MODEL_NAME_LENGTH,
    STOP_REASON_LOCAL_LIMIT,
    UPDATE_AGENT_STATUS,
    UPDATE_CONTEXT_WINDOW,
    bounded_model_id,
    bounded_model_text,
    compact_history,
    estimate_message_tokens,
    iter_tool,
    usage_tokens,
)
from kiro_crew.acp.lmstudio_models import (
    MAX_LOADED_MODEL_INSTANCES,
    MAX_MODEL_CATALOG_ENTRIES,
)
from kiro_crew.acp.mcp_bridge import (
    iter_mcp_tool,
    mcp_identity_raw_input,
    mcp_tool_policy_allows,
)
from kiro_crew.acp.stdio_permission import make_permission_decider
from kiro_crew.acp.tool_fs import (
    normalize_tool_input_paths,
    read_text_pinned,
    resolve_tool_path,
    write_text_pinned,
)
from kiro_crew.acp.types import (
    UPDATE_USAGE,
)
from kiro_crew.sandbox import run_limited, sandboxed_spawn_argv

_DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
logger = logging.getLogger(__name__)
_PROTOCOL_VERSION = 1
# Local models (e.g. a 27B MLX on a Mac) can take minutes for long
# generations; the old 120s cap killed healthy turns mid-completion.
_REQUEST_TIMEOUT_SECONDS = 600
# LM Studio can restart its local HTTP server while changing or recovering a
# model. A completion already in flight then ends with ``RemoteDisconnected``.
# Probe readiness for at most 15.5 seconds, then replay that completion once.
# A single replay avoids multiplying a long local inference while still
# recovering from the observed eight-second server restart.
_RECOVERY_PROBE_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
_RECOVERY_PROBE_TIMEOUT_SECONDS = 2

# --- v2 budgets (mirror the OpenRouter adapter) -----------------------------
_MAX_STEPS = int(os.environ.get("LMSTUDIO_MAX_STEPS", "200"))
_MAX_TOOL_CALLS = int(os.environ.get("LMSTUDIO_MAX_TOOL_CALLS", "200"))
_TURN_BUDGET_SECONDS = int(os.environ.get("LMSTUDIO_TURN_BUDGET_SECS", "7200"))
_MAX_TOOL_OUTPUT = 16000
#: Bound ONE local generation. LM Studio's own default leaves ``max_tokens``
#: unset, so a local model can emit unbounded output (slow, and it can wrap a
#: whole window of the user's context into one turn). The sibling
#: OpenAI-compatible adapter bounds the same way; local models get a smaller
#: ceiling because their prefill/decode is orders of magnitude slower.
_MAX_MODEL_OUTPUT_TOKENS = max(
    8192,
    min(int(os.environ.get("LMSTUDIO_MAX_OUTPUT_TOKENS", "32768")), 131072),
)
#: Hard ceiling for a PER-MODEL output row (see the sibling adapter's note).
_MAX_OUTPUT_HARD_CEILING = 131072


def _max_output_ceiling(model: str | None) -> int:
    """This model's own output ceiling, else the adapter default.

    A per-model row (``model_max_output.json``) wins when present, clamped by the
    hard ceiling; otherwise the adapter's own default stands.
    """
    from kiro_crew import model_registry

    per_model = model_registry.model_max_output(model)
    if per_model:
        return min(per_model, _MAX_OUTPUT_HARD_CEILING)
    return _MAX_MODEL_OUTPUT_TOKENS


# Local-model prefill is much slower than a hosted 1M-context service, so the
# request is bounded — but the bound has to be one a real turn can live inside.
# ``0`` (the default, and what Settings ships) means AUTOMATIC: the bound is
# derived from the model's OWN context window, which is the only ceiling that
# cannot refuse a request the model could have served. An explicit positive
# value is the operator's own prefill-time ceiling and is honoured.
#
# Measured reason for the change: the old fixed 32768 refused every turn on a
# rich agent (109 tools = 44,667 estimated schema tokens) and then, once the
# schemas alone were fitted, still refused a RESUMED session whose injected
# history exceeded the ~8k left over ("oversized injected context has no safe
# session-context wrapper") — on a model whose window is 1,048,576.
_LOCAL_MODEL_PROMPT_BUDGET_TOKENS = int(os.environ.get("LMSTUDIO_PROMPT_BUDGET_TOKENS", "0"))

#: Room a RAISED target leaves for the user's request and its framing, on top of
#: the mandatory schema cost. Only used when the mounted servers' tool schemas
#: alone exceed the configured target and the model's own window can pay for it
#: (see :func:`_compact_local_prompt`).
_LOCAL_SCHEMA_HEADROOM_TOKENS = 8192
_CURRENT_USER_REQUEST_MARKER = re.compile(
    r"\[CURRENT USER REQUEST\s+(?:--|—)\s+respond to this\]\s*"
)
_SESSION_CONTEXT_OPEN_MARKER = re.compile(
    r"\[SESSION CONTEXT\s+—\s+background reference only, NOT a task to act on\.\s*"
    r"This is your memory, lessons, and conversation history from prior sessions\.\s*"
    r"Use it to stay consistent but ONLY respond to the CURRENT USER REQUEST below\.\]\s*",
    re.IGNORECASE,
)
_SESSION_CONTEXT_CLOSE_MARKER = re.compile(r"\[\s*END\s+OF\s+SESSION\s+CONTEXT\s*\]", re.IGNORECASE)
_CRITICAL_RULES_BLOCK = re.compile(
    r"\[CRITICAL RULES\s*[—-]\s*always follow these\].*?\[END CRITICAL RULES\]\s*",
    re.DOTALL,
)
_COMPACTED_CONTEXT_BOUNDARY_MARKERS = re.compile(
    r"\[\s*(?:END\s+OF\s+SESSION\s+CONTEXT|"
    r"CURRENT\s+USER\s+REQUEST\s*(?:--|—)\s*respond\s+to\s+this)\s*\]",
    re.IGNORECASE,
)
_LOCAL_CONTEXT_OMISSION_MARKER = (
    "[Earlier Kiro Crew context omitted to fit the local-model prompt budget]\n"
)

# Prompt size (estimated tokens, request including tool schemas) at or above
# which a model round-trip is announced on the ACP status channel before the
# blocking POST. Calibrated on the measured incident: LM Studio logged
# "Prompt cache restore: cached_tokens=0 uncached_tokens=80128" and the model
# produced its first output 121 seconds after the user's message -- with the
# user seeing nothing at all in between (they closed the tab at 119s, three
# seconds before the answer existed). Subtracting that run's ~11-15s JIT model
# load, prompt processing ran at roughly 730 tok/s, so the ~20k threshold below
# is the point where the read is tens of seconds rather than a few -- below it a
# status line would be noise, and above it the user is owed one.
_WAIT_STATUS_MIN_PROMPT_TOKENS = int(os.environ.get("LMSTUDIO_WAIT_STATUS_MIN_TOKENS", "20000"))

_AGENT_SYSTEM_PROMPT = (
    "You are Kiro, an autonomous agent running inside KiroCrew on the user's Mac, "
    "served by a local LM Studio model. You have REAL tools: bash (run shell "
    "commands through a safety screen — there is no shell access without passing "
    "that screen, which refuses destructive commands), read_file, write_file. "
    "Rules: (1) To do anything - inspect files, run code, check "
    "status - call the tool; never print a command as text instead of calling it. "
    "(2) Keep working tool-call by tool-call until the request is fully handled, "
    "but use the FEWEST tool calls that satisfy it: a greeting or a plain "
    "question needs a direct answer, not tools. Never run an exploratory "
    "status/list/audit survey (listing directories, reading workspace "
    "metadata, or calling cron/workflow/artifact/ledger list tools) unless the "
    "user explicitly asked for it. "
    "(3) When done, reply with a concise final answer and no tool call. "
    "(4) Never run destructive commands. Additional tools from connected "
    "MCP servers may also appear in your tool list; call them the same way."
)

_TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the session working directory; returns stdout/stderr and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_secs": {
                        "type": "integer",
                        "description": "Optional, default 120, max 600",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file (first 200KB).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
]

_KIND = {"bash": "execute", "read_file": "read", "write_file": "edit"}
_PROVIDER_SECRET_ENV = {
    "DEEPSEEK_API_KEY",
    "LMSTUDIO_API_KEY",
    "LM_STUDIO_API_KEY",
    "OPENROUTER_API_KEY",
}


def _tool_env() -> dict[str, str]:
    """Child-tool environment with direct-provider credentials removed."""
    return {key: value for key, value in os.environ.items() if key not in _PROVIDER_SECRET_ENV}


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-lc", command]


_DANGER_PATTERNS = [
    re.compile(r"\brm\s+[^;|&]*\s-\w*r\w*f", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\b", re.IGNORECASE),
    re.compile(r"\bmkfs\b|\bdiskutil\s+erase\b|\bshutdown\b|\breboot\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+table\b|\btruncate\s+table\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{.*\};:"),
]


class LmStudioProtocolError(ValueError):
    """A configuration or wire value that must fail before any HTTP request."""


class LmStudioTransportError(LmStudioProtocolError):
    """A sanitized local transport failure, optionally safe to retry once."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LmStudioToolsUnsupported(LmStudioProtocolError):
    """LM Studio explicitly rejected the request's tool-calling fields."""


class _LmStudioCancelled(Exception):
    """Internal signal that stops replay after an ACP cancellation."""


def _tools_rejected(response_body: bytes) -> bool:
    """Return whether an HTTP error explicitly says tool calling is unsupported."""
    text = response_body[:65_536].decode("utf-8", errors="replace").lower()
    return bool(
        re.search(
            r"\btool(?: calling| use)\s+(?:is |are )?"
            r"(?:not supported|unsupported|unavailable)\b",
            text,
        )
        or re.search(
            r"\b(?:model|loaded model).{0,64}"
            r"(?:does not|doesn't|cannot|can't)\s+support\s+"
            r"(?:tool calling|tool use|tools?)\b",
            text,
        )
        or re.search(r"\bmodel.{0,64}not trained (?:for|to use) tools?\b", text)
    )


def _base_url(value: str | None) -> str:
    """Return a canonical loopback OpenAI-compatible base URL or raise.

    The adapter never accepts a remote endpoint.  A compromised config must not
    turn a supposedly local model selection into arbitrary outbound HTTP.
    ``localhost`` is intentionally not accepted because it is name-resolution
    dependent; use an explicit loopback address instead.
    """
    raw = (value or _DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise LmStudioProtocolError("LM Studio endpoint must be an http loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LmStudioProtocolError("LM Studio endpoint must not include credentials or query data")
    if parsed.path not in ("", "/v1"):
        raise LmStudioProtocolError("LM Studio endpoint path must be /v1")
    return f"{raw if parsed.path else raw + '/v1'}"


def _response(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _prompt_text(blocks: object) -> str:
    """Extract ACP text blocks without treating resources as executable input."""
    if not isinstance(blocks, list):
        raise LmStudioProtocolError("session/prompt requires a prompt block list")
    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    text = "".join(chunks).strip()
    if not text:
        raise LmStudioProtocolError("session/prompt contains no text")
    return text


def _content_text(value: object) -> str:
    """Normalise common OpenAI-compatible response content shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "".join(chunks)
    return ""


def _local_prefill_target(context_window: int) -> int:
    """The prefill target for a local request, before the schema-fit raise.

    An explicit ``LMSTUDIO_PROMPT_BUDGET_TOKENS`` (Settings → Agent → Local-model
    prefill budget) is the operator's own ceiling for prefill TIME, so it is
    honoured as written. Unset (``0``) means AUTOMATIC: everything the model can
    actually take, less the reply's reserve — the same ceiling
    :func:`_compact_local_prompt` then applies, so ``auto`` never refuses a
    request the model could have served. This is not a licence to run every turn
    at the window's edge: the SESSION-level autocompact still compacts at its own
    mark, so a conversation that grows is summarised long before here. Both paths
    are floored at 1,024 so a pathological window cannot produce a zero budget.
    """
    if _LOCAL_MODEL_PROMPT_BUDGET_TOKENS > 0:
        return max(1024, _LOCAL_MODEL_PROMPT_BUDGET_TOKENS)
    if context_window <= 0:
        return 1024
    return max(1024, context_window - 8192 - 2048)


def _compact_local_prompt(
    messages: list[dict[str, Any]],
    context_window: int,
    request_tools: list[dict[str, Any]],
    current_user: dict[str, Any] | None,
) -> tuple[bool, int, str | None]:
    """Fit history and injected context inside the local prefill target.

    Kiro Crew appends the user's actual turn after an explicit current-request
    marker. When older history compaction is insufficient, remove only injected
    context before that marker, retaining the newest context tail and the full
    user request. An unmarked request is never truncated: it fails with a clear
    refusal if it cannot fit.

    The target is :func:`_local_prefill_target` — the operator's value when one is
    set, otherwise a fraction of the model's own window — raised to fit the
    mounted servers' schemas whenever the window can pay for them, because those
    schemas are a cost the caller cannot shorten.
    """
    configured_target = _local_prefill_target(context_window)
    physical_target = context_window - 8192 - 2048
    if physical_target < 1024:
        return False, 0, "model context leaves less than 1,024 estimated input tokens"
    target = min(configured_target, physical_target)
    tool_tokens = estimate_message_tokens({"tools": request_tools})
    # The mounted servers' schemas are a MANDATORY cost the caller cannot
    # shorten, so a target below them refuses EVERY turn on any agent with a
    # rich tool surface. Measured on a real agent: 109 tools from 4 servers =
    # 44,667 estimated tokens against the 32,768 default -- the local path was
    # unusable there, and the refusal named an env var the packaged app gives
    # the operator no way to set. Raise the target to FIT the schemas while the
    # model's own window can pay for them, and keep the configured value as the
    # prefill-time FLOOR: what the refusal protects is "never silently prune a
    # schema", and that still holds -- nothing below removes a tool.
    if tool_tokens >= target:
        # Raised to the model's OWN ceiling, not merely to fit the schemas: a Crew
        # session also carries injected context (rules, memory, skills) that is not
        # the user's message, and a target leaving a token or two for it refuses the
        # very turn this bound exists to serve. Reached only when the schemas do not
        # fit the configured target at all -- i.e. when that value cannot serve this
        # session -- so an operator whose number DOES fit keeps it exactly.
        if physical_target > tool_tokens:
            target = physical_target
        else:
            raised = tool_tokens + _LOCAL_SCHEMA_HEADROOM_TOKENS
            if raised <= physical_target:
                target = raised
    message_budget = target - tool_tokens
    if message_budget < 1:
        return False, tool_tokens, "required tool schemas exceed the local prompt target"

    reserve_tokens = 8192 + tool_tokens
    compact_history(
        messages,
        context_window,
        reserve_tokens=reserve_tokens,
        preserve_from=current_user,
        budget_tokens=message_budget,
    )
    message_tokens = sum(estimate_message_tokens(message) for message in messages)
    if message_tokens + tool_tokens <= target:
        return True, tool_tokens, None
    if current_user is None or not isinstance(current_user.get("content"), str):
        return False, tool_tokens, "current request cannot be separated from injected context"

    content = current_user["content"]
    request_markers = list(_CURRENT_USER_REQUEST_MARKER.finditer(content))
    if not request_markers:
        return False, tool_tokens, "current request has no safe context boundary to compact"

    # Kiro Crew's assembled turn is one ACP user message, so retain the LAST
    # trusted request boundary. This also fails safely if malformed context
    # contains a forged earlier copy of the public marker.
    marker = request_markers[-1]
    protected_request = content[marker.start() :]
    context_prefix = content[: marker.start()]
    wrapper_open = _SESSION_CONTEXT_OPEN_MARKER.search(context_prefix)
    if wrapper_open is None:
        return False, tool_tokens, "oversized injected context has no safe session-context wrapper"
    wrapper_closes = list(
        _SESSION_CONTEXT_CLOSE_MARKER.finditer(context_prefix, wrapper_open.end())
    )
    if not wrapper_closes:
        return (
            False,
            tool_tokens,
            "oversized injected context has no safe session-context closing marker",
        )
    wrapper_close = wrapper_closes[-1]

    # Preserve the full trusted framing and Kiro Crew critical-rules block.
    # Compact only the untrusted body inside the existing background wrapper;
    # wrapping an arbitrary suffix could otherwise turn transcript text into
    # apparently authoritative instructions after its opener was trimmed.
    body = context_prefix[wrapper_open.end() : wrapper_close.start()]
    critical_rules = ""
    critical_match = _CRITICAL_RULES_BLOCK.search(body)
    if critical_match is not None:
        critical_rules = critical_match.group(0)
        body = body[: critical_match.start()] + body[critical_match.end() :]
    before_wrapper = context_prefix[: wrapper_open.start()]
    after_wrapper = context_prefix[wrapper_close.end() :]
    other_tokens = sum(
        estimate_message_tokens(message) for message in messages if message is not current_user
    )

    def candidate_content(context_chars: int) -> str:
        retained = body[-context_chars:] if context_chars else ""
        retained = _COMPACTED_CONTEXT_BOUNDARY_MARKERS.sub("[marker removed]", retained)
        return (
            before_wrapper
            + wrapper_open.group(0)
            + critical_rules
            + _LOCAL_CONTEXT_OMISSION_MARKER
            + retained
            + "\n"
            + wrapper_close.group(0)
            + after_wrapper
            + protected_request
        )

    def fits(context_chars: int) -> bool:
        probe = dict(current_user)
        probe["content"] = candidate_content(context_chars)
        return other_tokens + estimate_message_tokens(probe) + tool_tokens <= target

    if not fits(0):
        return (
            False,
            tool_tokens,
            "the full current user request and required schemas exceed the target",
        )

    # Keep as much of the newest injected context as possible while preserving
    # the complete request. The estimator is linear in serialized text size.
    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    current_user["content"] = candidate_content(low)
    return True, tool_tokens, None


def _exec_tool(name: str, args_json: str, cwd: str) -> str:
    try:
        args = json.loads(args_json or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except (TypeError, ValueError):
        return "ERROR: malformed tool arguments"
    if name == "bash":
        cmd = str(args.get("command", ""))
        if not cmd:
            return "ERROR: empty command"
        if any(p.search(cmd) for p in _DANGER_PATTERNS):
            return "BLOCKED: potentially destructive command. Use a safer form or ask the user to run it manually."
        try:
            t = max(5, min(int(args.get("timeout_secs") or 120), 600))
        except (TypeError, ValueError):
            t = 120
        try:
            wrapped, child_env, cleanup = sandboxed_spawn_argv(
                _shell_argv(cmd), mode="standard", env=_tool_env()
            )
            p = run_limited(
                wrapped,
                cwd=cwd,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=t,
            )
            out = p.stdout or ""
            if p.stderr:
                out += ("\n[stderr] " if out else "") + p.stderr
            return (out[:_MAX_TOOL_OUTPUT] or "(no output)") + f"\n[exit {p.returncode}]"
        except subprocess.TimeoutExpired:
            return f"ERROR: timed out after {t}s"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {type(exc).__name__}: {exc}"
        finally:
            if "cleanup" in locals() and cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass
    if name == "read_file":
        raw_path = str(args.get("path", ""))
        path = str(resolve_tool_path(raw_path, cwd))
        try:
            return read_text_pinned(raw_path, cwd=cwd) or "(empty file)"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {type(exc).__name__}: {exc}"
    if name == "write_file":
        raw_path = str(args.get("path", ""))
        path = str(resolve_tool_path(raw_path, cwd))
        content = str(args.get("content", ""))
        try:
            write_text_pinned(raw_path, content, cwd=cwd)
            return f"OK: wrote {len(content)} bytes to {path}"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: {type(exc).__name__}: {exc}"
    return f"ERROR: unknown tool {name}"


def _trim_history(messages: list) -> None:
    """Compress old tool results so long runs stay inside the context window."""
    tool_idx = [
        i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "tool"
    ]
    for i in tool_idx[:-10]:
        m = messages[i]
        c = m.get("content")
        if isinstance(c, str) and len(c) > 400:
            m["content"] = c[:300] + f"\n[trimmed {len(c) - 300} chars]"


@dataclass
class _Session:
    model: str = "auto"
    cwd: str = os.path.expanduser("~")
    messages: list[dict[str, Any]] = field(
        default_factory=lambda: [{"role": "system", "content": _AGENT_SYSTEM_PROMPT}]
    )
    # Set True once the loaded model rejects the `tools` parameter; the loop
    # then continues chat-only for this session instead of erroring.
    tools_unsupported: bool = False
    context_window: int = 262_144
    context_window_is_loaded: bool = False
    context_window_post_completion_checked: bool = False
    cancel_requested: bool = False
    # Token accounting for Kiro Crew's context meter. ``last_usage`` is the
    # ``usage`` object LM Studio returns on the ONE non-streaming
    # ``/chat/completions`` call (prompt/completion/total tokens); the running
    # sums feed the stderr step log. The meter is fed ONLY by the adapter's own
    # ``usage_update {used, size}`` frame -- the OpenRouter/DeepSeek
    # adapters already emit it from their streamed usage, and without the same
    # frame here the context pill sat at 0% for an LM Studio session even though
    # the window size ("~0/1M") was correct.
    last_usage: dict[str, Any] | None = None
    usage_prompt_tokens: int = 0
    usage_completion_tokens: int = 0
    # Shared MCP bridge state — the shapes mcp_bridge helpers expect, kept
    # identical across every direct adapter so the bridge serves all of them.
    mcp_servers: list = field(default_factory=list)
    mcp_handles: dict = field(default_factory=dict)
    mcp_tools: dict = field(default_factory=dict)
    mcp_tool_allowlist: dict[str, frozenset[str]] = field(default_factory=dict)
    mcp_tool_denylist: dict[str, frozenset[str]] = field(default_factory=dict)


class LmStudioAcpServer:
    """Synchronous JSON-RPC ACP adapter with bounded loopback HTTP calls."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base_url = _base_url(base_url)
        self._api_key = api_key or ""
        self._sessions: dict[str, _Session] = {}
        self._loaded_context_window_models: set[str] = set()
        self._permission_decider: Callable[[str, str, str, str, dict[str, Any]], bool] | None = None
        self._deferred: list[dict[str, Any]] = []
        self._inbox: queue.Queue | None = None
        self._eof_seen = False
        self._active_http: Any | None = None
        self._active_http_socket: socket.socket | None = None
        self._active_http_lock = threading.Lock()
        self._http_cancel_state = threading.local()

    def _http_json(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: int = _REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        # The prompt worker has a cancellation event. Use a connection object
        # registered before connect/getresponse so Stop can close a request even
        # while LM Studio is still silent and has not returned headers.
        if (
            path in {"/chat/completions", "/api/v1/models/load"}
            and self._request_cancel_event() is not None
        ):
            return self._http_json_cancellable(path, payload, timeout_seconds=timeout_seconds)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        base_url = self._base_url
        if path.startswith("/api/"):
            base_url = self._base_url.removesuffix("/v1")
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=body,
            method="GET" if body is None else "POST",
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body else {}),
            },
        )
        if self._api_key:
            request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_seconds
            ) as raw:  # noqa: S310 -- _base_url is loopback-only
                with self._active_http_lock:
                    self._active_http = raw
                try:
                    parsed: Any = json.loads(raw.read().decode("utf-8"))
                finally:
                    with self._active_http_lock:
                        if self._active_http is raw:
                            self._active_http = None
        except urllib.error.HTTPError as exc:
            if exc.code in {502, 503}:
                raise LmStudioTransportError(
                    "LM Studio local server is temporarily unavailable",
                    retryable=True,
                ) from None
            try:
                error_body = exc.read(65_536)
            except (AttributeError, OSError):
                error_body = b""
            if exc.code in {400, 422} and _tools_rejected(error_body):
                raise LmStudioToolsUnsupported(
                    "LM Studio model does not support tool calling"
                ) from None
            raise LmStudioProtocolError(f"LM Studio request failed: {type(exc).__name__}") from None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LmStudioProtocolError(f"LM Studio request failed: {type(exc).__name__}") from None
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
            raise LmStudioTransportError(
                "LM Studio local server disconnected during the request",
                retryable=True,
            ) from None
        except urllib.error.URLError as exc:
            # urllib wraps socket timeouts in URLError. Their outcome is
            # ambiguous, just like a bare TimeoutError, so only definite local
            # connection failures are replay-safe.
            retryable = isinstance(
                exc.reason,
                (ConnectionRefusedError, ConnectionResetError, BrokenPipeError),
            )
            raise LmStudioTransportError(
                "LM Studio local server could not be reached", retryable=retryable
            ) from None
        except (TimeoutError, http.client.HTTPException) as exc:
            # A timeout or malformed response is ambiguous: the server may
            # still be generating, so replaying the POST could duplicate a
            # lengthy inference. Surface a stable error instead.
            raise LmStudioTransportError(
                f"LM Studio request failed: {type(exc).__name__}",
                retryable=False,
            ) from None
        if not isinstance(parsed, dict):
            raise LmStudioProtocolError("LM Studio returned a non-object response")
        return parsed

    def _http_json_cancellable(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Issue a loopback request whose pre-header wait can be interrupted."""
        parsed_url = urllib.parse.urlparse(self._base_url)
        if not parsed_url.hostname:
            raise LmStudioProtocolError("LM Studio endpoint has no host")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        base_path = parsed_url.path.rstrip("/")
        if path.startswith("/api/") and base_path.endswith("/v1"):
            base_path = base_path[:-3]
        endpoint = f"{base_path}{path}"
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        connection = http.client.HTTPConnection(
            parsed_url.hostname,
            parsed_url.port,
            timeout=timeout_seconds,
        )
        active_response: http.client.HTTPResponse | None = None
        with self._active_http_lock:
            self._active_http = connection
        try:
            if self._request_cancelled():
                raise _LmStudioCancelled
            connection.connect()
            with self._active_http_lock:
                if self._active_http is connection:
                    self._active_http_socket = connection.sock
            if self._request_cancelled():
                raise _LmStudioCancelled
            connection.request(
                "GET" if body is None else "POST", endpoint, body=body, headers=headers
            )
            if self._request_cancelled():
                raise _LmStudioCancelled
            response = connection.getresponse()
            active_response = response
            with self._active_http_lock:
                if self._active_http is connection:
                    self._active_http = response
            response_body = response.read()
            if self._request_cancelled():
                raise _LmStudioCancelled
            if response.status in {502, 503}:
                raise LmStudioTransportError(
                    "LM Studio local server is temporarily unavailable",
                    retryable=True,
                )
            if response.status >= 400:
                if response.status in {400, 422} and _tools_rejected(response_body):
                    raise LmStudioToolsUnsupported("LM Studio model does not support tool calling")
                raise LmStudioProtocolError("LM Studio request failed: HTTPError")
            try:
                decoded: Any = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if self._request_cancelled():
                    raise _LmStudioCancelled from None
                raise LmStudioProtocolError(
                    f"LM Studio request failed: {type(exc).__name__}"
                ) from None
        except _LmStudioCancelled:
            raise
        except TimeoutError as exc:
            if self._request_cancelled():
                raise _LmStudioCancelled from None
            raise LmStudioTransportError(
                f"LM Studio request failed: {type(exc).__name__}",
                retryable=False,
            ) from None
        except (
            http.client.RemoteDisconnected,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ) as exc:
            if self._request_cancelled():
                raise _LmStudioCancelled from None
            raise LmStudioTransportError(
                "LM Studio local server disconnected during the request",
                retryable=isinstance(
                    exc,
                    (
                        ConnectionRefusedError,
                        ConnectionResetError,
                        BrokenPipeError,
                        http.client.RemoteDisconnected,
                    ),
                ),
            ) from None
        except http.client.HTTPException as exc:
            if self._request_cancelled():
                raise _LmStudioCancelled from None
            raise LmStudioTransportError(
                f"LM Studio request failed: {type(exc).__name__}",
                retryable=False,
            ) from None
        finally:
            with self._active_http_lock:
                if self._active_http is connection or self._active_http is active_response:
                    self._active_http = None
                    self._active_http_socket = None
            connection.close()
        if not isinstance(decoded, dict):
            raise LmStudioProtocolError("LM Studio returned a non-object response")
        return decoded

    def _request_cancel_event(self) -> threading.Event | None:
        event = getattr(self._http_cancel_state, "event", None)
        return event if isinstance(event, threading.Event) else None

    def _request_cancelled(self) -> bool:
        event = self._request_cancel_event()
        return bool(event is not None and event.is_set())

    def _interrupt_active_http(self) -> None:
        """Wake a blocked urllib read without waiting on its buffered lock."""
        with self._active_http_lock:
            raw = self._active_http
            sock = self._active_http_socket
        if raw is None:
            return
        if sock is None:
            sock = getattr(raw, "sock", None)
        if sock is None:
            try:
                sock = raw.fp.raw._sock  # type: ignore[attr-defined]
            except AttributeError:
                sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _wait_until_ready(self, should_cancel=None) -> bool:
        """Wait briefly for LM Studio's authenticated OpenAI endpoint."""

        for delay in _RECOVERY_PROBE_DELAYS:
            event = getattr(self._http_cancel_state, "event", None)
            if event is not None:
                if event.wait(delay):
                    return False
            elif should_cancel is None:
                time.sleep(delay)
            else:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if should_cancel is not None and should_cancel():
                        return False
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            if self._request_cancelled() or (should_cancel is not None and should_cancel()):
                return False
            try:
                self._http_json(
                    "/models",
                    timeout_seconds=_RECOVERY_PROBE_TIMEOUT_SECONDS,
                )
            except LmStudioTransportError as exc:
                if exc.retryable:
                    continue
                return False
            except LmStudioProtocolError:
                return False
            return True
        return False

    def _models(self, *, timeout_seconds: int = _REQUEST_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        data = self._http_json("/api/v1/models", timeout_seconds=timeout_seconds).get("models", [])
        if not isinstance(data, list):
            self._loaded_context_window_models = set()
            return []
        models: list[dict[str, Any]] = []
        loaded_context_window_models: set[str] = set()
        rejected_rows = max(0, len(data) - MAX_MODEL_CATALOG_ENTRIES)
        bounded_fields = 0
        for item in data[:MAX_MODEL_CATALOG_ENTRIES]:
            if (
                not isinstance(item, dict)
                or item.get("type") != "llm"
                or not isinstance(item.get("key"), str)
            ):
                continue
            model_id = bounded_model_id(item["key"])
            if model_id is None:
                rejected_rows += 1
                continue
            # MTP/draft artifacts are speculative-decoding companions, not
            # standalone chat models.  LM Studio advertises them as ordinary
            # ``llm`` entries, so accepting them here made a draft head appear
            # in KiroCrew's model picker and let it answer a chat by itself.
            # Keep the asset installed for a compatible target runtime, but
            # never offer it as an interactive ACP session model.
            from kiro_crew import model_registry  # noqa: PLC0415

            if model_registry.is_interactive_chat_model(model_id):
                model: dict[str, Any] = {"modelId": model_id}
                loaded_contexts = []
                loaded_instances = item.get("loaded_instances")
                if isinstance(loaded_instances, list):
                    bounded_fields += max(0, len(loaded_instances) - MAX_LOADED_MODEL_INSTANCES)
                    for instance in loaded_instances[:MAX_LOADED_MODEL_INSTANCES]:
                        config = instance.get("config") if isinstance(instance, dict) else None
                        context = config.get("context_length") if isinstance(config, dict) else None
                        if (
                            isinstance(context, int)
                            and not isinstance(context, bool)
                            and 1024 <= context <= MAX_MODEL_CONTEXT_WINDOW
                        ):
                            loaded_contexts.append(context)
                        elif isinstance(context, int) and not isinstance(context, bool):
                            bounded_fields += 1
                context_window = (
                    min(loaded_contexts) if loaded_contexts else item.get("max_context_length")
                )
                if loaded_contexts:
                    loaded_context_window_models.add(model_id)
                if (
                    isinstance(context_window, int)
                    and not isinstance(context_window, bool)
                    and 1024 <= context_window <= MAX_MODEL_CONTEXT_WINDOW
                ):
                    model["contextWindow"] = context_window
                elif isinstance(context_window, int) and not isinstance(context_window, bool):
                    bounded_fields += 1
                display_name = item.get("display_name")
                name, truncated = bounded_model_text(display_name, max_length=MAX_MODEL_NAME_LENGTH)
                if truncated or (display_name is not None and name is None):
                    bounded_fields += 1
                model["name"] = name or model_id
                models.append(model)
        self._loaded_context_window_models = loaded_context_window_models
        if rejected_rows or bounded_fields:
            logger.warning(
                "LM Studio model catalog bounded: dropped %d row(s), bounded %d field/instance(s)",
                rejected_rows,
                bounded_fields,
            )
        return models

    def _refresh_session_model_context(self, session: _Session) -> None:
        """Refresh one selected model and its active loaded-instance budget."""
        if session.model == "auto":
            return
        models = self._models(timeout_seconds=_RECOVERY_PROBE_TIMEOUT_SECONDS)
        selected = next(
            (entry for entry in models if entry["modelId"] == session.model),
            None,
        )
        if selected is None:
            raise LmStudioProtocolError("Selected model is no longer available in LM Studio")
        session.context_window = int(selected.get("contextWindow") or 262_144)
        session.context_window_is_loaded = session.model in self._loaded_context_window_models

    def _refresh_session_model_context_with_recovery(
        self, session: _Session, should_cancel
    ) -> None:
        """Refresh model metadata across one bounded local-server restart."""
        try:
            self._refresh_session_model_context(session)
            return
        except LmStudioTransportError as exc:
            if not exc.retryable:
                raise
        if should_cancel():
            raise _LmStudioCancelled
        if not self._wait_until_ready(should_cancel):
            if should_cancel():
                raise _LmStudioCancelled
            raise LmStudioTransportError(
                "LM Studio local server did not recover while refreshing its model",
                retryable=False,
            )
        self._refresh_session_model_context(session)

    def _poll_model_load(self, session: _Session, should_cancel) -> dict[str, Any] | None:
        """Load a selected local model while keeping ACP Stop responsive."""
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        stopped = threading.Event()

        def _worker() -> None:
            self._http_cancel_state.event = stopped
            try:
                result_queue.put(
                    (
                        "result",
                        self._http_json(
                            "/api/v1/models/load",
                            {"model": session.model},
                            timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
                        ),
                    )
                )
            except BaseException as exc:
                result_queue.put(("error", exc))
            finally:
                self._http_cancel_state.event = None

        threading.Thread(target=_worker, daemon=True, name="lmstudio-model-load").start()
        while True:
            if should_cancel():
                stopped.set()
                self._interrupt_active_http()
                return None
            try:
                kind, value = result_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if should_cancel():
                stopped.set()
                self._interrupt_active_http()
                return None
            if kind == "result" and isinstance(value, dict):
                return value
            if isinstance(value, _LmStudioCancelled):
                return None
            if isinstance(value, BaseException):
                raise value
            raise LmStudioProtocolError("LM Studio model-load request returned invalid data")

    def _load_model_before_prompt(self, session: _Session, should_cancel) -> bool:
        """Load an unloaded model and learn its active window before prefill."""
        if session.model == "auto" or session.context_window_is_loaded:
            return True

        response: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                response = self._poll_model_load(session, should_cancel)
                if response is None:
                    return False
                break
            except LmStudioTransportError as exc:
                if not exc.retryable or attempt or not self._wait_until_ready(should_cancel):
                    raise
                if should_cancel():
                    return False
                self._refresh_session_model_context(session)
                if session.context_window_is_loaded:
                    return True

        if response is None:
            raise LmStudioProtocolError("LM Studio did not return a model-load result")
        if response.get("status") != "loaded":
            raise LmStudioProtocolError("LM Studio did not load the selected model")

        load_config = response.get("load_config")
        loaded_window = load_config.get("context_length") if isinstance(load_config, dict) else None
        if (
            isinstance(loaded_window, int)
            and not isinstance(loaded_window, bool)
            and 1024 <= loaded_window <= 10_000_000
        ):
            session.context_window = loaded_window
            session.context_window_is_loaded = True
            self._loaded_context_window_models.add(session.model)
        else:
            # Some engines omit load_config.context_length. Their model catalog
            # may still report the active loaded-instance context.
            self._refresh_session_model_context(session)
        if not session.context_window_is_loaded:
            raise LmStudioProtocolError(
                "LM Studio loaded the model without reporting its active context length"
            )
        return True

    @staticmethod
    def _session_id(params: object) -> str:
        if not isinstance(params, dict) or not isinstance(params.get("sessionId"), str):
            raise LmStudioProtocolError("sessionId is required")
        return params["sessionId"]

    def _session(self, params: object) -> _Session:
        session_id = self._session_id(params)
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise LmStudioProtocolError("unknown session") from exc

    def _update(self, sid: str, update: dict[str, Any]) -> dict[str, Any]:
        return _notification("session/update", {"sessionId": sid, "update": update})

    def _completion(self, session: _Session) -> dict[str, Any]:
        """One /chat/completions round-trip with the served-model check.

        If the loaded model rejects the ``tools`` parameter, retry once
        without it and mark the session chat-only so later iterations do not
        keep failing.  Raises LmStudioProtocolError on genuine failure.
        """
        payload: dict[str, Any] = {"messages": session.messages, "stream": False}
        if session.model != "auto":
            payload["model"] = session.model
        # Never leave the output unbounded: LM Studio's default is unlimited, so
        # a chatty local model could spend its whole window on one reply. Capped
        # by the model's own window as well, so a small-context model is not
        # asked for more tokens than it can hold.
        payload["max_tokens"] = max(
            1024,
            min(
                _max_output_ceiling(session.model),
                max(8192, session.context_window // 4),
            ),
        )
        if not session.tools_unsupported:
            # Built-in trio plus any MCP tools bridged for this session
            # (mounted lazily on first prompt; empty until then).
            payload["tools"] = _TOOLS_SPEC + mcp_bridge.mcp_tools_spec(session)
            payload["tool_choice"] = "auto"

        def submit() -> dict[str, Any]:
            if self._request_cancelled():
                raise _LmStudioCancelled
            try:
                return self._http_json("/chat/completions", payload)
            except LmStudioTransportError:
                raise
            except LmStudioToolsUnsupported:
                if self._request_cancelled():
                    raise _LmStudioCancelled from None
                if session.tools_unsupported or "tools" not in payload:
                    raise
                # The loaded model does not accept tool calling. Fall back to a
                # plain chat completion for this session; it can still answer,
                # it just cannot act. Keep this inside submit() so the same
                # fallback applies after a recovered transport replay.
                session.tools_unsupported = True
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                if self._request_cancelled():
                    raise _LmStudioCancelled
                return self._http_json("/chat/completions", payload)

        try:
            response = submit()
        except LmStudioTransportError as exc:
            # No tool call can have executed before a response reaches this
            # adapter. A disconnect may duplicate local inference work, but one
            # replay after an authenticated readiness probe cannot duplicate an
            # external action or charge a remote provider.
            if self._request_cancelled():
                raise _LmStudioCancelled from None
            if not exc.retryable or not self._wait_until_ready():
                raise
            if self._request_cancelled():
                raise _LmStudioCancelled
            self._refresh_session_model_context(session)
            replay_tools = payload.get("tools")
            request_tools = replay_tools if isinstance(replay_tools, list) else []
            candidate = list(session.messages)
            current_user = next(
                (
                    message
                    for message in reversed(candidate)
                    if isinstance(message, dict) and message.get("role") == "user"
                ),
                None,
            )
            fits_local_budget, tool_tokens, reason = _compact_local_prompt(
                candidate,
                session.context_window,
                request_tools,
                current_user,
            )
            if not fits_local_budget:
                raise LmStudioProtocolError(
                    "LM Studio loaded context shrank below the safe local prompt budget: "
                    f"{reason}"
                )
            reserve_tokens = 8192 + tool_tokens
            input_budget = session.context_window - reserve_tokens - 2048
            if sum(estimate_message_tokens(message) for message in candidate) > input_budget:
                raise LmStudioProtocolError(
                    "LM Studio loaded context shrank below the pending request size"
                )
            # Keep replay compaction private to this worker. Stop returns the
            # owning prompt loop to stdio dispatch before a slow worker is
            # guaranteed to unwind; mutating shared history here could erase a
            # follow-up that completed meanwhile. The prompt loop owns all
            # committed session-history changes.
            payload["messages"] = candidate
            response = submit()

        # A selected model is an execution boundary, not display-only
        # metadata.  LM Studio's OpenAI-compatible response identifies the
        # model that actually completed the request; accepting a different
        # value here would let the dashboard show a pinned model while a
        # completed turn was served by another resident model.  Refuse the
        # response instead.
        reported_model = response.get("model")
        if session.model != "auto":
            if not isinstance(reported_model, str) or reported_model.strip() != session.model:
                raise LmStudioProtocolError(
                    "LM Studio served a different model than the selected model"
                )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LmStudioProtocolError("LM Studio returned no completion choice")
        # Forward the provider's own token accounting. LM Studio (OpenAI-
        # compatible) returns a ``usage`` object on this non-streaming call;
        # dropping it here is what left Crew's context meter with no numerator.
        reported_usage = response.get("usage")
        session.last_usage = reported_usage if isinstance(reported_usage, dict) else None
        return choices[0]

    def _drain_cancels(self) -> None:
        if self._inbox is None or self._eof_seen:
            return
        while True:
            try:
                message = self._inbox.get_nowait()
            except queue.Empty:
                return
            if message is None:
                self._mark_eof()
                return
            if message.get("method") == "session/cancel":
                cancel_sid = (message.get("params") or {}).get("sessionId")
                target = self._sessions.get(cancel_sid) if isinstance(cancel_sid, str) else None
                if target is not None:
                    target.cancel_requested = True
                continue
            self._deferred.append(message)

    def _mark_eof(self) -> None:
        """Cancel active work and remember that stdin cannot dispatch again."""
        self._eof_seen = True
        for session in self._sessions.values():
            session.cancel_requested = True
        self._interrupt_active_http()

    def _poll_completion(self, session: _Session, should_cancel) -> dict[str, Any] | None:
        """Run blocking LM Studio HTTP in a worker while polling ACP Stop."""
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        stopped = threading.Event()

        def _worker() -> None:
            self._http_cancel_state.event = stopped
            try:
                result_queue.put(("result", self._completion(session)))
            except BaseException as exc:
                result_queue.put(("error", exc))
            finally:
                self._http_cancel_state.event = None

        threading.Thread(target=_worker, daemon=True, name="lmstudio-completion").start()
        while True:
            if should_cancel():
                stopped.set()
                self._interrupt_active_http()
                return None
            try:
                kind, value = result_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if should_cancel():
                stopped.set()
                self._interrupt_active_http()
                return None
            if kind == "result" and isinstance(value, dict):
                return value
            if isinstance(value, _LmStudioCancelled):
                return None
            if isinstance(value, BaseException):
                raise value
            raise LmStudioProtocolError("LM Studio completion worker returned invalid data")

    def _message_chunk(self, sid: str, text: str) -> dict[str, Any]:
        return self._update(
            sid,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    def _status_update(self, sid: str, text: str) -> dict[str, Any]:
        """A progress line about a wait, NOT model output.

        It rides its own discriminant on purpose: the dashboard renders it as a
        status line, while an ``agent_message_chunk`` would have entered the
        answer transcript and an ``agent_thought_chunk`` would have read as
        reasoning AND been taken as evidence the backend produced output --
        which is what the gateway's pre-stream retry and poisoned-conversation
        recovery branches key on. An informational frame must not decide those.
        """
        return self._update(
            sid,
            {
                "sessionUpdate": UPDATE_AGENT_STATUS,
                "content": {"type": "text", "text": text},
            },
        )

    def _run_prompt(self, request_id: object, params: object) -> Iterator[dict[str, Any]]:
        session = self._session(params)
        if not isinstance(params, dict):
            raise LmStudioProtocolError("Invalid prompt parameters")
        sid = self._session_id(params)
        # EOF is terminal for the stdio transport. A prompt drained just before
        # the sentinel must not clear the cancellation that _mark_eof applied.
        if not self._eof_seen:
            session.cancel_requested = False
        text = _prompt_text(params.get("prompt"))
        history_before_turn = list(session.messages)
        current_user = {"role": "user", "content": text}
        session.messages.append(current_user)

        mcp_bridge.bridge_session_servers(
            session,
            client_name="kirocrew-lmstudio",
            log_tag="kirocrew-lmstudio",
        )
        tools_spec = _TOOLS_SPEC + mcp_bridge.mcp_tools_spec(session)
        turn_start = time.monotonic()
        tools_used = 0

        def _cancelled() -> bool:
            self._drain_cancels()
            return session.cancel_requested

        def _finish_cancelled() -> Iterator[dict[str, Any]]:
            yield self._message_chunk(sid, "\n[kirocrew-lmstudio] Turn cancelled by user.")
            yield _response(request_id, {"stopReason": "cancelled"})

        for step in range(_MAX_STEPS):
            if _cancelled():
                yield from _finish_cancelled()
                return
            elapsed = time.monotonic() - turn_start
            if elapsed > _TURN_BUDGET_SECONDS:
                yield self._message_chunk(
                    sid,
                    f"[turn budget reached: {int(elapsed)}s wall clock "
                    f"({_TURN_BUDGET_SECONDS}s limit), {tools_used} tool calls. "
                    "Send a follow-up to continue where this left off.]",
                )
                yield _response(request_id, {"stopReason": "end_turn"})
                return
            if tools_used >= _MAX_TOOL_CALLS:
                yield self._message_chunk(
                    sid,
                    f"[tool budget reached: {_MAX_TOOL_CALLS} tool calls. "
                    "Send a follow-up to continue where this left off.]",
                )
                yield _response(request_id, {"stopReason": "end_turn"})
                return

            try:
                previous_context_window = session.context_window
                self._refresh_session_model_context_with_recovery(session, _cancelled)
            except _LmStudioCancelled:
                yield from _finish_cancelled()
                return
            if session.context_window != previous_context_window:
                yield self._update(
                    sid,
                    {
                        "sessionUpdate": UPDATE_CONTEXT_WINDOW,
                        "modelId": session.model,
                        "contextWindow": session.context_window,
                    },
                )
            if session.model != "auto" and not session.context_window_is_loaded:
                yield self._status_update(
                    sid,
                    f"Loading {session.model} in LM Studio and checking its active context "
                    "window before sending the prompt.",
                )
                previous_context_window = session.context_window
                try:
                    loaded = self._load_model_before_prompt(session, _cancelled)
                except _LmStudioCancelled:
                    loaded = False
                except (LmStudioProtocolError, LmStudioTransportError):
                    session.messages[:] = history_before_turn
                    raise
                if not loaded:
                    session.messages[:] = history_before_turn
                    yield from _finish_cancelled()
                    return
                if session.context_window != previous_context_window:
                    yield self._update(
                        sid,
                        {
                            "sessionUpdate": UPDATE_CONTEXT_WINDOW,
                            "modelId": session.model,
                            "contextWindow": session.context_window,
                        },
                    )
            candidate = list(session.messages)
            request_tools: list[dict[str, Any]] = [] if session.tools_unsupported else tools_spec
            fits_local_budget, tool_tokens, reason = _compact_local_prompt(
                candidate,
                session.context_window,
                request_tools,
                current_user,
            )
            if not fits_local_budget:
                if step == 0:
                    session.messages[:] = history_before_turn
                    notice = (
                        "[Kiro Crew] This prompt exceeds the configured local-model prefill "
                        "target after older history and injected context were compacted. "
                        f"Reason: {reason}. Shorten the current request or adjust "
                        "LMSTUDIO_PROMPT_BUDGET_TOKENS."
                    )
                else:
                    session.messages[:] = candidate
                    notice = (
                        "[Kiro Crew] This turn reached the configured local-model prefill "
                        "target after tool execution. Completed tool results were retained; "
                        "send a short follow-up or start a new session."
                    )
                    session.messages.append({"role": "assistant", "content": notice})
                print(
                    "[kirocrew-lmstudio] local prompt budget refusal: "
                    f"model={session.model} "
                    f"target={min(_local_prefill_target(session.context_window), max(1024, session.context_window - 8192 - 2048))} "
                    f"tool_schemas={tool_tokens} reason={reason}",
                    file=sys.stderr,
                    flush=True,
                )
                yield self._message_chunk(sid, notice)
                yield _response(request_id, {"stopReason": STOP_REASON_LOCAL_LIMIT})
                return
            reserve_tokens = 8192 + tool_tokens
            input_budget = session.context_window - reserve_tokens - 2048
            if sum(estimate_message_tokens(message) for message in candidate) > input_budget:
                if step == 0:
                    session.messages[:] = history_before_turn
                    notice = (
                        "[Kiro Crew] The newest request alone exceeds this local model's "
                        "safe input budget. Shorten it or start a new session."
                    )
                else:
                    session.messages[:] = candidate
                    notice = (
                        "[Kiro Crew] This turn reached the local model's safe context limit "
                        "after tool execution. Completed tool results were retained; send "
                        "a short follow-up or start a new session."
                    )
                    session.messages.append({"role": "assistant", "content": notice})
                yield self._message_chunk(sid, notice)
                yield _response(request_id, {"stopReason": STOP_REASON_LOCAL_LIMIT})
                return
            session.messages[:] = candidate

            # This adapter has ONE non-streaming round-trip per model call, so
            # between the request below and its full completion the ACP stream
            # carries nothing at all -- while a large prompt costs minutes of
            # local prompt processing (and, right after a model load, another
            # ~11-15s of JIT). Tell the dashboard what is being waited on,
            # sized from the request actually about to be sent: the compacted
            # history plus the tool schemas, exactly as _completion builds it
            # (no tools at all once the loaded model has rejected them).
            system_tokens = sum(
                estimate_message_tokens(message)
                for message in candidate
                if message.get("role") == "system"
            )
            current_message_tokens = sum(
                estimate_message_tokens(message) for message in candidate if message is current_user
            )
            message_tokens = sum(estimate_message_tokens(message) for message in candidate)
            history_tokens = max(0, message_tokens - system_tokens - current_message_tokens)
            tool_tokens = estimate_message_tokens({"tools": request_tools})
            builtin_tool_tokens = (
                estimate_message_tokens({"tools": request_tools[: len(_TOOLS_SPEC)]})
                if request_tools
                else 0
            )
            mcp_tool_tokens = max(0, tool_tokens - builtin_tool_tokens)
            prompt_tokens = message_tokens + tool_tokens
            if prompt_tokens >= _WAIT_STATUS_MIN_PROMPT_TOKENS:
                yield self._status_update(
                    sid,
                    f"Local model is reading ~{prompt_tokens // 1000}k estimated tokens "
                    f"(current prompt ~{current_message_tokens // 1000}k, prior history "
                    f"~{history_tokens // 1000}k, tool schemas ~{tool_tokens // 1000}k; "
                    f"{len(session.mcp_tools)} bridged MCP tools; "
                    f"{session.context_window // 1000}k context window). The first token "
                    "can take a minute or two on a local model.",
                )
                print(
                    "[kirocrew-lmstudio] prompt-size estimate: "
                    f"model={session.model} window={session.context_window} "
                    f"total={prompt_tokens} system={system_tokens} "
                    f"current_message={current_message_tokens} history={history_tokens} "
                    f"tool_schemas={tool_tokens} builtin_schemas={builtin_tool_tokens} "
                    f"mcp_schemas={mcp_tool_tokens} mcp_tools={len(session.mcp_tools)} "
                    f"mcp_servers={len(session.mcp_handles)}",
                    file=sys.stderr,
                    flush=True,
                )

            choice = self._poll_completion(session, _cancelled)
            if choice is None:
                yield from _finish_cancelled()
                return
            if (
                not session.context_window_is_loaded
                and not session.context_window_post_completion_checked
            ):
                # Some LM Studio builds expose max_context_length before the
                # first completion triggers model loading. Re-read once after
                # that first response so future turns and Kiro Crew's context
                # meter use the actual loaded-instance window.
                session.context_window_post_completion_checked = True
                previous_context_window = session.context_window
                try:
                    self._refresh_session_model_context(session)
                except (LmStudioProtocolError, LmStudioTransportError) as exc:
                    print(
                        "[kirocrew-lmstudio] context-window refresh after first completion failed: "
                        f"error={type(exc).__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
                if session.context_window != previous_context_window:
                    yield self._update(
                        sid,
                        {
                            "sessionUpdate": UPDATE_CONTEXT_WINDOW,
                            "modelId": session.model,
                            "contextWindow": session.context_window,
                        },
                    )
            if session.last_usage:
                _pt = usage_tokens(session.last_usage.get("prompt_tokens"))
                _ct = usage_tokens(session.last_usage.get("completion_tokens"))
                session.usage_prompt_tokens += _pt
                session.usage_completion_tokens += _ct
                # The dashboard's context meter is fed EXCLUSIVELY by this frame
                # (parse_usage_update -> last_prompt_stats.context_used_tokens).
                # ``used`` is the CURRENT window occupancy: this request's prompt
                # plus its completion, i.e. what the next request will carry.
                yield self._update(
                    sid,
                    {
                        "sessionUpdate": UPDATE_USAGE,
                        "used": _pt + _ct,
                        "size": session.context_window,
                    },
                )
            message_data = choice.get("message") or {}
            content = _content_text(message_data.get("content"))
            tool_calls = message_data.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise LmStudioProtocolError("LM Studio returned invalid tool calls")

            if not tool_calls:
                if not content:
                    # Match the OpenRouter/DeepSeek adapter contract: an empty
                    # provider completion is a recoverable empty turn, not
                    # assistant text.  Roll back only an initial attempt so the
                    # dashboard can retry it without duplicating the user turn.
                    # After a tool step the completed tool history must survive.
                    if step == 0:
                        session.messages[:] = history_before_turn
                    yield _response(request_id, {"stopReason": "end_turn"})
                    return
                session.messages.append({"role": "assistant", "content": content})
                yield self._message_chunk(sid, content)
                yield _response(request_id, {"stopReason": "end_turn"})
                return

            clean: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                normalized = dict(tool_call)
                normalized.pop("index", None)
                normalized["id"] = normalized.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                function = normalized.get("function")
                if isinstance(function, dict):
                    function = dict(function)
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        function["arguments"] = json.dumps(
                            arguments if arguments is not None else {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    normalized["function"] = function
                clean.append(normalized)
            if not clean:
                raise LmStudioProtocolError("LM Studio returned no valid tool calls")
            session.messages.append(
                {"role": "assistant", "content": content or "", "tool_calls": clean}
            )

            def _record_cancelled(start: int) -> None:
                for pending in clean[start:]:
                    session.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": pending["id"],
                            "content": "ERROR: cancelled by user; tool not executed.",
                        }
                    )

            for call_index, tool_call in enumerate(clean):
                if _cancelled():
                    _record_cancelled(call_index)
                    yield from _finish_cancelled()
                    return
                fn = tool_call.get("function") or {}
                if not isinstance(fn, dict):
                    fn = {}
                name = fn.get("name") or "unknown"
                tc_id = tool_call["id"]
                arguments = fn.get("arguments") or "{}"
                try:
                    raw_input = json.loads(arguments)
                    if not isinstance(raw_input, dict):
                        raw_input = {"input": raw_input}
                except (TypeError, ValueError):
                    raw_input = {"input": arguments}
                raw_input = normalize_tool_input_paths(name, raw_input, cwd=session.cwd)
                is_mcp_tool = name in session.mcp_tools
                if is_mcp_tool:
                    raw_input = mcp_identity_raw_input(session, name, raw_input)

                yield self._update(
                    sid,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tc_id,
                        "title": f"{name}: {str(arguments)[:80]}",
                        "kind": _KIND.get(name, "other"),
                        "status": "pending",
                        "rawInput": raw_input,
                    },
                )
                policy_allows_tool = not is_mcp_tool or mcp_tool_policy_allows(session, name)
                permission_granted = bool(
                    policy_allows_tool
                    and self._permission_decider
                    and self._permission_decider(
                        sid, tc_id, name, _KIND.get(name, "other"), raw_input
                    )
                )
                if _cancelled():
                    _record_cancelled(call_index)
                    yield from _finish_cancelled()
                    return
                if not policy_allows_tool:
                    result = (
                        "ERROR: MCP tool is outside this agent's tool policy; tool not executed."
                    )
                    tool_iter = None
                elif not permission_granted:
                    result = "ERROR: permission denied; tool not executed."
                    tool_iter = None
                elif name in session.mcp_tools:
                    # Recheck at the dispatch boundary: a hidden schema or an
                    # approval decision must never widen the agent's MCP scope.
                    if not mcp_tool_policy_allows(session, name):
                        result = (
                            "ERROR: MCP tool is outside this agent's tool policy; "
                            "tool not executed."
                        )
                        tool_iter = None
                    else:
                        result = ""
                        tool_iter = iter_mcp_tool(
                            session, name, arguments, should_cancel=_cancelled
                        )
                else:
                    result = ""
                    tool_iter = iter_tool(name, arguments, session.cwd, should_cancel=_cancelled)

                last_progress = None
                if tool_iter is not None:
                    for kind, payload_text in tool_iter:
                        if kind == "progress" and payload_text != last_progress:
                            last_progress = payload_text
                            yield self._update(
                                sid,
                                {
                                    "sessionUpdate": "tool_call_update",
                                    "toolCallId": tc_id,
                                    "status": "in_progress",
                                    "content": [
                                        {
                                            "type": "content",
                                            "content": {
                                                "type": "text",
                                                "text": payload_text[:2000],
                                            },
                                        }
                                    ],
                                },
                            )
                        elif kind == "final":
                            result = payload_text
                session.messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                if _cancelled():
                    _record_cancelled(call_index + 1)
                    yield from _finish_cancelled()
                    return
                tools_used += 1
                # Terminal frame for this call MUST be a ``tool_call_update``:
                # the ACP client maps ``sessionUpdate == "tool_call"`` to a
                # *new/refined call* (EVENT_TOOL_CALL) and only
                # ``tool_call_update`` to EVENT_TOOL_RESULT. Emitting the
                # terminal under the initial kind leaves the call open on
                # every consumer (the dashboard pill stays "Running · Ns",
                # no ``tool_result`` frame, the stall watchdog stays armed),
                # which is why a fast tool that never streams a progress
                # frame looked hung. Every direct adapter's prompt loop emits
                # the terminal update in this shape.
                _out = result[:2000]
                yield self._update(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tc_id,
                        "status": "completed",
                        "content": [{"type": "content", "content": {"type": "text", "text": _out}}],
                        "rawOutput": {"output": _out, "truncated": len(result) > 2000},
                    },
                )

        yield self._message_chunk(
            sid,
            f"[agent step cap reached: {_MAX_STEPS} model round-trips, "
            f"{tools_used} tool calls. Send a follow-up to continue where this left off.]",
        )
        yield _response(request_id, {"stopReason": "end_turn"})

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Return zero or more JSON-RPC frames for one inbound message."""
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})
        if not isinstance(method, str):
            return [_error(request_id, -32600, "Invalid Request")]
        try:
            if method == "session/prompt":
                return list(self._run_prompt(request_id, params))
            if method == "initialize":
                if (
                    not isinstance(params, dict)
                    or params.get("protocolVersion") != _PROTOCOL_VERSION
                ):
                    return [_error(request_id, -32602, "Unsupported ACP protocol version")]
                return [
                    _response(
                        request_id,
                        {
                            "protocolVersion": _PROTOCOL_VERSION,
                            "agentInfo": {"name": "kirocrew-lmstudio", "version": "0.2.0"},
                            "agentCapabilities": {
                                "loadSession": False,
                                "promptCapabilities": {"image": False, "embeddedContext": False},
                                "mcpCapabilities": {"http": True, "sse": True, "acp": False},
                                "sessionCapabilities": {"close": {}, "delete": {}, "list": {}},
                            },
                        },
                    )
                ]
            if method == "session/new":
                if not isinstance(params, dict) or not isinstance(params.get("cwd"), str):
                    return [_error(request_id, -32602, "session/new requires cwd")]
                try:
                    tool_allowlist, tool_denylist = mcp_bridge.parse_session_tool_policy(params)
                except mcp_bridge.McpToolPolicyError as exc:
                    return [_error(request_id, -32602, str(exc))]
                # The MCP array is accepted (bridged on first prompt) — H6
                # parity with the OpenRouter harness.
                mcp_servers = [s for s in (params.get("mcpServers") or []) if isinstance(s, dict)]
                models = self._models()
                current = models[0]["modelId"] if models else "auto"
                session_id = str(uuid.uuid4())
                current_entry = next((entry for entry in models if entry["modelId"] == current), {})
                self._sessions[session_id] = _Session(
                    model=current,
                    cwd=params["cwd"],
                    mcp_servers=mcp_servers,
                    mcp_tool_allowlist=tool_allowlist,
                    mcp_tool_denylist=tool_denylist,
                    context_window=int(current_entry.get("contextWindow") or 262_144),
                    context_window_is_loaded=current in self._loaded_context_window_models,
                )
                return [
                    _response(
                        request_id,
                        {
                            "sessionId": session_id,
                            "models": {"availableModels": models, "currentModelId": current},
                        },
                    )
                ]
            if method == "session/set_model":
                session = self._session(params)
                if not isinstance(params, dict) or not isinstance(params.get("modelId"), str):
                    return [_error(request_id, -32602, "modelId is required")]
                wanted = params["modelId"].strip()
                models = self._models()
                selected = next(
                    (entry for entry in models if entry["modelId"] == wanted),
                    None,
                )
                if selected is None:
                    return [
                        _error(request_id, -32602, "Requested model is not available in LM Studio")
                    ]
                session.model = wanted
                session.context_window = int(selected.get("contextWindow") or 262_144)
                session.context_window_is_loaded = wanted in self._loaded_context_window_models
                session.context_window_post_completion_checked = False
                # A newly loaded model may or may not support tools; re-probe.
                session.tools_unsupported = False
                sid = self._session_id(params)
                return [
                    _response(request_id, {}),
                    self._update(
                        sid,
                        {
                            "sessionUpdate": UPDATE_CONTEXT_WINDOW,
                            "modelId": session.model,
                            "contextWindow": session.context_window,
                        },
                    ),
                ]
            if method in {"session/close", "session/delete"}:
                old = self._sessions.pop(self._session_id(params), None)
                if old is not None:
                    # Kill any MCP subprocesses this session owns (no-op when
                    # none were bridged).
                    mcp_bridge.close_session_handles(old)
                return [_response(request_id, {})]
            if method == "session/list":
                return [_response(request_id, {"sessions": []})]
            if method == "session/cancel":
                cancel_sid = (params or {}).get("sessionId") if isinstance(params, dict) else None
                target = self._sessions.get(cancel_sid) if isinstance(cancel_sid, str) else None
                if target is not None:
                    target.cancel_requested = True
                    self._interrupt_active_http()
                return []
            return [_error(request_id, -32601, f"Method not found: {method}")]
        except LmStudioProtocolError as exc:
            return [_error(request_id, -32603, str(exc))]


def main() -> int:
    try:
        server = LmStudioAcpServer(
            base_url=os.environ.get("KIROCREW_LMSTUDIO_BASE_URL"),
            api_key=os.environ.get("LM_STUDIO_API_KEY"),
        )
    except LmStudioProtocolError as exc:
        print(json.dumps(_error(None, -32602, str(exc))), flush=True)
        return 2
    inbox: "queue.Queue" = queue.Queue()

    def _reader() -> None:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                print(json.dumps(_error(None, -32700, "Parse error")), flush=True)
                continue
            if not isinstance(message, dict):
                print(json.dumps(_error(None, -32600, "Invalid Request")), flush=True)
                continue
            inbox.put(message)
        inbox.put(None)

    threading.Thread(target=_reader, daemon=True, name="acp-stdin").start()
    server._inbox = inbox
    server._permission_decider = make_permission_decider(
        inbox,
        server._deferred,
        on_cancel=lambda sid: (
            setattr(server._sessions.get(sid), "cancel_requested", True)
            if server._sessions.get(sid) is not None
            else None
        ),
        on_eof=server._mark_eof,
    )
    while True:
        message = inbox.get()
        if message is None:
            break
        if message.get("method") == "session/prompt":
            try:
                frames = server._run_prompt(message.get("id"), message.get("params", {}))
                for frame in frames:
                    print(json.dumps(frame, separators=(",", ":")), flush=True)
            except LmStudioProtocolError as exc:
                print(
                    json.dumps(_error(message.get("id"), -32603, str(exc)), separators=(",", ":")),
                    flush=True,
                )
        else:
            for frame in server.handle(message):
                print(json.dumps(frame, separators=(",", ":")), flush=True)
        while server._deferred and not server._eof_seen:
            for frame in server.handle(server._deferred.pop(0)):
                print(json.dumps(frame, separators=(",", ":")), flush=True)
        if server._eof_seen:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
