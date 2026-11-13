import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer } from '../types'

/* ── Mocks: must run before importing the component ── */
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

// The row renders once the mocked `api.mcpServers` promise resolves and
// react-query settles; a named budget keeps that wait explicit.
const ROW_WAIT = { timeout: 5000 }

const server = (overrides: Partial<McpServer>): McpServer => ({
  name: 'example-server',
  command: 'example-mcp-program',
  status: 'ok',
  source: 'kiro-global',
  enabled: true,
  tools: [],
  ...overrides,
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

function rowOf(name: string): HTMLElement {
  const row = screen.getByText(name, { selector: 'code' }).closest('tr')
  if (!row) throw new Error(`no table row for ${name}`)
  return row
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({})
})

describe('McpTab: a server the agent rebuild dropped', () => {
  it('says in plain words that the program was not found, and what to do', async () => {
    mockApi.mcpServers.mockResolvedValue([
      server({ agentDropped: { command: 'example-mcp-program', reason: 'command_not_found' } }),
    ])
    renderTab()
    const note = await screen.findByTestId('mcp-agent-dropped', undefined, ROW_WAIT)
    expect(note).toHaveTextContent(
      "The program 'example-mcp-program' wasn't found on this computer, so your agent can't use this server's tools.",
    )
    expect(note).toHaveTextContent('set the full path to the program')
    expect(note).toHaveTextContent('in MCP Servers.')
    expect(note).toHaveTextContent('Settings → About → Restart gateway')
    expect(within(rowOf('example-server')).getByTestId('mcp-agent-dropped')).toBe(note)
  })

  it('shows no note on a server the agent has', async () => {
    mockApi.mcpServers.mockResolvedValue([server({})])
    renderTab()
    await screen.findByText('example-server', { selector: 'code' }, ROW_WAIT)
    expect(screen.queryByTestId('mcp-agent-dropped')).toBeNull()
  })
})
