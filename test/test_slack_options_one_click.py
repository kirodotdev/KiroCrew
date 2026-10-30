"""The Slack OPTIONS card: one-click buttons, a "Choose several..." modal, answered once.

Each choice is a plain button whose click sends that choice, more than five wrap into
further rows, and the last row's ⋯ menu opens a modal for picking several. After any
answer the card is edited in place to "✅ <who> chose <choice> · <time>"; a second or
stale click gets a private "Already answered" and sends nothing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack import interactions, outbound
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_SEVERAL_ACTION,
    OPTIONS_SEVERAL_BLOCK,
    OPTIONS_SEVERAL_CALLBACK,
    OPTIONS_SEVERAL_INPUT,
    build_options_blocks,
)
from kiro_crew.slack.outbound import decode_options_token, encode_options_token

_LONG = ("Rebase onto main, rerun the whole focused suite and then push with a lease " * 2).strip()


def _orch() -> MagicMock:
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.update_message = AsyncMock()
    orch.slack.post_blocks = AsyncMock(return_value="new_ts")
    orch.slack.delete_message = AsyncMock()
    orch.slack.post_ephemeral = AsyncMock()
    orch.slack.views_open = AsyncMock()
    orch.dashboard_state = None
    orch._handler_tasks = set()
    return orch


@pytest.fixture
def world(monkeypatch):
    orch = _orch()
    sent = AsyncMock()
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: uid == "U1")
    monkeypatch.setattr(interactions, "handle_message", sent)

    async def _never_stale(*_a, **_k):
        return False

    monkeypatch.setattr(interactions, "options_control_is_stale", _never_stale)
    monkeypatch.setattr(outbound, "_SEVERAL_PENDING", type(outbound._SEVERAL_PENDING)())
    monkeypatch.setattr(outbound, "_ANSWERED", {})
    return orch, sent


def _buttons(blocks: list[dict]) -> list[dict]:
    return [
        el
        for b in blocks
        if b.get("type") == "actions"
        for el in b["elements"]
        if el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
    ]


def _card_payload(choices: list[str], *, user: str = "U1", msg_ts: str = "m1") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "team": {"id": "T1"},
        "channel": {"id": "C1"},
        "trigger_id": "trig",
        "message": {
            "ts": msg_ts,
            "thread_ts": "t1",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Pick one"}}]
            + build_options_blocks(choices),
        },
    }


def _click(choices: list[str], index: int, **kw) -> dict:
    payload = _card_payload(choices, **kw)
    button = _buttons(payload["message"]["blocks"])[index]
    payload["actions"] = [dict(button)]
    return payload


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class TestTheCard:
    def test_every_choice_is_a_plain_button_with_no_checkboxes(self):
        blocks = build_options_blocks(["Yes", "No"])
        elements = [el for b in blocks for el in b["elements"]]
        assert [el["type"] for el in elements] == ["button", "button", "overflow"]
        assert all("style" not in el for el in elements if el["type"] == "button")
        assert not any(el["type"] == "checkboxes" for el in elements)

    def test_more_than_five_choices_wrap_into_rows(self):
        blocks = build_options_blocks([f"c{i}" for i in range(7)])
        rows = [b for b in blocks if b["type"] == "actions"]
        assert [len(_buttons([r])) for r in rows] == [5, 2]
        assert rows[-1]["elements"][-1]["action_id"] == OPTIONS_SEVERAL_ACTION

    def test_each_row_carries_the_token_under_a_unique_block_id(self):
        token = encode_options_token("slack:1.0", "2026-10-20T00:00:00Z")
        blocks = build_options_blocks([f"c{i}" for i in range(7)], staleness_token=token)
        ids = [b["block_id"] for b in blocks]
        assert len(set(ids)) == len(ids) == 2
        assert all(decode_options_token(i) == ("slack:1.0", "2026-10-20T00:00:00Z") for i in ids)

    def test_a_long_label_is_cut_but_its_value_is_whole(self):
        (button,) = _buttons(build_options_blocks([_LONG]))
        assert len(button["text"]["text"]) == 75 and button["text"]["text"].endswith("…")
        assert button["value"] == _LONG

    def test_a_single_choice_has_no_choose_several_menu(self):
        blocks = build_options_blocks(["Only"])
        assert [el["type"] for el in blocks[0]["elements"]] == ["button"]


class TestOneClick:
    @pytest.mark.asyncio
    async def test_a_click_sends_exactly_that_option(self, world):
        orch, sent = world
        await interactions.dispatch(_click(["Alpha", "Beta", "Gamma"], 1))
        await _settle()
        sent.assert_called_once()
        assert sent.call_args.args[3] == "Beta"
        assert "[OPTIONS multi-select: Beta]" in sent.call_args.kwargs["action_context"]

    @pytest.mark.asyncio
    async def test_the_card_is_edited_in_place_to_who_chose_what(self, world, monkeypatch):
        orch, _ = world
        forget = MagicMock()
        monkeypatch.setattr(interactions, "_forget_options_control", forget)
        await interactions.dispatch(_click(["Alpha", "Beta"], 0))
        forget.assert_called_once()  # the spent card's record is dropped
        orch.slack.update_message.assert_called_once()
        assert orch.slack.update_message.call_args.args[:2] == ("C1", "m1")
        blocks = orch.slack.update_message.call_args.kwargs["blocks"]
        assert not [b for b in blocks if b["type"] == "actions"], "buttons removed"
        assert blocks[0]["type"] == "section", "the question stays"
        line = blocks[-1]["elements"][0]["text"]
        assert line.startswith("✅ <@U1> chose *Alpha* · <!date^")
        orch.slack.post_blocks.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_long_label_sends_the_full_text(self, world):
        _, sent = world
        await interactions.dispatch(_click([_LONG, "Other"], 0))
        await _settle()
        assert sent.call_args.args[3] == _LONG

    @pytest.mark.asyncio
    async def test_a_double_click_sends_once_and_tells_the_second_clicker(self, world):
        orch, sent = world
        payload = _click(["Alpha", "Beta"], 0, msg_ts="m-double")
        await asyncio.gather(interactions.dispatch(payload), interactions.dispatch(payload))
        await _settle()
        assert sent.call_count == 1
        orch.slack.post_ephemeral.assert_called_once()
        assert orch.slack.post_ephemeral.call_args.args[2] == "Already answered."

    @pytest.mark.asyncio
    async def test_a_stale_card_sends_nothing(self, world, monkeypatch):
        orch, sent = world

        async def _stale(*_a, **_k):
            return True

        monkeypatch.setattr(interactions, "options_control_is_stale", _stale)
        await interactions.dispatch(_click(["Alpha", "Beta"], 0, msg_ts="m-stale"))
        await _settle()
        sent.assert_not_called()
        assert orch.slack.post_ephemeral.call_args.args[2] == "Already answered."

    @pytest.mark.asyncio
    async def test_an_unauthorized_click_is_refused_privately(self, world):
        orch, sent = world
        await interactions.dispatch(_click(["Alpha", "Beta"], 0, user="U-stranger"))
        await _settle()
        sent.assert_not_called()
        orch.slack.update_message.assert_not_called()
        orch.slack.post_ephemeral.assert_called_once()
        assert "not authorized" in orch.slack.post_ephemeral.call_args.args[2]


class TestReviewEdges:
    @pytest.mark.asyncio
    async def test_a_choice_written_as_an_action_value_still_answers_once(self, world):
        """``action::`` on a card's button is just the choice: guarded, sent once."""
        orch, sent = world
        payload = _click(['action::{"deploy": 1}', "B"], 0, msg_ts="m-action")
        await interactions.dispatch(payload)
        await interactions.dispatch(payload)
        await _settle()
        assert sent.call_count == 1
        assert sent.call_args.args[3] == 'action::{"deploy": 1}'
        assert orch.slack.post_ephemeral.call_args.args[2] == "Already answered."

    @pytest.mark.asyncio
    async def test_send_on_a_pre_upgrade_checkbox_card_is_told_it_expired(self, world):
        orch, sent = world
        payload = _card_payload(["A", "B"])
        payload["actions"] = [{"action_id": "options_submit", "type": "button"}]
        await interactions.dispatch(payload)
        sent.assert_not_called()
        assert "expired" in orch.slack.post_ephemeral.call_args.args[2]
        payload["actions"] = [{"action_id": "options_checkboxes", "type": "checkboxes"}]
        await interactions.dispatch(payload)
        assert orch.slack.post_ephemeral.call_count == 1, "ticking a box stays silent"

    def test_a_long_answered_line_keeps_its_time_stamp(self):
        from kiro_crew.slack.format import build_options_answered_blocks

        text = build_options_answered_blocks("U1", ["&" * 74] * 10, 1_760_000_000)[0]["elements"][
            0
        ]["text"]
        assert len(text) <= 3000
        assert "<!date^1760000000^{time}|" in text and text.endswith(" UTC>")

    @pytest.mark.asyncio
    async def test_cancelling_the_modal_frees_the_held_card(self, world):
        orch, _ = world
        await interactions.dispatch(_open(["A", "B"], msg_ts="m-cancel"))
        view = orch.slack.views_open.call_args.args[1]
        assert view["notify_on_close"] is True
        assert len(outbound._SEVERAL_PENDING) == 1
        await interactions.dispatch(
            {
                "type": "view_closed",
                "user": {"id": "U1"},
                "view": {
                    "callback_id": view["callback_id"],
                    "private_metadata": view["private_metadata"],
                },
            }
        )
        assert len(outbound._SEVERAL_PENDING) == 0


def _open(choices: list[str], **kw) -> dict:
    payload = _card_payload(choices, **kw)
    rows = [b for b in payload["message"]["blocks"] if b["type"] == "actions"]
    menu = rows[-1]["elements"][-1]
    payload["actions"] = [{**menu, "selected_option": menu["options"][0]}]
    return payload


def _submission(view: dict, picks: list[int], *, user: str = "U1") -> dict:
    return {
        "type": "view_submission",
        "user": {"id": user},
        "view": {
            "callback_id": view["callback_id"],
            "private_metadata": view["private_metadata"],
            "state": {
                "values": {
                    OPTIONS_SEVERAL_BLOCK: {
                        OPTIONS_SEVERAL_INPUT: {
                            "selected_options": [{"value": str(i)} for i in picks]
                        }
                    }
                }
            },
        },
    }


class TestChooseSeveral:
    @pytest.mark.asyncio
    async def test_the_menu_opens_a_checkbox_modal(self, world):
        orch, sent = world
        await interactions.dispatch(_open(["A", "B", "C"]))
        orch.slack.views_open.assert_called_once()
        view = orch.slack.views_open.call_args.args[1]
        assert view["callback_id"] == OPTIONS_SEVERAL_CALLBACK
        element = view["blocks"][0]["element"]
        assert element["type"] == "checkboxes" and len(element["options"]) == 3
        sent.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_modal_sends_every_chosen_option_together(self, world):
        orch, sent = world
        await interactions.dispatch(_open(["A", _LONG, "C"], msg_ts="m-several"))
        view = orch.slack.views_open.call_args.args[1]
        await interactions.dispatch(_submission(view, [2, 1]))
        await _settle()
        sent.assert_called_once()
        assert sent.call_args.args[3] == f"{_LONG}, C"
        line = orch.slack.update_message.call_args.kwargs["blocks"][-1]["elements"][0]["text"]
        assert "chose *" in line and "*C*" in line

    @pytest.mark.asyncio
    async def test_a_resubmitted_modal_sends_nothing_more(self, world):
        orch, sent = world
        await interactions.dispatch(_open(["A", "B"], msg_ts="m-resubmit"))
        view = orch.slack.views_open.call_args.args[1]
        await interactions.dispatch(_submission(view, [0]))
        await interactions.dispatch(_submission(view, [1]))
        await _settle()
        assert sent.call_count == 1

    @pytest.mark.asyncio
    async def test_an_answered_card_does_not_open_the_modal(self, world):
        orch, sent = world
        await interactions.dispatch(_click(["A", "B"], 0, msg_ts="m-answered"))
        await interactions.dispatch(_open(["A", "B"], msg_ts="m-answered"))
        orch.slack.views_open.assert_not_called()
        assert orch.slack.post_ephemeral.call_args.args[2] == "Already answered."

    @pytest.mark.asyncio
    async def test_a_submission_by_a_disallowed_user_is_refused(self, world, monkeypatch):
        orch, sent = world
        await interactions.dispatch(_open(["A", "B"], msg_ts="m-revoked"))
        view = orch.slack.views_open.call_args.args[1]
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: False)
        await interactions.dispatch(_submission(view, [0]))
        await _settle()
        sent.assert_not_called()
        orch.slack.update_message.assert_not_called()
        assert "not authorized" in orch.slack.post_ephemeral.call_args.args[2]

    @pytest.mark.asyncio
    async def test_a_submission_from_a_refused_channel_sends_nothing(self, world, monkeypatch):
        orch, sent = world
        await interactions.dispatch(_open(["A", "B"], msg_ts="m-refused"))
        view = orch.slack.views_open.call_args.args[1]
        asked = []

        def refuse(channel, **kwargs):
            asked.append((channel, kwargs.get("operation")))
            return "refused"

        monkeypatch.setattr(interactions, "channel_refusal", refuse)
        await interactions.dispatch(_submission(view, [0]))
        await _settle()
        sent.assert_not_called()
        orch.slack.update_message.assert_not_called()
        assert [op for _c, op in asked] == ["slack.interactive"]
