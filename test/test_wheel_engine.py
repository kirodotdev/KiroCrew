"""Tests for the shadow-venv wheel update engine.

The engine's whole value is what it REFUSES: an unsigned or tampered manifest,
a wheel whose digest is not the signed one, a promotion over a non-symlink,
pruning a tree something might be running from. Each refusal is pinned here,
plus the one cross-file invariant nothing else checks — the trust root must be
byte-identical to cli.sh's copy, or the two verifiers drift apart silently.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.platform import wheel_engine
from kiro_crew.platform.wheel_engine import (
    ManagedVenvLayout,
    WheelUpdateError,
    managed_venv_layout,
    parse_and_validate_manifest,
    promote,
    respawn_executable,
    running_from_managed_venv,
)
from kiro_crew.platform_compat import IS_POSIX, trusted_system_bin

# The engine is POSIX-only by construction: cli.sh is a POSIX installer, and
# running_from_managed_venv() answers False on Windows, so no production path
# reaches this module there. The tests lean on POSIX semantics throughout —
# atomic rename over a symlink (WinError 5 on Windows), 0o600 mode bits, and
# venv bin/ layout — so they are skipped as a module rather than papered over
# with per-test shims that would test nothing real.
pytestmark = pytest.mark.skipif(not IS_POSIX, reason="the shadow-venv engine is POSIX-only")

_REPO_ROOT = Path(__file__).resolve().parents[1]

_ARTIFACT_BASE = "https://download.crew.kiro.dev"
_FEED_BASE = "https://updates.crew.kiro.dev"


def _manifest(
    channel: str = "stable",
    version: str = "9.9.9",
    artifact_base: str = _ARTIFACT_BASE,
    **overrides: object,
) -> dict[str, object]:
    wheel_name = f"kirocrew-{version}-py3-none-any.whl"
    manifest: dict[str, object] = {
        "algorithm": "RSASSA_PKCS1_V1_5_SHA_256",
        "channel": channel,
        "key_id": wheel_engine.CLI_MANIFEST_KEY_ID,
        "pub_date": "2026-01-01T00:00:00Z",
        "python_requires": ">=3.10",
        "schema": "kirocrew-cli-artifact-manifest-v1",
        "sha256": "a" * 64,
        "signature": base64.b64encode(b"not-a-real-signature").decode(),
        "version": version,
        "wheel_url": f"{artifact_base}/cli/{channel}/{version}/{wheel_name}",
    }
    manifest.update(overrides)
    return manifest


def _raw(manifest: dict[str, object]) -> bytes:
    return json.dumps(manifest).encode("utf-8")


class TestTrustRootMatchesInstaller:
    """The module's pinned trust root and cli.sh's must be ONE value."""

    def test_key_id_and_pem_match_cli_sh(self) -> None:
        cli_sh = (_REPO_ROOT / "cli.sh").read_text(encoding="utf-8")
        key_id = re.search(r'^CLI_MANIFEST_KEY_ID="([^"]+)"', cli_sh, re.MULTILINE)
        key_b64 = re.search(r'^CLI_MANIFEST_PUBLIC_KEY_B64="([^"]+)"', cli_sh, re.MULTILINE)
        assert key_id is not None and key_b64 is not None, "cli.sh trust root not found"
        assert wheel_engine.CLI_MANIFEST_KEY_ID == key_id.group(1)
        assert wheel_engine.CLI_MANIFEST_PUBLIC_KEY_B64 == key_b64.group(1)

    def test_key_id_is_fingerprint_of_embedded_key(self, tmp_path: Path) -> None:
        """The pair self-checks: SHA-256 over the SPKI DER equals the key id."""
        openssl = trusted_system_bin("openssl")
        if openssl is None:
            pytest.skip("openssl not available in a trusted system directory")
        pem = base64.b64decode(wheel_engine.CLI_MANIFEST_PUBLIC_KEY_B64, validate=True)
        proc = subprocess.run(
            [openssl, "pkey", "-pubin", "-outform", "DER"],
            input=pem,
            capture_output=True,
            timeout=30,
            # Rule 1c: a child inherits pytest's CWD (the repo root); pin it
            # under tmp_path so nothing a spawn creates can land in the checkout.
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr.decode(errors="replace")
        fingerprint = "sha256:" + hashlib.sha256(proc.stdout).hexdigest()
        assert fingerprint == wheel_engine.CLI_MANIFEST_KEY_ID


class TestManifestValidation:
    def test_valid_manifest_passes(self) -> None:
        payload, canonical, signature = parse_and_validate_manifest(
            _raw(_manifest()), channel="stable", artifact_base=_ARTIFACT_BASE
        )
        assert payload["version"] == "9.9.9"
        assert b'"signature"' not in canonical
        assert signature == b"not-a-real-signature"
        # Canonical form is the exact byte layout cli.sh signs: sorted keys,
        # compact separators, trailing newline, ASCII.
        assert canonical.endswith(b"\n")
        assert json.loads(canonical)["version"] == "9.9.9"

    def test_duplicate_key_refused(self) -> None:
        body = _raw(_manifest()).decode()
        dup = body[:-1] + ',"version":"9.9.9"}'
        with pytest.raises(WheelUpdateError, match="not valid JSON"):
            parse_and_validate_manifest(
                dup.encode(), channel="stable", artifact_base=_ARTIFACT_BASE
            )

    @pytest.mark.parametrize(
        "mutation",
        [
            {"schema": "something-else"},
            {"algorithm": "none"},
            {"key_id": "sha256:" + "0" * 64},
            {"channel": "insider"},
            {"version": "../evil"},
            {"sha256": "zz"},
            {"pub_date": "yesterday"},
            {"python_requires": "x" * 200},
            {"signature": "%%%not-base64%%%"},
            {"wheel_url": f"{_ARTIFACT_BASE}/cli/stable/9.9.9/other.whl"},
            {"wheel_url": "https://evil.example/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl"},
        ],
    )
    def test_field_tampering_refused(self, mutation: dict[str, object]) -> None:
        with pytest.raises(WheelUpdateError):
            parse_and_validate_manifest(
                _raw(_manifest(**mutation)), channel="stable", artifact_base=_ARTIFACT_BASE
            )

    def test_missing_and_extra_fields_refused(self) -> None:
        short = _manifest()
        short.pop("pub_date")
        with pytest.raises(WheelUpdateError, match="unexpected fields"):
            parse_and_validate_manifest(_raw(short), channel="stable", artifact_base=_ARTIFACT_BASE)
        long = _manifest()
        long["extra"] = "x"
        with pytest.raises(WheelUpdateError, match="unexpected fields"):
            parse_and_validate_manifest(_raw(long), channel="stable", artifact_base=_ARTIFACT_BASE)

    def test_optional_min_version_accepted(self) -> None:
        """A signed fleet floor is tolerated, mirroring cli.sh's optional set.

        The feed publishes ``min_version`` when a breaking release sets a
        floor; refusing it would abort every CLI and in-app update the moment
        the floor ships.
        """
        m = _manifest()
        m["min_version"] = "0.4.0"
        payload, _, _ = parse_and_validate_manifest(
            _raw(m), channel="stable", artifact_base=_ARTIFACT_BASE
        )
        assert payload["min_version"] == "0.4.0"

    def test_bad_min_version_refused(self) -> None:
        m = _manifest()
        m["min_version"] = "../evil"
        with pytest.raises(WheelUpdateError, match="min_version"):
            parse_and_validate_manifest(_raw(m), channel="stable", artifact_base=_ARTIFACT_BASE)

    def test_min_version_plus_extra_field_still_refused(self) -> None:
        """The optional set tolerates exactly min_version, nothing else."""
        m = _manifest()
        m["min_version"] = "0.4.0"
        m["extra"] = "x"
        with pytest.raises(WheelUpdateError, match="unexpected fields"):
            parse_and_validate_manifest(_raw(m), channel="stable", artifact_base=_ARTIFACT_BASE)

    def test_non_string_value_refused(self) -> None:
        with pytest.raises(WheelUpdateError, match="invalid field type"):
            parse_and_validate_manifest(
                _raw(_manifest(version=123)),  # type: ignore[arg-type]
                channel="stable",
                artifact_base=_ARTIFACT_BASE,
            )

    def test_oversized_manifest_refused(self) -> None:
        with pytest.raises(WheelUpdateError, match="size ceiling"):
            parse_and_validate_manifest(
                b" " * (wheel_engine._MANIFEST_MAX_BYTES + 1),
                channel="stable",
                artifact_base=_ARTIFACT_BASE,
            )


class TestSignatureVerification:
    """Round-trip against a throwaway RSA key, constants monkeypatched."""

    @pytest.fixture()
    def keypair(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        openssl = trusted_system_bin("openssl")
        if openssl is None:
            pytest.skip("openssl not available in a trusted system directory")
        priv = tmp_path / "priv.pem"
        pub = tmp_path / "pub.pem"
        der = tmp_path / "pub.der"
        subprocess.run(
            [
                openssl,
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(priv),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            cwd=str(tmp_path),
        )
        subprocess.run(
            [openssl, "pkey", "-in", str(priv), "-pubout", "-out", str(pub)],
            check=True,
            capture_output=True,
            timeout=30,
            cwd=str(tmp_path),
        )
        subprocess.run(
            [openssl, "pkey", "-pubin", "-in", str(pub), "-outform", "DER", "-out", str(der)],
            check=True,
            capture_output=True,
            timeout=30,
            cwd=str(tmp_path),
        )
        monkeypatch.setattr(
            wheel_engine,
            "CLI_MANIFEST_PUBLIC_KEY_B64",
            base64.b64encode(pub.read_bytes()).decode(),
        )
        monkeypatch.setattr(
            wheel_engine,
            "CLI_MANIFEST_KEY_ID",
            "sha256:" + hashlib.sha256(der.read_bytes()).hexdigest(),
        )
        return priv

    def _sign(self, priv: Path, payload: bytes, tmp_path: Path) -> bytes:
        openssl = trusted_system_bin("openssl")
        assert openssl is not None
        doc = tmp_path / "payload.bin"
        sig = tmp_path / "payload.sig"
        doc.write_bytes(payload)
        subprocess.run(
            [openssl, "dgst", "-sha256", "-sign", str(priv), "-out", str(sig), str(doc)],
            check=True,
            capture_output=True,
            timeout=30,
            cwd=str(tmp_path),
        )
        return sig.read_bytes()

    def test_valid_signature_accepted(self, keypair: Path, tmp_path: Path) -> None:
        canonical = b'{"v":"1"}\n'
        signature = self._sign(keypair, canonical, tmp_path)
        workdir = tmp_path / "work"
        workdir.mkdir()
        wheel_engine._verify_signature(canonical, signature, workdir)

    def test_tampered_payload_refused(self, keypair: Path, tmp_path: Path) -> None:
        signature = self._sign(keypair, b'{"v":"1"}\n', tmp_path)
        workdir = tmp_path / "work"
        workdir.mkdir()
        with pytest.raises(WheelUpdateError, match="signature verification failed"):
            wheel_engine._verify_signature(b'{"v":"2"}\n', signature, workdir)

    def test_wrong_pinned_fingerprint_refused(
        self, keypair: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        canonical = b'{"v":"1"}\n'
        signature = self._sign(keypair, canonical, tmp_path)
        monkeypatch.setattr(wheel_engine, "CLI_MANIFEST_KEY_ID", "sha256:" + "0" * 64)
        workdir = tmp_path / "work"
        workdir.mkdir()
        with pytest.raises(WheelUpdateError, match="fingerprint mismatch"):
            wheel_engine._verify_signature(canonical, signature, workdir)


class TestWheelDownload:
    class _FakeResponse:
        """Chunk-serving stand-in for the urlopen response."""

        def __init__(self, body: bytes, chunk: int = 7) -> None:
            self._view = memoryview(body)
            self._pos = 0
            self._chunk = chunk

        def read(self, n: int) -> bytes:
            take = min(self._chunk, n, len(self._view) - self._pos)
            out = bytes(self._view[self._pos : self._pos + take])
            self._pos += take
            return out

        def __enter__(self) -> "TestWheelDownload._FakeResponse":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def _serve(self, monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
        monkeypatch.setattr(
            wheel_engine.urllib.request,
            "urlopen",
            lambda req, timeout: self._FakeResponse(body),
        )

    def test_streamed_download_verifies_incrementally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b"wheel-bytes" * 100
        self._serve(monkeypatch, body)
        payload = {
            "wheel_url": f"{_ARTIFACT_BASE}/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl",
            "sha256": hashlib.sha256(body).hexdigest(),
            "version": "9.9.9",
        }
        out = wheel_engine.download_verified_wheel(payload, tmp_path)
        assert out.read_bytes() == body

    def test_sha_mismatch_refused_and_file_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._serve(monkeypatch, b"tampered")
        payload = {
            "wheel_url": f"{_ARTIFACT_BASE}/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl",
            "sha256": hashlib.sha256(b"expected").hexdigest(),
            "version": "9.9.9",
        }
        with pytest.raises(WheelUpdateError, match="SHA-256 mismatch"):
            wheel_engine.download_verified_wheel(payload, tmp_path)
        assert not list(tmp_path.iterdir()), "no wheel may survive a digest mismatch"

    def test_cap_enforced_against_received_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b"x" * 4096
        self._serve(monkeypatch, body)
        with pytest.raises(WheelUpdateError, match="ceiling"):
            wheel_engine._download_to_file(
                f"{_ARTIFACT_BASE}/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl",
                tmp_path / "w.whl",
                cap=1024,
                timeout=5,
                expected_sha="0" * 64,
            )
        assert not (tmp_path / "w.whl").exists(), "an over-cap partial must be removed"

    def test_disk_write_failure_is_operator_facing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENOSPC mid-stream narrates, never tracebacks."""
        self._serve(monkeypatch, b"wheel-bytes")

        import builtins

        real_open = builtins.open

        class _FullDisk:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            def write(self, data: bytes) -> int:
                raise OSError(28, "No space left on device")

            def __enter__(self) -> "_FullDisk":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_open(path: object, mode: str = "r", *a: object, **k: object):
            if str(path).endswith(".whl") and "wb" in mode:
                return _FullDisk()
            return real_open(path, mode, *a, **k)  # type: ignore[call-overload]

        monkeypatch.setattr(builtins, "open", fake_open)
        with pytest.raises(WheelUpdateError, match="could not fetch"):
            wheel_engine._download_to_file(
                f"{_ARTIFACT_BASE}/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl",
                tmp_path / "w.whl",
                cap=1 << 20,
                timeout=5,
                expected_sha="0" * 64,
            )

    def test_http_url_refused(self) -> None:
        with pytest.raises(WheelUpdateError, match="non-HTTPS"):
            wheel_engine._fetch_bytes("http://download.crew.kiro.dev/x", 10, 1)
        with pytest.raises(WheelUpdateError, match="non-HTTPS"):
            wheel_engine._download_to_file(
                "http://download.crew.kiro.dev/x", Path("/dev/null"), 10, 1, "0" * 64
            )


class TestFetchBytes:
    def test_success_and_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Resp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self, n: int) -> bytes:
                return self._body[:n]

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(
            wheel_engine.urllib.request, "urlopen", lambda req, timeout: _Resp(b"ok")
        )
        assert wheel_engine._fetch_bytes("https://x.example/f", 10, 1) == b"ok"
        monkeypatch.setattr(
            wheel_engine.urllib.request, "urlopen", lambda req, timeout: _Resp(b"x" * 20)
        )
        with pytest.raises(WheelUpdateError, match="ceiling"):
            wheel_engine._fetch_bytes("https://x.example/f", 10, 1)

    def test_network_error_is_operator_facing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error as _ue

        def raising(req: object, timeout: float) -> object:
            raise _ue.URLError("boom")

        monkeypatch.setattr(wheel_engine.urllib.request, "urlopen", raising)
        with pytest.raises(WheelUpdateError, match="could not fetch"):
            wheel_engine._fetch_bytes("https://x.example/f", 10, 1)


class TestRunHelper:
    def test_nonzero_exit_carries_stderr_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _P:
            returncode = 3
            stderr = b"broken pipe"

        monkeypatch.setattr(wheel_engine.subprocess, "run", lambda *a, **k: _P())
        with pytest.raises(WheelUpdateError, match="exited 3.*broken pipe"):
            wheel_engine._run(["x"], 5, "step-x")

    def test_timeout_is_operator_facing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(*a: object, **k: object) -> object:
            raise wheel_engine.subprocess.TimeoutExpired(cmd="x", timeout=5)

        monkeypatch.setattr(wheel_engine.subprocess, "run", raising)
        with pytest.raises(WheelUpdateError, match="timed out"):
            wheel_engine._run(["x"], 5, "step-x")

    def test_missing_binary_is_operator_facing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising(*a: object, **k: object) -> object:
            raise OSError("no such file")

        monkeypatch.setattr(wheel_engine.subprocess, "run", raising)
        with pytest.raises(WheelUpdateError, match="could not run"):
            wheel_engine._run(["x"], 5, "step-x")


class TestBuildGuards2:
    def test_stable_target_refused_before_sentinel_check(self, tmp_path: Path) -> None:
        """The promoted tree is refused even while it still carries a sentinel."""
        live = tmp_path / "crew-venv-9.9.9"
        live.mkdir()
        (live / "pyvenv.cfg").write_text("")
        (live / wheel_engine._SHADOW_SENTINEL).write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(live)
        with pytest.raises(WheelUpdateError, match="already promoted"):
            wheel_engine.build_shadow_venv(tmp_path / "w.whl", live, stable_link=stable)
        assert live.exists()

    def test_unclaimable_shadow_dir_is_operator_facing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "crew-venv-9.9.9"

        real_write = Path.write_text

        def failing_write(self: Path, *a: object, **k: object) -> int:
            if self.name == wheel_engine._SHADOW_SENTINEL:
                raise OSError(13, "denied")
            return real_write(self, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", failing_write)
        with pytest.raises(WheelUpdateError, match="could not claim"):
            wheel_engine.build_shadow_venv(tmp_path / "w.whl", target)


class TestManifestFetchOrchestration:
    def test_fetch_verified_manifest_wires_fetch_parse_verify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        raw = _raw(_manifest())
        monkeypatch.setattr(
            wheel_engine, "_fetch_bytes", lambda url, cap, timeout: (calls.append("fetch"), raw)[1]
        )
        monkeypatch.setattr(
            wheel_engine,
            "_verify_signature",
            lambda canonical, signature, workdir: calls.append("verify"),
        )
        payload = wheel_engine.fetch_verified_manifest(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            workdir=tmp_path,
        )
        assert calls == ["fetch", "verify"]
        assert payload["version"] == "9.9.9"


class TestPromotion:
    def test_promote_creates_stable_link(self, tmp_path: Path) -> None:
        tree = tmp_path / "crew-venv-1.0.0"
        tree.mkdir()
        stable = tmp_path / "crew-venv-current"
        promote(tree, stable)
        assert stable.is_symlink()
        assert stable.resolve() == tree.resolve()

    def test_promote_replaces_existing_link(self, tmp_path: Path) -> None:
        old = tmp_path / "crew-venv-1.0.0"
        new = tmp_path / "crew-venv-2.0.0"
        old.mkdir()
        new.mkdir()
        stable = tmp_path / "crew-venv-current"
        promote(old, stable)
        promote(new, stable)
        assert stable.resolve() == new.resolve()
        # No temp link litter after either promotion.
        leftovers = [p for p in tmp_path.iterdir() if ".new" in p.name]
        assert leftovers == []

    def test_promote_refuses_real_directory_at_stable_name(self, tmp_path: Path) -> None:
        tree = tmp_path / "crew-venv-1.0.0"
        tree.mkdir()
        stable = tmp_path / "crew-venv-current"
        stable.mkdir()  # corrupt state: the stable name must always be a symlink
        with pytest.raises(WheelUpdateError, match="not a symlink"):
            promote(tree, stable)


class TestLayoutAndDetection:
    def test_layout_honours_venv_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_VENV", str(tmp_path / "custom-venv"))
        layout = managed_venv_layout()
        assert layout.legacy == tmp_path / "custom-venv"
        assert layout.stable_link == tmp_path / "custom-venv-current"

    def test_layout_defaults_beside_data_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_VENV", raising=False)
        layout = managed_venv_layout()
        from kiro_crew.config.paths import data_home

        assert layout.legacy == Path(f"{str(data_home()).rstrip('/')}-venv")

    def test_running_from_legacy_tree_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        exe = legacy / "bin" / "python3"
        exe.write_text("")
        layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is True

    def test_running_from_versioned_tree_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tree = tmp_path / "crew-venv-1.2.3"
        (tree / "bin").mkdir(parents=True)
        exe = tree / "bin" / "python3"
        exe.write_text("")
        # Every real versioned install carries the console script; the
        # positive-identification rule keys on it.
        (tree / "bin" / "kirocrew").write_text("")
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is True

    def test_prefix_named_foreign_venv_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """crew-venv-dev with no engine artifacts is someone else's venv."""
        tree = tmp_path / "crew-venv-dev"
        (tree / "bin").mkdir(parents=True)
        exe = tree / "bin" / "python3"
        exe.write_text("")
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is False

    def test_foreign_interpreter_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        other = tmp_path / "somewhere-else" / "bin"
        other.mkdir(parents=True)
        exe = other / "python3"
        exe.write_text("")
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is False


class TestRespawnExecutable:
    def test_non_managed_install_answers_sys_executable(self) -> None:
        # The test process runs from a dev venv, never a managed tree.
        assert respawn_executable() == sys.executable

    def test_managed_install_routes_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        old_exe = legacy / "bin" / "python3"
        old_exe.write_text("")
        new_tree = tmp_path / "crew-venv-2.0.0"
        (new_tree / "bin").mkdir(parents=True)
        new_exe = new_tree / "bin" / "python3"
        new_exe.write_text("")
        new_exe.chmod(0o755)
        # A genuinely promoted tree always carries the console script; the
        # positive-identification rule keys on it.
        (new_tree / "bin" / "kirocrew").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(new_tree)

        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(old_exe))
        assert respawn_executable() == str(stable / "bin" / "python3")

    def test_link_repointed_outside_managed_trees_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stable link aimed outside the layout must never become the exec target."""
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        exe = legacy / "bin" / "python3"
        exe.write_text("")
        outside = tmp_path / "not-ours"
        (outside / "bin").mkdir(parents=True)
        planted = outside / "bin" / "python3"
        planted.write_text("")
        planted.chmod(0o755)
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(outside)

        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(exe))
        assert respawn_executable() == str(
            exe
        ), "a link outside the managed trees must fall back to sys.executable"

    def test_missing_stable_link_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        exe = legacy / "bin" / "python3"
        exe.write_text("")
        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(exe))
        assert respawn_executable() == str(exe)


class TestReexecExecutableParameter:
    def test_reexec_uses_supplied_executable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import platform_compat

        # The real call mutates os.environ (UTF-8 pinning) before exec; with
        # execv mocked the process KEEPS RUNNING, so the mutation would leak
        # into every later test on this worker. The env step is not what these
        # tests assert, so it is stubbed rather than let loose.
        monkeypatch.setattr(platform_compat, "_ensure_utf8_process_environment", lambda: None)

        captured: dict[str, object] = {}

        def fake_execv(path: str, argv: list[str]) -> None:
            captured["path"] = path
            captured["argv"] = argv

        monkeypatch.setattr(os, "execv", fake_execv)
        platform_compat.reexec_python_module("kiro_crew", ["--flag"], executable="/x/bin/python3")
        assert captured["path"] == "/x/bin/python3"
        argv = captured["argv"]
        assert isinstance(argv, list)
        assert argv[1:3] == ["-m", "kiro_crew"]
        assert argv[3:] == ["--flag"]

    def test_reexec_defaults_to_sys_executable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "_ensure_utf8_process_environment", lambda: None)
        captured: dict[str, object] = {}
        monkeypatch.setattr(os, "execv", lambda p, a: captured.update(path=p))
        platform_compat.reexec_python_module("kiro_crew", [])
        assert captured["path"] == sys.executable


class TestLauncherRepoint:
    def _layout(self, tmp_path: Path) -> ManagedVenvLayout:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        (legacy / "bin" / "kirocrew").write_text("")
        return ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")

    def test_repoints_managed_launcher_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout = self._layout(tmp_path)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        launcher = home / ".local" / "bin" / "kirocrew"
        launcher.symlink_to(layout.legacy / "bin" / "kirocrew")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert wheel_engine.repoint_launcher_symlink(layout) is True
        assert os.readlink(launcher) == str(layout.stable_link / "bin" / "kirocrew")

    def test_leaves_foreign_launcher_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout = self._layout(tmp_path)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        foreign = tmp_path / "pipx-venv" / "bin"
        foreign.mkdir(parents=True)
        (foreign / "kirocrew").write_text("")
        launcher = home / ".local" / "bin" / "kirocrew"
        launcher.symlink_to(foreign / "kirocrew")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert wheel_engine.repoint_launcher_symlink(layout) is False
        assert os.readlink(launcher) == str(foreign / "kirocrew")

    def test_regular_file_launcher_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout = self._layout(tmp_path)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        launcher = home / ".local" / "bin" / "kirocrew"
        launcher.write_text("#!/bin/sh\n")  # an operator's wrapper script
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert wheel_engine.repoint_launcher_symlink(layout) is False
        assert launcher.is_file() and not launcher.is_symlink()


class TestShadowBuildGuards:
    def test_refuses_symlink_at_shadow_path(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.mkdir()
        shadow = tmp_path / "crew-venv-9.9.9"
        shadow.symlink_to(target)
        with pytest.raises(WheelUpdateError, match="not a plain directory"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", shadow)

    def test_refuses_to_remove_a_non_venv_directory(self, tmp_path: Path) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        shadow.mkdir()
        (shadow / "user-data.txt").write_text("precious")
        with pytest.raises(WheelUpdateError, match="not a virtual"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", shadow)
        assert (shadow / "user-data.txt").exists()

    def test_refuses_to_rebuild_the_promoted_tree(self, tmp_path: Path) -> None:
        """A directory that IS the stable link's target is promoted, not leftover."""
        live = tmp_path / "crew-venv-9.9.9"
        live.mkdir()
        (live / "pyvenv.cfg").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(live)
        with pytest.raises(WheelUpdateError, match="already promoted"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", live, stable_link=stable)
        assert live.exists(), "the live tree must never be removed"
        assert stable.resolve() == live.resolve()

    def test_refuses_a_completed_tree_without_sentinel(self, tmp_path: Path) -> None:
        """No sentinel = completed or foreign; might serve a gateway. Refused."""
        tree = tmp_path / "crew-venv-9.9.9"
        (tree / "bin").mkdir(parents=True)
        (tree / "pyvenv.cfg").write_text("")
        with pytest.raises(WheelUpdateError, match="refusing to remove"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", tree)
        assert tree.exists(), "a sentinel-less tree must never be removed"

    def test_refuses_an_unrelated_sibling_venv(self, tmp_path: Path) -> None:
        """A custom KIROCREW_VENV shares a parent with unrelated venvs; a name
        collision must never delete someone else's environment."""
        foreign = tmp_path / "crew-venv-9.9.9"
        (foreign / "bin").mkdir(parents=True)
        (foreign / "pyvenv.cfg").write_text("")
        (foreign / "precious-data.txt").write_text("not ours")
        with pytest.raises(WheelUpdateError, match="refusing to remove"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", foreign)
        assert (foreign / "precious-data.txt").exists()

    def test_own_incomplete_debris_is_clearable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sentinel present = our own interrupted build; a retry clears it."""
        tree = tmp_path / "crew-venv-9.9.9"
        tree.mkdir()
        (tree / "pyvenv.cfg").write_text("")
        (tree / wheel_engine._SHADOW_SENTINEL).write_text("")
        calls: list[str] = []
        monkeypatch.setattr(
            wheel_engine, "_run", lambda argv, timeout, step, cwd=None: calls.append(step)
        )
        monkeypatch.setattr(
            wheel_engine.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})()
        )
        wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", tree)
        assert "venv creation" in calls, "the retry must rebuild after clearing debris"
        assert (
            tree / wheel_engine._SHADOW_SENTINEL
        ).exists(), "a fresh build must re-claim ownership until verification passes"

    def test_refuses_when_disk_space_is_low(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import shutil as _shutil

        usage = _shutil.disk_usage(tmp_path)
        monkeypatch.setattr(
            wheel_engine.shutil,
            "disk_usage",
            lambda p: type(usage)(usage.total, usage.total - 1024, 1024),
        )
        with pytest.raises(WheelUpdateError, match="disk space"):
            wheel_engine.build_shadow_venv(tmp_path / "kirocrew.whl", tmp_path / "crew-venv-9.9.9")


class TestShadowVerification:
    def _stub_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        returncode: int = 0,
        stdout: str = "9.9.9\n",
        stderr: str = "",
    ) -> None:
        class _Proc:
            def __init__(self) -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        monkeypatch.setattr(wheel_engine.subprocess, "run", lambda *a, **k: _Proc())

    def test_version_match_with_console_script_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        (shadow / "bin" / "kirocrew").write_text("")
        self._stub_probe(monkeypatch)
        wheel_engine.verify_shadow_venv(shadow, "9.9.9")

    def test_version_mismatch_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        (shadow / "bin" / "kirocrew").write_text("")
        self._stub_probe(monkeypatch, stdout="1.0.0\n")
        with pytest.raises(WheelUpdateError, match="not promoting"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9")

    def test_import_failure_refused(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        self._stub_probe(monkeypatch, returncode=1, stdout="", stderr="ImportError: boom")
        with pytest.raises(WheelUpdateError, match="cannot import"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9")

    def test_missing_console_script_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        shadow = tmp_path / "crew-venv-9.9.9"
        (shadow / "bin").mkdir(parents=True)
        self._stub_probe(monkeypatch)
        with pytest.raises(WheelUpdateError, match="console script"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9")


class TestApplyWheelUpdateOrchestration:
    """The full flow with the heavy steps stubbed: ordering and refusal seams."""

    def _wire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, feed_version: str = "9.9.9"
    ) -> tuple[ManagedVenvLayout, list[str]]:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")
        monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)

        calls: list[str] = []

        def fake_manifest(**kwargs: object) -> dict[str, str]:
            calls.append("manifest")
            return {
                "version": feed_version,
                "sha256": "a" * 64,
                "wheel_url": f"{_ARTIFACT_BASE}/cli/stable/{feed_version}/"
                f"kirocrew-{feed_version}-py3-none-any.whl",
            }

        def fake_download(payload: dict[str, str], dest: Path) -> Path:
            calls.append("download")
            out = dest / "kirocrew.whl"
            out.write_bytes(b"wheel")
            return out

        def fake_build(wheel: Path, shadow: Path, stable_link: Path | None = None) -> None:
            calls.append("build")
            (shadow / "bin").mkdir(parents=True)
            (shadow / "pyvenv.cfg").write_text("")
            (shadow / wheel_engine._SHADOW_SENTINEL).write_text("")

        def fake_verify(shadow: Path, version: str) -> None:
            calls.append("verify")

        monkeypatch.setattr(wheel_engine, "fetch_verified_manifest", fake_manifest)
        monkeypatch.setattr(wheel_engine, "download_verified_wheel", fake_download)
        monkeypatch.setattr(wheel_engine, "build_shadow_venv", fake_build)
        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", fake_verify)
        monkeypatch.setattr(wheel_engine, "repoint_launcher_symlink", lambda layout: True)
        return layout, calls

    def test_happy_path_promotes_in_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path)
        promoted = wheel_engine.apply_wheel_update(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            expected_version="9.9.9",
        )
        assert calls == ["manifest", "download", "build", "verify"]
        assert promoted == layout.versioned_tree("9.9.9")
        assert layout.stable_link.resolve() == promoted.resolve()
        assert not (
            promoted / wheel_engine._SHADOW_SENTINEL
        ).exists(), "a promoted tree must never carry the incomplete sentinel"

    def test_already_promoted_completes_launcher_handoff_without_rebuilding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A handoff interrupted after promote but before launcher-repoint must
        recover: the stable link already targets this version's tree, so a
        rebuild would hit build_shadow_venv's stable-target refusal and strand
        the launcher on the old venv forever. Instead, finish the one remaining
        step (repoint) and return."""
        layout, calls = self._wire(monkeypatch, tmp_path)
        # Simulate the crashed-after-promote state: the versioned tree exists
        # and the stable link already points at it, but the launcher was never
        # repointed.
        promoted = layout.versioned_tree("9.9.9")
        (promoted / "bin").mkdir(parents=True)
        (promoted / "pyvenv.cfg").write_text("")
        os.symlink(str(promoted.resolve()), str(layout.stable_link))
        repointed: list[bool] = []
        monkeypatch.setattr(
            wheel_engine, "repoint_launcher_symlink", lambda layout: repointed.append(True) or True
        )

        result = wheel_engine.apply_wheel_update(
            channel="stable",
            feed_base=_FEED_BASE,
            artifact_base=_ARTIFACT_BASE,
            expected_version="9.9.9",
        )
        assert result == promoted
        assert repointed == [True], "the launcher handoff must be completed"
        assert calls == [], "recovery must NOT re-fetch, re-download, or rebuild"

    def test_feed_moving_between_check_and_apply_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path, feed_version="10.0.0")
        with pytest.raises(WheelUpdateError, match="re-run the update"):
            wheel_engine.apply_wheel_update(
                channel="stable",
                feed_base=_FEED_BASE,
                artifact_base=_ARTIFACT_BASE,
                expected_version="9.9.9",
            )
        assert "download" not in calls, "nothing may download once the verdict is stale"
        assert not layout.stable_link.exists(), "a refused update must not touch the stable link"

    def test_second_concurrent_update_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The update lock admits ONE writer; a second run refuses cleanly."""
        import os as _os

        from kiro_crew.platform_compat import try_acquire_lock as _try

        layout, calls = self._wire(monkeypatch, tmp_path)
        lock_path = layout.stable_link.with_name(f"{layout.legacy.name}.update.lock")
        holder = _os.open(str(lock_path), _os.O_CREAT | _os.O_RDWR, 0o600)
        try:
            assert _try(holder, exclusive=True)
            with pytest.raises(WheelUpdateError, match="already in progress"):
                wheel_engine.apply_wheel_update(
                    channel="stable",
                    feed_base=_FEED_BASE,
                    artifact_base=_ARTIFACT_BASE,
                    expected_version="9.9.9",
                )
            assert calls == [], "a refused run must do no work at all"
        finally:
            _os.close(holder)

    def test_failed_verification_never_promotes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout, calls = self._wire(monkeypatch, tmp_path)

        def failing_verify(shadow: Path, version: str) -> None:
            raise WheelUpdateError("shadow venv cannot import kiro_crew — not promoting")

        monkeypatch.setattr(wheel_engine, "verify_shadow_venv", failing_verify)
        with pytest.raises(WheelUpdateError, match="not promoting"):
            wheel_engine.apply_wheel_update(
                channel="stable",
                feed_base=_FEED_BASE,
                artifact_base=_ARTIFACT_BASE,
                expected_version="9.9.9",
            )
        assert not layout.stable_link.exists(), "a failed verification must not promote"
