"""The launcher's pinned mounts at their edges: what the pin skips, follows and refuses.

``kiro_crew.sandbox_launcher_parts.pinned_mounts`` holds the launcher program's checked
mounts and the no-follow pin every hiding mount goes through. The suites that drive whole
stages reach the common paths; these pin the edges a stage reaches only on an odd host
shape, against real files under ``tmp_path``: a target that names no leaf, a name spelled
with trailing slashes, a relative link, a dangling link, a recorded kind the pass did not
know, a staging directory something already removed, and a masked object that cannot be
read back.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import RecordingLibc, launch, refusal

from kiro_crew import sandbox_launcher_program as program

#: The pin holds a descriptor on a link itself (``O_PATH``), which only Linux has; the
#: namespace launcher runs nowhere else.
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)

#: A descriptor number no test process holds open: far above any per-process limit in use.
_NEVER_OPEN_FD = 1 << 20


def test_a_target_naming_no_leaf_is_skipped_unless_it_is_required(tmp_path: Path) -> None:
    run = launch(tmp_path)
    assert program._pin_mount_path(run, b"/", program._any_kind) == (None, None)
    message = refusal(program._pin_mount_path, run, b"/", program._any_kind, True)
    assert message is not None and "it names no leaf to mask" in message


def test_trailing_slashes_name_the_same_leaf(tmp_path: Path) -> None:
    target = tmp_path / "masked"
    target.mkdir()
    run = launch(tmp_path)
    fd, path = program._pin_mount_path(run, (str(target) + "//").encode(), stat.S_ISDIR)
    assert fd is not None
    try:
        assert os.fstat(fd).st_ino == target.stat().st_ino
        assert path == ("/proc/self/fd/%d" % fd).encode()
    finally:
        os.close(fd)


def test_a_relative_link_is_followed_from_its_own_directory(tmp_path: Path) -> None:
    """A dotfile manager writes relative links; the target resolves against the held parent."""
    real = tmp_path / "real"
    real.mkdir()
    name = tmp_path / "name"
    name.symlink_to("real")
    run = launch(tmp_path)
    fd, _path = program._pin_mount_path(run, str(name).encode(), stat.S_ISDIR)
    assert fd is not None
    try:
        assert os.fstat(fd).st_ino == real.stat().st_ino
    finally:
        os.close(fd)
    assert run.pinned_occupants[str(name)][2] is True


def test_a_dangling_link_is_skipped_unless_it_is_required(tmp_path: Path) -> None:
    name = tmp_path / "name"
    name.symlink_to("gone")
    run = launch(tmp_path)
    target = str(name).encode()
    assert program._pin_mount_path(run, target, stat.S_ISDIR) == (None, None)
    message = refusal(program._pin_mount_path, run, target, stat.S_ISDIR, True)
    assert message is not None and "its link target is absent" in message


def test_a_recorded_kind_the_pass_did_not_know_reads_as_no_kind() -> None:
    assert program._kind_reached(1) == program._S_IFDIR
    assert program._kind_reached(2) == program._S_IFREG
    assert program._kind_reached(0) == program._kind_reached(3) == 0


def test_retiring_a_stage_something_already_removed_still_detaches_it(tmp_path: Path) -> None:
    libc = RecordingLibc()
    run = launch(tmp_path, libc=libc)
    stage = tmp_path / "stage-already-removed"
    assert refusal(program._retire_stage_or_die, run, str(stage), "private window w") is None
    assert libc.detached == [str(stage)]


def test_a_masked_object_whose_identity_cannot_be_read_refuses(tmp_path: Path) -> None:
    run = launch(tmp_path)
    message = refusal(program._register_stand_in, run, (1, 2), _NEVER_OPEN_FD)
    assert message is not None and "cannot read the identity" in message
    assert run.own_stand_ins == {}
