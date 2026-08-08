"""Tests for kiro_crew.session_digest."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.session_digest import SessionDigest, _collapse_whitespace, digest


@pytest.fixture()
def sessions_dir(tmp_path: Path) -> Path:
    """Create a fake sessions directory structure."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    (sdir / "archive").mkdir()
    return sdir


@pytest.fixture()
def cli_dir(tmp_path: Path) -> Path:
    """Create a fake kiro-cli sessions directory."""
    cdir = tmp_path / "cli"
    cdir.mkdir()
    return cdir


def _write_transcript(path: Path, lines: list[dict]) -> None:
    """Write a JSONL transcript file."""
    with open(path, "w", encoding="utf-8") as f:
        for record in lines:
            f.write(json.dumps(record) + "\n")


def _write_cli_log(path: Path, records: list[dict]) -> None:
    """Write a kiro-cli event log."""
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


class TestFirstMessageExtraction:
    """Tests for first_message field extraction."""

    def test_basic_first_message(self, sessions_dir: Path, cli_dir: Path) -> None:
        _write_transcript(
            sessions_dir / "dashboard_chat-1.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {"role": "user", "content": "Hello world", "ts": "2026-01-01T00:00:00"},
                {"role": "assistant", "content": "Hi there", "ts": "2026-01-01T00:00:01"},
                {"role": "user", "content": "Second message", "ts": "2026-01-01T00:01:00"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("dashboard_chat-1", ("dashboard_chat-1",), "sid-123")

        assert result.first_message == "Hello world"

    def test_whitespace_collapsed(self, sessions_dir: Path, cli_dir: Path) -> None:
        _write_transcript(
            sessions_dir / "test_ws.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {
                    "role": "user",
                    "content": "Hello   world\n\ttabs  and\n\nnewlines",
                    "ts": "2026-01-01T00:00:00",
                },
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_ws", ("test_ws",), "sid-nope")

        assert result.first_message == "Hello world tabs and newlines"

    def test_truncated_to_280_chars(self, sessions_dir: Path, cli_dir: Path) -> None:
        long_text = "A" * 500
        _write_transcript(
            sessions_dir / "test_long.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {"role": "user", "content": long_text, "ts": "2026-01-01T00:00:00"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_long", ("test_long",), "sid-nope")

        assert len(result.first_message) == 280
        assert result.first_message == "A" * 280

    def test_skips_empty_user_messages(self, sessions_dir: Path, cli_dir: Path) -> None:
        _write_transcript(
            sessions_dir / "test_empty.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {"role": "user", "content": "   ", "ts": "2026-01-01T00:00:00"},
                {"role": "user", "content": "Real message", "ts": "2026-01-01T00:01:00"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_empty", ("test_empty",), "sid-nope")

        assert result.first_message == "Real message"

    def test_fallback_to_cli_log(self, sessions_dir: Path, cli_dir: Path) -> None:
        """When no transcript exists, first_message comes from cli log."""
        _write_cli_log(
            cli_dir / "sid-abc.jsonl",
            [
                {
                    "kind": "Prompt",
                    "version": "1",
                    "data": {
                        "content": [{"kind": "text", "data": "CLI first message"}],
                        "message_id": "m1",
                    },
                },
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("no_transcript", ("no_transcript",), "sid-abc")

        assert result.first_message == "CLI first message"


class TestTurnCounting:
    """Tests for the turns field (real user prompt count)."""

    def test_counts_user_roles_only(self, sessions_dir: Path, cli_dir: Path) -> None:
        _write_transcript(
            sessions_dir / "test_turns.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {"role": "user", "content": "One", "ts": "t1"},
                {"role": "assistant", "content": "Reply", "ts": "t2"},
                {"role": "tool", "content": "Tool output", "ts": "t3"},
                {"role": "user", "content": "Two", "ts": "t4"},
                {"role": "user", "content": "Three", "ts": "t5"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_turns", ("test_turns",), "sid-nope")

        assert result.turns == 3

    def test_includes_archive_segments(self, sessions_dir: Path, cli_dir: Path) -> None:
        """Turns from archive segments are added to the live file count."""
        archive_dir = sessions_dir / "archive"
        _write_transcript(
            archive_dir / "test_arch__20260101-000000.jsonl",
            [
                {"_type": "archive", "reason": "rotate", "archived_at": "2026-01-01", "count": 2},
                {"role": "user", "content": "Old turn 1", "ts": "t1"},
                {"role": "assistant", "content": "Old reply", "ts": "t2"},
                {"role": "user", "content": "Old turn 2", "ts": "t3"},
            ],
        )
        _write_transcript(
            sessions_dir / "test_arch.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {"role": "user", "content": "New turn", "ts": "t4"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_arch", ("test_arch",), "sid-nope")

        assert result.turns == 3

    def test_whitespace_only_user_message_still_counts(
        self, sessions_dir: Path, cli_dir: Path
    ) -> None:
        """A user turn with only whitespace still counts as a turn (but not as first_message)."""
        _write_transcript(
            sessions_dir / "test_ws_turn.jsonl",
            [
                {"_type": "metadata", "created_at": "2026-01-01", "last_consolidated": 0},
                {"role": "user", "content": "   ", "ts": "t1"},
                {"role": "user", "content": "Real", "ts": "t2"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_ws_turn", ("test_ws_turn",), "sid-nope")

        # Whitespace-only content is truthy as a string, counts as a turn
        # but NOT as first_message (first_message requires .strip() truthy)
        assert result.turns == 2
        assert result.first_message == "Real"

    def test_fallback_to_cli_turns(self, sessions_dir: Path, cli_dir: Path) -> None:
        """When no transcript exists, turns come from cli log Prompt records."""
        _write_cli_log(
            cli_dir / "sid-turns.jsonl",
            [
                {"kind": "Prompt", "version": "1", "data": {"content": [], "message_id": "m1"}},
                {
                    "kind": "AssistantMessage",
                    "version": "1",
                    "data": {"content": [], "message_id": "m2"},
                },
                {"kind": "Prompt", "version": "1", "data": {"content": [], "message_id": "m3"}},
                {
                    "kind": "ToolResults",
                    "version": "1",
                    "data": {"content": [], "message_id": "m4", "results": []},
                },
                {"kind": "Prompt", "version": "1", "data": {"content": [], "message_id": "m5"}},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("no_transcript", ("no_transcript",), "sid-turns")

        assert result.turns == 3


class TestImageCounting:
    """Tests for the images field."""

    def test_counts_images_in_cli_log(self, sessions_dir: Path, cli_dir: Path) -> None:
        _write_cli_log(
            cli_dir / "sid-img.jsonl",
            [
                {
                    "kind": "Prompt",
                    "version": "1",
                    "data": {
                        "content": [
                            {"kind": "text", "data": "Look at this"},
                            {
                                "kind": "image",
                                "data": {
                                    "format": "png",
                                    "source": {"kind": "bytes", "data": [137, 80, 78, 71]},
                                },
                            },
                        ],
                        "message_id": "m1",
                    },
                },
                {
                    "kind": "AssistantMessage",
                    "version": "1",
                    "data": {
                        "content": [
                            {"kind": "text", "data": "I see the image"},
                            {
                                "kind": "image",
                                "data": {
                                    "format": "jpeg",
                                    "source": {"kind": "bytes", "data": [255, 216, 255]},
                                },
                            },
                            {
                                "kind": "image",
                                "data": {
                                    "format": "png",
                                    "source": {"kind": "bytes", "data": [137, 80]},
                                },
                            },
                        ],
                        "message_id": "m2",
                    },
                },
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("img_session", ("img_session",), "sid-img")

        assert result.images == 3

    def test_no_images_when_none_present(self, sessions_dir: Path, cli_dir: Path) -> None:
        _write_cli_log(
            cli_dir / "sid-noimg.jsonl",
            [
                {
                    "kind": "Prompt",
                    "version": "1",
                    "data": {
                        "content": [{"kind": "text", "data": "Just text"}],
                        "message_id": "m1",
                    },
                },
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("noimg", ("noimg",), "sid-noimg")

        assert result.images == 0


class TestRobustness:
    """Tests for error handling and graceful degradation."""

    def test_malformed_line_mid_file(self, sessions_dir: Path, cli_dir: Path) -> None:
        """A truncated/garbage line does not lose the whole count."""
        path = sessions_dir / "test_bad.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps({"_type": "metadata", "created_at": "x", "last_consolidated": 0}) + "\n"
            )
            f.write(json.dumps({"role": "user", "content": "Before garbage", "ts": "t1"}) + "\n")
            f.write("THIS IS NOT JSON {{{{ garbage\n")
            f.write(json.dumps({"role": "user", "content": "After garbage", "ts": "t2"}) + "\n")

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_bad", ("test_bad",), "sid-nope")

        assert result.first_message == "Before garbage"
        assert result.turns == 2

    def test_nonexistent_file(self, sessions_dir: Path, cli_dir: Path) -> None:
        """Non-existent files degrade to empty/zero."""
        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("ghost", ("ghost",), "ghost-sid")

        assert result == SessionDigest(first_message="", turns=0, images=0)

    def test_binary_content_in_line(self, sessions_dir: Path, cli_dir: Path) -> None:
        """Binary bytes embedded in a line don't crash the parser."""
        path = sessions_dir / "test_bin.jsonl"
        with open(path, "wb") as f:
            f.write(
                json.dumps(
                    {"_type": "metadata", "created_at": "x", "last_consolidated": 0}
                ).encode()
                + b"\n"
            )
            f.write(
                json.dumps({"role": "user", "content": "Good line", "ts": "t1"}).encode() + b"\n"
            )
            f.write(b"\xff\xfe\x00\x01 not valid utf8 at all\n")
            f.write(
                json.dumps({"role": "user", "content": "After binary", "ts": "t2"}).encode() + b"\n"
            )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_bin", ("test_bin",), "sid-nope")

        assert result.first_message == "Good line"
        assert result.turns == 2

    def test_empty_file(self, sessions_dir: Path, cli_dir: Path) -> None:
        """An empty transcript file degrades gracefully."""
        (sessions_dir / "test_empty.jsonl").write_text("", encoding="utf-8")

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("test_empty", ("test_empty",), "sid-nope")

        assert result == SessionDigest(first_message="", turns=0, images=0)

    def test_cli_log_malformed_line(self, sessions_dir: Path, cli_dir: Path) -> None:
        """Garbage in cli log doesn't crash."""
        path = cli_dir / "sid-bad.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json at all\n")
            f.write(
                json.dumps(
                    {
                        "kind": "Prompt",
                        "version": "1",
                        "data": {
                            "content": [
                                {"kind": "text", "data": "After garbage"},
                                {
                                    "kind": "image",
                                    "data": {
                                        "format": "png",
                                        "source": {"kind": "bytes", "data": [1]},
                                    },
                                },
                            ],
                            "message_id": "m1",
                        },
                    }
                )
                + "\n"
            )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("bad_cli", ("bad_cli",), "sid-bad")

        assert result.turns == 1
        assert result.images == 1
        assert result.first_message == "After garbage"


class TestCollapseWhitespace:
    """Unit tests for the _collapse_whitespace helper."""

    def test_collapses_internal(self) -> None:
        assert _collapse_whitespace("a   b\n\tc", 100) == "a b c"

    def test_truncates(self) -> None:
        assert _collapse_whitespace("hello world", 5) == "hello"

    def test_strips_leading_trailing(self) -> None:
        assert _collapse_whitespace("  spaced  ", 100) == "spaced"


class TestMultipleStems:
    """Tests for sessions that have multiple transcript stems (legacy Slack)."""

    def test_multiple_stems_union(self, sessions_dir: Path, cli_dir: Path) -> None:
        """Both canonical and legacy stems contribute turns."""
        _write_transcript(
            sessions_dir / "slack_1234.jsonl",
            [
                {"_type": "metadata", "created_at": "x", "last_consolidated": 0},
                {"role": "user", "content": "First msg on legacy stem", "ts": "t1"},
            ],
        )
        _write_transcript(
            sessions_dir / "slack_thread_1234.jsonl",
            [
                {"_type": "metadata", "created_at": "x", "last_consolidated": 0},
                {"role": "user", "content": "Canonical stem msg", "ts": "t2"},
            ],
        )

        with (
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("slack_session", ("slack_1234", "slack_thread_1234"), "sid-nope")

        assert result.turns == 2
        # First message comes from the first stem's file (ordered by stems tuple)
        assert result.first_message == "First msg on legacy stem"
