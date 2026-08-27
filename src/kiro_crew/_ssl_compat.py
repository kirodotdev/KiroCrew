"""Make platform trust available before any HTTPS client caches an SSL context.

macOS trust is policy-aware and cannot be represented faithfully as a static
PEM bundle: the Keychain carries user/admin/system trust, explicit distrust,
hostname policy, and validity decisions. Applications must therefore ask
Security.framework to evaluate each connection. Other platforms keep the
file-based bootstrap used for Linux distributions whose Python default points
at the wrong CA location.
"""

from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path

try:  # macOS-only dependency (setup.cfg marker); absent on other platforms.
    import truststore
except ImportError:
    truststore = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_CA_CANDIDATES = (
    "/etc/pki/tls/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
)
_TRUSTSTORE_INJECTED = False


def _inject_macos_system_trust() -> bool:
    """Install Security.framework-backed SSL contexts once per process.

    ``truststore`` evaluates the real server chain through SecTrust instead of
    flattening every certificate stored in a Keychain into an unconditional
    OpenSSL trust anchor.  Return ``False`` on failure so startup can retain the
    prior file-based behavior rather than losing all HTTPS capability.
    """
    global _TRUSTSTORE_INJECTED

    if _TRUSTSTORE_INJECTED:
        return True

    if truststore is None:
        logger.warning("truststore is not installed; falling back to file-based CA discovery")
        return False

    try:
        truststore.inject_into_ssl()
    except Exception as exc:
        # A log line rather than warnings.warn: under a strict warnings filter
        # a warning becomes an exception inside the startup prelude, turning
        # the fallback this function promises into a startup crash.
        logger.warning(
            "Could not enable the macOS system trust store; falling back to "
            "file-based CA discovery: %s",
            exc,
        )
        return False

    _TRUSTSTORE_INJECTED = True
    return True


def _ssl_context_has_ca_trust(context: ssl.SSLContext) -> bool:
    """Return whether *context* has a usable CA trust source.

    OpenSSL contexts expose a concrete CA count. Security.framework-backed
    truststore contexts evaluate anchors dynamically and intentionally cannot
    enumerate that count, so a successful process-level injection is their
    equivalent trust-source signal.
    """
    try:
        return context.cert_store_stats()["x509_ca"] > 0
    except NotImplementedError:
        return _TRUSTSTORE_INJECTED


def _ensure_ssl_certs() -> None:
    """Configure platform trust before any HTTPS library caches its context.

    An explicit ``SSL_CERT_FILE`` remains the highest-precedence escape hatch.
    macOS additionally installs Security.framework evaluation for this
    process's own clients, but ``inject_into_ssl()`` is process-local, so the
    file-based discovery below still runs to export ``SSL_CERT_FILE`` /
    ``REQUESTS_CA_BUNDLE`` for child processes (kiro-cli, Node MCP servers)
    that inherit this environment and cannot inherit a monkey-patch.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return

    if sys.platform == "darwin":
        _inject_macos_system_trust()

    defaults = ssl.get_default_verify_paths()
    if defaults.cafile and Path(defaults.cafile).exists():
        return

    for candidate in _CA_CANDIDATES:
        if Path(candidate).exists():
            os.environ["SSL_CERT_FILE"] = candidate
            os.environ.setdefault("REQUESTS_CA_BUNDLE", candidate)
            return

    # The file-based fallback keeps standalone/source installations usable when
    # the OS-specific bootstrap is unavailable.  requests makes certifi part of
    # Kiro Crew's installed dependency closure, including the desktop bundle.
    try:
        import certifi

        bundle = certifi.where()
    except ImportError:
        return
    if Path(bundle).exists():
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
