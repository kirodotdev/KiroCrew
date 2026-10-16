"""The live ``source_path`` pointer of an ``ArtifactStore`` artifact.

The roots a pointer may name (``allowed_source_roots``), the descriptor-pinned read
of the pointed-to file and its write-back, and ``relocate``, which moves the pointer.

Each function here is an ``ArtifactStore`` method: the class binds it by name, so it
reads the store's own state on the instance, and a patch of the method on the class or
on an instance reaches every caller.

Instance fields read: ``_root`` and ``_lock``.
Instance fields written: none.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import hooks
from kiro_crew.artifact_source import is_verifiable_root
from kiro_crew.artifact_store import dashboard_package as _dashboard
from kiro_crew.artifact_store.model import Artifact, ArtifactValidationError
from kiro_crew.artifact_store.rules import _validate_slug, _validate_source_path
from kiro_crew.artifacts_disk import MAX_CONTENT_BYTES
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.security import is_sensitive_path

if TYPE_CHECKING:
    from kiro_crew.artifacts import ArtifactStore

#: The store's own logger, shared with :mod:`kiro_crew.artifacts`, so a log filter or
#: capture keyed on that name sees this code's records.
logger = logging.getLogger("kiro_crew.artifacts")


def allowed_source_roots(self: ArtifactStore, source_root: str = "") -> list[Path]:
    """Resolved directory roots a file-backed ``source_path`` may live under.

    SINGLE producer of this set. Three sites consume it — the live read
    (:meth:`_try_read_source_path`), the live write
    (:meth:`_try_write_source_path`), and the relocate handler's containment
    barrier in ``dashboard/handlers/artifacts_parts/item_lifecycle.py`` — and
    they had drifted apart: the handler's copy omitted the data-home root, so
    relocate refused paths the store would happily read.

    The set is:

    * the user's home directory;
    * the data home (this store's parent — ``~/.kiro/crew`` in production,
      a tmp dir under test);
    * every operator-configured ``publish.relocate_roots`` entry;
    * when supplied, the artifact's own ``source_root`` — the project root
      that authorized the link at create time. This is what lets a linked
      project file outside ``$HOME`` (``/workplace/user/repo/doc.md``) be
      read at all, without widening the default set for every artifact.

    Callers MUST keep the ``p == r or p.is_relative_to(r)`` comparison
    INLINE at the call site. CodeQL's path-injection taint tracker only
    recognizes that sanitizer intra-procedurally, so moving the comparison
    into this method would reopen the alert
    (``handlers/artifacts_parts/item_lifecycle.py`` documents the same
    constraint). This method only assembles the roots; it never decides
    containment.
    """
    allowed = [Path.home().resolve(), self._root.resolve().parent]
    try:

        for extra in KiroCrewConfig.load().publish.relocate_roots:
            if isinstance(extra, str) and extra.strip():
                allowed.append(Path(extra).expanduser().resolve())
    except Exception:
        pass
    # A persisted source_root is a HINT, not authority: meta.json lives in
    # the agent-writable data home, so honouring it verbatim would let a
    # forged record (source_root="/", source_path="/etc/passwd") widen this
    # boundary to the whole filesystem. Re-verify it against something a
    # metadata write cannot fake.
    if source_root and isinstance(source_root, str) and source_root.strip():
        if is_verifiable_root(source_root):
            try:
                allowed.append(Path(source_root).expanduser().resolve())
            except (OSError, ValueError):  # pragma: no cover — defensive
                logger.warning("unusable source_root %r; ignoring", source_root)
        else:
            logger.warning(
                "source_root %r no longer verifies as a project root; ignoring",
                source_root,
            )
    return allowed


def _try_read_source_path(
    self: ArtifactStore, source_path: str, source_root: str = ""
) -> str | None:
    """Read the source file for a file-backed artifact (live pointer).

    Returns None on any failure (missing file, permission denied,
    sensitive path, outside allowed roots, oversize). Caller falls back
    to the artifact's own snapshot in that case so a missing/moved
    source doesn't break the artifact view — and MUST surface
    ``source_missing`` so that fallback isn't mistaken for a healthy,
    in-sync live pointer.

    ``source_root`` is the artifact's recorded authorizing root; pass
    ``meta.source_root`` so a linked project file outside ``$HOME``
    resolves. See :meth:`allowed_source_roots`.
    """
    try:
        # Resolve before the security check so traversal segments
        # (`..`) and symlinks pointing into sensitive locations can't
        # bypass is_sensitive_path. A benign-looking
        # `source_path` with `../../etc/shadow` would otherwise sneak
        # past, since is_sensitive_path() inspects the literal string.
        p = Path(source_path).expanduser().resolve()
        if not p.is_absolute():
            return None
        if is_sensitive_path(str(p)):
            return None
        # Root-confinement re-check on every read: a symlink replacement
        # after relocate could escape the allowed roots if we only checked
        # at set time. Re-validate that the RESOLVED path is under the
        # user's home, the KIROCREW_HOME tree, a configured relocate root,
        # or the artifact's own recorded source_root.
        allowed = self.allowed_source_roots(source_root)
        # Comparison stays INLINE (not in a helper) — CodeQL's taint
        # tracker needs the is_relative_to sanitizer to guard the SAME
        # `p` the reads below use.
        containing = next((r for r in allowed if p == r or p.is_relative_to(r)), None)
        within_root = containing is not None
        if not within_root:
            logger.warning(
                "source_path %r resolved outside allowed roots; refusing read", source_path
            )
            return None
        if not p.exists() or not p.is_file():
            # Dead pointer. Logged at WARNING (not silence) because the
            # caller falls back to the snapshot, which makes this
            # invisible in the artifact view unless it's reported.
            logger.warning(
                "source_path %r no longer exists (or is not a regular file); "
                "artifact falls back to its last snapshot",
                source_path,
            )
            return None
        # Bound the read at the FILE level, not after-the-fact: read
        # MAX_CONTENT_BYTES+1 bytes from disk, decode (errors='replace'
        # for invalid sequences). p.read_text() would load the entire
        # file into memory before the size check — a multi-GB file
        # pointed to by source_path would exhaust memory before
        # truncation triggered. Bounding the read caps memory at
        # MAX_CONTENT_BYTES+1 regardless of file size.
        # Read through the descriptor-pinned helper rather than by name.
        # The containment check above is on a RESOLVED path, which still
        # leaves a check-to-use window: the final component, or an ancestor
        # directory, can be swapped for a symlink before the open, so the
        # bytes could come from a file outside `containing`. The helper opens
        # with O_NOFOLLOW and re-verifies the OPENED inode's real path
        # against the same root, so the inode validated is the inode read.
        raw = hooks.safe_read_file_bytes_nolink(
            str(p),
            within_root=str(containing),
            max_bytes=MAX_CONTENT_BYTES,
            allow_truncate=True,
        )
        if raw is None:
            logger.warning("source_path %r refused by the descriptor-pinned read gate", source_path)
            return None
        if len(raw) == MAX_CONTENT_BYTES:
            logger.warning("source file %s hit MAX_CONTENT_BYTES; view is truncated", p)
        # errors='replace' keeps the artifact viewable even when the
        # file contains malformed UTF-8 sequences. The byte-level
        # truncation may chop a multi-byte character at the boundary;
        # the replace handler emits U+FFFD for that case.
        return raw.decode("utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        logger.warning("failed to read source_path %r: %s", source_path, exc)
        return None


def _try_write_source_path(
    self: ArtifactStore, source_path: str, content: str, source_root: str = ""
) -> bool:
    """Write to the source file for a file-backed artifact.

    Returns True on success, False if the path is unwritable. Caller
    proceeds to update the artifact's own storage either way — the
    snapshot remains authoritative even when the source can't be
    kept in sync.

    ``source_root`` mirrors the read side: the same recorded root that
    authorizes reads authorizes the write-back, so an edit to a linked
    project file lands in the project rather than being silently dropped.
    """
    try:
        # Same canonicalization as the read side — `.resolve()` prevents
        # symlink-based bypass of is_sensitive_path. Writing through a
        # symlink to a sensitive file is arguably worse than reading.
        p = Path(source_path).expanduser().resolve()
        if not p.is_absolute():
            return False
        if is_sensitive_path(str(p)):
            return False
        # Root-confinement re-check (same set as _try_read_source_path, via
        # the single producer): a symlink swap after relocate must not allow
        # writes outside the allowed roots.
        allowed = self.allowed_source_roots(source_root)
        # Comparison stays INLINE — see allowed_source_roots() on why the
        # CodeQL sanitizer cannot be factored out.
        containing = next((r for r in allowed if p == r or p.is_relative_to(r)), None)
        within_root = containing is not None
        if not within_root:
            logger.warning(
                "source_path %r resolved outside allowed roots; refusing write",
                source_path,
            )
            return False
        # Don't create the file if it never existed — that would be
        # surprising. The 'Add to artifacts' flow always saves an
        # existing file, so the file should exist.
        if not p.exists():
            return False
        # Write through the descriptor-pinned helper, not by name. The
        # containment check above is on a RESOLVED path, so a symlink
        # swapped into the final component -- or into an ancestor directory
        # -- between that check and the open would land these bytes on a
        # file outside `containing`. Writing through a symlink is strictly
        # worse than reading through one, so the same fd-pinned gate the
        # read side uses applies here: O_NOFOLLOW open first, then hardlink
        # / regular-file / real-path / sensitive checks on that descriptor.
        if not hooks.safe_write_file_nolink(str(p), content, within_root=str(containing)):
            logger.warning(
                "source_path %r refused by the descriptor-pinned write gate", source_path
            )
            return False
        return True
    except (OSError, ValueError) as exc:
        logger.warning("failed to write source_path %r: %s", source_path, exc)
        return False


def relocate(self: ArtifactStore, slug: str, source_path: str, source_root: str = "") -> Artifact:
    """Update the source_path (live file pointer) for an artifact.

    ``source_root`` is rewritten in lockstep — it describes the root that
    authorizes THIS ``source_path``, so carrying the previous pointer's root
    forward would leave a record claiming an authorization it does not hold.
    The default (``""``) clears it, which is correct for the relocate
    handler: it validates against the store's base root set (home / data
    home / configured relocate roots), none of which need recording.
    """
    slug = _validate_slug(slug)
    source_path = _validate_source_path(source_path)
    source_root = _validate_source_path(source_root, "source_root") if source_path else ""
    with self._lock:
        art = self._load_meta(slug)
        # A dashboard package is store-owned: ``create`` refuses a
        # ``source_path`` and ``update`` refuses converting into the kind
        # while one is kept, so this is the third door into the same state
        # and it has to be shut too. Pointing a dashboard at a file makes
        # every later get re-read the package from outside the store, past
        # the write gate -- and a revert, which recovers the live binding
        # from ``current.html``, would then write that binding into the
        # file, handing the page to whoever the stale copy named. Clearing
        # the pointer (an empty ``source_path``) stays allowed: that is the
        # direction which RESTORES store ownership.
        if source_path and art.kind == _dashboard.DASHBOARD_KIND:
            raise ArtifactValidationError(
                "a dashboard package is store-owned: kind='dashboard' takes no "
                "source_path, so it cannot be relocated onto a file"
            )
        art.source_path = source_path
        art.source_root = source_root
        # Relocate is an explicit "this artifact tracks THIS file" act, so
        # it promotes a copy into a live pointer. Leaving the flag set would
        # make the relocation silently inert -- reads and edits would keep
        # ignoring the very file the user just pointed at.
        art.source_copy_only = False
        self._write_meta(art)
        return art
