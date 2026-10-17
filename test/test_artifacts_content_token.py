"""The artifact store's optimistic-concurrency content token.

A token is 256 random bits as hex, every store-backed write returns the next one,
and a write that names a stale token is a conflict that changes nothing. The shared
``store`` fixture lives in ``artifacts_helpers.py``.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest
from artifacts_helpers import (  # noqa: F401 -- the fixture is requested by name
    store_fixture,
)

from kiro_crew.artifacts import (
    Artifact,
    ArtifactConflictError,
    ArtifactStore,
    ArtifactValidationError,
    _new_content_token,
)


@pytest.fixture
def sequential_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand out distinct content tokens in order, so no comparison rests on chance."""
    counter = itertools.count(1)
    monkeypatch.setattr("kiro_crew.artifacts._new_content_token", lambda: f"{next(counter):064x}")


# ── optimistic-concurrency content token ────────────────────────────────────


def test_a_new_content_token_is_256_random_bits_as_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", _new_content_token())


@pytest.mark.usefixtures("sequential_tokens")
class TestConflictToken:
    def test_create_assigns_a_token_for_store_backed_content(self, store: ArtifactStore) -> None:
        art = store.create(name="x", content="v1")
        assert art.content_token and len(art.content_token) == 64
        assert store.get("x").content_token == art.content_token

    def test_matching_token_writes_and_returns_the_next_token(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        token = store.get("x").content_token
        art = store.update("x", content="v2", expected_token=token)
        assert store.get("x").content == "v2"
        assert art.content_token == store.get("x").content_token != token

    def test_stale_token_raises_and_writes_nothing(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        stale = store.get("x").content_token
        store.update("x", content="from another window")
        with pytest.raises(ArtifactConflictError) as info:
            store.update("x", content="stale edit", expected_token=stale, snapshot=True)
        assert info.value.current_token == store.get("x").content_token
        assert store.get("x").content == "from another window"
        assert store.get("x").version == 1

    def test_a_silent_save_trips_the_guard_though_the_version_did_not_move(
        self, store: ArtifactStore
    ) -> None:
        store.create(name="x", content="v1")
        stale = store.get("x").content_token
        store.update("x", content="silent save")  # snapshot=False: version stays 1
        assert store.get("x").version == 1
        with pytest.raises(ArtifactConflictError):
            store.update("x", content="stale", expected_token=stale)

    def test_rewriting_identical_content_still_moves_the_token(self, store: ArtifactStore) -> None:
        # Every content write gets a fresh token, even when the bytes match.
        store.create(name="x", content="same")
        first = store.get("x").content_token
        store.update("x", content="same")
        assert store.get("x").content_token != first

    def test_omitted_token_keeps_last_write_wins(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        store.update("x", content="v2")
        store.update("x", content="v3")
        assert store.get("x").content == "v3"

    def test_metadata_only_update_ignores_the_token(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        stale = store.get("x").content_token
        store.update("x", content="v2")
        art = store.update("x", name="Renamed", expected_token=stale)
        assert art.name == "Renamed"
        # A rename is not a content write, so it does not move the token.
        token = store.get("x").content_token
        store.update("x", description="d")
        assert store.get("x").content_token == token

    def test_snapshot_of_unchanged_content_keeps_the_token(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        token = store.get("x").content_token
        store.update("x", snapshot=True)
        assert store.get("x").content_token == token
        store.update("x", content="v2", expected_token=token)

    @pytest.mark.parametrize("bad", ["abc", "G" * 64, "A" * 64, 123])
    def test_malformed_token_is_a_validation_error(self, store: ArtifactStore, bad) -> None:
        store.create(name="x", content="v1")
        with pytest.raises(ArtifactValidationError):
            store.update("x", content="v2", expected_token=bad)
        assert store.get("x").content == "v1"

    def test_live_file_backed_artifact_mints_no_token_and_ignores_one(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "live.md"
        src.write_text("v1", encoding="utf-8")
        created = store.create(name="live", content="v1", source_path=str(src), kind="markdown")
        assert created.content_token is None
        assert store.get("live").content_token is None
        updated = store.update("live", content="v2", expected_token="0" * 64)
        assert updated.content_token is None
        assert src.read_text(encoding="utf-8") == "v2"

    def test_versioned_read_carries_no_token(self, store: ArtifactStore) -> None:
        art = store.create(name="x", content="v1")
        store.update(art.slug, content="v2", snapshot=True)
        assert store.get(art.slug, version=1).content_token is None

    def test_token_is_shared_by_every_instance_on_a_root(self, tmp_path: Path) -> None:
        a = ArtifactStore(root=tmp_path / "artifacts")
        b = ArtifactStore(root=tmp_path / "artifacts")
        a.create(name="x", content="v1")
        b.update("x", content="v2", expected_token=a.get("x").content_token)
        assert a.get("x").content == "v2"

    def test_token_is_persisted_and_served_with_content_only(self, store: ArtifactStore) -> None:
        art = store.create(name="x", content="v1")
        meta = json.loads((store.root / "x" / "meta.json").read_text(encoding="utf-8"))
        assert meta["content_token"] == art.content_token
        wire = store.get("x").to_dict(include_content=True)
        assert wire["content_token"] == meta["content_token"]
        assert "content_token" not in store.get("x").to_dict()

    @pytest.mark.parametrize("bad", ["not-hex", "é" * 64, "A" * 64, 7])
    def test_corrupt_persisted_token_is_replaced_not_fatal(self, store: ArtifactStore, bad) -> None:
        store.create(name="x", content="v1")
        path = store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["content_token"] = bad
        path.write_text(json.dumps(meta), encoding="utf-8")
        art = store.get("x", assign_token=True)
        persisted = json.loads(path.read_text(encoding="utf-8"))["content_token"]
        assert art.content == "v1" and art.content_token == persisted
        assert len(persisted) == 64
        assert [a.slug for a in store.list()] == ["x"]
        store.update("x", content="v2", expected_token=art.content_token)

    def test_an_assigning_read_gives_an_untokened_artifact_a_guarded_token(
        self, store: ArtifactStore
    ) -> None:
        store.create(name="x", content="v1")
        path = store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["content_token"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        token = store.get("x", assign_token=True).content_token
        assert token
        assert json.loads(path.read_text(encoding="utf-8"))["content_token"] == token
        assert store.get("x").content_token == token
        store.update("x", content="v2", expected_token=token)
        with pytest.raises(ArtifactConflictError):
            store.update("x", content="v3", expected_token=token)

    def test_a_plain_read_of_an_untokened_artifact_writes_nothing(
        self, store: ArtifactStore
    ) -> None:
        store.create(name="x", content="v1")
        path = store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["content_token"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        before = path.read_bytes()
        art = store.get("x")
        assert art.content == "v1"
        assert art.content_token is None
        assert path.read_bytes() == before

    @staticmethod
    def _untokened_with_refused_meta_writes(
        store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        store.create(name="x", content="v1")  # events already backfilled
        path = store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["content_token"]
        path.write_text(json.dumps(meta), encoding="utf-8")

        def refuse(art: Artifact) -> None:
            raise PermissionError("read-only data directory")

        monkeypatch.setattr(store, "_write_meta", refuse)
        return path

    def test_untokened_read_survives_a_failed_meta_write_without_a_token(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._untokened_with_refused_meta_writes(store, monkeypatch)
        art = store.get("x", assign_token=True)
        assert art.content == "v1"
        assert art.content_token is None
        assert "content_token" not in json.loads(path.read_text(encoding="utf-8"))
        # A guarded content write on the same unwritable directory still fails.
        with pytest.raises(OSError):
            store.update("x", content="v2", expected_token="0" * 64)

    def test_read_after_a_failed_token_write_gets_a_working_token(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with monkeypatch.context() as mp:
            path = self._untokened_with_refused_meta_writes(store, mp)
            store.get("x", assign_token=True)
        token = store.get("x", assign_token=True).content_token
        assert token
        assert json.loads(path.read_text(encoding="utf-8"))["content_token"] == token
        store.update("x", content="v2", expected_token=token)
        assert store.get("x").content == "v2"

    @staticmethod
    def _fail_meta_writes_once_content_is(
        store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, slug: str, content: str
    ) -> None:
        """Make every meta.json write fail once current.html holds *content*."""
        current = store.root / slug / "current.html"
        write_meta = store._write_meta

        def failing(art: Artifact) -> None:
            if current.read_text(encoding="utf-8") == content:
                raise OSError("disk full")
            write_meta(art)

        monkeypatch.setattr(store, "_write_meta", failing)

    def test_a_save_that_fails_after_its_content_write_still_refuses_a_stale_token(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.create(name="x", content="v1")
        stale = store.get("x").content_token
        with monkeypatch.context() as mp:
            self._fail_meta_writes_once_content_is(store, mp, "x", "v2")
            with pytest.raises(OSError):
                store.update("x", content="v2", expected_token=stale)
        assert store.get("x").content == "v2"
        with pytest.raises(ArtifactConflictError):
            store.update("x", content="v3", expected_token=stale)
        assert store.get("x").content == "v2"

    def test_a_settled_draft_that_fails_after_its_content_write_still_refuses_a_stale_token(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.create(name="Untitled", content="", slug="u")
        stale = store.get("u").content_token
        with monkeypatch.context() as mp:
            self._fail_meta_writes_once_content_is(store, mp, "u", "# notes")
            with pytest.raises(OSError):
                store.settle_blank("u", untitled_name="Untitled", draft="# notes")
        assert store.get("u").content == "# notes"
        with pytest.raises(ArtifactConflictError):
            store.update("u", content="replaced", expected_token=stale)
        assert store.get("u").content == "# notes"

    @pytest.mark.parametrize("snapshot", [True, False])
    def test_metadata_only_update_of_an_untokened_artifact(
        self, store: ArtifactStore, snapshot: bool
    ) -> None:
        store.create(name="x", content="v1")
        path = store.root / "x" / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        del meta["content_token"]
        path.write_text(json.dumps(meta), encoding="utf-8")
        art = store.update("x", name="renamed", tags=["t"], snapshot=snapshot)
        assert art.name == "renamed"
        assert "content_token" not in json.loads(path.read_text(encoding="utf-8"))
        assert store.get("x", assign_token=True).content_token

    def test_token_survives_a_new_store_instance(self, tmp_path: Path) -> None:
        # No process-held key: a gateway restart must not 409 every open editor.
        a = ArtifactStore(root=tmp_path / "artifacts")
        a.create(name="x", content="v1")
        token = a.get("x").content_token
        b = ArtifactStore(root=tmp_path / "artifacts")
        assert b.get("x").content_token == token
