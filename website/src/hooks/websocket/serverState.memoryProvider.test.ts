/**
 * Which memory Crew uses (`['memory-provider']`) is read once with `staleTime:
 * Infinity`. A switch made in another window reaches this one only through the
 * `memory_provider` refresh frame the gateway pushes after the switch, and the
 * switch sheet copies from what that cache says is in use, so the frame must
 * re-read it and the move list under it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: { getCredentialRedaction: () => new Promise(() => {}) },
}))

import { invalidateRefreshQueries, refreshServerStateAfterReconnect } from './serverState'

let qc: QueryClient
const unsubscribers: (() => void)[] = []

afterEach(() => {
  for (const off of unsubscribers.splice(0)) off()
  qc.clear()
})

function observe(queryKey: readonly unknown[]): { reads: () => number } {
  let reads = 0
  const observer = new QueryObserver(qc, {
    queryKey,
    queryFn: async () => {
      reads += 1
      return { ok: true }
    },
  })
  unsubscribers.push(observer.subscribe(() => {}))
  return { reads: () => reads }
}

describe('memory provider cache', () => {
  it('re-reads the overview and the move list on the memory_provider frame', async () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const overview = observe(['memory-provider'])
    const moves = observe(['memory-provider', 'moves'])
    await vi.waitFor(() => expect([overview.reads(), moves.reads()]).toEqual([1, 1]))
    // The first reads have answered, so the frame's re-read is a fetch of its own.
    await vi.waitFor(() =>
      expect(qc.getQueryCache().getAll().map(q => q.state.status)).toEqual(['success', 'success']),
    )

    invalidateRefreshQueries(qc, ['memory_provider'])

    await vi.waitFor(() => expect([overview.reads(), moves.reads()]).toEqual([2, 2]))
  })

  it('re-reads whether the gateway runs the preview after a reconnect', async () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const preview = observe(['memoryProvider', 'preview'])
    await vi.waitFor(() => expect(preview.reads()).toBe(1))
    await vi.waitFor(() => expect(qc.getQueryCache().getAll()[0].state.status).toBe('success'))

    refreshServerStateAfterReconnect(qc)

    await vi.waitFor(() => expect(preview.reads()).toBe(2))
  })
})
