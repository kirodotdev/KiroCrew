/**
 * A shared MCP server that waits for an approval puts a count on the Customize
 * page's MCP Servers tab, where it is approved, while another tab is showing.
 * The Customize rail row carries the same count (railBadges.mcpShare.test.tsx).
 * Pins:
 *  1. two waiting servers put a "2" pill on the MCP Servers row, named for what
 *     it counts;
 *  2. one waiting server is named in the singular (the label is a plural
 *     catalog key, so the count is chosen inside t());
 *  3. nothing waiting shows no pill.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

const mockApi = vi.hoisted(() => ({
  skillsPending: vi.fn(),
  kirocrewConfig: vi.fn(),
  mcpGatewayStatus: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

// Tab bodies are irrelevant; the tab rail is what is under test.
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div data-testid="crews-pane" /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/KnowledgePage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))

import CapabilitiesPage from '../pages/CapabilitiesPage'

const WAIT = { timeout: 5000 }

function wrap(initialEntry = '/capabilities?tab=crews') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={qc}>
        <CapabilitiesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockApi.skillsPending.mockReset().mockResolvedValue({ pending: [] })
  mockApi.kirocrewConfig.mockReset().mockResolvedValue({})
  mockApi.mcpGatewayStatus.mockReset()
})

describe('CapabilitiesPage: waiting MCP shares on the MCP Servers tab', () => {
  it('shows how many shared servers wait for an approval while another tab is open', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue({
      enabled: true,
      share_refused: { 'alpha-mcp': 'changed_needs_reapproval', 'beta-mcp': 'changed_needs_reapproval' },
    })
    wrap()
    await waitFor(() => expect(screen.getByTestId('crews-pane')).toBeTruthy(), WAIT)
    const pill = await screen.findByRole('status', { name: '2 servers waiting for approval' }, WAIT)
    expect(pill.textContent).toBe('2')
    // The pill sits on the MCP Servers row itself, not elsewhere on the page.
    expect(pill.closest('button')?.textContent).toContain('MCP Servers')
  })

  it('names one waiting server in the singular', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue({
      enabled: true,
      share_refused: { 'alpha-mcp': 'changed_needs_reapproval' },
    })
    wrap()
    const pill = await screen.findByRole('status', { name: '1 server waiting for approval' }, WAIT)
    expect(pill.textContent).toBe('1')
    expect(pill.getAttribute('title')).toBe('1 server waiting for approval')
  })

  it('shows no count when no server waits', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue({ enabled: true, share_refused: {} })
    wrap()
    await waitFor(() => expect(mockApi.mcpGatewayStatus).toHaveBeenCalled(), WAIT)
    await waitFor(() => expect(screen.getAllByRole('button', { name: /MCP Servers/ }).length).toBeGreaterThan(0), WAIT)
    expect(screen.queryByRole('status', { name: /servers waiting for approval/ })).toBeNull()
  })
})
