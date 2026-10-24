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
from kiro_crew.dashboard import folder_rank
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


#: Four root folders from before ranks existed: integer ``order`` only.
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
) -> dict[str, Any]:
    """Routes for a ``chat_folder_move`` that may PATCH a row.

    A PATCH answers with the row it names, merged with what was written, unless
    ``patch`` says otherwise.
    """
    rows = _ORDERED if folders is None else folders

    def _patch_row(req: DashboardRequest) -> dict:
        fid = req.path.rsplit("/", 1)[-1]
        row = next((dict(f) for f in rows if f["id"] == fid), {"id": fid})
        return {**row, **(req.body or {})}

    return {
        **_reads(folders=rows, slots=_slots_with_caller() if slots is None else slots),
        "PATCH /api/chat/folders/{folder}": _patch_row if patch is None else patch,
    }


def _patch_bodies(dash: InMemoryDashboardClient) -> list[tuple[str, dict]]:
    """``(folder_id, body)`` for every PATCH the call issued."""
    return [
        (r.path.rsplit("/", 1)[-1], r.body) for r in dash.sent("PATCH /api/chat/folders/{folder}")
    ]


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
            key = folder_rank.name_key({"name": name})
            assert key == name.encode("utf-16-be", "surrogatepass"), name
        assert folder_rank.name_key({"name": "STRASSE"}) == ("strasse".encode("utf-16-be"))

    def test_a_dotted_capital_i_is_not_folded(self) -> None:
        """The concrete skew shape: a character whose fold is version-dependent.

        `İ` (LATIN CAPITAL I WITH DOT ABOVE) lowercases to a two-character
        sequence in Python, and the exact result has moved across Unicode versions.
        Unfolded it is one code unit on both sides and cannot skew.
        """
        key = folder_rank.name_key({"name": "İ"})
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
        key = folder_rank.name_key({"name": "Ab\U0001f600"})
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

    The tool sends the anchor to the gateway in the same PATCH as any reparent,
    and the gateway picks the rank. So a position is always exactly one write,
    and every ownership rule is the endpoint's.
    """

    def test_after_an_anchor_is_one_patch_naming_it(self) -> None:
        out, dash = _call("chat_folder_move", {"folder": "Delta", "after": "Alpha"}, _positioning())
        assert not out.startswith("Error:"), out
        # No parent_id: the folder is already at the top level.
        assert _patch_bodies(dash) == [
            ("dddddddddddd", {"after": "aaaaaaaaaaaa", "expected_parent": ""})
        ]
        assert len(_writes(dash)) == 1
        assert "after `Alpha`" in out

    def test_before_an_anchor_is_one_patch_naming_it(self) -> None:
        out, dash = _call(
            "chat_folder_move", {"folder": "Delta", "before": "Bravo"}, _positioning()
        )
        assert not out.startswith("Error:"), out
        assert _patch_bodies(dash) == [
            ("dddddddddddd", {"before": "bbbbbbbbbbbb", "expected_parent": ""})
        ]
        assert "before `Bravo`" in out

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
        assert not out.startswith("Error:"), out
        # Stays inside Parent, so parent_id is omitted and only the anchor is sent.
        assert _patch_bodies(dash) == [
            ("bbbbbbbbbbbb", {"before": "aaaaaaaaaaaa", "expected_parent": "pppppppppppp"})
        ]

    def test_an_anchor_names_the_container_it_was_read_in(self) -> None:
        """A deleted parent promotes its children to the root as siblings.

        The anchor check alone would then pass at the root and write a root
        reorder. Sending the container this call read lets the gateway refuse
        with 409 when the folder has left that container.
        """
        nested = [
            {"id": "pppppppppppp", "name": "Parent", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "pppppppppppp", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "pppppppppppp", "order": 1},
            {"id": "cccccccccccc", "name": "Charlie", "parent_id": "pppppppppppp", "order": 2},
        ]
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Parent/Charlie", "before": "Parent/Bravo"},
            _positioning(folders=nested),
        )
        assert not out.startswith("Error:"), out
        (write,) = _writes(dash)
        assert write.body["expected_parent"] == "pppppppppppp"

    def test_a_reparent_with_an_anchor_names_the_source_container(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "after": "kirocrew/0811"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert out.startswith("Moved folder"), out
        (write,) = _writes(dash)
        source = next(f for f in _FOLDERS if f["name"] == "Travel")
        assert write.body["expected_parent"] == str(source.get("parent_id") or "")

    def test_a_parent_outside_the_snapshot_sends_no_expected_parent(self) -> None:
        """A parent missing from the listing is either deleted or hidden from a
        crew member's filtered view. The gateway compares against the full
        store, so a deleted parent reads as the root there and a hidden one
        reads as itself. No single value matches both, and sending either
        would refuse every move of that folder with 409."""
        rows = [
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "zzzzzzzzzzzz", "order": 1},
        ]
        out, dash = _call(
            "chat_folder_move", {"folder": "Bravo", "before": "Alpha"}, _positioning(folders=rows)
        )
        assert not out.startswith("Error:"), out
        (write,) = _writes(dash)
        assert "expected_parent" not in write.body
        assert write.body["before"] == "aaaaaaaaaaaa"

    def test_a_move_without_an_anchor_sends_no_expected_parent(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "new_parent": "kirocrew"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert not out.startswith("Error:"), out
        (write,) = _writes(dash)
        assert "expected_parent" not in write.body

    def test_a_same_parent_move_without_anchor_writes_nothing(self) -> None:
        out, dash = _call(
            "chat_folder_move", {"folder": "Bravo", "new_parent": "root"}, _positioning()
        )
        assert not out.startswith("Error:"), out
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
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Travel", "after": "kirocrew/0811"},
            _positioning(folders=_FOLDERS, slots=_SLOTS),
        )
        assert out.startswith("Moved folder")
        assert "kirocrew/Travel" in out and "after `kirocrew/0811`" in out
        # Reparent and position travel in ONE request, so neither can land alone.
        (write,) = _writes(dash)
        assert write.body["parent_id"] == "aaaaaaaaaaaa" and "after" in write.body

    def test_an_endpoint_refusal_is_surfaced(self) -> None:
        """Ownership is the endpoint's: the tool forwards and reports its verdict.

        An app may position only its own folder. The endpoint decides that under
        the store lock, on the one row a position writes, so the tool has no
        ownership pre-check of its own to drift from it.
        """
        rows = [
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "AppOne", "parent_id": "", "order": 1, "owner_app": "x"},
        ]
        refused = {"error": "this app does not own that folder", "code": "folder_not_owned"}
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Person", "after": "AppOne"},
            _positioning(folders=rows, slots=[{"key": "chat-1-1", "app": "x"}], patch=refused),
            Caller.strict("dashboard:chat-1-1"),
        )
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        assert len(dash.sent("PATCH /api/chat/folders/{folder}")) == 1

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
        key = folder_rank.name_key({"name": "a\ud800b"})
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


class TestAnAppPositionsItsOwnFolder:
    """Positioning writes only the moved folder, so the person's siblings never block it."""

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

    def test_between_two_of_the_persons_folders_is_one_patch(self) -> None:
        out, dash = _call(
            "chat_folder_move",
            {"folder": "Radar out", "after": "Alpha"},
            _positioning(folders=self.OWNED, slots=self.RADAR_ROWS),
        )
        assert not out.startswith("Error:"), out
        assert _patch_bodies(dash) == [
            ("cccccccccccc", {"after": "aaaaaaaaaaaa", "expected_parent": ""})
        ]
        assert len(_writes(dash)) == 1


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
        by name, "after 01. Alpha" still names the gap after Alpha's STORED
        position. The tool sends the anchor as is, and the gateway resolves it in
        the custom order (``folder_rank.section_siblings``)."""
        _set_folder_sort("name")
        out, dash = _call(
            "chat_folder_move",
            {"folder": "aaaaaaaaaaaa", "after": "dddddddddddd"},
            _positioning(folders=_NUMBERED),
        )
        assert not out.startswith("Error"), out
        assert [(r.path, r.body) for r in _writes(dash)] == [
            ("/api/chat/folders/aaaaaaaaaaaa", {"after": "dddddddddddd", "expected_parent": ""})
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
