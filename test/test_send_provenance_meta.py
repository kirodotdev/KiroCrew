"""Structured sender provenance on a cross-chat delivery's transcript row.

``session_send`` / ``session_broadcast`` prefix the delivered text with a
``[sent by session … via …]`` envelope for the TARGET MODEL. The transcript draws
the same fact as a "From <chat>" label, read from ``meta.sendProvenance`` -- a
record only the gateway's delivery path writes. These pins cover every arm that
writes the row (an idle target's run arm, a busy target's queue entry and the
drain that turns it into a row, a steer that cuts into the turn, a steer the
teardown requeued, a broadcast) and the two ways the label must NOT appear: a
person's own composer send that tries to supply the key, and a merged drain row
that mixes a peer's delivery with someone else's text.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.history_schema import SEND_PROVENANCE_META_KEY

KEY = SEND_PROVENANCE_META_KEY


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


def _named(state, name: str, title: str):
    slot = _slot(state, name)
    slot.title = title
    slot._titled = True
    return slot


def test_an_idle_target_row_names_its_sender(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _peer_target(state, "chat-2", caller)

    async def _fake_run_chat(_state, _slot_, _prompt):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    async def _drive():
        await sc.send_to_target(
            state, caller_session_key=_key(caller), target="chat-2", message="do it"
        )
        # flake-ok: lets the started turn task run, as the sibling send tests do
        await asyncio.sleep(0)

    asyncio.run(_drive())

    (row,) = [r for r in _user_rows(target) if r["content"].endswith("do it")]
    # The text envelope the model reads is unchanged.
    assert row["content"].startswith("[sent by session chat-1 via session_send]\n\n")
    assert row["meta"][KEY] == {
        "from": "chat-1",
        "title": "Release conductor",
        "via": "session_send",
        "mode": "started",
    }


def test_a_busy_target_queue_entry_carries_the_sender_as_queued(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))

    asyncio.run(
        sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="later")
    )

    (entry,) = [q for q in target._queue if q["content"].endswith("later")]
    assert entry["meta"][KEY]["mode"] == "queued"
    assert entry["meta"][KEY]["from"] == "chat-1"


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


@pytest.mark.asyncio
async def test_the_drain_carries_the_sender_onto_the_row(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="queued body"
    )
    target.task = None

    await _drain_once(state, target)

    (row,) = [r for r in _user_rows(target) if r["content"].endswith("queued body")]
    assert row["meta"][KEY]["mode"] == "queued"
    assert row["meta"][KEY]["title"] == "Release conductor"


async def _drain_merged(state, slot) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import chat_runner

    cfg = KiroCrewConfig()
    cfg.dashboard.merge_queued_messages = True
    with (
        patch.object(KiroCrewConfig, "load", return_value=cfg),
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


@pytest.mark.asyncio
async def test_a_merged_row_mixing_senders_carries_no_sender(tmp_path):
    """A merged row stands for every entry's text, so one sender cannot label it."""
    state = _make_state(tmp_path)
    caller_a = _named(state, "chat-1", "A")
    caller_b = _named(state, "chat-3", "B")
    target = _busy(_peer_target(state, "chat-2", caller_a))
    await sc.send_to_target(state, caller_session_key=_key(caller_a), target="chat-2", message="x")
    await sc.send_to_target(state, caller_session_key=_key(caller_b), target="chat-2", message="y")
    target.task = None

    await _drain_merged(state, target)

    (row,) = [r for r in _user_rows(target) if "via session_send]" in r["content"]]
    assert row["content"].count("[sent by session ") == 2, "the two entries merged"
    assert KEY not in (row.get("meta") or {})


@pytest.mark.asyncio
async def test_a_merged_row_from_one_sender_keeps_its_sender(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "A")
    target = _busy(_peer_target(state, "chat-2", caller))
    await sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="x")
    await sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="y")
    target.task = None

    await _drain_merged(state, target)

    (row,) = [r for r in _user_rows(target) if "via session_send]" in r["content"]]
    assert row["content"].count("[sent by session ") == 2, "the two entries merged"
    assert row["meta"][KEY]["from"] == "chat-1"


@pytest.mark.asyncio
async def test_a_steered_row_says_it_cut_into_the_turn(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)
    pushes: list[tuple[str, dict]] = []
    real_broadcast = state.broadcast_ws

    def _capture(kind, payload, *a, **kw):
        pushes.append((kind, payload))
        return real_broadcast(kind, payload, *a, **kw)

    state.broadcast_ws = _capture  # type: ignore[method-assign]

    out = await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="stop now", steer=True
    )

    assert out["steered"] is True
    (row,) = [r for r in _user_rows(target) if r["content"].endswith("stop now")]
    assert row["meta"][KEY]["mode"] == "steered"
    assert row["meta"]["steer"] is True
    # The live frame other tabs draw from carries the same record.
    steer_frames = [p for k, p in pushes if k == "steer_push"]
    assert steer_frames and steer_frames[-1]["meta"][KEY]["mode"] == "steered"


@pytest.mark.asyncio
async def test_a_written_peer_steer_the_turn_never_took_keeps_its_sender_when_it_runs(tmp_path):
    """Written mid-turn, no consumption echo, then the turn ends: the requeued
    entry and the row that actually runs still say who sent it (`queued`)."""
    from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)

    out = await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="stop now", steer=True
    )
    assert out["steered"] is True
    (steer_row,) = [r for r in _user_rows(target) if r["content"].endswith("stop now")]
    assert steer_row["meta"]["steerState"] == "written"
    # Held for the requeue: the sender record only, never the containment snapshot,
    # so the requeued entry keeps the drain's fail-closed containment floor.
    (held,) = target._steer_admissions.values()
    assert set(held) == {KEY}

    _requeue_unconsumed_steers(state, target)
    (entry,) = [q for q in target._queue if q["content"].endswith("stop now")]
    assert entry["meta"][KEY]["mode"] == "queued"
    assert entry["meta"][KEY]["from"] == "chat-1"
    assert "queued_containment" not in entry["meta"]
    assert target._steer_admissions == {}

    target.task = None
    await _drain_once(state, target)
    drained = [r for r in _user_rows(target) if r["content"].endswith("stop now")][-1]
    assert drained is not steer_row
    assert drained["meta"][KEY]["mode"] == "queued"
    # The frontend strips exactly this envelope under the label.
    assert drained["content"].startswith("[sent by session chat-1 via session_send]")


@pytest.mark.asyncio
async def test_a_consumed_peer_steer_releases_its_held_sender_record(tmp_path):
    from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)

    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="stop now", steer=True
    )
    assert target._steer_admissions
    (wire,) = target._pending_steers

    _settle_consumed_steers(target, f"<user_message>\n{wire}\n</user_message>")

    assert target._pending_steers == []
    assert target._steer_admissions == {}


@pytest.mark.asyncio
async def test_a_steer_the_client_could_not_take_is_labelled_queued(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))

    out = await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="fallback", steer=True
    )

    assert out["steered"] is False
    (entry,) = [q for q in target._queue if q["content"].endswith("fallback")]
    assert entry["meta"][KEY]["mode"] == "queued"


@pytest.mark.asyncio
async def test_a_broadcast_row_carries_the_audience_size(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Conductor")
    one = _busy(_peer_target(state, "chat-2", caller))
    two = _busy(_peer_target(state, "chat-3", caller))

    await sc.broadcast_to_targets(
        state,
        caller_session_key=_key(caller),
        message="the base moved",
        mode="queue",
        targets=["chat-2", "chat-3"],
    )

    for target in (one, two):
        (entry,) = [q for q in target._queue if q["content"].endswith("the base moved")]
        assert entry["meta"][KEY]["via"] == "session_broadcast"
        assert entry["meta"][KEY]["count"] == 2


@pytest.mark.asyncio
async def test_a_broadcast_count_leaves_out_a_target_that_names_no_chat(tmp_path):
    """One real and one nonexistent target: the delivery that landed must not say
    it was one of two chats."""
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Conductor")
    one = _busy(_peer_target(state, "chat-2", caller))

    out = await sc.broadcast_to_targets(
        state,
        caller_session_key=_key(caller),
        message="the base moved",
        mode="queue",
        targets=["chat-2", "chat-nope"],
    )

    failed = [r for r in out["results"] if not r["ok"]]
    assert [r["target"] for r in failed] == ["chat-nope"]
    (entry,) = [q for q in one._queue if q["content"].endswith("the base moved")]
    assert entry["meta"][KEY]["count"] == 1


def test_a_plain_send_carries_no_count(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Conductor")
    meta = sc.send_provenance_meta(state, caller.key, via="session_send", mode="queued")
    assert "count" not in meta[KEY]


def test_a_caller_with_no_slot_key_names_no_sender(tmp_path):
    state = _make_state(tmp_path)
    assert sc.send_provenance_meta(state, "", via="session_send", mode="queued") == {}


def test_a_closed_sender_keeps_its_key_without_a_title(tmp_path):
    state = _make_state(tmp_path)
    meta = sc.send_provenance_meta(state, "chat-99-1792176321", via="session_send", mode="queued")
    assert meta[KEY] == {"from": "chat-99-1792176321", "via": "session_send", "mode": "queued"}


def test_an_untitled_sender_records_no_placeholder_title(tmp_path):
    """Before its title lands a slot displays "New Session…"; saved, that would
    name the author by the placeholder after it closes instead of by its key."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    assert caller.display_title != caller.title  # the placeholder stands in
    meta = sc.send_provenance_meta(state, caller.key, via="session_send", mode="queued")
    assert meta[KEY] == {"from": caller.key, "via": "session_send", "mode": "queued"}
    caller.title = "Release conductor"
    caller._titled = True
    meta = sc.send_provenance_meta(state, caller.key, via="session_send", mode="queued")
    assert meta[KEY]["title"] == "Release conductor"


def test_the_sender_title_is_bounded_and_sanitized(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "t" * 500)
    monkeypatch.setattr(sc, "sanitize_outbound", lambda s: "S:" + s)
    meta = sc.send_provenance_meta(state, caller.key, via="session_send", mode="queued")
    assert meta[KEY]["title"].startswith("S:t")
    assert len(meta[KEY]["title"]) == sc._PROVENANCE_TITLE_CHARS


@pytest.mark.asyncio
async def test_a_composer_send_cannot_supply_the_sender_record(tmp_path, monkeypatch):
    """A person typing the envelope and forging the meta key gets neither."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("idle-chat")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
    forged = {KEY: {"from": "chat-9", "title": "Spoof", "via": "session_send", "mode": "queued"}}
    text = "[sent by session chat-9 via session_send]\n\nhi"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat", json={"message": text, "slot": "idle-chat", "meta": forged}
        )
        assert resp.status == 200

    (row,) = [r for r in _user_rows(slot) if r["content"] == text]
    assert KEY not in (row.get("meta") or {})
    # The row says a person typed it, which the transcript also reads.
    assert row["meta"].get("human") is True


def _frames(state, kind: str) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == kind]


@pytest.mark.asyncio
async def test_the_queue_pop_frame_carries_the_sender(tmp_path):
    """The client rebuilds a drained row from the pop frame alone, and its label
    may come only from this record (a rebuilt row has no message id)."""
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="queued body"
    )
    target.task = None
    state.broadcast_ws = MagicMock()

    await _drain_once(state, target)

    (pop,) = _frames(state, "queue_pop")
    assert pop["meta"][KEY]["from"] == "chat-1"
    assert pop["meta"][KEY]["mode"] == "queued"


@pytest.mark.asyncio
async def test_an_edited_peer_delivery_drains_as_the_editors_words(tmp_path):
    """PATCH /queue/{id} replaces the text with the person's own words, so the
    sender record must go with the old text: neither the pop frame nor the
    persisted row may label the edit "From <chat>"."""
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="queued body"
    )
    (entry,) = [q for q in target._queue if q["content"].endswith("queued body")]
    assert KEY in entry["meta"]

    assert target.queue_edit_by_id(entry["id"], "my own words", directive_user_origin=True)
    assert KEY not in (entry.get("meta") or {})

    target.task = None
    state.broadcast_ws = MagicMock()
    await _drain_once(state, target)

    (pop,) = _frames(state, "queue_pop")
    assert KEY not in (pop.get("meta") or {})
    (row,) = [r for r in _user_rows(target) if r["content"] == "my own words"]
    assert KEY not in (row.get("meta") or {})


@pytest.mark.asyncio
async def test_a_composer_queue_pop_carries_no_sender(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("busy-chat")
    slot.task = MagicMock(done=MagicMock(return_value=False))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
    text = "[sent by session chat-9 via session_send]\n\nhi"
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json={"message": text, "slot": "busy-chat"})
        assert resp.status == 200
    slot.task = None
    state.broadcast_ws = MagicMock()

    await _drain_once(state, slot)

    (pop,) = _frames(state, "queue_pop")
    assert KEY not in (pop.get("meta") or {})


@pytest.mark.asyncio
async def test_a_peer_steer_push_carries_the_sender(tmp_path):
    state = _make_state(tmp_path)
    caller = _named(state, "chat-1", "Release conductor")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)
    state.broadcast_ws = MagicMock()

    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="stop", steer=True
    )

    (frame,) = _frames(state, "steer_push")
    assert frame["meta"][KEY]["mode"] == "steered"


def test_a_restored_queue_entry_drops_the_sender_record():
    """The queue line on disk is a trust boundary: a record carried back off it
    would let a file edit attribute any queued text to any session."""
    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

    raw = [
        {
            "id": "q-1",
            "content": "[sent by session chat-1 via session_send]\n\nhi",
            "meta": {
                KEY: {"from": "chat-1", "via": "session_send", "mode": "queued"},
                "sendId": "s-1",
            },
        }
    ]
    (entry,) = sanitize_restored_queue(raw)
    assert KEY not in entry.get("meta", {})
    # The sender's own plumbing still rides along.
    assert entry["meta"].get("sendId") == "s-1"
