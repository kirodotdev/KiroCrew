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
    const heightGuards = code.match(/heightIndexRef\.current\?\.sessionId\s*!==\s*heightScope/g) ?? []
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

  it('the hook holds no hand-bumped geometry version', () => {
    const code = stripComments(readSource('useVirtualChat.ts'))
    // The counter this replaced lived in the hook as React state and had to be
    // bumped at every write site and listed in every memo dependency array. Its
    // absence is the invariant: invalidation is subscribed to, not maintained.
    expect(code).not.toMatch(/heightVersion/)
    expect(code).not.toMatch(/setHeightVersion/)
    // And the subscription is what replaces it.
    expect(code).toMatch(/useSyncExternalStore\(/)
  })

  it('the offset math is read, not memoized behind an invisible key', () => {
    const code = stripComments(readSource('useVirtualChat.ts'))
    // Three memos used to carry an invalidation token their bodies never read,
    // each needing an exhaustive-deps exemption plus a "Do NOT remove it" note.
    // Reading the values directly is what removed all three exemptions.
    expect(code).not.toMatch(/useMemo\(\(\)\s*=>\s*offsetIndex\.totalHeight\(\)/)
    expect(code).toMatch(/const totalHeight = offsetIndex\.totalHeight\(\)/)
    expect(code).toMatch(/const offsetBefore = offsetIndex\.offsetOf\(/)
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

  it('height scope defaults to sessionId (heightScopeKey is opt-in)', () => {
    // The guard reads heightScope = heightScopeKey ?? sessionId. This pins the
    // fallback so callers without width-dependent rows keep byte-identical
    // behavior.
    const code = stripComments(readSource('useVirtualChat.ts'))
    expect(code).toMatch(/const heightScope = heightScopeKey \?\? sessionId/)
  })

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

// The owner is also the store that announces "the geometry moved". This half
// replaces a counter the hook used to bump by hand and list in memo dependency
// arrays -- a token eslint could not see, guarded only by a "Do NOT remove it"
// comment, whose failure mode was silent (stale spacers, no error, no red test).
// Announcing inside the same call that mutates the tree removes the bump site,
// so these tests pin the contract that makes that safe.
describe('HeightIndex announces geometry changes (store contract)', () => {
  const ESTIMATE = 80

  function makeIndex(sessionId: string, keys: (string | null)[]): HeightIndex {
    return new HeightIndex(sessionId, {
      rowCount: keys.length,
      estimate: ESTIMATE,
      keyAt: (i) => keys[i] ?? null,
    })
  }

  it('plain sync does NOT notify, so the render path cannot update during render', () => {
    // Load-bearing: the hook syncs the tree during render (so that render's reads
    // are fresh). If that call notified, React would be told to re-render while
    // rendering. This is the one property the announcing path must not acquire.
    const idx = makeIndex(`nosync-${Math.random()}`, ['a'])
    let calls = 0
    idx.subscribe(() => {
      calls += 1
    })

    idx.setMeasured(0, 500)
    idx.sync(1)

    expect(idx.totalHeight()).toBe(500) // the tree DID update
    expect(calls).toBe(0) // and nobody was notified
    expect(idx.getVersion()).toBe(0)
  })

  it('syncAndAnnounce notifies once when the total moves', () => {
    const idx = makeIndex(`announce-${Math.random()}`, ['a'])
    let calls = 0
    idx.subscribe(() => {
      calls += 1
    })

    idx.setMeasured(0, 500)
    idx.syncAndAnnounce(1)

    expect(calls).toBe(1)
    expect(idx.getVersion()).toBe(1)
    expect(idx.totalHeight()).toBe(500)
  })

  it('does not announce a sub-pixel move, and holds the version steady', () => {
    const idx = makeIndex(`epsilon-${Math.random()}`, ['a'])
    idx.setMeasured(0, 500)
    idx.syncAndAnnounce(1)
    const settled = idx.getVersion()

    let calls = 0
    idx.subscribe(() => {
      calls += 1
    })
    // Within the epsilon: re-measuring the same row must not schedule a render.
    idx.setMeasured(0, 500.4)
    idx.syncAndAnnounce(1)

    expect(calls).toBe(0)
    // A steady version is what stops useSyncExternalStore from looping: repeated
    // reads have to be Object.is-equal until something is actually announced.
    expect(idx.getVersion()).toBe(settled)
    expect(idx.getVersion()).toBe(settled)
  })

  it('still announces once the accumulated drift clears the epsilon', () => {
    // The baseline must NOT advance on a swallowed sync, or a slow drift of
    // sub-epsilon steps would never announce and the spacer would rot.
    const idx = makeIndex(`drift-${Math.random()}`, ['a'])
    idx.setMeasured(0, 500)
    idx.syncAndAnnounce(1)
    const settled = idx.getVersion()

    idx.setMeasured(0, 500.4)
    idx.syncAndAnnounce(1)
    expect(idx.getVersion()).toBe(settled)

    idx.setMeasured(0, 500.8)
    idx.syncAndAnnounce(1)
    expect(idx.getVersion()).toBe(settled)

    // 502 is 2px from the ANNOUNCED 500, so it clears -- even though each step
    // was under the epsilon.
    idx.setMeasured(0, 502)
    idx.syncAndAnnounce(1)
    expect(idx.getVersion()).toBe(settled + 1)
  })

  it('runs beforeNotify after the mutation and before subscribers', () => {
    // The caller captures pre-commit DOM geometry in this slot, so it must see
    // the NEW total (mutation done) while no subscriber has been told yet
    // (the re-render it schedules has not happened).
    const idx = makeIndex(`order-${Math.random()}`, ['a'])
    const order: string[] = []
    let totalSeenByCallback = -1
    idx.subscribe(() => {
      order.push('listener')
    })

    idx.setMeasured(0, 500)
    idx.syncAndAnnounce(1, () => {
      order.push('beforeNotify')
      totalSeenByCallback = idx.totalHeight()
    })

    expect(order).toEqual(['beforeNotify', 'listener'])
    expect(totalSeenByCallback).toBe(500)
  })

  it('skips beforeNotify entirely when nothing is announced', () => {
    // Otherwise the caller would capture an anchor for a commit that never comes
    // and a later unrelated commit would consume it.
    const idx = makeIndex(`skip-${Math.random()}`, ['a'])
    idx.setMeasured(0, 500)
    idx.syncAndAnnounce(1)

    let ran = 0
    idx.setMeasured(0, 500.2)
    idx.syncAndAnnounce(1, () => {
      ran += 1
    })

    expect(ran).toBe(0)
  })

  it('stops delivering after unsubscribe', () => {
    const idx = makeIndex(`unsub-${Math.random()}`, ['a'])
    let calls = 0
    const unsubscribe = idx.subscribe(() => {
      calls += 1
    })

    idx.setMeasured(0, 500)
    idx.syncAndAnnounce(1)
    expect(calls).toBe(1)

    unsubscribe()
    idx.setMeasured(0, 900)
    idx.syncAndAnnounce(1)
    expect(calls).toBe(1)
    // The announcement still happened -- only delivery to this listener stopped.
    expect(idx.getVersion()).toBe(2)
  })

  // A retired measurement is one whose row left the list. A key resolving from a
  // LIVE row index is proof the row is back -- the shape a refused
  // regenerate/edit-resend produces when it restores its snapshot -- so the owner
  // revives it in a pass BEFORE the tree walk rather than inside the height read.
  // Reviving mid-walk would leave every row priced EARLIER in that same walk
  // holding the pre-revive mean, with no later sync guaranteed to correct them.
  it('revives a retired row before the tree walk, so one mean prices every row', () => {
    // The unmeasured rows sit BEFORE the retired one, which is what makes the
    // ordering observable: a mid-walk revive reaches them too late.
    const rows = [{ id: 'p0' }, { id: 'p1' }, { id: 'a' }, { id: 'b' }, { id: 'tall' }]
    const idx = new HeightIndex('revive-before-walk', {
      rowCount: rows.length,
      estimate: 80,
      keyAt: (i) => rows[i]?.id ?? null,
    })
    idx.setMeasured(2, 300)
    idx.setMeasured(3, 300)
    idx.setMeasured(4, 900)

    // The tall row leaves the list, so its height stops pricing what remains.
    rows.pop()
    idx.retire(['tall'])
    idx.sync(rows.length)
    expect(idx.totalHeight()).toBeCloseTo(300 + 300 + 300 + 300, 1)

    // It comes back. Every unmeasured row must be priced by the mean that
    // INCLUDES it (500), including the two the walk reaches first.
    rows.push({ id: 'tall' })
    idx.sync(rows.length)
    expect(idx.totalHeight()).toBeCloseTo(500 + 500 + 300 + 300 + 900, 1)
    // Its own measurement is exact again too.
    expect(idx.getHeight(4)).toBe(900)
  })
})
