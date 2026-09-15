"""Tests for chat.py hook integration (validation, fail-closed, audit events)."""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.chat import _validate_tool_name
from kiro_crew.validation import MAX_TOOL_NAME_LEN


class TestToolNameValidation:
    """Test _validate_tool_name function for security controls."""

    def test_valid_tool_names(self):
        """Valid tool names pass validation."""
        assert _validate_tool_name("ReadFile") == "ReadFile"
        assert _validate_tool_name("builder-mcp--Search") == "builder-mcp--Search"
        assert _validate_tool_name("fs_write") == "fs_write"
        assert _validate_tool_name("git/commit") == "git/commit"

    def test_empty_tool_name(self):
        """Empty tool names are rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_tool_name("")

    def test_max_length_exceeded(self):
        """Tool names exceeding max length are rejected (non-execute)."""
        long_name = "a" * (MAX_TOOL_NAME_LEN + 1)
        with pytest.raises(ValueError, match="exceeds max length"):
            _validate_tool_name(long_name)

    def test_shell_skips_length_check(self):
        """Shell tool titles can be arbitrarily long (bash command lines)."""
        long_cmd = "Running: " + "x" * 1000
        assert _validate_tool_name(long_cmd, is_shell=True) == long_cmd

    def test_long_non_shell_name_still_rejected(self):
        """A long title that is NOT a shell command is still capped."""
        long_name = "Reading " + "a" * (MAX_TOOL_NAME_LEN + 1)
        with pytest.raises(ValueError, match="exceeds max length"):
            _validate_tool_name(long_name, is_shell=False)

    def test_display_titles_accepted(self):
        """Display titles with shell-like characters are accepted (used for hook matching only)."""
        assert _validate_tool_name("Running: echo hello") == "Running: echo hello"
        assert (
            _validate_tool_name("Creating random_2026-03-22.txt")
            == "Creating random_2026-03-22.txt"
        )
        assert _validate_tool_name("Reading /tmp/file.json") == "Reading /tmp/file.json"

    def test_unicode_normalization(self):
        """Hidden Unicode characters are stripped."""
        # Zero-width space
        assert _validate_tool_name("Tool\u200bName") == "ToolName"
        # Direction override
        assert _validate_tool_name("Tool\u202eName") == "ToolName"

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace is trimmed."""
        assert _validate_tool_name("  ToolName  ") == "ToolName"
        assert _validate_tool_name("\nToolName\n") == "ToolName"

    def test_valid_namespace_separator(self):
        """Forward slash for namespaces is allowed."""
        assert _validate_tool_name("mcp/core/read") == "mcp/core/read"

    def test_valid_underscore_hyphen(self):
        """Underscores and hyphens are allowed."""
        assert _validate_tool_name("my_tool-name") == "my_tool-name"
