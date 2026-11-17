/**
 * The MCP Servers row of a remote OAuth server whose saved sign-in stopped
 * working. The page's probe carries no token, so such a server answers it with
 * its OAuth challenge and the row reads `needs_auth` with a held grant: the same
 * shape as a working sign-in. The chat's "waiting for a sign-in" notice links
 * here, so the row has to offer a way in:
 *
 * - a server the owner added from this page (`ownerSignIn`) offers Sign in again;
 * - a server added any other way, which the gateway does not sign in to from
 *   here, says in words how to sign in.
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

/** What the gateway's probe reports for a remote OAuth server it holds a saved
 *  sign-in for: the tokenless probe is answered with the server's challenge. */
const heldGrant = (name: string, overrides: Partial<McpServer>): McpServer => ({
  name,
  command: '',
  url: `https://${name}.example.com/mcp`,
  status: 'needs_auth',
  source: 'mcp.json',
  enabled: true,
  tools: [],
  authChallenge: true,
  authGrantPresent: true,
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

const rowFor = (name: string) => {
  const row = screen.getAllByRole('row').find(r => r.textContent?.includes(name))
  if (!row) throw new Error(`no row for ${name}`)
  return row
}

const HINT_ID = 'mcp-signed-in-no-row-sign-in'

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.mcpGlobalScopes.mockResolvedValue({ scopes: [] })
  mockApi.kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kas' } })
})

describe('McpTab row of a server with a saved sign-in', () => {
  it('offers Sign in again on a server the owner added from this page', async () => {
    mockApi.mcpServers.mockResolvedValue([heldGrant('notes-internal', { ownerSignIn: true })])
    renderTab()
    await waitFor(() => expect(rowFor('notes-internal')).toBeTruthy())
    const row = within(rowFor('notes-internal'))
    expect(row.getByRole('button', { name: 'Sign in again' })).toBeEnabled()
    expect(row.queryByTestId(HINT_ID)).toBeNull()
  })

  it('says how to sign in on a server this page cannot sign in to', async () => {
    mockApi.mcpServers.mockResolvedValue([heldGrant('notes-internal', { ownerSignIn: false })])
    renderTab()
    await waitFor(() => expect(rowFor('notes-internal')).toBeTruthy())
    const row = within(rowFor('notes-internal'))
    expect(row.queryByRole('button', { name: 'Sign in again' })).toBeNull()
    const hint = row.getByTestId(HINT_ID)
    // The probe carries no token, so a working server shows no tools here too:
    // the line opens with Signed in, and the chat sign-in is the step after a
    // failed tool call, never a reinstall.
    expect(hint.textContent).toMatch(/^Signed in\. This page can't list this server's tools; calling one in chat is the real check\./)
    expect(hint.textContent).toContain('If a tool fails, send a chat message that names notes-internal, such as "Sign in to notes-internal".')
    expect(hint.textContent).not.toMatch(/uninstall|Add Custom|stopped working/i)
    // Catalog copy, never the key.
    expect(hint.textContent).not.toMatch(/signed_in_no_row_sign_in/)
  })

  it('keeps the tools list, not the hint, on a server whose tools the page can list', async () => {
    mockApi.mcpServers.mockResolvedValue([
      heldGrant('notes-internal', { ownerSignIn: false, status: 'ok', tools: [{ name: 'search', description: '' }] as McpServer['tools'] }),
    ])
    renderTab()
    await waitFor(() => expect(rowFor('notes-internal')).toBeTruthy())
    expect(within(rowFor('notes-internal')).queryByTestId(HINT_ID)).toBeNull()
  })
})
