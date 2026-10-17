import React from 'react'
import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { Provider } from 'react-redux'
import dashboardReducer from '../store/dashboardSlice'
import { useConnected } from '../hooks/useConnected'
import type { RootState } from '../store'
import { createConnectedTestStore, createTestStore } from './helpers'

/** Read the shared connection gate the way a store-connected component does. */
function readConnected(store: ReturnType<typeof createTestStore>): boolean {
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <Provider store={store}>{children}</Provider>
  )
  return renderHook(() => useConnected(), { wrapper }).result.current
}

const initialDashboard = dashboardReducer(undefined, { type: '@@test/init' })

describe('createTestStore', () => {
  it('starts disconnected, like the dashboard before its stream opens', () => {
    expect(readConnected(createTestStore())).toBe(false)
  })
})

describe('createConnectedTestStore', () => {
  it('connects the dashboard, so connection-gated code takes its connected branch', () => {
    expect(readConnected(createConnectedTestStore())).toBe(true)
  })

  it('otherwise keeps the reducer initial state', () => {
    const dashboard = createConnectedTestStore().getState().dashboard
    expect(dashboard).toEqual({ ...initialDashboard, connected: true })
  })

  it('lays the dashboard fields a test names over the initial state', () => {
    const dashboard = createConnectedTestStore({
      dashboard: { approvalMode: 'auto' } as unknown as RootState['dashboard'],
    }).getState().dashboard
    expect(dashboard.approvalMode).toBe('auto')
    expect(dashboard.slots).toEqual(initialDashboard.slots)
    expect(dashboard.connected).toBe(true)
  })

  it('stays connected even when the passed dashboard says otherwise', () => {
    const dashboard = createConnectedTestStore({
      dashboard: { connected: false } as unknown as RootState['dashboard'],
    }).getState().dashboard
    expect(dashboard.connected).toBe(true)
  })

  it('passes other slices through unchanged', () => {
    const chat = { ...createTestStore().getState().chat, marker: 'kept' } as unknown as RootState['chat']
    const state = createConnectedTestStore({ chat }).getState()
    expect(state.chat).toBe(chat)
  })
})
