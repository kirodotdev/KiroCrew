import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn() }))
vi.mock('../api/client', () => ({ api }))

import { sseSlotPatch } from '../store/dashboardSlice'
import { createChatEndpoints } from '../api/client/chat'
import type { ClientTransport } from '../api/client/transport'
import { CREW_WORKERS_FALLBACK_POLL_MS, CREW_WORKERS_PUSH_COALESCE_MS, useCrewWorkerSummary, useCrewWorkers } from '../pages/chat/command-center/CrewWorkerList'
import { CREW_POLL_CAP, crewWorkersGapMs, readCrewWorkers, resetCrewWorkersPoll } from './crewWorkersPoll'

const ANSWER = { root: 'r1', workers: [] }
const SLOT = { key: 'w1', crew_root: 'r1', title: 'W', running: false }

function Panel({ root, active }: { root: string; active: boolean }) {
  useCrewWorkers(root, active)
  return null
}
function Row({ root }: { root: string }) {
  useCrewWorkerSummary(root)
  return null
}

function mount(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const store = createTestStore({ dashboard: { slots: [SLOT] } } as never)
  const view = render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
  return { qc, store, ...view }
}
const push = (store: ReturnType<typeof createTestStore>, n: number) =>
  act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: 'w1', title: 'W' + n }] })) })

beforeEach(() => { vi.useFakeTimers(); resetCrewWorkersPoll(); api.crewWorkers.mockReset(); api.crewWorkers.mockResolvedValue(ANSWER) })
afterEach(() => { vi.useRealTimers() })

describe('crew-workers polling', () => {
  it('a hidden panel makes no request, however long it sits or whatever the crew does', async () => {
    const { store } = mount(<Panel root="r1" active={false} />)
    for (let i = 0; i < 5; i++) { push(store, i); await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_FALLBACK_POLL_MS)) }
    expect(api.crewWorkers).not.toHaveBeenCalled()
  })

  it('a sidebar row re-reads once for a burst of pushes, and not at all while the page is hidden', async () => {
    const { store } = mount(<Row root="r1" />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)
    for (let i = 0; i < 4; i++) push(store, i)
    await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    const state = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    push(store, 9)
    await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    state.mockRestore()
  })

  it('a push during a read owes one follow-up read after it settles, however many pushes land', async () => {
    const { store } = mount(<Panel root="r1" active />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)
    const pending: Array<(v: unknown) => void> = []
    api.crewWorkers.mockImplementation(() => new Promise(r => { pending.push(r) }))
    push(store, 1)
    await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    // More pushes while that read is out: no overlapping request.
    for (let i = 2; i < 6; i++) { push(store, i); await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10)) }
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    // The read settles with the older state; exactly one newer read follows.
    pending[0]({ root: 'r1', workers: [{ key: 'old' }] })
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(3)
    pending[1]({ root: 'r1', workers: [{ key: 'new' }] })
    await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_FALLBACK_POLL_MS / 2))
    expect(api.crewWorkers).toHaveBeenCalledTimes(3)
  })

  it('a page that comes back to view reads once at once; a hidden page does not read on a push', async () => {
    const { store } = mount(<Panel root="r1" active />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    const state = vi.spyOn(document, 'visibilityState', 'get')
    state.mockReturnValue('hidden')
    push(store, 1)
    await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)
    state.mockReturnValue('visible')
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')) })
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    state.mockRestore()
  })

  it('backs off while answers are slower than the wait, and resets on a fast one', async () => {
    api.crewWorkers.mockImplementation(() => new Promise(r => setTimeout(() => r(ANSWER), 1000)))
    const slow = readCrewWorkers('r1')
    await vi.advanceTimersByTimeAsync(1000)
    await slow
    expect(crewWorkersGapMs('r1', CREW_WORKERS_PUSH_COALESCE_MS)).toBe(1000)
    expect(crewWorkersGapMs('r1', CREW_WORKERS_FALLBACK_POLL_MS)).toBe(CREW_WORKERS_FALLBACK_POLL_MS)
    api.crewWorkers.mockResolvedValue(ANSWER)
    await readCrewWorkers('r1')
    expect(crewWorkersGapMs('r1', CREW_WORKERS_PUSH_COALESCE_MS)).toBe(CREW_WORKERS_PUSH_COALESCE_MS)
  })

  /** Reads that stay out until released, in the order they were sent. */
  function held() {
    const out: Array<{ root: string; settle: (v: unknown, ok?: boolean) => void }> = []
    let open = 0
    let peak = 0
    api.crewWorkers.mockImplementation((root: string) => {
      open++; peak = Math.max(peak, open)
      return new Promise((resolve, reject) => { out.push({ root, settle: (v, ok = true) => { open--; (ok ? resolve : reject)(v) } }) })
    })
    return { out, peak: () => peak }
  }

  it('a third root waits for a slot and then reads fresh data, not a cached list', async () => {
    const { out } = held()
    const reads = ['a', 'b', 'c'].map(r => readCrewWorkers(r))
    await vi.advanceTimersByTimeAsync(0)
    expect(out.map(o => o.root)).toEqual(['a', 'b'])
    out[0].settle({ root: 'a', workers: [] })
    await vi.advanceTimersByTimeAsync(0)
    expect(out.map(o => o.root)).toEqual(['a', 'b', 'c'])
    out[1].settle({ root: 'b', workers: [] })
    out[2].settle({ root: 'c', workers: [{ key: 'fresh' }] })
    expect((await reads[2] as { workers: unknown[] }).workers).toEqual([{ key: 'fresh' }])
  })

  it('a cold load of 6 roots never has more than the cap out, and keeps arrival order', async () => {
    const { out, peak } = held()
    const roots = ['r0', 'r1', 'r2', 'r3', 'r4', 'r5']
    const reads = roots.map(r => readCrewWorkers(r))
    await vi.advanceTimersByTimeAsync(0)
    expect(CREW_POLL_CAP).toBeLessThan(6)
    for (let i = 0; i < roots.length; i++) {
      out[i].settle(ANSWER)
      await vi.advanceTimersByTimeAsync(0)
    }
    await Promise.all(reads)
    expect(peak()).toBe(CREW_POLL_CAP)
    expect(out.map(o => o.root)).toEqual(roots)
  })

  it('a rejected read frees its slot and still reports its error', async () => {
    const { out } = held()
    const reads = ['a', 'b', 'c'].map(r => readCrewWorkers(r))
    const failed = expect(reads[0]).rejects.toThrow('boom')
    await vi.advanceTimersByTimeAsync(0)
    out[0].settle(new Error('boom'), false)
    await failed
    await vi.advanceTimersByTimeAsync(0)
    expect(out.map(o => o.root)).toEqual(['a', 'b', 'c'])
    out[1].settle(ANSWER)
    out[2].settle(ANSWER)
    await Promise.all(reads.slice(1))
  })
})

describe('api.crewWorkers revalidation', () => {
  const json = (status: number, etag?: string) => new Response(status === 304 ? null : JSON.stringify(ANSWER), { status, headers: etag ? { ETag: etag } : {} })

  it('sends the last ETag as received and keeps the cached list on a 304', async () => {
    const jfetch = vi.fn()
      .mockResolvedValueOnce(json(200, 'W/"abc"'))
      .mockResolvedValueOnce(json(304, 'W/"abc"'))
    const t = { jfetch, j: (r: Response) => r.json() } as unknown as ClientTransport
    const { crewWorkers } = createChatEndpoints(t).slotList
    const first = await crewWorkers('r-etag')
    expect(jfetch.mock.calls[0][1]).toBeUndefined()
    const second = await crewWorkers('r-etag')
    expect(jfetch.mock.calls[1][1]).toEqual({ headers: { 'If-None-Match': 'W/"abc"' } })
    expect(second).toBe(first)
  })
})
