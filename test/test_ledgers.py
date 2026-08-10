"""Unit tests for :mod:`kiro_crew.ledgers` (LedgerStore, #2641)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.ledgers import (
    LedgerConflictError,
    LedgerNotFoundError,
    LedgerStore,
    checklist_progress,
)


@pytest.fixture
def store(tmp_path: Path) -> LedgerStore:
    return LedgerStore(root=tmp_path / "ledgers")


class TestCreateGetList:
    def test_create_defaults(self, store: LedgerStore) -> None:
        meta = store.create("")
        assert meta["title"] == "Untitled ledger"
        assert meta["version"] == 1
        assert meta["content"] == ""
        got = store.get(meta["id"])
        assert got["content"] == ""
        assert got["title"] == "Untitled ledger"

    def test_title_truncated(self, store: LedgerStore) -> None:
        meta = store.create("x" * 600)
        assert len(meta["title"]) == 500

    def test_list_orders_by_updated_and_reports_progress(self, store: LedgerStore) -> None:
        a = store.create("a")
        b = store.create("b")
        store.update(b["id"], content="- [ ] one\n- [x] two\n", base_version=1)
        ledgers = store.list()
        assert [l["id"] for l in ledgers][0] == b["id"]  # most recently updated first
        assert ledgers[0]["progress"] == {"done": 1, "total": 2}
        assert ledgers[1]["progress"] == {"done": 0, "total": 0}
        assert a["id"] == ledgers[1]["id"]

    def test_get_unknown_raises(self, store: LedgerStore) -> None:
        with pytest.raises(LedgerNotFoundError):
            store.get("0123456789ab")

    def test_bad_id_grammar_rejected(self, store: LedgerStore) -> None:
        # Path-traversal-shaped ids must never touch the filesystem.
        for bad in ("../../etc/passwd", "..%2f..", "abc", "A" * 12, ""):
            with pytest.raises(LedgerNotFoundError):
                store.get(bad)


class TestUpdateCas:
    def test_content_write_bumps_version(self, store: LedgerStore) -> None:
        lid = store.create("t")["id"]
        meta = store.update(lid, content="hello", base_version=1)
        assert meta["version"] == 2
        assert store.get(lid)["content"] == "hello"

    def test_stale_base_version_conflicts_with_current_state(self, store: LedgerStore) -> None:
        lid = store.create("t")["id"]
        store.update(lid, content="first", base_version=1)  # v2
        with pytest.raises(LedgerConflictError) as exc:
            store.update(lid, content="second", base_version=1)
        assert exc.value.current["version"] == 2
        assert exc.value.current["content"] == "first"
        # Losing write must not have landed.
        assert store.get(lid)["content"] == "first"

    def test_missing_base_version_conflicts(self, store: LedgerStore) -> None:
        lid = store.create("t")["id"]
        with pytest.raises(LedgerConflictError):
            store.update(lid, content="x", base_version=None)

    def test_rename_only_does_not_bump_version(self, store: LedgerStore) -> None:
        lid = store.create("t")["id"]
        meta = store.update(lid, title="renamed")
        assert meta["title"] == "renamed"
        assert meta["version"] == 1

    def test_content_capped(self, store: LedgerStore) -> None:
        lid = store.create("t")["id"]
        store.update(lid, content="y" * 60_000, base_version=1)
        assert len(store.get(lid)["content"]) == 50_000


class TestToggle:
    def _seed(self, store: LedgerStore) -> str:
        lid = store.create("t")["id"]
        store.update(lid, content="# h\n- [ ] alpha\n- [x] beta\nplain\n", base_version=1)
        return lid

    def test_flip_on_and_off(self, store: LedgerStore) -> None:
        lid = self._seed(store)
        r = store.toggle(lid, 1, "- [ ] alpha")
        assert "- [x] alpha" in r["content"]
        r2 = store.toggle(lid, 2, "- [x] beta")
        assert "- [ ] beta" in r2["content"]
        assert r2["version"] == r["version"] + 1

    def test_expected_mismatch_conflicts(self, store: LedgerStore) -> None:
        lid = self._seed(store)
        with pytest.raises(LedgerConflictError) as exc:
            store.toggle(lid, 1, "- [ ] STALE TEXT")
        assert "alpha" in exc.value.current["content"]

    def test_non_checkbox_line_conflicts(self, store: LedgerStore) -> None:
        lid = self._seed(store)
        with pytest.raises(LedgerConflictError):
            store.toggle(lid, 3, "plain")

    def test_out_of_range_conflicts(self, store: LedgerStore) -> None:
        lid = self._seed(store)
        with pytest.raises(LedgerConflictError):
            store.toggle(lid, 99, "- [ ] alpha")

    def test_different_lines_toggle_independently(self, store: LedgerStore) -> None:
        # The parallel-sessions hot path: toggles of different items never
        # conflict, regardless of interleaving.
        lid = self._seed(store)
        store.toggle(lid, 1, "- [ ] alpha")
        store.toggle(lid, 2, "- [x] beta")  # original line text still matches
        content = store.get(lid)["content"]
        assert "- [x] alpha" in content and "- [ ] beta" in content

    def test_trailing_newline_preserved(self, store: LedgerStore) -> None:
        lid = self._seed(store)
        r = store.toggle(lid, 1, "- [ ] alpha")
        assert r["content"].endswith("\n")


class TestDelete:
    def test_delete_removes_meta_and_content(self, store: LedgerStore) -> None:
        lid = store.create("t")["id"]
        store.delete(lid)
        assert store.list() == []
        with pytest.raises(LedgerNotFoundError):
            store.get(lid)

    def test_delete_unknown_raises(self, store: LedgerStore) -> None:
        with pytest.raises(LedgerNotFoundError):
            store.delete("0123456789ab")


class TestChecklistProgress:
    def test_variants(self) -> None:
        content = "- [ ] a\n* [x] b\n  - [X] c\n-[ ] not item\ntext\n"
        assert checklist_progress(content) == {"done": 2, "total": 3}
