"""Tests for recording crash recovery (kiro_crew.recording.recovery).

Validates Requirement 6.7:
- Detect sessions whose metadata status is still ``recording`` at startup
- Resume transitions to ``transcribing``
- Discard transitions to ``failed``
- No false positives on ``complete``, ``failed``, etc.
- Reports partial data availability (audio, live transcript)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.recording.recovery import (
    UnfinishedSession,
    detect_unfinished_sessions,
    discard_session,
    register_meeting_store,
    resume_session,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """A temporary directory acting as the app data root."""
    return tmp_path


class _FsMeetingStore:
    """A filesystem store matching the layout ``_create_meeting`` writes.

    Recovery reads through a ``MeetingStore`` rather than importing an app, so
    this suite brings its own. That is the point of the seam: the recording
    package is core and must be testable without any app installed.

    Newest-first ordering falls out of reverse-sorting the date-based paths,
    which is what the real stores do too.
    """

    @staticmethod
    def _base(root: Path | None) -> Path:
        return (root or Path()) / "meetings"

    def _iter(self, root: Path | None):
        base = self._base(root)
        if not base.is_dir():
            return
        for path in sorted(base.rglob("metadata.json"), reverse=True):
            try:
                yield path, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A metadata file half-written by a crash is exactly the case
                # recovery exists for; skipping it beats failing the scan.
                continue

    def list_meetings(self, root: Path | None = None) -> list[dict]:
        return [meeting for _, meeting in self._iter(root)]

    def get_meeting(self, meeting_id: str, root: Path | None = None) -> dict | None:
        for _, meeting in self._iter(root):
            if meeting.get("id") == meeting_id:
                return meeting
        return None

    def update_meeting(self, meeting_id: str, patch: dict, root: Path | None = None) -> dict | None:
        for path, meeting in self._iter(root):
            if meeting.get("id") != meeting_id:
                continue
            meeting.update(patch)
            path.write_text(json.dumps(meeting), encoding="utf-8")
            return meeting
        return None

    def resolve_meeting_dir(self, meeting_id: str, root: Path | None = None) -> Path | None:
        meeting = self.get_meeting(meeting_id, root)
        if meeting is None:
            return None
        mdir = self._base(root) / (meeting.get("storage_path") or "")
        return mdir if mdir.is_dir() else None


@pytest.fixture(autouse=True)
def _registered_store() -> None:
    """Install the test store for every test in this module."""
    register_meeting_store(_FsMeetingStore())


def _create_meeting(
    root: Path,
    meeting_id: str,
    title: str,
    date: str,
    status: str,
    *,
    with_audio: bool = False,
    with_live_transcript: bool = False,
) -> Path:
    """Helper: write a metadata.json in the date-based layout."""
    storage_path = f"2026/08/01/1030_{title.replace(' ', '_')}"
    mdir = root / "meetings" / storage_path
    mdir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": meeting_id,
        "title": title,
        "date": date,
        "status": status,
        "storage_path": storage_path,
    }
    (mdir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    if with_audio:
        (mdir / "audio.wav").write_bytes(b"\x00" * 100)
    if with_live_transcript:
        transcript = [{"text": "hello", "is_final": True, "timestamp": 1719849600.0}]
        (mdir / "transcript_local.json").write_text(json.dumps(transcript), encoding="utf-8")
    return mdir


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetectUnfinishedSessions:
    def test_no_meetings(self, data_root: Path) -> None:
        result = detect_unfinished_sessions(data_root)
        assert result == []

    def test_no_unfinished_sessions(self, data_root: Path) -> None:
        _create_meeting(data_root, "m1", "Done Meeting", "2026-08-01T10:30:00Z", "complete")
        _create_meeting(data_root, "m2", "Failed Meeting", "2026-08-01T11:00:00Z", "failed")
        result = detect_unfinished_sessions(data_root)
        assert result == []

    def test_detects_recording_status(self, data_root: Path) -> None:
        _create_meeting(
            data_root,
            "m1",
            "Crashed",
            "2026-08-01T10:30:00Z",
            "recording",
            with_audio=True,
            with_live_transcript=True,
        )
        _create_meeting(data_root, "m2", "Done", "2026-08-01T11:00:00Z", "complete")

        result = detect_unfinished_sessions(data_root)
        assert len(result) == 1
        assert result[0].meeting_id == "m1"
        assert result[0].title == "Crashed"
        assert result[0].has_audio is True
        assert result[0].has_live_transcript is True

    def test_detects_multiple_unfinished(self, data_root: Path) -> None:
        _create_meeting(data_root, "m1", "Crash1", "2026-08-01T10:30:00Z", "recording")
        _create_meeting(data_root, "m2", "Crash2", "2026-08-01T11:30:00Z", "recording")

        result = detect_unfinished_sessions(data_root)
        assert len(result) == 2
        ids = {s.meeting_id for s in result}
        assert ids == {"m1", "m2"}

    def test_reports_no_partial_data(self, data_root: Path) -> None:
        """A session with no audio/transcript files reports both as False."""
        _create_meeting(data_root, "m1", "Empty", "2026-08-01T10:30:00Z", "recording")

        result = detect_unfinished_sessions(data_root)
        assert len(result) == 1
        assert result[0].has_audio is False
        assert result[0].has_live_transcript is False

    def test_ignores_transcribing_status(self, data_root: Path) -> None:
        """Only ``recording`` is treated as unfinished, not ``transcribing``."""
        _create_meeting(data_root, "m1", "Transcribing", "2026-08-01T10:30:00Z", "transcribing")
        result = detect_unfinished_sessions(data_root)
        assert result == []


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class TestResumeSession:
    def test_resume_transitions_to_transcribing(self, data_root: Path) -> None:
        _create_meeting(data_root, "m1", "Crashed", "2026-08-01T10:30:00Z", "recording")
        result = resume_session("m1", data_root)
        assert result is not None
        assert result["status"] == "transcribing"

        # Should no longer appear as unfinished
        unfinished = detect_unfinished_sessions(data_root)
        assert len(unfinished) == 0

    def test_resume_nonexistent_returns_none(self, data_root: Path) -> None:
        result = resume_session("nonexistent", data_root)
        assert result is None

    def test_resume_non_recording_returns_none(self, data_root: Path) -> None:
        _create_meeting(data_root, "m1", "Done", "2026-08-01T10:30:00Z", "complete")
        result = resume_session("m1", data_root)
        assert result is None

    def test_resume_idempotent(self, data_root: Path) -> None:
        """Once resumed, a second resume returns None (no longer in recording)."""
        _create_meeting(data_root, "m1", "Crashed", "2026-08-01T10:30:00Z", "recording")
        first = resume_session("m1", data_root)
        assert first is not None
        second = resume_session("m1", data_root)
        assert second is None


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


class TestDiscardSession:
    def test_discard_transitions_to_failed(self, data_root: Path) -> None:
        _create_meeting(data_root, "m1", "Crashed", "2026-08-01T10:30:00Z", "recording")
        result = discard_session("m1", data_root)
        assert result is not None
        assert result["status"] == "failed"

        # Should no longer appear as unfinished
        unfinished = detect_unfinished_sessions(data_root)
        assert len(unfinished) == 0

    def test_discard_nonexistent_returns_none(self, data_root: Path) -> None:
        result = discard_session("nonexistent", data_root)
        assert result is None

    def test_discard_non_recording_returns_none(self, data_root: Path) -> None:
        _create_meeting(data_root, "m1", "Done", "2026-08-01T10:30:00Z", "complete")
        result = discard_session("m1", data_root)
        assert result is None

    def test_discard_preserves_files(self, data_root: Path) -> None:
        """Discard marks status as failed but does NOT delete files."""
        mdir = _create_meeting(
            data_root,
            "m1",
            "Crashed",
            "2026-08-01T10:30:00Z",
            "recording",
            with_audio=True,
            with_live_transcript=True,
        )
        discard_session("m1", data_root)

        # Files should still exist
        assert (mdir / "audio.wav").is_file()
        assert (mdir / "transcript_local.json").is_file()
        assert (mdir / "metadata.json").is_file()


# ---------------------------------------------------------------------------
# UnfinishedSession serialization
# ---------------------------------------------------------------------------


class TestUnfinishedSessionSerialization:
    def test_to_dict(self) -> None:
        session = UnfinishedSession(
            meeting_id="abc-123",
            title="Test Meeting",
            date="2026-08-01T10:30:00Z",
            storage_path="2026/08/01/1030_Test_Meeting",
            has_audio=True,
            has_live_transcript=False,
        )
        d = session.to_dict()
        assert d == {
            "meeting_id": "abc-123",
            "title": "Test Meeting",
            "date": "2026-08-01T10:30:00Z",
            "storage_path": "2026/08/01/1030_Test_Meeting",
            "has_audio": True,
            "has_live_transcript": False,
        }
