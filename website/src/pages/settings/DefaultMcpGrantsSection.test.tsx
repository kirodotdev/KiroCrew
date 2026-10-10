/**
 * Settings > Developer > Agent MCP servers: the rows show what the default agent's
 * spec mounts; Add and Remove act on both servers; Remove asks first; the result
 * is a success line or an error notice naming the step that failed.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('../../api/client', () => ({
  api: {
    defaultMcpGrants: vi.fn(),
    setDefaultMcpGrants: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { DefaultMcpGrantsSection } from './DefaultMcpGrantsSection'

const state = (mounted: boolean, sessionControl = true) => ({
  servers: [
    { name: 'kirocrew-dashboard', mounted },
    { name: 'kirocrew-debug', mounted },
  ],
  session_control: sessionControl,
})

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
const addButton = () => screen.getByRole('button', { name: /Add dashboard and debug MCPs/i })
const removeButton = () => screen.getAllByRole('button', { name: /^Remove them$/i })[0]

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.defaultMcpGrants).mockResolvedValue(state(false))
})

describe('DefaultMcpGrantsSection', () => {
  it('shows the state read from the spec', async () => {
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(screen.getByTestId('default-mcp-state-kirocrew-dashboard').textContent).toMatch(/Not on the default agent/))
    expect(screen.getByTestId('default-mcp-state-kirocrew-debug').textContent).toMatch(/Not on the default agent/)
  })

  it('adds both and says so', async () => {
    vi.mocked(api.setDefaultMcpGrants).mockResolvedValue(state(true))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect((addButton() as HTMLButtonElement).disabled).toBe(false))
    vi.mocked(api.defaultMcpGrants).mockResolvedValue(state(true))
    fireEvent.click(addButton())
    await waitFor(() => expect(api.setDefaultMcpGrants).toHaveBeenCalledWith('add'))
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-done').textContent).toMatch(/Added/))
    expect(screen.getByTestId('default-mcp-state-kirocrew-debug').textContent).toMatch(/^On the default agent/)
  })

  it('asks before removing, and says a hand-added entry goes too', async () => {
    vi.mocked(api.defaultMcpGrants).mockResolvedValue(state(true))
    vi.mocked(api.setDefaultMcpGrants).mockResolvedValue(state(false))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect((removeButton() as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(removeButton())
    await waitFor(() => expect(screen.getByText(/including an entry you added by hand/i)).toBeTruthy())
    expect(api.setDefaultMcpGrants).not.toHaveBeenCalled()
    const confirmButtons = screen.getAllByRole('button', { name: /^Remove them$/i })
    fireEvent.click(confirmButtons[confirmButtons.length - 1])
    await waitFor(() => expect(api.setDefaultMcpGrants).toHaveBeenCalledWith('remove'))
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-done').textContent).toMatch(/Removed/))
  })

  it('names the step that failed', async () => {
    vi.mocked(api.setDefaultMcpGrants).mockRejectedValue(
      new ApiError(500, 'add failed at step rebuild', JSON.stringify({ code: 'step_failed', failed_step: 'rebuild' })),
    )
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect((addButton() as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(addButton())
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-save-error').textContent).toMatch(/rebuilding the agent specs/))
  })

  it('reports a request that never reached a step', async () => {
    vi.mocked(api.setDefaultMcpGrants).mockRejectedValue(new Error('network'))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect((addButton() as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(addButton())
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-save-error').textContent).toMatch(/Couldn't reach the gateway/))
  })

  it('shows the gateway refusal, not a reach failure, when no step ran', async () => {
    vi.mocked(api.setDefaultMcpGrants).mockRejectedValue(
      new ApiError(403, 'Only the dashboard owner can change this.', JSON.stringify({ code: 'owner_only' })),
    )
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect((addButton() as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(addButton())
    await waitFor(() => expect(screen.getByTestId('default-mcp-grants-save-error').textContent).toMatch(/Only the dashboard owner/))
    expect(screen.getByTestId('default-mcp-grants-save-error').textContent).not.toMatch(/Couldn't reach the gateway/)
  })

  it('says Session Control is off', async () => {
    vi.mocked(api.defaultMcpGrants).mockResolvedValue(state(false, false))
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(screen.getByTestId('default-mcp-session-control-note').textContent).toMatch(/Session Control is off/))
  })

  it('names the backends whose sessions never load these servers', async () => {
    vi.mocked(api.defaultMcpGrants).mockResolvedValue({ ...state(true), unreached_backends: ['deepseek', 'pi'] })
    wrap(<DefaultMcpGrantsSection />)
    await waitFor(() => expect(screen.getByTestId('default-mcp-unreached-note').textContent).toMatch(/deepseek, pi/))
  })
})
