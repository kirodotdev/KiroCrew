/**
 * Cross-window artifact-comment sync.
 *
 * The main dashboard and an artifact popout are separate JS contexts with
 * separate react-query caches, so a comment posted in one window doesn't
 * appear in the other until its cache goes stale (staleTime is 30s) — a lag
 * users read as "my comment didn't save". This module closes that gap with a
 * single BroadcastChannel: every window announces the slug after any comment
 * mutation, and every window viewing that slug invalidates its comments query
 * immediately. Fire-and-forget — the gateway remains the source of truth; a
 * missed message just falls back to the normal staleness refetch.
 */

export const ARTIFACT_COMMENTS_SYNC_CHANNEL = 'kirocrew-artifact-comments-sync'

type SyncMsg = { slug: string }

let channel: BroadcastChannel | null = null
const listeners = new Map<string, Set<() => void>>()

function ensureChannel(): BroadcastChannel | null {
  if (channel || typeof BroadcastChannel === 'undefined') return channel
  channel = new BroadcastChannel(ARTIFACT_COMMENTS_SYNC_CHANNEL)
  channel.onmessage = (e: MessageEvent<SyncMsg>) => {
    const slug = e.data?.slug
    if (!slug) return
    listeners.get(slug)?.forEach(l => l())
  }
  return channel
}

/** Announce that `slug`'s comments changed in THIS window (other windows refetch). */
export function announceCommentsChanged(slug: string): void {
  ensureChannel()?.postMessage({ slug })
}

/** Subscribe to comment changes for `slug` made in OTHER windows. Returns cleanup. */
export function onCommentsChanged(slug: string, listener: () => void): () => void {
  ensureChannel()
  let set = listeners.get(slug)
  if (!set) { set = new Set(); listeners.set(slug, set) }
  set.add(listener)
  return () => {
    set.delete(listener)
    if (set.size === 0) listeners.delete(slug)
  }
}
