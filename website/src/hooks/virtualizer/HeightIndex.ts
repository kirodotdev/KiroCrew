import { HeightCache } from './HeightCache'
import { OffsetIndex } from './WindowCalculator'

/**
 * Resolves a row index to its stable height-cache key, or `null` when the index
 * addresses no live row.
 *
 * Height identity keys on the row's stable key (`meta.clientTs`-derived), never
 * the array index -- a steered bubble's `ts` is rewritten by the server echo, so
 * an index-keyed measurement would be orphaned. The resolver is late-bound (it
 * reads the caller's live refs at call time) because the owner is constructed
 * during render, one statement before the item array it will be asked about.
 */
type RowKeyResolver = (index: number) => string | null

/**
 * HeightIndex -- the single read surface for row heights.
 *
 * WHY THIS EXISTS
 * ===============
 * Height truth used to be read from two places that had to agree: `HeightCache`
 * (keyed, persisted) was read directly at five call sites, while `OffsetIndex`
 * (a Fenwick prefix-sum tree over the same heights) answered the offset math
 * from its own cached copy, fed by a getter that read the cache. Nothing in the
 * types stopped a read of one from disagreeing with the other, and each
 * structure carried its OWN session guard -- both had to be present and agree
 * for a session switch to be correct. A same-item-count switch that satisfied
 * only one of them served the previous transcript's heights, which reads to a
 * user as the transcript opening at the wrong scroll position.
 *
 * This owner collapses that seam. It holds the cache and the tree, so:
 *   - the tree cannot outlive its cache: a session change constructs a new
 *     HeightIndex, and there is exactly ONE guard to get right;
 *   - callers never touch `HeightCache`, which is left as what it always was
 *     underneath -- load / store / flush / evict, i.e. persistence.
 *
 * `OffsetIndex` is deliberately left untouched as the pure Fenwick primitive
 * (index + height-getter, its own tests). This class is the seam the chat hook
 * consumes; the tree stays a data structure with no opinion about sessions,
 * keys, or persistence.
 *
 * TWO DIFFERENT QUESTIONS
 * =======================
 * The read surface is not one method, because callers ask two things that must
 * not be conflated:
 *
 *   - `getHeight(i)` -- the RESOLVED height: the measurement if there is one,
 *     otherwise the running-mean estimate. This is what the offset math needs;
 *     every row has an answer.
 *   - `peekMeasured(i)` / `readMeasured(i)` -- the measurement ITSELF, or
 *     `undefined` when the row has never been measured.
 *
 * The distinction is load-bearing, not stylistic. The ResizeObserver tells a
 * first mount apart from a genuine resize by exactly this: an absent previous
 * measurement means the row just mounted during scroll-driven window expansion,
 * and re-pinning then would yank a scrolling reader. Answering that question
 * with a resolved height (never `undefined`) would classify every first mount as
 * a resize. A single accessor would have made that regression a silent one.
 *
 * PROMOTING VS NON-PROMOTING
 * ==========================
 * `HeightCache` keeps LRU order by access, and promotion is expressed here
 * rather than left to which method a caller happened to reach for:
 *
 *   - `getHeight` and `peekMeasured` do NOT promote. They feed bulk scans that
 *     touch every row (tree sync, offset math, the debug probe); promoting on
 *     those would rewrite LRU order into transcript-index order and evict rows
 *     the user just viewed.
 *   - `readMeasured` DOES promote. It is for a row that is actually mounted or
 *     rendering, which is genuine access.
 */
export class HeightIndex {
  readonly sessionId: string
  private readonly cache: HeightCache
  private readonly tree: OffsetIndex
  private readonly keyAt: RowKeyResolver
  private estimate: number

  constructor(
    sessionId: string,
    options: { rowCount?: number; keyAt: RowKeyResolver; estimate: number },
  ) {
    this.sessionId = sessionId
    this.keyAt = options.keyAt
    this.estimate = options.estimate
    this.cache = new HeightCache(sessionId, { rowCount: options.rowCount })
    // Built empty and filled by the caller's first sync(), NOT from keyAt here:
    // the resolver reads refs that are assigned after this constructor runs, so
    // resolving a key during construction would read them in their initial
    // state. The caller syncs in the same render, before any offset is read.
    this.tree = new OffsetIndex(0, () => 0)
  }

  /**
   * Resolved height for row `index` -- the measurement if present, else the
   * running mean of measured heights. Non-promoting.
   *
   * Private: `getHeight` below is the single public spelling of this read, because
   * the O(N) free functions take it as a callback. Exposing both would leave two
   * names for one read -- the shape this class exists to remove.
   *
   * `Math.max(h, 1)` so a zero-height row still registers with IntersectionObserver.
   * The unmeasured fallback is the running MEAN rather than the configured flat
   * estimate: measured in a real browser on a bimodal transcript, holding the
   * flat guess until the sample grew made the peak scrollHeight correction far
   * worse (see HeightCache.averageHeight).
   */
  private heightAt(index: number): number {
    const key = this.keyAt(index)
    if (key === null) return this.estimate
    const cached = this.cache.peek(key)
    if (cached !== undefined) return Math.max(cached, 1)
    return this.cache.averageHeight(this.estimate)
  }

  /** The measurement for `index`, or `undefined` if never measured. Non-promoting. */
  peekMeasured(index: number): number | undefined {
    const key = this.keyAt(index)
    return key === null ? undefined : this.cache.peek(key)
  }

  /**
   * The measurement for `index`, or `undefined` if never measured, recording
   * genuine access (LRU promotion). For rows that are mounted or rendering.
   */
  readMeasured(index: number): number | undefined {
    const key = this.keyAt(index)
    return key === null ? undefined : this.cache.get(key)
  }

  /** Record a measured height for `index`. No-op when the index addresses no row. */
  setMeasured(index: number, height: number): void {
    const key = this.keyAt(index)
    if (key === null) return
    this.cache.set(key, height)
  }

  /**
   * Height getter for the O(N) free functions that still take one
   * (`getOffset` / `getTotalHeight` / `computeWindow`).
   *
   * A stable bound property, not a method reference, so callers can pass it
   * without rebinding and without widening their own dependency lists.
   */
  readonly getHeight = (index: number): number => this.heightAt(index)

  /** Reconcile the prefix-sum tree with the current row count and heights. */
  sync(itemCount: number): void {
    this.tree.sync(itemCount, this.getHeight)
  }

  /** Cumulative height of rows [0, index). O(log N). */
  offsetOf(index: number): number {
    return this.tree.offsetOf(index)
  }

  /** Sum of all row heights. O(1). */
  totalHeight(): number {
    return this.tree.totalHeight()
  }

  /** Row index whose vertical span contains `scrollTop`. O(log N). */
  indexAt(scrollTop: number): number {
    return this.tree.indexAt(scrollTop)
  }

  /**
   * Update the flat estimate used for rows with no measurement and no sample.
   *
   * The caller re-asserts this each render because it is an option that can
   * change; a change must be followed by a `sync()` so the tree picks up the new
   * estimates for still-unmeasured rows.
   */
  setEstimate(estimate: number): void {
    this.estimate = estimate
  }

  /** Raise the session row count driving the eviction cap (high-water mark). */
  setRowCount(rowCount: number): void {
    this.cache.setRowCount(rowCount)
  }

  /** Persist pending measurements. */
  flush(): void {
    this.cache.flush()
  }
}
