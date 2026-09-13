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
 * Cross-tab: `persistNow` overwrites the whole key, so two open tabs are
 * last-write-wins on the shared draft. No merge: merging breaks LRU order and
 * resurrects intentionally-deleted drafts. Still accepted for the DRAFT itself,
 * where the cost is a lost keystroke -- but no longer on the old grounds that the
 * dashboard is effectively single-tab, which popouts falsify. Unsent-work
 * DETECTION therefore does NOT live here: it reads per-owner keys, because a lost
 * claim discards work on close and a shared blob cannot carry another window's.
 */
import { safeSetItem, safeSetSessionItem } from './safeStorage'

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
  save(drafts: Record<string, T>): void
  set(drafts: Record<string, T>, slot: string, value: T): void
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

  const timestamps: Record<string, number> = {}
  let timestampsLoaded = false

  const store = (): Storage => (storage === 'local' ? localStorage : sessionStorage)
  const safeWrite = (k: string, v: string): boolean =>
    storage === 'local' ? safeSetItem(k, v) : safeSetSessionItem(k, v)

  function ensureTimestampsLoaded(): void {
    if (!hasTtl || timestampsLoaded) return
    timestampsLoaded = true
    try {
      const raw = store().getItem(tsKey)
      const parsed = raw ? JSON.parse(raw) : {}
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
          if (typeof v === 'number') timestamps[k] = v
        }
      }
    } catch { /* ignore */ }
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

  /** Cap `drafts` in place, persist, and report whether the drafts write stuck.
   *  The boolean drives the evict-after-write sync-back in `save`. */
  function persistNow(drafts: Record<string, T>): boolean {
    capEntries(drafts)
    capBytes(drafts)
    if (hasTtl) {
      for (const k of Object.keys(timestamps)) {
        if (!(k in drafts)) delete timestamps[k]
      }
    }
    try {
      // Timestamps BEFORE drafts: the two writes are non-atomic, so if the
      // drafts write fails (quota) we must not strand un-timestamped entries
      // that a later load would mistake for legacy and re-stamp, resetting TTL.
      // safeWrite reclaims disposable cache + retries on quota (never throws).
      if (hasTtl) safeWrite(tsKey, JSON.stringify(timestamps))
      return safeWrite(key, JSON.stringify(drafts))
    } catch (e) {
      // eslint-disable-next-line no-console -- intentional dev-only diagnostic
      if (import.meta.env.DEV) console.warn(`slotDraftStore[${key}]: save failed`, e)
      return false
    }
  }

  function load(): Record<string, T> {
    ensureTimestampsLoaded()
    try {
      const raw = store().getItem(key)
      const parsed = raw ? JSON.parse(raw) : {}
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
      const cutoff = Date.now() - (ttlMs ?? 0)
      const fresh: Record<string, T> = {}
      let pruned = false
      let stamped = false
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        const clean = sanitize(v)
        if (clean === null) { if (k in timestamps) { delete timestamps[k]; pruned = true } continue }
        if (!hasTtl) { fresh[k] = clean; continue }
        // No timestamp = legacy / pre-TTL entry; stamp now and treat as fresh.
        if (!(k in timestamps)) { timestamps[k] = Date.now(); stamped = true }
        if (timestamps[k] >= cutoff) fresh[k] = clean
        else { delete timestamps[k]; pruned = true }
      }
      // Persist when we evicted or stamped, so the next load sees real
      // timestamps instead of re-stamping legacy entries with a fresh Date.now()
      // (which would reset the TTL indefinitely on every reload).
      if (hasTtl && (pruned || stamped)) persistNow(fresh)
      return fresh
    } catch (e) {
      // eslint-disable-next-line no-console -- intentional dev-only diagnostic
      if (import.meta.env.DEV) console.warn(`slotDraftStore[${key}]: load failed`, e)
      return {}
    }
  }

  function save(drafts: Record<string, T>): void {
    ensureTimestampsLoaded()
    if (!evictAfterWrite) { persistNow(drafts); return }
    // Evict-after-write: cap a copy, persist it, and only mirror the evictions
    // back to the caller once the write actually stuck. A failed persist leaves
    // the caller's in-memory drafts whole so nothing that never reached storage
    // is silently dropped.
    const toSave = { ...drafts }
    if (persistNow(toSave)) {
      for (const k of Object.keys(drafts)) if (!(k in toSave)) delete drafts[k]
    }
  }

  /** Mutate `drafts` for `slot`: delete-then-reinsert a sanitized deep copy if
   *  accepted (refreshes LRU position), delete if `sanitize` rejects it (empty /
   *  corrupt). Stamps touch time for TTL eviction when the store has a TTL. */
  function set(drafts: Record<string, T>, slot: string, value: T): void {
    ensureTimestampsLoaded()
    delete drafts[slot]
    const clean = sanitize(value)
    if (clean !== null) {
      drafts[slot] = clean
      if (hasTtl) timestamps[slot] = Date.now()
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
