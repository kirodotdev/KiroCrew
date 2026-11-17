"""A launcher's stage directory is reclaimed without proof that every task was read.

``stage_stand_ins_privately`` makes one stage directory per launch on the host stand-in
root and mounts a tmpfs of the launcher's own namespace on it; the unreadable mask's stage
is built the same way. A launcher that ends before it retires a stage (a refusal during
setup, a SIGKILL) leaves the empty directory behind. A stage is a mount point and never a
bind source, so the janitor removes a dead launcher's empty stage even when the pin scan
cannot prove it read every task that could hold a mount, which is every scan on a
``hidepid`` procfs. A stand-in the launcher made on the host root is a bind source and
keeps the full gate. The stages are made by the launcher's own code, driven in-process
with a stand-in libc that mounts nothing.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, launch
from test_sandbox_mount_source_sweep import _pin

from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_mount_sweep
from kiro_crew.sandbox import _cleanup_stale_sandbox_mount_sources

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)


def _private_stage_left(run: program.Launch, monkeypatch: pytest.MonkeyPatch) -> str:
    """The stage ``stage_stand_ins_privately`` made, as a launcher killed before retiring
    it leaves it on the host root."""
    # The stand-in libc mounts nothing; the fresh-mount check is pinned to the answer a
    # real tmpfs gives.
    monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)
    host_root = run.tmpfs_src
    program.stage_stand_ins_privately(run)
    assert run.stand_in_stage is not None
    root_fd, stage, _ = run.stand_in_stage
    os.close(root_fd)  # the launcher's descriptor goes with the launcher
    run.stand_in_stage, run.tmpfs_src = None, host_root
    return stage


def _unreadable_stage_left(run: program.Launch, monkeypatch: pytest.MonkeyPatch) -> str:
    """The unreadable mask's stage, as a launcher killed before retiring it leaves it."""
    monkeypatch.setattr(program, "_stage_is_fresh_mount", lambda _fd, _parent: True)
    run.nondumpable = True
    opened = program._open_unreadable_stand_in(run, "token_signing.key")
    assert opened is not None
    file_fd, stage_fd, stage = opened
    os.close(file_fd)
    os.close(stage_fd)
    # On a host the mode-0 stand-in lives in the stage's own tmpfs, which goes with the
    # namespace; this libc mounted nothing, so it sits in the host directory here.
    os.unlink(os.path.join(stage, "stand-in"))
    return stage


def _age(*paths: str) -> None:
    """Put *paths* past the age backstop.

    The pid in every name is this live process, so an aged entry takes the recycled-pid
    path, which reaches the same gate a dead pid does.
    """
    for path in paths:
        os.utime(path, (0, 0))


_STAGES = pytest.mark.parametrize(
    "left", [_private_stage_left, _unreadable_stage_left], ids=["private", "unreadable"]
)


@_STAGES
def test_a_stage_a_dead_launcher_left_is_reclaimed_on_an_unproven_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    left: Callable[[program.Launch, pytest.MonkeyPatch], str],
) -> None:
    run = launch(tmp_path, libc=RecordingLibc())
    host_root = run.tmpfs_src
    assert host_root is not None
    stage = left(run, monkeypatch)
    # What a launcher whose private stage could not be mounted binds over a mask target.
    stand_in = program._stand_in_mkdtemp(run)
    assert os.path.dirname(stage) == os.path.dirname(stand_in) == host_root
    _age(stage, stand_in)
    # Neither proof: the scan is incomplete and did not read every task that could
    # hold a mount, as on a hidepid procfs.
    _pin(monkeypatch, set(), complete=False, covered=False)

    removed = _cleanup_stale_sandbox_mount_sources(roots=[host_root])

    assert removed == 1
    assert not os.path.lexists(stage)
    assert os.path.isdir(stand_in)


@_STAGES
def test_a_stage_a_readable_mount_names_as_its_source_is_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    left: Callable[[program.Launch, pytest.MonkeyPatch], str],
) -> None:
    run = launch(tmp_path, libc=RecordingLibc())
    stage = left(run, monkeypatch)
    _age(stage)
    _pin(monkeypatch, {os.path.basename(stage)}, complete=False, covered=False)

    assert _cleanup_stale_sandbox_mount_sources(roots=[os.path.dirname(stage)]) == 0
    assert os.path.isdir(stage)


def test_a_stage_with_contents_waits_for_the_full_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = launch(tmp_path, libc=RecordingLibc())
    stage = _private_stage_left(run, monkeypatch)
    planted = Path(stage, "planted")
    planted.write_text("x", encoding="utf-8")
    _age(stage)
    root = os.path.dirname(stage)

    _pin(monkeypatch, set(), complete=False, covered=False)
    assert _cleanup_stale_sandbox_mount_sources(roots=[root]) == 0
    assert planted.exists()

    _pin(monkeypatch, set(), complete=True)
    assert _cleanup_stale_sandbox_mount_sources(roots=[root]) == 1
    assert not os.path.lexists(stage)


def test_no_stand_in_name_can_carry_the_stage_mark() -> None:
    """The janitor reads the launcher's mark, and a stand-in's random suffix cannot
    spell it: ``tempfile`` draws that suffix from an alphabet without the mark's ``-``."""
    assert sandbox_mount_sweep._MOUNT_SOURCE_STAGE_MARK == program._STAGE_MARK
    alphabet = set(tempfile._RandomNameSequence.characters)
    assert not set(program._STAGE_MARK) <= alphabet
