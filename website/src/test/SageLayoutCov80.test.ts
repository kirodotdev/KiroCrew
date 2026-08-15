import { describe, it, expect, afterEach, vi } from 'vitest'

import {
  RAIL_WIDTH_KEY, LIST_WIDTH_KEY,
  DEFAULT_RAIL_WIDTH, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH,
  DEFAULT_LIST_WIDTH, MIN_LIST_WIDTH, MAX_LIST_WIDTH,
  LIVE_POLL_MS, IDLE_POLL_MS,
  loadRailWidth, loadListWidth,
} from '../apps/code-review-sage/lib/layout'

/**
 * Column geometry persistence. A stored width is user preference, so it has to
 * survive a reload — but it also has to be CLAMPED, because a value written by
 * an older build (or hand-edited storage) could otherwise collapse the rail to
 * nothing or push the detail pane off screen with no way back.
 */
afterEach(() => {
  localStorage.removeItem(RAIL_WIDTH_KEY)
  localStorage.removeItem(LIST_WIDTH_KEY)
  vi.restoreAllMocks()
})

describe('layout storage keys', () => {
  it('namespaces the rail and list keys per app so another app cannot collide', () => {
    expect(RAIL_WIDTH_KEY).toBe('kc:code-review-sage:rail-width')
    expect(LIST_WIDTH_KEY).toBe('kc:code-review-sage:list-width')
    expect(RAIL_WIDTH_KEY).not.toBe(LIST_WIDTH_KEY)
  })

  it('keeps the live poll cadence far tighter than the idle one', () => {
    expect(LIVE_POLL_MS).toBeLessThan(IDLE_POLL_MS)
  })
})

describe('loadRailWidth', () => {
  it('falls back to the default with nothing stored', () => {
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH)
  })

  it('returns a stored width inside the allowed range', () => {
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    expect(loadRailWidth()).toBe(400)
  })

  it('clamps below the minimum and above the maximum', () => {
    localStorage.setItem(RAIL_WIDTH_KEY, '10')
    expect(loadRailWidth()).toBe(MIN_RAIL_WIDTH)
    localStorage.setItem(RAIL_WIDTH_KEY, '9999')
    expect(loadRailWidth()).toBe(MAX_RAIL_WIDTH)
  })

  it('falls back to the default for a non-numeric value', () => {
    localStorage.setItem(RAIL_WIDTH_KEY, 'zzz')
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH)
  })

  it('falls back to the default when storage is blocked (private mode / quota)', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('zzz storage blocked')
    })
    expect(loadRailWidth()).toBe(DEFAULT_RAIL_WIDTH)
  })
})

describe('loadListWidth', () => {
  it('reads its OWN key, not the rail key', () => {
    localStorage.setItem(RAIL_WIDTH_KEY, '400')
    expect(loadListWidth()).toBe(DEFAULT_LIST_WIDTH)
    localStorage.setItem(LIST_WIDTH_KEY, '300')
    expect(loadListWidth()).toBe(300)
  })

  it('clamps to its own bounds', () => {
    localStorage.setItem(LIST_WIDTH_KEY, '1')
    expect(loadListWidth()).toBe(MIN_LIST_WIDTH)
    localStorage.setItem(LIST_WIDTH_KEY, '5000')
    expect(loadListWidth()).toBe(MAX_LIST_WIDTH)
  })

  it('treats an empty string as nothing stored', () => {
    localStorage.setItem(LIST_WIDTH_KEY, '')
    expect(loadListWidth()).toBe(DEFAULT_LIST_WIDTH)
  })
})
