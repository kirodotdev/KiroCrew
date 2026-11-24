// The "Before you connect" tip of a card that still needs its OAuth app offers
// to create that app from the manifest the gateway generates, and to copy the
// manifest (RFC rfc-crew-oauth-sign-in-redesign.md section 2). The link and the
// text come from GET /api/connections/app-manifests; the card names no vendor.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, waitFor, within } from '@testing-library/react'

const mcpServers = vi.fn()
const mcpProbe = vi.fn()
const connectionsStatus = vi.fn()
const connectionsMintState = vi.fn()
const connectionsAppManifests = vi.fn()
const connectionsOAuthClients = vi.fn()

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
    connectionsStatus: (...a: unknown[]) => connectionsStatus(...a),
    connectionsMintState: (...a: unknown[]) => connectionsMintState(...a),
    connectionsPremint: () => Promise.resolve({ ok: true, preminting: [] }),
    connectionsAppManifests: (...a: unknown[]) => connectionsAppManifests(...a),
    connectionsOAuthClients: (...a: unknown[]) => connectionsOAuthClients(...a),
  },
}))

vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn(() => Promise.resolve(true)),
}))

// The OAuth app part a card opens is its own unit (OAuthAppPanel.test.tsx).
vi.mock('../pages/connections/OAuthAppPanel', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    OAuthAppPanel: () => <div />,
    OAuthDraftsContext: React.createContext<(settingSlug: string, dirty: boolean) => void>(() => {}),
    OAuthClientWrittenContext: React.createContext<(slugs: string[]) => void>(() => {}),
    OAuthDraftValuesContext: React.createContext({ get: (_key: string) => undefined, set: (_key: string, _v: unknown) => {} }),
  }
})

import type { QueryClient } from '@tanstack/react-query'
import { ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { mount, card } from './ConnectionsPage.coverageTestUtils'

// The page reads the manifests, and only once they hold an app reads the saved
// clients; the buttons render after both. Every wait behind that two-request
// chain gets this named ceiling instead of Testing Library's 1 s default.
const CHAIN_WAIT = { timeout: 5000 }
const CREATE_URL = 'https://api.slack.com/apps?new_app=1&manifest_yaml=display_information%3A'
const LOAD_FAILED = 'Could not load the buttons that create an app.'
const MANIFEST = 'display_information:\n  name: test-app\n'

function slackClient(configured: boolean) {
  return {
    slug: 'slack', confidential: true, redirect_uri: 'http://localhost:48106/callback',
    registration_guide: 'oauth-app-registration/slack.md', client_id: configured ? 'id' : null,
    client_id_source: configured ? 'config' : null, client_secret_set: configured,
    client_secret_source: configured ? 'vault' : null, configured,
  }
}

function slackStatus(flags: Record<string, boolean>) {
  connectionsStatus.mockResolvedValue({
    schema_version: 1,
    connections: [{ slug: 'slack', status: 'not_connected', grantPresent: false, ...flags }],
  })
}

/** `closed`: the card as the gallery shows it; opening a card that needs its
 *  client shows the OAuth app form in place of the tip. */
async function openSlackTip({ closed = false } = {}): Promise<HTMLElement> {
  const slack = await waitFor(() => {
    if (!closed) return card('slack')
    const el = document.getElementById('connection-slack')
    if (!el) throw new Error('no slack card')
    return el
  })
  await waitFor(() => expect(slack).toHaveAttribute('data-state', 'needs-configuration'))
  fireEvent.click(within(slack).getByRole('button', { name: 'Slack prerequisites' }))
  // A dialog when the tip carries controls, a tooltip when it is text only.
  return waitFor(() => {
    const tip = document.querySelector<HTMLElement>('[role="dialog"], [role="tooltip"]')
    if (!tip) throw new Error('no tip open')
    return tip
  })
}

beforeEach(() => {
  mcpServers.mockReset().mockResolvedValue([])
  mcpProbe.mockReset().mockResolvedValue([])
  connectionsMintState.mockReset().mockResolvedValue({})
  connectionsStatus.mockReset()
  connectionsAppManifests.mockReset().mockResolvedValue({
    schema_version: 1, apps: { slack: { create_url: CREATE_URL, manifest: MANIFEST } },
  })
  connectionsOAuthClients.mockReset().mockResolvedValue({ schema_version: 1, clients: [slackClient(false)] })
  ;(copyToClipboard as unknown as ReturnType<typeof vi.fn>).mockClear()
})

describe('Create the Slack app', () => {
  it('opens the gateway’s create link from the tip while Crew sign-in is off', async () => {
    slackStatus({ needsCrewSignIn: true })
    mount()

    const tip = await openSlackTip()
    const create = await within(tip).findByRole('link', { name: /Create the Slack app/ }, CHAIN_WAIT)
    expect(create).toHaveAttribute('href', CREATE_URL)
    expect(create).toHaveAttribute('target', '_blank')
    expect(create.getAttribute('rel')).toContain('noopener')
    // One numbered list: creating the app is step 1, and the last step says
    // where its Client ID and Client Secret go once sign-in is on.
    expect(tip.textContent).toContain(
      'Slack signs in through Kiro Crew. To connect it: (1) create the Slack app with the button below (Slack opens with it filled in), (2) turn on Kiro Crew sign-in with the button on this card, (3) in Settings, set chats to run on Kiro Agent, (4) back here, click Set up Slack and paste the app’s Client ID and Client Secret into its form.',
    )
    expect(tip.textContent).not.toContain('Start by creating')
    expect(within(tip).getByRole('link', { name: 'Settings' }).getAttribute('href')).toBe('/settings/agent')
  })

  it('leads the client-not-saved tip with the create step, naming the Set up button', async () => {
    slackStatus({ needsClientConfig: true })
    mount()

    const tip = await openSlackTip({ closed: true })
    await within(tip).findByRole('link', { name: /Create the Slack app/ }, CHAIN_WAIT)
    const hint = 'Start by creating the Slack app: Slack opens with it filled in. Then click Set up Slack on this card and paste the app’s Client ID and Client Secret into its form.'
    expect(tip.textContent).toContain(hint)
    // One set of instructions: the generic setup text is not repeated beside it.
    expect(tip.textContent).not.toContain('needs a one-time setup')
  })

  it('copies the same manifest text the link carries', async () => {
    slackStatus({ needsClientConfig: true })
    mount()

    const tip = await openSlackTip({ closed: true })
    fireEvent.click(await within(tip).findByRole('button', { name: 'Copy manifest' }, CHAIN_WAIT))
    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(MANIFEST), CHAIN_WAIT)
    expect(await within(tip).findByText('Copied', undefined, CHAIN_WAIT)).toBeInTheDocument()
  })

  it('offers no second app once the card’s client is saved', async () => {
    connectionsOAuthClients.mockResolvedValue({ schema_version: 1, clients: [slackClient(true)] })
    slackStatus({ needsCrewSignIn: true })
    mount()

    const tip = await openSlackTip()
    await waitFor(() => expect(connectionsOAuthClients).toHaveBeenCalled(), CHAIN_WAIT)
    expect(within(tip).queryByRole('link', { name: /Create the Slack app/ })).not.toBeInTheDocument()
    expect(within(tip).queryByRole('button', { name: 'Copy manifest' })).not.toBeInTheDocument()
  })

  it('shows the tip without the buttons, and no notice, when the owner gate refuses', async () => {
    connectionsAppManifests.mockRejectedValue(new ApiError(403, 'owner_only'))
    slackStatus({ needsCrewSignIn: true })
    mount()

    const tip = await openSlackTip()
    await waitFor(() => expect(connectionsAppManifests).toHaveBeenCalled(), CHAIN_WAIT)
    expect(within(tip).queryByRole('link', { name: /Create the Slack app/ })).not.toBeInTheDocument()
    expect(connectionsOAuthClients).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toContain(LOAD_FAILED)
  })

  it('says so when the manifests fail to load for another reason', async () => {
    connectionsAppManifests.mockRejectedValue(new ApiError(500, 'manifest template missing'))
    slackStatus({ needsCrewSignIn: true })
    mount()

    await waitFor(() => expect(document.body.textContent).toContain(LOAD_FAILED), CHAIN_WAIT)
  })

  it('says so when the saved-client lookup that gates the buttons fails', async () => {
    connectionsOAuthClients.mockRejectedValue(new ApiError(500, 'vault unreadable'))
    slackStatus({ needsCrewSignIn: true })
    mount()

    await waitFor(() => expect(document.body.textContent).toContain(LOAD_FAILED), CHAIN_WAIT)
  })

  it('withdraws the cached buttons when a refetch of the manifests fails', async () => {
    slackStatus({ needsCrewSignIn: true })
    const { queryClient }: { queryClient: QueryClient } = mount()
    const tip = await openSlackTip()
    await within(tip).findByRole('link', { name: /Create the Slack app/ }, CHAIN_WAIT)

    connectionsAppManifests.mockRejectedValue(new ApiError(500, 'manifest template missing'))
    await queryClient.invalidateQueries({ queryKey: ['slack-manifest'] })
    await waitFor(() => expect(document.body.textContent).toContain(LOAD_FAILED), CHAIN_WAIT)
    expect(document.querySelector('a[href^="https://api.slack.com/apps"]')).toBeNull()
  })

  it('reads a malformed saved-client answer as no answer instead of crashing', async () => {
    connectionsOAuthClients.mockResolvedValue([])
    slackStatus({ needsCrewSignIn: true })
    mount()

    const tip = await openSlackTip()
    await waitFor(() => expect(connectionsOAuthClients).toHaveBeenCalled(), CHAIN_WAIT)
    expect(within(tip).queryByRole('link', { name: /Create the Slack app/ })).not.toBeInTheDocument()
  })

  it('refreshes the manifests when a Slack manifest save invalidates them', async () => {
    slackStatus({ needsCrewSignIn: true })
    const { queryClient }: { queryClient: QueryClient } = mount()
    await waitFor(() => expect(connectionsAppManifests).toHaveBeenCalledTimes(1), CHAIN_WAIT)

    await queryClient.invalidateQueries({ queryKey: ['slack-manifest'] })
    await waitFor(() => expect(connectionsAppManifests).toHaveBeenCalledTimes(2), CHAIN_WAIT)
  })

  it('never offers a create link that is not https', async () => {
    connectionsAppManifests.mockResolvedValue({
      schema_version: 1, apps: { slack: { create_url: 'javascript:alert(1)', manifest: MANIFEST } },
    })
    slackStatus({ needsCrewSignIn: true })
    mount()

    const tip = await openSlackTip()
    await within(tip).findByRole('button', { name: 'Copy manifest' }, CHAIN_WAIT)
    expect(within(tip).queryByRole('link', { name: /Create the Slack app/ })).not.toBeInTheDocument()
  })
})
