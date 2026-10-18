// First render-level coverage for the Connections page — the provider gallery
// shell that owns the four card states (not-connected → waiting-for-approval →
// connected / needs-attention), the two-tab switcher, and every write action a
// card can fire (connect, cancel, reconnect, disconnect, test, OAuth relay).
//
// Two things shape this file:
//
//   1. The gallery is GATED. `servicesEnabled` carries the `connections_ui`
//      escape hatch: the shipped default is on, but the component's own parameter
//      default is false, so a test that wants the closed panel passes
//      `servicesEnabled: false` — that flag is the module's own seam, not a hack.
//   2. The page's only outside seams are `api` (mocked here — nothing dials the
//      network) and the MCP Servers sub-tab, which is a whole page of its own.
//      `McpTab` is stubbed at its module boundary with a button that fires
//      `onManagedProviderClick`, which is the only contract this page has with
//      it.
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
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

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

// The MCP Servers sub-tab is a page in its own right; the only contract this
// page has with it is the managed-provider deep link back into the gallery.
vi.mock('../pages/overview/McpTab', () => ({
  default: ({ onManagedProviderClick }: { onManagedProviderClick: (slug: string) => void }) => (
    <button type="button" onClick={() => onManagedProviderClick('stripe')}>deep link to stripe</button>
  ),
}))

import ConnectionsPage, { errorIndicatesProviderRejection } from '../pages/connections/ConnectionsPage'
import { CONNECTION_PROVIDERS } from '../pages/connections/registry'
import ProviderLogo, { PROVIDER_LOGO_SLUGS } from '../pages/connections/ProviderLogo'
import { i18next } from '../i18n'
import { ApiError } from '../api/client'
import {
  NOTION_URL,
  server,
  banner,
  mount,
  card,
  deferred,
} from './ConnectionsPage.coverageTestUtils'

const STRIPE_URL = 'https://mcp.stripe.com'

const cards = (): HTMLElement[] => Array.from(document.querySelectorAll('article[data-state]'))

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

    // Both tabs still render — only the OFFER is withheld.
    expect(screen.getByRole('tab', { name: /Services/ })).toBeInTheDocument()
    expect(await screen.findByText('No services match this search.')).toBeInTheDocument()
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
    expect(await screen.findByText('No services match this search.')).toBeInTheDocument()
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
    expect(screen.getByText(`${CONNECTION_PROVIDERS.length} available`)).toBeInTheDocument()
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

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'needs-configuration'))
    expect(document.querySelector('#connection-github[data-state="needs-configuration"]')).toBe(github)
    expect(within(github).getByText('Needs configuration')).toBeInTheDocument()
    expect(within(github).queryByRole('button', { name: /Connect/ })).not.toBeInTheDocument()
    expect(within(github).getByRole('link', { name: /Documentation/ })).toHaveAttribute('target', '_blank')

    const configure = within(github).getByRole('link', { name: 'Configure OAuth app' })
    const href = configure.getAttribute('href') ?? ''
    expect(href.startsWith('/settings/connections')).toBe(true)
    // The registry entry id (settingsManual.ts), which resolves to the card's
    // data-setting-id and waits for the async panel; encodeURIComponent leaves
    // '.' and '-' alone, so the literal is what the URL carries.
    expect(href).toContain('highlight=connections.oauth-client-github')
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

    const github = await waitFor(() => card('github'))
    await waitFor(() => expect(github).toHaveAttribute('data-state', 'needs-configuration'))
    const setup = /GitHub needs a one-time setup\. Configure OAuth app opens a guided settings page/

    // Nothing but the badge, the value prop, Documentation and the action row
    // renders inline: the explanation is not in the document until asked for.
    expect(screen.queryByText(setup)).not.toBeInTheDocument()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    // The tip sits in the action row, immediately before the Configure link.
    const icon = within(github).getByRole('button', { name: 'GitHub prerequisites' })
    const configure = within(github).getByRole('link', { name: 'Configure OAuth app' })
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
    expect(within(github).queryByRole('link', { name: 'Configure OAuth app' })).not.toBeInTheDocument()
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
    expect(within(github).queryByRole('link', { name: 'Configure OAuth app' })).not.toBeInTheDocument()
  })

  it('marks only providers with a blocking prerequisite, as an icon beside Connect', async () => {
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    for (const provider of CONNECTION_PROVIDERS) {
      const icon = within(card(provider.slug)).queryByRole('button', { name: `${provider.name} prerequisites` })
      if (provider.prerequisite_copy) expect(icon).toBeInTheDocument()
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

describe('the two tabs', () => {
  it('switches panels on click and on arrow keys', async () => {
    mount()
    const services = screen.getByRole('tab', { name: /Services/ })
    const mcp = screen.getByRole('tab', { name: /MCP Servers/ })
    expect(services).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(mcp)
    expect(mcp).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('button', { name: 'deep link to stripe' })).toBeInTheDocument()
    expect(cards()).toHaveLength(0)

    // Either arrow toggles: there are only two tabs, so direction is irrelevant.
    fireEvent.keyDown(mcp, { key: 'ArrowLeft' })
    await waitFor(() => expect(screen.getByRole('tab', { name: /Services/ })).toHaveAttribute('aria-selected', 'true'))
    fireEvent.keyDown(screen.getByRole('tab', { name: /Services/ }), { key: 'ArrowRight' })
    await waitFor(() => expect(screen.getByRole('tab', { name: /MCP Servers/ })).toHaveAttribute('aria-selected', 'true'))
  })

  it('ignores keys that are not the arrows it owns', async () => {
    mount()
    const services = screen.getByRole('tab', { name: /Services/ })

    fireEvent.keyDown(services, { key: 'ArrowDown' })
    fireEvent.keyDown(services, { key: 'a' })

    expect(services).toHaveAttribute('aria-selected', 'true')
  })

  it('deep-links from the MCP table back to the highlighted provider card', async () => {
    mount()
    fireEvent.click(screen.getByRole('tab', { name: /MCP Servers/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'deep link to stripe' }))

    await waitFor(() => expect(screen.getByRole('tab', { name: /Services/ })).toHaveAttribute('aria-selected', 'true'))
    const stripe = await waitFor(() => card('stripe'))
    expect(stripe.className).toContain('border-accent')
    // Only the deep-linked card is highlighted.
    expect(card('notion').className).not.toContain('border-accent')
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
      expect(i18next.t(`pages.connectionsPage.prerequisite_${provider.slug}`)).toBe(provider.prerequisite_copy)
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

    const testing = await screen.findByRole('button', { name: 'Testing…' })
    expect(testing).toBeDisabled()
    expect(within(card('notion')).getByRole('button', { name: /Disconnect/ })).toBeDisabled()

    pending.resolve({ verdict: 'usable' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled())
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

    pending.resolve({ verdict: 'usable' })
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
    // revoke -- and it under-reports when more than one did.
    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent(
      'The stored grant was kept because the same endpoint is also configured by: notion-work, notion-personal. Revoking at the provider would cut off their access too.',
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
