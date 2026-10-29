"""Slack mrkdwn conversion: the heading and mermaid patterns keep their output.

Both patterns were rewritten so no run of whitespace can be claimed by two parts of
one pattern (CodeQL ``py/polynomial-redos``). These pin what each still converts.
"""

from __future__ import annotations

from kiro_crew.slack import format as slack_format


def test_a_mermaid_block_with_blank_lines_before_the_diagram_still_converts():
    out = slack_format.to_slack_mrkdwn("```mermaid  \n\n\ngraph TD\nA[Start] --> B[End]\n```")
    assert "```mermaid" not in out
    assert "Start" in out and "End" in out


def test_an_unclosed_mermaid_block_of_blank_lines_is_left_as_written():
    text = "```mermaid" + "\n\t" * 2000
    assert slack_format._convert_mermaid(text) == text


def test_a_heading_becomes_bold_and_a_bare_hash_run_does_not():
    assert slack_format.to_slack_mrkdwn("##   Title here") == "*Title here*"
    assert slack_format.to_slack_mrkdwn("#\t\tTabbed") == "*Tabbed*"
    # Only spaces after the hashes: nothing to bold, so the line stays as typed.
    assert slack_format.to_slack_mrkdwn("#   ") == "#   "
