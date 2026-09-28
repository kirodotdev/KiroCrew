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
import platform
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


class TestBuildUmask:
    def test_build_runs_children_under_the_build_umask(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """venv creation, the pip refresh, and the wheel install all get the owner-only
        build umask via subprocess's umask= (thread-safe, no preexec_fn), so bin/ is
        born non-group-writable."""
        seen: list[object] = []

        def fake_subprocess_run(*a: object, **k: object):  # type: ignore[no-untyped-def]
            seen.append(k.get("umask"))
            return type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

        monkeypatch.setattr(wheel_engine.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(wheel_engine, "_BUILD_UMASK", 0o077)

        wheel_engine.build_shadow_venv(tmp_path / "w.whl", tmp_path / "crew-venv-1.0.0")

        assert seen == [0o077, 0o077, 0o077]

    def test_build_creates_owner_only_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The shadow root is born 0o700 -- not group-writable even under umask 002."""
        monkeypatch.setattr(wheel_engine, "_run", lambda *a, **k: None)
        monkeypatch.setattr(
            wheel_engine.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})()
        )
        shadow = tmp_path / "crew-venv-1.0.0"
        saved = os.umask(0o002)
        try:
            wheel_engine.build_shadow_venv(tmp_path / "w.whl", shadow)
        finally:
            os.umask(saved)
        assert shadow.stat().st_mode & 0o077 == 0, oct(shadow.stat().st_mode)

    def test_real_venv_dirs_born_non_group_writable_under_build_umask(self, tmp_path: Path) -> None:
        """End-to-end: a real venv built with umask=0o077 under a umask-002 shell has
        a non-group/world-writable root and bin/. Those are the components the AppArmor
        profile walks (the launcher path + its ancestor dirs); venv's own activation
        scripts are siblings the profile never inspects, so they are not asserted."""
        target = tmp_path / "v"
        saved = os.umask(0o002)
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(target)],
                check=True,
                capture_output=True,
                cwd=str(tmp_path),
                umask=0o077,
            )
        finally:
            os.umask(saved)
        for path in (target, target / "bin"):
            assert not path.stat().st_mode & 0o022, (path, oct(path.stat().st_mode))


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

    def test_running_from_legacy_tree_detects_symlinked_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        base_python = tmp_path / "base-python" / "python3"
        base_python.parent.mkdir()
        base_python.write_text("")
        exe = legacy / "bin" / "python3"
        exe.symlink_to(base_python)
        layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is True

    def test_running_from_versioned_tree_detects_symlinked_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tree = tmp_path / "crew-venv-1.2.3"
        (tree / "bin").mkdir(parents=True)
        base_python = tmp_path / "base-python" / "python3"
        base_python.parent.mkdir()
        base_python.write_text("")
        exe = tree / "bin" / "python3"
        exe.symlink_to(base_python)
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

    def test_symlinked_interpreter_still_routes_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A real venv's bin/python3 is a symlink to the base interpreter.

        Identity must come from the tree the link lives in, not from where it
        points: resolving the file lands outside every venv and would make
        every real managed install answer plain sys.executable.
        """
        base = tmp_path / "base-python" / "bin"
        base.mkdir(parents=True)
        base_exe = base / "python3.12"
        base_exe.write_text("")
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        old_exe = legacy / "bin" / "python3"
        old_exe.symlink_to(base_exe)
        new_tree = tmp_path / "crew-venv-2.0.0"
        (new_tree / "bin").mkdir(parents=True)
        new_exe = new_tree / "bin" / "python3"
        new_exe.write_text("")
        new_exe.chmod(0o755)
        (new_tree / "bin" / "kirocrew").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(new_tree)

        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(old_exe))
        assert respawn_executable() == str(stable / "bin" / "python3")

    def _nested_venv_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """A data home whose ``venv/`` is the retired in-data-home install."""
        home = tmp_path / "crew"
        nested = home / "venv"
        (nested / "bin").mkdir(parents=True)
        (nested / "bin" / "python3").write_text("")
        (nested / "bin" / "kirocrew").write_text("")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.delenv("KIROCREW_VENV", raising=False)
        monkeypatch.setattr(sys, "executable", str(nested / "bin" / "python3"))
        return home

    def test_retired_nested_venv_routes_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The installer re-run cli.sh performs on migration: it builds the
        new tree BESIDE the data home, repoints the stable link at it, then
        ``rm -rf``s the in-data-home venv this process is running from. The
        restart must exec the stable link's interpreter — sys.executable no
        longer exists."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        new_tree = tmp_path / "crew-venv"
        (new_tree / "bin").mkdir(parents=True)
        new_exe = new_tree / "bin" / "python3"
        new_exe.write_text("")
        new_exe.chmod(0o755)
        (new_tree / "bin" / "kirocrew").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(new_tree)
        shutil.rmtree(home / "venv")

        assert not Path(sys.executable).exists()
        assert respawn_executable() == str(stable / "bin" / "python3")

    def test_nested_venv_before_migration_keeps_sys_executable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No stable link yet (the installer has not been re-run): the nested
        venv is the only interpreter, and the restart stays on it."""
        home = self._nested_venv_home(monkeypatch, tmp_path)
        assert respawn_executable() == str(home / "venv" / "bin" / "python3")

    def _migrated_legacy_tree(self, tmp_path: Path) -> Path:
        """The tree cli.sh's re-run builds beside the data home and verifies."""
        new_tree = tmp_path / "crew-venv"
        (new_tree / "bin").mkdir(parents=True)
        exe = new_tree / "bin" / "python3"
        exe.write_text("")
        exe.chmod(0o755)
        (new_tree / "bin" / "kirocrew").write_text("")
        return new_tree

    def test_unusable_stable_link_after_migration_uses_legacy_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A real directory at the stable name makes cli.sh SKIP the repoint
        (its guard only writes a symlink or an absent path), but the nested-venv
        delete is gated on the new tree's own import check, so it still runs.
        The cached interpreter is then gone and the stable link cannot be
        trusted — the restart must take the import-verified legacy tree rather
        than exec a path that does not exist."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        new_tree = self._migrated_legacy_tree(tmp_path)
        (tmp_path / "crew-venv-current").mkdir()  # corrupt: a directory, not a link
        shutil.rmtree(home / "venv")

        assert not Path(sys.executable).exists()
        assert respawn_executable() == str(new_tree / "bin" / "python3")

    def test_failed_stable_link_repoint_after_migration_uses_legacy_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """cli.sh's repoint failure is non-fatal (it warns and continues), so
        the migration can delete the nested venv leaving no stable link at
        all."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        new_tree = self._migrated_legacy_tree(tmp_path)
        shutil.rmtree(home / "venv")

        assert not (tmp_path / "crew-venv-current").exists()
        assert respawn_executable() == str(new_tree / "bin" / "python3")

    def test_deleted_interpreter_with_no_usable_legacy_tree_keeps_sys_executable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The fallback never invents an interpreter: with the legacy tree
        absent too there is nothing to validate, and the answer stays
        ``sys.executable`` exactly as before."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        nested_exe = home / "venv" / "bin" / "python3"
        shutil.rmtree(home / "venv")

        assert not (tmp_path / "crew-venv").exists()
        assert respawn_executable() == str(nested_exe)

    def test_live_interpreter_is_never_displaced_by_the_legacy_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The legacy fallback is reached ONLY when the cached path is gone. A
        process whose own interpreter still exists keeps it, so a corrupt
        stable link cannot silently move a healthy gateway onto another
        tree."""
        home = self._nested_venv_home(monkeypatch, tmp_path)
        self._migrated_legacy_tree(tmp_path)
        (tmp_path / "crew-venv-current").mkdir()

        assert Path(sys.executable).exists()
        assert respawn_executable() == str(home / "venv" / "bin" / "python3")


class TestReexecExecutableParameter:
    def test_reexec_uses_supplied_executable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
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
        assert argv[1:5] == ["-s", "-P", "-m", "kiro_crew"]
        assert argv[5:] == ["--flag"]

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
            wheel_engine,
            "_run",
            lambda argv, timeout, step, cwd=None: calls.append(step),
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


class TestBinaryOnlyDependencies:
    """The shadow install resolves dependencies from prebuilt wheels only.

    A dependency with no wheel for the gateway host must fail the update up
    front, not be compiled from its sdist inside the shadow tree: the host was
    never required to carry a C toolchain, and the policy has to match cli.sh's
    so an install that succeeded and its later update agree on what they will
    accept.
    """

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        seen: list[list[str]] = []

        def fake_subprocess_run(argv, *a: object, **k: object):  # type: ignore[no-untyped-def]
            seen.append(list(argv))
            return type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

        monkeypatch.setattr(wheel_engine.subprocess, "run", fake_subprocess_run)
        return seen

    def test_the_wheel_install_is_binary_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("KIROCREW_ALLOW_SOURCE_BUILDS", raising=False)
        seen = self._capture(monkeypatch)
        wheel = tmp_path / "w.whl"

        wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

        install = [argv for argv in seen if str(wheel) in argv]
        assert len(install) == 1, seen
        argv = install[0]
        # The flag governs THIS resolution, so it sits on the install command,
        # right before the wheel it constrains.
        assert argv[-2:] == ["--only-binary=:all:", str(wheel)], argv
        # The pip self-upgrade is a separate command; the policy is not smeared
        # onto it (a pip wheel always exists, and its failure is already ignored).
        others = [argv for argv in seen if str(wheel) not in argv]
        assert others and all("--only-binary=:all:" not in argv for argv in others), seen

    def test_the_opt_in_restores_the_compile_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_ALLOW_SOURCE_BUILDS", "1")
        seen = self._capture(monkeypatch)
        wheel = tmp_path / "w.whl"

        wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

        install = [argv for argv in seen if str(wheel) in argv]
        assert len(install) == 1, seen
        assert "--only-binary=:all:" not in install[0], install[0]
        assert install[0][-1] == str(wheel)

    @pytest.mark.parametrize("value", ["0", "", "true", "yes"])
    def test_only_the_literal_one_opts_in(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Same contract as cli.sh's `= "1"` test: anything else keeps the policy."""
        monkeypatch.setenv("KIROCREW_ALLOW_SOURCE_BUILDS", value)
        assert wheel_engine._pip_binary_policy() == ["--only-binary=:all:"]

    def test_policy_matches_the_installer(self) -> None:
        """cli.sh and the engine must accept the same wheels, or an install that
        succeeded can be followed by an update that refuses (or compiles)."""
        cli_sh = (_REPO_ROOT / "cli.sh").read_text(encoding="utf-8")
        flag = re.search(r'^PIP_BINARY_ONLY="([^"]+)"$', cli_sh, re.MULTILINE)
        assert flag is not None, "cli.sh no longer declares PIP_BINARY_ONLY"
        assert flag.group(1) == wheel_engine._PIP_BINARY_ONLY
        assert (
            f'"${{{wheel_engine._ALLOW_SOURCE_BUILDS_ENV}:-0}}" = "1"' in cli_sh
        ), "cli.sh's opt-in env var differs from the engine's"

    # pip's tail on a host no candidate wheel runs on, as the report showed it.
    _NO_WHEEL_TAIL = (
        b"ERROR: Could not find a version that satisfies the requirement numpy>=2.0 "
        b"(from kirocrew) (from versions: 2.2.6, 2.3.0)\n"
        b"ERROR: No matching distribution found for numpy>=2.0\n"
        b"ERROR: No matching distribution found for pillow>=10\n"
    )

    @staticmethod
    def _failing_install(monkeypatch: pytest.MonkeyPatch, wheel: Path, stderr: bytes) -> None:
        """Every build child succeeds except the wheel install, which fails with ``stderr``."""

        def fake_subprocess_run(argv, *a: object, **k: object):  # type: ignore[no-untyped-def]
            failing = str(wheel) in argv
            return type(
                "P",
                (),
                {
                    "returncode": 1 if failing else 0,
                    "stdout": b"",
                    "stderr": stderr if failing else b"",
                },
            )()

        monkeypatch.setattr(wheel_engine.subprocess, "run", fake_subprocess_run)

    def test_a_no_wheel_failure_names_the_platform_and_the_opt_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The update refuses in the installer's words, not pip's: which packages
        have no wheel, what host this is, and how to opt into compiling -- including
        that the opt-in has to reach the gateway's own environment (the service
        unit), which is the one fact an install-time success hides."""
        monkeypatch.delenv("KIROCREW_ALLOW_SOURCE_BUILDS", raising=False)
        wheel = tmp_path / "w.whl"
        self._failing_install(monkeypatch, wheel, self._NO_WHEEL_TAIL)

        with pytest.raises(wheel_engine.WheelUpdateError) as info:
            wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

        text = str(info.value)
        assert text.startswith(
            "pip found no prebuilt wheel of numpy>=2.0, pillow>=10 it may install on this host"
        ), text
        assert f"{platform.system()} {platform.machine()}" in text
        # The usual cause is stated as usual, and the other cause sits next to it.
        assert "Usually this means the host is older than the wheels' floor" in text
        assert "package index could not be reached or does not carry these releases" in text
        assert "KIROCREW_ALLOW_SOURCE_BUILDS=1" in text
        assert "service unit" in text
        assert "kirocrew update" in text
        # pip's own verdict is kept, so the operator can check the classification.
        assert "No matching distribution found for numpy>=2.0" in text
        # Nothing was compiled: the install command was the only pip install of
        # the wheel, and it carried the policy.
        assert isinstance(info.value.__cause__, wheel_engine.WheelUpdateError)

    def test_the_message_matches_the_installers_platform_floor(self) -> None:
        """Same supported-platform sentence as cli.sh's _report_pip_failure, so an
        install and its later update do not describe two different floors."""
        cli_sh = (_REPO_ROOT / "cli.sh").read_text(encoding="utf-8")
        floor = (
            "a newer Linux (Amazon Linux 2023, RHEL/Rocky 8+, Ubuntu 22.04+, "
            "Debian 12+) on x86_64/aarch64, or macOS"
        )
        assert floor in cli_sh, "cli.sh's platform floor sentence changed; update both"
        assert floor in wheel_engine._no_wheel_message(["numpy"])

    def test_other_pip_failures_keep_pips_own_words(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failure that is not the no-wheel verdict (a dead index, a broken
        wheel) must not be dressed up as an unsupported platform."""
        monkeypatch.delenv("KIROCREW_ALLOW_SOURCE_BUILDS", raising=False)
        wheel = tmp_path / "w.whl"
        self._failing_install(
            monkeypatch,
            wheel,
            b"ERROR: HTTPSConnectionPool(host='pypi.org'): Max retries exceeded\n",
        )

        with pytest.raises(wheel_engine.WheelUpdateError) as info:
            wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

        text = str(info.value)
        assert text.startswith("pip install into the shadow venv exited 1"), text
        assert "Max retries exceeded" in text
        assert "prebuilt wheel" not in text
        assert "KIROCREW_ALLOW_SOURCE_BUILDS" not in text

    def test_incompatible_wheels_read_as_versions_none_and_still_get_the_guidance(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """pip builds its `(from versions: ...)` list from the candidates that
        survived its link filter, so a package whose every wheel targets a newer
        libc or another arch reads `(from versions: none)` -- the pure no-wheel
        host. That shape must get the same guidance as a numeric list, with the
        index named as the other possible cause (pip's text cannot tell them
        apart); pip's own words stay attached so the operator can check."""
        monkeypatch.delenv("KIROCREW_ALLOW_SOURCE_BUILDS", raising=False)
        wheel = tmp_path / "w.whl"
        self._failing_install(
            monkeypatch,
            wheel,
            b"ERROR: Could not find a version that satisfies the requirement numpy>=2.0 "
            b"(from kirocrew) (from versions: none)\n"
            b"ERROR: No matching distribution found for numpy>=2.0\n",
        )

        with pytest.raises(wheel_engine.WheelUpdateError) as info:
            wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

        text = str(info.value)
        assert text.startswith(
            "pip found no prebuilt wheel of numpy>=2.0 it may install on this host"
        ), text
        assert "package index could not be reached or does not carry these releases" in text
        assert "KIROCREW_ALLOW_SOURCE_BUILDS=1" in text
        assert "from versions: none" in text

    def test_the_opt_in_never_reports_a_platform_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With compiling allowed, 'No matching distribution' means the release
        genuinely does not exist, not that a wheel is missing: no dressing-up."""
        monkeypatch.setenv("KIROCREW_ALLOW_SOURCE_BUILDS", "1")
        wheel = tmp_path / "w.whl"
        self._failing_install(monkeypatch, wheel, self._NO_WHEEL_TAIL)

        with pytest.raises(wheel_engine.WheelUpdateError) as info:
            wheel_engine.build_shadow_venv(wheel, tmp_path / "crew-venv-1.0.0")

        text = str(info.value)
        assert text.startswith("pip install into the shadow venv exited 1"), text
        assert "prebuilt wheel" not in text

    def test_no_wheel_packages_are_deduplicated_and_capped(self) -> None:
        seen = (
            "ERROR: Could not find a version that satisfies the requirement pkg0 "
            "(from versions: 1.0, 2.0)\n"
        )
        text = (
            seen
            + "\n".join(f"ERROR: No matching distribution found for pkg{i % 3}" for i in range(9))
            + "\n"
            + "\n".join(f"ERROR: No matching distribution found for extra{i}" for i in range(9))
        )
        got = wheel_engine._no_wheel_packages(text)
        assert got == ["pkg0", "pkg1", "pkg2", "extra0", "extra1"]
        assert wheel_engine._no_wheel_packages("ERROR: something else") == []
        # The verdict line is the whole signal: pip's release list cannot gate it
        # (see _no_wheel_packages), so the bare line classifies too.
        assert wheel_engine._no_wheel_packages(
            "ERROR: No matching distribution found for pkg0"
        ) == ["pkg0"]
