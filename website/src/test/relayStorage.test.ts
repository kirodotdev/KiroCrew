/**
 * Regression + contract tests for the bounded synchronous Web Storage adapter
 * the opaque Remote Crew relay pane requires (src/lib/relayStorage.ts).
 *
 * ## Why this module exists
 *
 * A relay pane is a sandboxed iframe WITHOUT `allow-same-origin`, so its
 * document runs at an opaque origin. Reading `window.localStorage` /
 * `window.sessionStorage` at an opaque origin throws `SecurityError` — and the
 * SPA touches storage during module evaluation (i18n language, ui prefs), so
 * without a synchronous shim the pane cannot boot at all. This module is that
 * shim's core plus the parent-side per-instance backing bank.
 *
 * Observable contract this owns (test-audit authoring gate):
 *   1. Behaviour: the child shim is a synchronous `Storage` (get/set/remove/
 *      clear/key/length) seeded from a snapshot, and every mutation is reported
 *      as a bounded message; reads never touch the network.
 *   2. Caps: key count, key bytes, value bytes, and total bytes are all
 *      enforced — an over-cap `setItem` throws `QuotaExceededError` (native
 *      `Storage` semantics) and does NOT mutate.
 *   3. Isolation: the parent bank stores each instance's keys under its own
 *      namespace in the parent origin's real Storage; one instance can never
 *      read or clear another's, and `clear()` for one instance leaves the other
 *      intact.
 *   4. Production seam: the parent injects `window.localStorage` as the backing
 *      store — a dependency, not a test-only hook; tests pass an in-memory
 *      `Storage` with the same interface.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  DEFAULT_RELAY_STORAGE_CAPS,
  createRelayStorage,
  memoryStorage,
  parseRelayStorageMutation,
  RelayStorageBank,
  utf8Bytes,
  type RelayStorageMutation,
} from '../lib/relayStorage'

/** A `Storage` whose every access throws — a disabled/opaque/full parent origin.
 *  The bank must degrade to dropped persistence, never let the throw escape. */
function throwingStorage(): Storage {
  const boom = () => {
    throw new DOMException('storage is not available', 'SecurityError')
  }
  return {
    get length(): number {
      return boom()
    },
    clear: boom,
    getItem: boom,
    key: boom,
    removeItem: boom,
    setItem: boom,
  } as unknown as Storage
}

describe('createRelayStorage (child shim)', () => {
  let mutations: RelayStorageMutation[]
  let storage: Storage
  beforeEach(() => {
    mutations = []
    storage = createRelayStorage({ theme: 'dark', lang: 'en' }, DEFAULT_RELAY_STORAGE_CAPS, m =>
      mutations.push(m),
    )
  })

  it('is seeded synchronously from the snapshot (get/length/key)', () => {
    expect(storage.getItem('theme')).toBe('dark')
    expect(storage.getItem('lang')).toBe('en')
    expect(storage.getItem('missing')).toBeNull()
    expect(storage.length).toBe(2)
    const keys = [storage.key(0), storage.key(1)].sort()
    expect(keys).toEqual(['lang', 'theme'])
    expect(storage.key(9)).toBeNull()
    // Seeding is not a mutation — the parent already holds these.
    expect(mutations).toEqual([])
  })

  it('setItem stores synchronously and reports one bounded set mutation', () => {
    storage.setItem('theme', 'light')
    expect(storage.getItem('theme')).toBe('light')
    storage.setItem('new', 'v')
    expect(storage.getItem('new')).toBe('v')
    expect(storage.length).toBe(3)
    expect(mutations).toEqual([
      { op: 'set', key: 'theme', value: 'light' },
      { op: 'set', key: 'new', value: 'v' },
    ])
  })

  it('coerces non-string values like native Storage', () => {
    // @ts-expect-error native Storage stringifies; the shim must match.
    storage.setItem('n', 42)
    expect(storage.getItem('n')).toBe('42')
    expect(mutations.at(-1)).toEqual({ op: 'set', key: 'n', value: '42' })
  })

  it('removeItem deletes synchronously and reports a remove mutation', () => {
    storage.removeItem('theme')
    expect(storage.getItem('theme')).toBeNull()
    expect(storage.length).toBe(1)
    expect(mutations).toEqual([{ op: 'remove', key: 'theme' }])
  })

  it('clear empties synchronously and reports a single clear mutation', () => {
    storage.clear()
    expect(storage.length).toBe(0)
    expect(storage.getItem('lang')).toBeNull()
    expect(mutations).toEqual([{ op: 'clear' }])
  })

  it('a downward update from the parent applies WITHOUT re-reporting a mutation', () => {
    // The parent pushes an authoritative value (e.g. reconciled ui-prefs). The
    // shim must apply it so a synchronous read sees it, but must NOT echo a
    // mutation back up or the two sides would ping-pong forever.
    const withUpdates = createRelayStorage({ a: '1' }, DEFAULT_RELAY_STORAGE_CAPS, m =>
      mutations.push(m),
    )
    mutations.length = 0
    withUpdates.applyDownstream({ op: 'set', key: 'a', value: '2' })
    withUpdates.applyDownstream({ op: 'set', key: 'b', value: '3' })
    withUpdates.applyDownstream({ op: 'remove', key: 'a' })
    expect(withUpdates.getItem('a')).toBeNull()
    expect(withUpdates.getItem('b')).toBe('3')
    expect(mutations).toEqual([])
  })

  describe('caps', () => {
    const tiny = { maxKeys: 2, maxKeyBytes: 8, maxValueBytes: 8, maxTotalBytes: 24 }

    it('rejects an over-length value and does not mutate', () => {
      const s = createRelayStorage({}, tiny, m => mutations.push(m))
      expect(() => s.setItem('k', 'x'.repeat(9))).toThrow(/quota/i)
      expect(s.getItem('k')).toBeNull()
      expect(mutations).toEqual([])
    })

    it('rejects an over-length key and does not mutate', () => {
      const s = createRelayStorage({}, tiny, m => mutations.push(m))
      expect(() => s.setItem('k'.repeat(9), 'v')).toThrow(/quota/i)
      expect(mutations).toEqual([])
    })

    it('rejects exceeding the key count', () => {
      const s = createRelayStorage({}, tiny, m => mutations.push(m))
      s.setItem('a', '1')
      s.setItem('b', '2')
      expect(() => s.setItem('c', '3')).toThrow(/quota/i)
      expect(s.length).toBe(2)
      // Overwriting an existing key at the cap is allowed (no new slot).
      expect(() => s.setItem('a', '9')).not.toThrow()
      expect(s.getItem('a')).toBe('9')
    })

    it('rejects exceeding the total byte budget across keys+values', () => {
      const s = createRelayStorage({}, { ...tiny, maxKeys: 10 }, m => mutations.push(m))
      s.setItem('a', 'xxxxxx') // key 1 + val 6 = 7,  total 7
      s.setItem('b', 'xxxxxx') // + 7 = 14
      s.setItem('c', 'xxxxxx') // + 7 = 21  (<= 24 ok)
      // 21 + (1 + 6) = 28 > 24 → over the total budget.
      expect(() => s.setItem('d', 'xxxxxx')).toThrow(/quota/i)
      expect(s.getItem('d')).toBeNull()
      // A value that fits the remaining 3 bytes (key 1 + val 2) is still accepted.
      expect(() => s.setItem('d', 'xx')).not.toThrow()
      expect(s.getItem('d')).toBe('xx')
    })

    it('a snapshot that already exceeds caps is truncated deterministically, never throws at seed', () => {
      // The parent bank enforces caps on write, so a seed should be within them;
      // but a shim must be defensive — seeding never throws (the pane must boot).
      const s = createRelayStorage(
        { a: 'x'.repeat(100), b: 'ok' },
        tiny,
        m => mutations.push(m),
      )
      // Whatever it kept, it must be self-consistent and not have reported a mutation.
      expect(mutations).toEqual([])
      expect(s.length).toBeLessThanOrEqual(tiny.maxKeys)
    })
  })
})

describe('RelayStorageBank (parent side)', () => {
  let backing: Storage
  let bank: RelayStorageBank
  beforeEach(() => {
    backing = memoryStorage()
    bank = new RelayStorageBank(backing, DEFAULT_RELAY_STORAGE_CAPS)
  })

  it('seeds an empty snapshot for an instance with nothing stored', () => {
    expect(bank.snapshot('cd-1')).toEqual({})
  })

  it('applies child set/remove/clear mutations and reflects them in the snapshot', () => {
    bank.apply('cd-1', { op: 'set', key: 'theme', value: 'dark' })
    bank.apply('cd-1', { op: 'set', key: 'lang', value: 'en' })
    expect(bank.snapshot('cd-1')).toEqual({ theme: 'dark', lang: 'en' })
    bank.apply('cd-1', { op: 'remove', key: 'lang' })
    expect(bank.snapshot('cd-1')).toEqual({ theme: 'dark' })
    bank.apply('cd-1', { op: 'clear' })
    expect(bank.snapshot('cd-1')).toEqual({})
  })

  it('persists across bank instances via the backing store (survives reload)', () => {
    bank.apply('cd-1', { op: 'set', key: 'theme', value: 'dark' })
    const reopened = new RelayStorageBank(backing, DEFAULT_RELAY_STORAGE_CAPS)
    expect(reopened.snapshot('cd-1')).toEqual({ theme: 'dark' })
  })

  it('isolates instances — one cannot read or clear another', () => {
    bank.apply('cd-1', { op: 'set', key: 'k', value: 'one' })
    bank.apply('cd-2', { op: 'set', key: 'k', value: 'two' })
    expect(bank.snapshot('cd-1')).toEqual({ k: 'one' })
    expect(bank.snapshot('cd-2')).toEqual({ k: 'two' })
    bank.apply('cd-1', { op: 'clear' })
    expect(bank.snapshot('cd-1')).toEqual({})
    // cd-2 is untouched by cd-1's clear.
    expect(bank.snapshot('cd-2')).toEqual({ k: 'two' })
  })

  it('enforces caps on apply — an over-cap child mutation is dropped, not stored', () => {
    const small = new RelayStorageBank(memoryStorage(), {
      maxKeys: 1,
      maxKeyBytes: 64,
      maxValueBytes: 8,
      maxTotalBytes: 64,
    })
    small.apply('cd-1', { op: 'set', key: 'a', value: 'ok' })
    small.apply('cd-1', { op: 'set', key: 'b', value: 'also' }) // exceeds maxKeys=1
    small.apply('cd-1', { op: 'set', key: 'a', value: 'x'.repeat(99) }) // exceeds value
    expect(bank.snapshot('cd-1')).toEqual({}) // different bank, unaffected
    expect(small.snapshot('cd-1')).toEqual({ a: 'ok' })
  })

  it('does not collide when an instance id contains the namespace separator', () => {
    // The namespace must be injective: an id carrying the separator must not be
    // read as "another instance's key".
    bank.apply('cd-1', { op: 'set', key: 'x', value: 'A' })
    bank.apply('cd-1::spoof', { op: 'set', key: 'x', value: 'B' })
    expect(bank.snapshot('cd-1')).toEqual({ x: 'A' })
    expect(bank.snapshot('cd-1::spoof')).toEqual({ x: 'B' })
  })

  it('enforces an AGGREGATE budget across instance namespaces, not just per-instance caps', () => {
    // Per-instance caps admit a large value each; the aggregate budget is what
    // bounds the whole relay set against the parent origin's quota. With a small
    // aggregate, a second instance's write that fits ITS own cap is still dropped
    // once the combined relay footprint would exceed the aggregate.
    const perInstance = { maxKeys: 50, maxKeyBytes: 64, maxValueBytes: 4096, maxTotalBytes: 4096 }
    const aggregate = 200 // bytes across ALL namespaces
    const store = memoryStorage()
    const b = new RelayStorageBank(store, perInstance, aggregate)
    // First instance fills most of the aggregate (within its own per-instance cap).
    b.apply('cd-1', { op: 'set', key: 'k', value: 'x'.repeat(120) })
    const afterFirst = b.snapshot('cd-1')
    expect(afterFirst.k).toBe('x'.repeat(120))
    // A second instance's write that fits its own cap but breaches the aggregate
    // is DROPPED, not stored and not thrown.
    b.apply('cd-2', { op: 'set', key: 'k', value: 'y'.repeat(120) })
    expect(b.snapshot('cd-2')).toEqual({})
    // The first instance's data is untouched; a small write that still fits the
    // aggregate is accepted, proving the budget is a live remaining-space check.
    b.apply('cd-2', { op: 'set', key: 's', value: 'z' })
    expect(b.snapshot('cd-2')).toEqual({ s: 'z' })
    expect(b.snapshot('cd-1')).toEqual({ k: 'x'.repeat(120) })
  })

  it('skips a malformed percent-escape key in snapshot and retains valid entries', () => {
    // A stale or hand-edited key whose suffix is a broken percent escape makes
    // decodeURIComponent throw. snapshot runs on the parent's iframe-seed path,
    // so it must NOT raise into render: the malformed entry is skipped and every
    // valid entry is still returned.
    const store = memoryStorage()
    const bank = new RelayStorageBank(store, DEFAULT_RELAY_STORAGE_CAPS)
    // Seed two valid entries through the bank …
    bank.apply('cd-1', { op: 'set', key: 'theme', value: 'dark' })
    bank.apply('cd-1', { op: 'set', key: 'lang', value: 'en' })
    // … and a malformed one written straight into the backing store, under this
    // instance's namespace prefix, with a suffix decodeURIComponent rejects.
    store.setItem('relay-ls:cd-1:%', 'orphan')
    let snap: Record<string, string> = {}
    expect(() => {
      snap = bank.snapshot('cd-1')
    }).not.toThrow()
    expect(snap).toEqual({ theme: 'dark', lang: 'en' })
    expect('%' in snap).toBe(false)
  })

  it('never throws when the backing Storage is unavailable/private/full', () => {
    // A disabled or opaque parent origin makes EVERY Storage access throw. The
    // bank must swallow it: snapshot reads empty, mutations drop, and the parent
    // message loop never sees an exception.
    const b = new RelayStorageBank(throwingStorage())
    expect(() => b.snapshot('cd-1')).not.toThrow()
    expect(b.snapshot('cd-1')).toEqual({})
    expect(() => b.apply('cd-1', { op: 'set', key: 'k', value: 'v' })).not.toThrow()
    expect(() => b.apply('cd-1', { op: 'remove', key: 'k' })).not.toThrow()
    expect(() => b.apply('cd-1', { op: 'clear' })).not.toThrow()
    expect(() => b.forget('cd-1')).not.toThrow()
    // Nothing persisted (the store rejected every write), but no throw escaped.
    expect(b.snapshot('cd-1')).toEqual({})
  })

  it('drops a single over-quota write without throwing, then keeps accepting fitting writes', () => {
    // A backing store that throws QuotaExceededError only on a write past a
    // threshold: the failing set is dropped silently and later fitting sets
    // still land, so one full key never poisons the bank.
    const inner = memoryStorage()
    let bytesUsed = 0
    const capped: Storage = {
      get length() {
        return inner.length
      },
      clear: () => inner.clear(),
      getItem: (k: string) => inner.getItem(k),
      key: (i: number) => inner.key(i),
      removeItem: (k: string) => {
        const v = inner.getItem(k)
        if (v !== null) bytesUsed -= v.length
        inner.removeItem(k)
      },
      setItem: (k: string, v: string) => {
        if (bytesUsed + v.length > 50) {
          throw new DOMException('quota', 'QuotaExceededError')
        }
        bytesUsed += v.length
        inner.setItem(k, v)
      },
    } as Storage
    const b = new RelayStorageBank(capped)
    b.apply('cd-1', { op: 'set', key: 'a', value: 'x'.repeat(40) })
    expect(() => b.apply('cd-1', { op: 'set', key: 'b', value: 'y'.repeat(40) })).not.toThrow()
    // The over-quota native write was dropped; the earlier fitting one survived.
    expect(b.snapshot('cd-1')).toEqual({ a: 'x'.repeat(40) })
  })

  describe('exception-safe key encoding (unmatched surrogate)', () => {
    // The confirmed encoding escape: `encodeURIComponent` throws `URIError` on a
    // lone surrogate, and the bank forms its backing key with it. A child
    // mutation carrying such a key must be dropped at the bank boundary rather
    // than raise into the parent's message listener — and a later valid mutation
    // must still apply.
    const LONE_HIGH = '\uD800' // a high surrogate with no following low surrogate

    it('drops a set with an unmatched-surrogate key without throwing or mutating', () => {
      const b = new RelayStorageBank(memoryStorage())
      // Sanity: this key really does make the native encoder throw.
      expect(() => encodeURIComponent(LONE_HIGH)).toThrow()
      expect(() => b.apply('cd-1', { op: 'set', key: LONE_HIGH, value: 'v' })).not.toThrow()
      // Nothing was stored — the malformed key did not mutate the bank.
      expect(b.snapshot('cd-1')).toEqual({})
    })

    it('drops a remove with an unmatched-surrogate key without throwing', () => {
      const b = new RelayStorageBank(memoryStorage())
      b.apply('cd-1', { op: 'set', key: 'theme', value: 'dark' })
      expect(() => b.apply('cd-1', { op: 'remove', key: LONE_HIGH })).not.toThrow()
      // The valid entry is untouched by the malformed remove.
      expect(b.snapshot('cd-1')).toEqual({ theme: 'dark' })
    })

    it('a later valid mutation still applies after a surrogate-key one is dropped', () => {
      const b = new RelayStorageBank(memoryStorage())
      expect(() => b.apply('cd-1', { op: 'set', key: LONE_HIGH, value: 'x' })).not.toThrow()
      b.apply('cd-1', { op: 'set', key: 'lang', value: 'en' })
      expect(b.snapshot('cd-1')).toEqual({ lang: 'en' })
    })

    it('tolerates an unencodable instance id without throwing', () => {
      const b = new RelayStorageBank(memoryStorage())
      expect(() => b.apply(LONE_HIGH, { op: 'set', key: 'k', value: 'v' })).not.toThrow()
      expect(b.snapshot(LONE_HIGH)).toEqual({})
    })
  })
})

describe('parseRelayStorageMutation', () => {
  // The parent message listener must parse an UNTRUSTED relay payload into the
  // exact union before handing it to the bank — a missing field, a wrong type,
  // or an unknown op is rejected (dropped, never cast).
  it('accepts each well-formed op and normalizes to the exact union', () => {
    expect(parseRelayStorageMutation({ op: 'clear' })).toEqual({ op: 'clear' })
    expect(parseRelayStorageMutation({ op: 'set', key: 'k', value: 'v' })).toEqual({
      op: 'set',
      key: 'k',
      value: 'v',
    })
    expect(parseRelayStorageMutation({ op: 'remove', key: 'k' })).toEqual({ op: 'remove', key: 'k' })
    // A well-typed but unmatched-surrogate key is a valid string and is accepted
    // here; the bank's encoding is what stays exception-safe.
    expect(parseRelayStorageMutation({ op: 'set', key: '\uD800', value: 'v' })).toEqual({
      op: 'set',
      key: '\uD800',
      value: 'v',
    })
  })

  it('rejects a non-object, a missing op, and an unknown op', () => {
    expect(parseRelayStorageMutation(null)).toBeNull()
    expect(parseRelayStorageMutation(undefined)).toBeNull()
    expect(parseRelayStorageMutation('set')).toBeNull()
    expect(parseRelayStorageMutation(42)).toBeNull()
    expect(parseRelayStorageMutation({})).toBeNull()
    expect(parseRelayStorageMutation({ op: 'nope', key: 'k', value: 'v' })).toBeNull()
  })

  it('rejects missing or wrong-typed fields per op', () => {
    // set: needs string key AND string value
    expect(parseRelayStorageMutation({ op: 'set', key: 'k' })).toBeNull()
    expect(parseRelayStorageMutation({ op: 'set', value: 'v' })).toBeNull()
    expect(parseRelayStorageMutation({ op: 'set', key: 1, value: 'v' })).toBeNull()
    expect(parseRelayStorageMutation({ op: 'set', key: 'k', value: 2 })).toBeNull()
    expect(parseRelayStorageMutation({ op: 'set', key: 'k', value: null })).toBeNull()
    // remove: needs string key
    expect(parseRelayStorageMutation({ op: 'remove' })).toBeNull()
    expect(parseRelayStorageMutation({ op: 'remove', key: 5 })).toBeNull()
  })

  it('does not carry extra fields onto the parsed union', () => {
    const parsed = parseRelayStorageMutation({ op: 'remove', key: 'k', evil: 'x' })
    expect(parsed).toEqual({ op: 'remove', key: 'k' })
    expect(parsed && 'evil' in parsed).toBe(false)
  })
})

describe('utf8Bytes', () => {
  it('counts UTF-8 bytes, not code units', () => {
    expect(utf8Bytes('abc')).toBe(3)
    expect(utf8Bytes('é')).toBe(2)
    expect(utf8Bytes('😀')).toBe(4)
  })
})
