"""``ctx.chat_slots``: an app's own dashboard chat slots, driven through one seam.

The SDK is the public door to the gateway functions behind a chat slot an app
owns: create and restore it, start or queue a turn, reserve it, stop a turn,
archive it, read its transcript, and keep an AutoNudge loop on it. What these
tests pin is what an app must not choose: which slots it may act on (its own,
stamped with its name, under its key prefix), the provenance a turn carries into
the chat runner, the containment stamp and redaction a queued message gets, the
order a stop or an archive runs in, and which AutoNudge loops it can see.

Every gateway function is replaced by a recorder at the module the SDK reads it
from; no real slot, runner or loop runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps import chat_slot_sdk
from kiro_crew.apps.chat_slot_sdk import QUEUED_TEXT_WITHHELD, ChatSlotRefused, ChatSlotSDK
from kiro_crew.apps.context import build_app_context

APP = "slot-probe"
KEY = f"{APP}-job-1"
#: Names a hang only; every awaited call here finishes on its own signal.
_BOUND = 30.0


class _Slot:
    def __init__(self, key: str = KEY, app: str | None = APP) -> None:
        self.key = key
        if app is not None:
            self._app = app
        self.task: asyncio.Task[Any] | None = None
        self.messages: list[dict[str, Any]] = []
        self.rows: list[tuple[str, str, dict[str, Any] | None]] = []
        self.queued: list[tuple[str, dict[str, Any], bool]] = []
        self._queue: list[Any] = []
        self._pending_steers: list[Any] = []
        self._pending_synthesis = True
        self._deferred_notes: list[Any] = []
        self.title = ""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def append(self, role: str, content: str, *, meta: dict[str, Any] | None = None) -> None:
        self.rows.append((role, content, meta))

    def queue_append(self, message: str, *, meta: dict[str, Any], directive_user_origin: bool):
        self.queued.append((message, meta, directive_user_origin))
        self._queue.append(message)


class _Sessions:
    def __init__(self) -> None:
        self.stops: list[tuple[str, bool]] = []

    async def stop_turn(self, key: str, *, force: bool) -> None:
        self.stops.append((key, force))


class _Log:
    """A saved conversation log: rows and the owner each transcript records."""

    def __init__(self, rows: list[dict[str, Any]], owners: dict[str, str] | None = None) -> None:
        self.rows = rows
        self.owners = owners or {}
        self.read: list[str] = []

    def read_messages(self, key: str) -> list[dict[str, Any]]:
        self.read.append(key)
        return self.rows

    def get_metadata(self, key: str) -> dict[str, Any]:
        return {"app": self.owners[key]} if key in self.owners else {}


class _State:
    def __init__(self, *slots: _Slot) -> None:
        self._slots = {slot.key: slot for slot in slots}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.sessions = _Sessions()
        self.conversation_log: _Log | None = None
        self.pushes = 0
        self.titles: list[tuple[str, str]] = []
        self.created: list[tuple[str, str]] = []

    def get_slot(self, key: str) -> _Slot | None:
        return self._slots.get(key)

    def get_or_create_slot(self, *, name: str, app: str = "") -> _Slot:
        self.created.append((name, app))
        slot = self._slots.setdefault(name, _Slot(name, app or None))
        return slot

    def push_slots_update(self) -> None:
        self.pushes += 1

    def push_slot_title(self, key: str, title: str) -> None:
        self.titles.append((key, title))


def _sdk() -> ChatSlotSDK:
    return ChatSlotSDK(APP)


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, _BOUND))


@pytest.fixture
def runner(monkeypatch):
    """A recording ``_run_chat`` and guarded turn start; the turn returns at once."""
    calls: list[dict[str, Any]] = []
    spawned: list[tuple[Any, Any]] = []

    async def _turn(state, slot, message, **kwargs):
        calls.append({"slot": slot, "message": message, **kwargs})

    def _run_chat(state, slot, message, **kwargs):
        return _turn(state, slot, message, **kwargs)

    def _spawn(state, slot, coro):
        spawned.append((state, slot))
        task = asyncio.create_task(coro)
        state._background_tasks.add(task)
        return task

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.turn_dispatch.spawn_guarded_turn", _spawn)
    return SimpleNamespace(calls=calls, spawned=spawned)


# ── presence and identity ───────────────────────────────────────────────────


@pytest.mark.parametrize(("declared", "present"), [(True, True), ("true", False), (None, False)])
def test_the_seam_is_present_only_for_a_literal_true_declaration(tmp_path, declared, present):
    permissions = {} if declared is None else {"chatSlots": declared}
    ctx = build_app_context(APP, tmp_path, permissions=permissions)
    assert (ctx.chat_slots is not None) is present
    if present:
        assert ctx.chat_slots.app_name == APP


def test_the_manifest_round_trips_the_declaration():
    from kiro_crew.apps.manifest_sections import Permissions

    assert Permissions.from_dict({"chatSlots": True}).to_dict() == {"chatSlots": True}
    assert Permissions.from_dict({"chatSlots": "true"}).chatSlots is False


@pytest.mark.parametrize(
    "key", ["", "slot-probe", "slot-probe-", "other-job-1", "slot-probe-a/b", "slot-probex-1", 7]
)
def test_a_key_outside_the_apps_prefix_is_refused(key):
    with pytest.raises(ValueError):
        _sdk().check_key(key)
    assert _sdk().is_own_key(key) is False


def test_the_longest_accepted_key_is_kept_as_spelled_and_fits_every_file_name():
    """A folded key names another slot and transcript; a long one, a file name too long.

    The longest key the seam accepts is the registry's own folded length: the
    registry keeps it as spelled, and its transcript, ``.lock`` sidecar and archive
    segment names fit a 255-byte file name. One character more is refused.
    """
    from kiro_crew.dashboard.chat_utils import _history_key_for
    from kiro_crew.dashboard.state_parts.slot_directory import _normalize_slot_key
    from kiro_crew.history_files import ARCHIVE_SEGMENT_DELIMITER, _safe_key

    longest = f"{APP}-" + "a" * (chat_slot_sdk._max_key_chars() - len(APP) - 1)
    assert _sdk().check_key(longest) == longest == _normalize_slot_key(longest)
    stem = _safe_key(_history_key_for(longest))
    archive = f"{stem}{ARCHIVE_SEGMENT_DELIMITER}20261113-000000-1000.jsonl"
    for name in (f"{stem}.jsonl", f"{stem}.jsonl.lock", archive):
        assert len(name.encode()) <= 255, name
    longer = longest + "a"
    with pytest.raises(ValueError):
        _sdk().check_key(longer)
    assert _sdk().is_own_key(longer) is False


def test_a_slot_is_owned_only_by_the_app_whose_name_it_carries():
    sdk = _sdk()
    assert sdk.owns(_Slot()) is True
    assert sdk.owns(_Slot(app="other")) is False
    assert sdk.owns(_Slot(app=None)) is False
    assert sdk.owns(None) is False


@pytest.mark.parametrize("app", ["other", None])
def test_open_refuses_a_slot_someone_else_holds_under_the_key(app):
    foreign = _Slot(app=app)
    state = _State(foreign)
    with pytest.raises(ChatSlotRefused):
        _run(_sdk().open(state, KEY))
    assert getattr(foreign, "_app", None) == app
    assert state.created == []


def test_open_creates_a_slot_stamped_with_the_app_and_reuses_its_own():
    state = _State()
    slot, created = _run(_sdk().open(state, KEY))
    assert created is True and state.created == [(KEY, APP)] and slot._app == APP
    again, created_again = _run(_sdk().open(state, KEY))
    assert again is slot and created_again is False


def test_get_answers_only_the_apps_own_slot():
    state = _State(_Slot(), _Slot(f"{APP}-job-2", app="other"))
    assert _sdk().get(state, KEY) is state.get_slot(KEY)
    assert _sdk().get(state, f"{APP}-job-2") is None


def _saved_log(owners: dict[str, str]) -> _Log:
    """A log whose saved transcripts record *owners* (slot key -> app; "" a person's)."""
    from kiro_crew.dashboard.chat_utils import _history_key_for

    return _Log([], {_history_key_for(key): owner for key, owner in owners.items()})


def test_restore_returns_a_restored_slot_only_when_it_is_the_apps(monkeypatch):
    restored: list[tuple[str, bool]] = []
    answers = {KEY: _Slot()}

    async def _rehydrate(state, key, *, adopt_closed):
        restored.append((key, adopt_closed))
        return answers[key]

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async", _rehydrate
    )
    state = _State()
    state.conversation_log = _saved_log({KEY: APP, f"{APP}-job-2": "other"})
    assert _run(_sdk().restore(state, KEY, adopt_closed=True)) is answers[KEY]
    assert restored == [(KEY, True)]


@pytest.mark.parametrize("owner", ["other", "", None])
def test_restore_never_brings_back_a_transcript_the_app_does_not_own(monkeypatch, owner):
    """Another app's, a person's, or no transcript: nothing is rehydrated, nothing published."""

    async def _rehydrate(state, key, *, adopt_closed):
        raise AssertionError("a transcript this app does not own must not be rehydrated")

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async", _rehydrate
    )
    state = _State()
    state.conversation_log = _saved_log({} if owner is None else {KEY: owner})
    assert _run(_sdk().restore(state, KEY, adopt_closed=True)) is None
    assert state.get_slot(KEY) is None and state.pushes == 0


@pytest.mark.parametrize("owner", ["other", ""])
def test_open_refuses_a_key_whose_saved_transcript_is_someone_elses(owner):
    state = _State()
    state.conversation_log = _saved_log({KEY: owner})
    with pytest.raises(ChatSlotRefused):
        _run(_sdk().open(state, KEY))
    assert state.created == [] and state.get_slot(KEY) is None


def test_open_creates_a_slot_over_its_own_or_no_saved_transcript():
    state = _State()
    state.conversation_log = _saved_log({KEY: APP})
    slot, created = _run(_sdk().open(state, KEY))
    assert created is True and slot._app == APP
    other = f"{APP}-job-2"
    slot, created = _run(_sdk().open(state, other))
    assert created is True and state.created[-1] == (other, APP)


def test_set_title_names_the_slot_once_when_asked():
    slot = _Slot()
    state = _State(slot)
    assert _sdk().set_title(state, slot, "Job 1") is True
    assert _sdk().set_title(state, slot, "Renamed", only_untitled=True) is False
    assert slot.title == "Job 1" and state.titles == [(KEY, "Job 1")]


def test_a_title_marks_the_slot_for_the_periodic_flush():
    """The flush saves only a dirty slot, and an idle one would keep the title in memory only."""
    slot = _Slot()
    slot._dirty = False
    state = _State(slot)
    assert _sdk().set_title(state, slot, "Job 1") is True
    assert slot._dirty is True
    slot._dirty = False
    assert _sdk().set_title(state, slot, "Again", only_untitled=True) is False
    assert slot._dirty is False


def test_a_title_is_redacted_before_it_is_pushed_and_withheld_when_it_cannot_be(monkeypatch):
    secret = "ghp_" + "b" * 36
    slot = _Slot()
    state = _State(slot)
    assert _sdk().set_title(state, slot, f"Job {secret}") is True
    assert secret not in slot.title and slot.title.startswith("Job ")
    assert state.titles == [(KEY, slot.title)]

    def _broken(self, text):
        raise RuntimeError("policy did not compose")

    monkeypatch.setattr(chat_slot_sdk.ScrubSDK, "outbound", _broken)
    fresh = _Slot()
    fresh_state = _State(fresh)
    assert _sdk().set_title(fresh_state, fresh, "anything") is False
    assert fresh.title == "" and fresh_state.titles == []


# ── every slot operation refuses a slot that is not the app's ──────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda sdk, state, slot: sdk.send(state, slot, "hi"),
        lambda sdk, state, slot: sdk.reserve(state, slot),
        lambda sdk, state, slot: sdk.discard_queued(slot),
        lambda sdk, state, slot: sdk.set_title(state, slot, "x"),
        lambda sdk, state, slot: _run(sdk.stop_turn(state, slot)),
        lambda sdk, state, slot: _run(sdk.archive(state, slot)),
        lambda sdk, state, slot: _run(
            sdk.arm_nudge(state, slot, message="go", idle_secs=60, max_cycles=1)
        ),
    ],
)
@pytest.mark.parametrize("owner", ["other", None])
def test_a_slot_the_app_does_not_own_is_refused_untouched(call, owner):
    slot = _Slot(app=owner)
    state = _State(slot)
    with pytest.raises(ChatSlotRefused):
        call(_sdk(), state, slot)
    assert slot.rows == [] and slot.queued == [] and slot._pending_synthesis is True
    assert state.get_slot(KEY) is slot and state.pushes == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda sdk, state, slot: sdk.send(state, slot, "hi"),
        lambda sdk, state, slot: sdk.reserve(state, slot),
        lambda sdk, state, slot: sdk.set_title(state, slot, "x"),
        lambda sdk, state, slot: _run(sdk.stop_turn(state, slot)),
        lambda sdk, state, slot: _run(
            sdk.arm_nudge(state, slot, message="go", idle_secs=60, max_cycles=1)
        ),
    ],
)
def test_a_kept_slot_object_is_refused_once_another_slot_holds_its_key(call):
    """The gateway acts on these by key, and the key may now hold another app's slot.

    The kept object still carries this app's stamp; the slot now under its key is
    app ``slot-probe-tools``'s, whose name extends this app's.
    """
    stale = _Slot()
    replacement = _Slot(app="slot-probe-tools")
    state = _State(replacement)
    with pytest.raises(ChatSlotRefused):
        call(_sdk(), state, stale)
    assert replacement.rows == [] and replacement.queued == [] and replacement.title == ""
    assert state.get_slot(KEY) is replacement and state.pushes == 0 and state.titles == []
    assert state.sessions.stops == []


# ── turns ───────────────────────────────────────────────────────────────────


def test_send_starts_a_bounded_turn_with_the_callers_provenance(runner):
    slot = _Slot()
    state = _State(slot)
    consumed: list[bool] = []

    async def _go():
        task = _sdk().send(
            state,
            slot,
            "build it",
            meta={"files": ["a.txt"]},
            user_origin=True,
            typed=False,
            on_consumed=consumed.append,
        )
        assert task is not None and slot.task is task and task in state._background_tasks
        await task

    _run(_go())
    assert slot.rows == [("user", "build it", {"files": ["a.txt"]})]
    (call,) = runner.calls
    assert call["slot"] is slot and call["message"] == "build it"
    assert call["_directive_user_origin"] is True and call["_message_is_typed"] is False
    # The app's turn, as an app's own /api/chat send is: automation, never a person.
    assert call["_turn_actor"] == "app"
    assert call["_on_consumed"] == consumed.append
    # Started through the gateway's guarded start, as /api/chat starts a turn.
    assert runner.spawned == [(state, slot)] and state.pushes == 1


def test_a_sent_turn_runs_under_the_unattended_turn_cap(runner):
    """The operator's background-turn cap admits it, as it does an app's /api/chat turn."""
    slot = _Slot()
    state = _State(slot)
    capped: list[Any] = []

    async def _run_background_turn(slot_, coro):
        capped.append(slot_)
        return await coro

    state.run_background_turn = _run_background_turn  # type: ignore[attr-defined]

    async def _go():
        await _sdk().send(state, slot, "build it")

    _run(_go())
    assert capped == [slot] and len(runner.calls) == 1


def test_send_defaults_to_the_cautious_provenance_and_can_skip_the_user_row(runner):
    slot = _Slot()

    async def _go():
        await _sdk().send(_State(slot), slot, "seed", append_user=False)

    _run(_go())
    assert slot.rows == []
    assert runner.calls[0]["_directive_user_origin"] is False
    assert runner.calls[0]["_message_is_typed"] is True


def test_a_restored_note_hold_is_delivered_above_the_user_row(runner, monkeypatch):
    order: list[str] = []
    slot = _Slot()
    real_append = slot.append

    def _append(role, content, *, meta=None):
        order.append(f"append:{role}")
        real_append(role, content, meta=meta)

    slot.append = _append  # type: ignore[method-assign]
    monkeypatch.setattr(
        "kiro_crew.dashboard.slot_buffers.flush_restored_hold",
        lambda target, *, logger: order.append(f"flush:{target is slot}"),
    )

    async def _go():
        await _sdk().send(_State(slot), slot, "next")
        await _sdk().send(_State(slot), slot, "replayed", append_user=False)

    _run(_go())
    assert order == ["flush:True", "append:user"]


def test_send_starts_the_turn_its_own_reservation_held(runner):
    slot = _Slot()
    state = _State(slot)

    async def _go():
        async def _validate_then_send():
            reservation = _sdk().reserve(state, slot)
            assert slot.running
            return _sdk().send(state, slot, "decided", reservation=reservation)

        task = await asyncio.create_task(_validate_then_send())
        assert task is not None and slot.task is task
        await task

    _run(_go())
    assert [call["message"] for call in runner.calls] == ["decided"]
    assert slot.queued == []


def test_a_reservation_that_is_no_longer_the_slots_task_does_not_release_it(runner, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control.containment_meta", lambda state, slot: {}
    )
    slot = _Slot()
    state = _State(slot)

    async def _go():
        other = asyncio.get_running_loop().create_future()
        slot.task = other  # type: ignore[assignment]
        stale = asyncio.current_task()
        assert _sdk().send(state, slot, "later", reservation=stale) is None
        assert slot.task is other
        other.cancel()

    _run(_go())
    assert runner.calls == [] and [q[0] for q in slot.queued] == ["later"]


def test_a_slot_whose_rows_are_not_read_in_is_refused_until_it_is_opened(runner, monkeypatch):
    loads: list[Any] = []

    async def _load(target):
        loads.append(target)
        target.window_pending = False

    monkeypatch.setattr("kiro_crew.dashboard.state.load_window_off_loop", _load)
    slot = _Slot()
    slot.window_pending = True
    state = _State(slot)
    with pytest.raises(RuntimeError):
        _sdk().send(state, slot, "too early")
    assert slot.rows == [] and runner.calls == []

    async def _go():
        opened, created = await _sdk().open(state, KEY)
        assert opened is slot and created is False
        await _sdk().send(state, slot, "now")

    _run(_go())
    assert loads == [slot] and [call["message"] for call in runner.calls] == ["now"]


def test_transcript_reads_a_restored_rows_window_off_the_loop_first(monkeypatch):
    loads: list[Any] = []
    slot = _Slot()

    async def _load(target):
        loads.append(target)
        target.window_pending = False
        target.messages = [{"role": "user", "content": "loaded"}]

    monkeypatch.setattr("kiro_crew.dashboard.state.load_window_off_loop", _load)
    slot.window_pending = True
    assert _run(_sdk().transcript(_State(slot), KEY)) == [{"role": "user", "content": "loaded"}]
    assert loads == [slot]


def test_a_message_sent_while_a_turn_runs_is_queued_stamped_and_redacted(monkeypatch):
    stamp = {"containment": "held-at-admission"}
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control.containment_meta", lambda state, slot: stamp
    )
    secret = "token ghp_" + "a" * 36
    slot = _Slot()
    state = _State(slot)

    async def _go():
        slot.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        # A caller's own actor stamp never outranks the app's.
        meta = {"files": ["a"], "turnActor": "user"}
        assert _sdk().send(state, slot, secret, meta=meta, user_origin=True) is None
        slot.task.cancel()

    _run(_go())
    assert slot.queued == [(secret, {"files": ["a"], **stamp, "turnActor": "app"}, True)]
    ((role, text, _),) = slot.rows
    assert role == "queued" and "ghp_" not in text and text
    assert state.pushes == 1


@pytest.mark.parametrize("typed, kept", [(True, True), (False, False)])
def test_a_queued_message_keeps_the_persons_origin_only_when_typed(monkeypatch, typed, kept):
    """The drain runs every queue entry as typed, so a composed one cannot carry the origin."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control.containment_meta", lambda state, slot: {}
    )
    slot = _Slot()

    async def _go():
        slot.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        _sdk().send(_State(slot), slot, "decided", user_origin=True, typed=typed)
        slot.task.cancel()

    _run(_go())
    assert slot.queued == [("decided", {"turnActor": "app"}, kept)]


def test_a_queued_row_is_withheld_when_redaction_fails(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.session_control.containment_meta", lambda state, slot: {}
    )

    def _broken(self, text):
        raise RuntimeError("policy did not compose")

    monkeypatch.setattr(chat_slot_sdk.ScrubSDK, "outbound", _broken)
    slot = _Slot()

    async def _go():
        slot.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
        _sdk().send(_State(slot), slot, "anything")
        slot.task.cancel()

    _run(_go())
    assert slot.rows == [("queued", QUEUED_TEXT_WITHHELD, None)]


def test_a_reservation_holds_the_slot_and_hands_a_queue_to_the_drain(monkeypatch):
    drained: list[Any] = []

    async def _start_next(state, slot):
        drained.append(slot)

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", _start_next)
    slot = _Slot()
    state = _State(slot)

    async def _go():
        released = asyncio.Event()

        async def _validate():
            reservation = _sdk().reserve(state, slot)
            assert reservation is asyncio.current_task() and slot.running
            assert _sdk().reserve(state, slot) is None
            # Runs after the SDK's own release callback, registered first.
            reservation.add_done_callback(lambda _task: released.set())
            slot._queue.append("arrived meanwhile")

        await asyncio.create_task(_validate())
        await released.wait()
        drain = slot.task
        assert drain is not None and drain in state._background_tasks
        await drain

    _run(_go())
    assert drained == [slot]


def test_a_released_reservation_with_nothing_queued_frees_the_slot():
    slot = _Slot()

    async def _go():
        released = asyncio.Event()

        async def _validate():
            reservation = _sdk().reserve(_State(slot), slot)
            reservation.add_done_callback(lambda _task: released.set())

        await asyncio.create_task(_validate())
        await released.wait()

    _run(_go())
    assert slot.task is None


# ── stop and archive ────────────────────────────────────────────────────────


def _history_key(key: str) -> str:
    from kiro_crew.dashboard.chat_utils import _history_key_for

    return _history_key_for(key)


@pytest.mark.parametrize("compacting", [False, True])
def test_stop_turn_drops_queued_work_before_it_stops_then_cancels(monkeypatch, compacting):
    order: list[str] = []
    monkeypatch.setattr(
        "kiro_crew.session_lifecycle.compaction_in_flight",
        lambda sessions, key: order.append(f"probe:{key}") or compacting,
    )
    slot = _Slot()
    slot._queue.append("next")
    slot._pending_steers.append("steer")
    state = _State(slot)

    async def _stop(key, *, force):
        order.append(f"stop:{bool(slot._queue)}")

    state.sessions.stop_turn = _stop  # type: ignore[method-assign]

    async def _go():
        slot.task = asyncio.create_task(asyncio.Event().wait())
        stopped = await _sdk().stop_turn(state, slot)
        return stopped, slot.task.cancelled()

    stopped, cancelled = _run(_go())
    assert stopped is True and cancelled is True
    assert slot._queue == [] and slot._pending_steers == [] and slot._pending_synthesis is False
    expected = [f"probe:{_history_key(KEY)}"] + ([] if compacting else ["stop:False"])
    assert order == expected


def test_a_stop_never_cancels_a_successor_started_during_its_cooperative_stop(monkeypatch):
    """The turn ends while the session stops; a new turn takes ``slot.task`` meanwhile."""
    monkeypatch.setattr(
        "kiro_crew.session_lifecycle.compaction_in_flight", lambda sessions, key: False
    )
    slot = _Slot()
    state = _State(slot)

    async def _go():
        original = asyncio.create_task(asyncio.Event().wait())
        successor = asyncio.create_task(asyncio.Event().wait())
        slot.task = original

        async def _stop(key, *, force):
            original.cancel()
            await asyncio.wait_for(asyncio.gather(original, return_exceptions=True), _BOUND)
            slot.task = successor

        state.sessions.stop_turn = _stop  # type: ignore[method-assign]
        stopped = await _sdk().stop_turn(state, slot)
        outcome = (stopped, successor.cancelled(), successor.done())
        successor.cancel()
        await asyncio.wait_for(asyncio.gather(successor, return_exceptions=True), _BOUND)
        return outcome

    assert _run(_go()) == (True, False, False)


def test_stop_turn_on_an_idle_slot_does_nothing():
    slot = _Slot()
    slot._queue.append("kept")
    assert _run(_sdk().stop_turn(_State(slot), slot)) is False
    assert slot._queue == ["kept"]


def _recording_close(monkeypatch, *, fail: bool = False) -> list[dict[str, Any]]:
    closes: list[dict[str, Any]] = []

    async def _close(state, slot, name):
        closes.append({"slot": slot, "name": name, "queue": list(slot._queue)})
        if fail:
            raise RuntimeError("history_save_failed")
        state._slots.pop(name, None)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _close)
    return closes


def test_archive_drops_queued_work_then_runs_the_gateways_own_close(monkeypatch):
    closes = _recording_close(monkeypatch)
    slot = _Slot()
    slot._queue.append("next")
    state = _State(slot)
    assert _run(_sdk().archive(state, slot)) is True
    assert closes == [{"slot": slot, "name": KEY, "queue": []}]
    assert state.get_slot(KEY) is None


def test_an_archive_whose_close_did_not_commit_answers_false(monkeypatch):
    """The gateway's close rolls itself back; the archive reports it, never success."""
    closes = _recording_close(monkeypatch, fail=True)
    slot = _Slot()
    state = _State(slot)
    assert _run(_sdk().archive(state, slot)) is False
    assert [close["slot"] for close in closes] == [slot]


def test_archive_leaves_a_slot_the_registry_replaced(monkeypatch):
    closes = _recording_close(monkeypatch)
    captured = _Slot()
    replacement = _Slot()
    state = _State(replacement)
    assert _run(_sdk().archive(state, captured)) is True
    assert state.get_slot(KEY) is replacement and closes == []


def test_archive_closes_a_real_slot_and_saves_its_transcript_closed(tmp_path, monkeypatch):
    """Against the gateway's real state and close: the slot leaves and its line says closed."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    sdk = _sdk()

    async def _go():
        slot, created = await sdk.open(state, KEY)
        slot.append("user", "plan the job")
        return created, slot, await sdk.archive(state, slot)

    created, slot, archived = _run(_go())
    assert created is True and archived is True
    assert state.get_slot(KEY) is None
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("closed") is True and meta.get("app") == APP


# ── transcript ──────────────────────────────────────────────────────────────


def test_transcript_reads_the_live_slot_then_the_saved_log():
    live = _Slot()
    live.messages = [{"role": "user", "content": "hi"}]
    assert _run(_sdk().transcript(_State(live), KEY)) == live.messages
    state = _State()
    state.conversation_log = _Log(
        [{"role": "assistant", "content": "saved"}], {_history_key(KEY): APP}
    )
    assert _run(_sdk().transcript(state, KEY)) == [{"role": "assistant", "content": "saved"}]
    assert state.conversation_log.read == [_history_key(KEY)]


@pytest.mark.parametrize("owner", ["slot-probe-tools", None])
def test_a_saved_transcript_another_owner_records_is_not_read(owner):
    """``slot-probe-tools-1`` is spelled under ``slot-probe``'s prefix too."""
    key = f"{APP}-tools-1"
    owners = {_history_key(key): owner} if owner else {}
    state = _State()
    state.conversation_log = _Log([{"role": "user", "content": "private"}], owners)
    assert _run(_sdk().transcript(state, key)) == []
    assert state.conversation_log.read == []


def test_transcript_of_a_foreign_live_slot_is_empty():
    foreign = _Slot(app="other")
    foreign.messages = [{"role": "user", "content": "private"}]
    state = _State(foreign)
    state.conversation_log = _Log([{"role": "user", "content": "private"}])
    assert _run(_sdk().transcript(state, KEY)) == []
    assert state.conversation_log.read == []


# ── AutoNudge ───────────────────────────────────────────────────────────────


class _Service:
    def __init__(self, *loops: SimpleNamespace) -> None:
        self.loops = {loop.id: loop for loop in loops}
        self.removed: list[tuple[str, dict[str, Any]]] = []
        self.paused: list[str] = []
        #: What ``remove`` answers: ``False`` is the live service refusing it.
        self.removes = True

    def get_by_slot(self, key: str) -> SimpleNamespace | None:
        return next((loop for loop in self.loops.values() if loop.slot_key == key), None)

    def get_by_id(self, loop_id: str) -> SimpleNamespace | None:
        return self.loops.get(loop_id)

    def list_all(self) -> list[SimpleNamespace]:
        return list(self.loops.values())

    async def remove(self, loop_id: str, **kwargs: Any) -> bool:
        self.removed.append((loop_id, kwargs))
        return self.removes

    async def deactivate_and_wait(self, loop_id: str, **kwargs: Any) -> bool:
        self.paused.append(loop_id)
        return True


_OWN = SimpleNamespace(id="own", slot_key=KEY)
#: Spelled under this app's prefix, but the slot is another app's (``slot-probe-tools``).
_EXTENDED = SimpleNamespace(id="extended", slot_key=f"{APP}-tools-1")
_COLD = SimpleNamespace(id="cold", slot_key=f"{APP}-job-9")
_THEIRS = SimpleNamespace(id="theirs", slot_key="other-job-1")
_PERSONAL = SimpleNamespace(id="personal", slot_key="default")


def _live(monkeypatch, service: Any) -> None:
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)


def _slots_state() -> _State:
    return _State(
        _Slot(),
        _Slot(f"{APP}-tools-1", app="slot-probe-tools"),
        _Slot("other-job-1", app="other"),
        _Slot("default", app=None),
    )


def test_the_nudge_view_sees_only_loops_on_slots_the_app_owns(monkeypatch):
    _live(monkeypatch, _Service(_OWN, _EXTENDED, _COLD, _THEIRS, _PERSONAL))
    nudges = _sdk().nudges(_slots_state())
    assert nudges is not None
    assert nudges.list_all() == [_OWN]
    assert nudges.get_by_slot(KEY) is _OWN
    for key in (f"{APP}-tools-1", f"{APP}-job-9", "other-job-1", "default"):
        assert nudges.get_by_slot(key) is None, key


def test_the_nudge_view_reads_the_gateway_state_when_given_none(monkeypatch):
    _live(monkeypatch, _Service(_OWN))
    sdk = ChatSlotSDK(APP, http_app={"state": _slots_state()})
    assert sdk.nudges().list_all() == [_OWN]
    assert ChatSlotSDK(APP).nudges().list_all() == []


def test_no_running_autonudge_is_no_view(monkeypatch):
    _live(monkeypatch, None)
    assert _sdk().nudges(_slots_state()) is None


@pytest.mark.parametrize("loop_id", ["extended", "cold", "theirs", "personal", "missing"])
def test_a_loop_on_a_slot_the_app_does_not_own_cannot_be_removed_or_paused(monkeypatch, loop_id):
    service = _Service(_OWN, _EXTENDED, _COLD, _THEIRS, _PERSONAL)
    _live(monkeypatch, service)
    nudges = _sdk().nudges(_slots_state())
    with pytest.raises(ChatSlotRefused):
        _run(nudges.remove(loop_id))
    with pytest.raises(ChatSlotRefused):
        _run(nudges.deactivate_and_wait(loop_id))
    assert service.removed == [] and service.paused == []


def test_remove_on_slot_honours_the_captured_loop(monkeypatch):
    service = _Service(_OWN, _EXTENDED)
    _live(monkeypatch, service)
    nudges = _sdk().nudges(_slots_state())
    assert _run(nudges.remove_on_slot(KEY, only_loop_id="replaced", stop_reason="x")) is False
    assert _run(nudges.remove_on_slot(KEY, only_loop_id="own", stop_reason="stopped")) is True
    assert _run(nudges.remove_on_slot(f"{APP}-tools-1")) is False
    assert service.removed == [("own", {"stop_reason": "stopped"})]


def test_a_removal_the_live_service_refused_is_not_reported_as_done(monkeypatch):
    service = _Service(_OWN)
    service.removes = False
    _live(monkeypatch, service)
    nudges = _sdk().nudges(_slots_state())
    assert _run(nudges.remove("own", stop_reason="stopped")) is False
    assert _run(nudges.remove_on_slot(KEY, only_loop_id="own", stop_reason="stopped")) is False
    assert [loop_id for loop_id, _ in service.removed] == ["own", "own"]


class _MaintenanceView:
    """The maintenance view's shape: no ``get_by_id``, and ``remove`` takes no reason."""

    def __init__(self, *loops: SimpleNamespace) -> None:
        self.loops = list(loops)
        self.removed: list[str] = []
        self.paused: list[tuple[str, str | None]] = []

    def list_all(self) -> list[SimpleNamespace]:
        return list(self.loops)

    def get_by_slot(self, key: str) -> SimpleNamespace | None:
        return next((loop for loop in self.loops if loop.slot_key == key), None)

    async def deactivate_and_wait(self, loop_id: str, *, stopped_reason: str | None = None):
        self.paused.append((loop_id, stopped_reason))
        return True

    async def remove(self, loop_id: str) -> None:
        self.removed.append(loop_id)


def test_the_maintenance_view_reads_saved_owners_and_uses_its_own_remove(monkeypatch):
    orphan = SimpleNamespace(id="orphan", slot_key=f"{APP}-job-7")
    view = _MaintenanceView(_OWN, _EXTENDED, _COLD, orphan, _THEIRS)
    state = _slots_state()
    state.conversation_log = _Log(
        [], {_history_key(f"{APP}-job-7"): APP, _history_key(f"{APP}-job-9"): "slot-probe-x"}
    )

    class _ServiceCls:
        @staticmethod
        @contextlib.asynccontextmanager
        async def maintenance_service():
            yield view

    monkeypatch.setattr("kiro_crew.autonudge.AutoNudgeService", _ServiceCls)

    async def _go():
        async with _sdk().nudge_maintenance(state) as nudges:
            assert nudges.list_all() == [_OWN, orphan]
            assert await nudges.deactivate_and_wait("orphan") is True
            assert await nudges.deactivate_and_wait("own", stopped_reason="orphaned") is True
            assert await nudges.remove("own", stop_reason="not recorded here") is True
            with pytest.raises(ChatSlotRefused):
                await nudges.remove("theirs")

    _run(_go())
    assert view.paused == [("orphan", None), ("own", "orphaned")] and view.removed == ["own"]


def test_arm_nudge_goes_through_the_shared_authorization_as_the_app(monkeypatch):
    service = _Service()
    _live(monkeypatch, service)
    seen: list[dict[str, Any]] = []

    async def _authorize(**kwargs):
        seen.append(kwargs)
        return "loop", None, 200

    monkeypatch.setattr("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authorize)
    slot = _Slot()
    state = _State(slot)
    answer = _run(
        _sdk().arm_nudge(
            state,
            slot,
            message="go on",
            idle_secs=120,
            max_cycles=60,
            stop_sentinel_path="/p/STOP",
            caller="owner",
        )
    )
    assert answer == ("loop", None, 200)
    assert seen == [
        {
            "svc": service,
            "state": state,
            "slot_key": KEY,
            "message": "go on",
            "idle_secs": 120,
            "max_cycles": 60,
            "stop_sentinel_path": "/p/STOP",
            "source": f"app:{APP}",
            "caller": "owner",
        }
    ]


def test_arm_nudge_without_autonudge_arms_nothing(monkeypatch):
    _live(monkeypatch, None)

    async def _authorize(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("armed without a service")

    monkeypatch.setattr("kiro_crew.autonudge_authz.authorize_and_add_nudge", _authorize)
    slot = _Slot()
    loop, error, status = _run(
        _sdk().arm_nudge(_State(slot), slot, message="go", idle_secs=60, max_cycles=1)
    )
    assert loop is None and error and status == 503


# ── disclosure ──────────────────────────────────────────────────────────────


def test_the_install_summary_and_the_catalog_name_the_declaration():
    from kiro_crew.apps.manifest import AppManifest, Permissions
    from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest
    from kiro_crew.apps.permissions import format_permissions_summary

    manifest = AppManifest(
        name=APP,
        version="1.0.0",
        displayName="T",
        description="T",
        permissions=Permissions(chatSlots=True),
    )
    assert "its own chat sessions" in format_permissions_summary(manifest)
    assert _disclosed_permissions_manifest({"permissions": {"chatSlots": True}}) == {
        "permissions": {"chatSlots": True}
    }
