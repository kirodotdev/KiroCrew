"""The ``project-report`` built-in's own rule: the epic tree and what it rolls up.

The generic built-in cases already resolve this template's fold paths and load its
manifest. What has no home there is the thing this page is FOR -- it draws the board
list as a four-level tree and states a number for every node -- and two of those
properties can be wrong with every other gate green.

**The roll-up.** A task row of the ``workstreams`` fold reports the WHOLE spend of the
worker session bound to it, which is that fold's own posture: that is what the task
cost to run. One session bound to two tasks therefore appears twice at the same amount,
so a tree that ADDED its rows up would bill that session once per task it served --
the error the fold explicitly refuses to make in a board total. A page contradicting
the fold about the same money is worse than a page without the number, because the
reader has no way to tell which of the two is the record.

**The subtask link.** A nested board arrives as a SIBLING row with a ``parent``, not as
a child, so a page that ignored the link would draw every sub-board as a root of its
own: the same work twice, once under its task and once beside its epic.

No JS engine is reachable from pytest, so the page's own drawing is evidence only
through ``scripts/render_dashboard_builtin.py``. What is checked here is (1) the
template's source, for the rules a regex can actually pin, and (2) the SAMPLE fixture
those renders are taken from -- because a fixture that stopped exercising nesting or
the shared-worker case would leave the screenshots green and prove nothing. The second
is the one that keeps the first honest: a source pin with no data behind it is a
spelling test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_templates.manifest import ManifestError, load_template

TEMPLATE_ID = "project-report"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "src/kiro_crew/dashboard_templates/builtin" / TEMPLATE_ID
SAMPLE = ROOT / "test/fixtures/dashboard_templates/sample_workstreams.json"

#: The four level names the page tags a row with, outermost first. Named here so a case
#: can say WHICH level went missing rather than that a count changed.
LEVELS = ("epic", "story", "task", "sub")


@pytest.fixture(scope="module")
def loaded() -> tuple[Any, str]:
    try:
        return load_template(DIRECTORY)
    except ManifestError as exc:  # pragma: no cover - the failure path is the message
        pytest.fail(f"{TEMPLATE_ID}: {exc}")


@pytest.fixture(scope="module")
def script(loaded: tuple[Any, str]) -> str:
    """Just the page's script, so a term used in prose is not read as code."""
    _, html = loaded
    blocks = re.findall(r"(?is)<script\b[^>]*>(.*?)</script\b[^>]*>", html)
    assert blocks, "the page carries no script, so nothing it shows is drawn in JS"
    return "\n".join(blocks)


@pytest.fixture(scope="module")
def sample() -> dict[str, Any]:
    doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return doc["folds"]["workstreams"]["value"]


def _boards(sample: dict[str, Any]) -> list[dict[str, Any]]:
    items = sample["items"]
    assert isinstance(items, list) and items
    return items


def _nested(sample: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in _boards(sample) if isinstance(b.get("parent"), dict)]


# --------------------------------------------------------------------------- #
# the tree is actually there
# --------------------------------------------------------------------------- #


class TestTheTreeItDraws:
    def test_it_names_all_four_levels(self, script: str) -> None:
        """A level the page has no tag for is a level it cannot draw."""
        block = re.search(r"var LEVELS = \{(.*?)\n  \};", script, re.S)
        assert block, "the page declares no LEVELS table, so no row can wear a tag"
        named = set(re.findall(r"(\w+)\s*:\s*\[", block.group(1)))
        missing = [level for level in LEVELS if level not in named]
        assert not missing, f"{missing} have no tag, so those rows draw unlabelled"

    def test_every_level_tint_is_a_theme_variable(self, script: str) -> None:
        """A hard-coded colour here would read correctly in one theme only, and the
        page is rendered in both. The frame injects the variables; a literal hex is
        what survives a theme switch unchanged."""
        block = re.search(r"var LEVELS = \{(.*?)\n  \};", script, re.S)
        assert block
        literal = re.findall(r"\[\s*'[^']*'\s*,\s*'(#[0-9a-fA-F]{3,8})'", block.group(1))
        assert not literal, f"level tint(s) hard-coded instead of themed: {literal}"
        tints = re.findall(r"\[\s*'[^']*'\s*,\s*'([^']+)'\s*\]", block.group(1))
        assert len(tints) >= len(LEVELS)
        unthemed = [t for t in tints if "var(--" not in t]
        assert not unthemed, f"level tint(s) that are not theme variables: {unthemed}"

    def test_a_subtree_carries_a_set_of_sessions_not_a_running_total(self, script: str) -> None:
        """THE ROLL-UP RULE, pinned where it is implemented.

        ``mergeBills`` assigning by session key is what makes a parent's cost the
        UNION of its children's rather than their sum. A ``+=`` here would be the
        double-billing this page exists to avoid.
        """
        block = re.search(r"function mergeBills\(into, from\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no mergeBills, so a subtree total is a plain sum"
        body = block.group(1)
        assert "into[k] = from[k]" in body, (
            "mergeBills does not ASSIGN by session key, so one worker serving two "
            f"tasks is billed twice: {body.strip()!r}"
        )
        assert "+=" not in body, (
            "mergeBills adds instead of unioning, which bills one session once per "
            "task it served"
        )

    def test_the_bill_key_is_the_spender(self, script: str) -> None:
        """The key has to be the spender, because the spender is what was charged.
        Keying by item id would make every row distinct and the union a sum again."""
        block = re.search(r"function billKey\(t\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no billKey"
        assert "spender" in block.group(1), (
            "billKey does not read spender, so the roll-up cannot know " "two rows were one charge"
        )

    def test_the_page_never_reads_a_worker_session_key(self, script: str) -> None:
        """The payload carries an opaque alias, so a page reading the key reads a
        field that is not there -- and a page that DREW it would put a session key
        in front of every dashboard caller."""
        assert (
            "worker_session_key" not in script
        ), "the page reads worker_session_key, which the render does not emit"

    def test_an_unmeasured_subtree_draws_a_dash_rather_than_zero(self, script: str) -> None:
        """``0`` claims a measurement of nothing; a dash reports no measurement. The
        page promises the second for a single row, so it owes it at every level."""
        block = re.search(r"function billTotal\(set\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no billTotal"
        body = block.group(1)
        assert re.search(r"var n = null", body), (
            "billTotal starts at 0, so a subtree nobody measured reports a spend of "
            "zero instead of reporting that nobody measured it"
        )

    def test_a_bind_cycle_and_a_deep_chain_both_terminate(self, script: str) -> None:
        """A worker's board can bind a worker whose board binds back. Recursion with
        neither a seen-set nor a depth cap hangs the page rather than drawing it."""
        assert re.search(r"var SUB_DEPTH = \d+;", script), "no depth cap on the nesting walk"
        block = re.search(
            r"function taskNode\(level, key, board, t, nest, depth, seen\) \{(.*?)\n  \}",
            script,
            re.S,
        )
        assert block, "taskNode does not take a depth and a seen-set"
        body = block.group(1)
        assert "if (seen[sid]) continue;" in body, "a bind cycle is walked twice"
        assert "depth <= 0" in body, "the depth cap is taken but never checked"
        assert "n.held +=" in body, (
            "rows dropped at the cap are not counted, so the page shows a smaller "
            "tree without saying it is smaller"
        )

    def test_a_nested_board_is_read_through_the_folds_parent_link(self, script: str) -> None:
        """The page must not re-derive the nesting from a board id or a session key of
        its own: the fold states it, and two derivations of one fact disagree."""
        block = re.search(r"function nestIndex\(list\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no nesting index, so sub-boards are drawn as roots"
        assert "list[i].parent" in block.group(1), "nestIndex does not read the fold's parent link"

    def test_a_nested_board_is_not_also_drawn_as_a_root(self, script: str) -> None:
        """The same work twice -- once under its task, once beside its epic -- is the
        failure the root filter exists to prevent."""
        block = re.search(r"function rootBoards\(list\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no root filter, so every nested board is a root too"
        assert "parent" in block.group(1)

    def test_the_rings_are_always_the_two_levels_under_the_root(self, script: str) -> None:
        """So "inside" and "outside" mean one thing in both views: a share of the
        root, and a share of that share. The global view's root is the page, so the
        rings are epic and story; scoped to one epic they move down with it."""
        block = re.search(r"function sectionTree\(nodes, one\) \{(.*?)\n  \}", script, re.S)
        assert block, "sectionTree does not take a prebuilt forest and a scope flag"
        body = block.group(1)
        assert "one ? ['story', 'stories'] : ['epic', 'epics']" in body
        assert "one ? ['task', 'tasks'] : ['story', 'stories']" in body

    def test_the_one_workstream_header_reports_the_tree_root(self, script: str) -> None:
        """The page states done/total and spend TWICE in that view -- once in the
        header, once on the epic row under it. The board row's own counters exclude
        its nested boards, so sourcing the header from them puts two different
        answers to one question a few pixels apart.
        """
        block = re.search(r"function renderStream\(s, list\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no one-workstream view"
        body = block.group(1)
        assert re.search(r"var nodes = buildTree\(list, s\);", body), (
            "renderStream does not build the tree before the header, so the header "
            "cannot report what the tree reports"
        )
        assert (
            "root ? root.done" in body and "root ? root.total" in body
        ), "the header's done/total does not come from the tree root"
        assert "billTotal(root.bill)" in body, (
            "the header's spend does not come from the tree root, so it disagrees "
            "with the epic row directly below it"
        )

    def test_the_forest_is_built_once_per_render(self, script: str) -> None:
        """Both views hand ``sectionTree`` a forest rather than the list, which is
        what makes "the header and the row agree" structural instead of a habit."""
        calls = re.findall(r"sectionTree\(([^)]*)\)", script)
        bad = [
            c
            for c in calls
            if "nodes, one" not in c and "buildTree" not in c and c != "nodes, true"
        ]
        assert not bad, f"sectionTree call(s) passing something other than a forest: {bad}"

    def test_epics_and_the_newest_story_start_open(self, script: str) -> None:
        """An epic opening onto its rounds, not onto every item at once: the default
        the page promises. Tasks shut is what keeps a long board readable."""
        block = re.search(r"function seedOpen\(nodes\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page seeds no open rows, so the tree opens fully collapsed"
        body = block.group(1)
        assert "treeOpen[nodes[i].key] = true" in body, "epics do not start open"
        assert "kids[0].key] = true" in body, "the newest story does not start open"

    def test_the_pill_row_offers_one_pill_per_epic(self, script: str) -> None:
        """A nested board is reached by opening its parent task, so a pill of its own
        is a second door to the same rows -- and makes the ``All`` count disagree with
        the number of epics the tree beside it draws."""
        block = re.search(r"function renderPills\(list\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no pill row"
        body = block.group(1)
        assert "var roots = rootBoards(list);" in body, (
            "renderPills iterates the raw board list, so every nested board gets a "
            "pill of its own"
        )
        assert "pill(ALL, 'All', roots.length)" in body, (
            "the All pill counts boards rather than epics, so it disagrees with the "
            "tree under it"
        )
        assert (
            "list.length" not in body
        ), f"renderPills still counts the raw list somewhere: {body.strip()!r}"

    def test_the_kpi_card_counts_epics_where_it_says_workstreams(self, script: str) -> None:
        """Same quantity, same answer. The cost and needs-you numbers on that card are
        summed over EVERY board, nested ones included -- a charge is a charge wherever
        it was billed -- and this case does not touch them."""
        block = re.search(r"function sectionKpi\(list, points\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no KPI card"
        body = block.group(1)
        assert (
            "var epics = rootBoards(list).length;" in body
        ), "the KPI card does not derive an epic count"
        assert (
            "plural(epics, 'workstream', 'workstreams')" in body
        ), "the card still labels a raw board count as workstreams"


# --------------------------------------------------------------------------- #
# the sample fixture: what the screenshots are evidence OF
# --------------------------------------------------------------------------- #


class TestTheSampleExercisesTheTree:
    """Without these, a render can be green and cover none of the above.

    Every screenshot in the round's evidence is taken from this one file. A fixture
    that lost its nested board, or in which no two tasks shared a worker, would draw
    a correct-looking page from which neither the subtask level nor the roll-up rule
    was ever visible.
    """

    def test_the_fold_value_has_the_keys_the_tree_needs(self, sample: dict[str, Any]) -> None:
        for board in _boards(sample):
            assert "parent" in board, f"board {board.get('id')!r} carries no parent key"
            for task in board["tasks"]:
                assert "spender" in task, (
                    f"task {task.get('item_id')!r} carries no spender, so "
                    "the roll-up has no key to bill it through"
                )

    def test_exactly_one_board_is_nested_and_it_names_a_real_task(
        self, sample: dict[str, Any]
    ) -> None:
        nested = _nested(sample)
        assert len(nested) == 1, (
            "the sample needs one nested board for the SUB level to appear at all; "
            f"found {len(nested)}"
        )
        parent = nested[0]["parent"]
        boards = {b["id"]: b for b in _boards(sample)}
        assert parent["board"] in boards, f"parent names no board in the sample: {parent}"
        tasks = {t["item_id"] for t in boards[parent["board"]]["tasks"]}
        assert parent["item_id"] in tasks, (
            f"parent names item {parent['item_id']!r}, which is not a retained task of "
            f"board {parent['board']!r}, so the sub-board would hang under nothing"
        )

    def test_the_nested_board_is_not_a_root(self, sample: dict[str, Any]) -> None:
        """Its parent must be a board that is itself a root, or the one epic the shots
        open would not contain it."""
        nested = _nested(sample)[0]
        boards = {b["id"]: b for b in _boards(sample)}
        assert boards[nested["parent"]["board"]]["parent"] is None

    def test_two_tasks_share_one_worker_so_the_roll_up_is_discriminating(
        self, sample: dict[str, Any]
    ) -> None:
        """THE CASE THAT TELLS THE TWO RULES APART.

        With every task on its own session, a union and a sum give the same number
        and a screenshot cannot show which one the page computed. The sample needs at
        least one session billed through two rows, and those rows must carry a cost,
        or the distinction is invisible again.
        """
        shared: list[tuple[str, str]] = []
        for board in _boards(sample):
            seen: dict[str, str] = {}
            for task in board["tasks"]:
                key = task.get("spender")
                if not isinstance(key, str) or not task.get("credits_reported"):
                    continue
                if key in seen:
                    shared.append((board["id"], key))
                seen[key] = task["item_id"]
        assert shared, (
            "no session in the sample is billed through two costed rows, so a page "
            "summing its rows and a page unioning its sessions would render the same "
            "number and the screenshots prove neither"
        )

    def test_the_union_and_the_sum_really_differ(self, sample: dict[str, Any]) -> None:
        """The consequence of the case above, stated as the two numbers.

        Restated in Python to ask whether the FIXTURE discriminates, not to judge the
        page: the page's JS is the only thing that draws. If these were equal the
        case above would be satisfied by data that still proves nothing.
        """
        nested = _nested(sample)[0]
        rows = [t for t in nested["tasks"] if t.get("credits_reported")]
        naive = round(sum(float(t["credits"]) for t in rows), 6)
        union = round(sum({t["spender"]: float(t["credits"]) for t in rows}.values()), 6)
        assert union != naive, (
            f"the nested board sums to {naive} either way, so the roll-up rule has no "
            "observable consequence in this fixture"
        )
        assert nested["credits"] == union, (
            f"the fold's own board total is {nested['credits']}, but the union of its "
            f"rows' sessions is {union}; the fixture contradicts the rule it is meant "
            "to exercise"
        )

    def test_some_task_reports_no_cost_at_all(self, sample: dict[str, Any]) -> None:
        """So the dash is on the page rather than only in the promise."""
        unmeasured = [
            t["item_id"]
            for b in _boards(sample)
            for t in b["tasks"]
            if not t.get("credits_reported")
        ]
        assert unmeasured, "every task in the sample has a cost, so no row draws a dash"

    def test_the_nested_board_has_more_than_one_story_worth_of_rounds_above_it(
        self, sample: dict[str, Any]
    ) -> None:
        """The parent epic needs at least two rounds, or the default-open rule (every
        epic, the newest story only) is indistinguishable from opening everything."""
        nested = _nested(sample)[0]
        boards = {b["id"]: b for b in _boards(sample)}
        parent = boards[nested["parent"]["board"]]
        rounds = {t.get("round") for t in parent["tasks"]}
        assert len(rounds) >= 2, (
            f"board {parent['id']!r} has one round, so a shot of it cannot show that "
            "only the newest story opens"
        )


# --------------------------------------------------------------------------- #
# the "needs you" card says what to do
# --------------------------------------------------------------------------- #


class TestTheNeedsYouCardNamesItsAction:
    """One label for the rows that wait on a reader, and a next step on every row.

    A reader who cannot tell two waiting labels apart treats both as something not to
    touch, so the label stops carrying the difference and the STEP carries it instead:
    a question is answered, a blocked task is unblocked. The card is the page's main
    call to action and every crewmate sees it on open, so a row that names no action
    is the whole card failing at the one thing it is for.

    Pinned against the source because no JS engine runs here, and against the sample
    because a source pin with no data behind it is a spelling test: the fixture has to
    hold BOTH waiting reasons or the two steps are never told apart on a real render.
    """

    def test_two_kinds_blocked_and_asks_you(self, script: str) -> None:
        """Blocked (red family) and Asks you (purple family) -- two kinds, not one.

        Paperclip collapsed eleven attention sources into two by borrowing the
        task-status colour vocabulary.  The fold carries ``status == 'blocked'`` vs
        ``'question'``; that is the same split, so the card borrows the same two
        families.  A single "Waiting on you" label made both rows look identical and
        taught the reader that nothing here was worth telling apart.
        """
        pushed = re.findall(r"rows\.push\(\{\s*kind:\s*'([^']+)'", script)
        assert pushed, "no row is pushed into the card, so it draws nothing"
        kinds = set(pushed)
        assert kinds == {
            "Blocked",
            "Asks you",
        }, f"expected exactly the two kinds 'Blocked' and 'Asks you'; got: {sorted(kinds)}"

    def test_every_row_carries_a_next_step(self, script: str) -> None:
        kinds = re.findall(r"rows\.push\(\{\s*kind:\s*[^,\n]+,\s*\n?\s*step:", script)
        pushed = re.findall(r"rows\.push\(\{\s*kind:", script)
        assert len(kinds) == len(pushed), (
            f"{len(pushed) - len(kinds)} of {len(pushed)} row kinds carry no step, so "
            "that row tells the reader nothing to do"
        )

    def test_the_step_differs_by_why_the_task_waits(self, script: str) -> None:
        """Answering and unblocking are different acts, so the step is not one string."""
        assert "'Unblock it in chat'" in script and "'Answer in chat'" in script, (
            "the waiting rows share one step, so the label was collapsed and nothing "
            "took over the difference it used to carry"
        )

    def test_the_total_counts_every_row_including_the_crewmates(self, script: str) -> None:
        """A number over the alarm has to match the rows under it.

        REPLACES a pin that held the total to `workers` alone. That pin was right that
        a count must match its list and wrong about which rows ask for something: the
        conductor's own instructions say an item written to this field "appears under
        Needs you with the act it needs", so leaving those rows out of the headline
        under-reported the one question the field answers.

        The two halves are added, not merged: `workers` is the fold's own number and
        can exceed the worker rows retained, while the crewmate's rows are counted in
        hand, so the guard raises `workers` to what is drawn before the sum.
        """
        block = re.search(r"function needsYou\(list, scope\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no needsYou"
        body = block.group(1)
        assert re.search(r"var total = workers \+ mine;", body), (
            "the needs-you total leaves out the rows the crewmate wrote, so the "
            "headline under-reports what is waiting"
        )
        assert re.search(r"if \(workers < fromWorkers\) workers = fromWorkers;", body), (
            "nothing raises the fold's count to the rows in hand, so the headline can "
            "sit below the list it is over"
        )

    def test_no_row_in_this_card_says_there_is_nothing_to_do(self, script: str) -> None:
        """The card is the page's call to action; a row with no act does not belong.

        `Nothing to do` and the `From the crewmate` group were the same mistake: they
        drew the conductor's "needs you" items as passive notes, so the question went
        from answered to looking unanswered.
        """
        for dead in ("'Nothing to do'", "'Note from crewmate'", "'From the crewmate'"):
            assert dead not in script, (
                f"{dead} is still drawn, so an item that needs the reader is shown as "
                "something to read past"
            )

    def test_the_crewmates_rows_are_still_attributed(self, script: str) -> None:
        """Counted with the rest, but still marked as the crewmate's.

        Who raised an item is worth showing, and that is all the `agent` flag decides:
        it marks the row and has no say in which group the row lands in or whether the
        headline counts it.
        """
        assert "'crewmate wrote this'" in script, (
            "the crewmate's rows are no longer attributed, so a reader cannot tell "
            "which items came from the crewmate rather than the task record"
        )

    def test_the_step_is_drawn_and_not_merely_computed(self, script: str) -> None:
        assert re.search(r"el\('div',\s*'pr-todo-s',\s*row\.step\)", script), (
            "the step is never appended to the row, so it exists only in the row "
            "object and no reader ever sees it"
        )

    def test_the_sample_holds_both_waiting_reasons(self, sample: dict[str, Any]) -> None:
        statuses = {
            t.get("status")
            for b in _boards(sample)
            for t in (b.get("tasks") or [])
            if isinstance(t, dict)
        }
        missing = {"blocked", "question"} - statuses
        assert not missing, (
            f"the sample has no {sorted(missing)} task, so a render of it cannot show "
            "that the two steps differ"
        )


# --------------------------------------------------------------------------- #
# the needs-you desk: two kinds, shelves, summary cleaning, owner display
# --------------------------------------------------------------------------- #


class TestTheNeedsYouDeskDesign:
    """The desk's five design choices, each pinned to the source.

    These are complementary to ``TestTheNeedsYouCardNamesItsAction``, which
    tests the earlier "one label, two steps" rule.  The tests here check the
    new structural work: two colour families, recency shelves, summary
    cleaning, owner/no-owner display, scoped zero state, and the bundle header
    for a session that asks several things in a row.
    """

    def test_blocked_uses_s1_and_asks_you_uses_s2(self, script: str) -> None:
        """Red for Blocked (s1), purple for Asks you (s2).

        The kind-colour token is set per-item in JS (``--pr-todo-c``) so the
        CSS can reference it without knowing the kind in advance.  A hard-coded
        hex or the shared ``--pr-accent`` would flatten both kinds to one colour
        and the desk would look the same as before.
        """
        # Blocked rows must carry s1
        blocked = re.search(r"kind:\s*'Blocked'.*?kindColor:\s*'([^']+)'", script, re.S)
        assert blocked, "Blocked row has no kindColor field"
        assert "--pr-s1" in blocked.group(
            1
        ), f"Blocked kindColor is {blocked.group(1)!r}; expected --pr-s1 (red family)"
        # Asks you rows must carry s2
        asks = re.search(r"kind:\s*'Asks you'.*?kindColor:\s*'([^']+)'", script, re.S)
        assert asks, "Asks you row has no kindColor field"
        assert "--pr-s2" in asks.group(
            1
        ), f"Asks you kindColor is {asks.group(1)!r}; expected --pr-s2 (purple family)"

    def test_kind_color_token_is_set_per_item(self, script: str) -> None:
        """``--pr-todo-c`` is set inline on the item element, not in a class.

        Setting it inline is what lets the CSS rules reference one variable and
        have each item resolve to its own colour -- a class per kind would
        duplicate the border and background rules instead.
        """
        assert "style.setProperty('--pr-todo-c'" in script, (
            "the page does not set --pr-todo-c per item, so every card is the "
            "same colour regardless of its kind"
        )

    def test_shelves_named_new_and_earlier(self, script: str) -> None:
        """Two shelves by recency: 'New since you looked' and 'Earlier'.

        An empty shelf is not drawn, so a board where all items are new shows
        only the first heading and vice versa.  The page must know the boundary
        (DESK_NEW_MS) and must pass both labels to ``drawShelf``.
        """
        assert "DESK_NEW_MS" in script, (
            "the page has no DESK_NEW_MS constant, so every item lands on one "
            "undifferentiated list regardless of when it arrived"
        )
        assert "'New since you looked'" in script, "'New since you looked' heading is absent"
        assert (
            "fold(W('earlier'), oldRows.length" in script
        ), "older items are not folded under their own closed heading"

    def test_shelf_boundary_is_one_hour(self, script: str) -> None:
        """The 'New since you looked' shelf holds items from the last hour.

        60 minutes is the boundary the Paperclip desk uses for 'Decide now'.
        The constant is named so one change moves the boundary everywhere.
        """
        block = re.search(r"var DESK_NEW_MS = ([^;]+);", script)
        assert block, "no DESK_NEW_MS assignment found"
        expr = block.group(1).strip()
        # Accept "60 * 60 * 1000" or "3600000"
        assert (
            "60" in expr and "1000" in expr or expr == "3600000"
        ), f"DESK_NEW_MS = {expr!r}; expected one hour in milliseconds"

    def test_scoped_zero_state_names_the_workstream(self, script: str) -> None:
        """Two different empty-state messages: global vs scoped.

        'Nothing needs you.' and 'Nothing in this workstream needs you.' so a
        reader on a one-workstream view knows their filter is what made it
        empty, rather than thinking the whole board has nothing.
        """
        assert "'Nothing needs you.'" in script, "'Nothing needs you.' zero state absent"
        assert "'Nothing in this workstream needs you.'" in script, (
            "'Nothing in this workstream needs you.' zero state absent; a scoped "
            "view and a truly empty board look the same"
        )
        # The scoped variant must depend on `scope` being truthy
        block = re.search(r"scope\s*\?.*?'Nothing in this workstream needs you\.'", script)
        assert block, (
            "the scoped zero-state message does not depend on `scope`, so both "
            "views render the same text"
        )

    def test_blocked_owner_line_only_when_fold_names_one(self, script: str) -> None:
        """The owner line is drawn only when the fold names an owner.

        A 'no owner named' warning on every blocked card was noise a reader
        could not act on, so the page stays silent when the fold has no owner.
        """
        assert "'no owner named'" not in script
        assert "row.kind === 'Blocked' && row.owner" in script

    def test_card_face_shows_one_headline_not_the_report(self, script: str) -> None:
        """The card shows one capped line; the worker's words sit behind a toggle."""
        assert "function headline(text)" in script
        assert "headline(row.why)" in script
        assert "'pr-todo-more'" in script and "worker's words" in script
        body = script.split("function headline(text)")[1].split("\n  }")[0]
        assert "120" in body, "the headline is not capped"
        assert "STOP_WORDS" in body, "the headline does not prefer the sentence naming the stop"
        assert "Ends when: '" not in script, "the constant 'Ends when' line is back"

    def test_clean_summary_strips_markdown_structure(self, script: str) -> None:
        """``cleanSummary`` removes headers, lists, fences, and tables.

        A raw summary can be nine lines of markdown.  The cleaning algorithm
        (AgentDetail.tsx:1592, team#8) strips structure tokens and keeps the
        first three prose lines up to 280 characters -- the same bounds
        Paperclip uses for the Latest Run card.
        """
        block = re.search(r"function cleanSummary\(raw\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no cleanSummary function"
        body = block.group(1)
        # Must strip markdown headers
        assert (
            r"replace(/^#{1,6}\s+/gm, '')" in body
        ), "cleanSummary does not strip markdown headers"
        # Must filter structure lines
        for pattern in ("'---'", "'|'", "'```'"):
            assert pattern in body, f"cleanSummary does not filter lines starting with {pattern}"
        # Must cap at 3 lines and 280 chars
        assert (
            "3" in body and "280" in body
        ), "cleanSummary does not enforce the 3-line / 280-char cap"

    def test_why_now_field_is_cleaned_summary(self, script: str) -> None:
        """The ``why`` field on each row is ``cleanSummary(raw)``, not the raw string.

        The clean excerpt is what appears as 'why now' on the card; the raw
        text is kept internally for future use but never drawn directly.
        """
        block = re.search(r"rows\.push\(\{.*?why:\s*cleanSummary", script, re.S)
        assert block, (
            "worker rows do not set 'why' to cleanSummary(...); the why-now line "
            "would display raw markdown"
        )

    def test_bundle_header_for_multiple_asks_from_same_session(self, script: str) -> None:
        """When one session asks several things in a row, a header names the count.

        'Session N asked N things' precedes the group so a reader is not
        surprised by consecutive cards from the same source.  The session
        ordinal is derived, not the raw session key.
        """
        assert "'Session '" in script, (
            "no bundle header text; multiple asks from one session are "
            "indistinguishable from unrelated items"
        )
        assert (
            "asked " in script and "plural(" in script
        ), "the bundle header does not count the asks, or does not pluralise them"
        # The page must NOT draw the raw spender key
        assert re.search(r"\.textContent\s*=\s*[^;]*spender", script) is None, (
            "the page draws the raw spender key, which is an opaque session alias "
            "and not a human-readable label"
        )

    def test_sample_has_items_in_both_shelves(self, sample: dict[str, Any]) -> None:
        """With now = 2026-10-04T17:12:00Z, the question task is within 1 h (new)
        and the blocked task is more than 1 h ago (earlier).

        Without at least one item in each shelf, a render cannot show that the
        desk actually splits them -- and the headings would never be tested.
        """
        import datetime as _dt

        now = _dt.datetime(2026, 10, 4, 17, 12, 0, tzinfo=_dt.timezone.utc)
        one_hour = _dt.timedelta(hours=1)

        new_found = False
        earlier_found = False

        for board in _boards(sample):
            for task in board.get("tasks", []):
                if task.get("status") not in ("blocked", "question"):
                    continue
                lr = task.get("last_report_at") or ""
                if not lr:
                    continue
                try:
                    ts = _dt.datetime.fromisoformat(lr.replace("Z", "+00:00"))
                except ValueError:
                    continue
                delta = now - ts
                if delta < one_hour:
                    new_found = True
                else:
                    earlier_found = True

        assert new_found, (
            "no needs-you task has last_report_at within 1 h of now=2026-10-04T17:12Z; "
            "the 'New since you looked' shelf never renders in a shot"
        )
        assert earlier_found, (
            "no needs-you task has last_report_at more than 1 h before now; "
            "the 'Earlier' shelf never renders in a shot"
        )


class TestWhoIsWorking:
    """One row per worker with open work (Paperclip's Latest Run card)."""

    def test_the_section_is_in_the_global_layout_after_needs_you(self, script: str) -> None:
        layout = re.search(r"var LAYOUT = \[([^\]]+)\]", script)
        assert layout
        order = [s.strip(" '") for s in layout.group(1).split(",")]
        assert order.index("crew") == order.index("todo") + 1

    def test_the_one_workstream_view_draws_it_too(self, script: str) -> None:
        body = script.split("function renderStream(")[1].split("\n  function ")[0]
        assert "sectionCrew([s])" in body

    def test_a_row_is_keyed_by_spender_and_only_open_tasks_count(self, script: str) -> None:
        body = script.split("function sectionCrew(")[1].split("\n  function ")[0]
        assert "txt(t.spender)" in body
        assert "txt(t.state) !== 'open'" in body
        assert "worker_session_key" not in body

    def test_quiet_progress_reads_gone_quiet_not_working(self, script: str) -> None:
        body = script.split("function crewState(")[1].split("\n  function ")[0]
        assert "QUIET_MS" in body and "'quiet'" in body
        assert re.search(r"quiet:\s*\['gone quiet'", script)

    def test_stuck_states_sort_first(self, script: str) -> None:
        ranks = dict(re.findall(r"(\w+): \['[^']+', '[^']+', (\d)\]", script))
        assert int(ranks["blocked"]) < int(ranks["question"]) < int(ranks["quiet"])
        assert int(ranks["quiet"]) < int(ranks["progress"]) < int(ranks["done"])

    def test_the_list_is_capped_and_says_how_many_it_held_back(self, script: str) -> None:
        assert "var CREW_CAP = 12;" in script
        body = script.split("function sectionCrew(")[1].split("\n  function ")[0]
        assert "' more'" in body and "no worker bound yet" in body

    def test_the_last_line_is_cleaned(self, script: str) -> None:
        body = script.split("function crewSaid(")[1].split("\n  function ")[0]
        assert body.count("cleanSummary(") == 2

    def test_a_row_opens_the_task_drawer_by_click_and_keyboard(self, script: str) -> None:
        body = script.split("function crewRow(")[1].split("\n  function ")[0]
        assert "renderDrawer()" in body
        assert "'click'" in body and "'keydown'" in body and "tabIndex = 0" in body

    def test_the_sample_has_open_work_from_several_workers(self, sample: dict[str, Any]) -> None:
        spenders = {
            t.get("spender")
            for b in _boards(sample)
            for t in b["tasks"]
            if t.get("state") == "open" and t.get("spender")
        }
        assert len(spenders) >= 3


class TestMotionIsATokenEverywhere:
    """Every transition names a motion token; reduced motion zeroes them in one place."""

    def test_no_literal_duration_in_a_transition_or_animation(
        self, loaded: tuple[Any, str]
    ) -> None:
        html = loaded[1]
        style = html.split("<style>")[1].split("</style>")[0]
        for line in style.splitlines():
            if re.search(r"\b(transition|animation)\s*:", line) and "none" not in line:
                assert "var(--pr-motion-" in line, line

    def test_reduced_motion_zeroes_the_token(self, loaded: tuple[Any, str]) -> None:
        style = loaded[1].split("<style>")[1].split("</style>")[0]
        block = style.split("@media (prefers-reduced-motion: reduce)")[1]
        assert "--pr-motion-fast: 0ms" in block


class TestCleaningKeepsALongFirstLine:
    def test_a_first_line_over_the_cap_is_cut_not_dropped(self, script: str) -> None:
        body = script.split("function cleanSummary(")[1].split("\n  function ")[0]
        assert "if (!out.length) out.push(lines[k].slice(0, 279)" in body


class TestNeedsYouCardsOfferChoices:
    """Each needs-you card carries buttons; picking one fills the chat box and folds it."""

    def test_a_choice_posts_an_act_and_never_sends(self, script: str) -> None:
        assert "type: 'kirocrew-dashboard:act'" in script
        assert "fetch(" not in script.split("function act(text)")[1].split("\n  }")[0]

    def test_every_kind_gets_choices_and_a_write_my_own(self, script: str) -> None:
        assert script.count("choices: choicesFor(") == 3
        assert "'Write my own'" in script
        assert "'Re-dispatch it'" in script and "'Drop it'" in script

    def test_a_ruling_line_supplies_its_own_options(self, script: str) -> None:
        assert "function parseRuling(raw)" in script
        assert "RULING:" in script.split("function parseRuling(raw)")[1].split("\n  }")[0]

    def test_a_picked_card_folds_and_can_be_undone(self, script: str) -> None:
        assert "deskPicked[row.key]" in script
        assert "function sentLine(row)" in script and "function fold(label, n, body)" in script


class TestManagerCards:
    """A crewmate's for_you item is a manager's card: kind, context, why, how, buttons."""

    def test_three_kinds_with_their_own_buttons(self, script: str) -> None:
        for badge in ("'Pick one'", "'OK to go?'", "'Only you'"):
            assert badge in script
        assert "'Go ahead'" in script and "'Not now'" in script
        assert "'Done it'" in script

    def test_brief_reads_context_why_how(self, script: str) -> None:
        body = script.split("function briefOf(item)")[1].split("\n  }")[0]
        for key in ("'context'", "'why'", "'how'"):
            assert key in body


class TestTheAgentPicksTheLanguage:
    """The desk's labels come from the crewmate's ui_words; English is only the fallback."""

    def test_words_ride_on_the_verdict(self, script: str) -> None:
        body = script.split("function W(key)")[1].split("\n  }")[0]
        assert "fields().verdict" in body and "vd.words" in body

    def test_desk_labels_go_through_W(self, script: str) -> None:
        assert "function W(key)" in script
        assert (
            "\\u66f4" not in script and "\\u5df2" not in script
        ), "a hard-coded Chinese label is back"
        for key in (
            "go",
            "not_now",
            "done",
            "cant",
            "redispatch",
            "drop",
            "own",
            "earlier",
            "answered",
            "undo",
        ):
            assert f"W('{key}')" in script, f"{key} is not read from ui_words"
