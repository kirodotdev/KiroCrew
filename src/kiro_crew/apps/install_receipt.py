"""Anonymous receipts for successful official-catalog app installs.

Receipts reuse the beacon endpoint and effective consent gate. The wire format is
fixed: official app slug in the path plus a per-app HMAC token, install kind,
and clamped KiroCrew release in the query. Custom registries and non-registry
install paths never call this sender. It deliberately does not use the metrics
schema's ``redact()`` pass: that pass would replace the HMAC token, while this
module's closed field set contains no caller-supplied free-form value.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import http.client
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew import __version__, beacon, platform_compat
from kiro_crew.apps.manifest import KEBAB_RE
from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

KIND_FRESH = "fresh"
KIND_UPDATE = "update"
_KINDS = frozenset({KIND_FRESH, KIND_UPDATE})
_TOKEN_HEX_CHARS = 32
_TOKEN_MESSAGE_PREFIX = b"app-install:"

# Receipt-only HMAC key. Deliberately NOT the beacon install id: that id is
# transmitted in every heartbeat, so the collector holding it could recompute
# the token for every public slug and link one installation's receipts across
# apps. This secret keys receipt tokens and nothing else, and never leaves the
# machine — with it unknown, tokens for different apps are unlinkable and no
# receipt can be tied back to a heartbeat.
_SECRET_FILE = "app_receipt_secret"
_SECRET_RE = re.compile(r"[0-9a-f]{64}")


def receipt_secret(*, create: bool = True, config_dir: Path | None = None) -> str:
    """Return the receipt-only local secret, generating it on first use.

    Mirrors ``beacon.install_id``'s atomic create (owner-only temp file +
    ``os.link``) so two processes racing the first receipt converge on ONE
    secret. Every failure returns ``""`` and the caller skips the receipt — a
    receipt is best-effort and must never fail an install or force state into
    existence. With ``create=False`` the file is only read, never generated.

    ``config_dir`` lets a caller pass an already-resolved directory instead of
    letting this function call ``beacon.config_dir()`` itself. This matters
    when the call happens on a background thread: ``beacon.config_dir()``
    honors ``KIROCREW_HOME`` at call time, so resolving it lazily on the
    worker thread risks reading a different (e.g. unpatched, real-home) value
    than what the dispatching thread saw. Defaults to ``beacon.config_dir()``
    for callers that are already on a safe thread (e.g. direct callers, tests).
    """
    try:
        path = (config_dir if config_dir is not None else beacon.config_dir()) / _SECRET_FILE
        if path.exists():
            existing = beacon._read_state(path)
            if _SECRET_RE.fullmatch(existing):
                return existing
            # Corrupt/truncated — remove before regenerating so a malformed
            # key never degrades every token derived from it.
            path.unlink(missing_ok=True)
        if not create:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = secrets.token_hex(32)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
        try:
            os.write(tmp_fd, fresh.encode("ascii"))
            os.close(tmp_fd)
            tmp_fd = -1
            with contextlib.suppress(OSError):
                platform_compat.restrict_to_owner(tmp_path)
            os.link(tmp_path, str(path))
            return fresh
        except FileExistsError:
            # Lost the race — adopt the winner's secret.
            existing = beacon._read_state(path)
            return existing if _SECRET_RE.fullmatch(existing) else ""
        finally:
            if tmp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    except (OSError, RuntimeError, KeyError):
        return ""


def _valid_slug(app_slug: str) -> bool:
    return len(app_slug) <= 64 and bool(KEBAB_RE.fullmatch(app_slug))


def receipt_token(secret: str, app_slug: str) -> str:
    """Derive the per-(installation, app) token, or return an empty string.

    Domain-separated HMAC keyed by the receipt-only local secret: the server
    can deduplicate one app's installs, but — never having seen the key — it
    cannot recompute tokens for other slugs, link one installation's apps
    together, or connect any receipt to the heartbeat's install id.
    """
    if not _SECRET_RE.fullmatch(secret) or not _valid_slug(app_slug):
        return ""
    digest = hmac.new(
        secret.encode("ascii"),
        _TOKEN_MESSAGE_PREFIX + app_slug.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:_TOKEN_HEX_CHARS]


def receipt_url(
    endpoint: str,
    app_slug: str,
    *,
    token: str,
    kind: str,
    app_version: str,
) -> str:
    """Build the exact versioned receipt route or raise ``ValueError``."""
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme != "https":
        raise ValueError("receipt endpoint must be https://")
    if not _valid_slug(app_slug):
        raise ValueError("invalid app slug")
    if len(token) != _TOKEN_HEX_CHARS or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid receipt token")
    if kind not in _KINDS:
        raise ValueError("invalid install kind")

    query = urllib.parse.urlencode({"t": token, "k": kind, "v": beacon.release(app_version)})
    return f"{endpoint.rstrip('/')}/b/{beacon.BEACON_SCHEMA}/install/" f"{app_slug}?{query}"


def should_send(*, enabled: bool, official: bool, acked: bool) -> beacon.Verdict:
    """Apply the provenance gate before the beacon's shared consent ladder."""
    if not official:
        return beacon.Verdict(False, "not an official catalog entry", "unofficial")
    return beacon.telemetry_permitted(
        enabled=enabled,
        acked=acked,
        audit_tool="install_receipt_send",
    )


def send(
    endpoint: str,
    app_slug: str,
    *,
    app_version: str,
    enabled: bool,
    official: bool,
    acked: bool,
    kind: str,
    config_dir: Path | None = None,
) -> bool:
    """Best-effort GET. Every refusal and transport failure returns ``False``.

    ``config_dir``: forwarded to :func:`receipt_secret` unchanged — see that
    function's docstring for why a background-thread caller must pass an
    already-resolved directory rather than let it resolve lazily.
    """
    if not endpoint:
        return False
    try:
        if not should_send(enabled=enabled, official=official, acked=acked).ok:
            return False
        secret = receipt_secret(config_dir=config_dir)
        token = receipt_token(secret, app_slug)
        if not token:
            return False
        url = receipt_url(
            endpoint,
            app_slug,
            token=token,
            kind=kind,
            app_version=app_version,
        )
        request = urllib.request.Request(url, method="GET")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- receipt_url enforces HTTPS and a fixed route/query allowlist
        with urllib.request.urlopen(request, timeout=beacon.HTTP_TIMEOUT_SECS):
            pass
        return True
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        RuntimeError,
        TimeoutError,
        UnicodeError,
        ValueError,
    ) as exc:
        logger.debug("install receipt failed (ignored): %s", exc)
        return False


def _send_configured(
    app_slug: str,
    *,
    official: bool,
    kind: str,
    config_dir: Path,
    endpoint: str,
    enabled: bool,
    acked: bool,
) -> None:
    """Send inside the worker thread, from values the caller already resolved.

    Every input that depends on the process environment -- ``config_dir`` and
    the three config fields ``send`` reads -- is resolved by ``dispatch()`` on
    the dispatching thread and passed in. ``beacon.config_dir()`` and
    ``KiroCrewConfig.load()`` both honor ``KIROCREW_HOME`` at the moment they
    are called; resolving either on this worker thread would race a test's
    teardown unpatching ``KIROCREW_HOME`` before this thread gets scheduled,
    and send the receipt secret to the real data home instead of the pinned
    test one. The worker therefore reads nothing from the environment.
    """
    try:
        send(
            endpoint,
            app_slug,
            app_version=__version__,
            enabled=enabled,
            official=official,
            acked=acked,
            kind=kind,
            config_dir=config_dir,
        )
    except Exception:
        logger.debug("install receipt worker failed (ignored)", exc_info=True)
    finally:
        with _pending_lock:
            _pending_threads.discard(threading.current_thread())


# Tracks in-flight dispatch() worker threads so tests can block until they
# finish (see wait_for_pending_receipt_writes) instead of leaving one alive
# past teardown, where it would resolve paths against whatever KIROCREW_HOME
# happens to be set to next.
_pending_lock = threading.Lock()
_pending_threads: set[threading.Thread] = set()

# wait_for_pending_receipt_writes() waits at most this long for dispatch()'s
# worker threads to finish before giving up. Matches the bounded-poll style
# used by kiro_crew.metrics.provider._wait_for_in_flight_consent_worker.
_PENDING_WAIT_BOUND_SECS = 10.0
_PENDING_WAIT_POLL_SECS = 0.01


def wait_for_pending_receipt_writes(timeout: float = _PENDING_WAIT_BOUND_SECS) -> None:
    """Block until every ``dispatch()`` worker thread has finished, or raise.

    Test-only: call this before undoing a ``KIROCREW_HOME`` monkeypatch (or
    any other per-test env override) so no receipt worker is still alive to
    resolve paths against a different value once the patch is gone. Raises
    ``RuntimeError`` if a worker is still running past ``timeout`` — a test
    must never proceed with a live stale worker able to touch real paths.
    """
    deadline = time.monotonic() + timeout
    while True:
        with _pending_lock:
            live = {t for t in _pending_threads if isinstance(t, threading.Thread) and t.is_alive()}
            _pending_threads.intersection_update(live)
            if not live:
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "wait_for_pending_receipt_writes: an install-receipt worker "
                f"is still running after {timeout}s; a test must never "
                "proceed with a live stale worker able to resolve paths "
                "against an unpatched KIROCREW_HOME"
            )
        time.sleep(_PENDING_WAIT_POLL_SECS)


def dispatch(
    app_slug: str,
    *,
    official: bool,
    kind: str,
    config: KiroCrewConfig | None = None,
) -> None:
    """Start a detached daemon sender without delaying the completed install.

    Synchronous: the config read happens on the CALLING thread when ``config``
    is not supplied, so this form is for callers that are already off the event
    loop. A coroutine uses :func:`dispatch_async`, which does that read in a
    worker thread and then hands the resolved config in here.
    """
    if not official:
        return
    # Resolved HERE, on the caller's thread, while KIROCREW_HOME (or whatever
    # pinned it) is still in effect — then handed to the worker as arguments.
    # The worker must not call beacon.config_dir() or KiroCrewConfig.load()
    # itself: by the time it runs, a test may already have undone the very env
    # override that made those resolve to the pinned test home.
    with contextlib.suppress(Exception):
        config_dir = beacon.config_dir()
        if config is None:
            config = KiroCrewConfig.load()
        thread = threading.Thread(
            target=_send_configured,
            args=(app_slug,),
            kwargs={
                "official": True,
                "kind": kind,
                "config_dir": config_dir,
                "endpoint": config.telemetry.beacon_endpoint,
                "enabled": config.telemetry.beacon_enabled,
                "acked": config.dashboard.privacy_acked,
            },
            name="kirocrew-install-receipt",
            daemon=True,
        )
        with _pending_lock:
            _pending_threads.add(thread)
        try:
            thread.start()
        except Exception:
            with _pending_lock:
                _pending_threads.discard(thread)
            raise


async def dispatch_async(app_slug: str, *, official: bool, kind: str) -> None:
    """:func:`dispatch` for the event loop.

    ``KiroCrewConfig.load()`` is a stat pass, a read, a JSON parse and a full
    schema validation — file I/O the loop must not do (``install_from_registry``
    reaches here at the end of every official install). It runs in a worker
    thread instead, and because this coroutine is AWAITED while it does, the
    environment that pins the data home is still in effect when it reads: the
    leak this module guards against was a detached thread outliving its caller's
    env, not a thread as such. The resolved config is then handed to
    :func:`dispatch`, which does no further I/O on this thread.
    """
    if not official:
        return
    try:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    except Exception:
        return
    dispatch(app_slug, official=True, kind=kind, config=config)
