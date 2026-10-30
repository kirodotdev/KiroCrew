"""Which tools a session may list and call: the managed tool policy and the managed-tools test.

Holds ``_resolve_tool_policy``, which asks the gateway for the calling session's policy,
with its per-session cache, its failure backoff and the reasons an unresolved policy
refuses a call, ``ToolPolicy``, the identity notes recorded when a server runs without a
gateway identity, and ``serves_managed_tools``, which decides whether a server's tools
are Kiro Crew's own and so under the policy at all.

Split out of ``kiro_crew.mcp_shared``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.mcp_shared.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New tool-policy work goes here.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import types
import urllib.request
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple

from kiro_crew import platform_compat
from kiro_crew.config.loader import read_local_secret
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import current_caller, resolve_own_identity
from kiro_crew.mcp_cleanup import STALE_MANAGED_MCP_SERVERS
from kiro_crew.port_resolution import resolve_client_port_src
from kiro_crew.sel import sel
from kiro_crew.session_token_sig import session_key_from_env_token

#: The family logs as ``kiro_crew.mcp_shared``, the name callers and log filters use.
logger = logging.getLogger("kiro_crew.mcp_shared")


# ── Managed tool policy cache ──────────────────────────────────────────────
# Keyed per RESOLVED SESSION: in the pooled topology one backend process serves many
# sessions (per-call identity via the caller-meta extension), so a single
# process-global set would apply the FIRST session's policy — or a cached
# fail-open — to every other session.
#
# RESOLVED is the load-bearing word, and it is not the same as "the identity the
# gateway supplied". Keyed on the latter, every caller the gateway could not name
# shared ONE entry under the empty string, which put back the collapse the per-session
# keying exists to prevent: two sessions on one process (``spawn_run`` session sharing)
# inherited each other's tool policy, and a warm-pool session kept the policy of the
# session that held the process before its rekey for the life of the process — this
# cache has no TTL, so nothing expired it. The key is now the session the request was
# actually MADE for, which is the same value that rides its ``X-Session-Key``: one
# entry per session, and a rekeyed or subagent caller misses rather than inheriting.
# BOUNDED: a long-lived pooled backend serves churning sessions;
# FIFO-evict the oldest entry past the cap so the dict cannot grow without
# limit. Eviction only costs a re-fetch on that session's next call.
_EXCLUDED_TOOLS_CACHE_MAX = 256


_excluded_tools_by_session: dict[str, set[str]] = {}


# The include lists cached beside each session's exclusions, written and evicted
# with them, and read only on a hit in ``_excluded_tools_by_session``. A session
# whose policy narrows no server has no entry.
_included_tools_by_session: dict[str, dict[str, frozenset[str]]] = {}


# Two separate negative caches with different TTLs because the two conditions get
# OPPOSITE answers at ``tools/call``: the long HTTP-error window refuses, the short
# startup-race window stays permissive, so a brief race must never be answered out
# of the long window.
_last_failure_time: float = 0.0  # gateway unreachable / non-404 HTTP error


_last_startup_race_time: float = 0.0  # no session key or 404 — recovers fast


# The identity the short window was opened FOR: ``""`` when no session key could be
# resolved, or the resolved key whose policy request the gateway answered 404. The
# window debounces a race that belongs to ONE identity, and it is process-global,
# so without this a window opened by an unidentified ``tools/list`` would answer
# ``no_session_key`` for a ``tools/call`` that resolved a real key seconds later —
# and ``tools/call`` does not refuse on that reason, so an operator exclusion would
# go unenforced for the rest of the window. The token makes that transition
# ordinary: a mapping is published mid-window on every warm-pool claim.
_last_startup_race_key: str = ""


_failure_count: int = 0


# Class name of the exception behind the last ``resolution_failed``. A negative
# cache hit hands it back as the refusal's cause, so the agent and the audit log
# still name WHY while the long window is answering instead of the gateway.
_last_failure_class: str = ""


# Cap on the scrubbed exception message the audit event carries.
_FAILURE_MESSAGE_MAX = 200


# Long TTL applies only when the gateway is genuinely unreachable
# (HTTP errors other than 404, connection refused, timeout).  Kept short
# (60s) because this window is how long ``tools/call`` keeps REFUSING for a
# session that has never resolved its policy: a longer one holds the refusal
# well past the gateway's recovery, for the non-kiro-cli MCP hosts (Claude
# Code, custom hosts) this layer is the only enforcement point for.  60s is
# enough to debounce the 5s urlopen storm during a transient gateway outage
# without outliving it.
_NEGATIVE_CACHE_TTL: float = 60.0  # seconds


# Short TTL for the benign startup-race cases (no session key resolvable,
# or 404 "agent not resolved" because gateway hasn't registered the
# session yet).  Long enough to debounce the warning storm during a
# parallel MCP startup, short enough that we recover to deny-enforcing
# behavior within seconds once the session is registered.  This addresses
# the security-controls concern: don't keep fail-open active for
# 5 minutes when the underlying race resolves in milliseconds.
_STARTUP_RACE_CACHE_TTL: float = 5.0  # seconds


# After this many consecutive failures, suppress the warning log entirely
# (still emit a structured audit event).  The warnings are noise once the
# 404 root cause is established for the session.
_MAX_WARNING_FAILURES: int = 2


# The most of the gateway's 409 ``reason`` a refusal repeats to the caller. A
# reason names one file and one remedy -- a few hundred characters -- so the cap
# is generous for a real one and small enough that a pathological filename in
# the agents directory cannot turn one refused call into a kilobyte of echo.
_POLICY_DETAIL_MAX_CHARS: int = 600


#: The ``ToolPolicy.included`` of a policy that narrows no server.
_NO_INCLUDE: Mapping[str, frozenset[str]] = types.MappingProxyType({})


class ToolPolicy(NamedTuple):
    """The exclusion set for one session, plus whether it was actually read.

    ``excluded`` empty is ambiguous on its own: it means BOTH "the operator
    excluded nothing" and "we never got to look".  Those two demand opposite
    behaviour at a call site, so the reason we failed to look travels with the
    value instead of being flattened into an empty set.

    ``unresolved`` is the audit operation name of the path that gave up
    (``no_session_key``, ``agent_not_resolved``, ``policy_forbidden``,
    ``policy_unreadable``, ``resolution_failed``, or the cached form of one of
    them) and is ``""`` only when the gateway's policy endpoint actually
    answered with a policy this code understood.  Which reason it is decides
    what ``tools/call`` does -- see ``_UNRESOLVED_REFUSES_CALL`` -- so a new
    failure path must name its own reason rather than borrow one: borrowing
    inherits a decision that was made about a different condition, and every
    reason here that was ever collapsed into another one hid a different bug.

    ``detail`` is the gateway's own account of an unresolved reason, when it
    gave one -- the ``reason`` field of a ``409 policy_unreadable`` body, which
    names the spec file it could not read and what to do about it. Text only,
    never a decision: nothing reads it but the refusal message, so a caller
    that ignores it behaves exactly as before it existed. For
    ``resolution_failed`` it is the class name of the exception that stopped the
    read (``ConnectionRefusedError``), so the agent sees which failure it hit;
    the scrubbed message goes only to the audit event. Empty on every other path.

    ``included`` maps a server name to the only tools that server may list and
    run for this session. A server with no entry is not narrowed. The gateway
    derives it from an agent spec that opts in with
    ``managedToolPolicy.honourToolRefs`` and grants the server only through
    ``@<server>/<tool>`` refs, and answers an EMPTY entry for each server the spec
    names in ``managedToolPolicy.excludeServers`` (that server lists and runs
    nothing); every other spec answers no entry at all.
    """

    excluded: frozenset[str]
    unresolved: str
    detail: str = ""
    included: Mapping[str, frozenset[str]] = _NO_INCLUDE

    def include_for(self, server: str) -> frozenset[str] | None:
        """The include list *server* applies, or ``None`` when it is not narrowed."""
        return self.included.get(server)


def _ambient_audit_session() -> str:
    """The session an AUDIT record names when the call carried no gateway caller.

    Attribution only, never authorization: this feeds ``session_key=`` on SEL
    records for calls the gateway did not stamp. It reads the same source the
    policy lookup resolves by — the signed per-session token — before the env var,
    for the same reason: after a warm-pool rekey the env names the previous
    session, and a subagent sharing its parent's process carries its own token
    where the env names the parent. ``"mcp"`` is the pre-existing placeholder for
    a process with neither. Never raises; an audit fallback that could raise would
    turn a loggable call into a failed one.
    """
    try:
        from_token = session_key_from_env_token()
    except Exception:
        from_token = ""
    return from_token or os.environ.get("KIROCREW_SESSION_KEY", "mcp")


def spawned_without_gateway_identity() -> bool:
    """True when nothing in this process's environment says a gateway started it.

    A server the gateway starts for a session carries the signed per-session
    token on its MCP element (``KIROCREW_STUB_SESSION_TOKEN``), and a sandboxed
    one additionally carries the launcher's ``KIROCREW_HOST_PID``. A process with
    neither was started by something else -- an editor's own MCP config, a shell
    -- and has no channel through which it could ever prove a session identity.
    ``KIROCREW_SESSION_KEY`` is deliberately NOT consulted here: it is exactly the
    variable a person copies into an editor config by hand, so its presence says
    nothing about who spawned the process.

    Read-only and env-only, so the answer is the same before and after a refusal
    and never depends on the gateway being reachable.
    """
    # Deferred like session_token_sig's read of the same name: ``claim`` pulls in
    # the executor and transport graph, and this runs inside every stdio server,
    # which must not pay for it at import time.
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    if os.environ.get(STUB_SESSION_TOKEN_ENV, ""):
        return False
    if os.environ.get("KIROCREW_HOST_PID", "").isdigit():
        return False
    return True


def external_client_identity_note(server: str = "kirocrew-core") -> str:
    """The one explanation every identity refusal appends for an unspawned server.

    The same missing identity surfaces at three sites -- the tool-policy gate's
    ``identity_unattested`` refusal, the strict-identity diagnosis behind
    ``memory_recall`` and its siblings, and ``learn_add``'s pass-through of the
    gateway's ``missing X-Session-Key`` -- and a reader who started ``kirocrew-core``
    from an editor's MCP config sees a different sentence at each, none of which
    says the one thing they need: that server has no identity channel by design,
    and the env var they are tempted to set is not a credential. This text says
    it once, and every site appends the SAME string, so the three refusals agree.
    Refusal decisions are untouched: this decorates a denial, it never grants.

    Callers gate it on :func:`spawned_without_gateway_identity`, so a server the
    gateway did start (token on the element, or a launcher host pid) keeps its
    existing wording -- its problem is a trust root or a spawn denial, and this
    note would point it away from both.

    Two clauses are per-server. The read-only tools named are kirocrew-core's,
    so they appear only for it. The "leftover from an older install" clause
    holds only for the names Kiro Crew itself once wrote into the shared config
    (``mcp_cleanup.STALE_MANAGED_MCP_SERVERS``); an opt-in server found there
    is the user's own wiring, and calling it residue would invite a deletion
    ``clean_stale_managed_mcp`` itself refuses to make.
    """
    if server == "kirocrew-core":
        read_only = (
            " Remove it and the read-only tools (learn_list, local_knowledge_search) "
            "work without a session; use a Kiro Crew session (dashboard or a "
            "messaging channel) for the rest."
        )
    else:
        read_only = (
            " Remove it; use a Kiro Crew session (dashboard or a messaging channel) "
            "for the tools that need one."
        )
    if server in STALE_MANAGED_MCP_SERVERS:
        entry = (
            f", and a {server} entry in ~/.kiro/settings/mcp.json is leftover from an "
            f"older install (docs/architecture/mcp.md)."
        )
    else:
        entry = " (docs/architecture/mcp.md)."
    return (
        f" If this {server} was not started by a Kiro Crew session -- for example "
        f"it is listed in an editor's own MCP config such as ~/.kiro/settings/mcp.json "
        f"-- the tools that need a session identity (memory writes, memory recall, "
        f"session control) are not supported from it: only a server the gateway "
        f"starts for a session receives the signed session token that proves which "
        f"session is calling, and there is no user-settable substitute. "
        f"KIROCREW_SESSION_KEY is a fallback the gateway injects into its own "
        f"processes, not a credential: setting it by hand identifies nothing and makes "
        f"the gateway refuse every tool call from this server (identity_unattested)."
        f"{read_only} The supported direction for an editor is to connect INTO a "
        f"Kiro Crew session, not to spawn {server} itself{entry}"
    )


def _policy_session_key() -> str | None:
    """Resolve the session whose tool policy is being asked for, absent a gateway caller.

    Three outcomes, and they are deliberately distinct because the caller owes each
    one a different answer — the same discipline :class:`ToolPolicy` applies to the
    policy itself, one level down:

    * a session key — ask the gateway for that session's policy, and cache under it;
    * ``""`` — no identity on this install YET (a startup race), or an identity that
      was explicitly REFUSED (a protected member record that exists and is
      invalid). That is the ``no_session_key`` reason, which ``tools/call``
      deliberately PROCEEDS on, so nothing that must deny a call may land here;
    * ``None`` — the question has no answer this process can act on: resolution
      itself broke (an unreadable home, a raising probe), a pid names SEVERAL
      sessions so none of them is singled out, or the ancestor walk ended having
      passed a mapping it could not use. That is the ``resolution_failed`` class,
      kept distinguishable so a broken host is not reported as a benign race and
      does not inherit the race's short window.
      A raising ``protected_member_session_for_pid`` stays in THIS class: the probe
      is not caught here, because the same-uid fence means an unreadable binding
      must never be re-read as identity from token/env. A host override that can
      tell an expected sandbox deny from an induced one returns ``None`` for it.

    The ladder is :func:`kiro_crew.mcp_caller.resolve_own_identity`, shared with
    every other client-side resolver so the rungs and their order cannot drift
    between them. The gateway's per-call identity is NOT part of it: that is exact
    and stamped per CALL, so :func:`_resolve_tool_policy` uses it directly and
    never calls this.

    On an install with no token and no protected binding the ladder resolves
    through the env var and then the pid mapping, and costs exactly that. It
    stops short of naming a co-tenant on a shared runtime: a misresolved key
    applies one session's operator tool exclusions to another, so this lookup
    refuses there rather than guessing.

    This reads the ``KIROCREW_SESSION_KEY`` rung that is skipped when
    ``host_pid_is_shared`` is true. On a shared pid the rung resolves no key,
    ``identity.failed`` is set, ``None`` is returned, and
    ``_resolve_tool_policy`` raises into the ``resolution_failed`` path —
    which is in ``_UNRESOLVED_REFUSES_CALL``, so ``tools/call`` is refused
    rather than proceeding under a co-tenant's exclusion set.
    """
    identity = resolve_own_identity()
    return None if identity.failed else identity.session_key


def _http_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """The ``code`` and ``reason`` fields of a JSON error body, ``""`` for each absent one.

    The gateway's refusals carry ``{"error": ..., "code": "<reason>", "reason":
    "<what it could not read>"}``; the status alone is not enough to tell two
    of its 409s apart, and ``reason`` is the one line that names the spec file
    an operator has to fix. Never raises: an unreadable or non-JSON body is
    ``("", "")``, and the caller treats that as the status's historical
    meaning rather than guessing a narrower one. Only string fields are
    returned -- the body is wire data, and a ``reason`` of any other type is
    dropped, not coerced.
    """
    try:
        raw = exc.read()
    except Exception:
        return "", ""
    if not raw:
        return "", ""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    code = payload.get("code")
    reason = payload.get("reason")
    return (
        code if isinstance(code, str) else "",
        reason if isinstance(reason, str) else "",
    )


def _resolve_tool_policy(
    caller_session: str = "",
    *,
    caller_token: str = "",
    ignore_negative_cache: bool = False,
) -> ToolPolicy:
    """Query the gateway for the current session's managedToolPolicy.exclude.

    ``caller_session`` is the verified per-call identity from the gateway's
    caller-meta extension (pooled topology); when non-empty it takes
    precedence over every source in :func:`_policy_session_key`, so
    sessions sharing one backend cannot inherit each other's policy. The
    RESOLVED session keys the cache either way — the policy returned and the policy
    stored are then the same session's, which is what stops a caller the gateway
    could not name from inheriting a co-tenant's or its own pre-rekey policy.

    ``caller_token`` is that same caller block's ``sessionToken`` -- the signed
    per-session token gatewayd hands a pooled control-plane backend PER FRAME,
    because the backend was spawned from the daemon's environment and has no
    token of its own there. It is passed explicitly rather than read from
    :func:`current_caller` because the ``tools/call`` dispatch evaluates the
    policy BEFORE it installs the caller ContextVar (the check runs on the read
    loop; ``set_current_caller`` runs in the worker), so at this point the
    ContextVar is still empty. The gateway accepts a declared ``X-Session-Key``
    only behind an attestation, and a loopback TCP request has none but this
    header; without it a pooled backend's every read is answered
    ``member_identity_unavailable`` and every tool call is refused. The
    ContextVar remains the fallback for a caller that reaches this resolver
    with the caller already installed.

    Returns the set of tool names to hide from this session, together with the
    reason the policy could not be read when it could not. Caches on success
    only, so a session that has ever resolved its policy is served from
    ``_excluded_tools_by_session`` and never reaches a failure path again
    (until FIFO eviction) -- the unresolved states below are reachable only for
    a session whose policy has never been read once.

    On failure this returns an EMPTY exclusion set with ``unresolved`` set, and
    the two consumers read that differently on purpose:

    - ``tools/list`` shows the unfiltered list. Hiding every tool here would be
      unrecoverable: kiro-cli calls ``tools/list`` once per session and caches
      the answer, so an empty list persists for the session's whole life. A
      listing is not an enforcement point.
    - ``tools/call`` REFUSES, loudly and attributably. This is the only place a
      withheld deny can actually be exercised, and refusing one call costs a
      retry rather than a session.

    An empty exclusion set therefore never stands alone as a permission: the
    audit event that records the failure is also what makes the fail-closed
    choice available without guessing which tools the operator meant to deny.
    """
    global _last_failure_time, _last_startup_race_time, _last_startup_race_key
    global _failure_count, _last_failure_class
    # Resolve BEFORE the cache is consulted when the gateway did not name the caller:
    # the entry has to be found under the session this call is for, and only
    # resolution knows which that is. A named caller needs no resolution at all — its
    # key IS the cache key — so the pooled hot path keeps its single dict lookup.
    if caller_session:
        session_key: str | None = caller_session
    else:
        session_key = _policy_session_key()
    if session_key:
        _cached = _excluded_tools_by_session.get(session_key)
        if _cached is not None:
            _cached_include = _included_tools_by_session.get(session_key)
            if _cached_include:
                return ToolPolicy(
                    frozenset(_cached), "", included=types.MappingProxyType(_cached_include)
                )
            return ToolPolicy(frozenset(_cached), "")

    now = time.monotonic()
    _failure_cached = bool(_last_failure_time and (now - _last_failure_time) < _NEGATIVE_CACHE_TTL)
    _race_cached = bool(
        not caller_session
        and _last_startup_race_time
        and (now - _last_startup_race_time) < _STARTUP_RACE_CACHE_TTL
        and session_key == _last_startup_race_key
    )
    # Negative cache: avoid hammering gateway on persistent failures.
    # Silent during the cache window -- only the structured audit event is
    # emitted to keep gateway.log readable.  Two windows: a long one for
    # genuine HTTP/network failure, a short one for benign startup races.
    # The startup-race window debounces a race that belongs to ONE identity
    # -- no key resolvable yet, or a key the gateway has not registered yet --
    # so it answers only for the identity that opened it. A call that
    # resolved a DIFFERENT identity is past that race by definition: an
    # unidentified ``tools/list`` opening the window must not make the
    # ``tools/call`` that resolves a real key seconds later answer
    # ``no_session_key`` for a session whose key is in hand, and a session
    # the gateway answered 404 for must not silence a sibling it has
    # registered. A caller WITH a verified per-call identity skips it
    # outright, as before: it is past every startup race by construction.
    if not ignore_negative_cache and (_failure_cached or _race_cached):
        # WHICH clock fired is part of the answer, not an implementation
        # detail: the two windows cache different conditions and the call site
        # treats them differently. Collapsing them into one reason would repeat
        # the very conflation this resolver exists to undo, one level down.
        _reason = "resolution_failed" if _failure_cached else "no_session_key"
        sel().log_api_access(
            caller=session_key or "mcp",
            operation="tool_policy.negative_cache_hit",
            outcome="unresolved",
            source="mcp_shared",
            resources=f"reason={_reason}",
            error=_last_failure_class if _failure_cached else "",
        )
        return ToolPolicy(frozenset(), _reason, _last_failure_class if _failure_cached else "")

    try:
        port, _source = resolve_client_port_src(None)
        api_base = f"http://127.0.0.1:{port}"

        # Credential for the port this function DIALS (parsed just above), not for
        # whichever gateway an ambient lookup would name -- those can differ on a
        # multi-gateway host, which is the desync being closed. The v4 loopback
        # LITERAL (matching the dial) reaches one family, so a single-family
        # gateway (v4-only or a wildcard/container bind) still authenticates; the
        # ambiguous ``localhost`` would demand both families and 403 such a gateway.
        secret = ""
        try:
            secret = read_local_secret(port, dial_host="127.0.0.1")
        except Exception:
            pass

        if session_key is None:
            # Resolution itself broke rather than finding nothing. Raise into the
            # handler below so a broken host keeps the LONG window and the
            # resolution_failed reason it already had when this resolution was
            # inline, instead of being reported as the benign startup race.
            raise RuntimeError("tool-policy session resolution failed")

        if not session_key:
            # PATH 1/3. No session key resolvable (startup race -- kiro-cli
            # hasn't written the PID file yet, or the process is from the warm
            # pool).  The exclusion set stays empty so ``tools/list`` still
            # lists everything: kiro-cli calls tools/list once and caches the
            # result, so hiding all tools here would hide them for the whole
            # session, unrecoverably.  ``unresolved`` is what stops that empty
            # set from being read as a permission -- ``tools/call`` refuses
            # while it is set.  Short negative cache (5s) debounces the warning
            # storm during parallel MCP startup; the session_pid file typically
            # appears within a few hundred ms of MCP spawn.
            _last_startup_race_time = now
            _last_startup_race_key = ""
            sel().log_api_access(
                caller="mcp",
                operation="tool_policy.no_session_key",
                outcome="unresolved",
                source="mcp_shared",
            )
            return ToolPolicy(frozenset(), "no_session_key")

        # The declared key needs the attestation that goes with it. Without the
        # token this read is answered as a caller the gateway cannot name, the
        # policy stays unresolved, and the fail-closed branch below then refuses
        # every tool call for the session. The explicit argument wins: the
        # ``tools/call`` dispatch checks the policy before it installs the
        # caller ContextVar, so ``current_caller()`` is None exactly when the
        # pooled backend most needs the token (see the docstring).
        from kiro_crew.session_token_sig import session_token_header

        _tok = caller_token
        if not _tok:
            _ctx = current_caller()
            _tok = _ctx.session_token if _ctx is not None and _ctx.from_gateway else ""
        token_header = session_token_header(_tok)

        # A key resolved LOCALLY (``not caller_session``) from the lenient tail of
        # :func:`_policy_session_key` -- the unsigned ``session_pid`` read reached
        # through ``KIROCREW_HOST_PID`` and the ancestor walk -- carries no
        # attestation the gateway can check: ``session_token_header`` found no token
        # to send with it, and the key is not the gateway-injected
        # ``KIROCREW_SESSION_KEY``. Dialled anyway, that request is answered
        # ``member_identity_unavailable`` on EVERY call, so the round-trip is futile
        # and its ``identity_unattested`` refusal text misreports a dial that never
        # needed to happen. Skip the dial and return a reason WITHOUT the key on the
        # wire -- but a fail-CLOSED one.
        #
        # Fail closed is required, not optional: a session key DID resolve, so an
        # operator ``managedToolPolicy.exclude`` may exist for it, and an exclusion
        # this process could not read is an unknown deny, never a permission.
        # ``identity_unattestable`` is therefore in ``_UNRESOLVED_REFUSES_CALL``
        # beside ``identity_unattested``: ``tools/list`` still lists everything
        # (kiro-cli caches one listing, so hiding tools there is unrecoverable) and
        # ``tools/call`` refuses until a real identity channel (a gateway caller
        # block, a signed token) makes the key attestable. It is a distinct reason
        # from ``identity_unattested`` only so the refusal text does not claim a
        # gateway read that never happened.
        #
        # Two sources are deliberately NOT withheld. A gateway-stamped
        # ``caller_session`` is the gateway's own per-call identity -- it named this
        # caller, so the key is vouched for even when the per-call block carried no
        # token. ``KIROCREW_SESSION_KEY`` is the identity the gateway injects into its
        # own processes, which the attested topologies accept; a bare declared key
        # that the current transport cannot attest is the gateway's boundary to hold
        # on the dial, not a resolution this client should pre-empt.
        _has_env_key = bool(os.environ.get("KIROCREW_SESSION_KEY", ""))
        if not caller_session and "X-Session-Token" not in token_header and not _has_env_key:
            sel().log_api_access(
                caller=session_key,
                operation="tool_policy.unattestable_key",
                outcome="unresolved",
                source="mcp_shared",
                resources=f"session_key={session_key}",
            )
            return ToolPolicy(frozenset(), "identity_unattestable")

        headers: dict[str, str] = {"X-Internal-Secret": secret, **token_header}
        headers["X-Session-Key"] = session_key

        req = urllib.request.Request(
            f"{api_base}/api/session-tool-policy",
            headers=headers,
        )
        try:
            with loopback_urlopen(req, timeout=5) as resp:
                policy = json.loads(resp.read())
        except urllib.error.HTTPError as http_exc:
            if http_exc.code == 404:
                # PATH 2/3. 404 = "agent not resolved" (the gateway hasn't
                # registered this session yet -- common during MCP startup
                # before the session_pid file is fully visible across
                # processes).  A benign race, so the short startup-race cache
                # applies and the session recovers within seconds; until it
                # does, ``unresolved`` keeps ``tools/call`` refusing rather
                # than serving an exclusion set nobody read.  Critically, do
                # NOT log a stack trace for 404 -- it floods gateway.log on
                # every fresh subagent spawn.
                _last_startup_race_time = now
                _last_startup_race_key = session_key
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.agent_not_resolved",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "agent_not_resolved")
            if http_exc.code == 409:
                # Two different refusals share this status, told apart by the
                # body's ``code`` -- the status alone stopped meaning one thing
                # when the endpoint grew its attestation gate.
                _code, _reason = _http_error_body(http_exc)
                if _code == "member_identity_unavailable":
                    # ``internal_memory_scope`` declined to answer THIS caller:
                    # the declared ``X-Session-Key`` reached the gateway without
                    # an attestation (no ``X-Session-Token`` in the request, or
                    # one that names another key). Nothing about the spec was
                    # read. Reported as its own reason so the refusal names the
                    # missing token rather than the agents directory, and so a
                    # reader of the audit trail can tell "no token reached this
                    # call" from "the operator's spec is malformed". Not
                    # negative-cached, for the same reason as the spec case:
                    # the answer is immediate and specific to this caller.
                    # ``pid``/``ppid`` name WHICH process asked: a server
                    # launched without the token often lives for one
                    # ``tools/list`` and is gone before anyone can look, and
                    # its parent (gatewayd for a pooled backend, the harness
                    # for a server it launched itself) is what tells the
                    # launch paths apart afterwards.
                    sel().log_api_access(
                        caller=session_key,
                        operation="tool_policy.unattested",
                        outcome="unresolved",
                        source="mcp_shared",
                        resources=(
                            f"session_key={session_key},"
                            f"token={'present' if _tok else 'absent'},"
                            f"pid={os.getpid()},ppid={os.getppid()}"
                        ),
                    )
                    return ToolPolicy(frozenset(), "identity_unattested")
                # The gateway read a spec for this session and could not
                # determine its policy (unparseable, wrong shape, or two specs
                # claiming the name). An unparseable or code-less body takes
                # this arm too: it is the status the endpoint has always used
                # for that condition, and an unknown 409 must not be read as
                # anything narrower. Deliberately NOT negative-cached: the
                # windows above exist to avoid repeated 5s urlopen timeouts, and
                # this answer is immediate, so there is nothing to debounce. It
                # is also specific to THIS session's agent spec, and
                # ``_last_failure_time`` is process-global -- caching it there
                # would refuse tool calls for every sibling session in a pooled
                # backend over one agent's malformed file. Re-asking each call
                # costs one loopback round-trip and recovers the moment the
                # operator fixes the spec. The body's ``reason`` -- the file the
                # gateway could not read, and what to do -- rides along as
                # ``detail`` so the refusal can say it; the decision is the
                # status and code alone, exactly as before.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.unreadable",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "policy_unreadable", _reason)
            from kiro_crew.enterprise_gate import GATE_CLOSED_CODE

            if http_exc.code == 403 and _http_error_body(http_exc)[0] == GATE_CLOSED_CODE:
                # An enterprise gateway whose login gate is closed refuses every
                # route but sign-in, this read included. That is not a decision
                # about this caller: the exclusion list is unread, so the call is
                # refused (``gateway_locked`` is in ``_UNRESOLVED_REFUSES_CALL``)
                # until the gate reopens. Not cached, so it clears on the first
                # read after the gate opens.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.gateway_locked",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key}",
                )
                return ToolPolicy(frozenset(), "gateway_locked")
            if http_exc.code in (400, 403):
                # The gateway ANSWERED and declined to tell this caller. 403 is
                # ``member_session_unverified`` from ``internal_memory_scope``:
                # a session claiming a private memory store without a proof the
                # gateway can verify. 400 is a caller that named no session or a
                # rejected agent name. Both are authorization boundaries the
                # gateway holds deliberately, they answer instantly, and they
                # are the permanent steady state for a whole class of callers
                # rather than a window that closes.
                #
                # Kept OUT of the process-global failure cache for the same
                # reason as 409: the answer is immediate so there is nothing to
                # debounce, and it is specific to THIS caller's identity, so
                # caching it globally would deny sibling sessions over one
                # caller's missing proof.
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.forbidden",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key},status={http_exc.code}",
                )
                return ToolPolicy(frozenset(), "policy_forbidden")
            if 400 <= http_exc.code < 500:
                # Any OTHER 4xx. The gateway answered and made a decision about
                # this caller -- it is enforcing something, not failing to read
                # the policy -- so it joins the permissive, audited class with
                # the two above.
                #
                # This arm exists because the alternative is an enumerated list
                # of statuses, and a list is only as good as its author's
                # knowledge of the endpoint. A status nobody enumerated would
                # fall through to the failure catch-all and be refused, which
                # would deny a whole class of callers over a condition that is
                # not a failure at all. Deciding by CLASS is robust to the
                # endpoint growing a status this code has never seen.
                logger.warning(
                    "Tool policy endpoint answered %s for session %s; treating it "
                    "as a boundary the gateway holds, not a failure to read",
                    http_exc.code,
                    session_key,
                )
                sel().log_api_access(
                    caller=session_key,
                    operation="tool_policy.forbidden",
                    outcome="unresolved",
                    source="mcp_shared",
                    resources=f"session_key={session_key},status={http_exc.code}",
                )
                return ToolPolicy(frozenset(), "policy_forbidden")
            raise

        exclude = policy.get("exclude", [])
        # A policy the gateway ANSWERED is not automatically a policy this
        # understood. An absent ``exclude`` key is a real empty exclusion list
        # and stays resolved -- that is the ordinary case. But a present
        # ``exclude`` of the wrong shape, or one holding an entry that is not a
        # tool name, is a policy whose meaning is unknown: the operator wrote
        # something there and it cannot be read. Silently narrowing it to the
        # part that happens to parse would enforce a policy nobody wrote, which
        # is the same withheld deny as reading none at all. Neither shape occurs
        # in a valid config, so refusing costs no working caller. The gateway
        # takes the identical line one level up for a non-dict
        # ``managedToolPolicy``.
        if not isinstance(exclude, list) or any(not isinstance(t, str) for t in exclude):
            _shape = (
                "not a list"
                if not isinstance(exclude, list)
                else "a list holding a non-string entry"
            )
            logger.warning(
                "Tool policy for session %s has a malformed exclude (%s); "
                "refusing calls rather than enforcing the part that parses",
                session_key,
                _shape,
            )
            sel().log_api_access(
                caller=session_key,
                operation="tool_policy.unreadable",
                outcome="unresolved",
                source="mcp_shared",
                resources=f"session_key={session_key},exclude={_shape}",
            )
            # The one ``policy_unreadable`` this process diagnoses itself, so
            # its ``detail`` is its own: the refusal then says what is wrong
            # with the policy the gateway answered, the way the gateway's
            # ``reason`` says which file it could not read.
            return ToolPolicy(
                frozenset(),
                "policy_unreadable",
                f"the policy the gateway answered has a managedToolPolicy.exclude that is "
                f"{_shape}; fix the exclude list in this agent's spec, no restart needed",
            )
        resolved = set(exclude)
        include = _parse_policy_include(policy.get("include"))
        if include is None:
            logger.warning(
                "Tool policy for session %s has a malformed include; refusing calls "
                "rather than enforcing the part that parses",
                session_key,
            )
            sel().log_api_access(
                caller=session_key,
                operation="tool_policy.unreadable",
                outcome="unresolved",
                source="mcp_shared",
                resources=f"session_key={session_key},include=malformed",
            )
            return ToolPolicy(
                frozenset(),
                "policy_unreadable",
                "the policy the gateway answered has an include that is not a map of "
                "server names to lists of tool names",
            )
        # FIFO bound: dicts preserve insertion order; drop the oldest
        # session's entry when full (pooled backends serve churning sessions).
        while len(_excluded_tools_by_session) >= _EXCLUDED_TOOLS_CACHE_MAX:
            _evicted = next(iter(_excluded_tools_by_session))
            _excluded_tools_by_session.pop(_evicted)
            _included_tools_by_session.pop(_evicted, None)
        _excluded_tools_by_session[session_key] = resolved
        if include:
            _included_tools_by_session[session_key] = include
            return ToolPolicy(
                frozenset(resolved), "", included=types.MappingProxyType(dict(include))
            )
        _included_tools_by_session.pop(session_key, None)
        return ToolPolicy(frozenset(resolved), "")
    except Exception as exc:
        # PATH 3/3. The gateway did not give a usable answer: no answer at all
        # (network error, timeout, connection refused), or a 5xx saying it is
        # broken. Every 4xx returns above, decided by CLASS rather than by an
        # enumerated list, so reaching here means the gateway could not read the
        # policy -- never that it made a decision about this caller.
        #
        # That single meaning is what the refusal recorded at
        # ``_UNRESOLVED_REFUSES_CALL`` rests on: the exclusion set here is unknown,
        # not empty. The LONG negative cache avoids repeated 5s urlopen blocks
        # across many MCP servers while the gateway is down, and it also bounds how
        # long the refusal lasts. That
        # cache is process-global, which is sound HERE and nowhere else: a gateway
        # this process cannot reach is unreachable for every session in it, so
        # there is no sibling the window wrongly affects.
        _last_failure_time = time.monotonic()
        _failure_count += 1
        _last_failure_class = exc.__class__.__name__
        # Suppress repeated warnings — once we've logged twice the operator
        # has all the diagnostic info and further entries flood gateway.log
        # at every MCP server startup (10+ servers × every session start).
        if _failure_count <= _MAX_WARNING_FAILURES:
            logger.warning(
                "Tool policy resolution failed (%s); this session's exclusions are "
                "unknown and tool calls are REFUSED for up to %.0fs",
                exc.__class__.__name__,
                _NEGATIVE_CACHE_TTL,
                exc_info=True,
            )
        elif _failure_count == _MAX_WARNING_FAILURES + 1:
            logger.warning(
                "Tool policy resolution still failing — further warnings suppressed; "
                "see audit log for tool_policy.resolution_failed events",
            )
        sel().log_api_access(
            caller=session_key or "mcp",
            operation="tool_policy.resolution_failed",
            outcome="unresolved",
            source="mcp_shared",
            resources=f"error_class={_last_failure_class}",
            error=_failure_cause(exc),
        )
        return ToolPolicy(frozenset(), "resolution_failed", _last_failure_class)


def _parse_policy_include(raw: Any) -> dict[str, frozenset[str]] | None:
    """The ``include`` of a policy the gateway answered, or ``None`` when it is malformed.

    Absent is an empty map: no server is narrowed. Present, it must map server
    names to lists of tool names. Any other shape is a narrowing whose extent is
    unknown, and the caller refuses calls on it rather than enforcing the part
    that parses, the same line it takes for a malformed ``exclude``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    out: dict[str, frozenset[str]] = {}
    for server, tools in raw.items():
        if not isinstance(server, str) or not server:
            return None
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            return None
        out[server] = frozenset(tools)
    return out


def _failure_cause(exc: BaseException) -> str:
    """``Class: message`` for an audit event -- redacted, one line, capped.

    The message can carry a URL, a path or a header value, so it goes through
    the log redactor before it is written. A redactor that cannot run withholds
    the message and keeps the class: the class alone is what names the cause.
    """
    name = exc.__class__.__name__
    try:
        from kiro_crew.platform import redact_log_via_context

        message = redact_log_via_context(" ".join(str(exc).split()))
    except Exception:
        message = "<withheld: redaction unavailable>"
    if len(message) > _FAILURE_MESSAGE_MAX:
        message = message[: _FAILURE_MESSAGE_MAX - 3] + "..."
    return f"{name}: {message}" if message else name


def _resolve_excluded_tools(caller_session: str = "") -> set[str]:
    """Return the ordinary tool-policy exclusions for compatibility callers.

    The policy resolver keeps the unresolved reason so ``tools/call`` can retain
    its fail-closed behavior. Diagnostics that only need exclusions use this
    narrow projection; it carries no member-memory capability or proof.
    """
    return set(_resolve_tool_policy(caller_session).excluded)


# Which unresolved reasons refuse a ``tools/call``.
#
# The test is not how bad the reason sounds. It is whether the reason means ONE
# thing, because a security decision derived from an ambiguous reason is wrong
# for half the callers it hits. Four reasons qualify, and each means "an operator
# exclusion may exist and this system could not read it":
#
# * ``policy_unreadable`` -- the gateway found a spec for this session and could
#   not determine its policy. It emits this for that condition and nothing else.
# * ``identity_unattested`` -- the gateway would not read the policy for THIS
#   caller because the declared session key arrived without the attestation
#   that goes with it (no ``X-Session-Token``, or one naming another key). The
#   spec was never consulted, so whatever it excludes is unknown here. Refusing
#   is the same withheld-deny argument as the line above; the reason is kept
#   separate so the refusal names the missing token, not the agents directory.
#   A legitimate pooled backend never lands here: it is handed the token per
#   frame in the caller block and ``_resolve_tool_policy`` sends it.
# * ``identity_unattestable`` -- a session key resolved from the lenient tail of
#   ``_policy_session_key`` (the unsigned ``session_pid`` read, the ancestor walk)
#   with no token to carry it and no gateway-injected ``KIROCREW_SESSION_KEY``.
#   The dial is skipped because the gateway would refuse it, so the exclusion
#   list is unread for a key that DID resolve -- the same unknown-deny as
#   ``identity_unattested``, before the request rather than after it. Kept
#   separate only so the refusal text does not claim a gateway read that never
#   happened.
# * ``gateway_locked`` -- an enterprise gateway's login gate is closed and it
#   answered the read with 403 ``enterprise_gate_closed``. The policy was never
#   consulted, so the exclusion set is unknown; the 403 is the gate's, not a
#   decision about this caller, so it is kept out of ``policy_forbidden``.
# * ``resolution_failed`` -- no usable answer reached this process: nothing came
#   back, the gateway answered ``5xx`` to say it is broken, or the resolve itself
#   raised. Every ``4xx`` returns before that arm, decided by status CLASS, so this
#   reason means the policy could not be READ and never that the gateway made a
#   decision about this caller. An operator exclusion may exist while the process
#   holding it cannot answer for it, so the exclusion set is unknown rather than
#   empty and the withheld deny stays withheld. The cost is bounded at both ends:
#   a session that resolves its policy once is served from
#   ``_excluded_tools_by_session`` and never reaches a failure path again, and for
#   one that has not, the refusal lasts at most ``_NEGATIVE_CACHE_TTL``.
#
# The remaining reasons stay permissive because each covers a caller for whom no
# operator exclusion is known to exist, or a class for which refusal is permanent
# rather than a window that closes:
#
# * ``agent_not_resolved`` is the gateway's 404, returned BOTH for a session
#   still registering (a policy may exist) and for a caller it can never map to
#   an agent (no policy can exist). Refusing denies the second class forever.
# * ``no_session_key`` is the same gap inside this process: no agent is named, so
#   no operator exclusion is known to exist for the call to bypass.
# * ``policy_forbidden`` is ANY 4xx. The gateway answered and made a decision
#   about this caller -- for 403 ``member_session_unverified`` that is the steady
#   state of a session claiming a private store without a verifiable proof.
#   Refusing would deny such a class permanently, and a boundary the gateway is
#   enforcing is not a boundary it failed to read. The test is the status class,
#   not a list of names, so an unfamiliar 4xx is permissive-and-audited rather
#   than silently reclassified as an outage.
#
# Those permissive windows are audited per call, so they are visible instead of
# silent -- which is what made this condition hard to find. Closing them needs
# the 404 to distinguish registering from unmappable, which is a change to the
# endpoint's contract rather than to this read.
_UNRESOLVED_REFUSES_CALL = frozenset(
    {
        "policy_unreadable",
        "identity_unattested",
        "identity_unattestable",
        "resolution_failed",
        "gateway_locked",
    }
)

#: The directory this package's own code lives in. A tool handler whose code is
#: under it belongs to a server Kiro Crew ships: its own control plane, and the
#: builtin apps under ``apps/builtins``.
_PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))


def serves_managed_tools(call_tool_fn: Callable[..., Any]) -> bool:
    """True when *call_tool_fn* is Kiro Crew's own code, so the tool policy applies.

    ``managedToolPolicy`` names the tools of Kiro Crew's managed servers that a
    session may not reach (``src/kiro_crew/docs/agent-spec-fields.md``). A user-installed app that serves its own tools through
    :func:`run_mcp_stdio_loop` is not one of those servers, and on the shared ACP
    runtime it is never handed a session token -- identity rides only the control
    plane -- so reading the policy for it could only ever fail closed and refuse
    every call it serves. Its tools are already gated where the session is: the
    agent spec's ``tools`` / ``allowedTools`` and kiro-cli's ``disabledTools``.

    The check runs inside the app's own process, so it is not a boundary an app
    could be held to: an app that wanted to skip it could skip this loop. What it
    must NOT do is let Kiro Crew's own servers out, so every answer it cannot
    establish is True: a handler with no readable code location, or one outside
    every known shape, keeps the policy.
    """
    target: Any = call_tool_fn
    while isinstance(target, functools.partial):
        target = target.func
    target = getattr(target, "__func__", target)
    code = getattr(target, "__code__", None)
    if code is None:
        code = getattr(getattr(type(target), "__call__", None), "__code__", None)
    filename = getattr(code, "co_filename", "") if code is not None else ""
    if not filename or filename.startswith("<"):
        return True
    try:
        real = os.path.realpath(filename)
        named = platform_compat.strip_extended_length_prefix(filename)
        drive = os.path.splitdrive(named)[0]
        located = bool(drive) and os.path.isabs(named) and os.path.isfile(real)
    except (OSError, ValueError):
        return True
    return _path_is_under(real, _PACKAGE_DIR, os.path, located=located)


def _path_is_under(path: str, root: str, pathmod: Any = os.path, *, located: bool = True) -> bool:
    """Whether absolute *path* is *root* or inside it; True when that cannot be told.

    A path on another Windows drive is never inside *root*, but ``commonpath``
    raises ``ValueError`` for it rather than answering. Read that case from the
    drives first, so code on ``C:`` is not mistaken for the package on ``D:``
    (a GitHub-hosted Windows runner checks out on ``D:`` and keeps its temp dir on
    ``C:``). Any other refusal keeps the fail-closed answer.

    Both sides are compared without the ``\\\\?\\`` long-path prefix, which
    Windows ``realpath`` keeps on deep paths and which only spells a path. Another
    drive is proof only when *located* (the caller found an existing file under an
    absolute name with a drive). A relative name, or a rooted one with no drive,
    resolves against the cwd at call time and a missing file proves nothing, so
    those keep the policy. Only the backslash ``\\\\?\\`` form that Windows
    ``realpath`` produces is folded; a forward-slash or ``\\\\.\\`` spelling is
    left alone and takes the plain comparison.
    """
    path = platform_compat.strip_extended_length_prefix(path)
    root = platform_compat.strip_extended_length_prefix(root)
    path_drive = pathmod.normcase(pathmod.splitdrive(path)[0])
    root_drive = pathmod.normcase(pathmod.splitdrive(root)[0])
    if path_drive != root_drive:
        return not located
    try:
        common = pathmod.commonpath([path, root])
    except ValueError:
        return True
    # ``commonpath`` keeps the casing of its input; Windows paths compare caseless.
    return pathmod.normcase(common) == pathmod.normcase(root)
