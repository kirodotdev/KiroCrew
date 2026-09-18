/**
 * Generic per-slot draft persistence factory. Extracts the
 * load -> sanitize -> TTL-prune -> LRU/byte-cap -> persist skeleton shared by
 * `chatDrafts`, `chatFileDrafts`, `chatPasteDrafts`, `goalDrafts`, and
 * `commentDrafts`. Each module becomes a thin instance configured through
 * `SlotDraftStoreOpts`.
 *
 * One storage key holds a single JSON blob (`Record<slot, T>`); quota is
 * enforced on the whole blob. Two eviction policies bound growth:
 *   - `maxEntries`: drop oldest slots beyond a count cap (LRU by insertion order).
 *   - `maxStoreBytes`: drop oldest slots until the serialized blob fits a byte
 *     budget. The newest slot is NEVER evicted, even if it alone exceeds the
 *     budget, so the most-recent large draft always survives. The byte-aware
 *     LRU applies uniformly, so a big paste persists whether it lives in a
 *     `PasteBlock` (collapsed) or spliced into the text draft (expanded).
 *
 * All functions are safe against corrupt / missing / quota-exhausted storage:
 * worst case the affected slot is dropped, never a throw. Writes go through
 * `safeSetItem` / `safeSetSessionItem` so a quota hit
 * reclaims disposable cache and retries instead of silently losing the write.
 *
 * Cross-tab: explicit saves overwrite the whole raw body key, so two user edits
 * remain last-write-wins. TTL loads read sidecar/body as a stable pair and never
 * rewrite the body while filtering expiry; a concurrent tab's committed body
 * therefore cannot be erased by load-time cleanup. No merge: merging breaks LRU
 * order and resurrects intentionally-deleted drafts.
 */
import { safeSetItem, safeSetSessionItem } from './safeStorage'

/**
 * Create module-owned state keyed by externally supplied slot names.
 *
 * Slot names may equal Object.prototype members such as `toString`. Every
 * timestamp, fallback, and reconciliation registry must therefore start with
 * no prototype: an unseen slot always reads as `undefined`, never as an
 * inherited function that can crash or masquerade as stored state.
 */
export function createSlotKeyedRecord<T>(): Record<string, T> {
  return Object.create(null) as Record<string, T>
}

export interface SlotDraftStoreOpts<T> {
  /** Storage key holding the `Record<slot, T>` blob. */
  key: string
  /** Which Web Storage to use. `local` survives tab close; `session` clears on it. */
  storage: 'local' | 'session'
  /** Discard entries not touched within this window. Omit for no TTL. */
  ttlMs?: number
  /** Max slot count; oldest beyond this are evicted (LRU). Omit for no entry cap. */
  maxEntries?: number
  /** Byte budget for the serialized blob; oldest slots are evicted until it
   *  fits. The newest slot is never the casualty. Omit for no byte cap. */
  maxStoreBytes?: number
  /** Corruption guard, emptiness predicate, and defensive copier in one: returns
   *  a cleaned deep copy to store, or `null` to drop the slot (corrupt, or
   *  semantically empty like `''` / `[]`). Run on both load and set, so a value
   *  it accepts is always isolated from the caller's reference. */
  sanitize: (v: unknown) => T | null
  /** Eviction ordering on `save`. Default (false) caps the caller's object in
   *  place BEFORE the write, so the in-memory copy always matches storage. True
   *  caps a COPY and only syncs evictions back to the caller AFTER a successful
   *  persist, so a failed write (e.g. QuotaExceeded) never silently drops
   *  in-memory drafts that were never persisted (`commentDrafts` contract).
   *  Pair only with non-TTL stores: a failed write keeps the caller's evicted
   *  slots while the shared `timestamps` map already dropped them, so combining
   *  with `ttlMs` would desync the two. No current instance combines them. */
  evictAfterWrite?: boolean
}

export interface SlotDraftStore<T> {
  load(): Record<string, T>
  /** Persist `drafts`. Outcome is observable through storage, never returned:
   *  every consumer discards it, and a failed write already leaves the caller's
   *  in-memory drafts whole (see `evictAfterWrite`). */
  save(drafts: Record<string, T>): void
  set(drafts: Record<string, T>, slot: string, value: T, updatedAt?: number): void
  /** @internal test-only: reset module state between tests. `undefined` in the
   *  prod bundle (gated on `!import.meta.env.PROD`). */
  __resetForTests: () => void
}

export function createSlotDraftStore<T>(opts: SlotDraftStoreOpts<T>): SlotDraftStore<T> {
  const { key, storage, ttlMs, maxEntries, maxStoreBytes, sanitize, evictAfterWrite } = opts
  const tsKey = `${key}-ts`
  const hasTtl = ttlMs !== undefined

  // evictAfterWrite keeps the caller's evicted slots on a failed write, but
  // capEntries already dropped them from `timestamps`; combining with a TTL
  // desyncs the two. Warn loudly so a future instance can't do it silently.
  if (import.meta.env.DEV && hasTtl && evictAfterWrite) {
    // eslint-disable-next-line no-console -- intentional dev-only diagnostic
    console.warn(`slotDraftStore[${key}]: evictAfterWrite + ttlMs desyncs timestamps on a failed write; use one or the other`)
  }

  const timestamps = createSlotKeyedRecord<number>()
  let timestampsLoaded = false

  const store = (): Storage => (storage === 'local' ? localStorage : sessionStorage)
  const safeWrite = (k: string, v: string): boolean =>
    storage === 'local' ? safeSetItem(k, v) : safeSetSessionItem(k, v)

  function replaceTimestamps(value: unknown): void {
    for (const k of Object.keys(timestamps)) delete timestamps[k]
    if (!value || typeof value !== 'object' || Array.isArray(value)) return
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (typeof v === 'number' && Number.isFinite(v)) timestamps[k] = v
    }
  }

  function replaceTimestampsFromRaw(raw: string | null): void {
    for (const k of Object.keys(timestamps)) delete timestamps[k]
    if (!raw) return
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
          if (typeof v === 'number') timestamps[k] = v
        }
      }
    } catch { /* ignore */ }
  }

  function refreshTimestampsFromStorage(): void {
    if (!hasTtl) return
    timestampsLoaded = true
    try {
      replaceTimestampsFromRaw(store().getItem(tsKey))
    } catch {
      // Disabled/denied Web Storage is an ordinary fallback condition, not a
      // reason to crash the editor. Clear the cache so load returns {} safely.
      replaceTimestamps(null)
    }
  }

  function ensureTimestampsLoaded(): void {
    if (!hasTtl || timestampsLoaded) return
    refreshTimestampsFromStorage()
  }

  interface RawDraftPair {
    bodyRaw: string | null
    timestampRaw: string | null
    stable: boolean
  }

  function readBodyAndTimestamps(): RawDraftPair {
    if (!hasTtl) return { bodyRaw: store().getItem(key), timestampRaw: null, stable: true }
    let timestampRaw = store().getItem(tsKey)
    let bodyRaw = store().getItem(key)
    let stable = false
    // Writers commit sidecar before body. If either value changes across the
    // confirmation reads, retry the whole pair rather than pruning a new body
    // with a stale timestamp snapshot.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const confirmedTimestampRaw = store().getItem(tsKey)
      const confirmedBodyRaw = store().getItem(key)
      if (confirmedTimestampRaw === timestampRaw && confirmedBodyRaw === bodyRaw) {
        stable = true
        break
      }
      timestampRaw = confirmedTimestampRaw
      bodyRaw = confirmedBodyRaw
    }
    return { bodyRaw, timestampRaw, stable }
  }

  function pairMatches(
    pair: RawDraftPair,
    bodyRaw: string | null,
    timestampRaw: string | null,
  ): boolean {
    return pair.stable && pair.bodyRaw === bodyRaw && pair.timestampRaw === timestampRaw
  }

  function writeTimestampRaw(raw: string | null): boolean {
    if (raw !== null) return safeWrite(tsKey, raw)
    try {
      store().removeItem(tsKey)
      return store().getItem(tsKey) === null
    } catch {
      return false
    }
  }

  type RestoreResult = 'restored' | 'superseded' | 'failed'

  function restoreTimestampSidecarIfOwned(
    previous: RawDraftPair,
    ownedTimestampRaw: string,
  ): RestoreResult {
    if (!previous.stable) return 'superseded'
    try {
      // A failed body write owns the sidecar only while BOTH halves still match
      // the pair it created. Another tab commits sidecar first, then body; either
      // difference means its newer pair must win and an old sidecar must not be
      // restored over it.
      const current = readBodyAndTimestamps()
      if (!pairMatches(current, previous.bodyRaw, ownedTimestampRaw)) return 'superseded'
      if (!writeTimestampRaw(previous.timestampRaw)) return 'failed'
      const confirmed = readBodyAndTimestamps()
      return pairMatches(confirmed, previous.bodyRaw, previous.timestampRaw)
        ? 'restored'
        : 'superseded'
    } catch {
      return 'failed'
    }
  }

  function refreshTimestampsAfterFailure(fallbackRaw: string | null): void {
    try {
      const current = readBodyAndTimestamps()
      if (current.stable) {
        replaceTimestampsFromRaw(current.timestampRaw)
        return
      }
    } catch { /* fall back to the pre-write sidecar below */ }
    replaceTimestampsFromRaw(fallbackRaw)
  }

  function persistMissingTimestamps(
    bodyRaw: string | null,
    timestampRaw: string | null,
    stampedSlots: string[],
  ): void {
    if (!hasTtl || stampedSlots.length === 0) return
    const merged = createSlotKeyedRecord<number>()
    try {
      if (timestampRaw) {
        const prior: unknown = JSON.parse(timestampRaw)
        if (prior && typeof prior === 'object' && !Array.isArray(prior)) {
          for (const [slot, value] of Object.entries(prior as Record<string, unknown>)) {
            if (typeof value === 'number' && Number.isFinite(value)) merged[slot] = value
          }
        }
      }
      for (const slot of stampedSlots) {
        if (!(slot in merged)) merged[slot] = timestamps[slot]
      }
      // Missing-stamp migration owns only the exact stable pair load observed.
      // It never rewrites the body, and it declines the sidecar write if another
      // tab changed either half before this repair reached storage.
      const current = readBodyAndTimestamps()
      if (!pairMatches(current, bodyRaw, timestampRaw)) return
      if (safeWrite(tsKey, JSON.stringify(merged))) replaceTimestamps(merged)
      // On false, safeWrite may have failed before or just after the browser
      // accepted the value. Do not restore an older sidecar: that repair would
      // have no CAS ownership once another tab can commit in between.
    } catch { /* a missing legacy stamp remains an in-memory fallback */ }
  }

  /**
   * Evict oldest entries (by insertion order) past `maxEntries`. Mutates in
   * place. `set` reinserts on every write so the most-recently-touched slot is
   * never evicted. Slot keys are `chat-<counter>-<timestamp>`, so the
   * numeric-key enumeration quirk of `Object.keys()` cannot trigger.
   */
  function capEntries(drafts: Record<string, T>): void {
    if (maxEntries === undefined) return
    const keys = Object.keys(drafts)
    if (keys.length <= maxEntries) return
    for (const k of keys.slice(0, keys.length - maxEntries)) {
      delete drafts[k]
      delete timestamps[k]
    }
  }

  /**
   * Byte-aware LRU: evict oldest slots until the serialized blob fits
   * `maxStoreBytes`. Oldest-first by insertion order (same `Object.keys()`
   * invariant `capEntries` documents); the newest slot is never evicted (the
   * loop stops with one entry remaining), so the most-recent large draft is
   * always durable even when it alone exceeds the budget. Mutates in place.
   *
   * O(n) of serialized size: the blob is stringified ONCE for the starting
   * total, then each evicted slot's exact contribution is subtracted from a
   * running total. Avoids re-stringifying the whole blob per iteration in the
   * large-paste path the byte cap exists for.
   *
   * "Bytes" here is `String.length` = UTF-16 code units, matching how the
   * budget is expressed and how the blob is measured. It's an approximation of
   * real storage bytes (most engines store UTF-16, but multi-byte chars and
   * key/value JSON escaping shift the true figure). Exact byte accounting isn't
   * needed: the budget is a generous headroom bound, not a hard quota.
   */
  function capBytes(drafts: Record<string, T>): void {
    if (maxStoreBytes === undefined) return
    let total = JSON.stringify(drafts).length
    if (total <= maxStoreBytes) return
    const keys = Object.keys(drafts)
    // Stop before the last (newest) key so it is never the eviction target.
    for (let i = 0; i < keys.length - 1 && total > maxStoreBytes; i++) {
      const k = keys[i]
      // Code units this slot adds to the blob: `"key"` + `:` + value + `,` (the
      // final entry's comma is replaced by `}`, also 1 unit). Subtracting the
      // exact contribution keeps `total` accurate without re-serializing.
      total -= JSON.stringify(k).length + JSON.stringify(drafts[k]).length + 2
      delete drafts[k]
      delete timestamps[k]
    }
  }

  /**
   * A sibling writer can restore the pre-write sidecar after this writer's
   * sidecar operation returns but before its body commits. Repair only that
   * exact rollback shape: this body still owns the pair and the sidecar equals
   * the stable pre-write value. Any other stable pair belongs to a concurrent
   * writer. In both cases adopt the durable sidecar in memory so a later save
   * cannot downgrade it.
   */
  function reconcileCommittedBodyTimestamp(
    previous: RawDraftPair,
    draftRaw: string,
    timestampRaw: string,
  ): void {
    try {
      let current = readBodyAndTimestamps()
      if (
        current.stable
        && current.bodyRaw === draftRaw
        && current.timestampRaw === previous.timestampRaw
        && current.timestampRaw !== timestampRaw
      ) {
        safeWrite(tsKey, timestampRaw)
        current = readBodyAndTimestamps()
      }
      if (current.stable) {
        replaceTimestampsFromRaw(current.timestampRaw)
        return
      }
    } catch { /* refresh from storage or the stable pre-write fallback below */ }
    refreshTimestampsAfterFailure(previous.timestampRaw)
  }

  /** Cap `drafts` in place, persist, and report whether the drafts write stuck.
   *  The boolean drives the evict-after-write sync-back in `save`. */
  function persistNow(drafts: Record<string, T>): boolean {
    capEntries(drafts)
    capBytes(drafts)
    if (hasTtl) {
      for (const k of Object.keys(timestamps)) {
        if (!Object.prototype.hasOwnProperty.call(drafts, k)) delete timestamps[k]
      }
    }
    try {
      const draftRaw = JSON.stringify(drafts)
      if (!hasTtl) return safeWrite(key, draftRaw)

      const timestampRaw = JSON.stringify(timestamps)
      const previous = readBodyAndTimestamps()
      if (!safeWrite(tsKey, timestampRaw)) {
        const restore = restoreTimestampSidecarIfOwned(previous, timestampRaw)
        refreshTimestampsAfterFailure(previous.timestampRaw)
        if (restore === 'failed' && import.meta.env.DEV) {
          // eslint-disable-next-line no-console -- partial persistence needs an operator-visible diagnostic
          console.warn(`slotDraftStore[${key}]: failed to restore timestamps after timestamp persist failure`)
        }
        return false
      }

      const saved = safeWrite(key, draftRaw)
      if (saved) {
        reconcileCommittedBodyTimestamp(previous, draftRaw, timestampRaw)
        return true
      }

      // A Storage implementation can accept a write and then throw. Treat the
      // exact completed pair as success; otherwise roll back only while the
      // sidecar is still ours and the body is still the pre-write body.
      const current = readBodyAndTimestamps()
      if (pairMatches(current, draftRaw, timestampRaw)) {
        replaceTimestampsFromRaw(current.timestampRaw)
        return true
      }
      const restore = restoreTimestampSidecarIfOwned(previous, timestampRaw)
      refreshTimestampsAfterFailure(previous.timestampRaw)
      if (restore === 'failed' && import.meta.env.DEV) {
        // eslint-disable-next-line no-console -- partial persistence needs an operator-visible diagnostic
        console.warn(`slotDraftStore[${key}]: failed to restore timestamps after draft persist failure`)
      }
      return false
    } catch (e) {
      // eslint-disable-next-line no-console -- intentional dev-only diagnostic
      if (import.meta.env.DEV) console.warn(`slotDraftStore[${key}]: save failed`, e)
      return false
    }
  }

  function load(): Record<string, T> {
    try {
      const { bodyRaw, timestampRaw, stable } = readBodyAndTimestamps()
      timestampsLoaded = true
      replaceTimestampsFromRaw(timestampRaw)
      if (!stable) return createSlotKeyedRecord<T>()
      const parsed: unknown = bodyRaw ? JSON.parse(bodyRaw) : {}
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        replaceTimestamps(null)
        return createSlotKeyedRecord<T>()
      }
      const cutoff = Date.now() - (ttlMs ?? 0)
      const fresh = createSlotKeyedRecord<T>()
      const stampedSlots: string[] = []
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        const clean = sanitize(v)
        if (clean === null) { delete timestamps[k]; continue }
        if (!hasTtl) { fresh[k] = clean; continue }
        // No timestamp = legacy / pre-TTL entry; stamp now and treat as fresh.
        if (!(k in timestamps)) {
          timestamps[k] = Date.now()
          stampedSlots.push(k)
        }
        if (timestamps[k] >= cutoff) fresh[k] = clean
        else delete timestamps[k]
      }
      if (hasTtl) {
        for (const k of Object.keys(timestamps)) {
          if (!Object.prototype.hasOwnProperty.call(fresh, k)) delete timestamps[k]
        }
      }
      // Never rewrite the shared body from a load: another tab may have
      // committed a body after this snapshot. Expired values stay filtered by
      // their persisted old timestamp; only missing legacy timestamps are
      // merged into the sidecar.
      persistMissingTimestamps(bodyRaw, timestampRaw, stampedSlots)
      return fresh
    } catch (e) {
      replaceTimestamps(null)
      // eslint-disable-next-line no-console -- intentional dev-only diagnostic
      if (import.meta.env.DEV) console.warn(`slotDraftStore[${key}]: load failed`, e)
      return createSlotKeyedRecord<T>()
    }
  }

  function save(drafts: Record<string, T>): void {
    ensureTimestampsLoaded()
    if (!evictAfterWrite) { persistNow(drafts); return }
    // Evict-after-write: cap a copy, persist it, and only mirror the evictions
    // back to the caller once the write actually stuck. A failed persist leaves
    // the caller's in-memory drafts whole so nothing that never reached storage
    // is silently dropped.
    const toSave = Object.assign(createSlotKeyedRecord<T>(), drafts)
    if (persistNow(toSave)) {
      for (const k of Object.keys(drafts)) if (!(k in toSave)) delete drafts[k]
    }
  }

  /** Mutate `drafts` for `slot`: delete-then-reinsert a sanitized deep copy if
   *  accepted (refreshes LRU position), delete if `sanitize` rejects it (empty /
   *  corrupt). Stamps touch time for TTL eviction when the store has a TTL. */
  function set(drafts: Record<string, T>, slot: string, value: T, updatedAt?: number): void {
    ensureTimestampsLoaded()
    delete drafts[slot]
    const clean = sanitize(value)
    if (clean !== null) {
      drafts[slot] = clean
      if (hasTtl) timestamps[slot] = updatedAt ?? Date.now()
    } else if (hasTtl) {
      delete timestamps[slot]
    }
  }

  const __resetForTests: () => void = import.meta.env.PROD
    ? (undefined as unknown as () => void)
    : () => {
        for (const k of Object.keys(timestamps)) delete timestamps[k]
        timestampsLoaded = false
      }

  return { load, save, set, __resetForTests }
}
