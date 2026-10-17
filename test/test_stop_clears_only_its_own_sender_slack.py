"""Slack's Stop surfaces clear the PRESSER's queued messages, not the whole thread's.

Every member of a Slack channel thread shares that thread's session key, so a whole-queue
clear there discards other people's messages without any unified ``dm_scope``. Slack's
``!stop`` is inlined in the event router rather than a transport dispatcher, and the Stop
button, the inline Stop and the handler's ``!stop`` each reach the queue on their own path,
so each is driven here against a session surface holding a real queue: the presser's
entries and their temp files go, the other member's entries and attachments survive, an
escalated stop that hard-resets the session still takes the whole queue, and the preserved
entry drains once the stop has settled -- never while it is in flight, and oldest first
when two stops overlap. The queue, receipt and call-site layers every channel shares are
pinned in ``test_stop_clears_only_its_own_sender.py``.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any, Iterator
from unittest.mock import AsyncMock

import pytest

from kiro_crew.messaging.link import canonical_key
from kiro_crew.messaging.queue_drain import entry_owner
from kiro_crew.slack import events as slack_events

# ── Slack's inlined !stop ───────────────────────────────────────────────────

#: Two allow-listed members of one Slack channel thread. No unified scope is needed
#: for them to share a queue: a thread IS the session key, for everybody in it.
SLACK_ALICE = slack_events._entry_owner("U_ALICE", "C1")
SLACK_BOB = slack_events._entry_owner("U_BOB", "C1")

#: The thread both members are typing in, which is also the session key.
_THREAD = "100.0"


@pytest.fixture()
def slack_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[pathlib.Path]:
    """Point every home-derived path at ``tmp_path`` so the router touches no real HOME.

    Also forgets the module's tracked background tasks this test's loop left behind: a
    held dispatch cancelled at loop teardown chains one more drain onto a closing loop,
    and a later test that gathers ``_bg_tasks`` would otherwise await it on its own.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".kiro" / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / ".kiro"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    before = set(slack_events._bg_tasks)
    yield tmp_path
    slack_events._bg_tasks.intersection_update(before)


def _slack_orch() -> Any:
    """The orchestrator surface ``_route_message`` touches for a busy channel thread.

    ``clear_queue`` is recorded rather than run so the test can hand the predicate it
    received to the REAL manager queue below; the pre-session queue is real.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.config.loader import (
        ACTIVATION_ALWAYS,
        ChannelConfig,
        KiroCrewConfig,
        MessagingConfig,
    )

    orch = MagicMock()
    orch._cfg = KiroCrewConfig(
        slack_channels={"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)},
        messaging=MessagingConfig(),
    )
    orch.slack_command = "kirocrew"
    orch._owner_id = "U_ALICE"
    orch._allowed_users = {"U_ALICE", "U_BOB"}
    orch._tracking_channels = set()
    orch._open_channels = set()
    orch._approval_mode = ""
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = AsyncMock()
    orch.slack.record_channel_team = MagicMock()
    orch.slack.ensure_channel_team = AsyncMock()
    orch.slack.get_user_info = AsyncMock(return_value={})
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=True)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.has_session = MagicMock(return_value=True)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.cancel_queued = MagicMock(return_value=False)
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch.dashboard_state = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


def _slack_event(user: str, text: str, ts: str, **over: object) -> dict:
    base = {
        "user": user,
        "channel": "C1",
        "text": text,
        "ts": ts,
        "thread_ts": _THREAD,
        "team": "T1",
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def _pending_entry(user: str, ts: str, temp: pathlib.Path) -> tuple[str, str, dict]:
    """A pre-session entry the way the Slack stash records it, holding one temp file."""
    temp.write_bytes(b"x")
    return (
        ts,
        f"{user} asked",
        dict(
            channel="C1",
            thread_ts=_THREAD,
            sender_id=user,
            image_temp_paths=[str(temp)],
            **slack_events._queue_tags(user, "C1"),
        ),
    )


async def _route(orch: Any, event: dict) -> None:
    from unittest.mock import MagicMock, patch

    with patch("kiro_crew.slack.events.sel", return_value=MagicMock()):
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=False):
                await slack_events._route_message(orch, event, slack_events.SeenCache())


class _NeverDone:
    """A registered turn task that is still running when the stop arrives."""

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class _SlackSessions:
    """The session surface Slack's ``!stop`` and drain touch, holding a REAL queue.

    ``detach_queue``, ``restore_queue``, ``clear_queue`` and ``stop_turn`` follow the
    manager's contracts where they matter here: a clear with no predicate empties the
    queue and unlinks every entry's temp files, ``only=`` unlinks exactly the handles it
    is given, and ``stop_turn`` makes the whole-queue clear itself unless the caller says
    ``preserve_queue``. ``during_stop`` runs inside ``stop_turn`` so a test can end the
    running turn, or admit a message, while the stop is in flight.
    """

    def __init__(self, entries: list[tuple[str, str, dict]], outcome: str = "soft") -> None:
        from collections import deque

        self.queue = deque(entries)
        self.outcome = outcome
        self.stop_calls: list[dict[str, Any]] = []
        self.during_stop: Any = None

    def has_session(self, key: str) -> bool:
        return key == _THREAD

    def get_session_for_thread(self, key: str) -> str | None:
        return None

    def is_busy(self, key: str) -> bool:
        return True

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        return False

    def enqueue(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def detach_queue(self, key: str) -> tuple[Any, ...]:
        taken = tuple(self.queue)
        self.queue.clear()
        return taken

    def restore_queue(self, key: str, entries: tuple[Any, ...]) -> None:
        self.queue.extendleft(reversed(entries))

    def clear_queue(self, key: str, owned_by: Any = None, *, only: Any = None) -> None:
        if only is not None:
            dropped = list(only)
        else:
            dropped = [item for item in self.queue if owned_by is None or owned_by(item[2])]
        for _, _, kwargs in dropped:
            for path in kwargs.get("image_temp_paths", ()):
                pathlib.Path(path).unlink(missing_ok=True)
        wanted = {id(item) for item in dropped}
        kept = [item for item in self.queue if id(item) not in wanted]
        self.queue.clear()
        self.queue.extend(kept)

    def dequeue(self, key: str) -> tuple[str, str, dict] | None:
        return self.queue.popleft() if self.queue else None

    async def stop_turn(self, key: str, *, preserve_queue: bool = False, **hooks: Any) -> str:
        self.stop_calls.append({"key": key, "preserve_queue": preserve_queue})
        if self.during_stop is not None:
            await self.during_stop()
        if not preserve_queue:
            self.clear_queue(key)
        on_soft = hooks.get("on_soft")
        if on_soft is not None and self.outcome == "soft":
            await on_soft()
        return self.outcome


class _Dispatches:
    """``_dispatch_queued`` that records each dispatch and never finishes it.

    A dispatched turn that stays running holds the key, so exactly one drain is
    observed: the first, which is the one under test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict]] = []
        self._never = asyncio.Event()
        #: What was still queued, and still on disk, once the stop and its drain
        #: settled -- read before the loop's teardown cancels the held dispatch and
        #: lets the chain drain on.
        self.queued_after: list[str] = []
        self.pending_after: list[str] = []
        self.files_after: dict[str, bool] = {}

    def snapshot(self, orch: Any, *paths: pathlib.Path) -> None:
        self.queued_after = [item[0] for item in orch.sessions.queue]
        self.pending_after = [item[0] for item in orch._pending_queue.get(_THREAD, ())]
        self.files_after = {path.name: path.exists() for path in paths}

    async def __call__(self, _orch: Any, key: str, ts: str, text: str, kwargs: dict) -> None:
        self.calls.append((key, ts, text, kwargs))
        # flake-ok: the held dispatch never finishes by design; asyncio.run cancels it at teardown, moved verbatim
        await self._never.wait()


async def _settle() -> None:
    for _ in range(5):
        # flake-ok: pre-existing zero-delay yield barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)


def _stop_by_bob(
    slack_home: pathlib.Path,
    *,
    registered: bool = True,
    outcome: str = "soft",
    pending: bool = False,
) -> tuple[Any, _SlackSessions, _Dispatches, tuple[pathlib.Path, pathlib.Path]]:
    """Alice and Bob each queued one message; Bob sends ``!stop``.

    Run to completion, background drains included, with ``_dispatch_queued`` recorded.
    """
    from unittest.mock import patch

    alice_temp, bob_temp = slack_home / "alice.png", slack_home / "bob.png"
    alice = _pending_entry("U_ALICE", "101.0", alice_temp)
    bob = _pending_entry("U_BOB", "102.0", bob_temp)
    orch = _slack_orch()
    sessions = _SlackSessions([] if pending else [alice, bob], outcome=outcome)
    orch.sessions = sessions
    if pending:
        orch._pending_queue = {_THREAD: [alice, bob]}
    dispatches = _Dispatches()

    async def run() -> None:
        if registered:
            orch._session_tasks = {_THREAD: _NeverDone()}
        with patch("kiro_crew.slack.events._dispatch_queued", new=dispatches):
            await _route(orch, _slack_event("U_BOB", "!stop", "103.0"))
            # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
            await _settle()
            dispatches.snapshot(orch, alice_temp, bob_temp)

    asyncio.run(run())
    return orch, sessions, dispatches, (alice_temp, bob_temp)


class TestSlacksStopIsTheCallersToo:
    """One member's ``!stop`` in a shared thread leaves the other member's messages."""

    def test_alices_entry_and_her_attachment_outlive_the_stop(
        self, slack_home: pathlib.Path
    ) -> None:
        _, sessions, dispatches, _ = _stop_by_bob(slack_home)
        assert [call[1] for call in dispatches.calls] == ["101.0"], "hers runs; Bob's never"
        assert dispatches.files_after == {"alice.png": True, "bob.png": False}
        assert sessions.stop_calls == [{"key": _THREAD, "preserve_queue": True}]

    def test_the_pre_session_queue_keeps_the_others_entries_and_their_files(
        self, slack_home: pathlib.Path
    ) -> None:
        _, _, dispatches, _ = _stop_by_bob(slack_home, pending=True)
        assert [call[1] for call in dispatches.calls] == ["101.0"]
        assert dispatches.pending_after == []
        assert dispatches.files_after == {"alice.png": True, "bob.png": False}

    def test_the_callers_own_entries_are_all_dropped(self, slack_home: pathlib.Path) -> None:
        """Bob queued twice; his stop takes both, and the empty list is removed."""
        first, second = slack_home / "one.png", slack_home / "two.png"
        orch = _slack_orch()
        orch.sessions = _SlackSessions([])
        orch._session_tasks = {_THREAD: _NeverDone()}
        orch._pending_queue = {
            _THREAD: [
                _pending_entry("U_BOB", "101.0", first),
                _pending_entry("U_BOB", "102.0", second),
            ]
        }
        asyncio.run(_route(orch, _slack_event("U_BOB", "!stop", "103.0")))

        assert _THREAD not in orch._pending_queue
        assert not first.exists() and not second.exists()

    def test_an_entry_that_named_no_owner_survives_a_slack_stop(
        self, slack_home: pathlib.Path
    ) -> None:
        """A dashboard-linked session's own entries are nobody's to drop from Slack."""
        from unittest.mock import patch

        orch = _slack_orch()
        orch.sessions = _SlackSessions([("101.0", "untagged", {"channel": "C1"})])
        dispatches = _Dispatches()

        async def run() -> None:
            orch._session_tasks = {_THREAD: _NeverDone()}
            with patch("kiro_crew.slack.events._dispatch_queued", new=dispatches):
                await _route(orch, _slack_event("U_BOB", "!stop", "103.0"))
                # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                await _settle()

        asyncio.run(run())
        assert [call[2] for call in dispatches.calls] == ["untagged"]

    def test_an_escalated_stop_still_drops_everybodys_entries(
        self, slack_home: pathlib.Path
    ) -> None:
        """The documented carve-out: a stop that hard-resets the session takes its queue."""
        _, _, dispatches, _ = _stop_by_bob(slack_home, outcome="hard")
        assert not dispatches.calls and not dispatches.queued_after
        assert dispatches.files_after == {"alice.png": False, "bob.png": False}

    def test_both_enqueue_sites_and_the_stash_record_the_sender(
        self, slack_home: pathlib.Path
    ) -> None:
        """Tagging comes first: a scoped clear over untagged entries drops nothing."""
        # The session-level queue while a task is running.
        orch = _slack_orch()
        orch._session_tasks = {_THREAD: _NeverDone()}
        orch.sessions.enqueue.return_value = True
        asyncio.run(_route(orch, _slack_event("U_ALICE", "hello", "101.0")))
        assert entry_owner(orch.sessions.enqueue.call_args.kwargs) == SLACK_ALICE

        # The pre-session stash when the session object does not exist yet.
        orch = _slack_orch()
        orch._session_tasks = {_THREAD: _NeverDone()}
        asyncio.run(_route(orch, _slack_event("U_ALICE", "hello", "101.0")))
        assert entry_owner(orch._pending_queue[_THREAD][0][2]) == SLACK_ALICE

        # The session-level queue when no task is registered but the session is busy.
        orch = _slack_orch()
        orch.sessions.enqueue.return_value = True
        asyncio.run(_route(orch, _slack_event("U_BOB", "hello", "102.0")))
        assert entry_owner(orch.sessions.enqueue.call_args.kwargs) == SLACK_BOB

    def test_a_root_and_a_threaded_message_from_one_dm_user_share_an_owner(
        self, slack_home: pathlib.Path
    ) -> None:
        """A single-session DM merges its threads into one key, so its owner must too.

        Otherwise a ``!stop`` typed at channel root would leave the same person's
        threaded messages queued in the one conversation the flat key exists to merge.
        """
        orch = _slack_orch()
        orch.sessions.enqueue.return_value = True
        asyncio.run(_route(orch, _slack_event("U_ALICE", "hello", "101.0", channel="D1")))
        threaded = entry_owner(orch.sessions.enqueue.call_args.kwargs)
        asyncio.run(
            _route(orch, _slack_event("U_ALICE", "hello", "102.0", channel="D1", thread_ts=None))
        )
        assert entry_owner(orch.sessions.enqueue.call_args.kwargs) == threaded


class TestThePreservedEntryDrainsOnlyOnceTheStopIsDone:
    """After Bob's stop, Alice's message is the one that runs next -- and not before."""

    def test_the_next_drain_dispatches_alices_message(self, slack_home: pathlib.Path) -> None:
        _, sessions, dispatches, _ = _stop_by_bob(slack_home)
        assert len(dispatches.calls) == 1
        key, ts, text, kwargs = dispatches.calls[0]
        assert (key, ts, text) == (_THREAD, "101.0", "U_ALICE asked")
        assert kwargs["sender_id"] == "U_ALICE"
        assert dispatches.queued_after == []

    def test_a_turn_no_route_registered_still_has_its_queue_drained(
        self, slack_home: pathlib.Path
    ) -> None:
        """A turn started outside ``_route_message`` (an interaction's ``handle_message``)
        leaves no ``_session_tasks`` entry and so no turn-end drain; the stop drains."""
        _, _, dispatches, _ = _stop_by_bob(slack_home, registered=False)
        assert [call[1] for call in dispatches.calls] == ["101.0"]
        assert dispatches.queued_after == []

    def test_a_turn_ending_mid_stop_does_not_drain_until_the_stop_returns(
        self, slack_home: pathlib.Path
    ) -> None:
        """The running turn finishes while the stop is in flight, with a message waiting.

        Its own turn-end drain (``_on_done``) must not start that message, or
        ``stop_turn`` would cancel the turn it just started and the dequeued message
        would be lost. Once the stop returns, Alice's preserved entry drains first.
        """
        from unittest.mock import patch

        alice = _pending_entry("U_ALICE", "101.0", slack_home / "alice.png")
        bob = _pending_entry("U_BOB", "102.0", slack_home / "bob.png")
        later = _pending_entry("U_ALICE", "104.0", slack_home / "later.png")
        orch = _slack_orch()
        orch._cfg.messaging.use_transport = False
        sessions = _SlackSessions([])
        orch.sessions = sessions
        dispatches = _Dispatches()
        seen_during_stop: list[int] = []

        async def run() -> None:
            turn_may_end = asyncio.Event()

            async def _turn(*_a: Any, **_kw: Any) -> None:
                # flake-ok: pre-existing unbounded Event.wait that the test sets on its own path, moved verbatim; tracked for a de-flake pass
                await turn_may_end.wait()

            with (
                patch("kiro_crew.slack.events.handle_message", new=_turn),
                patch("kiro_crew.slack.events._dispatch_queued", new=dispatches),
            ):
                await _route(orch, _slack_event("U_ALICE", "first", "100.5"))
                turn = orch._session_tasks[_THREAD]
                sessions.queue.extend([alice, bob])

                async def _turn_ends_mid_stop() -> None:
                    sessions.queue.append(later)  # admitted while the stop is in flight
                    turn_may_end.set()
                    # flake-ok: pre-existing unbounded await of a task the test has released, moved verbatim; tracked for a de-flake pass
                    await turn
                    # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                    await _settle()  # the turn's _on_done has run by now
                    seen_during_stop.append(len(dispatches.calls))

                sessions.during_stop = _turn_ends_mid_stop
                await _route(orch, _slack_event("U_BOB", "!stop", "103.0"))
                # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                await _settle()
                dispatches.snapshot(orch)

        asyncio.run(run())
        assert seen_during_stop == [0], "nothing may start while the stop is in flight"
        assert [call[1] for call in dispatches.calls] == ["101.0"]
        assert dispatches.queued_after == ["104.0"]

    def test_overlapping_stops_hold_the_drain_until_the_last_one_settles(
        self, slack_home: pathlib.Path
    ) -> None:
        """A second ``!stop`` lands, and finishes, while the first is still in flight.

        A message Alice queues between them must not be started when the SECOND stop
        settles: the first one's ``stop_turn`` is still to cancel whatever turn is
        running. It is dispatched once, after the first stop too has settled.
        """
        from unittest.mock import patch

        later = _pending_entry("U_ALICE", "104.0", slack_home / "later.png")
        orch = _slack_orch()
        sessions = _SlackSessions([])
        orch.sessions = sessions
        dispatches = _Dispatches()
        seen_after_second: list[int] = []

        async def run() -> None:
            orch._session_tasks = {_THREAD: _NeverDone()}
            with patch("kiro_crew.slack.events._dispatch_queued", new=dispatches):
                inner: list[bool] = []

                async def _second_stop_inside_the_first() -> None:
                    if inner:
                        return
                    inner.append(True)
                    sessions.queue.append(later)  # Alice queues between the two stops
                    await _route(orch, _slack_event("U_BOB", "!stop", "105.0"))
                    # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                    await _settle()
                    seen_after_second.append(len(dispatches.calls))

                sessions.during_stop = _second_stop_inside_the_first
                await _route(orch, _slack_event("U_BOB", "!stop", "103.0"))
                # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                await _settle()
                dispatches.snapshot(orch, slack_home / "later.png")

        asyncio.run(run())
        assert seen_after_second == [0], "the first stop is still in flight"
        assert [call[1] for call in dispatches.calls] == ["104.0"]
        assert dispatches.files_after == {"later.png": True}

    def test_the_running_turn_stays_the_registered_task_during_a_stop(
        self, slack_home: pathlib.Path
    ) -> None:
        """The hold lives beside ``_session_tasks``, not in it: anything reading that
        map mid-stop (the Stop button, the in-flight count) still sees the real turn."""
        turn = _NeverDone()
        orch = _slack_orch()
        sessions = _SlackSessions([])
        orch.sessions = sessions
        seen: list[Any] = []

        async def _look() -> None:
            seen.append(orch._session_tasks.get(_THREAD))

        sessions.during_stop = _look

        async def run() -> None:
            orch._session_tasks = {_THREAD: turn}
            await _route(orch, _slack_event("U_BOB", "!stop", "103.0"))

        asyncio.run(run())
        assert seen == [turn]

    def test_two_members_overlapping_stops_keep_the_survivors_order_and_drop_both(
        self, slack_home: pathlib.Path
    ) -> None:
        """Bob stops; Dave stops while Bob's is in flight and finishes AFTER it.

        Alice queued one message before Bob's press and one between the two presses, so
        each stop detached one of hers. Restored in settle order, Dave's snapshot would go
        back ahead of Bob's and her messages would run newest first. Both pressers' own
        entries -- including Dave's, which Bob's stop detached -- are dropped.
        """
        from unittest.mock import patch

        files = {name: slack_home / f"{name}.png" for name in ("a1", "b1", "d1", "a2", "d2")}
        a1 = _pending_entry("U_ALICE", "101.0", files["a1"])
        b1 = _pending_entry("U_BOB", "102.0", files["b1"])
        d1 = _pending_entry("U_DAVE", "103.0", files["d1"])
        a2 = _pending_entry("U_ALICE", "106.0", files["a2"])
        d2 = _pending_entry("U_DAVE", "107.0", files["d2"])
        orch = _slack_orch()
        orch._allowed_users = {"U_ALICE", "U_BOB", "U_DAVE"}
        sessions = _SlackSessions([a1, b1, d1])
        orch.sessions = sessions
        dispatched: list[str] = []
        seen: dict[str, Any] = {}

        async def _dispatch(_orch: Any, key: str, ts: str, text: str, kwargs: dict) -> None:
            dispatched.append(ts)

        async def run() -> None:
            orch._session_tasks = {_THREAD: _NeverDone()}
            dave_in_stop, dave_may_finish = asyncio.Event(), asyncio.Event()
            calls: list[str] = []

            async def _during() -> None:
                calls.append("stop")
                if len(calls) == 1:  # Bob's
                    sessions.queue.extend([a2, d2])
                    dave = asyncio.ensure_future(
                        _route(orch, _slack_event("U_DAVE", "!stop", "108.0"))
                    )
                    # flake-ok: pre-existing unbounded Event.wait that the test sets on its own path, moved verbatim; tracked for a de-flake pass
                    await dave_in_stop.wait()
                    seen["dave"] = dave
                else:  # Dave's: held until Bob's has settled
                    dave_in_stop.set()
                    # flake-ok: pre-existing unbounded Event.wait that the test sets on its own path, moved verbatim; tracked for a de-flake pass
                    await dave_may_finish.wait()

            sessions.during_stop = _during
            with patch("kiro_crew.slack.events._dispatch_queued", new=_dispatch):
                await _route(orch, _slack_event("U_BOB", "!stop", "105.0"))
                # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                await _settle()
                assert dispatched == [], "Dave's stop is still in flight"
                dave_may_finish.set()
                # flake-ok: pre-existing unbounded await of a task the test has released, moved verbatim; tracked for a de-flake pass
                await seen["dave"]
                for _ in range(10):
                    # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                    await _settle()
            seen["files"] = {name: path.exists() for name, path in files.items()}

        asyncio.run(run())
        assert dispatched == ["101.0", "106.0"], "Alice's, oldest first"
        assert not sessions.queue
        assert seen["files"] == {
            "a1": True,
            "a2": True,
            "b1": False,
            "d1": False,
            "d2": False,
        }

    def test_a_message_sent_after_ones_own_stop_survives_an_overlapping_stop(
        self, slack_home: pathlib.Path
    ) -> None:
        """Bob stops, then sends a new message; Alice's overlapping stop detaches it.

        Bob's stop withdraws what Bob had queued when he pressed it, not what he sent
        afterwards: the newer message is restored and drains, its attachment intact.
        """
        from unittest.mock import patch

        old, new = slack_home / "old.png", slack_home / "new.png"
        b1 = _pending_entry("U_BOB", "101.0", old)
        b2 = _pending_entry("U_BOB", "106.0", new)
        orch = _slack_orch()
        sessions = _SlackSessions([b1])
        orch.sessions = sessions
        dispatched: list[str] = []
        seen: dict[str, Any] = {}

        async def _dispatch(_orch: Any, key: str, ts: str, text: str, kwargs: dict) -> None:
            dispatched.append(ts)

        async def run() -> None:
            orch._session_tasks = {_THREAD: _NeverDone()}
            alice_in_stop, alice_may_finish = asyncio.Event(), asyncio.Event()
            calls: list[str] = []

            async def _during() -> None:
                calls.append("stop")
                if len(calls) == 1:  # Bob's
                    sessions.queue.append(b2)  # Bob's message after his own !stop
                    seen["alice"] = asyncio.ensure_future(
                        _route(orch, _slack_event("U_ALICE", "!stop", "107.0"))
                    )
                    # flake-ok: pre-existing unbounded Event.wait that the test sets on its own path, moved verbatim; tracked for a de-flake pass
                    await alice_in_stop.wait()
                else:  # Alice's: held until Bob's has settled
                    alice_in_stop.set()
                    # flake-ok: pre-existing unbounded Event.wait that the test sets on its own path, moved verbatim; tracked for a de-flake pass
                    await alice_may_finish.wait()

            sessions.during_stop = _during
            with patch("kiro_crew.slack.events._dispatch_queued", new=_dispatch):
                await _route(orch, _slack_event("U_BOB", "!stop", "105.0"))
                # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                await _settle()
                alice_may_finish.set()
                # flake-ok: pre-existing unbounded await of a task the test has released, moved verbatim; tracked for a de-flake pass
                await seen["alice"]
                for _ in range(10):
                    # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                    await _settle()
            seen["files"] = {"old": old.exists(), "new": new.exists()}

        asyncio.run(run())
        assert dispatched == ["106.0"], "only the message Bob sent after his stop runs"
        assert seen["files"] == {"old": False, "new": True}

    def test_a_stop_that_raises_after_its_outcome_still_releases_the_key(
        self, slack_home: pathlib.Path
    ) -> None:
        """The hold is released on every path: a failure in the hard-outcome branch
        must not leave the key busy, or the thread's queue would never drain again."""
        from unittest.mock import patch

        class _ClearFails(_SlackSessions):
            def clear_queue(self, key: str, owned_by: Any = None, *, only: Any = None) -> None:
                raise RuntimeError("clear failed")

        orch = _slack_orch()
        sessions = _ClearFails([], outcome="hard")
        orch.sessions = sessions
        dispatches = _Dispatches()

        async def run() -> None:
            orch._session_tasks = {_THREAD: _NeverDone()}
            with pytest.raises(RuntimeError):
                await _route(orch, _slack_event("U_BOB", "!stop", "103.0"))
            assert not slack_events._key_busy(orch, _THREAD)
            sessions.queue.append(_pending_entry("U_ALICE", "104.0", slack_home / "a.png"))
            with patch("kiro_crew.slack.events._dispatch_queued", new=dispatches):
                await slack_events._drain_slack_queue(orch, _THREAD)
                # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
                await _settle()

        asyncio.run(run())
        assert [call[1] for call in dispatches.calls] == ["104.0"]


# ── Slack's other Stop entry points ─────────────────────────────────────────


def _real_manager_with_two_members_queued() -> tuple[Any, Any]:
    """A REAL ``SessionManager`` queue holding one Alice and one Bob message.

    The provider acks the cancel, so ``stop_turn`` takes its soft path and its own
    queue clear is the only thing that touches the queue.
    """
    from unittest.mock import MagicMock

    from kiro_crew.session import SessionManager, _Session

    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {}
    mgr._lock = asyncio.Lock()
    mgr._background_tasks = set()
    cfg = MagicMock()
    cfg.agent.soft_stop_budget_secs = 5.0
    mgr._cfg = cfg
    # No dashboard link: the thread is its own session.
    mgr._session_map = MagicMock()
    mgr._session_map.get_session_for_thread.return_value = None
    provider = AsyncMock()
    provider.cancel = AsyncMock(return_value="acked")
    session = _Session(provider=provider)
    mgr._sessions[_THREAD] = session
    for user, ts in (("U_ALICE", "101.0"), ("U_BOB", "102.0")):
        mgr.enqueue(
            _THREAD, ts, f"{user} asked", force=True, **slack_events._queue_tags(user, "C1")
        )
    return mgr, session


def _press(handler: str, mgr: Any) -> tuple[list[str], list[int]]:
    """Bob presses the Stop surface *handler* names, on the shared thread.

    While the cancel is in flight the running turn ends, and its turn-end drain
    (``_drain_slack_queue``, what ``_on_done`` runs) is tried. Returns the
    timestamps dispatched once everything settled, and how many had been
    dispatched by the time that mid-stop drain returned.
    """
    from unittest.mock import MagicMock, patch

    from kiro_crew.slack import handler as slack_handler
    from kiro_crew.slack import interactions

    orch = MagicMock()
    orch.sessions = mgr
    orch.slack = AsyncMock()
    # The handler's ``!stop`` is itself the registered turn on the key; a button
    # press arrives beside whatever turn is running.
    orch._session_tasks = {_THREAD: _NeverDone()} if handler == "bang" else {}
    orch._handler_tasks = set()
    orch._pending_queue = {}
    dispatched: list[str] = []
    seen_mid_stop: list[int] = []

    async def _dispatch(_orch: Any, key: str, ts: str, text: str, kwargs: dict) -> None:
        dispatched.append(ts)

    async def _turn_ends_during_cancel(**_kw: Any) -> str:
        await slack_events._drain_slack_queue(orch, _THREAD)
        # The session layer's wake names the canonical key.
        await slack_events._drain_slack_queue(orch, canonical_key(_THREAD))
        # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
        await _settle()
        seen_mid_stop.append(len(dispatched))
        return "acked"

    mgr._sessions[_THREAD].provider.cancel = AsyncMock(side_effect=_turn_ends_during_cancel)

    async def go() -> None:
        with (
            patch.object(interactions, "_orch", orch),
            patch.object(interactions, "is_allowed_user", return_value=True),
            patch.object(interactions, "is_owner", return_value=True),
            patch.object(interactions, "sel", return_value=MagicMock()),
            patch.object(slack_handler, "sel", return_value=MagicMock()),
            patch.object(slack_events, "_dispatch_queued", new=_dispatch),
        ):
            if handler == "confirm":
                payload = {"message": {"thread_ts": _THREAD}, "response_url": ""}
                await interactions._handle_stop_confirm(payload, "C1", "m1", "U_BOB")
            elif handler in ("inline", "inline-canonical"):
                # The working message's button can carry ``slack:<ts>`` while
                # the router keys the turn by the bare ts.
                value = canonical_key(_THREAD) if handler == "inline-canonical" else _THREAD
                action = {"value": value}
                await interactions._handle_inline_stop({}, action, "C1", "m1", "U_BOB")
            else:
                await slack_handler._bang_stop(
                    "!stop", orch.slack, mgr, "C1", _THREAD, "103.0", _THREAD, "U_BOB", None
                )
            # flake-ok: pre-existing fixed-yield barrier, moved verbatim; tracked for a de-flake pass
            await _settle()

    asyncio.run(go())
    return dispatched, seen_mid_stop


class TestSlacksOtherStopSurfacesAreTheCallersToo:
    """The Stop button, the inline Stop and the handler's ``!stop`` drop only Bob's."""

    @pytest.mark.parametrize("handler", ["confirm", "inline"])
    def test_bobs_button_stop_keeps_alices_message_and_it_runs_next(self, handler: str) -> None:
        mgr, session = _real_manager_with_two_members_queued()
        dispatched, _ = _press(handler, mgr)
        assert dispatched == ["101.0"], "Alice's message runs; Bob's never does"
        assert not session.queue
        session.provider.cancel.assert_awaited_once()

    def test_bobs_handler_stop_leaves_alices_message_queued(self) -> None:
        """The handler's ``!stop`` runs as a registered turn, whose own end drains."""
        mgr, session = _real_manager_with_two_members_queued()
        _press("bang", mgr)
        assert [item[1] for item in session.queue] == ["U_ALICE asked"]
        session.provider.cancel.assert_awaited_once()

    @pytest.mark.parametrize("handler", ["confirm", "inline", "inline-canonical"])
    def test_a_turn_ending_mid_stop_starts_nothing_until_the_stop_settles(
        self, handler: str
    ) -> None:
        """Started mid-stop, Alice's message would be aborted if the stop escalated."""
        mgr, _ = _real_manager_with_two_members_queued()
        dispatched, seen_mid_stop = _press(handler, mgr)
        assert seen_mid_stop == [0], "the key reads busy while the stop is in flight"
        assert dispatched == ["101.0"], "and the release drains it once"
