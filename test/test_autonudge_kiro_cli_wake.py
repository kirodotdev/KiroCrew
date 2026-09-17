"""Phase-4 autonudge producer: divert a supervised owner's fire to a wake.

Two properties:

* A loop bound to a ``kiro-cli:<id>`` session ENQUEUES (calls the wake hook)
  instead of injecting into a gateway slot, and the enqueue counts as a
  delivered cycle exactly as an ordinary fire does.
* Every OTHER owner is untouched — a dashboard/channel loop still runs the
  ordinary ``on_fire`` path even when the wake hook is wired.

Plus the GET /api/autonudge owner-scoping for an internal-secret caller.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(scope="session")
def svc_base_dir(tmp_path_factory: pytest.TempPathFactory):
    return tmp_path_factory.mktemp("autonudge-kiro-cli-wake")


def _loop(**over: Any) -> NudgeLoop:
    fields: dict[str, Any] = {
        "id": "lp-1",
        "slot_key": "kiro-cli:sess-1",
        "message": "check the PR",
        "idle_secs": 300,
        "max_cycles": 24,
        "cycle_count": 0,
        "next_due_ts": 9_999_999_999.0,
    }
    fields.update(over)
    return NudgeLoop(**fields)


async def _run_armed_cycle(svc: AutoNudgeService, loop_id: str) -> None:
    task = svc._timers[loop_id]  # noqa: SLF001
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_kiro_cli_owner_enqueues_instead_of_injecting(svc_base_dir) -> None:
    injected: list[NudgeLoop] = []
    enqueued: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        injected.append(loop)
        return True

    async def on_wake(loop: NudgeLoop) -> bool:
        enqueued.append(loop)
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire, on_kiro_cli_wake=on_wake)
    loop = _loop()
    svc._loops[loop.id] = loop  # noqa: SLF001

    _armed, error, status = await svc.fire_now(loop.id)
    assert (error, status) == ("", 200)
    await _run_armed_cycle(svc, loop.id)

    # The wake hook ran; the inject path did NOT.
    assert [lp.id for lp in enqueued] == ["lp-1"]
    assert injected == []
    # Enqueue counted as a delivered turn.
    assert loop.cycle_count == 1
    svc.stop()


@pytest.mark.asyncio
async def test_non_kiro_cli_owner_still_injects(svc_base_dir) -> None:
    injected: list[NudgeLoop] = []
    enqueued: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        injected.append(loop)
        return True

    async def on_wake(loop: NudgeLoop) -> bool:
        enqueued.append(loop)
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire, on_kiro_cli_wake=on_wake)
    loop = _loop(id="lp-2", slot_key="chat-1-111")
    svc._loops[loop.id] = loop  # noqa: SLF001

    await svc.fire_now(loop.id)
    await _run_armed_cycle(svc, loop.id)

    assert [lp.id for lp in injected] == ["lp-2"]
    assert enqueued == []
    svc.stop()


@pytest.mark.asyncio
async def test_supervised_owner_falls_back_to_inject_when_hook_unset(svc_base_dir) -> None:
    # A standalone gateway wires no wake hook; a supervised-key loop must still
    # deliver via the ordinary path rather than silently drop its fire.
    injected: list[NudgeLoop] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        injected.append(loop)
        return True

    svc = AutoNudgeService(base_dir=svc_base_dir, on_fire=on_fire)  # no on_kiro_cli_wake
    loop = _loop(id="lp-3", slot_key="kiro-cli:sess-9")
    svc._loops[loop.id] = loop  # noqa: SLF001

    await svc.fire_now(loop.id)
    await _run_armed_cycle(svc, loop.id)

    assert [lp.id for lp in injected] == ["lp-3"]
    svc.stop()


# -- GET /api/autonudge owner scoping -----------------------------------------


class _FakeSvc:
    def __init__(self, loops: list[NudgeLoop]) -> None:
        self._loops = loops

    def list_all(self) -> list[NudgeLoop]:
        return self._loops


@pytest.mark.asyncio
async def test_autonudge_list_scopes_to_internal_caller_owner(monkeypatch) -> None:
    mine = _loop(id="lp-mine", slot_key="kiro-cli:sess-1")
    theirs = _loop(id="lp-theirs", slot_key="kiro-cli:sess-2")
    dash = _loop(id="lp-dash", slot_key="chat-1-111")
    monkeypatch.setattr(h, "_autonudge_get", lambda: _FakeSvc([mine, theirs, dash]))

    req = make_mocked_request("GET", "/api/autonudge", headers={"X-Session-Key": "kiro-cli:sess-1"})
    req["internal_auth"] = True
    resp = await h.api_autonudge_list(req)
    import json

    body = json.loads(resp.body)
    ids = [lp["id"] for lp in body["loops"]]
    assert ids == ["lp-mine"]


@pytest.mark.asyncio
async def test_autonudge_list_unscoped_for_browser_reader(monkeypatch) -> None:
    mine = _loop(id="lp-mine", slot_key="kiro-cli:sess-1")
    dash = _loop(id="lp-dash", slot_key="chat-1-111")
    monkeypatch.setattr(h, "_autonudge_get", lambda: _FakeSvc([mine, dash]))

    # No internal_auth grant: the dashboard reader still sees every armed loop.
    req = make_mocked_request("GET", "/api/autonudge")
    resp = await h.api_autonudge_list(req)
    import json

    body = json.loads(resp.body)
    ids = sorted(lp["id"] for lp in body["loops"])
    assert ids == ["lp-dash", "lp-mine"]
