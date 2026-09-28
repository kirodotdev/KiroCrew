/**
 * Canonical comment-forwarding filter for the chat-send and count paths.
 *
 * Shared by ArtifactDetailPage (the agent-facing count and the "address the N
 * open comments" prompt) and ArtifactPanel (the submit batch). Mirrors the
 * backend's `filter_comments_for_forward` in `artifacts.py`; the client keeps
 * its own copy because the count has to update the moment a thread is resolved
 * optimistically, before the comments query refetches.
 *
 * Thread-root granularity: a reply inherits its root's status, so resolving a
 * thread drops the whole thread rather than leaving replies whose parent is
 * gone.
 *
 * Only resolved threads are dropped. An anchor pointing at an older version is
 * deliberately NOT treated as stale — that span often still exists, in which
 * case the comment is live feedback nobody has addressed. `anchor_orphaned`
 * already marks the genuinely stale case and the UI warns on it.
 */
import type { ArtifactComment } from '../types'

/** The subset of `comments` eligible for forwarding to chat. */
export function filterCommentsForForward(comments: ArtifactComment[]): ArtifactComment[] {
  if (!comments.length) return []

  const byId = new Map(comments.map(c => [c.id, c]))

  function rootOf(c: ArtifactComment): ArtifactComment {
    // `thread_id` names the root directly (a root's is its own id), so it is the
    // first choice: a nested reply whose *immediate* parent has been deleted
    // still names its root, which a parent walk cannot reach.
    const root = byId.get(c.thread_id)
    if (root) return root
    // Fall back to the parent walk when thread_id resolves to nothing. The
    // `seen` set terminates a parent cycle; a parent missing from the list ends
    // the walk, leaving the comment its own root.
    const seen = new Set<string>()
    let cur = c
    while (cur.parent_id && byId.has(cur.parent_id) && !seen.has(cur.parent_id)) {
      seen.add(cur.id)
      cur = byId.get(cur.parent_id)!
    }
    return cur
  }

  return comments.filter(c => rootOf(c).status !== 'resolved')
}
