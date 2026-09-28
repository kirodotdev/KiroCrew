import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  GOAL_DRAFTS_KEY,
  GOAL_DRAFT_MAX_ENTRIES,
  GOAL_DRAFT_TTL_MS,
  loadGoalDraft,
  saveGoalDraft,
  __resetForTests,
} from '../utils/goalDrafts'
import { safeSetItem } from '../utils/safeStorage'

const draft = (message: string, idleSecs = 60, maxCycles = 0) => ({ message, idleSecs, maxCycles })

describe('goalDrafts', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.useRealTimers() })

  it('roundtrips a per-slot goal draft (survives close/re-open/refresh)', () => {
    saveGoalDraft('chat-1-100', draft('finish the migration', 120, 5))
    // New "page load" / popover re-mount reads it straight back.
    expect(loadGoalDraft('chat-1-100')).toEqual(draft('finish the migration', 120, 5))
  })

  it('returns null for an unknown slot', () => {
    expect(loadGoalDraft('never-set')).toBeNull()
  })

  it('keeps drafts per-slot (no crossover between sessions)', () => {
    saveGoalDraft('chat-1-100', draft('goal A'))
    saveGoalDraft('chat-2-200', draft('goal B'))
    expect(loadGoalDraft('chat-1-100')?.message).toBe('goal A')
    expect(loadGoalDraft('chat-2-200')?.message).toBe('goal B')
  })

  it('drops the slot when saved with null or a blank message (never pins the default)', () => {
    saveGoalDraft('chat-1-100', draft('typed goal'))
    expect(loadGoalDraft('chat-1-100')).not.toBeNull()
    saveGoalDraft('chat-1-100', null)
    expect(loadGoalDraft('chat-1-100')).toBeNull()
    // Whitespace-only message is also treated as empty.
    saveGoalDraft('chat-1-100', draft('   '))
    expect(loadGoalDraft('chat-1-100')).toBeNull()
  })

  it('overwrites an existing slot draft in place', () => {
    saveGoalDraft('chat-1-100', draft('first'))
    saveGoalDraft('chat-1-100', draft('second', 300, 10))
    expect(loadGoalDraft('chat-1-100')).toEqual(draft('second', 300, 10))
  })

  it('returns null on missing, corrupt, or non-object storage', () => {
    expect(loadGoalDraft('x')).toBeNull()
    // Inject deliberately-corrupt payloads via safeSetItem (it only wraps
    // quota handling — the raw string is written verbatim), proving
    // loadGoalDraft survives externally-corrupted / hand-edited storage.
    safeSetItem(GOAL_DRAFTS_KEY, 'not json'); __resetForTests()
    expect(loadGoalDraft('x')).toBeNull()
    safeSetItem(GOAL_DRAFTS_KEY, '[]'); __resetForTests()
    expect(loadGoalDraft('x')).toBeNull()
    safeSetItem(GOAL_DRAFTS_KEY, 'null'); __resetForTests()
    expect(loadGoalDraft('x')).toBeNull()
  })

  it('filters out entries with wrong-typed fields (corruption / hand-edit)', () => {
    // safeSetItem writes the serialized string verbatim, so it can still seed
    // wrong-typed fixture members — proving the sanitizer drops each of them.
    safeSetItem(GOAL_DRAFTS_KEY, JSON.stringify({
      good: { message: 'ok', idleSecs: 60, maxCycles: 0 },
      'no-message': { idleSecs: 60, maxCycles: 0 },
      'empty-message': { message: '', idleSecs: 60, maxCycles: 0 },
      'bad-idle': { message: 'x', idleSecs: 'nope', maxCycles: 0 },
      'not-object': 42,
    }))
    __resetForTests()
    expect(loadGoalDraft('good')?.message).toBe('ok')
    expect(loadGoalDraft('no-message')).toBeNull()
    expect(loadGoalDraft('empty-message')).toBeNull()
    expect(loadGoalDraft('bad-idle')).toBeNull()
    expect(loadGoalDraft('not-object')).toBeNull()
  })

  it('saveGoalDraft swallows QuotaExceededError without throwing', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      expect(() => saveGoalDraft('chat-1-100', draft('x'))).not.toThrow()
    } finally {
      Storage.prototype.setItem = orig
    }
  })

  it('evicts oldest entries when over the cap', () => {
    for (let i = 0; i < GOAL_DRAFT_MAX_ENTRIES + 5; i++) saveGoalDraft(`slot-${i}`, draft(`g${i}`))
    // Oldest evicted, newest retained.
    expect(loadGoalDraft('slot-0')).toBeNull()
    expect(loadGoalDraft('slot-4')).toBeNull()
    expect(loadGoalDraft('slot-5')?.message).toBe('g5')
    expect(loadGoalDraft(`slot-${GOAL_DRAFT_MAX_ENTRIES + 4}`)?.message).toBe(`g${GOAL_DRAFT_MAX_ENTRIES + 4}`)
  })

  it('re-saving a slot refreshes its LRU position (recently edited slot is not evicted)', () => {
    for (let i = 0; i < GOAL_DRAFT_MAX_ENTRIES; i++) saveGoalDraft(`slot-${i}`, draft(`g${i}`))
    // Keep editing slot-0 so it moves to the tail, then push one more slot.
    saveGoalDraft('slot-0', draft('still editing'))
    saveGoalDraft('slot-new', draft('brand new'))
    expect(loadGoalDraft('slot-0')?.message).toBe('still editing')
    expect(loadGoalDraft('slot-1')).toBeNull()
    expect(loadGoalDraft('slot-new')?.message).toBe('brand new')
  })

  it('discards drafts older than the TTL on load', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    saveGoalDraft('chat-old', draft('sensitive goal'))
    vi.setSystemTime(Date.now() + GOAL_DRAFT_TTL_MS + 1000)
    __resetForTests()
    expect(loadGoalDraft('chat-old')).toBeNull()
  })

  it('keeps drafts edited within the TTL window', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    saveGoalDraft('chat-fresh', draft('recent goal'))
    vi.setSystemTime(Date.now() + GOAL_DRAFT_TTL_MS - 1000)
    __resetForTests()
    expect(loadGoalDraft('chat-fresh')?.message).toBe('recent goal')
  })
})
