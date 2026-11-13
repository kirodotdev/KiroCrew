"""The working directory ``spawn_backend`` runs a pooled backend in.

Every stub the rewriter writes registers the gateway's own default work dir,
``workspace_root()/_default``, and the rewrite pass is the only place that creates
it. These tests pin what ``spawn_backend`` does when that directory has vanished
since: it recreates it (off the event loop, last directory only) and the spawn
goes ahead in it. A missing cwd that is not that default is left missing, because
a register frame is not trusted to choose a path the unsandboxed daemon creates,
and a missing workspace root is never rebuilt, so an unmounted volume is not
recreated on the boot disk. The subprocess seam is faked to fail with ``ENOENT``
for a missing cwd, as the OS does.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.loader_locations import workspace_root
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import spawn_backend
from kiro_crew.mcp_gateway.pool import PoolKey

_FAKE_PID = 5150


class _FakeProcess:
    """The parts of an ``asyncio`` subprocess ``spawn_backend`` reads."""

    def __init__(self) -> None:
        # nosemgrep: kirocrew.mock-pid-allocatable -- the fake process is never signalled; get_process_start_id is patched
        self.pid = _FAKE_PID
        self.returncode = None
        self.stdin = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdout = asyncio.StreamReader()
        self.stderr = None

    def kill(self) -> None:  # pragma: no cover - only reached when pipes are missing
        pass


@pytest.fixture
def fake_exec(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the spawn seam: a missing cwd raises ``ENOENT`` naming it, as ``exec`` does."""
    captured: dict[str, Any] = {}

    async def _fake_exec(program: str, *args: str, **kwargs: Any) -> _FakeProcess:
        cwd = kwargs["cwd"]
        captured["cwd"] = cwd
        if not os.path.isdir(cwd):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), cwd)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        backend_mod.platform_compat, "get_process_start_id", lambda pid: f"start-{pid}"
    )
    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)
    return captured


def _pool_key(work_dir: str) -> PoolKey:
    return PoolKey(
        server_name="example-mcp",
        command_args_hash="cah",
        effective_env_hash="eeh",
        work_dir=work_dir,
        binary_version="1.0",
        os_uid=1000,
    )


def _default_work_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point the workspace at *root* and return the default work dir the rewriter bakes."""
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(root))
    return workspace_root(create=False) / "_default"


@pytest.mark.asyncio
async def test_a_vanished_default_work_dir_is_recreated_and_the_spawn_succeeds(
    fake_exec: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    default = _default_work_dir(monkeypatch, root)
    assert not default.exists()

    created_on: list[int] = []
    real_mkdir = Path.mkdir

    def _recording_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if os.path.normcase(str(self)) == os.path.normcase(str(default)):
            created_on.append(threading.get_ident())
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _recording_mkdir)
    caplog.set_level(logging.WARNING, logger=backend_mod.logger.name)

    try:
        backend = await spawn_backend(_pool_key(str(default)), "cmd", [], {}, str(default))
    except FileNotFoundError as exc:
        pytest.fail(f"spawn failed on the vanished default work dir: {exc}")

    assert backend.pid == _FAKE_PID
    assert default.is_dir()
    assert fake_exec["cwd"] == str(default)
    # Created once, on a worker thread rather than the daemon's event loop.
    assert len(created_on) == 1
    assert created_on[0] != threading.get_ident()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("example-mcp" in m and str(default) in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_an_existing_default_work_dir_is_used_as_is(
    fake_exec: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    default = _default_work_dir(monkeypatch, root)
    default.mkdir()
    marker = default / "kept"
    marker.write_text("x", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=backend_mod.logger.name)

    await spawn_backend(_pool_key(str(default)), "cmd", [], {}, str(default))

    assert marker.read_text(encoding="utf-8") == "x"
    assert fake_exec["cwd"] == str(default)
    assert not [r for r in caplog.records if str(default) in r.getMessage()]


@pytest.mark.asyncio
async def test_a_missing_cwd_that_is_not_the_default_is_left_missing(
    fake_exec: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _default_work_dir(monkeypatch, root)
    elsewhere = root / "elsewhere"

    with pytest.raises(FileNotFoundError):
        await spawn_backend(_pool_key(str(elsewhere)), "cmd", [], {}, str(elsewhere))

    assert fake_exec["cwd"] == str(elsewhere)
    assert not elsewhere.exists()


@pytest.mark.asyncio
async def test_a_missing_workspace_root_is_not_recreated(
    fake_exec: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    volume = tmp_path / "vol"
    default = _default_work_dir(monkeypatch, volume / "ws")

    with pytest.raises(FileNotFoundError):
        await spawn_backend(_pool_key(str(default)), "cmd", [], {}, str(default))

    assert fake_exec["cwd"] == str(default)
    assert not volume.exists()
