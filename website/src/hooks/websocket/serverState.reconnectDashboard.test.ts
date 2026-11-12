/**
 * A RECONNECT RE-READS EVERY OPEN DASHBOARD.
 *
 * An open Dashboard tab moves on `dashboard_fields`, `member_projection` and
 * `dashboard_value_written` frames, and every one sent while the socket was down is
 * lost. Its read sets no staleTime, so without this it sat on pre-drop values until
 * the 60 s fallback tick. After a gateway restart it is worse: the restart empties
 * the armed live pages, so a package page gets no push at all until a GET arms one
 * again -- and this re-read is that GET.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: { getCredentialRedaction: () => new Promise(() => {}) },
}))

import { MEMBER_DASHBOARD_QUERY_PREFIX, SESSION_DASHBOARD_QUERY_PREFIX, refreshServerStateAfterReconnect } from './serverState'
import {
  normalizeAutomationRecord,
  resetRuntimeMonitorContract,
  setRuntimeMonitorContract,
  type StructuredMonitor,
} from '../../monitoring/automation'
import { structuredMonitorLoop } from '../../test/monitorFixtures'

let qc: QueryClient
const unsubscribe: Array<() => void> = []
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

afterEach(() => {
  for (const u of unsubscribe.splice(0)) u()
  qc.clear()
})

/** One counting read per key; `held` keys stay in flight until released. */
async function open(keys: string[][], held: string[] = []) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const reads: Record<string, number> = {}
  const releases: Array<() => void> = []
  let holding = false
  for (const key of keys) {
    const id = key.join('/')
    reads[id] = 0
    const observer = new QueryObserver(qc, {
      queryKey: key,
      queryFn: () => {
        reads[id] += 1
        if (holding && held.includes(id)) return new Promise((resolve) => releases.push(() => resolve(id)))
        return Promise.resolve(id)
      },
    })
    unsubscribe.push(observer.subscribe(() => {}))
  }
  await settle()
  holding = true
  for (const id of Object.keys(reads)) reads[id] = 0
  return { reads, releases }
}

describe('refreshServerStateAfterReconnect', () => {
  it('re-reads an open crewmate dashboard and an open session dashboard', async () => {
    const member = [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'en', 'live']
    const session = [SESSION_DASHBOARD_QUERY_PREFIX, 'chat-1', 'en', 'live']
    const { reads } = await open([member, session])
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(reads[member.join('/')]).toBe(1)
    expect(reads[session.join('/')]).toBe(1)
  })

  it('re-reads every open crewmate dashboard, not only one', async () => {
    const a = [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'en', 'live']
    const b = [MEMBER_DASHBOARD_QUERY_PREFIX, 'nyx', 'Nyx', 'en', 'live']
    const { reads } = await open([a, b])
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(reads[a.join('/')]).toBe(1)
    expect(reads[b.join('/')]).toBe(1)
  })

  it('does not stack reads when two reconnects land while one is out', async () => {
    // A flapping socket: the one-read-at-a-time rule of `refreshDashboardReads`
    // still holds, so the gateway is asked once now and once after.
    const member = [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas', 'Atlas', 'en', 'live']
    const { reads, releases } = await open([member], [member.join('/')])
    refreshServerStateAfterReconnect(qc)
    await settle()
    refreshServerStateAfterReconnect(qc)
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(reads[member.join('/')]).toBe(1)
    releases.splice(0).forEach((r) => r())
    await settle()
    await settle()
    expect(reads[member.join('/')]).toBe(2)
  })

  it('asks nothing of the gateway for a dashboard no tab is showing', async () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const queryFn = vi.fn(() => Promise.resolve(1))
    await qc.prefetchQuery({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas'], queryFn })
    queryFn.mockClear()
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(queryFn).not.toHaveBeenCalled()
    // But the cached read is marked stale, so the next open re-reads.
    expect(qc.getQueryState([MEMBER_DASHBOARD_QUERY_PREFIX, 'atlas'])?.isInvalidated).toBe(true)
  })

  // A gateway restart can register a contributed monitor kind while the socket
  // is down. The runtime contract is fetched once with no staleTime, so without
  // a reconnect refetch the dashboard keeps the pre-restart vocabulary and the
  // new kind reads as blocked until the page reloads.
  it('refetches the monitor contract on reconnect so a newly added kind stops reading as blocked', async () => {
    const CONTRIBUTED = 'salesforce_pull_request'
    resetRuntimeMonitorContract()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    let reads = 0
    let served = { pullRequestMonitorKinds: ['github_pull_request'] }
    const observer = new QueryObserver(qc, {
      queryKey: ['monitor-contract'],
      queryFn: () => {
        reads += 1
        return Promise.resolve(served)
      },
    })
    unsubscribe.push(observer.subscribe(() => {}))
    await settle()

    // Install the first (contributed-kind-unaware) vocabulary, as the hook does.
    setRuntimeMonitorContract(qc.getQueryData(['monitor-contract']) as never)
    const before = normalizeAutomationRecord(
      structuredMonitorLoop({ kind: CONTRIBUTED }),
    ) as StructuredMonitor
    expect(before.actionable).toBe(false)
    reads = 0

    // The gateway now serves the contributed kind; reconnect must re-read it.
    served = { pullRequestMonitorKinds: ['github_pull_request', CONTRIBUTED] }
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(reads).toBe(1)

    setRuntimeMonitorContract(qc.getQueryData(['monitor-contract']) as never)
    const after = normalizeAutomationRecord(
      structuredMonitorLoop({ kind: CONTRIBUTED }),
    ) as StructuredMonitor
    expect(after.actionable).toBe(true)
    resetRuntimeMonitorContract()
  })
})
