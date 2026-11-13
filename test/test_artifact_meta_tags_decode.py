"""A hand-edited ``tags`` in ``meta.json`` reads as a list of strings.

The store only writes a list of strings, but ``meta.json`` is a plain file. A tag
that is ``null`` or a number made the Library's content search (``' '.join`` over
the tags) raise, so the whole search answered 500; a bare-string ``tags`` split
into one tag per character; and a ``tags`` that is a number dropped the record
from every listing.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.handlers.artifacts_parts.library_reads import _scan_artifacts

_SLUG = "hand-edited-tags"


@pytest.fixture
def store(tmp_path):
    s = ArtifactStore(root=tmp_path / "artifacts")
    s.create(name="Hand edited", content="<p>quarterly numbers</p>", slug=_SLUG)
    return s


def _set_tags(store: ArtifactStore, tags: object) -> None:
    meta = store._artifact_dir(_SLUG) / "meta.json"
    raw = json.loads(meta.read_text(encoding="utf-8"))
    raw["tags"] = tags
    meta.write_text(json.dumps(raw), encoding="utf-8")


def test_a_content_search_over_a_null_or_numeric_tag_does_not_raise(store):
    _set_tags(store, [None, 7, "report"])
    items = store.list()
    hits = _scan_artifacts(store, items, "quarterly", want_snippet=False, do_content=True)
    assert [h["slug"] for h in hits] == [_SLUG]


@pytest.mark.parametrize(
    ("on_disk", "read_as"),
    [
        ([None, 7, 2.5, True, {"a": 1}, ["x"], "report"], ["7", "2.5", "report"]),
        ("report", ["report"]),
        ("", []),
        (5, []),
        ({"report": 1}, []),
        (None, []),
    ],
)
def test_tags_read_as_strings(store, on_disk, read_as):
    _set_tags(store, on_disk)
    [art] = store.list()
    assert art.tags == read_as


def test_well_formed_tags_are_unchanged(store):
    store.update(_SLUG, tags=["report", "q3"])
    assert store.get(_SLUG).tags == ["report", "q3"]
