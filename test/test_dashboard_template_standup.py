"""The ``standup`` built-in, checked against a fold value a pod really served.

Fold paths first, for the reason every module in this family states: the manifest format
validates a path's SPELLING and never its target, so a plausible path that resolves to
nothing ships and renders a blank cell forever with every gate green. Every path here is
resolved against ``test/fixtures/dashboard_templates/pod_folds.json``.

This page's own risk is its FOUR buckets. "What changed since you last looked" is a
delta, and the split into finished / claimed-done / still-moving / stuck is derived from
an item's ``state`` together with its worker's ``status`` -- neither field names the
buckets alone, and the one a reader most needs (a worker said done, nothing has ruled on
it) is named by neither. A state the product adds and this page has no bucket for must
not land in "still moving" and read as healthy, so the bucketing is pinned against
:mod:`kiro_crew.work_vocab` rather than against this page's own list.

The second risk is the silence band. Every other element on the page shows WORK time, and
a worker that died 31 minutes ago looks identical to one that is thinking. The band is
the only element that answers that, so it is required to be computed from a write time
and to have an honest "no write time on the record" state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template
from kiro_crew.work_vocab import WORK_ITEM_STATES, WORK_VERDICTS, WORK_WORKER_STATUSES

TEMPLATE_ID = "standup"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
SAMPLE = FIXTURES / "sample_work-kanban.json"

#: The buckets the page draws, in its own order. Mirrored so a case can say WHICH bucket
#: an item went to; the mapping itself is checked against the product's vocabulary.
BUCKETS = ("done", "await", "new", "stuck")


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


def _bucket(item: dict[str, Any]) -> str:
    """The bucket the page's own ``bucket()`` sends *item* to.

    Restated in Python for ONE purpose: asking which bucket each state and status in the
    product's vocabulary lands in. It is not evidence that the page is right -- the
    page's JS is the only thing that runs -- so the case below that compares them reads
    the page's source and fails when the two drift.
    """
    state = item.get("state")
    status = item.get("status")
    if state == "accepted":
        return "done"
    if state in ("rejected", "abandoned"):
        return "stuck"
    if status in ("blocked", "question"):
        return "stuck"
    if item.get("fails"):
        return "stuck"
    if item.get("verdict") == "fail":
        return "stuck"
    if status == "done":
        return "await"
    return "new"


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

    def test_only_the_headline_is_agentic(self, loaded: tuple[Any, str]) -> None:
        """The crewmate's own one-sentence read is the one fact no fold records.

        Everything else on this page -- which items moved, how long the silence is, how
        many asks are waiting -- is folded, and a second agentic field would make the
        page a report the agent wrote rather than the record's own.
        """
        manifest, _ = loaded
        agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
        assert agentic == ["headline"], f"{TEMPLATE_ID} declares {agentic} as agentic"

    def test_it_reads_the_delta_sources_rather_than_only_the_board(
        self, loaded: tuple[Any, str]
    ) -> None:
        """A delta needs a time axis the board does not have.

        ``work`` says what the items ARE; ``timeline`` and ``status`` are what say what
        happened recently and when the last write was, which is the question.
        """
        manifest, _ = loaded
        assert {"work", "timeline", "status"} <= set(manifest.folds), (
            f"{TEMPLATE_ID} reads {sorted(manifest.folds)}; without timeline and status "
            "the page can only restate the board, which is not a delta"
        )


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
        assert not _walk(folds["timeline"], "moments.length")[
            0
        ], "timeline.moments.length resolved, so the resolver walks through a list"
        assert not _walk(folds["status"], "turns.completed")[
            0
        ], "status.turns.completed resolved; the real key is status.turns_completed"
        found, value = _walk(folds["timeline"], "moments")
        assert found and isinstance(value, list), "the control path did not resolve"


class TestTheFourBuckets:
    """The page's own derivation, against the product's vocabulary."""

    def test_the_page_declares_the_buckets_this_module_checks(self, html: str) -> None:
        block = re.search(r"var COLS = \[(.*?)\];", html, re.S)
        assert block, "the page declares no COLS array, so its buckets cannot be read"
        keys = re.findall(r"\[\s*'([a-z]+)'\s*,", block.group(1))
        assert tuple(keys) == BUCKETS, f"the page's buckets are {keys}, not {list(BUCKETS)}"

    def test_the_page_tests_every_state_the_ledger_can_stamp(self, html: str) -> None:
        """A state the product adds must be visible in the page's source.

        Every state except ``open`` is named in the bucketing, and ``open`` is the one
        that falls through -- so a NEW state would fall through with it and read as work
        still moving, which is why the vocabulary is the authority rather than this page.
        """
        body = re.search(r"function bucket\(it\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no bucket() function"
        source = body.group(1)
        unhandled = [s for s in WORK_ITEM_STATES if s != "open" and f"'{s}'" not in source]
        assert not unhandled, (
            f"bucket() never names {unhandled}, so item(s) in those states fall through "
            "to 'still moving' and a stopped board reads as a healthy one"
        )

    def test_a_worker_claim_of_done_is_its_own_bucket(self) -> None:
        """The bucket a reader opens this page to find.

        An item still open whose worker reported done is waiting on the conductor, and
        neither its state nor its status says that on its own. Collapsing it into
        "still moving" tells the reader to wait for a worker that is already finished.
        """
        item = {"state": "open", "status": "done"}
        assert _bucket(item) == "await"
        assert _bucket({"state": "open", "status": "progress"}) == "new"

    @pytest.mark.parametrize("status", WORK_WORKER_STATUSES)
    def test_every_worker_status_lands_somewhere_deliberate(self, status: str) -> None:
        bucket = _bucket({"state": "open", "status": status})
        assert bucket in BUCKETS
        if status in ("blocked", "question"):
            assert bucket == "stuck", f"a worker reporting {status!r} is not shown as stuck"

    def test_a_failed_acceptance_on_an_open_item_reads_as_stuck(self) -> None:
        """A verdict is not a state, so a failed acceptance leaves the item ``open``.

        Shown as "still moving" it looks like progress, while the fold's own record is
        that the acceptance evaluator refused it.
        """
        assert _bucket({"state": "open", "status": "progress", "verdict": "fail"}) == "stuck"
        assert _bucket({"state": "open", "status": "progress", "fails": 2}) == "stuck"

    def test_every_verdict_in_the_vocabulary_is_a_value_this_page_can_meet(self) -> None:
        """Guards the case above from drifting: it keys on ``fail`` by name."""
        assert "fail" in WORK_VERDICTS

    def test_the_python_restatement_matches_the_pages_own_order(self, html: str) -> None:
        """The two are allowed to exist only while they agree.

        The restatement above answers "which bucket", and it would answer confidently
        and wrongly the moment the page's own order changed, so the order is compared.
        Each probe matches the page's CHECK rather than a bare literal: ``'done'`` also
        appears as the accepted branch's return value, and matching that would compare
        the wrong five positions and read as a drift that is not there.
        """
        body = re.search(r"function bucket\(it\) \{(.*?)\n      \}", html, re.S)
        assert body
        source = body.group(1)
        probes = [
            "state === 'accepted'",
            "state === 'rejected'",
            "status === 'blocked'",
            "num(it.fails)",
            "status === 'done'",
        ]
        missing = [p for p in probes if p not in source]
        assert not missing, f"bucket() does not check {missing}"
        positions = [source.index(p) for p in probes]
        assert positions == sorted(positions), (
            "the page checks accepted / rejected / blocked / fails / done in a different "
            "order than the restatement above, so the two can disagree"
        )


class TestTheSilenceBand:
    def test_it_is_computed_from_a_write_time(self, html: str) -> None:
        """Silence, not work time. Durations on every other element are how long
        something TOOK, which a dead worker and a thinking one share."""
        assert "function drawQuiet" in html
        assert "Date.now() - last" in html, (
            "the band does not measure time since the last write, so it cannot tell a "
            "crewmate that is thinking from one that died"
        )

    def test_it_has_an_honest_state_for_no_write_time_at_all(self, html: str) -> None:
        """A band that silently draws nothing when it has no timestamp reads as "all
        good", which is the one answer absence must never produce."""
        assert "cannot say whether anything is still moving" in html

    def test_the_threshold_is_a_named_constant(self, html: str) -> None:
        assert re.search(r"var QUIET_MS = \d+ \* \d+ \* \d+;", html), (
            "the quiet threshold is inlined at its use site, so the page's two readers "
            "of it can drift apart"
        )

    def test_a_card_with_no_report_says_so_rather_than_showing_zero(self, html: str) -> None:
        assert "no report yet" in html, (
            "an item whose worker never reported renders a zero-length quiet time, which "
            "reads as a report that just arrived"
        )


class TestItDrawsFromTheHost:
    def test_it_reaches_no_network(self, html: str) -> None:
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html

    def test_the_written_tag_is_gated_on_the_hosts_agentic_mark(self, html: str) -> None:
        """``window.kirocrew.agentic`` is a LIST, not an object.

        :func:`kiro_crew.dashboard_frame.read_payload` puts ``sorted(agentic)`` there, so
        asking it for a PROPERTY named ``headline`` answers no on every render -- which
        would label every written headline as one nobody wrote. The shipped
        ``session-ledger`` reads it with ``indexOf`` for this reason.
        """
        assert (
            "agentic.indexOf('headline')" in html
        ), "the agentic mark is not read by membership in the host's array"
        assert "Array.isArray(agentic)" in html
        assert "hasOwnProperty.call(agentic" not in html
        assert "no headline written" in html, (
            "a page with no headline shows an empty line where a judgment belongs, "
            "which reads as a judgment of nothing rather than as none having been made"
        )

    def test_the_payload_builder_really_serves_a_list_and_typed_fields(self) -> None:
        """The planted control for the case above, against the host's own builder.

        Two halves, because the page depends on both: the mark is a list, and the field
        bag keeps a fold's array an ARRAY. A page written against a stringified bag
        draws nothing while every cell still fills.
        """
        from kiro_crew.dashboard_frame import read_payload

        payload = read_payload({"moments": [{"type": "turn/completed"}]}, agentic=["headline"])
        assert payload["agentic"] == ["headline"]
        assert isinstance(payload["fields"]["moments"], list)

    def test_the_truncated_feed_says_so(self, html: str) -> None:
        """Every fold here is bounded and reports what it dropped, so a partial picture
        is never shown as a whole one."""
        assert 'data-dashboard-field="moments_dropped"' in html
        assert "truncated" in html

    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('message'" in html
