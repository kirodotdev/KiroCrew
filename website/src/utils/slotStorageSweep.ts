/** The localStorage orphan sweep a live `slots` frame runs.
 *
 *  The boot sweep in `shell/appShell.tsx` runs once per page load, so a tab
 *  left open keeps every session closed elsewhere (another tab, another device,
 *  an agent) until the next reload. This runs the same `gcOrphanedStorage`
 *  against each complete pushed list instead, under the same rules the
 *  in-memory reconcile (`reconcileSlotResidue`) uses: the active slot and both
 *  `safeKey()` spellings are live, and the gateway's drain hold protects every
 *  key it names. */
import { gcOrphanedStorage } from './storageGc'
import { slotListHoldTest, type SlotListHold } from './slotListHold'
import { isUnsafeKey } from './sanitize'

export interface SlotStorageSweepInput {
  /** The rows of the frame just applied. */
  rows: readonly { key: string }[]
  /** Keys the dashboard list still carries after the frame (rows the reducer
   *  kept, such as a session this tab just created). */
  keptKeys: readonly string[]
  activeSlot: string | null
  hold: SlotListHold
  /** Whether a real snapshot had been seen before this frame. */
  snapshotSeenBefore: boolean
}

/** A sweep with its own memory of the last complete key set. It walks
 *  localStorage only when a complete frame has lost a key since the last one,
 *  so the frequent identical-keyed frames stay off the storage walk. */
export function createSlotStorageSweep(): (input: SlotStorageSweepInput) => number {
  let lastKeys: Set<string> | null = null
  return ({ rows, keptKeys, activeSlot, hold, snapshotSeenBefore }) => {
    // A partial list omits sessions a restart has not restored yet.
    if (hold.partial) return 0
    // The empty frame a reconnect can send before the first snapshot is not a list.
    if (rows.length === 0 && !snapshotSeenBefore) return 0
    const keys = new Set(rows.map(row => row.key))
    const previous = lastKeys
    lastKeys = keys
    if (!previous || ![...previous].some(key => !keys.has(key))) return 0
    const live = new Set([...keys, ...keptKeys])
    if (activeSlot) live.add(activeSlot)
    // The `safeKey()` spelling, written out because `safeKey` is private to
    // the chat store.
    for (const key of [...live]) if (isUnsafeKey(key)) live.add(`unsafe-key:${key}`)
    const held = slotListHoldTest(hold)
    const names = (slot: string) => live.has(slot) || held(slot)
    // A slot key can contain ':' (a channel key such as `slack:<thread>`), so
    // the first segment is not always the slot. Match the whole rest or any
    // prefix of it that ends at a ':'.
    const namesSlot = (scoped: string): boolean => {
      for (let end = scoped.indexOf(':'); end !== -1; end = scoped.indexOf(':', end + 1)) {
        if (names(scoped.slice(0, end))) return true
      }
      return names(scoped)
    }
    return gcOrphanedStorage(live, scoped => namesSlot(scoped)
      || TRANSCRIPT_HOSTS.some(host => scoped.startsWith(host) && namesSlot(scoped.slice(host.length))))
  }
}

/** Host prefixes a hosted transcript puts before its slot in the virtualizer
 *  `sessionId` (`pane:<slot>` in `components/ChatPane.tsx`, `side:<slot>` in
 *  `pages/chat/SideChat.tsx`, `embed:<slot>` in `app-sdk/ChatEmbed.tsx`), so its
 *  scroll anchor is stored as `vc_anchor3_pane:<slot>`. */
const TRANSCRIPT_HOSTS = ['pane:', 'side:', 'embed:'] as const
