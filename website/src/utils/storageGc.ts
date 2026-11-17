/**
 * localStorage garbage collection.
 *
 * Removes orphaned per-session keys that accumulate unboundedly and
 * eventually overflow the ~5 MB origin quota, white-screening the app.
 *
 * Entry point: `gcOrphanedStorage(liveIds)` removes keys for sessions that no
 * longer exist. Runs at boot, and again when a complete live slot list drops a
 * key (`utils/slotStorageSweep.ts`).
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
  // The caught-up marker (`chat-core/transcript/caughtUpMarker.ts`): keyed by
  // the raw slot key, so it is swept with the slot's heights and anchors.
  'vc_seen_',
  // The pre-version-bump anchor prefixes. `ScrollAnchorCache` reaps these
  // outright when it loads, but that only happens once the chat virtualizer is
  // on screen — so the boot sweep keeps owning them for a user who never opens a
  // chat. Dropping a line when the prefix was bumped would leave those keys
  // uncollectable for exactly the sessions that are already gone.
  'vc_anchor2_',
  'vc_anchor_',
  'kirocrew:touched-files:',
  'mc-panel-tabs:',
  // Per-chat side panel width and bottom-dock height. Writer: `sidePanelDimKey`
  // over `SIDE_PANEL_WIDTH_KEY` / `SIDE_PANEL_HEIGHT_KEY` in
  // `pages/chat/sidePanelWidth.ts`. The bare base key, read as the fallback for
  // every chat, carries no ':' and so never matches either prefix.
  'mc-side-panel-width:',
  'mc-side-panel-height:',
  'mc-activity-open:',
  'mc-webpreview-url:',
  'mc-webpreview-pending:',
  'mc-webpreview-applied:',
  'mc-busy-send-mode:',
  // No live writer: the one-time Dashboard hint card wrote this flag before the
  // dock became persistent. Kept so a browser that already carries the keys
  // still has them collected.
  'mc-task-dashboard-dismissed:',
] as const

/** Names that appear where a session id is expected but are not sessions.
 *
 *  The orphan sweep spares them: it is *guessing* which names are dead, and a
 *  non-session name looks dead forever.
 *
 *  Each name must stay byte-identical to its writer; a divergence collects the
 *  parked value silently.
 *
 *  - `artifacts-gallery` — the virtualizer partitions its height cache by
 *    `sessionId`, and the gallery is not a chat session but still has to name a
 *    partition. Writer: `ARTIFACT_HEIGHT_NS` in `pages/ArtifactsPageGallery.tsx`.
 *    Reserved under every prefix.
 *  - `no-slot` — slot-less consumers park a per-slot preference here. Writer:
 *    `busySendModeKey` in `components/BusySendButton.tsx`. Reserved under that
 *    one prefix so a real slot spelled the same still loses everything else it
 *    owns.
 *
 *  Residual: a slot named exactly like a reserved name is indistinguishable from
 *  the reserved value under a shared prefix. */
const RESERVED_NAMES: ReadonlyMap<string, {
  /** Prefixes the name is reserved under; `null` means every prefix. */
  prefixes: ReadonlySet<string> | null
}> = new Map([
  ['artifacts-gallery', { prefixes: null }],
  ['no-slot', { prefixes: new Set(['mc-busy-send-mode:']) }],
])

/** Whether `sessionId` under `prefix` is a reserved name the sweep must leave alone. */
const isReservedName = (sessionId: string, prefix: string): boolean => {
  const reserved = RESERVED_NAMES.get(sessionId)
  if (!reserved) return false
  return reserved.prefixes === null || reserved.prefixes.has(prefix)
}

/**
 * Remove localStorage keys belonging to sessions not in `liveSessionIds`.
 * `isKept` receives everything after the prefix and spares the key when it
 * answers true, so a caller can keep keys whose slot key itself contains ':'
 * or that it cannot list but must not lose.
 *
 * Returns the number of keys removed.
 */
export function gcOrphanedStorage(
  liveSessionIds: Set<string>,
  isKept: (scoped: string) => boolean = () => false,
): number {
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
        const scoped = key.slice(prefix.length)
        const sessionId = scoped.split(':')[0]
        if (sessionId && !isReservedName(sessionId, prefix) && !liveSessionIds.has(sessionId) && !isKept(scoped)) {
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
