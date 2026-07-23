import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { McpServer } from '../types'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))

// The modal has its own suite (McpBrowserModal.test.tsx) — probe only the
// open/close wiring here.
vi.mock('../components/McpBrowserModal', () => ({
  default: ({ open }: { open: boolean }) => (
    <div data-testid="mcp-browser-modal" data-open={String(open)} />
  ),
}))

import McpTab from '../pages/overview/McpTab'

const server = (name: string): McpServer => ({
  name, command: `${name}-cmd`, status: 'ok', source: 'kirocrew', enabled: true, tools: ['t1'],
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><McpTab /></QueryClientProvider>)
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue([server('alpha'), server('beta')])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
})

describe('McpTab restructure', () => {
  it('header shows MCP Servers with the installed count', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument())
  })

  it('the inline registry card is gone', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('MCP Servers (2)')).toBeInTheDocument())
    expect(screen.queryByText('Browse Integrations')).not.toBeInTheDocument()
    expect(screen.queryByText('Installed Integrations')).not.toBeInTheDocument()
  })

  it('Add Server button opens the browser modal', async () => {
    renderTab()
    const addBtn = await screen.findByRole('button', { name: /Add Server/ })
    expect(screen.getByTestId('mcp-browser-modal')).toHaveAttribute('data-open', 'false')
    fireEvent.click(addBtn)
    expect(screen.getByTestId('mcp-browser-modal')).toHaveAttribute('data-open', 'true')
  })

  it('keeps the installed-servers table as the page body', async () => {
    renderTab()
    // Both configured servers render as table rows (name in a <code> cell —
    // the status badge chips also contain the name, so scope the query).
    await waitFor(() => expect(screen.getByText('alpha', { selector: 'code' })).toBeInTheDocument())
    expect(screen.getByText('beta', { selector: 'code' })).toBeInTheDocument()
    expect(screen.getByText('alpha-cmd')).toBeInTheDocument()
    // Uninstall stays in the table (per-row action), not in the modal.
    expect(screen.getAllByRole('button', { name: 'Uninstall' })).toHaveLength(2)
  })
})
