import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useMonitorContract } from './useMonitorContract'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../components/autoNudgeLoop'
import {
  activeMonitorKinds,
  isKnownMonitorKind,
  normalizeAutomationRecord,
  resetRuntimeMonitorContract,
  type StructuredMonitor,
} from '../monitoring/automation'
import { structuredMonitorLoop as structuredLoop } from '../test/monitorFixtures'

const CONTRIBUTED_KIND = 'salesforce_pull_request'

const monitorContract = vi.fn()
vi.mock('../api/client', () => ({
  api: { monitorContract: () => monitorContract() },
}))

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
}

describe('useMonitorContract', () => {
  let client: QueryClient

  beforeEach(() => {
    // The hook owns its own retry predicate; collapse the backoff to ~0 so a
    // non-404 fault's retries settle within the test.
    client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
    resetRuntimeMonitorContract()
    monitorContract.mockReset()
  })

  afterEach(() => {
    resetRuntimeMonitorContract()
    client.clear()
  })

  it('installs the gateway kind list so a contributed kind is recognized', async () => {
    monitorContract.mockResolvedValue({
      pullRequestMonitorKinds: ['github_pull_request', CONTRIBUTED_KIND],
    })
    renderHook(() => useMonitorContract(), { wrapper: wrapper(client) })
    await waitFor(() => expect(isKnownMonitorKind(CONTRIBUTED_KIND)).toBe(true))
    expect(activeMonitorKinds().has('github_pull_request')).toBe(true)
  })

  it('falls back to the bundled contract on a 404 (older gateway)', async () => {
    monitorContract.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
    renderHook(() => useMonitorContract(), { wrapper: wrapper(client) })
    // The bundled vocabulary stands; the contributed kind is never admitted.
    await waitFor(() => expect(monitorContract).toHaveBeenCalled())
    expect(isKnownMonitorKind(CONTRIBUTED_KIND)).toBe(false)
    expect(isKnownMonitorKind('github_pull_request')).toBe(true)
  })

  it('falls back to the bundled contract on any fetch error', async () => {
    monitorContract.mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    renderHook(() => useMonitorContract(), { wrapper: wrapper(client) })
    // One shot, no retry: the fetch settles once and the bundled vocabulary
    // stays in force.
    await waitFor(() => expect(monitorContract).toHaveBeenCalledTimes(1))
    expect(isKnownMonitorKind(CONTRIBUTED_KIND)).toBe(false)
    expect(isKnownMonitorKind('github_pull_request')).toBe(true)
  })

  it('a payload without a kind list leaves the bundled kinds in force', async () => {
    monitorContract.mockResolvedValue({ monitorStateVersion: 1 })
    renderHook(() => useMonitorContract(), { wrapper: wrapper(client) })
    await waitFor(() => expect(monitorContract).toHaveBeenCalled())
    expect(isKnownMonitorKind(CONTRIBUTED_KIND)).toBe(false)
    expect(isKnownMonitorKind('github_pull_request')).toBe(true)
  })

  it('repaints an already-normalized contributed-kind record with no extra poll', async () => {
    // A contributed-kind record normalized BEFORE the contract arrives reads as
    // blocked. The fetch resolves exactly once (no re-poll); installing the
    // contract invalidates the dependent queries, which is what flips the record
    // to its real state.
    const before = normalizeAutomationRecord(
      structuredLoop({ kind: CONTRIBUTED_KIND }),
    ) as StructuredMonitor
    expect(before.actionable).toBe(false)

    const invalidate = vi.spyOn(client, 'invalidateQueries')
    monitorContract.mockResolvedValue({
      pullRequestMonitorKinds: ['github_pull_request', CONTRIBUTED_KIND],
    })
    renderHook(() => useMonitorContract(), { wrapper: wrapper(client) })

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY }),
    )
    // Re-normalizing the same raw record now yields its real state, and the
    // monitor-contract query was fetched exactly once.
    const after = normalizeAutomationRecord(
      structuredLoop({ kind: CONTRIBUTED_KIND }),
    ) as StructuredMonitor
    expect(after.actionable).toBe(true)
    expect(monitorContract).toHaveBeenCalledTimes(1)
  })

  it('touches no queries when the payload adds nothing beyond the bundled kinds', async () => {
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    monitorContract.mockResolvedValue({ pullRequestMonitorKinds: ['github_pull_request'] })
    renderHook(() => useMonitorContract(), { wrapper: wrapper(client) })
    await waitFor(() => expect(monitorContract).toHaveBeenCalled())
    expect(invalidate).not.toHaveBeenCalled()
  })
})
