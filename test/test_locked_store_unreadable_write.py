"""A locked read-modify-write never saves an empty store over a present, unreadable file.

``deploy/pending.py`` (``add_pending`` / ``remove_pending``) and ``feature_videos``
(``record_status``) load, mutate and save under a lock. Their tolerant loaders answer
"empty" for a present file they cannot read, so a write that starts from that answer
kept only the new entry and the old bytes were gone. A UTF-8 byte-order mark reads
normally. A file that does not decode or parse is moved to ``<name>.corrupt-<utc>``
before the save. A failed read, or a failed move, makes the write raise with the file
left as it is.
Readers keep degrading.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from kiro_crew import feature_videos as fv
from kiro_crew.deploy import pending

BOM = b"\xef\xbb\xbf"
_UNREADABLE = [
    b'[{"id": "caf\xe9"}]',  # latin-1: not UTF-8
    b'[{"id": "p1"},',  # truncated
    b"[" * 100_000 + b"]" * 100_000,  # nested past the recursion limit
]


@pytest.fixture()
def store(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "deploy" / "pending-deploys.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(pending, "_store_path", lambda: path)
    return path


@pytest.fixture()
def state(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "feature_videos_state.json"
    monkeypatch.setattr(fv, "_state_path", lambda: path)
    return path


def _asides(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def _fail_reads_of(module, path: Path, monkeypatch) -> None:
    """A Windows share-mode-0 handle held past the retry budget fails every read."""
    real = module.read_bytes_with_retry

    def held(p, *args, **kwargs):
        if Path(p) == path:
            raise PermissionError(13, "being used by another process")
        return real(p, *args, **kwargs)

    monkeypatch.setattr(module, "read_bytes_with_retry", held)


def _fail_moves_of(module, path: Path, monkeypatch) -> None:
    real = module.replace_with_retry

    def refuse(src, dst):
        if Path(src) == path and ".corrupt-" in str(dst):
            raise PermissionError(13, "being used by another process")
        return real(src, dst)

    monkeypatch.setattr(module, "replace_with_retry", refuse)


def _pending_ids(path: Path) -> list[str]:
    return [e["id"] for e in json.loads(path.read_text(encoding="utf-8"))]


def _seed_pending(store: Path) -> bytes:
    pending.add_pending({"id": "p1"})
    pending.add_pending({"id": "p2"})
    return store.read_bytes()


class TestPendingStore:
    def test_a_bom_store_keeps_its_entries_through_add_and_remove(self, store: Path) -> None:
        store.write_bytes(BOM + _seed_pending(store))
        pending.add_pending({"id": "p3"})
        assert _pending_ids(store) == ["p1", "p2", "p3"]
        store.write_bytes(BOM + store.read_bytes())
        assert pending.remove_pending("p2") is True
        assert _pending_ids(store) == ["p1", "p3"]
        assert _asides(store) == []

    def test_readers_see_a_bom_stores_entries(self, store: Path) -> None:
        store.write_bytes(BOM + _seed_pending(store))
        assert [e["id"] for e in pending.list_pending()] == ["p1", "p2"]

    @pytest.mark.parametrize(
        "content",
        [*_UNREADABLE, b'{"id": "p1"}'],
        ids=["undecodable", "truncated", "deep-nesting", "dict-root"],
    )
    def test_an_unreadable_store_is_moved_aside_before_add(
        self, store: Path, content: bytes, caplog
    ) -> None:
        store.write_bytes(content)
        with caplog.at_level(logging.WARNING, logger=pending.__name__):
            pending.add_pending({"id": "p3"})
        assert _pending_ids(store) == ["p3"]
        [aside] = _asides(store)
        assert aside.read_bytes() == content
        assert aside.name in caplog.text

    def test_an_unreadable_store_is_moved_aside_before_remove(self, store: Path) -> None:
        store.write_bytes(_UNREADABLE[1])
        assert pending.remove_pending("p1") is False
        [aside] = _asides(store)
        assert aside.read_bytes() == _UNREADABLE[1]

    def test_a_failed_read_refuses_the_write_and_keeps_the_store(
        self, store: Path, monkeypatch
    ) -> None:
        original = _seed_pending(store)
        _fail_reads_of(pending, store, monkeypatch)
        with pytest.raises(pending.PendingStoreUnreadable, match="pending-deploys.json"):
            pending.add_pending({"id": "p3"})
        with pytest.raises(pending.PendingStoreUnreadable):
            pending.remove_pending("p1")
        assert store.read_bytes() == original
        assert _asides(store) == []

    def test_a_store_that_cannot_be_moved_aside_refuses_the_write(
        self, store: Path, monkeypatch
    ) -> None:
        store.write_bytes(_UNREADABLE[1])
        _fail_moves_of(pending, store, monkeypatch)
        with pytest.raises(pending.PendingStoreUnreadable, match="pending-deploys.json"):
            pending.add_pending({"id": "p3"})
        with pytest.raises(pending.PendingStoreUnreadable):
            pending.remove_pending("p1")
        assert store.read_bytes() == _UNREADABLE[1]
        assert _asides(store) == []

    def test_a_missing_store_still_starts_empty(self, store: Path) -> None:
        pending.add_pending({"id": "p1"})
        assert _pending_ids(store) == ["p1"]
        assert _asides(store) == []

    @pytest.mark.parametrize(
        "content", _UNREADABLE, ids=["undecodable", "truncated", "deep-nesting"]
    )
    def test_readers_and_claims_still_degrade_on_a_malformed_store(
        self, store: Path, content: bytes
    ) -> None:
        store.write_bytes(content)
        assert pending.list_pending() == []
        assert pending.claim_pending("p1") is None
        assert store.read_bytes() == content


def _video_ids(path: Path) -> list[str]:
    return sorted(json.loads(path.read_text(encoding="utf-8"))["videos"])


def _seed_videos(state: Path) -> bytes:
    fv.record_status("vid-a", "seen")
    fv.record_status("vid-b", "dismissed")
    return state.read_bytes()


class TestFeatureVideoState:
    def test_a_bom_state_keeps_its_rows_through_a_write(self, state: Path) -> None:
        state.write_bytes(BOM + _seed_videos(state))
        fv.record_status("vid-c", "seen")
        assert _video_ids(state) == ["vid-a", "vid-b", "vid-c"]
        assert _asides(state) == []

    def test_readers_see_a_bom_states_rows(self, state: Path) -> None:
        state.write_bytes(BOM + _seed_videos(state))
        assert fv.load_state().status_of("vid-b") == "dismissed"

    @pytest.mark.parametrize(
        "content",
        [*_UNREADABLE, b"[]", b'{"videos": 3}'],
        ids=["undecodable", "truncated", "deep-nesting", "list-root", "videos-not-object"],
    )
    def test_an_unreadable_state_is_moved_aside_before_the_write(
        self, state: Path, content: bytes, caplog
    ) -> None:
        state.write_bytes(content)
        with caplog.at_level(logging.WARNING, logger=fv.__name__):
            fv.record_status("vid-c", "seen")
        assert _video_ids(state) == ["vid-c"]
        [aside] = _asides(state)
        assert aside.read_bytes() == content
        assert aside.name in caplog.text

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_the_set_aside_state_stays_owner_only(self, state: Path) -> None:
        state.write_bytes(_UNREADABLE[1])
        state.chmod(0o644)
        fv.record_status("vid-c", "seen")
        [aside] = _asides(state)
        assert stat.S_IMODE(aside.stat().st_mode) == 0o600

    def test_a_failed_read_refuses_the_write_and_keeps_the_state(
        self, state: Path, monkeypatch
    ) -> None:
        original = _seed_videos(state)
        _fail_reads_of(fv, state, monkeypatch)
        with pytest.raises(fv.FeatureVideoStateUnreadable, match="feature_videos_state.json"):
            fv.record_status("vid-c", "seen")
        assert state.read_bytes() == original
        assert _asides(state) == []

    def test_a_state_that_cannot_be_moved_aside_refuses_the_write(
        self, state: Path, monkeypatch
    ) -> None:
        state.write_bytes(_UNREADABLE[1])
        _fail_moves_of(fv, state, monkeypatch)
        with pytest.raises(fv.FeatureVideoStateUnreadable, match="feature_videos_state.json"):
            fv.record_status("vid-c", "seen")
        assert state.read_bytes() == _UNREADABLE[1]
        assert _asides(state) == []

    def test_a_missing_state_still_starts_empty(self, state: Path) -> None:
        fv.record_status("vid-a", "seen")
        assert _video_ids(state) == ["vid-a"]
        assert _asides(state) == []

    def test_readers_still_degrade_on_a_malformed_state(self, state: Path) -> None:
        state.write_bytes(b'{"videos": ')
        assert fv.load_state().videos == {}
        assert _asides(state) == []
