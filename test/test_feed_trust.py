"""The gateway's feed trust root must be cli.sh's trust root, and must verify.

``platform/feed_trust.py`` gates the forced-update floor on the manifest
signature. Two properties are load-bearing:

* **One trust root.** The module's pinned key id and public key are copies of
  ``cli.sh``'s embedded constants. A drift (key rotation updating one and not
  the other) would silently disable every future floor — verification fails
  closed, so nothing reddens. Pin the equality structurally.
* **Fail toward freedom.** Any verification failure must read as "no floor",
  never as "required". These tests exercise the failure modes the caller
  relies on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.platform import feed_trust

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "cli.sh"


def _installer_constant(name: str) -> str:
    match = re.search(rf'^{name}="([^"]+)"', INSTALLER.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"cli.sh no longer defines {name}"
    return match.group(1)


class TestOneTrustRoot:
    def test_key_id_matches_cli_sh(self) -> None:
        assert feed_trust.PINNED_KEY_ID == _installer_constant("CLI_MANIFEST_KEY_ID")

    def test_public_key_matches_cli_sh(self) -> None:
        assert feed_trust.PINNED_PUBLIC_KEY_B64 == _installer_constant(
            "CLI_MANIFEST_PUBLIC_KEY_B64"
        )

    def test_key_id_is_the_digest_of_the_pinned_key(self) -> None:
        """The two constants describe the same key, DER-normalized the same way
        cli.sh computes it (openssl pkey -pubin -outform DER | sha256)."""
        if shutil.which("openssl") is None:
            pytest.skip("openssl not available")
        pem = base64.b64decode(feed_trust.PINNED_PUBLIC_KEY_B64, validate=True)
        der = subprocess.run(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=pem,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        assert feed_trust.PINNED_KEY_ID == f"sha256:{hashlib.sha256(der).hexdigest()}"


@pytest.fixture()
def signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh RSA key pair, with the module's pins repointed at it so the
    positive path is testable without the production private key. The openssl
    resolution is pinned to the harness's own (absolute) binary too: on hosts
    whose openssl lives outside the fixed system directories (Windows CI's
    Git-bundled copy), production resolution would return None and every
    positive case would fail for a reason unrelated to what it tests."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl not available")
    monkeypatch.setattr(feed_trust, "trusted_system_bin", lambda _n: openssl)
    private = tmp_path / "private.pem"
    public = tmp_path / "public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(public), "-outform", "DER"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    monkeypatch.setattr(
        feed_trust,
        "PINNED_PUBLIC_KEY_B64",
        base64.b64encode(public.read_bytes()).decode("ascii"),
    )
    monkeypatch.setattr(feed_trust, "PINNED_KEY_ID", f"sha256:{hashlib.sha256(der).hexdigest()}")
    return private


def _signed_manifest(private: Path, tmp_path: Path, **fields: str) -> dict:
    payload = {
        "channel": "stable",
        "key_id": feed_trust.PINNED_KEY_ID,
        "schema": "kirocrew-cli-artifact-manifest-v1",
        "version": "0.6.0",
        **fields,
    }
    canonical = tmp_path / "payload.json"
    canonical.write_bytes(
        (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
    )
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private), str(canonical)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return {**payload, "signature": base64.b64encode(signature).decode("ascii")}


class TestVerification:
    def test_a_correctly_signed_manifest_verifies(self, signing_key, tmp_path) -> None:
        manifest = _signed_manifest(signing_key, tmp_path, min_version="0.6.0")
        assert feed_trust.verify_manifest_signature(manifest) is True

    def test_tampering_any_field_invalidates_the_signature(self, signing_key, tmp_path) -> None:
        manifest = _signed_manifest(signing_key, tmp_path, min_version="0.6.0")
        manifest["min_version"] = "9.9.9"
        assert feed_trust.verify_manifest_signature(manifest) is False

    def test_wrong_key_id_fails_before_any_subprocess(self, signing_key, tmp_path) -> None:
        manifest = _signed_manifest(signing_key, tmp_path)
        manifest["key_id"] = "sha256:" + "0" * 64
        assert feed_trust.verify_manifest_signature(manifest) is False

    def test_missing_or_junk_signature_fails(self, signing_key, tmp_path) -> None:
        manifest = _signed_manifest(signing_key, tmp_path)
        for bad in ("", "not base64!!", None):
            probe = dict(manifest)
            if bad is None:
                probe.pop("signature")
            else:
                probe["signature"] = bad
            assert feed_trust.verify_manifest_signature(probe) is False

    def test_non_string_field_fails_instead_of_crashing(self, signing_key, tmp_path) -> None:
        manifest = _signed_manifest(signing_key, tmp_path)
        manifest["min_version"] = 7  # type: ignore[assignment]
        assert feed_trust.verify_manifest_signature(manifest) is False

    def test_openssl_unavailable_reads_as_unverified(
        self, signing_key, tmp_path, monkeypatch
    ) -> None:
        manifest = _signed_manifest(signing_key, tmp_path)
        # No trusted system openssl (the module never falls back to PATH).
        monkeypatch.setattr(feed_trust, "trusted_system_bin", lambda _n: None)
        assert feed_trust.verify_manifest_signature(manifest) is False

    def test_path_is_never_consulted_for_openssl(self, signing_key, tmp_path, monkeypatch) -> None:
        """The binary comes from trusted_system_bin, not a bare argv name: a
        PATH shim exiting 0 must not be able to bless a forged floor."""
        manifest = _signed_manifest(signing_key, tmp_path)
        seen: list[str] = []
        real_run = feed_trust.subprocess.run

        def _spy(argv, *a, **k):
            seen.append(argv[0])
            return real_run(argv, *a, **k)

        monkeypatch.setattr(feed_trust.subprocess, "run", _spy)
        assert feed_trust.verify_manifest_signature(manifest) is True
        assert (
            seen and Path(seen[0]).is_absolute()
        ), f"openssl invoked as {seen!r} — must be an absolute trusted path"
