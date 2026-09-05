"""The Slack OPTIONS path must not put the ``(recommended)`` marker on the wire.

A button's ``value`` is echoed back as the user's own message on submit, so a marker that
survives the extraction is submitted as though the user typed it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.slack.format import build_options_blocks, extract_options

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
    # Every file that builds OPTIONS blocks. Named rather than counted so the assertion
    # fires on a NEW producer and not on a refactor that moves a call within a file.
    PRODUCER_FILES = {
        "dashboard/chat_slack.py",
        "dashboard/handlers/messaging.py",
        "dashboard/chat_runner.py",
        "slack/gateway.py",
        "slack/handler.py",
    }

    def test_extraction_returns_the_clean_label(self):
        _body, choices = extract_options(MARKED)
        assert choices == ["Merge it now", "Keep it open"]

    def test_the_emitted_button_value_is_exactly_the_clean_label(self):
        _body, choices = extract_options(MARKED)
        assert _button_values(choices) == ["Merge it now", "Keep it open"]

    def test_the_sink_no_longer_strips_because_every_producer_parses(self):
        """One strip, in the extractor -- and this is what keeps that safe.

        The sink used to strip as well, defending against a producer that built choices
        without parsing them out of text. No such producer exists: every
        ``build_options_blocks`` call site is fed from ``extract_options``. A caller that
        hands over an unparsed label therefore reaches the button verbatim, and this
        pins that so the second strip is not re-added on a guess.
        """
        assert _button_values(["(recommended) Merge it now"]) == ["(recommended) Merge it now"]

    def test_every_option_producer_parses_through_the_extractor(self):
        """The mechanical link the single strip rests on.

        If a new call site builds choices without ``extract_options``, the marker would
        reach the button. Enumerated here so adding one fails rather than shipping.
        """
        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        callers: dict[str, int] = {}
        for path in src.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            hits = text.count("build_options_blocks(") - text.count("def build_options_blocks(")
            if hits > 0:
                callers[path.relative_to(src).as_posix()] = hits
        assert callers, "no build_options_blocks call sites found -- re-anchor this scan"
        # A declared SET rather than a count: a refactor moving a call inside a file passes,
        # while a NEW producer file fails -- the event that would put a marker on a button.
        assert set(callers) == self.PRODUCER_FILES, (
            f"OPTIONS producers changed: {sorted(set(callers) ^ self.PRODUCER_FILES)}. "
            "A new producer must parse its choices through extract_options."
        )
        for rel in callers:
            text = (src / rel).read_text(encoding="utf-8")
            assert "extract_options" in text, (
                f"{rel} builds OPTIONS blocks without extract_options -- its choices are "
                "unparsed, so the marker would reach the button value. Strip them."
            )

    @pytest.mark.parametrize(
        "label",
        ["(recommended) /clear", "(recommended) @deploy", "(recommended) [Monitor wake]"],
    )
    def test_a_label_that_would_become_dispatchable_is_left_verbatim(self, label):
        assert _button_values([label]) == [label]

    def test_an_unmarked_label_is_untouched(self):
        assert _button_values(["Merge it now"]) == ["Merge it now"]
