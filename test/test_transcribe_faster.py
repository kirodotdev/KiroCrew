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

import sys
from contextlib import contextmanager
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
    _faster_whisper_model,
    _is_boilerplate_line,
    _run_faster_whisper_sync,
    filter_hallucinations,
    is_available,
    transcribe_audio,
)


@contextmanager
def _library_absent():
    """Simulate faster-whisper being uninstalled.

    Patching the cached class alone is not enough since the lazy helper retries
    the import — on a dev machine that happens to have the library, the retry
    would succeed and the "absent" test would silently test presence. Poisoning
    ``sys.modules`` makes the retry raise ImportError everywhere.
    """
    with patch.dict(sys.modules, {"faster_whisper": None}):
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            yield


@pytest.fixture(autouse=True)
def _clear_fw_model_cache():
    """Isolate the per-(model, device) instance cache between tests.

    The cache is a module global keyed on config values most tests share
    (turbo/cpu), so without clearing, one test's MagicMock model leaks into the
    next test's dispatch and every assertion after the first tests the cache,
    not the code.
    """
    from kiro_crew import transcribe

    transcribe._FW_MODEL_CACHE.clear()
    yield
    transcribe._FW_MODEL_CACHE.clear()


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

    @pytest.mark.parametrize("offmenu", ["tiny.en", "base.en", "small.en", "medium.en", "large-v2"])
    def test_offmenu_string_models_pass_through_with_a_warning(self, offmenu):
        # openai-whisper legitimately accepts names outside the dashboard's size
        # menu; a hand-edited config holding one must NOT be silently coerced to
        # turbo — that would remove a real capability the old loader allowed.
        assert _validated_stt_model(offmenu) == offmenu

    def test_unknown_string_passes_through_rather_than_coercing(self):
        # Providers degrade safely per-recording on a bad name (logged, non-fatal),
        # so the loader's job is to warn, not to rewrite the user's config.
        assert _validated_stt_model("large-v9") == "large-v9"

    @pytest.mark.parametrize("bad", ["", None, 42, ["small"]])
    def test_non_string_or_empty_model_falls_back_instead_of_raising(self, bad):
        # A mangled config field must not stop the Gateway from starting, and a
        # non-string cannot be handed to any provider at all.
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
        with _library_absent():
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


class TestLazyImportRetry:
    def test_helper_retries_the_import_after_an_on_demand_install(self):
        # The Settings install lands the library in this interpreter AFTER module
        # load cached None. Without a retry, the button reports "Done" while
        # availability stays False until a gateway restart.
        sentinel = MagicMock()
        fake_module = MagicMock(WhisperModel=sentinel)
        with patch("kiro_crew.transcribe._FasterWhisperModel", None):
            with patch.dict(sys.modules, {"faster_whisper": fake_module}):
                assert _faster_whisper_model() is sentinel

    def test_helper_returns_none_while_the_library_is_absent(self):
        with _library_absent():
            assert _faster_whisper_model() is None

    def test_helper_prefers_the_cached_class(self):
        cached = MagicMock()
        with patch("kiro_crew.transcribe._FasterWhisperModel", cached):
            assert _faster_whisper_model() is cached


class TestModelMemoization:
    def test_same_model_and_device_constructs_once(self):
        # Constructing a WhisperModel re-loads and re-quantizes the weights;
        # concurrent recordings each holding a copy compounds to RAM exhaustion.
        model_cls = MagicMock(return_value=_fake_model(["one"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            _run_faster_whisper_sync("/tmp/b.wav", "turbo", "cpu")
        assert model_cls.call_count == 1

    def test_distinct_keys_get_distinct_instances(self):
        model_cls = MagicMock(side_effect=lambda *a, **k: _fake_model(["x"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            _run_faster_whisper_sync("/tmp/a.wav", "small", "cpu")
        assert model_cls.call_count == 2

    def test_switching_models_evicts_the_previous_instance(self):
        # SINGLE-SLOT on purpose: keeping every size ever selected resident
        # would accumulate multi-GB native models and OOM a small gateway host.
        from kiro_crew import transcribe

        model_cls = MagicMock(side_effect=lambda *a, **k: _fake_model(["x"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            _run_faster_whisper_sync("/tmp/a.wav", "large-v3", "cpu")
            assert list(transcribe._FW_MODEL_CACHE) == [("large-v3", "cpu")]
            # Switching BACK constructs again — correctness over reload cost.
            _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu")
            assert list(transcribe._FW_MODEL_CACHE) == [("turbo", "cpu")]
        assert model_cls.call_count == 3

    def test_a_failed_construction_is_not_cached(self):
        # One bad load (e.g. interrupted download) must not poison every later
        # recording with a cached broken instance or a cached None.
        model_cls = MagicMock(side_effect=RuntimeError("load failed"))
        with patch("kiro_crew.transcribe._FasterWhisperModel", model_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") is None
        ok_cls = MagicMock(return_value=_fake_model(["recovered"]))
        with patch("kiro_crew.transcribe._FasterWhisperModel", ok_cls):
            assert _run_faster_whisper_sync("/tmp/a.wav", "turbo", "cpu") == "recovered"


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
        with _library_absent():
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
        with _library_absent():
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
            # A sentence CONTAINING a boilerplate phrase is not boilerplate: these
            # are real speech on the normal path, and deleting them is silent
            # content loss (the blocking finding this rule exists to prevent).
            "Thanks for joining today's standup, let's start with Priya.",
            "I really do thank you for watching over the rollout last week.",
            "The transcript is available in the shared drive for everyone.",
            "See you next time we meet in Boston, bring the roadmap.",
            # Even ONE extra word must spare the sentence — "Thanks for joining,
            # everyone." is a normal meeting opener, not an artefact.
            "Thanks for joining, everyone.",
            "Thanks for watching this, team.",
            # Ordinary-speech phrases were REMOVED from the list entirely: a
            # dictated farewell or rights notice is plausible real speech even
            # as a complete utterance, so it must never be filtered.
            "Goodbye.",
            "goodbye",
            "Copyright",
            "All rights reserved.",
            "Thanks for listening.",
            "Thanks for joining.",
            "See you next time!",
            "The transcript is available.",
        ],
    )
    def test_keeps_real_speech(self, line):
        assert _is_boilerplate_line(line) is False

    def test_all_phrases_require_a_whole_line_match(self):
        # Substring or word-count-proximity matching deletes real sentences that
        # merely mention (or lightly extend) a phrase — the cases above.
        assert _is_boilerplate_line("Thanks for watching") is True
        assert _is_boilerplate_line("We kept thanks for watching in the caption doc") is False

    def test_every_listed_phrase_is_implausible_as_dictation(self):
        # LIST DISCIPLINE: an entry that is also ordinary speech deletes genuine
        # dictation. Everything on the list must smell like video-caption
        # boilerplate, pinned here by requiring caption-domain vocabulary.
        import re as _re

        caption_markers = _re.compile(
            r"watch|subscribe|subtitle|caption|transcri|translat|video|bell|amara|mooji"
        )
        from kiro_crew.transcribe import _WHISPER_BOILERPLATE as phrases

        for phrase in phrases:
            assert caption_markers.search(phrase), (
                f"'{phrase}' has no caption-domain marker — plausible as real"
                " dictated speech, so it must not be on the filter list"
            )

    def test_known_artefact_variants_are_listed_as_full_phrases(self):
        # "Subtitles by Amara.org" is the canonical artefact shape; it matches by
        # being IN the phrase list, not by loosening the match rule.
        assert _is_boilerplate_line("Subtitles by Amara.org") is True
        assert _is_boilerplate_line("Subtitles by the Amara.org community") is True


class TestCollapseRepeatedPhrases:
    def test_collapses_a_long_run_to_one(self):
        text = " ".join(["Thank you."] * 12)
        assert _collapse_repeated_phrases(text) == "Thank you."

    def test_leaves_a_short_run_alone(self):
        # Real emphasis reaches well past two — "No. No. No." is ordinary
        # insistence, and even five repeats is plausible counted speech. Only
        # dozens-long runs are the Whisper artefact.
        for n in range(2, 6):
            text = " ".join(["No."] * n)
            assert _collapse_repeated_phrases(text) == text, n

    def test_only_consecutive_runs_collapse(self):
        # The same sentence recurring later in a meeting is ordinary speech.
        text = "Okay. Next item. Okay."
        assert _collapse_repeated_phrases(text) == "Okay. Next item. Okay."

    def test_preserves_surrounding_speech(self):
        run = " ".join(["Uh huh."] * 8)
        text = f"We start now. {run} Then we ship."
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
        run = " ".join(["Okay."] * 10)
        text = f"{run} Ship it. Please subscribe."
        assert filter_hallucinations(text) == "Okay. Ship it."


# ---------------------------------------------------------------------------
# Install path
# ---------------------------------------------------------------------------


class TestInstallScript:
    def test_installs_into_the_gateways_own_interpreter(self):
        # The library is imported IN-PROCESS by kiro_crew.transcribe, so the one
        # environment that matters is sys.executable's. A system python's
        # user-site would be invisible here — and inside a venv pip refuses
        # `--user` outright — so the script must target the gateway interpreter
        # and must not pass `--user`.
        script = _build_stt_install_script("faster")
        assert "pip install -q faster-whisper" in script
        assert "--user" not in script
        assert sys.executable in script

    def test_does_not_probe_for_a_system_python(self):
        # The $PY probe belongs to the CLI providers, whose binary any python can
        # own. Probing here risks installing into an interpreter the gateway
        # never imports from.
        assert "for py in" not in _build_stt_install_script("faster")

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
