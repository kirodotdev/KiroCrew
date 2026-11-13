/** `useResumeAll` at hook level: the ordering between the run's 202 (or 409)
 *  and its `resume_all_done` frame, which arrive on separate channels. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const resumeAll = vi.fn()
vi.mock('../api/client', () => ({ api: { chatSlotsResumeAll: (...a: unknown[]) => resumeAll(...a) } }))

import { useResumeAll, resumeAllPendingCeilingMs } from '../pages/chat-sidebar/resumeAll'
import { RESUME_ALL_DONE_EVENT } from '../lib/resumeAllEvent'
import { useResumeAllWaiting } from '../lib/resumeAllWaiting'
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

  it('settles from its own run\u2019s frame when another run\u2019s frame came first', async () => {
    const answer = deferred<unknown>()
    resumeAll.mockReturnValue(answer.promise)
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    act(() => frame('older'))
    act(() => frame('r1'))
    await act(async () => { answer.resolve({ ok: true, total: 1, run_id: 'r1' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(false)
    expect(result.current.result?.run_id).toBe('r1')
  })

  it('a 202 read after a second run finished settles from its own run\u2019s frame', async () => {
    // The server's one-run guard ends when a run's dispatch is done, not when
    // its 202 reaches the tab, so another tab's run can start and finish before
    // this tab reads its answer. Both frames arrive first; the 202 names r1.
    const answer = deferred<unknown>()
    resumeAll.mockReturnValue(answer.promise)
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    act(() => frame('r1'))
    act(() => frame('r2'))
    await act(async () => { answer.resolve({ ok: true, total: 1, run_id: 'r1' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(false)
    expect(result.current.result?.run_id).toBe('r1')
  })

  it('keeps nothing once the answer is read: a later press does not settle from a frame heard before it', async () => {
    // The map lives only while a request is open. After the 202 is read, a
    // frame for another run is not kept, so the next press's 202 that names
    // that run waits for a frame of its own.
    const first = deferred<unknown>()
    resumeAll.mockReturnValueOnce(first.promise)
    const { result } = renderHook(() => useResumeAll({ localSlots: slots, connected: true }), { wrapper: wrap })
    act(() => result.current.run())
    await act(async () => { first.resolve({ ok: true, total: 1, run_id: 'r1' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    act(() => frame('r2'))
    act(() => frame('r1'))
    expect(result.current.result?.run_id).toBe('r1')
    const second = deferred<unknown>()
    resumeAll.mockReturnValueOnce(second.promise)
    act(() => result.current.run())
    await act(async () => { second.resolve({ ok: true, total: 1, run_id: 'r2' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(true)
    expect(result.current.result).toBeNull()
    act(() => frame('r2'))
    expect(result.current.pending).toBe(false)
    expect(result.current.result?.run_id).toBe('r2')
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

  it('stops waiting when the socket goes down, since a frame sent then never arrives', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 1, run_id: 'r2' })
    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) => useResumeAll({ localSlots: slots, connected }),
      { wrapper: wrap, initialProps: { connected: true } },
    )
    act(() => result.current.run())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(true)
    rerender({ connected: false })
    expect(result.current.pending).toBe(false)
  })

  it('does not wait when the socket went down before the 202 was read', async () => {
    const answer = deferred<unknown>()
    resumeAll.mockReturnValue(answer.promise)
    const { result, rerender } = renderHook(
      ({ connected }: { connected: boolean }) => useResumeAll({ localSlots: slots, connected }),
      { wrapper: wrap, initialProps: { connected: true } },
    )
    act(() => result.current.run())
    rerender({ connected: false })
    await act(async () => { answer.resolve({ ok: true, total: 1, run_id: 'r4' }); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.pending).toBe(false)
  })

  it('a counted session waits until a push shows it running, then never again in that run', async () => {
    resumeAll.mockResolvedValue({ ok: true, total: 2, run_id: 'r3' })
    const two = [
      { key: 'a', title: 'a', running: false, messages: 1, interrupted: true },
      { key: 'b', title: 'b', running: false, messages: 1, interrupted: true },
    ] as unknown as Slot[]
    const { result, rerender } = renderHook(
      ({ rows }: { rows: Slot[] }) => ({
        row: useResumeAll({ localSlots: rows, connected: true }),
        a: useResumeAllWaiting('a'),
        b: useResumeAllWaiting('b'),
      }),
      { wrapper: wrap, initialProps: { rows: two } },
    )
    act(() => result.current.row.run())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.a).toBe(true)
    expect(result.current.b).toBe(true)
    // The run reaches `a`, and its resumed turn fails again: it reads Resume.
    rerender({ rows: [{ ...two[0], running: true, interrupted: false }, two[1]] as Slot[] })
    rerender({ rows: two })
    expect(result.current.a).toBe(false)
    expect(result.current.b).toBe(true)
    act(() => frame('r3'))
    expect(result.current.b).toBe(false)
  })

  it('a 409 adopting another press\u2019s run marks no session as waiting', async () => {
    resumeAll.mockRejectedValue(Object.assign(new Error('a resume-all run is already in progress'), {
      status: 409,
      body: JSON.stringify({ code: 'resume_all_running', run_id: 'r8', total: 3 }),
    }))
    const { result } = renderHook(
      () => ({ row: useResumeAll({ localSlots: slots, connected: true }), a: useResumeAllWaiting('a') }),
      { wrapper: wrap },
    )
    act(() => result.current.row.run())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(result.current.row.alreadyRunning).toBe(true)
    expect(result.current.a).toBe(false)
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
