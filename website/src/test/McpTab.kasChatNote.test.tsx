/**
 * #24693: on the KAS harness a sign-in made from the MCP Servers table lands in
 * kiro-cli's cache, which a KAS chat never reads. Every row that offers Sign in
 * or Sign in again says so, off the tab's own config read; other harnesses, and
 * a config that has not loaded, show no line.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer } from '../types'

const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpResetProbeFailures: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const NOTE = "Chats on Kiro Agent don't use a sign-in made here. Sign in from the chat, with its Authorize card."

const userAdded = (name: string, overrides: Partial<McpServer>): McpServer => ({
  name,
  command: '',
  url: `https://${name}.example.com/mcp`,
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  ownerSignIn: true,
  ...overrides,
})

const ROWS = [
  userAdded('docs-internal', { authGrantPresent: false }),
  userAdded('wiki-internal', { authGrantPresent: true }),
  userAdded('tickets-internal', { status: 'error', error: 'HTTP 401' }),
]

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue(ROWS)
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
})

describe('McpTab sign-in rows on the KAS harness (#24693)', () => {
  it.each([
    ['KAS named', { agent: { acp_backend: 'kas' } }],
    ['KAS by default (no harness choice)', {}],
    ['KAS with sign-in persistence on', { agent: { acp_backend: 'kas', kas_persist_mcp_sign_in: true } }],
  ])('puts the line under every sign-in control: %s', async (_label, cfg) => {
    mockApi.kirocrewConfig.mockResolvedValue(cfg)
    renderTab()

    await waitFor(() => expect(screen.getAllByTestId('mcp-sign-in-kas-chat-note')).toHaveLength(3))
    for (const note of screen.getAllByTestId('mcp-sign-in-kas-chat-note')) expect(note).toHaveTextContent(NOTE)
    // The controls are unchanged.
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Sign in again' })).toHaveLength(2)
  })

  it('shows no line on the kiro-cli harness', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    renderTab()

    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Sign in again' })).toHaveLength(2))
    expect(screen.queryByTestId('mcp-sign-in-kas-chat-note')).toBeNull()
  })
})
