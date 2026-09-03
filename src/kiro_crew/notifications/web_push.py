"""Web Push sender: VAPID (RFC 8292) auth + RFC 8291 aes128gcm encryption.

Sends one encrypted notification to one browser push subscription over the
standard Web Push protocol. Every push service (Apple, Mozilla, Google) speaks
the same wire format, so a single implementation covers Safari/iOS, Firefox and
Chrome; WebKit's Declarative Web Push (iOS 18.4+) consumes the very same
encrypted POST, rendering the JSON payload natively without a service-worker
push handler.

No third-party dependency: ``cryptography`` (already a core dependency) provides
ES256 JWT signing and the ECDH/HKDF/AES-128-GCM primitives RFC 8291 needs.

The two RFCs this file implements:
- RFC 8291 (Message Encryption): ECDH between an ephemeral server key and the
  subscription's ``p256dh`` key derives a shared secret; HKDF with the ``auth``
  secret expands it to a content-encryption key + nonce; the plaintext is
  sealed with AES-128-GCM and framed with the ``aes128gcm`` content-coding
  header (salt, record size, server public key).
- RFC 8292 (VAPID): a short-lived ES256 JWT, signed with the application
  server key, proves to the push service which application server is sending.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kiro_crew.notifications import vapid_keys

logger = logging.getLogger(__name__)

# The application-server contact URI in the VAPID JWT `sub` claim. Apple's push
# service is the strict one: `sub` must be a mailto: or https: URI and the JWT
# `exp` must be no more than 24h out, else it returns 403 BadJwtToken.
_VAPID_SUBJECT = "mailto:kirocrew@localhost"
# Well under Apple's 24h ceiling, comfortably longer than any single request.
_VAPID_TTL_SECONDS = 12 * 60 * 60
# Push-service TTL: how long to retain the message for a disconnected device.
_MESSAGE_TTL_SECONDS = 24 * 60 * 60


@dataclass
class PushResult:
    """Outcome of one push attempt.

    ``gone`` is the actionable signal: the push service returned 404/410, so the
    subscription is dead and the caller must delete it from the store. Nothing
    else about the attempt is actionable — a non-gone failure is logged inside
    :func:`send_web_push` and needs no further discrimination by the caller.
    """

    gone: bool


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _es256_jwt(claims: dict[str, Any], private_key: ec.EllipticCurvePrivateKey) -> str:
    """Sign a JWT with ES256, emitting the JOSE-required raw 64-byte signature.

    ``cryptography`` produces an ASN.1 DER-encoded ECDSA signature; JOSE (and
    thus VAPID) requires the fixed-width r||s concatenation, so we transcode.
    """
    header = {"typ": "JWT", "alg": "ES256"}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    segments.append(_b64url(raw_sig))
    return ".".join(segments)


def _vapid_headers(endpoint: str, private_key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    parsed = urlparse(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"
    claims = {
        "aud": audience,
        "exp": int(time.time()) + _VAPID_TTL_SECONDS,
        "sub": _VAPID_SUBJECT,
    }
    token = _es256_jwt(claims, private_key)
    return {"Authorization": f"vapid t={token}, k={vapid_keys.public_key_b64url()}"}


def _encrypt(payload: bytes, p256dh_b64: str, auth_b64: str) -> bytes:
    """Encrypt a payload for one subscription per RFC 8291 (aes128gcm coding).

    Returns the full body: the aes128gcm header (salt, record size, server
    public key) followed by the single AES-128-GCM record.
    """
    ua_public_bytes = _b64url_decode(p256dh_b64)
    auth_secret = _b64url_decode(auth_b64)

    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_bytes)
    server_private = ec.generate_private_key(ec.SECP256R1())
    server_public_bytes = server_private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    shared_secret = server_private.exchange(ec.ECDH(), ua_public)

    salt = os.urandom(16)

    # Step 1: mix the ECDH secret with the auth secret into a pseudo-random key.
    # The info string binds the PRK to both parties' public keys (RFC 8291 §3.3).
    key_info = b"WebPush: info\x00" + ua_public_bytes + server_public_bytes
    prk = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(
        shared_secret
    )

    # Step 2: derive the content-encryption key (16B) and nonce (12B) from the
    # PRK, salted with the per-message salt.
    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(prk)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(prk)

    # RFC 8188 record: plaintext gets a 0x02 delimiter (last-and-only record),
    # then AES-128-GCM seals it (the 16-byte tag is appended by AESGCM).
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)

    record_size = len(ciphertext) + 21  # header overhead the coding accounts for
    header = salt + struct.pack("!L", record_size) + bytes([len(server_public_bytes)])
    header += server_public_bytes
    return header + ciphertext


async def send_web_push(
    session: Any, subscription: dict[str, Any], payload: dict[str, Any]
) -> PushResult:
    """Encrypt and POST one notification to one subscription.

    ``session`` is an ``aiohttp.ClientSession``. ``payload`` is the JSON the
    service worker / declarative renderer receives. Returns a :class:`PushResult`;
    the caller deletes the subscription when ``gone`` is set (404/410). Network
    and encryption errors are caught and reported as a non-ok, non-gone result
    so one dead endpoint never blocks fan-out to the others.
    """
    endpoint = subscription["endpoint"]
    keys = subscription["keys"]
    try:
        body = _encrypt(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            keys["p256dh"],
            keys["auth"],
        )
        headers = {
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(_MESSAGE_TTL_SECONDS),
            "Urgency": "normal",
            **_vapid_headers(endpoint, vapid_keys.get_private_key()),
        }
        async with session.post(endpoint, data=body, headers=headers) as resp:
            status = resp.status
            # 404/410 are the push service telling us the subscription is dead
            # (RFC 8030): prune it. 201/200 are success; anything else — incl.
            # 413 for a payload over the 4 KB ceiling (guarded against upstream
            # by truncating the note in _web_push_payload) — is a transient
            # failure we neither retry here nor treat as gone.
            gone = status in (404, 410)
            ok = 200 <= status < 300
            if not ok and not gone:
                logger.warning("Web push to %s returned HTTP %s", urlparse(endpoint).netloc, status)
            return PushResult(gone=gone)
    except Exception:
        logger.warning("Web push delivery failed for %s", urlparse(endpoint).netloc, exc_info=True)
        return PushResult(gone=False)
