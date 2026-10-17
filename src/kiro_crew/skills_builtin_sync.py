"""The packaged-skill sync: install and update with a claim before every destructive step,
retirement of a verified copy, quarantine of a diverged one, the carried Context-budget
opt-out, stale and relocated skill cleanup, and the retired conductor skill's removal.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Callable

from kiro_crew import pinned_fs
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import FileTooLargeError, safe_read_file, safe_read_file_bytes_nolink
from kiro_crew.platform_compat import ensure_owner_rwx_dirs, is_link_or_junction, rmtree_force
from kiro_crew.skill_runtime import authoring as _authoring
from kiro_crew.skills_builtin_provenance import (
    _FINGERPRINT_MAX_ENTRIES,
    _PROVENANCE_MARKER,
    _recorded_fingerprint,
    _skill_tree_fingerprint,
    _tree_entries,
    _verified_unchanged_fingerprint,
    _write_provenance_marker,
)
from kiro_crew.skills_discovery import (
    _BUILTIN_SKILLS_DIR,
    _iter_skill_files,
    _project_skills_dir,
    skills_dir,
)

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


# Skills RELOCATED into the kirocrew-dev/ folder (the Kiro Crew development
# suite). Without this, an upgraded install keeps BOTH the old flat copy
# and the new nested copy — two divergent copies of the same skill matched
# nondeterministically by trigger overlap. The flat copy is NOT deleted (it
# may carry user edits
# the mtime-preserving sync deliberately protects): its SKILL.md is renamed
# to SKILL.md.pre-relocation, which removes it from loader discovery while
# preserving every byte on disk for the user to reconcile. Only done when
# the nested replacement is verifiably present, so a failed/partial sync
# never disables the only copy.
#
# Module level so the packaging guard in test/test_builtin_skill_packaging.py
# can assert every destination actually ships: a destination the package never
# installs makes this migration a permanent no-op and leaves the flat copy as
# the only one the loader finds.
_RELOCATED_SKILLS: dict[str, str] = {
    "prepare-pr": "kirocrew-dev/kirocrew-prepare-pr",
    # Renamed in place: the nested copy an earlier release installed.
    "kirocrew-dev/prepare-pr": "kirocrew-dev/kirocrew-prepare-pr",
    "babysit": "kirocrew-dev/babysit",
    "kirocrew-worktree-dev": "kirocrew-dev/kirocrew-worktree-dev",
}


def _claim_dir_for_replacement(dest_dir: Path) -> Path | None:
    """Atomically move *dest_dir* to a dot-prefixed sibling before verifying.

    Verify-then-delete has a race: another process (an editor, a second
    Kiro Crew instance syncing the same home) can swap the directory between
    the fingerprint check and the rmtree, destroying a tree the check never
    saw. Renaming first makes the claim atomic — whatever tree the caller
    verifies is exactly the tree it then deletes, restores, or quarantines.
    The claim name is dot-prefixed so a crash mid-resolution leaves the data
    hidden from skill discovery but intact on disk. Returns None when the
    claim itself fails; the caller must then leave the destination untouched.
    """
    claim = dest_dir.with_name(f".{dest_dir.name}.sync-claim")
    counter = 2
    while os.path.lexists(claim):
        claim = dest_dir.with_name(f".{dest_dir.name}.sync-claim.{counter}")
        counter += 1
    try:
        replace_with_retry(dest_dir, claim)
    except OSError:
        logger.warning(
            "could not claim skill dir %s for replacement; leaving it untouched",
            dest_dir,
            exc_info=True,
        )
        return None
    return claim


def _manifest_is_newer(src_file: Path, dest_file: Path) -> bool:
    """Whether the packaged manifest is newer than the installed one.

    Both stats are guarded because this runs on the gateway's startup path while
    another process (the CLI syncing the same home) may be claiming the very
    destination being measured. The two outcomes are deliberately different:

    * an unreadable DESTINATION means it vanished or was claimed mid-sync, so
      installing the packaged version is the correct answer -- update-due;
    * an unreadable SOURCE means the package itself cannot be read, and there is
      nothing to install from, so the destination is left alone.

    Raising instead would abort the whole sync for every remaining skill, which
    is what an unguarded ``stat`` did once the tree walks widened the window
    between the destination check and this comparison.
    """
    try:
        dest_mtime = dest_file.stat().st_mtime
    except OSError:
        return True
    try:
        return src_file.stat().st_mtime > dest_mtime
    except OSError:
        return False


def _tree_newest_mtime(root: Path) -> float | None:
    """Newest mtime of any regular file in *root*, or None when unprovable.

    The update gate needs to know whether a PACKAGED skill changed at all, not
    whether its ``SKILL.md`` did: a skill directory ships scripts, profiles and
    references alongside the manifest, and those are the files that carry the
    behaviour. Walking for the newest mtime is what makes a script-only release
    visible to the gate.

    The provenance marker is excluded for the same reason
    ``_tree_entries`` excludes it: the sync writes it AFTER copying, so its
    mtime is install time and would dominate every destination tree, making a
    later package update read as older than the copy it should replace — the
    gate would then never fire again.

    Returns None when the tree cannot be measured: an unreadable entry, or more
    entries than ``_FINGERPRINT_MAX_ENTRIES``. None is not a comparable value,
    so the caller falls back to the manifest comparison rather than guessing.
    """
    newest: float | None = None
    entries = 0
    for rel, kind, _value in _tree_entries(root):
        entries += 1
        if entries > _FINGERPRINT_MAX_ENTRIES:
            return None
        if kind == "unreadable":
            return None
        if kind != "file":
            continue
        try:
            mtime = os.lstat(root / rel).st_mtime
        except OSError:
            return None
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _tree_has_content(root: Path) -> bool:
    """True when the tree holds anything worth preserving.

    Only a COMPLETELY empty directory (zero entries — e.g. the placeholder an
    app registration leaves behind) counts as content-free; quarantining those
    would only mint junk backups on every update cycle. Any entry at all —
    files, links, specials, unreadable entries, and nested subdirectories,
    whose structure is itself user-made data — counts as content.
    """
    return any(True for _entry in _tree_entries(root))


def _finalize_user_backup(claim: Path, dest_dir: Path) -> Path | None:
    """Move a claimed, diverged tree to its ``.<name>.user-backup`` quarantine.

    Follows the collision behavior of the ``SKILL.md.pre-relocation``
    quarantine below: never overwrite an existing quarantine (``lexists``, so a
    dangling symlink also counts as occupied), pick the first unused numbered
    suffix.

    The quarantine name is ALWAYS dot-prefixed: dot-entries are pruned from
    skill discovery, so the single rename both preserves and deactivates the
    tree. Nothing inside the moved tree is ever touched afterwards — an
    earlier revision renamed ``backup / "SKILL.md"`` post-move, but that
    resolves a path THROUGH the backup directory, and a concurrent writer
    swapping the backup for a symlink between the two steps would redirect
    the rename into the symlink's target tree, outside the skills directory.
    One atomic rename of the claim itself has no such window, and a claim
    that is itself a link or junction is equally safe: the rename moves the
    link object, never its target.
    """
    stem = f".{dest_dir.name}.user-backup"
    backup = dest_dir.with_name(stem)
    counter = 2
    while os.path.lexists(backup):
        backup = dest_dir.with_name(f"{stem}.{counter}")
        counter += 1
    try:
        os.replace(claim, backup)
    except OSError:
        logger.warning(
            "could not move quarantined skill dir %s to %s; data preserved at " "the claim path",
            claim,
            backup,
            exc_info=True,
        )
        return None
    return backup


def _remove_ignorable_dir(path: Path) -> bool:
    """Remove a directory holding nothing worth preserving, race-free.

    The only ignorable content is the provenance marker this sync wrote
    (``_tree_entries`` excludes it, so ``_tree_has_content`` reports such a
    directory content-free). A marker file is only ignorable when it VERIFIES:
    its recorded fingerprint must parse and match the tree it sits in. A
    user-made file that merely shares the marker name (a marker-only name
    collision) fails that check — it is user bytes, so this returns False and
    the caller quarantines the tree instead of deleting anything. The rmdir
    is kernel-atomic: it succeeds only if the directory is STILL empty at
    unlink time, so a file created through a lingering directory handle after
    the emptiness check makes this return False instead of being lost.
    Callers must preserve the tree on False.
    """
    marker = path / _PROVENANCE_MARKER
    try:
        if os.path.lexists(marker):
            recorded = _recorded_fingerprint(path)
            if recorded is None or recorded != _skill_tree_fingerprint(path):
                return False
            marker.unlink()
        os.rmdir(path)
    except OSError:
        return False
    return True


def _dispose_superseded_slot(slot: Path, dest_dir: Path) -> bool:
    """Free the retirement slot name, deleting only what is re-verified.

    The occupant is CLAIMED first (atomic rename), so the tree that gets
    re-verified is exactly the tree that gets deleted — without the claim,
    a concurrent sync could park a fresh copy at the slot between this
    process's verification and its rmtree and have it destroyed unverified
    (this file's other destructive paths all follow the same claim-first
    invariant, see ``_claim_dir_for_replacement``). An occupant that fails
    re-verification carries bytes that landed after it was parked — the
    exact data the retirement exists to protect — and is preserved as a
    user backup instead of deleted.

    Returns True when the slot name is free afterwards. Every failure path
    keeps the occupant's bytes on disk (hidden at a dot-prefixed name at
    worst).
    """
    if not os.path.lexists(slot):
        return True
    slot_claim = _claim_dir_for_replacement(slot)
    if slot_claim is None:
        return False
    if is_link_or_junction(slot_claim):
        # A link at the slot name is user-made; preserve without following.
        _finalize_user_backup(slot_claim, dest_dir)
    elif not _tree_has_content(slot_claim):
        if not _remove_ignorable_dir(slot_claim):
            _finalize_user_backup(slot_claim, dest_dir)
    elif _verified_unchanged_fingerprint(slot_claim, None) is not None:
        if not rmtree_force(slot_claim):
            logger.warning(
                "could not remove epoch-old superseded skill copy %s; " "preserving what remains",
                slot_claim,
            )
            _finalize_user_backup(slot_claim, dest_dir)
    else:
        # Diverged since it was parked: late writes are user data.
        backup = _finalize_user_backup(slot_claim, dest_dir)
        logger.warning(
            "superseded skill copy %s changed after it was parked; " "preserved it at %s",
            slot,
            backup if backup is not None else slot_claim,
        )
    # The claim rename itself freed the slot name; whatever became of the
    # claimed occupant, its bytes are still on disk unless re-verified.
    return True


def _retire_verified_claim(claim: Path, dest_dir: Path, verified_fingerprint: str | None) -> bool:
    """Park a verified-unchanged claim at the hidden per-name retirement slot.

    Deleting a verified claim immediately would still lose bytes written
    through file descriptors that survived the claim rename: the fingerprint
    ran before those writes landed, so verification cannot see them. Instead
    the claim is parked at ``.<name>.superseded`` for one full sync cycle,
    and only the slot's PREVIOUS occupant — quiescent since the last update —
    is ever deleted, after being claimed and re-verified (see
    ``_dispose_superseded_slot``). A late write that landed in the meantime
    makes that re-check fail and the occupant is preserved as a user backup
    instead of deleted. Retention is bounded by construction: at most one
    hidden superseded copy per skill name; update-path slots rotate on the
    next update, and the stale-cleanup pass disposes of its slots on the
    following sweep.

    Returns True when the claim ended up parked; False when the slot could
    not be freed or the park itself failed, in which case the caller must
    preserve the claim rather than delete it.
    """
    slot = dest_dir.with_name(f".{dest_dir.name}.superseded")
    if not _dispose_superseded_slot(slot, dest_dir):
        return False
    try:
        os.replace(claim, slot)
    except OSError:
        logger.warning(
            "could not park verified skill copy %s at %s",
            claim,
            slot,
            exc_info=True,
        )
        return False
    # The parked tree must be re-verifiable next cycle. A claim proven by the
    # first-install migration rule (matches the packaged tree, no marker yet)
    # carries no marker of its own, so record the verified fingerprint now;
    # the marker file itself is excluded from fingerprints, so writing it
    # does not diverge the tree.
    if verified_fingerprint is not None and _recorded_fingerprint(slot) is None:
        _write_provenance_marker(slot, verified_fingerprint)
    return True


def _linked_component(base: Path, name: str) -> Path | None:
    """The first directory of *name* under *base* that is a link or junction, if any.

    *name* is a catalog key, so a nested one (``kirocrew-dev/x``) has a family
    directory on the way. A link anywhere on that path points outside the skills
    home, and the sync must not rename or remove anything through it.
    """
    parts = Path(name).parts
    for i in range(len(parts)):
        candidate = base.joinpath(*parts[: i + 1])
        if is_link_or_junction(candidate):
            return candidate
    return None


def _dest_opted_out_of_injection(dest_dir: Path) -> bool:
    """Did the user flip the Context-budget switch OFF on this installed builtin?

    The switch writes ``inject_on_trigger: false`` into the installed
    ``SKILL.md`` frontmatter (``skill_runtime.authoring.set_inject_on_trigger``),
    the one user-mutable setting on a built-in skill. Read it from the
    destination BEFORE the update claims the directory, so the opt-out can be
    carried onto the freshly installed packaged copy.

    Returns False on any read/parse failure: a carry is a best-effort
    convenience, never a reason to abort an install. Only a top-level
    ``inject_on_trigger: false`` counts, matching the writer and
    ``_parse_frontmatter`` (an indented occurrence is prose, not the setting).
    """
    skill_file = dest_dir / "SKILL.md"
    try:
        content = safe_read_file(str(skill_file))
    except (OSError, PermissionError, ValueError):
        return False
    try:
        meta = parse_frontmatter(content, SKILL_LOADER)
    except (OSError, ValueError):
        return False
    return str(meta.get("inject_on_trigger", "")).strip().lower() == "false"


def _carried_skill_md(src_dir: Path) -> bytes | None:
    """Return the packaged ``SKILL.md`` bytes with ``inject_on_trigger: false`` applied.

    Mirrors ``skill_runtime.versions._rewrite_update_frontmatter`` and the
    auto-skill refine path: a packaged ``SKILL.md`` never carries the switch, so
    an update that reinstalls it would silently turn full-body injection back on
    for a skill the user had made pointer-only — the setting reverting itself
    behind an unrelated app update. Append the one frontmatter
    line the user set, leaving the rest of the packaged body authoritative.

    The input is the PACKAGED file, never the installed one: the carried bytes
    are staged before anything is published, so no installed file is read and
    then rewritten while a concurrent writer could replace it.

    Returns None (install the packaged file verbatim) when the packaged copy
    already opts out, so the install still equals the packaged tree the marker
    records, and on any read/decode/parse failure: a carry is best-effort,
    never a reason to fail the sync.
    """
    try:
        data = safe_read_file_bytes_nolink(str(src_dir / "SKILL.md"), within_root=str(src_dir))
    except (OSError, ValueError, FileTooLargeError):
        return None
    if data is None:
        return None
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        packaged_meta = parse_frontmatter(content, SKILL_LOADER)
    except (OSError, ValueError):
        return None
    if str(packaged_meta.get("inject_on_trigger", "")).strip().lower() == "false":
        return None
    new_content = _authoring.rewrite_inject_on_trigger(content, False)
    if new_content is None:
        return None
    return new_content.encode("utf-8")


def _copy_with_carried_skill_md(dest_dir: Path, carried: bytes) -> Callable[[str, str], str]:
    """``copytree`` copy function that publishes *carried* as the top-level ``SKILL.md``.

    Every other file is copied with ``shutil.copy2``. The top-level
    ``SKILL.md`` is created exclusively with the carried bytes and then given
    the packaged file's mode and timestamps (``shutil.copystat``), exactly as
    ``copy2`` would, so the published file differs from the packaged one only
    by the carried line. The fingerprint hashes file modes, and the carried-line
    tolerance substitutes only the size, so a mode that differed (an
    owner-only temp file, say) would read as an edit and be quarantined.

    The file is written once, as it is published. If something else created it
    first, that file is left alone rather than overwritten; the marker records
    the packaged fingerprint, so it reads as a divergence and is preserved.
    """
    target = os.path.normcase(os.path.join(os.fspath(dest_dir), "SKILL.md"))

    def _copy(src: str, dst: str) -> str:
        if os.path.normcase(os.fspath(dst)) != target:
            return shutil.copy2(src, dst)
        try:
            with open(dst, "xb") as fh:
                fh.write(carried)
        except FileExistsError:
            logger.info("%s appeared before the sync published it; keeping it", dst)
            return dst
        shutil.copystat(src, dst)
        return dst

    return _copy


def _ensure_builtin_skills(base: Path) -> None:
    """Sync built-in skills: copy new/updated, remove known-stale ones.

    Supports nested directories (e.g. ``utils/tiny-url/SKILL.md``).
    Copies the entire skill directory (scripts, assets, etc.), not just SKILL.md.

    Destruction is provenance-gated: a destination directory is only ever
    removed (or replaced) when it is verifiably an unchanged copy this sync
    installed (see ``_verified_unchanged_fingerprint``), and it is atomically
    claimed before verification so the tree that gets verified is the tree
    that gets destroyed. Anything else — a user skill whose name collides with
    a builtin, a user-edited installed builtin, or a destination carrying
    user-added files — is preserved: moved aside to a ``<name>.user-backup``
    quarantine on update, or left alone entirely in the stale-cleanup pass.

    Cost note: the gateway runs this in a worker thread (``asyncio.to_thread``
    around ``SkillsLoader()``), and all verification work is bounded anyway:
    the steady state (marker present, no update due) costs one small marker
    read per skill; unmarked diverged directories cost a stat-level walk that
    stops at the first mismatch; content hashing only runs on trees whose stat
    manifest already matches a packaged skill, capped at
    ``_FINGERPRINT_MAX_BYTES`` / ``_FINGERPRINT_MAX_ENTRIES``.
    """
    source_names: set[str] = set()
    supplied: set[str] = set()
    for src_root in (_project_skills_dir(), _BUILTIN_SKILLS_DIR):
        if not src_root or not src_root.exists():
            continue
        for name, src_file in _iter_skill_files(src_root):
            source_names.add(name)
            # First source root to ship a name owns it for this run. Without
            # this, the second root races the copy the first just made: the
            # destination is this run's own output rather than user data, and
            # which tree ends up installed is decided by comparing mtimes
            # across two unrelated source trees. The project dir is iterated
            # first, so a project skill is not replaced by a packaged
            # one that merely carries a newer file.
            if name in supplied:
                continue
            supplied.add(name)
            src_dir = src_file.parent
            dest_dir = base / name
            dest_file = dest_dir / "SKILL.md"
            # Set only when a diverged destination carried the user's
            # Context-budget opt-out; applied as the packaged copy is written.
            carry_opt_out = False
            # The manifest's own mtime is not a proxy for the skill's: a
            # release that only changes ``scripts/`` leaves ``SKILL.md``
            # byte-identical with its packaged mtime, so a manifest-only
            # comparison reports "up to date" and the installed skill keeps
            # running superseded code indefinitely. Observed on prepare-pr,
            # whose extractor was fixed in the package while every install
            # kept the previous copy and failed against the current workflow.
            #
            # Both arms are kept, OR-ed: the tree arm adds the updates the
            # manifest arm cannot see, and the manifest arm still governs when
            # the tree is unmeasurable or when a locally edited destination
            # carries an mtime newer than anything the package ships. Since
            # ``copytree`` copies with ``copy2``, an unmodified install
            # fingerprints mtime-equal to its package, so a steady state does
            # not re-copy on every startup.
            update_due = not dest_file.exists()
            if not update_due:
                src_newest = _tree_newest_mtime(src_dir)
                dest_newest = _tree_newest_mtime(dest_dir)
                update_due = (
                    src_newest is not None and dest_newest is not None and src_newest > dest_newest
                ) or _manifest_is_newer(src_file, dest_file)
            if not update_due:
                # First-install migration adoption: an up-to-date destination
                # with no marker is from a pre-provenance install. Record
                # ownership NOW, while the installed package still matches it —
                # waiting until the next content update would find the trees
                # differing (new version vs old copy) and wrongly quarantine an
                # untouched builtin. The verified fingerprint is recorded
                # as-is rather than re-scanned, so files added concurrently
                # after the comparison can never be blessed as builtin-owned.
                if dest_dir.exists() and _recorded_fingerprint(dest_dir) is None:
                    adopted = _verified_unchanged_fingerprint(dest_dir, src_dir)
                    if adopted is not None:
                        _write_provenance_marker(dest_dir, adopted)
                continue
            if dest_dir.exists() or is_link_or_junction(dest_dir):
                claim = _claim_dir_for_replacement(dest_dir)
                if claim is None:
                    continue
                verified: str | None = None
                if not is_link_or_junction(claim):
                    verified = _verified_unchanged_fingerprint(claim, src_dir)
                    # The Context-budget switch is the one user-mutable setting
                    # on a built-in skill. Read it from the CLAIMED tree, after
                    # the atomic rename aside, not from dest_dir before the
                    # claim: the claim fixes which tree is verified and retired,
                    # so a dashboard toggle landing in that window is reflected
                    # here instead of being read stale and then discarded.
                    carry_opt_out = _dest_opted_out_of_injection(claim)
                if not is_link_or_junction(claim) and not _tree_has_content(claim):
                    # A placeholder holding nothing but (at most) our own
                    # provenance marker has no user bytes to preserve; the
                    # kernel-atomic rmdir inside fails — and the tree is
                    # preserved instead — if anything landed after the check.
                    if not _remove_ignorable_dir(claim):
                        backup = _finalize_user_backup(claim, dest_dir)
                        logger.warning(
                            "placeholder skill dir %s gained content before "
                            "removal; preserved it at %s",
                            dest_dir,
                            backup if backup is not None else claim,
                        )
                elif verified is not None:
                    if not _retire_verified_claim(claim, dest_dir, verified):
                        # The retirement slot was unusable: preserve the
                        # verified copy rather than delete it. Installing the
                        # packaged version is still correct either way.
                        backup = _finalize_user_backup(claim, dest_dir)
                        logger.warning(
                            "could not retire verified skill copy of %s; " "preserved it at %s",
                            dest_dir,
                            backup if backup is not None else claim,
                        )
                else:
                    backup = _finalize_user_backup(claim, dest_dir)
                    # A failed finalize leaves the data at the dot-prefixed
                    # claim path (hidden but intact); installing the packaged
                    # version is still correct either way.
                    logger.warning(
                        "Skill directory %s does not match the copy this sync "
                        "installed (user-authored or locally edited); preserved "
                        "it at %s before installing the packaged version",
                        dest_dir,
                        backup if backup is not None else claim,
                    )
            # Fingerprint the PACKAGED tree (immutable while this runs) and
            # record that as the installed state: fingerprinting the freshly
            # copied destination instead would bless any user write that lands
            # during the hash as sync-owned, licensing its later deletion. The
            # copy equals the source (the package ships only regular files and
            # directories), so the source fingerprint is the copy's.
            src_fingerprint = _skill_tree_fingerprint(src_dir, assume_owner_rwx_dirs=True)
            # The carried opt-out is staged from the PACKAGED SKILL.md and
            # written by the copy itself, so the published file is created
            # once, with the packaged mode, and never read back and rewritten.
            carried = _carried_skill_md(src_dir) if carry_opt_out else None
            try:
                if carried is None:
                    shutil.copytree(src_dir, dest_dir)
                else:
                    shutil.copytree(
                        src_dir,
                        dest_dir,
                        copy_function=_copy_with_carried_skill_md(dest_dir, carried),
                    )
            except FileExistsError:
                # Another process (gateway + CLI syncing the same home) won
                # the install race after our claim; its copy of the same
                # packaged skill is the destination now. Losing must not
                # crash the sync.
                logger.info("Skill %s installed concurrently elsewhere; keeping it", name)
                continue
            # copytree preserves source modes verbatim, so a read-only
            # install source (0o555 -- a Nix store path, a read-only mount)
            # yields a copy whose directories reject the provenance-marker
            # write below. Add owner rwx: file creation needs a writable
            # and searchable parent (including 0o455-class sources). The
            # recorded source fingerprint above is computed with
            # ``assume_owner_rwx_dirs=True``, i.e. it describes the
            # copy AS IT EXISTS AFTER this repair, so a clean install does
            # not read as a user customization on the next sync -- while any
            # later chmod on the installed copy (including removing
            # any owner-rwx bit) still diverges.
            ensure_owner_rwx_dirs(dest_dir)
            if src_fingerprint is not None:
                _write_provenance_marker(dest_dir, src_fingerprint)
            else:
                logger.warning(
                    "packaged skill tree %s cannot be fingerprinted; installed "
                    "%s without provenance",
                    src_dir,
                    name,
                )
            logger.info("Synced skill: %s", name)

    # Remove known stale builtin skills (replaced by MCP tools). A name a
    # source STILL ships (e.g. a project-level skill named ``cron``) is not
    # stale: sweeping it would delete on every startup what the loop above
    # just installed. Removal is provenance-gated by the same rule as updates:
    # only an unchanged copy this sync verifiably installed may be deleted by
    # name. A directory with no recorded provenance is user-authored by
    # assumption (a user skill named ``cron`` must survive every startup) and
    # is left alone — its removal, if ever wanted, is a human decision.
    # Deliberate consequence: installs that predate provenance recording keep
    # their stale builtin dirs until a human removes them, because there is no
    # packaged tree left to prove ownership against.
    stale_builtins = {
        "learn",
        "subagent",
        "cron",
        "kirocrew-core",
        # Not shipped: retire the installed copy.
        "kirocrew-dev/kirocrew-codebase-refactor",
    } - source_names
    if base.exists():
        for name in stale_builtins:
            stale = base / name
            family = Path(name).parent
            if family.parts and _linked_component(base, family.as_posix()) is not None:
                # A nested name's family directory is a user-made link: its
                # parked slot lives inside the link target too, so nothing on
                # this path is claimed, disposed of or read.
                logger.debug("Leaving %s in place: its family directory is a link", stale)
                continue
            # Unlike update-path slots (rotated by the next update), nothing
            # ever ships for a stale name again, so its parked copy is
            # disposed of here on the sweep AFTER the one that parked it —
            # that is its full quiescent cycle. Ordered before the live-dir
            # handling below, which can park a fresh copy this same run.
            slot = stale.with_name(f".{stale.name}.superseded")
            if not stale.is_dir() and os.path.lexists(slot):
                _dispose_superseded_slot(slot, stale)
            if _linked_component(base, name) is not None:
                # The sync only ever creates real directories; a link on the
                # way (a nested name's family directory included) is user-made
                # and its target must not even be read.
                logger.debug("Leaving link %s in place: user-made", stale)
                continue
            if not stale.is_dir():
                continue
            if _recorded_fingerprint(stale) is None:
                logger.debug(
                    "Leaving %s in place: no recorded provenance, so treated as " "user-authored",
                    stale,
                )
                continue
            claim = _claim_dir_for_replacement(stale)
            if claim is None:
                continue
            retired = False
            stale_fp = _verified_unchanged_fingerprint(claim, None)
            if stale_fp is not None:
                retired = _retire_verified_claim(claim, stale, stale_fp)
                if retired:
                    logger.info("Retired stale builtin skill: %s", name)
            if not retired:
                # Diverged since the marker was recorded (user data), or the
                # retirement slot was unusable: restore the tree to its
                # original name; on failure it stays hidden but intact at the
                # claim path.
                try:
                    os.replace(claim, stale)
                except OSError:
                    logger.warning(
                        "could not restore %s from claim %s; data preserved " "there",
                        stale,
                        claim,
                        exc_info=True,
                    )
        for old_name, new_name in _RELOCATED_SKILLS.items():
            old_skill_md = base / old_name / "SKILL.md"
            if old_skill_md.is_file() and (base / new_name / "SKILL.md").exists():
                # A directory on the way to the old SKILL.md that is a link or
                # junction points outside the skills home. Renaming through it
                # would rename a file the operator linked in, so leave it alone.
                linked = _linked_component(base, old_name)
                if linked is not None:
                    logger.warning(
                        "Skill %s relocated to %s, but %s is a link; not "
                        "quarantining through it (the linked copy is untouched)",
                        old_name,
                        new_name,
                        linked,
                    )
                    continue
                try:
                    # Never overwrite an earlier quarantine (a rollback or
                    # reinstall can recreate SKILL.md after a prior migration;
                    # os.replace would silently destroy the preserved copy).
                    # Pick the first unused numbered name instead.
                    quarantine = old_skill_md.with_name("SKILL.md.pre-relocation")
                    counter = 2
                    while quarantine.exists():
                        quarantine = old_skill_md.with_name(f"SKILL.md.pre-relocation.{counter}")
                        counter += 1
                    os.replace(old_skill_md, quarantine)
                    logger.info(
                        "Skill %s relocated to %s; flat copy quarantined at %s "
                        "(preserved on disk, no longer loaded)",
                        old_name,
                        new_name,
                        quarantine,
                    )
                except OSError:
                    logger.warning(
                        "could not quarantine relocated skill's flat copy %s",
                        old_skill_md,
                        exc_info=True,
                    )


#: SHA-256 of the static outputs from the retired conductor skill generator.
#: Exact identity keeps user-authored or edited files outside cleanup scope.
RETIRED_CONDUCTOR_SKILL_SHA256 = frozenset(
    {
        # select_crew-era text (crew triggers, `spawn_run(crew=...)` guidance)
        "ee91da7d58b89ddc4cd3ff097a87f520335193d78cb5937d7636e5baa9ee6ca5",
        # the first select_crew revision, before the crew= vs agent= warning
        "e967c693613dca258f66992b9788a8e5e8e12c4397147f58f6595ee42ffa21be",
    }
)
_RETIRED_CONDUCTOR_SKILL_MAX_BYTES = 16 * 1024


def is_retired_conductor_skill(data: bytes) -> bool:
    """Return whether *data* is a generated conductor skill revision.

    CRLF output from Windows is normalized to the generator's LF form before
    hashing. Bare carriage returns stay significant, as the generator emits none.
    """
    normalized = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest() in RETIRED_CONDUCTOR_SKILL_SHA256


def remove_retired_conductor_skill() -> bool:
    """Remove a byte-exact generated conductor skill through pinned descriptors.

    Return ``True`` only when the skill file is removed. Missing, user-authored,
    and edited files return ``False``. Read and unlink errors propagate so each
    caller can report them without blocking setup or gateway startup; an empty-dir
    prune failure is ignored. A linked conductor directory is refused before any
    file is read.

    The conductor directory is opened relative to the pinned skills-root
    descriptor with ``O_NOFOLLOW``, so a link swapped in at that name raises
    ``OSError`` and propagates to the caller instead of being followed.

    Platforms without descriptor-relative opens keep the no-link final-name and
    bounded-read checks, but ancestor pinning and atomic identity-checked unlink
    degrade to by-name checks around the open and unlink.
    """
    skill_path = skills_dir() / "conductor" / "SKILL.md"
    parent = skill_path.parent
    parent_info = pinned_fs.lstat_by_name(parent)
    if (
        parent_info is None
        or not stat.S_ISDIR(parent_info.st_mode)
        or pinned_fs.is_reparse_point(parent)
    ):
        return False

    if not pinned_fs.supports_pinned_walk():
        before = pinned_fs.lstat_by_name(skill_path)
        if (
            before is None
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
        ):
            return False
        fd = os.open(skill_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
                or (
                    before.st_ino
                    and opened.st_ino
                    and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                )
            ):
                return False
            data = os.read(fd, _RETIRED_CONDUCTOR_SKILL_MAX_BYTES)
            if os.fstat(fd).st_size != len(data):
                return False
        finally:
            os.close(fd)
        if not is_retired_conductor_skill(data):
            return False
        current = pinned_fs.lstat_by_name(skill_path)
        if (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or (
                opened.st_ino
                and current.st_ino
                and (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            )
        ):
            return False
        skill_path.unlink()
        try:
            if not os.listdir(parent):
                parent.rmdir()
        except OSError:
            pass
        return True

    root_fd = pinned_fs.pin_parent(
        os.path.realpath(parent.parent),
        what="retired conductor skill directory",
        refusal=OSError,
    )
    dir_fd: int | None = None
    try:
        current_parent = pinned_fs.stat_at(root_fd, parent.name)
        if (
            current_parent is None
            or not stat.S_ISDIR(current_parent.st_mode)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_info.st_dev, parent_info.st_ino)
        ):
            return False
        dir_fd = os.open(parent.name, pinned_fs.dir_flags(), dir_fd=root_fd)
        pinned_parent = os.fstat(dir_fd)
        parent_identity = (pinned_parent.st_dev, pinned_parent.st_ino)
        if parent_identity != (current_parent.st_dev, current_parent.st_ino):
            return False
        before = pinned_fs.stat_at(dir_fd, skill_path.name)
        if (
            before is None
            or not stat.S_ISREG(before.st_mode)
            or before.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
        ):
            return False
        fd = os.open(
            skill_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
        try:
            opened = os.fstat(fd)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
            ):
                return False
            data = os.read(fd, _RETIRED_CONDUCTOR_SKILL_MAX_BYTES)
            if os.fstat(fd).st_size != len(data):
                return False
        finally:
            os.close(fd)
        if not is_retired_conductor_skill(data):
            return False
        if not pinned_fs.unlink_verified(dir_fd, skill_path.name, identity):
            return False
        try:
            if not os.listdir(dir_fd):
                pinned_fs.remove_dir_verified(
                    root_fd,
                    parent.name,
                    expect=parent_identity,
                )
        except OSError:
            pass
        return True
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        os.close(root_fd)


def sync_builtins(self) -> None:
    """Run the builtin-skill sync for this loader's directory.

    The explicit seam for callers that own an off-loop context (the
    gateway runs this in a worker thread as a background task after the
    dashboard socket binds). Construction-time sync skips itself on a
    running event loop, so without this seam a loop-thread process would
    have no way to sync at all.
    """
    _ensure_builtin_skills(self._dir)
