import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createElement, type ReactNode } from 'react'
import { act, renderHook as rtlRenderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { WorkBoardItem, WorkBoardResponse } from '../../api/crewBoard'

vi.mock('../../api/client', () => ({ api: { crewBoard: vi.fn(), memberRecap: vi.fn() } }))

import { api } from '../../api/client'
import { crewBoardQueryKey } from '../../api/crewBoard'
import { COLD_AFTER_MS, isColdOpen, useMateGreeting } from './mateGreeting'

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

describe('useMateGreeting', () => {
  it('active work gets no immediate static warm greeting', async () => {
    crewBoard.mockResolvedValue(board([item()]))
    const { result, rerender } = renderHook(({ slot }) => useMateGreeting(slot, true, {
      slug: 'ops', member: 'Ops', lastActiveTs: 0,
    }), { initialProps: { slot: 'member-ops' } })
    await waitFor(() => expect(crewBoard).toHaveBeenCalledExactlyOnceWith('member-ops'))
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
    expect(result.current.failure).toBeNull()
    expect(memberRecap).not.toHaveBeenCalled()
    rerender({ slot: 'member-ops' })
    expect(crewBoard).toHaveBeenCalledTimes(1)
  })

  it('reads nothing while the mate is mid-turn', async () => {
    const { result } = renderHook(() => useMateGreeting('member-ops', false, null))
    await act(async () => {})
    expect(crewBoard).not.toHaveBeenCalled()
    expect(result.current.greeting).toBeNull()
  })

  it('a non-404 board failure is reported', async () => {
    const boom = Object.assign(new Error('dirty ledger'), { status: 409 })
    crewBoard.mockRejectedValue(boom)
    const { result } = renderHook(() => useMateGreeting('member-ops', true, null))
    await waitFor(() => expect(result.current.failure).not.toBeNull(), { timeout: 4000 })
    expect(result.current.failure).toEqual({ slot: 'member-ops', kind: 'board', error: boom })
    expect(result.current.greeting).toBeNull()
    act(() => result.current.dismiss())
    expect(result.current.failure).toBeNull()
  })

  it('a mate with no ledger has no greeting and no failure without cold data', async () => {
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

  it('with no mate named for it, only warm greets', async () => {
    crewBoard.mockRejectedValue(noLedger())
    const { result } = renderHook(() => useMateGreeting('member-ops', true, null))
    await waitFor(() => expect(crewBoard).toHaveBeenCalled())
    await act(async () => {})
    expect(result.current.greeting).toBeNull()
    expect(memberRecap).not.toHaveBeenCalled()
  })
})
