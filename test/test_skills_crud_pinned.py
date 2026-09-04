"""Descriptor-pinned skill CRUD in ``SkillsLoader`` (create/update/delete).

``create_skill``/``update_skill``/``delete_skill`` address the skill directory and
its ``SKILL.md`` relative to a parent descriptor pinned by ``pinned_fs``, so an
ancestor swapped for a link after the by-name existence check cannot redirect the
write; ``update_skill`` additionally routes through ``atomic_write`` with the ACL
carry, closing the gap where a plain ``write_text`` dropped a named POSIX ACL.

NOT EXECUTED IN THE INTEGRATIONS_ONLY SANDBOX. Importing ``kiro_crew.skills`` pulls
``kiro_crew.cron`` -> ``croniter`` and ``kiro_crew.vector_memory`` ->
``snowballstemmer``, neither installable offline (pip 403), so these run in CI only.

CI invocation:

    python -m pytest test/test_skills_crud_pinned.py
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from kiro_crew import pinned_fs
from kiro_crew.skills import SkillsLoader

# Forcing ``_DIR_FD_SUPPORTED`` True takes the pinned branch, which reaches
# ``pinned_fs.pin_parent`` and therefore ``os.O_DIRECTORY`` -- absent on Windows,
# where the probe is False for exactly that reason. Derived from the probe rather
# than from ``sys.platform`` so the Windows-simulation tests that delete
# ``os.O_NOFOLLOW`` at runtime are covered too. Forcing it FALSE needs no guard:
# the by-name floor is what every platform can run.
needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(),
    reason="platform without a descriptor-relative directory walk",
)


@pytest.fixture()
def loader(tmp_path):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


def test_create_writes_skill_md_byte_exact(loader):
    body = "---\nname: demo\n---\n\n## Steps\ndo it\n"
    assert loader.create_skill("demo", body) is True
    written = (loader._dir / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert written == body


def test_create_lands_the_same_skill_md_mode_on_both_branches(loader, monkeypatch):
    """The pin must not change the permissions a new SKILL.md gets.

    The pinned create passes ``0o666`` to ``O_CREAT`` for exactly this reason: the
    by-name floor writes with ``write_text``, whose ``open(..., "w")`` is also
    ``0o666`` masked by umask. Asserting the two branches AGREE, rather than
    asserting a literal, is what makes this catch the failure that matters — a
    tightening (or loosening) that reaches one platform's branch and not the
    other's, so the same operator gets different skill permissions per platform.
    """
    import kiro_crew.skills as skills_mod

    assert loader.create_skill("modeA", "x") is True
    pinned_mode = stat.S_IMODE((loader._dir / "modeA" / "SKILL.md").stat().st_mode)

    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    assert loader.create_skill("modeB", "x") is True
    floor_mode = stat.S_IMODE((loader._dir / "modeB" / "SKILL.md").stat().st_mode)

    assert pinned_mode == floor_mode
    # And it really is the umask default rather than both branches being wrong in
    # the same direction, which an equality-only assertion would accept.
    umask = os.umask(0)
    os.umask(umask)
    assert pinned_mode == 0o666 & ~umask


def test_create_refuses_a_pre_existing_name(loader):
    assert loader.create_skill("dup", "first") is True
    assert loader.create_skill("dup", "second") is False
    # The original content is untouched by the refused second create.
    assert (loader._dir / "dup" / "SKILL.md").read_text(encoding="utf-8") == "first"


@pytest.mark.parametrize(
    "pinned", [pytest.param(True, marks=needs_pinned_walk), pytest.param(False)]
)
def test_a_create_that_loses_the_race_refuses_instead_of_overwriting(loader, monkeypatch, pinned):
    """A rival create winning the window refuses on BOTH branches, losing no body.

    ``create_skill``'s ``exists()`` guard is a by-name check with a window after
    it. If the loser then writes anyway, two concurrent POSTs both report success
    and one submitted body is silently gone -- and the 409 the endpoint documents
    never happens. The pinned branch refuses via
    ``create_and_open_dir_pinned(must_create=True)``; the by-name floor needs
    ``mkdir(exist_ok=False)`` to say the same thing, and both are asserted here
    because a fork whose two halves disagree on one request is the failure this
    migration must not introduce.

    The rival is injected INSIDE the window rather than raced: ``Path.exists`` is
    wrapped so that, for this skill's own directory name, it returns the real
    pre-race answer and only then lets the rival win.
    """
    import kiro_crew.skills as skills_mod

    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", pinned)
    real_exists = Path.exists
    rival_ran = {"done": False}

    def exists_then_lose_the_race(self, *args, **kwargs):
        answer = real_exists(self, *args, **kwargs)
        if not rival_ran["done"] and self.name == "racer":
            self.mkdir(parents=True, exist_ok=True)
            (self / "SKILL.md").write_text("RIVAL", encoding="utf-8")
            rival_ran["done"] = True
        return answer

    monkeypatch.setattr(Path, "exists", exists_then_lose_the_race)
    assert loader.create_skill("racer", "MINE") is False
    monkeypatch.undo()

    assert rival_ran["done"], "the rival never ran, so nothing was raced"
    # The rival's body is intact: the loser refused rather than overwriting.
    assert (loader._dir / "racer" / "SKILL.md").read_text(encoding="utf-8") == "RIVAL"


@needs_pinned_walk
def test_create_addresses_the_leaf_through_the_parent_it_already_pinned(loader, monkeypatch):
    """The create must not re-resolve the parent it was handed a descriptor for.

    ``create_skill`` pins ``skill_dir.parent`` once. Anything below it — the leaf
    directory, its ``SKILL.md``, and the rollback that removes both — must be
    addressed through THAT descriptor. Routing the leaf create through a helper
    that resolves ``skill_dir.parent`` again is a second chance for an ancestor
    swapped since the first resolution to be followed, and it also splits the
    create and the rollback across two different directories: the skill lands
    outside the skills root while the rollback reports an identity mismatch on an
    unrelated one.

    A NESTED name is used because that is where the parent is a directory an
    attacker can replace (``<skills>/group``) rather than the skills root itself.
    The swap is injected inside the window by wrapping the caller's own pin so it
    returns the genuine descriptor and only then moves the directory aside.
    """
    import kiro_crew.skills as skills_mod

    group = loader._dir / "group"
    group.mkdir(parents=True)
    decoy = loader._dir / "decoy"
    decoy.mkdir()

    real_pin = skills_mod.pinned_fs.open_dir_pinned

    def pin_then_swap(path, **kwargs):
        fd = real_pin(path, **kwargs)
        group.rename(loader._dir / "moved")
        decoy.rename(group)
        return fd

    monkeypatch.setattr(skills_mod.pinned_fs, "open_dir_pinned", pin_then_swap)
    assert loader.create_skill("group/nested", "MINE") is True
    monkeypatch.undo()

    # The skill landed in the directory that was PINNED, now reachable at its new
    # name -- never in the directory swapped onto the name afterwards.
    assert (loader._dir / "moved" / "nested" / "SKILL.md").read_text(encoding="utf-8") == "MINE"
    assert not (group / "nested").exists(), "the create followed the swapped ancestor"


@needs_pinned_walk
@pytest.mark.parametrize("victim", ["directory", "leaf"], ids=["dir-identity", "leaf-identity"])
def test_a_failed_identity_probe_still_leaves_nothing_behind(loader, monkeypatch, victim):
    """An ``fstat`` that fails must be rolled back, not escape leaving a half-made skill.

    The rollback verifies identity, so it needs one — and capturing it is itself a
    syscall that can fail (EIO, ESTALE on a network filesystem). If that failure is
    not inside the guarded region, the create escapes with its directory (and, for the
    leaf probe, its ``SKILL.md``) still on disk, and every retry answers 409 over a
    body nobody can replace. Both probes are exercised because they strand different
    amounts: the directory one also leaked the descriptor.

    The failure is injected ONCE. The leaf's identity is re-asked through the still-open
    descriptor precisely so a transient EIO on a network filesystem reaches the verified
    rollback arm rather than stranding the create; this pins that recovery.
    """
    real_fstat = os.fstat
    skill_dir = loader._dir / "probefail"
    failed_once: list[int] = []

    def fail_the_chosen_probe(fd):
        info = real_fstat(fd)
        if failed_once:
            return info
        if stat.S_ISDIR(info.st_mode) and victim == "directory":
            failed_once.append(fd)
            raise OSError(errno.EIO, "identity probe failed")
        if stat.S_ISREG(info.st_mode) and victim == "leaf":
            failed_once.append(fd)
            raise OSError(errno.EIO, "identity probe failed")
        return info

    monkeypatch.setattr(os, "fstat", fail_the_chosen_probe)
    with pytest.raises(OSError):
        loader.create_skill("probefail", "BODY")
    monkeypatch.setattr(os, "fstat", real_fstat)

    assert failed_once, "the identity probe was never exercised"
    assert not skill_dir.exists(), "the half-made skill survived a failed identity probe"
    assert [p.name for p in loader._dir.iterdir()] == []
    # And the retry is a create again rather than a permanent 409.
    assert loader.create_skill("probefail", "second try") is True
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "second try"


@needs_pinned_walk
def test_no_leaf_identity_at_all_leaves_an_empty_body_and_never_unlinks(loader, monkeypatch):
    """With BOTH leaf probes failing there is no identity, so nothing is unlinked by name.

    This is the one rollback arm that cannot reach a verified unlink, and the two
    demands on it are opposed: removing ``SKILL.md`` lets the directory removal finish
    and spares the caller a permanent 409, and not removing it spares a file this code
    has never read. Only one of those is a data loss, so the name stays.

    What it costs is bounded: the leaf's identity probe precedes the first ``os.write``,
    so with no identity nothing was written and the body left behind is EMPTY, never
    truncated. The skill is listed and both ``update_skill`` and ``delete_skill`` reach
    it, so the recovery is a save rather than a shell.
    """
    real_fstat = os.fstat
    real_unlink = os.unlink
    unlinked: list[str] = []
    skill_dir = loader._dir / "noidentity"

    def always_fail_the_leaf_probe(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            raise OSError(errno.EIO, "identity probe failed")
        return info

    def record_unlink(path, *args, **kwargs):
        unlinked.append(str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fstat", always_fail_the_leaf_probe)
    monkeypatch.setattr(os, "unlink", record_unlink)
    with pytest.raises(OSError):
        loader.create_skill("noidentity", "BODY")
    monkeypatch.setattr(os, "fstat", real_fstat)
    monkeypatch.setattr(os, "unlink", real_unlink)

    assert unlinked == [], f"the rollback unlinked a name with no identity to verify: {unlinked}"
    # The directory removal is refused because the empty leaf is still in it, and the
    # name is put back where a human -- and the API -- can find it.
    assert (skill_dir / "SKILL.md").read_bytes() == b""
    # Recovery is a save through the update path, not a shell.
    assert loader.update_skill("noidentity", "recovered") is True
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "recovered"


@needs_pinned_walk
def test_no_leaf_identity_at_all_spares_a_rivals_replacement(loader, monkeypatch):
    """The no-identity arm cannot reach a rival's file, because it unlinks nothing.

    A descriptor pins the DIRECTORY, not the entry inside it. With no captured inode
    there is nothing to verify a by-name unlink against, so a rival that took
    ``SKILL.md`` inside the failure window would lose its file to a best-effort
    ``os.unlink`` — a data loss in the arm that exists to prevent one.
    """
    real_fstat = os.fstat
    skill_dir = loader._dir / "rivalled-noidentity"

    def swap_the_leaf_then_fail_the_probe(fd):
        info = real_fstat(fd)
        if stat.S_ISREG(info.st_mode):
            # The rival replaces the name in the same instant the probe fails, so no
            # timing is involved.
            (skill_dir / "SKILL.md").unlink()
            (skill_dir / "SKILL.md").write_text("RIVAL", encoding="utf-8")
            raise OSError(errno.EIO, "identity probe failed")
        return info

    monkeypatch.setattr(os, "fstat", swap_the_leaf_then_fail_the_probe)
    with pytest.raises(OSError):
        loader.create_skill("rivalled-noidentity", "MINE")
    monkeypatch.setattr(os, "fstat", real_fstat)

    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "RIVAL"


@needs_pinned_walk
def test_the_rollback_does_not_delete_a_rivals_replacement_leaf(loader, monkeypatch):
    """The rollback removes the inode it created, not whatever answers to the name.

    The cleanup addresses ``SKILL.md`` under a descriptor, and a descriptor pins the
    DIRECTORY, not the name inside it. A rival that unlinks our ``O_EXCL`` leaf and
    creates its own inside the failure window would lose its file to a bare
    ``os.unlink``, which is a silent data loss in the arm that exists to prevent one.
    Verifying ``(st_dev, st_ino)`` turns "remove whatever holds this name" into
    "remove this object, or nothing".

    The rival is injected at the moment of failure — the ``os.write`` that fails is
    what swaps the leaf — so no timing is involved.
    """
    real_write = os.write
    skill_dir = loader._dir / "rivalled"

    def swap_the_leaf_then_fail(fd, data):
        if b"MINE" in bytes(data):
            # The rival replaces our leaf with its own inode, then our write fails.
            (skill_dir / "SKILL.md").unlink()
            (skill_dir / "SKILL.md").write_text("RIVAL", encoding="utf-8")
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", swap_the_leaf_then_fail)
    with pytest.raises(OSError):
        loader.create_skill("rivalled", "MINE")
    monkeypatch.undo()

    # The rival's file survived the rollback. The directory removal is refused for
    # the same reason -- it is no longer empty -- so the skill is left for a human
    # rather than either file being destroyed.
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "RIVAL"


@needs_pinned_walk
def test_a_failed_create_leaves_nothing_behind_so_a_retry_can_succeed(loader, monkeypatch):
    """A create that dies mid-body rolls back the leaf AND the directory it made.

    Without the rollback the half-made skill is permanent, not merely untidy: the
    leftover directory makes ``create_skill``'s ``exists()`` guard answer False on
    every retry, so the endpoint returns 409 "already exists" forever while
    ``list_skills()`` keeps serving the truncated body. The failure is injected at
    ``os.write`` — a short write is exactly the disk-full shape — after the
    directory and the ``O_EXCL`` leaf both exist, which is the only window where the
    two-step cleanup matters.
    """
    real_write = os.write

    def die_after_the_first_chunk(fd, data):
        if b"BODY" in bytes(data):
            raise OSError(errno.ENOSPC, "no space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", die_after_the_first_chunk)
    with pytest.raises(OSError):
        loader.create_skill("halfmade", "BODY")
    monkeypatch.undo()

    assert not (loader._dir / "halfmade").exists(), "the directory the create made survived"
    # No staging residue from remove_dir_verified either, so nothing is left for a
    # human to clean up on the ordinary failure path.
    assert [p.name for p in loader._dir.iterdir()] == []
    # And the retry is a CREATE again rather than a permanent 409.
    assert loader.create_skill("halfmade", "second try") is True
    assert (loader._dir / "halfmade" / "SKILL.md").read_text(encoding="utf-8") == "second try"


def test_update_replaces_content_and_returns_true(loader):
    assert loader.create_skill("edit", "before") is True
    assert loader.update_skill("edit", "after") is True
    assert (loader._dir / "edit" / "SKILL.md").read_text(encoding="utf-8") == "after"


def test_update_refuses_a_missing_skill(loader):
    assert loader.update_skill("nope", "x") is False


def test_update_carries_the_acl(loader, monkeypatch):
    """update_skill routes through the ACL carry, so the source xattrs are read.

    Monkeypatches the xattr syscalls so the assertion holds on any filesystem: the
    captured source ACL value must reach ``setxattr`` on the replacement inode.
    """
    if not all(hasattr(os, a) for a in ("listxattr", "getxattr", "setxattr")):
        pytest.skip("platform without xattr syscalls")
    assert loader.create_skill("acl", "before") is True

    monkeypatch.setattr(os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"acl-bytes", raising=False)
    recorded: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda fd, attr, value, *a, **k: recorded.append((attr, value)),
        raising=False,
    )

    assert loader.update_skill("acl", "after") is True
    monkeypatch.undo()
    assert ("system.posix_acl_access", b"acl-bytes") in recorded


def test_update_is_atomic_no_temp_residue(loader):
    assert loader.create_skill("atomic", "before") is True
    assert loader.update_skill("atomic", "after") is True
    names = sorted(p.name for p in (loader._dir / "atomic").iterdir())
    assert names == ["SKILL.md"]


def test_update_refuses_a_skill_md_swapped_to_a_symlink(loader, tmp_path):
    """A SKILL.md that is a symlink must not be written through.

    The pinned open uses ``O_NOFOLLOW`` and the by-name floor's ``atomic_write``
    replaces the link's directory entry rather than following it, so the link's
    target keeps its old bytes either way.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW required")
    assert loader.create_skill("linked", "seed") is True
    skill_md = loader._dir / "linked" / "SKILL.md"
    outside = tmp_path / "outside.txt"
    outside.write_text("protected", encoding="utf-8")
    skill_md.unlink()
    skill_md.symlink_to(outside)

    loader.update_skill("linked", "attacker body")
    # Whatever the outcome token, the file the link pointed at is not overwritten.
    assert outside.read_text(encoding="utf-8") == "protected"


@pytest.mark.parametrize(
    "pinned", [pytest.param(True, marks=needs_pinned_walk), pytest.param(False)]
)
def test_update_rejects_the_target_when_the_acl_source_open_fails(loader, monkeypatch, pinned):
    """A failed ACL-source open rejects the update; it does not write without it.

    ``open_access_control_source`` documents that its ``OSError`` propagates so
    the caller can treat the leaf as a rejected target, and both sibling update
    surfaces (steering, file-write) answer it with a not-found token. Swallowing
    it here would publish a fresh inode carrying permission BITS only, silently
    dropping a named POSIX ACL -- on the path whose whole purpose is to keep it.
    Pinned and by-name floor both, so neither branch regains the swallow.
    """
    import kiro_crew.skills as skills_mod

    assert loader.create_skill("acl-src", "before") is True
    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", pinned)

    seen: list[int | None] = []

    def refuse(_path, *, dir_fd=None):
        # Recorded, not ignored: on the pinned branch the source MUST be opened
        # relative to the walked descriptor, so a stub that silently accepted
        # ``dir_fd=None`` would keep passing after that regressed.
        seen.append(dir_fd)
        raise OSError(errno.ELOOP, "leaf swapped for a link")

    monkeypatch.setattr(skills_mod, "open_access_control_source", refuse)
    assert loader.update_skill("acl-src", "after") is False
    assert len(seen) == 1
    assert (seen[0] is not None) is pinned
    assert (loader._dir / "acl-src" / "SKILL.md").read_text(encoding="utf-8") == "before"
    # No temp file was staged and left behind by the refused update.
    assert sorted(p.name for p in (loader._dir / "acl-src").iterdir()) == ["SKILL.md"]


def test_delete_removes_the_skill(loader):
    assert loader.create_skill("gone", "x") is True
    assert loader.delete_skill("gone") is True
    assert not (loader._dir / "gone").exists()


def test_delete_refuses_a_symlinked_skill_dir(loader, tmp_path):
    """A skill directory that is a link is refused, not followed into an rmtree."""
    from kiro_crew.platform_compat import symlink_or_junction

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    loader._dir.mkdir(parents=True, exist_ok=True)
    link = loader._dir / "linkskill"
    symlink_or_junction(str(victim), str(link))

    assert loader.delete_skill("linkskill") is False
    # The link's target and its contents survive.
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


@needs_pinned_walk
def test_the_acl_source_is_opened_through_the_pinned_skill_directory(loader, monkeypatch):
    """A directory replaced at the skill's name cannot supply the replacement's mode.

    Pinning the skill directory buys nothing if ``SKILL.md`` is then addressed BY
    NAME again, and the mode/ACL read is a leaf reference like any other. With the
    source opened by name, a directory swapped in at the skill's name between the
    pin and that open supplies the mode and the ACL, while ``atomic_write`` still
    publishes through the pinned descriptor into the ORIGINAL directory -- so the
    real skill is handed back carrying permissions chosen by whoever did the swap.

    The swap is injected exactly in that window by wrapping ``open_dir_pinned``,
    which returns the genuine descriptor and only then renames the directories, so
    the production code is untouched and the race is deterministic.
    """
    import kiro_crew.skills as skills_mod

    assert loader.create_skill("swap", "before") is True
    original = loader._dir / "swap"
    os.chmod(original / "SKILL.md", 0o600)

    # The decoy's mode only has to DIFFER from the original's 0o600 for the
    # assertion below to tell which inode the mode came from, so it is the
    # ordinary 0o644 rather than something world-writable a SAST rule would flag.
    decoy = loader._dir / "decoy"
    decoy.mkdir()
    (decoy / "SKILL.md").write_text("decoy", encoding="utf-8")
    os.chmod(decoy / "SKILL.md", 0o644)

    real_pin = skills_mod.pinned_fs.open_dir_pinned

    def pin_then_swap(path, **kwargs):
        fd = real_pin(path, **kwargs)
        original.rename(loader._dir / "moved")
        decoy.rename(original)
        return fd

    monkeypatch.setattr(skills_mod.pinned_fs, "open_dir_pinned", pin_then_swap)

    assert loader.update_skill("swap", "after") is True

    published = loader._dir / "moved" / "SKILL.md"
    assert published.read_text(encoding="utf-8") == "after"
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    # The decoy is untouched: it was never the write's destination.
    assert (original / "SKILL.md").read_text(encoding="utf-8") == "decoy"


def test_update_degrades_to_the_floor_when_the_publish_cannot_be_pinned(loader, monkeypatch):
    """Without the descriptor-relative rename, update passes None rather than a fd.

    ``atomic_write`` REFUSES a ``parent_dir_fd`` it cannot publish through, so
    gating on the pinned WALK alone would make every skill edit raise on a platform
    where the two capabilities disagree instead of taking the by-name floor.

    The disagreement is emulated by removing ``os.rename`` from
    ``os.supports_dir_fd`` -- the platform FACT the probe reads -- not by patching a
    module's binding of the probe, which would convince only one of the two callers
    of it. ``os.open`` stays a member so the pinned walk is still supported, which
    is what makes the two probes genuinely disagree.
    """
    assert loader.create_skill("floorpub", "one") is True
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd - {os.rename})
    assert loader.update_skill("floorpub", "two") is True
    assert (loader._dir / "floorpub" / "SKILL.md").read_text(encoding="utf-8") == "two"


def test_by_name_floor_when_capability_probe_is_false(loader, monkeypatch):
    """With the probe forced False, create/update/delete use the by-name path.

    Pins that the by-name floor still produces correct results, so the platform
    without openat (Windows) keeps working.
    """
    import kiro_crew.skills as skills_mod

    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    assert loader.create_skill("floor", "one") is True
    assert (loader._dir / "floor" / "SKILL.md").read_text(encoding="utf-8") == "one"
    assert loader.update_skill("floor", "two") is True
    assert (loader._dir / "floor" / "SKILL.md").read_text(encoding="utf-8") == "two"
    assert loader.delete_skill("floor") is True
    assert not (loader._dir / "floor").exists()
