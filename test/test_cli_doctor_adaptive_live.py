"""``kirocrew doctor`` prints the RUNNING gateway's adaptive-controller state.

The controller lives only in the gateway process, so doctor reads its snapshot
from ``GET /api/spawn/adaptive`` with the local credential (the way the other
CLI clients of the local gateway authenticate) and renders it with the
``resource_status`` renderer: the mode, both caps against their ceilings, the
last pressure cut, and the recent cap changes, newest last. When nothing can be
read it says why in one line and records no doctor issue.

The end-to-end tests drive a real controller through a real cut and serve it
through the real route behind the real auth middleware, on loopback.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from overload_fakes import Clock

from kiro_crew import loopback_http, port_resolution
from kiro_crew.adaptive import controller as ctl_mod
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import spawn_resume
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.doctor_checks import workload

_SECRET = "doctor-adaptive-test-secret"


class _Manager:
    """The ``ExecActuator`` surface the controller drives, with no runs."""

    def __init__(self, user_max: int) -> None:
        self._user = user_max
        self.running_count = 0
        self._queue: list[dict[str, Any]] = []
        self._agents: dict[str, Any] = {}

    @property
    def user_max_concurrent(self) -> int:
        return self._user

    def set_effective_cap(self, cap: Optional[int]) -> int:
        return self._user if cap is None else min(self._user, cap)


def _controller() -> tuple[AdaptiveController, Clock]:
    cfg = KiroCrewConfig()
    cfg.agent.adaptive_slow_start = False
    clock = Clock()
    host = HostSample(free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000)
    ctl = AdaptiveController(
        _Manager(9),  # type: ignore[arg-type]
        cfg=cfg,
        host_probe=lambda: host,
        clock=clock,
        sleep=AsyncMock(),
    )
    return ctl, clock


async def _cut(ctl: AdaptiveController, clock: Clock) -> None:
    """A real pressure cut: starts timing out on two MCP servers take 9 to 5."""
    await ctl.tick()
    clock.advance(31.0)
    for key in ("srv-a", "srv-b"):
        ctl.record_start(45_000.0, ok=False, attributable_timeout=True, key=key)
    await ctl.tick()
    clock.advance(10.0)
    await ctl.tick()
    assert ctl.state()["effective_exec_cap"] == 5


def _gateway_app(secret: str = _SECRET) -> web.Application:
    """The route doctor reads, behind the middleware the gateway runs."""
    app = web.Application(
        middlewares=[
            token_auth_middleware(
                internal_paths=_STRICT_INTERNAL_API_PATHS,
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret=secret,
            )
        ]
    )
    app["state"] = SimpleNamespace(subagents=None)
    spawn_resume.setup_spawn_resume_routes(app)
    return app


def _point_doctor_at(monkeypatch: pytest.MonkeyPatch, port: int, secret: str = _SECRET) -> None:
    """Resolve doctor's client port to *port* and its credential to *secret*.

    The credential is handed out only for the loopback listener doctor dials,
    so a read that asked for any other host or port would go unauthenticated.
    """
    monkeypatch.setattr(port_resolution, "resolve_client_port", lambda cli_port: port)

    def _secret(asked_port: int, dial_host: str | None = None) -> str:
        return secret if (asked_port, dial_host) == (port, "127.0.0.1") else ""

    monkeypatch.setattr(loader, "read_local_secret", _secret)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _run_doctor_against(
    app: web.Application, monkeypatch: pytest.MonkeyPatch, secret: str = _SECRET
) -> int:
    """Serve *app* on loopback, run doctor's live read against it, return the port.

    Doctor's read is blocking, so it runs on a worker thread while this loop
    serves the request.
    """
    async with TestServer(app, host="127.0.0.1") as server:
        port = int(server.port)
        _point_doctor_at(monkeypatch, port, secret)
        await asyncio.to_thread(workload._doctor_adaptive_live)
    return port


@pytest.mark.asyncio
async def test_doctor_prints_the_running_controllers_caps_cut_and_history(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ctl, clock = _controller()
    await _cut(ctl, clock)
    monkeypatch.setattr(ctl_mod, "_current", ctl)
    port = await _run_doctor_against(_gateway_app(), monkeypatch)
    out = capsys.readouterr().out
    assert f"adaptive controller (running gateway on port {port}):" in out
    assert "Mode: aimd   Execution cap: 5/9" in out
    assert "Last pressure cut: decrease 10s ago (corroborated pressure: " in out
    assert "Recent cap changes (newest last):" in out
    assert "decrease -> exec 5 gate" in out
    # Indented under doctor's own heading, which replaces the renderer's.
    assert "\n    Mode: aimd" in out
    assert "Adaptive concurrency (enforced beneath the user cap)" not in out


@pytest.mark.asyncio
async def test_after_recovery_the_history_shows_the_cut_then_each_restore(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """How long the cap stayed low reads off the history: the cut, then every
    step back to the ceiling, newest last."""
    ctl, clock = _controller()
    await _cut(ctl, clock)
    # Past the evidence window in one step, so the timeouts behind the cut
    # have aged out and nothing cuts again on the way back up.
    clock.advance(61.0)
    for _ in range(80):
        clock.advance(5.0)
        await ctl.tick()
    assert ctl.state()["effective_exec_cap"] == 9
    monkeypatch.setattr(ctl_mod, "_current", ctl)
    await _run_doctor_against(_gateway_app(), monkeypatch)
    out = capsys.readouterr().out
    history = out.split("Recent cap changes (newest last):", 1)[1].splitlines()
    moves = [line.split()[1:5] for line in history if " -> exec " in line]
    assert moves == [
        ["decrease", "->", "exec", "5"],
        ["increase", "->", "exec", "6"],
        ["increase", "->", "exec", "7"],
        ["increase", "->", "exec", "8"],
        ["increase", "->", "exec", "9"],
    ]
    assert "Execution cap: 9/9" in out
    # Back at the ceiling the cut is history: it stays in the list above and
    # leaves the "last pressure cut" line, the same as the resource_status tool.
    assert "Last pressure cut" not in out


def test_no_credential_sends_nothing_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(port_resolution, "resolve_client_port", lambda cli_port: 5999)
    monkeypatch.setattr(loader, "read_local_secret", lambda port, dial_host=None: "")

    def _no_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("doctor dialled the gateway without a credential")

    monkeypatch.setattr(loopback_http, "loopback_urlopen", _no_request)
    workload._doctor_adaptive_live()
    out = capsys.readouterr().out
    assert out == (
        "  adaptive controller: not read on port 5999: no local gateway credential "
        "for this port (gateway not running?)\n"
    )


@pytest.mark.asyncio
async def test_a_refused_credential_is_named_with_its_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = await _run_doctor_against(
        _gateway_app(secret="the-real-one"), monkeypatch, secret="a-stale-one"
    )
    out = capsys.readouterr().out
    assert f"adaptive controller: not read on port {port}: " in out
    assert "the gateway refused the read (HTTP 40" in out
    assert "Execution cap" not in out


def test_nothing_listening_reads_as_no_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    _point_doctor_at(monkeypatch, port)
    workload._doctor_adaptive_live()
    out = capsys.readouterr().out
    assert out == f"  adaptive controller: not read on port {port}: no gateway answered\n"


@pytest.mark.asyncio
async def test_a_gateway_without_a_controller_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ctl_mod, "_current", None)
    port = await _run_doctor_against(_gateway_app(), monkeypatch)
    out = capsys.readouterr().out
    assert out == (
        f"  adaptive controller: not read on port {port}: "
        "the running gateway has no adaptive controller\n"
    )


def test_the_live_block_sits_between_the_configured_line_and_the_ladder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = {
        "enabled": True,
        "mode": "aimd",
        "effective_exec_cap": 2,
        "exec_ceiling": 16,
        "spawn_gate_capacity": 1,
        "gate_ceiling": 8,
    }
    monkeypatch.setattr(workload, "_gateway_adaptive_state", lambda: (5476, state, ""))
    workload._doctor_overload_resilience(KiroCrewConfig())
    lines = capsys.readouterr().out.splitlines()
    configured = next(i for i, line in enumerate(lines) if "adaptive concurrency: " in line)
    assert lines[configured + 1] == "  adaptive controller (running gateway on port 5476):"
    assert lines[configured + 2].startswith("    Mode: aimd   Execution cap: 2/16")
    assert "MCP spawn gate: 1/8" in lines[configured + 2]
    assert any(line == "  recovery ladder:" for line in lines[configured + 3 :])


def test_snapshot_text_cannot_drive_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The snapshot arrives over a socket, so a control sequence in it prints inert."""
    state = {
        "enabled": True,
        "mode": "aimd",
        "effective_exec_cap": 4,
        "exec_ceiling": 8,
        "last": {"action": "hold", "reason": "waiting\x1b]0;title\x07\nforged line"},
    }
    monkeypatch.setattr(workload, "_gateway_adaptive_state", lambda: (5476, state, ""))
    workload._doctor_adaptive_live()
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "\nforged line" not in out
    assert "Last decision: hold (waiting" in out


def test_a_failing_read_is_one_line_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> tuple[int, dict[str, Any] | None, str]:
        raise RuntimeError("port lookup failed")

    monkeypatch.setattr(workload, "_gateway_adaptive_state", _boom)
    workload._doctor_adaptive_live()
    assert capsys.readouterr().out == (
        "  adaptive controller: not read (check failed: port lookup failed)\n"
    )
