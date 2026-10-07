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
    assert "No board has reached this crewmate" in page
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
    kid = re.search(r"function drawKid\(kid, into, now\) \{(.*?)\n    \}", page, re.S)
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
