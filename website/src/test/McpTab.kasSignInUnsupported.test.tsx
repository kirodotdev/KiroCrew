/**
 * Crew never sends a remote server's `oauth` block to KAS when it names a client
 * secret or keeps nothing Crew sends (`oauthWithheld`), so that server cannot
 * sign in on the KAS harness. Its row must say so, with its own badge, instead of
 * sending the user to chat for an approval that never comes or offering a Sign
 * in control. A block KAS does receive keeps the normal guidance.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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

const remote = (overrides: Partial<McpServer>): McpServer => ({
  name: 'team-chat',
  command: '',
  url: 'https://team-chat.example.com/mcp',
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
})

describe('McpTab: a sign-in the KAS harness cannot do', () => {
  it.each([
    ['not signed in, chat guidance', {}],
    ['not signed in, owner sign-in', { ownerSignIn: true }],
  ])('says sign-in is unavailable, with no step that cannot finish: %s', async (_label, extra) => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    mockApi.mcpServers.mockResolvedValue([remote({ oauthWithheld: true, ...extra })])
    renderTab()

    const line = await screen.findByTestId('mcp-sign-in-unsupported')
    expect(line).toHaveTextContent("This server can't sign in with the Kiro Agent engine yet.")
    expect(screen.getByText('Sign-in unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Sign-in required')).toBeNull()
    // A Kiro CLI pick runs on KAS where kiro-cli offers v3, so no harness link either.
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
  })

  it('says the same when a Kiro CLI sign-in is already held', async () => {
    // The grant is kiro-cli's; a KAS chat never uses it, so "Signed in" would mislead.
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    mockApi.mcpServers.mockResolvedValue([
      remote({ oauthWithheld: true, ownerSignIn: true, authGrantPresent: true }),
    ])
    renderTab()

    await waitFor(() => expect(screen.getByTestId('mcp-sign-in-unsupported')).toBeInTheDocument())
    expect(screen.getByText('Sign-in unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Signed in')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign in again' })).toBeNull()
  })

  it('keeps the Outdated tip, which is not a sign-in step', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    mockApi.mcpServers.mockResolvedValue([remote({ oauthWithheld: true, status: 'outdated', authChallenge: undefined })])
    renderTab()

    const badge = await screen.findByText('Outdated')
    expect(within(badge.parentElement!).getByRole('button', { name: 'More information' })).toBeInTheDocument()
  })

  it('replaces Sign in again on a failed row', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    mockApi.mcpServers.mockResolvedValue([
      remote({ oauthWithheld: true, ownerSignIn: true, status: 'error', error: 'HTTP 401' }),
    ])
    renderTab()

    await waitFor(() => expect(screen.getByTestId('mcp-sign-in-unsupported')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Sign in again' })).toBeNull()
    // The probe failure itself renders through the shared error notice.
    expect(screen.getByTestId('mcp-probe-error')).toHaveTextContent('HTTP 401')
  })

  it('keeps the chat guidance for a server without its own client', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    mockApi.mcpServers.mockResolvedValue([remote({})])
    renderTab()

    await waitFor(() => expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument())
    expect(screen.queryByTestId('mcp-sign-in-unsupported')).toBeNull()
    expect(screen.getByText('Sign-in required')).toBeInTheDocument()
  })

  it('keeps the chat guidance for a public client block KAS receives', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
    mockApi.mcpServers.mockResolvedValue([remote({ clientId: '123.456' })])
    renderTab()

    await waitFor(() => expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument())
    expect(screen.queryByTestId('mcp-sign-in-unsupported')).toBeNull()
  })

  it('keeps the chat guidance on the Kiro CLI harness', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    mockApi.acpBackends.mockResolvedValue({ backends: [], kiro_selection_runs_on_kas: false })
    mockApi.mcpServers.mockResolvedValue([remote({ oauthWithheld: true })])
    renderTab()

    await waitFor(() => expect(screen.getByRole('link', { name: /Go to chat/ })).toBeInTheDocument())
    expect(screen.queryByTestId('mcp-sign-in-unsupported')).toBeNull()
  })

  it('says sign-in is unavailable when a Kiro CLI pick runs on KAS', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    mockApi.acpBackends.mockResolvedValue({ backends: [], kiro_selection_runs_on_kas: true })
    mockApi.mcpServers.mockResolvedValue([remote({ oauthWithheld: true })])
    renderTab()

    await waitFor(() => expect(screen.getByTestId('mcp-sign-in-unsupported')).toBeInTheDocument())
    expect(screen.queryByRole('link', { name: /Go to chat/ })).toBeNull()
  })

  it('ignores a cached KAS probe once the config selects another harness', async () => {
    mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    mockApi.acpBackends.mockResolvedValue({ backends: [], kiro_selection_runs_on_kas: true })
    mockApi.mcpServers.mockResolvedValue([remote({ oauthWithheld: true })])
    renderTab()

    await waitFor(() => expect(mockApi.acpBackends).toHaveBeenCalled())
    await screen.findByText('Sign-in required')
    expect(screen.queryByTestId('mcp-sign-in-unsupported')).toBeNull()
    expect(screen.queryByText('Sign-in unavailable')).toBeNull()
  })
})
