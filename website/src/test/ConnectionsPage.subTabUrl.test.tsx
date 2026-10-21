// #21303: the Connections sub-tab is read from and written to `?view=`, so a
// link such as `/capabilities?tab=mcp&view=mcp-servers` lands on the MCP
// Servers list, and a hand switch keeps the URL in step without adding a Back
// step. The page's only outside seams are `api` (every call resolves empty) and
// the MCP Servers list itself, stubbed at its module boundary.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    mcpServers: vi.fn(async () => []),
    mcpProbe: vi.fn(async () => []),
    connectionsStatus: vi.fn(async () => ({ schema_version: 1, connections: [] })),
    connectionsPremint: vi.fn(async () => ({ ok: true, preminting: [] })),
    connectionsMintState: vi.fn(async () => ({ state: 'idle' })),
  },
}))

vi.mock('../pages/overview/McpTab', () => ({
  default: () => <p>mcp servers list</p>,
}))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'

/** Prints the router's current URL and offers a Back control, so a test can see
 *  both what the page wrote and whether it wrote it as a new history entry. */
function UrlProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output data-testid="url">{location.pathname + location.search}</output>
      <button type="button" onClick={() => navigate(-1)}>back</button>
    </>
  )
}

/** `entries` is the history stack; the last one is where the page opens. */
function mount(entries: string[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
            <ConnectionsPage servicesEnabled />
            <UrlProbe />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const tab = (name: RegExp) => screen.getByRole('tab', { name })
const url = () => screen.getByTestId('url').textContent

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Connections sub-tab in the URL', () => {
  it('opens on MCP Servers when the link carries view=mcp-servers', async () => {
    mount(['/capabilities?tab=mcp&view=mcp-servers'])
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
  })

  it('falls back to Services on an unknown view', async () => {
    mount(['/capabilities?tab=mcp&view=bogus'])
    await waitFor(() => expect(tab(/Services/)).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByText('mcp servers list')).not.toBeInTheDocument()
  })

  it('writes the switch into the URL by replacing, so Back skips it', async () => {
    mount(['/before', '/capabilities?tab=mcp'])

    fireEvent.click(tab(/MCP Servers/))
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))

    // Arrow keys go through the same writer, and Services is the bare URL.
    fireEvent.keyDown(tab(/MCP Servers/), { key: 'ArrowRight' })
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp'))
    expect(tab(/Services/)).toHaveAttribute('aria-selected', 'true')

    // Two switches, zero new entries: one Back leaves the page.
    fireEvent.click(screen.getByRole('button', { name: 'back' }))
    await waitFor(() => expect(url()).toBe('/before'))
  })
})
