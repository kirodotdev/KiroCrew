"""The ``pr-watch`` built-in, checked against a fold value a pod really served.

The template declares ``{"fold": ..., "path": ...}`` per field and the host resolves
each one at run time. :mod:`kiro_crew.dashboard_templates.manifest` validates a path's
SPELLING, never its target, so a plausible path that resolves to nothing would ship and
render a blank cell on every dashboard with every gate green. These cases resolve all of
them against ``test/fixtures/dashboard_templates/pod_folds.json``.

This page's own risk is different and is covered below too. Its reason to exist is one
distinction -- a check that RAN and failed against a check that never started -- and
nothing in the manifest format can hold that apart: both are a string inside an agentic
array. So the page's own lane vocabulary is read out of its source and required to keep
the two separate, to carry an owner per lane, and to leave a green lane out of the strip
while still counting it. A page that collapsed them would pass every other gate here.

The lanes are the one agentic field, which is the honest shape: no fold reads a code
host, so a CI reading cannot come from the record, and a page that folded one would be
claiming the record said something it never said.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template

TEMPLATE_ID = "pr-watch"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
SAMPLE = FIXTURES / "sample_work-kanban.json"

#: The lane readings the page draws, and whether each one belongs in the strip. Mirrored
#: here so a case can name WHICH reading went missing; the page's source is the authority
#: and every case below reads it rather than this table.
LANE_READINGS = {"fail": True, "skipped": True, "running": True, "pass": False}


def _folds(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {name: entry["value"] for name, entry in doc["folds"].items()}


def _walk(value: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted *path* the way the host's resolver must.

    Returns ``(found, value)``. ``found`` is False both for an absent key and for a path
    that tries to walk THROUGH a non-mapping, the two cases that each end in an empty
    cell and so must be reported rather than papered over.
    """
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _type_of(value: Any) -> str:
    # ``bool`` first: it passes an ``int`` check, and a flag reported as a number is how
    # a toggle ends up rendered as ``1``.
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
def lane_table(html: str) -> list[tuple[str, str, bool]]:
    """The page's own ``LANE_STATES`` rows, read out of its source.

    Parsed rather than restated so these cases are about the shipped page. A row is
    ``['<reading>', '<label>', '<colour>', <in the strip>]``.
    """
    block = re.search(r"var LANE_STATES = \[(.*?)\];", html, re.S)
    assert block, "the page declares no LANE_STATES array, so its lane vocabulary cannot be read"
    rows = re.findall(
        r"\[\s*'([a-z]+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*(true|false)\s*\]",
        block.group(1),
    )
    assert rows, f"no LANE_STATES row parsed out of {block.group(1)!r}"
    return [(reading, label, flag == "true") for reading, label, _colour, flag in rows]


class TestItLoads:
    def test_the_shared_loader_accepts_it(self, loaded: tuple[Any, str]) -> None:
        """``load_template`` is the one gate a user template also passes, so a built-in
        that needs an exception to it is a built-in in the wrong format."""
        manifest, page = loaded
        assert manifest.id == TEMPLATE_ID
        assert manifest.source == "builtin"
        assert page.strip()
        assert len(manifest.fields) <= MAX_FIELDS

    def test_it_declares_exactly_one_agentic_field_and_it_is_the_lanes(
        self, loaded: tuple[Any, str]
    ) -> None:
        """The lane reading is the half no fold can serve, and the ONLY half.

        With a second agentic field the page stops being the record's own view of the
        work beside one written reading, and a reader cannot tell which numbers came
        from where.
        """
        manifest, _ = loaded
        agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
        assert agentic == ["lanes"], (
            f"{TEMPLATE_ID} declares {agentic} as agentic; exactly 'lanes' must be, "
            "because no fold reads a code host and everything else here has one"
        )

    def test_it_reads_the_work_and_approvals_folds(self, loaded: tuple[Any, str]) -> None:
        """The page's non-agentic half has to come from the record, not from the lanes.

        Without this a page could declare `lanes` and nothing else and still look like a
        PR board, with every item, PR number and human ask also agent-written.
        """
        manifest, _ = loaded
        assert {"work", "approvals"} <= set(manifest.folds), (
            f"{TEMPLATE_ID} reads {sorted(manifest.folds)}; the items and the human asks "
            "must come from the work and approvals folds rather than from the agent"
        )


class TestEveryFoldPathResolves:
    """The case this module exists for."""

    @pytest.mark.parametrize("fixture", [POD, SAMPLE], ids=lambda p: p.stem)
    def test_each_path_is_present(self, fixture: Path, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        folds = _folds(fixture)
        unresolved = [
            f"{spec.name} -> {spec.fold}.{spec.path}"
            for spec in manifest.fields.values()
            if spec.fold and not _walk(folds.get(spec.fold), spec.path)[0]
        ]
        assert not unresolved, (
            f"path(s) that resolve to nothing in {fixture.name}, so the cell is blank at "
            f"every render: {unresolved}"
        )

    def test_each_resolved_value_has_the_declared_type(self, loaded: tuple[Any, str]) -> None:
        """A path that resolves is not enough: the host type-checks the value, and a
        mismatch is a refusal rather than a wrong-looking cell."""
        manifest, _ = loaded
        folds = _folds(POD)
        wrong: list[str] = []
        for spec in manifest.fields.values():
            if not spec.fold:
                continue
            found, value = _walk(folds.get(spec.fold), spec.path)
            # ``None`` is what a fold renders for a measurement nobody reported. It is
            # the ABSENCE of the declared type rather than another type, so it cannot be
            # judged against one here; the page draws it as a dash.
            if not found or value is None:
                continue
            got = _type_of(value)
            if got != spec.type:
                wrong.append(f"{spec.name} declares {spec.type!r} but served {got!r}")
        assert not wrong, "; ".join(wrong)

    def test_a_wrong_path_is_actually_caught(self) -> None:
        """The planted failure. Without it, a resolver bug that answered "found" for
        everything would make the two cases above pass over every field."""
        folds = _folds(POD)
        assert not _walk(folds["approvals"], "pending.count")[0], (
            "approvals.pending.count resolved, so the resolver walks through a number "
            "and the cases above would pass on any path at all"
        )
        found, value = _walk(folds["approvals"], "pending")
        assert found and isinstance(value, int), "the control path did not resolve"


class TestTheLaneVocabulary:
    """The distinction the page exists for, read out of the page itself."""

    def test_a_failed_lane_and_a_lane_that_never_ran_are_separate_readings(
        self, lane_table: list[tuple[str, str, bool]]
    ) -> None:
        """The finding this whole page is for.

        A failed check needs a fix and a check that never started needs a re-run, so one
        red dot for both spends a reader's attention on nothing. The two must be distinct
        readings AND carry distinct labels: two readings rendered with the same word are
        the same dot with extra steps.
        """
        readings = {reading: label for reading, label, _ in lane_table}
        assert "fail" in readings, "the page has no reading for a check that FAILED"
        assert "skipped" in readings, "the page has no reading for a check that NEVER RAN"
        assert readings["fail"] != readings["skipped"], (
            f"both readings render as {readings['fail']!r}, so the page prints one word "
            "for a failed check and a check that never started"
        )
        assert "did not run" in readings["skipped"].lower(), (
            f"the never-ran label is {readings['skipped']!r}; it must say so in words, "
            "because a colour alone is what a reader has to be told how to read"
        )

    def test_a_green_lane_is_counted_but_kept_out_of_the_strip(
        self, lane_table: list[tuple[str, str, bool]]
    ) -> None:
        """Only the things in the way belong in the strip; the green count stays.

        Raymond's asks were for the red list with an owner each, never for the whole
        board, and a strip of ninety greens buries the one red inside it.
        """
        in_strip = {reading: flag for reading, _label, flag in lane_table}
        assert in_strip.get("pass") is False, "a green lane is listed in the strip"
        for reading in ("fail", "skipped", "running"):
            assert in_strip.get(reading) is True, f"{reading!r} is kept out of the strip"

    def test_every_reading_the_page_knows_is_one_this_module_lists(
        self, lane_table: list[tuple[str, str, bool]]
    ) -> None:
        """A reading added to the page without a case here would go unexamined."""
        assert {reading for reading, _, _ in lane_table} == set(LANE_READINGS)

    def test_an_unknown_reading_is_shown_rather_than_dropped(self, html: str) -> None:
        """A lane state the page has no row for must still reach the strip.

        A reading silently dropped reads as a clear board, which is the one answer that
        must never be produced by not knowing something.
        """
        fallback = re.search(r"return \[state, state, '[^']+', true\];", html)
        assert fallback, (
            "laneRow has no fallback row returning the unknown state as itself with the "
            "in-strip flag true, so an unrecognised lane state vanishes from the strip"
        )

    def test_each_lane_row_names_an_owner_even_when_none_was_written(self, html: str) -> None:
        """Who clears it was asked every single time, so an absent owner says so."""
        assert "owner not said" in html, (
            "a lane with no owner renders a blank chip; the page must say the owner was "
            "not written, which is a different fact from nobody owning it"
        )

    def test_one_prs_worst_lane_decides_its_chip(self, html: str) -> None:
        """A PR with nine greens and one red is red.

        Reporting the majority, or the last lane read, would call that PR green -- which
        is the failure mode of every board that aggregates by counting.
        """
        assert re.search(r"if \(s === 'fail'", html), (
            "the per-PR rollup does not special-case a failed lane, so a PR's chip can "
            "be decided by its greens"
        )


class TestTheVerdictLine:
    def test_it_carries_an_honest_no_word_yet_state(self, html: str) -> None:
        """A verdict with no silence state becomes a stale lie the moment the crewmate
        stops writing, which is the normal case on a box whose gateway restarts often."""
        assert "No word yet" in html, (
            "the verdict line has no state for 'the crewmate has not written a lane "
            "reading', so an unwritten board renders as a judgment nobody made"
        )

    def test_green_and_merged_are_not_the_same_word(self, html: str) -> None:
        """Five PRs the board called green carried unresolved change requests."""
        assert (
            "Green is not merged" in html or "not merged" in html
        ), "the all-green verdict does not distinguish checks-green from mergeable"

    def test_the_acceptance_verdict_is_labelled_apart_from_ci(self, html: str) -> None:
        """The ledger's verdict is an acceptance ruling, NOT a CI result.

        Rendered as a bare chip beside the lane states it would read as one more check,
        and a green board with a failed acceptance is exactly the case to see.
        """
        assert "'acceptance ' + verdict" in html, (
            "the item's acceptance verdict is drawn without a label separating it from "
            "the CI lane readings beside it"
        )


class TestItDrawsFromTheHost:
    def test_it_reaches_no_network(self, html: str) -> None:
        """The frame's CSP blocks network, so a page that fetches renders a hole.

        Checked on the SOURCE rather than left to the CSP: a template whose chart never
        appears is indistinguishable from one whose data was empty.
        """
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html

    def test_the_written_tag_is_drawn_from_the_hosts_mark_only(self, html: str) -> None:
        """A value arriving some other way must not borrow the label.

        The page is allowed to say "crewmate wrote this" exactly when the host says the
        field IS agentic; deciding from its own belief would let a folded value wear the
        tag, or a written one shed it.
        """
        assert "bag().agentic" in html, "the written tag is not gated on the host's mark"

    def test_the_mark_is_read_as_the_hosts_array_of_names(self, html: str) -> None:
        """``window.kirocrew.agentic`` is a LIST, not an object.

        :func:`kiro_crew.dashboard_frame.read_payload` puts ``sorted(agentic)`` there, so
        asking it for a PROPERTY named ``lanes`` answers no on every render and the tag
        never appears -- a page that reads it that way is gated on the host's mark and
        still always wrong, which is why the shape is pinned rather than the gating
        alone. The shipped ``session-ledger`` reads it with ``indexOf`` for this reason.
        """
        assert "agentic.indexOf('lanes')" in html, (
            "the agentic mark is not read by membership in the host's array; a property "
            "lookup on a list is false at every render"
        )
        assert "Array.isArray(agentic)" in html, (
            "the mark is used without checking it is an array, so a host that sent "
            "nothing would throw rather than draw no tag"
        )
        assert (
            "hasOwnProperty.call(agentic" not in html
        ), "the page still asks the agentic LIST for a property, which cannot answer"

    def test_the_payload_builder_really_serves_a_list(self) -> None:
        """The planted control for the case above, against the host's own builder.

        Without it, that case pins this page to a spelling rather than to the host's
        contract, and would keep passing if the host ever changed shape.
        """
        from kiro_crew.dashboard_frame import read_payload

        payload = read_payload({"lanes": []}, agentic=["lanes"])
        assert isinstance(payload["agentic"], list), (
            "the host's agentic mark is not a list, so this page's membership read is "
            "the wrong one for it"
        )
        assert payload["agentic"] == ["lanes"]

    def test_the_fields_bag_keeps_the_arrays_typed(self) -> None:
        """The page reads ``fields.lanes`` with ``Array.isArray``.

        The host stringifies a value only for the CELL (``text()`` in the bootstrap);
        ``fields`` itself holds the value the fold served. A page written against a
        stringified bag draws nothing while every cell still fills, which is the exact
        failure this case names.
        """
        from kiro_crew.dashboard_frame import read_payload

        payload = read_payload({"lanes": [{"name": "x", "state": "fail"}], "omitted": 2})
        assert isinstance(payload["fields"]["lanes"], list)
        assert isinstance(payload["fields"]["omitted"], int)

    def test_the_array_cells_are_overwritten_with_a_count(self, html: str) -> None:
        """The host binds by ``textContent``, so an array left alone reads as dumped
        JSON in the middle of a sentence."""
        for name in ("lanes", "items", "pending_requests"):
            assert f"{name}:" in html, f"no count is written over the {name!r} cell"

    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        """The order of the host's first fill against the script is not the page's to
        assume, and a later fold write arrives as a message."""
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('message'" in html
