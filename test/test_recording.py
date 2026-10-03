"""Tests for kiro_crew.recording — state machine and WAV writer.

Validates:
- State machine transitions (valid and invalid)
- WAV writer creates valid WAV files with correct format
- The writer never opens an existing file; a session never reuses a name
- All blocking I/O is offloaded (executor usage)
"""

from __future__ import annotations

import asyncio
import threading
import wave
from pathlib import Path

import pytest

from kiro_crew.recording import WavWriter
from kiro_crew.recording import session as session_mod
from kiro_crew.recording.session import (
    InvalidTransitionError,
    RecordingSession,
    SessionState,
    active_session_count,
    next_audio_path,
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

    @pytest.mark.asyncio
    async def test_close_propagates_header_failure_but_closes_handle(self, tmp_path: Path) -> None:
        """A failed header patch is visible even though the raw handle is closed."""
        writer = WavWriter(tmp_path / "audio.wav")

        class BrokenWave:
            def close(self) -> None:
                raise OSError("disk full")

        class ObservedHandle:
            closed = False

            def close(self) -> None:
                self.closed = True

        handle = ObservedHandle()
        writer._wf = BrokenWave()  # type: ignore[assignment]
        writer._fh = handle  # type: ignore[assignment]

        with pytest.raises(OSError, match="disk full"):
            await writer.close()
        assert handle.closed

    @pytest.mark.asyncio
    async def test_open_refuses_an_existing_file(self, tmp_path: Path) -> None:
        """An existing recording is never truncated: open() fails, bytes stay put.

        This is the regression guard for the second-Record-erases-the-first bug:
        the old ``wave.open(name, "wb")`` silently zeroed whatever was there.
        """
        audio_path = tmp_path / "audio.wav"
        first = WavWriter(audio_path)
        await first.open()
        await first.write(b"\x01\x00" * 16000)
        await first.close()
        before = audio_path.read_bytes()

        second = WavWriter(audio_path)
        with pytest.raises(FileExistsError):
            await second.open()
        # A failed open leaves nothing to close, and closing must still be safe.
        await second.close()

        assert audio_path.read_bytes() == before
        with wave.open(str(audio_path), "rb") as wf:
            assert wf.getnframes() == 16000

    @pytest.mark.asyncio
    async def test_header_is_finalized_on_close(self, tmp_path: Path) -> None:
        """The frame count is patched into the header before the handle closes.

        ``wave`` does not own a file object it was handed, so the writer closes
        the handle itself -- and must do so AFTER ``Wave_write.close`` rewrote
        the header, or the file reads back as zero frames.
        """
        audio_path = tmp_path / "audio.wav"
        writer = WavWriter(audio_path)
        await writer.open()
        await writer.write(b"\x00\x00" * 800)
        await writer.close()
        with wave.open(str(audio_path), "rb") as wf:
            assert wf.getnframes() == 800


class TestNextAudioPath:
    """Each recording in a directory gets its own file."""

    @pytest.mark.asyncio
    async def test_session_probes_next_path_off_event_loop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        event_loop_thread = threading.get_ident()
        called_from: list[int] = []
        real_next_audio_path = next_audio_path

        def _observed(storage_dir: Path) -> Path:
            called_from.append(threading.get_ident())
            return real_next_audio_path(storage_dir)

        monkeypatch.setattr(session_mod, "next_audio_path", _observed)
        session = RecordingSession(storage_dir=tmp_path)
        await session.start()
        await session.stop()

        assert called_from
        assert all(thread_id != event_loop_thread for thread_id in called_from)

    def test_first_recording_is_audio_wav(self, tmp_path: Path) -> None:
        assert next_audio_path(tmp_path) == tmp_path / "audio.wav"

    def test_later_recordings_are_numbered(self, tmp_path: Path) -> None:
        (tmp_path / "audio.wav").write_bytes(b"x")
        assert next_audio_path(tmp_path) == tmp_path / "audio-2.wav"
        (tmp_path / "audio-2.wav").write_bytes(b"x")
        assert next_audio_path(tmp_path) == tmp_path / "audio-3.wav"

    def test_a_gap_is_filled_but_nothing_existing_is_returned(self, tmp_path: Path) -> None:
        (tmp_path / "audio.wav").write_bytes(b"x")
        (tmp_path / "audio-3.wav").write_bytes(b"x")
        chosen = next_audio_path(tmp_path)
        assert chosen == tmp_path / "audio-2.wav"
        assert not chosen.exists()

    def test_an_empty_leftover_still_counts_as_taken(self, tmp_path: Path) -> None:
        """A zero-byte file from a crashed session is stepped over, not reused."""
        (tmp_path / "audio.wav").write_bytes(b"")
        assert next_audio_path(tmp_path) == tmp_path / "audio-2.wav"


# ---------------------------------------------------------------------------
# Session + writer integration
# ---------------------------------------------------------------------------


class TestSessionIntegration:
    """Test session integrates the writer."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        session = RecordingSession(storage_dir=tmp_path / "meeting")
        assert session.audio_path is None

        await session.start()
        assert session.state == SessionState.RECORDING
        assert session.audio_path == tmp_path / "meeting" / "audio.wav"

        # Write some audio
        pcm = b"\x00\x00" * 1600
        await session.write_audio(pcm)

        await session.stop()
        assert session.state == SessionState.PROCESSING

        # Check the file was created
        meeting_dir = tmp_path / "meeting"
        assert (meeting_dir / "audio.wav").exists()

    @pytest.mark.asyncio
    async def test_second_recording_in_the_same_directory_keeps_the_first(
        self, tmp_path: Path
    ) -> None:
        """Record, stop, record again: the first WAV is intact, the second is new.

        Regression test for the bug where every recording of a meeting reopened
        ``audio.wav`` in truncating mode, so recording a second time erased the
        first recording's audio.
        """
        meeting_dir = tmp_path / "meeting"
        first = RecordingSession(storage_dir=meeting_dir)
        await first.start()
        await first.write_audio(b"\x01\x00" * 48000)  # 3.0 s
        await first.stop()
        await first.close()
        assert first.audio_path == meeting_dir / "audio.wav"

        second = RecordingSession(storage_dir=meeting_dir)
        await second.start()
        assert second.audio_path == meeting_dir / "audio-2.wav"
        await second.write_audio(b"\x02\x00" * 8000)  # 0.5 s
        await second.stop()
        await second.close()

        with wave.open(str(meeting_dir / "audio.wav"), "rb") as wf:
            assert wf.getnframes() == 48000
            assert wf.readframes(1) == b"\x01\x00"
        with wave.open(str(meeting_dir / "audio-2.wav"), "rb") as wf:
            assert wf.getnframes() == 8000
            assert wf.readframes(1) == b"\x02\x00"

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
        await session.stop()

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
