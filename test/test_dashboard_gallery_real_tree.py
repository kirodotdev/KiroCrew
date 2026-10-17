"""The gallery page against a REAL folded tree, not a shape this repository wrote down.

Every other check of this page compares it to something written here: the end-to-end
route test stubs ``read_tree_projection``, the gallery test walks the renderer's source,
and the capture harness builds its own rows. All three agreed with each other and all
three were wrong together -- the page read ``slot_key`` / ``on_cycle`` / ``entries`` and
the fold emits ``board`` / ``cycle`` / ``items``, so every row of a copied page drew
"a board" with a count of zero and no cycle flag.

So this one plants real ``work/recorded`` entries in real session logs, folds them with
the real :func:`~kiro_crew.crew_log.projection.read_tree_projection`, and drives the
gallery page's own script over that output. Nothing between the log and the assertion is
a fixture, which is what makes it able to catch a key the page and the harness both
misspell the same way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

GATEWAY = "gateway"
ROOT = "chat-1-root"
WORKER = "chat-2-worker"
FOLD = "worktree"

GALLERY = (
    Path(mdf.__file__).resolve().parents[2]
    / "builtin_skills"
    / "dashboard"
    / "gallery"
    / "dispatch-tree"
    / "package.json"
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Any, _floor_monkeypatch: Any) -> Any:
    """Its own data home, through the isolation floor's own MonkeyPatch.

    The same pin ``test_crew_log_worktree_read`` uses, and for its reason: a test that
    called ``monkeypatch.undo()`` on the shared instance would drop this with it and the
    next test would resolve the operator's real home.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()


def _log(unit_id: str, slot: str) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _work(handle: CrewLog, slot: str, action: str, *, by: str = "", **data: Any) -> Any:
    return handle.append(
        WORK_ENTRY_TYPE,
        {"slot": slot, "actor": "conductor", "by": by or slot, "action": action, **data},
        src=GATEWAY,
    )


def _planted_tree() -> dict[str, Any]:
    """ROOT conducts WORKER, folded by the real read. Two logs, because an edge is
    recorded in both: the parent's ``create`` owns the item and the child's ``goal``
    cites it, and neither log alone places the child."""
    root = _log("s-root", ROOT)
    _work(root, ROOT, "goal", goal="ship the page host", round=1, generation="g-root")
    _work(root, ROOT, "create", item_id="it-worker", title="write the page", generation="g-root")
    _work(root, ROOT, "bind", item_id="it-worker", worker_session_key=WORKER, generation="g-root")
    worker = _log("s-worker", WORKER)
    _work(
        worker,
        WORKER,
        "goal",
        goal="write the page",
        round=1,
        parent_item="it-worker",
        generation="g-worker",
    )
    _work(worker, WORKER, "create", item_id="it-sub", title="draw a row", generation="g-worker")
    return crew_log.read_tree_projection(ROOT, FOLD).value


@pytest.fixture(scope="module")
def page() -> dict[str, Any]:
    from kiro_crew.artifact_store import dashboard_package as dp

    return dp.parse_package(GALLERY.read_text(encoding="utf-8"))


def test_the_real_fold_emits_the_keys_the_page_reads(page: dict[str, Any]) -> None:
    """THE CHECK THAT WOULD HAVE CAUGHT IT. The keys come off a folded value, so a name
    only this repository believes in cannot satisfy it."""
    value = _planted_tree()
    assert value["boards"], "the planted fleet folded to no boards"
    emitted = set(value["boards"][0])
    read = set(re.findall(r"\bboard\.([a-z_]+)", page["view"]["html"]))
    assert read, "the page reads no board field at all"
    unknown = read - emitted
    assert not unknown, (
        f"the page reads {sorted(unknown)} off a board row. A real folded row carries "
        f"{sorted(emitted)}, so every copied page draws a blank there."
    )


def test_the_page_draws_the_planted_fleet(page: dict[str, Any]) -> None:
    """End to end over real data: the page's own script, the real fold's output, and the
    board names and goals the logs recorded appear on screen.

    A key mismatch shows up here as the literal fallback text, which is what a reader of
    a broken copy would have seen.
    """
    from test_dashboard_frame_subscribe import _run

    from kiro_crew import dashboard_frame

    value = _planted_tree()
    read = dashboard_frame.read_payload(
        {
            "boards": value["boards"],
            "roots": value["roots"],
            "cycles": value["cycles"],
            "unresolved": value["unresolved"],
        }
    )
    page_js = page["view"]["html"].split("<script>")[1].split("</script>")[0]
    drawn = _run(
        dashboard_frame._BOOTSTRAP_JS,
        read,
        "const node = () => ({ setAttribute() {}, appendChild(c) {"
        "    if (c && c.text !== undefined) __calls.push(c.text); }, style: {}, hidden: false,"
        "  set textContent(v) { this.text = v; }, get textContent() { return ''; } });"
        "const owned = { nodes: node(), empty: node(), rootcount: node() };"
        "const island = globalThis.document.getElementById;"
        "globalThis.document.getElementById = (id) => owned[id] || island(id);"
        "globalThis.document.createElement = () => node();" + page_js,
    )
    assert ROOT in drawn, f"the root board's name is not on the page: {drawn}"
    assert WORKER in drawn, f"the worker board's name is not on the page: {drawn}"
    assert "ship the page host" in drawn, "the root's goal, as its own log recorded it"
    assert "write the page" in drawn, "the worker's goal"
    assert "a board" not in drawn, (
        "a row fell back to its placeholder, so the page read a key the real fold does " "not emit"
    )


def test_the_item_count_is_a_number_and_the_page_prints_it(page: dict[str, Any]) -> None:
    """``items`` is a COUNT in a folded row, not the items.

    Read off the fold rather than assumed, which is the whole point of this file: the
    capture harness and the route stub both built it as a list of dicts, and a page that
    took ``.length`` of a number would print nothing.
    """
    value = _planted_tree()
    root = [row for row in value["boards"] if row["board"] == ROOT][0]
    assert isinstance(root["items"], int) and not isinstance(
        root["items"], bool
    ), f"the fold's items member is {type(root['items']).__name__}, not a count"
    html = page["view"]["html"]
    assert "board.items || 0" in html, "the page does not print the row's own item count"
    # The regression this guards: taking `.length` of a COUNT prints nothing.
    assert "board.items.length" not in html, "the page treats the count as a list"
    # The count carries its unit, so a bare number is never floated beside a
    # board name, and the singular is right at one. "own" is part of the unit: the
    # UX read of a bare "9 items" on a parent row was that it covered the rows
    # underneath, which their own counts then contradicted.
    assert (
        "' own item' : ' own items'" in html
    ), "the row's count is printed without its unit, or without whose items it counts"


def test_the_real_rows_carry_no_key_the_page_still_expects(page: dict[str, Any]) -> None:
    """Named explicitly, so a reader of the earlier page sees these are gone for good."""
    value = _planted_tree()
    emitted = set(value["boards"][0])
    for absent in ("slot_key", "on_cycle", "entries"):
        assert absent not in emitted, f"the fold now emits {absent!r}; this test is stale"
        assert absent not in page["view"]["html"], f"the page still reads {absent!r}"


def test_the_planted_tree_is_what_the_capture_harness_claims(tmp_path: Any) -> None:
    """The harness builds rows by hand for the stills. Its row keys must be the real
    ones, or the published evidence shows a page the product cannot produce -- which is
    exactly what the first two takes did.
    """
    value = _planted_tree()
    emitted = set(value["boards"][0])
    # The keys the stills are built from, written here as the contract the harness must
    # satisfy. The harness lives outside the repository, so this is the pin that travels.
    harness: dict[str, type | tuple[type, ...]] = {
        "board": str,
        "parent": type(None),
        "parent_item": type(None),
        "cycle": bool,
        "depth": int,
        "goal": str,
        "items": int,
        "generation": str,
    }
    row = value["boards"][0]
    assert set(harness) == emitted, (
        f"a folded row carries {sorted(emitted)}; the stills are built from "
        f"{sorted(harness)}, so the published evidence would not match the product"
    )
    # TYPES TOO, because the key set matching is what let `items` be built as a list of
    # dicts while the fold emits a count -- identical keys, and a different page.
    wrong = {
        k: type(row[k]).__name__ for k, want in harness.items() if not isinstance(row[k], want)
    }
    assert not wrong, f"the harness builds the wrong type for {wrong}"
    assert json.dumps(row)  # serializes, as a frame must
