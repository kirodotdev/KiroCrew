"""Enterprise boot exits before the bind when the kiro login or the policy fails.

Design: docs/request-for-change/rfc-crew-enterprise-edition.md, "Enterprise gate":
"Start | Login and fetch rules. If either fails, exit".
"""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import enterprise_boot
from kiro_crew.dashboard import enterprise_gate_middleware as gate_mw
from kiro_crew.kiro_prerequisite import PrerequisiteStatus, ProcessResult
from kiro_crew.platform.context import PlatformCompositionError


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture
def tcp_listens(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every TCP socket that starts listening while the test runs."""
    seen: list[Any] = []
    real_listen = socket.socket.listen

    def listen(self: socket.socket, *args: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            seen.append(self.getsockname())
        real_listen(self, *args)

    monkeypatch.setattr(socket.socket, "listen", listen)
    return seen


class FakeService:
    """A ``KiroPrerequisiteService`` whose boot probe answers what the test sets."""

    instances: list[FakeService] = []
    status = PrerequisiteStatus(platform="linux")

    def __init__(self, **_kwargs: Any) -> None:
        self.boot_probes = 0
        FakeService.instances.append(self)

    @property
    def authenticated(self) -> bool:
        return bool(self.boot_probes) and FakeService.status.authenticated

    async def boot_login_status(self) -> PrerequisiteStatus:
        self.boot_probes += 1
        return FakeService.status


@pytest.fixture
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeService]:
    FakeService.instances = []
    FakeService.status = PrerequisiteStatus(platform="linux", installed=True)
    monkeypatch.setattr("kiro_crew.kiro_prerequisite.KiroPrerequisiteService", FakeService)
    return FakeService


def _set(fake: type[FakeService], **fields: Any) -> None:
    fake.status = replace(fake.status, **fields)


# --------------------------------------------------------------------------- #
# The login check


@pytest.mark.asyncio
async def test_no_login_exits_non_zero_naming_the_sign_in(
    enterprise: None, fake_service: type[FakeService], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        await enterprise_boot.require_kiro_login()
    assert exc.value.code == enterprise_boot.EXIT_CODE != 0
    err = capsys.readouterr().err
    assert "no Kiro sign-in was found" in err
    assert "`kiro-cli login`" in err
    assert fake_service.instances[0].boot_probes == 1


@pytest.mark.asyncio
async def test_a_missing_cli_names_the_install(
    enterprise: None, fake_service: type[FakeService], capsys: pytest.CaptureFixture[str]
) -> None:
    _set(fake_service, installed=False)
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "kiro-cli was not found" in err
    assert "https://kiro.dev/cli/" in err


@pytest.mark.asyncio
async def test_a_sandbox_refusal_names_the_sandbox_error(
    enterprise: None, fake_service: type[FakeService], capsys: pytest.CaptureFixture[str]
) -> None:
    _set(fake_service, sandbox_unavailable=True, sandbox_detail="unshare failed")
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert "could not be checked (unshare failed)" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_version_probe_timeout_is_not_reported_as_signed_out(
    enterprise: None, fake_service: type[FakeService], capsys: pytest.CaptureFixture[str]
) -> None:
    _set(fake_service, probe_timed_out=True)
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "did not answer in time" in err
    assert "no Kiro sign-in was found" not in err


@pytest.mark.asyncio
async def test_a_probe_error_is_not_reported_as_signed_out(
    enterprise: None, fake_service: type[FakeService], capsys: pytest.CaptureFixture[str]
) -> None:
    _set(fake_service, probe_error="kiro-cli whoami timed out after 30s")
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "could not be checked (kiro-cli whoami timed out after 30s)" in err
    assert "no Kiro sign-in was found" not in err


def _real_service(tmp_path: Path, whoami: ProcessResult) -> tuple[Any, list[list[str]]]:
    from kiro_prerequisite_helpers import _make_executable, _no_audit

    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    executable = tmp_path / ".local" / "bin" / "kiro-cli"
    _make_executable(executable)
    calls: list[list[str]] = []

    async def run(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
        calls.append(args)
        if args == ["--version"]:
            return ProcessResult(ok=True)
        if args == ["whoami"]:
            return whoami
        return ProcessResult(ok=False)

    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": f"{executable.parent}:/usr/bin:/bin"},
        home=tmp_path,
        process_runner=run,
        audit_writer=_no_audit,
    )
    return service, calls


@pytest.mark.asyncio
async def test_the_boot_probe_runs_only_version_and_whoami(tmp_path: Path) -> None:
    # The spec and acp checks wait for the post-bind probe, which still runs
    # them: the boot probe leaves the probe not done.
    service, calls = _real_service(tmp_path, ProcessResult(ok=True))
    status = await service.boot_login_status()
    assert status.authenticated is True and status.ready is False
    assert calls == [["--version"], ["whoami"]]
    assert service.authenticated
    await service.snapshot()
    assert ["acp", "--help"] in calls[2:]


@pytest.mark.asyncio
async def test_a_whoami_timeout_carries_its_failure(tmp_path: Path) -> None:
    service, _calls = _real_service(tmp_path, ProcessResult(ok=False, timed_out=True))
    status = await service.boot_login_status()
    assert status.authenticated is False
    assert "whoami timed out" in status.probe_error


@pytest.mark.asyncio
async def test_a_probe_that_raises_exits(
    enterprise: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.KiroPrerequisiteService",
        MagicMock(side_effect=OSError("boom")),
    )
    with pytest.raises(SystemExit) as exc:
        await enterprise_boot.require_kiro_login()
    assert exc.value.code == enterprise_boot.EXIT_CODE
    assert "could not be checked (OSError)" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_login_returns_the_verified_service(
    enterprise: None, fake_service: type[FakeService]
) -> None:
    _set(fake_service, authenticated=True)
    service = await enterprise_boot.require_kiro_login()
    assert service is fake_service.instances[0]
    assert service.authenticated


@pytest.mark.asyncio
async def test_oss_checks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.KiroPrerequisiteService",
        MagicMock(side_effect=AssertionError("oss must not probe at boot")),
    )
    assert await enterprise_boot.require_kiro_login() is None


# --------------------------------------------------------------------------- #
# The gate starts open on the verified login, and refuses a caller that skipped it


def test_gate_starts_open_after_the_boot_check(
    enterprise: None, fake_service: type[FakeService]
) -> None:
    _set(fake_service, authenticated=True)
    service = FakeService()
    service.boot_probes = 1
    app = web.Application()
    gate_mw.install_enterprise_gate(app, service)  # type: ignore[arg-type]
    assert app[gate_mw.GATE_APP_KEY].is_open
    assert not gate_mw.login_screen_required(app)


def test_gate_refuses_to_install_without_a_login(enterprise: None) -> None:
    app = web.Application()
    with pytest.raises(RuntimeError, match="boot login check"):
        gate_mw.install_enterprise_gate(app, SimpleNamespace(authenticated=False))  # type: ignore[arg-type]
    assert gate_mw.GATE_APP_KEY not in app


# --------------------------------------------------------------------------- #
# The gateway: the check runs before any service and the port is never opened


def _bare_orchestrator(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    from kiro_crew import session
    from kiro_crew.metrics import sessions as session_metrics
    from kiro_crew.slack import gateway
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._background_tasks = set()
    orch._test_mode = True
    orch._no_dashboard = False
    for method in (
        "_init_services",
        "_start_embeddings",
        "_init_mcp_gateway",
        "_init_cron",
        "_init_dashboard",
        "_init_api_server",
    ):
        monkeypatch.setattr(orch, method, AsyncMock())
    monkeypatch.setattr(gateway.crash_guard, "install_loop_handler", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "raise_nofile_soft_limit", MagicMock())
    monkeypatch.setattr(gateway.platform_compat, "probe_file_persistence", lambda home: None)
    monkeypatch.setattr(gateway, "data_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "warm_backend", MagicMock())
    monkeypatch.setattr(session, "cleanup_orphaned_sessions", MagicMock())
    monkeypatch.setattr(session_metrics, "backfill_crashed_sessions", lambda cutoff: 0)
    return orch


@pytest.mark.asyncio
async def test_gateway_without_a_login_exits_before_any_service_or_bind(
    enterprise: None,
    fake_service: type[FakeService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    tcp_listens: list[Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    orch = _bare_orchestrator(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        await orch.run()
    assert exc.value.code == enterprise_boot.EXIT_CODE
    assert "no Kiro sign-in was found" in capsys.readouterr().err
    for method in ("_init_services", "_init_mcp_gateway", "_init_dashboard", "_init_api_server"):
        getattr(orch, method).assert_not_awaited()
    assert tcp_listens == []


@pytest.mark.asyncio
async def test_oss_gateway_boots_on_without_a_login_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    class ReachedServices(Exception):
        pass

    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.KiroPrerequisiteService",
        MagicMock(side_effect=AssertionError("oss must not probe at boot")),
    )
    orch = _bare_orchestrator(monkeypatch, tmp_path)
    monkeypatch.setattr(orch, "_init_services", AsyncMock(side_effect=ReachedServices))
    from kiro_crew.slack import gateway

    monkeypatch.setattr(gateway.cautious_boot, "initialize", AsyncMock())
    with pytest.raises(ReachedServices):
        await orch.run()
    assert orch._boot_kiro_service is None


@pytest.mark.asyncio
async def test_api_server_without_a_login_exits_before_the_bind(
    enterprise: None,
    fake_service: type[FakeService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    tcp_listens: list[Any],
) -> None:
    # A caller that hands in no verified service gets the same check, still
    # before the TCP site starts.
    import kiro_crew.config.loader as _loader
    import kiro_crew.dashboard.server as _srv
    import kiro_crew.dashboard.state as _st

    monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_srv, "data_home", lambda: tmp_path)
    monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)
    site_start = AsyncMock()
    monkeypatch.setattr(web.TCPSite, "start", site_start)

    from kiro_crew.dashboard.server import start_api_server

    with pytest.raises(SystemExit) as exc:
        await start_api_server(sessions=MagicMock(), crons=MagicMock(), lessons=MagicMock(), port=0)
    assert exc.value.code == enterprise_boot.EXIT_CODE
    site_start.assert_not_awaited()
    assert tcp_listens == []


# --------------------------------------------------------------------------- #
# The governance policy


def test_failed_policy_exits_with_the_fix(
    enterprise: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        enterprise_boot.refuse_policy_boot(PlatformCompositionError("central policy unreachable"))
    assert exc.value.code == enterprise_boot.EXIT_CODE
    err = capsys.readouterr().err
    assert "governance rules" in err and "central policy unreachable" in err
    assert "start Kiro Crew again" in err


def test_oss_policy_failure_is_left_to_the_caller() -> None:
    # Returns, so the caller re-raises exactly as before this change.
    enterprise_boot.refuse_policy_boot(PlatformCompositionError("x"))


def test_enterprise_non_composition_error_is_left_to_the_caller(enterprise: None) -> None:
    enterprise_boot.refuse_policy_boot(ValueError("x"))


@pytest.mark.asyncio
async def test_run_gateway_with_a_failed_policy_exits_before_the_orchestrator(
    enterprise: None,
    monkeypatch: pytest.MonkeyPatch,
    tcp_listens: list[Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # run_gateway runs on the facade's globals, so the patch surface is the facade.
    from kiro_crew.slack import gateway

    def failed_policy(_cfg: Any) -> None:
        raise PlatformCompositionError("central policy unreachable")

    monkeypatch.setattr(gateway, "boot_platform", failed_policy)
    monkeypatch.setattr(gateway, "set_publish_disabled", MagicMock())
    monkeypatch.setattr(
        gateway, "GatewayOrchestrator", MagicMock(side_effect=AssertionError("must not construct"))
    )
    with pytest.raises(SystemExit) as exc:
        await gateway.run_gateway(MagicMock(), test_mode=True)
    assert exc.value.code == enterprise_boot.EXIT_CODE
    assert "central policy unreachable" in capsys.readouterr().err
    assert tcp_listens == []


def test_cli_with_a_failed_policy_exits_before_the_gateway(
    enterprise: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    tcp_listens: list[Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kiro_crew import cli, cli_server

    def failed_policy(_cfg: Any) -> None:
        raise PlatformCompositionError("central policy unreachable")

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "boot_platform", failed_policy)
    monkeypatch.setattr(cli, "maybe_reexec", lambda _argv: None)
    # Process-wide side effects of a real gateway start that this test skips.
    monkeypatch.setattr(
        cli.platform_compat, "limit_malloc_arenas", cli.platform_compat.MallocArenaCap
    )
    monkeypatch.setattr(cli, "_setup_cli_logging", lambda *_args: None)
    monkeypatch.setattr(
        cli_server, "_gateway", MagicMock(side_effect=AssertionError("must not start"))
    )
    monkeypatch.setattr(cli.sys, "argv", ["kirocrew", "gateway", "--test-mode", "--no-open"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == enterprise_boot.EXIT_CODE
    assert "central policy unreachable" in capsys.readouterr().err
    assert tcp_listens == []


@pytest.mark.asyncio
async def test_a_clear_signed_out_whoami_reads_as_no_sign_in(tmp_path: Path) -> None:
    from kiro_crew.kiro_prerequisite import _WHOAMI_SIGNED_OUT_MARKER

    signed_out = ProcessResult(ok=False, returncode=1, output=_WHOAMI_SIGNED_OUT_MARKER)
    service, _calls = _real_service(tmp_path, signed_out)
    status = await service.boot_login_status()
    assert status.authenticated is False and status.probe_error == ""


@pytest.mark.asyncio
async def test_a_cli_that_fails_to_start_names_the_reinstall(
    enterprise: None, fake_service: type[FakeService], capsys: pytest.CaptureFixture[str]
) -> None:
    _set(fake_service, installed=False, probe_error="segfault")
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "kiro-cli did not start (segfault)" in err and "https://kiro.dev/cli/" in err
