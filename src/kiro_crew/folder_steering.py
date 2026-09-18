"""Read a chat's folder-inherited steering directories into prompt documents.

A sidebar folder may declare extra steering roots (``steering_dirs``) that every
chat under it inherits. This module is the ONE reader for them: the
Context_Builder calls it on the non-member session-start path and on the member
essentials path, so a single admissibility check, a single inclusion rule, a
single dedup and a single double-load skip serve both. A second reader would be
a second set of those rules, which is how one of them silently diverges.

The module is pure with respect to the process -- no config load, no dashboard
state, no clock -- which is what lets the same call serve both paths and be
property-tested over real temporary trees.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path

from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter
from kiro_crew.hooks import safe_read_file
from kiro_crew.member_essential_context import _MAX_DOCUMENTS, _MAX_SOURCE_BYTES

logger = logging.getLogger(__name__)

#: Running ceiling on a single collection pass. A folder's steering roots are
#: operator-declared and validated, but an operator can still point one at a
#: very large ``.md`` tree; without a running bound the whole tree's paths and
#: bodies would be materialized before any per-document truncation, spiking a
#: worker. Each document body is already capped at ``_MAX_SOURCE_BYTES`` on read,
#: so a document-count ceiling bounds the aggregate (count x per-doc cap) and the
#: traversal together. Collection stops once the ceiling is reached. Mirrors the
#: essential-context document cap.
_MAX_FOLDER_STEERING_DOCUMENTS = _MAX_DOCUMENTS

#: Header of the rendered prompt section. Names the provenance ("this chat's
#: folder") because the bodies are operator-authored standards the model has no
#: other way to place: without it, a checklist from an org-standards repo reads
#: as if it came from the project in front of it.
FOLDER_STEERING_HEADER = (
    "[FOLDER STEERING — standards inherited from this chat's folder. "
    "Follow these as you would project steering.]"
)
FOLDER_STEERING_FOOTER = "[END FOLDER STEERING]"

#: ``inclusion`` values that mean "not every session". Compared case-folded, so
#: the ``fileMatch`` spelling the IDE writes is recognised as ``filematch``.
#: Anything else -- including an absent key -- is treated as ``always``, which
#: matches how the steering loader reads a document with no frontmatter at all.
_SKIPPED_INCLUSIONS = frozenset({"manual", "auto", "filematch"})


def _is_within(path: Path, parent: Path) -> bool:
    """Is *path* strictly inside *parent*?

    A separator-terminated prefix test, the same shape
    :func:`kiro_crew.context.steering_target_admissible` uses, so ``/a/bc``
    never counts as being under ``/a/b``. Both sides are expected to be
    already-resolved paths; comparing an unresolved spelling here would let a
    symlinked home defeat the test.
    """
    return str(path).startswith(str(parent) + os.sep)


def _double_load_roots(project: str | None, home: Path | None) -> tuple[Path, ...]:
    """Steering roots every provider path ALREADY delivers.

    A document under the project's ``.kiro/steering`` or under
    ``~/.kiro/steering`` reaches the model through the existing project/global
    steering path, so re-sending it here would double its tokens. An
    unresolvable root simply contributes no skip rule: it cannot contain a file
    we are about to emit either.
    """
    roots: list[Path] = []
    candidates: list[Path] = []
    if project:
        candidates.append(Path(project))
    candidates.append(home if home is not None else Path.home())
    for candidate in candidates:
        try:
            # RuntimeError, not only OSError: ``Path.resolve()`` raises it on a
            # symlink loop, which a stale operator path can easily be.
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        roots.append(resolved / ".kiro" / "steering")
    return tuple(roots)


def _admissible(resolved: Path, root_resolved: Path) -> bool:
    """Apply the existing steering gate with the declared directory as base.

    Imported inside the function on purpose: ``context`` imports this module,
    so a module-level import would close a cycle. The trust base is the
    directory the operator declared, never ``$HOME`` -- a symlink inside a
    steering root can then never read outside the root it sits in.
    """
    from kiro_crew.context import steering_target_admissible

    return steering_target_admissible(resolved, base=root_resolved)


def collect_folder_steering(
    steering_dirs: Sequence[str],
    *,
    project: str | None,
    home: Path | None = None,
) -> list[tuple[str, str]]:
    """``(source_path, body)`` for every always-inclusion ``*.md`` under *steering_dirs*.

    Roots are read in the order given (the resolver hands them over root-first)
    and each document is emitted at most once across all of them, keyed by
    realpath. A missing root and an unreadable document are debug-logged and
    skipped so one stale path never breaks a turn.

    *home* exists for the tests and for a caller that knows the operator home
    without paying ``Path.home()``; it defaults to ``Path.home()``.
    """
    documents: list[tuple[str, str]] = []
    if not steering_dirs:
        return documents
    skip_roots = _double_load_roots(project, home)
    seen: set[str] = set()
    for raw in steering_dirs:
        try:
            root_resolved = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            logger.debug("folder steering directory could not be resolved: %r", raw)
            continue
        if not root_resolved.is_dir():
            logger.debug("folder steering directory is not a directory: %s", root_resolved)
            continue
        for candidate in sorted(root_resolved.glob("**/*.md")):
            if len(documents) >= _MAX_FOLDER_STEERING_DOCUMENTS:
                # A running ceiling, not a post-collection trim: stop before the
                # next read so a very large operator-pointed tree cannot be fully
                # materialized. Each body is already per-doc capped, so the count
                # ceiling bounds the aggregate too. Logged once at debug so an
                # operator can see their steering was capped without it breaking
                # the turn.
                logger.debug(
                    "folder steering collection hit its %d-document ceiling; "
                    "remaining files skipped",
                    _MAX_FOLDER_STEERING_DOCUMENTS,
                )
                return documents
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                logger.debug("folder steering document could not be resolved: %s", candidate)
                continue
            key = str(resolved)
            if key in seen:
                continue
            if any(_is_within(resolved, skip_root) for skip_root in skip_roots):
                continue
            if not _admissible(resolved, root_resolved):
                continue
            try:
                # ``PermissionError`` is an ``OSError``; ``safe_read_file`` raises
                # it for a sensitive target or a symlink race, and the plain
                # ``OSError`` cases (ENOENT on a file that vanished between the
                # glob and the read, EACCES) land in the same skip.
                body = safe_read_file(key)[:_MAX_SOURCE_BYTES]
            except (OSError, UnicodeDecodeError):
                logger.debug("folder steering document unreadable: %s", key)
                continue
            fields, stripped = split_frontmatter(body, STEERING_LOADER)
            if fields.get("inclusion", "").strip().casefold() in _SKIPPED_INCLUSIONS:
                continue
            seen.add(key)
            documents.append((key, stripped))
    return documents


def render_folder_steering(documents: list[tuple[str, str]]) -> str:
    """Render *documents* as one prompt section; ``""`` when there are none.

    Returning ``""`` rather than a bare header matters: the caller appends the
    result to the context parts unconditionally, and an empty-but-present
    section would tell the model a folder declared standards it then cannot
    see.
    """
    if not documents:
        return ""
    blocks = "\n\n".join(f"# {path}\n{body.strip()}" for path, body in documents)
    return f"{FOLDER_STEERING_HEADER}\n{blocks}\n{FOLDER_STEERING_FOOTER}"
