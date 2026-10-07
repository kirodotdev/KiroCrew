"""The ``roadmap`` built-in, checked against a fold value a pod really served.

Fold paths first, for the reason every module in this family states: the manifest format
validates a path's SPELLING and never its target, so a plausible path that resolves to
nothing ships and renders a blank cell forever with every gate green.

This page's own risk is its TIME WINDOW, because every bar's position is computed against
it and a wrong window is not a wrong-looking bar -- it is a page that draws nothing at
all while every gate stays green. Two ways that happens, and both are covered: a window
taken from the clock instead of from the board pushes a board that ran yesterday off the
left edge, and a window whose ends are equal divides by zero and places every bar at the
same spot. The page is required to derive the window from the items' own stamps and to
refuse to draw, in words, when it cannot.

The second risk is the open bar. An item with no ``closed_at`` has no right-hand end, and
drawing it to its last report would make a live item look finished -- so an open item is
required to be drawn to now and marked as open rather than closed early.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template
from kiro_crew.work_vocab import WORK_ITEM_STATES

TEMPLATE_ID = "roadmap"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
SAMPLE = FIXTURES / "sample_work-kanban.json"


def _folds(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {name: entry["value"] for name, entry in doc["folds"].items()}


def _walk(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


@pytest.fixture(scope="module")
def loaded() -> tuple[Any, str]:
    try:
        return load_template(DIRECTORY)
    except ManifestError as exc:  # pragma: no cover - the failure path is the message
        pytest.fail(f"{TEMPLATE_ID}: {exc}")


@pytest.fixture(scope="module")
def html(loaded: tuple[Any, str]) -> str:
    return loaded[1]


class TestItLoads:
    def test_the_shared_loader_accepts_it(self, loaded: tuple[Any, str]) -> None:
        manifest, page = loaded
        assert manifest.id == TEMPLATE_ID
        assert manifest.source == "builtin"
        assert page.strip()
        assert len(manifest.fields) <= MAX_FIELDS

    def test_it_declares_no_agentic_field(self, loaded: tuple[Any, str]) -> None:
        """Every bar's position comes from a stamp the work fold writes.

        An agentic field here would be a date somebody typed, sitting in a chart a
        reader takes for the record -- and the one thing a timeline must not do is place
        a bar where nothing happened.
        """
        manifest, _ = loaded
        agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
        assert agentic == [], f"{TEMPLATE_ID} declares {agentic} as agentic"

    def test_it_reads_only_the_work_fold(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        assert set(manifest.folds) == {"work"}, f"reads {sorted(manifest.folds)}"


class TestEveryFoldPathResolves:
    @pytest.mark.parametrize("fixture", [POD, SAMPLE], ids=lambda p: p.stem)
    def test_each_path_is_present(self, fixture: Path, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        folds = _folds(fixture)
        unresolved = [
            f"{spec.name} -> {spec.fold}.{spec.path}"
            for spec in manifest.fields.values()
            if spec.fold and not _walk(folds.get(spec.fold), spec.path)[0]
        ]
        assert not unresolved, f"unresolved in {fixture.name}: {unresolved}"

    def test_each_resolved_value_has_the_declared_type(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        folds = _folds(POD)
        wrong: list[str] = []
        for spec in manifest.fields.values():
            if not spec.fold:
                continue
            found, value = _walk(folds.get(spec.fold), spec.path)
            if not found or value is None:
                continue
            got = _type_of(value)
            if got != spec.type:
                wrong.append(f"{spec.name} declares {spec.type!r} but served {got!r}")
        assert not wrong, "; ".join(wrong)

    def test_a_wrong_path_is_actually_caught(self) -> None:
        """The planted failure, so a resolver answering "found" for everything cannot
        make the cases above pass over every field."""
        folds = _folds(POD)
        assert not _walk(folds["work"], "conductor.round.number")[
            0
        ], "work.conductor.round.number resolved, so the resolver walks through an int"
        assert not _walk(folds["work"], "items.0")[0], (
            "work.items.0 resolved; the host's resolver walks mapping keys, not indices, "
            "so a path into a list element would be blank at every render"
        )
        found, value = _walk(folds["work"], "items")
        assert found and isinstance(value, list), "the control path did not resolve"


class TestTheTimeWindow:
    """Where every bar is placed, and the two ways that silently draws nothing."""

    def test_the_window_comes_from_the_items_own_stamps(self, html: str) -> None:
        """Not from the clock.

        A fixed last-N-hours window pushes a board that ran yesterday entirely off the
        left edge, and the page then renders a correct-looking empty track.
        """
        body = re.search(r"function render\(\) \{(.*)\n      \}", html, re.S)
        assert body, "the page has no render()"
        source = body.group(1)
        assert (
            "stamp(list[i].created_at)" in source and "if (a !== null && (lo === null" in source
        ), (
            "the window is not widened to each item's own open time, so an item outside "
            "the conductor's own entry span is drawn off the edge"
        )

    def test_an_equal_or_inverted_window_refuses_to_draw_in_words(self, html: str) -> None:
        """``hi <= lo`` divides by zero or goes negative, stacking every bar at one spot.

        A page that draws that stack looks like a board where everything happened at
        once, which is a claim about the work rather than about the page.
        """
        assert "hi <= lo" in html, "the page does not guard an equal or inverted window"
        assert "no bar can be placed" in html, (
            "the un-drawable case renders an empty track instead of saying why, so it "
            "reads as a board with no items"
        )

    def test_an_empty_board_and_an_undrawable_one_say_different_things(self, html: str) -> None:
        """Both end in no bars, and they are not the same fact: one means no work, the
        other means the work carries no usable stamps."""
        assert "No item on this board yet." in html
        assert "an open and a close time" in html, (
            "the un-drawable case does not name the missing stamps, so it reads the "
            "same as an empty board"
        )

    def test_the_ruler_is_drawn_from_the_same_window_as_the_bars(self, html: str) -> None:
        """A ruler on its own scale makes every bar read against the wrong labels."""
        assert re.search(r"var at = lo \+ \(width \* t\) / \(ticks - 1\)", html), (
            "the ruler's tick positions are not computed from lo and width, so its "
            "labels can disagree with where the bars sit"
        )

    def test_a_multi_day_window_labels_the_day(self, html: str) -> None:
        """Clock-only labels on a two-day board repeat, so two bars twenty-four hours
        apart read as sitting at the same time."""
        assert "multiDay" in html and "function day(" in html


class TestTheBars:
    def test_an_open_item_is_drawn_to_now_and_marked_open(self, html: str) -> None:
        """An item with no close has no right-hand end.

        Drawn to its last report it would end early and read as finished, which is the
        opposite of what the row says.
        """
        assert "var open = end === null;" in html
        assert "|| now;" in html, "an open item's bar is not extended to now"
        assert "'bar' + (open ? ' open' : '')" in html, "an open bar is not marked"
        assert "an arrow end means still open" in html, (
            "the legend never tells a reader what the open end means, so the marking is "
            "a convention only the author knows"
        )

    def test_a_bar_has_a_floor_width_so_a_short_item_is_visible(self, html: str) -> None:
        """An item that opened and closed in seconds computes to a sub-pixel width and
        disappears, which reads as an item that never existed."""
        assert re.search(
            r"Math\.max\(1\.2, right - left\)", html
        ), "a bar's width has no floor, so a fast item renders as nothing"

    def test_a_bar_is_clamped_into_the_track(self, html: str) -> None:
        assert "Math.max(0, pct(start))" in html and "Math.min(100, pct(end))" in html

    def test_an_item_with_no_open_stamp_is_named_rather_than_skipped(self, html: str) -> None:
        """Silently dropping it makes the row count disagree with the bar count, and the
        page's own footer prints the row count."""
        assert "no open time stamped" in html

    def test_every_state_the_ledger_can_stamp_has_a_colour(self, html: str) -> None:
        block = re.search(r"var STATES = \[(.*?)\];", html, re.S)
        assert block, "the page declares no STATES array"
        coloured = set(re.findall(r"\['([a-z]+)',", block.group(1)))
        missing = sorted(set(WORK_ITEM_STATES) - coloured)
        assert not missing, (
            f"{missing} have no colour, so bars in those states fall back to the neutral "
            "one and read as a state the page does not distinguish"
        )

    def test_an_unknown_state_still_gets_a_visible_colour(self, html: str) -> None:
        assert re.search(
            r"return '#[0-9a-fA-F]{6}';\s*\n\s*\}", html
        ), "colourFor has no fallback, so a new state renders with no fill"


class TestTheLanes:
    def test_items_are_grouped_by_their_own_round(self, html: str) -> None:
        assert "num(list[k].round)" in html

    def test_an_item_with_no_round_gets_its_own_lane_rather_than_round_zero(
        self, html: str
    ) -> None:
        """An unstamped round folded into the zeroth claims the item belongs to a
        round that may not exist, and zero sorts first so it leads the page."""
        assert "'none'" in html and "no round stamped" in html

    def test_the_unstamped_lane_sorts_last(self, html: str) -> None:
        assert re.search(r"if \(x === 'none'\) return 1;", html)


class TestItDrawsFromTheHost:
    def test_it_reaches_no_network(self, html: str) -> None:
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html

    def test_the_truncated_list_says_so(self, html: str) -> None:
        assert 'data-dashboard-field="omitted"' in html and "truncated" in html

    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('message'" in html
