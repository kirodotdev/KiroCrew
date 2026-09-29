"""Tests for the client-side session-identity ladder reached via _resolve_session_key.

The ancestor PID walk these exercise lives once, in
``mcp_caller.resolve_own_identity``, which every client-side resolver shares,
so the seams are that module's: ``mcp_caller._parent_pid`` for the parent
lookup and ``kiro_crew.config.loader.config_dir`` for the mapping directory.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from kiro_crew.mcp_core import _resolve_session_key

_PPID = "kiro_crew.mcp_caller._parent_pid"
_CFG = "kiro_crew.config.loader.config_dir"


class TestResolveSessionKey:
    def test_env_var_takes_priority(self):
        """Env var is returned immediately without file I/O."""
        with patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-1-123"}):
            assert _resolve_session_key() == "dashboard:chat-1-123"

    def test_fresh_session_returns_env_without_walking_pids(self):
        """Fresh (non-pooled) session: the key is baked into the env at spawn
        (AcpClient._spawn sets KIROCREW_SESSION_KEY), so resolution returns it
        immediately and NEVER walks ancestor PIDs. Guards against regressing to
        a walk-first order, which would needlessly depend on the parent lookup
        (broken on sandboxed macOS) even when the env answer is already
        present."""
        with (
            patch.dict("os.environ", {"KIROCREW_SESSION_KEY": "dashboard:chat-9-999"}),
            patch(_PPID) as mock_ppid,
        ):
            assert _resolve_session_key() == "dashboard:chat-9-999"
            mock_ppid.assert_not_called()

    def test_warm_pool_session_resolves_via_pid_file(self, tmp_path):
        """Warm-pool session: the process is spawned keyless (empty env), then
        rekey writes session_pid_<pid>.txt. With no env var, resolution must
        fall through to the PID walk and find that mapping."""
        ppid = os.getppid()
        (tmp_path / f"session_pid_{ppid}.txt").write_text("dashboard:chat-warm-7")
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(_CFG, return_value=tmp_path):
            assert _resolve_session_key() == "dashboard:chat-warm-7"

    def test_pid_file_immediate_parent(self, tmp_path):
        """Finds PID file on immediate parent (cold-start case)."""
        ppid = os.getppid()
        (tmp_path / f"session_pid_{ppid}.txt").write_text("dashboard:chat-2-456")

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with patch.dict("os.environ", env, clear=True), patch(_CFG, return_value=tmp_path):
            assert _resolve_session_key() == "dashboard:chat-2-456"

    def test_ancestor_walk_finds_grandparent(self, tmp_path):
        """Walks up ancestors when immediate parent has no PID file."""
        (tmp_path / "session_pid_25.txt").write_text("dashboard:chat-3-789")

        # Mock: PID 100 -> parent 50 -> parent 25 (has file)
        def fake_get_ppid(pid):
            return {100: 50, 50: 25}.get(pid, 0)

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(_CFG, return_value=tmp_path),
            patch("os.getppid", return_value=100),
            patch(_PPID, side_effect=fake_get_ppid),
        ):
            assert _resolve_session_key() == "dashboard:chat-3-789"

    def test_returns_empty_when_no_file_found(self, tmp_path):
        """Returns empty string when ancestor chain reaches init."""

        def fake_get_ppid(pid):
            return {100: 1}.get(pid, 0)

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(_CFG, return_value=tmp_path),
            patch("os.getppid", return_value=100),
            patch(_PPID, side_effect=fake_get_ppid),
        ):
            assert _resolve_session_key() == ""

    def test_stops_on_ppid_failure(self, tmp_path):
        """Stops walking when the parent lookup returns 0 (failure)."""
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(_CFG, return_value=tmp_path),
            patch("os.getppid", return_value=99999),
            patch(_PPID, return_value=0),
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
        with (
            patch.dict("os.environ", env, clear=True),
            patch(_CFG, return_value=tmp_path),
            patch(_PPID, return_value=0),
        ):
            assert _resolve_session_key() == ""

    def test_handles_cycle_detection(self, tmp_path):
        """Stops if PID chain forms a cycle (prevents infinite loop)."""

        def fake_get_ppid(pid):
            return {100: 50, 50: 100}.get(pid, 0)

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(_CFG, return_value=tmp_path),
            patch("os.getppid", return_value=100),
            patch(_PPID, side_effect=fake_get_ppid),
        ):
            assert _resolve_session_key() == ""


class TestParentLookupSpawnsNothing:
    """The walk must resolve a parent pid without spawning a process.

    The macOS app sandbox denies ``ps`` (``Operation not permitted``), and a
    ``ps``-based lookup there broke parent-session resolution outright: spawned
    sub-agents resolved an empty key and trusted sessions grew spurious
    tool-approval cards. Two copies of the walk carried their own
    ``libproc``-then-``ps`` ladder to work around it. The shared walk delegates
    to ``platform_compat.get_ppid``, which uses ``/proc`` on Linux,
    ``libproc.proc_pidinfo`` on macOS and ``CreateToolhelp32Snapshot`` on
    Windows and has no ``ps`` branch at all — so the guard is now structural,
    and this pins it rather than the platform-specific preference order it
    replaced. ``platform_compat``'s own suite covers the per-platform results.
    """

    def test_walk_never_spawns_a_subprocess(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(_CFG, return_value=tmp_path),
            patch("os.getppid", return_value=os.getpid()),
            patch("subprocess.check_output", side_effect=AssertionError("must not spawn ps")),
            patch("subprocess.Popen", side_effect=AssertionError("must not spawn ps")),
            patch("subprocess.run", side_effect=AssertionError("must not spawn ps")),
        ):
            # Walks the real ancestry of this process and finds no mapping.
            assert _resolve_session_key() == ""

    def test_parent_lookup_normalises_failure_to_zero(self):
        """``platform_compat.get_ppid`` reports failure as -1; the walk's guard
        is ``while pid > 1``, so the seam must hand it 0 instead."""
        from kiro_crew import mcp_caller

        with patch("kiro_crew.platform_compat.get_ppid", return_value=-1):
            assert mcp_caller._parent_pid(1234) == 0
