"""The warm welcome redesign, one test group per root cause of the analysis.

RC1 the payload was sliced at 40,000 characters and lost the transcript; RC2
assistant turns were cut to 400+400 characters and the pending ask with them;
RC3 the reply language followed a guessed "original request"; RC6 a
conductor's own crewmate thread got the crewmate prompt; RC8 a separate opener
could be orphaned. RC4 and RC5 (live PR state, buttons, links) come in a
follow-up change. RC7 (the whole-board count) is pinned in
``test_warm_task_brief_shape.py``.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_warm_task_brief import (
    _arm_model,
    _claimed,
    _focus,
    _foreground_slot,
    _iso,
    _snapshot,
    _State,
    _thread,
)

from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.history import HUMAN_TURN_META_KEY
from kiro_crew.session_summary import TranscriptTurn

_ASK = (
    "Two PRs wait on you.\n"
    + "Detail line. " * 120
    + "\n#26100 is green; I recommend merging it.\n"
    "[OPTIONS: Merge #26100 now | Hold #26100 until Monday]"
)


def _turn(index: int, role: str, text: str, ago: float, *, injected: bool = False):
    return TranscriptTurn(
        index=index, role=role, text=text, ts=_iso(time.time() - ago), injected=injected
    )


def _item(n: int, **fields) -> dict:
    item = {
        "item_id": f"it_{n:08x}",
        "title": f"Item {n}",
        "state": "accepted",
        "status": "done",
        "summary": "s" * 500,
        "decision": "d" * 2000,
        "acceptance": {"kind": "pr_checks", "pr": n, "repo": "kirodotdev/KiroCrew"},
        "artifacts": {},
        "created_at": "2026-10-10T00:00:00+00:00",
        "last_report_at": "2026-10-10T00:00:00+00:00",
    }
    item.update(fields)
    return item


class _FixedToday(_dt.datetime):
    """``datetime`` whose ``now`` never moves, so clock() picks the same
    form (with or without the date) on both sides of an assertion."""

    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2026, 10, 31, 12, 0, tzinfo=_dt.timezone.utc).astimezone(tz)


@pytest.fixture(autouse=True)
def _fixed_today(_floor_monkeypatch):
    _floor_monkeypatch.setattr(inputs, "datetime", _FixedToday)


def _payload_of(prompt: str) -> dict:
    body = prompt.split("UNTRUSTED TASK SNAPSHOT\n", 1)[1].split("\nEND UNTRUSTED", 1)[0]
    return json.loads(body)


def _snap(**fields) -> warm_task_brief.TaskSnapshot:
    return replace(_snapshot(), **fields)


# ---------------------------------------------------------------- RC1


def test_rc1_a_huge_board_never_cuts_the_payload_or_loses_the_transcript():
    away = time.time() - 3600
    items = tuple(_item(n) for n in range(170)) + (
        _item(500, state="open", status="question", title="Pick a layout"),
    )
    turns = (
        _turn(1, "user", "Ship wave 2", 7200),
        _turn(2, "assistant", "Done; the newest line is here.", 60),
    )
    snapshot = _snap(work_items=items, turns=turns, last_user_at=away)
    prompt = warm_task_brief._build_prompt(snapshot, anchor=away)
    payload = _payload_of(prompt)  # whole, valid JSON: nothing was sliced
    assert payload["transcript_since_owner"][-1]["text"] == "Done; the newest line is here."
    # Terminal items that did not change while the owner was away stay out.
    assert [entry["title"] for entry in payload["open_items"]] == ["Pick a layout"]
    order = list(payload)
    assert order.index("pending_ask") < order.index("transcript_since_owner")
    assert order.index("changes_since_away") < order.index("open_items")


def test_rc1_each_section_keeps_whole_entries_within_its_budget():
    entries = [{"text": "x" * 900} for _ in range(20)]
    kept = inputs.fit(entries, 5_000)
    assert 0 < len(kept) < 20 and all(entry == entries[0] for entry in kept)
    items = tuple(_item(n, state="open", status="progress") for n in range(100))
    open_items = inputs.open_item_entries(items, None, warm_task_brief.work_item_is_terminal)
    size = len(json.dumps(open_items, ensure_ascii=False, separators=(",", ":")))
    assert 0 < len(open_items) < 100 and size <= inputs.OPEN_ITEMS_CHARS + 2 * len(open_items)


def test_rc1_questions_and_changes_rank_first_and_changed_closes_are_kept():
    away = time.time() - 3600
    items = (
        _item(1, state="open", status="progress"),
        _item(2, state="accepted", status="done", closed_at=_iso(time.time() - 60)),
        _item(3, state="open", status="blocked", reason="needs_human"),
        _item(4, state="accepted"),
    )
    entries = inputs.open_item_entries(items, away, warm_task_brief.work_item_is_terminal)
    assert [entry["title"] for entry in entries] == ["Item 3", "Item 2", "Item 1"]
    assert entries[1]["changed_since_away"] is True
    changes = inputs.change_entries(items, away)
    assert changes == [
        {"item": "Item 2", "state": "accepted", "status": "done", "source": "work ledger"}
        | {"at": inputs.clock(inputs.parse_epoch(items[1]["closed_at"]))}
    ]


# ---------------------------------------------------------------- RC2


def test_rc2_the_pending_ask_is_read_whole_with_its_options():
    rows = [
        {"role": "user", "content": "Status?", "ts": _iso(time.time() - 7200)},
        {"role": "assistant", "content": _ASK, "ts": _iso(time.time() - 7000)},
        # The hidden claim row is a notice, not a reply.
        {"role": "notice", "content": "", "meta": {"kind": "warm_task_brief_claim"}},
    ]
    ask = inputs.pending_ask(rows)
    assert ask is not None
    assert ask.options == ("Merge #26100 now", "Hold #26100 until Monday")
    assert "#26100 is green; I recommend merging it." in ask.text
    assert "[OPTIONS" not in ask.text


@pytest.mark.parametrize(
    "answer",
    [
        {"role": "user", "content": "Hold it"},
        {"role": "assistant", "content": "Merged.", "meta": {}},
        {"role": "user", "content": "x", "meta": {HUMAN_TURN_META_KEY: True}},
        # Any newer reply retires the ask, a wake or an earlier brief included:
        # an older [OPTIONS:] turn is never brought back.
        {"role": "user", "content": "[work-ledger wake] item done"},
        {"role": "assistant", "content": "Catch-up", "meta": {"kind": "warm_task_brief_result"}},
    ],
)
def test_rc2_an_answered_ask_is_not_pending(answer):
    rows = [{"role": "assistant", "content": _ASK}, answer]
    assert inputs.pending_ask(rows) is None


def test_rc2_the_transcript_slice_starts_at_the_owners_last_message_whole():
    long_reply = "head " + "middle " * 300 + "tail [OPTIONS: A | B]"
    turns = (
        _turn(1, "user", "The 10-10 goal", 9000),
        _turn(2, "assistant", "Old reply", 8000),
        _turn(3, "user", "Now do wave 2", 7200),
        _turn(4, "user", "[work-ledger wake] x", 3000, injected=True),
        _turn(5, "assistant", long_reply, 60),
        _turn(6, "assistant", "\u200b", 30),
    )
    entries = inputs.turns_since_owner(turns)
    assert [entry["role"] for entry in entries] == ["user", "automation", "assistant"]
    assert entries[0]["text"] == "Now do wave 2"
    assert entries[-1]["text"] == long_reply


def test_rc2_over_budget_the_oldest_turns_drop_and_the_owner_message_stays():
    turns = (_turn(1, "user", "Go", 9000),) + tuple(
        _turn(n, "assistant", f"{n} " + "x" * 3000, 9000 - n) for n in range(2, 12)
    )
    entries = inputs.turns_since_owner(turns)
    assert entries[0]["text"] == "Go"
    assert entries[-1]["text"].startswith("11 ")
    assert len(entries) < len(turns)


# ---------------------------------------------------------------- RC3


@pytest.mark.asyncio
async def test_rc3_fixed_lines_follow_the_owners_recent_messages(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    turns = (
        _turn(1, "user", "Fix the failing test", 9000),
        _turn(2, "user", "现在进展如何？", 7200),
        _turn(3, "assistant", "还在跑。", 60),
    )
    base = _snap(turns=turns)

    async def fail(*args, **kwargs):
        raise RuntimeError("provider failed")

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], fail)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert slot.rows[-1]["content"] == warm_task_brief._FAILURE_TEXT["zh"]
    prompt = warm_task_brief._build_prompt(base)
    assert _payload_of(prompt)["owner_recent_messages"][-1] == "现在进展如何？"
    assert 'language of "owner_recent_messages"' in prompt


# ---------------------------------------------------------------- RC6


@pytest.mark.asyncio
async def test_rc6_a_crewmate_thread_that_owns_a_ledger_gets_the_conductor_prompt(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    assert slot.mode == "member"
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Here is where we are."}'

    base = _snapshot()  # carries a conductor goal
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert prompts[0].startswith("You are the conductor in this chat.")

    plain = _snap(conductor=None, work_items=())
    prompts.clear()
    _arm_model(monkeypatch, slot, [plain, _claimed(plain), _claimed(plain)], run_model)
    slot.rows.clear()
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert prompts[0].startswith("You are the crewmate in this chat.")


# ---------------------------------------------------------------- quiet, RC8


@pytest.mark.asyncio
async def test_a_quiet_return_posts_one_line_and_spends_nothing(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    base = _snap(turns=(_turn(1, "user", "Fix it", 9000),), work_items=())

    async def unexpected(*args, **kwargs):
        raise AssertionError("a quiet return makes no model call")

    _arm_model(monkeypatch, slot, [base, _claimed(base)], unexpected)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    rows = [row for row in slot.rows if row["role"] == "assistant"]
    assert len(rows) == 1
    assert rows[0]["content"] == warm_task_brief._QUIET_TEXT["en"].format(
        at=inputs.clock(base.last_user_at)
    )
    assert slot._preparing_count == 0 and state.preparing_pushes == []


@pytest.mark.asyncio
async def test_a_plain_chat_is_never_briefed_even_with_pending_choices(monkeypatch):
    state, slot, log = _thread(monkeypatch, user_ago=2 * 3600)
    slot.mode = "chat"
    log.rows.append({"role": "assistant", "content": _ASK, "ts": _iso(time.time() - 60)})
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(resolve_model=lambda role: "background")),
    )
    await _focus(state, 1, slot.key)
    assert [row for row in slot.rows if (row.get("meta") or {}).get("kind")] == []
    assert log.reads == 0


# ---------------------------------------------------------------- review fixes


@pytest.mark.asyncio
async def test_session_ledger_progress_while_away_is_not_a_quiet_return(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    recent = _iso(time.time() - 120)
    base = _snap(
        turns=(_turn(1, "user", "Fix it", 9000),),
        work_items=(),
        session_state={
            "phase": "implementing",
            "last_progress_at": recent,
            "events": [{"ts": recent, "kind": "progress", "text": "Rebased onto main"}],
        },
    )
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Rebased onto main."}'

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert len(prompts) == 1
    changes = _payload_of(prompts[0])["changes_since_away"]
    assert changes[0]["event"] == "Rebased onto main"
    assert changes[0]["source"] == "session ledger"


@pytest.mark.asyncio
async def test_a_question_reported_while_the_claim_settles_is_not_missed(monkeypatch):
    state = _State()
    slot = _foreground_slot(state)
    quiet = _snap(turns=(_turn(1, "user", "Fix it", 9000),), work_items=())
    # A worker asks a question between the first read and the settled re-read.
    asked = replace(
        _claimed(quiet),
        work_items=(_item(4, state="open", status="question", title="Pick a layout"),),
    )
    prompts: list[str] = []

    async def run_model(sessions, prompt, **kwargs):
        prompts.append(prompt)
        return '{"headline":"Pick a layout."}'

    _arm_model(monkeypatch, slot, [quiet, asked, asked], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert len(prompts) == 1
    assert _payload_of(prompts[0])["open_items"][0]["title"] == "Pick a layout"
    # The claim and the posted row keep the first snapshot's fingerprint.
    assert slot.rows[-1]["meta"]["fingerprint"] == quiet.fingerprint


def test_the_quiet_line_claims_only_nothing_new_and_dates_an_older_time(monkeypatch):
    from datetime import datetime as real_datetime
    from datetime import timezone

    noon = real_datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    class _Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return noon.astimezone(tz) if tz else noon.replace(tzinfo=None)

    # A frozen "now", in UTC, so the same-day test never straddles midnight.
    monkeypatch.setattr(inputs, "datetime", _Frozen)
    monkeypatch.setattr(
        "kiro_crew.cron_service.schedule.get_local_tz", lambda: ("UTC", timezone.utc)
    )
    assert all("waiting" not in text for text in warm_task_brief._QUIET_TEXT.values())
    assert inputs.clock(noon.timestamp() - 3600) == "11:00"
    assert inputs.clock(noon.timestamp() - 26 * 3600) == "2026-10-30 10:00"
