"""Copy-vs-link classification for promoting a file into the artifact library.

Promoting a file to an artifact has two defensible outcomes:

``copy``
    Snapshot the bytes into artifact storage and forget the path. Correct for
    *disposable* files — a download, a scratch file in the temp dir, something
    on the Desktop. The original can be deleted tomorrow without hollowing out
    the artifact.

``link``
    Record the path as a live pointer (:attr:`Artifact.source_path`) so the
    artifact and the file stay one document — editing either shows up in the
    other. Correct for files that live in a *real project*, where the file, not
    the artifact, is the thing under version control.

This module holds that decision and nothing else: :func:`classify_source` is a
pure function over a path plus a caller-supplied list of project roots, so the
ordering rules below are unit-testable without a store, a session, or a running
gateway.

Why the verdict carries a *root*
--------------------------------
A ``link`` verdict returns the directory that authorises later reads of the
file. :class:`~kiro_crew.artifacts.ArtifactStore` re-validates ``source_path``
containment on **every** read (a symlink swap after the fact must not escape),
and its default roots are the user's home dir plus the data home. A project at
``/workplace/alice/repo`` is outside both, so a linked project file would be
refused on read — and the store would silently fall back to its stale snapshot.
Recording the authorising root on the artifact (``source_root``) is what makes
an out-of-home link actually work. It has to be recorded at create time because
the store has no session — and therefore no notion of "the current project" —
at read time.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

#: Verdicts returned by :func:`classify_source`.
COPY = "copy"
LINK = "link"

#: Bound on the parent walk when looking for a git repository root. Same value
#: and idiom as ``dashboard.handlers.files._PROJECT_ROOT_WALK_LIMIT`` — a fixed cap
#: so a pathological path can't turn classification into an unbounded stat
#: storm.
PROJECT_ROOT_WALK_LIMIT = 40

#: Filenames whose presence marks a directory as a PROJECT ROOT.
#:
#: Not all projects are git repositories, so ``.git`` alone is too narrow — a
#: checked-out-from-a-tarball tree, a Brazil package, or a plain notes+Makefile
#: directory is still a project the user expects to link.
#:
#: Every entry is deliberately a path INSIDE the directory being authorized, and
#: that is the security property, not an accident. A remote nomination (a list in
#: our own agent-writable data home naming an arbitrary root) let one writable
#: file authorize reads anywhere; a marker inside the candidate root requires
#: write access to that very directory, which is not an escalation over reading
#: it. ``is_sensitive_path`` still applies on top, and ``source_path`` must still
#: resolve inside the returned root.
PROJECT_ROOT_MARKERS: frozenset[str] = frozenset(
    {
        # version control
        ".git", ".hg", ".svn", ".bzr",
        # language / build manifests
        "package.json", "pyproject.toml", "setup.py", "setup.cfg",
        "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "Gemfile", "composer.json", "CMakeLists.txt",
        "Makefile",
        # workspace / tooling roots
        ".kiro", ".vscode", ".idea", ".editorconfig",
    }
)


def project_root_marker(directory: str) -> str | None:
    """Return the marker naming *directory* a project root, or ``None``.

    ``os.path.exists`` rather than ``isdir``/``isfile`` because the markers are a
    mix of both, and because a git *worktree*'s ``.git`` is a FILE (a gitdir
    pointer) — an ``isdir`` probe would miss every worktree.

    A FILESYSTEM ROOT is never a project root, however marked. A stray
    ``/Makefile`` (or ``C:\\Makefile``) would otherwise make ``/`` verifiable, and
    a forged artifact naming ``source_root="/"`` would then authorize reading any
    file the process can open — the whole-filesystem escape the marker rule
    exists to prevent. ``dirname(root) == root`` is the portable root test.
    """
    if os.path.dirname(directory) == directory:
        return None
    # A HOME directory is never a project root either. KiroCrew's OWN data home
    # is ``~/.kiro/crew``, so ``~/.kiro`` exists for every user -- which would
    # make the entire home directory a "project" and turn a loose ``~/notes.md``
    # into a LIVE link whose artifact edits overwrite the original. The same
    # applies to a stray ``~/Makefile`` or ``~/package.json``, so the home
    # directory is rejected wholesale rather than special-casing one marker.
    #
    # EVERY spelling is rejected, not just ``_home()``. The OS reports home
    # through several channels that do not always agree -- on Windows
    # ``expanduser`` reads ``USERPROFILE`` while ``HOME`` may say something else,
    # and a Windows profile really does ship markers (``.vscode``, ``.idea``).
    # Keying the rule to one accessor let a second, equally real home path be
    # walked into and authorize a link spanning the whole user profile.
    if _norm(directory) in _home_dirs():
        return None
    for marker in PROJECT_ROOT_MARKERS:
        try:
            if os.path.exists(os.path.join(directory, marker)):
                return marker
        except OSError:  # pragma: no cover — defensive (ELOOP on a parent)
            return None
    return None


# ── Seams (monkeypatched in tests; never inlined) ──────────────────────────────


def _tempdir() -> str:
    """The system temp dir. Indirected so tests can point it somewhere narrow.

    Test fixtures live under the *real* temp dir, so a test that needs a
    non-disposable directory has to be able to move this.
    """
    return tempfile.gettempdir()


def _home() -> str:
    """The user's home dir. Indirected for the same reason as :func:`_tempdir`."""
    return os.path.expanduser("~")


def _home_dirs() -> set[str]:
    """Every normalized path the OS might call "the user's home directory".

    Union rather than a single accessor: see :func:`project_root_marker` for why
    one spelling is not enough. Unset/blank env vars are skipped; a failure to
    resolve any one channel must not lose the others.
    """
    out: set[str] = set()
    candidates = [_home(), os.environ.get("USERPROFILE") or "", os.environ.get("HOME") or ""]
    try:
        candidates.append(str(Path.home()))
    except (OSError, RuntimeError):  # pragma: no cover -- no home resolvable
        pass
    for c in candidates:
        if not c:
            continue
        try:
            out.add(_norm(c))
        except (OSError, ValueError):  # pragma: no cover -- defensive
            continue
    return out


# ── Path helpers ──────────────────────────────────────────────────────────────


def canonical_path(path: str) -> str:
    """Expand ``~`` and resolve symlinks/``..`` to an absolute real path.

    Classification must run on the canonical path: ``/tmp/x/../projects/f`` is
    not in the temp dir, and a symlink from a project into ``~/Downloads`` is
    still a download.
    """
    return os.path.realpath(os.path.expanduser(path))


def _norm(path: str) -> str:
    """Normalize for comparison (case-folded on Windows, no trailing sep)."""
    return os.path.normcase(os.path.normpath(path))


def _contains(root: str, child: str) -> bool:
    """True when *child* is *root* itself or lives underneath it.

    Deliberately string-only (both sides are already canonical) and
    exception-free: ``os.path.commonpath`` raises on Windows when the two paths
    sit on different drives, which is a legitimate "not contained" answer.
    """
    r = _norm(root)
    c = _norm(child)
    if not r:
        return False
    if c == r:
        return True
    return c.startswith(r.rstrip(os.sep) + os.sep)


def disposable_roots() -> list[str]:
    """Roots whose contents are treated as throwaway: temp dir, Downloads, Desktop.

    A file here is snapshotted, never linked — these directories get cleared by
    the OS, by a browser, or by the user without a second thought, and an
    artifact that quietly empties itself when that happens is worse than no
    artifact.
    """
    home = _home()
    return [
        canonical_path(_tempdir()),
        canonical_path(os.path.join(home, "Downloads")),
        canonical_path(os.path.join(home, "Desktop")),
    ]


# ── The decision ──────────────────────────────────────────────────────────────


def classify_source(path: str) -> tuple[str, str]:
    """Decide whether promoting *path* should copy its bytes or link to it.

    Returns ``(COPY, "")`` or ``(LINK, root)``, where *root* is the directory
    that authorises later reads of the file — stored on the artifact as
    ``source_root`` and added to the store's allowed-roots set.

    The order is STRICT; the first rule that matches wins:

    0. **Unusable input → copy.** Not a non-empty ``str``, not absolute after
       expansion, missing, not a regular file, or refused by
       :func:`~kiro_crew.security.is_sensitive_path`. Nothing here is
       link-worthy: a pointer we can't read is the exact failure this whole
       change exists to remove.
    1. **Disposable root → copy** (temp dir, ``~/Downloads``, ``~/Desktop``).
       This MUST precede the project walk: ``/tmp/scratch`` can perfectly well
       contain a project marker, and a throwaway clone in the temp dir is still
       throwaway.
    2. **Inside a project → link** with the project root, found by walking up at
       most :data:`PROJECT_ROOT_WALK_LIMIT` levels for any of
       :data:`PROJECT_ROOT_MARKERS`. The NEAREST marker wins, so a package inside
       a monorepo links to the package rather than the whole tree. Not every
       project is a git repository, which is why the marker set is broader than
       ``.git`` — see :data:`PROJECT_ROOT_MARKERS` for why every marker must live
       inside the directory it authorizes.
    3. **Otherwise → copy.** No project marker anywhere above it: treat the
       artifact as owning a snapshot of the bytes.

    Pure: never reads config or session state, so it is fully unit-testable.
    """
    if not isinstance(path, str) or not path:
        return COPY, ""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # A relative path has no stable meaning once the artifact outlives the
        # cwd that produced it.
        return COPY, ""
    resolved = canonical_path(path)
    # is_sensitive_path is checked on the RESOLVED path so a benign-looking
    # symlink or `..` chain into ~/.aws can't be linked (the store would refuse
    # the read anyway — refusing here keeps the pointer from being recorded at
    # all).
    if is_sensitive_path(resolved):
        return COPY, ""
    try:
        if not os.path.isfile(resolved):
            return COPY, ""
    except OSError:  # pragma: no cover — defensive (ELOOP, EACCES on a parent)
        return COPY, ""

    # (1) Disposable first — before any repo detection.
    for root in disposable_roots():
        if _contains(root, resolved):
            return COPY, ""

    # (2) Bounded walk up to a git repository root.
    #
    # A registered-project branch used to sit here and was REMOVED: the recents
    # list it drew on lives in the agent-writable data home, so it let an agent
    # nominate its own authorizing root (write ["/"], then forge an artifact
    # naming source_root="/"). Whatever authorizes a LINK has to be re-verifiable
    # at read time from something the attacker cannot also write — see
    # is_verifiable_root. A project that is not a repo therefore COPIES, which is
    # a real capability, unlike a link that degrades to a stale snapshot.
    # Operator-configured publish.relocate_roots remains the escape hatch for a
    # shared tree that is not a git repository.
    cur = os.path.dirname(resolved)
    for _ in range(PROJECT_ROOT_WALK_LIMIT):
        # NEAREST marker wins, so a package inside a monorepo links to the
        # package rather than the whole tree.
        if project_root_marker(cur) is not None:
            return LINK, cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    # (4) Nothing claims it.
    return COPY, ""


# ── Project-root assembly ─────────────────────────────────────────────────────


def is_verifiable_root(root: str) -> bool:
    """True when ``root`` is still an observable git repository root.

    ``source_root`` is PERSISTED on the artifact, and ``meta.json`` is inside the
    agent-writable data home — so the stored value is a HINT, never authority. A
    forged record naming ``source_root="/"`` with ``source_path="/etc/passwd"``
    would otherwise widen the store's read boundary to the whole filesystem.

    Re-deriving the authority at read time closes that: a root only authorizes a
    read while it independently looks like a project, which a metadata write
    cannot manufacture. ``.git`` is probed with ``os.path.exists`` because a git
    worktree's ``.git`` is a FILE (a gitdir pointer), not a directory.

    The trade is deliberate: a linked file in a project that is not a git repo
    (and is not otherwise inside an allowed root) degrades to serving its
    snapshot. That is visible rather than silent — ``source_missing`` is set —
    and a widened boundary is the worse failure.
    """
    if not root or not isinstance(root, str) or not root.strip():
        return False
    try:
        resolved = os.path.realpath(os.path.expanduser(root))
    except (OSError, ValueError):
        return False
    if not os.path.isdir(resolved):
        return False
    if is_sensitive_path(resolved):
        return False
    # Deliberately NOT rejecting disposable roots here. classify_source already
    # refuses to LINK anything under them (the disposable test runs before the
    # git walk), so a temp-dir repo never earns a recorded root in the first
    # place; and the escape this guards against is a forged root reaching
    # arbitrary system paths, not a read confined to the temp dir.
    # An observable git repository root is the ONLY proof accepted here, and it
    # must match what classify_source is willing to LINK so that create and read
    # never disagree.
    #
    # Registered-project membership was considered and REJECTED: the recents
    # list lives in the agent-writable data home, so an agent could write
    # ["/"] into it and then forge an artifact naming source_root="/" —
    # re-verification would rubber-stamp its own forged input and hand the store
    # read access to the whole filesystem. A proof has to be something the
    # attacker cannot also write; a `.git` directory on disk is, a JSON file in
    # our own data home is not.
    return project_root_marker(resolved) is not None
