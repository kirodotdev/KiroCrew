"""The stitch's ``PATH`` fallback never answers with the working directory (#39118).

On Windows ``shutil.which("ffmpeg")`` searches the working directory before
``PATH`` unless ``NoDefaultCurrentDirectoryInExePath`` is set, so an
``ffmpeg.exe`` beside the gateway ran for the voice reply stitch, outside the
agent sandbox, with no ``PATH`` entry naming it. A relative ``PATH`` entry such
as ``.`` names the same folder on every OS.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _capture_mkstemp, _mock_subprocess

from kiro_crew import transcribe
from kiro_crew.voice_reply import stitch_mp3s


def _two_inputs(tmp_path) -> list[str]:
    paths = []
    for name in ("a.mp3", "b.mp3"):
        p = tmp_path / name
        p.write_bytes(b"fake-mp3")
        paths.append(str(p))
    return paths


def _plant(directory) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    binary.write_bytes(b"not a real ffmpeg")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return str(directory)


def _plant_in_working_directory(tmp_path, mp) -> None:
    cwd = tmp_path / "gateway-cwd"
    _plant(cwd)
    mp.chdir(cwd)


@pytest.mark.asyncio
@pytest.mark.parametrize("relative_entry", [False, True], ids=["absolute-path-only", "dot-on-path"])
async def test_stitch_never_runs_an_ffmpeg_from_the_working_directory(
    tmp_path, relative_entry
) -> None:
    """No trusted ffmpeg and none in an absolute PATH entry: the decoy in the
    working directory does not start, and the stitch drops the replay clip as
    it does when no ffmpeg is installed."""
    empty = tmp_path / "empty"
    empty.mkdir()
    started: list[str] = []

    async def fake_exec(*cmd, **kwargs):
        started.append(cmd[0])
        return _mock_subprocess(returncode=1)

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
        inputs = _two_inputs(tmp_path)
        _plant_in_working_directory(tmp_path, mp)
        entries = [".", str(empty)] if relative_entry else [str(empty)]
        mp.setenv("PATH", os.pathsep.join(entries))
        mp.setattr(transcribe, "_find_system_ffmpeg", lambda: None)
        allocated = _capture_mkstemp(mp)
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(inputs)

    assert started == [], f"an ffmpeg from the working directory started: {started}"
    assert result is None
    assert not os.path.exists(allocated[0])


@pytest.mark.asyncio
async def test_an_absolute_path_entry_still_wins_over_the_working_directory(tmp_path) -> None:
    """The PATH fallback #34751 kept still works: the ffmpeg in an absolute
    PATH entry runs, by its absolute path, even with a decoy beside the gateway."""
    argv: list[str] = []

    async def fake_exec(*cmd, **kwargs):
        argv.extend(cmd)
        with open(cmd[-1], "wb") as f:
            f.write(b"stitched-mp3-data")
        return _mock_subprocess(returncode=0)

    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
        inputs = _two_inputs(tmp_path)
        on_path = _plant(tmp_path / "home" / ".local" / "bin")
        _plant_in_working_directory(tmp_path, mp)
        mp.setenv("PATH", on_path)
        mp.setattr(transcribe, "_find_system_ffmpeg", lambda: None)
        allocated = _capture_mkstemp(mp)
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await stitch_mp3s(inputs)

    try:
        assert result == allocated[0]
        assert os.path.isabs(argv[0])
        assert os.path.normcase(os.path.dirname(argv[0])) == os.path.normcase(on_path)
    finally:
        for path in allocated:
            if os.path.exists(path):
                os.unlink(path)
