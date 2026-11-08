"""``_pod_checkout_guard`` compares the pod's pin with the worktree off the loop.

The comparison resolves two paths, and each ``Path.resolve()`` is a realpath
walk on disk. The guard runs before every pod verb, so it does that walk in an
``asyncio.to_thread`` hop. These tests pin the hop and pin that a matching pin,
a pin reached through a symlink, a foreign pin and a resolve failure give the
same verdicts and refusal strings as before.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.apps.builtins.dev_fleet import repository, runtime
from kiro_crew.apps.builtins.dev_fleet import worktree_ops_pods as pods

_NAME = "kirocrew-wt-feature"


@pytest.fixture
def resolve_threads(monkeypatch):
    """Record, per resolve of a path under the test's tmp dir, whether it ran on the loop thread."""
    seen: list[tuple[str, bool]] = []
    real_resolve = Path.resolve

    def _recording_resolve(self, *args, **kwargs):
        if _NAME in str(self):
            seen.append((str(self), threading.current_thread() is threading.main_thread()))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _recording_resolve)
    return seen


async def _guard(ours: Path, pinned: str):
    with (
        patch.object(
            repository,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime, "_load_cfg", return_value=object()),
        patch.object(pods, "_read_pin_strict", return_value=(True, pinned)),
    ):
        return await pods._pod_checkout_guard(_NAME)


@pytest.mark.asyncio
async def test_matching_pin_proceeds_and_resolves_off_the_loop(tmp_path, resolve_threads):
    ours = tmp_path / "repo-a" / _NAME
    ours.mkdir(parents=True)

    assert await _guard(ours, str(ours)) is None
    assert len(resolve_threads) == 2
    assert [on_loop for _, on_loop in resolve_threads] == [False, False]


@pytest.mark.asyncio
async def test_foreign_pin_is_refused_and_resolves_off_the_loop(tmp_path, resolve_threads):
    ours = tmp_path / "repo-a" / _NAME
    ours.mkdir(parents=True)
    foreign = tmp_path / "repo-b" / _NAME
    foreign.mkdir(parents=True)

    err = await _guard(ours, str(foreign))

    assert err == (
        f"pod {_NAME!r} is pinned to a different checkout — refusing "
        "cross-repository pod operation (basename collision)"
    )
    assert len(resolve_threads) == 2
    assert [on_loop for _, on_loop in resolve_threads] == [False, False]


@pytest.mark.skipif(os.name == "nt", reason="directory symlinks need privilege on Windows")
@pytest.mark.asyncio
async def test_pin_through_a_symlink_names_the_same_checkout(tmp_path):
    ours = tmp_path / "repo-a" / _NAME
    ours.mkdir(parents=True)
    link = tmp_path / f"link-{_NAME}"
    link.symlink_to(ours, target_is_directory=True)

    assert await _guard(ours, str(link)) is None


@pytest.mark.asyncio
async def test_a_resolve_failure_keeps_its_refusal(tmp_path, monkeypatch):
    ours = tmp_path / "repo-a" / _NAME
    ours.mkdir(parents=True)
    calls: list[bool] = []

    def _failing(pinned, path):
        calls.append(threading.current_thread() is threading.main_thread())
        raise OSError("loop in symlinks")

    monkeypatch.setattr(pods, "_same_checkout", _failing)

    err = await _guard(ours, str(ours))

    assert err == "cannot resolve checkout paths for pod guard: loop in symlinks"
    assert calls == [False]
