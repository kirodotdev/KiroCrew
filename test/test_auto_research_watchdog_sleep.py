"""The research watchdog measures worker inactivity in awake time.

A campaign that is idle between nudges must stay RUNNING when the laptop
sleeps or the wall clock steps forward, because the nudge timer does not
run while the machine sleeps either. Awake time past the deadline fails it.
"""

from __future__ import annotations

import asyncio
import json
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import pytest
from auto_research_helpers import isolate_fixture  # noqa: F401

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.auto_research.handlers import (
    CampaignStatus,
    create_campaign,
    get_campaign,
    update_campaign_status,
)

HOUR = 3600.0


class _StandIn:
    """Stands in for a module the watchdog uses: the given names, the rest from it."""

    def __init__(self, module, **names) -> None:
        self._module = module
        self.__dict__.update(names)

    def __getattr__(self, name: str):
        return getattr(self._module, name)


async def _three_polls(isolate: Path, tmp_path: Path, monkeypatch, *, wall: float, awake: float):
    """Run three watchdog polls over a RUNNING campaign whose worker is idle.

    Before the second poll the wall clock moves by ``wall`` seconds and awake
    time by ``awake`` seconds. Only the watchdog module sees the moved clocks
    and the counted sleep, so the autonudge service's own timers are untouched.
    Returns the campaign row, whether the slot has its autonudge loop, and
    whether that loop is active.
    """
    from kiro_crew.apps.builtins.auto_research import handlers as h
    from kiro_crew.apps.builtins.auto_research.campaign import watchdog as wd
    from kiro_crew.autonudge import AutoNudgeService

    shift = {"wall": 0.0, "awake": 0.0}
    polls = 0

    async def _poll(_secs: float) -> None:
        nonlocal polls
        polls += 1
        if polls > 3:
            raise asyncio.CancelledError
        if polls == 2:
            shift["wall"], shift["awake"] = wall, awake

    monkeypatch.setattr(
        wd, "time", _StandIn(real_time, time=lambda: real_time.time() + shift["wall"])
    )
    monkeypatch.setattr(
        wd,
        "platform_compat",
        _StandIn(platform_compat, awake_now=lambda: platform_compat.awake_now() + shift["awake"]),
        raising=False,
    )
    monkeypatch.setattr(wd, "asyncio", _StandIn(asyncio, sleep=_poll))
    svc = AutoNudgeService(base_dir=tmp_path / "autonudge")
    await svc.start()
    try:
        cid = create_campaign(
            {"question": "Research a sufficiently detailed question here", "sources": ["web"]}
        )["id"]
        update_campaign_status(cid, CampaignStatus.RUNNING)
        findings = isolate / "research" / cid / "findings"
        findings.mkdir(parents=True, exist_ok=True)
        findings.joinpath("cycle_001.json").write_text(
            json.dumps({"summary": "cycle 1", "verification": None})
        )
        slot_key = f"research-{cid}"
        loop = await svc.add(slot_key=slot_key, message="run", idle_secs=120)
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "is_app_enabled", lambda _name: True)

        slot = SimpleNamespace(_trust=True, running=False)  # idle between nudges
        await wd._watchdog_loop({"state": SimpleNamespace(_slots={slot_key: slot})})
        return get_campaign(cid), svc.get_by_slot(slot_key) is not None, loop.active
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_wall_clock_jump_keeps_idle_campaign_running(
    _isolate: Path, tmp_path: Path, monkeypatch
):
    # A sleeping laptop moves the wall clock but not awake time.
    row, has_loop, loop_active = await _three_polls(
        _isolate, tmp_path, monkeypatch, wall=HOUR, awake=0.0
    )

    assert row["status"] == CampaignStatus.RUNNING
    assert row["error_message"] is None
    assert has_loop and loop_active


@pytest.mark.asyncio
async def test_awake_time_past_deadline_fails_campaign(_isolate: Path, tmp_path: Path, monkeypatch):
    # An hour of awake time passes with no worker activity.
    row, has_loop, loop_active = await _three_polls(
        _isolate, tmp_path, monkeypatch, wall=HOUR, awake=HOUR
    )

    assert row["status"] == CampaignStatus.FAILED
    assert "research stalled" in row["error_message"]
    assert not has_loop and not loop_active
