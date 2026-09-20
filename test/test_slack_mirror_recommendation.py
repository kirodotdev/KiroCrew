"""Slack has no badge, so a mirrored recommendation must be restated in the message.

The dashboard-only producer rule tells an agent to MARK a chip instead of naming its
pick in prose, and the dashboard renders that marker as a badge. Slack renders no badge
and the label is stripped before dispatch, so without a substitute a dashboard turn
mirrored into a linked Slack thread shows identical buttons under prose that never says
which one is recommended -- worse than before the marker existed, when the raw marker at
least appeared in the label.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.slack import format as fmt
from kiro_crew.slack.format import build_options_blocks, extract_options_with_recommendation

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _texts(blocks: list[dict]) -> str:
    out = []
    for block in blocks:
        for element in block.get("elements", []):
            if isinstance(element, dict) and element.get("type") == "mrkdwn":
                out.append(element.get("text", ""))
            for option in (element.get("options") or []) if isinstance(element, dict) else []:
                out.append(option.get("text", {}).get("text", ""))
    return "\n".join(out)


class TestReadingTheMarkerBeforeItIsStripped:
    def test_it_reports_the_cleaned_marked_label(self):
        text = "Body.\n[OPTIONS: (recommended) Merge it now | Hold it]"
        assert extract_options_with_recommendation(text)[2] == "Merge it now"

    def test_a_trailing_marker_is_not_read(self):
        # Not admitted: no producer emits one, so it stays as ordinary label text and
        # there is no recommendation to restate.
        text = "Body.\n[OPTIONS: Merge it now (recommended) | Hold it]"
        assert extract_options_with_recommendation(text)[2] is None

    def test_an_unmarked_list_reports_nothing(self):
        assert (
            extract_options_with_recommendation("Body.\n[OPTIONS: Merge it now | Hold it]")[2]
            is None
        )

    def test_no_options_at_all_reports_nothing(self):
        assert extract_options_with_recommendation("Just prose.")[2] is None

    def test_first_wins_when_the_agent_marks_more_than_one(self):
        text = "Body.\n[OPTIONS: (recommended) First | (recommended) Second]"
        assert extract_options_with_recommendation(text)[2] == "First"

    def test_a_guarded_label_reports_nothing_because_nothing_was_stripped(self):
        # The strip declines it, so the marker is still visible in the label itself and
        # restating it would double up.
        text = "Body.\n[OPTIONS: (recommended) /clear | Hold it]"
        assert extract_options_with_recommendation(text)[2] is None


class TestTheMirrorRestatesItAboveTheButtons:
    def test_the_recommendation_is_rendered(self):
        blocks = build_options_blocks(["Merge it now", "Hold it"], recommended="Merge it now")
        assert "*Recommended:* Merge it now" in _texts(blocks)

    def test_it_sits_above_the_controls(self):
        blocks = build_options_blocks(["Merge it now", "Hold it"], recommended="Merge it now")
        kinds = [b.get("type") for b in blocks]
        assert kinds.index("context") < kinds.index("actions")

    def test_the_dispatched_value_never_carries_the_marker(self):
        # The restatement is display only: what a click sends must stay the clean label.
        blocks = build_options_blocks(["Merge it now", "Hold it"], recommended="Merge it now")
        values = [
            option["value"]
            for block in blocks
            for element in block.get("elements", [])
            for option in (element.get("options") or [])
        ]
        assert values == ["Merge it now", "Hold it"]

    def test_an_unmarked_turn_adds_no_line(self):
        blocks = build_options_blocks(["Merge it now", "Hold it"])
        assert "Recommended:" not in _texts(blocks)
        assert [b.get("type") for b in blocks] == ["actions"]


class TestTheRestatedLabelCannotFireASlackEntity:
    """The restatement is the only mrkdwn field here, so it is the only one Slack parses.

    A label is model-authored and reachable by injected external content, and the strip
    happily returns `<!channel>` because `<` is not a reserved dispatch sigil. The
    checkbox label is `plain_text` and the button `value` is echoed back to the session,
    so neither may be escaped -- which is exactly why escaping belongs at this sink.
    """

    ENTITIES = ["<!channel>", "<!here>", "<@U024BE7LH>", "<!everyone>"]

    @pytest.mark.parametrize("entity", ENTITIES)
    def test_the_raw_entity_never_reaches_the_mrkdwn_field(self, entity):
        blocks = build_options_blocks([entity, "Hold it"], recommended=entity)
        mrkdwn = "\n".join(
            element["text"]
            for block in blocks
            for element in block.get("elements", [])
            if isinstance(element, dict) and element.get("type") == "mrkdwn"
        )
        assert mrkdwn, "no mrkdwn field found -- did the block move?"
        assert entity not in mrkdwn, mrkdwn
        assert "&lt;" in mrkdwn, mrkdwn

    def test_the_ampersand_is_escaped_first(self):
        # Escaping `<` before `&` would double-escape into `&amp;lt;`. The marked label is
        # ALSO a choice, which is the only shape that renders a restatement.
        blocks = build_options_blocks(["Tom & <!here>"], recommended="Tom & <!here>")
        text = blocks[0]["elements"][0]["text"]
        assert "&amp;lt;" not in text, text
        assert "&amp; &lt;!here&gt;" in text, text

    def test_the_dispatched_value_is_left_unescaped(self):
        # The value is read back as the user's message, so an escaped entity would
        # change the answer they picked.
        blocks = build_options_blocks(["<!channel> deploy"], recommended="<!channel> deploy")
        values = [
            option["value"]
            for block in blocks
            for element in block.get("elements", [])
            for option in (element.get("options") or [])
        ]
        assert values == ["<!channel> deploy"]


class TestTheMirrorPathIsActuallyWiredToIt:
    """Building the block is not the same as the mirror sending it.

    The reported regression is the WIRING: the mirror can strip the marker and post
    buttons with no substitute while every block-level assertion above still passes.
    """

    def _mirror_region(self) -> str:
        source = (SRC / "dashboard" / "chat_runner.py").read_text(encoding="utf-8")
        start = source.index("mirror response to linked Slack thread")
        return source[start : start + 4000]

    def test_the_region_is_found_at_all(self):
        # Guards the guard: a moved region would make both assertions below vacuous.
        region = self._mirror_region()
        assert "build_options_blocks(" in region
        assert "extract_options_with_recommendation(" in region

    def test_it_reads_the_marker_in_the_same_parse_as_the_strip(self):
        region = self._mirror_region()
        assert "extract_options_with_recommendation(" in region

    def test_it_passes_the_recommendation_into_the_blocks(self):
        region = self._mirror_region()
        call = region[region.index("build_options_blocks(") :]
        assert "recommended=" in call[:400], call[:400]


class TestTheTrailerIsParsedExactlyOnce:
    """The two readers must not drift, which is what a second parse would allow."""

    VECTORS = [
        "body\n[OPTIONS: (recommended) Ship it | Hold]",
        "body\n[OPTIONS: Ship it | (recommended) Hold]",
        "body\n[OPTIONS: (recommended) /deploy | Hold]",
        "body\n[OPTIONS: (recommended)]",
        "body\n[OPTIONS: Ship it | Hold]",
        "body with no trailer at all",
        "body\n[OPTIONS:  | (recommended) Hold |  ]",
    ]

    @pytest.mark.parametrize("text", VECTORS)
    def test_the_parse_is_self_consistent(self, text):
        cleaned, choices, recommended = fmt.extract_options_with_recommendation(text)
        assert fmt.extract_options_with_recommendation(text) == (cleaned, choices, recommended)

    def test_there_is_no_second_two_value_spelling(self):
        # The two-value wrapper is gone, so a seam cannot drop the recommendation by
        # picking the narrower call: there is nothing narrower left to pick.
        assert not hasattr(fmt, "extract_options")

    def test_the_module_searches_the_trailer_once(self):
        source = (SRC / "slack" / "format.py").read_text(encoding="utf-8")
        assert source.count("_OPTIONS_RE.search") == 1, "the trailer is parsed twice again"
