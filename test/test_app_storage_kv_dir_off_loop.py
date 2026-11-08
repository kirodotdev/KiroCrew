"""AppStorage creates its ``kv`` dir on the write path, not the event loop.

``build_app_context`` is a synchronous factory the hook-context builders call
on the gateway event loop (``LifecycleDispatcher._build_context`` and
``hooks_integration._build_app_context_from_info``). For a storage-enabled app
it constructs an :class:`~kiro_crew.apps.app_storage.AppStorage`. That
constructor touches no filesystem: a ``mkdir`` in it would be a blocking disk
call on the loop, run on every lifecycle event (enable, boot, disable,
shutdown).

The directory is created on the first write (``set``), the way the sibling
``JobStore`` does, and with the mode a plain ``Path.mkdir`` produces. These
tests pin both halves:

* building the context (and constructing the store) creates no ``kv`` dir --
  a create in the constructor runs it on the loop thread and fails this test;
* the first ``set`` creates the dir, with the same permissions a plain
  ``Path.mkdir`` gives.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.app_storage import AppStorage
from kiro_crew.apps.context import build_app_context


def test_build_app_context_does_not_create_the_kv_dir(tmp_path: Path) -> None:
    """Constructing the store via the on-loop factory touches no filesystem.

    A ``mkdir`` in ``AppStorage.__init__`` records a call here and leaves the
    directory present after the build, which fails this test.
    """
    data_dir = tmp_path / "data"
    kv_dir = data_dir / "kv"

    real_mkdir = Path.mkdir
    kv_mkdir_calls: list[Path] = []

    def _recording_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == kv_dir:
            kv_mkdir_calls.append(self)
        real_mkdir(self, *args, **kwargs)

    with mock.patch.object(Path, "mkdir", _recording_mkdir):
        ctx = build_app_context("kv-app", data_dir, permissions={"storage": True})

    assert ctx.storage is not None, "the storage SDK was not built"
    assert kv_mkdir_calls == [], "building the context created the kv dir on the loop"
    assert not kv_dir.exists(), "the kv dir must not exist until the first write"


def test_first_set_creates_the_kv_dir(tmp_path: Path) -> None:
    """The first write creates the dir the constructor leaves uncreated."""
    data_dir = tmp_path / "data"
    kv_dir = data_dir / "kv"

    storage = AppStorage("kv-app", data_dir)
    assert not kv_dir.exists(), "the constructor must not create the kv dir"

    storage.set("k", {"v": 1})

    assert kv_dir.is_dir()
    assert storage.get("k") == {"v": 1}


def test_reads_tolerate_a_missing_kv_dir(tmp_path: Path) -> None:
    """Every read behaves as empty before the dir exists, not as an error."""
    storage = AppStorage("kv-app", tmp_path / "data")

    assert storage.get("missing") is None
    assert storage.list_keys() == []
    assert storage.delete("missing") is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode bits")
def test_first_set_creates_the_kv_dir_with_the_prior_permissions(tmp_path: Path) -> None:
    """The lazily-created dir has the mode a plain ``Path.mkdir`` produces.

    The permission model must not change. ``set`` creates the dir with a plain
    ``Path.mkdir(parents=True, exist_ok=True)``, so this compares the mode of the
    dir ``set`` creates against the mode a plain ``mkdir`` produces in the same
    tree, tracking the process umask rather than hard-coding a bit pattern.
    """
    data_dir = tmp_path / "data"
    kv_dir = data_dir / "kv"

    storage = AppStorage("kv-app", data_dir)
    storage.set("k", {"v": 1})

    reference = tmp_path / "reference" / "kv"
    reference.mkdir(parents=True, exist_ok=True)

    actual = stat.S_IMODE(kv_dir.stat().st_mode)
    expected = stat.S_IMODE(reference.stat().st_mode)
    assert actual == expected, f"kv dir mode {oct(actual)} != prior mkdir mode {oct(expected)}"
