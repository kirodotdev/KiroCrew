"""The gallery's dispatch-tree page: the first thing that consumes the tree fold.

``read_tree_projection`` shipped GATED -- "no production caller reaches it yet ... this
read ships with the first dashboard consumer that can bound the walk". This page is that
consumer, so the claims worth pinning are the ones that would make the gate a lie:

* the page is a package the write gate accepts, so an owner can copy it and it will save;
* it reads the tree fold by its real field names, so the fields resolve rather than
  rendering a tree of em dashes;
* it is served through ``dashboard_frame``, bindings and all;
* a value holding markup stays text on the page, which is the one property a reference
  page must not teach an author to break.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.artifact_store import dashboard_package as dp
from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

GALLERY = (
    Path(mdf.__file__).resolve().parents[2]
    / "builtin_skills"
    / "dashboard"
    / "gallery"
    / "dispatch-tree"
    / "package.json"
)


@pytest.fixture(scope="module")
def page() -> dict[str, Any]:
    """The stored gallery package, through the same gate a save would use."""
    return dp.parse_package(GALLERY.read_text(encoding="utf-8"))


def test_the_gallery_page_is_a_package_the_write_gate_accepts(page: dict[str, Any]) -> None:
    """A reference page an owner cannot save is worse than no reference page: they copy
    it, the save is refused, and the refusal reads as their mistake."""
    assert page["view"]["html"]
    assert dp.canonical_package_content(json.dumps(page))


def test_it_subscribes_to_the_tree_fold_and_the_work_ledger(page: dict[str, Any]) -> None:
    sources = dp.subscribed_fields(page)
    assert sources["boards"] == "fold:worktree.boards"
    assert sources["roots"] == "fold:worktree.roots"
    assert sources["work_items"] == "fold:work.items"


def test_every_path_it_binds_is_a_field_the_fold_really_carries(page: dict[str, Any]) -> None:
    """Pinned against the fold's own declared field list, not against what the page
    expects. A page naming a key the fold does not carry resolves to nothing, and the
    page then draws an empty tree over a log that has the boards in it."""
    catalog = json.loads(
        (GALLERY.parents[3] / "kirocrew-dev" / "dashboard-template" / "folds.json").read_text(
            encoding="utf-8"
        )
    )
    folds = catalog["folds"] if isinstance(catalog, dict) else catalog
    carried = {
        entry["name"]: {field["name"] for field in entry.get("fields", [])} for entry in folds
    }
    for name, spec in (page["model"]["types"]).items():
        source = spec["source"]
        if source.get("agentic"):
            continue
        head = source["path"].split(".")[0]
        assert head in carried[source["fold"]], (
            f"field {name!r} reads {source['fold']}.{source['path']}, and the "
            f"{source['fold']} fold carries no {head!r}"
        )


def test_the_tree_fold_is_the_one_with_no_bus_scope(page: dict[str, Any]) -> None:
    """Which is why this page needs a read rather than a subscription: a tree joins the
    logs of many slots, so its value is stale when any member's log grows and there is
    no key an event could carry to say which."""
    from kiro_crew.crew_log.projection import TREE_PROJECTION_NAMES
    from kiro_crew.dashboard_feed import scope_for

    assert "worktree" in TREE_PROJECTION_NAMES
    assert scope_for("worktree") == ""


def test_the_page_draws_from_subscribe_rather_than_polling(page: dict[str, Any]) -> None:
    """The reason the page updates live at all. A reference page that polled would teach
    every copy of it to poll."""
    html = page["view"]["html"]
    assert "window.kirocrew.subscribe('boards'" in html
    assert "setInterval" not in html and "fetch(" not in html


def test_the_page_builds_its_rows_as_text_never_as_markup(page: dict[str, Any]) -> None:
    """A board's goal is whatever a conductor wrote in the crew log. The reference page
    is the thing authors copy, so it has to show the safe spelling: elements created and
    ``textContent`` set, never a template string assigned to ``innerHTML``."""
    html = page["view"]["html"]
    assert "textContent" in html
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in html, f"the gallery page reaches {sink}"


def test_it_is_served_through_the_frame_contract(page: dict[str, Any]) -> None:
    from kiro_crew import dashboard_frame

    read = dashboard_frame.read_payload({"roots": 2, "cycles": 0, "unresolved": 0})
    body = mdf.render_package_page(page, read)
    assert page["view"]["html"] in body, "the page is not placed verbatim"
    assert dashboard_frame.BINDING_ATTRIBUTE in body
    assert "kirocrew-dashboard-data" in body
    assert body.index("kirocrew-dashboard-data") < body.index(
        page["view"]["html"]
    ), "the data island follows the page, so the page's own first script reads undefined"


def test_a_board_goal_holding_markup_is_not_rendered_as_markup() -> None:
    """End to end through the host's half: a value the log recorded reaches the document
    escaped, so a goal carrying a tag is text in the island and text in the cell."""
    from kiro_crew import dashboard_frame

    read = dashboard_frame.read_payload({"roots": "<img src=x onerror=alert(1)>"})
    body = dashboard_frame.compose_body('<p data-dashboard-field="roots"></p>', read)
    assert "<img src=x" not in body
    assert "\\u003cimg src=x" in body


def test_the_gallery_page_fits_the_view_cap(page: dict[str, Any]) -> None:
    assert len(page["view"]["html"].encode("utf-8")) < dp.MAX_VIEW_HTML_BYTES


def test_every_row_key_the_page_reads_is_one_the_fold_emits(page: dict[str, Any]) -> None:
    """The defect this exists to stop: the page read `slot_key`, `on_cycle` and
    `entries`, and the fold emits `board`, `cycle` and `items`. Every row of a copied
    page showed "a board" with a count of zero and no cycle flag.

    Two layers of checking missed it. The gallery test above compares the first segment
    of each bound PATH against the fold's field list, which says nothing about the keys
    inside a row. And the route's end-to-end test stubs `read_tree_projection` with a
    value this repository wrote, so an invented row shape satisfied it.

    So the keys are taken from the fold's OWN renderer here. `_worktree_render` builds
    one row literal, and the set of keys in that literal is the contract; a page reading
    anything outside it draws a blank.
    """
    import inspect

    from kiro_crew.crew_log import projection

    source = inspect.getsource(projection._worktree_render)
    # The row literal's keys, read off the renderer rather than listed here, so a key
    # the fold renames fails this test instead of silently emptying the page.
    emitted = set(re.findall(r'^\s*"([a-z_]+)":', source, re.MULTILINE))
    assert {
        "board",
        "cycle",
        "items",
        "depth",
        "goal",
    } <= emitted, f"the renderer no longer emits the keys this test is built on: {sorted(emitted)}"
    html = page["view"]["html"]
    # Every `board.<key>` the page reads must be one of them.
    read = set(re.findall(r"\bboard\.([a-z_]+)", html))
    unknown = read - emitted
    assert not unknown, (
        f"the gallery page reads {sorted(unknown)} off a board row, which the worktree "
        f"fold does not emit (it emits {sorted(emitted)}). Every copied page would draw "
        "a blank there."
    )
    assert read, "the page reads no board field at all"


def test_the_page_does_not_read_the_keys_that_were_wrong(page: dict[str, Any]) -> None:
    """Named explicitly, because the general check above passes the moment a key is
    removed and these three are the ones a reader of the old page will look for."""
    html = page["view"]["html"]
    for gone in ("slot_key", "on_cycle", "board.entries"):
        assert gone not in html, f"the page still reads {gone!r}, which no fold emits"


class TestWhatTheStaticPageSays:
    """With scripts withheld, the page must not make a claim only a script could
    have corrected.

    The rows are drawn by `subscribe('boards', draw)`. An empty line that ships
    VISIBLE and is hidden once rows arrive makes a scripts-off page report "No
    board has dispatched another yet." about a tree that HAS boards -- a false
    statement about the owner's own data, which is worse than showing less.

    So the line ships HIDDEN and the script reveals it. It appears only because a
    script counted zero boards, which is the one thing it describes.
    """

    def test_the_empty_line_ships_hidden(self, page: dict[str, Any]) -> None:
        html = page["view"]["html"]
        assert 'id="empty" class="empty" hidden' in html, (
            "the empty line ships visible, so a scripts-off page would claim an "
            "empty tree whatever the data says"
        )

    def test_the_script_is_what_reveals_it(self, page: dict[str, Any]) -> None:
        """Hidden markup plus nothing to unhide it would be a line that never
        shows, which is the opposite failure."""
        assert "empty.hidden = rows.length > 0" in page["view"]["html"]

    def test_a_noscript_block_explains_the_static_page(self, page: dict[str, Any]) -> None:
        """A sandboxed frame WITHOUT allow-scripts renders <noscript> content, so
        this is the one message guaranteed to reach a reader of a static page."""
        html = page["view"]["html"]
        assert "<noscript>" in html and "</noscript>" in html
        block = html.split("<noscript>")[1].split("</noscript>")[0]
        # Plain words: what is missing and why, with no jargon for the cause.
        assert "script" in block.lower()
        assert "network policy" in block.lower()
        # It must not repeat the false claim it exists to replace.
        assert "No board has dispatched another yet" not in block

    def test_the_counts_do_not_depend_on_script(self, page: dict[str, Any]) -> None:
        """Why the noscript text can honestly call the counts current: they are
        `data-dashboard-field` bindings, which the HOST fills as textContent in
        the composed document before the frame ever runs."""
        html = page["view"]["html"]
        for field in ("cycles", "unresolved"):
            assert f'data-dashboard-field="{field}"' in html

    def test_the_header_counts_ship_hidden_too(self, page: dict[str, Any]) -> None:
        """The same rule as the empty line, for the same reason.

        MEASURED in a frame with the empty grant set: the sub-line read
        "0 roots, on a cycle, unresolved" -- the `0 roots` literal contradicted a
        tree that had one root, and the two `data-dashboard-field` spans rendered
        EMPTY because the frame's own bootstrap fills them, not the host. So the
        header is a claim only a script could correct, and it ships hidden.
        """
        html = page["view"]["html"]
        assert 'class="sub" id="sub" hidden' in html, (
            "the header ships visible, so a scripts-off page states a root count "
            "the data does not support"
        )
        assert "sub.hidden = false" in html, "nothing reveals the header once filled"

    def test_the_noscript_text_does_not_promise_counts(self, page: dict[str, Any]) -> None:
        """It said the counts were "current" in an earlier revision, which the
        measurement above disproved."""
        block = page["view"]["html"].split("<noscript>")[1].split("</noscript>")[0]
        assert "not the counts" in block
        assert "are current" not in block

    def test_the_noscript_text_names_someone_who_can_lift_the_restriction(
        self, page: dict[str, Any]
    ) -> None:
        """A dead end must end in an action the reader can take.

        The earlier revision closed with "Open the dispatch tree from a crew whose
        network access is unrestricted", which the UX lane's blind reader could not
        act on: it named no person, and "unrestricted" is the posture's own word,
        not a reader's. The restriction is the administrator's to lift, so the text
        says to ask them, and names no product object the UI does not label.
        """
        block = page["view"]["html"].split("<noscript>")[1].split("</noscript>")[0]
        assert "Ask whoever administers" in block, "the notice ends in no action"
        assert "unrestricted" not in block, "the notice uses the posture's own word"
        assert "board list" not in block, "the notice names a thing nothing is labelled"

    def test_the_noscript_block_is_a_notice_not_a_footnote(self, page: dict[str, Any]) -> None:
        """It shared `.empty`'s muted grey, and read as a caption worth skipping.

        Its own class, carrying a rule and a tint rather than the muted colour, so
        the one message a scripts-off reader can see does not look like a footnote.
        """
        html = page["view"]["html"]
        block = html.split("<noscript>")[1].split("</noscript>")[0]
        assert 'class="notice"' in block, "the notice is still styled as muted text"
        assert ".notice {" in html, "the notice class has no rule"
        assert "border-left: 2px solid" in html.split(".notice {")[1].split("}")[0]
