"""Tests for _resolve_session_key ancestor PID walk."""

from __future__ import annotations

import os
import platform
from unittest.mock import patch

import pytest

from kiro_crew.mcp_core import _get_ppid, _ppid_via_libproc, _resolve_session_key


class TestResolveSessionKey:
    def test_env_var_takes_priority(self):
        """Env var is returned immediately without file I/O."""
        with patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-1-123"}):
            assert _resolve_session_key() == "dashboard:chat-1-123"

    def test_fresh_session_returns_env_without_walking_pids(self):
        """Fresh (non-pooled) session: the key is baked into the env at spawn
        (AcpClient._spawn sets KIROCREW_SESSION_KEY), so resolution returns it
        immediately and NEVER walks ancestor PIDs. Guards against regressing to
        a walk-first order, which would needlessly depend on _get_ppid (broken
        on sandboxed macOS) even when the env answer is already present."""
        with patch.dict(
            "os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-9-999"}
        ), patch("kiro_crew.mcp_core._get_ppid") as mock_ppid:
            assert _resolve_session_key() == "dashboard:chat-9-999"
            mock_ppid.assert_not_called()

    def test_warm_pool_session_resolves_via_pid_file(self, tmp_path):
        """Warm-pool session: the process is spawned keyless (empty env), then
        rekey writes session_pid_<pid>.txt. With no env var, resolution must
        fall through to the PID walk and find that mapping — the path the
        macOS libproc _get_ppid fix restores."""
        ppid = os.getppid()
        (tmp_path / f"session_pid_{ppid}.txt").write_text("dashboard:chat-warm-7")
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ):
            assert _resolve_session_key() == "dashboard:chat-warm-7"

    def test_pid_file_immediate_parent(self, tmp_path):
        """Finds PID file on immediate parent (cold-start case)."""
        ppid = os.getppid()
        (tmp_path / f"session_pid_{ppid}.txt").write_text("dashboard:chat-2-456")

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ):
            assert _resolve_session_key() == "dashboard:chat-2-456"

    def test_ancestor_walk_finds_grandparent(self, tmp_path):
        """Walks up ancestors when immediate parent has no PID file."""
        (tmp_path / "session_pid_25.txt").write_text("dashboard:chat-3-789")

        # Mock: PID 100 -> parent 50 -> parent 25 (has file)
        def fake_get_ppid(pid):
            return {100: 50, 50: 25}.get(pid, 0)

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ), patch("os.getppid", return_value=100), patch(
            "kiro_crew.mcp_core._get_ppid", side_effect=fake_get_ppid
        ):
            assert _resolve_session_key() == "dashboard:chat-3-789"

    def test_returns_empty_when_no_file_found(self, tmp_path):
        """Returns empty string when ancestor chain reaches init."""

        def fake_get_ppid(pid):
            return {100: 1}.get(pid, 0)

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ), patch("os.getppid", return_value=100), patch(
            "kiro_crew.mcp_core._get_ppid", side_effect=fake_get_ppid
        ):
            assert _resolve_session_key() == ""

    def test_stops_on_ppid_failure(self, tmp_path):
        """Stops walking when _get_ppid returns 0 (failure)."""
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ), patch("os.getppid", return_value=99999), patch(
            "kiro_crew.mcp_core._get_ppid", return_value=0
        ):
            assert _resolve_session_key() == ""

    def test_symlinked_pid_file_refused(self, tmp_path):
        """SYMLINK ATTACK on the lenient path: session_pid_<pid>.txt replaced
        by a symlink to a sensitive file must NOT be followed — the lenient
        resolver reads through session_pid_sig's hardened no-follow reader
        (same discipline as the strict verifier, minus the signature)."""
        secret = tmp_path / "victim-secret"
        secret.write_text("dashboard:chat-stolen", encoding="utf-8")
        ppid = os.getppid()
        (tmp_path / f"session_pid_{ppid}.txt").symlink_to(secret)
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ), patch("kiro_crew.mcp_core._get_ppid", return_value=0):
            assert _resolve_session_key() == ""

    def test_handles_cycle_detection(self, tmp_path):
        """Stops if PID chain forms a cycle (prevents infinite loop)."""

        def fake_get_ppid(pid):
            return {100: 50, 50: 100}.get(pid, 0)

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(
            "kiro_crew.mcp_core.config_dir", return_value=tmp_path
        ), patch("os.getppid", return_value=100), patch(
            "kiro_crew.mcp_core._get_ppid", side_effect=fake_get_ppid
        ):
            assert _resolve_session_key() == ""


class TestGetPpid:
    def test_linux_reads_proc(self):
        """On Linux, parses PPid from /proc status."""
        fake_content = "Name:\tkiro-cli\nPPid:\t42\nTgid:\t100\n"

        with patch("platform.system", return_value="Linux"), patch(
            "pathlib.Path.read_text", return_value=fake_content
        ):
            assert _get_ppid(100) == 42

    def test_linux_returns_0_on_missing_proc(self):
        """On Linux, returns 0 when /proc file doesn't exist."""
        with patch("platform.system", return_value="Linux"), patch(
            "pathlib.Path.read_text", side_effect=FileNotFoundError
        ):
            assert _get_ppid(99999) == 0

    def test_linux_returns_0_on_malformed(self):
        """On Linux, returns 0 when PPid line is missing.

        The malformed ``/proc/<pid>/status`` (no ``PPid:`` line) falls through
        to the last-resort ``ps`` fallback, so that MUST also be stubbed —
        otherwise the test reads the *real* ppid of pid 100 on the host (e.g.
        ``2``/kthreadd) and is non-deterministic. With ``ps`` unavailable the
        contract is: unresolved → 0.
        """
        with patch("platform.system", return_value="Linux"), patch(
            "pathlib.Path.read_text", return_value="Name:\ttest\nTgid:\t1\n"
        ), patch("subprocess.check_output", side_effect=OSError("ps unavailable")):
            assert _get_ppid(100) == 0

    def test_macos_prefers_libproc_over_ps(self):
        """On macOS, _get_ppid resolves via libproc and never spawns ps.

        This is the regression guard for the fix: the macOS app sandbox denies
        ps, so a ps-first (or ps-only) _get_ppid breaks parent-session-key
        resolution. libproc must win and ps must not be invoked.
        """
        with patch("platform.system", return_value="Darwin"), patch(
            "kiro_crew.mcp_core._ppid_via_libproc", return_value=42
        ) as mock_libproc, patch("subprocess.check_output") as mock_ps:
            assert _get_ppid(100) == 42
            mock_libproc.assert_called_once_with(100)
            mock_ps.assert_not_called()

    def test_macos_falls_back_to_ps_when_libproc_yields_zero(self):
        """On macOS, if libproc can't resolve the ppid, fall back to ps."""
        with patch("platform.system", return_value="Darwin"), patch(
            "kiro_crew.mcp_core._ppid_via_libproc", return_value=0
        ), patch("subprocess.check_output", return_value="  42\n"):
            assert _get_ppid(100) == 42

    def test_macos_returns_0_when_libproc_and_ps_both_fail(self):
        """On macOS, returns 0 when neither libproc nor ps can resolve."""
        with patch("platform.system", return_value="Darwin"), patch(
            "kiro_crew.mcp_core._ppid_via_libproc", return_value=0
        ), patch("subprocess.check_output", side_effect=Exception("no such process")):
            assert _get_ppid(99999) == 0


class TestPpidViaLibproc:
    @pytest.mark.skipif(platform.system() != "Darwin", reason="libproc is macOS-only")
    def test_resolves_real_ppid(self):
        """proc_pidinfo returns the true parent PID on macOS — no /proc, no ps.

        Exercises the real syscall so a struct-offset or ABI regression is
        caught. On a sandboxed macOS this is the only path that works.
        """
        assert _ppid_via_libproc(os.getpid()) == os.getppid()

    def test_returns_0_on_load_failure(self):
        """A ctypes/libproc failure degrades to 0 so the caller can fall back."""
        with patch("ctypes.CDLL", side_effect=OSError("cannot load libproc")):
            assert _ppid_via_libproc(os.getpid()) == 0
