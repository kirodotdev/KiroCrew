"""Test that OPTIONS tags in subagent-triggered responses render as Slack buttons."""

from __future__ import annotations

from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_SEVERAL_ACTION,
    build_options_blocks,
    extract_options,
)


class TestSubagentOptionsExtraction:
    """OPTIONS tags in subagent-triggered LLM responses must be extracted and rendered."""

    def test_extract_options_from_subagent_response(self):
        """Regression: subagent completion responses had OPTIONS posted as plain text."""
        response = (
            "That subagent got confused.\n\n"
            "Want me to commit what we have and raise a CR?\n\n"
            "[OPTIONS: Commit and raise CR | Spawn adversarial review | Just commit]"
        )
        from kiro_crew.slack.format import to_slack_mrkdwn

        reply_text = to_slack_mrkdwn(response)
        reply_text, options = extract_options(reply_text)

        assert options == ["Commit and raise CR", "Spawn adversarial review", "Just commit"]
        assert "[OPTIONS:" not in reply_text

    def test_options_blocks_appended_to_footer(self):
        """Extracted options should produce one Block Kit button per choice."""
        options = ["Choice A", "Choice B"]
        footer_blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "⏱ 5s"}]}]
        footer_blocks.extend(build_options_blocks(options))

        assert len(footer_blocks) == 2
        actions = footer_blocks[1]
        assert actions["type"] == "actions"
        assert [el["action_id"] for el in actions["elements"]] == [
            f"{OPTIONS_ACTION_PREFIX}0",
            f"{OPTIONS_ACTION_PREFIX}1",
            OPTIONS_SEVERAL_ACTION,
        ]

    def test_no_options_no_extra_blocks(self):
        """When response has no OPTIONS tag, footer should not get option blocks."""
        response = "Here is my response with no options."
        _, options = extract_options(response)
        assert options == []

        footer_blocks = [{"type": "context", "elements": []}]
        if options:
            footer_blocks.extend(build_options_blocks(options))
        assert len(footer_blocks) == 1
