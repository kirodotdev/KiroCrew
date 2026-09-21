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

/** Teardown tombstones retained at once, across every slug. Sized for many
 *  more concurrent teardowns than a roster response's flight time can overlap,
 *  while still bounding a tab that churns contributed keys for days. */
const MAX_TEARDOWN_TOMBSTONES = 1024

export class MemberProjectionStore {
  private readonly rows = new Map<string, Map<string, Row>>()
  private readonly listeners = new Map<string, Set<() => void>>()
  /** Bumped whenever the SET of keys held for a slug changes, so a consumer
   *  listing contributed views re-renders on a new card rather than only on a
   *  value change to a card it already knows about. */
  private readonly keysetListeners = new Map<string, Set<() => void>>()
  private readonly keysetVersions = new Map<string, number>()
  /** Teardown seq per (slug, key), consulted ONLY by a baseline seed.
   *
   *  A teardown removes the row and leaves nothing behind, so a roster response
   *  already in flight when it arrived re-seeds the key and the card comes back
   *  for good. Remembering the seq it died at lets a seed recognise itself as
   *  older than the teardown and decline, WITHOUT becoming a row: a row here
   *  would sit at the teardown's (maximum) seq and block the legitimate
   *  re-enable that folds at the contributor's own lower seq, which is the very
   *  thing the teardown branch removes the row to avoid.
   *
   *  A live publish supersedes a tombstone whatever its seq, because a live
   *  frame is current by construction; only a BASELINE is suspect. */
  private readonly tombstones = new Map<string, number>()
  /** Bumped whenever a teardown guard is evicted.
   *
   *  Eviction alone reopens the hole the tombstones close: enough teardowns at
   *  once (one app with 64 projections across 17 members is 1,088 frames) push
   *  the earliest guards out while the baseline they guard against is still in
   *  flight. What identifies an at-risk baseline is WHEN it was assembled, not
   *  what seq it carries -- a contributed key's seq is the contributor's own
   *  fold position, a different domain per key, so one key's number says nothing
   *  about another's and a shared numeric threshold would suppress cards that
   *  were never torn down. A baseline older than this counter is refused; every
   *  other baseline, and every live push, is unaffected. */
  private baselineGeneration = 0

  private static faceKey(slug: string, key: string): string {
    return slug + '\u0000' + key
  }

  /** The generation a baseline read starting NOW belongs to.
   *
   *  A caller reads this before issuing its roster request and hands it back to
   *  :meth:`seed`, which is what lets the store tell a baseline assembled before
   *  a guard was lost from one assembled after.
   */
  currentBaselineGeneration(): number {
    return this.baselineGeneration
  }

  /** Record a teardown seq, bounded. Oldest-first eviction: a Map keeps
   *  insertion order, and the useful life of a tombstone is the flight time of
   *  one roster response, so the oldest is the one least likely to still matter.
   *  Unbounded would be a leak in a long-lived tab that churns contributed
   *  keys. */
  private rememberTeardown(face: string, seq: number): void {
    this.tombstones.delete(face)
    this.tombstones.set(face, seq)
    while (this.tombstones.size > MAX_TEARDOWN_TOMBSTONES) {
      const oldest = this.tombstones.keys().next()
      if (oldest.done) break
      // A guard is about to be forgotten, so every baseline assembled before now
      // is suspect. Counting that here is what lets the refusal below name the
      // at-risk baselines exactly, instead of every baseline under a number.
      this.baselineGeneration += 1
      this.tombstones.delete(oldest.value)
    }
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
    opts?: { baseline?: boolean },
  ): void {
    let byKey = this.rows.get(slug)
    const existing = byKey?.get(key)
    const contributed = key.includes('/')
    if (existing && seq <= existing.seq) {
      // The schema arrives on its own frame, at the SAME seq as the value it
      // describes, so higher-seq-wins alone drops it and the card renders
      // fallback content until a reload. Merge the schema, keep the value: the
      // frame is not newer, so it has nothing to say about the value.
      if (seq === existing.seq && schema !== undefined && existing.schema === undefined) {
        byKey?.set(key, { value: existing.value, seq: existing.seq, schema })
        this.notify(slug, key)
        if (contributed) this.notifyKeyset(slug)
      }
      return
    }
    const face = MemberProjectionStore.faceKey(slug, key)
    const isTeardown = contributed && (value === null || value === undefined)

    if (isTeardown) {
      // Remember the death seq first, and for a key that was never held as much
      // as one that was: either way a roster response still in flight can seed
      // the key straight back, and the unheld case leaves no row for
      // higher-seq-wins to defend. The tombstone is not a row, so it does not
      // block a lower-seq re-enable -- see the field's own note.
      this.rememberTeardown(face, seq)
      if (!existing) return
      byKey?.delete(key)
      if (byKey && byKey.size === 0) this.rows.delete(slug)
      this.notify(slug, key)
      this.notifyKeyset(slug)
      return
    }

    if (opts?.baseline) {
      const died = this.tombstones.get(face)
      if (died !== undefined && died >= seq) {
        // This baseline was assembled before the teardown and would resurrect a
        // card the user has already seen go. Declining leaves the key absent,
        // which is what the teardown established; a later live push still wins.
        return
      }
    }

    if (!byKey) {
      byKey = new Map<string, Row>()
      this.rows.set(slug, byKey)
    }
    byKey.set(key, { value, seq, schema: schema ?? existing?.schema })
    this.tombstones.delete(face)
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
    generation?: number,
  ): void {
    if (generation !== undefined && generation < this.baselineGeneration) {
      // Guards were evicted while this response was in flight, so it cannot be
      // shown not to carry a card that has since been torn down. Dropping the
      // whole baseline is right where dropping suspect keys is not: the caller
      // refetches, and the next response is assembled in the current
      // generation.
      return
    }
    for (const key of Object.keys(values)) {
      const seq = seqs && typeof seqs[key] === 'number' ? seqs[key] : asOfSeq
      this.apply(slug, key, values[key], seq, schemas?.[key], { baseline: true })
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
   * Drop contributed keys this slug holds that the AUTHORITATIVE set does not.
   *
   * A contributed row is torn down by its own `value: null` frame, which a
   * disconnected browser never receives: an app disabled while the socket was
   * down left its card on screen for good, because the reconnect baseline
   * (`members_subscribed` → truncate) deliberately skips contributed keys — their
   * seq is the contributor's own fold position, not the member log's, so it is
   * not comparable to `lastSeq`.
   *
   * The roster read IS comparable: the server lists exactly the contributed rows
   * it still holds, so a key missing from it is a key the gateway no longer
   * serves. Reconciling on that is what makes a missed teardown recoverable by a
   * reload or a reconnect refetch. Built-in keys are untouched — they are bounded
   * by the member-log baseline instead.
   */
  reconcileContributed(slug: string, keys: Iterable<string>): void {
    const byKey = this.rows.get(slug)
    if (!byKey) return
    const keep = new Set(keys)
    let dropped = false
    for (const key of Array.from(byKey.keys())) {
      if (!key.includes('/')) continue
      if (keep.has(key)) continue
      byKey.delete(key)
      this.notify(slug, key)
      dropped = true
    }
    if (byKey.size === 0) this.rows.delete(slug)
    if (dropped) this.notifyKeyset(slug)
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
