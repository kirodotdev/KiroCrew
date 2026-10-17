"""Kiro CLI prerequisite tests: how the readiness probe finds, stages and runs a candidate.

Candidate resolution and the ``--version``, ``whoami`` and ``acp --help`` probes, the
environment each probe gets, the identity-store projection an isolated probe signs in
from, and what an API key or a missing ``acp`` subcommand means for readiness.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _FakeRuntime, _make_executable, _no_audit

from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.kiro_cli import BUNDLED_KIRO_CLI_ENTRY
from kiro_crew.kiro_prerequisite import (
    BUNDLED_CLI_UPDATE_REFUSAL,
    KIRO_CLI_UPDATE_COMMAND,
    KiroPrerequisiteService,
    ProcessResult,
)


class TestKiroPrerequisiteWorkflowProbe:
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
        assert calls == [["--version"], ["whoami"], ["acp", "--help"]]

    @pytest.mark.asyncio
    async def test_whoami_runs_against_unresolved_multiplexer_path(
        self,
        tmp_path: Path,
    ) -> None:
        # A multiplexer launcher (toolbox) dispatches on argv[0] basename, so
        # whoami/login must run against the resolved-but-symlink-named candidate
        # (``kiro-cli``), NOT its realpath (``toolbox-exec``). Guards against the
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
            if args in (["--version"], ["acp", "--help"]):
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
            if args == ["acp", "--help"]:
                return ProcessResult(ok=True, output="Usage: kiro-cli acp")
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
            (str(planted), ["acp", "--help"]),
        ]

    @pytest.mark.asyncio
    async def test_windows_override_takes_priority_over_program_files_candidate(
        self,
        tmp_path: Path,
    ) -> None:
        # The explicit override wins over a Program Files install (ACP resolves
        # the override first), and being outside Program Files does not block
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
            (str(planted), ["acp", "--help"]),
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
            (str(official), ["acp", "--help"]),
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


class TestKiroPrerequisiteWorkflowIdentityStore:
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
        # not byte-copied, so a large store must not abort sign-in.)
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
            db.execute("create table conversations_v2 (key text primary key, value text)")
            db.execute("create index idx_conv_v2_key on conversations_v2(key)")
            db.execute("create table state (key text primary key, value blob)")
            db.execute("insert into auth_kv values ('kirocli:odic:token', 'tok-secret')")
            db.execute("insert into auth_kv values ('kirocli:odic:device-registration', 'reg')")
            db.execute("insert into migrations values (1, 11, 0)")
            # Identity-describing state rows (must project) …
            db.execute("insert into state values ('auth.idc.region', 'us-east-1')")
            db.execute("insert into state values ('auth.idc.start-url', 'https://example')")
            db.execute("insert into state values ('api.codewhisperer.profile', 'arn')")
            # … alongside unrelated local state (must NOT project).
            db.execute("insert into state values ('telemetryClientId', 'tele-id')")
            db.execute("insert into state values ('desktop.completedOnboarding', '1')")
            for index in range(transcript_rows):
                db.execute("insert into history values (?, ?)", (index, f"chat-{index}" * 64))
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

        Byte-copying the identity DB under ``_MAX_AUTH_STORE_FILE_BYTES`` (64 MB)
        aborts on a large store. A real user's store had grown to
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

    def test_projection_carries_identity_and_drops_transcripts(self, tmp_path: Path) -> None:
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
            assert db.execute("select count(*) from conversations_v2").fetchone()[0] == 0
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
        assert (
            identity.get("kirocli:odic:token") == "wal-tok"
        ), "WAL-resident identity must be projected, not read as signed-out"

    def test_projection_refuses_symlinked_and_non_database_sources(self, tmp_path: Path) -> None:
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

    def test_projection_refuses_a_store_with_no_identity_table(self, tmp_path: Path) -> None:
        """Fail closed: never hand the CLI a store it would read as signed-out."""
        source = tmp_path / "data.sqlite3"
        with contextlib.closing(sqlite3.connect(source)) as db:
            db.execute("create table history (id integer primary key)")
            db.commit()
        destination = tmp_path / "staged" / "data.sqlite3"

        assert not prerequisite_module._project_identity_database(source, destination)
        assert not destination.exists()

    def test_projection_refuses_when_only_some_identity_tables_exist(self, tmp_path: Path) -> None:
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

    def test_projection_stages_a_store_without_the_state_table(self, tmp_path: Path) -> None:
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


class TestKiroCliApiKeyCountsAsSignedIn:
    """A host authenticated by Kiro CLI's own API-key env var reads as signed in.

    ``kiro-cli`` accepts a model credential through ``KIRO_API_KEY`` as an
    alternative to a ``kiro-cli login`` token store, and ``whoami`` succeeds only
    when it can see that variable. Filtering it out of the probe environment
    latches ``authenticated=False`` on a host where an ACP session — which
    inherits the real environment — authenticates fine, so the first-run gate
    demands a sign-in the user has already done and every ``verified_ready``
    caller answers 503.
    """

    @staticmethod
    def _service(
        tmp_path: Path,
        run: Any,
        *,
        api_key: str = "",
    ) -> KiroPrerequisiteService:
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        environ = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
        if api_key:
            environ["KIRO_API_KEY"] = api_key
        return KiroPrerequisiteService(
            platform_name="linux",
            environ=environ,
            home=tmp_path,
            process_runner=run,
            audit_writer=_no_audit,
        )

    @pytest.mark.asyncio
    async def test_identity_probe_gets_the_key_and_version_probe_does_not(
        self,
        tmp_path: Path,
    ) -> None:
        """Only ``whoami`` carries the credential; ``--version`` never needs one."""

        seen: dict[str, dict[str, str]] = {}

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            seen[args[0]] = dict(kwargs.get("env") or {})
            return ProcessResult(ok=True)

        service = self._service(tmp_path, run, api_key="key-value")

        await service.snapshot(force=True)

        assert seen["whoami"].get("KIRO_API_KEY") == "key-value"
        assert "KIRO_API_KEY" not in seen["--version"]

    @pytest.mark.asyncio
    async def test_api_key_host_latches_authenticated(self, tmp_path: Path) -> None:
        """With the key visible, ``whoami`` succeeds and readiness follows it."""

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            # Mimic the CLI: `whoami` reports a signed-in identity only when the
            # API key reaches it. `--version` runs regardless.
            if args == ["--version"]:
                return ProcessResult(ok=True)
            env = kwargs.get("env") or {}
            return ProcessResult(ok=bool(env.get("KIRO_API_KEY")))

        service = self._service(tmp_path, run, api_key="key-value")

        status = await service.snapshot(force=True)

        assert status["authenticated"] is True
        assert status["ready"] is True
        assert await service.session_ready() is True

    @pytest.mark.asyncio
    async def test_a_rejected_key_is_still_signed_out(self, tmp_path: Path) -> None:
        """Presence of the variable is not itself proof of a signed-in identity.

        ``whoami`` remains the sole authority: a key the CLI rejects must leave
        readiness signed out. This pins the invariant against the shortcut a later
        reader might reach for — treating a set ``KIRO_API_KEY`` as authentication
        and skipping the probe — which would latch ``ready=True`` on a stale or
        mistyped key and turn a "sign in" prompt into a failure on every turn.
        """

        async def run(
            _command: str,
            args: list[str],
            **_kwargs: Any,
        ) -> ProcessResult:
            # The key reaches the CLI; the CLI does not accept it.
            return ProcessResult(ok=args == ["--version"])

        service = self._service(tmp_path, run, api_key="rejected-key")

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["authenticated"] is False
        assert status["ready"] is False
        assert await service.session_ready() is False

    @pytest.mark.asyncio
    async def test_absent_api_key_is_not_a_false_positive(self, tmp_path: Path) -> None:
        """No key and no token store must still read as signed out."""

        async def run(
            _command: str,
            args: list[str],
            **kwargs: Any,
        ) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            env = kwargs.get("env") or {}
            return ProcessResult(ok=bool(env.get("KIRO_API_KEY")))

        service = self._service(tmp_path, run)

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["authenticated"] is False
        assert status["ready"] is False


class TestAcpSubcommandSupportNarrowsReadiness:
    """A signed-in CLI too old for the `acp` subcommand cannot start a session.

    It runs and authenticates, so the pre-existing probes both pass — but every
    session launches as `kiro-cli acp ...`, so a CLI without that subcommand
    fails at session-create with an opaque error. The readiness probe therefore
    asks `acp --help` and narrows `ready` when the subcommand is unknown, with
    an UPDATE (not a reinstall) as the remedy.
    """

    def _service(
        self,
        tmp_path: Path,
        runner: Any,
    ) -> KiroPrerequisiteService:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        return KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runner,
            audit_writer=_no_audit,
        )

    @pytest.mark.asyncio
    async def test_unknown_acp_subcommand_narrows_ready(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            if args == ["whoami"]:
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                # A clap CLI too old to have the subcommand exits nonzero here.
                return ProcessResult(
                    ok=False,
                    returncode=2,
                    output="error: unrecognized subcommand 'acp'",
                )
            return ProcessResult(ok=False)

        status = await self._service(tmp_path, run).snapshot(force=True)

        # Installed and authenticated, but not ready: the acp subcommand is the
        # missing piece, and its remedy is an update.
        assert status["installed"] is True
        assert status["authenticated"] is True
        assert status["acp_supported"] is False
        assert status["ready"] is False
        assert status["update_command"] == KIRO_CLI_UPDATE_COMMAND

    @pytest.mark.asyncio
    async def test_present_acp_subcommand_is_ready(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["acp", "--help"]:
                return ProcessResult(ok=True, output="Usage: kiro-cli acp [OPTIONS]")
            return ProcessResult(ok=True)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["acp_supported"] is True
        assert status["ready"] is True

    @pytest.mark.asyncio
    async def test_probe_that_cannot_run_is_treated_as_supported(self, tmp_path: Path) -> None:
        """A timeout or unrecognized failure must NOT be reported as too-old.

        Only a clean "unknown subcommand" rejection sets acp_supported False; a
        probe that merely failed to run leaves a healthy install ready rather
        than blocking it behind an update card it does not need.
        """

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["acp", "--help"]:
                # Failed, but with no recognizable rejection wording (e.g. a
                # transient spawn error captured as generic text).
                return ProcessResult(ok=False, returncode=1, output="some unrelated error")
            return ProcessResult(ok=True)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["acp_supported"] is True
        assert status["ready"] is True

    @pytest.mark.asyncio
    async def test_acp_probe_runs_independently_of_whoami(self, tmp_path: Path) -> None:
        """KAS needs ACP support even when kiro-cli is signed out."""
        acp_probed = False

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            nonlocal acp_probed
            if args == ["--version"]:
                return ProcessResult(ok=True)
            if args == ["whoami"]:
                return ProcessResult(ok=False)
            if args == ["acp", "--help"]:
                acp_probed = True
                return ProcessResult(ok=True, output="Usage: kiro-cli acp [OPTIONS]")
            return ProcessResult(ok=False)

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["authenticated"] is False
        assert acp_probed is True
        assert status["acp_supported"] is True
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_update_cli_runs_the_self_update_and_reprobes(self, tmp_path: Path) -> None:
        """A successful `kiro-cli update` flips acp_supported and clears ready."""
        acp_ok = {"value": False}

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"] or args == ["whoami"]:
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                if acp_ok["value"]:
                    return ProcessResult(ok=True, output="Usage: kiro-cli acp")
                return ProcessResult(ok=False, returncode=2, output="unrecognized subcommand 'acp'")
            if args == ["update"]:
                # The update makes the next acp probe succeed.
                acp_ok["value"] = True
                return ProcessResult(ok=True, output="updated")
            return ProcessResult(ok=False)

        service = self._service(tmp_path, run)
        before = await service.snapshot(force=True)
        assert before["acp_supported"] is False and before["ready"] is False

        after = await service.update_cli("owner")

        assert after["cli_update_error"] == ""
        assert after["acp_supported"] is True
        assert after["ready"] is True

    @pytest.mark.asyncio
    async def test_update_cli_reports_a_nonzero_update_failure(self, tmp_path: Path) -> None:
        """A failed update surfaces its output as cli_update_error, not a crash."""

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"] or args == ["whoami"]:
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                return ProcessResult(ok=False, returncode=2, output="unrecognized subcommand 'acp'")
            if args == ["update"]:
                return ProcessResult(
                    ok=False, returncode=1, output="update failed: network unreachable"
                )
            return ProcessResult(ok=False)

        service = self._service(tmp_path, run)
        # The gate probes and finds acp unsupported before offering the update.
        before = await service.snapshot(force=True)
        assert before["acp_supported"] is False
        result = await service.update_cli("owner")

        assert "network unreachable" in result["cli_update_error"]
        # Still not ready — the update did not fix anything.
        assert result["acp_supported"] is False

    @pytest.mark.asyncio
    async def test_update_cli_refuses_the_bundled_copy_without_spawning(
        self, tmp_path: Path
    ) -> None:
        """The desktop app's bundled kiro-cli is never self-updated in place.

        It sits inside the signed app bundle, so a write there breaks the seal;
        the app update replaces it. The refusal is a served error, and no
        ``update`` spawn happens at all."""
        bundled_dir = tmp_path / "resources" / "kiro-cli"
        executable = bundled_dir / BUNDLED_KIRO_CLI_ENTRY
        _make_executable(executable)
        spawned: list[list[str]] = []

        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            spawned.append(list(args))
            if args == ["--version"] or args == ["whoami"]:
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                return ProcessResult(ok=False, returncode=2, output="unrecognized subcommand 'acp'")
            return ProcessResult(ok=False)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": "",
                "KIROCREW_BUNDLED_KIRO_DIR": str(bundled_dir),
            },
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=run,
            audit_writer=_no_audit,
        )
        before = await service.snapshot(force=True)
        # The bundled copy is what resolved: its absolute path is the served
        # sign-in command, since the copy is not on the user's shell PATH, and
        # the status says so, which is what lets the gate explain the path.
        assert str(executable) in before["login_command"].replace("'", "")
        assert before["bundled_cli"] is True

        result = await service.update_cli("owner")

        assert result["cli_update_error"] == BUNDLED_CLI_UPDATE_REFUSAL
        assert ["update"] not in spawned

    @pytest.mark.asyncio
    async def test_update_cli_runs_unverified_binary_under_strict_sandbox(
        self, tmp_path: Path
    ) -> None:
        """The self-update spawn hardens against a planted binary.

        ``candidates[0]`` is whatever sits first on PATH, so the update must run
        under the strict sandbox (``~/.aws`` / ``~/.ssh`` hidden) — not the
        standard, real-home posture — and must forward proxy config for network
        reach WITHOUT handing the unverified binary the Kiro identity credential.
        """
        seen: dict[str, Any] = {}

        async def run(_command: str, args: list[str], **kwargs: Any) -> ProcessResult:
            if args == ["--version"] or args == ["whoami"]:
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                return ProcessResult(ok=False, returncode=2, output="unrecognized subcommand 'acp'")
            if args == ["update"]:
                seen.update(kwargs)
                return ProcessResult(ok=True, output="updated")
            return ProcessResult(ok=False)

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": str(executable.parent),
                "HTTPS_PROXY": "http://proxy.example:8080",
                prerequisite_module.CRED_KIRO_API_KEY: "secret-token",
            },
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=run,
            audit_writer=_no_audit,
        )
        await service.snapshot(force=True)
        await service.update_cli("owner")

        assert seen.get("sandbox_mode") == prerequisite_module._UNVERIFIED_SANDBOX_MODE
        # The identity token store must be hidden from the unverified binary too —
        # same isolation the read-only probe applies, not the weaker crew-only set.
        assert seen.get("extra_hidden_dirs") == service._hidden_probe_dirs
        env = seen.get("env") or {}
        assert env.get("HTTPS_PROXY") == "http://proxy.example:8080"
        assert prerequisite_module.CRED_KIRO_API_KEY not in env
