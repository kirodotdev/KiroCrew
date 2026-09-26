"""Papyrus — on-disk project layout and path containment.

Every paper lives under ``~/.kiro/crew/apps/papyrus/data/projects/<name>/`` (via
:func:`kiro_crew.apps.manager.app_data_dir`, the platform-standard app-scoped
data dir). Nothing is stored outside that tree and nothing is uploaded anywhere.

``root`` is accepted on every function (mirroring ``issue_radar``'s ``store.py``)
so tests can point at a tmp dir instead of the real app data dir.

**This module is the app's path-containment gate.** A LaTeX editor writes
user-controlled relative paths and hands a directory to an external compiler, so
two rules hold everywhere:

* every caller-supplied path goes through :func:`safe_child` before it reaches
  the filesystem — it rejects absolute paths, ``..`` in any segment, backslashes
  (a Windows-style separator that would sidestep a ``/``-only check), NUL bytes,
  and — after ``resolve()`` — anything that does not land inside the project;
* a project NAME goes through :func:`safe_project_dir`, which additionally
  refuses anything that is not a single slug segment, so a project name can
  never introduce a path separator of its own.

Both are synchronous filesystem code: call them from a worker thread
(``asyncio.to_thread`` / ``run_in_executor``), never on the event loop for a
large tree.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import unicodedata
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.hooks import safe_read_file_bytes_nolink
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger("kirocrew.app.papyrus")

APP_NAME = "papyrus"

#: Per-project config file holding the chosen main ``.tex`` document.
PROJECT_CONFIG_FILENAME = ".papyrus.json"

#: The document compiled when a project has no configured main file.
DEFAULT_MAIN_FILE = "main.tex"

#: Candidate main documents probed, in order, when ``main.tex`` is absent.
MAIN_FILE_CANDIDATES = ("main.tex", "paper.tex", "article.tex", "manuscript.tex")

#: Config key holding a display name the USER chose for the paper.
#:
#: Separate from the directory name on purpose: the directory name is the app's
#: identifier — it appears in every route (``?name=``), in the PDF URL, in the
#: "last opened paper" pointer and in the key of the paper's co-author session —
#: so renaming the directory would break links a rename has no business
#: touching. This key renames only what the list DISPLAYS.
PROJECT_TITLE_KEY = "title"

#: Ceiling on a displayed title, in characters.
#:
#: Both sources are untrusted (see :func:`project_title`), so a title is
#: attacker-influenced text landing in a table cell: without a cap, a one-line
#: ``\title{}`` holding 40 kB of text is a layout attack on the paper list, and
#: every project row carries it in the list payload.
MAX_TITLE_CHARS = 120

#: How much of a raw title :func:`sanitize_title` works on. Generous against the
#: 120-char display cap (LaTeX markup flattens away), small enough that the work
#: stays bounded whatever a cloned ``.papyrus.json`` ships. The display cap itself
#: is applied by the route AFTER redaction, never here: cutting first can split a
#: credential across the boundary, and the half left behind does not match the
#: pattern that would have redacted it.
_TITLE_INPUT_CHARS = MAX_TITLE_CHARS * 64

#: How much of the main document :func:`extract_title` reads, in bytes.
#:
#: ``\title{}`` belongs in the preamble, so a bounded prefix finds it in every
#: conventional document while keeping the cost of listing N projects bounded —
#: the alternative is reading each paper whole (up to
#: :data:`MAX_FILE_BYTES`) on every request to ``GET /projects``. A document that
#: declares its title past this point simply has no extractable title, which is
#: the same answer as having none at all: the directory name is shown.
TITLE_SCAN_BYTES = 64 * 1024

#: ``\title`` with its optional short form: ``\title[Papyrus]{The long one}``.
#:
#: The braced argument is NOT matched here — ``[^}]*`` would stop at the first
#: closing brace and truncate ``\title{A \textbf{bold} claim}`` to
#: ``A \textbf{bold``. :func:`_braced_group` walks the braces instead.
_RE_TITLE = re.compile(r"\\title\s*(?:\[(?P<short>[^\]]*)\])?\s*\{")

#: A TeX comment: an unescaped ``%`` to end of line.
#:
#: Stripped before the title is looked for, because a commented-out alternative
#: title above the real one is ordinary in a paper under revision, and taking it
#: would show a title the document does not typeset.
_RE_TEX_COMMENT = re.compile(r"(?<!\\)%.*?$", re.MULTILINE)

#: Groups dropped whole from a title: author-note commands whose content is
#: never part of the title as typeset.
_RE_TITLE_NOTE = re.compile(r"\\(?:thanks|footnote|footnotemark|label)\s*\{")

#: One pass over a title's markup. The alternation order is the whole design:
#:
#: * ``\%`` and friends are BACKSLASH-ESCAPED LITERALS — the character is part of
#:   the title's text (``90\%``), so it is kept, and it must be recognised before
#:   the generic control-sequence branch, which would eat it as a one-character
#:   command and silently print ``90 and above``;
#: * any other control sequence becomes a space, keeping its braced content
#:   (``\textbf{x}`` -> ``x``): the markup goes, the words stay;
#: * a bare brace or ``~`` becomes a space.
#:
#: Done as ONE pass rather than three substitutions because sequential passes
#: reintroduce each other's output: unescaping ``\{`` first hands a literal brace
#: to a later brace-stripping pass, which then deletes it.
#:
#: ``\$`` inside the class is a semantic no-op (a literal ``$`` either way) written
#: that way on purpose: ``test_regex_anchor_contract`` walks every pattern in this
#: package for a ``$`` that could be a trailing-newline-permissive ANCHOR, and its
#: heuristic does not parse character classes. Do not "simplify" the escape away —
#: ``cloud/login_target.py`` spells its own literal dollar the same way.
_RE_TEX_TOKEN = re.compile(r"\\([%&_\$#{}])|\\(?:[A-Za-z@]+\*?\s*|.)|[{}~]")

#: Control characters that SEPARATE words, so they collapse to a space instead of
#: being deleted: ``"first\nsecond"`` is two words, not ``firstsecond``. Every
#: other ``Cc``/``Cf`` character is removed outright — see :func:`sanitize_title`.
_WHITESPACE_CONTROLS = frozenset("\t\n\v\f\r\x85")

#: A project name must be one lowercase slug segment. Anything else (a slash, a
#: dot, a leading dash) is refused rather than sanitized, because a "cleaned up"
#: name silently addresses a different project than the one the user typed.
#: Anchored at ``\Z``: Python's ``$`` matches before a trailing newline, so a
#: ``$`` anchor would accept ``"name\n"`` and mint a directory whose name
#: carries the newline into every later ``git``/``pdflatex`` argv.
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")

#: Ceiling on how many files ``list_files`` will walk/return. A cloned repo can
#: contain a build tree; an unbounded walk would be both a slow response and an
#: unbounded JSON body.
MAX_PROJECT_FILES = 4000

#: Ceiling on a single file body accepted by ``write_file`` / returned by
#: ``read_text_file``. Generous for a thesis, small enough that a hostile client
#: cannot balloon the gateway's memory.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Suffixes that mark a LaTeX build artifact rather than paper source.
#:
#: Read only by ``is_artifact``. ``list_files`` does NOT filter on it, so the
#: tree the API returns still carries artifacts and the UI is what drops them
#: (``website/src/apps/papyrus/lib.ts``). The two lists have already drifted:
#: the UI also lists ``.pdf`` and spells ``.synctex.gz`` as one suffix, where
#: this set has ``.synctex`` and ``.gz`` separately -- so this one would also
#: hide any plain ``.gz``. Reconcile them before wiring either side to the
#: other.
ARTIFACT_SUFFIXES = frozenset(
    {
        ".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out",
        ".synctex", ".gz", ".toc", ".lof", ".lot", ".nav", ".snm", ".vrb",
    }
)


class PathRejected(Exception):
    """A caller-supplied project name or relative path was refused."""


@dataclass(frozen=True)
class ProjectSummary:
    """One row of the project list."""

    name: str
    modified: float
    has_pdf: bool
    #: What to DISPLAY for this paper — see :func:`project_title`. Never the
    #: identifier: ``name`` stays the key every route and stored pointer uses.
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "modified": self.modified,
            "has_pdf": self.has_pdf,
            # Falls back to the identifier rather than shipping "", so a client
            # can render `title` unconditionally and never print an empty row.
            "title": self.title or self.name,
        }


def data_dir(root: Path | None = None) -> Path:
    """Return the app's data dir, creating it if missing."""
    data = root if root is not None else app_data_dir(APP_NAME)
    data.mkdir(parents=True, exist_ok=True)
    return data


def projects_dir(root: Path | None = None) -> Path:
    """Return ``<data>/projects``, creating it if missing."""
    d = data_dir(root) / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_project_name(raw: str) -> str:
    """Slugify a user-typed project name: trim, collapse spaces to hyphens, lower.

    The RESULT is still validated by :func:`safe_project_dir`, so this only
    handles the friendly cases (``"My Paper"`` -> ``"my-paper"``); it never makes
    a traversal attempt safe.
    """
    return re.sub(r"\s+", "-", (raw or "").strip()).lower()


#: ``os.path.isjunction`` exists only on Python 3.12+. Preferred when present; the
#: fallback below covers 3.10/3.11, which this project still supports.
_ISJUNCTION = getattr(os.path, "isjunction", None)

#: Windows marks every reparse point (junction, symlink, and others) with this
#: attribute bit. ``stat`` exports it on every platform and every supported
#: version, so it is safe to reference unconditionally.
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

#: The reparse tag for a directory junction (a "mount point"). ``stat`` does not
#: export a constant for it, so the documented value is named here.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _is_junction_fallback(path: Path) -> bool:
    """``os.path.isjunction`` for Python 3.10/3.11, which lack it.

    Mirrors what CPython's own implementation does: a junction is a reparse point
    (``FILE_ATTRIBUTE_REPARSE_POINT``) whose tag is ``IO_REPARSE_TAG_MOUNT_POINT``.
    Both fields are Windows-only additions to ``os.stat_result``, so their absence
    off Windows makes this ``False`` — which is correct, since junctions do not
    exist there.

    ``follow_symlinks=False``: the question is what THIS name is, not what it
    points at.
    """
    try:
        info = path.stat(follow_symlinks=False)
    except (OSError, ValueError, TypeError):
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    if not attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return getattr(info, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def is_reparse_link(path: Path) -> bool:
    """True when *path* is a symlink **or** a Windows directory junction.

    A junction is a reparse point that ``islink()`` / ``is_symlink()`` does NOT
    report, so a symlink-only guard is bypassable on Windows by the one link type
    a user can create there without elevation. ``resolve()`` follows a junction
    like any other indirection, so every place this module refuses a link at a name
    KiroCrew owns has to refuse a junction too, or the refusal is POSIX-only.

    Detected WITHOUT depending on ``os.path.isjunction``'s availability: that
    helper only exists on 3.12+, and this project supports 3.10, so keying the
    guard on it left the protection silently absent on two supported interpreters —
    the failure mode being a no-op guard, which is worse than a loud one.

    Shared so the project-entry guard and ``gitops``'s attributes guard cannot
    drift apart on which link types they cover.
    """
    if path.is_symlink():
        return True
    if _ISJUNCTION is not None:
        try:
            return bool(_ISJUNCTION(str(path)))
        except (OSError, ValueError):
            return False
    return _is_junction_fallback(path)


def safe_project_dir(name: str, root: Path | None = None) -> Path:
    """Return the directory for project *name*, or raise :class:`PathRejected`.

    The name must match :data:`PROJECT_NAME_RE` — ONE slug segment — so it can
    never contribute a path separator, a ``..``, a drive letter, or a leading
    dash that a later ``git``/``pdflatex`` argv could read as an option. The
    resolved directory is then re-checked for STRICT containment under
    ``projects_dir()``, and a symlink or junction at the project entry is refused
    outright.
    """
    if not PROJECT_NAME_RE.match(name or ""):
        raise PathRejected("invalid project name")
    base = projects_dir(root)
    candidate = base / name
    # A LINK at the project entry is illegitimate wherever it points — same
    # reasoning as `_config_path`: this is a directory KiroCrew owns and creates.
    # Refused before containment is asked about, because a link can satisfy
    # containment while still not being the directory it claims to be.
    #
    # Junctions included: they are the link type a Windows user can create WITHOUT
    # elevation, and `is_symlink()` does not report them — so a symlink-only check
    # left the whole guard bypassable on exactly the platform this PR adds.
    if is_reparse_link(candidate):
        raise PathRejected("project path is a symlink")
    try:
        resolved = candidate.resolve()
        base_resolved = base.resolve()
    except OSError as exc:
        raise PathRejected("project path could not be resolved") from exc
    # STRICT containment: `resolved` must be a CHILD of the projects dir, never the
    # projects dir itself. The old check allowed `resolved == base_resolved`, and
    # `projects/<name> -> .` satisfied exactly that — making every other paper a
    # "child" of the fake project, so `safe_child` then happily resolved
    # `other-paper/main.tex` as an in-project path (cross-project read AND write),
    # and `DELETE /project` ran `rmtree` on the projects ROOT, destroying every
    # paper. `projects_dir` itself is never a project, so nothing legitimate needs
    # the equality case.
    if base_resolved not in resolved.parents:
        raise PathRejected("project path escapes the projects directory")
    if is_sensitive_path(str(resolved)):
        raise PathRejected("project path is sensitive")
    return resolved


def safe_child(project: Path, relative: str) -> Path:
    """Resolve *relative* inside *project*, or raise :class:`PathRejected`.

    Forward slashes in the middle are allowed so papers with ``sections/intro.tex``
    work (a ``\\input{sections/intro}`` is the norm in conference templates).
    Everything else is refused:

    * empty, or longer than a filesystem component budget;
    * an absolute POSIX path (``/etc/passwd``) or a Windows/UNC one
      (``C:\\...``, ``\\\\host\\share``) — backslash is rejected outright, since
      it IS a separator on Windows and would otherwise pass a ``/``-only check;
    * any ``..`` segment, anywhere;
    * a NUL byte (truncates the path at the syscall boundary);
    * a resolved target outside *project* — this is what catches a **symlink
      escape**, where every segment looks innocent but a link points out of the
      tree (a cloned repo can ship one);
    * anything under ``.git`` — see below;
    * a resolved target the shared sensitive-path gate rejects, so a project that
      somehow sits beside a credential store still cannot read it.

    **``.git`` is refused outright**, because it is not document content: it is the
    machinery that decides what ``git`` EXECUTES. ``.git/config`` names
    ``filter.<x>.clean`` and ``core.*Command`` programs, ``.git/info/attributes``
    is the highest-precedence attributes source, and ``.git/hooks/*`` is run
    directly — so a write there converts "edit a file in my paper" into code
    execution on the next commit or push, on the one path that deliberately keeps
    ``~/.ssh`` readable. Containment does not cover this: those paths are all
    legitimately INSIDE the project, so every other rule here passes them. The
    app's own git work goes through :mod:`.gitops`, and ``list_files`` already
    hides dotfiles, so nothing legitimate is lost.
    """
    if not relative or len(relative) > 1024:
        raise PathRejected("invalid path")
    if "\0" in relative or "\\" in relative:
        raise PathRejected("invalid path")
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        raise PathRejected("invalid path")
    parts = relative.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PathRejected("invalid path")
    try:
        resolved = (project / relative).resolve()
        project_resolved = project.resolve()
    except OSError as exc:
        raise PathRejected("path could not be resolved") from exc
    if project_resolved not in resolved.parents:
        raise PathRejected("path escapes the project")
    # Checked on the REQUESTED segments **and** the RESOLVED ones. Neither alone is
    # sufficient, because a symlink can move `.git` in either direction:
    #
    #   * requested-only misses `meta -> .git` + `meta/config` — no `.git` component
    #     in the request, resolves straight into the machinery;
    #   * resolved-only misses `.git/config -> ../repo-config` — the request names
    #     `.git`, but resolution lands outside it, so the resolved parts are clean
    #     while git still READS the file through its own path.
    #
    # The second is the subtler one: it is not about where the bytes live, it is that
    # the app must not write anything git will treat as its config, whatever the file
    # is really called.
    #
    # Case-insensitively, because macOS and Windows resolve `.GIT` to the same
    # directory; at any depth, because a submodule's `.git` has the same execution
    # surface. The resolved check is against the PROJECT-RELATIVE part only, so a
    # `.git` component in the absolute path ABOVE the project (a data home that
    # itself sits inside a checkout) cannot refuse every legitimate file.
    if any(p.lower() == ".git" for p in parts):
        raise PathRejected("invalid path")
    try:
        rel_parts = resolved.relative_to(project_resolved).parts
    except ValueError:  # pragma: no cover - containment above already guarantees this
        raise PathRejected("path escapes the project") from None
    if any(p.lower() == ".git" for p in rel_parts):
        raise PathRejected("invalid path")
    if is_sensitive_path(str(resolved)):
        raise PathRejected("path is sensitive")
    return resolved


def _config_path(project: Path) -> Path | None:
    """The project's config file, or ``None`` when it is not contained.

    ``.papyrus.json`` is a file a CLONED REPOSITORY can ship, including as a symlink —
    and both accessors below follow one: the reader would ``read_text`` whatever it
    points at, and the writer would replace it. So it goes through :func:`safe_child`
    like every other path in this module, which resolves the link and applies the
    sensitive-path gate.

    This is the same omission the deck readers, ``pdf_path`` and ``resolve_main_file``
    each had: a filename the code supplies itself still lands in a directory the
    repository controls.
    """
    # A SYMLINK at the literal path is refused outright, before containment is even
    # asked about. `safe_child` answers "does this resolve inside the project", and an
    # IN-PROJECT link satisfies it — so `.papyrus.json -> paper.tex` passed, and
    # `set_main_file` (reached from `resolve_main_file`, i.e. every compile) replaced the
    # user's manuscript with JSON. Containment was never the whole question here: this
    # file is one KiroCrew owns and writes, so a link at that name is illegitimate
    # wherever it points.
    #
    # Same reasoning as the generated-artifact guard in `latex`: for a path the app
    # writes by name, the presence of a link is itself the problem.
    #
    # `is_reparse_link`, not `is_symlink()`: a Windows directory JUNCTION is a reparse
    # point `is_symlink()` does not report, and it is the one link type a user can
    # create there without elevation, so a symlink-only check makes this refusal
    # POSIX-only -- which is exactly what that helper's docstring says every guard in
    # this module must avoid. A junction is directory-only, so it cannot stand in for
    # the `.papyrus.json -> paper.tex` overwrite above; what it does is slip past the
    # refusal, take the write no further than an opaque later failure, and lose the
    # warning that says why.
    candidate = project / PROJECT_CONFIG_FILENAME
    try:
        if is_reparse_link(candidate):
            logger.warning(
                "papyrus: refused a linked project config in %s", project.name
            )
            return None
    except OSError:  # pragma: no cover - defensive
        return None
    try:
        return safe_child(project, PROJECT_CONFIG_FILENAME)
    except PathRejected:
        logger.warning("papyrus: refused an uncontained project config in %s", project.name)
        return None


class TitleRejected(ValueError):
    """A non-blank rename that sanitizes to nothing (``\\LaTeX``, ``~``).

    Only a blank field clears the override; a name with no displayable text is
    refused rather than read as a request to clear.
    """


class ConfigWriteRefused(Exception):
    """``.papyrus.json`` exists as a link or outside the project, so it is not written.

    Raised by the one caller whose success the user sees — a rename — instead of
    answering "saved" over a write that never happened. :func:`set_main_file`
    keeps the silent no-op: the compile path calls it implicitly and must not fail.
    """


# Weak values: a lock lives only while a writer holds it, so a deleted paper leaves
# no entry behind. A concurrent writer still gets the SAME lock, because the one
# holding it keeps it alive for as long as it matters.
_CONFIG_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
_CONFIG_LOCKS_GUARD = threading.Lock()


def _config_lock(project: Path) -> threading.Lock:
    """Return the lock serializing *project*'s config read-modify-write.

    Two mutators (the main document and the display title) each read the whole
    file and write it back; run concurrently, both read the same stale config and
    the later write silently discards the other's key. Keyed per project so two
    papers never wait on each other.
    """
    key = str(project)
    with _CONFIG_LOCKS_GUARD:
        return _CONFIG_LOCKS.setdefault(key, threading.Lock())


def read_project_config(project: Path) -> dict[str, Any]:
    """Read ``.papyrus.json``, returning ``{}`` when absent, corrupt or uncontained."""
    path = _config_path(project)
    if path is None or not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_project_config(project: Path, config: dict[str, Any]) -> bool:
    """Persist ``.papyrus.json`` atomically (crash mid-write keeps the old file).

    Refuses an uncontained path for the mirror of the reason the reader does: a
    symlinked config would have this WRITE land on whatever it points at.
    Returns whether it wrote, so a caller that must not report a refused write
    as saved can tell the two apart.

    Also refuses once the project directory is gone: a write that lands after
    :func:`delete_project` would recreate the directory, and the deleted paper's
    name would stay taken. Callers hold :func:`_config_lock`, which the delete
    holds too, so the check and the write cannot straddle a removal.
    """
    path = _config_path(project)
    if path is None or not project.is_dir():
        return False
    atomic_write(path, json.dumps(config, indent=2), fsync=True)
    return True


def get_main_file(project: Path) -> str:
    """Return the configured main ``.tex`` file, defaulting to ``main.tex``.

    The configured value is UNTRUSTED: ``.papyrus.json`` can arrive inside a
    cloned repository, so a hostile one could name ``../../etc/passwd.tex`` and
    pivot through the PDF-serving route. It is therefore re-validated through
    :func:`safe_child` on every read and ignored when it fails.
    """
    configured = read_project_config(project).get("main_file")
    if isinstance(configured, str) and configured:
        try:
            safe_child(project, configured)
        except PathRejected:
            logger.warning("papyrus: ignoring unsafe main_file in %s", project.name)
        else:
            return configured
    return DEFAULT_MAIN_FILE


def resolve_main_file(project: Path) -> str | None:
    """Resolve the main document, persisting a non-default discovery.

    Order: the configured value, then :data:`MAIN_FILE_CANDIDATES`, then the
    first ``*.tex`` in sorted order. Returns ``None`` when the project holds no
    ``.tex`` file at all.
    """
    # Every candidate is probed through `_contained_file`, never `(project / x)`
    # directly. A cloned repo can ship `main.tex` as a SYMLINK to a document outside
    # the project, and the compiler is then pointed at that path — so external content
    # is typeset and served, and the escape is invisible to a plain `is_file()` because
    # every path segment looks innocent. Same omission as `pdf_path` had, one function
    # over: a name that is derived rather than caller-supplied still lands in a
    # directory the repository controls.
    main_file = get_main_file(project)
    if _contained_file(project, main_file):
        return main_file
    for candidate in MAIN_FILE_CANDIDATES:
        if _contained_file(project, candidate):
            if candidate != DEFAULT_MAIN_FILE:
                set_main_file(project, candidate)
            return candidate
    # The glob branch too: `p.is_file()` follows a link exactly as the probes above do.
    tex_files = sorted(
        p.name for p in project.glob("*.tex") if _contained_file(project, p.name)
    )
    if not tex_files:
        return None
    discovered = tex_files[0]
    if discovered != DEFAULT_MAIN_FILE:
        set_main_file(project, discovered)
    return discovered


def _contained_file(project: Path, relative: str) -> bool:
    """True when *relative* is a regular file that stays inside *project*.

    The containment half is what a bare ``(project / relative).is_file()`` misses: it
    follows symlinks, so a cloned repository shipping ``main.tex -> /etc/passwd`` (or
    anything else readable) passes it. :func:`safe_child` resolves the link and rejects
    the escape.
    """
    try:
        return safe_child(project, relative).is_file()
    except PathRejected:
        return False


#: Deletions performed while a lock was alive, keyed WEAKLY by that lock: a
#: rename that holds the lock object sees every delete made meanwhile, and the
#: count goes away with the lock, so nothing accumulates per deleted paper.
_DELETIONS: weakref.WeakKeyDictionary[threading.Lock, int] = weakref.WeakKeyDictionary()


def deletion_count(lock: threading.Lock) -> int:
    """How many deletes ran under *lock* (see :data:`_DELETIONS`)."""
    return _DELETIONS.get(lock, 0)


def _delete_locked(project: Path, lock: threading.Lock) -> bool:
    """Body of :func:`delete_project`; the caller holds *lock*."""
    _DELETIONS[lock] = _DELETIONS.get(lock, 0) + 1
    return platform_compat.rmtree_force(project)


def delete_project(project: Path) -> bool:
    """Remove *project*'s tree, returning whether it is gone afterwards.

    Holds the config lock so a rename or main-document write in flight either
    finishes before the removal or sees the directory gone and writes nothing.
    """
    lock = _config_lock(project)
    with lock:
        return _delete_locked(project, lock)


def set_main_file(project: Path, main_file: str) -> None:
    """Set the main document (validated), preserving other config keys."""
    safe_child(project, main_file)
    with _config_lock(project):
        config = read_project_config(project)
        config["main_file"] = main_file
        write_project_config(project, config)


def _braced_group(text: str, open_brace: int) -> str | None:
    """Return the content of the ``{...}`` group starting at *open_brace*.

    Walks nesting so a title carrying its own groups survives whole, and stops
    at the matching close; returns ``None`` for an unbalanced group, which is a
    truncated read as often as it is a broken document — either way there is no
    title to show.

    ``\\{`` is not a brace: an escaped brace is a literal character in the
    title's text, and counting it would end the group one character into
    ``\\title{a \\{ b}``.
    """
    depth = 0
    index = open_brace
    limit = len(text)
    while index < limit:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index]
        index += 1
    return None


def _flatten_tex(raw: str) -> str:
    """Reduce a title's LaTeX source to the words it typesets.

    Deliberately small: this renders nothing, it only removes the markup that
    would otherwise be READ ALOUD in a table cell (``\\textbf``, ``\\\\``, ``~``).
    Author notes are dropped with their content first — dropping the command
    alone would splice the note INTO the title — and :data:`_RE_TEX_TOKEN` then
    does the rest in one pass.
    """
    text = raw
    while (match := _RE_TITLE_NOTE.search(text)) is not None:
        group = _braced_group(text, match.end() - 1)
        if group is None:
            text = text[: match.start()]
            break
        # `match.end()` is one past the opening brace, so the group's content
        # ends at `match.end() + len(group)` — the index OF the closing brace.
        text = text[: match.start()] + text[match.end() + len(group) + 1 :]
    return _RE_TEX_TOKEN.sub(lambda m: m.group(1) or " ", text)


def sanitize_title(raw: str) -> str:
    """Normalize an untrusted title for display: strip, flatten, bound.

    Removes Unicode control and FORMAT characters (categories ``Cc``/``Cf``)
    rather than only the ASCII controls: both title sources can arrive inside a
    cloned repository, and the format class is where a bidi override lives — one
    of those in a paper name reorders the surrounding row for every reader.

    A control that SEPARATES words becomes a space rather than vanishing
    (:data:`_WHITESPACE_CONTROLS`), so a two-line title reads as two words and a
    newline still cannot break out of its cell. Deleting them uniformly is what
    turns ``"first\\nsecond"`` into ``firstsecond``.

    Returns ``""`` when nothing displayable survives, which every caller reads as
    "no title", never as an empty name.

    Does NOT apply :data:`MAX_TITLE_CHARS`: the route cuts to that length after
    redacting, so a token that straddles the cap is redacted whole instead of
    being split into a fragment the redactor does not recognize.
    """
    # Bound the WORK, not just the result: `.papyrus.json` is unbounded and
    # clone-supplied, and the note-stripping loop recopies the remainder once per
    # note, so capping only the output leaves a crafted title quadratic. The
    # margin lets markup that flattens away still yield a full-length title.
    head = raw[:_TITLE_INPUT_CHARS]
    if len(raw) > _TITLE_INPUT_CHARS:
        # Drop the word the cut landed in: a credential split here would leave a
        # prefix the redactor does not recognize, so it goes whole or not at all.
        cut = len(head)
        while cut and not head[cut - 1].isspace():
            cut -= 1
        head = head[:cut]
    flattened = _flatten_tex(head)
    stripped = "".join(
        " " if char in _WHITESPACE_CONTROLS
        else char if unicodedata.category(char) not in ("Cc", "Cf")
        else ""
        for char in flattened
    )
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed


def extract_title(project: Path, main_file: str) -> str:
    """Return the ``\\title{}`` of *main_file*, or ``""`` when it declares none.

    Reads a bounded prefix (:data:`TITLE_SCAN_BYTES`) through :func:`safe_child`,
    like every other read in this module: ``main_file`` can be a name a cloned
    repository chose.

    A beamer-style short title wins: ``\\title[Papyrus]{Papyrus: a LaTeX…}``
    declares ``Papyrus`` as the form for a cramped slot, and a list row is
    exactly that. Comments are stripped first so a commented-out alternative
    title is not mistaken for the live one.
    """
    try:
        path = safe_child(project, main_file)
    except PathRejected:
        logger.warning("papyrus: refused an uncontained main file in %s", project.name)
        return ""
    # Opened without following a link and checked against the project root on
    # the descriptor itself: a pull can swap `main.tex` for a symlink after
    # `safe_child`, and the list would otherwise title a paper with outside text.
    raw = safe_read_file_bytes_nolink(
        str(path),
        str(project.resolve()),
        max_bytes=TITLE_SCAN_BYTES,
        allow_truncate=True,
    )
    if raw is None:
        return ""
    source = _RE_TEX_COMMENT.sub("", raw.decode("utf-8", errors="replace"))
    match = _RE_TITLE.search(source)
    if match is None:
        return ""
    short = match.group("short")
    if short and sanitize_title(short):
        return sanitize_title(short)
    group = _braced_group(source, match.end() - 1)
    return sanitize_title(group) if group is not None else ""


def project_title(project: Path, main_file: str | None) -> str:
    """Resolve what the paper list should CALL this project.

    In order: the name the user set, then the document's own ``\\title{}``, then
    the directory name. The user's choice comes first by design — it is the only
    one of the three they can act on, and a document title that outranked it
    would make renaming a paper that declares one do nothing at all.

    Both of the first two are UNTRUSTED text — ``.papyrus.json`` can arrive
    inside a cloned repository exactly as the ``.tex`` does — so both go through
    :func:`sanitize_title`, and an empty result falls through to the next source
    rather than showing a blank row.
    """
    configured = read_project_config(project).get(PROJECT_TITLE_KEY)
    if isinstance(configured, str) and (chosen := sanitize_title(configured)):
        return chosen
    if main_file and (extracted := extract_title(project, main_file)):
        return extracted
    return project.name


def config_lock(project: Path) -> threading.Lock:
    """The lock a caller holds across authorizing *project* and writing its config.

    :func:`set_project_title_locked` expects it held: taken BEFORE the route's
    existence check, it keeps a delete (which holds it too) and a same-name
    create from landing between that check and the write.
    """
    return _config_lock(project)


def set_project_title(project: Path, title: str) -> str:
    """Set the display name under :func:`config_lock`, returning the resolved title.

    Returns what the list will now show, so a caller does not have to re-resolve
    it to answer the request. The resolution runs AFTER the lock is released:
    :func:`resolve_main_file` can persist a main document, which takes the same
    (non-reentrant) lock.
    """
    with _config_lock(project):
        set_project_title_locked(project, title)
    return project_title(project, resolve_main_file(project))


def set_project_title_locked(project: Path, title: str) -> None:
    """Set (or clear) the user's display name; the caller holds :func:`config_lock`.

    An empty *title* REMOVES the override instead of storing a blank, so the
    field doubles as "go back to the document's own title" — which is the only
    way back once a paper has been renamed, and needs no second control.
    """
    cleaned = sanitize_title(title)
    if title.strip() and not cleaned:
        raise TitleRejected(project.name)
    config = read_project_config(project)
    if cleaned:
        config[PROJECT_TITLE_KEY] = cleaned
    else:
        config.pop(PROJECT_TITLE_KEY, None)
    # The path is re-checked at write time: a link at `.papyrus.json`, or a
    # directory deleted meanwhile, is refused there and must not answer as saved.
    if not write_project_config(project, config):
        raise ConfigWriteRefused(project.name)


def pdf_path(project: Path, main_file: str) -> Path | None:
    """Return the PDF the compiler emits for *main_file*, or ``None`` if uncontained.

    Goes through :func:`safe_child` like every other caller-influenced path in this
    module. It looks derived-and-therefore-safe — the stem comes from the configured
    main file and the suffix is a literal — but the RESULT is a name in a directory a
    cloned repository controls, and ``safe_child`` is what resolves symlinks.

    A repo shipping ``main.pdf -> ~/.kiro/crew/.local_secret`` had that file served
    verbatim by the ``/pdf`` route, which renders it inline in the browser. Plain
    concatenation could not see it: every segment is innocent and the link is the whole
    trick. This is the same containment the editor's file reads have always had; the
    PDF was simply never routed through it.

    ``None`` rather than raising, because every caller here already treats a missing
    PDF as "not compiled yet" and an uncontained one deserves exactly that answer —
    they are indistinguishable to a user and neither is servable.
    """
    try:
        return safe_child(project, Path(main_file).stem + ".pdf")
    except PathRejected:
        logger.warning("papyrus: refused an uncontained PDF path in %s", project.name)
        return None


def list_files(project: Path) -> list[str]:
    """Return POSIX-style relative paths of the project's editable files.

    Hidden entries are skipped (``.git`` would explode the tree, ``.papyrus.json``
    is config), symlinks are skipped entirely (a link is not a file the editor
    should follow — and following one is how a tree walk escapes containment),
    and the walk is bounded by :data:`MAX_PROJECT_FILES`.

    Synchronous filesystem walk — call it off the event loop.
    """
    files: list[str] = []
    stack = [project]
    while stack and len(files) < MAX_PROJECT_FILES:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            # `is_reparse_link`, not `is_symlink()`: a junction is a DIRECTORY
            # link `is_symlink()` does not report, so it fell through to the
            # `is_dir()` arm below and the walk followed it out of the project,
            # returning outside names under project-relative paths.
            if is_reparse_link(entry):
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                files.append(entry.relative_to(project).as_posix())
                if len(files) >= MAX_PROJECT_FILES:
                    break
    files.sort()
    return files


def list_projects(root: Path | None = None) -> list[ProjectSummary]:
    """Return every project that has a resolvable main document.

    Synchronous filesystem scan — call it off the event loop.
    """
    out: list[ProjectSummary] = []
    for entry in sorted(projects_dir(root).iterdir()):
        # Same helper as `safe_project_dir`, which refuses a linked project entry
        # outright: a junction under `projects/` is a directory `is_symlink()`
        # misses, so it was enumerated here as a real project.
        if not entry.is_dir() or is_reparse_link(entry) or entry.name.startswith("."):
            continue
        main_file = resolve_main_file(entry)
        if main_file is None:
            continue
        tex = entry / main_file
        try:
            modified = tex.stat().st_mtime
        except OSError:
            continue
        out.append(
            ProjectSummary(
                name=entry.name,
                modified=modified,
                # An uncontained PDF reads as "no PDF": not servable either way.
                has_pdf=bool((_pdf := pdf_path(entry, main_file)) and _pdf.is_file()),
                title=project_title(entry, main_file),
            )
        )
    return out


def read_text_file(project: Path, relative: str) -> str:
    """Read a project file as UTF-8 text.

    Raises :class:`PathRejected` for a refused path, ``FileNotFoundError`` when
    absent, ``ValueError`` when the file is binary or over
    :data:`MAX_FILE_BYTES`.
    """
    target = safe_child(project, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError("file too large")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("binary file") from exc


def write_file(project: Path, relative: str, content: str) -> None:
    """Write a project file atomically, creating parent directories as needed.

    ``newline=""`` so a document that is read, edited and saved repeatedly lands
    byte-for-byte instead of accumulating carriage returns on Windows.
    """
    target = safe_child(project, relative)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("content too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, content, newline="")


def create_file(project: Path, relative: str, content: str = "") -> None:
    """Create a project file, refusing to clobber an existing one.

    Uses EXCLUSIVE creation (``O_CREAT | O_EXCL``) rather than an ``exists()`` probe
    followed by a write. The probe leaves a check/use window, and this function runs on
    worker threads, so two concurrent creates for the same path both passed it and both
    answered 201 — while ``write_file``'s atomic replace meant the second silently
    overwrote the first. Both callers were told their content was created; only one
    copy survived.

    ``O_EXCL`` is the filesystem answering the same question with no window in front of
    it. The size check stays ahead of the open, so an oversized body is refused before
    anything is created.
    """
    target = safe_child(project, relative)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("content too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    # `x` is `O_CREAT | O_EXCL`: it raises FileExistsError itself, which is exactly the
    # error the probe raises, so callers need no second case. `newline=""` matches
    # `write_file` so a round-tripped document does not accumulate carriage returns.
    with open(target, "x", encoding="utf-8", newline="") as handle:
        handle.write(content)


def delete_file(project: Path, relative: str) -> None:
    """Delete a project file. The main document is refused.

    The guard compares RESOLVED PATHS, not the request string against the configured
    name. A string comparison protects only the exact spelling in ``.papyrus.json``,
    and a cloned repository can contain a symlink: with ``alias.tex -> main.tex``
    configured as the main file, deleting ``main.tex`` passed the check (the strings
    differ) and removed the real document, leaving the configured main dangling and
    every compile broken. The file tree offers exactly that deletion.

    ``safe_child`` already refuses the string-level dodges (``./main.tex``,
    ``sections/../main.tex``, a trailing slash), so a symlink is the case it cannot
    see — and it is the one a hostile or merely careless repository actually brings.
    """
    target = safe_child(project, relative)
    main = safe_child(project, get_main_file(project))
    # `os.path.realpath`, not `Path.resolve(strict=True)`: the main document may not
    # exist yet (a fresh project, or a config naming a file the clone omitted), and a
    # missing main must not make deletion of everything else raise.
    if os.path.realpath(target) == os.path.realpath(main):
        raise ValueError("cannot delete the main document")
    if not target.is_file():
        raise FileNotFoundError(relative)
    target.unlink()


def is_artifact(relative: str) -> bool:
    """True when *relative* is a LaTeX build artifact rather than source."""
    return Path(relative).suffix.lower() in ARTIFACT_SUFFIXES
