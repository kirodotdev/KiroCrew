// Feature: chat-virtualizer -- the PER-WIDTH height-cache family is bounded.
//
// Each pane width bucket persists its own `vc_heights_<base>:w<bucket>` blob so
// a table measured at one desktop width is never reused at another (see
// HeightIndex / TranscriptScrollShell). That partition is uncapped by design in
// the WIDTH dimension -- but nothing bounded how MANY width blobs one live slot
// retained, so a slot dragged across many widths grew its family without limit
// toward the ~5 MB localStorage quota (the white-screen `storageGc` exists to
// prevent, one dimension deeper). `widthFamilyGc` adds the missing bound: keep
// the N most-recently-touched widths per slot/host base, evict the rest, never
// touch the current/warm scope, and never cap the measurable width.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { HeightCache, LS_KEY_PREFIX, TOUCHED_AT_KEY, SCHEMA_VERSION_KEY, HEIGHT_SCHEMA_VERSION } from '../hooks/virtualizer/HeightCache'
import {
  MAX_WIDTH_FAMILIES,
  parseWidthScope,
  pruneWidthFamily,
  boundWidthFamilyFor,
} from './widthFamilyGc'

const keyFor = (scope: string) => `${LS_KEY_PREFIX}${scope}`

/** Persist a width blob at `scope` with a definite `lastTouched` stamp so the
 *  recency ordering under test is deterministic (real writes stamp Date.now()).
 */
function persistAt(scope: string, touchedAt: number, height = 120): void {
  const blob: Record<string, number | string> = {
    [SCHEMA_VERSION_KEY]: HEIGHT_SCHEMA_VERSION,
    // Production stamps the recency marker as a STRING (so an older reader
    // cannot read it as a row height); mirror that shape here.
    [TOUCHED_AT_KEY]: String(touchedAt),
    'row-a': height,
  }
  localStorage.setItem(keyFor(scope), JSON.stringify(blob))
}

const familyBuckets = (base: string): number[] => {
  const out: number[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (!k || !k.startsWith(LS_KEY_PREFIX)) continue
    const p = parseWidthScope(k.slice(LS_KEY_PREFIX.length))
    if (p && p.base === base) out.push(p.bucket)
  }
  return out.sort((a, b) => a - b)
}

beforeEach(() => {
  localStorage.clear()
})

describe('parseWidthScope', () => {
  it('splits a slot/host width scope into base and bucket', () => {
    expect(parseWidthScope('chat-1-1:tables1:w1216')).toEqual({ base: 'chat-1-1:tables1', bucket: 1216 })
    expect(parseWidthScope('slot:tables1:pane:w704')).toEqual({ base: 'slot:tables1:pane', bucket: 704 })
  })

  it('returns null for a non-width key so it is left alone', () => {
    // A bare per-session key with no `:w<digits>` suffix is not a family member.
    expect(parseWidthScope('chat-1-1')).toBeNull()
    expect(parseWidthScope('chat-1-1:tables1')).toBeNull()
    // A trailing `:w` with no digits is not a bucket.
    expect(parseWidthScope('chat-1-1:tables1:w')).toBeNull()
  })

  it('anchors the bucket to the END, so a base containing :w is not mistaken', () => {
    expect(parseWidthScope('chat:w9:tables1:w1216')).toEqual({ base: 'chat:w9:tables1', bucket: 1216 })
  })
})

describe('width-family recency bound', () => {
  it('measures unbounded growth WITHOUT the policy, then bounds it WITH it', () => {
    const base = 'chat-1-1:tables1'
    // A slot dragged across many desktop widths: one blob per 16px bucket.
    const buckets = Array.from({ length: 40 }, (_, i) => 1024 + i * 16)
    buckets.forEach((w, i) => persistAt(`${base}:w${w}`, 1000 + i))
    // Growth is real and uncapped until we prune.
    expect(familyBuckets(base)).toHaveLength(40)

    // Open the most-recent width; the family is bounded to N, sparing it.
    const current = buckets[buckets.length - 1]
    const removed = boundWidthFamilyFor(`${base}:w${current}`)
    expect(removed).toBe(40 - MAX_WIDTH_FAMILIES)
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
  })

  it('keeps the MOST-RECENTLY-TOUCHED widths and evicts the least recent', () => {
    const base = 'chat-1-1:tables1'
    // Ascending recency: w1024 oldest ... w1600 newest.
    const buckets = Array.from({ length: MAX_WIDTH_FAMILIES + 3 }, (_, i) => 1024 + i * 16)
    buckets.forEach((w, i) => persistAt(`${base}:w${w}`, 1000 + i))

    // Open a fresh current width so recency alone decides the survivors.
    persistAt(`${base}:w2000`, 5000)
    boundWidthFamilyFor(`${base}:w2000`)

    const survivors = familyBuckets(base)
    expect(survivors).toHaveLength(MAX_WIDTH_FAMILIES)
    // The current width is always kept.
    expect(survivors).toContain(2000)
    // The three oldest are gone; the newest of the original set remain.
    expect(survivors).not.toContain(1024)
    expect(survivors).not.toContain(1040)
    expect(survivors).not.toContain(1056)
    expect(survivors).toContain(buckets[buckets.length - 1])
  })

  it('spares the current scope even when its own blob is the OLDEST (warm return)', () => {
    const base = 'chat-1-1:tables1'
    // The warm width was measured long ago and is the stalest by timestamp...
    persistAt(`${base}:w1216`, 1)
    // ...while many newer widths pile up. Start well above 1216 so none of these
    // buckets collide with the warm scope and silently overwrite its old stamp
    // with a fresh one -- the warm scope must stay the genuinely-oldest member,
    // or this exercises recency instead of the keep-scope pin.
    for (let i = 0; i < MAX_WIDTH_FAMILIES + 5; i++) persistAt(`${base}:w${2048 + i * 16}`, 9000 + i)

    // Returning to the warm width re-opens it: it must survive the prune even
    // though its stamp is the oldest, because it is the scope being opened.
    boundWidthFamilyFor(`${base}:w1216`)
    expect(familyBuckets(base)).toContain(1216)
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
  })

  it('does nothing while the family is at or under the bound', () => {
    const base = 'chat-1-1:tables1'
    for (let i = 0; i < MAX_WIDTH_FAMILIES; i++) persistAt(`${base}:w${1024 + i * 16}`, 1000 + i)
    expect(boundWidthFamilyFor(`${base}:w1024`)).toBe(0)
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
  })

  it('does NOT enumerate storage when the opened scope has no persisted blob', () => {
    // A scope that was opened but has not written a height blob yet cannot have
    // added anything to the family, so the policy must take an O(1) existence
    // check and never run the `key()` scan. This is the render-phase common case
    // (a session with no measured rows, e.g. no pull requests) and enumerating
    // there is the wasted-work / unexpected-storage-touch regression.
    const keySpy = vi.spyOn(Storage.prototype, 'key')
    try {
      expect(boundWidthFamilyFor('chat-9-9:tables1:w1216')).toBe(0)
      expect(keySpy).not.toHaveBeenCalled()
    } finally {
      keySpy.mockRestore()
    }
  })

  it('enumerates only once the opened scope IS persisted (warm return over the bound)', () => {
    const base = 'chat-1-1:tables1'
    for (let i = 0; i < MAX_WIDTH_FAMILIES + 3; i++) persistAt(`${base}:w${1024 + i * 16}`, 1000 + i)
    const current = `${base}:w1024` // oldest, but it exists, so it is spared and the scan runs
    const keySpy = vi.spyOn(Storage.prototype, 'key')
    try {
      const removed = boundWidthFamilyFor(current)
      expect(keySpy).toHaveBeenCalled()
      expect(removed).toBe(3)
    } finally {
      keySpy.mockRestore()
    }
    expect(familyBuckets(base)).toContain(1024)
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
  })

  it('bounds each slot/host base INDEPENDENTLY, never crossing bases', () => {
    const a = 'chat-1-1:tables1'
    const b = 'chat-1-1:tables1:pane'
    const other = 'chat-2-2:tables1'
    for (let i = 0; i < MAX_WIDTH_FAMILIES + 4; i++) persistAt(`${a}:w${1024 + i * 16}`, 1000 + i)
    for (let i = 0; i < 3; i++) persistAt(`${b}:w${1024 + i * 16}`, 2000 + i)
    for (let i = 0; i < 3; i++) persistAt(`${other}:w${1024 + i * 16}`, 3000 + i)

    boundWidthFamilyFor(`${a}:w${1024 + (MAX_WIDTH_FAMILIES + 3) * 16}`)

    expect(familyBuckets(a)).toHaveLength(MAX_WIDTH_FAMILIES)
    // The host-scoped and the sibling slot's families are untouched.
    expect(familyBuckets(b)).toHaveLength(3)
    expect(familyBuckets(other)).toHaveLength(3)
  })

  it('leaves a non-width vc_heights_ key strictly alone', () => {
    // No `:w<bucket>` suffix -> not a family member -> never a prune candidate.
    localStorage.setItem(keyFor('chat-1-1'), '{}')
    localStorage.setItem(keyFor('chat-1-1:tables1'), '{}')
    const base = 'chat-1-1:tables1'
    for (let i = 0; i < MAX_WIDTH_FAMILIES + 2; i++) persistAt(`${base}:w${1024 + i * 16}`, 1000 + i)

    boundWidthFamilyFor(`${base}:w${1024 + (MAX_WIDTH_FAMILIES + 1) * 16}`)

    expect(localStorage.getItem(keyFor('chat-1-1'))).toBe('{}')
    expect(localStorage.getItem(keyFor('chat-1-1:tables1'))).toBe('{}')
  })

  it('treats an unstamped (pre-policy) blob as oldest', () => {
    const base = 'chat-1-1:tables1'
    // One blob with no TOUCHED_AT stamp, N stamped newer ones.
    localStorage.setItem(keyFor(`${base}:w1024`), JSON.stringify({ [SCHEMA_VERSION_KEY]: HEIGHT_SCHEMA_VERSION, 'row-a': 100 }))
    for (let i = 0; i < MAX_WIDTH_FAMILIES; i++) persistAt(`${base}:w${1200 + i * 16}`, 9000 + i)

    boundWidthFamilyFor(`${base}:w${1200}`)

    // The unstamped one sorts oldest and is the first evicted.
    expect(familyBuckets(base)).not.toContain(1024)
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
  })

  it('pruneWidthFamily is a no-op when storage.key throws mid-enumeration', () => {
    // Seed the opened scope's blob so the existence gate passes and the key()
    // scan is actually reached -- that is the path under test.
    persistAt('chat-1-1:tables1:w1216', 5000)
    const spy = vi.spyOn(Storage.prototype, 'key').mockImplementation(() => { throw new Error('boom') })
    try {
      expect(pruneWidthFamily('chat-1-1:tables1', 1216)).toBe(0)
    } finally {
      spy.mockRestore()
    }
  })

  it('pruneWidthFamily is a no-op when storage.length throws', () => {
    persistAt('chat-1-1:tables1:w1216', 5000)
    const spy = vi.spyOn(Storage.prototype, 'length', 'get').mockImplementation(() => { throw new Error('boom') })
    try {
      expect(pruneWidthFamily('chat-1-1:tables1', 1216)).toBe(0)
    } finally {
      spy.mockRestore()
    }
  })

  it('pruneWidthFamily is a no-op when the existence check (getItem) throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('boom') })
    try {
      expect(pruneWidthFamily('chat-1-1:tables1', 1216)).toBe(0)
    } finally {
      spy.mockRestore()
    }
  })

  it('does not throw into render when a storage policy DENIES access on acquisition', () => {
    // A denied-storage browser policy makes the `window.localStorage` accessor
    // itself throw SecurityError -- even reading `typeof localStorage` invokes
    // it. The entry point runs in `useHeightOwner`'s render phase with no local
    // try/catch, so it must swallow this at acquisition and return 0, never let
    // the throw escape and crash the chat surface.
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage')
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new DOMException('denied', 'SecurityError')
      },
    })
    try {
      expect(() => boundWidthFamilyFor('chat-1-1:tables1:w1216')).not.toThrow()
      expect(boundWidthFamilyFor('chat-1-1:tables1:w1216')).toBe(0)
      expect(() => pruneWidthFamily('chat-1-1:tables1', 1216)).not.toThrow()
      expect(pruneWidthFamily('chat-1-1:tables1', 1216)).toBe(0)
    } finally {
      if (original) Object.defineProperty(window, 'localStorage', original)
    }
  })
})

describe('HeightCache lastTouched stamp', () => {
  it('stamps a write and never surfaces the stamp as a row height', () => {
    const before = Date.now()
    const c = new HeightCache('chat-1-1:tables1:w1216')
    c.set('row-a', 300)
    c.flush()
    const blob = JSON.parse(localStorage.getItem(keyFor('chat-1-1:tables1:w1216'))!)
    // The stamp is a STRING, so a reader from a build before the stamp existed
    // (same schema version 'h1') rejects it on its numeric row-height gate
    // rather than loading a ~1.7e12 epoch value as a 1.7-trillion-pixel row.
    expect(typeof blob[TOUCHED_AT_KEY]).toBe('string')
    expect(Number(blob[TOUCHED_AT_KEY])).toBeGreaterThanOrEqual(before)
    // Read back: the stamp is not a measurement.
    const reopened = new HeightCache('chat-1-1:tables1:w1216')
    expect(reopened.peek('row-a')).toBe(300)
    expect(reopened.peek(TOUCHED_AT_KEY)).toBeUndefined()
    expect(reopened.size()).toBe(1)
  })

  it('an older reader (numeric row-height gate) does NOT load the string stamp as a height', () => {
    // Reproduce the exact load loop a pre-stamp build runs: skip the schema
    // version, admit any finite number > 0 as a row height. With a numeric
    // stamp this would admit a ~1.7e12 epoch value as a giant row (corruption);
    // the string stamp is skipped by the `typeof v === 'number'` gate.
    const c = new HeightCache('chat-1-1:tables1:w1216')
    c.set('row-a', 300)
    c.flush()
    const persisted = JSON.parse(localStorage.getItem(keyFor('chat-1-1:tables1:w1216'))!) as Record<string, unknown>
    const admitted: Record<string, number> = {}
    for (const k of Object.keys(persisted)) {
      if (k === SCHEMA_VERSION_KEY) continue // the ONLY key an old reader skipped
      const v = persisted[k]
      if (typeof v === 'number' && Number.isFinite(v) && v > 0) admitted[k] = v
    }
    expect(admitted).toEqual({ 'row-a': 300 })
    expect(TOUCHED_AT_KEY in admitted).toBe(false)
  })
})

describe('width-family bound fires on the FIRST flush of a NEW width', () => {
  // The bound must trigger when a brand-new width's blob is first PERSISTED
  // (absent -> present), not when its HeightCache is CONSTRUCTED. Construction
  // only loads, and a new width has no blob to load -- the write happens later
  // when a measured row flushes. These tests drive a REAL HeightCache end to
  // end (open -> set -> flush), wiring `onFirstPersist` exactly as
  // measurement.ts does, so they fail if the bound is attached to construction.
  const base = 'chat-1-1:tables1'
  const openWidth = (bucket: number): HeightCache =>
    new HeightCache(`${base}:w${bucket}`, {
      onFirstPersist: () => boundWidthFamilyFor(`${base}:w${bucket}`),
    })

  it('a pane dragged across N NEW widths bounds the family as each new width persists', () => {
    // No blob is pre-seeded: every width is created the way production creates
    // one -- open the scope, measure a row, flush. A broken construction-time
    // trigger would never prune here, because at construction the width's own
    // blob does not exist yet (keepExists is false).
    //
    // Each flush stamps `lastTouched` from Date.now(). This loop runs in well
    // under a millisecond, so on the real clock most widths would share ONE
    // stamp and recency could not order them: the sort then falls back to
    // storage enumeration order and evicts an arbitrary tied member (seen in
    // CI as the OLDEST width surviving). Drive the clock so every flush lands
    // one ms after the previous one and the recency order is the write order.
    let now = 1_000_000
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => ++now)
    try {
      const widths = Array.from({ length: MAX_WIDTH_FAMILIES + 12 }, (_, i) => 1024 + i * 16)
      for (const w of widths) {
        const cache = openWidth(w)
        cache.set('row-a', 300)
        cache.flush() // first persist of THIS width -> onFirstPersist -> bound
      }
      // The family never exceeded the bound: each new width's first flush evicted
      // the least-recently-used older width, so growth stayed capped throughout.
      expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
      // The most-recently-written widths are the survivors.
      const survivors = familyBuckets(base)
      expect(survivors).toContain(widths[widths.length - 1])
      expect(survivors).not.toContain(widths[0])
    } finally {
      clock.mockRestore()
    }
  })

  it('spares the just-written current width and keeps it after its own first flush', () => {
    // Pre-fill the family to the bound with older widths via real writes.
    for (let i = 0; i < MAX_WIDTH_FAMILIES; i++) {
      const cache = openWidth(2048 + i * 16)
      cache.set('row-a', 120)
      cache.flush()
    }
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
    // Now a brand-new width is dragged to and measured. Its first flush bounds
    // the family and MUST keep the width just written (the current one).
    const current = 9000
    const cache = openWidth(current)
    cache.set('row-a', 300)
    cache.flush()
    expect(familyBuckets(base)).toHaveLength(MAX_WIDTH_FAMILIES)
    expect(familyBuckets(base)).toContain(current)
  })

  it('does NOT re-fire the bound on a warm REOPEN of an existing width', () => {
    // Create a width and persist it.
    const first = openWidth(1216)
    first.set('row-a', 300)
    first.flush()
    // Reopen the same width (warm return). load() sees the existing blob, so a
    // subsequent flush is NOT an absent -> present transition and must not fire
    // onFirstPersist again. Spy proves the bound is not re-entered on reopen.
    const boundSpy = vi.fn()
    const reopened = new HeightCache(`${base}:w1216`, { onFirstPersist: boundSpy })
    reopened.set('row-b', 150)
    reopened.flush()
    expect(boundSpy).not.toHaveBeenCalled()
  })
})
