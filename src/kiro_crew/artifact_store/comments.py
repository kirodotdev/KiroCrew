"""Comment-thread rules for an artifact's ``comments.json`` mirror.

Threads are one level deep: a root carries its own id as ``thread_id`` and every
reply carries the root's. The functions here decide thread membership -- which
comments are forwarded to chat, which whole threads the retention cap drops, how a
provider's comments reconcile into the local mirror, which anchors are orphaned and
what a delete removes. They operate on in-memory lists; loading, locking and writing
the sidecar stay with :class:`kiro_crew.artifacts.ArtifactStore`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import List as _List

from kiro_crew.artifact_store.model import ArtifactComment


def filter_comments_for_forward(
    comments: _List[ArtifactComment],
) -> _List[ArtifactComment]:
    """Canonical comment-forwarding filter.

    The single source of truth for which comments get *forwarded/counted* when
    an artifact's feedback is sent into chat, so the UI count, the side-panel
    submit and the agent's read all agree.

    Evaluated at **thread-root granularity**: a reply inherits its root's
    status, so resolving a thread drops the whole thread rather than leaving
    replies whose parent is gone.

    A resolved thread is the only thing dropped. Staleness is deliberately NOT
    inferred from the anchor version: a comment anchored to an older version
    whose quoted span still exists is live feedback nobody has addressed, and
    dropping it would silently stop forwarding a thread the sidebar still shows
    as open. ``anchor_orphaned`` already marks the genuinely stale case (the
    quote is gone) and the UI warns on it, so that call stays with the human.
    """
    by_id = {c.id: c for c in comments}

    def root_of(c: ArtifactComment) -> ArtifactComment:
        # ``thread_id`` names the root directly (a root's is its own id), so it
        # is the first choice: a nested reply whose *immediate* parent has been
        # deleted still names its root, which a parent walk cannot reach.
        root = by_id.get(c.thread_id)
        if root is not None:
            return root
        # Fall back to the parent walk when thread_id resolves to nothing (the
        # field defaults to ""). The `seen` set terminates a parent cycle; a
        # parent missing from the list ends the walk, leaving the comment its
        # own root.
        seen: set[str] = set()
        cur = c
        while cur.parent_id and cur.parent_id in by_id and cur.parent_id not in seen:
            seen.add(cur.id)
            cur = by_id[cur.parent_id]
        return cur

    return [c for c in comments if root_of(c).status != "resolved"]


def prune_oldest_threads(
    comments: _List[ArtifactComment], *, keep_id: str, cap: int
) -> _List[ArtifactComment]:
    """Drop whole oldest threads until at most ``cap`` comments remain.

    A thread is a root plus every comment carrying its id as ``thread_id``.
    Threads are ranked by their root's list position (append order == age),
    oldest first; the thread containing ``keep_id`` is never dropped.
    """

    # Map each comment to its thread key (root id). A root's thread_id is its
    # own id (or empty -> itself); a reply carries the root's id.
    def thread_key(c: ArtifactComment) -> str:
        return c.thread_id or c.id

    keep_thread = next((thread_key(c) for c in comments if c.id == keep_id), keep_id)
    # Oldest-first order of thread keys by first appearance.
    order: _List[str] = []
    for c in comments:
        k = thread_key(c)
        if k not in order:
            order.append(k)
    drop: set[str] = set()
    remaining = len(comments)
    for k in order:
        if remaining <= cap:
            break
        if k == keep_thread:
            continue
        members = sum(1 for c in comments if thread_key(c) == k)
        drop.add(k)
        remaining -= members
    if not drop:
        return comments
    return [c for c in comments if thread_key(c) not in drop]


def merge_remote(
    existing: _List[ArtifactComment], remote_comments: _List[ArtifactComment]
) -> tuple[_List[ArtifactComment], bool]:
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

    Returns ``(merged, changed)``; ``merged`` is only worth persisting when
    ``changed`` is true.
    """
    incoming = {rc.origin: rc for rc in remote_comments if rc.origin and rc.origin != "local"}
    deleted_origins = {origin for origin, rc in incoming.items() if getattr(rc, "deleted", False)}
    # Threads whose ROOT was deleted upstream — cascade-drop the whole
    # subtree, not just the tombstoned root. Two confirmed realities:
    #   * some remotes do NOT cascade-delete replies when a parent is
    #     deleted — the replies keep coming back deleted=False — so we drop
    #     them by thread rather than waiting for per-reply tombstones.
    #   * the remote retains the deleted ROOT as a tombstone in *every*
    #     fetch, so seed the drop-set from the INCOMING tombstones (the
    #     authoritative, always-present signal), not only the local mirror
    #     — that mirror is gone after the first cascade, so relying on it
    #     would let the orphans come back on the next fetch.
    # Replies share the root's thread_id (provider: thread_id =
    # parentCommentId or commentId), so this drops the subtree on every
    # fetch — self-healing, no persistent local tombstone needed. Local
    # threads can't collide: a deleted root is always a provider comment.
    dropped_threads: set[str] = set()
    for irc in incoming.values():
        irc_root = not irc.parent_id or irc.thread_id == irc.id
        if getattr(irc, "deleted", False) and irc_root:
            dropped_threads.add(irc.thread_id)
            dropped_threads.add(irc.id)
    for c in existing:
        if c.origin in deleted_origins and (not c.parent_id or c.thread_id == c.id):
            dropped_threads.add(c.thread_id)
            dropped_threads.add(c.id)
    result: _List["ArtifactComment"] = []
    seen: set[str] = set()
    changed = False
    for c in existing:
        if c.thread_id in dropped_threads:
            # Reply (local or provider) under a root deleted upstream —
            # cascade-drop it too, even if it's local-origin (orphaned).
            changed = True
            continue
        if not c.origin or c.origin == "local":
            result.append(c)  # local comment — never touched by remote sync
            continue
        if c.origin in deleted_origins:
            changed = True  # tombstoned on the provider — drop the mirror
            continue
        rc = incoming.get(c.origin)
        if rc is None:
            result.append(c)  # not in this fetch — keep (don't wipe)
            continue
        seen.add(c.origin)
        if c.status != rc.status or c.body != rc.body or c.author != rc.author:
            c.status, c.body, c.author = rc.status, rc.body, rc.author
            c.updated_at = rc.updated_at or c.updated_at
            changed = True
        result.append(c)
    for origin, rc in incoming.items():
        if origin in seen or origin in deleted_origins:
            continue
        if rc.thread_id in dropped_threads:
            continue  # belongs to a thread whose root was deleted upstream
        result.append(rc)
        changed = True
    return result, changed


def rescan_anchors(
    comments: _List[ArtifactComment], content: str, *, clock: Callable[[], str]
) -> bool:
    """Flip ``anchor_orphaned`` on every open anchored comment against ``content``.

    Matching is a plain substring check on ``anchor_quote`` -- the same exactness
    contract as the frontend highlighter's ``indexOf`` matcher
    (``useMarkdownCommentHighlights``). No fuzzy matching: a quote either exists in
    the content or the thread is flagged. Resolved threads are skipped (their
    anchors are historical by definition). The flag is symmetric: content that
    brings the quote back (e.g. a revert) clears it. Each flipped comment gets
    ``clock()`` as its ``updated_at``. Returns whether anything changed.
    """
    changed = False
    for c in comments:
        if not c.anchor_quote or c.status == "resolved":
            continue
        orphaned = c.anchor_quote not in content
        if orphaned != c.anchor_orphaned:
            c.anchor_orphaned = orphaned
            c.updated_at = clock()
            changed = True
    return changed


def remove_comment(comments: _List[ArtifactComment], comment_id: str) -> _List[ArtifactComment]:
    """``comments`` without ``comment_id``; removing a thread ROOT removes its replies too.

    Threads are one level deep and replies carry the root's id as their
    ``thread_id``, so a root removal takes the whole thread rather than orphaning
    the children into top-level comments. Removing a reply removes only that reply.
    """
    target = next((c for c in comments if c.id == comment_id), None)
    if target is not None and (not target.parent_id or target.thread_id == target.id):
        # Root: drop the root and every reply in its thread.
        comments = [c for c in comments if c.thread_id != comment_id and c.id != comment_id]
    else:
        comments = [c for c in comments if c.id != comment_id]
    return comments
