import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { TAB_ID } from '../api/tabId'
import {
  GOAL_DRAFTS_KEY,
  GOAL_DRAFT_MAX_CYCLES,
  GOAL_DRAFT_MAX_ENTRIES,
  GOAL_DRAFT_MAX_IDLE_SECS,
  GOAL_DRAFT_MAX_MESSAGE_CHARS,
  GOAL_DRAFT_SYNC_TIMEOUT_MS,
  GOAL_DRAFT_TTL_MS,
  LEGACY_GOAL_DRAFTS_KEY,
  GoalDraftConflict,
  __resetForTests,
  adoptGoalDraft,
  clampGoalDraftMessage,
  confirmedGoalDraft,
  deleteLegacyGoalDraft,
  loadLegacyGoalDraft,
  loadPendingGoalDraft,
  loadRemoteGoalDraft,
  saveRemoteGoalDraft,
  savePendingGoalDraft,
  syncGoalDraft,
} from '../utils/goalDrafts'

// Each pending record names the tab that wrote it; a fixed id keeps its checks exact.
vi.mock('../api/tabId', () => ({ TAB_ID: 'tab-under-test' }))

const SLOT = 'chat-1-100'
const draft = (message: string, idleSecs = 60, maxCycles = 0) => ({ message, idleSecs, maxCycles })
const wire = (message: string) => ({ message, idle_secs: 60, max_cycles: 0 })
const ok = (body: unknown) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) })

/** Stub the draft PUT: `answer` maps the sent body to the response. */
function stubPut(answer: (body: { base_updated_at: number, draft: unknown }) => Promise<unknown>) {
  const puts: Array<{ base_updated_at: number, draft: unknown }> = []
  vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as { base_updated_at: number, draft: unknown }
    puts.push(body)
    return answer(body)
  }) as unknown as typeof fetch)
  return puts
}

describe('goalDrafts', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  describe('the pending edit', () => {
    it('keeps one latest unconfirmed edit per slot, with its base, across reloads', () => {
      savePendingGoalDraft(SLOT, draft('first'), 4)
      savePendingGoalDraft(SLOT, draft('second'))
      savePendingGoalDraft('chat-2-200', null, 9)
      __resetForTests()

      expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: draft('second'), base: 4, tab: TAB_ID })
      expect(loadPendingGoalDraft('chat-2-200')).toEqual({ draft: null, base: 9, tab: TAB_ID })
      expect(Object.keys(JSON.parse(localStorage.getItem(GOAL_DRAFTS_KEY)!))).toHaveLength(2)
    })

    it('an edit with no pending record is made on the version this tab confirmed', () => {
      adoptGoalDraft(SLOT, { draft: draft('shown'), updatedAt: 7 })
      expect(savePendingGoalDraft(SLOT, draft('typed'))).toEqual({ draft: draft('typed'), base: 7, tab: TAB_ID })
    })

    it('a reloaded tab builds its next edit on the edit it restored, on that edit\'s version', async () => {
      savePendingGoalDraft(SLOT, draft('typed before the reload'), 4)
      // A reload is a new page, with a new tab id.
      vi.resetModules()
      vi.doMock('../api/tabId', () => ({ TAB_ID: 'tab-after-reload' }))
      const reloaded = await import('../utils/goalDrafts')
      expect(reloaded.savePendingGoalDraft(SLOT, draft('typed after the reload')))
        .toEqual({ draft: draft('typed after the reload'), base: 4, tab: 'tab-after-reload' })
    })

    it('normalizes accepted values to the server bounds and drops corrupt records', () => {
      const long = '😀'.repeat(GOAL_DRAFT_MAX_MESSAGE_CHARS + 3)
      savePendingGoalDraft(SLOT, { message: long, idleSecs: 1, maxCycles: GOAL_DRAFT_MAX_CYCLES + 5 }, 0)
      const saved = loadPendingGoalDraft(SLOT)!
      expect(Array.from(saved.draft!.message)).toHaveLength(GOAL_DRAFT_MAX_MESSAGE_CHARS)
      expect(saved.draft).toMatchObject({ idleSecs: 15, maxCycles: GOAL_DRAFT_MAX_CYCLES })
      expect(clampGoalDraftMessage('short')).toBe('short')

      localStorage.setItem(GOAL_DRAFTS_KEY, JSON.stringify({
        a: { draft: { message: 'x', idleSecs: GOAL_DRAFT_MAX_IDLE_SECS * 2, maxCycles: 0 }, base: 1 },
        b: { draft: { message: '   ', idleSecs: 60, maxCycles: 0 }, base: 1 },
        c: { draft: null },
        d: 'nope',
      }))
      __resetForTests()
      expect(loadPendingGoalDraft('a')?.draft?.idleSecs).toBe(GOAL_DRAFT_MAX_IDLE_SECS)
      expect(loadPendingGoalDraft('b')).toBeNull()
      expect(loadPendingGoalDraft('c')).toBeNull()
      expect(loadPendingGoalDraft('d')).toBeNull()
    })

    it('shares the chat drafts TTL and slot cap', () => {
      vi.useFakeTimers()
      for (let index = 0; index <= GOAL_DRAFT_MAX_ENTRIES; index += 1) savePendingGoalDraft(`slot-${index}`, draft(`goal ${index}`), 0)
      expect(loadPendingGoalDraft('slot-0')).toBeNull()
      expect(loadPendingGoalDraft(`slot-${GOAL_DRAFT_MAX_ENTRIES}`)).not.toBeNull()
      vi.advanceTimersByTime(GOAL_DRAFT_TTL_MS + 1)
      expect(loadPendingGoalDraft(`slot-${GOAL_DRAFT_MAX_ENTRIES}`)).toBeNull()
    })
  })

  describe('the pre-sync browser draft', () => {
    it('reads the shape main wrote and deletes only this slot, sidecar included', () => {
      localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({ [SLOT]: draft('old goal', 90, 2), other: draft('other goal') }))
      localStorage.setItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`, JSON.stringify({ [SLOT]: Date.now(), other: Date.now() })) // flake-ok: fresh stamps, compared only to the TTL

      expect(loadLegacyGoalDraft(SLOT)).toEqual(draft('old goal', 90, 2))
      deleteLegacyGoalDraft(SLOT)

      expect(loadLegacyGoalDraft(SLOT)).toBeNull()
      expect(loadLegacyGoalDraft('other')).toEqual(draft('other goal'))
      expect(Object.keys(JSON.parse(localStorage.getItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`)!))).toEqual(['other'])
    })
  })

  describe('syncGoalDraft', () => {
    it('sends the wire format with the pending base and confirms the answer', async () => {
      savePendingGoalDraft(SLOT, draft('typed'), 4)
      const puts = stubPut(() => ok({ draft: wire('typed'), updated_at: 8 }))

      const outcome = await syncGoalDraft(SLOT, draft('typed'))

      expect(puts).toEqual([{ base_updated_at: 4, draft: wire('typed') }])
      expect(outcome).toEqual({ result: 'persisted', canonical: { draft: draft('typed'), updatedAt: 8 } })
      expect(confirmedGoalDraft(SLOT)).toEqual({ draft: draft('typed'), updatedAt: 8 })
      expect(loadPendingGoalDraft(SLOT)).toBeNull()
    })

    it('an edit typed after the save stays pending, now made on the answer', async () => {
      savePendingGoalDraft(SLOT, draft('sent'), 4)
      stubPut(() => {
        savePendingGoalDraft(SLOT, draft('typed meanwhile'))
        return ok({ draft: wire('sent'), updated_at: 8 })
      })

      await syncGoalDraft(SLOT, draft('sent'))

      expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: draft('typed meanwhile'), base: 8, tab: TAB_ID })
    })

    it('a 409 is a conflict naming the stored record, and the edit stays pending on its base', async () => {
      savePendingGoalDraft(SLOT, draft('typed'), 4)
      stubPut(() => Promise.resolve({
        ok: false,
        status: 409,
        json: () => Promise.resolve({ code: 'goal_draft_conflict', current: { draft: wire('other device'), updated_at: 9 } }),
      }))

      const outcome = await syncGoalDraft(SLOT, draft('typed'))

      expect(outcome).toEqual({ result: 'conflict', canonical: { draft: draft('other device'), updatedAt: 9 } })
      expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: draft('typed'), base: 4, tab: TAB_ID })
      expect(confirmedGoalDraft(SLOT)).toBeUndefined()
    })

    it('a failed save is remote-failed and leaves the edit pending', async () => {
      savePendingGoalDraft(SLOT, draft('typed'), 4)
      stubPut(() => Promise.reject(new TypeError('Failed to fetch')))

      expect(await syncGoalDraft(SLOT, draft('typed'))).toEqual({ result: 'remote-failed' })
      expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: draft('typed'), base: 4, tab: TAB_ID })
    })

    it('queues writes per slot, so the second goes out on the first answer', async () => {
      let stamp = 4
      let release = () => {}
      const held = new Promise<void>(resolve => { release = resolve })
      const puts = stubPut(async body => {
        if (puts.length === 1) await held
        stamp += 1
        return ok({ draft: body.draft, updated_at: stamp })
      })
      savePendingGoalDraft(SLOT, draft('one'), 4)
      const first = syncGoalDraft(SLOT, draft('one'))
      savePendingGoalDraft(SLOT, draft('two'))
      const second = syncGoalDraft(SLOT, draft('two'))
      await Promise.resolve()
      expect(puts).toHaveLength(1)

      release()
      await Promise.all([first, second])

      expect(puts.map(put => put.base_updated_at)).toEqual([4, 5])
      expect(confirmedGoalDraft(SLOT)?.updatedAt).toBe(6)
      expect(loadPendingGoalDraft(SLOT)).toBeNull()
    })

    it('an explicit base wins over the pending one', async () => {
      savePendingGoalDraft(SLOT, draft('mine'), 4)
      const puts = stubPut(() => ok({ draft: wire('mine'), updated_at: 10 }))
      await syncGoalDraft(SLOT, draft('mine'), { base: 9 })
      expect(puts[0].base_updated_at).toBe(9)
    })
  })

  describe('remote requests', () => {
    it('reads the canonical record and surfaces a 409 write as a GoalDraftConflict', async () => {
      vi.stubGlobal('fetch', vi.fn(() => ok({ draft: null, updated_at: 3 })) as unknown as typeof fetch)
      expect(await loadRemoteGoalDraft(SLOT)).toEqual({ draft: null, updatedAt: 3 })

      vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
        ok: false, status: 409, json: () => Promise.resolve({ code: 'goal_draft_conflict', current: 'malformed' }),
      })) as unknown as typeof fetch)
      const refused = await saveRemoteGoalDraft(SLOT, draft('x'), 0).catch((error: unknown) => error)
      expect(refused).toBeInstanceOf(GoalDraftConflict)
      expect((refused as GoalDraftConflict).current).toBeNull()
    })

    it('ends a request whose body stalls after its headers at the sync deadline', async () => {
      vi.useFakeTimers()
      vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, status: 200, json: () => new Promise(() => {}) })) as unknown as typeof fetch)
      const read = loadRemoteGoalDraft(SLOT).then(() => 'answered', () => 'failed')
      await vi.advanceTimersByTimeAsync(GOAL_DRAFT_SYNC_TIMEOUT_MS + 1)
      expect(await read).toBe('failed')
    })
  })
})
