from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import slot_retention, warm_task_brief


class _State:
    def __init__(self) -> None:
        self._foreground_slots: dict[int, str] = {}
        self._warm_task_brief_away: dict[str, tuple[float, int]] = {}
        self._warm_task_brief_generation = 0
        self._warm_task_brief_slot_generation: dict[str, int] = {"thread-x": 1}
        self._warm_task_brief_tasks = {}
        self._warm_task_brief_model_active: set[str] = set()
        self.frames: list[tuple[str, dict]] = []
        self.preparing_pushes: list[dict[str, bool]] = []
        self.slots = {}
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


def _apply_presence(state: _State, conn: int, slot: object, *, hidden: object = False, now: float):
    candidate = warm_task_brief.note_focus_frame(state, conn, slot, hidden=hidden, now=now)
    slot_retention.note_foreground(state, conn, slot)
    return candidate


def test_visible_for_thirty_minutes_has_no_episode_or_return():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    candidate = _apply_presence(state, 1, "thread-x", now=3600)
    assert candidate is None
    assert state._warm_task_brief_away == {}
    assert state.frames == []


def test_visible_but_unfocused_has_no_signal_or_episode():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    # Window blur sends no frame at all.
    assert state._foreground_slots == {1: "thread-x"}
    assert state._warm_task_brief_away == {}


def test_wall_clock_rollback_cannot_make_a_return_eligible(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    state._warm_task_brief_away[slot.key] = (100.0, 1)
    monkeypatch.setattr(warm_task_brief, "time", SimpleNamespace(time=lambda: 50.0))

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert state._warm_task_brief_tasks == {}


def test_hidden_only_arms_and_does_not_trigger():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    candidate = _apply_presence(state, 1, None, hidden=True, now=10)
    assert candidate is None
    assert state._warm_task_brief_away == {"thread-x": (10, 1)}
    assert state.frames == []


def test_short_same_thread_return_is_identified_but_below_threshold():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    _apply_presence(state, 1, None, hidden=True, now=10)
    candidate = _apply_presence(state, 1, "thread-x", now=20)
    assert candidate == warm_task_brief.ReturnCandidate("thread-x", 10, 1)
    assert 20 - candidate.away_started < warm_task_brief.WARM_TASK_BRIEF_AWAY_SECS
    assert state._warm_task_brief_away == {}


def test_last_owner_tab_hidden_arms_one_episode():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    _apply_presence(state, 2, "thread-x", now=0)
    _apply_presence(state, 1, None, hidden=True, now=10)
    assert state._warm_task_brief_away == {}
    _apply_presence(state, 2, None, hidden=True, now=20)
    assert state._warm_task_brief_away == {"thread-x": (20, 1)}


def test_unreasoned_null_never_arms():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    _apply_presence(state, 1, None, now=20)
    assert state._warm_task_brief_away == {}


def test_switching_to_another_thread_arms_the_one_left():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    _apply_presence(state, 1, "thread-y", now=10)
    assert state._warm_task_brief_away == {"thread-x": (10, 1)}
    assert _apply_presence(state, 1, "thread-x", now=2000) == warm_task_brief.ReturnCandidate(
        "thread-x", 10, 1
    )
    # Coming back to x is itself a switch away from y.
    assert state._warm_task_brief_away == {"thread-y": (2000, 2)}


def test_switch_does_not_arm_while_another_tab_shows_the_thread():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    _apply_presence(state, 2, "thread-x", now=0)
    _apply_presence(state, 1, "thread-y", now=10)
    assert state._warm_task_brief_away == {}


def test_episode_survives_frames_for_other_threads():
    # The reported case: leave the conductor for another chat, hide the page
    # there, come back to that chat, then open the conductor again.
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    _apply_presence(state, 1, "thread-y", now=10)
    _apply_presence(state, 1, None, hidden=True, now=20)
    _apply_presence(state, 2, "thread-z", now=30)
    _apply_presence(state, 1, "thread-y", now=40_000)
    candidate = _apply_presence(state, 1, "thread-x", now=40_010)
    assert candidate == warm_task_brief.ReturnCandidate("thread-x", 10, 1)
    assert "thread-x" not in state._warm_task_brief_away


def test_away_episodes_are_capped_oldest_first(monkeypatch):
    monkeypatch.setattr(warm_task_brief, "_MAX_AWAY_EPISODES", 2)
    state = _State()
    for index, key in enumerate(["a", "b", "c", "d"]):
        _apply_presence(state, 1, key, now=index)
    assert set(state._warm_task_brief_away) == {"b", "c"}
    assert "a" not in state._warm_task_brief_slot_generation


def test_socket_cleanup_clears_retention_only_and_never_arms():
    state = _State()
    _apply_presence(state, 1, "thread-x", now=0)
    slot_retention.note_foreground(state, 1, None)
    assert state._foreground_slots == {}
    assert state._warm_task_brief_away == {}


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
        previous_claim=previous_claim,
    )


def _claimed(snapshot: warm_task_brief.TaskSnapshot) -> warm_task_brief.TaskSnapshot:
    return replace(snapshot, previous_claim=snapshot.fingerprint)


def _posted(slot: _Slot) -> list[dict]:
    return [row for row in slot.rows if row["role"] == "assistant"]


@pytest.mark.asyncio
async def test_opener_precedes_the_single_model_call_and_result_is_plain_prose(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()

    async def read_snapshot(*args):
        return _claimed(snapshot) if slot.flushed else snapshot

    kinds_at_call = []
    call_kwargs = {}

    async def run_model(*args, **kwargs):
        kinds_at_call.extend(row["meta"]["kind"] for row in slot.rows)
        call_kwargs.update(kwargs)
        call_kwargs["prompt"] = args[1]
        return (
            '{"message":"You asked me to fix the failing test. I reproduced the failure. '
            'Want me to **apply** the fix next?"}'
        )

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", run_model)
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )

    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )

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
    assert result["meta"] == {"kind": "warm_task_brief_result", "fingerprint": "fp"}
    assert result["content"] == (
        "You asked me to fix the failing test. I reproduced the failure. "
        "Want me to apply the fix next?"
    )
    assert '"original_request": "Fix the failing test"' in call_kwargs["prompt"]
    assert "No headings, no bold" in call_kwargs["prompt"]


@pytest.mark.asyncio
async def test_existing_claim_suppresses_status_and_model(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key

    async def read_snapshot(*args):
        return _snapshot(previous_claim="fp")

    async def unexpected(*args, **kwargs):
        raise AssertionError("model must not run")

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", unexpected)
    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )
    assert state.frames == []
    assert slot.rows == []


@pytest.mark.asyncio
async def test_model_failure_posts_an_ordinary_failure_message(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()

    async def read_snapshot(*args):
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
    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )
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

    async def read_snapshot(*args):
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
    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )
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
    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )
    assert slot.rows == []
    assert state.frames == []


@pytest.mark.asyncio
async def test_handle_focus_starts_once_only_on_eligible_return(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    calls = []

    async def prepare(*args):
        calls.append(args[2])

    monkeypatch.setattr(warm_task_brief, "_prepare_warm_task_brief", prepare)
    state._warm_task_brief_away[slot.key] = (
        warm_task_brief.time.time() - warm_task_brief.WARM_TASK_BRIEF_AWAY_SECS,
        1,
    )
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    slot_retention.note_foreground(state, 1, slot.key)
    await asyncio.sleep(0)
    warm_task_brief.handle_focus_frame(state, 1, slot.key)
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert calls[0].slot_key == slot.key
    assert calls[0].generation == 1


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
async def test_second_leave_cancels_pre_model_task_and_advances_generation():
    state = _State()
    state._foreground_slots[1] = "thread-x"
    task = asyncio.create_task(asyncio.sleep(60))
    state._warm_task_brief_tasks["thread-x"] = task

    warm_task_brief.note_focus_frame(state, 1, None, hidden=True, now=20)
    await asyncio.sleep(0)

    assert task.cancelled()
    assert state.frames == []
    assert state._warm_task_brief_slot_generation["thread-x"] == 1


def test_ordinary_chat_return_never_starts_a_brief():
    state = _State()
    slot = _Slot()
    slot.mode = "chat"
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    state._warm_task_brief_away[slot.key] = (
        warm_task_brief.time.time() - warm_task_brief.WARM_TASK_BRIEF_AWAY_SECS,
        1,
    )

    warm_task_brief.handle_focus_frame(state, 1, slot.key)

    assert state._warm_task_brief_tasks == {}


def test_gateway_restart_has_no_durable_away_episode():
    restarted = _State()
    restarted._foreground_slots[1] = "thread-x"
    warm_task_brief.handle_focus_frame(restarted, 1, "thread-x")
    assert restarted._warm_task_brief_tasks == {}
    assert restarted._warm_task_brief_away == {}


@pytest.mark.asyncio
async def test_unpersisted_claim_never_surfaces_status_or_spends(monkeypatch):
    state = _State()
    slot = _Slot()
    state.slots[slot.key] = slot
    state._foreground_slots[1] = slot.key
    snapshot = _snapshot()
    state.flush_slot_now = lambda _slot: None

    async def read_snapshot(*args):
        return snapshot

    async def unexpected(*args, **kwargs):
        raise AssertionError("model must not run without a durable claim")

    monkeypatch.setattr(warm_task_brief, "_snapshot", read_snapshot)
    monkeypatch.setattr(warm_task_brief, "run_bg_oneliner", unexpected)
    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )

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

    async def read_snapshot(*args):
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
    task = asyncio.create_task(
        warm_task_brief._prepare_warm_task_brief(
            state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
        )
    )
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
    "text",
    ['{"current_state":["x"],"next_steps":["y"]}', '{"message":"   "}', '{"message":3}', "no json"],
)
def test_result_without_a_prose_message_is_refused(text):
    assert warm_task_brief._parse_result(text) is None


@pytest.mark.parametrize("kind", [[], {}, ["warm_task_brief_result"], None, 3])
def test_non_string_meta_kind_is_not_a_brief_row_and_never_raises(kind):
    from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

    assert is_warm_task_brief_message({"role": "user", "meta": {"kind": kind}}) is False


def _arm_model(monkeypatch, slot, snapshots, run_model=None):
    async def read_snapshot(*args):
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

    async def spy(state, slot, generation, deadline):
        if slot.running:
            waiting.set()
        return await wait(state, slot, generation, deadline)

    monkeypatch.setattr(warm_task_brief, "_wait_for_idle_turn", spy)
    return waiting


async def _start_brief(state, slot) -> asyncio.Task:
    task = asyncio.create_task(
        warm_task_brief._prepare_warm_task_brief(
            state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
        )
    )
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
        warm_task_brief._prepare_warm_task_brief(
            state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
        ),
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
        warm_task_brief._prepare_warm_task_brief(
            state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
        ),
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
        warm_task_brief._prepare_warm_task_brief(
            state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
        ),
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

    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )

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

    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )

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

    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )

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

    await warm_task_brief._prepare_warm_task_brief(
        state, slot, warm_task_brief.ReturnCandidate(slot.key, 0, 1)
    )

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
