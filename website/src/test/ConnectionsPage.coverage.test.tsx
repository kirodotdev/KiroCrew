// First render-level coverage for the Connections page — the provider gallery
// shell that owns the four card states (not-connected → waiting-for-approval →
// connected / needs-attention), the links in, the Google entry, and every write action a
// card can fire (connect, cancel, reconnect, disconnect, test, OAuth relay).
//
// Two things shape this file:
//
//   1. The gallery is GATED. `servicesEnabled` carries the `connections_ui`
//      escape hatch: the shipped default is on, but the component's own parameter
//      default is false, so a test that wants the closed panel passes
//      `servicesEnabled: false` — that flag is the module's own seam, not a hack.
//   2. The page's only outside seams are `api` (mocked here — nothing dials the
//      network) and the OAuth app part a card opens, stubbed at its module
//      boundary because it has its own tests.
//
// Card state comes from real data, not from prop drilling: the server list is
// what `api.mcpServers` returns, and the OAuth banners are real `mcp_oauth`
// messages preloaded into the Redux chat slice — the same shape the gateway
// broadcasts. Interactions use `fireEvent` (no fake timers anywhere, so no
// clock to keep in sync) and every assertion waits on rendered output.
//
// The connect flow (installing a provider, the mint and its approval link,
// waiting for approval, the loopback relay, the authorization status feed and
// the approval tab) is in ConnectionsPage.coverage.connect.test.tsx; the
// fixtures both files read are in ConnectionsPage.coverageTestUtils.tsx.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'

import type { McpServer } from '../types'

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
  // A real-shaped class, not a stub: `testConnection`'s 409 handling narrows
  // with `e instanceof ApiError` before it may read `.status`/`.body`, so a
  // bare object would make every rejection fall through to the generic
  // action_failed message and the single-flight branch would never run.
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

// The OAuth app part a card opens is its own unit (OAuthAppPanel.test.tsx).
vi.mock('../pages/connections/OAuthAppPanel', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  const OAuthDraftsContext = React.createContext<(settingSlug: string, dirty: boolean) => void>(() => {})
  const OAuthClientWrittenContext = React.createContext<(slugs: string[]) => void>(() => {})
  const OAuthDraftValuesContext = React.createContext({ get: (_key: string) => undefined, set: (_key: string, _v: unknown) => {} })
  // A stand-in form whose "type" button marks it as holding unsaved input, and
  // whose "save" button reports the client written, as a real Save does.
  const OAuthAppPanel = ({ settingSlug, slugs }: { settingSlug: string; slugs: string[] }) => {
    const report = React.useContext(OAuthDraftsContext)
    const written = React.useContext(OAuthClientWrittenContext)
    return (
      <div data-testid={`oauth-app-${settingSlug}`} data-slugs={slugs.join(',')}>
        <button type="button" onClick={() => report(settingSlug, true)}>{`type into ${settingSlug}`}</button>
        <button type="button" onClick={() => written(slugs)}>{`save ${settingSlug}`}</button>
      </div>
    )
  }
  return { OAuthAppPanel, OAuthDraftsContext, OAuthClientWrittenContext, OAuthDraftValuesContext }
})

import ConnectionsPage, { errorIndicatesProviderRejection } from '../pages/connections/ConnectionsPage'
import { CONNECTION_PROVIDERS } from '../pages/connections/registry'
import { isGoogleSlug } from '../pages/connections/connectionsLinks'
import ProviderLogo, { PROVIDER_LOGO_SLUGS } from '../pages/connections/ProviderLogo'
import { i18next } from '../i18n'
import { ApiError } from '../api/client'
import {
  NOTION_URL,
  server,
  banner,
  mount,
  card,
  openAllCards,
  deferred,
} from './ConnectionsPage.coverageTestUtils'

const STRIPE_URL = 'https://mcp.stripe.com'

/** An element read as rendered, without opening it; throws until it exists. */
const byId = (id: string, attrs = ''): HTMLElement => {
  const el = document.querySelector<HTMLElement>(`#${id}${attrs}`)
  if (!el) throw new Error(`no #${id}${attrs}`)
  return el
}

const cards = (): HTMLElement[] => {
  openAllCards()
  // A search hides cards rather than unmounting them: count the shown ones.
  return Array.from(document.querySelectorAll<HTMLElement>('article[data-state]')).filter(el => !el.closest('[hidden]'))
}

beforeEach(() => {
  mcpServers.mockReset().mockResolvedValue([])
  mcpProbe.mockReset().mockResolvedValue([])
  mcpApply.mockReset().mockResolvedValue({ ok: true })
  mcpCustomAdd.mockReset().mockResolvedValue({ ok: true, added: [], enabled: true })
  // Stored spec deliberately carries OAuth hints, so the reconnect assertions
  // pin that a url rewrite round-trips them instead of clearing them.
  mcpCustomGet.mockReset().mockResolvedValue({
    name: 'notion',
    spec: { url: 'https://old.example/mcp', scopes: ['read'], clientId: 'client-1' },
    enabled: true,
  })
  mcpCustomUpdate.mockReset().mockResolvedValue({ ok: true, name: 'notion' })
  mcpOAuthRelay.mockReset().mockResolvedValue({ ok: true })
  connectionsMint.mockReset().mockResolvedValue({
    ok: true, slug: 'notion', state: 'minting', token: 'tok1',
  })
  connectionsMintState.mockReset().mockResolvedValue({
    slug: 'notion', state: 'minting', token: 'tok1',
  })
  connectionsPremint.mockReset().mockResolvedValue({ ok: true, preminting: ['notion'] })
  // Authorization axis: empty by default, so the reachability-derived card states
  // these tests assert on are unchanged by the status feed.
  connectionsStatus.mockReset().mockResolvedValue({ schema_version: 1, connections: [] })
  connectionsCancel.mockReset().mockResolvedValue({ ok: true, slug: 'notion', dropped: true })
  connectionsDisconnect.mockReset().mockResolvedValue({
    ok: true,
    grantRemoved: true,
    grantSurviving: [],
    entryRemoved: true,
    grantSharedWith: [],
  })
  connectionsTest.mockReset().mockResolvedValue({
    schema_version: 1,
    slug: 'notion',
    verdict: 'usable',
    code: 'tools_available',
    toolCount: 2,
  })
})

describe('the opted-out gallery', () => {
  it('offers no provider, no search and no way to connect when services are disabled', async () => {
    mount({ servicesEnabled: false })

    // Only the OFFER is withheld: the OAuth app parts stay, and with no search
    // box there is no empty-search notice either.
    await waitFor(() => expect(screen.getByTestId('connections-oauth-apps-only')).toBeInTheDocument())
    await waitFor(() => expect(mcpServers).toHaveBeenCalled())
    expect(screen.queryByText('No services match this search.')).not.toBeInTheDocument()
    expect(cards()).toHaveLength(0)
    expect(screen.queryByLabelText('Search services')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect' })).not.toBeInTheDocument()
  })
})

// Warming the approval URLs ahead of any click. The engine (POST
// /api/connections/premint) shipped with zero callers; this is the caller, and
// what these tests pin is the shape of the call rather than its result — the
// response is never a verdict, so nothing about the rendered gallery may depend
// on it.
describe('premint on mount', () => {
  it('warms the mintable providers once, with no body, when the gallery mounts', async () => {
    mount()

    await waitFor(() => expect(connectionsPremint).toHaveBeenCalledTimes(1))
    // Bodyless by contract: what is mintable is the server's to decide, so the
    // caller passes nothing — no source, no provider list, no telemetry.
    expect(connectionsPremint).toHaveBeenCalledWith()
  })

  it('warms once under StrictMode, where the mount effect is double-invoked', async () => {
    // The real double-fire hazard: an in-flight warm is not cancellable, so a
    // teardown flag cannot un-spawn the first activation and only a latching ref
    // holds. Asserting "once" on a plain mount proves nothing about the guard —
    // that render invokes the effect a single time either way.
    mount({ strict: true })

    await waitFor(() => expect(connectionsPremint).toHaveBeenCalledTimes(1))
  })

  it('does not warm while the services flag is off', async () => {
    mount({ servicesEnabled: false })

    // The gate offers no card and no Connect button, so there is nothing a warm
    // URL could serve — and warming spawns a process.
    await waitFor(() => expect(mcpServers).toHaveBeenCalled())
    expect(connectionsPremint).not.toHaveBeenCalled()
  })

  it('warms when the flag arrives after the first render, still only once', async () => {
    // The flag rides the config query, so the page's first render is ALWAYS
    // gated-off. A mount-only effect would either warm every install that never
    // opted in, or never warm at all — this pins the keyed-on-the-flag shape.
    const { rerender } = mount({ servicesEnabled: false })
    expect(connectionsPremint).not.toHaveBeenCalled()

    rerender(<ConnectionsPage servicesEnabled />)
    await waitFor(() => expect(connectionsPremint).toHaveBeenCalledTimes(1))

    rerender(<ConnectionsPage servicesEnabled />)
    expect(connectionsPremint).toHaveBeenCalledTimes(1)
  })

  it('renders the gallery unchanged when the warm request rejects', async () => {
    // A non-owner session is denied by design and the gateway may be older than
    // the endpoint; either way the cold mint on Connect is the fallback, so the
    // rejection must reach neither the cards nor an alert.
    connectionsPremint.mockRejectedValue(new Error('403 forbidden'))

    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    expect(within(card('notion')).getByRole('button', { name: 'Connect' })).toBeEnabled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('the provider gallery', () => {
  it('renders one card per visible provider and withholds the rest', async () => {
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    expect(screen.getByRole('heading', { name: 'Notion' })).toBeInTheDocument()
    // GitHub has not passed the launch gate but is PRE-REGISTERED, so its card
    // ships as the operator's instruction; Superhuman Mail is gated off with
    // no exception and stays hidden.
    expect(screen.getByRole('heading', { name: 'GitHub' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Superhuman Mail' })).not.toBeInTheDocument()
    // The six Google products count as their one entry.
    expect(screen.getByText(`${CONNECTION_PROVIDERS.length - 5} available`)).toBeInTheDocument()
  })

  it('keeps feedback slots out of the compact gallery until feedback exists', async () => {
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    expect(document.querySelectorAll('[data-slot="connection-feedback"]')).toHaveLength(0)
  })

  it('reserves a feedback slot on every displayed card when one card has feedback', async () => {
    mcpServers.mockResolvedValue([server()])
    mcpProbe.mockResolvedValue([server()])
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Test' }))
    await screen.findByText('Connection is healthy.')

    const slots = document.querySelectorAll('[data-slot="connection-feedback"]')
    expect(slots).toHaveLength(CONNECTION_PROVIDERS.length)
    expect(Array.from(slots).filter(slot => slot.querySelector('[role="status"]'))).toHaveLength(1)
  })

  it('shows an unconnected provider its docs link and a Connect button', async () => {
    mount()

    const notion = await waitFor(() => card('notion'))
    expect(notion).toHaveAttribute('data-state', 'not-connected')
    expect(within(notion).getByText('Search your Notion workspace and read pages and databases.')).toBeInTheDocument()
    expect(within(notion).getByRole('link', { name: /Documentation/ })).toHaveAttribute('target', '_blank')
    expect(within(notion).getByRole('button', { name: 'Connect' })).toBeEnabled()
  })

  // A pre-registered provider whose operator has not entered an OAuth client.
  // The status feed says so (`needsClientConfig`), and the card becomes an
  // instruction with a route to Settings → OAuth Apps -- there is no Connect
  // button, because the mint would only fail at the vendor.
  it('renders a pre-registered provider without a client as needs-configuration, routing to Settings', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'not_connected', grantPresent: false, needsClientConfig: true }],
    })
    mount()

    // Read without card(): this card must start closed, its Configure showing.
    const github = await waitFor(() => byId('connection-github', '[data-state="needs-configuration"]'))
    expect(within(github).getByText('Needs configuration')).toBeInTheDocument()
    expect(within(github).queryByRole('button', { name: /Connect/ })).not.toBeInTheDocument()
    expect(within(github).getByRole('link', { name: /Documentation/ })).toHaveAttribute('target', '_blank')

    // The missing step opens in place on this card, not on another page.
    const configure = within(github).getByRole('button', { name: 'Set up GitHub' })
    expect(configure).toHaveAttribute('aria-expanded', 'false')
    expect(within(github).getByTestId('oauth-app-github')).not.toBeVisible()
    fireEvent.click(configure)
    // Open, the form is the next step: Configure goes, the name toggle closes.
    expect(within(github).queryByRole('button', { name: 'Set up GitHub' })).toBeNull()
    expect(within(github).getByTestId('oauth-app-github')).toBeVisible()
  })

  it('keeps the one-time-setup explanation behind the warning tip beside Configure, not in a band', async () => {
    // The card's ONE surface for a pre-action caveat is the amber triangle
    // beside the action (the same PrerequisiteTip GitLab and Atlassian carry
    // beside Connect). Pinned so the explanation cannot render as an
    // always-visible band above the action row -- a row of chrome every grid
    // card in this state would pay for prose a user reads once.
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'not_connected', grantPresent: false, needsClientConfig: true }],
    })
    mount()

    const github = await waitFor(() => byId('connection-github', '[data-state="needs-configuration"]'))
    const setup = /GitHub needs a one-time setup\. Set up GitHub opens it on this card/

    // Nothing but the badge, the value prop, Documentation and the action row
    // renders inline: the explanation is not in the document until asked for.
    expect(screen.queryByText(setup)).not.toBeInTheDocument()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    // The tip sits in the action row, immediately before the Configure link.
    const icon = within(github).getByRole('button', { name: 'GitHub prerequisites' })
    const configure = within(github).getByRole('button', { name: 'Set up GitHub' })
    expect(icon.parentElement).toBe(configure.parentElement)
    expect(icon.compareDocumentPosition(configure) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    // Hover previews the bubble with the same heading the Connect-side caveats
    // use, carrying the full setup explanation; an outside press dismisses it.
    fireEvent.mouseEnter(icon)
    const bubble = screen.getByRole('tooltip')
    expect(within(bubble).getByText('Before you connect')).toBeInTheDocument()
    expect(within(bubble).getByText(setup)).toBeInTheDocument()
    expect(bubble).toHaveTextContent('client ID and secret')
    fireEvent.click(icon)
    expect(icon).toHaveAttribute('aria-expanded', 'true')
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  // A pre-registered provider while chats run on a backend that drops the
  // `oauth` block (`backendLacksOauth`). Connect stays, because the sign-in runs
  // on Kiro CLI; the caveat rides the tip beside it and never claims a failure.
  it('keeps Connect for a pre-registered provider on KAS and puts the caveat in its tip', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'not_connected', grantPresent: false, backendLacksOauth: true }],
    })
    mount()

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'not-connected'))
    expect(within(github).getByRole('button', { name: /Connect/ })).toBeInTheDocument()

    fireEvent.mouseEnter(within(github).getByRole('button', { name: 'GitHub prerequisites' }))
    const bubble = screen.getByRole('tooltip')
    expect(within(bubble).getByText(/Connect still finishes, and the GitHub sign-in is saved for chats that support it/)).toBeInTheDocument()
    expect(bubble).toHaveTextContent('its tools may not work there')
  })

  // The grant is on disk but Kiro Crew has no entry for the provider (it lives
  // only in a user's own agent spec), so `/api/mcp` lists no server.
  it('says why a provider with a stored sign-in but no server stays not connected', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'linear', status: 'connected', reason: 'grant_present', grantPresent: true }],
    })
    mount()

    const linear = await waitFor(() => card('linear'))
    await waitFor(() => expect(within(linear).getByRole('button', { name: 'Linear prerequisites' })).toBeInTheDocument())
    expect(linear).toHaveAttribute('data-state', 'not-connected')
    expect(within(linear).getByRole('button', { name: /Connect/ })).toBeInTheDocument()

    fireEvent.mouseEnter(within(linear).getByRole('button', { name: 'Linear prerequisites' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent(
      "You're already signed in to Linear on this machine, but Kiro Crew isn't set up to use it yet, which is why it isn't connected. Connect sets it up and uses that saved sign-in when it can, so it may still ask you to sign in again.",
    )
  })

  it('states the backend caveat in the status list on a connected, unshared pre-registered provider', async () => {
    mcpServers.mockResolvedValue([server({ name: 'github', url: 'https://api.githubcopilot.com/mcp/' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'connected', grantPresent: true, backendLacksOauth: true }],
    })
    mount()

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'connected'))
    // No tip (and no new heading): the caveat is a fact about the current state.
    expect(within(github).queryByRole('button', { name: 'GitHub prerequisites' })).not.toBeInTheDocument()
    expect(within(github).getByText('In chats')).toBeInTheDocument()
    expect(within(github).getByText("Saved for chats that support it. Some chats can't use this kind of sign-in yet.")).toBeInTheDocument()
  })

  // While chats run on KAS, a connected row says which word signs a KAS chat in.
  it('tells a connected row on KAS to type the server name once in chat', async () => {
    mcpServers.mockResolvedValue([server()])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'connected', grantPresent: true, signInInChat: true }],
    })
    mount()

    const notion = await waitFor(() => card('notion'))
    await waitFor(() => expect(notion).toHaveAttribute('data-state', 'connected'))
    expect(within(notion).getByText('In chats')).toBeInTheDocument()
    expect(within(notion).getByText('Chats sign in separately: type notion in a chat once.')).toBeInTheDocument()
  })

  it('shows no chat sign-in line on a connected row when chats run on Kiro CLI', async () => {
    mcpServers.mockResolvedValue([server()])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'connected', grantPresent: true }],
    })
    mount()

    const notion = await waitFor(() => card('notion'))
    await waitFor(() => expect(notion).toHaveAttribute('data-state', 'connected'))
    expect(within(notion).queryByText('In chats')).not.toBeInTheDocument()
  })

  // Test runs on Kiro CLI's grant; the gateway refuses it (409
  // crew_oauth_test_unsupported) for a Crew-held sign-in, so the card drops it.
  it.each([
    ['Crew-held', true, false],
    ['native', undefined, true],
  ])('offers Test on a connected %s row only when Crew does not hold it', async (_label, crewHeld, hasTest) => {
    mcpServers.mockResolvedValue([server({ name: 'github', url: 'https://api.githubcopilot.com/mcp/' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'connected', grantPresent: true, crewHeld }],
    })
    mount()

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'connected'))
    expect(within(github).queryByRole('button', { name: 'Test' }) !== null).toBe(hasTest)
    expect(within(github).getByRole('button', { name: 'Disconnect' })).toBeInTheDocument()
  })

  it('merges the backend caveat into the shared-grant tip beside Disconnect', async () => {
    mcpServers.mockResolvedValue([server({ name: 'github', url: 'https://api.githubcopilot.com/mcp/' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'connected', grantPresent: true, backendLacksOauth: true, grantSharedWith: ['other-entry'] }],
    })
    mount()

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'connected'))
    const tips = within(github).getAllByRole('button', { name: /GitHub/ }).filter(b => b.getAttribute('aria-label')?.includes('GitHub'))
    const disconnect = within(github).getByRole('button', { name: /Disconnect/ })
    const tip = tips.find(t => t.parentElement === disconnect.parentElement)
    expect(tip).toBeDefined()
    fireEvent.mouseEnter(tip as HTMLElement)
    const bubble = screen.getByRole('tooltip')
    expect(within(bubble).getByText('Before you disconnect')).toBeInTheDocument()
    expect(bubble).toHaveTextContent('its tools may not work there')
    expect(within(github).queryByText('In chats')).not.toBeInTheDocument()
  })

  it('keeps a pre-registered provider on the Connect path once its client is configured', async () => {
    // No `needsClientConfig` on the row: the operator has configured the app,
    // so the card offers the ordinary first-connect CTA.
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'not_connected', grantPresent: false }],
    })
    mount()

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(connectionsStatus).toHaveBeenCalled())
    expect(github).toHaveAttribute('data-state', 'not-connected')
    expect(within(github).getByRole('button', { name: /Connect/ })).toBeEnabled()
    expect(within(github).queryByRole('link', { name: 'Set up GitHub' })).not.toBeInTheDocument()
  })

  it('lets a consent in flight outrank the needs-configuration flag', async () => {
    // Backend mint table says a GitHub flow is live right now even though the
    // client record was cleared meanwhile: the approval URL is still valid, so
    // the card must keep waiting rather than send the user to Settings.
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'awaiting_consent', grantPresent: false, needsClientConfig: true }],
    })
    mount()

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'waiting-for-approval'))
    expect(within(github).queryByRole('link', { name: 'Set up GitHub' })).not.toBeInTheDocument()
  })

  it('marks only providers with a blocking prerequisite, as an icon beside Connect', async () => {
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    for (const provider of CONNECTION_PROVIDERS) {
      const icon = within(card(provider.slug)).queryByRole('button', { name: `${provider.name} prerequisites` })
      // A Google product row carries none: the Google entry states that step
      // once, above its rows.
      if (provider.prerequisite_copy && !isGoogleSlug(provider.slug)) expect(icon).toBeInTheDocument()
      else expect(icon).not.toBeInTheDocument()
    }
    // The launch set's two provider-side prerequisites: GitLab (Duo/group
    // access, else zero tools) and Atlassian (site required, else Accept stays
    // disabled). Pinned here so a registry edit that drops either surfaces as
    // a failure instead of silently deleting the warning.
    expect(within(card('gitlab')).getByRole('button', { name: 'GitLab prerequisites' })).toBeInTheDocument()
    expect(within(card('atlassian')).getByRole('button', { name: 'Atlassian prerequisites' })).toBeInTheDocument()
  })

  it('previews the prerequisite on hover and pins it on click until an outside click', async () => {
    mount()

    const gitlab = CONNECTION_PROVIDERS.find(provider => provider.slug === 'gitlab')
    expect(gitlab?.prerequisite_copy).toBeTruthy()
    const icon = await waitFor(() =>
      within(card('gitlab')).getByRole('button', { name: 'GitLab prerequisites' }),
    )

    // Hover previews the bubble (portal-rendered, so queried on the document).
    fireEvent.mouseEnter(icon)
    const bubble = screen.getByRole('tooltip')
    expect(within(bubble).getByText(gitlab?.prerequisite_copy ?? '__missing_gitlab_copy__')).toBeInTheDocument()
    expect(bubble).toHaveTextContent('GitLab Duo')
    // All three provider-side blockers stay named — dropping any one recreates
    // the silent zero-tools connect this warning exists to prevent.
    expect(bubble).toHaveTextContent('beta and experimental features')
    expect(bubble).toHaveTextContent('top-level group')
    expect(bubble).toHaveTextContent('exposes no tools')

    // Leaving without clicking dismisses the preview — after the short grace
    // that lets the pointer travel into the bubble (WCAG 1.4.13 hoverable).
    fireEvent.mouseLeave(icon)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('tooltip')).not.toBeInTheDocument())

    // Crossing from the icon into the bubble keeps it open, and a mousedown
    // inside it (starting a drag-selection of the steps) does not dismiss.
    fireEvent.mouseEnter(icon)
    const hoverBubble = screen.getByRole('tooltip')
    fireEvent.mouseLeave(icon)
    fireEvent.mouseEnter(hoverBubble)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.mouseDown(hoverBubble)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.mouseLeave(hoverBubble)
    await waitFor(() => expect(screen.queryByRole('tooltip')).not.toBeInTheDocument())

    // Clicking pins the bubble open past mouse-leave...
    fireEvent.click(icon)
    fireEvent.mouseLeave(icon)
    expect(icon).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('tooltip')).toBeInTheDocument()

    // ...and a click anywhere else dismisses it.
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(icon).toHaveAttribute('aria-expanded', 'false')

    // A tip opened by hover/focus alone (never pinned) must also dismiss on
    // Escape (WCAG 1.4.13) — regression for the pinned-only listener gate.
    fireEvent.mouseEnter(icon)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    // The bubble is position:fixed and computed once, so a scroll anywhere
    // dismisses it rather than letting it detach from its icon.
    fireEvent.click(icon)
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    fireEvent.scroll(document)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('hides the prerequisite icon once the provider is connected', async () => {
    mcpServers.mockResolvedValue([
      server({ name: 'gitlab', url: 'https://gitlab.com/api/v4/mcp' }),
    ])
    mount()

    const gitlab = await waitFor(() => card('gitlab'))
    expect(gitlab).toHaveAttribute('data-state', 'connected')
    expect(within(gitlab).queryByRole('button', { name: 'GitLab prerequisites' })).not.toBeInTheDocument()
  })

  it('keeps the prerequisite icon beside Connect when a configured provider is ungranted', async () => {
    // A GitLab entry whose grant is confirmed absent offers the same Connect
    // CTA as the first-connect path — same consent flow, same Duo/group wall
    // -- so the warning must ride this button too, not only the first-connect
    // one. The label used to read "Authorize" here while every other
    // consent-starting button read "Connect" for the identical startMint
    // path; both are unified on "Connect" now.
    mcpServers.mockResolvedValue([
      server({ name: 'gitlab', url: 'https://gitlab.com/api/v4/mcp', status: 'needs_auth' }),
    ])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'gitlab', status: 'not_connected', grantPresent: false }],
    })
    mount()

    const gitlab = await waitFor(() => card('gitlab'))
    await waitFor(() => expect(gitlab).toHaveAttribute('data-state', 'not-verified'))
    expect(within(gitlab).getByRole('button', { name: /Connect/ })).toBeInTheDocument()
    expect(within(gitlab).getByRole('button', { name: 'GitLab prerequisites' })).toBeInTheDocument()
  })

  it('renders a skeleton while the server list is in flight, then the cards', async () => {
    const pending = deferred<McpServer[]>()
    mcpServers.mockReturnValue(pending.promise)

    mount()

    expect(document.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(cards()).toHaveLength(0)

    pending.resolve([])
    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
  })

  it('warns that cards may be stale when the status read fails, without hiding them', async () => {
    mcpServers.mockRejectedValue(new Error('gateway down'))

    mount()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Connection status could not be loaded. Cards may be out of date.',
    )
    expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length)
  })

  it('filters by name and explains an empty result', async () => {
    mount()
    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    const search = screen.getByLabelText('Search services')

    fireEvent.change(search, { target: { value: 'linear' } })
    await waitFor(() => expect(cards()).toHaveLength(1))
    expect(screen.getByRole('heading', { name: 'Linear' })).toBeInTheDocument()
    expect(screen.getByText('1 available')).toBeInTheDocument()

    fireEvent.change(search, { target: { value: 'nothing-matches-this' } })
    expect(await screen.findByText('No services match this search.')).toBeInTheDocument()
    expect(cards()).toHaveLength(0)
  })

  it('also matches on the MCP endpoint, not just the display name', async () => {
    mount()
    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))

    fireEvent.change(screen.getByLabelText('Search services'), { target: { value: 'mcp.stripe.com' } })
    await waitFor(() => expect(cards()).toHaveLength(1))
    expect(screen.getByRole('heading', { name: 'Stripe' })).toBeInTheDocument()
  })
})

describe('a link in and the OAuth app part', () => {
  it('rings the provider card a managed MCP row names, and only that one', async () => {
    mount({ route: '/settings/connections?provider=stripe' })
    const stripe = await waitFor(() => card('stripe'))
    await waitFor(() => expect(stripe.className).toContain('border-accent'))
    expect(card('notion').className).not.toContain('border-accent')
  })

  it('opens the named OAuth app part for a settings search hit, the Google one for a Google product', async () => {
    mount({ route: '/settings/connections?highlight=connections.oauth-client-gmail' })
    await waitFor(() => expect(screen.getByTestId('oauth-app-google')).toBeVisible())
    expect(screen.getByTestId('oauth-app-github')).not.toBeVisible()
  })

  it('shows the six Google products as one entry holding their cards', async () => {
    mount()
    const entry = await waitFor(() => {
      const el = document.getElementById('connection-google')
      if (!el) throw new Error('no Google entry')
      return el
    })
    for (const slug of ['gmail', 'google-drive', 'google-calendar', 'google-sheets', 'google-docs', 'google-slides']) {
      expect(entry.contains(card(slug))).toBe(true)
    }
    expect(within(entry).getByRole('heading', { name: 'Google' })).toBeInTheDocument()
  })

  it('keeps Connect on a closed not-connected card, and opens the card on a click of its name', async () => {
    mount()
    const notion = await waitFor(() => byId('connection-notion', '[data-state="not-connected"]'))
    const toggle = within(notion).getByRole('button', { name: 'Notion' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(notion).getByRole('button', { name: 'Connect' })).toBeInTheDocument()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('hides a connected card\'s Test and Disconnect until the card is opened', async () => {
    mcpServers.mockResolvedValue([server({ connectedSince: '2026-03-04T10:00:00Z' })])
    mount()
    const notion = await waitFor(() => byId('connection-notion', '[data-state="connected"]'))
    expect(within(notion).queryByRole('button', { name: /Disconnect/ })).toBeNull()
    fireEvent.click(within(notion).getByRole('button', { name: 'Notion' }))
    expect(within(notion).getByRole('button', { name: /Disconnect/ })).toBeInTheDocument()
  })

  it('keeps the Google entry closed until clicked, then shows its products', async () => {
    mount()
    const entry = await waitFor(() => byId('connection-google'))
    expect(byId('connection-gmail')).not.toBeVisible()
    expect(within(entry).getByText(/Gmail · Google Drive/)).toBeInTheDocument()
    fireEvent.click(within(entry).getByRole('button', { name: 'Google' }))
    expect(entry.contains(document.getElementById('connection-gmail'))).toBe(true)
    expect(byId('connection-gmail')).toBeVisible()
    // The products and their Connect come before the shared OAuth app form.
    expect(byId('connection-gmail').compareDocumentPosition(screen.getByTestId('oauth-app-google')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps the Google OAuth app part on all six products whatever the search shows', async () => {
    mount()
    fireEvent.change(await screen.findByLabelText('Search services'), { target: { value: 'drive' } })
    const entry = await waitFor(() => byId('connection-google'))
    fireEvent.click(within(entry).getByRole('button', { name: 'Google' }))
    const all = CONNECTION_PROVIDERS.filter(p => p.slug === 'gmail' || p.slug.startsWith('google-')).map(p => p.slug)
    expect(all).toHaveLength(6)
    expect(screen.getByTestId('oauth-app-google').getAttribute('data-slugs')).toBe(all.join(','))
  })

  it('drops a Connect wait once its OAuth client is saved, since the save cancels that sign-in', async () => {
    connectionsMint.mockResolvedValue({ ok: true, slug: 'github', state: 'minting', token: 'tok1' })
    connectionsMintState.mockResolvedValue({ slug: 'github', state: 'minting', token: 'tok1' })
    mount()
    const github = await waitFor(() => byId('connection-github', '[data-state="not-connected"]'))
    fireEvent.click(within(github).getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'waiting-for-approval'))
    // The backend's cancel leaves the mint poll reading idle, which settles nothing.
    connectionsMintState.mockResolvedValue({ slug: 'github', state: 'idle' })
    fireEvent.click(within(github).getByRole('button', { name: 'save github' }))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'not-connected'))
    expect(within(github).getByRole('button', { name: 'Connect' })).toBeEnabled()
  })

  it('drops a cached awaiting-consent verdict once its OAuth client is saved, and re-reads the status', async () => {
    // The status feed cached the backend's own awaiting_consent row (a refresh mid-consent).
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'awaiting_consent', grantPresent: false }],
    })
    mount()
    const github = await waitFor(() => byId('connection-github', '[data-state="waiting-for-approval"]'))
    const reads = connectionsStatus.mock.calls.length
    // The save cancels that sign-in. The re-read hangs here, so only dropping
    // the cached verdict can bring the card back to Connect.
    connectionsStatus.mockReturnValue(deferred<{ schema_version: number; connections: never[] }>().promise)
    fireEvent.click(within(github).getByRole('button', { name: 'save github' }))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'not-connected'))
    expect(connectionsStatus.mock.calls.length).toBeGreaterThan(reads)
  })

  it('opens the Google entry by itself while a Google consent is in flight', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'google-drive', status: 'awaiting_consent', grantPresent: false }],
    })
    mount()
    await waitFor(() => expect(byId('connection-google-drive')).toBeVisible())
    expect(byId('connection-google-drive')).toHaveAttribute('data-state', 'waiting-for-approval')
  })

  it('keeps a closed card\'s OAuth app part mounted, so closing it loses no typed draft', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'not_connected', grantPresent: false, needsClientConfig: true }],
    })
    mount()
    const github = await waitFor(() => byId('connection-github', '[data-state="needs-configuration"]'))
    fireEvent.click(within(github).getByRole('button', { name: 'Set up GitHub' }))
    const part = within(github).getByTestId('oauth-app-github')
    expect(part).toBeVisible()
    fireEvent.click(within(github).getByRole('button', { name: 'GitHub' }))
    expect(within(github).getByTestId('oauth-app-github')).toBe(part)
    expect(part).not.toBeVisible()
  })

  it('gives a Google product row no tip naming a Configure button it does not have', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'gmail', status: 'not_connected', grantPresent: false, needsClientConfig: true }],
    })
    mount()
    const gmail = await waitFor(() => byId('connection-gmail', '[data-state="needs-configuration"]'))
    expect(within(gmail).queryByRole('button', { name: /prerequisites/ })).toBeNull()
    expect(within(gmail).queryByRole('button', { name: 'Set up Gmail' })).toBeNull()
  })

  it('offers no chat hand-off on any card while an OAuth app form holds unsaved input', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', error: 'invalid_grant' })])
    mount()
    const notion = await waitFor(() => byId('connection-notion', '[data-state="needs-attention"]'))
    expect(within(notion).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    // Unsaved input in the shared Google form, not Notion's own card.
    fireEvent.click(screen.getByRole('button', { name: 'type into google', hidden: true }))
    expect(within(notion).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('opens a broken connection by itself, and says so to a screen reader', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', error: 'invalid_grant' })])
    mount()
    const notion = await waitFor(() => byId('connection-notion', '[data-state="needs-attention"]'))
    expect(within(notion).getByRole('button', { name: /Reconnect/ })).toBeVisible()
    expect(within(notion).getByRole('button', { name: 'Notion' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('keeps a hidden card\'s OAuth app part mounted through a search', async () => {
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'github', status: 'not_connected', grantPresent: false, needsClientConfig: true }],
    })
    mount()
    const github = await waitFor(() => byId('connection-github', '[data-state="needs-configuration"]'))
    const part = within(github).getByTestId('oauth-app-github')
    const search = screen.getByLabelText('Search services')
    fireEvent.change(search, { target: { value: 'asana' } })
    expect(github).not.toBeVisible()
    fireEvent.change(search, { target: { value: '' } })
    expect(byId('connection-github').contains(part)).toBe(true)
  })

  it('gives the closed Google entry a button that opens its product list, named for that', async () => {
    mount()
    const entry = await waitFor(() => byId('connection-google'))
    // Not "Connect": the button links no account, it lists the six products.
    expect(within(entry).queryByRole('button', { name: 'Connect' })).toBeNull()
    const choose = await within(entry).findByRole('button', { name: 'Choose Google products' })
    fireEvent.click(choose)
    expect(byId('connection-gmail')).toBeVisible()
  })

  it('gives the Google entry one step tip beside its own action, and no tip on the product rows', async () => {
    mount()
    const entry = await waitFor(() => byId('connection-google'))
    const choose = await within(entry).findByRole('button', { name: 'Choose Google products' })
    // Closed: the one tip sits in the action row, beside Choose Google products.
    expect(within(choose.parentElement!).getAllByRole('button', { name: 'Google prerequisites' })).toHaveLength(1)
    fireEvent.click(choose)
    const tip = within(entry).getByRole('button', { name: 'Google prerequisites' })
    expect(tip).toBeVisible()
    expect(within(entry).getAllByRole('button', { name: /prerequisites$/ })).toHaveLength(1)
    fireEvent.click(tip)
    expect(screen.getByRole('tooltip')).toHaveTextContent(i18next.t('pages.connectionsPage.google_entry_step_connect'))
    expect(within(byId('connection-gmail')).getByRole('button', { name: 'Connect' })).toBeVisible()
  })

  it('shows one status on the closed Google entry', async () => {
    mcpServers.mockResolvedValue([server({ name: 'gmail', url: CONNECTION_PROVIDERS.find(p => p.slug === 'gmail')!.mcp_url })])
    mount()
    const entry = await waitFor(() => byId('connection-google'))
    await waitFor(() => expect(within(entry.querySelector('header')!).getByText('Connected')).toBeInTheDocument())
  })

  it('scrolls a ?provider= deep link once the gallery is turned on after the list loaded', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const { rerender } = mount({ servicesEnabled: false, route: '/settings/connections?provider=stripe' })
    await waitFor(() => expect(mcpServers).toHaveBeenCalled())
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    expect(scroll).not.toHaveBeenCalled()
    rerender(<ConnectionsPage servicesEnabled />)
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1))
  })

  it('keeps every OAuth app part on the page when the gallery is turned off', async () => {
    mount({ servicesEnabled: false })
    const list = await waitFor(() => screen.getByTestId('connections-oauth-apps-only'))
    expect(within(list).getByTestId('oauth-app-github')).toBeVisible()
    expect(within(list).getByTestId('oauth-app-google')).toBeVisible()
    expect(document.querySelectorAll('article[data-state]')).toHaveLength(0)
    // No search box and no search: an empty-search notice would mean nothing here.
    await waitFor(() => expect(mcpServers).toHaveBeenCalled())
    expect(screen.queryByText(i18next.t('pages.connectionsPage.no_matching_services'))).toBeNull()
  })

  it('scrolls a ?provider= deep link to its card once the list has loaded', async () => {
    const list = deferred<McpServer[]>()
    mcpServers.mockReturnValue(list.promise)
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    mount({ route: '/settings/connections?provider=stripe' })
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    expect(scroll).not.toHaveBeenCalled()
    await act(async () => { list.resolve([]) })
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(1))
  })
})

describe('a connected provider', () => {
  const connected = [server({ accountLabel: 'ada@example.com', connectedSince: '2026-03-04T10:00:00Z' })]

  it('reports the connection date and nothing it cannot know', async () => {
    mcpServers.mockResolvedValue(connected)
    mount()

    const notion = await waitFor(() => {
      const el = card('notion')
      expect(el).toHaveAttribute('data-state', 'connected')
      return el
    })
    expect(within(notion).getByText('Mar 4, 2026')).toBeInTheDocument()
    // The card never invents identity or permissions: the status API carries
    // no account or scope facts, so no Account/Access rows may render.
    expect(within(notion).queryByText('Account')).not.toBeInTheDocument()
    expect(within(notion).queryByText('ada@example.com')).not.toBeInTheDocument()
    expect(within(notion).queryByText(/Recommended scopes/)).not.toBeInTheDocument()
    // Revoke guidance appears only after Disconnect, not as standing boilerplate.
    expect(within(notion).queryByRole('link', { name: /Revoke at Notion/ })).not.toBeInTheDocument()
  })

  it('omits the connection date row when no date is known', async () => {
    mcpServers.mockResolvedValue([server({ name: 'stripe', url: STRIPE_URL })])
    mount()

    const stripe = await waitFor(() => {
      const el = card('stripe')
      expect(el).toHaveAttribute('data-state', 'connected')
      return el
    })
    expect(within(stripe).queryByText('Connected since')).not.toBeInTheDocument()
    expect(within(stripe).queryByText('Authorized account')).not.toBeInTheDocument()
    expect(within(stripe).queryByText('Access is controlled by enabled tools.')).not.toBeInTheDocument()
  })

  it('confirms authenticated usable tools as success feedback', async () => {
    mcpServers.mockResolvedValue(connected)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    expect(await screen.findByText('Connection is healthy.')).toBeInTheDocument()
    expect(connectionsTest).toHaveBeenCalledWith('notion')
    expect(mcpProbe).not.toHaveBeenCalled()
  })

  it('shows connected but zero exposed tools as an honest warning', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsTest.mockResolvedValue({
      schema_version: 1,
      slug: 'notion',
      verdict: 'no_tools',
      code: 'no_tools_exposed',
      toolCount: 0,
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    const warning = await screen.findByRole('alert')
    expect(warning).toHaveTextContent('Offered no tools to inspect')
    // A no_tools verdict is NOT a failed request -- the call succeeded and
    // returned a verdict -- so it keeps this page's plain feedback line (a
    // <div>) rather than being dressed as an error through ErrorNotice.
    expect(warning.tagName).toBe('DIV')
    expect(card('notion')).toHaveAttribute('data-state', 'connected')
    expect(screen.queryByText('Connection is healthy.')).toBeNull()
  })

  it('links GitLab zero-tools guidance to its separately maintained prerequisites', async () => {
    mcpServers.mockResolvedValue([
      server({ name: 'gitlab', url: 'https://gitlab.com/api/v4/mcp' }),
    ])
    connectionsTest.mockResolvedValue({
      schema_version: 1,
      slug: 'gitlab',
      verdict: 'no_tools',
      code: 'no_tools_exposed',
      toolCount: 0,
    })
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'gitlab', status: 'connected', grantPresent: true }],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('gitlab')).getByRole('button', { name: 'Test' })))

    const warning = await screen.findByRole('alert')
    // The connected-but-toolless user gets the exact provider-side steps, not
    // just a generic reason and a link. The steps render localized via the
    // slug-keyed catalog entry, whose English value must stay in lockstep with
    // the registry's prerequisite_copy (the drift guard below).
    expect(warning).toHaveTextContent('beta and experimental features')
    expect(warning).toHaveTextContent('top-level group')
    expect(within(warning).getByText(i18next.t('pages.connectionsPage.prerequisite_gitlab_steps'))).toBeInTheDocument()
    expect(within(warning).getByRole('link', { name: 'Documentation' }))
      .toHaveAttribute('href', 'https://docs.gitlab.com/user/model_context_protocol/mcp_server/')
  })

  it('keeps the localized prerequisite catalogs in lockstep with the registry English', () => {
    // The registry decides WHETHER a card warns and is the English fallback;
    // the en catalog is what actually renders in the default locale. If they
    // drift, English users silently read different steps than the registry
    // documents — so equality is pinned here for every provider that warns.
    for (const provider of CONNECTION_PROVIDERS) {
      if (!provider.prerequisite_copy) continue
      // Catalog keys spell a slug's hyphens as underscores (google-drive -> google_drive).
      expect(i18next.t(`pages.connectionsPage.prerequisite_${provider.slug.replace(/-/g, '_')}`)).toBe(provider.prerequisite_copy)
    }
  })

  it('surfaces an authenticated failure as an error on the card', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsTest.mockResolvedValue({
      schema_version: 1,
      slug: 'notion',
      verdict: 'failed',
      code: 'mcp_server_failed',
      toolCount: 0,
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    const failure = await screen.findByRole('alert')
    expect(failure).toHaveTextContent('Action failed: The provider did not pass the connection test.')
    // A generic action failure is a FAILED request, so it renders through the
    // shared error surface (`ErrorNotice`) rather than a hand-written alert box
    // -- its inline variant is a <span role="alert">, while this page's plain
    // feedback line is a <div>, so the tag names which surface rendered it.
    expect(failure.tagName).toBe('SPAN')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    // No hand-off beside the return-address paste-back input, whose typed value
    // survives a failed relay and would be discarded by navigating to the chat.
    expect(within(failure).queryByRole('button', { name: /ask.*agent/i })).toBeNull()
  })

  it('shows the busy label while authenticated enumeration is in flight', async () => {
    const pending = deferred<{ verdict: string }>()
    mcpServers.mockResolvedValue(connected)
    connectionsTest.mockReturnValue(pending.promise)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    // The card's own busy Test is aria-disabled, not disabled, so it stays in
    // the tab order (#32987); Disconnect is still disabled outright.
    const testing = await screen.findByRole('button', { name: 'Testing…' })
    expect(testing).not.toBeDisabled()
    expect(testing).toHaveAttribute('aria-disabled', 'true')
    expect(within(card('notion')).getByRole('button', { name: /Disconnect/ })).toBeDisabled()

    pending.resolve({ verdict: 'usable' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled())
  })

  it('keeps keyboard focus on the pressed Test while the test runs, and ignores a second press', async () => {
    const pending = deferred<{ verdict: string }>()
    mcpServers.mockResolvedValue(connected)
    connectionsTest.mockReturnValue(pending.promise)
    mount()

    const test = await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' }))
    test.focus()
    fireEvent.click(test)

    const testing = await screen.findByRole('button', { name: 'Testing…' })
    expect(testing).toBe(test)
    // A real browser drops focus from a disabled button; jsdom does not, so
    // the tab-order half is pinned on the attribute itself.
    expect(testing).not.toBeDisabled()
    expect(testing).toHaveFocus()
    fireEvent.click(testing)
    expect(connectionsTest).toHaveBeenCalledTimes(1)

    pending.resolve({ verdict: 'usable' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Test' })).toHaveFocus())
  })

  it('disables every OTHER card\'s Test button while one card is testing, with an explanation', async () => {
    const pending = deferred<{ verdict: string }>()
    mcpServers.mockResolvedValue([
      server({ accountLabel: 'ada@example.com' }),
      server({ name: 'stripe', url: STRIPE_URL }),
    ])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [
        { slug: 'notion', status: 'connected', grantPresent: true },
        { slug: 'stripe', status: 'connected', grantPresent: true },
      ],
    })
    connectionsTest.mockImplementation((slug: string) => slug === 'notion' ? pending.promise : Promise.resolve({ verdict: 'usable' }))
    mount()

    await waitFor(() => expect(card('stripe')).toHaveAttribute('data-state', 'connected'))
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Test' }))

    // The sibling's button is disabled and explains itself; it is NOT relabeled
    // "Testing…" -- that label is reserved for the card that owns the request.
    // aria-label overrides the visible "Test" text for the accessible name, so
    // the disabled sibling's true accessible name IS the explanation.
    const explanation = 'One connection test runs at a time — Notion is testing'
    const stripeTest = await waitFor(() => within(card('stripe')).getByRole('button', { name: explanation }))
    expect(stripeTest).toBeDisabled()
    expect(stripeTest).toHaveTextContent('Test')
    expect(stripeTest).toHaveAttribute('title', 'One connection test runs at a time — Notion is testing')
    // Every OTHER action on the sibling card stays enabled -- only Test is blocked.
    expect(within(card('stripe')).getByRole('button', { name: /Disconnect/ })).toBeEnabled()
    // The testing card's own label is still the busy spinner label, not the
    // sibling-disable text -- the two must never collide on one card.
    expect(within(card('notion')).getByRole('button', { name: 'Testing…' })).toBeInTheDocument()

    pending.resolve({ verdict: 'usable' })
    await waitFor(() => expect(within(card('stripe')).getByRole('button', { name: 'Test' })).toBeEnabled())
  })

  it('never disables a testing card\'s OWN button against its own name', async () => {
    const pending = deferred<{ verdict: string }>()
    mcpServers.mockResolvedValue(connected)
    connectionsTest.mockReturnValue(pending.promise)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    // The busy spinner label covers this card's own in-flight state; it must
    // never ALSO carry the sibling-disable title naming itself.
    const testing = within(card('notion')).getByRole('button', { name: 'Testing…' })
    expect(testing).not.toHaveAttribute('title')

    await act(async () => { pending.resolve({ verdict: 'usable' }) })
  })

  it('renders a 409 test_in_flight refusal as a named warning, never an opaque error', async () => {
    mcpServers.mockResolvedValue([
      server({ accountLabel: 'ada@example.com' }),
      server({ name: 'stripe', url: STRIPE_URL }),
    ])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [
        { slug: 'notion', status: 'connected', grantPresent: true },
        { slug: 'stripe', status: 'connected', grantPresent: true },
      ],
    })
    // The server is the ground truth for the refusal: the client's own busy
    // state disables the sibling button, but this proves the CARD reacts
    // correctly to a 409 arriving regardless of what disabled it client-side --
    // a stale disabled state, a second tab, or a race all reach this path.
    connectionsTest.mockRejectedValue(
      new ApiError(409, 'a connection test for notion is already running', JSON.stringify({
        error: 'a connection test for notion is already running',
        code: 'test_in_flight',
        slug: 'notion',
      })),
    )
    mount()

    await waitFor(() => expect(card('stripe')).toHaveAttribute('data-state', 'connected'))
    // Directly invoking the click handler bypasses the client-side `disabled`
    // attribute, isolating the response-handling path from the button-state path.
    fireEvent.click(within(card('stripe')).getByRole('button', { name: 'Test' }))

    const warning = await screen.findByRole('alert')
    expect(warning).toHaveTextContent('A connection test for Notion is already running')
    expect(warning).not.toHaveTextContent('Unknown error')
    expect(warning).not.toHaveTextContent('Action failed')
    // A rejected request renders through the SHARED error surface, so the
    // structured context (endpoint, status, backend `code`) is recoverable
    // rather than thrown away by a hand-written line. ErrorNotice's inline
    // variant owns the role="alert", so exactly one alert exists -- a nested
    // pair would announce twice.
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    // ErrorNotice's inline variant is a <span role="alert">, while this page's
    // plain feedback line is a <div> -- so the tag proves WHICH surface rendered
    // it without reaching into the component's internals.
    expect(warning.tagName).toBe('SPAN')
    // No hand-off beside the return-address paste-back field: the button
    // navigates to the chat and unmounts this gallery.
    expect(within(warning).queryByRole('button', { name: /ask.*agent/i })).toBeNull()
    // The card stays connected -- a single-flight refusal is not a test failure.
    expect(card('stripe')).toHaveAttribute('data-state', 'connected')
  })

  it('uninstalls the entry on Disconnect and keeps pointing at the provider revoke page', async () => {
    mcpServers.mockResolvedValue(connected)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent(
      'Disconnected locally. Revoke access at the provider to cancel the grant completely.',
    )
    expect(within(note).getByRole('link', { name: /Revoke at Notion/ })).toBeInTheDocument()
    expect(connectionsDisconnect).toHaveBeenCalledWith('notion')
  })

  it('announces a surviving grant artifact as an alert, not a success', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: true,
      grantSurviving: ['registration'],
      entryRemoved: true,
      grantSharedWith: [],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // role=alert rather than role=status: a local grant outliving the click is the
    // exact state this endpoint exists to prevent, so rendering it as a green
    // success would be the dishonesty the slice was written to remove. The revoke
    // link still shows, because acting at the provider is now the user's next step.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'Part of the stored grant could not be removed. Revoke access at the provider.',
    )
    // The error TEXT is owned by ErrorNotice (which carries the role), so the
    // revoke affordance is its sibling in the feedback slot rather than inside
    // the announced region -- supplemental guidance, not part of the error
    // string. The message itself already says to revoke at the provider.
    expect(alert.tagName).toBe('SPAN')
    const slot = alert.closest('[data-slot="connection-feedback"]')
    expect(slot).not.toBeNull()
    expect(within(slot as HTMLElement).getByRole('link', { name: /Revoke at Notion/ })).toBeInTheDocument()
  })

  it('alerts when the Disconnect did not reach the agent spec', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: true,
      grantSurviving: [],
      entryRemoved: true,
      grantSharedWith: [],
      grantCensusIncomplete: false,
      agentSpecRewritten: false,
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // The entry left the config, but new sessions may still load it: a failed
    // step, so an alert rather than a green success.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Disconnected locally.')
    expect(alert).toHaveTextContent('new chats still load this connection')
  })

  it('does not blame the agent spec when the Disconnect removed no entry', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      grantSurviving: [],
      entryRemoved: false,
      grantSharedWith: [],
      grantCensusIncomplete: false,
      agentSpecRewritten: false,
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    await screen.findByText(/server configuration was left unchanged/i)
    expect(screen.queryByText(/new chats still load this connection/i)).not.toBeInTheDocument()
  })

  it('says the grant was kept when another entry shares the endpoint', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      // A pair kept for a sharer is never re-stat'd, so grantSurviving is empty
      // by construction -- survivors now mean a FAILED unlink and nothing else.
      grantSurviving: [],
      entryRemoved: true,
      grantSharedWith: ['notion-work'],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // Artifacts surviving BY DESIGN are not the same event as a failed unlink, so
    // this is role=status, not role=alert. But the user must not be told their
    // access here was removed when it deliberately was not.
    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent(
      'The stored grant was kept because the same endpoint is also configured by: notion-work. Revoking at the provider would cut off their access too.',
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('names every entry the kept grant is shared with, not just that one exists', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      grantSurviving: [],
      entryRemoved: true,
      grantSharedWith: ['notion-work', 'notion-personal'],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // The response already carries the names. Saying only that "another server"
    // uses the endpoint leaves the user unable to find the entry that blocked the
    // revoke -- and it under-reports when more than one did. The names are a
    // locale list ("and" in English, 、 in Japanese), not an English comma join.
    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent(
      'The stored grant was kept because the same endpoint is also configured by: notion-work and notion-personal. Revoking at the provider would cut off their access too.',
    )
  })

  it('reports the kept entry alongside the kept grant, never "Entry removed"', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      grantSurviving: [],
      entryRemoved: false,
      grantSharedWith: ['notion-work'],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // The backend's own `if not ours` early return: grant kept for a sharer AND
    // entry left alone. Each fact gets its own clause; asserting either removal
    // would be the dishonesty two review rounds landed on in this span.
    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent(
      'The stored grant was kept because the same endpoint is also configured by: notion-work. Revoking at the provider would cut off their access too. Your server configuration was left unchanged. You can manage this entry from the MCP Servers tab.',
    )
    expect(note).not.toHaveTextContent('Entry removed')
  })

  it('says the entry was left alone when it is not ours by endpoint', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: true,
      grantSurviving: [],
      entryRemoved: false,
      grantSharedWith: [],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // Telling the user their entry came out while it is still configured would be
    // the same dishonesty class this slice exists to remove.
    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent(
      'The stored grant was removed. Your server configuration was left unchanged. You can manage this entry from the MCP Servers tab.',
    )
  })

  it('warns with the MCP Servers recourse when nothing here was ours', async () => {
    mcpServers.mockResolvedValue(connected)
    // The not-ours outcome: no grant existed and no purge-eligible entry
    // matched, so the backend changed nothing at all.
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      grantSurviving: [],
      entryRemoved: false,
      grantSharedWith: [],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // role=alert with WARN styling, not a green role=status success: the card
    // still shows Connected with a live Disconnect button, so a bare "left
    // unchanged" success reads as an action that worked yet changed nothing,
    // and the user's only move is to click again. The message must carry the
    // recourse (the MCP Servers tab) — and it must state no cause, because
    // entryRemoved=false cannot prove WHY the entry was kept.
    const note = await screen.findByRole('alert')
    expect(note).toHaveClass('text-warn')
    expect(note).not.toHaveClass('text-ok')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(note).toHaveTextContent(
      'Your server configuration was left unchanged. You can manage this entry from the MCP Servers tab.',
    )
    expect(note).not.toHaveTextContent('points at a different server')
  })

  it('reports a census-incomplete keep as a deliberate refusal, never a failed removal', async () => {
    mcpServers.mockResolvedValue(connected)
    // The backend's fail-closed path: an unreadable spec source can HIDE a sharer,
    // so the grant is kept with census_incomplete=true and no sharer to name.
    // Nothing is attempted, so nothing is re-stat'd and grantSurviving is empty --
    // the keep must still read as a deliberate refusal with a next step.
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      grantSurviving: [],
      entryRemoved: true,
      grantSharedWith: [],
      grantCensusIncomplete: true,
      grantCensusUnreadable: ['dev.json'],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    // role=alert, and WARN rather than OK styling. This is the one outcome that
    // tells the user their access was NOT withdrawn and hands them a repair to
    // make; rendering it green under role=status announced a chore as a success
    // and let a screen reader treat it as a passing status update. It is still not
    // an `error`: nothing failed, a safety rule declined to act.
    const note = await screen.findByRole('alert')
    expect(note).toHaveClass('text-warn')
    expect(note).not.toHaveClass('text-ok')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    // The instruction has to name the file. "Fix the unreadable file" with no file
    // named is a repair the user cannot locate, and the census already knows it.
    expect(note).toHaveTextContent(
      'The stored grant was kept because dev.json could not be read to rule out another server using it. Fix or remove that file and disconnect again.',
    )
    expect(note).not.toHaveTextContent('could not be removed')
  })

  it('falls back to the source-less census wording when no file can be named', async () => {
    mcpServers.mockResolvedValue(connected)
    // The other half of census_incomplete: an entry whose URL could not be safely
    // compared. There is no unreadable FILE, so the list is empty and the message
    // must not interpolate a blank name into "fix that file".
    connectionsDisconnect.mockResolvedValue({
      ok: true,
      grantRemoved: false,
      grantSurviving: [],
      entryRemoved: true,
      grantSharedWith: [],
      grantCensusIncomplete: true,
      grantCensusUnreadable: [],
    })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    const note = await screen.findByRole('alert')
    expect(note).toHaveTextContent(
      'The stored grant was kept because a configuration source could not be read to rule out another server using it. Check your server configuration and disconnect again.',
    )
    // The source-less trigger can be an entry whose URL could not be compared,
    // which involves no file — the guidance must not name one.
    expect(note).not.toHaveTextContent('unreadable file')
  })

  it('reports a failed disconnect as an error instead of claiming success', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockRejectedValue(new Error('config is read-only'))
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    expect(await screen.findByRole('alert')).toHaveTextContent('Action failed: config is read-only')
  })

  it('names an unknown thrown value rather than rendering "undefined"', async () => {
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockRejectedValue('not an Error')
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    expect(await screen.findByRole('alert')).toHaveTextContent('Action failed: Unknown error')
  })
})

describe('a provider that needs attention', () => {
  it('explains an invalid grant with the runtime error and offers Reconnect', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', error: 'invalid_grant' })])
    mount()

    const notion = await waitFor(() => {
      const el = card('notion')
      expect(el).toHaveAttribute('data-state', 'needs-attention')
      return el
    })
    expect(within(notion).getByText('Notion says this connection is no longer valid.')).toBeInTheDocument()
    expect(within(notion).getByText('invalid_grant')).toBeInTheDocument()
    expect(within(notion).getByRole('button', { name: /Reconnect/ })).toBeEnabled()
  })

  it('does not put a provider verdict on a transport timeout', async () => {
    // A probe timeout is transport noise, not a provider statement — the banner
    // must not claim "Notion says…" over it (the copy the user reported seeing
    // beside a literal "timeout" detail). The verdict copy needs auth-shaped
    // evidence; everything else gets the honest could-not-reach framing with the
    // same Reconnect affordance.
    mcpServers.mockResolvedValue([server({ status: 'error', error: 'timeout' })])
    mount()

    const notion = await waitFor(() => {
      const el = card('notion')
      expect(el).toHaveAttribute('data-state', 'needs-attention')
      return el
    })
    expect(within(notion).getByText('Notion could not be reached to check this connection.')).toBeInTheDocument()
    expect(within(notion).queryByText('Notion says this connection is no longer valid.')).not.toBeInTheDocument()
    expect(within(notion).getByText('timeout')).toBeInTheDocument()
    expect(within(notion).getByRole('button', { name: /Reconnect/ })).toBeEnabled()
  })

  it('classifies error details: provider verdicts need auth-shaped evidence', () => {
    // Rejections the provider actually expressed.
    for (const detail of ['invalid_grant', 'HTTP 401 Unauthorized', 'token revoked', 'user denied consent', 'access forbidden (403)', 'token expired', 'authorization has expired']) {
      expect(errorIndicatesProviderRejection(detail)).toBe(true)
    }
    // Transport noise and unknowns: no verdict without evidence. The TLS row is
    // the Opus counterexample: "expired" as a bare substring would have matched
    // a certificate error and re-created the exact misattribution this fix
    // removes; likewise bare 401/403 digits inside ports or ids.
    for (const detail of ['timeout', 'ECONNRESET', 'getaddrinfo ENOTFOUND gitlab.com', 'server returned 502', 'certificate verify failed: certificate has expired', 'connect to host port 4013 failed', '', undefined]) {
      expect(errorIndicatesProviderRejection(detail)).toBe(false)
    }
  })

  it('prefers the OAuth banner error over the stale server error', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', error: 'stale server error' })])
    mount({ chat: { messages: [banner('Notion', { failed: true, error: 'user denied consent' })] } })

    const notion = await waitFor(() => {
      const el = card('notion')
      expect(el).toHaveAttribute('data-state', 'needs-attention')
      return el
    })
    expect(within(notion).getByText('user denied consent')).toBeInTheDocument()
    expect(within(notion).queryByText('stale server error')).not.toBeInTheDocument()
  })

  it('rewrites the endpoint and re-enables a disabled entry, because reconnect IS consent', async () => {
    mcpServers.mockResolvedValue([server({
      status: 'error',
      enabled: false,
      presence: { kirocrew: false, kiroGlobal: true, ccGlobal: false },
    })])
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Reconnect/ })))

    await waitFor(() => expect(mcpCustomUpdate).toHaveBeenCalledWith('notion', {
      url: NOTION_URL, scopes: ['read'], clientId: 'client-1',
    }))
    // Global scopes are passed through unchanged; only Kiro Crew's own is turned on.
    expect(mcpApply).toHaveBeenCalledWith([{ name: 'notion', kirocrew: true, kiroGlobal: true, ccGlobal: false }])
  })

  it('leaves an already-enabled entry alone apart from the endpoint rewrite', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', enabled: true })])
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Reconnect/ })))

    await waitFor(() => expect(mcpCustomUpdate).toHaveBeenCalledWith('notion', {
      url: NOTION_URL, scopes: ['read'], clientId: 'client-1',
    }))
    expect(mcpApply).not.toHaveBeenCalled()
  })
})

// Provider brand marks. The art inventory and the card roster are maintained
// separately, so a provider CAN ship without a mark -- `superhuman` is in the
// registry today with no `.svg` file at all, while every provider the launch
// gate lets through onto the visible gallery now ships one (github is also
// launch-gated off, but the inventory already covers it, so it cannot stand
// in for the gap case). That gap case must degrade to the lettered tile
// rather than to an empty gap, and it did not: the card built
// `<ProviderLogo …/>` unconditionally, which is a truthy element even for a
// slug with no mark, so the `??` fallback beside it was unreachable.
// Exercised directly against `ProviderLogo` / `PROVIDER_LOGO_SLUGS` below
// rather than through a real gallery card, because no visible card is
// currently gapped to hang that assertion on.
describe('the card brand mark', () => {
  it('ships no mark for a provider the launch gate holds back (superhuman)', () => {
    // The card's own fallback logic (`PROVIDER_LOGO_SLUGS.includes(slug) ? <ProviderLogo .../> : null`)
    // reads this list; asserting on it directly pins the gap the lettered
    // tile exists to cover, independent of which providers are launch-gated.
    // `superhuman` is in the registry (launch_gate_passed: false) with no
    // `.svg` file in `logos/` at all -- unlike github, which the launch gate
    // also holds back but which the inventory already covers.
    expect(PROVIDER_LOGO_SLUGS).not.toContain('superhuman')
    expect(ProviderLogo({ slug: 'superhuman' })).toBeNull()
  })

  it('renders the mark, and no letter, for a provider that ships one', async () => {
    mount()

    await waitFor(() => expect(card('notion')).toBeInTheDocument())
    const slot = card('notion').querySelector('header [role="img"]')
    if (!slot) throw new Error('no brand-mark slot on the notion card')
    expect(slot.querySelector('[data-testid="provider-logo-notion"]')).not.toBeNull()
    // The tile must not double up with the mark.
    expect(slot.textContent).toBe('')
  })

  it('renders the full-colour gitlab mark, and no letter', async () => {
    mount()

    await waitFor(() => expect(card('gitlab')).toBeInTheDocument())
    const slot = card('gitlab').querySelector('header [role="img"]')
    if (!slot) throw new Error('no brand-mark slot on the gitlab card')
    expect(slot.querySelector('[data-testid="provider-logo-gitlab"]')).not.toBeNull()
    expect(slot.textContent).toBe('')
  })
})

// sizes to its own copy makes the row it sits in ragged. jsdom runs no layout
// engine, so the reserved box IS the observable here: asserting the floor/clamp
// pair on the description is what a browser's equal-height rows reduce to, and it
// is what a later "tidy up the classes" edit would break.
describe('the card description box', () => {
  it('reserves two lines for every description and allows a third', async () => {
    mount()

    await waitFor(() => expect(card('notion')).toBeInTheDocument())
    // GitLab's copy wraps to two lines where Notion's takes one: both cards must
    // still reserve the same vertical space.
    for (const slug of ['notion', 'gitlab']) {
      const description = card(slug).querySelector('p')
      if (!description) throw new Error(`no description paragraph on the ${slug} card`)
      // A FLOOR, not a fixed height: a locale whose copy needs a third line grows
      // rather than clipping a write-scope disclosure behind a hover-only title.
      expect(description).toHaveClass('min-h-[34px]')
      expect(description).not.toHaveClass('h-[34px]')
      expect(description).toHaveClass('line-clamp-3')
      // An explicit line-height is what makes the reserved height hold whole
      // lines instead of cutting one mid-glyph.
      expect(description).toHaveClass('leading-[17px]')
    }
  })
})

// jsdom runs no layout engine, so the CLASS is the observable here: an
// `items-start` override on the grid is what let one row's cards take their
// own heights instead of stretching to a shared bottom edge, which is what
// "nothing is flush" reported. This supersedes an earlier deliberate choice
// ("a taller card must not stretch its siblings") -- the user has since
// overridden that with an explicit flush-rows request, and every card is
// already `flex flex-col` with its action region on `mt-auto`, so a stretched
// row aligns buttons on a shared edge rather than clipping content.
describe('the gallery grid', () => {
  it('stretches row cards to equal height instead of sizing each to its own content', async () => {
    mount()

    await waitFor(() => expect(card('notion')).toBeInTheDocument())
    const grid = card('notion').parentElement
    if (!grid) throw new Error('no grid parent above the notion card')
    expect(grid).not.toHaveClass('items-start')
    expect(grid).toHaveClass('grid')
  })
})
