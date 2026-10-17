"""Shared HMAC verification for the gateway → app-backend reverse proxy.

The gateway (``apps/routes.py::handle_app_api_proxy``) signs every forwarded
request with the per-app secret::

    X-KiroCrew-Proxy: <ts>:<hmac_sha256(secret, "<ts>:<method>:<target>:<sha256(body)>")>

where ``<target>`` is the forwarded request-target the backend receives as
``self.path`` (e.g. ``/api/read?path=x``).

Built-in app backends bind plain loopback ``ThreadingHTTPServer`` sockets, so
without verifying this HMAC any *other* local process — another app's backend,
a compromised/third-party app, or the prompt-injectable agent — could connect
directly and bypass the gateway's token auth + per-app scope enforcement
(CWE-306, "network-only authentication"). Backends receive the secret via the
``KIROCREW_PROXY_SECRET`` env var injected at spawn
(``apps/backend.py::_start_app_backend_body``).

The gateway's own health probe hits the backend directly (unsigned), so callers
must leave the health endpoint unauthenticated — see the backends' dispatch.

An app whose manifest sets ``backend.signedPrincipal`` also receives
``X-KiroCrew-Principal``: a base64url canonical-JSON claim naming the kind of
caller the gateway authenticated, a period, and an HMAC that binds that exact
claim to the method, target, body digest and issue time. A backend trusts the
claim only through :func:`verify_proxy_principal_claim`, called with one
:class:`ProxyPrincipalReplayCache` that lives as long as the backend process.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # typing only — ThreadingHTTPServer backends never need aiohttp
    from aiohttp import web

_ENV_KEY = "KIROCREW_PROXY_SECRET"
_MAX_SKEW_SECONDS = 60

PROXY_PRINCIPAL_HEADER = "X-KiroCrew-Principal"
PRINCIPAL_OWNER_SESSION = "owner-session"
PRINCIPAL_APP_TOKEN = "app-token"
PRINCIPAL_AGENT_TOOL = "agent-tool"
PRINCIPAL_NONE = "none"
_PRINCIPAL_KINDS = frozenset(
    {PRINCIPAL_OWNER_SESSION, PRINCIPAL_APP_TOKEN, PRINCIPAL_AGENT_TOOL, PRINCIPAL_NONE}
)
_PRINCIPAL_KEYS = frozenset({"version", "kind", "ownerId", "issuedAt", "requestId"})
_PRINCIPAL_SIGNATURE_DOMAIN = "kirocrew-proxy-principal-v1"
_MAX_PRINCIPAL_HEADER_BYTES = 4096
_MAX_OWNER_ID_LENGTH = 256
_REQUEST_ID_HEX_LENGTH = 32


def raw_request_target(request: web.Request) -> str:
    """The raw, percent-encoded request-target an aiohttp backend received.

    The gateway signs ``target_url.raw_path_qs`` — the request-target exactly
    as it goes on the wire, query string included, with no ``?`` when there is
    none. aiohttp *decodes* ``request.path`` and ``request.query_string``, so a
    reconstruction from those diverges from the signed bytes (and fails closed
    with 401) as soon as a query parameter carries a percent-encodable
    character such as a space, ``+``, or non-ASCII. ``request.raw_path`` is the
    request line's target verbatim, so verifying against it recomputes the HMAC
    over the same bytes the gateway signed. aiohttp middlewares must use this;
    ``ThreadingHTTPServer`` backends already get the raw form as ``self.path``.
    """
    return request.raw_path


def proxy_secret() -> str:
    """The per-app proxy secret injected into this backend's environment."""
    return os.environ.get(_ENV_KEY, "")


def verify_proxy_request(
    header_value: str,
    *,
    method: str,
    target: str,
    body: bytes,
    secret: str | None = None,
    now: float | None = None,
) -> bool:
    """Return ``True`` iff *header_value* is a valid, fresh gateway signature.

    Fails closed: a missing secret, absent/malformed header, non-numeric or
    stale (±60s) timestamp, non-ASCII signature, or signature mismatch all
    return ``False``.
    """
    key = proxy_secret() if secret is None else secret
    if not key or not header_value or ":" not in header_value:
        return False
    ts_str, _, sig = header_value.partition(":")
    if not ts_str.isdigit() or not sig:
        return False
    clock = time.time() if now is None else now
    if abs(clock - int(ts_str)) > _MAX_SKEW_SECONDS:
        return False
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{ts_str}:{method}:{target}:{body_hash}"
    expected = hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    # Compared as BYTES, never as ``str``. ``hmac.compare_digest`` rejects a str
    # holding a non-ASCII character by raising ``TypeError``, and ``sig`` is the
    # attacker-chosen header: any local process can open the loopback socket this
    # verifier guards, and aiohttp decodes a header byte that is not valid UTF-8
    # into a lone surrogate. Encoding first gives every possible header value a
    # verdict instead of an unhandled ``TypeError`` that drops the connection and
    # skips the caller's SEL ``proxy_auth_failed`` record. ``surrogatepass``
    # because a lone surrogate must still compare rather than raise on the way
    # in, and it keeps two distinct strings distinct. ``expected`` is a hexdigest
    # by construction, so a signature that matched before still matches.
    return hmac.compare_digest(
        expected.encode("utf-8", "surrogatepass"), sig.encode("utf-8", "surrogatepass")
    )


@dataclass(frozen=True)
class ProxyPrincipalClaim:
    """One verified principal classification, bound to one proxied request."""

    kind: str
    owner_id: str
    issued_at: int
    request_id: str
    version: int = 1


class ProxyPrincipalReplayCache:
    """Remember each accepted request id until its freshness window closes.

    Hold ONE cache for the life of the backend process. A cache built per request
    remembers nothing, so it would accept a replayed claim. When ``max_entries``
    live ids are held, the cache refuses a new id instead of evicting one, because
    evicting a live id would make that request replayable again.
    """

    def __init__(self, *, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, request_id: str, *, expires_at: float, now: float) -> bool:
        """Record a fresh *request_id*; refuse a replay or a full cache."""
        with self._lock:
            expired = [key for key, expiry in self._seen.items() if expiry < now]
            for key in expired:
                del self._seen[key]
            if request_id in self._seen or len(self._seen) >= self._max_entries:
                return False
            self._seen[request_id] = expires_at
            return True


def _claim_shape_valid(
    *, kind: Any, owner_id: Any, issued_at: Any, request_id: Any, version: Any
) -> bool:
    """Whether the claim fields have the one shape the gateway ever signs."""
    # The claim is attacker-chosen until the HMAC check, so a list or dict ``kind``
    # must be a ``False`` verdict here, not a ``TypeError`` from the set lookup.
    if version != 1 or not isinstance(kind, str) or kind not in _PRINCIPAL_KINDS:
        return False
    # ``bool`` is an ``int`` subclass, and ``true`` is not an issue time.
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        return False
    if not isinstance(owner_id, str) or len(owner_id) > _MAX_OWNER_ID_LENGTH:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in owner_id):
        return False
    # Only an owner session names an owner. Every other kind carries an empty id,
    # so a backend can never read an identity off a claim that does not vouch for one.
    if (kind == PRINCIPAL_OWNER_SESSION) != bool(owner_id):
        return False
    if not isinstance(request_id, str) or len(request_id) != _REQUEST_ID_HEX_LENGTH:
        return False
    return all(char in "0123456789abcdef" for char in request_id)


def _encode_claim(claim: ProxyPrincipalClaim) -> str:
    payload = {
        "version": claim.version,
        "kind": claim.kind,
        "ownerId": claim.owner_id,
        "issuedAt": claim.issued_at,
        "requestId": claim.request_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_claim(encoded: str) -> ProxyPrincipalClaim | None:
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        # ``binascii.Error`` and ``json.JSONDecodeError`` are both ``ValueError``.
        return None
    if not isinstance(payload, dict) or set(payload) != _PRINCIPAL_KEYS:
        return None
    if not _claim_shape_valid(
        kind=payload["kind"],
        owner_id=payload["ownerId"],
        issued_at=payload["issuedAt"],
        request_id=payload["requestId"],
        version=payload["version"],
    ):
        return None
    return ProxyPrincipalClaim(
        kind=payload["kind"],
        owner_id=payload["ownerId"],
        issued_at=payload["issuedAt"],
        request_id=payload["requestId"],
        version=payload["version"],
    )


def _principal_signature(
    key: str, encoded_claim: str, issued_at: int, *, method: str, target: str, body: bytes
) -> str:
    """The principal HMAC: a domain tag, then each bound field on its own line."""
    message = "\n".join(
        (
            _PRINCIPAL_SIGNATURE_DOMAIN,
            str(issued_at),
            method,
            target,
            hashlib.sha256(body or b"").hexdigest(),
            encoded_claim,
        )
    )
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_proxy_principal_claim(
    *,
    kind: str,
    owner_id: str,
    method: str,
    target: str,
    body: bytes,
    secret: str,
    now: float | None = None,
    request_id: str | None = None,
) -> str:
    """Build the ``X-KiroCrew-Principal`` value for one proxied request.

    Raises ``ValueError`` for an empty secret or a claim outside the one valid
    shape, so the gateway refuses the request rather than forwarding an unsigned
    or ambiguous principal.
    """
    claim = ProxyPrincipalClaim(
        kind=kind,
        owner_id=owner_id,
        issued_at=int(time.time() if now is None else now),
        request_id=request_id or secrets.token_hex(_REQUEST_ID_HEX_LENGTH // 2),
    )
    if not secret or not _claim_shape_valid(
        kind=claim.kind,
        owner_id=claim.owner_id,
        issued_at=claim.issued_at,
        request_id=claim.request_id,
        version=claim.version,
    ):
        raise ValueError("invalid proxy principal claim")
    encoded = _encode_claim(claim)
    signature = _principal_signature(
        secret, encoded, claim.issued_at, method=method, target=target, body=body
    )
    return f"{encoded}.{signature}"


def verify_proxy_principal_claim(
    header_value: str,
    *,
    method: str,
    target: str,
    body: bytes,
    replay_cache: ProxyPrincipalReplayCache,
    secret: str | None = None,
    now: float | None = None,
) -> ProxyPrincipalClaim | None:
    """Verify and consume one ``X-KiroCrew-Principal`` value.

    Returns the claim, or ``None`` for a missing secret or cache, an oversized or
    malformed header, a claim outside the one valid shape, an issue time outside
    ±60s, a signature mismatch, a request id already accepted, or a full cache.
    The replay check runs last, so only a claim that passed every other check
    takes a cache entry.
    """
    key = proxy_secret() if secret is None else secret
    if not key or replay_cache is None or not header_value or "." not in header_value:
        return None
    if len(header_value.encode("utf-8", "surrogatepass")) > _MAX_PRINCIPAL_HEADER_BYTES:
        return None
    encoded, _, signature = header_value.rpartition(".")
    if not encoded or not signature:
        return None
    claim = _decode_claim(encoded)
    if claim is None:
        return None
    clock = time.time() if now is None else now
    # Python compares an int with a float exactly, so this chained form returns a
    # verdict for an ``issuedAt`` past float range. Subtracting such a value from
    # the float clock would raise ``OverflowError`` before the HMAC check.
    if not clock - _MAX_SKEW_SECONDS <= claim.issued_at <= clock + _MAX_SKEW_SECONDS:
        return None
    expected = _principal_signature(
        key, encoded, claim.issued_at, method=method, target=target, body=body
    )
    # Compared as bytes for the reason ``verify_proxy_request`` gives above: the
    # signature is attacker-chosen header text, and a non-ASCII ``str`` would make
    # ``compare_digest`` raise instead of returning a verdict.
    if not hmac.compare_digest(
        expected.encode("utf-8", "surrogatepass"), signature.encode("utf-8", "surrogatepass")
    ):
        return None
    if not replay_cache.accept(
        claim.request_id, expires_at=claim.issued_at + _MAX_SKEW_SECONDS, now=clock
    ):
        return None
    return claim
