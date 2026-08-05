/**
 * Filter-selection migration when a notification kind is added.
 *
 * `NotificationFeed` treats "every known kind selected" as a special state --
 * `allActive` is what makes notifications with UNKNOWN kinds visible. The
 * selection is persisted as an explicit list, so adding a kind silently turns a
 * stored full set into a partial one, flips filtering to strict, and hides the
 * new kind for every existing install with no chip that brings it back. The
 * versioned key plus the seed below is what stops that.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'

import {
  KIND_KEYS,
  KINDS_STORAGE_KEY,
  LEGACY_KINDS_STORAGE_KEY,
  loadActiveKinds,
} from '../components/notifications/notifMeta'

const LEGACY_ALL = ['cron', 'hook', 'heartbeat', 'agent', 'approval', 'subagent', 'taskrunner']

beforeEach(() => {
  localStorage.clear()
})

describe('loadActiveKinds', () => {
  it('defaults to every kind on a fresh install', () => {
    expect(loadActiveKinds()).toEqual(new Set(KIND_KEYS))
  })

  it('includes skills so the chip register covers skill notifications', () => {
    expect(KIND_KEYS).toContain('skills')
    expect(loadActiveKinds().has('skills')).toBe(true)
  })

  it('promotes a full legacy set to every CURRENT kind', () => {
    // The regression this guards: a v1 payload listing all seven legacy kinds
    // meant "all". Carried over verbatim it would be 7-of-8, which is a PARTIAL
    // set -- strict filtering, and skill notes invisible forever.
    localStorage.setItem(LEGACY_KINDS_STORAGE_KEY, JSON.stringify(LEGACY_ALL))
    const loaded = loadActiveKinds()
    expect(loaded).toEqual(new Set(KIND_KEYS))
    expect(loaded.size).toBe(KIND_KEYS.length)
  })

  it('carries a deliberate legacy subset over verbatim', () => {
    localStorage.setItem(LEGACY_KINDS_STORAGE_KEY, JSON.stringify(['cron', 'agent']))
    expect(loadActiveKinds()).toEqual(new Set(['cron', 'agent']))
  })

  it('prefers the versioned key over the legacy one', () => {
    localStorage.setItem(LEGACY_KINDS_STORAGE_KEY, JSON.stringify(LEGACY_ALL))
    localStorage.setItem(KINDS_STORAGE_KEY, JSON.stringify(['cron']))
    expect(loadActiveKinds()).toEqual(new Set(['cron']))
  })

  it('honours an empty versioned selection instead of falling back to all', () => {
    // "" nothing selected" is a real state the All chip produces; treating it as
    // absent would silently re-select everything on reload.
    localStorage.setItem(KINDS_STORAGE_KEY, JSON.stringify([]))
    expect(loadActiveKinds()).toEqual(new Set())
  })

  it('drops unknown kinds from a stored selection', () => {
    localStorage.setItem(KINDS_STORAGE_KEY, JSON.stringify(['cron', 'not-a-kind']))
    expect(loadActiveKinds()).toEqual(new Set(['cron']))
  })

  it('falls back to all kinds on corrupted json', () => {
    localStorage.setItem(KINDS_STORAGE_KEY, '{not json')
    expect(loadActiveKinds()).toEqual(new Set(KIND_KEYS))
  })

  it('falls back to all kinds when the stored value is not an array', () => {
    localStorage.setItem(KINDS_STORAGE_KEY, JSON.stringify({ cron: true }))
    expect(loadActiveKinds()).toEqual(new Set(KIND_KEYS))
  })

  it('survives storage that THROWS on read', () => {
    // Regression: reading localStorage raises SecurityError when a browser
    // policy or embedding context blocks it, and this function is a useState
    // initializer -- an uncaught throw took down the whole notification feed,
    // not just the filter selection. Only `parseKinds`'s JSON.parse was
    // guarded, which is a different failure.
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    })
    try {
      expect(() => loadActiveKinds()).not.toThrow()
      expect(loadActiveKinds()).toEqual(new Set(KIND_KEYS))
    } finally {
      spy.mockRestore()
    }
  })
})
