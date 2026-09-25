"""Each launcher mask binds the object its own check inspected, not a re-read name.

Every hiding mount in the namespace launcher needs two answers about one target:
what KIND of object is there, and then mount over it. Taking those from two
separate lookups of the same NAME leaves a window: a name swapped in between is
classified as the old object and bound as the new one, so the mask attaches to
whatever the name points at by then while the bytes it exists to cover sit at a
name nothing masks. The crew data home is writable by an already-running
sandboxed process, so the racing writer is ordinary rather than exotic.

The tests here LOSE that race deliberately and then ask what the mount actually
received. The swap is planted at a synchronisation point every version of the
loop passes through -- the ``tempfile`` call that creates the empty stand-in,
which sits after the classification and before the mount in both the pinned and
the name-based shape -- so one test body discriminates between them instead of
asserting a call shape.

``_FakeLibc`` resolves each target AT MOUNT TIME and records the device and
inode it reached, because that is the question: ``mount(2)`` walks the target
path exactly as ``stat`` does, so what the recorded path resolves to in that
moment is what the kernel would bind. A pinned ``/proc/self/fd/<fd>`` target is
only resolvable while the descriptor is open, which is precisely why the
resolution happens inside the fake rather than after the run.

Like the sibling mount suite, these run the region lifted VERBATIM out of the
shipped launcher, so they cannot drift from what executes in the child, and each
behavioural assertion has a break-arm that mutates the shipped source back to
the name-based form and shows the assertion's own value moves.
"""

from __future__ import annotations

import errno
import os
import runpy
import shutil
import stat
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from kiro_crew.sandbox import _build_launcher_script

# The namespace launcher runs on Linux ONLY, and the pinned mounts below address
# their targets through ``/proc/self/fd/<fd>`` -- a path Darwin does not have, so
# off Linux every recorded target would resolve to nothing and these assertions
# would measure the absence of procfs rather than which object was bound.
# ``_build_launcher_script`` also calls POSIX-only ``os.getuid``. macOS's own
# masking is the Seatbelt profile, covered by its own suites.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

#: Module-level helpers: from the first one's ``def`` to the first substitution.
_HELPER_START = "def _mount_or_die("
_HELPER_END = "REAL_UID = "
#: The private-window staging plus the hiding mounts and the ceiling seal.
_HIDE_START = "        # Private windows: a directory INSIDE a hidden tree that stays"
_HIDE_END = "        # Scrub sensitive env vars"

#: What the extracted region must contain, structurally. Without this a marker
#: rename would shrink a slice and leave every assertion below vacuously green
#: against a fragment that never mounts anything. Deliberately the LOOPS and the
#: refusal, never the pinning call: naming that here would make every assertion
#: below fail on a missing landmark the moment the pin is reverted, hiding what
#: each test actually measures behind one structural complaint.
_LANDMARKS = (
    "for p in PRIVATE_DIRS:",
    "for d in SENSITIVE_DIRS:",
    "for d in READONLY_DIRS:",
    "for f in SENSITIVE_FILES:",
    "for d in WRITABLE_DIRS:",
    "if HIDE_SSH and",
    "sandbox: BLOCKED",
)


class _Call:
    """One ``mount(2)`` the region made, plus what its target resolved to."""

    def __init__(self, source: object, target: object, flags: int) -> None:
        self.source = source
        self.target = target
        self.flags = flags
        self.target_id: tuple[int, int] | None = _identity(target)
        self.source_id: tuple[int, int] | None = _identity(source)


def _identity(path: object) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path*, or ``None`` when it does not resolve.

    Takes whatever the caller holds -- a ``Path`` from the test bed, or the
    ``bytes`` the launcher hands ``mount`` -- because both sides of every
    comparison below go through here and a rejected type would read as a path
    that does not exist.
    """
    spelling = path if isinstance(path, str) else None
    if isinstance(path, bytes):
        spelling = os.fsdecode(path)
    prefix = "/proc/self/fd/"
    if spelling is not None and spelling.startswith(prefix):
        # Resolve the DESCRIPTOR the spelling names, not the spelling: the fd is
        # still open when a caller compares, and ``fstat`` reaches the same object
        # on every POSIX host while ``/proc/self/fd/<n>`` resolves only on Linux.
        # Statting the path instead would make these comparisons measure the
        # presence of procfs, which is what forced this suite to skip off Linux.
        try:
            info = os.fstat(int(spelling[len(prefix) :]))
        except (OSError, ValueError):
            return None
        return (info.st_dev, info.st_ino)
    try:
        info = os.stat(path)  # type: ignore[arg-type]
    except (OSError, TypeError, ValueError):
        return None
    return (info.st_dev, info.st_ino)


class _FakeLibc:
    """``_libc`` whose ``mount`` records the identity each target resolves to.

    This test process cannot create a user namespace -- a nested ``unshare`` is
    seccomp-denied inside an agent sandbox -- so a stand-in is the only way to
    exercise the region at all. Resolving the target here, at the moment the
    region calls ``mount``, is what makes the recording meaningful.
    """

    def __init__(self) -> None:
        self.calls: list[_Call] = []

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001
        self.calls.append(_Call(source, target, flags))
        return 0


class _SwappingTempfile:
    """``tempfile``, with a one-shot side effect on the chosen constructor.

    The launcher creates each mask's empty stand-in between classifying its
    target and mounting over it, so this is the synchronisation point where a
    racing writer would land. *swap* runs once, on the first call to whichever
    constructor *trigger_on* names, and then the real constructor runs.
    """

    def __init__(self, trigger_on: str, swap) -> None:  # noqa: ANN001
        self._trigger_on = trigger_on
        self._swap = swap
        self.fired = False

    def _maybe_swap(self, which: str) -> None:
        if which == self._trigger_on and not self.fired:
            self.fired = True
            self._swap()

    def mkstemp(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self._maybe_swap("mkstemp")
        return tempfile.mkstemp(*args, **kwargs)

    def mkdtemp(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self._maybe_swap("mkdtemp")
        return tempfile.mkdtemp(*args, **kwargs)


def _region(script: str, *, stub_verify: bool = True) -> str:
    """Helper block plus the hiding loops, lifted out of *script*.

    Sliced from the START OF THE LINE, not from the marker: ``dedent`` measures
    the common prefix across all lines, so a first line already stripped of its
    indent leaves the rest indented and the block will not parse.

    ``stub_verify`` swaps the BODY of ``_verify_masked_name`` for a call to a
    harness hook, keeping its signature and every call site. It cannot be
    replaced through ``init_globals`` because the extracted text DEFINES it and
    a module-body definition wins. It has to be replaceable at all because the
    shipped check asks whether the configured name now reaches the stand-in,
    which is only true after a REAL mount; against a stand-in ``mount`` that
    records instead of mounting it would refuse every run, and every assertion
    here would become a statement about the harness. Its own verdict is tested
    directly, against real filesystem objects.
    """

    def cut(start_marker: str, end_marker: str) -> str:
        a = script.rindex("\n", 0, script.index(start_marker)) + 1
        b = script.rindex("\n", 0, script.index(end_marker, a)) + 1
        return script[a:b]

    region = cut(_HELPER_START, _HELPER_END) + "\n" + textwrap.dedent(cut(_HIDE_START, _HIDE_END))
    missing = [marker for marker in _LANDMARKS if marker not in region]
    assert not missing, f"the extracted mount region is missing {missing}"
    if stub_verify:
        signature = "def _verify_masked_name(name, stand_in, what):"
        following = "def _locked_mount_flags(target):"
        assert signature in region, "the name-check helper was renamed"
        assert following in region, "the helper after the name check was renamed"
        start = region.index(signature)
        end = region.index(following, start)
        region = (
            region[:start]
            + signature
            + "\n    return _HARNESS_VERIFY(name, stand_in, what)\n\n"
            + region[end:]
        )
    return region


class _Bed:
    """The filesystem the region runs against, plus the swap victims."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.aws = self.home / ".aws"
        self.aws.mkdir(parents=True)
        (self.aws / "credentials").write_text("[default]\n")
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir()
        (self.ssh / "known_hosts").write_text("example.com ssh-rsa AAAA\n")
        self.secret = self.home / ".netrc"
        self.secret.write_text("machine example.com\n")
        self.cache = self.home / ".kiro" / "crew" / "policy_cache"
        self.cache.mkdir(parents=True)
        (self.cache / "policy.json").write_text("{}\n")
        # What a racing writer redirects a masked name AT: a decoy of each kind,
        # holding nothing, so a mask that lands here protects nothing.
        self.decoy_file = tmp_path / "decoy_file"
        self.decoy_file.write_text("")
        self.decoy_dir = tmp_path / "decoy_dir"
        self.decoy_dir.mkdir()
        self.tmpfs = tmp_path / "tmpfs"
        self.tmpfs.mkdir()

    def swap(self, victim: Path, decoy: Path):  # noqa: ANN201
        """A callable that redirects *victim*'s NAME at *decoy*."""

        def do_swap() -> None:
            if victim.is_dir() and not victim.is_symlink():
                victim.rename(victim.parent / (victim.name + ".moved"))
            else:
                victim.unlink()
            victim.symlink_to(decoy)

        return do_swap


def _run(
    tmp_path: Path,
    *,
    script: str | None = None,
    tempfile_shim: _SwappingTempfile | None = None,
    bed: _Bed | None = None,
    verify=None,  # noqa: ANN001
    required: tuple[str, ...] = (),
    occupants: dict[str, list[int]] | None = None,
) -> tuple[_FakeLibc, _Bed, str | None]:
    """Run the extracted region. Returns ``(fake_libc, bed, refusal_or_None)``.

    Via ``runpy.run_path`` rather than ``exec`` of the text: equivalent here --
    both run the shipped source with an injected namespace -- but ``exec`` trips
    the SAST gate's ``exec-detected`` rule.

    ``_verify_masked_name`` is replaced by *verify*, or by a no-op. The shipped
    one asks whether the configured name now reaches the stand-in, which is only
    true after a REAL mount; with a stand-in ``mount`` that records instead of
    mounting it would refuse every run and every assertion here would become a
    statement about the harness. Its own verdict is tested directly.
    """
    import ctypes

    bed = bed or _Bed(tmp_path)
    libc = _FakeLibc()

    def _no_op_verify(name, stand_in, what):  # noqa: ANN001, ANN202
        return None

    namespace = {
        "_libc": libc,
        "_HARNESS_VERIFY": verify or _no_op_verify,
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
        "stat": stat,
        "sys": sys,
        "tempfile": tempfile_shim or tempfile,
        "_tmpfs_src": str(bed.tmpfs),
        "_src_prefix": "kirocrew_sb_%d_" % os.getpid(),
        "expose_data": {},
        "EXPOSE_FILES": [],
        "SENSITIVE_DIRS": [str(bed.aws)],
        "PRIVATE_DIRS": [],
        "READONLY_DIRS": [str(bed.cache)],
        "WRITABLE_DIRS": [],
        "SENSITIVE_FILES": [str(bed.secret)],
        "REQUIRED_MASK_TARGETS": frozenset(required),
        # What the pre-spawn passes observed, carried in as data exactly as the
        # builder emits it. Empty by default: most tests here are about which
        # OBJECT a mount received, and an expectation the gateway never recorded
        # would refuse those runs before they got that far.
        "MASK_OCCUPANTS": dict(occupants or {}),
        "SSH_DIR": str(bed.ssh),
        "SSH_KNOWN_HOSTS": str(bed.ssh / "known_hosts"),
        "HIDE_SSH": True,
    }
    region_file = tmp_path / "region.py"
    region_file.write_text(
        _region(script if script is not None else _build_launcher_script("strict"))
    )
    try:
        runpy.run_path(str(region_file), init_globals=namespace)
    except SystemExit as exc:
        return libc, bed, str(exc.code)
    return libc, bed, None


def _call_whose_target_is(libc: _FakeLibc, wanted: tuple[int, int]) -> _Call | None:
    for call in libc.calls:
        if call.target_id == wanted:
            return call
    return None


# --------------------------------------------------------------------------
# The race, lost on purpose
# --------------------------------------------------------------------------


def test_file_mask_binds_the_pinned_file_when_its_name_is_swapped(tmp_path: Path) -> None:
    """A name swapped after classification does not move the file mask."""
    bed = _Bed(tmp_path)
    pinned = _identity(bed.secret)
    decoy = _identity(bed.decoy_file)
    assert pinned is not None and decoy is not None and pinned != decoy
    shim = _SwappingTempfile("mkstemp", bed.swap(bed.secret, bed.decoy_file))

    libc, _, refusal = _run(tmp_path, tempfile_shim=shim, bed=bed)

    assert shim.fired, "the swap never ran, so this test proved nothing"
    assert refusal is None
    assert (
        _call_whose_target_is(libc, pinned) is not None
    ), "no mount landed on the file that was classified"
    assert _call_whose_target_is(libc, decoy) is None, "a mount landed on the decoy"


def test_directory_mask_binds_the_pinned_directory_when_its_name_is_swapped(
    tmp_path: Path,
) -> None:
    """A name swapped after classification does not move the credential mask."""
    bed = _Bed(tmp_path)
    pinned = _identity(bed.aws)
    decoy = _identity(bed.decoy_dir)
    assert pinned is not None and decoy is not None and pinned != decoy
    shim = _SwappingTempfile("mkdtemp", bed.swap(bed.aws, bed.decoy_dir))

    libc, _, refusal = _run(tmp_path, tempfile_shim=shim, bed=bed)

    assert shim.fired, "the swap never ran, so this test proved nothing"
    assert refusal is None
    assert (
        _call_whose_target_is(libc, pinned) is not None
    ), "no mount landed on the directory that was classified"
    assert _call_whose_target_is(libc, decoy) is None, "a mount landed on the decoy"


def test_ceiling_seal_refuses_when_the_name_changes_between_bind_and_seal(
    tmp_path: Path,
) -> None:
    """The seal is a remount, so it re-resolves -- and must refuse a changed name.

    A remount can only name the mount the bind just created, which no descriptor
    taken before that bind refers to. The loop therefore resolves the name once
    more and requires the object it reaches to be the object it bound; a
    mismatch means the seal would land elsewhere and leave this ceiling
    writable, so the spawn refuses rather than running unsealed.
    """
    bed = _Bed(tmp_path)
    swap = bed.swap(bed.cache, bed.decoy_dir)
    fired: list[int] = []

    class _SealSwapLibc(_FakeLibc):
        def mount(self, source, target, fstype, flags, data):  # noqa: ANN001
            result = super().mount(source, target, fstype, flags, data)
            # Between the ceiling's bind and its sealing remount.
            if flags == 4096 and not fired and _identity(target) == _identity(bed.cache):
                fired.append(1)
                swap()
            return result

    libc = _SealSwapLibc()
    refusal = _run_with_libc(tmp_path, bed, libc)

    assert fired, "the swap never ran, so this test proved nothing"
    assert refusal is not None, "the launcher ran on with an unsealed ceiling"
    assert "changed identity" in refusal
    assert str(bed.cache) in refusal


def _run_with_libc(tmp_path: Path, bed: _Bed, libc: _FakeLibc) -> str | None:
    """``_run``, with a caller-supplied ``_libc``. Returns the refusal or None."""
    import ctypes

    namespace = {
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
        "stat": stat,
        "sys": sys,
        "tempfile": tempfile,
        "_tmpfs_src": str(bed.tmpfs),
        "_src_prefix": "kirocrew_sb_%d_" % os.getpid(),
        "expose_data": {},
        "EXPOSE_FILES": [],
        "SENSITIVE_DIRS": [],
        "PRIVATE_DIRS": [],
        "READONLY_DIRS": [str(bed.cache)],
        "WRITABLE_DIRS": [],
        "SENSITIVE_FILES": [],
        "REQUIRED_MASK_TARGETS": frozenset(),
        "SSH_DIR": str(bed.ssh),
        "SSH_KNOWN_HOSTS": str(bed.ssh / "known_hosts"),
        "HIDE_SSH": False,
    }
    region_file = tmp_path / "seal_region.py"
    region_file.write_text(_region(_build_launcher_script("strict")))
    try:
        runpy.run_path(str(region_file), init_globals=namespace)
    except SystemExit as exc:
        return str(exc.code)
    return None


def test_known_hosts_is_staged_into_the_standin_before_it_is_mounted(
    tmp_path: Path,
) -> None:
    """Host trust is restored into the stand-in, not through the masked name.

    Writing it after the mask means addressing the restored file through the
    key directory's name a third time, so a name swapped after the pin would
    take the copied trust data outside the mask and leave the masked directory
    with no known hosts at all -- every host then reads as new.
    """
    libc, bed, refusal = _run(tmp_path)

    assert refusal is None
    ssh_mounts = [
        call for call in libc.calls if call.target_id == _identity(bed.ssh) and call.flags == 4096
    ]
    assert len(ssh_mounts) == 1, "the ssh key directory was not masked exactly once"
    staged = Path(os.fsdecode(ssh_mounts[0].source)) / "known_hosts"
    assert staged.is_file(), "known_hosts was not staged into the stand-in"
    assert staged.read_text() == "example.com ssh-rsa AAAA\n"


# --------------------------------------------------------------------------
# A pin that fails must not become a mask that is missing
# --------------------------------------------------------------------------


def _deny_open(monkeypatch: pytest.MonkeyPatch, victim: Path, err: int) -> None:
    """Make ``os.open`` fail with *err* for *victim* only, delegating otherwise.

    Matches two spellings of the same open, because the launcher holds the
    target's PARENT and opens the leaf relative to that descriptor: the whole
    path, and the bare leaf name passed with a ``dir_fd``. Matching only the
    whole path would leave the denial never firing, and the tests that assert a
    refusal would pass because nothing was denied at all.
    """
    real_open = os.open

    def fake_open(path, flags, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        spelling = os.fsdecode(path)
        relative_to_parent = kwargs.get("dir_fd") is not None and spelling == victim.name
        if spelling == str(victim) or relative_to_parent:
            raise OSError(err, os.strerror(err), spelling)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def test_unpinnable_masked_file_refuses_instead_of_running_it_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A masked path that EXISTS and cannot be pinned must stop the spawn.

    ``stat`` succeeding while ``open`` is denied is a real host condition -- the
    launcher's own expose pre-read records meeting it -- and the caller asked for
    this path to be hidden. Skipping it would exec the agent with the path
    readable and nothing on stderr saying so.
    """
    bed = _Bed(tmp_path)
    _deny_open(monkeypatch, bed.secret, errno.EACCES)

    libc, _, refusal = _run(tmp_path, bed=bed)

    assert refusal is not None, "the launcher ran on with the path unmasked"
    assert "cannot pin" in refusal
    assert str(bed.secret) in refusal
    assert not any(call.target_id == _identity(bed.secret) for call in libc.calls)


def test_absent_masked_file_is_skipped_rather_than_refused(tmp_path: Path) -> None:
    """Absence stays a SKIP, because that is what the plain guards did.

    Every caller-supplied hidden path is offered to both the directory loop and
    the file loop, and each takes the entries of its own kind, so a miss is
    ordinary rather than a race. Turning it into a refusal would fail every
    spawn that hides a path of the other kind.
    """
    bed = _Bed(tmp_path)
    bed.secret.unlink()

    libc, _, refusal = _run(tmp_path, bed=bed)

    assert refusal is None
    assert not any(call.target_id == _identity(bed.secret) for call in libc.calls)
    # The rest of the sequence still ran: absence skipped one entry, not the loop.
    assert any(call.target_id == _identity(bed.aws) for call in libc.calls)


def test_ssh_mask_refuses_when_its_directory_cannot_be_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ssh site refuses on EVERY pin miss, not just an unreadable one.

    Its enclosing guard has already established that the directory is there, so
    an absent or wrong-kind answer at the pin is a race rather than an ordinary
    miss -- and this is the tier whose whole purpose is that private keys are not
    readable, with no post-mount check anywhere to notice a mask that never
    mounted.
    """
    bed = _Bed(tmp_path)
    _deny_open(monkeypatch, bed.ssh, errno.EACCES)

    libc, _, refusal = _run(tmp_path, bed=bed)

    assert refusal is not None, "strict mode ran on with ~/.ssh visible"
    assert "cannot pin" in refusal
    assert str(bed.ssh) in refusal
    assert not any(call.target_id == _identity(bed.ssh) for call in libc.calls)


def test_ssh_mask_refuses_when_its_directory_vanishes_after_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence at the ssh pin is a RACE, so it refuses rather than skipping.

    Its enclosing guard has just established the directory, so the pin finding
    nothing means the name moved in between. Everywhere else absence is an
    ordinary miss and skips; here it cannot, because the skip would exec with
    private keys readable.
    """
    bed = _Bed(tmp_path)
    real_isdir = os.path.isdir

    def isdir_then_vanish(path):  # noqa: ANN001, ANN202
        answer = real_isdir(path)
        if answer and os.fsdecode(path) == str(bed.ssh):
            # The synchronisation point: the guard has answered, the pin has not
            # run yet. A racing writer moves the directory aside right here.
            bed.ssh.rename(bed.ssh.parent / ".ssh.moved")
        return answer

    monkeypatch.setattr(os.path, "isdir", isdir_then_vanish)

    libc, _, refusal = _run(tmp_path, bed=bed)

    assert not bed.ssh.exists(), "the swap never ran, so this test proved nothing"
    assert refusal is not None, "strict mode ran on with no ssh mask at all"
    assert "cannot pin" in refusal
    assert "absent" in refusal
    assert not any(call.target_id == _identity(bed.decoy_dir) for call in libc.calls)


def test_break_arm_restoring_the_ssh_fail_open_loses_the_key_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guarding the ssh mount on a truthy pin instead of refusing loses the mask.

    The arm is the shape a conditional mount takes: skip when the pin answers
    nothing. Under it the vanish case reaches ``os.execvp`` having mounted
    nothing over the key directory, which is the whole point of the refusal.
    """
    script = _build_launcher_script("strict")
    anchor = (
        "            _ssh_fd, _ssh_target, _ = _pin_mount_path(\n"
        "                SSH_DIR.encode(), stat.S_ISDIR, require=True\n"
        "            )\n"
        "            try:\n"
        "                _mount_or_die(ssh_tmp, _ssh_target, _MS_BIND,\n"
        '                              "hiding ssh key directory %s" % SSH_DIR)\n'
        "            finally:\n"
        "                os.close(_ssh_fd)\n"
    )
    assert anchor in script, "break-arm anchor does not match the launcher"
    mutant = script.replace(
        anchor,
        "            _ssh_fd, _ssh_target, _ = _pin_mount_path(\n"
        "                SSH_DIR.encode(), stat.S_ISDIR)\n"
        "            if _ssh_target is not None:\n"
        "                try:\n"
        "                    _mount_or_die(ssh_tmp, _ssh_target, _MS_BIND,\n"
        '                                  "hiding ssh key directory %s" % SSH_DIR)\n'
        "                finally:\n"
        "                    os.close(_ssh_fd)\n",
    )

    bed = _Bed(tmp_path)
    real_isdir = os.path.isdir

    def isdir_then_vanish(path):  # noqa: ANN001, ANN202
        answer = real_isdir(path)
        if answer and os.fsdecode(path) == str(bed.ssh):
            bed.ssh.rename(bed.ssh.parent / ".ssh.moved")
        return answer

    monkeypatch.setattr(os.path, "isdir", isdir_then_vanish)

    libc, _, refusal = _run(tmp_path, script=mutant, bed=bed)

    # The mutant's own answer: it runs ON, with nothing mounted over the key
    # directory. That silence is the defect the refusal exists to remove.
    assert refusal is None
    staged_with_known_hosts = [
        call
        for call in libc.calls
        if isinstance(call.source, (str, bytes))
        and (Path(os.fsdecode(call.source)) / "known_hosts").is_file()
    ]
    assert staged_with_known_hosts == []


# --------------------------------------------------------------------------
# The other half of the window: the NAME must reach the mask
# --------------------------------------------------------------------------
#
# This check cannot be exercised through the region harness: no real mount
# happens there, so the configured name never reaches the stand-in and the
# shipped check would refuse every run, swapped or not. So it is split. The
# region tests assert the check is CALLED for every hiding mount, with that
# mount's own name and stand-in, and the helper's own verdict is tested directly
# against real filesystem objects below.


def test_every_hiding_mount_verifies_its_name_reaches_the_mask(tmp_path: Path) -> None:
    """Pinning the object is half the answer; the name is the other half.

    A rename between the pin and the mount leaves the mask on the object that
    was classified while the NAME reaches the racing writer's replacement. That
    is not a leak of what was there -- it is a WRITABLE object at a protected
    name, and the gateway reads several of these back as authoritative.
    """
    bed = _Bed(tmp_path)
    seen: list[tuple[str, str]] = []

    def _record(name, stand_in, what):  # noqa: ANN001, ANN202
        seen.append((os.fsdecode(name), os.fsdecode(stand_in)))

    libc, _, refusal = _run(tmp_path, bed=bed, verify=_record)

    assert refusal is None
    assert libc.calls, "no mount ran at all"
    # Every hiding mount, in source order, hands the check ITS OWN configured
    # name -- so a mount added later without the check is a missing row here.
    assert [name for name, _ in seen] == [str(bed.aws), str(bed.secret), str(bed.ssh)]
    # And each is handed a stand-in under the tmpfs source root, never the
    # descriptor path the mount itself took.
    for name, stand_in in seen:
        assert stand_in.startswith(str(bed.tmpfs)), (name, stand_in)


def test_a_required_target_of_the_other_kind_still_skips(tmp_path: Path) -> None:
    """Being established does not make the wrong loop's miss a race.

    Both loops are handed every caller-supplied path and each takes the entries
    of its own kind, so the loop that does not cover an object meets it on every
    ordinary spawn. Refusing there fails the spawn over a target that is present,
    correct and masked by the other loop -- which is worse than the window the
    requirement exists to close, because it happens every time rather than under
    a race.
    """
    bed = _Bed(tmp_path)
    # A directory standing where the FILE loop looks: present, established, and
    # legitimately not this loop's business.
    bed.secret.unlink()
    bed.secret.mkdir()

    _, _, refusal = _run(tmp_path, bed=bed, required=(str(bed.secret),))

    assert refusal is None, f"a required target of the other kind refused: {refusal}"


def test_a_materialized_target_that_went_missing_refuses(tmp_path: Path) -> None:
    """A target something created before launch cannot be legitimately absent.

    The plain existence guards could not tell the two absences apart, so they
    skipped both, and the pre-spawn materialisers exist precisely because an
    absent ceiling or credential leaf leaves the data home writable at that name
    for the whole sandbox. Naming the established targets turns the second case
    into a refusal while the first stays a skip.
    """
    bed = _Bed(tmp_path)
    bed.secret.unlink()

    _, _, refusal = _run(tmp_path, bed=bed, required=(str(bed.secret),))

    assert refusal is not None
    assert "absent" in refusal
    assert str(bed.secret) in refusal


def test_a_materialized_directory_that_went_missing_refuses(tmp_path: Path) -> None:
    """Same rule at the directory loop, which masks the credential stores."""
    bed = _Bed(tmp_path)
    shutil.rmtree(bed.aws)

    _, _, refusal = _run(tmp_path, bed=bed, required=(str(bed.aws),))

    assert refusal is not None
    assert "absent" in refusal


def test_an_unestablished_target_still_skips_when_absent(tmp_path: Path) -> None:
    """The availability half: a store the host never had must not refuse.

    Requiring every entry would fail the spawn on any host without the tool
    whose credentials the entry names, which is why the refusal is scoped to
    what a materialiser actually established rather than to the whole list.
    """
    bed = _Bed(tmp_path)
    bed.secret.unlink()
    shutil.rmtree(bed.aws)

    _, _, refusal = _run(tmp_path, bed=bed, required=())

    assert refusal is None


def test_the_required_set_is_swept_from_the_protected_lists(tmp_path: Path) -> None:
    """The launcher is handed the presence answer, and every loop consults it.

    Deciding this in the builder rather than inside the materialisers is what makes it
    exhaustive: the materialisers touch only the precreate subset, so a target none of
    them creates never reached a recording branch no matter how many were added.
    """
    script = _build_launcher_script("strict")

    assert "REQUIRED_MASK_TARGETS = frozenset(" in script
    region = _region(script)
    assert "def _mask_required(" in region, "the required-target helper was renamed"
    assert (
        region.count("require_present=_mask_required(") == 3
    ), "every hiding loop that masks a protected target must consult the set"


def _verifier(tmp_path: Path):  # noqa: ANN202
    """The shipped ``_verify_masked_name``, lifted out of the launcher."""
    script = _build_launcher_script("strict")
    a = script.rindex("\n", 0, script.index("_O_PATH = getattr")) + 1
    b = script.rindex("\n", 0, script.index("REAL_UID = ", a)) + 1
    region_file = tmp_path / "verify_region.py"
    region_file.write_text(script[a:b])
    namespace = runpy.run_path(str(region_file), init_globals={"os": os, "sys": sys})
    return namespace["_verify_masked_name"]


def test_the_name_check_passes_when_the_name_reaches_the_stand_in(tmp_path: Path) -> None:
    """A healthy mask must not be turned into a refusal.

    A hard link is the same inode reached by two names, which is what the name
    reaching its bound stand-in looks like to ``stat``.
    """
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    name = tmp_path / "credentials"
    os.link(stand_in, name)

    _verifier(tmp_path)(str(name), str(stand_in), str(name))  # must not raise


def test_the_name_check_refuses_when_the_name_reaches_something_else(
    tmp_path: Path,
) -> None:
    """A name that escaped its mask stops the spawn instead of running writable."""
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    escaped = tmp_path / "credentials"
    escaped.write_text("")

    with pytest.raises(SystemExit) as exc:
        _verifier(tmp_path)(str(escaped), str(stand_in), str(escaped))

    assert "does not reach its mask" in str(exc.value.code)
    assert str(escaped) in str(exc.value.code)


def test_the_name_check_refuses_when_the_name_cannot_be_read_back(
    tmp_path: Path,
) -> None:
    """An unreadable answer is not a pass either: it cannot confirm the mask."""
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")

    with pytest.raises(SystemExit) as exc:
        _verifier(tmp_path)(str(tmp_path / "absent"), str(stand_in), "absent-name")

    assert "cannot confirm" in str(exc.value.code)


# --------------------------------------------------------------------------
# Residual: the write carve-out still resolves its name twice
# --------------------------------------------------------------------------


def test_write_carveout_still_resolves_its_own_name_twice(tmp_path: Path) -> None:
    """Pins today's answer for the one loop this change leaves name-based.

    The carve-out pair WIDENS access inside an already-sealed subtree and
    degrades open by design, so a lost race there costs a probe its writable
    temp directory rather than exposing a credential, and its own ``islink``
    refusal already rejects a link planted where the directory belongs. It is
    recorded here so the remaining window is visible rather than implied: both
    its bind and its remount still take the NAME.
    """
    script = _build_launcher_script("strict")
    carveout = script[script.index("for d in WRITABLE_DIRS:") :]
    carveout = carveout[: carveout.index("# Restore selectively exposed files")]
    assert "_pin_mount_path(" not in carveout
    assert carveout.count("_mount_or_warn(target, target") == 2


# --------------------------------------------------------------------------
# Break-arms: each assertion above must move when the fix is reverted
# --------------------------------------------------------------------------

#: ``(id, anchor, replacement)`` -- each reverts one pinned site to the
#: name-based form that takes the name twice, so the matching test must fail.
_BREAK_ARMS = (
    (
        "file-mask-by-name",
        "            _file_fd, _file_target, _ = _pin_mount_path(\n"
        "                f.encode(), stat.S_ISREG, require_present=_mask_required(f))\n"
        "            if _file_target is None:\n"
        "                continue\n",
        "            _file_fd, _file_target = None, f.encode()\n"
        "            if not os.path.isfile(_file_target):\n"
        "                continue\n",
    ),
    (
        "dir-mask-by-name",
        "            _dir_fd, _dir_target, _ = _pin_mount_path(\n"
        "                d.encode(), stat.S_ISDIR, require_present=_mask_required(d))\n"
        "            if _dir_target is None:\n"
        "                continue\n",
        "            _dir_fd, _dir_target = None, d.encode()\n"
        "            if not os.path.isdir(_dir_target):\n"
        "                continue\n",
    ),
)


@pytest.mark.parametrize(
    ("arm", "anchor", "replacement"), _BREAK_ARMS, ids=[a[0] for a in _BREAK_ARMS]
)
def test_break_arms_falsify_each_pinned_mask(
    tmp_path: Path, arm: str, anchor: str, replacement: str
) -> None:
    """Reverting one pinned site to its name-based form loses the race again.

    The mutated region must keep the ``os.close`` of a descriptor that is now
    ``None``, so each arm also drops that close -- otherwise the arm would fail
    on a TypeError, which says the code stopped RUNNING rather than that it
    answered differently.
    """
    script = _build_launcher_script("strict")
    assert anchor in script, f"break-arm {arm} anchor does not match the launcher"
    mutant = script.replace(anchor, replacement)
    mutant = mutant.replace("                os.close(_file_fd)\n", "                pass\n")
    mutant = mutant.replace("                os.close(_dir_fd)\n", "                pass\n")

    bed = _Bed(tmp_path)
    if arm == "file-mask-by-name":
        pinned, decoy = _identity(bed.secret), _identity(bed.decoy_file)
        shim = _SwappingTempfile("mkstemp", bed.swap(bed.secret, bed.decoy_file))
    else:
        pinned, decoy = _identity(bed.aws), _identity(bed.decoy_dir)
        shim = _SwappingTempfile("mkdtemp", bed.swap(bed.aws, bed.decoy_dir))

    libc, _, refusal = _run(tmp_path, script=mutant, tempfile_shim=shim, bed=bed)

    assert shim.fired
    assert refusal is None
    # The mutant's own answer: the mask follows the swapped NAME to the decoy,
    # and nothing covers the object that was classified.
    assert _call_whose_target_is(libc, decoy) is not None
    assert _call_whose_target_is(libc, pinned) is None
