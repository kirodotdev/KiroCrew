/** The socket layer's connect/disconnect helpers move the store flag AND the
 *  errorReport seam the "Ask the agent" button reads, so the button follows
 *  the real socket state end to end. */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import AskAgentButton from '../../components/AskAgentButton'
import reducer, { sseConnected, sseDisconnected, sseStatus } from '../../store/dashboardSlice'
import type { StatusData } from '../../types'
import { __resetGatewayConnectedForTests, isGatewayConnected } from '../../utils/errorReport'
import { markGatewayConnected, markGatewayDisconnected } from './gatewayConnectivity'

beforeEach(() => { __resetGatewayConnectedForTests() })
afterEach(() => { __resetGatewayConnectedForTests() })

describe('gatewayConnectivity helpers', () => {
  it('disconnect dispatches sseDisconnected and hides the hand-off; connect restores both', () => {
    const dispatch = vi.fn()
    render(<AskAgentButton message="Failed to fetch" />)
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()

    act(() => markGatewayDisconnected(dispatch as never))
    expect(dispatch).toHaveBeenLastCalledWith(sseDisconnected())
    expect(isGatewayConnected()).toBe(false)
    expect(screen.queryByRole('button', { name: /ask the agent/i })).not.toBeInTheDocument()

    act(() => markGatewayConnected(dispatch as never))
    expect(dispatch).toHaveBeenLastCalledWith(sseConnected())
    expect(isGatewayConnected()).toBe(true)
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  // Pins the errorReport docstring: sseStatus sets the store flag on its own,
  // so after a disconnect the two flags can differ.
  it('sseStatus sets the store flag on its own, leaving the seam apart', () => {
    let state = reducer(undefined, { type: '@@INIT' })
    const dispatch = (action: Parameters<typeof reducer>[1]) => { state = reducer(state, action) }

    markGatewayDisconnected(dispatch as never)
    expect(state.connected).toBe(false)
    expect(isGatewayConnected()).toBe(false)

    dispatch(sseStatus({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 } as StatusData))
    expect(state.connected).toBe(true)
    expect(isGatewayConnected()).toBe(false)
  })
})
