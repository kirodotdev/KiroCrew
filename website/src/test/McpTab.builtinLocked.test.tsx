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

// The mocked list resolves, react-query settles, and the table renders the row.
const ROW_WAIT = { timeout: 5000 }

const server = (overrides: Partial<McpServer>): McpServer => ({
  name: 'kirocrew-core',
  command: 'kirocrew',
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

describe('McpTab: a built-in Kiro Crew server', () => {
  it('locks its Kiro Crew switch on, says why, and offers no Uninstall', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ builtin: true })])
    renderTab()
    const row = await rowOf('kirocrew-core')
    const sw = kirocrewSwitch(row)
    expect(sw).toBeDisabled()
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(sw.getAttribute('aria-label') ?? '').toMatch(/cannot be turned off/)
    expect(sw).toHaveClass('disabled:cursor-default')
    expect(within(sw).getByTestId('mcp-scope-switch-glyph')).toHaveClass('lucide-toggle-right')
    expect(within(sw).getByTestId('mcp-scope-switch-lock')).toHaveClass('lucide-lock')
    expect(within(row).getByTestId('mcp-builtin-chip')).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Uninstall' })).toBeNull()
  })

  it('keeps a live switch when it is already off, and a staged turn-on can be clicked back', async () => {
    mockApi.mcpServers.mockResolvedValue([
      server({ builtin: true, enabled: false, status: 'disabled', disabledIn: 'kirocrew', presence: { kirocrew: false, kiroGlobal: false } }),
    ])
    renderTab()
    const row = await rowOf('kirocrew-core')
    const sw = kirocrewSwitch(row)
    expect(sw).not.toBeDisabled()
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(sw).toHaveClass('cursor-pointer')
    expect(within(sw).getByTestId('mcp-scope-switch-glyph')).toHaveClass('lucide-toggle-left')
    expect(within(sw).queryByTestId('mcp-scope-switch-lock')).toBeNull()
    fireEvent.click(sw)
    expect(kirocrewSwitch(row)).not.toBeDisabled()
    expect(kirocrewSwitch(row)).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(kirocrewSwitch(row))
    expect(kirocrewSwitch(row)).toHaveAttribute('aria-checked', 'false')
  })
})

describe('McpTab: a third-party server', () => {
  it('keeps its live switch and Uninstall, with no built-in chip', async () => {
    mockApi.mcpServers.mockResolvedValue([server({ name: 'weather', builtin: false })])
    renderTab()
    const row = await rowOf('weather')
    expect(kirocrewSwitch(row)).not.toBeDisabled()
    expect(kirocrewSwitch(row)).toHaveClass('cursor-pointer')
    expect(kirocrewSwitch(row)).not.toHaveClass('disabled:cursor-default')
    expect(within(kirocrewSwitch(row)).getByTestId('mcp-scope-switch-glyph')).toHaveClass('lucide-toggle-right')
    expect(within(kirocrewSwitch(row)).queryByTestId('mcp-scope-switch-lock')).toBeNull()
    expect(row.querySelector('.lucide-lock')).toBeNull()
    expect(within(row).queryByTestId('mcp-builtin-chip')).toBeNull()
    expect(within(row).getByRole('button', { name: 'Uninstall' })).toBeInTheDocument()
  })
})
