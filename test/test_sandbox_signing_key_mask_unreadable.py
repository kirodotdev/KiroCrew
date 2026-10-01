"""The Linux mask over ``token_signing.key`` refuses reads instead of answering empty.

The launcher hides a sensitive file by binding an empty tmpfs file over it. For most
leaves an empty read is harmless. For the signing key it is not: a data-home copy run
from an agent shell (``rsync``, ``cp -a``, ``tar``) reads the mask and writes a 0-byte
``token_signing.key`` on the destination, which the gateway then refuses to replace and
answers with an ephemeral secret on every boot. The mask source for that leaf is mode 0,
so the copy fails with ``Permission denied`` instead.

These tests run the hiding region lifted verbatim from the shipped launcher, with the
same fake ``_libc`` as ``test_sandbox_mount_checked``: a real bind needs a user
namespace, which a nested sandbox cannot create. The fake records each bind's source
mode and bytes at mount time, since that inode is what the sandboxed process reads
through the bind. The key's mask is then sealed: its stand-in is created mode 0 in a
private tmpfs stage mounted in the launcher's namespace only, bound through the
launcher's own descriptor while it is non-dumpable, its bind is remounted read-only,
and the stage is detached, so no same-uid process keeps a writable path to the inode.
"""

from __future__ import annotations

import ctypes
import os
import runpy
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from test_sandbox_mount_checked import _FakeLibc, _region, _resolved_identity

from kiro_crew import sandbox
from kiro_crew.sandbox import _build_launcher_script

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the namespace launcher is Linux-only, and its stand-ins are bound "
    "through /proc/self/fd, which macOS does not have",
)


class _RecordingLibc(_FakeLibc):
    """Also snapshot each plain bind's source mode and bytes as it is mounted."""

    def __init__(self) -> None:
        super().__init__(fail_at=None)
        #: Keyed by the ``(st_dev, st_ino)`` the bind target resolved to, because the
        #: launcher binds onto a pinned ``/proc/self/fd/<n>`` path, not the name.
        self.seen: dict[tuple[int, int] | None, tuple[int, bytes | None]] = {}
        #: Whether the process was dumpable when each plain bind was made.
        self.dumpable_at_bind: dict[tuple[int, int] | None, bool] = {}
        self.dumpable = True
        self.prctls: list[tuple[int, int]] = []
        self.tmpfs_mounts: list[tuple[bytes, int]] = []
        self.refuse_tmpfs = False
        self.real_source: dict[tuple[int, int] | None, str] = {}

    def prctl(self, option, arg2, _a3, _a4, _a5):  # noqa: ANN001
        self.prctls.append((option, arg2))
        if option == 4:  # PR_SET_DUMPABLE
            self.dumpable = bool(arg2)
        return 0

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001
        if fstype is not None:
            self.tmpfs_mounts.append((fstype, flags))
            if self.refuse_tmpfs:
                ctypes.set_errno(1)  # EPERM
                return -1
        elif source is not None and not flags & 32:
            src = os.fsdecode(source)
            mode = stat.S_IMODE(os.stat(src).st_mode)
            try:
                content: bytes | None = Path(src).read_bytes()
            except PermissionError:
                content = None
            self.seen[_resolved_identity(target)] = (mode, content)
            self.dumpable_at_bind[_resolved_identity(target)] = self.dumpable
            self.real_source[_resolved_identity(target)] = os.path.realpath(src)
        return super().mount(source, target, fstype, flags, data)


@pytest.fixture(autouse=True)
def _pin_ssh_accept_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``_build_launcher_script`` from spawning a real ``ssh -V``."""
    monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)


def _hide(
    tmp_path: Path, level: str, *, prctl: bool = True, refuse_tmpfs: bool = False
) -> tuple[_RecordingLibc, Path, Path, Path]:
    """Run the hiding region against a fake home; return the libc and paths."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    key = crew / "token_signing.key"
    key.write_bytes(os.urandom(32))
    netrc = home / ".netrc"
    netrc.write_text("machine example.com\n")
    ssh = home / ".ssh"
    ssh.mkdir()
    src_dir = tmp_path / "tmpfs"
    src_dir.mkdir()

    libc = _RecordingLibc()
    if prctl:
        # What Step 2 of the launcher does before unshare(CLONE_NEWNS), which
        # this region does not include.
        libc.prctl(4, 0, 0, 0, 0)
    libc.refuse_tmpfs = refuse_tmpfs
    ns = {
        "_libc": libc,
        "_MS_BIND": 4096,
        "_MS_REC": 16384,
        "_MS_PRIVATE": 1 << 18,
        "_MS_RDONLY": 1,
        "_MS_REMOUNT": 32,
        "_MS_NOSUID": 2,
        "_MS_NODEV": 4,
        "_MS_NOEXEC": 8,
        "ctypes": ctypes,
        "os": os,
        "sys": sys,
        "tempfile": tempfile,
        "_tmpfs_src": str(src_dir),
        "_src_prefix": "kirocrew_sb_%d_" % os.getpid(),
        "expose_data": {},
        "EXPOSE_FILES": [],
        "SENSITIVE_DIRS": [],
        "PRIVATE_DIRS": [],
        "READONLY_DIRS": [],
        "WRITABLE_DIRS": [],
        "SENSITIVE_FILES": [str(key), str(netrc)],
        "FAIL_CLOSED_FILE_MASKS": [],
        "REQUIRED_MASK_TARGETS": frozenset(),
        "SENSITIVE_DIR_IDS": {},
        "PRIVATE_DIR_IDS": {},
        "_MNT_DETACH": 2,
        "stat": stat,
        "SSH_DIR": str(ssh),
        "SSH_KNOWN_HOSTS": str(ssh / "known_hosts"),
        "HIDE_SSH": False,
        "_locked_mount_flags": lambda _target: 0,
        "_PR_SET_DUMPABLE": 4,
        "_launcher_nondumpable": prctl,
    }
    region_file = tmp_path / "region.py"
    # The stage freshness check asks whether a REAL tmpfs now covers the stage,
    # which this recording ``_libc`` cannot produce; neutralise it as _region
    # neutralises the post-mount name check.
    region = _region(_build_launcher_script(level))
    fresh = "def _stage_is_fresh_mount(dfd, parent):"
    assert fresh in region, "the stage freshness helper was renamed"
    region = region.replace(
        fresh, fresh + "\n    return True\ndef _stage_check_unused(dfd, parent):"
    )
    region_file.write_text(region)
    runpy.run_path(str(region_file), init_globals=ns)
    return libc, key, netrc, src_dir


def _ident(path: Path) -> tuple[int, int]:
    st = os.lstat(path)
    return (st.st_dev, st.st_ino)


def _bind_source(libc: _RecordingLibc, target: Path) -> Path:
    return next(
        Path(os.fsdecode(src))
        for (src, _tgt, flags), ident in zip(libc.calls, libc.resolved)
        if ident == _ident(target) and not flags & 32 and src != b"tmpfs"
    )


@pytest.mark.parametrize("level", ["strict", "standard"])
def test_signing_key_mask_source_is_unreadable(tmp_path: Path, level: str) -> None:
    """The key's mask source carries no permission bits; other masks keep theirs."""
    libc, key, netrc, _src = _hide(tmp_path, level)

    assert libc.seen[_ident(key)][0] == 0
    # The control: an ordinary hidden file still reads as empty, unchanged.
    assert libc.seen[_ident(netrc)] == (0o600, b"")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a mode-0 file, so the refusal cannot be observed",
)
def test_a_copy_through_the_signing_key_mask_fails(tmp_path: Path) -> None:
    """Reading the mask raises, so a copy cannot carry zero bytes out as the key."""
    libc, key, _netrc, _src = _hide(tmp_path, "strict")

    assert libc.seen[_ident(key)][1] is None


@pytest.mark.parametrize("level", ["strict", "standard"])
def test_signing_key_mask_cannot_be_chmodded_back(tmp_path: Path, level: str) -> None:
    """The stand-in lives only in a private stage tmpfs, and its bind is read-only.

    The sandboxed uid owns the mode-0 inode. A stand-in named in the shared tmpfs
    could be chmodded readable again, or swapped for a symlink that redirects a
    chmod onto the real key. So the key's stand-in is created mode 0 in a tmpfs
    mounted over a stage directory in this namespace only, bound through this
    process's own descriptor while it is non-dumpable. The bind is then remounted
    read-only and the stage is detached, so no writable path to the inode remains.
    """
    libc, key, netrc, src_dir = _hide(tmp_path, level)

    remounts = [
        flags for _s, tgt, flags in libc.calls if os.fsdecode(tgt) == str(key) and flags & 32
    ]
    assert len(remounts) == 1
    assert remounts[0] & 1 and remounts[0] & 4096  # MS_RDONLY | MS_BIND
    # One private tmpfs, nosuid/nodev/noexec, detached again afterwards.
    assert libc.tmpfs_mounts == [(b"tmpfs", 2 | 4 | 8)]
    assert [flags for _t, flags in libc.unmounts] == [2]  # MNT_DETACH
    # Bound through a descriptor onto a file created in that stage, never through
    # a name in the shared tmpfs root.
    assert str(_bind_source(libc, key)).startswith("/proc/self/fd/")
    real = Path(libc.real_source[_ident(key)])
    assert real.name == "stand-in" and real.parent.parent == src_dir
    assert not libc.dumpable_at_bind[_ident(key)]
    assert libc.dumpable, "dumpable must be restored once the masks are in place"
    # The control: ordinary masks keep their named source, for the janitor.
    assert _bind_source(libc, netrc).parent == src_dir


def test_without_a_private_stage_the_key_mask_reads_empty(tmp_path: Path) -> None:
    """No prctl means no safe private stage, so the key keeps main's readable mask."""
    libc, key, _netrc, src_dir = _hide(tmp_path, "strict", prctl=False)

    assert libc.seen[_ident(key)] == (0o600, b"")
    assert _bind_source(libc, key).parent == src_dir
    assert not [
        flags for _s, tgt, flags in libc.calls if os.fsdecode(tgt) == str(key) and flags & 32
    ]


def test_a_refused_private_tmpfs_warns_on_its_own_line_and_falls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning ends in a real newline, so a later refusal line stays readable."""
    libc, key, _netrc, src_dir = _hide(tmp_path, "strict", refuse_tmpfs=True)

    assert libc.seen[_ident(key)] == (0o600, b"")
    assert libc.dumpable
    assert len(list(src_dir.iterdir())) == 2  # both ordinary named masks, no stage
    err = capsys.readouterr().err
    assert err.startswith("sandbox: WARNING -- could not mount a private tmpfs")
    assert err.endswith("instead.\n") and "\\n" not in err


@pytest.mark.parametrize("level", ["strict", "cc", "standard"])
def test_the_launcher_is_non_dumpable_before_its_mount_namespace_exists(level: str) -> None:
    """A /proc/<pid>/root opened after unshare(CLONE_NEWNS) would reach the stage.

    So dumpability is cleared after the parent has written the id maps and
    before the mount namespace is created, and restored only after the
    sensitive-file loop, which is where the stage is detached.
    """
    script = _build_launcher_script(level)
    maps = script.index("os.read(p2c_r, 1)  # wait for maps")
    clear = script.index("_libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)")
    newns = script.index("_libc.unshare(_CLONE_NEWNS)")
    retire = script.index("_retire_unreadable_stage(_sealed, f)")
    restore = script.index("_libc.prctl(_PR_SET_DUMPABLE, 1, 0, 0, 0)")
    assert maps < clear < newns < retire < restore
    assert script.count("_PR_SET_DUMPABLE, 0") == 1
    assert script.count("_PR_SET_DUMPABLE, 1") == 1


def test_unreadable_set_names_only_the_signing_key() -> None:
    """Widening the set changes what sandboxed readers see, so it is pinned here."""
    assert getattr(sandbox, "_CREW_UNREADABLE_MASK_LEAVES", None) == frozenset(
        {"token_signing.key"}
    )
