/**
 * A pin recorded for a surface that was later RETIRED from the registry must
 * not survive the read: with no rail row and no pin control left, it would
 * hold one of the NAV_PINNED_LIMIT slots forever and keep every remaining
 * pin control disabled at the cap. `readNavPinned` drops retired ids BEFORE
 * the cap applies (`capabilities-templates` is the retired Agent Templates
 * tab), so a full set of live pins still fits.
 */
import { describe, it, expect, beforeEach } from 'vitest'

import { readNavPinned, toggleNavPinned, NAV_PINNED_KEY, NAV_PINNED_LIMIT } from '../lib/navPinned'

describe('retired surface ids in the persisted pinned set', () => {
  beforeEach(() => localStorage.clear())

  it('drops a retired id on read, before the cap applies', () => {
    const live = ['capabilities-crews', 'capabilities-skills', 'capabilities-mcp', 'capabilities-hooks']
    expect(live.length + 1).toBeLessThanOrEqual(NAV_PINNED_LIMIT)
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-templates', ...live]))
    const pinned = readNavPinned()
    expect(pinned.has('capabilities-templates')).toBe(false)
    for (const id of live) expect(pinned.has(id)).toBe(true)
  })

  it('a ghost pin no longer consumes a cap slot: a new pin is accepted', () => {
    localStorage.setItem(
      NAV_PINNED_KEY,
      JSON.stringify(['capabilities-templates', 'a', 'b', 'c', 'd']),
    )
    // 5 stored ids, but one is retired — the set reads as 4, so this succeeds.
    expect(toggleNavPinned('capabilities-workflows')).toBe(true)
    const pinned = readNavPinned()
    expect(pinned.has('capabilities-workflows')).toBe(true)
    expect(pinned.has('capabilities-templates')).toBe(false)
  })
})
