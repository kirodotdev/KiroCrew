from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _make_executable, _no_audit

from kiro_crew import _process_group_supervisor as supervisor
from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.kiro_cli import (
    BUNDLED_KIRO_CLI_ENTRY,
    BUNDLED_KIRO_CLI_WINDOWS_ENTRY,
    resolve_kiro_cli,
)
from kiro_crew.kiro_prerequisite import (
    KIRO_CLI_LOGIN_COMMAND,
    KIRO_CLI_SSO_LOGIN_COMMAND,
    KiroPrerequisiteService,
    ProcessResult,
    find_kiro_cli_candidates,
    login_commands_for,
)


class TestHostSandboxReadiness:
    def test_probe_failure_payload_does_not_claim_host_sandbox(self) -> None:
        from kiro_crew.dashboard.handlers.kiro_prerequisite import _not_ready_snapshot

        assert _not_ready_snapshot()["sandbox_backend_available"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("floor,blocked", [(None, True), ("standard", False)])
    async def test_effective_off_tier_blocks_only_enforced_backends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, floor, blocked: bool
    ) -> None:
        from kiro_crew import sandbox

        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: floor)
        monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
        monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: "namespace")
        monkeypatch.setattr(
            prerequisite_module, "find_kiro_cli_candidates", lambda *args, **kwargs: []
        )
        service = KiroPrerequisiteService(home=tmp_path, environ={}, audit_writer=_no_audit)
        status = await service.snapshot(force=True)
        assert status["sandbox_backend_available"] is True
        assert ("codex" in status["sandbox_blocked_backends"]) is blocked
        assert "claude" not in status["sandbox_blocked_backends"]
        assert "kas" not in status["sandbox_blocked_backends"]
        assert "kiro-cli" not in status["sandbox_blocked_backends"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "platform_name,backend,available",
        [
            ("win32", "none", False),
            ("darwin", "sandbox-exec", True),
            ("linux", "namespace", True),
            ("linux", "none", False),
        ],
    )
    async def test_no_kiro_candidate_still_reports_host_sandbox(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        platform_name: str,
        backend: str,
        available: bool,
    ) -> None:
        from kiro_crew import sandbox

        loop_thread = threading.get_ident()

        def detect(**kwargs) -> str:
            assert threading.get_ident() != loop_thread
            return backend

        monkeypatch.setattr(sandbox, "detect_backend", detect)
        monkeypatch.setattr(
            prerequisite_module, "find_kiro_cli_candidates", lambda *args, **kwargs: []
        )
        service = KiroPrerequisiteService(
            platform_name=platform_name,
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        status = await service.snapshot(force=True)
        assert status["installed"] is False
        assert status["sandbox_unavailable"] is False
        assert status["sandbox_backend_available"] is available
        # The no-op repair response is cached by the browser as the same payload.
        repaired = await service.repair_agent_specs()
        assert repaired["sandbox_backend_available"] is available


class TestKiroPrerequisiteHelpers:
    def test_identity_file_lockdown_precedes_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staged identity file must never exist with content in it before
        it is locked down.

        ``_atomic_write_secret_bytes`` routes through
        ``atomic_write(restrict_to_owner=True)``, which applies the lockdown
        to the TEMP file before the identity bytes reach it (the previous
        post-rename lockdown left them readable under the inherited DACL on
        Windows for the write window). Asserted by measuring the
        file's SIZE at lockdown time — zero means no payload byte existed yet.
        """
        sizes: list[int] = []
        real_restrict = platform_compat.restrict_to_owner

        def _measuring(target):
            sizes.append(os.stat(target).st_size)
            return real_restrict(target)

        monkeypatch.setattr("kiro_crew.platform_compat.restrict_to_owner", _measuring)
        target = tmp_path / "staged" / "kiro-token.json"
        prerequisite_module._atomic_write_secret_bytes(target, b"identity-bytes")

        assert target.read_bytes() == b"identity-bytes"
        if platform_compat.IS_POSIX:
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert sizes, "premise: the lockdown ran at all"
        assert sizes[0] == 0, f"the file already held {sizes[0]} payload bytes at lockdown time"

    @pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
    def test_identity_env_forwards_proxy_configuration_and_refuses_secrets(
        self,
        monkeypatch: pytest.MonkeyPatch,
        windows: bool,
    ) -> None:
        """A proxy-only host's ``whoami`` reaches the IdP the way the user's shell does.

        Every spelling of the proxy set passes through the IDENTITY probe env —
        matching is exact on POSIX and the HTTP stacks disagree on which case
        they honour — while a non-allowlisted variable is still refused, so the
        allowlist does not silently widen into a passthrough. The
        ``--version``/``whoami`` split stays intact: the base probe environment
        carries neither the CLI's own credential nor the (possibly
        credentialed) proxy configuration, because ``--version`` is the first
        execution of an unvalidated candidate and needs no network.
        """
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
        proxies = {
            "ALL_PROXY": "socks5://proxy.example:1080",
            "HTTP_PROXY": "http://proxy.example:3128",
            "HTTPS_PROXY": "http://user:pass@proxy.example:3128",
            "NO_PROXY": "localhost,127.0.0.1",
            "all_proxy": "socks5://proxy.example:1080",
            "http_proxy": "http://proxy.example:3128",
            "https_proxy": "http://user:pass@proxy.example:3128",
            "no_proxy": "localhost,127.0.0.1",
        }
        secrets = {
            "AWS_SECRET_ACCESS_KEY": "secret",
            "GITHUB_TOKEN": "ghp_secret",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
        }
        environ = {"HOME": "/home/u", **proxies, **secrets}

        version_env = prerequisite_module._probe_env(environ, "/probe/search/path")
        identity_env = prerequisite_module._identity_probe_env(environ, version_env)

        # The unvalidated --version candidate never sees proxy configuration.
        for name in proxies:
            assert name not in version_env
        # whoami gets the full proxy set, values intact.
        for name, value in proxies.items():
            assert identity_env[name] == value
        # Non-allowlisted variables stay refused in both environments.
        for name in secrets:
            assert name not in version_env
            assert name not in identity_env
        # _probe_env pins PATH to the caller's search path regardless of the
        # allowlist admitting the host PATH.
        assert version_env["PATH"] == "/probe/search/path"
        assert identity_env["PATH"] == "/probe/search/path"

    def test_probe_env_keeps_the_cli_update_guard(self) -> None:
        """Readiness probes must not update the bundled executable in place."""

        version_env = prerequisite_module._probe_env(
            {"KIRO_NO_AUTO_UPDATE": "1"}, "/probe/search/path"
        )

        assert version_env["KIRO_NO_AUTO_UPDATE"] == "1"

    def test_binary_digest_rejects_oversized_candidate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executable = tmp_path / "kiro-cli"
        executable.write_bytes(b"oversized")
        executable.chmod(0o700)
        monkeypatch.setattr(prerequisite_module, "_MAX_AUTH_EXECUTABLE_BYTES", 4)

        with pytest.raises(OSError, match="bounded regular executable"):
            prerequisite_module._binary_sha256(str(executable))

    def test_binary_digest_preserves_windows_crlf_bytes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executable = tmp_path / "kiro-cli.exe"
        content = b"line one\r\nline two\r\n"
        executable.write_bytes(content)
        native_binary_flag = getattr(os, "O_BINARY", 0)
        binary_flag = native_binary_flag or 0x8000
        binary_fds: set[int] = set()
        real_open = os.open
        real_read = os.read

        # ``**kwargs`` because this replaces the os module attribute, so it is live for
        # the whole test INCLUDING teardown, where pytest's own tmp_path cleanup calls
        # os.open with dir_fd=. A stub narrower than the API it stands in for turns that
        # cleanup into a TypeError reported as an error at teardown.
        def windows_open(path: str, flags: int, mode: int = 0o777, **kwargs: object) -> int:
            real_flags = flags if native_binary_flag else flags & ~binary_flag
            fd = real_open(path, real_flags, mode, **kwargs)  # type: ignore[arg-type]
            if flags & binary_flag:
                binary_fds.add(fd)
            return fd

        def windows_read(fd: int, size: int) -> bytes:
            chunk = real_read(fd, size)
            if fd not in binary_fds:
                return chunk.replace(b"\r\n", b"\n")
            return chunk

        monkeypatch.setattr(prerequisite_module.os, "O_BINARY", binary_flag, raising=False)
        monkeypatch.setattr(prerequisite_module.os, "open", windows_open)
        monkeypatch.setattr(prerequisite_module.os, "read", windows_read)

        assert (
            prerequisite_module._binary_sha256(str(executable))
            == hashlib.sha256(content).hexdigest()
        )

    def test_windows_launches_resolved_candidate_in_place(
        self,
        tmp_path: Path,
    ) -> None:
        # Trust is "it runs": a Windows CLI outside Program Files (winget/scoop,
        # a venv Scripts dir, a user install) launches in place, not rejected.
        candidate = tmp_path / "venv" / "Scripts" / "kiro-cli.exe"
        _make_executable(candidate)

        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            str(candidate),
            platform_name="win32",
            environ={"ProgramFiles": str(tmp_path / "Program Files")},
        )

        assert snapshot.launch_path == os.path.realpath(str(candidate))

    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_launches_the_users_installed_binary_in_place(
        self,
        tmp_path: Path,
        platform_name: str,
    ) -> None:
        # The user's installed CLI is launched at its own path on every POSIX
        # platform — never copied into a private snapshot dir and run from there.
        executable = tmp_path / "kiro-cli"
        executable.write_bytes(b"installed cli bytes")
        executable.chmod(0o700)
        data_home = tmp_path / "data"
        data_home.mkdir()

        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            str(executable),
            data_home=data_home,
            platform_name=platform_name,
            environ={},
        )

        assert snapshot.launch_path == str(executable)
        # No copy anywhere under the data home.
        assert not list(data_home.rglob("kiro-cli*"))

    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_preserves_sibling_layout_for_multi_call_binary(
        self,
        tmp_path: Path,
        platform_name: str,
    ) -> None:
        # Regression: Kiro CLI 2.15+ is a multi-call binary that dispatches by
        # exec'ing a SIBLING executable (``kiro-cli-chat``) resolved relative to
        # its own path. Copying into a flat dir stranded the sibling and every
        # spawn died with "No such file or directory (os error 2)" →
        # "process exited (rc=None)". The launch path must therefore keep its
        # real directory, siblings intact.
        macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        executable = macos_dir / "kiro-cli"
        executable.write_bytes(b"multi-call dispatcher")
        executable.chmod(0o700)
        sibling = macos_dir / "kiro-cli-chat"
        sibling.write_bytes(b"subcommand payload")
        sibling.chmod(0o700)

        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            str(executable),
            data_home=tmp_path / "data",
            platform_name=platform_name,
            environ={},
        )

        launch = Path(snapshot.launch_path)
        assert launch == executable
        # The sibling the CLI exec's is reachable from the launch path.
        assert (launch.parent / "kiro-cli-chat").exists()
        assert launch.parent.name == "MacOS"

    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_keeps_multiplexer_symlink_path_not_realpath(
        self,
        tmp_path: Path,
        platform_name: str,
    ) -> None:
        # A multiplexer launcher (e.g. ~/.toolbox/bin/kiro-cli -> toolbox-exec)
        # dispatches on its argv[0] basename, so the launch path must stay the
        # caller's ``kiro-cli`` symlink rather than the realpath'd
        # ``toolbox-exec``, which would run as the wrong tool.
        real = tmp_path / "toolbox-exec"
        real.write_bytes(b"multiplexer bytes")
        real.chmod(0o700)
        symlink = tmp_path / "kiro-cli"
        symlink.symlink_to(real)

        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            str(symlink),
            data_home=tmp_path / "data",
            platform_name=platform_name,
            environ={},
        )

        assert snapshot.launch_path == str(symlink)
        assert Path(snapshot.launch_path).name == "kiro-cli"

    def test_launches_packaged_fake_backend_by_the_ordinary_path(
        self,
        tmp_path: Path,
    ) -> None:
        # The offline E2E harness's fake ACP backend needs no special-case
        # bypass any more: it is a runnable packaged entry point, so the
        # ordinary in-place path launches it. The test-mode marker grants no
        # launch privilege, so the result must not depend on it.
        import kiro_crew.testing as kc_testing

        fake = Path(kc_testing.__file__).with_name("fake_acp_backend.py")
        assert fake.is_file(), "packaged fake ACP backend is missing"

        without_marker = prerequisite_module.snapshot_trusted_acp_executable(
            str(fake),
            data_home=tmp_path,
            platform_name="darwin",
            environ={},
        )
        with_marker = prerequisite_module.snapshot_trusted_acp_executable(
            str(fake),
            data_home=tmp_path,
            platform_name="darwin",
            environ={
                "KIROCREW_KIRO_BIN": str(fake),
                prerequisite_module.FAKE_ACP_TEST_MODE_ENV: "1",
            },
        )

        assert without_marker.launch_path == str(fake)
        assert with_marker.launch_path == str(fake)

    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_anchors_relative_candidate_to_an_absolute_path(
        self,
        tmp_path: Path,
        platform_name: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An operator KIROCREW_KIRO_BIN=./kiro-cli resolves against the GATEWAY's
        # cwd, but ACP spawns with cwd=<session work_dir> — so a relative launch
        # path dies with ENOENT there. Anchor it, without resolving symlinks.
        executable = tmp_path / "kiro-cli"
        executable.write_bytes(b"#!/bin/sh\n")
        executable.chmod(0o755)
        monkeypatch.chdir(tmp_path)

        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            "./kiro-cli",
            data_home=tmp_path / "data",
            platform_name=platform_name,
            environ={},
        )

        assert os.path.isabs(snapshot.launch_path)
        assert Path(snapshot.launch_path) == executable

    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_anchoring_preserves_multiplexer_symlink(
        self,
        tmp_path: Path,
        platform_name: str,
    ) -> None:
        # Anchoring must use abspath, NOT realpath: resolving the symlink would
        # name the launch path ``toolbox-exec`` and break argv[0] dispatch.
        real = tmp_path / "toolbox-exec"
        real.write_bytes(b"#!/bin/sh\n")
        real.chmod(0o755)
        link_dir = tmp_path / "bin"
        link_dir.mkdir()
        symlink = link_dir / "kiro-cli"
        symlink.symlink_to(real)

        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            str(symlink),
            data_home=tmp_path / "data",
            platform_name=platform_name,
            environ={},
        )

        assert snapshot.launch_path == str(symlink)
        assert Path(snapshot.launch_path).name == "kiro-cli"

    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_rejects_zero_byte_candidate(
        self,
        tmp_path: Path,
        platform_name: str,
    ) -> None:
        # An interrupted install / self-update can leave a truncated but
        # executable kiro-cli. Exec'ing it dies without an ACP frame, producing
        # the same opaque "process exited (rc=None)" this module avoids, so it
        # must fail as a readable prerequisite error instead.
        truncated = tmp_path / "kiro-cli"
        truncated.write_bytes(b"")
        truncated.chmod(0o755)

        with pytest.raises(ValueError, match="not a runnable executable"):
            prerequisite_module.snapshot_trusted_acp_executable(
                str(truncated),
                data_home=tmp_path / "data",
                platform_name=platform_name,
                environ={},
            )

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason=(
            "execute-bit semantics are POSIX-only, so this rejection is "
            "unobservable on a Windows HOST: chmod(0o600) cannot clear a POSIX "
            "exec bit there, and is_executable_file() deliberately accepts any "
            "existing regular file for an explicit POSIX target (a Windows host "
            "cannot represent POSIX exec bits). The candidate is therefore always "
            "'runnable' and no ValueError is raised. The size-based rejection in "
            "test_rejects_zero_byte_candidate is platform-independent and keeps "
            "the not-runnable gate covered on Windows."
        ),
    )
    @pytest.mark.parametrize("platform_name", ["darwin", "linux"])
    def test_rejects_non_runnable_candidate(
        self,
        tmp_path: Path,
        platform_name: str,
    ) -> None:
        not_executable = tmp_path / "kiro-cli"
        not_executable.write_bytes(b"data")
        not_executable.chmod(0o600)

        with pytest.raises(ValueError, match="not a runnable executable"):
            prerequisite_module.snapshot_trusted_acp_executable(
                str(not_executable),
                data_home=tmp_path / "data",
                platform_name=platform_name,
                environ={},
            )

    def test_windows_candidate_includes_machine_wide_directory(self, tmp_path: Path) -> None:
        program_files = tmp_path / "Program Files"
        executable = program_files / "Kiro-Cli" / "kiro-cli.exe"
        _make_executable(executable)

        candidates = find_kiro_cli_candidates(
            "win32",
            tmp_path / "Users" / "new-user",
            {"ProgramFiles": str(program_files), "PATH": ""},
        )

        assert str(executable) in candidates

    def test_bundled_dir_ranks_above_system_installs(self, tmp_path: Path) -> None:
        """KIROCREW_BUNDLED_KIRO_DIR (the desktop app's own copy) wins over a
        user install, because the app was built against that exact version. The
        bundled POSIX candidate is the chat binary, the only file it ships."""
        bundled = tmp_path / "resources" / "kiro-cli" / BUNDLED_KIRO_CLI_ENTRY
        user_install = tmp_path / "home" / ".local" / "bin" / "kiro-cli"
        _make_executable(bundled)
        _make_executable(user_install)

        resolved = resolve_kiro_cli(
            platform_name="linux",
            home=tmp_path / "home",
            environ={"KIROCREW_BUNDLED_KIRO_DIR": str(bundled.parent), "PATH": ""},
        )

        assert resolved == str(bundled)

    def test_windows_bundled_dir_uses_the_msi_executable(self, tmp_path: Path) -> None:
        """Windows ranks the one executable extracted from the pinned MSI."""
        bundled = tmp_path / "resources" / "kiro-cli" / "kiro-cli.exe"
        local_app_data = tmp_path / "home" / "AppData" / "Local"
        user_install = local_app_data / "Kiro-Cli" / "kiro-cli.exe"
        _make_executable(bundled)
        _make_executable(user_install)

        resolved = resolve_kiro_cli(
            platform_name="win32",
            home=tmp_path / "home",
            environ={
                "KIROCREW_BUNDLED_KIRO_DIR": str(bundled.parent),
                "LOCALAPPDATA": str(local_app_data),
                "PATH": "",
            },
        )

        assert resolved == str(bundled)

    def test_a_bundled_dir_holding_only_the_launcher_is_not_a_candidate(
        self, tmp_path: Path
    ) -> None:
        """The ``kiro-cli`` launcher never finds the chat binary beside itself
        (it resolves through ``$HOME/.local/bin`` and ``PATH``), so a bundled
        directory that ships only the launcher would run the USER's copy or fail
        on a clean machine. It is therefore not a candidate at all; discovery
        falls through to the user's own install."""
        launcher_only = tmp_path / "resources" / "kiro-cli" / "kiro-cli"
        user_install = tmp_path / "home" / ".local" / "bin" / "kiro-cli"
        _make_executable(launcher_only)
        _make_executable(user_install)

        resolved = resolve_kiro_cli(
            platform_name="linux",
            home=tmp_path / "home",
            environ={"KIROCREW_BUNDLED_KIRO_DIR": str(launcher_only.parent), "PATH": ""},
        )

        assert resolved == str(user_install)

    def test_bundled_dir_is_pinned_for_off_path_spawns(self, tmp_path: Path) -> None:
        """The unattended spawns drop the inherited PATH but keep the bundled
        copy: it is set by the shell that starts the gateway, not by a directory
        an agent can plant a file in, so the pinned and interactive paths agree
        on which binary runs."""
        bundled = tmp_path / "resources" / "kiro-cli" / BUNDLED_KIRO_CLI_ENTRY
        on_path_only = tmp_path / "venv" / "bin" / "kiro-cli"
        _make_executable(bundled)
        _make_executable(on_path_only)

        resolved = resolve_kiro_cli(
            platform_name="linux",
            home=tmp_path / "home",
            environ={
                "KIROCREW_BUNDLED_KIRO_DIR": str(bundled.parent),
                "PATH": str(on_path_only.parent),
            },
            include_inherited_path=False,
        )

        assert resolved == str(bundled)

    def test_operator_override_still_beats_the_bundled_dir(self, tmp_path: Path) -> None:
        """KIROCREW_KIRO_BIN stays the highest-priority escape hatch: an
        operator can force a different binary even on a bundled install."""
        bundled = tmp_path / "resources" / "kiro-cli" / BUNDLED_KIRO_CLI_ENTRY
        forced = tmp_path / "operator" / "kiro-cli"
        _make_executable(bundled)
        _make_executable(forced)

        resolved = resolve_kiro_cli(
            platform_name="linux",
            home=tmp_path / "home",
            environ={
                "KIROCREW_KIRO_BIN": str(forced),
                "KIROCREW_BUNDLED_KIRO_DIR": str(bundled.parent),
                "PATH": "",
            },
        )

        assert resolved == str(forced)

    def test_bundled_login_command_carries_the_absolute_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bundled resolution serves sign-in commands the user can actually
        run: the bundled copy is not on their shell PATH, and the macOS
        resources path contains a space, so the path must be quoted."""
        bundled_dir = tmp_path / "Kiro Res" / "kiro-cli"
        binary = bundled_dir / BUNDLED_KIRO_CLI_ENTRY
        _make_executable(binary)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        login, sso, bundled = login_commands_for(
            str(binary), {"KIROCREW_BUNDLED_KIRO_DIR": str(bundled_dir)}
        )

        assert bundled is True
        assert login.endswith(" login")
        assert str(bundled_dir) in login.replace("'", "")
        assert "--license pro" in sso

    def test_windows_bundled_login_command_names_powershell(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bundled_dir = tmp_path / "Kiro Crew" / "kiro-cli"
        binary = bundled_dir / BUNDLED_KIRO_CLI_WINDOWS_ENTRY
        _make_executable(binary)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        login, sso, bundled = login_commands_for(
            str(binary), {"KIROCREW_BUNDLED_KIRO_DIR": str(bundled_dir)}
        )

        quoted = str(binary).replace("'", "''")
        # The typed command runs outside the gateway tree, so it must carry the
        # self-update switch itself; ``Set-Item`` survives an interactive
        # PowerShell's interpolation of the double-quoted -Command string.
        head = 'powershell.exe -NoProfile -Command "Set-Item Env:KIRO_NO_AUTO_UPDATE 1; '
        assert bundled is True
        assert login == f"{head}& '{quoted}' login\""
        assert sso == f"{head}& '{quoted}' login --use-device-flow --license pro\""
        assert "$env:" not in login

    def test_system_resolution_keeps_the_bare_login_command(self, tmp_path: Path) -> None:
        bundled_dir = tmp_path / "resources" / "kiro-cli"
        bundled_dir.mkdir(parents=True)

        login, sso, bundled = login_commands_for(
            "/usr/local/bin/kiro-cli",
            {"KIROCREW_BUNDLED_KIRO_DIR": str(bundled_dir)},
        )

        assert bundled is False
        assert login == KIRO_CLI_LOGIN_COMMAND
        assert sso == KIRO_CLI_SSO_LOGIN_COMMAND

    def test_off_path_override_serves_its_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``KIROCREW_KIRO_BIN`` the user's shell would not find as ``kiro-cli``
        gets the quoted absolute path, like the bundled copy: the bare command
        would fail or sign in a different install."""
        binary = tmp_path / "opt" / "kiro build" / "kiro-cli"
        _make_executable(binary)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        login, sso, bundled = login_commands_for(
            str(binary), {"KIROCREW_KIRO_BIN": str(binary), "PATH": "/nonexistent"}
        )

        assert bundled is False
        assert login == f"{shlex.quote(str(binary))} login"
        assert sso.startswith(shlex.quote(str(binary)))

    def test_override_that_is_the_path_kiro_cli_keeps_the_bare_command(
        self, tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / "bin"
        binary = bin_dir / "kiro-cli"
        _make_executable(binary)

        login, _sso, bundled = login_commands_for(
            str(binary), {"KIROCREW_KIRO_BIN": str(binary), "PATH": str(bin_dir)}
        )

        assert bundled is False
        assert login == KIRO_CLI_LOGIN_COMMAND

    def test_no_bundled_env_keeps_the_bare_login_command(self, tmp_path: Path) -> None:
        binary = tmp_path / "resources" / "kiro-cli" / "kiro-cli"
        _make_executable(binary)

        login, _sso, bundled = login_commands_for(str(binary), {})

        assert bundled is False
        assert login == KIRO_CLI_LOGIN_COMMAND

    def test_windows_candidates_include_standard_user_tool_directory(
        self,
        tmp_path: Path,
    ) -> None:
        home = tmp_path / "Users" / "new-user"
        managed_bin = home / ".local" / "bin"
        executable = managed_bin / "kiro-cli.exe"
        _make_executable(executable)

        candidates = find_kiro_cli_candidates(
            "win32",
            home,
            {
                "LOCALAPPDATA": str(tmp_path / "AppData" / "Local"),
                "ProgramFiles": str(tmp_path / "Program Files"),
                "PATH": "",
            },
        )

        assert str(executable) in candidates

    def test_windows_truncated_per_user_candidate_does_not_shadow_machine_wide(
        self,
        tmp_path: Path,
    ) -> None:
        local_app_data = tmp_path / "AppData" / "Local"
        truncated = local_app_data / "Kiro-Cli" / "kiro-cli.exe"
        truncated.parent.mkdir(parents=True)
        truncated.write_bytes(b"")
        machine_wide = tmp_path / "Program Files" / "Kiro-Cli" / "kiro-cli.exe"
        _make_executable(machine_wide)

        candidates = find_kiro_cli_candidates(
            "win32",
            tmp_path / "Users" / "new-user",
            {
                "LOCALAPPDATA": str(local_app_data),
                "ProgramFiles": str(tmp_path / "Program Files"),
                "PATH": "",
            },
        )

        assert candidates[0] == str(machine_wide)
        assert str(truncated) not in candidates

    @pytest.mark.asyncio
    async def test_windows_refresh_discovers_per_user_install_without_new_path(
        self,
        tmp_path: Path,
    ) -> None:
        local_app_data = tmp_path / "AppData" / "Local"
        executable = local_app_data / "Kiro-Cli" / "kiro-cli.exe"
        calls: list[tuple[str, list[str]]] = []

        async def run(command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append((command, args))
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="win32",
            environ={
                "LOCALAPPDATA": str(local_app_data),
                "PATH": "",
                "ProgramFiles": str(tmp_path / "Program Files"),
            },
            home=tmp_path / "Users" / "new-user",
            data_home=tmp_path / "data-home",
            process_runner=run,
            audit_writer=_no_audit,
        )

        missing = await service.snapshot(force=True)
        assert missing["installed"] is False

        # The native installer updates the user's PATH, but a running desktop
        # gateway keeps its old environment. A forced refresh must find the
        # install at its fixed per-user location without a process restart.
        _make_executable(executable)
        refreshed = await service.snapshot(force=True)

        assert refreshed["ready"] is True
        assert calls == [
            (str(executable), ["--version"]),
            (str(executable), ["whoami"]),
            (str(executable), ["acp", "--help"]),
        ]

    def test_windows_candidates_include_inherited_path(
        self,
        tmp_path: Path,
    ) -> None:
        # A winget/scoop/user Windows install on PATH (outside Program Files) is
        # a valid candidate for both ACP launch and setup discovery — trust is
        # "it runs". The shared resolver picks it up on win32 like every OS.
        planted = tmp_path / "user-install" / "kiro-cli.exe"
        _make_executable(planted)
        environ = {
            "ProgramFiles": str(tmp_path / "Program Files"),
            "PATH": str(planted.parent),
        }

        launch_candidates = find_kiro_cli_candidates(
            "win32",
            tmp_path / "Users" / "new-user",
            environ,
        )

        assert str(planted) in launch_candidates
        assert resolve_kiro_cli(
            platform_name="win32",
            home=tmp_path / "Users" / "new-user",
            environ=environ,
        ) == str(planted)

    def test_process_group_membership_ignores_zombies(self) -> None:
        assert supervisor._proc_stat_group_member(b"123 (child) S 1 42 0", 42)
        assert not supervisor._proc_stat_group_member(b"123 (child) Z 1 42 0", 42)
        assert supervisor._parse_ps_group_members(
            "100 42 S\n101 42 Z\n102 7 R\n",
            42,
            999,
        ) == {100}

    def test_supervisor_rlimit_spec_ignores_junk(self) -> None:
        """A malformed --rlimits= entry must never fail the spawn.

        Safe to run in-process precisely because nothing here resolves to a real
        rlimit, so no setrlimit call touches this test process.
        """
        supervisor._apply_rlimits("RLIMIT_DOES_NOT_EXIST:1")
        supervisor._apply_rlimits("RLIMIT_NOFILE:not-an-int")
        supervisor._apply_rlimits("")
        supervisor._apply_rlimits("garbage")

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="POSIX resource limits")
    def test_supervisor_applies_rlimits_and_child_inherits_them(self) -> None:
        """The post-exec replacement for preexec_fn actually enforces a ceiling.

        Runs the real supervisor the way ``_run_process`` does -- including
        ``start_new_session=True``, without which the supervisor waits on the
        caller's own process group and never exits -- and reads the limit from
        the exec'd grandchild, which is what the ceiling has to cover.
        """
        code = Path(supervisor.__file__).read_text(encoding="utf-8")
        probe = "import resource;print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
        real_python = os.path.realpath(sys.executable)

        def run(*extra: str) -> str:
            done = subprocess.run(  # noqa: S603 - fixed argv, test-local
                [sys.executable, "-I", "-c", code, *extra, real_python, "-c", probe],
                capture_output=True,
                text=True,
                timeout=60,
                start_new_session=True,
            )
            assert done.returncode == 0, done.stderr
            return done.stdout.strip()

        inherited = int(run())
        capped = int(run("--rlimits=RLIMIT_NOFILE:64"))
        assert capped == 64
        assert inherited != capped  # the cap came from the flag, not the host

    @pytest.mark.skipif(sys.platform != "linux", reason="oom_score_adj is Linux-only")
    def test_supervisor_biases_oom_score_without_any_rlimit_flag(self) -> None:
        """The OOM bias is independent of the rlimits and must not be gated on them.

        ``preexec_fn`` applied it on every spawn. Gating it on ``--rlimits=``
        would silently drop it for an operator who disables every limit.
        """
        code = Path(supervisor.__file__).read_text(encoding="utf-8")
        probe = "print(open('/proc/self/oom_score_adj').read().strip())"
        done = subprocess.run(  # noqa: S603 - fixed argv, test-local
            [
                sys.executable,
                "-I",
                "-c",
                code,
                os.path.realpath(sys.executable),
                "-c",
                probe,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            start_new_session=True,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "1000"

    def test_posix_candidates_are_discoverable_on_windows_host(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        candidates = find_kiro_cli_candidates(
            "linux",
            tmp_path,
            {"PATH": ""},
        )

        assert str(executable) in candidates


class TestConfigSetHintsForLauncher:
    """``config_set_hints_for_launcher``: the prefix each shell pastes before ``config set``."""

    @staticmethod
    def _hints(launcher: str, platform_name: str) -> dict[str, Any]:
        from kiro_crew.kiro_prerequisite_status import config_set_hints_for_launcher

        return config_set_hints_for_launcher(launcher, platform_name)

    @staticmethod
    def _prefixes(hints: dict[str, Any]) -> dict[str, str]:
        assert hints["version"] == 1
        assert hints["encoding"] == "utf-8-base64"
        return {row["id"]: row["prefix"] for row in hints["shells"]}

    def test_windows_lists_powershell_then_cmd(self) -> None:
        launcher = r"C:\Program Files\Kiro Crew\resources\bin\kirocrew.cmd"

        hints = self._hints(launcher, "win32")

        assert [row["id"] for row in hints["shells"]] == ["powershell", "cmd"]
        assert self._prefixes(hints) == {
            "powershell": f"& '{launcher}'",
            "cmd": f'"{launcher}"',
        }

    def test_windows_path_with_ampersand_stays_one_quoted_token(self) -> None:
        launcher = r"C:\a&b\kirocrew.cmd"

        prefixes = self._prefixes(self._hints(launcher, "win32"))

        assert prefixes["powershell"] == r"& 'C:\a&b\kirocrew.cmd'"
        assert prefixes["cmd"] == r'"C:\a&b\kirocrew.cmd"'

    def test_windows_apostrophe_is_doubled_for_powershell_only(self) -> None:
        launcher = r"C:\Users\O'Brien\kirocrew.cmd"

        prefixes = self._prefixes(self._hints(launcher, "win32"))

        assert prefixes["powershell"] == r"& 'C:\Users\O''Brien\kirocrew.cmd'"
        assert prefixes["cmd"] == r'"C:\Users\O' + "'" + r'Brien\kirocrew.cmd"'

    def test_a_caret_stays_inside_the_quotes_when_there_is_no_percent(self) -> None:
        launcher = r"C:\a^b\kirocrew.cmd"

        prefixes = self._prefixes(self._hints(launcher, "win32"))

        assert prefixes["cmd"] == r'"C:\a^b\kirocrew.cmd"'

    @pytest.mark.parametrize(
        ("launcher", "expected_powershell"),
        [
            (r"C:\100%\kirocrew.cmd", r"& 'C:\100%\kirocrew.cmd'"),
            (r"C:\%KC_MARK%\kirocrew.cmd", r"& 'C:\%KC_MARK%\kirocrew.cmd'"),
            (r"C:\%%\kirocrew.cmd", r"& 'C:\%%\kirocrew.cmd'"),
            (r"C:\a%b\kirocrew.CMD", r"& 'C:\a%b\kirocrew.CMD'"),
            (r"\\host\share\100%\kirocrew.bat", r"& '\\host\share\100%\kirocrew.bat'"),
            (
                r"C:\Program Files\100%\Scripts\kirocrew.exe",
                r"& 'C:\Program Files\100%\Scripts\kirocrew.exe'",
            ),
            (r"C:\Users\O'Brien%\kirocrew.cmd", r"& 'C:\Users\O''Brien%\kirocrew.cmd'"),
        ],
    )
    def test_a_percent_in_the_path_leaves_only_the_powershell_row(
        self, launcher: str, expected_powershell: str
    ) -> None:
        hints = self._hints(launcher, "win32")

        assert [row["id"] for row in hints["shells"]] == ["powershell"]
        assert self._prefixes(hints) == {"powershell": expected_powershell}

    @pytest.mark.parametrize(
        ("launcher", "expected_powershell"),
        [
            ("C:\\O\u2018Brien\\kirocrew.cmd", "& 'C:\\O\u2018\u2018Brien\\kirocrew.cmd'"),
            ("C:\\O\u2019Brien\\kirocrew.cmd", "& 'C:\\O\u2019\u2019Brien\\kirocrew.cmd'"),
            ("C:\\O\u201aBrien\\kirocrew.cmd", "& 'C:\\O\u201a\u201aBrien\\kirocrew.cmd'"),
            ("C:\\O\u201bBrien\\kirocrew.cmd", "& 'C:\\O\u201b\u201bBrien\\kirocrew.cmd'"),
            (
                "C:\\a'\u2018\u2019\u201a\u201b\\kirocrew.cmd",
                "& 'C:\\a''\u2018\u2018\u2019\u2019\u201a\u201a\u201b\u201b\\kirocrew.cmd'",
            ),
        ],
    )
    def test_every_powershell_single_quote_character_is_doubled(
        self, launcher: str, expected_powershell: str
    ) -> None:
        prefixes = self._prefixes(self._hints(launcher, "win32"))

        assert prefixes["powershell"] == expected_powershell
        assert prefixes["cmd"] == f'"{launcher}"'

    def test_a_double_quote_like_character_is_not_doubled_for_powershell(self) -> None:
        launcher = "C:\\a\u201cb\u201d\\kirocrew.cmd"

        prefixes = self._prefixes(self._hints(launcher, "win32"))

        assert prefixes["powershell"] == f"& '{launcher}'"

    @pytest.mark.parametrize(
        ("launcher", "expected_powershell"),
        [
            (r"C:\a!b\kirocrew.cmd", r"& 'C:\a!b\kirocrew.cmd'"),
            (r"C:\a!b\kirocrew.bat", r"& 'C:\a!b\kirocrew.bat'"),
            (r"C:\a!b\kirocrew.exe", r"& 'C:\a!b\kirocrew.exe'"),
            (
                r"C:\Program Files\Hi!\Scripts\kirocrew.exe",
                r"& 'C:\Program Files\Hi!\Scripts\kirocrew.exe'",
            ),
            (r"C:\x!y!z\kirocrew.cmd", r"& 'C:\x!y!z\kirocrew.cmd'"),
            (r"C:\100%!\kirocrew.cmd", r"& 'C:\100%!\kirocrew.cmd'"),
            (r"C:\a%b!c\kirocrew.CMD", r"& 'C:\a%b!c\kirocrew.CMD'"),
            (r"C:\100%!\kirocrew.exe", r"& 'C:\100%!\kirocrew.exe'"),
            (r"C:\Users\O'Brien!\kirocrew.cmd", r"& 'C:\Users\O''Brien!\kirocrew.cmd'"),
        ],
    )
    def test_a_bang_in_the_path_leaves_only_the_powershell_row(
        self, launcher: str, expected_powershell: str
    ) -> None:
        hints = self._hints(launcher, "win32")

        assert [row["id"] for row in hints["shells"]] == ["powershell"]
        assert self._prefixes(hints) == {"powershell": expected_powershell}

    @pytest.mark.parametrize("platform_name", ["linux", "darwin"])
    def test_a_bang_in_a_posix_path_is_left_to_sh_quoting(self, platform_name: str) -> None:
        launcher = "/opt/hi!/kirocrew"

        prefixes = self._prefixes(self._hints(launcher, platform_name))

        assert prefixes == {"posix": shlex.quote(launcher)}

    @pytest.mark.parametrize("platform_name", ["linux", "darwin"])
    def test_posix_has_one_row_quoted_for_sh(self, platform_name: str) -> None:
        launcher = "/opt/kiro crew/it's/kirocrew"

        hints = self._hints(launcher, platform_name)

        assert [row["id"] for row in hints["shells"]] == ["posix"]
        assert self._prefixes(hints)["posix"] == shlex.quote(launcher)

    def test_a_percent_in_a_posix_path_is_left_to_sh_quoting(self) -> None:
        launcher = "/opt/100%/kirocrew"

        prefixes = self._prefixes(self._hints(launcher, "linux"))

        assert prefixes == {"posix": shlex.quote(launcher)}

    def test_a_plain_posix_launcher_is_not_quoted(self) -> None:
        prefixes = self._prefixes(self._hints("/usr/bin/kirocrew", "linux"))

        assert prefixes == {"posix": "/usr/bin/kirocrew"}
