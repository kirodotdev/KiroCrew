"""Shaping Kiro Crew's own MCP servers for a public-ACP-spec adapter.

kiro-cli receives Crew's servers through its ``--agent`` spec, which it reads off
disk. A spec adapter (claude-agent-acp, codex-acp, ``goose acp``,
``opencode acp``, ``pi-acp``) reads no Kiro
Crew config at all, so the only channel is the ``mcpServers`` array on
``session/new`` / ``session/load``. Without it the crew is present but INERT: no
memory, no cron, no spawn, no artifacts, no learn — a bare vendor agent with Kiro
Crew acting as a chat transport.

Dialect-neutral on purpose. An earlier version of these helpers lived in
``acp/codex.py`` and was never called from anywhere; claude and goose need the same
shaping, and keying it to one adapter is what let it rot unnoticed.

WHAT IS DELIVERED, AND WHAT IS DELIBERATELY NOT
-----------------------------------------------
Only Crew's OWN managed servers, and only those currently permitted:

- ``spec_gate`` is honoured, so a server whose gate is closed is not delivered.
  ``kirocrew-computer`` carries one; delivering it regardless would spawn the
  desktop-automation shim on a host where computer use is unsupported or the
  keystone enable is off.
- ``opt_in`` servers are EXCLUDED. ``kirocrew-dashboard`` is an assignable set,
  not an always-on capability — it writes the operator's session layout. The two
  loops that write kiro specs skip it unless an agent was granted it, and handing
  it to every adapter session would be a privilege grant nobody made.
- USER-CONFIGURED servers are excluded entirely, and this is the load-bearing
  boundary. ``mcp_gateway/session_servers`` documents the rule: a non-poolable
  user server is left to the agent spec precisely so its ``env`` — which
  routinely holds tokens and API keys — never leaves the file it was declared
  in. kiro-cli reads those from disk. A spec adapter can only be told over the
  wire, so delivering them would transmit the operator's secrets through a
  third-party binary's stdin. Crew's own managed entries are safe by comparison:
  ``_managed_mcp_env`` returns nothing at all on a default install.

No ``autoApprove`` key survives, ever. An auto-approved MCP tool is approved
inside the adapter's runtime and emits no permission request, so
``HookManager.on_tool_call`` — the only place the bundled deny rules, the
sensitive-path block and the governance ceiling execute — is never reached for it.
"""

from __future__ import annotations

import hashlib
import logging
import re

from kiro_crew.acp.types import ACP_BACKEND_PI

logger = logging.getLogger(__name__)

#: Keys a strict spec-adapter deserializer accepts on an ``mcpServers`` entry.
#: Kiro's passthrough keys (``autoApprove``, ``timeout``, vendor keys) make a Rust
#: serde deserializer reject the WHOLE ``session/new``, not just the offending
#: element — so reduction is a correctness requirement, not tidiness.
SPEC_STDIO_SERVER_KEYS = frozenset({"name", "command", "args", "env"})

#: ``McpServerStdio`` requires all four of these. ``args`` and ``env`` are
#: REQUIRED even when empty: omitting ``env`` fails deserialization on a default
#: install, which is the common case and was the bug that made the earlier
#: unwired version of this module unusable had anything called it.
_REQUIRED_STDIO_KEYS = ("name", "command", "args", "env")

# A spec adapter accepts only these characters in an MCP server name.
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_UNSAFE_RUN_RE = re.compile(r"[^A-Za-z0-9_-]+")
_NAME_HASH_LEN = 6


def reserved_managed_names() -> frozenset[str]:
    """Names a user-configured server must never be allowed to sanitise onto.

    Fails CLOSED. Returning an empty set when the managed table cannot be read is
    the dangerous direction: with nothing reserved, a user-configured
    ``kirocrew core`` sanitises to ``kirocrew-core`` and impersonates the trusted
    managed server, inheriting whatever trust the declared name carries.
    """
    from kiro_crew.agent import _MANAGED_MCP_SERVERS

    return frozenset(_MANAGED_MCP_SERVERS)


def safe_server_name(name: str, taken: set[str]) -> str:
    """Coerce ``name`` into the adapter's charset without colliding.

    Collisions are resolved with a widening hash of the ORIGINAL name, so two
    different inputs that sanitise to the same base stay distinguishable rather
    than one silently shadowing the other.
    """
    if _SAFE_NAME_RE.fullmatch(name) and name not in taken:
        return name

    base = _UNSAFE_RUN_RE.sub("-", name).strip("-")
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    if not base:
        base = digest[:_NAME_HASH_LEN]
    candidate = base
    width = _NAME_HASH_LEN
    while candidate in taken and width <= len(digest):
        candidate = f"{base}-{digest[:width]}"
        width += 2
    return candidate


def reduce_to_spec_keys(entry: dict) -> dict:
    """Drop keys a spec adapter's deserializer rejects, and satisfy the required set.

    ``args`` and ``env`` are emitted even when empty, because ``McpServerStdio``
    requires them. Anything outside :data:`SPEC_STDIO_SERVER_KEYS` is dropped —
    including ``autoApprove``, which must never reach an adapter.
    """
    out = {key: value for key, value in entry.items() if key in SPEC_STDIO_SERVER_KEYS}
    out.setdefault("args", [])
    out.setdefault("env", [])
    return out


def managed_spec_servers() -> list[dict]:
    """Crew's own managed MCP servers as spec-dialect stdio entries.

    Honours ``spec_gate`` and skips ``opt_in`` servers — see the module docstring
    for why each matters. A server whose invocation cannot be resolved is skipped
    with a warning rather than failing the whole session: one unavailable shim
    should not cost the operator every other Crew tool.
    """
    from kiro_crew.agent import (
        _MANAGED_MCP_SERVERS,
        _gated_off_servers,
        _managed_mcp_env,
    )

    env = _managed_mcp_env()
    env_pairs = [{"name": key, "value": value} for key, value in sorted(env.items())]

    try:
        gated_off = _gated_off_servers()
    except Exception:
        # Fail CLOSED, matching _gated_off_servers' own posture: if the gates
        # cannot be evaluated, deliver no gated server rather than guess.
        logger.warning("spec MCP: gate evaluation failed; withholding gated servers")
        gated_off = frozenset(_MANAGED_MCP_SERVERS)

    entries: list[dict] = []
    for name, spec in _MANAGED_MCP_SERVERS.items():
        if name in gated_off:
            continue
        if spec.get("opt_in"):
            continue
        invocation = spec.get("invocation_fn")
        if not callable(invocation):
            continue
        try:
            command, args = invocation()
        except Exception:
            logger.warning("spec MCP: %s resolution failed", name, exc_info=True)
            continue
        entries.append(
            {
                "name": name,
                "command": command,
                "args": list(args),
                # Always present, never conditional — the schema requires it.
                "env": list(env_pairs),
            }
        )
    return entries


def merge_session_servers(managed: list[dict], pooled: list[dict]) -> list[dict]:
    """Merge managed entries with MCP-gateway broker stubs, deduped by name.

    The broker stub WINS on a name collision. It reaches the same backend through
    gatewayd and is the addressing layer MCP Apps callbacks route through, so
    preferring the direct entry would silently unroute those callbacks. Two
    elements sharing a name is undefined in the ACP schema, so exactly one
    survives.

    Every surviving entry is reduced to the spec key set, including the pooled
    stubs: those carry operator passthrough keys copied verbatim from the overlay,
    and one of them is enough to make a strict deserializer reject the entire
    ``session/new``.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for entry in list(pooled) + list(managed):
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        out.append(reduce_to_spec_keys(entry))
    return out


def pin_session_callback_env(
    entries: list[dict],
    *,
    session_key: str = "",
    channel_id: str = "",
    bound_port: str = "",
) -> list[dict]:
    """Stamp gateway callback identity onto each ``mcpServers`` env list.

    Spec adapters spawn these stdio servers themselves and often pass ONLY the
    declared env, not the ACP process environment. Session-bound tools
    (``ask_question``, ``workflow_run``) resolve the caller and the loopback
    port from env, so those values must ride the ``session/new`` entry.
    Empty values are omitted rather than written as blanks: a blank
    ``KIROCREW_SESSION_KEY`` would hide a later inherited identity.
    """
    extras: list[tuple[str, str]] = []
    if session_key:
        extras.append(("KIROCREW_SESSION_KEY", session_key))
    if channel_id:
        extras.append(("KIROCREW_CHANNEL_ID", channel_id))
    if bound_port:
        extras.append(("KIROCREW_BOUND_PORT", bound_port))
        extras.append(("KIROCREW_PORT", bound_port))
    if not extras:
        return entries

    overwrite = {key for key, _ in extras}
    pinned: list[dict] = []
    for entry in entries:
        raw_env = entry.get("env")
        kept: list[dict] = []
        if isinstance(raw_env, list):
            for pair in raw_env:
                if isinstance(pair, dict) and pair.get("name") not in overwrite:
                    kept.append(pair)
        kept.extend({"name": key, "value": value} for key, value in extras)
        pinned.append({**entry, "env": kept})
    return pinned


def crew_mcp_forwarding_unverified(backend: str) -> bool:
    """True when Crew MCP is delivered on the wire but forwarding is unverified.

    Official pi-acp accepts ``mcpServers`` on ``session/new`` and may not
    wire them through to the agent. Delivery still happens when ROUTED;
    do not treat spawn or Crew tools as verified on that backend.
    """
    return backend == ACP_BACKEND_PI


def entry_is_spec_legal(entry: dict) -> bool:
    """Whether ``entry`` satisfies ``McpServerStdio``'s required field set.

    Used by tests and as a cheap self-check: a missing required field is refused
    by the adapter as a whole-request deserialization failure, which surfaces as
    an opaque protocol error rather than a named problem with one server.
    """
    return all(key in entry for key in _REQUIRED_STDIO_KEYS)


__all__ = [
    "SPEC_STDIO_SERVER_KEYS",
    "crew_mcp_forwarding_unverified",
    "entry_is_spec_legal",
    "managed_spec_servers",
    "merge_session_servers",
    "pin_session_callback_env",
    "reduce_to_spec_keys",
    "reserved_managed_names",
    "safe_server_name",
]
