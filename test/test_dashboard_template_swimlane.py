"""The ``swimlane`` built-in, checked against a fold value a pod really served.

Fold paths first, for the reason every module in this family states: the manifest format
validates a path's SPELLING and never its target, so a plausible path that resolves to
nothing ships and renders a blank cell forever with every gate green.

Two risks of this page's own, and they are different kinds.

**The columns are derived, not folded.** The column a conductor opens this page to find
-- an item whose worker reported ``done`` and which nothing has ruled on yet -- is named
by neither ``state`` nor ``status`` alone, so the mapping is the page's own. A state the
product adds and this page has no column for would land in "in progress" and read as work
still moving, so the vocabulary is pinned against :mod:`kiro_crew.work_vocab` rather than
against this page's own list.

**The lane is the item's own round.** There is no epic field anywhere in the work fold, so
a lane labelled "epic" would be a grouping this page invented and a reader would take the
label for a record. ``round`` is what the fold stamps, so ``round`` is the lane -- and an
item with no round stamped gets a lane that says so instead of being folded into the
zeroth, which both claims a round it may not belong to and sorts first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template
from kiro_crew.work_vocab import (
    WORK_FOLD_NAME,
    WORK_ITEM_STATES,
    WORK_STORED_ITEM_LIMIT,
    WORK_WORKER_STATUSES,
)

TEMPLATE_ID = "swimlane"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
SAMPLE = FIXTURES / "sample_work-kanban.json"

#: The columns the page draws, in its own order. Mirrored so a case can say WHICH column
#: a mapping sent an item to; the mapping itself is checked against the vocabulary.
COLUMNS = ("prog", "await", "acc", "shut")


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


def _column_of(item: dict[str, Any]) -> str:
    """The column the page's own ``columnOf`` sends *item* to.

    Restated in Python for ONE purpose: asking which column each state and status in the
    product's vocabulary lands in. It is not evidence that the page is right -- the
    page's JS is the only thing that runs -- so the case below reads the page's source
    and fails when the two drift.
    """
    state = item.get("state")
    if state == "accepted":
        return "acc"
    if state in ("rejected", "abandoned"):
        return "shut"
    if item.get("status") == "done":
        return "await"
    return "prog"


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
def column_table(html: str) -> list[tuple[str, str]]:
    """The page's own ``COLUMNS`` rows, in its own order, read out of its source."""
    block = re.search(r"var COLUMNS = \[(.*?)\];", html, re.S)
    assert block, "the page declares no COLUMNS array, so its columns cannot be read"
    rows = re.findall(r"\[\s*'([a-z]+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\]", block.group(1))
    assert rows, f"no COLUMNS row parsed out of {block.group(1)!r}"
    return [(key, label) for key, label, _colour in rows]


class TestItLoads:
    def test_the_shared_loader_accepts_it(self, loaded: tuple[Any, str]) -> None:
        manifest, page = loaded
        assert manifest.id == TEMPLATE_ID
        assert manifest.source == "builtin"
        assert page.strip()
        assert len(manifest.fields) <= MAX_FIELDS

    def test_it_declares_no_agentic_field(self, loaded: tuple[Any, str]) -> None:
        """Every value here has a fold, including the lane the card sits in."""
        manifest, _ = loaded
        agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
        assert agentic == [], f"{TEMPLATE_ID} declares {agentic} as agentic"

    def test_it_reads_the_work_fold_the_product_registers(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        assert set(manifest.folds) == {WORK_FOLD_NAME}, f"reads {sorted(manifest.folds)}"


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
        assert not _walk(folds["work"], "conductor.depth.value")[
            0
        ], "work.conductor.depth.value resolved, so the resolver walks through an int"
        assert not _walk(folds["work"], "conductor.epic")[0], (
            "work.conductor.epic resolved; there is no epic field, which is precisely "
            "why this page's lane is the item's round"
        )
        found, value = _walk(folds["work"], "conductor.depth")
        assert found and isinstance(value, int), "the control path did not resolve"

    def test_no_item_in_either_fixture_carries_an_epic_field(self) -> None:
        """The reason the lane is the round.

        If an epic field appeared on an item, this page's whole grouping choice would be
        the wrong one and the lane label would be a worse answer than the record's own.
        """
        for fixture in (POD, SAMPLE):
            items = _folds(fixture)["work"]["items"]
            for item in items:
                assert "epic" not in item and "parent_item" not in item, (
                    f"{fixture.name} carries an epic or parent field on an item, so the "
                    "lane should group by that rather than by round"
                )


class TestTheFourColumns:
    def test_the_page_declares_the_columns_this_module_checks(
        self, column_table: list[tuple[str, str]]
    ) -> None:
        assert (
            tuple(key for key, _ in column_table) == COLUMNS
        ), f"the page's columns are {[k for k, _ in column_table]}, not {list(COLUMNS)}"

    def test_a_worker_claim_of_done_gets_its_own_column(self) -> None:
        """The column a conductor opens this page to find.

        An item still open whose worker reported done is waiting on the conductor's own
        verdict, and neither its state nor its status says that alone. Folded into
        "in progress" it tells the reader to wait for a worker that is finished.
        """
        assert _column_of({"state": "open", "status": "done"}) == "await"
        assert _column_of({"state": "open", "status": "progress"}) == "prog"

    def test_the_awaiting_column_says_whose_verdict_it_waits_on(
        self, column_table: list[tuple[str, str]]
    ) -> None:
        labels = {key: label for key, label in column_table}
        assert "verdict" in labels["await"].lower(), (
            f"the awaiting column is labelled {labels['await']!r}; it must name the "
            "verdict, because 'waiting' alone does not say who has to act"
        )

    @pytest.mark.parametrize("state", WORK_ITEM_STATES)
    def test_every_state_the_ledger_can_stamp_lands_in_a_named_column(self, state: str) -> None:
        column = _column_of({"state": state})
        assert column in COLUMNS
        if state == "accepted":
            assert column == "acc"
        elif state in ("rejected", "abandoned"):
            assert column == "shut", f"{state!r} is not shown as closed"

    @pytest.mark.parametrize("status", WORK_WORKER_STATUSES)
    def test_every_worker_status_on_an_open_item_lands_deliberately(self, status: str) -> None:
        column = _column_of({"state": "open", "status": status})
        assert column == ("await" if status == "done" else "prog")

    def test_the_page_names_every_closed_state_rather_than_only_rejected(self, html: str) -> None:
        """An abandoned item left out of the closed test falls through to "in progress"
        and reads as work still moving, which is the quietest wrong answer here."""
        body = re.search(r"function columnOf\(it\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no columnOf()"
        source = body.group(1)
        unhandled = [s for s in WORK_ITEM_STATES if s != "open" and f"'{s}'" not in source]
        assert not unhandled, f"columnOf() never names {unhandled}"

    def test_the_python_restatement_matches_the_pages_own_order(self, html: str) -> None:
        """The two are allowed to exist only while they agree.

        Each probe matches the page's CHECK rather than a bare literal, because a state
        name also appears as a return value and matching that would compare the wrong
        positions and read as a drift that is not there.
        """
        body = re.search(r"function columnOf\(it\) \{(.*?)\n      \}", html, re.S)
        assert body
        source = body.group(1)
        probes = ["state === 'accepted'", "state === 'rejected'", "it.status) === 'done'"]
        missing = [p for p in probes if p not in source]
        assert not missing, f"columnOf() does not check {missing}"
        positions = [source.index(p) for p in probes]
        assert positions == sorted(positions), (
            "the page checks accepted / rejected / done in a different order than the "
            "restatement above, so the two can disagree"
        )


class TestTheLanes:
    def test_the_lane_is_the_items_own_round(self, html: str) -> None:
        assert "num(list[i].round)" in html

    def test_an_item_with_no_round_gets_a_lane_that_says_so(self, html: str) -> None:
        """Folded into the zeroth round it claims a round it may not belong to, and
        zero sorts first so the invented lane leads the page."""
        assert "'none'" in html and "No round stamped" in html

    def test_the_newest_round_leads_and_the_unstamped_lane_sorts_last(self, html: str) -> None:
        assert re.search(r"if \(x === 'none'\) return 1;", html)
        assert "return Number(y) - Number(x);" in html, (
            "the lanes are not ordered newest round first, so a reader scrolls past "
            "finished rounds to reach the one in flight"
        )

    def test_each_lane_header_carries_its_own_fill_bar(self, html: str) -> None:
        """Per lane, not per board: a board-wide bar beside a per-lane header is a
        number about a different set than the cards under it."""
        assert "(n / group.length) * 100" in html


class TestTheCards:
    def test_a_card_shows_silence_rather_than_only_duration(self, html: str) -> None:
        """A closed item shows how long it took; an open one shows how long it has been
        quiet, and those are different questions with different answers."""
        assert "'took ' + ago(closed - born)" in html
        assert "'quiet ' + ago(silent)" in html

    def test_a_card_with_no_report_is_marked_rather_than_left_blank(self, html: str) -> None:
        assert "no report yet" in html
        assert re.search(r"q\.className = 'quiet late';", html)

    def test_the_quiet_threshold_is_a_named_constant(self, html: str) -> None:
        assert re.search(r"var QUIET_MS = \d+ \* \d+ \* \d+;", html)

    def test_a_card_carries_an_evidence_pointer(self, html: str) -> None:
        """A row that asserts without showing invites the follow-up it exists to avoid."""
        assert "it.artifacts" in html and "artifact(s)" in html

    def test_a_verdict_is_labelled_apart_from_the_column(self, html: str) -> None:
        """A verdict is the acceptance evaluator's answer and the column is the
        conductor's stamp; showing one hides a disagreement between them."""
        assert "text(it.verdict)" in html
        assert "num(it.fails)" in html

    def test_an_empty_column_draws_a_dash_rather_than_nothing(self, html: str) -> None:
        """A column with no header-level content collapses, and a collapsed column reads
        as one the board does not have."""
        assert "none.textContent = DASH;" in html


class TestItDrawsFromTheHost:
    def test_it_reaches_no_network(self, html: str) -> None:
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html

    def test_the_truncated_board_says_so(self, html: str) -> None:
        """The work fold stores a bounded number of items and reports what it dropped,
        so a board drawn from a part of it must not look like the whole one."""
        assert WORK_STORED_ITEM_LIMIT > 0
        assert 'data-dashboard-field="omitted"' in html and "truncated" in html

    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('message'" in html
