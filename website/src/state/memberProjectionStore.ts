/**
 * Framework-free per-member projection store.
 *
 * Holds the latest projected value per (slug, key), fed by two WebSocket
 * frames and a roster baseline. Four invariants keep it correct against
 * replays, races, and server restarts:
 *
 *   - higher-seq-wins: apply() drops any frame whose seq <= the held row's
 *     seq, so replays and out-of-order stale frames are no-ops.
 *   - an ATTRIBUTABLE baseline never truncates: while the roster stands behind
 *     a slug's sequence, seed() only applies values, so a live frame that raced
 *     ahead of the baseline keeps winning.
 *   - an UNATTRIBUTABLE baseline (asOfSeq < 0) clears the slug and refuses its
 *     frames. That sentinel is not a sequence, so nothing can be compared
 *     against it, and the state it would otherwise leave on screen belongs to
 *     whichever member last held the slug. See seed().
 *   - truncate only from the subscribed frame: rows with seq > lastSeq are
 *     dropped ONLY when the server tells us (members_subscribed), which is the
 *     one moment we learn a torn tail was rolled back after a restart.
 *
 * faceOf() exposes a useSyncExternalStore-shaped view per (slug, key) whose
 * snapshot is referentially stable until that row actually changes.
 */
import type { ContributedView, ProjectionSchema } from './memberProjectionTypes'

/**
 * How many withdrawn contributed keys are remembered per slug.
 *
 * A withdrawal record only has to outlive the roster reads that were already in
 * flight when it landed — one round trip. The cap keeps a member that publishes
 * and tears down many distinct keys from growing the record without bound; the
 * oldest entry is evicted first, because a newer withdrawal is the one an
 * in-flight read can still contradict.
 *
 * It is sized against the SERVER's bound, not picked for comfort, and the bound
 * that matters is the one on a UNIT rather than on one app. `MAX_PROJECTION_KEYS_PER_UNIT`
 * (200) is charged per app, so a unit legitimately carries several contributors'
 * rows up to `MAX_PROJECTION_KEYS_PER_FILE` (2000) — and a teardown withdraws
 * every key its app published. A cap set to one app's share therefore evicts the
 * oldest markers as soon as a second contributor tears down beside the first,
 * which is an ordinary multi-app uninstall and is exactly the event the record
 * exists to survive; a roster read already in flight then restores those cards.
 * Keep this at or above the server's per-unit FILE limit; anything scoped to a
 * single app's share re-opens that hole.
 */
const MAX_WITHDRAWN_KEYS_PER_SLUG = 2_000

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
  /**
   * Slugs the server has declared UNATTRIBUTABLE, and whose frames are therefore
   * refused until it says otherwise. See {@link seed} for why a slug lands here.
   */
  private readonly unattributable = new Set<string>()
  /**
   * Contributed keys a teardown frame WITHDREW, and the write id it was
   * withdrawn at: slug -> key -> writeId.
   *
   * A teardown removes the row outright (see {@link apply}), which leaves
   * higher-seq-wins nothing to compare a later baseline against — so a roster
   * answer that was already in flight when the withdrawal landed would seed the
   * card straight back onto the screen. The write id is what tells the two
   * apart: a baseline whose `mark()` predates the withdrawal is older than it
   * and may not recreate the key.
   *
   * An entry is dropped the moment it can no longer be right: when a live frame
   * writes the key again (that frame is newer than the withdrawal by
   * construction), when a baseline taken AFTER the withdrawal still carries the
   * key, and when the roster refuses to attribute the slug at all.
   */
  private readonly withdrawn = new Map<string, Map<string, number>>()
  /** Monotonic, store-wide. Stamps both a write (`Row.writeId`) and a
   *  withdrawal (see {@link withdrawn}), so the two are comparable. */
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
   *
   * A frame for a slug the roster declared unattributable is DROPPED. The roster
   * is the only surface that checks whether a slug names one member; the live
   * publish and the on-connect replay both read a snapshot straight out of the
   * service, so a frame can arrive for a slug the roster has already refused to
   * attribute. Refusing here is what makes the refusal reach the screen, and it
   * needs no audit of every future emitter.
   */
  apply(
    slug: string,
    key: string,
    value: unknown,
    seq: number,
    schema?: ProjectionSchema,
    stateVersion = 0,
  ): void {
    if (this.unattributable.has(slug)) return
    let byKey = this.rows.get(slug)
    const existing = byKey?.get(key)
    const contributed = key.includes('/')
    const isTeardown = contributed && (value === null || value === undefined)

    // A TEARDOWN is decided BEFORE the ordering guard below, because a deletion
    // carries no stateVersion to compare with: the server's frame omits the
    // field (`apps/teardown.py`), so it arrives here as 0, and any contributor
    // that has ever refolded holds a version above that. Asking the version
    // first therefore dropped every deletion and left the removed app's card on
    // screen with no correction until that slug next changed. The server stamps
    // `_DELETION_SEQ` (2^53-1) precisely so a deletion cannot be mistaken for a
    // replay, so the seq rule still applies here; it is the version rule that
    // must not.
    if (isTeardown) {
      // An out-of-order teardown is ignored whole: it is older than the row held
      // here, so neither the removal nor the marker belongs to it.
      if (existing && seq < existing.seq) return
      // RECORD the withdrawal, whether or not a row is held. Removing the row is
      // what leaves a later baseline unguarded: higher-seq-wins compares against a
      // held row, and there is no longer one to compare against, so a roster
      // answer that was already in flight when this landed would seed the card
      // back. The write id says when the withdrawal happened, which is the only
      // thing that can tell such an answer from one the server produced
      // afterwards. A key this browser never held needs the marker MOST: there is
      // no row to compare against at all, so an in-flight answer is otherwise
      // applied unguarded.
      this.recordWithdrawal(slug, key)
      if (!existing) return
      // Remove the row rather than storing null: a stored teardown would sit at
      // this (max) seq and block a re-enable that legitimately folds at a lower
      // seq.
      byKey?.delete(key)
      if (byKey && byKey.size === 0) this.rows.delete(slug)
      this.notify(slug, key)
      this.notifyKeyset(slug)
      return
    }

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
        // A SCHEMA that arrives at the same position is not a replay. The schema
        // decides how the card renders, so dropping it leaves the view drawn by
        // a shape the contributor has replaced. Adopt the schema and leave the
        // value and the position exactly where they are.
        if (schema !== undefined && schema !== existing.schema) {
          byKey?.set(key, { ...existing, schema })
          this.notify(slug, key)
          // The KEYSET too, because that is what the contributed-view card
          // subscribes to. A schema is what the card renders WITH, so notifying
          // only the key leaves an already-mounted card drawn by the shape the
          // contributor has just replaced -- the exact outcome the branch above
          // adopts the schema to avoid.
          this.notifyKeyset(slug)
        }
        return
      }
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
    // This write is newer than any withdrawal of the same key, so the record has
    // done its job and must go: keeping it would make the next baseline refuse
    // the card a contributor has just republished.
    this.withdrawn.get(slug)?.delete(key)
    this.notify(slug, key)
    // A NEW built-in key changes the key set the same way it always did; a
    // contributed key notifies the contributed face on every change (new OR an
    // update to a card already shown), so the card list re-renders on a value
    // push, not only when a card first appears.
    if (!existing || contributed) this.notifyKeyset(slug)
  }

  /**
   * Seed a slug's baseline from the roster block. Each key is applied at asOfSeq
   * through apply(), so a live frame that already advanced the row past asOfSeq
   * keeps winning.
   *
   * `seqs` overrides asOfSeq PER KEY, which contributed rows need: such a row's
   * seq is the contributor's own fold position, not this response's asOfSeq.
   * Seeding one at asOfSeq (usually higher) would make higher-seq-wins drop the
   * contributor's next live push and freeze the card at its baseline.
   *
   * An EMPTY block is a statement, not an absence of one: the roster says this
   * slug holds nothing readable, which is what a shared-slug collision produces
   * for the slug both members claim. Applying no keys would leave whatever is
   * cached in place, and for a collision that cache is the OTHER member's
   * projection -- so the page would keep showing one member's data under a slug
   * the server refuses to attribute, and a stale read of that kind looks
   * identical to a live one.
   *
   * Two empty blocks arrive, and the DIFFERENCE IS THE SEQUENCE:
   *
   * * ``asOfSeq >= 0`` is a real position: the log was read and holds no
   *   projections yet. Higher-seq-wins applies exactly as it does per key, so a
   *   row a live frame already carried past this point is newer and is kept.
   * * ``asOfSeq < 0`` is not a position at all. It is the sentinel the roster
   *   emits when it will not attribute the slug -- a collision, a header naming
   *   another member, or a read that failed -- and every real row's seq is above
   *   it, so comparing them keeps the whole cache instead of dropping it: the
   *   clear fails on precisely the case it exists for. The slug is therefore
   *   cleared UNCONDITIONALLY and marked unattributable, which also refuses the
   *   live frames that reach {@link apply} from the publish and replay paths,
   *   neither of which checks attribution. The mark lifts the moment the roster
   *   sends a baseline carrying a real sequence.
   *
   * The sentinel governs the whole block, not just an empty one: a value carried
   * at a sequence the server would not stand behind is not a baseline, so it is
   * dropped rather than applied at a negative seq that every later frame beats.
   *
   * `since` is the {@link mark} taken BEFORE this baseline was requested, and it
   * decides one case the seq rules cannot: a contributed key a teardown frame
   * withdrew while the request was in flight. Such a key is absent from the
   * store, so higher-seq-wins has no row to compare the stale value against and
   * would recreate the withdrawn card. A baseline older than the withdrawal is
   * refused that key; one taken afterwards is believed, and retires the record.
   * OMITTING it means "this baseline is newer than anything the store holds",
   * which is what every caller without a mark to offer is really saying.
   */
  seed(
    slug: string,
    values: { [key: string]: unknown },
    asOfSeq: number,
    seqs?: { [key: string]: number },
    schemas?: { [key: string]: ProjectionSchema },
    stateVersions?: { [key: string]: number },
    since: number = Number.POSITIVE_INFINITY,
  ): void {
    if (asOfSeq < 0) {
      this.unattributable.add(slug)
      // Every row for this slug goes, and the roster will not say whose they
      // were, so a withdrawal record here guards nothing and is dropped with
      // them rather than left to refuse a later attributable baseline.
      this.withdrawn.delete(slug)
      const byKey = this.rows.get(slug)
      if (!byKey) return
      let droppedContributed = false
      for (const key of [...byKey.keys()]) {
        byKey.delete(key)
        this.notify(slug, key)
        if (key.includes('/')) droppedContributed = true
      }
      this.rows.delete(slug)
      // A contributed card is rendered from a LIST recomputed on the keyset
      // version, not from the per-key face, so the per-key notify above leaves it
      // on screen. Every path that removes a contributed key bumps the keyset for
      // that reason, and a refused attribution is the strongest of them: the
      // server will not say whose rows these are.
      if (droppedContributed) this.notifyKeyset(slug)
      return
    }
    this.unattributable.delete(slug)
    const keys = Object.keys(values)
    if (keys.length === 0) {
      const byKey = this.rows.get(slug)
      if (!byKey) return
      let droppedContributed = false
      for (const [key, row] of [...byKey]) {
        if (row.seq > asOfSeq) continue
        byKey.delete(key)
        this.notify(slug, key)
        if (key.includes('/')) droppedContributed = true
      }
      if (byKey.size === 0) this.rows.delete(slug)
      if (droppedContributed) this.notifyKeyset(slug)
      return
    }
    for (const key of keys) {
      if (this.isWithdrawnAfter(slug, key, since)) continue
      const seq = seqs && typeof seqs[key] === 'number' ? seqs[key] : asOfSeq
      const version =
        stateVersions && typeof stateVersions[key] === 'number' ? stateVersions[key] : 0
      this.apply(slug, key, values[key], seq, schemas?.[key], version)
    }
  }

  /** The write id to take BEFORE a baseline read and hand back to both
   *  `reconcileContributed` and {@link seed}, which is how each tells this
   *  answer from anything that landed while it was in flight. */
  mark(): number {
    return this.writeCounter
  }

  /**
   * Note that `key` was withdrawn, at a write id of its own.
   *
   * The id comes from the same counter a write takes, so a withdrawal and a
   * write are directly comparable — which is what lets {@link isWithdrawnAfter}
   * answer whether a baseline is older than the withdrawal without a clock.
   */
  private recordWithdrawal(slug: string, key: string): void {
    let forSlug = this.withdrawn.get(slug)
    if (!forSlug) {
      forSlug = new Map<string, number>()
      this.withdrawn.set(slug, forSlug)
    }
    forSlug.delete(key)
    forSlug.set(key, ++this.writeCounter)
    // Insertion order is write-id order, because the counter only ever rises and
    // the line above re-inserts a repeat at the end, so the FIRST entry is the
    // oldest. See MAX_WITHDRAWN_KEYS_PER_SLUG for why the oldest is the one to
    // lose.
    while (forSlug.size > MAX_WITHDRAWN_KEYS_PER_SLUG) {
      const oldest = forSlug.keys().next()
      if (oldest.done) break
      forSlug.delete(oldest.value)
    }
  }

  /**
   * Whether a baseline marked `since` is older than this key's withdrawal, and
   * must therefore not recreate it.
   *
   * ANSWERS false and RETIRES the record when the baseline is newer: the read
   * went out after the withdrawal and the server still lists the key, so the
   * server is the authority and the record can only do harm from here on.
   */
  private isWithdrawnAfter(slug: string, key: string, since: number): boolean {
    const forSlug = this.withdrawn.get(slug)
    const at = forSlug?.get(key)
    if (at === undefined) return false
    if (at > since) return true
    forSlug?.delete(key)
    if (forSlug && forSlug.size === 0) this.withdrawn.delete(slug)
    return false
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
   *
   * ANSWERS whether anything was dropped, which the caller needs. Dropping the
   * row is right -- a row above the server's seq records something that did not
   * happen -- but it leaves the card with no value at all, when the truth is
   * whatever the value was at `lastSeq`. This store is a cache and cannot
   * synthesise that, so the only honest repair is for the caller to refetch the
   * authoritative baseline; a silent drop renders a blank card that is
   * indistinguishable from a member who has no such projection.
   */
  truncate(slug: string, lastSeq: number): boolean {
    const byKey = this.rows.get(slug)
    if (!byKey) return false
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
    return dropped
  }

  /**
   * Apply a members_subscribed frame as the authoritative baseline for this
   * connection.
   *
   * `lastSeqs` carries EVERY slug the server holds a readable log for, so a slug
   * the client still caches and the frame omits is one the server cannot serve:
   * its header was damaged or unreadable, or the member is gone. Leaving those
   * rows cached lets them override the empty roster baseline, so the page shows a
   * member's pre-restart state indefinitely -- the stale read looks identical to a
   * live one. Dropped rather than kept, which is the same choice `truncate` makes
   * for a row that ran ahead of the server: the server's view wins.
   */
  truncateAll(lastSeqs: { [slug: string]: number }): boolean {
    let dropped = false
    for (const slug of Object.keys(lastSeqs)) {
      if (this.truncate(slug, lastSeqs[slug])) dropped = true
    }
    // Snapshot the slugs before mutating, since dropping edits `this.rows`.
    for (const slug of [...this.rows.keys()]) {
      if (Object.prototype.hasOwnProperty.call(lastSeqs, slug)) continue
      const byKey = this.rows.get(slug)
      if (!byKey) continue
      for (const key of [...byKey.keys()]) {
        byKey.delete(key)
        this.notify(slug, key)
        dropped = true
      }
      this.rows.delete(slug)
    }
    return dropped
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
