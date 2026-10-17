import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  GOAL_DRAFT_RECORD_PREFIX,
  LEGACY_GOAL_DRAFTS_KEY,
  GOAL_DRAFT_MAX_ENTRIES,
  GOAL_DRAFT_MAX_CYCLES,
  GOAL_DRAFT_MAX_IDLE_SECS,
  GOAL_DRAFT_MAX_MESSAGE_CHARS,
  GOAL_DRAFT_SYNC_TIMEOUT_MS,
  GOAL_DRAFT_TTL_MS,
  GoalDraftConflict,
  cacheCanonicalGoalDraft,
  clampGoalDraftMessage,
  goalDraftMessageLength,
  enqueueRemoteGoalDraft,
  loadGoalDraft,
  loadGoalDraftSnapshot,
  loadRemoteGoalDraft,
  latestLocalGoalDraft,
  saveGoalDraft,
  savePendingGoalDraft,
  saveRemoteGoalDraft,
  __resetForTests,
} from '../utils/goalDrafts'
import { safeSetItem } from '../utils/safeStorage'

const draft = (message: string, idleSecs = 60, maxCycles = 0) => ({ message, idleSecs, maxCycles })

const goalRecordKeys = () => Array.from({ length: localStorage.length }, (_, index) => (
  localStorage.key(index)
)).filter((key): key is string => key?.startsWith(GOAL_DRAFT_RECORD_PREFIX) === true)

const goalRecords = () => goalRecordKeys().map(key => ({
  key,
  value: JSON.parse(localStorage.getItem(key) || '{}') as Record<string, unknown>,
}))

const isGoalRecordKey = (key: string) => key.startsWith(GOAL_DRAFT_RECORD_PREFIX)

describe('goalDrafts', () => {
  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('keeps distinct module-owned pending intent over equal-stamp durable content', () => {
    const slot = 'chat-equal-pending'
    const stamp = Date.now()
    saveGoalDraft(slot, draft('durable other-tab value'), stamp)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceeded')
    })

    const pending = savePendingGoalDraft(slot, draft('this-tab unsynced intent'))
    setItem.mockRestore()
    saveGoalDraft(slot, draft('other-tab equal-stamp value'), pending.updatedAt)

    expect(pending).toEqual({
      draft: draft('this-tab unsynced intent'),
      updatedAt: pending.updatedAt,
      persisted: false,
    })
    expect(pending.updatedAt).toBeGreaterThan(stamp)
    expect(latestLocalGoalDraft(slot)).toEqual(pending)
  })

  it('retires an older quota fallback after a fresh durable edit supersedes it', () => {
    const slot = 'chat-superseded-fallback'
    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      if (isGoalRecordKey(key)) throw new Error('QuotaExceeded')
      return originalSetItem.call(this, key, value)
    }
    const older = savePendingGoalDraft(slot, draft('older quota-failed edit'), Date.now())
    Storage.prototype.setItem = originalSetItem

    const newer = savePendingGoalDraft(slot, draft('fresh durable edit'), older.updatedAt + 1)

    expect(latestLocalGoalDraft(slot)).toEqual(newer)
    expect(loadGoalDraftSnapshot(slot)).toEqual(newer)
  })

  it('advances a fresh edit past retained canonical and durable timestamps', () => {
    const slot = 'chat-retained-canonical'
    const durableStamp = Date.now() + 5_000
    const submitted = saveGoalDraft(slot, draft('future-stamped submitted edit'), durableStamp)
    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      if (isGoalRecordKey(key)) throw new Error('QuotaExceeded')
      return originalSetItem.call(this, key, value)
    }
    const canonicalStamp = Date.now()
    try {
      expect(cacheCanonicalGoalDraft(slot, submitted, {
        draft: draft('server-confirmed value'),
        updatedAt: canonicalStamp,
      })).toBe('retained')
    } finally {
      Storage.prototype.setItem = originalSetItem
    }

    const fresh = savePendingGoalDraft(slot, draft('fresh edit'))
    const [record] = goalRecords()
    expect(fresh.updatedAt).toBeGreaterThan(durableStamp)
    expect(record.value.baseUpdatedAt).toBe(canonicalStamp)
    expect(latestLocalGoalDraft(slot)).toEqual(fresh)
  })

  it('roundtrips a per-slot goal draft (survives close/re-open/refresh)', () => {
    saveGoalDraft('chat-1-100', draft('finish the migration', 120, 5))
    // New "page load" / popover re-mount reads it straight back.
    expect(loadGoalDraft('chat-1-100')).toEqual(draft('finish the migration', 120, 5))
  })

  it('commits a pending draft and its base in one storage write', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_800_000_000_000)
    const slot = 'chat-atomic-record'
    const loaded = saveGoalDraft(slot, draft('loaded canonical'), Date.now())
    expect(cacheCanonicalGoalDraft(slot, loaded, loaded)).toBe('persisted')

    const originalSetItem = Storage.prototype.setItem
    let goalWrites = 0
    Storage.prototype.setItem = function(key: string, value: string) {
      if (key.startsWith('mc-goal-drafts')) {
        goalWrites += 1
        if (goalWrites === 2) throw new Error('interrupted second write')
      }
      return originalSetItem.call(this, key, value)
    }
    let pending: ReturnType<typeof savePendingGoalDraft>
    try {
      pending = savePendingGoalDraft(slot, draft('pending edit'))
    } finally {
      Storage.prototype.setItem = originalSetItem
    }

    expect(goalWrites).toBe(1)
    expect(loadGoalDraftSnapshot(slot)).toEqual(pending)
    const [record] = goalRecords()
    expect(record.value).toMatchObject({
      updatedAt: pending.updatedAt,
      baseUpdatedAt: loaded.updatedAt,
      value: draft('pending edit'),
    })
  })

  it('preserves an explicit server timestamp in the local fallback', () => {
    const stamp = Date.now() - 1_000
    saveGoalDraft('chat-1-100', draft('server goal', 90, 4), stamp)
    expect(loadGoalDraftSnapshot('chat-1-100')).toEqual({
      draft: draft('server goal', 90, 4),
      updatedAt: stamp,
    })
  })

  it.each(['toString', 'constructor', 'hasOwnProperty'])(
    'allocates finite monotonic timestamps for inherited slot name %s',
    slot => {
      vi.useFakeTimers()
      vi.setSystemTime(1_800_000_000_000)
      const serverStamp = Date.now() + 1_000
      saveGoalDraft(slot, draft('server goal'), serverStamp)

      const edited = saveGoalDraft(slot, draft('newer local edit'))
      expect(edited.updatedAt).toBe(serverStamp + 1)
      expect(Number.isFinite(edited.updatedAt)).toBe(true)
      expect(loadGoalDraftSnapshot(slot)).toEqual(edited)
    },
  )

  it('reads a legacy body and sidecar without rewriting either key', () => {
    const slot = 'chat-cross-tab'
    const stamp = Date.now()
    localStorage.setItem(
      `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      JSON.stringify({ [slot]: stamp }),
    )
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [slot]: draft('legacy tab') }),
    )
    const bodyBefore = localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)
    const timestampsBefore = localStorage.getItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`)

    expect(loadGoalDraftSnapshot(slot)).toEqual({
      draft: draft('legacy tab'),
      updatedAt: stamp,
    })
    expect(goalRecordKeys()).toEqual([])
    expect(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)).toBe(bodyBefore)
    expect(localStorage.getItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`)).toBe(timestampsBefore)
  })

  it('ignores an envelope-shaped legacy body that no released client ever wrote', () => {
    // Every shipped writer of `mc-goal-drafts` stores a bare `Record<slot, draft>`
    // with its timestamps in the `-ts` sidecar. A wrapped `{ drafts, timestamps }`
    // object is therefore not a legacy input: reading it as one would resurrect a
    // shape nothing produces, so the reader treats it as an unknown slot map.
    const slot = 'chat-envelope-shape'
    const payload = JSON.stringify({
      __slotDraftStore: 1,
      drafts: { [slot]: draft('never-shipped envelope goal') },
      timestamps: { [slot]: Date.now() },
    })
    localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, payload)

    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })
    expect(goalRecordKeys()).toEqual([])
    expect(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)).toBe(payload)
  })

  it('gives consecutive quota-failed edits distinct stamps even after storage rollback', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_800_000_000_000)
    const slot = 'chat-quota-clock'
    const futureCanonical = Date.now() + 5_000
    saveGoalDraft(slot, draft('canonical'), futureCanonical)

    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      if (isGoalRecordKey(key)) throw new Error('QuotaExceeded')
      return originalSetItem.call(this, key, value)
    }
    try {
      const first = savePendingGoalDraft(slot, draft('first failed edit'))
      const second = savePendingGoalDraft(slot, draft('second failed edit'))
      expect(first.persisted).toBe(false)
      expect(second.persisted).toBe(false)
      expect(first.updatedAt).toBe(futureCanonical + 1)
      expect(second.updatedAt).toBe(futureCanonical + 2)
    } finally {
      Storage.prototype.setItem = originalSetItem
    }
  })

  it.each([
    {
      name: 'a stale clear cannot erase a newer draft',
      newer: draft('newer unsynced draft'),
      stale: null,
    },
    {
      name: 'a stale draft cannot resurrect a newer tombstone',
      newer: null,
      stale: draft('stale resurrected draft'),
    },
  ])('$name', ({ newer, stale }) => {
    const slot = 'chat-opposite-modes'
    const staleStamp = Date.now()
    const newerStamp = staleStamp + 5_000
    saveGoalDraft(slot, newer, newerStamp)

    const staleResult = saveGoalDraft(slot, stale, staleStamp)

    expect(staleResult).toEqual({ draft: newer, updatedAt: newerStamp })
    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: newer, updatedAt: newerStamp })
    expect(goalRecords()).toHaveLength(1)
  })

  it('repairs malformed v2 records on the next explicit save', () => {
    const malformedKey = `${GOAL_DRAFT_RECORD_PREFIX}malformed`
    localStorage.setItem(malformedKey, '{not-json')

    saveGoalDraft('constructor', draft('repaired prototype-key goal'), Date.now())

    expect(localStorage.getItem(malformedKey)).toBeNull()
    expect(loadGoalDraft('constructor')).toEqual(draft('repaired prototype-key goal'))
  })

  it('reads every immutable primary record at most once per snapshot scan', () => {
    saveGoalDraft('chat-read-once', draft('one atomic record'), Date.now())
    const [recordKey] = goalRecordKeys()
    const originalGetItem = Storage.prototype.getItem
    let recordReads = 0
    Storage.prototype.getItem = function(key: string) {
      if (key === recordKey) recordReads += 1
      return originalGetItem.call(this, key)
    }
    try {
      expect(loadGoalDraft('chat-read-once')).toEqual(draft('one atomic record'))
    } finally {
      Storage.prototype.getItem = originalGetItem
    }
    expect(recordReads).toBe(1)
  })

  it('translates the canonical server wire format in both directions', async () => {
    const stamp = Date.now()
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body))
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'remote goal', idle_secs: 120, max_cycles: 7 },
          updated_at: stamp,
        }),
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await loadRemoteGoalDraft('chat/1')).toEqual({
        draft: draft('remote goal', 120, 7),
        updatedAt: stamp,
      })
      const snapshot = { draft: draft('local goal', 45, 2), updatedAt: stamp + 1 }
      expect(await saveRemoteGoalDraft('chat/1', snapshot)).toEqual(snapshot)
      expect(fetchMock.mock.calls[0][0]).toBe('/api/autonudge/draft/slot/chat%2F1')
      expect(fetchMock.mock.calls[1][0]).toBe('/api/autonudge/draft/slot/chat%2F1')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('does not rebase an edit onto a GET response the editor never adopted', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_800_000_000_000)
    const slot = 'chat-delayed-get-base'
    const loaded = saveGoalDraft(slot, draft('loaded goal'), Date.now())
    expect(cacheCanonicalGoalDraft(slot, loaded, loaded)).toBe('persisted')
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        draft: { message: 'unseen canonical', idle_secs: 60, max_cycles: 0 },
        updated_at: loaded.updatedAt + 100,
      }),
    })))
    await loadRemoteGoalDraft(slot)
    const edited = savePendingGoalDraft(slot, draft('local edit'))
    const writer = vi.fn(async (_slot: string, snapshot: typeof edited) => snapshot)

    await enqueueRemoteGoalDraft(slot, edited, writer)

    expect(writer).toHaveBeenCalledWith(
      slot,
      edited,
      expect.objectContaining({ baseUpdatedAt: loaded.updatedAt }),
    )
  })

  it('sends the canonical base and surfaces a 409 as a goal-draft conflict', async () => {
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => {
      const sent = JSON.parse(String(init?.body))
      expect(sent.base_updated_at).toBe(41)
      return Promise.resolve({
        ok: false,
        status: 409,
        json: () => Promise.resolve({
          code: 'goal_draft_conflict',
          error: 'goal draft changed since it was loaded',
          current: { draft: { message: 'newer', idle_secs: 60, max_cycles: 0 }, updated_at: 42 },
        }),
      })
    }))

    await expect(saveRemoteGoalDraft(
      'chat-conflict',
      { draft: draft('stale'), updatedAt: 43 },
      { baseUpdatedAt: 41 },
    )).rejects.toMatchObject({
      name: 'GoalDraftConflict',
      current: { draft: draft('newer'), updatedAt: 42 },
    } satisfies Partial<GoalDraftConflict>)
  })

  it('bounds a remote request instead of leaving reconciliation pending forever', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
    })))

    const rejection = expect(loadRemoteGoalDraft('chat-1')).rejects.toThrow('aborted')
    await vi.advanceTimersByTimeAsync(GOAL_DRAFT_SYNC_TIMEOUT_MS)
    await rejection
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

  it('loads null after a clear or blank message (never pins the default)', () => {
    saveGoalDraft('chat-1-100', draft('typed goal'))
    expect(loadGoalDraft('chat-1-100')).not.toBeNull()
    saveGoalDraft('chat-1-100', null)
    expect(loadGoalDraft('chat-1-100')).toBeNull()
    // Whitespace-only message is also treated as empty.
    saveGoalDraft('chat-1-100', draft('   '))
    expect(loadGoalDraft('chat-1-100')).toBeNull()
  })

  it('retains a clear tombstone timestamp across reloads', () => {
    const stamp = Date.now()
    saveGoalDraft('chat-1-100', draft('goal to clear'), stamp - 1)
    saveGoalDraft('chat-1-100', null, stamp)
    __resetForTests()

    expect(loadGoalDraftSnapshot('chat-1-100')).toEqual({
      draft: null,
      updatedAt: stamp,
    })
    const [record] = goalRecords()
    expect(record.value).toEqual({
      updatedAt: stamp,
      value: { deleted: true },
    })
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
    safeSetItem(LEGACY_GOAL_DRAFTS_KEY, 'not json'); __resetForTests()
    expect(loadGoalDraft('x')).toBeNull()
    safeSetItem(LEGACY_GOAL_DRAFTS_KEY, '[]'); __resetForTests()
    expect(loadGoalDraft('x')).toBeNull()
    safeSetItem(LEGACY_GOAL_DRAFTS_KEY, 'null'); __resetForTests()
    expect(loadGoalDraft('x')).toBeNull()
  })

  it('filters out entries with wrong-typed fields (corruption / hand-edit)', () => {
    // safeSetItem writes the serialized string verbatim, so it can still seed
    // wrong-typed fixture members — proving the sanitizer drops each of them.
    safeSetItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({
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

  it('normalizes locally accepted numbers to the canonical server bounds', () => {
    safeSetItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({
      high: { message: 'bounded high', idleSecs: 172_800, maxCycles: 2_147_483_648 },
      low: { message: 'bounded low', idleSecs: -5, maxCycles: -2 },
    }))
    __resetForTests()

    expect(loadGoalDraft('high')).toEqual({
      message: 'bounded high',
      idleSecs: GOAL_DRAFT_MAX_IDLE_SECS,
      maxCycles: GOAL_DRAFT_MAX_CYCLES,
    })
    expect(loadGoalDraft('low')).toEqual({
      message: 'bounded low',
      idleSecs: 15,
      maxCycles: 0,
    })
  })

  it.each([7_999, 8_000, 8_001])(
    'stores an ASCII goal of %i characters within the shared server limit',
    length => {
      const snapshot = saveGoalDraft(
        `chat-message-${length}`,
        draft('x'.repeat(length)),
        Date.now(),
      )
      const expectedLength = Math.min(length, GOAL_DRAFT_MAX_MESSAGE_CHARS)
      expect(snapshot.draft?.message).toBe('x'.repeat(expectedLength))
      expect(goalDraftMessageLength(snapshot.draft?.message ?? '')).toBe(expectedLength)
    },
  )

  it('counts and clamps multibyte goals by Unicode code point like the server', async () => {
    const overLimit = '😀'.repeat(GOAL_DRAFT_MAX_MESSAGE_CHARS + 1)
    expect(overLimit.length).toBe((GOAL_DRAFT_MAX_MESSAGE_CHARS + 1) * 2)
    expect(goalDraftMessageLength(overLimit)).toBe(GOAL_DRAFT_MAX_MESSAGE_CHARS + 1)
    expect(clampGoalDraftMessage(overLimit)).toBe('😀'.repeat(GOAL_DRAFT_MAX_MESSAGE_CHARS))

    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      const sent = JSON.parse(String(init?.body))
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      await saveRemoteGoalDraft('chat-unicode-limit', {
        draft: draft(overLimit),
        updatedAt: Date.now(),
      })
      const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body))
      expect(body.draft.message).toBe('😀'.repeat(GOAL_DRAFT_MAX_MESSAGE_CHARS))
      expect(goalDraftMessageLength(body.draft.message)).toBe(GOAL_DRAFT_MAX_MESSAGE_CHARS)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('live edits advance past the previous canonical timestamp despite a lagging clock', () => {
    const serverStamp = Date.now() + 60_000
    saveGoalDraft('chat-clock', draft('canonical'), serverStamp)
    vi.spyOn(Date, 'now').mockReturnValue(serverStamp - 30_000)

    expect(saveGoalDraft('chat-clock', draft('newer local edit'))).toEqual({
      draft: draft('newer local edit'),
      updatedAt: serverStamp + 1,
    })
  })

  it('saveGoalDraft reports QuotaExceededError without throwing', () => {
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = () => { throw new Error('QuotaExceeded') }
    try {
      expect(saveGoalDraft('chat-1-100', draft('x'))).toMatchObject({
        draft: draft('x'),
        persisted: false,
      })
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

  it('retires only the evicted slot from legacy storage before removing its tombstone', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_800_000_000_000)
    const victim = 'legacy-cleared-victim'
    const survivor = 'legacy-survivor'
    localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({
      [victim]: draft('must stay cleared'),
      [survivor]: draft('keep this legacy slot'),
    }))
    localStorage.setItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`, JSON.stringify({
      [victim]: Date.now() - 1,
      [survivor]: Date.now() - 1,
    }))
    saveGoalDraft(victim, null, Date.now())
    for (let i = 0; i < GOAL_DRAFT_MAX_ENTRIES; i++) {
      saveGoalDraft(`new-slot-${i}`, draft(`new goal ${i}`), Date.now() + i + 1)
    }

    expect(loadGoalDraft(victim)).toBeNull()
    const legacy = JSON.parse(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY) || '{}')
    expect(legacy).not.toHaveProperty(victim)
    expect(legacy[survivor]).toEqual(draft('keep this legacy slot'))
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
