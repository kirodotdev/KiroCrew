"""A Side Chat answer keeps the credential records its placeholders need.

The main chat stores a reply with ``redactions`` beside its redacted text and
draws a lock tag from each. A side answer is stored as ``redact(raw)``, the text
it always had, with the records the main chat's own credential pass wrote for that
text; its final ``chat.side_result`` frame and ``GET .../side`` carry them beside
``link_blocks``, and the removed value is kept for the owner's reveal once the row
is stored.
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from side_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _capture_broadcasts,
    _make_side_app,
    _published_readonly_spec_fixture,
)

from kiro_crew.dashboard.handlers.side import _run_side_turn
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.llm_helpers import PromptBusyExhaustedError
from kiro_crew.security import redact, reveal_store
from kiro_crew.testing.synthetic_credentials import github_classic

_GH = github_classic()
_HOST = "collect.example.com"
_LINK = f"https://{_HOST}/x?d=" + "A" * 250
_RAW = f"See {_LINK} and use the token {_GH} for the upload."
_CRED_TAG = "[REDACTED: credential]"


@pytest.fixture(autouse=True)
def _fresh_reveal_store(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _floor_monkeypatch.setattr(reveal_store, "STORE", reveal_store.RevealStore())
    yield


def _parent(state, run_id: str = "run-1"):
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("where?", run_id=run_id)
    parent._side.last_run_id = run_id
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    return parent


def _answering(monkeypatch, raw: str, *, before_return=None) -> None:
    async def _fake_stream(provider, message, *, on_chunk=None, **kwargs):
        on_chunk(raw)
        if before_return is not None:
            before_return()
        return raw

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)


def _final_frames(events: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [d for t, d in events if t == "chat.side_result" and d.get("final")]


@web.middleware
async def _as_owner(request: web.Request, handler):
    """Stand in for the token middleware: the local owner is asking."""
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


async def _read(state) -> dict[str, Any]:
    app = _make_side_app(state)
    app.middlewares.append(_as_owner)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/chat/slots/parent/side")
        assert resp.status == 200
        return await resp.json()


def _the_credential_record(holder: dict[str, Any]) -> dict[str, Any]:
    assert "redactions" in holder, sorted(holder)
    [record] = holder["redactions"]
    assert record["rule"] == "github_token"
    return record


@pytest.mark.asyncio
async def test_the_stored_answer_is_mains_text_with_its_credential_record(tmp_path, monkeypatch):
    """The stored row's text is byte-identical to ``redact(raw)`` and carries the
    credential record of its one placeholder."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = _parent(state)
    _answering(monkeypatch, _RAW)

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)

    answer = parent._side.messages[-1]
    assert answer["content"] == redact(_RAW)
    assert answer["content"].count(_CRED_TAG) == 1 and _GH not in answer["content"]
    assert _the_credential_record(answer)["ordinal"] == 0


@pytest.mark.asyncio
async def test_the_final_frame_and_the_read_carry_the_records_and_deltas_none(
    tmp_path, monkeypatch
):
    """The terminal frame names the stored row and carries its ``redactions`` beside
    its ``link_blocks``, as ``GET .../side`` serves that row; a streamed delta and
    the question carry none."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _parent(state)
    _answering(monkeypatch, _RAW)

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)
    question, answer = (await _read(state))["messages"]

    [final] = _final_frames(events)
    assert final["message_id"] == parent._side.messages[-1]["id"] == answer["id"]
    stored = _the_credential_record(parent._side.messages[-1])
    for holder in (final, answer):
        assert holder["content"] == redact(_RAW)
        assert holder.get("redactions") == [stored]
        [link] = holder["link_blocks"]
        assert link["domain"] == _HOST
    deltas = [d for t, d in events if t == "chat.side_result" and not d.get("final")]
    assert deltas and all("redactions" not in d for d in deltas)
    assert "redactions" not in question


@pytest.mark.asyncio
async def test_a_stored_answers_removed_value_is_revealable_for_the_slot(tmp_path, monkeypatch):
    """The record's ``reveal_id`` opens the removed token in this slot only."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = _parent(state)
    _answering(monkeypatch, _RAW)

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)

    handle = _the_credential_record(parent._side.messages[-1])["reveal_id"]
    assert reveal_store.STORE.reveal(handle, scope="parent") == _GH
    assert reveal_store.STORE.reveal(handle, scope="other") is None


@pytest.mark.asyncio
async def test_a_sidecar_closed_mid_turn_keeps_nothing_and_sends_no_final_frame(
    tmp_path, monkeypatch
):
    """A sidecar closed before the answer settles stores no row, so no removed value
    is kept and no final frame goes out."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _parent(state)

    def _close() -> None:
        parent._side = None

    _answering(monkeypatch, _RAW, before_return=_close)

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)

    assert len(reveal_store.STORE) == 0
    assert _final_frames(events) == []


@pytest.mark.asyncio
async def test_a_turn_admitted_after_a_close_and_reopen_frames_no_records(tmp_path, monkeypatch):
    """A turn still waiting for admission when its sidecar is closed and reopened
    runs against the new sidecar, which does not store it: its final frame carries
    no records, and no removed value is kept."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _parent(state)
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:01Z")
    _answering(monkeypatch, _RAW)

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)

    [final] = _final_frames(events)
    assert final["content"] == redact(_RAW)
    assert "message_id" not in final and "redactions" not in final
    assert parent._side.messages == [] and len(reveal_store.STORE) == 0


@pytest.mark.asyncio
async def test_the_prompt_busy_partial_gets_its_record_frame_and_kept_value(tmp_path, monkeypatch):
    """The partial answer of an interrupted turn is stored and broadcast with its
    record, and its removed value is kept, as a settled answer's is; the notice
    after it carries none."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _parent(state)

    async def _interrupted(provider, message, *, on_chunk=None, **kwargs):
        on_chunk(_RAW)
        raise PromptBusyExhaustedError("busy")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _interrupted)

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)

    partial, notice = parent._side.messages[-2:]
    assert partial["content"] == redact(_RAW)
    record = _the_credential_record(partial)
    assert "redactions" not in notice
    partial_frame, notice_frame = _final_frames(events)
    assert partial_frame["message_id"] == partial["id"]
    assert partial_frame.get("redactions") == [record]
    assert "redactions" not in notice_frame
    assert reveal_store.STORE.reveal(record["reveal_id"], scope="parent") == _GH


@pytest.mark.asyncio
async def test_an_answer_with_nothing_removed_carries_no_records(tmp_path, monkeypatch):
    """A plain answer is stored, framed and read exactly as before: no ``redactions``."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _parent(state)
    _answering(monkeypatch, "Nothing was removed from this answer.")

    await _run_side_turn(state, parent, "run-1", "where?", is_first_turn=True)
    answer = (await _read(state))["messages"][-1]

    [final] = _final_frames(events)
    assert answer["content"] == final["content"] == "Nothing was removed from this answer."
    assert all("redactions" not in holder for holder in (parent._side.messages[-1], answer, final))
