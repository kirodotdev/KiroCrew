"""A folder edit whose save fails leaves the in-memory tree as it was.

Otherwise every later read reports a tree that a restart would not load.
"""

import pytest

from kiro_crew.artifact_store.folders import ArtifactFolderStore
from kiro_crew.artifacts import ArtifactStore


def _failing_save(self):
    raise OSError("disk full")


def _tree(store):
    return [dict(f) for f in store.list()]


@pytest.fixture
def folders(tmp_path):
    store = ArtifactFolderStore(tmp_path / "folders.json")
    keep = store.create("Keep")["id"]
    other = store.create("Other")["id"]
    return store, keep, other


def _run(op, store, keep, other, tmp_path):
    if op == "create":
        store.create("Ghost")
    elif op == "rename":
        store.rename(keep, "Renamed")
    elif op == "reparent":
        store.reparent(keep, other)
    elif op == "set_icon":
        store.set_icon(keep, "x")
    elif op == "set_color":
        store.set_color(keep, "#ff0000")
    elif op == "reorder":
        store.reorder([{"id": keep, "order": 7}])
    elif op == "generated_icon":
        store.set_icon_if_epoch(keep, "y", expected_epoch=0)
    elif op == "resolve_path":
        store.resolve_path("Made/On/Demand", create_missing=True)
    else:
        store.delete(keep, delete_contents=False, artifact_store=ArtifactStore(tmp_path / "a"))


@pytest.mark.parametrize(
    "op",
    [
        "create",
        "rename",
        "reparent",
        "set_icon",
        "set_color",
        "reorder",
        "generated_icon",
        "resolve_path",
        "delete",
    ],
)
def test_failed_save_leaves_in_memory_tree_matching_disk(tmp_path, monkeypatch, folders, op):
    store, keep, other = folders
    path = tmp_path / "folders.json"
    on_disk_before = path.read_text()
    before = _tree(store)
    with monkeypatch.context() as patched:
        patched.setattr(ArtifactFolderStore, "_save", _failing_save)
        with pytest.raises(OSError):
            _run(op, store, keep, other, tmp_path)
    assert path.read_text() == on_disk_before
    assert _tree(store) == before
    assert _tree(store) == _tree(ArtifactFolderStore(path))


def test_reorder_with_a_bad_value_changes_nothing(folders):
    store, keep, other = folders
    before = _tree(store)
    with pytest.raises(ValueError):
        store.reorder([{"id": keep, "order": 5}, {"id": other, "order": "x"}])
    assert _tree(store) == before
