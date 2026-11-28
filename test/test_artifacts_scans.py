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

import kiro_crew.artifacts_scans
from kiro_crew.artifacts import ArtifactError, ArtifactStore
from kiro_crew.testing.links import make_dir_link


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


def _reads(store: ArtifactStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The file names the store's fenced reader opens from here on."""
    opened: list[str] = []
    real = store._read_text

    def read_text(path: Path) -> str:
        opened.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(store, "_read_text", read_text)
    return opened


def test_kind_of_reads_the_kind_from_meta_json_alone_and_writes_nothing(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create(name="Report", content="<html><body>hi</body></html>", kind="html")
    before = (store.root / "report" / "meta.json").read_bytes()
    opened = _reads(store, monkeypatch)
    monkeypatch.setattr(store, "_write_text", lambda *a, **k: pytest.fail("kind_of wrote"))

    assert store.kind_of("report") == "html"
    assert opened == ["meta.json"]
    assert (store.root / "report" / "meta.json").read_bytes() == before


def test_kind_of_answers_none_for_a_missing_or_unreadable_record(store: ArtifactStore) -> None:
    store.create(name="Broken", content="words", kind="markdown")
    (store.root / "broken" / "meta.json").write_text("{not json", encoding="utf-8")

    assert store.kind_of("missing") is None
    assert store.kind_of("broken") is None


@pytest.mark.parametrize(
    "slug",
    ["../x", "..", "/etc/passwd", "a/b", "a\\b", "a\x00b", "C:\\x", "x" * 81, "", "-a", "A"],
)
def test_kind_of_refuses_a_slug_outside_the_store_shape_before_any_read(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, slug: str
) -> None:
    store.create(name="X", content="words", kind="html")
    opened = _reads(store, monkeypatch)
    monkeypatch.setattr(store, "_artifact_dir", lambda s: pytest.fail(f"resolved a path for {s!r}"))
    # The plain character checks stand on their own, without the store's slug regex.
    monkeypatch.setattr("kiro_crew.artifacts._validate_slug", lambda s: s)
    assert kiro_crew.artifacts_scans._validate_slug("../x") == "../x"

    assert store.kind_of(slug) is None
    assert opened == []


@pytest.mark.parametrize("dir_guard", ["store", "kind_of"])
def test_kind_of_refuses_an_artifact_dir_that_resolves_outside_the_root(
    store: ArtifactStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_guard: str
) -> None:
    outside = tmp_path / "outside" / "evil"
    outside.mkdir(parents=True)
    store.create(name="Evil", content="<p>x</p>", kind="html")
    (outside / "meta.json").write_bytes((store.root / "evil" / "meta.json").read_bytes())
    store.delete("evil")
    make_dir_link(store.root / "evil", outside)
    if dir_guard == "kind_of":
        # kind_of's own root check, with the store's directory guard taken away.
        monkeypatch.setattr(store, "_artifact_dir", lambda s: store.root / s)
    opened = _reads(store, monkeypatch)

    assert store.kind_of("evil") is None
    assert opened == []
