/**
 * The bounded synchronous Web Storage adapter for the opaque Remote Crew relay
 * pane, and its parent-side per-instance backing bank.
 *
 * ## Why this exists
 *
 * A relay pane is a sandboxed iframe served WITHOUT `allow-same-origin`, so its
 * document runs at an opaque origin. At an opaque origin `window.localStorage`
 * and `window.sessionStorage` throw `SecurityError` on *access* — and the SPA
 * reads storage during module evaluation (i18n language, ui-prefs), so the pane
 * would crash before it rendered. The child therefore installs these shims
 * BEFORE any app module runs (see `relayPaneBootstrap.ts`): reads are served
 * synchronously from an in-memory map seeded by the parent, and each mutation is
 * reported so the parent can persist it under its own origin.
 *
 * The parent owns the durable copy. `localStorage` is per-origin, and the pane's
 * opaque origin has none it can keep, so the parent stores each connected crew's
 * relay `localStorage` under the PARENT origin, namespaced per instance and
 * bounded, and seeds the child from it on every (re)mount. `sessionStorage` is
 * modelled the same way but lives only for the parent's page session.
 *
 * ## Bounds (a hostile or buggy pane must not exhaust the parent)
 *
 * Every write is checked against four per-instance caps — key count, key bytes,
 * value bytes, and total bytes across all entries — AND, on the parent bank, one
 * aggregate byte budget across every instance namespace in the backing store, so
 * many panes each within their own cap can never sum past the parent origin's
 * quota. The child shim throws `QuotaExceededError` (native `Storage` semantics)
 * so the SPA's own quota handling runs; the parent bank silently drops an
 * over-cap or over-budget mutation rather than throw, and every access to the
 * backing store is guarded, because a child message must never be able to raise
 * in the parent's message loop — even when the parent origin's storage is
 * unavailable, private, or full.
 *
 * No test-only seams: the parent injects its real `window.localStorage` as the
 * bank's backing store (a dependency), and the child injects the seed snapshot
 * and a mutation sink. Tests pass an in-memory `Storage` and a capturing sink
 * through the same parameters production uses.
 */

/** UTF-8 byte length of a string (what a byte cap must measure, not code units). */
const _encoder = new TextEncoder()
export function utf8Bytes(s: string): number {
  return _encoder.encode(s).length
}

/** The four independent bounds every relay-storage write is held to. */
export interface RelayStorageCaps {
  /** Maximum number of distinct keys. */
  readonly maxKeys: number
  /** Maximum UTF-8 bytes in a single key. */
  readonly maxKeyBytes: number
  /** Maximum UTF-8 bytes in a single value. */
  readonly maxValueBytes: number
  /** Maximum UTF-8 bytes across every key + value held. */
  readonly maxTotalBytes: number
}

/**
 * Conservative defaults. Web Storage is nominally ~5 MB per origin; a relay pane
 * needs only its own small preference set (theme, language, ui-prefs, a few
 * feature flags), so these are far tighter than a real origin's quota and keep
 * one crew's pane from crowding the parent origin's own storage.
 */
export const DEFAULT_RELAY_STORAGE_CAPS: RelayStorageCaps = Object.freeze({
  maxKeys: 200,
  maxKeyBytes: 512,
  maxValueBytes: 256 * 1024,
  maxTotalBytes: 2 * 1024 * 1024,
})

/** A single storage change, the unit the child reports up and the parent applies. */
export type RelayStorageMutation =
  | { readonly op: 'set'; readonly key: string; readonly value: string }
  | { readonly op: 'remove'; readonly key: string }
  | { readonly op: 'clear' }

/**
 * Parse an UNTRUSTED value (a relay pane's `postMessage` payload) into the exact
 * {@link RelayStorageMutation} union, or `null` when it does not match.
 *
 * The relay pane is a sandboxed, attacker-reachable frame, so the parent MUST
 * validate the mutation shape here before handing it to {@link RelayStorageBank}
 * rather than casting it. A missing field, a wrong-typed field, or an unknown
 * `op` is rejected — so a malformed message is dropped without mutating the bank
 * and without raising in the parent's message loop, while a well-formed later
 * message still applies. It does NOT reject an unmatched-surrogate key (that is a
 * well-typed string); the bank's key encoding is made exception-safe instead.
 */
export function parseRelayStorageMutation(value: unknown): RelayStorageMutation | null {
  if (typeof value !== 'object' || value === null) return null
  const op = (value as { op?: unknown }).op
  if (op === 'clear') return { op: 'clear' }
  if (op === 'set') {
    const key = (value as { key?: unknown }).key
    const val = (value as { value?: unknown }).value
    if (typeof key !== 'string' || typeof val !== 'string') return null
    return { op: 'set', key, value: val }
  }
  if (op === 'remove') {
    const key = (value as { key?: unknown }).key
    if (typeof key !== 'string') return null
    return { op: 'remove', key }
  }
  return null
}

/** The child shim: a synchronous `Storage` plus a downstream-apply entry point. */
export interface RelayStorage extends Storage {
  /**
   * Apply a mutation the PARENT pushed down (an authoritative reconcile) without
   * echoing it back up — the sink is deliberately not called, so the two sides
   * cannot ping-pong.
   */
  applyDownstream(mutation: RelayStorageMutation): void
  /**
   * Replace the whole map from an authoritative parent snapshot, without echoing
   * up. The bootstrap port handshake calls this on the reply: the channel and the
   * durable bank both arrive over the document port, so a snapshot the child
   * pre-seeded synchronously (a first-load `window.name` accelerator) is
   * SUPERSEDED by the bank the parent is authoritative for. Bounded exactly like
   * a seed — an over-cap entry is skipped, never thrown — because the pane must
   * boot even from a snapshot a later cap change made too large.
   */
  reseed(snapshot: Record<string, string>): void
}

/** True when adding/replacing `key`→`value` in `map` would breach any cap. */
function _wouldExceed(
  map: Map<string, string>,
  key: string,
  value: string,
  caps: RelayStorageCaps,
): boolean {
  if (utf8Bytes(key) > caps.maxKeyBytes) return true
  if (utf8Bytes(value) > caps.maxValueBytes) return true
  const isNew = !map.has(key)
  if (isNew && map.size >= caps.maxKeys) return true
  let total = 0
  for (const [k, v] of map) {
    if (k === key) continue // replaced below
    total += utf8Bytes(k) + utf8Bytes(v)
  }
  total += utf8Bytes(key) + utf8Bytes(value)
  return total > caps.maxTotalBytes
}

/** Seed a map from a snapshot, silently skipping entries that would breach caps.
 *  Seeding NEVER throws: the pane must boot even from a snapshot a cap change
 *  later made too large. Insertion order is preserved so truncation is
 *  deterministic. */
function _seed(snapshot: Record<string, string>, caps: RelayStorageCaps): Map<string, string> {
  const map = new Map<string, string>()
  for (const [k, v] of Object.entries(snapshot)) {
    const value = String(v)
    if (!_wouldExceed(map, k, value, caps)) map.set(k, value)
  }
  return map
}

function _quotaError(): DOMException {
  return new DOMException('relay storage quota exceeded', 'QuotaExceededError')
}

/**
 * Build the child's synchronous storage shim.
 *
 * @param snapshot  the parent-seeded initial contents (not a mutation)
 * @param caps      the four write bounds
 * @param onMutate  called with each local set/remove/clear so the parent can
 *                   persist it; NEVER called for a seed or a downstream apply
 */
export function createRelayStorage(
  snapshot: Record<string, string>,
  caps: RelayStorageCaps,
  onMutate: (mutation: RelayStorageMutation) => void,
): RelayStorage {
  let map = _seed(snapshot, caps)

  const setLocal = (key: string, value: string, report: boolean): void => {
    const v = String(value)
    if (_wouldExceed(map, key, v, caps)) {
      if (report) throw _quotaError()
      return // downstream/seed: drop silently, never throw
    }
    map.set(key, v)
    if (report) onMutate({ op: 'set', key, value: v })
  }

  const shim: RelayStorage = {
    get length() {
      return map.size
    },
    key(index: number): string | null {
      if (!Number.isInteger(index) || index < 0) return null
      let i = 0
      for (const k of map.keys()) {
        if (i === index) return k
        i++
      }
      return null
    },
    getItem(key: string): string | null {
      return map.has(key) ? map.get(key)! : null
    },
    setItem(key: string, value: string): void {
      setLocal(String(key), String(value), true)
    },
    removeItem(key: string): void {
      const k = String(key)
      if (map.delete(k)) onMutate({ op: 'remove', key: k })
    },
    clear(): void {
      map.clear()
      onMutate({ op: 'clear' })
    },
    applyDownstream(mutation: RelayStorageMutation): void {
      switch (mutation.op) {
        case 'set':
          setLocal(mutation.key, mutation.value, false)
          break
        case 'remove':
          map.delete(mutation.key)
          break
        case 'clear':
          map.clear()
          break
      }
    },
    reseed(snapshot: Record<string, string>): void {
      map = _seed(snapshot, caps)
    },
  }
  return shim
}

// ── parent-side bank ─────────────────────────────────────────────────────────

/**
 * `relay-ls:<enc(instanceId)>:` — the per-instance namespace prefix in the
 * parent's backing store. `encodeURIComponent` never emits a bare `:` (it maps
 * to `%3A`), so the id segment is injective and an id that itself contains `:`
 * cannot be read as another instance's key.
 */
const _NS = 'relay-ls:'

/**
 * `encodeURIComponent` made exception-safe, returning `null` instead of throwing
 * `URIError`.
 *
 * `encodeURIComponent` throws `URIError: URI malformed` on an unmatched
 * surrogate (a lone `\uD800`–`\uDFFF`). An inbound child mutation key — or a
 * pathological instance id — reaching it unguarded would raise straight out of
 * the parent's `message` listener. Every backing key this bank forms is encoded
 * through here so that a key that cannot be encoded DROPS the write (the caller
 * returns) rather than throwing or storing a mangled key; a later valid mutation
 * is unaffected. The result round-trips through `decodeURIComponent` on the
 * snapshot read path, so a stored key still decodes back to what the child set.
 */
function _encodeKeyOrNull(s: string): string | null {
  try {
    return encodeURIComponent(s)
  } catch {
    return null
  }
}

/** The per-instance namespace prefix, or `null` when the instance id cannot be
 *  encoded (defensive: ids are parent-minted, but the bank must never throw). */
function _prefix(instanceId: string): string | null {
  const enc = _encodeKeyOrNull(instanceId)
  return enc === null ? null : `${_NS}${enc}:`
}

/**
 * Total bytes the relay may hold across EVERY instance namespace in one backing
 * store — the aggregate budget that per-instance caps alone cannot provide. Web
 * Storage is ~5 MiB per origin and the parent origin also keeps the dashboard's
 * own preferences there, so the whole relay set is bounded well below that: with
 * up to ten warm panes each permitted 2 MiB (`maxTotalBytes`), the per-instance
 * caps would otherwise admit 20 MiB and blow the origin quota. This ceiling is
 * what keeps a hostile or merely busy set of panes from exhausting the parent
 * origin's storage.
 */
export const DEFAULT_RELAY_AGGREGATE_BYTES = 4 * 1024 * 1024

/**
 * A minimal synchronous in-memory `Storage`. The parent falls back to this when
 * the real `window.localStorage`/`sessionStorage` is unavailable (private mode,
 * blocked cookies, an opaque parent) so relay panes still get seeded and mutated
 * for the page's lifetime — persistence simply does not survive a reload, which
 * is the documented degradation rather than a thrown error.
 */
export function memoryStorage(): Storage {
  const m = new Map<string, string>()
  return {
    get length() {
      return m.size
    },
    clear: () => m.clear(),
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    key: (i: number) => Array.from(m.keys())[i] ?? null,
    removeItem: (k: string) => void m.delete(k),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
  } as Storage
}

/**
 * The parent-side durable bank for relay-pane storage. Each connected crew's
 * relay `localStorage` lives under the parent origin, namespaced and bounded,
 * so the pane's opaque origin (which can persist nothing itself) still keeps its
 * preferences across reloads. Instances are isolated: `snapshot`, `apply`, and a
 * `clear` mutation all operate only within one instance's namespace.
 *
 * NOTHING here throws. Every access to the backing `Storage` is guarded, so an
 * unavailable, private, or full parent origin degrades to dropped persistence
 * rather than an exception in the parent's message loop; a write is also bounded
 * by an aggregate budget across every instance namespace, so per-instance caps
 * can never sum past the origin's quota.
 */
export class RelayStorageBank {
  constructor(
    private readonly backing: Storage,
    private readonly caps: RelayStorageCaps = DEFAULT_RELAY_STORAGE_CAPS,
    private readonly aggregateBytes: number = DEFAULT_RELAY_AGGREGATE_BYTES,
  ) {}

  // ── exception-safe backing access ──────────────────────────────────────────
  // A native `Storage` can throw on ANY access (SecurityError when storage is
  // disabled/opaque, QuotaExceededError on a full write). Each wrapper swallows
  // that so a child message can never raise in the parent's listener; a failed
  // read reports "absent" and a failed write silently drops (persistence lost).

  private _len(): number {
    try {
      return this.backing.length
    } catch {
      return 0
    }
  }

  private _key(i: number): string | null {
    try {
      return this.backing.key(i)
    } catch {
      return null
    }
  }

  private _get(key: string): string | null {
    try {
      return this.backing.getItem(key)
    } catch {
      return null
    }
  }

  private _set(key: string, value: string): void {
    try {
      this.backing.setItem(key, value)
    } catch {
      /* full / unavailable — persistence drops; the child keeps its own copy */
    }
  }

  private _remove(key: string): void {
    try {
      this.backing.removeItem(key)
    } catch {
      /* unavailable — nothing to remove */
    }
  }

  /** UTF-8 bytes held across EVERY relay instance namespace in this backing
   *  store, optionally excluding one full key (the one about to be rewritten). */
  private _aggregateBytesExcluding(excludeFullKey: string | null): number {
    let total = 0
    const len = this._len()
    for (let i = 0; i < len; i++) {
      const full = this._key(i)
      if (full === null || !full.startsWith(_NS)) continue
      if (full === excludeFullKey) continue
      const value = this._get(full)
      if (value === null) continue
      total += utf8Bytes(full) + utf8Bytes(value)
    }
    return total
  }

  /** This instance's full key→value snapshot, for seeding a (re)mounted pane.
   *
   *  A stored key whose suffix is a malformed percent escape (a stale or
   *  hand-edited `relay-ls:<id>:%`) makes `decodeURIComponent` throw. That must
   *  not raise into `snapshot` — it runs on the parent's iframe-seed path, whose
   *  whole contract is that unavailable or malformed storage degrades to a
   *  smaller snapshot rather than an exception in the render/message loop. The
   *  malformed entry is skipped and every valid entry is retained. */
  snapshot(instanceId: string): Record<string, string> {
    const prefix = _prefix(instanceId)
    if (prefix === null) return {} // unencodable id — degrade to an empty seed
    const out: Record<string, string> = {}
    const len = this._len()
    for (let i = 0; i < len; i++) {
      const full = this._key(i)
      if (full === null || !full.startsWith(prefix)) continue
      const value = this._get(full)
      if (value === null) continue
      let key: string
      try {
        key = decodeURIComponent(full.slice(prefix.length))
      } catch {
        continue // malformed percent escape — skip this one, keep the rest
      }
      out[key] = value
    }
    return out
  }

  /** Apply one child mutation, enforcing per-instance caps AND the aggregate
   *  budget. Over-cap or over-budget writes are dropped, and every backing
   *  access is guarded — a child message never raises in the parent's message
   *  loop, whatever the origin's storage state. */
  apply(instanceId: string, mutation: RelayStorageMutation): void {
    const prefix = _prefix(instanceId)
    if (prefix === null) return // unencodable id — drop, never throw
    switch (mutation.op) {
      case 'set': {
        // Encode the key exception-safely: an unmatched-surrogate key would make
        // encodeURIComponent throw straight out of the parent's message loop.
        // A key that cannot be encoded drops the write rather than raising.
        const encKey = _encodeKeyOrNull(mutation.key)
        if (encKey === null) return
        // Re-materialise this instance's map to check per-instance caps against
        // the whole set.
        const current = this.snapshot(instanceId)
        const map = new Map(Object.entries(current))
        if (_wouldExceed(map, mutation.key, mutation.value, this.caps)) return
        // Aggregate budget across ALL instance namespaces: every relay key's
        // bytes, minus the exact key being replaced, plus the new key+value,
        // must stay within the total.
        const fullKey = prefix + encKey
        const projected =
          this._aggregateBytesExcluding(fullKey) + utf8Bytes(fullKey) + utf8Bytes(mutation.value)
        if (projected > this.aggregateBytes) return
        this._set(fullKey, mutation.value)
        break
      }
      case 'remove': {
        const encKey = _encodeKeyOrNull(mutation.key)
        if (encKey === null) return // unencodable key — drop, never throw
        this._remove(prefix + encKey)
        break
      }
      case 'clear': {
        // Collect this instance's keys first, then delete — never touch another's.
        const doomed: string[] = []
        const len = this._len()
        for (let i = 0; i < len; i++) {
          const full = this._key(i)
          if (full !== null && full.startsWith(prefix)) doomed.push(full)
        }
        for (const full of doomed) this._remove(full)
        break
      }
    }
  }

  /** Drop every stored key for one instance (disconnect / eviction hygiene). */
  forget(instanceId: string): void {
    this.apply(instanceId, { op: 'clear' })
  }
}
