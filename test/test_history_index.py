"""Tests for the session search candidate index (``kiro_crew.history_index``).

The index's contract is narrow and the tests below are written against it
directly: it must return a SUPERSET of the true content hits, and it must refuse
to vouch for a row whose file has moved on. Both halves matter — a subset loses
results silently, and a stale row serves deleted text.
"""

from __future__ import annotations

import os
import threading

import pytest

from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history_index import SessionSearchIndex, cjk_inventory
from kiro_crew.history_search import parse_search_query

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


# CJK fixtures are written as escapes because this repository forbids literal
# Chinese characters in source. They are the point of these tests rather than
# incidental data: ``parse_search_query`` turns a multi-character CJK run into one
# required needle PER CHARACTER, which is the case the trigram tokenizer cannot
# answer and the distinct-character column exists for.
CJK_LEAK_QUERY = "\u5185\u5b58\u6cc4\u6f0f"  # "memory leak", 4 characters
CJK_LEAK_SENTENCE = "\u5185\u5b58\u6cc4\u6f0f\u5bfc\u81f4\u5360\u7528\u4e00\u76f4\u6da8"
CJK_USAGE_NORMAL = "\u5185\u5b58\u5360\u7528\u6b63\u5e38"  # shares only the first two
CJK_LEAK_SCATTERED = "\u5185 ... \u5b58 ... \u6cc4 ... \u6f0f"
CJK_CAT = "\u732b"
CJK_CAT_SENTENCE = "\u732b\u5728\u684c\u5b50\u4e0a"
CJK_DOG_SENTENCE = "\u72d7\u5728\u9662\u5b50\u91cc"


def _index(tmp_path) -> SessionSearchIndex:
    return SessionSearchIndex(tmp_path / "session_index.db")


def _sync(index: SessionSearchIndex, tmp_path, key: str, text: str) -> os.stat_result:
    """Write *text* to a file for *key* and index it under that file's stat."""
    path = tmp_path / f"{key}.jsonl"
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    index.sync(
        key,
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        dev=st.st_dev,
        ino=st.st_ino,
        texts=[text],
    )
    return st


def _candidates(index: SessionSearchIndex, query: str) -> set[str] | None:
    """Intersect the per-needle content sets, as the search path does.

    The search path additionally unions in title matches and unvouched keys;
    these tests have neither, so the intersection alone is what the index
    contributes.
    """
    needles, _phrase, _floor = parse_search_query(query)
    _fresh, per_needle = index.shortlist({}, needles)
    if per_needle is None:
        return None
    out: set[str] | None = None
    for keys in per_needle.values():
        out = keys if out is None else (out & keys)
    return out if out else set()


def test_substring_needle_narrows_to_the_matching_session(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "we discussed ack contention at length")
    _sync(index, tmp_path, "b", "unrelated notes about deployment")
    assert _candidates(index, "contention") == {"a"}


def test_substring_not_prefix(tmp_path):
    """``cont`` must find ``contention`` — the scan path matches substrings."""
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "ack contention")
    assert _candidates(index, "cont") == {"a"}


def test_multi_word_query_intersects_needles(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "both", "contention appears here and hypotheses too")
    _sync(index, tmp_path, "one", "contention appears here alone")
    assert _candidates(index, "contention hypotheses") == {"both"}


def test_short_needle_does_not_filter(tmp_path):
    """A 2-character term has no trigram entry, so it must not exclude anything.

    Returning ``None`` (or a superset) is required; returning an empty set would
    silently lose every hit for a short query.
    """
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "memory leak")
    assert _candidates(index, "me") is None


def test_short_needle_alongside_long_one_still_supersets(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "memory leak in the runtime")
    _sync(index, tmp_path, "b", "runtime only")
    got = _candidates(index, "me runtime")
    assert got is not None and "a" in got


def test_cjk_query_gates_on_every_character(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "hit", CJK_LEAK_SENTENCE)
    _sync(index, tmp_path, "partial", CJK_USAGE_NORMAL)
    # The 3rd and 4th characters of the query are absent from "partial", so it
    # cannot satisfy the per-character gate.
    assert _candidates(index, CJK_LEAK_QUERY) == {"hit"}


def test_cjk_scatter_is_a_candidate_but_the_caller_still_gates(tmp_path):
    """Character presence is all the index can prove; adjacency stays the scorer's job.

    The index must NOT try to enforce the adjacency floor: it would have to drop
    rows the gate might still accept, and a candidate filter may only over-return.
    """
    index = _index(tmp_path)
    _sync(index, tmp_path, "scatter", CJK_LEAK_SCATTERED)
    assert _candidates(index, CJK_LEAK_QUERY) == {"scatter"}


def test_single_cjk_character_query(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", CJK_CAT_SENTENCE)
    _sync(index, tmp_path, "b", CJK_DOG_SENTENCE)
    assert _candidates(index, CJK_CAT) == {"a"}


def test_forge_reference_matches_any_spelling(tmp_path):
    """A forge reference must find the session that wrote a different spelling."""
    index = _index(tmp_path)
    _sync(index, tmp_path, "url", "see https://github.com/o/r/pull/4411 for the fix")
    _sync(index, tmp_path, "other", "nothing relevant here")
    got = _candidates(index, "#4411")
    assert got is not None and "url" in got


def test_no_usable_needle_returns_none(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "anything")
    assert _candidates(index, "ab") is None


def test_fresh_keys_returns_the_rowid_for_an_unchanged_file(tmp_path):
    index = _index(tmp_path)
    st = _sync(index, tmp_path, "a", "hello world")
    fresh = index.fresh_keys({"a": st})
    assert set(fresh) == {"a"}
    assert index.document(fresh["a"], "a") == (len("hello world"), "hello world")


def test_stale_row_is_not_vouched_for(tmp_path):
    """A rewrite that PRESERVES mtime must still un-vouch the row.

    This is the case a timestamp alone cannot see, and the reason ``size`` is
    part of the freshness key.
    """
    index = _index(tmp_path)
    path = tmp_path / "a.jsonl"
    st = _sync(index, tmp_path, "a", "the original text")
    path.write_text("replaced with something longer", encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert after.st_mtime_ns == st.st_mtime_ns
    assert index.fresh_keys({"a": after}) == {}


def test_document_returns_the_indexed_text(tmp_path):
    index = _index(tmp_path)
    st = _sync(index, tmp_path, "a", "Ack Contention")
    rowid = index.fresh_keys({"a": st})["a"]
    doc_chars, folded = index.document(rowid, "a")
    assert folded == "ack contention"
    assert doc_chars == len("Ack Contention")


def test_raw_texts_returns_the_original_unfolded_text(tmp_path):
    index = _index(tmp_path)
    st = _sync(index, tmp_path, "a", "Ack Contention")
    assert index.raw_texts("a", st) == ["Ack Contention"]


def test_raw_texts_refuses_a_stale_row(tmp_path):
    """A snippet must come only from the revision the file currently holds."""
    index = _index(tmp_path)
    path = tmp_path / "a.jsonl"
    st = _sync(index, tmp_path, "a", "the original text")
    path.write_text("replaced with something longer", encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert index.raw_texts("a", path.stat()) is None


def test_raw_texts_is_none_for_an_unknown_key(tmp_path):
    index = _index(tmp_path)
    st = _sync(index, tmp_path, "a", "text")
    assert index.raw_texts("nope", st) is None


def test_shortlist_answers_both_questions_from_one_snapshot(tmp_path):
    """Freshness and candidates must describe the same index state.

    Read separately, a row dropped between them leaves its session vouched for
    AND out of every candidate set, which loses a hit the scan path returns.
    """
    index = _index(tmp_path)
    st_a = _sync(index, tmp_path, "a", "ack contention here")
    st_b = _sync(index, tmp_path, "b", "unrelated notes")
    needles, _phrase, _floor = parse_search_query("contention")
    fresh, per_needle = index.shortlist({"a": st_a, "b": st_b}, needles)
    assert set(fresh) == {"a", "b"}
    assert per_needle is not None
    assert list(per_needle.values()) == [{"a"}]


def test_shortlist_reports_no_candidates_when_nothing_is_lookupable(tmp_path):
    index = _index(tmp_path)
    st = _sync(index, tmp_path, "a", "memory leak")
    fresh, per_needle = index.shortlist({"a": st}, parse_search_query("me")[0])
    assert set(fresh) == {"a"}
    assert per_needle is None


def test_shortlist_degrades_to_a_full_scan_when_unavailable(tmp_path):
    index = _index(tmp_path)
    index.available = False
    assert index.shortlist({}, parse_search_query("contention")[0]) == ({}, None)


def test_drop_reports_success_and_failure(tmp_path):
    """A deletion funnel needs to know; a swallowed failure ships the copy."""
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "contention")
    assert index.drop(["a"]) is True
    assert index.drop(["never-indexed"]) is True
    assert index.drop([]) is True
    index.available = False
    assert index.drop(["a"]) is False


def test_drop_zeroes_the_removed_text_rather_than_unlinking_it(tmp_path):
    """The deleted session's bytes must be gone from the files, not just unreferenced."""
    index = _index(tmp_path)
    marker = "zsecretzcontentionzmarkerz"
    _sync(index, tmp_path, "a", f"a session about {marker} and nothing else")
    db = tmp_path / "session_index.db"
    assert index.drop(["a"]) is True
    found = []
    for path in (db, db.with_name(db.name + "-wal")):
        if path.exists() and marker.encode() in path.read_bytes():
            found.append(path.name)
    assert not found, f"deleted text still readable in {found}"


def test_concurrent_writers_do_not_collide(tmp_path):
    """A drop must not fail because another thread is mid-sync.

    On one shared connection the second ``BEGIN IMMEDIATE`` raises "cannot start a
    transaction within a transaction" — a nesting error no busy timeout can help
    with — and the loser's rollback can tear the winner's write. With a connection
    per thread they contend normally instead.
    """
    index = _index(tmp_path)
    for i in range(12):
        _sync(index, tmp_path, f"k{i}", f"session {i} about contention and other things")

    errors: list[BaseException] = []
    drops: list[bool] = []

    def syncer() -> None:
        try:
            for i in range(12):
                st = (tmp_path / f"k{i}.jsonl").stat()
                index.sync(
                    f"k{i}",
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                    dev=st.st_dev,
                    ino=st.st_ino,
                    texts=[f"session {i} rewritten about contention"],
                )
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
            errors.append(exc)

    def dropper() -> None:
        try:
            for i in range(12):
                drops.append(index.drop([f"k{i}"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=syncer), threading.Thread(target=dropper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert all(drops), "a drop reported failure while another thread was writing"


def test_a_busy_checkpoint_is_reported_as_failure(tmp_path, monkeypatch):
    """A WAL truncation that did not happen must not be reported as erasure.

    ``PRAGMA wal_checkpoint(TRUNCATE)`` returns ``(busy, ...)`` instead of raising,
    so ignoring the row claims the deleted bytes are gone when they are not.
    """
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "contention")
    monkeypatch.setattr(index, "_truncate_wal", lambda conn: False)
    assert index.drop(["a"]) is False


def test_a_clear_checkpoint_is_reported_as_success(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "contention")
    assert index.drop(["a"]) is True


def test_store_exists_distinguishes_absent_from_unopenable(tmp_path):
    index = _index(tmp_path)
    assert index.store_exists() is True
    missing = SessionSearchIndex(tmp_path / "gone" / "nope.db")
    missing.close()
    (tmp_path / "gone" / "nope.db").unlink()
    assert missing.store_exists() is False


def test_a_reused_rowid_does_not_hand_over_another_sessions_text(tmp_path):
    """A rowid is not an identity: SQLite reuses freed ones, and the backfill frees them.

    Without the key in the lookup, a caller holding a rowid from an earlier snapshot
    gets whichever session now owns that number, and scores its own session against
    that text -- the equivalence guarantee, broken silently.
    """
    index = _index(tmp_path)
    st_old = _sync(index, tmp_path, "old", "the old session mentions contention")
    rowid = index.fresh_keys({"old": st_old})["old"]

    # Free the rowid, then let the next insert take it for a different session.
    assert index.drop(["old"]) is True
    _sync(index, tmp_path, "new", "the new session mentions deployment")
    st_new = (tmp_path / "new.jsonl").stat()
    assert index.fresh_keys({"new": st_new})["new"] == rowid, "rowid was not reused"

    # The stale rowid must not resolve to the new session's text.
    assert index.document(rowid, "old") is None
    assert index.document(rowid, "new") is not None


def test_a_replaced_inode_is_not_vouched_for(tmp_path):
    """Identity is part of the stamp, so an atomic rewrite cannot be trusted.

    ``atomic_write`` replaces the file through ``os.replace``, which gives a new
    inode. Size and mtime can both be preserved across such a rewrite, so they are
    not enough on their own; the inode changes every time.
    """
    index = _index(tmp_path)
    path = tmp_path / "a.jsonl"
    st = _sync(index, tmp_path, "a", "the original text")
    replacement = tmp_path / "a.new"
    replacement.write_text("the original text", encoding="utf-8")  # same length
    os.replace(replacement, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert after.st_mtime_ns == st.st_mtime_ns and after.st_size == st.st_size
    assert after.st_ino != st.st_ino, "the rewrite did not change the inode"

    assert index.fresh_keys({"a": after}) == {}
    assert index.raw_texts("a", after) is None


def test_optimize_reclaims_the_pages_a_delete_left(tmp_path):
    """A dropped row's trigram postings must not stay readable in the file.

    ``secure_delete`` zeroes the content pages, but the tombstoned postings sit in
    segment pages that a plain delete does not free -- so 3-character fragments
    survive until the maintenance pass merges and rewrites the index.
    """
    index = _index(tmp_path)
    marker = "zqxjvbnm"
    for i in range(20):
        _sync(index, tmp_path, f"noise{i}", f"noise session {i} about deployment " * 20)
    _sync(index, tmp_path, "doomed", f"a session that mentions {marker} once")
    db = tmp_path / "session_index.db"

    assert index.drop(["doomed"]) is True

    def fragments() -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = db.with_name(db.name + suffix)
            if path.exists():
                total += path.read_bytes().count(marker[:3].encode())
        return total

    assert fragments() > 0, "nothing to reclaim, so this test proves nothing"
    index.optimize()
    assert fragments() == 0, "the maintenance pass left deleted fragments readable"


def test_a_store_failure_is_reported_once(tmp_path, monkeypatch, caplog):
    """A broken index must not present only as slow search."""
    index = _index(tmp_path)
    monkeypatch.setattr(type(index), "_sync_failure_warned", False)
    monkeypatch.setattr(index, "_ensure_open", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level("WARNING"):
        index.sync("a", mtime_ns=1, size=1, dev=1, ino=1, texts=["x"])
        index.sync("b", mtime_ns=1, size=1, dev=1, ino=1, texts=["x"])
    warnings = [r for r in caplog.records if "could not store" in r.getMessage()]
    assert len(warnings) == 1


def test_resync_replaces_rather_than_duplicates(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "first revision mentions contention")
    _sync(index, tmp_path, "a", "second revision mentions deployment")
    assert _candidates(index, "contention") == set()
    assert _candidates(index, "deployment") == {"a"}


def test_drop_removes_the_row(tmp_path):
    index = _index(tmp_path)
    _sync(index, tmp_path, "a", "contention")
    index.drop(["a"])
    assert index.indexed_keys() == set()
    assert _candidates(index, "contention") == set()


def test_drop_is_safe_for_unknown_key(tmp_path):
    index = _index(tmp_path)
    index.drop(["never-indexed"])
    assert index.indexed_keys() == set()


def test_cjk_inventory_is_distinct_characters_only(tmp_path):
    doubled = CJK_LEAK_QUERY[:2] + CJK_LEAK_QUERY + " abc 123"
    assert cjk_inventory(doubled) == " ".join(CJK_LEAK_QUERY)


def test_optimize_is_safe_on_an_empty_index(tmp_path):
    index = _index(tmp_path)
    index.optimize()
    assert index.available


def test_unavailable_index_answers_none_not_empty(tmp_path):
    """A broken index must fall back to the scan path, not to zero results."""
    index = _index(tmp_path)
    index.available = False
    assert _candidates(index, "contention") is None
    assert index.fresh_keys({}) == {}
    assert index.document(1, "a") is None
