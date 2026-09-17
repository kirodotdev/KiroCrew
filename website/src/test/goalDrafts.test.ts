import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  GOAL_DRAFT_RECORD_PREFIX,
  GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX,
  LEGACY_GOAL_DRAFTS_KEY,
  GOAL_DRAFT_MAX_ENTRIES,
  GOAL_DRAFT_MAX_CYCLES,
  GOAL_DRAFT_MAX_IDLE_SECS,
  GOAL_DRAFT_MAX_MESSAGE_CHARS,
  GOAL_DRAFT_SYNC_TIMEOUT_MS,
  GOAL_DRAFT_TTL_MS,
  clampGoalDraftMessage,
  goalDraftMessageLength,
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

const legacyRetirementKeys = () => Array.from({ length: localStorage.length }, (_, index) => (
  localStorage.key(index)
)).filter((key): key is string => (
  key?.startsWith(GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX) === true
))

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

  it('roundtrips a per-slot goal draft (survives close/re-open/refresh)', () => {
    saveGoalDraft('chat-1-100', draft('finish the migration', 120, 5))
    // New "page load" / popover re-mount reads it straight back.
    expect(loadGoalDraft('chat-1-100')).toEqual(draft('finish the migration', 120, 5))
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

  it('retires a migrated timestamp-less legacy input without rewriting it', () => {
    const slot = 'chat-timestampless-raw'
    const now = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(now)
    const value = draft('timestamp-less raw goal')
    localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({ [slot]: value }))
    const legacyBefore = localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)

    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: value, updatedAt: 0 })
    saveGoalDraft(slot, value, now)
    expect(legacyRetirementKeys()).toHaveLength(1)

    vi.mocked(Date.now).mockReturnValue(now + GOAL_DRAFT_TTL_MS + 1)
    saveGoalDraft(`${slot}-compaction-trigger`, draft('unrelated fresh slot'))
    __resetForTests()
    expect(goalRecordKeys().some(key => key.includes(encodeURIComponent(slot) + ':'))).toBe(false)
    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })
    expect(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)).toBe(legacyBefore)
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
    expect(legacyRetirementKeys()).toEqual([])
    expect(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)).toBe(payload)
  })

  it('retires a timestamped legacy input immediately past the TTL boundary', () => {
    const slot = 'chat-legacy-expiration-boundary'
    const editedAt = Date.now()
    const now = vi.spyOn(Date, 'now')
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [slot]: draft('boundary legacy goal') }),
    )
    localStorage.setItem(
      `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      JSON.stringify({ [slot]: editedAt }),
    )

    now.mockReturnValue(editedAt + GOAL_DRAFT_TTL_MS)
    expect(loadGoalDraftSnapshot(slot)).toEqual({
      draft: draft('boundary legacy goal'),
      updatedAt: editedAt,
    })

    now.mockReturnValue(editedAt + GOAL_DRAFT_TTL_MS + 1)
    __resetForTests()
    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })
    expect(legacyRetirementKeys()).toHaveLength(1)

    // A later backward clock adjustment cannot make the retired input fresh.
    now.mockReturnValue(editedAt + GOAL_DRAFT_TTL_MS - 1_000)
    __resetForTests()
    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })
  })

  it.each([
    { legacy: draft('stale raw draft'), current: null },
    { legacy: { deleted: true }, current: draft('newer v2 draft') },
  ] as const)(
    'does not resurrect legacy opposite-mode state after v2 expires',
    ({ legacy, current }) => {
      const slot = `chat-expired-opposite-${current ? 'draft' : 'clear'}`
      const nowValue = Date.now()
      const legacyStamp = nowValue - 1_000
      const now = vi.spyOn(Date, 'now').mockReturnValue(nowValue)
      localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({ [slot]: legacy }))
      localStorage.setItem(
        `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
        JSON.stringify({ [slot]: legacyStamp }),
      )
      const legacyBefore = localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)

      saveGoalDraft(slot, current, nowValue)
      expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: current, updatedAt: nowValue })

      now.mockReturnValue(nowValue + GOAL_DRAFT_TTL_MS + 1)
      saveGoalDraft(`${slot}-compaction-trigger`, draft('unrelated fresh slot'))
      __resetForTests()
      expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })
      expect(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)).toBe(legacyBefore)
    },
  )

  it('retires a future-skewed stale-tab draft while preserving equal-stamp identity', () => {
    const slot = 'chat-stale-tab-retirement'
    const currentStamp = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(currentStamp)
    saveGoalDraft(slot, null, currentStamp)

    // A pre-upgrade tab keeps writing the raw body and its sidecar; a stamp
    // ahead of the v2 tombstone must not outrank it.
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [slot]: draft('future-skewed stale tab') }),
    )
    localStorage.setItem(
      `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      JSON.stringify({ [slot]: currentStamp + 10_000 }),
    )
    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: currentStamp })

    for (const key of goalRecordKeys()) localStorage.removeItem(key)
    __resetForTests()
    expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })

    // A distinct equal-stamp input is not the retired future-skewed identity;
    // it remains eligible for the existing equal-stamp reissue protocol.
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [slot]: draft('equal-stamp distinct edit') }),
    )
    localStorage.setItem(
      `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      JSON.stringify({ [slot]: currentStamp }),
    )
    __resetForTests()
    expect(loadGoalDraftSnapshot(slot)).toEqual({
      draft: draft('equal-stamp distinct edit'),
      updatedAt: currentStamp,
    })
  })

  it('merges concurrent equal-stamp retirement identities with exact-key compaction', () => {
    const slot = 'chat-retirement-compaction'
    const stamp = Date.now()
    for (const [suffix, value] of [
      ['a', draft('retired equal draft')],
      ['b', { deleted: true }],
    ] as const) {
      localStorage.setItem(
        `${GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX}${suffix}`,
        JSON.stringify({
          __goalDraftLegacyRetirement: 1,
          slot,
          through: stamp,
          timestampLess: false,
          exactInputs: [{ updatedAt: stamp, value }],
        }),
      )
    }

    saveGoalDraft(slot, null, stamp)

    expect(legacyRetirementKeys()).toHaveLength(1)
    const retirement = JSON.parse(localStorage.getItem(legacyRetirementKeys()[0]) || '{}')
    expect(retirement).toMatchObject({ through: stamp, timestampLess: false })
    expect(retirement.exactInputs).toHaveLength(2)
  })

  it('keeps an expired v2 retirement carrier when compact marker persistence fails', () => {
    const slot = 'chat-retirement-quota'
    const nowValue = Date.now()
    const now = vi.spyOn(Date, 'now').mockReturnValue(nowValue)
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [slot]: draft('legacy must stay retired') }),
    )
    localStorage.setItem(
      `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      JSON.stringify({ [slot]: nowValue - 1 }),
    )
    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      if (key.startsWith(GOAL_DRAFT_LEGACY_RETIREMENT_PREFIX)) {
        throw new Error('QuotaExceeded')
      }
      return originalSetItem.call(this, key, value)
    }
    try {
      saveGoalDraft(slot, null, nowValue)
      now.mockReturnValue(nowValue + GOAL_DRAFT_TTL_MS + 1)
      __resetForTests()

      expect(loadGoalDraftSnapshot(slot)).toEqual({ draft: null, updatedAt: 0 })
      expect(goalRecordKeys()).toHaveLength(1)
      expect(legacyRetirementKeys()).toHaveLength(0)
    } finally {
      Storage.prototype.setItem = originalSetItem
    }
  })

  it('gives consecutive quota-failed edits distinct stamps even after storage rollback', () => {
    const slot = 'chat-quota-clock'
    const futureCanonical = Date.now() + 5_000
    saveGoalDraft(slot, draft('canonical'), futureCanonical)

    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function(key: string, value: string) {
      if (isGoalRecordKey(key)) throw new Error('QuotaExceeded')
      return originalSetItem.call(this, key, value)
    }
    try {
      const first = saveGoalDraft(slot, draft('first failed edit'))
      const second = saveGoalDraft(slot, draft('second failed edit'))
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
    expect(record.value).toMatchObject({
      __goalDraftRecord: 2,
      slot: 'chat-1-100',
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
