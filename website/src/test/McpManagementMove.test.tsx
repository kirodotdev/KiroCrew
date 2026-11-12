/**
 * MCP Management lives in Customize → Connections → MCP Servers.
 *
 * The decision that stubs MCP servers and shares their backends used to live on
 * the Developer internals pages (`mcp-pool`). This suite pins where it is now:
 *
 *  1. In Customize → MCP, the MCP Servers sub-tab (`?tab=mcp&view=mcp-servers`)
 *     renders MCP Management beside the servers list — WITHOUT Developer Mode,
 *     and NOT on the Services sub-tab, whose cards are a different thing.
 *  2. `/developer?tab=mcp-pool` (bookmarks, docs, the share-refused notice's old
 *     target) redirects to the new Customize spot, landing on the MCP Servers
 *     sub-tab at the MCP Management section.
 *
 * McpManagement and McpTab are heavy (gateway + server queries); they are mocked
 * to markers, because what is under test is the WIRING, not their bodies.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor, render, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'

import { renderWithProviders } from './helpers'

// A test can swap in a pending server list to hold McpTab in its loading state.
const serversRead = vi.hoisted(() => ({ next: (): Promise<unknown[]> => Promise.resolve([]) }))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    mcpServers: () => serversRead.next(),
    connectionsStatus: () => Promise.resolve({ connections: [] }),
    connectionsMintState: () => Promise.resolve(undefined),
    connectionsPremint: () => Promise.resolve(undefined),
    mcpProbe: () => Promise.resolve([]),
  },
}))
vi.mock('../hooks/useConnectionsUi', () => ({ useConnectionsUiEnabled: () => true }))
vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'packages' } }),
}))
vi.mock('../pages/settings/McpManagement', () => ({
  McpManagement: () => <div data-testid="mcp-management" />,
}))
vi.mock('../pages/overview/McpTab', () => ({ default: () => <div data-testid="mcp-tab" /> }))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import DeveloperPage from '../pages/DeveloperPage'

// The `mcp-management` marker sits behind ConnectionsPage's React.lazy import of
// McpManagement (mocked above), so a wait for it waits on that dynamic import
// resolving and gets its own deadline rather than waitFor's 1 s default.
const LAZY_PAGE_TIMEOUT_MS = 5000

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>
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

describe('MCP Management in Customize → Connections → MCP Servers', () => {
  it('renders MCP Management beside the servers list, without Developer Mode', async () => {
    // No Developer Mode is set up anywhere; the MCP Servers sub-tab must still
    // show the surface, beside the servers list.
    renderWithProviders(<ConnectionsPage servicesEnabled />, { route: '/capabilities?tab=mcp&view=mcp-servers' })
    await waitFor(() => expect(screen.getByTestId('mcp-management')).toBeTruthy(), {
      timeout: LAZY_PAGE_TIMEOUT_MS,
    })
    expect(screen.getByTestId('mcp-tab')).toBeTruthy()
  })

  it('does not render MCP Management on the Services sub-tab', async () => {
    // Default (no `view`) is the Services gallery; MCP Management belongs with
    // the servers list, not the connector cards.
    renderWithProviders(<ConnectionsPage servicesEnabled />, { route: '/capabilities?tab=mcp' })
    await waitFor(() => expect(screen.getByRole('tab', { name: /Services/ })).toBeTruthy())
    expect(screen.queryByTestId('mcp-management')).toBeNull()
  })

  it('re-scrolls to the section on a REPEAT link to the same #mcp-management URL', async () => {
    // Clicking "Review in MCP Management" a second time navigates to the
    // identical URL while ConnectionsPage stays mounted. The scroll still fires,
    // because the effect keys off the location's navigation key as well as
    // activeTab/hash (which do not change on a repeat same-URL navigation).
    const scrollSpy = vi.fn()
    const origScroll = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollSpy
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      cb(0)
      return 0
    }) as typeof globalThis.requestAnimationFrame
    try {
      function Relink() {
        const nav = useNavigate()
        return (
          <button type="button" onClick={() => nav('/capabilities?tab=mcp&view=mcp-servers#mcp-management')}>
            relink
          </button>
        )
      }
      renderWithProviders(
        <>
          <Relink />
          <ConnectionsPage servicesEnabled />
        </>,
        { route: '/capabilities?tab=mcp&view=mcp-servers#mcp-management' },
      )
      await waitFor(() => expect(screen.getByTestId('mcp-management')).toBeTruthy(), {
        timeout: LAZY_PAGE_TIMEOUT_MS,
      })
      await waitFor(() => expect(scrollSpy).toHaveBeenCalled())
      const afterFirst = scrollSpy.mock.calls.length
      // Re-navigate to the SAME url — the GPT trigger. Must scroll again.
      await act(async () => {
        screen.getByText('relink').click()
      })
      await waitFor(() => expect(scrollSpy.mock.calls.length).toBeGreaterThan(afterFirst))
    } finally {
      Element.prototype.scrollIntoView = origScroll
      globalThis.requestAnimationFrame = origRaf
    }
  })

  it('waits for the servers list to load before scrolling to the section', async () => {
    // A cold deep link: while the shared ['mcp-servers'] query loads, McpTab
    // shows a short skeleton, so a scroll then would clamp at the top and be
    // left behind when the taller table arrives. The scroll must wait for it.
    const scrollSpy = vi.fn()
    const origScroll = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollSpy
    const origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      cb(0)
      return 0
    }) as typeof globalThis.requestAnimationFrame
    let resolveServers: (v: unknown[]) => void = () => {}
    serversRead.next = () => new Promise(resolve => { resolveServers = resolve })
    try {
      renderWithProviders(<ConnectionsPage servicesEnabled />, {
        route: '/capabilities?tab=mcp&view=mcp-servers#mcp-management',
      })
      await waitFor(() => expect(screen.getByTestId('mcp-management')).toBeTruthy(), {
        timeout: LAZY_PAGE_TIMEOUT_MS,
      })
      expect(scrollSpy).not.toHaveBeenCalled()
      await act(async () => {
        resolveServers([])
      })
      await waitFor(() => expect(scrollSpy).toHaveBeenCalled())
    } finally {
      serversRead.next = () => Promise.resolve([])
      Element.prototype.scrollIntoView = origScroll
      globalThis.requestAnimationFrame = origRaf
    }
  })
})

describe('/developer?tab=mcp-pool redirects to Customize → Connections → MCP Servers', () => {
  it('lands on the MCP Servers sub-tab at the MCP Management section', async () => {
    wrapDeveloper('/developer?tab=mcp-pool')
    expect(await screen.findByTestId('where')).toHaveTextContent(
      '/capabilities?tab=mcp&view=mcp-servers#mcp-management',
    )
  })
})
