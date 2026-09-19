/**
 * Framework-free per-member projection store.
 *
 * Holds the latest projected value per (slug, key), fed by two WebSocket
 * frames and a roster baseline. Three invariants keep it correct against
 * replays, races, and server restarts:
 *
 *   - higher-seq-wins: apply() drops any frame whose seq <= the held row's
 *     seq, so replays and out-of-order stale frames are no-ops.
 *   - seed never truncates: the roster baseline only ever applies values; a
 *     live frame that raced ahead of the baseline keeps winning.
 *   - truncate only from the subscribed frame: rows with seq > lastSeq are
 *     dropped ONLY when the server tells us (members_subscribed), which is the
 *     one moment we learn a torn tail was rolled back after a restart.
 *
 * faceOf() exposes a useSyncExternalStore-shaped view per (slug, key) whose
 * snapshot is referentially stable until that row actually changes.
 */

/** One held projection: the value, the seq it arrived at, and how to render it. */
interface Row {
  value: unknown
  seq: number
}

/** The useSyncExternalStore-shaped view for a single (slug, key). */
export interface ProjectionFace {
  subscribe(listener: () => void): () => void
  getSnapshot(): unknown | undefined
}

export class MemberProjectionStore {
  private readonly rows = new Map<string, Map<string, Row>>()
  private readonly listeners = new Map<string, Set<() => void>>()
  private static faceKey(slug: string, key: string): string {
    return slug + '\u0000' + key
  }

  private notify(slug: string, key: string): void {
    const set = this.listeners.get(MemberProjectionStore.faceKey(slug, key))
    if (!set) return
    for (const fn of set) fn()
  }

  /**
   * Apply one projected value. Higher-seq-wins: if a row exists and the incoming
   * seq is not strictly greater, do nothing, so an equal-seq replay and a stale
   * frame both drop. Otherwise store it and notify the (slug, key) face.
   */
  apply(slug: string, key: string, value: unknown, seq: number): void {
    let byKey = this.rows.get(slug)
    const existing = byKey?.get(key)
    if (existing && seq <= existing.seq) return
    if (!byKey) {
      byKey = new Map<string, Row>()
      this.rows.set(slug, byKey)
    }
    byKey.set(key, { value, seq })
    this.notify(slug, key)
  }

  /**
   * Seed a slug's baseline from the roster block. Each key is applied at asOfSeq
   * through apply(), so a live frame that already advanced the row past asOfSeq
   * keeps winning. Never truncates.
   */
  seed(slug: string, values: { [key: string]: unknown }, asOfSeq: number): void {
    for (const key of Object.keys(values)) {
      this.apply(slug, key, values[key], asOfSeq)
    }
  }

  /**
   * Drop this slug's rows whose seq > lastSeq and notify them. Called ONLY
   * from the members_subscribed frame: the server may have truncated a torn
   * tail after a restart, and this is where the client learns of it.
   */
  truncate(slug: string, lastSeq: number): void {
    const byKey = this.rows.get(slug)
    if (!byKey) return
    for (const [key, row] of byKey) {
      if (row.seq > lastSeq) {
        byKey.delete(key)
        this.notify(slug, key)
      }
    }
    if (byKey.size === 0) this.rows.delete(slug)
  }

  /**
   * Apply truncate per slug from a members_subscribed frame. Slugs absent
   * from lastSeqs are left untouched.
   */
  truncateAll(lastSeqs: { [slug: string]: number }): void {
    for (const slug of Object.keys(lastSeqs)) {
      this.truncate(slug, lastSeqs[slug])
    }
  }

  /**
   * A useSyncExternalStore-shaped view of one (slug, key). getSnapshot returns
   * the SAME Row.value reference until the row changes, which
   * useSyncExternalStore requires to avoid an infinite render loop.
   */
  faceOf(slug: string, key: string): ProjectionFace {
    const faceKey = MemberProjectionStore.faceKey(slug, key)
    return {
      subscribe: (listener: () => void): (() => void) => {
        let set = this.listeners.get(faceKey)
        if (!set) {
          set = new Set<() => void>()
          this.listeners.set(faceKey, set)
        }
        set.add(listener)
        return () => {
          const s = this.listeners.get(faceKey)
          if (!s) return
          s.delete(listener)
          if (s.size === 0) this.listeners.delete(faceKey)
        }
      },
      // Reads the live row each call; the stored value reference only changes
      // when apply() replaces the Row, so identity is stable between changes.
      getSnapshot: (): unknown | undefined => this.rows.get(slug)?.get(key)?.value,
    }
  }

  /** Read one held value (test/consumer helper). */
  get(slug: string, key: string): unknown | undefined {
    return this.rows.get(slug)?.get(key)?.value
  }

  /** Whether any row is held for this slug. */
  has(slug: string): boolean {
    return this.rows.has(slug)
  }

  /** Drop all rows and listeners (tests). */
  clear(): void {
    this.rows.clear()
    this.listeners.clear()
  }
}

/** Process-wide singleton the WebSocket layer feeds and hooks read. */
export const memberProjectionStore = new MemberProjectionStore()
