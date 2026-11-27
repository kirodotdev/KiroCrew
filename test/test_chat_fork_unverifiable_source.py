"""A fork refused because its source could not be checked says so, not "deleted".

The fork refuses to copy a source whose delete witness answers ``DELETED`` or
``UNVERIFIABLE``. Both keep the 409 and the ``fork_source_deleted`` code clients
match on. Only a proven delete may tell the user, or the log, that the session was
permanently deleted: an unverifiable read is a transient failure on a live
session, and a retry succeeds once the read works.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork
from kiro_crew.dashboard.chat_persistence import DeleteWitness, _save_slot_to_history

_LOGGER = "kiro_crew.dashboard.chat_fork"
_DELETED_TEXT = "permanently deleted"


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())


def _seeded(tmp_path):
    """A settled, saved two-turn source whose slot has observed its transcript."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("parent")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot.drain()
    assert _save_slot_to_history(state, slot, force=True) is True
    slot._dirty = False
    return state, slot


async def _fork(state) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json={})
        return resp.status, await resp.json()


def _fork_logs(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.name == _LOGGER)


@pytest.mark.asyncio
async def test_an_unreadable_source_identity_refuses_with_a_retryable_cause(
    tmp_path, monkeypatch, caplog
):
    state, _slot = _seeded(tmp_path)
    log = state.conversation_log
    real_status = log.get_metadata_status

    def _status(key):
        if key == "dashboard:parent":
            raise OSError("metadata read failed")
        return real_status(key)

    monkeypatch.setattr(log, "get_metadata_status", _status)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        status, body = await _fork(state)

    assert status == 409
    assert body["code"] == "fork_source_deleted"
    assert "retry" in body["error"]
    assert _DELETED_TEXT not in body["error"]
    logged = _fork_logs(caplog)
    assert "could not be checked" in logged
    assert _DELETED_TEXT not in logged
    assert list(state._slots) == ["parent"], "a refused fork must not mint a child"


@pytest.mark.asyncio
async def test_a_deleted_source_keeps_the_delete_wording(tmp_path):
    state, _slot = _seeded(tmp_path)
    assert state.conversation_log.delete_session("dashboard:parent") is True

    status, body = await _fork(state)

    assert status == 409
    assert body == {
        "error": "the source session was permanently deleted",
        "code": "fork_source_deleted",
    }


@pytest.mark.parametrize(
    ("answer", "wording"),
    [
        (DeleteWitness.UNVERIFIABLE, "could not be checked"),
        (DeleteWitness.DELETED, _DELETED_TEXT),
    ],
)
@pytest.mark.asyncio
async def test_the_acknowledgment_refusal_rolls_back_and_names_the_real_cause(
    tmp_path, monkeypatch, caplog, answer, wording
):
    """The re-probe after the destination save refuses and removes the child for
    both answers; only the wording follows the answer."""
    state, _slot = _seeded(tmp_path)
    before = {p.resolve() for p in tmp_path.rglob("*.jsonl")}
    answers = iter([DeleteWitness.PRESENT])
    monkeypatch.setattr(
        chat_fork, "session_delete_witness", lambda *_a, **_k: next(answers, answer)
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        status, body = await _fork(state)

    assert status == 409
    assert body["code"] == "fork_source_deleted"
    assert wording in body["error"]
    leftover = sorted(p.name for p in tmp_path.rglob("*.jsonl") if p.resolve() not in before)
    assert leftover == [], f"the unacknowledged fork was not rolled back: {leftover}"
    assert list(state._slots) == ["parent"]
    logged = _fork_logs(caplog)
    assert wording in logged
    if answer is DeleteWitness.UNVERIFIABLE:
        assert _DELETED_TEXT not in logged
