"""Two action clicks merged into one turn keep BOTH payloads.

A busy slot's queue drains into a single turn. Reading only the first `action_context`
discarded every later click's payload, so a user who clicked two option buttons while the
agent was mid-turn lost the second one's context with no error anywhere. These exercise the
real assembly the drain uses.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.dashboard.chat_runner import pair_action_contexts


def _click(content: str, payload: str) -> dict[str, Any]:
    return {"content": content, "role": "user", "meta": {"action_context": payload}}


def _typed(content: str) -> dict[str, Any]:
    return {"content": content, "role": "user"}


class TestMergedActionClicksKeepEveryPayload:
    def test_both_payloads_survive_a_merge(self):
        got = pair_action_contexts([_click("Merge it now", "ALPHA"), _click("Skip it", "BETA")])
        assert got is not None
        assert "ALPHA" in got, "first payload lost"
        assert "BETA" in got, "LATER payload dropped -- this is the reported defect"

    def test_each_payload_names_the_label_it_arrived_with(self):
        """Surviving is not enough: a payload paired with the wrong label misleads."""
        got = pair_action_contexts([_click("Merge it now", "ALPHA"), _click("Skip it", "BETA")])
        assert got is not None
        assert got.index("Merge it now") < got.index("ALPHA") < got.index("Skip it")
        assert got.index("Skip it") < got.index("BETA")

    def test_a_third_click_is_not_silently_truncated(self):
        got = pair_action_contexts([_click("A", "ONE"), _click("B", "TWO"), _click("C", "THREE")])
        assert got is not None
        for payload in ("ONE", "TWO", "THREE"):
            assert payload in got, payload

    def test_a_lone_click_is_passed_through_unwrapped(self):
        """The ordinary one-click turn must not gain envelope text."""
        assert pair_action_contexts([_click("Merge it now", "ALPHA")]) == "ALPHA"

    def test_typed_entries_alongside_a_click_do_not_displace_it(self):
        got = pair_action_contexts([_typed("hello"), _click("Merge it now", "ALPHA")])
        assert got == "ALPHA"

    def test_no_click_carries_no_payload(self):
        assert pair_action_contexts([_typed("typed text")]) is None
        assert pair_action_contexts([]) is None

    def test_an_empty_payload_is_not_treated_as_present(self):
        """An empty string must not produce an envelope naming a payload that is not there."""
        assert pair_action_contexts([_click("Merge it now", "")]) is None
