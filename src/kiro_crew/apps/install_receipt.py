"""Anonymous receipts for successful official-catalog app installs.

Receipts reuse the beacon endpoint and effective consent gate. The wire format is
fixed: official app slug in the path plus a per-app HMAC token, install kind,
and clamped KiroCrew release in the query. Custom registries and non-registry
install paths never call this sender. It deliberately does not use the metrics
schema's ``redact()`` pass: that pass would replace the HMAC token, while this
module's closed field set contains no caller-supplied free-form value.
"""

from __future__ import annotations

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
import urllib.error
import urllib.parse
import urllib.request

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


def receipt_secret(*, create: bool = True) -> str:
    """Return the receipt-only local secret, generating it on first use.

    Mirrors ``beacon.install_id``'s atomic create (owner-only temp file +
    ``os.link``) so two processes racing the first receipt converge on ONE
    secret. Every failure returns ``""`` and the caller skips the receipt — a
    receipt is best-effort and must never fail an install or force state into
    existence. With ``create=False`` the file is only read, never generated.
    """
    try:
        path = beacon.config_dir() / _SECRET_FILE
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

    query = urllib.parse.urlencode(
        {"t": token, "k": kind, "v": beacon.release(app_version)}
    )
    return (
        f"{endpoint.rstrip('/')}/b/{beacon.BEACON_SCHEMA}/install/"
        f"{app_slug}?{query}"
    )


def should_send(*, enabled: bool, official: bool) -> tuple[bool, str]:
    """Apply the provenance gate before the beacon's shared consent ladder."""
    if not official:
        return False, "not an official catalog entry"
    return beacon.telemetry_permitted(
        enabled=enabled,
        audit_tool="install_receipt_send",
    )


def send(
    endpoint: str,
    app_slug: str,
    *,
    app_version: str,
    enabled: bool,
    official: bool,
    kind: str,
) -> bool:
    """Best-effort GET. Every refusal and transport failure returns ``False``."""
    if not endpoint:
        return False
    try:
        permitted, _reason = should_send(enabled=enabled, official=official)
        if not permitted:
            return False
        secret = receipt_secret()
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


def _send_configured(app_slug: str, *, official: bool, kind: str) -> None:
    """Load current config and send inside the worker thread."""
    try:
        config = KiroCrewConfig.load()
        send(
            config.telemetry.beacon_endpoint,
            app_slug,
            app_version=__version__,
            enabled=config.telemetry.beacon_enabled,
            official=official,
            kind=kind,
        )
    except Exception:
        logger.debug("install receipt worker failed (ignored)", exc_info=True)


def dispatch(app_slug: str, *, official: bool, kind: str) -> None:
    """Start a detached daemon sender without delaying the completed install."""
    if not official:
        return
    with contextlib.suppress(Exception):
        threading.Thread(
            target=_send_configured,
            args=(app_slug,),
            kwargs={"official": True, "kind": kind},
            name="kirocrew-install-receipt",
            daemon=True,
        ).start()
