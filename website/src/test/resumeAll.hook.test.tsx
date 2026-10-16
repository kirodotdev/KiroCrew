/** `useResumeAll` at hook level: the ordering between the run's 202 (or 409)
 *  and its `resume_all_done` frame, which arrive on separate channels. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const resumeAll = vi.fn()
vi.mock('../api/client', () => ({ api: { chatSlotsResumeAll: (...a: unknown[]) => resumeAll(...a) } }))

import { useResumeAll, resumeAllPendingCeilingMs } from '../pages/chat-sidebar/resumeAll'
import { RESUME_ALL_DONE_EVENT } from '../hooks/useWebSocket'
import type { Slot } from '../pages/chat-sidebar/types'

const slots = [{ key: 'a', title: 'a', running: false, messages: 1, interrupted: true }] as unknown as Slot[]

const wrap = ({ children }: { children: React.ReactNode }) =>
  React.createElement(QueryClientProvider, { client: new QueryClient() }, children)

const frame = (run_id: string) => window.dispatchEvent(new CustomEvent(RESUME_ALL_DONE_EVENT, {
  detail: { ok: true, run_id, total: 1, resumed: ['a'], skipped: [], failed: [] },
}))

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); resumeAll.mockReset() })
afterEach(() => vi.useRealTimers())

describe('useResumeAll — the outcome frame and the 202 in either order', () => {
  it('settles on a frame that arrived before the 202 was read', async () => {
    const answer = deferred<unknown>()
    resumeAll.mockReturnValue(answer.promise)
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    act(() => frame('r1'))
    await act(async () => { answer.resolve({ ok: true, total: 1, run_id: 'r1' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(false)
    expect(result.current.result?.run_id).toBe('r1')
    expect(result.current.result?.resumed).toEqual(['a'])
  })

  it('an early frame for another run does not settle this one', async () => {
    const answer = deferred<unknown>()
    resumeAll.mockReturnValue(answer.promise)
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    act(() => frame('other'))
    await act(async () => { answer.resolve({ ok: true, total: 1, run_id: 'r1' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(true)
    expect(result.current.result).toBeNull()
    act(() => frame('r1'))
    expect(result.current.pending).toBe(false)
    expect(result.current.result?.run_id).toBe('r1')
  })

  it('a 409 adopts the other run with that run\u2019s total for its ceiling', async () => {
    const total = 500
    resumeAll.mockRejectedValue(Object.assign(new Error('a resume-all run is already in progress'), {
      status: 409,
      body: JSON.stringify({ code: 'resume_all_running', run_id: 'r9', total }),
    }))
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.alreadyRunning).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(resumeAllPendingCeilingMs(0) + 1_000) })
    expect(result.current.pending).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(resumeAllPendingCeilingMs(total)) })
    expect(result.current.pending).toBe(false)
  })

  it('a 409 whose run already finished settles from the kept frame', async () => {
    const answer = deferred<unknown>()
    resumeAll.mockReturnValue(answer.promise)
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    act(() => frame('r9'))
    await act(async () => {
      answer.reject(Object.assign(new Error('running'), {
        status: 409,
        body: JSON.stringify({ code: 'resume_all_running', run_id: 'r9', total: 1 }),
      }))
      await Promise.resolve()
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(false)
    expect(result.current.result?.run_id).toBe('r9')
  })
})
