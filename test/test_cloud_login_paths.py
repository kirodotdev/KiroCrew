"""The remote login log/PID/FIFO live in a private per-user directory.

These lock in the symlink-hardening invariant: every remote login script writes
the device-code / callback secrets into ``$KC_LOGIN_DIR`` under the user's home,
never a predictable /tmp name, and refuses to run unless that directory is a
real, owner-only, non-symlink directory.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from kiro_crew.cloud import login
from kiro_crew.cloud.login_target import KiroLoginTarget
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# The guard runs on a POSIX remote instance; the two tests that execute it need
# a POSIX bash plus POSIX mode bits and symlinks, so they skip elsewhere.
_BASH = shutil.which("bash") if os.name == "posix" else None
_needs_posix_bash = pytest.mark.skipif(
    _BASH is None, reason="requires a POSIX bash to execute the remote guard"
)

_IDC = KiroLoginTarget(
    license="pro", start_url="https://example.awsapps.com/start", region="us-east-1"
)

# Every generated remote login command that touches the paths. resume builds a
# device-login command internally, so its output must carry the guard too.
_PATH_COMMANDS = {
    "logout": login._logout_command(),
    "device_builder": login._device_login_command(replace_existing=True),
    "device_idc": login._device_login_command(replace_existing=False, **_IDC.login_kwargs()),
    "callback": login._callback_login_command(),
    "continue_callback": login._continue_callback_login_command(),
    "resume": login._resume_login_command(),
    "resume_idc": login._resume_login_command(**_IDC.login_kwargs()),
}


class TestPrivateLoginDir:
    @pytest.mark.parametrize("name", sorted(_PATH_COMMANDS))
    def test_guard_precedes_first_path_use(self, name):
        cmd = _PATH_COMMANDS[name]
        guard_at = cmd.find('KC_LOGIN_DIR="${HOME:?}/.kirocrew/login"')
        assert guard_at != -1, f"{name} is missing the login-dir guard"
        # First use of any of the three paths must come AFTER the guard.
        first_use = min(
            pos
            for pos in (
                cmd.find("$KC_LOGIN_DIR/kiro-login.log"),
                cmd.find("$KC_LOGIN_DIR/kiro-login.pid"),
                cmd.find("$KC_LOGIN_DIR/kiro-login.stdin"),
            )
            if pos != -1
        )
        assert guard_at < first_use, f"{name} uses a path before the guard"

    @pytest.mark.parametrize("name", sorted(_PATH_COMMANDS))
    def test_no_predictable_tmp_path(self, name):
        assert "/tmp/kirocrew-kiro-login" not in _PATH_COMMANDS[name]

    def test_guard_snippet_shape(self):
        guard = login._login_dir_guard()
        assert 'KC_LOGIN_DIR="${HOME:?}/.kirocrew/login"' in guard
        # Both levels are walked, parent first.
        assert 'for kc_dir in "${HOME:?}/.kirocrew" "$KC_LOGIN_DIR"' in guard
        # The symlink refusal comes before anything creates or chmods a path:
        # chmod follows links, so the order is the point.
        first_symlink_check = guard.index('[ -L "$kc_dir" ]')
        assert first_symlink_check < guard.index("mkdir")
        assert first_symlink_check < guard.index("chmod 0700")
        assert "mkdir -m 0700" in guard
        assert '[ ! -d "$kc_dir" ]' in guard
        assert '[ ! -O "$kc_dir" ]' in guard
        # A failing check must abort non-zero, not continue.
        assert "exit 1" in guard

    def test_constants_are_under_private_dir(self):
        assert login._LOGIN_LOG_PATH == "$KC_LOGIN_DIR/kiro-login.log"
        assert login._LOGIN_PID_PATH == "$KC_LOGIN_DIR/kiro-login.pid"
        assert login._LOGIN_FIFO_PATH == "$KC_LOGIN_DIR/kiro-login.stdin"

    @staticmethod
    def _run_guard(home) -> "subprocess.CompletedProcess[str]":
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
        return subprocess.run(
            [_BASH, "-c", login._login_dir_guard()],
            env=env,
            capture_output=True,
            **UTF8_TEXT,
        )

    @_needs_posix_bash
    def test_guard_creates_private_dir_with_bash(self, tmp_path):
        res = self._run_guard(tmp_path)
        assert res.returncode == 0, res.stderr
        for rel in (".kirocrew", ".kirocrew/login"):
            made = tmp_path / rel
            assert made.is_dir()
            mode = stat.S_IMODE(made.stat().st_mode)
            assert mode == 0o700, (rel, oct(mode))

    @_needs_posix_bash
    def test_guard_refuses_symlinked_dir_with_bash(self, tmp_path):
        # Plant a symlink where $KC_LOGIN_DIR should be a real directory: the
        # exact attack the guard exists to stop.
        (tmp_path / ".kirocrew").mkdir()
        elsewhere = tmp_path / "attacker"
        elsewhere.mkdir(mode=0o755)
        (tmp_path / ".kirocrew" / "login").symlink_to(elsewhere)
        res = self._run_guard(tmp_path)
        assert res.returncode != 0
        assert "refusing" in res.stderr.lower()
        # Refusal happens before chmod, so the link target's mode is untouched.
        assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755

    @_needs_posix_bash
    def test_guard_refuses_symlinked_parent_with_bash(self, tmp_path):
        # The parent level gets the same treatment: a link at $HOME/.kirocrew
        # is refused before the guard creates or chmods anything beneath it.
        elsewhere = tmp_path / "attacker"
        elsewhere.mkdir(mode=0o755)
        (tmp_path / ".kirocrew").symlink_to(elsewhere)
        res = self._run_guard(tmp_path)
        assert res.returncode != 0
        assert "refusing" in res.stderr.lower()
        assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
        assert not (elsewhere / "login").exists()

    @_needs_posix_bash
    def test_guard_tightens_a_loose_preexisting_dir_with_bash(self, tmp_path):
        # An owner-owned directory left group/world-writable is where a second
        # user could plant a log symlink; the guard must end with it at 0700.
        loose = tmp_path / ".kirocrew" / "login"
        loose.mkdir(parents=True)
        os.chmod(tmp_path / ".kirocrew", 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        os.chmod(loose, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        res = self._run_guard(tmp_path)
        assert res.returncode == 0, res.stderr
        for path in (tmp_path / ".kirocrew", loose):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

    @_needs_posix_bash
    def test_guard_refuses_when_chmod_fails_with_bash(self, tmp_path):
        # A chmod that fails (or leaves the mode loose) must stop the script,
        # since the redirects that follow would otherwise land in a directory
        # another user can write to.
        loose = tmp_path / ".kirocrew" / "login"
        loose.mkdir(parents=True)
        os.chmod(loose, 0o777)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        shim = tmp_path / "shim"
        shim.mkdir()
        fake_chmod = shim / "chmod"
        fake_chmod.write_text("#!/bin/sh\nexit 1\n")
        fake_chmod.chmod(0o755)
        env = {
            "HOME": str(tmp_path),
            "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        res = subprocess.run(
            [_BASH, "-c", login._login_dir_guard()],
            env=env,
            capture_output=True,
            **UTF8_TEXT,
        )
        assert res.returncode != 0
        assert "cannot make it private" in res.stderr
        # The loose directory stays loose, and the script did not continue.
        assert stat.S_IMODE(loose.stat().st_mode) == 0o777
