/**
 * Slack's Connections card needs a client saved under Settings → Connections.
 * Until one is, the gateway marks a `slack` row `connectionUnconfigured` and the
 * card leaves it alone: the row keeps the chat guidance, where the client the
 * user wrote in the entry signs in, instead of a Sign in that could only fail.
 * A marked row whose chat sign-in cannot start (`connectionNeedsApp`) names the
 * missing OAuth app instead. Once the record exists the card claims the row again.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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
  acpBackends: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const slackRow = (overrides: Partial<McpServer>): McpServer => ({
  name: 'slack',
  command: '',
  url: 'https://mcp.slack.com/mcp',
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  authGrantPresent: false,
  ...overrides,
})

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><McpTab /></QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
  mockApi.acpBackends.mockResolvedValue({ backends: [], kiro_selection_runs_on_kas: false })
})

describe('McpTab: a slack row and its Connections card', () => {
  it('names the missing OAuth app when no chat could start the sign-in', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({ connectionUnconfigured: true, connectionNeedsApp: true })])
    renderTab()

    const setUp = await waitFor(() => screen.getByRole('link', { name: 'Set up Slack' }))
    const href = setUp.getAttribute('href') ?? ''
    expect(href.startsWith('/settings/connections')).toBe(true)
    expect(href).toContain('highlight=connections.oauth-client-slack')
    expect(screen.getByText(/Slack needs its OAuth app before anyone can sign in/)).toBeInTheDocument()
    expect(screen.getByText('Sign-in required')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
  })

  it('keeps the chat guidance for an entry with its own client while no OAuth app is saved', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({ connectionUnconfigured: true, clientId: 'mine' })])
    renderTab()

    await waitFor(() => expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument())
    expect(screen.getByText('Sign-in required')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
    expect(screen.queryByText('Managed by Connections')).toBeNull()
  })

  it('keeps the in-place Sign in of a slack server the owner added, while no OAuth app is saved', async () => {
    // The gateway reports `ownerSignIn` for the owner's own server as long as the
    // Slack card has neither an app nor a grant, so the row signs in by its name.
    mockApi.mcpServers.mockResolvedValue([slackRow({ connectionUnconfigured: true, ownerSignIn: true })])
    renderTab()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    expect(screen.queryByText('Managed by Connections')).toBeNull()
    expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
  })

  it.each([
    { status: 'needs_auth', authGrantPresent: false },
    { status: 'error', error: 'OAuth grant rejected', authGrantPresent: false },
    { status: 'needs_auth', authGrantPresent: true },
    { status: 'needs_auth', authGrantPresent: false, ownerSignIn: true },
  ])('links to Crew sign-in setup without mounting Sign in: %j', async (state) => {
    mockApi.mcpServers.mockResolvedValue([slackRow({ ...state, connectionNeedsCrewSignIn: true })])
    renderTab()

    const setUp = await screen.findByRole('link', { name: 'Open MCP sign-in settings' })
    const href = setUp.getAttribute('href') ?? ''
    expect(href.startsWith('/settings/developer/feature-previews')).toBe(true)
    expect(href).toContain('highlight=developer.mcp-sign-in-by-kiro-crew')
    expect(screen.getByText(/Slack signs in through Kiro Crew/)).toBeInTheDocument()
    // The help reuses the tip's text: step 2 links to Agent Harness, no raw tag.
    const harness = screen.getByRole('link', { name: 'Settings → Agent Harness' })
    expect(harness.getAttribute('href')).toBe('/settings/agent')
    expect(document.body.textContent).not.toContain('settingsLink')
    expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
    expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
    // A probe error stays on the row, the setup line beside it.
    expect(screen.queryByTestId('mcp-probe-error') !== null).toBe(state.status === 'error')
  })

  it('keeps the probe error of a marked row, with the setup line only where a retry would be', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({
      status: 'error', error: 'HTTP 401', headers: { Authorization: '***' }, connectionNeedsCrewSignIn: true,
    })])
    renderTab()

    await waitFor(() => expect(screen.getByTestId('mcp-probe-error')).toBeInTheDocument())
    expect(screen.getByText(/HTTP 401/)).toBeInTheDocument()
    expect(screen.getByText(/static Authorization header cannot satisfy it/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open MCP sign-in settings' })).toBeNull()
  })

  it('keeps the tools and their toggles on a marked row that lists tools', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({
      status: 'ok', authGrantPresent: true, tools: ['search', 'post'], connectionNeedsCrewSignIn: true,
    })])
    renderTab()

    await waitFor(() => expect(screen.getByRole('button', { name: /2 tools/ })).toBeInTheDocument())
    expect(screen.queryByRole('link', { name: 'Open MCP sign-in settings' })).toBeNull()
  })

  describe.each([
    { url: 'https://MCP.SLACK.COM/mcp', inline: false },
    { url: 'https://mcp.slack.com/mcp/', inline: false },
    { url: 'https://mcp.slack.com/mcp', inline: true },
  ])('configured Slack URL $url', ({ url, inline }) => {
    it.each([
      { status: 'needs_auth', authGrantPresent: false },
      { status: 'error', error: 'OAuth grant rejected', authGrantPresent: false },
      { status: 'needs_auth', authGrantPresent: true },
    ])('offers only a usable sign-in path: %j', async (state) => {
      mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
      mockApi.mcpServers.mockResolvedValue([slackRow({ ...state, url })])
      renderTab()

      await screen.findByText('Managed by Connections')
      if (inline) {
        expect(screen.getByRole('button', { name: /Sign in/ })).toBeInTheDocument()
        expect(screen.queryByRole('link', { name: 'Open the Slack connection card' })).toBeNull()
      } else {
        expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
        const card = screen.getByRole('link', { name: 'Open the Slack connection card' })
        expect(card.getAttribute('href')).toBe('/settings/connections?highlight=connections.oauth-client-slack')
        expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
      }
      expect(screen.queryByTestId('mcp-probe-error') !== null).toBe(state.status === 'error')
    })
  })

  it('belongs to the card once the OAuth app is saved', async () => {
    mockApi.mcpServers.mockResolvedValue([slackRow({})])
    renderTab()

    await waitFor(() => expect(screen.getByText('Managed by Connections')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
  })
})
