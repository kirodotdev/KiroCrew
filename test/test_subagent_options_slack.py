"""Test that OPTIONS tags in subagent-triggered responses render as Slack buttons."""

from __future__ import annotations

from kiro_crew.slack.format import (
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
    build_options_blocks,
    extract_options_with_recommendation,
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
        reply_text, options, _ = extract_options_with_recommendation(reply_text)

        assert options == ["Commit and raise CR", "Spawn adversarial review", "Just commit"]
        assert "[OPTIONS:" not in reply_text

    def test_options_blocks_appended_to_footer(self):
        """Extracted options should produce Block Kit checkboxes + Send button."""
        options = ["Choice A", "Choice B"]
        footer_blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "⏱ 5s"}]}]
        footer_blocks.extend(build_options_blocks(options))

        assert len(footer_blocks) == 2
        actions = footer_blocks[1]
        assert actions["type"] == "actions"
        assert actions["elements"][0]["type"] == "checkboxes"
        assert actions["elements"][0]["action_id"] == OPTIONS_CHECKBOXES_ACTION
        assert actions["elements"][1]["action_id"] == OPTIONS_SUBMIT_ACTION
        assert len(actions["elements"][0]["options"]) == 2

    def test_no_options_no_extra_blocks(self):
        """When response has no OPTIONS tag, footer should not get checkbox blocks."""
        response = "Here is my response with no options."
        _, options, _ = extract_options_with_recommendation(response)
        assert options == []

        footer_blocks = [{"type": "context", "elements": []}]
        if options:
            footer_blocks.extend(build_options_blocks(options))
        assert len(footer_blocks) == 1
