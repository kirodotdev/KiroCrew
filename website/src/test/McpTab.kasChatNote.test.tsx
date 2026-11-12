/**
 * #24693: while chats run on KAS, the gateway stamps `kasChatSignIn` on each MCP
 * row whose sign-in a KAS chat does not use, and the row's sign-in control says so,
 * row by row. `chat`: a sign-in made here runs on kiro-cli, whose cache a KAS chat
 * never reads. `again`: a sign-in made here is saved for KAS chats, but the grant the
 * row holds is kiro-cli's only. A row without the key shows no line.
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
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({
  useProvider: () => ({ displayName: 'kiro', labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../components/McpBrowserModal', () => ({ default: () => null }))

import McpTab from '../pages/overview/McpTab'

const NOTE = "Chats on Kiro Agent don't use a sign-in made here. Sign in from the chat, with its Authorize card."
const AGAIN_NOTE = "Chats on Kiro Agent don't use this sign-in yet. Sign in again to save it for them."

const userAdded = (name: string, overrides: Partial<McpServer>): McpServer => ({
  name,
  command: '',
  url: `https://${name}.example.com/mcp`,
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  ownerSignIn: true,
  ...overrides,
})

const ROWS = [
  userAdded('docs-internal', { authGrantPresent: false }),
  userAdded('wiki-internal', { authGrantPresent: true }),
  userAdded('tickets-internal', { status: 'error', error: 'HTTP 401' }),
]

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
  mockApi.mcpServers.mockResolvedValue(ROWS)
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
})

const NOTE_ID = 'mcp-sign-in-kas-chat-note'
const notes = () => screen.queryAllByTestId(NOTE_ID)
const rowFor = (name: string) => {
  const row = screen.getAllByRole('row').find(r => r.textContent?.includes(name))
  if (!row) throw new Error(`no row for ${name}`)
  return row
}

describe('McpTab sign-in rows on the KAS harness (#24693)', () => {
  it('puts the line under every sign-in control whose sign-in runs on kiro-cli', async () => {
    mockApi.mcpServers.mockResolvedValue(ROWS.map(row => ({ ...row, kasChatSignIn: 'chat' as const })))
    renderTab()

    await waitFor(() => expect(notes()).toHaveLength(3))
    for (const note of notes()) expect(note).toHaveTextContent(NOTE)
    // The controls are unchanged.
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Sign in again' })).toHaveLength(2)
  })

  it('shows no line on a row the gateway gives no caveat: its sign-in reaches KAS chats', async () => {
    renderTab()

    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Sign in again' })).toHaveLength(2))
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(notes()).toHaveLength(0)
  })

  it('words each row by its own caveat', async () => {
    // A grant only kiro-cli's cache holds on a row that signs in on KAS, a row whose
    // entry keeps the kiro-cli mint, and a fresh row whose sign-in reaches KAS chats.
    mockApi.mcpServers.mockResolvedValue([
      userAdded('wiki-internal', { authGrantPresent: true, kasChatSignIn: 'again' }),
      userAdded('tickets-internal', { authGrantPresent: true, headers: { 'X-Tenant': 'a' }, kasChatSignIn: 'chat' }),
      userAdded('docs-internal', { authGrantPresent: false }),
    ])
    renderTab()

    await waitFor(() => expect(notes()).toHaveLength(2))
    expect(within(rowFor('wiki-internal')).getByTestId(NOTE_ID)).toHaveTextContent(AGAIN_NOTE)
    expect(within(rowFor('tickets-internal')).getByTestId(NOTE_ID)).toHaveTextContent(NOTE)
    // The fresh row offers Sign in with no line under it.
    expect(within(rowFor('docs-internal')).getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(within(rowFor('docs-internal')).queryByTestId(NOTE_ID)).toBeNull()
  })

  it('a Crew-held row reads signed in, with no Sign in again and no KAS line', async () => {
    // dashboard.feature_previews.crew_oauth on KAS: Crew holds the grant and serves KAS chats.
    mockApi.mcpServers.mockResolvedValue([
      userAdded('github', { authGrantPresent: true, authCrewHeld: true }),
    ])
    renderTab()

    await waitFor(() => expect(screen.getByText('Signed in')).toBeInTheDocument())
    expect(screen.queryByText('Sign-in required')).toBeNull()
    expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
    expect(notes()).toHaveLength(0)
  })

  it('a Crew-held row not yet signed in offers Sign in without the KAS line', async () => {
    mockApi.mcpServers.mockResolvedValue([
      userAdded('github', { authGrantPresent: false, authCrewHeld: true }),
    ])
    renderTab()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    expect(notes()).toHaveLength(0)
  })
})
