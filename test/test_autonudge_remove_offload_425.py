"""Regression: async remove() must offload persistence, not fsync on the loop (#425).

``remove()`` called ``remove_sync(persist=True)`` -> ``_save()`` -> ``_write_state``
which does a blocking ``os.fsync`` directly on the event loop. It must instead
snapshot under the lock and offload the write to an executor (as update() does).
"""

from __future__ import annotations

import pytest

from kiro_crew.autonudge import AutoNudgeService


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path):
    return AutoNudgeService(base_dir=tmp_path)


@pytest.mark.asyncio
async def test_remove_offloads_persist_without_blocking_save(svc, monkeypatch):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)

    save_called = {"v": False}
    monkeypatch.setattr(svc, "_save", lambda: save_called.__setitem__("v", True))

    writes: list[dict] = []
    orig_write = svc._write_state
    monkeypatch.setattr(
        svc, "_write_state", lambda payload: (writes.append(payload), orig_write(payload))[1]
    )

    await svc.remove(loop.id)

    assert loop.id not in svc._loops
    assert save_called["v"] is False, "blocking _save() must not run on the event loop"
    assert writes, "removal must still be persisted (via the offloaded write)"


@pytest.mark.asyncio
async def test_remove_missing_loop_is_noop(svc, monkeypatch):
    await svc.start()
    writes: list[dict] = []
    monkeypatch.setattr(svc, "_write_state", lambda payload: writes.append(payload))
    await svc.remove("does-not-exist")
    assert writes == []
