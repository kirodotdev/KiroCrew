import type { ChatSlot } from '../types'
import type { RootState } from './index'

/**
 * A slot's newest message time (`last_ts`), read through one place.
 *
 * While a turn streams, every assistant or tool row moves `last_ts`. Writing
 * that onto the row would replace the row and the whole `dashboard.slots`
 * array once per row, re-rendering everything that subscribes to the list
 * (the chat page, the sidebar shell, tab strips, every row menu) for a value
 * none of them shows: the list orders and labels by `last_turn_ts` once a row
 * has one. So `touchSlotActivity` writes those mid-turn bumps into
 * `dashboard.slotLastTs` instead, and the row's own `last_ts` is the server's
 * last value or a settled bump.
 *
 * The real value is therefore the newer of the two, and every reader of
 * `last_ts` must ask for it here; `src/test/slotLastTs.readers.test.ts`
 * fails on a direct read elsewhere. A reader that misses the overlay sees a
 * stale unread watermark or a stale recency order.
 */

/** Overlay key. Prefixed so a slot key such as `__proto__` cannot reach the
 *  record's prototype. */
export const slotLastTsKey = (slotKey: string): string => `k:${slotKey}`

/** The later of two ISO timestamps; an unparseable or missing one loses. */
export const newerTs = (a: string | undefined, b: string | undefined): string | undefined => {
  if (!a) return b
  if (!b) return a
  const ta = Date.parse(a)
  const tb = Date.parse(b)
  if (!Number.isFinite(tb)) return a
  if (!Number.isFinite(ta)) return b
  return ta >= tb ? a : b
}

type Overlay = Record<string, string> | undefined

/** `slot.last_ts` with the mid-turn overlay applied. */
export const slotLastTsWith = (slot: Pick<ChatSlot, 'key' | 'last_ts'>, overlay: Overlay): string | undefined =>
  newerTs(overlay?.[slotLastTsKey(slot.key)], slot.last_ts) // last-ts: the helper itself

/** Newest message time of `key` in a dashboard state: for reducers and
 *  `getState()` reads (unread watermarks). */
export const slotLastTsOf = (
  dashboard: { slots?: ChatSlot[]; slotLastTs?: Record<string, string> } | undefined,
  key: string,
): string | undefined => {
  const row = dashboard?.slots?.find(s => s.key === key) // row-read: returns a timestamp
  const overlay = dashboard?.slotLastTs?.[slotLastTsKey(key)]
  return row ? slotLastTsWith(row, dashboard?.slotLastTs) : overlay
}

/** Drop overlay entries a row has caught up with, in a reducer draft. An
 *  older row value keeps its entry: a list serialized before the bump must
 *  not move recency back. */
export const dropCaughtUpLastTs = (
  dashboard: { slotLastTs?: Record<string, string> },
  rows: readonly Pick<ChatSlot, 'key' | 'last_ts'>[],
): void => {
  const overlay = dashboard.slotLastTs
  if (!overlay || !Object.keys(overlay).length) return
  for (const row of rows) {
    const k = slotLastTsKey(row.key)
    const held = overlay[k]
    if (held === undefined) continue
    if (Date.parse(row.last_ts ?? '') >= Date.parse(held)) delete overlay[k] // last-ts: the helper itself
  }
}

/** Advance `key`'s overlay entry to `ts` in a reducer draft. Never moves
 *  backwards, and an equal time is no write. Returns whether it wrote. */
export const bumpLastTsOverlay = (
  dashboard: { slots?: ChatSlot[]; slotLastTs?: Record<string, string> },
  key: string,
  ts: string,
): boolean => {
  const t = Date.parse(ts)
  const current = slotLastTsOf(dashboard, key)
  if (!Number.isFinite(t) || (current && Date.parse(current) >= t)) return false
  ;(dashboard.slotLastTs ??= {})[slotLastTsKey(key)] = ts
  return true
}

/** Drop overlay entries for keys an authoritative list no longer holds. */
export const pruneLastTsOverlay = (dashboard: { slotLastTs?: Record<string, string> }, liveKeys: Set<string>): void => {
  for (const k of Object.keys(dashboard.slotLastTs ?? {})) {
    if (!liveKeys.has(k.slice(2))) delete dashboard.slotLastTs![k]
  }
}

export const selectSlotLastTs = (state: RootState, key: string): string | undefined =>
  slotLastTsOf(state.dashboard, key)

/** `rows` with each row's `last_ts` replaced by its live value. Rows the
 *  overlay does not move keep their identity, and so does the array when none
 *  moved. For surfaces that sort or label by message recency (palette, command
 *  bar, Worlds, broadcast picker); they re-render on every mid-turn bump, which
 *  is what they need while open. */
export const withLiveLastTs = <T extends Pick<ChatSlot, 'key' | 'last_ts'>>(rows: readonly T[], overlay: Overlay): T[] => {
  if (!overlay || !Object.keys(overlay).length) return rows as T[]
  let moved = false
  const out = rows.map(row => {
    const live = slotLastTsWith(row, overlay)
    if (live === row.last_ts) return row // last-ts: compares against the live value
    moved = true
    return { ...row, last_ts: live }
  })
  return moved ? out : (rows as T[])
}

let memo: { slots: ChatSlot[]; overlay: Overlay; out: ChatSlot[] } | undefined

/** The slot list with live `last_ts` values (see `withLiveLastTs`). Memoized
 *  on the two inputs rather than on the state object, so it returns the same
 *  array until the list or the overlay changes. */
export const selectSlotsWithLiveLastTs = (state: RootState): ChatSlot[] => {
  const { slots, slotLastTs: overlay } = state.dashboard
  if (memo && memo.slots === slots && memo.overlay === overlay) return memo.out
  memo = { slots, overlay, out: withLiveLastTs(slots, overlay) }
  return memo.out
}
