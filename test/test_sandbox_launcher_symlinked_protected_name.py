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
try: it carries the identity of what occupied the name at its FIRST look and
refuses when a later look finds a different occupant. A link that was already
there is the same link at both looks and passes; a directory replaced by a link
is not, and refuses.

The ``O_DIRECTORY | O_NOFOLLOW`` shape a no-follow fix reaches for first is
measured here as the thing that breaks the supported layout, so the refusal it
would cause cannot creep back in unnoticed.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import os
import re
import stat
import sys
from pathlib import Path

import pytest
from test_sandbox_mount_pinned_target import (
    _Bed,
    _identity,
    _region,
    _run,
)

from kiro_crew.sandbox import _build_launcher_script

# Same ground as the sibling pinned-mount suite: the launcher runs on Linux only
# and addresses its pinned targets through ``/proc/self/fd/<fd>``, which Darwin
# does not have, so off Linux every recorded target resolves to nothing.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")


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
    ``_refuse_aliased_masked_leaves`` records and the builder serialises. Built
    here so a test states the identity the gateway SAW rather than letting the
    launcher take its own look, which is the distinction under test.
    """
    carried: dict[str, list[int]] = {}
    for path in paths:
        info = os.lstat(str(path))
        carried[str(path)] = [info.st_dev, info.st_ino, int(stat.S_ISLNK(info.st_mode))]
    return carried


def _staged_with_known_hosts(libc) -> list[Path]:  # noqa: ANN001
    """Stand-in sources this run staged host trust into."""
    found = []
    for call in libc.calls:
        if not isinstance(call.source, (str, bytes)):
            continue
        source = Path(os.fsdecode(call.source))
        if (source / "known_hosts").is_file():
            found.append(source)
    return found


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

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=_observed(bed.ssh))

    assert refusal is None, f"a symlinked ~/.ssh refused the spawn: {refusal}"
    assert _identity(store) is not None


def test_the_key_store_behind_the_symlink_is_the_object_masked(tmp_path: Path) -> None:
    """Following the link once is what puts the mask over the real keys.

    A mask that stopped at the link would cover nothing, and the private key in
    the store would stay readable inside the sandbox.
    """
    bed, store = _stow_bed(tmp_path)

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=_observed(bed.ssh))

    assert refusal is None
    assert _identity(store) in [call.target_id for call in libc.calls], (
        "no mount landed on the store the symlink resolves to, so the keys "
        "behind it were never masked"
    )


def test_host_trust_is_still_carried_across_a_symlinked_name(tmp_path: Path) -> None:
    """``known_hosts`` read through the link reaches the store's copy.

    Losing it would point ``UserKnownHostsFile`` at an absent file while
    ``accept-new`` is still on, so every host would read as new.
    """
    bed, _ = _stow_bed(tmp_path)

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=_observed(bed.ssh))

    assert refusal is None
    staged = _staged_with_known_hosts(libc)
    assert staged, "no stand-in carried host trust, so verification was dropped"
    assert (staged[0] / "known_hosts").read_text() == "example.com ssh-rsa AAAA\n"


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

    ``os.path.isdir`` is the launcher's own guard on the ssh name and follows
    links, so it answers True both before and after the swap -- which is exactly
    why the guard alone cannot see this happen.
    """
    real_isdir = os.path.isdir

    def isdir_then_substitute(path):  # noqa: ANN001, ANN202
        answer = real_isdir(path)
        if answer and os.fsdecode(path) == str(victim) and not victim.is_symlink():
            victim.rename(victim.parent / (victim.name + ".moved"))
            victim.symlink_to(decoy)
        return answer

    monkeypatch.setattr(os.path, "isdir", isdir_then_substitute)


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
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink(), "the substitution never ran, so this proved nothing"
    assert refusal is not None, (
        "the launcher masked a decoy the planter chose and ran on, leaving the "
        "renamed key directory readable"
    )
    assert _identity(bed.decoy_dir) not in [call.target_id for call in libc.calls]


def test_a_symlink_that_was_always_there_is_not_treated_as_a_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminator is a CHANGE of occupant, not the presence of a link.

    Same hook as the substitution above, firing on a name that is already a
    link. Nothing is swapped, so the spawn proceeds -- this is what keeps the
    supported layout working while the substitution refuses.
    """
    bed, store = _stow_bed(tmp_path)
    carried = _observed(bed.ssh)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink()
    assert refusal is None, f"an untouched symlinked name refused: {refusal}"
    assert _identity(store) in [call.target_id for call in libc.calls]


# --------------------------------------------------------------------------
# Enumerated by condition, not by a list of sites
# --------------------------------------------------------------------------

#: The launcher's protected-name resolutions, found by what they DO rather than
#: by where they are: an ``os.open`` of a caller-supplied protected target. A new
#: site added without a no-follow first look is caught by this, which a hand-kept
#: list of line numbers would not be.
_OPEN_CALL = re.compile(r"os\.open\(\s*([^,]+),\s*([^\n]*?)\)", re.S)


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
    without anyone maintaining a list of sites.
    """
    script = _build_launcher_script("strict")

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


# --------------------------------------------------------------------------
# The swap ACROSS the follow, which a by-name reopen would miss
# --------------------------------------------------------------------------


def _swap_the_link_during_the_follow(
    monkeypatch: pytest.MonkeyPatch, victim: Path, decoy: Path
) -> dict:
    """Replace an existing link at *victim* while its target is being resolved.

    The launcher looks at the leaf three times relative to its held parent: a
    no-follow first look, the single follow, and a no-follow read-back. This
    lands the swap between the first two, which is the window a reopen of the
    whole name would leave open and the read-back closes.
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
    becomes a mount target.
    """
    bed, store = _stow_bed(tmp_path)
    carried = _observed(bed.ssh)
    state = _swap_the_link_during_the_follow(monkeypatch, bed.ssh, bed.decoy_dir)

    libc, _, refusal = _run(tmp_path, bed=bed, occupants=carried)

    monkeypatch.undo()
    assert state["fired"], "the swap never ran, so this proved nothing"
    assert refusal is None, f"the supported layout refused: {refusal}"
    targets = [call.target_id for call in libc.calls]
    assert (
        _identity(bed.decoy_dir) not in targets
    ), "the resolution followed the swapped entry to the decoy"
    assert _identity(store) in targets, "the mask left the classified link's store"


def test_mutation_resolving_the_name_again_loses_the_in_flight_catch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve the name a second time and the swapped entry wins.

    This is the shape the held-descriptor read replaces: a fresh whole-path
    lookup placed after the occupant comparison, which that comparison cannot
    cover.
    """
    script = _build_launcher_script("strict")
    assert _HELD_READ in script, "the held-descriptor link read is not shipped"
    mutant = script.replace(_HELD_READ, _NAME_REREAD)
    _assert_mutated(script, mutant, _HELD_READ)

    bed, _ = _stow_bed(tmp_path)
    carried = _observed(bed.ssh)
    state = _swap_the_link_during_the_follow(monkeypatch, bed.ssh, bed.decoy_dir)

    libc, _, refusal = _run(tmp_path, script=mutant, bed=bed, occupants=carried)

    monkeypatch.undo()
    assert state["fired"], "the swap never ran"
    # The mutant's own answer: it resolves the swapped entry and masks the decoy.
    assert refusal is None
    assert _identity(bed.decoy_dir) in [call.target_id for call in libc.calls]


def test_the_name_is_never_resolved_as_a_whole_path_twice() -> None:
    """Stated over the source rather than left to review.

    The parent is held, the first look is relative to it, and the link's target
    comes from the descriptor open on that link. A second whole-path lookup of
    the protected name is what bypasses the occupant comparison.
    """
    script = _build_launcher_script("strict")
    assert "parent_fd = os.open(" in script, "the parent is not held"
    assert _HELD_READ in script, "the link target is not read from its descriptor"
    assert (
        _NAME_REREAD not in script
    ), "the protected name is resolved a second time as a whole path"


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


# --------------------------------------------------------------------------
# Mutation: each half of the mechanism has its own nail
# --------------------------------------------------------------------------
#
# The two halves do different work and a single mutation cannot falsify both.
# Carrying the identity is what catches a name whose occupant was REPLACED.
# Taking the first look WITHOUT following is what makes that comparison exact,
# and it is falsified by a substitution the following form cannot see: a link
# aimed at the renamed original, whose resolved identity is unchanged.

#: The carried-identity comparison, and the no-follow first look. Each is
#: reverted on its own below.
_CARRIED = "if _replaced:"
_HELD = "os.O_RDONLY | _O_PATH | os.O_NOFOLLOW"
_FOLLOWING = "os.O_RDONLY | _O_PATH"

#: The follow's source of truth: the link's own content, read from the descriptor
#: already open on it, and the whole-path re-read that would replace it. A second
#: lookup of the protected name is what the occupant comparison cannot cover.
_HELD_READ = 'os.readlink("", dir_fd=name_fd)'
_NAME_REREAD = "os.readlink(_t)"

#: ``O_PATH`` for this file's own direct syscall probes, resolved the same way the
#: launcher resolves it so the probes cannot disagree with what ships.
_PROBE_O_PATH = getattr(os, "O_PATH", 0)


def _assert_mutated(script: str, mutant: str, gone: str) -> None:
    """Prove the mutation reached the text, so a no-op cannot score as a catch."""
    assert (
        hashlib.sha256(mutant.encode()).hexdigest() != hashlib.sha256(script.encode()).hexdigest()
    ), "the mutation did not change the launcher text"
    assert gone not in mutant, "the mutation left the mutated form behind"


def _repoint_at_the_renamed_original(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Rename *victim* aside and leave a link to it at the old name.

    The substitution a FOLLOWING first look cannot see: the name now holds a
    link rather than the directory it held a moment ago, but that link resolves
    to the very same inode, so two following looks agree.
    """
    real_isdir = os.path.isdir

    def isdir_then_repoint(path):  # noqa: ANN001, ANN202
        answer = real_isdir(path)
        if answer and os.fsdecode(path) == str(victim) and not victim.is_symlink():
            moved = victim.parent / (victim.name + ".moved")
            victim.rename(moved)
            victim.symlink_to(moved)
        return answer

    monkeypatch.setattr(os.path, "isdir", isdir_then_repoint)


def test_control_the_shipped_source_refuses_both_substitutions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control arm for both mutations: unmutated, each one refuses.

    Without this a mutation that failed to apply would score as caught and the
    mutation tests below would pass while proving nothing.
    """
    script = _build_launcher_script("strict")

    bed = _Bed(tmp_path)
    carried = _observed(bed.ssh)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)
    _, _, decoy_refusal = _run(tmp_path, script=script, bed=bed, occupants=carried)
    assert decoy_refusal is not None, "the decoy substitution was not refused"

    monkeypatch.undo()
    other = tmp_path / "second"
    other.mkdir()
    bed2 = _Bed(other)
    carried2 = _observed(bed2.ssh)
    _repoint_at_the_renamed_original(monkeypatch, bed2.ssh)
    _, _, repoint_refusal = _run(other, script=script, bed=bed2, occupants=carried2)
    assert repoint_refusal is not None, "the same-object re-point was not refused"


def test_mutation_dropping_the_carried_identity_loses_the_decoy_catch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revert the comparison and the planter's decoy is masked again.

    This is the half that closes the finding: without it the launcher follows
    whatever the name points at by then, binds the mask over the decoy, and the
    post-mount name check agrees because it follows the same link.
    """
    script = _build_launcher_script("strict")
    assert _CARRIED in script, "the carried-identity comparison is not shipped"
    mutant = script.replace(_CARRIED, "if False:")
    _assert_mutated(script, mutant, _CARRIED)

    bed = _Bed(tmp_path)
    carried = _observed(bed.ssh)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)

    libc, _, refusal = _run(tmp_path, script=mutant, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink(), "the substitution never ran"
    # The mutant's own answer: it runs ON, having masked the decoy the planter
    # chose, leaving the renamed key directory readable.
    assert refusal is None
    assert _identity(bed.decoy_dir) in [call.target_id for call in libc.calls]


def test_mutation_reverting_the_first_look_loses_the_same_object_catch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revert the first look to a following one and the re-point goes unseen.

    Both looks then resolve to the same inode, so the comparison cannot tell
    that a link took the directory's place at the name -- which is what the
    no-follow first look is for.
    """
    script = _build_launcher_script("strict")
    assert _HELD in script, "the no-follow first look is not shipped"
    mutant = script.replace(_HELD, _FOLLOWING)
    _assert_mutated(script, mutant, _HELD)

    bed = _Bed(tmp_path)
    carried = _observed(bed.ssh)
    _repoint_at_the_renamed_original(monkeypatch, bed.ssh)

    _, _, refusal = _run(tmp_path, script=mutant, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink(), "the re-point never ran"
    assert refusal is None, "the following form refused, so this mutation does not discriminate"


def test_the_two_halves_fail_the_supported_layout_differently(
    tmp_path: Path,
) -> None:
    """Which mutant the layout can tell apart, under the strict comparison.

    Dropping the comparison is LENIENT: it removes a refusal, so a symlinked name
    still boots and only the substitution tests can see the loss.

    Reverting the first look to a following one is STRICT: the carried identity was
    recorded no-follow, so a following look reports the link's TARGET inode and
    disagrees with it on an ordinary machine. That makes the no-follow first look
    load-bearing for the supported layout as well as for the catch.
    """
    script = _build_launcher_script("strict")

    lenient = tmp_path / "dropped_comparison"
    lenient.mkdir()
    bed, store = _stow_bed(lenient)
    libc, _, refusal = _run(
        lenient,
        script=script.replace(_CARRIED, "if False:"),
        bed=bed,
        occupants=_observed(bed.ssh),
    )
    assert refusal is None, f"the dropped-comparison mutant refused the layout: {refusal}"
    assert _identity(store) in [call.target_id for call in libc.calls]

    following = tmp_path / "following_first_look"
    following.mkdir()
    bed2, _ = _stow_bed(following)
    _, _, refusal2 = _run(
        following,
        script=script.replace(_HELD, _FOLLOWING),
        bed=bed2,
        occupants=_observed(bed2.ssh),
    )
    assert refusal2 is not None, (
        "a following first look matched a no-follow observation, so the two "
        "spellings are interchangeable and the shipped one is not load-bearing"
    )


# --------------------------------------------------------------------------
# The emitted script must be valid PYTHON, not merely valid JSON
# --------------------------------------------------------------------------
#
# The carried map is embedded in the launcher as source. ``json.dumps`` spells a
# bool ``true``/``false``, which Python does not define, so a populated map made
# the child die with ``NameError`` before it mounted anything -- on every spawn.
# Every test that only BUILDS the script text missed it, because the map is empty
# on a host with no crew leaves; the namespace and e2e lanes caught it. These two
# close that gap: one executes the data, one reads the whole script for any name
# it uses without binding.


def _populated_launcher() -> str:
    """A launcher built with a map that actually has entries, and real bools."""
    return _build_launcher_script(
        "strict",
        mask_occupants={
            "/home/u/.kiro/crew/live_target.json": (66305, 12345, False),
            "/home/u/.ssh": (66305, 999, True),
        },
    )


def test_the_carried_map_is_valid_python_when_it_has_entries() -> None:
    """The emitted map must be a Python LITERAL, not merely valid JSON.

    ``ast.parse`` accepts ``true`` as a NAME, so parsing the script proves nothing
    here. ``literal_eval`` rejects it, which is the property that failed: the map
    is embedded as source, and a bool spelled ``true`` killed the child with
    ``NameError`` before it mounted anything.

    Deliberately NOT ``exec``: the repository's SAST gate flags it, and the
    sibling harness already avoids it for that reason.
    """
    script = _populated_launcher()
    assignment = [line for line in script.splitlines() if line.startswith("MASK_OCCUPANTS")]
    assert assignment, "the carried map is not emitted"

    carried = ast.literal_eval(assignment[0].split("=", 1)[1].strip())
    assert isinstance(carried, dict) and carried, "the map came back empty"
    for ident in carried.values():
        assert len(ident) == 3
        assert isinstance(ident[2], int) and not isinstance(ident[2], bool), (
            "the link flag is a bool, which serialises as a name Python does not " "define"
        )
    assert bool(carried["/home/u/.ssh"][2]) is True
    assert bool(carried["/home/u/.kiro/crew/live_target.json"][2]) is False


def test_the_emitted_launcher_defines_every_name_it_reads() -> None:
    """No undefined global anywhere in the script, at any tier.

    Stated over the whole emitted source so the next datum embedded as JSON
    cannot reintroduce this by a different spelling.
    """
    for level in ("strict", "cc", "standard"):
        script = _populated_launcher() if level == "strict" else _build_launcher_script(level)
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

    staging.rmdir()
    staging.mkdir()  # ordinary recreation: same name, same KIND, new inode
    assert _observed(staging) != carried, "the recreation did not change the inode"

    bed.aws = staging
    _, _, refusal = _run(tmp_path, bed=bed, occupants=carried, required=(str(staging),))

    assert refusal is not None, (
        "the strict comparison stopped rejecting a recreated target; if that is "
        "deliberate, the exception belongs in the spec and this test should say so"
    )
    assert "DIFFERENT object" in refusal


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
            if victim.is_symlink():
                victim.unlink()
            else:
                victim.rmdir()
            if after == "link":
                victim.symlink_to(store)
            else:
                victim.mkdir()

        bed.aws = victim
        _, _, refusal = _run(root, bed=bed, occupants=carried, required=(str(victim),))
        outcomes[label] = "refused" if refusal else "proceeded"

    assert outcomes == {
        "real untouched": "proceeded",
        "real recreated": "refused",
        "real to link": "refused",
        "link untouched": "proceeded",
        "link recreated": "refused",
        "link to real": "refused",
    }, outcomes


def test_the_comparison_sits_below_the_kind_check() -> None:
    """A loop meeting an object of the other kind must SKIP, not meet a refusal.

    Every caller-supplied path is offered to both the directory loop and the file
    loop. With the comparison above the kind check, the loop that was never meant
    to mask an object refused on it instead of skipping.
    """
    script = _build_launcher_script("strict")
    body = _pin_body(script)
    kind_check = "if not matched:"
    assert kind_check in body, "the kind check is gone"
    assert _CARRIED in body, "the occupant comparison is gone"
    assert body.index(kind_check) < body.index(_CARRIED), (
        "the occupant comparison runs before the kind check, so a wrong-kind "
        "object refuses where it used to skip"
    )


def test_the_carried_flag_is_an_int_at_every_recording_site() -> None:
    """One spelling wherever the flag is RECORDED, since the reader casts with bool.

    Scoped to the recording expressions, not to every ``S_ISLNK`` use: the alias
    pass also tests link-ness to decide whether to refuse, and that call is a
    predicate rather than a recorded value.
    """
    import inspect

    from kiro_crew import sandbox

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


def test_the_region_harness_still_covers_the_ssh_site() -> None:
    """A slice that lost the ssh block would make every assertion here vacuous."""
    region = _region(_build_launcher_script("strict"))
    assert "if HIDE_SSH and" in region
    assert "_pin_mount_path(" in region


# --------------------------------------------------------------------------
# The class invariant: no protected-link follow without a carried identity
# --------------------------------------------------------------------------
#
# Three findings on this PR were the same defect at three sites. A pin that
# listed those three sites would pass while a fourth was written, so the
# invariant is stated over the SEAM instead: every hiding mount reaches one
# function, that function looks the expectation up itself, and the follow is
# downstream of the comparison. A new call site inherits all of it, and a site
# that somehow resolves a protected name outside the seam FAILS here rather than
# being silently uncovered.

#: Every launcher call that pins a protected target.
_PIN_CALL = re.compile(r"_pin_mount_path\(", re.S)
#: The lookup, the comparison, and the follow, in the order they must appear.
_LOOKUP = "expect_occupant = _carried_occupant(target)"


def _pin_body(script: str) -> str:
    """The shipped ``_pin_mount_path`` source, sliced out of the launcher."""
    start = script.index("def _pin_mount_path(")
    end = script.index("def _mask_required(", start)
    return script[start:end]


def test_every_protected_pin_is_gated_by_a_carried_identity() -> None:
    """Enumerated by condition over the seam, not by a list of sites.

    The count of call sites is deliberately NOT asserted: a new one is supposed
    to be free to appear, and the point of this test is that it arrives already
    covered.
    """
    script = _build_launcher_script("strict")
    body = _pin_body(script)

    sites = len(_PIN_CALL.findall(script)) - 1  # minus the definition itself
    assert sites >= 6, f"only {sites} pin call sites found; the matcher has drifted"

    assert _LOOKUP in body, (
        "the pin does not look the carried expectation up itself, so covering a "
        "call site depends on that site remembering a keyword"
    )
    assert body.index(_LOOKUP) < body.index(
        _CARRIED
    ), "the lookup runs after the comparison, so it cannot inform it"
    # The comparison sits BELOW the follow and the kind check on purpose: a
    # wrong-kind object must skip, and the follow only opens a descriptor. What
    # matters is that nothing mountable is handed BACK to a caller until the
    # comparison has run.
    assert body.index(_CARRIED) < body.index(
        'return fd, ("/proc/self/fd/%d"'
    ), "the pin returns a mountable path before the occupant comparison has run"
    # No call site may resolve a protected name for itself, outside the seam.
    outside = [
        line.strip()
        for line in script.splitlines()
        if "os.open(" in line
        and "_O_PATH" in line
        and "_leaf" not in line
        and "_link_to" not in line
        and "parent_fd" not in line
    ]
    assert not outside, f"a protected name is resolved outside the pin: {outside}"


def test_mutation_bypassing_the_seam_at_one_site_goes_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reintroduce a bare follow at ONE site and the invariant fails.

    The mutation removes the lookup, which is what every site depends on, so the
    site-level catch disappears even though each call site is untouched. That is
    the shape of the three findings this PR answered one at a time.
    """
    script = _build_launcher_script("strict")
    assert _LOOKUP in script, "the seam's lookup is not shipped"
    mutant = script.replace(_LOOKUP, "expect_occupant = None")
    _assert_mutated(script, mutant, _LOOKUP)

    # The invariant pin's own assertion moves.
    assert _LOOKUP not in _pin_body(mutant)

    # And the behaviour it protects is gone: the substitution is masked again.
    bed = _Bed(tmp_path)
    carried = _observed(bed.ssh)
    _substitute_link_at(monkeypatch, bed.ssh, bed.decoy_dir)
    libc, _, refusal = _run(tmp_path, script=mutant, bed=bed, occupants=carried)

    assert bed.ssh.is_symlink(), "the substitution never ran"
    assert refusal is None
    assert _identity(bed.decoy_dir) in [call.target_id for call in libc.calls]


def test_the_carried_identity_reaches_a_site_that_never_asked_for_it(
    tmp_path: Path,
) -> None:
    """The finding's own site: a required FILE mask, which passes no keyword.

    ``SENSITIVE_FILES`` calls the pin without an expectation. Under the seam it is
    covered anyway, which is what makes this a class fix rather than a third
    single-site patch.

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

    carried = _observed(keystone)  # what the pre-spawn pass saw: a regular file
    keystone.rename(keystone.parent / "live_target.json.moved")
    keystone.symlink_to(decoy)  # the racing writer, after that observation

    bed.secret = keystone  # the SENSITIVE_FILES entry this run masks
    libc, _, refusal = _run(tmp_path, bed=bed, occupants=carried, required=(str(keystone),))

    assert keystone.is_symlink(), "the substitution never ran, so this proved nothing"
    assert refusal is not None, (
        "a required file mask followed a substituted link with no carried identity, "
        "leaving the keystone name writable"
    )
    assert _identity(decoy) not in [call.target_id for call in libc.calls]


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
    keystone = bed.cache / "live_target.json"
    keystone.symlink_to(store)

    carried = _observed(keystone)  # a LINK is what the pass saw

    bed.secret = keystone
    libc, _, refusal = _run(tmp_path, bed=bed, occupants=carried, required=(str(keystone),))

    assert refusal is None, f"a legitimately linked keystone refused: {refusal}"
    assert _identity(store) in [
        call.target_id for call in libc.calls
    ], "the mask did not land on the store the link resolves to"
