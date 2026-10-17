"""A symlinked protected name keeps working, and a SUBSTITUTED one refuses.

The launcher's hiding mounts resolve each protected name to decide what to mask.
Two layouts put a symlink at such a name, and they need OPPOSITE answers:

* an ordinary ``stow`` or ``chezmoi`` dotfile layout, where ``~/.ssh`` has been a
  link to the user's own store since before the gateway started. Refusing it
  would fail every strict spawn on a supported machine, so it must WORK -- the
  mask follows the link once and covers the store the keys actually live in;
* a link SUBSTITUTED for a directory while the launcher is looking, which is the
  redirect: the mask lands on the planter's decoy while the real directory,
  renamed aside, stays readable.

Nothing at a single instant separates them, which is why the launcher does not
try: it carries the identity of what occupied the name when the pre-spawn pass
looked, takes its own FIRST look without following, and refuses when that look
finds a different occupant. A link that was already there is the same link at
both looks and passes; a directory replaced by a link is not, and refuses.

The cases run the launcher program's own stages
(``kiro_crew.sandbox_launcher_program``) in-process against real files, through
the sibling suite's harness (``test_sandbox_mount_pinned_target``), whose stand-in
libc makes a bind hide its target so the post-mount name check runs for real.

The ``O_DIRECTORY | O_NOFOLLOW`` shape a no-follow fix reaches for first is
measured here as the thing that breaks the supported layout, so the refusal it
would cause cannot creep back in unnoticed.
"""

from __future__ import annotations

import ast
import builtins
import os
import re
import stat
import sys
from pathlib import Path

import pytest
from test_sandbox_launcher_program import identity, launch, payload, refusal, rendered_payload
from test_sandbox_mount_pinned_target import _Bed, _Libc, _run, _window_bed

from kiro_crew import sandbox, sandbox_launcher_program

program = sandbox_launcher_program

# Same ground as the sibling pinned-mount suite: the launcher runs on Linux only.
# The no-follow first look relies on ``O_PATH`` -- absent on Darwin, where
# ``O_RDONLY | O_NOFOLLOW`` on a symlinked protected name raises ELOOP instead of
# returning a descriptor on the link -- so these symlink-substitution cases are
# meaningful on Linux alone. macOS's own masking is the Seatbelt profile, covered
# by its own suites.
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace launcher only")


def _stow_bed(tmp_path: Path) -> tuple[_Bed, Path]:
    """A bed whose protected ssh NAME is a symlink, as ``stow`` leaves it.

    The real store keeps the key material and the host trust, so a mask that
    lands on the store hides the keys and one that lands anywhere else does not.
    """
    bed = _Bed(tmp_path)
    store = tmp_path / "dotfiles" / "ssh"
    store.mkdir(parents=True)
    (store / "known_hosts").write_text("example.com ssh-rsa AAAA\n")
    (store / "id_ed25519").write_text("PRIVATE KEY\n")
    for child in bed.ssh.iterdir():
        child.unlink()
    bed.ssh.rmdir()
    bed.ssh.symlink_to(store)
    return bed, store


def _observed(*paths: Path) -> dict[str, list[int]]:
    """What a pre-spawn pass would record for *paths*, in the carried form.

    One ``lstat`` each, no following, keyed by the name -- the shape
    ``_refuse_aliased_masked_leaves`` records and the planner hands on. Built
    here so a test states the identity the gateway SAW rather than letting the
    launcher take its own look, which is the distinction under test.
    """
    carried: dict[str, list[int]] = {}
    for path in paths:
        info = os.lstat(str(path))
        carried[str(path)] = [info.st_dev, info.st_ino, int(stat.S_ISLNK(info.st_mode))]
    return carried


# --------------------------------------------------------------------------
# The supported layout must keep working
# --------------------------------------------------------------------------


def test_strict_spawn_still_boots_when_the_protected_name_is_a_symlink(
    tmp_path: Path,
) -> None:
    """A stow-shaped ``~/.ssh`` does not refuse the spawn.

    This is the cost a no-follow tightening charges, and it is charged on an
    ordinary machine rather than an exotic one, so it is asserted first.
    """
    bed, store = _stow_bed(tmp_path)

    ran = _run(tmp_path, bed=bed, occupants=_observed(bed.ssh))

    assert ran.refusal is None, f"a symlinked ~/.ssh refused the spawn: {ran.refusal}"
    assert identity(store) is not None


def test_the_key_store_behind_the_symlink_is_the_object_masked(tmp_path: Path) -> None:
    """Following the link once is what puts the mask over the real keys.

    A mask that stopped at the link would cover nothing, and the private key in
    the store would stay readable inside the sandbox.
    """
    bed, store = _stow_bed(tmp_path)
    store_id = identity(store)

    ran = _run(tmp_path, bed=bed, occupants=_observed(bed.ssh))

    assert ran.refusal is None, ran.refusal
    assert store_id in ran.libc.target_ids(), (
        "no mount landed on the store the symlink resolves to, so the keys "
        "behind it were never masked"
    )
    assert not (bed.ssh / "id_ed25519").exists(), "the private key is readable through the link"


def test_host_trust_is_still_carried_across_a_symlinked_name(tmp_path: Path) -> None:
    """``known_hosts`` read through the link reaches the store's copy.

    Losing it would point ``UserKnownHostsFile`` at an absent file while
    ``accept-new`` is still on, so every host would read as new.
    """
    bed, _ = _stow_bed(tmp_path)

    ran = _run(tmp_path, bed=bed, occupants=_observed(bed.ssh))

    assert ran.refusal is None, ran.refusal
    assert sorted(os.listdir(bed.ssh)) == ["known_hosts"], "host trust was dropped by the mask"
    assert (bed.ssh / "known_hosts").read_text() == "example.com ssh-rsa AAAA\n"


def test_the_nofollow_directory_open_is_what_breaks_the_supported_layout(
    tmp_path: Path,
) -> None:
    """Measure the tightening's cost rather than arguing about it.

    ``O_DIRECTORY | O_NOFOLLOW`` is the shape "just refuse a link" reaches for.
    On the supported layout it raises, which is a refused spawn on a machine
    that has done nothing wrong -- the reason the launcher carries an identity
    instead.
    """
    _, store = _stow_bed(tmp_path)
    name = store.parent / "linked.ssh"
    name.symlink_to(store)

    with pytest.raises(NotADirectoryError):
        os.open(str(name), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    # The same flags on the store itself are fine, so the refusal above is about
    # the LINK and not about the flag combination being unusable.
    fd = os.open(str(store), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# The substitution must refuse
# --------------------------------------------------------------------------


def _substitute_link_at(monkeypatch: pytest.MonkeyPatch, victim: Path, decoy: Path) -> None:
    """Replace *victim* with a link to *decoy* once the guard has answered.

    The launcher's own guard on the ssh name is ``os.path.lexists`` (a no-follow
    existence check that enters the block for a link too, leaving the pin to
    catch a substitution). It answers True both before and after the swap,
    which is exactly why the guard alone cannot see this happen -- the carried
    identity is what catches it at the pin.
    """
    real_lexists = os.path.lexists

    def lexists_then_substitute(path):  # noqa: ANN001, ANN202
        answer = real_lexists(path)
        if answer and os.fsdecode(path) == str(victim) and not victim.is_symlink():
            victim.rename(victim.parent / (victim.name + ".moved"))
            victim.symlink_to(decoy)
        return answer

    monkeypatch.setattr(os.path, "lexists", lexists_then_substitute)


def test_a_directory_substituted_by_a_link_after_the_guard_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redirect the guard cannot see, caught by the carried identity.

    A real directory occupies the name at the first look. A racing writer renames
    it aside and drops a link to its own decoy. Following that link would mask
    the decoy and leave the renamed directory readable, with the post-mount name
    check passing because it follows the same link.
    """
    bed = _Bed(tmp_path)
    # What the gateway saw: a real directory at that name, recorded before the
    # script was written. The swap below happens after that, which is the whole
    # window this carries an identity across.
    carried = _observed(bed.ssh)
    decoy_id = identity(bed.decoy_dir)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)

    ran = _run(tmp_path, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink(), "the substitution never ran, so this proved nothing"
    assert ran.refusal is not None, (
        "the launcher masked a decoy the planter chose and ran on, leaving the "
        "renamed key directory readable"
    )
    assert "DIFFERENT object" in ran.refusal
    assert decoy_id not in ran.libc.target_ids()


def test_a_symlink_that_was_always_there_is_not_treated_as_a_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminator is a CHANGE of occupant, not the presence of a link.

    Same hook as the substitution above, firing on a name that is already a
    link. Nothing is swapped, so the spawn proceeds -- this is what keeps the
    supported layout working while the substitution refuses.
    """
    bed, store = _stow_bed(tmp_path)
    store_id = identity(store)
    carried = _observed(bed.ssh)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)

    ran = _run(tmp_path, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink()
    assert ran.refusal is None, f"an untouched symlinked name refused: {ran.refusal}"
    assert store_id in ran.libc.target_ids()


# --------------------------------------------------------------------------
# Enumerated by condition, not by a list of sites
# --------------------------------------------------------------------------

#: The launcher's protected-name resolutions, found by what they DO rather than
#: by where they are: an ``os.open`` of a caller-supplied protected target. A new
#: site added without a no-follow first look is caught by this, which a hand-kept
#: list of line numbers would not be.
_OPEN_CALL = re.compile(r"os\.open\(\s*([^,]+),\s*([^\n]*?)\)", re.S)


def _program_source() -> str:
    """The launcher program a spawn runs, minus only its one plan substitution."""
    return Path(program.__file__).read_text(encoding="utf-8")


def _launcher_protected_opens(script: str) -> list[tuple[str, str]]:
    """Every ``os.open`` in the launcher whose subject is a protected target."""
    found = []
    for match in _OPEN_CALL.finditer(script):
        subject, flags = match.group(1).strip(), match.group(2).strip()
        if subject.startswith('"/proc/self/fd/') or "/proc/self/fd/" in subject:
            continue  # re-opening a descriptor this launcher already holds
        found.append((subject, flags))
    return found


def test_every_protected_name_resolution_takes_a_no_follow_first_look() -> None:
    """Stated as a condition over the source, so a new site cannot slip in.

    The condition is not "this line looks right at line N". It is: the launcher
    resolves protected names through ONE helper, that helper's first look does
    not follow, and it can be handed an identity to compare against. A resolution
    added anywhere else, or a first look that starts following again, fails this
    without anyone maintaining a list of sites. Read off the program module, which
    is the launcher a spawn runs with its plan substituted in.
    """
    script = _program_source()

    assert (
        "_O_PATH | os.O_NOFOLLOW" in script or "os.O_NOFOLLOW | _O_PATH" in script
    ), "the launcher takes no no-follow first look at any protected name"
    assert (
        "expect_occupant" in script
    ), "no resolution can be asked to compare against an earlier look"

    opens = _launcher_protected_opens(script)
    assert opens, "no protected-name resolution found; the matcher has drifted"
    # The condition is about the protected NAME, not about following as such.
    # Following a link's own TARGET is the supported layout working; following the
    # protected name a second time is the bypass. So: no open whose subject is the
    # name may follow, and the only following opens left take the link's content.
    name_subjects = ("_t", "target", "_leaf")
    following_the_name = [
        (subject, flags)
        for subject, flags in opens
        if subject in name_subjects and "_O_PATH" in flags and "O_NOFOLLOW" not in flags
    ]
    assert not following_the_name, (
        "a protected name is resolved with following semantics: %r" % following_the_name
    )
    # No call site may resolve a PROTECTED name for itself, outside the pin. The
    # stand-in pin is the one other ``_O_PATH`` open, and it resolves a directory
    # this launcher created moments ago, not a protected name.
    outside = [
        line.strip()
        for line in script.splitlines()
        if "os.open(" in line
        and "_O_PATH" in line
        and "_leaf" not in line
        and "_link_to" not in line
        and "parent_fd" not in line
        and "os.open(stand_in," not in line
    ]
    assert not outside, f"a protected name is resolved outside the pin: {outside}"


# --------------------------------------------------------------------------
# The swap ACROSS the follow, which a by-name reopen would miss
# --------------------------------------------------------------------------


def _swap_the_link_during_the_follow(
    monkeypatch: pytest.MonkeyPatch, victim: Path, decoy: Path
) -> dict:
    """Replace an existing link at *victim* while its target is being resolved.

    The launcher looks at the leaf relative to its held parent: a no-follow first
    look, then the single follow through the descriptor open on that link. This
    lands the swap between the two, which is the window a reopen of the whole
    name would leave open.
    """
    state = {"fired": False}
    real_open = os.open

    def open_with_swap(path, flags, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        first_look = (
            kwargs.get("dir_fd") is not None
            and os.fsdecode(path) == victim.name
            and bool(flags & os.O_NOFOLLOW)
        )
        result = real_open(path, flags, *args, **kwargs)
        if first_look and not state["fired"]:
            state["fired"] = True
            victim.unlink()
            victim.symlink_to(decoy)
        return result

    monkeypatch.setattr(os, "open", open_with_swap)
    return state


def test_a_link_replaced_while_it_is_being_resolved_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirent swapped during the resolution does not move the mask.

    The link's target is read from the descriptor already held on that link, so
    replacing the directory entry mid-resolution cannot redirect it. The mask
    lands on the store the classified link pointed at, and the decoy never
    becomes a mount target. The pin itself does not refuse; the post-mount name
    check does, because the name now holds a link that does not reach the mask.
    """
    bed, store = _stow_bed(tmp_path)
    store_id, decoy_id = identity(store), identity(bed.decoy_dir)
    carried = _observed(bed.ssh)
    state = _swap_the_link_during_the_follow(monkeypatch, bed.ssh, bed.decoy_dir)

    ran = _run(tmp_path, bed=bed, occupants=carried)

    monkeypatch.undo()
    assert state["fired"], "the swap never ran, so this proved nothing"
    targets = ran.libc.target_ids()
    assert decoy_id not in targets, "the resolution followed the swapped entry to the decoy"
    assert store_id in targets, "the mask left the classified link's store"
    assert ran.refusal is not None, "the swapped link at the name was not caught"
    assert "cannot pin" not in ran.refusal, f"the supported layout refused: {ran.refusal}"
    # The new link reuses the old inode on some filesystems, so it is caught either
    # as a link the pin never saw or as a link that does not reach the stand-in.
    assert "planted" in ran.refusal or "does not reach its mask" in ran.refusal


def test_the_held_descriptor_keeps_answering_for_the_link_it_was_opened_on(
    tmp_path: Path,
) -> None:
    """Why the target is read through the descriptor rather than compared after.

    An earlier attempt bracketed the resolution with two no-follow reads and
    compared identity. Recreating a symlink was observed reusing inodes on this
    filesystem, which lets such a comparison pass across a real swap -- and the
    reuse is not reliable enough to test for, which is the point: a control that
    only sometimes holds is not a control. Reading through the held descriptor
    does not depend on identity at all, and that is asserted here.
    """
    target = tmp_path / "store"
    target.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)

    held = os.open(str(link), os.O_RDONLY | _PROBE_O_PATH | os.O_NOFOLLOW)
    try:
        before = os.readlink("", dir_fd=held)
        link.unlink()
        link.symlink_to(decoy)
        assert (
            os.readlink("", dir_fd=held) == before == str(target)
        ), "the held descriptor stopped answering for the link it was opened on"
        # The NAME now reaches the decoy, which is what makes the held read the
        # load-bearing part rather than a formality.
        assert os.path.realpath(str(link)) == str(decoy)
    finally:
        os.close(held)


#: ``O_PATH`` for this file's own direct syscall probes, resolved the same way the
#: launcher resolves it so the probes cannot disagree with what ships.
_PROBE_O_PATH = getattr(os, "O_PATH", 0)


# --------------------------------------------------------------------------
# Each half of the mechanism has its own catch
# --------------------------------------------------------------------------
#
# The two halves do different work. Carrying the identity is what catches a name
# whose occupant was REPLACED. Taking the first look WITHOUT following is what
# makes that comparison exact, and it is what catches a substitution a following
# look cannot see: a link aimed at the renamed original, whose resolved identity
# is unchanged. Both refusals come from the pin itself, before anything is mounted.


def _repoint_at_the_renamed_original(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Rename *victim* aside and leave a link to it at the old name.

    The substitution a FOLLOWING first look cannot see: the name now holds a
    link rather than the directory it held a moment ago, but that link resolves
    to the very same inode, so two following looks agree.
    """
    real_lexists = os.path.lexists

    def lexists_then_repoint(path):  # noqa: ANN001, ANN202
        answer = real_lexists(path)
        if answer and os.fsdecode(path) == str(victim) and not victim.is_symlink():
            moved = victim.parent / (victim.name + ".moved")
            victim.rename(moved)
            victim.symlink_to(moved)
        return answer

    monkeypatch.setattr(os.path, "lexists", lexists_then_repoint)


def test_control_the_shipped_source_refuses_both_substitutions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both substitutions refuse at the pin: a decoy link, and a link to the original.

    The decoy is caught by the carried identity, the same-object re-point by the
    no-follow first look; either way the pin names a DIFFERENT object, which is
    what tells its refusal apart from the post-mount name check's.
    """
    bed = _Bed(tmp_path)
    carried = _observed(bed.ssh)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)
    decoy_refusal = _run(tmp_path, bed=bed, occupants=carried).refusal
    assert decoy_refusal is not None, "the decoy substitution was not refused"
    assert "DIFFERENT object" in decoy_refusal

    monkeypatch.undo()
    other = tmp_path / "second"
    other.mkdir()
    bed2 = _Bed(other)
    carried2 = _observed(bed2.ssh)
    _repoint_at_the_renamed_original(monkeypatch, bed2.ssh)
    repoint_refusal = _run(other, bed=bed2, occupants=carried2).refusal
    assert bed2.ssh.is_symlink(), "the re-point never ran"
    assert repoint_refusal is not None, "the same-object re-point was not refused"
    assert "DIFFERENT object" in repoint_refusal


# --------------------------------------------------------------------------
# The carried map must reach the child as valid PYTHON, not merely valid JSON
# --------------------------------------------------------------------------
#
# The carried map is embedded in the launcher as source. ``json.dumps`` spells a
# bool ``true``/``false``, which Python does not define, so a populated map spelled
# that way kills the child with ``NameError`` before it mounts anything -- on every
# spawn. A test that only BUILDS the script text misses it, because the map is empty
# on a host with no crew leaves. These two close that gap: one evaluates the data
# as a literal, one reads the whole script for any name it uses without binding.


def _populated_launcher() -> str:
    """A launcher built with a map that actually has entries, and real bools."""
    return sandbox._build_launcher_script(
        "strict",
        mask_occupants={
            "/data/op/.kiro/crew/live_target.json": (66305, 12345, False),
            "/data/op/.ssh": (66305, 999, True),
        },
    )


def test_the_carried_map_is_valid_python_when_it_has_entries() -> None:
    """The emitted map must be a Python LITERAL, not merely valid JSON.

    ``ast.parse`` accepts ``true`` as a NAME, so parsing the script proves nothing
    here. ``literal_eval`` rejects it, which is the property that matters: the map
    is embedded as source, and a bool spelled ``true`` kills the child with
    ``NameError`` before it mounts anything.

    Deliberately NOT ``exec``: the repository's SAST gate flags it.
    """
    carried = rendered_payload(_populated_launcher())["mask_occupants"]
    assert isinstance(carried, dict) and carried, "the map came back empty"
    for ident in carried.values():
        assert len(ident) == 3, "an identity recorded without a kind must not be padded"
        assert isinstance(ident[2], int) and not isinstance(ident[2], bool), (
            "the link flag is a bool, which serialises as a name Python does not " "define"
        )
    assert bool(carried["/data/op/.ssh"][2]) is True
    assert bool(carried["/data/op/.kiro/crew/live_target.json"][2]) is False


def test_the_emitted_launcher_defines_every_name_it_reads() -> None:
    """No undefined global anywhere in the script, at any tier.

    Stated over the whole emitted source so the next datum embedded as JSON
    cannot reintroduce this by a different spelling.
    """
    for level in ("strict", "cc", "standard"):
        script = (
            _populated_launcher() if level == "strict" else sandbox._build_launcher_script(level)
        )
        tree = ast.parse(script)
        bound: set[str] = set()
        read: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound.add(alias.asname or alias.name)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    bound.add(node.id)
                else:
                    read.setdefault(node.id, node.lineno)
        undefined = {
            name: line
            for name, line in read.items()
            if name not in bound and name not in dir(builtins)
        }
        assert not undefined, f"{level} launcher reads undefined names: {undefined}"


def _recreate_with_distinct_inode(victim: Path, make) -> None:  # noqa: ANN001
    """Recreate *victim* via ``make`` so its identity is GUARANTEED to differ.

    ``rmdir``/``unlink`` frees an inode number, and several filesystems (some CI
    runners' overlay/tmpfs among them, though not the ext4 a developer usually
    runs on) hand that SAME number straight back to the next create at the path.
    A test that just removes and recreates then depends on inode allocation it
    does not control: on a recycling filesystem the recreation lands on the
    recorded ``(dev, ino)`` and reads as the ORIGINAL object, which is a real
    property of the mechanism -- an object indistinguishable by device and inode
    is indistinguishable, full stop -- but not the substitution the test means to
    exercise. So the freed inode is consumed by a throwaway placeholder before the
    real recreation, forcing a different number on every filesystem; the
    placeholder is then removed, leaving only *victim*.
    """
    parent = victim.parent
    if victim.is_symlink() or victim.exists():
        if victim.is_symlink() or not victim.is_dir():
            victim.unlink()
        else:
            victim.rmdir()
    placeholder = parent / (victim.name + ".__inode_hold__")
    placeholder.mkdir()
    try:
        make()
    finally:
        placeholder.rmdir()


def test_a_legitimately_recreated_target_is_rejected_with_no_exception(
    tmp_path: Path,
) -> None:
    """The strict comparison refuses a recreated target, and that is a POLICY GAP.

    This is not an assertion that refusing is right. It records what the
    device/inode comparison costs while it carries NO exceptions: a protected
    target recreated between the pass that observed it and the pin refuses the
    spawn, though nothing hostile happened.

    ``aws-control-staging`` is the measured case -- the namespace test lane failed
    on exactly this refusal. Which recreations a sandbox should permit is a
    threat-model decision, so no exception is invented here. This test exists so
    the cost is visible in the suite rather than discovered by a host.
    """
    bed = _Bed(tmp_path)
    staging = bed.cache / "aws-control-staging"
    staging.mkdir()
    carried = _observed(staging)

    # Best effort at a distinct inode; a recycling filesystem may still reuse it.
    _recreate_with_distinct_inode(staging, staging.mkdir)
    if _observed(staging) == carried:
        # The filesystem recycled the inode, so the recreation is indistinguishable
        # by (dev, ino, kind) -- there is no substitution for the pin to catch and
        # this row cannot be demonstrated here. That is the recycling-inode residual,
        # not a failure of the comparison.
        pytest.skip("filesystem recycled the inode; recreation is indistinguishable")

    bed.aws = staging
    ran = _run(tmp_path, bed=bed, occupants=carried, required=(str(staging),))

    assert ran.refusal is not None, (
        "the strict comparison stopped rejecting a recreated target; if that is "
        "deliberate, the exception belongs in the spec and this test should say so"
    )
    assert "DIFFERENT object" in ran.refusal


def test_every_inode_change_is_rejected_including_a_recreated_symlink(
    tmp_path: Path,
) -> None:
    """The full cost table, as the record for whoever sets the policy.

    Link-ness alone admitted a same-kind decoy, so the comparison is on device and
    inode. The consequence is that EVERY inode change refuses -- including a
    recreated symlink, which is what a dotfile manager does on a restow. Stated
    here rather than implied.
    """
    outcomes = {}
    for label, before, after in (
        ("real untouched", "real", "same"),
        ("real recreated", "real", "real"),
        ("real to link", "real", "link"),
        ("link untouched", "link", "same"),
        ("link recreated", "link", "link"),
        ("link to real", "link", "real"),
    ):
        root = tmp_path / label.replace(" ", "_")
        root.mkdir()
        bed = _Bed(root)
        store = root / "store"
        store.mkdir()
        victim = bed.cache / "leaf"

        if before == "link":
            victim.symlink_to(store)
        else:
            victim.mkdir()
        carried = _observed(victim)

        if after != "same":
            # Best effort at a distinct inode; a filesystem that recycles inode
            # numbers (some CI runners do) may still hand the recreation the same
            # (dev, ino) the pass recorded, and same-target-symlink recreation is
            # then genuinely indistinguishable by (dev, ino, kind).
            if after == "link":
                _recreate_with_distinct_inode(victim, lambda: victim.symlink_to(store))
            else:
                _recreate_with_distinct_inode(victim, victim.mkdir)

        # The launcher's invariant is exact: refuse IFF the object now at the name
        # differs by (dev, ino, kind) from what the pass carried. Assert against
        # what the recreation ACTUALLY produced rather than assuming an inode
        # always changes -- an object indistinguishable by device, inode and kind
        # is indistinguishable, and treating it as unchanged is the correct answer
        # on a recycling filesystem, not a miss.
        now = _observed(victim)
        changed = now[str(victim)] != carried[str(victim)]

        bed.aws = victim
        ran = _run(root, bed=bed, occupants=carried, required=(str(victim),))
        got = "refused" if ran.refusal else "proceeded"
        want = "refused" if changed else "proceeded"
        outcomes[label] = (got, want)

    assert all(got == want for got, want in outcomes.values()), outcomes


def test_the_carried_flag_is_an_int_at_every_recording_site() -> None:
    """One spelling wherever the flag is RECORDED, since the reader casts with bool.

    Scoped to the recording expressions, not to every ``S_ISLNK`` use: the alias
    pass also tests link-ness to decide whether to refuse, and that call is a
    predicate rather than a recorded value.
    """
    import inspect

    recorded = []
    for fn in (sandbox._refuse_aliased_masked_leaves, sandbox.namespace_argv):
        for line in inspect.getsource(fn).splitlines():
            stripped = line.strip()
            # A recorded value ends the tuple element with a comma; a predicate
            # ends its own statement with a colon.
            if "S_ISLNK" in stripped and stripped.endswith(","):
                recorded.append((fn.__name__, stripped))

    assert len(recorded) >= 3, f"expected three recording sites, found {recorded}"
    for name, line in recorded:
        assert line.startswith("int("), f"{name} records the link flag without int(): {line}"


def test_a_vanished_carried_symlink_target_refuses_rather_than_skips(tmp_path: Path) -> None:
    """A carried link whose referent has vanished fails closed, not open.

    The link-follow finds nothing when the referent is gone. With no
    ``require_present`` set, only the carried expectation the pass recorded
    distinguishes an ordinary absent optional from an established mask target: a
    symlinked ``.env`` whose dotfile-managed referent is mid-restow is such a
    target, and skipping it leaves the credential name unmasked and whatever is
    recreated there exposed. So the file mask refuses whenever a pass recorded an
    occupant for the name and saw it reach something.
    """
    store = tmp_path / "dotfiles" / "env"
    store.parent.mkdir()
    store.write_text("TOKEN=x\n")
    name = tmp_path / ".env"
    name.symlink_to(store)
    seen = os.lstat(name)
    # What the pass saw: a link (third element) that reached a regular file (fourth).
    carried = {str(name): [seen.st_dev, seen.st_ino, 1, 2]}
    store.rename(tmp_path / "env.mid-restow")
    libc = _Libc(tmp_path)
    run = launch(tmp_path, payload(sensitive_files=[str(name)], mask_occupants=carried), libc=libc)

    message = refusal(program.mask_sensitive_files, run)

    assert message is not None, (
        "a carried symlink whose referent vanished silently skipped its mask, so "
        "whatever is recreated at the name is exposed"
    )
    assert "has vanished" in message and str(name) in message
    assert libc.calls == []


# --------------------------------------------------------------------------
# The class invariant: no protected-link follow without a carried identity
# --------------------------------------------------------------------------
#
# A pin that listed the sites known to need the comparison would pass while a new
# one was written, so the invariant is stated over every site instead: each hiding
# stage pins through one function, that function looks the expectation up itself,
# and none of the stages passes it a keyword. A substituted name refuses at every
# site the same way.

_SITES = ("seal", "directory mask", "file mask", "ssh mask", "nested re-hide")


@pytest.mark.parametrize("site", _SITES)
def test_every_protected_pin_is_gated_by_a_carried_identity(tmp_path: Path, site: str) -> None:
    """Every stage that pins a protected target compares it with the carried identity.

    No stage asks for the comparison: the pin looks the expectation up for itself,
    so a stage written later is covered the day it is written. A protected name the
    pass saw holding a directory or file, swapped for a link to a decoy of the same
    kind, refuses at each site before the decoy can be mounted.
    """
    decoy = tmp_path / "decoy"
    fields: dict = {}
    if site == "file mask":
        target = tmp_path / "protected"
        target.write_text("secret\n")
        decoy.write_text("")
        fields["sensitive_files"] = [str(target)]
    elif site == "nested re-hide":
        crew, window, target = _window_bed(tmp_path)
        decoy.mkdir()
        fields.update(sensitive_dirs=[str(crew), str(target)], private_dirs=[str(window)])
    else:
        target = tmp_path / "protected"
        target.mkdir()
        (target / "id_ed25519").write_text("PRIVATE KEY\n")
        decoy.mkdir()
        if site == "seal":
            fields["readonly_dirs"] = [str(target)]
        elif site == "directory mask":
            fields["sensitive_dirs"] = [str(target)]
        else:
            fields.update(
                hide_ssh=1, ssh_dir=str(target), ssh_known_hosts=str(target / "known_hosts")
            )
    carried = _observed(target)
    decoy_id = identity(decoy)
    target.rename(target.parent / (target.name + ".moved"))
    target.symlink_to(decoy)
    libc = _Libc(tmp_path)
    run = launch(tmp_path, payload(mask_occupants=carried, **fields), libc=libc)

    message = refusal(program.place_masks, run)

    assert message is not None, f"the {site} followed a substituted link and ran on"
    assert "DIFFERENT object" in message and str(target) in message
    assert decoy_id not in libc.target_ids()


def test_the_carried_identity_reaches_a_site_that_never_asked_for_it(
    tmp_path: Path,
) -> None:
    """The finding's own site: a required FILE mask, which passes no keyword.

    The file mask calls the pin without an expectation. Under the seam it is
    covered anyway, which is what makes this a class fix rather than a single-site
    patch.

    The swap is planted between the observation and the run, with no hook inside
    the launcher, because that IS the window: the gateway records the identity,
    then the script is written, ``mkstemp`` runs and the child forks, and only
    then does the pin look. A racing writer has all of that to work in.
    """
    bed = _Bed(tmp_path)
    keystone = bed.cache / "live_target.json"
    keystone.write_text("{}\n")
    decoy = tmp_path / "decoy_keystone.json"
    decoy.write_text("attacker\n")
    decoy_id = identity(decoy)

    carried = _observed(keystone)  # what the pre-spawn pass saw: a regular file
    keystone.rename(keystone.parent / "live_target.json.moved")
    keystone.symlink_to(decoy)  # the racing writer, after that observation

    bed.secret = keystone  # the file mask entry this run masks
    ran = _run(tmp_path, bed=bed, occupants=carried, required=(str(keystone),))

    assert keystone.is_symlink(), "the substitution never ran, so this proved nothing"
    assert ran.refusal is not None, (
        "a required file mask followed a substituted link with no carried identity, "
        "leaving the keystone name writable"
    )
    assert decoy_id not in ran.libc.target_ids()


def test_that_same_site_still_masks_a_legitimately_linked_keystone(
    tmp_path: Path,
) -> None:
    """And it does not refuse the layout: the link was there when the pass looked.

    Same site, same seam, link present at the observation. The mask lands on what
    the link resolves to, which is the supported behaviour the refusal above must
    not cost.
    """
    bed = _Bed(tmp_path)
    store = tmp_path / "dotfiles_keystone.json"
    store.write_text("{}\n")
    store_id = identity(store)
    keystone = bed.cache / "live_target.json"
    keystone.symlink_to(store)

    carried = _observed(keystone)  # a LINK is what the pass saw

    bed.secret = keystone
    ran = _run(tmp_path, bed=bed, occupants=carried, required=(str(keystone),))

    assert ran.refusal is None, f"a legitimately linked keystone refused: {ran.refusal}"
    assert (
        store_id in ran.libc.target_ids()
    ), "the mask did not land on the store the link resolves to"
