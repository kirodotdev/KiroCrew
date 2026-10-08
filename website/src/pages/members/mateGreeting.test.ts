import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createElement, type ReactNode } from 'react'
import { act, renderHook as rtlRenderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { WorkBoardItem, WorkBoardResponse } from '../../api/crewBoard'

vi.mock('../../api/client', () => ({ api: { crewBoard: vi.fn(), memberRecap: vi.fn() } }))

import { api } from '../../api/client'
import { crewBoardQueryKey } from '../../api/crewBoard'
import { COLD_AFTER_MS, RESUME_REPEAT_MS, isColdOpen, summarizeResume, useMateGreeting } from './mateGreeting'

function item(overrides: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return {
    schema: 1, item_id: 'it_1', title: 'Item', acceptance: {}, state: 'open', verdict: null,
    decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null,
    last_report_at: '2026-10-07T10:00:00Z', created_at: '2026-10-07T09:00:00Z', closed_at: null,
    orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false,
    alive: 'running', events: [],
    ...overrides,
  }
}

function board(items: WorkBoardItem[], goal = 'Ship the crew page'): WorkBoardResponse {
  return {
    conductor: { schema: 1, slot_key: 'member-ops', goal, round: 1, depth: 0, parent_item: null, created_at: '' },
    conductor_alive: 'idle',
    items,
    take_over_available: false,
  }
}

const crewBoard = vi.mocked(api.crewBoard)
const memberRecap = vi.mocked(api.memberRecap)
const noLedger = () => Object.assign(new Error('no_ledger'), { status: 404 })

// One client per case, shared by every hook the case renders: the hook reads
// through the Crew board's query key, so a cache entry is part of the state.
let client: QueryClient
const renderHook: typeof rtlRenderHook = ((cb: never, opts: Record<string, unknown> = {}) =>
  rtlRenderHook(cb, {
    ...opts,
    wrapper: ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children),
  })) as never

beforeEach(() => {
  crewBoard.mockReset()
  sessionStorage.clear()
  client = new QueryClient()
  memberRecap.mockReset()
  localStorage.clear()
})

describe('summarizeResume', () => {
  it('is null when no goal is in flight: no items, or every item closed', () => {
    expect(summarizeResume(board([]))).toBeNull()
    expect(summarizeResume(board([item({ state: 'accepted', terminal: true })]))).toBeNull()
  })

  it('counts finished, running and idle work, and lists what needs a look, most urgent first', () => {
    const r = summarizeResume(board([
      item({ item_id: 'a', title: 'A', state: 'accepted', terminal: true }),
      item({ item_id: 'b', title: 'B' }),
      item({ item_id: 'c', title: 'C', status: 'done' }),
      item({ item_id: 'd', title: 'D', stale: true, alive: 'idle' }),
      item({ item_id: 'e', title: 'E', status: 'blocked' }),
      item({ item_id: 'f', title: 'F', status: 'question', outstanding: true }),
    ]))!
    expect(r.goal).toBe('Ship the crew page')
    expect(r.finished).toBe(1)
    expect([r.running, r.idle]).toEqual([1, 0])
    expect(r.attention.map((a) => [a.title, a.reason])).toEqual([
      ['F', 'question'], ['E', 'blocked'], ['D', 'quiet'], ['C', 'done'],
    ])
    expect(r.next).toEqual({ kind: 'attention', item: { title: 'F', reason: 'question' } })
  })

  it('an orphaned item reads as gone quiet, even with a question on it: nobody is left to read the answer', () => {
    const r = summarizeResume(board([item({ orphaned: true, outstanding: true, alive: 'closed' })]))!
    expect(r.attention).toEqual([{ title: 'Item', reason: 'quiet' }])
  })

  it('every item is counted once: closed ones as finished (the board\'s Finished band), open ones as running or idle', () => {
    const r = summarizeResume(board([
      item({ item_id: 'a', state: 'abandoned', terminal: true }),
      item({ item_id: 'b', alive: 'idle', status: null }),
      item({ item_id: 'c' }),
    ]))!
    expect([r.finished, r.running, r.idle, r.attention.length]).toEqual([1, 1, 1, 0])
  })

  it('next is "wait" only when every open item runs, and "continue" names the first idle one', () => {
    expect(summarizeResume(board([item()]))!.next).toEqual({ kind: 'wait' })
    expect(summarizeResume(board([item({ item_id: 'r' }), item({ item_id: 's', alive: 'idle', status: null, title: 'Start' })]))!.next)
      .toEqual({ kind: 'continue', title: 'Start' })
  })

  it('a question shows the worker\'s own report, which is where it asks', () => {
    const r = summarizeResume(board([item({ status: 'question', outstanding: true, summary: 'Cold or warm first?' })]))!
    expect(r.attention).toEqual([{ title: 'Item', reason: 'question', detail: 'Cold or warm first?' }])
  })

  it('the fingerprint moves with anything the card shows, a worker going quiet included, and holds when nothing changed', () => {
    const a = summarizeResume(board([item()]))!.fingerprint
    expect(summarizeResume(board([item()]))!.fingerprint).toBe(a)
    expect(summarizeResume(board([item({ stale: true })]))!.fingerprint).not.toBe(a)
    expect(summarizeResume(board([item({ alive: 'idle' })]))!.fingerprint).not.toBe(a)
  })
})

describe('useMateGreeting', () => {
  it('reads the board once per open, not on every render, and shows the warm greeting', async () => {
    crewBoard.mockResolvedValue(board([item()]))
    const { result, rerender } = renderHook(({ slot, idle }) => useMateGreeting(slot, idle, null), {
      initialProps: { slot: 'member-ops', idle: true },
    })
    await waitFor(() => expect(result.current.greeting?.kind).toBe('warm'))
    rerender({ slot: 'member-ops', idle: true })
    rerender({ slot: 'member-ops', idle: true })
    expect(crewBoard).toHaveBeenCalledExactlyOnceWith('member-ops')
    // A turn that runs and ends inside the same open is not a new return.
    rerender({ slot: 'member-ops', idle: false })
    rerender({ slot: 'member-ops', idle: true })
    await act(async () => {})
    expect(crewBoard).toHaveBeenCalledTimes(1)
  })

  it('reads nothing while the crewmate is mid-turn, and a turn starting takes a shown greeting down', async () => {
    crewBoard.mockResolvedValue(board([item()]))
    const busy = renderHook(() => useMateGreeting('member-ops', false, null))
    await act(async () => {})
    expect(crewBoard).not.toHaveBeenCalled()
    expect(busy.result.current.greeting).toBeNull()
    busy.unmount()

    const { result, rerender } = renderHook(({ idle }) => useMateGreeting('member-ops', idle, null), {
      initialProps: { idle: true },
    })
    await waitFor(() => expect(result.current.greeting).not.toBeNull())
    rerender({ idle: false })
    expect(result.current.greeting).toBeNull()
  })

  it('a return to an unchanged status soon after is quiet; after the window, or on a change, it speaks again', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000_000)
    try {
      crewBoard.mockResolvedValue(board([item()]))
      const { result, rerender } = renderHook(({ slot }) => useMateGreeting(slot, true, null), {
        initialProps: { slot: 'member-ops' },
      })
      await waitFor(() => expect(result.current.greeting).not.toBeNull())

      // Leave and come straight back: read again, but nothing new to say.
      rerender({ slot: '' })
      rerender({ slot: 'member-ops' })
      await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))
      act(() => result.current.dismiss())
      rerender({ slot: '' })
      rerender({ slot: 'member-ops' })
      await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(3))
      await act(async () => {})
      expect(result.current.greeting).toBeNull()

      // Back after the window: the same status is said again.
      now.mockReturnValue(1_000_000 + RESUME_REPEAT_MS)
      rerender({ slot: '' })
      rerender({ slot: 'member-ops' })
      await waitFor(() => expect(result.current.greeting).not.toBeNull())

      // A change inside the window speaks at once.
      act(() => result.current.dismiss())
      crewBoard.mockResolvedValue(board([item({ status: 'blocked' })]))
      rerender({ slot: '' })
      rerender({ slot: 'member-ops' })
      await waitFor(() => expect(result.current.greeting?.resume.next.kind).toBe('attention'))
    } finally {
      now.mockRestore()
    }
  })

  it('a crewmate with no ledger opens with no greeting and no failure, after one read', async () => {
    crewBoard.mockRejectedValue(noLedger())
    const { result } = renderHook(() => useMateGreeting('member-ops', true, null))
    await waitFor(() => expect(crewBoard).toHaveBeenCalled())
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
    expect(result.current.failure).toBeNull()
    // Settled, not waiting out a retry delay: a 404 is never retried.
    const state = client.getQueryState(crewBoardQueryKey('member-ops'))
    expect([state?.status, state?.fetchStatus, state?.fetchFailureCount]).toEqual(['error', 'idle', 1])
    expect(crewBoard).toHaveBeenCalledTimes(1)
  })

  it('any other failed read is reported, not hidden as "no ledger"', async () => {
    const boom = Object.assign(new Error('dirty ledger'), { status: 409 })
    crewBoard.mockRejectedValue(boom)
    const { result } = renderHook(() => useMateGreeting('member-ops', true, null))
    await waitFor(() => expect(result.current.failure).not.toBeNull(), { timeout: 4000 })
    expect(result.current.failure).toEqual({ slot: 'member-ops', kind: 'warm', error: boom })
    expect(result.current.greeting).toBeNull()
    act(() => result.current.dismiss())
    expect(result.current.failure).toBeNull()
  })

  it('a board cached by an earlier visit is refreshed, never shown as this return\'s status', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(5_000_000)
    try {
      client.setQueryData(crewBoardQueryKey('member-ops'), board([item({ status: 'blocked' })]), { updatedAt: 1_000 })
      crewBoard.mockResolvedValue(board([item()]))
      const { result } = renderHook(() => useMateGreeting('member-ops', true, null))
      await waitFor(() => expect(result.current.greeting).not.toBeNull())
      expect(crewBoard).toHaveBeenCalledTimes(1)
      expect(result.current.greeting?.resume.next).toEqual({ kind: 'wait' })
    } finally {
      now.mockRestore()
    }
  })

  it('a greeting for one crewmate never shows on another', async () => {
    crewBoard.mockImplementation((slot: string) =>
      slot === 'member-ops' ? Promise.resolve(board([item()])) : Promise.reject(noLedger()))
    const { result, rerender } = renderHook(({ slot }) => useMateGreeting(slot, true, null), {
      initialProps: { slot: 'member-ops' },
    })
    await waitFor(() => expect(result.current.greeting).not.toBeNull())
    rerender({ slot: 'member-other' })
    expect(result.current.greeting).toBeNull()
  })

  it('leaving and reopening shows only what the repeat guard admits, never the card kept from before', async () => {
    crewBoard.mockResolvedValue(board([item()]))
    const { result, rerender } = renderHook(({ slot }) => useMateGreeting(slot, true, null), {
      initialProps: { slot: 'member-ops' },
    })
    await waitFor(() => expect(result.current.greeting).not.toBeNull())
    // Back to the roster without dismissing, then the same crewmate again.
    rerender({ slot: '' })
    rerender({ slot: 'member-ops' })
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
  })
})

describe('isColdOpen', () => {
  const now = 10 * COLD_AFTER_MS
  it('a never-active thread, or one idle past the window, is cold', () => {
    expect(isColdOpen(0, now, null)).toBe(true)
    expect(isColdOpen((now - COLD_AFTER_MS) / 1000, now, null)).toBe(true)
  })
  it('a recently active thread is not cold', () => {
    expect(isColdOpen((now - COLD_AFTER_MS + 1000) / 1000, now, null)).toBe(false)
  })
  it('an idle stretch already welcomed is not welcomed again', () => {
    expect(isColdOpen(0, now, '0')).toBe(false)
    expect(isColdOpen(5, now, '5')).toBe(false)
    expect(isColdOpen(6, now, '5')).toBe(true)
  })
})

describe('useMateGreeting cold welcome', () => {
  const recap = { slug: 'ops', member: 'Ops', paused: [{ goal: 'Rotate keys', next: '' }], recent: [] }
  const cold = { slug: 'ops', member: 'Ops', lastActiveTs: 0 }
  // Active "just now" without reading the clock: a stamp past any real now.
  const FUTURE_TS = 4_102_444_800
  // A cold welcome is two chained reads (board, then recap), each a settled
  // query plus an effect, so its wait names its own ceiling.
  const CHAIN_WAIT = { timeout: 3000 }

  it('with no ledger, a new thread opens on the recap', async () => {
    crewBoard.mockRejectedValue(noLedger())
    memberRecap.mockResolvedValue(recap)
    const { result } = renderHook(() => useMateGreeting('member-ops', true, cold))
    await waitFor(() => expect(result.current.greeting?.kind).toBe('cold'), CHAIN_WAIT)
    expect(memberRecap).toHaveBeenCalledExactlyOnceWith('ops', 'Ops')
    expect(result.current.failure).toBeNull()
  })

  it('a board whose goal ended is cold too', async () => {
    crewBoard.mockResolvedValue(board([item({ state: 'accepted', terminal: true })]))
    memberRecap.mockResolvedValue(recap)
    const { result } = renderHook(() => useMateGreeting('member-ops', true, cold))
    await waitFor(() => expect(result.current.greeting?.kind).toBe('cold'), CHAIN_WAIT)
  })

  it('a goal in flight wins: warm, and the recap is never read', async () => {
    crewBoard.mockResolvedValue(board([item()]))
    const { result } = renderHook(() => useMateGreeting('member-ops', true, cold))
    await waitFor(() => expect(result.current.greeting?.kind).toBe('warm'))
    expect(memberRecap).not.toHaveBeenCalled()
  })

  it('a recently active thread gets no welcome and reads no recap', async () => {
    crewBoard.mockRejectedValue(noLedger())
    const { result } = renderHook(() => useMateGreeting('member-ops', true, { ...cold, lastActiveTs: FUTURE_TS }))
    await waitFor(() => expect(crewBoard).toHaveBeenCalled())
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
    expect(memberRecap).not.toHaveBeenCalled()
  })

  it('welcomes once per idle stretch: a reopen with no new activity stays quiet', async () => {
    crewBoard.mockRejectedValue(noLedger())
    memberRecap.mockResolvedValue(recap)
    const { result, rerender } = renderHook(({ slot }) => useMateGreeting(slot, true, cold), {
      initialProps: { slot: 'member-ops' },
    })
    await waitFor(() => expect(result.current.greeting?.kind).toBe('cold'), CHAIN_WAIT)
    rerender({ slot: '' })
    rerender({ slot: 'member-ops' })
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(memberRecap).toHaveBeenCalledTimes(1)
  })

  it('a turn starting before the recap answers drops it: no card', async () => {
    crewBoard.mockRejectedValue(noLedger())
    let answer: (r: typeof recap) => void = () => {}
    memberRecap.mockReturnValue(new Promise((r) => { answer = r }))
    const { result, rerender } = renderHook(({ idle }) => useMateGreeting('member-ops', idle, cold), {
      initialProps: { idle: true },
    })
    await waitFor(() => expect(memberRecap).toHaveBeenCalled(), CHAIN_WAIT)
    rerender({ idle: false })
    await act(async () => { answer(recap) })
    expect(result.current.greeting).toBeNull()
    expect(localStorage.getItem('kc-mate-welcomed-member-ops')).toBeNull()
  })

  it('a failed recap is reported as a cold failure, not dropped', async () => {
    crewBoard.mockRejectedValue(noLedger())
    memberRecap.mockRejectedValue(Object.assign(new Error('boom'), { status: 400 }))
    const { result } = renderHook(() => useMateGreeting('member-ops', true, cold))
    await waitFor(() => expect(result.current.failure?.kind).toBe('cold'), CHAIN_WAIT)
    expect(result.current.greeting).toBeNull()
    expect(localStorage.getItem('kc-mate-welcomed-member-ops')).toBeNull()
  })

  it('a recap with nothing in it draws no card', async () => {
    crewBoard.mockRejectedValue(noLedger())
    memberRecap.mockResolvedValue({ ...recap, paused: [], recent: [] })
    const { result } = renderHook(() => useMateGreeting('member-ops', true, cold))
    await waitFor(() => expect(memberRecap).toHaveBeenCalled(), CHAIN_WAIT)
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
  })

  it('with no crewmate named for it, only warm greets', async () => {
    crewBoard.mockRejectedValue(noLedger())
    const { result } = renderHook(() => useMateGreeting('member-ops', true, null))
    await waitFor(() => expect(crewBoard).toHaveBeenCalled())
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
    expect(memberRecap).not.toHaveBeenCalled()
  })
})
