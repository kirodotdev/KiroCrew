// #25962: opening a chat unmounts Customize, so changes staged on MCP Servers
// but not yet applied must come back when the page mounts again in the same tab.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpApply: vi.fn(),
  mcpProbe: vi.fn(),
  kirocrewConfig: vi.fn(),
  acpBackends: vi.fn(),
  mcpGatewayStatus: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi, ApiError: class ApiError extends Error {} }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'
import { MCP_STAGED_CHANGES_KEY } from '../pages/overview/mcpStagedChangesMemory'

const server = (name: string) => ({
  name,
  command: `/x/${name}`,
  status: 'ok',
  tools: ['send'],
  source: 'kirocrew',
  enabled: true,
  disabledTools: [],
  presence: { kirocrew: true, kiroGlobal: false },
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

const row = (name: string) => screen.getByText(name, { selector: 'code' }).closest('tr')!

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue([server('alpha'), server('beta')])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.mcpApply.mockResolvedValue({ applied: 1 })
  mockApi.kirocrewConfig.mockResolvedValue({})
  mockApi.acpBackends.mockResolvedValue({ backends: [] })
  mockApi.mcpGatewayStatus.mockResolvedValue({ enabled: false })
})

describe('McpTab — staged changes survive leaving the page (#25962)', () => {
  it('brings a staged scope toggle back after an unmount and applies it', async () => {
    const first = renderTab()
    fireEvent.click(await within(await waitFor(() => row('alpha'))).findByRole('switch', { name: /Kiro:/ }))
    expect(await screen.findByRole('button', { name: /^Apply/ })).toBeInTheDocument()
    first.unmount()

    renderTab()
    const apply = await screen.findByRole('button', { name: /^Apply/ })
    fireEvent.click(apply)
    await waitFor(() => expect(mockApi.mcpApply).toHaveBeenCalled())
    expect(mockApi.mcpApply.mock.calls[0][0]).toEqual([
      expect.objectContaining({ name: 'alpha', kirocrew: true, kiroGlobal: true }),
    ])
  })

  it('brings a staged uninstall back, and Discard clears the stored copy', async () => {
    const first = renderTab()
    fireEvent.click(await within(await waitFor(() => row('beta'))).findByRole('button', { name: 'Uninstall' }))
    expect(await within(row('beta')).findByRole('button', { name: 'Undo' })).toBeInTheDocument()
    first.unmount()

    const second = renderTab()
    expect(await within(await waitFor(() => row('beta'))).findByRole('button', { name: 'Undo' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Discard/ }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /Discard/ })).toBeNull())
    second.unmount()

    renderTab()
    expect(await within(await waitFor(() => row('beta'))).findByRole('button', { name: 'Uninstall' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Discard/ })).toBeNull()
  })

  it('opens with nothing staged when the stored value is unreadable', async () => {
    sessionStorage.setItem(MCP_STAGED_CHANGES_KEY, '{not json')
    renderTab()
    await waitFor(() => expect(row('alpha')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /Discard/ })).toBeNull()
  })
})
