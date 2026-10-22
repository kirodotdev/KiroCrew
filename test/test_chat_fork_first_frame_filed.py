"""A fork child's first ``slots`` frame already shows it in its folder, titled.

``get_or_create_slot`` pushes the slot list when it registers a slot. On an idle
coalescer that push is sent at once, so anything the fork sets on the child
after the mint reaches clients one frame later. The dashboard then drew the
child at the top level as "New Session…" and moved it into the parent's folder
about 200 ms later, when the trailing flush carried the folder and fork title.

Both fork routes go through ``chat_fork.fork_slot``: the person's
``POST /api/chat/slots/{slot}/fork`` and the agent ``session_fork`` verb, whose
``stamp`` sets a caller-chosen title and folder. The tests record every real
``slots`` broadcast and require that no frame lists the child without its
final folder, title and pin. Asserting only the end state would pass with the
flash still there.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_title import FORK_TITLE_MARKER
from kiro_crew.dashboard.chat_utils import slot_history_key

PARENT_FOLDER = "fold0000par1"
AGENT_FOLDER = "fold0000agt1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    patch = _floor_monkeypatch
    patch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    patch.setattr(sc, "session_control_enabled", lambda: True)
    patch.setattr(chat_fork, "sel", lambda: MagicMock())
    patch.setattr(sc, "sel", lambda: MagicMock())
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _seeded_state(tmp_path):
    state = _make_state(tmp_path)
    state._folders.append({"id": PARENT_FOLDER, "name": "Parent", "parent_id": ""})
    state._folders.append({"id": AGENT_FOLDER, "name": "Agent", "parent_id": ""})
    parent = state.get_or_create_slot("parent")
    parent.append("user", "hello", "msg msg-u")
    parent.append("assistant", "hi", "msg msg-a")
    parent.drain()
    _save_slot_to_history(state, parent, closed=False)
    parent._resumed_count = len(parent.messages)
    parent._disk_window_len = len(parent.messages)
    parent._dirty = False
    parent.folder_id = PARENT_FOLDER
    parent.title = "Design notes"
    parent._titled = True
    parent.pinned = True
    return state, parent


def _record_child_frames(state) -> list[list[dict]]:
    """Capture each real ``slots`` broadcast, then open the coalescing window.

    ``_broadcast`` is replaced rather than ``push_slots_update`` so the
    suspend and coalesce logic stays under test. The window is reset because
    seeding the parent pushed too: without the reset the mint's push would be
    absorbed into a trailing flush that runs after the fork has finished,
    which hides the early frame this suite exists to catch.
    """
    seen: list[list[dict]] = []

    def capture(payload):
        if payload.get("_type") == "slots":
            seen.append(json.loads(payload["slots"]))

    state._broadcast = capture  # type: ignore[method-assign]
    state._slots_broadcast_last = float("-inf")
    return seen


def _child_rows(frames: list[list[dict]], child_key: str) -> list[dict]:
    return [row for frame in frames for row in frame if row["key"] == child_key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "word"),
    [({}, "Fork of"), ({"at_message_index": 0, "direction": "tail"}, "Tail of")],
)
async def test_the_dashboard_fork_is_first_announced_filed_and_titled(
    tmp_path, monkeypatch, payload, word
) -> None:
    # The tail fork is config-gated; without the flag the route forks the head.
    tail_cfg = MagicMock()
    tail_cfg.dashboard.tail_fork_enabled = True
    monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: tail_cfg)
    state, _parent = _seeded_state(tmp_path)
    frames = _record_child_frames(state)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json=payload)
        assert resp.status == 200
        child_key = (await resp.json())["key"]

    rows = _child_rows(frames, child_key)
    assert rows, "the fork never announced its child"
    for row in rows:
        assert row["folder_id"] == PARENT_FOLDER, (
            "a slots frame announced the fork child unfiled; the sidebar draws "
            "that as the child flashing at the top level"
        )
        assert row["title"] == f"{FORK_TITLE_MARKER}{word} Design notes"
        assert row["pinned"] is True


def test_the_agent_fork_is_first_announced_in_the_folder_it_asked_for(tmp_path) -> None:
    state, parent = _seeded_state(tmp_path)
    frames = _record_child_frames(state)

    result = asyncio.run(
        sc.fork_session(
            state,
            caller_session_key=slot_history_key(parent),
            title="cluster 3",
            folder_id=AGENT_FOLDER,
        )
    )

    rows = _child_rows(frames, result["target"])
    assert rows, "the fork never announced its child"
    for row in rows:
        assert row["folder_id"] == AGENT_FOLDER
        assert row["title"] == "cluster 3"


@pytest.mark.asyncio
async def test_a_trailing_timer_that_expires_during_the_bind_waits_for_the_block(
    tmp_path, monkeypatch
) -> None:
    """A slots timer armed before the fork can expire while the bind runs.

    The bind runs on a worker thread, so the loop is free to fire that timer.
    It must not publish the half-built child; its push moves to the block's exit.
    """
    state, _parent = _seeded_state(tmp_path)
    frames = _record_child_frames(state)
    loop = asyncio.get_running_loop()
    fired = threading.Event()
    real_bind = chat_fork._bind_fork_execution

    def fire_trailing_timer() -> None:
        try:
            state._trailing_slots_flush()
        finally:
            fired.set()

    def bind_while_timer_fires(source, child_key, inherited):
        loop.call_soon_threadsafe(fire_trailing_timer)
        assert fired.wait(5), "the trailing timer never ran during the bind"
        real_bind(source, child_key, inherited)

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", bind_while_timer_fires)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json={})
        assert resp.status == 200
        child_key = (await resp.json())["key"]

    assert fired.is_set(), "the fork never reached its bind"
    rows = _child_rows(frames, child_key)
    assert rows, "the fork never announced its child"
    for row in rows:
        assert (
            row["folder_id"] == PARENT_FOLDER
        ), "the trailing timer published the fork child before it was filed"
        assert row["title"] == f"{FORK_TITLE_MARKER}Fork of Design notes"
