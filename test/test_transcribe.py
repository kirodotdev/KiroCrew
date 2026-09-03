"""Tests for speech-to-text transcription of whole audio files.

Three providers reach this module: the resident local recogniser (the default),
Apple's on-device speech, and AWS Transcribe. Everything a test here needs about
the recogniser is stubbed at the ``kiro_crew.stt`` seam, because the point of the
batch path is what it does with an availability answer and a PCM buffer, not how
whisper.cpp decodes one.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import threading
import types
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import imageio_ffmpeg
import numpy as np
import pytest

# The authoritative upstream map of platform key -> shipped ffmpeg filename, plus
# the resolver for which key THIS host is. The pin-completeness test derives the
# expected filename set from the map rather than hand-copying names, so a
# one-character drift fails at test time. imageio-ffmpeg is a dev dependency
# precisely so this import (and the tests) run for real; a `pytest.importorskip`
# here would silently pass and reproduce the very "green while checking nothing"
# shape these tests replace.
from imageio_ffmpeg._definitions import FNAME_PER_PLATFORM, get_platform

from kiro_crew import platform_compat as _pc
from kiro_crew import stt, transcribe
from kiro_crew.config.loader import SttConfig
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.transcribe import (
    _ProfileCredentialResolver,
    availability_detail,
    find_brew,
    is_available,
    transcribe_audio,
)


def _write_wav(
    path,
    samples: np.ndarray,
    *,
    rate: int = 16_000,
    channels: int = 1,
    sampwidth: int = 2,
) -> None:
    """Write *samples* as a RIFF WAV with the stdlib writer.

    Built in the test body rather than committed as a fixture: the whole question
    these tests ask is which (rate, width, channel) combinations the stdlib reader
    accepts, and a binary blob in the repo hides its own header from review.
    """
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(rate)
        wav.writeframes(samples.astype("<i2").tobytes())


def _ramp_int16(count: int) -> np.ndarray:
    """A deterministic int16 signal that is not silence, so a fold is observable."""
    return (np.arange(count, dtype=np.int32) * 37 % 20_000 - 10_000).astype(np.int16)


def _stub_recogniser_importable(monkeypatch) -> None:
    """Make ``engine.probe()`` find a recogniser on a host without the extra.

    ``probe`` asks two separate questions -- is it INSTALLED (`find_spec`) and does it
    IMPORT -- so the stub has to satisfy both. A bare `types.ModuleType` has
    ``__spec__ = None``, which makes `find_spec` raise ``ValueError`` rather than
    report the module present, so each stub carries a real spec.
    """
    for name in ("pywhispercpp", "pywhispercpp.model"):
        module = types.ModuleType(name)
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        monkeypatch.setitem(sys.modules, name, module)


def _stub_recogniser_absent(monkeypatch) -> None:
    """Make the recogniser look NOT INSTALLED, which is what `find_spec` answers.

    Absence has to be simulated at the finder rather than by making the import raise.
    Those are different states with different next actions, and conflating them
    reported an installed-but-unloadable wheel (missing system library, too-old
    glibc) as "install the voice extra" to someone who already had.
    """
    from kiro_crew.stt import engine as engine_mod

    monkeypatch.setattr(engine_mod.importlib.util, "find_spec", lambda name: None)


class TestFindBrew:
    """A GUI-launched gateway inherits PATH=/usr/bin:/bin:/usr/sbin:/sbin, so
    ``shutil.which("brew")`` reports Homebrew MISSING on a machine that has it.
    ``find_brew`` falls back to the fixed install prefixes."""

    def test_found_on_path(self):
        with patch("kiro_crew.transcribe.shutil.which", return_value="/opt/homebrew/bin/brew"):
            assert find_brew() == "/opt/homebrew/bin/brew"

    def test_found_off_path_via_prefix(self, tmp_path, monkeypatch):
        brew = tmp_path / "brew"
        brew.write_text("#!/bin/sh\n")
        brew.chmod(0o755)
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._BREW_CANDIDATE_PATHS", [str(brew)])
            assert find_brew() == str(brew)

    def test_not_installed(self, monkeypatch):
        with patch("kiro_crew.transcribe.shutil.which", return_value=None):
            monkeypatch.setattr("kiro_crew.transcribe._BREW_CANDIDATE_PATHS", ["/nonexistent/brew"])
            assert find_brew() is None


# ---------------------------------------------------------------------------
# availability_detail / is_available
# ---------------------------------------------------------------------------
class TestIsAvailable:
    def test_disabled(self):
        cfg = SttConfig(enabled=False)
        assert is_available(cfg) is False

    def test_loads_config_when_none(self):
        mock_cfg = MagicMock()
        mock_cfg.stt = SttConfig(enabled=False)
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_cfg):
            assert is_available(None) is False


class TestAvailabilityDetail:
    """One shape for all three providers, and one code per distinct next action.

    "Install an extra", "your platform has no prebuilt wheel" and "this needs a
    newer macOS" lead the user somewhere different, so each is pinned separately:
    collapsing them into a boolean is what makes voice input feel broken rather
    than unconfigured. The codes themselves travel to the browser in JSON and
    select the message the dashboard renders, so they are pinned as literals via
    the constants that own them.
    """

    @staticmethod
    def _detail(cfg) -> stt.Availability:
        """The detailed answer, having checked the boolean view agrees with it.

        ``is_available`` is derived from ``availability_detail`` rather than
        implemented beside it, and the pair that disagrees hands a caller a 503
        for a provider the settings panel is showing as ready. Asserted on every
        case below, so a re-implementation of either cannot pass.
        """
        detail = availability_detail(cfg)
        assert is_available(cfg) is detail.ok
        return detail

    def test_disabled_is_a_choice_not_a_fault(self):
        """Its own code, because "you turned this off" and "this cannot run here"
        are not the same report and only one of them is worth a fix hint."""
        detail = self._detail(SttConfig(enabled=False))
        assert detail.ok is False
        assert detail.code == transcribe.CODE_DISABLED
        assert detail.detail

    def test_local_missing_extra(self, monkeypatch):
        """The recogniser wheel is absent on a default install."""
        _stub_recogniser_absent(monkeypatch)
        detail = self._detail(SttConfig(enabled=True, provider="local"))
        assert detail.ok is False
        assert detail.code == stt.CODE_EXTRA_MISSING
        # The recogniser distribution, not the extra name: `kirocrew[voice]` is
        # not installable from any index.
        assert "pywhispercpp" in detail.detail
        assert "kirocrew[" not in detail.detail

    def test_local_no_wheel_for_platform(self, monkeypatch):
        """A platform with no published wheel means "install a C++ toolchain",
        not "install the extra"."""
        from kiro_crew.stt import engine as engine_mod

        _stub_recogniser_absent(monkeypatch)
        monkeypatch.setattr(engine_mod, "_has_prebuilt_wheel", lambda: False)
        detail = self._detail(SttConfig(enabled=True, provider="local"))
        assert detail.ok is False
        assert detail.code == stt.CODE_NO_WHEEL

    def test_local_import_failed(self, monkeypatch):
        """An installed wheel that will not load (missing system library,
        incompatible CPU baseline) is a third state: nothing to install, and the
        loader's own message is the only useful thing to show."""

        class _ExplodingFinder:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] == "pywhispercpp":
                    raise RuntimeError("dlopen failed: libwhisper")
                return None

        monkeypatch.delitem(sys.modules, "pywhispercpp", raising=False)
        monkeypatch.delitem(sys.modules, "pywhispercpp.model", raising=False)
        monkeypatch.setattr(sys, "meta_path", [_ExplodingFinder(), *sys.meta_path])
        detail = self._detail(SttConfig(enabled=True, provider="local"))
        assert detail.ok is False
        assert detail.code == stt.CODE_IMPORT_FAILED
        assert "dlopen failed: libwhisper" in detail.detail

    def test_local_ok_ignores_a_model_that_is_not_downloaded(self, monkeypatch):
        """A missing model is deliberately NOT part of the answer.

        It resolves itself on first use, so reporting it as unavailable would hide
        a working install behind a condition that fixes itself. ``models_dir`` is
        under the per-test data home and holds nothing, so the configured model is
        genuinely absent here.
        """
        from kiro_crew.stt import models

        _stub_recogniser_importable(monkeypatch)
        cfg = SttConfig(enabled=True, provider="local", model="base")
        assert models.is_present(models.resolve(cfg.model)) is False
        detail = self._detail(cfg)
        assert detail.ok is True
        assert detail.code == stt.CODE_OK

    def test_transcribe_without_boto3(self, monkeypatch):
        monkeypatch.setattr(transcribe, "boto3", None)
        detail = self._detail(SttConfig(enabled=True, provider="transcribe"))
        assert detail.ok is False
        assert detail.code == stt.CODE_EXTRA_MISSING
        # The cloud half by name, and never the unresolvable extras form.
        assert "amazon-transcribe" in detail.detail
        assert "kirocrew[" not in detail.detail

    def test_transcribe_without_the_streaming_client(self, monkeypatch):
        """boto3 alone is not enough: the streaming client is a separate package
        in the same extra, and its absence must report the same fix."""
        monkeypatch.setattr(transcribe, "boto3", object())
        monkeypatch.setitem(sys.modules, "amazon_transcribe", None)
        detail = self._detail(SttConfig(enabled=True, provider="transcribe"))
        assert detail.ok is False
        assert detail.code == stt.CODE_EXTRA_MISSING

    def test_transcribe_ok_does_not_consult_consent(self, monkeypatch):
        """Consent is checked where audio would leave the host, not here.

        This predicate is polled (once per inbound Slack message, on every
        settings read) and a refusal writes an audit entry, so asking here would
        fill the audit log with refusals nobody requested.
        """
        from kiro_crew import aws_consent

        monkeypatch.setattr(transcribe, "boto3", object())
        monkeypatch.setitem(sys.modules, "amazon_transcribe", types.ModuleType("amazon_transcribe"))

        def _refuse(*args, **kwargs):
            raise AssertionError("availability must not touch the consent gate")

        monkeypatch.setattr(aws_consent, "refuse_and_log", _refuse)
        detail = self._detail(SttConfig(enabled=True, provider="transcribe"))
        assert detail.ok is True
        assert detail.code == stt.CODE_OK

    def test_apple_needs_toolchain(self, monkeypatch):
        """Separate from unsupported because this one has a one-line fix."""
        from kiro_crew import apple_speech

        monkeypatch.setattr(
            apple_speech,
            "availability",
            lambda: apple_speech.Availability(
                False, "run: xcode-select --install", needs_toolchain=True
            ),
        )
        detail = self._detail(SttConfig(enabled=True, provider="apple"))
        assert detail.ok is False
        assert detail.code == transcribe.CODE_APPLE_NEEDS_TOOLCHAIN
        assert detail.detail == "run: xcode-select --install"

    def test_apple_unsupported_host(self, monkeypatch):
        """Not macOS, or too old a macOS for the SpeechAnalyzer API. No install
        fixes it, so it must not be reported as something to install."""
        from kiro_crew import apple_speech

        monkeypatch.setattr(
            apple_speech,
            "availability",
            lambda: apple_speech.Availability(False, "Apple speech is macOS only"),
        )
        detail = self._detail(SttConfig(enabled=True, provider="apple"))
        assert detail.ok is False
        assert detail.code == transcribe.CODE_APPLE_UNSUPPORTED

    def test_apple_available_never_builds_the_helper(self, monkeypatch):
        """Availability runs on the event loop (the settings read, the transcribe
        endpoint, the Slack voice path), and compiling the Swift helper there
        would freeze the gateway for as long as swiftc takes."""
        from kiro_crew import apple_speech

        monkeypatch.setattr(apple_speech, "availability", lambda: apple_speech.Availability(True))

        def _no_build(*args, **kwargs):
            raise AssertionError("availability must not build the Swift helper")

        monkeypatch.setattr(apple_speech, "_build_helper", _no_build)
        detail = self._detail(SttConfig(enabled=True, provider="apple"))
        assert detail.ok is True
        assert detail.code == stt.CODE_OK

    def test_retired_provider_answers_on_the_local_floor(self, monkeypatch):
        """A hand-edited config naming a retired provider must not report a fault.

        ``local`` is the floor every other value degrades to, so the answer is the
        local recogniser's own, not "unknown provider".
        """
        _stub_recogniser_absent(monkeypatch)
        detail = self._detail(SttConfig(enabled=True, provider="mlx"))
        assert detail.ok is False
        assert detail.code == stt.CODE_EXTRA_MISSING


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
    async def test_aws_audio_read_runs_off_event_loop(self, tmp_path, monkeypatch):
        from threading import get_ident

        from kiro_crew import transcribe as tr

        audio = tmp_path / "test.ogg"
        audio.write_bytes(b"fake audio")
        cfg = SttConfig(enabled=True, provider="transcribe", timeout_secs=10)
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
        monkeypatch.setattr(tr, "_read_audio_bytes", read_audio)
        monkeypatch.setattr(
            tr,
            "_load_aws_transcribe_components",
            lambda: (FakeClient, FakeHandler),
        )

        result = await tr._transcribe_aws(str(audio), cfg)

        assert result is None
        assert read_threads
        assert read_threads[0] != loop_thread

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
    async def test_boilerplate_only_transcript_returns_none(self, tmp_path, monkeypatch):
        """The hallucination filter can empty a transcript that was entirely
        caption boilerplate. Empty means no transcript, so the caller reports a
        memo it could not hear instead of writing boilerplate into agent notes."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        self._recogniser(monkeypatch, transcript="")
        assert await transcribe_audio(str(audio), cfg) is None

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
# _whisper_language
# ---------------------------------------------------------------------------


class TestWhisperLanguage:
    """Whisper names its languages by bare ISO 639 code, never a region, so a
    configured locale has to be cut down to its primary subtag."""

    @pytest.mark.parametrize(
        "configured, expected",
        [
            ("en-US", "en"),
            ("zh-CN", "zh"),
            ("pt-BR", "pt"),
            # A POSIX locale spells the separator with an underscore.
            ("en_GB", "en"),
            ("fr_FR", "fr"),
            ("FR-fr", "fr"),
            # Whitespace survives a hand-edited config.json.
            ("  de-DE  ", "de"),
            ("fr", "fr"),
            # Three letters is a valid ISO 639-3 code.
            ("haw", "haw"),
        ],
    )
    def test_locale_reduces_to_its_primary_subtag(self, configured, expected):
        assert transcribe._whisper_language(configured) == expected

    @pytest.mark.parametrize("configured", ["auto", "", None])
    def test_unset_means_auto_detect(self, configured):
        """The empty string is what the recogniser reads as auto-detect, so an
        unset or explicitly automatic setting resolves to it rather than to a
        guessed language."""
        assert transcribe._whisper_language(configured) == ""

    def test_non_string_means_auto_detect(self):
        """A hand-edited config.json can hold a number or an object here. ``or ""``
        alone would let a truthy non-string through, because it only substitutes
        on a falsy value."""
        assert transcribe._whisper_language(0) == ""
        assert transcribe._whisper_language(1234) == ""
        assert transcribe._whisper_language(["en-US"]) == ""

    @pytest.mark.parametrize("configured", ["e", "english", "12", "x-klingon", "!!", "-"])
    def test_garbage_tag_auto_detects_rather_than_raising(self, configured):
        """A mistyped setting must cost the user a detection pass, never a failed
        transcription."""
        assert transcribe._whisper_language(configured) == ""


# ---------------------------------------------------------------------------
# _pcm_from_wav
# ---------------------------------------------------------------------------


class TestPcmFromWav:
    """The dashboard's audio worklet and the recogniser already agree on 16 kHz
    mono int16, so audio in that form needs no external tool. Anything else
    returns None so the caller hands it to ffmpeg, because resampling correctly
    is ffmpeg's job and a naive stride would change the pitch the model hears.
    """

    def test_mono_16k_decodes_to_scaled_float32(self, tmp_path):
        samples = _ramp_int16(800)
        audio = tmp_path / "mono.wav"
        _write_wav(audio, samples)

        pcm = transcribe._pcm_from_wav(str(audio))

        assert pcm is not None
        assert pcm.dtype == np.float32
        np.testing.assert_allclose(pcm, samples.astype(np.float32) / 32768.0)

    def test_stereo_is_downmixed_and_stays_float32(self, tmp_path):
        """Folding channels rather than refusing them: a two-channel 16 kHz
        recording needs no resampling, so spending an ffmpeg spawn on it would be
        pure latency."""
        frames = 400
        interleaved = np.empty(frames * 2, dtype=np.int16)
        interleaved[0::2] = 10_000  # left
        interleaved[1::2] = -6_000  # right
        audio = tmp_path / "stereo.wav"
        _write_wav(audio, interleaved, channels=2)

        pcm = transcribe._pcm_from_wav(str(audio))

        assert pcm is not None
        assert pcm.dtype == np.float32
        assert pcm.size == frames
        np.testing.assert_allclose(pcm, np.full(frames, 2_000 / 32768.0, dtype=np.float32))

    @pytest.mark.parametrize("rate", [8_000, 44_100, 48_000])
    def test_other_sample_rates_defer_to_ffmpeg(self, tmp_path, rate):
        audio = tmp_path / "rate.wav"
        _write_wav(audio, _ramp_int16(400), rate=rate)
        assert transcribe._pcm_from_wav(str(audio)) is None

    @pytest.mark.parametrize("sampwidth", [1, 4])
    def test_other_sample_widths_defer_to_ffmpeg(self, tmp_path, sampwidth):
        """Only int16 is read here. A different width is a conversion, and the
        conversion belongs to the tool that also resamples."""
        audio = tmp_path / "width.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(sampwidth)
            wav.setframerate(16_000)
            wav.writeframes(b"\x00" * (400 * sampwidth))
        assert transcribe._pcm_from_wav(str(audio)) is None

    @pytest.mark.parametrize("header_bytes", [0, 4, 20, 40])
    def test_truncated_header_returns_none_rather_than_raising(self, tmp_path, header_bytes):
        """A transfer cut off before the format chunk is unreadable, and the
        reader says so by raising. ffmpeg reads far more than the stdlib does, so
        that is a "try the other route" rather than a failure to report."""
        audio = tmp_path / "cut.wav"
        _write_wav(audio, _ramp_int16(800))
        audio.write_bytes(audio.read_bytes()[:header_bytes])
        assert transcribe._pcm_from_wav(str(audio)) is None

    def test_truncated_data_keeps_the_frames_that_arrived(self, tmp_path):
        """A recording cut off mid-stream has a header promising more frames than
        the file holds. What is there is still speech, so it decodes rather than
        costing the user the whole memo."""
        audio = tmp_path / "short.wav"
        _write_wav(audio, _ramp_int16(800))
        whole = audio.read_bytes()
        audio.write_bytes(whole[: len(whole) - 600])

        pcm = transcribe._pcm_from_wav(str(audio))

        assert pcm is not None
        assert pcm.dtype == np.float32
        # 600 bytes short of the promised 800 int16 frames.
        assert pcm.size == 800 - 300

    def test_non_wav_payload_returns_none(self, tmp_path):
        """A ``.wav`` suffix is only trusted to decide whether to TRY: a Slack
        voice memo saved under the wrong name must fall through to the transcode.
        """
        audio = tmp_path / "actually-ogg.wav"
        audio.write_bytes(b"OggS\x00\x02" + b"\x00" * 200)
        assert transcribe._pcm_from_wav(str(audio)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert transcribe._pcm_from_wav(str(tmp_path / "absent.wav")) is None


# ---------------------------------------------------------------------------
# Slack voice-memo adapter
# ---------------------------------------------------------------------------


class TestTranscribeFiles:
    @pytest.mark.xdist_group(name="serial")
    @pytest.mark.asyncio
    async def test_transcribe_audio_files(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()
        mock_orch.slack.download_file = AsyncMock()

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://files.slack.com/a.webm",
                "filetype": "webm",
                "name": "voice.webm",
            },
        ]

        with patch(
            "kiro_crew.slack.events.transcribe_audio", new_callable=AsyncMock, return_value="Hello"
        ):
            result = await _transcribe_files(mock_orch, files)
        assert result == ["Hello"]

    @pytest.mark.asyncio
    async def test_skips_non_audio(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()

        files = [
            {"mimetype": "image/png", "url_private": "https://x.com/img.png", "name": "pic.png"}
        ]

        result = await _transcribe_files(mock_orch, files)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_no_url(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()

        files = [{"mimetype": "audio/webm", "name": "voice.webm"}]

        result = await _transcribe_files(mock_orch, files)
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_transcription_failure(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()
        mock_orch.slack.download_file = AsyncMock()

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://x.com/a.webm",
                "filetype": "webm",
                "name": "v.webm",
            },
        ]

        # Patch where events.py BOUND the symbol, not where it is defined: events.py
        # does `from kiro_crew.transcribe import transcribe_audio`, so it holds its own
        # module global. Patching the definition left the REAL transcriber running --
        # the assertion passed for the wrong reason and the test was the 3rd slowest in
        # the suite. Matches the sibling test above.
        with patch(
            "kiro_crew.slack.events.transcribe_audio", new_callable=AsyncMock, return_value=None
        ):
            result = await _transcribe_files(mock_orch, files)
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        from kiro_crew.slack.events import _transcribe_files

        mock_orch = MagicMock()
        mock_orch.slack = AsyncMock()
        mock_orch.slack.download_file = AsyncMock(side_effect=Exception("download failed"))

        files = [
            {
                "mimetype": "audio/webm",
                "url_private_download": "https://x.com/a.webm",
                "filetype": "webm",
                "name": "v.webm",
            },
        ]

        result = await _transcribe_files(mock_orch, files)
        assert result == []


# ---------------------------------------------------------------------------
# client.py: download_file
# ---------------------------------------------------------------------------


class TestSlackClientDownloadFile:
    @pytest.mark.asyncio
    async def test_base_class_raises(self):
        from kiro_crew.slack.client import SlackClientOps

        class MinimalClient(SlackClientOps):
            async def post_message(self, *a, **kw):
                pass

            async def post_blocks(self, *a, **kw):
                pass

            async def update_message(self, *a, **kw):
                pass

            async def delete_message(self, *a, **kw):
                pass

            async def add_reaction(self, *a, **kw):
                pass

            async def remove_reaction(self, *a, **kw):
                pass

            async def open_dm(self, *a, **kw):
                pass

            async def post_ephemeral(self, *a, **kw):
                pass

            async def views_publish(self, *a, **kw):
                pass

            async def views_open(self, *a, **kw):
                pass

            async def views_update(self, *a, **kw):
                pass

            async def upload_file(self, *a, **kw):
                pass

        client = MinimalClient()
        with pytest.raises(NotImplementedError):
            await client.download_file("https://example.com/f", "/tmp/out")


# ---------------------------------------------------------------------------
# SttConfig
# ---------------------------------------------------------------------------


class TestSttConfig:
    def test_defaults(self):
        """The shipped defaults, stated as the values a user gets.

        Two of these are the whole point of the resident local recogniser and are
        pinned here rather than left implicit: ``provider`` is ``local``, which
        needs no account and no out-of-band install, and ``streaming`` is ON,
        because every provider produces partials and the panel that shows words
        while you speak is the default experience rather than an opt-in.
        """
        cfg = SttConfig()
        assert cfg.enabled is True
        assert cfg.provider == "local"
        assert cfg.model == "base"
        assert cfg.language_code == "en-US"
        assert cfg.streaming is True
        assert cfg.silence_ms == 700
        assert cfg.partial_interval_ms == 400
        assert cfg.idle_evict_secs == 600
        assert cfg.endpointing is False
        assert cfg.dictation_panel is True
        assert cfg.timeout_secs == 300

    def test_default_model_is_a_real_catalog_entry(self):
        """The default names a model the downloader can actually fetch. The
        advertised menu offering a model with no sha256 pin is how a first
        dictation fails on a fresh install."""
        from kiro_crew.stt import models

        assert SttConfig().model in {m.name for m in models.CATALOG}

    def test_custom_values(self):
        cfg = SttConfig(
            enabled=True,
            provider="transcribe",
            model="small",
            streaming=False,
            timeout_secs=60,
        )
        assert cfg.enabled is True
        assert cfg.provider == "transcribe"
        assert cfg.model == "small"
        assert cfg.streaming is False
        assert cfg.timeout_secs == 60

    @pytest.mark.parametrize(
        "retired_field", ["whisper_path", "mlx_model", "parakeet_model", "device"]
    )
    def test_fields_of_retired_providers_are_gone(self, retired_field):
        """Each named an out-of-band install or a device selector belonging to a
        provider that no longer exists. Re-adding one would put a setting back in
        the panel that nothing reads."""
        assert not hasattr(SttConfig(), retired_field)


# ---------------------------------------------------------------------------
# The two provider-independent guards
# ---------------------------------------------------------------------------


class TestSensitivePathGuard:
    """Both guards run outside every provider branch, deliberately: a per-branch
    copy is a copy that will be missing from the next branch someone adds."""

    @pytest.mark.asyncio
    async def test_sensitive_path_blocked_before_any_local_decode(self, tmp_path, monkeypatch):
        """The refusal lands before dispatch, so the local recogniser is never
        even asked whether it could run: reading the file at all is the thing
        being refused."""
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local")

        def _never_probed():
            raise AssertionError("the guard must refuse before the provider is reached")

        def _never_decoded(path):
            raise AssertionError("a refused path must not be read")

        monkeypatch.setattr(stt, "availability", _never_probed)
        monkeypatch.setattr(transcribe, "_pcm_from_wav", _never_decoded)
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None

    @pytest.mark.asyncio
    async def test_sensitive_path_blocked_for_transcribe(self, tmp_path):
        audio = tmp_path / "test.webm"
        audio.write_text("fake")
        cfg = SttConfig(enabled=True, provider="transcribe")
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            result = await transcribe_audio(str(audio), cfg)
        assert result is None


class TestTranscriptRedaction:
    @pytest.mark.asyncio
    async def test_local_transcript_is_redacted(self, tmp_path, monkeypatch):
        """Spoken credentials are redacted on the way out of the local provider
        too, not only the cloud one.

        The transcript becomes a turn in a session and a line in the history, so a
        key read aloud must not survive the trip even though this recogniser never
        sent the audio anywhere.
        """
        audio = tmp_path / "voice.wav"
        _write_wav(audio, _ramp_int16(1600))
        cfg = SttConfig(enabled=True, provider="local", timeout_secs=10)
        monkeypatch.setattr(stt, "availability", lambda: stt.Availability(True))

        async def fake_transcribe_pcm(pcm, **kwargs):
            return "the key is AKIAIOSFODNN7EXAMPLE ok", stt.Availability(True)

        monkeypatch.setattr(stt, "transcribe_pcm", fake_transcribe_pcm)

        result = await transcribe_audio(str(audio), cfg)

        assert result is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED: credential]" in result
        # The surrounding speech survives: redaction replaces the secret, it does
        # not discard the utterance.
        assert result.startswith("the key is ")


# ---------------------------------------------------------------------------
# ffmpeg discovery: bundled with desktop releases, with system fallback for source
# ---------------------------------------------------------------------------

# Floor for a plausible pinned ffmpeg payload. The real upstream executables are
# 49-88 MB, so this is an order of magnitude below the smallest of them: it is here
# to catch a size transcribed with digits dropped, not to track a version, and it
# must stay far enough below the true sizes that an upstream bump never trips it.
_MIN_PLAUSIBLE_FFMPEG_BYTES = 8 * 1024 * 1024

# GitHub Actions sets CI=true on every runner. Used only to decide whether a test
# that cannot obtain its inputs is allowed to skip: a developer machine may lack
# them, a runner may not.
_RUNNING_UNDER_CI = os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


class TestBundledFfmpeg:
    """Resolve only the executable inside the pinned imageio-ffmpeg wheel."""

    @staticmethod
    def _fake_package(
        monkeypatch, tmp_path, *, create_binary: bool = True, signer_rewritten: bool = False
    ):
        package_dir = tmp_path / "imageio_ffmpeg"
        binaries = package_dir / "binaries"
        binaries.mkdir(parents=True)
        filename = "ffmpeg-test.exe" if _pc.IS_WINDOWS else "ffmpeg-test"
        binary = binaries / filename
        payload = b"bundled decoder"
        if create_binary:
            binary.write_bytes(payload)
            binary.chmod(0o755)

        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", lambda: (str(tmp_path),))
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(payload), transcribe.hashlib.sha256(payload).hexdigest())},
        )
        monkeypatch.setattr(
            transcribe,
            "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS",
            frozenset({filename} if signer_rewritten else ()),
        )
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: True)
        return binary

    def test_resolves_the_exact_wheel_resource(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_missing_wheel_resource_is_not_replaced_by_path(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path, create_binary=False)
        monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(tmp_path / "attacker-controlled"))
        assert transcribe._bundled_ffmpeg() is None

    def test_missing_bundle_never_falls_back_to_system_for_status(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path, create_binary=False)

        def _unexpected_system_lookup():
            raise AssertionError("bundled runtime fell back to a system decoder")

        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", _unexpected_system_lookup)
        assert transcribe._find_ffmpeg() is None

    def test_missing_bundle_never_falls_back_to_system_for_execution(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path, create_binary=False)

        def _unexpected_system_lookup():
            raise AssertionError("bundled runtime fell back to a system decoder")

        monkeypatch.setattr(transcribe, "_find_system_ffmpeg", _unexpected_system_lookup)
        assert transcribe._open_ffmpeg_for_execution() is None

    def test_project_venv_is_never_trusted_as_a_bundled_runtime(self, monkeypatch):
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: False)

        def _unexpected_roots():
            raise AssertionError("source environment was scanned for an executable")

        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", _unexpected_roots)
        assert transcribe._bundled_ffmpeg() is None

    def test_packaged_cli_without_electron_parent_uses_release_decoder(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)

        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_same_size_replacement_is_rejected_by_digest(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"changed decoder")

        assert transcribe._bundled_ffmpeg() is None

    def test_truncated_payload_is_rejected_by_size(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"short")

        assert transcribe._bundled_ffmpeg() is None

    def _probe_with_exit(self, monkeypatch, tmp_path, script: bytes):
        """Install *script* as the packaged decoder and probe it for real.

        The decoder is EXECUTED rather than stubbed, because the split this covers
        is about what a real spawn reports back.
        """
        package_dir = tmp_path / "imageio_ffmpeg"
        binaries = package_dir / "binaries"
        binaries.mkdir(parents=True)
        filename = "ffmpeg-test.exe" if _pc.IS_WINDOWS else "ffmpeg-test"
        binary = binaries / filename
        binary.write_bytes(script)
        binary.chmod(0o755)
        monkeypatch.setattr(transcribe, "_trusted_site_package_roots", lambda: (str(tmp_path),))
        monkeypatch.setattr(
            transcribe,
            "_PACKAGED_FFMPEG_ARTIFACTS",
            {filename: (len(script), transcribe.hashlib.sha256(script).hexdigest())},
        )
        monkeypatch.setattr(transcribe, "_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS", frozenset())
        return binary, transcribe._packaged_ffmpeg_version_probe()

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="needs a POSIX shell for the fake decoder")
    def test_probe_reports_ok_when_the_authenticated_decoder_runs(self, monkeypatch, tmp_path):
        _, probe = self._probe_with_exit(
            monkeypatch, tmp_path, b"#!/bin/sh\necho 'ffmpeg version 7'\nexit 0\n"
        )

        assert probe.ok is True
        assert probe.authentic is True
        assert probe.code == transcribe.DECODER_OK

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="needs a POSIX shell for the fake decoder")
    def test_a_host_that_cannot_run_an_authentic_decoder_is_not_reported_as_tampering(
        self, monkeypatch, tmp_path
    ):
        """This is the WHOLE point of the split, so it is pinned explicitly.

        A build image missing a library the decoder needs makes the loader refuse
        an executable whose bytes are exactly the pinned payload. Reporting that as
        an unauthentic artifact fails the release over a property of the build
        machine, and sends whoever reads the log hunting a supply-chain problem
        that is not there.
        """
        _, probe = self._probe_with_exit(
            monkeypatch,
            tmp_path,
            b"#!/bin/sh\necho 'libavcodec.so.61: cannot open shared object' >&2\nexit 127\n",
        )

        assert probe.ok is False
        # Authentic: the release gate must NOT fail the build on this.
        assert probe.authentic is True
        assert probe.code == transcribe.DECODER_NOT_EXECUTABLE
        # The child's own complaint reaches the caller, rather than DEVNULL.
        assert "libavcodec.so.61" in probe.detail

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="needs a POSIX shell for the fake decoder")
    def test_a_decoder_that_cannot_be_spawned_at_all_still_counts_as_authentic(
        self, monkeypatch, tmp_path
    ):
        """The OSError arm is a DISTINCT path from a non-zero exit and must reach
        the same verdict: the bytes authenticated, the host would not run them."""
        _, probe = self._probe_with_exit(
            monkeypatch, tmp_path, b"#!/nonexistent/interpreter\nbody\n"
        )

        assert probe.ok is False
        assert probe.authentic is True
        assert probe.code == transcribe.DECODER_NOT_EXECUTABLE

    def test_a_payload_that_fails_authentication_is_still_refused(self, monkeypatch, tmp_path):
        """Splitting the outcomes must not have weakened the half that matters:
        bytes that are not the pinned payload stay a hard failure."""
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"attacker decoder")

        probe = transcribe._packaged_ffmpeg_version_probe()

        assert probe.ok is False
        assert probe.authentic is False
        assert probe.code == transcribe.DECODER_UNAUTHENTIC

    def test_windows_loader_statuses_are_named_in_the_detail(self):
        """0xC0000135 is the observed Server Core refusal, and a bare signed
        integer in a build log is not something a reader can act on."""

        class _Refused:
            returncode = -1073741515
            stdout = b""
            stderr = b""

        detail = transcribe._decoder_exit_detail("/x/ffmpeg", _Refused())

        assert "0xC0000135" in detail
        assert "STATUS_DLL_NOT_FOUND" in detail

    def test_probe_detail_is_bounded(self):
        """A decoder can emit unbounded output; the report stays log-sized."""

        class _Noisy:
            returncode = 1
            stdout = b""
            stderr = b"x" * 5000

        detail = transcribe._decoder_exit_detail("/x/ffmpeg", _Noisy())

        assert len(detail) < transcribe._DECODER_DETAIL_MAX_CHARS + 200
        assert detail.endswith("…")

    def test_payload_chunks_yield_the_file_bytes(self, tmp_path):
        raw = tmp_path / "ffmpeg"
        raw.write_bytes(b"bundled decoder")
        descriptor = os.open(raw, os.O_RDONLY)
        try:
            assert b"".join(transcribe._ffmpeg_payload_chunks(descriptor)) == b"bundled decoder"
        finally:
            os.close(descriptor)

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="signer-rewritten payloads are macOS-only")
    def test_signer_rewritten_payload_is_accepted_on_a_developer_id_signature(
        self, monkeypatch, tmp_path
    ):
        """A released macOS decoder no longer matches the upstream digest, because
        signing replaced its Mach-O signature. macOS vouching for the exact bytes
        staged for execution is the second anchor."""
        binary = self._fake_package(monkeypatch, tmp_path, signer_rewritten=True)
        binary.write_bytes(b"bundled decoder + developer id signature")
        binary.chmod(0o755)
        verified: list[str] = []

        def authentic(path):
            verified.append(path)
            return True

        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", authentic)

        opened = transcribe._open_packaged_ffmpeg_resource()

        assert opened is not None
        try:
            assert opened.source_path == str(binary)
            # The signature was checked on the snapshot, not on the bundle path.
            assert verified == [opened.execution_path]
            assert opened.execution_path != str(binary)
        finally:
            opened.close()

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="signer-rewritten payloads are macOS-only")
    def test_signer_rewritten_payload_without_a_valid_signature_is_refused(
        self, monkeypatch, tmp_path
    ):
        binary = self._fake_package(monkeypatch, tmp_path, signer_rewritten=True)
        binary.write_bytes(b"attacker decoder, ad-hoc signed")
        binary.chmod(0o755)
        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", lambda _path: False)

        assert transcribe._bundled_ffmpeg() is None

    @pytest.mark.skipif(_pc.IS_WINDOWS, reason="signer-rewritten payloads are macOS-only")
    def test_upstream_digest_still_authenticates_without_any_signature(self, monkeypatch, tmp_path):
        """The desktop build gate runs BEFORE signing, so the pinned wheel bytes
        must keep authenticating on their own."""
        binary = self._fake_package(monkeypatch, tmp_path, signer_rewritten=True)

        def _unexpected_codesign(_path):
            raise AssertionError("codesign consulted for pinned upstream bytes")

        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", _unexpected_codesign)

        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_non_signer_rewritten_artifact_never_consults_codesign(self, monkeypatch, tmp_path):
        """Linux and Windows payloads stay digest-only: a mismatch is refused
        outright, never escalated to a signature check."""
        binary = self._fake_package(monkeypatch, tmp_path)
        binary.write_bytes(b"changed decoder!!")

        def _unexpected_codesign(_path):
            raise AssertionError("codesign consulted for a digest-pinned artifact")

        monkeypatch.setattr(transcribe, "_macos_developer_id_authentic", _unexpected_codesign)

        assert transcribe._bundled_ffmpeg() is None

    def test_codesign_requirement_pins_the_release_team(self):
        """Chained to Apple's root AND to our team, so an ad-hoc or self-signed
        replacement cannot satisfy it."""
        assert transcribe._MACOS_SIGNING_TEAM_ID == "94KV3E626L"
        assert transcribe._MACOS_FFMPEG_REQUIREMENT == (
            'anchor apple generic and certificate leaf[subject.OU] = "94KV3E626L"'
        )

    def test_codesign_requirement_quotes_the_team_identifier(self):
        """The team id must be a QUOTED string constant, not a bare token.

        A requirement-language identifier cannot begin with a digit, so an
        unquoted 94KV3E626L does not parse as a comparison operand at all --
        codesign rejects the whole requirement and exits non-zero before it looks
        at any signature. :func:`_macos_developer_id_authentic` only sees that
        exit status, so the malformed constant is indistinguishable there from an
        inauthentic binary, and every signed release refused its own decoder.
        Asserting the exact string above cannot catch this on its own: the old
        assertion pinned the unparseable form and passed.
        """
        assert f'"{transcribe._MACOS_SIGNING_TEAM_ID}"' in transcribe._MACOS_FFMPEG_REQUIREMENT

    @pytest.mark.skipif(not sys.platform.startswith("darwin"), reason="codesign is macOS-only")
    def test_codesign_accepts_the_requirement_as_parseable(self):
        """Let codesign itself judge the syntax -- the only oracle that counts.

        Run against a binary the OS signs (never our team), so a PARSEABLE
        requirement is simply unsatisfied while an unparseable one reports a
        syntax error. Only the latter is asserted against: this test is about the
        requirement compiling, not about /bin/ls being authentic.
        """
        result = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                "-R",
                f"={transcribe._MACOS_FFMPEG_REQUIREMENT}",
                "--",
                "/bin/ls",
            ],
            check=False,
            capture_output=True,
            timeout=120,
            **UTF8_TEXT,
        )
        assert "syntax error" not in result.stderr.lower()
        assert "invalid or corrupted code requirement" not in result.stderr.lower()

    def test_codesign_is_invoked_by_absolute_path_with_requirement_source_text(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(transcribe.platform_compat, "IS_MACOS", True)
        recorded: list[list[str]] = []

        class _Result:
            returncode = 0

        def fake_run(argv, **_kwargs):
            recorded.append(argv)
            return _Result()

        monkeypatch.setattr(transcribe.subprocess, "run", fake_run)

        assert transcribe._macos_developer_id_authentic(str(tmp_path / "ffmpeg")) is True
        argv = recorded[0]
        assert argv[0] == "/usr/bin/codesign"
        assert "--strict" in argv
        # A bare requirement string would be read as a FILE PATH by codesign.
        assert argv[argv.index("-R") + 1].startswith("=anchor apple generic")

    def test_codesign_absence_is_not_authenticity(self, monkeypatch, tmp_path):
        monkeypatch.setattr(transcribe.platform_compat, "IS_MACOS", True)

        def missing_codesign(*_args, **_kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(transcribe.subprocess, "run", missing_codesign)

        assert transcribe._macos_developer_id_authentic(str(tmp_path / "ffmpeg")) is False

    def test_linux_memfd_explicitly_requests_executable_mode(self, monkeypatch):
        calls = []

        def create_memfd(name, flags):
            calls.append((name, flags))
            return 41

        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(transcribe.os, "memfd_create", create_memfd, raising=False)

        assert transcribe._new_executable_snapshot() == (41, -1, True, None)
        assert len(calls) == 1
        assert calls[0][0] == "kirocrew-ffmpeg"
        assert calls[0][1] & getattr(transcribe.os, "MFD_EXEC", 0x0010)

    def test_linux_memfd_exec_flag_falls_back_on_old_kernels(self, monkeypatch):
        calls = []

        def create_memfd(_name, flags):
            calls.append(flags)
            if len(calls) == 1:
                raise OSError(transcribe.errno.EINVAL, "unsupported flag")
            return 42

        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(transcribe.os, "memfd_create", create_memfd, raising=False)

        assert transcribe._new_executable_snapshot() == (42, -1, True, None)
        assert calls[0] & getattr(transcribe.os, "MFD_EXEC", 0x0010)
        assert not calls[1] & getattr(transcribe.os, "MFD_EXEC", 0x0010)

    def test_linux_seal_constants_work_with_old_python_headers(self, monkeypatch):
        calls = []
        fake_fcntl = SimpleNamespace(fcntl=lambda *args: calls.append(args))
        monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
        monkeypatch.setattr(
            transcribe.os, "open", lambda path, flags: (calls.append((path, flags)), 52)[1]
        )

        assert transcribe._seal_linux_memfd(51) == 52
        assert calls[0] == (51, 1033, 0x000F)
        assert calls[1][0] == "/proc/self/fd/51"

    def test_macos_snapshot_keeps_a_private_name_until_close(self, monkeypatch, tmp_path):
        parent = tmp_path / "private"

        def make_private_dir(*_args, **_kwargs):
            parent.mkdir(mode=0o700)
            return str(parent)

        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(transcribe, "_ffmpeg_snapshot_root", lambda: str(tmp_path))
        monkeypatch.setattr(transcribe.tempfile, "mkdtemp", make_private_dir)

        writer, reader, seal_writer, path = transcribe._new_executable_snapshot()
        assert path is not None
        assert not seal_writer
        os.write(writer, b"bundled decoder")
        os.close(writer)
        opened = transcribe._AuthenticatedFfmpeg("source", reader, path, cleanup_path=path)
        assert os.path.isfile(path)

        opened.close()

        assert not parent.exists()

    def test_macos_snapshot_is_staged_under_agent_denied_root(self, monkeypatch, tmp_path):
        calls = []

        def make_private_dir(*, prefix, dir):
            calls.append((prefix, dir))
            parent = tmp_path / "private"
            parent.mkdir(mode=0o700)
            return str(parent)

        root = tmp_path / "voice-runtime"
        root.mkdir()
        monkeypatch.setattr(transcribe.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(transcribe, "_ffmpeg_snapshot_root", lambda: str(root))
        monkeypatch.setattr(transcribe.tempfile, "mkdtemp", make_private_dir)

        writer, reader, _seal_writer, path = transcribe._new_executable_snapshot()
        os.close(writer)
        opened = transcribe._AuthenticatedFfmpeg("source", reader, path, cleanup_path=path)
        try:
            assert calls == [(f".kirocrew-ffmpeg-{os.getpid()}-", str(root))]
        finally:
            opened.close()

    def test_snapshot_root_is_private_and_pruned_once(self, monkeypatch, tmp_path):
        root = tmp_path / "voice-runtime"
        root.mkdir(mode=0o755)
        cleanup_calls = []
        monkeypatch.setattr(
            "kiro_crew.sandbox.prime_voice_runtime_sandbox_paths",
            lambda: str(root),
        )
        monkeypatch.setattr(transcribe, "_ffmpeg_snapshot_roots_cleaned", set())
        monkeypatch.setattr(
            transcribe,
            "_cleanup_stale_ffmpeg_snapshots",
            lambda path: cleanup_calls.append(path),
        )

        assert transcribe._ffmpeg_snapshot_root() == str(root)
        assert transcribe._ffmpeg_snapshot_root() == str(root)

        assert cleanup_calls == [str(root)]
        if not _pc.IS_WINDOWS:
            assert os.stat(root).st_mode & 0o777 == 0o700

    def test_snapshot_root_rejects_a_regular_file(self, monkeypatch, tmp_path):
        root = tmp_path / "not-a-directory"
        root.write_bytes(b"not a runtime root")
        monkeypatch.setattr(
            "kiro_crew.sandbox.prime_voice_runtime_sandbox_paths",
            lambda: str(root),
        )

        with pytest.raises(OSError, match="not a real directory"):
            transcribe._ffmpeg_snapshot_root()

    def test_snapshot_cleanup_is_best_effort_for_every_operation(self, tmp_path):
        payload = tmp_path / "private" / "ffmpeg"
        calls = []

        def fail(operation):
            def _raise(path, *_args):
                calls.append((operation, path))
                raise OSError(f"{operation} denied")

            return _raise

        with (
            patch.object(transcribe.os, "chmod", fail("chmod")),
            patch.object(transcribe.os, "unlink", fail("unlink")),
            patch.object(transcribe.os, "rmdir", fail("rmdir")),
        ):
            transcribe._remove_named_snapshot(str(payload))

        assert calls == [
            ("chmod", str(payload.parent)),
            ("unlink", str(payload)),
            ("rmdir", str(payload.parent)),
        ]

    def test_stale_macos_snapshots_are_pruned_without_following_unknown_entries(
        self, monkeypatch, tmp_path
    ):
        stale = tmp_path / ".kirocrew-ffmpeg-123-dead"
        stale.mkdir()
        (stale / "ffmpeg").write_bytes(b"stale")
        active = tmp_path / f".kirocrew-ffmpeg-{os.getpid()}-active"
        active.mkdir()
        (active / "ffmpeg").write_bytes(b"active")
        unknown = tmp_path / ".kirocrew-ffmpeg-not-a-pid"
        unknown.mkdir()
        monkeypatch.setattr(
            transcribe.platform_compat,
            "pid_liveness",
            lambda pid: transcribe.platform_compat.PID_DEAD if pid == 123 else "alive",
        )

        transcribe._cleanup_stale_ffmpeg_snapshots(str(tmp_path))

        assert not stale.exists()
        assert active.exists()
        assert unknown.exists()

    def test_release_artifact_pins_cover_every_supported_desktop(self):
        """The pin list must cover EXACTLY the shipped desktop platforms.

        No more, no less. The expected filenames are DERIVED from upstream
        imageio_ffmpeg._definitions.FNAME_PER_PLATFORM through the maintainer-owned
        source of truth transcribe._SHIPPED_FFMPEG_PLATFORMS, rather than
        hand-copied into this test. So an omitted platform, a one-character name
        typo, or a stale version suffix fails loudly HERE instead of silently at
        runtime, where _open_packaged_ffmpeg_resource() would treat the missed
        filename as an absent binary and report a generic "missing or damaged"
        decoder.
        """
        # A stale or renamed platform key would silently drop that platform's
        # binary from expected_filenames below, so check membership first.
        missing_keys = transcribe._SHIPPED_FFMPEG_PLATFORMS - set(FNAME_PER_PLATFORM)
        assert not missing_keys, (
            f"_SHIPPED_FFMPEG_PLATFORMS keys absent from upstream "
            f"FNAME_PER_PLATFORM (renamed or removed upstream?): {sorted(missing_keys)}"
        )

        expected_filenames = {
            FNAME_PER_PLATFORM[key] for key in transcribe._SHIPPED_FFMPEG_PLATFORMS
        }
        assert set(transcribe._PACKAGED_FFMPEG_ARTIFACTS) == expected_filenames

    def test_host_platform_pin_matches_the_real_installed_bytes(self):
        """The pin for THIS host's platform is checked against the actual bytes.

        Deriving the KEY set from upstream says nothing about the (size, sha256)
        VALUES, and the digest is what the module calls its trust anchor: a
        fat-fingered or truncated one ships a decoder that no platform can
        authenticate. imageio-ffmpeg is a real dev dependency, so its wheel puts
        this platform's executable in site-packages -- the same upstream bytes the
        desktop build stages -- which turns one transcribed literal per host into a
        checked claim. Across the Linux, Windows and macOS-arm64 legs that covers
        three of the five pins; the two no runner can supply are shape-guarded
        below.

        CI may not skip this. Every CI leg runs on a platform the desktop matrix
        ships, and pip resolves that platform's imageio-ffmpeg wheel, which carries
        the executable -- so under CI both of those are requirements rather than
        luck, and a resolver that produced the pure-Python wheel instead has
        silently removed the only byte-level check on the pinned values. A skip
        scores as a pass, which is the exact "green while checking nothing" shape
        this whole class of test exists to end, so it becomes a red instead. A
        contributor on an unshipped platform, or one whose environment genuinely
        cannot supply the bytes, still skips -- fetching them over the network is
        not something a unit test may do.
        """
        host_key = get_platform()
        filename = FNAME_PER_PLATFORM.get(host_key)
        binary = (
            Path(imageio_ffmpeg.__file__).parent / "binaries" / filename
            if filename is not None
            else None
        )

        unavailable = ""
        if host_key not in transcribe._SHIPPED_FFMPEG_PLATFORMS:
            unavailable = f"host platform {host_key!r} is not a shipped desktop target"
        elif binary is None or not binary.is_file():
            unavailable = f"imageio-ffmpeg installed no upstream executable at {binary}"

        if unavailable:
            assert not _RUNNING_UNDER_CI, (
                "CI must check this host's pin against the real installed bytes, "
                f"and cannot: {unavailable}"
            )
            pytest.skip(unavailable)

        assert binary is not None  # narrowed by the guard above

        payload = binary.read_bytes()
        assert (
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        ) == transcribe._PACKAGED_FFMPEG_ARTIFACTS[filename], (
            f"the pin for {filename} disagrees with the bytes imageio-ffmpeg "
            "actually installs -- one of the two is wrong"
        )

    def test_every_pin_is_a_well_formed_size_and_sha256(self):
        """Shape guard for the pinned values no CI leg can hash.

        macOS Intel and linux-aarch64 have no runner that installs their upstream
        executable, and the only gate that EXECUTES a staged decoder
        (``_packaged_ffmpeg_version_probe``, called from
        ``local_voice_runtime_gate`` in packaging/build-desktop.sh) is skipped for
        the Intel slice. So for those two entries a truncated digest or a
        zero-length size would otherwise reach a desktop user as a generic
        "missing or damaged decoder" remedy with CI fully green. The bounds are
        deliberately loose -- an ffmpeg build is tens of megabytes and a signed one
        stays under _MAX_SIGNED_FFMPEG_BYTES -- so a legitimate version bump passes
        while a transcription slip does not.
        """
        assert transcribe._PACKAGED_FFMPEG_ARTIFACTS, "the pin table must not be empty"

        for filename, (size, digest) in sorted(transcribe._PACKAGED_FFMPEG_ARTIFACTS.items()):
            assert (
                _MIN_PLAUSIBLE_FFMPEG_BYTES <= size <= transcribe._MAX_SIGNED_FFMPEG_BYTES
            ), f"{filename}: pinned size {size} is not a plausible ffmpeg payload"
            assert re.fullmatch(
                r"[0-9a-f]{64}", digest
            ), f"{filename}: pinned digest is not a lowercase 64-hex SHA-256: {digest!r}"

        # Two platforms pinning one digest is the copy-paste slip that a per-entry
        # check cannot see, and it authorizes one platform's bytes to run as
        # another's.
        digests = [digest for _, digest in transcribe._PACKAGED_FFMPEG_ARTIFACTS.values()]
        assert len(set(digests)) == len(
            digests
        ), f"duplicate pinned digest across platforms: {digests}"

    def test_signer_rewritten_artifacts_are_exactly_the_macos_slices(self):
        """The signature-anchored set is derived, not hand-listed.

        The macOS app signer rewrites every nested Mach-O on its way into a
        release, so exactly the macOS slices -- and only those -- are the ones
        whose pinned upstream digest cannot match the released bytes and which
        therefore need the signature anchor instead. Deriving the set means adding
        a macOS slice cannot forget it: an omitted name makes every SIGNED release
        on that platform refuse its own decoder, because the digest cannot match
        post-signing and the signature anchor is never consulted for a name that is
        not listed.
        """
        expected = {
            FNAME_PER_PLATFORM[key]
            for key in transcribe._SHIPPED_FFMPEG_PLATFORMS
            if key.startswith("macos-")
        }
        assert transcribe._SIGNER_REWRITTEN_FFMPEG_ARTIFACTS == expected
        # Authenticating-by-signature a name the module never pins would be a claim
        # about nothing.
        assert transcribe._SIGNER_REWRITTEN_FFMPEG_ARTIFACTS <= set(
            transcribe._PACKAGED_FFMPEG_ARTIFACTS
        )

    @pytest.mark.asyncio
    async def test_authenticated_bytes_remain_bound_until_spawn(self, monkeypatch, tmp_path):
        binary = self._fake_package(monkeypatch, tmp_path)
        opened = transcribe._open_packaged_ffmpeg_resource()
        assert opened is not None
        descriptor = opened.descriptor

        if _pc.IS_WINDOWS:
            # CreateFileW denies writes and replacement while CreateProcess
            # opens this exact image.
            with pytest.raises(PermissionError):
                binary.write_bytes(b"changed decoder")
        else:
            # The source pathname may change freely: execution uses the sealed
            # memfd/private snapshot, never this name again.
            binary.write_bytes(b"changed decoder")

        sentinel = object()

        async def fake_spawn(execution_path, *args, **kwargs):
            os.fstat(descriptor)  # still open for the whole spawn operation
            if _pc.IS_WINDOWS:
                assert execution_path == str(binary)
            else:
                assert kwargs["pass_fds"] == (descriptor,)
                os.lseek(descriptor, 0, os.SEEK_SET)
                assert os.read(descriptor, 1024) == b"bundled decoder"
            return sentinel

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        assert await transcribe._create_ffmpeg_subprocess(opened, "-version") is sentinel
        with pytest.raises(OSError):
            os.fstat(descriptor)

    @pytest.mark.asyncio
    async def test_authenticated_handle_closes_off_event_loop(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path)
        opened = transcribe._open_packaged_ffmpeg_resource()
        assert opened is not None
        event_loop_thread = threading.get_ident()
        close_threads = []
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            if self.descriptor >= 0:
                close_threads.append(threading.get_ident())
            original_close(self)

        async def fake_spawn(*_args, **_kwargs):
            return object()

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        await transcribe._create_ffmpeg_subprocess(opened, "-version")

        assert len(close_threads) == 1
        assert close_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_pre_spawn_temp_failure_closes_handle_off_event_loop(self, monkeypatch, tmp_path):
        self._fake_package(monkeypatch, tmp_path)
        event_loop_thread = threading.get_ident()
        close_threads = []
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            if self.descriptor >= 0:
                close_threads.append(threading.get_ident())
            original_close(self)

        def disk_full():
            raise OSError("disk full")

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)
        monkeypatch.setattr(transcribe, "_make_temp_wav", disk_full)

        with pytest.raises(OSError, match="disk full"):
            await transcribe._pcm_via_ffmpeg(str(tmp_path / "memo.webm"), 1)

        assert len(close_threads) == 1
        assert close_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_cancelled_resolver_closes_eventual_handle_off_loop(self, monkeypatch, tmp_path):
        binary = tmp_path / "ffmpeg"
        binary.write_bytes(b"decoder")
        descriptor = os.open(binary, os.O_RDONLY)
        opened = transcribe._AuthenticatedFfmpeg(str(binary), descriptor, str(binary))
        event_loop_thread = threading.get_ident()
        resolver_started = threading.Event()
        release_resolver = threading.Event()
        handle_closed = threading.Event()
        close_threads = []
        original_close = transcribe._AuthenticatedFfmpeg.close

        def recording_close(self):
            if self.descriptor >= 0:
                close_threads.append(threading.get_ident())
            original_close(self)
            handle_closed.set()

        def slow_resolver():
            resolver_started.set()
            release_resolver.wait(timeout=5)
            return opened

        monkeypatch.setattr(transcribe._AuthenticatedFfmpeg, "close", recording_close)
        monkeypatch.setattr(transcribe, "_open_ffmpeg_for_execution", slow_resolver)
        task = asyncio.create_task(transcribe._resolve_ffmpeg_for_execution())
        assert await asyncio.to_thread(resolver_started.wait, 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release_resolver.set()
        assert await asyncio.to_thread(handle_closed.wait, 2)

        assert close_threads
        assert close_threads[0] != event_loop_thread

    def test_cwd_package_shadow_cannot_supply_the_decoder(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        shadow = repo / "imageio_ffmpeg"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text(
            "raise AssertionError('cwd package imported')\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(repo)
        monkeypatch.syspath_prepend(str(repo))
        binary = self._fake_package(monkeypatch, tmp_path / "trusted")

        assert transcribe._bundled_ffmpeg() == str(binary)

    def test_resolved_binary_cannot_escape_the_binaries_directory(self, monkeypatch, tmp_path):
        attacker = tmp_path / "attacker-ffmpeg"
        attacker.write_text("attacker decoder", encoding="utf-8")
        attacker.chmod(0o755)
        binary = self._fake_package(monkeypatch, tmp_path)
        realpath = transcribe.os.path.realpath

        def escape_candidate(path):
            resolved = realpath(path)
            if resolved == str(binary):
                return str(attacker)
            return resolved

        monkeypatch.setattr(transcribe.os.path, "realpath", escape_candidate)

        assert transcribe._bundled_ffmpeg() is None

    def test_bundle_wins_before_every_system_candidate(self, monkeypatch, tmp_path):
        binary = tmp_path / "bundle" / ("ffmpeg.exe" if _pc.IS_WINDOWS else "ffmpeg")
        monkeypatch.setattr(transcribe.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(transcribe, "_bundled_ffmpeg", lambda: str(binary))

        def _system_probe(*_args, **_kwargs):
            raise AssertionError("system lookup ran before the bundled decoder")

        monkeypatch.setattr(transcribe.shutil, "which", _system_probe)
        assert transcribe._find_ffmpeg() == str(binary)


class TestFfmpegCandidateDirsWindows:
    """Regression guards for the Windows ffmpeg discovery fix.

    Runs on POSIX CI by monkeypatching ``platform_compat.IS_WINDOWS`` (same
    pattern as ``TestTaskkillErrorMapping`` in ``test_platform_compat.py``) —
    the branch construction is platform-independent code.
    """

    def test_windows_dirs_appended(self, monkeypatch):
        from kiro_crew import platform_compat as pc
        from kiro_crew import transcribe as tr

        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        pf = r"C:\Program Files"
        la = r"C:\Users\user\AppData\Local"
        monkeypatch.setenv("ProgramFiles", pf)
        monkeypatch.setenv("LOCALAPPDATA", la)

        dirs = tr._ffmpeg_candidate_dirs()
        assert os.path.join(pf, "ffmpeg", "bin") in dirs
        assert os.path.join(la, "Programs", "ffmpeg", "bin") in dirs
        assert "/usr/local/bin" in dirs

    def test_non_windows_omits_windows_dirs(self, monkeypatch):
        from kiro_crew import platform_compat as pc
        from kiro_crew import transcribe as tr

        monkeypatch.setattr(pc, "IS_WINDOWS", False)
        dirs = tr._ffmpeg_candidate_dirs()
        for d in dirs:
            assert "Program Files" not in d
            assert "AppData" not in d

    def test_ensure_ffmpeg_probes_with_which(self, tmp_path, monkeypatch):
        """``ensure_ffmpeg_in_path`` must use ``shutil.which(name, path=d)`` so it
        catches ``ffmpeg.exe`` on Windows in addition to plain ``ffmpeg`` on POSIX.
        Regression: the prior implementation called ``os.path.isfile(<d>/ffmpeg)``
        which is blind to the ``.exe`` suffix.
        """
        from kiro_crew import transcribe as tr

        target_dir = tmp_path / "ffbin"
        target_dir.mkdir()

        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(target_dir)])
        monkeypatch.setenv("PATH", "/nowhere")

        calls: list[tuple[str, str]] = []

        def fake_which(name, path=None):
            calls.append((name, path or ""))
            return f"{path}/ffmpeg" if path == str(target_dir) else None

        monkeypatch.setattr(tr.shutil, "which", fake_which)
        tr.ensure_ffmpeg_in_path()

        assert calls and calls[0][0] == "ffmpeg"
        assert calls[0][1] == str(target_dir)
        assert os.environ["PATH"].startswith(str(target_dir))

    def test_ensure_ffmpeg_skips_dirs_already_on_path(self, tmp_path, monkeypatch):
        from kiro_crew import transcribe as tr

        target_dir = tmp_path / "ffbin"
        target_dir.mkdir()
        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(target_dir)])
        monkeypatch.setenv("PATH", f"{target_dir}{os.pathsep}/nowhere")

        called: list[str] = []

        def fake_which(name, path=None):
            called.append(name)
            return None

        monkeypatch.setattr(tr.shutil, "which", fake_which)
        tr.ensure_ffmpeg_in_path()

        assert called == []
        assert os.environ["PATH"].startswith(str(target_dir))


class TestFfmpegDiscoveryWindowsOnly:
    """Real, unmocked Windows behaviour — mkdir a fake install dir, drop an
    ``ffmpeg.exe`` inside, point ``_FFMPEG_CANDIDATE_DIRS`` at it, verify
    ``ensure_ffmpeg_in_path`` picks it up. Skipped on POSIX.
    """

    @pytest.mark.skipif(
        not _pc.IS_WINDOWS,
        reason="Windows-only: exercises PATHEXT-driven .exe suffix resolution.",
    )
    def test_ffmpeg_exe_discovered(self, tmp_path, monkeypatch):
        from kiro_crew import transcribe as tr

        ffbin = tmp_path / "ffbin"
        ffbin.mkdir()
        exe = ffbin / "ffmpeg.exe"
        exe.write_bytes(b"MZ")

        monkeypatch.setattr(tr, "_FFMPEG_CANDIDATE_DIRS", [str(ffbin)])
        monkeypatch.setenv("PATH", r"C:\\Windows\\System32")

        tr.ensure_ffmpeg_in_path()
        assert os.environ["PATH"].startswith(str(ffbin))


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
    @pytest.mark.asyncio
    async def test_none_credentials_raises(self):
        resolver = _ProfileCredentialResolver.__new__(_ProfileCredentialResolver)
        mock_session = MagicMock()
        mock_session.get_credentials.return_value = None
        mock_session.profile_name = "test-profile"
        resolver._session = mock_session
        mock_creds_module = MagicMock()
        with patch.dict(
            "sys.modules",
            {"amazon_transcribe": MagicMock(), "amazon_transcribe.auth": mock_creds_module},
        ):
            with pytest.raises(RuntimeError, match="No AWS credentials found"):
                await resolver.get_credentials()


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
    ``test_apple_speech.py::TestTranscodeTempOwnership`` (#5777).
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
        remove ``tmp_ogg``, and re-raise — the old ``except Exception`` guard
        did none of that (#5780)."""
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
        still remove ``tmp_ogg`` and let the cancellation propagate (#5780)."""
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
        event loop (#5834)."""
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


class TestFfmpegIsNotResolvedFromPath:
    """The resolved binary is exec'd by the gateway, so PATH must not choose it.

    A gateway's PATH can legitimately lead with agent-writable directories (a worktree
    venv's ``bin``, ``~/.local/bin``), which is the threat
    `platform_compat.trusted_system_bin` documents: a planted ``ffmpeg`` would run with
    the gateway's environment and credentials. `_find_ffmpeg` therefore searches fixed
    directories, most-trusted first, and never the ambient PATH.
    """

    @pytest.fixture(autouse=True)
    def _without_the_optional_bundle(self, monkeypatch):
        """Keep these fallback tests independent of installed optional extras."""
        monkeypatch.setattr(transcribe, "_bundled_ffmpeg", lambda: None)

    @staticmethod
    def _plant(directory):
        """Plant a findable ffmpeg, named the way the host's loader requires.

        ``.exe`` on Windows: `shutil.which` needs a PATHEXT match there, so a file
        named plainly ``ffmpeg`` is invisible to it. Without this the two positive
        assertions below failed on Windows, and — worse — the NEGATIVE one passed for
        the wrong reason: "PATH did not choose it" was true because nothing could have
        chosen it. An absence assertion that holds with the guard deleted guards
        nothing.
        """
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / ("ffmpeg.exe" if _pc.IS_WINDOWS else "ffmpeg")
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        binary.chmod(0o755)
        return binary

    @staticmethod
    def _same_file(found, expected):
        """Compare two paths the way the host's filesystem does.

        `shutil.which` returns the name it CONSTRUCTED, not the directory entry it
        matched: on Windows it appends each `PATHEXT` entry as spelled in the
        environment, which is uppercase, so a planted ``ffmpeg.exe`` comes back as
        ``ffmpeg.EXE``. `os.path.normcase` is identity on POSIX, where the two
        spellings really are different files.
        """
        return found is not None and os.path.normcase(found) == os.path.normcase(str(expected))

    def test_a_binary_only_on_path_is_never_chosen(self, tmp_path, monkeypatch):
        planted = self._plant(tmp_path / "evil")
        monkeypatch.setenv("PATH", str(planted.parent))
        # No fixed candidate holds one, so the honest answer is "not installed".
        monkeypatch.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(tmp_path / "nowhere")])
        monkeypatch.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        assert transcribe._find_ffmpeg() is None, "a PATH-planted ffmpeg was selected"

    def test_a_trusted_directory_wins_over_a_writable_candidate(self, tmp_path, monkeypatch):
        """Order is the guard: a writable dir must not SHADOW a system install."""
        system = self._plant(tmp_path / "system")
        writable = self._plant(tmp_path / "home")
        monkeypatch.setenv("PATH", str(writable.parent))
        monkeypatch.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(writable.parent)])
        monkeypatch.setattr(
            transcribe.platform_compat, "trusted_system_path", lambda: str(system.parent)
        )
        assert self._same_file(transcribe._find_ffmpeg(), system)

    def test_a_fixed_candidate_is_still_used_when_no_system_copy_exists(
        self, tmp_path, monkeypatch
    ):
        """The per-user dirs stay usable: refusing them makes voice memos undecodable
        on a host whose only ffmpeg was unzipped by hand."""
        candidate = self._plant(tmp_path / "opt")
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(candidate.parent)])
        monkeypatch.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        assert self._same_file(transcribe._find_ffmpeg(), candidate)

    def test_no_generic_user_writable_directory_is_a_candidate(self, monkeypatch):
        """Ordering them last was not enough, so they are not candidates at all.

        On a host with no packaged ffmpeg a trailing `~/.local/bin` was still trusted,
        and that directory exists to hold loose binaries on nearly every PATH. A
        package-manager root is a different proposition even when user-owned (Homebrew
        owns `/opt/homebrew` on Apple Silicon): planting there overwrites a managed
        file rather than adding a name.
        """
        for windows in (False, True):
            monkeypatch.setattr(transcribe.platform_compat, "IS_WINDOWS", windows)
            dirs = transcribe._ffmpeg_candidate_dirs()
            assert dirs, "the list must not be empty"
            for d in dirs:
                assert d not in (
                    os.path.expanduser("~/ffmpeg"),
                    os.path.expanduser("~/.local/bin"),
                ), f"{d} is a generic user-writable directory"
                assert os.path.basename(d.rstrip(os.sep)) in (
                    "bin",
                ), f"{d} is not a package-manager bin directory"
