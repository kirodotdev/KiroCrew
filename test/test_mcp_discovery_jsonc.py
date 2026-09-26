"""Tests for JSONC-tolerant mcp.json loading.

The Kiro IDE and CLI accept ``//`` comments in ``~/.kiro/settings/mcp.json``;
a strict ``json.loads`` there drops *every* server in the file because a
single comment aborted the whole parse. These tests drive the real reader
against files on disk, no stubs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew import mcp_discovery

CONFIG_WITH_JSONC = """{
  // A commented-out server block, exactly the edit the IDE's conventions invite.
  // "dead-server": { "command": "npx", "args": ["-y", "gone"] }
  "mcpServers": {
    "alive-server": {
      "command": "uvx",
      "args": ["some-server"],
      "env": {
        "endpoint": "https://example.com/v1",  // comment after a value
        /* block comment
           spanning lines */
        "token": "abc//not-a-comment",
      },
    },
  },
}
"""


@pytest.fixture
def read_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point discovery at a single config file and call the real reader."""

    def _make(text: str, name: str = "mcp.json") -> Path:
        cfg = tmp_path / name
        cfg.write_text(text, encoding="utf-8")
        monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (cfg,))
        monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: [])
        return cfg

    return _make


class TestStripJsonc:
    def test_preserves_url_slashes(self):
        text = '{"u": "https://host/path//x"}'
        assert json.loads(mcp_discovery.strip_jsonc(text)) == {"u": "https://host/path//x"}

    def test_preserves_escaped_quote_with_slashes(self):
        text = '{"u": "a \\" https://b // c"}'
        assert json.loads(mcp_discovery.strip_jsonc(text)) == {"u": 'a " https://b // c'}

    def test_line_comment_to_eof_without_newline(self):
        assert json.loads(mcp_discovery.strip_jsonc('{"a": 1} // done')) == {"a": 1}

    def test_block_comment_without_terminator_is_refused(self):
        # An unterminated /* must not swallow everything through EOF: that
        # would let a malformed file parse tolerantly. A comment that never
        # closes is a broken file, not a tolerated one.
        with pytest.raises(ValueError, match="unterminated block comment"):
            mcp_discovery.strip_jsonc('{"a": 1} /* open')
        # The same file must still fail when the comment sits mid-document.
        with pytest.raises(ValueError, match="unterminated block comment"):
            mcp_discovery.strip_jsonc('{"a": [1, /* open')
        # A terminated comment after a value keeps working.
        assert json.loads(mcp_discovery.strip_jsonc('{"a": 1} /* open */')) == {"a": 1}

    def test_trailing_commas(self):
        text = '{"a": [1, 2,], "b": {"c": 3,},}'
        assert json.loads(mcp_discovery.strip_jsonc(text)) == {
            "a": [1, 2],
            "b": {"c": 3},
        }

    def test_trailing_comma_before_a_comment(self):
        """Comma + commented-out LAST server: the lookahead must cross the
        comment to see the closing brace, or the comma stays and the whole
        file fails again -- the exact edit the IDE's conventions invite."""
        text = '{"a": {"x": 1,},\n// "dead": {"y": 2}\n}'
        assert json.loads(mcp_discovery.strip_jsonc(text)) == {"a": {"x": 1}}
        # Same shape with a block comment, and comma-at-EOF after a comment.
        text2 = '{"a": [1,], /* tail */}'
        assert json.loads(mcp_discovery.strip_jsonc(text2)) == {"a": [1]}
        text3 = '{"a": {"x": 1, // comment\n}}'
        assert json.loads(mcp_discovery.strip_jsonc(text3)) == {"a": {"x": 1}}

    def test_trailing_comma_inside_a_string_value_is_not_rewritten(self):
        """Trailing-comma removal only ever runs outside strings.

        A value that itself contains a comma followed by whitespace and a
        closing brace/bracket is ordinary hand-written text; mangling it would
        corrupt a value that later flows into the written agent config and the
        CC sidecar with no re-validation.
        """
        text = '{"a": "literal,}", "b": ["x, ]", "y"], "c": "keep,  comma"}'
        assert json.loads(mcp_discovery.strip_jsonc(text)) == {
            "a": "literal,}",
            "b": ["x, ]", "y"],
            "c": "keep,  comma",
        }
        # Real trailing commas are still removed, including across newlines.
        assert json.loads(mcp_discovery.strip_jsonc('{"a": [1, 2,],\n"b": 3,}')) == {
            "a": [1, 2],
            "b": 3,
        }

    def test_broken_json_still_broken(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads(mcp_discovery.strip_jsonc("{ this is not json"))


class TestLoadMcpJsonBySourceJsonc:
    def test_line_comment_and_trailing_commas_keep_all_servers(self, read_source):
        """A file with one comment still yields every uncommented server."""
        read_source(CONFIG_WITH_JSONC)
        by_source = mcp_discovery._load_mcp_json_by_source()
        servers = by_source[mcp_discovery.SCOPE_KIROCREW]
        assert "alive-server" in servers
        # The commented-out block must NOT come back as a server.
        assert "dead-server" not in servers
        spec = servers["alive-server"]
        # String contents with // and the trailing commas survived intact.
        assert spec["env"]["endpoint"] == "https://example.com/v1"
        assert spec["env"]["token"] == "abc//not-a-comment"

    def test_genuinely_broken_file_is_still_skipped(self, read_source, caplog):
        """A non-JSON file must keep the old log-and-skip behavior."""
        cfg = read_source("{ this is not json")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
            by_source = mcp_discovery._load_mcp_json_by_source()
        assert by_source[mcp_discovery.SCOPE_KIROCREW] == {}
        assert any(
            "Failed to load MCP config" in rec.message and str(cfg) in rec.message
            for rec in caplog.records
        )

    def test_unterminated_block_comment_is_still_skipped(self, read_source, caplog):
        """A file whose block comment never closes is malformed, not JSONC:
        strip_jsonc refuses it and the reader keeps the log-and-skip path."""
        cfg = read_source(
            '{\n  "mcpServers": {\n    "alive": {"command": "uvx"}\n  },\n} /* open'
        )
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
            by_source = mcp_discovery._load_mcp_json_by_source()
        assert by_source[mcp_discovery.SCOPE_KIROCREW] == {}
        assert any(
            "Failed to load MCP config" in rec.message and str(cfg) in rec.message
            for rec in caplog.records
        )

    def test_tolerant_parse_warns_about_the_dialect(self, read_source, caplog):
        """Success after the retry should say so, not pass silently."""
        read_source(CONFIG_WITH_JSONC)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
            by_source = mcp_discovery._load_mcp_json_by_source()
        assert "alive-server" in by_source[mcp_discovery.SCOPE_KIROCREW]
        assert any("JSONC" in rec.message for rec in caplog.records)
