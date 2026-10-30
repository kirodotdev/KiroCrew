"""The git floor: auto maintenance off, and git's location variables removed, for
every test on every testpath.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and ``test/git_maintenance_helpers.py`` (loaded by path), and nothing
here imports ``conftest``: the name is ambiguous, and an in-package app suite runs with
``test/`` off ``sys.path``.
"""

from __future__ import annotations

import os
import pathlib
import sys
from importlib.util import module_from_spec, spec_from_file_location

import pytest


def _load_git_maintenance_helpers():
    """``test/git_maintenance_helpers.py``, under the name ``test/`` imports it by.

    Loaded by path because ``test/`` is not on ``sys.path`` for the other
    testpaths, and registered in ``sys.modules`` so ``from git_maintenance_helpers
    import ...`` in a ``test/`` module binds this same module.
    """
    name = "git_maintenance_helpers"
    if name in sys.modules:
        return sys.modules[name]
    path = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_GIT_MAINTENANCE = _load_git_maintenance_helpers()
_git_maintenance_off_entries = _GIT_MAINTENANCE.git_maintenance_off_entries


@pytest.fixture(autouse=True, scope="session")
def _git_global_config(_xdg_config_root, tmp_path_factory):
    """Turn git's auto maintenance off before any session, module or class fixture runs.

    Several higher-scoped fixtures commit a template repository that every test
    then copies (``test_project_tree._repo_template``), and the function-scoped
    floor below is not live yet when pytest builds them. Within a scope pytest
    runs autouse fixtures first, and the rootdir conftest's before ``test/``'s.

    ``GIT_CONFIG_GLOBAL`` points at a file that includes the global config git
    would read anyway, then turns auto maintenance off. A global file reaches
    git that ``GIT_CONFIG_COUNT`` does not: the ``receive-pack`` a local push
    starts, and git that code under test runs with an env of its own built over
    ``os.environ``. The includes keep what tests read otherwise unchanged:
    ``~`` follows ``$HOME`` when git reads the file, and the XDG file is the one
    under the redirect :func:`_isolate_xdg_config_home` sets for every test.

    Yields the HERMETIC file (only the maintenance keys), which ``test/``'s
    ``_git_identity`` uses in place of ``/dev/null``. It holds its own
    MonkeyPatch, undone at session end.
    """
    directory = tmp_path_factory.mktemp("git-global")
    inherited = os.environ.get("GIT_CONFIG_GLOBAL")
    if inherited is None:
        includes = [(_xdg_config_root / "git" / "config").as_posix(), "~/.gitconfig"]
    elif inherited and inherited != os.devnull:
        includes = [pathlib.Path(inherited).absolute().as_posix()]
    else:
        includes = []
    inherit = directory / "inherit.gitconfig"
    inherit.write_text(_GIT_MAINTENANCE.git_global_config_text(includes), encoding="utf-8")
    hermetic = directory / "hermetic.gitconfig"
    hermetic.write_text(_GIT_MAINTENANCE.git_global_config_text(), encoding="utf-8")
    patch = pytest.MonkeyPatch()
    try:
        patch.setenv("GIT_CONFIG_GLOBAL", str(inherit))
        for name, value in _git_maintenance_off_entries(os.environ).items():
            patch.setenv(name, value)
        for name in _git_vars_to_strip():
            patch.delenv(name, raising=False)
        yield hermetic
    finally:
        patch.undo()


def _git_vars_to_strip() -> frozenset[str]:
    """The git variables the floor removes from every test's environment.

    The location list ``update_governance`` strips before it runs git against a
    discovered checkout, less ``GIT_CEILING_DIRECTORIES``, which the floor sets
    itself to bound git's upward walk (``_stop_git_discovery_above_the_temp_roots``).
    A git hook exports ``GIT_INDEX_FILE`` (and ``GIT_DIR`` or ``GIT_WORK_TREE`` when
    the caller set them), so a suite run from a hook would otherwise point every
    fixture repo's ``git add`` and ``git commit`` at the operator's real
    repository.

    Also ``GIT_CONFIG_PARAMETERS``: a hook started by ``git -c key=value ...``
    exports the caller's ``-c`` settings there, and git reads them after
    ``GIT_CONFIG_COUNT``, so an operator's ``-c maintenance.auto=true`` would
    outrank the maintenance entries above. Imported at call time, since this
    module's module-level imports stay stdlib-only.
    """
    from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

    return (_GIT_LOCATION_VARS - {"GIT_CEILING_DIRECTORIES"}) | {"GIT_CONFIG_PARAMETERS"}


@pytest.fixture(autouse=True)
def _git_location_unset(_floor_monkeypatch, _git_global_config) -> None:
    """Remove the stripped git variables for a test whose environment regained them.

    The session fixture has normally removed them already; this covers a module
    or class fixture that set one again.
    """
    for name in _git_vars_to_strip():
        _floor_monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _git_maintenance_off(_floor_monkeypatch, _git_global_config) -> None:
    """Re-add the maintenance entries for a test whose environment lost them.

    The session fixture has normally set them already, so this only appends
    when a module or class fixture replaced ``GIT_CONFIG_COUNT``.
    """
    for name, value in _git_maintenance_off_entries(os.environ).items():
        _floor_monkeypatch.setenv(name, value)
