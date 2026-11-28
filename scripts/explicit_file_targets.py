"""Collect the files named on the command line without walking their siblings.

Loaded with ``-p scripts.explicit_file_targets`` by ``scripts/run_scoped_tests.py``,
whose related set is hundreds of explicit test files. pytest resolves a file
argument by walking down from the rootdir, and for each argument it collects the
file's directory AGAIN (``Session.collect`` passes ``handle_dupes=False`` when the
last part of an argument is a file): every sibling entry is offered to
``pytest_ignore_collect`` and then ``pytest_collect_file`` before the walk discards
the ones that are not the argument. ``test/`` holds about 8,900 entries, so a
492-file selection makes about 4.4 million such offers in every xdist worker, and
on Windows each path mismatch also falls back to ``samefile``. Measured on Linux:
60 explicit files collected in 143 s, 38 s with ``--ignore-glob=*``, 12.6 s with
this plugin (one file alone takes 2.5 s, so most of what remains is importing the
60 modules).

pytest never offers an initial path, or a directory above one, to
``pytest_ignore_collect`` (``Dir.collect`` and ``Package.collect`` consult
``Session.isinitpath`` first). So when every argument is a file, every path this
hook IS offered during collection is one no argument can match, and ignoring it
changes nothing the walk would have kept: the same node IDs in the same order, and
the same conftests loaded along each argument's own path. The hook is ``tryfirst``
and answers before pytest's own ``--ignore``/``--ignore-glob`` matching does its
per-entry path work, which is the measured gap to ``--ignore-glob=*``.

The gate does not reuse ``scripts/ci_file_shards.py --file-shard-targets`` because
that option turns explicit arguments into ``testpaths`` discovery, so ordinary
ignore rules apply to the targets.
``src/kiro_crew/apps/builtins/aws_control/crew/runtime/container_tests/conftest.py``
sets ``collect_ignore_glob = ["test_*.py"]`` when its environment declines: a
target there given as an explicit argument with this plugin runs as ``1 skipped``
(its ``pytest_pycollect_makemodule`` substitutes a declining module), while through
``--file-shard-targets`` it is ``collected 0 items``, silently, and a related set
of only such files exits 5. Only explicit arguments let the gate report what it
selected. A targets file is also state: ``scripts/local-gate.py`` keys its proof
cache on the argv, and ``--dry-run`` and ``argv_batches`` print and measure a pure
argv, so a file path there would need a deterministic location and an owner for
its lifetime across those callers. This plugin adds one ``-p`` option and no file.

The initial-path check compares paths as spelled, so an argument must be spelled
the way the walk will spell its directory entries. A path relative to the
invocation directory is, which is what the runner passes; an 8.3 short-name
component on Windows is not, because directory entries carry long names.

A directory argument would be emptied by this, so the plugin refuses to start
unless every argument names a file: a usage error, never a silent empty run.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

_collecting = False


def _names_a_file(base: Path, arg: str) -> bool:
    """Whether ``arg`` (a path, optionally ``::``-qualified) is an existing file."""
    try:
        return Path(os.path.abspath(base / arg.split("::", 1)[0])).is_file()
    except OSError:  # pragma: no cover - a name too long or odd to stat
        return False


def pytest_configure(config: pytest.Config) -> None:
    base = Path(config.invocation_params.dir)
    not_files = [arg for arg in config.args if not _names_a_file(base, arg)]
    if config.option.pyargs or not_files:
        raise pytest.UsageError(
            "scripts.explicit_file_targets collects only file arguments; "
            f"not files: {not_files or config.args}"
        )


@pytest.hookimpl(wrapper=True)
def pytest_collection(session: pytest.Session) -> Generator[None, object, object]:
    """Answer the ignore hook only while pytest walks for its arguments.

    A call after that is a test asking the live hook chain a question about a path
    of its own choosing (``test_platform_collect_ignore.py`` does), and the chain's
    ordinary answer is the one it wants.
    """
    global _collecting
    _collecting = True
    try:
        return (yield)
    finally:
        _collecting = False


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path: Path) -> bool | None:
    """Every path pytest asks about during its walk is a sibling no argument names."""
    return True if _collecting else None
