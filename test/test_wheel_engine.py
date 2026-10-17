"""Tests for the shadow-venv wheel update engine.

The engine's whole value is what it REFUSES: an unsigned or tampered manifest,
a wheel whose digest is not the signed one, a promotion over a non-symlink,
pruning a tree something might be running from. Each refusal is pinned here or in a
``test_wheel_engine_*`` sibling, plus the one cross-file invariant nothing else checks — the
trust root must be byte-identical to cli.sh's copy, or the two verifiers drift apart silently.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from spawn_test_helpers import (
    LONG_CHILD_WITH_GRANDCHILD,
    await_gone,
    await_pids,
    kill_leftovers,
)
from wheel_engine_helpers import (
    _ARTIFACT_BASE,
    _REPO_ROOT,
    _manifest,
    _raw,
    _stub_children,
)

from kiro_crew.platform import wheel_engine
from kiro_crew.platform.wheel_engine import (
    ManagedVenvLayout,
    WheelUpdateError,
    parse_and_validate_manifest,
)
from kiro_crew.platform_compat import IS_POSIX, trusted_system_bin

# The engine is POSIX-only by construction: cli.sh is a POSIX installer, and
# running_from_managed_venv() answers False on Windows, so no production path
# reaches this module there. The tests lean on POSIX semantics throughout —
# atomic rename over a symlink (WinError 5 on Windows), 0o600 mode bits, and
# venv bin/ layout — so they are skipped as a module rather than papered over
# with per-test shims that would test nothing real.
pytestmark = pytest.mark.skipif(not IS_POSIX, reason="the shadow-venv engine is POSIX-only")


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

    @pytest.mark.skipif(not IS_POSIX, reason="the /dev/fd fast path is POSIX-only")
    def test_posix_verification_stages_no_files(self, keypair: Path, tmp_path: Path) -> None:
        """RED-THEN-GREEN. On POSIX the key/signature go to openssl over
        anonymous pipe FDs and the payload over stdin, so NOTHING is written to
        the workdir — there is no attacker-plantable name for the gateway to
        follow out of the sandbox. Fails pre-fix, which wrote the PEM, DER,
        payload and signature into the workdir by name."""
        canonical = b'{"v":"1"}\n'
        signature = self._sign(keypair, canonical, tmp_path)
        workdir = tmp_path / "work"
        workdir.mkdir()
        wheel_engine._verify_signature(canonical, signature, workdir)
        assert list(workdir.iterdir()) == [], "verification wrote files into the workdir"

    def test_fallback_refuses_a_symlink_planted_at_the_pem_name(
        self, keypair: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED-THEN-GREEN. The non-/dev/fd fallback (Windows, or a POSIX host
        without /dev/fd) stages files in the workdir — but opens each
        O_CREAT|O_EXCL|O_NOFOLLOW, so a symlink an attacker pre-planted at the
        PEM name is REFUSED, not followed and overwritten. Pre-fix the gateway's
        pem.write_bytes() followed the symlink and clobbered the target."""
        # Force the fallback even on a POSIX host with /dev/fd.
        monkeypatch.setattr(wheel_engine.os.path, "isdir", lambda p: False)
        canonical = b'{"v":"1"}\n'
        signature = self._sign(keypair, canonical, tmp_path)
        workdir = tmp_path / "work"
        workdir.mkdir()
        victim = tmp_path / "operator-secret"
        victim.write_bytes(b"OPERATOR CREDENTIAL\n")
        # The attacker pre-plants a symlink at the exact name the gateway writes.
        planted = workdir / "cli-manifest-public.pem"
        try:
            planted.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host")
        with pytest.raises(WheelUpdateError, match="stage verification inputs"):
            wheel_engine._verify_signature(canonical, signature, workdir)
        # The victim file is untouched: the write was refused, not followed.
        assert victim.read_bytes() == b"OPERATOR CREDENTIAL\n"

    def test_fallback_write_is_byte_exact_no_newline_translation(self, tmp_path: Path) -> None:
        """The fallback stages the canonical payload with O_BINARY, so the bytes
        land verbatim. In text mode Windows would translate the payload's
        trailing LF to CRLF, changing the signed bytes and failing verification
        closed — a spurious Windows upgrade-refusal. The write must preserve
        every byte, LF included."""
        payload = b'{"v":"1","trailing":"newline"}\n'
        assert b"\r\n" not in payload
        dest = tmp_path / "signed-payload.json"
        wheel_engine._create_exclusive_nofollow(dest, payload)
        assert dest.read_bytes() == payload

    def test_an_openssl_timeout_is_not_reported_as_a_bad_signature(
        self, keypair: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a genuine refusal may say "signature verification failed"."""
        canonical = b'{"v":"1"}\n'
        signature = self._sign(keypair, canonical, tmp_path)
        monkeypatch.setattr(wheel_engine, "_OPENSSL_TIMEOUT_SECS", 0)
        workdir = tmp_path / "work"
        workdir.mkdir()
        with pytest.raises(WheelUpdateError) as info:
            wheel_engine._verify_signature(canonical, signature, workdir)
        assert "timed out after 0s" in str(info.value)
        assert "signature verification failed" not in str(info.value)


class TestEngineLockMatchesInstaller:
    def test_cli_sh_takes_the_engine_lock_in_its_managed_venv_branch(self) -> None:
        """One update lease per layout: cli.sh and the engine lock the same file."""
        cli_sh = (_REPO_ROOT / "cli.sh").read_text(encoding="utf-8")
        assert '_VENV_LOCK="${VENV%/}.update.lock"' in cli_sh
        assert 'exec 9>>"$_VENV_LOCK"' in cli_sh
        layout = ManagedVenvLayout(
            legacy=Path("/srv/crew-venv"), stable_link=Path("/srv/crew-venv-current")
        )
        assert wheel_engine._update_lock_path(layout) == Path("/srv/crew-venv.update.lock")


class TestWheelDownload:
    class _FakeResponse:
        """Chunk-serving stand-in for the urlopen response."""

        def __init__(self, body: bytes, chunk: int = 7) -> None:
            self._view = memoryview(body)
            self._pos = 0
            self._chunk = chunk

        def read1(self, n: int) -> bytes:
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

    @pytest.mark.parametrize("deadline_elapsed", [False, True])
    def test_disk_write_failure_names_the_destination_not_the_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline_elapsed: bool
    ) -> None:
        """ENOSPC mid-stream narrates as ``could not write <dest>``, never as a CDN
        fetch failure and never as the fetch's own total-time deadline."""
        self._serve(monkeypatch, b"wheel-bytes")

        import builtins

        real_open = builtins.open

        class _FullDisk:
            def __init__(self, *a: object, **k: object) -> None:
                pass

            def write(self, data: bytes) -> int:
                if deadline_elapsed:
                    # The write itself outlives the fetch's total budget (below).
                    time.sleep(0.3)
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
        dest = tmp_path / "w.whl"
        with pytest.raises(WheelUpdateError) as info:
            wheel_engine._download_to_file(
                f"{_ARTIFACT_BASE}/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl",
                dest,
                cap=1 << 20,
                timeout=5,
                expected_sha="0" * 64,
                total_secs=0.2 if deadline_elapsed else 5,
            )
        text = str(info.value)
        assert text.startswith(f"could not write {dest}: ") and "No space left" in text
        assert "could not fetch" not in text and "did not finish" not in text
        assert isinstance(info.value.__cause__, OSError), "the OSError stays on the chain"

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

            def read1(self, n: int) -> bytes:
                out, self._body = self._body[:n], self._body[n:]
                return out

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


class _LoopbackServer(HTTPServer):
    """One-request-at-a-time loopback origin that serves ``payload`` with a chosen framing."""

    payload: bytes = b""
    chunked: bool = False

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host name between bind() and
        # listen(); a loopback origin needs no name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _PayloadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _LoopbackServer)
        body = server.payload
        self.send_response(200)
        if server.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if not server.chunked:
            self.wfile.write(body)
            return
        for start in range(0, len(body), 4096):
            part = body[start : start + 4096]
            self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, format: str, *args: object) -> None:
        return None


class TestRealHttpResponse:
    """The fetch over a real ``http.client`` response, which the fakes above cannot model.

    A Content-Length response closes itself, and the socket under it, in the read
    that returns its last byte. The fakes expose no socket, so only a real
    response reaches the per-read timeout after that close.
    """

    @pytest.fixture
    def origin(self, monkeypatch: pytest.MonkeyPatch):
        server = _LoopbackServer(("127.0.0.1", 0), _PayloadHandler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        # A ProxyHandler({}) opener: a developer host's proxy environment must not
        # route the loopback request elsewhere.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        loopback = f"http://127.0.0.1:{server.server_port}/"
        monkeypatch.setattr(
            wheel_engine.urllib.request,
            "urlopen",
            lambda req, timeout: opener.open(loopback, timeout=timeout),
        )
        sockets: list[object] = []
        real_socket = wheel_engine._response_socket

        def _spy(resp: object) -> object | None:
            sock = real_socket(resp)
            sockets.append(sock)
            return sock

        monkeypatch.setattr(wheel_engine, "_response_socket", _spy)
        try:
            yield server, sockets
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive(), "the loopback origin did not stop"

    @pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
    def test_small_body_is_read_to_the_end(self, origin, chunked: bool) -> None:
        server, sockets = origin
        server.payload = json.dumps({"version": "9.9.9"}).encode()
        server.chunked = chunked
        try:
            got = wheel_engine._fetch_bytes("https://x.example/latest-cli.json", 65536, 5)
        except WheelUpdateError as exc:
            pytest.fail(f"a complete body must not fail the fetch: {exc}")
        assert got == server.payload
        assert sockets and sockets[0] is not None, "the real response must expose its socket"

    def test_multi_read_wheel_is_written_and_verified(self, origin, tmp_path: Path) -> None:
        server, sockets = origin
        server.payload = bytes(range(256)) * 769  # three full 64 KiB reads plus a tail
        dest = tmp_path / "w.whl"
        try:
            wheel_engine._download_to_file(
                f"{_ARTIFACT_BASE}/cli/stable/9.9.9/kirocrew-9.9.9-py3-none-any.whl",
                dest,
                cap=1 << 20,
                timeout=5,
                expected_sha=hashlib.sha256(server.payload).hexdigest(),
            )
        except WheelUpdateError as exc:
            pytest.fail(f"a complete wheel must not fail the download: {exc}")
        assert dest.read_bytes() == server.payload
        assert sockets and sockets[0] is not None, "the real response must expose its socket"


class TestRunHelper:
    def test_nonzero_exit_carries_stderr_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_children(monkeypatch, lambda _argv: (3, b"", b"broken pipe"))
        with pytest.raises(WheelUpdateError, match="exited 3.*broken pipe"):
            wheel_engine._run(["x"], 5, "step-x")

    def test_a_timeout_names_the_step(self, tmp_path: Path) -> None:
        with pytest.raises(WheelUpdateError, match=r"^step-x timed out after 0s$"):
            wheel_engine._run([sys.executable, "-c", "import time; time.sleep(30)"], 0, "step-x")

    def test_a_missing_binary_names_the_step(self, tmp_path: Path) -> None:
        with pytest.raises(WheelUpdateError, match="^step-x could not run"):
            wheel_engine._run([str(tmp_path / "no-such-binary")], 5, "step-x")

    @pytest.mark.parametrize(
        "secret",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        ],
    )
    def test_a_credential_straddling_the_cut_is_redacted_whole(
        self, monkeypatch: pytest.MonkeyPatch, secret: str
    ) -> None:
        """The stderr is redacted IN FULL before it is cut to its tail.

        Cutting first can split a credential, and the half that survives does not
        match the redactor's pattern. Every cut position inside the token is tried.
        """
        limit = wheel_engine._ERROR_DETAIL_CHARS
        for keep in range(1, len(secret)):
            # `keep` characters of the token fall inside the kept tail.
            stderr = ("x" * (limit + 50) + secret + "y" * (limit - keep)).encode()
            _stub_children(monkeypatch, lambda _argv, err=stderr: (1, b"", err))
            with pytest.raises(WheelUpdateError) as info:
                wheel_engine._run(["x"], 5, "step-x")
            assert secret[-keep:] not in str(info.value), keep


@pytest.fixture(params=["waitid", "no-waitid"])
def reap_platform(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a kill test as it runs on Linux and as it runs on macOS CPython < 3.13,
    which has no ``os.waitid``: the reap must not depend on it."""
    if request.param == "no-waitid":
        monkeypatch.delattr(os, "waitid", raising=False)
    return request.param


class TestCancellableBuildChild:
    """Every child is killed WITH its group and reaped, on every way out."""

    @staticmethod
    def _run_in_thread(
        argv: list[str], timeout: float, ctx: object
    ) -> tuple[threading.Thread, list[BaseException]]:
        outcome: list[BaseException] = []

        def _build() -> None:
            try:
                wheel_engine._run(argv, timeout, "venv creation", ctx=ctx)  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
                outcome.append(exc)

        worker = threading.Thread(target=_build)
        worker.start()
        return worker, outcome

    def test_cancel_kills_and_reaps_the_child_and_its_group(
        self, tmp_path: Path, reap_platform: str
    ) -> None:
        pidfile = tmp_path / "pids"
        cancel = wheel_engine.ApplyCancel()
        worker, outcome = self._run_in_thread(
            [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)],
            60,
            wheel_engine._BuildContext(cancel=cancel),
        )
        pids: tuple[int, ...] = ()
        try:
            pids = await_pids(pidfile)
            cancel.set("shutdown")
            worker.join(timeout=30)
            assert not worker.is_alive(), "a cancelled build child must not be waited out"
            assert len(outcome) == 1
            assert isinstance(outcome[0], wheel_engine.WheelUpdateCancelled)
            assert outcome[0].reason == "shutdown"
            assert all(await_gone(pid) for pid in pids), "the whole group must die"
        finally:
            cancel.set()
            worker.join(timeout=30)
            kill_leftovers(pids)

    def test_a_set_cancel_spawns_nothing(self, tmp_path: Path) -> None:
        cancel = wheel_engine.ApplyCancel()
        cancel.set()
        pidfile = tmp_path / "pids"
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            wheel_engine._run(
                [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)],
                60,
                "venv creation",
                ctx=wheel_engine._BuildContext(cancel=cancel),
            )
        assert not pidfile.exists()

    def test_the_timeout_kills_the_group_and_never_reports_a_status(
        self, tmp_path: Path, reap_platform: str
    ) -> None:
        pidfile = tmp_path / "pids"
        pids: tuple[int, ...] = ()
        try:
            with pytest.raises(WheelUpdateError, match="timed out"):
                wheel_engine._spawn_build_child(
                    [
                        sys.executable,
                        "-c",
                        LONG_CHILD_WITH_GRANDCHILD + "",
                        str(pidfile),
                    ],
                    1,
                    "venv creation",
                )
            if pidfile.exists():
                pids = await_pids(pidfile)
                assert all(await_gone(pid) for pid in pids)
        finally:
            kill_leftovers(pids)

    def test_an_error_while_waiting_kills_reaps_and_names_the_step(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reap_platform: str
    ) -> None:
        """An OSError out of the wait (here from the poll interval) kills the group."""
        pidfile = tmp_path / "pids"
        real_sleep = time.sleep
        raised: list[bool] = []

        def failing_sleep(secs: float) -> None:
            if pidfile.exists() and not raised:
                raised.append(True)
                raise OSError(12, "Cannot allocate memory")
            real_sleep(secs)

        monkeypatch.setattr(wheel_engine.time, "sleep", failing_sleep)
        pids: tuple[int, ...] = ()
        try:
            with pytest.raises(WheelUpdateError, match="venv creation failed while running"):
                wheel_engine._run(
                    [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)],
                    60,
                    "venv creation",
                )
            monkeypatch.setattr(wheel_engine.time, "sleep", real_sleep)
            pids = await_pids(pidfile)
            assert all(await_gone(pid) for pid in pids)
        finally:
            kill_leftovers(pids)

    def test_a_failure_arming_the_cancel_still_kills_and_reaps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Everything after the spawn sits inside the kill-and-reap guard."""
        pidfile = tmp_path / "pids"
        cancel = wheel_engine.ApplyCancel()
        started: list[int] = []
        real_popen = wheel_engine.subprocess.Popen

        def recording_popen(*args: object, **kwargs: object) -> object:
            proc = real_popen(*args, **kwargs)  # type: ignore[call-overload]
            started.append(proc.pid)
            return proc

        def broken_on_set(hook: object) -> object:
            raise RuntimeError("cannot arm")

        monkeypatch.setattr(wheel_engine.subprocess, "Popen", recording_popen)
        monkeypatch.setattr(cancel, "on_set", broken_on_set)
        try:
            with pytest.raises(RuntimeError, match="cannot arm"):
                wheel_engine._run(
                    [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)],
                    60,
                    "venv creation",
                    ctx=wheel_engine._BuildContext(cancel=cancel),
                )
            assert started and all(await_gone(pid) for pid in started)
        finally:
            kill_leftovers(started)

    def test_a_finished_child_reports_its_output(self) -> None:
        rc, out, err = wheel_engine._spawn_build_child(
            [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"],
            30,
            "probe",
            want_stdout=True,
        )
        assert (rc, out.strip(), err) == (0, b"out", b"err")

    @pytest.mark.parametrize("trusted", [True, False])
    def test_children_run_in_their_own_session_with_the_build_umask(
        self, monkeypatch: pytest.MonkeyPatch, trusted: bool
    ) -> None:
        """The real Popen call: own session, owner-only umask, cwd ``/``, and the
        environment the route chooses (the gateway's scrubbed one, or the CLI's own)."""
        seen: list[dict[str, object]] = []
        real_popen = wheel_engine.subprocess.Popen

        def recording_popen(*args: object, **kwargs: object) -> object:
            seen.append(kwargs)
            return real_popen(*args, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(wheel_engine.subprocess, "Popen", recording_popen)
        monkeypatch.setenv("PYTHONPATH", "/somewhere/foreign")
        monkeypatch.setenv("LD_PRELOAD", "/somewhere/evil.so")
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/python/lib")
        wheel_engine._run(
            [sys.executable, "-c", "pass"],
            30,
            "probe",
            ctx=wheel_engine._BuildContext(trusted_env=trusted),
        )
        (kwargs,) = seen
        assert kwargs["start_new_session"] is True
        assert kwargs["umask"] == wheel_engine._BUILD_UMASK
        assert kwargs["cwd"] == "/"
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["LD_LIBRARY_PATH"] == "/opt/python/lib", "library paths are kept"
        if trusted:
            assert "PYTHONPATH" not in env and "LD_PRELOAD" not in env
        else:
            assert env["PYTHONPATH"] == "/somewhere/foreign", "the CLI passes its own shell"

    def test_every_child_holds_the_update_lock_past_its_parent(self, tmp_path: Path) -> None:
        """The lock fd is handed to the child, so an orphaned child keeps the lock."""
        from kiro_crew.platform_compat import try_acquire_lock

        lock_path = tmp_path / "crew-venv.update.lock"
        parent_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        assert try_acquire_lock(parent_fd, exclusive=True)
        pidfile = tmp_path / "pids"
        cancel = wheel_engine.ApplyCancel()
        worker, _outcome = self._run_in_thread(
            [sys.executable, "-c", LONG_CHILD_WITH_GRANDCHILD, str(pidfile)],
            60,
            wheel_engine._BuildContext(cancel=cancel, lock_fd=parent_fd),
        )
        pids: tuple[int, ...] = ()
        probe = os.open(str(lock_path), os.O_RDWR)
        try:
            pids = await_pids(pidfile)
            os.close(parent_fd)  # the parent lets go WITHOUT unlocking, as an exit does
            parent_fd = -1
            assert not try_acquire_lock(probe, exclusive=True), "the child must still hold it"
            cancel.set()
            worker.join(timeout=30)
            assert all(await_gone(pid) for pid in pids)
            assert try_acquire_lock(probe, exclusive=True), "freed once the child is gone"
        finally:
            cancel.set()
            worker.join(timeout=30)
            kill_leftovers(pids)
            os.close(probe)
            if parent_fd >= 0:
                os.close(parent_fd)


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
        seen = _stub_children(monkeypatch)
        monkeypatch.setattr(wheel_engine, "_BUILD_UMASK", 0o077)

        wheel_engine.build_shadow_venv(tmp_path / "w.whl", tmp_path / "crew-venv-1.0.0")

        assert [kwargs.get("umask") for _argv, kwargs in seen] == [0o077, 0o077, 0o077]

    def test_build_creates_owner_only_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The shadow root is born 0o700 -- not group-writable even under umask 002."""
        monkeypatch.setattr(wheel_engine, "_run", lambda *a, **k: None)
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

        def answer(argv: list[str]) -> tuple[int, bytes, bytes]:
            seen.append(argv)
            return 0, b"", b""

        _stub_children(monkeypatch, answer)
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

        def answer(argv: list[str]) -> tuple[int, bytes, bytes]:
            failing = str(wheel) in argv
            return (1 if failing else 0), b"", (stderr if failing else b"")

        _stub_children(monkeypatch, answer)

    def test_a_no_wheel_failure_names_the_platform_and_the_opt_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The update refuses in the installer's words, not pip's: which packages
        have no wheel, what host this is, and how to opt into compiling -- including
        that only `kirocrew update` from the operator's shell honours it (the
        gateway's own build steps run on the trusted system PATH), which is the one
        fact an install-time success hides."""
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
        assert "trusted system PATH only" in text
        assert "kirocrew update` from a shell" in text
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
