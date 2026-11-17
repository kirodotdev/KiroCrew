/**
 * A RECONNECT REFETCHES THE READS THE OUTAGE BROKE.
 *
 * While the gateway (or a relay Crew's host) is gone, every panel's read fails
 * and the calm offline view covers the panels. When the socket comes back, a
 * read that failed and never held a value must be read again, or its panel
 * keeps its error once the cover lifts. A read that kept its last value is left
 * alone: it may sit under an unsaved draft.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: { getCredentialRedaction: () => new Promise(() => {}) },
}))

import { refreshServerStateAfterReconnect } from './serverState'

const settle = () => new Promise((resolve) => setTimeout(resolve, 0))
const unsubscribe: Array<() => void> = []
afterEach(() => { for (const u of unsubscribe.splice(0)) u() })

describe('refreshServerStateAfterReconnect', () => {
  it('refetches a read that failed with no value, and leaves one that kept its value', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    let gatewayUp = false
    const reads = { broken: 0, kept: 0 }
    const observe = (key: 'broken' | 'kept', fn: () => Promise<string>) => {
      const observer = new QueryObserver(qc, { queryKey: ['outage-test', key], queryFn: () => { reads[key] += 1; return fn() } })
      unsubscribe.push(observer.subscribe(() => {}))
    }
    observe('broken', () => (gatewayUp ? Promise.resolve('folders') : Promise.reject(new Error('NetworkError'))))
    observe('kept', () => Promise.resolve('draft source'))
    await settle()
    // The kept read then fails a refetch during the outage, holding its value.
    gatewayUp = false
    await qc.refetchQueries({ queryKey: ['outage-test', 'kept'] }).catch(() => {})
    qc.getQueryCache().find({ queryKey: ['outage-test', 'kept'] })!.setState({ status: 'error', error: new Error('NetworkError') })
    expect(qc.getQueryState(['outage-test', 'broken'])?.status).toBe('error')
    expect(qc.getQueryState(['outage-test', 'kept'])?.data).toBe('draft source')
    reads.broken = 0
    reads.kept = 0

    gatewayUp = true
    refreshServerStateAfterReconnect(qc)
    await settle()

    expect(reads.broken).toBe(1)
    expect(qc.getQueryState(['outage-test', 'broken'])?.data).toBe('folders')
    expect(reads.kept).toBe(0)
    qc.clear()
  })
})
