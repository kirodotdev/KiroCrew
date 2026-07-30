from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import _process_group_supervisor as supervisor
from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.dashboard.chat_handlers import api_chat, api_chat_slot_create
from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
)
from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.handlers.kiro_prerequisite import (
    api_kiro_prerequisite_install,
    api_kiro_prerequisite_login,
    api_kiro_prerequisite_status,
)
from kiro_crew.dashboard.kiro_readiness import kiro_session_ready
from kiro_crew.kiro_cli import resolve_kiro_cli
from kiro_crew.kiro_prerequisite import (
    OFFICIAL_INSTALL_URL,
    OFFICIAL_WINDOWS_INSTALL_URL,
    KiroPrerequisiteService,
    OperationStatus,
    ProcessResult,
    _installer_proxy,
    _run_process,
    _trusted_installer_path,
    _trusted_installer_url,
    extract_secure_login_url,
    find_kiro_cli_candidates,
    official_installer_command,
    validate_installer_script,
)


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o700)


class _FakeRuntime:
    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.installed = executable.exists()
        self.authenticated = False
        self.calls: list[tuple[str, list[str]]] = []
        self.sandboxed: list[bool | None] = []
        self.kwargs: list[dict[str, Any]] = []

    async def run(
        self,
        command: str,
        args: list[str],
        **kwargs: Any,
    ) -> ProcessResult:
        self.calls.append((command, args))
        self.sandboxed.append(kwargs.get("sandboxed"))
        self.kwargs.append(kwargs)
        if args == ["--version"]:
            return ProcessResult(ok=self.installed)
        if args == ["whoami"]:
            return ProcessResult(ok=self.authenticated)
        if args == ["login", "--use-device-flow"]:
            callback = kwargs.get("on_output")
            if callback:
                callback(
                    "Open https://view.awsapps.com/start/#/device?"
                    "user_code=ABCD-EFGH and enter code ABCD-EFGH\n"
                )
            self.authenticated = True
            return ProcessResult(ok=True)

        _make_executable(self.executable)
        self.installed = True
        return ProcessResult(ok=True, output="installed")


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


async def _wait_for_operation(service: KiroPrerequisiteService) -> None:
    task = service._task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)


class TestKiroPrerequisiteHelpers:
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

        def windows_open(path: str, flags: int, mode: int = 0o777) -> int:
            real_flags = flags if native_binary_flag else flags & ~binary_flag
            fd = real_open(path, real_flags, mode)
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

    def test_extract_secure_login_url_rejects_non_https(self) -> None:
        assert (
            extract_secure_login_url("Open https://view.awsapps.com/start/#/device?user_code=ABCD.")
            == "https://view.awsapps.com/start/#/device?user_code=ABCD"
        )
        assert extract_secure_login_url("Open http://example.test/device") == ""
        assert extract_secure_login_url("Open https://phishing.example/device") == ""
        assert extract_secure_login_url("Open https://app.kiro.dev.evil.test/device") == ""
        assert extract_secure_login_url("Open https://view.awsapps.com.evil.test/start") == ""
        assert extract_secure_login_url("Open https://view.awsapps.com/not-start") == ""
        assert extract_secure_login_url("Open https://evil.example\\@view.awsapps.com/start") == ""
        assert extract_secure_login_url("Open https://user@app.kiro.dev/device") == ""

    def test_installer_validation_is_digest_pinned(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        posix = b"#!/bin/bash\n# Kiro CLI Installation Script\n"
        windows = (
            b"# Kiro CLI Installation Script for Windows\n" b'$ErrorActionPreference = "Stop"\n'
        )
        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "posix",
            hashlib.sha256(posix).hexdigest(),
        )
        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "win32",
            hashlib.sha256(windows).hexdigest(),
        )

        assert validate_installer_script("linux", posix)
        assert validate_installer_script("win32", windows)
        assert not validate_installer_script("linux", posix + b"# modified\n")
        assert not validate_installer_script("linux", b"<html>error</html>")
        assert not validate_installer_script("win32", b"#!/bin/bash\n")

    def test_windows_installer_uses_fixed_system_powershell(self, tmp_path: Path) -> None:
        powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        _make_executable(powershell)
        plan = official_installer_command(
            "win32",
            {"SystemRoot": str(tmp_path)},
        )

        assert plan is not None
        assert plan[0] == str(powershell)
        assert plan[1][-2:] == ["-Command", "-"]
        assert "-NonInteractive" in plan[1]

    def test_installer_path_excludes_user_writable_discovery_dirs(self, tmp_path: Path) -> None:
        environ = {
            "HOME": str(tmp_path),
            "PATH": str(tmp_path / ".local" / "bin"),
            "SystemRoot": r"C:\Windows",
        }

        assert _trusted_installer_path("linux", environ) == "/usr/bin:/bin:/usr/sbin:/sbin"
        windows_path = _trusted_installer_path("win32", environ)
        assert str(tmp_path) not in windows_path
        assert windows_path.startswith(r"C:\Windows\System32")

    def test_installer_proxy_is_explicit_and_honors_no_proxy(self) -> None:
        environ = {
            "HTTPS_PROXY": "http://proxy.example:8443",
            "NO_PROXY": "localhost,.internal.example",
        }

        assert (
            _installer_proxy("https://cli.kiro.dev/install", environ) == "http://proxy.example:8443"
        )
        assert _installer_proxy("https://api.internal.example/install", environ) is None
        assert (
            _installer_proxy(
                "https://cli.kiro.dev/install",
                {"HTTPS_PROXY": "file:///tmp/proxy"},
            )
            is None
        )

    def test_installer_url_is_restricted_to_exact_official_endpoints(self) -> None:
        assert _trusted_installer_url("https://cli.kiro.dev/install")
        assert _trusted_installer_url("https://cli.kiro.dev/install.ps1")
        assert not _trusted_installer_url("https://evil.example/install")
        assert not _trusted_installer_url("https://cli.kiro.dev.evil.example/install")
        assert not _trusted_installer_url("https://cli.kiro.dev/other")
        assert not _trusted_installer_url("https://user@cli.kiro.dev/install")
        assert not _trusted_installer_url("https://cli.kiro.dev:444/install")
        assert not _trusted_installer_url("https://cli.kiro.dev/install?channel=other")

    @pytest.mark.asyncio
    async def test_installer_redirect_is_validated_before_destination_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested: list[str] = []

        async def redirect(request: web.Request) -> web.StreamResponse:
            requested.append(request.path)
            raise web.HTTPFound(location="/private")

        async def private(request: web.Request) -> web.Response:
            requested.append(request.path)
            return web.Response(body=b"must not be fetched")

        app = web.Application()
        app.router.add_get("/install", redirect)
        app.router.add_get("/private", private)

        async with TestServer(app) as server:
            installer_url = str(server.make_url("/install"))
            monkeypatch.setattr(
                prerequisite_module,
                "_trusted_installer_url",
                lambda candidate: candidate == installer_url,
            )

            with pytest.raises(RuntimeError, match="redirect left"):
                await prerequisite_module._download_installer(installer_url, {})

        assert requested == ["/install"]

    @pytest.mark.asyncio
    async def test_installer_download_follows_validated_redirect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested: list[str] = []

        async def redirect(request: web.Request) -> web.StreamResponse:
            requested.append(request.path)
            raise web.HTTPFound(location="/install.ps1")

        async def installer(request: web.Request) -> web.Response:
            requested.append(request.path)
            return web.Response(body=b"validated installer")

        app = web.Application()
        app.router.add_get("/install", redirect)
        app.router.add_get("/install.ps1", installer)

        async with TestServer(app) as server:
            install_url = str(server.make_url("/install"))
            windows_url = str(server.make_url("/install.ps1"))
            monkeypatch.setattr(
                prerequisite_module,
                "_trusted_installer_url",
                lambda candidate: candidate in {install_url, windows_url},
            )

            content = await prerequisite_module._download_installer(install_url, {})

        assert content == b"validated installer"
        assert requested == ["/install", "/install.ps1"]

    def test_windows_candidate_includes_official_msi_directory(self, tmp_path: Path) -> None:
        program_files = tmp_path / "Program Files"
        executable = program_files / "Kiro-Cli" / "kiro-cli.exe"
        _make_executable(executable)

        candidates = find_kiro_cli_candidates(
            "win32",
            tmp_path / "Users" / "new-user",
            {"ProgramFiles": str(program_files), "PATH": ""},
        )

        assert str(executable) in candidates

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
        assert supervisor._proc_stat_group_member("123 (child) S 1 42 0", 42)
        assert not supervisor._proc_stat_group_member("123 (child) Z 1 42 0", 42)
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

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS, reason="POSIX resource limits"
    )
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

    def test_install_output_does_not_become_a_login_link(self, tmp_path: Path) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._operation = OperationStatus(kind="install", status="running")

        service._capture_operation_output("Downloaded https://example.test/kiro.zip\n")

        assert service._operation.url == ""
        assert service._operation.message == ""

    def test_capture_operation_output_drops_progress_spinner(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._operation = OperationStatus(kind="login", status="running")

        # Real kiro-cli separates repaints with carriage returns; the frames here
        # deliberately mix a CR-delimited pair with a CR-less pair so neither
        # delimiter alone can carry the dedupe.
        service._capture_operation_output(
            "\x1b[?25l▰▱▱▱▱▱▱ Logging in... | Press (^) + C to cancel\r"
            "▰▰▱▱▱▱▱ Logging in... | Press (^) + C to cancel"
            "▰▰▰▱▱▱▱ Logging in... | Press (^) + C to cancel"
        )

        assert "▰" not in service._operation.detail
        assert "▱" not in service._operation.detail
        assert "\x1b" not in service._operation.detail
        # A repeated one-line spinner must not accumulate.
        assert service._operation.detail.count("Logging in") <= 1

    def test_capture_operation_output_keeps_device_code_line(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._operation = OperationStatus(kind="login", status="running")

        service._capture_operation_output(
            "Confirm the code: ABCD-1234\nhttps://view.awsapps.com/start/#/device\n"
        )

        assert "ABCD-1234" in service._operation.detail
        assert service._operation.url == "https://view.awsapps.com/start/#/device"

    def test_capture_operation_output_keeps_chunks_on_separate_lines(
        self,
        tmp_path: Path,
    ) -> None:
        # Each stream read is re-deduped over the WHOLE accumulated detail, so a
        # newline-terminated chunk must keep its terminator -- otherwise the next
        # chunk fuses onto the device-code line and the URL fallback can capture
        # trailing text.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._operation = OperationStatus(kind="login", status="running")

        service._capture_operation_output("Confirm the code: ABCD-1234\n")
        service._capture_operation_output("https://view.awsapps.com/start/#/device\n")
        service._capture_operation_output("Waiting for the browser confirmation...\n")

        lines = service._operation.detail.splitlines()
        assert lines == [
            "Confirm the code: ABCD-1234",
            "https://view.awsapps.com/start/#/device",
            "Waiting for the browser confirmation...",
        ]
        assert "ABCD-1234\nhttps://" in service._operation.detail
        assert service._operation.url == "https://view.awsapps.com/start/#/device"

    def test_capture_operation_output_keeps_install_progress_lines_separate(
        self,
        tmp_path: Path,
    ) -> None:
        # The installer emits plain multi-line progress with no spinner glyphs at
        # all; consecutive reads must not fuse into one unreadable line.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._operation = OperationStatus(kind="install", status="running")

        service._capture_operation_output("Downloading Kiro CLI...\nVerifying signature...\n")
        service._capture_operation_output("Installing to /usr/local/bin...\nDone.\n")

        assert service._operation.detail.splitlines() == [
            "Downloading Kiro CLI...",
            "Verifying signature...",
            "Installing to /usr/local/bin...",
            "Done.",
        ]

    def test_capture_operation_output_reassembles_url_split_across_chunks(
        self,
        tmp_path: Path,
    ) -> None:
        # One stream read can split the URL, so the accumulated-text fallback has
        # to keep working: a chunk with NO trailing newline must not gain one.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._operation = OperationStatus(kind="login", status="running")

        service._capture_operation_output("Open https://view.awsapps.com/start")
        service._capture_operation_output("/#/device\n")

        assert service._operation.url == "https://view.awsapps.com/start/#/device"


class TestKiroPrerequisiteWorkflow:
    @pytest.mark.asyncio
    async def test_missing_prerequisite_service_fails_closed(self) -> None:
        assert await kiro_session_ready(None) is False
        assert await kiro_session_ready(object()) is False

    @pytest.mark.asyncio
    async def test_missing_route_prerequisite_wiring_fails_closed(self) -> None:
        app = web.Application()
        app["state"] = SimpleNamespace()
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/chat/slots", json={})
            body = await response.json()

        assert response.status == 503
        assert body["code"] == "kiro_prerequisite_required"

    @pytest.mark.asyncio
    async def test_explicit_test_harness_mode_assumes_ready(self, tmp_path: Path) -> None:
        async def should_not_run(
            command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            del command, args, kwargs
            raise AssertionError("test harness readiness must not probe the host")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=should_not_run,
            audit_writer=_no_audit,
            assume_ready=True,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["authenticated"] is True
        assert status["ready"] is True
        assert status["initial_setup_complete"] is True

    @pytest.mark.asyncio
    async def test_user_owned_path_candidate_probes_version_then_whoami(
        self,
        tmp_path: Path,
    ) -> None:
        # A user-owned ``~/.local/bin`` CLI (the common non-root install) runs,
        # so it is eligible for sign-in: the probe first checks ``--version``
        # then ``whoami``. Trust is "runs + valid login", not root ownership.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        token = tmp_path / ".aws" / "sso" / "cache" / "kiro-auth-token-cli.json"
        token.parent.mkdir(parents=True)
        token.write_text('{"accessToken":"secret"}', encoding="utf-8")
        calls: list[list[str]] = []

        async def run(
            _command: str,
            args: list[str],
            **_kwargs: Any,
        ) -> ProcessResult:
            calls.append(args)
            return ProcessResult(ok=args == ["--version"])

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["can_login"] is True
        assert status["authenticated"] is False
        assert calls == [["--version"], ["whoami"]]

    @pytest.mark.asyncio
    async def test_whoami_runs_against_unresolved_multiplexer_path(
        self,
        tmp_path: Path,
    ) -> None:
        # A multiplexer launcher (toolbox) dispatches on argv[0] basename, so
        # whoami/login must run against the resolved-but-symlink-named candidate
        # (``kiro-cli``), NOT its realpath (``toolbox-exec``). Regression for the
        # toolbox "Command doesn't appear to be associated with any tool" error.
        real = tmp_path / "toolbox-exec"
        _make_executable(real)
        # A fixed home-relative dir the resolver checks first, so the real
        # host's toolbox install cannot leak in as an earlier candidate.
        symlink = tmp_path / ".local" / "bin" / "kiro-cli"
        symlink.parent.mkdir(parents=True)
        symlink.symlink_to(real)
        commands: list[str] = []

        async def run(command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            commands.append(command)
            # Only the tmp symlink is viable, so the real host binary (if it
            # leaks into discovery) is skipped and cannot shadow the assertion.
            return ProcessResult(ok=command == str(symlink) and args == ["--version"])

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["can_login"] is True
        # whoami runs against the symlink name, never the realpath'd toolbox-exec.
        assert str(symlink) in commands
        assert str(real) not in commands

    @pytest.mark.asyncio
    async def test_status_probe_failure_degrades_to_not_ready(
        self,
        tmp_path: Path,
    ) -> None:
        # A whoami that cannot even run (e.g. a wedged binary) must degrade to
        # not-authenticated, never raise — otherwise the status endpoint 500s
        # and the dashboard flashes the full-screen "could not check" gate.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            raise OSError("whoami could not spawn")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["can_login"] is True
        assert status["authenticated"] is False
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_version_probe_failure_degrades_to_not_installed(
        self,
        tmp_path: Path,
    ) -> None:
        # A --version probe that cannot even spawn (e.g. sandbox failure) must
        # degrade to not-installed, never raise — same 500-flash guard as the
        # whoami branch, on the other probe.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            raise OSError("--version could not spawn")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is False
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_identity_probe_runs_against_real_home_like_acp(
        self,
        tmp_path: Path,
    ) -> None:
        # The readiness whoami runs against the REAL home (like an ACP session),
        # not a credential-minimal rewritten home — so a CLI whose session or
        # tool registry lives in the real home is detected. Only Kiro Crew's own
        # secret home is hidden.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        whoami_homes: list[str] = []

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            home = kwargs["env"]["HOME"]
            whoami_homes.append(home)
            assert home == str(tmp_path)
            assert kwargs["extra_hidden_dirs"] == (
                str(tmp_path / ".kiro" / "crew"),
                str(tmp_path / ".kirocrew"),
            )
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["can_login"] is True
        assert status["authenticated"] is False
        # A single whoami, run against the real home (no isolated staging).
        assert whoami_homes == [str(tmp_path)]

    @pytest.mark.asyncio
    async def test_real_home_probe_detects_out_of_band_session(
        self,
        tmp_path: Path,
    ) -> None:
        # A CLI whose session/tool registry lives in the real home (e.g. a
        # toolbox multiplexer) reports signed-out under a rewritten HOME but is
        # logged in against the real home. The readiness whoami runs real-home
        # (like ACP), so it detects the live session and readiness is true.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        whoami_calls: list[str] = []

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            home = kwargs["env"]["HOME"]
            whoami_calls.append(home)
            # Signed-out under a rewritten HOME, signed-in against the real home.
            return ProcessResult(ok=home == str(tmp_path))

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["authenticated"] is True
        assert status["ready"] is True
        # A single whoami, run against the real home.
        assert whoami_calls == [str(tmp_path)]

    @pytest.mark.asyncio
    async def test_real_home_whoami_carries_full_session_env_like_acp(
        self,
        tmp_path: Path,
    ) -> None:
        # The real-home whoami mirrors an ACP session's environment, so session
        # vars the CLI's keyring needs reach it — e.g. DBUS_SESSION_BUS_ADDRESS /
        # XDG_RUNTIME_DIR for the secret-service keyring (AL2023). A curated
        # allowlist would drop them and break login detection.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        seen_env: dict[str, str] = {}

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            seen_env.update(kwargs["env"])
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
                "XDG_RUNTIME_DIR": "/run/user/4242",
            },
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        await service.snapshot(force=True)

        # Session env reached the whoami (unlike the minimal probe allowlist).
        assert seen_env.get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/run/user/4242/bus"
        assert seen_env.get("XDG_RUNTIME_DIR") == "/run/user/4242"

    @pytest.mark.asyncio
    async def test_version_probe_forwards_session_bus_vars(
        self,
        tmp_path: Path,
    ) -> None:
        # Some Kiro CLI builds connect to the D-Bus secret-service keyring at
        # startup even for `--version`, so the version probe must forward the
        # session-bus vars when the host sets them (AL2023) — otherwise
        # `--version` exits "Failed to connect to bus" and installed-detection
        # fails before the whoami check is ever reached. No-op where unset.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        version_env: dict[str, str] = {}

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            if args == ["--version"]:
                version_env.update(kwargs["env"])
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/9/bus",
                "XDG_RUNTIME_DIR": "/run/user/9",
            },
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert version_env.get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/run/user/9/bus"
        assert version_env.get("XDG_RUNTIME_DIR") == "/run/user/9"

    @pytest.mark.asyncio
    async def test_self_updated_candidate_still_signs_in(
        self,
        tmp_path: Path,
    ) -> None:
        # A Kiro CLI whose bytes changed after startup (its own self-updater ran
        # as the user) must still sign in — trust is "it runs + valid login",
        # not a pinned digest that a legitimate update invalidates.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = True

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )
        service._attest_candidate(str(executable))
        executable.write_text("#!/bin/sh\n# self-updated\n", encoding="utf-8")
        executable.chmod(0o700)

        status = await service.snapshot(force=True)

        assert status["can_login"] is True
        assert status["ready"] is True

    @pytest.mark.parametrize("payload", ("[]", "null"))
    def test_non_object_binary_trust_payload_is_ignored(
        self,
        tmp_path: Path,
        payload: str,
    ) -> None:
        # A malformed trust file must never crash the recorded-digest reader;
        # it simply yields no pinned digest (trust no longer depends on it).
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._binary_trust_path.parent.mkdir(parents=True, exist_ok=True)
        service._binary_trust_path.write_text(payload, encoding="utf-8")

        assert (
            prerequisite_module._recorded_trust_digest(
                service._binary_trust_path,
                str(executable),
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_runnable_path_cli_can_login_without_attestation(
        self,
        tmp_path: Path,
    ) -> None:
        # Trust model: a Kiro CLI that RUNS is eligible for sign-in, regardless
        # of install source (PATH / toolbox / Homebrew), owner, or attestation.
        # This mirrors a real toolbox/self-updated bundle: user-owned, no
        # trust-file, not on any official fixed path.
        executable = tmp_path / "toolbox" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = True

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)
        assert status["installed"] is True
        assert status["can_login"] is True
        assert status["ready"] is True
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_runnable_path_cli_not_signed_in_still_offers_login(
        self,
        tmp_path: Path,
    ) -> None:
        # Zezhen's exact stuck state: installed + runnable but no valid login.
        # Must offer sign-in (can_login=True), NOT a button-less dead end.
        executable = tmp_path / "toolbox" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = False

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)
        assert status["installed"] is True
        assert status["can_login"] is True
        assert status["ready"] is False
        assert status["repair_required"] is False

    def test_acp_snapshot_accepts_runnable_cli_without_provenance(
        self,
        tmp_path: Path,
    ) -> None:
        # The ACP launch gate must not refuse a runnable CLI for lack of
        # provenance, or sessions 503 even after a successful sign-in.
        if not sys.platform.startswith(("linux", "darwin")):
            pytest.skip("POSIX snapshot path")
        real = Path(sys.executable)  # a genuinely executable, user-owned file
        snapshot = prerequisite_module.snapshot_trusted_acp_executable(
            str(real),
            data_home=tmp_path,
            platform_name="linux" if sys.platform.startswith("linux") else "darwin",
            environ={},
        )
        assert snapshot.launch_path

    @pytest.mark.asyncio
    async def test_login_runs_against_real_home_without_staging(
        self,
        tmp_path: Path,
    ) -> None:
        """Login delegates fully to kiro-cli: no staged home, no copy-back."""
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        seen: dict[str, object] = {}

        async def runner(command, args, **kwargs):
            if args == ["--version"]:
                return ProcessResult(ok=True, output="kiro-cli 1.0")
            if args == ["whoami"]:
                # Signed out before login, signed in after it runs.
                return ProcessResult(ok=bool(seen.get("login")))
            if args[:1] == ["login"]:
                seen["login"] = True
                seen["env_home"] = kwargs["env"].get("HOME")
                return ProcessResult(ok=True)
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runner,
            audit_writer=_no_audit,
        )

        await service._login("tester")

        assert seen["login"] is True
        # The login child saw the user's REAL home, not a staging dir.
        assert seen["env_home"] == str(tmp_path)
        # No staging dir was left behind under the auth-staging parent.
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        assert list(staging.glob("auth-*")) == []
        assert service._operation.status == "succeeded"

    @pytest.mark.asyncio
    async def test_run_auth_command_no_longer_accepts_commit(
        self,
        tmp_path: Path,
    ) -> None:
        """The credential copy-back parameter is gone — kiro-cli owns its store."""
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=AsyncMock(return_value=ProcessResult(ok=True)),
            audit_writer=_no_audit,
        )

        with pytest.raises(TypeError, match="commit"):
            await service._run_auth_command(
                str(executable),
                ["login"],
                base_env={},
                timeout_secs=1,
                commit=True,
            )

    @pytest.mark.asyncio
    async def test_unreadable_identity_store_aborts_isolated_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A store that cannot be read safely is never staged, and nothing runs."""
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        live = tmp_path / ".local" / "share" / "kiro-cli" / "data.sqlite3"
        live.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(live)) as db:
            db.execute("create table identity(value text)")
            db.execute("insert into identity values ('original')")
            db.commit()
        original = live.read_bytes()
        monkeypatch.setattr(prerequisite_module, "_MAX_AUTH_STORE_FILE_BYTES", 1)
        probe = AsyncMock(return_value=ProcessResult(ok=True))

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=probe,
            audit_writer=_no_audit,
        )
        service._attest_candidate(str(executable))

        with pytest.raises(OSError, match="could not be read safely"):
            await service._run_auth_command(
                str(executable),
                ["whoami"],
                base_env={},
                timeout_secs=1,
            )

        probe.assert_not_awaited()
        assert live.read_bytes() == original
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        assert list(staging.glob("auth-*")) == []

    @pytest.mark.asyncio
    async def test_cancelled_isolated_probe_removes_staging_home(self, tmp_path: Path) -> None:
        """The staged probe home is a scratch dir: always removed, never published."""
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        token = tmp_path / ".aws" / "sso" / "cache" / "kiro-auth-token-cli.json"
        token.parent.mkdir(parents=True)
        token.write_text('{"accessToken":"original"}', encoding="utf-8")

        async def cancel_after_write(
            _command: str,
            _args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            staged = Path(kwargs["env"]["HOME"]) / ".aws" / "sso" / "cache" / token.name
            staged.write_text('{"accessToken":"partial"}', encoding="utf-8")
            raise asyncio.CancelledError

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=cancel_after_write,
            audit_writer=_no_audit,
        )
        service._attest_candidate(str(executable))

        with pytest.raises(asyncio.CancelledError):
            await service._run_auth_command(
                str(executable),
                ["whoami"],
                base_env={},
                timeout_secs=1,
            )
        assert token.read_text(encoding="utf-8") == '{"accessToken":"original"}'
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        assert list(staging.glob("auth-*")) == []

    @pytest.mark.asyncio
    async def test_probe_has_paired_audit_events_and_hides_crew_homes(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        events: list[dict[str, Any]] = []

        async def audit(**kwargs: Any) -> None:
            events.append(kwargs)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": "/usr/bin:/bin",
                "HTTPS_PROXY": "http://secret@proxy.example:8443",
                "DISPLAY": ":0",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            },
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=audit,
        )

        status = await service.snapshot(force=True)

        # A runnable CLI is eligible for sign-in, so the probe pairs a version
        # check with an identity (whoami) check; here whoami reports not-signed.
        assert [(item["action"], item["outcome"]) for item in events] == [
            ("probe_version", "invoked"),
            ("probe_version", "completed"),
            ("probe_identity", "invoked"),
            ("probe_identity", "failed"),
        ]
        assert status["can_login"] is True
        assert status["ready"] is False
        assert events[0]["critical"] is True
        assert all("secret" not in repr(item) for item in events)
        for index, call in enumerate(runtime.calls):
            if call[1] == ["--version"]:
                assert runtime.kwargs[index]["sandbox_mode"] == "strict"
                assert runtime.kwargs[index]["extra_hidden_dirs"] == (
                    str(tmp_path / ".kiro" / "crew"),
                    str(tmp_path / ".kirocrew"),
                    str(tmp_path / ".aws" / "sso" / "cache"),
                    str(tmp_path / ".local" / "share" / "kiro-cli"),
                    str(tmp_path / ".local" / "share" / "amazon-q"),
                )
                assert "HTTPS_PROXY" not in runtime.kwargs[index]["env"]
                assert "DISPLAY" not in runtime.kwargs[index]["env"]
                # The session bus IS forwarded now — some CLI builds connect to
                # the D-Bus secret-service keyring even at --version (AL2023).
                # Other desktop IPC / proxy vars stay excluded.
                assert (
                    runtime.kwargs[index]["env"].get("DBUS_SESSION_BUS_ADDRESS")
                    == "unix:path=/tmp/bus"
                )

    @pytest.mark.asyncio
    async def test_probe_does_not_spawn_when_invoked_audit_fails(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)

        async def broken_audit(**_kwargs: Any) -> None:
            raise OSError("audit unavailable")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=broken_audit,
        )

        with pytest.raises(OSError, match="audit unavailable"):
            await service.snapshot(force=True)
        assert runtime.calls == []

    @pytest.mark.asyncio
    async def test_probe_cache_ttl_starts_after_slow_probe(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        timestamps = iter((100.0, 110.0, 111.0))
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: next(timestamps),
        )

        await service.snapshot(force=True)
        calls_after_probe = len(runtime.calls)
        await service.snapshot()

        assert len(runtime.calls) == calls_after_probe

    @pytest.mark.asyncio
    async def test_paused_session_gate_reprobes_after_manual_login(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        now = [100.0]
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: now[0],
        )

        assert await service.session_ready() is False
        runtime.authenticated = True
        now[0] += prerequisite_module._SESSION_GUARD_REPROBE_SECS + 0.1

        # A stale not-ready value starts one background refresh without
        # blocking the session-start path on CLI subprocesses.
        assert await service.session_ready() is False
        assert service._session_probe_task is not None
        await service._session_probe_task

        assert await service.session_ready() is True
        # A runnable CLI is probed for identity every time (trust is "runs +
        # valid login"), so both probes run version then whoami.
        assert runtime.calls.count((str(executable), ["--version"])) == 2
        assert runtime.calls.count((str(executable), ["whoami"])) == 2

    @pytest.mark.asyncio
    async def test_close_cancels_background_session_probe(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        probe_started = asyncio.Event()
        probe_cancelled = asyncio.Event()

        async def blocking_runtime(
            command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            del command, args, kwargs
            probe_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                probe_cancelled.set()
                raise

        now = [100.0]
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=blocking_runtime,
            audit_writer=_no_audit,
            clock=lambda: now[0],
        )
        # Simulate a completed, VERIFIED probe: this test exercises the
        # background-refresh path, which is only reached once the session gate's
        # one-time real verification has already happened.
        service._has_probed = True
        service._has_verified = True
        service._last_probe_at = now[0]
        now[0] += prerequisite_module._SESSION_GUARD_REPROBE_SECS + 0.1

        assert await service.session_ready() is False
        await asyncio.wait_for(probe_started.wait(), timeout=1)
        task = service._session_probe_task
        assert task is not None

        await service.close()

        assert task.cancelled()
        assert probe_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_ready_session_gate_detects_later_sign_out_after_ttl(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True
        now = [100.0]
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: now[0],
        )
        service._attest_candidate(str(executable))

        assert await service.session_ready() is True
        runtime.authenticated = False
        now[0] += prerequisite_module._SESSION_GUARD_REPROBE_SECS + 0.1

        # The stale ready value never makes the chat hot path wait on CLI
        # subprocesses; the first guard starts one background refresh.
        assert await service.session_ready() is True
        assert service._session_probe_task is not None
        await service._session_probe_task

        assert await service.session_ready() is False
        assert runtime.calls.count((str(executable), ["whoami"])) == 2

    @pytest.mark.asyncio
    async def test_successful_auth_persists_first_run_completion(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True
        data_home = tmp_path / "data-home"
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=data_home,
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )
        service._attest_candidate(str(executable))

        ready = await service.snapshot(force=True)
        restarted = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=data_home,
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )

        assert ready["initial_setup_complete"] is True
        assert (data_home / prerequisite_module._SETUP_COMPLETE_FILENAME).is_file()
        assert restarted._initial_setup_complete is True

    def test_auto_created_config_does_not_skip_first_run_setup(self, tmp_path: Path) -> None:
        data_home = tmp_path / "data-home"
        data_home.mkdir()
        (data_home / "config.json").write_text("{}\n", encoding="utf-8")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=data_home,
            audit_writer=_no_audit,
        )

        assert service._initial_setup_complete is False

    def test_startup_created_empty_session_dirs_do_not_skip_first_run_setup(
        self,
        tmp_path: Path,
    ) -> None:
        data_home = tmp_path / "data-home"
        (data_home / "sessions").mkdir(parents=True)
        (data_home / "history").mkdir()
        (data_home / "sessions" / "empty.jsonl").touch()

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=data_home,
            audit_writer=_no_audit,
        )

        assert service._initial_setup_complete is False

    def test_nonempty_persisted_session_marks_installation_established(
        self,
        tmp_path: Path,
    ) -> None:
        data_home = tmp_path / "data-home"
        session = data_home / "sessions" / "existing.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text('{"role":"user"}\n', encoding="utf-8")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=data_home,
            audit_writer=_no_audit,
        )

        assert service._initial_setup_complete is True

    def test_established_installation_is_reportable_before_any_probe(
        self,
        tmp_path: Path,
    ) -> None:
        """A returning user is identifiable without running a CLI probe.

        The full-screen setup gate must never flash at a returning user. The
        first-run bit is derived from the data home at construction, so it is
        already known before the (multi-second, subprocess-backed) probe runs —
        expose it so the dashboard can skip setup chrome on the very first
        response.
        """

        data_home = tmp_path / "data-home"
        session = data_home / "sessions" / "existing.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text('{"role":"user"}\n', encoding="utf-8")

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=data_home,
            audit_writer=_no_audit,
        )

        assert service.initial_setup_complete is True
        assert service._has_probed is False

    @pytest.mark.asyncio
    async def test_warm_up_probes_in_background_without_blocking_caller(
        self,
        tmp_path: Path,
    ) -> None:
        """Boot-time warm-up moves the cold probe off the first request.

        The cold probe spawns sandboxed subprocesses and can take seconds. Doing
        it lazily on the dashboard's first status call is what made the setup
        gate visible long enough to read. Warming up at gateway start makes that
        first call a cache hit, without making startup itself wait.
        """

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runtime.run,
            audit_writer=_no_audit,
            warm_up_delay=0,
        )

        warm_up = service.warm_up()
        assert warm_up is not None
        await warm_up

        assert service._has_probed is True
        assert service._status.ready is True
        # The warmed result serves the first dashboard request from cache.
        before = len(runtime.calls)
        assert (await service.snapshot())["ready"] is True
        assert len(runtime.calls) == before

    @pytest.mark.asyncio
    async def test_session_gate_probes_once_then_reuses_the_result(
        self,
        tmp_path: Path,
    ) -> None:
        """The session gate probes once, not per turn — the hot path stays fast."""

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runtime.run,
            audit_writer=_no_audit,
        )

        assert await service.session_ready() is True
        after_first = len(runtime.calls)
        assert after_first, "the first session check must actually probe"

        # Subsequent turns reuse the verified state (within the freshness window).
        assert await service.session_ready() is True
        assert len(runtime.calls) == after_first

    @pytest.mark.asyncio
    async def test_warm_up_failure_is_contained(self, tmp_path: Path) -> None:
        """A failing warm-up must never take the gateway down."""

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            audit_writer=_no_audit,
            warm_up_delay=0,
        )

        async def exploding_probe(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("probe exploded")

        service._probe = exploding_probe  # type: ignore[method-assign]

        warm_up = service.warm_up()
        assert warm_up is not None
        await warm_up  # must not raise

    def test_first_run_home_reports_no_established_installation(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            audit_writer=_no_audit,
            warm_up_delay=0,
        )

        assert service.initial_setup_complete is False

    @pytest.mark.skipif(sys.platform == "win32", reason="requires the POSIX Kiro installer")
    @pytest.mark.asyncio
    async def test_linux_clean_install_then_device_login(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        runtime = _FakeRuntime(executable)
        downloaded: list[str] = []

        async def download(url: str) -> bytes:
            downloaded.append(url)
            return b"#!/bin/bash\n# Kiro CLI Installation Script\n"

        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "posix",
            hashlib.sha256(b"#!/bin/bash\n# Kiro CLI Installation Script\n").hexdigest(),
        )

        environ = {
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "https_proxy": "http://proxy.example:8443",
        }
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ=environ,
            home=tmp_path,
            process_runner=runtime.run,
            downloader=download,
            audit_writer=_no_audit,
        )

        initial = await service.snapshot(force=True)
        assert initial["installed"] is False
        assert initial["can_auto_install"] is True

        service.start_install("test-user")
        await _wait_for_operation(service)
        installed = await service.snapshot(force=True)
        assert downloaded == [OFFICIAL_INSTALL_URL]
        assert installed["installed"] is True
        assert installed["authenticated"] is False
        assert installed["operation"]["status"] == "succeeded"
        assert "KIROCREW_KIRO_BIN" not in environ
        assert all(
            sandboxed is True
            for call, sandboxed in zip(runtime.calls, runtime.sandboxed)
            if call[1] in (["--version"], ["whoami"])
        )
        whoami_indexes = [
            index for index, call in enumerate(runtime.calls) if call[1] == ["whoami"]
        ]
        assert whoami_indexes
        # The readiness whoami now runs against the real home (like ACP): the
        # standard sandbox, HOME left as the real home, and only Kiro Crew's own
        # secret home hidden (not the identity stores).
        assert all(runtime.kwargs[index]["sandbox_mode"] == "standard" for index in whoami_indexes)
        assert all(
            runtime.kwargs[index]["env"]["HOME"] == str(tmp_path) for index in whoami_indexes
        )
        assert all(
            runtime.kwargs[index]["extra_hidden_dirs"]
            == (
                str(tmp_path / ".kiro" / "crew"),
                str(tmp_path / ".kirocrew"),
            )
            for index in whoami_indexes
        )
        installer_index = next(
            index for index, call in enumerate(runtime.calls) if call[1] == ["-s"]
        )
        assert runtime.kwargs[installer_index]["stdin_data"].startswith(b"#!/bin/bash")
        assert runtime.kwargs[installer_index]["env"]["PATH"] == ("/usr/bin:/bin:/usr/sbin:/sbin")
        assert runtime.kwargs[installer_index]["env"]["https_proxy"] == "http://proxy.example:8443"
        assert (
            str(tmp_path / ".local" / "bin") not in runtime.kwargs[installer_index]["env"]["PATH"]
        )

        service.start_login("test-user")
        await _wait_for_operation(service)
        ready = await service.snapshot(force=True)
        assert ready["ready"] is True
        assert ready["operation"]["status"] == "succeeded"
        assert (str(executable), ["login", "--use-device-flow"]) in runtime.calls
        login_index = runtime.calls.index((str(executable), ["login", "--use-device-flow"]))
        assert runtime.sandboxed[login_index] is True
        assert runtime.kwargs[login_index]["sandbox_mode"] == "standard"
        assert runtime.kwargs[login_index]["env"]["https_proxy"] == ("http://proxy.example:8443")
        # Sign-in is fully delegated: kiro-cli runs against the REAL home and
        # writes its own credential store there, as it does from a terminal.
        assert runtime.kwargs[login_index]["env"]["HOME"] == str(tmp_path)
        assert runtime.kwargs[login_index]["extra_hidden_dirs"] == (
            str(tmp_path / ".kiro" / "crew"),
            str(tmp_path / ".kirocrew"),
        )
        assert list((tmp_path / ".kiro" / "crew-auth-staging").glob("auth-*")) == []
        for index, call in enumerate(runtime.calls):
            if call[1] in (["--version"], ["whoami"]):
                assert "https_proxy" not in runtime.kwargs[index]["env"]
                assert "DISPLAY" not in runtime.kwargs[index]["env"]

    @pytest.mark.asyncio
    async def test_windows_clean_install_uses_powershell_script(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        system_root = tmp_path / "Windows"
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        _make_executable(powershell)
        program_files = tmp_path / "Program Files"
        executable = program_files / "Kiro-Cli" / "kiro-cli.exe"
        runtime = _FakeRuntime(executable)
        downloaded: list[str] = []

        installer_bytes = (
            b"# Kiro CLI Installation Script for Windows\n" b'$ErrorActionPreference = "Stop"\n'
        )
        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "win32",
            hashlib.sha256(installer_bytes).hexdigest(),
        )

        async def download(url: str) -> bytes:
            downloaded.append(url)
            return installer_bytes

        service = KiroPrerequisiteService(
            platform_name="win32",
            environ={
                "HOME": str(tmp_path),
                "PATH": "",
                "ProgramFiles": str(program_files),
                "SystemRoot": str(system_root),
            },
            home=tmp_path,
            process_runner=runtime.run,
            downloader=download,
            audit_writer=_no_audit,
        )

        service.start_install("test-user")
        await _wait_for_operation(service)
        status = await service.snapshot(force=True)

        assert downloaded == [OFFICIAL_WINDOWS_INSTALL_URL]
        assert status["installed"] is True
        installer_index = next(
            index for index, call in enumerate(runtime.calls) if call[0] == str(powershell)
        )
        installer_call = runtime.calls[installer_index]
        assert installer_call[1][-2:] == ["-Command", "-"]
        assert runtime.kwargs[installer_index]["stdin_data"] == installer_bytes
        assert (
            str(tmp_path / ".local" / "bin") not in runtime.kwargs[installer_index]["env"]["PATH"]
        )

    @pytest.mark.asyncio
    async def test_cargo_install_is_usable_without_reattestation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A ``~/.cargo/bin`` install that runs is directly usable — no
        # "repair", no forced reinstall. Clicking Install reports it is already
        # installed rather than reinstalling to earn a provenance pin.
        cargo_executable = tmp_path / ".cargo" / "bin" / "kiro-cli"
        _make_executable(cargo_executable)
        runtime = _FakeRuntime(cargo_executable)
        runtime.installed = True
        runtime.authenticated = True
        installer = b"#!/bin/bash\n# Kiro CLI Installation Script\n"
        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "posix",
            hashlib.sha256(installer).hexdigest(),
        )
        monkeypatch.setattr(
            prerequisite_module,
            "official_installer_command",
            lambda _platform, _environ: ("bash", ["-s"]),
        )

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=runtime.run,
            downloader=lambda _url: asyncio.sleep(0, result=installer),
            audit_writer=_no_audit,
        )
        before = await service.snapshot(force=True)
        assert before["installed"] is True
        assert before["can_login"] is True
        assert before["repair_required"] is False

        service.start_install("test-user")
        await _wait_for_operation(service)
        after = await service.snapshot(force=True)

        assert after["can_login"] is True
        assert after["operation"]["status"] == "succeeded"
        assert "already installed" in after["operation"]["message"]

    @pytest.mark.asyncio
    async def test_installer_refuses_shadowing_candidate_instead_of_attesting_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # linux keeps candidate discovery to home-relative dirs (no real system
        # install can leak in). The installer's official target lands in a dir
        # that is not searched, while ``.local/bin`` shadows it as the first
        # resolved candidate — the install-quality guard must refuse rather than
        # bless the shadow, even though the shadow itself runs.
        installer = b"#!/bin/bash\n# Kiro CLI Installation Script\n"
        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "posix",
            hashlib.sha256(installer).hexdigest(),
        )
        monkeypatch.setattr(
            prerequisite_module,
            "_official_install_target",
            lambda _platform, _home, _environ: str(tmp_path / "official" / "kiro-cli"),
        )
        monkeypatch.setattr(
            prerequisite_module,
            "_interactive_repair_required",
            lambda _platform, _candidates, _home: False,
        )
        monkeypatch.setattr(
            prerequisite_module,
            "official_installer_command",
            lambda _platform, _environ: ("bash", ["-s"]),
        )
        shadow_executable = tmp_path / ".local" / "bin" / "kiro-cli"
        official_executable = tmp_path / "official" / "kiro-cli"
        installed = {"done": False}

        async def run(command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                # Only the tmp-path shadow is viable, and only after install —
                # so no real system CLI on the host can leak into discovery.
                return ProcessResult(ok=installed["done"] and command == str(shadow_executable))
            installed["done"] = True
            _make_executable(shadow_executable)
            _make_executable(official_executable)
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            process_runner=run,
            downloader=lambda _url: asyncio.sleep(0, result=installer),
            audit_writer=_no_audit,
        )

        service.start_install("test-user")
        await _wait_for_operation(service)

        assert service._operation.status == "failed"
        assert "shadowed" in service._operation.error
        assert not service._binary_trust_path.exists()

    @pytest.mark.asyncio
    async def test_installer_refuses_unchanged_existing_target(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        # The target exists but is not runnable yet (so Install is entered, not
        # short-circuited). The installer "runs" but leaves the exact same bytes
        # in place; the guard must refuse a no-op install rather than pin it.
        runtime = _FakeRuntime(executable)
        runtime.installed = False
        installer = b"#!/bin/bash\n# Kiro CLI Installation Script\n"
        monkeypatch.setitem(
            prerequisite_module._INSTALLER_SHA256,
            "posix",
            hashlib.sha256(installer).hexdigest(),
        )
        monkeypatch.setattr(
            prerequisite_module,
            "_interactive_repair_required",
            lambda _platform, _candidates, _home: False,
        )
        monkeypatch.setattr(
            prerequisite_module,
            "official_installer_command",
            lambda _platform, _environ: ("bash", ["-s"]),
        )

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            process_runner=runtime.run,
            downloader=lambda _url: asyncio.sleep(0, result=installer),
            audit_writer=_no_audit,
        )

        service.start_install("test-user")
        await _wait_for_operation(service)

        assert service._operation.status == "failed"
        assert "did not replace" in service._operation.error
        assert not service._binary_trust_path.exists()

    @pytest.mark.asyncio
    async def test_probe_does_not_skip_broken_first_acp_candidate(self, tmp_path: Path) -> None:
        first = tmp_path / ".local" / "bin" / "kiro-cli"
        second = tmp_path / ".cargo" / "bin" / "kiro-cli"
        _make_executable(first)
        _make_executable(second)
        calls: list[str] = []

        async def run(command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append(command)
            return ProcessResult(ok=command == str(second))

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )
        status = await service.snapshot(force=True)

        assert calls == [str(first)]
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_windows_override_that_runs_is_used_for_setup(
        self,
        tmp_path: Path,
    ) -> None:
        # Trust is "it runs": a Windows override outside Program Files (a winget/
        # user install) that answers --version is probed and usable — no
        # Program-Files restriction. (ACP launches the same override in place.)
        planted = tmp_path / "user-install" / "kiro-cli.exe"
        _make_executable(planted)
        calls: list[tuple[str, list[str]]] = []

        async def run(command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append((command, args))
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="win32",
            environ={
                "HOME": str(tmp_path),
                "PATH": "",
                "ProgramFiles": str(tmp_path / "Program Files"),
                "KIROCREW_KIRO_BIN": str(planted),
            },
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["can_login"] is True
        assert status["ready"] is True
        assert calls == [
            (str(planted), ["--version"]),
            (str(planted), ["whoami"]),
        ]

    @pytest.mark.asyncio
    async def test_windows_override_takes_priority_over_program_files_candidate(
        self,
        tmp_path: Path,
    ) -> None:
        # The explicit override wins over a Program Files install (ACP resolves
        # the override first), and being outside Program Files no longer blocks
        # it — it is probed and usable because it runs.
        planted = tmp_path / "user-install" / "kiro-cli.exe"
        official = tmp_path / "Program Files" / "Kiro-Cli" / "kiro-cli.exe"
        _make_executable(planted)
        _make_executable(official)
        calls: list[tuple[str, list[str]]] = []

        async def run(command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append((command, args))
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="win32",
            environ={
                "HOME": str(tmp_path),
                "PATH": "",
                "ProgramFiles": str(tmp_path / "Program Files"),
                "KIROCREW_KIRO_BIN": str(planted),
            },
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["ready"] is True
        assert calls == [
            (str(planted), ["--version"]),
            (str(planted), ["whoami"]),
        ]

    @pytest.mark.asyncio
    async def test_missing_windows_override_does_not_shadow_program_files_candidate(
        self,
        tmp_path: Path,
    ) -> None:
        missing = tmp_path / "missing" / "kiro-cli.exe"
        official = tmp_path / "Program Files" / "Kiro-Cli" / "kiro-cli.exe"
        _make_executable(official)
        calls: list[tuple[str, list[str]]] = []

        async def run(command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            calls.append((command, args))
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="win32",
            environ={
                "HOME": str(tmp_path),
                "PATH": "",
                "ProgramFiles": str(tmp_path / "Program Files"),
                "KIROCREW_KIRO_BIN": str(missing),
            },
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["ready"] is True
        assert calls == [
            (str(official), ["--version"]),
            (str(official), ["whoami"]),
        ]

    @pytest.mark.asyncio
    async def test_broken_linux_target_requires_manual_repair(self, tmp_path: Path) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)

        async def always_fail(
            command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            del command, args, kwargs
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=always_fail,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert status["installed"] is False
        assert status["repair_required"] is True
        assert status["can_auto_install"] is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows cannot represent POSIX execute-bit semantics",
    )
    @pytest.mark.asyncio
    async def test_non_executable_linux_target_requires_manual_repair(
        self,
        tmp_path: Path,
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        executable.parent.mkdir(parents=True)
        executable.write_text("damaged", encoding="utf-8")
        executable.chmod(0o600)
        run_calls: list[str] = []

        async def should_not_run(
            command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            del args, kwargs
            run_calls.append(command)
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=should_not_run,
            audit_writer=_no_audit,
        )

        status = await service.snapshot(force=True)

        assert str(executable) not in run_calls
        assert status["installed"] is False
        assert status["repair_required"] is True
        assert status["can_auto_install"] is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows accepts only the fixed Program Files candidate",
    )
    @pytest.mark.asyncio
    async def test_probe_process_routes_planted_binary_through_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        class _EmptyStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _Process:
            pid = 4321
            returncode: int | None = None
            stdout = _EmptyStream()
            stderr = _EmptyStream()

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        def sandbox(
            argv: list[str],
            **kwargs: Any,
        ) -> tuple[list[str], dict[str, str], None]:
            captured["sandbox_argv"] = argv
            captured["sandbox_kwargs"] = kwargs
            return ["/sandbox/launcher", *argv], {"PATH": "/sandbox"}, None

        async def spawn(*argv: str, **kwargs: Any) -> _Process:
            captured["spawn_argv"] = list(argv)
            captured["spawn_kwargs"] = kwargs
            return _Process()

        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.sandboxed_spawn_argv",
            sandbox,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            prerequisite_module,
            "_PROCESS_GROUP_SUPERVISOR",
            "/tmp/agent-replaced-supervisor.py",
        )

        result = await _run_process(
            "/tmp/agent-writable/kiro-cli",
            ["--version"],
            env={"PATH": "/tmp/agent-writable"},
            timeout_secs=1,
            sandboxed=True,
        )

        assert result.ok is True
        assert captured["sandbox_argv"] == [
            "/usr/bin/env",
            "/tmp/agent-writable/kiro-cli",
            "--version",
        ]
        assert captured["sandbox_kwargs"] == {
            "mode": "strict",
            "env": {"PATH": "/tmp/agent-writable"},
            "strip_python_env": True,
            "extra_hidden_dirs": (),
            "extra_visible_dirs": (),
        }
        assert captured["spawn_argv"] == [
            sys.executable,
            "-I",
            "-c",
            prerequisite_module._PROCESS_GROUP_SUPERVISOR_CODE,
            # Resource limits ride on the supervisor's argv, not preexec_fn.
            *prerequisite_module.resource_limit_supervisor_argv(),
            "/sandbox/launcher",
            "/usr/bin/env",
            "/tmp/agent-writable/kiro-cli",
            "--version",
        ]
        assert prerequisite_module._PROCESS_GROUP_SUPERVISOR not in captured["spawn_argv"]

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows does not use the POSIX process-group supervisor",
    )
    @pytest.mark.asyncio
    async def test_missing_process_group_supervisor_fails_before_spawn(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spawn = AsyncMock(side_effect=OSError("empty supervisor was spawned"))
        monkeypatch.setattr(prerequisite_module, "_PROCESS_GROUP_SUPERVISOR_CODE", "")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        result = await _run_process(
            "/fixed/kiro-cli",
            ["--version"],
            env={"PATH": "/usr/bin:/bin"},
            timeout_secs=1,
            sandboxed=False,
        )

        assert result.ok is False
        assert result.error == "Kiro process-group supervisor is unavailable"
        spawn.assert_not_awaited()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows does not use the POSIX process-group supervisor",
    )
    @pytest.mark.parametrize("wrapper", ("env", "systemd-run"))
    @pytest.mark.asyncio
    async def test_probe_resolves_sandbox_wrapper_before_supervisor_exec(
        self,
        wrapper: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        class _EmptyStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _Process:
            pid = 4321
            returncode: int | None = None
            stdout = _EmptyStream()
            stderr = _EmptyStream()

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        def sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], None]:
            return [wrapper, *argv], {"PATH": "/trusted/bin"}, None

        def which(executable: str, *, path: str | None = None) -> str:
            assert executable == wrapper
            assert path == os.defpath
            return f"/usr/bin/{wrapper}"

        async def spawn(*argv: str, **_kwargs: Any) -> _Process:
            captured["spawn_argv"] = list(argv)
            return _Process()

        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)
        monkeypatch.setattr(prerequisite_module.shutil, "which", which)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        result = await _run_process(
            "/fixed/kiro-cli",
            ["--version"],
            env={"PATH": "/trusted/bin"},
            timeout_secs=1,
            sandboxed=True,
        )

        assert result.ok is True
        # 4 supervisor items (python, -I, -c, code) plus the optional --rlimits=
        # fragment, then the resolved sandbox wrapper.
        wrapper_index = 4 + len(prerequisite_module.resource_limit_supervisor_argv())
        assert captured["spawn_argv"][wrapper_index] == f"/usr/bin/{wrapper}"

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows does not create a POSIX sandbox launcher",
    )
    @pytest.mark.asyncio
    async def test_sandbox_preparation_and_cleanup_do_not_block_event_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        cleanup_path = tmp_path / "sandbox-profile"
        cleanup_path.write_text("profile", encoding="utf-8")
        real_unlink = os.unlink

        class _EmptyStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _Process:
            pid = 4321
            returncode: int | None = None
            stdout = _EmptyStream()
            stderr = _EmptyStream()

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        def sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], str]:
            preparation_started.set()
            assert release_preparation.wait(timeout=1)
            return argv, {}, str(cleanup_path)

        def slow_unlink(path: str) -> None:
            if path == str(cleanup_path):
                cleanup_started.set()
                assert release_cleanup.wait(timeout=1)
            real_unlink(path)

        async def spawn(*_argv: str, **_kwargs: Any) -> _Process:
            return _Process()

        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)
        monkeypatch.setattr(prerequisite_module.os, "unlink", slow_unlink)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        process_task = asyncio.create_task(
            _run_process(
                "/fixed/tool",
                ["--version"],
                env={},
                timeout_secs=1,
                sandboxed=True,
            )
        )
        assert await asyncio.to_thread(preparation_started.wait, 1)
        ticked_during_preparation = False

        async def tick_preparation() -> None:
            nonlocal ticked_during_preparation
            await asyncio.sleep(0)
            ticked_during_preparation = True

        await tick_preparation()
        assert ticked_during_preparation
        release_preparation.set()
        assert await asyncio.to_thread(cleanup_started.wait, 1)
        ticked_during_cleanup = False

        async def tick_cleanup() -> None:
            nonlocal ticked_during_cleanup
            await asyncio.sleep(0)
            ticked_during_cleanup = True

        await tick_cleanup()
        assert ticked_during_cleanup
        release_cleanup.set()
        assert (await process_task).ok is True

    @pytest.mark.asyncio
    async def test_process_timeout_escalates_while_supervisor_anchors_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tree_kills: list[tuple[int, int]] = []

        class _HeldOpenStream:
            async def read(self, _size: int) -> bytes:
                await asyncio.Event().wait()
                return b""

        class _AnchoredParent:
            pid = 9876
            returncode: int | None = None
            stdout = _HeldOpenStream()
            stderr = _HeldOpenStream()

            async def wait(self) -> int:
                await asyncio.Event().wait()
                return 1

        async def spawn(*_args: str, **_kwargs: Any) -> _AnchoredParent:
            return _AnchoredParent()

        async def kill_tree(pid: int, signal_number: int) -> None:
            tree_kills.append((pid, signal_number))

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", kill_tree)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._TERMINATION_GRACE_SECS",
            0.001,
        )

        result = await _run_process(
            "/fixed/tool",
            ["--version"],
            env={},
            timeout_secs=0.01,
            sandboxed=False,
        )

        assert result.timed_out is True
        assert tree_kills == [
            (9876, platform_compat.SIGTERM),
            (9876, platform_compat.SIGKILL),
        ]

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX process-group supervisor",
    )
    @pytest.mark.asyncio
    async def test_posix_supervisor_rejects_relative_executable(self) -> None:
        result = await _run_process(
            "kiro-cli",
            ["--version"],
            env={"PATH": "/tmp/agent-writable"},
            timeout_secs=1,
            sandboxed=False,
        )

        assert result.ok is False
        assert result.returncode == 127

    @pytest.mark.asyncio
    async def test_windows_timeout_terminates_retained_descendant_handle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        terminated_handles: list[int] = []
        closed_handles: list[int] = []

        class _HeldOpenStream:
            async def read(self, _size: int) -> bytes:
                await asyncio.Event().wait()
                return b""

        class _ExitedParent:
            pid = 4321
            returncode: int | None = None
            stdout = _HeldOpenStream()
            stderr = _HeldOpenStream()
            stdin = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        async def spawn(*_args: str, **_kwargs: Any) -> _ExitedParent:
            return _ExitedParent()

        async def descendants(
            _pid: int,
            _retained_handles: dict[int, int] | None = None,
            _root_handle: int | None = None,
        ) -> dict[int, int]:
            return {4322: 9001}

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(
            platform_compat,
            "duplicate_asyncio_process_handle",
            lambda _proc: 8001,
        )
        monkeypatch.setattr(
            platform_compat,
            "terminate_process_handle",
            lambda handle: terminated_handles.append(handle) or True,
        )
        monkeypatch.setattr(
            platform_compat,
            "close_process_handle",
            lambda handle: closed_handles.append(handle),
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._TERMINATION_GRACE_SECS",
            0.01,
        )

        result = await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=0.01,
            sandboxed=False,
        )

        assert result.timed_out is True
        assert terminated_handles == [9001]
        assert closed_handles == [9001, 8001]

    @pytest.mark.asyncio
    async def test_windows_immediate_exit_still_takes_anchored_initial_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        snapshot_roots: list[int] = []
        closed_handles: list[int] = []

        class _ClosedStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _ExitedParent:
            pid = 4321
            returncode = 0
            stdout = _ClosedStream()
            stderr = _ClosedStream()
            stdin = None

            async def wait(self) -> int:
                return 0

        async def spawn(*_args: str, **_kwargs: Any) -> _ExitedParent:
            return _ExitedParent()

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            _root_handle: int | None = None,
        ) -> dict[int, int]:
            snapshot_roots.append(root_pid)
            await asyncio.sleep(0)
            return {4322: 9001} if root_pid == 4321 else {}

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            platform_compat,
            "duplicate_asyncio_process_handle",
            lambda _proc: 8001,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(platform_compat, "process_handle_active", lambda _handle: False)
        monkeypatch.setattr(
            platform_compat,
            "close_process_handle",
            closed_handles.append,
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

        result = await _run_process(
            r"C:\fixed\tool.exe",
            ["--version"],
            env={},
            timeout_secs=1,
            sandboxed=False,
        )

        assert result.ok is True
        assert snapshot_roots == [4321, 4322]
        assert closed_handles == [9001, 8001]

    @pytest.mark.asyncio
    async def test_windows_success_waits_for_live_launcher_descendant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        active_handles = {9001}
        child_observed = asyncio.Event()
        closed_handles: list[int] = []

        class _ClosedStream:
            async def read(self, _size: int) -> bytes:
                return b""

        class _ExitedParent:
            pid = 4321
            returncode = 0
            stdout = _ClosedStream()
            stderr = _ClosedStream()
            stdin = None

            async def wait(self) -> int:
                return 0

        async def spawn(*_args: str, **_kwargs: Any) -> _ExitedParent:
            return _ExitedParent()

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            if root_pid == 4321:
                assert root_handle == 8001
                child_observed.set()
                return {4322: 9001}
            assert root_pid == 4322
            assert root_handle == 9001
            return {}

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            platform_compat,
            "duplicate_asyncio_process_handle",
            lambda _proc: 8001,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda handle: handle in active_handles,
        )
        monkeypatch.setattr(
            platform_compat,
            "close_process_handle",
            closed_handles.append,
        )
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            prerequisite_module,
            "_WINDOWS_DESCENDANT_POLL_SECS",
            0.001,
        )

        process_task = asyncio.create_task(
            _run_process(
                r"C:\fixed\launcher.exe",
                ["install"],
                env={},
                timeout_secs=1,
                sandboxed=False,
            )
        )
        await asyncio.wait_for(child_observed.wait(), timeout=1)
        await asyncio.sleep(0)

        assert process_task.done() is False

        active_handles.clear()
        result = await asyncio.wait_for(process_task, timeout=1)

        assert result.ok is True
        assert closed_handles == [9001, 8001]

    @pytest.mark.asyncio
    async def test_windows_tracker_discovers_from_live_child_after_root_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExitedRoot:
            pid = 4321
            returncode = 0

        tracked = {4322: 9001}
        active_handles = {9001}
        child_root_scans = 0

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            nonlocal child_root_scans
            if root_pid == 4322:
                assert root_handle == 9001
                child_root_scans += 1
                return {4323: 9002} if child_root_scans == 1 else {}
            assert root_pid == 4323
            assert root_handle == 9002
            return {}

        async def one_poll(_delay: float) -> None:
            active_handles.clear()

        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda handle: handle in active_handles,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", one_poll)

        await prerequisite_module._track_windows_descendants(_ExitedRoot(), tracked)  # type: ignore[arg-type]

        assert tracked[4323] == 9002
        assert child_root_scans == 2

    @pytest.mark.asyncio
    async def test_windows_tracker_accepts_validated_discovery_when_anchor_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExitedRoot:
            pid = 4321
            returncode = 0

        tracked = {4322: 9001}
        active_handles = {9001}
        child_root_scans = 0

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            nonlocal child_root_scans
            if root_pid == 4322:
                assert root_handle == 9001
                child_root_scans += 1
                if child_root_scans == 1:
                    # The parent exits after this scan. The child appears only
                    # in the required post-exit terminal snapshot.
                    active_handles.clear()
                    return {}
                return {9876: 9002}
            assert root_pid == 9876
            assert root_handle == 9002
            return {}

        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda handle: handle in active_handles,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
            _ExitedRoot(),
            tracked,
        )

        assert tracked[9876] == 9002
        assert child_root_scans == 2

    @pytest.mark.asyncio
    async def test_windows_tracker_scans_each_inactive_child_root_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ExitedRoot:
            pid = 4321
            returncode = 0

        tracked = {4322: 9001}
        snapshot_roots: list[int] = []

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            snapshot_roots.append(root_pid)
            if root_pid == 4322:
                assert root_handle == 9001
                return {4323: 9002}
            assert root_pid == 4323
            assert root_handle == 9002
            return {}

        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda _handle: False,
        )
        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
            _ExitedRoot(),
            tracked,
        )

        assert tracked[4323] == 9002
        assert snapshot_roots == [4322, 4323]

    @pytest.mark.asyncio
    async def test_windows_tracker_fails_closed_on_later_snapshot_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _RunningRoot:
            pid = 4321
            returncode = None

        snapshot_calls = 0

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            nonlocal snapshot_calls
            assert root_pid == 4321
            assert root_handle == 8001
            snapshot_calls += 1
            if snapshot_calls == 1:
                return {}
            raise OSError("Toolhelp unavailable")

        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(
            platform_compat,
            "process_handle_active",
            lambda _handle: True,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        with pytest.raises(OSError, match="Toolhelp unavailable"):
            await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
                _RunningRoot(),
                {},
                8001,
            )

        assert snapshot_calls == 2

    @pytest.mark.asyncio
    async def test_windows_tracker_accepts_validated_discovery_when_primary_exits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _PrimaryRoot:
            pid = 4321
            returncode: int | None = None

        root = _PrimaryRoot()
        tracked: dict[int, int] = {}

        async def descendants(
            root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            root_handle: int | None = None,
        ) -> dict[int, int]:
            if root_pid == root.pid:
                assert root_handle is None
                root.returncode = 0
                return {9876: 9002}
            assert root_pid == 9876
            assert root_handle == 9002
            return {}

        monkeypatch.setattr(
            platform_compat,
            "descendant_termination_handles_async",
            descendants,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await prerequisite_module._track_windows_descendants(  # type: ignore[arg-type]
            root,
            tracked,
        )

        assert tracked == {9876: 9002}

    @pytest.mark.asyncio
    async def test_broken_stdin_cleans_up_process_and_readers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        terminated: list[int] = []

        class _HeldOpenStream:
            async def read(self, _size: int) -> bytes:
                await asyncio.Event().wait()
                return b""

        class _BrokenStdin:
            def write(self, _data: bytes) -> None:
                raise BrokenPipeError("closed")

        class _RunningProcess:
            pid = 7654
            returncode: int | None = None
            stdout = _HeldOpenStream()
            stderr = _HeldOpenStream()
            stdin = _BrokenStdin()

            async def wait(self) -> int:
                await asyncio.Event().wait()
                return 0

            def terminate(self) -> None:
                terminated.append(self.pid)
                self.returncode = 1

            def kill(self) -> None:
                self.returncode = 1

        async def spawn(*_args: str, **_kwargs: Any) -> _RunningProcess:
            return _RunningProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        result = await _run_process(
            "/fixed/tool",
            ["--version"],
            env={},
            timeout_secs=1,
            sandboxed=False,
            stdin_data=b"installer",
        )

        assert result.ok is False
        assert result.error == "closed"
        assert terminated == [7654]


class TestKiroPrerequisiteHandlers:
    @staticmethod
    def _app(
        service: KiroPrerequisiteService,
        *,
        app_claim: str,
        user: str = "test-user",
        owner_id: str = "test-user",
    ) -> web.Application:
        @web.middleware
        async def identity(
            request: web.Request,
            handler: Any,
        ) -> web.StreamResponse:
            request["user"] = user
            request["app"] = app_claim
            return await handler(request)

        app = web.Application(middlewares=[identity])
        app["state"] = SimpleNamespace(owner_id=owner_id)
        app["kiro_prerequisite_service"] = service
        app.router.add_get("/api/kiro-prerequisite", api_kiro_prerequisite_status)
        app.router.add_post(
            "/api/kiro-prerequisite/install",
            api_kiro_prerequisite_install,
        )
        app.router.add_post(
            "/api/kiro-prerequisite/login",
            api_kiro_prerequisite_login,
        )
        return app

    @pytest.mark.asyncio
    async def test_dashboard_user_can_read_and_start_setup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        snapshot = {
            "platform": "Linux",
            "installed": False,
            "authenticated": False,
            "ready": False,
            "initial_setup_complete": False,
            "can_auto_install": True,
            "can_login": False,
            "repair_required": False,
            "docs_url": "https://kiro.dev/docs/cli/installation/",
            "operation": {
                "kind": "",
                "status": "idle",
                "message": "",
                "detail": "",
                "url": "",
                "error": "",
            },
        }
        calls: list[tuple[str, str]] = []

        async def fake_snapshot() -> dict[str, Any]:
            return snapshot

        monkeypatch.setattr(service, "snapshot", fake_snapshot)
        monkeypatch.setattr(
            service,
            "start_install",
            lambda caller: calls.append(("install", caller)) or snapshot,
        )
        monkeypatch.setattr(
            service,
            "start_login",
            lambda caller: calls.append(("login", caller)) or snapshot,
        )

        async with TestClient(TestServer(self._app(service, app_claim=""))) as client:
            assert (await client.get("/api/kiro-prerequisite")).status == 200
            assert (await client.post("/api/kiro-prerequisite/install")).status == 202
            assert (await client.post("/api/kiro-prerequisite/login")).status == 202

        assert calls == [("install", "test-user"), ("login", "test-user")]

    @pytest.mark.asyncio
    async def test_status_endpoint_returns_not_ready_instead_of_500_on_probe_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A transient probe failure must not surface as an HTTP 500 (which
        # flashes the full-screen "could not check Kiro CLI" gate on reload).
        # The handler returns a retryable not-ready snapshot instead.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def boom() -> dict[str, Any]:
            raise OSError("probe wedged")

        monkeypatch.setattr(service, "snapshot", boom)

        async with TestClient(TestServer(self._app(service, app_claim=""))) as client:
            resp = await client.get("/api/kiro-prerequisite")
            assert resp.status == 200
            body = await resp.json()

        assert body["ready"] is False
        assert body["operation"]["status"] == "failed"
        assert body["setup_allowed"] is True

    @pytest.mark.asyncio
    async def test_session_create_and_send_are_rejected_before_enqueue_when_not_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def not_ready_snapshot(*, force: bool = False) -> dict[str, Any]:
            del force
            return {"ready": False}

        monkeypatch.setattr(service, "snapshot", not_ready_snapshot)
        app = web.Application()
        app["state"] = SimpleNamespace()
        app["kiro_prerequisite_service"] = service
        app.router.add_post("/api/chat", api_chat)
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            create_response = await client.post("/api/chat/slots", json={})
            create_body = await create_response.json()
            send_response = await client.post(
                "/api/chat",
                json={"message": "must not enqueue"},
            )
            send_body = await send_response.json()

        assert create_response.status == 503
        assert send_response.status == 503
        assert create_body["code"] == "kiro_prerequisite_required"
        assert send_body["code"] == "kiro_prerequisite_required"

    @pytest.mark.asyncio
    async def test_central_chat_runner_blocks_non_http_turn_entry(self, tmp_path: Path) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._has_verified = True  # pre-verified: not exercising the gate's own probe
        broadcasts: list[tuple[str, dict[str, Any]]] = []
        refreshes: list[str] = []
        state = SimpleNamespace(
            kiro_prerequisite_service=service,
            broadcast_ws=lambda event, payload: broadcasts.append((event, payload)),
            push_slots_update=lambda: None,
            push_refresh=lambda target: refreshes.append(target),
        )
        slot = SimpleNamespace(
            key="taskrunner-slot",
            append=lambda role, content, css: appended.append((role, content, css)),
            task=object(),
        )
        appended: list[tuple[str, str, str]] = []

        await _run_chat(state, slot, "workflow auto-turn")

        assert appended == [
            (
                "error",
                "Kiro CLI setup or sign-in is required before starting a session.",
                "msg msg-err",
            ),
            ("done", "", "done"),
        ]
        assert slot.task is None
        assert broadcasts == [("chat_done", {"slot": "taskrunner-slot"})]
        assert refreshes == ["history"]

    @pytest.mark.asyncio
    async def test_central_chat_runner_posts_readiness_error_to_linked_slack(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._has_verified = True  # pre-verified: not exercising the gate's own probe
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        slot = state.get_or_create_slot("linked-readiness")
        slot._slack_linked = True
        slot._slack_thread_ts = "1712345.6789"
        slot._slack_channel = "C123"

        await _run_chat(state, slot, "message from linked thread")

        state.slack_client.post_message.assert_awaited_once_with(
            "C123",
            "Kiro CLI setup or sign-in is required before starting a session.",
            "1712345.6789",
        )
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_paused_destructive_chat_routes_leave_slot_and_sessions_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            clock=lambda: 1.0,
        )
        service._has_probed = True
        service._has_verified = True  # pre-verified: not exercising the gate's own probe
        service._last_probe_at = 1.0
        messages = [
            {"role": "user", "content": "question", "ts": "u1"},
            {"role": "assistant", "content": "answer", "ts": "a1"},
        ]
        original_messages = copy.deepcopy(messages)
        slot = SimpleNamespace(messages=messages)
        sessions = MagicMock()
        persistence = MagicMock()
        state = SimpleNamespace(
            _slots={"paused": slot},
            sessions=sessions,
            conversation_log=persistence,
        )
        app = web.Application()
        app["state"] = state
        app["kiro_prerequisite_service"] = service
        app.router.add_post(
            "/api/chat/slots/{slot}/regenerate",
            api_chat_slot_regenerate,
        )
        app.router.add_post(
            "/api/chat/slots/{slot}/edit-resend",
            api_chat_slot_edit_resend,
        )
        app.router.add_post(
            "/api/chat/slots/{slot}/rewind",
            api_chat_slot_rewind,
        )

        async with TestClient(TestServer(app)) as client:
            responses = [
                await client.post("/api/chat/slots/paused/regenerate", json={}),
                await client.post(
                    "/api/chat/slots/paused/edit-resend",
                    json={"index": 0, "content": "edited"},
                ),
                await client.post(
                    "/api/chat/slots/paused/rewind",
                    json={"at_message_index": 0, "content": "edited"},
                ),
            ]
            bodies = [await response.json() for response in responses]

        assert [response.status for response in responses] == [503, 503, 503]
        assert [body["code"] for body in bodies] == ["kiro_prerequisite_required"] * 3
        assert messages == original_messages
        assert sessions.mock_calls == []
        assert persistence.mock_calls == []

    @pytest.mark.asyncio
    async def test_readiness_loss_waits_then_resumes_queued_message_fifo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            clock=lambda: 1.0,
        )
        service._has_probed = True
        service._has_verified = True  # pre-verified: not exercising the gate's own probe
        service._last_probe_at = 1.0
        service._status.ready = True

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

        readiness = iter((True, False, True, True))

        async def session_ready(_service: object) -> bool:
            return next(readiness)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("queued")
        slot._titled = True
        queue_id = slot.queue_append("keep this queued")
        monkeypatch.setattr(
            "kiro_crew.dashboard.kiro_readiness.kiro_session_ready",
            session_ready,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await _run_chat(state, slot, "first message")
        readiness_waiter = slot.task
        assert readiness_waiter is not None
        assert readiness_waiter in state._background_tasks
        await readiness_waiter
        queued_turn = slot.task
        assert queued_turn is not None
        assert queued_turn is not readiness_waiter
        await queued_turn

        assert delivered[0] == "first message"
        assert delivered[1].endswith("keep this queued")
        assert slot._queue == []
        assert slot.task is None
        assert any(
            call.args[0] == "queue_pop" and call.args[1]["queue_id"] == queue_id
            for call in state.broadcast_ws.call_args_list
        )
        assert any(
            message.get("role") == "error" and "queued messages" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.asyncio
    async def test_readiness_loss_keeps_synthesis_armed_until_it_resumes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PROMPT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

        readiness = iter((True, False, True))
        pending_at_readiness_check: list[bool] = []

        async def session_ready(_service: object) -> bool:
            pending_at_readiness_check.append(slot._pending_synthesis)
            return next(readiness)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = object()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = []
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("synthesis-readiness")
        slot._titled = True
        slot._pending_synthesis = True
        monkeypatch.setattr(
            "kiro_crew.dashboard.kiro_readiness.kiro_session_ready",
            session_ready,
        )
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())

        await _run_chat(state, slot, "first message")
        synthesis_task = slot.task
        assert synthesis_task is not None
        await synthesis_task

        assert pending_at_readiness_check == [True, True, True]
        assert delivered[0] == "first message"
        assert any(message.endswith(SUBAGENT_SYNTHESIS_PROMPT) for message in delivered[1:])
        assert slot._pending_synthesis is False

    @pytest.mark.asyncio
    async def test_app_token_is_denied_even_with_route_access(
        self,
        tmp_path: Path,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async with TestClient(TestServer(self._app(service, app_claim="untrusted-app"))) as client:
            for method, path in (
                ("get", "/api/kiro-prerequisite"),
                ("post", "/api/kiro-prerequisite/install"),
                ("post", "/api/kiro-prerequisite/login"),
            ):
                response = await getattr(client, method)(path)
                assert response.status == 403

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_gets_only_redacted_readiness(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        app = self._app(
            service,
            app_claim="",
            user="allowed-slack-user",
            owner_id="configured-owner",
        )

        async def ready_snapshot() -> dict[str, Any]:
            return {
                "platform": "Linux",
                "installed": True,
                "authenticated": True,
                "ready": True,
                "initial_setup_complete": True,
                "can_auto_install": True,
                "can_login": True,
                "repair_required": False,
                "docs_url": "https://kiro.dev/docs/cli/installation/",
                "operation": {
                    "kind": "login",
                    "status": "succeeded",
                    "message": "host detail",
                    "detail": "host output",
                    "url": "https://app.kiro.dev/device",
                    "error": "",
                },
            }

        monkeypatch.setattr(service, "snapshot", ready_snapshot)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/kiro-prerequisite")
            assert response.status == 200
            body = await response.json()
            assert body["ready"] is True
            assert body["initial_setup_complete"] is True
            assert body["setup_allowed"] is False
            assert body["platform"] == "gateway"
            assert body["operation"]["detail"] == ""
            assert body["operation"]["url"] == ""

            for method, path in (
                ("post", "/api/kiro-prerequisite/install"),
                ("post", "/api/kiro-prerequisite/login"),
            ):
                response = await getattr(client, method)(path)
                assert response.status == 403

    @pytest.mark.asyncio
    async def test_probe_failure_backstop_preserves_returning_user_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed probe must not demote a returning user to first-run.

        The last-resort backstop previously reported
        ``initial_setup_complete=False``, which makes the SPA restore the
        full-screen first-run setup gate for someone who has used the app for
        months. Carry the construction-time bit through instead.
        """

        data_home = tmp_path / "data-home"
        session = data_home / "sessions" / "existing.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text('{"role":"user"}\n', encoding="utf-8")
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=data_home,
            audit_writer=_no_audit,
        )

        async def exploding_snapshot() -> dict[str, Any]:
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(service, "snapshot", exploding_snapshot)

        app = self._app(service, app_claim="")
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/kiro-prerequisite")
            assert response.status == 200
            body = await response.json()
            assert body["ready"] is False
            assert body["initial_setup_complete"] is True

    @pytest.mark.asyncio
    async def test_local_bootstrap_identity_is_owner_when_unconfigured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )

        async def empty_snapshot() -> dict[str, Any]:
            return {}

        monkeypatch.setattr(service, "snapshot", empty_snapshot)

        app = self._app(
            service,
            app_claim="",
            user="local-app",
            owner_id="",
        )
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/kiro-prerequisite")).status == 200
