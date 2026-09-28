"""Windows portability: the browsing allow-list holds no POSIX-only root.

``/home`` and ``/opt`` are POSIX filesystem conventions with no Windows
counterpart -- on Windows a bare ``Path("/home")`` is read relative to the
current drive and lands on ``C:\\home``, a directory nobody asked for.
``server._compute_allowed_roots`` appends the two conventions only under
``platform_compat.IS_POSIX``, so a Windows allow-list is the user profile
(``Path.home()``, which reads %USERPROFILE%) plus the system temp dir, and
nothing else.

These tests drive the platform switch through ``monkeypatch`` instead of the
host, so every shard asserts the same thing.
"""

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.file_explorer import server


@pytest.fixture
def home_and_tmp(tmp_path):
    """Two distinct existing directories standing in for the profile and temp dirs."""
    home = tmp_path / "profile"
    tmp = tmp_path / "temp"
    for d in (home, tmp):
        d.mkdir()
    return home, tmp


def test_windows_allow_list_is_exactly_home_and_tmp(monkeypatch, home_and_tmp):
    """With ``IS_POSIX`` false the function synthesises no third root."""
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)

    roots = server._compute_allowed_roots(home, tmp)

    assert roots == [home, tmp]


def test_windows_allow_list_names_no_drive_relative_convention_dir(monkeypatch, home_and_tmp):
    """No root is a drive-relative reading of a POSIX convention directory.

    The shape being guarded is a root sitting directly under a drive letter and
    named ``home`` or ``opt`` -- the form ``Path("/home")`` takes on Windows.
    """
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)

    roots = server._compute_allowed_roots(home, tmp)

    drive_relative = [p for p in roots if p.name in {"home", "opt"} and p.parent == Path(p.anchor)]
    assert not drive_relative, drive_relative


def test_posix_branch_appends_and_keeps_home_first(monkeypatch, home_and_tmp):
    """The POSIX branch appends, so the frontend's ``roots[0]`` stays the home dir."""
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", True)

    roots = server._compute_allowed_roots(home, tmp)

    assert roots[0] == home
    assert roots[1] == tmp
    # Whatever the POSIX branch contributes is absolute and present: the
    # exists() filter drops a convention dir the host does not have.
    assert all(p.is_absolute() and p.exists() for p in roots)


def test_no_configured_root_is_a_whole_volume():
    """The live allow-list never exposes a drive or filesystem root.

    ``ALLOWED_ROOTS`` is computed against the real host at import time. A root
    equal to its own anchor (``C:\\`` or ``/``) would make every file on the
    volume reachable through a browser scoped to the user's own directories.
    """
    assert server.ALLOWED_ROOTS
    volume_roots = [p for p in server.ALLOWED_ROOTS if p == Path(p.anchor)]
    assert not volume_roots, volume_roots
