"""The gatewayd ping probe reports a missing socket as a probe result, not a fault.

On every cold start no daemon is listening yet, so the connect raises
``FileNotFoundError`` and the manager correctly goes on to spawn one. Logging
that at WARNING made every healthy start look broken. A socket that exists but
refuses the connect is the case worth a WARNING, and still gets one.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.mcp_gateway import manager as mgr


def _manager(tmp_path) -> mgr.GatewayManager:
    return mgr.GatewayManager(mgr.GatewaySpec(socket_path=tmp_path / "gw.sock"))


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == mgr.logger.name and r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_absent_socket_is_not_logged_as_a_warning(tmp_path, monkeypatch, caplog):
    async def _connect(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(mgr.transport, "connect", _connect)
    caplog.set_level(logging.DEBUG, logger=mgr.logger.name)

    assert await _manager(tmp_path)._ping_raw() is None
    assert _warnings(caplog) == []
    assert any(
        r.levelno == logging.DEBUG and "no daemon socket" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_refused_connect_still_warns(tmp_path, monkeypatch, caplog):
    async def _connect(*_a, **_k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(mgr.transport, "connect", _connect)
    caplog.set_level(logging.DEBUG, logger=mgr.logger.name)

    assert await _manager(tmp_path)._ping_raw() is None
    assert [r.getMessage() for r in _warnings(caplog)] == [
        "mcp-gateway ping connect failed: [Errno 111] Connection refused"
    ]
