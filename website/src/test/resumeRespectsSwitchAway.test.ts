/**
 * A resume from Older sessions (or any other resume entry point) that answers
 * after the user moved must leave the user where they are, the rule creates
 * and forks follow (`forkMayOpen`): another session opened meanwhile, or a
 * return to the origin, is a choice the late answer must not undo. The resumed
 * session is still listed as an open tab and leaves Older sessions.
 *
 * Two resumes in a row: the later click wins, so an earlier resume answering
 * after the later one does not take the view back. A repeat of a resume on its
 * way sends nothing, also with another row resumed in between, until the
 * pending-open lease ends. While a resume is pending the chat page knows one
 * is on its way (`pendingResumes`), as it knows of a pending create or fork.
 *
 * The real `resumeFromHistory` thunk runs against a deferred
 * `api.resumeChatSlot`, one per history key.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ resumeChatSlot: vi.fn() }))
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { dropActiveSlot, PENDING_OPEN_LEASE_MS, resumeFromHistory, setActiveSlot, switchSlot } from '../store/chatSlice'

const ORIGIN = 'chat-resume-origin'
const OTHER = 'chat-resume-other'
const R1 = 'chat-resume-r1'
const R2 = 'chat-resume-r2'

// The deferred answers per history key, oldest first; `land(key)` resolves the
// oldest one still open.
const pending = new Map<string, Array<(v: unknown) => void>>()
function land(key: string) {
  const resolve = pending.get(key)?.shift()
  if (!resolve) throw new Error(`no resume pending for ${key}`)
  resolve({ ok: true, key, mode: 'orchestrator', messages: [] })
}
const posts = (key: string) => mocks.resumeChatSlot.mock.calls.filter(c => c[0] === key).length
const pendingKeys = () => Object.values(store.getState().chat.pendingResumes ?? {}).map(p => p.key).sort()

const activeSlot = () => store.getState().chat.activeSlot
const listed = (key: string) => store.getState().dashboard.slots.some(s => s.key === key)
const inHistory = (key: string) => store.getState().chat.history.some(s => s.key === key)
const resume = (key: string) => store.dispatch(resumeFromHistory({ key, title: key }))

beforeEach(() => {
  mocks.resumeChatSlot.mockImplementation((key: string) => new Promise(r => { pending.set(key, [...(pending.get(key) ?? []), r]) }))
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ORIGIN), slot(OTHER)]))
  store.dispatch(setActiveSlot(ORIGIN))
  store.dispatch({ type: 'chat/fetchHistory/fulfilled', payload: { sessions: [R1, R2].map(key => ({ key, title: key })), hasMore: false, offset: 0, append: false } })
})
afterEach(async () => {
  vi.useRealTimers()
  // Settle every resume a test left open, so no entry leaks into the next.
  for (const [key, list] of pending) while (list.length) land(key)
  await vi.waitFor(() => expect(pendingKeys()).toEqual([]))
  vi.clearAllMocks()
  pending.clear()
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

describe('resumeFromHistory resolving after the user moved', () => {
  it('leaves the user on the session they opened while the resume ran', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(OTHER)
    expect(listed(R1)).toBe(true)
    expect(inHistory(R1)).toBe(false)
  })

  it('leaves the user on the origin when they went away and came back while the resume ran', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    store.dispatch(setActiveSlot(ORIGIN))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(ORIGIN)
    expect(listed(R1)).toBe(true)
  })

  it('opens the later of two resumes when the earlier one answers last', async () => {
    const first = resume(R1)
    const second = resume(R2)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalledTimes(2))
    land(R2)
    await second
    expect(activeSlot()).toBe(R2)
    land(R1)
    await first

    expect(activeSlot()).toBe(R2)
    expect(listed(R1)).toBe(true)
    expect(inHistory(R1)).toBe(false)
  })

  it('sends one resume for a repeated click on the row on its way', async () => {
    const resumed = resume(R1)
    void resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    expect(mocks.resumeChatSlot).toHaveBeenCalledTimes(1)
    expect(pendingKeys()).toEqual([R1])
    land(R1)
    await resumed

    expect(activeSlot()).toBe(R1)
    expect(pendingKeys()).toEqual([])
  })

  it('clears the pending resume when it fails', async () => {
    mocks.resumeChatSlot.mockRejectedValueOnce(new Error('boom'))
    await resume(R1)

    expect(pendingKeys()).toEqual([])
    expect(activeSlot()).toBe(ORIGIN)
  })

  // The chat page lost the origin and moved the view itself: no user choice,
  // so the resumed session still opens.
  it('opens the session when the page itself dropped the origin and auto-selected another session', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(dropActiveSlot())
    void store.dispatch(switchSlot({ key: OTHER, automatic: true }))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(R1)
  })

  it('opens the session when the user stayed on the origin', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(ORIGIN))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(R1)
    expect(inHistory(R1)).toBe(false)
  })

  // A resume of another row in between must not reopen the guard: each
  // pending resume holds its own entry.
  it('sends one resume for H1, H2, H1 while the first H1 is still on its way, and opens H1', async () => {
    const first = resume(R1)
    const second = resume(R2)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalledTimes(2))
    land(R2)
    await second
    expect(activeSlot()).toBe(R2)
    expect(pendingKeys()).toEqual([R1])

    const repeat = await resume(R1)
    expect(resumeFromHistory.rejected.match(repeat) && repeat.meta.condition).toBe(true)
    expect(posts(R1)).toBe(1)
    expect(store.getState().chat.unresumableResume).toBeNull()

    // The repeat was the user's newest click, so the H1 already on its way
    // opens when it answers, from R2 where they now stand.
    land(R1)
    await first
    expect(activeSlot()).toBe(R1)
    expect(pendingKeys()).toEqual([])
  })

  it('does not open H1 when the user moved after the repeated H1 click', async () => {
    const first = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalledTimes(1))
    await resume(R1)
    store.dispatch(setActiveSlot(OTHER))
    land(R1)
    await first

    expect(activeSlot()).toBe(OTHER)
    expect(listed(R1)).toBe(true)
  })
})

describe('the repeat guard is bounded by the pending-open lease', () => {
  // The resume POST carries no deadline. Only Date is faked, and set by hand,
  // so the guard's age is exact. No `vi.waitFor` here: with fake timers on it
  // advances the clock on every poll.
  const T0 = new Date('2026-10-22T12:00:00Z').getTime()
  const at = (ms: number) => vi.setSystemTime(T0 + ms)
  const settle = () => new Promise(r => setTimeout(r, 0))
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    at(0)
  })

  it('drops a repeat until the lease ends, then sends a fresh resume for a hung one', async () => {
    const hung = resume(R1)
    await settle()
    expect(posts(R1)).toBe(1)

    at(PENDING_OPEN_LEASE_MS - 1)
    await resume(R1)
    expect(posts(R1)).toBe(1)

    at(PENDING_OPEN_LEASE_MS)
    const fresh = resume(R1)
    await settle()
    expect(posts(R1)).toBe(2)
    // The hung entry is dropped when the fresh one starts; only the fresh one
    // is on its way.
    expect(pendingKeys()).toEqual([R1])

    // The fresh one is the latest: the hung one answering first lists the
    // session without opening it, and the fresh one opens it.
    land(R1)
    await hung
    expect(activeSlot()).toBe(ORIGIN)
    expect(listed(R1)).toBe(true)
    land(R1)
    await fresh
    expect(activeSlot()).toBe(R1)
    expect(pendingKeys()).toEqual([])
  })

  it('a late answer from a hung resume does not open once a newer resume ran', async () => {
    const hung = resume(R1)
    await settle()
    at(PENDING_OPEN_LEASE_MS)
    const later = resume(R2)
    await settle()
    expect(posts(R2)).toBe(1)
    land(R2)
    await later
    expect(activeSlot()).toBe(R2)

    land(R1)
    await hung
    expect(activeSlot()).toBe(R2)
    expect(listed(R1)).toBe(true)
    expect(pendingKeys()).toEqual([])
  })
})
