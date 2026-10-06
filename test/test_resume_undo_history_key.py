"""Undo after a session-tree close must reopen the ORIGINAL transcript.

The sidebar's tree close shows "Closed N sessions · Undo". Undo calls the same
resume endpoint the Older sessions list calls, and it must send the same key that
list sends for the row: the transcript's filename stem, ``dashboard_<slot>``.

Sending the bare slot key instead names no transcript. The endpoint then reads
``<slot>.jsonl``, finds nothing, and publishes an EMPTY session with default
settings; that session's next save overwrites the real transcript's saved model
and folder. These tests drive the real endpoint with both spellings so the
difference is pinned on the server side, not only in the client's mock.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

SLOT = "chat-undo-1"
TRANSCRIPT = f"dashboard:{SLOT}"
FOLDER = "fldrUNDO0001"
MODEL = "claude-opus-4"


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append(TRANSCRIPT, "user", "the original question")
    log.append(TRANSCRIPT, "assistant", "the original answer")
    state._folders.append({"id": FOLDER, "name": "Kept folder", "order": 0})
    log.update_metadata(TRANSCRIPT, {"folder_id": FOLDER, "model": MODEL})
    return state


@pytest.mark.asyncio
async def test_undo_history_key_reopens_the_original_transcript(tmp_path, monkeypatch):
    """The key Undo now sends -- ``dashboard_<slot>``, as Older sessions does."""
    state = _seed(tmp_path, monkeypatch)
    history_key = f"dashboard_{SLOT}"

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{history_key}/resume",
            json={"name": history_key, "key": history_key, "title": "t"},
        )
        body = await resp.json()

    assert resp.status == 200, body
    assert body["key"] == SLOT, f"resume opened slot {body['key']!r}, not the closed one"
    slot = state._slots[SLOT]
    contents = [m.get("content") for m in slot.messages]
    assert (
        "the original question" in contents and "the original answer" in contents
    ), f"Undo reopened a session without its transcript: {contents!r}"
    assert slot.folder_id == FOLDER, f"saved folder lost on Undo: {slot.folder_id!r}"
    assert slot.model == MODEL, f"saved model lost on Undo: {slot.model!r}"


@pytest.mark.asyncio
async def test_bare_slot_key_reads_no_transcript(tmp_path, monkeypatch):
    """CONTROL: a bare slot key reads no transcript, so Undo must not send it."""
    state = _seed(tmp_path, monkeypatch)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/resume",
            json={"name": SLOT, "key": SLOT, "title": "t"},
        )

    slot = state._slots.get(SLOT)
    contents = [m.get("content") for m in slot.messages] if slot else []
    assert "the original answer" not in contents, (
        "the bare slot key finds the transcript, so this control does not "
        f"prove the bug it pins (status {resp.status})"
    )


CHANNEL_KEY = "slack:1783733803.877979"
STEM = "slack_1783733803.877979"


def _rows(payload):
    return payload if isinstance(payload, list) else payload.get("slots", payload)


@pytest.mark.asyncio
async def test_slot_payload_carries_the_history_key_for_a_dashboard_slot(tmp_path, monkeypatch):
    """An ordinary dashboard slot serializes the stem Older sessions lists it under."""
    state = _seed(tmp_path, monkeypatch)
    state.get_or_create_slot(SLOT)

    async with TestClient(TestServer(_make_app(state))) as client:
        rows = _rows(await (await client.get("/api/chat/slots")).json())

    row = next(r for r in rows if r["key"] == SLOT)
    assert row["history_key"] == f"dashboard_{SLOT}"


@pytest.mark.asyncio
async def test_unbound_channel_slot_serializes_its_channel_stem_and_resumes_it(
    tmp_path, monkeypatch
):
    """A channel-born slot the session map could not bind keeps its channel transcript.

    Its ``linked_session_key`` is empty, so a key built on the client from the slot
    name would be ``dashboard_slack_...``: a transcript that does not exist. The
    server knows the slot's channel provenance and serializes the channel stem, and
    resuming with that stem reads the original thread.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.sessions.channel_key_for_stem = lambda stem: ""
    log = state.conversation_log
    log.append(CHANNEL_KEY, "user", "hello from the thread")
    log.append(CHANNEL_KEY, "assistant", "hi back")
    log.update_metadata(CHANNEL_KEY, {"tab_id": "aaaabbbbcccc", "model": MODEL})
    slot = state.get_or_create_slot(STEM, channel_origin=True)
    assert slot.linked_session_key == "", "fixture expected an UNBOUND channel slot"

    async with TestClient(TestServer(_make_app(state))) as client:
        rows = _rows(await (await client.get("/api/chat/slots")).json())
        row = next(r for r in rows if r["key"] == STEM)
        assert (
            row["history_key"] == STEM
        ), f"an unbound channel slot serialized {row['history_key']!r}, not its channel stem"
        # The tree close: the slot leaves the live list, then Undo resumes it.
        state._slots.pop(STEM, None)
        resp = await client.post(
            f"/api/chat/slots/{row['history_key']}/resume",
            json={"name": row["history_key"], "key": row["history_key"], "title": "t"},
        )
        body = await resp.json()

    assert resp.status == 200, body
    resumed = state._slots[body["key"]]
    contents = [m.get("content") for m in resumed.messages]
    assert (
        "hello from the thread" in contents and "hi back" in contents
    ), f"Undo with the served key reopened a session without its thread: {contents!r}"
