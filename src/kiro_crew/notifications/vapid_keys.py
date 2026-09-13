"""VAPID keypair management for Web Push (RFC 8292).

A single application-server keypair identifies this gateway to every push
service. The private key signs the VAPID JWT; the public key, handed to the
browser as ``applicationServerKey``, lets the push service verify that
signature. The pair is durable state: rotating it invalidates every existing
subscription (the browser bound its subscription to the old public key), so we
generate once and persist, mirroring the atomic-write discipline of
``notifications/settings.py``.

Key format is NIST P-256 (secp256r1), the only curve Web Push allows. The
public key is published in uncompressed point form, base64url without padding
(the exact shape the browser's ``PushManager.subscribe`` expects).
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_KEYS_FILENAME = "vapid_keys.json"
# Serializes the generate-and-persist path only; readers take a lock-free
# snapshot of the module-cached keypair (see _get_keys).
_lock = threading.Lock()
_cached: dict[str, Any] | None = None


def _keys_path():
    return config_dir() / _KEYS_FILENAME


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding (the Web Push / JWT convention)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _private_key_from_pem(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("stored VAPID key is not an EC private key")
    return key


def _public_key_b64url(private_key: ec.EllipticCurvePrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(raw)


def _generate() -> dict[str, Any]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {"private_pem": pem, "public_b64url": _public_key_b64url(private_key)}


def _get_keys() -> dict[str, Any]:
    """Return the cached keypair dict, loading or generating it on first use.

    ``_cached`` is rebound wholesale under the lock, so the fast path (already
    cached) never blocks. The private key material only ever leaves this module
    via :func:`get_private_key`, keeping callers off the PEM.
    """
    global _cached
    cached = _cached
    if cached is not None:
        return cached
    with _lock:
        if _cached is not None:
            return _cached
        path = _keys_path()
        keys: dict[str, Any] | None = None
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(data, dict)
                    and isinstance(data.get("private_pem"), str)
                    and isinstance(data.get("public_b64url"), str)
                ):
                    # Validate the PEM parses before trusting the cached file;
                    # a corrupt key must regenerate, not crash every push.
                    _private_key_from_pem(data["private_pem"])
                    keys = {
                        "private_pem": data["private_pem"],
                        "public_b64url": data["public_b64url"],
                    }
        except Exception:
            logger.warning("Failed to load %s; regenerating VAPID keys", path, exc_info=True)
            keys = None
        if keys is None:
            keys = _generate()
            # Owner-only: the file holds the VAPID EC private key. Same posture
            # as sel.py's key write — mode must not come from the process umask.
            atomic_write(_keys_path(), json.dumps(keys, indent=2), restrict_to_owner=True)
        _cached = keys
        return keys


def public_key_b64url() -> str:
    """The application server public key for the browser's applicationServerKey."""
    return _get_keys()["public_b64url"]


def get_private_key() -> ec.EllipticCurvePrivateKey:
    """The EC private key used to sign the VAPID JWT (server-side only)."""
    return _private_key_from_pem(_get_keys()["private_pem"])


def reset_cache_for_tests() -> None:
    """Drop the module cache so a test can point config_dir at a fresh tmp_path."""
    global _cached
    with _lock:
        _cached = None
