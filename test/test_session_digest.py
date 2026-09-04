"""Tests for kiro_crew.session_digest."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import session_digest
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


class _SpyHandle:
    """Wraps a real file handle, recording how the reader draws bytes from it."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.readline_limits: list[int] = []
        self.iterated = False

    def readline(self, limit: int = -1) -> str:
        self.readline_limits.append(limit)
        return self._real.readline(limit)  # type: ignore[attr-defined]

    def __iter__(self) -> object:
        # `for line in handle` — the unbounded shape this fix removes.
        self.iterated = True
        return iter(self._real)  # type: ignore[call-overload]

    def __enter__(self) -> _SpyHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.__exit__(*exc)  # type: ignore[attr-defined]


def _user_record_of_length(total: int) -> str:
    """A JSON user record whose serialized form is exactly *total* characters."""
    pad = total - len(json.dumps({"role": "user", "content": ""}))
    assert pad >= 0, "requested length is shorter than the record envelope"
    return json.dumps({"role": "user", "content": "x" * pad})


class TestBoundedRecords:
    """A crafted newline-free record must not be materialised (#6345).

    Both trees read here are agent-writable, so `for line in handle` let one
    line without a newline in it allocate the whole file. These tests pin the
    cap's behaviour with a small patched cap; the real one is 256 MiB.
    """

    def test_over_cap_transcript_record_is_skipped(self, sessions_dir: Path, cli_dir: Path) -> None:
        """An over-cap user record contributes neither a turn nor first_message."""
        path = sessions_dir / "over_cap.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"_type": "metadata", "created_at": "x"}) + "\n")
            f.write(json.dumps({"role": "user", "content": "H" * 400}) + "\n")
            f.write(json.dumps({"role": "user", "content": "kept"}) + "\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", 200, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("over_cap", ("over_cap",), "sid-nope")

        assert result.turns == 1
        assert result.first_message == "kept"

    def test_transcript_handle_is_never_read_unbounded(
        self, sessions_dir: Path, cli_dir: Path
    ) -> None:
        """Every read carries a limit, and the handle is never iterated."""
        _write_transcript(
            sessions_dir / "bounded.jsonl",
            [
                {"_type": "metadata", "created_at": "x"},
                {"role": "user", "content": "hello"},
            ],
        )
        spies: list[_SpyHandle] = []
        real_open = open

        def _spy_open(*args: object, **kwargs: object) -> _SpyHandle:
            spy = _SpyHandle(real_open(*args, **kwargs))  # type: ignore[arg-type]
            spies.append(spy)
            return spy

        with (
            patch("kiro_crew.session_digest.open", _spy_open),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("bounded", ("bounded",), "sid-nope")

        assert result.turns == 1
        assert spies, "the transcript was never opened"
        for spy in spies:
            assert not spy.iterated, "the handle was iterated, so one line is unbounded"
            assert spy.readline_limits, "no bounded read was issued"
            cap = session_digest._RECORD_CAP
            # cap + 2, relaxed from cap + 1 when the shared reader began bounding
            # each read by what the CARRIED record has left rather than reading a
            # full cap every time. cap + 2 is forced, not convenient: a legal
            # at-cap record ending CRLF is cap + 2 bytes, so a read ceiling of
            # cap + 1 could never assemble one and would refuse it. This assertion
            # exists to pin that no read is UNBOUNDED, which it still does.
            assert all(0 < limit <= cap + 2 for limit in spy.readline_limits)

    def test_record_exactly_at_cap_survives(self, sessions_dir: Path, cli_dir: Path) -> None:
        """The cap is inclusive: a record of exactly cap bytes still counts."""
        cap = 200
        path = sessions_dir / "at_cap.jsonl"
        # newline="" so the terminator is one byte on every platform: text mode
        # would write CRLF on Windows, pushing a cap-byte record to cap+1 bytes
        # with no LF in the first read and making it look over-cap.
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(_user_record_of_length(cap) + "\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("at_cap", ("at_cap",), "sid-nope")

        assert result.turns == 1

    def test_record_one_over_cap_is_skipped(self, sessions_dir: Path, cli_dir: Path) -> None:
        """One byte past the cap is already over it."""
        cap = 200
        path = sessions_dir / "over_by_one.jsonl"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(_user_record_of_length(cap + 1) + "\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("over_by_one", ("over_by_one",), "sid-nope")

        assert result.turns == 0

    def test_drain_resumes_at_the_next_record(self, sessions_dir: Path, cli_dir: Path) -> None:
        """A record several caps long is drained, not lost mid-way, and the scan continues."""
        cap = 100
        path = sessions_dir / "deep_drain.jsonl"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps({"role": "user", "content": "H" * (cap * 7)}) + "\n")
            f.write(json.dumps({"role": "user", "content": "first kept"}) + "\n")
            f.write(json.dumps({"role": "user", "content": "second kept"}) + "\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("deep_drain", ("deep_drain",), "sid-nope")

        assert result.turns == 2
        assert result.first_message == "first kept"

    def test_unterminated_final_record_still_parses(
        self, sessions_dir: Path, cli_dir: Path
    ) -> None:
        """A crash mid-append leaves no trailing newline; a within-cap tail still counts."""
        path = sessions_dir / "no_terminator.jsonl"
        path.write_text(json.dumps({"role": "user", "content": "tail"}), encoding="utf-8")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", 200, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("no_terminator", ("no_terminator",), "sid-nope")

        assert result.turns == 1
        assert result.first_message == "tail"

    def test_exotic_line_boundary_stays_one_record(self, sessions_dir: Path, cli_dir: Path) -> None:
        """U+2028 inside a message is not a record boundary, so the turn survives.

        `str.splitlines` — the shape session_storage's manifest reader needs —
        would split here and lose the record; `readline` matches the iteration
        this replaced.
        """
        path = sessions_dir / "exotic.jsonl"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps({"role": "user", "content": "a\u2028b"}, ensure_ascii=False) + "\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", 200, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("exotic", ("exotic",), "sid-nope")

        assert result.turns == 1

    def test_over_cap_record_cannot_forge_a_record_from_its_tail(
        self, sessions_dir: Path, cli_dir: Path
    ) -> None:
        """A refused record must not smuggle a valid record out of its own tail.

        The whole file below is ONE line. Its first cap+1 characters are junk, and
        a complete user record sits immediately after that boundary. Draining the
        refused record is what keeps the tail from being read as a record of its
        own: without the drain, the next bounded read starts on the forged object,
        ends on the real newline, and the reader counts a turn inside a record it
        reported as skipped.
        """
        cap = 100
        forged = json.dumps({"role": "user", "content": "phantom"})
        path = sessions_dir / "forged_tail.jsonl"
        path.write_text("H" * (cap + 1) + forged + "\n", encoding="utf-8", newline="")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("forged_tail", ("forged_tail",), "sid-nope")

        assert result.turns == 0
        assert result.first_message == ""

    def test_cap_counts_bytes_not_characters(self, sessions_dir: Path, cli_dir: Path) -> None:
        """A record under the cap in code points but over it in bytes is refused.

        The cap has to be a memory bound, and one astral code point costs four
        bytes of `str` under PEP 393 -- so counting characters would admit four
        times the resident text the number promises. The record below is ~86 code
        points and 266 bytes against a 200-byte cap.
        """
        cap = 200
        content = "\U0001f600" * 60  # 60 code points, 240 UTF-8 bytes
        record = json.dumps({"role": "user", "content": content}, ensure_ascii=False)
        assert len(record) <= cap < len(record.encode("utf-8"))
        path = sessions_dir / "astral.jsonl"
        path.write_text(record + "\n", encoding="utf-8", newline="")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("astral", ("astral",), "sid-nope")

        assert result.turns == 0

    def test_crlf_terminated_records_still_parse(self, sessions_dir: Path, cli_dir: Path) -> None:
        """Reading binary drops universal-newline translation; CRLF must still work.

        `readline` splits on the LF, so the CR rides on the end of the record and
        every caller's `strip()` removes it before `json.loads`.
        """
        path = sessions_dir / "crlf.jsonl"
        body = "".join(
            json.dumps({"role": "user", "content": text}) + "\r\n"
            for text in ("first crlf", "second crlf")
        )
        path.write_text(body, encoding="utf-8", newline="")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", 4096, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("crlf", ("crlf",), "sid-nope")

        assert result.turns == 2
        assert result.first_message == "first crlf"

    def test_crlf_record_at_the_cap_boundary_is_accepted(
        self, sessions_dir: Path, cli_dir: Path
    ) -> None:
        """A CRLF-terminated record of exactly cap bytes is accepted.

        This REVERSES a one-byte refusal that main pinned deliberately. The pin's
        stated cost was that buying the byte back "would cost the reader its
        single invariant (a return shorter than cap+1 is a whole record)" -- true
        of the old reader, which decided over-cap straight from the length of one
        `readline(cap + 1)`. The shared reader no longer has that invariant to
        lose: it already defers a trailing carriage return across reads, because
        it must not split a CRLF whose line feed has not arrived. Excluding that
        pending byte from the body length therefore costs nothing that was not
        already being paid, and the record below is legal by the cap's own
        definition -- its body IS cap bytes.

        Left refused, it suppressed a real participation entry in
        `members.read_activity`, which is why this is a correctness fix rather
        than a cosmetic one. Flagged to the reviewer as a main-owned behaviour
        change rather than folded in silently.
        """
        cap = 200
        path = sessions_dir / "crlf_at_cap.jsonl"
        path.write_bytes(_user_record_of_length(cap).encode("utf-8") + b"\r\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            at_cap = digest("crlf_at_cap", ("crlf_at_cap",), "sid-nope")

        assert at_cap.turns == 1

        # One byte OVER the cap is still refused, which locates the boundary
        # precisely instead of asserting only that something was accepted.
        over = sessions_dir / "crlf_over_cap.jsonl"
        over.write_bytes(_user_record_of_length(cap + 1).encode("utf-8") + b"\r\n")

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", cap, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            refused = digest("crlf_over_cap", ("crlf_over_cap",), "sid-nope")

        assert refused.turns == 0

    def test_over_cap_cli_record_drops_its_turn_and_images(
        self, sessions_dir: Path, cli_dir: Path
    ) -> None:
        """An over-cap kiro-cli record contributes neither a turn nor an image."""
        path = cli_dir / "sid-big.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "kind": "Prompt",
                        "data": {
                            "content": [
                                {"kind": "image", "data": "B" * 400},
                                {"kind": "text", "data": "oversized"},
                            ]
                        },
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "kind": "Prompt",
                        "data": {"content": [{"kind": "text", "data": "small kept"}]},
                    }
                )
                + "\n"
            )

        with (
            patch("kiro_crew.session_digest._RECORD_CAP", 200, create=True),
            patch("kiro_crew.session_digest.data_home", return_value=sessions_dir.parent),
            patch("kiro_crew.session_digest.kiro_sessions_dir", return_value=cli_dir),
        ):
            result = digest("cli_big", ("no_transcript",), "sid-big")

        assert result.turns == 1
        assert result.images == 0
        assert result.first_message == "small kept"
