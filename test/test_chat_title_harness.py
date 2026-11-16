"""A claude chat takes its harness's session title only in place of Crew's echo.

Crew titles a chat itself; when its model names nothing the chat keeps the echo of its
first message (``chat_title._fallback_title_from_messages``). After chat_done's whole
title chain, ``chat_title_harness.adopt_harness_title`` puts the title claude-agent-acp
gave the session (``acp.harness_title``) where that echo is, and nowhere else: a
title Crew's model wrote, a rename, a pinned, crewmate or fork title, a slot Crew does
not auto-title and a session holding an app's or a restored turn are all kept.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_runner_coverage_helpers import _slot as _runner_slot
from chat_runner_coverage_helpers import _state as _runner_state

from kiro_crew.acp import harness_title
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_runner, chat_title, chat_title_harness
from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_MEMBER,
    _TITLE_ORIGIN_USER,
    FORK_TITLE_MARKER,
    RESTORED_TURN_META_KEY,
    apply_manual_title,
)
from kiro_crew.dashboard.chat_title_harness import adopt_harness_title, titled_then_harness
from kiro_crew.dashboard.state import NEW_SESSION_TITLE, _ChatSlot
from kiro_crew.members import DM_SLOT_MODE
from kiro_crew.testing.wait import await_until, finished, until_parked

OPENER = "hey there, can you help me with something quick?"
HARNESS = "Quick help request"
SESSION = "claude-session-chat"


class _Log:
    """A conversation log that records each title write the metadata fold allows."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def update_metadata_if(self, _key, fields, fold, *, after_commit_under_lock=None):
        if not fold({}):
            return False
        self.writes.append(dict(fields))
        if after_commit_under_lock is not None:
            after_commit_under_lock()
        return True


def _claude(title: str = HARNESS, backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = SESSION
    update = {"sessionUpdate": "session_info_update", "title": title}
    harness_title.note(
        client,
        JsonRpcMessage(method="session/update", params={"sessionId": SESSION, "update": update}),
    )
    return client


def _state(slot: _ChatSlot, provider: Any = "claude") -> MagicMock:
    state = MagicMock()
    state.conversation_log = _Log()
    state._slots = {slot.key: slot}
    if provider == "claude":
        provider = types.SimpleNamespace(client=_claude())
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


def _echo_slot(
    first: str = OPENER, *, locked: bool = False, rows: list[dict[str, Any]] | None = None
) -> _ChatSlot:
    """A slot whose title is Crew's own echo of its first message, as Crew leaves it."""
    slot = _ChatSlot("chat-harness-1")
    slot.messages = rows or [
        {"role": "user", "content": first},
        {"role": "assistant", "content": "Sure, what do you need?"},
    ]
    slot.title = chat_title._fallback_title_from_messages(chat_title._titling_messages(slot))
    slot._titled = locked
    slot._title_origin = _TITLE_ORIGIN_AUTO
    slot._title_low_signal = True
    return slot


@pytest.fixture(autouse=True)
def _no_folder_card(_floor_monkeypatch):
    suggest = AsyncMock()
    _floor_monkeypatch.setattr(chat_title_harness, "maybe_suggest_folder", suggest)
    return suggest


@pytest.mark.asyncio
async def test_crews_own_generated_title_is_never_replaced():
    slot = _echo_slot(locked=True)
    slot.title = "Help with a quick question"
    state = _state(slot)
    assert await adopt_harness_title(state, slot) is False
    assert slot.title == "Help with a quick question"
    state.push_slot_title.assert_not_called()
    assert state.conversation_log.writes == []


@pytest.mark.asyncio
async def test_an_unlocked_fallback_takes_the_harness_title(_no_folder_card):
    slot = _echo_slot(locked=False)
    epoch = slot._title_epoch
    state = _state(slot)
    assert await adopt_harness_title(state, slot) is True
    assert slot.title == HARNESS
    assert slot._titled is True
    assert slot._title_origin == _TITLE_ORIGIN_AUTO
    assert slot._title_low_signal is False
    assert slot._title_epoch == epoch + 1
    assert [(w["title"], w["title_origin"]) for w in state.conversation_log.writes] == [
        (HARNESS, _TITLE_ORIGIN_AUTO)
    ]
    state.push_slot_title.assert_called_once_with(slot.key, HARNESS)
    _no_folder_card.assert_awaited_once_with(state, slot)


@pytest.mark.parametrize("first", [OPENER, ""], ids=["skip", "attachment-only"])
@pytest.mark.asyncio
async def test_a_locked_echo_fallback_takes_the_harness_title(first, _no_folder_card):
    slot = _echo_slot(first, locked=True)
    if not first:
        assert slot.title == NEW_SESSION_TITLE
    state = _state(slot)
    assert await adopt_harness_title(state, slot) is True
    assert slot.title == HARNESS
    state.push_slot_title.assert_called_once_with(slot.key, HARNESS)
    # A locked echo was offered its folder card when Crew settled it.
    _no_folder_card.assert_not_awaited()


@pytest.mark.parametrize("kept", ["rename", "pinned", "member-name", "member-thread"])
@pytest.mark.asyncio
async def test_a_user_rename_a_pinned_title_a_member_name_or_a_member_thread_is_kept(kept):
    slot = _echo_slot(locked=True)
    state = _state(slot)
    if kept == "rename":
        await apply_manual_title(state, slot, "My own name", route="test", caller="test")
    elif kept == "pinned":
        slot._title_origin = _TITLE_ORIGIN_USER  # slot_create pins a given title as user
    elif kept == "member-name":
        slot._title_origin = _TITLE_ORIGIN_MEMBER
    else:
        slot.mode = DM_SLOT_MODE
    title = slot.title
    state.push_slot_title.reset_mock()
    assert await adopt_harness_title(state, slot) is False
    assert slot.title == title
    state.push_slot_title.assert_not_called()


@pytest.mark.asyncio
async def test_a_slot_crew_never_auto_titled_is_left_alone():
    slot = _echo_slot()
    slot._title_origin = ""  # a cron, campaign or plan slot Crew does not title
    state = _state(slot)
    assert await adopt_harness_title(state, slot) is False
    assert slot.title != HARNESS


@pytest.mark.parametrize("fork", ["marker", "pending"])
@pytest.mark.asyncio
async def test_a_fork_child_is_left_alone(fork, monkeypatch):
    slot = _echo_slot()
    if fork == "marker":
        slot.title = f"{FORK_TITLE_MARKER}{slot.title}"
    else:
        monkeypatch.setattr(chat_title_harness, "fork_title_pending", lambda _slot: True)
    title = slot.title
    assert await adopt_harness_title(_state(slot), slot) is False
    assert slot.title == title


@pytest.mark.parametrize(
    "meta", [{TURN_ACTOR_META_KEY: "app"}, {RESTORED_TURN_META_KEY: True}], ids=["app", "restored"]
)
@pytest.mark.asyncio
async def test_a_session_with_an_app_or_restored_turn_is_left_alone(meta):
    rows = [
        {"role": "user", "content": "an app's opening words", "meta": meta},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": OPENER},
        {"role": "assistant", "content": "Sure."},
    ]
    slot = _echo_slot(rows=rows)
    assert slot.title.startswith("hey there")
    title = slot.title
    assert await adopt_harness_title(_state(slot), slot) is False
    assert slot.title == title


@pytest.mark.parametrize(
    "offered",
    [
        OPENER[:40] + "x" * 215 + "…",
        "Help me with…",
        "a" * 81,
        "This is the session about a quick question.",
        "SKIP",
        "KEEP",
        OPENER,
        "  HEY there, can you help me with   something quick ",
        "Hey there, can you",
        "Read https://example.com/docs/page",
        "Ticket 12345678 triage",
    ],
    ids=[
        "summary",
        "cut",
        "long",
        "sentence",
        "skip",
        "keep",
        "echo-sentence",
        "echo",
        "echo-prefix",
        "url",
        "opaque-id",
    ],
)
@pytest.mark.asyncio
async def test_a_cut_long_prose_echo_or_low_signal_harness_title_is_refused(offered):
    slot = _echo_slot()
    title = slot.title
    state = _state(slot, types.SimpleNamespace(client=_claude(offered)))
    assert await adopt_harness_title(state, slot) is False
    assert slot.title == title
    assert state.conversation_log.writes == []


@pytest.mark.parametrize("hop", ["config", "save"])
@pytest.mark.asyncio
async def test_a_rename_during_the_config_hop_or_the_save_wins(hop, monkeypatch):
    slot = _echo_slot()
    state = _state(slot)
    entered, release = asyncio.Event(), asyncio.Event()
    if hop == "config":
        real_key = chat_title_harness._ticket_key_if_enabled

        async def gated_key(key: str) -> str:
            entered.set()
            await release.wait()
            return await real_key(key)

        monkeypatch.setattr(chat_title_harness, "_ticket_key_if_enabled", gated_key)
    else:
        real_persist = chat_title_harness._persist_title

        async def gated_persist(*args: Any, **kwargs: Any) -> bool:
            entered.set()
            await release.wait()
            return await real_persist(*args, **kwargs)

        monkeypatch.setattr(chat_title_harness, "_persist_title", gated_persist)
    adopting = asyncio.ensure_future(adopt_harness_title(state, slot))
    await await_until(entered, what="adoption reaching its await")
    await apply_manual_title(state, slot, "Renamed", route="test", caller="test")
    release.set()
    await await_until(adopting, what="adoption finishing")
    assert slot.title == "Renamed"
    assert slot._title_origin == _TITLE_ORIGIN_USER
    assert [c.args for c in state.push_slot_title.call_args_list] == [(slot.key, "Renamed")]
    assert state.conversation_log.writes[-1]["title"] == "Renamed"


@pytest.mark.asyncio
async def test_a_slot_replaced_at_its_key_is_neither_written_nor_pushed(monkeypatch):
    slot = _echo_slot()
    state = _state(slot)
    real_persist = chat_title_harness._persist_title

    async def replaced_then_persist(*args: Any, **kwargs: Any) -> bool:
        state._slots[slot.key] = _ChatSlot(slot.key)
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(chat_title_harness, "_persist_title", replaced_then_persist)
    await adopt_harness_title(state, slot)
    assert state.conversation_log.writes == []
    state.push_slot_title.assert_not_called()


@pytest.mark.asyncio
async def test_the_ticket_key_leads_an_adopted_title_when_title_ticket_prefix_is_on(monkeypatch):
    monkeypatch.setattr(chat_title, "_title_ticket_prefix_enabled", lambda: True)
    slot = _echo_slot("PROJ-1234 hey there, can you help me with something quick?")
    assert await adopt_harness_title(_state(slot), slot) is True
    assert slot.title == f"PROJ-1234: {HARNESS}"


@pytest.mark.parametrize("crew_names_it", [True, False])
@pytest.mark.asyncio
async def test_adoption_runs_only_after_crews_chain(crew_names_it):
    slot = _echo_slot()
    state = _state(slot)
    release = asyncio.Event()

    async def crews_chain() -> None:
        await release.wait()
        if crew_names_it:
            slot.title = "Crew's own name"
            slot._titled = True

    task = asyncio.ensure_future(titled_then_harness(state, slot, crews_chain()))
    await until_parked(release)
    assert slot.title != HARNESS, "nothing is adopted while Crew's chain runs"
    release.set()
    await await_until(task, what="the chain and the adoption")
    assert slot.title == ("Crew's own name" if crew_names_it else HARNESS)


@pytest.mark.parametrize(
    "provider",
    [None, MagicMock(), "kiro"],
    ids=["none", "mock", "kiro"],
)
@pytest.mark.asyncio
async def test_a_non_claude_provider_or_no_provider_offers_nothing(provider):
    if provider == "kiro":
        kiro = AcpClient(acp_backend=ACP_BACKEND_KIRO)
        kiro._session_id = SESSION
        kiro._harness_title = harness_title.HarnessTitle(SESSION, HARNESS, 0)
        provider = types.SimpleNamespace(client=kiro)
    slot = _echo_slot()
    title = slot.title
    assert await adopt_harness_title(_state(slot, provider), slot) is False
    assert slot.title == title


@pytest.mark.asyncio
async def test_chat_done_spawns_one_task_and_keeps_the_title_then_refresh_patch_seam(
    tmp_path, monkeypatch
):
    state, slot = _runner_state(tmp_path), _runner_slot()
    slot._cycle_reached_provider = True
    order: list[str] = []
    chain = AsyncMock(side_effect=lambda *_a: order.append("chain"))
    adopt = AsyncMock(side_effect=lambda *_a: order.append("harness"))
    monkeypatch.setattr(chat_runner, "title_then_refresh", chain)
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    monkeypatch.setattr(chat_title_harness, "adopt_harness_title", adopt)
    before = set(state._background_tasks)
    await chat_runner._finish_queue_cycle(state, slot)
    for task in set(state._background_tasks) - before:
        await await_until(task, what="a chat_done background task")
    chain.assert_awaited_once_with(state, slot)
    adopt.assert_awaited_once_with(state, slot)
    assert order == ["chain", "harness"]


@pytest.mark.asyncio
async def test_a_failing_adoption_never_fails_the_chain(monkeypatch):
    slot = _echo_slot()
    monkeypatch.setattr(
        chat_title_harness, "adopt_harness_title", AsyncMock(side_effect=RuntimeError("boom"))
    )

    async def crews_chain() -> None:
        return None

    await finished(titled_then_harness(_state(slot), slot, crews_chain()), "the chain")
