"""Folder order on the kirocrew-dashboard server: where a folder sits among its siblings.

``chat_folder_move``'s ``before``/``after`` anchors, the renumber an app may not
half-apply, and the tree listed in the order the sidebar draws, under the person's
folder sort mode. The tool and the sidebar compare names identically, checked
against the golden fixture both suites read.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from mcp_dashboard_folders_helpers import (
    _FOLDERS,
    _SLOTS,
    _call,
    _reads,
    _slots_with_caller,
    _writes,
)

from kiro_crew import mcp_dashboard
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.mcp_dashboard import _list_tools
from kiro_crew.mcp_tools.dashboard_client import DashboardRequest, InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller


def _set_folder_sort(value: str) -> None:
    """Write the person's folder sort mode where the dashboard keeps it."""
    (config_dir() / "config.json").write_text(
        json.dumps({"dashboard": {"folder_sort": value}}), encoding="utf-8"
    )


def _config_answering(value: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the config loader answer ``value`` verbatim, or raise it.

    For the two readings a real config file cannot produce: a value the loader
    would never store, and a loader that fails outright.
    """

    def _load(*_args: object, **_kwargs: object) -> Any:
        if isinstance(value, BaseException):
            raise value
        return type("Cfg", (), {"dashboard": type("Dash", (), {"folder_sort": value})()})()

    monkeypatch.setattr(KiroCrewConfig, "load", _load)


#: Four root folders with explicit, contiguous positions — the shape a sidebar
#: drag leaves behind (``computeReorderedFolders`` renumbers 0..n-1), so these
#: cases assert the tool writes what a drag would have written.
_ORDERED = [
    {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
    {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
    {"id": "cccccccccccc", "name": "Charlie", "parent_id": "", "order": 2},
    {"id": "dddddddddddd", "name": "Delta", "parent_id": "", "order": 3},
]


def _positioning(
    folders: list[dict] | None = None,
    slots: list[dict] | None = None,
    patch: Any = None,
    reorder: Any = None,
) -> dict[str, Any]:
    """Routes for a ``chat_folder_move`` that may PATCH a row and POST a reorder.

    A PATCH answers with the row it names, merged with what was written, unless
    ``patch`` says otherwise; the reorder answers ``{"ok": True}`` unless
    ``reorder`` does.
    """
    rows = _ORDERED if folders is None else folders

    def _patch_row(req: DashboardRequest) -> dict:
        fid = req.path.rsplit("/", 1)[-1]
        row = next((dict(f) for f in rows if f["id"] == fid), {"id": fid})
        return {**row, **(req.body or {})}

    return {
        **_reads(folders=rows, slots=_slots_with_caller() if slots is None else slots),
        "PATCH /api/chat/folders/{folder}": _patch_row if patch is None else patch,
        "POST /api/chat/folders/reorder": {"ok": True} if reorder is None else reorder,
    }


def _patched_orders(dash: InMemoryDashboardClient) -> dict[str, int]:
    """``{folder_id: order}`` over every PATCH the call issued."""
    return {
        r.path.rsplit("/", 1)[-1]: r.body["order"]
        for r in dash.sent("PATCH /api/chat/folders/{folder}")
        if "order" in r.body
    }


def _posted_orders(dash: InMemoryDashboardClient) -> dict[str, int]:
    """``{folder_id: order}`` over every atomic reorder POST the call issued.

    The renumber path sends the whole ``{"orders": [{id, order}, ...]}`` list to
    ``/api/chat/folders/reorder`` in ONE request, so a renumber is read off that
    route rather than off per-row PATCHes.
    """
    return {
        str(entry["id"]): entry["order"]
        for r in dash.sent("POST /api/chat/folders/reorder")
        for entry in r.body.get("orders", [])
    }


class TestTheTwoSidesCompareNamesIdentically:
    """The tool's sort key and the sidebar's comparator must not disagree.

    `chat_folder_tree` is where an agent picks a `before`/`after` anchor, so a
    sequence that differs from the rendered one makes the anchor point at the
    wrong gap.

    The key consults no Unicode table. `str.lower()` reads the interpreter's tables
    and `String.prototype.toLowerCase` reads the browser's, so a character whose case
    mapping differs between those two versions would fold differently on each side,
    and neither side owns both tables.

    `A`-`Z` is the exception, folded through a literal table here and by arithmetic on
    the frontend. That range is fixed in every Unicode version, so folding it costs no
    version dependency — and a store written before `order` existed has every sibling
    tied at 0, which makes this tie-break the whole sort for those sidebars.
    """

    def test_ascii_case_folds_so_ordering_stays_alphabetical(self) -> None:
        """`Apple` belongs next to `apricot`, not ahead of every lowercase name."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "apricot", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Apple", "parent_id": "", "order": 0},
            {"id": "cccccccccccc", "name": "banana", "parent_id": "", "order": 0},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["Apple", "apricot", "banana"]

    def test_only_ascii_letters_are_folded(self) -> None:
        """Pins the fold's exact reach, so widening it back to a table is caught.

        Every character outside `A`-`Z` must survive byte-for-byte: those are the
        ones whose case mapping can differ between the two runtimes.
        """
        for name in ("ßeta", "İstanbul", "Éclair", "ǅ", "straße"):
            key = mcp_dashboard._chat_folder_name_key({"name": name})
            assert key == name.encode("utf-16-be", "surrogatepass"), name
        assert mcp_dashboard._chat_folder_name_key({"name": "STRASSE"}) == (
            "strasse".encode("utf-16-be")
        )

    def test_a_dotted_capital_i_is_not_folded(self) -> None:
        """The concrete skew shape: a character whose fold is version-dependent.

        `İ` (LATIN CAPITAL I WITH DOT ABOVE) lowercases to a two-character
        sequence in Python, and the exact result has moved across Unicode versions.
        Unfolded it is one code unit on both sides and cannot skew.
        """
        key = mcp_dashboard._chat_folder_name_key({"name": "İ"})
        assert key == b"\x01\x30"
        assert len(key) == 2

    def test_an_astral_name_sorts_by_utf16_code_unit_not_code_point(self) -> None:
        """Python orders str by code point; JavaScript orders by UTF-16 code unit.

        Above U+FFFF the two disagree: an astral character's surrogates begin at
        0xD800, so U+1F600 sorts AFTER U+FF21 by code point and BEFORE it by code
        unit. The frontend comparator uses `<` on strings, so the tool has to speak
        code units or the anchor an agent picks lands in the wrong gap.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "\U0001f600", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Ａ", "parent_id": "", "order": 0},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["\U0001f600", "Ａ"]
        # And the naive key would have put them the other way round.
        assert sorted((str(r["name"]) for r in rows)) == ["Ａ", "\U0001f600"]

    def test_the_key_is_utf16_bytes(self) -> None:
        """Pins the encoding, so a future edit back to a str key is caught."""
        key = mcp_dashboard._chat_folder_name_key({"name": "Ab\U0001f600"})
        assert key == "ab\U0001f600".encode("utf-16-be")


def _fixture_name(row: dict) -> dict:
    """Materialise a fixture row's `name_code_units` into a `name`.

    An unpaired surrogate is legal in a JSON string escape but strict parsers
    reject it, so the shared fixture carries that one name as UTF-16 code units
    and each side builds the identical string from them.
    """
    units = row.get("name_code_units")
    if units is None:
        return {}
    return {"name": "".join(chr(u) for u in units)}


class TestFolderPosition:
    """``before``/``after`` set a folder's place among its siblings.

    The position is written as contiguous 0..n-1 over the destination's
    siblings, which is exactly what a sidebar drag writes — so a tool call and a
    drag leave one convention in the store rather than two.
    """

    def test_after_an_anchor_lands_immediately_behind_it(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:")
        # Alpha 0, Delta 1, Bravo 2, Charlie 3 -- the whole renumber, including the
        # moved row, lands in ONE atomic reorder request rather than per-row PATCHes.
        assert _posted_orders(dash) == {
            "dddddddddddd": 1,
            "bbbbbbbbbbbb": 2,
            "cccccccccccc": 3,
        }
        # No reparent PATCH: the folder is already at the top level, so nothing
        # but the atomic reorder is written.
        assert not dash.sent("PATCH /api/chat/folders/{folder}")
        assert "after `Alpha`" in out

    def test_before_an_anchor_lands_immediately_ahead_of_it(self) -> None:
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Bravo"}, _positioning()
        )
        assert not out.startswith("Error:")
        assert _posted_orders(dash) == {
            "dddddddddddd": 1,
            "bbbbbbbbbbbb": 2,
            "cccccccccccc": 3,
        }
        assert not dash.sent("PATCH /api/chat/folders/{folder}")
        assert "before `Bravo`" in out

    def test_the_renumber_states_its_container_to_the_endpoint(self) -> None:
        """The reorder POST carries ``expected_parent`` naming the destination.

        The batch is computed from a snapshot of the tree, so the claim is what
        lets the endpoint refuse the renumber (409) when a concurrent reparent
        moves a sibling between that read and the write. The destination here is
        the root lane, and the claim for it is the empty string -- a real value,
        present in the body, not an omitted key.
        """
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:")
        (reorder,) = dash.sent("POST /api/chat/folders/reorder")
        assert reorder.body["expected_parent"] == ""

    def test_an_anchor_alone_reorders_without_moving(self) -> None:
        """The reason an anchor may stand in for ``new_parent``.

        An omitted ``new_parent`` means the TOP LEVEL, so demanding one would
        make repositioning inside a folder inexpressible: every call would drag
        the folder out to the root as the price of ordering it.
        """
        nested = [
            {"id": "pppppppppppp", "name": "Parent", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "pppppppppppp", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "pppppppppppp", "order": 1},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Parent/Bravo", "before": "Parent/Alpha"},
            _positioning(folders=nested),
        )
        assert not out.startswith("Error:")
        # Stays inside Parent — the anchor chose the destination. Alpha sits at 0
        # and nothing precedes it, so one write puts Bravo ahead of it; and since
        # Bravo stays inside Parent, parent_id is omitted and only the position
        # is written.
        assert [r.body for r in _writes(dash)] == [{"order": -1}]

    def test_only_folders_whose_position_changes_are_written(self) -> None:
        """A no-op reposition must not spend a write per sibling."""
        out, dash = _call("chat_folder_move", {"folder": "Bravo", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:")
        # Bravo already sits right after Alpha, so every sibling keeps its
        # number -- and with the same parent AND the position it already holds,
        # there is nothing to write at all.
        assert _writes(dash) == []

    def test_a_same_parent_reposition_is_not_reported_as_a_move(self) -> None:
        """ "Moved to `Delta`" would name the folder's OWN path as a destination.

        The reparent did not happen, so the result names what did change.
        """
        out, _ = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert out.startswith("Repositioned folder")
        assert "after `Alpha`" in out and "(top level)" in out
        assert "Moved" not in out

    def test_a_reparent_that_also_positions_says_both(self) -> None:
        out, _ = _call(
            "chat_folder_move",
            {"folder": "Travel", "after": "kirocrew/0811"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert out.startswith("Moved folder")
        assert "kirocrew/Travel" in out and "after `kirocrew/0811`" in out

    def test_a_free_slot_makes_the_whole_reposition_one_write(self) -> None:
        """The answer to "several writes cannot be atomic": usually there is one.

        Several writes CAN land half-applied, since the endpoint takes one row at a
        time. So a position the store already has room for is written as a single
        PATCH and cannot be partial at all; only adjacent neighbours force the
        renumber. Alpha is first, so the slot ahead of it is free.
        """
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Alpha"}, _positioning()
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 1
        assert _patched_orders(dash) == {"dddddddddddd": -1}

    def test_a_gap_between_neighbours_is_used_instead_of_renumbering(self) -> None:
        """A deleted folder leaves a gap, and a gap is a free slot."""
        gapped = [
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 10},
            {"id": "dddddddddddd", "name": "Delta", "parent_id": "", "order": 20},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Delta", "after": "Alpha"},
            _positioning(folders=gapped),
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 1
        # Midpoint of the 0..10 gap, so a later insert on either side still fits.
        assert _patched_orders(dash) == {"dddddddddddd": 5}

    def test_an_app_cannot_position_a_folder_whose_subtree_holds_a_foreign_one(self) -> None:
        """Positioning takes the descendants with it, so the blast radius is the subtree.

        The app owns the row it names, so the moved-folder check passes. But the
        person's folder is nested inside it, and repositioning the parent relocates
        the child -- the same violation the endpoint refuses on a reparent, reached
        one level down. Placing AppRoot after AppTwo has a free slot, so it is a
        single order PATCH on AppRoot's own row; the endpoint refuses that write
        because AppRoot's subtree holds the person's folder, and the tool surfaces
        the refusal rather than pre-checking it.
        """
        rows = [
            {
                "id": "aaaaaaaaaaaa",
                "name": "AppRoot",
                "parent_id": "",
                "order": 0,
                "owner_app": "x",
            },
            {"id": "pppppppppppp", "name": "Person", "parent_id": "aaaaaaaaaaaa", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 1, "owner_app": "x"},
        ]
        refused = {"error": "this app does not own that folder", "code": "folder_not_owned"}
        out, dash = _call(
            "chat_folder_move",
            {"folder": "AppRoot", "after": "AppTwo"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}], patch=refused),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # The order PATCH on AppRoot was attempted and refused by the endpoint's
        # subtree guard, not pre-empted in the tool.
        assert dash.sent("PATCH /api/chat/folders/{folder}")

    def test_a_renumber_that_rewrites_a_foreign_row_is_refused_by_the_endpoint(
        self,
    ) -> None:
        """Ownership lives in the endpoint, not a tool-layer pre-check.

        When a renumber's batch includes a row the app does not own, the reorder
        endpoint re-validates every row under the store lock and refuses the whole
        batch, leaving the order untouched. The tool does not pre-check this; it
        sends the batch and surfaces the endpoint's atomic refusal.

        Moving AppTwo just after AppOne renumbers the contiguous 0,1,2 set, so
        Person's row (the person's, not the app's) is one of the writes -- which is
        what the endpoint refuses.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "AppOne", "parent_id": "", "order": 0, "owner_app": "x"},
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 1},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        refused = {
            "error": "this app does not own one of those folders",
            "code": "folder_not_owned",
        }
        out, dash = _call(
            "chat_folder_move",
            {"folder": "AppTwo", "after": "AppOne"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}], reorder=refused),
            Caller.strict("dashboard:chat-1-1"),
        )
        # A refused renumber is reported on the reposition line (not an "Error:"
        # prefix): the reparent, if any, landed and the ordering did not.
        assert "ordering was refused" in out, out
        assert "does not own" in out, out
        assert "stored order is unchanged" in out, out
        # The batch really did name Person (the foreign row), so the endpoint had
        # something to refuse -- the renumber is not silently app-only.
        assert "pppppppppppp" in _posted_orders(dash), out

    def test_an_app_cannot_position_a_folder_it_does_not_own(self) -> None:
        """The tool refuses the position BEFORE any write -- the pinned no-write case.

        Positioning is relative, so it does not need a write to its own target: a
        pure reposition sends no `parent_id`, so the endpoint's reparent rule never
        fires, and renumbering the app's OWN siblings around the person's folder
        changes where the person's folder renders with no write to it for the
        endpoint to refuse. The moved-folder ownership refusal therefore lives in
        the tool, and it fires before any PATCH or reorder is issued -- this test
        pins that no write is attempted at all.
        """
        rows = [
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "AppOne", "parent_id": "", "order": 1, "owner_app": "x"},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Person", "after": "AppTwo"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}]),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # No write of any kind: not the free-slot PATCH on the moved row, and not a
        # batched reorder of the app's siblings around it. The refusal is the tool's,
        # not the endpoint's, because a relative renumber can name only owned rows.
        assert _writes(dash) == []

    def test_a_relative_renumber_around_a_foreign_folder_is_refused_with_no_write(
        self,
    ) -> None:
        """The exact reachable gap: the moved row keeps its order, only siblings write.

        Person(order 1) sits between AppA(0) and AppB(2). Placing Person after AppA
        leaves Person at the index it already occupies, so `own_pos is None` and no
        PATCH names Person; without the moved-folder refusal the only writes would
        renumber the app's OWN siblings, every one owned, and both endpoints would
        allow it -- the person's folder relocated by an app with nothing refused.
        The tool-layer moved-folder check closes this: it refuses before computing
        or issuing any write.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "AppA", "parent_id": "", "order": 0, "owner_app": "x"},
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 1},
            {"id": "bbbbbbbbbbbb", "name": "AppB", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Person", "after": "AppA"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}]),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        assert _writes(dash) == []

    def test_a_relative_renumber_of_a_folder_with_a_foreign_subtree_is_refused_with_no_write(
        self,
    ) -> None:
        """The subtree half of the same gap: the moved folder is owned, its child is not.

        AppMid(order 1, app-owned) sits between AppA(0) and AppB(2) and holds the
        person's PersonKid inside it. Placing AppMid after AppA leaves it at index 1,
        so `own_pos is None` and no write names AppMid; the moved-folder OWNERSHIP
        check passes (the app owns AppMid), and without the subtree check the only
        writes would renumber owned siblings, so both endpoints would allow it -- the
        person's nested folder relocated with nothing refused. The tool-layer
        moved-folder SUBTREE check closes this: it refuses before any write.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "AppA", "parent_id": "", "order": 0, "owner_app": "x"},
            {"id": "mmmmmmmmmmmm", "name": "AppMid", "parent_id": "", "order": 1, "owner_app": "x"},
            {"id": "pppppppppppp", "name": "PersonKid", "parent_id": "mmmmmmmmmmmm", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "AppB", "parent_id": "", "order": 2, "owner_app": "x"},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "AppMid", "after": "AppA"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}]),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # No write of any kind: the subtree refusal fires before the renumber.
        assert _writes(dash) == []

    def test_a_row_deeper_than_the_sidebar_draws_is_listed_at_the_cap_depth(self) -> None:
        """Clamp the indentation, keep the row.

        `renderFolderBlock` returns nothing for `depth > 10`, and the store caps
        folder count but not nesting, so a deeper chain is legal. Reporting depth 14
        would claim a level the sidebar never draws — but omitting the row would hide
        the folder AND the live sessions filed in it from an agent's only tree view,
        to buy indentation parity on a row nobody can see.
        """
        rows = [
            {
                "id": f"{i:012d}",
                "name": f"L{i}",
                "parent_id": "" if i == 0 else f"{i - 1:012d}",
                "order": 0,
            }
            for i in range(14)
        ]
        order = mcp_dashboard._chat_folder_render_order(rows)
        assert len(order) == 14, "every folder is listed"
        depths = [d for _fid, d in order]
        assert max(depths) == mcp_dashboard._SIDEBAR_MAX_DRAWN_DEPTH
        assert depths.count(0) == 1, "the deep rows are not relocated to the top level"
        assert depths == list(range(11)) + [10, 10, 10]

    def test_a_saturated_edge_has_no_free_slot(self) -> None:
        """One past the bound reads back AS the bound, so it is not outside anything.

        `_free_slot_order` earns its single write by naming an order no sibling
        holds. At the numeric limit that is impossible: the value it would return
        comes back through the same clamp as the anchor's own order, so the pair
        ties and the name tie-break — not the requested side — decides where the
        folder lands. There is no representable slot, so the caller must renumber.
        """
        limit = mcp_dashboard._CHAT_FOLDER_ORDER_LIMIT
        at_top = [{"id": "aaaaaaaaaaaa", "name": "A", "parent_id": "", "order": limit}]
        at_bottom = [{"id": "bbbbbbbbbbbb", "name": "B", "parent_id": "", "order": -limit}]
        assert mcp_dashboard._free_slot_order(at_top, 1) is None
        assert mcp_dashboard._free_slot_order(at_bottom, 0) is None
        # The other edge of each is still free: only the saturated side is refused.
        assert mcp_dashboard._free_slot_order(at_top, 0) == limit - 1
        assert mcp_dashboard._free_slot_order(at_bottom, 1) == -limit + 1

    def test_the_shared_golden_fixture_orders_identically_on_this_side(self) -> None:
        """One fixture, both suites — see test/fixtures/chat_folder_sibling_order.json.

        `folderTree.test.ts` drives these same rows through `bySidebarOrder`, so the
        agreement between the tool's order and the sidebar's is checked by one
        artifact rather than asserted in prose on each side. A coercion that changes
        on either side fails one of the two runs.
        """
        spec = json.loads(
            (
                pathlib.Path(__file__).parent / "fixtures" / "chat_folder_sibling_order.json"
            ).read_text()
        )
        modes_seen: set[str] = set()
        for case in spec["cases"]:
            rows = [{**r, "parent_id": "", **_fixture_name(r)} for r in case["rows"]]
            mode = case.get("mode", "custom")
            modes_seen.add(mode)
            got = [f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "", mode)]
            assert got == case["expected"], case["name"]
            # The listing walk is the sort the tree tool prints; it must agree
            # with the sibling sort for the same mode, at the root as at depth.
            walked = [fid for fid, _depth in mcp_dashboard._chat_folder_render_order(rows, mode)]
            assert walked == case["expected"], f"render order: {case['name']}"
        # Every mode the loader admits is exercised by at least one case, so a
        # fourth mode cannot land with no parity row.
        assert modes_seen == set(mcp_dashboard.FOLDER_SORT_MODES)

    def test_no_persisted_order_shape_can_raise_out_of_the_sort_key(self) -> None:
        """Totality over the store, checked as a set rather than one shape at a time.

        The folder store is read with a bare `json.loads`, so `order` can arrive as
        anything JSON expresses. `1e999` parses to `inf`, and `int(inf)` raises
        `OverflowError` — which is neither `TypeError` nor `ValueError`. An exception
        escaping a SORT KEY aborts the whole sort, so one row would take down every
        folder tool.
        """
        junk = [float("inf"), float("-inf"), float("nan"), "abc", [1], {"a": 1}, None, True]
        for value in junk:
            row = {"id": "aaaaaaaaaaaa", "name": "X", "parent_id": "", "order": value}
            assert isinstance(mcp_dashboard._chat_folder_order(row), int), value
            # And through the sorts that consume it, which is where a raise lands.
            assert mcp_dashboard._chat_folder_siblings([row], "") == [row], value
            assert mcp_dashboard._chat_folder_render_order([row]) == [("aaaaaaaaaaaa", 0)], value

    def test_a_lone_surrogate_name_does_not_crash_the_sort_key(self) -> None:
        """A raised encoder inside a sort key takes down every folder tool.

        A folder name is persisted JSON, so it can hold a LONE surrogate, and the
        strict `utf-16-be` codec refuses one outright. `surrogatepass` both survives
        it and keeps the byte-for-byte match with the frontend, whose string holds
        that same unit and compares it as 0xD800.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "a\ud800b", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "plain", "parent_id": "", "order": 1},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["a\ud800b", "plain"]
        key = mcp_dashboard._chat_folder_name_key({"name": "a\ud800b"})
        assert key == b"\x00a\xd8\x00\x00b"

    def test_a_same_parent_reposition_sends_no_parent_id(self) -> None:
        """The endpoint reads a present `parent_id` as a reparent.

        It then applies the reparent-only rule that a subtree holding a folder the
        caller does not own cannot be moved. Sending the CURRENT parent back would
        put a pure reposition through a guard about a move that is not happening,
        and an app reordering its own folder that contains one of the person's would
        be refused for no reason.
        """
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Alpha"}, _positioning()
        )
        assert not out.startswith("Error:")
        for write in dash.sent("PATCH /api/chat/folders/{folder}"):
            assert "parent_id" not in write.body, write.body

    def test_a_real_reparent_still_sends_parent_id(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "new_parent": "kirocrew"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert not out.startswith("Error:")
        assert dash.sent("PATCH /api/chat/folders/{folder}")[0].body == {
            "parent_id": "aaaaaaaaaaaa"
        }

    def test_both_anchors_at_once_is_refused(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Delta", "before": "Alpha", "after": "Bravo"},
            _positioning(),
        )
        assert out.startswith("Error:") and "not both" in out
        assert _writes(dash) == []

    def test_an_anchor_outside_the_destination_is_refused(self) -> None:
        """``before``/``after`` names a SIBLING, so it must live in the destination."""
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "new_parent": "root", "after": "kirocrew/0811"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert out.startswith("Error:") and "not in the destination" in out
        assert _writes(dash) == []

    @pytest.mark.parametrize(
        "anchor,why",
        [
            pytest.param("Delta", "relative to itself", id="a-folder-cannot-anchor-on-itself"),
            pytest.param("root", "SIBLING", id="root-is-not-an-anchor"),
            pytest.param("Echo", "folder not found", id="a-missing-anchor-is-refused"),
        ],
    )
    def test_a_bad_anchor_is_refused_before_any_write(self, anchor, why) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": anchor}, _positioning())
        assert out.startswith("Error:") and why in out
        assert _writes(dash) == []

    def test_a_failed_order_write_says_the_position_itself_landed(self) -> None:
        """A refused reorder is reported as atomic, not half-applied.

        The renumber is one atomic request now, so a refusal leaves the stored
        order untouched -- the message says exactly that and tells the caller to
        re-run. With the parent unchanged there was no move, so it names a
        reposition, not a reparent that did not occur -- in the one message a
        caller reads while deciding what to retry.
        """
        out, _ = _call(
            "chat_folder_move",
            {"folder": "Delta", "after": "Alpha"},
            _positioning(reorder={"error": "this app does not own one of those folders"}),
        )
        assert "ordering was refused" in out, out
        assert "stored order is unchanged" in out, out
        assert "Repositioned folder" in out, out
        assert "Moved folder" not in out, out
        assert not out.startswith("Error:")


class TestPositionRenumberIsAllOrNothingForAnApp:
    """An app may not half-shuffle the person's sidebar.

    Repositioning several siblings is ONE atomic reorder request, and the
    endpoint re-validates this app's ownership of every row under the store lock.
    A batch that names a row the app does not own is refused whole, leaving the
    order untouched -- so the tool relies on the endpoint rather than pre-checking,
    and a refusal cannot land midway.
    """

    OWNED = [
        {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
        {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
        {
            "id": "cccccccccccc",
            "name": "Radar out",
            "parent_id": "",
            "order": 2,
            "owner_app": "issue-radar",
        },
    ]
    RADAR_ROWS = [
        {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"}
    ]
    REFUSED = {"error": "this app does not own one of those folders", "code": "folder_not_owned"}

    def test_renumbering_a_folder_the_app_does_not_own_is_refused_by_the_endpoint(
        self,
    ) -> None:
        # Alpha and Bravo hold adjacent integers, so landing BETWEEN them has no
        # free slot and can only be reached by renumbering the person's two rows --
        # which the reorder endpoint refuses atomically, leaving the order intact.
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "after": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS, reorder=self.REFUSED),
        )
        assert "ordering was refused" in out and "does not own" in out, out
        # The person's Bravo was named in the atomic batch, so there was a foreign
        # row for the endpoint to refuse; no per-row PATCH was ever issued.
        assert "bbbbbbbbbbbb" in _posted_orders(dash), out
        assert not dash.sent("PATCH /api/chat/folders/{folder}")

    def test_an_app_may_place_its_own_folder_where_a_slot_is_free(self) -> None:
        """The rule is about renumbering the person's rows, not about positioning.

        Ahead of Alpha the slot is free, so the app writes only its OWN row: none
        of the person's folders change, and the endpoint judges that single write.
        """
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "before": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS),
        )
        assert not out.startswith("Error:")
        assert len(_writes(dash)) == 1
        assert _patched_orders(dash) == {"cccccccccccc": -1}

    def test_the_same_move_without_a_position_still_reaches_the_endpoint(self) -> None:
        """The refusal is about the RENUMBER, not about moving at all."""
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "new_parent": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS),
        )
        assert not out.startswith("Error:")
        assert dash.sent("PATCH /api/chat/folders/{folder}")

    def test_a_lone_FOREIGN_order_write_is_still_refused(self) -> None:
        """A renumber can change exactly ONE row, and not the moved folder's.

        The endpoint re-validates EVERY row in the batch, so a single foreign row
        (the person's Bravo) is refused whole -- the count is not the question,
        whether any row belongs to someone else is. Here the app's folder already
        holds its target position, so the only row whose order changes is Bravo.
        """
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
            {
                "id": "cccccccccccc",
                "name": "Radar out",
                "parent_id": "",
                "order": 1,
                "owner_app": "issue-radar",
            },
        ]
        # Alpha(0) and the app's own Radar out(1) are adjacent, so landing
        # between them renumbers; Radar out keeps position 1 and only the
        # person's Bravo has to move.
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "after": "Alpha"},
            _positioning(folders=rows, slots=self.RADAR_ROWS, reorder=self.REFUSED),
        )
        assert "ordering was refused" in out and "does not own" in out, out
        # The batch named the person's Bravo, which is what the endpoint refuses.
        assert "bbbbbbbbbbbb" in _posted_orders(dash), out
        assert not dash.sent("PATCH /api/chat/folders/{folder}")

    def test_a_person_reordering_their_own_tree_is_not_gated(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Delta", "before": "Alpha"},
            _positioning(
                slots=[{"key": "chat-1-100", "title": "Raymond", "folder_id": "", "app": ""}]
            ),
        )
        assert not out.startswith("Error:")
        # Alpha is first, so the slot ahead of it is free -- one write, no renumber.
        assert _patched_orders(dash) == {"dddddddddddd": -1}

    def test_a_cross_parent_move_needing_a_foreign_renumber_refuses_before_the_reparent(
        self,
    ) -> None:
        """The reparent must NOT commit when the renumber it needs would be refused.

        A cross-parent move sends a reparent PATCH first, then an atomic reorder.
        When the destination has no free slot, the reorder names sibling rows to
        renumber; if one of those is the person's, the endpoint refuses the reorder
        -- but the reparent PATCH has already landed, leaving the folder moved into
        the new parent yet unpositioned. The tool preflights the batch's ownership
        for the reparent case, so a batch that would be refused writes NOTHING: no
        reparent PATCH, no reorder POST. This pins that no write is attempted.
        """
        rows = [
            {
                "id": "tttttttttttt",
                "name": "AppTop",
                "parent_id": "",
                "order": 0,
                "owner_app": "issue-radar",
            },
            {"id": "pppppppppppp", "name": "PersonTop", "parent_id": "", "order": 1},
            {
                "id": "oooooooooooo",
                "name": "Other",
                "parent_id": "",
                "order": 2,
                "owner_app": "issue-radar",
            },
            {
                "id": "cccccccccccc",
                "name": "Mover",
                "parent_id": "oooooooooooo",
                "order": 0,
                "owner_app": "issue-radar",
            },
        ]
        # Mover (own, nested under Other) up to the top level after AppTop(0):
        # AppTop and PersonTop are adjacent, so landing between them renumbers,
        # and the batch names PersonTop (the person's). The move also reparents
        # Mover (parent Other -> top level), so without the preflight the
        # reparent PATCH would commit before the reorder is refused.
        out, dash = _call(
            "chat_folder_move",
            {"folder": "cccccccccccc", "after": "tttttttttttt"},
            _positioning(folders=rows, slots=self.RADAR_ROWS),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        # No write of any kind: not the reparent PATCH, not the atomic reorder.
        assert _writes(dash) == []


class TestTreeListsInSidebarOrder:
    """The tree is what an anchor is picked from, so it must show the order the
    person sees. Listing it by path would show a sequence that exists nowhere
    and make every ``before``/``after`` a guess."""

    def test_folders_follow_their_stored_order_not_the_alphabet(self) -> None:
        reversed_alpha = [
            {"id": "aaaaaaaaaaaa", "name": "Zulu", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Mike", "parent_id": "", "order": 1},
            {"id": "cccccccccccc", "name": "Alpha", "parent_id": "", "order": 2},
        ]
        out, _ = _call(
            "chat_folder_tree", {}, _reads(folders=reversed_alpha, slots=_slots_with_caller())
        )
        lines = [ln for ln in out.splitlines() if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert [ln.split()[1] for ln in lines] == ["Zulu", "Mike", "Alpha"]

    def test_a_child_still_follows_its_own_parent(self) -> None:
        """Order sequences siblings; it never reparents the render."""
        nested = [
            {"id": "aaaaaaaaaaaa", "name": "First", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Deep", "parent_id": "aaaaaaaaaaaa", "order": 99},
            {"id": "cccccccccccc", "name": "Second", "parent_id": "", "order": 1},
        ]
        out, _ = _call("chat_folder_tree", {}, _reads(folders=nested, slots=_slots_with_caller()))
        body = out.splitlines()
        order = [ln.split()[1] for ln in body if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert order == ["First", "First/Deep", "Second"]

    def test_a_parent_cycle_neither_hangs_nor_swallows_its_folders(self) -> None:
        cyclic = [
            {"id": "aaaaaaaaaaaa", "name": "Ping", "parent_id": "bbbbbbbbbbbb", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Pong", "parent_id": "aaaaaaaaaaaa", "order": 1},
            {"id": "cccccccccccc", "name": "Sane", "parent_id": "", "order": 2},
        ]
        out, _ = _call("chat_folder_tree", {}, _reads(folders=cyclic, slots=_slots_with_caller()))
        for fid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            assert fid in out


#: The reporter's scheme: zero-padded prefixes whose stored positions were set
#: by placing them, so the custom order and the name order disagree.
_NUMBERED = [
    {"id": "aaaaaaaaaaaa", "name": "10. Zulu", "parent_id": "", "order": 0},
    {"id": "bbbbbbbbbbbb", "name": "99. Omega", "parent_id": "", "order": 1},
    {"id": "cccccccccccc", "name": "02. Mike", "parent_id": "", "order": 2},
    {"id": "dddddddddddd", "name": "01. Alpha", "parent_id": "", "order": 3},
]
#: The numbered folders as the dashboard serves them; the sort mode is the
#: person's config, written with ``_set_folder_sort`` where a case needs one.
_NUMBERED_ROUTES = _reads(folders=_NUMBERED, slots=_slots_with_caller())


def _tree_folder_names(out: str) -> list[str]:
    return [
        ln.split()[1]
        for ln in out.splitlines()
        if ln.strip().startswith(("aaaa", "bbbb", "cccc", "dddd"))
    ]


class TestTreeHonoursTheFolderSortMode:
    """The tree lists what the sidebar draws, and the sidebar draws the person's
    folder sort mode. The header names the mode so an agent can tell whether the
    sequence it reads is the one a before/after anchor lands in."""

    def test_custom_is_the_stored_order_and_the_header_says_so(self) -> None:
        _set_folder_sort("custom")
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert out.splitlines()[0].endswith("(folder order: custom):")
        assert _tree_folder_names(out) == ["10.", "99.", "02.", "01."]
        assert "chat_folder_move" not in out, "no caveat in custom mode: the anchor IS the view"

    def test_no_setting_reads_as_custom(self) -> None:
        """A dashboard that never saved a mode lists the stored order."""
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert out.splitlines()[0].endswith("(folder order: custom):")

    def test_name_mode_lists_by_natural_name_and_warns_about_anchors(self) -> None:
        _set_folder_sort("name")
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        lines = out.splitlines()
        assert lines[0].endswith("(folder order: name):")
        # The caveat is the SECOND line, before any folder, so it is read before
        # the sequence it qualifies.
        assert lines[1].startswith("Folders are sorted by name, so a before/after anchor")
        assert "stored (custom) position" in lines[1]
        assert _tree_folder_names(out) == ["01.", "02.", "10.", "99."]

    def test_created_mode_lists_newest_first_at_every_depth(self) -> None:
        rows = [
            {
                "id": "aaaaaaaaaaaa",
                "name": "Old root",
                "parent_id": "",
                "order": 0,
                "created_at": 100,
            },
            {
                "id": "bbbbbbbbbbbb",
                "name": "New root",
                "parent_id": "",
                "order": 1,
                "created_at": 300,
            },
            {
                "id": "cccccccccccc",
                "name": "Old child",
                "parent_id": "bbbbbbbbbbbb",
                "order": 0,
                "created_at": 150,
            },
            {
                "id": "dddddddddddd",
                "name": "New child",
                "parent_id": "bbbbbbbbbbbb",
                "order": 1,
                "created_at": 250,
            },
        ]
        _set_folder_sort("created")
        out, _ = _call("chat_folder_tree", {}, _reads(folders=rows, slots=_slots_with_caller()))
        assert out.splitlines()[0].endswith("(folder order: created):")
        ids = [
            ln.split()[0]
            for ln in out.splitlines()
            if ln.strip().startswith(("aaaa", "bbbb", "cccc", "dddd"))
        ]
        # Newest root first, and under it the newest child first -- the same
        # comparator at both depths, as the sidebar applies it.
        assert ids == ["bbbbbbbbbbbb", "dddddddddddd", "cccccccccccc", "aaaaaaaaaaaa"]
        # Every row is stamped, so nothing is said about unstamped ones.
        assert "with no created_at" not in out

    def test_created_mode_says_when_unstamped_folders_sit_last_in_stored_order(self) -> None:
        """A folder from before the stamp existed has none and lists after every
        stamped row in the stored order -- on a pre-upgrade tree that is the order
        the person already had. The header says so, before the rows, the same fact
        the sidebar's menu states under its rows; an agent must not read that tail
        as a date order."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Old A", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "New", "parent_id": "", "order": 1, "created_at": 300},
            {"id": "cccccccccccc", "name": "Old C", "parent_id": "", "order": 2},
        ]
        _set_folder_sort("created")
        out, _ = _call("chat_folder_tree", {}, _reads(folders=rows, slots=_slots_with_caller()))
        lines = out.splitlines()
        assert lines[1].startswith("Folders are sorted by created, so a before/after anchor")
        assert lines[2] == (
            "2 folders with no created_at (made before the stamp existed) list last, "
            "in the stored (custom) order, not by date."
        )
        ids = [ln.split()[0] for ln in lines if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert ids == ["bbbbbbbbbbbb", "aaaaaaaaaaaa", "cccccccccccc"]

    def test_an_unreadable_setting_is_said_rather_than_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The listing survives, in the stored order, and the header says the
        order is assumed -- an agent must not read a failed lookup as a fact."""
        _config_answering(OSError("config unavailable"), monkeypatch)
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        head = out.splitlines()[0]
        assert "folder order: custom, assumed" in head
        assert "config unavailable" in head
        assert _tree_folder_names(out) == ["10.", "99.", "02.", "01."]

    def test_a_value_the_loader_would_never_store_reads_as_custom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_answering("sideways", monkeypatch)
        out, _ = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert out.splitlines()[0].endswith("(folder order: custom):")

    def test_the_mode_reading_is_its_readers_answer(self) -> None:
        """``_chat_folder_sort_mode`` over each reading its config read can give."""

        def _raises() -> object:
            raise OSError("gone")

        assert mcp_dashboard._chat_folder_sort_mode(lambda: "created") == ("created", None)
        assert mcp_dashboard._chat_folder_sort_mode(lambda: "sideways") == ("custom", None)
        assert mcp_dashboard._chat_folder_sort_mode(lambda: 7) == ("custom", None)
        assert mcp_dashboard._chat_folder_sort_mode(_raises) == ("custom", "OSError: gone")

    def test_the_setting_is_read_from_the_config_file_not_over_http(self) -> None:
        """``GET /api/config/kirocrew`` is cookie-only -- it is in neither
        internal-secret allowlist -- so a tool that fetched it would always read
        an auth error and always list the stored order. The read goes through the
        loader instead, like the other dashboard settings the MCP tools read."""
        _set_folder_sort("name")
        assert mcp_dashboard._read_folder_sort_setting() == "name"
        out, dash = _call("chat_folder_tree", {}, _NUMBERED_ROUTES)
        assert all("/api/config" not in r.path for r in dash.requests)
        assert out.splitlines()[0].endswith("(folder order: name):")

    def test_a_position_is_computed_in_the_stored_order_whatever_the_mode(self) -> None:
        """Choosing a view mode never rewrites the stored positions, and a
        before/after anchor is a stored-position concept: with the sidebar sorted
        by name, "after 01. Alpha" still lands in the gap after Alpha's STORED
        position (3, the last), not after its displayed one (first)."""
        _set_folder_sort("name")
        out, dash = _call(
            "chat_folder_move",
            {"folder": "aaaaaaaaaaaa", "after": "dddddddddddd"},
            _positioning(folders=_NUMBERED),
        )
        assert not out.startswith("Error"), out
        assert not dash.sent(
            "POST /api/chat/folders/reorder"
        ), "a free slot after the last stored position needs no renumber"
        assert [(r.path, r.body) for r in _writes(dash)] == [
            ("/api/chat/folders/aaaaaaaaaaaa", {"order": 4})
        ]

    def test_the_default_sibling_sort_is_the_custom_order(self) -> None:
        """The placement helpers call ``_chat_folder_siblings`` without a mode and
        mean the stored order; pinning the default keeps a future caller from
        computing a gap in a view that has none."""
        rows = [dict(f) for f in _NUMBERED]
        assert [f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "")] == [
            f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "", "custom")
        ]
        assert mcp_dashboard.FOLDER_SORT_DEFAULT == "custom"

    def test_both_tool_descriptions_name_the_mode_contract(self) -> None:
        tools = {t["name"]: t["description"] for t in _list_tools()}
        assert "folder sort mode" in tools["chat_folder_tree"]
        assert "header line names the active mode" in tools["chat_folder_tree"]
        assert "STORED position" in tools["chat_folder_move"]
        assert "chat_folder_tree's header says which" in tools["chat_folder_move"]

    def test_no_persisted_created_at_shape_can_raise_out_of_the_sort_key(self) -> None:
        """Same totality rule as ``order``: the stamp is read with a bare
        ``json.loads``, so every JSON shape must sort rather than raise."""
        junk = [
            float("inf"),
            float("-inf"),
            float("nan"),
            "abc",
            [1],
            {"a": 1},
            None,
            True,
            10**400,
        ]
        for value in junk:
            row = {"id": "aaaaaaaaaaaa", "name": "X", "parent_id": "", "created_at": value}
            assert mcp_dashboard._chat_folder_created(row) is None or value == 10**400, value
            assert mcp_dashboard._chat_folder_siblings([row], "", "created") == [row], value
            assert mcp_dashboard._chat_folder_render_order([row], "created") == [
                ("aaaaaaaaaaaa", 0)
            ]
        assert mcp_dashboard._chat_folder_created({"created_at": 10**400}) == float(
            mcp_dashboard._CHAT_FOLDER_ORDER_LIMIT
        )


class TestPositionIsAdvertised:
    def test_the_tool_declares_both_anchors(self) -> None:
        move = next(t for t in _list_tools() if t["name"] == "chat_folder_move")
        props = move["inputSchema"]["properties"]
        assert "before" in props and "after" in props
        assert move["inputSchema"]["required"] == ["folder"]

    def test_an_anchor_is_accepted_by_the_schema(self) -> None:
        """The schema rejects unknown fields, so an undeclared anchor would 400
        before the handler ever ran."""
        out, _ = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert "unknown field" not in out

    def test_an_unknown_field_is_still_rejected(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "position": "first"})
        assert out.startswith("Error: position: unknown field")
        assert dash.requests == []
