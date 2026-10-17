"""The ``flow`` built-in, checked against a fold value a pod really served.

Fold paths first, for the reason every module in this family states: the manifest format
validates a path's SPELLING and never its target, so a plausible path that resolves to
nothing ships and renders a blank cell forever with every gate green.

This page's own risk is that both charts are COMPUTED rather than read. Nothing in the
record is a burndown series; the page reconstructs one by asking, at each of a fixed
number of samples across the board's own window, how many items were open then. Three
ways that silently produces a plausible wrong picture, each covered below:

* **Sampling.** One point per event gives two boards of the same length different
  resolutions, so neither can be read against the other. The step count is fixed.
* **The stack.** A cumulative flow whose bands are drawn in the wrong order puts the
  band that should stay flat on top of the one that should grow, which inverts the only
  thing the chart says. The order is pinned, finished at the bottom.
* **The window.** An equal or inverted window divides by zero, and a chart drawn from
  that is a flat line at the wrong height rather than an error.

The charts are hand-drawn SVG in the page's own script because the frame's CSP blocks the
network, so a chart library would render as a hole rather than as a failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template
from kiro_crew.work_vocab import WORK_ITEM_STATES

TEMPLATE_ID = "flow"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
SAMPLE = FIXTURES / "sample_work-kanban.json"

#: The stack, bottom up. Mirrored so a case can say which band moved; the page's own
#: ``BANDS`` array is read out of its source and compared against this.
BAND_ORDER = ("accepted", "rejected", "open")


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


@pytest.fixture(scope="module")
def bands(html: str) -> list[tuple[str, str]]:
    """The page's own ``BANDS`` rows, in its own order, read out of its source."""
    block = re.search(r"var BANDS = \[(.*?)\];", html, re.S)
    assert block, "the page declares no BANDS array, so its stack order cannot be read"
    rows = re.findall(r"\[\s*'([a-z]+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\]", block.group(1))
    assert rows, f"no BANDS row parsed out of {block.group(1)!r}"
    return [(key, label) for key, label, _colour in rows]


class TestItLoads:
    def test_the_shared_loader_accepts_it(self, loaded: tuple[Any, str]) -> None:
        manifest, page = loaded
        assert manifest.id == TEMPLATE_ID
        assert manifest.source == "builtin"
        assert page.strip()
        assert len(manifest.fields) <= MAX_FIELDS

    def test_it_declares_no_agentic_field(self, loaded: tuple[Any, str]) -> None:
        """Both series are reconstructed from stamps the work fold writes.

        An agentic number in a trend chart is the worst case of the whole format: a
        reader reads a chart as the record, and nobody has to keep a computed one up to
        date.
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
        assert not _walk(folds["work"], "conductor.entries.count")[
            0
        ], "work.conductor.entries.count resolved, so the resolver walks through an int"
        assert not _walk(folds["work"], "conductor.generation_id")[
            0
        ], "work.conductor.generation_id resolved; the real key is generation"
        found, value = _walk(folds["work"], "conductor.generation")
        assert found and isinstance(value, str), "the control path did not resolve"


class TestTheSampling:
    def test_both_series_are_sampled_at_a_fixed_step_count(self, html: str) -> None:
        """Not one point per event.

        An event series has as many x positions as there were writes, so a quiet board
        and a busy one of the same duration draw at different resolutions and cannot be
        read against each other -- and the busy one looks more detailed rather than
        just noisier.
        """
        assert re.search(r"var STEPS = \d+;", html), (
            "the step count is not a named constant, so the two charts can sample at "
            "different resolutions"
        )
        assert (
            "at.push(lo + (width * s) / (STEPS - 1))" in html
        ), "the samples are not evenly spaced across the window"

    def test_an_item_is_counted_only_once_it_was_opened(self, html: str) -> None:
        """Counting it from the window's start would show work that did not exist yet,
        which flattens the opened line and makes the board look pre-planned."""
        assert "if (born === null || born > at[k]) continue;" in html

    def test_an_item_counts_as_finished_only_at_or_after_its_close(self, html: str) -> None:
        """A close compared the wrong way retires the item one sample early at every
        step, which bends the whole burndown down."""
        assert "closed !== null && closed <= at" in html

    def test_both_charts_share_one_y_scale(self, html: str) -> None:
        """Two scales make the burndown's last point and the flow's top band disagree
        about the same number on the same page."""
        assert html.count("function y(v)") == 1, (
            "the page defines more than one y scale, so its two charts can be drawn "
            "against different peaks"
        )


class TestTheBurndown:
    def test_it_draws_an_ideal_line_as_a_dashed_straight_line(self, html: str) -> None:
        """The pace the board would need. Without it the remaining line is a shape with
        nothing to be behind, and "are we behind" is the question."""
        assert "'stroke-dasharray': '4 3'" in html, "the ideal pace line is not dashed"
        assert "x1: 0, y1: y(first), x2: W, y2: y(0)" in html, (
            "the ideal line does not run from the first sample's remaining count to "
            "zero, so it is not a pace"
        )

    def test_the_last_point_is_marked(self, html: str) -> None:
        assert "svg('circle'" in html

    def test_the_axis_labels_name_the_counts_rather_than_only_the_clock(self, html: str) -> None:
        assert "' open'" in html


class TestTheCumulativeFlow:
    def test_the_page_stacks_the_bands_this_module_checks(
        self, bands: list[tuple[str, str]]
    ) -> None:
        assert tuple(key for key, _ in bands) == BAND_ORDER, (
            f"the page stacks {[k for k, _ in bands]}; finished must be at the BOTTOM "
            "and open at the top, or the band that should stay flat sits above the one "
            "that should grow and the chart says the opposite of what it means"
        )

    def test_every_band_carries_a_label_a_reader_can_act_on(
        self, bands: list[tuple[str, str]]
    ) -> None:
        labels = {key: label for key, label in bands}
        assert labels["rejected"] != "rejected" or "left" in labels["rejected"], (
            "the band holding rejected AND abandoned items is labelled as rejected "
            "only, so an abandoned item reads as one somebody ruled against"
        )

    def test_the_bands_cover_every_state_the_ledger_can_stamp(
        self, html: str, bands: list[tuple[str, str]]
    ) -> None:
        """No state may fall outside the stack.

        A state with no band is an item counted as opened and drawn in no band, so the
        stack's top stops matching the opened count and the chart loses items silently.
        """
        body = re.search(r"function bandOf\(it, at\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no bandOf()"
        source = body.group(1)
        assert "state === 'accepted' ? 'accepted' : 'rejected'" in source, (
            "a closed item is not sorted into accepted-or-not, so a closed item in a "
            "state the page does not name lands nowhere"
        )
        assert "return 'open';" in source, "an item that is not closed has no band"
        keys = {key for key, _ in bands}
        for state in WORK_ITEM_STATES:
            if state == "open":
                assert "open" in keys
            elif state == "accepted":
                assert "accepted" in keys
            else:
                assert "rejected" in keys, f"{state!r} has no band to land in"

    def test_each_band_is_drawn_from_the_previous_bands_top(self, html: str) -> None:
        """A stack whose bands all start at zero is three overlapping areas, and the
        tallest one hides the rest."""
        assert "top.push(base[q] + stacks[q][key])" in html
        assert "base = top;" in html

    def test_the_legend_names_the_latest_count_per_band(self, html: str) -> None:
        assert "stacks[at.length - 1][key]" in html


class TestTheUndrawableCases:
    def test_an_equal_or_inverted_window_refuses_to_draw_in_words(self, html: str) -> None:
        assert "hi <= lo" in html
        assert (
            "neither chart can be drawn" in html
        ), "the un-drawable case renders an empty plot rather than saying why"

    def test_an_empty_board_and_an_undrawable_one_say_different_things(self, html: str) -> None:
        assert "No item on this board yet." in html

    def test_a_zero_peak_cannot_divide_the_y_scale(self, html: str) -> None:
        """``peak`` is the divisor for every point on both charts."""
        assert "if (!peak) peak = 1;" in html

    def test_a_kpi_with_no_items_draws_a_dash_rather_than_zero(self, html: str) -> None:
        """Absent is not zero: a board nobody opened and a board with nothing accepted
        are different facts and 0 states the second."""
        assert "list.length ? done : null" in html
        assert "value === null ? DASH" in html


class TestItDrawsFromTheHost:
    def test_it_reaches_no_network(self, html: str) -> None:
        """The frame's CSP blocks network, so a chart library renders as a hole."""
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_the_charts_are_built_in_the_pages_own_script(self, html: str) -> None:
        assert "createElementNS" in html and "http://www.w3.org/2000/svg" in html

    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html

    def test_a_truncated_board_says_both_charts_are_drawn_on_a_part_of_it(self, html: str) -> None:
        """Every fold here is bounded. A trend chart over a truncated list is a trend
        over a sample, and a reader told the count can judge that."""
        assert 'data-dashboard-field="omitted"' in html
        assert "part of the board" in html

    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('message'" in html
