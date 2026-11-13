"""The doctor and the pipeline find ffmpeg/ffprobe through one lookup.

deps.py (the doctor), narrate.py and verify_align.py all fall back to
``~/.local/bin`` when a tool is not on PATH, and all three resolve through
``_toolpath.which_any``. It probes each directory with
``shutil.which(name, path=dir)``, which applies ``PATHEXT``, so ``ffprobe.exe``
there is found on Windows. A bare-path test of ``~/.local/bin/ffprobe`` would
miss it and leave a red doctor row and ``SystemExit`` from the pipeline.

The real-file tests run on every platform, and on a Windows runner they exercise
``PATHEXT`` itself. The emulated tests reproduce the Windows miss on POSIX by
giving ``shutil.which`` Windows' suffix rule, so the defect is pinned on every
runner, not only on Windows.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
from types import SimpleNamespace

import pytest
from skill_script_helpers import load_skill_script

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REFS = (
    _REPO_ROOT / "src/kiro_crew/apps/builtins/dev_fleet/skills/feature-demo-recording/references"
)
_WIN = sys.platform == "win32"
_REAL_WHICH = shutil.which


@pytest.fixture
def refs(monkeypatch):
    """Load the three scripts without leaking their sys.path insert or helpers."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    for helper in ("_pathcheck", "_toolpath"):
        monkeypatch.setitem(sys.modules, helper, sys.modules.get(helper))
        monkeypatch.delitem(sys.modules, helper)
    return SimpleNamespace(
        deps=load_skill_script("kc_video_deps_toolpath", _REFS / "deps.py"),
        narrate=load_skill_script("kc_video_narrate_toolpath", _REFS / "narrate.py"),
        verify=load_skill_script("kc_video_verify_toolpath", _REFS / "verify_align.py"),
    )


@pytest.fixture
def home(refs, monkeypatch, tmp_path):
    """A scratch home every script resolves, and a PATH holding nothing."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    for mod in (refs.deps, refs.narrate, refs.verify):
        monkeypatch.setattr(mod, "_home", lambda: home)
    monkeypatch.setenv("PATH", str(empty))
    # imageio-ffmpeg is a separate fallback; keep it out so only the lookup answers.
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    # check_ffmpeg runs the binary to look for libx264; the files here are stubs.
    monkeypatch.setattr(refs.deps, "run", lambda _cmd: SimpleNamespace(stdout="V libx264"))
    return home


def _place(directory: pathlib.Path, filename: str, *, executable: bool = True) -> pathlib.Path:
    path = directory / filename
    path.write_bytes(b"")
    path.chmod(0o755 if executable else 0o644)
    return path


def _native(name: str) -> str:
    return f"{name}.exe" if _WIN else name


def _windows_which(cmd, mode=os.F_OK | os.X_OK, path=None):
    """shutil.which with Windows' PATHEXT rule, on any host.

    Windows tries the bare name and then each PATHEXT suffix in every directory it
    searches; POSIX tries only the bare name. Reproducing the suffix step lets a
    POSIX runner hold the lookup to the behaviour Windows users get.
    """
    found = _REAL_WHICH(cmd, mode=mode, path=path)
    if found or cmd.lower().endswith(".exe"):
        return found
    return _REAL_WHICH(cmd + ".exe", mode=mode, path=path)


def _lookups(refs, binary):
    """Every call path that resolves *binary*: the doctor's and the pipeline's."""
    check = {"ffmpeg": refs.deps.check_ffmpeg, "ffprobe": refs.deps.check_ffprobe}[binary]

    def doctor_row() -> str | None:
        row = check()
        return row["detail"].removesuffix(" (libx264 present)") if row["ok"] else None

    return {
        "deps.which_any": lambda: refs.deps.which_any(binary, refs.deps._ffmpeg_dirs()),
        "deps.check": doctor_row,
        "narrate.tool": lambda: refs.narrate.tool(binary),
        "verify_align.tool": lambda: refs.verify.tool(binary),
    }


@pytest.mark.parametrize("binary", ["ffmpeg", "ffprobe"])
def test_exe_in_local_bin_found_by_doctor_and_pipeline(refs, home, monkeypatch, binary):
    """The Windows case: only ``<name>.exe`` is in ~/.local/bin and PATH is empty."""
    monkeypatch.setattr(shutil, "which", _windows_which)
    exe = _place(home / ".local" / "bin", f"{binary}.exe")
    # Windows' own shutil.which spells the suffix from PATHEXT (``.EXE``), so
    # compare paths the way the filesystem does.
    results = {
        site: os.path.normcase(call() or "") for site, call in _lookups(refs, binary).items()
    }
    assert results == dict.fromkeys(results, os.path.normcase(str(exe)))


@pytest.mark.parametrize("binary", ["ffmpeg", "ffprobe"])
def test_native_binary_in_local_bin_found_by_doctor_and_pipeline(refs, home, binary):
    """Real ``shutil.which``: ``ffprobe`` on POSIX, ``ffprobe.exe`` on Windows."""
    exe = _place(home / ".local" / "bin", _native(binary))
    for site, call in _lookups(refs, binary).items():
        found = call()
        assert found is not None, site
        assert os.path.normcase(found) == os.path.normcase(str(exe)), site


def test_explicit_exe_name_is_found(refs, home, monkeypatch):
    monkeypatch.setattr(shutil, "which", _windows_which)
    exe = _place(home / ".local" / "bin", "ffprobe.exe")
    assert refs.narrate.tool("ffprobe.exe") == str(exe)
    assert refs.verify.tool("ffprobe.exe") == str(exe)
    assert refs.deps.which_any("ffprobe.exe", refs.deps._ffmpeg_dirs()) == str(exe)


def test_path_still_wins_over_local_bin(refs, home, monkeypatch, tmp_path):
    on_path = tmp_path / "on-path"
    on_path.mkdir()
    first = _place(on_path, _native("ffprobe"))
    _place(home / ".local" / "bin", _native("ffprobe"))
    monkeypatch.setenv("PATH", str(on_path))
    for site, call in _lookups(refs, "ffprobe").items():
        assert os.path.normcase(call()) == os.path.normcase(str(first)), site


def test_missing_tool_is_none_for_the_doctor_and_the_install_hint_for_the_pipeline(refs, home):
    assert refs.deps.which_any("ffprobe", refs.deps._ffmpeg_dirs()) is None
    assert refs.deps.check_ffprobe()["ok"] is False
    for tool in (refs.narrate.tool, refs.verify.tool):
        with pytest.raises(SystemExit) as exc:
            tool("ffprobe")
        assert "deps.py --install" in str(exc.value)


@pytest.mark.skipif(_WIN, reason="POSIX execute bit; Windows has no X_OK distinction")
def test_non_executable_file_in_local_bin_is_rejected_everywhere(refs, home):
    _place(home / ".local" / "bin", "ffprobe", executable=False)
    assert refs.deps.which_any("ffprobe", refs.deps._ffmpeg_dirs()) is None
    for tool in (refs.narrate.tool, refs.verify.tool):
        with pytest.raises(SystemExit):
            tool("ffprobe")


def test_a_directory_named_like_the_tool_is_not_a_hit(refs, home):
    (home / ".local" / "bin" / _native("ffprobe")).mkdir()
    assert refs.deps.which_any("ffprobe", refs.deps._ffmpeg_dirs()) is None


def test_doctor_and_pipeline_search_the_same_directories(refs, home):
    # One list, so a directory added for the doctor cannot be missed by the pipeline.
    toolpath = sys.modules["_toolpath"]
    assert refs.deps._ffmpeg_dirs() == toolpath.user_bin_dirs(home)
    assert refs.deps.which_any is toolpath.which_any
    assert refs.narrate.which_any is toolpath.which_any
    assert refs.verify.which_any is toolpath.which_any


def _cwd_first_which(cmd, mode=os.F_OK | os.X_OK, path=None):
    """shutil.which with Windows' rule that a bare name tries the cwd first, ``path=`` or not."""
    if os.path.dirname(cmd):
        return _REAL_WHICH(cmd, mode)
    return _REAL_WHICH(cmd, mode, os.pathsep.join([os.curdir, path or os.environ["PATH"]]))


@pytest.mark.skipif(_WIN, reason="emulates the Windows lookup on POSIX")
def test_windows_lookup_skips_the_working_directory(refs, home, monkeypatch, tmp_path):
    cwd, on_path = tmp_path / "cwd", tmp_path / "on-path"
    cwd.mkdir()
    on_path.mkdir()
    _place(cwd, "ffmpeg.EXE")
    real = _place(on_path, "ffmpeg.EXE")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(on_path)]))
    monkeypatch.delenv("PATHEXT", raising=False)
    monkeypatch.setattr(shutil, "which", _cwd_first_which)
    monkeypatch.setattr(os, "name", "nt")
    assert refs.deps.check_ffmpeg()["detail"] == f"{real} (libx264 present)"
    # The ~/.local/bin fallback is asked by full path too, so the cwd stays out.
    monkeypatch.setenv("PATH", ".")
    local = _place(home / ".local" / "bin", "ffmpeg.EXE")
    assert refs.deps.which_any("ffmpeg", refs.deps._ffmpeg_dirs()) == str(local)
    local.unlink()
    assert refs.deps.which_any("ffmpeg", refs.deps._ffmpeg_dirs()) is None


def _which_without_pathext(cmd, mode=os.F_OK | os.X_OK, path=None):
    """Python <=3.11 on Windows: a name with a directory part gets no PATHEXT suffix."""
    return _REAL_WHICH(cmd, mode) if os.path.dirname(cmd) and os.path.isfile(cmd) else None


@pytest.mark.skipif(_WIN, reason="emulates the Windows lookup on POSIX")
def test_windows_lookup_applies_pathext_itself(refs, home, monkeypatch, tmp_path):
    on_path = tmp_path / "on-path"
    on_path.mkdir()
    real = _place(on_path, "ffmpeg.EXE")
    local = _place(home / ".local" / "bin", "ffprobe.EXE")
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.delenv("PATHEXT", raising=False)
    monkeypatch.setattr(shutil, "which", _which_without_pathext)
    monkeypatch.setattr(os, "name", "nt")
    assert refs.deps.which_any("ffmpeg", refs.deps._ffmpeg_dirs()) == str(real)
    assert refs.deps.which_any("ffprobe", refs.deps._ffmpeg_dirs()) == str(local)
    assert refs.deps.which_any("ffmpeg.EXE", []) == str(real)
    # A file with no PATHEXT suffix is not a Windows executable.
    _place(on_path, "ffprobe")
    local.unlink()
    assert refs.deps.which_any("ffprobe", refs.deps._ffmpeg_dirs()) is None
