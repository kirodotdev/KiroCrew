import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  PASTE_DRAFTS_KEY, PASTE_DRAFT_MAX_ENTRIES, PASTE_DRAFT_TTL_MS,
  loadPasteDrafts, savePasteDrafts, setPasteDraft, __resetForTests,
} from '../utils/chatPasteDrafts'
import { DRAFT_MAX_STORE_BYTES } from '../utils/draftConstants'
import type { PasteBlock } from '../utils/pasteTokens'

const block = (seq: number, content = 'pasted content'): PasteBlock => ({
  id: `id-${seq}`, seq, lines: content.split('\n').length, content,
})

describe('chatPasteDrafts', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.useRealTimers() })


  it('keeps drafts a user already typed when they sit between the old and new caps', () => {
    // Deriving the cap from the shared budget LOWERED it to ~1.07 MiB. A store already holding
    // ~1.4 MiB is under 2 MiB but OVER that, so the next save LRU-evicted work already typed.
    const each = 460 * 1024
    const drafts = {
      'chat-oldest-1': [block(1, 'a'.repeat(each))],
      'chat-middle-2': [block(1, 'b'.repeat(each))],
      'chat-newest-3': [block(1, 'c'.repeat(each))],
    }
    savePasteDrafts(drafts)
    const held = JSON.stringify(loadPasteDrafts()).length
    expect(held, 'premise: the fixture sits between the derived ~1.07 MiB and the 2 MiB cap')
      .toBeGreaterThan(1_150_000)
    expect(held, 'premise: and it is genuinely under the retained cap').toBeLessThan(DRAFT_MAX_STORE_BYTES)
    expect(Object.keys(loadPasteDrafts()).sort(),
      'no entry may be dropped: the cap moving is not a reason to delete unsent work')
      .toEqual(['chat-middle-2', 'chat-newest-3', 'chat-oldest-1'])
  })

  it('roundtrips paste blocks through localStorage (survives close/refresh)', () => {
    const drafts = { 'chat-1-100': [block(1)], 'chat-2-200': [block(1), block(2)] }
    savePasteDrafts(drafts)
    expect(loadPasteDrafts()).toEqual(drafts)
    // Uses localStorage, NOT sessionStorage — refresh must not drop the blocks
    // (that would reproduce the dead-token bug, just with a different trigger).
    expect(localStorage.getItem(PASTE_DRAFTS_KEY)).toBeTruthy()
    expect(sessionStorage.getItem(PASTE_DRAFTS_KEY)).toBeNull()
  })

  it('returns {} on missing, corrupt, or non-object storage', () => {
    expect(loadPasteDrafts()).toEqual({})
    localStorage.setItem(PASTE_DRAFTS_KEY, 'not json')
    expect(loadPasteDrafts()).toEqual({})
    localStorage.setItem(PASTE_DRAFTS_KEY, '[]')
    expect(loadPasteDrafts()).toEqual({})
    localStorage.setItem(PASTE_DRAFTS_KEY, 'null')
    expect(loadPasteDrafts()).toEqual({})
  })

  it('drops corrupt / malformed blocks (missing or wrong-typed fields)', () => {
    localStorage.setItem(PASTE_DRAFTS_KEY, JSON.stringify({
      'good': [block(1)],
      'string-value': 'not-an-array',
      'number-value': 42,
      'null-value': null,
      'empty-array': [],
      'mixed': [block(1), { id: 'x' }, { seq: 2 }, 42, null, block(2)],
      'wrong-types': [{ id: 5, seq: '1', lines: '3', content: 99 }],
    }))
    expect(loadPasteDrafts()).toEqual({
      'good': [block(1)],
      'mixed': [block(1), block(2)],
      // empty-array, all-invalid slots dropped entirely
    })
  })

  it('setPasteDraft stores non-empty and deletes empty', () => {
    const d: Record<string, PasteBlock[]> = { 'chat-1-100': [block(9)] }
    setPasteDraft(d, 'chat-1-100', [block(1), block(2)])
    expect(d).toEqual({ 'chat-1-100': [block(1), block(2)] })
    setPasteDraft(d, 'chat-1-100', [])
    expect(d).toEqual({})
  })

  it('setPasteDraft stores a deep copy so caller mutations do not leak', () => {
    const d: Record<string, PasteBlock[]> = {}
    const live = [block(1, 'original')]
    setPasteDraft(d, 'chat-1', live)
    // Mutate both the array and the block in place
    live.push(block(2))
    live[0].content = 'mutated'
    expect(d['chat-1']).toEqual([block(1, 'original')])
  })

  it('savePasteDrafts swallows QuotaExceededError without throwing', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      expect(() => savePasteDrafts({ a: [block(1)] })).not.toThrow()
    } finally {
      Storage.prototype.setItem = orig
    }
  })

  it('byte-aware LRU evicts oldest slots until the blob fits, keeping the newest', () => {
    const drafts: Record<string, PasteBlock[]> = {}
    // Sized FROM the budget, not a literal: four slots overflow it while any one still fits,
    // which is what makes eviction rather than the keep-the-newest rule below the behaviour here.
    const each = Math.floor(DRAFT_MAX_STORE_BYTES / 3)
    for (let i = 0; i < 4; i++) setPasteDraft(drafts, `slot-${i}`, [block(i, 'x'.repeat(each))])
    savePasteDrafts(drafts)
    const persisted = loadPasteDrafts()
    expect(JSON.stringify(persisted).length).toBeLessThanOrEqual(DRAFT_MAX_STORE_BYTES)
    expect(persisted['slot-0']).toBeUndefined() // oldest evicted to make room
    expect(persisted['slot-3']).toBeDefined()   // newest survives
  })

  it('byte-aware LRU never drops the newest slot even when it alone exceeds the budget', () => {
    const drafts: Record<string, PasteBlock[]> = {}
    setPasteDraft(drafts, 'newest', [block(1, 'y'.repeat(DRAFT_MAX_STORE_BYTES + 5000))])
    savePasteDrafts(drafts)
    // This slot persists even though it alone exceeds the budget, so its chip
    // stays an expandable token instead of rehydrating as a dead literal.
    expect(loadPasteDrafts()['newest']).toBeDefined()
  })

  it('evicts oldest entries when over cap', () => {
    const drafts: Record<string, PasteBlock[]> = {}
    for (let i = 0; i < PASTE_DRAFT_MAX_ENTRIES + 5; i++) drafts[`slot-${i}`] = [block(1, `d${i}`)]
    savePasteDrafts(drafts)
    expect(Object.keys(drafts).length).toBe(PASTE_DRAFT_MAX_ENTRIES)
    expect(drafts['slot-0']).toBeUndefined()
    expect(drafts['slot-4']).toBeUndefined()
    expect(drafts['slot-5']).toEqual([block(1, 'd5')])
    expect(Object.keys(loadPasteDrafts()).length).toBe(PASTE_DRAFT_MAX_ENTRIES)
  })

  it('setPasteDraft refreshes insertion order (LRU - recently touched slot not evicted)', () => {
    const drafts: Record<string, PasteBlock[]> = {}
    for (let i = 0; i < PASTE_DRAFT_MAX_ENTRIES; i++) drafts[`slot-${i}`] = [block(1, `d${i}`)]
    setPasteDraft(drafts, 'slot-0', [block(1, 'still editing')])
    setPasteDraft(drafts, 'slot-new', [block(1, 'brand new')])
    savePasteDrafts(drafts)
    expect(drafts['slot-0']).toEqual([block(1, 'still editing')])
    expect(drafts['slot-1']).toBeUndefined()
    expect(drafts['slot-new']).toEqual([block(1, 'brand new')])
  })

  it('discards blocks older than the TTL on load', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const drafts: Record<string, PasteBlock[]> = {}
    setPasteDraft(drafts, 'chat-old', [block(1, 'sensitive paste')])
    savePasteDrafts(drafts)
    vi.setSystemTime(Date.now() + PASTE_DRAFT_TTL_MS + 1000)
    __resetForTests()
    expect(loadPasteDrafts()).toEqual({})
  })

  it('keeps blocks touched within the TTL window', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const drafts: Record<string, PasteBlock[]> = {}
    setPasteDraft(drafts, 'chat-fresh', [block(1, 'recent')])
    savePasteDrafts(drafts)
    vi.setSystemTime(Date.now() + PASTE_DRAFT_TTL_MS - 1000)
    __resetForTests()
    expect(loadPasteDrafts()).toEqual({ 'chat-fresh': [block(1, 'recent')] })
  })

  it('simulates the dead-token slot-switch cycle: blocks survive away-and-back', () => {
    // Slot A has a collapsed paste; user switches to B then back to A.
    const d: Record<string, PasteBlock[]> = {}
    setPasteDraft(d, 'slot-a', [block(1, 'big paste body')])
    savePasteDrafts(d)
    // Fresh load (as on slot switch) — A's block is intact, so the token can
    // re-pair and stays an expandable chip instead of going literal.
    const reloaded = loadPasteDrafts()
    expect(reloaded['slot-a']).toEqual([block(1, 'big paste body')])
    // Switch into B (no paste) and back — A unaffected, B absent.
    setPasteDraft(reloaded, 'slot-b', [])
    savePasteDrafts(reloaded)
    const final = loadPasteDrafts()
    expect(final['slot-a']).toEqual([block(1, 'big paste body')])
    expect(final['slot-b']).toBeUndefined()
  })

  it('writes timestamps before drafts to prevent TTL reset on partial quota failure', () => {
    const calls: string[] = []
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      calls.push(key)
      if (calls.length === 2) throw new Error('QuotaExceeded')
      return orig.call(this, key, value)
    }
    try {
      savePasteDrafts({ 'chat-1': [block(1)] })
    } finally {
      Storage.prototype.setItem = orig
    }
    expect(calls[0]).toBe('mc-chat-paste-drafts-ts')
    expect(calls[1]).toBe(PASTE_DRAFTS_KEY)
  })
})
