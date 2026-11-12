"""A fork's birth save reuses the parent's stored display verdicts.

The child's rows are copies of the parent's, so with the parent's verdicts on
disk the save should not rescan them when the process cache is cold. Before the
fix it scanned every copied meta string again: 1,624 scans and 2.7 s of a 2.9 s
fork for a 25 MB parent.
"""

from __future__ import annotations

import os

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork, chat_utils_display, display_verdicts
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

# Stored verdicts are off where there is no effective uid (Windows).
pytestmark = pytest.mark.skipif(os.name == "nt", reason="stored verdicts are POSIX-only")

_ROWS = 40
_NOTE = "tool output {} that the battery has to judge"


async def _fork_and_count_rescans(tmp_path, monkeypatch) -> list[str]:
    monkeypatch.setattr(display_verdicts, "_derive_key", lambda: b"k" * 32)
    assert display_verdicts.enable(tmp_path, tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("src")
    for i in range(_ROWS):
        slot.append("user", f"question {i}", "msg msg-u")
        slot.append("assistant", f"answer {i}", "msg msg-a", meta={"note": _NOTE.format(i)})
    slot.drain()
    assert await save_slot_off_loop(state, slot, best_effort=False)
    # The save's projection is what stores the parent's verdicts.
    display_verdicts.drain_for_tests()
    # A restart, or enough other sessions' renders, empties the process cache.
    chat_utils_display._clear_display_redaction_cache()

    notes = {_NOTE.format(i) for i in range(_ROWS)}
    rescanned: list[str] = []
    real = chat_utils_display.display_battery

    def counting(text, *args, **kwargs):
        if text in notes:
            rescanned.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(chat_utils_display, "display_battery", counting)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/src/fork", json={})
        assert resp.status == 200
        assert (await resp.json())["messages"] == 2 * _ROWS
    display_verdicts.drain_for_tests()
    return rescanned


@pytest.mark.asyncio
async def test_fork_reuses_parent_verdicts_on_a_cold_cache(tmp_path, monkeypatch):
    rescanned = await _fork_and_count_rescans(tmp_path, monkeypatch)
    # The read scope scans a few strings before a cold open loads the sidecar;
    # every later copied string is judged from it.
    assert len(rescanned) <= display_verdicts.COLD_MISSES, rescanned


@pytest.mark.asyncio
async def test_fork_skips_reuse_when_the_workspace_allows_a_link_host(tmp_path, monkeypatch):
    """Stored tags carry the allowed hosts and the save judges with none, so
    nothing could match: the save scans as before and seeds no dead tags."""
    seen: list[str | None] = []
    real_save = chat_fork.save_slot_off_loop

    async def recording(state, slot, *args, **kwargs):
        seen.append(kwargs.get("verdict_source"))
        return await real_save(state, slot, *args, **kwargs)

    monkeypatch.setattr(chat_fork, "allowed_hosts_for", lambda _ws: frozenset({"example.com"}))
    monkeypatch.setattr(chat_fork, "save_slot_off_loop", recording)
    rescanned = await _fork_and_count_rescans(tmp_path, monkeypatch)
    assert None in seen and "dashboard:src" not in seen
    assert len(rescanned) == _ROWS
