import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
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
// The modal fires its own queries and has its own suite; not under test here.
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

// Every row assertion waits on the same chain: the mocked `api.mcpServers`
// promise resolves, react-query settles the query, and the table renders the
// row. The tooltip waits add one click-driven render on top of that. A named
// budget keeps the wait explicit rather than leaning on the library default.
const ROW_WAIT = { timeout: 5000 }

// The exact text the backend's direct probe sets when it refuses a server whose
// env holds a `secret://` reference. Only env KEYS are named, never a value.
const REFUSAL =
  'not probed: env API_TOKEN holds a secret:// reference, which is resolved only when the MCP gateway starts the server'

const server = (overrides: Partial<McpServer>): McpServer => ({
  name: 'vault-backed',
  command: 'vault-backed-cmd',
  status: 'ok',
  source: 'agent',
  enabled: true,
  tools: [],
  ...overrides,
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // MemoryRouter because the sign-in guidance renders a <Link>, which throws
  // without a router context.
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

describe('McpTab: an outdated row that was not probed', () => {
  it('states the refusal reason on the row', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ status: 'outdated', error: REFUSAL })])
    renderTab()
    const reason = await screen.findByTestId('mcp-not-probed-reason', undefined, ROW_WAIT)
    expect(reason).toHaveTextContent(REFUSAL)
    expect(reason).toHaveClass('text-warn')
    expect(reason).not.toHaveClass('text-danger')
    // Prose wraps between words; `break-all` would split them mid-word.
    expect(reason).toHaveClass('break-words')
    expect(reason).not.toHaveClass('break-all')
    // The reason line belongs to the server's own row.
    expect(within(rowOf('vault-backed')).getByTestId('mcp-not-probed-reason')).toBe(reason)
  })

  it('links the reason to the guide on routing through the MCP gateway', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ status: 'outdated', error: REFUSAL })])
    renderTab()
    const reason = await screen.findByTestId('mcp-not-probed-reason', undefined, ROW_WAIT)
    const link = within(reason).getByRole('link', { name: 'How to route this server through the MCP gateway' })
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/secrets-env.md#route-the-server-through-the-gateway',
    )
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('drops the aged-result hint, which a refresh cannot satisfy', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ status: 'outdated', error: REFUSAL })])
    renderTab()
    const badge = await screen.findByText('Outdated', undefined, ROW_WAIT)
    expect(within(badge.parentElement!).queryByRole('button', { name: 'More information' })).toBeNull()
    expect(screen.queryByText(/30 minutes/)).toBeNull()
  })

  it('shows no empty-tools dash beside the reason', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ status: 'outdated', error: REFUSAL })])
    renderTab()
    const reason = await screen.findByTestId('mcp-not-probed-reason', undefined, ROW_WAIT)
    expect(reason.parentElement!.textContent).not.toContain('—')
  })

  it('keeps a cached tool list under the reason', async () => {
    mockApi.mcpServers.mockResolvedValue([
      server({ status: 'outdated', error: REFUSAL, tools: ['lookup', 'store'] }),
    ])
    renderTab()
    await screen.findByTestId('mcp-not-probed-reason', undefined, ROW_WAIT)
    expect(within(rowOf('vault-backed')).getByText(/2\s+tools/i)).toBeInTheDocument()
  })
})

describe('McpTab: outdated rows without a reason are unchanged', () => {
  it('an aged reading keeps the refresh hint and shows no reason line', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ status: 'outdated', error: '' })])
    renderTab()
    const badge = await screen.findByText('Outdated', undefined, ROW_WAIT)
    fireEvent.click(within(badge.parentElement!).getByRole('button', { name: 'More information' }))
    const tip = await screen.findByRole('tooltip', undefined, ROW_WAIT)
    expect(tip.textContent ?? '').toMatch(/30 minutes/)
    expect(screen.queryByTestId('mcp-not-probed-reason')).toBeNull()
  })

  it('a quarantined row keeps its Failing badge and shows no reason line', async () => {
    mockApi.mcpServers.mockResolvedValue([
      server({ status: 'outdated', error: '', probeFailures: 3, probeFailing: true }),
    ])
    renderTab()
    const badge = await screen.findByText('Outdated', undefined, ROW_WAIT)
    expect(within(badge.parentElement!).queryByRole('button', { name: 'More information' })).toBeNull()
    expect(screen.getByText('Failing')).toBeInTheDocument()
    expect(screen.queryByTestId('mcp-not-probed-reason')).toBeNull()
  })

  it('an error row still renders its error, not the not-probed line', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ status: 'error', error: 'timeout after 15s' })])
    renderTab()
    expect(await screen.findByText(/timeout after 15s/, undefined, ROW_WAIT)).toBeInTheDocument()
    expect(screen.queryByTestId('mcp-not-probed-reason')).toBeNull()
  })
})
