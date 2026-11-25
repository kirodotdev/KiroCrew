"""The rootdir floor refuses a Windows stat or resolve of a UNC path (testing-conventions D10).

On Windows those calls open SMB to the host, so a test that reaches one answers with
the runner's network. ``_refuse_unc_network_paths`` makes them raise at once instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

floor = sys.modules["conftest_floor_configure"]

windows_only = pytest.mark.skipif(os.name != "nt", reason="the guard is installed on Windows only")


@pytest.mark.parametrize(
    ("path", "share"),
    [
        ("\\\\host\\share\\x", "\\\\host\\share"),
        ("//Host/Share/x", "\\\\host\\share"),
        ("\\\\host\\share", "\\\\host\\share"),
        ("\\\\host", "\\\\host\\"),
        ("\\\\?\\UNC\\host\\share\\x", "\\\\host\\share"),
        ("\\\\.\\UNC\\host\\share", "\\\\host\\share"),
        (b"\\\\host\\share\\x", "\\\\host\\share"),
        (Path("//host/share/x"), "\\\\host\\share"),
    ],
)
def test_a_unc_path_names_its_share(path, share) -> None:
    assert floor._unc_share(path) == share


@pytest.mark.parametrize(
    "path",
    [
        "C:\\Users\\x",
        "\\\\?\\C:\\very\\long\\path",
        "\\\\.\\pipe\\name",
        "\\\\.\\C:",
        "\\\\?\\GLOBALROOT\\Device\\x",
        "\\x",
        "relative\\x",
        "",
        "\\\\",
        "\\\\.\x00\x00.\\",
        3,
        None,
    ],
)
def test_a_local_path_names_no_share(path) -> None:
    assert floor._unc_share(path) is None


@windows_only
@pytest.mark.parametrize(
    "call",
    [
        os.path.realpath,
        os.stat,
        os.lstat,
        os.path.exists,
        os.path.isdir,
        os.path.isfile,
        os.path.islink,
        lambda p: Path(p).resolve(),
        lambda p: Path(p).exists(),
    ],
)
def test_a_unc_stat_or_resolve_is_refused_without_the_network(call) -> None:
    with pytest.raises(floor.UncNetworkPathRefused, match="D10"):
        call("\\\\host.invalid\\share\\x")


@windows_only
def test_local_paths_still_resolve(tmp_path) -> None:
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    long_form = "\\\\?\\" + str(target)
    assert os.path.exists(long_form)
    assert os.stat(long_form).st_size == 1
    assert Path(os.path.realpath(target)).name == "f"
    assert target.resolve().is_file()
    with open(target, "rb") as handle:
        assert os.stat(handle.fileno()).st_size == 1


@windows_only
def test_a_path_object_is_read_once_and_checked_as_opened(tmp_path) -> None:
    local = str(tmp_path / "missing")

    class Shifty:
        """Local on the first read, UNC on every later one."""

        reads = 0

        def __fspath__(self) -> str:
            Shifty.reads += 1
            return local if Shifty.reads == 1 else "\\\\host.invalid\\share\\x"

    with pytest.raises(FileNotFoundError):
        os.stat(Shifty())
    assert Shifty.reads == 1
    Shifty.reads = 0
    with pytest.raises(FileNotFoundError):
        os.stat(path=Shifty())
    assert Shifty.reads == 1


@windows_only
def test_the_wrapped_calls_keep_their_supports_membership_and_pickle() -> None:
    assert os.stat in os.supports_fd
    assert os.stat in os.supports_follow_symlinks
    # Pickle stores a function as module + qualname and finds it again by that name.
    for call in (os.stat, os.lstat, os.path.exists):
        assert getattr(sys.modules[call.__module__], call.__qualname__) is call


@windows_only
def test_a_stubbed_seam_is_not_refused(monkeypatch) -> None:
    monkeypatch.setattr(os.path, "realpath", os.path.normpath)
    assert os.path.realpath("\\\\host.invalid\\share\\..\\y") == "\\\\host.invalid\\share\\y"
