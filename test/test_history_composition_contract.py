"""Composition contracts for the public :mod:`kiro_crew.history` facade.

These tests pin seams that are easy to lose when ``history.py`` is split into
focused components: the import/call surface other modules use and the exact
append-only JSONL bytes existing transcripts are built from.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import executors, frontmatter, history, llm_helpers, skills, skills_script_validator


class _FrozenDateTime(datetime):
    """Keep both metadata and row timestamps stable across host timezones."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc)

    def astimezone(self, tz=None):
        return self


def test_history_facade_keeps_composed_entrypoints_callable(tmp_path: Path) -> None:
    module_entrypoints = (
        "ConversationLog",
        "HistoryConsolidator",
        "append_off_loop",
        "append_if_absent_off_loop",
        "append_rows_if_absent_off_loop",
        "update_metadata_off_loop",
        "parse_search_query",
        "snippet_needles",
        "needles_match_text",
        "is_incognito_transcript",
        "carry_provenance",
        "carry_unowned_metadata",
        "metadata_now_iso",
        "monotonic_transcript_ts",
        "transcript_sort_key",
        "transcript_stem",
        "transcript_stems",
    )
    for name in module_entrypoints:
        assert callable(getattr(history, name, None)), f"history facade lost callable {name}"

    log = history.ConversationLog(base_dir=tmp_path)
    log_entrypoints = (
        "append",
        "append_if_absent",
        "atomic_appends",
        "read_messages",
        "recent",
        "list_sessions",
        "search_sessions",
        "get_metadata",
        "get_metadata_status",
        "set_title",
        "update_metadata",
        "delete_session",
        "rewrite_session",
        "rotation_generation",
        "snapshot_for_consolidation",
        "mark_consolidated",
    )
    for name in log_entrypoints:
        assert callable(getattr(log, name, None)), f"ConversationLog facade lost method {name}"


def test_history_facade_reexports_consolidation_collaborators() -> None:
    """Keep the semantic import surface that existed before extraction."""
    expected = {
        "AutoSkillProvenance": skills.AutoSkillProvenance,
        "SKILL_UPDATE": frontmatter.SKILL_UPDATE,
        "ToolApprovalPolicy": llm_helpers.ToolApprovalPolicy,
        "background_turn": llm_helpers.background_turn,
        "frontmatter_value": frontmatter.frontmatter_value,
        "run_in_embed_pool": executors.run_in_embed_pool,
        "validate_skill_script": skills_script_validator.validate_skill_script,
    }
    for name, collaborator in expected.items():
        assert getattr(history, name) is collaborator


def test_append_preserves_exact_jsonl_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(history, "datetime", _FrozenDateTime)
    log = history.ConversationLog(base_dir=tmp_path)

    log.append(
        "thread:1",
        "assistant",
        "h\u00e9llo\n\u4e16\u754c",
        tools=["ReadFile"],
        source_thread="slack:123.456",
        source_user="U123",
        agent="agent-a",
        tab_id="tab-1",
        cls="notice",
        mid="message-1",
    )

    expected = os.linesep.encode().join(
        (
            b'{"_type": "metadata", "created_at": "2026-08-29T12:34:56+00:00", '
            b'"last_consolidated": 0, "agent": "agent-a", "tab_id": "tab-1"}',
            b'{"role": "assistant", "content": "h\\u00e9llo\\n\\u4e16\\u754c", '
            b'"cls": "notice", "ts": "2026-08-29T12:34:56+00:00", '
            b'"tools": ["ReadFile"], "source_thread": "slack:123.456", '
            b'"source_user": "U123", "meta": {"mid": "message-1"}}',
            b"",
        )
    )
    assert (tmp_path / "thread_1.jsonl").read_bytes() == expected


def test_search_scan_window_reads_the_history_facade_setting(tmp_path: Path, monkeypatch) -> None:
    log = history.ConversationLog(base_dir=tmp_path)
    log.append("older", "user", "window-only-match")
    log.append("newer", "user", "unrelated")
    os.utime(tmp_path / "older.jsonl", (1, 1))
    os.utime(tmp_path / "newer.jsonl", (2, 2))

    monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 1)
    assert log.search_sessions("window-only-match") == []

    monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 2)
    assert [item["key"] for item in log.search_sessions("window-only-match")] == ["older"]


def test_search_projection_calls_patchable_owner_text_seams(tmp_path: Path, monkeypatch) -> None:
    log = history.ConversationLog(base_dir=tmp_path)
    monkeypatch.setattr(
        log,
        "_iter_message_texts",
        lambda key: iter(("Straße", "second message")),
    )

    assert log._build_folded("session", 12.0, 0) == (20, "strasse\x00second message")
    assert list(log._snippet_cache.get("session")[2]) == ["Straße", "second message"]

    monkeypatch.setattr(log, "_snippet_texts", lambda key: iter(("before NEEDLE after",)))
    assert log._content_snippet("session", "needle") == "before NEEDLE after"
