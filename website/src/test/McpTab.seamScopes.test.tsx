import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpApply: vi.fn(),
  mcpProbe: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'ACP', labels: { pluginRegistryName: 'Packages' } }),
}))
// McpBrowserModal fires its own queries; not under test here.
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const SERVER = {
  name: 'aws-outlook-mcp',
  command: '/x/aws-outlook-mcp',
  status: 'ok',
  tools: ['send'],
  source: 'agent',
  enabled: true,
  disabledTools: [],
  presence: { kirocrew: true, kiroGlobal: true, ccGlobal: false },
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><McpTab /></QueryClientProvider>)
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.mcpServers.mockResolvedValue([SERVER])
  mockApi.mcpApply.mockResolvedValue({ applied: 1 })
})

describe('McpTab — seam-aware Globals column', () => {
  it('shows only the core Kiro global badge when no provider scope is configured', async () => {
    mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
    renderTab()
    expect(await screen.findByRole('button', { name: /Kiro:/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Claude/ })).toBeNull()
  })

  it('re-surfaces a companion scope badge when the seam contributes one', async () => {
    mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [{ id: 'ccGlobal', label: 'Claude' }] })
    renderTab()
    expect(await screen.findByRole('button', { name: /Claude:/ })).toBeInTheDocument()
  })

  it('toggling the companion scope and applying sends its <id>Global presence', async () => {
    mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [{ id: 'ccGlobal', label: 'Claude' }] })
    renderTab()

    // ccGlobal starts off (presence.ccGlobal === false) → click to enable.
    const claude = await screen.findByRole('button', { name: /Claude:/ })
    fireEvent.click(claude)

    // Pending change surfaces the Apply button; commit it.
    const applyBtn = await screen.findByRole('button', { name: /Apply/ })
    fireEvent.click(applyBtn)

    await waitFor(() => expect(mockApi.mcpApply).toHaveBeenCalled())
    const changes = mockApi.mcpApply.mock.calls[0][0]
    expect(changes).toHaveLength(1)
    // Core scopes preserved, companion scope enabled.
    expect(changes[0]).toMatchObject({
      name: 'aws-outlook-mcp',
      kirocrew: true,
      kiroGlobal: true,
      ccGlobal: true,
    })
  })
})
