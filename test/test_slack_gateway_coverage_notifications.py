"""Coverage for ``kiro_crew.slack.gateway``: notification closures and module helpers.

``on_orphan_notify`` / ``on_orphan_dm`` handed to ``SubagentManager`` and
``on_notify`` handed to ``TaskRunner``; ``_channel_transport_permitted``'s
audit-failure and fail-closed branches; and the module-level helpers: the
subagent digest chunk-size and hold parse guards, the heartbeat render and the
bounded nudge-turn timeout.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_coverage_helpers import _awaited, _make_orchestrator, _mock_dashboard_state

from kiro_crew import subagent as _sa
from kiro_crew.slack import gateway as gw

# ═════════════════════════════════════════════════════════════════════════
# _init_subagents orphan-notification closures
# ═════════════════════════════════════════════════════════════════════════


def _capture_subagent_kwargs(orch: Any) -> dict:
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            inst = MagicMock()
            inst.start_reaper = MagicMock()
            mock_sm.return_value = inst
            orch._init_subagents()
            return mock_sm.call_args.kwargs


class TestOrphanNotifications:
    """``on_orphan_notify`` (slot injection) and ``on_orphan_dm`` (owner fallback)."""

    def _orch(self, ds: MagicMock | None) -> Any:
        orch = _make_orchestrator()
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.dashboard_state = ds
        return orch

    @pytest.mark.asyncio
    async def test_notify_injects_into_slot_and_queues_for_llm(self, monkeypatch):
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot._pending_subagent_failures = []
        ds.get_slot.return_value = slot
        orch = self._orch(ds)
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")

        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("dashboard:chat-1", "agent a1 was orphaned") is True

        slot.append.assert_called_once()
        assert slot.append.call_args.args[0] == "assistant"
        assert slot._pending_subagent_failures == ["agent a1 was orphaned"]
        ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_takes_the_notice_tombstone_under_the_queued_text(self, monkeypatch):
        """The card is the same with a debt as without; the debt is keyed on the text."""
        cards = []
        for delivery in (None, _sa.SubagentDelivery("a1", 0.0, 0.0, tombstone={"cause": "x"})):
            ds = _mock_dashboard_state()
            slot = MagicMock()
            slot._pending_subagent_failures = []
            ds.get_slot.return_value = slot
            orch = self._orch(ds)
            monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")
            notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
            verdict = await notify("dashboard:chat-1", "agent a1 was orphaned", {"k": 1}, delivery)
            cards.append(slot.append.call_args)
            if delivery is None:
                assert verdict is True
                slot.note_pending_subagent_delivery.assert_not_called()
            else:
                assert verdict == _sa.ORPHAN_NOTICE_OWED
                slot.note_pending_subagent_delivery.assert_called_once_with(
                    "agent a1 was orphaned", [delivery]
                )
        assert cards[0] == cards[1]

    @pytest.mark.asyncio
    async def test_notify_returns_false_when_no_tab_shows_the_parent(self, monkeypatch):
        orch = self._orch(_mock_dashboard_state())
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "")
        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("cron:nightly", "orphaned") is False

    @pytest.mark.asyncio
    async def test_notify_returns_false_without_dashboard_state(self, monkeypatch):
        orch = self._orch(None)
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")
        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("dashboard:chat-1", "orphaned") is False

    @pytest.mark.asyncio
    async def test_notify_returns_false_when_slot_is_gone(self, monkeypatch):
        ds = _mock_dashboard_state()
        ds.get_slot.return_value = None
        orch = self._orch(ds)
        monkeypatch.setattr(gw, "dashboard_slot_key", lambda _k: "chat-1")
        notify = _capture_subagent_kwargs(orch)["on_orphan_notify"]
        assert await notify("dashboard:chat-1", "orphaned") is False

    @pytest.mark.asyncio
    async def test_dm_uses_bell_and_slack_dm(self):
        ds = _mock_dashboard_state()
        orch = self._orch(ds)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned by restart") is True

        ds.notify.assert_called_once()
        orch.slack.post_message.assert_awaited_once_with("D1", "a1 orphaned by restart")

    @pytest.mark.asyncio
    async def test_dm_bell_failure_still_reports_slack_delivery(self):
        ds = _mock_dashboard_state()
        ds.notify.side_effect = RuntimeError("bell broken")
        orch = self._orch(ds)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is True

    @pytest.mark.asyncio
    async def test_dm_slack_failure_still_reports_bell_delivery(self):
        ds = _mock_dashboard_state()
        orch = self._orch(ds)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is True

    @pytest.mark.asyncio
    async def test_dm_returns_false_when_nothing_can_deliver(self):
        orch = self._orch(None)
        orch.slack = None
        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is False

    @pytest.mark.asyncio
    async def test_dm_skips_slack_when_open_dm_returns_no_channel(self):
        orch = self._orch(None)
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value=None)
        orch.slack.post_message = AsyncMock()

        dm = _capture_subagent_kwargs(orch)["on_orphan_dm"]
        assert await dm("a1 orphaned") is False
        orch.slack.post_message.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# _init_task_runner's _task_notify closure
# ═════════════════════════════════════════════════════════════════════════


class TestTaskNotify:
    """``on_notify`` mirrors bell + refresh, and DMs only approval/denial titles."""

    def _capture(self, orch: Any):
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        with patch("kiro_crew.slack.gateway.TaskRunner") as mock_tr:
            mock_tr.return_value = MagicMock()
            orch._init_task_runner()
            return mock_tr.call_args.kwargs["on_notify"]

    @pytest.mark.asyncio
    async def test_notifies_dashboard_with_task_meta(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        notify = self._capture(orch)

        await notify("Step 2 complete", "all good", "task-7")

        surface, title, body = ds.notify.call_args.args
        assert (surface, title, body) == ("taskrunner", "Step 2 complete", "all good")
        assert ds.notify.call_args.kwargs["meta"] == {"task_id": "task-7"}
        ds.push_refresh.assert_called_once_with("taskrunner")

    @pytest.mark.asyncio
    async def test_no_meta_without_task_id(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        notify = self._capture(orch)

        await notify("Step 2 complete", "all good")
        assert ds.notify.call_args.kwargs["meta"] is None

    @pytest.mark.asyncio
    async def test_approval_title_also_dms_the_owner(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Task 3 requires approval", "run the deploy?")

        channel, text = _awaited(orch.slack.post_message).args
        assert channel == "D1"
        assert text == "*Task 3 requires approval*\nrun the deploy?"

    @pytest.mark.asyncio
    async def test_denied_title_also_dms_the_owner(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Task 3 denied", "nope")
        orch.slack.post_message.assert_awaited_once_with("D1", "*Task 3 denied*\nnope")

    @pytest.mark.asyncio
    async def test_ordinary_title_does_not_dm(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D1")
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Investigating gateway error", "looking")
        assert ds.notify.call_args.args == ("taskrunner", "Investigating gateway error", "looking")
        orch.slack.open_dm.assert_not_called()
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dm_failure_is_swallowed(self):
        orch = _make_orchestrator()
        ds = _mock_dashboard_state()
        orch.dashboard_state = ds
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("slack down"))
        orch.slack.post_message = AsyncMock()
        notify = self._capture(orch)

        await notify("Task 3 requires approval", "run it?")
        orch.slack.open_dm.assert_awaited_once()
        orch.slack.post_message.assert_not_awaited()
        assert ds.notify.call_args.args == ("taskrunner", "Task 3 requires approval", "run it?")


# ═════════════════════════════════════════════════════════════════════════
# _channel_transport_permitted — audit-failure and fail-closed branches
# ═════════════════════════════════════════════════════════════════════════


def _decision(*, permitted: bool, layer: str = "default", rule: str = "rule2-intersect"):
    return SimpleNamespace(permitted=permitted, layer=layer, rule=rule, reason="")


class TestChannelTransportPermittedAuditPaths:
    """The connect-time ``channels`` gate's audit disposition and error posture."""

    def test_deny_audit_failure_still_denies(self):
        """A best-effort deny audit that cannot be written must not mask the deny."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with (
            patch.object(gw, "governance_permits", return_value=_decision(permitted=False)),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("telegram") is False

    def test_ungoverned_allow_survives_an_unwritable_audit(self):
        """No policy governs ``channels`` → SEL disk health must not block startup."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer="default")
            ),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("telegram") is True

    def test_governed_allow_denies_when_its_audit_cannot_be_written(self):
        """audit-or-deny: a policy-governed transport never connects unaudited."""
        audit = MagicMock()
        audit.log_governance_decision.side_effect = RuntimeError("sel unwritable")
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer="policy")
            ),
            patch.object(gw, "sel", return_value=audit),
            patch.object(gw, "audit_governance_degraded") as degraded,
        ):
            assert gw._channel_transport_permitted("telegram") is False
        assert degraded.call_args.kwargs["failed_closed"] is True

    @pytest.mark.parametrize(
        ("layer", "critical"),
        [
            pytest.param("profile", True, id="test_governed_allow_is_audited_critically"),
            pytest.param("", False, id="test_ungoverned_allow_is_audited_best_effort"),
        ],
    )
    def test_allow_audit_disposition(self, layer: str, critical: bool):
        audit = MagicMock()
        with (
            patch.object(
                gw, "governance_permits", return_value=_decision(permitted=True, layer=layer)
            ),
            patch.object(gw, "sel", return_value=audit),
        ):
            assert gw._channel_transport_permitted("webex") is True
        assert audit.log_governance_decision.call_args.kwargs["critical"] is critical

    def test_evaluation_error_fails_closed(self):
        with (
            patch.object(gw, "governance_permits", side_effect=RuntimeError("resolver broke")),
            patch.object(gw, "audit_governance_degraded") as degraded,
        ):
            assert gw._channel_transport_permitted("wecom") is False
        degraded.assert_called_once()

    def test_degrade_audit_failure_does_not_mask_the_deny(self):
        with (
            patch.object(gw, "governance_permits", side_effect=RuntimeError("resolver broke")),
            patch.object(
                gw, "audit_governance_degraded", side_effect=RuntimeError("audit import failed")
            ),
        ):
            assert gw._channel_transport_permitted("wecom") is False

    def test_composition_error_propagates(self):
        """A broken CPP composition must abort, not silently deny."""
        with patch.object(
            gw, "governance_permits", side_effect=gw.PlatformCompositionError("broken")
        ):
            with pytest.raises(gw.PlatformCompositionError):
                gw._channel_transport_permitted("wecom")


# ═════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═════════════════════════════════════════════════════════════════════════


class TestDigestChunkSize:
    """``KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE`` parse guard: never crash import."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", raising=False)
        assert gw._digest_chunk_size() == 10

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("25", 25, id="test_explicit_value_is_honoured"),
            pytest.param("not-a-number", 10, id="test_malformed_value_falls_back_to_default"),
        ],
    )
    def test_set_value_is_parsed(self, monkeypatch, raw: str, expected: int):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", raw)
        assert gw._digest_chunk_size() == expected

    def test_value_is_clamped_to_a_sane_range(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "0")
        assert gw._digest_chunk_size() == 1
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "99999")
        assert gw._digest_chunk_size() == 1000


class TestDigestHoldSecs:
    """``KIROCREW_SUBAGENT_DIGEST_HOLD_SECS`` parse guard: the
    latency half of the digest split must never crash import, and 0 is the
    documented opt-out back to count-trigger-only delivery."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", raising=False)
        assert _sa._digest_hold_secs() == 120.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("45.5", 45.5, id="test_explicit_value_is_honoured"),
            pytest.param("not-a-number", 120.0, id="test_malformed_value_falls_back_to_default"),
        ],
    )
    def test_set_value_is_parsed(self, monkeypatch, raw: str, expected: float):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", raw)
        assert _sa._digest_hold_secs() == expected

    def test_zero_and_negative_disable_the_deadline(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "0")
        assert _sa._digest_hold_secs() == 0.0
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "-30")
        assert _sa._digest_hold_secs() == 0.0

    def test_clamped_to_the_per_agent_hard_ceiling(self, monkeypatch):
        """A deadline beyond the reap window is meaningless — the member is
        already dead by then and the wave closes on its own."""
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "999999")
        assert _sa._digest_hold_secs() == float(_sa._TIMEOUT_SECS)

    def test_nan_is_malformed_input_not_a_deadline(self, monkeypatch):
        """GPT 5.6 BLOCKING: NaN parses but loses every comparison, so it is
        neither disabled (``nan <= 0`` False) nor bounded (``min(nan, x)`` is
        nan). It would make the sweep force a flush on the FIRST hold, and
        ``int(nan)`` then raises during digest composition — after the hold
        clocks were cleared and ``flushed`` advanced — permanently withholding
        the results the deadline exists to release."""
        for spelling in ("nan", "NaN", "-nan"):
            monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", spelling)
            got = _sa._digest_hold_secs()
            assert not math.isnan(got), f"{spelling!r} leaked NaN into the deadline"
            assert got == 120.0
            # The two downstream operations a NaN would have broken:
            # the sweep's grace-window comparison, and digest composition's int().
            assert (5.0 < got) is True, "a fresh hold must stay inside the window"
            assert (99999.0 < got) is False, "an aged hold must leave the window"
            assert int(got) == 120

    def test_infinity_is_clamped_not_leaked(self, monkeypatch):
        """+inf clamps to the ceiling; -inf is a valid opt-out. Unlike NaN,
        both order correctly, so neither needs rejecting."""
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "inf")
        assert _sa._digest_hold_secs() == float(_sa._TIMEOUT_SECS)
        monkeypatch.setenv("KIROCREW_SUBAGENT_DIGEST_HOLD_SECS", "-inf")
        assert _sa._digest_hold_secs() == 0.0


class TestHeartbeatSlackParts:
    """The shared heartbeat render: captioned, split, and redacted after transform."""

    def test_caption_is_prepended(self):
        parts = gw._heartbeat_slack_parts("Nightly sweep", "all clear")
        assert parts
        assert parts[0].startswith("💓 *Nightly sweep*")
        assert "all clear" in parts[0]

    def test_long_result_is_split_instead_of_dropped(self):
        parts = gw._heartbeat_slack_parts("Big", "x" * 20000)
        assert len(parts) > 1
        assert all(len(p) <= 40000 for p in parts)


class TestNudgeTurnTimeout:
    """Unattended turns must stay bounded — no human is present to cancel them."""

    def test_timeout_is_positive_and_finite(self):
        assert 0 < gw._NUDGE_TURN_TIMEOUT < float("inf")

    def test_background_approval_sources_include_autonudge(self):
        assert "autonudge" in gw._BACKGROUND_APPROVAL_SOURCES
        assert "cron" in gw._BACKGROUND_APPROVAL_SOURCES


def test_event_loop_is_not_required_for_module_helpers():
    """Quick check: the pure helpers above run with no running loop (import-time safe)."""
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
    assert gw._digest_chunk_size() >= 1
