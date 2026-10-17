// Reader-row DOM geometry for the chat virtualizer's anchors.
//
// Every compensation and restore in the hook asks the same two questions of
// the mounted rows: which row is the reader looking at, and where is that row
// on screen now. These are the pure answers, over the scroller, the
// Element -> index registry of mounted rows, and a caller-supplied identity
// resolver -- so the shift compensation (see shiftCompensation.ts) and the
// persisted reading position (see readingPosition.ts) resolve a row the same
// way. None of them writes anything.

// Capture the topmost visible mounted row (smallest index whose bottom edge
// is still below the viewport top) and its offset from the scroller's top.
// Pure over its inputs so it can run both from the hook's callbacks (live
// items) and from the slot-switch flush, which must resolve keys against the
// OUTGOING session's items snapshot. Returns null when no mounted row
// qualifies or the environment has no layout (jsdom). `index` is the row's
// index as the mounted node carries it — the PREVIOUS commit's, for a caller
// resolving across a list change.
export function captureTopAnchorFrom(
  el: HTMLDivElement,
  entries: Iterable<[Element, number]>,
  keyAt: (index: number) => string | null,
): { key: string; top: number; index: number } | null {
  if (typeof el.getBoundingClientRect !== 'function') return null
  const srTop = el.getBoundingClientRect().top
  let bestIdx = Infinity
  let bestTop = 0
  let bestKey: string | null = null
  for (const [node, idx] of entries) {
    const rect = (node as HTMLElement).getBoundingClientRect()
    const top = rect.top - srTop
    // Skip rows fully above the viewport top — they aren't the anchor the
    // user is looking at (their screen position is off-screen).
    if (rect.bottom - srTop <= 0) continue
    if (idx < bestIdx) {
      const key = keyAt(idx)
      if (key === null) continue
      bestIdx = idx
      bestTop = top
      bestKey = key
    }
  }
  return bestKey !== null ? { key: bestKey, top: bestTop, index: bestIdx } : null
}

/** Screen offset of the mounted row whose key matches, relative to the
 *  scroller's top; null when it is not mounted. Pure over its inputs like the
 *  capture above, so both anchor consumers resolve a row the same way. */
export function rowTopFrom(
  el: HTMLDivElement,
  entries: Iterable<[Element, number]>,
  keyAt: (index: number) => string | null,
  key: string,
): number | null {
  if (typeof el.getBoundingClientRect !== 'function') return null
  for (const [node, idx] of entries) {
    if (keyAt(idx) !== key) continue
    const srTop = el.getBoundingClientRect().top
    return (node as HTMLElement).getBoundingClientRect().top - srTop
  }
  return null
}

/** Collect up to 3 visible rows as height-anchor candidates, priced through
 *  `idOf` against the CURRENT items/registry pairing. Only meaningful when
 *  those two are consistent — i.e. not mid-prepend-commit, where the items
 *  have already advanced while the registry still carries pre-shift indices
 *  and every priced key is off by the inserted count (the capture tear behind
 *  the measured −721px dropped correction). */
export function captureAnchorCandsFrom<T>(
  el: HTMLElement,
  entries: Iterable<[Element, number]>,
  itemAt: (index: number) => T | undefined,
  idOf: (item: T, index: number) => string,
): { key: string; top: number }[] {
  if (typeof el.getBoundingClientRect !== 'function') return []
  const srTop = el.getBoundingClientRect().top
  const cands: { key: string; top: number }[] = []
  for (const [node, i] of entries) {
    if (typeof node.getBoundingClientRect !== 'function') continue
    const r = node.getBoundingClientRect()
    if (r.height <= 0 || r.bottom - srTop <= 0) continue
    const it = itemAt(i)
    if (!it) continue
    cands.push({ key: idOf(it, i), top: r.top - srTop })
  }
  cands.sort((x, y) => x.top - y.top)
  return cands.slice(0, 3)
}
