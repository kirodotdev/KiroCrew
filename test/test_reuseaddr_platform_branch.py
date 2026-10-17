"""Every production ``setsockopt(..., SO_REUSEADDR, ...)`` sits under a platform branch.

On POSIX the option only waives TIME_WAIT. On Windows two listeners that both set it
share one address, so a "first free port" loop or a "did the bind fail" check does not
see a port that another SO_REUSEADDR listener holds as busy. Each listener therefore
picks its option per platform (``SO_REUSEADDR`` on POSIX, ``SO_EXCLUSIVEADDRUSE`` on
Windows), and this scan fails a new listener that sets the shared option
unconditionally.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew

SRC_ROOT = Path(kiro_crew.__file__).resolve().parent
_PLATFORM_MARKERS = ("IS_POSIX", "IS_WINDOWS", "os.name", "sys.platform")


def _unguarded_reuseaddr_lines(source: str) -> list[int]:
    """Lines of ``setsockopt`` calls naming ``SO_REUSEADDR`` outside any platform ``if``."""
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    lines = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setsockopt"
            and any(
                isinstance(arg, ast.Attribute) and arg.attr == "SO_REUSEADDR" for arg in node.args
            )
        ):
            continue
        ancestor = parent.get(node)
        while ancestor is not None:
            if isinstance(ancestor, (ast.If, ast.IfExp)) and any(
                marker in ast.unparse(ancestor.test) for marker in _PLATFORM_MARKERS
            ):
                break
            ancestor = parent.get(ancestor)
        else:
            lines.append(node.lineno)
    return lines


def _production_sources_naming_the_option() -> list[Path]:
    paths = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if (
            path.name.startswith("test_")
            or "/tests/" in relative
            or "/container_tests/" in relative
        ):
            continue
        if "SO_REUSEADDR" in path.read_text(encoding="utf-8"):
            paths.append(path)
    return paths


def test_no_listener_sets_so_reuseaddr_outside_a_platform_branch() -> None:
    offenders = [
        f"{path.relative_to(SRC_ROOT).as_posix()}:{line}"
        for path in _production_sources_naming_the_option()
        for line in _unguarded_reuseaddr_lines(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_scan_can_actually_fail() -> None:
    """A scan that finds nothing would pass the test above vacuously."""
    bare = "def bind(s):\n    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    posix_only = (
        "def bind(s):\n"
        "    if platform_compat.IS_POSIX:\n"
        "        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    )
    other_branch = (
        "def bind(s):\n"
        "    if runtime.IS_WINDOWS:\n"
        "        pass\n"
        "    else:\n"
        "        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    )
    unrelated_branch = (
        "def bind(s, flag):\n"
        "    if flag:\n"
        "        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    )
    assert _unguarded_reuseaddr_lines(bare) == [2]
    assert _unguarded_reuseaddr_lines(unrelated_branch) == [3]
    assert _unguarded_reuseaddr_lines(posix_only) == []
    assert _unguarded_reuseaddr_lines(other_branch) == []


def test_the_scan_reads_the_sites_that_exist() -> None:
    """The listeners this rule was written for are in the scanned set."""
    names = {
        path.relative_to(SRC_ROOT).as_posix() for path in _production_sources_naming_the_option()
    }
    assert "auth/login/portal.py" in names
    assert "dashboard/server_runtime/listener.py" in names
