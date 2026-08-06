"""Auto-registration of project directories as document Knowledge sources.

A directory the user is actually working in -- the project dir of a live chat
slot -- is registered as a watched ``local_folder`` source restricted to
documents by :mod:`kiro_crew.knowledge.doc_filter`, so a project's design docs,
specs and READMEs become searchable without the user adding the source by hand.

Two things make this safe to do without the confirmation step the manual
folder-add path uses:

* The manual path confirms because an unfiltered folder walk is unbounded --
  it will happily ingest a repository's source code. The document filter plus
  a per-sweep chunk budget makes the walk bounded, so the gate is unnecessary
  rather than skipped. The source is seeded ``sync_status: "active"`` for the
  same reason :mod:`kiro_crew.knowledge.autosource` does: no user is present
  to confirm.
* Dismissal is after-the-fact instead of before: deleting an auto-added source
  writes a tombstone (``dismissed_auto_sources``) that survives the delete, so
  a source the user removes never comes back.

The trigger is a slot's project dir rather than observation of which files the
agent read. "You are working in this project" is a stronger relevance signal
than "I read one file there", and it needs no new observation machinery --
the gateway already persists per-slot project dirs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiro_crew.security import is_sensitive_path

from . import doc_filter
from .autosource import AUTO_ADDED_PROP

logger = logging.getLogger(__name__)

#: Property naming which auto-source flavour a row is. The containment
#: invariant differs per flavour (the drop folder must stay inside the
#: workspace; a project repo root is outside it by definition), so the sweep
#: has to be able to tell them apart.
SOURCE_KIND_PROP = "source_kind"

#: Value of :data:`SOURCE_KIND_PROP` for rows this module creates.
PROJECT_DOCS_KIND = "project_docs"

#: File cap per project source. Well above the ~165 documents the filter takes
#: from a large repository, so it is a runaway backstop rather than a limit
#: reached in normal use.
DEFAULT_PROJECT_MAX_FILES = 2000


def resolve_repo_root(project_dir: str) -> str:
    """Nearest ancestor of *project_dir* containing ``.git``, else the dir itself.

    Returns ``""`` when the path is unusable: not a directory, a sensitive
    path, or a root that is the user's home directory or a filesystem root.

    The home-directory refusal is not paranoia. ``.git`` in a home directory is
    a common dotfiles setup, and without the guard a project dir anywhere under
    such a home would resolve its repo root to the whole home directory -- and
    register it. The document filter would bound WHAT is taken, but the walk
    would still traverse everything the user owns.
    """
    if not project_dir:
        return ""
    try:
        start = Path(project_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    if not start.is_dir():
        return ""
    root = start
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    resolved = str(root)
    if root == root.parent:  # filesystem root
        return ""
    try:
        if root == Path.home().resolve():
            logger.warning(
                "Not auto-registering %s as project docs: its repo root is the "
                "home directory", project_dir)
            return ""
    except (OSError, RuntimeError):
        pass
    if is_sensitive_path(resolved):
        logger.warning("Project docs root is a restricted path, skipping: %s", resolved)
        return ""
    return resolved


def project_source_properties() -> dict:
    """Properties for a project-docs source row."""
    return {
        "sync_status": "active",
        AUTO_ADDED_PROP: True,
        SOURCE_KIND_PROP: PROJECT_DOCS_KIND,
        "max_files": DEFAULT_PROJECT_MAX_FILES,
        # Nobody confirmed this source's scope, so a file symlink must not carry
        # the scan outside the repository it was registered for.
        "confine_to_root": True,
        **doc_filter.project_doc_properties(),
    }


def ensure_project_doc_source(store, root: str) -> tuple[str | None, bool]:
    """Get-or-create the project-docs source for *root*, unless it is dismissed.

    Returns ``(source_id, created)``, or ``(None, False)`` when the user
    previously deleted this source. The tombstone check, the existing-row check
    and the INSERT share one transaction inside
    ``create_auto_source_unless_dismissed`` so a concurrent delete cannot
    interleave and let a dismissed source come back.

    A folder the user already registered by hand at the same path is re-used,
    never shadowed -- and its properties are left alone, so their explicit
    choice of what to ingest is not overwritten with the document filter.
    """
    name = f"{Path(root).name or root} docs"
    try:
        return store.create_auto_source_unless_dismissed(
            name=name,
            source_type="local_folder",
            uri=root,
            properties=project_source_properties(),
        )
    except Exception:
        # Lost a race on the UNIQUE uri -- re-read and treat as pre-existing.
        existing = store.get_source_by_uri(root)
        if existing:
            return existing["id"], False
        raise


def project_source_still_valid(uri: str) -> bool:
    """Re-validate a REGISTERED project-docs source before each scan.

    Registration-time validation is not sufficient. The persisted URI is the
    literal repo root that existed then, so replacing that directory with a
    symlink to another tree afterwards would make ``os.walk`` follow it out of
    the project on the next sweep. Comparing the stored path against its own
    resolution catches exactly that: it matched at registration (the URI IS the
    resolved path), so a divergence now means the path was swapped.

    Workspace containment -- the invariant the drop-folder source re-checks --
    is the wrong test here, because a project repo root lives outside the
    workspace by design.
    """
    if not uri:
        return False
    try:
        target = Path(uri)
        resolved = target.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if str(resolved) != str(target):
        return False
    if not resolved.is_dir():
        return False
    return not is_sensitive_path(str(resolved))


def discover_and_register(store, project_dirs: list[str]) -> list[str]:
    """Register a document source for each distinct project repo root.

    Returns the ids of sources actually CREATED -- an already-registered or
    previously-dismissed root yields nothing, which is what makes repeated
    sweeps idempotent. Synchronous: it stats the filesystem and writes SQLite,
    so callers on the event loop must hand it to ``asyncio.to_thread``.
    """
    created: list[str] = []
    seen: set[str] = set()
    for project_dir in project_dirs:
        root = resolve_repo_root(project_dir)
        if not root or root in seen:
            continue
        seen.add(root)
        source_id, was_created = ensure_project_doc_source(store, root)
        if source_id and was_created:
            logger.info(
                "Auto-registered project documents: %s (source %s)", root, source_id)
            created.append(source_id)
    return created


def is_project_doc_source(props: dict) -> bool:
    """True when *props* belong to a row this module created."""
    return bool(props.get(AUTO_ADDED_PROP)) and props.get(SOURCE_KIND_PROP) == PROJECT_DOCS_KIND


__all__ = [
    "DEFAULT_PROJECT_MAX_FILES",
    "PROJECT_DOCS_KIND",
    "SOURCE_KIND_PROP",
    "discover_and_register",
    "ensure_project_doc_source",
    "is_project_doc_source",
    "project_source_properties",
    "project_source_still_valid",
    "resolve_repo_root",
]
