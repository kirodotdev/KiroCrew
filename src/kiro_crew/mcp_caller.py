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
               "principalId": "alice",
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

   Gatewayd inspects this capability at backend boot and uses it to decide
   whether to INJECT the caller block. It does NOT decide pooling: a backend
   that never advertises is pooled all the same, and simply never receives an
   identity -- pooled and identity-blind, which is worse than either alone.
   The "refuse to pool an unadvertised backend" behaviour described in earlier
   revisions of this docstring was never implemented; ``rewriter.py`` records
   the same fact next to ``UNPOOLABLE_SERVERS``, which is empty and remains the
   only mechanism of its kind. A backend that cannot consume the block must be
   listed there.

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
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from kiro_crew import platform_compat

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
#: and ancestor pidfile chain are immutable once present, so the walk need run
#: at most once. It no longer forks ``ps`` per ancestor (``_parent_pid``
#: delegates to ``platform_compat.get_ppid``), but it is still a chain of file
#: reads or syscalls on a hot path. Only a non-empty result is cached, so a
#: warm-pool session claimed after the first call can still resolve once its
#: pidfile appears (see ``from_env``).
_FROM_ENV_CACHE: "CallerContext | None" = None


def _parent_pid(pid: int) -> int:
    """Best-effort parent-PID lookup. Returns 0 on failure so an ancestor walk
    terminates safely.

    Delegates to :func:`kiro_crew.platform_compat.get_ppid`, which reads
    ``/proc/<pid>/status`` on Linux, ``libproc.proc_pidinfo`` on macOS and
    ``CreateToolhelp32Snapshot`` on Windows -- none of which spawns a process.

    Avoiding a ``ps`` fork per ancestor matters here: this walk runs on the
    stub's Register path (bounded at ten levels) and on every iteration of the
    recaller poll, whose backoff caps at 30s but does not terminate while the
    session key is unresolved -- and an unresolved key is exactly the condition
    that starts the poll, so it cannot be served from the non-empty-only cache.
    ``get_ppid`` also resolves on Windows, where ``ps`` does not exist.

    ``get_ppid`` reports failure as ``-1``; normalise it to ``0`` so the walk
    loops (``while pid > 1``) and the callers' guards behave correctly.
    """
    ppid = platform_compat.get_ppid(pid)
    return ppid if ppid > 0 else 0


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


# --- Per-call current caller (stdio-loop dispatch state) --------------------
#
# ``run_mcp_stdio_loop`` dispatches at most ONE tool call at a time (a single
# worker thread, joined before the next dispatch). The loop sets this from
# the request's verified ``params._meta`` block immediately before invoking
# the tool and clears it in the dispatch ``finally`` — tool handlers (and the
# identity resolvers in ``mcp_core``) read it via :func:`current_caller` as
# the AUTHENTICATED per-call identity in the pooled topology, where env-var
# identity is wrong-by-construction (one shared backend, many sessions).
#
# Held in a ``contextvars.ContextVar`` rather than a bare module global: a
# security identity must not depend on the "dispatch is sequential" invariant
# alone — if dispatch ever becomes concurrent, each thread/task context reads
# its own value instead of bleeding another session's identity. Set and read
# happen in the same thread today (the worker
# sets it at its own start), so behavior is unchanged.
#
# Trust: in the pooled topology gatewayd strips any stub-supplied
# ``kirocrew.caller`` block from every inbound frame (``backend.py``
# strip-on-forward + the initialize forge guard) and injects its own, built
# from the uid-gated claim-push at ``rekey()`` — so a block seen here is
# gateway-authored. In the non-pooled stdio topology no client sends the
# block and this stays ``None`` (callers fall back to env/HMAC-pid sources).

_CURRENT_CALLER: ContextVar["CallerContext | None"] = ContextVar(
    "kirocrew_current_caller", default=None
)


def set_current_caller(ctx: "CallerContext | None") -> None:
    """Install (or clear, with ``None``) the current call's verified caller.

    ONLY the stdio dispatch loop may call this — tool code must treat the
    slot as read-only via :func:`current_caller`.
    """
    _CURRENT_CALLER.set(ctx)


def current_caller() -> "CallerContext | None":
    """The gateway-injected caller identity for the tool call in flight.

    ``None`` when the gateway did not inject the extension (non-pooled
    topology, legacy gateway) — callers must fall back to their env/PID
    resolution paths.
    """
    return _CURRENT_CALLER.get()
