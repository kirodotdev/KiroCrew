"""The Windows missing-ffmpeg Fix line names only remedies the resolver can see.

The Windows twin of ``TestFfmpegLinuxHintResolvable`` in ``test_cli_doctor.py``.
winget's ``Gyan.FFmpeg`` lands under ``WinGet\\Packages``, which
``_ffmpeg_candidate_dirs`` never searches, so the hint is held against the
resolver's own Windows candidate list. Runs on any host: the platform is patched.
"""

from __future__ import annotations

from kiro_crew import platform_compat, transcribe
from kiro_crew.doctor_checks import features
from kiro_crew.stt import decoder


def test_hint_does_not_name_winget() -> None:
    assert "winget" not in features._FFMPEG_WINDOWS_HINT.lower()


def test_doctor_uses_the_windows_hint_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(features._plat, "system", lambda: "Windows")
    hint = features._os_fix_hint(
        "brew install ffmpeg",
        features._FFMPEG_LINUX_HINT,
        windows=features._FFMPEG_WINDOWS_HINT,
    )
    assert hint == features._FFMPEG_WINDOWS_HINT


def test_every_directory_named_is_actually_searched(monkeypatch) -> None:
    program_files = r"C:\Program Files"
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setenv("ProgramFiles", program_files)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    # This host's os.path may join with "/", so compare with one separator.
    searched = [d.replace("\\", "/") for d in transcribe._ffmpeg_candidate_dirs()]
    dirs = [
        token.replace("%ProgramFiles%", program_files).replace("\\", "/")
        for token in features._FFMPEG_WINDOWS_HINT.split()
        if "\\" in token
    ]
    assert dirs, "the hint must name at least one concrete install directory"
    for directory in dirs:
        assert (
            directory in searched
        ), f"{directory} is in the doctor hint but _find_ffmpeg never searches it"


def test_candidate_dirs_hold_no_winget_folder(monkeypatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert not any("WinGet" in d for d in transcribe._ffmpeg_candidate_dirs())


def test_hint_offers_the_managed_store_download() -> None:
    # Anchored to the decoder table: without a pinned windows-x86_64 artifact the
    # hint would promise a download the gateway refuses.
    assert "dashboard" in features._FFMPEG_WINDOWS_HINT
    assert decoder.artifact_for("Windows", "AMD64") is not None
