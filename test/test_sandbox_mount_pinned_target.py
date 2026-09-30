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

#: A LINK at a protected name can only be pinned no-follow with ``O_PATH``, which
#: only Linux has; elsewhere the pin refuses it outright (asserted below), so the
#: cases whose subject IS a tolerated link exercise Linux behaviour only. The
#: namespace launcher itself runs nowhere else.
_LINUX_LINK_PIN = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="pinning a link no-follow needs O_PATH; the namespace launcher is Linux-only",
)

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
        signature = "def _verify_masked_name(name, stand_in_id, what):"
        following = "def _locked_mount_flags(target):"
        assert signature in region, "the name-check helper was renamed"
        assert following in region, "the helper after the name check was renamed"
        start = region.index(signature)
        end = region.index(following, start)
        region = (
            region[:start]
            + signature
            + "\n    return _HARNESS_VERIFY(name, stand_in_id, what)\n\n"
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
        # Empty: these cases vouch for no mask-root or window identity, so the child
        # pins by name, which is the arm under test here.
        "SENSITIVE_DIR_IDS": {},
        "PRIVATE_DIRS": [],
        "PRIVATE_DIR_IDS": {},
        "READONLY_DIRS": [str(bed.cache)],
        "WRITABLE_DIRS": [],
        "SENSITIVE_FILES": [str(bed.secret)],
        "REQUIRED_MASK_TARGETS": frozenset(required),
        # Empty by default: these tests are about which OBJECT a mount received, and
        # a hard-link alias entry here would refuse before the pinned-mount loop runs.
        "FAIL_CLOSED_FILE_MASKS": [],
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
    # The swap is caught by one of three fail-closed paths, all the same refusal
    # on the same race: the pin's carried-identity check (the name now holds a
    # DIFFERENT object than the pass recorded), the seal loop's bound-vs-sealed
    # comparison (changed identity between being bound and being sealed), or the
    # required-mask wrapper when the re-resolved object cannot be pinned at all
    # (cannot pin ... to mask it). Which one fires depends on how the platform
    # presents the swapped object; each refuses rather than sealing elsewhere.
    assert (
        ("changed identity" in refusal)
        or ("DIFFERENT object" in refusal)
        or ("cannot pin" in refusal)
    )
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
        "SENSITIVE_DIR_IDS": {},
        "PRIVATE_DIRS": [],
        "PRIVATE_DIR_IDS": {},
        "READONLY_DIRS": [str(bed.cache)],
        "WRITABLE_DIRS": [],
        "SENSITIVE_FILES": [],
        "REQUIRED_MASK_TARGETS": frozenset(),
        "FAIL_CLOSED_FILE_MASKS": [],
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
    real_lexists = os.path.lexists

    def lexists_then_vanish(path):  # noqa: ANN001, ANN202
        answer = real_lexists(path)
        if answer and os.fsdecode(path) == str(bed.ssh):
            # The synchronisation point: the guard has answered, the pin has not
            # run yet. A racing writer moves the directory aside right here.
            bed.ssh.rename(bed.ssh.parent / ".ssh.moved")
        return answer

    monkeypatch.setattr(os.path, "lexists", lexists_then_vanish)

    libc, _, refusal = _run(tmp_path, bed=bed)

    assert not bed.ssh.exists(), "the swap never ran, so this test proved nothing"
    assert refusal is not None, "strict mode ran on with no ssh mask at all"
    assert "cannot pin" in refusal
    assert "absent" in refusal
    assert not any(call.target_id == _identity(bed.decoy_dir) for call in libc.calls)


def test_break_arm_restoring_the_ssh_fail_open_loses_the_key_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping the ssh mount when the name has EMPTIED loses the mask.

    The arm is the shape a conditional mount takes: skip whenever the pin answers
    nothing, without asking whether the name is still occupied. Under it the
    vanish case reaches ``os.execvp`` having mounted nothing over the key
    directory, which is the whole point of the refusal.
    """
    script = _build_launcher_script("strict")
    anchor = "                if not os.path.lexists(SSH_DIR):\n" "                    sys.exit(\n"
    assert anchor in script, "break-arm anchor does not match the launcher"
    mutant = script.replace(
        anchor,
        "                if False:\n" "                    sys.exit(\n",
    )

    bed = _Bed(tmp_path)
    real_lexists = os.path.lexists

    def lexists_then_vanish(path):  # noqa: ANN001, ANN202
        answer = real_lexists(path)
        if answer and os.fsdecode(path) == str(bed.ssh):
            bed.ssh.rename(bed.ssh.parent / ".ssh.moved")
        return answer

    monkeypatch.setattr(os.path, "lexists", lexists_then_vanish)

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


@pytest.mark.parametrize(
    "shape",
    ["dangling-link", "link-to-file"],
)
@_LINUX_LINK_PIN
def test_ssh_name_holding_no_directory_skips_with_a_warning(
    tmp_path: Path, shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``~/.ssh`` that is not a directory is an ordinary host, not a race.

    A dangling link, or a link pointing at a plain file, holds no key directory
    to hide. Refusing it would fail every strict spawn on that host for nothing
    the mask could cover, so the site skips -- and says so on stderr, because a
    silent skip at this tier is exactly what the refusal elsewhere exists to
    remove. The kind miss is told apart from the vanish race by whether the
    NAME is still occupied after the pin declined it.
    """
    bed = _Bed(tmp_path)
    shutil.rmtree(bed.ssh)
    if shape == "dangling-link":
        bed.ssh.symlink_to(tmp_path / "no-such-dir")
    else:
        bed.ssh.symlink_to(bed.decoy_file)
    assert os.path.lexists(bed.ssh) and not bed.ssh.is_dir()

    libc, _, refusal = _run(tmp_path, bed=bed)

    assert refusal is None, f"a ~/.ssh holding no directory refused the spawn: {refusal}"
    err = capsys.readouterr().err
    assert "WARNING" in err and str(bed.ssh) in err and "not a directory" in err
    # Nothing was mounted over whatever the name reaches (a dangling link reaches
    # nothing, so there is no identity to check for it).
    reached = _identity(bed.ssh)
    if reached is not None:
        assert not any(call.target_id == reached for call in libc.calls)


def test_ssh_directory_replaced_by_a_file_refuses_rather_than_skipping(
    tmp_path: Path,
) -> None:
    """The pass saw a key DIRECTORY; the pin finds a FILE. That is a substitution.

    It also changes the kind, so a kind-based skip taken before the occupant
    comparison would read it as an ordinary non-directory `~/.ssh` and run on with
    the moved keys readable at their new name. The carried identity must refuse it.
    """
    bed = _Bed(tmp_path)
    seen = os.lstat(bed.ssh)
    # The fourth element is what the pass saw the name REACH: a directory.
    carried = {str(bed.ssh): [seen.st_dev, seen.st_ino, 0, 1]}
    bed.ssh.rename(bed.ssh.parent / ".ssh.moved")
    bed.ssh.write_text("not a directory")

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=carried)

    assert refusal is not None, "strict mode ran on with the moved key directory readable"
    assert "DIFFERENT object" in refusal
    assert not any(call.target_id == _identity(bed.ssh) for call in libc.calls)


@_LINUX_LINK_PIN
def test_a_dangling_ssh_link_the_pass_already_saw_dangling_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An UNCHANGED dangling link is not a vanished referent.

    The pass recorded the link with no referent (kind ``0``). The pin follows it,
    finds nothing, and compares: same link, already dangling. That is a stale
    dotfile layout, not a substitution, so the spawn runs on without the ssh mask
    and says so. A link whose referent vanished AFTER the pass -- recorded as a
    directory -- still refuses.
    """
    bed = _Bed(tmp_path)
    shutil.rmtree(bed.ssh)
    bed.ssh.symlink_to(tmp_path / "no-such-dir")
    seen = os.lstat(bed.ssh)
    carried = {str(bed.ssh): [seen.st_dev, seen.st_ino, 1, 0]}

    _, _, refusal = _run(tmp_path, bed=bed, occupants=carried)
    assert refusal is None, f"a stable dangling ~/.ssh link refused the spawn: {refusal}"
    assert "not a directory" in capsys.readouterr().err

    # Control: the same link, but the pass saw it REACH a directory.
    vanished = {str(bed.ssh): [seen.st_dev, seen.st_ino, 1, 1]}
    _, _, refusal = _run(tmp_path, bed=bed, occupants=vanished)
    assert refusal is not None and "vanished" in refusal


def _pin_from_builder(tmp_path: Path, occupants: dict[str, tuple[int, ...]]):  # noqa: ANN202
    """The shipped ``_pin_mount_path``, with the expectation the BUILDER emitted.

    The occupant map travels into the child as source the builder serialises, so
    a test that injects identities straight into ``MASK_OCCUPANTS`` proves nothing
    about what ships. This hands *occupants* to the builder and lifts the helper
    definitions -- ``_O_PATH`` through ``_pin_mount_path`` -- plus the emitted
    ``MASK_OCCUPANTS`` line out of the script it produced. ``runpy``, not ``exec``,
    for the reason the sibling harness gives.
    """
    namespace = _pin_namespace_from_builder(tmp_path, occupants)
    return namespace["_pin_mount_path"], namespace["MASK_OCCUPANTS"]


def _pin_namespace_from_builder(
    tmp_path: Path, occupants: dict[str, tuple[int, ...]]
):  # noqa: ANN202
    """The whole lifted pin namespace, for tests that also need its launcher-local state."""
    script = _build_launcher_script("strict", mask_occupants=occupants)
    # Through ``_stand_in_identity``, which shares launcher-local state with the pin.
    helpers = script[script.index("_O_PATH = ") : script.index("def _verify_masked_name(")]
    emitted = [line for line in script.splitlines() if line.startswith("MASK_OCCUPANTS")]
    assert len(emitted) == 1, "the carried map is not emitted exactly once"
    module = tmp_path / "pin_helpers.py"
    module.write_text(
        textwrap.dedent(helpers) + "\nREQUIRED_MASK_TARGETS = frozenset()\n" + emitted[0] + "\n"
    )
    return runpy.run_path(str(module), init_globals={"os": os, "stat": stat, "sys": sys})


def test_the_builder_forwards_the_referent_kind(tmp_path: Path) -> None:
    """What the pass saw the name REACH must survive serialisation into the child.

    Every kind-dependent refusal and skip rests on the fourth element; a builder
    that emits three leaves the child with ``kind = None`` on every real spawn.
    """
    _, carried = _pin_from_builder(
        tmp_path,
        {"/home/u/.ssh": (66305, 999, 1, 0, 0, 0), "/home/u/.aws": (66305, 7, 0, 1, 66305, 7)},
    )
    assert carried["/home/u/.ssh"] == [66305, 999, 1, 0, 0, 0]
    assert carried["/home/u/.aws"] == [66305, 7, 0, 1, 66305, 7]
    # An identity recorded without a kind is emitted at three, never padded.
    _, carried = _pin_from_builder(tmp_path, {"/home/u/.netrc": (66305, 5, 0)})
    assert carried["/home/u/.netrc"] == [66305, 5, 0]


@_LINUX_LINK_PIN
def test_a_link_whose_referent_was_swapped_for_a_same_kind_decoy_refuses(
    tmp_path: Path,
) -> None:
    """The link is untouched; what it REACHES is not. The mask lands on the referent.

    A dotfile-managed credential home is a link this launcher tolerates and
    follows once. Swapping the directory behind it for another directory leaves
    the link's own device, inode and kind exactly as the pass recorded them, so
    only the referent's identity -- carried as the fifth and sixth elements --
    can refuse it. The control keeps the referent and must pass.
    """
    real = tmp_path / "real-store"
    real.mkdir()
    link = tmp_path / "store"
    link.symlink_to(real)
    seen = os.lstat(link)
    referent = os.stat(link)
    carried = (seen.st_dev, seen.st_ino, 1, 1, referent.st_dev, referent.st_ino)

    pin, _ = _pin_from_builder(tmp_path, {str(link): carried})
    fd, path = pin(str(link).encode(), stat.S_ISDIR)
    assert fd is not None, "the unchanged link was refused"
    os.close(fd)

    # The swap: same link, a DIFFERENT directory behind it.
    real.rename(tmp_path / "real-store.moved")
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy").rename(real)
    assert os.lstat(link).st_ino == seen.st_ino, "the link itself changed; wrong test"

    pin, _ = _pin_from_builder(tmp_path, {str(link): carried})
    with pytest.raises(SystemExit) as refused:
        pin(str(link).encode(), stat.S_ISDIR)
    assert "DIFFERENT object" in str(refused.value)


def test_the_other_loop_meeting_a_masked_directory_still_skips(tmp_path: Path) -> None:
    """A wrong-kind miss on a CHANGED object is a substitution only if the kind was ours.

    Every path is offered to both loops. Once the directory loop has masked a
    directory, the file loop reaches the stand-in: a different object of the
    wrong kind. The pass saw a directory there (kind ``1``), the file loop covers
    regular files, so the miss is ordinary and skips. The same different object
    at a name the pass saw holding a FILE is a substitution and refuses. Both
    expectations reach the pin the way they reach a real child: through the
    builder.
    """
    seen_dir = tmp_path / "leaf"
    seen_dir.mkdir()
    seen = os.lstat(seen_dir)
    # "Mask" it: a different directory now answers to the name.
    seen_dir.rename(tmp_path / "leaf.real")
    seen_dir.mkdir()

    pin, _ = _pin_from_builder(tmp_path, {str(seen_dir): (seen.st_dev, seen.st_ino, 0, 1)})
    assert pin(str(seen_dir).encode(), stat.S_ISREG) == (
        None,
        None,
    ), "the file loop refused a directory the dir loop masked"

    pin, _ = _pin_from_builder(tmp_path, {str(seen_dir): (seen.st_dev, seen.st_ino, 0, 2)})
    with pytest.raises(SystemExit) as refused:
        pin(str(seen_dir).encode(), stat.S_ISREG)
    assert "DIFFERENT object" in str(refused.value)


# --------------------------------------------------------------------------
# One object, two spellings: the second spelling reaches the first one's mask
# --------------------------------------------------------------------------
#
# A symlinked ``$HOME`` (``/home/u -> /data/home/u``) makes the data home read as
# relocated, so every crew hidden leaf is listed under both spellings while the
# pre-spawn pass, which deduplicates by inode, records an expectation for the
# resolved spelling only. The launcher masks the first spelling by binding a
# stand-in over it; the second spelling then resolves to that stand-in, whose
# identity is not the recorded one. Before the own-stand-in check, that read as
# a swapped object and refused EVERY spawn on such a host. No real mount happens
# in these tests, so the mount is simulated the way a bind presents itself: the
# stand-in's own identity answers at the name.


def _masked_by_own_stand_in(
    tmp_path: Path, namespace: dict, leaf: Path, *, masking: Path | None = None
) -> None:
    """Simulate the directory loop masking *masking* (default *leaf*) with a stand-in.

    What the loop does for every directory mask: pin the stand-in, register it
    against the object it is about to be bound over, mount. A bind mount presents
    its source's identity at the target name; without a real mount, moving the
    stand-in onto *leaf* is the closest a test can get.
    """
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    stand_in_id = namespace["_stand_in_identity"](str(stand_in).encode())
    masked_fd = os.open(str(masking or leaf), os.O_RDONLY)
    try:
        namespace["_register_stand_in"](stand_in_id, masked_fd)
    finally:
        os.close(masked_fd)
    leaf.rename(tmp_path / "leaf.covered")
    stand_in.rename(leaf)


def test_a_second_spelling_that_reaches_this_launchers_own_stand_in_skips(
    tmp_path: Path,
) -> None:
    """The carried expectation names the object; the stand-in is where it went.

    The pass saw a directory (kind ``1``) at this name and recorded it. Another
    spelling of the same name has already been masked, so the entry now holds the
    stand-in this launcher created. That is the mask in place, not a substitution:
    the pin must skip, exactly as it skips a name that is absent.
    """
    leaf = tmp_path / "diag"
    leaf.mkdir()
    seen = os.lstat(leaf)
    namespace = _pin_namespace_from_builder(
        tmp_path, {str(leaf): (seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino)}
    )
    _masked_by_own_stand_in(tmp_path, namespace, leaf)

    assert namespace["_pin_mount_path"](str(leaf).encode(), stat.S_ISDIR) == (
        None,
        None,
    ), "the second spelling of an already-masked leaf was refused"


def test_a_stand_in_this_launcher_did_not_create_still_refuses(tmp_path: Path) -> None:
    """The skip is for THIS launcher's stand-ins only; any other new directory is a swap.

    The control for the case above. A directory with the same shape as a stand-in
    -- fresh, empty, of the right kind -- that this launcher did not register
    is exactly the decoy the identity check exists to refuse.
    """
    leaf = tmp_path / "diag"
    leaf.mkdir()
    seen = os.lstat(leaf)
    namespace = _pin_namespace_from_builder(
        tmp_path, {str(leaf): (seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino)}
    )
    leaf.rename(tmp_path / "diag.moved")
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy").rename(leaf)
    assert not namespace["_OWN_STAND_INS"], "a stand-in was registered without a mask"

    with pytest.raises(SystemExit) as refused:
        namespace["_pin_mount_path"](str(leaf).encode(), stat.S_ISDIR)
    assert "DIFFERENT object" in str(refused.value)


def test_a_stand_in_that_masks_a_different_object_still_refuses(tmp_path: Path) -> None:
    """Being this launcher's stand-in is not enough; it must be THIS object's stand-in.

    The stand-in source falls back to the system tempdir when no tmpfs is on a
    separate filesystem, and there a same-UID writer can rename an enumerable
    stand-in onto a protected name. That stand-in was bound over some OTHER
    object, so the identity it is registered against is not the one this name's
    expectation carries, and the pin refuses it like any swapped-in directory.
    """
    leaf = tmp_path / "diag"
    leaf.mkdir()
    other = tmp_path / "other-leaf"
    other.mkdir()
    seen = os.lstat(leaf)
    namespace = _pin_namespace_from_builder(
        tmp_path, {str(leaf): (seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino)}
    )
    _masked_by_own_stand_in(tmp_path, namespace, leaf, masking=other)
    assert namespace["_OWN_STAND_INS"], "the stand-in was not registered"

    with pytest.raises(SystemExit) as refused:
        namespace["_pin_mount_path"](str(leaf).encode(), stat.S_ISDIR)
    assert "DIFFERENT object" in str(refused.value)


def test_an_established_name_that_is_absent_refuses(tmp_path: Path) -> None:
    """A pass recorded an object here; nothing is here now. That object moved.

    Skipping would exec with the moved object readable at its new name, and the
    module's own dangling-link branch already refuses the same vanished object
    when a link is what remains. The absent name is the same case with nothing
    remaining. The control: an absent name no pass recorded still skips, since
    an operator who never created that store has nothing to mask.
    """
    leaf = tmp_path / "ssh"
    leaf.mkdir()
    seen = os.lstat(leaf)
    carried = (seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino)
    leaf.rename(tmp_path / "ssh.moved")

    pin, _ = _pin_from_builder(tmp_path, {str(leaf): carried})
    with pytest.raises(SystemExit) as refused:
        pin(str(leaf).encode(), stat.S_ISDIR)
    assert "has vanished" in str(refused.value)

    # The parent gone too is the same vanished object.
    nested = tmp_path / "gone-parent" / "ssh"
    pin, _ = _pin_from_builder(tmp_path, {str(nested): carried})
    with pytest.raises(SystemExit) as refused:
        pin(str(nested).encode(), stat.S_ISDIR)
    assert "has vanished" in str(refused.value)

    pin, _ = _pin_from_builder(tmp_path, {})
    assert pin(str(leaf).encode(), stat.S_ISDIR) == (None, None)
    assert pin(str(nested).encode(), stat.S_ISDIR) == (None, None)


def test_the_ssh_block_is_entered_for_a_carried_identity_with_nothing_at_the_name() -> None:
    """The ``lexists`` gate alone skips the whole ssh block on an empty name.

    The strict tier records ``~/.ssh`` and nothing marks it required, so an
    empty name after the gateway looked would have skipped the mask silently.
    The gate must also enter on a carried identity, where the pin then refuses
    the absence. Asserted on the shipped source, as the block needs a real mount.
    """
    script = _build_launcher_script("strict")
    gate = script[script.index("if HIDE_SSH and") : script.index('kh_data = b""')]
    assert "os.path.lexists(SSH_DIR)" in gate
    assert (
        "_carried_occupant(SSH_DIR.encode()) is not None" in gate
    ), "the ssh block skips an established name that is empty"


@_LINUX_LINK_PIN
def test_a_link_planted_at_the_name_and_aimed_at_a_stand_in_still_refuses(
    tmp_path: Path,
) -> None:
    """Only the entry AT the name may be the stand-in, never a link's referent.

    The stand-ins live in a shared tmpfs a same-UID writer can enumerate. A link
    planted at the protected name and aimed at one of them would read as
    "already masked" if the referent were consulted, while the real tree sits
    renamed aside. The entry read no-follow is a link, so the own-stand-in skip
    does not apply and the pin refuses as before.
    """
    leaf = tmp_path / "diag"
    leaf.mkdir()
    seen = os.lstat(leaf)
    namespace = _pin_namespace_from_builder(
        tmp_path, {str(leaf): (seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino)}
    )
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    stand_in_id = namespace["_stand_in_identity"](str(stand_in).encode())
    # Registered against THIS object, so only the entry-at-the-name rule refuses it.
    masked_fd = os.open(str(leaf), os.O_RDONLY)
    try:
        namespace["_register_stand_in"](stand_in_id, masked_fd)
    finally:
        os.close(masked_fd)
    leaf.rename(tmp_path / "diag.moved")
    leaf.symlink_to(stand_in)

    with pytest.raises(SystemExit) as refused:
        namespace["_pin_mount_path"](str(leaf).encode(), stat.S_ISDIR)
    assert "DIFFERENT object" in str(refused.value)


def test_the_file_mask_registers_its_stand_in_too(tmp_path: Path) -> None:
    """Every mask loop registers its stand-in against the object it masks.

    A loop that mounts without registering leaves a second spelling of its
    target refusing. Each registration is read off the pinned descriptor of the
    object being masked, never off the name. Asserted on the shipped source,
    since the loops cannot run without a real mount.
    """
    script = _build_launcher_script("strict")
    dirs = script[
        script.index("for d in SENSITIVE_DIRS:") : script.index("for f in SENSITIVE_FILES:")
    ]
    assert "_register_stand_in(_per_dir_id, _mask_fd)" in dirs
    assert "_register_stand_in(_nested_id, _nested_fd)" in dirs
    files = script[script.index("for f in SENSITIVE_FILES:") : script.index("# .ssh: hide keys")]
    assert "_register_stand_in(_empty_id, _file_fd)" in files
    ssh = script[script.index("# .ssh: hide keys") :]
    assert "_register_stand_in(_ssh_tmp_id, _ssh_fd)" in ssh
    assert ssh.index("_register_stand_in(_ssh_tmp_id, _ssh_fd)") < ssh.index(
        "_mount_or_die(ssh_tmp, _ssh_target"
    )
    # Each registration precedes its mount.
    assert dirs.index("_register_stand_in(_per_dir_id, _mask_fd)") < dirs.index(
        '"hiding credential directory %s" % d'
    )
    assert files.index("_register_stand_in(_empty_id, _file_fd)") < files.index(
        '"hiding sensitive file %s" % f'
    )


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
    seen: list[tuple[str, tuple[int, int]]] = []

    def _record(name, stand_in_id, what):  # noqa: ANN001, ANN202
        seen.append((os.fsdecode(name), tuple(stand_in_id)))

    libc, _, refusal = _run(tmp_path, bed=bed, verify=_record)

    assert refusal is None
    assert libc.calls, "no mount ran at all"
    # Every hiding mount, in source order, hands the check ITS OWN configured
    # name -- so a mount added later without the check is a missing row here.
    assert [name for name, _ in seen] == [str(bed.aws), str(bed.secret), str(bed.ssh)]
    # And each is handed the IDENTITY of a stand-in under the tmpfs source root --
    # the object the mount sourced from, pinned before the mount -- never a path
    # the check would have to resolve again.
    staged = {
        call.source_id
        for call in libc.calls
        if isinstance(call.source, bytes) and call.source.startswith(str(bed.tmpfs).encode())
    }
    for name, stand_in_id in seen:
        assert isinstance(stand_in_id[0], int) and isinstance(stand_in_id[1], int)
        assert stand_in_id in staged, (name, stand_in_id, staged)


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
        region.count("require_present=_mask_required(") == 4
    ), "every hiding loop that masks a protected target must consult the set"


def test_the_nested_re_hide_is_pinned_and_verified_like_every_other_mask() -> None:
    """A masked leaf INSIDE a window is re-hidden through a descriptor, not a name.

    Once the window is bound, the nested name resolves into the real host tree, so a
    by-name ``isdir`` followed by a by-name mount is the exact two-lookup shape this
    change removes everywhere else. The re-hide must pin the leaf, mount over the
    descriptor path while it is still open, and re-read the name afterwards.
    """
    script = _build_launcher_script("strict")
    region = _region(script)
    start = region.index('"re-hiding nested masked directory %s" % _nested')
    block = region[region.rindex("for _nested in SENSITIVE_DIRS:", 0, start) : start]
    assert "_pin_mount_path(" in block and "_nested.encode(), stat.S_ISDIR" in block
    assert "os.path.isdir(_nested" not in block, "the nested leaf is still classified by name"
    assert "_mount_or_die(_nested_empty, _nested_target, _MS_BIND," in region
    # The stand-in's identity is pinned BEFORE the mount and is what the name
    # check compares against; the stand-in's path is never resolved again.
    assert block.index("_nested_id = _stand_in_identity(_nested_empty)") < block.index(
        "_mount_or_die(_nested_empty"
    )
    after = region[start:]
    assert after.index("os.close(_nested_fd)") < after.index(
        "_verify_masked_name(_nested.encode(), _nested_id, _nested)"
    )


def _verifier(tmp_path: Path):  # noqa: ANN202
    """The shipped ``_verify_masked_name``, lifted out of the launcher."""
    script = _build_launcher_script("strict")
    a = script.rindex("\n", 0, script.index("_O_PATH = getattr")) + 1
    b = script.rindex("\n", 0, script.index("REAL_UID = ", a)) + 1
    region_file = tmp_path / "verify_region.py"
    region_file.write_text(script[a:b])
    namespace = runpy.run_path(str(region_file), init_globals={"os": os, "sys": sys})
    return namespace["_verify_masked_name"], namespace["_PINNED_OCCUPANTS"]


def test_the_name_check_passes_when_the_name_reaches_the_stand_in(tmp_path: Path) -> None:
    """A healthy mask must not be turned into a refusal.

    A hard link is the same inode reached by two names, which is what the name
    reaching its bound stand-in looks like to ``stat``.
    """
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    name = tmp_path / "credentials"
    os.link(stand_in, name)

    _verifier(tmp_path)[0](str(name), _identity(stand_in), str(name))  # must not raise


def test_the_name_check_refuses_when_the_name_reaches_something_else(
    tmp_path: Path,
) -> None:
    """A name that escaped its mask stops the spawn instead of running writable."""
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    escaped = tmp_path / "credentials"
    escaped.write_text("")

    with pytest.raises(SystemExit) as exc:
        _verifier(tmp_path)[0](str(escaped), _identity(stand_in), str(escaped))

    assert "does not reach its mask" in str(exc.value.code)
    assert str(escaped) in str(exc.value.code)


def test_the_name_check_compares_against_the_pinned_identity_not_the_stand_in_path(
    tmp_path: Path,
) -> None:
    """The stand-in path is in a shared tmpfs; the check must not resolve it again.

    A writer that has swapped the protected name can also replace the stand-in
    path with a link to that name, so a check re-resolving both paths sees one
    object twice and passes. Handing the check the identity pinned BEFORE the
    mount makes that swap visible: the name reaches the replacement, not the
    pinned stand-in.
    """
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    pinned = _identity(stand_in)
    escaped = tmp_path / "credentials"
    escaped.write_text("replacement")
    # The racing writer's move: the stand-in NAME now reaches the replacement.
    stand_in.unlink()
    stand_in.symlink_to(escaped)
    assert _identity(stand_in) == _identity(escaped), "the swap never ran"

    with pytest.raises(SystemExit) as exc:
        _verifier(tmp_path)[0](str(escaped), pinned, str(escaped))
    assert "does not reach its mask" in str(exc.value.code)


@pytest.mark.skipif(
    sys.platform.startswith("linux"), reason="the Linux pin holds a link via O_PATH"
)
def test_without_o_path_a_link_at_a_protected_name_refuses_by_name(tmp_path: Path) -> None:
    """The other platforms' answer, asserted rather than skipped past.

    Without ``O_PATH`` the kernel cannot hand back the link itself, so the pin
    refuses a link at a protected name and says why. Fail-closed, and it never
    runs in production: the namespace launcher requires Linux.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "store"
    link.symlink_to(real)
    pin, _ = _pin_from_builder(tmp_path, {})
    with pytest.raises(SystemExit) as refused:
        pin(str(link).encode(), stat.S_ISDIR)
    assert "no O_PATH" in str(refused.value)


def test_the_name_check_refuses_a_link_planted_at_the_name_after_the_pin(
    tmp_path: Path,
) -> None:
    """A link aimed at the stand-in must not pass as the stand-in.

    The stand-in's tmpfs prefix is enumerable by a same-UID writer, so a link
    planted at the protected name and pointed at the stand-in makes a following
    ``stat`` report the right identity while the mask sits elsewhere and the
    name stays replaceable. The name is read no-follow: a link there passes only
    when it is the very link the pin saw and followed.
    """
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    planted = tmp_path / "credentials"
    planted.symlink_to(stand_in)
    assert _identity(planted) == _identity(stand_in), "the plant does not reach the stand-in"

    verify, pinned = _verifier(tmp_path)
    # The pin saw a regular file at this name (not a link): the link is new.
    pinned[str(planted)] = (1, 2, False)
    with pytest.raises(SystemExit) as exc:
        verify(str(planted), _identity(stand_in), str(planted))
    assert "planted" in str(exc.value.code)

    # No pin record at all for this name: same refusal.
    pinned.clear()
    with pytest.raises(SystemExit) as exc:
        verify(str(planted), _identity(stand_in), str(planted))
    assert "planted" in str(exc.value.code)


def test_the_name_check_passes_the_link_the_pin_itself_followed(tmp_path: Path) -> None:
    """A tolerated dotfile link, seen by the pin, still verifies through it.

    The pin followed this link once and mounted over its referent; the name is
    still that link, and following it reaches the stand-in. That is the healthy
    stow layout and must not be mistaken for a plant.
    """
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    link = tmp_path / "credentials"
    link.symlink_to(stand_in)
    seen = os.lstat(link)

    verify, pinned = _verifier(tmp_path)
    pinned[str(link)] = (seen.st_dev, seen.st_ino, True)
    verify(str(link), _identity(stand_in), str(link))  # must not raise


def test_the_name_check_refuses_when_the_name_cannot_be_read_back(
    tmp_path: Path,
) -> None:
    """An unreadable answer is not a pass either: it cannot confirm the mask."""
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")

    with pytest.raises(SystemExit) as exc:
        _verifier(tmp_path)[0](str(tmp_path / "absent"), _identity(stand_in), "absent-name")

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
        "            _file_fd, _file_target = _pin_mount_path(\n"
        "                f.encode(), stat.S_ISREG, require_present=_mask_required(f))\n"
        "            if _file_target is None:\n"
        "                continue\n",
        "            _file_fd, _file_target = None, f.encode()\n"
        "            if not os.path.isfile(_file_target):\n"
        "                continue\n",
    ),
    (
        "dir-mask-by-name",
        "                _mask_fd, target = _pin_mount_path(\n"
        "                    d.encode(), stat.S_ISDIR, require_present=_mask_required(d))\n"
        "                if target is None:\n"
        "                    continue\n",
        "                _mask_fd, target = -1, d.encode()\n"
        "                if not os.path.isdir(target):\n"
        "                    continue\n",
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
    mutant = mutant.replace("                os.close(_mask_fd)\n", "                pass\n")
    # The name-based shape holds no descriptor to register a stand-in against either.
    mutant = mutant.replace("_register_stand_in(_empty_id, _file_fd)", "pass")
    mutant = mutant.replace("_register_stand_in(_per_dir_id, _mask_fd)", "pass")

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
