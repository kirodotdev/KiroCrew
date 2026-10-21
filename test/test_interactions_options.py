"""OPTIONS controls and action buttons in Slack, driven through ``kiro_crew.slack.interactions``.

Covers the single-click OPTIONS path end to end (the in-place edit, the post-and-delete
fallback, a delete that fails and keeps the control's record, a duplicate click, a
superseded control struck through, the answer pinned to the conversation that asked),
the multi-select submit's stale and duplicate refusals, the ``action::`` routing edge
cases (an overflow label, a failing message edit, an oversized payload), the helpers
that read a control back off its own blocks, and the Slack-thread import into a
dashboard slot.

Every patch goes through the facade; it forwards the write to the owner module that
reads the name. The per-message claim and edit-lock maps are reset between tests by
``test/conftest.py``.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import chat_persistence
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
)
from kiro_crew.slack.outbound import (
    PostedOptions,
    claim_options_answer,
    encode_options_token,
)
from kiro_crew.testing.wait import await_until

LOGGER = "kiro_crew.slack.interactions"
MINTED_TS = "2026-10-01T10:00:00+00:00"
LATER_TS = "2026-10-01T10:05:00+00:00"
HEADER = {"type": "section", "text": {"type": "mrkdwn", "text": "Which one?"}}
EXFIL_BLOB = "A" * 50
EXFIL_URL = f"https://evil.com/exfil?data={EXFIL_BLOB}"


# ---------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------


class _Log:
    """A conversation log whose newest transcript row is *newest*."""

    def __init__(self, newest: str) -> None:
        self.newest = newest
        self.asked: list[str] = []

    def last_row_ts(self, session_key: str) -> str:
        self.asked.append(session_key)
        return self.newest


def _make_orch() -> MagicMock:
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.update_message = AsyncMock()
    orch.slack.post_message = AsyncMock(return_value="label-ts")
    orch.slack.post_blocks = AsyncMock(return_value="fallback-ts")
    orch.slack.delete_message = AsyncMock()
    orch.dashboard_state = None
    orch._handler_tasks = set()
    return orch


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A bound orchestrator double, every caller allowed."""
    o = _make_orch()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
    return o


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    run = AsyncMock()
    monkeypatch.setattr(ix, "handle_message", run)
    return run


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    recorder = MagicMock()
    monkeypatch.setattr(ix, "sel", lambda: recorder)
    return recorder


@pytest.fixture
def forget(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Records the forget of a spent control, with the owner keys pinned to one value."""
    recorder = MagicMock()
    monkeypatch.setattr(ix, "forget_slack_options_for_thread", recorder)
    monkeypatch.setattr(ix, "slack_options_owner_keys_snapshot", lambda state, ts: ("slack:t1",))
    return recorder


async def _join(orch: MagicMock) -> None:
    tasks = list(orch._handler_tasks)
    if tasks:
        await await_until(asyncio.gather(*tasks), what="the handler's background tasks")


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def _buttons(choices: list[str], block_id: str | None = None) -> dict:
    block: dict[str, Any] = {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": f"{OPTIONS_ACTION_PREFIX}{i}",
                "text": {"type": "plain_text", "text": c},
                "value": c,
            }
            for i, c in enumerate(choices)
        ],
    }
    if block_id:
        block["block_id"] = block_id
    return block


def _checkboxes(choices: list[str], block_id: str = "blk1") -> dict:
    return {
        "type": "actions",
        "block_id": block_id,
        "elements": [
            {
                "type": "checkboxes",
                "action_id": OPTIONS_CHECKBOXES_ACTION,
                "options": [
                    {"text": {"type": "plain_text", "text": c}, "value": c} for c in choices
                ],
            },
            {
                "type": "button",
                "action_id": OPTIONS_SUBMIT_ACTION,
                "text": {"type": "plain_text", "text": "Send"},
            },
        ],
    }


def _click(choices: list[str], index: int, *, block_id: str | None = None) -> tuple[dict, dict]:
    """A single-click OPTIONS press on button *index*, from thread ``t1`` message ``m1``."""
    control = _buttons(choices, block_id)
    action = dict(control["elements"][index])
    if block_id:
        action["block_id"] = block_id
    payload = {
        "user": {"id": "U1"},
        "team": {"id": "T1"},
        "message": {"ts": "m1", "thread_ts": "t1", "blocks": [HEADER, control]},
    }
    return payload, action


def _submit(choices: list[str], picked: list[str], block_id: str = "blk1") -> dict:
    """A multi-select Send press from thread ``t1`` message ``m1``."""
    return {
        "user": {"id": "U1"},
        "team": {"id": "T1"},
        "message": {"ts": "m1", "thread_ts": "t1", "blocks": [_checkboxes(choices, block_id)]},
        "state": {
            "values": {
                block_id: {
                    OPTIONS_CHECKBOXES_ACTION: {
                        "selected_options": [{"value": v} for v in picked],
                    }
                }
            }
        },
    }


def _summary(blocks: list[dict]) -> str:
    """The mrkdwn text of the context block that replaced the control."""
    texts = [
        el["text"]
        for b in blocks
        if b.get("type") == "context"
        for el in b.get("elements", [])
        if el.get("type") == "mrkdwn"
    ]
    assert len(texts) == 1, blocks
    return texts[0]


def _has_live_control(blocks: list[dict]) -> bool:
    return any(
        el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
        or el.get("action_id") in (OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION)
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    )


# ---------------------------------------------------------------------------
# Single-click OPTIONS
# ---------------------------------------------------------------------------


class TestSingleClickOptions:
    @pytest.mark.asyncio
    async def test_a_choice_is_rendered_in_place_and_sent_to_the_session(
        self, orch: MagicMock, turns: AsyncMock, forget: MagicMock
    ) -> None:
        payload, action = _click(["A", "B"], 1)
        await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        orch.slack.update_message.assert_awaited_once()
        (channel, ts), kwargs = orch.slack.update_message.await_args
        assert (channel, ts, kwargs["text"]) == ("C1", "m1", "B")
        assert kwargs["blocks"][0] is HEADER
        assert not _has_live_control(kwargs["blocks"])
        assert _summary(kwargs["blocks"]) == "~A~  |  *B*"
        orch.slack.post_blocks.assert_not_awaited()
        orch.slack.delete_message.assert_not_awaited()

        turns.assert_awaited_once()
        args, hm = turns.await_args.args, turns.await_args.kwargs
        assert args[2:7] == ("C1", "B", "t1", "m1", "U1")
        assert hm["team_id"] == "T1"
        assert (hm["target_slot_name"], hm["route_pinned"], hm["asker_key"]) == (None, False, None)
        forget.assert_called_once_with(None, "t1", "m1", keys=("slack:t1",))
        # The answer is spent: a later click on the same control cannot claim it.
        assert claim_options_answer("C1", "m1") is False

    @pytest.mark.asyncio
    async def test_a_failed_edit_falls_back_to_a_new_post_and_a_delete(
        self, orch: MagicMock, turns: AsyncMock, forget: MagicMock
    ) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("slack 500"))
        payload, action = _click(["A", "B"], 0)
        await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        orch.slack.post_blocks.assert_awaited_once()
        channel, blocks, text, thread_ts = orch.slack.post_blocks.await_args.args
        assert (channel, text, thread_ts) == ("C1", "A", "t1")
        assert _summary(blocks) == "*A*  |  ~B~"
        orch.slack.delete_message.assert_awaited_once_with("C1", "m1")
        assert turns.await_args.args[2:7] == ("C1", "A", "t1", "fallback-ts", "U1")
        forget.assert_called_once_with(None, "t1", "m1", keys=("slack:t1",))

    @pytest.mark.asyncio
    async def test_a_failed_delete_keeps_the_record_and_still_answers(
        self,
        orch: MagicMock,
        turns: AsyncMock,
        forget: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("slack 500"))
        orch.slack.delete_message = AsyncMock(side_effect=RuntimeError("cant_delete"))
        payload, action = _click(["A", "B"], 1)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        # The original buttons are still on screen, so their record must outlive the click.
        forget.assert_not_called()
        assert any("Failed to delete original OPTIONS message" in m for m in _messages(caplog))
        assert turns.await_args.args[2:7] == ("C1", "B", "t1", "fallback-ts", "U1")
        # The selection reached Slack, so the claim stays held against a second click.
        assert claim_options_answer("C1", "m1") is False

    @pytest.mark.asyncio
    async def test_a_second_click_on_an_answered_control_does_nothing(
        self,
        orch: MagicMock,
        turns: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        assert claim_options_answer("C1", "m1") is True
        payload, action = _click(["A", "B"], 0)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        orch.slack.update_message.assert_not_awaited()
        orch.slack.post_blocks.assert_not_awaited()
        turns.assert_not_awaited()
        assert orch._handler_tasks == set()
        assert any("was already answered" in m for m in _messages(caplog))

    @pytest.mark.asyncio
    async def test_a_superseded_choice_is_struck_through_and_not_answered(
        self, orch: MagicMock, turns: AsyncMock
    ) -> None:
        log = _Log(LATER_TS)
        orch.dashboard_state = SimpleNamespace(conversation_log=log)
        token = encode_options_token("slack:t1", MINTED_TS)
        payload, action = _click(["A", "B"], 0, block_id=token)
        await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        assert log.asked == ["slack:t1"]
        turns.assert_not_awaited()
        orch.slack.update_message.assert_awaited_once()
        kwargs = orch.slack.update_message.await_args.kwargs
        assert _summary(kwargs["blocks"]) == "~A~  |  ~B~"
        assert kwargs["blocks"][0] is HEADER
        # Struck through means retired: no later click may answer it.
        assert claim_options_answer("C1", "m1") is False

    @pytest.mark.asyncio
    async def test_a_tokened_choice_is_pinned_to_the_conversation_that_asked(
        self,
        orch: MagicMock,
        turns: AsyncMock,
        forget: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = SimpleNamespace(conversation_log=_Log(MINTED_TS))
        orch.dashboard_state = state
        slot_lookup = MagicMock(return_value=SimpleNamespace(key="chat-1"))
        monkeypatch.setattr(ix, "slack_options_slot", slot_lookup)
        token = encode_options_token("dashboard:chat-1", MINTED_TS)
        payload, action = _click(["A", "B"], 1, block_id=token)
        await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        slot_lookup.assert_called_once_with(state, "dashboard:chat-1")
        hm = turns.await_args.kwargs
        assert hm["target_slot_name"] == "chat-1"
        assert hm["route_pinned"] is True
        assert hm["asker_key"] == "dashboard:chat-1"
        forget.assert_called_once_with(state, "t1", "m1", keys=("slack:t1",))

    @pytest.mark.parametrize(
        "action,channel,bound",
        [
            pytest.param({"action_id": "x"}, "C1", True, id="no-choice"),
            pytest.param({"action_id": "x", "value": "A"}, "", True, id="no-channel"),
            pytest.param({"action_id": "x", "value": "A"}, "C1", False, id="no-orchestrator"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_unanswerable_press_is_ignored(
        self,
        orch: MagicMock,
        turns: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        action: dict,
        channel: str,
        bound: bool,
    ) -> None:
        if not bound:
            monkeypatch.setattr(ix, "_orch", None)
        payload, _ = _click(["A", "B"], 0)
        await ix._handle_options(payload, action, channel, "m1")
        orch.slack.update_message.assert_not_awaited()
        orch.slack.post_message.assert_not_awaited()
        turns.assert_not_awaited()
        assert claim_options_answer("C1", "m1") is True


# ---------------------------------------------------------------------------
# action:: buttons and elements
# ---------------------------------------------------------------------------


class TestActionRouting:
    @pytest.mark.asyncio
    async def test_an_overflow_action_takes_its_label_from_the_selected_option(
        self, orch: MagicMock, turns: AsyncMock, audit: MagicMock
    ) -> None:
        action = {
            "action_id": "overflow_1",
            "selected_option": {"value": 'action::{"snooze": 5}', "text": {"text": "Snooze 5m"}},
        }
        payload, _ = _click(["A"], 0)
        await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        orch.slack.post_message.assert_awaited_once_with("C1", "Snooze 5m", "t1")
        assert orch.slack.update_message.await_args.kwargs["text"] == "Snooze 5m"
        args, hm = turns.await_args.args, turns.await_args.kwargs
        assert args[2:7] == ("C1", "Snooze 5m", "t1", "label-ts", "U1")
        assert '[Action button clicked: {"snooze": 5}]' in hm["action_context"]
        access = audit.log_api_access.call_args.kwargs
        assert (access["operation"], access["resources"]) == ("slack.action", "overflow_1")

    @pytest.mark.asyncio
    async def test_a_failing_message_edit_still_routes_the_action(
        self,
        orch: MagicMock,
        turns: AsyncMock,
        audit: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("slack 500"))
        action = {"action_id": "btn_go", "value": 'action::{"go": 1}', "text": {"text": "Go"}}
        payload, _ = _click(["A"], 0)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        assert "Failed to update action message" in _messages(caplog)
        orch.slack.post_message.assert_awaited_once_with("C1", "Go", "t1")
        turns.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_oversized_action_payload_is_capped(
        self, orch: MagicMock, turns: AsyncMock, audit: MagicMock
    ) -> None:
        body = "word " * 1000
        action = {"action_id": "btn_big", "value": f"action::{body}", "text": {"text": "Big"}}
        payload, _ = _click(["A"], 0)
        await ix._handle_options(payload, action, "C1", "m1")
        await _join(orch)

        context = turns.await_args.kwargs["action_context"]
        embedded = context.split("[Action button clicked: ", 1)[1].rsplit("]\n", 1)[0]
        assert embedded == body[: ix._ACTION_PAYLOAD_CAP] + "… [truncated]"


# ---------------------------------------------------------------------------
# Forgetting a spent control
# ---------------------------------------------------------------------------


class TestForgetOptionsControl:
    def test_nothing_is_forgotten_without_an_orchestrator_or_a_thread(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = MagicMock()
        monkeypatch.setattr(ix, "forget_slack_options_for_thread", recorder)
        ix._forget_options_control("", "m1")
        monkeypatch.setattr(ix, "_orch", None)
        ix._forget_options_control("t1", "m1")
        recorder.assert_not_called()

    def test_a_failing_forget_is_logged_not_raised(
        self,
        orch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        recorder = MagicMock(side_effect=RuntimeError("store unreadable"))
        monkeypatch.setattr(ix, "forget_slack_options_for_thread", recorder)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            ix._forget_options_control("t1", "m1", keys=("slack:t1",))
        recorder.assert_called_once_with(None, "t1", "m1", keys=("slack:t1",))
        assert "Failed to clear recorded OPTIONS control" in _messages(caplog)


# ---------------------------------------------------------------------------
# Reading a control back off its own blocks
# ---------------------------------------------------------------------------


class TestOptionsBlockId:
    def test_the_actions_own_block_id_wins(self) -> None:
        payload = {"actions": [{"block_id": "from-payload"}]}
        assert ix._options_block_id(payload, {"block_id": "from-action"}) == "from-action"

    def test_the_payload_actions_are_read_when_the_action_has_none(self) -> None:
        payload = {"actions": ["not-a-dict", {"block_id": ""}, {"block_id": "second"}]}
        assert ix._options_block_id(payload, {"block_id": 7}) == "second"

    def test_the_checkbox_state_key_is_the_last_resort(self) -> None:
        payload = {
            "actions": [],
            "state": {"values": {"other": {"x": {}}, "cb-block": {OPTIONS_CHECKBOXES_ACTION: {}}}},
        }
        assert ix._options_block_id(payload) == "cb-block"

    def test_an_unmarked_payload_has_no_block_id(self) -> None:
        assert ix._options_block_id({"state": None, "actions": None}) is None


class TestOptionsChoicesFromPayload:
    def test_checkbox_choices_read_their_label_then_their_value(self) -> None:
        blocks = [
            "not-a-block",
            {"type": "section", "elements": ["not-an-element"]},
            {
                "type": "actions",
                "elements": [
                    {
                        "action_id": OPTIONS_CHECKBOXES_ACTION,
                        "options": [
                            {"text": {"text": "Alpha"}, "value": "a"},
                            {"value": "b"},
                            {"text": {}},
                            "skipped",
                        ],
                    }
                ],
            },
        ]
        assert ix._options_choices_from_payload(blocks) == ["Alpha", "b", ""]

    def test_button_choices_are_gathered_across_every_block(self) -> None:
        blocks = [
            HEADER,
            _buttons(["A", "B"]),
            {"type": "actions", "elements": [{"action_id": "unrelated", "value": "z"}]},
            {"type": "actions", "elements": [{"action_id": f"{OPTIONS_ACTION_PREFIX}2"}]},
        ]
        assert ix._options_choices_from_payload(blocks) == ["A", "B", ""]

    def test_blocks_without_a_control_yield_no_choices(self) -> None:
        assert ix._options_choices_from_payload([HEADER]) == []
        assert ix._options_choices_from_payload(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Refusing a stale control
# ---------------------------------------------------------------------------


class TestRefuseStaleOptions:
    @pytest.mark.asyncio
    async def test_without_a_slack_client_nothing_is_edited(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slack = orch.slack
        orch.slack = None
        await ix._refuse_stale_options("C1", "m1", _submit(["A"], ["A"]))
        monkeypatch.setattr(ix, "_orch", None)
        await ix._refuse_stale_options("C1", "m1", _submit(["A"], ["A"]))
        slack.update_message.assert_not_awaited()
        assert claim_options_answer("C1", "m1") is True

    @pytest.mark.asyncio
    async def test_an_answered_control_is_left_as_the_answer_rendered_it(
        self, orch: MagicMock
    ) -> None:
        assert claim_options_answer("C1", "m1") is True
        await ix._refuse_stale_options("C1", "m1", _submit(["A", "B"], ["A"]))
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_stale_control_is_struck_through_and_retired(self, orch: MagicMock) -> None:
        payload = _submit(["A", "B"], ["A"])
        payload["message"]["blocks"].insert(0, HEADER)
        await ix._refuse_stale_options("C1", "m1", payload)
        orch.slack.update_message.assert_awaited_once()
        (channel, ts), kwargs = orch.slack.update_message.await_args
        assert (channel, ts) == ("C1", "m1")
        assert kwargs["blocks"][0] is HEADER
        assert _summary(kwargs["blocks"]) == "~A~  |  ~B~"
        assert not _has_live_control(kwargs["blocks"])
        assert claim_options_answer("C1", "m1") is False

    @pytest.mark.asyncio
    async def test_a_failing_strike_through_is_logged_and_the_control_stays_retired(
        self,
        orch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        expire = AsyncMock(side_effect=RuntimeError("slack 500"))
        monkeypatch.setattr(ix, "expire_options", expire)
        payload = _submit(["A", "B"], ["A"])
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._refuse_stale_options("C1", "m1", payload)
        expire.assert_awaited_once()
        slack, posted = expire.await_args.args
        assert slack is orch.slack
        assert posted == PostedOptions(
            channel="C1",
            ts="m1",
            choices=("A", "B"),
            blocks=tuple(payload["message"]["blocks"]),
        )
        assert any(
            "could not strike through the stale OPTIONS control" in m for m in _messages(caplog)
        )
        assert claim_options_answer("C1", "m1") is False


# ---------------------------------------------------------------------------
# Multi-select submit
# ---------------------------------------------------------------------------


class TestOptionsSubmitRefusals:
    @pytest.mark.asyncio
    async def test_a_superseded_submit_is_refused_and_audited(
        self, orch: MagicMock, turns: AsyncMock, audit: MagicMock
    ) -> None:
        orch.dashboard_state = SimpleNamespace(conversation_log=_Log(LATER_TS))
        token = encode_options_token("slack:t1", MINTED_TS)
        assert token is not None
        await ix._handle_options_submit(_submit(["A", "B"], ["B"], block_id=token), "C1", "m1")
        await _join(orch)

        turns.assert_not_awaited()
        invocation = audit.log_tool_invocation.call_args.kwargs
        assert invocation["outcome"] == "denied"
        assert invocation["metadata"] == {"reason": "superseded_control", "channel": "C1"}
        assert invocation["session_key"] == "t1"
        orch.slack.update_message.assert_awaited_once()
        assert _summary(orch.slack.update_message.await_args.kwargs["blocks"]) == "~A~  |  ~B~"

    @pytest.mark.asyncio
    async def test_a_second_send_on_an_answered_control_does_nothing(
        self,
        orch: MagicMock,
        turns: AsyncMock,
        audit: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        assert claim_options_answer("C1", "m1") is True
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await ix._handle_options_submit(_submit(["A", "B"], ["A"]), "C1", "m1")
        await _join(orch)

        orch.slack.update_message.assert_not_awaited()
        orch.slack.post_blocks.assert_not_awaited()
        turns.assert_not_awaited()
        audit.log_tool_invocation.assert_not_called()
        assert any(
            "was already answered; dropping the duplicate click" in m for m in _messages(caplog)
        )


# ---------------------------------------------------------------------------
# Importing a Slack thread into a dashboard slot
# ---------------------------------------------------------------------------


class _Slot:
    def __init__(self) -> None:
        self.key = "slot-1"
        self.title = ""
        self.rows: list[tuple[str, str, str]] = []

    def append(self, role: str, text: str, css: str) -> None:
        self.rows.append((role, text, css))


def _dashboard(linked: Any = None) -> MagicMock:
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=linked)
    ds.get_or_create_slot = MagicMock(return_value=_Slot())
    ds._self_bot_id = "UBOT"
    return ds


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_persistence, "save_slot_off_loop", save)
    return save


class TestImportThreadToSlot:
    @pytest.mark.asyncio
    async def test_an_already_linked_thread_returns_its_slot(self, saved: AsyncMock) -> None:
        existing = object()
        ds = _dashboard(linked=existing)
        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock()
        assert await ix._import_thread_to_slot(slack, ds, "C1", "1700000000.000100") is existing
        ds.get_linked_slot.assert_called_once_with("1700000000.000100")
        slack.fetch_thread_replies.assert_not_awaited()
        ds.get_or_create_slot.assert_not_called()
        saved.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_thread_of_only_blank_and_link_messages_imports_nothing(
        self, saved: AsyncMock
    ) -> None:
        ds = _dashboard()
        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(
            return_value=[
                {"user": "U1", "text": "   "},
                {"user": "U1"},
                {"user": "U1", "text": "!link-to-dashboard"},
            ]
        )
        assert await ix._import_thread_to_slot(slack, ds, "C1", "1700000000.000100") is None
        ds.get_or_create_slot.assert_not_called()
        ds.link_slack.assert_not_called()
        saved.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_long_thread_keeps_its_newest_fifty_messages_redacted(
        self, saved: AsyncMock
    ) -> None:
        ds = _dashboard()
        replies: list[dict] = [{"user": "U1", "text": f"msg {i}"} for i in range(55)]
        replies[-3] = {"bot_id": "B1", "text": "from a bot"}
        replies[-2] = {"user": "UBOT", "text": f"see {EXFIL_URL}"}
        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=replies)

        slot = await ix._import_thread_to_slot(slack, ds, "C1", "1700000000.000100")

        assert isinstance(slot, _Slot)
        slack.fetch_thread_replies.assert_awaited_once_with("C1", "1700000000.000100")
        assert slot.title == "Slack thread 1700000000 (truncated)"
        assert len(slot.rows) == 50
        assert slot.rows[0] == ("user", "msg 5", "msg msg-u")
        assert slot.rows[-1] == ("user", "msg 54", "msg msg-u")
        assert slot.rows[-3] == ("assistant", "from a bot", "msg msg-a")
        role, text, css = slot.rows[-2]
        assert (role, css) == ("assistant", "msg msg-a")
        assert EXFIL_BLOB not in text
        assert "REDACTED" in text
        ds.link_slack.assert_called_once_with("slot-1", "1700000000.000100", "C1")
        saved.assert_awaited_once_with(ds, slot)
        ds.push_slots_update.assert_called_once_with()
