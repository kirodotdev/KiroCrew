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

  it('a shown panel re-reads on a push, and never starts a read while one is in flight', async () => {
    let release: (v: unknown) => void = () => {}
    const { store } = mount(<Panel root="r1" active />)
    await act(() => vi.advanceTimersByTimeAsync(10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)
    api.crewWorkers.mockImplementation(() => new Promise(r => { release = r }))
    push(store, 1)
    await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10))
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    // More pushes while the second read is still out: no third request.
    for (let i = 2; i < 6; i++) { push(store, i); await act(() => vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 10)) }
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
    release(ANSWER)
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
    const qc = new QueryClient()
    api.crewWorkers.mockImplementation(() => new Promise(r => setTimeout(() => r(ANSWER), 1000)))
    const slow = readCrewWorkers(qc, ['k'], 'r1')
    await vi.advanceTimersByTimeAsync(1000)
    await slow
    expect(crewWorkersGapMs('r1', CREW_WORKERS_PUSH_COALESCE_MS)).toBe(1000)
    expect(crewWorkersGapMs('r1', CREW_WORKERS_FALLBACK_POLL_MS)).toBe(CREW_WORKERS_FALLBACK_POLL_MS)
    api.crewWorkers.mockResolvedValue(ANSWER)
    await readCrewWorkers(qc, ['k'], 'r1')
    expect(crewWorkersGapMs('r1', CREW_WORKERS_PUSH_COALESCE_MS)).toBe(CREW_WORKERS_PUSH_COALESCE_MS)
  })

  it('never has more than the cap of list requests out, so a send always finds a free connection', async () => {
    const qc = new QueryClient()
    const out: Array<(v: unknown) => void> = []
    let peak = 0
    let open = 0
    api.crewWorkers.mockImplementation(() => { open++; peak = Math.max(peak, open); return new Promise(r => { out.push(v => { open--; r(v) }) }) })
    // Every root already has a list, so these are re-reads.
    for (let i = 0; i < 8; i++) qc.setQueryData(['k', i], ANSWER)
    const reads = Array.from({ length: 8 }, (_, i) => readCrewWorkers(qc, ['k', i], 'r' + i))
    await vi.advanceTimersByTimeAsync(0)
    expect(peak).toBe(CREW_POLL_CAP)
    expect(CREW_POLL_CAP).toBeLessThan(6)
    out.forEach(r => r(ANSWER))
    await Promise.all(reads)
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
