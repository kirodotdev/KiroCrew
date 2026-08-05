"""Tests for the artifact-folder MCP tool handlers.

Covers dispatch for ``artifact_folder_list/create/rename/move/delete``,
``artifact_move``, and the ``folder`` passthrough on ``artifact_save`` —
verifying schema validation, path→id resolution, HTTP call shape, and result
formatting. HTTP helpers are patched (the store layer is tested elsewhere).
"""

from __future__ import annotations

from unittest.mock import patch

from kiro_crew.mcp_core import _call_tool_inner

# A representative folder listing returned by GET /api/artifact-folders.
_FOLDERS = [
    {"id": "f_reports", "name": "Reports", "parent_id": "", "path": "Reports", "item_count": 2},
    {"id": "f_q3", "name": "Q3", "parent_id": "f_reports", "path": "Reports/Q3", "item_count": 1},
]


class TestFolderList:
    def test_list_formats_id_path_count(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": _FOLDERS}):
            out = _call_tool_inner("artifact_folder_list", {})
        assert "f_reports" in out and "Reports/Q3" in out
        assert "1 item" in out and "2 item" in out

    def test_list_empty(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": []}):
            out = _call_tool_inner("artifact_folder_list", {})
        assert "No artifact folders" in out


class TestFolderCreate:
    def test_create_forwards_parent(self) -> None:
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"id": "f_new", "name": "Q3", "path": "Reports/Q3"},
        ) as mock_post:
            out = _call_tool_inner(
                "artifact_folder_create", {"name": "Q3", "parent": "Reports"}
            )
        path, body = mock_post.call_args.args
        assert path == "/api/artifact-folders"
        assert body == {"name": "Q3", "parent": "Reports"}
        assert "Reports/Q3" in out and "f_new" in out


class TestFolderRename:
    def test_rename_resolves_path_then_patches(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": _FOLDERS}), patch(
            "kiro_crew.mcp_core._patch",
            return_value={"id": "f_q3", "name": "Q4", "path": "Reports/Q4"},
        ) as mock_patch:
            out = _call_tool_inner(
                "artifact_folder_rename", {"folder": "Reports/Q3", "name": "Q4"}
            )
        path, body = mock_patch.call_args.args
        assert path == "/api/artifact-folders/f_q3"
        assert body == {"name": "Q4"}
        assert "Q4" in out

    def test_rename_unknown_path_errors(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": _FOLDERS}):
            out = _call_tool_inner(
                "artifact_folder_rename", {"folder": "Nope/Missing", "name": "x"}
            )
        assert out.startswith("Error:")


class TestFolderMove:
    def test_move_resolves_both_refs(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": _FOLDERS}), patch(
            "kiro_crew.mcp_core._patch",
            return_value={"id": "f_q3", "path": "Q3", "parent_id": ""},
        ) as mock_patch:
            _call_tool_inner("artifact_folder_move", {"folder": "f_q3", "new_parent": "root"})
        path, body = mock_patch.call_args.args
        assert path == "/api/artifact-folders/f_q3"
        assert body == {"parent_id": ""}


class TestFolderDelete:
    def test_keep_default(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": _FOLDERS}), patch(
            "kiro_crew.mcp_core._delete",
            return_value={"ok": True, "reparented_artifact_slugs": ["a", "b"]},
        ) as mock_delete:
            out = _call_tool_inner("artifact_folder_delete", {"folder": "f_reports"})
        (path,) = mock_delete.call_args.args
        assert path == "/api/artifact-folders/f_reports"  # no ?delete_contents
        assert "kept 2 artifacts" in out

    def test_cascade_sets_query(self) -> None:
        with patch("kiro_crew.mcp_core._get", return_value={"folders": _FOLDERS}), patch(
            "kiro_crew.mcp_core._delete",
            return_value={
                "ok": True,
                "deleted_folder_ids": ["f_reports", "f_q3"],
                "deleted_artifact_slugs": ["a", "b", "c"],
            },
        ) as mock_delete:
            out = _call_tool_inner(
                "artifact_folder_delete",
                {"folder": "f_reports", "delete_contents": True},
            )
        (path,) = mock_delete.call_args.args
        assert path == "/api/artifact-folders/f_reports?delete_contents=true"
        assert "3 artifacts" in out


class TestArtifactMove:
    def test_move_patches_folder_route(self) -> None:
        with patch(
            "kiro_crew.mcp_core._patch",
            return_value={"slug": "doc", "folder_id": "f_reports"},
        ) as mock_patch:
            out = _call_tool_inner("artifact_move", {"slug": "doc", "folder": "Reports"})
        path, body = mock_patch.call_args.args
        assert path == "/api/artifacts/doc/folder"
        assert body == {"folder": "Reports"}
        assert "f_reports" in out

    def test_unfile_message(self) -> None:
        with patch(
            "kiro_crew.mcp_core._patch",
            return_value={"slug": "doc", "folder_id": ""},
        ):
            out = _call_tool_inner("artifact_move", {"slug": "doc", "folder": ""})
        assert "unfiled" in out.lower()


class TestSaveForwardsFolder:
    def test_save_includes_folder_in_body(self) -> None:
        # kind=markdown skips the widget dedup probe (which would call _get).
        with patch(
            "kiro_crew.mcp_core._post",
            return_value={"slug": "doc", "version": 1, "name": "Doc", "kind": "markdown"},
        ) as mock_post:
            _call_tool_inner(
                "artifact_save",
                {"name": "Doc", "content": "# hi", "kind": "markdown", "folder": "Reports/Q3"},
            )
        _path, body = mock_post.call_args.args
        assert body.get("folder") == "Reports/Q3"
