"""Writing skills: create, update, and the ``pinned`` / ``inject_on_trigger`` toggles.

Create and update address the skill directory through a descriptor pinning its
parent chain where the platform supports it, and keep the by-name floor
elsewhere. Create rolls back everything it made on failure, verifying identity
first. ``delete_skill`` stays in the facade, where the link-screen baseline pins it.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger("kiro_crew.skills")


def create_skill(loader: SkillsLoader, name: str, content: str) -> bool:
    """Create a new skill directory with SKILL.md.  Returns True on success."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader._safe_name(name):
        return False
    skill_dir = loader._dir / name
    if skill_dir.exists():
        return False
    if not sk._DIR_FD_SUPPORTED:
        # exist_ok=False so a skill directory that appeared between the
        # exists() check above and here is REFUSED rather than written
        # through: two concurrent creates would otherwise both mkdir, both
        # write_text the same SKILL.md, and both report success, losing one
        # submitted body. The pinned branch answers the same way, through
        # its own O_EXCL-equivalent -- os.mkdir under the pinned parent raising
        # FileExistsError -- so without this the two branches of this fork
        # disagree on the same request. parents=True
        # still creates the intermediates a nested name needs; only the leaf
        # is refused.
        try:
            skill_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return False
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        loader._invalidate_iter_cache()  # so the new skill shows in list_skills() now
        logger.info("Created skill: %s", name)
        return True

    # Ensure the intermediate tree by name (a nested skill name has parents
    # the caller owns), then create the leaf skill dir and its SKILL.md
    # relative to a pinned descriptor so an ancestor swapped for a link after
    # the exists() check cannot redirect the write. The leaf mkdir refuses a
    # skill dir that appeared in the meantime, matching the exists() guard.
    skill_dir.parent.mkdir(parents=True, exist_ok=True)
    # ONE resolution of the parent chain, and everything below it addressed
    # through the descriptor it produced: the leaf directory, its SKILL.md, and
    # the rollback that removes both. A second walk would be a second chance for
    # an ancestor swapped since the first to be followed, and would also leave
    # the create and the rollback pointing at different directories.
    try:
        parent_fd = sk.pinned_fs.open_dir_pinned(skill_dir.parent, what="skill directory")
    except sk.pinned_fs.PinnedPathRefusal:
        return False
    except OSError:
        return False
    try:
        return loader._create_skill_pinned(name, content, skill_dir, parent_fd)
    finally:
        os.close(parent_fd)


def _create_skill_pinned(
    loader: SkillsLoader, name: str, content: str, skill_dir: Path, parent_fd: int
) -> bool:
    """Create *skill_dir* and its SKILL.md under *parent_fd*, or leave nothing behind.

    Split out so the rollback has one exit rather than being threaded through
    ``create_skill``'s branches. A partial create is not merely untidy here: the
    leftover directory makes ``create_skill``'s ``exists()`` guard answer False
    forever, so every retry is a 409 over a truncated body that ``list_skills()``
    still serves. Steering's create already unlinks its partial leaf for exactly
    that reason; this is the same rule, plus the directory, because this call is
    the one that created it.

    The leaf directory is created and opened RELATIVE to *parent_fd*, not through
    ``pinned_fs.create_and_open_dir_pinned``. That helper resolves
    ``skill_dir.parent`` with its own ``realpath`` and pins it again, discarding
    the descriptor the caller already walked -- a second resolution, which an
    ancestor swapped since the first is followed by. It would also leave the
    create and the rollback addressing two different directories, so on such a
    swap ``SKILL.md`` lands outside the skills root while the rollback reports an
    identity mismatch on an unrelated one. The helper's two other jobs are
    reproduced here rather than borrowed: a name that already exists is refused
    because ``os.mkdir`` under the pinned parent raises ``FileExistsError`` (the
    exclusivity is the syscall's, not a flag on a helper), and a link or
    non-directory at the leaf becomes the one refusal the caller maps rather than
    a raw errno.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    try:
        os.mkdir(skill_dir.name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        # Something holds the name that this call did not create, so it is not
        # ours to write into -- the exists() guard's answer, re-asked without a
        # window. 0o700 matches create_and_open_dir_pinned's mode for every
        # caller, so the directory-mode behaviour is unchanged.
        return False
    try:
        dir_fd = os.open(skill_dir.name, sk.pinned_fs.dir_flags(), dir_fd=parent_fd)
    except OSError as exc:
        # A link or a plain file raced onto the name between the mkdir and here.
        # Reclaim the directory this call just made -- rmdir only ever removes an
        # EMPTY one, so the worst case on a swap is losing a directory nobody has
        # written to yet, and leaving it would make every retry answer 409.
        with suppress(OSError):
            os.rmdir(skill_dir.name, dir_fd=parent_fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return False
        raise
    # Identity of the directory THIS call created, taken from the descriptor before
    # anything can be swapped at the name, so the rollback below can only ever
    # remove what this call brought into being. Guarded, because an fstat that
    # fails (EIO or ESTALE on a network filesystem) would otherwise leak the
    # descriptor AND strand the directory, and a stranded directory answers every
    # retry with 409.
    try:
        created = os.fstat(dir_fd)
    except BaseException:
        os.close(dir_fd)
        with suppress(OSError):
            os.rmdir(skill_dir.name, dir_fd=parent_fd)
        raise
    # Bound before the guarded region so the rollback can tell "no identity to
    # verify against" from "the identity is X" without inspecting locals.
    leaf: os.stat_result | None = None
    try:
        # 0o666, masked by umask, is what the by-name floor's write_text
        # produces, so the two branches land the same permissions and the pin
        # changes no default. This is the mode prompts.py's own pinned O_EXCL
        # create of user content passes, for the same reason. A tighter default
        # for user-authored skill bodies is a policy change that has to cover
        # both branches and both platforms, so it does not ride a migration.
        fd = os.open(
            "SKILL.md",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o666,
            dir_fd=dir_fd,
        )
        try:
            try:
                # Identity of the inode this call created, from the descriptor while
                # it is provably ours: the rollback addresses a NAME, and a rival can
                # unlink ours and create its own inside the failure window.
                leaf = os.fstat(fd)
                data = content.encode("utf-8")
                written = 0
                while written < len(data):
                    written += os.write(fd, data[written:])
            except BaseException:
                # Ask for the identity once more while the descriptor is still open --
                # the close below is what takes it away, and the rollback arm cannot
                # unlink anything without one. An EIO or ESTALE that made the first
                # fstat fail on a network filesystem is usually transient, so this is
                # a free path back to the verified arm; it can never answer with
                # another object, because it addresses a descriptor rather than a name.
                if leaf is None:
                    with suppress(OSError):
                        leaf = os.fstat(fd)
                raise
        finally:
            os.close(fd)
    except BaseException:
        # Roll the whole create back, leaf first, both through descriptors. Caught
        # broadly rather than on OSError: a KeyboardInterrupt or a MemoryError
        # building the buffer leaves the same half-made skill, and the retry is
        # just as permanently 409 either way.
        #
        # Both halves verify identity, because both address a NAME under a
        # descriptor and a name can be replaced inside the failure window. The
        # leaf goes through unlink_verified, which stats through the directory's
        # own fd and unlinks only if the inode is still the one created above, so
        # a rival that replaced SKILL.md keeps ITS file. The directory goes
        # through remove_dir_verified, which renames it aside under the pinned
        # parent, re-checks (st_dev, st_ino), and only then rmdirs -- a directory
        # swapped in at the name is reported rather than removed. A bare unlink
        # or rmdir by name would delete whatever answers to the name, which is
        # the step this whole migration exists to remove.
        #
        # ``leaf`` is None only when the leaf open failed, or when BOTH fstats on
        # the descriptor this call owned failed -- and the first of those precedes
        # the first os.write, so in every one of those cases nothing was written.
        # No identity therefore means no unlink: the empty SKILL.md keeps the
        # directory non-empty, remove_dir_verified's rmdir fails and puts the name
        # back, and the create is left as a skill with an empty body, which the
        # Skills tab lists and which update_skill and delete_skill both reach. That
        # is a save away from correct; unlinking whatever answers to the name to
        # spare that would destroy a file this code has never read.
        if leaf is not None:
            sk.pinned_fs.unlink_verified(dir_fd, "SKILL.md", (leaf.st_dev, leaf.st_ino))
        outcome = sk.pinned_fs.remove_dir_verified(
            parent_fd, skill_dir.name, expect=(created.st_dev, created.st_ino)
        )
        if not outcome.removed:
            # Reported, not raised over: the original failure is the one the caller
            # needs, and a rollback that could not finish leaves a name a human has
            # to look at. staged_name is set only when the entry was left aside.
            logger.warning(
                "skill create rollback left %s behind (%s%s)",
                name,
                outcome.reason,
                f", staged as {outcome.staged_name}" if outcome.staged_name else "",
            )
        raise
    finally:
        os.close(dir_fd)
    loader._invalidate_iter_cache()  # so the new skill shows in list_skills() now
    logger.info("Created skill: %s", name)
    return True


def update_skill(loader: SkillsLoader, name: str, content: str) -> bool:
    """Overwrite an existing skill's SKILL.md.  Returns True if found."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader._safe_name(name):
        return False
    skill_dir = loader._dir / name
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return False
    # Both capabilities, not one: the walk that produces the descriptor and the
    # descriptor-relative rename that consumes it are separate probes, and
    # atomic_write REFUSES a descriptor it cannot publish through rather than
    # quietly writing by name, so the floor is chosen here instead.
    if not (sk._DIR_FD_SUPPORTED and sk.pinned_parent_replace_supported()):
        if not loader._write_skill_md(skill_file, content, dir_fd=None):
            return False
        loader._invalidate_iter_cache()  # so the edit is reflected in list_skills() now
        logger.info("Updated skill: %s", name)
        return True

    # Pin the skill directory so the atomic replace stages and renames
    # through the walked descriptor rather than by name.
    #
    # open_dir_pinned, not pin_parent: ``self._dir / name`` is a lexical join
    # that nothing canonicalized, so this realpath is the FIRST resolution of
    # the chain rather than a second one, and there is no earlier canonical form
    # for pin_parent to walk. pin_parent here would instead refuse the ordinary
    # symlinks that legitimately sit above the skills root -- a symlinked $HOME
    # is the common one -- and break every update on such a host.
    try:
        dir_fd = sk.pinned_fs.open_dir_pinned(skill_dir, what="skill directory")
    except sk.pinned_fs.PinnedPathRefusal:
        return False
    except OSError:
        return False
    try:
        if not loader._write_skill_md(skill_file, content, dir_fd=dir_fd):
            return False
    finally:
        os.close(dir_fd)
    loader._invalidate_iter_cache()  # so the edit is reflected in list_skills() now
    logger.info("Updated skill: %s", name)
    return True


def _write_skill_md(skill_file: Path, content: str, *, dir_fd: int | None) -> bool:
    """Atomically replace *skill_file*, carrying its access-control xattrs.

    Routes through ``atomic_write`` with the same ACL carry the steering and
    file-write update paths use: ``mode=`` alone reproduces permission BITS
    only, so a named POSIX ACL the owner set on a skill's SKILL.md would be
    dropped the moment the replace installs a fresh inode. When *dir_fd* is a
    pinned parent the temp create and rename run relative to it, and the ACL
    source is opened relative to it too -- addressing the leaf by name after
    the caller pinned its directory would let a directory replaced at that
    name supply the mode and the ACL while the write published into the pinned
    original, handing the real skill back with permissions chosen by whoever
    did the replacing.

    Returns False when the target is REJECTED -- the source open failed, so
    there is no inode to carry from. A write failure still raises.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    try:
        src_fd = sk.open_access_control_source(skill_file, dir_fd=dir_fd)
    except OSError:
        # The same disposition the steering and file-write updates give this:
        # a rejected target, not a server fault. Continuing with src_fd=None
        # would publish a fresh inode carrying only the permission bits, so a
        # named POSIX ACL the owner set on this SKILL.md would be dropped and
        # the file handed back protected differently from the one it replaced
        # -- silently, on the one path that was supposed to fix that.
        return False
    try:
        # By-name stat only on the unpinned floor: with dir_fd the helper
        # always hands back a descriptor, so the bits and the ACL come from
        # one inode and neither is re-resolved.
        mode = (
            stat.S_IMODE(os.fstat(src_fd).st_mode)
            if src_fd is not None
            else stat.S_IMODE(skill_file.stat().st_mode)
        )
        sk.atomic_write(
            skill_file,
            content,
            mode=mode,
            newline="",
            preserve_access_control_from=src_fd,
            parent_dir_fd=dir_fd,
        )
    finally:
        if src_fd is not None:
            try:
                os.close(src_fd)
            except OSError:
                pass
    return True


def set_pinned(loader: SkillsLoader, name: str, pinned: bool) -> bool:
    """Pin/unpin an auto-skill (exempt from lifecycle eviction).

    Edits the ``pinned:`` frontmatter line in place. Returns True on
    success. Only auto-generated skills may be pinned.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader.is_auto_generated(name):
        return False
    skill_file = loader._dir / name / "SKILL.md"
    if not skill_file.exists():
        return False
    content = skill_file.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", content, re.DOTALL)
    if not m:
        return False
    fm_lines = [ln for ln in m.group(1).split("\n") if not ln.strip().startswith("pinned:")]
    if pinned:
        fm_lines.append("pinned: true")
    new_content = "---\n" + "\n".join(fm_lines) + "\n---\n" + m.group(2)
    # Atomic write (temp + rename): a partial write must never truncate the
    # live SKILL.md and lose the skill's content on a full-disk failure.
    sk.atomic_write(skill_file, new_content)
    loader._invalidate_iter_cache()
    logger.info("%s auto skill: %s", "Pinned" if pinned else "Unpinned", name)
    return True


def set_inject_on_trigger(loader: SkillsLoader, name: str, inject: bool) -> bool:
    """Opt a skill in or out of full-body injection on a trigger match.

    Edits the ``inject_on_trigger:`` frontmatter line in place, mirroring
    :meth:`set_pinned`. ``inject=False`` writes the opt-out; ``inject=True``
    removes the line rather than writing ``true``, because injecting is the
    default and an absent key is the honest way to say "unchanged".

    Refuses any skill whose file resolves outside this loader's own skills
    dir. ``_resolve_path`` also reaches ``skills.extra_paths`` and the
    kiro-cli user/workspace skill dirs — directories Kiro Crew does not own
    and may not even be able to write. Rewriting a foreign ``SKILL.md``
    because a dashboard toggle was flipped is a side effect nobody asked
    for, so ownership is checked before the write, not left to the UI (which
    does gate on source, but the endpoint is reachable directly).

    Returns False when the skill cannot be resolved, is not ours, or has no
    frontmatter block to edit — the caller surfaces that as a failed toggle
    rather than silently reporting success on a no-op.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader._safe_name(name):
        return False
    skill_file = loader._resolve_path(name)
    if skill_file is None or not skill_file.exists():
        return False
    try:
        owned_root = loader._dir.resolve()
        if not skill_file.resolve().is_relative_to(owned_root):
            logger.warning("Refusing to edit a skill outside %s: %s", owned_root, skill_file)
            return False
    except OSError:
        return False
    try:
        content = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", content, re.DOTALL)
    if not m:
        return False
    fm_lines = [
        ln
        for ln in m.group(1).split("\n")
        # Only a TOP-LEVEL key, matched without stripping: an indented
        # `inject_on_trigger:` belongs to a block scalar (a description that
        # documents the flag, say), and deleting that line would silently
        # rewrite the skill's prose while toggling a setting.
        if not ln.lower().startswith("inject_on_trigger:")
    ]
    if not inject:
        fm_lines.append("inject_on_trigger: false")
    new_content = "---\n" + "\n".join(fm_lines) + "\n---\n" + m.group(2)
    # Atomic write (temp + rename), for the same reason set_pinned uses it:
    # a partial write must never truncate the live SKILL.md.
    sk.atomic_write(skill_file, new_content)
    loader._invalidate_iter_cache()
    logger.info("Skill %s on trigger: %s", "injects fully" if inject else "sends a pointer", name)
    return True
