"""Which session is calling a kirocrew-core tool.

The lenient resolver (the per-call caller block, then this process's own identity),
the strict, HMAC-verified one and its fail-closed gate ``require_strict_session_key``
with the diagnosis it reports, the modules registered as reflexive, and the Slack
identity classification the channel tools read.

:mod:`kiro_crew.mcp_core` stays the import path and the patch surface; see
:mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

import os

from kiro_crew.mcp_caller import current_caller, host_pid_is_shared, resolve_own_identity
from kiro_crew.mcp_shared import external_client_identity_note, spawned_without_gateway_identity
from kiro_crew.messaging.link import is_legacy_slack_key, legacy_key
from kiro_crew.session_pid_sig import session_pid_mapping_path
from kiro_crew.session_token_sig import session_key_from_env_token


def _session_key_from_token() -> str:
    """Resolve this session's key from its signed per-session TOKEN, or "".

    The identity channel that needs no daemon, no broker stub and no config
    switch. The token is minted per ACP ``session/new``
    (:func:`kiro_crew.mcp_gateway.claim.mint_stub_session_token`) and rides THIS
    session's own ``mcpServers`` elements in ``env``, so — unlike every
    process-keyed source — a ``spawn_run`` subagent sharing its parent's kiro-cli
    process carries a different name than its parent. The gateway publishes the
    token -> session-key mapping to a MAC-signed file
    (:func:`kiro_crew.session_token_sig.publish_session_token`) at ``session/new``
    and again on every warm-pool ``rekey()``, and the MAC is what makes the file
    trustworthy in a directory an agent can write.

    The read itself belongs to
    :func:`kiro_crew.session_token_sig.session_key_from_env_token`, the one reader
    every resolver shares — this module's two, the client-side
    ``mcp_caller.CallerContext.from_env`` and the managed-tool-policy lookup in
    ``mcp_shared`` — so the token's position and its fail-closed behaviour cannot
    drift between them. Read at the SAME position in each: after the
    gateway-injected per-call caller context, and BEFORE the
    ``KIROCREW_SESSION_KEY`` env var. That order is load-bearing rather than
    arbitrary — a warm-pool process is re-keyed to a new session while the env its
    MCP children were spawned with keeps naming the PREVIOUS one, so where the two
    disagree the env var is the stale answer and the file is the current one.
    Placing the token second (below the caller context) keeps the gateway's own
    per-call injection authoritative wherever it exists, since that is stamped per
    CALL and therefore cannot go stale at all.

    Fails closed: an unset token, a missing mapping, or a MAC that does not verify
    all return ``""`` and the caller falls through to the sources it had before.
    Never raises — an identity source that can raise would turn a resolvable
    session into a crashed tool call.
    """
    try:
        return session_key_from_env_token()
    except Exception:
        # No logger here, and no bare stderr write: this module runs inside the
        # kirocrew-core stdio MCP server, whose stray stdout/stderr would corrupt
        # the JSON-RPC stream — the same constraint the governance-degrade branch
        # in ``mcp_core_parts/governance.py`` states, and the reason this module keeps
        # no logger at all. The
        # observable consequence of a failure is the refusal the caller already
        # emits, which carries :func:`strict_identity_diagnosis`.
        pass
    return ""


def _resolve_session_key() -> str:
    """Return this session's key, or ``""`` when nothing can name it.

    Source 0 is the gateway-injected per-call caller context (pooled
    topology): gatewayd strips client-forged ``kirocrew.caller`` blocks and
    injects its own on every forwarded call, so it is the authoritative
    identity when present — env-var identity is wrong-by-construction in a
    shared backend (one process, many sessions).

    Everything below it is :func:`kiro_crew.mcp_caller.resolve_own_identity`,
    the one client-side ladder every such resolver shares: the signed
    per-session token, then ``KIROCREW_SESSION_KEY``, then the gateway-published
    pid mapping by host pid and by ancestor walk. The protected member binding is
    deliberately NOT consulted here — it gates a private memory store, and this
    lenient resolver's consumers are attribution rather than authorization.

    Warm-pool kiro-cli processes have no ``KIROCREW_SESSION_KEY`` (the pool
    spawns with an empty key so rekey() + pid mapping provide the mapping), and
    a pid mapping answers for the PROCESS: on a runtime hosting several sessions
    it names a co-tenant, so the ladder refuses there rather than returning the
    wrong session.
    """
    ctx = current_caller()
    if ctx is not None and ctx.session_key:
        return ctx.session_key
    return resolve_own_identity(consult_protected_binding=False).session_key


def _resolve_session_key_strict() -> str:
    """Resolve the session key, refusing PID-walked and unsigned identities.

    Like ``_resolve_session_key`` but drops the ``/proc`` ancestor walk.
    Three identity sources are accepted:

    1. The signed per-SESSION token (:func:`_session_key_from_token`). Minted per
       ``session/new``, carried in the ``env`` of this session's own
       ``mcpServers`` elements, and resolved through a MAC-signed mapping file the
       gateway republishes on every ``rekey()``. It is the only source that is
       both per-session (so a ``spawn_run`` subagent does not inherit its
       parent's identity on a shared runtime) and switch-free (no gatewayd, no
       broker stub, no config key) — which is why it is accepted ABOVE the env
       var: where the two disagree, a warm-pool rekey has made the env stale and
       the file is current.
    2. The gateway-injected ``KIROCREW_SESSION_KEY`` env var, EXCEPT on a host
       pid the mapping records as hosting several sessions. The var is stamped
       once per runtime and inherited by every MCP child of it, so on a shared
       runtime it names one tenant and cannot say which one is calling — the same
       ground on which source 3 refuses such a pid
       (:func:`kiro_crew.mcp_caller.host_pid_is_shared`).
    3. The direct ``KIROCREW_HOST_PID`` -> ``session_pid_<pid>.txt``
       lookup, but ONLY when the HMAC sidecar written by the gateway
       verifies (:func:`kiro_crew.session_pid_sig.verify_session_pid`).
       PID-namespace sandboxing strips ``KIROCREW_SESSION_KEY`` from the
       sandboxed env, but the sandbox launcher exports its OWN host pid
       (``sandbox.py``) — exactly the pid the gateway keys
       ``session_pid_<pid>.txt`` by on session claim. The bare ``.txt``
       file is agent-writable and therefore forgeable; the sidecar is
       signed with the SEL trust root (``sel_hmac.key``), which agents
       cannot read, and binds the pid into the MAC so another pid's
       pair cannot be replayed. Without this branch,
       ``monitor_start``/``monitor_update``/``autonudge_stop``/``set_project``
       fail closed
       in every sandboxed dashboard session even though the session is
       fully identified.

    Returns ``""`` when only the ``/proc`` ancestor WALK would have
    matched, when the sidecar is missing/invalid, or when the mapping
    records that its pid hosts SEVERAL sessions — one key cannot name
    them, so answering would attribute this caller to a co-tenant, and a
    state-mutating tool would write another session's state. The walk
    stays excluded: a subagent spawned via ``spawn_run`` lives under the
    parent slot's process tree, so walking ancestors from its MCP-core
    child silently resolves to the parent — which would let the
    subagent mutate state on the wrong slot. Read-only callers (audit,
    telemetry) keep the lenient resolver where misattribution is
    harmless.

    Source 0 (accepted BEFORE all three of the above): the gateway-injected
    per-call caller context. In the pooled topology gatewayd strips any
    client-forged ``kirocrew.caller`` block on every inbound frame and
    injects its own (built from the uid-gated claim-push at ``rekey()``),
    so a context present here is gateway-authored — strictly stronger
    provenance than the env var, and the ONLY correct identity in a
    shared backend serving many sessions. This is what keeps
    ``send_notification`` working for warm-pool sessions on platforms
    without the sandbox launcher (macOS/Windows), where neither env
    source exists in the backend process.
    """
    ctx = current_caller()
    if ctx is not None and ctx.session_key:
        return ctx.session_key
    # The signed per-session token, ABOVE the env var: after a warm-pool rekey the
    # env key names the previous session and the mapping file names the current
    # one. See :func:`_session_key_from_token`.
    from_token = _session_key_from_token()
    if from_token:
        return from_token
    sk = os.environ.get("KIROCREW_SESSION_KEY", "")
    host_pid = os.environ.get("KIROCREW_HOST_PID", "")
    # The env var is PROCESS-keyed: it is stamped once, with the session the
    # runtime was spawned for, and every MCP child of that runtime inherits the
    # same value. One runtime hosts many ACP sessions, so on a runtime a signed
    # mapping records as multi-tenant the var names ONE tenant and cannot say
    # which one is calling — the same reason source 3 below refuses such a pid.
    # Answering would attribute this caller to a co-tenant and a state-mutating
    # tool would write another session's state.
    if sk and not host_pid_is_shared(host_pid):
        return sk
    try:
        if host_pid.isdigit():
            from kiro_crew.session_pid_sig import verify_session_pid

            return verify_session_pid(host_pid)
    except Exception:
        pass
    return ""


def strict_identity_diagnosis(server: str = "kirocrew-core") -> str:
    """Return the machine-specific reason strict identity is unavailable, or "".

    Every strict refusal already says *what* was refused; none of them says why
    THIS install cannot answer "which session is calling", so the reader has no
    next step. This inspects the same sources
    :func:`_resolve_session_key_strict` accepts and names the missing channel.

    The distinction that matters to an operator: on the kiro backend a session's
    kiro-cli process is an ``AcpRuntime``, which is deliberately
    session-UNBOUND (one process multiplexes N sessions, so it cannot carry a
    single session's key in its environment — ``acp/runtime.py`` injects none).
    The gateway's per-call caller injection is therefore one identity channel
    for that backend, and it exists only for servers listed in
    ``mcp_gateway.stub_servers``. The signed per-session token and the verified
    host-pid sidecar are independent of the gateway. A server that is not
    routed through the gateway and has neither is a topology gap an operator can
    close in one line — not a bug in the calling session.

    Returns "" when identity IS resolvable (the caller should not be refusing),
    so a caller can append this unconditionally.
    """
    if _resolve_session_key_strict():
        return ""
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    if os.environ.get(STUB_SESSION_TOKEN_ENV, ""):
        # The switch-free channel was WIRED — this session's MCP element carried
        # a token — and the signed mapping is what failed. That is a trust-root
        # or publication problem, never a routing one, so pointing the reader at
        # ``stub_servers`` here would send them to configure a channel they
        # already have. Named separately from the pid sidecar below because the
        # two fail for different reasons and only one of them is fixed by the
        # sandbox launcher being present.
        return (
            f" No identity channel: this session's MCP element carried a session "
            f"token but its signed mapping did not verify. Check `kirocrew doctor` "
            f"(SEL trust root) — {server} needs no routing when this channel works."
        )
    host_pid = os.environ.get("KIROCREW_HOST_PID", "")
    if host_pid.isdigit():
        # The sandbox launcher declared a host pid, so the channel exists and
        # the sidecar is what failed — a signing/trust-root problem, not routing.
        # Name the directory the verifier actually searched: when an agent spec
        # pinned a foreign KIROCREW_HOME into this stub's environment,
        # the path is the poisoned home, and without it the operator is sent to
        # `kirocrew doctor` on the real gateway, which reports a healthy trust
        # root and points nowhere.
        suffix = ""
        try:
            mapping = session_pid_mapping_path(host_pid)
            # Existence-independent on purpose: the mapping directory is
            # same-uid agent-writable, so any wording that varies with what is
            # at the path (present, absent, a planted symlink) becomes an
            # existence oracle. Naming the searched path is the whole of the
            # diagnostic; whether anything is there is one `ls` away.
            suffix = f" (mapping searched: {mapping})"
        except (OSError, RuntimeError, ValueError):
            # A diagnostic must never replace the denial it decorates with a
            # crash: Path.home() raises RuntimeError with no resolvable home,
            # expanduser RuntimeError on "~unknown", resolution OSError, and
            # garbage ValueError. On any of them, keep the generic wording.
            suffix = ""
        return (
            f" No identity channel: the signed pid mapping for this session did "
            f"not verify{suffix}. Check `kirocrew doctor` (trust root) — {server} "
            f"does not need routing when this channel works."
        )
    # Reaching here means no token on the element and no launcher pid, the same
    # shape ``spawned_without_gateway_identity`` reads; the note is gated on it
    # anyway so this branch and the tool-policy refusal agree by construction.
    out = (
        f" No identity channel on this install: {server}'s MCP element carries "
        f"neither a session token nor a session key, {server} is not in "
        f"mcp_gateway.stub_servers, so the gateway injects no per-call caller, and "
        f"the kiro backend's AcpRuntime is session-unbound (it carries no session "
        f"key in its environment by design). A session token on the element is the "
        f"switch-free fix and needs no gateway; where this backend emits no element "
        f"for {server} at all, route it from MCP Management (or add it to "
        f"mcp_gateway.stub_servers and restart) to give this session a verifiable "
        f"identity. `kirocrew doctor` reports the same check."
    )
    if spawned_without_gateway_identity():
        out += external_client_identity_note(server)
    return out


#: The REFLEXIVE tool surface: every module whose MCP tools embed "my session"
#: in their semantics (ledger writes, monitor loops, session-scoped control,
#: attributed channel sends, crew/app state, cron ownership). These operations
#: resolve the caller STRICTLY through :func:`require_strict_session_key`, never
#: the lenient resolver whose ``/proc`` ancestor walk can return a parent slot.
#: The registry includes every module with a strict operation. This is data:
#: ``test/test_identity_topology.py`` scans the source tree and fails when a
#: module calls the strict resolver directly (bypassing the gate) or calls the
#: gate without being registered here, so the NEXT reflexive tool cannot skip
#: the gate silently. Paths are relative to ``src/kiro_crew``.
REFLEXIVE_TOOL_MODULES: frozenset[str] = frozenset(
    {
        "mcp_computer.py",
        "mcp_crew_log.py",
        "mcp_cron.py",
        "mcp_dashboard.py",
        "mcp_dashboard_caller_scope.py",
        "mcp_dashboard_folder_runs.py",
        "mcp_dashboard_tag_runs.py",
        "mcp_debug.py",
        "mcp_work.py",
        "mcp_panel.py",
        "mcp_guide.py",
        "mcp_mate.py",
        "mcp_tools/apps.py",
        "mcp_tools/code_channels.py",
        "mcp_tools/control.py",
        "mcp_tools/control_parts/monitor.py",
        "mcp_tools/learn.py",
        "mcp_tools/ledger.py",
        "mcp_tools/logs.py",
        "mcp_tools/messaging.py",
        "mcp_tools/sessions.py",
        "mcp_tools/skills.py",
        # Not a tool module: the tool table's production caller, which every
        # table-built server's strict checks route through.
        "mcp_tools/table.py",
        "mcp_tools/workflows.py",
    }
)


def require_strict_session_key(refusal: str, server: str = "kirocrew-core") -> tuple[str, str]:
    """The ONE fail-closed identity gate every reflexive tool routes through.

    Returns ``(key, "")`` when the caller is strictly identified, and
    ``("", error)`` otherwise, where ``error`` is the caller-supplied
    ``refusal`` text with :func:`strict_identity_diagnosis` appended so the
    operator learns why THIS install cannot answer "which session is calling".

    Resolution is :func:`_resolve_session_key_strict` — gateway-injected
    caller context, the HMAC-verified per-session token,
    ``KIROCREW_SESSION_KEY``, or the HMAC-verified host-pid
    sidecar; never the lenient ``/proc`` ancestor walk, under which a subagent
    resolves to its PARENT slot and could read or mutate the parent's state.

    The tuple shape is deliberate: each call site keeps its own arm. Most
    refuse with the ``error`` half; tools that legitimately degrade instead
    (``autonudge_stop`` short-circuits, ``ask_question`` gates on the
    dashboard surface, computer-use falls back to an unresolved placeholder)
    use the resolve half only and ignore ``error``. What no call site may do
    is resolve identity for a reflexive tool through anything but this gate —
    the ratchet test over :data:`REFLEXIVE_TOOL_MODULES` enforces exactly
    that. Callers must send the KEY THIS GATE RETURNED on the wire:
    re-resolving at the write would check one identity and act as another.
    """
    sk = _resolve_session_key_strict()
    if sk:
        return sk, ""
    return "", refusal + strict_identity_diagnosis(server)


def _current_session_thread_ts() -> str | None:
    """Return the CALLER's Slack thread_ts, or None.

    Thin ``thread_ts | None`` view over :func:`_classify_slack_identity` — see
    that function for the three-state discrimination (``thread`` /
    ``non_slack`` / ``unresolved``) that ``file_send`` uses to fail CLOSED for
    audience when the caller cannot be attributed. This wrapper returns the
    bare thread_ts only for the ``thread`` state and ``None`` otherwise; on its
    own it does NOT distinguish "not a Slack session" from "identity
    unresolved", so callers on the outward-facing send path MUST use
    :func:`_classify_slack_identity` directly to avoid the channel-root
    disclosure hazard (unresolved identity + explicit channel -> channel root).

    Resolution is via :func:`_resolve_session_key_strict`, which accepts ONLY
    the gateway-injected env var or an HMAC-sidecar-verified
    ``KIROCREW_HOST_PID`` lookup. It deliberately drops the ``/proc`` ancestor
    walk and the bare (agent-writable, forgeable) ``.txt`` fallback the lenient
    resolver allows — closing both the forged-pid-file and the subagent->parent
    misresolution paths, and the prior newest-mtime ``session_pid_*.txt`` glob
    that frequently resolved to a DIFFERENT session than the caller.
    """
    state, thread_ts = _classify_slack_identity()
    return thread_ts if state == "thread" else None


def _classify_slack_identity() -> tuple[str, str | None]:
    """Classify the caller's STRICT Slack identity for outward file delivery.

    ``_current_session_thread_ts`` collapses the result to a bare
    ``thread_ts | None``; this returns the underlying THREE-state discrimination
    that ``file_send`` needs to tell "this is not a Slack session" apart from
    "the caller's Slack identity could not be resolved". Collapsing those two
    into a bare ``None`` is a channel-root disclosure hazard: an *unresolved*
    caller that still supplies an explicit tracked channel would upload at the
    CHANNEL ROOT (``thread_ts=None`` + channel), exposing a file meant for one
    thread to the entire channel — a reachable cross-session disclosure that is
    fail-OPEN with respect to audience, not fail-closed. Warm-pool-claimed Slack
    sessions have no strict identity source (the gateway writes the env var /
    HMAC sidecar only at sandbox spawn, not at warm-pool claim), so every one of
    their ``file_send`` calls hits this seam.

    Returns one of:

    * ``("thread", "<bare_ts>")`` — caller is a RESOLVED Slack thread (a
      canonical ``slack:<thread_ts>`` key, converted via
      :func:`messaging.link.legacy_key`, or an already-bare legacy Slack key).
      Deliver threaded to ``thread_ts``.
    * ``("non_slack", None)``     — caller is a RESOLVED non-Slack session
      (``dashboard:``/``discord:``/app/channel/future namespace). It has no
      Slack thread, but its identity IS known, so the handler's authorized
      routing (owner DM, session-map-linked thread, or an explicitly-supplied
      tracked channel) is safe — never a channel-root broadcast for an
      unknown caller.
    * ``("unresolved", None)``    — strict resolution failed (no gateway env var
      and no HMAC-verified host-pid). The caller cannot be attributed, so an
      outward Slack send must fail CLOSED (refuse) rather than broadcast.
    """
    key = _resolve_session_key_strict()
    if not key:
        return ("unresolved", None)
    # Canonical ``slack:<thread_ts>`` -> bare thread_ts.
    bare = legacy_key(key)
    if bare is not None:
        return ("thread", bare)
    # Already-bare legacy Slack thread_ts -> pass through.
    if is_legacy_slack_key(key):
        return ("thread", key)
    # Resolved, but not a Slack thread (dashboard:, discord:, apps, channels,
    # future ns) — identity is known, so downstream routing is authorized.
    return ("non_slack", None)
