import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import chatReducer, { selectAutomationForSlot } from '../../store/chatSlice'
import type { AppDispatch } from '../../store'
import { useAutomationSeed } from './automationSeed'
import { useMonitorContract } from '../useMonitorContract'
import { resetRuntimeMonitorContract, type StructuredMonitor } from '../../monitoring/automation'
import { structuredMonitorLoop } from '../../test/monitorFixtures'

const CONTRIBUTED_KIND = 'salesforce_pull_request'

// Mock only the endpoints these two hooks touch.
const autonudgeList = vi.fn()
const monitorsList = vi.fn()
const monitorContract = vi.fn()
vi.mock('../../api/client', () => ({
  api: {
    autonudgeList: () => autonudgeList(),
    monitorsList: () => monitorsList(),
    monitorContract: () => monitorContract(),
  },
}))

/* The contributed-kind monitor as the structured REST list ships it: a running
   monitor whose kind the bundled contract does not know. */
const contributedMonitorWire = structuredMonitorLoop({ kind: CONTRIBUTED_KIND, active: true })

function actionableForSlot(
  store: ReturnType<typeof configureStore>,
  slot: string,
): boolean | undefined {
  const rec = selectAutomationForSlot(store.getState() as never, slot) as StructuredMonitor | null
  return rec?.actionable
}

describe('runtime contract re-seeds the Redux automation projection', () => {
  let store: ReturnType<typeof configureStore>
  let client: QueryClient

  beforeEach(() => {
    resetRuntimeMonitorContract()
    autonudgeList.mockReset().mockResolvedValue({ enabled: true, loops: [] })
    monitorsList.mockReset().mockResolvedValue({ enabled: true, monitors: [contributedMonitorWire] })
    monitorContract.mockReset()
    store = configureStore({ reducer: { chat: chatReducer } })
    client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  })

  afterEach(() => {
    resetRuntimeMonitorContract()
    client.clear()
  })

  function wrapper({ children }: { children: ReactNode }) {
    return createElement(
      QueryClientProvider,
      { client },
      createElement(Provider, { store }, children),
    )
  }

  it('flips a seeded contributed-kind record from blocked to actionable on install, no websocket frame', async () => {
    // The contract resolves only after the first seed below, so the record first
    // enters Redux normalized against the bundled (contributed-kind-unaware)
    // vocabulary. A deferred promise lets the test control that ordering.
    let resolveContract: (value: { pullRequestMonitorKinds: string[] }) => void = () => {}
    monitorContract.mockReturnValue(
      new Promise<{ pullRequestMonitorKinds: string[] }>(resolve => {
        resolveContract = resolve
      }),
    )

    // Both hooks mounted as useWebSocket wires them: ONE seed instance, with the
    // contract hook driving that same instance's seedAutomations on install.
    const { result } = renderHook(
      () => {
        const seed = useAutomationSeed(store.dispatch as AppDispatch, client)
        useMonitorContract(seed.seedAutomations)
        return seed
      },
      { wrapper },
    )

    // Cold-seed before the contract installs: the contributed-kind record enters
    // Redux as blocked (non-actionable), the user-visible bug.
    await act(async () => {
      result.current.seedAutomations()
      await Promise.resolve()
    })
    await waitFor(() => expect(actionableForSlot(store, 'chat-1')).toBe(false))
    const listCallsBeforeInstall = monitorsList.mock.calls.length

    // Install the contract. useMonitorContract re-runs the SAME seed, which
    // re-normalizes the record to its real, actionable state.
    await act(async () => {
      resolveContract({ pullRequestMonitorKinds: ['github_pull_request', CONTRIBUTED_KIND] })
      await Promise.resolve()
    })
    await waitFor(() => expect(actionableForSlot(store, 'chat-1')).toBe(true))

    // The flip came from a REST re-seed, never a websocket frame: this test
    // never dispatched onAutonudgeState. The contract was fetched exactly once.
    expect(monitorsList.mock.calls.length).toBeGreaterThan(listCallsBeforeInstall)
    expect(monitorContract).toHaveBeenCalledTimes(1)
  })
})
