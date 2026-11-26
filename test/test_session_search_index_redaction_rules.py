"""The session search index and chunk store name the redaction rules their rows
were written under, so a change of rules rebuilds them."""

from __future__ import annotations

import json

import pytest

from kiro_crew import history_search
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.dashboard import display_battery
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex
from kiro_crew.history_vectors_index import Tokenizer, chunker_value
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

KEY = "dashboard:plain"


def _write(log: ConversationLog) -> None:
    rows = [
        {"_type": "metadata", "created_at": "2026-11-20T10:00:00+00:00", "title": ""},
        {"role": "user", "content": "the plum report", "ts": "2026-11-20T10:00:01+00:00"},
    ]
    log._path(KEY).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _vouched(log: ConversationLog) -> bool:
    st = log._path(KEY).stat()
    return "dashboard_plain" in log._catalog_projection.search_index.fresh_keys(
        {"dashboard_plain": st}
    )


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
def test_a_change_of_redaction_rules_drops_the_index_and_the_backfill_rebuilds_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history_search, "_INDEX_QUIET_WINDOW_SECS", 0.0)
    log = ConversationLog(base_dir=tmp_path)
    _write(log)
    log._catalog_projection.backfill_index(budget_secs=FALLBACK_TIMEOUT_SECS)
    assert _vouched(log)
    log._catalog_projection.search_index.close()

    monkeypatch.setattr(display_battery, "LOADED_DIGEST", b"\x01" * 32)
    reopened = SessionSearchIndex(tmp_path / ".index" / INDEX_FILENAME)
    try:
        st = log._path(KEY).stat()
        assert reopened.fresh_keys({"dashboard_plain": st}) == {}
    finally:
        reopened.close()

    later = ConversationLog(base_dir=tmp_path)
    later._catalog_projection.backfill_index(budget_secs=FALLBACK_TIMEOUT_SECS)
    try:
        assert _vouched(later)
        assert [r["key"] for r in later.search_sessions("plum")] == ["dashboard_plain"]
    finally:
        later._catalog_projection.search_index.close()


def test_the_chunker_value_names_the_redaction_rules(monkeypatch):
    tokenizer = Tokenizer._make([None] * len(Tokenizer._fields))._replace(signature="tok")
    before = chunker_value(tokenizer)
    monkeypatch.setattr(display_battery, "LOADED_DIGEST", b"\x02" * 32)

    assert chunker_value(tokenizer) != before
