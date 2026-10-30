"""A real gateway sends a slot fold's value over the socket for ``work`` only.

The browser caches one slot fold by fold, ``work``; for every other slot fold it drops
the value and re-reads the board. So the WS exporter sends those other folds as
``{slot, fold, revision}`` with no value, and keeps the whole value on the ``work``
frame. A unit test of ``_push_fold`` pins the rule against a stub hub; this pins it
through everything in between: the real gateway booted on a temp home, the emitter's
own append job writing a real crew log on disk, the eager fold worker folding it, the
bus, the exporter, and an owner socket reading the frames off the wire.

``work/recorded`` is the entry used because it moves both ``work`` and
``workstreams`` -- the largest of the non-work values -- in one append.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from integration.conftest import GatewayBoot

from kiro_crew.crew_log import eager, emit

_SLOT = "dashboard:fold-frame-e2e"
_SESSION = "s-fold-frame-e2e"
_DEADLINE_SECS = 20.0


def _record_work_item() -> None:
    """Open a unit on the slot and record one work item, through the real emitter."""
    emit.on_session_opened(
        _SESSION, agent="kirocrew", slot=_SLOT, model="opus", cwd="/w", owner="owner"
    )
    assert emit.flush()
    emit._write(
        _SESSION,
        "work/recorded",
        {
            "slot": _SLOT,
            "actor": "conductor",
            "by": _SLOT,
            "action": "create",
            "item_id": "it-e2e",
            "title": "frame shape",
        },
        src="gateway",
    )
    assert emit.flush()
    assert eager.drain(timeout=10.0)


@pytest.mark.asyncio
async def test_only_the_work_frame_carries_a_value(gateway_boot: GatewayBoot, monkeypatch):
    # The suite pins the crew log off for the process; this test is about it being on.
    # Set before the boot, so the boot installs the exporter's bus subscriptions.
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    emit.reset_caches()
    async with gateway_boot() as gw, aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{gw.base_url}/api/ws", headers={"Cookie": gw._cookie}
        ) as ws:
            first = await ws.receive_json(timeout=15)
            assert first.get("type") == "slots", f"owner WS did not register: {first!r}"

            await asyncio.to_thread(_record_work_item)

            frames: dict[str, dict] = {}
            deadline = time.monotonic() + _DEADLINE_SECS
            while not {"work", "workstreams"} <= frames.keys():
                left = deadline - time.monotonic()
                assert left > 0, f"no work and workstreams frames; saw {sorted(frames)}"
                message = await ws.receive_json(timeout=left)
                data = message.get("data") or {}
                if message.get("type") == "slot_projection" and data.get("slot") == _SLOT:
                    if "fold" in data:
                        frames[data["fold"]] = data

    work = frames["work"]
    assert work["revision"] > 0
    assert [item["item_id"] for item in work["value"]["items"]] == ["it-e2e"]

    workstreams = frames["workstreams"]
    assert workstreams["revision"] > 0
    assert set(workstreams) == {"slot", "fold", "revision"}, workstreams

    # Every other slot fold this append moved arrives the same way.
    for fold, data in frames.items():
        if fold != "work":
            assert "value" not in data, f"{fold} frame still carries its value"
