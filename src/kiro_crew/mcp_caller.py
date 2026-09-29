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

A ``KIROCREW_SESSION_KEY`` environment variable baked in at spawn time cannot
carry this identity under pooling: one backend serves many sessions but sees
only the first session's env var.

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

import logging
import os
import secrets
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from kiro_crew import platform_compat
from kiro_crew.session_token_sig import session_key_from_env_token

logger = logging.getLogger(__name__)

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

#: Namespaced key placed inside ``params._meta`` carrying a per-CONNECTION
#: nonce, on every forwarded request — including the ones the gateway cannot
#: attach an identity to.
#:
#: This is deliberately NOT part of the caller block, and the distinction is the
#: whole point: the caller block is an IDENTITY (who is calling, used for
#: routing callbacks and for authorization), while this is only a SEPARATOR (two
#: calls carrying different nonces came from different stub connections). A
#: backend that needs distinct per-tenant state for callers the gateway could
#: not name must key that state on the nonce; it must never present the nonce as
#: attribution, and no identity resolver reads it.
#:
#: Why a nonce is needed at all: a backend serving an unnamed caller falls back
#: to a per-PROCESS namespace, which separates sessions exactly as far as the
#: 1:1 shim topology makes them separate processes. On a POOLED backend one
#: process serves N connections, so that fallback collapses every unnamed
#: co-tenant onto one namespace.
TENANT_META_KEY = "kirocrew.tenant"

#: Schema version of the tenant block. Same additive rule as the caller block.
TENANT_SCHEMA_VERSION = 1

#: Bytes of entropy in a minted nonce. It is a namespace separator, not a
#: capability, so this only has to make an accidental collision impossible;
#: 64 bits does that for any number of connections a gateway will ever hold.
_TENANT_NONCE_BYTES = 8

#: Servers whose POOLED separation rests on the nonce above, by name.
#:
#: Membership is a statement about the BACKEND: for a caller the gateway cannot
#: name, this server keeps per-tenant state and separates it by the nonce, so one
#: pooled process serving N unnamed connections is separated only while a nonce
#: keeps arriving. ``kirocrew-computer`` is the case — its
#: ``_unresolved_session_key`` composes ``unresolved:<pid>#<nonce>`` and hands
#: that to ``SnapshotIndex``, so without the nonce half every unnamed co-tenant
#: of one pooled process holds a single namespace.
#:
#: The stub reads this to refuse the one combination nothing downstream can
#: detect: pooling asked for, and a serving daemon that mints no nonce (see
#: ``mcp_gateway.stub.must_degrade_nonce_blind``). Absence of the tenant block is
#: ambiguous at the backend by construction — for an unnamed caller it is also
#: what a 1:1 topology with no gateway at all looks like, and there the
#: per-process fallback is correct — so the judgement has to be made on the
#: handshake, where the daemon's own attestation is readable.
#:
#: A NAME set, like the discovery classification: the stub decides before any
#: backend module has been imported, and importing one to ask would put package
#: code on the handshake path.
POOLING_REQUIRES_TENANT_NONCE: frozenset[str] = frozenset({"kirocrew-computer"})

#: Process-lifetime cache of a resolved PER-PROCESS ``from_env()`` identity —
#: the env var and the pid mapping only. The pidfile chain is immutable once
#: present, so that walk need run at most once, and it is a chain of file reads
#: or syscalls on a hot path (``_parent_pid`` delegates to
#: ``platform_compat.get_ppid``, so no ``ps`` fork per ancestor).
#:
#: The two rungs ABOVE it are deliberately excluded, and no cache key can
#: substitute for excluding them. A per-session token is rekeyed by rewriting
#: the mapping body while the token STRING survives, so every value derived
#: from the environment is byte-identical across the rekey a cache must notice;
#: keying on the token would therefore serve the pre-rekey session key for the
#: life of the process, which is the cross-session misattribution this module
#: exists to prevent. ``session_key_from_env_token`` states the same rule from
#: its own side: never memoise that answer.
#:
#: Only a non-empty result is cached, so a warm-pool session claimed after the
#: first call can still resolve once its pidfile appears (see ``from_env``).
_FROM_ENV_CACHE: "CallerContext | None" = None

#: Session sources, in ladder order. ``source`` doubles as
#: ``CallerContext.session_type``, so these strings are wire-visible.
SOURCE_PROTECTED = "protected-pid"
SOURCE_TOKEN = "token"
SOURCE_ENV = "env"
SOURCE_PIDFILE = "pidfile"

#: The ``skip_pid_mapping`` sentinel: rungs 1-3 neither answered nor refused.
#: It is a SOURCE rather than an empty key because rung 1's refusal is itself an
#: empty key, so emptiness cannot tell "no private binding" from "a binding that
#: exists and is invalid" — and the second must never fall through to a weaker
#: source. Never reaches the wire: the only caller replaces it with rung 4's
#: answer.
SOURCE_UNRESOLVED = "unresolved"

#: Co-tenant refusals already reported, so a resolver on a hot path (the
#: recaller poll does not terminate while the key is unresolved) logs the
#: operator-facing line once per pid per process rather than per call.
_reported_co_tenancy: set[int] = set()
_report_lock = threading.Lock()


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


# --- The one client-side identity ladder ------------------------------------


@dataclass(frozen=True)
class OwnIdentity:
    """This process's own session identity, and where it came from.

    ``session_key`` is empty when nothing could name the session.
    ``source`` names the rung that answered and is what ``CallerContext``
    reports as ``session_type``. ``failed`` marks an answer that CANNOT BE ACTED
    ON, which its one consumer — the managed-tool-policy lookup — turns into a
    refusal instead of a permissive empty exclusion set. Two unrelated things
    qualify, and both are empty keys: the resolution machinery breaking, and a
    positive answer that several sessions live on this process so none of them is
    singled out. What separates them from an ordinary empty key is that an
    ordinary one means "nothing has named me YET" — a warm-pool session about to
    be claimed, which must keep working — where these two mean the question has
    no answer this process can use.

    A pid mapping's own reason for declining is deliberately NOT carried here.
    It has no consumer: the one action it would drive, the operator-facing
    co-tenancy warning, is logged where the reason is read, so re-exporting it
    would be a field every caller ignores.
    """

    session_key: str = ""
    source: str = SOURCE_ENV
    failed: bool = False


def resolve_own_identity(
    *, consult_protected_binding: bool = True, skip_pid_mapping: bool = False
) -> OwnIdentity:
    """Resolve the session THIS process belongs to, from its own environment.

    The single client-side ladder. Every client-side consumer shares it —
    ``mcp_core``'s lenient path, the managed-tool-policy lookup in
    ``mcp_shared``, and the stub's register block through this module — so the
    rungs and their order cannot drift between them, and a change to the process
    tree (a pid namespace, a runtime hosting several sessions) is modelled once
    rather than in per-file mocks that each encode their author's assumptions.

    Rungs, strongest first. The order is load-bearing, not arbitrary:

    1. The protected member binding for this process, when the caller asks for
       it. ``None`` means no private binding; an EMPTY string means a record
       that exists and is invalid, which is a REFUSAL rather than an absence and
       must never fall through to a token, an env var or a pid file — each is
       writable by the same uid the binding exists to fence.
    2. The signed per-SESSION token on this process's own element. Above the env
       var because a warm-pool rekey makes the env stale, and per SESSION where
       every rung below answers per PROCESS.
    3. ``KIROCREW_SESSION_KEY``.
    4. The ``session_pid_<pid>.txt`` mapping, by the launcher-exported
       ``KIROCREW_HOST_PID`` and then by ancestor walk.

    Rung 4 is the one that cannot express the tree it reads. A pid names a
    PROCESS, and one kiro-cli process hosts many ACP sessions, so on a shared
    runtime the mapping holds one of several keys and the walk SUCCEEDS with
    whichever session published last. That is why the mapping records its
    tenant set and why this refuses instead of answering there: a wrong name is
    worse than no name, because the caller cannot tell it is wrong. Callers
    that reach rung 4 at all get a warning, since a per-process answer for a
    per-session question is a degraded result even when it is right.

    ``skip_pid_mapping`` stops after rung 3 and reports an absence instead of
    walking. It exists so a caller may place a process-lifetime memo UNDER the
    session-scoped rungs: rung 4 is both the only expensive rung and the only
    one whose answer is stable for the life of the process, so it is the only
    one worth memoising — and a memo in FRONT of rung 2 would outlive the rekey
    rung 2 exists to observe, since a rekey rewrites the token's mapping while
    the token string itself survives. The absence is reported as
    :data:`SOURCE_UNRESOLVED`, NOT as an empty key: rung 1's refusal is an empty
    key as well, and a caller that read emptiness as "keep going" would fall
    through the one rung that must never be fallen through.

    Never raises: an identity source that can raise turns a resolvable session
    into a crashed tool call.
    """
    try:
        if consult_protected_binding:
            # This is an ordinary MCP identity extension only. Memory V2 does
            # not use PID ancestry, namespaces, or proof records to authorize a
            # store.
            try:
                from kiro_crew.member_memory_auth import protected_member_session_for_pid

                protected = protected_member_session_for_pid(os.getpid())
            except Exception as exc:
                # A probe that RAISED is the machinery breaking, not a verdict.
                # It still stops the ladder rather than falling through to a
                # weaker source, because the record it could not read may be
                # the refusal in rung 1 — and because `KIROCREW_SESSION_KEY`,
                # the signed token and the pid mapping are all writable by the
                # same uid this binding exists to fence, so re-reading identity
                # from them after a failed binding read would let the fenced
                # process re-identify as the ambient session by making its OWN
                # record unreadable (chmod needs only ownership). A host
                # override that can tell an EXPECTED sandbox deny (Seatbelt)
                # from an induced one returns ``None`` for it — see the
                # contract on ``protected_member_session_for_pid``.
                logger.warning(
                    "protected_member_session_for_pid probe failed (%s); refusing the "
                    "protected identity rather than falling through to token/env",
                    exc,
                )
                return OwnIdentity(source=SOURCE_PROTECTED, failed=True)
            if protected is not None:
                return OwnIdentity(session_key=protected, source=SOURCE_PROTECTED)
        try:
            from_token = session_key_from_env_token()
        except Exception:
            from_token = ""
        if from_token:
            return OwnIdentity(session_key=from_token, source=SOURCE_TOKEN)
        env_key = os.environ.get("KIROCREW_SESSION_KEY", "")
        if env_key:
            return OwnIdentity(session_key=env_key, source=SOURCE_ENV)
        if skip_pid_mapping:
            return OwnIdentity(source=SOURCE_UNRESOLVED)
        return _identity_from_pid_mapping()
    except Exception:
        return OwnIdentity(failed=True)


def _identity_from_pid_mapping() -> OwnIdentity:
    """Rung 4: the gateway-published pid mapping, by host pid then by ancestry.

    Reads go through ``session_pid_sig``'s hardened reader (symlink refusal,
    regular-file check, size bound) — the same read discipline as the strict
    verifier, minus the signature requirement.

    The sandbox launcher exports its own HOST pid, the exact pid the gateway
    keys mappings by, so that direct lookup is tried first: it works even when
    this process's ``/proc`` view of pids diverges from the host's
    (PID-namespace sandboxing), where the ancestor walk can never match.

    The walk covers the FULL chain rather than ``os.getppid()`` alone: the tree
    can be gateway -> kiro-cli (has the mapping) -> kiro-cli-chat (forked child)
    -> MCP server, so the immediate parent usually has no mapping. It stops at
    the first ancestor whose mapping ANSWERS — see :func:`_mapping_answers` —
    and walks past one that does not, because only an answer is specific to
    this process: a co-tenant refusal names this pid's own sessions, while a
    malformed or recycled file names nobody and would strand the walk short of
    the ancestor holding the real mapping.

    Passing such a record does not DISCARD it. Two chains end with no key and
    they owe different answers: one that held a record this reader could not
    use, and one that held nothing. The second is the warm-pool session not yet
    claimed, and it must keep working; the first cannot be acted on, so it is
    reported as :attr:`OwnIdentity.failed` like any other unusable answer. The
    distinction only became expressible with a reader that CLASSIFIES its
    refusal — the string-returning reader this replaced collapsed both to ``""``
    — and without it a record in this same-uid directory turns a call whose
    exclusions the full walk enforces into one that enforces none.

    Never raises, and the guard lives HERE rather than at the call sites: the
    ladder function's own contract line promises it to every consumer, but this
    rung reaches `config_dir()`, which creates the data home and therefore
    raises on one that cannot be created. A caller that consults the rungs in
    two halves -- the memo has to sit between rung 3 and rung 4 -- holds the
    second half outside the ladder's own guard, so a guarantee attached to the
    ladder is not attached to this rung. Owning it here is what makes the
    promise true for every caller, including one added later.
    """
    # circular import: config.loader imports mcp_caller top-level for
    # CallerContext typing; we can only reach config_dir here.
    from kiro_crew.config.loader import config_dir
    from kiro_crew.session_pid_sig import REFUSAL_ABSENT, read_session_pid_mapping

    try:
        cfg_dir = config_dir()
        unusable = False
        host_pid = os.environ.get("KIROCREW_HOST_PID", "")
        if host_pid.isdigit():
            mapping = read_session_pid_mapping(host_pid, cfg_dir)
            if _mapping_answers(mapping):
                return _identity_from_mapping(int(host_pid), mapping)
            unusable = unusable or mapping.refusal != REFUSAL_ABSENT
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            mapping = read_session_pid_mapping(pid, cfg_dir)
            if _mapping_answers(mapping):
                return _identity_from_mapping(pid, mapping)
            unusable = unusable or mapping.refusal != REFUSAL_ABSENT
            pid = _parent_pid(pid)
    except Exception:
        # The machinery breaking, reported as such rather than as an absence:
        # `failed` is what the policy lookup turns into a refusal, and it is the
        # same value the ladder's outer guard produces, so the rung reached
        # through the ladder behaves exactly as before.
        return OwnIdentity(failed=True)
    if unusable:
        # Walking PAST a record is how this rung reaches the ancestor holding the
        # real mapping, but the record it passed is still evidence: a chain that
        # ends with no key, having held one this reader could not use, is not the
        # same as a chain that held nothing. Only the second is the warm-pool
        # "nothing has named me yet" that must keep working. Reporting the first
        # as an absence is what let a record in this same-uid directory turn an
        # enforced call into an unenforced one, because the permissive empty key
        # is not a class `tools/call` refuses on.
        return OwnIdentity(source=SOURCE_PIDFILE, failed=True)
    return OwnIdentity()


def _mapping_answers(mapping: Any) -> bool:
    """True when this pid's mapping answers the identity question AT ALL.

    Only two mappings do: one naming a session, and a co-tenant refusal, which
    is a real answer about THIS pid ("several sessions live here, so no single
    key names you") and must stop the walk rather than let it reach a different
    process's mapping.

    The other refusals answer nothing and MUST NOT stop it. ``.txt`` lives in
    the same-uid, agent-writable config dir, so any process could otherwise
    plant a two-line file at a nearer ancestor pid and halt the walk short of
    the kiro-cli ancestor that holds the real mapping. ``REFUSAL_RECYCLED`` is
    the same shape without the planting: the sweep deliberately leaves a stale
    file on disk for every live recycled pid.

    Not stopping is only half of it. The caller records that it passed such a
    record, so a chain that ends with no key at all still refuses rather than
    reporting the absence that ``tools/call`` proceeds on — see
    :func:`_identity_from_pid_mapping`. Answering here and refusing there are
    two different questions: this one asks whether the record describes THIS
    process, and only a named key or a co-tenant refusal does.
    """
    from kiro_crew.session_pid_sig import REFUSAL_CO_TENANT

    return bool(mapping.session_key) or mapping.refusal == REFUSAL_CO_TENANT


def _identity_from_mapping(pid: int, mapping: Any) -> OwnIdentity:
    """Turn one pid mapping into an identity, reporting what it cost.

    A named session is a DEGRADED answer even when correct — the mapping
    answers per process for a per-session question — so reaching it at all is
    worth a debug line naming the pid. A co-tenant refusal is worth an operator
    line: the element resolving here has no per-session token, which is a
    configuration fact (a ``fallback_exec``'d third-party backend, a
    caller-supplied ``mcpServers`` array) rather than a transient one, and
    without the reason an operator sees only a tool that lost its session.

    A co-tenant refusal reports ``failed``, which is what makes an authorization
    consumer refuse rather than proceed. The distinction that matters to such a
    consumer is not "did the machinery work" but "can this answer be acted on":
    an empty key that means "nothing has named me yet" is a warm-pool session
    about to be claimed and must not start denying calls, while an empty key that
    means "several sessions live here and none of them is singled out" cannot be
    acted on at all. Without this, the policy lookup read the second as the first
    and ran the call with an EMPTY exclusion set, so the operator's managed tool
    exclusions went unenforced on exactly the topology that produces this
    refusal. Signedness is not the test: the signature says who wrote the record,
    while this says the record cannot name one session either way.

    Reached only for a mapping that ANSWERS -- both call sites consult
    :func:`_mapping_answers` first -- so every path without a key is unusable and
    says so. That covers the co-tenant refusal and, fail-closed, any shape this
    function does not model; the warm-pool "nothing yet" answer never arrives
    here, because no mapping answered at all and the walk returns its own.
    """
    from kiro_crew.session_pid_sig import REFUSAL_CO_TENANT

    if mapping.session_key:
        logger.debug(
            "session identity resolved from the pid mapping for %d; this names the "
            "PROCESS, so it is correct only while that process hosts one session",
            pid,
        )
        return OwnIdentity(session_key=mapping.session_key, source=SOURCE_PIDFILE)
    if mapping.refusal == REFUSAL_CO_TENANT:
        _report_co_tenancy(pid, mapping.tenant_count)
    return OwnIdentity(source=SOURCE_PIDFILE, failed=True)


def _report_co_tenancy(pid: int, tenants: int) -> None:
    """Report a co-tenant refusal once per pid per process."""
    with _report_lock:
        first = pid not in _reported_co_tenancy
        _reported_co_tenancy.add(pid)
    if not first:
        logger.debug("pid %d still names %d sessions; identity still refused", pid, tenants)
        return
    logger.warning(
        "refusing to resolve a session identity from the pid mapping for %d: that "
        "process hosts %d ACP sessions, so the mapping names a co-tenant rather "
        "than this caller. This element carries no per-session token, which is "
        "what distinguishes sessions on a shared runtime. Tools needing an "
        "identity are refused here rather than attributed to another session. "
        "Repeat occurrences log at debug.",
        pid,
        tenants,
    )


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
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    #: The signed per-session token gatewayd forwards to Kiro Crew's OWN pooled
    #: control-plane backends (``kirocrew-core`` / ``kirocrew-cron``) so their
    #: loopback gateway requests can carry ``X-Session-Token``. gatewayd spawns
    #: a shared backend from its own environment, so the per-session env token
    #: never reaches it. Never forwarded to a third-party backend.
    session_token: str = ""
    #: Why gatewayd withheld ``session_token`` from a backend spawned under one
    #: of Kiro Crew's OWN server names (the ``_deny_control_plane`` reason:
    #: "spawned '/opt/local/bin/kirocrew' is not the spec's '…'"). Set only
    #: on the frames forwarded to such a denied backend, so its
    #: ``identity_unattested`` refusal can name the cause; the reason otherwise
    #: reaches only the daemon's own log, which no session surfaces
    #: (the Toolbox-shim report took three wrong diagnoses to find it). Diagnostic text,
    #: never a credential; empty everywhere else.
    identity_denial: str = ""

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
            session_token=str(block.get("sessionToken") or ""),
            identity_denial=str(block.get("identityDenial") or ""),
        )

    @classmethod
    def from_env(cls) -> "CallerContext":
        """Resolve the single-session environment/PID identity.

        Used when the gateway does not inject the extension — i.e., per-session
        deployments, legacy topology, or a gateway that pre-dates this
        extension. Returns a context with ``from_gateway=False``; tool
        handlers can log or sample this to detect topology regressions.

        The ladder itself is :func:`resolve_own_identity`, shared with every
        other client-side resolver so the rungs and their order cannot drift
        between them. This adds only the process-lifetime cache, and the cache
        is BELOW every session-scoped rung: the ladder is asked for rungs 1-3
        first, and the cache is reached only once they decline. That ordering,
        not the cache's key, is what keeps a warm-pool rekey visible — a rekey
        rewrites the token's mapping while the token STRING survives, so no
        value this process can read changes, and any cache consulted in front
        of the token would keep answering the pre-rekey session for the life of
        the process.

        Consulting the ladder in two halves puts rung 4 outside the ladder
        function's own guard, which is why that rung owns its never-raises
        guarantee itself: this method must return a context even when the data
        home cannot be resolved, because the stub's register path does not catch
        and a raise here would take the whole stub down instead of degrading.

        When no source provides a key, returns an empty-key context.
        Downstream code decides whether to reject (pooled backends should)
        or fall through (session-key-agnostic tools).
        """
        global _FROM_ENV_CACHE
        session_scoped = resolve_own_identity(skip_pid_mapping=True)
        if session_scoped.source != SOURCE_UNRESOLVED:
            # Rungs 1-3 ANSWERED, or rung 1 refused. Both are session-scoped
            # and neither is memoised. The test is the source rather than the
            # key, because rung 1's refusal is an empty key too and must stop
            # here rather than fall through to the pid mapping.
            return cls(
                session_key=session_scoped.session_key,
                session_type=session_scoped.source,
                from_gateway=False,
            )
        cached = _FROM_ENV_CACHE
        if cached is not None:
            return cached
        identity = _identity_from_pid_mapping()
        result = cls(
            session_key=identity.session_key,
            session_type=identity.source,
            from_gateway=False,
        )
        if identity.session_key:
            # Cache only a RESOLVED identity. An empty answer is left uncached
            # so a warm-pool session claimed after the first call can still
            # resolve once its pidfile appears, and so a co-tenant REFUSAL is
            # re-read -- and re-reported -- rather than frozen for the life of
            # the process. Rung 4 is the only rung that reaches here, so what
            # keeps a session-scoped answer out of the memo is the ordering
            # above, not a test on this value.
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
    meta = {
        CALLER_META_KEY: {
            "schemaVersion": CALLER_SCHEMA_VERSION,
            "sessionKey": ctx.session_key,
            "sessionType": ctx.session_type,
            "principalId": ctx.principal_id,
            "channelId": ctx.channel_id,
        }
    }
    if ctx.session_token:
        meta[CALLER_META_KEY]["sessionToken"] = ctx.session_token
    if ctx.identity_denial:
        meta[CALLER_META_KEY]["identityDenial"] = ctx.identity_denial
    return meta


def new_tenant_nonce() -> str:
    """Mint a fresh per-connection nonce.

    Minted by the GATEWAY, never derived from anything the stub sends. A stub
    supplies its own ``stub_uuid`` on the Register frame, so deriving the nonce
    from that value would let one stub choose to land in another unnamed
    co-tenant's namespace — re-creating the unnamed-co-tenant collision
    deliberately instead of by accident.
    """
    return secrets.token_hex(_TENANT_NONCE_BYTES)


def build_tenant_meta(nonce: str) -> dict[str, Any]:
    """Build the ``_meta`` block carrying a per-connection *nonce*.

    Separate from :func:`build_caller_meta` so the two can be injected
    independently: a connection the gateway cannot name has a nonce but NO
    identity, which is exactly the case that needs the separator.
    """
    return {
        TENANT_META_KEY: {
            "schemaVersion": TENANT_SCHEMA_VERSION,
            "nonce": nonce,
        }
    }


def tenant_nonce_from_meta(meta: Any) -> str:
    """Parse the per-connection nonce out of ``params._meta``, or ``""``.

    Returns ``""`` for every malformed shape rather than raising: a missing or
    unparseable nonce must degrade to the backend's own per-process fallback,
    not fail the call.
    """
    if not isinstance(meta, dict):
        return ""
    block = meta.get(TENANT_META_KEY)
    if not isinstance(block, dict):
        return ""
    schema_v = block.get("schemaVersion")
    if not isinstance(schema_v, int) or schema_v < 1:
        return ""
    nonce = block.get("nonce")
    if not isinstance(nonce, str):
        return ""
    return nonce


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

    Only a trusted dispatch boundary (stdio or an independently verified
    internal HTTP request) may call this. Tool implementations must treat the
    slot as read-only via :func:`current_caller`. HTTP dispatch restores the
    previous context in a finally block inside its request-scoped worker.
    """
    _CURRENT_CALLER.set(ctx)


def current_caller() -> "CallerContext | None":
    """The gateway-injected caller identity for the tool call in flight.

    ``None`` when the gateway did not inject the extension (non-pooled
    topology, legacy gateway) — callers must fall back to their env/PID
    resolution paths.
    """
    return _CURRENT_CALLER.get()


# Held in its own slot rather than on ``CallerContext`` because the case it
# exists for is precisely the one where there IS no ``CallerContext``: a
# connection the gateway could not name. Folding it into the identity object
# would have forced a context with an empty ``session_key`` into existence, and
# every ``if caller is not None`` in the tree reads that as "identity known".
_CURRENT_TENANT_NONCE: ContextVar[str] = ContextVar("kirocrew_current_tenant_nonce", default="")


def set_current_tenant_nonce(nonce: str) -> None:
    """Install (or clear, with ``""``) the current call's per-connection nonce.

    ONLY the stdio dispatch loop may call this, mirroring
    :func:`set_current_caller`.
    """
    _CURRENT_TENANT_NONCE.set(nonce)


def current_tenant_nonce() -> str:
    """The gateway-injected per-connection nonce for the tool call in flight.

    ``""`` when the gateway did not inject one (non-pooled topology, legacy
    gateway, or a backend that never advertised the caller extension). A backend
    that keys per-tenant state on this MUST keep working when it is empty — the
    1:1 topology, where the backend's own pid already separates sessions.

    NOT an identity: it names a connection, not a session, and nothing about it
    is attributable to a principal. Never write it into an audit record as the
    caller.
    """
    return _CURRENT_TENANT_NONCE.get()
