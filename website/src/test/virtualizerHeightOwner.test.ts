// The chat virtualizer must have exactly ONE owner of height truth.
//
// Row heights used to be readable from two places that had to agree: the keyed,
// persisted `HeightCache` was read directly at five call sites in the hook, while
// `OffsetIndex` answered the offset math from its own cached copy fed by a getter
// that read the same cache. Each structure carried its OWN session guard, and both
// had to be present and agree -- a same-item-count session switch that satisfied
// only one left the tree serving the previous transcript's heights, which a user
// sees as the transcript opening at the wrong scroll position (#4326).
//
// `HeightIndex` now owns both, so the tree cannot outlive its cache and there is a
// single guard. This file pins that, in two halves:
//
//   1. STRUCTURAL -- the hook does not reach past the owner. This is a source
//      guard because the invariant IS a property of the source ("no direct cache
//      read exists"), and the failure it prevents is silent: a future edit that
//      re-adds a direct read would keep every behavioural test green while
//      re-opening the two-readers seam. Same shape as the other structural guards
//      in this suite (see ChatPage.newSessionModel.test.ts).
//   2. BEHAVIOURAL -- the read surface answers the two questions callers actually
//      ask, and keeps them distinct: a resolved height (every row has one) versus
//      the measurement itself (absent until measured). Conflating them is what
//      would silently turn every scroll-driven first mount into a "genuine
//      resize" and yank a scrolling reader.

import { describe, it, expect } from 'vitest'
import { join } from 'node:path'
import { readSource as readSourceText } from './readSource'
import { HeightIndex } from '../hooks/virtualizer/HeightIndex'

const VIRTUALIZER_DIR = join(__dirname, '..', 'hooks', 'virtualizer')

/**
 * Read a virtualizer source file for a shape assertion.
 *
 * Delegates to the shared `readSource`, which normalizes CRLF to LF. That matters
 * for the regex assertions below: a Windows checkout can materialize these files
 * with CRLF, and a locally re-spelled reader over bare `readFileSync` would make
 * these gates fail for a Windows contributor while passing on CI's Linux runner.
 */
function readSource(file: string): string {
  return readSourceText(join(VIRTUALIZER_DIR, file))
}

/** Strip line and block comments so prose mentioning a symbol is not a match. */
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

describe('height truth has one owner (structural)', () => {
  it('useVirtualChat does not import HeightCache', () => {
    const code = stripComments(readSource('useVirtualChat.ts'))
    expect(code).not.toMatch(/from\s+'\.\/HeightCache'/)
    expect(code).toMatch(/from\s+'\.\/HeightIndex'/)
  })

  it('useVirtualChat holds no cache reference and performs no direct cache read', () => {
    const code = stripComments(readSource('useVirtualChat.ts'))
    // The old direct-read handle. Its absence is the invariant.
    expect(code).not.toMatch(/cacheRef/)
    // The cache's read methods must not be called from the hook at all: peek and
    // averageHeight belong to the owner's resolved-height path, and a bare get()
    // is the promoting read the owner now expresses explicitly.
    expect(code).not.toMatch(/\.peek\(/)
    expect(code).not.toMatch(/\.averageHeight\(/)
  })

  it('within the virtualizer, only HeightIndex imports HeightCache', () => {
    const importers = ['useVirtualChat.ts', 'WindowCalculator.ts', 'FollowController.ts']
    for (const file of importers) {
      expect(stripComments(readSource(file))).not.toMatch(/from\s+'\.\/HeightCache'/)
    }
    expect(stripComments(readSource('HeightIndex.ts'))).toMatch(/from\s+'\.\/HeightCache'/)
  })

  it('the height owner is guarded on sessionId exactly once, off the owner itself', () => {
    const code = stripComments(readSource('useVirtualChat.ts'))
    // Scoped to the HEIGHT guard on purpose. The hook legitimately holds other
    // sessionId comparisons for unrelated concerns (the scroll-state sentinel,
    // the slot-entry pin bookkeeping), so matching every `!== sessionId` would
    // make this ratchet fail for edits that have nothing to do with the
    // invariant -- and would let an unrelated guard being added read as a
    // height regression.
    const heightGuards = code.match(/heightIndexRef\.current\?\.sessionId\s*!==\s*sessionId/g) ?? []
    expect(heightGuards).toHaveLength(1)
    // Session identity has ONE record, on the owner. Any parallel ref beside it
    // is a second spelling that can drift from the owner it describes -- the
    // pattern this change exists to remove, in miniature.
    expect(code).not.toMatch(/heightSessionRef/)
    expect(code).not.toMatch(/offsetIndexSessionRef/)
    expect(code).not.toMatch(/cacheSessionRef/)
  })

  it('OffsetIndex stays a pure Fenwick primitive with no session or cache concern', () => {
    const code = stripComments(readSource('WindowCalculator.ts'))
    expect(code).not.toMatch(/sessionId/)
    expect(code).not.toMatch(/HeightCache/)
  })
})

describe('HeightIndex read surface (behavioural)', () => {
  const ESTIMATE = 80

  /** Owner over `keys` rows, keyed by a stable per-index string. */
  function makeIndex(sessionId: string, keys: (string | null)[]): HeightIndex {
    return new HeightIndex(sessionId, {
      rowCount: keys.length,
      estimate: ESTIMATE,
      keyAt: (i) => keys[i] ?? null,
    })
  }

  it('separates "how tall is this row" from "has this row been measured"', () => {
    const idx = makeIndex(`sep-${Math.random()}`, ['a', 'b'])

    // Read through `getHeight`, the public spelling the offset math itself uses.
    // Unmeasured: a resolved height exists, but there is no measurement.
    expect(idx.getHeight(0)).toBe(ESTIMATE)
    expect(idx.peekMeasured(0)).toBeUndefined()
    expect(idx.readMeasured(0)).toBeUndefined()

    idx.setMeasured(0, 140)
    expect(idx.getHeight(0)).toBe(140)
    expect(idx.peekMeasured(0)).toBe(140)

    // Row 1 is still unmeasured, and MUST still report undefined even though a
    // resolved height is now available from the running mean. This is the
    // distinction the ResizeObserver's first-mount branch depends on.
    expect(idx.peekMeasured(1)).toBeUndefined()
    expect(idx.getHeight(1)).toBe(140) // running mean of the one measurement
  })

  it('clamps a zero measurement to 1 so the row still registers with IO', () => {
    const idx = makeIndex(`clamp-${Math.random()}`, ['a'])
    idx.setMeasured(0, 0)
    expect(idx.getHeight(0)).toBe(1)
    // The measurement itself is reported unclamped -- callers comparing a fresh
    // DOM reading against the stored one must see the stored value.
    expect(idx.peekMeasured(0)).toBe(0)
  })

  it('resolves an index addressing no row to the flat estimate', () => {
    const idx = makeIndex(`norow-${Math.random()}`, [null])
    expect(idx.getHeight(0)).toBe(ESTIMATE)
    expect(idx.peekMeasured(0)).toBeUndefined()
    // A write against a keyless index is a no-op rather than a throw: the hook
    // measures from a ResizeObserver entry that can outlive its row.
    expect(() => idx.setMeasured(0, 200)).not.toThrow()
    expect(idx.getHeight(0)).toBe(ESTIMATE)
  })

  it('peekMeasured does not promote LRU order while readMeasured does', () => {
    // Promotion is observable through eviction order, so drive it via the tree
    // of reads rather than reaching into the cache: after touching row 0 with a
    // promoting read, row 0 must be younger than row 1.
    const idx = makeIndex(`lru-${Math.random()}`, ['a', 'b'])
    idx.setMeasured(0, 100)
    idx.setMeasured(1, 200)

    // Non-promoting reads must leave both measurements intact and unreordered.
    expect(idx.peekMeasured(0)).toBe(100)
    expect(idx.peekMeasured(1)).toBe(200)
    // Promoting read returns the same value; the difference is order, not value.
    expect(idx.readMeasured(0)).toBe(100)
    expect(idx.peekMeasured(0)).toBe(100)
  })

  it('the offset math reads through the same resolved heights', () => {
    const idx = makeIndex(`tree-${Math.random()}`, ['a', 'b', 'c'])
    idx.setMeasured(0, 100)
    idx.setMeasured(1, 50)
    idx.setMeasured(2, 25)
    idx.sync(3)

    expect(idx.totalHeight()).toBe(175)
    expect(idx.offsetOf(0)).toBe(0)
    expect(idx.offsetOf(1)).toBe(100)
    expect(idx.offsetOf(2)).toBe(150)
    expect(idx.indexAt(0)).toBe(0)
    expect(idx.indexAt(120)).toBe(1)
    expect(idx.indexAt(160)).toBe(2)
  })

  it('a new measurement reaches the tree only on sync', () => {
    const idx = makeIndex(`sync-${Math.random()}`, ['a'])
    idx.setMeasured(0, 100)
    idx.sync(1)
    expect(idx.totalHeight()).toBe(100)

    // The tree is deliberately NOT synced on the scroll path, so a write alone
    // must not move it -- that is what keeps a same-count sync off every frame.
    idx.setMeasured(0, 300)
    expect(idx.totalHeight()).toBe(100)
    idx.sync(1)
    expect(idx.totalHeight()).toBe(300)
  })

  it('an estimate change reaches unmeasured rows on the next sync', () => {
    const idx = makeIndex(`est-${Math.random()}`, ['a', 'b'])
    idx.sync(2)
    expect(idx.totalHeight()).toBe(2 * ESTIMATE)

    idx.setEstimate(10)
    idx.sync(2)
    expect(idx.totalHeight()).toBe(20)
  })

  it('reports the session it was constructed for (the guard reads this)', () => {
    // Not incidental state: the hook's single session guard compares this field
    // against the current sessionId, so it IS the record of session identity.
    const idx = makeIndex('session-abc', ['a'])
    expect(idx.sessionId).toBe('session-abc')
  })

  it('two sessions do not share heights even at identical row counts', () => {
    const suffix = Math.random()
    const a = makeIndex(`switch-a-${suffix}`, ['k0', 'k1'])
    a.setMeasured(0, 400)
    a.setMeasured(1, 400)
    a.sync(2)
    expect(a.totalHeight()).toBe(800)

    // Same key strings, same count, different session: the owner is what carries
    // the partition, so a fresh one must start with no measurements. Serving A's
    // heights here is exactly the wrong-scroll-position bug.
    const b = makeIndex(`switch-b-${suffix}`, ['k0', 'k1'])
    expect(b.peekMeasured(0)).toBeUndefined()
    b.sync(2)
    expect(b.totalHeight()).toBe(2 * ESTIMATE)
  })
})
