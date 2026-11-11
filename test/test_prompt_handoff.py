"""A session's background helpers never run ahead of the turn a person is waiting on.

A send starts helpers that use the shared background runtime: the session title, a
fork's name, topic tags. Starting that runtime holds a lock the turn's own admission
waits on, so each helper waits for the turn's prompt handoff
(``dashboard/prompt_handoff.py``) before it reaches the runtime. Where the chat runner
marks the handoff is pinned by ``test_title_after_prompt_handoff.py``.

Every wait here is on a signal the test controls: the turn is a task parked on an
event, and "the helper is waiting" is read off the slot's handoff mark before any
absence is asserted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state, drain_background_tasks

from kiro_crew.dashboard import chat_auto_tag, chat_title, chat_title_fork
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.testing.wait import (
    FALLBACK_TIMEOUT_SECS,
    async_wait_until,
    await_until,
    finished,
)


class _Turn:
    """A turn task the test steps: it runs until told how to end."""

    def __init__(self, slot: _ChatSlot) -> None:
        self._end = asyncio.Event()
        self._error: BaseException | None = None
        self.task = asyncio.ensure_future(self._run())
        slot.task = self.task

    async def _run(self) -> None:
        await self._end.wait()
        if self._error is not None:
            raise self._error

    async def end(self, how: str = "returns") -> None:
        """End the turn the way a refused, failed or stopped turn ends."""
        if how == "cancelled":
            self.task.cancel()
        else:
            self._error = RuntimeError("the turn failed") if how == "raises" else None
            self._end.set()
        await _ended(self.task, "the turn")
        if not self.task.cancelled():
            self.task.exception()  # read, so the loop does not report it as unretrieved


async def _ended(task: asyncio.Future[Any], what: str) -> None:
    """Wait for *task* to end, however it ends, under the lost-run bound."""
    done, _ = await asyncio.wait({task}, timeout=FALLBACK_TIMEOUT_SECS)
    assert done, f"{what} did not end within {FALLBACK_TIMEOUT_SECS:.0f} s"


async def _waiting(slot: _ChatSlot) -> None:
    """Return once a helper is parked on *slot*'s handoff mark."""
    await async_wait_until(
        lambda: slot._handoff_mark is not None,
        describe=lambda: "no helper is waiting for the slot's handoff",
    )


def _slot_with_question(text: str = "why is the build red?") -> _ChatSlot:
    slot = _ChatSlot("chat-1")
    slot.messages.append({"role": "user", "content": text, "meta": {HUMAN_TURN_META_KEY: True}})
    return slot


def _title_state(*slots: _ChatSlot) -> Any:
    state = MagicMock()
    state.conversation_log = MagicMock()
    state._slots = {slot.key: slot for slot in slots}
    return state


@pytest.fixture(name="handoff")
def handoff_fixture() -> Any:
    """The module under test, imported per test.

    A tree without it then fails these tests one by one; a module-level import
    would instead stop the collection of every file run in the same session.
    """
    from kiro_crew.dashboard import prompt_handoff

    return prompt_handoff


# ── the wait itself ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_slot_with_no_turn_does_not_wait(handoff: Any) -> None:
    slot = _ChatSlot("chat-1")

    await finished(handoff.turn_handed_off(slot), "the wait on an idle slot")

    assert slot._handoff_mark is None, "an idle slot parked a helper"


@pytest.mark.asyncio
async def test_a_stand_in_slot_that_publishes_no_task_does_not_wait(handoff: Any) -> None:
    """Tagging is also called with a plain stand-in for a slot: it has no turn."""
    await finished(handoff.turn_handed_off(SimpleNamespace(key="chat-1")), "the wait on a stand-in")


@pytest.mark.asyncio
async def test_a_helper_waits_for_the_running_turn_s_handoff(handoff: Any) -> None:
    slot = _ChatSlot("chat-1")
    turn = _Turn(slot)
    helper = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await _waiting(slot)
    assert not helper.done()

    handoff.prompt_handed_off(slot)

    await await_until(helper, what="the helper after the handoff")
    assert not turn.task.done(), "the handoff must not end the turn"
    assert slot._handoff_mark is None
    await turn.end()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["returns", "raises", "cancelled"])
async def test_a_turn_that_ends_before_its_handoff_releases_the_helper(
    handoff: Any, how: str
) -> None:
    """Refused, failed and stopped turns: none of them hands off, none strands a helper."""
    slot = _ChatSlot("chat-1")
    turn = _Turn(slot)
    helper = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await _waiting(slot)
    assert not helper.done()

    await turn.end(how)

    await await_until(helper, what=f"the helper after a turn that {how}")
    assert slot._handoff_generation != slot._turn_generation, "no handoff was marked"
    assert slot._handoff_mark is None, "a mark nothing will resolve was left on the slot"


@pytest.mark.asyncio
async def test_a_turn_past_its_handoff_holds_no_later_helper(handoff: Any) -> None:
    slot = _ChatSlot("chat-1")
    turn = _Turn(slot)
    handoff.prompt_handed_off(slot)

    await finished(handoff.turn_handed_off(slot), "the wait on a turn that already handed off")

    assert slot._handoff_mark is None
    await turn.end()


@pytest.mark.asyncio
async def test_the_next_turn_is_waited_for_again(handoff: Any) -> None:
    slot = _ChatSlot("chat-1")
    first = _Turn(slot)
    handoff.prompt_handed_off(slot)
    await first.end()
    second = _Turn(slot)
    helper = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await _waiting(slot)
    assert not helper.done(), "the first turn's handoff released a helper of the second"

    handoff.prompt_handed_off(slot)

    await await_until(helper, what="the helper after the second turn's handoff")
    await second.end()


@pytest.mark.asyncio
async def test_a_turn_that_ends_early_and_passes_the_slot_on_keeps_the_helper_waiting(
    handoff: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused turn hands its queue to the next one before its own task is done."""
    slot = _ChatSlot("chat-1")
    first = _Turn(slot)
    checks: list[object] = []
    real = handoff._turn_before_handoff

    def _checked(checked_slot: Any) -> Any:
        found = real(checked_slot)
        checks.append(found)
        return found

    monkeypatch.setattr(handoff, "_turn_before_handoff", _checked)
    helper = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await _waiting(slot)
    second = _Turn(slot)

    await first.end()

    await async_wait_until(
        lambda: second.task in checks,
        describe=lambda: f"the helper did not look again after the first turn ended: {checks!r}",
    )
    assert not helper.done(), "the helper ran ahead of the turn that took the slot"
    handoff.prompt_handed_off(slot)
    await await_until(helper, what="the helper after the successor's handoff")
    await second.end()


@pytest.mark.asyncio
async def test_helpers_parked_on_one_mark_all_wake_at_the_successor_s_handoff(
    handoff: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One helper waits on the turn that ends early, one joins while the queued turn has the slot."""
    slot = _ChatSlot("chat-1")
    first = _Turn(slot)
    looks: list[object] = []
    real = handoff._turn_before_handoff

    def _looked(checked_slot: Any) -> Any:
        found = real(checked_slot)
        looks.append(found)
        return found

    monkeypatch.setattr(handoff, "_turn_before_handoff", _looked)
    title = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await _waiting(slot)
    second = _Turn(slot)
    tags = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await async_wait_until(
        lambda: second.task in looks,
        describe=lambda: f"the second helper has not looked at the slot yet: {looks!r}",
    )

    await first.end()
    # Both helpers look again once the first turn is over: three looks at the successor.
    await async_wait_until(
        lambda: looks.count(second.task) >= 3,
        describe=lambda: f"a helper was not woken when the first turn ended: {looks!r}",
    )
    assert not title.done() and not tags.done(), "a helper ran ahead of the queued turn"

    handoff.prompt_handed_off(slot)

    await await_until(title, what="the helper that waited on the first turn")
    await await_until(tags, what="the helper that joined while the queued turn had the slot")
    await second.end()


@pytest.mark.asyncio
async def test_a_cancelled_helper_leaves_the_turn_running(handoff: Any) -> None:
    slot = _ChatSlot("chat-1")
    turn = _Turn(slot)
    helper = asyncio.ensure_future(handoff.turn_handed_off(slot))
    await _waiting(slot)

    helper.cancel()
    await _ended(helper, "the cancelled helper")

    assert helper.cancelled()
    assert not turn.task.done()
    handoff.prompt_handed_off(slot)
    await turn.end()


@pytest.mark.asyncio
async def test_a_helper_the_turn_itself_awaits_does_not_wait_for_that_turn(handoff: Any) -> None:
    slot = _ChatSlot("chat-1")

    async def _turn_that_awaits_a_helper() -> None:
        slot.task = asyncio.current_task()
        await handoff.turn_handed_off(slot)

    await finished(_turn_that_awaits_a_helper(), "a helper awaited inside its own turn")


# ── the helpers a send starts ───────────────────────────────────────────────


@pytest.fixture(name="generated")
def generated_fixture(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """Title generations asked of the background runtime, in order."""
    asked: list[list[dict[str, Any]]] = []

    async def _generate(_state: Any, messages: Any, *, session_key: str = "") -> str:
        asked.append(list(messages))
        return "Named"

    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _generate)
    monkeypatch.setattr(chat_title, "_persist_title", AsyncMock())
    monkeypatch.setattr(chat_title, "maybe_suggest_folder", AsyncMock())
    monkeypatch.setattr(chat_title, "_ticket_key_if_enabled", AsyncMock(return_value=""))
    monkeypatch.setattr(chat_title, "_reveal_title", AsyncMock())
    return asked


@pytest.mark.asyncio
async def test_the_title_reaches_the_background_runtime_only_after_the_handoff(
    handoff: Any,
    generated: list[list[dict[str, Any]]],
) -> None:
    slot = _slot_with_question()
    state = _title_state(slot)
    turn = _Turn(slot)
    title = asyncio.ensure_future(chat_title._maybe_auto_title(state, slot))
    await _waiting(slot)
    assert generated == [], "the title was generated before the turn's handoff"
    assert slot._title_in_flight is False

    handoff.prompt_handed_off(slot)
    await await_until(title, what="the title attempt after the handoff")

    assert len(generated) == 1, "exactly one generation per send"
    assert slot.title == "Named" and slot._titled is True
    await turn.end()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["returns", "raises", "cancelled"])
async def test_a_turn_that_never_hands_off_still_gets_its_session_titled(
    handoff: Any, generated: list[list[dict[str, Any]]], how: str
) -> None:
    """As before: the title comes from the person's message, whatever the turn did."""
    slot = _slot_with_question()
    state = _title_state(slot)
    turn = _Turn(slot)
    title = asyncio.ensure_future(chat_title._maybe_auto_title(state, slot))
    await _waiting(slot)
    assert generated == []

    await turn.end(how)
    await await_until(title, what=f"the title attempt after a turn that {how}")

    assert len(generated) == 1
    assert not title.cancelled() and title.exception() is None, "no orphan, no error"


@pytest.mark.asyncio
async def test_the_end_of_turn_title_pass_does_not_wait(
    handoff: Any,
    generated: list[list[dict[str, Any]]],
) -> None:
    """chat_done's pass runs for a turn past its handoff: it goes straight through."""
    slot = _slot_with_question()
    state = _title_state(slot)
    turn = _Turn(slot)
    handoff.prompt_handed_off(slot)

    await finished(chat_title._maybe_auto_title(state, slot), "the end-of-turn title pass")

    assert len(generated) == 1
    await turn.end()


@pytest.mark.asyncio
async def test_a_fork_is_named_only_after_the_handoff(
    handoff: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _slot_with_question()
    state = _title_state(slot)
    state._background_tasks = set()
    named = AsyncMock()
    monkeypatch.setattr(chat_title_fork, "refresh_forked_title", named)
    turn = _Turn(slot)

    chat_title_fork.schedule_fork_title(state, slot)
    await _waiting(slot)
    named.assert_not_called()
    assert slot._title_task is not None and not slot._title_task.done()

    handoff.prompt_handed_off(slot)
    await await_until(slot._title_task, what="the fork's title pass")

    named.assert_awaited_once_with(state, slot)
    await turn.end()


@pytest.mark.asyncio
async def test_topic_tags_wait_for_the_handoff_and_the_project_tag_does_not(
    handoff: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.project = str(tmp_path / "workspace-topic")
    slot.append("user", "why is the build red?", "msg msg-u", meta={HUMAN_TURN_META_KEY: True})
    asked = AsyncMock(return_value="build")
    monkeypatch.setattr(chat_auto_tag, "run_bg_oneliner", asked)
    monkeypatch.setattr(chat_auto_tag, "_topic_tags_enabled", AsyncMock(return_value=True))
    turn = _Turn(slot)

    tagging = asyncio.ensure_future(chat_auto_tag.maybe_auto_tag(state, slot))
    await _waiting(slot)

    names = {tag["name"] for tag in state._tags if tag["id"] in slot.tags}
    assert names == {"workspace-topic"}, "the project tag needs no model and does not wait"
    asked.assert_not_called()

    handoff.prompt_handed_off(slot)
    await await_until(tagging, what="the topic pass after the handoff")

    asked.assert_awaited_once()
    await turn.end()


# ── the send handler ────────────────────────────────────────────────────────


class _Sends:
    """The real ``POST /api/chat`` handler over a turn and a pre-dispatch hold the test steps."""

    def __init__(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, handoff: Any) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        self.state = _make_state(tmp_path)
        self.state.broadcast_ws = MagicMock()
        self.state.push_slots_update = MagicMock()
        self.started, self.hand_off, self.end = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.held, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

        async def _turn(_state: Any, slot: Any, _message: str, **_kwargs: Any) -> None:
            self.started.set()
            await self.hand_off.wait()
            handoff.prompt_handed_off(slot)
            await self.end.wait()

        async def _hold(_state: Any, _slot: Any) -> None:
            # Stands for the Slack auto-link the handler awaits before it dispatches.
            self.held.set()
            await self.release.wait()

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", _turn)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.maybe_auto_link_slack", _hold)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.maybe_auto_tag", AsyncMock())

    def client(self) -> TestClient:
        return TestClient(TestServer(_make_app(self.state)))

    @property
    def slot(self) -> Any:
        return self.state._slots["s1"]


async def _send(client: TestClient, message: str) -> dict[str, Any]:
    response = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": message})
    assert response.status == 200, await response.text()
    body: dict[str, Any] = await response.json()
    return body


@pytest.mark.asyncio
async def test_a_second_message_before_the_handoff_starts_no_second_title(
    handoff: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    generated: list[list[dict[str, Any]]],
) -> None:
    sends = _Sends(tmp_path, monkeypatch, handoff)

    async with sends.client() as client:
        await finished(_send(client, "first"), "the first send's receipt")
        slot = sends.slot
        await await_until(sends.started, what="the first turn")
        await _waiting(slot)
        assert generated == [], "the title ran ahead of the first turn"

        await finished(_send(client, "second"), "the second send's receipt")
        assert [entry["content"] for entry in slot._queue] == ["second"]
        assert generated == [], "a queued second message started the title early"

        sends.hand_off.set()
        title = slot._title_task
        assert title is not None
        await await_until(title, what="the title attempt after the handoff")
        assert len(generated) == 1, "one title attempt for the session, not one per message"

        sends.end.set()
        await finished(drain_background_tasks(sends.state), "the background tasks draining")

    assert len(generated) == 1


@pytest.mark.asyncio
async def test_a_send_held_before_its_dispatch_starts_no_title_during_the_hold(
    handoff: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    generated: list[list[dict[str, Any]]],
) -> None:
    """The handler awaits before it publishes the turn's task: no helper may slip through there."""
    sends = _Sends(tmp_path, monkeypatch, handoff)
    sends.release.clear()

    async with sends.client() as client:
        post = asyncio.ensure_future(_send(client, "first"))
        # The hold wakes this test after every task the handler created before it.
        await await_until(sends.held, what="the handler's hold before dispatch")
        assert generated == [], "the title reached the background runtime before the turn existed"

        sends.release.set()
        await await_until(post, what="the send's receipt")
        await await_until(sends.started, what="the turn")
        await _waiting(sends.slot)
        assert generated == []

        sends.hand_off.set()
        await await_until(sends.slot._title_task, what="the title attempt after the handoff")
        assert len(generated) == 1

        sends.end.set()
        await finished(drain_background_tasks(sends.state), "the background tasks draining")


@pytest.mark.asyncio
async def test_a_send_stopped_during_the_hold_is_still_titled(
    handoff: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    generated: list[list[dict[str, Any]]],
) -> None:
    """No turn starts, so nothing is left to delay: the row names the session, as before."""
    sends = _Sends(tmp_path, monkeypatch, handoff)
    sends.release.clear()

    async with sends.client() as client:
        post = asyncio.ensure_future(_send(client, "first"))
        await await_until(sends.held, what="the handler's hold before dispatch")
        sends.slot._stop_generation += 1  # a Stop pressed during the hold
        sends.release.set()
        receipt = await await_until(post, what="the send's receipt")

        assert receipt.get("stopped") is True
        assert not sends.started.is_set(), "a stopped send must not start its turn"
        await await_until(sends.slot._title_task, what="the title attempt of a stopped send")
        await finished(drain_background_tasks(sends.state), "the background tasks draining")

    assert len(generated) == 1
