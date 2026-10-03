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
    blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)
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

    def test_the_bill_key_is_the_worker_session(self, script: str) -> None:
        """The key has to be the session, because the session is what was charged.
        Keying by item id would make every row distinct and the union a sum again."""
        block = re.search(r"function billKey\(t\) \{(.*?)\n  \}", script, re.S)
        assert block, "the page has no billKey"
        assert "worker_session_key" in block.group(1), (
            "billKey does not read worker_session_key, so the roll-up cannot know "
            "two rows were one charge"
        )

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
                assert "worker_session_key" in task, (
                    f"task {task.get('item_id')!r} carries no worker_session_key, so "
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
                key = task.get("worker_session_key")
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
        union = round(sum({t["worker_session_key"]: float(t["credits"]) for t in rows}.values()), 6)
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
