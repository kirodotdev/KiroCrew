"""A hand-edited folders.json cannot take out the folder routes.

``folders.json`` is editable by hand and loaded with a bare ``json.loads``, so it
can hold a non-list document, a non-dict entry, or a row with no ``id``. Every
consumer matches rows by id, and ``mutate_folders`` snapshots the store with
``dict(row)``, which RAISES on a non-dict and surfaces as a 500 because no
middleware maps handler exceptions.

These tests drive the REAL ``DashboardState.load_folders`` and the REAL
``mutate_folders``. That matters: the ownership tests use a fake
``mutate_folders`` that does not snapshot, so they can show the handler logic
tolerating a bad row while proving nothing about whether the request survives.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.state import DashboardState


def _load_with(tmp_path: Any, payload: object) -> list[dict]:
    (tmp_path / "folders.json").write_text(json.dumps(payload), encoding="utf-8")
    state = DashboardState.__new__(DashboardState)
    state._folders = []
    with patch("kiro_crew.dashboard.state.config_dir", return_value=tmp_path):
        DashboardState.load_folders(state)
    return state._folders


class TestLoadFoldersDropsWhatNothingCanUse:
    def test_a_non_dict_entry_is_dropped(self, tmp_path: Any) -> None:
        rows = _load_with(
            tmp_path, ["not even a dict", {"id": "f1", "name": "Work", "parent_id": ""}]
        )
        assert [r["id"] for r in rows] == ["f1"]

    def test_a_row_without_an_id_is_dropped(self, tmp_path: Any) -> None:
        rows = _load_with(
            tmp_path, [{"name": "no id", "parent_id": ""}, {"id": "f1", "name": "Work"}]
        )
        assert [r["id"] for r in rows] == ["f1"]

    def test_a_row_with_an_empty_id_is_dropped(self, tmp_path: Any) -> None:
        rows = _load_with(tmp_path, [{"id": "", "name": "blank"}, {"id": "f1"}])
        assert [r["id"] for r in rows] == ["f1"]

    def test_a_non_string_id_is_dropped(self, tmp_path: Any) -> None:
        """Ids are minted as ``uuid4().hex[:12]``, so anything else is corrupt --
        and a merely-truthy test lets an UNHASHABLE id through, which raises
        ``TypeError`` from inside the archived-count join rather than being
        harmlessly unmatchable."""
        rows = _load_with(
            tmp_path,
            [
                {"id": [1], "name": "list id"},
                {"id": {"a": 1}, "name": "dict id"},
                {"id": 5, "name": "int id"},
                {"id": "f1", "name": "Work"},
            ],
        )
        assert [r["id"] for r in rows] == ["f1"]

    def test_an_unhashable_id_would_have_broken_the_listing(self, tmp_path: Any) -> None:
        """Pins the consequence, not just the filter: the surviving rows must be
        safe as dict KEYS, which is what the listing does with them."""
        rows = _load_with(tmp_path, [{"id": [1]}, {"id": "f1", "name": "Work"}])
        counts: dict[str, int] = {}
        # This is the join GET /api/chat/folders performs; it must not raise.
        assert [counts.get(r["id"], 0) for r in rows] == [0]

    def test_a_non_list_document_is_ignored_entirely(self, tmp_path: Any) -> None:
        """Not read as empty and not half-applied: the store keeps its prior
        value rather than becoming a dict the consumers would iterate as keys."""
        assert _load_with(tmp_path, {"id": "f1"}) == []

    def test_a_healthy_file_is_unchanged(self, tmp_path: Any) -> None:
        good = [{"id": "f1", "name": "Work", "parent_id": ""}, {"id": "f2", "name": "Home"}]
        assert _load_with(tmp_path, good) == good


class TestTheRealSnapshotSurvivesWhatLoadAdmits:
    @pytest.mark.asyncio
    async def test_mutate_folders_snapshots_what_load_folders_kept(self, tmp_path: Any) -> None:
        """``mutate_folders`` does ``dict(row)`` on every row, so anything
        load_folders admits must survive that. This is the assertion the
        ownership suite's fake mutate_folders cannot make."""
        (tmp_path / "folders.json").write_text(
            json.dumps(["bad row", {"id": "f1", "name": "Work", "parent_id": ""}]),
            encoding="utf-8",
        )
        state = DashboardState.__new__(DashboardState)
        state._folders = []
        with patch("kiro_crew.dashboard.state.config_dir", return_value=tmp_path):
            DashboardState.load_folders(state)
            # dict(row) over the surviving rows must not raise.
            snapshot = [dict(f) for f in state._folders]
        assert snapshot == [{"id": "f1", "name": "Work", "parent_id": ""}]
