"""Tests for the assembled critical-rules blocks.

`_CRITICAL_RULES_TAIL` is shared by the dashboard and channel blocks, so a rule
appended to it reaches both surfaces. The recommended-option marker is NOT in that
tail: it is appended to the dashboard block alone, because only the dashboard paints
a badge. A channel session is therefore never told to emit the marker, and no channel
seam restates a recommendation. These tests pin that per-surface split, and pin that
splitting the terminator out of the shared tail dropped no rule and left no block
unterminated.
"""

from __future__ import annotations

TERMINATOR = "[END CRITICAL RULES]"

# Sentinels for rules that predate the marker rule. Each must survive on BOTH
# surfaces -- inserting the marker rule must not evict any of them.
SHARED_SENTINELS = (
    "[OPTIONS: Choice A | Choice B | Choice C]",
    "inside inline `code` backticks",
    "Write every option label in the USER's voice",
    "Every option must be SELF-CONTAINED",
    "Keep each option label SHORT",
)


def _blocks():
    from kiro_crew.context import _CRITICAL_RULES, _CRITICAL_RULES_CHANNEL

    return _CRITICAL_RULES, _CRITICAL_RULES_CHANNEL


class TestMarkerRuleSurfaceSplit:
    """Both blocks instruct the marker; each describes its own surface's handling."""

    def test_dashboard_block_instructs_the_marker(self):
        dashboard, _ = _blocks()
        assert "(recommended)" in dashboard
        assert "renders that marker as a badge" in dashboard

    def test_channel_block_does_not_instruct_the_marker(self):
        _, channel = _blocks()
        assert "(recommended)" not in channel

    def test_selector_routes_the_rule_by_runtime_source(self):
        from kiro_crew.context import _critical_rules_for

        assert "(recommended)" in _critical_rules_for(None, "dashboard")
        for source in ("slack", "cron", "subagent", "channel", None):
            assert "(recommended)" not in _critical_rules_for(None, source), source

    def test_marker_rule_falls_between_the_shared_rules_and_the_terminator(self):
        dashboard, _ = _blocks()
        assert (
            dashboard.index("Keep each option label SHORT")
            < dashboard.index("(recommended)")
            < dashboard.index(TERMINATOR)
        )


class TestBlockStructure:
    """Splitting the terminator out of the shared tail preserved the block."""

    def test_terminator_closes_each_block_exactly_once(self):
        for block in _blocks():
            assert block.count(TERMINATOR) == 1
            assert block.rstrip().endswith(TERMINATOR)

    def test_no_shared_rule_was_evicted(self):
        for block in _blocks():
            for sentinel in SHARED_SENTINELS:
                assert sentinel in block

    def test_shared_tail_is_emitted_once_per_block(self):
        from kiro_crew.context import _CRITICAL_RULES_TAIL

        for block in _blocks():
            assert block.count(_CRITICAL_RULES_TAIL) == 1

    def test_each_block_carries_only_its_own_diff_rule(self):
        from kiro_crew.context import _DIFF_RULE_CHANNEL, _DIFF_RULE_DASHBOARD

        dashboard, channel = _blocks()
        assert _DIFF_RULE_DASHBOARD in dashboard
        assert _DIFF_RULE_CHANNEL not in dashboard
        assert _DIFF_RULE_CHANNEL in channel
        assert _DIFF_RULE_DASHBOARD not in channel


RETRACTION = "do NOT mark any option"


class TestNoOrdinaryChannelTurnCarriesTheRetraction:
    """An ORDINARY channel turn has nothing to retract: the marker is instructed on the
    dashboard only, so the channel rule block never carries it and spending prompt tokens
    forbidding it would be wasted. A channel RESUME of a dashboard-origin session is the
    separate case -- restored history there does carry the instruction, so that path does
    retract it, gated on the resume variant.
    """

    def _turn(self, tmp_path, runtime_source: str) -> str:
        from kiro_crew.context import ContextBuilder
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        return builder.build_message(
            "carry on",
            is_new_session=False,
            session_key="chat-1-1",
            runtime_source=runtime_source,
        )[0]

    def test_the_scan_reaches_a_real_channel_turn(self, tmp_path):
        # Guards the guard: the absence asserted below must be measured on a built turn.
        assert "[RUNTIME]" in self._turn(tmp_path, "slack")

    def test_no_channel_turn_carries_the_retraction(self, tmp_path):
        for source in ("slack", "telegram", "discord", "cron"):
            assert RETRACTION not in self._turn(tmp_path, source), source

    def test_a_dashboard_turn_carries_it_no_more_than_before(self, tmp_path):
        assert RETRACTION not in self._turn(tmp_path, "dashboard")

    def test_the_channel_still_gets_its_diff_rule(self, tmp_path):
        # The sibling channel-only instruction shares the same branch, so removing the
        # retraction must not have taken it out too.
        assert "```diff" in self._turn(tmp_path, "slack")


class TestReservedDispatchPrefixesStayBracketed:
    """The frontend marker-strip guard refuses any label opening with a bracket. That is
    only sufficient while every reserved prefix the backend byte-matches is bracketed.
    """

    def test_every_reserved_dispatch_prefix_opens_with_a_bracket(self):
        from kiro_crew.constants import (
            SUBAGENT_BATCH_COMPLETION_PREFIX,
            SUBAGENT_COMPLETION_PREFIX,
        )
        from kiro_crew.dashboard.state import (
            CRON_NOTIFY_PREFIX,
            HOOK_CONTINUATION_RECOVERY_PREFIX,
            MONITOR_WAKE_PREFIX,
            SUBAGENT_SYNTHESIS_PREFIX,
        )

        reserved = (
            CRON_NOTIFY_PREFIX,
            HOOK_CONTINUATION_RECOVERY_PREFIX,
            MONITOR_WAKE_PREFIX,
            SUBAGENT_SYNTHESIS_PREFIX,
            SUBAGENT_COMPLETION_PREFIX,
            SUBAGENT_BATCH_COMPLETION_PREFIX,
        )
        assert reserved
        for prefix in reserved:
            assert prefix.startswith("["), prefix
