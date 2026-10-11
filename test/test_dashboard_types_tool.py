"""The ``dashboard_types`` tool and the route's seq read.

Two halves, each with one claim worth pinning.

The TOOL is a pure read, and the test that matters is that it issues a GET and nothing
else: this is the one tool on the panel server an agent is told to call before it
composes anything, so a POST sneaking in here would be a write on the path that
advertises itself as free of them.

The ROUTE's half is ``_folded_through``, and its claim is that an unreadable fold comes
back as ``None`` rather than ``0``. The two are different answers -- "nobody looked"
against "this fold has consumed nothing" -- and merging them would have a composing
agent bind a block to a fold it cannot see and draw it as empty. ``radar`` is the
permanent case: its owner orders the slot's units itself, so a generic read is refused
by design and the catalog says so instead of advertising a value nobody can fetch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import dashboard_types as dt
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers.agent_panel import _folded_through
from kiro_crew.mcp_panel import _call_tool_inner, _list_tools, _render_types

GOOD_KEY = "dashboard:chat-types-1"


@pytest.fixture()
def _verified_caller() -> Any:
    with patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=GOOD_KEY):
        yield


class TestTheToolSurface:
    def test_the_tool_is_advertised(self) -> None:
        assert "dashboard_types" in {t["name"] for t in _list_tools()}

    def test_it_advertises_no_arguments(self) -> None:
        """The catalog is the same for every caller but the seqs, and those come from
        the calling session's own identity -- so an accepted argument would be a filter
        the tool does not apply."""
        tool = next(t for t in _list_tools() if t["name"] == "dashboard_types")
        assert tool["inputSchema"].get("properties") == {}

    def test_its_description_warns_about_the_three_readings(self) -> None:
        """An agent that misreads any of the three binds a block that draws nothing:
        an array is where a path stops, an opaque object may accept a path this tool
        cannot promise, and a null position is not a zero one."""
        tool = next(t for t in _list_tools() if t["name"] == "dashboard_types")
        text = tool["description"]
        assert "array" in text and "opaque" in text
        assert "folded_through" in text


class TestTheToolIsARead:
    def test_it_issues_a_get_and_no_write(self, _verified_caller: Any) -> None:
        with (
            patch("kiro_crew.mcp_panel._get", return_value=dt.describe({})) as mock_get,
            patch("kiro_crew.mcp_panel._post") as mock_post,
        ):
            out = _call_tool_inner("dashboard_types", {})
        assert mock_get.call_args[0][0] == "/api/agent-panel/dashboard/types"
        assert mock_post.call_count == 0
        assert "data types" in out

    def test_an_api_error_comes_back_whole(self, _verified_caller: Any) -> None:
        with patch("kiro_crew.mcp_panel._get", return_value={"error": "forbidden"}):
            out = _call_tool_inner("dashboard_types", {})
        assert out == "Error: forbidden"

    def test_a_subagent_is_refused_before_the_read(self) -> None:
        """No verified caller: the strict gate answers first, so no request is made."""
        with patch("kiro_crew.mcp_panel._get") as mock_get:
            out = _call_tool_inner("dashboard_types", {})
        assert mock_get.call_count == 0
        assert out.startswith("Error:")


class TestTheRenderedCatalog:
    def test_every_fold_and_its_keying_are_named(self) -> None:
        out = _render_types(dt.describe({}))
        for name in projection.FOLD_NAMES:
            assert f"`{name}`" in out
        assert "slot-keyed" in out and "session-keyed" in out

    def test_a_read_seq_is_printed_and_an_unread_one_is_not_printed_as_zero(self) -> None:
        out = _render_types(dt.describe({"usage": 0, "status": 814}))
        assert "folded through seq 814" in out
        assert "folded through seq 0" in out
        assert "not read -- see the note below" in out
        assert "NOT the same as 0" in out

    def test_the_owner_served_fold_says_why_it_is_unread(self) -> None:
        out = _render_types(dt.describe({}))
        assert "owner-served" in out
        assert f"`{projection.OWNER_SERVED_SLOT_PROJECTION}` fold is always this way" in out

    def test_the_note_is_absent_when_every_position_was_read(self) -> None:
        """Said once and only when it applies: a reader whose every fold came back
        with a number should not spend context on a caveat about none of them."""
        every = {name: 3 for name in projection.FOLD_NAMES}
        out = _render_types(dt.describe(every))
        assert "NOT the same as 0" not in out
        assert "not read" not in out

    def test_paths_are_printed_rather_than_nested_json(self) -> None:
        """A path is what the agent pastes into a Model field."""
        out = _render_types(dt.describe({}))
        assert "tokens.total: number" in out
        assert "last.decision: string" in out
        assert '"properties"' not in out

    def test_an_array_says_a_path_cannot_reach_inside_it(self) -> None:
        out = _render_types(dt.describe({}))
        assert "moments: array -- a path cannot reach inside it" in out

    def test_an_opaque_object_names_its_key(self) -> None:
        out = _render_types(dt.describe({}))
        assert "by_model: object, keyed by a model id" in out

    def test_a_nullable_leaf_is_marked(self) -> None:
        out = _render_types(dt.describe({}))
        assert "parent_item: string (may be null)" in out

    def test_an_empty_catalog_is_reported_rather_than_rendered_blank(self) -> None:
        out = _render_types({"types": []})
        assert "nothing to bind" in out

    def test_a_wide_shape_reports_what_it_omitted(self) -> None:
        """A trimmed list that did not say so would read as the whole shape."""
        wide = {
            "types": [
                {
                    "name": "wide",
                    "keyed_by": "slot",
                    "source_fold": "wide",
                    "folded_through": 1,
                    "shape": {
                        "type": "object",
                        "properties": {f"f{i}": {"type": "number"} for i in range(60)},
                    },
                }
            ]
        }
        out = _render_types(wide)
        assert "and 20 more paths under this type" in out

    def test_a_derived_type_names_the_fold_it_came_from(self) -> None:
        """``source_fold`` is not decoration: a block binds a subscription to the fold
        the value is folded from, which for a derived type is not its own name."""
        derived = {
            "types": [
                {
                    "name": "outline",
                    "keyed_by": "slot",
                    "source_fold": "work",
                    "folded_through": 9,
                    "shape": {"type": "object", "properties": {"n": {"type": "number"}}},
                }
            ]
        }
        out = _render_types(derived)
        assert "derived from the `work` fold" in out


class TestFoldedThrough:
    def test_every_registered_fold_gets_an_answer(self) -> None:
        """A name missing from the map would come back ``None`` from the catalog
        anyway, but silently -- so the route answers for all of them explicitly."""
        assert set(_folded_through("", "")) == set(projection.FOLD_NAMES)

    def test_no_slot_and_no_unit_reads_nothing_and_claims_nothing(self) -> None:
        reached = _folded_through("", "")
        assert set(reached.values()) == {None}

    def test_the_owner_served_fold_is_never_read_generically(self) -> None:
        """Its owner puts the recorded order ahead of the header clock and pins the
        live unit last. A generic read has neither fact, so this route does not make
        one -- it does not merely discard the answer."""
        with patch.object(projection, "read_slot_projection") as mock_read:
            mock_read.side_effect = lambda slot, name: projection.Projection(name, 5, {})
            reached = _folded_through("slot-a", "")
        asked = {call.args[1] for call in mock_read.call_args_list}
        assert projection.OWNER_SERVED_SLOT_PROJECTION not in asked
        assert reached[projection.OWNER_SERVED_SLOT_PROJECTION] is None

    def test_a_slot_fold_reports_the_seq_its_value_reflects(self) -> None:
        with patch.object(projection, "read_slot_projection") as mock_read:
            mock_read.side_effect = lambda slot, name: projection.Projection(name, 77, {})
            reached = _folded_through("slot-a", "")
        assert reached["work"] == 77
        assert reached["agentic"] == 77
        # A session fold has no unit to be read against here.
        assert reached["status"] is None

    def test_a_fold_zero_entries_in_is_zero_and_not_none(self) -> None:
        """The other side of the distinction: ``0`` is a real answer and must survive
        the trip, or "nothing folded yet" would be indistinguishable from unreadable."""
        with patch.object(projection, "read_slot_projection") as mock_read:
            mock_read.side_effect = lambda slot, name: projection.Projection(name, 0, {})
            reached = _folded_through("slot-a", "")
        assert reached["work"] == 0

    def test_one_unreadable_slot_fold_does_not_take_the_others_with_it(self) -> None:
        def _read(slot: str, name: str) -> projection.Projection:
            if name == "panel":
                raise RuntimeError("the panel fold's cell is wedged")
            return projection.Projection(name, 12, {})

        with patch.object(projection, "read_slot_projection", side_effect=_read):
            reached = _folded_through("slot-a", "")
        assert reached["panel"] is None
        assert reached["ledger"] == 12

    def test_the_session_folds_come_from_one_pass(self) -> None:
        """One ``fold_session`` call, not seven: the bundle folds every name it was
        given in a single walk, and seven calls would walk the same file seven times."""
        bundle = projection.empty_session("unit-1", projection.SESSION_FOLD_NAMES)
        with patch.object(projection, "fold_session", return_value=bundle) as mock_fold:
            reached = _folded_through("", "unit-1")
        assert mock_fold.call_count == 1
        assert mock_fold.call_args[0][1] == projection.SESSION_FOLD_NAMES
        for name in projection.SESSION_FOLD_NAMES:
            assert reached[name] == 0

    def test_an_unreadable_session_log_leaves_the_slot_folds_answered(self) -> None:
        with (
            patch.object(projection, "fold_session", side_effect=RuntimeError("gone")),
            patch.object(projection, "read_slot_projection") as mock_read,
        ):
            mock_read.side_effect = lambda slot, name: projection.Projection(name, 31, {})
            reached = _folded_through("slot-a", "unit-1")
        assert reached["status"] is None
        assert reached["work"] == 31
