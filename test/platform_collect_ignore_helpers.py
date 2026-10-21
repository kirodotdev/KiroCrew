"""Whole ``test/`` files a platform does not collect, skipped from a hook.

``test/conftest.py`` re-exports :func:`pytest_ignore_collect`, which is how pytest finds
it. The names come from ``windows-collect-ignore.txt`` (or ``macos-collect-ignore.txt``)
rather than an inline list: the CI reduced-scope selector (scripts/ci-surface-tests.py)
has to apply the same exclusion, because naming a file explicitly on the pytest command
line bypasses this hook. One file, two readers, no drift.

A hook, not a conftest ``collect_ignore`` list. pytest's own ``pytest_ignore_collect``
turns a conftest's ``collect_ignore`` into a fresh list of absolute paths on EVERY
directory entry it is offered (``Config._getconftest_pathlist`` caches nothing), before
it even reads ``--ignore-glob``. ``test/`` holds ~5,400 entries and the Windows list ~90
names, so as a list one walk of ``test/`` builds ~490,000 paths -- and pytest walks
``test/`` once per FILE argument, so a nested ``--collect-only`` of three files builds
~1.47 million. On Windows each build is a ``GetFullPathNameW`` call: with the list, the
nested collections in ``test_ci_ipv6_routing.py`` take 44-64 s on a Windows runner
against about 4 s on Linux, where it is empty (a Linux child given the Windows list
takes about 17 s). Here a skip costs one set lookup per entry.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from kiro_crew import platform_compat

TEST_DIR = pathlib.Path(os.path.abspath(os.path.dirname(__file__)))

#: The ignore-list file each platform reads; a platform not named here ignores nothing.
_PLATFORM_IGNORE_LISTS = {
    "win32": "windows-collect-ignore.txt",
    "darwin": "macos-collect-ignore.txt",
}


def collect_ignore_from(listname: str) -> list[str]:
    """Bare test filenames listed in ``test/<listname>``, comments stripped."""
    try:
        with open(TEST_DIR / listname, encoding="utf-8") as fh:
            return [name for name in (ln.split("#", 1)[0].strip() for ln in fh) if name]
    except OSError:  # pragma: no cover - list file absent in a partial checkout
        return []


def platform_ignore_names(platform: str) -> frozenset[str]:
    """The bare ``test/`` filenames *platform* does not collect."""
    listname = _PLATFORM_IGNORE_LISTS.get(platform)
    return frozenset(collect_ignore_from(listname)) if listname else frozenset()


PLATFORM_IGNORED = platform_ignore_names(
    "win32" if platform_compat.IS_WINDOWS else "darwin" if platform_compat.IS_MACOS else ""
)


def pytest_ignore_collect(collection_path: pathlib.Path, config: pytest.Config) -> bool | None:
    """Skip the platform's listed files, with the reach of a ``test/conftest.py`` list.

    Only ``test/<name>`` itself: never a same-named file in a subdirectory, and never a
    path named on the command line (pytest does not offer an initial path to this hook).
    """
    if not PLATFORM_IGNORED or collection_path.name not in PLATFORM_IGNORED:
        return None
    return True if collection_path.parent == TEST_DIR else None
