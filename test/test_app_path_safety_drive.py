"""``_check_path_safety`` refuses a drive-qualified name on Windows only.

On Windows a name such as ``D:x`` has no separator, yet joining it onto a
data-home path changes the drive. Elsewhere it is one plain path component,
so the gate keeps accepting it. Every other row pins what the gate already
decided on every host.
"""

from __future__ import annotations

import os

import pytest

from kiro_crew.apps import manager_tree
from kiro_crew.apps.manager_tree import _check_path_safety

_DRIVE_NAMES = ["D:x", "C:probe", "Q:"]


@pytest.mark.parametrize("name", _DRIVE_NAMES)
def test_drive_qualified_name_follows_host(name: str) -> None:
    assert _check_path_safety(name) is (os.name != "nt")


@pytest.mark.parametrize("name", _DRIVE_NAMES)
def test_drive_qualified_name_refused_on_windows(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_tree.os, "name", "nt")
    assert _check_path_safety(name) is False


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        # Plain single components: allowed.
        ("probe", True),
        ("my-app", True),
        ("ab:c", True),
        ("app.v2", True),
        # Separators, traversal and absolute paths: refused on every host.
        ("d:foo\\bar", False),
        ("C:..\\x", False),
        ("./a:b", False),
        ("..", False),
        ("a/b", False),
        ("a\\b", False),
        ("/etc/passwd", False),
        ("C:\\x", False),
        ("\\\\server\\share", False),
    ],
)
def test_check_path_safety_rows(name: str, safe: bool) -> None:
    assert _check_path_safety(name) is safe
