"""The dashboard rules are the producer of the close-session chip's control tag.

The chip (`website/src/app-sdk/protocol/optionActionTag.ts`) renders only when a reply
ends with `<!-- option-action:close=<label> -->`. These tests pin that the agent is
taught to write one on the dashboard surface alone, and that the tag the rule shows is
one the backend strip removes, so a reply that follows the rule never reaches another
channel as raw text.
"""

import re

from kiro_crew import constants
from kiro_crew.context_assembly import output_contract as oc

_EXAMPLE = re.compile(r"<!-- option-action:close=[^<>\n]+ -->")


def test_the_dashboard_rules_teach_the_close_tag():
    assert _EXAMPLE.search(oc._CRITICAL_RULES)


def test_no_other_surface_is_taught_the_close_tag():
    assert "option-action" not in oc._CRITICAL_RULES_CHANNEL


def test_every_example_tag_in_the_rule_is_stripped_off_the_dashboard():
    examples = _EXAMPLE.findall(oc._DIFF_RULE_DASHBOARD)
    assert examples
    for tag in examples:
        reply = "Merged and deployed.\n\n[OPTIONS: Show the diff | Do nothing]\n" + tag
        stripped = constants.strip_control_comments(reply)
        assert "option-action" not in stripped
        assert stripped.rstrip().endswith("[OPTIONS: Show the diff | Do nothing]")
