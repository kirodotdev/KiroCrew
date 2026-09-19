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
  /**
   * The contributor's own fold GENERATION for a contributed row, mirroring the
   * server's `stateVersion`. Held because the two sides do not apply the same
   * rule: the server accepts a publish whose stateVersion rose even when its seq
   * did not advance (a contributor refolding from scratch), so seq alone would
   * drop a frame the server already committed.
   */
  stateVersion: number
  /**
   * Monotonic write id, assigned by the store. Only `reconcileContributed` reads
   * it, to tell a row that predates a baseline read from one a live frame wrote
   * while that read was in flight.
   */
  writeId: number
}

/** The useSyncExternalStore-shaped view for a single (slug, key). */
export interface ProjectionFace {
  subscribe(listener: () => void): () => void
  getSnapshot(): unknown | undefined
}

export class MemberProjectionStore {
  private readonly rows = new Map<string, Map<string, Row>>()
  private readonly listeners = new Map<string, Set<() => void>>()
  /** Monotonic, store-wide. See `Row.writeId`. */
  private writeCounter = 0
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
   *
   * A null/undefined value on a CONTRIBUTED key is the §6 teardown: the row is
   * REMOVED from the map (not stored as a null the list filters out), so a
   * later re-enable at the contributor's own — possibly lower — seq is applied
   * as a fresh key rather than dropped by higher-seq-wins against a lingering
   * teardown seq.
   *
   * The contributed KEY SET is notified whenever a contributed key changes at
   * all — appears, updates value, or is torn down — because a list consumer
   * (`useMemberContributedViews`) recomputes only on the keyset version, so a
   * value update or a removal that only fired the per-key face would leave the
   * rendered card list stale.
   */
  apply(
    slug: string,
    key: string,
    value: unknown,
    seq: number,
    schema?: ProjectionSchema,
    stateVersion = 0,
  ): void {
    let byKey = this.rows.get(slug)
    const existing = byKey?.get(key)
    // stateVersion is asked FIRST, because it can legitimately carry a LOWER
    // seq: a contributor that refolds from scratch bumps the version and starts
    // its seq again, and the server accepts that (contrib.py
    // `ExternalProjectionStore.publish`, `by_state_version`). Seq-wins alone
    // dropped it and left the obsolete card on screen. A version that went
    // BACKWARDS is stale by the same rule the server refuses it with.
    if (existing) {
      if (stateVersion > existing.stateVersion) {
        // accept, whatever the seq says
      } else if (stateVersion < existing.stateVersion) {
        return
      } else if (seq <= existing.seq) {
        return
      }
    }

    const contributed = key.includes('/')
    const isTeardown = contributed && (value === null || value === undefined)

    if (isTeardown) {
      // Remove the row rather than storing null: a stored teardown would sit at
      // this (max) seq and block a re-enable that legitimately folds at a lower
      // seq. Nothing to do if the key was never held.
      if (!existing) return
      byKey?.delete(key)
      if (byKey && byKey.size === 0) this.rows.delete(slug)
      this.notify(slug, key)
      this.notifyKeyset(slug)
      return
    }

    if (!byKey) {
      byKey = new Map<string, Row>()
      this.rows.set(slug, byKey)
    }
    byKey.set(key, {
      value,
      seq,
      schema: schema ?? existing?.schema,
      stateVersion,
      writeId: ++this.writeCounter,
    })
    this.notify(slug, key)
    // A NEW built-in key changes the key set the same way it always did; a
    // contributed key notifies the contributed face on every change (new OR an
    // update to a card already shown), so the card list re-renders on a value
    // push, not only when a card first appears.
    if (!existing || contributed) this.notifyKeyset(slug)
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
    stateVersions?: { [key: string]: number },
  ): void {
    for (const key of Object.keys(values)) {
      const seq = seqs && typeof seqs[key] === 'number' ? seqs[key] : asOfSeq
      const version =
        stateVersions && typeof stateVersions[key] === 'number' ? stateVersions[key] : 0
      this.apply(slug, key, values[key], seq, schemas?.[key], version)
    }
  }

  /** The write id to hand `reconcileContributed` after a baseline read. */
  mark(): number {
    return this.writeCounter
  }

  /**
   * Delete this slug's contributed rows that the authoritative baseline does not
   * carry, so a card the server has already forgotten cannot outlive it.
   *
   * `truncate` deliberately leaves contributed keys alone (their seq is a
   * different domain from the member log's), which left teardown resting on the
   * one null-value frame §6 sends. A socket that drops at the wrong moment never
   * delivers it, and the card then stayed on screen indefinitely — the baseline
   * read is the only thing that can say the row is gone.
   *
   * `since` is a `mark()` taken BEFORE the baseline request went out: a row a
   * live frame wrote while that request was in flight is newer than the answer,
   * so it is kept rather than deleted for being absent from a stale snapshot.
   */
  reconcileContributed(slug: string, baselineKeys: Iterable<string>, since: number): void {
    const byKey = this.rows.get(slug)
    if (!byKey) return
    const keep = new Set(baselineKeys)
    let dropped = false
    for (const [key, row] of byKey) {
      if (!key.includes('/')) continue
      if (keep.has(key)) continue
      if (row.writeId > since) continue
      byKey.delete(key)
      this.notify(slug, key)
      dropped = true
    }
    if (byKey.size === 0) this.rows.delete(slug)
    if (dropped) this.notifyKeyset(slug)
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
      // `lastSeq` is the member LOG's asOfSeq domain. A contributed key (`/` in
      // the name) carries the contributor's OWN fold-position seq, a different
      // domain, so it is not comparable to lastSeq -- truncating it here would
      // drop a live contributed card on every reconnect whenever its own seq
      // happened to exceed the member log's. Only built-in keys are bounded by
      // the member-log baseline; a contributed key's teardown arrives as its
      // own null-value frame instead.
      if (key.includes('/')) continue
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
