"""Whether an installed builtin skill is an unchanged copy the sync installed, and whether it
still matches the package: the tree fingerprint, the provenance marker, the ownership check,
the doctor's currency verdicts, and deployed cron scripts judged against their skill assets.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from typing import Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.skill_runtime import authoring as _authoring
from kiro_crew.skills_discovery import (
    _BUILTIN_SKILLS_DIR,
    _iter_skill_files,
    _project_skills_dir,
    skills_dir,
)

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


# Provenance marker written into every skill directory this sync installs.
# A dotfile (never a SKILL.md field) so it can never render in skill listings:
# the loader only reads SKILL.md, and dot-entries are pruned from discovery.
# Its content is the full-tree fingerprint of the copy the sync wrote, which is
# what later runs compare against before destroying the destination.
_PROVENANCE_MARKER = ".builtin-skill-provenance"

# Version prefix on the marker content ("<format>:<fingerprint>"). Bump this
# whenever the fingerprint encoding changes (new entry kinds, mode bits, hash
# input layout): a marker in any other format is unparseable rather than
# comparable, so ``_recorded_fingerprint`` reports "no provenance" and the
# sync falls back to the packaged-tree adoption comparison. Without the
# version, an encoding change would make every recorded fingerprint mismatch
# its own unchanged tree and quarantine every untouched builtin fleet-wide.
_PROVENANCE_FORMAT = "2"

# Ceilings on what one tree verification may cost. Fingerprinting runs at
# gateway startup on the event loop, so both the read volume and the walk
# length must stay bounded regardless of what a user placed in the skills dir;
# a tree over either ceiling is treated as "cannot prove" (diverged), and the
# safe direction for anything unprovable is preservation. Packaged builtin
# skills are a few MB and a few dozen entries at most.
_FINGERPRINT_MAX_BYTES = 32 * 1024 * 1024
_FINGERPRINT_MAX_ENTRIES = 4096


#: Python's bytecode cache directory. Build output, not skill content: one
#: install flavour ships it (the desktop app precompiles its whole tree) and
#: another does not (a source checkout), from the same sources.
_BYTECODE_CACHE_DIR = "__pycache__"


def _tree_entries(
    root: Path, *, assume_owner_rwx_dirs: bool = False, skip_bytecode_caches: bool = False
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(relative path, kind, detail)`` for the tree under *root*.

    Deterministic order (sorted, top-down), lstat-based, and it never opens or
    follows anything: symlinks yield their target text (``link``), regular
    files their size (``file``), directories ``dir``, and FIFOs / devices /
    sockets ``special`` — so a hostile or accidental special file can never
    hang the walk. Entries that cannot be lstat'ed — and directories the walk
    itself cannot list (``os.walk`` reports those through ``onerror`` instead
    of raising) — yield ``unreadable``, which callers must treat as unequal to
    everything (fail toward "diverged"). The provenance marker itself is
    skipped: it records the fingerprint, so including it would make the
    recorded value impossible to reproduce.

    ``skip_bytecode_caches`` leaves out every ``__pycache__`` directory and
    everything under it. Only the doctor's currency comparison sets it; the
    ownership checks never do, so a cache on an installed copy still diverges it.
    """
    walk_errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_errors.append):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames.sort()
        if skip_bytecode_caches:
            dirnames[:] = [d for d in dirnames if d != _BYTECODE_CACHE_DIR]
        for dname in list(dirnames):
            entry = Path(dirpath) / dname
            rel = (rel_dir / dname).as_posix()
            try:
                mode = os.lstat(entry).st_mode
            except OSError:
                dirnames.remove(dname)
                yield rel, "unreadable", ""
                continue
            if stat.S_ISLNK(mode) or is_link_or_junction(entry):
                # os.walk(followlinks=False) does not descend POSIX symlinks,
                # but a Windows junction lstats as a plain directory and WOULD
                # be descended — into whatever tree it targets (e.g. a
                # credential directory), enumerating paths outside the
                # file-read gate. Classify both as links so a retargeted
                # link/junction changes the fingerprint, and keep the walk
                # out of the target either way.
                dirnames.remove(dname)
                try:
                    yield rel, "link", os.readlink(entry)
                except OSError:
                    yield rel, "unreadable", ""
            else:
                # Permission bits, like file modes below: a chmod on an
                # installed builtin's directory is a user customization and
                # must diverge the tree instead of being silently reset by
                # the next sync. One deliberate asymmetry: when the caller
                # sets ``assume_owner_rwx_dirs`` (used ONLY for the
                # PACKAGED SOURCE side of a comparison), owner rwx is OR-ed
                # in because the install adds those bits to the fresh
                # copy's directories (``ensure_owner_rwx_dirs`` -- a
                # read-only source such as a Nix store ships 0o555 and the
                # copy must accept marker writes and directory search).
                # A 0o455-class source needs execute added too. Hashing AS
                # THE COPY WILL LOOK keeps that install-owned repair from
                # reading as a user chmod, while the INSTALLED side is always
                # hashed with its real modes -- so a user chmod on the copy,
                # including removing an owner-rwx bit, still diverges. Files
                # are never normalized: the install never rewrites file modes.
                dir_mode = stat.S_IMODE(mode)
                if assume_owner_rwx_dirs:
                    dir_mode |= stat.S_IRWXU
                yield rel, "dir", f"{dir_mode:o}"
        for fname in sorted(filenames):
            entry = Path(dirpath) / fname
            rel = (rel_dir / fname).as_posix()
            if rel == _PROVENANCE_MARKER:
                continue
            try:
                st = os.lstat(entry)
            except OSError:
                yield rel, "unreadable", ""
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    yield rel, "link", os.readlink(entry)
                except OSError:
                    yield rel, "unreadable", ""
            elif stat.S_ISREG(st.st_mode):
                # Size AND permission bits: a mode-only customization (e.g.
                # chmod +x on a builtin script) is a user edit and must
                # diverge the tree. copytree preserves modes, so a clean
                # install still fingerprints equal to its package.
                yield rel, "file", f"{st.st_size}:{stat.S_IMODE(st.st_mode):o}"
            else:
                yield rel, "special", ""
    for err in walk_errors:
        # A directory the walk could not list may hold anything: surface it as
        # an unreadable entry so no consumer can mistake the tree for empty,
        # equal, or provable.
        yield getattr(err, "filename", None) or "<walk-error>", "unreadable", ""


def _trees_stat_equal(a: Path, b: Path) -> bool:
    """Stat-level lazy tree comparison: bail at the first mismatching entry.

    This is the cheap gate in front of content hashing on the startup path: a
    diverged destination (the common case for an unmarked directory that is
    not ours) costs directory listings and lstats up to the first difference,
    never a file read. ``unreadable`` equals nothing, including itself, and a
    pair of trees longer than the entry ceiling is unprovable (unequal) so the
    walk itself stays bounded. The roots' own permission bits are compared
    too: ``_tree_entries`` only yields children, and a chmod on the skill
    directory itself is as much a user customization as one on any child.
    """
    try:
        # ``a`` is the INSTALLED tree (hashed with real modes), ``b`` is the
        # PACKAGED SOURCE, whose directory modes are compared as the copy
        # will look after ``ensure_owner_rwx_dirs`` -- the same
        # asymmetry ``_tree_entries`` applies for child directories. A user
        # chmod on the installed side (including removing owner rwx)
        # therefore still diverges.
        if stat.S_IMODE(os.lstat(a).st_mode) != (stat.S_IMODE(os.lstat(b).st_mode) | stat.S_IRWXU):
            return False
    except OSError:
        return False
    entries = 0
    for ea, eb in zip_longest(_tree_entries(a), _tree_entries(b, assume_owner_rwx_dirs=True)):
        entries += 1
        if entries > _FINGERPRINT_MAX_ENTRIES:
            return False
        if ea is None or eb is None or ea != eb or ea[1] == "unreadable":
            return False
    return True


def _strip_carried_opt_out(data: bytes) -> bytes | None:
    """Return the SKILL.md bytes *data* minus a carried ``inject_on_trigger: false``.

    Only the exact form the builtin sync writes is recognised: *data* must be
    what the shared rewrite produces when it applies the opt-out to the
    returned bytes. Anything else (no line, a ``true`` value, an indented
    occurrence, a hand-placed line elsewhere in the block, undecodable bytes)
    returns None, so the caller treats the tree as unprovable rather than
    tolerating an edit.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    stripped = _authoring.rewrite_inject_on_trigger(text, True)
    if stripped is None or _authoring.rewrite_inject_on_trigger(stripped, False) != text:
        return None
    return stripped.encode("utf-8")


def _skill_tree_fingerprint(
    root: Path,
    *,
    assume_owner_rwx_dirs: bool = False,
    strip_carried_opt_out: bool = False,
    skip_bytecode_caches: bool = False,
) -> str | None:
    """Stable content hash of the whole skill tree under *root*.

    Covers every entry ``_tree_entries`` yields — file bytes, symlink targets,
    directory structure, special-file presence — so a destination differing
    only by a user-added script, note, empty directory, or a file swapped for
    a symlink fingerprints as diverged.

    Returns None when the tree cannot be proven: a link-or-junction root, an
    unreadable entry, more entries than ``_FINGERPRINT_MAX_ENTRIES``, or more
    file content than ``_FINGERPRINT_MAX_BYTES``. None never equals a recorded
    or computed fingerprint, so every unprovable tree is treated as diverged
    and preserved. File bytes are read through
    :func:`kiro_crew.hooks.safe_read_file_bytes_nolink` with the tree root as
    containment: the descriptor-pinned check rejects symlinks, hardlinked
    inodes, non-regular files, sensitive paths, and any resolved path outside
    the root — so a component swapped between the walk and the open (or a
    hardlink planted at a walked name) reads as unprovable instead of leaking
    outside bytes (e.g. credentials) into the hash.

    ``strip_carried_opt_out`` hashes the tree as it was before the sync carried
    the user's Context-budget opt-out onto it: the top-level ``SKILL.md`` bytes
    already read through the hardened path above are passed through
    ``_strip_carried_opt_out`` and hashed with their stripped size. Every
    other entry, and every guard, is unchanged. A ``SKILL.md`` that does not
    hold exactly the carried line makes the tree unprovable (None).

    ``skip_bytecode_caches`` is passed through to ``_tree_entries``.
    """
    if is_link_or_junction(root):
        return None
    digest = hashlib.sha256()
    # The root's own permission bits are part of the installed state: a chmod
    # on the skill directory itself must diverge the fingerprint exactly like
    # a chmod on any entry inside it.
    try:
        # ``assume_owner_rwx_dirs`` (set only when hashing the PACKAGED
        # SOURCE) ORs owner rwx in, so the recorded fingerprint describes
        # the copy as it will exist after ``ensure_owner_rwx_dirs``.
        # The installed side is always hashed with its real modes.
        root_mode = stat.S_IMODE(os.lstat(root).st_mode)
        if assume_owner_rwx_dirs:
            root_mode |= stat.S_IRWXU
    except OSError:
        return None
    digest.update(f"root\0{root_mode:o}\0".encode("utf-8"))
    budget = _FINGERPRINT_MAX_BYTES
    entries = 0
    for rel, kind, detail in _tree_entries(
        root,
        assume_owner_rwx_dirs=assume_owner_rwx_dirs,
        skip_bytecode_caches=skip_bytecode_caches,
    ):
        if kind == "unreadable":
            return None
        entries += 1
        if entries > _FINGERPRINT_MAX_ENTRIES:
            return None
        if kind != "file":
            digest.update(f"{kind}\0{rel}\0{detail}\0".encode("utf-8", "surrogatepass"))
            continue
        try:
            data = safe_read_file_bytes_nolink(
                str(root / rel), within_root=str(root), max_bytes=budget
            )
        except FileTooLargeError:
            # Over the remaining byte budget: the tree costs more to prove
            # than the ceiling allows, so it is unprovable (preserved).
            return None
        if data is None:
            return None
        budget -= len(data)
        if strip_carried_opt_out and rel == "SKILL.md":
            # The bytes come from the descriptor-pinned read above; only
            # their content and the size recorded for them are substituted.
            stripped = _strip_carried_opt_out(data)
            if stripped is None:
                return None
            data = stripped
            detail = f"{len(data)}:{detail.split(':', 1)[1]}"
        digest.update(f"{kind}\0{rel}\0{detail}\0".encode("utf-8", "surrogatepass"))
        digest.update(data)
    return digest.hexdigest()


def _recorded_fingerprint(dest_dir: Path) -> str | None:
    """Return the fingerprint the sync recorded in *dest_dir*, or None.

    A link or junction at the marker path is not a marker (the sync writes
    only regular files): it reads as "no provenance" (user-authored by
    assumption) instead of being followed. ``O_NOFOLLOW`` enforces this
    race-free on POSIX; Windows has no such flag, so the explicit
    link-or-junction probe carries the check there. The fstat re-check keeps
    a FIFO raced onto the path from blocking startup.
    """
    marker = dest_dir / _PROVENANCE_MARKER
    if is_link_or_junction(marker):
        return None
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(marker, open_flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    content = data.decode("utf-8", errors="replace").strip()
    # Only the current format is comparable. An older (or newer, on
    # downgrade) format encodes the fingerprint differently, so comparing it
    # against a freshly computed value would misread every unchanged tree as
    # diverged; treating it as "no provenance" routes those trees through the
    # packaged-tree adoption comparison instead, which re-records ownership
    # in the current format when the copy is verifiably unchanged.
    prefix = _PROVENANCE_FORMAT + ":"
    if not content.startswith(prefix):
        return None
    return content[len(prefix) :] or None


def _write_provenance_marker(dest_dir: Path, fingerprint: str) -> None:
    """Record *fingerprint* as the sync-installed state of *dest_dir*.

    ``atomic_write`` stages a unique temp file and renames it over the marker
    path: the rename replaces whatever occupies that path (including a planted
    symlink) rather than following it, so this write can never land outside
    the skill directory. Best-effort: a failed write only means the next run
    re-derives ownership against the packaged tree, so absence self-heals and
    must never break skill loading.
    """
    try:
        atomic_write(
            dest_dir / _PROVENANCE_MARKER,
            f"{_PROVENANCE_FORMAT}:{fingerprint}\n",
        )
    except OSError:
        logger.warning("could not record builtin-skill provenance in %s", dest_dir, exc_info=True)


def _record_builtin_provenance(dest_dir: Path) -> None:
    """Fingerprint the tree at *dest_dir* and record it as sync-installed."""
    fingerprint = _skill_tree_fingerprint(dest_dir)
    if fingerprint is None:
        logger.warning("skill tree %s cannot be fingerprinted; leaving it unmarked", dest_dir)
        return
    _write_provenance_marker(dest_dir, fingerprint)


def _matches_recorded(root: Path, recorded: str, current: str | None) -> bool:
    """Does the tree at *root* (fingerprinted as *current*) match *recorded*?

    The marker records the PACKAGED tree's fingerprint. A tree the sync
    installed while carrying the user's Context-budget opt-out differs from
    that by the one ``inject_on_trigger: false`` line on ``SKILL.md``, so when
    the straight comparison fails the tree is fingerprinted again with that
    line stripped. Any other difference still fails both comparisons.
    """
    if current is None:
        return False
    if current == recorded:
        return True
    return _skill_tree_fingerprint(root, strip_carried_opt_out=True) == recorded


def _verified_unchanged_fingerprint(dest_dir: Path, src_dir: Path | None) -> str | None:
    """Return *dest_dir*'s fingerprint iff it is verifiably an unchanged copy
    this sync installed, else None.

    Two ways to prove ownership:
    - The recorded provenance fingerprint still matches the tree on disk,
      allowing for a carried Context-budget opt-out (``_matches_recorded``).
      The value returned is the tree's own fingerprint either way.
    - First-install migration rule: installs that predate provenance recording
      carry no marker, and a naive "no marker means user-authored" rule would
      freeze every already-installed builtin at its current version forever.
      So an UNMARKED destination counts as builtin-owned exactly when it
      matches the packaged tree (*src_dir*) byte-for-byte; anything that
      genuinely differs — a user skill, a user-edited builtin, or a builtin
      from an older package whose content has since changed — is user data by
      assumption and is preserved. The stale-cleanup entries have no packaged
      tree left to compare against (``src_dir`` is None), so for them an
      unmarked directory is always user data.

    A destination that is itself a link or junction is never owned: the sync
    only ever creates real directories, and every verification primitive here
    would otherwise read the link's TARGET tree.
    """
    if is_link_or_junction(dest_dir):
        return None
    recorded = _recorded_fingerprint(dest_dir)
    if recorded is not None:
        current = _skill_tree_fingerprint(dest_dir)
        return current if _matches_recorded(dest_dir, recorded, current) else None
    if src_dir is None:
        return None
    if not _trees_stat_equal(dest_dir, src_dir):
        return None
    dest_fingerprint = _skill_tree_fingerprint(dest_dir)
    if dest_fingerprint is None:
        return None
    if dest_fingerprint != _skill_tree_fingerprint(src_dir, assume_owner_rwx_dirs=True):
        return None
    return dest_fingerprint


SKILL_INSTALL_IN_SYNC = "in-sync"
SKILL_INSTALL_BEHIND = "behind"
SKILL_INSTALL_EDITED = "edited"
SKILL_INSTALL_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class InstalledSkillCurrency:
    """One installed builtin skill judged against the packaged tree it came from."""

    name: str
    source: Path
    state: str


def _first_linked_skill_component(base: Path, name: str) -> Path | None:
    """First directory strictly BETWEEN *base* and ``base / name`` that is a link.

    A skill name may be nested (``kirocrew-dev/kirocrew-prepare-pr``), so testing the
    leaf alone leaves the directories above it unscreened while every probe of
    the leaf still resolves through them. A link at ``<skills>/kirocrew-dev``
    then makes the fingerprint hash a tree outside the skills directory and
    report it as this install, which is the verdict the leaf screen exists to
    refuse.

    Components are tested ROOT-FIRST and the walk stops at the first hit, so
    each ``lstat`` runs only after the component above it is known not to be a
    link and the walk itself never traverses one.

    A link here is operator-made: these are directories the sync creates itself,
    exactly as the leaf is. A flat name has no components between the two paths
    and costs no stat at all.
    """
    current = base
    for part in PurePosixPath(name).parts[:-1]:
        current = current / part
        if is_link_or_junction(current):
            return current
    return None


def installed_skill_currency() -> list[InstalledSkillCurrency]:
    """Judge each installed skill against the packaged tree the sync would pick.

    The question this answers is currency: does the install still correspond to
    the package this process is running. A script can report its own identity
    but not its own currency, because currency is a relation between the
    install and the package, and only one side of it is visible from inside a
    shipped file. Both sides are visible here.

    No new provenance data is needed, because the marker already holds the
    answer. ``_ensure_builtin_skills`` records the fingerprint of the PACKAGED
    tree (not of the copy it wrote), and ``_tree_entries`` hashes relative
    paths, modes, link targets and file bytes while excluding mtime entirely.
    The recorded value is therefore a portable content identity of the package
    the install came from, and comparing it against a fresh fingerprint of the
    packaged tree needs no version constant inside any shipped file, no marker
    format change, and no network call. One install differs from the tree the
    marker records: a builtin the sync reinstalled while carrying the user's
    Context-budget opt-out holds one extra ``inject_on_trigger: false`` line in
    ``SKILL.md``. The marker still records the PACKAGED fingerprint, and the
    ownership check tolerates exactly that line (``_matches_recorded``), so
    such an install reads as in sync rather than edited or behind.

    What makes staleness silent today is the update gate, not the marker: it
    compares mtimes, so an installed copy carrying an mtime newer than anything
    the package ships is judged up to date, no copy happens, and nothing says
    so. This function reads the same two trees the gate reads and reports the
    comparison the gate throws away.

    ``SKILL_INSTALL_BEHIND`` states that the two sides disagree, not which is
    older: a content hash cannot order two revisions, exactly as
    :func:`deployed_cron_script_sources` reports divergence without a
    direction. "Behind" names the actionable direction in practice because the
    packaged side is the code this process is running.

    ``SKILL_INSTALL_EDITED`` takes precedence over a currency verdict when both
    apply. The marker still identifies the package an edited install came from,
    but the edit is the fact that has to be reconciled first: while it stands,
    the sync quarantines the directory rather than updating it, so reporting
    the install merely as behind would name a remedy that does not apply.

    Source roots are consulted in the sync's own order, project skills before
    packaged ones, and the first root to ship a name owns it. Comparing a
    project skill against a packaged tree of the same name would otherwise
    report every shadowing skill as behind the builtin it deliberately
    replaces.

    An installed directory that NO source root ships is ABSENT from the result
    rather than reported: with no packaged tree there is nothing to be out of
    step with, and a skill the operator installed themselves must not be
    described as stale. Likewise a name that is packaged but not installed --
    the sync installs it on the next run, and an absent directory has no
    currency to judge.

    Every read failure yields ``SKILL_INSTALL_UNVERIFIABLE`` rather than a
    comparison: an unmarked install (pre-provenance, or user-authored by
    assumption), a destination that is itself a link or junction, a skills
    directory that is itself linked, a linked directory between the skills
    directory and a nested install, and a tree over the fingerprint ceilings
    all land there. Unverifiable never reads as agreement, because an
    instrument whose read failed must not report the two sides equal.

    POSIX only, and that gate is the first thing here rather than a detail.
    Every read below reaches its target by name, and the link test guarding each
    one is a separate syscall from the read it guards, so a concurrent writer to
    the skills directory can substitute a link in between. On POSIX the cost of
    losing that race is a wrong verdict. On Windows a junction whose target is a
    UNC share turns the next local-looking stat into an outbound connection that
    authenticates as this process, which nothing afterwards can take back, and
    the descriptor-pinned walk that would close it is unavailable there:
    :func:`kiro_crew.pinned_fs.supports_pinned_walk` requires ``O_NOFOLLOW`` and
    ``os.open`` in ``os.supports_dir_fd``, and Windows offers neither. So this
    reports nothing on Windows rather than reading unsafely, and the doctor
    section says the check does not run instead of printing verdicts it cannot
    stand behind. Tracked separately, with the core primitive it needs.
    """
    if os.name == "nt":
        return []
    base = skills_dir()
    # Screened BEFORE the first stat, because every probe below is built from
    # this path. A link here makes each local-looking stat a read of whatever
    # the link targets, and the sync only ever creates this directory for real,
    # so a link is operator-made and its target is not this gateway's install
    # tree. A fingerprint taken through it would describe some other tree and
    # be reported as this install.
    base_readable = not is_link_or_junction(base)
    if base_readable and not base.is_dir():
        return []

    results: list[InstalledSkillCurrency] = []
    supplied: set[str] = set()
    for src_root in (_project_skills_dir(), _BUILTIN_SKILLS_DIR):
        if not src_root or not src_root.exists():
            continue
        for name, src_file in _iter_skill_files(src_root):
            # First source root to ship a name owns it, mirroring the sync.
            if name in supplied:
                continue
            supplied.add(name)
            src_dir = src_file.parent
            if not base_readable:
                # Nothing under an unreadable base can be judged, and presence
                # is the first thing that cannot be tested. Reported rather than
                # skipped: a section that printed nothing would read as a
                # gateway with no installs, and unverifiable must never read as
                # agreement.
                results.append(
                    InstalledSkillCurrency(
                        name=name,
                        source=src_dir,
                        state=SKILL_INSTALL_UNVERIFIABLE,
                    )
                )
                continue
            dest_dir = base / name
            if _first_linked_skill_component(base, name) is not None:
                # Screened before the leaf, for the same reason the base is
                # screened before this loop: a nested name's intermediate
                # directories are resolved through by every probe below,
                # including the link test on the leaf itself, so a link there is
                # traversed by whichever probe runs first. Reported rather than
                # skipped, because presence is the first thing that cannot be
                # tested and an omitted name would read as not installed.
                results.append(
                    InstalledSkillCurrency(
                        name=name,
                        source=src_dir,
                        state=SKILL_INSTALL_UNVERIFIABLE,
                    )
                )
                continue
            # Order is the safety property, not a style choice. The link test is
            # local to dest_dir, while the SKILL.md stat resolves THROUGH it, so
            # testing the link first means a linked install is never traversed.
            # Reversed, the stat reads the link's target before anything has
            # judged the path, and a tree outside the skills directory then
            # decides whether this install is present.
            if not is_link_or_junction(dest_dir) and not (dest_dir / "SKILL.md").is_file():
                # Not installed: nothing on disk to judge. A link IS judged even
                # when its SKILL.md does not resolve, because a dangling one is
                # an install whose currency cannot be established, not an
                # absence -- and the state check refuses to read any link's
                # target, so reaching it costs no traversal.
                continue
            results.append(
                InstalledSkillCurrency(
                    name=name,
                    source=src_dir,
                    state=_skill_currency_state(dest_dir, src_dir),
                )
            )
    return sorted(results, key=lambda entry: entry.name)


def _skill_currency_state(dest_dir: Path, src_dir: Path) -> str:
    """Compare one installed skill tree against the packaged tree it came from."""
    if is_link_or_junction(dest_dir):
        # The sync only ever creates real directories, so a link here is
        # user-made and its target must not even be read.
        return SKILL_INSTALL_UNVERIFIABLE
    recorded = _recorded_fingerprint(dest_dir)
    if recorded is None:
        return SKILL_INSTALL_UNVERIFIABLE
    installed = _skill_tree_fingerprint(dest_dir)
    if installed is None:
        return SKILL_INSTALL_UNVERIFIABLE
    if not _matches_recorded(dest_dir, recorded, installed):
        return SKILL_INSTALL_EDITED
    packaged = _skill_tree_fingerprint(src_dir, assume_owner_rwx_dirs=True)
    if packaged is None:
        return SKILL_INSTALL_UNVERIFIABLE
    if recorded == packaged or _same_but_bytecode_caches(dest_dir, src_dir):
        return SKILL_INSTALL_IN_SYNC
    return SKILL_INSTALL_BEHIND


def _same_but_bytecode_caches(dest_dir: Path, src_dir: Path) -> bool:
    """Do the two trees match once every ``__pycache__`` is left out of both?

    The marker records the packaged tree of whichever install ran the sync. A
    machine with a source checkout and the desktop app has two packaged trees
    built from the same sources, and only the app's carries ``__pycache__``
    beside each ``scripts/*.py``. Doctor from one install therefore reads a copy
    the other synced as behind, and no restart or reinstall can clear it.

    Only reached once ``_matches_recorded`` proved the installed copy is the one
    the sync wrote, so a cache someone added to the copy has already read as
    edited. Both opt-out spellings of the installed ``SKILL.md`` are tried, the
    same tolerance the ownership check applies.
    """
    packaged = _skill_tree_fingerprint(
        src_dir, assume_owner_rwx_dirs=True, skip_bytecode_caches=True
    )
    if packaged is None:
        return False
    if _skill_tree_fingerprint(dest_dir, skip_bytecode_caches=True) == packaged:
        return True
    return (
        _skill_tree_fingerprint(dest_dir, skip_bytecode_caches=True, strip_carried_opt_out=True)
        == packaged
    )


# A cron script body is one file, not a tree, so its ceiling sits far below the
# whole-tree budget above. A body over this size reads as unverifiable rather
# than being compared -- the same fail-safe direction an unprovable tree takes.
_CRON_SOURCE_MAX_BYTES = 2 * 1024 * 1024

CRON_SOURCE_IN_SYNC = "in-sync"
CRON_SOURCE_DIVERGED = "diverged"
CRON_SOURCE_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class CronScriptSource:
    """One deployed cron script judged against the installed skill asset it came from."""

    name: str
    source: Path
    state: str


def _skill_script_index(base: Path) -> dict[str, list[Path]]:
    """Map each ``*.py`` script asset name to the installed skills shipping it.

    A name can be shipped by more than one skill, so the value is a list and the
    caller decides -- guessing an owner would invent provenance the copy never
    recorded.
    """
    index: dict[str, list[Path]] = {}
    for _name, skill_file in _iter_skill_files(base):
        scripts = skill_file.parent / "scripts"
        if not scripts.is_dir():
            continue
        for entry in sorted(scripts.glob("*.py")):
            index.setdefault(entry.name, []).append(entry)
    return index


def _read_for_comparison(path: Path, root: Path) -> bytes | None:
    """Read *path* under *root* containment, or None when it cannot be proven."""
    try:
        return safe_read_file_bytes_nolink(
            str(path), within_root=str(root), max_bytes=_CRON_SOURCE_MAX_BYTES
        )
    except FileTooLargeError:
        return None
    except OSError:
        return None


def deployed_cron_script_sources() -> list[CronScriptSource]:
    """Judge each deployed cron script that has an installed skill asset of its name.

    This is the second hop of the journey :func:`_verified_unchanged_fingerprint`
    already guards. The first hop -- packaged ``builtin_skills/`` into the
    installed skills dir -- is verified by CONTENT, the ``scripts/`` subtree
    included, precisely because a release that changes only a script leaves
    ``SKILL.md`` byte-identical, so a manifest-only comparison reports "up to
    date" while the install keeps running superseded code. The second hop --
    installed skill asset into ``<config_dir>/crons/`` -- is a hand-run ``cp``
    documented in the owning skill, and nothing has ever compared its two sides.
    The same silent staleness the first hop was taught to catch is therefore
    unobserved one step later.

    Scope is deliberately narrow. A deployed script with NO installed skill asset
    of that name is ABSENT from the result rather than reported: cron script
    bodies are LLM-writeable by design (see :mod:`kiro_crew.cron_script`) and
    most are authored in place with no source anywhere, so whether they ought to
    have one is a product question this function does not raise. Only a script
    that DOES have a source can be out of step with it.

    Reads go through the containment-checked reader the fingerprint helpers use,
    so a symlink, a hardlinked inode, a non-regular file, a path escaping its
    root, or an oversized body yields ``CRON_SOURCE_UNVERIFIABLE`` instead of a
    comparison. Unverifiable never reads as agreement -- an instrument whose read
    failed must not report the two sides equal.

    When several skills ship the same script name, agreement with ANY of them is
    ``CRON_SOURCE_IN_SYNC``: the copy records no owner, so a mismatch against an
    arbitrarily chosen candidate would be a fabricated finding.
    """
    crons_root = config_dir() / "crons"
    skills_root = skills_dir()
    if not crons_root.is_dir() or not skills_root.is_dir():
        return []
    index = _skill_script_index(skills_root)
    if not index:
        return []

    results: list[CronScriptSource] = []
    for deployed in sorted(crons_root.glob("*.py")):
        candidates = index.get(deployed.name)
        if not candidates:
            # No source to be out of step with -- out of scope by design.
            continue
        body = _read_for_comparison(deployed, crons_root)
        state = CRON_SOURCE_UNVERIFIABLE
        matched = candidates[0]
        if body is not None:
            unreadable = 0
            for candidate in candidates:
                source_body = _read_for_comparison(candidate, skills_root)
                if source_body is None:
                    unreadable += 1
                    continue
                if source_body == body:
                    matched = candidate
                    state = CRON_SOURCE_IN_SYNC
                    break
            else:
                # Every candidate was read and none matched, or some could not
                # be read at all. Only the fully-read case is a real divergence;
                # an unread candidate might have been the matching one.
                state = CRON_SOURCE_UNVERIFIABLE if unreadable else CRON_SOURCE_DIVERGED
        results.append(CronScriptSource(name=deployed.name, source=matched, state=state))
    return results
