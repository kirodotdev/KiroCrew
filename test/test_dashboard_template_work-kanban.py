"""The ``work-kanban`` built-in, checked against a fold value a pod really served.

The template declares ``{"fold": ..., "path": ...}`` per field and the host resolves
each one at run time. :mod:`kiro_crew.dashboard_templates.manifest` validates a path's
SPELLING, never its target, so a plausible path that resolves to nothing would ship and
render a blank cell on every conductor's dashboard with every gate green. These cases
resolve all of them against ``test/fixtures/dashboard_templates/pod_folds.json`` -- the
eight folds a real pod session served -- so that failure lands here instead.

The board's own risk is different and is covered below too. Its four columns are not a
fold field: they are DERIVED from an item's ``state`` together with its worker's
``status``, because the column a conductor opens this page to find -- an item whose
worker reported ``done`` and which nothing has ruled on yet -- is named by neither
field alone. A state the product adds and this page has no column for would land in
"in progress" and read as work still moving, so the vocabulary is pinned against
:mod:`kiro_crew.work_vocab` rather than against this page's own list.
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
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
)

TEMPLATE_ID = "work-kanban"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
SAMPLE = FIXTURES / f"sample_{TEMPLATE_ID}.json"

#: The columns the page draws, in its own order. Mirrored here so the cases below can
#: say WHICH column a mapping sent an item to; the mapping itself is checked against
#: the product's vocabulary, not against this list.
COLUMNS = ("prog", "await", "acc", "shut")


def _folds(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {name: entry["value"] for name, entry in doc["folds"].items()}


def _walk(value: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted *path* the way the host's resolver must.

    Returns ``(found, value)``. ``found`` is False both for an absent key and for a
    path that tries to walk THROUGH a non-mapping, the two cases that each end in an
    empty cell and so must be reported rather than papered over.
    """
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _type_of(value: Any) -> str:
    # ``bool`` first: it passes an ``int`` check, and a flag reported as a number is
    # how a toggle ends up rendered as ``1``.
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

    Restated in Python for ONE purpose: asking whether a fixture exercises all four
    columns. It is not evidence that the page is right -- the page's JS is the only
    thing that draws -- so no case below uses it to judge the template's output.
    """
    state = item.get("state")
    if state == "accepted":
        return "acc"
    if state in ("rejected", "abandoned"):
        return "shut"
    return "await" if item.get("status") == "done" else "prog"


def _status_tones(script: str) -> dict[str, str]:
    """The page's ``STATUS_TONES`` table, as ``status -> theme variable expression``.

    Read out of the source because the rule it encodes is a COLOUR rule, and no fold
    value or rendered DOM is reachable from pytest. A status absent from the table is
    neutral, which is the table's whole point: it names the exceptions.
    """
    block = re.search(r"var STATUS_TONES = \{(.*?)\};", script, re.S)
    assert block, "the page declares no STATUS_TONES table"
    return dict(re.findall(r"(\w+)\s*:\s*'([^']+)'", block.group(1)))


@pytest.fixture(scope="module")
def loaded() -> tuple[Any, str]:
    try:
        return load_template(DIRECTORY)
    except ManifestError as exc:  # pragma: no cover - the failure path is the message
        pytest.fail(f"{TEMPLATE_ID}: {exc}")


@pytest.fixture(scope="module")
def script(loaded: tuple[Any, str]) -> str:
    """Just the page's own script, so a term found in prose is not read as code."""
    _, html = loaded
    blocks = re.findall(r"(?is)<script\b[^>]*>(.*?)</script\b[^>]*>", html)
    assert blocks, "the page carries no script, so nothing it shows is drawn in JS"
    return "\n".join(blocks)


class TestItLoads:
    def test_the_shared_loader_accepts_it(self, loaded: tuple[Any, str]) -> None:
        """``load_template`` is the one gate a user template also passes, so a built-in
        needing an exception to it is a built-in in the wrong format. It also enforces
        parity both ways, which is why no case here re-counts the bound elements."""
        manifest, html = loaded
        assert manifest.id == TEMPLATE_ID, (
            f"the manifest calls itself {manifest.id!r}; the registry discovers these "
            "by directory name, so the two must agree"
        )
        assert manifest.source == "builtin"
        assert manifest.version >= 1
        assert html.strip()

    def test_it_reads_only_the_work_fold(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        assert manifest.folds == frozenset({WORK_FOLD_NAME})

    def test_it_declares_no_agentic_field(self, loaded: tuple[Any, str]) -> None:
        """Every number on this board is folded from the record, so the page has no
        business marking any cell as something the agent wrote."""
        manifest, _ = loaded
        agentic = sorted(name for name, spec in manifest.fields.items() if spec.agentic)
        assert not agentic, f"{agentic} are agentic on a board that only reports the record"

    def test_it_stays_under_the_hosts_field_cap(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        assert len(manifest.fields) <= MAX_FIELDS


class TestEveryFoldPathResolves:
    """The case this module exists for."""

    def test_each_path_is_present_in_the_captured_fold(self, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        folds = _folds(POD)
        unresolved = []
        for spec in manifest.fields.values():
            assert spec.fold is not None and spec.path is not None
            found, _value = _walk(folds.get(spec.fold), spec.path)
            if not found:
                unresolved.append(f"{spec.name} -> {spec.fold}.{spec.path}")
        assert not unresolved, (
            "fold path(s) that resolve to nothing on a real session, so the cell is "
            f"blank at every render: {unresolved}"
        )

    def test_each_resolved_value_has_the_declared_type(self, loaded: tuple[Any, str]) -> None:
        """A path that resolves is not enough: the host type-checks the value, and a
        mismatch is a refusal rather than a wrong-looking cell."""
        manifest, _ = loaded
        folds = _folds(POD)
        wrong = []
        for spec in manifest.fields.values():
            assert spec.fold is not None and spec.path is not None
            found, value = _walk(folds.get(spec.fold), spec.path)
            if not found or value is None:
                # ``None`` is the ABSENCE of the declared type rather than another
                # type -- what a fold renders for something nobody wrote -- so it
                # cannot be judged against one here. Drawing it as a dash is the
                # page's job, which is why every value reaches the page through a
                # helper that answers for null.
                continue
            got = _type_of(value)
            if got != spec.type:
                wrong.append(
                    f"{spec.name} declares {spec.type!r} but "
                    f"{spec.fold}.{spec.path} served {got!r}"
                )
        assert not wrong, "; ".join(wrong)

    def test_a_wrong_path_is_actually_caught(self) -> None:
        """The planted failure. Without it, a resolver that answered "found" for
        everything would make both cases above pass over every field."""
        folds = _folds(POD)
        found, _ = _walk(folds[WORK_FOLD_NAME], "conductor.goal.text")
        assert not found, (
            "conductor.goal.text resolved, so the resolver walks through a string; "
            "the cases above would then pass on any path at all"
        )
        found, _ = _walk(folds[WORK_FOLD_NAME], "items.0")
        assert not found, "items.0 resolved; a dotted path cannot index a list"
        found, value = _walk(folds[WORK_FOLD_NAME], "conductor.goal")
        assert found and isinstance(value, str), "the control path did not resolve"


class TestThePageDrawsItself:
    def test_it_reaches_no_network(self, loaded: tuple[Any, str]) -> None:
        """The frame's CSP blocks network, so a page that fetches renders a hole --
        indistinguishable from a page whose data was empty, and the author of the next
        template copies whichever one is in the tree."""
        _, html = loaded
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_it_reads_the_hosts_own_field_bag(self, script: str) -> None:
        """The bound elements are filled by the host whether or not any script runs, so
        a page could pass parity while its charts drew from nothing."""
        assert "window.kirocrew" in script

    def test_it_draws_its_charts_as_inline_svg(self, script: str) -> None:
        """The strip and the per-round bars are the page's own SVG. A chart built from
        divs would still look like a chart and would stop being one the moment it had
        to carry a shape, so the element route is the thing asserted."""
        assert "createElementNS" in script
        assert "http://www.w3.org/2000/svg" in script

    def test_it_rerenders_when_the_host_refills(self, script: str) -> None:
        """The order of the host's first fill against this script is not the page's to
        assume, so it renders on load and again on every push."""
        assert "addEventListener('message'" in script.replace('"', "'")


class TestTheColumnVocabulary:
    """A column is derived, so the derivation is pinned to the product's own words."""

    @pytest.mark.parametrize("state", sorted(set(WORK_ITEM_STATES) - {"open"}))
    def test_every_closed_state_is_named_by_the_page(self, state: str, script: str) -> None:
        """``open`` is the fall-through and needs no mention; every other state must be
        named, or a board would file it under "in progress" and show closed work as
        still moving."""
        assert f"'{state}'" in script, (
            f"the page never names the {state!r} state, so an item in it lands in the "
            "fall-through column and reads as work still with a worker"
        )

    def test_the_awaiting_column_keys_off_a_real_worker_status(self, script: str) -> None:
        """The second column exists because ``state`` alone cannot say "waiting on the
        conductor". It keys off the worker's own ``done``, which must be a status the
        product actually lets a worker report."""
        assert "done" in WORK_WORKER_STATUSES
        assert "'done'" in script

    def test_only_a_status_that_wants_somebody_is_coloured(self, script: str) -> None:
        """Amber means "needs you" on this dashboard, so it cannot sit on a status that
        needs nobody. An accepted item's worker has also reported ``done``, so an amber
        ``done`` pill would make every accepted card read as a warning. ``blocked`` and
        ``question`` are the two statuses that ARE asking -- for an external dependency
        and for the conductor -- and they are the only coloured ones.
        """
        tones = _status_tones(script)
        assert set(tones) == {"blocked", "question"}, (
            f"worker statuses coloured: {sorted(tones)}; only blocked and question ask "
            "for anybody, and every other status must stay neutral"
        )
        for status in tones:
            assert status in WORK_WORKER_STATUSES, (
                f"{status!r} is coloured but is not a status the product lets a worker "
                "report, so the branch is dead"
            )
        assert "--danger" in tones["blocked"], "blocked waits on something and reads red"
        assert "--warn" in tones["question"], "question waits on the conductor, which is amber"

    def test_green_and_red_are_left_to_the_verdict_pill(self, script: str) -> None:
        """A closed card's reader is looking for the verdict, so the verdict pill owns
        the pass/fail colours. A status pill carrying them competes with it."""
        tones = _status_tones(script)
        assert not any("--ok" in tone for tone in tones.values()), (
            "a worker status is green; green is the verdict's, and a worker's claim is "
            "not an acceptance"
        )
        assert "verdict === 'pass'" in script

    def test_the_awaiting_column_colour_is_not_the_status_colour(self, script: str) -> None:
        """The COLUMN keeps amber, and that is not the same rule. A column holding items
        nobody has ruled on is the board's own "needs you", which is what amber is for.
        The pill rule is narrower: it withholds amber from a status that asks nobody.
        """
        columns = dict(
            (key, tone)
            for key, _label, tone in re.findall(r"\['(\w+)',\s*'([^']+)',\s*'([^']+)'\]", script)
        )
        assert "--warn" in columns["await"], (
            "the awaiting-verdict column lost its amber; items waiting on the "
            "conductor are exactly what the dashboard paints amber"
        )
        assert "--warn" not in columns["acc"] and "--warn" not in columns["prog"]

    def test_it_shows_a_verdict_verbatim_rather_than_through_a_table(self, script: str) -> None:
        """Five verdicts exist and more may. A page mapping them through a lookup drops
        the ones it does not know, so the verdict string is concatenated as it came and
        only the COLOUR branches, on ``pass`` alone -- everything that is not a pass is
        one colour, which needs no name."""
        assert "'verdict ' + verdict" in script
        compared = set(re.findall(r"verdict\s*===\s*'([a-z]+)'", script))
        assert compared <= {"pass"}, (
            f"the page branches on verdict(s) {sorted(compared - {'pass'})}; a verdict "
            "added to the product later would fall out of that branch unnoticed"
        )
        assert "pass" in WORK_VERDICTS

    def test_it_caps_each_column_and_says_what_it_held_back(self, script: str) -> None:
        """One board may carry ``WORK_STORED_ITEM_LIMIT`` items. Stacking those into
        four columns would bury the few that want the reader, so a column caps -- and
        a cap that stayed silent would be the page lying about the board's size."""
        match = re.search(r"PER_COLUMN\s*=\s*(\d+)", script)
        assert match, "the page declares no per-column cap"
        per_column = int(match.group(1))
        assert 0 < per_column < WORK_STORED_ITEM_LIMIT
        assert "more in this column" in script


class TestTheShowcaseFixture:
    """The sample capture the showcase screenshots are made from.

    The pod capture is thin -- three items, none of them rejected and none awaiting a
    verdict -- so three of the four columns render empty and a screenshot of it proves
    nothing about them. The sample exists for that, which makes its own shape the thing
    to check: a sample that drifted into a shape no real fold produces would make the
    screenshots a picture of nothing.
    """

    def test_it_carries_the_same_item_shape_as_the_pod_capture(self) -> None:
        pod_items = _folds(POD)[WORK_FOLD_NAME]["items"]
        sample_items = _folds(SAMPLE)[WORK_FOLD_NAME]["items"]
        assert pod_items and sample_items
        expected = set(pod_items[0])
        for item in sample_items:
            assert set(item) == expected, (
                f"sample item {item.get('item_id')!r} differs from the captured shape: "
                f"{sorted(set(item) ^ expected)}"
            )

    def test_every_value_it_invents_is_one_the_product_allows(self) -> None:
        for item in _folds(SAMPLE)[WORK_FOLD_NAME]["items"]:
            assert item["state"] in WORK_ITEM_STATES
            assert item["status"] is None or item["status"] in WORK_WORKER_STATUSES
            assert item["verdict"] is None or item["verdict"] in WORK_VERDICTS

    def test_it_fills_all_four_columns(self) -> None:
        """Otherwise the showcase shot shows an empty column and the board's hardest
        case -- the one waiting on the conductor -- goes undemonstrated."""
        items = _folds(SAMPLE)[WORK_FOLD_NAME]["items"]
        drawn = {_column_of(item) for item in items}
        assert drawn == set(COLUMNS), f"columns with no sample item: {sorted(set(COLUMNS) - drawn)}"

    def test_it_holds_an_item_the_fold_dropped(self) -> None:
        """The page prints ``omitted`` beside the list. A sample with nothing omitted
        screenshots that sentence as a zero and never shows it working."""
        assert _folds(SAMPLE)[WORK_FOLD_NAME]["omitted"] > 0

    def test_it_spans_more_than_one_round(self) -> None:
        """The round rows are the board's grouping; one round draws one bar."""
        rounds = {item["round"] for item in _folds(SAMPLE)[WORK_FOLD_NAME]["items"]}
        assert len(rounds) > 1, f"the sample carries one round: {rounds}"
