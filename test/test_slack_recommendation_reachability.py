"""The Slack recommendation line never names a choice the reader cannot reach.

A choice past the checkbox cap is pickable only by TYPING it, so the line telling the
reader to "reply with that option" is honest only while the option's full label is
somewhere on screen. The overflow listing packs into at most three blocks and drops the
tail with a count marker, so a recommendation naming a dropped choice pointed at nothing.
"""

from kiro_crew.slack.format import build_options_blocks


def _texts(blocks: list[dict]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        for element in block.get("elements", []):
            if isinstance(element, dict) and element.get("type") == "mrkdwn":
                out.append(element.get("text", ""))
    return out


def _recommendation_lines(blocks: list[dict]) -> list[str]:
    return [t for t in _texts(blocks) if t.startswith("*Recommended:*")]


class TestTheRecommendationNamesOnlyAReachableChoice:
    """Both arms, so the rule is pinned rather than the passing case."""

    def test_a_checkbox_choice_is_still_recommended(self):
        """Control: inside the checkbox cap nothing is dropped, so the line renders."""
        blocks = build_options_blocks(["Merge it now", "Skip it"], recommended="Merge it now")
        lines = _recommendation_lines(blocks)
        assert len(lines) == 1, _texts(blocks)
        assert "Merge it now" in lines[0]
        assert "reply with that option" not in lines[0]

    def test_a_packed_overflow_choice_is_recommended_and_says_how(self):
        """Control: in overflow but LISTED, so the reader can type it."""
        choices = [f"Choice number {n}" for n in range(1, 15)]
        blocks = build_options_blocks(choices, recommended=choices[-1])
        lines = _recommendation_lines(blocks)
        assert len(lines) == 1, _texts(blocks)
        assert "reply with that option to choose it" in lines[0]

    def test_a_dropped_overflow_choice_is_not_recommended_at_all(self):
        """The finding: past three packed blocks the label is nowhere to be read."""
        # Each choice is long enough that three ~2900-char blocks cannot hold them all,
        # so the tail is dropped with the count marker and its label never renders.
        choices = ["Keep this one"] + [f"{'z' * 900} option {n}" for n in range(1, 40)]
        blocks = build_options_blocks(choices, recommended=choices[-1])
        assert any("more choices omitted" in t for t in _texts(blocks)), _texts(blocks)[:2]
        assert _recommendation_lines(blocks) == [], (
            "the line names a choice whose label was dropped from the listing, so the "
            "reader is told to reply with text that appears nowhere on screen"
        )
