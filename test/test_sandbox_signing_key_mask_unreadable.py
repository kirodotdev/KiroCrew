"""The Linux mask over ``token_signing.key`` refuses reads instead of answering empty.

The launcher hides a sensitive file by binding an empty tmpfs file over it. For most
leaves an empty read is harmless. For the signing key it is not: a data-home copy run
from an agent shell (``rsync``, ``cp -a``, ``tar``) reads the mask and writes a 0-byte
``token_signing.key`` on the destination, which the gateway then refuses to replace and
answers with an ephemeral secret on every boot. The mask source for that leaf is mode 0,
so the copy fails with ``Permission denied`` instead.

These tests drive the launcher program's own stages --
:func:`~kiro_crew.sandbox_launcher_program.enter_namespaces` and
:func:`~kiro_crew.sandbox_launcher_program.place_masks` -- with the shared stand-in
libc from ``test_sandbox_launcher_program``: a real bind needs a user namespace, which a
nested sandbox cannot create. The libc here also accepts the private tmpfs and records
each bind's source mode and bytes at mount time, since that inode is what the sandboxed
process reads through the bind. Each tier's unreadable set is the one its plan carries
(:func:`kiro_crew.sandbox._spawn_plan`). The key's mask is then sealed: its stand-in is
created mode 0 in a private tmpfs stage mounted in the launcher's namespace only, bound
through the launcher's own descriptor while it is non-dumpable, its bind is remounted
read-only, and the stage is detached, so no same-uid process keeps a writable path to
the inode.
"""

from __future__ import annotations

import ctypes
import errno
import os
import select
import stat
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import CoveringLibc, launch, payload

from kiro_crew import sandbox, sandbox_launcher_program, sandbox_plan

program = sandbox_launcher_program

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the namespace launcher is Linux-only, and its stand-ins are bound "
    "through /proc/self/fd, which macOS does not have",
)

_PR_SET_DUMPABLE = 4
_MS_REMOUNT = 32


class _SealingLibc(CoveringLibc):
    """A covering libc that also accepts the private tmpfs and snapshots each plain bind.

    Every snapshot is keyed by the path the bind's target named when it was made,
    because the stages bind onto a pinned ``/proc/self/fd/<n>`` path, not the name.
    """

    def __init__(self, *, refuse_tmpfs: bool = False) -> None:
        super().__init__()
        self.refuse_tmpfs = refuse_tmpfs
        #: ``(mode, bytes)`` of each plain bind's source; ``None`` bytes when unreadable.
        self.seen: dict[str, tuple[int, bytes | None]] = {}
        #: Whether the process was dumpable when each plain bind was made.
        self.dumpable_at_bind: dict[str, bool] = {}
        self.bind_source: dict[str, str] = {}
        self.real_source: dict[str, str] = {}
        self.dumpable = True
        self.tmpfs_mounts: list[tuple[bytes, int]] = []
        self.unmounts: list[tuple[object, int]] = []
        #: Every unshare, dumpability change and unmount, in the order they happened.
        self.events: list[tuple[object, ...]] = []
        #: A read end on the parent's "maps written" pipe, held open by the test.
        self.maps_pipe: int | None = None

    def unshare(self, flags):  # noqa: ANN001, ANN201
        self.events.append(("unshare", flags))
        return super().unshare(flags)

    def prctl(self, option, a2, a3, a4, a5):  # noqa: ANN001, ANN201
        if option == _PR_SET_DUMPABLE:
            self.dumpable = bool(a2)
            pending = None
            if self.maps_pipe is not None:
                pending = bool(select.select([self.maps_pipe], [], [], 0)[0])
            self.events.append(("dumpable", a2, pending))
        return super().prctl(option, a2, a3, a4, a5)

    def umount2(self, target, flags):  # noqa: ANN001, ANN201
        self.unmounts.append((target, flags))
        self.events.append(("umount2", target))
        return super().umount2(target, flags)

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if fstype is not None:
            self.tmpfs_mounts.append((fstype, flags))
            if self.refuse_tmpfs:
                ctypes.set_errno(errno.EPERM)
                return -1
            # The stage stays a plain directory; ``_no_fresh_mount_check`` stands in
            # for the device change a real tmpfs would show.
            return 0
        if source is not None and not flags & _MS_REMOUNT:
            spelling = os.fsdecode(source)
            named = self.calls[-1].target_path
            try:
                content: bytes | None = Path(spelling).read_bytes()
            except PermissionError:
                content = None
            self.seen[named] = (stat.S_IMODE(os.stat(spelling).st_mode), content)
            self.dumpable_at_bind[named] = self.dumpable
            self.bind_source[named] = spelling
            self.real_source[named] = os.path.realpath(spelling)
        return super().bound(source, target, fstype, flags)


@pytest.fixture(autouse=True)
def _pin_ssh_accept_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``_spawn_plan`` from spawning a real ``ssh -V``."""
    monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)


@pytest.fixture(autouse=True)
def _no_fresh_mount_check(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage freshness check asks whether a REAL tmpfs now covers the stage.

    The stand-in libc accepts the tmpfs mount without making one, so the stage would
    read as replaced and every sealed mask would refuse; here it reads as freshly
    mounted instead. The post-mount name check needs no such help: the covering libc
    moves each stand-in onto the name it masks, so that check runs for real.
    """
    _floor_monkeypatch.setattr(program, "_stage_is_fresh_mount", lambda _dfd, _parent: True)


def _unreadable_masks(level: str) -> list[str]:
    """The unreadable-mask leaves the *level* tier's namespace plan carries."""
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, level)
    return sandbox_plan.namespace_payload(plan)["unreadable_masks"]


def _enter(run: program.Launch, libc: _SealingLibc) -> None:
    """Run :func:`enter_namespaces` with the parent's maps already written."""
    c2p_r, c2p_w = os.pipe()
    p2c_r, p2c_w = os.pipe()
    os.write(p2c_w, b"x")
    libc.maps_pipe = os.dup(p2c_r)
    try:
        program.enter_namespaces(run, c2p_w, p2c_r)
    finally:
        os.close(c2p_r)
        os.close(p2c_w)
        os.close(libc.maps_pipe)
        libc.maps_pipe = None


def _hide(
    tmp_path: Path, level: str, *, prctl: bool = True, refuse_tmpfs: bool = False
) -> tuple[_SealingLibc, str, str, str]:
    """Enter the namespaces and place every mask over a fake home.

    Returns the libc, the key's and ``.netrc``'s resolved paths, and the stand-in root.
    """
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    crew.mkdir(parents=True)
    key = crew / "token_signing.key"
    key.write_bytes(os.urandom(32))
    netrc = home / ".netrc"
    netrc.write_text("machine example.com\n")
    ssh = home / ".ssh"
    ssh.mkdir()

    libc = _SealingLibc(refuse_tmpfs=refuse_tmpfs)
    if not prctl:
        libc.prctl = None  # type: ignore[assignment]
    run = launch(
        tmp_path,
        payload(
            sensitive_files=[str(key), str(netrc)],
            unreadable_masks=_unreadable_masks(level),
            ssh_dir=str(ssh),
            ssh_known_hosts=str(ssh / "known_hosts"),
            hide_ssh=0,
            sandbox_level=level,
        ),
        libc=libc,
        environ={"HOME": str(home)},
    )
    _enter(run, libc)
    program.place_masks(run)
    return libc, os.path.realpath(key), os.path.realpath(netrc), run.tmpfs_src


def _remounts_of(libc: _SealingLibc, path: str) -> list[int]:
    return [
        call.flags
        for call in libc.calls
        if call.flags & _MS_REMOUNT and os.path.realpath(os.fsdecode(call.target)) == path
    ]


@pytest.mark.parametrize("level", ["strict", "standard"])
def test_signing_key_mask_source_is_unreadable(tmp_path: Path, level: str) -> None:
    """The key's mask source carries no permission bits; other masks keep theirs."""
    libc, key, netrc, _src = _hide(tmp_path, level)

    assert libc.seen[key][0] == 0
    # The control: an ordinary hidden file still reads as empty, unchanged.
    assert libc.seen[netrc] == (0o600, b"")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a mode-0 file, so the refusal cannot be observed",
)
def test_a_copy_through_the_signing_key_mask_fails(tmp_path: Path) -> None:
    """Reading the mask raises, so a copy cannot carry zero bytes out as the key."""
    libc, key, _netrc, _src = _hide(tmp_path, "strict")

    assert libc.seen[key][1] is None


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

    remounts = _remounts_of(libc, key)
    assert len(remounts) == 1
    assert remounts[0] & 1 and remounts[0] & 4096  # MS_RDONLY | MS_BIND
    # One private tmpfs, nosuid/nodev/noexec, detached again afterwards.
    assert libc.tmpfs_mounts == [(b"tmpfs", 2 | 4 | 8)]
    assert [flags for _t, flags in libc.unmounts] == [2]  # MNT_DETACH
    # Bound through a descriptor onto a file created in that stage, never through
    # a name in the shared tmpfs root.
    assert libc.bind_source[key].startswith("/proc/self/fd/")
    real = Path(libc.real_source[key])
    assert real.name == "stand-in" and real.parent.parent == Path(os.path.realpath(src_dir))
    assert not libc.dumpable_at_bind[key]
    assert libc.dumpable, "dumpable must be restored once the masks are in place"
    # The control: ordinary masks keep their named source, for the janitor.
    assert Path(libc.bind_source[netrc]).parent == Path(src_dir)


def test_without_a_private_stage_the_key_mask_reads_empty(tmp_path: Path) -> None:
    """No prctl means no safe private stage, so the key keeps the readable empty mask."""
    libc, key, _netrc, src_dir = _hide(tmp_path, "strict", prctl=False)

    assert libc.seen[key] == (0o600, b"")
    assert Path(libc.bind_source[key]).parent == Path(src_dir)
    assert not _remounts_of(libc, key)


def test_a_refused_private_tmpfs_warns_on_its_own_line_and_falls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning ends in a real newline, so a later refusal line stays readable."""
    libc, key, netrc, src_dir = _hide(tmp_path, "strict", refuse_tmpfs=True)

    assert libc.seen[key] == (0o600, b"")
    assert libc.dumpable
    # Both masks are ordinary named stand-ins in the stand-in root, and the refused
    # stage left nothing behind there: the covering libc moved each stand-in onto the
    # name it masks, so the root is empty.
    sources = {Path(libc.bind_source[key]), Path(libc.bind_source[netrc])}
    assert len(sources) == 2 and {source.parent for source in sources} == {Path(src_dir)}
    assert list(Path(src_dir).iterdir()) == []
    err = capsys.readouterr().err
    assert err.startswith("sandbox: WARNING -- could not mount a private tmpfs")
    assert err.endswith("instead.\n") and "\\n" not in err


@pytest.mark.parametrize("level", ["strict", "cc", "standard"])
def test_the_launcher_is_non_dumpable_before_its_mount_namespace_exists(
    tmp_path: Path, level: str
) -> None:
    """A /proc/<pid>/root opened after unshare(CLONE_NEWNS) would reach the stage.

    So dumpability is cleared after the parent has written the id maps and
    before the mount namespace is created, and restored only after the
    sensitive-file masks, once the stage is detached.
    """
    libc, _key, _netrc, _src = _hide(tmp_path, level)

    events = libc.events
    clears = [i for i, event in enumerate(events) if event[:2] == ("dumpable", 0)]
    restores = [i for i, event in enumerate(events) if event[:2] == ("dumpable", 1)]
    assert len(clears) == 1 and len(restores) == 1
    # The parent's "maps written" byte was already consumed when dumpability went.
    assert events[clears[0]][2] is False
    newns = events.index(("unshare", program._CLONE_NEWNS))
    retire = next(i for i, event in enumerate(events) if event[0] == "umount2")
    assert events.index(("unshare", program._CLONE_NEWUSER)) < clears[0]
    assert clears[0] < newns < retire < restores[0]


def test_unreadable_set_names_only_the_signing_key() -> None:
    """Widening the set changes what sandboxed readers see, so it is pinned here."""
    assert getattr(sandbox, "_CREW_UNREADABLE_MASK_LEAVES", None) == frozenset(
        {"token_signing.key"}
    )
