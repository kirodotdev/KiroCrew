// Render-level coverage of what a card says when the provider refuses a sign-in
// (the backend's closed `crew_oauth_callback_*` codes) and of the redirect
// address the status feed reports for an org admin. The api mock, the McpTab
// stub and the beforeEach are copied from ConnectionsPage.coverage.connect.test.tsx,
// whose header explains the seams.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'

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

const copyToClipboard = vi.fn()
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: (...a: unknown[]) => copyToClipboard(...a),
}))

vi.mock('../pages/overview/McpTab', () => ({
  default: () => null,
}))

import { mount, card } from './ConnectionsPage.coverageTestUtils'
import { CONNECTION_PROVIDERS } from '../pages/connections/registry'

// One test drives the hover-off grace on fake timers; every test ends on real ones.
afterEach(() => {
  vi.useRealTimers()
})

const ATLASSIAN_GUIDE =
  'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/connections.md#atlassian-organization-admin-checklist'

beforeEach(() => {
  copyToClipboard.mockReset().mockResolvedValue(true)
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

const GENERIC = 'Notion did not complete the sign-in. Try connecting again.'

describe('a sign-in the provider refused', () => {
  it.each([
    ['crew_oauth_callback_access_denied', 'Notion refused the sign-in. Either it was declined on Notion\'s consent page, or your organization\'s admin does not allow this connection.'],
    ['crew_oauth_callback_invalid_scope', 'Notion refused the permissions this sign-in asked for.'],
    ['crew_oauth_callback_unauthorized_client', 'Notion does not allow this app to sign in.'],
    // No org admin policy causes these, so they share the one generic line.
    ['crew_oauth_callback_invalid_request', GENERIC],
    ['crew_oauth_callback_unsupported_response_type', GENERIC],
    ['crew_oauth_callback_server_error', GENERIC],
    ['crew_oauth_callback_temporarily_unavailable', GENERIC],
    ['crew_oauth_callback_refused', GENERIC],
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
      'Atlassian refused the sign-in. Either it was declined on Atlassian\'s consent page, or your organization\'s admin does not allow this connection.',
    ))
  })

  it('does not link the checklist for a refusal no admin policy causes', async () => {
    const alert = await refuse('atlassian', 'crew_oauth_callback_server_error')
    await waitFor(() => expect(alert).toHaveTextContent('Atlassian did not complete the sign-in.'))
    expect(card('atlassian').querySelector('a[href*="connections.md#"]')).toBeNull()
  })
})

describe('the redirect address an org admin allows', () => {
  const icon = () => within(card('atlassian')).getByRole('button', { name: 'Atlassian prerequisites' })
  function openTip() {
    fireEvent.mouseEnter(icon())
    return screen.getByRole('dialog', { name: 'Before you connect' })
  }
  function status(signInRedirect: object) {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'atlassian', status: 'not_connected', grantPresent: false, signInRedirect }],
    })
  }

  it('shows the exact redirect Crew registers, with a control that copies all of it', async () => {
    const exact = 'https://gw.example.com/api/connections/oauth/callback'
    status({ exact })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    const tip = await waitFor(() => {
      const t = openTip()
      expect(t).toHaveTextContent('Redirect address for this sign-in')
      return t
    })
    expect(within(tip).getByText(exact).tagName).toBe('CODE')
    const copy = within(tip).getByRole('button', { name: `Copy redirect address ${exact}` })
    await act(async () => { fireEvent.click(copy) })
    expect(copyToClipboard).toHaveBeenCalledWith(exact)
    // A visible text confirmation, in a polite live region, with no animation class.
    const done = within(copy).getByText('Copied')
    expect(done).toHaveAttribute('aria-live', 'polite')
    expect(copy.innerHTML).not.toMatch(/animate-/)
  })

  it('gives each loopback origin its own copy control when the runtime picks the port', async () => {
    status({ anyPort: ['http://127.0.0.1', 'http://localhost'] })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    const tip = await waitFor(() => {
      const t = openTip()
      expect(t).toHaveTextContent('so allow these redirect addresses on any port.')
      return t
    })
    await act(async () => {
      fireEvent.click(within(tip).getByRole('button', { name: 'Copy redirect address http://localhost' }))
    })
    expect(copyToClipboard).toHaveBeenCalledTimes(1)
    expect(copyToClipboard).toHaveBeenCalledWith('http://localhost')
    expect(within(tip).getByRole('button', { name: 'Copy redirect address http://127.0.0.1' })).toBeInTheDocument()
  })

  it('links the org admin checklist from the tip, in a new tab', async () => {
    const atlassian = CONNECTION_PROVIDERS.find(provider => provider.slug === 'atlassian')
    expect(atlassian?.org_admin_guide).toBe('connections.md#atlassian-organization-admin-checklist')
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    const tip = openTip()
    expect(tip).toHaveTextContent("Atlassian's consent page")
    expect(tip).toHaveTextContent("this sign-in's redirect address and the Read, Write and Search permissions")
    const link = within(tip).getByRole('link', { name: /Organization admin checklist/ })
    expect(link).toHaveAttribute('href', ATLASSIAN_GUIDE)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // No status row: nothing to show, rather than a guessed address.
    expect(within(tip).queryByText(/^http/)).toBeNull()
  })

  it('says a refused copy failed instead of staying silent, with no agent hand-off', async () => {
    copyToClipboard.mockResolvedValue(false)
    status({ exact: 'https://gw.example.com/api/connections/oauth/callback' })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    const tip = await waitFor(() => {
      const t = openTip()
      expect(within(t).queryByRole('button', { name: /Copy redirect address/ })).not.toBeNull()
      return t
    })
    await act(async () => {
      fireEvent.click(within(tip).getByRole('button', { name: /Copy redirect address/ }))
    })
    expect(within(tip).getByRole('alert')).toHaveTextContent('Copy failed. Select the redirect address and copy it yourself.')
    expect(within(tip).queryByText('Copied')).toBeNull()
    expect(within(tip).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    // Dismissed, focus returns to the copy button; a quick retry that fails
    // again shows the notice again.
    const dismiss = within(tip).getByRole('button', { name: /Dismiss/ })
    dismiss.focus()
    expect(document.activeElement).toBe(dismiss)
    fireEvent.click(dismiss)
    expect(within(tip).queryByRole('alert')).toBeNull()
    expect(document.activeElement).toBe(within(tip).getByRole('button', { name: /Copy redirect address/ }))
    await act(async () => {
      fireEvent.click(within(tip).getByRole('button', { name: /Copy redirect address/ }))
    })
    expect(within(tip).getByRole('alert')).toHaveTextContent('Copy failed.')
  })

  it('stays open while one of its controls holds focus after the pointer leaves', async () => {
    status({ exact: 'https://gw.example.com/api/connections/oauth/callback' })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    const tip = await waitFor(() => {
      const t = openTip()
      expect(within(t).queryByRole('button', { name: /Copy redirect address/ })).not.toBeNull()
      return t
    })
    const copy = within(tip).getByRole('button', { name: /Copy redirect address/ })
    copy.focus()
    vi.useFakeTimers()
    fireEvent.mouseLeave(tip)
    fireEvent.mouseLeave(icon())
    // Past the component's hover-off grace.
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(document.activeElement).toBe(copy)
  })

  it('reaches the tip controls from the keyboard and returns to the icon', async () => {
    status({ exact: 'https://gw.example.com/api/connections/oauth/callback' })
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(within(card('atlassian')).getByRole('button', { name: 'Atlassian prerequisites' })).toBeInTheDocument())
    await waitFor(() => {
      fireEvent.focus(icon())
      expect(within(screen.getByRole('dialog')).queryByRole('button', { name: /Copy redirect address/ })).not.toBeNull()
    })
    fireEvent.keyDown(icon(), { key: 'Tab' })
    const tip = screen.getByRole('dialog')
    const copy = within(tip).getByRole('button', { name: /Copy redirect address/ })
    expect(document.activeElement).toBe(copy)
    fireEvent.keyDown(copy, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(icon())
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps a plain caveat an uncapped tooltip with nothing to operate', async () => {
    mount()
    const gitlab = await waitFor(() => within(card('gitlab')).getByRole('button', { name: 'GitLab prerequisites' }))
    vi.spyOn(gitlab, 'getBoundingClientRect').mockReturnValue({
      top: 150, bottom: 174, left: 400, right: 424, width: 24, height: 24, x: 400, y: 150, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.mouseEnter(gitlab)
    const tip = screen.getByRole('tooltip')
    expect(screen.queryByRole('dialog')).toBeNull()
    // No focus stop inside to scroll with, so no cap that could hide its text.
    expect(tip.style.maxHeight).toBe('')
    expect(tip).not.toHaveClass('overflow-y-auto')
  })

  it('caps the tip to the room above its icon and scrolls inside it', async () => {
    mount()
    await waitFor(() => within(card('atlassian')).getByRole('button', { name: 'Connect' }))
    vi.spyOn(icon(), 'getBoundingClientRect').mockReturnValue({
      top: 150, bottom: 174, left: 400, right: 424, width: 24, height: 24, x: 400, y: 150, toJSON: () => ({}),
    } as DOMRect)
    const tip = openTip()
    expect(tip.style.maxHeight).toBe('136px')
    expect(tip).toHaveClass('overflow-y-auto')
    // Scrolling inside the capped tip does not dismiss it.
    fireEvent.scroll(tip)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
