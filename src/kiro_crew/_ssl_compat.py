"""Ensure the system CA bundle is visible to OpenSSL before any HTTPS call.

Must run before any library (aiohttp, slack_sdk, requests) caches its
default SSL context, otherwise every HTTPS call fails with
CERTIFICATE_VERIFY_FAILED.

Two distinct gaps, one fix: on Amazon Linux dev-desktops, mise-installed
Python looks for certs at ``/etc/ssl/`` but Amazon Linux stores them at
``/etc/pki/tls/`` (the ``_CA_CANDIDATES`` file paths below). On macOS, a
python.org/Homebrew/pyenv-built CPython ships no bundled CA store at all and
does not read the OS Keychain via any of these file paths — the standard
fix there is ``certifi``'s bundled Mozilla root store, already present as a
transitive dependency (``pip``, ``requests``, ``slack-sdk`` all pull it) but
never installed as a direct one, so it is used only as a soft fallback: if
it is not importable, this function is a no-op on that platform, matching
prior behavior.
"""

from __future__ import annotations

import os
from pathlib import Path

_CA_CANDIDATES = (
    "/etc/pki/tls/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
)


def _ensure_ssl_certs() -> None:
    """Point OpenSSL at a working CA bundle before any library caches it.

    Sets both ``SSL_CERT_FILE`` (used by OpenSSL / aiohttp) and
    ``REQUESTS_CA_BUNDLE`` (used by the ``requests`` library / slack_sdk).
    """
    if os.environ.get("SSL_CERT_FILE"):
        return

    import ssl

    defaults = ssl.get_default_verify_paths()
    if defaults.cafile and Path(defaults.cafile).exists():
        return

    for candidate in _CA_CANDIDATES:
        if Path(candidate).exists():
            os.environ["SSL_CERT_FILE"] = candidate
            os.environ.setdefault("REQUESTS_CA_BUNDLE", candidate)
            return

    # None of the Linux system paths exist (e.g. macOS) — fall back to
    # certifi's bundle if it happens to be installed transitively.
    try:
        import certifi

        bundle = certifi.where()
    except ImportError:
        return
    if Path(bundle).exists():
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
