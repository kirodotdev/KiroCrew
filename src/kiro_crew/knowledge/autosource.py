"""Auto-discovery of the workspace drop folder as a Knowledge source.

A convention folder under the active workspace ("drop documents here and they
become searchable") that is registered as a watched ``local_folder`` source
without the user adding it by hand.

Discovery is **recurring, not one-shot**: it runs on every
:class:`~kiro_crew.knowledge.watcher.KnowledgeWatcher` sweep, so a folder
created after the gateway started is picked up on the next sweep rather than
requiring a restart. Registration is idempotent -- the source row's existence is
the marker, so repeated sweeps re-use it.

Why the folder is NOT named ``knowledge``: the Library's own SQLite store lives
at ``<workspace>/knowledge/``, created eagerly by the dashboard state
(``os.makedirs(..., exist_ok=True)``). A drop folder by that name would always
exist, which would make "discover it when it appears" vacuous and would mix
user documents with the store's own DB files. Hence a distinct default name,
overridable via ``knowledge.auto_discover_dirname``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

# Convention folder name under the active workspace. Deliberately not
# "knowledge" -- see module docstring.
DEFAULT_DROP_DIRNAME = "knowledge-docs"

# Display name of the auto-registered source.
DROP_SOURCE_NAME = "Workspace Documents"

# Marker in source properties identifying a row this module created. Lets the
# UI label it and lets a future migration find auto-added rows.
AUTO_ADDED_PROP = "auto_added"


def _is_within(candidate: Path, base: Path) -> bool:
    """True when ``candidate`` is ``base`` or lives underneath it.

    Both paths must already be resolved. ``Path.is_relative_to`` is 3.9+, which
    the project targets, but ``commonpath`` is used so the comparison is on
    normalized strings and a sibling like ``/ws-evil`` cannot pass a naive
    ``startswith`` test against ``/ws``.
    """
    try:
        return os.path.commonpath([str(candidate), str(base)]) == str(base)
    except ValueError:
        # Different drives on Windows -- definitionally not contained.
        return False


def resolve_drop_folder(base: str, dirname: str = DEFAULT_DROP_DIRNAME) -> str:
    """Return the drop folder path if it currently exists, else ``""``.

    ``base`` is the active workspace directory. Returns the resolved absolute
    path only when the folder exists, is a directory, and clears the
    sensitive-path gate -- otherwise the empty string, which callers treat as
    "nothing to add yet". The folder is never created: it exists because the
    user made it, and its absence is the signal that they have not opted in.

    ``dirname`` is treated as a single path segment; anything containing a
    separator or traversal is rejected so a config value cannot redirect the
    source outside the workspace.
    """
    if not base or not dirname:
        return ""
    if dirname != Path(dirname).name or dirname in (".", ".."):
        logger.warning("Ignoring invalid knowledge drop dirname: %r", dirname)
        return ""
    try:
        base_real = Path(base).resolve()
        candidate = (base_real / dirname).resolve()
    except OSError:
        return ""
    # Containment: the segment check above only constrains the config STRING. A
    # symlink at <workspace>/<dirname> pointing outside the workspace would
    # survive it, and is_sensitive_path() does not help -- it only classifies
    # credential/trust dirs, so a link to an unrelated home or system tree
    # passes. Without this check the whole target tree would be walked and
    # LLM-extracted into the graph. Compare with the manual POST flow, where
    # escaping the workspace is the user's explicit intent; here the contract is
    # "a folder inside the active workspace", so escape is always a defect.
    if not _is_within(candidate, base_real):
        logger.warning(
            "Knowledge drop folder resolves outside the workspace, skipping: %s", candidate
        )
        return ""
    if not candidate.is_dir():
        return ""
    resolved = str(candidate)
    if is_sensitive_path(resolved):
        logger.warning("Knowledge drop folder is a restricted path, skipping: %s", resolved)
        return ""
    return resolved


def ensure_drop_source(store, uri: str) -> tuple[str | None, bool]:
    """Get-or-create the drop-folder source row, unless the URI is dismissed.

    Returns ``(source_id, created)``, or ``(None, False)`` when the user has
    dismissed this URI by deleting the auto-added source.

    The tombstone check, the existing-row check and the INSERT are delegated to
    ``store.create_auto_source_unless_dismissed`` so all three share ONE
    transaction. Doing them as separate statements here would let a concurrent
    delete land in between: discovery would see no source row and no tombstone
    yet, and re-create the source the user just deleted.

    A folder the user already registered by hand at the same path is re-used,
    never shadowed. ``sync_status`` is seeded ``active`` rather than the
    ``pending_confirmation`` the POST endpoint uses, because no user is present
    to confirm: the watcher's gate skips only ``paused`` and
    ``pending_confirmation``.
    """
    props = {"sync_status": "active", AUTO_ADDED_PROP: True}
    try:
        return store.create_auto_source_unless_dismissed(
            name=DROP_SOURCE_NAME,
            source_type="local_folder",
            uri=uri,
            properties=props,
        )
    except Exception:
        # Lost a race on the UNIQUE uri -- re-read and treat as pre-existing.
        existing = store.get_source_by_uri(uri)
        if existing:
            return existing["id"], False
        raise


def auto_source_still_contained(uri: str, base: str) -> bool:
    """Re-validate a REGISTERED auto source before each scan.

    Registration-time containment is not sufficient. When
    ``<workspace>/<dirname>`` is a real directory, the persisted URI is that
    literal path -- so replacing the directory with a symlink to an external
    tree afterwards makes ``os.walk`` follow it out of the workspace on the next
    sweep, and outside files get ingested and LLM-extracted. The stored URI is
    only trustworthy if it is re-checked every time it is used.

    Applies to auto-added sources only: for a folder the user registered by hand,
    pointing outside the workspace is their explicit choice, not a defect.
    """
    if not uri or not base:
        return False
    try:
        base_real = Path(base).resolve()
        target = Path(uri).resolve()
    except OSError:
        return False
    if not _is_within(target, base_real):
        return False
    if is_sensitive_path(str(target)):
        return False
    return True


def discover_and_register(store, base: str, dirname: str = DEFAULT_DROP_DIRNAME) -> str | None:
    """Register the drop folder if it exists and is not already a source.

    Returns the new source id when a row was created, otherwise ``None`` (folder
    absent, already registered, or previously dismissed by the user).
    Synchronous: callers on the event loop should hand this to
    ``asyncio.to_thread`` -- it stats the filesystem and writes SQLite.
    """
    uri = resolve_drop_folder(base, dirname)
    if not uri:
        return None
    source_id, created = ensure_drop_source(store, uri)
    if not created:
        return None
    logger.info("Auto-discovered knowledge drop folder: %s (source %s)", uri, source_id)
    return source_id
