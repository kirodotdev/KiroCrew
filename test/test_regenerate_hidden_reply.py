"""Regenerate targets the reply the dashboard shows, not a hidden row after it.

The dashboard puts Regenerate on the newest assistant row it draws
(``isHiddenReplyAt``). A row it draws as nothing -- a bare zero-width reply, or a
reply that is only a ``[STEERING …]`` note in a turn with a tool-blocked inject --
must not be what the server regenerates: the swap keeps only the target as the
earlier variant, so the reply on screen would leave the transcript and the
variant history.

Nothing is removed when Regenerate is pressed: the rows after the request become
the turn's target, replaced only once the new turn produces a reply.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_turn.agent_started import AGENT_ORIGIN_META_KEY, AGENT_STARTED_ACTOR
from kiro_crew.dashboard.state_parts.injected_turns import REFUSAL_INBAND_RECOVERY_PREFIX

_PROMPT = "draft the rollout plan"
_REPLY = "Stage one ships to beta on Monday."
_BLOCK = f"{REFUSAL_INBAND_RECOVERY_PREFIX} The command was denied by policy."
_NOTE = "[STEERING steer-0a1b2c3d: Stopped retrying the blocked command.]"
_ZWSP = "\u200b"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


async def _regenerate(state, slot):
    """POST regenerate with the turn stubbed out.

    Returns the status, the body, the stubbed turn, the variants it was handed and
    the rows it was armed to replace, both read while the turn "runs": the turn's
    done callback clears them once it ends.
    """
    variants: list[dict] = []
    armed: list[tuple[str, str]] = []

    def _turn(*_a, **_kw):
        variants.extend(slot._pending_variants)
        armed.extend((m["role"], m["content"]) for m in slot._regenerate_target.rows)

    run_chat = AsyncMock(side_effect=_turn)
    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run_chat):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/regenerate")
            body = await resp.json()
            await asyncio.sleep(0)
    return resp.status, body, run_chat, variants, armed


def _transcript(slot) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in slot.messages]


def _s1(slot) -> None:
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    slot.append("inject", _BLOCK, "msg msg-inject")
    slot.append("assistant", _NOTE)


@pytest.mark.asyncio
async def test_a_steering_note_after_a_policy_block_is_not_the_target(state):
    slot = state.get_or_create_slot("s1")
    _s1(slot)
    slot.drain()
    before = _transcript(slot)

    status, body, run_chat, variants, armed = await _regenerate(state, slot)

    assert status == 200, body
    assert run_chat.call_args.args[2] == _PROMPT
    assert [v["content"] for v in variants] == [_REPLY]
    assert armed == before[1:]
    assert _transcript(slot) == before


@pytest.mark.asyncio
async def test_a_zero_width_reply_is_not_the_target(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    slot.append("assistant", _ZWSP)
    slot.drain()
    before = _transcript(slot)

    status, body, _run_chat, variants, armed = await _regenerate(state, slot)

    assert status == 200, body
    assert [v["content"] for v in variants] == [_REPLY]
    assert armed == before[1:]


@pytest.mark.asyncio
async def test_a_reply_with_no_content_is_judged_as_the_dashboard_receives_it(state):
    # The wire sends ``None`` content as "", which the dashboard hides.
    slot = state.get_or_create_slot("s1")
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    slot.append("assistant", "")
    slot.messages[-1]["content"] = None
    slot.drain()

    status, body, _run_chat, variants, _armed = await _regenerate(state, slot)

    assert status == 200, body
    assert [v["content"] for v in variants] == [_REPLY]


def _note_without_block(slot) -> str:
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    slot.append("assistant", _NOTE)
    return _NOTE


def _note_after_person_steer(slot) -> str:
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    slot.append("inject", _BLOCK, "msg msg-inject")
    slot.append("user", "only cover the EU stage", "msg msg-u", meta={"steer": True})
    slot.append("assistant", _NOTE)
    return _NOTE


def _invisible_with_file_changes(slot) -> str:
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    slot.append("assistant", _ZWSP, meta={"file_changes": [{"path": "notes.md"}]})
    return _ZWSP


def _invisible_with_visible_variant(slot) -> str:
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY)
    row = slot.append("assistant", _ZWSP)
    row["variants"] = [{"content": "An earlier answer."}, {"content": _ZWSP}]
    row["variant_idx"] = 1
    return _ZWSP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        _note_without_block,
        _note_after_person_steer,
        _invisible_with_file_changes,
        _invisible_with_visible_variant,
    ],
)
async def test_a_row_the_dashboard_draws_stays_the_target(state, build):
    slot = state.get_or_create_slot("s1")
    target = build(slot)
    slot.drain()

    status, body, _run_chat, variants, _armed = await _regenerate(state, slot)

    assert status == 200, body
    assert target in [v["content"] for v in variants]
    assert _REPLY not in [v["content"] for v in variants]


@pytest.mark.asyncio
async def test_a_turn_holding_only_hidden_replies_is_refused(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", "an earlier request")
    slot.append("assistant", "the earlier reply")
    slot.append("user", _PROMPT)
    slot.append("assistant", _ZWSP)
    slot.drain()
    before = _transcript(slot)

    status, body, run_chat, _variants, _armed = await _regenerate(state, slot)

    assert (status, body["code"]) == (400, "no_assistant_message")
    assert _transcript(slot) == before
    run_chat.assert_not_called()


def _lone_hidden_reply(slot) -> None:
    slot.append("assistant", _ZWSP)


def _cron_opener(slot) -> None:
    slot.append(
        "inject",
        '[Cron notification from "nightly"] done',
        "msg msg-inject",
        meta={"injectKind": "cron"},
    )
    slot.append("assistant", _ZWSP)


def _agent_started_hidden_reply(slot) -> None:
    slot.append("user", _PROMPT)
    slot.append("assistant", _ZWSP, meta={AGENT_ORIGIN_META_KEY: {"kind": AGENT_STARTED_ACTOR}})


def _empty_request(slot) -> None:
    slot.append("user", "")
    slot.append("assistant", _ZWSP)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "code"),
    [
        (_lone_hidden_reply, "no_user_message"),
        (_cron_opener, "automated_turn_reply"),
        (_agent_started_hidden_reply, "agent_started_reply"),
        (_empty_request, "empty_user_message"),
    ],
)
async def test_every_earlier_refusal_still_answers_first(state, build, code):
    slot = state.get_or_create_slot("s1")
    build(slot)
    slot.drain()
    before = _transcript(slot)

    status, body, run_chat, _variants, _armed = await _regenerate(state, slot)

    assert (status, body["code"]) == (400, code)
    assert _transcript(slot) == before
    run_chat.assert_not_called()


@pytest.mark.asyncio
async def test_the_shown_reply_survives_the_swap_as_a_variant(state):
    slot = state.get_or_create_slot("s1")
    _s1(slot)
    slot.drain()
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history

    _save_slot_to_history(state, slot)

    async def _turn(*a, **kw):
        # A reply the turn recorded, then the turn's own append-only save.
        chat_runner._append_command_reply(slot, "Stage one ships to beta on Tuesday.")
        _save_slot_to_history(state, slot)
        await chat_runner.settle_regenerate(state, slot)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_turn):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/regenerate")
            assert resp.status == 200
            await slot.task
    new = "Stage one ships to beta on Tuesday."
    assert [m["content"] for m in slot.messages] == [_PROMPT, new]
    assert [v["content"] for v in slot.messages[-1]["variants"]] == [_REPLY, new]
    assert slot.messages[-1]["variant_idx"] == 1
    persisted = state.conversation_log.read_messages(_history_key_for(slot.key))
    assert [m.get("content") for m in persisted] == [_PROMPT, new]
