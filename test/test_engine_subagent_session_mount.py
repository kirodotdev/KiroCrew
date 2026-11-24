"""Which chats' sub-agents are the engine's own is what the session registered, not the switch now.

A chat keeps the sub-agent path it was opened with: the ``kas_subagents`` preview
is read when a session registers its agents, and moving it reaches only a session
registered afterwards. So a chat that mounted the engine's sub-agent tool still runs
engine children after the preview is turned off, and its cards must stay engine
cards (marked ``engine``, which is what takes the Cancel button off the card and
puts the child on the chip) and a Stop must still retire the approval cards it
rejected on every page. A chat registered with the preview off, whose preview is
then turned on, mounted nothing and is drawn as it was.

The registration is the real one: ``KasHarness.session_extras`` builds the batch
under the preview's value at that moment and ``record_session_projection`` writes
what it mounted on a real ``AcpSessionHandle``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from engine_turn_helpers import client, live_reading_says_kas, session
from kas_engine_child_helpers import (
    _HELPER,
    _agents_dir,
    _batch,
    _default_spec,
    _handle,
    _own,
    _setting,
    _write,
)

from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.acp.session_handle_permissions import _shipped_helper_mounted
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard import engine_subagent_cards as cards

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

SUBTASK = "31c14746-6447-4300-8ee9-074e74540ae3"
CALL = "invoke_subagent_toolu_bdrk_019Hyz9XEeDzAmxkRpDxDj4q"
CHILD_CALL = "run_command_toolu_1"
_OTHER = {"on": "off", "off": "on", "unset": "on"}


async def _registered(agents_dir: Any, setting: Any, tmp_path: Any, preview: str) -> Any:
    """A default-agent dashboard session registered while the preview read *preview*."""
    _write(agents_dir, _default_spec())
    setting(preview)
    batch = await _batch()
    assert (_HELPER in {a["id"] for a in batch}) is (preview == "on")
    return _handle(batch, tmp_path, sid="sess-1")


def _slot_on(handle: Any) -> Any:
    """A slot whose running turn published *handle*'s client."""
    return SimpleNamespace(_acp_client=client(handle))


# ── the reading ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("preview", "expected"), [("on", True), ("off", False), ("unset", False)])
async def test_the_reading_is_what_the_session_registered_whatever_the_switch_reads_now(
    agents_dir, setting, tmp_path, preview: str, expected: bool
) -> None:
    handle = await _registered(agents_dir, setting, tmp_path, preview)
    slot = _slot_on(handle)
    assert cards.session_runs_engine_children(slot) is expected

    # The switch moves while the chat is open: the chat keeps what it mounted.
    setting(_OTHER[preview])
    assert cards.session_runs_engine_children(slot) is expected
    # One rule with the session's permission stamps, which read the same record.
    assert cards.session_runs_engine_children(slot) is _shipped_helper_mounted(handle)


@pytest.mark.asyncio
async def test_a_session_moved_to_another_agent_is_no_longer_the_mounts(
    agents_dir, setting, tmp_path
) -> None:
    """A KAS mode switch moves ``kas_projected_agent``: the helper was mounted for the
    agent the session left, so its children are not the helper's any more."""
    handle = await _registered(agents_dir, setting, tmp_path, "on")
    handle.kas_projected_agent = "someone-else"
    assert cards.session_runs_engine_children(_slot_on(handle)) is False
    assert _shipped_helper_mounted(handle) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", ["on", "off"])
async def test_a_custom_agent_that_mounts_the_engines_tool_itself_is_not_an_engine_chat(
    agents_dir, setting, tmp_path, preview: str
) -> None:
    """A person's agent whose own spec lists sub-agents runs them in the engine on
    either value of the preview; Kiro Crew mounted nothing for it, so its cards are
    the native cards they were."""
    _write(
        agents_dir,
        _own(
            "boss",
            tools=["fs_read", "use_subagent"],
            toolsSettings={"subagent": {"availableAgents": ["own-model"]}},
        ),
    )
    _write(agents_dir, _own("own-model"))
    setting(preview)
    batch = await _batch("boss")
    assert {"boss", "own-model"} <= {a["id"] for a in batch}
    handle = _handle(batch, tmp_path, agent="boss", sid="sess-1")

    assert cards.session_runs_engine_children(_slot_on(handle)) is False


@pytest.mark.asyncio
async def test_a_session_registered_again_answers_by_its_newest_registration(
    agents_dir, setting, tmp_path
) -> None:
    """A session resumed after a gateway restart registers its agents again, on a new
    handle, under the preview as it reads then: that registration is the answer."""
    before = await _registered(agents_dir, setting, tmp_path, "on")
    assert cards.session_runs_engine_children(_slot_on(before)) is True

    setting("off")
    resumed = _handle(await _batch(), tmp_path, sid="sess-1")
    assert cards.session_runs_engine_children(_slot_on(resumed)) is False

    setting("on")
    resumed_on = _handle(await _batch(), tmp_path, sid="sess-1")
    assert cards.session_runs_engine_children(_slot_on(resumed_on)) is True


@pytest.mark.parametrize(
    "slot",
    [
        object(),
        _slot_on(None),
        _slot_on(object()),
    ],
    ids=["no-turn", "no-handle", "another-harness"],
)
def test_a_slot_with_no_registration_to_read_is_not_an_engine_chat(slot: Any) -> None:
    assert cards.session_runs_engine_children(slot) is False


# ── a real turn after the switch moved ────────────────────────────────────────


def _roster(status: str) -> list[dict[str, Any]]:
    parser = SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )
    frame = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": CALL,
        "title": "Sub-agent: kirocrew-helper",
        "status": status,
        "rawInput": {"name": "kirocrew-helper", "prompt": "Run ls and report"},
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": SUBTASK}},
    }
    [event] = children._handle_kas_subagent(parser, frame) or []
    return event.subagents


def _stopped_turn(handle: Any) -> Any:
    """An engine child starts, its call waits on an approval card, and a Stop lands."""
    from turn_harness import STOP, TurnScript

    return TurnScript(
        events=[
            AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_roster("in_progress")),
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="req-child",
                tool_call_id=CHILD_CALL,
                title="ls",
                tool_kind="execute",
                requesting_subagent="kirocrew-helper",
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Stopped."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ],
        answers={"req-child": STOP},
        provider=session(handle),
    )


def _retired_by_the_stop(turn: Any) -> list[dict[str, Any]]:
    """The ``approval_resolved`` frames that say a card was rejected."""
    return [f.payload for f in turn.frames("approval_resolved") if f.payload["approved"] is False]


async def _turn_after_the_switch_moved(
    agents_dir: Any, setting: Any, tmp_path: Any, monkeypatch: Any, opened_with: str
) -> Any:
    """A turn of a chat registered while the preview read *opened_with*, run after the
    preview was moved to its other value. The live reading's own inputs (a KAS
    session, the preview as it reads now) say the opposite of what the chat mounted."""
    from turn_harness import run_turn

    handle = await _registered(agents_dir, setting, tmp_path, opened_with)
    setting(_OTHER[opened_with])
    live_reading_says_kas(monkeypatch)
    return await run_turn(_stopped_turn(handle))


@pytest.mark.asyncio
async def test_a_chat_opened_with_the_preview_on_keeps_its_engine_cards_after_it_is_turned_off(
    agents_dir, setting, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await _turn_after_the_switch_moved(agents_dir, setting, tmp_path, monkeypatch, "on")

    [spawn] = [f.payload for f in turn.frames("subagent_spawn")]
    assert spawn.get("engine") is True
    [card] = turn.approval_cards
    assert card["meta"].get("asked_by_subagent") == "kirocrew-helper"


@pytest.mark.asyncio
async def test_a_stop_in_that_chat_still_retires_the_approval_card_it_rejected(
    agents_dir, setting, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await _turn_after_the_switch_moved(agents_dir, setting, tmp_path, monkeypatch, "on")

    [retired] = _retired_by_the_stop(turn)
    assert retired["id"] == "req-child"


@pytest.mark.asyncio
async def test_a_chat_opened_with_the_preview_off_gains_no_engine_mark_when_it_is_turned_on(
    agents_dir, setting, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await _turn_after_the_switch_moved(agents_dir, setting, tmp_path, monkeypatch, "off")

    [spawn] = [f.payload for f in turn.frames("subagent_spawn")]
    assert "engine" not in spawn
    [card] = turn.approval_cards
    assert "asked_by_subagent" not in card["meta"]


@pytest.mark.asyncio
async def test_a_stop_in_that_chat_sends_no_frame_of_its_own_as_before(
    agents_dir, setting, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await _turn_after_the_switch_moved(agents_dir, setting, tmp_path, monkeypatch, "off")

    assert _retired_by_the_stop(turn) == []
