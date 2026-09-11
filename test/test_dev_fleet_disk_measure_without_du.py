"""Dev Fleet disk measurement on a host where ``du`` does not resolve.

Native Windows has no reachable ``du``: Git for Windows ships it under
``Git\\usr\\bin``, which is not one of :data:`runtime._TRUSTED_BIN_DIRS`, so
:func:`runtime._trusted_bin` fails closed for it. The measurement helpers must
then produce a real number from the portable ``os.scandir`` walk -- absent or
zero would both be wrong on a figure the app advertises as working anywhere.

Every test here stubs the RESOLVER (``runtime._trusted_bin``), not the command
output. A test that stubbed ``_run_cmd`` alone would take the ``du`` branch on a
host that has ``du`` and the walk branch on one that does not, so it would pass
on Linux CI and prove nothing about the Windows shard. Stubbing the resolver
makes the branch under test the same on every platform, and nothing here spawns
a subprocess or depends on a host tool.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import fleet_state, runtime

# An opaque stand-in for a resolved ``du``. Built from the platform temp dir
# rather than written as a POSIX literal so the file carries no path that
# cannot exist on Windows. Nothing executes it: the calls that see it resolve
# have ``_run_cmd`` stubbed.
_FAKE_DU = os.path.join(tempfile.gettempdir(), "kc-fake-du")

# A byte count no fixture below adds up to, so a test that wrongly took the
# ``du`` branch fails on the VALUE as well as on the await-count assertion.
_WRONG = "999999\t."


def _tree(root, sizes: dict[str, int]) -> int:
    """Materialize ``{relative posix-ish path: byte count}`` under *root*.

    Returns the exact total. Written with ``write_bytes`` so the size on disk is
    the size asked for: ``write_text`` would translate ``\\n`` to ``\\r\\n`` on
    Windows and inflate every count by one byte per line.
    """
    total = 0
    for rel, size in sizes.items():
        target = root.joinpath(*rel.split("|"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        total += size
    return total


@pytest.mark.asyncio
async def test_measure_dir_bytes_walks_when_du_does_not_resolve(monkeypatch, tmp_path):
    """No ``du`` on the host means a measured byte count, not None and not 0."""
    root = tmp_path / "tree"
    expected = _tree(root, {"a.bin": 1000, "sub|b.bin": 2000, "sub|deep|c.bin": 3000})

    run_cmd = AsyncMock(return_value=(0, _WRONG, ""))
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: None)
    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)

    size = await fleet_state._measure_dir_bytes(str(root), timeout=5)

    assert size == expected
    assert run_cmd.await_count == 0, "the walk branch must not spawn anything"


@pytest.mark.asyncio
async def test_measure_dir_mb_walks_when_du_does_not_resolve(monkeypatch, tmp_path):
    """The MB helper reports whole MB from the same walk."""
    root = tmp_path / "tree"
    _tree(root, {"big.bin": 2 * 1024 * 1024, "small.bin": 10})

    run_cmd = AsyncMock(return_value=(0, _WRONG, ""))
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: None)
    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)

    assert await fleet_state._measure_dir_mb(str(root), timeout=5) == 2
    assert run_cmd.await_count == 0


@pytest.mark.asyncio
async def test_a_resolvable_du_that_fails_reports_not_measured(monkeypatch, tmp_path):
    """A failed ``du`` is None, never silently re-measured by the walk.

    The walk is the fallback for a host with no ``du``. Re-running a FAILED
    measurement a different way would publish a number whose meaning differs
    from the one the caller asked for, under the same label.
    """
    root = tmp_path / "tree"
    _tree(root, {"a.bin": 4096})

    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: _FAKE_DU)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(1, "", "du: boom")))

    assert await fleet_state._measure_dir_bytes(str(root), timeout=5) is None
    assert await fleet_state._measure_dir_mb(str(root), timeout=5) is None


@pytest.mark.asyncio
async def test_unparsable_du_output_reports_not_measured(monkeypatch, tmp_path):
    """A zero exit with output that carries no leading integer is not a number."""
    root = tmp_path / "tree"
    _tree(root, {"a.bin": 4096})

    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: _FAKE_DU)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))

    assert await fleet_state._measure_dir_bytes(str(root), timeout=5) is None
    assert await fleet_state._measure_dir_mb(str(root), timeout=5) is None


@pytest.mark.asyncio
async def test_missing_root_is_unmeasured_not_zero(monkeypatch, tmp_path):
    """A root that is not a directory reports None so the UI can say so."""
    monkeypatch.setattr(runtime, "_trusted_bin", lambda name: None)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, _WRONG, "")))

    gone = tmp_path / "never-created"
    assert await fleet_state._measure_dir_bytes(str(gone), timeout=5) is None
    assert await fleet_state._measure_dir_mb(str(gone), timeout=5) is None


def test_dir_size_bytes_is_empty_tree_zero_not_none(tmp_path):
    """An existing but empty directory is a measured 0, distinct from None."""
    root = tmp_path / "empty"
    root.mkdir()
    assert fleet_state._dir_size_bytes(str(root)) == 0


def test_dir_size_bytes_skips_an_unreadable_subtree(monkeypatch, tmp_path):
    """A subtree that cannot be scanned is skipped, not fatal to the total."""
    root = tmp_path / "tree"
    expected = _tree(root, {"a.bin": 1500})
    blocked = root / "blocked"
    blocked.mkdir()

    real_scandir = os.scandir

    def fake_scandir(path):
        if str(path) == str(blocked):
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(fleet_state.os, "scandir", fake_scandir)

    assert fleet_state._dir_size_bytes(str(root)) == expected
