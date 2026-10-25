// The MCP servers list says when a server set to share is not shared.
//
// Contract under test:
// - a server on the share list whose launch the gateway refused is named, with
//   the restart line and a link to Developer > MCP Management, where it is approved.
// - the link lands on /developer?tab=mcp-pool, a route served whether or not
//   Developer Mode shows it in the side rail.
// - the list comes from the gateway's `share_refused`, which matches a refusal
//   recorded under a slash-named server's alias.
// - a failed status read renders an error notice; a 403 for a non-owner is silent;
//   an expired-session 403 keeps polling.
// - the open notice re-reads status, so a refusal recorded later still shows.
// - a refusal kept for a server no longer on the share list, sharing switched
//   off, or no refusal at all renders nothing.
// - with staged edits on the host page the link opens a new tab, so they survive.
// - McpTab renders the notice above its server table.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const mockApi = vi.hoisted(() => ({
  mcpGatewayStatus: vi.fn(),
  mcpServers: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  kirocrewConfig: vi.fn(),
  acpBackends: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import { ApiError } from '../api/apiError'
import McpShareRefusedNotice, { MCP_MANAGEMENT_PATH, STATUS_POLL_MS, refusedShares } from '../pages/overview/McpShareRefusedNotice'
import McpTab from '../pages/overview/McpTab'

const WAIT = { timeout: 5000 }

const status = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  stub: ['alpha-mcp', 'beta-mcp'],
  stub_count: 2,
  running: true,
  ping_ok: true,
  supported: true,
  ...over,
})

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search}</div>
}

let lastClient: QueryClient | undefined

function mount(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastClient = qc
  return render(
    <MemoryRouter initialEntries={['/capabilities?tab=mcp']}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route path="/capabilities" element={ui} />
          <Route path="/developer" element={<Where />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpServers.mockResolvedValue([])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({})
  mockApi.acpBackends.mockResolvedValue({ backends: [] })
})
afterEach(cleanup)

describe('McpShareRefusedNotice', () => {
  it('names every refused server set to share and links to MCP Management', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(
      status({
        share_refused: { 'beta-mcp': 'changed_needs_reapproval', 'alpha-mcp': 'added_outside_dashboard' },
      }),
    )
    mount(<McpShareRefusedNotice />)
    const notice = await screen.findByTestId('mcp-share-refused', undefined, WAIT)
    expect(notice).toHaveTextContent('2 servers set to share are not shared: alpha-mcp, beta-mcp.')
    expect(notice).toHaveTextContent('every chat starts its own copy')
    expect(notice).toHaveTextContent('Approve sharing')
    expect(notice).toHaveTextContent('Settings → About → Restart gateway')
    const link = screen.getByRole('link', { name: 'Review in MCP Management' })
    expect(link).toHaveAttribute('href', MCP_MANAGEMENT_PATH)
    fireEvent.click(link)
    expect(await screen.findByTestId('where')).toHaveTextContent('/developer?tab=mcp-pool')
  })

  it('opens MCP Management in a new tab while the page holds staged edits', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(
      status({ share_refused: { 'alpha-mcp': 'added_outside_dashboard' } }),
    )
    mount(<McpShareRefusedNotice stagedChanges />)
    await screen.findByTestId('mcp-share-refused', undefined, WAIT)
    const link = screen.getByRole('link', { name: 'Review in MCP Management' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('href', MCP_MANAGEMENT_PATH)
  })

  it('navigates in place when nothing is staged', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(
      status({ share_refused: { 'alpha-mcp': 'added_outside_dashboard' } }),
    )
    mount(<McpShareRefusedNotice />)
    await screen.findByTestId('mcp-share-refused', undefined, WAIT)
    expect(screen.getByRole('link', { name: 'Review in MCP Management' })).not.toHaveAttribute('target')
  })

  it('uses the singular form for one server', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(
      status({ share_refused: { 'alpha-mcp': 'added_outside_dashboard' } }),
    )
    mount(<McpShareRefusedNotice />)
    expect(await screen.findByTestId('mcp-share-refused', undefined, WAIT)).toHaveTextContent(
      '1 server set to share is not shared: alpha-mcp.',
    )
  })

  it('is rendered by McpTab', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(
      status({ share_refused: { 'alpha-mcp': 'added_outside_dashboard' } }),
    )
    mount(<McpTab />)
    expect(await screen.findByTestId('mcp-share-refused', undefined, WAIT)).toHaveTextContent('alpha-mcp')
  })

  it('is told by McpTab when a change is staged, so the link keeps the tab', async () => {
    mockApi.mcpGatewayStatus.mockResolvedValue(
      status({ share_refused: { 'alpha-mcp': 'added_outside_dashboard' } }),
    )
    mockApi.mcpServers.mockResolvedValue([
      { name: 'alpha-mcp', command: 'alpha-mcp', status: 'ok', source: 'agent', enabled: true, tools: [] },
    ])
    mount(<McpTab />)
    await screen.findByTestId('mcp-share-refused', undefined, WAIT)
    const link = () => screen.getByRole('link', { name: 'Review in MCP Management' })
    expect(link()).not.toHaveAttribute('target')
    fireEvent.click(await screen.findByRole('button', { name: /Uninstall/ }, WAIT))
    await waitFor(() => expect(link()).toHaveAttribute('target', '_blank'))
  })
})

describe('McpShareRefusedNotice refresh', () => {
  afterEach(() => vi.useRealTimers())

  it('shows a refusal recorded after the tab opened, on the next poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockApi.mcpGatewayStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ share_refused: { 'alpha-mcp': 'changed_needs_reapproval' } }))
    mount(<McpShareRefusedNotice />)
    await waitFor(() => expect(mockApi.mcpGatewayStatus).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('mcp-share-refused')).toBeNull()
    await vi.advanceTimersByTimeAsync(STATUS_POLL_MS)
    expect(await screen.findByTestId('mcp-share-refused', undefined, WAIT)).toHaveTextContent('alpha-mcp')
  })
})

describe('McpShareRefusedNotice read failures', () => {
  it('names a failed status read instead of passing for no refusals', async () => {
    mockApi.mcpGatewayStatus.mockRejectedValue(new Error('network down'))
    mount(<McpShareRefusedNotice />)
    expect(await screen.findByTestId('mcp-share-status-error', undefined, WAIT)).toHaveTextContent(
      'Could not check whether the servers set to share are approved.',
    )
    expect(screen.queryByTestId('mcp-share-refused')).toBeNull()
  })

  it('stays silent when the viewer is not the owner', async () => {
    mockApi.mcpGatewayStatus.mockRejectedValue(new ApiError(403, '{}'))
    mount(<McpShareRefusedNotice />)
    // Settled means retries are spent and the error is final, so a notice that
    // was going to render has rendered.
    await waitFor(
      () => expect(lastClient?.getQueryState(['mcpGatewayStatus'])?.status).toBe('error'),
      WAIT,
    )
    expect(mockApi.mcpGatewayStatus).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('mcp-share-status-error')).toBeNull()
    expect(screen.queryByTestId('mcp-share-refused')).toBeNull()
  })
})

describe('McpShareRefusedNotice session expiry', () => {
  afterEach(() => vi.useRealTimers())

  it('keeps polling through an expired-session 403 and shows a later refusal', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockApi.mcpGatewayStatus
      .mockRejectedValueOnce(new ApiError(403, 'session expired', '{}', true))
      .mockRejectedValueOnce(new ApiError(403, 'session expired', '{}', true))
      .mockResolvedValue(status({ share_refused: { 'alpha-mcp': 'added_outside_dashboard' } }))
    mount(<McpShareRefusedNotice />)
    await waitFor(
      () => expect(lastClient?.getQueryState(['mcpGatewayStatus'])?.status).toBe('error'),
      WAIT,
    )
    await vi.advanceTimersByTimeAsync(STATUS_POLL_MS)
    expect(await screen.findByTestId('mcp-share-refused', undefined, WAIT)).toHaveTextContent('alpha-mcp')
  })
})

describe('refusedShares', () => {
  it('reads the gateway-matched share list, not the raw refusal keys', () => {
    // The gateway matched `npm:@acme/mcp` to the refusal it recorded as `acme-mcp`.
    expect(
      refusedShares(status({
        stub: ['npm:@acme/mcp'],
        launch_refused: { 'acme-mcp': { reason: 'added_outside_dashboard' } },
        share_refused: { 'npm:@acme/mcp': 'added_outside_dashboard' },
      })),
    ).toEqual(['npm:@acme/mcp'])
    expect(
      refusedShares(status({ launch_refused: { 'gone-mcp': { reason: 'added_outside_dashboard' } } })),
    ).toEqual([])
  })

  it('is empty while sharing is switched off', () => {
    expect(
      refusedShares(status({ enabled: false, share_refused: { 'alpha-mcp': 'added_outside_dashboard' } })),
    ).toEqual([])
  })

  it('is empty when the gateway sends no refusals', () => {
    expect(refusedShares(status())).toEqual([])
    expect(refusedShares(undefined)).toEqual([])
  })
})
