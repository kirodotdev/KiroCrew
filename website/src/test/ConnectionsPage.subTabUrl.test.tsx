// #21303: the Connections sub-tab is read from and written to `?view=`, so a
// link such as `/capabilities?tab=mcp&view=mcp-servers` lands on the MCP
// Servers list, and a hand switch keeps the URL in step without adding a Back
// step. The page's only outside seams are `api` (every call resolves empty) and
// the MCP Servers list itself, stubbed at its module boundary.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from 'react-router-dom'

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

// Desktop unless a test opts into the phone layout.
let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import SidePanelLayout, { type SidePanelTab } from '../components/SidePanelLayout'
import { SUBNAV_PUSH_STATE } from '../components/subNavParams'
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
  return render(
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
  mobile = false
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

describe('Connections sub-tab on a return without view', () => {
  it('opens Services in a browser tab that has not shown MCP Servers', async () => {
    mount(['/capabilities?tab=mcp'])
    await waitFor(() => expect(tab(/Services/)).toHaveAttribute('aria-selected', 'true'))
    expect(url()).toBe('/capabilities?tab=mcp')
  })

  it('resumes MCP Servers and writes it back into the URL', async () => {
    const first = mount(['/capabilities?tab=mcp&view=mcp-servers'])
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
    first.unmount()

    mount(['/capabilities?tab=mcp'])
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))
  })

  it('resumes Services when Services was the last one picked', async () => {
    const first = mount(['/capabilities?tab=mcp&view=mcp-servers'])
    fireEvent.click(tab(/Services/))
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp'))
    first.unmount()

    mount(['/capabilities?tab=mcp'])
    await waitFor(() => expect(tab(/Services/)).toHaveAttribute('aria-selected', 'true'))
    expect(screen.queryByText('mcp servers list')).not.toBeInTheDocument()
    expect(url()).toBe('/capabilities?tab=mcp')
  })

  it('resumes MCP Servers on a view that names no Connections sub-tab, without forgetting it', async () => {
    const first = mount(['/capabilities?tab=mcp&view=mcp-servers'])
    await screen.findByText('mcp servers list')
    first.unmount()

    const second = mount(['/capabilities?tab=mcp&view=budget'])
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))
    second.unmount()

    mount(['/capabilities?tab=mcp'])
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
  })
})

// The sidebar's Customize entry opens a bare `/capabilities`. SidePanelLayout
// restores Connections from its own memory and writes `?tab=` from an effect in
// the same flush as the sub-tab's own URL write, so this drives the real layout.
const CAPABILITY_TABS: SidePanelTab[] = [
  { key: 'crews', label: 'Crewmates', icon: null },
  { key: 'mcp', label: 'Connections', icon: null },
  { key: 'skills', label: 'Skills', icon: null },
]

/** Navigates within the mounted page, as the sidebar entry does when clicked
 *  from Customize itself. */
function GoTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(to)}>go {to}</button>
}

/** Reports how the router reached the current entry and whether that entry
 *  still carries SidePanelLayout's phone drill-in marker. */
function HistoryProbe() {
  const location = useLocation()
  const navType = useNavigationType()
  const pushed = !!(location.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]
  return <output data-testid="history" data-navtype={navType} data-push-marker={String(pushed)} />
}

function mountCustomize(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[entry]}>
            <SidePanelLayout title="Customize" tabs={CAPABILITY_TABS} rememberKey="capabilities">
              {active => <>
                {active === 'mcp' && <ConnectionsPage servicesEnabled />}
                {active === 'skills' && <p>skills pane</p>}
              </>}
            </SidePanelLayout>
            <UrlProbe />
            <HistoryProbe />
            <GoTo to="/capabilities" />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('Connections sub-tab through the Customize layout', () => {
  it('returns from a chat to MCP Servers when the sidebar opens a bare /capabilities', async () => {
    const first = mountCustomize('/capabilities?tab=mcp&view=mcp-servers')
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
    first.unmount()

    mountCustomize('/capabilities')
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))
  })

  it('stays on MCP Servers when the sidebar entry is clicked from Customize', async () => {
    mountCustomize('/capabilities?tab=mcp&view=mcp-servers')
    await screen.findByText('mcp servers list')

    fireEvent.click(screen.getByRole('button', { name: 'go /capabilities' }))
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
  })

  it("keeps MCP Servers when Connections is picked from Skills' Context Budget view", async () => {
    const first = mountCustomize('/capabilities?tab=mcp&view=mcp-servers')
    await screen.findByText('mcp servers list')
    first.unmount()

    // A tab switch keeps `view`, so Connections opens with Skills' value in it.
    const second = mountCustomize('/capabilities?tab=skills&view=budget')
    expect(screen.getByText('skills pane')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Connections'))
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
    expect(tab(/MCP Servers/)).toHaveAttribute('aria-selected', 'true')
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))
    second.unmount()

    // And the memory still holds MCP Servers for the next bare return.
    mountCustomize('/capabilities')
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
  })

  it('leaves the phone back bar able to pop after resuming MCP Servers', async () => {
    const first = mountCustomize('/capabilities?tab=mcp&view=mcp-servers')
    await screen.findByText('mcp servers list')
    first.unmount()

    mobile = true
    mountCustomize('/capabilities')
    fireEvent.click(screen.getByRole('button', { name: 'Connections' }))
    expect(await screen.findByText('mcp servers list')).toBeInTheDocument()
    await waitFor(() => expect(url()).toBe('/capabilities?tab=mcp&view=mcp-servers'))
    // The resume write replaced the drill-in entry but kept its marker.
    expect(screen.getByTestId('history')).toHaveAttribute('data-push-marker', 'true')

    fireEvent.click(screen.getByRole('button', { name: /Customize/ }))
    await waitFor(() => expect(screen.getByTestId('history')).toHaveAttribute('data-navtype', 'POP'))
    expect(url()).toBe('/capabilities')
  })
})
