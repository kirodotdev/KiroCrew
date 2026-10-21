"""The small pure helpers of :mod:`kiro_crew.slack.handler`, called directly.

Pins tool phases, options-tag filtering, the timing footer and footer actions, condensed
thinking, grant wording and the owner, channel, trust and config accessors. The commands
live in ``test_slack_handler_coverage.py``, privacy modifiers and linked-thread
routing in ``test_slack_handler_coverage_inbound.py``, the approval prompts in
``test_slack_handler_coverage_approvals.py``, and status reactions, review drafts and the
safe update paths in ``test_slack_handler_coverage_delivery.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from slack_handler_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clean_handler_state,
)

from kiro_crew.safety_override import NO_EXPIRY_TEXT, fmt_grant_duration
from kiro_crew.slack import handler as h


# ──────────────────────────────────────────────────────────────────────
# small pure helpers
# ──────────────────────────────────────────────────────────────────────
class TestPureHelpers:
    def test_tool_to_phase_by_kind(self):
        assert h._tool_to_phase("Whatever", "bash") == "coding"
        assert h._tool_to_phase("Whatever", "webfetch") == "browsing"

    def test_tool_to_phase_by_name(self):
        assert h._tool_to_phase("Edit") == "coding"
        assert h._tool_to_phase("WebSearch") == "browsing"
        assert h._tool_to_phase("mcp__example-mcp__Bash") == "coding"
        assert h._tool_to_phase("SomethingElse") == "tool"

    def test_build_phase_emojis_collects_unknown_keys(self):
        result, unknown = h._build_phase_emojis({"done": "tada", "bogus": "x"})
        assert result["done"] == "tada"
        assert unknown == ["bogus"]

    def test_build_phase_emojis_accepts_suppression(self):
        result, unknown = h._build_phase_emojis({"done": None})
        assert result["done"] is None and unknown == []

    def test_filter_options_brackets_drops_options_tag(self):
        hold, buf = h._filter_options_brackets("hi [OPTIONS: a | b] bye", "", "")
        assert hold == "" and buf == "hi  bye"

    def test_filter_options_brackets_keeps_other_brackets(self):
        _, buf = h._filter_options_brackets("see [1] here", "", "")
        assert buf == "see [1] here"

    def test_filter_options_brackets_holds_open_bracket(self):
        hold, buf = h._filter_options_brackets("tail [OPTI", "", "")
        assert hold == "[OPTI" and buf == "tail "

    def test_timing_footer_seconds_and_minutes(self):
        _, text = h.build_timing_footer(42.0)
        assert text == "Finished in 42s"
        _, text = h.build_timing_footer(125.0)
        assert text == "Finished in 2m 5s"

    @pytest.mark.parametrize(("pct", "icon"), [(80, "🔴"), (60, "🟠"), (40, "🟡"), (5, "🟢")])
    def test_timing_footer_context_icon(self, pct, icon):
        client = MagicMock()
        client.context_usage_pct.return_value = pct
        _, text = h.build_timing_footer(1.0, client)
        assert icon in text and f"ctx {pct}%" in text

    def test_timing_footer_survives_broken_client(self):
        client = MagicMock()
        client.context_usage_pct.side_effect = RuntimeError("no ctx")
        _, text = h.build_timing_footer(1.0, client)
        assert text == "Finished in 1s"

    def test_append_footer_actions_adds_options(self):
        blocks = h._append_footer_actions([{"type": "context"}], ["a", "b"], None, None, None)
        assert len(blocks) > 1

    def test_append_footer_actions_appends_link_button_to_existing_actions(self):
        footer = [{"type": "actions", "elements": []}]
        out = h._append_footer_actions(footer, None, "t1", None, MagicMock())
        assert len(out[-1]["elements"]) == 1

    def test_append_footer_actions_creates_actions_block(self):
        out = h._append_footer_actions([{"type": "context"}], None, "t1", None, MagicMock())
        assert out[-1]["type"] == "actions"

    def test_append_footer_actions_skips_when_already_linked(self):
        footer = [{"type": "context"}]
        assert h._append_footer_actions(footer, None, "t1", "slot-1", MagicMock()) == footer

    def test_condense_thinking_short_text(self):
        out = h._condense_thinking("one\n\ntwo")
        assert out.startswith("💭 *Thinking*")
        assert "> one" in out and "\n>\n" in out
        assert "full reasoning" not in out

    def test_condense_thinking_truncates_on_whitespace(self):
        out = h._condense_thinking(" ".join(["word"] * 300), limit=40)
        assert "full reasoning in dashboard Activity" in out
        assert len(out) < 200

    def test_condense_thinking_hard_cut_without_early_boundary(self):
        out = h._condense_thinking("a" * 50 + " tail", limit=20)
        assert "full reasoning" in out

    def test_fmt_duration(self):
        assert fmt_grant_duration(7200) == "2h"
        assert fmt_grant_duration(1800) == "30min"

    def test_describe_new_grant(self):
        assert h.describe_new_grant(0) == NO_EXPIRY_TEXT
        assert h.describe_new_grant(3600) == "auto-expires in 1h"

    def test_describe_grant_lifetime_off(self):
        assert h.describe_grant_lifetime() == "off"

    def test_describe_grant_lifetime_when_active(self):
        h.enable_yolo_with_ttl(600)
        try:
            assert "remaining" in h.describe_grant_lifetime()
        finally:
            h.disable_yolo()

    def test_disable_yolo_when_inactive_is_a_no_op(self):
        h.add_trusted_session("t1")
        h.disable_yolo()
        assert h.is_session_trusted("t1")

    def test_disable_yolo_clears_trusted_sessions(self):
        h.enable_yolo_with_ttl(600)
        h.add_trusted_session("t1")
        h.disable_yolo()
        assert not h.is_session_trusted("t1")

    def test_is_owner_cross_matches_w_and_u_prefixes(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "U123")
        assert h.is_owner("U123") is True
        assert h.is_owner("W123") is True
        assert h.is_owner("U999") is False
        assert h.is_owner("") is False

    def test_is_owner_without_configured_owner(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "")
        assert h.is_owner("U1") is False

    def test_is_allowed_user_requires_owner(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "U1")
        assert h.is_allowed_user("") is False
        assert h.is_allowed_user("U1") is True
        assert h.is_allowed_user("U2") is False

    def test_open_channels_are_disabled(self):
        h.set_open_channels({"C1"})
        assert h.is_open_channel("C1") is False

    def test_tracked_channels(self):
        h.set_tracking_channels({"C1"})
        assert h.is_tracked_channel("C1") is True
        assert h.is_tracked_channel("C2") is False
        assert h.is_tracked_channel("") is False

    def test_set_allowed_users_and_owner_setters(self):
        h.set_allowed_users({"U9"})
        h.set_owner_id("U9")
        try:
            assert h.is_allowed_user("U9") is True
        finally:
            h.set_owner_id("")

    def test_trusted_session_requires_non_empty_key(self):
        assert h.is_slack_session_trusted("") is False
        h.add_trusted_session("")
        assert not h._trusted_sessions

    def test_add_trusted_session_survives_policy_failure(self):
        sm = MagicMock()
        sm.set_approval_policy.side_effect = RuntimeError("closed")
        h.add_trusted_session("t1", sm)
        assert h.is_slack_session_trusted("t1")

    def test_dashboard_state_accessors(self):
        state = object()
        h.set_dashboard_state(state)
        assert h.get_dashboard_state() is state

    def test_orch_cfg_accessor_defaults_to_none(self):
        assert h.get_orch_cfg() is None

    def test_reload_orch_cfg_without_config_is_a_no_op(self):
        h._reload_orch_cfg()
        assert h.get_orch_cfg() is None

    def test_reload_orch_cfg_refreshes_channel_state(self):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        h.set_orch_cfg(cfg)
        h._persist_channel_config("C1", activation="observe")
        h._reload_orch_cfg()
        assert cfg.slack_channels["C1"].activation == "observe"

    def test_cancel_background_tasks_empties_the_set(self, monkeypatch):
        # Swap in a private set: the module-level one may hold real leaked tasks
        # from earlier tests whose loops are already closed.
        own: set = set()
        monkeypatch.setattr(h, "_background_tasks", own)
        task = MagicMock()
        own.add(task)
        h.cancel_background_tasks()
        task.cancel.assert_called_once()
        assert not own

    def test_should_auto_approve_spawn(self):
        from types import SimpleNamespace

        builder = MagicMock()
        builder.is_auto_approved_spawn.return_value = True
        event = SimpleNamespace(
            tool_name="spawn_run",
            mcp_server_name="kirocrew-core",
            mcp_identity_trusted=True,
            title="spawn_run",
            is_shell=False,
            shell_classified=True,
        )
        assert h._should_auto_approve_spawn(builder, event) is True
        assert h._should_auto_approve_spawn(None, event) is False
