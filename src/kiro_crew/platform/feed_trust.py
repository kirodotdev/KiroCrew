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
pinned by ``test_feed_trust.py``): one trust root, several consumers.

The second in-process consumer is the hosted feature-video manifest
(``feature_videos_manifest``), which reaches the same key through
:func:`verify_document_signature`. It differs from the CLI feed in exactly two
respects — its payload is NESTED (it carries a list of clips) and it is allowed
to be larger — so both are parameters of the shared core rather than a second
copy of it. What is NOT a parameter is the key, the canonical-JSON shape or the
openssl invocation: one trust root means one verification.

The two documents cannot be substituted for one another despite sharing a key,
because each carries a ``schema`` string inside the signed payload and each
consumer refuses a schema that is not its own.

Rotating the pinned key is therefore a THREE-party event, not two: ``cli.sh``,
the update feed's publisher, and the feature-video manifest's publisher must all
move together, and every hosted manifest a release still reads back
(``feature_videos_manifest.release_candidates`` — up to five release folders)
must be re-signed, or those installs lose their clips until the next publish.
The pin below is the one place the key lives in this tree; the consumers are
listed here so the next rotation does not discover the third one in production.

The bytes to sign
-----------------

A publisher that produces either document kind MUST hash exactly these bytes.
Stated here rather than left to be read off :func:`_verify_signature`, because a
signing tool lives in a different tree (and, for the feature-video manifest, is
built to run from a bare checkout without importing this package) and the only
thing that makes the two agree is one written definition:

1. Take the document, drop the ``signature`` key. Everything else is signed,
   including ``key_id`` when present and including nested values.
2. Serialize with **sorted keys, no whitespace, ASCII-escaped**:
   ``json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)``.
   ``sort_keys`` sorts NESTED objects too, so a nested payload has one form.
3. Append a single ``"\n"``.
4. Encode **ASCII**. Step 2 already escaped every non-ASCII character, so this
   cannot fail — and it is what makes a clip title in any language sign to the
   same bytes on both sides. A tool that emitted UTF-8 with ``ensure_ascii=False``
   would produce a valid-looking document that never verifies, and only for the
   entries carrying non-ASCII text.
5. Sign with ``RSASSA-PKCS1-v1_5`` over SHA-256 (``openssl dgst -sha256 -sign``;
   AWS KMS names the same thing ``RSASSA_PKCS1_V1_5_SHA_256``), base64 the
   signature, and put it back as the top-level ``signature`` field.

The reference implementation of steps 1-4 is
``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"``
encoded ``ascii`` — nine tokens, one place, and the shared core below is the only
copy this package holds. There is no per-document variation of any step: the two
public entry points differ ONLY in whether a nested payload is allowed and in the
size cap they pass.
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
    """Does the CLI feed *manifest*'s signature verify against the pinned key?

    Mirrors ``cli.sh``'s verification: the signature covers canonical JSON
    (sorted keys, compact separators, ASCII, trailing newline) of every field
    except ``signature`` itself. Synchronous — it shells out to openssl — so
    async callers must offload it (``asyncio.to_thread``).

    Returns ``False`` on ANY failure, including openssl being unavailable:
    the caller treats an unverifiable floor as no floor.

    ``key_id`` is REQUIRED here and the payload must be FLAT (every value a
    string), exactly as ``cli.sh`` produces it — this is the strict feed shape,
    not the general one. A document of another kind uses
    :func:`verify_document_signature`.
    """
    if not isinstance(manifest, dict) or manifest.get("key_id") != PINNED_KEY_ID:
        return False
    return _verify_signature(manifest, max_payload_bytes=_MAX_PAYLOAD_BYTES, flat_strings_only=True)


def verify_document_signature(document: dict, *, max_payload_bytes: int) -> bool:
    """Does *document*'s signature verify against the same pinned offline key?

    The general form of :func:`verify_manifest_signature`, for a signed document
    whose payload is not the CLI feed's flat string map. Two differences, both
    forced by the shape of such a document and neither of them a relaxation of
    the trust decision:

    * the payload may carry nested JSON (lists, numbers, objects) — the
      canonical-JSON encoding sorts nested keys too, so the publisher and this
      verifier agree on the bytes either way;
    * *max_payload_bytes* is the caller's, because a catalog is legitimately
      larger than a channel descriptor. It is still a hard cap: a document over
      it is refused, not truncated.

    ``key_id`` is OPTIONAL and, when present, must equal :data:`PINNED_KEY_ID`.
    The key is PINNED, so ``key_id`` was never what established trust — it is a
    publisher-side hint about which key was used, and requiring it would make a
    publisher that omits the hint indistinguishable from a forgery.

    Returns ``False`` on ANY failure, including openssl being unavailable.
    Synchronous — async callers must offload it.
    """
    if not isinstance(document, dict):
        return False
    if "key_id" in document and document.get("key_id") != PINNED_KEY_ID:
        return False
    return _verify_signature(document, max_payload_bytes=max_payload_bytes, flat_strings_only=False)


def _verify_signature(manifest: dict, *, max_payload_bytes: int, flat_strings_only: bool) -> bool:
    """Shared core: canonicalize the payload and verify it with openssl.

    One body for every signed document Kiro Crew honours, so the canonical-JSON
    shape, the signature bounds, the trusted-openssl resolution and the
    fail-safe direction cannot drift between callers. Callers own only their own
    shape checks (which key_id discipline, which payload cap, which schema).
    """
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
    if flat_strings_only and not all(isinstance(value, str) for value in payload.values()):
        return False
    try:
        canonical = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        # A nested payload can carry something json cannot serialize (or can nest
        # past the recursion limit). Unencodable means unverifiable, which is the
        # fail-safe direction — never an exception out of a verification call.
        return False
    if len(canonical) > max_payload_bytes:
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


__all__ = [
    "PINNED_KEY_ID",
    "PINNED_PUBLIC_KEY_B64",
    "verify_document_signature",
    "verify_manifest_signature",
]
