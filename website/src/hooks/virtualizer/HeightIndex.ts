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
 *
 * ANNOUNCING A CHANGE
 * ===================
 * The owner is also the subscribable store for "the geometry moved". Callers do
 * NOT maintain an invalidation token: `syncAndAnnounce` mutates the tree and, if
 * the total actually moved, bumps a version and notifies subscribers in one step,
 * so a mutation cannot reach the tree without being announced. Read the version
 * through `subscribe` / `getVersion` (React's `useSyncExternalStore` shape).
 *
 * The announce threshold lives here rather than at the call site because it is a
 * property of height truth, not of any one consumer: a sub-pixel total change is
 * not worth a re-render, and announcing every sync would re-render on every
 * measurement -- the render storm the caller's debounce exists to prevent.
 */

/**
 * Minimum total-height movement, in px, that counts as a change worth
 * announcing. Sub-pixel drift from re-measuring the same rows must not schedule
 * a render, and the spacer cannot express it anyway.
 */
const ANNOUNCE_EPSILON_PX = 1

export class HeightIndex {
  readonly sessionId: string
  private readonly cache: HeightCache
  private readonly tree: OffsetIndex
  private readonly keyAt: RowKeyResolver
  private estimate: number
  // Bumped only by syncAndAnnounce, and only when the total actually moved. The
  // subscribed value is what tells a consumer its cached geometry is stale.
  private version = 0
  // Total as of the last ANNOUNCED change. Starts at -1 ("nothing announced
  // yet") so the first real total always announces, including a total of 0.
  private lastAnnouncedTotal = -1
  private readonly listeners = new Set<() => void>()

  constructor(
    sessionId: string,
    options: {
      rowCount?: number
      keyAt: RowKeyResolver
      estimate: number
    },
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
    // A content-aware per-row estimate beats the running mean where content is
    // bimodal: a code-fenced row is often 5-30x the mean, and mounting it near
    // the top used to land a huge height correction that read as a scroll jump
    // (which the top-parked pagination poll then amplified into runaway page
    // loads). The resolver answers only for rows it can price (code fences at
    // known per-line metrics); everything else keeps the measured mean.
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
   * Retire measurements for rows that have LEFT the list, so their heights stop
   * pricing the unmeasured rows that remain. The measurements are KEPT and only
   * dropped from the mean, so an optimistic removal that gets rolled back
   * restores exact geometry -- see HeightCache.retire.
   *
   * Deliberately does NOT sync or announce: the caller retires during the render
   * that dropped the rows and the tree is re-synced in that same render, so the
   * reprice lands in the commit whose shift is already compensated.
   */
  retire(keys: Iterable<string>): void {
    for (const key of keys) this.cache.retire(key)
  }

  /** Key-currency migration for rows that SURVIVED a regroup under a new
   *  display key (see HeightCache.rename). Like retire(), deliberately does
   *  not sync or announce: the caller runs during the render whose commit
   *  is already compensated. */
  rename(pairs: Iterable<readonly [string, string]>): void {
    for (const [oldKey, newKey] of pairs) this.cache.rename(oldKey, newKey)
  }

  /**
   * Height getter for the O(N) free functions that still take one
   * (`getOffset` / `getTotalHeight` / `computeWindow`).
   *
   * A stable bound property, not a method reference, so callers can pass it
   * without rebinding and without widening their own dependency lists.
   */
  readonly getHeight = (index: number): number => this.heightAt(index)

  /**
   * Revive every retired key whose row is LIVE again -- BEFORE anything reads a
   * height.
   *
   * A key resolving from a live row index falsifies the premise of its
   * retirement (see HeightCache.retire): the row is in the list again, an
   * optimistically removed row the server refused and restored wholesale. This
   * is the only available signal, because a restore has no commit shape of its
   * own to hook -- an optimistic TAIL truncation comes back as a plain append.
   *
   * It runs as a PASS BEFORE the tree walk rather than inside the height read,
   * because the walk prices rows in index order and reads the mean per row: a
   * revive partway through would leave every row priced earlier in that same
   * walk holding the pre-revive mean, with no later sync guaranteed to correct
   * them (a transcript that goes idle right after the rollback gets none). One
   * pass first makes the whole tree consistent with one mean.
   *
   * Skipped outright when nothing is retired, which is the overwhelmingly common
   * case, so the ordinary sync pays one integer comparison.
   */
  private reviveLiveRows(itemCount: number): void {
    if (!this.cache.hasRetired()) return
    for (let i = 0; i < itemCount; i++) {
      const key = this.keyAt(i)
      if (key !== null) this.cache.reviveIfRetired(key)
    }
  }

  /** Reconcile the prefix-sum tree with the current row count and heights. */
  sync(itemCount: number): void {
    this.reviveLiveRows(itemCount)
    this.tree.sync(itemCount, this.getHeight)
  }

  /**
   * Reconcile the tree and, if the total moved, announce it to subscribers.
   *
   * For a mutation that happens OUTSIDE render (a measurement batch settling, the
   * streaming tick). Use plain `sync` from inside render, where the reader is
   * about to read fresh values anyway and notifying would be a state update
   * during render.
   *
   * `beforeNotify` runs synchronously AFTER the tree is mutated but BEFORE
   * subscribers are notified, and only when a change is actually being announced.
   * That slot exists because the caller has to capture pre-commit DOM geometry
   * (the top visible row, so a spacer reprice can be compensated) and the capture
   * is only valid before the re-render this announcement schedules. Passing it in
   * makes the ordering unconditional instead of relying on when React happens to
   * flush the update.
   *
   * Deliberately does NOT update the announced baseline when nothing is
   * announced, so a later sync still sees the full accumulated delta.
   */
  syncAndAnnounce(itemCount: number, beforeNotify?: () => void): void {
    this.reviveLiveRows(itemCount)
    this.tree.sync(itemCount, this.getHeight)
    const total = this.tree.totalHeight()
    if (Math.abs(total - this.lastAnnouncedTotal) <= ANNOUNCE_EPSILON_PX) return
    this.lastAnnouncedTotal = total
    beforeNotify?.()
    this.version += 1
    for (const listener of this.listeners) listener()
  }

  /**
   * Subscribe to announced geometry changes. Returns an unsubscribe function.
   *
   * A stable bound property so React's `useSyncExternalStore` does not resubscribe
   * on every render; its identity changes only when the owner itself is replaced,
   * which is exactly when a consumer should rebind (a session switch).
   */
  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /**
   * Current announced version -- the `getSnapshot` half of the store.
   *
   * A plain number so successive reads are `Object.is`-equal until something is
   * actually announced, which is what keeps `useSyncExternalStore` from looping.
   */
  readonly getVersion = (): number => this.version

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
