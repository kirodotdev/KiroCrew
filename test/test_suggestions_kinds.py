"""The suggestions parser emits {text, kind} and never an unknown kind."""

from __future__ import annotations

import json

from kiro_crew.suggestions import (
    _FALLBACK_SUGGESTIONS,
    SUGGESTION_KINDS,
    SuggestionsCache,
    _parse_suggestions,
    _redact_suggestions,
)


def test_object_items_keep_a_known_kind() -> None:
    raw = json.dumps([{"text": "Review the auth PR", "kind": "review"}])
    assert _parse_suggestions(raw) == [{"text": "Review the auth PR", "kind": "review"}]


def test_kind_is_case_and_whitespace_normalized() -> None:
    raw = json.dumps([{"text": "Fix the build", "kind": " OPS "}])
    assert _parse_suggestions(raw)[0]["kind"] == "ops"


def test_unknown_or_missing_kind_becomes_general() -> None:
    raw = json.dumps([{"text": "a", "kind": "banana"}, {"text": "b"}, {"text": "c", "kind": 3}])
    assert [s["kind"] for s in _parse_suggestions(raw)] == ["general"] * 3


def test_legacy_string_items_are_still_accepted() -> None:
    assert _parse_suggestions('["Draft notes"]') == [{"text": "Draft notes", "kind": "general"}]


def test_malformed_blank_and_overlong_items_are_dropped() -> None:
    raw = json.dumps([{"kind": "code"}, {"text": "   "}, {"text": "x" * 81}, 5, {"text": "ok"}])
    assert _parse_suggestions(raw) == [{"text": "ok", "kind": "general"}]


def test_fenced_response_and_six_item_cap() -> None:
    items = [{"text": f"s{i}", "kind": "code"} for i in range(9)]
    parsed = _parse_suggestions("```json\n" + json.dumps(items) + "\n```")
    assert len(parsed) == 6


def test_unparseable_response_yields_empty() -> None:
    assert _parse_suggestions("not json") == []


def test_redaction_keeps_kind() -> None:
    out = _redact_suggestions([{"text": "Deploy with AKIAIOSFODNN7EXAMPLE", "kind": "ops"}])
    assert out[0]["kind"] == "ops"
    assert "AKIAIOSFODNN7EXAMPLE" not in out[0]["text"]


def test_fallbacks_use_only_known_kinds_and_are_copied() -> None:
    assert all(s["kind"] in SUGGESTION_KINDS for s in _FALLBACK_SUGGESTIONS)
    cache = SuggestionsCache()
    cache.suggestions[0]["text"] = "mutated"
    assert _FALLBACK_SUGGESTIONS[0]["text"] != "mutated"
