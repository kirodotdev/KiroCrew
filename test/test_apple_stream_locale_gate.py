"""The streaming Apple helper's locale gate, pinned by source.

CI never compiles the Swift helper (macOS 26 with Speech assets only), so these
read ``StreamTranscribe.swift`` from the package directory ``_build_helper`` reads
it from, the same way ``test_swift_helper_still_accepts_fast`` pins ``--fast``.
"""

from pathlib import Path

from kiro_crew import apple_speech


def _stream_source() -> str:
    return Path(apple_speech.__file__).with_name("StreamTranscribe.swift").read_text("utf-8")


def _dictation_branch(src: str) -> str:
    """The text of the dictation module's locale gate, up to the speech branch."""
    start = src.index("DictationTranscriber.installedLocales")
    return src[start : src.index("guard match(speechSupported)", start)]


def test_dictation_is_gated_on_its_own_inventory_before_it_is_built():
    """The default module is DictationTranscriber, whose installed locales are a
    separate and usually much shorter list than SpeechTranscriber's. Gating on
    SpeechTranscriber's list lets a zh-CN session through to a dictation module
    with no model, and the user gets silence.
    """
    src = _stream_source()
    assert src.index("DictationTranscriber.installedLocales") < src.index(
        "DictationTranscriber(\n"
    ), "the dictation inventory must be checked before the module is built"


def test_missing_dictation_model_falls_back_to_the_speech_module():
    """With only the speech model installed, stream with it instead of failing.

    The batch helper's ``--install`` fetches exactly that model, so this is also
    what makes the error's hint actually fix the streaming path.
    """
    branch = _dictation_branch(_stream_source())
    assert "match(speechInstalled)" in branch
    assert 'moduleKind = "speech"' in branch


def test_errors_name_the_missing_model_not_the_audio_format():
    """The audio format is never the cause of a missing model, so no error blames it."""
    src = _stream_source()
    assert "no compatible audio format for locale" not in src
    assert "no on-device speech model is installed for locale" in _dictation_branch(src)
