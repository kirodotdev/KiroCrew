"""The launcher's stand-ins live on a tmpfs of its own namespace, off the host roots.

``stage_stand_ins_privately`` mounts that tmpfs on one stage directory under the chosen
stand-in root and points ``tmpfs_src`` at it through a descriptor; ``retire_stand_in_stage``
detaches it once the masks are placed and removes the empty host directory. Both are
driven here in-process with a stand-in libc: one that records the tmpfs mount, and one
that refuses it, which must leave the stand-ins on the host root as before. What a real
host deletion does to a running sandbox is in ``test_stand_in_stage_real_sandbox.py``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, launch

from kiro_crew import sandbox_launcher_program as program

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)

_MS_NOSUID = 2
_MS_NODEV = 4
_MNT_DETACH = 2


class _RefusingTmpfsLibc(RecordingLibc):
    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if fstype == b"tmpfs":
            ctypes.set_errno(errno.EPERM)
            return -1
        return 0


def _host_root(run: program.Launch) -> str:
    assert run.tmpfs_src is not None
    return run.tmpfs_src


def test_the_stand_ins_move_onto_a_private_tmpfs_and_the_stage_is_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stand-in libc mounts nothing, so the stage stays on the host filesystem; the
    # fresh-mount check is what a real tmpfs passes, and is pinned to that answer.
    monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)
    libc = RecordingLibc()
    run = launch(tmp_path, libc=libc)
    host_root = _host_root(run)

    program.stage_stand_ins_privately(run)

    ((call,),) = [libc.calls]
    assert call.fstype == b"tmpfs"
    assert call.flags == _MS_NOSUID | _MS_NODEV
    (stage,) = os.listdir(host_root)
    assert stage.startswith(run.src_prefix)
    assert call.target_path == os.path.join(host_root, stage)
    # Every stand-in is now created through the descriptor on the stage's root.
    assert run.tmpfs_src.startswith("/proc/self/fd/")
    stand_in = program._stand_in_mkdtemp(run)
    assert os.path.dirname(os.path.realpath(stand_in)) == os.path.join(host_root, stage)
    os.rmdir(stand_in)

    program.retire_stand_in_stage(run)

    assert libc.detached and libc.detached[-1].startswith("/proc/self/fd/")
    assert os.listdir(host_root) == []
    assert run.tmpfs_src == host_root
    assert run.stand_in_stage is None
    program.retire_stand_in_stage(run)  # a second call has nothing left to do
    assert len(libc.detached) == 1


def test_a_refused_tmpfs_keeps_the_stand_ins_on_the_host_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    libc = _RefusingTmpfsLibc()
    run = launch(tmp_path, libc=libc)
    host_root = _host_root(run)

    program.stage_stand_ins_privately(run)

    assert run.tmpfs_src == host_root
    assert run.stand_in_stage is None
    assert os.listdir(host_root) == []
    assert "errno %d" % errno.EPERM in capsys.readouterr().err
    program.retire_stand_in_stage(run)
    assert libc.detached == []


def test_a_stage_that_is_not_a_fresh_mount_keeps_the_host_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A mount the libc reports done that left the stage on its parent's filesystem is
    # not a stage to create stand-ins in.
    run = launch(tmp_path, libc=RecordingLibc())
    host_root = _host_root(run)

    program.stage_stand_ins_privately(run)

    assert run.tmpfs_src == host_root
    assert run.stand_in_stage is None
    assert os.listdir(host_root) == []
    assert "the stage was replaced" in capsys.readouterr().err


def test_a_host_root_that_vanishes_after_the_mount_does_not_end_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # logind can remove /run/user/$UID at any moment; the fresh-mount check compares
    # devices read off descriptors and never looks the host root up again.
    run = launch(tmp_path, libc=RecordingLibc())
    host_root = _host_root(run)
    real_stat = os.stat

    def _stat(path, *args, **kwargs):  # noqa: ANN001, ANN202
        if os.fspath(path) == host_root:
            raise FileNotFoundError(errno.ENOENT, "removed by logind", host_root)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _stat)

    program.stage_stand_ins_privately(run)

    assert run.tmpfs_src == host_root
    assert "the stage was replaced" in capsys.readouterr().err


def test_a_stage_that_will_not_detach_stays_mounted_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)

    class _Stuck(RecordingLibc):
        def umount2(self, target, flags):  # noqa: ANN001, ANN201
            super().umount2(target, flags)
            ctypes.set_errno(errno.EBUSY)
            return -1

    run = launch(tmp_path, libc=_Stuck())
    host_root = _host_root(run)
    program.stage_stand_ins_privately(run)

    program.retire_stand_in_stage(run)

    assert run.tmpfs_src == host_root
    assert len(os.listdir(host_root)) == 1  # left in place: it is still a mount point
    assert "errno %d" % errno.EBUSY in capsys.readouterr().err
