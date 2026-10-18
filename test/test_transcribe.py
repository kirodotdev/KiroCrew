"""Tests for speech-to-text transcription of whole audio files.

Three providers reach this module: the resident local recogniser (the default),
Apple's on-device speech, and AWS Transcribe. Everything a test here needs about
the recogniser is stubbed at the ``kiro_crew.stt`` seam, because the point of the
batch path is what it does with an availability answer and a PCM buffer, not how
whisper.cpp decodes one.

This file holds availability, the local recogniser's language and WAV input,
the Slack voice-memo adapter, ``SttConfig``, and the two provider-independent
guards. The sibling ``test_transcribe_*.py`` files hold the rest:
``transcribe_audio``'s providers (``test_transcribe_providers.py``), the ffmpeg
decoder (``test_transcribe_ffmpeg.py``) and long recordings
(``test_transcribe_segments.py``); ``transcribe_helpers.py`` holds the WAV
builders the family shares.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import types
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from transcribe_helpers import (
    _ramp_int16,
    _write_wav,
)

from kiro_crew import stt, transcribe
from kiro_crew.config.loader import SttConfig
from kiro_crew.transcribe import availability_detail, find_brew, is_available, transcribe_audio


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

    def test_multichannel_folding_is_sliced_and_exact_across_the_slice_boundary(self, tmp_path):
        """A whole-file multichannel read holds the interleaved int16 buffer
        AND its float32 conversion at once — ~1.5 GiB for a four-channel hour,
        an OOM on an input the import byte cap admits (GPT review r23). The
        fold therefore runs in byte-bounded slices (8 MiB of raw int16 per
        slice — GPT review r34: bounding by SECONDS let the channel count
        scale the transient without limit); this file spans the slice boundary
        and must fold to exactly the same per-frame mean as a whole-file fold."""
        channels = 4
        # One slice is (8 MiB // (channels * 2)) frames; cross it by a second.
        frames = (8 * 1024 * 1024) // (channels * 2) + transcribe.stt.SAMPLE_RATE_HZ
        rng = np.random.default_rng(23)
        interleaved = rng.integers(-30_000, 30_000, frames * channels, dtype=np.int16)
        audio = tmp_path / "quad.wav"
        _write_wav(audio, interleaved, channels=channels)

        pcm = transcribe._pcm_from_wav(str(audio))

        assert pcm is not None and pcm.size == frames
        expected = (interleaved.astype(np.float32) / 32768.0).reshape(-1, channels).mean(axis=1)
        np.testing.assert_allclose(pcm, expected, rtol=0, atol=1e-7)

    def test_slice_byte_budget_is_channel_count_independent(self, tmp_path):
        """GPT review r34: a valid 256-channel WAV under the import size cap
        must not make one slice allocate hundreds of MiB. The slice frame
        count shrinks with the channel count, so the raw bytes read per
        ``readframes`` call stay bounded — pinned here by folding a
        256-channel file whose whole-file read would be ~50 MiB while each
        slice stays at 8 MiB, and checking exactness end to end."""
        channels = 256
        # One slice for 256 channels is (8 MiB // 512) = 16384 frames; span
        # three slices so the loop and the boundary are both exercised.
        frames = 16384 * 2 + 1000
        rng = np.random.default_rng(34)
        interleaved = rng.integers(-30_000, 30_000, frames * channels, dtype=np.int16)
        audio = tmp_path / "many.wav"
        _write_wav(audio, interleaved, channels=channels)

        pcm = transcribe._pcm_from_wav(str(audio))

        assert pcm is not None and pcm.size == frames
        expected = (interleaved.astype(np.float32) / 32768.0).reshape(-1, channels).mean(axis=1)
        np.testing.assert_allclose(pcm, expected, rtol=0, atol=1e-7)

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

        with (
            patch("kiro_crew.slack.events.batch_duration_cap_secs", return_value=None),
            patch(
                "kiro_crew.slack.events.transcribe_audio",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
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
        with (
            patch("kiro_crew.slack.events.batch_duration_cap_secs", return_value=None),
            patch(
                "kiro_crew.slack.events.transcribe_audio",
                new_callable=AsyncMock,
                return_value=None,
            ),
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
        assert cfg.language_code == "auto"
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

    @pytest.mark.parametrize("provider", ["local", "apple", "transcribe"])
    @pytest.mark.parametrize("language", ["auto", "AUTO", "", None, 42])
    def test_language_default_matches_provider_capabilities(self, provider, language):
        cfg = SttConfig(provider=provider, language_code=language)
        assert cfg.language_code == "auto"
        assert cfg.effective_language_code == ("auto" if provider == "local" else "en-US")

    @pytest.mark.parametrize("provider", ["local", "apple", "transcribe"])
    @pytest.mark.parametrize("language", ["en-US", "zh-CN"])
    def test_explicit_language_is_preserved(self, provider, language):
        cfg = SttConfig(provider=provider, language_code=language)
        assert cfg.language_code == language
        assert cfg.effective_language_code == language

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
        """Each names an out-of-band install or a device selector belonging to a
        provider that does not exist. Re-adding one would put a setting back in
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

    def test_the_gateways_own_voice_runtime_snapshot_is_not_refused(self, tmp_path, monkeypatch):
        """GPT review r30: the crew ``run/`` dir is a sensitive leaf, and the
        import snapshots are staged under ``run/voice-runtime`` precisely
        because agents cannot reach it — so without the exemption every import
        would be refused by the gateway's own guard and the feature would
        always fail."""
        root = tmp_path / "voice-root"
        root.mkdir()
        snap = root / "kc-meetings-x-import" / "recording.wav"
        snap.parent.mkdir()
        snap.write_bytes(b"bytes")
        monkeypatch.setattr(
            "kiro_crew.sandbox.prime_voice_runtime_sandbox_paths", lambda: str(root)
        )
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            assert transcribe._is_sensitive_audio_path(str(snap)) is False

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlinks")
    def test_a_link_planted_under_the_runtime_root_is_still_judged(self, tmp_path, monkeypatch):
        """The exemption is by REAL path: a link under the root resolves to its
        target outside it and is judged as whatever it points at."""
        root = tmp_path / "voice-root"
        root.mkdir()
        outside = tmp_path / "outside.wav"
        outside.write_bytes(b"bytes")
        link = root / "recording.wav"
        link.symlink_to(outside)
        monkeypatch.setattr(
            "kiro_crew.sandbox.prime_voice_runtime_sandbox_paths", lambda: str(root)
        )
        with patch("kiro_crew.security.is_sensitive_path", return_value=True):
            assert transcribe._is_sensitive_audio_path(str(link)) is True


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
