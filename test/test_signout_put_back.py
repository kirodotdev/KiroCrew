"""A turn that hits a signed-out CLI puts the person's input back on the queue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import responsive_chat as rc

start, run_chat = cr._start_next_queued_turn, cr._run_chat


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


@pytest.fixture
def slot(state):
    return state.get_or_create_slot("s1")


def _signed_out(state, monkeypatch, *, consumed: bool = False) -> None:
    from kiro_crew.acp.client import AcpAuthRequired

    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    acp = MagicMock()
    acp.context_usage_pct = MagicMock(return_value=10.0)

    async def _needs_login(_msg):
        raise AcpAuthRequired("kiro-cli is not logged in.")
        yield  # an async generator

    acp.stream = _needs_login
    acp.stream_command = _needs_login
    acp.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(acp, True, False))
    state.sessions.reset = AsyncMock()

    async def _no_drain(_state, _slot, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(cr, "_start_next_queued_turn", _no_drain)


async def _run_from_queue(state, slot, msg, row, *queued):
    await cr._run_chat(
        state, slot, msg, _directive_user_origin=True, _current_message=row, _queued_entries=queued
    )


@pytest.mark.asyncio
async def test_a_drained_person_entry_goes_back_as_the_same_entry(state, slot, monkeypatch):
    entry = {"id": "q1", "content": "first", "kind": "", "_directive_user_origin": True}
    slot.queue_append("waiting behind it")
    row = slot.append("user", "first", "msg msg-u")
    _signed_out(state, monkeypatch)

    await _run_from_queue(state, slot, "first", row, entry)

    assert slot._queue[0] is entry and entry["_responsive_row"] is row
    assert [item["content"] for item in slot._queue] == ["first", "waiting behind it"]
    assert slot._last_turn_auth_required is True


@pytest.mark.asyncio
async def test_a_merged_run_goes_back_as_its_own_entries_under_one_row(state, slot, monkeypatch):
    from kiro_crew.dashboard.slot_queue_repository import SOURCE_CHANNELS_KEY

    origin = {"_directive_user_origin": True, "_directive_channel_origin": True}
    first, second = (
        {"id": t, "content": t, "kind": "", **origin, SOURCE_CHANNELS_KEY: ("C1",)}
        for t in ("first", "second")
    )
    joined = "[2 queued messages merged]\n\nfirst\n\nsecond"
    row = slot.append("user", joined, "msg msg-u")
    _signed_out(state, monkeypatch)

    await _run_from_queue(state, slot, joined, row, first, second)

    assert slot._queue == [first, second]
    assert first["_directive_channel_origin"] is True and first[SOURCE_CHANNELS_KEY] == ("C1",)
    assert first["_responsive_row"] is row and second["_responsive_row"] is row
    assert first["_signout_put_back"] == second["_signout_put_back"]
    assert rc.reusable_row(slot, [first, second], joined) is row
    assert rc.reusable_row(slot, [first], "first") is None


@pytest.mark.asyncio
async def test_a_channel_composer_send_keeps_its_channel_stamps(state, slot, monkeypatch):
    from kiro_crew.dashboard.slot_queue_repository import source_channels_of

    slot.append("user", "from the channel", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(
        state,
        slot,
        "from the channel",
        _directive_user_origin=True,
        _directive_channel_origin=True,
        _directive_source_channels=("C1",),
    )

    (entry,) = slot._queue
    assert entry.get("_directive_channel_origin") is True
    assert source_channels_of(entry) == ("C1",)


def test_a_put_back_entry_reuses_its_row_when_the_drain_redacted_it(state, slot):
    row = slot.append("user", "token [REDACTED]", "msg msg-u")
    entry = {"id": "q1", "content": "token sk-raw", "kind": "", "_signout_put_back": "g1"}
    rc.remember_row(slot, entry, row)

    assert rc.reusable_row(slot, [entry], "token [REDACTED]") is row


@pytest.mark.asyncio
async def test_tagged_drained_entries_are_left_to_the_kind_restore(state, slot, monkeypatch):
    cron = {"id": "q1", "content": "[Cron notification] digest", "kind": "cron_notification"}
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, cron["content"], _queued_entries=(cron,))

    assert all(entry is not cron for entry in slot._queue)


@pytest.mark.asyncio
async def test_a_composer_send_gets_an_entry_with_its_own_words(state, slot, monkeypatch):
    slot.append("user", "sent from the composer", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "sent from the composer", _directive_user_origin=True)

    (entry,) = slot._queue
    assert entry["content"] == "sent from the composer"
    assert entry.get("_directive_user_origin") is True
    assert not entry.get("kind")


def test_restore_skips_an_entry_already_queued(state, slot):
    qid = slot.queue_append("already there")
    again = {"id": qid, "content": "already there", "kind": ""}
    fresh = {"id": "new", "content": "back", "kind": ""}

    assert slot.queue_restore([again, fresh]) == 1
    assert [entry["content"] for entry in slot._queue] == ["back", "already there"]


def _user_rows(slot) -> list[str]:
    return [row["content"] for row in slot.messages if row.get("role") == "user"]


async def _drain_capturing(state, slot, monkeypatch, start) -> list[tuple[str, dict]]:
    slot._last_turn_auth_required = False
    ran: list[tuple[str, dict]] = []

    async def fake_run_chat(_st, _sl, msg, **kwargs):
        ran.append((msg, kwargs))

    monkeypatch.setattr(cr, "_run_chat", fake_run_chat)
    real_config = cr.KiroCrewConfig
    cfg = MagicMock()
    cfg.load.return_value.dashboard.merge_queued_messages = True
    monkeypatch.setattr(cr, "KiroCrewConfig", cfg)
    try:
        await start(state, slot)
    finally:
        monkeypatch.setattr(cr, "KiroCrewConfig", real_config)
    if slot.task is not None:
        await slot.task
    return ran


async def _fail_then_redrain(state, slot, monkeypatch) -> tuple[list[str], list[str]]:
    ((msg, kwargs),) = await _drain_capturing(state, slot, monkeypatch, start)
    after_first = _user_rows(slot)
    monkeypatch.setattr(cr, "_run_chat", run_chat)
    _signed_out(state, monkeypatch)
    await run_chat(state, slot, msg, **kwargs)
    assert slot._queue, "nothing was put back"
    ran = await _drain_capturing(state, slot, monkeypatch, start)
    assert [m for m, _ in ran] == [msg], "the second drain did not run what failed"
    return after_first, _user_rows(slot)


def _expands_prompts(monkeypatch) -> None:
    expand = AsyncMock(return_value=("The expanded checklist prompt.", "ok"))
    monkeypatch.setattr(cr, "_expand_prompt_mention_off_loop", expand)


@pytest.mark.asyncio
@pytest.mark.parametrize("texts", [["only one"], ["one", "two"], ["/prompts get checklist"]])
async def test_a_put_back_queued_send_writes_no_second_row(state, slot, monkeypatch, texts):
    for text in texts:
        slot.queue_append(text, directive_user_origin=True)
    _expands_prompts(monkeypatch)

    first, second = await _fail_then_redrain(state, slot, monkeypatch)

    assert len(first) == 1 and all(text in first[0] for text in texts)
    assert second == first


def test_a_put_back_group_drains_whole_with_merging_off(slot):
    from kiro_crew.dashboard.chat_utils_injected_rows import _dequeue_next_message

    for text in ("one", "two", "later"):
        slot.queue_append(text, directive_user_origin=True)
    slot._queue[0]["_signout_put_back"] = slot._queue[1]["_signout_put_back"] = "g1"

    msg, items = _dequeue_next_message(slot, merge_enabled=False)
    assert [i["content"] for i in items] == ["one", "two"] and "later" not in msg


@pytest.mark.asyncio
async def test_run_now_on_one_card_of_a_put_back_group_runs_only_that_card(
    state, slot, monkeypatch
):
    for text in ("one", "two"):
        slot.queue_append(text, directive_user_origin=True)
        slot._queue[-1]["_signout_put_back"] = "g1"
    second = slot._queue[1]["id"]

    async def run_now(st, sl):
        return await start(st, sl, allow_user_during_subagents=True, required_queue_id=second)

    ran = await _drain_capturing(state, slot, monkeypatch, run_now)
    assert [m for m, _ in ran] == ["two"]


@pytest.mark.asyncio
async def test_a_send_behind_held_prompts_still_passes_a_running_child(state, slot, monkeypatch):
    slot.queue_append("held", directive_user_origin=True)
    slot._queue[0].update({"_signout_put_back": "g1", "_signout_retained": True})
    slot.queue_append("new", directive_user_origin=True)
    slot._queue[1][rc.RESPONSIVE_KEY] = True
    slot._subagent_deliveries_inflight = 1

    ran = await _drain_capturing(state, slot, monkeypatch, start)
    assert [m for m, _ in ran] == ["held"]


@pytest.mark.asyncio
async def test_a_put_back_entry_does_not_merge_with_a_later_send(state, slot, monkeypatch):
    put_back = slot.queue_insert(0, "earlier", directive_user_origin=True)
    slot._queue[0]["_signout_put_back"] = True
    slot.queue_append("later", directive_user_origin=True)

    ran = await _drain_capturing(state, slot, monkeypatch, cr._start_next_queued_turn)

    assert [m for m, _ in ran] == ["earlier"]
    assert [entry["content"] for entry in slot._queue] == ["later"]
    assert put_back not in {entry["id"] for entry in slot._queue}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["from the composer", "/prompts get checklist"])
async def test_a_put_back_composer_send_goes_back_as_typed_with_no_second_row(
    state, slot, monkeypatch, text
):
    slot.append("user", text, "msg msg-u")
    _signed_out(state, monkeypatch)
    _expands_prompts(monkeypatch)

    await run_chat(state, slot, text, _directive_user_origin=True)
    ran = await _drain_capturing(state, slot, monkeypatch, start)

    assert [m for m, _ in ran] == [text]
    assert _user_rows(slot) == [text]


@pytest.mark.asyncio
async def test_a_drained_prompts_get_goes_back_as_the_same_entry(state, slot, monkeypatch):
    text = "/prompts get checklist"
    entry = {"id": "q1", "content": text, "kind": "", "_directive_user_origin": True}
    row = slot.append("user", text, "msg msg-u")
    _signed_out(state, monkeypatch)
    _expands_prompts(monkeypatch)

    await _run_from_queue(state, slot, text, row, entry)

    assert slot._queue == [entry] and slot._queue[0] is entry


def test_each_member_of_a_merged_put_back_counts_as_already_written(state, slot):
    row = slot.append("user", "[2 queued messages merged]\n\nfirst\n\nsecond", "msg msg-u")
    first = {"id": "q1", "content": "first", "kind": "", "_signout_put_back": "g1"}
    second = {"id": "q2", "content": "second", "kind": "", "_signout_put_back": "g1"}
    for entry in (first, second):
        rc.remember_row(slot, entry, row)
        entry[rc.SHARED_ROW_KEY] = 2
    slot._queue[:] = [first, second]

    assert rc.row_already_written(slot, first) and rc.row_already_written(slot, second)


@pytest.mark.asyncio
async def test_a_redacted_thread_send_goes_back_under_its_row(state, slot, monkeypatch):
    row = slot.append("user", "token [REDACTED]", "msg msg-u")
    _signed_out(state, monkeypatch)

    await run_chat(
        state,
        slot,
        "token sk-raw",
        _directive_user_origin=True,
        _directive_channel_origin=True,
        _directive_source_channels=("C1",),
        _current_message=row,
    )
    (entry,) = slot._queue
    assert entry["content"] == "token sk-raw"
    ran = await _drain_capturing(state, slot, monkeypatch, start)

    assert [m for m, _ in ran] == ["token sk-raw"]
    assert _user_rows(slot) == ["token [REDACTED]"]


@pytest.mark.asyncio
async def test_a_composer_send_goes_back_charged_to_its_author(state, slot, monkeypatch):
    from kiro_crew.shared_crew import turn_gate
    from kiro_crew.shared_crew.attribution import author_meta
    from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY
    from kiro_crew.shared_crew.identity import (
        ActorContext,
        Provenance,
        Transport,
        tailnet_principal,
    )

    alice = tailnet_principal("alice@example.com", "laptop.tail.ts.net", Provenance.TAILSCALE_SERVE)
    author = author_meta(ActorContext(principal=alice, request_id="r", transport=Transport.HTTP))
    slot.append("user", "from alice", "msg msg-u", meta={MESSAGE_AUTHOR_META_KEY: author})
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "from alice", _directive_user_origin=True)

    assert turn_gate.queued_initiator(slot._queue) == alice.id


@pytest.mark.asyncio
async def test_drained_entries_go_back_without_their_ended_admission_work(state, slot, monkeypatch):
    from kiro_crew.shared_crew.admission_entry import QUEUED_WORK_META_KEY, queued_work_ids

    first = {"id": "q1", "content": "one", "kind": "", "meta": {QUEUED_WORK_META_KEY: "w1"}}
    second = {"id": "q2", "content": "two", "kind": "", "meta": {QUEUED_WORK_META_KEY: "w2"}}
    joined = "[2 queued messages merged]\n\none\n\ntwo"
    row = slot.append("user", joined, "msg msg-u")
    _signed_out(state, monkeypatch)

    await _run_from_queue(state, slot, joined, row, first, second)

    assert [entry["id"] for entry in slot._queue] == ["q1", "q2"]
    assert queued_work_ids(slot._queue) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_text, kwargs",
    [
        ("same", {"_turn_actor": "app"}),
        ("same", {"_message_is_typed": False}),
        ("another row", {}),
    ],
    ids=["app_send", "composed_prompt", "no_row_of_its_own"],
)
async def test_a_send_that_is_not_a_persons_typed_row_is_not_put_back(
    state, slot, monkeypatch, row_text, kwargs
):
    slot.append("user", row_text, "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "same", _directive_user_origin=True, **kwargs)

    assert slot._queue == []
