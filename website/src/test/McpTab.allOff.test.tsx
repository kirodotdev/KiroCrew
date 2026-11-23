import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer, McpApplyChange } from '../types'

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
// The modal fires its own queries and has its own suite; not under test here.
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const ROW_WAIT = { timeout: 5000 }

const server = (name: string, overrides: Partial<McpServer> = {}): McpServer => ({
  name,
  command: `/x/${name}`,
  status: 'ok',
  source: 'kirocrew',
  enabled: true,
  tools: [],
  presence: { kirocrew: true, kiroGlobal: false },
  ...overrides,
})

/** Off in Kiro Crew's own store: what a registry install or custom add starts as. */
const off = { enabled: false, status: 'disabled', disabledIn: 'kirocrew', presence: { kirocrew: false, kiroGlobal: false } } as const

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

const rowOf = (name: string): HTMLElement => {
  const row = screen.getByText(name, { selector: 'code' }).closest('tr')
  if (!row) throw new Error(`no table row for ${name}`)
  return row
}

const switchOf = (name: string): HTMLElement => {
  const sw = rowOf(name).querySelector<HTMLElement>('[data-scope="kirocrew"]')
  if (!sw) throw new Error(`no Kiro Crew switch on ${name}`)
  return sw
}

const allOff = () => screen.getByTestId('mcp-all-off')

/** The changes the one Apply sent, by server name. */
async function applied(): Promise<Record<string, McpApplyChange>> {
  fireEvent.click(screen.getByRole('button', { name: /Apply/ }))
  await waitFor(() => expect(mockApi.mcpApply).toHaveBeenCalledTimes(1))
  const changes = mockApi.mcpApply.mock.calls[0][0] as McpApplyChange[]
  return Object.fromEntries(changes.map(c => [c.name, c]))
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.mcpApply.mockResolvedValue({ applied: 1 })
  mockApi.kirocrewConfig.mockResolvedValue({})
  mockApi.acpBackends.mockResolvedValue({ backends: [] })
  mockApi.mcpGatewayStatus.mockResolvedValue({ enabled: false })
})

describe('McpTab: All off stages every listed Kiro Crew switch off', () => {
  it('stages each live switch off and leaves built-in, shared-disabled, off and uninstalling rows', async () => {
    mockApi.mcpServers.mockResolvedValue([
      server('weather'),
      server('notes'),
      server('core', { builtin: true }),
      server('figma', { enabled: false, status: 'disabled', disabledIn: 'shared' }),
      server('fresh-install', off),
      server('old'),
    ])
    renderTab()
    await screen.findByText('weather', { selector: 'code' }, ROW_WAIT)
    fireEvent.click(within(rowOf('old')).getByRole('button', { name: 'Uninstall' }))

    fireEvent.click(allOff())

    expect(switchOf('weather')).toHaveAttribute('aria-checked', 'false')
    expect(switchOf('notes')).toHaveAttribute('aria-checked', 'false')
    expect(switchOf('core')).toHaveAttribute('aria-checked', 'true')
    expect(switchOf('fresh-install')).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText(/3 pending changes/)).toBeInTheDocument()
    expect(allOff()).toBeDisabled()
    const sent = await applied()
    expect(Object.keys(sent).sort()).toEqual(['notes', 'old', 'weather'])
    expect(sent.weather.kirocrew).toBe(false)
    expect(sent.notes.kirocrew).toBe(false)
    expect(sent.old).toEqual({ name: 'old', uninstall: true })
  })

  it('stages only the rows the search shows', async () => {
    mockApi.mcpServers.mockResolvedValue([server('weather'), server('weather-alerts'), server('notes')])
    renderTab()
    await screen.findByText('weather', { selector: 'code' }, ROW_WAIT)

    fireEvent.change(screen.getByPlaceholderText(/Filter servers or tools/), { target: { value: 'weather' } })
    expect(screen.queryByText('notes', { selector: 'code' })).toBeNull()
    fireEvent.click(allOff())

    expect(screen.getByText(/2 pending changes/)).toBeInTheDocument()
    const sent = await applied()
    expect(Object.keys(sent).sort()).toEqual(['weather', 'weather-alerts'])
    expect(Object.values(sent).map(c => c.kirocrew)).toEqual([false, false])
  })

  it('is disabled when every listed switch is already off, and while Apply runs', async () => {
    mockApi.mcpServers.mockResolvedValue([server('weather'), server('notes', off)])
    let finish: (v: { applied: number }) => void = () => {}
    mockApi.mcpApply.mockReturnValue(new Promise(resolve => { finish = resolve }))
    renderTab()
    await screen.findByText('weather', { selector: 'code' }, ROW_WAIT)
    expect(allOff()).not.toBeDisabled()

    fireEvent.click(switchOf('notes'))
    expect(screen.getByText(/1 pending change/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Apply/ }))
    await waitFor(() => expect(mockApi.mcpApply).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(allOff()).toBeDisabled())

    finish({ applied: 1 })
    await waitFor(() => expect(screen.queryByText(/1 pending change/)).toBeNull())
    await waitFor(() => expect(allOff()).not.toBeDisabled())
    fireEvent.click(allOff())
    expect(allOff()).toBeDisabled()
  })
})
