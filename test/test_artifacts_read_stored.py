"""``artifacts.read_stored_artifact``: ``ArtifactStore.get``'s current read, writing nothing.

For a record that is no live pointer and already has its event log it answers what
``get`` answers, field for field, and it raises what ``get`` raises for the same
record. Unlike ``get`` it creates no store directory and does not backfill a record
written before the event log existed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew.artifacts import (
    STORED_READ_FAILURES,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
    read_stored_artifact,
)


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def _edit_meta(store: ArtifactStore, slug: str, change: dict[str, Any], drop: tuple = ()) -> Path:
    path = store.root / slug / "meta.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(change)
    for key in drop:
        raw.pop(key, None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_it_answers_what_get_answers_for_a_current_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>", tags=["ops"])
    store.update(art.slug, content="<p>two</p>", snapshot=True)

    assert asdict(read_stored_artifact(store.root, art.slug)) == asdict(store.get(art.slug))


def test_it_writes_nothing_where_get_backfills_a_legacy_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    _edit_meta(store, art.slug, {}, drop=("events", "events_backfilled"))
    before = _tree(store.root)

    read = read_stored_artifact(store.root, art.slug)

    assert _tree(store.root) == before
    assert read.content == "<p>one</p>" and read.events == []
    store.get(art.slug)
    assert _tree(store.root) != before  # the control: get does write it


def test_it_creates_no_store_directory(tmp_path: Path) -> None:
    root = tmp_path / "absent" / "artifacts"

    with pytest.raises(ArtifactNotFoundError):
        read_stored_artifact(root, "board")

    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("version", [None, [], {}, "x", 1e400])
def test_a_meta_version_get_cannot_read_raises_the_same_error(tmp_path: Path, version) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    _edit_meta(store, art.slug, {"version": version})

    with pytest.raises(STORED_READ_FAILURES) as from_get:
        store.get(art.slug)
    with pytest.raises(STORED_READ_FAILURES) as from_read:
        read_stored_artifact(store.root, art.slug)

    assert type(from_read.value) is type(from_get.value)


def test_a_meta_nested_past_the_decoder_raises_the_same_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    (store.root / art.slug / "meta.json").write_text("[" * 50000, encoding="utf-8")

    with pytest.raises(RecursionError):
        store.get(art.slug)
    with pytest.raises(RecursionError):
        read_stored_artifact(store.root, art.slug)


@pytest.mark.parametrize("slug", ["missing", "../escape", "Not A Slug"])
def test_a_slug_get_refuses_is_refused_the_same_way(tmp_path: Path, slug: str) -> None:
    store = _store(tmp_path)

    with pytest.raises((ArtifactNotFoundError, ArtifactValidationError)) as from_get:
        store.get(slug)
    with pytest.raises((ArtifactNotFoundError, ArtifactValidationError)) as from_read:
        read_stored_artifact(store.root, slug)

    assert type(from_read.value) is type(from_get.value)


def _outcome(read: Any) -> tuple[str, str]:
    try:
        return "content", read().content
    except STORED_READ_FAILURES as exc:
        return "raised", type(exc).__name__


@requires_symlinks
@pytest.mark.parametrize("leaf", ["meta.json", "current.html"])
def test_a_linked_store_file_is_answered_as_get_answers_it(tmp_path: Path, leaf: str) -> None:
    # The same path resolution, fence and descriptor-pinned open as get's: no read
    # reaches a file get would not.
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    target = store.root / art.slug / leaf
    moved = tmp_path / f"moved-{leaf}"
    target.rename(moved)
    os.symlink(moved, target)

    assert _outcome(lambda: read_stored_artifact(store.root, art.slug)) == _outcome(
        lambda: store.get(art.slug)
    )


def test_a_live_pointer_is_read_from_the_stores_own_copy(tmp_path: Path) -> None:
    # get serves a live pointer from its source_path; this never opens that file.
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>stored</p>")
    elsewhere = tmp_path / "elsewhere.html"
    elsewhere.write_text("<p>elsewhere</p>", encoding="utf-8")
    _edit_meta(store, art.slug, {"source_path": str(elsewhere), "source_copy_only": False})

    read = read_stored_artifact(store.root, art.slug)

    assert ArtifactStore._is_live_pointer(read)
    assert read.content == "<p>stored</p>"
