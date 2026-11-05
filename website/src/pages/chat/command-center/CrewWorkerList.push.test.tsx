/**
 * The worker list is re-read when the slot store says the crew moved: the session
 * tree's lineage push, the root's `crew_work` push, a worker's state. A row of
 * another crew moves nothing, and a burst is one re-read.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import { sseSlotPatch } from '../../../store/dashboardSlice'
import type { ChatSlot } from '../../../types'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))

import { CREW_WORKERS_PUSH_COALESCE_MS, crewWorkersPushKey, useCrewWorkerSummary } from './CrewWorkerList'

const SLOTS = [
  { key: 'root', title: 'Lead', crew_mode_root: true },
  { key: 'w1', title: 'Worker one', crew_root: 'root', created_by: 'root', running: false },
  { key: 'other', title: 'Not in the crew', running: false },
] as unknown as ChatSlot[]

function Summary() {
  useCrewWorkerSummary('root')
  return null
}

function renderSummary() {
  api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: SLOTS } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><Summary /><Summary /></Provider></QueryClientProvider>)
  return store
}

async function settle() {
  await act(async () => { await vi.advanceTimersByTimeAsync(CREW_WORKERS_PUSH_COALESCE_MS + 50) })
}

describe('crewWorkersPushKey', () => {
  it('moves with a crew row and the root crew_work, not with another row', () => {
    const base = crewWorkersPushKey(SLOTS, 'root')
    const running = SLOTS.map(s => (s.key === 'w1' ? { ...s, running: true } : s))
    const work = SLOTS.map(s => (s.key === 'root' ? { ...s, crew_work: { open: 1, person: 0, person_workers: [], items: [] } } : s))
    const other = SLOTS.map(s => (s.key === 'other' ? { ...s, running: true } : s))
    expect(crewWorkersPushKey(running, 'root')).not.toBe(base)
    expect(crewWorkersPushKey(work as ChatSlot[], 'root')).not.toBe(base)
    expect(crewWorkersPushKey(other, 'root')).toBe(base)
  })
})

describe('useCrewWorkerSummary push', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('re-reads once on a pushed crew change and not on another row', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const store = renderSummary()
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledTimes(1))

    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: 'other', running: true }] })) })
    await settle()
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)

    // A worker's turn starting and the root's ledger moving, in one burst: one read.
    act(() => {
      store.dispatch(sseSlotPatch({ slots: [{ key: 'w1', running: true }] }))
      store.dispatch(sseSlotPatch({ slots: [{ key: 'root', crew_work: { open: 1, person: 0, person_workers: [], items: [] } }] }))
    })
    await settle()
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledTimes(2))

    // The session tree's lineage push re-parents the worker.
    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: 'w1', parent: { slot: 'other', key: 'other' } }] })) })
    await settle()
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledTimes(3))
  })

  it('re-reads a cached list once on remount when the crew moved while unmounted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const store = createTestStore({ dashboard: { slots: SLOTS } } as never)
    const ui = <QueryClientProvider client={qc}><Provider store={store}><Summary /><Summary /></Provider></QueryClientProvider>
    const first = render(ui)
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledTimes(1))
    await settle()
    expect(api.crewWorkers).toHaveBeenCalledTimes(1)
    first.unmount()

    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: 'w1', running: true }] })) })
    render(ui)
    await settle()
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledTimes(2))
    await settle()
    expect(api.crewWorkers).toHaveBeenCalledTimes(2)
  })
})
