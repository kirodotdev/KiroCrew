"""The loopback health probe every verdict about a backend is read from.

Adoption, the startup poll and the standing watch all ask this one probe, so the
URL an app-authored ``healthCheck`` may name, the redirect/proxy refusal of
``loopback_urlopen``, and the way a failure is classified cannot differ between them.
"""

from __future__ import annotations

import http.client
import logging
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.loopback_http import loopback_urlopen

logger = logging.getLogger(_FACADE)


_HEALTH_CHECK_TIMEOUT = 5
# How much of a failed probe's exception message reaches the log. Part of it is the app
# backend's own bytes (see _probe_failure_detail), and a status line may be 64 KB long.
_PROBE_DETAIL_MAX_CHARS = 120
# ``healthCheck`` is app-authored text. A leading slash terminates the URL authority;
# the remaining class is the ordinary RFC 3986 path/query set with characters that
# can be parsed inconsistently (userinfo, fragments, backslashes, brackets) excluded.
_HEALTH_PATH_RE = re.compile(r"\A/[A-Za-z0-9._~!$&'()*+,;=:/?%-]*\Z")
_warned_health_paths: set[str] = set()
_health_warn_lock = threading.Lock()


def _warn_bad_health_path(health_path: str) -> None:
    """Log a rejected manifest health path once per distinct value."""

    with _health_warn_lock:
        if health_path in _warned_health_paths:
            return
        _warned_health_paths.add(health_path)
    logger.warning(
        "App backend health path %r is unsafe; healthCheck must be an absolute "
        "loopback path such as /health",
        health_path,
    )


def _health_probe_url(port: int, health_path: str) -> str | None:
    """Build a loopback health URL without letting app text change its authority."""

    if not 0 < port < 65536:
        return None
    if not _HEALTH_PATH_RE.fullmatch(health_path or ""):
        _warn_bad_health_path(health_path)
        return None
    return f"http://127.0.0.1:{port}{health_path}"


@dataclass(frozen=True)
class HealthProbeOutcome:
    """What one unsigned loopback health GET observed.

    Carries the observed HTTP status so a failure can be READ rather than investigated:
    a 403 (the health path sits behind the app's own auth), a 404 (no handler) and a dead
    port are the same single log line otherwise. ``status`` is None when no HTTP response
    was produced at all, and ``detail`` is the short phrase the logs print.

    ``healthy`` is recorded by the probe rather than derived from ``status``, because the
    two ways a status arrives do NOT share a verdict: the opener REFUSES redirects, so a
    3xx reaches us as an ``HTTPError`` whose code is below 400 while nothing served the
    health check. Deriving the verdict from the number would promote exactly that.
    """

    status: int | None
    detail: str
    healthy: bool = False

    @classmethod
    def answered(cls, status: int) -> HealthProbeOutcome:
        """A status on a response the opener RETURNED — the unchanged verdict, < 400."""
        return cls(status, f"HTTP {status}", healthy=status < 400)

    @classmethod
    def refused(cls, status: int) -> HealthProbeOutcome:
        """A status the opener raised: a 4xx/5xx, or a redirect it would not follow."""
        return cls(status, f"HTTP {status}")


def _probe_failure_detail(exc: BaseException) -> str:
    """Name why an unsigned loopback GET produced no status, in one short phrase.

    Unwraps ``URLError``, whose ``reason`` is the interesting exception; the wrapper's own
    ``str`` buries it in ``<urlopen error ...>``.

    The fallback renders the message with ``repr`` and a length cap because an app backend
    is arbitrary third-party code and part of that message is ITS bytes: a ``BadStatusLine``
    carries the raw first line off the socket, up to 64 KB of it, which is free to hold a
    carriage return or a terminal escape. Printed as-is into the gateway log, those bytes
    could add a line the gateway never wrote. ``repr`` escapes them and the cap keeps one
    failed probe from writing a screenful.
    """
    inner = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        inner = exc.reason
    if isinstance(inner, ConnectionRefusedError):
        return "connection refused"
    if isinstance(inner, TimeoutError):  # socket.timeout is an alias for it
        return "timed out"
    message = str(inner)
    if len(message) > _PROBE_DETAIL_MAX_CHARS:
        message = message[:_PROBE_DETAIL_MAX_CHARS] + "..."
    return f"{type(inner).__name__}: {message!r}"


def _health_probe(
    port: int,
    health_path: str,
    *,
    timeout: float = _HEALTH_CHECK_TIMEOUT,
) -> HealthProbeOutcome:
    """What the validated loopback health endpoint answered. Never raises.

    `http.client.HTTPException` is caught alongside the socket errors because it is NOT
    an `OSError` or `URLError` subclass (only `RemoteDisconnected` is, via
    `ConnectionResetError`), and `urllib`'s `do_open` re-raises `getresponse()` failures
    unwrapped. An app backend is arbitrary third-party code — an `exec` backend, or an
    adopted process we do not own — so a non-HTTP first line on the port is a real
    condition, and `BadStatusLine` escaping here would kill the standing watch thread
    and freeze `healthy` at its last value: silently reinstating the write-once bug this
    module's watch exists to prevent.
    """
    url = _health_probe_url(port, health_path)
    if url is None:
        return HealthProbeOutcome(None, "unsafe healthCheck path")
    try:
        req = urllib.request.Request(url, method="GET")
        with loopback_urlopen(req, timeout=timeout) as resp:
            return HealthProbeOutcome.answered(int(resp.status))
    except urllib.error.HTTPError as exc:
        # `urlopen` RAISES instead of returning the response for every status the probe
        # must call unhealthy — a 403 from an auth-gated health path, a 404 from a missing
        # handler, and a 3xx the loopback opener will not follow — so the status an
        # operator needs arrives HERE, never above. Caught before URLError, its base class.
        with exc:  # the error IS the response; closing it releases the socket
            return HealthProbeOutcome.refused(int(exc.code))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        return HealthProbeOutcome(None, _probe_failure_detail(exc))


def _health_failure_hint(outcome: HealthProbeOutcome) -> str:
    """The misconfigurations a status alone does not explain, or "".

    Both are cases where the backend is up and answering, so the number on its own reads
    like a working service. 401/403: the health path most likely sits behind auth the probe
    cannot satisfy, because the probe is deliberately UNSIGNED — though the backend is free
    to refuse for a reason of its own, so this one names the likely cause rather than
    asserting it. 3xx: the loopback opener refuses redirects, so a status a reader would
    call success never reached a handler.
    """
    if outcome.status in (401, 403):
        return (
            " — usually the healthCheck path is behind auth the unsigned probe cannot "
            "satisfy; point backend.healthCheck at an unauthenticated route"
        )
    if outcome.status is not None and 300 <= outcome.status < 400:
        return (
            " — the probe does not follow redirects; point backend.healthCheck at the "
            "route that answers directly"
        )
    return ""
