"""An ``Infinity`` number in one ``meta.json`` must not take down the artifact list.

``json.loads`` reads ``Infinity`` as a float, and ``int()`` on it raises
``OverflowError``, which the per-field fallbacks and the per-record skip in
``ArtifactStore.list`` both have to treat like any other unreadable number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew.artifacts import ArtifactStore


def _two_artifacts(tmp_path: Path) -> tuple[ArtifactStore, str, str]:
    store = ArtifactStore(root=tmp_path / "artifacts")
    bad = store.create(name="Bad", content="x").slug
    good = store.create(name="Good", content="y").slug
    return store, bad, good


def _edit_meta(store: ArtifactStore, slug: str, edit: Callable[[dict[str, Any]], None]) -> None:
    path = store.root / slug / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    edit(meta)
    path.write_text(json.dumps(meta).replace('"INF"', "Infinity"), encoding="utf-8")


@pytest.mark.parametrize(
    ("block", "value"),
    [
        ("publication", {"artifact_id": "p1", "version_map": {"1": "INF", "2": 3}}),
        ("publication", {"artifact_id": "p1", "last_synced_kirocrew_version": "INF"}),
        ("publication", {"artifact_id": "p1", "wrapper_revision": "INF"}),
        ("fork_metadata", {"upstream_artifact_id": "u1", "upstream_version": "INF"}),
    ],
    ids=["version_map", "last_synced", "wrapper_revision", "upstream_version"],
)
def test_an_infinite_sub_field_falls_back_and_the_artifact_still_loads(
    tmp_path: Path, block: str, value: dict[str, Any]
) -> None:
    store, bad, good = _two_artifacts(tmp_path)
    _edit_meta(store, bad, lambda m: m.__setitem__(block, value))

    assert sorted(a.slug for a in store.list()) == sorted([bad, good])
    art = store.get(bad)
    if block == "publication":
        assert art.publication is not None
        assert art.publication.last_synced_kirocrew_version == 0
        assert art.publication.wrapper_revision == 0
        assert art.publication.version_map == ({"2": 3} if "version_map" in value else {})
    else:
        assert art.fork_metadata is not None
        assert art.fork_metadata.upstream_version == 0


def test_an_infinite_version_skips_only_that_record(tmp_path: Path) -> None:
    store, bad, good = _two_artifacts(tmp_path)
    _edit_meta(store, bad, lambda m: m.__setitem__("version", "INF"))

    assert [a.slug for a in store.list()] == [good]
    with pytest.raises(ValueError):
        store.get(bad)


def test_a_word_version_is_still_skipped_the_same_way(tmp_path: Path) -> None:
    store, bad, good = _two_artifacts(tmp_path)
    _edit_meta(store, bad, lambda m: m.__setitem__("version", "abc"))

    assert [a.slug for a in store.list()] == [good]
