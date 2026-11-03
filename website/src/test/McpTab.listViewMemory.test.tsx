// The MCP Servers list unmounts whenever the reader leaves Customize for a chat.
// What they narrowed (the filter) and opened (a server's tool list) is kept for
// the browser session, so the return lands on the same rows. The api module is
// the only outside seam; every call resolves a fixed two-server answer.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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

vi.mock('../components/McpBrowserModal', () => ({
  default: () => null,
}))

import McpTab from '../pages/overview/McpTab'

const server = (name: string): McpServer => ({
  name, command: `${name}-cmd`, status: 'ok', source: 'kirocrew', enabled: true, tools: [`${name}_search`],
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

const filterBox = () => screen.getByPlaceholderText('Filter servers or tools…') as HTMLInputElement
const row = (name: string) => screen.queryByText(name, { selector: 'code' })

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue([server('alpha'), server('beta')])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({})
})

describe('McpTab list view across a leave and return', () => {
  it('keeps the server filter', async () => {
    const first = renderTab()
    expect(await screen.findByText('beta', { selector: 'code' })).toBeInTheDocument()
    fireEvent.change(filterBox(), { target: { value: 'alp' } })
    expect(row('beta')).not.toBeInTheDocument()
    first.unmount()

    renderTab()
    expect(await screen.findByText('alpha', { selector: 'code' })).toBeInTheDocument()
    expect(filterBox().value).toBe('alp')
    expect(row('beta')).not.toBeInTheDocument()
  })

  it('keeps an opened tool list open and a closed one closed', async () => {
    const first = renderTab()
    await screen.findByText('beta', { selector: 'code' })
    const toggles = screen.getAllByRole('button', { name: /1 tools/ })
    expect(toggles).toHaveLength(2)
    fireEvent.click(toggles[0])
    expect(await screen.findByText('alpha_search')).toBeInTheDocument()
    first.unmount()

    renderTab()
    expect(await screen.findByText('alpha_search')).toBeInTheDocument()
    expect(screen.queryByText('beta_search')).not.toBeInTheDocument()
  })

  it('opens unfiltered with every list closed on a fresh browser tab', async () => {
    renderTab()
    await screen.findByText('beta', { selector: 'code' })
    expect(filterBox().value).toBe('')
    expect(screen.queryByText('alpha_search')).not.toBeInTheDocument()
  })

  it('ignores a saved view it cannot read', async () => {
    sessionStorage.setItem('kirocrew:mcp-servers-list-view', '{not json')
    renderTab()
    await screen.findByText('beta', { selector: 'code' })
    expect(filterBox().value).toBe('')
  })
})
