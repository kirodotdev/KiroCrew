"""``ArtifactStore.migrate_kinds``: the corrective ``widget`` -> ``markdown`` reclassification.

Pinned through the store's interface (``kiro_crew.artifacts_scans`` holds the body): a dry
run reports every candidate and writes nothing; ``apply=True`` rewrites the record's
``kind`` and its ``widget`` ``version_kinds`` entries, and a second run finds nothing;
genuine widgets, other kinds, unreadable records and records another writer reclassified
first are left alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.artifacts import ArtifactError, ArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


def _meta(store: ArtifactStore, slug: str) -> dict[str, Any]:
    return json.loads((store.root / slug / "meta.json").read_text(encoding="utf-8"))


def test_a_dry_run_reports_each_candidate_and_writes_nothing(store: ArtifactStore) -> None:
    store.create(name="Plain notes", content="just words, no markup", kind="widget")
    store.create(name="Genuine widget", content="<div>a widget</div>", kind="widget")
    store.create(name="A doc", content="# already markdown", kind="markdown")
    before = {slug: _meta(store, slug) for slug in ("plain-notes", "genuine-widget", "a-doc")}

    report = store.migrate_kinds()

    assert report == [
        {
            "slug": "plain-notes",
            "name": "Plain notes",
            "from_kind": "widget",
            "to_kind": "markdown",
            "reason": "no HTML tags",
        }
    ]
    assert {slug: _meta(store, slug) for slug in before} == before


def test_apply_flips_the_kind_and_every_widget_version_and_a_second_run_finds_nothing(
    store: ArtifactStore,
) -> None:
    store.create(name="Plain notes", content="first words", kind="widget")
    store.update("plain-notes", content="second words", snapshot=True)
    store.create(name="More notes", content="other words", kind="widget")
    assert set(_meta(store, "plain-notes")["version_kinds"].values()) == {"widget"}

    report = store.migrate_kinds(apply=True)

    assert sorted(entry["slug"] for entry in report) == ["more-notes", "plain-notes"]
    for slug in ("more-notes", "plain-notes"):
        meta = _meta(store, slug)
        assert meta["kind"] == "markdown"
        assert set(meta["version_kinds"].values()) == {"markdown"}
        assert store.get(slug).kind == "markdown"
    assert len(_meta(store, "plain-notes")["version_kinds"]) == 2
    assert store.migrate_kinds(apply=True) == []


def test_an_unreadable_record_is_skipped_and_the_rest_still_migrate(
    store: ArtifactStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.create(name="Plain notes", content="words", kind="widget")
    broken = store.root / "broken"
    broken.mkdir()
    (broken / "meta.json").write_text("{not json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="kiro_crew.artifacts"):
        report = store.migrate_kinds(apply=True)

    assert [entry["slug"] for entry in report] == ["plain-notes"]
    assert _meta(store, "plain-notes")["kind"] == "markdown"
    skipped = [r.getMessage() for r in caplog.records if "skipping unreadable" in r.getMessage()]
    assert len(skipped) == 1
    assert skipped[0].startswith(f"migrate_kinds: skipping unreadable {broken / 'meta.json'}: ")


def test_a_record_whose_content_cannot_be_read_is_left_alone(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create(name="Plain notes", content="words", kind="widget")
    store.create(name="Hidden notes", content="more words", kind="widget")
    real_get = store.get

    def get(slug: str, **kwargs: Any) -> Any:
        if slug == "hidden-notes":
            raise ArtifactError("content refused")
        return real_get(slug, **kwargs)

    monkeypatch.setattr(store, "get", get)

    report = store.migrate_kinds(apply=True)

    assert [entry["slug"] for entry in report] == ["plain-notes"]
    assert _meta(store, "hidden-notes")["kind"] == "widget"


def test_a_record_another_writer_reclassified_first_is_not_rewritten(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate is reported, but the rewrite rereads the record under the lock and
    leaves a record whose ``kind`` is not ``widget`` by then exactly as the other writer
    left it."""
    store.create(name="Plain notes", content="words", kind="widget")
    real_get = store.get

    def get(slug: str, **kwargs: Any) -> Any:
        got = real_get(slug, **kwargs)
        store.update(slug, kind="json")
        return got

    monkeypatch.setattr(store, "get", get)

    report = store.migrate_kinds(apply=True)

    assert [entry["slug"] for entry in report] == ["plain-notes"]
    assert _meta(store, "plain-notes")["kind"] == "json"
