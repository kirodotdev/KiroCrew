"""Kiro CLI prerequisite tests: the readiness state the gateway serves between probes.

The fail-closed session gate, the latched snapshot and its freshness, sign-out latching,
the boot warm-up probe, the audit events each probe writes, and the first-run setup record
for Kiro, for KAS and for a harness set up independently.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from kiro_prerequisite_helpers import _agents_dir_never_the_real_home  # noqa: F401
from kiro_prerequisite_helpers import _isolate_agent_specs  # noqa: F401
from kiro_prerequisite_helpers import _FakeRuntime, _make_executable, _no_audit

from kiro_crew import kiro_prerequisite as prerequisite_module
from kiro_crew.dashboard.kiro_readiness import kiro_session_ready
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService, ProcessResult


class TestKiroPrerequisiteWorkflowReadinessLatch:
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
    async def test_explicit_test_harness_mode_assumes_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import sandbox

        def no_host_probe():
            raise AssertionError("test harness readiness must not probe sandbox facts")

        monkeypatch.setattr(sandbox, "detect_backend", no_host_probe)

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
        assert status["sandbox_backend_available"] is True
        assert status["sandbox_blocked_backends"] == []

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

        # A runnable CLI gets identity and ACP checks even when signed out;
        # every probe keeps its paired audit lifecycle.
        assert [(item["action"], item["outcome"]) for item in events] == [
            ("probe_version", "invoked"),
            ("probe_version", "completed"),
            ("probe_identity", "invoked"),
            ("probe_identity", "failed"),
            ("probe_acp_support", "invoked"),
            ("probe_acp_support", "completed"),
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
                # Proxy configuration never reaches the unvalidated --version
                # candidate (a proxy URL can embed credentials); it joins only
                # the whoami stage below. Desktop IPC beyond the session bus
                # stays excluded.
                assert "HTTPS_PROXY" not in runtime.kwargs[index]["env"]
                assert "DISPLAY" not in runtime.kwargs[index]["env"]
                # The session bus IS forwarded — some CLI builds connect to
                # the D-Bus secret-service keyring even at --version (AL2023).
                assert (
                    runtime.kwargs[index]["env"].get("DBUS_SESSION_BUS_ADDRESS")
                    == "unix:path=/tmp/bus"
                )
            if call[1] == ["whoami"]:
                # A proxy-only host is exactly where whoami must reach the IdP
                # the way the user's shell does, so the identity stage carries
                # the proxy configuration the version stage withheld.
                assert (
                    runtime.kwargs[index]["env"].get("HTTPS_PROXY")
                    == "http://secret@proxy.example:8443"
                )
                assert "DISPLAY" not in runtime.kwargs[index]["env"]

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


class TestTerminalAuditErrorLabels:
    """Pin the three SEL terminal-audit error labels to their exact values.

    ``_terminal_audit_detail`` is the single source for the ``error`` field on
    the terminal audit event of every finished kiro-cli child run — consumed by
    ``update_cli``, ``_audited_probe`` (probe_version / probe_acp_support), and
    ``_audited_identity_probe`` (probe_identity). Before these tests nothing
    read the label's VALUE off an emitted event, so the mapping could change,
    or invert "timeout" and "nonzero exit", with the suite staying green.
    """

    @pytest.mark.parametrize(
        ("succeeded", "result", "label"),
        [
            # The success verdict always maps to the empty label.
            (True, ProcessResult(ok=True), ""),
            # *succeeded* is the caller's verdict, not ``result.ok``, so it
            # wins even over a result that says timed_out: the label can never
            # contradict the ``outcome`` recorded beside it.
            (True, ProcessResult(ok=False, timed_out=True), ""),
            (False, ProcessResult(ok=False, timed_out=True), "timeout"),
            (
                False,
                ProcessResult(ok=False, timed_out=False, returncode=1),
                "nonzero exit",
            ),
            # No caller reaches succeeded=False with result.ok=True today, but
            # the second parameter permits it: a failed verdict over an ok
            # result is labeled as an exit failure, never as silence.
            (False, ProcessResult(ok=True), "nonzero exit"),
        ],
    )
    def test_detail_matrix(self, succeeded: bool, result: ProcessResult, label: str) -> None:
        assert prerequisite_module._terminal_audit_detail(result, succeeded) == label

    # -- End-to-end: the label as it lands on the emitted SEL event ----------

    def _service_with_audit(
        self, tmp_path: Path, run: Any
    ) -> tuple[KiroPrerequisiteService, list[dict[str, Any]]]:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        events: list[dict[str, Any]] = []

        async def audit(**kwargs: Any) -> None:
            events.append(kwargs)

        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": str(executable.parent)},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=run,
            audit_writer=audit,
        )
        return service, events

    @staticmethod
    def _terminal_labels(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
        """The (action, outcome) audit inspection, extended with the error label."""
        return [
            (item["action"], item["outcome"], item["error"])
            for item in events
            if item["outcome"] != "invoked"
        ]

    @staticmethod
    def _assert_outcome_and_error_agree(events: list[dict[str, Any]]) -> None:
        """``completed`` and an empty error label imply each other, both ways."""
        for item in events:
            if item["outcome"] == "invoked":
                continue
            assert (item["outcome"] == "completed") == (item["error"] == ""), item

    @pytest.mark.asyncio
    async def test_successful_probes_carry_the_empty_label(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args in (["--version"], ["whoami"]):
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                return ProcessResult(ok=True, output="Usage: kiro-cli acp")
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        await service.snapshot(force=True)

        assert self._terminal_labels(events) == [
            ("probe_version", "completed", ""),
            ("probe_identity", "completed", ""),
            ("probe_acp_support", "completed", ""),
        ]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_timed_out_version_probe_is_labeled_timeout(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=False, timed_out=True)
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        await service.snapshot(force=True)

        # A failed version probe short-circuits the pipeline: no whoami runs.
        assert self._terminal_labels(events) == [
            ("probe_version", "failed", "timeout"),
        ]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_nonzero_version_probe_is_labeled_nonzero_exit(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=False, returncode=1)
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        await service.snapshot(force=True)

        assert self._terminal_labels(events) == [
            ("probe_version", "failed", "nonzero exit"),
        ]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_timed_out_identity_probe_is_labeled_timeout(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            if args == ["whoami"]:
                return ProcessResult(ok=False, timed_out=True)
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        await service.snapshot(force=True)

        # ACP support is probed even when the identity probe times out.
        assert self._terminal_labels(events) == [
            ("probe_version", "completed", ""),
            ("probe_identity", "failed", "timeout"),
            ("probe_acp_support", "failed", "nonzero exit"),
        ]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_signed_out_identity_probe_is_labeled_nonzero_exit(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["--version"]:
                return ProcessResult(ok=True)
            if args == ["whoami"]:
                return ProcessResult(ok=False, returncode=1, output="Not logged in")
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        await service.snapshot(force=True)

        assert self._terminal_labels(events) == [
            ("probe_version", "completed", ""),
            ("probe_identity", "failed", "nonzero exit"),
            ("probe_acp_support", "failed", "nonzero exit"),
        ]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_successful_update_carries_the_empty_label(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["update"]:
                return ProcessResult(ok=True, output="updated")
            if args in (["--version"], ["whoami"]):
                return ProcessResult(ok=True)
            if args == ["acp", "--help"]:
                return ProcessResult(ok=True, output="Usage: kiro-cli acp")
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        await service.update_cli("owner")

        # The post-update re-probe appends its own probe events; the update's
        # terminal event is pinned by filtering to its action.
        update_labels = [item for item in self._terminal_labels(events) if item[0] == "update_cli"]
        assert update_labels == [("update_cli", "completed", "")]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_timed_out_update_is_labeled_timeout(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["update"]:
                return ProcessResult(ok=False, timed_out=True)
            if args in (["--version"], ["whoami"]):
                return ProcessResult(ok=True)
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        result = await service.update_cli("owner")

        assert "did not finish in time" in result["cli_update_error"]
        update_labels = [item for item in self._terminal_labels(events) if item[0] == "update_cli"]
        assert update_labels == [("update_cli", "failed", "timeout")]
        self._assert_outcome_and_error_agree(events)

    @pytest.mark.asyncio
    async def test_nonzero_update_is_labeled_nonzero_exit(self, tmp_path: Path) -> None:
        async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            if args == ["update"]:
                return ProcessResult(ok=False, returncode=1, output="update failed")
            if args in (["--version"], ["whoami"]):
                return ProcessResult(ok=True)
            return ProcessResult(ok=False)

        service, events = self._service_with_audit(tmp_path, run)
        result = await service.update_cli("owner")

        assert result["cli_update_error"] == "update failed"
        update_labels = [item for item in self._terminal_labels(events) if item[0] == "update_cli"]
        assert update_labels == [("update_cli", "failed", "nonzero exit")]
        self._assert_outcome_and_error_agree(events)


@pytest.mark.asyncio
class TestKasRecordsFirstRunSetup:
    """KAS (kiro-cli's relay) records first-run completion on the acp_supported
    rule, in lock-step with the onboarding gate's client admission.

    Before the fix ``record_independent_backend_setup`` refused KAS outright (it
    is deliberately absent from ``ACP_BACKENDS_INDEPENDENT_SETUP``), so a KAS
    first run never wrote the durable marker and ``initial_setup_complete``
    stayed false forever — the gate force-probed kiro-cli and a non-owner was
    pinned on the owner-setup screen. The client admits KAS when
    ``acp_supported`` holds; this test pins the server marker to the SAME rule.
    """

    def _service(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        acp_supported: bool,
        runner: Any = None,
        resolved: bool = True,
    ):
        from kiro_crew import acp_backends, sandbox
        from kiro_crew.agent_sdk import backend_install

        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        if runner is None and resolved:
            # A gateway whose probe already resolved the kiro-cli that sessions
            # launch, so recording re-asks only that binary for ACP support.
            _make_executable(executable)
            runner = _FakeRuntime(executable).run
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=runner,
            audit_writer=_no_audit,
        )
        service._status = replace(service._status, acp_supported=acp_supported)
        if resolved:
            service._viable_binary = str(executable)
        monkeypatch.setattr(sandbox, "detect_backend", lambda: "bwrap")
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "standard")
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: True)
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: {"kas"})
        monkeypatch.setattr(
            backend_install,
            "probe_backend",
            lambda backend: backend_install.BackendInstallState(
                backend, backend, backend_install.INSTALLED
            ),
        )
        return service

    @pytest.mark.parametrize("installed", [False, True], ids=["no-engine", "engine-installed"])
    async def test_kas_bundle_route_records_setup_from_the_installed_engine_not_kiro_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
    ) -> None:
        # agent.kas_bundle on, and NO kiro-cli anywhere on this host: the route
        # never runs kiro-cli, so the marker must follow the installed engine --
        # and nothing may try to spawn the absent binary while deciding.
        from kiro_crew.agent_sdk import kas_bundle

        calls: list[tuple[str, list[str]]] = []

        async def _no_binary_runner(executable: str, args: list[str], **_kw: Any) -> Any:
            calls.append((executable, args))
            raise AssertionError("the bundle route must not spawn kiro-cli")

        service = self._service(
            tmp_path, monkeypatch, acp_supported=True, runner=_no_binary_runner, resolved=False
        )
        monkeypatch.setattr(
            KiroPrerequisiteService, "_kas_bundle_route", staticmethod(lambda: True)
        )
        monkeypatch.setattr(
            kas_bundle,
            "installed_bundle",
            lambda *a, **k: object() if installed else None,
        )

        assert await service.record_independent_backend_setup("kas") is installed
        assert service.initial_setup_complete is installed
        assert service._setup_marker.exists() is installed
        assert calls == []

    @pytest.mark.parametrize("acp_supported", [False, True], ids=["too-old", "acp-capable"])
    async def test_signed_out_kas_setup_uses_probed_acp_support(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acp_supported: bool
    ) -> None:
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.acp_supported = acp_supported
        service = self._service(tmp_path, monkeypatch, acp_supported=True, runner=runtime.run)

        status = await service.snapshot(force=True)

        assert status["installed"] is True
        assert status["authenticated"] is False
        assert status["ready"] is False
        assert status["acp_supported"] is acp_supported
        assert status["initial_setup_complete"] is False
        assert not service._setup_marker.exists()
        assert runtime.calls == [
            (str(executable), ["--version"]),
            (str(executable), ["whoami"]),
            (str(executable), ["acp", "--help"]),
        ]

        assert await service.record_independent_backend_setup("kas") is acp_supported
        assert service.initial_setup_complete is acp_supported
        assert service._setup_marker.exists() is acp_supported
        if acp_supported:
            assert service._setup_marker.read_text(encoding="utf-8") == "complete\n"

    async def test_kas_setup_reprobes_acp_support_when_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The probe saw an ACP-capable kiro-cli; it is then replaced by a build
        # without `acp`. Recording must ask the binary again, not trust the
        # earlier verdict, and must serve the too-old status afterwards.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        service = self._service(tmp_path, monkeypatch, acp_supported=True, runner=runtime.run)

        status = await service.snapshot(force=True)
        assert status["acp_supported"] is True

        runtime.acp_supported = False
        runtime.calls.clear()

        assert await service.record_independent_backend_setup("kas") is False
        assert runtime.calls == [(str(executable), ["acp", "--help"])]
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()
        assert service._status.acp_supported is False

    @pytest.mark.parametrize("acp_supported", [False, True], ids=["too-old", "acp-capable"])
    async def test_kas_setup_before_any_probe_resolves_and_checks_the_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acp_supported: bool
    ) -> None:
        # Recording can arrive before the boot probe has resolved a kiro-cli
        # (its warm-up delay), while acp_supported still reads True. Recording
        # must resolve the binary sessions launch and check it for `acp`.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        runtime = _FakeRuntime(executable)
        runtime.acp_supported = acp_supported
        service = self._service(
            tmp_path, monkeypatch, acp_supported=True, runner=runtime.run, resolved=False
        )
        assert service._viable_binary == ""

        assert await service.record_independent_backend_setup("kas") is acp_supported
        assert (str(executable), ["--version"]) in runtime.calls
        assert (str(executable), ["acp", "--help"]) in runtime.calls
        assert service._status.acp_supported is acp_supported
        assert service.initial_setup_complete is acp_supported
        assert service._setup_marker.exists() is acp_supported

    async def test_kas_setup_refused_when_no_kiro_cli_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # KAS runs on kiro-cli, so with no runnable kiro-cli there is nothing a
        # KAS session could launch and the marker must not be written.
        runtime = _FakeRuntime(tmp_path / ".local" / "bin" / "kiro-cli")
        service = self._service(
            tmp_path, monkeypatch, acp_supported=True, runner=runtime.run, resolved=False
        )

        assert await service.record_independent_backend_setup("kas") is False
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()

    async def test_kas_records_setup_when_kiro_cli_speaks_acp(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service = self._service(tmp_path, monkeypatch, acp_supported=True)
        # A gateway that already probed at boot: ordinary polls serve the latch.
        service._has_probed = True

        async def _same_identity() -> bool:
            return False

        monkeypatch.setattr(service, "identity_changed_since_probe", _same_identity)
        assert service.initial_setup_complete is False
        assert await service.record_independent_backend_setup("kas") is True
        assert service.initial_setup_complete is True
        # The SERVED snapshot must agree without a forced probe: a non-owner's
        # poll cannot force one, so a stale latch would hold it on setup forever.
        assert (await service.snapshot())["initial_setup_complete"] is True

    async def test_kas_refused_when_kiro_cli_too_old_for_acp(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A too-old kiro-cli (acp_supported False) blocks KAS here, matching the
        # client's `status.acp_supported !== false` refusal.
        service = self._service(tmp_path, monkeypatch, acp_supported=False)
        assert await service.record_independent_backend_setup("kas") is False
        assert service.initial_setup_complete is False

    async def test_kas_refused_when_an_agent_spec_is_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # KAS loads Kiro Crew's agent specs, so a missing spec keeps the gate on
        # its repair card (client) and must not write the marker (server).
        service = self._service(tmp_path, monkeypatch, acp_supported=True)
        monkeypatch.setattr(
            KiroPrerequisiteService,
            "_missing_agent_specs",
            staticmethod(lambda: ["kirocrew-lite"]),
        )
        assert await service.record_independent_backend_setup("kas") is False
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()

    async def test_kas_refused_when_an_agent_spec_is_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service = self._service(tmp_path, monkeypatch, acp_supported=True)
        service._status = replace(service._status, rejected_agent_specs=["kirocrew"])
        assert await service.record_independent_backend_setup("kas") is False
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()

    async def test_kas_refused_when_not_selectable(self, tmp_path: Path, monkeypatch) -> None:
        from kiro_crew import acp_backends

        service = self._service(tmp_path, monkeypatch, acp_supported=True)
        # KAS is only selectable in Developer Mode; when it is not offered, the
        # marker is refused just as the client gates on `probe.selectable`.
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: set())
        assert await service.record_independent_backend_setup("kas") is False
        assert service.initial_setup_complete is False


@pytest.mark.asyncio
class TestUnsandboxedExecRecordsIndependentSetup:
    """A non-enforced harness finishes first-run setup on a backend-less host
    that permits unsandboxed exec (native Windows platform default, or an
    operator opt-in), matching the client's ``configuredBackendCanStart``.

    Before the fix ``record_independent_backend_setup`` refused whenever
    ``sandbox_backend_available`` was false, for EVERY backend — so Claude Code
    / KAS could never complete setup on native Windows (``detect_backend`` is
    permanently "none" there while unsandboxed exec is the platform default),
    even though a session would start fine. A non-enforced harness needs no Crew
    OS credential mask; an enforced one stays held back by
    ``sandbox_blocked_backends``.
    """

    def _service(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        unsandboxed_permitted: bool,
        mask_applies: bool = True,
        selectable=("claude", "codex"),
    ):
        from kiro_crew import acp_backends, sandbox
        from kiro_crew.agent_sdk import backend_install

        # KAS runs on kiro-cli: give the gateway the binary its probe resolved,
        # so a KAS case reaches the sandbox rule this class is about.
        executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(executable)
        service = KiroPrerequisiteService(
            platform_name="win32",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            data_home=tmp_path / "data-home",
            process_runner=_FakeRuntime(executable).run,
            audit_writer=_no_audit,
        )
        service._viable_binary = str(executable)
        # A backend-less host: the Crew OS sandbox reports "none".
        monkeypatch.setattr(sandbox, "detect_backend", lambda: "none")
        monkeypatch.setattr(
            sandbox,
            "unsandboxed_exec_permitted_by",
            lambda: "platform" if unsandboxed_permitted else "",
        )
        monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "standard")
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: mask_applies)
        monkeypatch.setattr(acp_backends, "selectable_backend_values", lambda: set(selectable))
        monkeypatch.setattr(
            backend_install,
            "probe_backend",
            lambda backend: backend_install.BackendInstallState(
                backend, backend, backend_install.INSTALLED
            ),
        )
        return service

    async def test_no_backend_but_permitted_records_claude(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service = self._service(tmp_path, monkeypatch, unsandboxed_permitted=True)
        assert service.initial_setup_complete is False
        assert await service.record_independent_backend_setup("claude") is True
        assert service.initial_setup_complete is True
        assert service._setup_marker.exists()

    @pytest.mark.parametrize("backend", ["claude", "kas"])
    @pytest.mark.parametrize("grant", ["platform", "operator"])
    @pytest.mark.parametrize(
        "floor,permitted",
        [(None, True), ("off", True), ("standard", False), ("cc", False), ("strict", False)],
    )
    async def test_governed_floor_overrides_unsandboxed_setup_grant(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        grant: str,
        floor: str | None,
        permitted: bool,
    ) -> None:
        from kiro_crew import sandbox
        from kiro_crew.platform import governance_profiles

        service = self._service(
            tmp_path,
            monkeypatch,
            unsandboxed_permitted=True,
            mask_applies=False,
            selectable=(backend,),
        )
        monkeypatch.setattr(sandbox, "unsandboxed_exec_permitted_by", lambda: grant)

        def governed_floor(scope: str) -> str | None:
            assert scope == "sandbox.min_level"
            return floor

        # Keep wrap_argv's real floor resolver and isolation predicate in play.
        monkeypatch.setattr(governance_profiles, "governance_floor_ordinal", governed_floor)
        status = await service._dashboard_snapshot()
        assert status["sandbox_backend_available"] is False
        assert status["unsandboxed_exec_permitted"] is permitted
        assert await service.record_independent_backend_setup(backend) is permitted
        assert service.initial_setup_complete is permitted
        assert service._setup_marker.exists() is permitted

    async def test_no_backend_and_not_permitted_refuses_claude(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        service = self._service(tmp_path, monkeypatch, unsandboxed_permitted=False)
        assert await service.record_independent_backend_setup("claude") is False
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()

    async def test_enforced_backend_in_blocked_list_still_refused(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # codex is a runtime-enforced harness. With the credential mask not
        # applying at the effective tier it lands in sandbox_blocked_backends,
        # so it is refused even though unsandboxed exec is permitted.
        service = self._service(
            tmp_path, monkeypatch, unsandboxed_permitted=True, mask_applies=False
        )
        assert await service.record_independent_backend_setup("codex") is False
        assert service.initial_setup_complete is False
        assert not service._setup_marker.exists()
