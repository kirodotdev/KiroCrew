"""Where a kirocrew-core tool's gateway call goes.

The gateway's port, base URL and unix socket and the caches that pin them, one
request's target resolution, the refused-target replay and re-verification, the
internal secret, the identity headers every verb sends, and the retry schedule,
failure text and error body ``mcp_core._send`` uses.

:mod:`kiro_crew.mcp_core` stays the import path and the patch surface; see
:mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

import json
import re as _re
import urllib.request

from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.origin import dashboard_socket_path
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_caller import current_caller
from kiro_crew.mcp_shared import internal_caller
from kiro_crew.port_resolution import port_is_gateway_owned, resolve_client_port_src
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def _resolve_api_port() -> tuple[int, str]:
    """Resolve the gateway API port a callback should aim at.

    Delegates to :func:`kiro_crew.port_resolution.resolve_client_port_src`, the
    same precedence every client CLI command applies: ``KIROCREW_PORT``, then a
    port **explicitly written** in ``dashboard.url``, then the sole
    gateway-owned run-marker, then the documented default. The marker step is
    what keeps a portless ``dashboard.url`` from collapsing to the default
    port while a live gateway is bound elsewhere — ``parse_dashboard_url``
    substitutes the default for the *server's* benefit (it must bind
    something), which is exactly the wrong guess for a client callback.

    Returns ``(port, source)`` — the source string names the chain step that
    produced the port (``"env"`` / ``"bound"`` / ``"config"`` / ``"marker"`` /
    ``"default"``), because the caching rule below differs by source.
    """
    return resolve_client_port_src(None)


#: Port sources stable for the process lifetime, safe to pin. Deliberately
#: excludes ``"marker"``: a marker-discovered port is verified at that instant
#: only — the gateway can exit or move and any local process may then rebind
#: the port, so every secret-bearing request must re-run the discovery chain
#: (which re-proves ownership via ``_gateway_owns_port``) instead of trusting
#: a pinned value. Also excludes ``"default"``, the no-evidence fall-through.
_STABLE_PORT_SOURCES = frozenset({"cli", "env", "bound", "config"})


# Lazily-resolved caches for the gateway API port, base URL, and unix-socket
# path. ``None`` means "not resolved yet"; the getters below fill them on
# first use (tests may pre-seed any of them with a concrete value).
# Deliberately NOT computed at import: resolution reads config and the
# gateway's live run-marker, both of which can change between process start
# and the first gateway call — an import-time snapshot froze a wrong guess
# for the whole process lifetime. A REQUEST derives its URL and socket path
# from one resolution (``_resolve_api_target``), so the two transports of an
# attempt can never name different gateways; these caches hold that pair only
# for the stable sources that are safe to pin.
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
        # Both memos populated: hand back exactly what the standalone getters
        # would answer. The condition is deliberately NOT also `_API_PORT is not
        # None`. `_api_base` / `_api_unix_socket` return a populated memo
        # whatever the port cache holds, and tests pre-seed these two directly
        # (the cache comment above documents that) -- requiring the port here
        # made this resolver discard a seeded pair and re-resolve, which is the
        # very drift between `_send` and the getters this function exists to
        # remove. In production the two are written together and
        # `_invalidate_api_base` clears them together, so they stay consistent.
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


# Extra attempts a REFUSED target gets, and the pause before each.
#
# A refusal is the one failure where nothing reached the gateway, so re-dialling
# the identical request cannot double-execute a verb — that invariant is what
# makes this retry legal (see ``_retry_refused`` inside :func:`_send`). Two extra
# dials, pausing ~250ms then ~500ms, cover the sub-second window in which a
# restarting gateway is rebinding its port, without making a genuinely-down
# gateway feel hung.
_REFUSED_RETRY_BACKOFFS: tuple[float, ...] = (0.25, 0.5)


def _refused_message(refused_base: str, exc: BaseException) -> str:
    """Actionable text for a base that refused every attempt.

    A bare ``<urlopen error [Errno 61] Connection refused>`` tells the caller
    nothing it can act on, so name the address that was dialled and the likely
    reason (the gateway is down, or restarting) together with the advice that
    follows from it. The raw error is appended so the original diagnostic
    survives for a bug report.

    The address comes from the base that was actually refused; re-resolving here
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
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "error" in parsed:
                message = str(parsed["error"])
                # Preserve api_spawn's "this rejection was already counted"
                # marker (wave-liveness reconcile) — it must survive the
                # error-body flattening or spawn_run would double-reconcile
                # in-process rejections and close waves early.
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
    return out
