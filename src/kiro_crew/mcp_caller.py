"""Caller-identity protocol extension for KiroCrew MCP servers.

BACKGROUND
----------
The base MCP protocol (Anthropic spec 2024-11-05) models a 1:1 stdio
transport: one client, one server. When KiroCrew introduces a broker that
fans N client sessions into a shared backend, the backend no
longer has an implicit 1:1 identity — it needs to know *which* client made
each call so it can:

* Route callbacks back to the originating session (``spawn_run`` completion,
  ``register_hook``, ``send_message(session='origin')``).
* Scope per-session state (memory keys, file paths, audit records).
* Enforce per-session authorization policies.

Previously, the backend read this identity from the ``KIROCREW_SESSION_KEY``
environment variable baked in at spawn time. That approach is incompatible
with pooling because one backend would serve many sessions but see only the
first session's env var.

DESIGN
------
This module defines a **namespaced, versioned, typed** protocol extension:

1. **Wire format** — the gateway injects identity into every forwarded
   ``tools/call`` request under a namespaced ``_meta`` key::

       "_meta": {
           "kirocrew.caller": {
               "schemaVersion": 1,
               "sessionKey": "T123ABC:C456DEF:1777...",
               "sessionType": "slack-thread" | "dashboard" | "cron" | ...,
               "principalId": "mingweic",
               "channelId": "C0AUNEY55NV"
           }
       }

2. **Capability negotiation** — backends that support pooled operation
   advertise the extension in their ``initialize`` response::

       "capabilities": {
           "tools": {"listChanged": false},
           "experimental": {
               "kirocrew.caller-identity": {"schemaVersion": 1}
           }
       }

   Gatewayd inspects this capability at backend boot. Backends that **do
   not** advertise the extension are treated as single-session: gatewayd
   will refuse to pool them and fall back to per-session spawn (the
   equivalent of the old ``UNPOOLABLE_SERVERS`` list, but auto-detected).

3. **Typed context** — tool handlers receive a ``CallerContext`` dataclass
   as an explicit argument. No contextvars, no env reads, no implicit state.
   When the gateway does not inject the extension (non-pooled topology,
   older gateway, legacy clients), a fallback ``CallerContext`` is
   constructed from the ambient ``KIROCREW_SESSION_KEY`` env var so
   existing behaviour is preserved.

4. **Forward compatibility** — ``schemaVersion`` lets the extension evolve.
   A v2 schema may add fields; v1 consumers must ignore unknown keys.

This module is the **single source of truth** for the extension's wire
format, capability name, and validation rules. mcp_core / mcp_cron and
any future KiroCrew-owned MCP server must go through this module.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

# --- Protocol identifiers ---------------------------------------------------

#: Namespaced key placed inside ``params._meta`` on forwarded tools/call.
CALLER_META_KEY = "kirocrew.caller"

#: Capability key backends advertise in ``initialize`` response under
#: ``capabilities.experimental``. Presence indicates the backend
#: understands the ``kirocrew.caller`` ``_meta`` block and is safe to pool.
CALLER_CAPABILITY_KEY = "kirocrew.caller-identity"

#: Current schema version of the caller identity block. Bump when fields
#: change in a non-additive way. Additive changes (new optional fields) do
#: NOT bump this version — consumers MUST ignore unknown fields.
CALLER_SCHEMA_VERSION = 1

#: Process-lifetime cache of a RESOLVED ``from_env()`` identity. The env var
#: and ancestor pidfile chain are immutable once present, so the ancestor walk
#: — which forks ``ps`` per ancestor on non-Linux and can stall an event loop —
#: need run at most once. Only a non-empty result is cached (see ``from_env``).
_FROM_ENV_CACHE: "CallerContext | None" = None


def _parent_pid(pid: int) -> int:
    """Best-effort parent-PID lookup (Linux /proc, ``ps`` fallback). Returns 0
    on failure so an ancestor walk terminates safely."""
    try:
        if platform.system() == "Linux":
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        return int(line.split()[1])
        else:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)], text=True, timeout=2
            )
            return int(out.strip())
    except Exception:
        pass
    return 0


# --- Typed context ----------------------------------------------------------


@dataclass(frozen=True)
class CallerContext:
    """Identity of the originating KiroCrew session for a single MCP call.

    Immutable by design — passing by reference is safe, and tool handlers
    cannot accidentally mutate the caller on their way to downstream code.

    The ``session_key`` is the authoritative identifier for routing
    callbacks (spawn_run completion events, hook deliveries, ``send_message``
    with ``session='origin'``). All other fields are diagnostic or
    authorization hints.
    """

    session_key: str
    session_type: str = "unknown"
    principal_id: str = ""
    channel_id: str = ""
    #: True if this context came from a gateway-injected identity block,
    #: False if synthesized from env-var fallback. Useful for logging and
    #: tests that want to assert "we're really in pooled mode".
    from_gateway: bool = False
    #: Raw extension payload, preserved for forward compatibility. Tool
    #: handlers SHOULD access named fields above; this exists for gateways
    #: or diagnostics that need the original dict. Exposed as a read-only
    #: ``MappingProxyType`` so shared references cannot mutate the state
    #: observed by peer sessions in a pooled topology — ``frozen=True`` on
    #: the dataclass only blocks attribute *reassignment*, not mutation of
    #: mutable field contents.
    raw: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def from_meta(cls, meta: Any) -> "CallerContext | None":
        """Parse ``params._meta`` into a ``CallerContext`` if the extension
        block is present and well-formed. Returns ``None`` otherwise.

        Unknown schema versions are accepted additively: v1 consumers read
        only v1 fields, ignoring unknown keys. This future-proofs against
        gateway upgrades that add fields before backends catch up.
        """
        if not isinstance(meta, dict):
            return None
        block = meta.get(CALLER_META_KEY)
        if not isinstance(block, dict):
            return None
        schema_v = block.get("schemaVersion")
        if not isinstance(schema_v, int) or schema_v < 1:
            return None
        session_key = block.get("sessionKey")
        if not isinstance(session_key, str) or not session_key:
            # sessionKey is the only required field — without it the
            # extension is useless and we treat as absent.
            return None
        return cls(
            session_key=session_key,
            session_type=str(block.get("sessionType") or "unknown"),
            principal_id=str(block.get("principalId") or ""),
            channel_id=str(block.get("channelId") or ""),
            from_gateway=True,
            raw=MappingProxyType(dict(block)),
        )

    @classmethod
    def from_env(cls) -> "CallerContext":
        """Synthesize a context from the ``KIROCREW_SESSION_KEY`` env var, or
        the warm-pool PID file if env is absent.

        Used when the gateway does not inject the extension — i.e., per-session
        deployments, legacy topology, or a gateway that pre-dates this
        extension. Returns a context with ``from_gateway=False``; tool
        handlers can log or sample this to detect topology regressions.

        Fallback order:
          1. ``KIROCREW_SESSION_KEY`` env var (primary, per-session mode)
          2. ``config_dir() / session_pid_{parent_pid}.txt`` (warm-pool mode,
             where kiro-cli is pre-spawned with no key and rekey()+PID file
             provides the mapping once the session is claimed)

        When neither source provides a key, returns an empty-key context.
        Downstream code decides whether to reject (pooled backends should)
        or fall through (session-key-agnostic tools).
        """
        global _FROM_ENV_CACHE
        if _FROM_ENV_CACHE is not None:
            return _FROM_ENV_CACHE
        sk = os.environ.get("KIROCREW_SESSION_KEY", "")
        source = "env"
        if not sk:
            try:
                # circular import: config.loader imports mcp_caller top-level
                # for CallerContext typing; we can only reach config_dir here.
                from kiro_crew.config.loader import config_dir
                from kiro_crew.session_pid_sig import read_session_pid_txt

                # Walk the FULL ancestor chain, not
                # just os.getppid(). The tree can be gateway -> kiro-cli (has
                # the session_pid file) -> kiro-cli-chat (forked child) -> MCP
                # server, so the immediate parent usually has no PID file.
                # Mirrors mcp_core._resolve_session_key; a single-parent check
                # silently loses caller identity in deep process trees.
                cfg_dir = config_dir()
                # Sandbox launcher exports its own HOST pid — the exact pid
                # the gateway keys session_pid files by. Checking it directly
                # works even when this process's /proc view of pids diverges
                # from the host's (PID-namespace sandboxing), where the
                # ancestor walk below can never match.
                # Reads go through session_pid_sig's hardened reader (symlink
                # refusal, regular-file check, size bound) — same read
                # discipline as the strict verifier, minus the signature
                # requirement.
                host_pid = os.environ.get("KIROCREW_HOST_PID", "")
                if host_pid.isdigit():
                    sk = read_session_pid_txt(host_pid, cfg_dir)
                    if sk:
                        source = "pidfile"
                if not sk:
                    pid = os.getppid()
                    seen: set[int] = set()
                    while pid > 1 and pid not in seen:
                        seen.add(pid)
                        sk = read_session_pid_txt(pid, cfg_dir)
                        if sk:
                            source = "pidfile"
                            break
                        pid = _parent_pid(pid)
            except Exception:
                pass
        result = cls(session_key=sk, session_type=source, from_gateway=False)
        if sk:
            # Cache only a RESOLVED identity: an empty result is left uncached
            # so a warm-pool session claimed after the first call can still
            # resolve once its pidfile appears.
            _FROM_ENV_CACHE = result
        return result


# --- Backend capability advertisement ---------------------------------------


def caller_identity_capability() -> dict[str, Any]:
    """Return the capability block backends should include under
    ``capabilities.experimental`` in their ``initialize`` response to
    advertise support for pooled, caller-identity-aware operation.

    Usage in ``run_mcp_stdio_loop``::

        "capabilities": {
            "tools": {"listChanged": False},
            "experimental": caller_identity_capability(),
        }
    """
    return {CALLER_CAPABILITY_KEY: {"schemaVersion": CALLER_SCHEMA_VERSION}}


# --- Gateway injection helper (for tests and Python-side gateways) ---------


def build_caller_meta(ctx: CallerContext) -> dict[str, Any]:
    """Build the ``_meta`` block a gateway should inject into a forwarded
    ``tools/call`` request. Exposed here so the gateway — or a test that
    simulates the gateway — produces exactly the same wire format backends
    expect to parse.

    Returns a dict suitable for placement at ``params._meta`` in the
    JSON-RPC request.
    """
    return {
        CALLER_META_KEY: {
            "schemaVersion": CALLER_SCHEMA_VERSION,
            "sessionKey": ctx.session_key,
            "sessionType": ctx.session_type,
            "principalId": ctx.principal_id,
            "channelId": ctx.channel_id,
        }
    }
