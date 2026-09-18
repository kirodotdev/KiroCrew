"""Unit tests for chat_utils.py — redaction, model normalization, queue ops."""

from __future__ import annotations

import json

import pytest

import kiro_crew.dashboard.chat_utils as chat_utils
from kiro_crew.dashboard.chat_utils import (
    _history_key_for,
    _normalize_model,
    _prepare_messages,
    _redact_deep,
    _redact_for_display,
    _redact_tool_field,
    _remove_queued_by_id,
    _validate_tool_name,
    is_deprecated_model,
)
from kiro_crew.trust_patterns import extract_bash_command


class TestRedactDeep:
    def test_string(self):
        # AKIAIOSFODNN7EXAMPLE is a well-known test AWS key
        result = _redact_deep("key AKIAIOSFODNN7EXAMPLE here")
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_dict(self):
        result = _redact_deep({"a": "AKIAIOSFODNN7EXAMPLE"})
        assert "AKIAIOSFODNN7EXAMPLE" not in result["a"]

    def test_list(self):
        result = _redact_deep(["AKIAIOSFODNN7EXAMPLE"])
        assert "AKIAIOSFODNN7EXAMPLE" not in result[0]

    def test_nested(self):
        result = _redact_deep({"a": [{"b": "AKIAIOSFODNN7EXAMPLE"}]})
        assert "AKIAIOSFODNN7EXAMPLE" not in result["a"][0]["b"]

    def test_non_string_passthrough(self):
        assert _redact_deep(42) == 42
        assert _redact_deep(None) is None


class TestExtractBashCommand:
    def test_json_input(self):
        assert extract_bash_command('{"command": "ls -la"}') == "ls -la"

    def test_raw_input(self):
        assert extract_bash_command("ls -la") == "ls -la"

    def test_empty_json(self):
        assert extract_bash_command("{}") == ""

    def test_invalid_json(self):
        assert extract_bash_command("not json {") == "not json {"


class TestNormalizeModel:
    # _normalize_model is deprecation-migration only: it must round-trip user
    # selections verbatim (only deprecated ids change) so persistence works.
    def test_deprecated_mapped(self):
        assert _normalize_model("claude-opus-4.6-1m") == "claude-opus-4.6"
        assert _normalize_model("claude-sonnet-4.6-1m") == "claude-sonnet-4.6"

    def test_non_deprecated_passthrough(self):
        # opus/sonnet versions preserved verbatim (NOT remapped here)
        assert _normalize_model("claude-opus-4.7") == "claude-opus-4.7"
        assert _normalize_model("claude-opus-4.8") == "claude-opus-4.8"
        assert _normalize_model("claude-sonnet-4.6") == "claude-sonnet-4.6"
        assert _normalize_model("opus") == "opus"
        assert _normalize_model("custom-model") == "custom-model"
        assert _normalize_model("") == ""

    def test_is_deprecated(self):
        assert is_deprecated_model("claude-opus-4.6-1m") is True
        assert is_deprecated_model("claude-sonnet-4.6") is False


class TestValidateToolName:
    def test_valid_name(self):
        assert _validate_tool_name("execute_bash") == "execute_bash"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_tool_name("")

    def test_shell_flag_skips_length(self):
        long_name = "x" * 500
        result = _validate_tool_name(long_name, is_shell=True)
        assert len(result) == 500


class TestHistoryKeyFor:
    def test_raw_key(self):
        assert _history_key_for("chat-1-123") == "dashboard:chat-1-123"

    def test_already_prefixed(self):
        assert _history_key_for("dashboard:chat-1-123") == "dashboard:chat-1-123"

    def test_filesystem_roundtrip(self):
        assert _history_key_for("dashboard_chat-1-123") == "dashboard:chat-1-123"

    def test_stacked_prefixes(self):
        assert _history_key_for("dashboard_dashboard_chat-1") == "dashboard:chat-1"


class TestRemoveQueuedById:
    def test_removes_matching(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "queued", "cls": json.dumps({"queue_id": "q1"})},
        ]
        assert _remove_queued_by_id(msgs, "q1") is True
        assert len(msgs) == 1

    def test_no_match(self):
        msgs = [{"role": "queued", "cls": json.dumps({"queue_id": "q1"})}]
        assert _remove_queued_by_id(msgs, "q2") is False
        assert len(msgs) == 1

    def test_non_queued_skipped(self):
        msgs = [{"role": "user", "cls": json.dumps({"queue_id": "q1"})}]
        assert _remove_queued_by_id(msgs, "q1") is False


class TestPrepareMessages:
    def test_reuses_canonical_wire_row_collapse(self, monkeypatch):
        messages = [
            {"role": "chunk", "content": "hel"},
            {"role": "done", "content": ""},
            {"role": "chunk", "content": "lo"},
        ]
        calls = []
        collapse = chat_utils._collapse_wire_rows

        def record_collapse(rows):
            calls.append(rows)
            return collapse(rows)

        monkeypatch.setattr(chat_utils, "_collapse_wire_rows", record_collapse)

        result = _prepare_messages(messages, running=True, live_child="")

        assert calls == [messages]
        assert result == [{"role": "streaming", "content": "hello", "cls": "msg msg-a"}]

    def test_strips_done(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "done", "content": ""}]
        result = _prepare_messages(msgs, running=False, live_child="")
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_collapses_chunks(self):
        msgs = [
            {"role": "chunk", "content": "hel"},
            {"role": "chunk", "content": "lo"},
            {"role": "user", "content": "next"},
        ]
        result = _prepare_messages(msgs, running=False, live_child="")
        assert result[0]["role"] == "streaming"
        assert "hel" in result[0]["content"]
        assert result[1]["role"] == "user"

    def test_trailing_chunks(self):
        msgs = [{"role": "chunk", "content": "partial"}]
        result = _prepare_messages(msgs, running=True, live_child="")
        assert len(result) == 1
        assert result[0]["role"] == "streaming"

    def test_streaming_row_carries_the_newest_chunk_seq(self):
        """The fold stands for every delta, so it vouches for the NEWEST seq.

        The client seeds its replay guard from this value; the first delta's
        seq would let every later delta be applied a second time.
        """
        msgs = [
            {"role": "chunk", "content": "hel", "seq": 1},
            {"role": "chunk", "content": "lo", "seq": 2},
            {"role": "chunk", "content": "!", "seq": 3},
        ]
        result = _prepare_messages(msgs, running=True, live_child="")
        assert result == [{"role": "streaming", "content": "hello!", "cls": "msg msg-a", "seq": 3}]

    def test_streaming_row_carries_the_generation_beside_the_seq(self):
        """The fold also carries the gateway generation the seqs belong to, so a
        client can tell a floor numbered by a process that has since restarted
        apart from this one instead of ordering the two against each other."""
        msgs = [
            {"role": "chunk", "content": "hel", "seq": 1, "gen": "abcd1234"},
            {"role": "chunk", "content": "lo", "seq": 2, "gen": "abcd1234"},
        ]
        result = _prepare_messages(msgs, running=True, live_child="")
        assert result == [
            {
                "role": "streaming",
                "content": "hello",
                "cls": "msg msg-a",
                "seq": 2,
                "gen": "abcd1234",
            }
        ]

    def test_streaming_row_omits_seq_when_the_rows_carry_none(self):
        """Legacy window rows have no seq; the wire shape stays as before."""
        msgs = [{"role": "chunk", "content": "hel"}, {"role": "chunk", "content": "lo"}]
        result = _prepare_messages(msgs, running=True, live_child="")
        assert result == [{"role": "streaming", "content": "hello", "cls": "msg msg-a"}]

    def test_collapse_keeps_the_max_seq_and_never_mutates_input(self):
        rows = [
            {"role": "chunk", "content": "a", "seq": 4},
            {"role": "chunk", "content": "b", "seq": 5},
            {"role": "user", "content": "next"},
            {"role": "chunk", "content": "c", "seq": 6},
        ]
        snapshot = [dict(r) for r in rows]
        out = chat_utils._collapse_wire_rows(rows)
        assert [m.get("seq") for m in out] == [5, None, 6]
        assert rows == snapshot


class TestRedactForDisplay:
    def test_redacts_credentials(self):
        result = _redact_for_display("key AKIAIOSFODNN7EXAMPLE here")
        assert "AKIAIOSFODNN7EXAMPLE" not in result


class TestRedactToolField:
    def test_empty_returns_empty(self):
        assert _redact_tool_field(None) == ""
        assert _redact_tool_field("") == ""

    def test_short_ascii_passthrough(self):
        result = _redact_tool_field("hello world")
        assert result == "hello world"

    def test_redacts_credentials(self):
        result = _redact_tool_field("key AKIAIOSFODNN7EXAMPLE here")
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_ascii_truncation(self):
        # ASCII: 1 byte per char, so 200 chars = 200 bytes; cap at 50.
        result = _redact_tool_field("a" * 200, limit=50)
        assert len(result.encode("utf-8")) <= 50 + len(
            "\n… [truncated at 50 bytes]".encode("utf-8")
        )
        assert "[truncated at 50 bytes]" in result

    def test_multibyte_capped_on_bytes_not_chars(self):
        # 200 emoji × 4 bytes = 800 bytes UTF-8 (each "🎉" is 4 bytes).
        # Pre-fix this would have passed through if limit were char-based ≥ 200.
        # With true byte cap at 100, the encoded prefix must be ≤ 100 bytes.
        text = "🎉" * 200
        result = _redact_tool_field(text, limit=100)
        # Strip the sentinel before checking the capped slice.
        head = result.split("\n… [truncated")[0]
        assert len(head.encode("utf-8")) <= 100
        assert "[truncated at 100 bytes]" in result

    def test_multibyte_partial_sequence_dropped(self):
        # Cut at a non-multiple-of-4 boundary in a 4-byte-per-char stream.
        # errors='ignore' drops the partial trailing bytes; no UnicodeDecodeError.
        text = "🎉" * 50
        result = _redact_tool_field(text, limit=7)  # 7 bytes = 1 emoji + partial
        head = result.split("\n… [truncated")[0]
        # First emoji (4 bytes) survives, partial 3 bytes dropped.
        assert head == "🎉"

    def test_under_limit_no_sentinel(self):
        result = _redact_tool_field("ok", limit=1_000_000)
        assert "truncated" not in result
        assert result == "ok"

    def test_purpose_limit(self):
        result = _redact_tool_field("x" * 10_000, limit=8_000)
        assert "[truncated at 8,000 bytes]" in result
