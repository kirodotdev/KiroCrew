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

import base64
import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
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
from kiro_crew.secrets_mediation.ssrf import PinnedTarget, check_url

#: Methods a mediated request may use.
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})

#: Bounds. A credential-bearing request is short and bounded by construction.
_DEFAULT_TIMEOUT_S = 20.0
_MAX_TIMEOUT_S = 60.0
_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 1_000_000  # 1 MB
_MAX_REQUEST_BODY_BYTES = 256_000

#: Response headers safe to echo back. Anything else (including any the origin
#: reflects) is dropped so a reflected credential cannot ride the response out.
_SAFE_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-length", "date", "etag", "last-modified", "cache-control"}
)

#: Response content types we return a body for. Others return metadata only.
_ALLOWED_CONTENT_PREFIXES = ("application/json", "text/", "application/xml", "application/x-ndjson")


class MediationError(Exception):
    """A mediated request failed. The message is safe (never the secret)."""


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
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().init_poolmanager(*args, **kwargs)
        pinned_ip = self._pinned_ip

        class _PinnedHTTPSConnection(HTTPSConnection):
            def _new_conn(self):  # type: ignore[no-untyped-def]
                # Same call urllib3 2.x makes, but to the validated IP instead of
                # ``self._dns_host``. TLS still uses ``self.host`` for SNI and cert
                # verification, so pinning changes only the socket destination.
                return urllib3_connection.create_connection(
                    (pinned_ip, self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )

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


def _secret_reflections(secret_value: str) -> list[str]:
    """The plaintext secret plus the common reversible encodings an upstream might
    echo it in, so an exact-substring scrub catches the frequent cases (base64,
    hex, percent-encoding) rather than only the raw value.

    This is defense-in-depth, not a completeness claim: an arbitrary transform
    (a hash, a per-character split, a bespoke encoding) cannot be enumerated, so
    the caller also bounds the body by content-type and size, and the tool
    surface documents that a mediated response is only as trustworthy as the
    owner-authorized origin.
    """
    variants: set[str] = set()
    raw = secret_value.encode("utf-8", errors="ignore")
    derived: set[str] = set()
    if raw:
        derived.add(base64.b64encode(raw).decode("ascii"))
        derived.add(base64.b64encode(raw).decode("ascii").rstrip("="))
        derived.add(base64.urlsafe_b64encode(raw).decode("ascii"))
        derived.add(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
        derived.add(raw.hex())
        derived.add(urllib.parse.quote(secret_value, safe=""))
        # JSON-escaped form: an echo endpoint that returns the credential inside a
        # JSON string escapes ``"`` and ``\`` (and control chars), so the raw scrub
        # would miss ``a\"b`` reflected as ``a\\\"b``. ``json.dumps`` minus its outer
        # quotes is exactly the escaped inner string the body would contain.
        json_escaped = json.dumps(secret_value)[1:-1]
        if json_escaped != secret_value:
            derived.add(json_escaped)
    # The RAW secret is ALWAYS scrubbed, however short — a 4-char key reflected
    # verbatim must not slip through. The length floor applies only to DERIVED
    # encodings, where a short fragment could otherwise over-redact ordinary body
    # text (e.g. a 4-char base64 chunk colliding with unrelated content).
    variants.add(secret_value)
    variants.update(v for v in derived if len(v) >= 8)
    return [v for v in variants if v]


def _sanitize_response(
    resp: requests.Response, final_origin: str, secret_value: str
) -> SanitizedResponse:
    headers = {k: v for k, v in resp.headers.items() if k.lower() in _SAFE_RESPONSE_HEADERS}
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    body = ""
    truncated = False
    if any(ctype.startswith(p) for p in _ALLOWED_CONTENT_PREFIXES):
        raw = resp.content[: _MAX_RESPONSE_BYTES + 1]
        if len(raw) > _MAX_RESPONSE_BYTES:
            raw = raw[:_MAX_RESPONSE_BYTES]
            truncated = True
        try:
            body = raw.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, TypeError):
            body = raw.decode("utf-8", errors="replace")
    else:
        body = f"[response body omitted: unsupported content-type {ctype or 'unknown'!r}]"

    # Defense against a REFLECTED credential: an upstream that echoes the auth
    # header (some debug/echo endpoints, some error bodies) would otherwise carry
    # the plaintext straight back to the agent through this sanitized result. The
    # secret never legitimately appears in a response, so scrub the plaintext AND
    # its common reversible encodings from both the body and the returned header
    # values. This cannot catch every possible transform (a hash, a split, a
    # bespoke encoding), so the response body is also held to the safe
    # content-type + size limits above and the tool description warns that a
    # response is only as trustworthy as the owner-authorized origin.
    if secret_value:
        redaction = "[redacted-secret]"
        for variant in _secret_reflections(secret_value):
            body = body.replace(variant, redaction)
            headers = {k: v.replace(variant, redaction) for k, v in headers.items()}
    return SanitizedResponse(
        status=resp.status_code,
        headers=headers,
        body=body,
        truncated=truncated,
        final_url_origin=final_origin,
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
    timeout = min(max(float(req.timeout_s or _DEFAULT_TIMEOUT_S), 0.1), _MAX_TIMEOUT_S)

    # 1. Owner authorization (fail closed before any vault read / network).
    auth = load_authorization(config_dir, req.secret_name)

    # 2. Request origin must equal the authorized origin EXACTLY.
    request_origin = normalize_origin(req.url)
    if request_origin != auth.origin:
        raise PolicyError(
            f"Secret {req.secret_name!r} is authorized only for {auth.origin}, not "
            f"{request_origin}. The request was refused before the secret was read."
        )

    # 3. SSRF-validate + DNS-pin.
    target = check_url(req.url)

    # Caller-supplied headers must not collide with the injection slot or set
    # framing/host. Drop any that would.
    safe_caller_headers = _filter_caller_headers(req.headers, auth.placement)

    if req.json_body is not None:
        encoded = json.dumps(req.json_body).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BODY_BYTES:
            raise MediationError("Request body is too large.")

    # 4. Resolve the secret — only now, only in memory.
    vault = SecretVault(config_dir)
    secret = vault.get(req.secret_name)
    if secret is None:
        raise PolicyError(
            f"Secret {req.secret_name!r} is authorized but not stored in the vault. "
            f"The owner must add it to the vault before it can be used from chat."
        )
    secret_value = secret.reveal()

    try:
        return _dispatch_with_redirects(
            method=method,
            url=req.url,
            query=dict(req.query or {}),
            headers=safe_caller_headers,
            json_body=req.json_body,
            placement=auth.placement,
            secret_value=secret_value,
            authorized_origin=auth.origin,
            target=target,
            timeout=timeout,
        )
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
    timeout: float,
) -> SanitizedResponse:
    current_url = url
    current_target = target
    current_method = method
    current_json = json_body
    for _hop in range(_MAX_REDIRECTS + 1):
        send_headers = dict(headers)
        _inject(placement, secret_value, send_headers)

        session = requests.Session()
        # No environment proxies/netrc; we control the destination fully.
        session.trust_env = False
        adapter = _PinnedHTTPSAdapter(current_target.ip, max_retries=0)
        session.mount("https://", adapter)
        try:
            resp = session.request(
                method=current_method,
                url=current_url,
                params=query or None,
                headers=send_headers,
                json=current_json,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    location = requests.compat.urljoin(current_url, resp.headers["location"])
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
                    current_target = check_url(location)  # re-SSRF-pin the new hop
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
                # Enforce the response size cap while streaming.
                _read_capped(resp)
                return _sanitize_response(resp, authorized_origin, secret_value)
            finally:
                resp.close()
        except requests.RequestException as exc:
            raise MediationError(f"The mediated request failed: {type(exc).__name__}.") from exc
        finally:
            session.close()
    raise MediationError("The mediated request exceeded the redirect limit.")


def _read_capped(resp: requests.Response) -> None:
    """Force-read the body up to the cap so ``resp.content`` is bounded."""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            break
    resp._content = b"".join(chunks)[: _MAX_RESPONSE_BYTES + 1]  # type: ignore[attr-defined]
