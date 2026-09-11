"""Tests for the chat (sidebar) folder tools on the kirocrew-dashboard server.

Covers dispatch for ``chat_folder_tree/create/move/move_session`` — schema
validation, path→id resolution, mkdir -p, session-reference resolution, HTTP
call shape, and result formatting. The HTTP helpers are patched; the endpoints
themselves are tested by ``test_folder_store_writer.py`` and
``test_dashboard_chat.py``.
"""

from __future__ import annotations

import inspect
import json
import pathlib
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import mcp_dashboard
from kiro_crew.mcp_dashboard import _call_tool_inner, _list_tools
from kiro_crew.validation import ValidationError

# Representative GET /api/chat/folders body — a bare JSON array (no envelope),
# each row carrying only parent_id (the human path is derived client-side).
_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": "", "history_count": 3},
    {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa", "history_count": 0},
    {"id": "cccccccccccc", "name": "Travel", "parent_id": "", "history_count": 0},
]

_SLOTS = [
    {"key": "chat-1-100", "title": "Backup M1", "folder_id": "aaaaaaaaaaaa", "running": True},
    {"key": "chat-2-200", "title": "Folder MCP", "folder_id": "bbbbbbbbbbbb"},
    {"key": "chat-3-300", "title": "Scratch", "folder_id": ""},
]


def _rows(path: str) -> list[dict]:
    """Stand in for the two array endpoints the tools read."""
    if path == "/api/chat/folders":
        return [dict(f) for f in _FOLDERS]
    if path == "/api/chat/slots":
        return [dict(s) for s in _SLOTS]
    raise AssertionError(f"unexpected GET {path}")


#: The caller's OWN slot, which the raw ``/api/chat/slots`` list always carries.
#: A live dashboard session is necessarily in that list, so a fixture that omits
#: it models a state production cannot reach — and one that is now refused,
#: because a ``dashboard:`` key naming an absent slot means the tab was closed
#: mid-call or the key is wrong.
#:
#: Marked non-persistent so it stays LOCATABLE (the scope resolver reads the raw
#: rows) while being filtered out of the RENDERED list — which is what the
#: tree-shape cases below want to assert.
_CALLER_ROW = {
    "key": "chat-1-100",
    "title": "Caller",
    "folder_id": "",
    "memory_mode": "incognito",
}


def _slots_with_caller(*extra: dict) -> list[dict]:
    """The caller's own row plus whatever the case under test needs."""
    return [dict(_CALLER_ROW), *[dict(e) for e in extra]]


@pytest.fixture(autouse=True)
def _verified_caller() -> Any:
    """Filing another session requires an identity the gateway vouches for.

    ``chat_folder_move_session`` resolves it strictly, so a fixture without one
    exercises the refusal rather than the move. Applied module-wide because
    several classes drive that tool; the case that tests the refusal patches
    this to empty itself, and the inner patch wins.
    """
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value="dashboard:chat-1-100",
    ):
        yield


class TestFolderTree:
    def test_renders_paths_sessions_and_unfiled(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows):
            out = _call_tool_inner("chat_folder_tree", {})
        # Derived human path, not just the leaf name.
        assert "kirocrew/0811" in out
        # Sessions nest under their folder, with the slot key the move tool takes.
        assert "chat-2-200" in out and "Folder MCP" in out
        assert "running" in out  # live state surfaces
        # The folders endpoint reports history_count, but this server does not
        # render it: an archived count covers filed incognito/temporary
        # transcripts with no memory_mode to filter on, so a folder holding one
        # would disclose it as a number. See TestPrivateSessionsAreInvisible.
        assert "3 archived" not in out and "archived" not in out
        assert "(unfiled" in out and "chat-3-300" in out
        assert "3 folders, 3 live sessions" in out

    def test_slot_pointing_at_unknown_folder_falls_back_to_unfiled(self) -> None:
        """A dangling folder_id must not make the session disappear."""
        orphan = _slots_with_caller(
            {"key": "chat-9-900", "title": "Orphan", "folder_id": "deadbeefdead"}
        )

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else orphan

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_get):
            out = _call_tool_inner("chat_folder_tree", {})
        assert "(unfiled" in out and "chat-9-900" in out

    def test_empty_tree(self) -> None:
        def _get(path: str) -> list[dict]:
            # No folders, and the caller's own (incognito) session is the only
            # slot — so nothing is RENDERED while the caller stays locatable.
            return [] if path == "/api/chat/folders" else _slots_with_caller()

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_get):
            out = _call_tool_inner("chat_folder_tree", {})
        assert "No sidebar folders and no live sessions." == out

    def test_folder_endpoint_error_is_not_reported_as_empty(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", return_value={"error": "Token required"}):
            out = _call_tool_inner("chat_folder_tree", {})
        assert out.startswith("Error:") and "Token required" in out

    def test_unexpected_body_shape_is_an_error(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", return_value={"folders": []}):
            out = _call_tool_inner("chat_folder_tree", {})
        assert out.startswith("Error:") and "unexpected response shape" in out


class TestFolderCreate:
    def test_creates_subfolder_under_existing_parent_path(self) -> None:
        made = {"id": "dddddddddddd", "name": "0812", "parent_id": "aaaaaaaaaaaa"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "0812", "parent": "kirocrew"})
        path, body = mock_post.call_args.args
        assert path == "/api/chat/folders"
        assert body == {"name": "0812", "parent_id": "aaaaaaaaaaaa"}
        assert "kirocrew/0812" in out and "dddddddddddd" in out

    def test_accepts_parent_by_id(self) -> None:
        made = {"id": "dddddddddddd", "name": "x", "parent_id": "bbbbbbbbbbbb"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            _call_tool_inner("chat_folder_create", {"name": "x", "parent": "bbbbbbbbbbbb"})
        assert mock_post.call_args.args[1]["parent_id"] == "bbbbbbbbbbbb"

    def test_top_level_when_parent_omitted(self) -> None:
        made = {"id": "eeeeeeeeeeee", "name": "Solo", "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            _call_tool_inner("chat_folder_create", {"name": "Solo"})
        assert mock_post.call_args.args[1] == {"name": "Solo", "parent_id": ""}

    def test_a_redacted_segment_is_not_recreated_on_every_call(self) -> None:
        """The stored name is the redacted one, so the LOOKUP must use it too.

        Redacting only at the write meant the next call searched for the raw
        text, never matched the folder this walk had just created, and made
        another one — an unbounded pile of same-named siblings and an ambiguous
        path, from a caller simply retrying the same request.
        """
        store: list[dict] = [dict(f) for f in _FOLDERS]

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in store]
            if path == "/api/chat/slots":
                return [dict(s) for s in _SLOTS]
            raise AssertionError(f"unexpected GET {path}")

        posts: list[dict] = []

        def _post(path: str, body: dict, **kw: object) -> dict:
            posts.append(body)
            made = {
                "id": f"new{len(posts):09d}",
                "name": body["name"],
                "parent_id": body["parent_id"],
            }
            store.append(made)
            return made

        secret = "AKIAIOSFODNN7EXAMPLE"
        args = {"name": "leaf", "parent": f"keys-{secret}"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._post", side_effect=_post),
        ):
            _call_tool_inner("chat_folder_create", args)
            before = len(posts)
            _call_tool_inner("chat_folder_create", args)

        # First call creates the parent + the leaf; the second finds both.
        parents = [p for p in posts if p["name"] != "leaf"]
        assert len(parents) == 1, f"parent recreated: {[p['name'] for p in posts]}"
        assert secret not in parents[0]["name"]
        # And the second call did not re-mint the parent it could now see.
        assert [p["name"] for p in posts[before:]] == ["leaf"]

    def test_the_length_limit_is_measured_on_what_gets_stored(self) -> None:
        """Redaction can change the length, and the endpoint truncates the
        redacted form — so a segment that only overruns AFTER redaction must
        still be refused, or it comes back truncated and unmatchable."""
        seg = "k" * 95
        with (
            patch("kiro_crew.mcp_dashboard.redact", side_effect=lambda s: s + "x" * 20),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "leaf", "parent": seg})
        assert "too long" in out
        mock_post.assert_not_called()

    def test_a_name_that_only_overruns_after_redaction_is_refused(self) -> None:
        """The schema caps the CALLER's name; redaction can make it longer.

        The endpoint stores ``name[:100]``, so a name that grew past the limit
        during redaction would be persisted truncated — unmatchable by any later
        path, the same mismatch the segment walk refuses.
        """
        with (
            patch("kiro_crew.mcp_dashboard.redact", side_effect=lambda s: s + "x" * 30),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "k" * 90})
        assert "too long after redaction" in out
        mock_post.assert_not_called()

    def test_mkdir_p_creates_missing_parent_segments(self) -> None:
        posts: list[dict] = []

        def _post(path: str, body: dict, **kw: object) -> dict:
            posts.append(body)
            return {
                "id": f"new{len(posts):09d}",
                "name": body["name"],
                "parent_id": body["parent_id"],
            }

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", side_effect=_post),
        ):
            out = _call_tool_inner(
                "chat_folder_create", {"name": "week1", "parent": "kirocrew/2026/august"}
            )
        # "kirocrew" exists; "2026" and "august" are created, then the leaf.
        assert [p["name"] for p in posts] == ["2026", "august", "week1"]
        assert posts[0]["parent_id"] == "aaaaaaaaaaaa"
        # Each created segment becomes the next one's parent — the walk threads
        # the freshly minted id through instead of restarting at the top level.
        assert posts[1]["parent_id"] == "new000000001"
        assert posts[2]["parent_id"] == "new000000002"
        assert "created parent path: 2026/august" in out

    def test_partial_mkdir_p_reports_what_was_created(self) -> None:
        calls: list[dict] = []

        def _post(path: str, body: dict, **kw: object) -> dict:
            calls.append(body)
            if len(calls) == 1:
                return {"id": "new000000001", "name": body["name"], "parent_id": ""}
            return {"error": "name required"}

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", side_effect=_post),
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "leaf", "parent": "new/deeper"})
        assert out.startswith("Error:")
        assert "created parent path: new" in out

    def test_stale_id_reference_is_not_created_as_a_folder_name(self) -> None:
        """An id-shaped parent that does not exist is a lookup failure.

        Treating it as a path segment would create a folder literally named
        after the hex id.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "x", "parent": "0123456789ab"})
        assert out.startswith("Error:") and "folder not found" in out
        mock_post.assert_not_called()

    def test_name_is_required(self) -> None:
        # Raised inside the tool and converted to a clean "Error: ..." string by
        # call_tool_with_logging's outer guard (the schema is registered in
        # validation.TOOL_SCHEMAS precisely so that guard runs).
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_folder_create", {"parent": "kirocrew"})


class TestAmbiguousFolderPaths:
    """Folder names are not unique within a parent, so a path can be ambiguous.

    Taking the first match would create under — or move into — an arbitrary
    sibling, which is a silent wrong-placement rather than a visible failure.
    """

    # Two folders named "0811" under the same parent, as the sidebar allows.
    DUPES = [
        {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": ""},
        {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
        {"id": "cccccccccccc", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
    ]

    def _get(self, path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in self.DUPES]
        return [{"key": "chat-1-100", "title": "S", "folder_id": ""}]

    def test_create_refuses_an_ambiguous_parent_path(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._get),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "x", "parent": "kirocrew/0811"})
        assert out.startswith("Error:")
        assert "bbbbbbbbbbbb" in out and "cccccccccccc" in out
        mock_post.assert_not_called()

    def test_move_refuses_an_ambiguous_destination_path(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._get),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "kirocrew", "new_parent": "kirocrew/0811"}
            )
        assert out.startswith("Error:") and "pass the folder id" in out
        mock_patch.assert_not_called()

    def test_session_move_refuses_an_ambiguous_destination_path(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._get),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-1-100", "folder": "kirocrew/0811"},
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_an_id_still_addresses_one_of_the_duplicates(self) -> None:
        """The refusal must leave a way through: the id is unambiguous."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._get),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-1-100", "folder": "cccccccccccc"},
            )
        assert not out.startswith("Error:")
        assert mock_patch.call_args.args[1] == {"folder_id": "cccccccccccc"}

    def test_mkdir_p_does_not_add_a_third_duplicate_sibling(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._get),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner(
                "chat_folder_create", {"name": "leaf", "parent": "kirocrew/0811/deeper"}
            )
        # Ambiguity found mid-walk (not at the final segment), so this is the
        # segment refusal — it must name both duplicates.
        assert out.startswith("Error:") and "share the same parent" in out
        assert "bbbbbbbbbbbb" in out and "cccccccccccc" in out
        mock_post.assert_not_called()


class TestSlashBearingFolderNames:
    """A folder NAME may contain '/', so a rendered path has two readings.

    The sidebar permits a folder literally named ``A/B``, which renders exactly
    like ``B`` nested inside ``A``. Resolving the agent's own displayed path to
    the nested pair would act on a different folder than the one it read.
    """

    LITERAL = [{"id": "aaaaaaaaaaaa", "name": "A/B", "parent_id": ""}]
    BOTH = [
        {"id": "aaaaaaaaaaaa", "name": "A/B", "parent_id": ""},
        {"id": "bbbbbbbbbbbb", "name": "A", "parent_id": ""},
        {"id": "cccccccccccc", "name": "B", "parent_id": "bbbbbbbbbbbb"},
    ]

    @staticmethod
    def _getter(folders: list[dict]) -> Any:
        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in folders]
            return [{"key": "chat-1-100", "title": "S", "folder_id": ""}]

        return _get

    def test_the_literal_folder_wins_when_no_nested_pair_exists(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._getter(self.LITERAL)),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-1-100", "folder": "A/B"}
            )
        assert not out.startswith("Error:")
        assert mock_patch.call_args.args[1] == {"folder_id": "aaaaaaaaaaaa"}

    def test_create_under_a_literal_slash_name_does_not_build_a_nested_pair(self) -> None:
        made = {"id": "dddddddddddd", "name": "leaf", "parent_id": "aaaaaaaaaaaa"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._getter(self.LITERAL)),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            _call_tool_inner("chat_folder_create", {"name": "leaf", "parent": "A/B"})
        # Exactly one POST: the leaf. No "A" and no "B" were manufactured.
        assert mock_post.call_count == 1
        assert mock_post.call_args.args[1] == {"name": "leaf", "parent_id": "aaaaaaaaaaaa"}

    def test_collision_between_the_two_readings_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._getter(self.BOTH)),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-1-100", "folder": "A/B"}
            )
        assert out.startswith("Error:") and "render the same path" in out
        # Both candidate ids are named so the caller can choose one.
        assert "aaaaaaaaaaaa" in out and "cccccccccccc" in out
        mock_patch.assert_not_called()

    def test_a_reading_divergence_that_survives_the_path_render_is_refused(self) -> None:
        """The two readings can differ without rendering the same path.

        A leading space inside the nested name makes the pair render ``A/ B``
        while the literal folder renders ``A/B``, so the duplicate-path check
        passes and the walk-vs-exact disagreement (the walk strips each segment)
        is the only thing left to catch it.
        """
        padded = [
            {"id": "aaaaaaaaaaaa", "name": "A/B", "parent_id": ""},
            {"id": "bbbbbbbbbbbb", "name": "A", "parent_id": ""},
            {"id": "cccccccccccc", "name": " B", "parent_id": "bbbbbbbbbbbb"},
        ]
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._getter(padded)),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-1-100", "folder": "A/B"}
            )
        assert out.startswith("Error:") and "ambiguous" in out
        assert "aaaaaaaaaaaa" in out and "cccccccccccc" in out
        mock_patch.assert_not_called()

    def test_an_id_resolves_either_way(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._getter(self.BOTH)),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-1-100", "folder": "cccccccccccc"},
            )
        assert not out.startswith("Error:")
        assert mock_patch.call_args.args[1] == {"folder_id": "cccccccccccc"}

    def test_the_agent_cannot_mint_a_new_slash_bearing_name(self) -> None:
        """The tool refuses to grow the ambiguity its resolver exists to refuse.

        The sidebar keeps its freedom — a human may still name a folder ``A/B``;
        this only stops the agent adding more unaddressable-by-path names.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Projects/Web"})
        assert out.startswith("Error:") and "cannot contain '/'" in out
        mock_post.assert_not_called()


class TestFolderNameRedaction:
    """A folder name is agent-authored and the sidebar re-renders it forever.

    Persisting a credential the agent quoted into a name would re-display it on
    every visit, so the name takes the egress pass BEFORE the write.
    """

    LEAKY = "AKIAIOSFODNN7EXAMPLE"

    def test_leaf_name_is_redacted_before_the_write(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={"id": "dddddddddddd", "name": "x", "parent_id": ""},
            ) as mock_post,
        ):
            _call_tool_inner("chat_folder_create", {"name": self.LEAKY})
        assert self.LEAKY not in mock_post.call_args.args[1]["name"]

    def test_created_parent_segments_are_redacted_before_the_write(self) -> None:
        posts: list[dict] = []

        def _post(path: str, body: dict, **kw: object) -> dict:
            posts.append(body)
            return {
                "id": f"new{len(posts):09d}",
                "name": body["name"],
                "parent_id": body["parent_id"],
            }

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", side_effect=_post),
        ):
            out = _call_tool_inner(
                "chat_folder_create", {"name": "leaf", "parent": f"kirocrew/{self.LEAKY}"}
            )
        assert all(self.LEAKY not in p["name"] for p in posts)
        assert self.LEAKY not in out


class TestFolderMove:
    def test_reparents_by_path(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "cccccccccccc", "name": "Travel", "parent_id": "aaaaaaaaaaaa"},
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "Travel", "new_parent": "kirocrew"}
            )
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/folders/cccccccccccc"
        assert body == {"parent_id": "aaaaaaaaaaaa"}
        assert "kirocrew/Travel" in out

    def test_move_to_root(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "kirocrew/0811", "new_parent": "root"}
            )
        assert mock_patch.call_args.args[1] == {"parent_id": ""}
        assert "0811" in out

    def test_root_is_not_a_movable_subject(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "root"})
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_cycle_verdict_comes_from_the_endpoint(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"error": "cannot move a folder into its own descendant"},
            ),
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "kirocrew", "new_parent": "kirocrew/0811"}
            )
        assert out.startswith("Error:") and "own descendant" in out

    def test_unknown_folder_errors(self) -> None:
        """Move RESOLVES a folder; it must never create one on the way."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Nope/Missing"})
        assert out.startswith("Error:") and "folder not found" in out
        mock_post.assert_not_called()
        mock_patch.assert_not_called()

    def test_resolve_only_walk_refuses_a_mid_path_duplicate(self) -> None:
        """Ambiguity below the addressed path is refused in resolve mode too."""
        dupes = [
            {"id": "aaaaaaaaaaaa", "name": "kirocrew", "parent_id": ""},
            {"id": "bbbbbbbbbbbb", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
            {"id": "cccccccccccc", "name": "0811", "parent_id": "aaaaaaaaaaaa"},
        ]

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in dupes] if path == "/api/chat/folders" else _slots_with_caller()

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "kirocrew/0811/deeper"})
        assert out.startswith("Error:") and "share the same parent" in out
        assert "bbbbbbbbbbbb" in out and "cccccccccccc" in out
        mock_post.assert_not_called()
        mock_patch.assert_not_called()


class TestFolderMoveSession:
    def test_an_unverifiable_caller_cannot_file_another_session(self) -> None:
        """The lenient resolver would hand a subagent its parent's authority.

        An unresolved identity also reaches the endpoint as no session header at
        all, where it reads as the unconfined dashboard user — so the write is
        refused here rather than sent with an authority nobody can name.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-3-300", "folder": "kirocrew/0811"},
            )
        assert out.startswith("Error:")
        assert "cannot verify which session is calling" in out
        mock_patch.assert_not_called()

    def test_the_verified_key_is_passed_through_unchanged(self) -> None:
        """Re-resolving inside the helper would carry a different authority.

        The key names a slot the fixture actually holds: a ``dashboard:`` key
        with no matching row is the closed-tab race and is refused, so an absent
        one would exercise that refusal instead of the pass-through.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="dashboard:chat-2-200",
            ),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-3-300", "folder": "kirocrew/0811"},
            )
        assert mock_patch.call_args.kwargs["session_key"] == "dashboard:chat-2-200"

    def test_moves_by_slot_key_into_a_path(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"ok": True, "folder_id": "bbbbbbbbbbbb"},
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-3-300", "folder": "kirocrew/0811"},
            )
        path, body = mock_patch.call_args.args
        assert path == "/api/chat/slots/chat-3-300/folder"
        assert body == {"folder_id": "bbbbbbbbbbbb"}
        assert "kirocrew/0811" in out

    def test_accepts_a_dashboard_session_key(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner(
                "chat_folder_move_session",
                {"session": "dashboard:chat-1-100", "folder": "Travel"},
            )
        assert mock_patch.call_args.args[0] == "/api/chat/slots/chat-1-100/folder"

    def test_accepts_an_exact_unique_title(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner(
                "chat_folder_move_session", {"session": "folder mcp", "folder": "Travel"}
            )
        assert mock_patch.call_args.args[0] == "/api/chat/slots/chat-2-200/folder"

    def test_ambiguous_title_refuses_rather_than_guessing(self) -> None:
        dupes = [
            {"key": "chat-1-100", "title": "Same", "folder_id": ""},
            {"key": "chat-2-200", "title": "Same", "folder_id": ""},
        ]

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else dupes

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "Same", "folder": "Travel"}
            )
        assert out.startswith("Error:") and "chat-1-100" in out and "chat-2-200" in out
        mock_patch.assert_not_called()

    def test_partial_title_is_not_a_match(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "Folder", "folder": "Travel"}
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_unknown_session_names_the_archived_limitation(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-nope-1", "folder": "Travel"}
            )
        assert out.startswith("Error:") and "ARCHIVED" in out

    def test_an_explicit_key_never_falls_through_to_title_matching(self) -> None:
        """`dashboard:` asserts a KEY, so an absent key must not resolve by title.

        Honouring the title here would file a session the caller did not name,
        which is the opposite of what the prefix says.
        """
        rows = [
            {"key": "chat-1-100", "title": "dashboard:chat-9-999", "folder_id": ""},
            {"key": "chat-2-200", "title": "Other", "folder_id": ""},
        ]

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else rows

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "dashboard:chat-9-999", "folder": "Travel"},
            )
        assert out.startswith("Error:") and "no live session has the key" in out
        mock_patch.assert_not_called()

    def test_a_key_that_is_also_another_session_title_is_refused(self) -> None:
        """One session's key can be another session's title — that is ambiguous."""
        rows = [
            {"key": "chat-1-100", "title": "Real one", "folder_id": ""},
            {"key": "chat-2-200", "title": "chat-1-100", "folder_id": ""},
        ]

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else rows

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-1-100", "folder": "Travel"}
            )
        assert out.startswith("Error:")
        assert "chat-1-100" in out and "chat-2-200" in out
        mock_patch.assert_not_called()

    def test_the_dashboard_prefix_selects_the_key_through_that_collision(self) -> None:
        rows = [
            {"key": "chat-1-100", "title": "Real one", "folder_id": ""},
            {"key": "chat-2-200", "title": "chat-1-100", "folder_id": ""},
        ]

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else rows

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "dashboard:chat-1-100", "folder": "Travel"},
            )
        assert not out.startswith("Error:")
        assert mock_patch.call_args.args[0] == "/api/chat/slots/chat-1-100/folder"

    def test_unfile_to_top_level(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch", return_value={"ok": True, "folder_id": ""}
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move_session", {"session": "chat-1-100"})
        assert mock_patch.call_args.args[1] == {"folder_id": ""}
        assert "top level" in out

    def test_unknown_destination_folder_never_reaches_the_endpoint(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-1-100", "folder": "Nope"}
            )
        assert out.startswith("Error:") and "folder not found" in out
        mock_patch.assert_not_called()

    def test_slot_key_is_url_quoted(self) -> None:
        """A slot key can be a folded human name; it must not break the path."""
        odd = _slots_with_caller({"key": "Artifact: My Doc", "title": "Doc", "folder_id": ""})

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else odd

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            _call_tool_inner(
                "chat_folder_move_session", {"session": "Artifact: My Doc", "folder": "Travel"}
            )
        assert mock_patch.call_args.args[0] == "/api/chat/slots/Artifact%3A%20My%20Doc/folder"

    def test_unfile_result_is_redacted(self) -> None:
        """A slot key is a folded human name — it can carry a pasted credential.

        Every other return in these tools goes through redact(); the unfile leg
        echoes the key verbatim, so it needs the same pass or a secret reaches
        the model and the tool-result audit.
        """
        leaky = "AKIAIOSFODNN7EXAMPLE"
        rows = _slots_with_caller({"key": leaky, "title": "Leaky", "folder_id": "aaaaaaaaaaaa"})

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else rows

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True, "folder_id": ""}),
        ):
            out = _call_tool_inner("chat_folder_move_session", {"session": leaky})
        assert leaky not in out
        assert "top level" in out

    def test_ambiguous_session_refusal_is_redacted(self) -> None:
        """The refusal lists candidate slot keys — those need redaction too."""
        leaky = "AKIAIOSFODNN7EXAMPLE"
        rows = [
            {"key": leaky, "title": "Same", "folder_id": ""},
            {"key": "chat-2-200", "title": "Same", "folder_id": ""},
        ]

        def _get(path: str) -> list[dict]:
            return [dict(f) for f in _FOLDERS] if path == "/api/chat/folders" else rows

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_get):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "Same", "folder": "Travel"}
            )
        assert out.startswith("Error:")
        assert leaky not in out


class TestNamesTheEndpointWouldTruncate:
    """A name longer than the endpoint's limit is refused, not posted.

    The folder endpoints store ``name[:100]``. Posting a longer one creates a
    folder under a name this server cannot match afterwards, so the NEXT call
    walks the same path, still misses, and creates another sibling — silent
    duplicates under a path the caller never asked for.

    The two arguments are bounded in different places, which is why both are
    tested: ``name`` is capped by its own schema field, while ``parent`` is a
    PATH bounded at 4096, so a single overlong SEGMENT inside it reaches the
    walk and has to be refused there.
    """

    LONG = "x" * 101

    def test_the_schema_refuses_an_overlong_leaf_name(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            with pytest.raises(ValidationError):
                _call_tool_inner("chat_folder_create", {"name": self.LONG})
        mock_post.assert_not_called()

    def test_a_parent_segment_is_refused_before_any_write(self) -> None:
        """The schema's 4096-char path bound cannot see a per-segment overrun."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner(
                "chat_folder_create", {"name": "leaf", "parent": f"Travel/{self.LONG}"}
            )
        assert out.startswith("Error:") and "too long" in out
        mock_post.assert_not_called()

    def test_the_refusal_does_not_echo_a_credential(self) -> None:
        """The refusal quotes the name back, so it redacts what it quotes.

        Every refusal this resolver mints redacts at the source, and it has to:
        ``chat_folder_move`` and ``chat_folder_move_session`` return a resolver
        error verbatim, with no redaction at their own return boundary. So this
        is exercised through ``chat_folder_move`` — testing it through
        ``chat_folder_create`` proves nothing, because that tool wraps its whole
        error in ``redact()`` and would mask an unredacted message.
        """
        leaky = "AKIAIOSFODNN7EXAMPLE" + "z" * 90
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": f"Travel/{leaky}"})
        assert "too long" in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        mock_patch.assert_not_called()

    def test_a_segment_at_the_limit_still_creates(self) -> None:
        """Exactly at the limit round-trips, so the guard is off-by-one clean."""
        at_limit = "y" * 100
        made = {"id": "ffffffffffff", "name": at_limit, "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "leaf", "parent": at_limit})
        assert not out.startswith("Error:")
        assert mock_post.call_count == 2  # the parent segment, then the leaf


class TestASubagentCannotOutrankItsParent:
    """A subagent key matches no slot, but absence must not read as "no app".

    An app that may not touch a foreign session would otherwise gain that reach
    simply by spawning a helper: the helper's key resolves to nothing, and reading
    "nothing" as unscoped is what grants it. A subagent inherits authority; it
    never mints it.
    """

    @staticmethod
    def _mixed(path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in _FOLDERS]
        return [
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-3-300", "title": "Raymond's own", "folder_id": "", "app": ""},
        ]

    def test_a_subagent_is_shown_no_sessions(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="subagent:abc123",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert out.startswith("Error:")
        assert "runs on behalf of whatever created it" in out
        assert "Radar run" not in out and "Raymond's own" not in out

    def test_a_subagent_cannot_reshape_the_tree(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="subagent:abc123",
            ),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Output"})
        assert out.startswith("Error:")
        mock_post.assert_not_called()

    def test_a_subagent_cannot_file_a_session(self) -> None:
        """The resolver reads the withheld list, so the write is refused too."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="subagent:abc123",
            ),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-1-100", "folder": "kirocrew/0811"},
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_an_app_cron_is_shown_no_sessions(self) -> None:
        """A cron can be app-created, so a cron key can carry an app's reach."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="cron:job-abc123",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert out.startswith("Error:")
        assert "Radar run" not in out and "Raymond's own" not in out

    def test_a_cron_cannot_reshape_the_tree(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="cron:job-abc123",
            ),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Output"})
        assert out.startswith("Error:")
        mock_post.assert_not_called()

    def test_the_delegated_list_is_knowingly_incomplete(self) -> None:
        """Pins the position, not a claim of completeness.

        The prefix tuple enumerates the delegated key forms that exist today; a
        form added later reads as unscoped until someone adds it here. That gap
        is accepted deliberately, so this asserts the two known forms are
        covered AND that the code says the list is incomplete — if someone
        deletes that admission, this fails and they have to re-argue it.
        """
        assert set(mcp_dashboard._DELEGATED_CALLER_PREFIXES) == {"subagent:", "cron:"}
        src = inspect.getsource(mcp_dashboard)
        marker = src.split("_DELEGATED_CALLER_PREFIXES = ")[0]
        assert "KNOWINGLY INCOMPLETE" in marker

    def test_a_dashboard_caller_whose_slot_is_gone_is_refused(self) -> None:
        """The closed-tab race: the slot is popped, the call is still in flight.

        Slot removal is synchronous and does not drain in-flight MCP calls, so an
        app-owned session's agent can outlive its own row. Reading that absence
        as "no app" would hand it the authority the app does not have.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="dashboard:chat-gone-999",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert out.startswith("Error:")
        assert "Radar run" not in out and "Raymond's own" not in out

    def test_a_vanished_dashboard_caller_cannot_reshape_the_tree(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="dashboard:chat-gone-999",
            ),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Output"})
        assert out.startswith("Error:")
        mock_post.assert_not_called()

    def test_the_dashboard_refusal_is_not_the_declined_inversion(self) -> None:
        """Pins the SCOPE of this refusal, which is the reason it is acceptable.

        Refusing every unplaceable caller would also cost Slack threads and
        channel sessions these tools — a tradeoff that was weighed and declined.
        This refusal is narrower by construction: it keys on the ``dashboard:``
        prefix, which those callers do not carry. If someone later widens it
        into the blanket inversion, this test fails and says so.
        """
        rows: list[dict] = []
        assert mcp_dashboard._caller_app_scope("dashboard:gone", rows) is None
        # These have no slot either, and must STAY unscoped.
        assert mcp_dashboard._caller_app_scope("slack:T1:C1:1777", rows) == ""
        assert mcp_dashboard._caller_app_scope("channel:C123", rows) == ""

    def test_a_slack_or_channel_caller_is_still_unscoped(self) -> None:
        """The exemption covers delegated callers only — these have no app."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="slack:T1:C1:1777",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert not out.startswith("Error:")
        assert "Radar run" in out and "Raymond's own" in out

    def test_a_subagent_WITH_a_slot_uses_that_slot(self) -> None:
        """Absence is the trigger, not the prefix: a locatable one is scoped."""

        def _rows_with_sub(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in _FOLDERS]
            return [
                {"key": "abc123", "title": "Helper", "folder_id": "", "app": "issue-radar"},
                {"key": "chat-2-200", "title": "Other app", "folder_id": "", "app": "spec-builder"},
            ]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_sub),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="subagent:abc123",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert not out.startswith("Error:")
        assert "Helper" in out
        assert "Other app" not in out


class TestTheFolderPolicyIsTheEndpointsNotThisServers:
    """Folders carry an owner now, so an app HAS a folder of its own to write to
    and this server stops deciding the policy: the tool call reaches the
    endpoint, which bounds the write to the caller's own folders under the store
    lock. A second copy of that rule here could only drift or race it.

    What this layer still decides is the one thing the endpoint cannot — whether
    the caller can be placed at all.
    """

    @staticmethod
    def _mixed(path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in _FOLDERS]
        return [
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-3-300", "title": "Raymond's own", "folder_id": "", "app": ""},
        ]

    def _as(self, slot: str) -> Any:
        return patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value=f"dashboard:{slot}",
        )

    def test_an_apps_create_reaches_the_endpoint(self) -> None:
        """An app's create reaches the endpoint, which stamps the owner."""
        made = {"id": "new000000001", "name": "Radar output", "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Radar output"})
        assert not out.startswith("Error:")
        assert mock_post.called

    def test_an_apps_move_reaches_the_endpoint(self) -> None:
        moved = {"id": "fldr00000002", "name": "0811", "parent_id": "fldr00000003"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._patch", return_value=moved) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "kirocrew/0811", "new_parent": "Travel"}
            )
        assert not out.startswith("Error:")
        assert mock_patch.called

    def test_the_endpoints_ownership_refusal_is_surfaced_not_reinvented(self) -> None:
        """The tool must report the endpoint's verdict rather than pre-judging
        it — that is what keeps one rule in one place."""
        denied = {"error": "this app does not own that folder", "code": "folder_not_owned"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._patch", return_value=denied),
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "kirocrew/0811", "new_parent": "Travel"}
            )
        assert out.startswith("Error:")
        assert "does not own that folder" in out

    def test_an_app_can_still_file_its_own_session(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._patch", return_value={"ok": True}) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "chat-1-100", "folder": "kirocrew/0811"},
            )
        assert not out.startswith("Error:")
        assert mock_patch.called

    def test_an_app_can_still_read_the_tree(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed), self._as("chat-1-100"):
            out = _call_tool_inner("chat_folder_tree", {})
        assert not out.startswith("Error:")
        assert "kirocrew" in out

    def test_the_person_keeps_full_authority(self) -> None:
        """Reorganising sessions is the point of the tools — an unscoped caller
        is the person's own agent and is not confined."""
        made = {"id": "new000000001", "name": "Q3", "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-3-300"),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Q3"})
        assert not out.startswith("Error:")
        assert mock_post.called

    def test_an_unverifiable_caller_cannot_reshape_it_either(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Q3"})
        assert out.startswith("Error:")
        assert "cannot verify which session is calling" in out
        mock_post.assert_not_called()

    def test_an_app_owned_linked_session_is_refused(self) -> None:
        """A channel- or cron-bound slot runs under linked_session_key, and the
        endpoint's staleness guard is dashboard:-only BY DESIGN -- for any other
        shape, absence cannot be told from "never had a slot". So this layer,
        which resolved the scope positively, has to refuse it."""

        def rows(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in _FOLDERS]
            return [
                {
                    "key": "chat-5-500",
                    "title": "Radar channel",
                    "folder_id": "",
                    "app": "issue-radar",
                    "linked_session_key": "channel:C123",
                }
            ]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=rows),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="channel:C123",
            ),
            patch("kiro_crew.mcp_dashboard._post") as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Runs"})
        assert out.startswith("Error:")
        assert "channel- or schedule-bound" in out
        mock_post.assert_not_called()

    def test_a_channel_session_with_no_app_still_works(self) -> None:
        """The refusal is scoped to an APP-owned linked session. A person's own
        channel session never had an app and keeps full authority."""

        def rows(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in _FOLDERS]
            return [{"key": "chat-5-500", "title": "Mine", "folder_id": "", "app": ""}]

        made = {"id": "new000000001", "name": "Runs", "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=rows),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="slack:T1/C1",
            ),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            out = _call_tool_inner("chat_folder_create", {"name": "Runs"})
        assert not out.startswith("Error:")
        assert mock_post.called


class TestEveryFolderWriteCarriesTheVerifiedKey:
    """The endpoint's ownership rule is only as good as the identity that
    reaches it, so the key the gate STRICTLY verified must be the key the write
    sends. The write helpers default to the lenient resolver, whose /proc
    ancestor walk can land on a different slot -- for an app-owned session that
    makes the write arrive looking like the unconfined person, which would check
    one identity and write under another.
    """

    @staticmethod
    def _mixed(path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in _FOLDERS]
        return [
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
        ]

    def _as(self, slot: str) -> Any:
        return patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value=f"dashboard:{slot}",
        )

    def test_create_sends_the_verified_key(self) -> None:
        made = {"id": "new000000001", "name": "Runs", "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            _call_tool_inner("chat_folder_create", {"name": "Runs"})
        assert mock_post.call_args.kwargs["session_key"] == "dashboard:chat-1-100"

    def test_mkdir_p_segments_are_created_under_the_verified_key(self) -> None:
        """The intermediate segments are real folders, so each write needs it too."""
        made = {"id": "new000000001", "name": "seg", "parent_id": ""}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._post", return_value=made) as mock_post,
        ):
            _call_tool_inner("chat_folder_create", {"name": "Leaf", "parent": "Fresh/Deep"})
        assert mock_post.call_count > 1
        for call in mock_post.call_args_list:
            assert call.kwargs["session_key"] == "dashboard:chat-1-100"

    def test_move_sends_the_verified_key(self) -> None:
        moved = {"id": "fldr00000002", "name": "0811", "parent_id": "fldr00000003"}
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            self._as("chat-1-100"),
            patch("kiro_crew.mcp_dashboard._patch", return_value=moved) as mock_patch,
        ):
            _call_tool_inner(
                "chat_folder_move", {"folder": "kirocrew/0811", "new_parent": "Travel"}
            )
        assert mock_patch.call_args.kwargs["session_key"] == "dashboard:chat-1-100"


class TestTheSessionListIsScopedToTheCaller:
    """The endpoint is not app-scoped, so this server has to be.

    Without it an app agent holding the set reads every session's title and key
    — across other apps and the person's own work — through `chat_folder_tree`.
    """

    @staticmethod
    def _mixed(path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [{"id": "aaaaaaaaaaaa", "name": "Work", "parent_id": ""}]
        return [
            {"key": "chat-1-100", "title": "Radar run", "folder_id": "", "app": "issue-radar"},
            {"key": "chat-2-200", "title": "Spec draft", "folder_id": "", "app": "spec-builder"},
            {"key": "chat-3-300", "title": "Raymond's own work", "folder_id": "", "app": ""},
        ]

    def test_an_app_sees_only_its_own_sessions(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="dashboard:chat-1-100",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert "Radar run" in out
        assert "Spec draft" not in out
        assert "Raymond's own work" not in out

    def test_the_user_sees_every_session(self) -> None:
        """An unscoped caller is the person's own agent — the point of the tools."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="dashboard:chat-3-300",
            ),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert "Radar run" in out and "Spec draft" in out and "Raymond's own work" in out

    def test_an_unverifiable_caller_is_shown_nothing(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
        ):
            out = _call_tool_inner("chat_folder_tree", {})
        assert out.startswith("Error:")
        assert "cannot verify which session is calling" in out
        assert "Radar run" not in out and "Spec draft" not in out

    def test_an_app_cannot_resolve_a_foreign_session_by_title(self) -> None:
        """The resolver reads the same filtered list, so scope covers writes too."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch(
                "kiro_crew.mcp_core._resolve_session_key_strict",
                return_value="dashboard:chat-1-100",
            ),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session",
                {"session": "Spec draft", "folder": "Work"},
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()


class TestPrivateSessionsAreInvisible:
    """Incognito and temporary sessions are out of the record by the user's choice.

    ``/api/chat/slots`` returns them like any other row, so these tools filter
    them: an agent tidying folders must not learn a private session's title or
    key, and must not be able to file one anywhere.
    """

    def _mixed(self, path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [dict(f) for f in _FOLDERS]
        return [
            {
                "key": "chat-1-100",
                "title": "Public work",
                "folder_id": "",
                "memory_mode": "persistent",
            },
            {
                "key": "chat-9-900",
                "title": "Secret thing",
                "folder_id": "",
                "memory_mode": "incognito",
            },
            {
                "key": "chat-8-800",
                "title": "Scratch pad",
                "folder_id": "",
                "memory_mode": "temporary",
            },
        ]

    def test_no_archived_count_is_rendered(self) -> None:
        """An archived transcript may be a private one, so the count stays out.

        ``history_count`` counts filed history with no ``memory_mode`` to filter
        on, so a folder holding one incognito conversation would disclose it as a
        number. The invariant is per-server, not per-tool: nothing rendered here
        may reveal a non-persistent session, and a count this server cannot prove
        clean is therefore never emitted — not even when it is large.
        """
        folders = [{"id": "aaaaaaaaaaaa", "name": "Work", "parent_id": "", "history_count": 42}]

        def _rows_with_history(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in folders]
            return _slots_with_caller()

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_history):
            out = _call_tool_inner("chat_folder_tree", {})
        assert "Work" in out
        assert "42" not in out and "archived" not in out

    def test_the_tree_omits_them(self) -> None:
        with patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed):
            out = _call_tool_inner("chat_folder_tree", {})
        assert "Public work" in out
        assert "Secret thing" not in out and "chat-9-900" not in out
        assert "Scratch pad" not in out and "chat-8-800" not in out

    def test_one_cannot_be_moved_by_key(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "chat-9-900", "folder": "Work"}
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()

    def test_one_cannot_be_moved_by_title(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._mixed),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move_session", {"session": "Secret thing", "folder": "Work"}
            )
        assert out.startswith("Error:")
        mock_patch.assert_not_called()


class TestTheVerifiedCallerKeyReachesTheRequest:
    """The gate resolves the caller strictly; the request must SEND that key.

    Gating on `_resolve_session_key_strict` and then letting the request helper
    resolve again authorizes the check and the action as potentially different
    sessions: the lenient walk reads mutable process state, so what it answers
    at request time need not be what the gate approved. The endpoint authorizes
    on the key it receives, which makes the sent key the security-relevant one.
    """

    VERIFIED = "dashboard:chat-verified"

    def test_create_carries_the_verified_key(self):
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch(
                "kiro_crew.mcp_dashboard._post", return_value={"target": "chat-2", "title": "w"}
            ) as post,
        ):
            _call_tool_inner("session_create", {"title": "worker"})
        assert post.call_args.kwargs["session_key"] == self.VERIFIED

    def test_stop_carries_the_verified_key(self):
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch("kiro_crew.mcp_dashboard._post", return_value={"ok": True}) as post,
        ):
            _call_tool_inner("session_stop", {"target": "peer"})
        assert post.call_args.kwargs["session_key"] == self.VERIFIED

    def test_read_carries_the_verified_key(self):
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch("kiro_crew.mcp_dashboard._get", return_value={"messages": [], "total": 0}) as get,
        ):
            _call_tool_inner("session_read_message", {"target": "peer"})
        # `_get` takes the key positionally, matching its signature.
        assert get.call_args.args[1] == self.VERIFIED

    def test_a_re_sent_stop_is_not_reported_as_nothing_to_stop(self):
        """A de-duplicated retry lands on the no-op reply routinely.

        Its earlier cooperative stop IS still in flight, so rendering it the way a
        never-running target is rendered would tell the caller the opposite of what
        happened — and invite it to act as though the target were free-running.

        Mutation guard: ignoring `already_stopping` restores "nothing to stop".
        """
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={
                    "ok": True,
                    "target": "peer",
                    "info": "stop already in progress",
                    "already_stopping": True,
                },
            ),
        ):
            out = _call_tool_inner("session_stop", {"target": "peer"})
        assert "stop already in progress" in out
        assert "nothing to stop" not in out
        assert "the earlier stop still stands" in out

    def test_a_target_that_was_never_running_still_says_nothing_to_stop(self):
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch(
                "kiro_crew.mcp_dashboard._post",
                return_value={
                    "ok": True,
                    "target": "peer",
                    "info": "not running",
                    "already_stopping": False,
                },
            ),
        ):
            out = _call_tool_inner("session_stop", {"target": "peer"})
        assert "nothing to stop" in out

    def test_an_empty_window_still_hands_back_the_cursor(self):
        """A poll loop's commonest answer is empty, and it must not lose its place.

        Without the cursor the caller either re-reads with no `since` -- taking the
        tail, which skips everything older than the last `limit` rows once the
        target answers in a burst -- or reuses a stale position and re-reads rows it
        has already seen.

        Mutation guard: returning only the head line and "No messages" fails here.
        """
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch(
                "kiro_crew.mcp_dashboard._get",
                return_value={"messages": [], "total": 7, "next_since": 7},
            ),
        ):
            out = _call_tool_inner("session_read_message", {"target": "peer"})
        assert "since=7" in out, "an empty window must still carry next_since"

    def test_a_trimmed_transcript_invents_no_cursor_on_an_empty_window(self):
        """The renderer never invents a cursor the response did not carry.

        The live server now returns `next_since` on trimmed sessions too, so
        this pins the renderer's defensive behaviour for a cursor-less
        response shape, whatever produces one.
        """
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=self.VERIFIED),
            patch("kiro_crew.mcp_dashboard._get", return_value={"messages": [], "total": 7}),
        ):
            out = _call_tool_inner("session_read_message", {"target": "peer"})
        assert "since=" not in out, "no cursor may be invented once rows are trimmed"

    def test_an_unverifiable_caller_never_reaches_the_request(self):
        """The refusal must precede the call, not merely alter its key."""
        with (
            patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=""),
            patch("kiro_crew.mcp_dashboard._post") as post,
            patch("kiro_crew.mcp_dashboard._get") as get,
        ):
            for tool, args in (
                ("session_create", {"title": "worker"}),
                ("session_stop", {"target": "peer"}),
                ("session_read_message", {"target": "peer"}),
            ):
                out = _call_tool_inner(tool, args)
                assert "cannot be identified" in out
        post.assert_not_called()
        get.assert_not_called()


class TestSessionCreateFolder:
    """`session_create.folder` — filing atomic with creation.

    The reference resolves with `chat_folder_create`'s `parent` semantics
    (missing segments created), which is tree shaping — so the SAME gate
    applies, not a second authorization path. The endpoint receives the
    resolved id and re-confirms it in the create's own synchronous window;
    these cases cover the MCP half: gate, resolution, payload shape, refusal.
    """

    CREATED = {"target": "chat-9-900", "title": "worker", "folder_id": "bbbbbbbbbbbb"}

    def test_files_at_creation_with_the_resolved_id(self) -> None:
        """One POST, to the create route, already carrying the folder id."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", return_value=dict(self.CREATED)) as post,
        ):
            out = _call_tool_inner("session_create", {"title": "worker", "folder": "kirocrew/0811"})
        assert post.call_count == 1, "filing must not be a second call after the create"
        path, body = post.call_args.args
        assert path == "/api/session-control/create"
        assert body["folder_id"] == "bbbbbbbbbbbb"
        assert "kirocrew/0811" in out

    def test_missing_segments_are_created_like_a_parent_ref(self) -> None:
        """mkdir -p over the reference, then the create rides the new leaf id."""
        made = {"id": "dddddddddddd", "name": "fresh", "parent_id": "aaaaaaaaaaaa"}

        def _post_route(path, body, **kwargs):
            if path == "/api/chat/folders":
                return dict(made)
            assert path == "/api/session-control/create"
            return {**self.CREATED, "folder_id": body["folder_id"]}

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", side_effect=_post_route) as post,
        ):
            out = _call_tool_inner(
                "session_create", {"title": "worker", "folder": "kirocrew/fresh"}
            )
        create_call = post.call_args_list[-1]
        assert create_call.args[0] == "/api/session-control/create"
        assert create_call.args[1]["folder_id"] == "dddddddddddd"
        assert "created folder path" in out

    def test_an_unresolvable_folder_refuses_the_whole_create(self) -> None:
        """No session may exist when the filing half cannot be honored.

        An id-shaped reference that does not exist is a lookup failure even
        under mkdir -p (ids are minted server-side), and 'created but unfiled'
        would silently honor half the request — so the create POST never fires.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as post,
        ):
            out = _call_tool_inner("session_create", {"title": "worker", "folder": "ffffffffffff"})
        assert out.startswith("Error:")
        assert "folder not found" in out
        post.assert_not_called()

    def test_folder_resolution_rides_the_tree_shaping_gate(self) -> None:
        """A caller the gate refuses cannot file-by-naming at create time.

        Resolution can CREATE folders, so it is tree shaping: reusing the gate —
        rather than a second authorization path — is what keeps 'could not
        reshape the tree by creating a folder' and 'cannot reach the same write
        through session_create' the same statement.
        """
        with (
            patch(
                "kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable",
                return_value=("", "", "Error: refused by the tree-shaping gate"),
            ),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post") as post,
        ):
            out = _call_tool_inner("session_create", {"title": "worker", "folder": "kirocrew/0811"})
        assert out == "Error: refused by the tree-shaping gate"
        post.assert_not_called()

    def test_no_folder_means_no_gate(self) -> None:
        """A plain create is not tree shaping and must not grow that refusal."""
        with (
            patch("kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable") as gate,
            patch(
                "kiro_crew.mcp_dashboard._post", return_value={"target": "chat-9", "title": "w"}
            ) as post,
        ):
            _call_tool_inner("session_create", {"title": "worker"})
        gate.assert_not_called()
        assert post.call_count == 1
        assert "folder_id" not in post.call_args.args[1]

    def test_an_app_scoped_caller_cannot_leave_folder_segments_behind(self) -> None:
        """The endpoint refuses `app_scoped_caller`, so resolution must not run.

        An app may create folders in its own subtree, but it can never complete
        session_create — resolving (and mkdir -p creating) the folder for it
        would leave path segments behind for a call that cannot succeed. The
        refusal here is a side-effect guard; the endpoint's own refusal stays
        authoritative.
        """

        def _rows_with_app(path: str) -> list[dict]:
            if path == "/api/chat/slots":
                return [{"key": "chat-1-100", "title": "Caller", "app": "some-app"}]
            return _rows(path)

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_with_app),
            patch("kiro_crew.mcp_dashboard._post") as post,
        ):
            out = _call_tool_inner(
                "session_create", {"title": "worker", "folder": "kirocrew/fresh"}
            )
        assert out.startswith("Error:")
        assert "app-scoped" in out
        post.assert_not_called()

    def test_segment_creation_writes_under_the_gates_verified_key(self) -> None:
        """The gate's returned key is what the folder writes carry — its contract."""
        made = {"id": "dddddddddddd", "name": "fresh", "parent_id": "aaaaaaaaaaaa"}

        def _post_route(path, body, **kwargs):
            if path == "/api/chat/folders":
                return dict(made)
            return dict(self.CREATED)

        with (
            patch(
                "kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable",
                return_value=("dashboard:gate-key", "", None),
            ),
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._post", side_effect=_post_route) as post,
        ):
            _call_tool_inner("session_create", {"title": "worker", "folder": "kirocrew/fresh"})
        folder_call = post.call_args_list[0]
        assert folder_call.args[0] == "/api/chat/folders"
        assert folder_call.kwargs["session_key"] == "dashboard:gate-key"


class TestAdvertisedSet:
    """Reaching this server means an agent spec referenced it.

    The assignment happened in that spec, so the process has nothing left to
    decide: it advertises its whole set.
    """

    def test_the_whole_set_is_advertised(self) -> None:
        names = {t["name"] for t in _list_tools()}
        assert names == {
            "chat_folder_tree",
            "chat_folder_create",
            "chat_folder_move",
            "chat_folder_move_session",
            "session_create",
            "session_stop",
            "session_close",
            "session_send",
            "session_read_message",
        }


#: Four root folders with explicit, contiguous positions — the shape a sidebar
#: drag leaves behind (``computeReorderedFolders`` renumbers 0..n-1), so these
#: cases assert the tool writes what a drag would have written.
_ORDERED = [
    {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
    {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 1},
    {"id": "cccccccccccc", "name": "Charlie", "parent_id": "", "order": 2},
    {"id": "dddddddddddd", "name": "Delta", "parent_id": "", "order": 3},
]


def _ordered_rows(path: str) -> list[dict]:
    if path == "/api/chat/folders":
        return [dict(f) for f in _ORDERED]
    if path == "/api/chat/slots":
        return _slots_with_caller()
    raise AssertionError(f"unexpected GET {path}")


def _patched_orders(mock_patch: Any) -> dict[str, int]:
    """``{folder_id: order}`` over every PATCH the call issued."""
    out: dict[str, int] = {}
    for call in mock_patch.call_args_list:
        path, body = call.args[0], call.args[1]
        if "order" in body:
            out[path.rsplit("/", 1)[-1]] = body["order"]
    return out


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
        for name in ("\u00dfeta", "\u0130stanbul", "\u00c9clair", "\u01c5", "stra\u00dfe"):
            key = mcp_dashboard._chat_folder_name_key({"name": name})
            assert key == name.encode("utf-16-be", "surrogatepass"), name
        assert mcp_dashboard._chat_folder_name_key({"name": "STRASSE"}) == (
            "strasse".encode("utf-16-be")
        )

    def test_a_dotted_capital_i_is_not_folded(self) -> None:
        """The concrete skew shape: a character whose fold is version-dependent.

        `\u0130` (LATIN CAPITAL I WITH DOT ABOVE) lowercases to a two-character
        sequence in Python, and the exact result has moved across Unicode versions.
        Unfolded it is one code unit on both sides and cannot skew.
        """
        key = mcp_dashboard._chat_folder_name_key({"name": "\u0130"})
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
            {"id": "bbbbbbbbbbbb", "name": "\uff21", "parent_id": "", "order": 0},
        ]
        names = [str(f["name"]) for f in mcp_dashboard._chat_folder_siblings(rows, "")]
        assert names == ["\U0001f600", "\uff21"]
        # And the naive key would have put them the other way round.
        assert sorted((str(r["name"]) for r in rows)) == ["\uff21", "\U0001f600"]

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
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Alpha"})
        assert not out.startswith("Error:")
        # Alpha 0, Delta 1, Bravo 2, Charlie 3.
        assert _patched_orders(mock_patch) == {
            "dddddddddddd": 1,
            "bbbbbbbbbbbb": 2,
            "cccccccccccc": 3,
        }
        # The moved row's own position rides along with the reparent: one write
        # for the folder this call is about.
        first_path, first_body = mock_patch.call_args_list[0].args[:2]
        assert first_path.endswith("dddddddddddd")
        # No parent_id: the folder is already at the top level, and sending it
        # back would make the endpoint judge this as a reparent.
        assert first_body == {"order": 1}
        assert "after `Alpha`" in out

    def test_before_an_anchor_lands_immediately_ahead_of_it(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "before": "Bravo"})
        assert not out.startswith("Error:")
        assert _patched_orders(mock_patch) == {
            "dddddddddddd": 1,
            "bbbbbbbbbbbb": 2,
            "cccccccccccc": 3,
        }
        assert "before `Bravo`" in out

    def test_an_anchor_alone_reorders_without_moving(self) -> None:
        """The reason an anchor may stand in for ``new_parent``.

        An omitted ``new_parent`` means the TOP LEVEL, so demanding one would
        make repositioning inside a folder inexpressible: every call would drag
        the folder out to the root as the price of ordering it.
        """
        nested = [
            {"id": "pppppppppppp", "name": "Parent", "parent_id": "", "order": 0},
            {
                "id": "aaaaaaaaaaaa",
                "name": "Alpha",
                "parent_id": "pppppppppppp",
                "order": 0,
            },
            {
                "id": "bbbbbbbbbbbb",
                "name": "Bravo",
                "parent_id": "pppppppppppp",
                "order": 1,
            },
        ]

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in nested]
            return _slots_with_caller()

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={
                    "id": "bbbbbbbbbbbb",
                    "name": "Bravo",
                    "parent_id": "pppppppppppp",
                },
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "Parent/Bravo", "before": "Parent/Alpha"}
            )
        assert not out.startswith("Error:")
        # Stays inside Parent — the anchor chose the destination. Alpha sits at 0
        # and nothing precedes it, so one write puts Bravo ahead of it.
        assert mock_patch.call_count == 1
        # Stays inside Parent, so parent_id is omitted and only the position
        # is written.
        assert mock_patch.call_args_list[0].args[1] == {"order": -1}

    def test_only_folders_whose_position_changes_are_written(self) -> None:
        """A no-op reposition must not spend a write per sibling."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Bravo", "after": "Alpha"})
        assert not out.startswith("Error:")
        # Bravo already sits right after Alpha, so every sibling keeps its
        # number and only the reparent write goes out.
        assert _patched_orders(mock_patch) == {}
        # Same parent AND the position it already holds: nothing to write.
        assert mock_patch.call_count == 0

    def test_a_same_parent_reposition_is_not_reported_as_a_move(self) -> None:
        """ "Moved to `Delta`" would name the folder's OWN path as a destination.

        The reparent did not happen, so the result names what did change.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ),
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Alpha"})
        assert out.startswith("Repositioned folder")
        assert "after `Alpha`" in out and "(top level)" in out
        assert "Moved" not in out

    def test_a_reparent_that_also_positions_says_both(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={
                    "id": "cccccccccccc",
                    "name": "Travel",
                    "parent_id": "aaaaaaaaaaaa",
                },
            ),
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "Travel", "after": "kirocrew/0811"}
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
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "before": "Alpha"})
        assert not out.startswith("Error:")
        assert mock_patch.call_count == 1
        assert _patched_orders(mock_patch) == {"dddddddddddd": -1}

    def test_a_gap_between_neighbours_is_used_instead_of_renumbering(self) -> None:
        """A deleted folder leaves a gap, and a gap is a free slot."""
        gapped = [
            {"id": "aaaaaaaaaaaa", "name": "Alpha", "parent_id": "", "order": 0},
            {"id": "bbbbbbbbbbbb", "name": "Bravo", "parent_id": "", "order": 10},
            {"id": "dddddddddddd", "name": "Delta", "parent_id": "", "order": 20},
        ]

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in gapped]
            return _slots_with_caller()

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Alpha"})
        assert not out.startswith("Error:")
        assert mock_patch.call_count == 1
        # Midpoint of the 0..10 gap, so a later insert on either side still fits.
        assert _patched_orders(mock_patch) == {"dddddddddddd": 5}

    def test_an_app_cannot_position_a_folder_whose_subtree_holds_a_foreign_one(self) -> None:
        """Positioning takes the descendants with it, so the blast radius is the subtree.

        The app owns the row it names, so the moved-folder check passes. But the
        person's folder is nested inside it, and moving the parent relocates the
        child — the same violation the endpoint refuses on a reparent, reached one
        level down. The rule is imported from the endpoint rather than restated, so
        the two cannot disagree about it.
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

        def _rows_only(path: str, **_kw: object) -> list[dict]:
            if path == "/api/chat/folders":
                return rows
            return [{"key": "chat-1-1", "app": "x"}]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_only),
            patch(
                "kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable",
                return_value=("dashboard:chat-1-1", "x", None),
            ),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "AppRoot", "after": "AppTwo"})
        assert out.startswith("Error:"), out
        assert "contains folders this app does not own" in out, out
        mock_patch.assert_not_called()

    def test_an_app_cannot_position_a_folder_it_does_not_own(self) -> None:
        """Positioning is relative, so it does not need a write to its own target.

        The endpoint refuses an app a write to a foreign row, and it refuses an app
        a REPARENT of a foreign subtree — but a pure reposition sends no
        `parent_id`, so the reparent rule never fires, and when the moved folder
        already holds its target number no write to it is issued at all. Renumbering
        the app's OWN siblings around it then changes where the person's folder
        renders, with nothing for the endpoint to refuse.
        """
        rows = [
            {"id": "pppppppppppp", "name": "Person", "parent_id": "", "order": 0},
            {"id": "aaaaaaaaaaaa", "name": "AppOne", "parent_id": "", "order": 1, "owner_app": "x"},
            {"id": "bbbbbbbbbbbb", "name": "AppTwo", "parent_id": "", "order": 2, "owner_app": "x"},
        ]

        def _rows_only(path: str, **_kw: object) -> list[dict] | dict:
            if path == "/api/chat/folders":
                return rows
            return [{"key": "chat-1-1", "app": "x"}]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows_only),
            patch(
                "kiro_crew.mcp_dashboard._refuse_tree_shaping_if_unverifiable",
                return_value=("dashboard:chat-1-1", "x", None),
            ),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "pppppppppppp", "name": "Person", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Person", "after": "AppTwo"})
        assert out.startswith("Error:"), out
        assert "does not own" in out, out
        mock_patch.assert_not_called()

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
        for case in spec["cases"]:
            rows = [{**r, "parent_id": "", **_fixture_name(r)} for r in case["rows"]]
            got = [f["id"] for f in mcp_dashboard._chat_folder_siblings(rows, "")]
            assert got == case["expected"], case["name"]

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
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "before": "Alpha"})
        assert not out.startswith("Error:")
        for call in mock_patch.call_args_list:
            assert "parent_id" not in call.args[1], call.args[1]

    def test_a_real_reparent_still_sends_parent_id(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "cccccccccccc", "name": "Travel", "parent_id": "aaaaaaaaaaaa"},
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "Travel", "new_parent": "kirocrew"}
            )
        assert not out.startswith("Error:")
        assert mock_patch.call_args_list[0].args[1] == {"parent_id": "aaaaaaaaaaaa"}

    def test_both_anchors_at_once_is_refused(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move",
                {"folder": "Delta", "before": "Alpha", "after": "Bravo"},
            )
        assert out.startswith("Error:") and "not both" in out
        mock_patch.assert_not_called()

    def test_an_anchor_outside_the_destination_is_refused(self) -> None:
        """``before``/``after`` names a SIBLING, so it must live in the destination."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move",
                {"folder": "Travel", "new_parent": "root", "after": "kirocrew/0811"},
            )
        assert out.startswith("Error:") and "not in the destination" in out
        mock_patch.assert_not_called()

    def test_a_folder_cannot_anchor_on_itself(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Delta"})
        assert out.startswith("Error:") and "relative to itself" in out
        mock_patch.assert_not_called()

    def test_root_is_not_an_anchor(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "root"})
        assert out.startswith("Error:") and "SIBLING" in out
        mock_patch.assert_not_called()

    def test_a_missing_anchor_is_refused_before_any_write(self) -> None:
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Echo"})
        assert out.startswith("Error:") and "folder not found" in out
        mock_patch.assert_not_called()

    def test_a_failed_order_write_says_the_position_itself_landed(self) -> None:
        """The two halves have different outcomes, so they get different words.

        Re-moving a folder that already arrived is the mistake this prevents. The
        wording also has to match what happened: with the parent unchanged there was
        no move, so naming one would describe a reparent that did not occur — and it
        would say it in the one message a caller reads while deciding what to retry.
        """
        calls: list[dict] = []

        def _fake_patch(path: str, body: dict, **_kw: Any) -> dict:
            calls.append(body)
            # Keyed on the ROW, not on parent_id: a same-parent reposition
            # writes only `order`, so the moved folder carries no parent_id.
            if path.endswith("dddddddddddd"):
                return {"id": "dddddddddddd", "name": "Delta", "parent_id": ""}
            return {"error": "this app does not own that folder"}

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch("kiro_crew.mcp_dashboard._patch", side_effect=_fake_patch),
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Alpha"})
        assert "stopped partway" in out
        assert "Repositioned folder" in out, out
        assert "Moved folder" not in out, out
        assert not out.startswith("Error:")


class TestPositionRenumberIsAllOrNothingForAnApp:
    """An app may not half-shuffle the person's sidebar.

    Repositioning is several writes and the endpoint judges each against this
    app's ownership on its own, so a refusal landing midway would leave an order
    nobody chose and nothing to roll it back with. The whole set is therefore
    checked before the first write.
    """

    @staticmethod
    def _owned(path: str) -> list[dict]:
        if path == "/api/chat/folders":
            return [
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
        return [
            {
                "key": "chat-1-100",
                "title": "Radar run",
                "folder_id": "",
                "app": "issue-radar",
            },
        ]

    def test_renumbering_a_folder_the_app_does_not_own_is_refused_up_front(
        self,
    ) -> None:
        # Alpha and Bravo hold adjacent integers, so landing BETWEEN them has no
        # free slot and can only be reached by renumbering the person's two rows.
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._owned),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Radar out", "after": "Alpha"})
        assert out.startswith("Error:") and "does not own" in out
        mock_patch.assert_not_called()

    def test_an_app_may_place_its_own_folder_where_a_slot_is_free(self) -> None:
        """The rule is about renumbering the person's rows, not about positioning.

        Ahead of Alpha the slot is free, so the app writes only its OWN row: none
        of the person's folders change, and the endpoint judges that single write.
        """
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._owned),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "cccccccccccc", "name": "Radar out", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Radar out", "before": "Alpha"})
        assert not out.startswith("Error:")
        assert mock_patch.call_count == 1
        assert _patched_orders(mock_patch) == {"cccccccccccc": -1}

    def test_the_same_move_without_a_position_still_reaches_the_endpoint(self) -> None:
        """The refusal is about the RENUMBER, not about moving at all."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=self._owned),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={
                    "id": "cccccccccccc",
                    "name": "Radar out",
                    "parent_id": "aaaaaaaaaaaa",
                },
            ) as mock_patch,
        ):
            out = _call_tool_inner(
                "chat_folder_move", {"folder": "Radar out", "new_parent": "Alpha"}
            )
        assert not out.startswith("Error:")
        assert mock_patch.called

    def test_a_lone_FOREIGN_order_write_is_still_preflighted(self) -> None:
        """A renumber can change exactly ONE row, and not the moved folder's.

        Gating the preflight on a multi-row set let this through: the app's own
        reparent landed, then the single foreign order write was refused at the
        endpoint, leaving the sidebar in an order nobody chose. The count is not
        the question — whether any row belongs to someone else is.

        Here the app's folder already holds its target position, so the only row
        whose order changes is the person's Bravo.
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

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in rows]
            return [
                {
                    "key": "chat-1-100",
                    "title": "Radar run",
                    "folder_id": "",
                    "app": "issue-radar",
                },
            ]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch("kiro_crew.mcp_dashboard._patch") as mock_patch,
        ):
            # Alpha(0) and the app's own Radar out(1) are adjacent, so landing
            # between them renumbers; Radar out keeps position 1 and only the
            # person's Bravo has to move.
            out = _call_tool_inner("chat_folder_move", {"folder": "Radar out", "after": "Alpha"})
        assert out.startswith("Error:") and "does not own" in out
        mock_patch.assert_not_called()

    def test_a_person_reordering_their_own_tree_is_not_gated(self) -> None:
        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in _ORDERED]
            return [{"key": "chat-1-100", "title": "Raymond", "folder_id": "", "app": ""}]

        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_get),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ) as mock_patch,
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "before": "Alpha"})
        assert not out.startswith("Error:")
        # Alpha is first, so the slot ahead of it is free — one write, no renumber.
        assert _patched_orders(mock_patch) == {"dddddddddddd": -1}


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

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in reversed_alpha]
            return _slots_with_caller()

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_get):
            out = _call_tool_inner("chat_folder_tree", {})
        lines = [ln for ln in out.splitlines() if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert [ln.split()[1] for ln in lines] == ["Zulu", "Mike", "Alpha"]

    def test_a_child_still_follows_its_own_parent(self) -> None:
        """Order sequences siblings; it never reparents the render."""
        nested = [
            {"id": "aaaaaaaaaaaa", "name": "First", "parent_id": "", "order": 0},
            {
                "id": "bbbbbbbbbbbb",
                "name": "Deep",
                "parent_id": "aaaaaaaaaaaa",
                "order": 99,
            },
            {"id": "cccccccccccc", "name": "Second", "parent_id": "", "order": 1},
        ]

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in nested]
            return _slots_with_caller()

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_get):
            out = _call_tool_inner("chat_folder_tree", {})
        body = out.splitlines()
        order = [ln.split()[1] for ln in body if ln.strip().startswith(("aaaa", "bbbb", "cccc"))]
        assert order == ["First", "First/Deep", "Second"]

    def test_a_parent_cycle_neither_hangs_nor_swallows_its_folders(self) -> None:
        cyclic = [
            {
                "id": "aaaaaaaaaaaa",
                "name": "Ping",
                "parent_id": "bbbbbbbbbbbb",
                "order": 0,
            },
            {
                "id": "bbbbbbbbbbbb",
                "name": "Pong",
                "parent_id": "aaaaaaaaaaaa",
                "order": 1,
            },
            {"id": "cccccccccccc", "name": "Sane", "parent_id": "", "order": 2},
        ]

        def _get(path: str) -> list[dict]:
            if path == "/api/chat/folders":
                return [dict(f) for f in cyclic]
            return _slots_with_caller()

        with patch("kiro_crew.mcp_dashboard._get", side_effect=_get):
            out = _call_tool_inner("chat_folder_tree", {})
        for fid in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            assert fid in out


class TestPositionIsAdvertised:
    def test_the_tool_declares_both_anchors(self) -> None:
        move = next(t for t in _list_tools() if t["name"] == "chat_folder_move")
        props = move["inputSchema"]["properties"]
        assert "before" in props and "after" in props
        assert move["inputSchema"]["required"] == ["folder"]

    def test_an_anchor_is_accepted_by_the_schema(self) -> None:
        """The schema rejects unknown fields, so an undeclared anchor would 400
        before the handler ever ran."""
        with (
            patch("kiro_crew.mcp_dashboard._get", side_effect=_ordered_rows),
            patch(
                "kiro_crew.mcp_dashboard._patch",
                return_value={"id": "dddddddddddd", "name": "Delta", "parent_id": ""},
            ),
        ):
            out = _call_tool_inner("chat_folder_move", {"folder": "Delta", "after": "Alpha"})
        assert "unknown field" not in out

    def test_an_unknown_field_is_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _call_tool_inner("chat_folder_move", {"folder": "Delta", "position": "first"})
