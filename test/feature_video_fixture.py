"""Shared access to the signed feature-video manifest fixture. Not a test module.

Two tools have to agree on one signed byte format: the dashboard's verifier
(``kiro_crew.platform.feed_trust``) and the publishing tool that produces a release
folder. This module is the single place either side's tests reach that agreement
through, so a helper cannot drift from the fixture it loads.

What is here, and which half each caller needs:

* :func:`load_fixture_manifest` + :func:`pin_fixture_key` — the VERIFY half. Load
  the committed document, point the verifier's pins at the committed public key,
  and assert it verifies. Needs nothing but the two files in
  ``test/fixtures/feature-videos/``.
* :func:`mint_throwaway_key` + :func:`sign_document` — the SIGN half, for a
  round-trip test that produces a document and then verifies it. The private key is
  minted per test rather than committed (see
  ``test/fixtures/feature-videos/regenerate.py`` for why), and
  :func:`sign_document` is the canonical byte form written out in ``feed_trust``'s
  module docstring.

:func:`canonical_bytes` is deliberately a LOCAL implementation rather than an
import from ``feed_trust``: a test that signs with the same function the verifier
canonicalizes with cannot detect the two disagreeing, which is the one failure this
fixture exists to catch. It is nine tokens, and ``test_feature_videos_hosted.py``
pins it against the verifier's own answer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "feature-videos"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
PUBLIC_KEY_PATH = FIXTURE_DIR / "signing-key.pub.pem"

#: RSA size for a minted test key. Matches the floor the publishing tool enforces.
RSA_BITS = 3072


def canonical_bytes(document: dict) -> bytes:
    """The exact bytes a signature covers.

    Sorted keys (nested objects too), no whitespace, ASCII-escaped, one trailing
    newline, encoded ASCII. The full rationale for each step, and what breaks when
    a tool picks UTF-8 instead, is in ``feed_trust``'s module docstring.
    """
    payload = {key: value for key, value in document.items() if key != "signature"}
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")


def load_fixture_manifest() -> dict:
    """The committed signed manifest, as a dict."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def openssl_or_skip() -> str:
    """Absolute path to openssl, or raise ``SkipTest``.

    Absolute rather than a bare name for the same reason production resolves it
    from fixed directories: a shim earlier on PATH would make every positive case
    pass for the wrong reason.
    """
    import pytest

    found = shutil.which("openssl")
    if found is None:
        pytest.skip("openssl not available")
    return found


def key_id_of(public_key: Path) -> str:
    """The ``sha256:<hex>`` identity of *public_key*, computed as ``cli.sh`` does."""
    der = subprocess.run(
        [openssl_or_skip(), "pkey", "-pubin", "-in", str(public_key), "-outform", "DER"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def pin_fixture_key(monkeypatch: object, public_key: "Path | None" = None) -> str:
    """Point ``feed_trust``'s pins at *public_key* (the fixture's by default).

    Returns the pinned key id. Also pins ``trusted_system_bin`` to the harness's own
    absolute openssl: on a host whose openssl sits outside the fixed system
    directories (Windows CI's Git-bundled copy), production resolution returns None
    and every positive case would fail for a reason unrelated to what it tests.
    """
    from kiro_crew.platform import feed_trust

    path = public_key or PUBLIC_KEY_PATH
    openssl = openssl_or_skip()
    monkeypatch.setattr(feed_trust, "trusted_system_bin", lambda _n: openssl)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        feed_trust,
        "PINNED_PUBLIC_KEY_B64",
        base64.b64encode(path.read_bytes()).decode("ascii"),
    )
    pinned = key_id_of(path)
    monkeypatch.setattr(feed_trust, "PINNED_KEY_ID", pinned)  # type: ignore[attr-defined]
    return pinned


def mint_throwaway_key(directory: Path) -> "tuple[Path, Path]":
    """Generate an RSA pair in *directory*. Returns ``(private, public)``.

    For the signing half of a round-trip test. Pass a temp directory that pytest
    reclaims — the private half must not outlive the run, which is the whole reason
    it is not a committed file. A session-scoped temp dir is fine and is what
    ``test_feature_videos_hosted.py`` uses, since a 3072-bit keygen per test is most
    of a file's runtime.
    """
    openssl = openssl_or_skip()
    private = directory / "throwaway-private.pem"
    public = directory / "throwaway-public.pem"
    subprocess.run(
        [
            openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            f"rsa_keygen_bits:{RSA_BITS}",
            "-out",
            str(private),
        ],
        check=True,
        # cwd=directory on both: a test's children must not inherit the checkout as
        # their working directory, so anything either one writes relative to it
        # lands in the temp dir the caller owns rather than in the repository.
        cwd=directory,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [openssl, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
        cwd=directory,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return private, public


def sign_document(private_key: Path, document: dict, scratch: Path) -> dict:
    """Return *document* with a ``signature`` over its canonical bytes.

    RSASSA-PKCS1-v1_5 over SHA-256 — what ``openssl dgst -sha256 -sign`` produces
    and what AWS KMS calls ``RSASSA_PKCS1_V1_5_SHA_256``.
    """
    payload = scratch / "canonical.json"
    payload.write_bytes(canonical_bytes(document))
    signature = subprocess.run(
        [openssl_or_skip(), "dgst", "-sha256", "-sign", str(private_key), str(payload)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return {**document, "signature": base64.b64encode(signature).decode("ascii")}


__all__ = [
    "FIXTURE_DIR",
    "MANIFEST_PATH",
    "PUBLIC_KEY_PATH",
    "RSA_BITS",
    "canonical_bytes",
    "key_id_of",
    "load_fixture_manifest",
    "mint_throwaway_key",
    "openssl_or_skip",
    "pin_fixture_key",
    "sign_document",
]
