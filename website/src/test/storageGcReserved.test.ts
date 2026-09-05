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

    const removed = gcOrphanedStorage(new Set(['live-session']))

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

    const removed = gcOrphanedStorage(new Set(['alive']))

    expect(removed).toBe(3)
    expect(localStorage.length).toBe(0)
  })

  it('collects a dead session under the PRE-BUMP anchor prefix too', async () => {
    // The anchor key shape carries a format version. `ScrollAnchorCache` reaps
    // the old shape outright, but only once the chat virtualizer loads — so for
    // a user who never opens a chat the boot sweep is the only thing that ever
    // reaches those keys, and bumping the prefix without keeping the old one here
    // would strand them permanently.
    localStorage.setItem('vc_anchor_gone', '{}')
    localStorage.setItem(ANCHOR + 'gone', '{}')

    expect(gcOrphanedStorage(new Set(['alive']))).toBe(2)
    expect(localStorage.length).toBe(0)
  })
})
