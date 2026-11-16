/** Slot read counts -- a dev-build-only tally, per slot, of the transcript reads
 *  `refreshSlot` and `warmSlotCache` make: every call that fetched (`reads`), the
 *  ones whose window walk fetched older pages (`walked`), how many older pages
 *  those walks took (`pages`) and why each walked (`reasons`). Bounded is
 *  `reads - walked`.
 *
 *  Read it from devtools with `window.__kcSlotReads()`, installed by the first
 *  recording. Nothing is stored, sent or printed, and a production build records
 *  nothing: `import.meta.env.DEV` is replaced with `false` there.
 */

export interface SlotReadCount {
  reads: number
  walked: number
  pages: number
  /** Walked calls per reason, keyed `refresh:<reason>` / `warm:<reason>`. */
  reasons: Record<string, number>
}

const tally = new Map<string, SlotReadCount>()

type DevWindow = { __kcSlotReads?: () => Record<string, SlotReadCount> }

function entry(key: string): SlotReadCount {
  if (typeof window !== 'undefined') (window as unknown as DevWindow).__kcSlotReads ??= slotReadCounts
  let e = tally.get(key)
  if (!e) {
    e = { reads: 0, walked: 0, pages: 0, reasons: {} }
    tally.set(key, e)
  }
  return e
}

/** One `refreshSlot` / `warmSlotCache` call that is about to send its first request. */
export function recordSlotRead(key: string): void {
  if (!import.meta.env.DEV) return
  entry(key).reads += 1
}

/** One walk that fetched `pages` (> 0) older pages, for the reason `countAs`. */
export function recordSlotWalk(key: string, countAs: string, pages: number): void {
  if (!import.meta.env.DEV) return
  const e = entry(key)
  e.walked += 1
  e.pages += pages
  e.reasons[countAs] = (e.reasons[countAs] ?? 0) + 1
}

/** A copy of the tally, keyed by slot. */
export function slotReadCounts(): Record<string, SlotReadCount> {
  return Object.fromEntries(
    [...tally].map(([key, e]) => [key, { ...e, reasons: { ...e.reasons } }]),
  )
}

/** Test reset: clears the tally and removes the devtools global. */
export function resetSlotReadCounts(): void {
  tally.clear()
  if (typeof window !== 'undefined') delete (window as unknown as DevWindow).__kcSlotReads
}
