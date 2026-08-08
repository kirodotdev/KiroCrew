"""The ``faster`` (faster-whisper) STT provider, and the hallucination filter.

Two things under test, and they are separable:

* The provider — dispatch, availability, and the fact that it needs neither a
  subprocess nor the system ffmpeg the CLI providers depend on.
* The hallucination filter — pure text logic applied to every Whisper-family
  provider. It matters because transcripts here go to agents: a hallucinated
  sign-off becomes a meeting note, and a phrase repeated forty times becomes
  forty note lines.

``faster_whisper`` is not installed (it is an on-demand runtime, not a declared
extra), so the library itself is always patched. That is the same situation CI is
in, which is the point.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.loader import (
    _VALID_STT_MODELS,
    _VALID_STT_PROVIDERS,
    SttConfig,
    _validated_stt_model,
)
from kiro_crew.dashboard.handlers.core import (
    _STT_MODEL_SIZES,
    _build_stt_install_script,
    _stt_prereq_commands,
)
from kiro_crew.transcribe import (
    _WHISPER_FAMILY_PROVIDERS,
    _collapse_repeated_phrases,
    _is_boilerplate_line,
    _run_faster_whisper_sync,
    filter_hallucinations,
    is_available,
    transcribe_audio,
)


def _fake_model(text_segments: list[str]) -> MagicMock:
    """A stand-in for ``faster_whisper.WhisperModel`` yielding *text_segments*."""
    model = MagicMock()
    model.transcribe.return_value = (
        iter([MagicMock(text=t) for t in text_segments]),
        MagicMock(),
    )
    return model


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestProviderRegistration:
    def test_faster_is_a_valid_provider(self):
        assert "faster" in _VALID_STT_PROVIDERS

    def test_faster_is_in_the_whisper_family(self):
        # Which is what subjects it to the hallucination filter.
        assert "faster" in _WHISPER_FAMILY_PROVIDERS

    def test_transcribe_is_not_in_the_whisper_family(self):
        # AWS Transcribe uses a different decoder and does not produce these
        # artefacts, so filtering it could only ever delete real speech.
        assert "transcribe" not in _WHISPER_FAMILY_PROVIDERS


# ---------------------------------------------------------------------------
# Model enum
# ---------------------------------------------------------------------------


class TestModelEnum:
    def test_turbo_remains_the_default(self):
        assert SttConfig().model == "turbo"

    def test_every_size_is_accepted(self):
        for model in _VALID_STT_MODELS:
            assert _validated_stt_model(model) == model

    @pytest.mark.parametrize("bad", ["large-v9", "", "TURBO", None, 42, ["small"]])
    def test_unknown_model_falls_back_instead_of_raising(self, bad):
        # A typo in one config field must not stop the Gateway from starting.
        assert _validated_stt_model(bad) == "turbo"

    def test_dashboard_offers_a_size_for_every_valid_model(self):
        # `_STT_MODEL_SIZES` is the dashboard's PUT allowlist, so a model the config
        # loader accepts but this dict omits would be silently rejected by the API.
        assert set(_STT_MODEL_SIZES) == set(_VALID_STT_MODELS)

    def test_every_size_is_human_readable(self):
        for model, size in _STT_MODEL_SIZES.items():
            assert size.startswith("~"), model
            assert size.endswith(("MB", "GB")), model


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_available_when_the_library_imports(self):
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", MagicMock()):
            assert is_available(cfg) is True

    def test_unavailable_when_the_library_is_missing(self):
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            assert is_available(cfg) is False

    def test_does_not_probe_for_ffmpeg(self):
        # faster-whisper decodes in-process through PyAV's bundled FFmpeg, so the
        # system binary is irrelevant. Probing for it would make availability depend
        # on something this provider never calls.
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", MagicMock()):
            with patch("kiro_crew.transcribe.ensure_ffmpeg_in_path") as ensure:
                assert is_available(cfg) is True
        ensure.assert_not_called()

    def test_disabled_beats_available(self):
        cfg = SttConfig(enabled=False, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", MagicMock()):
            assert is_available(cfg) is False


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


class TestRunFasterWhisperSync:
    def test_joins_segment_text(self):
        model_cls = MagicMock(return_value=_fake_model([" Hello ", "world. ", "  "]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") == "Hello world."

    def test_quantises_to_int8_on_the_configured_device(self):
        # int8 is what makes CPU inference fast enough to be usable on a
        # meeting-length recording.
        model_cls = MagicMock(return_value=_fake_model(["hi"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "small", "cuda")
        model_cls.assert_called_once_with("small", device="cuda", compute_type="int8")

    def test_empty_output_is_none_not_empty_string(self):
        model_cls = MagicMock(return_value=_fake_model(["   ", ""]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None

    def test_returns_none_when_the_library_is_missing(self):
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None

    def test_an_inference_failure_is_logged_not_raised(self):
        # Same contract as every other provider: one bad recording must not take a
        # caller down.
        model_cls = MagicMock(side_effect=RuntimeError("model load failed"))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None


class TestDispatch:
    @pytest.mark.asyncio
    async def test_faster_provider_is_dispatched(self, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster", model="small", device="cpu")
        model_cls = MagicMock(return_value=_fake_model(["Real speech here."]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            result = await transcribe_audio(str(audio), cfg)
        assert result == "Real speech here."

    @pytest.mark.asyncio
    async def test_does_not_shell_out_or_need_ffmpeg(self, tmp_path):
        # The reason this provider is worth having: no binary discovery at all.
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        model_cls = MagicMock(return_value=_fake_model(["ok"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            with patch("kiro_crew.transcribe.ensure_ffmpeg_in_path") as ensure:
                with patch("kiro_crew.transcribe._run_whisper_cli") as cli:
                    assert await transcribe_audio(str(audio), cfg) == "ok"
        ensure.assert_not_called()
        cli.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_library_returns_none(self, tmp_path):
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            assert await transcribe_audio(str(audio), cfg) is None

    @pytest.mark.asyncio
    async def test_hallucinated_output_becomes_none(self, tmp_path):
        # The whole point of the filter being inside transcribe_audio: a recording of
        # silence must come back as "no transcript", not as boilerplate for an agent
        # to write into the meeting notes.
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        cfg = SttConfig(enabled=True, provider="faster")
        model_cls = MagicMock(return_value=_fake_model(["Thank you for watching."]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert await transcribe_audio(str(audio), cfg) is None


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------


class TestBoilerplateDetection:
    @pytest.mark.parametrize(
        "line",
        [
            "Thank you for watching",
            "thank you for watching.",
            "  Thanks for watching!  ",
            "Please subscribe.",
            "Subtitles by Amara.org",
            "goodbye",
            "Copyright",
        ],
    )
    def test_detects_boilerplate(self, line):
        assert _is_boilerplate_line(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "Let's ship the recording change on Friday.",
            "The copyright review is blocked on legal.",
            "I said goodbye to the old design.",
        ],
    )
    def test_keeps_real_speech(self, line):
        assert _is_boilerplate_line(line) is False

    def test_single_word_phrases_require_a_whole_line_match(self):
        # Substring-matching "goodbye" or "copyright" would delete real sentences
        # that merely mention them — the two cases above.
        assert _is_boilerplate_line("goodbye") is True
        assert _is_boilerplate_line("We said goodbye and moved on") is False


class TestCollapseRepeatedPhrases:
    def test_collapses_a_long_run_to_one(self):
        text = "Thank you. Thank you. Thank you. Thank you."
        assert _collapse_repeated_phrases(text) == "Thank you."

    def test_leaves_a_short_run_alone(self):
        # Twice is emphasis; three times is the artefact.
        text = "Yes. Yes."
        assert _collapse_repeated_phrases(text) == "Yes. Yes."

    def test_only_consecutive_runs_collapse(self):
        # The same sentence recurring later in a meeting is ordinary speech.
        text = "Okay. Next item. Okay."
        assert _collapse_repeated_phrases(text) == "Okay. Next item. Okay."

    def test_preserves_surrounding_speech(self):
        text = "We start now. Uh huh. Uh huh. Uh huh. Then we ship."
        assert _collapse_repeated_phrases(text) == "We start now. Uh huh. Then we ship."

    def test_single_sentence_is_untouched(self):
        assert _collapse_repeated_phrases("Just one sentence") == "Just one sentence"


class TestFilterHallucinations:
    def test_empty_input_is_returned_as_is(self):
        assert filter_hallucinations("") == ""

    def test_real_speech_survives_intact(self):
        text = "We agreed to ship on Friday. Priya owns the rollout."
        assert filter_hallucinations(text) == text

    def test_a_fully_hallucinated_transcript_becomes_empty(self):
        # Which the caller turns into None. An empty string is the honest answer for
        # a recording of silence.
        assert filter_hallucinations("Thank you for watching. Please subscribe.") == ""

    def test_strips_boilerplate_but_keeps_the_meeting(self):
        text = "Priya owns the rollout. Thanks for watching! We ship Friday."
        assert filter_hallucinations(text) == "Priya owns the rollout. We ship Friday."

    def test_handles_both_artefacts_together(self):
        text = "Okay. Okay. Okay. Okay. Ship it. Please subscribe."
        assert filter_hallucinations(text) == "Okay. Ship it."


# ---------------------------------------------------------------------------
# Install path
# ---------------------------------------------------------------------------


class TestInstallScript:
    def test_installs_faster_whisper_via_pip(self):
        script = _build_stt_install_script("faster")
        assert "pip install -q --user faster-whisper" in script

    def test_does_not_install_ffmpeg(self):
        # It is not needed, and installing it would make the button slower and more
        # failure-prone for no benefit.
        script = _build_stt_install_script("faster")
        assert "brew install ffmpeg" not in script
        assert "openai-whisper" not in script

    def test_documents_the_windows_arm_gap(self):
        # CTranslate2 publishes no wheel there, so the install cannot succeed and the
        # script should say why rather than fail opaquely.
        assert "Windows on ARM" in _build_stt_install_script("faster")

    def test_includes_the_path_prelude(self):
        # A brew-installed python3 is common on macOS, and the gateway's inherited
        # PATH does not contain the Homebrew prefix.
        assert "brew shellenv" in _build_stt_install_script("faster")

    def test_emits_the_progress_line_the_status_parser_matches(self):
        # `_stt_install_status` keys the `installing_faster` step off this exact text.
        assert "Installing faster-whisper" in _build_stt_install_script("faster")

    def test_requires_no_manual_prerequisites(self):
        assert _stt_prereq_commands("faster") == []
