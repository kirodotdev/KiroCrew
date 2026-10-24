"""Status reactions, review drafts and the safe update paths of :mod:`kiro_crew.slack.handler`.

Pins the ``StatusReactionController`` phases, stall watchdog and finalize, the review-draft
store's expiry and cap, and the safe update paths (truncation, splitting, and the Slack,
wire and voice failures they swallow). The commands live in ``test_slack_handler_coverage.py``,
privacy modifiers and linked-thread routing in ``test_slack_handler_coverage_inbound.py``,
the approval prompts in ``test_slack_handler_coverage_approvals.py``, and the small pure
helpers in ``test_slack_handler_coverage_pure_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_handler_coverage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _clean_handler_state,
    slack_fixture,
)

from kiro_crew.slack import handler as h


# ──────────────────────────────────────────────────────────────────────
# review drafts
# ──────────────────────────────────────────────────────────────────────
class TestReviewDrafts:
    def test_missing_key(self):
        assert h._review_drafts_get("nope") == ("", "")
        assert h._review_drafts_pop("nope") == ("", "")

    def test_roundtrip(self):
        h._review_drafts_set("k", "draft body", "U1")
        assert h._review_drafts_get("k") == ("draft body", "U1")
        assert h._review_drafts_pop("k") == ("draft body", "U1")
        assert h._review_drafts_get("k") == ("", "")

    def test_expired_entry_is_evicted_on_get(self, monkeypatch):
        monkeypatch.setattr(h, "_REVIEW_DRAFT_TTL", -1)
        # flake-ok: pre-existing read of the real monotonic clock to stamp a draft, moved verbatim
        h._review_drafts["k"] = ("body", "U1", time.monotonic())
        assert h._review_drafts_get("k") == ("", "")
        assert "k" not in h._review_drafts

    def test_expired_entry_is_dropped_on_pop(self, monkeypatch):
        monkeypatch.setattr(h, "_REVIEW_DRAFT_TTL", -1)
        # flake-ok: pre-existing read of the real monotonic clock to stamp a draft, moved verbatim
        h._review_drafts["k"] = ("body", "U1", time.monotonic())
        assert h._review_drafts_pop("k") == ("", "")

    def test_set_evicts_expired_and_oldest(self, monkeypatch):
        monkeypatch.setattr(h, "_REVIEW_DRAFT_MAX", 2)
        # flake-ok: pre-existing read of the real monotonic clock to stamp a draft, moved verbatim
        now = time.monotonic()
        h._review_drafts["stale"] = ("x", "U1", now - h._REVIEW_DRAFT_TTL - 10)
        h._review_drafts["old"] = ("y", "U1", now - 100)
        h._review_drafts["new"] = ("z", "U1", now)
        h._review_drafts_set("fresh", "w", "U1")
        assert "stale" not in h._review_drafts
        assert "old" not in h._review_drafts
        assert "fresh" in h._review_drafts


# ──────────────────────────────────────────────────────────────────────
# status reactions
# ──────────────────────────────────────────────────────────────────────
class TestStatusReactionController:
    @pytest.mark.asyncio
    async def test_disabled_controller_is_inert(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1", enabled=False)
        ctrl.set_phase("queued")
        ctrl.on_progress()
        ctrl.resume_stall_watchdog()
        ctrl.finalize()
        await asyncio.sleep(0)
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_immediate_phase_then_finalize_swaps_emoji(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            assert [a for a in slack.actions if a[0] == "react"]
            slack.actions.clear()
            ctrl.set_phase("done")  # terminal phase routes to finalize()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            kinds = [a[0] for a in slack.actions]
            assert "unreact" in kinds and "react" in kinds
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_error_phase_uses_error_emoji(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.set_phase("error")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        emojis = [a[1]["emoji"] for a in slack.actions if a[0] == "react"]
        assert emojis == [h._PHASE_EMOJIS["error"]]

    @pytest.mark.asyncio
    async def test_finalize_is_idempotent(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.finalize()
        await asyncio.sleep(0)
        count = len(slack.actions)
        ctrl.finalize()
        await asyncio.sleep(0)
        assert len(slack.actions) == count

    @pytest.mark.asyncio
    async def test_pause_stops_the_watchdog(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl.on_progress()
            ctrl.pause_stall_watchdog()
            assert ctrl._stall_soft_handle is None
            ctrl.on_progress()  # paused — must stay disarmed
            assert ctrl._stall_soft_handle is None
            ctrl.resume_stall_watchdog()
            assert ctrl._stall_soft_handle is not None
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_stall_emoji_upgrades_then_clears(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            await ctrl._add_stall_emoji(h._STALL_EMOJI_SOFT)
            await ctrl._add_stall_emoji(h._STALL_EMOJI_HARD)
            assert ctrl._stall_emoji == h._STALL_EMOJI_HARD
            unreacted = [a[1]["emoji"] for a in slack.actions if a[0] == "unreact"]
            assert h._STALL_EMOJI_SOFT in unreacted
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_stall_emoji_not_added_after_finalize(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.finalize()
        await asyncio.sleep(0)
        slack.actions.clear()
        await ctrl._add_stall_emoji(h._STALL_EMOJI_SOFT)
        assert not slack.actions

    @pytest.mark.asyncio
    async def test_suppressed_phase_removes_without_adding(self, slack, monkeypatch):
        monkeypatch.setitem(h._PHASE_EMOJIS, "queued", None)
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl._current_emoji = "eyes"
            await ctrl._swap_emoji(None)
            kinds = [a[0] for a in slack.actions]
            assert kinds == ["unreact"]
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_swap_to_same_emoji_is_a_no_op(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl._current_emoji = "eyes"
            await ctrl._swap_emoji("eyes")
            assert not slack.actions
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_debounced_phase_applies_on_fire(self, slack):
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        try:
            ctrl.set_phase("coding")
            assert ctrl._pending_phase == "coding"
            await ctrl._apply_pending()
            assert ctrl._pending_phase is None
            assert [a for a in slack.actions if a[0] == "react"]
            # A second apply with nothing pending must not react again.
            slack.actions.clear()
            await ctrl._apply_pending()
            assert not slack.actions
        finally:
            ctrl.finalize()

    @pytest.mark.asyncio
    async def test_add_phase_reaction_honours_suppression(self, slack, monkeypatch):
        monkeypatch.setitem(h._PHASE_EMOJIS, "done", None)
        await h._add_phase_reaction(slack, "C1", "m1", "done")
        assert not slack.actions


# ──────────────────────────────────────────────────────────────────────
# safe update paths
# ──────────────────────────────────────────────────────────────────────
class TestSafeUpdates:
    @pytest.mark.asyncio
    async def test_update_truncates_over_limit(self, slack):
        from kiro_crew.slack.format import SLACK_MSG_LIMIT

        await h._safe_update(slack, "C1", "m1", "x" * (SLACK_MSG_LIMIT + 50))
        text = slack.actions[0][1]["text"]
        assert len(text) > SLACK_MSG_LIMIT  # limit + truncation notice
        assert text.startswith("x")

    @pytest.mark.asyncio
    async def test_update_failure_is_swallowed(self):
        broken = MagicMock()
        broken.update_message = AsyncMock(side_effect=RuntimeError("gone"))
        await h._safe_update(broken, "C1", "m1", "hi")
        broken.update_message.assert_awaited_once_with("C1", "m1", "hi")

    @pytest.mark.asyncio
    async def test_final_update_splits_long_text(self, slack):
        from kiro_crew.slack.format import SLACK_MSG_LIMIT

        await h._safe_final_update(slack, "C1", "m1", "y " * SLACK_MSG_LIMIT, "t1")
        assert [a for a in slack.actions if a[0] == "update"]
        assert [a for a in slack.actions if a[0] == "post"]

    @pytest.mark.asyncio
    async def test_final_update_survives_both_failures(self):
        broken = MagicMock()
        broken.update_message = AsyncMock(side_effect=RuntimeError("x"))
        broken.post_message = AsyncMock(side_effect=RuntimeError("y"))
        await h._safe_final_update(broken, "C1", "m1", "z " * 8000, "t1")
        broken.update_message.assert_awaited_once()
        # Every overflow part is still attempted after the primary update failed.
        assert broken.post_message.await_count >= 1
        assert all(c.args[2] == "t1" for c in broken.post_message.await_args_list)

    @pytest.mark.asyncio
    async def test_reject_orphaned_tool_swallows_failure(self):
        provider = MagicMock()
        provider.reject_tool = AsyncMock(side_effect=RuntimeError("dead"))
        await h._reject_orphaned_tool(provider, "r1")
        provider.reject_tool.assert_awaited_once_with("r1")

    @pytest.mark.asyncio
    async def test_safe_voice_reply_never_raises(self, slack, monkeypatch):
        boom = AsyncMock(side_effect=RuntimeError("no tts"))
        monkeypatch.setattr(h, "_voice_reply_fn", boom)
        await h._safe_voice_reply(slack, "C1", "t1", "hello")
        assert boom.await_args is not None
        assert boom.await_args.args == (slack, "C1", "t1", "hello")

    @pytest.mark.asyncio
    async def test_safe_voice_reply_forwards_settings(self, slack, monkeypatch):
        fake = AsyncMock()
        monkeypatch.setattr(h, "_voice_reply_fn", fake)
        await h._safe_voice_reply(slack, "C1", "t1", "hello", voice_id="Joanna")
        call = fake.await_args
        assert call is not None
        assert call.kwargs["voice_id"] == "Joanna"
