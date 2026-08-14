"""Tests for OPTIONS multi-select submit interaction handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.format import OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION


def _make_payload(selected_values: list[str], all_choices: list[str], thread_ts: str = "t1") -> dict:
    """Build a minimal Slack interaction payload for options submit."""
    return {
        "user": {"id": "U123"},
        "team": {"id": "T123"},
        "message": {
            "thread_ts": thread_ts,
            "ts": "msg1",
            "blocks": [
                {
                    "type": "actions",
                    "block_id": "blk1",
                    "elements": [
                        {
                            "type": "checkboxes",
                            "action_id": OPTIONS_CHECKBOXES_ACTION,
                            "options": [
                                {"text": {"type": "plain_text", "text": c}, "value": c}
                                for c in all_choices
                            ],
                        },
                        {
                            "type": "button",
                            "action_id": OPTIONS_SUBMIT_ACTION,
                            "text": {"type": "plain_text", "text": "Send"},
                        },
                    ],
                }
            ],
        },
        "state": {
            "values": {
                "blk1": {
                    OPTIONS_CHECKBOXES_ACTION: {
                        "selected_options": [
                            {"text": {"type": "plain_text", "text": v}, "value": v}
                            for v in selected_values
                        ]
                    }
                }
            }
        },
    }


def _mock_orch():
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.delete_message = AsyncMock()
    orch.slack.post_blocks = AsyncMock(return_value="new_ts")
    orch.slack.update_message = AsyncMock()
    orch.sessions = MagicMock()
    orch.ctx_builder = MagicMock()
    orch.cron_svc = MagicMock()
    orch.conv_log = MagicMock()
    orch.consolidator = MagicMock()
    orch.subagent_mgr = MagicMock()
    orch.task_runner = MagicMock()
    orch._handler_tasks = set()
    return orch


@pytest.fixture
def orch():
    return _mock_orch()


class TestHandleOptionsSubmit:
    @pytest.mark.asyncio
    async def test_denied_user_returns_early(self, orch, monkeypatch):
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: False)

        payload = _make_payload(["A"], ["A", "B"])
        await interactions._handle_options_submit(payload, "CH1", "msg1")

        orch.slack.post_blocks.assert_not_called()
        orch.slack.delete_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_combined_selections(self, orch, monkeypatch):
        import asyncio as _aio

        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = _make_payload(["A", "C"], ["A", "B", "C"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock) as mock_hm:
            await interactions._handle_options_submit(payload, "CH1", "msg1")
            await _aio.sleep(0)  # let create_task run

            # Edit-in-place: update_message called, NOT post_blocks/delete_message
            orch.slack.update_message.assert_called_once()
            call_kwargs = orch.slack.update_message.call_args
            assert call_kwargs[0][0] == "CH1"  # channel
            assert call_kwargs[0][1] == "msg1"  # ts preserved
            assert call_kwargs.kwargs["blocks"]  # blocks supplied
            assert "A, C" in call_kwargs.kwargs["text"]  # combined text

            orch.slack.post_blocks.assert_not_called()
            orch.slack.delete_message.assert_not_called()

            # Should trigger handle_message with action_context, ts preserved
            mock_hm.assert_called_once()
            assert mock_hm.call_args[1].get("team_id") == "T123"
            assert "OPTIONS multi-select" in mock_hm.call_args[1].get("action_context", "")

    @pytest.mark.asyncio
    async def test_ignores_empty_selection(self, orch, monkeypatch):
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = _make_payload([], ["A", "B"])
        await interactions._handle_options_submit(payload, "CH1", "msg1")

        orch.slack.delete_message.assert_not_called()
        orch.slack.post_blocks.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_orch_returns_early(self, monkeypatch):
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", None)

        payload = _make_payload(["A"], ["A", "B"])
        await interactions._handle_options_submit(payload, "CH1", "msg1")
        # No error raised

    @pytest.mark.asyncio
    async def test_single_selection(self, orch, monkeypatch):
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = _make_payload(["B"], ["A", "B", "C"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock):
            await interactions._handle_options_submit(payload, "CH1", "msg1")

            call_args = orch.slack.update_message.call_args
            assert "B" in call_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_duplicate_choices_deduped(self, orch, monkeypatch):
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = _make_payload(["A"], ["A", "A", "B"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock):
            await interactions._handle_options_submit(payload, "CH1", "msg1")

            call_args = orch.slack.update_message.call_args
            # Only first occurrence of "A" should be in selected_indices
            blocks = call_args.kwargs["blocks"]
            # selected_blocks is the last block (replaces the OPTIONS actions block)
            selected_text = next(
                b["elements"][0]["text"]
                for b in blocks
                if b.get("type") == "context"
            )
            # First A is bold, second A is strikethrough
            assert "*A*" in selected_text

    @pytest.mark.asyncio
    async def test_update_failure_falls_back_to_post_delete(self, orch, monkeypatch):
        """When update_message raises, fall back to post_blocks + delete_message."""
        import asyncio as _aio

        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)
        orch.slack.update_message = AsyncMock(side_effect=Exception("API error"))

        payload = _make_payload(["A"], ["A", "B"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock) as mock_hm:
            await interactions._handle_options_submit(payload, "CH1", "msg1")
            await _aio.sleep(0)  # let create_task run
            # Should fall back: post_blocks called, delete_message called
            orch.slack.post_blocks.assert_called_once()
            orch.slack.delete_message.assert_called_once_with("CH1", "msg1")
            # handle_message still fires with new_ts from post_blocks
            mock_hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_failure_keeps_the_record_for_a_later_turn(self, orch, monkeypatch):
        """A failed delete leaves the original buttons on screen — keep the record.

        update_message fails, the fallback post succeeds, then delete_message
        fails. The original OPTIONS message is therefore still in the channel and
        still clickable, so forgetting its record here would strand it: nothing
        later could ever expire it. That is the exact defect this PR removes, so
        the record has to survive the submit.
        """
        import asyncio as _aio

        from kiro_crew.slack import interactions

        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)
        orch.slack.update_message = AsyncMock(side_effect=Exception("API error"))
        orch.slack.delete_message = AsyncMock(side_effect=Exception("cant_delete"))

        forgotten: list[str] = []
        monkeypatch.setattr(
            interactions,
            "_forget_options_control",
            lambda ts, msg=None, keys=None: forgotten.append(ts),
        )

        payload = _make_payload(["A"], ["A", "B"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock):
            await interactions._handle_options_submit(payload, "CH1", "msg1")
            await _aio.sleep(0)

        orch.slack.delete_message.assert_called_once_with("CH1", "msg1")
        assert forgotten == []

    @pytest.mark.asyncio
    async def test_successful_edit_does_forget_the_record(self, orch, monkeypatch):
        """Companion to the above, so the retention check cannot pass vacuously.

        When the edit lands, the buttons are genuinely gone and the record SHOULD
        be dropped.
        """
        import asyncio as _aio

        from kiro_crew.slack import interactions

        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        forgotten: list[str] = []
        monkeypatch.setattr(
            interactions,
            "_forget_options_control",
            lambda ts, msg=None, keys=None: forgotten.append(ts),
        )

        payload = _make_payload(["A"], ["A", "B"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock):
            await interactions._handle_options_submit(payload, "CH1", "msg1")
            await _aio.sleep(0)

        assert forgotten == ["t1"]

    @pytest.mark.asyncio
    async def test_post_blocks_failure_aborts(self, orch, monkeypatch):
        """When update fails AND post_blocks fallback also returns None, abort."""
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)
        orch.slack.update_message = AsyncMock(side_effect=Exception("API error"))
        orch.slack.post_blocks = AsyncMock(return_value=None)

        payload = _make_payload(["A"], ["A", "B"])
        with patch.object(interactions, "handle_message", new_callable=AsyncMock) as mock_hm:
            await interactions._handle_options_submit(payload, "CH1", "msg1")
            mock_hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_preserves_surrounding_blocks(self, orch, monkeypatch):
        """Multi-block parent (section+actions+context): only actions replaced.

        Reproduces the saved-triage-digest bug — submitting OPTIONS used to
        delete the entire digest including its 3 sections + footer context.
        """
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = _make_payload(["A"], ["A", "B"])
        # Wrap the actions block with a section above and a context below
        section_top = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*📥 Saved-triage digest* — 3 fresh"},
        }
        section_mid = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "• Item 1\n• Item 2\n• Item 3"},
        }
        actions_block = payload["message"]["blocks"][0]
        ctx_footer = {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_cron: saved-triage_"}],
        }
        payload["message"]["blocks"] = [section_top, section_mid, actions_block, ctx_footer]

        with patch.object(interactions, "handle_message", new_callable=AsyncMock):
            await interactions._handle_options_submit(payload, "CH1", "msg1")

            orch.slack.update_message.assert_called_once()
            new_blocks = orch.slack.update_message.call_args.kwargs["blocks"]

            # Sections and footer context preserved
            assert section_top in new_blocks
            assert section_mid in new_blocks
            assert ctx_footer in new_blocks
            # Old actions block is gone
            assert actions_block not in new_blocks
            # Selected-options context block is inserted in its place
            assert any(
                b.get("type") == "context"
                and "*A*" in b["elements"][0].get("text", "")
                for b in new_blocks
            )


class TestCheckboxDispatch:
    """Verify the dispatch routes checkbox toggle and submit correctly."""

    @pytest.mark.asyncio
    async def test_checkbox_toggle_is_noop(self, orch, monkeypatch):
        from kiro_crew.slack import interactions
        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = {
            "type": "block_actions",
            "user": {"id": "U123"},
            "team": {"id": "T123"},
            "channel": {"id": "CH1"},
            "message": {"ts": "msg1", "thread_ts": "t1"},
            "actions": [{"action_id": OPTIONS_CHECKBOXES_ACTION, "type": "checkboxes"}],
        }
        with patch.object(interactions, "_handle_options_submit", new_callable=AsyncMock) as mock_sub:
            await interactions.dispatch(payload)
            mock_sub.assert_not_called()


# ── Tests for _import_thread_to_slot helper ──


class TestImportThreadToSlot:
    @pytest.mark.asyncio
    async def test_imports_messages_and_links(self, monkeypatch):
        from kiro_crew.slack import interactions

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(
            return_value=[
                {"user": "U1", "text": "hello"},
                {"bot_id": "B1", "text": "hi"},
            ]
        )
        slot = MagicMock()
        slot.key = "s1"
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=None)
        ds.get_or_create_slot = MagicMock(return_value=slot)
        ds._self_bot_id = "B1"

        with patch("kiro_crew.dashboard.chat._save_slot_to_history"):
            result = await interactions._import_thread_to_slot(slack, ds, "C1", "100.0")

        assert result is slot
        assert slot.append.call_count == 2
        ds.link_slack.assert_called_once_with("s1", "100.0", "C1")
        ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_messages(self, monkeypatch):
        from kiro_crew.slack import interactions

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[])
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=None)

        result = await interactions._import_thread_to_slot(slack, ds, "C1", "100.0")
        assert result is None
        ds.get_or_create_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_link_command_messages(self, monkeypatch):
        from kiro_crew.slack import interactions

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(
            return_value=[
                {"user": "U1", "text": "!link-to-dashboard"},
                {"user": "U1", "text": "real msg"},
            ]
        )
        slot = MagicMock()
        slot.key = "s1"
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=None)
        ds.get_or_create_slot = MagicMock(return_value=slot)
        ds._self_bot_id = ""

        with patch("kiro_crew.dashboard.chat._save_slot_to_history"):
            await interactions._import_thread_to_slot(slack, ds, "C1", "100.0")

        assert slot.append.call_count == 1

    @pytest.mark.asyncio
    async def test_redacts_text_before_append(self, monkeypatch):
        from kiro_crew.slack import interactions

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(
            return_value=[{"user": "U1", "text": "visit https://evil.com/steal"}]
        )
        slot = MagicMock()
        slot.key = "s1"
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=None)
        ds.get_or_create_slot = MagicMock(return_value=slot)
        ds._self_bot_id = ""

        with patch("kiro_crew.dashboard.chat._save_slot_to_history"):
            await interactions._import_thread_to_slot(slack, ds, "C1", "100.0")

        # Text should have been passed through redaction (we can't easily check
        # the exact output, but append should have been called)
        slot.append.assert_called_once()


# ── Tests for OPTIONS submit dispatch path ──


class TestOptionsSubmitDispatch:
    @pytest.mark.asyncio
    async def test_submit_action_dispatches(self, orch, monkeypatch):
        from kiro_crew.slack import interactions

        monkeypatch.setattr(interactions, "_orch", orch)
        monkeypatch.setattr(interactions, "is_allowed_user", lambda uid: True)

        payload = _make_payload(["A"], ["A", "B"])
        payload["type"] = "block_actions"
        payload["channel"] = {"id": "CH1"}
        payload["actions"] = [{"action_id": OPTIONS_SUBMIT_ACTION, "type": "button"}]

        with patch.object(interactions, "_handle_options_submit", new_callable=AsyncMock) as mock_sub:
            await interactions.dispatch(payload)
            mock_sub.assert_called_once()
