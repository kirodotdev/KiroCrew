"""Tests for ``transcribe_audio`` across its providers.

Covers the dispatch to the local recogniser, Apple speech and AWS Transcribe;
the AWS provider's unsupported-format refusal, profile credential resolver and
ownership of its temporary ogg file on every exit; and the authenticated
ffmpeg handle that the conversions keep open until the spawned child exits.
``TestTranscribeAudio`` and ``TestStagedFfmpegOutlivesSpawn`` record
Transcribe consent through ``TestTranscribeAwsTempOwnership._grant_consent``,
so the three classes share this module.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from transcribe_helpers import (
    _ramp_int16,
    _write_wav,
)

from kiro_crew import stt, transcribe
from kiro_crew.config.loader import SttConfig
from kiro_crew.transcribe import _ProfileCredentialResolver, transcribe_audio

# ---------------------------------------------------------------------------
# transcribe_audio
# ---------------------------------------------------------------------------


class TestTranscribeAudio:
    @staticmethod
    def _recogniser(monkeypatch, transcript="Hello world", available=True, seen=None):
        """Stand the recogniser up at the ``kiro_crew.stt`` seam.

        Patched on the package namespace ``transcribe.py`` actually reads, so the
        lazy re-export cannot route a call past the stub. *seen* collects the
        arguments the local branch passed, for the tests that pin them.
        """
        if available:
            probed = stt.Availability(True)
        else:
            probed = stt.Availability(False, stt.CODE_EXTRA_MISSING, "no voice extra")
        monkeypatch.setattr(stt, "availability", lambda: probed)

        async def fake_transcribe_pcm(pcm, **kwargs):
            if seen is not None:
                seen["pcm"] = pcm
                seen.update(kwargs)
            return transcript, stt.Availability(True)

        monkeypatch.setattr(stt, "transcribe_pcm", fake_transcribe_pcm)

    @staticmethod
    def _watch_transcode(monkeypatch, calls: list) -> None:
        """Record any fall-through to ffmpeg instead of running one."""

        async def _record(audio_path, timeout_secs):
            calls.append(audio_path)
            return None

        monkeypatch.setattr(transcribe, "_pcm_via_ffmpeg", _record)

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        cfg = SttConfig(enabled=False)
        result = await transcribe_audio("/tmp/test.webm", cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_unavailable_recogniser_returns_none(self, tmp_path, monkeypatch):
        """No exception, ever: eight channel adapters turn None into a visible
        "transcription failed" note, whereas an exception becomes a log line
        nobody reads and a turn that never starts."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local")
        decoded: list[str] = []
        self._recogniser(monkeypatch, available=False)
        self._watch_transcode(monkeypatch, decoded)

        def _never(path):
            raise AssertionError("audio must not be decoded when the engine is absent")

        monkeypatch.setattr(transcribe, "_pcm_from_wav", _never)
        assert await transcribe_audio(str(audio), cfg) is None
        assert decoded == []

    @pytest.mark.asyncio
    async def test_local_availability_probe_runs_off_event_loop(self, tmp_path, monkeypatch):
        """The first probe links the recogniser's native extension, and this
        coroutine is awaited from the Slack path and the transcribe endpoint, so
        it must not land on the loop."""
        from threading import get_ident

        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local")
        loop_thread = get_ident()
        probe_threads: list[int] = []

        def probe():
            probe_threads.append(get_ident())
            return stt.Availability(False, stt.CODE_EXTRA_MISSING, "no voice extra")

        monkeypatch.setattr(stt, "availability", probe)

        result = await transcribe_audio(str(audio), cfg)

        assert result is None
        assert probe_threads
        assert probe_threads[0] != loop_thread

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("language", "expected_locale"),
        [("auto", "en-US"), ("en-US", "en-US"), ("zh-CN", "zh-CN")],
    )
    async def test_aws_audio_read_runs_off_event_loop(
        self, tmp_path, monkeypatch, language, expected_locale
    ):
        from threading import get_ident

        from kiro_crew import transcribe as tr

        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake audio")
        cfg = SttConfig(
            enabled=True, provider="transcribe", language_code=language, timeout_secs=10
        )
        # Transcribe is a paid service and `_transcribe_aws` refuses without a
        # recorded consent for this profile+region, so this case -- which is
        # about WHERE the read runs, not about the gate -- consents first. The
        # refusal itself is covered in `test_aws_consent.py`.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        from kiro_crew import aws_consent
        from kiro_crew.config.loader import config_dir

        config_dir().mkdir(parents=True, exist_ok=True)
        aws_consent.record_grant(
            aws_consent.SERVICE_TRANSCRIBE,
            profile=cfg.transcribe_profile,
            region=cfg.transcribe_region,
            account="111122223333",
            arn="arn:aws:iam::111122223333:user/test",
            granted_at="2026-08-21T00:00:00+00:00",
        )

        # The gate also verifies the LIVE account, which would spawn the AWS CLI.
        # This case is about WHERE the read runs, so return a matching identity.
        async def _probe(_profile, _region, *, use_cache=True):
            return aws_consent.Identity(ok=True, account="111122223333")

        monkeypatch.setattr(aws_consent, "probe_identity", _probe)
        loop_thread = get_ident()
        read_threads = []

        def read_audio(path):
            assert path == str(audio)
            read_threads.append(get_ident())
            return b"fake audio"

        input_stream = SimpleNamespace(
            send_audio_event=AsyncMock(),
            end_stream=AsyncMock(),
        )
        stream = SimpleNamespace(input_stream=input_stream, output_stream=object())
        started = {}

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def start_stream_transcription(self, **kwargs):
                started.update(kwargs)
                return stream

        class FakeHandler:
            def __init__(self, output_stream, transcript_parts):
                pass

            async def handle_events(self):
                pass

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_read_audio_bytes", read_audio)
        monkeypatch.setattr(
            tr,
            "_load_aws_transcribe_components",
            lambda: (FakeClient, FakeHandler),
        )

        result = await tr._transcribe_aws(str(audio), cfg)

        # The fake handler collects no results, so this is a stream that
        # completed hearing nothing: a success with an empty transcript.
        assert result == ""
        assert read_threads
        assert read_threads[0] != loop_thread
        assert started["language_code"] == expected_locale
        assert cfg.language_code == language

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("language", "expected_locale"),
        [("auto", "en-US"), ("en-US", "en-US"), ("zh-CN", "zh-CN")],
    )
    async def test_apple_batch_uses_effective_locale(
        self, tmp_path, monkeypatch, language, expected_locale
    ):
        from kiro_crew import apple_speech

        cfg = SttConfig(provider="apple", language_code=language, timeout_secs=10)
        recognize = AsyncMock(return_value=("heard", {}))
        monkeypatch.setattr(apple_speech, "transcribe", recognize)
        audio_path = str(tmp_path / "voice.wav")

        assert await transcribe._transcribe_apple(audio_path, cfg) == "heard"

        recognize.assert_awaited_once_with(audio_path, locale=expected_locale, timeout_secs=10)
        assert cfg.language_code == language

    @pytest.mark.asyncio
    async def test_apple_no_speech_and_failure_stay_distinct(self, tmp_path, monkeypatch):
        """``apple_speech.transcribe`` answers ``("", metrics)`` for a clean run
        that heard nothing and ``(None, {"error": ...})`` for a failure; the
        provider boundary passes the first through as ``""`` and only the second
        as None."""
        from kiro_crew import apple_speech

        cfg = SttConfig(provider="apple", timeout_secs=10)
        audio_path = str(tmp_path / "voice.wav")

        monkeypatch.setattr(
            apple_speech, "transcribe", AsyncMock(return_value=("", {"audio_secs": 1.0}))
        )
        assert await transcribe._transcribe_apple(audio_path, cfg) == ""

        monkeypatch.setattr(
            apple_speech, "transcribe", AsyncMock(return_value=(None, {"error": "timed out"}))
        )
        assert await transcribe._transcribe_apple(audio_path, cfg) is None

    @pytest.mark.asyncio
    async def test_aws_no_speech_and_failure_stay_distinct(self, tmp_path, monkeypatch):
        """A Transcribe stream that completes with no final results is ``""``;
        a stream that raises is None. The join-and-strip must not fold the first
        onto the second."""
        from kiro_crew import transcribe as tr

        audio = tmp_path / "quiet.ogg"
        audio.write_bytes(b"fake audio")
        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        TestTranscribeAwsTempOwnership._grant_consent(tmp_path, monkeypatch, cfg)

        stream = SimpleNamespace(
            input_stream=SimpleNamespace(send_audio_event=AsyncMock(), end_stream=AsyncMock()),
            output_stream=object(),
        )

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def start_stream_transcription(self, **kwargs):
                return stream

        class SilentHandler:
            def __init__(self, output_stream, transcript_parts):
                pass

            async def handle_events(self):
                pass  # no final results: the stream completed hearing nothing

        class BrokenHandler(SilentHandler):
            async def handle_events(self):
                raise RuntimeError("stream reset by peer")

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_read_audio_bytes", lambda path: b"fake audio")

        monkeypatch.setattr(
            tr, "_load_aws_transcribe_components", lambda: (FakeClient, SilentHandler)
        )
        assert await tr._transcribe_aws(str(audio), cfg) == ""

        monkeypatch.setattr(
            tr, "_load_aws_transcribe_components", lambda: (FakeClient, BrokenHandler)
        )
        assert await tr._transcribe_aws(str(audio), cfg) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("vocabulary", "expected"), [("team-terms", "team-terms"), ("", None)])
    async def test_aws_batch_sends_the_configured_vocabulary(
        self, tmp_path, monkeypatch, vocabulary, expected
    ):
        """A voice memo hears the same names live dictation does: both Transcribe
        paths read the one setting, and None (not ``""``) means no header at all."""
        from kiro_crew import transcribe as tr

        audio = tmp_path / "memo.ogg"
        audio.write_bytes(b"fake audio")
        cfg = SttConfig(
            enabled=True, provider="transcribe", timeout_secs=10, transcribe_vocabulary=vocabulary
        )
        TestTranscribeAwsTempOwnership._grant_consent(tmp_path, monkeypatch, cfg)
        started: dict = {}
        stream = SimpleNamespace(
            input_stream=SimpleNamespace(send_audio_event=AsyncMock(), end_stream=AsyncMock()),
            output_stream=object(),
        )

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def start_stream_transcription(self, **kwargs):
                started.update(kwargs)
                return stream

        class SilentHandler:
            def __init__(self, output_stream, transcript_parts):
                pass

            async def handle_events(self):
                pass

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_read_audio_bytes", lambda path: b"fake audio")
        monkeypatch.setattr(
            tr, "_load_aws_transcribe_components", lambda: (FakeClient, SilentHandler)
        )

        assert await tr._transcribe_aws(str(audio), cfg) == ""
        assert started["vocabulary_name"] == expected

    @pytest.mark.asyncio
    async def test_local_wav_decode_runs_off_event_loop(self, tmp_path, monkeypatch):
        """Reading and converting the WAV is file I/O plus an array copy, so it
        belongs off the loop like every other blocking step on this path."""
        from threading import get_ident

        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        loop_thread = get_ident()
        io_threads: list[int] = []
        real_pcm_from_wav = transcribe._pcm_from_wav

        def decode(path):
            io_threads.append(get_ident())
            return real_pcm_from_wav(path)

        self._recogniser(monkeypatch)
        monkeypatch.setattr(transcribe, "_pcm_from_wav", decode)

        result = await transcribe_audio(str(audio), cfg)

        # Asserted together: a decode that never ran would also satisfy the
        # thread check, so the transcript is what proves the hop was the real one.
        assert result == "Hello world"
        assert io_threads
        assert all(thread != loop_thread for thread in io_threads)

    @pytest.mark.asyncio
    async def test_successful_transcription(self, tmp_path, monkeypatch):
        """A 16 kHz mono WAV reaches the recogniser with no external tool at all,
        carrying the operator's model, language and both bounds.

        The bounds are passed on every call because the recogniser is a singleton:
        they are re-applied to the live instance rather than fixed by whichever
        surface reached it first.
        """
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(
            enabled=True,
            provider="local",
            model="small",
            language_code="fr-FR",
            idle_evict_secs=42,
            timeout_secs=10,
        )
        seen: dict = {}
        transcoded: list[str] = []
        self._recogniser(monkeypatch, transcript="Bonjour le monde", seen=seen)
        self._watch_transcode(monkeypatch, transcoded)

        result = await transcribe_audio(str(audio), cfg)

        assert result == "Bonjour le monde"
        assert transcoded == []
        assert seen["model_name"] == "small"
        # The configured locale is reduced to what whisper names its languages by.
        assert seen["language"] == "fr"
        assert seen["idle_evict_secs"] == 42
        assert seen["timeout_secs"] == 10
        assert seen["pcm"].dtype == np.float32
        assert seen["pcm"].size == 1600

    @pytest.mark.asyncio
    async def test_recogniser_failure_returns_none(self, tmp_path, monkeypatch):
        """A decode that could not run (an undownloaded model, a wedged native
        call) is the same contract as every other failure here: None."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        monkeypatch.setattr(stt, "availability", lambda: stt.Availability(True))

        async def failing_transcribe_pcm(pcm, **kwargs):
            return "", stt.Availability(False, stt.CODE_MODEL_MISSING, "not downloaded")

        monkeypatch.setattr(stt, "transcribe_pcm", failing_transcribe_pcm)
        assert await transcribe_audio(str(audio), cfg) is None

    @pytest.mark.asyncio
    async def test_boilerplate_only_transcript_is_an_empty_string_not_none(
        self, tmp_path, monkeypatch
    ):
        """The hallucination filter can empty a transcript that was entirely
        caption boilerplate. That is a SUCCESSFUL decode with nothing to write
        down, so it must stay ``""``: the transcribe endpoint answers ``""`` with
        a 200 and None with a 500, and a quiet memo collapsed onto None would
        report a broken backend for every silent recording."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        self._recogniser(monkeypatch, transcript="")
        result = await transcribe_audio(str(audio), cfg)
        assert result == ""
        assert result is not None

    @pytest.mark.asyncio
    async def test_local_no_speech_and_failure_stay_distinct(self, tmp_path, monkeypatch):
        """The recogniser reports ``("", ok)`` for silence and ``("", not ok)``
        for a failed decode; the provider boundary must not fold them together."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        monkeypatch.setattr(stt, "availability", lambda: stt.Availability(True))

        async def silent(pcm, **kwargs):
            return "", stt.Availability(True)

        monkeypatch.setattr(stt, "transcribe_pcm", silent)
        assert await transcribe._transcribe_local(str(audio), cfg) == ""

        async def failed(pcm, **kwargs):
            return "", stt.Availability(False, stt.CODE_MODEL_MISSING, "not downloaded")

        monkeypatch.setattr(stt, "transcribe_pcm", failed)
        assert await transcribe._transcribe_local(str(audio), cfg) is None

    @pytest.mark.asyncio
    async def test_transcode_targets_the_recogniser_format(self, tmp_path, monkeypatch):
        """A Slack voice memo arrives as ogg/Opus, which the stdlib cannot read.

        The recogniser accepts exactly one format, so the transcode names it
        directly rather than leaving a rate or channel conversion for later, and
        the duration cap bounds the temp file as well as the later read: a
        container that decodes forever must not fill the disk while it does.
        """
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS fake")
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        seen: dict = {}
        self._recogniser(monkeypatch, transcript="Salut", seen=seen)
        owned = tmp_path / "owned.wav"
        monkeypatch.setattr(transcribe, "_make_temp_wav", lambda: str(owned))
        captured: list = []

        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))

        async def fake_exec(*args, **kwargs):
            captured.extend(args)
            # A real ffmpeg writes the transcode here, and the decode reads it
            # back, so the whole hand-off is exercised rather than stubbed out.
            _write_wav(owned, _ramp_int16(800))
            return proc

        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = await transcribe_audio(str(audio), cfg)

        assert result == "Salut"
        assert seen["pcm"].size == 800
        assert captured[0] == "/fake/ffmpeg"
        assert str(audio) in captured
        for flag, value in (
            ("-ar", str(stt.SAMPLE_RATE_HZ)),
            ("-ac", "1"),
            ("-c:a", "pcm_s16le"),
            ("-t", str(transcribe._MAX_AUDIO_SECS)),
        ):
            assert captured[captured.index(flag) + 1] == value
        # Owned by the transcode: removed on the success path too.
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_missing_ffmpeg_for_a_compressed_memo_returns_none(self, tmp_path, monkeypatch):
        """ffmpeg is a prerequisite, not a fallback: without it a compressed memo
        cannot be decoded at all, and that is a None rather than a crash."""
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS fake")
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        self._recogniser(monkeypatch)

        async def _never(*args, **kwargs):
            raise AssertionError("no child may be spawned without an ffmpeg binary")

        with (
            patch("kiro_crew.transcribe._open_ffmpeg_for_execution", return_value=None),
            patch("asyncio.create_subprocess_exec", side_effect=_never),
        ):
            assert await transcribe_audio(str(audio), cfg) is None

    @pytest.mark.asyncio
    async def test_ffmpeg_timeout_reaps_the_child_and_returns_none(self, tmp_path, monkeypatch):
        """A container ffmpeg decodes forever must not hold the request open, and
        the killed child must be REAPED with ``communicate()`` so a full stderr
        pipe cannot deadlock the cleanup."""
        audio = tmp_path / "voice.webm"
        audio.write_bytes(b"not really webm")
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=1)
        monkeypatch.setattr(stt, "availability", lambda: stt.Availability(True))
        events: list[str] = []
        owned = tmp_path / "owned.wav"
        owned.write_bytes(b"")
        monkeypatch.setattr(transcribe, "_make_temp_wav", lambda: str(owned))

        class _Proc:
            returncode = -9

            def __init__(self):
                self._calls = 0

            async def communicate(self):
                self._calls += 1
                if self._calls == 1:
                    raise asyncio.TimeoutError
                events.append("reaped")
                return b"", b""

            def kill(self):
                events.append("killed")

            async def wait(self):
                raise AssertionError("reap via communicate(), never wait()")

        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=_Proc()),
        ):
            result = await transcribe_audio(str(audio), cfg)

        assert result is None
        assert events == ["killed", "reaped"]
        # The temp WAV is owned by the transcode: every exit removes it.
        assert not owned.exists()
        assert audio.exists()

    @pytest.mark.asyncio
    async def test_cancelled_transcode_reaps_its_child_before_removing_its_temp(
        self, tmp_path, monkeypatch
    ):
        """An abandoned request must not leave an ffmpeg child behind.

        ``CancelledError`` is a ``BaseException``, so the timeout arm never sees it.
        The child is stopped AND reaped before the temp is removed, because Windows
        keeps the output file locked until the child fully exits and on POSIX a live
        child can race the removal. The cancellation itself is what must reach the
        awaiter, not a cleanup error.
        """
        audio = tmp_path / "voice.ogg"
        audio.write_bytes(b"OggS fake")
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        monkeypatch.setattr(stt, "availability", lambda: stt.Availability(True))
        events: list[str] = []
        owned = tmp_path / "owned.wav"
        owned.write_bytes(b"")
        monkeypatch.setattr(transcribe, "_make_temp_wav", lambda: str(owned))
        real_unlink = transcribe._unlink_if_exists

        def tracked_unlink(path):
            if str(path) == str(owned):
                events.append("unlinked")
            return real_unlink(path)

        monkeypatch.setattr(transcribe, "_unlink_if_exists", tracked_unlink)

        class _Proc:
            def __init__(self):
                self._calls = 0

            async def communicate(self):
                self._calls += 1
                if self._calls == 1:
                    raise asyncio.CancelledError()
                events.append("reaped")
                return b"", b""

            def kill(self):
                events.append("killed")

        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=_Proc()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await transcribe_audio(str(audio), cfg)

        assert events == ["killed", "reaped", "unlinked"]
        assert not owned.exists()
        assert audio.exists()

    @pytest.mark.asyncio
    async def test_undecodable_audio_returns_none(self, tmp_path, monkeypatch):
        """A WAV the reader accepts but that carries no samples never reaches the
        recogniser: an empty buffer would be reported as a silent success."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, np.zeros(0, dtype=np.int16))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        monkeypatch.setattr(stt, "availability", lambda: stt.Availability(True))

        async def _never(pcm, **kwargs):
            raise AssertionError("an empty buffer must not reach the recogniser")

        monkeypatch.setattr(stt, "transcribe_pcm", _never)
        assert await transcribe_audio(str(audio), cfg) is None

    @pytest.mark.asyncio
    async def test_loads_config_when_none(self):
        mock_cfg = MagicMock()
        mock_cfg.stt = SttConfig(enabled=False)
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_cfg):
            result = await transcribe_audio("/tmp/test.webm", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_retired_provider_transcribes_on_the_local_floor(self, tmp_path, monkeypatch):
        """``local`` is the floor, and reaching the dispatch with a retired value
        transcribes rather than raising.

        The config loader normally degrades such a value on load, so this is the
        hand-edited-config case: it must cost the user a different engine, not a
        dead voice path.
        """
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="mlx", timeout_secs=10)
        self._recogniser(monkeypatch, transcript="Hola mundo")

        assert await transcribe_audio(str(audio), cfg) == "Hola mundo"


# ---------------------------------------------------------------------------
# Unsupported format rejection for Transcribe
# ---------------------------------------------------------------------------


class TestTranscribeFormatValidation:
    @pytest.mark.asyncio
    async def test_rejects_unsupported_format(self, tmp_path):
        audio = tmp_path / "test.mp3"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="transcribe")
        with patch("kiro_crew.security.is_sensitive_path", return_value=False):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None


# ---------------------------------------------------------------------------
# _ProfileCredentialResolver null check (Fix #4)
# ---------------------------------------------------------------------------


class TestProfileCredentialResolver:
    @staticmethod
    def _resolver(session: MagicMock) -> _ProfileCredentialResolver:
        with patch.object(transcribe, "boto3") as boto3_module:
            boto3_module.Session.return_value = session
            return _ProfileCredentialResolver("test-profile")

    @pytest.mark.asyncio
    async def test_none_credentials_raises(self):
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None
        mock_session.profile_name = "test-profile"
        resolver = self._resolver(mock_session)
        with pytest.raises(RuntimeError, match="No AWS credentials found"):
            await resolver.get_credentials()

    @pytest.mark.asyncio
    async def test_a_stream_keeps_the_credentials_it_opened_with(self):
        """A refresh mid-stream must not reach the events still to be signed.

        The SDK asks the resolver again for every audio event, and each event's
        signature chains from the opening request's, so Transcribe refuses an
        event signed with a refreshed key.
        """
        pytest.importorskip("amazon_transcribe")
        botocore_credentials = pytest.importorskip("botocore.credentials")
        now = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
        # Inside botocore's advisory refresh window, so every read refreshes.
        expiry = now + timedelta(minutes=12)
        issued = iter(range(2, 100))

        def _refresh() -> dict[str, str]:
            n = next(issued)
            return {
                "access_key": f"AKIA{n}",
                "secret_key": f"secret-{n}",
                "token": f"token-{n}",
                "expiry_time": expiry.isoformat(),
            }

        refreshing = botocore_credentials.RefreshableCredentials(
            access_key="AKIA1",
            secret_key="secret-1",
            token="token-1",
            expiry_time=expiry,
            refresh_using=_refresh,
            method="test",
            time_fetcher=lambda: now,
        )
        session = MagicMock()
        session.get_credentials.return_value = refreshing
        resolver = self._resolver(session)

        opening = await resolver.get_credentials()
        later = [await resolver.get_credentials() for _ in range(3)]

        assert refreshing.get_frozen_credentials().access_key != opening.access_key_id
        assert {(c.access_key_id, c.secret_access_key, c.session_token) for c in later} == {
            (opening.access_key_id, opening.secret_access_key, opening.session_token)
        }
        session.get_credentials.assert_called_once()


# ---------------------------------------------------------------------------
# AWS Transcribe temp-file ownership
# ---------------------------------------------------------------------------


class TestTranscribeAwsTempOwnership:
    """``_transcribe_aws`` owns ``tmp_ogg`` until every exit removes it.

    The webm→ogg remux creates the temp with ``_make_temp_ogg``; a cancellation
    (``CancelledError`` is a ``BaseException``, so an ``except Exception`` guard
    misses it) must kill AND reap the ffmpeg child before the unlink — Windows
    keeps the output file locked until the child fully exits — and then let the
    cancellation propagate. Reference pattern:
    ``test_apple_speech.py::TestTranscodeTempOwnership``.
    """

    @staticmethod
    def _grant_consent(tmp_path, monkeypatch, cfg):
        """Record Transcribe consent so the paid-service gate lets tests pass."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        from kiro_crew import aws_consent
        from kiro_crew.config.loader import config_dir

        config_dir().mkdir(parents=True, exist_ok=True)
        aws_consent.record_grant(
            aws_consent.SERVICE_TRANSCRIBE,
            profile=cfg.transcribe_profile,
            region=cfg.transcribe_region,
            account="111122223333",
            arn="arn:aws:iam::111122223333:user/test",
            granted_at="2026-08-21T00:00:00+00:00",
        )

        async def _probe(_profile, _region, *, use_cache=True):
            return aws_consent.Identity(ok=True, account="111122223333")

        monkeypatch.setattr(aws_consent, "probe_identity", _probe)

    @staticmethod
    def _owned_temp(tmp_path, monkeypatch):
        """Pin ``_make_temp_ogg`` to a known file so the tests can watch it."""
        from kiro_crew import transcribe as tr

        owned = tmp_path / "owned.ogg"
        owned.write_bytes(b"")
        monkeypatch.setattr(tr, "_make_temp_ogg", lambda: str(owned))
        return owned

    @pytest.mark.asyncio
    async def test_cancellation_reaps_ffmpeg_before_removing_the_owned_temp(
        self, tmp_path, monkeypatch
    ):
        """A cancellation mid-``communicate`` must kill the child, reap it, THEN
        remove ``tmp_ogg``, and re-raise — an ``except Exception`` guard would
        miss the ``CancelledError`` and do none of that."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")
        events: list[str] = []

        class _Proc:
            def __init__(self):
                self._calls = 0

            async def communicate(self):
                self._calls += 1
                if self._calls == 1:
                    raise asyncio.CancelledError()
                events.append("reaped")
                return b"", b""

            def kill(self):
                events.append("killed")

        real_unlink = tr._unlink_if_exists

        def tracked_unlink(path):
            if str(path) == str(owned):
                events.append("unlinked")
            return real_unlink(path)

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_load_aws_transcribe_components", lambda: (object, object))
        monkeypatch.setattr(tr, "_unlink_if_exists", tracked_unlink)
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=_Proc()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tr._transcribe_aws(str(src), cfg)
        assert events == ["killed", "reaped", "unlinked"]
        assert not owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_remux_maps_audio_only(self, tmp_path, monkeypatch):
        """The webm→ogg remux must drop video: a Slack ``video/webm`` clip has a
        video track, and the audio-only decoder has no theora encoder for ogg's
        default video stream, so mapping it fails the remux outright."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")
        captured: list = []

        class _Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"stop here"

            def kill(self):
                pass

        async def fake_exec(*args, **_kwargs):
            captured.extend(args)
            return _Proc()

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_load_aws_transcribe_components", lambda: (object, object))
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            assert await tr._transcribe_aws(str(src), cfg) is None
        assert str(owned) in captured
        assert "-vn" in captured
        assert captured.index("-vn") < captured.index(str(owned))
        assert captured[captured.index("-c:a") + 1] == "copy"

    @pytest.mark.asyncio
    async def test_remux_failure_still_unlinks_and_returns_none(self, tmp_path, monkeypatch):
        """The new cancellation path must not eat the established ``Exception``
        contract: a failed remux logs, removes the temp, and returns None."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 1
        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_load_aws_transcribe_components", lambda: (object, object))
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=proc),
        ):
            result = await tr._transcribe_aws(str(src), cfg)
        assert result is None
        assert not owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_repeat_cancellation_in_stream_cleanup_still_unlinks(self, tmp_path, monkeypatch):
        """A REPEAT cancellation landing on the cleanup ``end_stream`` await
        escapes its ``except Exception`` guard; the nested ``finally`` must
        still remove ``tmp_ogg`` and let the cancellation propagate."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        remux_proc = AsyncMock()
        remux_proc.communicate = AsyncMock(return_value=(b"", b""))
        remux_proc.returncode = 0

        input_stream = SimpleNamespace(
            # First cancellation: aborts the streaming phase from inside the
            # ``try``. Second: lands on the cleanup ``end_stream`` in ``finally``.
            send_audio_event=AsyncMock(side_effect=asyncio.CancelledError()),
            end_stream=AsyncMock(side_effect=asyncio.CancelledError()),
        )
        stream = SimpleNamespace(input_stream=input_stream, output_stream=object())

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def start_stream_transcription(self, **kwargs):
                return stream

        class FakeHandler:
            def __init__(self, output_stream, transcript_parts):
                pass

            async def handle_events(self):
                pass

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_read_audio_bytes", lambda path: b"fake audio")
        monkeypatch.setattr(
            tr, "_load_aws_transcribe_components", lambda: (FakeClient, FakeHandler)
        )
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=remux_proc),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tr._transcribe_aws(str(src), cfg)
        assert not owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_repeat_cancellation_and_locked_file_keep_the_cancellation(
        self, tmp_path, monkeypatch
    ):
        """Worst case on Windows: a repeat cancellation interrupts the reap, so
        the child may still hold the file and the unlink raises
        ``PermissionError``. That must not REPLACE the in-flight cancellation
        — the guard swallows the ``OSError`` and the original propagates."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")
        events: list[str] = []

        class _Proc:
            async def communicate(self):
                # First call: the cancellation under test. Second call (the
                # reap): a REPEAT cancellation lands on the cleanup await.
                raise asyncio.CancelledError()

            def kill(self):
                events.append("killed")

        def locked_unlink(path):
            events.append("unlink_attempted")
            raise PermissionError("file is locked by the child")

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_load_aws_transcribe_components", lambda: (object, object))
        monkeypatch.setattr(tr, "_unlink_if_exists", locked_unlink)
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=_Proc()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tr._transcribe_aws(str(src), cfg)
        assert events == ["killed", "unlink_attempted"]
        # The locked unlink never removed the file — the guarantee under test
        # is exception identity, not removal.
        assert owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_cancellation_during_spawn_still_removes_the_owned_temp(
        self, tmp_path, monkeypatch
    ):
        """A cancellation landing on ``create_subprocess_exec`` itself means no
        child exists — the owned temp must still be removed and the
        cancellation must propagate."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_load_aws_transcribe_components", lambda: (object, object))
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=asyncio.CancelledError(),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tr._transcribe_aws(str(src), cfg)
        assert not owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_stream_cleanup_sync_fallback_swallows_locked_file(self, tmp_path, monkeypatch):
        """When the off-loop unlink hop is itself cancelled AND the synchronous
        fallback hits a locked file, the ``OSError`` must be swallowed so the
        cancellation — not a ``PermissionError`` — reaches the awaiter."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        remux_proc = AsyncMock()
        remux_proc.communicate = AsyncMock(return_value=(b"", b""))
        remux_proc.returncode = 0

        input_stream = SimpleNamespace(
            send_audio_event=AsyncMock(side_effect=asyncio.CancelledError()),
            end_stream=AsyncMock(),
        )
        stream = SimpleNamespace(input_stream=input_stream, output_stream=object())

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def start_stream_transcription(self, **kwargs):
                return stream

        class FakeHandler:
            def __init__(self, output_stream, transcript_parts):
                pass

            async def handle_events(self):
                pass

        def locked_unlink(path):
            raise PermissionError("file is locked")

        real_to_thread = asyncio.to_thread

        async def cancelled_unlink_hop(func, *args, **kwargs):
            # Simulate a repeat cancellation eating the off-loop hop for the
            # unlink only; every other to_thread call runs normally.
            if func is tr._unlink_if_exists:
                raise asyncio.CancelledError()
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_read_audio_bytes", lambda path: b"fake audio")
        monkeypatch.setattr(
            tr, "_load_aws_transcribe_components", lambda: (FakeClient, FakeHandler)
        )
        monkeypatch.setattr(tr, "_unlink_if_exists", locked_unlink)
        monkeypatch.setattr(asyncio, "to_thread", cancelled_unlink_hop)
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=remux_proc),
        ):
            with pytest.raises(asyncio.CancelledError):
                await tr._transcribe_aws(str(src), cfg)
        # The locked unlink never removed the file — the guarantee under test
        # is exception identity, not removal.
        assert owned.exists()
        assert src.exists()

    @pytest.mark.asyncio
    async def test_timeout_reaps_ffmpeg_via_communicate(self, tmp_path, monkeypatch):
        """When the ffmpeg remux times out, the killed child must be reaped via
        ``communicate()`` -- not ``wait()`` -- so the PIPE buffers are drained
        and a child blocked writing to a full stderr PIPE cannot hang the
        event loop."""
        from kiro_crew import transcribe as tr

        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        self._grant_consent(tmp_path, monkeypatch, cfg)
        owned = self._owned_temp(tmp_path, monkeypatch)
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        proc.returncode = -9

        monkeypatch.setattr(tr, "boto3", object())
        monkeypatch.setattr(tr, "_load_aws_transcribe_components", lambda: (object, object))
        with (
            patch(
                "kiro_crew.transcribe._open_ffmpeg_for_execution",
                return_value="/fake/ffmpeg",
            ),
            patch("asyncio.create_subprocess_exec", return_value=proc),
        ):
            result = await tr._transcribe_aws(str(src), cfg)

        # The timeout is caught by ``except Exception``; returns None.
        assert result is None
        proc.kill.assert_called_once()
        # The critical pin: reap via communicate(), not wait(). The remux
        # call itself awaits communicate once; the reap must award a SECOND
        # await, and wait() must never be touched.
        assert proc.communicate.await_count == 2
        proc.wait.assert_not_awaited()
        assert not owned.exists()
        assert src.exists()


class TestStagedFfmpegOutlivesSpawn:
    """The authenticated handle outlives the spawn.

    On macOS the system policy assessment resolves the staged *path*
    asynchronously after ``create_subprocess_exec`` returns; closing the handle
    removes the staged directory, so the kernel denied the exec with SIGKILL
    and STT was unconditionally broken on desktop installs. The contract
    pinned here is platform-independent: the handle is never closed before the
    spawned child has exited, and it is closed exactly once on the success,
    timeout, and cancellation paths.
    """

    @staticmethod
    def _recording_handle(tmp_path, monkeypatch, events):
        binary = tmp_path / "staged-ffmpeg"
        binary.write_bytes(b"decoder")
        descriptor = os.open(str(binary), os.O_RDONLY)

        # A subclass rather than a class-level ``close`` monkeypatch: the
        # resolver patch below keeps the handle alive until teardown, and an
        # instance ``__del__`` firing into a half-restored patched class
        # attribute crashes the interpreter.
        class _Recording(transcribe._AuthenticatedFfmpeg):
            def close(self):
                # Only the first effective close counts: ``close`` is
                # idempotent and ``__del__`` re-enters it with the descriptor
                # already surrendered.
                if self.descriptor >= 0:
                    events.append("closed")
                super().close()

        opened = _Recording(str(binary), descriptor, str(binary))
        monkeypatch.setattr(transcribe, "_open_ffmpeg_for_execution", lambda: opened)
        return opened

    @pytest.mark.asyncio
    async def test_success_path_closes_after_child_exit_exactly_once(self, tmp_path, monkeypatch):
        events: list = []
        self._recording_handle(tmp_path, monkeypatch, events)
        owned = tmp_path / "owned.wav"
        monkeypatch.setattr(transcribe, "_make_temp_wav", lambda: str(owned))

        class _Proc:
            returncode = 0

            async def communicate(self):
                assert "closed" not in events, "handle closed before the child exited"
                events.append("exited")
                _write_wav(owned, _ramp_int16(80))
                return b"", b""

        async def fake_exec(*_args, **_kwargs):
            assert "closed" not in events, "handle closed before the spawn returned"
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        result = await transcribe._pcm_via_ffmpeg(str(tmp_path / "memo.webm"), 1)
        assert result is not None
        assert result.size == 80
        assert events == ["exited", "closed"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_timeout_path_closes_after_reap_exactly_once(self, tmp_path, monkeypatch):
        events: list = []
        self._recording_handle(tmp_path, monkeypatch, events)
        owned = tmp_path / "owned.wav"
        owned.write_bytes(b"")
        monkeypatch.setattr(transcribe, "_make_temp_wav", lambda: str(owned))

        class _Proc:
            returncode = -9

            def __init__(self):
                self._calls = 0

            async def communicate(self):
                self._calls += 1
                if self._calls == 1:
                    raise asyncio.TimeoutError
                assert "closed" not in events, "handle closed before the child was reaped"
                events.append("reaped")
                return b"", b""

            def kill(self):
                assert "closed" not in events, "handle closed before the child was killed"
                events.append("killed")

        async def fake_exec(*_args, **_kwargs):
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert await transcribe._pcm_via_ffmpeg(str(tmp_path / "memo.webm"), 1) is None
        assert events == ["killed", "reaped", "closed"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_cancellation_closes_after_reap_exactly_once(self, tmp_path, monkeypatch):
        events: list = []
        self._recording_handle(tmp_path, monkeypatch, events)
        owned = tmp_path / "owned.wav"
        owned.write_bytes(b"")
        monkeypatch.setattr(transcribe, "_make_temp_wav", lambda: str(owned))
        started = asyncio.Event()

        class _Proc:
            returncode = None

            def __init__(self):
                self._calls = 0

            async def communicate(self):
                self._calls += 1
                if self._calls == 1:
                    started.set()
                    # flake-ok: pre-existing hour-long sleep that parks the fake child's first communicate() until the test cancels the conversion, moved verbatim; tracked for a de-flake pass
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(3600)
                assert "closed" not in events, "handle closed before the child was reaped"
                events.append("reaped")
                return b"", b""

            def kill(self):
                events.append("killed")

        async def fake_exec(*_args, **_kwargs):
            return _Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        task = asyncio.create_task(transcribe._pcm_via_ffmpeg(str(tmp_path / "memo.webm"), 60))
        # flake-ok: pre-existing Event wait with no timeout that blocks until the fake child's first communicate sets the started signal, moved verbatim; tracked for a de-flake pass
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # flake-ok: pre-existing await on the cancelled transcode task with no timeout, waiting for the kill and reap cleanup to finish, moved verbatim; tracked for a de-flake pass
            await task
        assert events == ["killed", "reaped", "closed"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_remux_path_closes_after_child_exit_exactly_once(self, tmp_path, monkeypatch):
        """The webm→ogg remux call site owns the same deferred close: the
        handle survives until the child has exited, then is released exactly
        once even when the remux itself fails."""
        events: list = []
        self._recording_handle(tmp_path, monkeypatch, events)
        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        TestTranscribeAwsTempOwnership._grant_consent(tmp_path, monkeypatch, cfg)
        owned = tmp_path / "owned.ogg"
        owned.write_bytes(b"")
        monkeypatch.setattr(transcribe, "_make_temp_ogg", lambda: str(owned))
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        class _Proc:
            # A nonzero exit keeps the test inside the remux block: the child
            # HAS exited when the error path runs, so the close must follow it.
            returncode = 1

            async def communicate(self):
                assert "closed" not in events, "handle closed before the child exited"
                events.append("exited")
                return b"", b""

        async def fake_exec(*_args, **_kwargs):
            assert "closed" not in events, "handle closed before the spawn returned"
            return _Proc()

        monkeypatch.setattr(transcribe, "boto3", object())
        monkeypatch.setattr(transcribe, "_load_aws_transcribe_components", lambda: (object, object))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert await transcribe._transcribe_aws(str(src), cfg) is None
        assert events == ["exited", "closed"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_remux_cancellation_on_the_close_await_still_removes_the_temp(
        self, tmp_path, monkeypatch
    ):
        """A cancellation landing exactly on the deferred close await -- the
        only suspension point between the remux child exiting and the temp
        changing owner -- must not propagate with ``tmp_ogg`` still on disk."""
        events: list = []
        self._recording_handle(tmp_path, monkeypatch, events)
        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
        TestTranscribeAwsTempOwnership._grant_consent(tmp_path, monkeypatch, cfg)
        owned = tmp_path / "owned.ogg"
        owned.write_bytes(b"")
        monkeypatch.setattr(transcribe, "_make_temp_ogg", lambda: str(owned))
        src = tmp_path / "voice.webm"
        src.write_bytes(b"data")

        class _Proc:
            returncode = 0

            async def communicate(self):
                events.append("exited")
                return b"", b""

        async def fake_exec(*_args, **_kwargs):
            return _Proc()

        async def cancelled_close(_executable, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(transcribe, "boto3", object())
        monkeypatch.setattr(transcribe, "_load_aws_transcribe_components", lambda: (object, object))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(transcribe, "_close_ffmpeg_for_execution", cancelled_close)
        with pytest.raises(asyncio.CancelledError):
            await transcribe._transcribe_aws(str(src), cfg)
        assert not owned.exists()
        assert src.exists()


class TestTranscribeKeepsNoJobHistory:
    """Settings tells a Transcribe user that nothing is created in their AWS
    account, which holds only while every path streams. A batch job would leave
    a job record, and its output, in the account."""

    def test_no_source_starts_a_transcription_job(self) -> None:
        src = Path(transcribe.__file__).resolve().parent
        offenders = [
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if re.search(
                r"start_transcription_job|StartTranscriptionJob", path.read_text(encoding="utf-8")
            )
        ]
        assert offenders == []
