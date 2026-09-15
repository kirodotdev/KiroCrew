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
import type { ContributedView, ProjectionSchema } from './memberProjectionTypes'

/** One held projection: the value, the seq it arrived at, and how to render it. */
interface Row {
  value: unknown
  seq: number
  /** Rendering declared by a contributor for an `<app>/<key>` view, if any. */
  schema?: ProjectionSchema
}

/** The useSyncExternalStore-shaped view for a single (slug, key). */
export interface ProjectionFace {
  subscribe(listener: () => void): () => void
  getSnapshot(): unknown | undefined
}

export class MemberProjectionStore {
  private readonly rows = new Map<string, Map<string, Row>>()
  private readonly listeners = new Map<string, Set<() => void>>()
  /** Bumped whenever the SET of keys held for a slug changes, so a consumer
   *  listing contributed views re-renders on a new card rather than only on a
   *  value change to a card it already knows about. */
  private readonly keysetListeners = new Map<string, Set<() => void>>()
  private readonly keysetVersions = new Map<string, number>()

  private static faceKey(slug: string, key: string): string {
    return slug + '\u0000' + key
  }

  private notify(slug: string, key: string): void {
    const set = this.listeners.get(MemberProjectionStore.faceKey(slug, key))
    if (!set) return
    for (const fn of set) fn()
  }

  private notifyKeyset(slug: string): void {
    this.keysetVersions.set(slug, (this.keysetVersions.get(slug) ?? 0) + 1)
    const set = this.keysetListeners.get(slug)
    if (!set) return
    for (const fn of set) fn()
  }

  /**
   * Apply one projected value. Higher-seq-wins: if a row exists and the
   * incoming seq is not strictly greater, do nothing (equal-seq replays and
   * stale frames drop). Otherwise store it and notify the (slug, key) face.
   *
   * `schema` is optional and STICKY: a contributor publishes it once per key
   * (contribution protocol §7), and later value pushes carry no schema, so an
   * absent one keeps the rendering the key already has instead of dropping the
   * card back to the untyped fallback on the next fold.
   */
  apply(slug: string, key: string, value: unknown, seq: number, schema?: ProjectionSchema): void {
    let byKey = this.rows.get(slug)
    const existing = byKey?.get(key)
    if (existing && seq <= existing.seq) return
    if (!byKey) {
      byKey = new Map<string, Row>()
      this.rows.set(slug, byKey)
    }
    const isNewKey = !existing
    byKey.set(key, { value, seq, schema: schema ?? existing?.schema })
    this.notify(slug, key)
    if (isNewKey) this.notifyKeyset(slug)
  }

  /**
   * Seed a slug's baseline from the roster block. Each key is applied at
   * asOfSeq through apply(), so a live frame that already advanced the row
   * past asOfSeq keeps winning. Never truncates.
   *
   * `seqs` overrides asOfSeq PER KEY, which contributed rows need: such a row's
   * seq is the contributor's own fold position, not this response's asOfSeq.
   * Seeding one at asOfSeq (usually higher) would make higher-seq-wins drop the
   * contributor's next live push and freeze the card at its baseline.
   */
  seed(
    slug: string,
    values: { [key: string]: unknown },
    asOfSeq: number,
    seqs?: { [key: string]: number },
    schemas?: { [key: string]: ProjectionSchema },
  ): void {
    for (const key of Object.keys(values)) {
      const seq = seqs && typeof seqs[key] === 'number' ? seqs[key] : asOfSeq
      this.apply(slug, key, values[key], seq, schemas?.[key])
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
    let dropped = false
    for (const [key, row] of byKey) {
      if (row.seq > lastSeq) {
        byKey.delete(key)
        this.notify(slug, key)
        dropped = true
      }
    }
    if (byKey.size === 0) this.rows.delete(slug)
    if (dropped) this.notifyKeyset(slug)
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

  /** The rendering a contributor declared for one key, if any. */
  schemaOf(slug: string, key: string): ProjectionSchema | undefined {
    return this.rows.get(slug)?.get(key)?.schema
  }

  /**
   * Every CONTRIBUTED view held for a slug, sorted by key.
   *
   * A contributed key is namespaced `<app>/<key>` (contribution protocol §2),
   * and the four built-in keys are bare words, so the presence of a `/` is the
   * whole test -- no list of built-ins to keep in sync with the backend, and a
   * fifth built-in key does not accidentally render as somebody's app card.
   *
   * Rows whose value is null are omitted: that is the teardown frame saying the
   * app is gone (§6), and a card reading "null" is worse than no card.
   */
  contributedViews(slug: string): ContributedView[] {
    const byKey = this.rows.get(slug)
    if (!byKey) return []
    const out: ContributedView[] = []
    for (const [key, row] of byKey) {
      if (!key.includes('/')) continue
      if (row.value === null || row.value === undefined) continue
      out.push({ key, value: row.value, seq: row.seq, schema: row.schema })
    }
    out.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
    return out
  }

  /**
   * A useSyncExternalStore-shaped view of a slug's contributed KEY SET.
   * getSnapshot returns a version number (O(1), referentially stable), so a
   * consumer rebuilds its list in a memo keyed on it rather than on every
   * render.
   */
  contributedFace(slug: string): ProjectionFace {
    return {
      subscribe: (listener: () => void): (() => void) => {
        let set = this.keysetListeners.get(slug)
        if (!set) {
          set = new Set<() => void>()
          this.keysetListeners.set(slug, set)
        }
        set.add(listener)
        return () => {
          const s = this.keysetListeners.get(slug)
          if (!s) return
          s.delete(listener)
          if (s.size === 0) this.keysetListeners.delete(slug)
        }
      },
      getSnapshot: (): unknown => this.keysetVersions.get(slug) ?? 0,
    }
  }

  /** Whether any row is held for this slug. */
  has(slug: string): boolean {
    return this.rows.has(slug)
  }

  /** Drop all rows and listeners (tests). */
  clear(): void {
    this.rows.clear()
    this.listeners.clear()
    this.keysetListeners.clear()
    this.keysetVersions.clear()
  }
}

/** Process-wide singleton the WebSocket layer feeds and hooks read. */
export const memberProjectionStore = new MemberProjectionStore()
