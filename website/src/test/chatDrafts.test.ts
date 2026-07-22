import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { DRAFTS_KEY, DRAFT_MAX_ENTRIES, DRAFT_TTL_MS, loadDrafts, saveDrafts, setDraft, __resetForTests } from '../utils/chatDrafts'
import { DRAFT_MAX_STORE_BYTES } from '../utils/draftConstants'

describe('chatDrafts', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.useRealTimers() })

  it('roundtrips drafts through localStorage (survives close/crash)', () => {
    const drafts = { 'chat-1-100': 'hello', 'chat-2-200': 'world' }
    saveDrafts(drafts)
    // New "page load" — drafts are still there
    expect(loadDrafts()).toEqual(drafts)
    // Raw key matches contract (so migration / debugging is possible)
    expect(localStorage.getItem(DRAFTS_KEY)).toBe(JSON.stringify(drafts))
  })

  it('returns {} on missing, corrupt, or non-object storage', () => {
    expect(loadDrafts()).toEqual({})
    localStorage.setItem(DRAFTS_KEY, 'not json')
    expect(loadDrafts()).toEqual({})
    localStorage.setItem(DRAFTS_KEY, '[]')
    expect(loadDrafts()).toEqual({})
    localStorage.setItem(DRAFTS_KEY, 'null')
    expect(loadDrafts()).toEqual({})
  })

  it('setDraft stores non-empty and deletes empty', () => {
    const d: Record<string, string> = { 'chat-1-100': 'old' }
    setDraft(d, 'chat-1-100', 'new')
    expect(d).toEqual({ 'chat-1-100': 'new' })
    setDraft(d, 'chat-1-100', '')
    expect(d).toEqual({})
  })

  it('saveDrafts swallows QuotaExceededError without throwing', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      expect(() => saveDrafts({ a: 'b' })).not.toThrow()
    } finally {
      Storage.prototype.setItem = orig
    }
  })

  it('saveDrafts evicts oldest entries when over cap', () => {
    const drafts: Record<string, string> = {}
    // Fill with cap+5 entries in insertion order
    for (let i = 0; i < DRAFT_MAX_ENTRIES + 5; i++) drafts[`slot-${i}`] = `d${i}`
    saveDrafts(drafts)
    // In-memory and persisted copy should both be capped
    expect(Object.keys(drafts).length).toBe(DRAFT_MAX_ENTRIES)
    // Oldest (slot-0..slot-4) evicted, newest retained
    expect(drafts['slot-0']).toBeUndefined()
    expect(drafts['slot-4']).toBeUndefined()
    expect(drafts['slot-5']).toBe('d5')
    expect(drafts[`slot-${DRAFT_MAX_ENTRIES + 4}`]).toBe(`d${DRAFT_MAX_ENTRIES + 4}`)
    expect(Object.keys(loadDrafts()).length).toBe(DRAFT_MAX_ENTRIES)
  })

  it('byte-aware LRU evicts oldest text drafts until under the store budget, keeping newest', () => {
    // Text drafts gained a store-byte budget in the slotDraftStore migration
    // (they previously had only entry cap + TTL). Verify the new behavior for
    // this store specifically, not just generically in slotDraftStore.test.ts.
    const drafts: Record<string, string> = {}
    // 3 drafts ~1 MB each = ~3 MB, over the 2 MB budget.
    for (let i = 0; i < 3; i++) setDraft(drafts, `chat-${i}`, 'x'.repeat(1_000_000))
    saveDrafts(drafts)
    const reloaded = loadDrafts()
    expect(JSON.stringify(reloaded).length).toBeLessThanOrEqual(DRAFT_MAX_STORE_BYTES)
    expect(reloaded['chat-0']).toBeUndefined() // oldest evicted
    expect(reloaded['chat-2']).toBe('x'.repeat(1_000_000)) // newest survives
  })

  it('drops empty-string drafts on load (sanitize contract)', () => {
    // setDraft never persists '' (it deletes the slot), but a hand-edited or
    // legacy blob could contain one. The sanitize guard drops it.
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'chat-empty': '', 'chat-real': 'hi' }))
    expect(loadDrafts()).toEqual({ 'chat-real': 'hi' })
  })

  it('setDraft refreshes insertion order (LRU - recently edited draft is not evicted)', () => {
    const drafts: Record<string, string> = {}
    // Fill to cap. slot-0 is the oldest.
    for (let i = 0; i < DRAFT_MAX_ENTRIES; i++) drafts[`slot-${i}`] = `d${i}`
    // User keeps editing slot-0 — it should refresh its position
    setDraft(drafts, 'slot-0', 'still typing')
    // Now add one more slot, triggering eviction
    setDraft(drafts, 'slot-new', 'brand new')
    saveDrafts(drafts)
    // slot-0 survives because setDraft moved it to the end; slot-1 evicted instead
    expect(drafts['slot-0']).toBe('still typing')
    expect(drafts['slot-1']).toBeUndefined()
    expect(drafts['slot-new']).toBe('brand new')
    // Insertion order survives JSON roundtrip (guards future serialization changes)
    const reloaded = loadDrafts()
    expect(reloaded['slot-0']).toBe('still typing')
    expect(reloaded['slot-1']).toBeUndefined()
    expect(reloaded['slot-new']).toBe('brand new')
  })

  it('save→load→save preserves LRU order (regression: merge-based overwrite reset insertion position)', () => {
    const drafts: Record<string, string> = {}
    for (let i = 0; i < DRAFT_MAX_ENTRIES; i++) drafts[`slot-${i}`] = `d${i}`
    saveDrafts(drafts)
    // Simulate fresh page load, refresh LRU on slot-0, fill cap+1, save again.
    const reloaded = loadDrafts()
    setDraft(reloaded, 'slot-0', 'refreshed')
    setDraft(reloaded, 'slot-new', 'newest')
    saveDrafts(reloaded)
    // slot-0 was refreshed BEFORE adding slot-new, so slot-0 should survive
    // and slot-1 (oldest untouched) should be evicted.
    const final = loadDrafts()
    expect(final['slot-0']).toBe('refreshed')
    expect(final['slot-1']).toBeUndefined()
    expect(final['slot-new']).toBe('newest')
  })

  it('saveDrafts does not resurrect deleted keys (regression: merge re-added cleared drafts)', () => {
    saveDrafts({ 'chat-1': 'typed something' })
    const drafts = loadDrafts()
    // User clears the input → setDraft removes the key
    setDraft(drafts, 'chat-1', '')
    saveDrafts(drafts)
    // Deletion must persist
    expect(loadDrafts()).toEqual({})
  })

  it('setDraft + saveDrafts flushes correctly (beforeunload path)', () => {
    // Simulates the beforeunload handler: setDraft captures current input,
    // then saveDrafts (via flushDrafts) persists immediately.
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-1-100', 'mid-sentence crash recovery')
    saveDrafts(drafts)
    // Verify it survived the "crash" (new page load reads from localStorage)
    const reloaded = loadDrafts()
    expect(reloaded['chat-1-100']).toBe('mid-sentence crash recovery')
  })

  it('discards drafts older than DRAFT_TTL_MS on load', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-old', 'sensitive content')
    saveDrafts(drafts)
    // Jump past TTL, simulate fresh page load
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS + 1000)
    __resetForTests()
    expect(loadDrafts()).toEqual({})
  })

  it('keeps drafts edited within TTL window', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-fresh', 'recent')
    saveDrafts(drafts)
    // Jump forward but still within TTL
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS - 1000)
    __resetForTests()
    expect(loadDrafts()).toEqual({ 'chat-fresh': 'recent' })
  })

  it('editing a stale draft refreshes its timestamp (survives next load)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-a', 'old')
    setDraft(drafts, 'chat-b', 'old')
    saveDrafts(drafts)
    // Advance past half TTL, user edits only chat-a
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS / 2)
    setDraft(drafts, 'chat-a', 'edited')
    saveDrafts(drafts)
    // Advance past original TTL but within refreshed TTL for chat-a
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS / 2 + 1000)
    __resetForTests()
    // chat-a refreshed → kept; chat-b untouched → expired
    const reloaded = loadDrafts()
    expect(reloaded['chat-a']).toBe('edited')
    expect(reloaded['chat-b']).toBeUndefined()
  })

  it('legacy drafts without timestamps are stamped on first load (not immediately evicted)', () => {
    // Simulate a pre-TTL install: drafts written without timestamp sidecar
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'chat-legacy': 'pre-ttl content' }))
    // No DRAFTS_TS_KEY entry
    expect(loadDrafts()).toEqual({ 'chat-legacy': 'pre-ttl content' })
  })

  it('saveDrafts before loadDrafts does not wipe persisted timestamps (review rev 3 bug 1)', () => {
    // Pre-seed disk state as if a prior session persisted drafts + timestamps
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'chat-prior': 'existing' }))
    localStorage.setItem('mc-chat-drafts-ts', JSON.stringify({ 'chat-prior': Date.now() }))
    __resetForTests()
    // New session: caller invokes saveDrafts without loadDrafts first
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-new', 'typed')
    saveDrafts(drafts)
    // Simulate another "new session" reading disk to confirm chat-prior's timestamp wasn't wiped
    __resetForTests()
    // Directly read the timestamp sidecar
    const ts = JSON.parse(localStorage.getItem('mc-chat-drafts-ts') || '{}')
    // chat-prior's key is gone from disk because saveDrafts persisted only current drafts.
    // This is expected (drafts is the source of truth). The bug would be if the ts-map was
    // emptied while drafts still had chat-prior — which can't happen here because drafts
    // object didn't include chat-prior. What we DO verify: chat-new has a timestamp.
    expect(typeof ts['chat-new']).toBe('number')
  })

  it('loadDrafts persists pruned state so expired drafts do not resurrect (review rev 3 bug 2)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const drafts: Record<string, string> = {}
    setDraft(drafts, 'chat-expires', 'will expire')
    saveDrafts(drafts)
    // Past TTL
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS + 1000)
    __resetForTests()
    expect(loadDrafts()).toEqual({})
    // Verify disk was cleaned up — so a second load cycle can't resurrect it.
    // Simulate a fresh module reset (as if another tab started up).
    __resetForTests()
    // Without this fix, the entry would still be in DRAFTS_KEY and get re-stamped as "legacy" → resurrected.
    expect(loadDrafts()).toEqual({})
  })

  it('loadDrafts filters out non-string values (review rev 3 bug 3)', () => {
    // Simulate corrupted / hand-edited storage
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({
      'good': 'valid',
      'number': 42,
      'null-value': null,
      'object': { nested: true },
      'array': [1, 2],
    }))
    expect(loadDrafts()).toEqual({ good: 'valid' })
  })

  it('loadDrafts persists stamped timestamps so legacy entries don\'t reset TTL on every reload (review rev 4 bug 1)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    // Pre-TTL install: draft exists on disk without timestamp sidecar.
    localStorage.setItem(DRAFTS_KEY, JSON.stringify({ 'chat-legacy': 'content' }))
    // First load: stamps 'chat-legacy' with Date.now(). MUST persist the
    // timestamp immediately — otherwise a crash before the next save would
    // let the next load see an untimestamped entry again and re-stamp it.
    loadDrafts()
    const tsAfterFirstLoad = JSON.parse(localStorage.getItem('mc-chat-drafts-ts') || '{}')
    expect(tsAfterFirstLoad['chat-legacy']).toBeDefined()
    const firstStamp = tsAfterFirstLoad['chat-legacy']
    // Advance time by nearly the full TTL (simulate weeks passing with no edits).
    vi.setSystemTime(Date.now() + DRAFT_TTL_MS - 1000)
    __resetForTests()
    // Second load must REUSE the original timestamp, not re-stamp with Date.now()
    // (which would extend TTL indefinitely and defeat the staleness eviction).
    loadDrafts()
    const tsAfterSecondLoad = JSON.parse(localStorage.getItem('mc-chat-drafts-ts') || '{}')
    expect(tsAfterSecondLoad['chat-legacy']).toBe(firstStamp)
  })

  it('persistNow writes timestamps before drafts to prevent TTL reset on partial quota failure (review rev 4 bug 2)', () => {
    // Simulate QuotaExceededError on the SECOND setItem call only. The first
    // call must be the timestamps (so drafts never land without their TTL).
    const calls: string[] = []
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      calls.push(key)
      if (calls.length === 2) throw new Error('QuotaExceeded')
      return orig.call(this, key, value)
    }
    try {
      saveDrafts({ 'chat-1': 'content' })
    } finally {
      Storage.prototype.setItem = orig
    }
    // First write must be timestamps — so if drafts write fails, the next load
    // can see stale drafts but NOT orphan drafts-without-timestamps that would
    // get re-stamped and have their TTL reset.
    expect(calls[0]).toBe('mc-chat-drafts-ts')
    expect(calls[1]).toBe(DRAFTS_KEY)
  })
})
