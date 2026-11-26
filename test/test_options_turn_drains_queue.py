"""A Slack turn an interaction opened drains the thread's queue when it ends.

A message typed while an OPTIONS choice, an ``action::`` button, a message-shortcut
forward or a review revision is being answered is queued with ⏳, because that turn holds
the thread's session. The queue drains only at a router task's end, so the interaction's
turn is registered as that task: the typed message is dispatched exactly once when it
ends. The registration waits for the key: a click admitted while a typed turn runs is not
the turn a Stop should cancel, so the routed task stays the key's until it ends, and the
Stop button cancels it as on main. A ``!stop`` pressed during an interaction's turn still
drops only the presser's entries and drains once it settles, and a routed turn's own drain
stays as it was. Each scenario runs the real router (``_route_message``) and the real
interaction handler against a session surface holding a real queue, with the turn and the
queued dispatch recorded instead of run.

The patch of ``handle_message`` goes through the interactions facade and the one of
``_dispatch_queued`` through the events facade; each forwards to the owner that reads it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections import deque
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _event, _make_orch

from kiro_crew.config.loader import ACTIVATION_ALWAYS, ChannelConfig
from kiro_crew.slack import events as slack_events
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.events_queue import router_task_thread
from kiro_crew.slack.format import OPTIONS_ACTION_PREFIX
from kiro_crew.testing.wait import async_wait_until, await_until

#: The DM thread the card was posted in, which is also the router's session key.
_THREAD = "100.0"
_CHANNEL = "D1"
_ALICE, _BOB = "U_OWNER", "U_BOB"
#: A review draft requested in channel ``C1``, thread ``_THREAD``.
_DRAFT_KEY = f"C1|{_THREAD}|abc"


class _Sessions:
    """The session surface the router and a button turn touch, holding a REAL queue.

    ``enqueue`` follows the manager's rule: an entry is taken when the caller forces it
    or while the session's turn holds the semaphore (``busy``), which the recorded turn
    sets for as long as it runs. The ``!stop`` members follow the manager's contracts
    where this file reads them: ``detach_queue`` empties the queue into its result,
    ``clear_queue(only=...)`` unlinks exactly those entries' files, and ``stop_turn``
    ends the running turn through ``during_stop`` before it reports a soft stop.
    """

    def __init__(self) -> None:
        self.queue: deque[tuple[str, str, dict]] = deque()
        self.busy = False
        self.stop_calls: list[dict[str, Any]] = []
        self.during_stop: Any = None

    def has_session(self, key: str) -> bool:
        return True

    def get_session_for_thread(self, key: str) -> str | None:
        return None

    def is_busy(self, key: str) -> bool:
        return self.busy

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        return False

    def enqueue(self, key: str, msg_ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
        if force or self.busy:
            self.queue.append((msg_ts, text, kw))
            return True
        return False

    def has_queued(self, key: str) -> bool:
        return bool(self.queue)

    def dequeue(self, key: str) -> tuple[str, str, dict] | None:
        return self.queue.popleft() if self.queue else None

    def detach_queue(self, key: str) -> tuple[Any, ...]:
        taken = tuple(self.queue)
        self.queue.clear()
        return taken

    def restore_queue(self, key: str, entries: tuple[Any, ...]) -> None:
        self.queue.extendleft(reversed(entries))

    def clear_queue(self, key: str, owned_by: Any = None, *, only: Any = None) -> None:
        dropped = list(only) if only is not None else list(self.queue)
        for _, _, kwargs in dropped:
            for path in kwargs.get("image_temp_paths", ()):
                pathlib.Path(path).unlink(missing_ok=True)
        wanted = {id(item) for item in dropped}
        kept = [item for item in self.queue if id(item) not in wanted]
        self.queue.clear()
        self.queue.extend(kept)

    async def stop_turn(self, key: str, *, preserve_queue: bool = False, **hooks: Any) -> str:
        self.stop_calls.append({"key": key, "preserve_queue": preserve_queue})
        if self.during_stop is not None:
            await self.during_stop()
        if not preserve_queue:
            self.clear_queue(key)
        on_soft = hooks.get("on_soft")
        if on_soft is not None:
            await on_soft()
        return "soft"


class _Turn:
    """``handle_message`` recorded, not run: it holds the session until released."""

    def __init__(self, sessions: _Sessions) -> None:
        self.sessions = sessions
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, **kwargs})
        self.sessions.busy = True
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.sessions.busy = False


class _Dispatches:
    """``_dispatch_queued`` recorded: each call is one queued message started."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(
        self, _orch: Any, key: str, ts: str, text: str, kwargs: dict, **_kw: Any
    ) -> None:
        self.calls.append((key, ts, text))


class _Thread:
    """One DM thread: the orchestrator, its session surface and the recorders."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``C1`` always answers, so a typed message there reaches the busy check.
        self.orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        self.orch._allowed_users = {_ALICE, _BOB}
        self.orch.slack.open_dm = AsyncMock(return_value=_CHANNEL)
        self.orch.slack.post_message = AsyncMock(return_value="91.0")
        self.sessions = _Sessions()
        self.orch.sessions = self.sessions
        self.turn = _Turn(self.sessions)
        self.dispatches = _Dispatches()
        #: The module's fire-and-forget tasks before this test, so only its own are joined.
        self._bg_before = set(slack_events._bg_tasks)
        monkeypatch.setattr(ix, "_orch", self.orch)
        monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
        monkeypatch.setattr(ix, "is_owner", lambda uid: uid == _ALICE)
        monkeypatch.setattr(ix, "sel", lambda: MagicMock())
        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=True))
        monkeypatch.setattr(ix, "handle_message", self.turn)
        monkeypatch.setattr(slack_events, "_dispatch_queued", self.dispatches)
        # The router's own start of a turn, which no scenario here expects.
        self.routed = AsyncMock()
        monkeypatch.setattr(slack_events, "handle_message", self.routed)
        monkeypatch.setattr(slack_events, "is_allowed_user", lambda uid: True)
        monkeypatch.setattr(slack_events, "is_owner", lambda uid: uid == _ALICE)

    async def click(self, user: str = _ALICE) -> None:
        """Press the first button of an OPTIONS card posted in the thread."""
        control = {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": f"{OPTIONS_ACTION_PREFIX}0",
                    "text": {"type": "plain_text", "text": "A"},
                    "value": "A",
                }
            ],
        }
        payload = {
            "user": {"id": user},
            "team": {"id": "T1"},
            "message": {"ts": "90.0", "thread_ts": _THREAD, "blocks": [control]},
        }
        await ix._handle_options(payload, dict(control["elements"][0]), _CHANNEL, "90.0")
        await await_until(self.turn.started, what="the button's turn to start")

    async def press_action(self, user: str = _ALICE) -> None:
        """Press an ``action::`` button on a message in the thread."""
        action = {"action_id": "btn_go", "value": 'action::{"go": 1}', "text": {"text": "Go"}}
        payload = {
            "user": {"id": user},
            "team": {"id": "T1"},
            "message": {"ts": "90.0", "thread_ts": _THREAD, "blocks": []},
        }
        await ix._handle_options(payload, action, _CHANNEL, "90.0")
        await await_until(self.turn.started, what="the action's turn to start")

    async def forward_shortcut(self, user: str = _ALICE) -> None:
        """Submit the message-shortcut modal: a new DM thread is started from the post."""
        payload = {
            "user": {"id": user},
            "team": {"id": "T1"},
            "view": {
                "callback_id": "send_to_kirocrew",
                "private_metadata": json.dumps(
                    {"channel": "C9", "ts": "80.0", "user": _BOB, "text": "look at this"}
                ),
                "state": {"values": {"comment_block": {"comment_input": {"value": "please"}}}},
                "blocks": [],
            },
        }
        await ix._handle_shortcut_submission(payload)
        await await_until(self.turn.started, what="the forwarded turn to start")

    async def revise_review(self, user: str = _ALICE) -> None:
        """Submit the review-revise modal for a draft *user* requested in ``C1`` / ``_THREAD``."""
        slack_handler._review_drafts_set(_DRAFT_KEY, "the first draft", user)
        payload = {
            "user": {"id": user},
            "view": {
                "private_metadata": _DRAFT_KEY,
                "state": {
                    "values": {
                        "mc_review_revise_block": {"mc_review_revise_input": {"value": "shorter"}}
                    }
                },
            },
        }
        await ix._handle_review_revise_submit(payload)
        await await_until(self.turn.started, what="the revision turn to start")

    async def stop_button(self, user: str = _ALICE) -> None:
        """Confirm the Stop button on a message in the thread."""
        payload = {"user": {"id": user}, "message": {"ts": "95.0", "thread_ts": _THREAD}}
        await ix._handle_stop_confirm(payload, _CHANNEL, "95.0", user)

    async def say(
        self, user: str, text: str, ts: str, *, channel: str = _CHANNEL, thread_ts: str = _THREAD
    ) -> None:
        """A member types *text* in the thread."""
        event = _event(user=user, text=text, ts=ts, thread_ts=thread_ts, channel=channel)
        await slack_events._route_message(self.orch, event, slack_events.SeenCache())

    def _own_tasks(self) -> set[asyncio.Task[Any]]:
        """The tasks this thread started that have not run their end-of-task callbacks.

        A turn and a queued dispatch leave ``_handler_tasks``, and a drain leaves
        ``_bg_tasks``, from the done callback that chains the next drain, so an empty set
        means every turn, dispatch and drain has ended AND its chaining has run.
        """
        return set(self.orch._handler_tasks) | (set(slack_events._bg_tasks) - self._bg_before)

    async def settle(self) -> None:
        """Wait until every task this thread started, drains included, has ended."""
        await async_wait_until(
            lambda: not self._own_tasks(),
            describe=lambda: sorted(task.get_name() for task in self._own_tasks()),
        )

    def queued(self) -> list[str]:
        return [entry[0] for entry in self.sessions.queue]


@pytest.fixture
def thread(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Thread]:
    saved = dict(slack_handler._review_drafts)
    slack_handler._review_drafts.clear()
    yield _Thread(monkeypatch)
    slack_handler._review_drafts.clear()
    slack_handler._review_drafts.update(saved)


class TestAButtonTurnDrainsTheQueue:
    @pytest.mark.asyncio
    async def test_a_message_typed_during_an_options_turn_is_answered_once_it_ends(
        self, thread: _Thread
    ) -> None:
        await thread.click()
        assert thread.turn.calls[0]["args"][2:5] == (_CHANNEL, "A", _THREAD)
        # The router sees the key as busy, so the typed message takes the queue path.
        assert slack_events._key_busy(thread.orch, _THREAD)

        await thread.say(_ALICE, "and then?", "101.0")
        assert thread.queued() == ["101.0"]
        thread.orch.slack.add_reaction.assert_awaited_once_with(
            _CHANNEL, "101.0", "hourglass_flowing_sand"
        )
        thread.routed.assert_not_called()
        assert thread.dispatches.calls == [], "nothing starts under the running turn"

        thread.turn.release.set()
        await thread.settle()
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]
        assert thread.queued() == [] and thread.orch._pending_queue == {}
        assert _THREAD not in thread.orch._session_tasks

    @pytest.mark.asyncio
    async def test_a_message_typed_during_an_action_button_turn_is_answered_too(
        self, thread: _Thread
    ) -> None:
        await thread.press_action()
        assert thread.turn.calls[0]["args"][2:6] == (_CHANNEL, "Go", _THREAD, "91.0")
        assert router_task_thread(thread.orch._session_tasks[_THREAD]) == _THREAD

        await thread.say(_ALICE, "and then?", "101.0")
        assert thread.queued() == ["101.0"]

        thread.turn.release.set()
        await thread.settle()
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]
        assert thread.queued() == []

    @pytest.mark.asyncio
    async def test_a_message_typed_during_a_forwarded_turn_is_answered_too(
        self, thread: _Thread
    ) -> None:
        """The message-shortcut forward starts a turn in a new DM thread it just posted."""
        await thread.forward_shortcut()
        assert thread.turn.calls[0]["args"][2:6] == (
            _CHANNEL,
            thread.turn.calls[0]["args"][3],
            "91.0",
            "91.0",
        )
        assert "look at this" in thread.turn.calls[0]["args"][3]

        await thread.say(_ALICE, "and then?", "101.0", thread_ts="91.0")
        assert thread.queued() == ["101.0"]

        thread.turn.release.set()
        await thread.settle()
        assert thread.dispatches.calls == [("91.0", "101.0", "and then?")]

    @pytest.mark.asyncio
    async def test_a_message_typed_during_a_review_revision_is_answered_too(
        self, thread: _Thread
    ) -> None:
        await thread.revise_review()
        assert thread.turn.calls[0]["args"][2:6] == (
            "C1",
            thread.turn.calls[0]["args"][3],
            _THREAD,
            _THREAD,
        )
        assert "the first draft" in thread.turn.calls[0]["args"][3]

        await thread.say(_ALICE, "and then?", "101.0", channel="C1")
        assert thread.queued() == ["101.0"]

        thread.turn.release.set()
        await thread.settle()
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]


class TestAClickDuringARoutedTurn:
    """The routed task keeps the key until it ends; the click's turn takes it after."""

    async def _routed_turn(self, thread: _Thread) -> tuple[asyncio.Task[Any], asyncio.Event]:
        started, release = asyncio.Event(), asyncio.Event()

        async def _routed(*args: Any, **kwargs: Any) -> None:
            thread.sessions.busy = True
            started.set()
            await release.wait()

        thread.routed.side_effect = _routed
        await thread.say(_ALICE, "first", "99.0")
        task = thread.orch._session_tasks[_THREAD]
        await await_until(started, what="the routed turn to start")
        return task, release

    @pytest.mark.asyncio
    async def test_the_click_is_registered_once_the_routed_turn_ends_and_drains_once(
        self, thread: _Thread
    ) -> None:
        routed_task, routed_release = await self._routed_turn(thread)
        await thread.click()
        (button_task,) = {t for t in thread.orch._handler_tasks if t is not routed_task}
        # Not the key's task while the typed turn runs: that one is what a Stop ends.
        assert thread.orch._session_tasks[_THREAD] is routed_task
        await thread.say(_ALICE, "and then?", "101.0")
        assert thread.queued() == ["101.0"]

        routed_release.set()
        # The typed turn's own end drains the queue, as on main; the click's turn takes
        # the key once that dispatch has ended.
        await async_wait_until(lambda: thread.orch._session_tasks.get(_THREAD) is button_task)
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]
        thread.turn.release.set()
        await thread.settle()
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]
        assert _THREAD not in thread.orch._session_tasks

    @pytest.mark.asyncio
    async def test_a_stop_button_ends_the_routed_turn_and_the_click_still_runs(
        self, thread: _Thread
    ) -> None:
        routed_task, _ = await self._routed_turn(thread)
        await thread.click()
        (button_task,) = {t for t in thread.orch._handler_tasks if t is not routed_task}

        await thread.stop_button(_ALICE)
        assert thread.sessions.stop_calls == [{"key": _THREAD, "preserve_queue": False}]
        # The cancel lands on the loop's next pass; the Stop asked for it before returning.
        await async_wait_until(routed_task.cancelled, describe=lambda: repr(routed_task))
        assert not button_task.done(), "and left the click's turn, which it never named"
        # The key is the click's once the cancelled task's callbacks have run.
        await async_wait_until(lambda: thread.orch._session_tasks.get(_THREAD) is button_task)

        await thread.say(_ALICE, "and then?", "101.0")
        assert thread.queued() == ["101.0"]
        thread.turn.release.set()
        await thread.settle()
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]


class TestAStopDuringAButtonTurn:
    @pytest.mark.asyncio
    async def test_a_stop_drops_only_the_pressers_entries_and_drains_once_it_settles(
        self, thread: _Thread, tmp_path: pathlib.Path
    ) -> None:
        """The per-sender stop rule holds for a turn a button opened."""
        await thread.click(_ALICE)
        # The turn's task, read where a turn the router never registered is found too.
        (button_task,) = thread.orch._handler_tasks
        await thread.say(_BOB, "bob asked", "101.0")
        await thread.say(_ALICE, "alice asked", "102.0")
        bob_file, alice_file = tmp_path / "bob.png", tmp_path / "alice.png"
        for entry, path in zip(thread.sessions.queue, (bob_file, alice_file)):
            path.write_bytes(b"x")
            entry[2]["image_temp_paths"] = [str(path)]
        assert thread.queued() == ["101.0", "102.0"]

        dispatched_during_stop: list[tuple[str, str, str]] = []

        async def _end_the_turn() -> None:
            thread.turn.release.set()
            await await_until(button_task, what="the button's turn to end")
            dispatched_during_stop.extend(thread.dispatches.calls)

        thread.sessions.during_stop = _end_the_turn
        await thread.say(_ALICE, "!stop", "103.0")
        await thread.settle()

        assert thread.sessions.stop_calls == [{"key": _THREAD, "preserve_queue": True}]
        assert dispatched_during_stop == [], "the turn's end drains nothing while the stop runs"
        assert thread.dispatches.calls == [(_THREAD, "101.0", "bob asked")]
        assert (bob_file.exists(), alice_file.exists()) == (True, False)
        assert thread.queued() == []


class TestARoutedTurnIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_typed_turns_own_drain_still_answers_the_next_message(
        self, thread: _Thread
    ) -> None:
        routed = asyncio.Event()

        async def _routed(*args: Any, **kwargs: Any) -> None:
            thread.sessions.busy = True
            await routed.wait()

        thread.routed.side_effect = _routed
        await thread.say(_ALICE, "first", "99.0")
        await thread.say(_ALICE, "and then?", "101.0")
        assert thread.queued() == ["101.0"]

        routed.set()
        await thread.settle()
        assert thread.dispatches.calls == [(_THREAD, "101.0", "and then?")]


class TestTheRegistration:
    @pytest.mark.asyncio
    async def test_a_click_from_no_thread_is_keyed_by_the_posted_label(
        self, thread: _Thread
    ) -> None:
        """The Home tab posts the label top-level; its ts is the thread a reply joins."""
        started = asyncio.Event()

        async def _turn() -> None:
            started.set()

        task = ix._start_interaction_turn(thread.orch, _turn(), "91.0", None)
        assert thread.orch._session_tasks["91.0"] is task
        assert router_task_thread(task) is None
        await thread.settle()
        assert "91.0" not in thread.orch._session_tasks
