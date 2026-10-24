from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import slot_retention, warm_task_brief


class _State:
    def __init__(self) -> None:
        self._foreground_slots: dict[int, str] = {}
        self._warm_task_brief_last_seen: dict[str, float] = {}
        self._warm_task_brief_not_before: dict[str, float] = {}
        self._warm_task_brief_tasks = {}
        self._warm_task_brief_model_active: set[str] = set()
        self._warm_task_brief_abandoned: set[str] = set()
        self.frames: list[tuple[str, dict]] = []
        self.preparing_pushes: list[dict[str, bool]] = []
        # The real registry is ``_slots``; one dict serves both names.
        self.slots = self._slots = {}
        self.sessions = object()

    def broadcast_ws_owners(self, kind: str, data: dict) -> None:
        self.frames.append((kind, data))

    def broadcast_ws(self, kind: str, data: dict) -> None:
        self.frames.append((kind, data))

    def get_slot(self, key: str):
        return self.slots.get(key)

    def push_slots_update(self) -> None:
        # What every slots frame would say about each slot's indicator.
        self.preparing_pushes.append(
            {key: slot._preparing_count > 0 for key, slot in self.slots.items()}
        )

    def flush_slot_now(self, slot) -> bool:
        slot.flushed += 1
        return True


class _Slot:
    def __init__(self, key: str = "thread-x") -> None:
        self.key = key
        self.running = False
        self._created_by = ""
        self._revived_by = ""
        self.linked_session_key = ""
        self.channel_origin = False
        self.mode = "member"
        self._has_reader = False
        self.rows: list[dict] = []
        self.messages = self.rows
        self.flushed = 0
        self._preparing_count = 0

    def append(self, role: str, content: str, cls: str = "", **kwargs):
        row = {"role": role, "content": content, "cls": cls}
        if kwargs.get("meta") is not None:
            row["meta"] = kwargs["meta"]
        self.rows.append(row)
        return row


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class _DurableSlot(_Slot):
    """A slot whose rows are the thread's history, stamped like real rows."""

    def append(self, role: str, content: str, cls: str = "", **kwargs):
        row = super().append(role, content, cls, **kwargs)
        row["ts"] = _iso(time.time())
        return row


class _Log:
    """The on-disk transcript: it outlives any one ``_State`` (a restart)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.reads = 0

    def derive_messages_chained(self, key: str) -> list[dict]:
        self.reads += 1
        return list(self.rows)


def _thread(monkeypatch, *, user_ago: float | None, log: _Log | None = None):
    """A crewmate thread with an open task whose owner last typed *user_ago* s ago.

    Passing an existing *log* simulates a gateway restart: a fresh state and
    slot over the same durable history.
    """
    if log is None:
        rows: list[dict] = []
        if user_ago is not None:
            rows.append(
                {
                    "role": "user",
                    "content": "Fix the failing test",
                    "ts": _iso(time.time() - user_ago),
                }
            )
        rows.append({"role": "assistant", "content": "On it.", "ts": _iso(time.time() - 60)})
        log = _Log(rows)
    state = _State()
    state.conversation_log = log
    slot = _DurableSlot()
    slot.rows = log.rows
    slot.messages = log.rows
    state.slots[slot.key] = slot
    monkeypatch.setattr(warm_task_brief, "read_state", lambda key: {"phase": "implementing"})
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, []))

    async def run_model(*args, **kwargs):
        return '{"message":"You asked me to fix the test. It is in progress."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    return state, slot, log


async def _focus(state: _State, conn: int, slot_key: object, *, announce: bool = False) -> None:
    """Apply one owner focus frame as ws.py does, then let any brief finish."""
    warm_task_brief.handle_focus_frame(state, conn, slot_key, announce=announce)
    slot_retention.note_foreground(state, conn, slot_key)
    task = state._warm_task_brief_tasks.get(slot_key) if isinstance(slot_key, str) else None
    if task is not None:
        await asyncio.wait_for(task, timeout=30)


def _brief_kinds(slot: _Slot) -> list[str]:
    return [
        row["meta"]["kind"]
        for row in slot.rows
        if str((row.get("meta") or {}).get("kind", "")).startswith("warm_task_brief")
    ]


_FULL_BRIEF = ["warm_task_brief_claim", "warm_task_brief_preparing", "warm_task_brief_result"]


@pytest.mark.asyncio
async def test_return_after_gateway_restart_briefs_from_history(monkeypatch):
    # The owner left two hours ago; this process never saw them leave.
    state, slot, log = _thread(monkeypatch, user_ago=2 * 3600)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF

    # Another restart: the durable claim stops a second brief for this state.
    restarted, slot2, _ = _thread(monkeypatch, user_ago=None, log=log)
    await _focus(restarted, 1, slot2.key)
    assert _brief_kinds(slot2) == _FULL_BRIEF


def _seen(state: _State, slot_key: str, ago: float) -> None:
    """This process last saw *slot_key* on screen *ago* seconds back."""
    state._warm_task_brief_last_seen[slot_key] = time.time() - ago


@pytest.mark.asyncio
async def test_watching_a_running_thread_for_hours_then_a_focus_frame_gives_no_brief(
    monkeypatch,
):
    # Last typed three hours ago; the tab has shown the thread for two hours.
    state, slot, log = _thread(monkeypatch, user_ago=3 * 3600)
    _seen(state, slot.key, 2 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    await _focus(state, 1, slot.key)
    # A second window opening the same thread is the same view.
    await _focus(state, 2, slot.key)
    assert _brief_kinds(slot) == []
    assert log.reads == 0


@pytest.mark.asyncio
async def test_switching_away_briefly_after_hours_of_watching_gives_no_brief(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=3 * 3600)
    _seen(state, slot.key, 2 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    await _focus(state, 1, "thread-y")
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_hidden_45_minutes_then_return_briefs(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_hidden_45_minutes_but_typed_10_minutes_ago_gives_no_brief(monkeypatch):
    # Typed from another device while this view was hidden.
    state, slot, _ = _thread(monkeypatch, user_ago=600)
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_reconnect_wave_after_restart_never_briefs(monkeypatch):
    # A restarted gateway: five visible tabs re-announce five idle threads.
    state, first, log = _thread(monkeypatch, user_ago=2 * 3600)
    keys = [first.key]
    for n in range(4):
        other = _DurableSlot(f"thread-{n}")
        other.rows = other.messages = log.rows
        state.slots[other.key] = other
        keys.append(other.key)
    for conn, key in enumerate(keys, start=1):
        await _focus(state, conn, key, announce=True)
    assert state._warm_task_brief_tasks == {}
    assert log.reads == 0
    # Each announced thread now counts as seen: a quick switch is no return.
    await _focus(state, 1, None)
    await _focus(state, 1, keys[0])
    assert _brief_kinds(first) == []


@pytest.mark.asyncio
async def test_reload_after_a_real_absence_briefs(monkeypatch):
    # The process saw the thread leave the screen; a fresh page announces it.
    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    _seen(state, slot.key, 45 * 60)
    await _focus(state, 7, slot.key, announce=True)
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_closed_socket_stamps_the_thread_it_showed(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=3 * 3600)
    _seen(state, slot.key, 2 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    # The tab was closed with no hide frame; the socket close is the leave.
    warm_task_brief.note_view_closed(state, 1)
    slot_retention.note_foreground(state, 1, None)
    await _focus(state, 2, slot.key, announce=True)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("typed_ago", "briefs"), [(600, False), (2 * 3600, True)])
async def test_old_naive_rows_read_as_local_time(monkeypatch, local_tz, typed_ago, briefs):
    # Older builds wrote naive local wall-clock. Read as UTC west of UTC, a
    # message typed ten minutes ago would look seven hours old.
    local_tz("America/Los_Angeles")
    state, slot, log = _thread(monkeypatch, user_ago=None)
    log.rows.insert(
        0,
        {
            "role": "user",
            "content": "Fix the failing test",
            "ts": datetime.fromtimestamp(time.time() - typed_ago).isoformat(),
        },
    )
    await _focus(state, 1, slot.key)
    assert (_brief_kinds(slot) == _FULL_BRIEF) is briefs


@pytest.mark.asyncio
async def test_reconnect_resend_and_second_window_brief_once(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    # Two windows report the same thread in one tick: one task.
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    slot_retention.note_foreground(state, 1, slot.key)
    first = state._warm_task_brief_tasks[slot.key]
    warm_task_brief.handle_focus_frame(state, 2, slot.key)
    slot_retention.note_foreground(state, 2, slot.key)
    assert state._warm_task_brief_tasks[slot.key] is first
    await asyncio.wait_for(first, timeout=30)
    # A reconnect re-sends slot_focused on a new socket; a window refocuses.
    await _focus(state, 3, slot.key)
    await _focus(state, 2, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_patrol_after_a_brief_does_not_brief_again(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    await _focus(state, 1, slot.key)
    # A patrol cycle changes the task fingerprint but the owner never typed.
    slot.append("user", "[auto-nudge cycle 3]", "msg msg-u")
    slot.append("assistant", "Patrol: one PR went green.", "msg msg-a")
    state._warm_task_brief_not_before.clear()
    await _focus(state, 1, None)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_message_typed_ten_minutes_ago_gives_no_brief_and_caches(monkeypatch):
    state, slot, log = _thread(monkeypatch, user_ago=600)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []
    typed_at = warm_task_brief._row_epoch(log.rows[0]["ts"])
    assert typed_at is not None
    assert state._warm_task_brief_not_before[slot.key] == (
        typed_at + warm_task_brief.WARM_TASK_BRIEF_AWAY_SECS
    )
    reads = log.reads
    await _focus(state, 2, slot.key)
    assert log.reads == reads


@pytest.mark.asyncio
async def test_thread_without_a_user_message_gives_no_brief(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=None)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_user_message_in_the_future_gives_no_brief(monkeypatch):
    # A wall clock set back must not make an absence look long.
    state, slot, _ = _thread(monkeypatch, user_ago=-3600)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == []


@pytest.mark.asyncio
async def test_hidden_and_null_frames_never_start_work(monkeypatch):
    state, slot, log = _thread(monkeypatch, user_ago=2 * 3600)
    slot_retention.note_foreground(state, 1, slot.key)
    await _focus(state, 1, None)
    assert state._warm_task_brief_tasks == {}
    assert log.reads == 0


@pytest.mark.asyncio
async def test_leave_and_return_during_the_model_call_posts_no_result(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_model(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=30)
        return '{"message":"Old return."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    slot_retention.note_foreground(state, 1, slot.key)
    task = state._warm_task_brief_tasks[slot.key]
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        # The last window switches away and back without typing.
        await _focus(state, 1, "thread-y")
        warm_task_brief.handle_focus_frame(state, 1, slot.key)
        slot_retention.note_foreground(state, 1, slot.key)
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=30)

    assert _brief_kinds(slot) == ["warm_task_brief_claim", "warm_task_brief_preparing"]
    assert state._warm_task_brief_abandoned == set()


@pytest.mark.asyncio
async def test_one_window_leaving_keeps_the_other_windows_brief(monkeypatch):
    state, slot, _ = _thread(monkeypatch, user_ago=2 * 3600)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_model(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=30)
        return '{"message":"Here is where we are."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    slot_retention.note_foreground(state, 1, slot.key)
    task = state._warm_task_brief_tasks[slot.key]
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        # Window 2 opens the thread, then switches away; window 1 still shows it.
        warm_task_brief.handle_focus_frame(state, 2, slot.key)
        slot_retention.note_foreground(state, 2, slot.key)
        assert state._warm_task_brief_tasks[slot.key] is task
        await _focus(state, 2, "thread-y")
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=30)

    assert _brief_kinds(slot) == _FULL_BRIEF


def test_an_overlong_focus_key_is_never_retained():
    state = _State()
    warm_task_brief.handle_focus_frame(state, 1, "x" * (slot_retention._MAX_FOREGROUND_KEY + 1))
    assert state._warm_task_brief_last_seen == {}
    assert state._warm_task_brief_tasks == {}


def test_last_seen_record_is_bounded(monkeypatch):
    monkeypatch.setattr(warm_task_brief, "_MAX_LAST_SEEN", 2)
    state = _State()
    for key in ["a", "b", "c"]:
        warm_task_brief._stamp_seen(state, key, 1.0)
    assert list(state._warm_task_brief_last_seen) == ["b", "c"]


def test_not_before_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(warm_task_brief, "_MAX_NOT_BEFORE", 2)
    state = _State()
    for key in ["a", "b", "c"]:
        warm_task_brief._remember_not_before(state, key, 1.0)
    assert list(state._warm_task_brief_not_before) == ["b", "c"]


def _snapshot(fingerprint: str = "fp", *, previous_claim: str = ""):
    return warm_task_brief.TaskSnapshot(
        slot_key="thread-x",
        history_key="dashboard:thread-x",
        original_request="Fix the failing test",
        fingerprint=fingerprint,
        transcript="User: Fix the failing test",
        session_state={"phase": "implementing"},
        conductor={"goal": "Fix the test"},
        work_items=({"state": "open", "summary": "Failure reproduced"},),
        progress=(0, 1),
        previous_claim=previous_claim,
        # Hours before any test clock: the owner has been away.
        last_user_at=0.0,
    )


def _claimed(snapshot: warm_task_brief.TaskSnapshot) -> warm_task_brief.TaskSnapshot:
    return replace(snapshot, previous_claim=snapshot.fingerprint)


def _posted(slot: _Slot) -> list[dict]:
    return [row for row in slot.rows if row["role"] == "assistant"]


@pytest.mark.asyncio
async def test_opener_precedes_the_single_model_call_and_result_is_a_structured_brief(
    monkeypatch,
):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()

    async def read_snapshot(*args, **kwargs):
        return _claimed(snapshot) if slot.flushed else snapshot

    kinds_at_call = []
    call_kwargs = {}

    async def run_model(*args, **kwargs):
        kinds_at_call.extend(row["meta"]["kind"] for row in slot.rows)
        call_kwargs.update(kwargs)
        call_kwargs["prompt"] = args[1]
        # The model's own progress numbers are ignored: the card shows the
        # gateway's count from the snapshot's work items.
        return json.dumps(
            {
                "status": "needs_you",
                "headline": "I reproduced the **failure**.",
                "asked": "Fix the failing test",
                "progress": {"accepted": 9, "total": 9},
                "remaining": "Push the fix and rerun CI",
                "done": ["Reproduced the failure"],
                "needs_you": ["Approve the push so CI can run"],
                "next": ["Apply the fix", "Rerun CI"],
                "extra": [{"title": "Risk", "items": ["The fix touches a shared helper"]}],
            }
        )

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(resolve_model=lambda role: "background"),
            dashboard=SimpleNamespace(verbosity="answer_only"),
        ),
    )

    await warm_task_brief._prepare_warm_task_brief(state, slot)

    assert kinds_at_call == ["warm_task_brief_claim", "warm_task_brief_preparing"]
    preparing = slot.rows[1]
    assert preparing["role"] == "assistant"
    assert preparing["content"] == "Welcome back, let me catch you up on where we are."
    assert preparing["meta"]["kind"] == "warm_task_brief_preparing"
    # The claim flush, then the pre-publication flush.
    assert slot.flushed == 2
    assert call_kwargs["crew_log_kind"] == "warm_task_brief"
    assert call_kwargs["crew_log_session_key"] == "dashboard:thread-x"
    assert call_kwargs["timeout"] == 60.0
    assert call_kwargs["retry_rejected_model"] is False
    assert slot.rows[0]["meta"] == {"kind": "warm_task_brief_claim", "fingerprint": "fp"}
    result = slot.rows[-1]
    assert result["role"] == "assistant"
    assert result["meta"] == {
        "kind": "warm_task_brief_result",
        "fingerprint": "fp",
        "brief": {
            "status": "needs_you",
            "headline": "I reproduced the failure.",
            "asked": "Fix the failing test",
            "progress": {"accepted": 0, "total": 1},
            "remaining": "Push the fix and rerun CI",
            "done": ["Reproduced the failure"],
            "needs_you": ["Approve the push so CI can run"],
            "next": ["Apply the fix", "Rerun CI"],
            "extra": [{"title": "Risk", "items": ["The fix touches a shared helper"]}],
        },
    }
    # The stored content is the same brief as plain text, in the card's order,
    # for every reader that does not draw the card.
    assert result["content"] == (
        "Waiting on you: I reproduced the failure.\n"
        "Needs you:\n- Approve the push so CI can run\n"
        "Progress: 0 of 1 done\n"
        "Left: Push the fix and rerun CI\n"
        "You asked: Fix the failing test\n"
        "Done so far:\n- Reproduced the failure\n"
        "Next:\n1. Apply the fix\n2. Rerun CI\n"
        "Risk:\n- The fix touches a shared helper"
    )
    prompt = call_kwargs["prompt"]
    assert '"original_request": "Fix the failing test"' in prompt
    # The model sees the gateway's count so its "remaining" line agrees with it.
    assert '"progress": {"accepted": 0, "total": 1}' in prompt
    assert "professional manager" in prompt and "Never invent numbers" in prompt
    assert '"needs_you"' in prompt and "no markdown, no HTML, no links" in prompt
    # The user's reply-style choice reaches the prompt.
    assert warm_task_brief._STYLE_HINTS["answer_only"] in prompt


@pytest.mark.asyncio
async def test_existing_claim_suppresses_status_and_model(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key

    async def read_snapshot(*args, **kwargs):
        return _snapshot(previous_claim="fp")

    async def unexpected(*args, **kwargs):
        raise AssertionError("model must not run")

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", unexpected)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert state.frames == []
    assert slot.rows == []


@pytest.mark.asyncio
async def test_model_failure_posts_an_ordinary_failure_message(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()

    async def read_snapshot(*args, **kwargs):
        return _claimed(snapshot) if slot.flushed else snapshot

    async def fail(*args, **kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", fail)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert [row["content"] for row in _posted(slot)] == [
        "Welcome back, let me catch you up on where we are.",
        "Sorry, I couldn't pull that together just now. Ask me where things stand.",
    ]


@pytest.mark.asyncio
async def test_user_send_drops_stale_result_and_keeps_preparing(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    base = _snapshot()
    sent = replace(_claimed(base), fingerprint="changed", user_fingerprint="sent")
    snapshots = [base, _claimed(base), sent]

    async def read_snapshot(*args, **kwargs):
        return snapshots.pop(0)

    async def run_model(*args, **kwargs):
        return '{"message":"Old state."}'

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert [row["content"] for row in _posted(slot)] == [
        "Welcome back, let me catch you up on where we are."
    ]
    assert snapshots == []
    assert all(row.get("meta", {}).get("kind") != "warm_task_brief_result" for row in slot.rows)


@pytest.mark.asyncio
async def test_live_derivation_withheld_slot_never_claims_or_calls_model(monkeypatch):
    state = _State()
    slot = _Slot()
    slot.memory_mode = "incognito"
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key

    async def unexpected(*args, **kwargs):
        raise AssertionError("restricted transcript must not be read or sent")

    monkeypatch.setattr(warm_task_brief, "_snapshot", unexpected)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", unexpected)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows == []
    assert state.frames == []


def test_generated_plain_messages_stay_out_of_summary_input():
    turns = warm_task_brief.extract_turns(
        [
            {"role": "user", "content": "real request"},
            {
                "role": "assistant",
                "content": "Welcome back, let me catch you up on where we are.",
                "meta": {"kind": "warm_task_brief_preparing"},
            },
            {"role": "assistant", "content": "real answer"},
        ]
    )

    assert [turn.text for turn in turns] == ["real request", "real answer"]


def test_generated_plain_messages_stay_out_of_model_replay():
    from kiro_crew import context

    class _Log:
        def read_messages_chained(self, key):
            return [
                {"role": "user", "content": "real request"},
                {
                    "role": "assistant",
                    "content": "summary",
                    "meta": {"kind": "warm_task_brief_result"},
                },
                {"role": "assistant", "content": "real answer"},
            ]

    rows = context._replay_rows(_Log(), "dashboard:test")
    assert [row["content"] for row in rows] == ["real request", "real answer"]


def test_generated_plain_messages_stay_out_of_secondary_model_inputs():
    from kiro_crew.autonudge_judge import session_evidence
    from kiro_crew.history_consolidation import _prompt_rows

    generated = {
        "role": "assistant",
        "content": "summary",
        "meta": {"kind": "warm_task_brief_result"},
    }
    real = {"role": "assistant", "content": "real answer"}

    assert _prompt_rows([generated, real]) == [real]
    assert [item["text"] for item in session_evidence([generated, real], "chat-test")] == [
        "real answer"
    ]


def test_transfer_and_projection_keep_generated_rows_out_of_replay():
    from kiro_crew.dashboard.session_transfer import _assemble_bundle
    from kiro_crew.history_projection import TranscriptReadProjection

    generated = {
        "role": "assistant",
        "content": "summary",
        "ts": "1",
        "meta": {
            "kind": "warm_task_brief_result",
            "fingerprint": "fp",
            "mid": "m-brief",
        },
    }
    real = {"role": "assistant", "content": "real answer", "ts": "2"}
    log = SimpleNamespace(
        _tail_reads=False,
        _read_messages=lambda key: [generated, real],
        read_messages_chained=lambda key: [generated, real],
    )

    assert TranscriptReadProjection(log).recent("chat-test") == [
        {"role": "assistant", "content": "real answer"}
    ]
    bundle = _assemble_bundle([generated, real], "Title", "agent", "source")
    assert [row["content"] for row in bundle["messages"]] == ["real answer"]


def test_snapshot_fingerprint_ignores_claim_and_brief_rows(monkeypatch):
    slot = _Slot()
    rows = [
        {"role": "user", "content": "Earlier finished request", "ts": "2026-01-01T10:00:00+00:00"},
        {
            "role": "assistant",
            "content": "Earlier work finished",
            "ts": "2026-01-01T10:05:00+00:00",
        },
        {"role": "user", "content": "Original request", "ts": "2026-01-02T10:00:00+00:00"},
        {"role": "assistant", "content": "Work started", "ts": "2026-01-02T10:05:00+00:00"},
    ]
    log = SimpleNamespace(derive_messages_chained=lambda key: list(rows))
    state = SimpleNamespace(conversation_log=log)
    open_item = SimpleNamespace(
        item_id="it_1",
        title="Implement",
        state="open",
        verdict=None,
        decision="",
        worker_session_key="worker",
        status="progress",
        reason=None,
        summary="Started",
        artifacts={},
        last_report_at="3",
        created_at="2026-01-02T10:01:00+00:00",
        closed_at=None,
        decided_at=None,
    )
    monkeypatch.setattr(warm_task_brief, "read_state", lambda key: {"phase": "implementing"})
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, [open_item]))

    first = warm_task_brief._snapshot_sync(state, slot)
    assert first is not None and first.progress is None
    rows.extend(
        [
            {
                "role": "notice",
                "content": "",
                "meta": {"kind": "warm_task_brief_claim", "fingerprint": "old"},
            },
            {"role": "assistant", "content": "summary", "meta": {"kind": "warm_task_brief_result"}},
            {"role": "user", "content": "[auto-nudge cycle 2]", "ts": "3"},
        ]
    )
    second = warm_task_brief._snapshot_sync(state, slot)

    assert first is not None and second is not None
    assert first.original_request == "Original request"
    assert first.fingerprint == second.fingerprint
    assert second.previous_claim == "old"


def test_terminal_task_is_not_eligible():
    assert not warm_task_brief._has_open_task(
        {"phase": "done"},
        {"goal": "Finished"},
        ({"state": "accepted"},),
    )


@pytest.mark.asyncio
async def test_leaving_cancels_a_pre_model_task():
    state = _State()
    state._foreground_slots[1] = "thread-x"
    task = asyncio.create_task(asyncio.sleep(60))
    state._warm_task_brief_tasks["thread-x"] = task

    warm_task_brief.handle_focus_frame(state, 1, None)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert state.frames == []


def test_ordinary_chat_return_never_starts_a_brief():
    state = _State()
    slot = _Slot()
    slot.mode = "chat"
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert state._warm_task_brief_tasks == {}


@pytest.mark.asyncio
async def test_unpersisted_claim_never_surfaces_status_or_spends(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()
    state.flush_slot_now = lambda _slot: None

    async def read_snapshot(*args, **kwargs):
        return snapshot

    async def unexpected(*args, **kwargs):
        raise AssertionError("model must not run without a durable claim")

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", unexpected)
    await warm_task_brief._prepare_warm_task_brief(state, slot)

    assert _posted(slot) == []


def test_session_phase_anchors_original_request_to_its_created_at():
    turns = [
        warm_task_brief.TranscriptTurn(
            index=1,
            role="user",
            text="old request",
            ts="2026-01-01T09:00:00+00:00",
            user_turn=1,
        ),
        warm_task_brief.TranscriptTurn(
            index=2,
            role="user",
            text="start this task",
            ts="2026-01-01T10:00:00+00:00",
            user_turn=2,
        ),
        warm_task_brief.TranscriptTurn(
            index=3,
            role="user",
            text="later follow-up",
            ts="2026-01-01T11:00:00+00:00",
            user_turn=3,
        ),
    ]

    result = warm_task_brief._original_request_for_task(
        turns,
        {"phase": "working", "created_at": "2026-01-01T10:01:00+00:00"},
        None,
        (),
    )

    assert result == "start this task"


def test_long_original_request_has_an_explicit_omission_mark():
    request = "x" * 5000
    result = warm_task_brief._original_request_for_task(
        [
            warm_task_brief.TranscriptTurn(
                index=1,
                role="user",
                text=request,
                ts="2026-01-01T10:00:00+00:00",
                user_turn=1,
            )
        ],
        {},
        None,
        ({"state": "open", "created_at": "2026-01-01T10:01:00+00:00"},),
    )
    assert len(result) <= 4000
    kept = result.split("\n[... ")[0]
    assert f"[... {5000 - len(kept)} characters omitted ...]" in result
    assert result.endswith("characters omitted ...]")


@pytest.mark.asyncio
async def test_cancel_during_model_suppresses_result_without_cancelling_acquisition(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()
    entered = asyncio.Event()
    release = asyncio.Event()
    model_cancelled = False

    async def read_snapshot(*args, **kwargs):
        return _claimed(snapshot) if slot.rows else snapshot

    async def run_model(*args, **kwargs):
        nonlocal model_cancelled
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=30)
        except asyncio.CancelledError:
            model_cancelled = True
            raise
        return "not json"

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    task = asyncio.create_task(warm_task_brief._prepare_warm_task_brief(state, slot))
    try:
        await asyncio.wait_for(entered.wait(), timeout=30)
        state._foreground_slots.clear()
        warm_task_brief.cancel_warm_task_brief(state, slot.key)

        assert not task.done()
        assert [row["meta"]["kind"] for row in _posted(slot)] == ["warm_task_brief_preparing"]
        release.set()
        await asyncio.wait_for(task, timeout=30)

        assert model_cancelled is False
        assert all(row.get("meta", {}).get("kind") != "warm_task_brief_result" for row in slot.rows)
        assert all(
            row.get("meta", {}).get("kind") != "warm_task_brief_failure" for row in slot.rows
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)


@pytest.mark.parametrize(
    ("request_text", "language"),
    [
        ("Fix the failing test", "en"),
        ("修复失败的测试", "zh"),
        ("テストを直してください", "ja"),
        ("테스트를 고쳐 주세요", "ko"),
        ("Исправь тест", "ru"),
        ("", "en"),
    ],
)
def test_opener_follows_the_script_of_the_request(request_text, language):
    assert warm_task_brief._language_of(request_text) == language
    assert warm_task_brief._OPENER_TEXT[language]
    assert warm_task_brief._FAILURE_TEXT[language]


@pytest.mark.parametrize(
    ("verbosity", "level"),
    [
        ("answer_only", "answer_only"),
        ("concise", "concise"),
        ("default", "default"),
        ("bogus", "default"),
        (None, "default"),
    ],
)
def test_prompt_follows_the_reply_style_setting(verbosity, level):
    prompt = warm_task_brief._build_prompt(_snapshot(), verbosity)
    assert warm_task_brief._STYLE_HINTS[level] in prompt


def test_every_text_label_language_has_every_label():
    assert set(warm_task_brief._TEXT_LABELS) == set(warm_task_brief._OPENER_TEXT)
    for labels in warm_task_brief._TEXT_LABELS.values():
        assert set(labels) == set(warm_task_brief._TEXT_LABEL_KEYS)
        assert set(warm_task_brief._STATUSES) <= set(labels)
        assert all(labels.values())
        assert labels["count"].format(accepted=1, total=2) != labels["count"]


@pytest.mark.parametrize("kind", [[], {}, ["warm_task_brief_result"], None, 3])
def test_non_string_meta_kind_is_not_a_brief_row_and_never_raises(kind):
    from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

    assert is_warm_task_brief_message({"role": "user", "meta": {"kind": kind}}) is False


def _arm_model(monkeypatch, slot, snapshots, run_model=None):
    async def read_snapshot(*args, **kwargs):
        return snapshots.pop(0)

    async def default_model(*args, **kwargs):
        return '{"message":"Here is where we are."}'

    monkeypatch.setattr(warm_task_brief, "_IDLE_POLL_SECS", 0)
    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model or default_model)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )


def _foreground_slot(state: _State) -> _Slot:
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    return slot


def _kinds(slot: _Slot) -> list[str]:
    return [row.get("meta", {}).get("kind", "") for row in slot.rows]


class _BusySlot(_Slot):
    """A slot whose turn ends after a set number of ``running`` reads."""

    def __init__(self, busy_reads: int) -> None:
        super().__init__()
        self.busy_reads = busy_reads

    @property
    def running(self) -> bool:
        if self.busy_reads <= 0:
            return False
        self.busy_reads -= 1
        return True

    @running.setter
    def running(self, value: bool) -> None:
        self.busy_reads = 0


class _LiveState(_State):
    """A state whose real slots route ``append`` callbacks back to it."""

    def on_message(self, key: str, msg: dict) -> None:
        self.frames.append(("chat_message", msg))

    def flush_slot_now(self, slot) -> bool:
        return True


def _live_slot(state: _LiveState):
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("thread-x")
    slot.mode = "member"
    slot._on_message = state.on_message
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    return slot


def _live_kinds(slot) -> list[str]:
    return [(row.get("meta") or {}).get("kind", "") for row in slot.messages]


def _spy_wait(monkeypatch) -> asyncio.Event:
    """An event set once the brief polls the turn it waits for."""
    waiting = asyncio.Event()
    wait = warm_task_brief._wait_for_idle_turn

    async def spy(state, slot, deadline):
        if slot.running:
            waiting.set()
        return await wait(state, slot, deadline)

    monkeypatch.setattr(warm_task_brief, "_wait_for_idle_turn", spy)
    return waiting


async def _start_brief(state, slot) -> asyncio.Task:
    task = asyncio.create_task(warm_task_brief._prepare_warm_task_brief(state, slot))
    state._warm_task_brief_tasks[slot.key] = task
    return task


@pytest.mark.asyncio
async def test_return_during_a_turn_survives_that_turns_own_reply(monkeypatch):
    state = _LiveState()
    slot = _live_slot(state)
    base = _snapshot()
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    waiting = _spy_wait(monkeypatch)

    async def patrol_turn():
        await waiting.wait()
        # The real append path: a broadcast assistant row ends the turn.
        slot.append("assistant", "Patrol: nothing new.", "msg msg-a")

    slot.task = asyncio.create_task(patrol_turn())
    task = await _start_brief(state, slot)
    await asyncio.wait_for(task, timeout=30)

    assert _live_kinds(slot)[-1] == "warm_task_brief_result"


@pytest.mark.asyncio
async def test_typed_user_row_during_the_deferral_cancels_the_brief(monkeypatch):
    state = _LiveState()
    slot = _live_slot(state)
    _arm_model(monkeypatch, slot, [])
    waiting = _spy_wait(monkeypatch)
    release = asyncio.Event()

    async def user_turn():
        await waiting.wait()
        slot.append("user", "Actually, stop and do this instead.", "msg msg-u")
        await release.wait()

    slot.task = asyncio.create_task(user_turn())
    task = await _start_brief(state, slot)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
    finally:
        release.set()
        await asyncio.wait_for(slot.task, timeout=30)

    assert "warm_task_brief_claim" not in _live_kinds(slot)


@pytest.mark.asyncio
async def test_a_wake_row_does_not_cancel_the_brief():
    state = _State()
    task = SimpleNamespace(done=lambda: False, cancelled=False)
    task.cancel = lambda: setattr(task, "cancelled", True)
    state._warm_task_brief_tasks["thread-x"] = task

    warm_task_brief.cancel_for_live_user_row(
        state, "thread-x", "[work-ledger wake] item=it_1 status=done"
    )
    assert task.cancelled is False
    warm_task_brief.cancel_for_live_user_row(state, "thread-x", "a typed request")
    assert task.cancelled is True


def _brief_seen_by_a_real_turn(*, user_row: bool, message: str) -> list[bool]:
    """Run one real dashboard turn beside a pending brief; was it cancelled?"""
    from turn_harness import Do, SlotSpec, TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent

    seen: list[bool] = []
    brief = SimpleNamespace(done=lambda: False, cancelled=False)
    brief.cancel = lambda: setattr(brief, "cancelled", True)

    def arm(ctx) -> None:
        ctx.state._warm_task_brief_tasks[ctx.slot.key] = brief

    def look(ctx) -> None:
        seen.append(brief.cancelled)

    script = TurnScript(
        events=(AcpEvent(kind=EVENT_TEXT_CHUNK, text="Patrol: nothing new."), Do(look)),
        message=message,
        user_row=user_row,
        setup=arm,
    )
    asyncio.run(run_turn(script, slot=SlotSpec()))
    return seen


def test_an_admitted_patrol_turn_does_not_cancel_a_brief():
    assert _brief_seen_by_a_real_turn(user_row=False, message="[auto-nudge cycle 3]") == [False]


def test_a_typed_turn_cancels_a_brief():
    assert _brief_seen_by_a_real_turn(user_row=True, message="Do this next.") == [True]


@pytest.mark.asyncio
async def test_user_turn_during_the_deferral_drops_the_brief(monkeypatch):
    state = _State()
    slot = _BusySlot(busy_reads=1)
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    monkeypatch.setattr(
        warm_task_brief, "time", SimpleNamespace(time=lambda: 100.0, monotonic=lambda: 0.0)
    )
    spoke = replace(_snapshot(), last_user_at=101.0)
    _arm_model(monkeypatch, slot, [spoke])

    await asyncio.wait_for(
        warm_task_brief._prepare_warm_task_brief(state, slot),
        timeout=30,
    )

    assert slot.rows == []


@pytest.mark.asyncio
async def test_deferred_brief_is_dropped_when_the_owner_leaves(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    slot.running = True
    _arm_model(monkeypatch, slot, [])
    state._foreground_slots.clear()

    await asyncio.wait_for(
        warm_task_brief._prepare_warm_task_brief(state, slot),
        timeout=30,
    )

    assert slot.rows == []


@pytest.mark.asyncio
async def test_deferral_gives_up_at_the_deadline(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    slot.running = True
    _arm_model(monkeypatch, slot, [])
    monkeypatch.setattr(warm_task_brief, "WARM_TASK_BRIEF_DEFER_MAX_SECS", 0)

    await asyncio.wait_for(
        warm_task_brief._prepare_warm_task_brief(state, slot),
        timeout=30,
    )

    assert slot.rows == []


@pytest.mark.asyncio
async def test_patrol_turn_at_publication_defers_and_still_publishes(monkeypatch):
    state = _LiveState()
    slot = _live_slot(state)
    base = _snapshot()
    # A patrol turn and a worker report changed the task fingerprint, but
    # nothing the user typed changed.
    patrolled = replace(_claimed(base), fingerprint="after-patrol")

    async def patrol_turn():
        await waiting.wait()
        slot.append("assistant", "Patrol: one PR went green.", "msg msg-a")

    async def run_model(*args, **kwargs):
        # A patrol turn is admitted while the model runs.
        slot.task = asyncio.create_task(patrol_turn())
        return '{"message":"Here is where we are."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), patrolled], run_model)
    waiting = _spy_wait(monkeypatch)
    task = await _start_brief(state, slot)
    await asyncio.wait_for(task, timeout=30)

    kinds = _live_kinds(slot)
    assert kinds[-1] == "warm_task_brief_result"
    # The brief posts after the patrol reply, never inside the running turn.
    contents = [row["content"] for row in slot.messages]
    assert contents.index("Patrol: one PR went green.") < len(contents) - 1


def test_user_fingerprint_ignores_assistant_rows_and_ledger(monkeypatch):
    slot = _Slot()
    rows = [{"role": "user", "content": "Original request", "ts": "2026-01-02T10:00:00+00:00"}]
    state = SimpleNamespace(
        conversation_log=SimpleNamespace(derive_messages_chained=lambda key: list(rows))
    )
    item = SimpleNamespace(
        **{
            field: None
            for field in (
                "item_id title verdict decision worker_session_key status reason "
                "artifacts last_report_at created_at closed_at decided_at"
            ).split()
        },
        state="open",
        summary="one",
    )
    monkeypatch.setattr(warm_task_brief, "read_state", lambda key: {})
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, [item]))

    first = warm_task_brief._snapshot_sync(state, slot)
    rows.append({"role": "assistant", "content": "patrol answer"})
    rows.append({"role": "user", "content": "[work-ledger wake] item=it_1 status=done"})
    item.summary = "two"
    patrolled = warm_task_brief._snapshot_sync(state, slot)
    rows.append({"role": "user", "content": "please also do this"})
    sent = warm_task_brief._snapshot_sync(state, slot)

    assert first is not None and patrolled is not None and sent is not None
    assert patrolled.fingerprint != first.fingerprint
    assert patrolled.user_fingerprint == first.user_fingerprint
    assert sent.user_fingerprint != first.user_fingerprint


def test_work_ledger_wake_is_not_a_user_turn():
    turns = warm_task_brief.extract_turns(
        [{"role": "user", "content": "[work-ledger wake] item=it_1 status=done"}]
    )
    assert turns[0].injected is True


def _arm_indicator_probe(state: _State, slot: _Slot) -> list[tuple[str, bool]]:
    """Record, per posted row, whether the indicator was on when it landed."""
    seen: list[tuple[str, bool]] = []
    append = slot.append

    def spy(role, content, cls="", **kwargs):
        row = append(role, content, cls, **kwargs)
        seen.append(((row.get("meta") or {}).get("kind", ""), slot._preparing_count > 0))
        return row

    slot.append = spy
    return seen


@pytest.mark.asyncio
async def test_indicator_spans_the_opener_to_the_result(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snapshot()
    running_during_model: list[bool] = []

    async def run_model(*args, **kwargs):
        running_during_model.append(slot.running)
        return '{"message":"Here is where we are."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    seen = _arm_indicator_probe(state, slot)

    await warm_task_brief._prepare_warm_task_brief(state, slot)

    assert seen == [
        ("warm_task_brief_claim", False),
        ("warm_task_brief_preparing", True),
        ("warm_task_brief_result", True),
    ]
    # The indicator is not a turn: the brief's own deferral still sees idle.
    assert running_during_model == [False]
    assert state.preparing_pushes == [{slot.key: True}, {slot.key: False}]


@pytest.mark.asyncio
async def test_indicator_clears_after_the_failure_line(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snapshot()

    async def fail(*args, **kwargs):
        raise RuntimeError("provider failed")

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], fail)
    seen = _arm_indicator_probe(state, slot)

    await warm_task_brief._prepare_warm_task_brief(state, slot)

    assert seen[-1] == ("warm_task_brief_failure", True)
    assert slot._preparing_count == 0
    assert state.preparing_pushes[-1] == {slot.key: False}


@pytest.mark.asyncio
async def test_indicator_clears_on_a_stale_drop(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snapshot()
    sent = replace(_claimed(base), fingerprint="changed", user_fingerprint="sent")
    _arm_model(monkeypatch, slot, [base, _claimed(base), sent])

    await warm_task_brief._prepare_warm_task_brief(state, slot)

    assert "warm_task_brief_result" not in _kinds(slot)
    assert slot._preparing_count == 0
    assert state.preparing_pushes == [{slot.key: True}, {slot.key: False}]


@pytest.mark.asyncio
async def test_indicator_clears_when_the_brief_task_is_cancelled(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snapshot()
    entered = asyncio.Event()

    async def run_model(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    _arm_model(monkeypatch, slot, [base, _claimed(base)], run_model)
    task = await _start_brief(state, slot)
    await asyncio.wait_for(entered.wait(), timeout=30)
    assert slot._preparing_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=30)

    assert slot._preparing_count == 0
    assert state.preparing_pushes[-1] == {slot.key: False}


@pytest.mark.asyncio
async def test_no_indicator_when_the_brief_skips_before_the_opener(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    _arm_model(monkeypatch, slot, [_snapshot(previous_claim="fp")])

    await warm_task_brief._prepare_warm_task_brief(state, slot)

    assert state.preparing_pushes == []


@pytest.mark.asyncio
async def test_a_turn_started_while_preparing_defers_and_the_indicator_holds(monkeypatch):
    state = _LiveState()
    slot = _live_slot(state)
    base = _snapshot()
    held: list[bool] = []

    async def patrol_turn():
        await waiting.wait()
        held.append(slot._preparing_count > 0)
        slot.append("assistant", "Patrol: nothing new.", "msg msg-a")

    async def run_model(*args, **kwargs):
        slot.task = asyncio.create_task(patrol_turn())
        return '{"message":"Here is where we are."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    waiting = _spy_wait(monkeypatch)
    task = await _start_brief(state, slot)
    await asyncio.wait_for(task, timeout=30)

    assert held == [True]
    assert _live_kinds(slot)[-1] == "warm_task_brief_result"
    assert slot._preparing_count == 0


def test_structured_brief_survives_a_transcript_round_trip(tmp_path):
    from kiro_crew.history import ConversationLog

    brief = warm_task_brief._parse_result(
        '{"asked":"Fix it","headline":"Ready.","needs_you":[],"next":["Review"]}', "", (1, 1)
    )
    assert brief is not None
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.append(
        "dashboard:thread-x",
        "assistant",
        warm_task_brief._brief_text(brief, "en"),
        cls="msg msg-a",
        extra_meta={
            "kind": "warm_task_brief_result",
            "fingerprint": "fp",
            "brief": brief.as_meta(),
        },
    )
    (row,) = log.derive_messages_chained("dashboard:thread-x")
    assert row["meta"]["brief"] == brief.as_meta()
    assert row["content"] == (
        "On track: Ready.\nProgress: 1 of 1 done\nYou asked: Fix it\nNext:\n1. Review"
    )


class _CrewmateState(_State):
    """A state whose registry holds a crewmate thread and the worker slots it made."""

    def add_worker(self, creator_key: str, *, running: bool, revived: bool = False) -> _Slot:
        worker = _Slot(f"worker-{len(self.slots)}")
        worker.running = running
        if revived:
            worker._created_by = "someone-else"
            worker._revived_by = creator_key
        else:
            worker._created_by = creator_key
        self.slots[worker.key] = worker
        return worker


def _ledgerless_crewmate(
    monkeypatch, *, workers: int, finished_workers: int = 0
) -> tuple[_CrewmateState, _Slot]:
    """A crewmate thread hidden two hours, with no work-ledger or session-ledger record.

    *workers* of its worker slots are running; *finished_workers* stay open but idle.
    """
    _, slot, log = _thread(monkeypatch, user_ago=2 * 3600)
    state = _CrewmateState()
    state.conversation_log = log
    state.slots[slot.key] = slot
    for index in range(workers):
        # Every other running worker was revived rather than created by the thread.
        state.add_worker(slot.key, running=True, revived=index % 2 == 1)
    for _ in range(finished_workers):
        state.add_worker(slot.key, running=False)
    # A running worker another thread created is not this thread's work.
    state.add_worker("another-thread", running=True)
    monkeypatch.setattr(warm_task_brief, "read_state", lambda key: {})
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, []))
    monkeypatch.setattr(warm_task_brief, "_skips_logged", {})
    return state, slot


def _skip_lines(caplog, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == warm_task_brief.logger.name and r.levelno == level
    ]


@pytest.mark.asyncio
async def test_crewmate_with_open_worker_sessions_but_no_ledger_briefs(monkeypatch):
    state, slot = _ledgerless_crewmate(monkeypatch, workers=2)
    await _focus(state, 1, slot.key)
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_open_worker_count_reaches_the_brief_prompt(monkeypatch):
    state, slot = _ledgerless_crewmate(monkeypatch, workers=3)
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"now":"Three workers are still going."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    await _focus(state, 1, slot.key)
    assert len(prompts) == 1
    assert '"running_worker_sessions": 3' in prompts[0]


@pytest.mark.asyncio
async def test_finished_worker_tabs_count_neither_as_work_nor_in_the_prompt(monkeypatch):
    state, slot = _ledgerless_crewmate(monkeypatch, workers=1, finished_workers=2)
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"now":"One worker is still going."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    await _focus(state, 1, slot.key)
    assert len(prompts) == 1
    assert '"running_worker_sessions": 1' in prompts[0]


@pytest.mark.asyncio
async def test_crewmate_whose_workers_all_finished_gives_no_brief(monkeypatch, caplog):
    caplog.set_level("INFO", logger=warm_task_brief.logger.name)
    state, slot = _ledgerless_crewmate(monkeypatch, workers=0, finished_workers=2)
    await _focus(state, 1, slot.key)

    assert _brief_kinds(slot) == []
    assert _skip_lines(caplog, 20) == [f"warm task brief skipped for {slot.key}: no_open_task"]


@pytest.mark.asyncio
async def test_idle_crewmate_logs_no_open_task_once_at_info(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=warm_task_brief.logger.name)
    state, slot = _ledgerless_crewmate(monkeypatch, workers=0)
    await _focus(state, 1, slot.key)
    # A second real return to the same idle thread.
    await _focus(state, 1, "elsewhere")
    state._warm_task_brief_last_seen[slot.key] = time.time() - 2 * 3600
    await _focus(state, 1, slot.key)

    assert _brief_kinds(slot) == []
    expected = f"warm task brief skipped for {slot.key}: no_open_task"
    assert _skip_lines(caplog, 20) == [expected]
    assert _skip_lines(caplog, 10).count(expected) == 1


@pytest.mark.asyncio
async def test_busy_crewmate_without_a_typed_request_logs_no_request(monkeypatch, caplog):
    caplog.set_level("INFO", logger=warm_task_brief.logger.name)
    state, slot = _ledgerless_crewmate(monkeypatch, workers=1)
    # Only the crewmate has spoken in this thread.
    del slot.rows[0]
    await _focus(state, 1, slot.key)

    assert _brief_kinds(slot) == []
    assert _skip_lines(caplog, 20) == [f"warm task brief skipped for {slot.key}: no_request"]


def test_has_open_task_counts_open_worker_sessions():
    assert warm_task_brief._has_open_task({}, None, (), running_workers=1)
    assert not warm_task_brief._has_open_task({}, None, (), running_workers=0)


@pytest.mark.parametrize(
    ("reason", "level"),
    [
        pytest.param("on_screen", 10, id="frame_reason_stays_debug"),
        pytest.param("short_away", 10, id="short_away_stays_debug"),
        pytest.param("no_request", 20, id="no_request_is_info"),
        pytest.param("snapshot_unreadable", 20, id="unreadable_is_info"),
    ],
)
def test_skip_level_follows_the_reason(monkeypatch, caplog, reason, level):
    caplog.set_level("DEBUG", logger=warm_task_brief.logger.name)
    monkeypatch.setattr(warm_task_brief, "_skips_logged", {})
    warm_task_brief._skip("thread-x", reason)
    assert _skip_lines(caplog, level) == [f"warm task brief skipped for thread-x: {reason}"]


def test_info_skip_repeats_only_when_the_reason_changes(monkeypatch, caplog):
    caplog.set_level("INFO", logger=warm_task_brief.logger.name)
    monkeypatch.setattr(warm_task_brief, "_skips_logged", {})
    for reason in ("no_open_task", "no_open_task", "no_request", "no_open_task"):
        warm_task_brief._skip("thread-x", reason)
    assert [line.rsplit(": ", 1)[1] for line in _skip_lines(caplog, 20)] == [
        "no_open_task",
        "no_request",
        "no_open_task",
    ]


def test_info_skip_record_is_bounded(monkeypatch):
    monkeypatch.setattr(warm_task_brief, "_skips_logged", {})
    monkeypatch.setattr(warm_task_brief, "_MAX_LAST_SEEN", 2)
    for key in ("a", "b", "c"):
        warm_task_brief._skip(key, "no_open_task")
    assert list(warm_task_brief._skips_logged) == ["b", "c"]


def _work_item(state: str) -> SimpleNamespace:
    return SimpleNamespace(
        item_id="item-1",
        title="Fix the failing test",
        state=state,
        verdict="",
        decision="",
        worker_session_key="",
        status="",
        reason="",
        summary="",
        artifacts=[],
        last_report_at="",
        created_at="",
        closed_at="",
        decided_at="",
    )


@pytest.mark.asyncio
async def test_last_worker_finishing_during_the_model_call_still_publishes(monkeypatch):
    state, slot = _ledgerless_crewmate(monkeypatch, workers=1)
    (worker,) = [w for w in state.slots.values() if w is not slot and w._created_by == slot.key]

    async def run_model(*args, **kwargs):
        # The thread's only open work closes while the brief is being written.
        worker.running = False
        return '{"now":"The worker just finished."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    await _focus(state, 1, slot.key)

    assert warm_task_brief._running_workers(state, slot.key) == 0
    assert _brief_kinds(slot) == _FULL_BRIEF


@pytest.mark.asyncio
async def test_work_item_closing_during_the_model_call_still_publishes(monkeypatch):
    state, slot = _ledgerless_crewmate(monkeypatch, workers=0)
    items = [_work_item("dispatched")]
    monkeypatch.setattr(warm_task_brief, "task_records", lambda key: (None, list(items)))

    async def run_model(*args, **kwargs):
        # The ledger's only open item closes while the brief is being written.
        items[0] = _work_item("accepted")
        return '{"now":"The item was accepted."}'

    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    await _focus(state, 1, slot.key)

    assert items[0].state == "accepted"
    assert _brief_kinds(slot) == _FULL_BRIEF
