/**
 * localStorage garbage collection.
 *
 * Removes orphaned per-session keys that accumulate unboundedly and
 * eventually overflow the ~5 MB origin quota, white-screening the app.
 *
 * Two entry points:
 *   - `gcOrphanedStorage(liveIds)` — startup pass, removes keys for sessions
 *     that no longer exist. This is the authoritative collector.
 *
 * There is deliberately NO per-key collector. A client cannot prove a key is dead:
 * localStorage is shared across tabs, so another tab may hold or resume that session
 * while this one deletes it. Only the startup pass, which works from an authoritative
 * live list, may collect.
 */

/** Prefixes that are scoped per-session and should be cleaned up.
 *  localStorage key prefixes — storage identifiers, never rendered. Not UI copy.
 *  Each must stay byte-identical to the writer that produces it (the first is
 *  `LS_KEY_PREFIX` in `hooks/virtualizer/HeightCache.ts`, the second is
 *  `ANCHOR_KEY_PREFIX` in `hooks/virtualizer/ScrollAnchorCache.ts`); a
 *  translated or reworded entry silently stops collecting that family of keys. */
const SESSION_PREFIXES = [
  'vc_heights_',
  'vc_anchor3_',
  // The pre-version-bump anchor prefixes. `ScrollAnchorCache` reaps these
  // outright when it loads, but that only happens once the chat virtualizer is
  // on screen — so the boot sweep keeps owning them for a user who never opens a
  // chat. Dropping a line when the prefix was bumped would leave those keys
  // uncollectable for exactly the sessions that are already gone.
  'vc_anchor2_',
  'vc_anchor_',
  'kirocrew:touched-files:',
  'mc-panel-tabs:',
  'mc-activity-open:',
  'mc-webpreview-url:',
  'mc-webpreview-pending:',
  'mc-webpreview-applied:',
  'mc-busy-send-mode:',
] as const

/** Names that appear where a session id is expected but are not sessions.
 *
 *  The orphan sweep always spares them: it is *guessing* which names are dead,
 *  and a non-session name looks dead forever. An explicit per-session delete is
 *  different — the caller named its target — so it is honoured unless the key is
 *  shared state that target does not own. That is the whole difference between
 *  the two entries below, and it turns on ownership, not on convenience.
 *
 *  Each name must stay byte-identical to its writer; a divergence collects the
 *  parked value silently.
 *
 *  - `artifacts-gallery` — the virtualizer partitions its height cache by
 *    `sessionId`, and the gallery is not a chat session but still has to name a
 *    partition. Writer: `ARTIFACT_HEIGHT_NS` in `pages/ArtifactsPage.tsx`.
 *    Reserved under every prefix, and the partition IS owned by this name, so an
 *    explicit delete of it is honoured.
 *  - `no-slot` — slot-less consumers park a per-slot preference here. Writer:
 *    `busySendModeKey` in `components/BusySendButton.tsx`. Reserved under that
 *    one prefix so a real slot spelled the same still loses everything else it
 *    owns, and spared even on an explicit delete because the parked value is
 *    shared state belonging to the slot-less case rather than to any slot.
 *
 *  Residual: a slot named exactly like a reserved name is indistinguishable from
 *  the reserved value under a shared prefix. */
const RESERVED_NAMES: ReadonlyMap<string, {
  /** Prefixes the name is reserved under; `null` means every prefix. */
  prefixes: ReadonlySet<string> | null
  /** Whether an explicit per-session delete must spare it too. */
}> = new Map([
  ['artifacts-gallery', { prefixes: null }],
  ['no-slot', { prefixes: new Set(['mc-busy-send-mode:']) }],
])

/** Whether `sessionId` under `prefix` is a reserved name the given sweep must
 *  leave alone. One predicate, so a name registered for one sweep cannot be
 *  silently missing from the other. */
const isReservedName = (sessionId: string, prefix: string): boolean => {
  const reserved = RESERVED_NAMES.get(sessionId)
  if (!reserved) return false
  return reserved.prefixes === null || reserved.prefixes.has(prefix)
}

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
        if (sessionId && !isReservedName(sessionId, prefix) && !liveSessionIds.has(sessionId)) {
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

