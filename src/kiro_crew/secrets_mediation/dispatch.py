"""Trusted dispatch boundary for a mediated secret request.

This is the ONLY place the plaintext of a Custom secret is read for an outbound
API call, and it never leaves this function: the value is resolved from the
vault at the moment of dispatch, injected into the request per the owner's
placement policy, and dropped when the response returns. The value is never
placed in the tool result, an error message, a log line, telemetry, or the SEL
audit record — callers receive only a :class:`SanitizedResponse`.

Order of operations (all fail closed):

1. Load the owner authorization for the secret name. No authorization -> refuse
   BEFORE any vault read or network I/O.
2. Normalize the request URL's origin and require it to equal the authorized
   origin EXACTLY. Mismatch -> refuse before resolving the secret.
3. SSRF-validate + DNS-pin the URL (:mod:`.ssrf`).
4. Resolve the secret (:class:`~kiro_crew.secrets.SecretVault`) and inject it per
   placement — only now, only in memory, only for this send.
5. Send with automatic redirects DISABLED, connecting to the pinned IP. A 3xx is
   handled manually: the redirect target is re-normalized, required to keep the
   SAME authorized origin, and re-pinned before another hop — so a redirect can
   never move the credential-bearing request to a different or internal origin.
6. Return a size- and header-sanitized response.
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import io
import json
import logging
import math
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from cryptography.exceptions import InvalidTag
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.util import connection as urllib3_connection

from kiro_crew.secrets import SecretVault
from kiro_crew.secrets_mediation.policy import (
    CredentialPlacement,
    PolicyError,
    load_authorization,
    normalize_origin,
)
from kiro_crew.secrets_mediation.ssrf import PinnedTarget, SsrfError, check_url
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

#: Methods a mediated request may use.
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})

#: Bounds. A credential-bearing request is short and bounded by construction.
_DEFAULT_TIMEOUT_S = 20.0
_MAX_TIMEOUT_S = 60.0
_MAX_REDIRECTS = 3
_MAX_REQUEST_BODY_BYTES = 256_000
#: Hard cap on how much of a redirect response body is ever buffered. ``requests``
#: consumes a redirect response's body in full (``resolve_redirects`` reads
#: ``resp.content`` to release the socket before the next hop) even with
#: ``allow_redirects=False``, where ``Session.send`` runs that prep once to compute
#: ``r._next`` — so an origin that answers 3xx with an arbitrarily large body would
#: have the whole thing buffered into memory. The mediated result is the fixed
#: withheld constant and never derives anything from the body, so no byte of it is
#: needed: a small cap is enough to let ``requests`` release the connection, and a
#: body past the cap is refused like any other failure. Matches the order of the
#: request-body bound above; the redirect body only has to be drained, not kept.
_MAX_REDIRECT_BODY_BYTES = 64_000
#: Slice reserved WITHIN the normalized request deadline for the overrun teardown
#: (force-close + the grace join confirming the worker unwound), so that teardown
#: completes before the deadline rather than spending time past it where it would
#: be observable. Small relative to the minimum 0.1s timeout floor and clamped to
#: the remaining budget, so it never inverts the join.
_TEARDOWN_RESERVE_S = 0.05

#: The status returned on every mediated call. A FIXED value, never the upstream
#: ``resp.status_code`` — the status code is origin-controlled, so echoing it
#: would leave a covert channel (an origin could encode the injected credential a
#: few bits per call across the status). 0 is not a real HTTP status, so a
#: consumer cannot mistake it for one.
_WITHHELD_STATUS = 0


class MediationError(Exception):
    """A mediated request failed. The message is safe (never the secret)."""


class _BlockAllCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Reject every cookie, so ``requests`` does near-zero work per response.

    ``requests.Session.request`` runs ``extract_cookies_to_jar`` on the response
    before it returns, and ``http.cookiejar`` splits each ``Set-Cookie`` /
    ``Set-Cookie2`` header into attributes with pure-Python, super-linear work. An
    authorized origin can answer with many huge cookie headers to make that parse
    cost origin-chosen CPU time INSIDE the mediated call's padded region — a
    socket force-close cannot interrupt a CPU-bound parse, so the completion time
    overruns the normalized deadline by a secret-attributable amount (the origin
    picks cheap vs expensive headers from the credential it just received). The
    mediated result is a fixed withheld constant that never carries a cookie, so
    cookie parsing is pure dead work here: refusing to set any cookie makes the
    per-response jar work independent of the origin's header choices and closes
    that timing channel at its source.
    """

    def set_ok(self, cookie: Any, request: Any) -> bool:  # noqa: ANN401 - stdlib shape
        return False


def _cookie_free_jar() -> "requests.cookies.RequestsCookieJar":
    """A requests cookie jar that accepts nothing (see :class:`_BlockAllCookiePolicy`)."""
    jar = requests.cookies.RequestsCookieJar()
    jar.set_policy(_BlockAllCookiePolicy())
    return jar


@dataclass
class SanitizedResponse:
    status: int
    headers: dict[str, str]
    body: str
    truncated: bool = False
    #: Redirect origins visited, for the agent's situational awareness. Only the
    #: authorized origin can ever appear here (a cross-origin redirect fails).
    final_url_origin: str = ""


@dataclass
class MediatedRequest:
    """The non-secret request intent supplied by the agent."""

    secret_name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    json_body: Optional[Any] = None
    timeout_s: float = _DEFAULT_TIMEOUT_S


class _PinnedHTTPSAdapter(HTTPAdapter):
    """Force the TCP connection to a pinned IP while keeping TLS SNI + cert
    validation against the real hostname — WITHOUT touching any urllib3 global.

    urllib3 would otherwise resolve the hostname itself, re-opening the
    DNS-rebinding window ``ssrf.check_url`` closed. This adapter installs a
    PoolManager whose connection class dials ``pinned_ip`` for THIS adapter's
    connections only; the connection keeps its ``server_hostname`` and the
    ``Host`` header, so SNI and certificate verification still use the hostname
    and TLS is unchanged. Because the pin lives on the connection instance (not
    on ``urllib3.util.connection.create_connection``), two mediated requests
    running concurrently cannot race to overwrite each other's resolver.
    """

    def __init__(self, pinned_ip: str, *args: Any, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        # Every connection this adapter opens registers itself here while live and
        # removes itself on close. A checked-OUT connection (one a worker is
        # actively using) is absent from the pool's idle ``queue``, so the idle
        # sweep alone cannot reach it; this set is how a timeout force-closes the
        # exact in-flight socket a drip-fed worker is blocked on. Guarded by a lock
        # because register/deregister run on the worker thread while the timeout
        # teardown runs on the caller thread.
        self._live_connections: set[Any] = set()
        self._live_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _register_connection(self, conn: Any) -> None:
        with self._live_lock:
            self._live_connections.add(conn)

    def _deregister_connection(self, conn: Any) -> None:
        with self._live_lock:
            self._live_connections.discard(conn)

    def force_close_live_connections(self) -> None:
        """Hard-abort the socket of every connection this adapter currently holds
        live — checked-out IN-FLIGHT connections included, which the idle-queue
        sweep cannot see. ``shutdown`` + ``close`` makes a blocked ``recv()``
        return at once so the worker thread unwinds. Snapshots under the lock so a
        concurrent deregister cannot mutate the set mid-iteration.

        A connection stalled DURING the TLS handshake needs the extra fd below:
        ``ssl.SSLContext.wrap_socket`` DETACHES the raw socket it is handed, so
        for the whole handshake ``conn.sock`` is that detached raw socket (its
        ``fileno()`` is -1) and shutting it down cannot reach the live fd the
        handshake is blocked on. ``connect`` records a dup of the raw socket's fd
        the instant the socket is created, before wrap detaches it;
        ``shutdown(SHUT_RDWR)`` on that dup unblocks the shared kernel socket the
        handshake is stalled on, and closing the dup drops only our own reference
        (never the live fd). So an origin that stalls its handshake past the
        deadline is force-closed, not left to run the completion loop over the
        budget."""
        with self._live_lock:
            conns = list(self._live_connections)
        for conn in conns:
            # The handshake-window dup first: it reaches the live socket while
            # ``conn.sock`` is still the detached raw socket mid-wrap.
            handshake_sock = getattr(conn, "_mediated_handshake_sock", None)
            if handshake_sock is not None:
                try:
                    handshake_sock.shutdown(socket.SHUT_RDWR)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    handshake_sock.close()
                except Exception:  # noqa: BLE001
                    pass
            # ``conn.sock`` covers the post-handshake live SSL socket and any
            # established/idle connection.
            sock = getattr(conn, "sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().init_poolmanager(*args, **kwargs)
        pinned_ip = self._pinned_ip
        register = self._register_connection
        deregister = self._deregister_connection

        class _PinnedHTTPSConnection(HTTPSConnection):
            def connect(self):  # type: ignore[no-untyped-def]
                # Register BEFORE the (possibly stalling) TLS connect so a worker
                # blocked mid-handshake is still reachable by the timeout teardown.
                register(self)
                super().connect()

            def close(self):  # type: ignore[no-untyped-def]
                try:
                    super().close()
                finally:
                    # Drop our handshake-fd dup once the connection is torn down so
                    # it does not outlive the socket it shadows. Closing the dup
                    # releases only our own reference; it never touches the live fd.
                    handshake_sock = getattr(self, "_mediated_handshake_sock", None)
                    if handshake_sock is not None:
                        self._mediated_handshake_sock = None
                        try:
                            handshake_sock.close()
                        except Exception:  # noqa: BLE001
                            pass
                    deregister(self)

            def _new_conn(self):  # type: ignore[no-untyped-def]
                # Same call urllib3 2.x makes, but to the validated IP instead of
                # ``self._dns_host``. TLS still uses ``self.host`` for SNI and cert
                # verification, so pinning changes only the socket destination.
                raw = urllib3_connection.create_connection(
                    (pinned_ip, self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
                # Keep a dup of the raw socket BEFORE ``ssl.wrap_socket`` detaches
                # it during the TLS handshake. For the whole handshake ``self.sock``
                # is the detached raw socket, so a deadline force-close that only
                # reaches ``self.sock`` cannot abort a stalled handshake; a
                # ``shutdown`` on this dup unblocks the shared kernel socket the
                # handshake is waiting on. The dup is dropped in ``close``.
                try:
                    self._mediated_handshake_sock = raw.dup()
                except Exception:  # noqa: BLE001
                    # A platform that cannot dup leaves the handshake abort to the
                    # connect-timeout bound; never fail the connection over it.
                    self._mediated_handshake_sock = None
                return raw

        class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):
            ConnectionCls = _PinnedHTTPSConnection

        # Register the pinned pool class for https on THIS PoolManager instance.
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme["https"] = _PinnedHTTPSConnectionPool


def _inject(placement: CredentialPlacement, secret_value: str, headers: dict[str, str]) -> None:
    """Inject the resolved secret into *headers* per *placement*. Mutates in place."""
    if placement.type == "bearer":
        headers["Authorization"] = f"Bearer {secret_value}"
    elif placement.type == "header" and placement.header:
        headers[placement.header] = secret_value
    else:  # pragma: no cover - policy validation already rejects other shapes
        raise MediationError("Unsupported credential placement.")


def _withheld_response(final_origin: str) -> SanitizedResponse:
    """The fixed result of EVERY mediated call, returned for every outcome once
    control reaches the padded region — success, an SSRF/DNS refusal, a redirect
    refusal, a transport error, a timeout, an authorized-but-unstored secret, an
    unreadable store — so nothing an owner-authorized origin controls (a body, a
    header, a status, or even error-vs-success) can encode a bit of the credential
    back to the agent. Because it stands for refusals that never sent anything as
    well as for a call that did, the body does NOT assert the request was sent or
    completed. The mediated tool lets the agent USE a secret against an
    owner-authorized origin without ever reading its plaintext; it deliberately
    returns no origin-controlled response value."""
    return SanitizedResponse(
        status=_WITHHELD_STATUS,
        headers={},
        body=(
            "[The response is withheld. This tool lets you USE the secret against "
            "the owner-authorized origin without revealing anything the origin "
            "controls (body, headers, status, or length) and without telling you "
            "whether the request was sent, so a reflected credential has no channel "
            "to ride back out.]"
        ),
        truncated=False,
        final_url_origin=final_origin,
    )


def _wait_for_withheld_deadline(deadline: float) -> None:
    """Pad every mediated completion to its fixed, request-bounded deadline.

    The authorized origin controls when its response completes. Waiting out the
    full normalized request budget makes success, failure, and origin-selected
    latency indistinguishable to the agent — the completion time carries no
    origin-controlled bit.
    """
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _sanitize_response(resp: requests.Response, final_origin: str) -> SanitizedResponse:
    """Return the fixed WITHHELD CONSTANT — never anything derived from the
    upstream response.

    An owner-authorized origin can echo the injected credential back through an
    UNENUMERABLE transform (a hash, a per-field split, a bespoke encoding) that no
    substring scrub can catch, and that is true of the body, the response HEADERS,
    AND the response METADATA (status, content-type, body length): all are
    origin-controlled, so a cooperating origin and agent could encode the
    credential over successive calls into any of them. So the mediated result is a
    CONSTANT that does not vary with ANY upstream response data — no status, no
    content-type, no length, no body, no headers — leaving zero origin-controlled
    channel for a reflection to ride out on. The agent learns only that the call
    completed. There is deliberately NO opt-in to return any origin-controlled
    value: an agent that needs a credential-authorized API's RESPONSE uses a
    different, non-mediated integration; this tool exists to let the agent USE the
    secret against an owner-authorized origin without ever reading its plaintext.
    """
    return _withheld_response(final_origin)


def _iter_outbound_strings(value: Any) -> Any:
    """Yield every string leaf reachable in an agent-supplied outbound value
    (a JSON body, a headers map, or a query map), recursing through dicts and
    lists so a credential nested inside the request body is scanned too."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield k
            yield from _iter_outbound_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_outbound_strings(item)


def _refuse_if_outbound_redacted(url: Any, json_body: Any, headers: Any, query: Any) -> None:
    """Refuse the request if EITHER required redactor would change any
    agent-supplied outbound string.

    ``redact_credentials`` and ``redact_exfiltration_urls`` are the two mandatory
    scans for content that leaves the machine. Here the agent-authored request
    URL, body, headers, and query params are exactly that: they are sent verbatim
    to the external origin. The URL string is scanned whole (path AND query
    included), so a credential the agent moves out of the ``query`` map into the
    URL itself as ``?t=<secret>`` is caught by the same gate rather than slipping
    past it. A redactor changing a value means the payload carries a credential or
    an exfiltration URL, so the whole request fails closed (before the vault read)
    rather than sending it. The injected secret is added by the dispatcher at the
    owner's placement, never by the agent, so a clean payload is unaffected.
    """
    for source in (url, json_body, headers, query):
        for text in _iter_outbound_strings(source):
            if not text:
                continue
            redacted_cred, _ = redact_credentials(text)
            if redacted_cred != text:
                raise MediationError(
                    "The request carries a credential in an agent-supplied value; "
                    "refused before sending."
                )
            redacted_url, _ = redact_exfiltration_urls(text)
            if redacted_url != text:
                raise MediationError(
                    "The request carries a disallowed URL in an agent-supplied "
                    "value; refused before sending."
                )


def perform_mediated_request(req: MediatedRequest, config_dir: str | Path) -> SanitizedResponse:
    """Execute *req*, injecting the named secret only inside this call.

    Raises :class:`MediationError` / :class:`PolicyError` / :class:`SsrfError`
    (all fail closed) on any policy, destination, or transport violation. The
    exception messages are safe to surface: they never contain the secret value.
    """
    method = (req.method or "").upper()
    if method not in ALLOWED_METHODS:
        raise MediationError(f"HTTP method {req.method!r} is not allowed.")
    raw_timeout = float(req.timeout_s or _DEFAULT_TIMEOUT_S)
    if not math.isfinite(raw_timeout):
        # A NaN/Infinity timeout (valid JSON "NaN"/"Infinity" via float()) defeats
        # min/max — every comparison is False, so the NaN propagates into the
        # end-to-end deadline (time.monotonic() + NaN) and breaks the timeout.
        # Refuse it cleanly rather than let a non-finite budget through.
        raise MediationError("Request timeout must be a finite number.")
    timeout = min(max(raw_timeout, 0.1), _MAX_TIMEOUT_S)

    # 1. Owner authorization (fail closed before any vault read / network).
    auth = load_authorization(config_dir, req.secret_name)

    # 2. Request origin must equal the authorized origin EXACTLY.
    # Reject confusable URLs BEFORE the origin check: a ``userinfo@`` authority or
    # a backslash can be split one way by our parser and another by the transport,
    # so ``normalize_origin`` might read the authorized host while the request is
    # actually sent to an attacker host — injecting the secret off-origin. urlsplit
    # keeps userinfo out of ``hostname``, and requests/urllib3 translate ``\`` to
    # ``/``; refusing both here removes the divergence rather than trusting the two
    # parsers to agree.
    _reject_confusable_url(req.url)
    request_origin = normalize_origin(req.url)
    if request_origin != auth.origin:
        raise PolicyError(
            f"Secret {req.secret_name!r} is authorized only for {auth.origin}, not "
            f"{request_origin}. The request was refused before the secret was read."
        )

    # Caller-supplied headers must not collide with the injection slot or set
    # framing/host. Drop any that would.
    safe_caller_headers = _filter_caller_headers(req.headers, auth.placement)

    if req.json_body is not None:
        encoded = json.dumps(req.json_body).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BODY_BYTES:
            raise MediationError("Request body is too large.")

    # Scan every AGENT-controlled outbound value (the request URL, the body, the
    # caller headers, and the query params) with BOTH required redactors and refuse
    # the whole request if either would change any of them. The agent must never
    # smuggle a credential or an exfiltration URL OUT through the request it hands
    # us: those values are model-authored and leave the machine to the external
    # origin, so a match means the outbound payload carries exactly what these
    # controls exist to stop. The URL is scanned whole (path and query included), so
    # a value refused in the ``query`` map cannot slip out spelled ``?t=<secret>`` in
    # the URL instead. Fail closed BEFORE the secret is read, so a refused request
    # never mints the credential — the injected secret is added by us at the
    # authorized placement, never by the agent, so a clean payload loses nothing.
    _refuse_if_outbound_redacted(req.url, req.json_body, safe_caller_headers, dict(req.query or {}))

    # One absolute deadline starts BEFORE the SSRF/DNS check, and every outcome
    # from here on — an SSRF/DNS refusal, a transport error, a redirect refusal, a
    # timeout, or a success — collapses to the SAME fixed withheld constant padded
    # to that deadline. The origin controls its own DNS, so a per-call
    # blocked-vs-public DNS answer would otherwise be a fast pre-deadline refusal
    # versus a padded success — a timing oracle a hostile origin that already
    # learned the secret could drive one bit at a time. Padding the SSRF/DNS
    # refusal too collapses that channel. The secret is read ONLY after a
    # successful check_url, so a refused destination never resolves the credential.
    send_deadline = time.monotonic() + timeout
    secret_value = ""
    try:
        try:
            # 3. SSRF-validate + DNS-pin (inside the padded region). The DNS
            # lookup is bounded by the same deadline so its latency cannot push
            # completion past the normalized budget and leak an origin-controlled
            # bit.
            target = check_url(req.url, send_deadline)

            # 4. Resolve the secret — only now, only in memory, only after the
            # destination validated, only for this send.
            vault = SecretVault(config_dir)
            try:
                secret = vault.get(req.secret_name)
            except (ValueError, InvalidTag, OSError):
                # A corrupt store (ValueError), a mismatched .vault_key
                # (InvalidTag), or an unreadable store (OSError — the file is
                # missing, permission-denied, or the I/O failed) is a plausible
                # operational state. Translate it to the fixed mediated refusal
                # rather than letting it escape as a raw 500: an OSError that
                # reaches the handler strands the already-consumed capability
                # (it cannot be retried) AND returns the wrong, un-padded status.
                # Refusing here collapses it to the same withheld constant every
                # other error path returns, with no store internals in the
                # traceback.
                raise MediationError(
                    "The secret store could not be read; the request was refused."
                ) from None
            if secret is None:
                raise MediationError(
                    "The secret is authorized but not stored in the vault; refused."
                )
            secret_value = secret.reveal()
            result = _dispatch_with_redirects(
                method=method,
                url=req.url,
                query=dict(req.query or {}),
                headers=safe_caller_headers,
                json_body=req.json_body,
                placement=auth.placement,
                secret_value=secret_value,
                authorized_origin=auth.origin,
                target=target,
                deadline=send_deadline,
            )
        except (MediationError, PolicyError, SsrfError):
            # Every outcome collapses to the SAME fixed constant a success returns,
            # so neither an error-vs-success signal nor a DNS blocked-vs-public
            # timing difference can encode a secret bit. All pre-send refusals
            # (method, timeout finiteness, authorization, origin mismatch,
            # confusable URL, body size, outbound redaction) were raised ABOVE,
            # before this padded region. The post-send outcome is NOT audited apart
            # from a success either: ``security_events.jsonl`` is a sandbox-visible
            # leaf, so recording refused-vs-ok there would itself be a readable
            # one-bit oracle on the authorized origin's behaviour. The result is the
            # withheld constant and nothing distinguishes it from a success.
            result = _withheld_response(auth.origin)
        _wait_for_withheld_deadline(send_deadline)
        return result
    finally:
        # Best-effort scrub of the local reference. Python strings are immutable
        # so this only drops our binding, but it keeps the plaintext out of any
        # frame that outlives this call (e.g. a traceback capturing locals).
        secret_value = ""  # noqa: F841


def _filter_caller_headers(
    headers: dict[str, str], placement: CredentialPlacement
) -> dict[str, str]:
    forbidden = {"host", "content-length", "connection", "transfer-encoding", "authorization"}
    if placement.type == "header" and placement.header:
        forbidden.add(placement.header.lower())
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.lower() in forbidden:
            continue
        if any(ord(c) < 0x20 or c in "\r\n" for c in k + v):
            continue
        out[k] = v
    return out


def _reject_confusable_url(url: str) -> None:
    """Refuse a URL whose authority a parser and the transport could read apart.

    Two classic origin-confusion vectors: ``userinfo@host`` (a ``@`` in the
    authority — ``user@authorized.example`` vs ``authorized.example@evil`` are
    read differently by different parsers) and a backslash (browsers and some
    HTTP stacks fold ``\\`` to ``/``, moving the host boundary). Refusing both,
    plus any control char, means the origin ``normalize_origin`` validates is the
    origin the request is actually sent to.
    """
    if not isinstance(url, str) or not url:
        raise MediationError("The request URL is missing.")
    if "\\" in url:
        raise MediationError("The request URL must not contain a backslash.")
    if any(ord(c) < 0x20 for c in url):
        raise MediationError("The request URL must not contain control characters.")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        # A malformed authority (e.g. an unmatched IPv6 bracket) makes urlsplit
        # itself raise. Convert it to the boundary's own error so a bad URL exits
        # as a mediation refusal, never an uncaught 500.
        raise MediationError("The request URL is malformed.")
    # ``netloc`` holds ``[userinfo@]host[:port]``; a ``@`` means userinfo is
    # present, which is the ambiguous form we refuse rather than try to canonicalize.
    if "@" in parts.netloc:
        raise MediationError(
            "The request URL must not contain userinfo (a '@' in the host); refused."
        )


def _force_close_adapter_connections(adapter: HTTPAdapter) -> None:
    """Hard-abort every live socket the adapter holds — checked-out AND idle.

    ``requests.Session.close`` / ``HTTPAdapter.close`` return pooled connections
    and close IDLE ones, but a connection whose worker thread is blocked in a
    ``recv()`` on a drip-feeding origin is checked OUT of the pool, so it is NOT
    in the idle ``queue`` and the idle sweep alone cannot reach it — the daemon
    thread and its socket would outlive the deadline, and repeated timed-out
    calls would pile them up without bound. The authoritative teardown is the
    adapter's own live-connection registry, which tracks the exact in-flight
    connection each worker checked out (see ``_PinnedHTTPSAdapter``); closing its
    socket makes the blocked ``recv()`` return at once. The idle-``queue`` sweep
    below is belt-and-suspenders for any connection that was returned to the pool
    but not yet closed. Every step is best-effort because a half-built connection
    may expose none of these attributes yet.
    """
    # Primary: the checked-out, in-flight connection(s) the workers are using.
    close_live = getattr(adapter, "force_close_live_connections", None)
    if callable(close_live):
        try:
            close_live()
        except Exception:  # noqa: BLE001
            pass
    # Belt-and-suspenders: any connection still sitting idle in a pool queue.
    pools = getattr(getattr(adapter, "poolmanager", None), "pools", None)
    if pools is None:
        return
    try:
        connection_pools = list(pools._container.values())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            connection_pools = list(pools.values())
        except Exception:  # noqa: BLE001
            return
    for pool in connection_pools:
        conns = getattr(pool, "pool", None)
        if conns is None:
            continue
        try:
            queued = list(getattr(conns, "queue", []))
        except Exception:  # noqa: BLE001
            queued = []
        for conn in queued:
            sock = getattr(conn, "sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


# urllib3 loggers that can emit ORIGIN-CONTROLLED bytes while a response is read.
# ``urllib3.connectionpool`` logs a WARNING carrying the raw header text when
# ``assert_header_parsing`` reports defects (a colonless/garbage header line), and
# the record's args include the ``HeaderParsingError`` whose ``defects`` and
# ``unparsed_data`` are the origin's own bytes. ``urllib3.connection`` /
# ``urllib3.response`` can likewise surface origin text. The filter is attached to
# each one directly rather than to the parent ``urllib3`` logger because a logging
# Filter only screens records emitted ON the logger it is attached to, not records
# that merely PROPAGATE up from a child.
_ORIGIN_NOISY_URLLIB3_LOGGERS = (
    "urllib3.connectionpool",
    "urllib3.connection",
    "urllib3.response",
)


class _DropAllFilter(logging.Filter):
    """A logging ``Filter`` that drops every record it sees."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib name
        return False


@contextlib.contextmanager
def _suppress_origin_urllib3_logs() -> Any:
    """Drop every urllib3 log record for the duration of the mediated send.

    The mediated response is built as a fixed withheld/sanitized constant so no
    origin byte reaches the agent, telemetry, or the SEL record (see
    :func:`_sanitize_response`). urllib3's OWN diagnostics are a channel outside
    that invariant: when a cooperating authorized origin returns the injected
    Custom-secret value as a colonless header line, urllib3's header parser raises
    :class:`~urllib3.exceptions.HeaderParsingError` and logs the raw header text
    (including the exception's ``defects``/``unparsed_data`` and traceback) at
    WARNING on ``urllib3.connectionpool`` — and the installed credential redactor
    does not match arbitrary Custom-secret values, so the plaintext would land in
    the logs. This context manager attaches a drop-all filter to the urllib3
    loggers that can carry origin bytes, so NO such record — header value,
    exception args, or traceback — reaches any handler during the send. The
    filters are removed on exit, so only the mediated call is affected; ordinary
    urllib3 logging elsewhere is untouched. This is the log-channel counterpart to
    the response-withholding invariant: the origin gets no oracle, in the result
    OR in the logs.
    """
    drop = _DropAllFilter()
    loggers = [logging.getLogger(name) for name in _ORIGIN_NOISY_URLLIB3_LOGGERS]
    for lg in loggers:
        lg.addFilter(drop)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeFilter(drop)


def _request_within_deadline(
    session: requests.Session,
    adapter: HTTPAdapter,
    *,
    deadline: float,
    **request_kwargs: Any,
) -> requests.Response:
    """Perform ``session.request(**kwargs)`` without letting the TLS handshake and
    response-header read outlive the absolute *deadline*.

    ``requests``' ``timeout`` is a per-read INACTIVITY timeout, not an absolute
    deadline: a complicit origin can drip-feed the TLS handshake or response
    headers, each byte inside the inactivity window, so ``session.request``
    returns only after an origin-chosen total time. Inside the mediated call's
    padded region that stall is a timing channel — the deadline padding only waits
    UP TO the normalized budget, so a header read that overruns it pushes total
    completion PAST the deadline by an origin-controlled amount, one bit of a
    credential the origin already learned. Running the call on a worker thread and
    waiting on it only until ``deadline`` makes the header-read latency bounded by
    the same normalized budget as every other outcome (mirrors
    :func:`ssrf._resolve_within_deadline` for DNS). On overrun the worker's live
    socket is force-closed (``shutdown`` + ``close``), which aborts the blocked
    recv so the daemon thread unwinds AT ONCE — ``Session.close`` alone would wait
    on the busy connection and leave the thread and its socket running, so
    repeated timed-out calls would leak threads/connections without bound. The
    teardown then waits only until the ABSOLUTE deadline: socket force-close
    cannot reach a worker stuck in CPU-BOUND response PROCESSING (``requests``
    parses the response in pure Python after the bytes are read, and a thread
    cannot be interrupted mid-parse), so once the socket is dead the caller stops
    waiting at the deadline rather than spinning until an origin-chosen parse
    finishes — otherwise that parse would push completion past the budget by a
    secret-attributable amount. A :class:`MediationError` is raised and the caller
    collapses it to the padded withheld constant. The abandoned thread holds no
    secret, has no live socket to send on, and finishes harmlessly.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediationError("The mediated request exceeded its overall time budget.")

    result: list[requests.Response] = []
    error: list[BaseException] = []

    def _call() -> None:
        try:
            # Recheck the absolute deadline on the WORKER, immediately before the
            # send. The deadline is struck before the DNS/SSRF pin and the vault
            # read, so by the time the worker runs ``remaining`` can already be
            # below the reserve — which makes the main thread's
            # ``join(timeout=remaining - reserve)`` a 0.0s no-op that hands the
            # worker no supervised window at all. A worker still pre-
            # ``create_connection`` is then unreachable by the overrun force-close
            # (no socket to shut down) and ``session.close`` leaves the pool able
            # to build a fresh connection, so the daemon could connect and issue
            # the request — a mutation (e.g. a DELETE) landing at the origin AFTER
            # the caller already raised timeout, with no recovery. Guarding here
            # means a worker whose deadline is already spent never calls
            # ``session.request`` at all: it refuses in-thread exactly as an
            # overrun would, so no send can outlive the normalized budget.
            left = deadline - time.monotonic()
            if left <= 0:
                raise MediationError("The mediated request exceeded its overall time budget.")
            # Bind the socket connect AND the response-header read to the time
            # LEFT until the absolute deadline, recomputed HERE rather than reused
            # from the ``remaining`` struck before DNS/vault. ``requests``' timeout
            # is a per-phase INACTIVITY timeout, so a single pre-send check that
            # passes with milliseconds left still lets a slow connect or a
            # drip-fed header read run on — each byte inside the inactivity window
            # — and complete the mutation PAST the deadline, while the caller has
            # already returned on timeout. A ``(connect, read)`` tuple each capped
            # at ``left`` makes connect+read inactivity bounded by the deadline:
            # neither phase can make the socket outlive the budget on its own, and
            # the worker-thread join + force-close below bounds the sum. No byte
            # leaves after the deadline.
            send_kwargs = dict(request_kwargs)
            send_kwargs["timeout"] = (left, left)
            # Suppress urllib3's own origin-controlled diagnostics for the send:
            # a cooperating origin's malformed (e.g. colonless) header line makes
            # urllib3 log the raw header bytes at WARNING, a leak channel outside
            # the response-withholding invariant. See
            # _suppress_origin_urllib3_logs.
            with _suppress_origin_urllib3_logs():
                result.append(session.request(**send_kwargs))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            error.append(exc)

    worker = threading.Thread(target=_call, name="mediated-request", daemon=True)
    worker.start()
    # Reserve a teardown slice INSIDE the normalized budget: join the worker only
    # up to deadline-minus-reserve, so the overrun teardown (force-close + the
    # grace join confirming the thread unwound) also completes before the deadline
    # rather than after it. If the reserve were spent AFTER the deadline instead,
    # its duration would land outside the padding window at
    # _wait_for_withheld_deadline (which no-ops once remaining <= 0) and be
    # observable by the caller — and its duration DEPENDS on the stall point the
    # origin picked (a create_connection stall leaves conn.sock None, so the
    # socket force-close is a no-op and the join waits out its full timeout),
    # which is exactly the per-call bit this module's padding exists to deny.
    reserve = min(_TEARDOWN_RESERVE_S, remaining)
    worker.join(timeout=max(0.0, remaining - reserve))
    if worker.is_alive():
        # The handshake/header read did not finish inside the budget. Hard-abort
        # the live socket so a blocked recv returns and the daemon thread
        # terminates — Session.close waits on the busy connection, so closing the
        # socket is what actually bounds the thread's lifetime. A worker stalled in
        # create_connection (socket not yet created, so unreachable by the
        # force-close) is bounded instead by its own connect timeout, which is the
        # per-read ``timeout`` and so never exceeds the budget. Then refuse:
        # completion time is bounded by the normalized budget, not by the origin's
        # drip rate, so it carries no secret-attributable bit. A response that
        # races in after the abort is discarded by the caller.
        # Session.close waits on a busy connection, so close the pooled session
        # first (frees idle connections) but rely on the force-close loop below to
        # abort the in-flight socket — that is what actually bounds the thread.
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
        # Do NOT return while the worker can still SEND. A single bounded join is
        # not enough: a worker caught in the window BETWEEN the first force-close
        # and its own ``create_connection`` opens a fresh socket the first abort
        # never saw, and could still write the mutation after the caller returned.
        # Re-abort any socket that appeared since the last pass, so no live socket
        # the worker could write to survives this teardown. The connect/read
        # timeouts are bounded to the deadline (set in ``_call``), so a worker
        # stalled in socket I/O converges here rather than spinning.
        #
        # But socket force-close cannot reach a worker stuck in CPU-BOUND response
        # PROCESSING — ``requests`` parses the response after the bytes are read,
        # in pure Python, and a thread cannot be interrupted mid-parse. An origin
        # that drives that parse (cookie extraction is the worst case, now blocked
        # by ``_cookie_free_jar``; a pathological header block another) could keep
        # the daemon ``is_alive()`` with its socket already dead, so an UNBOUNDED
        # ``while worker.is_alive()`` loop would block the caller past the deadline
        # by an origin-chosen, secret-attributable amount — the exact timing bit
        # this padding exists to deny. So the loop is bounded by the ABSOLUTE
        # deadline: once the socket is force-closed (no further send possible), the
        # caller stops waiting at the deadline and refuses even if the daemon is
        # still parsing. The abandoned thread holds no secret, has no live socket
        # to write to, and finishes harmlessly; completion time is the normalized
        # budget for every stall point, I/O-bound or CPU-bound alike. Each wait is
        # a tiny reserve slice absorbed by _wait_for_withheld_deadline when short.
        while worker.is_alive():
            _force_close_adapter_connections(adapter)
            left = deadline - time.monotonic()
            if left <= 0:
                # Deadline reached and the worker is still in uninterruptible
                # CPU-bound processing. Its socket is dead (force-closed above), so
                # it can neither send nor mutate; abandon it and refuse now rather
                # than let an origin-driven parse push completion past the budget.
                break
            worker.join(timeout=min(reserve, left))
        raise MediationError("The mediated request exceeded its overall time budget.")
    if error:
        raise error[0]
    return result[0]


def _bound_redirect_body(resp: "requests.Response", **_kwargs: Any) -> None:
    """Cap a redirect response's body before anything buffers it in full.

    ``requests`` releases a redirect response's socket by reading its body in
    full (``resolve_redirects`` -> ``resp.content``), and ``Session.send`` runs
    that prep once to compute ``r._next`` even with ``allow_redirects=False`` —
    so an origin answering 3xx with an arbitrarily large body would have the
    whole thing buffered into memory. This runs as a ``response`` hook, which
    ``Session.send`` dispatches BEFORE that ``_next`` prep, so it is the one point
    where the body can be bounded before ``requests`` consumes it.

    Only a redirect body is bounded here: a non-redirect response is closed
    without draining (its body is never read), so capping it would be dead work.
    The read is one bounded ``raw.read`` of at most one byte past the cap; a body
    at or under the cap is replaced with a finite in-memory stream so the later
    ``resp.content`` consume returns those exact bytes and nothing reads the
    socket again, and a body over the cap is refused like any other failure
    (the caller collapses :class:`MediationError` to the fixed withheld
    constant, so this adds no error-vs-success oracle). The mediated result is
    the withheld constant and never derives from the body, so no byte of it is
    needed beyond releasing the connection.
    """
    if resp.status_code not in (301, 302, 303, 307, 308):
        return
    try:
        raw = resp.raw
        peeked = raw.read(_MAX_REDIRECT_BODY_BYTES + 1, decode_content=False)
    except Exception as exc:  # noqa: BLE001 — a body read failure is just a failed hop
        raise MediationError(
            f"The redirect response body could not be read ({type(exc).__name__})."
        ) from None
    if len(peeked) > _MAX_REDIRECT_BODY_BYTES:
        raise MediationError("The redirect response body exceeded the size limit; refused.")
    # Hand ``requests`` a finite, already-drained stream so its own
    # ``resp.content`` consume reads these bounded bytes rather than the socket.
    resp.raw = io.BytesIO(peeked)
    resp._content = False  # type: ignore[attr-defined]  # let .content recompute from the capped raw
    resp._content_consumed = False  # type: ignore[attr-defined]


def _dispatch_with_redirects(
    *,
    method: str,
    url: str,
    query: dict[str, str],
    headers: dict[str, str],
    json_body: Optional[Any],
    placement: CredentialPlacement,
    secret_value: str,
    authorized_origin: str,
    target: PinnedTarget,
    deadline: float,
) -> SanitizedResponse:
    current_url = url
    current_target = target
    current_method = method
    current_json = json_body
    for _hop in range(_MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediationError("The mediated request exceeded its overall time budget.")
        # Re-guard every hop's URL before the secret is injected: a redirect
        # Location (already origin-checked below) must also be free of the
        # userinfo/backslash confusion that could split one way here and another
        # in the transport. The secret is only injected into a URL that passed.
        _reject_confusable_url(current_url)
        send_headers = dict(headers)
        _inject(placement, secret_value, send_headers)

        session = requests.Session()
        # No environment proxies/netrc; we control the destination fully.
        session.trust_env = False
        # Reject every response cookie: the mediated result carries none, and a
        # super-linear cookie parse over origin-chosen Set-Cookie headers is a
        # CPU-bound timing channel the socket force-close cannot interrupt (see
        # _BlockAllCookiePolicy).
        session.cookies = _cookie_free_jar()
        adapter = _PinnedHTTPSAdapter(current_target.ip, max_retries=0)
        session.mount("https://", adapter)
        try:
            resp = _request_within_deadline(
                session,
                adapter,
                deadline=deadline,
                method=current_method,
                url=current_url,
                params=query or None,
                headers=send_headers,
                json=current_json,
                # Per-read inactivity backstop; the absolute-deadline bound that
                # closes the timing channel is enforced by _request_within_deadline.
                timeout=remaining,
                allow_redirects=False,
                stream=True,
                hooks={"response": _bound_redirect_body},
            )
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    # ``urljoin`` -> ``urlsplit`` raises ValueError on a malformed
                    # origin-controlled Location (e.g. an invalid IPv6 literal).
                    # Collapse it to MediationError so it rides the default-path
                    # withheld-constant collapse rather than escaping as a 500 —
                    # a raw 500 is distinguishable from the fixed withheld result
                    # and would restore a per-call error-vs-withheld oracle.
                    try:
                        location = requests.compat.urljoin(current_url, resp.headers["location"])
                    except ValueError as exc:
                        raise MediationError(
                            "The response redirect location could not be parsed "
                            f"({type(exc).__name__})."
                        ) from None
                    # A redirect may not move the credential to another origin.
                    try:
                        next_origin = normalize_origin(location)
                    except PolicyError:
                        raise PolicyError(
                            "The response redirected to a non-https destination; refused."
                        )
                    if next_origin != authorized_origin:
                        raise PolicyError(
                            "The response tried to redirect the credential-bearing request to a "
                            "different origin; refused."
                        )
                    current_target = check_url(location, deadline)  # re-SSRF-pin the new hop
                    current_url = location
                    query = {}  # query already applied; don't re-append on the redirect
                    # RFC 7231/7538 method handling, so a redirect never REPLAYS a
                    # mutation: 303 always becomes GET with no body; a 301/302 on ANY
                    # unsafe method (POST/PUT/PATCH/DELETE — not just POST) also
                    # degrades to GET with no body, so a same-origin 301/302 after a
                    # PATCH/PUT/DELETE cannot re-run the write on the next hop. Only
                    # GET/HEAD keep their method on 301/302/303; 307/308 preserve both
                    # method AND body by design.
                    code = resp.status_code
                    _SAFE = {"GET", "HEAD"}
                    if code in (301, 302, 303) and current_method not in _SAFE:
                        current_method = "GET"
                        current_json = None
                    continue
                # The mediated result is the fixed withheld constant and never
                # includes anything derived from the upstream body, so there is no
                # reason to read it — and reading it is a timing channel: a
                # complicit origin can send headers fast then DRIP the body, so
                # the per-read/deadline checks only fire on the next chunk yield
                # and the origin controls completion timing past the normalized
                # deadline. Close the response right after headers (releasing the
                # connection WITHOUT draining the body), so a hostile origin cannot
                # hold it open and the whole operation stays within the deadline.
                resp.close()
                return _sanitize_response(resp, authorized_origin)
            finally:
                resp.close()
        except requests.RequestException as exc:
            raise MediationError(f"The mediated request failed: {type(exc).__name__}.") from exc
        except UnicodeError as exc:
            # An injected credential (or caller header) that is not HTTP-encodable
            # (headers are latin-1) makes http.client raise UnicodeEncodeError — a
            # ValueError, NOT a RequestException, so it would otherwise escape here
            # and could surface the secret in a traceback. Translate to a
            # secret-free MediationError; never chain the cause (it can carry the
            # offending bytes). ``from None`` drops the original.
            raise MediationError(
                "The mediated request could not be encoded for transport "
                f"({type(exc).__name__})."
            ) from None
        except ValueError as exc:
            # ``Session.send`` prepares ``r._next`` even with
            # ``allow_redirects=False`` by running ``resolve_redirects`` once with
            # ``yield_requests=True`` (sessions.py), which ``urlparse``/rebuilds
            # the raw origin-controlled ``Location`` header. A malformed Location
            # (e.g. an invalid IPv6 literal in its authority) makes that
            # preparation raise a bare ValueError — not a RequestException and not
            # the UnicodeError handled above — so without this clause it escapes
            # the mediated path as a distinguishable error, while every other
            # outcome collapses to the fixed withheld constant: an
            # error-vs-success oracle a hostile origin drives by its own header.
            # Collapse it to the same secret-free MediationError so a bad Location
            # fails identically to any other failure, preserving the
            # constant-time/padded withheld behavior. Never chain the cause (it can
            # carry the offending header bytes); ``from None`` drops the original.
            raise MediationError(
                "The mediated request failed while preparing the response "
                f"({type(exc).__name__})."
            ) from None
        finally:
            session.close()
    raise MediationError("The mediated request exceeded the redirect limit.")
