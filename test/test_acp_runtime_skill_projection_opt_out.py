"""A runtime built with ``native_skill_projection=False`` spawns without preparing views.

The warm mint process runs from a generation tree under the protected ``run/`` dir,
whose project-scope specs the projection's reader refuses. A projected process would
then advertise only its launch agent, so the opt-out must keep the preparation from
running at all, while every other runtime keeps projecting.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.acp import skill_projection
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError

_STAND_IN = "import time; time.sleep(60)"


async def _spawn_until_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **runtime_kwargs: Any
) -> list[Path]:
    """Spawn a stand-in process, fail its handshake, and return the projection calls."""
    prepared: list[Path] = []

    def _record(work_dir: Path, **_kwargs: Any) -> None:
        prepared.append(Path(work_dir))
        return None

    rt = AcpRuntime(
        work_dir=tmp_path / "wd", agent="kirocrew", sandbox_mode="off", **runtime_kwargs
    )
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0

    async def _plan() -> SpawnPlan:
        return SpawnPlan(argv=[sys.executable, "-c", _STAND_IN, "--agent", "kirocrew"])

    async def _failing_handshake(_caps: Any) -> None:
        raise AcpRuntimeError("initialize timed out")

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", _record)
    monkeypatch.setattr(rt, "_initialize_handshake", _failing_handshake)

    with pytest.raises(AcpRuntimeError, match="initialize timed out"):
        await rt.spawn()
    return prepared


@pytest.mark.asyncio
async def test_a_runtime_with_projection_off_prepares_no_skill_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await _spawn_until_handshake(tmp_path, monkeypatch, native_skill_projection=False)
    assert prepared == []


@pytest.mark.asyncio
async def test_a_default_runtime_still_prepares_skill_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await _spawn_until_handshake(tmp_path, monkeypatch)
    assert prepared == [tmp_path / "wd"]
