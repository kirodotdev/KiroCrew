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

  it('badges a registry-managed remote server', async () => {
    mockApi.mcpServers.mockResolvedValue([{
      ...server('notion'),
      command: '',
      url: 'https://mcp.notion.com/mcp',
    }])
    renderTab()
    await waitFor(() => expect(screen.getByText('Managed by Connections')).toBeInTheDocument())
  })
})

/**
 * #1853: the status probe runs without the OAuth token kiro-cli holds, so a
 * remote OAuth server answers it with 401 while the agent runtime calls the same
 * server fine. The gateway reports that as `needs_auth`, and the table must say
 * only what it knows — the authorization is not visible from here — rather than
 * calling a working server broken or claiming it needs a grant it may already have.
 */
describe('McpTab needs_auth status', () => {
  const remote = (status: string): McpServer => ({
    name: 'atlassian',
    command: '',
    url: 'https://mcp.atlassian.com/v1/sse',
    status,
    source: 'mcp.json',
    enabled: true,
    tools: [],
  })

  it('renders the not-verified state, not an error badge', async () => {
    mockApi.mcpServers.mockResolvedValue([remote('needs_auth')])
    renderTab()

    await waitFor(() => expect(screen.getByText('Not verified')).toBeInTheDocument())
    // The badge carries the warn tone, never the error tone.
    expect(screen.getByText('Not verified').className).toContain('text-warn')
    expect(screen.getByText('Not verified').className).not.toContain('text-danger')
    // Neither the old "Error" label nor the uninformative "Unknown" fallback.
    expect(screen.queryByText('Error')).not.toBeInTheDocument()
    expect(screen.queryByText('Unknown')).not.toBeInTheDocument()
  })

  it('explains the unverifiable status on hover, naming the server', async () => {
    mockApi.mcpServers.mockResolvedValue([remote('needs_auth')])
    renderTab()

    const badge = await screen.findByText('Not verified')
    const hint = badge.getAttribute('title') || ''
    // Says who holds the token and that a working server is still working —
    // the two facts that make the badge honest instead of alarming.
    expect(hint).toContain('atlassian')
    expect(hint).toContain('Kiro CLI')
    expect(hint).toMatch(/cannot see the authorization/)
  })

  it('leaves every other status without a hover explanation', async () => {
    mockApi.mcpServers.mockResolvedValue([remote('ok')])
    renderTab()

    const badge = await screen.findByText('Online')
    expect(badge).not.toHaveAttribute('title')
  })

  it('still renders a real failure as an error badge with its message', async () => {
    mockApi.mcpServers.mockResolvedValue([{ ...remote('error'), error: 'HTTP 500' }])
    renderTab()

    await waitFor(() => expect(screen.getByText('Error')).toBeInTheDocument())
    expect(screen.getByText('Error').className).toContain('text-danger')
    expect(screen.getByText('HTTP 500')).toBeInTheDocument()
    expect(screen.queryByText('Not verified')).not.toBeInTheDocument()
  })
})

describe('McpTab declared-vs-handshake status', () => {
  it('a declared server shows "Declared", never the green "Online"', async () => {
    // probeMode 'declared' means the tool list came from the package's own
    // static declaration — nothing spawned the server. Rendering the same green
    // "Online" as a handshake-proven row asserts something no one verified.
    mockApi.mcpServers.mockResolvedValue([
      { ...server('managed'), probeMode: 'declared', probedAt: 1_700_000_000 },
    ])
    renderTab()
    await waitFor(() => expect(screen.getByText('Declared')).toBeInTheDocument())
    expect(screen.queryByText('Online')).not.toBeInTheDocument()
  })

  it('a handshake-proven server still shows "Online"', async () => {
    mockApi.mcpServers.mockResolvedValue([
      { ...server('real'), probeMode: 'handshake', probedAt: 1_700_000_000 },
    ])
    renderTab()
    await waitFor(() => expect(screen.getByText('Online')).toBeInTheDocument())
    expect(screen.queryByText('Declared')).not.toBeInTheDocument()
  })
})
