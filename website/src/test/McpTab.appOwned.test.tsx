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
// The modal fires its own queries and has its own suite; not under test here.
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

// The mocked list resolves, react-query settles, and the table renders the row.
const ROW_WAIT = { timeout: 5000 }

const server = (overrides: Partial<McpServer>): McpServer => ({
  name: 'chorus:chorus',
  command: 'chorus-mcp',
  status: 'ok',
  source: 'agent',
  enabled: true,
  tools: [],
  presence: { kirocrew: true, kiroGlobal: false },
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

async function rowOf(name: string): Promise<HTMLElement> {
  const code = await screen.findByText(name, { selector: 'code' }, ROW_WAIT)
  const row = code.closest('tr')
  if (!row) throw new Error(`no table row for ${name}`)
  return row
}

function kirocrewSwitch(row: HTMLElement): HTMLElement {
  const sw = row.querySelector<HTMLElement>('[data-scope="kirocrew"]')
  if (!sw) throw new Error('no Kiro Crew switch on the row')
  return sw
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({})
})

describe("McpTab: an installed app's server", () => {
  // The agent rebuild re-derives the server from the app's manifest, so an
  // uninstall from here cannot stick: the row names the app instead and keeps
  // the one control that does work, its Kiro Crew switch.
  it('names its app, offers no Uninstall, and keeps a live Kiro Crew switch', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ app: 'chorus' })])
    renderTab()
    const row = await rowOf('chorus:chorus')
    expect(within(row).getByTestId('mcp-app-owned-chip')).toHaveTextContent('From the chorus app')
    expect(within(row).queryByRole('button', { name: 'Uninstall' })).toBeNull()
    expect(within(row).queryByTestId('mcp-builtin-chip')).toBeNull()
    expect(kirocrewSwitch(row)).not.toBeDisabled()
  })

  it('a colon in the name alone is not app ownership', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ name: 'npm:foo' })])
    renderTab()
    const row = await rowOf('npm:foo')
    expect(within(row).queryByTestId('mcp-app-owned-chip')).toBeNull()
    expect(within(row).getByRole('button', { name: 'Uninstall' })).toBeInTheDocument()
  })
})
