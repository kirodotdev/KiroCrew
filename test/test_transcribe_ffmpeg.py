"""Tests for where ``kiro_crew.transcribe`` finds its ffmpeg decoder.

Covers the pinned imageio-ffmpeg wheel and how its image is authenticated
before it runs, the fixed system directories on Windows and elsewhere, and
never the ambient PATH. The release pin-completeness test here imports
``imageio_ffmpeg`` at module scope on purpose: a dev install without it is a
collection error, never a silent skip.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import imageio_ffmpeg
import pytest

# The authoritative upstream map of platform key -> shipped ffmpeg filename, plus
# the resolver for which key THIS host is. The pin-completeness test derives the
# expected filename set from the map rather than hand-copying names, so a
# one-character drift fails at test time. imageio-ffmpeg is a dev dependency
# precisely so this import (and the tests) run for real; a `pytest.importorskip`
# here would silently pass and reproduce the very "green while checking nothing"
# shape these tests replace.
from imageio_ffmpeg._definitions import FNAME_PER_PLATFORM, get_platform

from kiro_crew import platform_compat as _pc
from kiro_crew import transcribe
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# ---------------------------------------------------------------------------
# ffmpeg discovery: bundled with desktop releases, with system fallback for source
# ---------------------------------------------------------------------------

# Floor for a plausible pinned ffmpeg payload. The real upstream executables are
# 49-88 MB, so this is an order of magnitude below the smallest of them: it is here
# to catch a size transcribed with digits dropped, not to track a version, and it
# must stay far enough below the true sizes that an upstream bump never trips it.
_MIN_PLAUSIBLE_FFMPEG_BYTES = 8 * 1024 * 1024

# GitHub Actions sets CI=true on every runner. Used only to decide whether a test
# that cannot obtain its inputs is allowed to skip: a developer machine may lack
# them, a runner may not.
_RUNNING_UNDER_CI = os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


class TestBundledFfmpeg:
    """Resolve only the executable inside the pinned imageio-ffmpeg wheel."""

    @staticmethod
    def _fake_package(
        monkeypatch, tmp_path, *, create_binary: bool = True, signer_rewritten: bool = False
    ):
        package_dir = tmp_path / "imageio_ffmpeg"
        binaries = package_dir / "binaries"
        binaries.mkdir(parents=True)
        filename = "ffmpeg-test.exe" if _pc.IS_WINDOWS else "ffmpeg-test"
        binary = binaries / filename
        payload = b"bundled decoder"
        if create_binary:
            binary.write_bytes(payload)
            binary.chmod(0o755)

        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", lambda: (str(tmp_path),))
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(payload), transcribe.hashlib.sha256(payload).hexdigest())},
        )
        monkeypatch.setattr(
            transcribe,
            "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS",
            frozenset({filename} if signer_rewritten else ()),
        )
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: True)
        # A release that SHIPS a decoder, whatever this host's own platform is: the
        # fail-closed rules below are about that release, not about macOS Intel.
        monkeypatch.setattr(
            transcribe.decoder,
            "platform_key",
            lambda *a, **k: transcribe.decoder.ARTIFACTS[0].platform_key,
        )
        return binary

    def test_resolves_the_exact_wheel_resource(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_a_release_on_an_unpinned_host_cpu_still_uses_its_own_decoder(
        self, monkeypatch, tmp_path
    ):
        """The x64 Windows release on an ARM64 PC keeps the decoder it carries.

        Python reports the physical CPU there, which keys to the unpinned
        ``windows-aarch64``. What the release ships decides, not that key.
        """
        binary = self._fake_package(monkeypatch, tmp_path)
        monkeypatch.setattr(transcribe.decoder, "platform_key", lambda *a, **k: "windows-aarch64")

        def _unexpected_system_lookup():
            raise AssertionError("a release with a decoder looked for a system one")

        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", _unexpected_system_lookup)
        assert transcribe._find_ffmpeg() == str(binary)
        assert transcribe.ffmpeg_source() == transcribe.FFMPEG_SOURCE_BUNDLED

    def test_missing_wheel_resource_is_not_replaced_by_path(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path, create_binary=False)
        monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(tmp_path / "attacker-controlled"))
        assert transcribe._bundled_ffmpeg() is None

    def test_missing_bundle_never_falls_back_to_system_for_status(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path, create_binary=False)

        def _unexpected_system_lookup():
            raise AssertionError("bundled runtime fell back to a system decoder")

        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", _unexpected_system_lookup)
        assert transcribe._find_ffmpeg() is None

    def test_missing_bundle_never_falls_back_to_system_for_execution(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path, create_binary=False)

        def _unexpected_system_lookup():
            raise AssertionError("bundled runtime fell back to a system decoder")

        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", _unexpected_system_lookup)
        assert transcribe._open_ffmpeg_for_execution() is None

    def test_project_venv_is_never_trusted_as_a_bundled_runtime(self, monkeypatch):
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: False)

        def _unexpected_roots():
            raise AssertionError("source environment was scanned for an executable")

        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", _unexpected_roots)
        assert transcribe._bundled_ffmpeg() is None

    @staticmethod
    def _release_without_a_decoder(monkeypatch, system_ffmpeg):
        """A bundled interpreter on the backend that ships no decoder (macOS Intel)."""
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            transcribe.decoder,
            "platform_key",
            lambda *a, **k: transcribe.decoder.DECODERLESS_BUNDLE_PLATFORM,
        )

        def _unexpected_roots():
            raise AssertionError("a release that ships no decoder scanned its bundle for one")

        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", _unexpected_roots)
        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", lambda: system_ffmpeg)

    def test_a_release_with_no_decoder_executes_the_system_one(self, monkeypatch):
        """Nothing was shipped to be damaged, so failing closed would only break voice.

        The Intel half of the universal app carries no imageio-ffmpeg. It resolves
        exactly like a source install: a system FFmpeg from the fixed directories
        first.
        """
        self._release_without_a_decoder(monkeypatch, "/usr/local/bin/ffmpeg")
        assert transcribe._open_ffmpeg_for_execution() == "/usr/local/bin/ffmpeg"

    def test_a_release_with_no_decoder_reports_the_system_one(self, monkeypatch):
        """Status names the decoder execution would run, and where it came from."""
        self._release_without_a_decoder(monkeypatch, "/usr/local/bin/ffmpeg")
        assert transcribe._find_ffmpeg() == "/usr/local/bin/ffmpeg"
        assert transcribe.ffmpeg_source() == transcribe.FFMPEG_SOURCE_SYSTEM

    def test_a_release_with_no_decoder_and_no_system_one_reports_none(self, monkeypatch, tmp_path):
        self._release_without_a_decoder(monkeypatch, None)
        monkeypatch.setattr(transcribe.decoder, "store_dir", lambda: tmp_path)
        assert transcribe._open_ffmpeg_for_execution() is None
        assert transcribe._find_ffmpeg() is None
        assert transcribe.ffmpeg_source() is None

    def test_packaged_cli_without_electron_parent_uses_release_decoder(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)

        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_same_size_replacement_is_rejected_by_digest(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"changed decoder")

        assert transcribe._bundled_ffmpeg() is None

    def test_truncated_payload_is_rejected_by_size(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"short")

        assert transcribe._bundled_ffmpeg() is None

    def _probe_with_exit(self, monkeypatch, tmp_path, script: bytes):
        """Install *script* as the packaged decoder and probe it for real.

        The decoder is EXECUTED rather than stubbed, because the split this covers
        is about what a real spawn reports back.
        """
        package_dir = tmp_path / "imageio_ffmpeg"
        binaries = package_dir / "binaries"
        binaries.mkdir(parents=True)
        filename = "ffmpeg-test.exe" if _pc.IS_WINDOWS else "ffmpeg-test"
        binary = binaries / filename
        binary.write_bytes(script)
        binary.chmod(0o755)
        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", lambda: (str(tmp_path),))
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(script), transcribe.hashlib.sha256(script).hexdigest())},
        )
        monkeypatch.setattr(transcribe, "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS", frozenset())
        return binary, transcribe._packaged_ffmpeg_version_probe()

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="needs a POSIX shell for the fake decoder")
    def test_probe_reports_ok_when_the_authenticated_decoder_runs(self, monkeypatch, tmp_path):
        _, probe = self._probe_with_exit(
            monkeypatch, tmp_path, b"#!/bin/sh\necho 'ffmpeg version 7'\nexit 0\n"
        )

        assert probe.ok is True
        assert probe.authentic is True
        assert probe.code == transcribe.DECODER_OK

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="needs a POSIX shell for the fake decoder")
    def test_a_host_that_cannot_run_an_authentic_decoder_is_not_reported_as_tampering(
        self, monkeypatch, tmp_path
    ):
        """This is the WHOLE point of the split, so it is pinned explicitly.

        A build image missing a library the decoder needs makes the loader refuse
        an executable whose bytes are exactly the pinned payload. Reporting that as
        an unauthentic artifact fails the release over a property of the build
        machine, and sends whoever reads the log hunting a supply-chain problem
        that is not there.
        """
        _, probe = self._probe_with_exit(
            monkeypatch,
            tmp_path,
            b"#!/bin/sh\necho 'libavcodec.so.61: cannot open shared object' >&2\nexit 127\n",
        )

        assert probe.ok is False
        # Authentic: the release gate must NOT fail the build on this.
        assert probe.authentic is True
        assert probe.code == transcribe.DECODER_NOT_EXECUTABLE
        # The child's own complaint reaches the caller, rather than DEVNULL.
        assert "libavcodec.so.61" in probe.detail

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="needs a POSIX shell for the fake decoder")
    def test_a_decoder_that_cannot_be_spawned_at_all_still_counts_as_authentic(
        self, monkeypatch, tmp_path
    ):
        """The OSError arm is a DISTINCT path from a non-zero exit and must reach
        the same verdict: the bytes authenticated, the host would not run them."""
        _, probe = self._probe_with_exit(
            monkeypatch, tmp_path, b"#!/nonexistent/interpreter\nbody\n"
        )

        assert probe.ok is False
        assert probe.authentic is True
        assert probe.code == transcribe.DECODER_NOT_EXECUTABLE

    def test_a_payload_that_fails_authentication_is_still_refused(self, monkeypatch, tmp_path):
        """Splitting the outcomes must not have weakened the half that matters:
        bytes that are not the pinned payload stay a hard failure."""
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"attacker decoder")

        probe = transcribe._packaged_ffmpeg_version_probe()

        assert probe.ok is False
        assert probe.authentic is False
        assert probe.code == transcribe.DECODER_UNAUTHENTIC

    def test_windows_loader_statuses_are_named_in_the_detail(self):
        """0xC0000135 is the observed Server Core refusal, and a bare signed
        integer in a build log is not something a reader can act on."""

        class _Refused:
            returncode = -1073741515
            stdout = b""
            stderr = b""

        detail = transcribe._decoder_exit_detail("/x/ffmpeg", _Refused())

        assert "0xC0000135" in detail
        assert "STATUS_DLL_NOT_FOUND" in detail

    def test_probe_detail_is_bounded(self):
        """A decoder can emit unbounded output; the report stays log-sized."""

        class _Noisy:
            returncode = 1
            stdout = b""
            stderr = b"x" * 5000

        detail = transcribe._decoder_exit_detail("/x/ffmpeg", _Noisy())

        assert len(detail) < transcribe._DECODER_DETAIL_MAX_CHARS + 200
        assert detail.endswith("…")

    def test_payload_chunks_yield_the_file_bytes(self, tmp_path):
        raw = tmp_path / "ffmpeg"
        raw.write_bytes(b"bundled decoder")
        descriptor = os.open(raw, os.O_RDONLY)
        try:
            assert b"".join(transcribe._ffmpeg_payload_chunks(descriptor)) == b"bundled decoder"
        finally:
            os.close(descriptor)

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="signer-rewritten payloads are macOS-only")
    def test_signer_rewritten_payload_is_accepted_on_a_developer_id_signature(
        self, monkeypatch, tmp_path
    ):
        """A released macOS decoder does not match the upstream digest, because
        signing rewrites its Mach-O signature. macOS vouching for the exact bytes
        staged for execution is the second anchor."""
        binary = self._fake_package(monkeypatch, tmp_path, signer_rewritten=True)
        binary.write_bytes(b"bundled decoder + developer id signature")
        binary.chmod(0o755)
        verified: list[str] = []

        def authentic(path):
            verified.append(path)
            return True

        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", authentic)

        opened = transcribe._open_packaged_ffmpeg_resource()

        assert opened is not None
        try:
            assert opened.source_path == str(binary)
            # The signature was checked on the snapshot, not on the bundle path.
            assert verified == [opened.execution_path]
            assert opened.execution_path != str(binary)
        finally:
            opened.close()

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="signer-rewritten payloads are macOS-only")
    def test_signer_rewritten_payload_without_a_valid_signature_is_refused(
        self, monkeypatch, tmp_path
    ):
        binary = self._fake_package(monkeypatch, tmp_path, signer_rewritten=True)
        binary.write_bytes(b"attacker decoder, ad-hoc signed")
        binary.chmod(0o755)
        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", lambda _path: False)

        assert transcribe._bundled_ffmpeg() is None

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="signer-rewritten payloads are macOS-only")
    def test_upstream_digest_still_authenticates_without_any_signature(self, monkeypatch, tmp_path):
        """The desktop build gate runs BEFORE signing, so the pinned wheel bytes
        must keep authenticating on their own."""
        binary = self._fake_package(monkeypatch, tmp_path, signer_rewritten=True)

        def _unexpected_codesign(_path):
            raise AssertionError("codesign consulted for pinned upstream bytes")

        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", _unexpected_codesign)

        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_non_signer_rewritten_artifact_never_consults_codesign(self, monkeypatch, tmp_path):
        """Linux and Windows payloads stay digest-only: a mismatch is refused
        outright, never escalated to a signature check."""
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"changed decoder!!")

        def _unexpected_codesign(_path):
            raise AssertionError("codesign consulted for a digest-pinned artifact")

        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", _unexpected_codesign)

        assert transcribe._bundled_ffmpeg() is None

    def test_codesign_requirement_pins_the_release_team(self):
        """Chained to Apple's root AND to our team, so an ad-hoc or self-signed
        replacement cannot satisfy it."""
        assert transcribe._MACOS_SIGNING_TEAM_ID == "94KV3E626L"
        assert transcribe._MACOS_FFMPEG_REQUIREMENT == (
            'anchor apple generic and certificate leaf[subject.OU] = "94KV3E626L"'
        )

    def test_codesign_requirement_quotes_the_team_identifier(self):
        """The team id must be a QUOTED string constant, not a bare token.

        A requirement-language identifier cannot begin with a digit, so an
        unquoted 94KV3E626L does not parse as a comparison operand at all --
        codesign rejects the whole requirement and exits non-zero before it looks
        at any signature. :func:`_macos_developer_id_authentic` only sees that
        exit status, so the malformed constant is indistinguishable there from an
        inauthentic binary, and every signed release refused its own decoder.
        Asserting the exact string above cannot catch this on its own: the old
        assertion pinned the unparseable form and passed.
        """
        assert f'"{transcribe._MACOS_SIGNING_TEAM_ID}"' in transcribe._MACOS_FFMPEG_REQUIREMENT

    @pytest.mark.skipif(not sys.platform.startswith("darwin"), reason="codesign is macOS-only")
    def test_codesign_accepts_the_requirement_as_parseable(self):
        """Let codesign itself judge the syntax -- the only oracle that counts.

        Run against a binary the OS signs (never our team), so a PARSEABLE
        requirement is simply unsatisfied while an unparseable one reports a
        syntax error. Only the latter is asserted against: this test is about the
        requirement compiling, not about /bin/ls being authentic.
        """
        result = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                "-R",
                f"={transcribe._MACOS_FFMPEG_REQUIREMENT}",
                "--",
                "/bin/ls",
            ],
            check=False,
            capture_output=True,
            timeout=120,
            **UTF8_TEXT,
        )
        assert "syntax error" not in result.stderr.lower()
        assert "invalid or corrupted code requirement" not in result.stderr.lower()

    def test_codesign_is_invoked_by_absolute_path_with_requirement_source_text(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(transcribe.platform_compat, "IS_MACOS", True)
        recorded: list[list[str]] = []

        class _Result:
            returncode = 0

        def fake_run(argv, **_kwargs):
            recorded.append(argv)
            return _Result()

        monkeypatch.setattr(transcribe.subprocess, "run", fake_run)

        assert transcribe._macos_developer_id_authentic(str(tmp_path / "ffmpeg")) is True
        argv = recorded[0]
        assert argv[0] == "/usr/bin/codesign"
        assert "--strict" in argv
        # A bare requirement string would be read as a FILE PATH by codesign.
        assert argv[argv.index("-R") + 1].startswith("=anchor apple generic")

    def test_codesign_absence_is_not_authenticity(self, monkeypatch, tmp_path):
        monkeypatch.setattr(transcribe.platform_compat, "IS_MACOS", True)

        def missing_codesign(*_args, **_kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(transcribe.subprocess, "run", missing_codesign)

        assert transcribe._macos_developer_id_authentic(str(tmp_path / "ffmpeg")) is False

    def test_linux_memfd_explicitly_requests_executable_mode(self, monkeypatch):
        calls = []

        def create_memfd(name, flags):
            calls.append((name, flags))
            return 41

        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(transcribe.os, "memfd_create", create_memfd, raising=False)

        assert transcribe._new_executable_snapshot() == (41, -1, True, None)
        assert len(calls) == 1
        assert calls[0][0] == "kirocrew-ffmpeg"
        assert calls[0][1] & getattr(transcribe.os, "MFD_EXEC", 0x0010)

    def test_linux_memfd_exec_flag_falls_back_on_old_kernels(self, monkeypatch):
        calls = []

        def create_memfd(_name, flags):
            calls.append(flags)
            if len(calls) == 1:
                raise OSError(transcribe.errno.EINVAL, "unsupported flag")
            return 42

        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(transcribe.os, "memfd_create", create_memfd, raising=False)

        assert transcribe._new_executable_snapshot() == (42, -1, True, None)
        assert calls[0] & getattr(transcribe.os, "MFD_EXEC", 0x0010)
        assert not calls[1] & getattr(transcribe.os, "MFD_EXEC", 0x0010)

    def test_linux_seal_constants_work_with_old_python_headers(self, monkeypatch):
        calls = []
        fake_fcntl = SimpleNamespace(fcntl=lambda *args: calls.append(args))
        monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
        monkeypatch.setattr(
            transcribe.os, "open", lambda path, flags: (calls.append((path, flags)), 52)[1]
        )

        assert transcribe._seal_linux_memfd(51) == 52
        assert calls[0] == (51, 1033, 0x000F)
        assert calls[1][0] == "/proc/self/fd/51"

    def test_macos_snapshot_keeps_a_private_name_until_close(self, monkeypatch, tmp_path):
        parent = tmp_path / "private"

        def make_private_dir(*_args, **_kwargs):
            parent.mkdir(mode=0o700)
            return str(parent)

        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(transcribe, "_ffmpeg_snapshot_root", lambda: str(tmp_path))
        monkeypatch.setattr(transcribe.tempfile, "mkdtemp", make_private_dir)

        writer, reader, seal_writer, path = transcribe._new_executable_snapshot()
        assert path is not None
        assert not seal_writer
        os.write(writer, b"bundled decoder")
        os.close(writer)
        opened = transcribe._AuthenticatedFfmpeg("source", reader, path, cleanup_path=path)
        assert os.path.isfile(path)

        opened.close()

        assert not parent.exists()

    def test_macos_snapshot_is_staged_under_agent_denied_root(self, monkeypatch, tmp_path):
        calls = []

        def make_private_dir(*, prefix, dir):
            calls.append((prefix, dir))
            parent = tmp_path / "private"
            parent.mkdir(mode=0o700)
            return str(parent)

        root = tmp_path / "voice-runtime"
        root.mkdir()
        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(transcribe, "_ffmpeg_snapshot_root", lambda: str(root))
        monkeypatch.setattr(transcribe.tempfile, "mkdtemp", make_private_dir)

        writer, reader, _seal_writer, path = transcribe._new_executable_snapshot()
        os.close(writer)
        opened = transcribe._AuthenticatedFfmpeg("source", reader, path, cleanup_path=path)
        try:
            assert calls == [(f".kirocrew-ffmpeg-{os.getpid()}-", str(root))]
        finally:
            opened.close()

    def test_snapshot_root_is_private_and_pruned_once(self, monkeypatch, tmp_path):
        root = tmp_path / "voice-runtime"
        root.mkdir(mode=0o755)
        cleanup_calls = []
        monkeypatch.setattr(
            "kiro_crew.sandbox.prime_voice_runtime_sandbox_paths",
            lambda: str(root),
        )
        monkeypatch.setattr(transcribe, "_ffmpeg_snapshot_roots_cleaned", set())
        monkeypatch.setattr(
            transcribe,
            "_cleanup_stale_ffmpeg_snapshots",
            lambda path: cleanup_calls.append(path),
        )

        assert transcribe._ffmpeg_snapshot_root() == str(root)
        assert transcribe._ffmpeg_snapshot_root() == str(root)

        assert cleanup_calls == [str(root)]
        if not _pc.IS_WINDOWS:
            assert os.stat(root).st_mode & 0o777 == 0o700

    def test_snapshot_root_rejects_a_regular_file(self, monkeypatch, tmp_path):
        root = tmp_path / "not-a-directory"
        root.write_bytes(b"not a runtime root")
        monkeypatch.setattr(
            "kiro_crew.sandbox.prime_voice_runtime_sandbox_paths",
            lambda: str(root),
        )

        with pytest.raises(OSError, match="not a real directory"):
            transcribe._ffmpeg_snapshot_root()

    def test_snapshot_cleanup_is_best_effort_for_every_operation(self, tmp_path):
        payload = tmp_path / "private" / "ffmpeg"
        calls = []

        def fail(operation):
            def _raise(path, *_args):
                calls.append((operation, path))
                raise OSError(f"{operation} denied")

            return _raise

        with (
            patch.object(transcribe.os, "chmod", fail("chmod")),
            patch.object(transcribe.os, "unlink", fail("unlink")),
            patch.object(transcribe.os, "rmdir", fail("rmdir")),
        ):
            transcribe._remove_named_snapshot(str(payload))

        assert calls == [
            ("chmod", str(payload.parent)),
            ("unlink", str(payload)),
            ("rmdir", str(payload.parent)),
        ]

    def test_stale_macos_snapshots_are_pruned_without_following_unknown_entries(
        self, monkeypatch, tmp_path
    ):
        stale = tmp_path / ".kirocrew-ffmpeg-123-dead"
        stale.mkdir()
        (stale / "ffmpeg").write_bytes(b"stale")
        active = tmp_path / f".kirocrew-ffmpeg-{os.getpid()}-active"
        active.mkdir()
        (active / "ffmpeg").write_bytes(b"active")
        unknown = tmp_path / ".kirocrew-ffmpeg-not-a-pid"
        unknown.mkdir()
        monkeypatch.setattr(
            transcribe.platform_compat,
            "pid_liveness",
            lambda pid: transcribe.platform_compat.PID_DEAD if pid == 123 else "alive",
        )

        transcribe._cleanup_stale_ffmpeg_snapshots(str(tmp_path))

        assert not stale.exists()
        assert active.exists()
        assert unknown.exists()

    def test_release_artifact_pins_cover_every_supported_desktop(self):
        """The pin list must cover EXACTLY the shipped desktop platforms.

        No more, no less. The expected filenames are DERIVED from upstream
        imageio_ffmpeg._definitions.FNAME_PER_PLATFORM through the maintainer-owned
        source of truth transcribe._SHIPPED_FFMPEG_PLATFORMS, rather than
        hand-copied into this test. So an omitted platform, a one-character name
        typo, or a stale version suffix fails loudly HERE instead of silently at
        runtime, where _open_packaged_ffmpeg_resource() would treat the missed
        filename as an absent binary and report a generic "missing or damaged"
        decoder.
        """
        # A stale or renamed platform key would silently drop that platform's
        # binary from expected_filenames below, so check membership first.
        missing_keys = transcribe._SHIPPED_FFMPEG_PLATFORMS - set(FNAME_PER_PLATFORM)
        assert not missing_keys, (
            f"_SHIPPED_FFMPEG_PLATFORMS keys absent from upstream "
            f"FNAME_PER_PLATFORM (renamed or removed upstream?): {sorted(missing_keys)}"
        )

        expected_filenames = {
            FNAME_PER_PLATFORM[key] for key in transcribe._SHIPPED_FFMPEG_PLATFORMS
        }
        assert set(transcribe._PACKAGED_FFMPEG_ARTIFACTS) == expected_filenames

    def test_host_platform_pin_matches_the_real_installed_bytes(self):
        """The pin for THIS host's platform is checked against the actual bytes.

        Deriving the KEY set from upstream says nothing about the (size, sha256)
        VALUES, and the digest is what the module calls its trust anchor: a
        fat-fingered or truncated one ships a decoder that no platform can
        authenticate. imageio-ffmpeg is a real dev dependency, so its wheel puts
        this platform's executable in site-packages -- the same upstream bytes the
        desktop build stages -- which turns one transcribed literal per host into a
        checked claim. The Linux, Windows and macOS-arm64 legs cover every pin but
        linux-aarch64, which no runner can supply and is shape-guarded below.

        CI may not skip this. Every CI leg runs on a platform the desktop matrix
        ships, and pip resolves that platform's imageio-ffmpeg wheel, which carries
        the executable -- so under CI both of those are requirements rather than
        luck, and a resolver that produced the pure-Python wheel instead has
        silently removed the only byte-level check on the pinned values. A skip
        scores as a pass, which is the exact "green while checking nothing" shape
        this whole class of test exists to end, so it becomes a red instead. A
        contributor on an unshipped platform, or one whose environment genuinely
        cannot supply the bytes, still skips -- fetching them over the network is
        not something a unit test may do.
        """
        host_key = get_platform()
        filename = FNAME_PER_PLATFORM.get(host_key)
        binary = (
            Path(imageio_ffmpeg.__file__).parent / "binaries" / filename
            if filename is not None
            else None
        )

        unavailable = ""
        if host_key not in transcribe._SHIPPED_FFMPEG_PLATFORMS:
            unavailable = f"host platform {host_key!r} is not a shipped desktop target"
        elif binary is None or not binary.is_file():
            unavailable = f"imageio-ffmpeg installed no upstream executable at {binary}"

        if unavailable:
            assert not _RUNNING_UNDER_CI, (
                "CI must check this host's pin against the real installed bytes, "
                f"and cannot: {unavailable}"
            )
            pytest.skip(unavailable)

        assert binary is not None  # narrowed by the guard above

        payload = binary.read_bytes()
        assert (
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        ) == transcribe._PACKAGED_FFMPEG_ARTIFACTS[filename], (
            f"the pin for {filename} disagrees with the bytes imageio-ffmpeg "
            "actually installs -- one of the two is wrong"
        )

    def test_every_pin_is_a_well_formed_size_and_sha256(self):
        """Shape guard for the pinned values no CI leg can hash.

        linux-aarch64 has no runner that installs its upstream executable, so for
        that entry a truncated digest or a zero-length size would otherwise reach a
        desktop user as a generic "missing or damaged decoder" remedy with CI fully
        green. The bounds are
        deliberately loose -- an ffmpeg build is tens of megabytes and a signed one
        stays under _MAX_SIGNED_FFMPEG_BYTES -- so a legitimate version bump passes
        while a transcription slip does not.
        """
        assert transcribe._PACKAGED_FFMPEG_ARTIFACTS, "the pin table must not be empty"

        for filename, (size, digest) in sorted(transcribe._PACKAGED_FFMPEG_ARTIFACTS.items()):
            assert (
                _MIN_PLAUSIBLE_FFMPEG_BYTES <= size <= transcribe._MAX_SIGNED_FFMPEG_BYTES
            ), f"{filename}: pinned size {size} is not a plausible ffmpeg payload"
            assert re.fullmatch(
                r"[0-9a-f]{64}", digest
            ), f"{filename}: pinned digest is not a lowercase 64-hex SHA-256: {digest!r}"

        # Two platforms pinning one digest is the copy-paste slip that a per-entry
        # check cannot see, and it authorizes one platform's bytes to run as
        # another's.
        digests = [digest for _, digest in transcribe._PACKAGED_FFMPEG_ARTIFACTS.values()]
        assert len(set(digests)) == len(
            digests
        ), f"duplicate pinned digest across platforms: {digests}"

    def test_signer_rewritten_artifacts_are_exactly_the_macos_slices(self):
        """The signature-anchored set is derived, not hand-listed.

        The macOS app signer rewrites every nested Mach-O on its way into a
        release, so exactly the macOS slices -- and only those -- are the ones
        whose pinned upstream digest cannot match the released bytes and which
        therefore need the signature anchor instead. Deriving the set means adding
        a macOS slice cannot forget it: an omitted name makes every SIGNED release
        on that platform refuse its own decoder, because the digest cannot match
        post-signing and the signature anchor is never consulted for a name that is
        not listed.
        """
        expected = {
            FNAME_PER_PLATFORM[key]
            for key in transcribe._SHIPPED_FFMPEG_PLATFORMS
            if key.startswith("macos-")
        }
        assert transcribe._SIGNER_REWRITTEN_FFMPEG_ARTIFACTS == expected
        # Authenticating-by-signature a name the module never pins would be a claim
        # about nothing.
        assert transcribe._SIGNER_REWRITTEN_FFMPEG_ARTIFACTS <= set(
            transcribe._PACKAGED_FFMPEG_ARTIFACTS
        )

    @pytest.mark.asyncio
    async def test_authenticated_bytes_remain_bound_until_spawn(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        opened = transcribe._open_packaged_ffmpeg_resource()
        assert opened is not None
        descriptor = opened.descriptor

        if _pc.IS_WINDOWS:
            # CreateFileW denies writes and replacement while CreateProcess
            # opens this exact image.
            with pytest.raises(PermissionError):
                binary.write_bytes(b"changed decoder")
        else:
            # The source pathname may change freely: execution uses the sealed
            # memfd/private snapshot, never this name again.
            binary.write_bytes(b"changed decoder")

        sentinel = object()

        async def fake_spawn(execution_path, *args, **kwargs):
            os.fstat(descriptor)  # still open for the whole spawn operation
            if _pc.IS_WINDOWS:
                assert execution_path == str(binary)
            else:
                assert kwargs["pass_fds"] == (descriptor,)
                os.lseek(descriptor, 0, os.SEEK_SET)
                assert os.read(descriptor, 1024) == b"bundled decoder"
            return sentinel

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        assert await transcribe._create_ffmpeg_subprocess(opened, "-version") is sentinel
        # The staged image must remain resolvable AFTER the spawn returns: the
        # macOS syspolicy assessment resolves the path asynchronously,
        # so the caller owns the close once the child has exited.
        os.fstat(descriptor)
        await transcribe._close_ffmpeg_for_execution(opened)
        with pytest.raises(OSError):
            os.fstat(descriptor)

    @pytest.mark.asyncio
    async def test_every_spawn_is_pinned_to_local_protocols(self, monkeypatch):
        """GPT review r19: the import suffix allowlist is not a content check,
        so an allowed-suffix file whose bytes are an HLS/ffconcat playlist
        reaches the demuxer — which would then FETCH the playlist's segment
        URLs (SSRF). Every spawn must therefore carry FFmpeg's protocol
        allowlist flag (pinned to ``file,pipe``) AHEAD of the caller's args,
        so it precedes every ``-i`` and applies to nested (playlist-segment)
        opens too."""
        captured: list[tuple[str, ...]] = []
        sentinel = object()

        async def fake_spawn(execution_path, *args, **kwargs):
            captured.append(args)
            return sentinel

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        result = await transcribe._create_ffmpeg_subprocess(
            "ffmpeg", "-y", "-i", "/tmp/a.wav", "out.ogg"
        )
        assert result is sentinel
        flag = "-protocol_whitelist"  # wokeignore:rule=whitelist
        assert captured[0][:2] == (flag, "file,pipe")
        assert captured[0].index(flag) < captured[0].index("-i")

    def test_every_import_suffix_has_a_forced_demuxer(self):
        """GPT review r20: the protocol pin stops NETWORK fetches, but an
        auto-probed playlist demuxer could still open other LOCAL files its
        text names — reads that never went through ``validate_file_path``.
        Forcing the validated suffix's own demuxer turns playlist text into a
        decode error. Every import-admissible suffix must therefore map to a
        demuxer, so no attacker-influenced input ever falls back to content
        sniffing."""
        from kiro_crew.apps.builtins.meetings.backend import constants as k

        for suffix in k.IMPORT_AUDIO_EXTENSIONS:
            forced = transcribe._forced_demuxer_args(f"/tmp/talk{suffix}")
            assert forced and forced[0] == "-f", f"no forced demuxer for {suffix}"
        # Internal temp files with unrecognized names keep the old behaviour.
        assert transcribe._forced_demuxer_args("/tmp/unknown.xyz") == ()

    @pytest.mark.skipif(os.name == "nt", reason="descriptor paths are the POSIX branch")
    def test_a_descriptor_path_resolves_the_pinned_files_real_suffix(self, tmp_path):
        """An import hands consumers ``/dev/fd/N`` (GPT review r21). Format
        decisions must follow the suffix of the file the descriptor REALLY
        holds — resolved through the kernel, never the mutable name."""
        real = tmp_path / "talk.webm"
        real.write_bytes(b"x")
        fd = os.open(str(real), os.O_RDONLY)
        try:
            dev_path = f"/dev/fd/{fd}"
            assert transcribe._input_suffix(dev_path) == ".webm"
            assert transcribe._forced_demuxer_args(dev_path) == ("-f", "matroska,webm")
        finally:
            os.close(fd)
        # Closed descriptor: the suffix is unknowable, and the demuxer pin
        # refuses rather than letting FFmpeg sniff the content.
        assert transcribe._input_suffix(dev_path) is None
        with pytest.raises(OSError):
            transcribe._forced_demuxer_args(dev_path)

    @pytest.mark.skipif(os.name == "nt", reason="descriptor paths are the POSIX branch")
    def test_a_descriptor_input_renamed_to_an_unmapped_suffix_is_refused(self, tmp_path):
        """The r25 attack: the suffix behind ``/dev/fd/N`` follows the
        descriptor's CURRENT name, so a same-uid rename that strips the suffix
        after pinning would drop the forced demuxer and re-enable content
        sniffing. A descriptor input must REFUSE on an unmapped suffix — the
        sniffing fallback belongs only to plain-path internal temps."""
        real = tmp_path / "talk.webm"
        real.write_bytes(b"x")
        fd = os.open(str(real), os.O_RDONLY)
        try:
            os.rename(real, tmp_path / "talk")  # the racer strips the suffix
            with pytest.raises(OSError, match="unmapped suffix"):
                transcribe._forced_demuxer_args(f"/dev/fd/{fd}")
        finally:
            os.close(fd)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="descriptor paths are the POSIX branch")
    async def test_descriptor_inputs_are_inherited_by_the_ffmpeg_child(self, tmp_path, monkeypatch):
        """A ``/dev/fd/N`` argv entry is useless to the child unless N survives
        the exec: the spawn seam must put it in ``pass_fds``."""
        real = tmp_path / "talk.wav"
        real.write_bytes(b"x")
        fd = os.open(str(real), os.O_RDONLY)
        captured: dict = {}

        async def fake_spawn(program, *args, **kwargs):
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        try:
            await transcribe._create_ffmpeg_subprocess(
                "ffmpeg", "-y", "-i", f"/dev/fd/{fd}", "out.ogg"
            )
            assert fd in captured["kwargs"].get("pass_fds", ())
        finally:
            os.close(fd)

    @pytest.mark.asyncio
    async def test_failed_spawn_closes_handle_off_event_loop(self, monkeypatch, tmp_path):
        """A spawn that raises never produced a child, so nothing depends on
        the staged path surviving: the handle is closed immediately (and off
        the event loop) before the error propagates. A SUCCESSFUL spawn must
        NOT close here — see ``TestStagedFfmpegOutlivesSpawn``."""
        self._fake_package(monkeypatch, tmp_path)
        opened = transcribe._open_packaged_ffmpeg_resource()
        assert opened is not None
        event_loop_thread = threading.get_ident()
        close_threads = []
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            if self.descriptor >= 0:
                close_threads.append(threading.get_ident())
            original_close(self)

        async def fake_spawn(*_args, **_kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        with pytest.raises(OSError, match="exec failed"):
            await transcribe._create_ffmpeg_subprocess(opened, "-version")

        assert len(close_threads) == 1
        assert close_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_pre_spawn_temp_failure_closes_handle_off_event_loop(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path)
        event_loop_thread = threading.get_ident()
        close_threads = []
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            if self.descriptor >= 0:
                close_threads.append(threading.get_ident())
            original_close(self)

        def disk_full():
            raise OSError("disk full")

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)
        monkeypatch.setattr(transcribe, "_make_temp_wav", disk_full)

        with pytest.raises(OSError, match="disk full"):
            await transcribe._pcm_via_ffmpeg(str(tmp_path / "memo.webm"), 1)

        assert len(close_threads) == 1
        assert close_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_cancelled_resolver_closes_eventual_handle_off_loop(self, monkeypatch, tmp_path):
        binary = tmp_path / "ffmpeg"
        binary.write_bytes(b"decoder")
        descriptor = os.open(binary, os.O_RDONLY)
        opened = transcribe._AuthenticatedFfmpeg(str(binary), descriptor, str(binary))
        event_loop_thread = threading.get_ident()
        resolver_started = threading.Event()
        release_resolver = threading.Event()
        handle_closed = threading.Event()
        close_threads = []
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            if self.descriptor >= 0:
                close_threads.append(threading.get_ident())
            original_close(self)
            handle_closed.set()

        def slow_resolver():
            resolver_started.set()
            # flake-ok: pre-existing 5 s timed Event wait that parks the fake resolver thread until the test releases it after the cancel, moved verbatim; tracked for a de-flake pass
            release_resolver.wait(timeout=5)
            return opened

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)
        monkeypatch.setattr(transcribe, "_open_ffmpeg_for_execution", slow_resolver)
        task = asyncio.create_task(transcribe._resolve_ffmpeg_for_execution())
        # flake-ok: pre-existing 1 s timed Event wait parked on a pool thread that asserts the resolver thread started before the task is cancelled, moved verbatim; tracked for a de-flake pass
        assert await asyncio.to_thread(resolver_started.wait, 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing await of the cancelled resolver task with no timeout bound that expects CancelledError to surface, moved verbatim; tracked for a de-flake pass
            await task
        release_resolver.set()
        # flake-ok: pre-existing 2 s timed Event wait parked on a pool thread that asserts the abandoned handle is closed off the event loop after the release, moved verbatim; tracked for a de-flake pass
        assert await asyncio.to_thread(handle_closed.wait, 2)

        assert close_threads
        assert close_threads[0] != event_loop_thread

    def test_cwd_package_shadow_cannot_supply_the_decoder(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        shadow = repo / "imageio_ffmpeg"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text(
            "raise AssertionError('cwd package imported')\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(repo)
        monkeypatch.syspath_prepend(str(repo))
        binary = self._fake_package(monkeypatch, tmp_path / "trusted")

        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_resolved_binary_cannot_escape_the_binaries_directory(self, monkeypatch, tmp_path):
        attacker = tmp_path / "attacker-ffmpeg"
        attacker.write_text("attacker decoder", encoding="utf-8")
        attacker.chmod(0o755)
        binary = self._fake_package(monkeypatch, tmp_path)
        realpath = transcribe.os.path.realpath

        def escape_candidate(path):
            resolved = realpath(path)
            if resolved == str(binary):
                return str(attacker)
            return resolved

        monkeypatch.setattr(transcribe.os.path, "realpath", escape_candidate)

        assert transcribe._bundled_ffmpeg() is None

    def test_bundle_wins_before_every_system_candidate(self, monkeypatch, tmp_path):
        binary = tmp_path / "bundle" / ("ffmpeg.exe" if _pc.IS_WINDOWS else "ffmpeg")
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(transcribe, "_bundled_ffmpeg", lambda: str(binary))

        def _system_probe(*_args, **_kwargs):
            raise AssertionError("system lookup ran before the bundled decoder")

        monkeypatch.setattr(transcribe.shutil, "which", _system_probe)
        assert transcribe._find_ffmpeg() == str(binary)


class TestFfmpegCandidateDirsWindows:
    """Regression guards for the Windows ffmpeg discovery fix.

    Runs on POSIX CI by monkeypatching ``platform_compat.IS_WINDOWS`` (same
    pattern as ``TestTaskkillErrorMapping`` in ``test_platform_compat_kill_reap.py``) —
    the branch construction is platform-independent code.
    """

    def test_windows_dirs_appended(self, monkeypatch):
        from kiro_crew import platform_compat as pc
        from kiro_crew import transcribe as tr

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        pf = r"C:\Program Files"
        la = r"C:\Users\user\AppData\Local"
        monkeypatch.setenv("ProgramFiles", pf)
        monkeypatch.setenv("LOCALAPPDATA", la)

        dirs = tr._ffmpeg_candidate_dirs()
        assert os.path.join(pf, "ffmpeg", "bin") in dirs
        assert os.path.join(la, "Programs", "ffmpeg", "bin") in dirs
        assert "/usr/local/bin" in dirs

    def test_non_windows_omits_windows_dirs(self, monkeypatch):
        from kiro_crew import platform_compat as pc
        from kiro_crew import transcribe as tr

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        dirs = tr._ffmpeg_candidate_dirs()
        for d in dirs:
            assert "Program Files" not in d
            assert "AppData" not in d

    def test_ensure_ffmpeg_probes_with_which(self, tmp_path, monkeypatch):
        """``ensure_ffmpeg_in_path`` must use ``shutil.which(name, path=d)`` so it
        catches ``ffmpeg.exe`` on Windows in addition to plain ``ffmpeg`` on POSIX.
        Regression: the prior implementation called ``os.path.isfile(<d>/ffmpeg)``
        which is blind to the ``.exe`` suffix.
        """
        from kiro_crew import transcribe as tr

        target_dir = tmp_path / "ffbin"
        target_dir.mkdir()

        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(target_dir)])
        monkeypatch.setenv("PATH", "/nowhere")

        calls: list[tuple[str, str]] = []

        def fake_which(name, path=None):
            calls.append((name, path or ""))
            return f"{path}/ffmpeg" if path == str(target_dir) else None

        monkeypatch.setattr(tr.shutil, "which", fake_which)
        tr.ensure_ffmpeg_in_path()

        assert calls and calls[0][0] == "ffmpeg"
        assert calls[0][1] == str(target_dir)
        assert os.environ["PATH"].startswith(str(target_dir))

    def test_ensure_ffmpeg_skips_dirs_already_on_path(self, tmp_path, monkeypatch):
        from kiro_crew import transcribe as tr

        target_dir = tmp_path / "ffbin"
        target_dir.mkdir()
        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(target_dir)])
        monkeypatch.setenv("PATH", f"{target_dir}{os.pathsep}/nowhere")

        called: list[str] = []

        def fake_which(name, path=None):
            called.append(name)
            return None

        monkeypatch.setattr(tr.shutil, "which", fake_which)
        tr.ensure_ffmpeg_in_path()

        assert called == []
        assert os.environ["PATH"].startswith(str(target_dir))


class TestFfmpegDiscoveryWindowsOnly:
    """Real, unmocked Windows behaviour — mkdir a fake install dir, drop an
    ``ffmpeg.exe`` inside, point ``_FFMPEG_CANDIDATE_DIRS`` at it, verify
    ``ensure_ffmpeg_in_path`` picks it up. Skipped on POSIX.
    """

    @pytest.mark.skipif(
        not _pc.IS_WINDOWS,
        reason="Windows-only: exercises PATHEXT-driven .exe suffix resolution.",
    )
    def test_ffmpeg_exe_discovered(self, tmp_path, monkeypatch):
        from kiro_crew import transcribe as tr

        ffbin = tmp_path / "ffbin"
        ffbin.mkdir()
        exe = ffbin / "ffmpeg.exe"
        exe.write_bytes(b"MZ")

        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(ffbin)])
        monkeypatch.setenv("PATH", r"C:\\Windows\\System32")

        tr.ensure_ffmpeg_in_path()
        assert os.environ["PATH"].startswith(str(ffbin))


class TestFfmpegIsNotResolvedFromPath:
    """The resolved binary is exec'd by the gateway, so PATH must not choose it.

    A gateway's PATH can legitimately lead with agent-writable directories (a worktree
    venv's ``bin``, ``~/.local/bin``), which is the threat
    `platform_compat.trusted_system_bin` documents: a planted ``ffmpeg`` would run with
    the gateway's environment and credentials. `_find_ffmpeg` therefore searches fixed
    directories, most-trusted first, and never the ambient PATH.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _without_the_optional_bundle(self, monkeypatch):
        """Keep these fallback tests independent of installed optional extras."""
        monkeypatch.setattr(transcribe, "_bundled_ffmpeg", lambda: None)

    @staticmethod
    def _plant(directory):
        """Plant a findable ffmpeg, named the way the host's loader requires.

        ``.exe`` on Windows: `shutil.which` needs a PATHEXT match there, so a file
        named plainly ``ffmpeg`` is invisible to it. Without this the two positive
        assertions below failed on Windows, and — worse — the NEGATIVE one passed for
        the wrong reason: "PATH did not choose it" was true because nothing could have
        chosen it. An absence assertion that holds with the guard deleted guards
        nothing.
        """
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / ("ffmpeg.exe" if _pc.IS_WINDOWS else "ffmpeg")
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        binary.chmod(0o755)
        return binary

    @staticmethod
    def _same_file(found, expected):
        """Compare two paths the way the host's filesystem does.

        `shutil.which` returns the name it CONSTRUCTED, not the directory entry it
        matched: on Windows it appends each `PATHEXT` entry as spelled in the
        environment, which is uppercase, so a planted ``ffmpeg.exe`` comes back as
        ``ffmpeg.EXE``. `os.path.normcase` is identity on POSIX, where the two
        spellings really are different files.
        """
        return found is not None and os.path.normcase(found) == os.path.normcase(str(expected))

    def test_a_binary_only_on_path_is_never_chosen(self, tmp_path, monkeypatch):
        planted = self._plant(tmp_path / "evil")
        monkeypatch.setenv("PATH", str(planted.parent))
        # No fixed candidate holds one, so the honest answer is "not installed".
        monkeypatch.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(tmp_path / "nowhere")])
        monkeypatch.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        assert transcribe._find_ffmpeg() is None, "a PATH-planted ffmpeg was selected"

    def test_a_trusted_directory_wins_over_a_writable_candidate(self, tmp_path, monkeypatch):
        """Order is the guard: a writable dir must not SHADOW a system install."""
        system = self._plant(tmp_path / "system")
        writable = self._plant(tmp_path / "home")
        monkeypatch.setenv("PATH", str(writable.parent))
        monkeypatch.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(writable.parent)])
        monkeypatch.setattr(
            transcribe.platform_compat, "trusted_system_path", lambda: str(system.parent)
        )
        assert self._same_file(transcribe._find_ffmpeg(), system)

    def test_a_fixed_candidate_is_still_used_when_no_system_copy_exists(
        self, tmp_path, monkeypatch
    ):
        """The per-user dirs stay usable: refusing them makes voice memos undecodable
        on a host whose only ffmpeg was unzipped by hand."""
        candidate = self._plant(tmp_path / "opt")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(candidate.parent)])
        monkeypatch.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        assert self._same_file(transcribe._find_ffmpeg(), candidate)

    def test_no_generic_user_writable_directory_is_a_candidate(self, monkeypatch):
        """Ordering them last was not enough, so they are not candidates at all.

        On a host with no packaged ffmpeg a trailing `~/.local/bin` was still trusted,
        and that directory exists to hold loose binaries on nearly every PATH. A
        package-manager root is a different proposition even when user-owned (Homebrew
        owns `/opt/homebrew` on Apple Silicon): planting there overwrites a managed
        file rather than adding a name.
        """
        for windows in (False, True):
            monkeypatch.setattr(transcribe.platform_compat, "IS_WINDOWS", windows)
            dirs = transcribe._ffmpeg_candidate_dirs()
            assert dirs, "the list must not be empty"
            for d in dirs:
                assert d not in (
                    os.path.expanduser("~/ffmpeg"),
                    os.path.expanduser("~/.local/bin"),
                ), f"{d} is a generic user-writable directory"
                assert os.path.basename(d.rstrip(os.sep)) in (
                    "bin",
                ), f"{d} is not a package-manager bin directory"
