"""A Bot thread reply keeps the credential records its placeholders need.

The main chat stores a reply with ``redactions`` beside its redacted text and
draws a lock tag from each. A thread reply is stored as ``redact(raw)``, the text
it always had, with the records the main chat's own credential pass wrote for that
text; the sidecar keeps them, the thread read and the final frame's ``reply`` carry
them, and the removed value is kept for the owner's reveal once the sidecar holds
the row. The raw value reaches none of the sidecar, the read or a frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp_backends import ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.dashboard import chat_threads
from kiro_crew.dashboard.chat_runner import _redact_segment_text
from kiro_crew.dashboard.chat_threads import _run_thread_turn, api_chat_thread_detail
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.side_readonly_spec import PublishedSpec
from kiro_crew.members import DM_SLOT_MODE
from kiro_crew.security import redact, reveal_store
from kiro_crew.testing.synthetic_credentials import github_classic

_GH = github_classic(seed=41149)
_RAW = f"Use the token {_GH} for the upload."
_CRED_TAG = "[REDACTED: credential]"
_SLOT = "member-radar"


@pytest.fixture(autouse=True)
def _fresh_reveal_store(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _floor_monkeypatch.setattr(reveal_store, "STORE", reveal_store.RevealStore())
    yield


@pytest.fixture(autouse=True)
def _threads_on() -> Iterator[None]:
    chat_threads._in_flight.clear()
    cfg = KiroCrewConfig()
    cfg.dashboard.crewmate_threads = True
    live.reset_for_tests()
    live.watch().prime(cfg)
    yield
    chat_threads._in_flight.clear()


def _member_slot(state, parent_text: str = "Overnight triage: 9 new issues."):
    """A Bot's chat whose assistant row is flushed to disk; returns ``(slot, mid)``."""
    slot = state.get_or_create_slot(_SLOT, agent="Radar", mode=DM_SLOT_MODE)
    row = slot.append("assistant", parent_text, broadcast=False)
    state.conversation_log.append(
        slot_history_key(slot), "assistant", row["content"], mid=row["meta"]["mid"]
    )
    return slot, row["meta"]["mid"]


def _capture(state) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    state.broadcast_ws = lambda t, d: events.append((t, d))
    state.broadcast_ws_owners = lambda t, d: events.append((t, d))
    return events


def _answering(state, monkeypatch, raw: str) -> None:
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    state.sessions.release = MagicMock()
    state.sessions.destroy = AsyncMock()

    async def _fake_stream(provider, message, *, on_chunk=None, **kwargs):
        on_chunk(raw)
        return raw

    monkeypatch.setattr(chat_threads, "stream_and_collect", _fake_stream)
    monkeypatch.setattr(
        chat_threads,
        "publish_readonly_spec",
        lambda base, project=None: PublishedSpec(name=f"{base}--readonly", digest="d" * 64),
    )
    monkeypatch.setattr(
        chat_threads.KiroCrewConfig,
        "load",
        classmethod(lambda cls: MagicMock(agent=MagicMock(acp_backend=ACP_BACKEND_KIRO))),
    )
    monkeypatch.setattr(chat_threads, "warm_project_agent_names", AsyncMock())
    monkeypatch.setattr(
        chat_threads,
        "resolve_agent_bindings",
        lambda *a, **k: MagicMock(kiro_agent="kirocrew", requested_resolved=True),
    )


async def _turn(state, slot, mid) -> None:
    identity = state.conversation_log.thread_transcript_identity(slot_history_key(slot))
    parent = {"role": "assistant", "content": "Overnight triage: 9 new issues."}
    await _run_thread_turn(
        state, slot, mid, "run-1", "which token?", parent, [], f"{slot.key}:{mid}", identity
    )


async def _detail(state, mid) -> dict[str, Any]:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/threads/{mid}", api_chat_thread_detail)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/chat/threads/{mid}?slot={_SLOT}")
        assert resp.status == 200
        return await resp.json()


def _the_record(holder: dict[str, Any]) -> dict[str, Any]:
    assert "redactions" in holder, sorted(holder)
    [record] = holder["redactions"]
    assert record["rule"] == "github_token" and record["ordinal"] == 0
    return record


@pytest.mark.asyncio
async def test_a_thread_reply_is_mains_text_with_its_record_and_the_raw_value_reaches_nothing(
    tmp_path, monkeypatch, caplog
):
    state = _make_state(tmp_path)
    events = _capture(state)
    slot, mid = _member_slot(state)
    _answering(state, monkeypatch, _RAW)

    with caplog.at_level(logging.DEBUG):
        await _turn(state, slot, mid)
    assert _GH not in caplog.text

    sidecar = state.conversation_log.threads_sidecar_path(slot_history_key(slot))
    assert _GH not in sidecar.read_text(encoding="utf-8")
    [stored] = state.conversation_log.read_threads(slot_history_key(slot))[mid]
    assert stored["content"] == redact(_RAW) and stored["content"].count(_CRED_TAG) == 1
    record = _the_record(stored)

    detail = await _detail(state, mid)
    [shown] = detail["replies"]
    assert shown["content"] == redact(_RAW)
    assert _the_record(shown) == record
    assert "redactions" not in detail["parent"]

    [final] = [d for t, d in events if t == "chat.thread_reply" and d.get("final")]
    assert _the_record(final["reply"]) == record
    # The rendered thread is built from these: none of them holds the raw value.
    assert _GH not in json.dumps(detail) and _GH not in json.dumps([d for _, d in events])


@pytest.mark.asyncio
async def test_a_stored_replys_value_is_revealable_in_its_slot_only(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    _capture(state)
    slot, mid = _member_slot(state)
    _answering(state, monkeypatch, _RAW)

    await _turn(state, slot, mid)

    [stored] = state.conversation_log.read_threads(slot_history_key(slot))[mid]
    handle = _the_record(stored)["reveal_id"]
    assert reveal_store.STORE.reveal(handle, scope=slot.key) == _GH
    assert reveal_store.STORE.reveal(handle, scope="other") is None


_UNFLUSHED_MID = "m-" + "f" * 16


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["full", "unflushed"])
async def test_a_reply_the_store_refuses_keeps_no_value(tmp_path, monkeypatch, refusal):
    """A write the store refuses keeps no value (the thread full, or a parent the
    transcript does not hold), and the panel is told why."""
    state = _make_state(tmp_path)
    events = _capture(state)
    slot, mid = _member_slot(state)
    _answering(state, monkeypatch, _RAW)
    if refusal == "full":
        monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_THREAD", 0)
    else:
        mid = _UNFLUSHED_MID

    await _turn(state, slot, mid)

    assert len(reveal_store.STORE) == 0
    [final] = [d for t, d in events if t == "chat.thread_reply" and d.get("final")]
    assert final.get("is_error") and "reply" not in final
    fallback = {
        "full": chat_threads._FULL_FALLBACK,
        "unflushed": chat_threads._PARENT_GONE_FALLBACK,
    }[refusal]
    assert final["content"] == fallback


@pytest.mark.asyncio
async def test_a_value_the_reveal_store_refuses_leaves_the_frame_matching_the_sidecar(
    tmp_path, monkeypatch
):
    """The write lands before the commit, so a refused stash changes neither copy:
    the frame names the handle the sidecar holds, and that handle opens nothing."""
    state = _make_state(tmp_path)
    events = _capture(state)
    slot, mid = _member_slot(state)
    _answering(state, monkeypatch, _RAW)
    monkeypatch.setattr(reveal_store.STORE, "stash_as", lambda handle, value, *, scope: False)

    await _turn(state, slot, mid)

    [stored] = state.conversation_log.read_threads(slot_history_key(slot))[mid]
    [final] = [d for t, d in events if t == "chat.thread_reply" and d.get("final")]
    assert final["reply"]["redactions"] == stored["redactions"]
    handle = _the_record(stored)["reveal_id"]
    assert isinstance(handle, str) and reveal_store.REVEAL_ID_RE.match(handle)
    assert reveal_store.STORE.reveal(handle, scope=slot.key) is None


@pytest.mark.asyncio
async def test_the_read_keeps_only_records_the_bound_accepts(tmp_path):
    """A record planted in the sidecar is dropped at the read, and a user row keeps none."""
    state = _make_state(tmp_path)
    slot, mid = _member_slot(state)
    key = slot_history_key(slot)
    text, [record] = _redact_segment_text(slot, _RAW)
    # Well formed, every key in place, but naming a command the rule never offers,
    # or a handle not in the store's shape.
    planted_command = {**record, "view_command": "cat ~/.aws/credentials"}
    bad_handle = {**record, "reveal_id": "not-a-handle"}
    rows = [
        {**chat_threads._new_reply("assistant", text), "redactions": [record]},
        {**chat_threads._new_reply("assistant", text), "redactions": [{"rule": "x"}]},
        {**chat_threads._new_reply("assistant", text), "redactions": [planted_command]},
        {**chat_threads._new_reply("assistant", text), "redactions": [bad_handle]},
        {**chat_threads._new_reply("user", "hi"), "redactions": [record]},
    ]
    sidecar = state.conversation_log.threads_sidecar_path(key)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({"version": 1, "threads": {mid: rows}}), encoding="utf-8")

    kept, *forged, user = (await _detail(state, mid))["replies"]
    assert kept["redactions"] == [record]
    assert len(forged) == 3 and all("redactions" not in r for r in forged)
    assert "redactions" not in user


@pytest.mark.asyncio
async def test_the_parent_carries_the_records_of_its_main_chat_row(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(_SLOT, agent="Radar", mode=DM_SLOT_MODE)
    text, records = _redact_segment_text(slot, _RAW)
    row = slot.append("assistant", text, broadcast=False)
    row["meta"]["redactions"] = records
    state.conversation_log.append(slot_history_key(slot), "assistant", text, mid=row["meta"]["mid"])

    parent = (await _detail(state, row["meta"]["mid"]))["parent"]
    assert parent["content"] == redact(_RAW)
    assert _the_record(parent) == records[0]
    assert _GH not in json.dumps(parent)


@pytest.mark.asyncio
async def test_a_clip_that_cuts_the_placeholder_off_drops_its_records_and_keeps_no_value(
    tmp_path, monkeypatch
):
    """The main chat's rule: records whose text holds no placeholder are not kept."""
    state = _make_state(tmp_path)
    events = _capture(state)
    slot, mid = _member_slot(state)
    _answering(state, monkeypatch, _RAW)
    monkeypatch.setattr(chat_threads, "_MAX_STORED_REPLY_CHARS", len("Use the token "))

    await _turn(state, slot, mid)

    [stored] = state.conversation_log.read_threads(slot_history_key(slot))[mid]
    assert _CRED_TAG not in stored["content"] and "redactions" not in stored
    [final] = [d for t, d in events if t == "chat.thread_reply" and d.get("final")]
    assert "redactions" not in final["reply"]
    assert len(reveal_store.STORE) == 0


def _cancelled_mid_stream(state, monkeypatch, raw: str) -> None:
    """A turn whose stream sends *raw* and is then cancelled (a gateway restart)."""
    _answering(state, monkeypatch, raw)

    async def _fake_stream(provider, message, *, on_chunk=None, **kwargs):
        on_chunk(raw)
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_threads, "stream_and_collect", _fake_stream)


async def _cancelled_turn(state, slot, mid) -> None:
    identity = state.conversation_log.thread_transcript_identity(slot_history_key(slot))
    parent = {"role": "assistant", "content": "Overnight triage: 9 new issues."}
    with pytest.raises(asyncio.CancelledError):
        await chat_threads._thread_turn_body(
            state, slot, mid, "run-1", "which token?", parent, [], f"{slot.key}:{mid}", identity
        )


@pytest.mark.asyncio
async def test_the_interrupted_partial_answer_keeps_its_record_and_value(
    tmp_path, monkeypatch, caplog
):
    state = _make_state(tmp_path)
    _capture(state)
    slot, mid = _member_slot(state)
    _cancelled_mid_stream(state, monkeypatch, _RAW)

    with caplog.at_level(logging.DEBUG):
        await _cancelled_turn(state, slot, mid)
    assert _GH not in caplog.text

    sidecar = state.conversation_log.threads_sidecar_path(slot_history_key(slot))
    assert _GH not in sidecar.read_text(encoding="utf-8")
    [stored] = state.conversation_log.read_threads(slot_history_key(slot))[mid]
    expected = f"{redact(_RAW).strip()}\n\n{chat_threads._INTERRUPTED_MARKER}"
    assert stored["content"] == expected
    handle = _the_record(stored)["reveal_id"]
    assert reveal_store.STORE.reveal(handle, scope=slot.key) == _GH


@pytest.mark.asyncio
async def test_an_interrupted_answer_the_store_refuses_keeps_no_value(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    _capture(state)
    slot, mid = _member_slot(state)
    _cancelled_mid_stream(state, monkeypatch, _RAW)
    monkeypatch.setattr(chat_threads, "_MAX_REPLIES_PER_THREAD", 0)

    await _cancelled_turn(state, slot, mid)

    assert state.conversation_log.read_threads(slot_history_key(slot)).get(mid) is None
    assert len(reveal_store.STORE) == 0
