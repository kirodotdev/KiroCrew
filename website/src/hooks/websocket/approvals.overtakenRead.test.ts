/**
 * The approvals reconcile when its `GET /api/approvals` reads overlap.
 *
 * Every socket open starts one read: the first connect (`runFirstConnect`) and
 * each reconnect catch-up (`runReconnectCatchUp`) chain `syncPendingApprovals`
 * into `reconcilePending`, and nothing makes one open wait for another's read.
 * A slow read can therefore answer after a newer one, in either order. Only the
 * newest read's answer may retire or adopt rows. These tests drive the registry
 * directly, reading and writing the one app store as production does.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { store } from '../../store'
import { deleteNotification } from '../../store/notificationsSlice'
import { useApprovalRegistry } from './approvals'
import type { FrameData } from './frames'

vi.mock('../../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: { approvals: vi.fn() },
}))

type Snapshot = Awaited<ReturnType<typeof api.approvals>>

function deferredRead() {
  let resolve: (value: Snapshot) => void = () => {}
  const promise = new Promise<Snapshot>((r) => { resolve = r })
  return { promise, resolve }
}

const listing = (id: string, instance: string, ts: number) =>
  [{ id, instance, source: 'cron', tool: `tool_${instance}`, ts }] as Snapshot

const rowsFor = (id: string) => store.getState().notifications.items
  .filter(n => n.approval_id === id)
  .map(n => [n.ts, n.approval_instance])

function mountRegistry() {
  const { result } = renderHook(() => useApprovalRegistry(store.dispatch, new QueryClient()))
  return result.current
}

describe('reconcilePending with overlapping reads', () => {
  afterEach(() => {
    for (const ts of ['70', '71', '72']) store.dispatch(deleteNotification.fulfilled(ts, '', ts))
    vi.mocked(api.approvals).mockReset()
  })

  it('an older read answering after a newer one neither drops the newer row nor brings the ended one back', async () => {
    const id = 'ap-overtaken-last'
    const older = deferredRead()
    const newer = deferredRead()
    vi.mocked(api.approvals).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    const registry = mountRegistry()
    // Request A is live: its frame raised the feed row and the registry entry.
    act(() => {
      registry.onApprovalFrame({ id, instance: 'inst-a', source: 'cron', tool: 'tool_inst-a', ts: 70 } as FrameData, true)
    })
    const writeRow = vi.fn()
    // The first read starts while the server holds A. Before the second read
    // starts, the server replaces A with B under the same id.
    const first = registry.reconcilePending(writeRow)
    const second = registry.reconcilePending(writeRow)
    await act(async () => { newer.resolve(listing(id, 'inst-b', 71)); await second })
    expect(rowsFor(id)).toEqual([['71', 'inst-b']])
    // The first read answers last, still listing A.
    await act(async () => { older.resolve(listing(id, 'inst-a', 70)); await first })
    expect(rowsFor(id)).toEqual([['71', 'inst-b']])
    expect(writeRow.mock.calls.map(([approval]) => approval.instance)).toEqual(['inst-b'])
  })

  it('an older read answering before a newer one adopts nothing the newer read no longer lists', async () => {
    const id = 'ap-overtaken-first'
    const older = deferredRead()
    const newer = deferredRead()
    vi.mocked(api.approvals).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    const registry = mountRegistry()
    const writeRow = vi.fn()
    const first = registry.reconcilePending(writeRow)
    const second = registry.reconcilePending(writeRow)
    // The first read lists a request that ends before the second read starts.
    // Adopted, it would sit outside the second read's starting snapshot, which
    // could then never retire it.
    await act(async () => { older.resolve(listing(id, 'inst-y', 72)); await first })
    await act(async () => { newer.resolve([] as Snapshot); await second })
    expect(rowsFor(id)).toEqual([])
    expect(writeRow).not.toHaveBeenCalled()
  })
})
