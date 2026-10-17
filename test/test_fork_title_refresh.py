"""A fork child is renamed after the latest question it holds.

A fork is born titled ``↳ Fork of <parent>``, which names the parent's topic.
Before this fix the child's title also lost its "auto" provenance, so the
background refresh treated the fork name as a manual one and never touched it.
Now the fork keeps the parent's provenance and, once acknowledged, runs ONE
background title pass (``chat_title.refresh_forked_title``) over the newest
question a person typed into the copied branch.

Invariants locked here:

- The child is renamed and the parent's title and transcript are unchanged.
- Only a person-typed question names it; a fork with none keeps its fork title.
- A generation failure never fails the fork and is not retried.
- A manual rename while the pass is pending wins.
- A permanent delete in flight while the pass is pending gets no title write.
- A newer question typed into the child while the pass is pending wins over
  the stale result, within a bounded number of generations.
- One fork spends one pass: no duplicate generation.
- A parent with a manual name, or an agent fork given a title, keeps a final
  title and spends nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import TURN_WAIT_SECS, _make_app, _make_state, drain_background_tasks

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_USER,
    refresh_forked_title,
)
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.testing.wait import async_wait_until

_HUMAN = {HUMAN_TURN_META_KEY: True}


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


def _seeded_state(tmp_path, *, origin: str = _TITLE_ORIGIN_AUTO, human: bool = True):
    """A parent auto-titled for its opening topic whose latest question moved on."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("parent")
    meta = dict(_HUMAN) if human else None
    slot.append("user", "how do I rotate the log files", "msg msg-u", meta=meta)
    slot.append("assistant", "use logrotate", "msg msg-a")
    slot.append("user", "now plan the canary deploy for the API", "msg msg-u", meta=meta)
    slot.append("assistant", "here is a plan", "msg msg-a")
    slot.title = "Rotating log files"
    slot._titled = True
    slot._title_origin = origin
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return state


class _Generator:
    """Stands in for the initial-title generator and records every call."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []
        self.release: list[asyncio.Event] = []

    def hold(self) -> None:
        """Make each later call wait for its own ``release`` event."""
        self._hold = True

    async def __call__(self, _state, messages, *, session_key: str = "") -> str:
        self.calls.append([m.get("content", "") for m in messages])
        if getattr(self, "_hold", False):
            gate = asyncio.Event()
            self.release.append(gate)
            await asyncio.wait_for(gate.wait(), TURN_WAIT_SECS)
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return reply


async def _wait_for_calls(gen: _Generator, n: int) -> None:
    await async_wait_until(
        lambda: len(gen.release) >= n,
        timeout=TURN_WAIT_SECS,
        describe=lambda: f"generator reached {len(gen.release)} of {n} calls",
    )


async def _fork(client, payload=None) -> dict:
    resp = await client.post("/api/chat/slots/parent/fork", json=payload or {})
    assert resp.status == 200
    return await resp.json()


@pytest.mark.asyncio
async def test_the_child_is_renamed_from_its_latest_question(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    gen = _Generator(["Canary deploy plan"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)
    pushes: list[tuple[str, str]] = []
    real_push = state.push_slot_title
    monkeypatch.setattr(
        state,
        "push_slot_title",
        lambda key, title, **kw: (pushes.append((key, title)), real_push(key, title, **kw))[1],
    )
    parent = state._slots["parent"]
    parent_rows = [(m["role"], m["content"]) for m in parent.messages]

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        # The fork answers with its own name; the rename arrives later.
        assert body["title"] == "↳ Fork of Rotating log files"
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert child.title == "↳ Canary deploy plan"
    assert child._title_origin == _TITLE_ORIGIN_AUTO
    # Only the newest person-typed question was sent, never the opening turns.
    assert gen.calls == [["now plan the canary deploy for the API"]]
    # The sidebar heard about it.
    assert (child.key, "↳ Canary deploy plan") in pushes
    # The parent is untouched.
    assert parent.title == "Rotating log files"
    assert [(m["role"], m["content"]) for m in parent.messages] == parent_rows
    # The new name and its provenance are durable.
    del state._slots[body["key"]]
    reloaded = _rehydrate_slot_from_history(state, body["key"])
    assert reloaded is not None
    assert reloaded.title == "↳ Canary deploy plan"
    assert reloaded._title_origin == _TITLE_ORIGIN_AUTO


@pytest.mark.asyncio
async def test_a_fork_at_an_earlier_point_uses_the_question_at_that_point(
    tmp_path, monkeypatch
) -> None:
    gen = _Generator(["Log rotation"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client, {"at_message_index": 1})
        await drain_background_tasks(state)

    assert gen.calls == [["how do I rotate the log files"]]
    assert state._slots[body["key"]].title == "↳ Log rotation"


@pytest.mark.asyncio
async def test_no_person_typed_question_keeps_the_fork_title(tmp_path, monkeypatch) -> None:
    """Rows without the human marker (automation envelopes, app or session_send
    turns) never name a session, so there is nothing to generate from."""
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path, human=False)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await drain_background_tasks(state)

    assert gen.calls == []
    assert state._slots[body["key"]].title == "↳ Fork of Rotating log files"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("bg session down"), ""])
async def test_a_generation_failure_keeps_the_fork_and_is_not_retried(
    tmp_path, monkeypatch, failure
) -> None:
    gen = _Generator([failure, "Never asked for"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert len(gen.calls) == 1
    assert child.title == "↳ Fork of Rotating log files"
    assert child._title_in_flight is False


@pytest.mark.asyncio
async def test_a_manual_rename_while_pending_wins(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Canary deploy plan"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _wait_for_calls(gen, 1)
        resp = await client.patch(f"/api/chat/slots/{body['key']}/title", json={"title": "Mine"})
        assert resp.status == 200
        gen.release[0].set()
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert child.title == "Mine"
    assert child._title_origin == _TITLE_ORIGIN_USER


@pytest.mark.asyncio
async def test_a_delete_in_flight_while_pending_writes_no_title(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.chat_utils import slot_history_key

    gen = _Generator(["Canary deploy plan"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _wait_for_calls(gen, 1)
        child = state._slots[body["key"]]
        history_key = slot_history_key(child)
        before = state.conversation_log.get_metadata(history_key).get("title")
        # The permanent delete holds this window from its first await until
        # the slot is gone; the pass must not upsert the line back meanwhile.
        with state.conversation_log.delete_in_flight_window(history_key):
            gen.release[0].set()
            await drain_background_tasks(state)
            after = state.conversation_log.get_metadata(history_key).get("title")

    assert after == before
    assert "Canary" not in str(after)


@pytest.mark.asyncio
async def test_a_failed_persist_publishes_nothing(tmp_path, monkeypatch) -> None:
    """Persist before publish: when the metadata write fails (a full disk), the
    live slot keeps the title and refresh state a reload would restore."""
    gen = _Generator(["Canary deploy plan"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "push_slot_title", lambda key, title, **kw: pushes.append((key, title))
    )

    async def _not_durable(_state, _slot, *, still_current=None) -> bool:
        return False

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _wait_for_calls(gen, 1)
        child = state._slots[body["key"]]
        before = (child.title, child._title_low_signal, child._title_refresh_mark)
        monkeypatch.setattr(chat_title, "_persist_title", _not_durable)
        gen.release[0].set()
        await drain_background_tasks(state)

    assert (child.title, child._title_low_signal, child._title_refresh_mark) == before
    assert child.title == "↳ Fork of Rotating log files"
    assert all(key != child.key for key, _ in pushes)


@pytest.mark.asyncio
async def test_a_newer_child_question_while_pending_wins(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Canary deploy plan", "Database failover drill"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        child = state._slots[body["key"]]
        await _wait_for_calls(gen, 1)
        child.append("user", "actually, run the database failover drill", "msg msg-u", meta=_HUMAN)
        gen.release[0].set()
        await _wait_for_calls(gen, 2)
        # The stale result was never applied.
        assert child.title == "↳ Fork of Rotating log files"
        gen.release[1].set()
        await drain_background_tasks(state)

    assert gen.calls == [
        ["now plan the canary deploy for the API"],
        ["actually, run the database failover drill"],
    ]
    assert child.title == "↳ Database failover drill"


@pytest.mark.asyncio
async def test_generation_is_bounded_when_questions_keep_arriving(tmp_path, monkeypatch) -> None:
    gen = _Generator(["One", "Two", "Three"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        child = state._slots[body["key"]]
        for n in (1, 2):
            await _wait_for_calls(gen, n)
            child.append("user", f"newer question {n}", "msg msg-u", meta=_HUMAN)
            gen.release[n - 1].set()
        await drain_background_tasks(state)

    assert len(gen.calls) == 2
    assert child.title == "↳ Fork of Rotating log files"
    assert child._title_in_flight is False


@pytest.mark.asyncio
async def test_one_fork_spends_one_pass(tmp_path, monkeypatch) -> None:
    """A second pass started while the fork's own pass generates is excluded by
    the in-flight guard, and the pass spends the refresh milestones the copied
    turns already crossed, so the next chat_done refresh does not immediately
    generate again."""
    gen = _Generator(["Canary deploy plan", "Duplicate"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    refreshes: list[str] = []

    async def _refresh(_state, _messages, current_title, *, session_key: str = "") -> str:
        refreshes.append(current_title)
        return ""

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _refresh)
    state = _seeded_state(tmp_path)
    for i in range(8):
        state._slots["parent"].append("user", f"question {i}", "msg msg-u", meta=_HUMAN)
        state._slots["parent"].append("assistant", f"answer {i}", "msg msg-a")

    async with TestClient(TestServer(_make_app(state))) as client:
        gen.hold()
        body = await _fork(client)
        child = state._slots[body["key"]]
        # The fork's own pass is parked inside generation, so a second pass
        # started now meets the in-flight guard and generates nothing.
        await _wait_for_calls(gen, 1)
        await refresh_forked_title(state, child)
        assert len(gen.calls) == 1
        gen.release[0].set()
        await drain_background_tasks(state)
        await chat_title.maybe_refresh_title(state, child)

    assert len(gen.calls) == 1
    assert refreshes == []
    assert child.title == "↳ Canary deploy plan"


@pytest.mark.asyncio
async def test_a_manually_named_parent_hands_down_a_final_title(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path, origin=_TITLE_ORIGIN_USER)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert gen.calls == []
    assert child.title == "↳ Fork of Rotating log files"
    assert child._title_origin == _TITLE_ORIGIN_USER


@pytest.mark.asyncio
async def test_an_agent_fork_with_a_title_keeps_it(tmp_path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import chat_fork, create_rate_limit
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.chat_utils import slot_history_key

    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())
    monkeypatch.setattr(sc, "sel", lambda: MagicMock())
    create_rate_limit.reset_for_tests()
    state = _seeded_state(tmp_path)
    caller = state._slots["parent"]

    try:
        result = await sc.fork_session(
            state, caller_session_key=slot_history_key(caller), title="cluster 3"
        )
        await drain_background_tasks(state)
    finally:
        create_rate_limit.reset_for_tests()

    child = state.get_slot(result["target"])
    assert gen.calls == []
    assert child.title == "cluster 3"
    assert child._title_origin == _TITLE_ORIGIN_USER


@pytest.mark.asyncio
async def test_a_later_refresh_keeps_the_fork_marker(tmp_path, monkeypatch) -> None:
    """The fork is now refreshable, so the milestone refresh must not drop ``↳ ``."""
    gen = _Generator(["Canary deploy plan"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    seen: list[str] = []

    async def _refresh(_state, _messages, current_title, *, session_key: str = "") -> str:
        seen.append(current_title)
        return "Canary rollout and rollback"

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _refresh)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await drain_background_tasks(state)
    child = state._slots[body["key"]]
    for i in range(8):
        child.append("user", f"follow-up {i}", "msg msg-u", meta=_HUMAN)
    await chat_title.maybe_refresh_title(state, child)

    assert seen == ["Canary deploy plan"], "the model judges the name without the marker"
    assert child.title == "↳ Canary rollout and rollback"
