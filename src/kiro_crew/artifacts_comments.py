"""The ``comments.json`` sidecar of an ``ArtifactStore`` artifact.

Comment CRUD, the provider merge, the anchor rescan after a content write and the
comment lifecycle events, all under the store lock; the thread rules themselves are
:mod:`kiro_crew.artifact_store.comments`.

Each function here is an ``ArtifactStore`` method: the class binds it by name, so it
reads the store's own state on the instance, and a patch of the method on the class or
on an instance reaches every caller.

Instance fields read: ``_lock`` and ``_MUTABLE_COMMENT_FIELDS`` (a class attribute).
Instance fields written: none.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from typing import List as _List

from kiro_crew.artifact_store import comments as _threads
from kiro_crew.artifact_store import records as _records
from kiro_crew.artifact_store.model import ArtifactComment, ArtifactError
from kiro_crew.artifact_store.rules import _validate_slug
from kiro_crew.artifacts_disk import MAX_COMMENTS_PER_ARTIFACT, _now_iso

if TYPE_CHECKING:
    from kiro_crew.artifacts import ArtifactStore

#: The store's own logger, shared with :mod:`kiro_crew.artifacts`, so a log filter or
#: capture keyed on that name sees this code's records.
logger = logging.getLogger("kiro_crew.artifacts")


def list_comments(self: ArtifactStore, slug: str) -> _List["ArtifactComment"]:
    """Load all comments for an artifact from comments.json sidecar."""
    slug = _validate_slug(slug)
    with self._lock:
        return self._load_comments(slug)


def add_comment(self: ArtifactStore, slug: str, comment: "ArtifactComment") -> "ArtifactComment":
    """Persist a new comment to the sidecar store.

    Bounded at :data:`MAX_COMMENTS_PER_ARTIFACT` (FIFO). When appending would
    exceed the cap, the OLDEST thread roots (and their replies) are dropped
    as whole threads — never orphaning a reply — until the list fits. The
    just-added comment is always kept.
    """
    slug = _validate_slug(slug)
    with self._lock:
        comments = self._load_comments(slug)
        comments.append(comment)
        if len(comments) > MAX_COMMENTS_PER_ARTIFACT:
            comments = _threads.prune_oldest_threads(
                comments, keep_id=comment.id, cap=MAX_COMMENTS_PER_ARTIFACT
            )
        self._write_comments(slug, comments)
        return comment


def update_comment(
    self: ArtifactStore, slug: str, comment_id: str, **fields: Any
) -> "ArtifactComment | None":
    """Patch fields on an existing comment. Returns updated or None.

    Only fields in :data:`_MUTABLE_COMMENT_FIELDS` may be set; an unknown or
    disallowed field name raises :class:`ArtifactError` rather than silently
    writing it (consistent with ``update_publication`` /
    ``update_fork_metadata``).
    """
    slug = _validate_slug(slug)
    with self._lock:
        comments = self._load_comments(slug)
        for c in comments:
            if c.id == comment_id:
                for k, v in fields.items():
                    if k not in self._MUTABLE_COMMENT_FIELDS:
                        raise ArtifactError(f"comment field {k!r} is not patchable")
                    setattr(c, k, v)
                c.updated_at = _now_iso()
                self._write_comments(slug, comments)
                return c
        return None


def delete_comment(self: ArtifactStore, slug: str, comment_id: str) -> bool:
    """Remove a comment from the sidecar store. Returns True if found.

    Deleting a thread ROOT cascades to its replies: threads are one level
    deep and replies carry the root's id as their ``thread_id``, so a parent
    delete removes the whole thread rather than orphaning the children into
    top-level comments. Deleting a reply removes only that reply.
    """
    slug = _validate_slug(slug)
    with self._lock:
        comments = self._load_comments(slug)
        before = len(comments)
        comments = _threads.remove_comment(comments, comment_id)
        if len(comments) < before:
            self._write_comments(slug, comments)
            return True
        return False


def merge_remote_comments(
    self: ArtifactStore, slug: str, provider: str, remote_comments: _List["ArtifactComment"]
) -> _List["ArtifactComment"]:
    """Reconcile remote (provider) comments into the local mirror.

    The provider is authoritative for its own comments. This:
      * drops local mirrors that came back tombstoned (``deleted``) — so a
        comment deleted on the remote disappears locally;
      * syncs mutable fields (status/body/author) of changed provider
        comments — so an inbound resolve/edit is reflected;
      * adds newly-seen provider comments;
      * leaves local comments (``origin == "local"``) untouched, and keeps
        provider comments absent from this fetch (avoids wiping on a
        transient/paginated empty — real deletes return as tombstones).
    """
    slug = _validate_slug(slug)
    with self._lock:
        existing = self._load_comments(slug)
        result, changed = _threads.merge_remote(existing, remote_comments)
        if changed:
            self._write_comments(slug, result)
            return result
        return existing


def _rescan_comment_anchors_locked(self: ArtifactStore, slug: str, content: str) -> None:
    """Re-validate every anchored comment against ``content`` and flip
    ``anchor_orphaned`` accordingly. MUST be called with ``self._lock``
    held (``update()`` calls it from inside its locked content-write
    branch; the lock is non-reentrant so this cannot go through the
    public ``list_comments``/``update_comment`` wrappers).

    The matching rule is :func:`kiro_crew.artifact_store.comments.rescan_anchors`'s.
    Tolerant — an anchor-rescan failure must never break the content write it
    piggybacks on.
    """
    try:
        comments = self._load_comments(slug)
        if _threads.rescan_anchors(comments, content, clock=_now_iso):
            self._write_comments(slug, comments)
    except Exception:  # pragma: no cover — best-effort side scan
        logger.warning("comment anchor rescan failed for %s", slug, exc_info=True)


def record_comment_event(
    self: ArtifactStore,
    slug: str,
    *,
    action: str,
    by: str = "user",
    session_id: str | None = None,
    comment_snippet: str | None = None,
    reason: str | None = None,
) -> None:
    """Append a ``comment`` lifecycle event to ``slug``'s activity log.

    ``action`` says what happened to the comment (``deleted`` /
    ``reviewed`` / ``resolved``); ``comment_snippet`` is a short excerpt
    of the affected comment body so the timeline entry is readable
    without the (possibly deleted) comment; ``reason`` carries the
    agent's one-line justification on deletes. Tolerant — a timeline
    failure must never break the comment operation it annotates.
    """
    try:
        with self._lock:
            meta = self._load_meta(slug)
            metadata: dict[str, Any] = {"action": action}
            if comment_snippet:
                metadata["comment_snippet"] = comment_snippet
            if reason:
                metadata["reason"] = reason
            self._append_event(
                meta,
                type="comment",
                by=by,
                session_id=session_id,
                version=meta.version,
                metadata=metadata,
            )
            self._write_meta(meta)
    except Exception:  # pragma: no cover — timeline is best-effort
        logger.warning("comment event append failed for %s", slug, exc_info=True)


def _load_comments(self: ArtifactStore, slug: str) -> _List["ArtifactComment"]:
    """Load comments.json sidecar (tolerant — missing file = empty)."""
    path = self._artifact_dir(slug) / "comments.json"
    if not path.exists():
        return []
    try:
        raw_list = json.loads(self._read_text(path))
    except (json.JSONDecodeError, ArtifactError):
        return []
    return _records.decode_comments(raw_list)


def _write_comments(self: ArtifactStore, slug: str, comments: _List["ArtifactComment"]) -> None:
    """Persist comments list to comments.json sidecar."""
    path = self._artifact_dir(slug) / "comments.json"
    self._write_text(path, _records.encode_comments(comments))
