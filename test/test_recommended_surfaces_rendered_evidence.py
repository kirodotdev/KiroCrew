"""Rendered evidence for every surface that shows a recommended option.

A screenshot covers the dashboard badge only, and a reviewer working from a checkout
cannot open a binary anyway. The three non-dashboard surfaces are plain text, so their
evidence belongs here as the literal strings they emit -- readable in the diff, and
re-derived on every run rather than captured once and left to rot.

Slack caps a checkboxes element at ten, so choice eleven onward degrades to a numbered
list the reader answers by typing. When the recommended option is itself in that tail it
is NOT clickable and the copy has to say so, in two variants: a label long enough to be
cut cannot be replied to verbatim from the context line, so the cut arm points at the
full listing below instead.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from kiro_crew.slack.format import _CHECKBOX_TEXT_CAP, build_options_blocks

REPO = pathlib.Path(__file__).resolve().parents[1]


def _context_texts(blocks: list[dict]) -> list[str]:
    return [
        element["text"]
        for block in blocks
        if block.get("type") == "context"
        for element in block.get("elements", [])
        if element.get("type") == "mrkdwn"
    ]


class TestSlackContextBlock:
    """What Slack renders above the checkboxes."""

    def test_a_clickable_recommendation_carries_no_pointer(self):
        blocks = build_options_blocks(["Ship it", "Hold off"], recommended="Ship it")
        assert _context_texts(blocks) == ["*Recommended:* Ship it"]

    def test_absent_recommendation_renders_no_context_line(self):
        blocks = build_options_blocks(["Ship it", "Hold off"])
        assert not any("Recommended" in text for text in _context_texts(blocks))


class TestSlackOverflow:
    """Past the cap the named choice has no checkbox, so the line says how to pick it.

    The clause is earned whether or not the name is cut: the line cuts at the checkbox
    cap, but the full label still reads in the listing below, so "that option" resolves
    to text the reader can type back either way. Cutting the name is what makes the
    pointer necessary rather than optional -- a truncated name cannot be retyped from
    this line alone.
    """

    HINT = " — reply with that option to choose it"

    @staticmethod
    def _overflowed(recommended: str) -> list[str]:
        choices = [f"Choice number {i}" for i in range(1, 12)]
        choices.append(recommended)
        return _context_texts(build_options_blocks(choices, recommended=recommended))

    def test_an_uncut_overflow_recommendation_says_how_to_choose_it(self):
        texts = self._overflowed("Hold the release")
        assert texts[0] == f"*Recommended:* Hold the release{self.HINT}"

    def test_a_cut_overflow_recommendation_shows_the_capped_prefix(self):
        long_label = (
            "Hold the release until the staging fleet dashboards have been green for an hour"
        )
        assert len(long_label) > _CHECKBOX_TEXT_CAP
        texts = self._overflowed(long_label)
        assert texts[0] == f"*Recommended:* {long_label[:_CHECKBOX_TEXT_CAP]}...{self.HINT}"
        assert "..." in texts[0], "an unmarked mid-word stop reads as truncated by accident"
        assert long_label in texts[1], "the label past the cap still reads in the listing"

    def test_a_cut_overflow_recommendation_still_carries_the_pointer(self):
        """The truncated case is the one that CANNOT be retyped from this line."""
        long_label = "Hold the release " + "y" * 90
        assert len(long_label) > _CHECKBOX_TEXT_CAP
        texts = self._overflowed(long_label)
        assert texts[0].endswith(self.HINT), texts[0]
        assert long_label not in texts[0], "fixture wrong: expected the name to be cut"
        assert long_label in texts[1], "the full label must be readable in the listing"

    def test_the_cut_lands_at_the_shared_cap(self):
        long_label = "x" * (_CHECKBOX_TEXT_CAP + 20)
        texts = self._overflowed(long_label)
        assert texts[0] == f"*Recommended:* {'x' * _CHECKBOX_TEXT_CAP}...{self.HINT}"
        assert "x" * (_CHECKBOX_TEXT_CAP + 1) not in texts[0]


class TestDashboardBadgeCopy:
    """The badge is the one surface a screenshot covers; pin its copy instead."""

    @staticmethod
    def _en() -> dict:
        path = REPO / "website" / "src" / "i18n" / "locales" / "en.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_badge_key_ships_in_english(self):
        """Upper case, matching the decisions-panel sibling it is drawn to resemble.

        Same word in the same mono/11px/.06em form, so two casings read as two different
        things. The case lives in the CATALOG rather than in a ``uppercase`` class, because
        the recorded badge form forbids that token.
        """
        assert self._en()["components"]["followUpBar"]["recommended"] == "RECOMMENDED"

    def test_the_badge_renders_the_same_word_as_the_decisions_panel(self):
        """Two keys, one concept, and now one rendered form -- neither transforms case."""
        catalog = self._en()
        badge = catalog["components"]["followUpBar"]["recommended"]
        panel = _find_sibling_recommended(catalog, skip_parent="followUpBar")
        assert panel is not None, "the decisions panel key moved; re-point this test"
        assert badge.upper() == panel.upper()

    def test_the_badge_element_matches_the_decision_badge_form(self):
        source = (REPO / "website" / "src" / "components" / "FollowUpBar.tsx").read_text(
            encoding="utf-8"
        )
        badge = source[source.index("function ChipBadge") : source.index("function ChipBody")]
        # Mono means ONE thing now -- literal text a click dispatches -- so the badge, which
        # marks text the click does not send, wears the product's uppercase micro-label voice.
        assert "font-mono" not in badge
        assert "uppercase" in badge
        assert "followUpBar.recommended" in badge
        # The pill form this replaced, so a revert cannot pass by leaving the class list alone.
        for pill in ("rounded-full", "bg-bg-elevated"):
            assert pill not in badge, pill


def _find_sibling_recommended(node, *, skip_parent: str, parent: str = "") -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "recommended" and isinstance(value, str) and parent != skip_parent:
                return value
            found = _find_sibling_recommended(value, skip_parent=skip_parent, parent=key)
            if found is not None:
                return found
    return None


def test_every_shipped_locale_defines_the_badge_key():
    """A missing key renders the raw path in the badge, which is worse than English.

    The hand-authored English overlay is excluded deliberately: it is merged INTO the
    English catalogue for strings that have no upstream source, not served as a locale
    of its own, so the badge key belongs in the English catalogue and its absence from
    the overlay is correct rather than a gap.
    """
    locales = sorted(
        path
        for path in (REPO / "website" / "src" / "i18n" / "locales").glob("*.json")
        if path.name != "en.manual.json"
    )
    assert len(locales) == 13, f"expected the full catalogue set, saw {len(locales)}"
    missing = [
        path.name
        for path in locales
        if "recommended"
        not in json.loads(path.read_text(encoding="utf-8"))
        .get("components", {})
        .get("followUpBar", {})
    ]
    assert not missing, f"catalogues missing components.followUpBar.recommended: {missing}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
