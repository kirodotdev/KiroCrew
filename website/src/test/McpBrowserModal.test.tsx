import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { DiscoveredMcpServer } from '../types'

/* ── Mocks: must run before importing the component ── */
const { mockApi, MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    mockApi: {
      mcpDiscover: vi.fn(),
      mcpDiscoverDetail: vi.fn(),
      mcpDiscoverInstall: vi.fn(),
    },
    MockApiError,
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: MockApiError }))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

import McpBrowserModal from '../components/McpBrowserModal'

const officialServer = (over: Partial<DiscoveredMcpServer> = {}): DiscoveredMcpServer => ({
  id: 'io.github.acme/widgets',
  name: 'widgets',
  title: 'Widgets MCP',
  description: 'Widget tooling over MCP',
  provider: 'official',
  display_provider: 'MCP Registry',
  version: '1.2.3',
  repo_url: 'https://github.com/acme/widgets',
  installed: false,
  methods: ['npx'],
  deprecated: false,
  ...over,
})

function renderModal(open = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <McpBrowserModal open={open} onClose={() => {}} />
    </QueryClientProvider>
  )
  return { qc, ...utils }
}

/** Type into the search box and wait out the 300ms debounce. */
async function search(text: string) {
  fireEvent.change(screen.getByRole('combobox', { name: 'Search MCP servers' }), { target: { value: text } })
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpDiscoverDetail.mockResolvedValue({
    id: 'io.github.acme/widgets', name: 'widgets', title: 'Widgets MCP',
    description: 'Full widget description', provider: 'official', version: '1.2.3',
    repo_url: 'https://github.com/acme/widgets',
    install_plan: { method: 'npx', spec: { command: 'npx', args: ['-y', 'widgets-mcp@1.2.3'], env: {} } },
    required_env: [],
  })
})

describe('McpBrowserModal', () => {
  it('gates the search until at least 2 characters are typed', async () => {
    renderModal()
    expect(screen.getByText('Type at least 2 characters to search')).toBeInTheDocument()

    await search('w')
    // Outwait the 300ms debounce — a 1-char query must never hit the API.
    await new Promise(r => setTimeout(r, 400))
    expect(mockApi.mcpDiscover).not.toHaveBeenCalled()
    expect(screen.getByText('Type at least 2 characters to search')).toBeInTheDocument()
  })

  it('renders debounced search results with provider badges', async () => {
    mockApi.mcpDiscover.mockResolvedValue({
      results: [
        officialServer(),
        officialServer({ id: 'edition-bundle', name: 'edition-bundle', title: '', provider: 'capability', display_provider: 'Packages', version: '', methods: ['capability'] }),
      ],
      providers: ['official', 'capability'],
    })
    renderModal()
    await search('widgets')

    await waitFor(() => expect(screen.getByText('Widgets MCP')).toBeInTheDocument())
    expect(mockApi.mcpDiscover).toHaveBeenCalledWith('widgets')
    expect(screen.getAllByText('MCP Registry').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Packages').length).toBeGreaterThan(0)
    expect(screen.getByText('2 results')).toBeInTheDocument()
    expect(screen.getByText('Searching: official, capability')).toBeInTheDocument()
  })

  it('install flow flips the row to Installed and invalidates mcp-servers + mcp-discover', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    // Real official-registry contract: every fresh install lands disabled.
    mockApi.mcpDiscoverInstall.mockResolvedValue({ ok: true, name: 'widgets', required_env: [], method: 'npx', enabled: false })
    const { qc } = renderModal()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')

    await search('widgets')
    // Consent flow: rows are status-only — Install only exists in the
    // detail pane, next to the install-plan preview.
    const row = await screen.findByRole('option', { name: 'widgets' })
    expect(screen.queryByRole('button', { name: /Install/ })).not.toBeInTheDocument()
    fireEvent.click(row)
    // Button is consent-gated: it enables only after the plan preview loads.
    await screen.findByTestId('install-plan')
    const installBtn = await screen.findByRole('button', { name: /Install/ })
    invalidateSpy.mockClear() // isolate install-triggered invalidations from the on-open ones

    fireEvent.click(installBtn)
    await waitFor(() => expect(screen.getAllByText('Installed (disabled)').length).toBeGreaterThan(0))
    // Pane explains the disabled consent default and where to enable.
    expect(screen.getByTestId('installed-disabled-note')).toBeInTheDocument()

    expect(mockApi.mcpDiscoverInstall).toHaveBeenCalledWith('official', 'io.github.acme/widgets')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['mcp-servers'] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['mcp-discover'] })
  })

  it('shows the conflict state on a 409 name collision', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    mockApi.mcpDiscoverInstall.mockRejectedValue(new MockApiError(409, 'name already in use'))
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    await screen.findByTestId('install-plan')
    const installBtn = await screen.findByRole('button', { name: /Install/ })
    fireEvent.click(installBtn)

    await waitFor(() => expect(screen.getAllByText('Name in use').length).toBeGreaterThan(0))
    // No overwrite path for MCP conflicts — the row must not re-offer Install.
    expect(screen.queryByRole('button', { name: /Overwrite/ })).not.toBeInTheDocument()
  })

  it('surfaces non-409 install errors on the row', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    mockApi.mcpDiscoverInstall.mockRejectedValue(new MockApiError(503, 'provider unavailable'))
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    await screen.findByTestId('install-plan')
    fireEvent.click(await screen.findByRole('button', { name: /Install/ }))
    await waitFor(() => expect(screen.getAllByText('provider unavailable').length).toBeGreaterThan(0))
  })

  it('detail pane renders markdown description, install plan, and required-env callout', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    mockApi.mcpDiscoverDetail.mockResolvedValue({
      id: 'io.github.acme/widgets', name: 'widgets', title: 'Widgets MCP',
      description: 'Full **markdown** description', provider: 'official', version: '1.2.3',
      repo_url: 'https://github.com/acme/widgets',
      install_plan: { method: 'npx', spec: { command: 'npx', args: ['-y', 'widgets-mcp@1.2.3'], env: { API_KEY: '' } } },
      required_env: ['API_KEY'],
    })
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))

    await waitFor(() => expect(mockApi.mcpDiscoverDetail).toHaveBeenCalledWith('official', 'io.github.acme/widgets'))
    // Description goes through MarkdownRenderer only (mocked probe).
    await waitFor(() => expect(screen.getByTestId('md')).toHaveTextContent('Full **markdown** description'))
    // Install-plan preview: method badge + assembled command line.
    const plan = screen.getByTestId('install-plan')
    expect(plan).toHaveTextContent('npx')
    expect(plan).toHaveTextContent('npx -y widgets-mcp@1.2.3')
    // Required-env callout.
    const envBox = screen.getByTestId('required-env')
    expect(envBox).toHaveTextContent('API_KEY')
    expect(envBox).toHaveTextContent('Set these in the server config before enabling it.')
  })

  it('shows the remote url as the install plan for url-method servers', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer({ methods: ['url'] })], providers: ['official'] })
    mockApi.mcpDiscoverDetail.mockResolvedValue({
      id: 'io.github.acme/widgets', name: 'widgets', title: 'Widgets MCP',
      description: '', provider: 'official', version: '1.2.3', repo_url: '',
      install_plan: { method: 'url', spec: { url: 'https://mcp.acme.dev/http' } },
      required_env: [],
    })
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    await waitFor(() => expect(screen.getByTestId('install-plan')).toHaveTextContent('https://mcp.acme.dev/http'))
  })

  it('refuses to render a Source link for a non-http(s) repo_url (click-XSS guard)', async () => {
    // repo_url is publisher-controlled registry data — a javascript: scheme
    // must never become a clickable href in the detail pane.
    // Poison the SEARCH RESULT row — the detail pane renders its repo_url.
    mockApi.mcpDiscover.mockResolvedValue({
      results: [officialServer({ repo_url: 'javascript:alert(document.cookie)' })],
      providers: ['official'],
    })
    mockApi.mcpDiscoverDetail.mockResolvedValue({
      id: 'io.github.acme/widgets', name: 'widgets', title: 'Widgets MCP',
      description: '', provider: 'official', version: '1.2.3', repo_url: '',
      install_plan: null, required_env: [],
    })
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    await waitFor(() => expect(mockApi.mcpDiscoverDetail).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: /Source/ })).not.toBeInTheDocument()
  })

  it('renders the Source link for a normal https repo_url', async () => {
    mockApi.mcpDiscover.mockResolvedValue({
      results: [officialServer({ repo_url: 'https://github.com/acme/widgets' })],
      providers: ['official'],
    })
    mockApi.mcpDiscoverDetail.mockResolvedValue({
      id: 'io.github.acme/widgets', name: 'widgets', title: 'Widgets MCP',
      description: '', provider: 'official', version: '1.2.3', repo_url: '',
      install_plan: null, required_env: [],
    })
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    const link = await screen.findByRole('link', { name: /Source/ })
    expect(link).toHaveAttribute('href', 'https://github.com/acme/widgets')
  })

  it('keyboard: ArrowDown selects, Enter installs the selected server', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    mockApi.mcpDiscoverInstall.mockResolvedValue({ ok: true, name: 'widgets', required_env: [], method: 'npx', enabled: false })
    renderModal()

    await search('widgets')
    const input = screen.getByRole('combobox', { name: 'Search MCP servers' })
    await screen.findByText('Widgets MCP')

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'widgets' })).toHaveAttribute('aria-selected', 'true'))

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockApi.mcpDiscoverInstall).toHaveBeenCalledWith('official', 'io.github.acme/widgets'))
  })

  it('keyboard: Enter does not install before the detail preview has loaded', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    // Detail never resolves — the install-plan preview is never on screen.
    mockApi.mcpDiscoverDetail.mockReturnValue(new Promise(() => {}))
    renderModal()

    await search('widgets')
    await screen.findByRole('option', { name: 'widgets' })
    const input = screen.getByRole('combobox', { name: 'Search MCP servers' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })

    // Consent gate: no install without a rendered preview.
    expect(mockApi.mcpDiscoverInstall).not.toHaveBeenCalled()
  })

  it('detail-pane Install button is disabled until the plan preview loads', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    // Detail never resolves: the button must render but stay disabled.
    mockApi.mcpDiscoverDetail.mockReturnValue(new Promise(() => {}))
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    const btn = await screen.findByRole('button', { name: /Install/ })
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(mockApi.mcpDiscoverInstall).not.toHaveBeenCalled()
  })

  it('shows Installed (disabled) when the install landed disabled pending env config', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer()], providers: ['official'] })
    mockApi.mcpDiscoverInstall.mockResolvedValue({ ok: true, name: 'widgets', required_env: ['API_KEY'], method: 'npx', enabled: false })
    renderModal()

    await search('widgets')
    fireEvent.click(await screen.findByRole('option', { name: 'widgets' }))
    await screen.findByTestId('install-plan')
    fireEvent.click(await screen.findByRole('button', { name: /Install/ }))

    await waitFor(() => expect(screen.getAllByText('Installed (disabled)').length).toBeGreaterThan(0))
  })

  it('marks deprecated servers with a badge', async () => {
    mockApi.mcpDiscover.mockResolvedValue({ results: [officialServer({ deprecated: true })], providers: ['official'] })
    renderModal()
    await search('widgets')
    await waitFor(() => expect(screen.getAllByText('deprecated').length).toBeGreaterThan(0))
  })
})
