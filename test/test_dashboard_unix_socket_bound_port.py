"""What a gateway keys by its port after the bind names the port it actually bound.

Clients (``app_lifecycle_client``, ``mcp_core``) dial ``dashboard_socket_path`` of
the port they resolved, which is always the real one. Under ``--port auto`` the
REQUESTED port is ``0``, so a socket named for the request is ``dashboard-0.sock``:
no client dials it, every CLI lifecycle verb silently falls back to its file-only
path while the gateway keeps serving, and every auto-port gateway in one data home
shares (and unlinks) the same name. The prevent-sleep poll asks whether
``tailscale serve`` fronts this listener, which port ``0`` never answers.

No host listener is bound here (``no-test-side-effects``): both boots run on the
harnesses' inert TCP doubles, so the kernel's ephemeral choice is supplied through
``_resolved_bound_port`` after the bind, and through the reserved socket double's
``getsockname`` before it.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard import server as srv

_EPHEMERAL = 43127


def _kernel_assigns(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, MagicMock]:
    """Answer ``--port auto`` with :data:`_EPHEMERAL`; stub the second-family bind
    and the prevent-sleep poll, returning both doubles."""

    def _resolved(_runner: Any, port: int) -> int:
        return port or _EPHEMERAL

    monkeypatch.setattr(srv, "_resolved_bound_port", _resolved)
    second = AsyncMock(return_value=None)
    monkeypatch.setattr(srv, "_start_secondary_loopback_site", second)
    poll = MagicMock()
    monkeypatch.setattr(srv, "_arm_prevent_sleep_poll", poll)
    return second, poll


def _ports_named(unix_site: Any, second: AsyncMock, poll: MagicMock) -> list[int]:
    assert isinstance(unix_site, AsyncMock)
    assert unix_site.await_count == 1
    assert poll.call_count == 1
    return [unix_site.await_args.args[1], second.await_args.args[1], poll.call_args.args[1]]


@pytest.mark.asyncio
async def test_an_auto_port_dashboard_names_its_socket_for_the_bound_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    second, poll = _kernel_assigns(monkeypatch)
    async with harness._dashboard(tmp_path, monkeypatch):
        assert _ports_named(srv._start_unix_site, second, poll) == [_EPHEMERAL] * 3


@pytest.mark.asyncio
async def test_an_auto_port_dashboard_gives_instances_the_reserved_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tunnel manager's parent port is every pane token's frame-ancestor claim."""
    import test_dashboard_server_startup_coverage as harness

    hooks = MagicMock()
    monkeypatch.setattr(srv, "_register_instances_hooks", hooks)
    async with harness._dashboard(tmp_path, monkeypatch):
        reserved = srv._reserve_dashboard_port.return_value.getsockname()[1]
        assert reserved != 0
        assert hooks.call_args.args[2] == reserved


@pytest.mark.asyncio
async def test_an_auto_port_headless_gateway_names_its_socket_for_the_bound_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_coverage as api_harness
    import test_dashboard_server_startup_coverage as harness

    unix_site = AsyncMock(return_value=None)
    monkeypatch.setattr(srv, "_start_unix_site", unix_site)
    second, poll = _kernel_assigns(monkeypatch)
    runner, _state = await api_harness._start_api(tmp_path, monkeypatch)
    try:
        assert _ports_named(unix_site, second, poll) == [_EPHEMERAL] * 3
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_an_unknown_bound_port_stays_tcp_only_and_leaves_a_sibling_socket(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port ``0`` names no listener this process holds, so nothing is bound or unlinked."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.server.dashboard_socket_path",
        lambda port: short_sock_dir / f"dashboard-{port}.sock",
    )
    sibling_path = short_sock_dir / "dashboard-0.sock"
    sibling = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sibling.bind(str(sibling_path))
        sibling.listen(1)
        before = sibling_path.stat().st_ino
        runner = web.AppRunner(web.Application())
        await runner.setup()
        try:
            assert await srv._start_unix_site(runner, 0) is None
            assert not runner.addresses
        finally:
            await runner.cleanup()
        assert sibling_path.stat().st_ino == before
    finally:
        sibling.close()
