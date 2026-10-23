"""The per-platform test/ ignore list costs one set lookup per entry, not a path list.

``test/conftest.py`` keeps whole files out of collection on Windows (and macOS), named
in ``windows-collect-ignore.txt``. pytest's own ``pytest_ignore_collect`` turns a
conftest ``collect_ignore`` list into fresh absolute paths for EVERY directory entry it
is offered, before it reads ``--ignore-glob``. ``test/`` holds about 5,400 entries and
the Windows list about 90 names, and pytest walks ``test/`` once per file argument: as a
``collect_ignore`` list, the three-file nested ``--collect-only`` in
``test_ci_ipv6_routing.py`` builds about 1.47 million paths per child. On Windows each
build is a ``GetFullPathNameW`` call, and with the list those children take 44-64 s on
a Windows runner against about 4 s on Linux, whose list is empty; a Linux child given
the Windows list goes from about 4 s to about 17 s. So the conftest applies the list through its own hook, with a
set lookup.

These tests install the Windows list explicitly, so every platform guards the path only
Windows takes.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import _pytest.config
import pytest

TEST_DIR = Path(os.path.abspath(os.path.dirname(__file__)))
CONFTEST = TEST_DIR / "conftest.py"


def _listed(listname: str) -> set[str]:
    lines = (TEST_DIR / listname).read_text(encoding="utf-8").splitlines()
    return {name for name in (ln.split("#", 1)[0].strip() for ln in lines) if name}


@pytest.fixture
def windows_list(request, monkeypatch):
    """test/conftest.py with its Windows ignore list in force, whatever the host.

    Through the conftest's own hook when it re-exports the helper's; a conftest that
    declares the list as ``collect_ignore`` instead gets the list its win32 branch
    assigns, so the count below measures that form too rather than erroring.
    """
    plugin = request.config.pluginmanager.get_plugin(str(CONFTEST))
    assert plugin is not None, f"{CONFTEST} is not registered as a conftest plugin"
    names = frozenset(_listed("windows-collect-ignore.txt"))
    assert names, "the Windows ignore list parsed to nothing"
    helpers = sys.modules.get("platform_collect_ignore_helpers")
    if helpers is not None and getattr(plugin, "pytest_ignore_collect", None) is getattr(
        helpers, "pytest_ignore_collect", object()
    ):
        assert helpers.platform_ignore_names("win32") == names
        monkeypatch.setattr(helpers, "PLATFORM_IGNORED", names)
    else:
        monkeypatch.setattr(plugin, "collect_ignore", sorted(names), raising=False)
    return names


def test_conftest_declares_no_collect_ignore_list():
    """A ``collect_ignore`` here is rebuilt for every entry pytest scans in ``test/``.

    Checked in the source rather than on the loaded module: the list applies only under
    a platform branch, so on Linux a runtime ``hasattr`` passes whatever the source says.
    """
    # test/conftest.py binds most of its definitions from plain modules beside it.
    floor = set(TEST_DIR.glob("conftest_floor_*.py"))
    parts = sorted(set(TEST_DIR.glob("conftest_*.py")) - floor)
    source = "\n".join(p.read_text(encoding="utf-8") for p in (CONFTEST, *parts))
    tree = ast.parse(source, filename=str(CONFTEST))
    assigned = sorted(
        {
            (target.id, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
            and target.id in ("collect_ignore", "collect_ignore_glob")
        }
    )
    assert not assigned, (
        f"test/conftest.py assigns {assigned}: pytest re-materialises a conftest ignore "
        "list into absolute paths on every directory entry; list the names in "
        "windows-collect-ignore.txt, which platform_collect_ignore_helpers applies"
    )


def test_the_windows_list_ignores_exactly_the_listed_test_files(request, windows_list):
    """Reach is ``test/<name>`` only, asked through the real hook chain."""
    names = windows_list
    ihook = request.session.gethookproxy(TEST_DIR)
    config = request.config
    listed = sorted(names)[0]
    assert ihook.pytest_ignore_collect(collection_path=TEST_DIR / listed, config=config)
    # A same-named file below test/ is outside the list's reach, and an unlisted test/
    # file is not ignored.
    nested = TEST_DIR / "fixtures" / listed
    assert not request.session.gethookproxy(nested.parent).pytest_ignore_collect(
        collection_path=nested, config=config
    )
    assert not ihook.pytest_ignore_collect(collection_path=Path(__file__), config=config)


def test_offering_every_test_entry_rebuilds_no_conftest_path_list(
    request, windows_list, monkeypatch
):
    """The walk pytest makes over ``test/``, once per file argument, stays O(entries).

    ``Config._getconftest_pathlist`` is where a conftest ignore list is turned into
    absolute paths -- one ``absolutepath`` per listed name, per entry offered. With the
    Windows list in force, offering every entry of ``test/`` to the hook chain must build
    none of them; a ``collect_ignore`` list builds ``entries x names``.
    """
    names = windows_list
    built = []
    real = _pytest.config.absolutepath

    def counting(path):
        built.append(path)
        return real(path)

    monkeypatch.setattr(_pytest.config, "absolutepath", counting)
    ihook = request.session.gethookproxy(TEST_DIR)
    ignored = set()
    with os.scandir(TEST_DIR) as entries:
        offered = [Path(entry.path) for entry in entries]
    for path in offered:
        if ihook.pytest_ignore_collect(collection_path=path, config=request.config):
            ignored.add(path.name)
    assert len(offered) > 1000, f"test/ is unexpectedly small: {len(offered)} entries"
    assert names <= ignored, f"listed files not ignored: {sorted(names - ignored)}"
    assert not built, (
        f"offering {len(offered)} test/ entries built {len(built)} conftest ignore paths "
        f"(e.g. {built[:3]}); a nested three-file collect would build three times that"
    )
