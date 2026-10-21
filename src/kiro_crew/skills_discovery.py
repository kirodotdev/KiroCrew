"""Finding skill files: the skills directory and the packaged and trusted provider roots, the
installed package's RECORD admission of a hardlinked ``SKILL.md``, the configured extra roots,
the walk that enumerates every root (``_iter_skill_files``) with its no-follow traversal of a
trusted project's ``.kiro/skills``, and the bounds on how long a walk's answer is served.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields written: ``_configured_extra_paths``, ``_extra_paths``.

Instance fields read: ``_edition_extra_paths``.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import errno
import hashlib
import hmac
import logging
import os
import stat
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path, PurePosixPath
from typing import Iterator

from kiro_crew import skill_trust
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.dep_sync import normalize as normalize_distribution_name
from kiro_crew.platform_compat_links import is_link_or_junction
from kiro_crew.release_channel import _DISTRIBUTION_NAME
from kiro_crew.security import is_sensitive_path, is_sensitive_resolved_path

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


SKILLS_DIR_NAME = "skills"
# Filesystem latency dominates cold discovery. Bound both active readers and
# submitted work so a large catalog cannot create one thread/future per skill.
_CATALOG_READ_WORKERS = 8
_CATALOG_READ_BATCH = 64


# How long a discovered skill-file list is served before a REVALIDATION is due.
# Reaching this deadline never costs the caller a walk: the stale list is handed
# back and the re-walk is queued onto the catalog-refresh worker (see
# _request_catalog_refresh). So the deadline bounds how out-of-date an OUT OF BAND
# change (an AIM sync, a manual cp) may be, and nothing else — the app's own
# create/update/delete/refresh all call _invalidate_iter_cache(), so a skill
# written through the app is visible at once to the loader that wrote it. The
# fence is per loader: another loader, in this process or another one, that
# already HOLDS a list keeps serving it until this deadline, and only a loader
# that reads the stored snapshot afterwards sees the drop.
#
# The value is sized against the walk it amortizes, not picked for tidiness: a walk
# of a real skills tree (645 files across 21 roots on a dev desktop, incl.
# AIM-installed package roots) takes ~0.7s, while chat messages arrive MINUTES
# apart, so anything on the order of seconds is missed by every message and
# amortizes nothing. At 60s one walk covers ~12 messages.
_ITER_CACHE_TTL_SECS = 60.0

# How long a caller with NOTHING to serve waits for the first walk of a root set.
#
# This is the one case where a turn can wait on discovery at all: no in-memory
# list, and no stored snapshot either — a machine's very first run, or one whose
# index file was deleted. It is a bounded wait on a background build, not a walk
# on the calling thread: when the budget runs out the caller is served whatever
# has been published, the scope is marked INCOMPLETE (see `catalog_status`), and
# the same build keeps going, so the next call adopts the finished snapshot.
#
# Why wait at all rather than return empty immediately: a small tree finishes
# inside this budget, and finishing is what makes `always: true` bodies known and
# therefore honored. Returning empty would make the first session on every machine
# start without its required instructions. The budget is what keeps a
# 5,000-skill tree from turning that guarantee into a minute of silence — such a
# tree is served from its snapshot on every run but the first.
_COLD_CATALOG_WAIT_SECS = 2.0
#: Paths a check admitted for an unconfined read are remembered up to this many,
#: the oldest admission first out. The rows are agent-influenced (the stored
#: snapshot), so the set must not grow with them; an evicted path only costs
#: another check.
_VETTED_READS_MAX = 4096

# A snapshot read off disk is revalidated only when it is older than this, so a
# process that starts, answers one call and exits does not queue a walk of a tree
# another process enumerated moments ago.
_CATALOG_REVALIDATE_AFTER_SECS = 60.0

# What an agent is told when its scope is served from an unfinished first walk.
# Named rather than inlined because two properties are load-bearing: it must say
# that an always-loaded skill may be MISSING (silence about that is the failure
# this notice exists to avoid), and it must name the call that re-reads the set,
# so the agent has an action rather than a warning.
_DISCOVERY_IN_PROGRESS_NOTICE = (
    "[Skills: discovery in progress]\n"
    "This machine's skill directory is still being built, so the set below may be "
    "incomplete and an always-loaded skill may not have been injected yet. Re-run "
    "skill_search(action='list', offset=0) before concluding a skill does not exist.\n"
    "[End of skills notice]\n\n"
)

# A granted repository remains attacker-controlled after consent. Bound the
# descriptor-relative walker well below Python's recursion limit so a malicious
# nesting chain cannot crash discovery for the whole chat turn. Depth counts
# directories below the project's .kiro/skills root; files at the cap still load.
_PROJECT_SKILL_MAX_DEPTH = 64


# Bundled fallback — inside the kiro_crew package
_BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"

#: The installed ``kiro_crew`` package tree: a trusted provider root for skill
#: symlinks, and the only tree whose hardlinked ``SKILL.md`` can be admitted (see
#: :func:`_installed_package_bytes_match`). A module binding so a test can stand
#: up a fake site dir.
_INSTALLED_PACKAGE_DIR = Path(__file__).parent

#: RECORD hash names accepted as an admission witness: the wheel spec requires
#: sha256 or stronger, and a weaker one could be matched on purpose.
_RECORD_HASHES = frozenset({"sha256", "sha384", "sha512"})


def _project_skills_dir() -> Path | None:
    """Return project-level skills/ dir from KIROCREW_PROJECT_DIR, or None."""
    val = os.environ.get("KIROCREW_PROJECT_DIR")
    if val:
        p = Path(val) / "skills"
        if p.is_dir():
            return p
    return None


def _trusted_skill_roots() -> tuple[str, ...]:
    """Resolved roots a symlink inside the skills tree may legitimately point into.

    An app ships its skills inside its OWN tree, and
    ``apps.bridges._register_skills`` symlinks them into the skills dir "so the
    skill scanner finds the skill" — so their resolved paths land OUTSIDE the
    skills base by construction. Two roots are legitimate skill providers:

    * the installed ``kiro_crew`` package — built-in apps keep their skills
      under ``apps/builtins/<app>/skills/``;
    * ``<data home>/apps`` — externally installed apps.

    A symlink resolving anywhere else stays rejected: an arbitrary target would
    admit unvetted ``SKILL.md`` prose into the agent's context.
    """
    roots: list[str] = [os.path.realpath(_INSTALLED_PACKAGE_DIR)]
    try:
        roots.append(os.path.realpath(config_dir() / "apps"))
    except Exception:  # noqa: BLE001 — an unresolvable data home must not stop scanning
        pass
    return tuple(roots)


_package_record_lock = threading.Lock()
#: package dir -> (cache key, recorded ``SKILL.md`` digests). The key is the site
#: dir's own stamp plus each candidate RECORD's, so a dist-info added or removed
#: (the site dir changes) or a RECORD rewritten (its stamp changes) re-reads.
_package_record_cache: dict[str, tuple[tuple, dict[str, frozenset[tuple[str, str]]]]] = {}


def _record_stamp(record: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(record)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _package_record_paths(package_dir: str) -> list[str]:
    """Every RECORD the installer left for this package, found by name alone.

    One listing of the site dir, which reads no other distribution's metadata:
    the cost is that directory's entry count, never their RECORD sizes. More
    than one survives an upgrade that leaves the previous ``dist-info`` behind;
    each is a candidate, and a stale one can only vouch for bytes the previous
    release shipped. An editable install
    keeps its ``dist-info`` in site-packages, not beside the source tree, so it
    finds none and admits nothing.
    """
    wanted = normalize_distribution_name(_DISTRIBUTION_NAME)
    records: list[str] = []
    try:
        with os.scandir(os.path.dirname(package_dir)) as entries:
            for entry in entries:
                stem, dot, suffix = entry.name.rpartition(".")
                if (
                    dot
                    and suffix == "dist-info"
                    and normalize_distribution_name(stem.split("-", 1)[0]) == wanted
                ):
                    records.append(os.path.join(entry.path, "RECORD"))
    except OSError:
        return []
    return sorted(records)


def _read_package_record(
    record: str, package_dir: str
) -> tuple[tuple[int, int, int], dict[str, tuple[str, str]]] | None:
    """*record*'s identity and its ``SKILL.md`` digests, or ``None``.

    A RECORD that does not list the package's own ``__init__.py`` vouches for
    nothing, so it answers no digests. ``None`` means it could not be read or
    changed while it was read: the identity is taken before and after the parse,
    so digests are never filed under a stamp that belongs to a different RECORD.
    """
    before = _record_stamp(record)
    if before is None:
        return None
    site_dir = os.path.dirname(package_dir)
    package = os.path.basename(package_dir)
    owned = f"{package}/__init__.py"
    skill_row = f"/{_SKILL_FILE}"
    owns = False
    digests: dict[str, tuple[str, str]] = {}
    try:
        with open(record, encoding="utf-8", newline="") as fh:
            # A substring test first: only two kinds of row matter, and running
            # every one of a few thousand rows through the CSV parser is what
            # made this the slow part.
            wanted = (line for line in fh if line.startswith(owned) or skill_row in line)
            for row in csv.reader(wanted):
                if not row:
                    continue
                if row[0] == owned:
                    owns = True
                    continue
                parts = PurePosixPath(row[0]).parts
                if (
                    len(row) < 2
                    or parts[-1:] != (_SKILL_FILE,)
                    or parts[:1] != (package,)
                    or ".." in parts
                ):
                    continue
                name, sep, value = row[1].partition("=")
                if sep:
                    absolute = os.path.normpath(os.path.join(site_dir, *parts))
                    digests[os.path.normcase(absolute)] = (name.lower(), value)
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if _record_stamp(record) != before:
        return None
    return before, digests if owns else {}


def _recorded_skill_digests() -> dict[str, frozenset[tuple[str, str]]]:
    """The installed package's recorded ``SKILL.md`` digests, re-read on reinstall.

    Cached per process under the site dir's stamp and every candidate RECORD's,
    so the steady state costs a few ``stat`` calls and a reinstall, a dist-info
    appearing mid-upgrade, or a RECORD replaced mid-read is re-read on the next
    call rather than judged against a previous answer. A RECORD that changed
    while it was parsed is left out and its stamp is not recorded, so the next
    call reads it again.
    """
    package_dir = os.path.realpath(_INSTALLED_PACKAGE_DIR)
    site_stamp = _record_stamp(os.path.dirname(package_dir))
    with _package_record_lock:
        cached = _package_record_cache.get(package_dir)
    if cached is not None and cached[0][0] == site_stamp:
        if all(_record_stamp(record) == stamp for record, stamp in cached[0][1]):
            return cached[1]
    stamps: list[tuple[str, tuple[int, int, int] | None]] = []
    merged: dict[str, set[tuple[str, str]]] = {}
    for record in _package_record_paths(package_dir):
        found = _read_package_record(record, package_dir)
        if found is None:
            # Unreadable or mid-rewrite: a ``None`` stamp never matches a file
            # that exists, so the next call reads it again.
            stamps.append((record, None))
            continue
        stamps.append((record, found[0]))
        for path, digest in found[1].items():
            merged.setdefault(path, set()).add(digest)
    digests = {path: frozenset(found) for path, found in merged.items()}
    key = (site_stamp, tuple(stamps))
    with _package_record_lock:
        _package_record_cache[package_dir] = (key, digests)
    return digests


def _installed_package_bytes_match(path: str, data: bytes) -> bool:
    """Admit a hardlinked ``SKILL.md`` only when it IS the installed package's file.

    Installers such as uv hardlink package files out of their cache, so a
    built-in app skill can legitimately carry ``st_nlink > 1``. The no-link
    reader refuses that shape everywhere else; here it is admitted on content:
    *path* must be a ``SKILL.md`` the distribution's RECORD lists inside the
    installed package tree, and *data* (the bytes actually read) must hash to the
    digest recorded for it. Any other hardlinked file, including one in the
    skills dir, an app under the data home, an extra path or a project, has no
    RECORD entry and stays refused. Rewriting RECORD needs write access to the
    same tree as the package code, which already decides what the gateway runs.
    """
    try:
        recorded = _recorded_skill_digests().get(os.path.normcase(os.path.normpath(path)), ())
        for algorithm, value in recorded:
            if algorithm not in _RECORD_HASHES:
                continue
            digest = hashlib.new(algorithm, data).digest()
            actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            if hmac.compare_digest(actual, value):
                return True
        return False
    except Exception:  # noqa: BLE001 — an admission that cannot be decided refuses
        return False


def _within_any(candidate: str, roots: tuple[str, ...]) -> bool:
    """True when the already-resolved *candidate* equals one of *roots* or sits under it."""
    cand = Path(candidate)
    for root in roots:
        try:
            if cand == Path(root) or cand.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


#: Basename every skill's body lives under. Used as a cheap pre-filter before
#: any filesystem work when deciding whether a tool call touched a skill.
_SKILL_FILE = "SKILL.md"


_PROJECT_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


#: (base, refused component, errno) triples already warned about, so a project
#: whose chain is refused is named once per process rather than on every catalog
#: build. The linked-entry warning (a symlinked child under a real ``.kiro/skills``
#: that the walk skips) shares this one set under a distinct marker
#: ``(project_root, _LINKED_ENTRIES_MARKER, 0)`` -- keyed on the project root
#: alone, so a project whose skills are a tree of links is named once rather than
#: once per link. The outside-root skill-link warning (a direct-child link in the
#: skills base whose target is outside every allowed root) shares it too under
#: ``(str(link), _OUTSIDE_ROOT_MARKER, 0)``, so that drop is named once per link.
#: Bounded: past the cap the warning is still emitted, just not
#: recorded.
_CHAIN_REFUSALS_WARNED: set[tuple[str, str, int]] = set()
_CHAIN_REFUSALS_WARNED_CAP = 256

#: Sentinel "component" that marks a linked-entry warning in the shared set,
#: distinct from any real path component (a NUL cannot appear in a filename).
_LINKED_ENTRIES_MARKER = "\x00linked-entries"


def _note_chain_refusal(base: Path, component: str, exc: OSError) -> None:
    """Say which component of a project skills path the no-follow walk refused.

    A missing component is the ordinary case (most projects have no
    ``.kiro/skills``) and stays at DEBUG. Anything else -- a symlinked
    directory (refused by ``O_NOFOLLOW``), a file where a directory was
    expected, a permission denial -- means the operator's skills exist but will
    never load, and the remedy is to inspect that one path, so it is a WARNING
    naming it.
    """
    err = exc.errno or 0
    if err == errno.ENOENT:
        logger.debug("project skills path has no %r component: %s", component, base)
        return
    marker = (str(base), component, err)
    if marker in _CHAIN_REFUSALS_WARNED:
        return
    if len(_CHAIN_REFUSALS_WARNED) < _CHAIN_REFUSALS_WARNED_CAP:
        _CHAIN_REFUSALS_WARNED.add(marker)
    # O_DIRECTORY | O_NOFOLLOW reports a symlink as ELOOP or ENOTDIR depending
    # on the kernel, the same ENOTDIR a regular file gets, so name both.
    if err in (errno.ELOOP, errno.ENOTDIR):
        reason = "is a symlink or not a directory"
    else:
        reason = f"could not be opened ({exc.strerror or f'errno {err}'})"
    logger.warning(
        "project skills under %s are not loaded: path component %r %s; project "
        "skills are walked without following links, so this component must be "
        "a real, readable directory",
        base,
        component,
        reason,
    )


def _note_linked_skill_entries(project_root: str, link_paths: list[Path]) -> None:
    """Warn once per project root, naming every linked top-level entry skipped.

    A top-level child of a trusted ``.kiro/skills`` that is a symlink is neither
    descended nor read, because the confined walk follows no links and keeps only
    real directories and regular files -- a link target can leave the project or,
    on Windows, be a UNC path whose descent would leak credentials. A whole skill
    kept elsewhere and linked in at the top level is then dropped without a word,
    the silent skip this names. It fires only for a top-level child: the walk is
    otherwise recursive and discovers a nested ``family/leaf/SKILL.md`` as a real
    skill, so a symlink deeper in the tree is that skill's own internal structure,
    not a lost top-level skill, and a nested skill directory whose own ``SKILL.md``
    is itself a symlink stays silent -- naming that case would mean warning on
    every file-level link at every depth, which this warn-only change does not do.
    The message states the rule rather than guessing each entry's type: a project
    skill loads only from a real directory holding ``SKILL.md``, so the fix is a
    real directory (a linked plain file was never a skill either way). Each entry's
    own no-follow ``lstat`` is all this needs; a link is never resolved, so an
    automount or network target is not reached. The warning fires once per project
    root but names EVERY skipped top-level link in that one line, so an operator
    who replaces one link is not left with the others skipped silently.
    """
    if not link_paths:
        return
    marker = (project_root, _LINKED_ENTRIES_MARKER, 0)
    if marker in _CHAIN_REFUSALS_WARNED:
        return
    if len(_CHAIN_REFUSALS_WARNED) < _CHAIN_REFUSALS_WARNED_CAP:
        _CHAIN_REFUSALS_WARNED.add(marker)
    named = ", ".join(str(p) for p in link_paths)
    logger.warning(
        "project skills under %s: linked entries (%s) directly under "
        ".kiro/skills were not loaded; project skills are walked without "
        "following links and load only from a real directory holding SKILL.md, "
        "so replace each link with a real directory",
        project_root,
        named,
    )


def _open_project_dir_chain(base: Path) -> int | None:
    """Open every absolute path component through the prior no-follow handle."""
    if not skill_trust.project_skill_traversal_supported():
        return None
    parts = Path(os.path.abspath(base)).parts
    try:
        fd = os.open(parts[0], _PROJECT_DIR_OPEN_FLAGS)
    except OSError as exc:
        _note_chain_refusal(base, parts[0], exc)
        return None
    for part in parts[1:]:
        try:
            next_fd = os.open(part, _PROJECT_DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError as exc:
            os.close(fd)
            _note_chain_refusal(base, part, exc)
            return None
        os.close(fd)
        fd = next_fd
    return fd


def _walk_confined_skill_fd(
    fd: int,
    current: Path,
    *,
    depth: int = 0,
) -> Iterator[tuple[str, list[str], list[str]]]:
    """Yield an ``os.walk``-shaped tree anchored to directory descriptors."""
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(fd) as scanner:
            for entry in scanner:
                try:
                    entries.append((entry.name, entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        return

    dirs = sorted(name for name, st in entries if stat.S_ISDIR(st.st_mode))
    files = sorted(name for name, st in entries if stat.S_ISREG(st.st_mode))
    # A child whose lstat is a symlink is never descended, nor read as a file
    # (the walk follows no links and keeps only real directories and regular
    # files), so a linked entry directly under .kiro/skills is dropped. That is
    # the lost skill the operator put there, and dropping it in SILENCE is what
    # hides it, so name it once per project root -- and in that one line name
    # EVERY such link, so replacing one does not leave the rest skipped silently.
    # Only the TOP level (depth 0) is named: a link deeper inside a real, loaded
    # skill -- a skill's own myskill/references -> shared-docs link, say -- is
    # that skill's internal structure, not a lost top-level skill, and must not
    # warn. At depth 0 `current` IS the confined base <root>/.kiro/skills, so the
    # project root is current.parent.parent -- no separate parameter needed. Each
    # entry's own no-follow lstat is all the warning needs; no link is resolved,
    # so a target that is an automount or a network path is not reached, and the
    # message does not claim an entry is a directory (it may be a linked file).
    # A dot-link (an editor/tooling artifact, never a skill) stays quiet.
    if depth == 0:
        linked = [
            current / name
            for name, st in entries
            if stat.S_ISLNK(st.st_mode) and not name.startswith(".")
        ]
        _note_linked_skill_entries(str(current.parent.parent), sorted(linked))
    if depth >= _PROJECT_SKILL_MAX_DEPTH:
        dirs = []
    # The consumer prunes dot-directories in place before traversal resumes.
    yield str(current), dirs, files
    for name in dirs:
        try:
            child_fd = os.open(name, _PROJECT_DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError:
            # A directory swapped for a link, or removed, fails here without
            # resolving its target.
            continue
        try:
            yield from _walk_confined_skill_fd(child_fd, current / name, depth=depth + 1)
        finally:
            os.close(child_fd)


def project_skill_tree_present(project_dir: str | Path | None) -> bool:
    """Whether *project_dir* has a ``.kiro/skills`` tree Crew would read.

    Asks trust first and touches no path for an untrusted project; then opens
    the tree through the no-follow descriptor chain the project traversal uses,
    so a component that is a link (a network share on Windows, for one) is
    refused rather than followed. Unsupported platforms answer ``False``.
    """
    if not project_dir:
        return False
    if not skill_trust.project_skills_enabled() or not skill_trust.is_project_trusted(project_dir):
        return False
    fd = _open_project_dir_chain(Path(project_dir) / ".kiro" / "skills")
    if fd is None:
        return False
    os.close(fd)
    return True


def _walk_confined_skill_tree(base: Path) -> Iterator[tuple[str, list[str], list[str]]]:
    """Walk a project tree without path-based traversal or link following.

    The walk names a linked top-level entry under ``.kiro/skills`` it skips; at
    the top level *base* is ``<root>/.kiro/skills``, so the project root the
    warning keys on is derived from it, not passed in.
    """
    fd = _open_project_dir_chain(base)
    if fd is None:
        # A project without .kiro/skills is the common case. Missing, linked,
        # unreadable, and unsupported cannot be distinguished without probing
        # the path again, so keep the refusal observable without warning on
        # every ordinary catalog scan.
        logger.debug(
            "Refusing project skills traversal; a component is missing, linked, "
            "unreadable, or the platform lacks no-follow dirfd support: %s",
            base,
        )
        return
    try:
        yield from _walk_confined_skill_fd(fd, base)
    finally:
        os.close(fd)


#: Sentinel "component" that marks an outside-root skill-link warning in the
#: shared ``_CHAIN_REFUSALS_WARNED`` set, distinct from any real path component
#: and from ``_LINKED_ENTRIES_MARKER`` (a NUL cannot appear in a filename).
_OUTSIDE_ROOT_MARKER = "\x00outside-root-link"


def _warn_skill_link_outside_roots(link: Path, target: str) -> None:
    """Warn that a link placed in the skills tree was skipped as out of bounds.

    The containment gate is a security boundary — a link to an arbitrary target
    would admit unvetted ``SKILL.md`` prose into the agent's context — so the
    link is refused. A silent refusal gives a user who linked a skill folder in
    from a git repository no hint that the catalog dropped it, so name the link,
    its resolved target, and ``skills.extra_paths`` (the supported way to admit
    an outside location). Deduped through the module's shared
    ``_CHAIN_REFUSALS_WARNED`` registry under ``_OUTSIDE_ROOT_MARKER`` so the
    warning fires once per link per process rather than on every catalog scan;
    past the shared cap it still fires, just unrecorded, the same fail-safe
    direction the other warnings in that set take.
    """
    marker = (str(link), _OUTSIDE_ROOT_MARKER, 0)
    if marker in _CHAIN_REFUSALS_WARNED:
        return
    if len(_CHAIN_REFUSALS_WARNED) < _CHAIN_REFUSALS_WARNED_CAP:
        _CHAIN_REFUSALS_WARNED.add(marker)
    logger.warning(
        "Skill link %s is not followed: its target %s is outside the skills "
        "directory and every allowed root. To load the skill, add the target's "
        "parent directory to skills.extra_paths.",
        link,
        target,
    )


def _iter_skill_files(
    base: Path,
    *,
    confine_to: tuple[str, ...] | None = None,
    exclude_roots: tuple[str, ...] = (),
    warn_outside_links: bool = False,
    extra_path_roots: tuple[str, ...] = (),
) -> list[tuple[str, Path]]:
    """Recursively find all SKILL.md files under *base*.

    Returns ``(relative_name, skill_file_path)`` pairs sorted by name.
    The relative name uses ``/`` as separator (e.g. ``utils/tiny-url``).

    Unconfined provider trees follow links because apps register skills through
    them. Confined project trees never follow directory links or junctions: a
    link target can be a Windows UNC path, where descent would leak credentials.

    ``warn_outside_links`` is opt-in and off by default: only the catalog base
    walk sets it, because only the full skill catalog can say a link was dropped
    from it, and only that caller knows ``skills.extra_paths``. Every other
    walker (``kirocrew doctor``'s script index, provenance, selection, sync) does
    a narrower internal walk and stays silent. When it is on, a refused
    direct-child link whose resolved target is under one of ``extra_path_roots``
    (the configured ``skills.extra_paths``) does NOT warn, because that target
    loads through a separate extra-path walk and the operator already followed
    the advice. This gates only the warning; the containment decision that
    refuses the link in THIS walk is unchanged.
    """
    if confine_to is not None:
        if len(confine_to) != 1:
            return []
        expected_base = os.path.abspath(Path(confine_to[0]) / ".kiro" / "skills")
        supplied_base = os.path.abspath(base)
        if os.path.normcase(supplied_base) != os.path.normcase(expected_base):
            return []
        results: list[tuple[str, Path]] = []
        for dirpath, dirs, files in _walk_confined_skill_tree(base):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            if "SKILL.md" not in files:
                continue
            skill_file = Path(dirpath) / "SKILL.md"
            rel = skill_file.parent.relative_to(base)
            results.append((str(rel).replace("\\", "/"), skill_file))
        return sorted(results, key=lambda item: item[0])

    if not base.exists():
        return []
    base_real = os.path.realpath(base)
    allowed_roots = (base_real,) + _trusted_skill_roots()

    def probe(directory: Path) -> tuple[str | None, list[Path], Path | None]:
        lexical = os.path.abspath(directory)
        if exclude_roots and _within_any(lexical, exclude_roots):
            return lexical, [], None
        real = os.path.realpath(directory)
        if exclude_roots and _within_any(real, exclude_roots):
            return real, [], None
        if not _within_any(real, allowed_roots):
            # The containment gate is a security boundary, so the link is
            # refused. When this walk is the catalog base walk
            # (``warn_outside_links``), a link the user placed directly in the
            # skills base and pointing at a folder outside every allowed root
            # (the "link my skills in from a repo" case) is reported once so the
            # drop is not silent: name the target and the supported fix.
            # is_link_or_junction covers both a POSIX symlink and a Windows
            # junction, and the parent-realpath check limits the warning to a
            # direct child of the base the user themselves created. A target
            # under a configured ``skills.extra_paths`` root
            # (``extra_path_roots``) stays silent, since it loads through the
            # extra-path walk and the operator already followed the advice.
            warned = False
            if warn_outside_links:
                try:
                    is_direct_child_link = os.path.realpath(
                        directory.parent
                    ) == base_real and is_link_or_junction(directory)
                except OSError:
                    is_direct_child_link = False
                if is_direct_child_link and not _within_any(real, extra_path_roots):
                    _warn_skill_link_outside_roots(directory, real)
                    warned = True
            if not warned and os.name == "nt":
                # On Windows, record the resolved child and the roots it is
                # judged against whenever the containment gate turns a child of
                # the skills base away, so a CI run surfaces the exact
                # (candidate, roots) pair the gate rejects. Windows-only and
                # logging-only: the return is unchanged, so no platform's
                # behaviour is affected.
                logger.warning(
                    "skill discovery: _within_any rejected a child of base "
                    "(candidate=%r, roots=%r)",
                    real,
                    allowed_roots,
                )
            return real, [], None
        if is_sensitive_resolved_path(real):
            return real, [], None
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            # A failed alias has not visited its target. Another spelling may
            # still enumerate it successfully, as with os.walk's error handling.
            return None, [], None
        children = []
        has_skill = False
        for entry in entries:
            if exclude_roots and _within_any(os.path.abspath(entry.path), exclude_roots):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir and not entry.name.startswith("."):
                children.append(directory / entry.name)
            elif not is_dir and entry.name == "SKILL.md":
                has_skill = True
        children.sort(key=lambda path: path.name)
        skill_file = directory / "SKILL.md"
        if not has_skill:
            return real, children, None
        real_file = os.path.realpath(skill_file)
        if exclude_roots and _within_any(real_file, exclude_roots):
            return real, children, None
        if not _within_any(real_file, allowed_roots) or is_sensitive_resolved_path(real_file):
            return real, children, None
        return real, children, skill_file

    results = []
    seen_real: set[str] = set()
    # Commit probes in sorted depth-first order, irrespective of completion
    # order, so links sharing a target keep the same canonical skill key.
    stack = [base]
    pending: dict[Path, Future[tuple[str | None, list[Path], Path | None]]] = {}
    with ThreadPoolExecutor(
        max_workers=_CATALOG_READ_WORKERS, thread_name_prefix="skill-walk"
    ) as pool:
        while stack:
            for directory in reversed(stack):
                if len(pending) >= _CATALOG_READ_BATCH:
                    break
                if directory not in pending:
                    pending[directory] = pool.submit(copy_context().run, probe, directory)
            directory = stack.pop()
            future = pending.pop(directory, None)
            real, children, discovered_file = future.result() if future else probe(directory)
            if real is None or real in seen_real:
                continue
            seen_real.add(real)
            if discovered_file is not None:
                name = str(directory.relative_to(base)).replace("\\", "/")
                results.append((name, discovered_file))
            stack.extend(reversed(children))
    return sorted(results, key=lambda item: item[0])


def skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


async def _on_config_change(self, change: "live.ConfigChange") -> None:
    # Both halves run off-loop. The screening stats every configured root
    # (resolve + is_dir), and a root on a slow or network mount would stall the
    # loop; the adoption then invalidates the persisted catalog, which takes
    # SQLite's write lock and can wait out the busy timeout when another process
    # holds it. Neither belongs on the gateway's event loop.
    screened = await asyncio.to_thread(self._screen_extra_paths, change.new)
    await asyncio.to_thread(self._adopt_extra_paths, screened)


def reconfigure(self, cfg: KiroCrewConfig) -> None:
    """Re-resolve the configured extra skill roots from *cfg* (synchronously).

    The watcher path splits this into :meth:`_screen_extra_paths` off the loop
    and :meth:`_adopt_extra_paths` on it; this method is the one-call form for
    a caller that is not on the event loop.
    """
    self._adopt_extra_paths(self._screen_extra_paths(cfg))


def _screen_extra_paths(cfg: KiroCrewConfig) -> list[Path]:
    """Resolve and screen ``skills.extra_paths`` -- filesystem work, no state.

    Runs the SAME screening as construction -- expanduser, resolve,
    ``is_sensitive_path`` reject, existence check -- so a root added by hand to
    ``config.json`` can no more reach a credential directory than one present at
    boot. Fails closed per entry: a rejected or missing root is dropped with the
    same log line rather than admitted.
    """
    resolved_paths: list[Path] = []
    # Logged by position, not value: a rejected entry is by definition a path
    # under a credential home, and a reloaded config is an untrusted document,
    # so the string itself never reaches the log.
    for index, p in enumerate(cfg.skills.extra_paths):
        resolved = Path(p).expanduser().resolve()
        if is_sensitive_path(str(resolved)):
            logger.warning("Skipping sensitive skills.extra_paths[%d] on reload", index)
        elif resolved.is_dir():
            resolved_paths.append(resolved)
        else:
            logger.debug("skills.extra_paths[%d] does not exist; skipped on reload", index)
    return resolved_paths


def _adopt_extra_paths(self, resolved_paths: list[Path]) -> None:
    """Install screened roots.

    Edition-contributed roots are preserved and stay LAST (lowest precedence);
    they come from the platform context, not config, so a config write must not
    drop them. A root-set change is a full catalog invalidation, not just a
    cleared list: the stored snapshot belongs to the OLD root set, and a walk
    already in flight over those roots must not publish under the new scope, so
    the persisted rows are dropped and the generation moved as well.
    """
    self._configured_extra_paths = resolved_paths
    merged = list(resolved_paths)
    for edition_path in self._edition_extra_paths:
        if edition_path not in merged:
            merged.append(edition_path)
    self._extra_paths = merged
    self._invalidate_iter_cache()
