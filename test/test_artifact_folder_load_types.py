"""A wrong-type field in ``artifact_folders.json`` must not take down the folder list.

The file is loaded verbatim apart from the id check, so a hand edit can leave an
``order`` that ``int()`` cannot read, or an ``id`` that is a list or an object.
Either one must read as a harmless default, never raise out of
:meth:`ArtifactFolderStore.list` for every reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.artifacts import ArtifactFolderStore


def _store(tmp_path: Path, rows: list[dict[str, Any]]) -> ArtifactFolderStore:
    path = tmp_path / "artifact_folders.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return ArtifactFolderStore(path=path)


@pytest.mark.parametrize(
    "bad", [None, "first", [1], {"n": 1}], ids=["null", "word", "list", "object"]
)
def test_an_unreadable_order_sorts_as_zero(tmp_path: Path, bad: Any) -> None:
    store = _store(
        tmp_path,
        [
            {"id": "b", "name": "B", "order": 1},
            {"id": "a", "name": "A", "order": bad},
        ],
    )
    assert [f["id"] for f in store.list()] == ["a", "b"]
    # The stored value is left as it was: reading never rewrites the file.
    assert store.get("a")["order"] == bad


def test_an_infinite_order_sorts_as_zero(tmp_path: Path) -> None:
    path = tmp_path / "artifact_folders.json"
    path.write_text(
        '[{"id": "b", "name": "B", "order": 1}, {"id": "a", "name": "A", "order": Infinity}]',
        encoding="utf-8",
    )
    assert [f["id"] for f in ArtifactFolderStore(path=path).list()] == ["a", "b"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3", ["b", "a"]), (2.5, ["b", "a"]), (True, ["a", "b"])],
    ids=["numeric-string", "float", "bool"],
)
def test_an_order_int_reads_keeps_its_position(
    tmp_path: Path, value: Any, expected: list[str]
) -> None:
    store = _store(
        tmp_path,
        [
            {"id": "a", "name": "A", "order": value},
            {"id": "b", "name": "B", "order": 1},
        ],
    )
    assert [f["id"] for f in store.list()] == expected


@pytest.mark.parametrize("bad_id", [[1], {"n": 1}], ids=["list", "object"])
def test_a_list_or_object_id_is_dropped(tmp_path: Path, bad_id: Any) -> None:
    store = _store(tmp_path, [{"id": bad_id, "name": "X"}, {"id": "ok", "name": "Keep"}])
    assert [f["id"] for f in store.list()] == ["ok"]
    assert store.resolve_path("Keep") == "ok"


def test_a_number_id_still_loads(tmp_path: Path) -> None:
    store = _store(tmp_path, [{"id": 5, "name": "Five"}, {"id": "ok", "name": "Keep"}])
    assert [f["id"] for f in store.list()] == [5, "ok"]


def _strict_json(raw: bytes) -> Any:
    """Parse as the browser's ``Response.json()`` does: ``Infinity`` / ``NaN`` are errors."""

    def _refuse(token: str) -> Any:
        raise ValueError(f"not JSON: {token}")

    return json.loads(raw, parse_constant=_refuse)


@pytest.mark.asyncio
async def test_the_folder_list_route_answers_valid_json_over_a_non_finite_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from kiro_crew import artifacts as art_mod
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.dashboard.handlers.artifacts import api_artifact_folders

    path = tmp_path / "artifact_folders.json"
    raw = (
        '[{"id": "a", "name": "A", "order": Infinity, "color": NaN, "extra": [-Infinity]},'
        ' {"id": "b", "name": "B", "order": 1}]'
    )
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(art_mod, "_default_store", ArtifactStore(root=tmp_path / "artifacts"))
    monkeypatch.setattr(art_mod, "_default_folder_store", ArtifactFolderStore(path=path))
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = {}
    req.query = {}
    req.app = {"state": MagicMock()}

    resp = await api_artifact_folders(req)

    assert resp.status == 200
    by_id = {f["id"]: f for f in _strict_json(resp.body)["folders"]}
    assert by_id["a"]["order"] == 0
    assert by_id["a"]["color"] is None
    assert by_id["a"]["extra"] == [None]
    assert by_id["b"]["order"] == 1
    # Serializing never rewrites the stored file.
    assert path.read_text(encoding="utf-8") == raw
