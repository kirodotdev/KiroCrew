"""Behaviour tests for index-backed :meth:`ConversationLog.search_sessions`.

The index is a candidate filter and a text source, never a ranker, so the
property under test is EQUIVALENCE: for the same corpus and query, an indexed
search must return exactly what a scanning search returns — same rows, same
order, same snippets. A ranking change here would be invisible in production
until somebody noticed their search got worse.

The second property is that the index actually avoids work: a session the index
vouches for and rules out must never be read or folded. That is asserted as a
fold COUNT, not as elapsed time, matching the convention in
``test_search_sessions_cost``.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew import history
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog

pytestmark = pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")


# Written as escapes because this repository forbids literal Chinese characters in
# source. CJK is not incidental here: it is the script whose gate is made entirely
# of 1-character needles, so it exercises the distinct-character column rather than
# the trigram one.
CJK_LEAK_SENTENCE = "\u5185\u5b58\u6cc4\u6f0f\u5bfc\u81f4\u5360\u7528\u4e00\u76f4\u6da8"
CJK_RESTART_SENTENCE = "\u91cd\u542f\u4e4b\u540e\u5360\u7528\u56de\u843d"
CJK_SCATTERED = [
    "\u5185 alone",
    "\u5b58 also alone",
    "\u6cc4 and \u6f0f apart",
]
CJK_LEAK_QUERY = "\u5185\u5b58\u6cc4\u6f0f"  # "memory leak"
CJK_MEMORY_QUERY = "\u5185\u5b58"  # its first two characters
CJK_USAGE_QUERY = "\u5360\u7528"  # "usage", present in both CJK sessions
CJK_SINGLE_QUERY = "\u5185"  # one character, the shortest possible gate


CORPUS: dict[str, list[str]] = {
    "s-ack": [
        "we walked through ack contention and three hypotheses",
        "the second hypothesis held up under load",
    ],
    "s-deploy": ["deployment pipeline notes, nothing about contention here"],
    "s-cjk": [CJK_LEAK_SENTENCE, CJK_RESTART_SENTENCE],
    "s-cjk-scatter": CJK_SCATTERED,
    "s-forge": ["fixed in https://github.com/o/r/pull/4411 after review"],
    "s-number": ["run 4411 finished with no errors"],
}

QUERIES = [
    "contention",
    "cont",
    "contention hypotheses",
    CJK_LEAK_QUERY,
    CJK_MEMORY_QUERY,
    CJK_USAGE_QUERY,
    "#4411",
    "4411",
    "deployment",
    "nothing-matches-this",
    "me",
    CJK_SINGLE_QUERY,
]


def _seed(log: ConversationLog) -> None:
    for key, messages in CORPUS.items():
        for message in messages:
            log.append(key, "user", message)


def _fold_counter(monkeypatch) -> list[str]:
    """Record which sessions paid a real fold (a file read plus parse)."""
    seen: list[str] = []
    original = ConversationLog._build_folded

    def counted(self, key: str, mtime: float, gen: int):
        seen.append(key)
        return original(self, key, mtime, gen)

    monkeypatch.setattr(ConversationLog, "_build_folded", counted)
    return seen


def _strip(rows: list[dict]) -> list[tuple]:
    """Compare on the fields a caller can observe, in order."""
    return [(r["key"], r.get("title"), r.get("snippet", "")) for r in rows]


@pytest.mark.parametrize("query", QUERIES)
def test_indexed_search_matches_scanning_search(tmp_path, query):
    scan_dir = tmp_path / "scan"
    index_dir = tmp_path / "indexed"
    scan_log = ConversationLog(base_dir=scan_dir)
    index_log = ConversationLog(base_dir=index_dir)
    _seed(scan_log)
    _seed(index_log)

    # The scanning side must never consult an index.
    scan_log._catalog_projection.search_index.available = False
    scan_rows = scan_log.search_sessions(query)

    report = index_log._catalog_projection.backfill_index(budget_secs=30)
    assert report["remaining"] == 0
    indexed_rows = index_log.search_sessions(query)

    assert _strip(indexed_rows) == _strip(scan_rows)


def test_vouched_non_matching_sessions_are_never_read(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log._catalog_projection.backfill_index(budget_secs=30)

    folded = _fold_counter(monkeypatch)
    rows = log.search_sessions("contention")

    assert [r["key"] for r in rows] == ["s-ack", "s-deploy"] or {r["key"] for r in rows} == {
        "s-ack",
        "s-deploy",
    }
    # s-cjk / s-forge / s-number cannot contain "contention"; the index proved it,
    # so their files must not have been opened. Snippet building may still fold a
    # RETURNED row, so only non-returned sessions are asserted absent.
    returned = {r["key"] for r in rows}
    assert not (set(folded) - returned)


def test_a_match_only_in_a_later_message_is_still_found(tmp_path):
    """The indexed document joins messages with NUL, so later messages must be searchable.

    Verified against SQLite rather than assumed: the trigram tokenizer indexes past a
    NUL and the stored string round-trips at full length. This pins it, because every
    other test here would still pass if only the first message were indexed.
    """
    log = ConversationLog(base_dir=tmp_path)
    log.append("late", "user", "first message about deployment")
    log.append("late", "user", "second message about contention")
    log.append("late", "user", "third message about quarantine")
    log._catalog_projection.backfill_index(budget_secs=30)

    for term in ("deployment", "contention", "quarantine"):
        rows = log.search_sessions(term)
        assert "late" in {r["key"] for r in rows}, f"{term} was not found"


def test_an_append_between_the_stat_and_the_snapshot_is_not_vouched_for(tmp_path, monkeypatch):
    """A vouch must not rest on an observation older than the index read it certifies.

    The stats are taken before the snapshot, so without a re-stamp a row could be
    trusted while its file had already grown, and the session's superseded text would
    decide whether it matches.
    """
    log = ConversationLog(base_dir=tmp_path)
    log.append("grows", "user", "a session about deployment")
    projection = log._catalog_projection
    projection.backfill_index(budget_secs=30)

    real_shortlist = projection.search_index.shortlist

    def appending_shortlist(stats, needles):
        # Land the append in the window between the caller's stats and the snapshot.
        with open(log._path("grows"), "a", encoding="utf-8") as handle:
            handle.write('{"role": "user", "content": "now about contention", "ts": 0}\n')
        return real_shortlist(stats, needles)

    monkeypatch.setattr(projection.search_index, "shortlist", appending_shortlist)

    rows = log.search_sessions("contention")
    assert "grows" in {r["key"] for r in rows}, "the stale row was trusted"


def test_a_changed_file_falls_back_to_the_scan(tmp_path, monkeypatch):
    """An indexed session whose file moved on must still be searched correctly."""
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log._catalog_projection.backfill_index(budget_secs=30)

    # Append a term that exists in no indexed row, preserving nothing.
    log.append("s-deploy", "user", "now it mentions contention after all")
    rows = log.search_sessions("contention")
    assert "s-deploy" in {r["key"] for r in rows}


def test_mtime_preserving_rewrite_still_searches_the_new_text(tmp_path):
    """The case a timestamp alone cannot see: same mtime, different content."""
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log._catalog_projection.backfill_index(budget_secs=30)

    path = log._path("s-deploy")
    before = path.stat()
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"role": "user", "content": "smuggled contention line", "ts": 0}\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns

    rows = log.search_sessions("contention")
    assert "s-deploy" in {r["key"] for r in rows}


def test_title_only_match_survives_the_shortlist(tmp_path):
    """A needle satisfied by the TITLE must not be vetoed by a content-only index."""
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log.update_metadata("s-deploy", {"title": "quarantine notes"})
    log._catalog_projection.backfill_index(budget_secs=30)

    rows = log.search_sessions("quarantine")
    assert "s-deploy" in {r["key"] for r in rows}


def test_needles_split_across_title_and_content(tmp_path):
    """One needle in the title, another in the content, is still a match."""
    log = ConversationLog(base_dir=tmp_path)
    log.append("split", "user", "the body mentions contention only")
    log.update_metadata("split", {"title": "quarantine"})
    log._catalog_projection.backfill_index(budget_secs=30)

    rows = log.search_sessions("quarantine contention")
    assert "split" in {r["key"] for r in rows}


def test_unindexed_session_is_still_found(tmp_path):
    """A session added after the last backfill must not disappear from results."""
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log._catalog_projection.backfill_index(budget_secs=30)
    log.append("s-late", "user", "a late session about contention")

    rows = log.search_sessions("contention")
    assert "s-late" in {r["key"] for r in rows}


def test_backfill_drops_rows_that_left_the_window(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log._catalog_projection.backfill_index(budget_secs=30)
    indexed_before = log._catalog_projection.search_index.indexed_keys()
    assert len(indexed_before) == len(CORPUS)

    monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 2)
    report = log._catalog_projection.backfill_index(budget_secs=30)
    assert report["dropped"] == len(CORPUS) - 2
    assert len(log._catalog_projection.search_index.indexed_keys()) == 2


def test_delete_is_refused_when_the_index_row_cannot_be_removed(tmp_path, monkeypatch):
    """A transcript must not be destroyed while its indexed copy survives.

    The index row goes first precisely so a failure here has destroyed nothing.
    """
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    projection = log._catalog_projection
    projection.backfill_index(budget_secs=30)
    monkeypatch.setattr(projection.search_index, "drop", lambda keys: False)

    assert log.delete_session("s-ack") is False
    assert log._path("s-ack").exists()
    rows = log.search_sessions("contention")
    assert "s-ack" in {r["key"] for r in rows}


def test_deleting_by_a_logical_key_still_removes_the_indexed_copy(tmp_path):
    """A colon-form key names a row that does not exist under that spelling.

    Rows are written under ``list_sessions``' key, which is the file's stem, so a
    caller holding ``a:b`` would drop nothing and an empty match reads as success.
    """
    log = ConversationLog(base_dir=tmp_path)
    log.append("chan:room", "user", "a session about contention")
    projection = log._catalog_projection
    projection.backfill_index(budget_secs=30)
    stem = log._path("chan:room").stem
    assert stem in projection.search_index.indexed_keys()
    assert stem != "chan:room"

    assert log.delete_session("chan:room") is True

    assert projection.search_index.indexed_keys() == set()


def test_delete_fails_closed_when_an_index_exists_but_cannot_be_opened(tmp_path, monkeypatch):
    """An unopenable index may hold the text, and unprovable removal is not removal."""
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    projection = log._catalog_projection
    projection.backfill_index(budget_secs=30)
    monkeypatch.setattr(projection.search_index, "available", False)

    assert log.delete_session("s-ack") is False
    assert log._path("s-ack").exists()


def test_delete_still_works_when_no_index_exists_at_all(tmp_path, monkeypatch):
    """No store on disk means no copy to leak, so deletion must not depend on one.

    This is the case the fail-closed branch above must NOT catch: absent is safe,
    present-but-unopenable is not.
    """
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    projection = log._catalog_projection
    index = projection.search_index
    index.close()
    for suffix in ("", "-wal", "-shm"):
        candidate = index._db_path.with_name(index._db_path.name + suffix)
        if candidate.exists():
            candidate.unlink()
    monkeypatch.setattr(index, "available", False)
    assert index.store_exists() is False

    assert log.delete_session("s-ack") is True
    assert not log._path("s-ack").exists()


def test_indexing_a_session_deleted_mid_pass_does_not_resurrect_it(tmp_path):
    """The backfill must not write back the text of a session being deleted.

    ``index_session`` holds the session's own lock across stat, read and write, so
    a delete either completes first (the read then fails) or waits (the row it
    drops is the one just written). Asserted through the observable end state: no
    transcript, and no row carrying its text.
    """
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    projection = log._catalog_projection
    projection.backfill_index(budget_secs=30)

    assert log.delete_session("s-ack") is True
    # A pass that races in after the delete must not re-create the row.
    assert projection.index_session("s-ack") is False
    assert "s-ack" not in projection.search_index.indexed_keys()


def test_deleted_session_leaves_no_row(tmp_path):
    """Deleting a session must remove the index's copy of its text, not orphan it.

    Absence from results is not enough: an orphaned row keeps the deleted
    content readable on disk until some later backfill happens to evict it.
    """
    log = ConversationLog(base_dir=tmp_path)
    _seed(log)
    log._catalog_projection.backfill_index(budget_secs=30)
    assert "s-ack" in log._catalog_projection.search_index.indexed_keys()

    log.delete_session("s-ack")

    assert "s-ack" not in log._catalog_projection.search_index.indexed_keys()
    rows = log.search_sessions("contention")
    assert "s-ack" not in {r["key"] for r in rows}
