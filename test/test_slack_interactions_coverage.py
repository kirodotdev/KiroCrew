"""Coverage tests for ``kiro_crew.slack.interactions``.

Focuses on the interactive-payload surfaces the existing suite leaves
untested: the view-submission registry, ``ack_button``, ``dispatch``
action-id routing (including malformed / unknown ids and the
permission-rejection paths), the channels-modal handlers, the voice-config
modal, the allowlist / tracking approve-deny buttons, the select-menu
config writers, and the session resume-choice flow.

Everything runs in-process: no network, no subprocesses, no real Slack.
``aiohttp.ClientSession`` is stubbed for the whole module, and config
writes land in the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import ConfigReadError, config_path
from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_aiohttp():
    """Stub ``aiohttp.ClientSession`` so ``response_url`` posts never hit the network.

    Handlers post to Slack's ``response_url`` via ``async with
    aiohttp.ClientSession()``; without this the tests would attempt a real
    outbound connection.
    """
    sess = AsyncMock()
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=None)
    resp = MagicMock()
    resp.status = 200
    sess.post = AsyncMock(return_value=resp)
    with patch("aiohttp.ClientSession", return_value=sess):
        yield sess


@pytest.fixture(autouse=True)
def _restore_module_globals():
    """Snapshot/restore the process-global state these handlers mutate.

    ``VIEW_REGISTRY``, ``_resume_locks`` and ``handler._vc`` all outlive a
    single test, so without this a test that registers a handler or flips a
    voice setting would leak into whatever runs next (order dependence).
    """
    saved_registry = dict(ix.VIEW_REGISTRY)
    saved_locks = dict(ix._resume_locks)
    saved_vc = {
        f: getattr(sh._vc, f)
        for f in (
            "global_enabled",
            "auto_speak",
            "default_voice",
            "default_engine",
            "default_rate",
            "default_pitch",
            "aws_profile",
            "region",
        )
    }
    saved_allowed = sh._allowed_users
    saved_tracking = sh._tracking_channels
    yield
    ix.VIEW_REGISTRY.clear()
    ix.VIEW_REGISTRY.update(saved_registry)
    ix._resume_locks.clear()
    ix._resume_locks.update(saved_locks)
    for field, value in saved_vc.items():
        setattr(sh._vc, field, value)
    sh.set_allowed_users(saved_allowed)
    sh.set_tracking_channels(saved_tracking)


def _make_orch() -> MagicMock:
    """A minimal orchestrator double with async Slack ops."""
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock(return_value="ts1")
    orch.slack.post_blocks = AsyncMock(return_value="ts1")
    orch.slack.post_ephemeral = AsyncMock()
    orch.slack.update_message = AsyncMock()
    orch.slack.delete_message = AsyncMock()
    orch.slack.open_dm = AsyncMock(return_value="D1")
    orch.slack.views_update = AsyncMock()
    orch.slack.set_thread_status = AsyncMock()
    orch.slack.is_dm = AsyncMock(return_value=True)
    orch._allowed_users = set()
    orch._tracking_channels = set()
    orch.dashboard_state = None
    return orch


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Bind an orchestrator double and allow the caller by default."""
    o = _make_orch()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
    _set_owner(monkeypatch, True)
    monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=True))
    return o


def _set_owner(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Patch BOTH ``is_owner`` bindings.

    ``interactions`` imported ``is_owner`` at module scope, but several
    handlers re-import it from ``handler`` at call time, so a single patch
    only covers half the call sites.
    """
    monkeypatch.setattr(ix, "is_owner", lambda uid: value)
    monkeypatch.setattr(sh, "is_owner", lambda uid: value)


def _payload(**over: Any) -> dict:
    base: dict[str, Any] = {
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "m1", "blocks": []},
        "response_url": "",
    }
    base.update(over)
    return base


def _action_payload(action_id: str, value: str = "", **over: Any) -> dict:
    p = _payload(**over)
    p["actions"] = [{"action_id": action_id, "value": value}]
    return p


def _read_config() -> dict:
    raw = config_path().read_text(encoding="utf-8")
    parsed: dict = json.loads(raw)
    return parsed


# ---------------------------------------------------------------------------
# View submission / closed registry
# ---------------------------------------------------------------------------


class TestViewRegistry:
    @pytest.mark.asyncio
    async def test_submission_dispatches_to_registered_handler(self) -> None:
        seen: list[dict] = []

        async def handler(payload: dict) -> None:
            seen.append(payload)

        ix.register_view_handler("cb_ok", handler)
        payload = {"view": {"callback_id": "cb_ok"}}
        await ix.handle_view_submission(payload)
        assert seen == [payload]

    @pytest.mark.asyncio
    async def test_submission_unknown_callback_id_is_ignored(self) -> None:
        # No handler, no forward-callback match -> logged and dropped, no raise.
        await ix.handle_view_submission({"view": {"callback_id": "nope"}})

    @pytest.mark.asyncio
    async def test_submission_missing_view_key_is_ignored(self) -> None:
        await ix.handle_view_submission({})

    @pytest.mark.asyncio
    async def test_submission_swallows_handler_exception(self) -> None:
        async def boom(payload: dict) -> None:
            raise RuntimeError("handler blew up")

        ix.register_view_handler("cb_boom", boom)
        # Must not propagate: a raising modal handler cannot break the socket loop.
        await ix.handle_view_submission({"view": {"callback_id": "cb_boom"}})

    @pytest.mark.asyncio
    async def test_submission_falls_back_to_forward_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live-reconfigured forward callback resolves even when unregistered."""
        monkeypatch.setattr(ix, "_get_forward_callback", lambda: "fwd_live")
        called = AsyncMock()
        monkeypatch.setattr(ix, "_handle_shortcut_submission", called)
        ix.VIEW_REGISTRY.pop("fwd_live", None)
        await ix.handle_view_submission({"view": {"callback_id": "fwd_live"}})
        called.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_uses_closed_suffix_handler(self) -> None:
        seen: list[dict] = []

        async def handler(payload: dict) -> None:
            seen.append(payload)

        ix.register_view_handler("cb_x_closed", handler)
        await ix.handle_view_closed({"view": {"callback_id": "cb_x"}})
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_closed_without_handler_is_ignored(self) -> None:
        await ix.handle_view_closed({"view": {"callback_id": "unregistered"}})

    @pytest.mark.asyncio
    async def test_closed_swallows_handler_exception(self) -> None:
        async def boom(payload: dict) -> None:
            raise RuntimeError("nope")

        ix.register_view_handler("cb_y_closed", boom)
        await ix.handle_view_closed({"view": {"callback_id": "cb_y"}})


# ---------------------------------------------------------------------------
# Config modal submission
# ---------------------------------------------------------------------------


class TestConfigSubmission:
    def _view(self, channels: list[str]) -> dict:
        return {
            "user": {"id": "U1"},
            "view": {
                "state": {
                    "values": {
                        "channels_block": {"mc_config_channels": {"selected_channels": channels}}
                    }
                }
            },
        }

    @pytest.mark.asyncio
    async def test_non_owner_rejected_without_writing_config(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_config_submission(self._view(["C9"]))
        assert not config_path().exists()
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_persists_channels_and_updates_runtime(self, orch: MagicMock) -> None:
        await ix._handle_config_submission(self._view(["Cb", "Ca"]))
        assert _read_config()["slack"]["tracking_channels"] == [
            {"channel_id": "Ca"},
            {"channel_id": "Cb"},
        ]
        assert orch._tracking_channels == {"Ca", "Cb"}

    @pytest.mark.asyncio
    async def test_unreadable_config_fails_closed(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any) -> dict:
            raise ConfigReadError("corrupt")

        monkeypatch.setattr(ix, "read_config_for_update", boom)
        await ix._handle_config_submission(self._view(["C9"]))
        # Runtime state must NOT move ahead of an unwritable disk.
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_write_failure_leaves_runtime_untouched(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any, _data: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ix, "write_config_atomically", boom)
        await ix._handle_config_submission(self._view(["C9"]))
        assert orch._tracking_channels == set()


# ---------------------------------------------------------------------------
# ack_button
# ---------------------------------------------------------------------------


class TestAckButton:
    @pytest.mark.asyncio
    async def test_response_url_success_skips_chat_update(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        payload = _payload(response_url="https://hooks.slack.com/x")
        await ix.ack_button(payload, "C1", "m1")
        _mock_aiohttp.post.assert_awaited_once()
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_chat_update_on_non_200(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        bad = MagicMock()
        bad.status = 500
        _mock_aiohttp.post = AsyncMock(return_value=bad)
        await ix.ack_button(_payload(response_url="https://hooks.slack.com/x"), "C1", "m1")
        orch.slack.update_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_when_response_url_raises(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        _mock_aiohttp.post = AsyncMock(side_effect=RuntimeError("boom"))
        await ix.ack_button(_payload(response_url="https://hooks.slack.com/x"), "C1", "m1")
        orch.slack.update_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_strips_action_blocks_and_appends_ack_context(self, orch: MagicMock) -> None:
        payload = _payload(
            message={
                "ts": "m1",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "hello"}},
                    {"type": "actions", "elements": [{"action_id": "a"}]},
                ],
            }
        )
        await ix.ack_button(payload, "C1", "m1")
        blocks = orch.slack.update_message.await_args.kwargs["blocks"]
        assert all(b["type"] != "actions" for b in blocks)
        assert blocks[-1]["elements"][0]["text"] == "✅ Acknowledged"

    @pytest.mark.asyncio
    async def test_truncates_oversized_section_text(self, orch: MagicMock) -> None:
        payload = _payload(
            message={
                "ts": "m1",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 5000}}],
            }
        )
        await ix.ack_button(payload, "C1", "m1")
        blocks = orch.slack.update_message.await_args.kwargs["blocks"]
        assert len(blocks[0]["text"]["text"]) == 2990

    @pytest.mark.asyncio
    async def test_chat_update_failure_is_swallowed(self, orch: MagicMock) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix.ack_button(_payload(), "C1", "m1")


# ---------------------------------------------------------------------------
# dispatch — payload parsing and action routing
# ---------------------------------------------------------------------------


class TestDispatchPayloadParsing:
    @pytest.mark.asyncio
    async def test_view_submission_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(ix, "handle_view_submission", spy)
        await ix.dispatch({"type": "view_submission", "view": {}})
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_view_closed_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(ix, "handle_view_closed", spy)
        await ix.dispatch({"type": "view_closed", "view": {}})
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_message_action_routed_to_shortcut(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_message_shortcut", spy)
        await ix.dispatch({"type": "message_action", "callback_id": "cb"})
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_payload_returns_without_error(self) -> None:
        await ix.dispatch({})

    @pytest.mark.asyncio
    async def test_missing_actions_list_returns_early(self) -> None:
        await ix.dispatch({"type": "block_actions", "actions": []})

    @pytest.mark.asyncio
    async def test_unknown_action_id_falls_through_to_tool_approval(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised action_id is treated as a legacy tool-approval button."""
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_tool_approval", spy)
        await ix.dispatch(_action_payload("totally_unknown_action"))
        spy.assert_awaited_once()
        assert spy.await_args_list[0].args[1] == "totally_unknown_action"

    @pytest.mark.asyncio
    async def test_unknown_action_without_channel_does_nothing(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_tool_approval", spy)
        payload = _action_payload("mystery", channel={}, message={})
        await ix.dispatch(payload)
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_action_entry_without_action_id(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_tool_approval", spy)
        payload = _payload()
        payload["actions"] = [{}]
        await ix.dispatch(payload)
        # Empty action_id still reaches the fallback with an empty id.
        assert spy.await_args_list[0].args[1] == ""


class TestDispatchAuthorization:
    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected_with_ephemeral(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ix, "is_allowed_user", lambda uid: False)
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_tool_approval", spy)
        await ix.dispatch(_action_payload("anything"))
        spy.assert_not_awaited()
        orch.slack.post_ephemeral.assert_awaited_once()
        assert "not authorized" in orch.slack.post_ephemeral.await_args.args[2]

    @pytest.mark.asyncio
    async def test_unauthorized_ephemeral_failure_is_swallowed(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ix, "is_allowed_user", lambda uid: False)
        orch.slack.post_ephemeral = AsyncMock(side_effect=RuntimeError("api down"))
        await ix.dispatch(_action_payload("anything"))

    @pytest.mark.asyncio
    async def test_allowlist_button_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_allowlist", spy)
        await ix.dispatch(_action_payload(ACTION_ALLOWLIST_APPROVE, "U9:Nine"))
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_track_channel_button_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_track_channel", spy)
        await ix.dispatch(_action_payload(ACTION_TRACK_DENY, "C9:name"))
        spy.assert_not_awaited()


class TestDispatchRouting:
    """Each recognised action_id must reach exactly its own handler."""

    @pytest.mark.asyncio
    async def test_checkboxes_toggle_is_a_noop(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.format import OPTIONS_CHECKBOXES_ACTION

        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_options_submit", spy)
        await ix.dispatch(_action_payload(OPTIONS_CHECKBOXES_ACTION))
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spent_options_marker_is_a_noop(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.format import OPTIONS_ACTION_PREFIX

        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_options", spy)
        await ix.dispatch(_action_payload(f"{OPTIONS_ACTION_PREFIX}_done_0"))
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_options_blocked_by_channels_governance(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.format import OPTIONS_SUBMIT_ACTION

        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=False))
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_options_submit", spy)
        await ix.dispatch(_action_payload(OPTIONS_SUBMIT_ACTION))
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_review_action_blocked_by_channels_governance(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=False))
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_review_approve", spy)
        await ix.dispatch(_action_payload("mc_review_approve", "C1|t1|k"))
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_review_cancel_is_exempt_from_governance(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancel posts nothing, so a channels-deny must not strand the draft."""
        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=False))
        spy = AsyncMock()
        monkeypatch.setattr(ix, "_handle_review_cancel", spy)
        await ix.dispatch(_action_payload("mc_review_cancel", "C1|t1|k"))
        spy.assert_awaited_once()

    @pytest.mark.parametrize(
        "action_id,handler_name",
        [
            ("mc_stop_confirm", "_handle_stop_confirm"),
            ("mc_stop_cancel", "_handle_stop_cancel"),
            ("stop_kill_now", "_handle_stop_kill_now"),
            ("mc_agent_select", "_handle_agent_select"),
            ("mc_users_select", "_handle_users_select"),
            ("mc_channels_select", "_handle_channels_select"),
            ("mc_resume_thread_abc", "_handle_resume_choice"),
            ("mc_resume_dm_abc", "_handle_resume_choice"),
            ("mc_session_resume_abc", "_handle_session_resume"),
            ("mc_session_end_abc", "_handle_session_end"),
            ("mc_inline_stop_abc", "_handle_inline_stop"),
            ("mc_session_new", "_handle_session_new"),
            ("mc_ch_activation_C9", "_handle_ch_activation"),
            ("mc_ch_agent_C9", "_handle_ch_agent"),
            ("mc_ch_remove_C9", "_handle_ch_remove"),
            ("mc_ch_add", "_handle_ch_add"),
            ("mc_review_edit", "_handle_review_edit"),
            ("mc_review_revise", "_handle_review_revise"),
            ("mc_allowlist_remove_U9", "_handle_allowlist_remove"),
            ("mc_channel_remove_C9", "_handle_channel_remove"),
        ],
    )
    @pytest.mark.asyncio
    async def test_action_id_reaches_its_handler(
        self,
        orch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        action_id: str,
        handler_name: str,
    ) -> None:
        spy = AsyncMock()
        monkeypatch.setattr(ix, handler_name, spy)
        await ix.dispatch(_action_payload(action_id, "v"))
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dashboard_copy_posts_link_ephemerally(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        payload = _action_payload(
            "mc_dashboard_copy",
            "https://dash.example/x",
            response_url="https://hooks.slack.com/y",
        )
        await ix.dispatch(payload)
        body = _mock_aiohttp.post.await_args.kwargs["json"]
        assert body["response_type"] == "ephemeral"
        assert "https://dash.example/x" in body["text"]

    @pytest.mark.asyncio
    async def test_dashboard_copy_without_url_posts_nothing(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        await ix.dispatch(
            _action_payload("mc_dashboard_copy", "", response_url="https://hooks.slack.com/y")
        )
        _mock_aiohttp.post.assert_not_awaited()


class TestDispatchTransportToolApproval:
    """The ``mc_tool_*`` transport-path approval branch."""

    @pytest.fixture(autouse=True)
    def _decider(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        dec = MagicMock()
        dec.resolve_global = MagicMock(return_value=True)
        dec.session_for = MagicMock(return_value="sess1")
        monkeypatch.setattr(ix, "SlackApprovalDecider", dec)
        return dec

    @pytest.mark.asyncio
    async def test_approve_resolves_and_labels(
        self, orch: MagicMock, _decider: MagicMock
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid1", "sess1:rid1"))
        _decider.resolve_global.assert_called_once_with("sess1:rid1", True)
        assert orch.slack.update_message.await_args.kwargs["text"] == "✅ Approved"

    @pytest.mark.asyncio
    async def test_deny_resolves_false(self, orch: MagicMock, _decider: MagicMock) -> None:
        from kiro_crew.slack.renderer import TOOL_DENY_ACTION_PREFIX

        await ix.dispatch(_action_payload(f"{TOOL_DENY_ACTION_PREFIX}rid2", "sess1:rid2"))
        _decider.resolve_global.assert_called_once_with("sess1:rid2", False)
        assert orch.slack.update_message.await_args.kwargs["text"] == "🚫 Denied"

    @pytest.mark.asyncio
    async def test_trust_grants_session_trust_before_resolving(
        self, orch: MagicMock, _decider: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_TRUST_ACTION_PREFIX

        trust = MagicMock()
        monkeypatch.setattr(ix, "add_trusted_session", trust)
        await ix.dispatch(_action_payload(f"{TOOL_TRUST_ACTION_PREFIX}rid3", "sess1:rid3"))
        trust.assert_called_once()
        assert trust.call_args.args[0] == "sess1"
        _decider.resolve_global.assert_called_once_with("sess1:rid3", True)

    @pytest.mark.asyncio
    async def test_expired_approval_reports_expiry(
        self, orch: MagicMock, _decider: MagicMock
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        _decider.resolve_global = MagicMock(return_value=False)
        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid4", "sess1:rid4"))
        assert "expired" in orch.slack.update_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_approval_key_falls_back_to_action_id_suffix(
        self, orch: MagicMock, _decider: MagicMock
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid5", ""))
        assert _decider.resolve_global.call_args.args[0] == "rid5"

    @pytest.mark.asyncio
    async def test_governance_deny_resolves_approval_as_denied(
        self, orch: MagicMock, _decider: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A channels-deny must refuse the tool, not strand the pending future."""
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=False))
        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid6", "sess1:rid6"))
        _decider.resolve_global.assert_called_once_with("sess1:rid6", False)
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_message_failure_is_swallowed(
        self, orch: MagicMock, _decider: MagicMock
    ) -> None:
        from kiro_crew.slack.renderer import TOOL_APPROVE_ACTION_PREFIX

        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix.dispatch(_action_payload(f"{TOOL_APPROVE_ACTION_PREFIX}rid7", "sess1:rid7"))


# ---------------------------------------------------------------------------
# Channels modal handlers
# ---------------------------------------------------------------------------


class TestChannelsModal:
    @pytest.mark.asyncio
    async def test_refresh_pushes_updated_view(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C1"}
        await ix._refresh_channels_modal("V1")
        orch.slack.views_update.assert_awaited_once()
        assert orch.slack.views_update.await_args.kwargs["view_id"] == "V1"

    @pytest.mark.asyncio
    async def test_refresh_swallows_views_update_failure(self, orch: MagicMock) -> None:
        orch.slack.views_update = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._refresh_channels_modal("V1")

    @pytest.mark.asyncio
    async def test_refresh_without_orch_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._refresh_channels_modal("V1")

    @pytest.mark.asyncio
    async def test_activation_change_persists(self, orch: MagicMock) -> None:
        action = {
            "action_id": "mc_ch_activation_C7",
            "selected_option": {"value": ix.ACTIVATION_REVIEW},
        }
        await ix._handle_ch_activation(_payload(), action)
        assert _read_config()["slack"]["channels"]["C7"]["activation"] == ix.ACTIVATION_REVIEW

    @pytest.mark.asyncio
    async def test_activation_change_defaults_to_mention(self, orch: MagicMock) -> None:
        await ix._handle_ch_activation(_payload(), {"action_id": "mc_ch_activation_C7"})
        assert _read_config()["slack"]["channels"]["C7"]["activation"] == "mention"

    @pytest.mark.asyncio
    async def test_activation_change_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_ch_activation(
            _payload(), {"action_id": "mc_ch_activation_C7", "selected_option": {"value": "all"}}
        )
        assert not config_path().exists()

    @pytest.mark.asyncio
    async def test_agent_change_persists(self, orch: MagicMock) -> None:
        action = {"action_id": "mc_ch_agent_C7", "selected_option": {"value": "reviewer"}}
        await ix._handle_ch_agent(_payload(), action)
        assert _read_config()["slack"]["channels"]["C7"]["agent"] == "reviewer"

    @pytest.mark.asyncio
    async def test_agent_sentinel_clears_override(self, orch: MagicMock) -> None:
        action = {"action_id": "mc_ch_agent_C7", "selected_option": {"value": "__default__"}}
        await ix._handle_ch_agent(_payload(), action)
        assert _read_config()["slack"]["channels"]["C7"]["agent"] == ""

    @pytest.mark.asyncio
    async def test_agent_change_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_ch_agent(
            _payload(), {"action_id": "mc_ch_agent_C7", "selected_option": {"value": "x"}}
        )
        assert not config_path().exists()

    @pytest.mark.asyncio
    async def test_remove_discards_and_refreshes_modal(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C7", "C8"}
        payload = _payload()
        payload["view"] = {"id": "V1"}
        await ix._handle_ch_remove(payload, {"value": "C7"})
        assert orch._tracking_channels == {"C8"}
        orch.slack.views_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remove_without_view_id_skips_refresh(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C7"}
        await ix._handle_ch_remove(_payload(), {"value": "C7"})
        assert orch._tracking_channels == set()
        orch.slack.views_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remove_without_value_is_a_noop(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C7"}
        await ix._handle_ch_remove(_payload(), {})
        assert orch._tracking_channels == {"C7"}

    @pytest.mark.asyncio
    async def test_remove_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        orch._tracking_channels = {"C7"}
        await ix._handle_ch_remove(_payload(), {"value": "C7"})
        assert orch._tracking_channels == {"C7"}

    @pytest.mark.asyncio
    async def test_add_accepts_selected_conversation(self, orch: MagicMock) -> None:
        payload = _payload()
        payload["view"] = {"id": "V1"}
        await ix._handle_ch_add(payload, {"selected_conversation": "C9"})
        assert orch._tracking_channels == {"C9"}
        orch.slack.views_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_accepts_selected_channel_alias(self, orch: MagicMock) -> None:
        await ix._handle_ch_add(_payload(), {"selected_channel": "C9"})
        assert orch._tracking_channels == {"C9"}

    @pytest.mark.asyncio
    async def test_add_without_selection_is_a_noop(self, orch: MagicMock) -> None:
        await ix._handle_ch_add(_payload(), {})
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_add_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_ch_add(_payload(), {"selected_conversation": "C9"})
        assert orch._tracking_channels == set()


# ---------------------------------------------------------------------------
# Voice config modal
# ---------------------------------------------------------------------------


def _voice_view(
    *,
    tts: list[str],
    voice: str = "Joanna",
    engine: str = "neural",
    speed: str = "120%",
    pitch: str = "+5%",
    profile: str = "  prof  ",
    region: str = "us-west-2",
) -> dict:
    def opt(action_id: str, value: str) -> dict:
        return {action_id: {"selected_option": {"value": value}}}

    return {
        "user": {"id": "U1"},
        "view": {
            "state": {
                "values": {
                    "tts_enabled_block": {
                        "mc_voice_tts_enabled": {
                            "selected_options": [{"value": v} for v in tts]
                        }
                    },
                    "voice_block": opt("mc_voice_voice", voice),
                    "engine_block": opt("mc_voice_engine", engine),
                    "speed_block": opt("mc_voice_speed", speed),
                    "pitch_block": opt("mc_voice_pitch", pitch),
                    "profile_block": {"mc_voice_profile": {"value": profile}},
                    "region_block": {"mc_voice_region": {"value": region}},
                }
            }
        },
    }


class TestVoiceConfigSubmission:
    @pytest.mark.asyncio
    async def test_persists_and_applies_settings(self, orch: MagicMock) -> None:
        await ix._handle_voice_config_submission(
            _voice_view(tts=["enabled", "auto_speak"])
        )
        vr = _read_config()["voice_reply"]
        assert vr["enabled"] is True
        assert vr["auto_speak"] is True
        assert vr["voice_id"] == "Joanna"
        assert vr["engine"] == "neural"
        assert vr["rate"] == "120%"
        assert vr["pitch"] == "+5%"
        # Free-text fields are stripped.
        assert vr["aws_profile"] == "prof"
        assert sh._vc.global_enabled is True
        assert sh._vc.default_voice == "Joanna"

    @pytest.mark.asyncio
    async def test_unchecked_boxes_disable_tts(self, orch: MagicMock) -> None:
        await ix._handle_voice_config_submission(_voice_view(tts=[]))
        assert _read_config()["voice_reply"]["enabled"] is False
        assert sh._vc.global_enabled is False

    @pytest.mark.asyncio
    async def test_blank_selects_fall_back_to_current_defaults(self, orch: MagicMock) -> None:
        sh._vc.default_voice = "Ruth"
        await ix._handle_voice_config_submission(
            _voice_view(tts=["enabled"], voice="", engine="", speed="", pitch="")
        )
        assert _read_config()["voice_reply"]["voice_id"] == "Ruth"

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_voice_config_submission(_voice_view(tts=["enabled"]))
        assert not config_path().exists()

    @pytest.mark.asyncio
    async def test_unreadable_config_fails_closed(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any) -> dict:
            raise ConfigReadError("corrupt")

        monkeypatch.setattr(ix, "read_config_for_update", boom)
        sh._vc.global_enabled = False
        await ix._handle_voice_config_submission(_voice_view(tts=["enabled"]))
        # Live TTS must not be driven by a refused save.
        assert sh._vc.global_enabled is False

    @pytest.mark.asyncio
    async def test_write_failure_leaves_live_config_untouched(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any, _data: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ix, "write_config_atomically", boom)
        sh._vc.global_enabled = False
        await ix._handle_voice_config_submission(_voice_view(tts=["enabled"]))
        assert sh._vc.global_enabled is False


# ---------------------------------------------------------------------------
# Cron / subagent acknowledge
# ---------------------------------------------------------------------------


class TestAckHandlers:
    @pytest.mark.asyncio
    async def test_cron_ack_calls_service(self, orch: MagicMock) -> None:
        orch.cron_svc.ack_job_async = AsyncMock()
        orch.dashboard_state = None
        payload = _payload(message={"ts": "m1", "blocks": [], "text": "hello"})
        await ix._handle_cron_ack(payload, {"value": "job1"}, "C1", "m1")
        orch.cron_svc.ack_job_async.assert_awaited_once_with("job1", "hello")

    @pytest.mark.asyncio
    async def test_cron_ack_without_job_id_is_a_noop(self, orch: MagicMock) -> None:
        orch.cron_svc.ack_job_async = AsyncMock()
        await ix._handle_cron_ack(_payload(), {}, "C1", "m1")
        orch.cron_svc.ack_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cron_ack_survives_busy_store(self, orch: MagicMock) -> None:
        from kiro_crew.cron import CronStoreBusy

        orch.cron_svc.ack_job_async = AsyncMock(side_effect=CronStoreBusy("busy"))
        orch.dashboard_state = None
        # Ack is best-effort bookkeeping — a contended store must not raise.
        await ix._handle_cron_ack(_payload(), {"value": "job1"}, "C1", "m1")

    @pytest.mark.asyncio
    async def test_cron_ack_marks_matching_notification(self, orch: MagicMock) -> None:
        orch.cron_svc.ack_job_async = AsyncMock()
        ds = MagicMock()
        ds._notification_log = [
            {"job_id": "job1", "acked": False, "ts": "n1"},
            {"job_id": "job1", "acked": True, "ts": "n2"},
            {"job_id": "other", "acked": False, "ts": "n3"},
        ]
        ds.ack_notification = AsyncMock()
        orch.dashboard_state = ds
        await ix._handle_cron_ack(_payload(), {"value": "job1"}, "C1", "m1")
        ds.ack_notification.assert_awaited_once_with("n1")
        ds.broadcast_ws.assert_called_once_with("notification_ack", {"ts": "n1"})

    @pytest.mark.asyncio
    async def test_subagent_ack_marks_matching_notification(self, orch: MagicMock) -> None:
        ds = MagicMock()
        ds._notification_log = [
            {"kind": "subagent", "title": "agent abc done", "acked": False, "ts": "n1"},
            {"kind": "cron", "title": "abc", "acked": False, "ts": "n2"},
        ]
        ds.ack_notification = AsyncMock()
        orch.dashboard_state = ds
        await ix._handle_subagent_ack(_payload(), {"value": "abc"}, "C1", "m1")
        ds.ack_notification.assert_awaited_once_with("n1")

    @pytest.mark.asyncio
    async def test_subagent_ack_still_acks_button_without_id(self, orch: MagicMock) -> None:
        orch.dashboard_state = None
        await ix._handle_subagent_ack(_payload(), {}, "C1", "m1")
        # The visual ack happens before the early return.
        orch.slack.update_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Allowlist / tracking approve-deny buttons
# ---------------------------------------------------------------------------


class TestAllowlistButtons:
    @pytest.mark.asyncio
    async def test_approve_adds_user_and_dms_them(self, orch: MagicMock) -> None:
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, ACTION_ALLOWLIST_APPROVE, "C1", "m1", "U1"
        )
        assert orch._allowed_users == {"U9"}
        orch.slack.open_dm.assert_awaited_once_with("U9")
        assert "allowlist" in orch.slack.post_message.await_args.args[1]
        assert "Nine" in orch.slack.update_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_deny_removes_user_and_dms_them(self, orch: MagicMock) -> None:
        orch._allowed_users = {"U9"}
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, ACTION_ALLOWLIST_DENY, "C1", "m1", "U1"
        )
        assert orch._allowed_users == set()
        assert "denied" in orch.slack.post_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_missing_user_id_in_value_is_rejected(self, orch: MagicMock) -> None:
        await ix._handle_allowlist(
            _payload(), {"value": ""}, ACTION_ALLOWLIST_APPROVE, "C1", "m1", "U1"
        )
        assert orch._allowed_users == set()
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_value_without_display_name_falls_back_to_id(self, orch: MagicMock) -> None:
        await ix._handle_allowlist(
            _payload(), {"value": "U9"}, ACTION_ALLOWLIST_APPROVE, "C1", "m1", "U1"
        )
        assert "U9" in orch.slack.update_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_dm_failure_does_not_block_the_grant(self, orch: MagicMock) -> None:
        orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("cannot dm"))
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, ACTION_ALLOWLIST_APPROVE, "C1", "m1", "U1"
        )
        assert orch._allowed_users == {"U9"}

    @pytest.mark.asyncio
    async def test_update_message_failure_is_swallowed(self, orch: MagicMock) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, ACTION_ALLOWLIST_APPROVE, "C1", "m1", "U1"
        )
        assert orch._allowed_users == {"U9"}

    @pytest.mark.asyncio
    async def test_unknown_action_id_produces_no_label(self, orch: MagicMock) -> None:
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, "mc_unknown", "C1", "m1", "U1"
        )
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_without_orch_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, ACTION_ALLOWLIST_APPROVE, "C1", "m1", "U1"
        )

    @pytest.mark.asyncio
    async def test_deny_without_orch_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_allowlist(
            _payload(), {"value": "U9:Nine"}, ACTION_ALLOWLIST_DENY, "C1", "m1", "U1"
        )


class TestTrackChannelButtons:
    @pytest.mark.asyncio
    async def test_approve_starts_tracking(self, orch: MagicMock) -> None:
        await ix._handle_track_channel(
            _payload(), {"value": "C9:general"}, ACTION_TRACK_APPROVE, "C1", "m1", "U1"
        )
        assert orch._tracking_channels == {"C9"}
        assert "general" in orch.slack.update_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_deny_stops_tracking(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C9"}
        await ix._handle_track_channel(
            _payload(), {"value": "C9:general"}, ACTION_TRACK_DENY, "C1", "m1", "U1"
        )
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_missing_channel_id_is_rejected(self, orch: MagicMock) -> None:
        await ix._handle_track_channel(
            _payload(), {"value": ""}, ACTION_TRACK_APPROVE, "C1", "m1", "U1"
        )
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_unknown_action_id_produces_no_label(self, orch: MagicMock) -> None:
        await ix._handle_track_channel(
            _payload(), {"value": "C9:general"}, "mc_unknown", "C1", "m1", "U1"
        )
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_failure_is_swallowed(self, orch: MagicMock) -> None:
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._handle_track_channel(
            _payload(), {"value": "C9:general"}, ACTION_TRACK_APPROVE, "C1", "m1", "U1"
        )
        assert orch._tracking_channels == {"C9"}

    @pytest.mark.asyncio
    async def test_approve_without_orch_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_track_channel(
            _payload(), {"value": "C9:general"}, ACTION_TRACK_APPROVE, "C1", "m1", "U1"
        )

    @pytest.mark.asyncio
    async def test_deny_without_orch_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_track_channel(
            _payload(), {"value": "C9:general"}, ACTION_TRACK_DENY, "C1", "m1", "U1"
        )


# ---------------------------------------------------------------------------
# Select menus
# ---------------------------------------------------------------------------


class TestAgentSelect:
    @pytest.mark.asyncio
    async def test_switches_to_resolved_agent(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sh, "_resolve_agent_name", lambda n, project_dir=None: "reviewer")
        setter = MagicMock()
        monkeypatch.setattr(sh, "_set_default_agent", setter)
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "rev"}}
        await ix._handle_agent_select(_payload(), action, "C1", "m1", "U1")
        setter.assert_called_once_with("reviewer")
        assert "reviewer" in orch.slack.update_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_off_resets_to_default(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        setter = MagicMock()
        monkeypatch.setattr(sh, "_set_default_agent", setter)
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "off"}}
        await ix._handle_agent_select(_payload(), action, "C1", "m1", "U1")
        setter.assert_called_once_with("")
        assert "default" in orch.slack.update_message.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_reset_rejected_by_setter_aborts(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            sh, "_set_default_agent", MagicMock(side_effect=ValueError("bad name"))
        )
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "default"}}
        await ix._handle_agent_select(_payload(), action, "C1", "m1", "U1")
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unresolvable_agent_aborts(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sh, "_resolve_agent_name", lambda n, project_dir=None: None)
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "ghost"}}
        await ix._handle_agent_select(_payload(), action, "C1", "m1", "U1")
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_switch_rejected_by_setter_aborts(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sh, "_resolve_agent_name", lambda n, project_dir=None: "reviewer")
        monkeypatch.setattr(
            sh, "_set_default_agent", MagicMock(side_effect=ValueError("locked"))
        )
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "rev"}}
        await ix._handle_agent_select(_payload(), action, "C1", "m1", "U1")
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_selection_is_a_noop(self, orch: MagicMock) -> None:
        await ix._handle_agent_select(
            _payload(), {"action_id": "mc_agent_select"}, "C1", "m1", "U1"
        )
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "rev"}}
        await ix._handle_agent_select(_payload(), action, "C1", "m1", "U1")
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_response_url_path_skips_chat_update(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch, _mock_aiohttp: AsyncMock
    ) -> None:
        monkeypatch.setattr(sh, "_set_default_agent", MagicMock())
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "off"}}
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_agent_select(payload, action, "C1", "m1", "U1")
        _mock_aiohttp.post.assert_awaited_once()
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_response_url_failure_falls_back_to_chat_update(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch, _mock_aiohttp: AsyncMock
    ) -> None:
        monkeypatch.setattr(sh, "_set_default_agent", MagicMock())
        _mock_aiohttp.post = AsyncMock(side_effect=RuntimeError("boom"))
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "off"}}
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_agent_select(payload, action, "C1", "m1", "U1")
        orch.slack.update_message.assert_awaited_once()


class TestUsersSelect:
    @pytest.mark.asyncio
    async def test_persists_and_applies_allowlist(self, orch: MagicMock) -> None:
        action = {"action_id": "mc_users_select", "selected_users": ["Ub", "Ua"]}
        await ix._handle_users_select(_payload(), action, "C1", "m1", "U1")
        assert _read_config()["slack"]["allowed_users"] == [
            {"slack_id": "Ua"},
            {"slack_id": "Ub"},
        ]
        assert orch._allowed_users == {"Ua", "Ub"}

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        action = {"action_id": "mc_users_select", "selected_users": ["Ua"]}
        await ix._handle_users_select(_payload(), action, "C1", "m1", "U1")
        assert not config_path().exists()

    @pytest.mark.asyncio
    async def test_unreadable_config_fails_closed(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any) -> dict:
            raise ConfigReadError("corrupt")

        monkeypatch.setattr(ix, "read_config_for_update", boom)
        action = {"action_id": "mc_users_select", "selected_users": ["Ua"]}
        await ix._handle_users_select(_payload(), action, "C1", "m1", "U1")
        assert orch._allowed_users == set()

    @pytest.mark.asyncio
    async def test_write_failure_leaves_runtime_untouched(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any, _data: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ix, "write_config_atomically", boom)
        action = {"action_id": "mc_users_select", "selected_users": ["Ua"]}
        await ix._handle_users_select(_payload(), action, "C1", "m1", "U1")
        assert orch._allowed_users == set()


class TestChannelsSelect:
    @pytest.mark.asyncio
    async def test_persists_and_applies_channels(self, orch: MagicMock) -> None:
        action = {"action_id": "mc_channels_select", "selected_channels": ["Cb", "Ca"]}
        await ix._handle_channels_select(_payload(), action, "C1", "m1", "U1")
        assert _read_config()["slack"]["tracking_channels"] == [
            {"channel_id": "Ca"},
            {"channel_id": "Cb"},
        ]
        assert orch._tracking_channels == {"Ca", "Cb"}

    @pytest.mark.asyncio
    async def test_empty_selection_clears_tracking(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"Cold"}
        await ix._handle_channels_select(
            _payload(), {"action_id": "mc_channels_select"}, "C1", "m1", "U1"
        )
        assert _read_config()["slack"]["tracking_channels"] == []
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        action = {"action_id": "mc_channels_select", "selected_channels": ["Ca"]}
        await ix._handle_channels_select(_payload(), action, "C1", "m1", "U1")
        assert not config_path().exists()

    @pytest.mark.asyncio
    async def test_unreadable_config_fails_closed(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any) -> dict:
            raise ConfigReadError("corrupt")

        monkeypatch.setattr(ix, "read_config_for_update", boom)
        action = {"action_id": "mc_channels_select", "selected_channels": ["Ca"]}
        await ix._handle_channels_select(_payload(), action, "C1", "m1", "U1")
        assert orch._tracking_channels == set()

    @pytest.mark.asyncio
    async def test_write_failure_leaves_runtime_untouched(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_path: Any, _data: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ix, "write_config_atomically", boom)
        action = {"action_id": "mc_channels_select", "selected_channels": ["Ca"]}
        await ix._handle_channels_select(_payload(), action, "C1", "m1", "U1")
        assert orch._tracking_channels == set()


# ---------------------------------------------------------------------------
# Stop cancel / list-remove buttons / new session
# ---------------------------------------------------------------------------


class TestStopCancel:
    @pytest.mark.asyncio
    async def test_response_url_deletes_original(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        await ix._handle_stop_cancel(_payload(response_url="https://hooks.slack.com/z"), "C1", "m1")
        assert _mock_aiohttp.post.await_args.kwargs["json"] == {"delete_original": True}
        orch.slack.delete_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_response_url_failure_is_swallowed(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        _mock_aiohttp.post = AsyncMock(side_effect=RuntimeError("boom"))
        await ix._handle_stop_cancel(_payload(response_url="https://hooks.slack.com/z"), "C1", "m1")

    @pytest.mark.asyncio
    async def test_without_response_url_deletes_via_api(self, orch: MagicMock) -> None:
        await ix._handle_stop_cancel(_payload(), "C1", "m1")
        orch.slack.delete_message.assert_awaited_once_with("C1", "m1")

    @pytest.mark.asyncio
    async def test_api_delete_failure_is_swallowed(self, orch: MagicMock) -> None:
        orch.slack.delete_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._handle_stop_cancel(_payload(), "C1", "m1")


class TestListRemoveButtons:
    @pytest.mark.asyncio
    async def test_allowlist_remove_updates_message(self, orch: MagicMock) -> None:
        orch._allowed_users = {"U9", "U8"}
        await ix._handle_allowlist_remove(_payload(), {"value": "U9"}, "C1", "m1", "U1")
        assert orch._allowed_users == {"U8"}
        blocks = orch.slack.update_message.await_args.kwargs["blocks"]
        assert "U9" in blocks[-1]["elements"][0]["text"]

    @pytest.mark.asyncio
    async def test_allowlist_remove_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        orch._allowed_users = {"U9"}
        await ix._handle_allowlist_remove(_payload(), {"value": "U9"}, "C1", "m1", "U1")
        assert orch._allowed_users == {"U9"}

    @pytest.mark.asyncio
    async def test_allowlist_remove_without_target_is_a_noop(self, orch: MagicMock) -> None:
        orch._allowed_users = {"U9"}
        await ix._handle_allowlist_remove(_payload(), {}, "C1", "m1", "U1")
        assert orch._allowed_users == {"U9"}

    @pytest.mark.asyncio
    async def test_allowlist_remove_prefers_response_url(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        orch._allowed_users = {"U9"}
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_allowlist_remove(payload, {"value": "U9"}, "C1", "m1", "U1")
        _mock_aiohttp.post.assert_awaited_once()
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowlist_remove_falls_back_when_response_url_fails(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        _mock_aiohttp.post = AsyncMock(side_effect=RuntimeError("boom"))
        orch._allowed_users = {"U9"}
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_allowlist_remove(payload, {"value": "U9"}, "C1", "m1", "U1")
        orch.slack.update_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_channel_remove_updates_message(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C9", "C8"}
        await ix._handle_channel_remove(_payload(), {"value": "C9"}, "C1", "m1", "U1")
        assert orch._tracking_channels == {"C8"}
        blocks = orch.slack.update_message.await_args.kwargs["blocks"]
        assert "C9" in blocks[-1]["elements"][0]["text"]

    @pytest.mark.asyncio
    async def test_channel_remove_requires_owner(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        orch._tracking_channels = {"C9"}
        await ix._handle_channel_remove(_payload(), {"value": "C9"}, "C1", "m1", "U1")
        assert orch._tracking_channels == {"C9"}

    @pytest.mark.asyncio
    async def test_channel_remove_without_target_is_a_noop(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C9"}
        await ix._handle_channel_remove(_payload(), {}, "C1", "m1", "U1")
        assert orch._tracking_channels == {"C9"}

    @pytest.mark.asyncio
    async def test_channel_remove_prefers_response_url(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        orch._tracking_channels = {"C9"}
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_channel_remove(payload, {"value": "C9"}, "C1", "m1", "U1")
        _mock_aiohttp.post.assert_awaited_once()
        orch.slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_remove_api_failure_is_swallowed(self, orch: MagicMock) -> None:
        orch._tracking_channels = {"C9"}
        orch.slack.update_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._handle_channel_remove(_payload(), {"value": "C9"}, "C1", "m1", "U1")
        assert orch._tracking_channels == set()


class TestSessionNew:
    @pytest.mark.asyncio
    async def test_posts_fresh_thread_and_acks(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_session_new(payload, {"action_id": "mc_session_new"}, "C1", "m1", "U1")
        assert "New session started" in orch.slack.post_message.await_args.args[1]
        assert _mock_aiohttp.post.await_args.kwargs["json"]["text"] == "✨ New session created."

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_session_new(_payload(), {}, "C1", "m1", "U1")
        orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_failure_skips_ack(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        orch.slack.post_message = AsyncMock(side_effect=RuntimeError("api down"))
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_session_new(payload, {}, "C1", "m1", "U1")
        _mock_aiohttp.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ack_failure_is_swallowed(
        self, orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        _mock_aiohttp.post = AsyncMock(side_effect=RuntimeError("boom"))
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_session_new(payload, {}, "C1", "m1", "U1")

    @pytest.mark.asyncio
    async def test_without_orch_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_owner(monkeypatch, True)
        monkeypatch.setattr(ix, "_orch", None)
        await ix._handle_session_new(_payload(), {}, "C1", "m1", "U1")


# ---------------------------------------------------------------------------
# Session resume choice
# ---------------------------------------------------------------------------


@pytest.fixture
def resume_orch(orch: MagicMock) -> MagicMock:
    """An orchestrator whose session map reports no existing Slack link."""
    orch.sessions.get_slack_link = MagicMock(return_value=("", ""))
    orch.sessions.set_slack_link = MagicMock()
    orch.dashboard_state = None
    return orch


def _choice(key: str = "dashboard_s1", **extra: Any) -> dict:
    val = {"key": key, "title": "My session"}
    val.update(extra)
    return {"action_id": "mc_resume_thread_x", "value": json.dumps(val)}


class TestResumeChoice:
    @pytest.mark.asyncio
    async def test_thread_mode_links_session(self, resume_orch: MagicMock) -> None:
        await ix._handle_resume_choice(
            _payload(), _choice(src_channel="C5"), "C1", "m1", "U1", mode="thread"
        )
        resume_orch.slack.post_message.assert_any_await(
            "C5",
            "🧵 *My session*\nSession resumed. Continue the conversation in this thread.",
        )
        resume_orch.sessions.set_slack_link.assert_called_once_with("dashboard_s1", "ts1", "C5")

    @pytest.mark.asyncio
    async def test_dm_mode_opens_dm_and_links(self, resume_orch: MagicMock) -> None:
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="dm")
        resume_orch.slack.open_dm.assert_awaited_once_with("U1")
        resume_orch.sessions.set_slack_link.assert_called_once_with("dashboard_s1", "ts1", "D1")

    @pytest.mark.asyncio
    async def test_unknown_mode_returns_without_linking(self, resume_orch: MagicMock) -> None:
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="carrier")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, resume_orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="thread")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_json_value_is_rejected(self, resume_orch: MagicMock) -> None:
        action = {"action_id": "mc_resume_thread_x", "value": "not json{"}
        await ix._handle_resume_choice(_payload(), action, "C1", "m1", "U1", mode="thread")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_session_key_is_rejected(self, resume_orch: MagicMock) -> None:
        action = {"action_id": "mc_resume_thread_x", "value": json.dumps({"title": "t"})}
        await ix._handle_resume_choice(_payload(), action, "C1", "m1", "U1", mode="thread")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_linked_session_reports_link(
        self, resume_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        resume_orch.sessions.get_slack_link = MagicMock(return_value=("111.222", "C4"))
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_resume_choice(payload, _choice(), "C1", "m1", "U1", mode="thread")
        text = _mock_aiohttp.post.await_args.kwargs["json"]["text"]
        assert "archives/C4/p111222" in text
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_governance_deny_blocks_transcript_publication(
        self, resume_orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=False))
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="thread")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_post_failure_aborts(self, resume_orch: MagicMock) -> None:
        resume_orch.slack.post_message = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="thread")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_post_returning_no_ts_aborts(self, resume_orch: MagicMock) -> None:
        resume_orch.slack.post_message = AsyncMock(return_value="")
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="thread")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_open_failure_aborts(self, resume_orch: MagicMock) -> None:
        resume_orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("cannot dm"))
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="dm")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_without_channel_aborts(self, resume_orch: MagicMock) -> None:
        resume_orch.slack.open_dm = AsyncMock(return_value="")
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="dm")
        resume_orch.sessions.set_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_dashboard_state_link_is_notified(self, resume_orch: MagicMock) -> None:
        ds = MagicMock()
        resume_orch.dashboard_state = ds
        await ix._handle_resume_choice(
            _payload(), _choice(key="slack:slot9"), "C1", "m1", "U1", mode="dm"
        )
        ds.link_slack.assert_called_once_with("slot9", "ts1", "D1")

    @pytest.mark.asyncio
    async def test_posts_recent_transcript_context(
        self, resume_orch: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        lines = [json.dumps({"role": "user", "content": f"msg{i}"}) for i in range(7)]
        lines.append(json.dumps({"_type": "meta", "role": "user", "content": "skipme"}))
        lines.append("not-json")
        lines.append(json.dumps({"role": "tool", "content": "ignored"}))
        (sessions / "s9.jsonl").write_text("\n".join(lines), encoding="utf-8")
        monkeypatch.setattr(ix, "_orch", resume_orch)
        monkeypatch.setattr("kiro_crew.config.loader.data_home", lambda: tmp_path)

        await ix._handle_resume_choice(
            _payload(), _choice(key="s9"), "C1", "m1", "U1", mode="dm"
        )
        texts = [c.args[1] for c in resume_orch.slack.post_message.await_args_list]
        # Header + the last 5 user messages only.
        assert sum("msg" in t for t in texts) == 5
        assert not any("skipme" in t for t in texts)
        assert not any("ignored" in t for t in texts)

    @pytest.mark.asyncio
    async def test_dashboard_prefixed_transcript_is_found(
        self, resume_orch: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "dashboard_s9.jsonl").write_text(
            json.dumps({"role": "assistant", "content": "hi there"}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.data_home", lambda: tmp_path)
        await ix._handle_resume_choice(
            _payload(), _choice(key="s9"), "C1", "m1", "U1", mode="dm"
        )
        texts = [c.args[1] for c in resume_orch.slack.post_message.await_args_list]
        assert any("hi there" in t for t in texts)

    @pytest.mark.asyncio
    async def test_evicts_unlocked_locks_when_map_grows(self, resume_orch: MagicMock) -> None:
        import asyncio as _asyncio

        for i in range(1100):
            ix._resume_locks[f"stale{i}"] = _asyncio.Lock()
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="dm")
        # 200 evicted, plus this call's own key added.
        assert len(ix._resume_locks) == 901

    @pytest.mark.asyncio
    async def test_without_sessions_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        o = _make_orch()
        o.sessions = None
        monkeypatch.setattr(ix, "_orch", o)
        _set_owner(monkeypatch, True)
        await ix._handle_resume_choice(_payload(), _choice(), "C1", "m1", "U1", mode="dm")
        o.slack.open_dm.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stop confirm
# ---------------------------------------------------------------------------


@pytest.fixture
def stop_orch(orch: MagicMock) -> MagicMock:
    """An orchestrator with a live session and a stubbed ``stop_turn``."""
    orch.sessions.has_session = MagicMock(return_value=True)
    orch._session_tasks = {}
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    return orch


class TestStopConfirm:
    @pytest.mark.asyncio
    async def test_soft_stop_notifies_thread(self, stop_orch: MagicMock) -> None:
        async def stop_turn(key: str, on_soft: Any = None, on_hard: Any = None) -> str:
            await on_soft()
            return "soft"

        stop_orch.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
        payload = _payload(message={"ts": "m1", "blocks": [], "thread_ts": "t1"})
        await ix._handle_stop_confirm(payload, "C1", "m1", "U1")
        stop_orch.slack.post_message.assert_awaited_once_with("C1", "⏹ Execution stopped.", "t1")

    @pytest.mark.asyncio
    async def test_hard_stop_reports_session_reset(self, stop_orch: MagicMock) -> None:
        async def stop_turn(key: str, on_soft: Any = None, on_hard: Any = None) -> str:
            await on_hard()
            return "hard"

        stop_orch.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
        await ix._handle_stop_confirm(_payload(), "C1", "m1", "U1")
        assert "session reset" in stop_orch.slack.post_message.await_args.args[1]

    @pytest.mark.asyncio
    async def test_callback_posts_via_response_url(
        self, stop_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        async def stop_turn(key: str, on_soft: Any = None, on_hard: Any = None) -> str:
            await on_soft()
            return "soft"

        stop_orch.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_stop_confirm(payload, "C1", "m1", "U1")
        assert _mock_aiohttp.post.await_args.kwargs["json"]["text"] == "⏹ [Stopped]"

    @pytest.mark.asyncio
    async def test_response_url_failure_is_swallowed(
        self, stop_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        _mock_aiohttp.post = AsyncMock(side_effect=RuntimeError("boom"))

        async def stop_turn(key: str, on_soft: Any = None, on_hard: Any = None) -> str:
            await on_soft()
            return "soft"

        stop_orch.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_stop_confirm(payload, "C1", "m1", "U1")

    @pytest.mark.asyncio
    async def test_idle_outcome_dismisses_ephemeral(
        self, stop_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        stop_orch.sessions.stop_turn = AsyncMock(return_value="idle")
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_stop_confirm(payload, "C1", "m1", "U1")
        assert _mock_aiohttp.post.await_args.kwargs["json"]["text"] == "Nothing running."

    @pytest.mark.asyncio
    async def test_pending_task_is_cancelled(self, stop_orch: MagicMock) -> None:
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        stop_orch._session_tasks = {"m1": task}
        stop_orch.sessions.has_session = MagicMock(return_value=False)
        await ix._handle_stop_confirm(_payload(), "C1", "m1", "U1")
        task.cancel.assert_called_once()
        assert stop_orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_finished_task_is_not_cancelled(self, stop_orch: MagicMock) -> None:
        task = MagicMock()
        task.done = MagicMock(return_value=True)
        stop_orch._session_tasks = {"m1": task}
        await ix._handle_stop_confirm(_payload(), "C1", "m1", "U1")
        task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_session_reports_nothing_running(
        self, stop_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        stop_orch.sessions.has_session = MagicMock(return_value=False)
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_stop_confirm(payload, "C1", "m1", "U1")
        assert _mock_aiohttp.post.await_args.kwargs["json"]["text"] == "Nothing running."
        stop_orch.sessions.stop_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_session_without_response_url_updates_message(
        self, stop_orch: MagicMock
    ) -> None:
        stop_orch.sessions.has_session = MagicMock(return_value=False)
        await ix._handle_stop_confirm(_payload(), "C1", "m1", "U1")
        assert stop_orch.slack.update_message.await_args.kwargs["text"] == "Nothing running."

    @pytest.mark.asyncio
    async def test_unauthorized_user_only_acks(
        self, stop_orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ix, "is_allowed_user", lambda uid: False)
        await ix._handle_stop_confirm(_payload(), "C1", "m1", "U1")
        stop_orch.sessions.stop_turn.assert_not_awaited()
        # The button is still visually acked so it cannot be re-clicked forever.
        stop_orch.slack.update_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_without_sessions_only_acks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        o = _make_orch()
        o.sessions = None
        monkeypatch.setattr(ix, "_orch", o)
        monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
        await ix._handle_stop_confirm(_payload(), "C1", "m1", "U1")
        o.slack.update_message.assert_awaited_once()


# ---------------------------------------------------------------------------
# Session resume (choice-button offer)
# ---------------------------------------------------------------------------


class TestSessionResume:
    @pytest.mark.asyncio
    async def test_offers_thread_and_dm_buttons(self, resume_orch: MagicMock) -> None:
        action = {"value": json.dumps({"key": "s1", "title": "Nightly triage"})}
        await ix._handle_session_resume(_payload(), action, "C1", "m1", "U1")
        blocks = resume_orch.slack.post_blocks.await_args.args[1]
        ids = [e["action_id"] for e in blocks[1]["elements"]]
        assert ids[0].startswith("mc_resume_thread_")
        assert ids[1].startswith("mc_resume_dm_")
        # The choice value carries the originating channel forward.
        assert json.loads(blocks[1]["elements"][0]["value"])["src_channel"] == "C1"

    @pytest.mark.asyncio
    async def test_plain_string_value_is_treated_as_a_session_key(
        self, resume_orch: MagicMock
    ) -> None:
        await ix._handle_session_resume(_payload(), {"value": "s1"}, "C1", "m1", "U1")
        blocks = resume_orch.slack.post_blocks.await_args.args[1]
        assert json.loads(blocks[1]["elements"][0]["value"])["key"] == "s1"

    @pytest.mark.asyncio
    async def test_empty_value_is_rejected(self, resume_orch: MagicMock) -> None:
        await ix._handle_session_resume(_payload(), {"value": "{}"}, "C1", "m1", "U1")
        resume_orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_owner_rejected(
        self, resume_orch: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_owner(monkeypatch, False)
        await ix._handle_session_resume(_payload(), {"value": "s1"}, "C1", "m1", "U1")
        resume_orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_linked_reports_existing_conversation(
        self, resume_orch: MagicMock
    ) -> None:
        resume_orch.sessions.get_slack_link = MagicMock(return_value=("111.222", "C4"))
        await ix._handle_session_resume(_payload(), {"value": "s1"}, "C1", "m1", "U1")
        assert "archives/C4/p111222" in resume_orch.slack.post_message.await_args.args[1]
        resume_orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_linked_prefers_response_url(
        self, resume_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        resume_orch.sessions.get_slack_link = MagicMock(return_value=("111.222", "C4"))
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_session_resume(payload, {"value": "s1"}, "C1", "m1", "U1")
        _mock_aiohttp.post.assert_awaited_once()
        resume_orch.slack.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_home_tab_click_falls_back_to_dm(self, resume_orch: MagicMock) -> None:
        """A Home Tab payload has no channel — the offer must land in the user's DM."""
        await ix._handle_session_resume(_payload(channel={}), {"value": "s1"}, "", "", "U1")
        resume_orch.slack.open_dm.assert_awaited_once_with("U1")
        assert resume_orch.slack.post_blocks.await_args.args[0] == "D1"

    @pytest.mark.asyncio
    async def test_open_dm_failure_leaves_nowhere_to_post(self, resume_orch: MagicMock) -> None:
        resume_orch.slack.open_dm = AsyncMock(side_effect=RuntimeError("cannot dm"))
        await ix._handle_session_resume(_payload(channel={}), {"value": "s1"}, "", "", "U1")
        resume_orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_response_url_carries_the_choice_blocks(
        self, resume_orch: MagicMock, _mock_aiohttp: AsyncMock
    ) -> None:
        payload = _payload(response_url="https://hooks.slack.com/z")
        await ix._handle_session_resume(payload, {"value": "s1"}, "C1", "m1", "U1")
        body = _mock_aiohttp.post.await_args.kwargs["json"]
        assert len(body["blocks"]) == 2
        resume_orch.slack.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_blocks_failure_is_swallowed(self, resume_orch: MagicMock) -> None:
        resume_orch.slack.post_blocks = AsyncMock(side_effect=RuntimeError("api down"))
        await ix._handle_session_resume(_payload(), {"value": "s1"}, "C1", "m1", "U1")

    @pytest.mark.asyncio
    async def test_without_sessions_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        o = _make_orch()
        o.sessions = None
        monkeypatch.setattr(ix, "_orch", o)
        _set_owner(monkeypatch, True)
        await ix._handle_session_resume(_payload(), {"value": "s1"}, "C1", "m1", "U1")
        o.slack.post_blocks.assert_not_awaited()
