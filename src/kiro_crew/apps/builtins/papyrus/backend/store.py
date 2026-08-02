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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger("kirocrew.app.papyrus")

APP_NAME = "papyrus"

#: Per-project config file holding the chosen main ``.tex`` document.
PROJECT_CONFIG_FILENAME = ".papyrus.json"

#: The document compiled when a project has no configured main file.
DEFAULT_MAIN_FILE = "main.tex"

#: Candidate main documents probed, in order, when ``main.tex`` is absent.
MAIN_FILE_CANDIDATES = ("main.tex", "paper.tex", "article.tex", "manuscript.tex")

#: A project name must be one lowercase slug segment. Anything else (a slash, a
#: dot, a leading dash) is refused rather than sanitized, because a "cleaned up"
#: name silently addresses a different project than the one the user typed.
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Ceiling on how many files ``list_files`` will walk/return. A cloned repo can
#: contain a build tree; an unbounded walk would be both a slow response and an
#: unbounded JSON body.
MAX_PROJECT_FILES = 4000

#: Ceiling on a single file body accepted by ``write_file`` / returned by
#: ``read_text_file``. Generous for a thesis, small enough that a hostile client
#: cannot balloon the gateway's memory.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Suffixes never offered as editable text (LaTeX build artifacts + binaries).
#: Filtering them here as well as in the UI keeps the two from drifting.
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

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "modified": self.modified, "has_pdf": self.has_pdf}


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


def safe_project_dir(name: str, root: Path | None = None) -> Path:
    """Return the directory for project *name*, or raise :class:`PathRejected`.

    The name must match :data:`PROJECT_NAME_RE` — ONE slug segment — so it can
    never contribute a path separator, a ``..``, a drive letter, or a leading
    dash that a later ``git``/``pdflatex`` argv could read as an option. The
    resolved directory is then re-checked for containment under
    ``projects_dir()``, which catches a symlinked project entry.
    """
    if not PROJECT_NAME_RE.match(name or ""):
        raise PathRejected("invalid project name")
    base = projects_dir(root)
    candidate = base / name
    try:
        resolved = candidate.resolve()
        base_resolved = base.resolve()
    except OSError as exc:
        raise PathRejected("project path could not be resolved") from exc
    if resolved != base_resolved and base_resolved not in resolved.parents:
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
    * a resolved target the shared sensitive-path gate rejects, so a project that
      somehow sits beside a credential store still cannot read it.
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
    candidate = project / PROJECT_CONFIG_FILENAME
    try:
        if candidate.is_symlink():
            logger.warning(
                "papyrus: refused a symlinked project config in %s", project.name
            )
            return None
    except OSError:  # pragma: no cover - defensive
        return None
    try:
        return safe_child(project, PROJECT_CONFIG_FILENAME)
    except PathRejected:
        logger.warning("papyrus: refused an uncontained project config in %s", project.name)
        return None


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


def write_project_config(project: Path, config: dict[str, Any]) -> None:
    """Persist ``.papyrus.json`` atomically (crash mid-write keeps the old file).

    Refuses an uncontained path for the mirror of the reason the reader does: a
    symlinked config would have this WRITE land on whatever it points at.
    """
    path = _config_path(project)
    if path is None:
        return
    atomic_write(path, json.dumps(config, indent=2), fsync=True)


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


def set_main_file(project: Path, main_file: str) -> None:
    """Set the main document (validated), preserving other config keys."""
    safe_child(project, main_file)
    config = read_project_config(project)
    config["main_file"] = main_file
    write_project_config(project, config)


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
            if entry.is_symlink():
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
        if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
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
    # error the probe used to raise, so callers are unchanged. `newline=""` matches
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
