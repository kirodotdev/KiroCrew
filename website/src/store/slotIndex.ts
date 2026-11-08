/** Key-to-row lookup over `dashboard.slots`.
 *
 *  Selectors that resolve one slot's row run on every store notification, and
 *  `slots.find(x => x.key === key)` is a linear scan of every row each time.
 *  The index is built once per `slots` array identity and kept in a WeakMap,
 *  so it lives exactly as long as that array and needs no invalidation: any
 *  write to a row gives the array a new identity, and the next lookup rebuilds.
 *  It works from a selector and from a one-off `store.getState()` read alike. */
import type { ChatSlot } from '../types'

const INDEX = new WeakMap<readonly ChatSlot[], Map<string, ChatSlot>>()

function slotIndexOf(slots: readonly ChatSlot[]): Map<string, ChatSlot> {
  let index = INDEX.get(slots)
  if (!index) {
    index = new Map()
    // First row wins, the row `Array.prototype.find` would return.
    for (const row of slots) if (!index.has(row.key)) index.set(row.key, row)
    INDEX.set(slots, index)
  }
  return index
}

/** The row for `key`, the same row `slots.find(x => x.key === key)` returns,
 *  an empty key included. A null or undefined key matches no row, as a string
 *  key never equals either. Callers that must skip an empty key guard it. */
export function slotRowByKey(slots: readonly ChatSlot[], key: string | null | undefined): ChatSlot | undefined {
  return key == null ? undefined : slotIndexOf(slots).get(key)
}
