"""The floor keeps the operator's own kiro-cli out of every test.

``known_kiro_cli_dirs`` lists install directories under the home, and the pinned
resolvers search them without ``PATH``, so a test that never chose a kiro-cli ran the
developer's real one. The rootdir floor (``_hide_the_operators_kiro_cli``) filters the
entries under the REAL account home out of a call that passes that home, at every
module-level binding of the function. These tests pin both halves: the filter, and the
binding table it patches.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tempfile

import conftest_floor_home
import pytest
from conftest_floor_home import (
    _KIRO_CLI_DIR_BINDINGS,
    _operators_dirs_dropped,
)

from kiro_crew import kiro_cli
from kiro_crew.kiro_cli import known_kiro_cli_dirs as _UNWRAPPED  # bound before the floor wraps it
from kiro_crew.testing.links import make_dir_link

pytestmark = pytest.mark.xdist_group(name="tree_scan_floor_operators_kiro_cli")

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _platform() -> str:
    return "win32" if sys.platform == "win32" else "linux"


def _inside(directory: str, home: pathlib.Path) -> bool:
    return pathlib.Path(directory).resolve().is_relative_to(home.resolve())


@pytest.fixture
def operator_home(tmp_path, monkeypatch):
    """A stand-in for the operator's home, with temp roots that do not contain it.

    The filter spares directories under a temp root, so a real home that happens to
    sit under one (a harness that fakes ``HOME`` in its scratch) would keep its
    entries; the test pins both instead of reading the host's layout.
    """
    home = tmp_path / "operator-home"
    home.mkdir()
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    monkeypatch.setattr(conftest_floor_home, "_REAL_USER_HOME", home)
    monkeypatch.setattr(conftest_floor_home, "_TEMP_ROOT_AT_START", temp_root.resolve())
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return home


def test_the_operators_home_contributes_no_fixed_directory(operator_home):
    dirs = kiro_cli.known_kiro_cli_dirs(
        _platform(), operator_home, {}, include_inherited_path=False
    )

    unfiltered = _UNWRAPPED(_platform(), operator_home, {}, include_inherited_path=False)
    assert [d for d in unfiltered if _inside(d, operator_home)], "the premise: some live there"
    assert not [d for d in dirs if _inside(d, operator_home)], dirs


def test_a_test_owned_home_keeps_its_directories(tmp_path):
    dirs = kiro_cli.known_kiro_cli_dirs(_platform(), tmp_path, {})

    assert [d for d in dirs if _inside(d, tmp_path)], dirs


def test_a_link_out_of_the_home_still_counts_as_the_operators(tmp_path, monkeypatch):
    """``~/.toolbox/bin`` can resolve outside the home and is still an install there,
    while a directory under a temp root is a test's own even inside the home."""
    home = tmp_path / "home"
    shared = tmp_path / "shared" / "bin"
    shared.mkdir(parents=True)
    (home / ".toolbox").mkdir(parents=True)
    make_dir_link(home / ".toolbox" / "bin", shared)
    scratch = home / "scratch"
    scratch.mkdir()
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(conftest_floor_home, "_TEMP_ROOT_AT_START", scratch.resolve())
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    dirs = [
        str(home / ".local" / "bin"),
        str(home / ".toolbox" / "bin"),
        str(scratch),
        str(elsewhere),
    ]

    assert _operators_dirs_dropped(home, dirs) == [str(scratch), str(elsewhere)]


def test_every_module_binding_of_the_function_is_patched():
    for name in _KIRO_CLI_DIR_BINDINGS:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "known_kiro_cli_dirs"):
            assert getattr(module.known_kiro_cli_dirs, "_floor_hides_the_operators", False), name


def _module_name(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(_SRC).with_suffix("").parts)


def test_the_binding_table_names_every_module_that_imports_the_function():
    """A new ``from kiro_crew.kiro_cli import known_kiro_cli_dirs`` holds its own name,
    which the floor cannot reach unless the table lists the module."""
    found = {"kiro_crew.kiro_cli"}
    for path in (_SRC / "kiro_crew").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "known_kiro_cli_dirs" not in text:
            continue
        for node in ast.parse(text).body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "kiro_crew.kiro_cli"
                and any(alias.name == "known_kiro_cli_dirs" for alias in node.names)
            ):
                found.add(_module_name(path))
    assert found == set(_KIRO_CLI_DIR_BINDINGS)
