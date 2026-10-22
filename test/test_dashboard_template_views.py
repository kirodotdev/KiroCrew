"""The ``timeline`` and ``org-chart`` built-ins, checked as the pair they are.

One module for two templates because their risks are shared: both read the same fold,
both key a row on the same ``spender`` alias, both decide a worker's SITUATION with the
same rule, and both would be wrong in the same way if that rule lost a distinction. A
pair of modules asserting the same invariant twice drifts; this one cannot.

Fold paths first, for the reason every module in this family states: the manifest format
validates a path's SPELLING and never its target, so a plausible path that resolves to
nothing ships and renders a blank cell forever with every gate green.

Then the four things that would be wrong SILENTLY -- a page that still looks right in
review:

1. **The situation rule.** Four readings of an open task that must not share a word:
   blocked or question needs a PERSON, a recent report means a worker is producing, an
   old report means idle, and NO report is an absence rather than a long silence. Fold
   them together and the page reads as a calm board while a worker is stuck.
2. **One bill per session.** The fold repeats a worker session's spend on each of its
   task rows, so a row or card that ADDS them multiplies one bill by the tasks it
   served. The number still looks like a number.
3. **The window and the absences.** A timeline computes every bar's position against a
   window; a wrong one draws nothing while every gate is green. And a task with no
   start stamp must be listed rather than dropped, or the page's own row count lies.
4. **No session key on the page.** The fold aliases a worker's session key on purpose,
   because this document is embedded in a page any dashboard caller can read. A page
   that reached for the key would undo that at the last step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import MAX_FIELDS, ManifestError, load_template

IDS = ("timeline", "org-chart")
ROOT = Path(__file__).resolve().parents[1]
BUILTIN = ROOT / "src/kiro_crew/dashboard_templates/builtin"
FIXTURES = ROOT / "test/fixtures/dashboard_templates"
#: The shared sample every ``workstreams`` reader's paths are checked against, and this
#: pair's own richer one -- five workers running at once, which the shared sample has
#: no reason to carry.
SAMPLES = (FIXTURES / "sample_workstreams.json", FIXTURES / "sample_timeline.json")


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
def pages() -> dict[str, tuple[Any, str]]:
    out: dict[str, tuple[Any, str]] = {}
    for tid in IDS:
        try:
            out[tid] = load_template(BUILTIN / tid)
        except ManifestError as exc:  # pragma: no cover - the failure path is the message
            pytest.fail(f"{tid}: {exc}")
    return out


# --------------------------------------------------------------------------- #
# it loads, and it reads what it says it reads
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tid", IDS)
def test_the_shared_loader_accepts_it(tid: str, pages: dict[str, tuple[Any, str]]) -> None:
    manifest, page = pages[tid]
    assert manifest.id == tid
    assert manifest.source == "builtin"
    assert page.strip()
    assert len(manifest.fields) <= MAX_FIELDS


@pytest.mark.parametrize("tid", IDS)
def test_it_declares_no_agentic_field(tid: str, pages: dict[str, tuple[Any, str]]) -> None:
    """Every mark on both pages has a fold behind it.

    An agentic field here would be a stamp, a role or a cost somebody typed, sitting in
    a chart a reader takes for the record. The one thing a timeline must not do is place
    a bar where nothing happened, and the one thing an org chart must not do is draw a
    reporting line nobody bound.
    """
    manifest, _ = pages[tid]
    agentic = sorted(n for n, f in manifest.fields.items() if f.agentic)
    assert agentic == [], f"{tid} declares {agentic} as agentic"


@pytest.mark.parametrize("tid", IDS)
def test_it_reads_only_the_workstreams_fold(tid: str, pages: dict[str, tuple[Any, str]]) -> None:
    manifest, _ = pages[tid]
    assert set(manifest.folds) == {"workstreams"}, f"{tid} reads {sorted(manifest.folds)}"


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.stem)
@pytest.mark.parametrize("tid", IDS)
def test_each_fold_path_resolves(tid: str, sample: Path, pages: dict[str, tuple[Any, str]]) -> None:
    manifest, _ = pages[tid]
    folds = _folds(sample)
    unresolved = [
        f"{spec.name} -> {spec.fold}.{spec.path}"
        for spec in manifest.fields.values()
        if spec.fold and not _walk(folds.get(spec.fold), spec.path)[0]
    ]
    assert not unresolved, f"unresolved in {sample.name}: {unresolved}"


@pytest.mark.parametrize("tid", IDS)
def test_each_resolved_value_has_the_declared_type(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    manifest, _ = pages[tid]
    folds = _folds(SAMPLES[1])
    wrong: list[str] = []
    for spec in manifest.fields.values():
        if not spec.fold:
            continue
        found, value = _walk(folds.get(spec.fold), spec.path)
        if not found or value is None:
            continue
        got = _type_of(value)
        if got != spec.type:
            wrong.append(f"{tid}: {spec.name} declares {spec.type!r} but served {got!r}")
    assert not wrong, "; ".join(wrong)


def test_a_wrong_path_is_actually_caught() -> None:
    """The planted failure, so a resolver answering "found" for everything cannot make
    the cases above pass over every field."""
    folds = _folds(SAMPLES[1])
    assert not _walk(folds["workstreams"], "items.0")[0], (
        "workstreams.items.0 resolved; the host's resolver walks mapping keys, not "
        "indices, so a path into a list element would be blank at every render"
    )
    assert not _walk(folds["workstreams"], "omitted.count")[
        0
    ], "omitted.count resolved, so the resolver walks through an int"
    found, value = _walk(folds["workstreams"], "items")
    assert found and isinstance(value, list), "the control path did not resolve"


# --------------------------------------------------------------------------- #
# the fixture is the one the pages were designed against
# --------------------------------------------------------------------------- #


def test_the_sample_carries_every_case_the_pages_tell_apart() -> None:
    """MUTATION-SENSITIVE: a fixture that lost a case would make the shots prove less.

    Each page's whole worth is a distinction, and a screenshot of a fixture with no
    blocked task, no silent worker and no concurrency shows a page that happens to look
    fine. These are the cases the committed shots are evidence FOR.
    """
    value = _folds(SAMPLES[1])["workstreams"]
    tasks = [t for b in value["items"] for t in b["tasks"]]

    statuses = {t["status"] for t in tasks}
    assert {"blocked", "question", "progress", "done"} <= statuses, statuses
    states = {t["state"] for t in tasks}
    assert {"open", "accepted", "rejected", "abandoned"} <= states, states

    # An open task with NO report at all, which is not a long silence.
    assert any(t["state"] == "open" and not t["last_report_at"] for t in tasks)
    # A task with no START stamp: the page must list it, not drop it.
    assert any(not t["created_at"] for t in tasks)
    # A terminal task with no CLOSE stamp: the page must say so, not draw to now.
    assert any(t["state"] != "open" and not t["closed_at"] for t in tasks)
    # One session on TWO tasks, which is what the one-bill rule is about.
    spenders = [t["spender"] for t in tasks if t["spender"]]
    assert len(spenders) != len(set(spenders)), "no session serves two tasks"
    # A nested board, so a sub-lead exists to be drawn.
    assert any(b["parent"] for b in value["items"])
    # A truncation the pages must print.
    assert any(b["tasks_omitted"] for b in value["items"]) or value["spenders_omitted"]

    # CONCURRENCY: at least three worker tasks open across one instant. Without this
    # the overlap strip is drawn from a fixture that never overlaps.
    spans = []
    for t in tasks:
        if not t["created_at"]:
            continue
        spans.append((t["created_at"], t["closed_at"] or "9999"))
    at_once = max(sum(1 for lo, hi in spans if lo <= probe < hi) for probe, _ in spans)
    assert at_once >= 3, f"the sample never has three tasks open at once (max {at_once})"


# --------------------------------------------------------------------------- #
# the situation rule: four readings of an open task
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tid", IDS)
def test_a_person_needed_is_decided_before_any_silence(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """MUTATION-SENSITIVE: ``blocked`` / ``question`` is read BEFORE the report stamp.

    A blocked worker that reported two minutes ago would otherwise read as `working`,
    which is the exact case a reader opens the page to find. The order of the branches
    IS the invariant, so it is asserted as an order.
    """
    _, page = pages[tid]
    body = re.search(r"function situation\(task, now\) \{(.*?)\n    \}", page, re.S)
    assert body, f"{tid} has no situation()"
    source = body.group(1)
    need = source.index("'blocked'")
    last = source.index("last_report_at")
    assert need < last, (
        f"{tid}: the report stamp is read before the blocked/question status, so a "
        "worker waiting on a person reads as one that is working"
    )
    assert "'question'" in source, f"{tid}: a question is not treated as needing a person"


@pytest.mark.parametrize("tid", IDS)
def test_no_report_is_its_own_reading_and_not_a_long_silence(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """MUTATION-SENSITIVE: a missing stamp returns `mute`, never the idle branch.

    A task nobody has reported on has no elapsed silence to measure. Treating a null
    stamp as an infinitely old one would label a worker that was dispatched a minute
    ago as the coldest row on the page.
    """
    _, page = pages[tid]
    body = re.search(r"function situation\(task, now\) \{(.*?)\n    \}", page, re.S)
    assert body
    assert "if (last === null) return 'mute';" in body.group(
        1
    ), f"{tid}: an absent report stamp does not take its own branch"


@pytest.mark.parametrize("tid", IDS)
def test_both_pages_use_the_project_reports_own_quiet_threshold(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """20 minutes, the same number ``project-report`` draws its quiet chip at.

    Two thresholds would have two pages of one dashboard disagree about the same worker
    on the same record, and a reader cannot tell which to believe.
    """
    _, page = pages[tid]
    assert "QUIET_MS = 20 * 60 * 1000" in page, f"{tid} does not use the 20-minute threshold"
    report = (BUILTIN / "project-report/template.html").read_text(encoding="utf-8")
    assert (
        "QUIET_MS = 20 * 60 * 1000" in report
    ), "project-report's threshold moved; these two pages now disagree with it"


@pytest.mark.parametrize("tid", IDS)
def test_a_silence_past_two_hours_is_told_from_one_past_twenty_minutes(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """And in a colour of its own, not the same one dimmed: the two appear as two
    legend entries, and one swatch for both reads as one fact written twice."""
    _, page = pages[tid]
    assert "COLD_MS = 2 * 60 * 60 * 1000" in page, f"{tid} has no second silence band"
    assert re.search(r"--(tl|oc)-cold:\s*#", page), f"{tid}: the cold band has no colour of its own"


# --------------------------------------------------------------------------- #
# one bill per session
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tid", IDS)
def test_a_sessions_spend_is_taken_once_and_never_summed(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """MUTATION-SENSITIVE: the row's credits are not a sum over its task rows.

    The fold repeats one worker session's spend on EVERY task row it served, so adding
    those rows multiplies one bill by the number of tasks. It still prints as a
    plausible number, which is why this is asserted on the source rather than left to
    a reviewer to notice. The fixture has a session on two tasks, so the wrong code
    doubles a real figure.
    """
    _, page = pages[tid]
    assert "row.credits +=" not in page, f"{tid} accumulates a session's credits"
    assert (
        "if (c !== null && c > row.credits) row.credits = c;" in page
    ), f"{tid}: a session's spend is not taken as one figure"


@pytest.mark.parametrize("tid", IDS)
def test_an_unmeasured_cost_is_a_dash_and_never_a_zero(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """``credits_reported`` false beside 0.0 means nobody measured, which is not free."""
    _, page = pages[tid]
    assert (
        "row.reported" in page and "nobody measured" in page
    ), f"{tid} prints a cost without checking whether one was reported"


# --------------------------------------------------------------------------- #
# the timeline's window and its absences
# --------------------------------------------------------------------------- #


def test_the_window_comes_from_the_crews_own_stamps(pages: dict[str, tuple[Any, str]]) -> None:
    """Not from the clock. A fixed last-N-hours window pushes a crew that ran yesterday
    off the left edge, and the page then renders a correct-looking empty track."""
    _, page = pages["timeline"]
    assert "stamp(task.created_at)" in page and "if (lo === null || a < lo) lo = a;" in page, (
        "the window is not widened to each task's own start, so work outside it is "
        "drawn off the edge"
    )


def test_an_equal_or_inverted_window_refuses_to_draw_in_words(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """``hi <= lo`` divides by zero or goes negative, stacking every bar at one spot --
    which looks like a crew that did everything at once."""
    _, page = pages["timeline"]
    assert "hi <= lo" in page, "the page does not guard an equal or inverted window"
    assert "no row can be placed on a clock" in page


def test_an_empty_board_and_an_undrawable_one_say_different_things(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """Both end in no bars and they are not the same fact: one means there is no crew,
    the other means the crew's work carries no usable stamps."""
    _, page = pages["timeline"]
    assert "No board has reached this mate" in page
    assert "No task carries a start stamp" in page


def test_a_task_with_no_start_stamp_is_listed_rather_than_dropped(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: dropping it silently would leave the page's own task counts
    describing a different set of work than the one it drew."""
    _, page = pages["timeline"]
    assert "unplaceable.push(task)" in page, "a task with no start stamp is not collected"
    assert "cannot be placed" in page, "the unplaceable tasks are collected and never shown"


def test_the_silence_tail_is_drawn_only_while_a_task_is_open(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """A closed task's gap between its last report and its close is the CONDUCTOR's
    time. Hatching it would blame a worker for a wait it did not own."""
    _, page = pages["timeline"]
    assert (
        "lastAt !== null && end.open && now - lastAt >= QUIET_MS" in page
    ), "the silence tail does not check that the task is still open"


def test_a_leads_row_is_a_rule_and_says_it_is_an_extent(pages: dict[str, tuple[Any, str]]) -> None:
    """No fold records a conductor's own session span, so a solid bar on a lead's row
    would claim a measurement nobody took."""
    _, page = pages["timeline"]
    assert "tl-ext" in page, "a lead's row has no extent mark"
    assert (
        "end to end" in page and "not a measured session" in page
    ), "the lead's rule is not named as an extent, so it reads as a measured span"


def test_the_overlap_strip_counts_worker_tasks_and_not_the_leads_extents(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """An extent is the hull of its own board's tasks, so counting it would count every
    one of those tasks twice and the peak would be about double the truth."""
    _, page = pages["timeline"]
    strip = re.search(r"var counts = \[\], top = 0;(.*?)if \(top > 0\)", page, re.S)
    assert strip, "the overlap strip is gone"
    assert "rows[w].tasks" in strip.group(1), "the strip does not count worker tasks"
    assert "tl-ext" not in strip.group(1), "the strip counts the leads' extents too"


# --------------------------------------------------------------------------- #
# the org chart's graph and its cards
# --------------------------------------------------------------------------- #


def test_the_graph_reads_the_folds_own_parent_link(pages: dict[str, tuple[Any, str]]) -> None:
    """A reporting line is a BIND, never a guess from how deep a node sits.

    The fold records which task's bind created a nested board. Inferring the line from
    anything else -- a depth number, a round, the order of the list -- would draw a
    crew structure nobody actually created.
    """
    _, page = pages["org-chart"]
    build = re.search(r"function buildGraph\(list\) \{(.*?)\n    \}", page, re.S)
    assert build, "buildGraph is gone"
    source = build.group(1)
    # The map that holds "which board hangs off which task", keyed by BOTH, because two
    # boards can carry the same item id -- a board rebuilt under a new generation keeps
    # its predecessor's ids, and keying by item alone would put one board's children
    # under another's task.
    assert (
        "p.board" in source and "p.item_id" in source
    ), "the parent link does not read the fold's own board and item"
    assert "subOf[pb + '\\u0000' + pi]" in source, (
        "the parent map is not keyed by board AND item, so one board's child can land "
        "under another board's task of the same id"
    )
    # And a root is a board nobody bound, not a board at depth 0 by position.
    assert (
        "roots.push" in source and "list[k].parent" in source
    ), "a root is not decided from the absence of a parent link"


def test_a_sub_lead_is_its_own_role_and_appears_once(pages: dict[str, tuple[Any, str]]) -> None:
    """MUTATION-SENSITIVE: sub-lead is kept apart from worker, and is ONE node.

    A sub-lead both answers to someone and dispatches others. Calling it a worker hides
    half the crew's structure, and drawing it as a board node PLUS a task node puts the
    same agent on the page twice in two places with no sign they are one agent.
    """
    _, page = pages["org-chart"]
    assert "'sub-lead'" in page and "'lead'" in page
    kid = re.search(r"function drawKid\(kid, into, now[^)]*\) \{(.*?)\n    \}", page, re.S)
    assert kid, "drawKid is gone"
    assert kid.group(1).count("chip(") == 3, (
        "a sub-lead no longer draws exactly one chip for itself, one for its board and "
        "one for the plain-worker case"
    )


def test_a_card_names_its_lookup_keys_and_never_a_session_key(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """The boundary this pair must not relax at the last step.

    The fold aliases a worker's session key because the document is embedded in a page
    any dashboard caller can read. A card carries the board and the item -- the same
    pair a host resolves server-side -- so a link is possible without the key ever
    reaching the page.
    """
    _, page = pages["org-chart"]
    assert (
        "open via" in page and "pair.board.id" in page and "task.item_id" in page
    ), "a card does not name the two keys a host would resolve the session from"


@pytest.mark.parametrize("tid", IDS)
def test_no_page_reaches_for_a_session_key_or_a_slot(
    tid: str, pages: dict[str, tuple[Any, str]]
) -> None:
    """MUTATION-SENSITIVE: neither page reads a field that would BE a session key.

    ``worker_session_key`` is the fold's internal name for it and ``slot`` is a
    conductor's own. The rendered fold carries neither on a task row, so a page naming
    one is reaching past the alias -- which is the one direction this boundary fails
    in, since the field would simply resolve to nothing today and quietly start
    carrying a key if the fold's render ever changed.
    """
    _, page = pages[tid]
    manifest, _ = pages[tid]
    assert "worker_session_key" not in page, f"{tid} reads the fold's session-key field"
    for spec in manifest.fields.values():
        assert spec.path != "slot", f"{tid} binds the conductor's slot key"


def test_the_latest_run_is_the_workers_last_word_not_its_conductors(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: "latest" is ``last_report_at``, not a close stamp.

    A close is the conductor ruling on the work; the card is about what the WORKER did.
    Picking by close stamp would show a reader the task its conductor most recently
    signed off rather than the one its worker is on now.
    """
    _, page = pages["org-chart"]
    pick = re.search(r"var out = order\.map\(function \(k\) \{(.*?)return row;", page, re.S)
    assert pick, "the latest-run pick is gone"
    source = pick.group(1)
    assert "last_report_at" in source, "the pick does not read the worker's own last word"
    assert "closed_at" not in source, "the pick reads a close stamp, which is the conductor's"


def test_a_session_that_never_reported_still_gets_a_card_that_says_so(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """Dropping it would hide exactly the worker worth asking about, and a blank
    summary would read as a worker that reported nothing of interest."""
    _, page = pages["org-chart"]
    assert "never reported" in page, "a session with no report has no words of its own"
    assert "no report stamped" in page


def test_a_summary_is_cleaned_to_one_line_and_says_when_it_was_cut(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """A summary is free text a worker typed, so it arrives with newlines and runs of
    spaces. Dropped in unchanged it blows the card's height out; cut without a mark a
    reader cannot tell a short summary from a truncated one."""
    _, page = pages["org-chart"]
    one = re.search(r"function oneLine\(value\) \{(.*?)\n    \}", page, re.S)
    assert one, "oneLine is gone"
    source = one.group(1)
    assert "replace(/\\s+/g, ' ')" in source, "whitespace is not collapsed"
    assert "\\u2026" in source, "a cut summary carries no ellipsis"


# --------------------------------------------------------------------------- #
# the lead chip's fold toggle
# --------------------------------------------------------------------------- #


def test_lead_chip_carries_a_fold_toggle_button(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: drawNode adds a type=button to each lead chip.

    The button must be type='button' so it does not submit a form if one ever
    wraps the page. aria-expanded and aria-controls make it a first-class
    disclosure widget for keyboard and screen-reader users.
    """
    _, page = pages["org-chart"]
    # Button logic now lives in makeFoldToggle, not drawNode directly.
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "makeFoldToggle helper is gone"
    source = make_fn.group(1)
    assert "btn.type = 'button'" in source, "the fold button is missing type=button"
    assert "aria-expanded" in source, "the fold button has no aria-expanded"
    assert "aria-controls" in source, "the fold button has no aria-controls"
    assert "aria-label" in source, "the fold button has no accessible label"
    assert "oc-fold" in source, "the fold button has no class name"


def test_lead_groups_are_expanded_by_default(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: the initial state is expanded (aria-expanded='true').

    A collapsed-by-default graph hides the work the reader opened the page to see.
    The foldState entry for a new board must be true, and the button must reflect
    that with aria-expanded='true'.
    """
    _, page = pages["org-chart"]
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    assert (
        "foldState[stateKey] = true" in source
    ), "a new board is not initialised to expanded in foldState"
    assert "expanded ? 'true' : 'false'" in source, "aria-expanded does not read the expanded state"
    # The group is collapsed by adding oc-kids-closed; default (expanded) has no class.
    assert (
        "oc-kids-closed" in source
    ), "the kids container does not use the oc-kids-closed class for collapse"
    assert (
        "kids.hidden" not in source
    ), "drawNode still uses kids.hidden -- switch to oc-kids-closed for the CSS transition"
    # The inner body is also hidden from the AT when initially collapsed.
    assert (
        "kidsBodyEl.hidden = true" in source
    ), "kidsBodyEl.hidden is not set to sync aria-expanded=false with the AT"


def test_click_handler_toggles_hidden_and_aria_expanded(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: the click handler flips aria-expanded and the oc-kids-closed class.

    A handler that updates one but not the other leaves the DOM and the ARIA state
    out of sync: a screen reader would announce the wrong state, or the group would
    vanish without the button registering the change. The class toggle drives the
    CSS grid-template-rows transition instead of display:none.
    """
    _, page = pages["org-chart"]
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    # The handler reads the current state and inverts it.
    assert (
        "btn.getAttribute('aria-expanded') !== 'true'" in source
    ), "the click handler does not read aria-expanded to decide the next state"
    # Visibility is toggled via the class, not display:none.
    assert (
        "classList.remove('oc-kids-closed')" in source
    ), "the click handler does not remove oc-kids-closed when expanding"
    assert (
        "classList.add('oc-kids-closed')" in source
    ), "the click handler does not add oc-kids-closed when collapsing"
    assert (
        "btn.setAttribute('aria-expanded', next ? 'true' : 'false')" in source
    ), "the click handler does not update aria-expanded"
    # And the in-memory map is kept in sync.
    assert "foldState[stateKey] = next" in source, "the click handler does not update foldState"


def test_fold_state_is_declared_outside_render_so_it_survives_re_renders(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: foldState is at the IIFE scope, not inside render().

    The host re-renders the page on every message event. A variable declared inside
    render() resets on every data push, expanding every group the reader just folded.
    """
    _, page = pages["org-chart"]
    assert "var foldState" in page, "foldState is not declared"
    # The declaration must come before the render() call-site.
    fold_pos = page.index("var foldState")
    render_call = re.search(r"\n\s{4}render\(\);", page)
    assert render_call, "render() call-site is gone"
    assert (
        fold_pos < render_call.start()
    ), "foldState is declared after render() is invoked for the first time"
    # drawNode must read from (not re-declare) foldState.
    draw = re.search(r"function drawNode\(n, into, now\) \{(.*?)\n    \}", page, re.S)
    assert draw, "drawNode is gone"
    assert "var foldState" not in draw.group(
        1
    ), "drawNode re-declares foldState locally, so the outer map is never updated"


def test_fold_toggle_button_supports_keyboard_operation(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """A native <button> with type='button' is keyboard-operable without extra JS.

    No keydown or keypress handler is needed: Enter and Space activate a button
    natively. The button's focus-visible style confirms focus is visible to the user.
    """
    _, page = pages["org-chart"]
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    assert (
        "btn.type = 'button'" in source
    ), "the fold button is not type=button -- Enter and Space may not activate it"
    # CSS must make focus visible on the button.
    assert (
        ".oc-fold:focus-visible" in page
    ), "the fold button has no :focus-visible style -- keyboard focus is invisible"


def test_fold_toggle_uses_i18n_and_does_not_hardcode_english(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """State-specific labels must go through i18n() so non-English locales work.

    The button carries two labels: fold_hide (clicking will hide the group)
    and fold_show (clicking will show it, with a count). Both must be declared
    in the I18N table and fetched via i18n() in drawNode, never hardcoded.
    """
    _, page = pages["org-chart"]
    assert "'fold_hide'" in page, "fold_hide is not declared in I18N"
    assert (
        "'fold_show'" in page and "'fold_show_one'" in page
    ), "fold_show / fold_show_one i18n keys are missing"
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    assert "i18n('fold_hide')" in source, "fold_hide label is not fetched through i18n()"
    assert "i18n('fold_show" in source, "fold_show/fold_show_one label not fetched through i18n()"
    assert "fold_toggle" not in source, "old fold_toggle key is still used in drawNode"


def test_fold_button_text_changes_with_state(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: the visible text and aria-label change when the group toggles.

    When expanded the button reads 'Hide'; when collapsed it reads
    'Show N items'. A label that stays constant leaves the reader unable to
    tell whether clicking will show or hide the group.
    """
    _, page = pages["org-chart"]
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    # Both labels must be pre-computed so the click handler can swap them.
    assert "labelExpanded" in source, "expanded label variable is missing"
    assert "labelCollapsed" in source, "collapsed label variable is missing"
    # The click handler must update the visible text span AND the aria-label.
    assert (
        "textSpan.textContent = newLabel" in source
    ), "the click handler does not update the visible text"
    assert (
        "btn.setAttribute('aria-label', newLabel)" in source
    ), "the click handler does not update aria-label"


def test_fold_button_collapsed_count_matches_all_hidden_rows(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: the 'Show N' label counts every row that collapse hides.

    n.kids.length counts only direct children. A sub-lead child also has an "its
    board" chip and its own workers -- all hidden when the parent collapses.
    countVisibleRows must recurse using foldState so collapsed sub-leads count as 1 row.
    """
    _, page = pages["org-chart"]
    assert "function countVisibleRows" in page, "countVisibleRows function is missing"
    draw = re.search(r"function countVisibleRows\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert draw, "countVisibleRows body is gone"
    fn_source = draw.group(1)
    # It recurses into sub-lead children.
    assert ".sub" in fn_source, "countVisibleRows does not handle sub-lead children"
    assert "foldState" in fn_source, "countVisibleRows does not check foldState for sub-lead state"
    # drawNode must use countVisibleRows, not n.kids.length directly.
    node = re.search(r"function drawNode\(n, into, now\) \{(.*?)\n    \}", page, re.S)
    assert node, "drawNode is gone"
    node_source = node.group(1)
    assert (
        "countVisibleRows(n, boardId)" in node_source
    ), "drawNode uses n.kids.length instead of countVisibleRows"
    # countFn in makeFoldToggle handles the row count; drawNode passes countVisibleRows as lambda.


def test_fold_collapse_syncs_hidden_with_accessibility_tree(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: collapsed workers are removed from the AT after the transition.

    grid-template-rows: 0fr clips content visually but does not remove it from the
    accessibility tree. A screen reader would still announce the workers even when
    aria-expanded='false' unless hidden or inert is set after the grid row closes.
    Two mechanisms cover this: transitionend (animated) and a synchronous path for
    prefers-reduced-motion.
    """
    _, page = pages["org-chart"]
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    # transitionend listener sets hidden after the CSS animation ends.
    assert (
        "transitionend" in source
    ), "no transitionend listener: collapsed content stays in the AT indefinitely"
    assert (
        "kidsBodyEl.hidden = true" in source
    ), "the transitionend handler does not set kidsBodyEl.hidden"
    # Expanding must clear hidden before the transition so AT gets access during expand.
    assert (
        "kidsBodyEl.hidden = false" in source
    ), "expand does not restore kidsBodyEl.hidden = false before the grid opens"
    # prefers-reduced-motion path hides synchronously (no transition = no transitionend).
    assert "prm" in source, (
        "no synchronous path for prefers-reduced-motion (prm) -- AT sync relies on transitionend"
        " which may not fire when transition is none"
    )


def test_sub_lead_chip_also_carries_a_fold_toggle_button(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: drawKid adds a fold button to sub-lead chips too.

    The original issue asked to fold squad groups; in this codebase a squad is
    the sub-lead group (ITS BOARD chip + workers). drawKid must add the same
    .oc-fold button to the sub-lead chip as drawNode adds to the lead chip.
    """
    _, page = pages["org-chart"]
    kid = re.search(r"function drawKid\(kid, into, now[^)]*\) \{(.*?)\n    \}", page, re.S)
    assert kid, "drawKid is gone"
    source = kid.group(1)
    # Sub-lead fold button must use the same classes as the top-level one.
    # Sub-lead button is created inside makeFoldToggle; drawKid passes the chip to it.
    assert "foldControl" in source, "drawKid does not call makeFoldToggle for the sub-lead toggle"
    # Button attributes are set in makeFoldToggle; check that drawKid calls it.
    assert "oc-fold-icon" in page, "oc-fold-icon class is missing from the page"
    assert "oc-fold-text" in page, "oc-fold-text class is missing from the page"
    # Sub-lead uses a compound foldState key so its state is independent.
    assert "subKey" in source, "sub-lead has no independent foldState key"


def test_parent_label_updates_when_sub_lead_group_toggles(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: toggling a sub-lead group refreshes the parent label.

    countVisibleRows counts sub-lead rows according to their current foldState.
    When a sub-lead is collapsed, the parent 'Show N' label must decrease; when
    expanded, it must increase. This is achieved by passing a refreshParentLabel
    callback into drawKids that each sub-lead click handler calls.
    """
    _, page = pages["org-chart"]
    # drawNode must declare a callback for child updates.
    draw = re.search(r"function drawNode\(n, into, now\) \{(.*?)\n    \}", page, re.S)
    assert draw, "drawNode is gone"
    node_source = draw.group(1)
    assert (
        "refreshParentLabel" in node_source
    ), "drawNode has no refreshParentLabel callback for sub-lead toggle notifications"
    # drawKids must accept and forward the callback.
    kids_fn = re.search(r"function drawKids\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert kids_fn, "drawKids is gone"
    assert "onSubToggle" in kids_fn.group(
        1
    ), "drawKids does not forward the onSubToggle callback to drawKid"
    # drawKid's sub-lead click handler must call onSubToggle.
    kid_fn = re.search(r"function drawKid\(kid, into, now[^)]*\) \{(.*?)\n    \}", page, re.S)
    assert kid_fn, "drawKid is gone"
    assert "onSubToggle" in kid_fn.group(
        1
    ), "drawKid's sub-lead click handler does not call onSubToggle"


def test_sub_lead_fold_button_count_includes_its_board_row(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: the sub-lead 'Show N' adds 1 for the ITS BOARD chip.

    The sub-lead group's .oc-kids-body contains an ITS BOARD chip row plus the
    sub-lead's workers. countVisibleRows(kid.sub, subKey) counts only the workers;
    the sub-lead label must add 1 for the ITS BOARD chip so N matches what the
    user sees open up (the count used at the lead level already includes this +1
    via the `count++` line that runs before the recursion).
    """
    _, page = pages["org-chart"]
    kid = re.search(r"function drawKid\(kid, into, now[^)]*\) \{(.*?)\n    \}", page, re.S)
    assert kid, "drawKid is gone"
    source = kid.group(1)
    # Sub-lead label must add 1 for the ITS BOARD chip row.
    assert "1 + countVisibleRows(kid.sub, subKey)" in source, (
        "sub-lead count does not add 1 for the ITS BOARD chip -- "
        "'Show N' would undercount the hidden rows by one"
    )
    # The +1 must appear in all three places: initial label, subRefresh, and click handler.
    # The countFn lambda has 1 occurrence; makeFoldToggle calls countFn() in all label updates.
    assert (
        "1 + countVisibleRows(kid.sub, subKey)" in source
    ), "drawKid does not pass 1 + countVisibleRows as the countFn"


def test_an_empty_board_counts_its_nothing_dispatched_row(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: a board with no task counts as one row.

    drawKids draws a "nothing dispatched" line for a board with no task, so that line
    is what opens when the group opens. Counted as zero, a folded empty lead reads
    "Show 0" and then reveals a row, and every empty sub-lead below a lead makes that
    lead's count one too low. The behaviour is driven in
    ``website/src/test/orgChartFoldPage.test.ts``; this pins the two halves together.
    """
    _, page = pages["org-chart"]
    count = re.search(r"function countVisibleRows\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert count, "countVisibleRows is gone"
    first = count.group(1).strip().splitlines()[0].strip()
    assert (
        first == "if (!n.kids.length) return 1;"
    ), "countVisibleRows does not count an empty board's one line before anything else"
    kids = re.search(r"function drawKids\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert kids, "drawKids is gone"
    draws = kids.group(1)
    assert (
        "if (!n.kids.length)" in draws and "nothing_dispatched" in draws
    ), "drawKids does not draw one line for an empty board, so the count of 1 is wrong"


def test_the_rail_stops_at_the_last_elbow_at_both_widths(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: the last child's mask lands on the rail, and nothing clips it.

    The mask is a 1px strip in the card colour at ``left: -(padding-left + 1)`` from the
    last child, which is the rail's own column just outside .oc-kids-body's padding box.
    An ``overflow`` clip on .oc-kids-body cuts it off there, and a narrower padding kept
    with the wide offset puts it beside the rail. Either way the rail runs past the last
    elbow. The outer .oc-kids is the box that clips the closing row.
    """
    _, page = pages["org-chart"]
    css = page[: page.index("</style>")]
    narrow_at = css.index("@media (max-width: 520px)")
    wide, narrow = css[:narrow_at], css[narrow_at:]
    body = re.search(r"\.oc-kids-body \{([^}]*)\}", wide)
    assert body, "the .oc-kids-body rule is gone"
    assert "overflow" not in body.group(1), ".oc-kids-body clips, which cuts off the mask"
    outer = re.search(r"\.oc-kids \{([^}]*)\}", wide)
    assert outer, "the .oc-kids rule is gone"
    clips = "overflow: hidden" in outer.group(1)
    assert clips, ".oc-kids does not clip, so a closing group draws over the rows below it"
    mask_rule = r"\.oc-kids-body > \.oc-node:last-child:after \{[^}]*left: (-\d+)px"
    for name, block in (("wide", wide), ("narrow", narrow)):
        pad = re.search(r"\.oc-kids-body \{[^}]*padding-left: (\d+)px", block)
        mask = re.search(mask_rule, block)
        assert pad and mask, f"{name}: the body padding or the last-child mask offset is gone"
        assert int(mask.group(1)) == -(int(pad.group(1)) + 1), (
            f"{name}: the mask sits at {mask.group(1)}px but the rail is at "
            f"-{int(pad.group(1)) + 1}px, so the rail runs past the last elbow"
        )


def test_the_fold_button_says_the_cards_below_stay(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """Folding a group leaves the cards section alone, and the button says so.

    The cards are one per worker session, drawn from every task whatever the graph
    shows, so a reader who folds a group still sees its workers' cards below. The
    button's title says that in both languages.
    """
    _, page = pages["org-chart"]
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    source = make_fn.group(1)
    assert (
        "btn.title = i18n('fold_hint')" in source
    ), "the fold button does not say that the cards below stay"
    assert page.count("fold_hint: '") == 2, "fold_hint is not in both the en and zh-CN tables"


def test_a_re_render_gives_focus_back_to_the_rebuilt_fold_button(
    pages: dict[str, tuple[Any, str]],
) -> None:
    """MUTATION-SENSITIVE: a host push does not drop keyboard focus off a fold button.

    render() rebuilds the graph on every message event, which replaces every fold
    button. Unless it notes which group's button had focus before clearing the graph
    and focuses that group's new button after drawing it, a keyboard reader lands on
    the page body at each push and has to Tab back from the top.
    """
    _, page = pages["org-chart"]
    render = re.search(r"function render\(\) \{(.*?)\n    \}", page, re.S)
    assert render, "render is gone"
    source = render.group(1)
    clear = source.index("graph.textContent = ''")
    assert (
        0 <= source.find("document.activeElement") < clear
    ), "render does not read which fold button has focus before clearing the graph"
    assert "foldButtons = {}" in source, "render keeps the previous render's buttons"
    draw = source.index("drawNode(roots[i], graph, now)")
    assert (
        source.find("foldButtons[focusKey].focus(") > draw
    ), "render does not focus the rebuilt button after drawing the graph"
    make_fn = re.search(r"function foldControl\([^)]*\) \{(.*?)\n    \}", page, re.S)
    assert make_fn, "foldControl is gone"
    records = "foldButtons[stateKey] = btn" in make_fn.group(1)
    assert records, "foldControl does not record its button, so render cannot find it again"
