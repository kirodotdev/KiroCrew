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

const LS_KEY_PREFIX = 'vc_heights_'
// Baseline floor for the eviction cap. The effective cap grows with the
// session's row count up to HARD_CEILING (see effectiveCap()).
const MAX_ENTRIES = 2000
// Hard ceiling on retained entries regardless of row count, so a pathological
// session cannot make the persisted blob unbounded.
const HARD_CEILING = 20000
// Debounce for localStorage writes. Lengthened from the original 100ms because
// streaming fires many set()s per second; a longer window coalesces them into
// far fewer full-map JSON serializations. Manual flush() (e.g. on unmount)
// still persists immediately, and the dirty flag skips no-op flushes.
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
  // Running sum of MEASURED heights (every cache entry is a measurement), kept
  // in lockstep with the map through set / overwrite / eviction / load so
  // averageHeight() is O(1). The mean is sum / cache.size.
  private measuredSum = 0
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
    const n = this.cache.size
    if (n === 0) return fallback
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
      this.measuredSum -= prev
    }
    this.cache.set(key, height)
    this.measuredSum += height
    this.evictToCap()
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
      for (const [k, v] of this.cache) obj[k] = v
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
    for (const k of Object.keys(parsed as Record<string, unknown>)) {
      const v = (parsed as Record<string, unknown>)[k]
      if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
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
