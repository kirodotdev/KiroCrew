#!/usr/bin/env python3
"""Regenerate the shared feature-video manifest fixture. Not a test.

What lives in this directory, and why the private key does not
-------------------------------------------------------------

``manifest.json`` is a real signed manifest, and ``signing-key.pub.pem`` is the
public half of the throwaway key that signed it. It is a CROSS-TOOL fixture: the
dashboard's verifier (``kiro_crew.platform.feed_trust``) and the publishing tool's
verifier both have to accept these exact bytes, so it pins the signed byte format
in one artifact instead of in two independent test helpers that can drift.

The PRIVATE half is deliberately absent. A committed private key -- throwaway or
not -- puts a PEM private-key block in the diff, which the repo's SAST job flags
through `p/secrets`, and the honest fix for that finding is not to add an exclusion
for our own file. So the key is minted on demand instead:

* to VERIFY the fixture, nothing more than the two committed files is needed
  (``test/feature_video_fixture.py`` pins them into the verifier);
* to SIGN in a test -- a publisher's round-trip half -- mint a fresh pair with
  ``feature_video_fixture.mint_throwaway_key()`` and sign with
  ``feature_video_fixture.sign_document()``, which is the canonical byte form from
  ``feed_trust``'s module docstring. Both are importable from the ``test/``
  directory, so no tree needs its own copy.

Run this when the manifest schema changes:

    python3 test/fixtures/feature-videos/regenerate.py

It mints a new pair in a temp directory, re-signs the document, writes
``manifest.json`` and ``signing-key.pub.pem``, and lets the private half go with
the temp directory. Regenerating rotates the key, which is fine and expected: the
key has no security value and nothing pins its identity -- the fixture is proof of
a byte FORMAT, never of an authority.

Requires ``openssl`` on PATH.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.json"
PUBLIC_KEY_PATH = HERE / "signing-key.pub.pem"

#: RSA size. 3072 is the floor the publishing tool enforces on a signing key, so
#: the fixture key clears the same bar the real one does.
_RSA_BITS = 3072

#: The document, minus its signature. Deliberately exercises the parts of the
#: shape most likely to be encoded differently by two tools:
#:
#: * two entries, so nested-list ordering is pinned;
#: * keys written out of alphabetical order, so ``sort_keys`` is doing real work;
#: * a non-ASCII title, which is the case that separates an ASCII-escaped
#:   canonical form from a UTF-8 one -- the two produce different bytes and only
#:   for entries carrying such text, so a fixture without one would verify on both
#:   sides while the encodings disagreed;
#: * an entry with ``used_when`` and ``min_version`` set, and one with both empty,
#:   since those are the optional-ish fields.
_DOCUMENT: dict = {
    "schema": "kirocrew-feature-videos-manifest-v1",
    "release": "0.6.0",
    "cdn_base": "https://cdn.example.invalid/feature-videos",
    "generated_at": "2026-01-31T09:00:00Z",
    "entries": [
        {
            "id": "fixture-clip",
            "feature": "fixture-clip",
            "title": "A fixture clip",
            "description": "Two plain sentences. This one is the ASCII entry.",
            "file": "fixture-clip.mp4",
            "poster": "fixture-clip.jpg",
            "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "poster_sha256": ("60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752"),
            "bytes": 1024,
            "duration_s": 18.5,
            "doc": "feature-tips.md",
            "used_when": ["tips_feedback_exists"],
            "min_version": "0.6.0",
        },
        {
            "title": "Une brève présentation — 日本語も",
            "id": "fixture-clip-2",
            "feature": "monitor-loops",
            "description": "Non-ASCII on purpose: this entry is what pins the encoding.",
            "file": "fixture-clip-2.mp4",
            "poster": "fixture-clip-2.jpg",
            "sha256": "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d",
            "poster_sha256": ("e7f6c011776e8db7cd330b54174fd76f7d0216b612387a5ffcfb81e6f0919683"),
            "bytes": 2048,
            "duration_s": 22.0,
            "doc": "monitor-loops.md",
            "used_when": [],
            "min_version": "",
        },
    ],
}


def canonical_bytes(document: dict) -> bytes:
    """The exact bytes a signature covers. See ``feed_trust``'s module docstring."""
    payload = {key: value for key, value in document.items() if key != "signature"}
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fv-fixture-key-") as scratch:
        root = Path(scratch)
        private = root / "private.pem"
        payload = root / "payload.json"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                f"rsa_keygen_bits:{_RSA_BITS}",
                "-out",
                str(private),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(PUBLIC_KEY_PATH)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        payload.write_bytes(canonical_bytes(_DOCUMENT))
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(private), str(payload)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout

    signed = {**_DOCUMENT, "signature": base64.b64encode(signature).decode("ascii")}
    # Written indented and sorted for a readable diff. The signature covers the
    # CANONICAL form, not this one, so pretty-printing here changes nothing about
    # verification -- which is itself worth pinning, and the fixture tests do.
    MANIFEST_PATH.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST_PATH}")
    print(f"wrote {PUBLIC_KEY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
