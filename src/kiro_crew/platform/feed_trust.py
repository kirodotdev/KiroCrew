"""Gateway-side verification of the CLI feed manifest's RSA signature.

The channel feed is normally UNTRUSTED display metadata: the gateway takes
nothing actionable from it, so it deliberately skips signature verification
(see ``dashboard/handlers/updates.py``). The optional ``min_version`` floor is
the one exception — it coerces the dashboard into a non-dismissible update
prompt, so a tampered feed that could set it would hold every dashboard
hostage while the signed installer (correctly) refuses the tampered bytes.
The floor is therefore honored ONLY when the manifest's signature verifies
against the same offline key ``cli.sh`` pins.

Fail-safe direction: any verification failure — missing openssl, malformed
manifest, wrong key, bad signature — drops the floor and degrades to the
ordinary dismissible prompt. It never fails toward coercion.

The pinned constants MUST stay byte-identical to ``cli.sh``'s
``CLI_MANIFEST_KEY_ID`` / ``CLI_MANIFEST_PUBLIC_KEY_B64`` (structurally
pinned by ``test_feed_trust.py``): one trust root, two consumers.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from kiro_crew.platform_compat import trusted_system_bin

logger = logging.getLogger(__name__)

#: SHA-256 of the public SubjectPublicKeyInfo DER bytes — cli.sh's pin.
PINNED_KEY_ID = "sha256:d3a83f0c1ff84a2cbee6bd34d889d8725af34358148a6c18ed3ecbbbcceec06b"

#: Base64 of the PEM public key — cli.sh's embedded copy.
PINNED_PUBLIC_KEY_B64 = (
    "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQm9qQU5CZ2txaGtpRzl3MEJBUUVG"
    "QUFPQ0FZOEFNSUlCaWdLQ0FZRUF0MnR0NnZ3ZFZ4Z0tWbTRGQVdkeApwZjZFckx3Y2ljUHlHUGh2SXdXRTRqNmg1YjlwMzFiaktM"
    "aWlEakxvK3VpQUJPL21vUjdJUUtoaUNSaXY0d0dTCk1mYnd2ZnNhLy8xNlVBbkNURkRDb1pId0IwVm93cTRYWjZ1NHBrdTFqNlBl"
    "RXBMNjVqRXZvcjd1a29HS2xiOVMKQlBva01aN0VtYlpWbmJiSWJBVXYrZ0NWajRCWDRpam5GWkJEMmNPcmtkQWdGR3UraU9jRHVl"
    "RDNqTExicXVhUwp0K0tLWXltQ2VxaitPazZ0OFBMQ2VRZmYrWVc4YS9wRU03Wm1tMTJ0Y3BRdEF0OHVCSVdkZE9qaTN1c3BhVlA3"
    "CkZJUlhzNnJIajIwTDd0dE9kMGpmKzRWQ0ZtV09FWE4rNWc0YS8rNkcrc3lxeDk4VlR2RVF5cDZVdWZnb0FoQkMKLzFVNG5Xajdm"
    "MVRFQkV4dXBSRXFUK1lmUmp6aFJUR2NGN0czRUp3MmZjUU1taElIdFpVanM3endVY3NmblhDMwpGQzJBR3pBZnExSGV0WHU5amFO"
    "QWZSdjdLZXYxT2hvVmMzYUlONEd3UkpZRDNPNUFSQk5SRGpQUVFWUHBaVW5rCjB1WVdpZExSVDVRUVZMYnlSLzJFKytqTWFyRXBk"
    "VXRkZGY1anlwZW5pbFhUQWdNQkFBRT0KLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="
)

#: Same bounds cli.sh enforces on the equivalent values.
_MAX_SIGNATURE_BYTES = 1024
_MAX_PAYLOAD_BYTES = 16384
_OPENSSL_TIMEOUT_SECS = 10


def verify_manifest_signature(manifest: dict) -> bool:
    """Does *manifest*'s embedded signature verify against the pinned key?

    Mirrors ``cli.sh``'s verification: the signature covers canonical JSON
    (sorted keys, compact separators, ASCII, trailing newline) of every field
    except ``signature`` itself. Synchronous — it shells out to openssl — so
    async callers must offload it (``asyncio.to_thread``).

    Returns ``False`` on ANY failure, including openssl being unavailable:
    the caller treats an unverifiable floor as no floor.
    """
    if not isinstance(manifest, dict) or manifest.get("key_id") != PINNED_KEY_ID:
        return False
    signature_b64 = manifest.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        return False
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    if not signature or len(signature) > _MAX_SIGNATURE_BYTES:
        return False

    payload = {key: value for key, value in manifest.items() if key != "signature"}
    if not all(isinstance(value, str) for value in payload.values()):
        return False
    canonical = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")
    if len(canonical) > _MAX_PAYLOAD_BYTES:
        return False

    try:
        pem = base64.b64decode(PINNED_PUBLIC_KEY_B64, validate=True)
    except (ValueError, binascii.Error):  # pragma: no cover - constant is well-formed
        return False

    # Resolved from fixed system directories, never a bare argv name: the
    # gateway's PATH can lead with agent-writable directories, and a planted
    # openssl shim exiting 0 would accept a forged floor — the exact coercion
    # this verification exists to prevent. None (no system openssl) reads as
    # unverified, the fail-safe direction.
    openssl = trusted_system_bin("openssl")
    if openssl is None:
        logger.debug("no trusted openssl available; treating manifest as unverified")
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="kirocrew-feed-trust-") as scratch:
            root = Path(scratch)
            key_path = root / "public.pem"
            payload_path = root / "payload.json"
            signature_path = root / "signature.bin"
            key_path.write_bytes(pem)
            payload_path.write_bytes(canonical)
            signature_path.write_bytes(signature)
            proc = subprocess.run(
                [
                    openssl,
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(key_path),
                    "-signature",
                    str(signature_path),
                    str(payload_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_OPENSSL_TIMEOUT_SECS,
            )
    except (OSError, subprocess.SubprocessError):
        logger.debug("openssl unavailable or failed; treating manifest as unverified")
        return False
    return proc.returncode == 0


__all__ = ["PINNED_KEY_ID", "PINNED_PUBLIC_KEY_B64", "verify_manifest_signature"]
