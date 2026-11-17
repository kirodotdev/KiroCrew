/**
 * The Security panel's safety switches (`['safety-switches']`) are read once with
 * `staleTime: Infinity`. A save from another tab reaches this one only through the
 * `config` refresh frame the gateway pushes after the config write applies, and a
 * frame sent while the socket was down only through the reconnect re-read.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: { getCredentialRedaction: () => new Promise(() => {}) },
}))

import { invalidateRefreshQueries, refreshServerStateAfterReconnect } from './serverState'

let qc: QueryClient
let unsubscribe: () => void = () => {}
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

afterEach(() => {
  unsubscribe()
  qc.clear()
})

async function observeSafetySwitches(): Promise<{ reads: () => number }> {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  let reads = 0
  const observer = new QueryObserver(qc, {
    queryKey: ['safety-switches'],
    queryFn: async () => { reads += 1; return { degraded: false, switches: [] } },
  })
  unsubscribe = observer.subscribe(() => {})
  await settle()
  reads = 0
  return { reads: () => reads }
}

describe('safety switches cache', () => {
  it('re-reads on the config refresh frame another tab\'s save sends', async () => {
    const { reads } = await observeSafetySwitches()
    invalidateRefreshQueries(qc, ['config'])
    await settle()
    expect(reads()).toBe(1)
  })

  it('re-reads after a reconnect, which may have missed that frame', async () => {
    const { reads } = await observeSafetySwitches()
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(reads()).toBe(1)
  })
})
