// Feature: chat-virtualizer — ScrollAnchorCache unit tests.
//
// The persisted reading-position anchor (issue #2774). Pure storage-format
// tests: round-trip, malformed-blob rejection, and the storageGc coupling
// that keeps deleted sessions from leaking anchors.

import { describe, it, expect, beforeEach } from 'vitest'

import {
  ANCHOR_KEY_PREFIX,
  anchorWriteChangesState,
  saveScrollAnchor,
  loadScrollAnchor,
  clearScrollAnchor,
} from '../hooks/virtualizer/ScrollAnchorCache'
import { gcOrphanedStorage } from '../utils/storageGc'

describe('ScrollAnchorCache', () => {
  beforeEach(() => localStorage.clear())

  it('round-trips an anchor per session', () => {
    saveScrollAnchor('s1', { key: 'row-abc', top: -42.5 })
    saveScrollAnchor('s2', { key: 'row-def', top: 12 })
    expect(loadScrollAnchor('s1')).toEqual({ key: 'row-abc', top: -42.5 })
    expect(loadScrollAnchor('s2')).toEqual({ key: 'row-def', top: 12 })
  })

  it('returns null when nothing is saved', () => {
    expect(loadScrollAnchor('nope')).toBeNull()
  })

  it('clear removes only the target session', () => {
    saveScrollAnchor('s1', { key: 'k', top: 0 })
    saveScrollAnchor('s2', { key: 'k', top: 0 })
    clearScrollAnchor('s1')
    expect(loadScrollAnchor('s1')).toBeNull()
    expect(loadScrollAnchor('s2')).toEqual({ key: 'k', top: 0 })
  })

  it('treats a corrupted blob as absent and removes it so it cannot re-poison', () => {
    localStorage.setItem(`${ANCHOR_KEY_PREFIX}s1`, '{not json')
    expect(loadScrollAnchor('s1')).toBeNull()
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}s1`)).toBeNull()
  })

  it('rejects malformed shapes without throwing', () => {
    const bad = [
      'null',
      '[]',
      '42',
      '{"key":"","top":1}', // empty key
      '{"key":"k","top":"1"}', // non-numeric top
      '{"key":"k","top":null}',
      '{"key":"k"}', // missing top
      `{"key":"k","top":${'1e999'}}`, // Infinity after parse — non-finite
    ]
    for (const raw of bad) {
      localStorage.setItem(`${ANCHOR_KEY_PREFIX}sX`, raw)
      expect(loadScrollAnchor('sX'), raw).toBeNull()
    }
  })

  it('no-ops on an empty session id', () => {
    saveScrollAnchor('', { key: 'k', top: 0 })
    expect(localStorage.length).toBe(0)
    expect(loadScrollAnchor('')).toBeNull()
  })

  it('is collected by the orphan sweep once a session is gone', () => {
    // The SESSION_PREFIXES entry in utils/storageGc.ts must stay byte-
    // identical to ANCHOR_KEY_PREFIX — this is the coupling test.
    saveScrollAnchor('doomed', { key: 'k', top: 5 })
    saveScrollAnchor('alive', { key: 'k', top: 5 })
    gcOrphanedStorage(new Set(['alive']))
    expect(loadScrollAnchor('doomed')).toBeNull()
    expect(loadScrollAnchor('alive')).toEqual({ key: 'k', top: 5 })
  })
})


describe('legacy anchor amnesty (v1 / v2 -> v3)', () => {
  it('loadScrollAnchor never resolves a pre-gate v1 blob', () => {
    // A v1 anchor written before the hard-input gate existed: potentially a
    // self-scroll displacement laundered into a reading position. The v2
    // prefix orphans it; the reaper (module load) removes it, and no v2 read
    // can ever resolve it.
    localStorage.setItem('vc_anchor_sess-old', JSON.stringify({ key: 'm5', top: -90 }))
    expect(loadScrollAnchor('sess-old')).toBeNull()
    // Save/load under the CURRENT prefix round-trips normally. Asserted through
    // the constant, not a spelled-out version: a literal here couples the test to
    // one version number and fails on the next bump for no behavioural reason.
    saveScrollAnchor('sess-old', { key: 'a-m7', top: -12 })
    expect(loadScrollAnchor('sess-old')).toEqual({ key: 'a-m7', top: -12 })
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}sess-old`)).not.toBeNull()
  })

  it('never resolves a v2 blob written in the per-render key vocabulary', () => {
    // A v2 anchor holds a ChatPage `rowKeys` key, which is only valid inside the
    // render that produced it -- so it could never resolve after a switch, and
    // the failed restore cleared it. The v3 prefix orphans them up front instead
    // of making every session pay one failed restore to discover that.
    localStorage.setItem('vc_anchor2_sess-v2', JSON.stringify({ key: 'turn-3', top: -40 }))
    expect(loadScrollAnchor('sess-v2')).toBeNull()
  })
})

/**
 * Write de-duplication. A streaming turn schedules a save on every settle, and the
 * overwhelming majority land on the same row a pixel or two apart -- so the question the
 * write asks is whether the STATE would differ, not whether a save was requested.
 */
describe('anchorWriteChangesState', () => {
  it('treats the first write as a change', () => {
    expect(anchorWriteChangesState(null, { key: 'a-m1', top: -10 })).toBe(true)
  })

  it('is a change when the row differs', () => {
    expect(anchorWriteChangesState({ key: 'a-m1', top: -10 }, { key: 'a-m2', top: -10 })).toBe(true)
  })

  it('is a change when only the ALT identity differs', () => {
    // The two ends fail in opposite cases, so a row that gained or lost its lead id
    // is a different anchor even at an identical key and offset.
    expect(anchorWriteChangesState({ key: 'a-m1', top: -10 }, { key: 'a-m1', top: -10, alt: 'l-m1' })).toBe(true)
    expect(anchorWriteChangesState({ key: 'a-m1', top: -10, alt: 'l-m1' }, { key: 'a-m1', top: -10 })).toBe(true)
  })

  it('is NOT a change within the epsilon, and IS beyond it', () => {
    const prev = { key: 'a-m1', top: -10 }
    expect(anchorWriteChangesState(prev, { key: 'a-m1', top: -10.4 })).toBe(false)
    expect(anchorWriteChangesState(prev, { key: 'a-m1', top: -11.6 })).toBe(true)
  })
})

describe('saveScrollAnchor de-duplicates against what is already stored', () => {
  beforeEach(() => localStorage.clear())

  it('leaves the stored blob untouched for a sub-epsilon move', () => {
    saveScrollAnchor('s-dedup', { key: 'a-m1', top: -10 })
    const first = localStorage.getItem(`${ANCHOR_KEY_PREFIX}s-dedup`)
    saveScrollAnchor('s-dedup', { key: 'a-m1', top: -10.4 })
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}s-dedup`)).toBe(first)
  })

  it('writes when the move clears the epsilon', () => {
    saveScrollAnchor('s-dedup', { key: 'a-m1', top: -10 })
    saveScrollAnchor('s-dedup', { key: 'a-m1', top: -40 })
    expect(loadScrollAnchor('s-dedup')).toEqual({ key: 'a-m1', top: -40 })
  })

  it('writes over a stored blob it cannot parse rather than skipping', () => {
    // An unreadable previous state is not evidence that the write is redundant.
    localStorage.setItem(`${ANCHOR_KEY_PREFIX}s-bad`, '{not json')
    saveScrollAnchor('s-bad', { key: 'a-m9', top: -5 })
    expect(loadScrollAnchor('s-bad')).toEqual({ key: 'a-m9', top: -5 })
  })

  it.each([
    ['a non-object', JSON.stringify('nope')],
    ['an array', JSON.stringify([{ key: 'a-m1', top: 0 }])],
    ['an empty key', JSON.stringify({ key: '', top: 0 })],
    ['a non-finite top', JSON.stringify({ key: 'a-m1', top: null })],
  ])('writes over %s stored under the current prefix', (_label, blob) => {
    localStorage.setItem(`${ANCHOR_KEY_PREFIX}s-shape`, blob)
    saveScrollAnchor('s-shape', { key: 'a-m3', top: -7 })
    expect(loadScrollAnchor('s-shape')).toEqual({ key: 'a-m3', top: -7 })
  })

  it('carries the alt identity through a round trip', () => {
    saveScrollAnchor('s-alt', { key: 'a-m1', top: -3, alt: 'l-m1' })
    expect(loadScrollAnchor('s-alt')).toEqual({ key: 'a-m1', top: -3, alt: 'l-m1' })
  })
})

describe('legacy anchors are reaped at module load', () => {
  it('removes v1 and v2 keys and leaves the current prefix alone', async () => {
    localStorage.clear()
    localStorage.setItem('vc_anchor_sess-a', 'anything')
    localStorage.setItem('vc_anchor2_sess-b', 'anything')
    localStorage.setItem(`${ANCHOR_KEY_PREFIX}sess-c`, JSON.stringify({ key: 'a-m1', top: -1 }))
    localStorage.setItem('unrelated_key', 'keep me')

    // The reap runs once, at import. Re-import a fresh copy so it sees the seeds.
    const { resetModules } = await import('vitest').then(m => ({ resetModules: m.vi.resetModules }))
    resetModules()
    await import('../hooks/virtualizer/ScrollAnchorCache')

    expect(localStorage.getItem('vc_anchor_sess-a')).toBeNull()
    expect(localStorage.getItem('vc_anchor2_sess-b')).toBeNull()
    expect(localStorage.getItem(`${ANCHOR_KEY_PREFIX}sess-c`)).not.toBeNull()
    expect(localStorage.getItem('unrelated_key')).toBe('keep me')
  })
})
