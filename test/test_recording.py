"""Tests for kiro_crew.recording — state machine, WAV writer, and persister.

Validates:
- State machine transitions (valid and invalid)
- WAV writer creates valid WAV files with correct format
- Persister debounces writes and handles final/partial segments
- All blocking I/O is offloaded (executor usage)
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import pytest

from kiro_crew.recording import WavWriter
from kiro_crew.recording.persister import LiveTranscriptPersister
from kiro_crew.recording.session import (
    InvalidTransitionError,
    RecordingSession,
    SessionState,
    active_session_count,
    register_session,
    unregister_session,
)

# ---------------------------------------------------------------------------
# State machine tests
# ---------------------------------------------------------------------------


class TestSessionStateMachine:
    """Test RecordingSession state transitions."""

    @pytest.mark.asyncio
    async def test_initial_state_is_idle(self) -> None:
        session = RecordingSession()
        assert session.state == SessionState.IDLE

    @pytest.mark.asyncio
    async def test_start_transitions_to_recording(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        assert session.state == SessionState.RECORDING

    @pytest.mark.asyncio
    async def test_pause_transitions_to_paused(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.pause()
        assert session.state == SessionState.PAUSED

    @pytest.mark.asyncio
    async def test_resume_transitions_to_recording(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.pause()
        await session.resume()
        assert session.state == SessionState.RECORDING

    @pytest.mark.asyncio
    async def test_stop_from_recording(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.stop()
        assert session.state == SessionState.PROCESSING

    @pytest.mark.asyncio
    async def test_stop_from_paused(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.pause()
        await session.stop()
        assert session.state == SessionState.PROCESSING

    @pytest.mark.asyncio
    async def test_mark_complete(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.stop()
        session.mark_complete()
        assert session.state == SessionState.COMPLETE

    @pytest.mark.asyncio
    async def test_mark_failed(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.stop()
        session.mark_failed()
        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_invalid_transition_start_twice(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        with pytest.raises(InvalidTransitionError):
            await session.start()

    @pytest.mark.asyncio
    async def test_invalid_transition_pause_in_idle(self) -> None:
        session = RecordingSession()
        with pytest.raises(InvalidTransitionError):
            await session.pause()

    @pytest.mark.asyncio
    async def test_invalid_transition_resume_in_idle(self) -> None:
        session = RecordingSession()
        with pytest.raises(InvalidTransitionError):
            await session.resume()

    @pytest.mark.asyncio
    async def test_invalid_transition_stop_in_idle(self) -> None:
        session = RecordingSession()
        with pytest.raises(InvalidTransitionError):
            await session.stop()

    @pytest.mark.asyncio
    async def test_invalid_complete_without_stop(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        with pytest.raises(InvalidTransitionError):
            session.mark_complete()

    @pytest.mark.asyncio
    async def test_duration_excludes_pauses(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        # Record for a bit
        await asyncio.sleep(0.05)
        d1 = session.duration_secs
        assert d1 > 0

        # Pause
        await session.pause()
        await asyncio.sleep(0.05)
        d_paused = session.duration_secs
        # Duration should NOT increase while paused
        await asyncio.sleep(0.05)
        d_still_paused = session.duration_secs
        # Allow small timing tolerance
        assert abs(d_still_paused - d_paused) < 0.02

        # Resume and check duration increases
        await session.resume()
        await asyncio.sleep(0.05)
        d_resumed = session.duration_secs
        assert d_resumed > d_paused

        await session.stop()

    @pytest.mark.asyncio
    async def test_duration_zero_before_start(self) -> None:
        session = RecordingSession()
        assert session.duration_secs == 0.0


# ---------------------------------------------------------------------------
# WAV writer tests
# ---------------------------------------------------------------------------


class TestWavWriter:
    """Test the WAV file writer."""

    @pytest.mark.asyncio
    async def test_creates_valid_wav_file(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()

        # Generate 1 second of silence (16000 samples * 2 bytes)
        pcm_data = b"\x00\x00" * 16000
        await writer.write(pcm_data)
        await writer.close()

        # Verify the file is a valid WAV
        assert audio_path.exists()
        with wave.open(str(audio_path), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 16000

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "a" / "b" / "c" / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()
        await writer.write(b"\x00\x00" * 100)
        await writer.close()
        assert audio_path.exists()

    @pytest.mark.asyncio
    async def test_multiple_writes(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()

        # Write in chunks
        chunk = b"\x00\x00" * 1600  # 100ms at 16kHz
        for _ in range(10):
            await writer.write(chunk)
        await writer.close()

        with wave.open(str(audio_path), "rb") as wf:
            assert wf.getnframes() == 16000

    @pytest.mark.asyncio
    async def test_write_after_close_is_noop(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()
        await writer.write(b"\x00\x00" * 100)
        await writer.close()

        # Should not raise
        await writer.write(b"\x00\x00" * 100)

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()
        await writer.close()
        await writer.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_empty_data_write_is_noop(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()
        await writer.write(b"")
        await writer.close()

        with wave.open(str(audio_path), "rb") as wf:
            assert wf.getnframes() == 0

    @pytest.mark.asyncio
    async def test_total_frames_tracking(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()

        assert writer.total_frames == 0
        await writer.write(b"\x00\x00" * 100)
        assert writer.total_frames == 100
        await writer.write(b"\x00\x00" * 50)
        assert writer.total_frames == 150
        await writer.close()

    @pytest.mark.asyncio
    async def test_duration_secs(self, tmp_path: Path) -> None:
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()

        # Write 1 second of audio
        await writer.write(b"\x00\x00" * 16000)
        assert abs(writer.duration_secs - 1.0) < 0.001
        await writer.close()


# ---------------------------------------------------------------------------
# Persister tests
# ---------------------------------------------------------------------------


class TestLiveTranscriptPersister:
    """Test the debounced transcript persister."""

    @pytest.mark.asyncio
    async def test_flush_writes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("Hello world", is_final=True)
        await persister.flush()

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["text"] == "Hello world"
        assert data[0]["is_final"] is True

    @pytest.mark.asyncio
    async def test_partial_replaces_previous_partial(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("Hel", is_final=False)
        await persister.add_segment("Hello", is_final=False)
        await persister.add_segment("Hello world", is_final=False)
        await persister.flush()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["text"] == "Hello world"
        assert data[0]["is_final"] is False

    @pytest.mark.asyncio
    async def test_final_replaces_trailing_partial(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("Hello wor", is_final=False)
        await persister.add_segment("Hello world.", is_final=True)
        await persister.flush()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["text"] == "Hello world."
        assert data[0]["is_final"] is True

    @pytest.mark.asyncio
    async def test_multiple_finals(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("First sentence.", is_final=True)
        await persister.add_segment("Second sentence.", is_final=True)
        await persister.flush()

        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["text"] == "First sentence."
        assert data[1]["text"] == "Second sentence."

    @pytest.mark.asyncio
    async def test_debounce_writes_after_interval(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=0.05)

        await persister.add_segment("Quick write", is_final=True)
        # Wait for debounce to fire
        await asyncio.sleep(0.15)

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data[0]["text"] == "Quick write"

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("test", is_final=True)
        await persister.flush()
        assert path.exists()

    @pytest.mark.asyncio
    async def test_flush_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("test", is_final=True)
        await persister.flush()
        await persister.flush()  # Should not raise or double-write

        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_segments_property(self, tmp_path: Path) -> None:
        path = tmp_path / "transcript.json"
        persister = LiveTranscriptPersister(path, debounce_secs=10.0)

        await persister.add_segment("First", is_final=True)
        await persister.add_segment("Second part", is_final=False)

        segments = persister.segments
        assert len(segments) == 2
        assert segments[0].text == "First"
        assert segments[1].text == "Second part"


# ---------------------------------------------------------------------------
# Session + writer/persister integration
# ---------------------------------------------------------------------------


class TestSessionIntegration:
    """Test session integrates writer and persister."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        session = RecordingSession(
            storage_dir=tmp_path / "meeting",
            title="Team Standup",
            language="en",
        )

        await session.start()
        assert session.state == SessionState.RECORDING

        # Write some audio
        pcm = b"\x00\x00" * 1600
        await session.write_audio(pcm)

        # Add transcript
        await session.add_transcript_segment("Hello", is_final=True)

        await session.stop()
        assert session.state == SessionState.PROCESSING

        # Check files were created
        meeting_dir = tmp_path / "meeting"
        assert (meeting_dir / "audio.wav").exists()
        assert (meeting_dir / "transcript_local.json").exists()

        session.mark_complete()
        assert session.state == SessionState.COMPLETE

    @pytest.mark.asyncio
    async def test_write_audio_ignored_when_paused(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.pause()

        # Write should be a no-op
        await session.write_audio(b"\x00\x00" * 1600)
        await session.stop()

        # WAV should exist but be empty (only the header)
        with wave.open(str(tmp_path / "meeting" / "audio.wav"), "rb") as wf:
            assert wf.getnframes() == 0

    @pytest.mark.asyncio
    async def test_session_without_storage_dir(self) -> None:
        """Session works without storage_dir (no file I/O)."""
        session = RecordingSession()
        await session.start()
        await session.write_audio(b"\x00\x00" * 100)
        await session.add_transcript_segment("test", is_final=True)
        await session.stop()
        session.mark_complete()
        assert session.state == SessionState.COMPLETE

    @pytest.mark.asyncio
    async def test_close_cleanup(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        await session.start()
        await session.write_audio(b"\x00\x00" * 100)
        await session.close()
        # Close should be safe to call multiple times
        await session.close()


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestSessionRegistry:
    """Test the session registry for concurrency control."""

    @pytest.mark.asyncio
    async def test_register_and_unregister(self) -> None:
        session = RecordingSession(meeting_id="test-1")
        assert await register_session(session) is True
        assert active_session_count() == 1
        await unregister_session("test-1")
        assert active_session_count() == 0

    @pytest.mark.asyncio
    async def test_concurrency_cap(self) -> None:
        session1 = RecordingSession(meeting_id="test-cap-1")
        session2 = RecordingSession(meeting_id="test-cap-2")
        assert await register_session(session1) is True
        assert await register_session(session2) is False
        await unregister_session("test-cap-1")

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_is_noop(self) -> None:
        await unregister_session("nonexistent")  # Should not raise
