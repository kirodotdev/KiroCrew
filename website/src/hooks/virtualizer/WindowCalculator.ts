// Pure-function windowing math for the chat virtualizer.
//
// Given a scroll position, viewport height, item count, and a per-item
// height getter, compute which contiguous range of items must be mounted
// (visible items + overscan above/below). Also exposes `getOffset` (sum
// of heights up to an index) and `getIndexAtOffset` (inverse).
//
// Linear scan with early termination: O(N) worst case but amortized O(K)
// where K is the visible window. For 1000 items at ~80px average, this is
// <1ms.
//
// At 5000+ items the O(N) offset/total walks dominate rAF-throttled scroll
// frames and the 120ms streaming tick — the main jank source. `OffsetIndex`
// (bottom of this file) is the prefix-sum tree the header long promised: a
// Fenwick/binary-indexed tree that answers the same offset/total/index
// questions in O(log N) / O(1). The free functions below are retained
// verbatim (identical signatures + behaviour) so existing call sites and
// tests keep working; hot paths opt into `OffsetIndex` instead.

export interface WindowRange {
  /** First index to mount (inclusive). */
  start: number
  /** One past the last index to mount (exclusive). */
  end: number
}

/** Height getter — must return a non-negative finite number for every index in [0, count). */
export type HeightGetter = (index: number) => number

/**
 * Compute the mounted window range for a scroll state.
 *
 * The returned range satisfies:
 *   - 0 ≤ start ≤ end ≤ itemCount
 *   - All visible items (those overlapping the viewport rect) are inside
 *     [start, end), with `overscan` extra items on each side (clamped to bounds).
 *   - For empty lists (itemCount === 0), returns { start: 0, end: 0 }.
 */
export function computeWindow(
  scrollTop: number,
  viewportHeight: number,
  itemCount: number,
  getHeight: HeightGetter,
  overscan: number,
): WindowRange {
  if (itemCount <= 0) return { start: 0, end: 0 }
  // Defensive clamps — pathological inputs (negative scroll, NaN height)
  // shouldn't crash the renderer.
  const top = Math.max(0, scrollTop)
  const bottom = top + Math.max(0, viewportHeight)
  const overscanN = Math.max(0, Math.floor(overscan))

  // Walk forward accumulating heights. Track the first index whose bottom
  // edge passes `top` (firstVisible) and the first index whose top edge
  // passes `bottom` (firstAfter).
  let offset = 0
  let firstVisible = -1
  let firstAfter = -1
  for (let i = 0; i < itemCount; i++) {
    const h = Math.max(0, getHeight(i) || 0)
    const itemBottom = offset + h
    if (firstVisible === -1 && itemBottom > top) {
      firstVisible = i
    }
    if (firstVisible !== -1 && offset >= bottom) {
      firstAfter = i
      break
    }
    offset = itemBottom
  }
  // Edge case: scroll position is past the end of all content. Show the
  // tail (last item plus overscan above) so the user sees content.
  if (firstVisible === -1) firstVisible = itemCount - 1
  if (firstAfter === -1) firstAfter = itemCount

  const start = Math.max(0, firstVisible - overscanN)
  const end = Math.min(itemCount, firstAfter + overscanN)
  return { start, end }
}

/**
 * Compute the cumulative pixel offset at the top edge of `index`.
 *
 * For index === 0, returns 0. For index === itemCount, returns the total
 * content height. Out-of-range indices are clamped.
 */
export function getOffset(
  index: number,
  itemCount: number,
  getHeight: HeightGetter,
): number {
  const target = Math.max(0, Math.min(itemCount, Math.floor(index)))
  let offset = 0
  for (let i = 0; i < target; i++) {
    offset += Math.max(0, getHeight(i) || 0)
  }
  return offset
}

/**
 * Inverse of getOffset: find the index whose vertical span contains `pixelOffset`.
 *
 * Returns the largest index `i` such that the cumulative height of items
 * 0..i-1 is ≤ pixelOffset. For pixelOffset ≥ totalHeight returns
 * itemCount - 1 (clamped). For empty lists returns 0.
 */
export function getIndexAtOffset(
  pixelOffset: number,
  itemCount: number,
  getHeight: HeightGetter,
): number {
  if (itemCount <= 0) return 0
  const target = Math.max(0, pixelOffset)
  let offset = 0
  for (let i = 0; i < itemCount; i++) {
    const h = Math.max(0, getHeight(i) || 0)
    if (offset + h > target) return i
    offset += h
  }
  return itemCount - 1
}

/**
 * Compute a window centered on `targetIndex` for jump navigation.
 *
 * The returned range always includes `targetIndex` (provided 0 ≤ targetIndex < itemCount)
 * and has approximately `2 * overscan + 1` items, clamped to list bounds.
 */
export function computeJumpWindow(
  targetIndex: number,
  itemCount: number,
  overscan: number,
): WindowRange {
  if (itemCount <= 0) return { start: 0, end: 0 }
  const t = Math.max(0, Math.min(itemCount - 1, Math.floor(targetIndex)))
  const overscanN = Math.max(0, Math.floor(overscan))
  const start = Math.max(0, t - overscanN)
  const end = Math.min(itemCount, t + overscanN + 1)
  return { start, end }
}

/** Sum of all item heights — used to size the scroll container. */
export function getTotalHeight(itemCount: number, getHeight: HeightGetter): number {
  let total = 0
  for (let i = 0; i < itemCount; i++) {
    total += Math.max(0, getHeight(i) || 0)
  }
  return total
}

/**
 * Expand the window upward by `overscan` items, clamped at 0.
 *
 * Pure: returns the same object identity if start is already 0, otherwise
 * a new range. Used by the top-sentinel IntersectionObserver to load
 * older items as the user scrolls up.
 */
export function expandWindowUp(
  range: WindowRange,
  overscan: number,
): WindowRange {
  if (range.start === 0) return range
  const newStart = Math.max(0, range.start - Math.max(0, overscan))
  return { start: newStart, end: range.end }
}

/**
 * Expand the window downward by `overscan` items, clamped at itemCount.
 *
 * Pure: returns the same object identity if end is already itemCount,
 * otherwise a new range. Used by the bottom-sentinel
 * IntersectionObserver to load newer items in jump-mode scenarios.
 */
export function expandWindowDown(
  range: WindowRange,
  itemCount: number,
  overscan: number,
): WindowRange {
  if (range.end >= itemCount) return range
  const newEnd = Math.min(itemCount, range.end + Math.max(0, overscan))
  return { start: range.start, end: newEnd }
}

/**
 * Normalize a raw height reading the same way the free functions above do:
 * `getHeight(i) || 0` coerces NaN / undefined / 0 to 0, then Math.max clamps
 * negatives to 0. Keeping this identical guarantees OffsetIndex answers match
 * getOffset / getIndexAtOffset / getTotalHeight for every input.
 */
function normHeight(h: number): number {
  return Math.max(0, h || 0)
}

/**
 * OffsetIndex — a Fenwick (binary-indexed) tree over per-row heights.
 *
 * Answers the exact same questions as the O(N) free functions, but at the
 * cost profile the hot paths need:
 *   - offsetOf(index)  — cumulative height of rows [0, index)  → O(log N)
 *   - indexAt(scrollTop) — row whose span contains scrollTop   → O(log N)
 *   - totalHeight()    — sum of all row heights                → O(1)
 *
 * `sync(itemCount, getHeight)` reconciles the tree with the current data:
 *   - itemCount unchanged → diff the prefix and point-update only the rows
 *     whose height actually changed (refined measurements). No changed rows
 *     ⇒ zero tree writes (cheap no-op).
 *   - itemCount grew      → diff the overlap, then append the new tail rows
 *     in amortized O(1) each (transcripts grow at the tail).
 *   - itemCount shrank    → rebuild in O(N) (rare: rows removed).
 *
 * USAGE NOTE for callers (sibling B): the whole point is that scroll frames
 * become O(log N). Call `sync()` when the data changes (new rows, or a batch
 * of measurements landed — e.g. the 120ms streaming tick), NOT on every rAF
 * scroll frame. On pure scroll frames call only offsetOf / indexAt /
 * totalHeight. A same-itemCount sync still O(N)-scans the prefix to pick up
 * refined measurements, so calling it per scroll frame would re-introduce the
 * jank this class exists to remove.
 *
 * Behaviour matches getIndexAtOffset for zero-height rows (both skip them to
 * the next occupied / last row) and clamps out-of-range inputs identically.
 */
export class OffsetIndex {
  private n = 0
  // 1-indexed Fenwick tree; tree[0] is unused padding. tree.length === n + 1.
  private tree: number[] = [0]
  // Cached, normalized per-row heights (0..n-1) so sync() can diff in place.
  private heights: number[] = []
  // Running total kept in lockstep with the tree so totalHeight() is O(1).
  private total = 0

  constructor(itemCount: number, getHeight: HeightGetter) {
    this.sync(itemCount, getHeight)
  }

  /** Reconcile the tree with the current item count + heights. */
  sync(itemCount: number, getHeight: HeightGetter): void {
    const count = Math.max(0, Math.floor(itemCount))
    if (count < this.n) {
      // Shrink (rows removed) — cheapest to rebuild from scratch.
      this.rebuild(count, getHeight)
      return
    }
    // Diff the overlapping prefix: pick up refined measurements in place.
    // Untouched rows cost one comparison and zero tree writes.
    for (let i = 0; i < this.n; i++) {
      const h = normHeight(getHeight(i))
      const prev = this.heights[i]
      if (h !== prev) {
        this.pointUpdate(i, h - prev)
        this.heights[i] = h
        this.total += h - prev
      }
    }
    // Append any new tail rows in amortized O(1) each.
    for (let i = this.n; i < count; i++) {
      const h = normHeight(getHeight(i))
      this.heights.push(h)
      this.tree.push(0)
      this.appendInit(i + 1) // 1-indexed position of the new row
      this.total += h
    }
    this.n = count
  }

  /** Cumulative height of rows [0, index). Clamped to [0, totalHeight]. O(log N). */
  offsetOf(index: number): number {
    let k = Math.floor(index)
    if (k <= 0) return 0
    if (k > this.n) k = this.n
    let s = 0
    for (let x = k; x > 0; x -= x & -x) s += this.tree[x]
    return s
  }

  /** Sum of all row heights. O(1). */
  totalHeight(): number {
    return this.total
  }

  /** Row index whose vertical span contains scrollTop. Clamped to [0, n-1]. O(log N). */
  indexAt(scrollTop: number): number {
    if (this.n <= 0) return 0
    // Coerce NaN / negative to 0 (matches getIndexAtOffset's Math.max(0, …)).
    const t = scrollTop > 0 ? scrollTop : 0
    // Fenwick binary lift: largest `pos` with prefixSum(pos) <= t. That `pos`
    // is exactly the count of rows fully above t, i.e. the row containing t.
    let highBit = 1
    while (highBit << 1 <= this.n) highBit <<= 1
    let pos = 0
    let remaining = t
    for (let pw = highBit; pw > 0; pw >>= 1) {
      const next = pos + pw
      if (next <= this.n && this.tree[next] <= remaining) {
        pos = next
        remaining -= this.tree[next]
      }
    }
    return pos >= this.n ? this.n - 1 : pos
  }

  /** Full O(N) rebuild — used on shrink and initial construction fallback. */
  private rebuild(count: number, getHeight: HeightGetter): void {
    this.n = count
    this.heights = new Array<number>(count)
    this.tree = new Array<number>(count + 1).fill(0)
    this.total = 0
    for (let i = 0; i < count; i++) {
      const h = normHeight(getHeight(i))
      this.heights[i] = h
      this.total += h
    }
    // O(n) in-place Fenwick construction: push each node's sum to its parent.
    for (let p = 1; p <= count; p++) {
      this.tree[p] += this.heights[p - 1]
      const parent = p + (p & -p)
      if (parent <= count) this.tree[parent] += this.tree[p]
    }
  }

  /** Initialize tree[p] for a freshly appended row from its already-built children. */
  private appendInit(p: number): void {
    let val = this.heights[p - 1]
    const end = p - (p & -p)
    for (let j = p - 1; j > end; j -= j & -j) val += this.tree[j]
    this.tree[p] = val
  }

  /** Add `delta` to row `index` and propagate up the tree. O(log N). */
  private pointUpdate(index: number, delta: number): void {
    for (let x = index + 1; x <= this.n; x += x & -x) this.tree[x] += delta
  }
}
