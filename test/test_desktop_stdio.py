"""``desktop_stdio.redirect_desktop_stdio``: the Windows desktop gateway's log hand-off.

The shell spawns the gateway with null stdio (so Node adds ``CREATE_NO_WINDOW``) and
names its launch log in an environment variable instead. Spare descriptors stand in
for 1 and 2 here, so the test never re-points the runner's own streams.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import desktop_stdio
from kiro_crew.desktop_stdio import DESKTOP_STDIO_LOG_ENV, redirect_desktop_stdio


@pytest.fixture
def spare_fds():
    fds = (os.open(os.devnull, os.O_WRONLY), os.open(os.devnull, os.O_WRONLY))
    yield fds
    for fd in fds:
        os.close(fd)


def test_the_named_log_receives_both_streams_and_the_variable_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spare_fds: tuple[int, int]
) -> None:
    log = tmp_path / "gateway-launch.log"
    log.write_bytes(b"shell line\n")
    monkeypatch.setenv(DESKTOP_STDIO_LOG_ENV, str(log))

    redirect_desktop_stdio(spare_fds)
    os.write(spare_fds[0], b"out\n")
    os.write(spare_fds[1], b"err\n")

    assert log.read_bytes() == b"shell line\nout\nerr\n"
    assert DESKTOP_STDIO_LOG_ENV not in os.environ


def test_without_the_variable_nothing_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spare_fds: tuple[int, int]
) -> None:
    monkeypatch.delenv(DESKTOP_STDIO_LOG_ENV, raising=False)
    before = [os.fstat(fd).st_ino for fd in spare_fds]

    redirect_desktop_stdio(spare_fds)

    assert [os.fstat(fd).st_ino for fd in spare_fds] == before


def test_an_unopenable_log_leaves_the_descriptors_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spare_fds: tuple[int, int]
) -> None:
    monkeypatch.setenv(DESKTOP_STDIO_LOG_ENV, str(tmp_path / "missing-dir" / "log"))
    before = [os.fstat(fd).st_ino for fd in spare_fds]

    redirect_desktop_stdio(spare_fds)

    assert [os.fstat(fd).st_ino for fd in spare_fds] == before


def test_the_module_imports_the_standard_library_only() -> None:
    """``_bootstrap`` imports it before the package's dependencies are known to exist."""
    tree = ast.parse(Path(desktop_stdio.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}
