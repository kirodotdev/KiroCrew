"""What the bytecode-mirror prune is allowed to delete.

The prune runs from ``pytest_sessionfinish`` and deletes ``sys.pycache_prefix`` mirror
trees. Its whole safety argument is the SCOPE: the roots this run created, and nothing
else. The mirror of the PLATFORM temp root is shared with every concurrent run, so
deleting that tree throws away bytecode belonging to sibling runs that have not finished.

The hazard is invisible when the function is called mid-session: ``tempfile.gettempdir()``
still resolves to the run's own base while the redirect is installed, and only resolves to
the platform root once the session fixture's finalizer has undone it -- which is exactly
when this hook runs. So the scope must come from a value captured at CREATION, never from
reading the ambient temp directory at teardown.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

_ROOT_CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "conftest.py"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    A plain ``import conftest`` from here resolves to ``test/conftest.py``; loading by
    path is what names the rootdir one unambiguously. The fixtures it defines are inert
    in this namespace -- a ``@pytest.fixture`` decorator only marks a function.
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_prune_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()


def _mirror_of(prefix: pathlib.Path, source_root: pathlib.Path) -> pathlib.Path:
    """The mirror path CPython would use for sources under *source_root*."""
    drive, tail = os.path.splitdrive(str(source_root.absolute()))
    return prefix / (drive.replace(":", "") + tail.lstrip(os.sep))


@pytest.fixture
def mirror_world(tmp_path, monkeypatch):
    """A fake pycache mirror holding one run-owned tree and one shared platform tree."""
    prefix = tmp_path / "pycache"
    platform_temp = tmp_path / "platform-tmp"
    run_root = platform_temp / "kc-pytest-someone-123"
    for directory in (prefix, platform_temp, run_root):
        directory.mkdir(parents=True, exist_ok=True)

    run_mirror = _mirror_of(prefix, run_root)
    shared_mirror = _mirror_of(prefix, platform_temp)
    for mirror in (run_mirror, shared_mirror):
        mirror.mkdir(parents=True, exist_ok=True)
        (mirror / "module.cpython-312.pyc").write_bytes(b"\x00")
    # A sibling run's tree lives INSIDE the shared mirror, which is what makes deleting
    # that mirror a cross-run action rather than a self-cleanup.
    sibling = shared_mirror / "kc-pytest-someone-999"
    sibling.mkdir()
    (sibling / "other.cpython-312.pyc").write_bytes(b"\x00")

    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    # The state this hook actually runs in: the redirect is already undone, so the
    # ambient temp dir is the platform root shared with every other run.
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(platform_temp))
    assert tempfile.gettempdir() == str(platform_temp)
    monkeypatch.delenv(_root._SHORT_TMP_ROOT_ENV, raising=False)
    monkeypatch.setattr(_root, "_RUN_TEMP_ROOTS", [str(run_root)])
    # A stub factory: resolving the live basetemp is not what this scope rule is about.
    session = SimpleNamespace(
        config=SimpleNamespace(
            _tmp_path_factory=SimpleNamespace(getbasetemp=lambda: tmp_path / "no-basetemp")
        )
    )
    return SimpleNamespace(
        session=session,
        prefix=prefix,
        run_mirror=run_mirror,
        shared_mirror=shared_mirror,
        sibling=sibling,
        platform_temp=platform_temp,
    )


def test_the_mirror_of_a_root_this_run_created_is_pruned(mirror_world):
    _root._prune_bytecode_mirror_of_this_runs_temp_roots(mirror_world.session)
    assert not mirror_world.run_mirror.exists()


def test_the_shared_platform_mirror_survives(mirror_world):
    """The regression this locks: reading the ambient temp dir at teardown.

    A prune keyed on ``tempfile.gettempdir()`` deletes the mirror every concurrent run
    writes into, including the sibling tree below.
    """
    _root._prune_bytecode_mirror_of_this_runs_temp_roots(mirror_world.session)
    assert mirror_world.shared_mirror.is_dir()
    assert (mirror_world.sibling / "other.cpython-312.pyc").is_file()


def test_the_run_owned_short_root_is_pruned_too(mirror_world, monkeypatch):
    """The short root is run-owned as well, and it is named by an env var, not a global."""
    short_root = mirror_world.platform_temp / "kc-pytest-someone-123-short-abc"
    short_root.mkdir()
    short_mirror = _mirror_of(mirror_world.prefix, short_root)
    short_mirror.mkdir(parents=True)
    (short_mirror / "sock.cpython-312.pyc").write_bytes(b"\x00")
    monkeypatch.setenv(_root._SHORT_TMP_ROOT_ENV, str(short_root))
    _root._prune_bytecode_mirror_of_this_runs_temp_roots(mirror_world.session)
    assert not short_mirror.exists()
    assert mirror_world.shared_mirror.is_dir()
