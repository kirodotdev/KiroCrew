"""Which ffmpeg ``stitch_mp3s`` runs.

The stitch runs in the gateway, outside the agent sandbox. The fixed
directories the transcriber trusts (``kiro_crew.transcribe._find_system_ffmpeg``)
come first, so a ``PATH`` entry such as ``~/.local/bin`` cannot shadow a trusted
ffmpeg. Only when none of them has one does the stitch use ``PATH``, as main
does, so no ffmpeg main finds is lost.
"""

from __future__ import annotations

import os
import shutil
import stat
from unittest.mock import patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _capture_mkstemp, _mock_subprocess

from kiro_crew import transcribe
from kiro_crew.voice_reply import stitch_mp3s

_TRUSTED_FFMPEG = os.path.join(os.sep, "usr", "bin", "ffmpeg")


def _two_inputs(tmp_path) -> list[str]:
    paths = []
    for name in ("a.mp3", "b.mp3"):
        p = tmp_path / name
        p.write_bytes(b"fake-mp3")
        paths.append(str(p))
    return paths


def _plant_on_path(tmp_path) -> str:
    """Put an executable named ffmpeg in a directory and make it all of PATH."""
    local_bin = tmp_path / "home" / ".local" / "bin"
    local_bin.mkdir(parents=True)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    planted = local_bin / name
    planted.write_bytes(b"not a real ffmpeg")
    planted.chmod(planted.stat().st_mode | stat.S_IXUSR)
    return str(local_bin)


@pytest.mark.asyncio
async def test_stitch_runs_the_trusted_ffmpeg_not_the_path_one(tmp_path) -> None:
    allocated: list[str] = []
    argv: list[str] = []

    async def fake_exec(*cmd, **kwargs):
        argv.extend(cmd)
        with open(cmd[-1], "wb") as f:
            f.write(b"stitched-mp3-data")
        return _mock_subprocess(returncode=0)

    with pytest.MonkeyPatch.context() as mp:
        allocated = _capture_mkstemp(mp)
        mp.setenv("PATH", _plant_on_path(tmp_path))
        mp.setattr(transcribe, "_find_system_ffmpeg", lambda: _TRUSTED_FFMPEG)
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(_two_inputs(tmp_path))

    try:
        assert result == allocated[0]
        assert argv[0] == _TRUSTED_FFMPEG
        assert os.path.isabs(argv[0])
    finally:
        for path in allocated:
            if os.path.exists(path):
                os.unlink(path)


@pytest.mark.asyncio
async def test_stitch_starts_nothing_when_no_ffmpeg_anywhere(tmp_path) -> None:
    """No trusted ffmpeg and none on PATH: nothing starts and the owned output
    is removed, as on main when ffmpeg is not installed."""
    empty = tmp_path / "empty"
    empty.mkdir()

    async def fake_exec(*cmd, **kwargs):
        raise AssertionError(f"no ffmpeg may start, got {cmd[0]!r}")

    with pytest.MonkeyPatch.context() as mp:
        allocated = _capture_mkstemp(mp)
        mp.setenv("PATH", str(empty))
        mp.setattr(transcribe, "_find_system_ffmpeg", lambda: None)
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec) as spawn:
            result = await stitch_mp3s(_two_inputs(tmp_path))

    assert result is None
    spawn.assert_not_called()
    assert len(allocated) == 1
    assert not os.path.exists(allocated[0])


@pytest.mark.asyncio
async def test_stitch_falls_back_to_an_ffmpeg_only_on_path(tmp_path) -> None:
    """End to end through the real resolver: with no ffmpeg in a trusted
    directory, the stitch still finds the one on PATH, as main does, and runs
    it by its absolute path."""
    empty = tmp_path / "empty"
    empty.mkdir()
    on_path = _plant_on_path(tmp_path)
    argv: list[str] = []

    async def fake_exec(*cmd, **kwargs):
        argv.extend(cmd)
        with open(cmd[-1], "wb") as f:
            f.write(b"stitched-mp3-data")
        return _mock_subprocess(returncode=0)

    with pytest.MonkeyPatch.context() as mp:
        allocated = _capture_mkstemp(mp)
        mp.setenv("PATH", on_path)
        mp.setattr(transcribe.platform_compat, "trusted_system_path", lambda: str(empty))
        mp.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(empty)])
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(_two_inputs(tmp_path))

    try:
        assert result == allocated[0]
        assert os.path.isabs(argv[0])
        assert os.path.normcase(os.path.dirname(argv[0])) == os.path.normcase(on_path)
    finally:
        for path in allocated:
            if os.path.exists(path):
                os.unlink(path)


def _plant(directory) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / ("ffmpeg.exe" if transcribe.platform_compat.IS_WINDOWS else "ffmpeg")
    binary.write_bytes(b"not a real ffmpeg")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return str(binary)


def test_the_trusted_lookup_finds_an_ffmpeg_in_a_fixed_directory(tmp_path) -> None:
    binary = _plant(tmp_path / "trusted")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        mp.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [os.path.dirname(binary)])
        found = transcribe._find_system_ffmpeg()

    assert found is not None
    assert os.path.normcase(found) == os.path.normcase(binary)


def test_the_trusted_lookup_refuses_a_hit_outside_the_directory(tmp_path) -> None:
    """On Windows ``shutil.which(name, path=dir)`` searches the working directory
    before *dir*. Stand in for that answer on every host: the lookup must refuse
    a hit whose directory is not the one it asked about."""
    elsewhere = _plant(tmp_path / "work")
    trusted = tmp_path / "trusted"
    trusted.mkdir()

    def _which_answers_elsewhere(_name, mode=os.F_OK | os.X_OK, path=None):
        return elsewhere

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "which", _which_answers_elsewhere)
        mp.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        mp.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(trusted)])
        assert transcribe._find_system_ffmpeg() is None


def test_a_working_directory_hit_does_not_hide_a_trusted_install(tmp_path) -> None:
    """On Windows ``shutil.which(name, path=dir)`` answers from the working
    directory before *dir*. Stand in for that on every host: the probe must still
    find the ffmpeg that IS in the trusted directory, so the stitch never falls
    back to ``PATH`` while a trusted copy exists."""
    binary = _plant(tmp_path / "trusted")
    elsewhere = _plant(tmp_path / "work")
    real_which = shutil.which

    def _which_prefers_the_working_directory(name, mode=os.F_OK | os.X_OK, path=None):
        if os.path.dirname(name):
            return real_which(name, mode)
        return elsewhere

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(shutil, "which", _which_prefers_the_working_directory)
        mp.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        mp.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [os.path.dirname(binary)])
        found = transcribe._find_system_ffmpeg()

    assert found is not None
    assert os.path.normcase(os.path.realpath(found)) == os.path.normcase(os.path.realpath(binary))


@pytest.mark.skipif(
    not transcribe.platform_compat.IS_WINDOWS,
    reason="Windows-only: only Windows searches the working directory for a program.",
)
def test_an_ffmpeg_in_the_working_directory_does_not_hide_a_trusted_one(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "ffmpeg.exe").write_bytes(b"MZ")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "ffmpeg.exe").write_bytes(b"MZ")

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
        mp.chdir(work)
        mp.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        mp.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(trusted)])
        found = transcribe._find_system_ffmpeg()

    assert found is not None
    assert os.path.normcase(os.path.dirname(found)) == os.path.normcase(str(trusted))


@pytest.mark.skipif(
    not transcribe.platform_compat.IS_WINDOWS,
    reason="Windows-only: only Windows searches the working directory for a program.",
)
def test_an_ffmpeg_in_the_working_directory_is_not_chosen(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "ffmpeg.exe").write_bytes(b"MZ")
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.MonkeyPatch.context() as mp:
        mp.chdir(work)
        mp.setattr(transcribe.platform_compat, "trusted_system_path", lambda: None)
        mp.setattr(transcribe, "_FFMPEG_CANDIDATE_DIRS", [str(empty)])
        assert transcribe._find_system_ffmpeg() is None
