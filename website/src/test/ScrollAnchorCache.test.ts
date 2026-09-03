// Feature: chat-virtualizer — ScrollAnchorCache unit tests.
//
// The persisted reading-position anchor (issue #2774). Pure storage-format
// tests: round-trip, malformed-blob rejection, and the storageGc coupling
// that keeps deleted sessions from leaking anchors.

import { describe, it, expect, beforeEach } from 'vitest'

import {
  ANCHOR_KEY_PREFIX,
  saveScrollAnchor,
  loadScrollAnchor,
  clearScrollAnchor,
} from '../hooks/virtualizer/ScrollAnchorCache'
import { gcSessionStorage } from '../utils/storageGc'

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

  it('is collected by gcSessionStorage when a session is deleted', () => {
    // The SESSION_PREFIXES entry in utils/storageGc.ts must stay byte-
    // identical to ANCHOR_KEY_PREFIX — this is the coupling test.
    saveScrollAnchor('doomed', { key: 'k', top: 5 })
    saveScrollAnchor('alive', { key: 'k', top: 5 })
    gcSessionStorage('doomed')
    expect(loadScrollAnchor('doomed')).toBeNull()
    expect(loadScrollAnchor('alive')).toEqual({ key: 'k', top: 5 })
  })
})


describe('legacy anchor amnesty (v1 -> v2)', () => {
  it('loadScrollAnchor never resolves a pre-gate v1 blob', () => {
    // A v1 anchor written before the hard-input gate existed: potentially a
    // self-scroll displacement laundered into a reading position. The v2
    // prefix orphans it; the reaper (module load) removes it, and no v2 read
    // can ever resolve it.
    localStorage.setItem('vc_anchor_sess-old', JSON.stringify({ key: 'm5', top: -90 }))
    expect(loadScrollAnchor('sess-old')).toBeNull()
    // Save/load under v2 round-trips normally.
    saveScrollAnchor('sess-old', { key: 'm7', top: -12 })
    expect(loadScrollAnchor('sess-old')).toEqual({ key: 'm7', top: -12 })
    expect(localStorage.getItem('vc_anchor2_sess-old')).not.toBeNull()
  })
})
