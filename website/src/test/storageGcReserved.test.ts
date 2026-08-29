/**
 * The startup localStorage sweep must not eat the gallery's height cache.
 *
 * The virtualizer partitions measured heights by `sessionId`, so a caller that
 * is not a chat session still has to name a partition. The sweep reads whatever
 * follows `vc_heights_` as a session id and deletes it when no live session
 * matches — which would wipe the gallery's partition on every boot. That failure
 * is silent: the cache still works within one page load, so heights simply never
 * stay warm, and the symptom (cards correcting their height on a first scroll,
 * every single visit) looks like the cache not working rather than like a sweep.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { gcOrphanedStorage } from '../utils/storageGc'
import { ANCHOR_KEY_PREFIX } from '../hooks/virtualizer/ScrollAnchorCache'

const OWNER_ENTRY_PREFIX = 'mc-storage-gc-owner:'

/** A slot list, dated so instances are distinguishable. */
const live = (...keys: string[]): { key: string; created?: string }[] =>
  keys.map(key => ({ key, created: '2026-06-06T00:00:00Z' }))

/** Seed a prior-instance stamp for every EXISTING key of each id, so supersession is
 *  proven PER KEY. A per-id stamp would let one family vouch for its siblings. */
const seedOwners = (ids: string[], stamp = '2026-01-01T00:00:00Z'): void => {
  const matched: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (!k || k.startsWith(OWNER_ENTRY_PREFIX)) continue
    if (ids.some(id => k.endsWith(id) || k.includes(`${id}:`))) matched.push(k)
  }
  for (const k of matched) {
    localStorage.setItem(OWNER_ENTRY_PREFIX + k, JSON.stringify({ stamp, seenAt: 0 }))
  }
}

const sweepSuperseded = (supersededIds: string[], alsoLive: string[] = []): number => {
  seedOwners(supersededIds)
  return gcOrphanedStorage(live(...supersededIds, ...alsoLive))
}

/** Every surviving key except the sweep's own instance ledger, which is bookkeeping
 *  rather than session state and is meant to outlive the keys it dates. Enumerated
 *  through the Storage API, since `Object.keys` on it exposes jsdom internals. */
const sessionKeys = (): string[] => {
  const out: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && !k.startsWith(OWNER_ENTRY_PREFIX)) out.push(k)
  }
  return out
}


const HEIGHTS = 'vc_heights_'
/** Imported, not restated: the anchor key shape carries a format version, and a
 *  hardcoded copy here silently stops describing the keys the sweep owns the
 *  moment that version is bumped — which reads as the sweep having a hole. */
const ANCHOR = ANCHOR_KEY_PREFIX
/** Must stay in step with `ARTIFACT_HEIGHT_NS` in `pages/ArtifactsPage.tsx`. */
const GALLERY = 'artifacts-gallery'

describe('gcOrphanedStorage', () => {
  beforeEach(() => localStorage.clear())

  it('keeps the gallery height partition, which is not a session', () => {
    localStorage.setItem(HEIGHTS + GALLERY, '[["thumb900:1",220]]')
    localStorage.setItem(HEIGHTS + 'dead-session', '[["x",100]]')
    localStorage.setItem(HEIGHTS + 'live-session', '[["y",100]]')

    const removed = sweepSuperseded(['dead-session'], ['live-session'])

    expect(localStorage.getItem(HEIGHTS + GALLERY)).toBeTruthy()
    expect(localStorage.getItem(HEIGHTS + 'live-session')).toBeTruthy()
    expect(localStorage.getItem(HEIGHTS + 'dead-session')).toBeNull()
    expect(removed).toBe(1)
  })

  it('still collects a dead session under every session-scoped prefix', async () => {
    // The exemption must be narrow: it protects one reserved name, not the
    // sweep's whole reason for existing (an unbounded localStorage overflows the
    // origin quota and white-screens the app).
    localStorage.setItem(HEIGHTS + 'gone', '[]')
    localStorage.setItem(ANCHOR + 'gone', '{}')
    localStorage.setItem('mc-panel-tabs:gone', '[]')

    const removed = sweepSuperseded(['gone'], ['alive'])

    expect(removed).toBe(3)
    expect(sessionKeys()).toEqual([])
  })

  it('collects a dead session under the PRE-BUMP anchor prefix too', async () => {
    // The anchor key shape carries a format version. `ScrollAnchorCache` reaps
    // the old shape outright, but only once the chat virtualizer loads — so for
    // a user who never opens a chat the boot sweep is the only thing that ever
    // reaches those keys, and bumping the prefix without keeping the old one here
    // would strand them permanently.
    localStorage.setItem('vc_anchor_gone', '{}')
    localStorage.setItem(ANCHOR + 'gone', '{}')

    expect(sweepSuperseded(['gone'], ['alive'])).toBe(2)
    expect(sessionKeys()).toEqual([])
  })
})
