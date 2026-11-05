"""Decision buttons in ``kiro_crew.slack.interactions``, read back from real stores.

The transport-path tool-approval press (``mc_tool_*``) resolves a real reserved
``SlackApprovalDecider`` prompt and writes a real security event log; the cron and
subagent acknowledge buttons write a real cron store and a real dashboard
notification log. The fixtures and builders come from
``slack_interactions_coverage_helpers``, shared with ``test_slack_interactions_coverage.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from slack_interactions_coverage_helpers import _mock_aiohttp_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _restore_module_globals  # noqa: F401
from slack_interactions_coverage_helpers import ix_logs_fixture  # noqa: F401
from slack_interactions_coverage_helpers import orch_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _action_payload, _ix_messages, _payload

from kiro_crew import sel as sel_mod
from kiro_crew.messaging.session_trust import clear_trusted_sessions, is_session_trusted
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.renderer import SlackApprovalDecider
from kiro_crew.testing.wait import await_until


@pytest.fixture(name="audit")
def audit_fixture(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> sel_mod.SecurityEventLog:
    """A real, synchronous security event log under tmp_path, read back by the tests."""
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_initialized", False)
    (tmp_path / "sel").mkdir()
    log = sel_mod.SecurityEventLog(base_dir=tmp_path / "sel", sync=True)
    # Leave the process singleton unset so no other caller is handed this log.
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_initialized", False)
    monkeypatch.setattr(ix, "sel", lambda: log)
    return log


def _audit_ops(audit: sel_mod.SecurityEventLog, operation: str) -> list[tuple[str, str]]:
    """``(outcome, resources)`` of each stored row for *operation*, oldest first."""
    rows = [e for e in audit.recent() if e["operation"] == operation]
    return [(e["outcome"], e.get("resources", "")) for e in reversed(rows)]


# ---------------------------------------------------------------------------
# Transport-path tool approval
# ---------------------------------------------------------------------------


def _prompt(session_key: str, rid: str) -> tuple[SlackApprovalDecider, str, asyncio.Task]:
    """A real prompt: the reserved window, its nonce, and the wait a press decides."""
    dec = SlackApprovalDecider(session_key)
    nonce = dec.reserve(rid)
    waiter = asyncio.ensure_future(dec(SimpleNamespace(request_id=rid)))
    return dec, nonce, waiter


class TestDispatchTransportToolApproval:
    """The ``mc_tool_*`` transport-path approval branch."""

    @pytest.fixture(autouse=True)
    def _decider(self, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
        # The real registry, recorded: a press decides a real reserved prompt.
        _floor_monkeypatch.setattr(SlackApprovalDecider, "_REGISTRY", {})
        _floor_monkeypatch.setattr(SlackApprovalDecider, "_NONCES", {})
        _floor_monkeypatch.setattr(SlackApprovalDecider, "_AWAITED", set())
        dec = MagicMock(wraps=SlackApprovalDecider)
        _floor_monkeypatch.setattr(ix, "SlackApprovalDecider", dec)
        yield dec
        clear_trusted_sessions()

    @pytest.mark.asyncio
    async def test_approve_resolves_and_labels(
        self, orch: MagicMock, _decider: MagicMock, audit: Any
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        _, nonce, waiter = _prompt("sess1", "rid1")
        await ix.dispatch(
            _action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid1", f"sess1:rid1|{nonce}")
        )
        _decider.resolve_global.assert_called_once_with("sess1:rid1", True, nonce=nonce)
        assert orch.slack.update_message.await_args.kwargs["text"] == "✅ Approved"
        assert await await_until(waiter) is True
        assert _audit_ops(audit, "slack.transport_tool_approval") == [
            ("approved", "approval_key=sess1:rid1")
        ]

    @pytest.mark.asyncio
    async def test_deny_resolves_false(
        self, orch: MagicMock, _decider: MagicMock, audit: Any
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_DENY_ACTION_PREFIX

        _, nonce, waiter = _prompt("sess1", "rid2")
        await ix.dispatch(_action_payload(f"{TOOL_DENY_ACTION_PREFIX}rid2", f"sess1:rid2|{nonce}"))
        _decider.resolve_global.assert_called_once_with("sess1:rid2", False, nonce=nonce)
        assert orch.slack.update_message.await_args.kwargs["text"] == "🚫 Denied"
        assert await await_until(waiter) is False
        assert _audit_ops(audit, "slack.transport_tool_approval") == [
            ("denied", "approval_key=sess1:rid2")
        ]

    @pytest.mark.asyncio
    async def test_trust_grants_session_trust_before_resolving(
        self, orch: MagicMock, _decider: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_TRUST_ACTION_PREFIX

        trust = MagicMock(wraps=ix.add_trusted_session)
        monkeypatch.setattr(ix, "add_trusted_session", trust)
        _, nonce, waiter = _prompt("sess1", "rid3")
        await ix.dispatch(_action_payload(f"{TOOL_TRUST_ACTION_PREFIX}rid3", f"sess1:rid3|{nonce}"))
        trust.assert_called_once()
        assert trust.call_args.args[0] == "sess1"
        # Trust is looked up under the SAME nonce the press carried, so a stale
        # button cannot widen the session before the resolve behind it refuses.
        _decider.session_for.assert_called_once_with("sess1:rid3", nonce=nonce)
        _decider.resolve_global.assert_called_once_with("sess1:rid3", True, nonce=nonce)
        assert is_session_trusted("sess1")
        assert await await_until(waiter) is True
        assert orch.slack.update_message.await_args.kwargs["text"].startswith("🔓 Trusted")

    @pytest.mark.asyncio
    async def test_expired_approval_reports_expiry(
        self, orch: MagicMock, _decider: MagicMock, audit: Any
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        # No prompt is open at this key: the window already closed.
        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid4", "sess1:rid4"))
        assert "expired" in orch.slack.update_message.await_args.kwargs["text"]
        assert _audit_ops(audit, "slack.transport_tool_approval") == [
            ("expired", "approval_key=sess1:rid4")
        ]

    @pytest.mark.asyncio
    async def test_approval_key_falls_back_to_action_id_suffix(
        self, orch: MagicMock, _decider: MagicMock
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        dec = SlackApprovalDecider()
        nonce = dec.reserve("rid5")
        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid5", ""))
        assert _decider.resolve_global.call_args.args[0] == "rid5"
        # Nothing to split, so no nonce is claimed — the decider refuses it.
        assert _decider.resolve_global.call_args.kwargs["nonce"] == ""
        assert "expired" in orch.slack.update_message.await_args.kwargs["text"]
        # The refused press decided nothing: the prompt is still open to its own buttons.
        assert SlackApprovalDecider.resolve_global("rid5", False, nonce=nonce) is True

    @pytest.mark.asyncio
    async def test_governance_deny_resolves_approval_as_denied(
        self,
        orch: MagicMock,
        _decider: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        audit: Any,
    ) -> None:
        """A channels-deny must refuse the tool, not strand the pending future."""
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=False))
        _, nonce, waiter = _prompt("sess1", "rid6")
        await ix.dispatch(
            _action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid6", f"sess1:rid6|{nonce}")
        )
        _decider.resolve_global.assert_called_once_with("sess1:rid6", False, nonce=nonce)
        orch.slack.update_message.assert_not_awaited()
        assert await await_until(waiter) is False
        assert _audit_ops(audit, "slack.transport_tool_approval") == [
            ("denied", "approval_key=sess1:rid6 channels_policy")
        ]

    @pytest.mark.asyncio
    async def test_update_message_failure_is_swallowed(
        self, orch: MagicMock, _decider: MagicMock, ix_logs: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid7", "sess1:rid7"))
        _decider.resolve_global.assert_called_once_with("sess1:rid7", True, nonce="")
        orch.slack.update_message.assert_awaited_once()
        assert "Failed to update transport approval message" in _ix_messages(ix_logs)


# ---------------------------------------------------------------------------
# Cron / subagent acknowledge
# ---------------------------------------------------------------------------


def _dashboard_with_notes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, notes: list[dict]) -> Any:
    """A real ``DashboardState`` holding *notes*, its writes landing under tmp_path."""
    from kiro_crew.dashboard.state import DashboardState

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    state.broadcast_ws = MagicMock()
    state._notification_log = notes
    state.ack_notification = AsyncMock(wraps=state.ack_notification)
    return state


def _acked_by_ts(tmp_path: Any) -> dict[str, bool]:
    """The ``acked`` flag of each notification as persisted to disk."""
    text = (tmp_path / "notifications.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return {row["ts"]: row["acked"] for row in rows}


class TestAckHandlers:
    @pytest.mark.asyncio
    async def test_cron_ack_calls_service(self, orch: MagicMock, tmp_path: Any) -> None:
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path / "cron")
        job = await svc.add_job_async(name="j", message="m", every_secs=60)
        orch.cron_svc.ack_job_async = AsyncMock(wraps=svc.ack_job_async)
        orch.dashboard_state = None
        payload = _payload(message={"ts": "m1", "blocks": [], "text": "hello"})
        await ix._handle_cron_ack(payload, {"value": job.id}, "C1", "m1")
        orch.cron_svc.ack_job_async.assert_awaited_once_with(job.id, "hello")
        assert orch.slack.update_message.await_args.kwargs["text"] == "✅ Acknowledged"
        # The acknowledgement is stored on the job a fresh reader loads.
        assert CronService(base_dir=tmp_path / "cron").get_job(job.id).acked_items == ["hello"]

    @pytest.mark.asyncio
    async def test_cron_ack_without_job_id_is_a_noop(self, orch: MagicMock) -> None:
        orch.cron_svc.ack_job_async = AsyncMock()
        await ix._handle_cron_ack(_payload(), {}, "C1", "m1")
        orch.cron_svc.ack_job_async.assert_not_awaited()
        # Without a job id the button is not acked either.
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_ack_survives_busy_store(
        self, orch: MagicMock, ix_logs: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.cron import CronStoreBusy

        orch.cron_svc.ack_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
        orch.dashboard_state = None
        # Ack is best-effort bookkeeping — a contended store must not raise.
        await ix._handle_cron_ack(_payload(), {"value": "job1"}, "C1", "m1")
        orch.cron_svc.ack_job_async.assert_awaited_once_with("job1", "")
        assert "cron ack skipped: CronStoreBusy (job job1)" in _ix_messages(ix_logs)
        assert orch.slack.update_message.await_args.kwargs["text"] == "✅ Acknowledged"

    @pytest.mark.asyncio
    async def test_cron_ack_marks_matching_notification(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        orch.cron_svc.ack_job_async = AsyncMock()
        ds = _dashboard_with_notes(
            monkeypatch,
            tmp_path,
            [
                {"job_id": "job1", "acked": False, "ts": "n1"},
                {"job_id": "job1", "acked": True, "ts": "n2"},
                {"job_id": "other", "acked": False, "ts": "n3"},
            ],
        )
        orch.dashboard_state = ds
        await ix._handle_cron_ack(_payload(), {"value": "job1"}, "C1", "m1")
        ds.ack_notification.assert_awaited_once_with("n1")
        # The real ack broadcasts once itself, and the handler broadcasts once more.
        assert ds.broadcast_ws.call_args_list == [
            call("notification_ack", {"ts": "n1"}),
            call("notification_ack", {"ts": "n1"}),
        ]
        assert _acked_by_ts(tmp_path) == {"n1": True, "n2": True, "n3": False}

    @pytest.mark.asyncio
    async def test_subagent_ack_marks_matching_notification(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        ds = _dashboard_with_notes(
            monkeypatch,
            tmp_path,
            [
                {"kind": "subagent", "title": "agent abc done", "acked": False, "ts": "n1"},
                {"kind": "cron", "title": "abc", "acked": False, "ts": "n2"},
            ],
        )
        orch.dashboard_state = ds
        await ix._handle_subagent_ack(_payload(), {"value": "abc"}, "C1", "m1")
        ds.ack_notification.assert_awaited_once_with("n1")
        assert _acked_by_ts(tmp_path) == {"n1": True, "n2": False}

    @pytest.mark.asyncio
    async def test_subagent_ack_still_acks_button_without_id(self, orch: MagicMock) -> None:
        orch.dashboard_state = None
        await ix._handle_subagent_ack(_payload(), {}, "C1", "m1")
        # The visual ack happens before the early return.
        orch.slack.update_message.assert_awaited_once()
        assert orch.slack.update_message.await_args.kwargs["text"] == "✅ Acknowledged"
