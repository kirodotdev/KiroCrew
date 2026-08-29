"""Make platform trust available before any HTTPS client caches an SSL context.

macOS trust is policy-aware and cannot be represented faithfully as a static
PEM bundle, so in-process clients delegate evaluation to Security.framework.
Windows child processes cannot inherit CryptoAPI evaluation. For them, this
module exports only HTTPS-capable anchors from the trusted Root store into an
owner-only bundle layered on certifi, after removing any DER identities in
Windows' Disallowed store. If those stores cannot be read, the module leaves
the generated mirror unused and retains the older file-based fallback; that is
degraded Windows-policy fidelity, not fail-closed distrust enforcement. Other
platforms keep the file-based bootstrap used for Linux distributions whose
Python default points at the wrong CA location.
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
import sys
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import paths as config_paths

try:  # requests dependency; guarded for minimal/source-only installations.
    import certifi
except ImportError:
    certifi = None  # type: ignore[assignment]

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
_WINDOWS_CA_BUNDLE_RELATIVE_PATH = Path("trust") / "ca-bundle.pem"
_WINDOWS_CA_BUNDLE_ENV = "KIROCREW_MANAGED_CA_BUNDLE"
_WINDOWS_SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
_TRUSTSTORE_INJECTED = False
_WINDOWS_CA_REFRESHED = False


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


def _windows_ca_certs(certifi_path: Path) -> tuple[list[bytes], list[bytes], int]:
    """Return filtered certifi roots, extra Windows roots, and deny removals.

    The Windows ``CA`` store contains intermediates. Flattening it into a PEM
    CA file would promote every intermediate to an unconditional trust anchor,
    so only the trusted ``ROOT`` store is exported. Per-certificate enhanced
    key usage is retained as far as a PEM bundle allows by accepting entries
    trusted for every purpose or explicitly for TLS server authentication.

    Identity decisions use SHA-256 over DER throughout.  That lets Windows'
    ``Disallowed`` store remove a certificate even when it came from certifi,
    and lets one set enforce de-duplication across certifi and ``ROOT`` without
    relying on PEM whitespace or comments.
    """
    enum_certificates = getattr(ssl, "enum_certificates", None)
    if enum_certificates is None:
        raise RuntimeError("Windows certificate-store enumeration is unavailable")

    # Windows distrust wins over trust when the same DER certificate appears
    # in certifi or ROOT. Read the deny set first and let enumeration failures
    # abort this generated mirror. _ensure_ssl_certs then preserves the older
    # certifi/default verification fallback, which is availability-preserving
    # but cannot claim to reproduce Windows explicit distrust.
    distrusted_fingerprints = {
        hashlib.sha256(cert_der).digest()
        for cert_der, encoding, _trust in enum_certificates("Disallowed")
        if encoding == "x509_asn" and isinstance(cert_der, bytes)
    }
    context = ssl.create_default_context(cafile=str(certifi_path))
    known_fingerprints = set(distrusted_fingerprints)
    certifi_roots: list[bytes] = []
    removed_distrusted = 0
    for cert_der in context.get_ca_certs(binary_form=True):
        if not isinstance(cert_der, bytes):
            continue
        fingerprint = hashlib.sha256(cert_der).digest()
        if fingerprint in distrusted_fingerprints:
            removed_distrusted += 1
            continue
        if fingerprint in known_fingerprints:
            continue
        certifi_roots.append(cert_der)
        known_fingerprints.add(fingerprint)

    extra_roots: list[bytes] = []
    for cert_der, encoding, trust in enum_certificates("ROOT"):
        if encoding != "x509_asn" or not isinstance(cert_der, bytes):
            continue
        if trust is not True and (
            not isinstance(trust, set) or _WINDOWS_SERVER_AUTH_OID not in trust
        ):
            continue

        fingerprint = hashlib.sha256(cert_der).digest()
        if fingerprint in known_fingerprints:
            continue
        extra_roots.append(cert_der)
        known_fingerprints.add(fingerprint)
    return certifi_roots, extra_roots, removed_distrusted


def _build_windows_ca_bundle(bundle_path: Path) -> bool:
    """Publish filtered certifi plus trusted Windows roots at *bundle_path*.

    Returns ``False`` without changing TLS verification when certifi is absent,
    the Windows stores cannot be read, or the bundle cannot be protected and
    atomically published. A store-read failure falls back to the pre-existing
    file-based trust path; that preserves verified HTTPS availability but does
    not project Windows ``Disallowed`` decisions.
    """
    try:
        if certifi is None:
            return False
        certifi_path = Path(certifi.where())
        certifi_roots, extra_roots, _removed_distrusted = _windows_ca_certs(certifi_path)

        certifi_pem = "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in certifi_roots)
        extra_pem = "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in extra_roots)
        bundle_text = certifi_pem
        if extra_pem:
            bundle_text += "# Windows trusted Root certificates\n" + extra_pem
        bundle = bundle_text.encode("ascii")
        atomic_write(bundle_path, bundle, restrict_to_owner=True)
    except Exception:
        logger.warning(
            "Could not mirror the Windows system trust store; falling back to "
            "file-based CA discovery, which may not reflect Windows Disallowed entries",
            exc_info=True,
        )
        return False
    return True


def _ensure_ssl_certs() -> None:
    """Configure platform trust before any HTTPS library caches its context.

    An explicit ``SSL_CERT_FILE`` remains the highest-precedence escape hatch.
    A private provenance marker distinguishes it from this module's generated
    path after an in-process re-exec, where the managed bundle must be refreshed
    against the current Windows stores instead of mistaken for an operator
    override.
    macOS additionally installs Security.framework evaluation for this
    process's own clients. Windows exports trusted system roots to a protected
    bundle. Both platforms still set ``SSL_CERT_FILE`` /
    ``REQUESTS_CA_BUNDLE`` for child processes (kiro-cli, Node MCP servers)
    that cannot inherit process-local trust configuration.
    """
    global _WINDOWS_CA_REFRESHED

    configured_bundle = os.environ.get("SSL_CERT_FILE")
    managed_bundle = os.environ.get(_WINDOWS_CA_BUNDLE_ENV)
    managed_reentry = (
        sys.platform == "win32"
        and not _WINDOWS_CA_REFRESHED
        and bool(configured_bundle)
        and configured_bundle == managed_bundle
    )
    if configured_bundle and not managed_reentry:
        return

    if managed_reentry:
        # ``os.execve`` and surviving gatewayd children inherit our environment.
        # Remove only values this module marked as managed before rebuilding so
        # a store-read failure takes the verified fallback below rather than
        # silently retaining a stale mirror.  The old file stays intact for
        # already-running children until a successful atomic replacement.
        os.environ.pop("SSL_CERT_FILE", None)
        if os.environ.get("REQUESTS_CA_BUNDLE") == configured_bundle:
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
        os.environ.pop(_WINDOWS_CA_BUNDLE_ENV, None)

    if sys.platform == "darwin":
        _inject_macos_system_trust()

    if sys.platform == "win32":
        try:
            bundle_path = config_paths.config_dir() / _WINDOWS_CA_BUNDLE_RELATIVE_PATH
        except Exception:
            # Home resolution creates the directory and may fail on an invalid
            # override or an ACL-denied parent.  This prelude runs during CLI
            # import, so such a failure must not make even ``--help`` unusable.
            logger.warning(
                "Could not resolve the Windows CA bundle path; falling back to "
                "file-based CA discovery",
                exc_info=True,
            )
            bundle_path = None
        if bundle_path is not None and _build_windows_ca_bundle(bundle_path):
            os.environ["SSL_CERT_FILE"] = str(bundle_path)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", str(bundle_path))
            os.environ[_WINDOWS_CA_BUNDLE_ENV] = str(bundle_path)
            _WINDOWS_CA_REFRESHED = True
            return

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
    if certifi is None:
        return
    bundle = certifi.where()
    if Path(bundle).exists():
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
