r"""Windows spellings of an approved root in the outbound-file containment check.

``containing_root`` decides whether an outbound image lies inside an approved
root. On Windows one file has many spellings: any letter case, ``/`` or ``\``,
and the drive-local extended-length prefix ``\\?\C:\``. Each must be admitted
when it names a file inside the root, and the fold must admit nothing more: a
sibling, a parent, a ``..`` climb, another drive, a share or another namespace
stays outside. ``ntpath`` is importable on every OS, so these run on any CI
host. Off Windows its ``abspath`` is the pure ``normpath`` fallback, so the
plain-path cases here pin the fold and the compare, not ``GetFullPathNameW``.

Two folds are deliberately narrower than Windows itself. A ``\\?\`` path is
opened literally, so it is never normalised here, and a segment whose meaning
differs between the literal and the normalised spelling (``.``, ``..``, empty,
a trailing dot or space, a ``/``) refuses it. Case is folded for ASCII only,
because NTFS does not fold every pair Unicode lowercasing does.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import types
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

from kiro_crew.messaging import outbound_files as module
from kiro_crew.messaging.outbound_files import ApprovedRoot, containing_root, lexically_within

_ROOT = r"C:\work\proj"

_INSIDE = [
    pytest.param(r"C:\work\proj\shot.png", id="same-spelling"),
    pytest.param(r"c:\work\proj\shot.png", id="lowercase-drive"),
    pytest.param(r"C:\WORK\PROJ\SHOT.PNG", id="upper-cased"),
    pytest.param(r"C:/work/proj/shot.png", id="forward-slashes"),
    pytest.param(r"C:\work\proj\.\sub\..\shot.png", id="collapsed-dots"),
    pytest.param(r"\\?\C:\work\proj\shot.png", id="extended-prefix"),
    pytest.param(r"\\?\c:\Work\proj\shot.png", id="extended-prefix-lowercase"),
    pytest.param(r"c:\work\proj", id="the-root-itself"),
]

_OUTSIDE = [
    pytest.param(r"C:\work\proj2\shot.png", id="sibling-sharing-a-prefix"),
    pytest.param(r"C:\work\shot.png", id="parent"),
    pytest.param(r"C:\work\proj\..\secret.png", id="dot-dot-climb"),
    pytest.param(r"\\?\C:\work\proj\..\secret.png", id="extended-prefix-dot-dot-climb"),
    pytest.param(r"D:\work\proj\shot.png", id="other-drive"),
    pytest.param(r"\\srv\share\work\proj\shot.png", id="unc-share"),
    pytest.param(r"\\?\UNC\srv\share\work\proj\shot.png", id="extended-unc"),
    pytest.param(r"\\.\C:\work\proj\shot.png", id="device-namespace"),
    pytest.param(r"\\?\Volume{0}\work\proj\shot.png", id="volume-guid"),
    pytest.param(r"C:\other\\?\C:\work\proj\shot.png", id="prefix-not-at-start"),
    pytest.param(r"\\?\C:\work\proj.\shot.png", id="extended-trailing-dot-sibling"),
    pytest.param(r"\\?\C:\work.\proj\shot.png", id="extended-trailing-dot-ancestor"),
    pytest.param(r"\\?\C:\work\proj \shot.png", id="extended-trailing-space-sibling"),
    pytest.param(r"\\?\C:\work\other\..\proj\shot.png", id="extended-dot-dot-back-in"),
    pytest.param(r"\\?\C:\work\proj\.\shot.png", id="extended-single-dot"),
    pytest.param(r"\\?\C:\work\\proj\shot.png", id="extended-empty-segment"),
    pytest.param(r"\\?\C:\work\proj/shot.png", id="extended-forward-slash"),
    pytest.param(r"\\?\C:", id="extended-volume-without-separator"),
    pytest.param(r"//?/C:/work/proj/shot.png", id="extended-prefix-forward-slashes"),
    pytest.param(r"\??\C:\work\proj\shot.png", id="nt-object-namespace"),
    pytest.param("\\\\?\\C:\n", id="extended-drive-then-newline"),
    pytest.param("C:\\wor\u212a\\proj\\shot.png", id="kelvin-sign-sibling"),
]


@pytest.mark.parametrize("target", _INSIDE)
def test_a_windows_spelling_inside_the_root_is_within(target: str) -> None:
    assert lexically_within(target, _ROOT, ntpath) is True


@pytest.mark.parametrize("target", _OUTSIDE)
def test_a_windows_spelling_outside_the_root_is_not_within(target: str) -> None:
    assert lexically_within(target, _ROOT, ntpath) is False


def test_non_ascii_case_is_not_folded() -> None:
    """Fails closed: a non-ASCII case variant of an inside path is refused."""
    assert lexically_within("C:\\caf\u00e9\\shot.png", "C:\\Caf\u00c9", ntpath) is False
    assert lexically_within("C:\\CAF\u00c9\\shot.png", "C:\\caf\u00c9", ntpath) is True


def test_a_namespace_spelled_root_admits_nothing() -> None:
    """``normpath`` would collapse the ``..`` Windows opens literally here."""
    root = r"\\?\UNC\srv\share\ws"
    assert lexically_within(r"\\?\UNC\srv\share\other\..\ws\x.png", root, ntpath) is False
    assert lexically_within(r"\\?\UNC\srv\share\ws\x.png", root, ntpath) is False


def test_an_extended_prefix_on_the_root_is_folded_too() -> None:
    assert lexically_within(r"c:\work\proj\shot.png", r"\\?\C:\work\proj", ntpath) is True
    assert lexically_within(r"C:\work\proj2\shot.png", r"\\?\C:\work\proj", ntpath) is False


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/ws/shot.png", True),
        ("/ws", True),
        ("/ws2/shot.png", False),
        ("/ws/../secret.png", False),
        ("/WS/shot.png", False),
        ("//?/ws/shot.png", False),
    ],
)
def test_posix_rules_stay_exact_and_case_sensitive(target: str, expected: bool) -> None:
    assert lexically_within(target, "/ws", posixpath) is expected


def test_containing_root_applies_the_windows_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The call site reaches the compare with the module's own ``os.path``."""
    root = ApprovedRoot(lexical=_ROOT, read_root=_ROOT)
    monkeypatch.setattr(module, "approved_roots", lambda within, scratch="": (root,))
    monkeypatch.setattr(module, "os", types.SimpleNamespace(name="nt", path=ntpath))
    inside = cast(Path, PureWindowsPath(r"\\?\c:\work\proj\shot.png"))
    outside = cast(Path, PureWindowsPath(r"c:\work\proj2\shot.png"))
    assert containing_root(inside, _ROOT) is root
    assert containing_root(outside, _ROOT) is None


def test_containing_root_on_this_host_still_admits_a_real_cwd_file(tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    admitted = containing_root(shot, os.fspath(tmp_path))
    assert admitted is not None and admitted.lexical == os.path.abspath(tmp_path)
    assert containing_root(tmp_path.parent / "elsewhere.png", os.fspath(tmp_path)) is None
