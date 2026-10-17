/**
 * Storage contract for the promoted-sub-item pinned set (`mc-nav-pinned`).
 *
 * Mirrors `appNavHidden.test.ts` in shape because the two modules share one
 * contract: degrade to "the rail exactly as it shipped" on any unreadable
 * value, and dispatch a same-tab event on every persisted write.
 *
 * The cap tests are the ones specific to this module. `NAV_PINNED_LIMIT`
 * bounds how much the rail's non-scrolling Main block can grow, so it is
 * enforced on WRITE (a refused pin) and again on READ (a value that got past
 * the write path by a hand-edit, or was written under a higher earlier cap).
 * Both sites are asserted because either one alone leaves the other free to
 * render an unbounded rail.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

import {
  NAV_PINNED_KEY,
  NAV_PINNED_LIMIT,
  readNavPinned,
  toggleNavPinned,
  subscribeNavPinned,
} from './navPinned'

beforeEach(() => {
  localStorage.clear()
})

describe('readNavPinned', () => {
  it('is empty when the key is absent, so a fresh install pins nothing', () => {
    expect(readNavPinned()).toEqual(new Set())
  })

  it('is empty on malformed JSON and never throws', () => {
    localStorage.setItem(NAV_PINNED_KEY, '{not json')
    expect(() => readNavPinned()).not.toThrow()
    expect(readNavPinned()).toEqual(new Set())
  })

  it('is empty when the stored value is valid JSON but not an array', () => {
    localStorage.setItem(NAV_PINNED_KEY, '{"a":1}')
    expect(readNavPinned()).toEqual(new Set())
  })

  it('drops non-string entries from a tampered array', () => {
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-steering', 7, null, { a: 1 }]))
    expect(readNavPinned()).toEqual(new Set(['capabilities-steering']))
  })

  it('reads back exactly what was pinned', () => {
    toggleNavPinned('capabilities-steering')
    toggleNavPinned('capabilities-skills')
    expect(readNavPinned()).toEqual(new Set(['capabilities-steering', 'capabilities-skills']))
  })

  it('truncates a stored set that exceeds the cap, so a hand-edit cannot grow the rail', () => {
    // Distinct ids rather than a repeated one: with one id every candidate
    // truncation expression coincides, and this must prove the LENGTH is what
    // bounds the result.
    const tooMany = Array.from({ length: NAV_PINNED_LIMIT + 3 }, (_, i) => `sub-${i}`)
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(tooMany))
    expect(readNavPinned().size).toBe(NAV_PINNED_LIMIT)
  })
})

describe('toggleNavPinned', () => {
  it('pins then unpins one id, reporting the new state each time', () => {
    expect(toggleNavPinned('capabilities-steering')).toBe(true)
    expect(readNavPinned().has('capabilities-steering')).toBe(true)
    expect(toggleNavPinned('capabilities-steering')).toBe(false)
    expect(readNavPinned().has('capabilities-steering')).toBe(false)
  })

  it('refuses a pin that would exceed the cap and leaves the set untouched', () => {
    const filled = Array.from({ length: NAV_PINNED_LIMIT }, (_, i) => `sub-${i}`)
    for (const id of filled) expect(toggleNavPinned(id)).toBe(true)

    expect(toggleNavPinned('one-too-many')).toBe(false)
    // Per-item pairing, not just a size check: a refusal must not have evicted
    // an earlier pin to make room, which a size assertion alone cannot see.
    const after = readNavPinned()
    expect(after.size).toBe(NAV_PINNED_LIMIT)
    for (const id of filled) expect(after.has(id)).toBe(true)
    expect(after.has('one-too-many')).toBe(false)
  })

  it('still unpins while the set is at the cap', () => {
    const filled = Array.from({ length: NAV_PINNED_LIMIT }, (_, i) => `sub-${i}`)
    for (const id of filled) toggleNavPinned(id)

    expect(toggleNavPinned('sub-0')).toBe(false)
    expect(readNavPinned().has('sub-0')).toBe(false)
    expect(readNavPinned().size).toBe(NAV_PINNED_LIMIT - 1)
  })
})

describe('same-tab propagation', () => {
  it('dispatches the change event on a pin AND on an unpin', () => {
    const seen = vi.fn()
    const unsubscribe = subscribeNavPinned(seen)
    try {
      toggleNavPinned('capabilities-steering')
      expect(seen).toHaveBeenCalledTimes(1)
      toggleNavPinned('capabilities-steering')
      expect(seen).toHaveBeenCalledTimes(2)
    } finally {
      unsubscribe()
    }
  })

  it('does NOT dispatch when a pin was refused, so no listener repaints for a no-op', () => {
    for (let i = 0; i < NAV_PINNED_LIMIT; i++) toggleNavPinned(`sub-${i}`)
    const seen = vi.fn()
    const unsubscribe = subscribeNavPinned(seen)
    try {
      expect(toggleNavPinned('one-too-many')).toBe(false)
      expect(seen).not.toHaveBeenCalled()
    } finally {
      unsubscribe()
    }
  })

  it('unsubscribing stops delivery', () => {
    const seen = vi.fn()
    subscribeNavPinned(seen)()
    toggleNavPinned('capabilities-steering')
    expect(seen).not.toHaveBeenCalled()
  })
})

describe('serialization', () => {
  it('persists a sorted array so the stored value is stable across write order', () => {
    toggleNavPinned('b-sub')
    toggleNavPinned('a-sub')
    expect(localStorage.getItem(NAV_PINNED_KEY)).toBe(JSON.stringify(['a-sub', 'b-sub']))
  })
})
