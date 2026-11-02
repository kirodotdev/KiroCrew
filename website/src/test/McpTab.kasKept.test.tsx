import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { McpServer } from '../types'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  mcpDiscover: vi.fn(),
  mcpProbe: vi.fn(),
  mcpApply: vi.fn(),
  mcpGlobalScopes: vi.fn(),
  mcpResetProbeFailures: vi.fn(),
  kirocrewConfig: vi.fn(),
  connectionsMint: vi.fn(),
  connectionsMintState: vi.fn(),
  connectionsMintServer: vi.fn(),
  connectionsMintServerState: vi.fn(),
  mcpOAuthRelay: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

/** A remote server the owner added, that needs a sign-in, with no kiro-cli grant. */
const docs: McpServer = {
  name: 'docs-internal',
  command: '',
  url: 'https://docs.example.com/mcp',
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  ownerSignIn: true,
  authGrantPresent: false,
}

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
  mockApi.mcpServers.mockResolvedValue([docs])
  // The fresh probe after the sign-in still reads no kiro-cli grant: the KAS
  // vault is invisible to it.
  mockApi.mcpProbe.mockResolvedValue([docs])
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({})
  mockApi.connectionsMintServer.mockResolvedValue({ ok: true, slug: 'docs-internal', state: 'minting', token: 't' })
})

describe('McpTab after a sign-in kept in the KAS vault', () => {
  it('shows one status: the badge reads Signed in beside the kept line', async () => {
    mockApi.connectionsMintServerState.mockResolvedValue({ slug: 'docs-internal', state: 'granted', store: 'kas' })
    renderTab()

    await waitFor(() => expect(screen.getByText('Sign-in required')).toBeInTheDocument())
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(screen.getByText(/New Kiro Agent chats use this sign-in/)).toBeInTheDocument(), { timeout: 5000 })
    expect(screen.getByText('Signed in')).toBeInTheDocument()
    expect(screen.queryByText('Sign-in required')).not.toBeInTheDocument()
  })

  it('keeps the probe badge when the sign-in did not go to the KAS vault', async () => {
    mockApi.connectionsMintServerState.mockResolvedValue({ slug: 'docs-internal', state: 'granted' })
    renderTab()

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(mockApi.mcpProbe).toHaveBeenCalled(), { timeout: 5000 })
    expect(screen.getByText('Sign-in required')).toBeInTheDocument()
  })
})
