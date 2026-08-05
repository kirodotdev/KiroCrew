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
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

_ENV_KEY = "KIROCREW_PROXY_SECRET"
_MAX_SKEW_SECONDS = 60


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
    stale (±60s) timestamp, or signature mismatch all return ``False``.
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
    return hmac.compare_digest(expected, sig)
