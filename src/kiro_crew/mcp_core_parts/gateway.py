"""Where a kirocrew-core tool's gateway call goes, and the verbs that send it.

The gateway's port, base URL and unix socket and the caches that pin them, one
request's target resolution, the refused-target replay and re-verification, the
internal secret, the identity headers every verb sends, the retry schedule,
failure text and error body, and the verbs themselves: ``_send`` and the
``_get``, ``_post``, ``_patch``, ``_put`` and ``_delete`` helpers built on it.

The verbs live here, not in :mod:`kiro_crew.mcp_core`, so the other own servers
(``mcp_guide``, ``mcp_mate``, ``mcp_cron``) can call the gateway without loading
the whole core server: importing ``mcp_core`` pulls in agent discovery, the
conversation store and knowledge search, about 950 ms and 650 modules a server
never uses. :mod:`kiro_crew.mcp_core` stays the import path and the patch surface;
see :mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

import json
import re as _re
import socket
import time
import urllib.error
import urllib.request

from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.origin import dashboard_socket_path
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import current_caller
from kiro_crew.mcp_core_parts.identity import _resolve_session_key
from kiro_crew.mcp_shared import internal_caller
from kiro_crew.port_resolution import port_is_gateway_owned, resolve_client_port_src
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def _resolve_api_port() -> tuple[int, str]:
    """Resolve the gateway API port a callback should aim at.

    Same precedence as every client CLI command
    (:func:`kiro_crew.port_resolution.resolve_client_port_src`). The marker step
    keeps a portless ``dashboard.url`` from collapsing to the default port while
    a live gateway is bound elsewhere.

    Returns ``(port, source)``; the source (``"env"`` / ``"bound"`` /
    ``"config"`` / ``"marker"`` / ``"default"``) decides whether it may be cached.
    """
    return resolve_client_port_src(None)


#: Port sources stable for the process lifetime, safe to pin. Deliberately
#: excludes ``"marker"``: a marker-discovered port is verified at that instant
#: only — the gateway can exit or move and any local process may then rebind
#: the port, so every secret-bearing request must re-run the discovery chain
#: (which re-proves ownership via ``_gateway_owns_port``) instead of trusting
#: a pinned value. Also excludes ``"default"``, the no-evidence fall-through.
_STABLE_PORT_SOURCES = frozenset({"cli", "env", "bound", "config"})


# Lazy caches for the gateway port, base URL and unix-socket path; ``None`` means
# not resolved yet (tests may pre-seed them). Not computed at import: config and
# the run-marker can change before the first gateway call. They hold a pair only
# for stable sources; a request derives both from one resolution
# (``_resolve_api_target``) so its two transports never name different gateways.
_API_PORT: int | None = None


_API: str | None = None


_API_UNIX_SOCKET: str | None = None


def _api_port() -> int:
    """Gateway API port, resolved on first use; pinned only on stable evidence.

    Only a *stable* source — env var, exported bound port, or a port the user
    wrote in ``dashboard.url`` — is cached: those are user decisions that hold
    for the process lifetime. Two resolutions are returned but NOT cached:

    * The default fall-through: it only proves nothing was discoverable at
      that instant. During gateway boot a broker-descended server can race
      the asynchronous run-marker write — pinning that fall-through would
      freeze the wrong port for the process lifetime, with restart as the
      only recovery. The next call re-resolves and picks the marker up once
      it exists.
    * A **marker-discovered** port: ownership was proven for that instant,
      not forever. The gateway can exit or move ports, after which any local
      process — another user's included — may rebind the port; a pinned
      marker resolution would keep sending the internal secret there. Every
      call therefore re-runs the discovery chain, whose marker step re-proves
      ownership (``_gateway_owns_port``) before the port is trusted again.
    """
    global _API_PORT
    if _API_PORT is None:
        port, source = _resolve_api_port()
        if source not in _STABLE_PORT_SOURCES:
            return port
        _API_PORT = port
    return _API_PORT


def _api_base() -> str:
    """Gateway API base URL, resolved on first use and cached.

    Pinned to the IPv4 literal ``127.0.0.1`` rather than ``localhost``: these
    requests carry ``X-Internal-Secret``, and a hostname lookup could resolve
    to ``::1`` where a different (possibly foreign) process may be listening —
    the gateway itself binds IPv4 loopback. Mirrors ``cli_server._CLI_LOOPBACK``.
    """
    global _API
    if _API is None:
        base = f"http://127.0.0.1:{_api_port()}"
        if _API_PORT is None:
            # Port resolution fell through to the default — do not pin a URL
            # built on no evidence (see _api_port).
            return base
        _API = base
    return _API


def _api_unix_socket() -> str:
    """Path of the gateway's internal-API unix socket (may not exist yet).

    Preferred transport for every gateway API request: connecting through it
    lets the gateway kernel-verify (``SO_PEERCRED`` + /proc ancestry) that
    this process actually belongs to the session its ``X-Session-Key`` header
    declares, instead of taking the header on faith. ``loopback_urlopen``
    checks existence per call and falls back to TCP when the file is absent
    (Windows, older gateway, bind failure) or nobody answers on it, so
    caching the path after the first resolution — mirroring ``_api_base`` —
    is safe. Derived from the same cached ``_api_port`` resolution as the API
    base so both transports always aim at the same gateway.
    """
    global _API_UNIX_SOCKET
    if _API_UNIX_SOCKET is None:
        path = _socket_path_for(_api_port())
        if not path:
            _API_UNIX_SOCKET = ""
            return _API_UNIX_SOCKET
        if _API_PORT is None:
            # Same no-evidence rule as _api_base: usable now, not pinned.
            return path
        _API_UNIX_SOCKET = path
    return _API_UNIX_SOCKET


def _socket_path_for(port: int) -> str:
    """Unix-socket path for *port*, or ``""`` when one cannot be derived.

    Split out of :func:`_api_unix_socket` so a caller that already holds a
    resolved port can derive the socket from THAT port instead of re-running
    the discovery chain to find one.
    """
    try:
        return str(dashboard_socket_path(port))
    except Exception:
        return ""


def _resolve_api_target() -> tuple[str, str]:
    """The ``(base, socket_path)`` pair for ONE request attempt, from ONE resolution.

    ``_api_base`` and ``_api_unix_socket`` are correct for callers that want one
    of the two, but a request needs BOTH, and on a marker-discovered port
    neither is cached — so asking each of them ran the discovery chain twice
    for a single attempt. Two forks of ``lsof`` per gateway call is the cost;
    the correctness half is that the two answers are independent, and the
    marker rule exists precisely because the answer can change between them. A
    gateway that exits and is replaced mid-attempt would leave the TCP base
    naming one gateway and the unix socket another — and ``loopback_urlopen``
    PREFERS the socket, so the component actually carrying the request is the
    one ``_send``'s replay guard never inspected.

    Resolving once and deriving both halves from that single port restores the
    "both transports derive from one resolution" invariant the cache comment
    and :func:`_invalidate_api_base` already claim.

    This does NOT weaken the ownership proof. The proof is per ATTEMPT, not per
    process: a marker resolution is still never pinned, every attempt still
    re-runs the chain, and the replay in :func:`_send` resolves a FRESH pair
    rather than reusing the refused one. What changes is that one attempt now
    proves ownership once instead of twice.
    """
    global _API, _API_UNIX_SOCKET
    if _API is not None and _API_UNIX_SOCKET is not None:
        # Answer exactly what the standalone getters would. Do not also require
        # `_API_PORT`: the getters return a populated memo whatever the port cache
        # holds, and tests pre-seed these two directly.
        return _API, _API_UNIX_SOCKET
    port = _api_port()
    base = f"http://127.0.0.1:{port}"
    sock = _socket_path_for(port)
    if _API_PORT is not None:
        # Stable source (env / bound / config): pin exactly as the individual
        # getters do. A marker or default resolution is left unpinned.
        _API = base
        _API_UNIX_SOCKET = sock
    return base, sock


def _api_urlopen(
    req: urllib.request.Request | str,
    timeout: float,
    *,
    unix_socket_path: str | None = None,
):
    """``loopback_urlopen`` against the API base with the unix-socket preference.

    *unix_socket_path* lets a caller that already resolved a target supply the
    socket belonging to THAT resolution (see :func:`_resolve_api_target`); the
    default keeps the standalone behavior for callers holding only a base.
    """
    # A process holding an agent-socket capability sends every request there, with
    # the capability in place of the secret, and nowhere else (agent_ipc_client).
    # Imported here so the facade serves no name from outside its family.
    from kiro_crew.agent_ipc_client import current_capability, open_with_capability

    capability, agent_socket = current_capability(_api_port)
    if capability:
        return open_with_capability(req, timeout, capability=capability, socket_path=agent_socket)
    if unix_socket_path is None:
        unix_socket_path = _api_unix_socket()
    return loopback_urlopen(req, timeout=timeout, unix_socket_path=unix_socket_path or None)


def _invalidate_api_base() -> None:
    """Forget the pinned port, base and socket path so the next call re-resolves.

    Called when a connection is refused: the pinned base can predate a gateway
    that has since moved (or a config edit that retargeted it), and the current
    port is recorded only in the live run-marker. Dropping all three caches
    together keeps the invariant that both transports derive from one
    resolution — clearing only the URL would leave the socket path aimed at the
    old gateway.
    """
    global _API_PORT, _API, _API_UNIX_SOCKET
    _API_PORT = None
    _API = None
    _API_UNIX_SOCKET = None


def _replay_target(refused_base: str) -> tuple[str, str] | None:
    """The fresh ``(base, socket_path)`` to replay a refused request to, or ``None``.

    THE replay rule, owned here. A refused connection usually means the resolved
    base is stale — the gateway came up, or moved, after this tool server booted,
    and the new port is recorded only in the run marker — so one re-resolution
    and one replay are worth it. Two conditions end the attempt instead, and both
    answer ``None``:

    * the re-resolution fell through to the DEFAULT port, which is no evidence at
      all. A listener there could be any local process, and the request carries
      the internal secret; the replay exists to chase POSITIVE evidence of a
      moved gateway, so a no-evidence fall-through never gets to dial.
    * the re-resolution named the base that was just refused. Replaying an
      unchanged dead port only doubles the caller's latency to reach the
      identical failure, and nothing was handed to a live gateway — so the
      caller's first-attempt error stands as a definite rejection.

    Both halves of the returned pair come from the very resolution whose source
    was checked (same shape as :func:`_resolve_api_target`): re-resolving again
    could race a marker disappearing between the check and the dial, and
    deriving the socket separately let the replay's two transports name
    different gateways. It is always a FRESH pair, never the refused one — the
    ownership proof is per attempt.

    Callers keep their own error wording; this owns only the rule, so the two
    request paths (:func:`_send` and ``mcp_computer._invoke``) cannot drift on
    when a replay is allowed. ``None`` means "do not replay", not "report this
    particular text".
    """
    _invalidate_api_base()
    port, source = _resolve_api_port()
    if source == "default":
        return None
    base = f"http://127.0.0.1:{port}"
    if base == refused_base:
        return None
    return base, _socket_path_for(port)


def _secret_for_base(base: str) -> str:
    """The internal-API credential belonging to the gateway on *base*.

    ``read_local_secret`` resolves per LISTENER (``run/gateway-<port>.secret``)
    before the shared file, because the credential identifies ONE gateway
    GENERATION — its docstring is explicit that authenticating for a different
    generation than the one owning the dialled port earns a 403 on every
    internal call. A restarted gateway is a new generation, so a retry that
    re-proves the target must re-read the credential for it too; carrying the
    pre-restart secret is the desync that helper exists to close.

    The port comes from the base being dialled rather than a fresh resolution,
    so the credential and the target cannot name different gateways. The HOST is
    taken from that same base, so the credential is resolved for the exact
    listener this base addresses (``read_local_secret`` prefers the address-keyed
    entry and fails closed): a port names a SET of listeners, and pairing the
    read to the dialled host is what keeps the secret from reaching a co-resident
    that merely holds the port.
    """
    try:
        authority = base.split("//", 1)[-1].split("/", 1)[0]
        host = authority.rsplit(":", 1)[0]
        port = int(authority.rsplit(":", 1)[-1])
        return read_local_secret(port, dial_host=host)
    except Exception:
        return ""


def _reverify_refused_target(refused_base: str) -> tuple[str, str] | None:
    """The FRESH pair for *refused_base*, or ``None`` when it is not proven again.

    :func:`_replay_target`'s sibling, and the inverse test. That one answers
    "has the gateway MOVED, so is there a new base worth dialling"; this one
    answers "does re-resolution still positively identify the SAME base, so is
    re-dialling it still addressed to the gateway proved to own it".

    Why a same-base retry needs this at all: the ownership proof is per
    ATTEMPT, not per process (see :func:`_resolve_api_target`). A marker
    resolution is never pinned precisely because the gateway can exit and
    something else can take the port — and a retry that SLEEPS first is exactly
    that window. Re-dialling the cached pair would hand the internal secret and
    the session key to whatever now listens there, which is the same exposure
    the ``source == "default"`` rule above refuses, for the same reason.

    Three outcomes answer ``None``, so the caller stops instead of dialling:

    * the re-resolution fell through to the DEFAULT port — no evidence at all,
      so the listener could be any local process.
    * the re-resolution names a DIFFERENT base. The gateway moved mid-backoff,
      so the gateway does not own the refused base now and this retry is over. Chasing the
      new base is :func:`_replay_target`'s job on the caller's next attempt, not
      a second replay smuggled into a retry loop.
    * the port is not PROVEN to be held by this user's gateway. The source
      label alone cannot carry that proof: ``KIROCREW_BOUND_PORT`` is
      inherited process state naming the gateway that SPAWNED us, exported
      from the port that gateway owns — reserved (bound and listening, not yet
      accepting) on the dashboard path, republished by
      ``dashboard.server._export_bound_port`` once its
      site serves, and it ranks above the marker step — so ``bound``, not ``marker``, is
      the source for every gateway-spawned process, and it is never
      ownership-checked. A refusal is affirmative evidence the previous owner
      is gone, and a retry that SLEEPS first is exactly the window in which
      another local user can rebind the port, so the proof is re-earned here
      via :func:`port_is_gateway_owned` before the secret is dialled anywhere.
      A ``marker`` resolution already ran that proof inside its own step, so it
      is not re-run for one.

    Gating on the proof rather than on a list of trusted labels is deliberate.
    Trusting ``bound`` because it is "stable for the process lifetime" (which is
    what :data:`_STABLE_PORT_SOURCES` means, and all it means) confuses a value
    that does not change with a gateway that is still there; refusing it instead
    would have disabled this retry for the deployment it exists to serve, since
    ``bound`` is the only source that ever fires there. Verifying the port keeps
    the retry working when the gateway really did come back on it, and refuses
    only when something else answers.

    On non-POSIX the proof denies outright, so the retry is unavailable rather
    than approximated — the same trade :func:`_gateway_owns_port` already makes
    for marker discovery, and for the same reason: the file-permission argument
    that carries it does not hold there. ``--port`` / ``KIROCREW_PORT`` users on
    Windows get the actionable exhaustion message instead of a silent re-dial.

    Both halves of the returned pair come from the one resolution whose source
    was checked, keeping :func:`_resolve_api_target`'s "both transports derive
    from one resolution" invariant.
    """
    _invalidate_api_base()
    port, source = _resolve_api_port()
    if source == "default":
        return None
    base = f"http://127.0.0.1:{port}"
    if base != refused_base:
        return None
    # Cheapest checks first: the two above are string work, this one forks a
    # port lookup, and it is only worth paying when a dial is otherwise due.
    if source != "marker" and not port_is_gateway_owned(port):
        return None
    return base, _socket_path_for(port)


def _internal_secret() -> str:
    """Credential for the gateway this client will dial, paired to its port.

    Thin wrapper over ``config.loader.read_local_secret``, which owns the
    per-listener-then-shared order. The port is passed rather than re-resolved
    because ``_api_port`` already resolved and cached it for this process. The
    dial host is the IPv4 loopback literal every ``_api_base`` in this module
    addresses, so the credential is resolved for that listener and refuses when
    the address-keyed entry is absent rather than falling back to the port-keyed
    read.
    """
    try:
        return read_local_secret(_api_port(), dial_host="127.0.0.1")
    except Exception:
        return ""


def _session_key_header_error(sk: str) -> str | None:
    """Return an actionable error if the session key cannot go in an HTTP header.

    http.client encodes header values as latin-1, so a non-latin-1 char in the
    session key (e.g. an em-dash from a tab title) raises UnicodeEncodeError
    before the request is sent. Detect it up front and tell the user to rename
    the tab, rather than surfacing the raw codec error.
    """
    try:
        sk.encode("latin-1")
        return None
    except UnicodeEncodeError:
        return (
            "session key contains a character invalid in HTTP headers "
            "(non-latin-1, e.g. an em-dash or emoji in the tab title) — "
            "rename the chat tab to use ASCII characters and retry"
        )


def _caller_header() -> dict[str, str]:
    """Component attribution for independently authenticated internal requests.

    MCP stdio servers declare their component name via
    ``mcp_shared.set_internal_caller`` (done centrally in
    ``run_mcp_stdio_loop``), and every loopback request from these helpers
    carries it so the gateway's audit log can attribute an internal write to
    the actual component instead of inferring "some internal caller" from the
    secret's mere presence. Attribution only — the gateway
    authenticates on ``X-Internal-Secret`` and validates this name against a
    known set before trusting it into an audit line. Processes that never
    declared an identity (CLI, tests) send no header rather than a guess.

    """
    name = internal_caller()
    return {"X-Internal-Caller": name} if name else {}


def _session_token_header() -> dict[str, str]:
    """Attach the signed per-session token to internal gateway requests.

    The token is already injected into each MCP server's environment.  Sending
    it alongside ``X-Session-Key`` lets the gateway bind a loopback TCP request
    to the session that launched this MCP process; the gateway verifies the
    signature and the key match.  It is a transport identity proof, not a
    member-memory capability.

    EVERY verb in ``mcp_core`` attaches it, not only the writes: the gateway's
    session-scoped reads authorize on the declared key the same way, so a read
    that omitted it would be answered as an unnamed caller and a whole tool
    surface would refuse on a policy it could not fetch.

    A pooled backend is spawned from gatewayd's own environment, so the
    per-session token is not in ``os.environ``; gatewayd forwards it inside the
    per-call caller block for Kiro Crew's control-plane servers instead, and the
    caller block wins because it is stamped per CALL and cannot go stale. The
    header itself is spelled once, in :mod:`kiro_crew.session_token_sig`, beside
    the verifier that reads it.
    """
    from kiro_crew.session_token_sig import session_token_header

    ctx = current_caller()
    return session_token_header(ctx.session_token if ctx is not None and ctx.from_gateway else "")


def _transport_failure(message: str, mark: bool) -> dict:
    """Error payload for a request whose outcome is unknown.

    ``transport_error`` means acceptance is undetermined — the request may have
    reached the gateway before the response failed (a read timeout after spawn
    acceptance, say), so the caller must not declare a definite rejection nor
    retry on its own. Its readers (spawn_run's batch reconcile, the cron tool
    proxy, learn_add) all post, so the flag stays opt-in per verb rather than
    becoming a new field on every reply.
    """
    out: dict[str, object] = {"error": message}
    if mark:
        out["transport_error"] = True
    return out


# Pause before each extra attempt a REFUSED target gets. A refusal means nothing
# reached the gateway, so re-dialling cannot double-execute a verb (see
# ``_retry_refused`` in :func:`_send`). The pauses cover a restarting gateway
# rebinding its port without making a down gateway feel hung.
_REFUSED_RETRY_BACKOFFS: tuple[float, ...] = (0.25, 0.5)


def _refused_message(refused_base: str, exc: BaseException) -> str:
    """Actionable text for a base that refused every attempt.

    Names the dialled address and the likely reason, and appends the raw error
    for bug reports. The address comes from the refused base; re-resolving here
    would name a port nobody dialled.
    """
    host = refused_base.split("//", 1)[-1].rstrip("/") or refused_base
    return (
        f"Kiro Crew gateway not reachable on {host} — it may be down or "
        f"restarting; retry shortly ({exc})"
    )


def _http_error_body(exc: urllib.error.HTTPError) -> dict:
    """Decode the JSON body of an ``HTTPError`` into the standard error dict.

    Prefers the structured ``{"error": ...}`` JSON body (so callers can match
    on the backend's actual message), then the raw body text, then
    ``str(exc)`` — so a non-JSON or empty error response still yields a usable
    ``{"error": ...}`` payload instead of an opaque ``"HTTP Error 400"``.

    An HTTP response body is content originating outside Kiro Crew, so the
    decoded message is redacted (``redact_exfiltration_urls`` +
    ``redact_credentials``) before it is handed back to a caller that may echo
    it to the LLM / dashboard / Slack. Redaction leaves plain markers like
    ``"unknown session"`` intact, so downstream matching is unaffected.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        raw = ""
    message = raw or str(exc)
    counted = False
    code = ""
    hints: dict[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "error" in parsed:
                message = str(parsed["error"])
                # Keep api_spawn's "already counted" marker, or spawn_run
                # double-reconciles in-process rejections and closes waves early.
                counted = bool(parsed.get("counted"))
                # Preserve the backend's machine-readable error code (e.g.
                # ``unknown_session`` from DELETE /api/lessons) so callers can
                # dispatch on the stable code instead of matching the
                # human-readable wording. The body is untrusted external
                # content, so only a short identifier-shaped value survives —
                # anything else is dropped rather than echoed onward.
                raw_code = parsed.get("code")
                if isinstance(raw_code, str) and _re.fullmatch(r"[a-z0-9_.-]{1,64}", raw_code):
                    code = raw_code
                # The how-to-retry text a refusal carries next to its sentence
                # (deploy's "use ttl_hours=0" and the command to run). Redacted
                # below with the message, since it is the same untrusted body.
                for key in ("details", "remediation"):
                    value = parsed.get(key)
                    if isinstance(value, str) and value:
                        hints[key] = value
        except Exception:
            pass
    message, _ = redact_exfiltration_urls(message)
    message, _ = redact_credentials(message)
    if code == "internal_auth_mismatch":
        # Every internal tool receives the auth layer's bare "Forbidden", which
        # reads as a permission decision about the tool's own subject and sends
        # the reader after the wrong bug. It is an instance mix-up: the credential
        # this client read does not belong to the gateway generation that owns the
        # port it dialled. Rewritten HERE because all tool call sites already flow
        # through this decoder, so one mapping covers them instead of one branch
        # per tool -- and it is keyed on the CODE, since a genuine permission
        # denial produces the same body and must not be given this explanation.
        message = (
            "this client authenticated against the wrong Kiro Crew instance. "
            "The credential it read does not match the gateway now serving that "
            "port, usually because a second gateway started on this machine and "
            "replaced the shared credential file. Restart the gateway (or target "
            "the instance you meant) and retry; the gateway's security event log "
            "records both credential fingerprints for the mismatch."
        )
    elif code == "internal_secret_missing":
        message = (
            "the gateway received no internal credential from this client. A "
            "sandbox or harness mask that withholds it, or a desynchronized "
            "credential listener, are possible causes. This is not a "
            "second-gateway mix-up. The gateway's security event log marks "
            "these denials as received=absent."
        )
    elif code in ("ipc_capability_invalid", "ipc_session_unbound", "ipc_session_mismatch"):
        # The agent-socket capability this server holds does not admit it: the
        # gateway that issued it is gone, or so is the session it names.
        message = (
            "this tool server's connection to the gateway does not admit it for this "
            "session. Start a new session and retry."
        )
    elif code == "caller_record_missing":
        message = (
            "this session's cron or subagent record no longer exists; start a new "
            "session before retrying the tool."
        )
    elif code == "unix_peer_unverified":
        message = (
            "the gateway could not verify this Unix-socket caller as the same local "
            "user. Restart the gateway and start a new session; if the error persists, "
            "make sure the client and gateway run as the same OS user."
        )
    elif code == "peer_session_mismatch":
        message = (
            "this Unix-socket caller is bound to a different session than the "
            "X-Session-Key it declared. Restart the affected session, or start a new "
            "one, before retrying the tool."
        )
    out: dict = {"error": message}
    if counted:
        out["counted"] = True
    if code:
        out["code"] = code
    for key, value in hints.items():
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        out[key] = value
    return out


def _send(
    path: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str],
    method: str = "GET",
    timeout: float = 30,
    mark_transport_error: bool = False,
) -> dict:
    """Send one gateway request, recovering from a refused connection.

    Two layers, in order. A refused connection may mean the resolved base is
    stale: the gateway came up, or moved to another port, after this tool server
    booted, and that port is recorded only in the run marker — so the base is
    re-resolved and the request replayed once, but only when re-resolution
    actually produced a different base (the rule lives in
    :func:`_replay_target`). When there is no moved gateway to chase, the base
    that refused is the only candidate, and a gateway RESTARTING on its own port
    lands there: it gets a short bounded retry of the same target
    (``_retry_refused``) before the caller is told, actionably, that the gateway
    is unreachable.

    Every verb goes through here. Keeping both layers in one place is what stops
    PATCH-shaped calls from staying pinned to a base that POST already learned
    was wrong.
    """

    def _once(target: tuple[str, str], hdrs: dict[str, str] | None = None) -> dict:
        base, socket_path = target
        req = urllib.request.Request(
            f"{base}{path}", data=data, headers=hdrs if hdrs is not None else headers, method=method
        )
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_resolve_api_target(): 127.0.0.1 plus a port from config/env or a run-marker whose ownership is re-verified per request) + a fixed internal path; never user-controlled  # noqa: E501
        with _api_urlopen(req, timeout=timeout, unix_socket_path=socket_path) as resp:
            return json.loads(resp.read())

    def _refreshed_headers(base: str) -> dict[str, str]:
        """*headers* with the credential re-read for *base*, caller's dict intact.

        A copy, never an in-place edit: ``headers`` belongs to the caller and is
        reused for its own error reporting. An unreadable secret leaves the
        original value rather than sending an empty one, so a transient read
        failure cannot turn a retry into a guaranteed 403.
        """
        fresh = dict(headers)
        secret = _secret_for_base(base)
        if secret:
            fresh["X-Internal-Secret"] = secret
        return fresh

    def _retry_refused(refused: tuple[str, str], refusal: urllib.error.URLError) -> dict:
        """Re-dial the SAME refused target a few times, then report it actionably.

        A refusal is the one failure that is safe to replay: the connect itself
        never completed, so nothing was handed to the gateway and the request
        cannot have been executed — an identical retry therefore cannot
        double-execute a verb. That invariant is why this loop retries ONLY
        ``ConnectionRefusedError`` / ``socket.gaierror``; every other failure
        keeps its existing route, because an ``HTTPError`` carries a real
        response and any post-connect failure leaves acceptance undetermined,
        and spawn_run's reconcile depends on that ambiguity being reported
        rather than retried.

        The window this covers is a gateway restarting on its own port, which
        :func:`_replay_target` deliberately declines to chase — there is no
        moved base to find, so before this the caller got the raw errno with no
        retry at all. On exhaustion it gets the port and the restart hypothesis
        instead, with the raw error appended.

        Sleeping does NOT let the target go unproven. The ownership proof is per
        ATTEMPT, so every re-dial re-resolves through
        :func:`_reverify_refused_target` and proceeds only while positive
        evidence still names this same base — otherwise the port could have been
        taken by another local process during the backoff, and the retry would
        hand it the internal secret.

        A check that cannot prove the target consumes one step of the budget
        rather than ending it. "Not proven" is the EXPECTED reading mid-restart:
        between the old process releasing the port and the new one binding it
        nothing is listening, so no ownership can be shown — and that is the very
        window this retry exists to cover. Abandoning the schedule on the first
        such reading would have limited recovery to a gateway that rebinds inside
        the FIRST backoff, discarding the longer half of the budget for the case
        it was sized for. The schedule stays the bound: when the last step is
        still unproven, the caller gets the same actionable message as a
        dialled-and-refused exhaustion.
        """
        for backoff in _REFUSED_RETRY_BACKOFFS:
            time.sleep(backoff)
            # Re-prove ownership AFTER the sleep, never before it: the whole
            # point is that the gateway may have gone during the pause.
            proven = _reverify_refused_target(refused[0])
            if proven is None:
                # Spend the next step rather than the whole schedule: a gateway
                # part-way through rebinding is unprovable but about to be back.
                continue
            # The target was re-proven; the CREDENTIAL has to be re-read for it
            # too. A gateway that restarted on this port is a new generation with
            # a new per-listener secret, so replaying the header this request was
            # built with would earn a 403 instead of the retry this exists for.
            try:
                return _once(proven, _refreshed_headers(proven[0]))
            except urllib.error.HTTPError as exc:
                return _http_error_body(exc)
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
                    return _transport_failure(str(exc), mark_transport_error)
                refusal = exc
            except Exception as exc:
                return _transport_failure(str(exc), mark_transport_error)
        # ``refused`` is the one failure that proves nothing reached a gateway,
        # so a caller may fall back to a local path without risking a replay.
        return {"error": _refused_message(refused[0], refusal), "refused": True}

    # ONE resolution for this attempt; both transports derive from it.
    target = _resolve_api_target()
    base = target[0]
    try:
        return _once(target)
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError on 4xx/5xx; str(e) is only "HTTP Error 400:
        # Bad Request" — the structured {"error": ...} body lives in e.read().
        # Surface it so callers can act on the backend's actual error (e.g.
        # the learn_add "unknown session" mapping) instead of an opaque code.
        return _http_error_body(e)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
            return _transport_failure(str(e), mark_transport_error)
        # THE replay rule lives in _replay_target, shared with
        # mcp_computer._invoke: None means "do not replay" (no evidence, or the
        # re-resolution named the same dead base). Nothing was handed to a live
        # gateway in either case, so no refusal below carries transport
        # ambiguity — the outcome is a definite rejection once the retry window
        # for the refused target is spent.
        retry_target = _replay_target(base)
        if retry_target is None:
            # No moved gateway to chase, so the base that refused is the only
            # candidate — the same-port restart case. Give it the short retry
            # window rather than surfacing a bare errno.
            return _retry_refused(target, e)
        try:
            return _once(retry_target)
        except urllib.error.HTTPError as retry_exc:
            return _http_error_body(retry_exc)
        except urllib.error.URLError as retry_exc:
            if isinstance(retry_exc.reason, (ConnectionRefusedError, socket.gaierror)):
                # Both bases refused. The re-resolved one is the fresher
                # evidence of where the gateway lives, so the retry window
                # applies to it.
                return _retry_refused(retry_target, retry_exc)
            return _transport_failure(str(retry_exc), mark_transport_error)
        except Exception as retry_exc:
            # The replay reached the gateway and failed afterwards (a read timeout
            # after a spawn was accepted, say). Acceptance is undetermined, so this
            # must carry the same ambiguity flag as a first-attempt post-connect
            # failure — otherwise spawn_run reconciles a still-running member down
            # and orphans it.
            return _transport_failure(str(retry_exc), mark_transport_error)
    except Exception as e:
        # The request may have reached the gateway before the response failed
        # (for example, a read timeout after spawn acceptance). Callers must
        # not present this as a definite rejection or retry automatically.
        return _transport_failure(str(e), mark_transport_error)


def _post(
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 30,
    session_key: str | None = None,
) -> dict:
    """POST a gateway endpoint.

    ``session_key``: as in :func:`_patch`, and it matters for the same reason.
    The default resolution is :func:`_resolve_session_key`, which includes the
    ``/proc`` ancestor walk, so a caller that already gated itself on
    :func:`_resolve_session_key_strict` and then let this helper re-resolve
    would CHECK one identity and WRITE under another — the walk can land on an
    ancestor slot, which for an app-owned session means the write arrives
    looking like the unconfined person. Such a caller must pass the key it
    verified. It is still validated by ``_session_key_header_error``.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    # ``transport_error`` means acceptance is unknown. A caller that reads it
    # (spawn_run's batch reconcile, the cron tool proxy, learn_add) must not
    # report a definite failure; other _post callers treat the payload as a
    # normal error.
    return _send(
        path,
        data=data,
        headers=headers,
        method="POST",
        timeout=timeout,
        mark_transport_error=True,
    )


def _get(path: str, session_key: str | None = None, *, timeout: float = 10) -> dict:
    """GET a loopback gateway path with the internal-secret handshake.

    ``session_key`` exists so a caller that has ALREADY verified its identity can
    send the key it verified, instead of having this helper resolve one again. The
    default resolution is :func:`_resolve_session_key`, which includes the ``/proc``
    ancestor walk — fine for read-only telemetry, but for a caller gated on
    :func:`_resolve_session_key_strict` re-resolving here is a check-then-use
    split: the gate proves a strict identity exists, and this then attaches
    whatever the lenient walk answers at request time, which need not be the same
    session. Passing the verified key makes the value that was checked the value
    that is used. It is still validated by ``_session_key_header_error``.

    ``timeout`` defaults to the 10s a telemetry read needs. A route that does real
    work behind the GET raises it — the Dev Fleet pod reads spawn a
    ``python -m kiro_crew pod`` subprocess in the gateway, so they pay a cold
    interpreter start that 10s cannot cover on a loaded host.
    """
    headers = {
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, headers=headers, timeout=timeout)


def _patch(path: str, body: dict | None = None, *, session_key: str | None = None) -> dict:
    """PATCH a loopback gateway path with the internal-secret handshake.

    ``session_key``: as in :func:`_put`. A caller gated on
    :func:`_resolve_session_key_strict` must send the key it verified —
    re-resolving through the lenient walk here would let the request carry a
    different session's authority than the one the gate approved.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, data=data, headers=headers, method="PATCH")


def _put(path: str, body: dict | None = None, session_key: str | None = None) -> dict:
    """PUT to a loopback gateway path with the internal-secret handshake.

    Same trust model as :func:`_post` / :func:`_patch` — the target path must be
    listed in ``dashboard.server._MIXED_INTERNAL_API_PATHS`` (or the strict set)
    or the gateway answers 403 ``Token required``.

    ``session_key``: as in :func:`_get`, and it matters more here because this is
    the WRITE. A caller gated on :func:`_resolve_session_key_strict` must send the
    key it verified; re-resolving through the lenient walk would let the request
    carry a different session's authority than the one the gate approved, which on
    this path means writing another crew's work item and public ledger.
    """
    data = json.dumps(body or {}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    return _send(path, data=data, headers=headers, method="PUT")


def _delete(path: str, body: dict | None = None, *, session_key: str | None = None) -> dict:
    """DELETE a loopback gateway path with the internal-secret handshake.

    ``session_key``: as in :func:`_patch`. A caller gated on
    :func:`_resolve_session_key_strict` must send the key it verified.
    """
    data = json.dumps(body or {}).encode() if body else None
    headers = {
        "X-Internal-Secret": _internal_secret(),
        **_caller_header(),
        **_session_token_header(),
    }
    sk = _resolve_session_key() if session_key is None else session_key
    _sk_err = _session_key_header_error(sk)
    if _sk_err:
        return {"error": _sk_err}
    if sk:
        headers["X-Session-Key"] = sk
    if data:
        headers["Content-Type"] = "application/json"
    return _send(path, data=data, headers=headers, method="DELETE", timeout=10)
