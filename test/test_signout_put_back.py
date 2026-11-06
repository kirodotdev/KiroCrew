"""A turn that hits a signed-out CLI puts the person's input back on the queue.

``_requeue_auth_retry`` restores the person's own input as well as
runner-authored, synthesis and sub-agent input, so post-login resume runs it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr


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


@pytest.mark.asyncio
async def test_a_drained_person_entry_goes_back_as_the_same_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    entry = {"id": "q1", "content": "first", "kind": "", "_directive_user_origin": True}
    slot.queue_append("waiting behind it")
    row = slot.append("user", "first", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(
        state,
        slot,
        "first",
        _directive_user_origin=True,
        _current_message=row,
        _queued_entries=(entry,),
    )

    assert slot._queue[0] is entry and entry["_responsive_row"] is row
    assert [item["content"] for item in slot._queue] == ["first", "waiting behind it"]
    assert slot._last_turn_auth_required is True


@pytest.mark.asyncio
async def test_a_merged_run_goes_back_as_its_own_entries_under_one_row(tmp_path, monkeypatch):
    """Each entry keeps its own channel stamps, and the run shares the row it ran under."""
    from kiro_crew.dashboard.slot_queue_repository import SOURCE_CHANNELS_KEY

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    first = {
        "id": "q1",
        "content": "first",
        "kind": "",
        "_directive_user_origin": True,
        "_directive_channel_origin": True,
        SOURCE_CHANNELS_KEY: ("C1",),
    }
    second = {
        "id": "q2",
        "content": "second",
        "kind": "",
        "_directive_user_origin": True,
        "_directive_channel_origin": True,
        SOURCE_CHANNELS_KEY: ("C1",),
    }
    joined = "[2 queued messages merged]\n\nfirst\n\nsecond"
    row = slot.append("user", joined, "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(
        state,
        slot,
        joined,
        _directive_user_origin=True,
        _current_message=row,
        _queued_entries=(first, second),
    )

    assert slot._queue == [first, second]
    assert first["_directive_channel_origin"] is True and first[SOURCE_CHANNELS_KEY] == ("C1",)
    assert first["_responsive_row"] is row and second["_responsive_row"] is row
    assert first["_signout_put_back"] == second["_signout_put_back"]


@pytest.mark.asyncio
async def test_a_channel_composer_send_keeps_its_channel_stamps(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_queue_repository import source_channels_of

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
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


def test_a_put_back_entry_reuses_its_row_when_the_drain_redacted_it(tmp_path, monkeypatch):
    from kiro_crew.dashboard import responsive_chat

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    row = slot.append("user", "token [REDACTED]", "msg msg-u")
    entry = {"id": "q1", "content": "token sk-raw", "kind": "", "_signout_put_back": "g1"}
    responsive_chat.remember_row(slot, entry, row)

    assert responsive_chat.reusable_row(slot, [entry], "token [REDACTED]") is row


@pytest.mark.asyncio
async def test_tagged_drained_entries_are_left_to_the_kind_restore(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    cron = {"id": "q1", "content": "[Cron notification] digest", "kind": "cron_notification"}
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, cron["content"], _queued_entries=(cron,))

    assert all(entry is not cron for entry in slot._queue)


@pytest.mark.asyncio
async def test_a_composer_send_gets_an_entry_with_its_own_words(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "sent from the composer", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "sent from the composer", _directive_user_origin=True)

    (entry,) = slot._queue
    assert entry["content"] == "sent from the composer"
    assert entry.get("_directive_user_origin") is True
    assert not entry.get("kind")


@pytest.mark.asyncio
async def test_an_app_send_is_not_put_back_as_a_person(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "from an app", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "from an app", _directive_user_origin=True, _turn_actor="app")

    assert slot._queue == []


def test_restore_skips_an_entry_already_queued(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    qid = slot.queue_append("already there")
    again = {"id": qid, "content": "already there", "kind": ""}
    fresh = {"id": "new", "content": "back", "kind": ""}

    assert slot.queue_restore([again, fresh]) == 1
    assert [entry["content"] for entry in slot._queue] == ["back", "already there"]


def _user_rows(slot) -> list[str]:
    return [row["content"] for row in slot.messages if row.get("role") == "user"]


async def _drain_capturing(state, slot, monkeypatch, start) -> list[tuple[str, dict]]:
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
    """Drain once into a signed-out CLI, then drain what was put back."""
    start = cr._start_next_queued_turn
    run_chat = cr._run_chat
    ((msg, kwargs),) = await _drain_capturing(state, slot, monkeypatch, start)
    after_first = _user_rows(slot)
    monkeypatch.setattr(cr, "_run_chat", run_chat)
    _signed_out(state, monkeypatch)
    await run_chat(state, slot, msg, **kwargs)
    assert slot._queue, "nothing was put back"
    slot._last_turn_auth_required = False
    ran = await _drain_capturing(state, slot, monkeypatch, start)
    assert [m for m, _ in ran] == [msg], "the second drain did not run what failed"
    return after_first, _user_rows(slot)


@pytest.mark.asyncio
async def test_a_put_back_entry_writes_no_second_row(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.queue_append("only one", directive_user_origin=True)

    first, second = await _fail_then_redrain(state, slot, monkeypatch)

    assert first == ["only one"]
    assert second == first


@pytest.mark.asyncio
async def test_a_put_back_merged_run_writes_no_second_row(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.queue_append("one", directive_user_origin=True)
    slot.queue_append("two", directive_user_origin=True)

    first, second = await _fail_then_redrain(state, slot, monkeypatch)

    assert len(first) == 1 and "one" in first[0] and "two" in first[0]
    assert second == first


@pytest.mark.asyncio
async def test_a_put_back_entry_does_not_merge_with_a_later_send(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    put_back = slot.queue_insert(0, "earlier", directive_user_origin=True)
    slot._queue[0]["_signout_put_back"] = True
    slot.queue_append("later", directive_user_origin=True)

    ran = await _drain_capturing(state, slot, monkeypatch, cr._start_next_queued_turn)

    assert [m for m, _ in ran] == ["earlier"]
    assert [entry["content"] for entry in slot._queue] == ["later"]
    assert put_back not in {entry["id"] for entry in slot._queue}


@pytest.mark.asyncio
async def test_a_put_back_composer_send_writes_no_second_row(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "from the composer", "msg msg-u")
    start = cr._start_next_queued_turn
    run_chat = cr._run_chat
    _signed_out(state, monkeypatch)

    await run_chat(state, slot, "from the composer", _directive_user_origin=True)
    slot._last_turn_auth_required = False
    ran = await _drain_capturing(state, slot, monkeypatch, start)

    assert [m for m, _ in ran] == ["from the composer"]
    assert _user_rows(slot) == ["from the composer"]


def _expands_prompts(monkeypatch) -> None:
    expand = AsyncMock(return_value=("The expanded checklist prompt.", "ok"))
    monkeypatch.setattr(cr, "_expand_prompt_mention_off_loop", expand)


@pytest.mark.asyncio
async def test_a_composer_prompts_get_goes_back_as_what_was_typed(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "/prompts get checklist", "msg msg-u")
    start = cr._start_next_queued_turn
    run_chat = cr._run_chat
    _signed_out(state, monkeypatch)
    _expands_prompts(monkeypatch)

    await run_chat(state, slot, "/prompts get checklist", _directive_user_origin=True)
    assert [entry["content"] for entry in slot._queue] == ["/prompts get checklist"]
    slot._last_turn_auth_required = False
    ran = await _drain_capturing(state, slot, monkeypatch, start)

    assert [m for m, _ in ran] == ["/prompts get checklist"]
    assert _user_rows(slot) == ["/prompts get checklist"]


@pytest.mark.asyncio
async def test_a_queued_prompts_get_goes_back_as_its_own_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.queue_append("/prompts get checklist", directive_user_origin=True)
    _expands_prompts(monkeypatch)

    first, second = await _fail_then_redrain(state, slot, monkeypatch)

    assert first == ["/prompts get checklist"]
    assert second == first


@pytest.mark.asyncio
async def test_a_drained_prompts_get_goes_back_as_the_same_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    entry = {
        "id": "q1",
        "content": "/prompts get checklist",
        "kind": "",
        "_directive_user_origin": True,
    }
    row = slot.append("user", "/prompts get checklist", "msg msg-u")
    _signed_out(state, monkeypatch)
    _expands_prompts(monkeypatch)

    await cr._run_chat(
        state,
        slot,
        "/prompts get checklist",
        _directive_user_origin=True,
        _current_message=row,
        _queued_entries=(entry,),
    )

    assert slot._queue == [entry] and slot._queue[0] is entry


def test_a_merged_put_back_entry_counts_as_already_written(tmp_path, monkeypatch):
    from kiro_crew.dashboard import responsive_chat

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    row = slot.append("user", "[2 queued messages merged]\n\nfirst\n\nsecond", "msg msg-u")
    first = {"id": "q1", "content": "first", "kind": "", "_signout_put_back": "g1"}
    second = {"id": "q2", "content": "second", "kind": "", "_signout_put_back": "g1"}
    for entry in (first, second):
        responsive_chat.remember_row(slot, entry, row)
    slot._queue[:] = [first, second]

    assert responsive_chat.row_already_written(slot, first) is True
    assert responsive_chat.row_already_written(slot, second) is True


@pytest.mark.asyncio
async def test_a_composed_prompt_is_not_put_back(tmp_path, monkeypatch):
    """The OpenAI-compatible endpoint's client is told its request failed."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "system: be brief\nuser: hi", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(
        state,
        slot,
        "system: be brief\nuser: hi",
        _directive_user_origin=True,
        _message_is_typed=False,
    )

    assert slot._queue == []


@pytest.mark.asyncio
async def test_a_redacted_thread_send_goes_back_under_its_row(tmp_path, monkeypatch):
    """A linked thread's row holds the redacted text; the turn ran the raw text."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    row = slot.append("user", "token [REDACTED]", "msg msg-u")
    start = cr._start_next_queued_turn
    run_chat = cr._run_chat
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
    slot._last_turn_auth_required = False
    ran = await _drain_capturing(state, slot, monkeypatch, start)

    assert [m for m, _ in ran] == ["token sk-raw"]
    assert _user_rows(slot) == ["token [REDACTED]"]


@pytest.mark.asyncio
async def test_a_composer_send_goes_back_charged_to_its_author(tmp_path, monkeypatch):
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
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "from alice", "msg msg-u", meta={MESSAGE_AUTHOR_META_KEY: author})
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "from alice", _directive_user_origin=True)

    assert turn_gate.queued_initiator(slot._queue) == alice.id


@pytest.mark.asyncio
async def test_drained_entries_go_back_without_their_ended_admission_work(tmp_path, monkeypatch):
    """The merged turn ended the parked work; a stale id would read as cancelled."""
    from kiro_crew.shared_crew.admission_entry import QUEUED_WORK_META_KEY, queued_work_ids

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    first = {"id": "q1", "content": "one", "kind": "", "meta": {QUEUED_WORK_META_KEY: "w1"}}
    second = {"id": "q2", "content": "two", "kind": "", "meta": {QUEUED_WORK_META_KEY: "w2"}}
    joined = "[2 queued messages merged]\n\none\n\ntwo"
    row = slot.append("user", joined, "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(
        state,
        slot,
        joined,
        _directive_user_origin=True,
        _current_message=row,
        _queued_entries=(first, second),
    )

    assert [entry["id"] for entry in slot._queue] == ["q1", "q2"]
    assert queued_work_ids(slot._queue) == []


@pytest.mark.asyncio
async def test_a_send_with_no_row_of_its_own_is_not_put_back(tmp_path, monkeypatch):
    """With no row to tie it to, the drain would write the message a second time."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "some other row", "msg msg-u")
    _signed_out(state, monkeypatch)

    await cr._run_chat(state, slot, "no row was written for this", _directive_user_origin=True)

    assert slot._queue == []
    assert slot._last_turn_auth_required is True
