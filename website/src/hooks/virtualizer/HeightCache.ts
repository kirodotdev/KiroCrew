// Persistent height cache for the chat virtualizer.
//
// Maps stable item keys to measured pixel heights so placeholders for
// off-screen items have correct sizes. Persists to localStorage keyed by
// session ID, with debounced writes and LRU-style pruning.
//
// LRU recency tracks ACCESS, not just insertion: both get() and set()
// re-insert the key so recently-scrolled rows survive eviction instead of
// dying by age. The eviction cap is session-size-aware — a long session may
// keep up to min(rowCount, HARD_CEILING) heights so scrolling back to the top
// of a revisited transcript doesn't re-enter all-estimate territory — with a
// hard ceiling so localStorage can't blow up.
//
// averageHeight() exposes the running mean of MEASURED heights (O(1),
// maintained incrementally through set / overwrite / eviction / load) so
// callers can estimate unmeasured rows from real data instead of a flat
// constant.
//
// Falls back to in-memory-only mode when localStorage is unavailable
// (private browsing, quota exceeded, sandboxed iframes, etc.). Corrupted
// JSON triggers a console.warn and a fresh cache for that session.

// localStorage key prefix — a storage identifier, never rendered. Not UI copy.
// Kept in sync with SESSION_PREFIXES in `utils/storageGc.ts`, which garbage-
// collects these keys; changing it orphans every persisted height map.
const LS_KEY_PREFIX = 'vc_heights_'
// Baseline floor for the eviction cap. The effective cap grows with the
// session's row count up to HARD_CEILING (see effectiveCap()).
const MAX_ENTRIES = 2000
// Hard ceiling on retained entries regardless of row count, so a pathological
// session cannot make the persisted blob unbounded.
const HARD_CEILING = 20000
// Debounce for localStorage writes. Streaming fires many set()s per second, so
// a wide window coalesces them into far fewer full-map JSON serializations.
// Manual flush() (e.g. on unmount) still persists immediately, and the dirty
// flag skips no-op flushes.
const FLUSH_DELAY_MS = 750
// Fallback estimate returned by averageHeight() when nothing is measured yet.
// Matches the virtualizer's historical flat estimate.
const DEFAULT_ESTIMATED_HEIGHT = 100
// Absolute ceiling on the estimated height for an unmeasured row. One
// pathological row (a giant widget) must not be able to inflate every
// unmeasured row's estimate without bound. Deliberately generous: measured
// means on real transcripts sit in the hundreds of px, so this clips only the
// pathological case. A tighter bound (or a minimum-sample gate before trusting
// the mean at all) was MEASURED to be worse -- see the comment on averageHeight.
const MAX_MEAN_PX = 4000

type FlushTimer = ReturnType<typeof setTimeout>

/** Returns the localStorage object if accessible, else null. */
function getStorage(): Storage | null {
  try {
    // Touching localStorage can throw in sandboxed iframes / disabled storage.
    if (typeof window === 'undefined') return null
    const ls = window.localStorage
    // Round-trip test catches "quota=0" and other half-broken environments.
    const probe = '__vc_probe__'
    ls.setItem(probe, probe)
    ls.removeItem(probe)
    return ls
  } catch {
    return null
  }
}

export class HeightCache {
  // Map preserves insertion order, which we use as the LRU access order:
  // every `get` / `set` re-inserts the key so the most-recently-touched
  // entries sit at the tail and the oldest get evicted first.
  private readonly cache: Map<string, number> = new Map()
  private readonly sessionId: string
  private readonly storage: Storage | null
  private readonly storageKey: string
  private dirty = false
  private flushTimer: FlushTimer | null = null
  // Running sum of the LIVE measured heights, kept in lockstep with the map
  // through set / overwrite / eviction / load / retire so averageHeight() is
  // O(1). "Live" excludes retired keys (see `retired`), so the mean is
  // measuredSum / (cache.size - retired.size).
  private measuredSum = 0
  // Keys whose ROW HAS LEFT THE LIST but whose measurement is kept. They are
  // excluded from the mean and from the persisted blob, and revived by a later
  // set() -- see retire() for why the entry is kept rather than deleted.
  private readonly retired: Set<string> = new Set()
  // Session row count driving the size-aware eviction cap. 0 means UNKNOWN,
  // never "this session is empty" — see setRowCount() and load().
  private rowCount = 0

  constructor(sessionId: string, options?: { rowCount?: number }) {
    this.sessionId = sessionId
    this.storage = getStorage()
    this.storageKey = `${LS_KEY_PREFIX}${sessionId}`
    // Only a positive count is information. A caller that constructs the cache
    // before its transcript has loaded passes 0, which must not be mistaken for
    // a genuinely tiny session (load() seeds the cap from the blob instead).
    if (options && typeof options.rowCount === 'number' && options.rowCount > 0) {
      this.rowCount = Math.floor(options.rowCount)
    }
    this.load()
  }

  /**
   * Effective eviction cap: max(MAX_ENTRIES, min(rowCount, HARD_CEILING)).
   * Grows with the session so a large transcript keeps its heights, but never
   * exceeds HARD_CEILING so the persisted blob stays bounded.
   */
  private effectiveCap(): number {
    return Math.max(MAX_ENTRIES, Math.min(this.rowCount, HARD_CEILING))
  }

  /**
   * Update the session row count that drives the eviction cap.
   *
   * The count is treated as a HIGH-WATER MARK: it only ever rises. A non-
   * positive count means "not known yet" rather than "the session is empty" —
   * the chat hook sets sessionId when a slot changes, one render before the
   * transcript arrives — and a small POSITIVE count is just as untrustworthy,
   * because a single WS message can land before slot hydration and report
   * `itemCount === 1` for a 5,000-row session.
   *
   * Shrinking on either is unsafe in a way growing is not: the cap reduction
   * evicts measurements IRREVERSIBLY, and the authoritative count arriving a
   * render later raises the cap but cannot bring them back — dropping the
   * session straight into the estimated-offset jumping this cache exists to
   * prevent. Retention stays bounded because HARD_CEILING is independent of the
   * row count, so refusing to shrink cannot grow the persisted blob.
   */
  setRowCount(rowCount: number): void {
    if (!(rowCount > 0)) return
    const next = Math.floor(rowCount)
    if (next <= this.rowCount) return
    this.rowCount = next
    this.evictToCap()
  }

  /**
   * Running mean of MEASURED heights, for estimating unmeasured rows.
   *
   * Bounded by MAX_MEAN_PX so one pathological row (a giant widget) cannot
   * inflate the estimate for thousands of unmeasured rows without limit.
   *
   * It deliberately does NOT gate on a minimum sample count, even though the
   * first measurements all come from the mounted tail and are therefore biased
   * toward tall rows. That gate was implemented and MEASURED in a real browser
   * against an 800-row bimodal transcript, and it was worse: holding the flat
   * configured estimate until the sample grew reintroduced the original
   * under-estimate (the flat guess is ~5x too small, versus the tail-biased
   * mean being ~17% too large), and the peak scrollHeight correction rose from
   * ~51,500px to ~387,000px. A slightly-high adaptive estimate from the first
   * measurement beats a very-low flat one, so only the outlier ceiling is kept.
   */
  averageHeight(fallback: number = DEFAULT_ESTIMATED_HEIGHT): number {
    // Retired entries are still in the map (their own rows can come back) but
    // must not price OTHER rows, so they are out of both halves of the mean.
    const n = this.cache.size - this.retired.size
    if (n <= 0) return fallback
    return Math.min(this.measuredSum / n, MAX_MEAN_PX)
  }

  /**
   * Non-promoting read: returns the cached height WITHOUT touching LRU order.
   *
   * Bulk index synchronization (OffsetIndex.sync, window/offset scans) reads
   * every row's height. Routing those through `get()` would rewrite the LRU
   * order into transcript-index order on every sync, so at capacity the
   * eviction would drop top-of-transcript rows the user had just viewed —
   * defeating the access-based retention this cache exists to provide. Genuine
   * access is recorded by `set()` when a mounted row is measured.
   */
  peek(key: string): number | undefined {
    return this.cache.get(key)
  }

  /** Returns the cached height for `key`, or undefined if not measured. */
  get(key: string): number | undefined {
    const v = this.cache.get(key)
    if (v === undefined) return undefined
    // Re-insert to mark as most-recently-used. Cheap because Map operations
    // are O(1) and we only do this on cache hits.
    this.cache.delete(key)
    this.cache.set(key, v)
    return v
  }

  /** Stores `height` for `key`, evicting the oldest entry if over the cap. */
  set(key: string, height: number): void {
    // Re-insert so the key sits at the tail (most-recent position) regardless
    // of whether it already existed. On overwrite, adjust the running sum by
    // the delta so the mean stays exact.
    const prev = this.cache.get(key)
    if (prev !== undefined) {
      this.cache.delete(key)
      // A retired entry's height is ALREADY out of measuredSum, so subtracting
      // it again would double-count. Measuring the row also revives it: a row
      // being measured is mounted, so it is back in the list.
      if (!this.retired.delete(key)) this.measuredSum -= prev
    }
    this.cache.set(key, height)
    this.measuredSum += height
    this.evictToCap()
    this.dirty = true
    this.scheduleFlush()
  }

  /**
   * Retire the measurement for `key` -- the row it belonged to has LEFT the list.
   *
   * The entry is KEPT and only removed from the mean. That split is the whole
   * point, because the two halves have opposite failure modes:
   *
   *  - Keeping it in the MEAN is wrong. `averageHeight()` prices every
   *    UNMEASURED row, so a measurement is never local to its own row: a
   *    transient streaming placeholder measured at a fraction of a real message
   *    goes on dragging the estimate for rows that are still on screen, for the
   *    rest of the session and across reloads once the blob is persisted.
   *  - DELETING it is also wrong, because a row leaving the list is not always
   *    permanent. An optimistically truncated transcript is restored wholesale
   *    when the server refuses the press (regenerate and edit-resend both
   *    snapshot, truncate, then replace the snapshot back), and rows that came
   *    back without their measurements would be re-priced from the mean --
   *    wrong spacers and a viewport jump, which is the very failure this cache
   *    exists to prevent. Keeping the entry makes that restore exact.
   *
   * A later `set()` revives the key: a row being measured is mounted, so it is
   * live again. Retired entries are evicted BEFORE any live measurement (see
   * evictToCap), so they cannot accumulate and cannot cost a live row its
   * height.
   *
   * Callers must pass only keys that left the DATA, never keys that merely
   * UNMOUNTED -- remembering a row that scrolled out of the window is what this
   * cache is for.
   */
  /** Move a measurement to a new key: the ROW survived a commit but its
   *  display key changed (a landing regroup absorbs a page boundary and the
   *  turn's lead item -- its key currency -- changes). Retiring the old key
   *  would send a mounted giant row back to estimate pricing, and the
   *  re-measure plus next landing's re-retire cycle showed up on the bottom
   *  rig as a per-landing multi-thousand-px breathe. The value moves as-is;
   *  mean bookkeeping is unchanged (same measurement, new name). No-op when
   *  the old key holds nothing or the new key already has a measurement. */
  rename(oldKey: string, newKey: string): void {
    if (oldKey === newKey) return
    const v = this.cache.get(oldKey)
    if (v === undefined) return
    if (this.cache.get(newKey) !== undefined) return
    this.cache.delete(oldKey)
    // A renamed row is LIVE (it survived the commit under a new key), so
    // the entry lands un-retired. A retired source's height is already out
    // of measuredSum and must be added back, or a later set(newKey) would
    // subtract a value that was never counted.
    if (this.retired.delete(oldKey)) this.measuredSum += v
    this.cache.set(newKey, v)
    this.dirty = true
    this.scheduleFlush()
  }

  retire(key: string): void {
    if (!this.cache.has(key) || this.retired.has(key)) return
    this.retired.add(key)
    this.measuredSum -= this.cache.get(key) as number
    this.dirty = true
    this.scheduleFlush()
  }

  /**
   * Un-retire `key` because its row is in the list again, so its measurement
   * counts toward the mean once more.
   *
   * The caller is the resolved-height read, not a removal site: a restore is not
   * the mirror image of a removal. An optimistic TAIL truncation comes back as a
   * plain append, which is not a splice at all, so there is no commit shape to
   * hook a revive onto -- while a key resolving from a LIVE row index is itself
   * proof the retirement's premise no longer holds. Idempotent and O(1); the
   * mean it feeds converges on the next sync.
   */
  reviveIfRetired(key: string): void {
    if (!this.retired.delete(key)) return
    const h = this.cache.get(key)
    // Cannot be missing (eviction drops the retired mark with the entry), but a
    // silent no-op is the right answer if it ever is: adding an unknown height
    // to the sum would corrupt every later mean.
    if (h === undefined) return
    this.measuredSum += h
    this.dirty = true
    this.scheduleFlush()
  }

  /**
   * Evict oldest-first (insertion/access order) until size <= effectiveCap(),
   * keeping measuredSum in lockstep. Shared by set(), setRowCount() and load().
   *
   * An eviction changes what SHOULD be persisted, so it marks the cache dirty
   * and schedules a flush. Without that, a trim driven by setRowCount() (or by
   * load() hitting HARD_CEILING) stayed in memory only: the oversized blob
   * survived in localStorage and was re-read and re-trimmed on every single
   * open, so the wasted quota was never reclaimed.
   */
  private evictToCap(): void {
    const cap = this.effectiveCap()
    let evicted = 0
    while (this.cache.size > cap) {
      // RETIRED FIRST, ahead of LRU order. A retired row has left the list and
      // its entry exists only to serve a possible rollback, so it is strictly
      // less valuable than any live measurement. LRU order alone gets this
      // backwards: a transient row is measured immediately before it leaves, so
      // it sits at the most-recently-used end and would be the LAST evicted,
      // costing an older LIVE row its measurement while a dead one survives.
      // The set is insertion-ordered, so this drops the longest-retired first.
      const retiredKey = this.retired.values().next().value
      if (retiredKey !== undefined) {
        // Removed from `retired` first, so the loop makes progress even in the
        // impossible case where the entry is already gone from the map.
        this.retired.delete(retiredKey)
        // Already out of measuredSum -- retire() subtracted it.
        if (this.cache.delete(retiredKey)) evicted++
        continue
      }
      // Map iteration is in insertion order, so the first key is the oldest.
      const oldestEntry = this.cache.entries().next().value
      if (oldestEntry === undefined) break
      const [oldestKey, oldestVal] = oldestEntry
      this.cache.delete(oldestKey)
      this.measuredSum -= oldestVal
      evicted++
    }
    if (evicted > 0) {
      this.dirty = true
      this.scheduleFlush()
    }
  }

  /** Writes pending changes to localStorage immediately and clears the timer. */
  flush(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (!this.dirty) return
    this.dirty = false
    if (!this.storage) return
    try {
      // Use Object.create(null) so keys like "__proto__" or "constructor"
      // are stored as own properties instead of mutating the prototype.
      // (A naive `{}` literal swallows __proto__ on assignment.)
      const obj: Record<string, number> = Object.create(null)
      // Retired keys are deliberately NOT persisted: the row is gone from the
      // transcript, so after a reload its height would be back in the mean
      // pricing rows that are still there. The in-memory entry is what serves a
      // same-session rollback; a reload has no snapshot to roll back to.
      for (const [k, v] of this.cache) {
        if (this.retired.has(k)) continue
        obj[k] = v
      }
      this.storage.setItem(this.storageKey, JSON.stringify(obj))
    } catch {
      // Quota exceeded or transient failure — drop this flush. A future set()
      // will dirty the cache again and we'll retry on the next debounce window.
      this.dirty = true
    }
  }

  /** Clears both in-memory and persisted state for this session. */
  clear(): void {
    this.cache.clear()
    this.retired.clear()
    this.measuredSum = 0
    this.dirty = false
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (!this.storage) return
    try {
      this.storage.removeItem(this.storageKey)
    } catch {
      // Best-effort — swallow.
    }
  }

  /**
   * True when any measurement is retired.
   *
   * The O(1) guard a caller uses to skip a live-row scan entirely on the
   * overwhelmingly common commit, where nothing is retired at all.
   */
  hasRetired(): boolean {
    return this.retired.size > 0
  }

  /** Number of entries currently in the cache. Visible for tests/debug. */
  size(): number {
    return this.cache.size
  }

  private load(): void {
    if (!this.storage) return
    let raw: string | null
    try {
      raw = this.storage.getItem(this.storageKey)
    } catch {
      return
    }
    if (raw === null) return
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
    } catch {
      // Corrupted blob — log once, reset, and continue. We deliberately
      // wipe persisted state here so a bad write can't keep poisoning
      // future loads.
      // eslint-disable-next-line no-console
      console.warn(`[HeightCache] corrupted localStorage for session ${this.sessionId}; resetting`)
      try { this.storage.removeItem(this.storageKey) } catch { /* ignore */ }
      return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return
    // Preserve insertion order from the stored object (which preserved LRU
    // order at last flush). Skip non-numeric/non-finite values defensively.
    // Use Object.keys instead of Object.entries so own-property keys like
    // "__proto__" are visible (Object.entries skips them when they were
    // serialized into the prototype slot by JSON.parse).
    //
    // ZERO is poison, not a measurement, and it is dropped here (v > 0, not
    // >= 0). Blobs written before the RO write path gained its h > 0 floor
    // carry zeros from rows measured while an ancestor was display:none, and a
    // loaded zero DEFEATS the self-heal that floor relies on: the offset tree
    // prices the poisoned region at 0px, the window math finds nothing there to
    // mount, and a row that never mounts is never re-measured. Observed in the
    // field as a session opening to a full-viewport blank — 7 items, 0 mounted
    // rows, both spacers 0px — persisting across reloads. Dropping the entry
    // instead makes the row unmeasured, which is the state the estimate path
    // and the write-side floor already handle.
    for (const k of Object.keys(parsed as Record<string, unknown>)) {
      const v = (parsed as Record<string, unknown>)[k]
      if (typeof v === 'number' && Number.isFinite(v) && v > 0) {
        this.cache.set(k, v)
        this.measuredSum += v
      }
    }
    // Enforce the cap on load too. set() trims one-at-a-time, but a blob from an
    // older build, a hand-edited entry, or a long-lived session can persist
    // more than the cap allows — without this the cache would start a session
    // already over the cap. Trim oldest-first (insertion order) to match set(),
    // keeping measuredSum in lockstep.
    //
    // When the row count is not known yet, seed it from what we just read. The
    // trim below is IRREVERSIBLE — a later setRowCount() raises the cap but
    // cannot bring evicted measurements back — so trimming a long session's
    // blob to the baseline floor merely because the transcript had not loaded
    // when the cache was constructed would put a revisited long session right
    // back into all-estimate territory, which is the bug this cap exists to
    // prevent. HARD_CEILING still applies, so the blob stays bounded.
    if (this.rowCount === 0) this.rowCount = this.cache.size
    this.evictToCap()
  }

  private scheduleFlush(): void {
    if (this.flushTimer !== null) return
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null
      this.flush()
    }, FLUSH_DELAY_MS)
  }
}
