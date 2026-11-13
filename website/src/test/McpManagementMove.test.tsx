/**
 * MCP Management lives at the top of Customize → MCP Servers (`McpPage`).
 *
 * The decision that stubs MCP servers and shares their backends used to live on
 * the Developer internals pages (`mcp-pool`), then below the servers list on a
 * Customize sub-tab. This suite pins where it is now:
 *
 *  1. `/mcp` renders MCP Management (the sharing hero and approval card) above
 *     its Servers and Sharing views — WITHOUT Developer Mode — and Settings →
 *     Connections, whose cards are a different thing, does not render it.
 *  2. `?view=` picks the view, Servers by default, and a switch replaces the
 *     history entry.
 *  3. Old links land there: `/developer?tab=mcp-pool`, and the Customize
 *     `?tab=mcp` forms, which `legacyConnectionsTarget` maps.
 *
 * McpTab is heavy (server queries); it is mocked to a marker, because what is
 * under test is the WIRING, not its body. McpManagement renders for real with
 * an empty gateway.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor, render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'

import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    mcpServers: () => Promise.resolve([]),
    connectionsStatus: () => Promise.resolve({ connections: [] }),
    connectionsMintState: () => Promise.resolve(undefined),
    connectionsPremint: () => Promise.resolve(undefined),
    mcpProbe: () => Promise.resolve([]),
    mcpGatewayStatus: () => Promise.resolve({ enabled: false, supported: true, stub: [], running: [] }),
    mcpGatewayServers: () => Promise.resolve({ servers: [] }),
    mcpGatewayFootprint: () => Promise.resolve(null),
  },
}))
vi.mock('../hooks/useConnectionsUi', () => ({ useConnectionsUiEnabled: () => true }))
vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'packages' } }),
}))
vi.mock('../pages/overview/McpTab', () => ({ default: () => <div data-testid="mcp-tab" /> }))
vi.mock('../pages/connections/OAuthAppPanel', async () => ({
  OAuthAppPanel: () => null,
  OAuthDraftsContext: (await vi.importActual<typeof import('react')>('react')).createContext<(slug: string, dirty: boolean) => void>(() => {}),
  OAuthClientWrittenContext: (await vi.importActual<typeof import('react')>('react')).createContext<(slugs: string[]) => void>(() => {}),
  OAuthDraftValuesContext: (await vi.importActual<typeof import('react')>('react')).createContext({ get: () => undefined, set: () => {} }),
}))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import McpPage from '../pages/McpPage'
import DeveloperPage from '../pages/DeveloperPage'
import { legacyConnectionsTarget } from '../pages/connections/connectionsLinks'
import { i18nT } from '../i18n/t'

// MCP Management sits behind McpPage's React.lazy import, so a wait for it
// waits on that dynamic import and gets its own deadline.
const LAZY_PAGE_TIMEOUT_MS = 5000

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>
}

function Back() {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(-1)}>back</button>
}

function wrapDeveloper(entry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route path="/developer" element={<DeveloperPage />} />
          <Route path="/capabilities" element={<Where />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const heroTitle = () => i18nT('pages.mcpManagement.hero_title')

describe('MCP Management on the MCP Servers page', () => {
  it('renders the sharing hero above the Servers view, without Developer Mode', async () => {
    renderWithProviders(<McpPage />, { route: '/mcp' })
    const hero = await screen.findByText(heroTitle(), undefined, { timeout: LAZY_PAGE_TIMEOUT_MS })
    const list = await screen.findByTestId('mcp-tab', undefined, { timeout: LAZY_PAGE_TIMEOUT_MS })
    expect(hero.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByRole('tab', { name: i18nT('pages.mcpPage.view_servers') })).toHaveAttribute('aria-selected', 'true')
  })

  it('opens the Sharing view from ?view=sharing, with no Advanced toggle in front of it', async () => {
    renderWithProviders(<McpPage />, { route: '/mcp?view=sharing' })
    const sharing = await screen.findByRole('tab', { name: i18nT('pages.mcpPage.view_sharing') }, { timeout: LAZY_PAGE_TIMEOUT_MS })
    expect(sharing).toHaveAttribute('aria-selected', 'true')
    // The server list stays mounted (hidden) under the Sharing view.
    expect(screen.getByTestId('mcp-tab')).not.toBeVisible()
    expect(screen.getByRole('tab', { name: i18nT('pages.mcpManagement.view_servers') })).toBeInTheDocument()
  })

  it('switches views by replacing the history entry', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter initialEntries={['/before', '/mcp']} initialIndex={1}>
        <QueryClientProvider client={qc}>
          <Routes>
            <Route path="/mcp" element={<McpPage />} />
            <Route path="*" element={null} />
          </Routes>
          <Where />
          <Back />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const sharing = await screen.findByRole('tab', { name: i18nT('pages.mcpPage.view_sharing') }, { timeout: LAZY_PAGE_TIMEOUT_MS })
    fireEvent.mouseDown(sharing)
    fireEvent.click(sharing)
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/mcp?view=sharing'))
    fireEvent.click(screen.getByRole('button', { name: 'back' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/before'))
  })

  it('keeps the server list mounted on the Sharing view, so staged edits survive', async () => {
    renderWithProviders(<McpPage />, { route: '/mcp' })
    const list = await screen.findByTestId('mcp-tab', undefined, { timeout: LAZY_PAGE_TIMEOUT_MS })
    fireEvent.mouseDown(screen.getByRole('tab', { name: i18nT('pages.mcpPage.view_sharing') }))
    await waitFor(() => expect(screen.getByRole('tab', { name: i18nT('pages.mcpPage.view_sharing') })).toHaveAttribute('aria-selected', 'true'))
    expect(screen.getByTestId('mcp-tab')).toBe(list)
    expect(list).not.toBeVisible()
  })

  it('is not on Settings → Connections', async () => {
    renderWithProviders(<ConnectionsPage servicesEnabled />, { route: '/settings/connections' })
    await screen.findByRole('region', { name: 'Connections' })
    expect(screen.queryByText(heroTitle())).toBeNull()
    expect(screen.queryByTestId('mcp-tab')).toBeNull()
  })
})

describe('old links land on the new pages', () => {
  it('/developer?tab=mcp-pool opens the Sharing view', async () => {
    wrapDeveloper('/developer?tab=mcp-pool')
    expect(await screen.findByTestId('where')).toHaveTextContent('/capabilities?tab=mcp-servers&view=sharing')
  })

  it('maps every Customize ?tab=mcp form, keeping the other params', () => {
    expect(legacyConnectionsTarget('?tab=mcp&view=mcp-servers', '')).toBe('/capabilities?tab=mcp-servers')
    expect(legacyConnectionsTarget('?tab=mcp&view=mcp-servers', '#mcp-management')).toBe('/capabilities?tab=mcp-servers&view=sharing')
    expect(legacyConnectionsTarget('?tab=mcp', '')).toBe('/settings/connections')
    expect(legacyConnectionsTarget('?tab=mcp&provider=github', '')).toBe('/settings/connections?provider=github')
    expect(legacyConnectionsTarget('?tab=skills', '')).toBeNull()
    expect(legacyConnectionsTarget('', '')).toBeNull()
  })
})
