"""The Slack OPTIONS path must not put the ``(recommended)`` marker on the wire.

A button's ``value`` is echoed back as the user's own message on submit, so a marker that
survives the extraction is submitted as though the user typed it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.slack.format import (
    build_options_blocks,
    extract_options_with_recommendation,
)

MARKED = "[OPTIONS: (recommended) Merge it now | Keep it open]"


def _button_values(choices: list[str]) -> list[str]:
    """Every ``value`` Block Kit would carry for *choices*."""
    values: list[str] = []
    for block in build_options_blocks(choices):
        for element in block.get("elements", []):
            for option in element.get("options", []):
                values.append(option["value"])
    return values


class TestTheMarkerNeverReachesASlackButton:
    def test_extraction_returns_the_clean_label(self):
        _body, choices, _ = extract_options_with_recommendation(MARKED)
        assert choices == ["Merge it now", "Keep it open"]

    def test_the_emitted_button_value_is_exactly_the_clean_label(self):
        _body, choices, _ = extract_options_with_recommendation(MARKED)
        assert _button_values(choices) == ["Merge it now", "Keep it open"]

    def test_the_sink_does_not_strip_because_every_producer_parses(self):
        """One strip, in the extractor -- and this is what keeps that safe.

        The sink does not strip: every ``build_options_blocks`` call site is fed from
        a marker-aware parse, so a caller that hands over an unparsed label reaches
        the button verbatim. This pins that, so a second strip is not added on a guess.
        """
        assert _button_values(["(recommended) Merge it now"]) == ["(recommended) Merge it now"]

    def test_every_option_producer_parses_through_the_extractor(self):
        """The mechanical link the single strip rests on.

        A PROPERTY, not a file list: any producer is welcome provided its choices came out
        of a marker-aware parse. Adding a producer that parses correctly passes; adding one
        that hands over unparsed labels fails, which is the event that would put a marker
        on a button.
        """
        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        # Any of these strips the marker, so a producer fed from one is safe.
        parsers = (
            "extract_options_with_recommendation",
            "split_options_trailer",
            "_split_backfill_options",
        )
        callers: dict[str, int] = {}
        for path in src.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            hits = text.count("build_options_blocks(") - text.count("def build_options_blocks(")
            if hits > 0:
                callers[path.relative_to(src).as_posix()] = hits
        assert callers, "no build_options_blocks call sites found -- re-anchor this scan"
        unparsed = [
            rel
            for rel in callers
            if not any(p in (src / rel).read_text(encoding="utf-8") for p in parsers)
        ]
        assert unparsed == [], (
            f"these OPTIONS producers build choices without a marker-aware parse: {unparsed}. "
            "Their labels reach the button verbatim, so the marker would be dispatched."
        )

    @pytest.mark.parametrize(
        "label",
        ["(recommended) /clear", "(recommended) @deploy", "(recommended) [Monitor wake]"],
    )
    def test_a_label_that_would_become_dispatchable_is_left_verbatim(self, label):
        assert _button_values([label]) == [label]

    def test_an_unmarked_label_is_untouched(self):
        assert _button_values(["Merge it now"]) == ["Merge it now"]
