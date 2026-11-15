// Render-level coverage of what a card says when the provider refuses a sign-in
// (the backend's closed `crew_oauth_callback_*` codes) and of the redirect
// address the status feed reports for an org admin. The api mock, the McpTab
// stub and the beforeEach are copied from ConnectionsPage.coverage.connect.test.tsx,
// whose header explains the seams.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

const mcpServers = vi.fn()
const mcpProbe = vi.fn()
const mcpApply = vi.fn()
const mcpCustomAdd = vi.fn()
const mcpCustomGet = vi.fn()
const mcpCustomUpdate = vi.fn()
const mcpOAuthRelay = vi.fn()
const connectionsMint = vi.fn()
const connectionsMintState = vi.fn()
const connectionsPremint = vi.fn()
const connectionsStatus = vi.fn()
const connectionsCancel = vi.fn()
const connectionsDisconnect = vi.fn()
const connectionsTest = vi.fn()

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    mcpServers: (...a: unknown[]) => mcpServers(...a),
    mcpProbe: (...a: unknown[]) => mcpProbe(...a),
    mcpApply: (...a: unknown[]) => mcpApply(...a),
    mcpCustomAdd: (...a: unknown[]) => mcpCustomAdd(...a),
    mcpCustomGet: (...a: unknown[]) => mcpCustomGet(...a),
    mcpCustomUpdate: (...a: unknown[]) => mcpCustomUpdate(...a),
    mcpOAuthRelay: (...a: unknown[]) => mcpOAuthRelay(...a),
    connectionsMint: (...a: unknown[]) => connectionsMint(...a),
    connectionsMintState: (...a: unknown[]) => connectionsMintState(...a),
    connectionsPremint: (...a: unknown[]) => connectionsPremint(...a),
    connectionsStatus: (...a: unknown[]) => connectionsStatus(...a),
    connectionsCancel: (...a: unknown[]) => connectionsCancel(...a),
    connectionsDisconnect: (...a: unknown[]) => connectionsDisconnect(...a),
    connectionsTest: (...a: unknown[]) => connectionsTest(...a),
  },
}))

vi.mock('../pages/overview/McpTab', () => ({
  default: () => null,
}))

import { mount, card } from './ConnectionsPage.coverageTestUtils'
import { CONNECTION_PROVIDERS } from '../pages/connections/registry'

const ATLASSIAN_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/connections.md#atlassian-organization-admin-checklist'

beforeEach(() => {
  mcpServers.mockReset().mockResolvedValue([])
  mcpProbe.mockReset().mockResolvedValue([])
  mcpApply.mockReset().mockResolvedValue({ ok: true })
  mcpCustomAdd.mockReset().mockResolvedValue({ ok: true, added: [], enabled: true })
  mcpCustomGet.mockReset().mockResolvedValue({ name: 'notion', spec: {}, enabled: true })
  mcpCustomUpdate.mockReset().mockResolvedValue({ ok: true, name: 'notion' })
  mcpOAuthRelay.mockReset().mockResolvedValue({ ok: true })
  connectionsMint.mockReset().mockImplementation(async (slug: string) => ({
    ok: true, slug, state: 'minting', token: 'tok1',
  }))
  connectionsMintState.mockReset().mockResolvedValue({ slug: 'notion', state: 'minting', token: 'tok1' })
  connectionsPremint.mockReset().mockResolvedValue({ ok: true, preminting: [] })
  connectionsStatus.mockReset().mockResolvedValue({ schema_version: 1, connections: [] })
  connectionsCancel.mockReset().mockResolvedValue({ ok: true, slug: 'notion', dropped: true })
  connectionsDisconnect.mockReset().mockResolvedValue({
    ok: true, grantRemoved: true, grantSurviving: [], entryRemoved: true, grantSharedWith: [],
  })
  connectionsTest.mockReset().mockResolvedValue({
    schema_version: 1, slug: 'notion', verdict: 'usable', code: 'tools_available', toolCount: 2,
  })
})

async function refuse(slug: string, reason: string) {
  connectionsMintState.mockResolvedValue({ slug, state: 'failed', reason, token: 'tok1' })
  mount()
  fireEvent.click(await waitFor(() => within(card(slug)).getByRole('button', { name: 'Connect' })))
  return waitFor(() => within(card(slug)).getByRole('alert'))
}

describe('a sign-in the provider refused', () => {
  it.each([
    ['crew_oauth_callback_access_denied', 'Notion refused the sign-in. Either it was declined on the consent page, or your organization\'s admin does not allow this connection.'],
    ['crew_oauth_callback_invalid_scope', 'Notion refused the permissions this sign-in asked for.'],
    ['crew_oauth_callback_unauthorized_client', 'Notion does not allow this app to sign in.'],
    ['crew_oauth_callback_invalid_request', 'Notion rejected the sign-in request. Try connecting again.'],
    ['crew_oauth_callback_unsupported_response_type', 'Notion does not support this kind of sign-in.'],
    ['crew_oauth_callback_server_error', 'Notion hit an error during sign-in. Try connecting again.'],
    ['crew_oauth_callback_temporarily_unavailable', 'Signing in to Notion is temporarily unavailable. Try again in a few minutes.'],
    ['crew_oauth_callback_refused', 'Notion did not complete the sign-in. Try connecting again.'],
  ])('explains %s without claiming the user declined', async (reason, expected) => {
    const alert = await refuse('notion', reason)
    await waitFor(() => expect(alert).toHaveTextContent(`Action failed: ${expected}`))
    // No org admin guide in Notion's registry entry, so no checklist link.
    expect(card('notion').querySelector('a[href*="connections.md#"]')).toBeNull()
  })

  it.each([
    'crew_oauth_callback_access_denied',
    'crew_oauth_callback_unauthorized_client',
    'crew_oauth_callback_invalid_scope',
  ])('links the org admin checklist on %s for a provider that names one', async reason => {
    await refuse('atlassian', reason)
    const link = await waitFor(() => {
      const found = card('atlassian').querySelector(`a[href="${ATLASSIAN_GUIDE}"]`)
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    expect(link).toHaveTextContent('Documentation')
  })

  it('names both causes of access_denied on the Atlassian card', async () => {
    const alert = await refuse('atlassian', 'crew_oauth_callback_access_denied')
    await waitFor(() => expect(alert).toHaveTextContent(
      'Atlassian refused the sign-in. Either it was declined on the consent page, or your organization\'s admin does not allow this connection.',
    ))
  })

  it('does not link the checklist for a refusal no admin policy causes', async () => {
    const alert = await refuse('atlassian', 'crew_oauth_callback_server_error')
    await waitFor(() => expect(alert).toHaveTextContent('Atlassian hit an error during sign-in.'))
    expect(card('atlassian').querySelector('a[href*="connections.md#"]')).toBeNull()
  })
})

describe('the redirect address an org admin allows', () => {
  function openTip(slug: string, name: string) {
    fireEvent.mouseEnter(within(card(slug)).getByRole('button', { name: `${name} prerequisites` }))
    return screen.getByRole('tooltip')
  }

  it('shows the exact redirect Crew registers, as selectable text', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{
        slug: 'atlassian', status: 'not_connected', grantPresent: false,
        signInRedirect: { exact: 'https://gw.example.com/api/connections/oauth/callback' },
      }],
    })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    await waitFor(() => {
      const tip = openTip('atlassian', 'Atlassian')
      expect(tip).toHaveTextContent('Redirect address for this sign-in')
      const value = within(tip).getByText('https://gw.example.com/api/connections/oauth/callback')
      expect(value.tagName).toBe('CODE')
      expect(value).toHaveClass('select-all')
    })
  })

  it('lists loopback origins on any port when the runtime picks the port', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{
        slug: 'atlassian', status: 'not_connected', grantPresent: false,
        signInRedirect: { anyPort: ['http://127.0.0.1', 'http://localhost'] },
      }],
    })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    await waitFor(() => {
      const tip = openTip('atlassian', 'Atlassian')
      expect(tip).toHaveTextContent('so allow these addresses on any port.')
      expect(within(tip).getByText('http://127.0.0.1').tagName).toBe('CODE')
      expect(within(tip).getByText('http://localhost').tagName).toBe('CODE')
    })
  })

  it('points the Atlassian prerequisite at the org admin checklist', async () => {
    const atlassian = CONNECTION_PROVIDERS.find(provider => provider.slug === 'atlassian')
    expect(atlassian?.org_admin_guide).toBe('connections.md#atlassian-organization-admin-checklist')
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    const tip = openTip('atlassian', 'Atlassian')
    expect(tip).toHaveTextContent('Read, Write and Search permissions')
    expect(tip).toHaveTextContent('Atlassian organization admin checklist')
    // No status row: nothing to show, rather than a guessed address.
    expect(within(tip).queryByText(/^http/)).toBeNull()
  })
})
