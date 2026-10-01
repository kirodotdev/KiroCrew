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
# The two variables _ensure_ssl_certs exports for the children that cannot
# inherit a process-local trust injection (kiro-cli, Node MCP servers).
_CA_BUNDLE_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
# The provenance of those exports, one marker PER VARIABLE
# (KIROCREW_EXPORTED_SSL_CERT_FILE, KIROCREW_EXPORTED_REQUESTS_CA_BUNDLE),
# published beside a variable only when this runtime ASSIGNED it and holding
# the value it assigned. A variable equal to its own marker is the runtime's
# -- an earlier pass in this process, or a predecessor's that an in-app
# restart handed down -- never an operator's (see _inherited_export_reason).
# Per variable, not one shared path: an operator may hold REQUESTS_CA_BUNDLE
# at exactly the path this install derives (their appended-CA certifi bundle),
# and a shared marker would vouch for their variable too. It is the ONLY
# provenance: a value without its marker is an operator's, whatever its path
# looks like and whichever directory it lies in. A predecessor from before the
# provenance existed published none; its value is kept, and _derive_trust's
# warning names the way out.
_EXPORTED_ENV_PREFIX = "KIROCREW_EXPORTED_"
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


def _provenance_env(name: str) -> str:
    """The marker variable that carries *name*'s provenance (``KIROCREW_EXPORTED_<name>``)."""
    return f"{_EXPORTED_ENV_PREFIX}{name}"


def _inherited_export_reason(name: str, value: str) -> str | None:
    """Why *value*, read from *name* (one of ``_CA_BUNDLE_ENV``), is this runtime's own export.

    ``None`` when it is not: the value is an operator's. One way, and no other:
    it equals *name*'s own provenance marker (:func:`_provenance_env`), which
    :func:`_export_ca_bundle` publishes beside a variable only when it assigned
    that variable. A successor inherits a variable and its marker together, so
    it reads its predecessor's export as what it is. An operator's value never
    carries one: they set the variable alone, and a variable the derivation
    found already set gets no marker -- even when the operator's value is the
    very path this install derives, which is why the marker is per variable
    rather than one shared path.

    Nothing else is proof. Not the path's shape -- an operator who set
    ``SSL_CERT_FILE`` to a certifi bundle of their own (``$(python -m certifi)``,
    or one they appended a private CA to) holds a working policy while the file
    exists and a fail-closed one once it does not: every handshake fails until
    they repair the pin, exactly as they chose. Not the directory it lies in --
    an operator can place a restricted bundle inside this runtime's own venv,
    and a tree the installers delete takes that pin with it; what remains is a
    fail-closed state that is theirs, not a stale export to re-derive over. On
    macOS an explicit bundle is also an exclusion -- it bypasses the
    Security.framework injection, so the Keychain's CAs are not trusted -- and
    re-deriving over any of these would widen trust past what the operator
    chose.

    The one value this leaves unrecognised is a predecessor's export from
    before the provenance existed: it reads as an operator's and is kept. While
    its file exists it works; once the predecessor's tree is gone the process
    fails closed, and :func:`_derive_trust` names the file and the way out (a
    start from a clean environment, after which this runtime publishes the
    provenance and every later restart is recognised).

    The returned reason is for the one WARNING line, so the operator reading the
    log sees why the value was judged the runtime's.
    """
    exported = os.environ.get(_provenance_env(name))
    if exported and value == exported:
        return "the provenance marker beside it names it"
    return None


def _ensure_ssl_certs() -> None:
    """Configure platform trust before any HTTPS library caches its context.

    An explicit ``SSL_CERT_FILE`` remains the highest-precedence escape hatch,
    with one exception: a value that is this runtime's own export
    (:func:`_inherited_export_reason`), inherited from the predecessor an in-app
    restart replaced, names THAT install. It is dropped -- from
    ``REQUESTS_CA_BUNDLE`` too, and before the operator check, so a stale
    export beside an operator's ``SSL_CERT_FILE`` does not survive either --
    and trust is derived for this install exactly as a clean start derives it
    (:func:`_derive_trust`), with one warning, naming the reason, when the value
    changes. An operator's own bundle is left untouched, with one warning when
    the file it names cannot be found.

    Windows takes none of this: the derivation exports nothing there, so no
    Kiro Crew process can have left a value behind and whatever is set is an
    operator's.

    macOS additionally installs Security.framework evaluation for this
    process's own clients, but ``inject_into_ssl()`` is process-local, so the
    file-based discovery still runs to export ``SSL_CERT_FILE`` /
    ``REQUESTS_CA_BUNDLE`` for child processes (kiro-cli, Node MCP servers)
    that inherit this environment and cannot inherit a monkey-patch.
    """
    inherited: dict[str, tuple[str, str]] = {}
    if sys.platform != "win32":
        for name in _CA_BUNDLE_ENV:
            value = os.environ.get(name)
            if not value:
                continue
            reason = _inherited_export_reason(name, value)
            if reason is not None:
                inherited[name] = (value, reason)
        for name in inherited:
            del os.environ[name]
        # The provenance describes the exports this process makes below, if any;
        # an inherited one has been read and is stale from here on.
        for name in _CA_BUNDLE_ENV:
            os.environ.pop(_provenance_env(name), None)

    _derive_trust()

    replaced = [
        f"{name}={stale} ({reason})"
        for name, (stale, reason) in inherited.items()
        if os.environ.get(name) != stale
    ]
    if replaced:
        # WARNING, not INFO: the prelude runs before logging is configured, and
        # the default last-resort handler drops everything below WARNING.
        logger.warning(
            "Ignoring %s inherited from another Kiro Crew process; "
            "this install resolves its own CA bundle",
            " and ".join(replaced),
        )


def _derive_trust() -> None:
    """The clean-start trust derivation, in precedence order.

    An operator's ``SSL_CERT_FILE`` wins outright; Windows exports nothing;
    macOS injects system trust for this process; then the interpreter's own
    default cafile (nothing to export), the Linux distribution bundles, and
    this install's certifi bundle, exported for children by
    :func:`_export_ca_bundle`.
    """
    preset = os.environ.get("SSL_CERT_FILE")
    if preset:
        if not os.path.exists(preset):
            logger.warning(
                "SSL_CERT_FILE=%s names a file this process cannot find; TLS "
                "connections will fail until it is corrected or unset (if an "
                "earlier Kiro Crew install exported this value, start it from a "
                "clean environment)",
                preset,
            )
        return

    if sys.platform == "win32":
        return

    if sys.platform == "darwin":
        _inject_macos_system_trust()

    defaults = ssl.get_default_verify_paths()
    if defaults.cafile and Path(defaults.cafile).exists():
        return

    for candidate in _CA_CANDIDATES:
        if Path(candidate).exists():
            _export_ca_bundle(candidate)
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
        _export_ca_bundle(bundle)


def _export_ca_bundle(bundle: str) -> None:
    """Export *bundle* for child processes, with the provenance of each assignment.

    ``REQUESTS_CA_BUNDLE`` is only defaulted: an operator's own value there is
    theirs, and it gets no marker -- not even when it equals *bundle*. Each
    variable this call assigns gets its own marker holding the value assigned,
    which is what lets a successor tell this export from an operator's
    (:func:`_inherited_export_reason`).
    """
    os.environ["SSL_CERT_FILE"] = bundle
    os.environ[_provenance_env("SSL_CERT_FILE")] = bundle
    if "REQUESTS_CA_BUNDLE" not in os.environ:
        os.environ["REQUESTS_CA_BUNDLE"] = bundle
        os.environ[_provenance_env("REQUESTS_CA_BUNDLE")] = bundle
