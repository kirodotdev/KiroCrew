"""``artifacts.read_stored_record``: ``get``'s read of a record, writing nothing.

It returns ``meta.json`` as the raw document, not decoded, from one bounded read that
refuses a link at that name, with the stored ``current.html``. For a record that is no
live pointer and already has its event log, that document decoded with that content is
what ``get`` answers, field for field, and every failure of the read or of the decode
is one ``get`` raises too, inside ``STORED_READ_FAILURES``. Unlike ``get`` it creates
no store directory and does not backfill a record written before the event log existed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew.artifact_store import records
from kiro_crew.artifacts import (
    STORED_READ_FAILURES,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
    read_stored_record,
)

_CAP = 1 << 20


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


def _read(store: ArtifactStore, slug: str) -> Any:
    return read_stored_record(store.root, slug, max_meta_bytes=_CAP)


def _outcome(read: Any) -> tuple[str, str]:
    try:
        return "content", read().content
    except STORED_READ_FAILURES as exc:
        return "raised", type(exc).__name__


def test_the_decoded_document_is_what_get_answers_for_a_current_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>", tags=["ops"])
    store.update(art.slug, content="<p>two</p>", snapshot=True)

    stored = _read(store, art.slug)
    decoded = records.decode_meta(stored.document, stored.meta_path)
    decoded.content = stored.content

    assert asdict(decoded) == asdict(store.get(art.slug))


def test_it_returns_the_raw_document_and_writes_nothing_where_get_backfills(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    meta_path = _edit_meta(
        store, art.slug, {"source_copy_only": 0}, drop=("events", "events_backfilled")
    )
    before = _tree(store.root)

    stored = _read(store, art.slug)

    assert _tree(store.root) == before
    # Not decoded: decode_meta would read 0 as False.
    assert stored.document == json.loads(meta_path.read_text(encoding="utf-8"))
    assert stored.document["source_copy_only"] == 0
    assert stored.meta_path == meta_path.resolve()
    assert stored.content == "<p>one</p>"
    store.get(art.slug)
    assert _tree(store.root) != before  # the control: get does backfill it


def test_it_creates_no_store_directory(tmp_path: Path) -> None:
    root = tmp_path / "absent" / "artifacts"

    with pytest.raises(ArtifactNotFoundError):
        read_stored_record(root, "board", max_meta_bytes=_CAP)

    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("version", [None, [], {}, "x", 1e400])
def test_a_meta_version_get_cannot_read_fails_the_decode_the_same_way(
    tmp_path: Path, version
) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    _edit_meta(store, art.slug, {"version": version})

    stored = _read(store, art.slug)
    with pytest.raises(STORED_READ_FAILURES) as from_get:
        store.get(art.slug)
    with pytest.raises(STORED_READ_FAILURES) as from_decode:
        records.decode_meta(stored.document, stored.meta_path)

    assert type(from_decode.value) is type(from_get.value)


@pytest.mark.parametrize("slug", ["missing", "../escape", "Not A Slug"])
def test_a_slug_get_refuses_is_refused_the_same_way(tmp_path: Path, slug: str) -> None:
    store = _store(tmp_path)

    with pytest.raises((ArtifactNotFoundError, ArtifactValidationError)) as from_get:
        store.get(slug)
    with pytest.raises((ArtifactNotFoundError, ArtifactValidationError)) as from_read:
        _read(store, slug)

    assert type(from_read.value) is type(from_get.value)


def test_it_refuses_a_meta_over_its_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    size = (store.root / art.slug / "meta.json").stat().st_size

    assert read_stored_record(store.root, art.slug, max_meta_bytes=size).content == "<p>one</p>"
    with pytest.raises(ValueError):
        read_stored_record(store.root, art.slug, max_meta_bytes=size - 1)


@pytest.mark.parametrize(
    "data", [b"\xff{}", b"not json", b"[" * 50000], ids=["bad-utf8", "not-json", "nested"]
)
def test_every_read_failure_is_a_stored_read_failure(tmp_path: Path, data: bytes) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    (store.root / art.slug / "meta.json").write_bytes(data)

    with pytest.raises(STORED_READ_FAILURES):
        _read(store, art.slug)


@requires_symlinks
def test_it_refuses_a_linked_meta_that_get_follows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    target = store.root / art.slug / "meta.json"
    moved = tmp_path / "moved-meta.json"
    target.rename(moved)
    os.symlink(moved, target)

    assert _outcome(lambda: store.get(art.slug)) == ("content", "<p>one</p>")
    with pytest.raises(ArtifactError):
        _read(store, art.slug)


@requires_symlinks
def test_a_linked_current_html_is_answered_as_get_answers_it(tmp_path: Path) -> None:
    # The same path resolution, fence and descriptor-pinned open as get's: no read
    # reaches a file get would not.
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>one</p>")
    target = store.root / art.slug / "current.html"
    moved = tmp_path / "moved-current.html"
    target.rename(moved)
    os.symlink(moved, target)

    assert _outcome(lambda: _read(store, art.slug)) == _outcome(lambda: store.get(art.slug))


def test_a_live_pointer_is_read_from_the_stores_own_copy(tmp_path: Path) -> None:
    # get serves a live pointer from its source_path; this never opens that file.
    store = _store(tmp_path)
    art = store.create(name="Board", content="<p>stored</p>")
    elsewhere = tmp_path / "elsewhere.html"
    elsewhere.write_text("<p>elsewhere</p>", encoding="utf-8")
    _edit_meta(store, art.slug, {"source_path": str(elsewhere), "source_copy_only": False})

    stored = _read(store, art.slug)

    assert ArtifactStore._is_live_pointer(records.decode_meta(stored.document, stored.meta_path))
    assert stored.content == "<p>stored</p>"
