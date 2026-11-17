"""The side chat and a reply thread carry blocked-link records, as main chat rows do.

A finished side answer or crewmate thread reply is drawn by its own panel, outside
``_prepare_messages``, so its frames and its restore reads carry ``link_blocks``
computed from the text they send: a flagged link there renders as a Blocked link
card, and the text keeps the URL as written. A streamed delta carries none; the
panel draws a streaming reply's links inert.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.dashboard import chat_threads
from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard.handlers import side as side_handlers
from kiro_crew.dashboard.ws import (
    LINK_BLOCKS_KEY,
    SIDE_RESULT_EVENT,
    THREAD_REPLY_EVENT,
    broadcast_side_result,
    broadcast_thread_reply,
)

_HOST = "collect.example.com"
_URL = f"https://{_HOST}/report?q=" + "x" * 260


class _State:
    """The owner channel, recorded, and the one slot the frames name."""

    def __init__(self, workspace: str = "ws1") -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._slot = SimpleNamespace(key="chat-1", workspace=workspace)

    def broadcast_ws_owners(self, kind: str, payload: dict[str, Any]) -> None:
        self.sent.append((kind, payload))

    def get_slot(self, key: str) -> SimpleNamespace | None:
        return self._slot if key == self._slot.key else None


@pytest.fixture
def _threads_on():
    cfg = KiroCrewConfig()
    cfg.dashboard.crewmate_threads = True
    live.reset_for_tests()
    live.watch().prime(cfg)
    yield
    live.reset_for_tests()


def _urls(records: object) -> list[str]:
    assert isinstance(records, list)
    return [record["url"] for record in records]


def test_a_final_side_answer_carries_the_record_and_keeps_the_text() -> None:
    state = _State()
    broadcast_side_result(
        state, slot_key="chat-1", run_id="r1", role="assistant", content=f"see {_URL}", final=True
    )
    [(kind, payload)] = state.sent
    assert kind == SIDE_RESULT_EVENT
    assert payload["content"] == f"see {_URL}"
    assert _urls(payload[LINK_BLOCKS_KEY]) == [_URL]


def test_a_side_delta_a_question_and_a_clean_answer_carry_none() -> None:
    state = _State()
    broadcast_side_result(state, slot_key="chat-1", run_id="r1", role="assistant", content=_URL)
    broadcast_side_result(state, slot_key="chat-1", run_id="r1", role="user", content=_URL)
    broadcast_side_result(
        state, slot_key="chat-1", run_id="r1", role="assistant", content="all clean", final=True
    )
    assert all(LINK_BLOCKS_KEY not in payload for _kind, payload in state.sent)


def test_an_allowed_host_lets_the_side_answer_link_render_live(monkeypatch) -> None:
    monkeypatch.setattr(
        cu, "allowed_hosts_for", lambda ws: frozenset({_HOST}) if ws == "ws1" else frozenset()
    )
    allowed, other = _State("ws1"), _State("ws2")
    for state in (allowed, other):
        broadcast_side_result(
            state, slot_key="chat-1", run_id="r1", role="assistant", content=_URL, final=True
        )
    assert LINK_BLOCKS_KEY not in allowed.sent[0][1]
    assert _urls(other.sent[0][1][LINK_BLOCKS_KEY]) == [_URL]


def test_the_side_restore_read_carries_the_record_of_a_stored_answer() -> None:
    answer = side_handlers._wire_side_row(
        {"id": "a1", "role": "assistant", "content": f"see {_URL}", "ts": "t"}, "ws1"
    )
    question = side_handlers._wire_side_row(
        {"id": "q1", "role": "user", "content": f"is {_URL} ok?", "ts": "t"}, "ws1"
    )
    assert answer["content"] == f"see {_URL}"
    assert _urls(answer[LINK_BLOCKS_KEY]) == [_URL]
    assert LINK_BLOCKS_KEY not in question


@pytest.mark.usefixtures("_threads_on")
def test_a_final_thread_reply_frame_carries_the_record_on_it_and_its_reply() -> None:
    state = _State()
    stored = {"id": "r1", "role": "assistant", "content": f"see {_URL}", "ts": "t"}
    broadcast_thread_reply(
        state,
        slot_key="chat-1",
        mid="m1",
        run_id="run",
        role="assistant",
        content=f"see {_URL}",
        final=True,
        reply=stored,
    )
    [(kind, payload)] = state.sent
    assert kind == THREAD_REPLY_EVENT
    assert payload["content"] == f"see {_URL}"
    assert _urls(payload[LINK_BLOCKS_KEY]) == [_URL]
    assert _urls(payload["reply"][LINK_BLOCKS_KEY]) == [_URL]
    # The stored record is a copy's source, never changed.
    assert stored == {"id": "r1", "role": "assistant", "content": f"see {_URL}", "ts": "t"}


@pytest.mark.usefixtures("_threads_on")
def test_a_thread_delta_and_the_users_reply_carry_none() -> None:
    state = _State()
    broadcast_thread_reply(
        state, slot_key="chat-1", mid="m1", run_id="run", role="assistant", content=_URL
    )
    mine = {"id": "u1", "role": "user", "content": _URL, "ts": "t"}
    broadcast_thread_reply(
        state, slot_key="chat-1", mid="m1", run_id="run", role="user", content=_URL, reply=mine
    )
    for _kind, payload in state.sent:
        assert LINK_BLOCKS_KEY not in payload
        assert LINK_BLOCKS_KEY not in (payload.get("reply") or {})


def test_the_thread_read_carries_records_for_the_crewmates_text_only() -> None:
    crewmate = chat_threads._with_link_blocks(
        chat_threads._redacted_reply(
            {"id": "r1", "role": "assistant", "content": f"see {_URL}", "ts": "t"}
        ),
        "ws1",
    )
    user = chat_threads._with_link_blocks(
        chat_threads._redacted_reply({"id": "u1", "role": "user", "content": _URL, "ts": "t"}),
        "ws1",
    )
    parent = chat_threads._with_link_blocks(
        chat_threads._parent_payload({"role": "assistant", "content": _URL, "mid": "m1"}), "ws1"
    )
    assert crewmate["content"] == f"see {_URL}"
    assert _urls(crewmate[LINK_BLOCKS_KEY]) == [_URL]
    assert LINK_BLOCKS_KEY not in user
    assert _urls(parent[LINK_BLOCKS_KEY]) == [_URL]
