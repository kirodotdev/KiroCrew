"""Where the private stand-in stage and the sealed stand-in's entry lead, as a kernel keeps mounts.

The fake libcs of ``test_sandbox_launcher_program`` and ``test_scratch_root_seal`` place a mount
by renaming its source onto its target, and read a read-only flag back by inode. That answers
right while a stand-in has one path, and wrong for the questions here: a bind leaves its
source's entry in place, the read-only flag belongs to one mount and not to the directory, a
recursive bind copies every mount below it, and a step that reaches a mount through a
descriptor opened before that copy acts on the original while every path reaches the copy.
The round-12 launcher pinned the seals' parents after it had mounted its stand-in stage under
one of them. On a real kernel its cover and its detach then landed on the original stage, the
copy kept every stand-in writable at its host path, and the last check refused nearly every
spawn, while both fakes said it passed.

:class:`_Namespace` keeps a mount table as the kernel does for these steps, next to the
renaming fakes. Each launcher case drives ``run_child`` itself, with the steps after the masks
stubbed, so the order judged is the one the launcher uses. The same short sequence of mount
calls (:data:`PRIMITIVES`) runs against the model here and against a real kernel in
``test_scratch_root_seal_real_sandbox.py``, and both must give :data:`KERNEL_ANSWERS`.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_sandbox_launcher_program import RecordingLibc, _resolved, launch, payload, refusal
from test_scratch_root_seal import (
    _MS_BIND,
    _MS_RDONLY,
    _MS_REC,
    _MS_REMOUNT,
    _below_fd,
    _Scratch,
    _SealingLibc,
    _StagingLibc,
)

from kiro_crew import sandbox_launcher_program as program

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher stages pin with os.O_PATH and mount through /proc/self/fd",
)

_FD_PREFIX = "/proc/self/fd/"


def _spelled(path: Any) -> str:
    return os.fsdecode(path) if isinstance(path, bytes) else str(path)


def _bare_fd(spelling: str) -> bool:
    """Whether *spelling* is ``/proc/self/fd/<n>`` with nothing after the number."""
    return spelling.startswith(_FD_PREFIX) and spelling[len(_FD_PREFIX) :].isdigit()


def _normal(path: str) -> str:
    """*path* made absolute with every component but the last resolved, as a mount names it."""
    parent, leaf = os.path.split(os.path.abspath(path))
    return os.path.join(os.path.realpath(parent), leaf) if leaf else parent


def _at_or_below(path: str, point: str) -> bool:
    return path == point or path.startswith(point.rstrip("/") + "/")


@dataclass(eq=False)
class _Mount:
    """One mount: where it is attached, what it was attached on, and its read-only flag."""

    point: str
    parent: _Mount | None
    read_only: bool
    #: The directory it is mounted on, as ``(st_dev, st_ino)``: ``rmdir`` of that is EBUSY.
    point_id: tuple[int, int] | None
    #: A tmpfs's own token, which every copy of it shares; ``None`` for a bind.
    tmpfs: object | None = None
    attached: bool = True
    #: Whether a path lookup can reach it: no later mount covers it at or above its place.
    visible: bool = True
    #: Its mount ID, as ``/proc/self/mountinfo`` and a descriptor's ``fdinfo`` give it.
    ident: int = 0

    def descends_from(self, other: _Mount) -> bool:
        up = self.parent
        while up is not None:
            if up is other:
                return True
            up = up.parent
        return False


class _Namespace:
    """A mount table, kept as the kernel keeps a namespace's binds, tmpfs mounts and detaches.

    * A path reaches the deepest visible mount at or above it, the latest of several at one place.
    * A descriptor names the mount its path reached when it was opened, and keeps naming it;
      ``/proc/self/fd/<n>/rest`` and a ``dir_fd`` lookup walk on from that mount, into its
      own child mounts only.
    * A new mount hides every visible mount at or below its place. A recursive bind copies the
      source's child mounts below the source path, and paths reach the copies from then on.
    * A bind takes its read-only flag from the mount its source reaches (``clone_mnt``); a
      ``MS_REMOUNT|MS_BIND`` sets the flag on the mount at that place only.
    * A detach takes the mount and every mount below it out. When it was the last mount of its
      tmpfs, the tmpfs's files go with it, so the directory it sat on reads empty again. A
      detach does not bring back a mount it covered; no case here covers one.
    * ``rmdir`` of a directory an attached mount sits on fails with EBUSY.
    * ``/proc/self/mountinfo`` lists every attached mount, a hidden one too, by its ID and
      place, and a descriptor's ``fdinfo`` gives the ID of the mount it names.
    """

    def __init__(self) -> None:
        self.host = _Mount("/", None, False, None, ident=1)
        self.mounts: list[_Mount] = [self.host]
        #: Each descriptor ``os.open`` returned, as ``(path, mount)``.
        self.fds: dict[int, tuple[str, _Mount]] = {}
        #: Where :meth:`publish` writes the table as ``/proc/self/mountinfo`` lines.
        self.mountinfo: Path | None = None

    def _add(self, mount: _Mount) -> None:
        mount.ident = len(self.mounts) + 1
        self.mounts.append(mount)

    def publish(self) -> None:
        """Write every attached mount as a ``mountinfo`` line, with its place escaped."""
        if self.mountinfo is None:
            return
        lines = []
        for mount in self.mounts:
            if mount.attached:
                point = mount.point.replace("\\", "\\134").replace(" ", "\\040")
                parent = mount.parent.ident if mount.parent is not None else 1
                lines.append("%d %d 0:0 / %s rw - model model rw\n" % (mount.ident, parent, point))
        self.mountinfo.write_text("".join(lines), encoding="utf-8")

    def mount_id(self, fd: int) -> bytes | None:
        """The ID ``fdinfo`` gives for *fd*, or ``None`` for a descriptor the model did not see."""
        return str(self.fds[fd][1].ident).encode() if fd in self.fds else None

    def reach(self, path: str) -> _Mount:
        best = self.host
        for mount in self.mounts:
            if mount.attached and mount.visible and _at_or_below(path, mount.point):
                if len(mount.point) >= len(best.point):
                    best = mount
        return best

    def _walk(self, base: _Mount, base_path: str, rest: str) -> tuple[str, _Mount]:
        full = os.path.normpath(os.path.join(base_path, rest)) if rest else base_path
        if base.visible:
            reached = self.reach(full)
            return full, reached if reached is base or reached.descends_from(base) else base
        best = base
        for mount in self.mounts:
            if mount.attached and mount.descends_from(base) and _at_or_below(full, mount.point):
                if len(mount.point) >= len(best.point):
                    best = mount
        return full, best

    def _fd(self, fd: int) -> tuple[str, _Mount]:
        if fd in self.fds:
            return self.fds[fd]
        path = os.readlink(_FD_PREFIX + str(fd))
        return path, self.reach(path)

    def resolve(self, spelling: Any, dir_fd: int | None = None) -> tuple[str, _Mount]:
        """``(path, mount)``: what a lookup of *spelling* reaches now."""
        text = _spelled(spelling)
        if dir_fd is not None and not os.path.isabs(text):
            base_path, base = self._fd(dir_fd)
            return self._walk(base, base_path, text)
        if text.startswith(_FD_PREFIX):
            number, _slash, rest = text[len(_FD_PREFIX) :].partition("/")
            base_path, base = self._fd(int(number))
            return self._walk(base, base_path, rest)
        path = _normal(text)
        return path, self.reach(path)

    def attach(
        self, target: Any, read_only: bool, *, tmpfs: object | None = None, copy: Any = None
    ) -> _Mount:
        """Mount over *target*; a recursive bind passes its source as *copy*."""
        path, below = self.resolve(target)
        try:
            info = os.stat(_spelled(target))
            point_id: tuple[int, int] | None = (info.st_dev, info.st_ino)
        except OSError:
            point_id = None
        source = self.resolve(copy) if copy is not None else None
        # Read before this mount hides anything: a copy is reachable where its original was.
        seen = {id(mount) for mount in self.mounts if mount.visible}
        new = _Mount(path, below, read_only, point_id, tmpfs, visible=below.visible)
        if new.visible:
            for mount in self.mounts:
                if mount.visible and _at_or_below(mount.point, path):
                    mount.visible = False
        self._add(new)
        if source is not None:
            self._copy_below(source[1], source[0], new, seen)
        self.publish()
        return new

    def _copy_below(self, source: _Mount, source_path: str, onto: _Mount, seen: set[int]) -> None:
        copies: dict[int, _Mount] = {id(source): onto}
        for mount in list(self.mounts):
            parent = copies.get(id(mount.parent))
            if not mount.attached or mount is onto or parent is None:
                continue
            if not _at_or_below(mount.point, source_path):
                continue
            twin = _Mount(
                onto.point + mount.point[len(source_path.rstrip("/")) :],
                parent,
                mount.read_only,
                mount.point_id,
                mount.tmpfs,
                visible=onto.visible and id(mount) in seen,
            )
            self._add(twin)
            copies[id(mount)] = twin

    def detach(self, target: Any) -> bool:
        path, mount = self.resolve(target)
        if mount is self.host or mount.point != path:
            return False
        for gone in self.mounts:
            if gone is mount or gone.descends_from(mount):
                gone.attached = gone.visible = False
        self.publish()
        token = mount.tmpfs
        if token is not None and not any(m.attached and m.tmpfs is token for m in self.mounts):
            spelled = _spelled(target)
            directory = os.readlink(spelled) if _bare_fd(spelled) else _spelled(_below_fd(target))
            for name in os.listdir(directory):
                entry = os.path.join(directory, name)
                if os.path.isdir(entry) and not os.path.islink(entry):
                    shutil.rmtree(entry)
                else:
                    os.unlink(entry)
        return True

    def on(self, path: Any) -> list[_Mount]:
        """Every attached mount that sits on the directory *path* names now."""
        info = os.lstat(_spelled(path))
        return [m for m in self.mounts if m.attached and m.point_id == (info.st_dev, info.st_ino)]

    def read_only(self, mount: _Mount) -> bool | None:
        """*mount*'s read-only flag, or ``None`` for the host's own mounts."""
        return None if mount is self.host else mount.read_only


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Namespace:
    """A :class:`_Namespace` that ``os.open``, ``fstatvfs``, ``statvfs`` and ``rmdir`` consult.

    The launcher reads its mount table and its descriptors' mount IDs from the model too.
    """
    ns = _Namespace()
    ns.mountinfo = tmp_path / "model-mountinfo"
    ns.publish()
    monkeypatch.setattr(program, "_MOUNTINFO", str(ns.mountinfo))
    monkeypatch.setattr(program, "_mount_id_of", ns.mount_id)
    real_open, real_close = os.open, os.close
    real_fstatvfs, real_statvfs, real_rmdir = os.fstatvfs, os.statvfs, os.rmdir

    def _open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        ns.fds[fd] = ns.resolve(path, dir_fd)
        return fd

    def _close(fd: int) -> None:
        ns.fds.pop(fd, None)
        real_close(fd)

    def _flags(real: Any, read_only: bool | None) -> Any:
        if read_only is None:
            return real
        flag = real.f_flag & ~os.ST_RDONLY
        return SimpleNamespace(f_flag=flag | (os.ST_RDONLY if read_only else 0))

    def _fstatvfs(fd: int) -> Any:
        real = real_fstatvfs(fd)
        return _flags(real, ns.read_only(ns.fds[fd][1])) if fd in ns.fds else real

    def _statvfs(path: Any) -> Any:
        return _flags(real_statvfs(path), ns.read_only(ns.resolve(path)[1]))

    def _rmdir(path: Any, *, dir_fd: int | None = None) -> None:
        if dir_fd is None and os.path.lexists(_spelled(path)) and ns.on(path):
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), _spelled(path))
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", _open)
    monkeypatch.setattr(os, "close", _close)
    monkeypatch.setattr(os, "fstatvfs", _fstatvfs)
    monkeypatch.setattr(os, "statvfs", _statvfs)
    monkeypatch.setattr(os, "rmdir", _rmdir)
    return ns


class _KernelMounts:
    """A libc mixin that keeps a :class:`_Namespace` for every mount and detach it makes.

    Over a renaming fake it also puts a bind's source entry back, as a link to the target the
    fake moved it to, since a real bind leaves that entry in place. With *stuck* the stand-in
    stage, detached through its bare descriptor, stays mounted.
    """

    ns: _Namespace
    stuck: bool = False

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        ns = self.ns
        if flags & _MS_REMOUNT:
            path, mount = ns.resolve(target)
            result = super().mount(source, target, fstype, flags, data)  # type: ignore[misc]
            if result == 0 and mount is not ns.host and mount.point == path:
                mount.read_only = bool(flags & _MS_RDONLY)
            return result
        if fstype == b"tmpfs":
            result = super().mount(source, target, fstype, flags, data)  # type: ignore[misc]
            if result == 0:
                ns.attach(target, False, tmpfs=object())
            return result
        if source is None or not flags & _MS_BIND:
            return super().mount(source, target, fstype, flags, data)  # type: ignore[misc]
        src, tgt = _resolved(_below_fd(source)), _resolved(_below_fd(target))
        moved = os.path.lexists(src) and not _at_or_below(src, tgt)
        # Both names are resolved before the fake renames anything, as the kernel resolves
        # them before it mounts.
        read_only = ns.read_only(ns.resolve(source)[1]) or False
        new = ns.attach(target, read_only, copy=source if flags & _MS_REC else None)
        result = super().mount(source, target, fstype, flags, data)  # type: ignore[misc]
        if result != 0:
            new.attached = False
        elif moved and not os.path.lexists(src):
            os.symlink(tgt, src)
        return result

    def umount2(self, target, flags):  # noqa: ANN001, ANN201
        spelled = _spelled(target)
        if self.stuck and _bare_fd(spelled):
            self.detached.append(spelled)  # type: ignore[attr-defined]
            ctypes.set_errno(errno.EBUSY)
            return -1
        if not self.ns.detach(target):
            ctypes.set_errno(errno.EINVAL)
            return -1
        return super().umount2(target, flags)  # type: ignore[misc]


class _KernelRecordingLibc(_KernelMounts, RecordingLibc):
    """Changes nothing on disk; the table alone answers."""


class _KernelStagingLibc(_KernelMounts, _StagingLibc):
    """``_StagingLibc``, which accepts the private stage, with the kernel's mount table."""


class _KernelHostLibc(_KernelMounts, _SealingLibc):
    """``_SealingLibc``, which refuses the private stage, with the kernel's mount table."""


def _libc(cls: type, ns: _Namespace, *, stuck: bool = False) -> Any:
    libc = cls()
    libc.ns = ns
    libc.stuck = stuck
    return libc


# --------------------------------------------------------------------------- #
# The model against the kernel: one sequence of mount calls, here and on a real kernel.
# --------------------------------------------------------------------------- #

#: Mounts a tmpfs stage on ``D/stage``, pins ``D`` (a recursive bind over itself) before or
#: after that, makes ``entry`` on the stage through a descriptor held on the stage's root, binds
#: a read-only mount over it through a descriptor held on the entry, and detaches the stage
#: through its descriptor, as the launcher does. Given ``libc``, ``D`` and ``pin_first``; leaves
#: what each path reads as in ``result``.
PRIMITIVES = r"""
import ctypes, errno, os
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_REC, MNT_DETACH = 32, 4096, 16384, 2
O_PATH = getattr(os, "O_PATH", 0o10000000)
def must(rc, what):
    if rc != 0:
        raise OSError(ctypes.get_errno(), what)
def by_fd(fd, rest=""):
    return ("/proc/self/fd/%d" % fd + ("/" + rest if rest else "")).encode()
def pin():
    fd = os.open(D, O_PATH | os.O_DIRECTORY)
    must(libc.mount(by_fd(fd), by_fd(fd), None, MS_BIND | MS_REC, None), "pin")
    os.close(fd)
def locked(path):
    bits = os.statvfs(path).f_flag
    return ((MS_NOSUID if bits & os.ST_NOSUID else 0) | (MS_NODEV if bits & os.ST_NODEV else 0)
            | (MS_NOEXEC if bits & os.ST_NOEXEC else 0))
def reads(path):
    try:
        return "ro" if os.statvfs(path).f_flag & os.ST_RDONLY else "rw"
    except OSError as exc:
        return errno.errorcode[exc.errno]
stage, sealed = os.path.join(D, "stage"), os.path.join(D, "sealed")
os.mkdir(stage)
os.mkdir(sealed)
if pin_first:
    pin()
must(libc.mount(sealed.encode(), sealed.encode(), None, MS_BIND, None), "bind sealed")
must(libc.mount(sealed.encode(), sealed.encode(), None,
                MS_REMOUNT | MS_BIND | MS_RDONLY | locked(sealed), None), "seal")
ro_fd = os.open(sealed, O_PATH | os.O_DIRECTORY)
held = os.open(stage, O_PATH | os.O_DIRECTORY)
must(libc.mount(b"tmpfs", by_fd(held), b"tmpfs", MS_NOSUID | MS_NODEV, b"mode=0700"), "stage")
os.close(held)
root_fd = os.open(stage, O_PATH | os.O_DIRECTORY)
if not pin_first:
    pin()
os.mkdir(by_fd(root_fd, "entry"))
entry_fd = os.open(by_fd(root_fd, "entry"), O_PATH | os.O_NOFOLLOW)
must(libc.mount(by_fd(ro_fd), by_fd(entry_fd), None, MS_BIND, None), "cover")
entry = os.path.join(stage, "entry")
result = {"entry before the detach": reads(entry)}
must(libc.umount2(by_fd(root_fd), MNT_DETACH), "detach")
for fd in (entry_fd, root_fd, ro_fd):
    os.close(fd)
result["entry after the detach"] = reads(entry)
try:
    os.rmdir(stage)
    result["stage directory"] = "removed"
except OSError as exc:
    result["stage directory"] = errno.errorcode[exc.errno]
"""

#: What a kernel answers to :data:`PRIMITIVES`, by ``pin_first``. Pinned first, the stage is
#: the only mount at its place: the cover reads read-only, and the detach takes the stage and
#: the cover away. Pinned after, the pin copied the stage, the cover and the detach land on the
#: original, and the copy, which the path reaches, stays writable and keeps the directory busy.
KERNEL_ANSWERS = {
    True: {
        "entry before the detach": "ro",
        "entry after the detach": "ENOENT",
        "stage directory": "removed",
    },
    False: {
        "entry before the detach": "rw",
        "entry after the detach": "rw",
        "stage directory": "EBUSY",
    },
}


@pytest.mark.parametrize("pin_first", [True, False], ids=["pinned first", "pinned after"])
def test_the_model_answers_the_mount_sequence_as_a_kernel_does(
    tmp_path: Path, kernel: _Namespace, pin_first: bool
) -> None:
    """Its real-kernel twin is ``test_this_kernel_answers_the_mount_sequence_as_the_model_does``."""
    libc = _libc(_KernelRecordingLibc, kernel)
    scope = {"libc": libc, "D": str(tmp_path), "pin_first": pin_first}
    exec(compile(PRIMITIVES, "primitives", "exec"), scope)  # noqa: S102 - the sequence itself
    assert scope["result"] == KERNEL_ANSWERS[pin_first]


# --------------------------------------------------------------------------- #
# The launcher's own order, judged by the model.
# --------------------------------------------------------------------------- #

#: The steps ``run_child`` takes after the masks, stubbed: they confine and exec this process.
_AFTER_THE_MASKS = (
    "scrub_env",
    "drop_privileges",
    "install_seccomp",
    "refuse_hardlinked_credentials",
    "enter_exec_cwd",
    "exec_agent",
)


@dataclass
class _Layout:
    """A scratch root with two windows, and ``safe``, the launcher's temp directory."""

    scratch: _Scratch
    safe: Path
    pins: list[str]


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Layout:
    """No stand-in root qualifies, so the stage goes to ``safe``, under ``tmp_path``, a seal parent.

    As in the real-sandbox tests: the launcher's temp directory is under ``tmp_path``, and the
    gateway pins ``tmp_path`` among the parents of the crew home's seals.
    """
    scratch = _Scratch(tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", None)
    for key in ("TMP", "TEMP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TMPDIR", str(safe))
    for name in _AFTER_THE_MASKS:
        monkeypatch.setattr(program, name, lambda *_args: None)
    return _Layout(scratch, safe, [str(tmp_path), str(tmp_path / "home" / ".kiro")])


def _run_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: _Layout,
    libc: Any,
    *,
    stage_mounts: bool,
    **plan: Any,
) -> tuple[program.Launch, str | None]:
    """``run_child`` over *layout*, with *plan*'s payload keys added: ``(run, its refusal or None)``."""
    if stage_mounts:
        # The fake tmpfs is the directory itself; a real one is a device of its own.
        monkeypatch.setattr(program, "_covers_device", lambda _fd, _dev: True)
    root = str(layout.scratch.root)
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=[root],
            private_dirs=layout.scratch.windows,
            seal_windowed_masks=[root],
            parent_pins=layout.pins,
            **plan,
        ),
        libc=libc,
        environ=os.environ,
    )
    run.tmpfs_src = None
    try:
        return run, refusal(program.run_child, run, ["/bin/agent"])
    finally:
        for path in libc.read_only:
            os.chmod(path, 0o755)


@pytest.mark.parametrize("pinned", [True, False], ids=["under a seal parent", "under no pin"])
def test_a_stage_that_detaches_takes_every_path_to_the_stand_in_with_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel: _Namespace,
    layout: _Layout,
    pinned: bool,
) -> None:
    """The round-13 case: the stage sits under ``tmp_path``, a seal parent, and detaches.

    Pinned before the stage, the pin copies nothing, so the detach through the stage's
    descriptor takes the stage, the window stages and the cover away together: the spawn
    runs, the stand-in's entry is gone, nothing is mounted on the stage's directory any more,
    and that directory is removed. Pinned after it, as ``25469be9f`` did, the copy keeps the
    entry writable and the last check refuses.
    """
    if not pinned:
        layout.pins.clear()
    libc = _libc(_KernelStagingLibc, kernel)
    run, message = _run_child(tmp_path, monkeypatch, layout, libc, stage_mounts=True)
    root = str(layout.scratch.root)
    assert message is None, message
    entry, how = run.stand_in_sources[root]
    (stage,) = libc.staged
    assert how == "closed" and os.path.dirname(entry) == stage
    assert not os.path.lexists(entry)
    assert os.listdir(layout.safe) == [], "the stage's host directory was left behind"
    assert not [m for m in kernel.mounts if m.attached and _at_or_below(m.point, stage)]
    # The sealed root is a read-only mount, and each window a writable one of its own.
    sealed = kernel.reach(root)
    assert sealed.point == root and sealed.read_only
    for window in layout.scratch.windows:
        assert kernel.reach(window).point == window and not kernel.reach(window).read_only


def test_a_stage_under_a_seal_parent_that_does_not_detach_keeps_its_entry_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel: _Namespace, layout: _Layout
) -> None:
    """The stage stays mounted, so the agent can reach the entry, and the cover is what is there."""
    libc = _libc(_KernelStagingLibc, kernel, stuck=True)
    run, message = _run_child(tmp_path, monkeypatch, layout, libc, stage_mounts=True)
    assert message is None, message
    entry, _how = run.stand_in_sources[str(layout.scratch.root)]
    assert os.path.lexists(entry), "the stage stayed mounted"
    covered = kernel.reach(entry)
    assert covered.point == entry and covered.read_only


def test_a_refused_stage_under_a_seal_parent_keeps_its_entry_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel: _Namespace, layout: _Layout
) -> None:
    """The stage is refused, so the stand-in is made in ``safe`` after the pins, and covered."""
    libc = _libc(_KernelHostLibc, kernel)
    run, message = _run_child(tmp_path, monkeypatch, layout, libc, stage_mounts=False)
    assert message is None, message
    entry, how = run.stand_in_sources[str(layout.scratch.root)]
    assert how == "closed" and os.path.dirname(entry) == str(layout.safe)
    covered = kernel.reach(entry)
    assert covered.point == entry and covered.read_only


@pytest.mark.parametrize("ask", [True, False], ids=["asked", "not asked"])
@pytest.mark.parametrize("refuse", [False, True], ids=["placed", "a stage refuses"])
def test_the_mount_report_is_written_only_when_a_test_asks_for_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    kernel: _Namespace,
    layout: _Layout,
    ask: bool,
    refuse: bool,
) -> None:
    """Once the mounts are built, or as a stage refuses, and only with the variable set."""
    if ask:
        monkeypatch.setenv("KIROCREW_SANDBOX_MOUNT_REPORT", "1")
    if refuse:
        monkeypatch.setattr(program, "confirm_sealed_roots", lambda _run: sys.exit("BLOCKED"))
    libc = _libc(_KernelStagingLibc, kernel)
    run, message = _run_child(tmp_path, monkeypatch, layout, libc, stage_mounts=True)
    assert message == ("BLOCKED" if refuse else None)
    err = capfd.readouterr().err
    if not ask:
        assert "sandbox mount report" not in err
        return
    root = str(layout.scratch.root)
    entry, _how = run.stand_in_sources[root]
    for path in (root, *layout.scratch.windows):
        assert "sandbox mount report -- %s: statvfs " % path in err, err
    assert "sandbox mount report -- %s: errno %d " % (entry, errno.ENOENT) in err, err
    assert "sandbox mount report -- mountinfo " in err, err


class _KernelLayerLibc(_KernelStagingLibc):
    """``_KernelStagingLibc`` whose read-only remounts set the model's flag and nothing else.

    ``_SealingLibc`` makes a read-only remount a mode change on the directory, one inode for
    every mount of it. The operator's layer remounts the copies of the stage and of the
    window stages it makes, while the launcher keeps creating through the writable originals.
    """

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if flags & _MS_REMOUNT:
            return 0
        return super().bound(source, target, fstype, flags)


@pytest.mark.parametrize(
    "spelling", ["two levels inside an exception", "a link inside an exception", "through a link"]
)
def test_a_seal_parent_inside_the_operator_layer_is_pinned_on_top_of_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    kernel: _Namespace,
    layout: _Layout,
    spelling: str,
) -> None:
    """A read-only ``drive``, its ``Documents`` writable, ``Documents/Work/Archive`` read-only.

    The gateway pins the parents of ``Archive`` this uid could rename, and ``Documents/Work``
    is one. Pinned before the layer, the drive's recursive bind copies that pin and remounts
    the copy read-only, and the exception re-opens its own mount only, so ``Work`` would stay
    read-only; a pinned link inside ``Documents`` is a mount the layer cannot reach to seal,
    so the whole exception would stay read-only. Pinned on top of the layer, each copies the
    write access of the exception it sits in. The pin of a parent spelled through a link
    lands where the link leads, inside the drive.
    """
    drive = tmp_path / "drive"
    docs = drive / "Users" / "Documents"
    work, archive = docs / "Work", docs / "Work" / "Archive"
    archive.mkdir(parents=True)
    pins = [str(drive), str(drive / "Users"), str(docs)]
    if spelling == "two levels inside an exception":
        pins.append(str(work))
    elif spelling == "a link inside an exception":
        (tmp_path / "elsewhere").mkdir()
        (docs / "link").symlink_to(tmp_path / "elsewhere")
        pins.append(str(docs / "link"))
    else:
        (tmp_path / "alias").symlink_to(drive)
        pins.append(str(tmp_path / "alias" / "Users" / "Documents" / "Work"))
    layout.pins.extend(pins)
    libc = _libc(_KernelLayerLibc, kernel)
    _run, message = _run_child(
        tmp_path,
        monkeypatch,
        layout,
        libc,
        stage_mounts=True,
        operator_readonly_dirs=[str(drive), str(archive)],
        operator_writable_dirs=[str(docs)],
    )
    assert message is None, message
    assert "left read-only" not in capfd.readouterr().err
    reads = {path.name: kernel.reach(str(path)).read_only for path in (drive, docs, work, archive)}
    assert reads == {"drive": True, "Documents": False, "Work": False, "Archive": True}
    for pin in pins:
        assert kernel.reach(_normal(pin)).point == _normal(pin), f"{pin} was not pinned"


@pytest.mark.parametrize("holder", ["home", "."], ids=["the data home", "the stage too"])
def test_an_operator_read_only_entry_over_the_scratch_root_keeps_its_seal_and_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel: _Namespace,
    layout: _Layout,
    holder: str,
) -> None:
    """``agent.sandbox_readonly_paths`` names a directory that holds the scratch root.

    The layer lands after the windows are staged, so each window stays a writable mount of
    its own, and the sealed stand-in is mounted on top of it. Where the entry holds the stage
    too, nothing else being usable, the layer copies the stage and the window stages read-only, and the seals' parents
    inside the entry are pinned after it, so they copy those read-only copies: the spawn
    runs, and no path to the stand-in is writable.
    """
    entry = os.path.normpath(tmp_path / holder)
    _temp_candidates(monkeypatch, [str(tmp_path / "no-tmp")])
    libc = _libc(_KernelLayerLibc, kernel)
    run, message = _run_child(
        tmp_path,
        monkeypatch,
        layout,
        libc,
        stage_mounts=True,
        operator_readonly_dirs=[entry],
    )
    assert message is None, message
    root = str(layout.scratch.root)
    sealed = kernel.reach(root)
    assert sealed.point == root and sealed.read_only
    assert kernel.reach(os.path.join(root, "runtime-stale")) is sealed
    for window in layout.scratch.windows:
        assert kernel.reach(window).point == window and not kernel.reach(window).read_only
    assert kernel.reach(entry).read_only
    stand_in, how = run.stand_in_sources[root]
    assert how == "closed"
    assert not os.path.lexists(stand_in) or kernel.reach(stand_in).read_only
    (stage,) = libc.staged
    writable = [m for m in kernel.mounts if m.attached and _at_or_below(m.point, stage)]
    assert [m.point for m in writable if not m.read_only] == []


def test_which_seal_parents_land_inside_the_layer_is_read_once(tmp_path: Path) -> None:
    """A parent spelled through a link retargeted between the two pin calls is pinned once.

    Which pins land inside the operator's layer is read at the first call, before any mount,
    so a link moved before the second call cannot leave a parent to neither call.
    """
    drive, elsewhere, alias = tmp_path / "drive", tmp_path / "elsewhere", tmp_path / "alias"
    for top in (drive, elsewhere):
        (top / "sub").mkdir(parents=True)
    alias.symlink_to(drive)
    libc = RecordingLibc()
    run = launch(
        tmp_path,
        payload(parent_pins=[str(alias / "sub")], operator_readonly_dirs=[str(drive)]),
        libc=libc,
    )
    program.pin_seal_parents(run)
    assert libc.calls == [], "a parent inside the layer is pinned on top of it"
    alias.unlink()
    alias.symlink_to(elsewhere)
    program.pin_seal_parents(run, True)
    assert [call.flags for call in libc.calls] == [_MS_BIND | _MS_REC]


def _temp_candidates(monkeypatch: pytest.MonkeyPatch, platform: list[str]) -> None:
    """``tempfile`` tries the temp variables, then *platform* in place of ``/tmp`` and the rest."""

    def _candidates() -> list[str]:
        named = [os.environ[k] for k in ("TMPDIR", "TEMP", "TMP") if os.environ.get(k)]
        return [*named, *platform]

    monkeypatch.setattr(tempfile, "_candidate_tempdir_list", _candidates)
    monkeypatch.setattr(program, "_TEMP_PLATFORM_DIRS", tuple(platform))


@pytest.mark.parametrize("case", ["TMPDIR inside an entry", "inside an exception"])
def test_the_stage_is_kept_out_of_the_operator_layer_when_another_place_is_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel: _Namespace,
    layout: _Layout,
    case: str,
) -> None:
    """No stand-in root qualifies, and ``agent.sandbox_readonly_paths`` holds a temp candidate.

    The layer binds its directory recursively after the stage is mounted, which copies the
    stage and the window stages on it read-only, write exceptions included, and that copy
    stays at the stage's host path once the stage is detached: a read-only view of every
    window's real tree there, a leaf masked inside a window included. So the stage goes to a
    candidate outside the layer, ``safe``.
    """
    drive = tmp_path / "drive"
    docs = drive / "Users" / "Documents"
    temp = (docs if case == "inside an exception" else drive) / "tmp"
    temp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setenv("TEMP", str(layout.safe))
    libc = _libc(_KernelLayerLibc, kernel)
    _run, message = _run_child(
        tmp_path,
        monkeypatch,
        layout,
        libc,
        stage_mounts=True,
        operator_readonly_dirs=[str(drive)],
        operator_writable_dirs=[str(docs)],
    )
    assert message is None, message
    (stage,) = libc.staged
    reachable = [m.point for m in kernel.mounts if m.attached and m.visible]
    assert [p for p in reachable if _at_or_below(p, stage)] == [], "a copy of the stage stays"
    assert not os.path.lexists(stage) or os.listdir(stage) == []
    assert os.path.dirname(stage) == str(layout.safe)


def test_a_default_inside_the_operator_layer_leaves_the_stage_to_the_own_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: _Layout
) -> None:
    """``scratch_env`` names the own window, and the only other candidate is inside the layer.

    The window is taken: the scratch root's mask covers its path, so nothing the layer copied
    of a stage there is reachable. The renaming fakes cannot move a stage inside the tree a
    mask moves, so the mounts are judged on a real kernel, by
    ``test_a_default_temp_dir_inside_an_operator_entry_leaves_the_stage_to_the_own_window``.
    """
    held = tmp_path / "system-tmp"
    held.mkdir()
    own = layout.scratch.windows[0]
    monkeypatch.setenv("TMPDIR", own)
    _temp_candidates(monkeypatch, [str(held)])
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=[str(layout.scratch.root)],
            private_dirs=layout.scratch.windows,
            seal_windowed_masks=[str(layout.scratch.root)],
            operator_readonly_dirs=[str(held)],
        ),
        environ=os.environ,
    )
    program.pick_stand_in_root(run)
    assert tempfile.gettempdir() == own
    assert os.environ["TMPDIR"] == own


def test_with_every_candidate_inside_the_operator_layer_the_stage_still_goes_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel: _Namespace, layout: _Layout
) -> None:
    """Nothing but a directory inside the layer is usable: the spawn runs with the stage there.

    Refusing would end a spawn that runs without this choice, so the stage is made where
    ``tempfile`` points, and the layer's read-only copy of it stays at its host path.
    """
    temp = tmp_path / "drive" / "tmp"
    temp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(temp))
    _temp_candidates(monkeypatch, [str(tmp_path / "no-tmp")])
    libc = _libc(_KernelLayerLibc, kernel)
    _run, message = _run_child(
        tmp_path,
        monkeypatch,
        layout,
        libc,
        stage_mounts=True,
        operator_readonly_dirs=[str(tmp_path / "drive")],
    )
    assert message is None, message
    (stage,) = libc.staged
    assert os.path.dirname(stage) == str(temp)


def test_an_operator_entry_inside_a_masked_tree_does_not_make_it_a_temp_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: _Layout
) -> None:
    """A temp directory in the scratch root stays refused when an operator entry also holds it."""
    other = layout.scratch.root / "runtime-other"
    monkeypatch.setenv("TMPDIR", str(other))
    _temp_candidates(monkeypatch, [str(tmp_path / "no-tmp")])
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=[str(layout.scratch.root)],
            seal_windowed_masks=[str(layout.scratch.root)],
            operator_readonly_dirs=[str(other)],
        ),
        environ=os.environ,
    )
    message = refusal(program.pick_stand_in_root, run)
    assert message is not None and "no temporary directory outside the masked" in message


#: Each read of a pinned descriptor's metadata the seal adds: ``(os function, the launcher
#: function that calls it, that function's caller or None for any, the path it must name)``.
_SEAL_READS = {
    "a staged window's identity": ("fstat", "stage_private_windows", None, "window"),
    "the stand-in reached as the entry is covered": (
        "fstat",
        "_reach_stand_in",
        "_close_stand_in_source",
        "root",
    ),
    "the sealed root reached at the last check": (
        "fstat",
        "_reach_stand_in",
        "confirm_sealed_roots",
        "root",
    ),
    "the stand-in's entry reached at the last check": (
        "fstat",
        "_reach_stand_in",
        "_confirm_no_writable_source",
        "entry",
    ),
    "the sealed root's mount flags": ("fstatvfs", "confirm_sealed_roots", None, "root"),
    "the stand-in entry's mount flags": (
        "fstatvfs",
        "_confirm_no_writable_source",
        None,
        "entry",
    ),
}


@pytest.mark.parametrize("read", [None, *_SEAL_READS], ids=["no read fails", *_SEAL_READS])
def test_a_failed_metadata_read_of_the_seal_refuses_by_path_and_closes_its_descriptor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel: _Namespace,
    layout: _Layout,
    read: str | None,
) -> None:
    """``run_child`` refuses with the BLOCKED line naming the path, closes once, execs nothing.

    The stage does not detach, so the last check reads the stand-in's own entry too and
    every read in :data:`_SEAL_READS` is reached. The read named fails with ``EIO`` on its
    first call there, and only there; with none failing the agent is exec'd.
    """
    execs: list[list[str]] = []
    monkeypatch.setattr(program, "exec_agent", lambda _run, argv: execs.append(argv))
    function, reader, caller, names = _SEAL_READS[read] if read else ("fstat", "", None, "")
    real_read, real_open, real_close = getattr(os, function), os.open, os.close
    failed: list[int] = []
    closes: list[int] = []

    def failing_read(fd: int) -> Any:
        frame = sys._getframe(1)
        if (
            not failed
            and frame.f_code.co_name == reader
            and (caller is None or frame.f_back.f_code.co_name == caller)
        ):
            failed.append(fd)
            raise OSError(errno.EIO, "I/O error")
        return real_read(fd)

    def tracking_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if fd in failed:
            failed.append(-1)  # the number is reused: later closes are another descriptor's
        return fd

    def tracking_close(fd: int) -> None:
        if failed and fd == failed[0] and -1 not in failed:
            closes.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, function, failing_read)
    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    libc = _libc(_KernelStagingLibc, kernel, stuck=True)
    run, message = _run_child(tmp_path, monkeypatch, layout, libc, stage_mounts=True)

    if read is None:
        assert message is None, message
        assert execs == [["/bin/agent"]]
        return
    root = str(layout.scratch.root)
    path = {
        "window": layout.scratch.windows[0],
        "root": root,
        "entry": run.stand_in_sources.get(root, ("",))[0],
    }[names]
    assert failed[:1], f"{read} was never read"
    assert message is not None and message.startswith("sandbox: BLOCKED -- "), message
    assert path and path in message and "I/O error" in message, message
    assert message.endswith("Lower agent.sandbox to run without this control deliberately.")
    assert closes == failed[:1], "the descriptor the failed read was taken on"
    assert execs == []
