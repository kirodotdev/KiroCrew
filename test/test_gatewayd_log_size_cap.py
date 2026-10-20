"""``mcp-gatewayd.stdout`` is bounded: past a size cap a spawn moves it to ``.1``.

Every gatewayd spawn appends to the same file, and nothing else rotates it, so
without a cap it grows for as long as the install runs. The cap is checked at
spawn time through ``jsonl_util.rotate_jsonl_at``; one previous copy is kept
for diagnosing a failed start.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway import manager as manager_mod
from kiro_crew.mcp_gateway.manager import GatewayManager, GatewaySpec


class _SpawnCaptured(RuntimeError):
    """Raised from the fake spawn once it has written to the daemon's stdout."""


def _spawn_once_writing(monkeypatch, tmp_path: Path, line: bytes) -> Path:
    """Run ``_spawn_once`` with a fake daemon that writes *line* to its stdout."""
    log_path = tmp_path / "logs" / "mcp-gatewayd.stdout"

    async def _fake_spawn(*_argv, stdout=None, **_kwargs):
        stdout.write(line)
        raise _SpawnCaptured

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    manager = GatewayManager(GatewaySpec(socket_path=tmp_path / "gateway.sock"))
    monkeypatch.setattr(manager, "_gatewayd_log_path", lambda: log_path)
    monkeypatch.setattr(manager, "_credential_watch_paths", lambda: [])
    with pytest.raises(_SpawnCaptured):
        asyncio.run(manager._spawn_once())
    return log_path


def test_a_spawn_past_the_cap_moves_the_log_to_dot_one(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(manager_mod, "_GATEWAYD_LOG_MAX_BYTES", 8)
    log_path = tmp_path / "logs" / "mcp-gatewayd.stdout"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"previous run output\n")

    _spawn_once_writing(monkeypatch, tmp_path, b"this run\n")

    assert log_path.read_bytes() == b"this run\n"
    assert (tmp_path / "logs" / "mcp-gatewayd.stdout.1").read_bytes() == b"previous run output\n"


def test_a_spawn_under_the_cap_keeps_appending(monkeypatch, tmp_path) -> None:
    log_path = tmp_path / "logs" / "mcp-gatewayd.stdout"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b"previous run output\n")

    _spawn_once_writing(monkeypatch, tmp_path, b"this run\n")

    assert log_path.read_bytes() == b"previous run output\nthis run\n"
    assert not (tmp_path / "logs" / "mcp-gatewayd.stdout.1").exists()
