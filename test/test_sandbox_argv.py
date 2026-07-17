"""Additional tests for kiro_crew.sandbox — wrap_argv, profiles, env scrubbing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.sandbox import (
    _CC_FILES,
    _SENSITIVE_ENV_PREFIXES,
    _STRICT_DIRS,
    _build_launcher_script,
    _build_seatbelt_profile,
    _resolve_real_kiro_bin,
    _ssh_supports_accept_new,
    detect_backend,
    namespace_argv,
    reset_backend,
    sandbox_exec_argv,
    wrap_argv,
)

# Several tests spawn real child interpreters (subprocess.run([sys.executable, ...]));
# pin the module to a dedicated xdist worker so concurrent cold-starts under -n auto
# don't starve each other / blow the 30s timeout. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")


@pytest.fixture(autouse=True)
def clean_backend():
    """Reset cached backend between tests."""
    reset_backend()
    yield
    reset_backend()


class TestDetectBackend:
    def test_off_mode(self):
        result = detect_backend(config_mode="off")
        assert result == "none"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=False)
    @patch("kiro_crew.sandbox._probe_sandbox_exec", return_value=False)
    def test_no_backend_available(self, mock_sb, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "none"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_linux_namespace(self, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "namespace"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=False)
    @patch("kiro_crew.sandbox._probe_sandbox_exec", return_value=True)
    def test_macos_sandbox_exec(self, mock_sb, mock_ns):
        result = detect_backend(config_mode="auto")
        assert result == "sandbox-exec"

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_caches_result(self, mock_ns):
        detect_backend(config_mode="auto")
        detect_backend(config_mode="auto")
        # Only probed once due to caching
        assert mock_ns.call_count == 1

    @patch("kiro_crew.sandbox._probe_unshare", return_value=True)
    def test_invalidates_on_mode_change(self, mock_ns):
        detect_backend(config_mode="auto")
        detect_backend(config_mode="off")
        # Second call with different mode should re-evaluate
        assert mock_ns.call_count == 1  # off doesn't probe


class TestWrapArgv:
    @patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True)
    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_no_sandbox_returns_original(self, mock_detect, mock_allow):
        argv = ["kiro-cli", "acp"]
        result, cleanup = wrap_argv(argv, mode="auto")
        assert result == argv
        assert cleanup is None

    def test_off_mode_returns_original(self):
        argv = ["kiro-cli", "acp"]
        result, cleanup = wrap_argv(argv, mode="off")
        assert result == argv
        assert cleanup is None

    @patch("kiro_crew.sandbox.detect_backend", return_value="namespace")
    @patch("kiro_crew.sandbox.namespace_argv")
    def test_namespace_backend(self, mock_ns_argv, mock_detect):
        mock_ns_argv.return_value = [sys.executable, "/tmp/launcher.py", "kiro-cli"]
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_ns_argv.assert_called_once_with(["kiro-cli"], "strict", strip_python_env=False)

    @patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec")
    @patch("kiro_crew.sandbox.sandbox_exec_argv")
    def test_sandbox_exec_backend(self, mock_sb_argv, mock_detect):
        mock_sb_argv.return_value = (["sandbox-exec", "-f", "/tmp/p.sb", "kiro-cli"], "/tmp/p.sb")
        result, cleanup = wrap_argv(["kiro-cli"], mode="strict")
        mock_sb_argv.assert_called_once_with(["kiro-cli"], "strict", strip_python_env=False)


class TestBuildSeatbeltProfile:
    def test_strict_denies_all_dirs(self):
        profile = _build_seatbelt_profile("strict")
        assert "(version 1)" in profile
        assert "(deny file-read*" in profile
        home = str(Path.home())
        for d in _STRICT_DIRS:
            assert os.path.join(home, d) in profile

    def test_strict_denies_ssh_write(self):
        profile = _build_seatbelt_profile("strict")
        assert "(deny file-write*" in profile
        assert ".ssh" in profile

    def test_standard_does_not_deny_aws(self):
        profile = _build_seatbelt_profile("standard")
        home = str(Path.home())
        # Standard mode doesn't hide .aws
        assert f'(subpath "{home}/.aws")' not in profile

    def test_cc_mode_skips_aws_on_macos(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        # CC mode on macOS doesn't hide .aws (credential_process needs it)
        assert f'(subpath "{home}/.aws")' not in profile

    def test_cc_mode_denies_individual_files(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        for f in _CC_FILES:
            assert os.path.join(home, f) in profile

    def test_cc_mode_skips_aws_dir(self):
        """CC mode does NOT deny .aws as a directory (credential_process needs it)."""
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        # .aws should not appear as a subpath deny
        assert f'(subpath "{home}/.aws")' not in profile

    # ── AVP-23427: hardlink bypass ──
    def test_strict_denies_hardlink_creation_to_dirs(self):
        """Each read-denied dir must ALSO deny file-link (hardlink) creation, so a
        sandboxed agent cannot mint a hardlink at a non-denied path (/tmp) that
        reads the same inode past the path-based file-read* deny."""
        profile = _build_seatbelt_profile("strict")
        home = str(Path.home())
        for d in _STRICT_DIRS:
            assert f'(deny file-link (subpath "{os.path.join(home, d)}"))' in profile

    def test_strict_denies_hardlink_to_individual_files(self):
        profile = _build_seatbelt_profile("strict")
        home = str(Path.home())
        for f in _CC_FILES:
            assert f'(deny file-link (literal "{os.path.join(home, f)}"))' in profile

    def test_strict_denies_hardlink_to_ssh(self):
        profile = _build_seatbelt_profile("strict")
        home = str(Path.home())
        assert f'(deny file-link (subpath "{os.path.join(home, ".ssh")}"))' in profile

    def test_cc_mode_denies_hardlink_to_files(self):
        profile = _build_seatbelt_profile("cc")
        home = str(Path.home())
        for f in _CC_FILES:
            assert f'(deny file-link (literal "{os.path.join(home, f)}"))' in profile

    def test_uses_valid_file_link_token_not_star(self):
        """``file-link*`` is NOT a valid SBPL token (unbound variable); the rule
        must use the bare ``file-link`` operation."""
        profile = _build_seatbelt_profile("strict")
        assert "(deny file-link " in profile
        assert "file-link*" not in profile


class TestBuildLauncherScript:
    def test_strict_script_contains_dirs(self):
        script = _build_launcher_script("strict")
        assert "SENSITIVE_DIRS" in script
        assert ".aws" in script
        assert ".gnupg" in script

    def test_standard_script_excludes_aws(self):
        script = _build_launcher_script("standard")
        # Standard dirs don't include .aws
        assert "HIDE_SSH = False" in script

    def test_cc_script_exposes_aws_config(self):
        script = _build_launcher_script("cc")
        assert ".aws/config" in script
        assert "EXPOSE_FILES" in script

    def test_script_scrubs_env_vars(self):
        script = _build_launcher_script("strict")
        for prefix in _SENSITIVE_ENV_PREFIXES:
            assert prefix in script

    def test_strips_self_dir_before_ctypes_import(self):
        """The sys.path hardening must run before the first shadowable import.

        Regression guard for the /tmp/struct.py shadowing outage: ctypes does
        ``from struct import calcsize`` at import time, so the launcher dir must
        be removed from sys.path *before* ``import ctypes``.
        """
        script = _build_launcher_script("strict")
        assert "sys.path[:]" in script
        assert script.index("sys.path[:]") < script.index("import ctypes")
        # sys must be imported first (it is a builtin and cannot be shadowed).
        assert script.index("import sys") < script.index("sys.path[:]")


class TestLauncherStdlibShadowing:
    """End-to-end: a sibling /tmp/struct.py must NOT crash the launcher.

    Hermetic — every poison file lives in pytest's isolated tmp_path subdir,
    never bare /tmp, so the running gateway's launcher (sys.path[0] == /tmp) is
    never affected by these tests.
    """

    # A drop-in stdlib name that ctypes -> struct.calcsize depends on.
    _POISON = "def calcsize(*a, **k):\n    raise RuntimeError('shadowed!')\n"

    def _run_launcher(self, script_dir: Path) -> subprocess.CompletedProcess:
        """Write the launcher into script_dir and run it with no args.

        With no command argv the launcher exits immediately after its imports
        and the ``if not argv`` guard — it never forks/unshares/execs. So this
        exercises exactly the import path that the outage crashed on, and
        nothing else.
        """
        launcher = script_dir / "launcher.py"
        launcher.write_text(_build_launcher_script("standard"))
        return subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True, text=True, timeout=30,
        )

    def test_prelude_removes_script_dir_from_syspath(self, tmp_path):
        """Deterministic proof of the mechanism, independent of struct caching.

        Runs the launcher's real generated prelude (everything up to the first
        ``import ctypes``) from a tmp dir, then dumps sys.path. The script's own
        directory — which CPython puts at sys.path[0] — must be gone afterwards.
        Unlike the struct e2e below, this does not depend on whether the
        interpreter pre-imports ``struct``, so it always discriminates the fix.
        """
        script = _build_launcher_script("standard")
        prelude = script[: script.index("import ctypes")]
        probe = tmp_path / "launcher.py"
        probe.write_text(prelude + "import json\nprint(json.dumps(sys.path))\n")
        result = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        import json
        paths = json.loads(result.stdout.strip().splitlines()[-1])
        assert str(tmp_path) not in paths, f"script dir not stripped: {paths}"
        assert "" not in paths, f"cwd entry not stripped: {paths}"

    def test_launcher_survives_sibling_struct_py(self, tmp_path):
        """With the fix, a sibling struct.py is ignored and imports succeed."""
        (tmp_path / "struct.py").write_text(self._POISON)
        result = self._run_launcher(tmp_path)
        # No-args launcher exits via sys.exit("...: no command given") AFTER all
        # imports succeed — so a clean "no command given" proves imports passed.
        assert "calcsize" not in result.stderr, result.stderr
        # The launcher binds Linux-only libc symbols (unshare) at module import
        # time; on non-Linux hosts it dies there, AFTER the shadowable stdlib
        # imports the fix guards, but BEFORE the argv guard. That still proves
        # the imports survived the poison; only the argv guard is unreachable.
        if "unshare" in result.stderr and "no command given" not in result.stderr:
            pytest.skip("launcher needs Linux-only libc unshare; not this host")
        assert "no command given" in result.stderr, (
            f"launcher did not reach the argv guard; stderr={result.stderr!r}"
        )

    def test_control_unstripped_launcher_would_crash(self, tmp_path):
        """Sanity: prove the poison is real — an un-hardened launcher DOES crash.

        Strips the hardening line so we don't silently ship a test that passes
        for the wrong reason. The poison only bites if the interpreter imports
        ``struct`` fresh (not already cached at startup); if a given build
        interpreter pre-caches ``struct``, the shadowing can't be demonstrated
        here, so we skip rather than red the build for an unrelated reason.
        """
        (tmp_path / "struct.py").write_text(self._POISON)
        hardened = _build_launcher_script("standard")
        unstripped = "\n".join(
            ln for ln in hardened.splitlines() if "sys.path[:]" not in ln
        )
        launcher = tmp_path / "launcher.py"
        launcher.write_text(unstripped)
        result = subprocess.run(
            [sys.executable, str(launcher)],
            capture_output=True, text=True, timeout=30,
        )
        if "no command given" in result.stderr:
            pytest.skip(
                "interpreter pre-caches 'struct'; sibling shadowing not "
                "reproducible here — positive test still guards the fix"
            )
        # Otherwise the shadowed struct broke the ctypes import -> launcher
        # died before reaching the argv guard, proving the poison is real.
        if ("calcsize" not in result.stderr) and ("shadowed!" not in result.stderr):
            preview = repr(result.stderr)[:120]
            pytest.skip(
                "struct shadowing not observable on this interpreter "
                f"(stderr={preview}); "
                "positive test (test_launcher_survives_sibling_struct_py) still guards the fix"
            )


class TestSandboxExecArgv:
    @patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "fake", "SSH_AUTH_SOCK": "/tmp/ssh"})
    def test_includes_env_unset_flags(self):
        argv, profile_path = sandbox_exec_argv(["kiro-cli", "acp"], "strict")
        try:
            assert "env" == argv[0]
            assert "-u" in argv
            assert "AWS_SECRET_ACCESS_KEY" in argv
            assert "SSH_AUTH_SOCK" in argv
            assert "sandbox-exec" in argv
            assert "-f" in argv
            assert profile_path is not None
            assert os.path.exists(profile_path)
        finally:
            if profile_path:
                os.unlink(profile_path)

    @patch.dict(os.environ, {"PYTHONPATH": "/opt/kirocrew/site-packages", "PYTHONHOME": "/opt/py"})
    def test_strips_python_env_when_requested(self):
        # A foreign Python subprocess (kiro-cli's MCP servers, e.g. ord-mcp) must
        # NOT inherit KiroCrew's PYTHONPATH/PYTHONHOME, or it prepends KiroCrew's
        # site-packages to sys.path and imports KiroCrew's fastmcp/cryptography
        # instead of its own. strip_python_env=True unsets them.
        argv, profile_path = sandbox_exec_argv(
            ["kiro-cli", "acp"], "strict", strip_python_env=True
        )
        try:
            assert "PYTHONPATH" in argv
            assert "PYTHONHOME" in argv
        finally:
            if profile_path:
                os.unlink(profile_path)

    @patch.dict(os.environ, {"PYTHONPATH": "/opt/kirocrew/site-packages", "PYTHONHOME": "/opt/py"})
    def test_preserves_python_env_by_default(self):
        # KiroCrew's OWN sandboxed Python subprocesses (cron scripts, app
        # backends, code-review workers) import kiro_crew via PYTHONPATH, so it
        # must be preserved when strip_python_env is not set (regression guard).
        argv, profile_path = sandbox_exec_argv(["python3", "worker.py"], "standard")
        try:
            assert "PYTHONPATH" not in argv
            assert "PYTHONHOME" not in argv
        finally:
            if profile_path:
                os.unlink(profile_path)

    def test_creates_temp_profile(self):
        argv, profile_path = sandbox_exec_argv(["echo", "hi"], "strict")
        try:
            assert profile_path is not None
            content = Path(profile_path).read_text()
            assert "(version 1)" in content
        finally:
            if profile_path:
                os.unlink(profile_path)


class TestNamespaceArgv:
    @patch("kiro_crew.sandbox._resolve_real_kiro_bin", return_value="/usr/local/bin/kiro-cli")
    def test_wraps_with_python_launcher(self, mock_resolve):
        result = namespace_argv(["kiro-cli", "acp"], "strict")
        assert result[0] == sys.executable
        assert result[1].endswith(".py")
        assert result[2] == "/usr/local/bin/kiro-cli"
        assert result[3] == "acp"
        # Cleanup temp file
        os.unlink(result[1])

    @patch("kiro_crew.sandbox._resolve_real_kiro_bin", return_value="/usr/local/bin/kiro-cli")
    def test_launcher_script_is_executable(self, mock_resolve):
        result = namespace_argv(["kiro-cli"], "strict")
        launcher_path = result[1]
        mode = os.stat(launcher_path).st_mode
        assert mode & 0o700 == 0o700
        os.unlink(launcher_path)


class TestSshSupportsAcceptNew:
    def test_modern_ssh(self):
        _ssh_supports_accept_new.cache_clear()
        mock_result = MagicMock(stderr=b"OpenSSH_9.2p1 Debian-2, OpenSSL 3.0.8")
        with patch("subprocess.run", return_value=mock_result):
            assert _ssh_supports_accept_new() is True
        _ssh_supports_accept_new.cache_clear()

    def test_old_ssh(self):
        _ssh_supports_accept_new.cache_clear()
        mock_result = MagicMock(stderr=b"OpenSSH_7.4p1, OpenSSL 1.0.2k")
        with patch("subprocess.run", return_value=mock_result):
            assert _ssh_supports_accept_new() is False
        _ssh_supports_accept_new.cache_clear()

    def test_ssh_not_found(self):
        _ssh_supports_accept_new.cache_clear()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _ssh_supports_accept_new() is False
        _ssh_supports_accept_new.cache_clear()


class TestResolveRealKiroBin:
    def test_non_kiro_binary_returns_unchanged(self):
        assert _resolve_real_kiro_bin("/usr/bin/python3") == "/usr/bin/python3"

    def test_kiro_cli_fallback_when_no_real_binary(self):
        with patch("subprocess.run", return_value=MagicMock(stdout=b"")):
            result = _resolve_real_kiro_bin("/usr/local/bin/kiro-cli")
        assert result == "/usr/local/bin/kiro-cli"


class TestSandboxNoWarningWhenExpected:
    """Mesh-2054: no WARNING for an *acknowledged* no-sandbox state.

    CSE SEC-009 makes an unacknowledged no-sandbox fallback a loud WARNING
    (covered in test_sandbox_no_isolation.py). When the operator has opted in
    via ``agent.sandbox_allow_no_isolation`` the message is demoted to INFO —
    this preserves Mesh-2054's "don't spam on expected states" intent.
    """

    @patch("kiro_crew.sandbox._allow_unsandboxed_exec", return_value=True)
    @patch("kiro_crew.sandbox._allow_no_isolation", return_value=True)
    @patch("kiro_crew.sandbox.detect_backend", return_value="none")
    def test_no_sandbox_opted_in_logs_info_not_warning(self, mock_detect, mock_optin, mock_allow, caplog):
        import logging
        if hasattr(wrap_argv, "_warned"):
            del wrap_argv._warned  # type: ignore[attr-defined]
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.sandbox"):
            wrap_argv(["kiro-cli", "acp"], mode="auto")
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        info_msgs = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "isolation" in r.message.lower()
        ]
        assert not warning_msgs, f"Expected no WARNING but got: {warning_msgs}"
        assert info_msgs, "Expected INFO about running without isolation"


class TestCleanupStaleSandboxProfiles:
    """Tests for cleanup_stale_sandbox_profiles()."""

    def test_removes_dead_pid_profile(self, tmp_path):
        """Profile file whose PID is dead gets removed."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        stale_file = run_dir / "kirocrew_sandbox_99999_abc123.sb"
        stale_file.write_text("(version 1)")

        with patch("kiro_crew.sandbox.os.path.expanduser", return_value=str(tmp_path)):
            with patch("kiro_crew.sandbox.os.kill", side_effect=OSError("No such process")):
                cleanup_stale_sandbox_profiles()

        assert not stale_file.exists()

    def test_preserves_live_pid_profile(self, tmp_path):
        """Profile file whose PID is alive (current process) is preserved."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        live_file = run_dir / f"kirocrew_sandbox_{os.getpid()}_xyz789.sb"
        live_file.write_text("(version 1)")

        with patch("kiro_crew.sandbox.os.path.expanduser", return_value=str(tmp_path)):
            cleanup_stale_sandbox_profiles()

        assert live_file.exists()

    def test_ignores_non_sandbox_files(self, tmp_path):
        """Files not matching kirocrew_sandbox_*.sb pattern are left alone."""
        from kiro_crew.sandbox import cleanup_stale_sandbox_profiles

        run_dir = tmp_path / ".kirocrew" / "run"
        run_dir.mkdir(parents=True)
        other_file = run_dir / "something_else.txt"
        other_file.write_text("keep me")

        with patch("kiro_crew.sandbox.os.path.expanduser", return_value=str(tmp_path)):
            cleanup_stale_sandbox_profiles()

        assert other_file.exists()
