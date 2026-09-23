"""Tests for chat.py hook integration (validation, fail-closed, audit events)."""

from __future__ import annotations

import re

import pytest

from kiro_crew.dashboard import chat_runner
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


# A ``read`` title the way kiro-cli builds it for ``image_paths``: the operation
# content (long filenames with spaces) is the title, so its length tracks the
# user's filesystem, not any tool name. Well over MAX_TOOL_NAME_LEN.
_LONG_READ_TITLE = "View image " + " ".join(
    f"/mnt/Sign in with Apple - screenshot {i:02d} of the consent sheet.png" for i in range(6)
)


class TestCanonicalNameExemption:
    """A title travelling with a trusted canonical identity is content, not a name.

    The cap exists for the case where the title is the ONLY identity a hook can
    match on. ``AcpEvent.tool_name`` (the harness's ``_meta`` identity) is that
    identity when present, so the length predicate -- and only that predicate --
    is skipped for it, exactly as it is for ``is_shell``.
    """

    def test_long_title_with_canonical_name_passes(self):
        assert len(_LONG_READ_TITLE) > MAX_TOOL_NAME_LEN
        assert _validate_tool_name(_LONG_READ_TITLE, canonical_name="fs_read") == _LONG_READ_TITLE

    def test_long_title_without_canonical_name_still_rejected(self):
        """A backend that publishes no identity keeps today's loud refusal."""
        with pytest.raises(ValueError, match="exceeds max length"):
            _validate_tool_name(_LONG_READ_TITLE, canonical_name="")

    def test_canonical_name_does_not_skip_sanitisation(self):
        """Only the length predicate is relaxed: hidden characters are still stripped."""
        title = "View image\u200b " + "a" * (MAX_TOOL_NAME_LEN + 1) + "\u202e"
        result = _validate_tool_name(title, canonical_name="fs_read")
        assert "\u200b" not in result and "\u202e" not in result
        assert result == "View image " + "a" * (MAX_TOOL_NAME_LEN + 1)

    def test_canonical_name_does_not_skip_empty_check(self):
        """An empty (or all-hidden) title is refused even with a canonical identity."""
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_tool_name("", canonical_name="fs_read")
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_tool_name("\u200b \n", canonical_name="fs_read")

    def test_shell_exemption_unchanged(self):
        """The shell branch neither needs nor is affected by the canonical name."""
        long_cmd = "Running: " + "x" * 1000
        assert _validate_tool_name(long_cmd, is_shell=True, canonical_name="") == long_cmd
        assert _validate_tool_name(long_cmd, is_shell=True, canonical_name="shell") == long_cmd

    def test_every_chat_runner_call_site_passes_the_canonical_name(self):
        """Every permission path validates the title WITH the ``_meta`` identity.

        A path that still validates ``event.title`` alone re-breaks long ``read``
        titles on that path only, so the call sites are enumerated structurally
        rather than one path at a time.
        """
        from pathlib import Path

        source = Path(chat_runner.__file__).read_text(encoding="utf-8")
        calls = re.findall(r"_validate_tool_name\([^)]*\)", source)
        assert len(calls) >= 6, f"expected the known permission paths, found {len(calls)}"
        missing = [c for c in calls if "canonical_name=event.tool_name" not in c]
        assert not missing, missing
