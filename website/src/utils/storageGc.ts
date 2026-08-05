/**
 * localStorage garbage collection.
 *
 * Removes orphaned per-session keys that accumulate unboundedly and
 * eventually overflow the ~5 MB origin quota, white-screening the app.
 *
 * Two entry points:
 *   - `gcOrphanedStorage(liveIds)` — startup pass, removes keys for sessions
 *     that no longer exist.
 *   - `gcSessionStorage(sessionKey)` — called when a session is deleted,
 *     removes that session's associated keys immediately.
 */

/** Prefixes that are scoped per-session and should be cleaned up.
 *  localStorage key prefixes — storage identifiers, never rendered. Not UI copy.
 *  Each must stay byte-identical to the writer that produces it (the first is
 *  `LS_KEY_PREFIX` in `hooks/virtualizer/HeightCache.ts`); a translated or
 *  reworded entry silently stops collecting that family of keys. */
const SESSION_PREFIXES = [
  'vc_heights_',
  'kirocrew:touched-files:',
  'mc-panel-tabs:',
  'mc-activity-open:',
  'mc-webpreview-url:',
  'mc-webpreview-pending:',
  'mc-webpreview-applied:',
] as const

/**
 * Remove localStorage keys belonging to sessions not in `liveSessionIds`.
 * Call once on app boot after fetching the slot list.
 *
 * Returns the number of keys removed.
 */
export function gcOrphanedStorage(liveSessionIds: Set<string>): number {
  if (typeof localStorage === 'undefined') return 0
  let removed = 0
  // Collect doomed keys first — removing during iteration shifts indices.
  const doomed: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (!key) continue
    for (const prefix of SESSION_PREFIXES) {
      if (key.startsWith(prefix)) {
        // Extract the session ID: everything after the prefix, before any further ':'
        const sessionId = key.slice(prefix.length).split(':')[0]
        if (sessionId && !liveSessionIds.has(sessionId)) {
          doomed.push(key)
        }
        break
      }
    }
  }
  for (const key of doomed) {
    try { localStorage.removeItem(key); removed++ } catch { /* best-effort */ }
  }
  if (removed > 0 && import.meta.env.DEV) {
    // eslint-disable-next-line no-console
    console.log(`[storageGc] removed ${removed} orphaned key(s)`)
  }
  return removed
}

/**
 * Remove all localStorage keys associated with a specific session.
 * Call when a session/slot is deleted.
 */
export function gcSessionStorage(sessionKey: string): void {
  if (typeof localStorage === 'undefined' || !sessionKey) return
  const doomed: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (!key) continue
    for (const prefix of SESSION_PREFIXES) {
      if (key.startsWith(prefix + sessionKey)) {
        doomed.push(key)
        break
      }
    }
  }
  for (const key of doomed) {
    try { localStorage.removeItem(key) } catch { /* best-effort */ }
  }
}
