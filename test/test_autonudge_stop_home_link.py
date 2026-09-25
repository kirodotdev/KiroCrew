"""A stop is still recorded when the data home is reached through a symlink."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_stop_log as stoplog
from kiro_crew.autonudge import AutoNudgeService


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _unpublish():
    yield
    _an._INSTANCE = None


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX directory symlink")
def test_stop_is_recorded_under_a_symlinked_home(tmp_path):
    real = tmp_path / "real-home"
    real.mkdir()
    home = tmp_path / "home-link"
    home.symlink_to(real, target_is_directory=True)

    async def body():
        svc = AutoNudgeService(base_dir=home)
        loop = await svc.add("chat-7", "tick", idle_secs=60)
        await svc.update(loop.id, active=False, stopped_reason="runtime_budget")
        svc.stop()
        return loop.id

    loop_id = asyncio.run(body())
    path = stoplog.stop_log_path(real)
    [rec] = [json.loads(line) for line in path.read_text().splitlines()]
    assert (rec["loop_id"], rec["reason"]) == (loop_id, "runtime_budget")
