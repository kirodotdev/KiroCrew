"""Each launcher mask binds the object its own check inspected, not a re-read name.

Every hiding mount in the namespace launcher needs two answers about one target:
what KIND of object is there, and then mount over it. Taking those from two
separate lookups of the same NAME leaves a window: a name swapped in between is
classified as the old object and bound as the new one, so the mask attaches to
whatever the name points at by then while the bytes it exists to cover sit at a
name nothing masks. The crew data home is writable by an already-running
sandboxed process, so the racing writer is ordinary rather than exotic.

The tests here LOSE that race deliberately and then ask what the mount actually
received. They run the launcher program's own stages
(``kiro_crew.sandbox_launcher_program``) in-process, against real files under
``tmp_path``, with :class:`_Libc` in place of libc. The swap is planted inside the
``mount`` call itself, before the kernel would resolve its target: every shape of
a mask loop passes through that point after classifying the target, the pinned
shape and the name-based one alike, so one test body discriminates between them
instead of asserting a call shape.

``_Libc`` records, AT MOUNT TIME, the device and inode each target reaches,
because that is the question: ``mount(2)`` walks the target path exactly as
``stat`` does, so what the target resolves to in that moment is what the kernel
would bind. A pinned ``/proc/self/fd/<fd>`` target is resolved through its
descriptor, which is open while the stage mounts, so the comparisons hold on every
POSIX host instead of measuring whether procfs exists. A bind of a fresh stand-in
then HIDES its target as a real mount does -- the stand-in is renamed onto the
target's name on the same filesystem, so the name reports the stand-in's identity
-- which lets the launcher's own post-mount name check run for real. Both come
from the shared launcher-program harness (``test_sandbox_launcher_program``).
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest
from test_sandbox_launcher_program import CoveringLibc, identity, launch, payload, refusal

from kiro_crew import sandbox, sandbox_launcher_program, sandbox_plan

program = sandbox_launcher_program

# The namespace launcher runs on Linux ONLY. Its stages address pinned targets as
# ``/proc/self/fd/<fd>``, which the shared harness resolves through the descriptor
# itself rather than through procfs, so the cases that need nothing Linux-specific
# run on every POSIX host. The stages call POSIX-only ``os.getuid``
# and ``os.open(..., dir_fd=...)``, so not on Windows. macOS's own masking is the
# Seatbelt profile, covered by its own suites.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

#: A LINK at a protected name can only be pinned no-follow with ``O_PATH``, which
#: only Linux has; elsewhere the pin refuses it outright (asserted below), so the
#: cases whose subject IS a tolerated link exercise Linux behaviour only. The
#: private-window walk opens every component with ``os.O_PATH`` outright, so a case
#: that binds a window is Linux-only too. The namespace launcher itself runs nowhere
#: else.
_LINUX_LINK_PIN = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="pinning a link no-follow needs O_PATH; the namespace launcher is Linux-only",
)

_MS_BIND = 4096
_MS_REMOUNT = 32

#: ``(source, target, flags)`` -- called as a mount is made.
MountHook = Callable[[object, object, int], None]


# --------------------------------------------------------------------------
# The stand-in libc and the bed
# --------------------------------------------------------------------------


class _Libc(CoveringLibc):
    """The shared covering libc, with the hooks a lost race is planted through.

    A bind of a fresh stand-in hides its target as a real mount does, a window's
    staging bind and its bind back move the window's real tree, and a private tmpfs is
    refused, all as :class:`CoveringLibc` does; every call is recorded in ``calls``
    with the identities its source and target reached at that moment.

    *before* and *after* run around each mount, which is where a racing writer is
    planted. The FIRST bind over each identity in *uncovered* is recorded and then
    left with no effect, as a mount that landed somewhere other than the configured
    name; a later bind over the same object covers it as usual. Each libc moves what
    it covers into a directory of its own under *root*, so a second launch over the
    same tree never collides with the first.
    """

    def __init__(
        self,
        root: Path,
        *,
        before: MountHook | None = None,
        after: MountHook | None = None,
        uncovered: tuple[tuple[int, int] | None, ...] = (),
    ) -> None:
        super().__init__()
        index = 0
        while (root / f"under-masks-{index}").exists():
            index += 1
        (root / f"under-masks-{index}").mkdir()
        self.aside_root = str(root / f"under-masks-{index}")
        self.before = before
        self.after = after
        self.uncovered = set(uncovered)

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001, ANN201
        if self.before is not None:
            self.before(source, target, flags)
        result = super().mount(source, target, fstype, flags, data)
        if self.after is not None:
            self.after(source, target, flags)
        return result

    def bound(self, source, target, fstype, flags):  # noqa: ANN001, ANN201
        if flags == _MS_BIND and source is not None and source != target:
            target_id = identity(target)
            if target_id is not None and target_id in self.uncovered:
                self.uncovered.discard(target_id)
                return 0
        return super().bound(source, target, fstype, flags)

    def target_ids(self) -> list[tuple[int, int] | None]:
        return [call.target_id for call in self.calls]


class _Bed:
    """The filesystem a launch runs against, plus the swap victims."""

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

    def swap(self, victim: Path, decoy: Path) -> Callable[[], None]:
        """A callable that renames *victim* aside and points its NAME at *decoy*."""

        def do_swap() -> None:
            victim.rename(victim.parent / (victim.name + ".moved"))
            victim.symlink_to(decoy)

        return do_swap


class _Ran(NamedTuple):
    libc: _Libc
    bed: _Bed
    refusal: str | None
    launch: Any


def _run(
    tmp_path: Path,
    *,
    bed: _Bed | None = None,
    libc: _Libc | None = None,
    required: tuple[str, ...] = (),
    occupants: dict[str, list[int]] | None = None,
    private_dirs: tuple[str, ...] = (),
    sensitive_dirs: list[str] | None = None,
    sensitive_files: list[str] | None = None,
    readonly_dirs: list[str] | None = None,
    hide_ssh: bool = True,
) -> _Ran:
    """Place every mask (:func:`~kiro_crew.sandbox_launcher_program.place_masks`).

    The plan masks the bed's credential directory, its secret file and ``~/.ssh``,
    and seals its policy cache, unless a list is given. No mask root or window
    identity is vouched for, so every mask pins by name, which is the arm under
    test. The carried ``mask_occupants`` are empty by default: most tests here are
    about which OBJECT a mount received, and an expectation the gateway never
    recorded would refuse those runs before they got that far.
    """
    bed = bed or _Bed(tmp_path)
    libc = libc or _Libc(tmp_path)
    plan = payload(
        sensitive_dirs=[str(bed.aws)] if sensitive_dirs is None else list(sensitive_dirs),
        readonly_dirs=[str(bed.cache)] if readonly_dirs is None else list(readonly_dirs),
        sensitive_files=[str(bed.secret)] if sensitive_files is None else list(sensitive_files),
        private_dirs=list(private_dirs),
        required_mask_targets=list(required),
        mask_occupants=dict(occupants or {}),
        ssh_dir=str(bed.ssh),
        ssh_known_hosts=str(bed.ssh / "known_hosts"),
        hide_ssh=int(hide_ssh),
    )
    run = launch(tmp_path, plan, libc=libc)
    return _Ran(libc, bed, refusal(program.place_masks, run), run)


def _swap_when_bound(victim: Path, swap: Callable[[], None]) -> tuple[MountHook, list[bool]]:
    """A mount hook that runs *swap* once, as the first mask bind over *victim* is made."""
    victim_id = identity(victim)
    fired: list[bool] = []

    def before(source: object, target: object, flags: int) -> None:
        if not fired and flags == _MS_BIND and source != target:
            if identity(target) == victim_id:
                fired.append(True)
                swap()

    return before, fired


def _through_the_builder(occupants: dict[str, tuple[int, ...]]) -> dict[str, list[int]]:
    """The occupant map exactly as the builder hands it to the child.

    The map travels into the child as plan data the builder serialises, so a test
    that injects identities straight into a payload proves nothing about what ships.
    """
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "strict", mask_occupants=occupants)
    return sandbox_plan.namespace_payload(plan)["mask_occupants"]


def _pinning(tmp_path: Path, occupants: dict[str, tuple[int, ...]]) -> Any:
    """A launch whose carried expectations came through the builder."""
    return launch(tmp_path, payload(mask_occupants=_through_the_builder(occupants)))


def _pin(run: Any, path: Path, kind: Callable[[int], bool], **kwargs: Any) -> Any:
    return program._pin_mount_path(run, str(path).encode(), kind, **kwargs)


# --------------------------------------------------------------------------
# The race, lost on purpose
# --------------------------------------------------------------------------


def test_file_mask_binds_the_pinned_file_when_its_name_is_swapped(tmp_path: Path) -> None:
    """A name swapped after classification does not move the file mask.

    The mask lands on the file that was classified; the name now holds the planted
    link, which the post-mount name check then refuses.
    """
    bed = _Bed(tmp_path)
    pinned = identity(bed.secret)
    decoy = identity(bed.decoy_file)
    assert pinned is not None and decoy is not None and pinned != decoy
    before, fired = _swap_when_bound(bed.secret, bed.swap(bed.secret, bed.decoy_file))

    ran = _run(tmp_path, bed=bed, libc=_Libc(tmp_path, before=before))

    assert fired, "the swap never ran, so this test proved nothing"
    assert pinned in ran.libc.target_ids(), "no mount landed on the file that was classified"
    assert decoy not in ran.libc.target_ids(), "a mount landed on the decoy"
    assert ran.refusal is not None and "planted" in ran.refusal
    assert str(bed.secret) in ran.refusal


def test_directory_mask_binds_the_pinned_directory_when_its_name_is_swapped(
    tmp_path: Path,
) -> None:
    """A name swapped after classification does not move the credential mask."""
    bed = _Bed(tmp_path)
    pinned = identity(bed.aws)
    decoy = identity(bed.decoy_dir)
    assert pinned is not None and decoy is not None and pinned != decoy
    before, fired = _swap_when_bound(bed.aws, bed.swap(bed.aws, bed.decoy_dir))

    ran = _run(tmp_path, bed=bed, libc=_Libc(tmp_path, before=before))

    assert fired, "the swap never ran, so this test proved nothing"
    assert pinned in ran.libc.target_ids(), "no mount landed on the directory that was classified"
    assert decoy not in ran.libc.target_ids(), "a mount landed on the decoy"
    assert ran.refusal is not None and "planted" in ran.refusal
    assert str(bed.aws) in ran.refusal


def test_ceiling_seal_refuses_when_the_name_changes_between_bind_and_seal(
    tmp_path: Path,
) -> None:
    """The seal is a remount, so it re-resolves -- and must refuse a changed name.

    A remount can only name the mount the bind just created, which no descriptor
    taken before that bind refers to. The stage therefore resolves the name once
    more and requires the object it reaches to be the object it bound; a
    mismatch means the seal would land elsewhere and leave this ceiling
    writable, so the spawn refuses rather than running unsealed.
    """
    bed = _Bed(tmp_path)
    swap = bed.swap(bed.cache, bed.decoy_dir)
    cache_id = identity(bed.cache)
    fired: list[int] = []

    def after(source: object, target: object, flags: int) -> None:
        # Between the ceiling's bind and its sealing remount.
        if flags == _MS_BIND and not fired and identity(target) == cache_id:
            fired.append(1)
            swap()

    ran = _run(
        tmp_path,
        bed=bed,
        libc=_Libc(tmp_path, after=after),
        sensitive_dirs=[],
        sensitive_files=[],
        hide_ssh=False,
    )

    assert fired, "the swap never ran, so this test proved nothing"
    assert ran.refusal is not None, "the launcher ran on with an unsealed ceiling"
    # The swap is caught by one of three fail-closed paths, all the same refusal
    # on the same race: the pin's carried-identity check (the name now holds a
    # DIFFERENT object than the pass recorded), the seal's bound-vs-sealed
    # comparison (changed identity between being bound and being sealed), or the
    # required-mask wrapper when the re-resolved object cannot be pinned at all
    # (cannot pin ... to mask it). Which one fires depends on how the platform
    # presents the swapped object; each refuses rather than sealing elsewhere.
    assert (
        ("changed identity" in ran.refusal)
        or ("DIFFERENT object" in ran.refusal)
        or ("cannot pin" in ran.refusal)
    )
    assert str(bed.cache) in ran.refusal


def test_known_hosts_is_staged_into_the_standin_before_it_is_mounted(
    tmp_path: Path,
) -> None:
    """Host trust is restored into the stand-in, not through the masked name.

    Writing it after the mask means addressing the restored file through the
    key directory's name a third time, so a name swapped after the pin would
    take the copied trust data outside the mask and leave the masked directory
    with no known hosts at all -- every host then reads as new.
    """
    bed = _Bed(tmp_path)
    ssh_id = identity(bed.ssh)
    staged: list[str | None] = []

    def before(source: object, target: object, flags: int) -> None:
        if flags == _MS_BIND and source != target and identity(target) == ssh_id:
            known = Path(os.fsdecode(source)) / "known_hosts"
            staged.append(known.read_text() if known.is_file() else None)

    ran = _run(tmp_path, bed=bed, libc=_Libc(tmp_path, before=before))

    assert ran.refusal is None, ran.refusal
    assert len(staged) == 1, "the ssh key directory was not masked exactly once"
    assert staged[0] is not None, "known_hosts was not staged into the stand-in"
    assert staged[0] == "example.com ssh-rsa AAAA\n"
    # And the masked name shows exactly that copy.
    assert sorted(os.listdir(bed.ssh)) == ["known_hosts"]
    assert (bed.ssh / "known_hosts").read_text() == "example.com ssh-rsa AAAA\n"


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
    secret_id = identity(bed.secret)
    _deny_open(monkeypatch, bed.secret, errno.EACCES)

    ran = _run(tmp_path, bed=bed)

    assert ran.refusal is not None, "the launcher ran on with the path unmasked"
    assert "cannot pin" in ran.refusal
    assert str(bed.secret) in ran.refusal
    assert secret_id not in ran.libc.target_ids()


def test_absent_masked_file_is_skipped_rather_than_refused(tmp_path: Path) -> None:
    """Absence stays a SKIP, because that is what the plain guards did.

    Every caller-supplied hidden path is offered to both the directory loop and
    the file loop, and each takes the entries of its own kind, so a miss is
    ordinary rather than a race. Turning it into a refusal would fail every
    spawn that hides a path of the other kind.
    """
    bed = _Bed(tmp_path)
    secret_id = identity(bed.secret)
    aws_id = identity(bed.aws)
    bed.secret.unlink()

    ran = _run(tmp_path, bed=bed)

    assert ran.refusal is None, ran.refusal
    assert secret_id not in ran.libc.target_ids()
    # The rest of the sequence still ran: absence skipped one entry, not the loop.
    assert aws_id in ran.libc.target_ids()


def test_ssh_mask_refuses_when_its_directory_cannot_be_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ssh site refuses on EVERY pin miss, not just an unreadable one.

    Its enclosing guard has already established that the directory is there, so
    an absent or wrong-kind answer at the pin is a race rather than an ordinary
    miss -- and this is the tier whose whole purpose is that private keys are not
    readable.
    """
    bed = _Bed(tmp_path)
    ssh_id = identity(bed.ssh)
    _deny_open(monkeypatch, bed.ssh, errno.EACCES)

    ran = _run(tmp_path, bed=bed)

    assert ran.refusal is not None, "strict mode ran on with ~/.ssh visible"
    assert "cannot pin" in ran.refusal
    assert str(bed.ssh) in ran.refusal
    assert ssh_id not in ran.libc.target_ids()


def _vanish_after_the_guard(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Move *victim* aside the moment the ssh guard has answered that it is there."""
    real_lexists = os.path.lexists

    def lexists_then_vanish(path):  # noqa: ANN001, ANN202
        answer = real_lexists(path)
        if answer and os.fsdecode(path) == str(victim):
            # The synchronisation point: the guard has answered, the pin has not
            # run yet. A racing writer moves the directory aside right here.
            victim.rename(victim.parent / (victim.name + ".moved"))
        return answer

    monkeypatch.setattr(os.path, "lexists", lexists_then_vanish)


def test_ssh_mask_refuses_when_its_directory_vanishes_after_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence at the ssh pin is a RACE, so it refuses rather than skipping.

    Its enclosing guard has just established the directory, so the pin finding
    nothing means the name moved in between. Everywhere else absence is an
    ordinary miss and skips; here it cannot, because the skip would exec with
    private keys readable at the name they moved to.
    """
    bed = _Bed(tmp_path)
    decoy_id = identity(bed.decoy_dir)
    _vanish_after_the_guard(monkeypatch, bed.ssh)

    ran = _run(tmp_path, bed=bed)

    assert not bed.ssh.exists(), "the swap never ran, so this test proved nothing"
    assert ran.refusal is not None, "strict mode ran on with no ssh mask at all"
    assert "cannot pin" in ran.refusal
    assert "absent" in ran.refusal
    assert decoy_id not in ran.libc.target_ids()


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
    # Nothing may be mounted over whatever the name reaches (a dangling link
    # reaches nothing, so there is no identity to check for it).
    reached = identity(bed.ssh)

    ran = _run(tmp_path, bed=bed)

    assert ran.refusal is None, f"a ~/.ssh holding no directory refused the spawn: {ran.refusal}"
    err = capsys.readouterr().err
    assert "WARNING" in err and str(bed.ssh) in err and "not a directory" in err
    if reached is not None:
        assert reached not in ran.libc.target_ids()


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
    replacement = identity(bed.ssh)

    ran = _run(tmp_path, bed=bed, occupants=carried)

    assert ran.refusal is not None, "strict mode ran on with the moved key directory readable"
    assert "DIFFERENT object" in ran.refusal
    assert replacement not in ran.libc.target_ids()


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

    ran = _run(tmp_path, bed=bed, occupants=carried)
    assert ran.refusal is None, f"a stable dangling ~/.ssh link refused the spawn: {ran.refusal}"
    assert "not a directory" in capsys.readouterr().err

    # Control: the same link, but the pass saw it REACH a directory.
    vanished = {str(bed.ssh): [seen.st_dev, seen.st_ino, 1, 1]}
    ran = _run(tmp_path, bed=bed, occupants=vanished)
    assert ran.refusal is not None and "vanished" in ran.refusal


def test_the_builder_forwards_the_referent_kind() -> None:
    """What the pass saw the name REACH must survive serialisation into the child.

    Every kind-dependent refusal and skip rests on the fourth element; a builder
    that hands on three leaves the child with ``kind = None`` on every real spawn.
    """
    carried = _through_the_builder(
        {"/data/op/.ssh": (66305, 999, 1, 0, 0, 0), "/data/op/.aws": (66305, 7, 0, 1, 66305, 7)}
    )
    assert carried["/data/op/.ssh"] == [66305, 999, 1, 0, 0, 0]
    assert carried["/data/op/.aws"] == [66305, 7, 0, 1, 66305, 7]
    # An identity recorded without a kind is handed on at three, never padded.
    carried = _through_the_builder({"/data/op/.netrc": (66305, 5, 0)})
    assert carried["/data/op/.netrc"] == [66305, 5, 0]


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

    fd, path = _pin(_pinning(tmp_path, {str(link): carried}), link, stat.S_ISDIR)
    assert fd is not None, "the unchanged link was refused"
    os.close(fd)

    # The swap: same link, a DIFFERENT directory behind it.
    real.rename(tmp_path / "real-store.moved")
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy").rename(real)
    assert os.lstat(link).st_ino == seen.st_ino, "the link itself changed; wrong test"

    run = _pinning(tmp_path, {str(link): carried})
    message = refusal(_pin, run, link, stat.S_ISDIR)
    assert message is not None and "DIFFERENT object" in message


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

    run = _pinning(tmp_path, {str(seen_dir): (seen.st_dev, seen.st_ino, 0, 1)})
    assert _pin(run, seen_dir, stat.S_ISREG) == (
        None,
        None,
    ), "the file loop refused a directory the dir loop masked"

    run = _pinning(tmp_path, {str(seen_dir): (seen.st_dev, seen.st_ino, 0, 2)})
    message = refusal(_pin, run, seen_dir, stat.S_ISREG)
    assert message is not None and "DIFFERENT object" in message


# --------------------------------------------------------------------------
# One object, two spellings: the second spelling reaches the first one's mask
# --------------------------------------------------------------------------
#
# A symlinked ``$HOME`` makes the data home read as relocated, so every crew
# hidden leaf can be listed under both spellings while the pre-spawn pass, which
# deduplicates by inode, records an expectation for the resolved spelling only.
# The launcher masks the first spelling by binding a stand-in over it; the second
# spelling then resolves to that stand-in, whose identity is not the recorded one.
# Without the own-stand-in check that reads as a swapped object and refuses every
# spawn on such a host.


def _masked_by_own_stand_in(
    tmp_path: Path, run: Any, leaf: Path, *, masking: Path | None = None
) -> None:
    """Simulate a mask loop masking *masking* (default *leaf*) with a stand-in.

    What a loop does for every mask: pin the stand-in, register it against the
    object it is about to be bound over, mount. A bind mount presents its source's
    identity at the target name, so the stand-in is moved onto *leaf*. Nothing is
    recorded as a masked NAME, since no read-back ran.
    """
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    stand_in_id = program._stand_in_identity(str(stand_in).encode())
    masked_fd = os.open(str(masking or leaf), os.O_RDONLY)
    try:
        program._register_stand_in(run, stand_in_id, masked_fd)
    finally:
        os.close(masked_fd)
    leaf.rename(tmp_path / "leaf.covered")
    stand_in.rename(leaf)


def _carried_dir(path: Path) -> tuple[int, ...]:
    """What a pass records for a directory it saw at *path*: no link, a directory."""
    seen = os.lstat(path)
    return (seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino)


def test_a_second_spelling_that_reaches_this_launchers_own_stand_in_skips(
    tmp_path: Path,
) -> None:
    """The carried expectation names the object; the stand-in is where it went.

    The pass saw a directory (kind ``1``) at this name and recorded it. Another
    spelling of the same name -- through a linked parent -- is masked first, so
    the entry now holds the stand-in this launcher created. That is the mask in
    place, not a substitution: the second spelling must skip, exactly as it skips
    a name that is absent, and the spawn runs on with the one mask.
    """
    real = tmp_path / "real-home"
    leaf = real / "diag"
    leaf.mkdir(parents=True)
    alias = tmp_path / "linked-home"
    alias.symlink_to(real, target_is_directory=True)
    leaf_id = identity(leaf)
    libc = _Libc(tmp_path)
    run = launch(
        tmp_path,
        payload(
            sensitive_dirs=[str(alias / "diag"), str(leaf)],
            mask_occupants=_through_the_builder({str(leaf): _carried_dir(leaf)}),
        ),
        libc=libc,
    )

    assert refusal(program.mask_sensitive, run) is None, "the second spelling was refused"
    assert libc.target_ids() == [leaf_id], "the leaf was not masked exactly once"
    # And it now covers the leaves listed under it, exactly as the first spelling does.
    assert run.masked_names.get(str(leaf)) == program._stand_in_identity(
        str(leaf).encode()
    ), "the covered second spelling was not recorded as masked"


def test_a_stand_in_this_launcher_did_not_create_still_refuses(tmp_path: Path) -> None:
    """The skip is for THIS launcher's stand-ins only; any other new directory is a swap.

    The control for the case above. A directory with the same shape as a stand-in
    -- fresh, empty, of the right kind -- that this launcher did not register
    is exactly the decoy the identity check exists to refuse.
    """
    leaf = tmp_path / "diag"
    leaf.mkdir()
    run = _pinning(tmp_path, {str(leaf): _carried_dir(leaf)})
    leaf.rename(tmp_path / "diag.moved")
    (tmp_path / "decoy").mkdir()
    (tmp_path / "decoy").rename(leaf)
    assert not run.own_stand_ins, "a stand-in was registered without a mask"

    message = refusal(_pin, run, leaf, stat.S_ISDIR)
    assert message is not None and "DIFFERENT object" in message


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
    run = _pinning(tmp_path, {str(leaf): _carried_dir(leaf)})
    _masked_by_own_stand_in(tmp_path, run, leaf, masking=other)
    assert run.own_stand_ins, "the stand-in was not registered"

    message = refusal(_pin, run, leaf, stat.S_ISDIR)
    assert message is not None and "DIFFERENT object" in message


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
    carried = _carried_dir(leaf)
    leaf.rename(tmp_path / "ssh.moved")

    message = refusal(_pin, _pinning(tmp_path, {str(leaf): carried}), leaf, stat.S_ISDIR)
    assert message is not None and "has vanished" in message

    # The parent gone too is the same vanished object.
    nested = tmp_path / "gone-parent" / "ssh"
    message = refusal(_pin, _pinning(tmp_path, {str(nested): carried}), nested, stat.S_ISDIR)
    assert message is not None and "has vanished" in message

    run = _pinning(tmp_path, {})
    assert _pin(run, leaf, stat.S_ISDIR) == (None, None)
    assert _pin(run, nested, stat.S_ISDIR) == (None, None)


def test_the_ssh_block_is_entered_for_a_carried_identity_with_nothing_at_the_name(
    tmp_path: Path,
) -> None:
    """An empty ``~/.ssh`` name skips the ssh mask only when no pass recorded it.

    The strict tier records ``~/.ssh`` and nothing marks it required, so an
    empty name after the gateway looked would skip the mask silently on an
    existence gate alone. The stage also enters on a carried identity, where the
    pin then refuses the absence. The control: no carried identity, nothing at the
    name, and the stage returns without mounting anything.
    """
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    carried = {str(ssh): list(_carried_dir(ssh))}
    ssh.rename(tmp_path / ".ssh.moved")
    plan = {"hide_ssh": 1, "ssh_dir": str(ssh), "ssh_known_hosts": str(ssh / "known_hosts")}

    libc = _Libc(tmp_path)
    run = launch(tmp_path, payload(mask_occupants=carried, **plan), libc=libc)
    message = refusal(program.mask_ssh_keys, run)
    assert message is not None, "the ssh block skipped an established name that is empty"
    assert "has vanished" in message and str(ssh) in message

    libc = _Libc(tmp_path)
    run = launch(tmp_path, payload(**plan), libc=libc)
    assert refusal(program.mask_ssh_keys, run) is None
    assert libc.calls == []


def _mask_through_the_stage(tmp_path: Path, crew: Path, occupants: dict) -> tuple[Any, _Libc]:
    """Mask *crew* with the directory stage itself, which records the masked name."""
    libc = _Libc(tmp_path)
    run = launch(
        tmp_path,
        payload(sensitive_dirs=[str(crew)], mask_occupants=_through_the_builder(occupants)),
        libc=libc,
    )
    assert refusal(program.mask_sensitive, run) is None
    return run, libc


def test_a_leaf_absent_under_a_directory_this_launcher_masked_skips(tmp_path: Path) -> None:
    """The leaf is gone because its parent's stand-in covers it; that is the mask working.

    A probe hides the whole data home, and every crew hidden leaf is listed under
    it too. Once the directory's stand-in is bound, the leaf is absent from every
    later look -- the file loop is offered every directory entry and always runs
    after -- and the pass's expectation for it is still carried. Refusing there
    fails every spawn on an ordinary host (the readiness probe, so ``/api/models``
    503s and Settings reports ``Failed to load config``). Both absent branches
    take the skip: the leaf itself, and a leaf whose own parent is gone with it.
    """
    crew = tmp_path / "crew"
    leaf = crew / "diag"
    deep = crew / "apps" / "aws-control" / "data"
    deep.mkdir(parents=True)
    leaf.mkdir()
    run, _ = _mask_through_the_stage(
        tmp_path, crew, {str(leaf): _carried_dir(leaf), str(deep): _carried_dir(deep)}
    )
    assert not leaf.exists() and not deep.parent.exists(), "the stand-in is not empty"

    assert _pin(run, leaf, stat.S_ISDIR) == (None, None), "leaf under a mask refused"
    assert _pin(run, leaf, stat.S_ISREG) == (None, None), "file loop refused it"
    assert _pin(run, deep, stat.S_ISDIR) == (None, None), "deep leaf refused"
    # A required target under the mask is legitimately absent for the same reason.
    assert _pin(run, leaf, stat.S_ISDIR, require_present=True) == (None, None)


def test_an_absent_leaf_skips_only_when_the_ancestor_still_reaches_its_stand_in(
    tmp_path: Path,
) -> None:
    """The record alone is not the answer; the ancestor is resolved again, now.

    Two controls. Without the recorded name, the same absence is a vanished
    object and refuses. With the name recorded but the ancestor reaching
    something other than the stand-in the record names, the record and the
    filesystem disagree about a name this launcher masked, and the leaf
    refuses too.
    """
    crew = tmp_path / "crew"
    leaf = crew / "diag"
    leaf.mkdir(parents=True)
    carried = {str(leaf): _carried_dir(leaf)}

    run = _pinning(tmp_path, carried)
    _masked_by_own_stand_in(tmp_path, run, crew)
    assert not run.masked_names, "a name was recorded without a read-back"
    message = refusal(_pin, run, leaf, stat.S_ISDIR)
    assert message is not None and "has vanished" in message

    (tmp_path / "leaf.covered").rename(crew.parent / "crew.real")
    shutil.rmtree(crew)
    (crew.parent / "crew.real").rename(crew)
    run, _ = _mask_through_the_stage(tmp_path, crew, carried)
    # The stand-in at the ancestor is swapped for another empty directory. The
    # stand-in is moved aside rather than removed: a freed inode number is
    # commonly handed to the very next directory created on the same filesystem,
    # and a decoy wearing the stand-in's identity would make this control pass
    # for the wrong reason.
    recorded = run.masked_names[str(crew)]
    (tmp_path / "decoy").mkdir()
    crew.rename(tmp_path / "stand-in.aside")
    (tmp_path / "decoy").rename(crew)
    swapped = os.lstat(crew)
    assert (swapped.st_dev, swapped.st_ino) != tuple(recorded), "the decoy reused the identity"
    message = refusal(_pin, run, leaf, stat.S_ISDIR)
    assert message is not None and "has vanished" in message


def test_the_directory_loop_records_every_name_it_masks(tmp_path: Path) -> None:
    """What the pin consults is written by the stages, after the read-back, for each mask.

    The skip above is only as good as this record: a directory a stage masks but
    does not record leaves every leaf under it refusing.
    """
    ran = _run(tmp_path)
    assert ran.refusal is None, ran.refusal
    recorded = ran.launch.masked_names
    assert str(ran.bed.aws) in recorded, "the credential directory mask was not recorded"
    assert str(ran.bed.ssh) in recorded, "the ssh mask was not recorded"
    for name, stand_in_id in recorded.items():
        assert stand_in_id in ran.launch.own_stand_ins, name


@_LINUX_LINK_PIN
def test_the_directory_loop_records_every_window_it_binds(tmp_path: Path) -> None:
    """The walk's stop condition is written by the stage, for each window it mounts back.

    A window the stage binds but does not record leaves every leaf under it reading
    as covered by the mask above, which is the exposure the record exists to refuse.
    """
    bed = _Bed(tmp_path)
    window = bed.aws / "sso"
    window.mkdir()
    ran = _run(tmp_path, bed=bed, private_dirs=(str(window),))
    assert ran.refusal is None, ran.refusal
    assert ran.launch.bound_windows == {str(window)}, "the bound window was not recorded"
    assert str(bed.aws) in ran.launch.masked_names, "the window's mask root was not recorded"


def test_a_leaf_absent_inside_a_bound_window_is_not_covered_by_the_mask_above(
    tmp_path: Path,
) -> None:
    """Below a window the leaf sits in the REAL tree; the ancestor's stand-in is not over it.

    ``apps/meetings/data`` is mounted back over the data home's stand-in, read-write,
    and the masked ``apps/meetings/data/edits`` inside it is re-hidden afterwards.
    Renaming ``edits`` between the window bind and that re-hide leaves it absent at
    its name while its contents sit live inside the window. The recorded data-home
    mask does not cover that leaf, so the walk must stop at the window and the pin
    must refuse the vanished object. The control: the same absence with no window
    bound is covered and skips.
    """
    crew = tmp_path / "crew"
    window = crew / "apps" / "meetings" / "data"
    leaf = window / "edits"
    leaf.mkdir(parents=True)
    run, libc = _mask_through_the_stage(tmp_path, crew, {str(leaf): _carried_dir(leaf)})
    # What the window bind exposes: the real tree at the window's name, minus the
    # leaf a racing writer has just renamed away.
    real_window = Path(libc.aside[str(crew)]) / "apps" / "meetings" / "data"
    (real_window / "edits").rename(tmp_path / "edits.moved")
    window.parent.mkdir(parents=True)
    real_window.rename(window)
    run.bound_windows.add(str(window))
    assert not leaf.exists() and window.is_dir()

    message = refusal(_pin, run, leaf, stat.S_ISDIR)
    assert message is not None and "has vanished" in message

    # Control: no window bound, the same absence is under the data-home mask.
    run.bound_windows.clear()
    assert _pin(run, leaf, stat.S_ISDIR) == (None, None)


@_LINUX_LINK_PIN
def test_a_data_home_handed_to_the_launcher_under_two_spellings_is_refused(tmp_path: Path) -> None:
    """Two names for one directory break the per-name records; the producer must not hand them over.

    ``$HOME`` is a link to a directory elsewhere: the probe hides the data home
    under its ``$HOME`` spelling and under the resolved one, and the pass records
    the crew hidden leaves under the resolved spelling only. The second spelling
    carries no expectation, so the stage binds a second stand-in over the
    already-masked directory and moves the resolved name onto a stand-in the
    record for it does not name; the file loop then finds the leaf absent,
    ``_covered_by_own_mask`` sees the record and the filesystem disagree, and
    the spawn is refused as a vanished object. This is the refusal every probe
    on such a host would hit, reproduced with duplicate lists handed straight to
    the stages, lists the planner never emits. The launcher keeps that refusal: a
    stand-in reached at a name no pass vouched for is not evidence that the
    name is a second spelling of anything, so the fix is upstream, where the
    planner folds the two spellings onto one
    (``test_sandbox_symlinked_home_launcher.py``).
    """
    real_home = tmp_path / "mnt" / "home" / "u"
    crew = real_home / ".kirocrew"
    leaf = crew / "diag"
    leaf.mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "u").symlink_to(real_home)
    linked_crew = tmp_path / "home" / "u" / ".kirocrew"
    seen = os.lstat(leaf)
    bed = _Bed(tmp_path)
    # Duplicate lists in planner order: the leaf under both spellings, then the
    # data home under the resolved spelling and under ``$HOME``. The file loop is
    # offered every directory entry too.
    dirs = [str(linked_crew / "diag"), str(leaf), str(crew), str(linked_crew)]
    ran = _run(
        tmp_path,
        bed=bed,
        occupants={str(leaf): [seen.st_dev, seen.st_ino, 0, 1, seen.st_dev, seen.st_ino]},
        sensitive_dirs=dirs,
        sensitive_files=[str(bed.secret), *dirs],
    )
    assert ran.refusal is not None, "two spellings of one directory were accepted"
    assert "has vanished" in ran.refusal, ran.refusal
    assert (
        ran.libc.covered.count(str(crew)) == 2
    ), "the second spelling was not masked over the first"


@_LINUX_LINK_PIN
def test_a_link_planted_at_the_name_and_aimed_at_a_stand_in_still_refuses(
    tmp_path: Path,
) -> None:
    """Only the entry AT the name may be the stand-in, never a link's referent.

    The stand-ins live in a shared tmpfs a same-UID writer can enumerate. A link
    planted at the protected name and aimed at one of them would read as
    "already masked" if the referent were consulted, while the real tree sits
    renamed aside. The entry read no-follow is a link, so the own-stand-in skip
    does not apply and the pin refuses as for any substitution.
    """
    leaf = tmp_path / "diag"
    leaf.mkdir()
    run = _pinning(tmp_path, {str(leaf): _carried_dir(leaf)})
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    stand_in_id = program._stand_in_identity(str(stand_in).encode())
    # Registered against THIS object, so only the entry-at-the-name rule refuses it.
    masked_fd = os.open(str(leaf), os.O_RDONLY)
    try:
        program._register_stand_in(run, stand_in_id, masked_fd)
    finally:
        os.close(masked_fd)
    leaf.rename(tmp_path / "diag.moved")
    leaf.symlink_to(stand_in)

    message = refusal(_pin, run, leaf, stat.S_ISDIR)
    assert message is not None and "DIFFERENT object" in message


def test_the_file_mask_registers_its_stand_in_too(tmp_path: Path) -> None:
    """Every mask stage registers its stand-in against the object it masks.

    A stage that mounts without registering leaves a second spelling of its
    target refusing. Each registration is read off the pinned descriptor of the
    object being masked, so it names exactly the object the mount lands on, and it
    is in place before that mount is made. The directory, file and ``~/.ssh`` masks
    are checked here; the nested re-hide inside a window is checked with the
    window cases.
    """
    bed = _Bed(tmp_path)
    runs: list[Any] = []
    at_mount: list[tuple[Any, Any, Any]] = []

    def before(source: object, target: object, flags: int) -> None:
        if runs and flags == _MS_BIND and source != target:
            source_id = identity(source)
            at_mount.append((source_id, identity(target), runs[0].own_stand_ins.get(source_id)))

    libc = _Libc(tmp_path, before=before)
    targets = {name: identity(getattr(bed, name)) for name in ("aws", "secret", "ssh")}
    plan = payload(
        sensitive_dirs=[str(bed.aws)],
        sensitive_files=[str(bed.secret)],
        hide_ssh=1,
        ssh_dir=str(bed.ssh),
        ssh_known_hosts=str(bed.ssh / "known_hosts"),
    )
    runs.append(launch(tmp_path, plan, libc=libc))

    assert refusal(program.place_masks, runs[0]) is None
    assert sorted(target for _, target, _ in at_mount) == sorted(targets.values())
    for source_id, target_id, registered in at_mount:
        assert registered == target_id, "a stand-in was mounted before it was registered"
        assert runs[0].own_stand_ins.get(source_id) == target_id, "a stand-in was not registered"


# --------------------------------------------------------------------------
# The other half of the window: the NAME must reach the mask
# --------------------------------------------------------------------------


@pytest.mark.parametrize("masked", ["aws", "secret", "ssh"])
def test_every_hiding_mount_verifies_its_name_reaches_the_mask(tmp_path: Path, masked: str) -> None:
    """Pinning the object is half the answer; the name is the other half.

    A rename between the pin and the mount leaves the mask on the object that
    was classified while the NAME reaches the racing writer's replacement. That
    is not a leak of what was there -- it is a WRITABLE object at a protected
    name, and the gateway reads several of these back as authoritative. So each
    hiding mount reads its own configured name back: a mount that leaves that
    name reaching something other than its stand-in refuses the spawn, naming it.
    """
    bed = _Bed(tmp_path)
    name = getattr(bed, masked)
    libc = _Libc(tmp_path, uncovered=(identity(name),))

    ran = _run(tmp_path, bed=bed, libc=libc)

    assert identity(name) in ran.libc.target_ids(), "the mask was never mounted"
    assert ran.refusal is not None, f"{name} stayed unmasked and the spawn ran on"
    assert "does not reach its mask" in ran.refusal
    assert str(name) in ran.refusal


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

    ran = _run(tmp_path, bed=bed, required=(str(bed.secret),))

    assert ran.refusal is None, f"a required target of the other kind refused: {ran.refusal}"


def test_a_materialized_target_that_went_missing_refuses(tmp_path: Path) -> None:
    """A target something created before launch cannot be legitimately absent.

    The plain existence guards cannot tell the two absences apart, so they would
    skip both, and the pre-spawn materialisers exist precisely because an absent
    ceiling or credential leaf leaves the data home writable at that name for the
    whole sandbox. Naming the established targets turns the second case into a
    refusal while the first stays a skip.
    """
    bed = _Bed(tmp_path)
    bed.secret.unlink()

    ran = _run(tmp_path, bed=bed, required=(str(bed.secret),))

    assert ran.refusal is not None
    assert "absent" in ran.refusal
    assert str(bed.secret) in ran.refusal


def test_a_materialized_directory_that_went_missing_refuses(tmp_path: Path) -> None:
    """Same rule at the directory loop, which masks the credential stores."""
    bed = _Bed(tmp_path)
    shutil.rmtree(bed.aws)

    ran = _run(tmp_path, bed=bed, required=(str(bed.aws),))

    assert ran.refusal is not None
    assert "absent" in ran.refusal


def test_an_unestablished_target_still_skips_when_absent(tmp_path: Path) -> None:
    """The availability half: a store the host never had must not refuse.

    Requiring every entry would fail the spawn on any host without the tool
    whose credentials the entry names, which is why the refusal is scoped to
    what a materialiser actually established rather than to the whole list.
    """
    bed = _Bed(tmp_path)
    bed.secret.unlink()
    shutil.rmtree(bed.aws)

    ran = _run(tmp_path, bed=bed, required=())

    assert ran.refusal is None, ran.refusal


@pytest.mark.parametrize("site", ["seal", "directory mask", "file mask"])
def test_the_required_set_is_swept_from_the_protected_lists(tmp_path: Path, site: str) -> None:
    """The launcher is handed the presence answer, and every hiding stage consults it.

    The planner hands the established targets to the launcher, and each stage
    that masks a protected target refuses one that is absent. Deciding this in the
    planner rather than inside the materialisers is what makes it exhaustive: the
    materialisers touch only the precreate subset, so a target none of them creates
    would never reach a recording branch no matter how many were added. The nested
    re-hide inside a window is checked with the window cases.
    """
    missing = tmp_path / "established"
    handed = sandbox_plan.namespace_payload(
        sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, "strict", required_mask_targets=(str(missing),)
        )
    )["required_mask_targets"]
    assert handed == [str(missing)], "the planner did not hand the established target on"

    lists = {
        "seal": "readonly_dirs",
        "directory mask": "sensitive_dirs",
        "file mask": "sensitive_files",
    }
    plan = payload(**{lists[site]: [str(missing)]}, required_mask_targets=handed)
    run = launch(tmp_path, plan, libc=_Libc(tmp_path))
    message = refusal(program.place_masks, run)
    assert message is not None, f"the {site} skipped an established target that is absent"
    assert "cannot pin" in message and "absent" in message and str(missing) in message


def _window_bed(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A data home holding a private window that itself holds a masked leaf."""
    crew = tmp_path / "crew"
    window = crew / "apps" / "meetings" / "data"
    nested = window / "edits"
    nested.mkdir(parents=True)
    (nested / "draft").write_text("draft")
    (window / "state.db").write_text("db")
    return crew, window, nested


def _window_run(
    tmp_path: Path, crew: Path, window: Path, nested: Path, **kwargs: Any
) -> tuple[str | None, Any, _Libc]:
    libc = kwargs.pop("libc", None) or _Libc(tmp_path)
    plan = payload(sensitive_dirs=[str(crew), str(nested)], private_dirs=[str(window)], **kwargs)
    run = launch(tmp_path, plan, libc=libc)
    return refusal(program.place_masks, run), run, libc


@_LINUX_LINK_PIN
def test_the_nested_re_hide_is_pinned_and_verified_like_every_other_mask(tmp_path: Path) -> None:
    """A masked leaf INSIDE a window is re-hidden through a descriptor, not a name.

    Once the window is bound, the nested name resolves into the real host tree, so a
    by-name classification followed by a by-name mount is the exact two-lookup shape
    every other mask avoids. The re-hide pins the leaf and mounts over the
    descriptor, so a name swapped after the pin does not move the mask; it registers
    its stand-in against that leaf; and it reads the name back afterwards, so a
    re-hide that leaves the name reaching anything but its stand-in refuses.
    """
    crew, window, nested = _window_bed(tmp_path)
    nested_id = identity(nested)
    error, run, libc = _window_run(tmp_path, crew, window, nested)
    assert error is None, error
    assert sorted(os.listdir(window)) == ["edits", "state.db"]
    assert os.listdir(nested) == [], "the nested leaf came back live inside the window"
    rehide = [m for m in libc.calls if m.target_id == nested_id]
    assert len(rehide) == 1, "the nested leaf was not re-hidden exactly once"
    assert (
        run.own_stand_ins.get(rehide[0].source_id) == nested_id
    ), "the re-hide's stand-in was not registered against the nested leaf"

    # The race: the nested name swapped as its re-hide is mounted.
    other = tmp_path / "second"
    other.mkdir()
    crew, window, nested = _window_bed(other)
    decoy = other / "decoy"
    decoy.mkdir()
    nested_id, decoy_id = identity(nested), identity(decoy)

    def swap() -> None:
        nested.rename(nested.parent / "edits.moved")
        nested.symlink_to(decoy)

    before, fired = _swap_when_bound(nested, swap)
    error, _, libc = _window_run(other, crew, window, nested, libc=_Libc(other, before=before))
    assert fired, "the swap never ran, so this proved nothing"
    assert nested_id in libc.target_ids() and decoy_id not in libc.target_ids()
    assert error is not None and "planted" in error and str(nested) in error

    # A re-hide that leaves the name reaching the real leaf refuses.
    third = tmp_path / "third"
    third.mkdir()
    crew, window, nested = _window_bed(third)
    libc = _Libc(third, uncovered=(identity(nested),))
    error, _, _ = _window_run(third, crew, window, nested, libc=libc)
    assert error is not None and "does not reach its mask" in error and str(nested) in error


@_LINUX_LINK_PIN
def test_an_established_leaf_inside_a_window_refuses_when_absent(tmp_path: Path) -> None:
    """An established leaf inside a window that is absent once the window is bound refuses.

    Below the window the leaf sits in the real tree, so its absence is the leaf
    having moved, not the data-home mask covering it. The leaf is also its own
    mask entry, so the nested re-hide and the directory stage's own pass over that
    entry both consult the required set, and either refuses the same way.
    """
    for established, root in ((True, tmp_path / "established"), (False, tmp_path / "not")):
        root.mkdir()
        crew, window, nested = _window_bed(root)
        shutil.rmtree(nested)
        required = [str(nested)] if established else []
        error, _, _ = _window_run(root, crew, window, nested, required_mask_targets=required)
        if established:
            assert error is not None and "absent" in error and str(nested) in error
        else:
            # Control: not established, the absent leaf is skipped.
            assert error is None, error


def test_the_name_check_passes_when_the_name_reaches_the_stand_in(tmp_path: Path) -> None:
    """A healthy mask must not be turned into a refusal.

    A hard link is the same inode reached by two names, which is what the name
    reaching its bound stand-in looks like to ``stat``.
    """
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    name = tmp_path / "credentials"
    os.link(stand_in, name)

    run = launch(tmp_path)
    program._verify_masked_name(run, str(name), identity(stand_in), str(name))  # must not raise


def test_the_name_check_refuses_when_the_name_reaches_something_else(
    tmp_path: Path,
) -> None:
    """A name that escaped its mask stops the spawn instead of running writable."""
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")
    escaped = tmp_path / "credentials"
    escaped.write_text("")

    run = launch(tmp_path)
    message = refusal(
        program._verify_masked_name, run, str(escaped), identity(stand_in), str(escaped)
    )

    assert message is not None and "does not reach its mask" in message
    assert str(escaped) in message


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
    pinned = identity(stand_in)
    escaped = tmp_path / "credentials"
    escaped.write_text("replacement")
    # The racing writer's move: the stand-in NAME now reaches the replacement.
    stand_in.unlink()
    stand_in.symlink_to(escaped)
    assert identity(stand_in) == identity(escaped), "the swap never ran"

    run = launch(tmp_path)
    message = refusal(program._verify_masked_name, run, str(escaped), pinned, str(escaped))
    assert message is not None and "does not reach its mask" in message


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
    message = refusal(_pin, _pinning(tmp_path, {}), link, stat.S_ISDIR)
    assert message is not None and "no O_PATH" in message


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
    assert identity(planted) == identity(stand_in), "the plant does not reach the stand-in"

    run = launch(tmp_path)
    # The pin saw a regular file at this name (not a link): the link is new.
    run.pinned_occupants[str(planted)] = (1, 2, False)
    message = refusal(
        program._verify_masked_name, run, str(planted), identity(stand_in), str(planted)
    )
    assert message is not None and "planted" in message

    # No pin record at all for this name: same refusal.
    run.pinned_occupants.clear()
    message = refusal(
        program._verify_masked_name, run, str(planted), identity(stand_in), str(planted)
    )
    assert message is not None and "planted" in message


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

    run = launch(tmp_path)
    run.pinned_occupants[str(link)] = (seen.st_dev, seen.st_ino, True)
    program._verify_masked_name(run, str(link), identity(stand_in), str(link))  # must not raise


def test_the_name_check_refuses_when_the_name_cannot_be_read_back(
    tmp_path: Path,
) -> None:
    """An unreadable answer is not a pass either: it cannot confirm the mask."""
    stand_in = tmp_path / "stand_in"
    stand_in.write_text("")

    run = launch(tmp_path)
    message = refusal(
        program._verify_masked_name,
        run,
        str(tmp_path / "absent"),
        identity(stand_in),
        "absent-name",
    )

    assert message is not None and "cannot confirm" in message


# --------------------------------------------------------------------------
# Residual: the write carve-out still resolves its name twice
# --------------------------------------------------------------------------


def test_write_carveout_still_resolves_its_own_name_twice(tmp_path: Path) -> None:
    """Pins today's answer for the one stage that stays name-based.

    The carve-out pair WIDENS access inside an already-sealed subtree and
    degrades open by design, so a lost race there costs a probe its writable
    temp directory rather than exposing a credential, and its own ``islink``
    refusal already rejects a link planted where the directory belongs. It is
    recorded here so the remaining window is visible rather than implied: both
    its bind and its remount take the NAME.
    """
    probe = tmp_path / "run" / "mcp-tmp" / "probe"
    probe.mkdir(parents=True)
    libc = _Libc(tmp_path)
    run = launch(tmp_path, payload(writable_dirs=[str(probe)]), libc=libc)

    program.apply_carveouts(run)

    name = str(probe).encode()
    assert [(m.source, m.target) for m in libc.calls] == [(name, name), (name, name)]
    assert libc.calls[0].flags == _MS_BIND
    assert libc.calls[1].flags & _MS_REMOUNT and libc.calls[1].flags & _MS_BIND
