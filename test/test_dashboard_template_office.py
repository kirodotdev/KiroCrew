"""The ``office`` built-in, checked against the fold values a pod and a sample serve.

Fold paths first, for the reason every module in this family states: the manifest format
validates a path's SPELLING and never its target, so a plausible path that resolves to
nothing ships and renders a blank cell forever with every gate green. This page reads
one fold, ``workstreams``, which the pod capture PREDATES -- so the paths are resolved
against ``sample_workstreams.json`` and against this page's own
``sample_office.json``, whose fold half is the former's with only the TEXT rewritten.

This page's own risks are three, and they are what most of this module is about.

The first is that its two lists are the SAME shape drawn from different facts. "Delivered
today" is the conductor's own verdict (``state == 'accepted'``) and "Waiting for your
review" is the writer's claim about itself (``state == 'open'`` with the worker reporting
``done`` or ``question``). Collapsing the two would show a crewmate's claim as a ruled
delivery, which is the one error a page about someone else's work must not make. So the
split is pinned against :mod:`kiro_crew.work_vocab` rather than against this page's list,
and a state or status the product adds has to land somewhere deliberate.

The second is the TYPE chip. Deck, mail, sheet and doc are not a fold's field: the
crewmate writes a type in its ``drafts`` entry, and absent one the page matches words out
of the title. A guess drawn in the same ink as a record would pass an inference off as the
record, so the two renderings are required to differ and the guess is required to carry
its own mark.

The third is the three answers a published value has. ``drafts`` and ``steps`` are the
crewmate's own, and a missing entry, an entry written as something this page cannot read,
and an entry written properly are three different facts -- only one of which is anybody's
fault. Each is required to have its own wording, because folding the unreadable case into
the absent one shows a broken write as silence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template
from kiro_crew.work_vocab import WORK_ITEM_STATES, WORK_VERDICTS, WORK_WORKER_STATUSES

TEMPLATE_ID = "office"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
POD = FIXTURES / "pod_folds.json"
WORKSTREAMS = FIXTURES / "sample_workstreams.json"
SAMPLE = FIXTURES / "sample_office.json"

#: The fold's own series cap, which the page sums over and must disclose. Read out of the
#: projection rather than retyped, so a cap change fails here instead of leaving the page
#: qualifying a figure with a number nobody keeps any more.
from kiro_crew.crew_log.projection import WORKSTREAMS_SERIES_LIMIT  # noqa: E402

#: The sections the page draws, and what sends a row to each.
SECTIONS = ("delivered", "review", "progress")


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


def _section(task: dict[str, Any]) -> str | None:
    """Which list the page puts *task* in, restated in Python.

    Restated for ONE purpose: asking which section each state and status in the product's
    vocabulary lands in. It is not evidence that the page is right -- the page's JS is the
    only thing that runs -- so the cases below also read the page's source and fail when
    the two drift.
    """
    state = task.get("state")
    status = task.get("status")
    if state == "accepted":
        return "delivered"
    if state == "open" and status in ("done", "question"):
        return "review"
    if state == "open":
        return "progress"
    return None


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

    def test_the_page_is_well_under_the_hosts_document_cap(self, html: str) -> None:
        """A megabyte is the host's ceiling; a page near it is a page the frame fights."""
        assert len(html.encode("utf-8")) < 1_000_000

    def test_only_the_drafts_and_steps_maps_are_agentic(self, loaded: tuple[Any, str]) -> None:
        """Both answer a question no fold has a field for, and nothing else here does.

        The deliverable's TYPE and FILE are facts about an artifact, and the workstreams
        fold records work, not artifacts. The STEP a writer is on inside an open board is
        not recorded anywhere -- the fold knows what is open, never what is being done in
        it. Every other value on this page is the record's, and a third agentic field
        would make the page a report the crewmate wrote about itself.
        """
        manifest, _ = loaded
        agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
        assert agentic == ["drafts", "steps"], f"{TEMPLATE_ID} declares {agentic} as agentic"

    def test_it_reads_the_crewmates_own_work_rather_than_one_session(
        self, loaded: tuple[Any, str]
    ) -> None:
        """``workstreams`` is slot-keyed, so it answers for the crewmate across every
        session it ran under. A session-keyed fold answers for a SLICE, and a reader of
        "delivered today" cannot tell a slice from the whole."""
        manifest, _ = loaded
        assert set(manifest.folds) == {"workstreams"}, (
            f"{TEMPLATE_ID} reads {sorted(manifest.folds)}; this page is about one "
            "crewmate's whole output and workstreams is the fold that carries it"
        )

    def test_the_manifest_states_every_derivation_the_page_makes(
        self, loaded: tuple[Any, str]
    ) -> None:
        """Four paths produce four sections, and none of the four sections is a path.

        The manifest description is where a reader learns what "delivered today" is
        computed from, and without it the only record of the derivation is the page's own
        JS -- which is not where someone deciding whether to trust the number looks.
        """
        manifest, _ = loaded
        text = manifest.description.lower()
        for phrase in (
            "delivered today",
            "waiting for your review",
            "in progress",
            "credits this week",
            "accepted",
            "48",
        ):
            assert phrase in text, f"the manifest never explains {phrase!r}"


class TestEveryFoldPathResolves:
    @pytest.mark.parametrize("fixture", [WORKSTREAMS, SAMPLE], ids=lambda p: p.stem)
    def test_each_path_is_present(self, fixture: Path, loaded: tuple[Any, str]) -> None:
        manifest, _ = loaded
        folds = _folds(fixture)
        unresolved = [
            f"{spec.name} -> {spec.fold}.{spec.path}"
            for spec in manifest.fields.values()
            if spec.fold and not _walk(folds.get(spec.fold), spec.path)[0]
        ]
        assert not unresolved, f"unresolved in {fixture.name}: {unresolved}"

    @pytest.mark.parametrize("fixture", [WORKSTREAMS, SAMPLE], ids=lambda p: p.stem)
    def test_each_resolved_value_has_the_declared_type(
        self, fixture: Path, loaded: tuple[Any, str]
    ) -> None:
        manifest, _ = loaded
        folds = _folds(fixture)
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

    def test_the_pod_capture_really_predates_this_fold(self) -> None:
        """Why the two fixtures above are samples rather than the capture.

        If the capture DID carry ``workstreams``, resolving against a written value would
        be substituting a sample for a recording, and it would look like nothing at all
        because both resolve.
        """
        assert "workstreams" not in _folds(POD), (
            "the pod capture now carries workstreams, so this module should resolve "
            "against it rather than against the samples"
        )

    def test_a_wrong_path_is_actually_caught(self) -> None:
        """The planted failure, so a resolver answering "found" for everything cannot
        make the cases above pass over every field."""
        folds = _folds(SAMPLE)
        assert not _walk(folds["workstreams"], "items.length")[
            0
        ], "workstreams.items.length resolved, so the resolver walks through a list"
        assert not _walk(folds["workstreams"], "last.entry_at")[
            0
        ], "workstreams.last.entry_at resolved; the real key is last_entry_at"
        found, value = _walk(folds["workstreams"], "items")
        assert found and isinstance(value, list), "the control path did not resolve"


class TestTheSampleIsTheFoldsOwnShape:
    """``sample_office.json`` is written data, so what it may differ in is bounded.

    Its whole worth is that the office page is screenshotted against the shape the fold
    really serves. A sample that drifted in STRUCTURE would certify a page the product
    never serves, and the drift would be invisible -- every path would still resolve.
    """

    @staticmethod
    def _shape(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: TestTheSampleIsTheFoldsOwnShape._shape(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [TestTheSampleIsTheFoldsOwnShape._shape(v) for v in value]
        return _type_of(value)

    def test_its_fold_half_has_the_same_keys_and_types_as_the_capture_it_came_from(self) -> None:
        mine = _folds(SAMPLE)["workstreams"]
        theirs = _folds(WORKSTREAMS)["workstreams"]
        assert self._shape(mine) == self._shape(theirs), (
            "sample_office.json's workstreams value is not the same shape as "
            "sample_workstreams.json's, so the office screenshots are taken against a "
            "structure the fold does not serve"
        )

    def test_every_number_state_and_stamp_came_through_untouched(self) -> None:
        """Only TEXT was rewritten. A sample that also moved a count or a stamp would be
        a second invention standing where a recorded figure should be."""
        mine = _folds(SAMPLE)["workstreams"]
        theirs = _folds(WORKSTREAMS)["workstreams"]
        assert mine["series"] == theirs["series"]
        assert mine["last_entry_at"] == theirs["last_entry_at"]
        assert mine["omitted"] == theirs["omitted"]
        for a, b in zip(mine["items"], theirs["items"]):
            for key in (
                "id",
                "total",
                "accepted",
                "open",
                "needs_you",
                "credits",
                "last_activity_at",
                "round",
            ):
                assert a[key] == b[key], f"board {a['id']}.{key} was changed"
            for x, y in zip(a["tasks"], b["tasks"]):
                for key in (
                    "item_id",
                    "state",
                    "status",
                    "verdict",
                    "credits",
                    "last_report_at",
                    "duration_ms",
                ):
                    assert x[key] == y[key], f"task {x['item_id']}.{key} was changed"

    def test_it_leaves_one_review_row_and_one_board_unwritten(self) -> None:
        """The sample has to exercise the FALLBACK path too.

        A sample where the crewmate wrote everything screenshots only the happy half, and
        the page's own answer for "nobody wrote this" then ships unlooked-at.
        """
        doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
        drafts, steps = doc["agentic"]["drafts"], doc["agentic"]["steps"]
        fold = _folds(SAMPLE)["workstreams"]
        review = [
            t["item_id"] for b in fold["items"] for t in b["tasks"] if _section(t) == "review"
        ]
        assert review, "the sample has no review row at all"
        assert [r for r in review if r not in drafts], (
            "every review row in the sample carries a drafts entry, so no screenshot "
            "shows the guessed type and the absent file name"
        )
        boards = [b["id"] for b in fold["items"] if b["open"] > 0]
        assert [b for b in boards if b not in steps], (
            "every in-flight board in the sample carries a step, so no screenshot shows "
            "the page falling back to the open tasks' own titles"
        )


class TestTheTwoListsAreDifferentFacts:
    """The split this page exists to keep: a ruling versus a claim."""

    def test_delivered_is_the_conductors_verdict_and_not_the_workers_claim(self) -> None:
        accepted = {"state": "accepted", "status": "done"}
        claimed = {"state": "open", "status": "done"}
        assert _section(accepted) == "delivered"
        assert _section(claimed) == "review", (
            "a task whose worker reported done is shown as delivered, so the page "
            "presents the crewmate's claim about itself as a ruled delivery"
        )

    def test_the_page_keys_delivered_on_accepted_alone(self, html: str) -> None:
        body = re.search(r"function delivered\(now\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no delivered() function"
        source = body.group(1)
        assert "'accepted'" in source, "delivered() does not key on the accepted state"
        for claim in ("'done'", "'progress'", "'question'"):
            assert claim not in source, (
                f"delivered() reads the worker's own {claim} status, so a claim can "
                "reach the delivered list"
            )

    def test_the_page_keys_the_review_queue_on_an_open_claim(self, html: str) -> None:
        body = re.search(r"function waiting\(\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no waiting() function"
        source = body.group(1)
        for probe in ("'open'", "'done'", "'question'"):
            assert probe in source, f"waiting() never checks {probe}"
        assert "'accepted'" not in source, (
            "waiting() reads the accepted state, so something already ruled on can be "
            "asked about again"
        )

    @pytest.mark.parametrize("state", WORK_ITEM_STATES)
    @pytest.mark.parametrize("status", WORK_WORKER_STATUSES)
    def test_every_state_and_status_pair_lands_somewhere_deliberate(
        self, state: str, status: str
    ) -> None:
        """A state the product adds must not fall into a list by default.

        ``None`` is a deliberate answer here and the commonest one: a rejected or
        abandoned task is on no list, because this page is a desk and not a board -- a
        rejected draft is not delivered, is not waiting on the person, and is not being
        written.
        """
        where = _section({"state": state, "status": status})
        assert where is None or where in SECTIONS
        if state in ("rejected", "abandoned"):
            assert where is None, f"a {state} task appears in the {where!r} list"

    def test_a_rejected_draft_is_on_no_list_at_all(self, html: str) -> None:
        """The page's own source, so the restatement above cannot agree with itself.

        ``inFlight`` is keyed on a BOARD's open count rather than a task's state, so a
        rejected task cannot reach the progress list through it either.
        """
        body = re.search(r"function inFlight\(\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no inFlight() function"
        assert "num(b.open)" in body.group(1), (
            "inFlight() does not read the board's own open count, so what it lists is "
            "not what the record says is open"
        )

    def test_every_verdict_in_the_vocabulary_is_one_the_delivered_row_can_print(
        self, html: str
    ) -> None:
        """The row wears ``project-report``'s own result pill, and a row the conductor
        ruled on with no verdict recorded still says Accepted -- in the muted family,
        because the ruling is the record's and the verdict behind it is not there to
        show. Printing nothing would read as a row nobody ruled on."""
        body = re.search(r"function verdictPill\(task\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no verdictPill() function"
        source = body.group(1)
        assert "'Accepted'" in source and "'Rejected'" in source, (
            "the delivered row does not spell the two rulings the way project-report's "
            "result pill does"
        )
        assert (
            "'fail'" in source and "'pass'" in source
        ), "the pill does not read the acceptance evaluator's own verdict"
        assert (
            "var(--ok, #22c55e)" in source and "var(--danger, #ef4444)" in source
        ), "the pill's colours are not the theme's own ok and danger families"
        assert "pass" in WORK_VERDICTS  # guards the sample's own rows from drifting


class TestTheTypeChip:
    def test_a_type_nobody_wrote_is_drawn_nowhere_at_all(self, html: str) -> None:
        """A guess shown as a record is this page's own worst available failure, because
        the reader has no way to tell from the picture.

        So nothing is guessed: the tag carries the record or there is no tag at all.
        Drawing a guess in its own ink -- hollow, or marked with a trailing ``?`` -- asks
        the reader to know a convention before they can see which of the two a tag is,
        and the absent tag is the one rendering that needs no convention.
        """
        assert (
            "told: true" in html and "told: false" in html
        ), "typeOf() does not report whether anyone said what kind of thing this is"
        chip = re.search(r"function chip\(kind\) \{(.*?)\n      \}", html, re.S)
        assert chip, "the page has no chip() function"
        source = chip.group(1)
        assert "kind.told" in source, "the chip does not read whether anyone said so"
        assert re.search(r"if \(!kind\.told[^)]*\) return null;", source), (
            "chip() draws something for a type nobody wrote, so an inference is painted "
            "in the same place a record is"
        )
        assert (
            "'?'" not in source
        ), "chip() still marks a guess with a question mark, so it is still drawing one"

    def test_the_guess_the_page_used_to_make_is_gone_from_the_source(self, html: str) -> None:
        """The word lists that matched a kind out of a title are not kept unused.

        Dead data of exactly this sort is what a later reader restores by accident: the
        lists look like the page's own answer for a missing type, and nothing in the file
        says they are not wired to anything.
        """
        body = re.search(r"function typeOf\(task, draft\) \{(.*?)\n      \}", html, re.S)
        assert body
        for word in ("words", "indexOf", "hay"):
            assert word not in body.group(1), (
                f"typeOf() still reads {word!r}, so the page is matching a kind out of a "
                "title and something downstream can start drawing it again"
            )

    def test_the_four_kinds_are_the_ones_an_office_crewmate_makes(self, html: str) -> None:
        block = re.search(r"var TYPE_ORDER = \[(.*?)\];", html, re.S)
        assert block, "the page declares no TYPE_ORDER"
        kinds = re.findall(r"'([a-z]+)'", block.group(1))
        assert kinds == ["deck", "mail", "sheet", "doc"], f"the page's kinds are {kinds}"
        for label in ("'PPT'", "'MAIL'", "'XLS'", "'DOC'"):
            assert label in html, f"no chip prints {label}"

    def test_a_type_the_crewmate_wrote_is_only_honoured_if_it_names_a_real_kind(
        self, html: str
    ) -> None:
        """An unknown type must draw no tag, not render as itself.

        Printing it would put an arbitrary crewmate-written string in the slot a reader
        reads as one of four kinds.
        """
        body = re.search(r"function typeOf\(task, draft\) \{(.*?)\n      \}", html, re.S)
        assert body
        assert "hasOwnProperty.call(TYPES" in body.group(1), (
            "typeOf() does not check the written type against the four kinds, so any "
            "string the crewmate wrote reaches the chip"
        )


class TestThePublishedValuesHaveThreeStates:
    """Readable, never written, and written-but-unreadable. Three, not two."""

    def test_the_reader_reports_all_three(self, html: str) -> None:
        body = re.search(r"function written\(name\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no written() reader"
        source = body.group(1)
        for state in ("'absent'", "'unreadable'", "'ok'"):
            assert state in source, f"written() never answers {state}"

    def test_an_unreadable_write_is_said_out_loud_rather_than_drawn_as_silence(
        self, html: str
    ) -> None:
        """A crewmate's broken write and a crewmate that wrote nothing are different
        facts, and only one of them is anybody's fault. Each needs its own sentence."""
        assert "cannot read" in html, (
            "the page has no wording for a value the crewmate published that it cannot "
            "read, so a broken write renders exactly like silence"
        )
        assert "No file name written" in html
        assert "No step written" in html

    def test_a_row_with_no_entry_still_renders_from_the_record(self, html: str) -> None:
        """The crewmate's maps add to the record; they are never required by it. A page
        that needed them would be blank for every crewmate that has not written one."""
        assert "No file name written" in html, (
            "a review row with no drafts entry says nothing about the file it is "
            "missing, so an unwritten file name reads as a page that lost it"
        )
        assert "No step written. Open: " in html, (
            "an in-flight board with no step falls back to nothing, so the row shows a "
            "title and a bar and no word about what is being done"
        )

    def test_the_fallback_names_the_records_narrower_answer_as_such(self, html: str) -> None:
        """The open tasks' titles are not a step. Shown without a label they read as
        one, which would make the record answer a question it was never asked."""
        body = re.search(r"function drawInProgress\(now\) \{(.*?)\n      \}", html, re.S)
        assert body
        assert "txt(t.state) === 'open'" in body.group(1), (
            "the fallback does not list the tasks the record says are OPEN, so it names "
            "work that is already finished as what is being done now"
        )


class TestTheWeekFigure:
    def test_it_sums_only_the_last_seven_days(self, html: str) -> None:
        assert re.search(r"var WEEK_MS = 7 \* 24 \* 60 \* 60 \* 1000;", html), (
            "the week window is inlined at its use site, so the sum and the label can "
            "come to mean different spans"
        )
        body = re.search(r"function weekCredits\(now\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no weekCredits() function"
        assert "now - at > WEEK_MS" in body.group(1), (
            "weekCredits() does not drop buckets older than the window, so the figure "
            "labelled 'this week' is the whole series"
        )

    def test_it_discloses_that_the_series_is_capped(self, html: str) -> None:
        """The fold keeps a bounded number of hours with spend, so a sum over them is a
        FLOOR once it is sitting on the cap. A floor presented as a total is how a cost
        figure misleads a reader without being wrong anywhere on the page."""
        assert re.search(r"var SERIES_CAP = \d+;", html), "the cap is not a named constant"
        declared = int(re.search(r"var SERIES_CAP = (\d+);", html).group(1))
        assert declared == WORKSTREAMS_SERIES_LIMIT, (
            f"the page says the fold keeps {declared} hours with spend and it keeps "
            f"{WORKSTREAMS_SERIES_LIMIT}, so the figure is qualified with the wrong number"
        )
        assert "at least" in html, "a capped sum is not marked as a floor on the tile"
        assert "is a floor" in html, "the note does not explain why the figure is a floor"

    def test_the_all_time_total_is_printed_beside_it(self, html: str) -> None:
        """One number cannot stand for both, and the week figure alone invites the reader
        to treat it as what the crewmate has cost."""
        assert "function allTimeCredits" in html
        assert "All time across " in html

    def test_a_board_that_reports_no_cost_is_counted_as_such(self, html: str) -> None:
        assert "report no cost" in html, (
            "a board with no credits figure is silently dropped from the all-time total, "
            "so the total reads as complete when it is short"
        )


class TestTheProgressBar:
    def test_it_is_the_boards_accepted_over_total(self, html: str) -> None:
        """A per-task percentage is a number no fold carries. Inventing one would put the
        only made-up figure on the page right beside the real ones."""
        body = re.search(r"function drawInProgress\(now\) \{(.*?)\n      \}", html, re.S)
        assert body
        source = body.group(1)
        assert (
            "num(board.total)" in source and "num(board.accepted)" in source
        ), "the bar is not computed from the board's own counts"
        assert "done / total" in source

    def test_it_is_clamped_and_survives_a_zero_total(self, html: str) -> None:
        body = re.search(r"function drawInProgress\(now\) \{(.*?)\n      \}", html, re.S)
        assert body
        assert "Math.max(0, Math.min(100," in body.group(1), (
            "the bar width is not clamped, so a board whose accepted exceeds its total "
            "draws past the track"
        )

    def test_the_counts_are_printed_beneath_the_bar(self, html: str) -> None:
        """A bar with no numbers is a shape. The reader of this page is deciding whether
        to go and look, and two thirds of six is a different decision from two thirds of
        sixty."""
        assert "' / ' + total + ' accepted'" in html
        assert "does not count this board" in html, (
            "a board the record carries no counts for draws an empty bar, which reads as "
            "no progress rather than as no number"
        )


class TestTheButtonsFillTheChatBox:
    def test_the_page_posts_the_hosts_own_act_message(self, html: str) -> None:
        """The host fills the person's composer and the person presses send, so the page
        can offer a reply and can never speak as them."""
        assert "'kirocrew-dashboard:act'" in html, (
            "the buttons do not use the host's action path, so either they do nothing or "
            "they reach something this page should not"
        )
        assert "parent.postMessage" in html

    def test_it_never_sends_on_its_own(self, html: str) -> None:
        lowered = html.lower()
        for forbidden in (
            "<form",
            "fetch(",
            "xmlhttprequest",
            "navigator.sendbeacon",
            "<script src",
        ):
            assert forbidden not in lowered, f"the page reaches past the host via {forbidden!r}"

    def test_a_missing_host_is_survived(self, html: str) -> None:
        """The gallery and the screenshot harness have no parent to post to, and a throw
        there would stop the page's own render on the click."""
        assert re.search(r"catch \(e\) \{ /\* no host", html), (
            "act() does not survive a missing host, so one click in a harness kills the "
            "rest of the page"
        )

    def test_the_two_asks_get_different_primary_buttons(self, html: str) -> None:
        """A draft reported done needs a verdict; a draft whose writer asked something
        needs an answer. One shared label would hide which move the person owes."""
        assert "'Answer it'" in html and "'Approve it'" in html
        assert (
            "txt(task.status) === 'question'" in html
        ), "the primary button does not depend on which ask the row is"
        assert "'Revise'" in html

    def test_the_crewmate_can_name_the_primary_label_itself(self, html: str) -> None:
        """Which is how a page written in English offers a button in the person's own
        language, without the page carrying a language of its own."""
        assert "draft ? draft.action : null" in html

    def test_the_text_it_puts_in_the_box_names_the_draft(self, html: str) -> None:
        """A reply reading only "Approve it" is unanswerable in a chat with a crewmate
        running three boards."""
        body = re.search(r"function ref\(task\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no ref() function"
        source = body.group(1)
        assert "task.title" in source and "task.item_id" in source

    def test_a_picked_row_says_the_text_is_waiting_in_the_box(self, html: str) -> None:
        """The page cannot know whether the person pressed send, so it must not claim
        they did -- and Undo may only unfold the row, never unsay anything."""
        assert "In your chat box:" in html
        assert "'Undo'" in html


class TestItDrawsFromTheHost:
    def test_it_reaches_no_network(self, html: str) -> None:
        lowered = html.lower()
        for forbidden in ("<script src", "fetch(", "xmlhttprequest", "importscripts", "//cdn."):
            assert forbidden not in lowered, f"reaches the network via {forbidden!r}"

    def test_it_reads_the_hosts_own_field_bag(self, html: str) -> None:
        assert "window.kirocrew" in html

    def test_every_bound_cell_is_overwritten_with_a_readable_count(self, html: str) -> None:
        """The host fills each bound element with the value's raw JSON before any script
        runs, so an array left as bound renders as a wall of braces in a sentence."""
        for name in ("items", "omitted", "series", "drafts", "steps", "last_entry_at"):
            assert f"put('{name}'" in html, f"the {name!r} cell is left as the host filled it"

    def test_the_truncation_the_fold_reports_is_shown(self, html: str) -> None:
        assert 'data-dashboard-field="omitted"' in html
        assert "more not shown" in html

    def test_one_clock_read_serves_the_whole_render(self, html: str) -> None:
        """Four sections compare stamps against "now". Read per section, two of them can
        disagree about which day it is -- and a row then counts as delivered today in one
        list and not in another on the same page."""
        body = re.search(r"function render\(\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no render() function"
        source = body.group(1)
        assert source.count("Date.now()") == 1, (
            "render() reads the clock more than once, so its sections can disagree about "
            "what today is"
        )
        for call in (
            "drawHeadline(now)",
            "drawReview(now)",
            "drawDelivered(now)",
            "drawInProgress(now)",
        ):
            assert call in source, f"{call} is not passed the render's own clock read"

    def test_the_day_boundary_is_utc_on_both_sides(self, html: str) -> None:
        """The stamps on the record are UTC. Comparing them against a local-time day makes
        "delivered today" mean a different set of rows per reader timezone."""
        body = re.search(r"function sameUtcDay\(a, b\) \{(.*?)\n      \}", html, re.S)
        assert body, "the page has no sameUtcDay() helper"
        source = body.group(1)
        for part in ("getUTCFullYear", "getUTCMonth", "getUTCDate"):
            assert part in source, f"the day comparison does not use {part}"
        assert "getFullYear()" not in source and "getDate()" not in source

    def test_it_renders_on_load_and_on_every_message(self, html: str) -> None:
        assert "window.addEventListener('load', render)" in html
        assert "addEventListener('message'" in html

    def test_every_colour_falls_back_to_the_products_own(self, html: str) -> None:
        """The frame injects the theme variables, and the accent differs per theme (it is
        emerald on dark and indigo on light), so a hard-coded hex renders a colour the
        product never serves in one of them.

        Checked by REMOVING every ``var(--name, fallback)`` and looking at what hex is
        left, rather than by a lookbehind: a lookbehind only sees the character right
        before the hex, and in ``var(--text, #e4e4e7)`` that is a space, so it would
        report every legitimate fallback on the page as a bare literal.
        """
        stripped = re.sub(r"var\(\s*--[a-z0-9-]+\s*,[^()]*\)", "var(X)", html)
        bare = re.findall(r"#[0-9a-fA-F]{3,8}\b", stripped)
        assert not bare, f"colour literal(s) outside a var() fallback: {sorted(set(bare))}"
        # The control: the stripper must not simply be deleting every hex on the page, or
        # the assertion above would pass on a page written entirely in literals.
        assert re.search(r"var\(\s*--accent\s*,\s*#[0-9a-fA-F]{3,8}\s*\)", html), (
            "no var(--accent, #hex) fallback found, so the stripper above has nothing to "
            "strip and its emptiness proves nothing"
        )
