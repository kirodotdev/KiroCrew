from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from dataclasses import replace
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
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.dashboard.chat_handlers import api_chat_slot_create
from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
)
from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.handlers.kiro_prerequisite import (
    api_kiro_prerequisite_repair_specs,
    api_kiro_prerequisite_status,
)
from kiro_crew.dashboard.kiro_readiness import kiro_session_ready
from kiro_crew.kiro_cli import resolve_kiro_cli
from kiro_crew.kiro_prerequisite import (
    KIRO_CLI_LOGIN_COMMAND,
    KIRO_CLI_SSO_LOGIN_COMMAND,
    OFFICIAL_INSTALL_DOCS_URL,
    KiroPrerequisiteService,
    PrerequisiteStatus,
    ProcessResult,
    _run_process,
    find_kiro_cli_candidates,
)


@pytest.fixture(autouse=True)
def _isolate_agent_specs(tmp_path_factory, monkeypatch):
    """Point the agents dir at a POPULATED tmp dir for every test in this module.

    Readiness folds agent-spec presence into ``ready``: a missing spec means
    kiro-cli cannot resolve the agent, so the install is not ready. Those specs
    live in a MACHINE-WIDE directory that no ``KIROCREW_HOME`` override isolates,
    so an unpinned probe test reads the developer's real ``~/.kiro/agents`` and its
    verdict follows host state rather than the code under test. This pin removes
    that confound.

    Tests that exercise the missing-spec behaviour re-patch the same hook inside
    the test body, which runs after this fixture and therefore wins.
    """
    from kiro_crew import agent as agent_module
    from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

    agents = tmp_path_factory.mktemp("kiro-agents")
    for name in REQUIRED_KIRO_AGENT_FILES:
        (agents / name).write_text('{"name": "probe-fixture"}', encoding="utf-8")
    monkeypatch.setattr(agent_module, "KIRO_AGENTS_DIR", agents)


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
        self.kwargs: list[dict[str, Any]] = []

    async def run(
        self,
        command: str,
        args: list[str],
        **kwargs: Any,
    ) -> ProcessResult:
        self.calls.append((command, args))
        self.kwargs.append(kwargs)
        if args == ["--version"]:
            return ProcessResult(ok=self.installed)
        if args == ["whoami"]:
            return ProcessResult(ok=self.authenticated)
        return ProcessResult(ok=False)


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


class TestKiroPrerequisiteWorkflow:
    @pytest.mark.asyncio
    async def test_missing_prerequisite_service_fails_closed(self) -> None:
        assert await kiro_session_ready(None) is False
        assert await kiro_session_ready(object()) is False

    @pytest.mark.asyncio
    async def test_missing_route_prerequisite_wiring_fails_closed(self) -> None:
        """An unwired service must still fail closed on the BLOCKING gate.

        Turn-starting routes are advisory now, so the invariant is pinned on the
        poll-driven spawn gate — the one that still refuses to run ``kiro-cli``
        without a verified readiness latch.
        """

        from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified

        app = web.Application()
        app["state"] = SimpleNamespace()
        request = SimpleNamespace(app=app)

        blocked = await reject_if_kiro_unverified(request)  # type: ignore[arg-type]

        assert blocked is not None
        assert blocked.status == 503
        assert json.loads(blocked.body)["code"] == "kiro_prerequisite_required"

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

        await service.snapshot(force=True)

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
        # as the user) must still sign in: trust is "it runs + valid login", so a
        # legitimate update cannot lock the user out.
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
        executable.write_text("#!/bin/sh\n# self-updated\n", encoding="utf-8")
        executable.chmod(0o700)

        status = await service.snapshot(force=True)

        assert status["ready"] is True

    @pytest.mark.asyncio
    async def test_runnable_path_cli_is_usable_without_attestation(
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
        assert status["ready"] is True
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_runnable_path_cli_not_signed_in_is_reported_not_ready(
        self,
        tmp_path: Path,
    ) -> None:
        # Installed + runnable but no valid login. Reported honestly as
        # installed-but-not-ready; the user signs in with Kiro CLI.
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
        # Not a database at all: projection cannot read it, so staging aborts.
        # (Size is deliberately NOT the trigger — the identity DB is projected,
        # not byte-copied, so a large store must no longer abort sign-in.)
        live.write_bytes(b"this is not a sqlite database")
        original = live.read_bytes()
        probe = AsyncMock(return_value=ProcessResult(ok=True))

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=probe,
            audit_writer=_no_audit,
        )

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

    @staticmethod
    def _write_kiro_identity_db(path: Path, *, transcript_rows: int = 0) -> None:
        """Build a realistic kiro-cli store: identity tables + transcript tables."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("create table auth_kv (key text primary key, value text)")
            db.execute(
                "create table migrations (id integer primary key, "
                "version integer not null, migration_time integer not null)"
            )
            db.execute("create table history (id integer primary key, content text)")
            db.execute(
                "create table conversations_v2 (key text primary key, value text)"
            )
            db.execute("create index idx_conv_v2_key on conversations_v2(key)")
            db.execute("create table state (key text primary key, value blob)")
            db.execute("insert into auth_kv values ('kirocli:odic:token', 'tok-secret')")
            db.execute(
                "insert into auth_kv values ('kirocli:odic:device-registration', 'reg')"
            )
            db.execute("insert into migrations values (1, 11, 0)")
            # Identity-describing state rows (must project) …
            db.execute("insert into state values ('auth.idc.region', 'us-east-1')")
            db.execute(
                "insert into state values ('auth.idc.start-url', 'https://example')"
            )
            db.execute("insert into state values ('api.codewhisperer.profile', 'arn')")
            # … alongside unrelated local state (must NOT project).
            db.execute("insert into state values ('telemetryClientId', 'tele-id')")
            db.execute("insert into state values ('desktop.completedOnboarding', '1')")
            for index in range(transcript_rows):
                db.execute(
                    "insert into history values (?, ?)", (index, f"chat-{index}" * 64)
                )
                db.execute(
                    "insert into conversations_v2 values (?, ?)",
                    (f"c{index}", f"transcript-{index}" * 64),
                )
            db.commit()

    @pytest.mark.asyncio
    async def test_oversized_identity_store_is_projected_not_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A store far past the byte cap still stages: only identity tables copy.

        Regression: the identity DB used to be byte-copied under
        ``_MAX_AUTH_STORE_FILE_BYTES`` (64 MB). A real user's store had grown to
        ~429 MB of chat history, so staging aborted and sign-in failed with a
        message naming neither size nor cause. Projection must make the source
        file's size irrelevant.
        """
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        live = tmp_path / ".local" / "share" / "kiro-cli" / "data.sqlite3"
        self._write_kiro_identity_db(live, transcript_rows=200)
        # Force the old byte path to reject this store outright.
        monkeypatch.setattr(prerequisite_module, "_MAX_AUTH_STORE_FILE_BYTES", 1)

        staged_env: dict[str, str] = {}

        async def capture(*args: Any, **kwargs: Any) -> ProcessResult:
            staged_env.update(kwargs.get("env") or {})
            return ProcessResult(ok=True)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=capture,
            audit_writer=_no_audit,
        )

        result = await service._run_auth_command(
            str(executable), ["whoami"], base_env={}, timeout_secs=5
        )

        assert result.ok is True, "oversized identity store must not abort staging"
        assert staged_env["HOME"] != str(tmp_path), "probe must run in a staged home"

    def test_projection_carries_identity_and_drops_transcripts(
        self, tmp_path: Path
    ) -> None:
        """Identity rows transfer; transcript tables exist but arrive EMPTY.

        The schema must be complete even for withheld tables: ``migrations`` is
        projected with its rows, so kiro-cli treats the schema as current and
        runs no migration — a store missing ``history`` would then fail with
        "no such table".
        """
        source = tmp_path / "data.sqlite3"
        self._write_kiro_identity_db(source, transcript_rows=50)
        destination = tmp_path / "staged" / "data.sqlite3"

        assert prerequisite_module._project_identity_database(source, destination)

        with contextlib.closing(sqlite3.connect(destination)) as db:
            assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
            identity = dict(db.execute("select key, value from auth_kv").fetchall())
            assert identity["kirocli:odic:token"] == "tok-secret"
            assert db.execute("select count(*) from migrations").fetchone()[0] == 1
            # Transcript tables must be present-but-empty, not absent.
            assert db.execute("select count(*) from history").fetchone()[0] == 0
            assert (
                db.execute("select count(*) from conversations_v2").fetchone()[0] == 0
            )
            # `state` carries the identity-describing keys so `whoami` can render
            # its profile/region block — and NOT the telemetry identifiers.
            state = dict(db.execute("select key, value from state").fetchall())
            assert state["auth.idc.region"] == "us-east-1"
            assert state["auth.idc.start-url"] == "https://example"
            assert state["api.codewhisperer.profile"] == "arn"
            assert "telemetryClientId" not in state
            assert "desktop.completedOnboarding" not in state
        assert destination.stat().st_size < source.stat().st_size
        if platform_compat.IS_POSIX:
            assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    def test_projection_reads_wal_resident_identity_rows(self, tmp_path: Path) -> None:
        """A store in WAL mode must project the rows the CLI itself would read.

        Regression: opening the source with ``immutable=1`` tells SQLite the file
        cannot change, so the ``-wal`` is IGNORED. Against a kiro-cli store whose
        newest commits are still in ``data.sqlite3-wal``, the token row reads as
        missing and the staged store looks SIGNED OUT — a worse failure than the
        size abort this projection replaces.
        """
        source = tmp_path / "data.sqlite3"
        writer = sqlite3.connect(source)
        try:
            writer.execute("pragma journal_mode=WAL")
            writer.execute("create table auth_kv (key text primary key, value text)")
            writer.execute(
                "create table migrations (id integer primary key, "
                "version integer not null, migration_time integer not null)"
            )
            writer.execute("create table history (id integer primary key, content text)")
            writer.execute("insert into auth_kv values ('kirocli:odic:token', 'wal-tok')")
            writer.execute("insert into migrations values (1, 11, 0)")
            writer.commit()
            # Writer stays OPEN, so the commits are still WAL-resident (not yet
            # checkpointed into the main database file).
            assert (source.parent / "data.sqlite3-wal").exists()

            destination = tmp_path / "staged" / "data.sqlite3"
            assert prerequisite_module._project_identity_database(source, destination)
        finally:
            writer.close()

        with contextlib.closing(sqlite3.connect(destination)) as db:
            identity = dict(db.execute("select key, value from auth_kv").fetchall())
        assert identity.get("kirocli:odic:token") == "wal-tok", (
            "WAL-resident identity must be projected, not read as signed-out"
        )

    def test_projection_refuses_symlinked_and_non_database_sources(
        self, tmp_path: Path
    ) -> None:
        """Path defenses match the byte path: no symlink, and a real DB only."""
        real = tmp_path / "real.sqlite3"
        self._write_kiro_identity_db(real)
        link = tmp_path / "link.sqlite3"
        link.symlink_to(real)
        assert not prerequisite_module._project_identity_database(
            link, tmp_path / "out-link.sqlite3"
        )

        junk = tmp_path / "junk.sqlite3"
        junk.write_bytes(b"not a database")
        assert not prerequisite_module._project_identity_database(
            junk, tmp_path / "out-junk.sqlite3"
        )

        missing = tmp_path / "absent.sqlite3"
        assert not prerequisite_module._project_identity_database(
            missing, tmp_path / "out-missing.sqlite3"
        )

    def test_projection_refuses_a_store_with_no_identity_table(
        self, tmp_path: Path
    ) -> None:
        """Fail closed: never hand the CLI a store it would read as signed-out."""
        source = tmp_path / "data.sqlite3"
        with contextlib.closing(sqlite3.connect(source)) as db:
            db.execute("create table history (id integer primary key)")
            db.commit()
        destination = tmp_path / "staged" / "data.sqlite3"

        assert not prerequisite_module._project_identity_database(source, destination)
        assert not destination.exists()

    def test_projection_refuses_when_only_some_identity_tables_exist(
        self, tmp_path: Path
    ) -> None:
        """A PARTIAL identity schema must abort, not stage an empty identity.

        Guards the `all` (not `any`) gate: a future kiro-cli that renames
        ``auth_kv`` while keeping ``migrations`` would otherwise pass the gate and
        stage a store whose schema is present but whose identity rows are absent —
        silently producing the "signed out" outcome the gate exists to prevent.
        """
        source = tmp_path / "data.sqlite3"
        with contextlib.closing(sqlite3.connect(source)) as db:
            # migrations present, auth_kv renamed away (simulating a schema bump).
            db.execute(
                "create table migrations (id integer primary key, "
                "version integer not null, migration_time integer not null)"
            )
            db.execute("create table auth_kv_v2 (key text primary key, value text)")
            db.execute("insert into migrations values (1, 12, 0)")
            db.execute("insert into auth_kv_v2 values ('kirocli:odic:token', 'tok')")
            db.commit()
        destination = tmp_path / "staged" / "data.sqlite3"

        assert not prerequisite_module._project_identity_database(source, destination)
        assert not destination.exists()

    def test_projection_stages_a_store_without_the_state_table(
        self, tmp_path: Path
    ) -> None:
        """`state` is optional: an older schema without it must still stage."""
        source = tmp_path / "data.sqlite3"
        with contextlib.closing(sqlite3.connect(source)) as db:
            db.execute("create table auth_kv (key text primary key, value text)")
            db.execute(
                "create table migrations (id integer primary key, "
                "version integer not null, migration_time integer not null)"
            )
            db.execute("insert into auth_kv values ('kirocli:odic:token', 'tok')")
            db.execute("insert into migrations values (1, 11, 0)")
            db.commit()
        destination = tmp_path / "staged" / "data.sqlite3"

        assert prerequisite_module._project_identity_database(source, destination)
        with contextlib.closing(sqlite3.connect(destination)) as db:
            assert dict(db.execute("select key, value from auth_kv").fetchall()) == {
                "kirocli:odic:token": "tok"
            }

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
    async def test_session_gate_never_probes(
        self,
        tmp_path: Path,
    ) -> None:
        """The session gate spawns NO subprocess — it reads latched state only.

        Probing is boot-and-explicit-action only, so no amount of elapsed time or
        repeated session checks may spawn a ``kiro-cli``.
        """

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

        # Nothing probed yet: the gate reports not-ready without spawning.
        assert await service.session_ready() is False
        assert runtime.calls == []

        # Time passing does not license a re-probe.
        now[0] += 10_000.0
        assert await service.session_ready() is False
        assert runtime.calls == []

    @pytest.mark.asyncio
    async def test_session_gate_reads_boot_probe_result_without_reprobing(
        self,
        tmp_path: Path,
    ) -> None:
        """After the boot probe, the gate serves its result forever, unprobed."""

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

        # The boot probe (warm_up) resolves readiness once.
        await service._probe()
        calls_after_boot = len(runtime.calls)
        assert calls_after_boot

        # A later sign-out is NOT discovered by the gate; that is the ACP
        # attempt's job now. The latched value stands.
        runtime.authenticated = False
        now[0] += 10_000.0
        assert await service.session_ready() is True
        assert len(runtime.calls) == calls_after_boot

    @pytest.mark.asyncio
    async def test_verified_ready_reprobes_a_stale_ready_latch(
        self,
        tmp_path: Path,
    ) -> None:
        """A stale ready=True must NOT authorize a destructive or spawning call.

        Boot authenticated, then the user logs out externally and never sends a
        chat turn — so `mark_signed_out()` never fires and the latch still says
        ready. `session_ready()` (the send path) keeps trusting it; the
        authorization gate must re-probe and deny.
        """

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True
        now = [100.0]
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: now[0],
        )

        await service._probe()
        assert await service.session_ready() is True

        # External logout, no chat turn in between.
        runtime.authenticated = False
        now[0] += 10_000.0

        # The send path still trusts the latch (that is deliberate).
        assert await service.session_ready() is True
        # The authorization gate does not.
        assert await service.verified_ready(max_age_secs=30.0) is False

    @pytest.mark.asyncio
    async def test_verified_ready_reprobes_a_stale_not_ready_latch(
        self,
        tmp_path: Path,
    ) -> None:
        """A stale ready=False must not permanently 503 these endpoints either.

        This is the recovery direction: the user signed in from a terminal, so a
        re-probe must pick it up without a gateway restart.
        """

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        now = [100.0]
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: now[0],
        )

        await service._probe()
        assert await service.verified_ready(max_age_secs=30.0) is False

        runtime.authenticated = True
        now[0] += 31.0

        assert await service.verified_ready(max_age_secs=30.0) is True

    @pytest.mark.asyncio
    async def test_verified_ready_serves_a_fresh_latch_without_reprobing(
        self,
        tmp_path: Path,
    ) -> None:
        """Within the freshness window a burst of callers collapses to one probe."""

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.authenticated = True
        now = [100.0]
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: now[0],
        )

        await service._probe()
        calls_after_boot = len(runtime.calls)

        for _ in range(5):
            assert await service.verified_ready(max_age_secs=30.0) is True
        assert len(runtime.calls) == calls_after_boot

    @pytest.mark.asyncio
    async def test_verified_ready_fails_closed_when_the_probe_cannot_run(
        self,
        tmp_path: Path,
    ) -> None:
        """A probe that raises is not evidence of readiness."""

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = True

        async def exploding_probe(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("probe exploded")

        service._probe = exploding_probe  # type: ignore[method-assign]

        assert await service.verified_ready(max_age_secs=0.0) is False

    @pytest.mark.asyncio
    async def test_mark_signed_out_latches_without_probing(
        self,
        tmp_path: Path,
    ) -> None:
        """An observed ACP auth failure narrows readiness with no subprocess."""

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

        await service._probe()
        assert await service.session_ready() is True
        calls_after_boot = len(runtime.calls)

        service.mark_signed_out()

        assert await service.session_ready() is False
        snapshot = await service.snapshot()
        assert snapshot["authenticated"] is False
        assert snapshot["ready"] is False
        # Latching is pure bookkeeping — it must not spawn anything.
        assert len(runtime.calls) == calls_after_boot

    @pytest.mark.asyncio
    async def test_mark_signed_out_preserves_returning_user_state(
        self,
        tmp_path: Path,
    ) -> None:
        """Latching signed-out must not demote a returning user to first-run."""

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

        await service._probe()
        assert service.initial_setup_complete is True

        service.mark_signed_out()

        snapshot = await service.snapshot()
        assert snapshot["ready"] is False
        # Never demote a returning user to the full-screen first-run setup shell.
        assert snapshot["initial_setup_complete"] is True

    @pytest.mark.asyncio
    async def test_close_cancels_the_boot_warm_up_probe(
        self,
        tmp_path: Path,
    ) -> None:
        """A gateway shutdown mid-boot-probe must not leak the subprocess wait."""

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

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=blocking_runtime,
            audit_writer=_no_audit,
            warm_up_delay=0,
        )

        task = service.warm_up()
        assert task is not None
        await asyncio.wait_for(probe_started.wait(), timeout=1)

        await service.close()

        assert task.cancelled()
        assert probe_cancelled.is_set()

    @pytest.mark.asyncio
    async def test_status_endpoint_probes_only_when_refresh_is_forced(
        self,
        tmp_path: Path,
    ) -> None:
        """The polled snapshot reads latched state; ``force`` is the probe path."""

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

        await service._probe()
        calls_after_boot = len(runtime.calls)
        runtime.authenticated = False

        # Background polls are free and keep serving the latched value.
        for _ in range(5):
            snapshot = await service.snapshot()
            assert snapshot["ready"] is True
        assert len(runtime.calls) == calls_after_boot

        # An explicit refresh re-probes and picks up the sign-out.
        refreshed = await service.snapshot(force=True)
        assert refreshed["ready"] is False
        assert len(runtime.calls) > calls_after_boot

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
        """The boot probe resolves readiness; the gate then never spawns again."""

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
        after_boot = len(runtime.calls)
        assert after_boot, "the boot warm-up must actually probe"

        # Every later turn reads the latch — no subprocess, ever.
        for _ in range(5):
            assert await service.session_ready() is True
        assert len(runtime.calls) == after_boot

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
    async def test_broken_linux_target_reports_not_installed(self, tmp_path: Path) -> None:
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
        # No gateway-side remedy is claimed: the user obtains the CLI from Kiro.
        assert status["repair_required"] is False

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows cannot represent POSIX execute-bit semantics",
    )
    @pytest.mark.asyncio
    async def test_non_executable_linux_target_reports_not_installed(
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
        # No gateway-side remedy is claimed: the user obtains the CLI from Kiro.
        assert status["repair_required"] is False

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

        async def passthrough_sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], str | None]:
            return list(argv), {}, None

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(platform_compat, "kill_process_tree_async", kill_tree)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        # This case is about the timeout escalation, not about sandbox building,
        # and every spawn is sandboxed now — so stub the builder rather than let
        # host sandbox availability decide the outcome.
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._prepare_sandboxed_spawn",
            passthrough_sandbox,
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite._TERMINATION_GRACE_SECS",
            0.001,
        )

        result = await _run_process(
            "/fixed/tool",
            ["--version"],
            env={},
            timeout_secs=0.01,
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
    def test_posix_supervisor_rejects_relative_executable(self) -> None:
        # The supervisor is the last line of defence against resolving a bare
        # program name through an agent-writable PATH. Exercised directly rather
        # than through _run_process: every spawn is sandboxed now, and that path
        # hands the supervisor an absolute /usr/bin/env, so the guard cannot be
        # reached from there.
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-I",
                "-c",
                prerequisite_module._PROCESS_GROUP_SUPERVISOR_CODE,
                "kiro-cli",
                "--version",
            ],
            env={"PATH": "/tmp/agent-writable"},
            capture_output=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == 127

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
            "/api/kiro-prerequisite/repair-specs",
            api_kiro_prerequisite_repair_specs,
        )
        return app

    @pytest.mark.asyncio
    async def test_dashboard_user_reads_status_and_no_setup_verb_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The owner can READ readiness. Neither setup step is a Kiro Crew verb:
        # obtaining the CLI and signing in both belong to Kiro CLI, so both
        # routes are absent by construction rather than guarded.
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
            "repair_required": False,
            "docs_url": OFFICIAL_INSTALL_DOCS_URL,
            "login_command": KIRO_CLI_LOGIN_COMMAND,
            "sso_login_command": KIRO_CLI_SSO_LOGIN_COMMAND,
        }

        async def fake_snapshot(
            *, force: bool = False, coalesce: bool = False
        ) -> dict[str, Any]:
            del force, coalesce
            return snapshot

        monkeypatch.setattr(service, "snapshot", fake_snapshot)

        async with TestClient(TestServer(self._app(service, app_claim=""))) as client:
            read = await client.get("/api/kiro-prerequisite")
            assert read.status == 200
            body = await read.json()
            assert body["login_command"] == KIRO_CLI_LOGIN_COMMAND
            # The owner branch passes the snapshot through verbatim, so both
            # sign-in commands reach the gate that has to offer the tier choice.
            assert body["sso_login_command"] == KIRO_CLI_SSO_LOGIN_COMMAND
            assert (await client.post("/api/kiro-prerequisite/login")).status == 404
            assert (await client.post("/api/kiro-prerequisite/install")).status == 404

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
        # Reported as a retryable not-ready 200, never a 500 that would flash the
        # full-screen "could not check Kiro CLI" gate on reload.
        assert body["installed"] is True
        assert body["setup_allowed"] is True

    @pytest.mark.asyncio
    async def test_session_create_and_send_are_admitted_when_latch_is_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Turn-starting routes no longer 503 on a latched not-ready value.

        Readiness is probed at boot and on explicit action only, so a stale latch
        must not reject a request the CLI would have served — that was the stuck
        case (sign in from a terminal, stay locked out). The ACP attempt reports
        the real auth state as a chat error instead.
        """

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
        assert await service.session_ready() is False
        app = web.Application()
        app["state"] = SimpleNamespace()
        app["kiro_prerequisite_service"] = service
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            create_response = await client.post("/api/chat/slots", json={})
            create_text = await create_response.text()

        # Admitted past the (now removed) prerequisite gate: the request reaches
        # the handler body and only then fails on this bare SimpleNamespace state
        # (500), rather than being turned away with the prerequisite 503.
        assert create_response.status != 503
        assert "kiro_prerequisite_required" not in create_text

    @pytest.mark.asyncio
    async def test_central_chat_runner_posts_auth_error_to_linked_slack(
        self,
        tmp_path: Path,
    ) -> None:
        """An ACP auth failure still reaches a linked Slack thread.

        The pre-turn readiness gate used to own this delivery; the
        ``AcpAuthRequired`` handler now does, so a user driving the session from
        Slack is not left without a response when the CLI is signed out.
        """

        from kiro_crew.acp.client import AcpAuthRequired

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = True
        service._status.authenticated = True
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.slack_client = MagicMock()
        state.slack_client.post_message = AsyncMock()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False

        async def stream(_stream_message: str):
            raise AcpAuthRequired("kiro-cli is not logged in.")
            yield  # pragma: no cover — generator shape only

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()
        # No inbound mirror link: this test asserts the AUTH-error delivery, so
        # the ordinary user-message mirror must stay out of post_message's calls.
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))

        slot = state.get_or_create_slot("linked-readiness")
        slot._titled = True
        slot._slack_linked = True
        slot._slack_thread_ts = "1712345.6789"
        slot._slack_channel = "C123"

        await _run_chat(state, slot, "message from linked thread")

        state.slack_client.post_message.assert_awaited_once_with(
            "C123",
            "kiro-cli is not logged in.",
            "1712345.6789",
        )
        # The observed failure latches readiness so the SPA banner appears
        # without waiting for the user to hit Refresh.
        assert service._status.ready is False
        assert service._status.authenticated is False

    @pytest.mark.asyncio
    async def test_destructive_chat_routes_reject_before_mutating_history(
        self,
        tmp_path: Path,
    ) -> None:
        """regenerate / edit-resend / rewind MUST fail closed on a stale latch.

        These three truncate `slot.messages` and PERSIST the result before the
        background turn runs, so "let the ACP attempt be the authority" does not
        hold: by the time the turn raises AcpAuthRequired the history is already
        rewritten, and no error card can undo it. They therefore keep the
        blocking gate even though an ordinary send does not.
        """

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            clock=lambda: 1.0,
        )
        service._has_probed = True
        service._last_probe_at = 1.0
        assert await service.session_ready() is False
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
        # The refusal happens BEFORE any mutation: history is untouched and no
        # session/persistence call was made.
        assert messages == original_messages
        assert sessions.mock_calls == []
        assert persistence.mock_calls == []

    @pytest.mark.asyncio
    async def test_auth_failure_holds_the_queue_instead_of_draining_it(
        self,
        tmp_path: Path,
    ) -> None:
        """An auth failure must not pop every queued prompt into the same wall.

        Without this, teardown hands the queue to a successor turn that fails
        identically, repeats, and drains the whole queue — leaving nothing to
        resume after the user signs in. The queue is held intact instead (cards
        stay visible and individually cancellable).
        """

        from kiro_crew.acp.client import AcpAuthRequired

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = True
        service._status.authenticated = True

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            raise AcpAuthRequired("kiro-cli is not logged in.")
            yield  # pragma: no cover — generator shape only

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        state.kiro_prerequisite_service = service
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))

        slot = state.get_or_create_slot("auth-queue")
        slot._titled = True
        slot.queue_append("first queued")
        slot.queue_append("second queued")

        await _run_chat(state, slot, "message that hits the auth wall")

        # Only the original turn ran; neither queued prompt was consumed.
        assert delivered == ["message that hits the auth wall"]
        assert len(slot._queue) == 2
        # And the actionable message reached the transcript.
        assert any(
            message.get("role") == "error"
            and "not logged in" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.asyncio
    async def test_stale_not_ready_does_not_park_the_queue(
        self,
        tmp_path: Path,
    ) -> None:
        """A latched not-ready value must never strand a queued message.

        Readiness is only refreshed at boot and on explicit action, so parking
        the queue on it would wait forever. The successor turn runs and the ACP
        attempt reports the real auth state.
        """

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        # Deliberately stale/not-ready: the boot probe found a signed-out CLI and
        # the user has since signed in from a terminal.
        service._has_probed = True
        service._status.ready = False

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

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

        await _run_chat(state, slot, "first message")
        queued_turn = slot.task
        assert queued_turn is not None
        await queued_turn

        # Both turns ran despite the stale not-ready latch.
        assert delivered[0] == "first message"
        assert delivered[1].endswith("keep this queued")
        assert slot._queue == []
        assert slot.task is None
        assert any(
            call.args[0] == "queue_pop" and call.args[1]["queue_id"] == queue_id
            for call in state.broadcast_ws.call_args_list
        )
        # No "setup or sign-in is required" card — nothing was blocked.
        assert not any(
            message.get("role") == "error" and "sign-in is required" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.asyncio
    async def test_stale_not_ready_does_not_park_synthesis(
        self,
        tmp_path: Path,
    ) -> None:
        """Post-fan-out synthesis runs rather than waiting on latched readiness."""

        from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PROMPT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._has_probed = True
        service._status.ready = False

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

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
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = []
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("synthesis-readiness")
        slot._titled = True
        slot._pending_synthesis = True

        await _run_chat(state, slot, "first message")
        synthesis_task = slot.task
        assert synthesis_task is not None
        await synthesis_task

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
                ("post", "/api/kiro-prerequisite/repair-specs"),
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

        async def ready_snapshot(
            *, force: bool = False, coalesce: bool = False
        ) -> dict[str, Any]:
            del force, coalesce
            return {
                "platform": "Linux",
                "installed": True,
                "authenticated": True,
                "ready": True,
                "initial_setup_complete": True,
                "repair_required": False,
                "docs_url": OFFICIAL_INSTALL_DOCS_URL,
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
            # Present but empty: the payload shape must not vary by caller, and
            # only the owner can act on a missing spec.
            assert body["missing_agent_specs"] == []
            assert body["agent_spec_repair_error"] == ""
            # Both sign-in commands are served here too. The redacted branch
            # hardcodes its fields, so a field added only to the dataclass would
            # reach the owner and leave this caller's client reading undefined.
            assert body["login_command"] == KIRO_CLI_LOGIN_COMMAND
            assert body["sso_login_command"] == KIRO_CLI_SSO_LOGIN_COMMAND
            # The pre-upgrade-tab shim is served to every caller, so the payload
            # shape does not vary by who asks.
            assert body["operation"]["status"] == "idle"

            # The repair route is a mutation on the agent home, so it is
            # owner-gated. It is also the ONLY mutation left on this surface.
            for method, path in (
                ("post", "/api/kiro-prerequisite/repair-specs"),
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

        async def exploding_snapshot(*, force: bool = False) -> dict[str, Any]:
            del force
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

        async def empty_snapshot(*, force: bool = False) -> dict[str, Any]:
            del force
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


class TestSandboxUnavailableIsNotAMissingBinary:
    """A sandbox-refused probe must not be reported as "not installed".

    Verification runs the candidate INSIDE the sandbox
    (``_UNVERIFIED_SANDBOX_MODE``), so on a host where the sandbox cannot be
    constructed — Ubuntu >= 23.10 with
    ``kernel.apparmor_restrict_unprivileged_userns=1`` is the common case — a
    present, executable, already-authenticated CLI fails verification. The old
    code degraded that to ``installed=False``, telling the user to install a CLI
    that was already there.

    Every case here drives the decision through ``ProcessResult.sandbox_failure``
    — the typed signal the spawn itself produced — so the outcome does not depend
    on whether the machine running the tests happens to have a working sandbox.
    """

    @staticmethod
    def _service(tmp_path: Path, run: Any) -> KiroPrerequisiteService:
        return KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

    @staticmethod
    def _sandbox_refused(kind: str, detail: str, remedy: str = "") -> Any:
        """A runner standing in for wrap_argv fail-closing on this spawn."""

        async def run(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(
                ok=False,
                error=f"Sandbox backend unavailable ... Probe detail: {detail}.",
                sandbox_failure=(kind, detail, remedy),
            )

        return run

    @staticmethod
    async def _plain_failure(
        _command: str,
        _args: list[str],
        **_kwargs: Any,
    ) -> ProcessResult:
        """A probe that failed for a reason unrelated to the sandbox."""
        return ProcessResult(ok=False, error="exited with code 1")

    @pytest.mark.asyncio
    async def test_present_binary_reports_sandbox_unavailable_not_missing(
        self,
        tmp_path: Path,
    ) -> None:
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        detail = "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"

        status = await self._service(
            tmp_path, self._sandbox_refused("no_backend", detail)
        ).snapshot(force=True)

        assert status["sandbox_unavailable"] is True
        assert status["sandbox_failure_kind"] == "no_backend"
        assert status["sandbox_detail"] == detail
        # The whole point: the binary is on disk and executable, so claiming it
        # is not installed is the bug being fixed.
        assert status["installed"] is True
        assert status["ready"] is False
        # Signing in cannot fix a missing sandbox backend, so an action that
        # cannot help is not offered.
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_remedy_token_reaches_the_dashboard_payload(
        self,
        tmp_path: Path,
    ) -> None:
        """The mechanism the probe identified must survive to the gate screen.

        Without it the dashboard can only render ``errno 1 (EPERM)`` and a retry
        button, which is the dead end reported in issue #1660: the probe already
        knows the fix is an AppArmor profile and the user has no way to learn it.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        detail = "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"

        status = await self._service(
            tmp_path, self._sandbox_refused("no_backend", detail, "apparmor_userns")
        ).snapshot(force=True)

        assert status["sandbox_remedy"] == "apparmor_userns"

    @pytest.mark.asyncio
    async def test_absent_remedy_is_empty_not_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Shape stability: the key is always present, even with no mechanism.

        The gate reads it unconditionally, so an absent key would make the field
        ``undefined`` and select a remedy branch by accident.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        status = await self._service(
            tmp_path, self._sandbox_refused("no_backend", "not Linux")
        ).snapshot(force=True)

        assert status["sandbox_remedy"] == ""

    @pytest.mark.asyncio
    async def test_missing_binary_still_reports_not_installed(
        self,
        tmp_path: Path,
    ) -> None:
        """The genuine missing-binary path must be untouched by this change."""
        status = await self._service(tmp_path, self._plain_failure).snapshot(force=True)

        assert status["installed"] is False
        assert status["sandbox_unavailable"] is False
        assert status["sandbox_failure_kind"] == ""

    @pytest.mark.asyncio
    async def test_probe_failure_unrelated_to_the_sandbox_is_not_misattributed(
        self,
        tmp_path: Path,
    ) -> None:
        """A present binary that simply fails --version is NOT a sandbox problem.

        This is the over-claim guard, and it is what keeps the fix honest on
        platforms where verification is not sandboxed at all: ``_run_process``
        skips the wrap on Windows, and ``sandbox_allow_unsandboxed_exec``
        bypasses it, so a broken CLI there must keep its repair path instead of
        being blamed on the sandbox.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        status = await self._service(tmp_path, self._plain_failure).snapshot(force=True)

        assert status["sandbox_unavailable"] is False
        assert status["sandbox_failure_kind"] == ""
        assert status["installed"] is False

    @pytest.mark.asyncio
    async def test_transient_failure_is_reported_as_transient(
        self,
        tmp_path: Path,
    ) -> None:
        """A transient refusal must be distinguishable from a host verdict.

        The remedy differs sharply: retry, versus change the host. Reporting a
        momentary EAGAIN as "this host has no sandbox" is what would push a user
        into needlessly disabling their own isolation.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        status = await self._service(
            tmp_path, self._sandbox_refused("transient", "fork failed with errno 11 (EAGAIN)")
        ).snapshot(force=True)

        assert status["sandbox_unavailable"] is True
        assert status["sandbox_failure_kind"] == "transient"
        assert status["installed"] is True

    @pytest.mark.asyncio
    async def test_working_host_sets_no_sandbox_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """The healthy path must leave every new field at its default."""
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(ok=args == ["--version"])

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["installed"] is True
        assert status["sandbox_unavailable"] is False
        assert status["sandbox_failure_kind"] == ""
        assert status["sandbox_detail"] == ""


class TestKiroCrewNeverSetsUpKiroCli:
    """Kiro Crew DETECTS Kiro CLI. It never installs it and never signs in.

    These are contract tests, not behavior tests: they pin the ABSENCE of both
    setup capabilities. The removed install path downloaded a remote shell script
    and executed it unsandboxed, with a digest pin that silently broke setup
    whenever Kiro republished the script. The removed login path spawned a
    credential-writing child process on the user's behalf. Both belong to Kiro
    CLI, and a change that reintroduces either — or an unsandboxed spawn hook to
    build one on — fails here.
    """

    def test_no_login_execution_surface_exists(self) -> None:
        for attribute in (
            "extract_secure_login_url",
            "_TRUSTED_LOGIN_HOSTS",
            "_LOGIN_TIMEOUT_SECS",
            "OperationStatus",
        ):
            assert not hasattr(prerequisite_module, attribute), attribute
        for method in ("start_login", "_login", "_capture_operation_output"):
            assert not hasattr(KiroPrerequisiteService, method), method

    @pytest.mark.asyncio
    async def test_auto_poll_is_coalesced_but_check_again_always_probes(
        self,
        tmp_path: Path,
    ) -> None:
        # The blocking first-run gate polls with refresh=1 every 5s, and each
        # browser tab polls independently. Without a floor, N tabs mean N times
        # the kiro-cli spawns; with it they collapse onto one probe per interval.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = True
        clock = {"now": 1_000.0}

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: clock["now"],
        )

        await service.snapshot(force=True, coalesce=True)
        first = len(runtime.calls)
        assert first > 0, "the first forced probe must actually run"

        # Three more tabs poll within the floor: no new spawns.
        for _ in range(3):
            await service.snapshot(force=True, coalesce=True)
        assert len(runtime.calls) == first

        # A HUMAN Check again is never coalesced — a button that returns a cached
        # answer looks broken, so it probes even inside the floor.
        await service.snapshot(force=True)
        explicit = len(runtime.calls)
        assert explicit > first

        # Past the floor, the machine poll runs again too.
        clock["now"] += prerequisite_module._FORCED_PROBE_FLOOR_SECS
        await service.snapshot(force=True, coalesce=True)
        assert len(runtime.calls) > explicit

    @pytest.mark.asyncio
    async def test_simultaneous_auto_polls_collapse_to_one_probe(
        self,
        tmp_path: Path,
    ) -> None:
        # The floor in snapshot() is read OUTSIDE _probe_lock, so tabs polling in
        # the same instant all see the same stale timestamp and all pass it. What
        # stops N tabs from becoming N probes is handing the machine poll
        # force=False, so _probe's cache recheck -- which runs inside the lock,
        # after the winner refreshed the timestamp -- drops the queued callers.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.installed = True
        runtime.authenticated = True
        # Frozen clock: every caller reads an identical, floor-passing age, which
        # is the burst this guards against.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            process_runner=runtime.run,
            audit_writer=_no_audit,
            clock=lambda: 5_000.0,
        )

        await asyncio.gather(
            *(service.snapshot(force=True, coalesce=True) for _ in range(6))
        )

        # Exactly one probe's worth of spawns: --version then whoami.
        assert [args for _, args in runtime.calls] == [["--version"], ["whoami"]]

    def test_status_names_the_command_the_user_runs(self) -> None:
        # The UI needs the command to show, and the user runs it themselves.
        assert KIRO_CLI_LOGIN_COMMAND == "kiro-cli login"
        assert PrerequisiteStatus(platform="Linux").login_command == KIRO_CLI_LOGIN_COMMAND

    def test_status_names_the_organization_sso_command(self) -> None:
        # Both flags are pinned because either one alone silently does nothing:
        # kiro-cli discards every login flag unless --use-device-flow is set (or
        # the environment is already remote), so --license pro on its own falls
        # through to the browser portal where a free Builder ID is a peer option
        # -- the exact wrong-tier sign-in this command exists to rule out.
        assert KIRO_CLI_SSO_LOGIN_COMMAND == "kiro-cli login --use-device-flow --license pro"
        assert "--use-device-flow" in KIRO_CLI_SSO_LOGIN_COMMAND
        assert "--license pro" in KIRO_CLI_SSO_LOGIN_COMMAND
        assert (
            PrerequisiteStatus(platform="Linux").sso_login_command == KIRO_CLI_SSO_LOGIN_COMMAND
        )

    @pytest.mark.asyncio
    async def test_payload_keeps_an_idle_operation_for_pre_upgrade_tabs(
        self,
        tmp_path: Path,
    ) -> None:
        # No operation exists any more, but a dashboard loaded BEFORE this change
        # reads status.operation.status unconditionally (its optional chain guards
        # `status`, not `operation`) in a callback that runs for EVERY user. A tab
        # open across a gateway upgrade must not throw on its next poll, so the
        # payload keeps a permanently idle object.
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            assume_ready=True,
        )

        payload = await service.snapshot()

        assert payload["operation"] == {
            "kind": "",
            "status": "idle",
            "message": "",
            "detail": "",
            "url": "",
            "error": "",
        }
        # A fresh dict per call: a caller mutating one payload cannot poison the next.
        other = await service.snapshot()
        assert other["operation"] is not payload["operation"]

    def test_status_advertises_no_startable_action(self) -> None:
        status = PrerequisiteStatus(platform="Linux")
        for gone in ("can_login", "can_auto_install"):
            assert not hasattr(status, gone), gone

    def test_every_spawn_is_a_read_only_probe(self) -> None:
        # Only --version and whoami remain, so no spawn can mutate the user's
        # machine or write a credential.
        source = Path(prerequisite_module.__file__).read_text(encoding="utf-8")
        assert '"--version"' in source
        assert '"whoami"' in source
        # Matched as a STANDALONE QUOTED TOKEN, the shape a flag has when it is an
        # element of a spawn argv (and the same shape the two assertions above
        # use). The bare substring would also hit the sign-in command this module
        # SERVES for the user to type, which is a display string that is never
        # executed -- so matching it would forbid naming the flag while leaving
        # the actual "what gets spawned" question untested. The behavioral
        # guarantee is pinned separately by the argv-equality assertion in
        # test_repeated_forced_snapshots_probe_once.
        assert '"--use-device-flow"' not in source
        assert '"login"' not in source

    def test_no_installer_download_or_execution_surface_exists(self) -> None:
        for attribute in (
            "OFFICIAL_INSTALL_URL",
            "OFFICIAL_WINDOWS_INSTALL_URL",
            "_INSTALLER_SHA256",
            "_download_installer",
            "validate_installer_script",
            "official_installer_command",
            "_trusted_installer_url",
            "_trusted_installer_path",
            "_installer_proxy",
            "InstallerDownloader",
        ):
            assert not hasattr(prerequisite_module, attribute), attribute

    def test_service_exposes_no_install_operation(self) -> None:
        assert not hasattr(KiroPrerequisiteService, "start_install")
        assert not hasattr(KiroPrerequisiteService, "_install")
        assert not hasattr(KiroPrerequisiteService, "_attest_candidate")

    def test_status_carries_no_auto_install_capability(self) -> None:
        assert not hasattr(PrerequisiteStatus(platform="Linux"), "can_auto_install")

    def test_docs_url_points_at_kiros_official_setup_page(self) -> None:
        assert OFFICIAL_INSTALL_DOCS_URL == "https://kiro.dev/cli/"
        assert PrerequisiteStatus(platform="Linux").docs_url == OFFICIAL_INSTALL_DOCS_URL

    def test_process_runner_cannot_be_asked_for_an_unsandboxed_spawn(self) -> None:
        # `sandboxed=False` and `stdin_data` existed only to feed the installer
        # script to an unsandboxed interpreter. Every spawn left is sandboxed, so
        # there is no parameter through which to request otherwise.
        parameters = inspect.signature(_run_process).parameters
        assert "sandboxed" not in parameters
        assert "stdin_data" not in parameters


class TestSandboxUnavailableErrorIsTyped:
    """The sandbox refusal must be catchable structurally, not by prose match."""

    def test_error_carries_kind_and_detail_and_is_a_runtime_error(self) -> None:
        from kiro_crew.sandbox import SandboxUnavailableError

        exc = SandboxUnavailableError("refused", kind="no_backend", detail="EPERM at NEWNS")

        # RuntimeError subclass so existing ``except RuntimeError`` sites are unaffected.
        assert isinstance(exc, RuntimeError)
        assert exc.kind == "no_backend"
        assert exc.detail == "EPERM at NEWNS"


class TestAgentSpecsNarrowReadiness:
    """A viable binary + a good ``whoami`` are NOT sufficient for readiness.

    Kiro Crew's own agent specs (``~/.kiro/agents/kirocrew*.json``) were not an
    input to ``ready`` at all, so an install whose spec write failed reported
    ready while kiro-cli answered every ``session/set_mode`` with
    ``Mode '<name>' not found`` — the gate affirmatively told the user setup was
    complete on an install that could not run one turn. That is the customer
    report these tests pin.

    The status is set directly rather than probed: the probe's two subprocesses
    are irrelevant here (both of its inputs are already true in the scenario),
    and ``snapshot()`` without ``force`` reads the latch, so the overlay is what
    is under test.
    """

    @staticmethod
    def _service(tmp_path: Path) -> KiroPrerequisiteService:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._status = PrerequisiteStatus(
            platform="Linux",
            installed=True,
            authenticated=True,
            ready=True,
            initial_setup_complete=True,
        )
        service._has_probed = True
        return service

    @staticmethod
    def _agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, specs: bool) -> Path:
        """Point the agents dir at a tmp path, optionally fully populated."""
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        agents = tmp_path / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        if specs:
            for name in REQUIRED_KIRO_AGENT_FILES:
                (agents / name).write_text('{"name": "x"}', encoding="utf-8")
        monkeypatch.setattr(agent_module, "KIRO_AGENTS_DIR", agents)
        return agents

    @pytest.mark.asyncio
    async def test_missing_specs_make_a_ready_install_not_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        self._agents_dir(tmp_path, monkeypatch, specs=False)

        status = await self._service(tmp_path).snapshot()

        assert status["missing_agent_specs"] == list(REQUIRED_KIRO_AGENT_FILES)
        assert status["ready"] is False
        assert status["repair_required"] is True
        # Untouched: the binary IS installed and the user IS signed in. Reporting
        # either as false would send them to re-install or re-login for a
        # condition neither one causes.
        assert status["installed"] is True
        assert status["authenticated"] is True

    @pytest.mark.asyncio
    async def test_present_specs_leave_readiness_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._agents_dir(tmp_path, monkeypatch, specs=True)

        status = await self._service(tmp_path).snapshot()

        assert status["missing_agent_specs"] == []
        assert status["ready"] is True
        assert status["repair_required"] is False

    @pytest.mark.asyncio
    async def test_overlay_only_narrows_never_grants(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._agents_dir(tmp_path, monkeypatch, specs=True)
        service = self._service(tmp_path)
        service._status = replace(service._status, authenticated=False, ready=False)

        status = await service.snapshot()

        # Specs on disk say nothing about auth — present specs must not promote a
        # signed-out install to ready. The second assertion is what makes this
        # revert-verified: only the overlay populates that key, so a reverted
        # feature fails here instead of passing on the constructor's own False.
        assert status["ready"] is False
        assert status["missing_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_unreadable_agents_dir_does_not_block_a_working_install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed CHECK is not evidence of a missing spec."""
        from kiro_crew import agent as agent_module

        def _boom() -> list[str]:
            raise OSError("permission denied")

        monkeypatch.setattr(agent_module, "missing_required_agent_specs", _boom)

        status = await self._service(tmp_path).snapshot()

        assert status["ready"] is True
        assert status["missing_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_an_instance_that_may_not_write_the_specs_reports_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pod or worktree gateway must never be gated on specs it cannot write.

        ``_decline_shared_agent_home`` refuses the write for an ephemeral instance
        by design, and the overlay reads the AMBIENT machine-wide agents dir. On a
        host whose ``~/.kiro/agents`` is empty that combination would report
        ``ready=False`` permanently, put the whole dashboard behind the
        missing-spec screen, and offer a repair that declines every time -- turning
        "chat is broken" into "the product is unreachable" for exactly the
        instances an operator uses to diagnose.
        """
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch, specs=False)
        monkeypatch.setattr(
            agent_module,
            "_decline_shared_agent_home",
            lambda *, audit=True: tmp_path / "agents" / AGENT_FILENAME,
        )

        status = await self._service(tmp_path).snapshot()

        assert status["missing_agent_specs"] == []
        assert status["ready"] is True
        assert status["repair_required"] is False

    def test_the_decline_check_is_audit_free_when_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The status read must not write SEL records.

        ``_decline_shared_agent_home`` audits both its grant and its refusal
        because they are permission decisions about a WRITE. A polled status read
        is not one, and emitting a record every 30 seconds per client would bury
        the real decisions.
        """
        from kiro_crew import agent as agent_module

        events: list[str] = []

        class _Recorder:
            def log_api_access(self, **kwargs: Any) -> None:
                events.append(str(kwargs.get("outcome")))

        monkeypatch.setattr(agent_module, "sel", lambda: _Recorder())
        agent_module._decline_shared_agent_home(audit=False)

        assert events == []

    @pytest.mark.asyncio
    async def test_assume_ready_bypasses_the_overlay(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``assume_ready`` is the deliberate host-reality bypass.

        Test-mode gateways, fixtures and the offline E2E suite run on homes with
        no managed specs; applying the overlay there would put every one of them
        behind a repair gate.
        """
        self._agents_dir(tmp_path, monkeypatch, specs=False)
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=_no_audit,
            assume_ready=True,
        )

        status = await service.snapshot()

        assert status["ready"] is True
        assert status["missing_agent_specs"] == []

    @pytest.mark.asyncio
    async def test_session_ready_is_not_narrowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The overlay is dashboard-facing only, by design.

        ``session_ready()`` gates poll-driven spawn sites, and turn-starting paths
        deliberately do not block on it — they let the turn carry the failure,
        which now arrives as an actionable missing-spec message. Narrowing it here
        would re-introduce the stale-latch lockout that design removed.
        """
        self._agents_dir(tmp_path, monkeypatch, specs=False)
        service = self._service(tmp_path)

        assert (await service.snapshot())["ready"] is False
        assert await service.session_ready() is True


class TestAgentSpecRepairIsAPostNotAGet:
    """The repair must never be reachable from the status GET.

    ``/api/kiro-prerequisite`` is an ``add_get``, and BOTH dashboard barriers are
    method-scoped: ``csrf_middleware`` skips ``check_origin`` for
    ``{GET, HEAD, OPTIONS}`` and ``sel_audit_middleware`` logs only
    ``{POST, PUT, DELETE, PATCH}``. A spec rewrite hung off that GET would be
    cross-site triggerable (a ``SameSite=Lax`` cookie rides a top-level cross-site
    GET) and would leave no SEL record, so it lives on its own POST route.
    """

    @staticmethod
    def _service(tmp_path: Path) -> KiroPrerequisiteService:
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            home=tmp_path,
            audit_writer=_no_audit,
        )
        service._status = PrerequisiteStatus(
            platform="Linux",
            installed=True,
            authenticated=True,
            ready=True,
            initial_setup_complete=True,
        )
        service._has_probed = True

        async def _no_probe(*, force: bool = False) -> PrerequisiteStatus:
            del force
            return service._status

        service._probe = _no_probe  # type: ignore[method-assign]
        return service

    @staticmethod
    def _agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from kiro_crew import agent as agent_module

        agents = tmp_path / "agents"
        agents.mkdir(exist_ok=True)
        monkeypatch.setattr(agent_module, "KIRO_AGENTS_DIR", agents)
        return agents

    @pytest.mark.asyncio
    async def test_forced_status_read_never_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even ``?refresh=1`` is a pure read — this is the regression guard."""
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)
        calls: list[int] = []
        monkeypatch.setattr(
            agent_module, "rebuild_agent_config", lambda: calls.append(1)
        )

        status = await self._service(tmp_path).snapshot(force=True)

        assert calls == []
        # Still fully REPORTED — the read half of the feature is unaffected.
        assert status["missing_agent_specs"] != []
        assert status["ready"] is False

    def test_repair_route_accepts_only_POST(self) -> None:
        """Asserted over the ROUTE TABLE, not the module text.

        A source-text match proves nothing about what was registered and breaks on
        any reflow. What matters is the method the router will accept: GET is the
        one method csrf_middleware and sel_audit_middleware both skip.
        """
        from kiro_crew.dashboard.handlers.kiro_prerequisite import (
            api_kiro_prerequisite_repair_specs,
        )

        app = web.Application()
        app.router.add_post(
            "/api/kiro-prerequisite/repair-specs",
            api_kiro_prerequisite_repair_specs,
        )
        methods = {
            route.method
            for route in app.router.routes()
            if getattr(route.resource, "canonical", "")
            == "/api/kiro-prerequisite/repair-specs"
        }

        assert methods == {"POST"}, methods


class TestAgentSpecRepair:
    """``repair_agent_specs`` — the POST handler's service call."""

    _service = staticmethod(TestAgentSpecRepairIsAPostNotAGet._service)
    _agents_dir = staticmethod(TestAgentSpecRepairIsAPostNotAGet._agents_dir)

    @pytest.mark.asyncio
    async def test_writes_the_specs_and_returns_ready(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        agents = self._agents_dir(tmp_path, monkeypatch)
        calls: list[int] = []

        def _rebuild() -> Path:
            calls.append(1)
            for name in REQUIRED_KIRO_AGENT_FILES:
                (agents / name).write_text('{"name": "x"}', encoding="utf-8")
            return agents / REQUIRED_KIRO_AGENT_FILES[0]

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert calls == [1]
        # The response IS the post-repair snapshot, so one request fixes and
        # reports rather than making the user press the button twice.
        assert status["missing_agent_specs"] == []
        assert status["ready"] is True
        assert status["agent_spec_repair_error"] == ""

    @pytest.mark.asyncio
    async def test_failed_repair_reports_a_sanitized_exception(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The swallowed boot exception is what made the original undiagnosable.

        Sanitized like every other dashboard-facing string here: the rebuild's call
        graph merges ``mcpServers[*].env``, so raw exception text can carry a
        credential.
        """
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)

        def _rebuild() -> Path:
            raise FileNotFoundError("no shipped defaults.json")

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert "FileNotFoundError: no shipped defaults.json" in (
            status["agent_spec_repair_error"]
        )
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_credential_shaped_exception_text_is_redacted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)
        # Assembled at runtime so the literal never sits in the file for scrub-lint.
        secret = "ghp_" + "A" * 36

        def _rebuild() -> Path:
            raise RuntimeError(f"bad env GITHUB_TOKEN={secret}")

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert secret not in status["agent_spec_repair_error"]

    @pytest.mark.asyncio
    async def test_silent_no_op_rebuild_is_reported_as_a_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``rebuild_agent_config`` can decline WITHOUT raising.

        ``_decline_shared_agent_home`` returns early when an ephemeral instance
        would rewrite a shared agent home. Reporting that as success would leave
        the gate showing no error and a button that changes nothing on every press.
        """
        from kiro_crew import agent as agent_module

        self._agents_dir(tmp_path, monkeypatch)
        monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda: None)

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert "still missing" in status["agent_spec_repair_error"]
        assert "kirocrew setup --agent-only --clean" in status["agent_spec_repair_error"]
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_does_not_rebuild_over_a_present_main_spec(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A present kirocrew.json is never regenerated by this route.

        ``rebuild_agent_config`` rewrites the whole file. It re-merges
        ``mcpServers`` under ``bridges._mcp_lock``, but NOT the
        ``tools``/``allowedTools`` half ``api_mcp_toggle`` writes in a separate
        step — so rebuilding over an existing spec can drop a concurrent toggle's
        edit and resurrect a server the user just disabled. Gating on the MAIN
        spec's ABSENCE removes that window: with no file there is no edit to lose.
        """
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import LITE_AGENT_FILENAME

        agents = self._agents_dir(tmp_path, monkeypatch)
        (agents / AGENT_FILENAME).write_text('{"name": "kirocrew"}', encoding="utf-8")
        calls: list[int] = []
        monkeypatch.setattr(
            agent_module, "rebuild_agent_config", lambda: calls.append(1)
        )

        status = await self._service(tmp_path).repair_agent_specs("owner")

        assert calls == []
        assert status["missing_agent_specs"] == [LITE_AGENT_FILENAME]
        assert status["agent_spec_repair_error"] == ""

    @pytest.mark.asyncio
    async def test_concurrent_repairs_rebuild_exactly_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two owner POSTs landing together must produce ONE rebuild.

        ``operation_running`` does not cover repair -- it tracks ``_task``, which
        only install/login set -- so both callers pass that guard. Without the
        repair lock both observe the spec missing and both rebuild, and the second
        regenerates the file the first just wrote: the rebuild-over-an-existing-spec
        case the main-spec gate exists to avoid, which can drop a concurrent
        ``api_mcp_toggle`` write to ``tools``/``allowedTools``.

        The interleaving is FORCED rather than hoped for. An earlier version of
        this test simply gathered two calls and passed even with the lock removed,
        because the event loop happened to run them end to end. Here the stub parks
        inside the rebuild until the second caller has had its chance to reach its
        own check, which is the only ordering that distinguishes the two versions.
        """
        from kiro_crew import agent as agent_module
        from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

        agents = self._agents_dir(tmp_path, monkeypatch)
        calls: list[int] = []
        entered = threading.Event()
        release = threading.Event()

        def _rebuild() -> None:
            calls.append(1)
            entered.set()
            # Runs on a worker thread (asyncio.to_thread), so parking here does not
            # block the loop -- the second caller keeps running.
            release.wait(timeout=10)
            for name in REQUIRED_KIRO_AGENT_FILES:
                (agents / name).write_text('{"name": "x"}', encoding="utf-8")

        monkeypatch.setattr(agent_module, "rebuild_agent_config", _rebuild)
        service = self._service(tmp_path)

        first = asyncio.ensure_future(service.repair_agent_specs("owner"))
        # Wait until the first caller is INSIDE the rebuild, off-loop so the await
        # does not starve it.
        await asyncio.to_thread(entered.wait, 10)
        second = asyncio.ensure_future(service.repair_agent_specs("owner"))
        # Give the second caller room to run its own overlay + check. Unlocked, it
        # gets through both and starts a second rebuild here.
        for _ in range(20):
            await asyncio.sleep(0.01)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert calls == [1], "the second repair must be a no-op, not a second rebuild"
        # Both callers still get a truthful post-repair answer.
        for payload in (first_result, second_result):
            assert payload["missing_agent_specs"] == []
            assert payload["agent_spec_repair_error"] == ""
