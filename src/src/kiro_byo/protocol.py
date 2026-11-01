"""ACP/Kiro protocol facts for Kiro BYO.

The running agent's ``initialize`` response is the authority.  Keep protocol
advertisement here, separate from KiroCrew's product-level backend card, so the
App UI never invents a backend-card score that can drift from the host.

Stable ACP v1 is the compatibility floor.  Kiro CLI V3 also negotiates protocol
version 1 and moves model/effort selection to ``session/set_config_option``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

TYPE_SELECTION_PREFIX = "kiro-byo-type:"
_TYPE_SELECTION_ID_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class TypeSelection:
    """Namespaced virtual execution selection.

    A Type only has meaning inside its owning Mode, so the wire id always
    carries both identities: ``kiro-byo-type:<mode>:<type>``.  The old
    unnamespaced form is intentionally rejected rather than migrated.
    """

    mode_id: str
    type_id: str


def _selection_id_part(value: str, field: str) -> str:
    text = str(value or "").strip().lower()
    if not text or not _TYPE_SELECTION_ID_RE.fullmatch(text):
        raise ValueError(f"{field} must use lowercase a-z, 0-9, '-' or '_'")
    return text


def type_selection_id(mode_id: str, type_id: str) -> str:
    mode = _selection_id_part(mode_id, "mode_id")
    type_value = _selection_id_part(type_id, "type_id")
    return f"{TYPE_SELECTION_PREFIX}{mode}:{type_value}"


def parse_type_selection(value: str) -> TypeSelection | None:
    text = str(value or "").strip().lower()
    if not text.startswith(TYPE_SELECTION_PREFIX):
        return None
    payload = text[len(TYPE_SELECTION_PREFIX):]
    parts = payload.split(":")
    if len(parts) != 2:
        return None
    mode_id, type_id = parts
    if not _TYPE_SELECTION_ID_RE.fullmatch(mode_id) or not _TYPE_SELECTION_ID_RE.fullmatch(type_id):
        return None
    return TypeSelection(mode_id=mode_id, type_id=type_id)
MODEL_ECHO = "kiro-byo/echo"
MODEL_UPPER = "kiro-byo/echo-uppercase"

AGENT_NAME = "Kiro BYO"
AGENT_VERSION = "0.3.0"

MODEL_CONFIG_ID = "model"
THINKING_CONFIG_ID = "thinkingMode"
EFFORT_CONFIG_ID = "effortLevel"
LEGACY_EFFORT_CONFIG_IDS = frozenset({"reasoning_effort", "effort"})

# Kiro V3 keeps this historical extension name intentionally.
EXTENSION_STEER = "_session/steer"
# Kiro/Crew-specific helpers retained for older clients; the standard session
# lifecycle methods are advertised separately below.
EXTENSION_COMPACT = "_kiro/session/compact"


def initialize_result() -> dict[str, Any]:
    """Return the capabilities Kiro BYO actually implements.

    Do not add a method here until its behavior is covered by protocol tests.
    In particular, fork is omitted rather than guessed. additionalDirectories
    is advertised because the runtime enforces it as part of the filesystem root
    set on new/load/resume, matching stable ACP v1 semantics.
    """
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": {
            "loadSession": True,
            "promptCapabilities": {"image": True, "embeddedContext": False},
            # ACP v1 requires stdio MCP support from every agent. HTTP is
            # optional and advertised only because 0.9.10 implements Streamable
            # HTTP. Legacy SSE stays deliberately unadvertised.
            "mcpCapabilities": {"http": True},
            "sessionCapabilities": {
                "list": {},
                "delete": {},
                "resume": {},
                "close": {},
                "additionalDirectories": {},
            },
            "_meta": {
                "kiro": {
                    "extensionMethods": [EXTENSION_STEER, EXTENSION_COMPACT],
                }
            },
        },
        "authMethods": [],
        "agentInfo": {
            "name": AGENT_NAME,
            "title": AGENT_NAME,
            "version": AGENT_VERSION,
        },
    }


def runtime_capability_report(*, native_tools: list[str], mcp_transports: list[str]) -> dict[str, Any]:
    """Machine-readable implementation facts for the Kiro BYO App.

    This deliberately is *not* KiroCrew's ten-row backend card.  KiroCrew owns
    that card and derives it from its registered harness/capability sets.
    """
    init = initialize_result()
    session_caps = init["agentCapabilities"]["sessionCapabilities"]
    return {
        "source": "runtime-introspection",
        "acp_protocol_version": PROTOCOL_VERSION,
        "agent_version": AGENT_VERSION,
        "standard_methods": [
            "initialize",
            "session/new",
            "session/load",
            "session/list",
            "session/resume",
            "session/delete",
            "session/close",
            "session/prompt",
            "session/cancel",
            "session/set_mode",
            "session/set_config_option",
        ],
        "session_capabilities": sorted(session_caps),
        "kiro_extensions": list(init["agentCapabilities"]["_meta"]["kiro"]["extensionMethods"]),
        "native_tools": list(native_tools),
        "mcp_transports": list(mcp_transports),
        "notes": [
            "KiroCrew owns the product-level backend capability card; runtime facts never synthesize its score.",
            "MCP stdio and Streamable HTTP auto-negotiate 2026-07-28 with legacy initialize fallback; deprecated SSE is fail-closed.",
            "MCP multi-round input_required is explicitly unsupported until Crew exposes an interactive projection.",
        ],
    }
