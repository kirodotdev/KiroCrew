"""A fork child is renamed after the latest question it holds.

A fork is born titled ``↳ Fork of <parent>``, which names the parent's topic,
and keeps that name until the person sends the child its first message. That
send runs ONE background title pass (``chat_title.refresh_forked_title``) over
the newest window of the child, which ends with the message just sent. A
question copied from the parent never starts the pass.

Invariants locked here:

- Forking alone generates nothing: the child keeps its fork title.
- The first person-typed message renames the child; the parent's title and
  transcript are unchanged.
- A queued first message is named at chat_done instead.
- A non-person row (session_send, automation) or an untitleable first message
  never names it, and never falls back to a copied parent question.
- A generation failure never fails the send and is not retried.
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
    title_then_refresh,
)
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.testing.wait import async_wait_until

_HUMAN = {HUMAN_TURN_META_KEY: True}
_FIRST = "write the database failover drill"
# What a fork of ``_seeded_state``'s parent holds before its first message.
_COPIED = [
    "how do I rotate the log files",
    "use logrotate",
    "now plan the canary deploy for the API",
    "here is a plan",
]


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _instant_turn(_floor_monkeypatch):
    """A send's turn answers at once; only the send handler's titling runs."""

    async def reply(_state, slot, _message, **_kwargs):
        slot.append("assistant", "on it", "msg msg-a")
        slot.append("done", "", "done", broadcast=False)

    _floor_monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", reply)


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


async def _send(client, key: str, text: str = _FIRST) -> None:
    """Type *text* into slot *key* as the dashboard user."""
    resp = await client.post("/api/chat", json={"slot": key, "message": text})
    assert resp.status == 200
    await resp.text()


@pytest.mark.asyncio
async def test_forking_alone_keeps_the_fork_title(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await drain_background_tasks(state)

    assert body["title"] == "↳ Fork of Rotating log files"
    assert gen.calls == []
    child = state._slots[body["key"]]
    assert child.title == "↳ Fork of Rotating log files"
    assert child._title_origin == _TITLE_ORIGIN_AUTO


@pytest.mark.asyncio
async def test_the_first_message_names_the_child(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    gen = _Generator(["Database failover drill"])
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
        await drain_background_tasks(state)
        await _send(client, body["key"])
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert child.title == "↳ Database failover drill"
    assert child._title_origin == _TITLE_ORIGIN_AUTO
    # The message sent to the child, read after the turns copied from the parent.
    assert len(gen.calls) == 1
    assert gen.calls[0][:5] == [*_COPIED, _FIRST]
    assert (child.key, "↳ Database failover drill") in pushes
    assert parent.title == "Rotating log files"
    assert [(m["role"], m["content"]) for m in parent.messages] == parent_rows
    # The new name and its provenance are durable.
    del state._slots[body["key"]]
    reloaded = _rehydrate_slot_from_history(state, body["key"])
    assert reloaded is not None
    assert reloaded.title == "↳ Database failover drill"
    assert reloaded._title_origin == _TITLE_ORIGIN_AUTO


@pytest.mark.asyncio
async def test_a_tail_fork_is_named_by_its_first_message(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Database failover drill"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        # The name ``fork_slot`` gives a tail fork; the direction is behind a flag.
        state._slots[body["key"]].title = "↳ Tail of Rotating log files"
        await _send(client, body["key"])
        await drain_background_tasks(state)

    assert len(gen.calls) == 1
    assert gen.calls[0][:5] == [*_COPIED, _FIRST]
    assert state._slots[body["key"]].title == "↳ Database failover drill"


@pytest.mark.asyncio
async def test_a_queued_first_message_is_named_at_chat_done(tmp_path, monkeypatch) -> None:
    """A message that reached the turn through the queue skipped the send
    handler's trigger; chat_done's chained titling names the fork instead."""
    gen = _Generator(["Database failover drill"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
    child = state._slots[body["key"]]
    child.append("user", _FIRST, "msg msg-u", meta=_HUMAN)
    child.append("assistant", "on it", "msg msg-a")
    await title_then_refresh(state, child)

    assert gen.calls == [[*_COPIED, _FIRST, "on it"]]
    assert child.title == "↳ Database failover drill"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "meta"),
    [("a session_send from another session", None), ("", _HUMAN)],
    ids=["not-person-typed", "untitleable"],
)
async def test_a_first_row_that_cannot_name_it_keeps_the_fork_title(
    tmp_path, monkeypatch, content, meta
) -> None:
    """Neither a non-person row nor an untitleable message names the fork, and
    neither falls back to the parent's question the copy still holds."""
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
    child = state._slots[body["key"]]
    child.append("user", content, "msg msg-u", meta=meta)
    child.append("assistant", "ok", "msg msg-a")
    await title_then_refresh(state, child)

    assert gen.calls == []
    assert child.title == "↳ Fork of Rotating log files"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_turns",
    [5, 8],
    ids=["non-person-row-reaches-milestone", "copied-turns-past-milestone"],
)
async def test_copied_turns_start_no_refresh_before_a_person_names_the_fork(
    tmp_path, monkeypatch, extra_turns
) -> None:
    """A fork keeps its fork title through a chat_done that no person's message
    started, whether the copied turns already passed the first refresh
    milestone or a non-person row is the turn that reaches it."""
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    refreshes: list[str] = []

    async def _refresh(_state, _messages, current_title, *, session_key: str = "") -> str:
        refreshes.append(current_title)
        return "Named from the parent's turns"

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _refresh)
    state = _seeded_state(tmp_path)
    for i in range(extra_turns):
        state._slots["parent"].append("user", f"question {i}", "msg msg-u", meta=_HUMAN)
        state._slots["parent"].append("assistant", f"answer {i}", "msg msg-a")

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
    child = state._slots[body["key"]]
    child.append("user", "a session_send from another session", "msg msg-u")
    child.append("assistant", "ok", "msg msg-a")
    await title_then_refresh(state, child)

    assert gen.calls == []
    assert refreshes == []
    assert child.title == "↳ Fork of Rotating log files"


@pytest.mark.asyncio
async def test_a_chat_done_with_no_new_row_keeps_the_fork_title(tmp_path, monkeypatch) -> None:
    """A chat_done that added no user row finds a copied parent question as the
    newest person-typed row, and that question never names the fork."""
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
    child = state._slots[body["key"]]
    child.append("assistant", "a reply with no new question", "msg msg-a")
    await title_then_refresh(state, child)

    assert gen.calls == []
    assert child.title == "↳ Fork of Rotating log files"


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
        await _send(client, body["key"])
        await drain_background_tasks(state)
        await _send(client, body["key"], "and the rollback plan")
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert len(gen.calls) == 1
    assert child.title == "↳ Fork of Rotating log files"
    assert child._title_in_flight is False


@pytest.mark.asyncio
async def test_a_manual_rename_while_pending_wins(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Database failover drill"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _send(client, body["key"])
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

    gen = _Generator(["Database failover drill"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _send(client, body["key"])
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
    assert "failover" not in str(after)


@pytest.mark.asyncio
async def test_a_failed_persist_publishes_nothing(tmp_path, monkeypatch) -> None:
    """Persist before publish: when the metadata write fails (a full disk), the
    live slot keeps the title and refresh state a reload would restore."""
    gen = _Generator(["Database failover drill"])
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
        await _send(client, body["key"])
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
    gen = _Generator(["Database failover drill", "Rollback plan"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        child = state._slots[body["key"]]
        await _send(client, body["key"])
        await _wait_for_calls(gen, 1)
        child.append("user", "actually, write the rollback plan", "msg msg-u", meta=_HUMAN)
        gen.release[0].set()
        await _wait_for_calls(gen, 2)
        # The stale result was never applied.
        assert child.title == "↳ Fork of Rotating log files"
        gen.release[1].set()
        await drain_background_tasks(state)

    assert len(gen.calls) == 2
    assert "actually, write the rollback plan" not in gen.calls[0]
    assert "actually, write the rollback plan" in gen.calls[1]
    assert child.title == "↳ Rollback plan"


@pytest.mark.asyncio
async def test_generation_is_bounded_when_questions_keep_arriving(tmp_path, monkeypatch) -> None:
    gen = _Generator(["One", "Two", "Three"])
    gen.hold()
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        child = state._slots[body["key"]]
        await _send(client, body["key"])
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
    gen = _Generator(["Database failover drill", "Duplicate"])
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
        await _send(client, body["key"])
        # The fork's own pass is parked inside generation, so a second pass
        # started now meets the in-flight guard and generates nothing.
        await _wait_for_calls(gen, 1)
        await refresh_forked_title(state, child)
        assert len(gen.calls) == 1
        gen.release[0].set()
        await drain_background_tasks(state)
        await title_then_refresh(state, child)

    assert len(gen.calls) == 1
    assert refreshes == []
    assert child.title == "↳ Database failover drill"


@pytest.mark.asyncio
async def test_a_manually_named_parent_hands_down_a_final_title(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Should not be used"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path, origin=_TITLE_ORIGIN_USER)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _send(client, body["key"])
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
        child = state.get_slot(result["target"])
        child.append("user", _FIRST, "msg msg-u", meta=_HUMAN)
        child.append("assistant", "on it", "msg msg-a")
        await title_then_refresh(state, child)
    finally:
        create_rate_limit.reset_for_tests()

    assert gen.calls == []
    assert child.title == "cluster 3"
    assert child._title_origin == _TITLE_ORIGIN_USER


@pytest.mark.asyncio
async def test_a_later_refresh_keeps_the_fork_marker(tmp_path, monkeypatch) -> None:
    """The fork is refreshable, so the milestone refresh must not drop ``↳ ``."""
    gen = _Generator(["Database failover drill"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    seen: list[str] = []

    async def _refresh(_state, _messages, current_title, *, session_key: str = "") -> str:
        seen.append(current_title)
        return "Failover drill and rollback"

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _refresh)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _send(client, body["key"])
        await drain_background_tasks(state)
    child = state._slots[body["key"]]
    for i in range(8):
        child.append("user", f"follow-up {i}", "msg msg-u", meta=_HUMAN)
    await chat_title.maybe_refresh_title(state, child)

    assert seen == ["Database failover drill"], "the model judges the name without the marker"
    assert child.title == "↳ Failover drill and rollback"


@pytest.mark.asyncio
async def test_a_short_follow_up_is_named_with_the_turns_before_it(tmp_path, monkeypatch) -> None:
    """A first message such as "yes do that" names nothing alone; the turns the
    fork copied say what it is about, inside the usual title window."""
    gen = _Generator(["Canary deploy plan"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)
    for i in range(8):
        state._slots["parent"].append("user", f"question {i}", "msg msg-u", meta=_HUMAN)
        state._slots["parent"].append("assistant", f"answer {i}", "msg msg-a")

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _send(client, body["key"], "yes do that")
        await drain_background_tasks(state)

    assert len(gen.calls) == 1
    window = gen.calls[0]
    assert len(window) == chat_title._TITLE_PROMPT_WINDOW
    assert "answer 7" in window
    assert "yes do that" in window
    assert state._slots[body["key"]].title == "↳ Canary deploy plan"


@pytest.mark.asyncio
async def test_a_fork_of_a_fork_is_named_the_same_way(tmp_path, monkeypatch) -> None:
    gen = _Generator(["Database failover drill"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)

    async with TestClient(TestServer(_make_app(state))) as client:
        first = await _fork(client)
        resp = await client.post(f"/api/chat/slots/{first['key']}/fork", json={})
        assert resp.status == 200
        second = await resp.json()
        await _send(client, second["key"])
        await drain_background_tasks(state)

    assert second["title"] == "↳ Fork of Fork of Rotating log files"
    assert len(gen.calls) == 1
    assert gen.calls[0][:5] == [*_COPIED, _FIRST]
    assert state._slots[second["key"]].title == "↳ Database failover drill"
    assert state._slots[first["key"]].title == "↳ Fork of Rotating log files"


@pytest.mark.asyncio
async def test_a_temporary_fork_is_named_from_its_own_copy(tmp_path, monkeypatch) -> None:
    """A temporary session forks into a temporary child, and the pass reads the
    child's own rows, as the title pass of any temporary session does."""
    gen = _Generator(["Database failover drill"])
    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", gen)
    state = _seeded_state(tmp_path)
    state._slots["parent"].memory_mode = "temporary"

    async with TestClient(TestServer(_make_app(state))) as client:
        body = await _fork(client)
        await _send(client, body["key"])
        await drain_background_tasks(state)

    child = state._slots[body["key"]]
    assert child.memory_mode == "temporary"
    assert gen.calls[0][:5] == [*_COPIED, _FIRST]
    assert child.title == "↳ Database failover drill"
    assert state._slots["parent"].title == "Rotating log files"
