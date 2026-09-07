// First render-level coverage for the Connections page — the provider gallery
// shell that owns the four card states (not-connected → waiting-for-approval →
// connected / needs-attention), the two-tab switcher, and every write action a
// card can fire (connect, cancel, reconnect, disconnect, test, OAuth relay).
//
// Two things shape this file:
//
//   1. The gallery is GATED. `servicesEnabled` defaults to false and the panel
//      then deliberately offers zero providers, so every card test has to opt in
//      with `servicesEnabled` — that flag is the module's own seam, not a hack.
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
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { StrictMode } from 'react'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'

import type { ChatMessage, McpServer, RootState } from '../types'

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

import ConnectionsPage from '../pages/connections/ConnectionsPage'
import { CONNECTION_PROVIDERS } from '../pages/connections/registry'
import ProviderLogo, { PROVIDER_LOGO_SLUGS } from '../pages/connections/ProviderLogo'
import { i18next } from '../i18n'
import { createTestStore, renderWithProviders } from './helpers'
import { ApiError } from '../api/client'

const NOTION_URL = 'https://mcp.notion.com/mcp'
const STRIPE_URL = 'https://mcp.stripe.com'

function server(over: Partial<McpServer> = {}): McpServer {
  return {
    name: 'notion',
    command: '',
    url: NOTION_URL,
    status: 'ok',
    source: 'mcp.json',
    enabled: true,
    ...over,
  }
}

/** A gateway `mcp_oauth` banner for `serverName`, exactly as chatSlice holds it. */
function banner(serverName: string, meta: Record<string, unknown>, ts = '2026-03-04T10:00:00.000Z'): ChatMessage {
  return { role: 'mcp_oauth', content: '', cls: '', ts, meta: { server_name: serverName, ...meta } }
}

interface ChatSeed {
  messages?: ChatMessage[]
  slotMessages?: Record<string, ChatMessage[]>
}

function mount(
  { servicesEnabled = true, chat = {}, strict = false }: { servicesEnabled?: boolean; chat?: ChatSeed; strict?: boolean } = {},
) {
  const store = createTestStore({
    chat: {
      messages: chat.messages ?? [],
      slotMessages: chat.slotMessages ?? {},
    } as unknown as RootState['chat'],
  })
  const tree = <ConnectionsPage servicesEnabled={servicesEnabled} />
  return renderWithProviders(strict ? <StrictMode>{tree}</StrictMode> : tree, { store })
}

/** The card for one provider, addressed the way the DOM exposes it. */
function card(slug: string): HTMLElement {
  const el = document.getElementById(`connection-${slug}`)
  if (!el) throw new Error(`no card rendered for ${slug}`)
  return el
}

const cards = (): HTMLElement[] => Array.from(document.querySelectorAll('article[data-state]'))

/** A promise whose settlement this test controls. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
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

describe('the held-back gallery', () => {
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
  it('renders one card per launch-gated provider and withholds the rest', async () => {
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    expect(screen.getByRole('heading', { name: 'Notion' })).toBeInTheDocument()
    // GitHub is in the registry but has not passed the launch gate.
    expect(screen.queryByRole('heading', { name: 'GitHub' })).not.toBeInTheDocument()
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

describe('connecting a new provider', () => {
  it('installs the registry endpoint, probes, and moves the card to waiting', async () => {
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    await waitFor(() => expect(mcpCustomAdd).toHaveBeenCalledWith({ notion: { url: NOTION_URL } }, true))
    expect(mcpProbe).toHaveBeenCalled()
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    // jsdom grants no window, so `window.open` returns null and this click lands
    // on the refused-tab path -- where the neutral heading is the correct copy,
    // because there is no browser page to finish approving in. Asserted as an
    // absence: the neutral wording shares its text with the state badge, so a
    // positive match cannot tell the two apart. The granted-tab wording is
    // asserted in `the approval tab` below, against a stubbed open.
    expect(screen.queryByText('Finish approving in your browser…')).toBeNull()
  })

  it('asks for the approval URL instead of waiting for one', async () => {
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    // Ordered after the install: the mint activates a spec derived from the entry.
    await waitFor(() => expect(connectionsMint).toHaveBeenCalledWith('notion'))
    expect(mcpCustomAdd).toHaveBeenCalled()
  })

  it('renders the minted approval link once the mint is waiting', async () => {
    const minted = 'https://mcp.notion.com/authorize?state=minted'
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: minted })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    const link = await waitFor(() =>
      within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
    )
    expect(link).toHaveAttribute('href', minted)
  })

  it('offers no link while the mint has not produced one', async () => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    expect(within(card('notion')).queryByRole('link', { name: /Re-open approval/ })).toBeNull()
  })

  it('never enters the waiting state when the mint request is rejected', async () => {
    connectionsMint.mockRejectedValue(new Error('mint refused'))
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    // A suppressed rejection left the card spinning on a mint that was never
    // started; the failure has to reach the card's error surface instead.
    await waitFor(() => expect(screen.getByText(/mint refused/)).toBeInTheDocument())
    expect(card('notion')).not.toHaveAttribute('data-state', 'waiting-for-approval')
  })

  it.each(['failed', 'expired'] as const)('stops waiting when the mint reports %s', async state => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    connectionsMintState.mockResolvedValue({ slug: 'notion', state, reason: 'mint_timeouterror' })

    // Terminal means no URL is coming: the spinner must not outlive the mint.
    await waitFor(
      () => expect(card('notion')).not.toHaveAttribute('data-state', 'waiting-for-approval'),
      { timeout: 8000 },
    )
  }, 15000)

  it.each([
    {
      reason: 'mint_timeouterror',
      state: 'failed',
      expected: 'Action failed: Authorization setup timed out before an approval address was ready. Try connecting again.',
    },
    {
      reason: 'mint_process_gone',
      state: 'expired',
      expected: 'Action failed: The authorization process stopped before approval finished. Try connecting again.',
    },
    {
      reason: 'mint_server_absent',
      state: 'failed',
      expected: 'Action failed: The MCP server entry disappeared before authorization could start. Try connecting again.',
    },
    {
      reason: 'mint_url_rejected',
      state: 'failed',
      expected: 'Action failed: The provider returned an approval address containing credential-like data, so it was not displayed.',
    },
    {
      reason: 'mint_future_reason',
      state: 'failed',
      expected: 'Action failed: Authorization setup failed before an approval address was ready. Try connecting again.',
    },
  ])('explains $state mint reason $reason', async ({ reason, state, expected }) => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state, reason, token: 'tok1' })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    await waitFor(() => {
      expect(within(card('notion')).getByRole('alert')).toHaveTextContent(expected)
    })
  })

  it("does not surface another tab's expired mint reason", async () => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting', token: 'tok1' })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    connectionsMintState.mockResolvedValue({
      slug: 'notion', state: 'expired', reason: 'mint_process_gone', token: 'tok2',
    })

    await waitFor(
      () => expect(card('notion')).not.toHaveAttribute('data-state', 'waiting-for-approval'),
      { timeout: 8000 },
    )
    expect(within(card('notion')).queryByRole('alert')).toBeNull()
  }, 15000)

  it('probes for fresh status when the mint reports granted', async () => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    mcpProbe.mockClear()
    connectionsStatus.mockClear()
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'granted' })

    // The cached status predates consent; without a re-probe the card keeps its
    // pre-consent error after authorization succeeded.
    await waitFor(() => expect(mcpProbe).toHaveBeenCalled(), { timeout: 8000 })
    // Same staleness on the authorization axis: the 30s status poll would
    // otherwise keep serving the pre-consent verdict (grantPresent=false),
    // downgrading the just-connected card for up to a full interval.
    await waitFor(() => expect(connectionsStatus).toHaveBeenCalled(), { timeout: 8000 })
  }, 15000)

  it('clears the wait on an expired mint and keeps the entry', async () => {
    let installed = false
    const installedList = () => (installed ? [server({ status: 'unknown' })] : [])
    mcpCustomAdd.mockImplementation(async () => {
      installed = true
      return { ok: true, added: ['notion'], enabled: true }
    })
    mcpServers.mockImplementation(async () => installedList())
    mcpProbe.mockImplementation(async () => installedList())
    connectionsMint.mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 'aaa' })
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting', token: 'aaa' })
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    mcpApply.mockClear()
    const callsAtFlip = connectionsMintState.mock.calls.length
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'expired', token: 'aaa' })

    // Wait for the feed to deliver the expired row. Exactly one delivery is what
    // the effect needs -- and all it will get, since clearing the wait disables
    // the query.
    await waitFor(
      () => expect(connectionsMintState.mock.calls.length).toBeGreaterThan(callsAtFlip),
      { timeout: 8000 },
    )

    // Nothing deletes configuration on a timeout: the entry stays so the user can
    // retry with Connect or remove it with Disconnect.
    expect(connectionsDisconnect).not.toHaveBeenCalled()
    expect(installed).toBe(true)
  }, 15000)

  it('shows the connecting label while the install is in flight', async () => {
    const pending = deferred<{ ok: boolean }>()
    mcpCustomAdd.mockReturnValue(pending.promise)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))

    expect(await screen.findByRole('button', { name: 'Connecting…' })).toBeDisabled()
    pending.resolve({ ok: true })
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
  })

  it('uninstalls the just-created entry when the wait is cancelled', async () => {
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    // The probe has not surfaced the entry yet, so Cancel falls back to the slug
    // the connect just wrote — and says nothing, because the user asked for this.
    await waitFor(() => expect(mcpApply).toHaveBeenCalledWith([{ name: 'notion', uninstall: true }]))
    // Cancel must never reach the revoking endpoint: the grant is endpoint-keyed,
    // so revoking here could deauthorize a different entry at the same URL.
    expect(connectionsDisconnect).not.toHaveBeenCalled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'not-connected'))
  })

  it('cancelling a reconnect stops waiting without destroying the existing entry', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', enabled: true })])
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Reconnect/ })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'needs-attention'))
    expect(connectionsDisconnect).not.toHaveBeenCalled()
  })

  it('clears the local wait once the gateway reports the server healthy', async () => {
    const { queryClient } = mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    // The next status read is what ends the wait — nothing here polls a clock.
    mcpServers.mockResolvedValue([server()])
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
    expect(within(card('notion')).getByRole('button', { name: /Disconnect/ })).toBeInTheDocument()
  })
})

describe('waiting for approval', () => {
  const waiting = [server({ status: 'unknown' })]

  it('offers the approval link the gateway published', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount({ chat: { messages: [banner('notion', { oauth_url: 'https://notion.example/authorize?x=1' })] } })

    const link = await screen.findByRole('link', { name: /Re-open approval/ })
    expect(link).toHaveAttribute('href', 'https://notion.example/authorize?x=1')
  })

  it('refuses a non-http approval URL and keeps waiting instead of rendering it', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount({ chat: { messages: [banner('notion', { oauth_url: 'javascript:alert(1)' })] } })

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    expect(screen.queryByRole('link', { name: /Re-open approval/ })).not.toBeInTheDocument()
    expect(screen.getByText(/Waiting for the approval address/)).toBeInTheDocument()
  })

  it('keeps waiting when the banner carries no address at all', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount({ chat: { slotMessages: { 'slot-1': [banner('notion', {})] } } })

    expect(await screen.findByText(/Waiting for the approval address/)).toBeInTheDocument()
  })

  it('takes the newest banner for a server and ignores older ones', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount({
      chat: {
        slotMessages: {
          'slot-1': [banner('notion', { oauth_url: 'https://old.example/a' }, '2026-03-04T09:00:00.000Z')],
        },
        messages: [banner('notion', { oauth_url: 'https://new.example/b' }, '2026-03-04T11:00:00.000Z')],
      },
    })

    const link = await screen.findByRole('link', { name: /Re-open approval/ })
    expect(link).toHaveAttribute('href', 'https://new.example/b')
  })

  it('ignores banners that name no server', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount({ chat: { messages: [banner('   ', { oauth_url: 'https://nameless.example/a' })] } })

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    expect(screen.queryByRole('link', { name: /Re-open approval/ })).not.toBeInTheDocument()
  })

  it('marks the card connected when the banner reports the grant completed', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount({ chat: { messages: [banner('notion', { completed: true })] } })

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
  })
})

describe('relaying the loopback return address', () => {
  const waiting = [server({ status: 'unknown' })]

  const relayInput = () => within(card('notion')).getByLabelText('Return address')

  it('rejects an address that is not the loopback callback shape', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount()
    const input = await waitFor(relayInput)

    fireEvent.change(input, { target: { value: 'https://evil.example/?code=x' } })
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Complete' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Paste the full http://127.0.0.1:PORT/?code=… address from your browser.',
    )
    expect(relayInput()).toHaveAttribute('aria-invalid', 'true')
    expect(mcpOAuthRelay).not.toHaveBeenCalled()
  })

  it('rejects text that is not a URL at all', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount()
    const input = await waitFor(relayInput)

    fireEvent.change(input, { target: { value: 'pasted the wrong thing' } })
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Complete' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(mcpOAuthRelay).not.toHaveBeenCalled()
  })

  it('clears the rejection as soon as the address is edited again', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount()
    const input = await waitFor(relayInput)
    fireEvent.change(input, { target: { value: 'http://localhost:1/?code=x' } })
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Complete' }))
    await screen.findByRole('alert')

    fireEvent.change(relayInput(), { target: { value: 'http://127.0.0.1:4321/?code=one-time' } })

    expect(relayInput()).toHaveAttribute('aria-invalid', 'false')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('delivers a valid address, confirms it, and empties the field', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount()
    const input = await waitFor(relayInput)

    fireEvent.change(input, { target: { value: '  http://127.0.0.1:4321/?code=one-time  ' } })
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Complete' }))

    await waitFor(() => expect(mcpOAuthRelay).toHaveBeenCalledWith('notion', 'http://127.0.0.1:4321/?code=one-time'))
    expect(await screen.findByText('Return address delivered. Checking the connection…')).toBeInTheDocument()
    expect(relayInput()).toHaveValue('')
  })

  it('accepts Enter as the submit gesture', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount()
    const input = await waitFor(relayInput)

    fireEvent.change(input, { target: { value: 'http://[::1]:4321/callback?code=one-time' } })
    fireEvent.keyDown(relayInput(), { key: 'Enter' })

    await waitFor(() => expect(mcpOAuthRelay).toHaveBeenCalledWith('notion', 'http://[::1]:4321/callback?code=one-time'))
  })

  it('leaves other keys to the input', async () => {
    mcpServers.mockResolvedValue(waiting)
    mount()
    const input = await waitFor(relayInput)

    fireEvent.change(input, { target: { value: 'http://127.0.0.1:4321/?code=one-time' } })
    fireEvent.keyDown(relayInput(), { key: 'a' })

    expect(mcpOAuthRelay).not.toHaveBeenCalled()
  })

  it('keeps the address in the field when delivery fails, so it can be retried', async () => {
    mcpServers.mockResolvedValue(waiting)
    mcpOAuthRelay.mockRejectedValue(new Error('relay refused'))
    mount()
    const input = await waitFor(relayInput)

    fireEvent.change(input, { target: { value: 'http://127.0.0.1:4321/?code=one-time' } })
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Complete' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Action failed: relay refused')
    expect(relayInput()).toHaveValue('http://127.0.0.1:4321/?code=one-time')
  })

  it('cannot be submitted while empty, and shows the sending label in flight', async () => {
    const pending = deferred<{ ok: boolean }>()
    mcpServers.mockResolvedValue(waiting)
    mcpOAuthRelay.mockReturnValue(pending.promise)
    mount()
    const input = await waitFor(relayInput)
    expect(within(card('notion')).getByRole('button', { name: 'Complete' })).toBeDisabled()

    fireEvent.change(input, { target: { value: 'http://127.0.0.1:4321/?code=one-time' } })
    fireEvent.click(within(card('notion')).getByRole('button', { name: 'Complete' }))

    const sending = await screen.findByRole('button', { name: 'Sending…' })
    expect(sending).toBeDisabled()
    expect(relayInput()).toBeDisabled()

    pending.resolve({ ok: true })
    await waitFor(() => expect(relayInput()).toBeEnabled())
  })
})

describe('the authorization status feed', () => {
  it('disposes the backend mint when a new connect is cancelled, and still uninstalls', async () => {
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    // The mint's process, listener and spec are released by the backend...
    await waitFor(() => expect(connectionsCancel).toHaveBeenCalledWith('notion', 'tok1'))
    // ...and the entry this connect created is still removed, unchanged.
    await waitFor(() => expect(mcpApply).toHaveBeenCalledWith([{ name: 'notion', uninstall: true }]))
    expect(connectionsDisconnect).not.toHaveBeenCalled()
  })

  it('disposes the mint on a cancelled reconnect without destroying the entry', async () => {
    mcpServers.mockResolvedValue([server({ status: 'error', enabled: true })])
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Reconnect/ })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    // This is what main leaked: a cancelled reconnect dropped only the local wait
    // and left the mint held to its TTL.
    await waitFor(() => expect(connectionsCancel).toHaveBeenCalledWith('notion', 'tok1'))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'needs-attention'))
    expect(connectionsDisconnect).not.toHaveBeenCalled()
  })

  it('a failed dispose never blocks the local cancel', async () => {
    connectionsCancel.mockRejectedValue(new Error('gateway down'))
    mcpServers.mockResolvedValue([server({ status: 'error', enabled: true })])
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Reconnect/ })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'needs-attention'))
  })

  /**
   * Disposal waits on a child process shutdown, bounded only by the gateway's
   * ~10s shutdown timeout. Awaiting it would leave Cancel un-actioned and
   * re-clickable for that whole window, so the local withdrawal must not depend
   * on the dispose settling at all.
   */
  it('completes a reconnect cancel while the backend dispose is still in flight', async () => {
    const hanging = deferred<{ ok: boolean }>()
    connectionsCancel.mockReturnValue(hanging.promise)
    mcpServers.mockResolvedValue([server({ status: 'error', enabled: true })])
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Reconnect/ })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    // Never resolved: the local wait clears anyway, and the token still travelled.
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'needs-attention'))
    expect(connectionsCancel).toHaveBeenCalledWith('notion', 'tok1')
    expect(connectionsDisconnect).not.toHaveBeenCalled()
  })

  it('uninstalls a cancelled new connect while the backend dispose is still in flight', async () => {
    const hanging = deferred<{ ok: boolean }>()
    connectionsCancel.mockReturnValue(hanging.promise)
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    // The uninstall must not wait on the dispose either.
    await waitFor(() => expect(mcpApply).toHaveBeenCalledWith([{ name: 'notion', uninstall: true }]))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'not-connected'))
  })

  it('a failed uninstall after cancel never strands the waiting card', async () => {
    // The mint dies with the Cancel click, so if the wait outlived a rejected
    // uninstall no outcome could ever clear it -- the card would show a waiting
    // state with no live flow behind it. The wait must clear unconditionally.
    let installed = false
    mcpCustomAdd.mockImplementation(async () => {
      installed = true
      return { ok: true, added: ['notion'], enabled: true }
    })
    mcpServers.mockImplementation(async () => (installed ? [server({ status: 'error' })] : []))
    mcpProbe.mockImplementation(async () => (installed ? [server({ status: 'error' })] : []))
    mount()
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    mcpApply.mockRejectedValue(new Error('uninstall failed'))
    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    await waitFor(() => expect(mcpApply).toHaveBeenCalledWith([{ name: 'notion', uninstall: true }]))
    // The entry is still installed (uninstall failed) and reports an error --
    // the honest card -- but the dead wait state is gone.
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'needs-attention'))
  })

  it('renders the connected-since time the status feed reports', async () => {
    mcpServers.mockResolvedValue([server({ status: 'ok' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{
        slug: 'notion',
        status: 'connected',
        grantPresent: true,
        connectedSince: '2026-03-04T10:00:00Z',
      }],
    })
    mount()

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
    // The row appears only because a source-backed timestamp exists; nothing is
    // fabricated at render time.
    await waitFor(() => expect(within(card('notion')).getByText(/Connected since/i)).toBeInTheDocument())
  })

  it('omits connected-since when no date is recorded for a connected grant', async () => {
    mcpServers.mockResolvedValue([server({ status: 'ok' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'connected', grantPresent: true }],
    })
    mount()

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
    expect(within(card('notion')).queryByText(/Connected since/i)).not.toBeInTheDocument()
  })

  it('cancel escapes the waiting card even while the poll cached awaiting_consent', async () => {
    // The refresh-mid-consent state: no per-tab wait survives a reload, so the
    // waiting card here comes entirely from the backend's awaiting_consent
    // verdict -- and Cancel must not appear broken because the status poll
    // cached that verdict for up to 30 seconds.
    mcpServers.mockResolvedValue([server({ status: 'needs_auth' })])
    mcpProbe.mockResolvedValue([server({ status: 'needs_auth' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'awaiting_consent', grantPresent: false }],
    })
    mount()
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    // From here the backend truth is "flow disposed": the re-fetch that the
    // cancel triggers must land on the fresh verdict, not the cached one.
    connectionsStatus.mockClear()
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'not_connected', grantPresent: false }],
    })
    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

    await waitFor(() => expect(connectionsCancel).toHaveBeenCalledWith('notion', undefined))
    // Immediately out of waiting (optimistic drop of the stale cached verdict)...
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'not-verified'))
    // ...and the authorization feed re-fetched rather than waiting out the poll.
    await waitFor(() => expect(connectionsStatus).toHaveBeenCalled())
  })

  it('a stale in-flight status fetch cannot re-render waiting after cancel', async () => {
    // The fence under test: a 30s poll already in flight at click time was
    // fetched BEFORE the cancel. Unfenced, its resolution would land after the
    // optimistic drop and repopulate the stale awaiting_consent verdict.
    mcpServers.mockResolvedValue([server({ status: 'needs_auth' })])
    mcpProbe.mockResolvedValue([server({ status: 'needs_auth' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'awaiting_consent', grantPresent: false }],
    })
    const { queryClient } = mount()
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

    // Put a PRE-CANCEL fetch in flight, then make every later fetch (the
    // settlement invalidation) return the post-dispose truth.
    const stale = deferred<{ schema_version: number; connections: unknown[] }>()
    connectionsStatus.mockReturnValueOnce(stale.promise).mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'not_connected', grantPresent: false }],
    })
    void queryClient.invalidateQueries({ queryKey: ['connections-status'] })

    fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'not-verified'))

    // The stale response arrives late; the fence cancelled its query, so it
    // must be discarded rather than resurrecting the waiting card.
    stale.resolve({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'awaiting_consent', grantPresent: false }],
    })
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(card('notion')).not.toHaveAttribute('data-state', 'waiting-for-approval')
  })

  it('a reload mid-consent still loads the approval URL from the live mint', async () => {
    // The refresh-survival gap both review lanes flagged: after a reload the
    // per-tab wait map is empty, so if the mint poll keyed off it alone the
    // waiting card would render with no approval link and copy telling the
    // user to start a flow that is already running. The backend's
    // awaiting_consent verdict must feed the poll too.
    mcpServers.mockResolvedValue([server({ status: 'needs_auth' })])
    mcpProbe.mockResolvedValue([server({ status: 'needs_auth' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'awaiting_consent', grantPresent: false }],
    })
    connectionsMintState.mockResolvedValue({
      slug: 'notion',
      state: 'waiting',
      oauth_url: 'https://example.com/approve',
    })
    mount()

    // No Connect click in this tab -- the waiting card and its link come
    // entirely from backend state.
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    const link = await waitFor(() =>
      within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
    )
    expect(link).toHaveAttribute('href', 'https://example.com/approve')
  })

  it('downgrades a cached-ok card when the grant is confirmed absent', async () => {
    // The reachability probe is cached, so `ok` outlives revocation: the fresher
    // authorization fact (a CONFIRMED absent grant) must win over the stale badge.
    mcpServers.mockResolvedValue([server({ status: 'ok' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'not_connected', grantPresent: false }],
    })
    mount()

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'not-verified'))
    // The verdict is CONFIRMED at this render, so the copy must name the held
    // fact ("is not authorized"), not hedge that it cannot see the
    // authorization -- the hedge misdirects the reauthorize decision this card
    // exists to serve.
    expect(within(card('notion')).getByText(/is not authorized/)).toBeInTheDocument()
    expect(within(card('notion')).queryByText(/cannot see the authorization/)).toBeNull()
  })

  it('a failing status feed leaves the reachability-derived card intact', async () => {
    connectionsStatus.mockRejectedValue(new Error('status unavailable'))
    mcpServers.mockResolvedValue([server({ status: 'needs_auth' })])
    mount()

    // No grant fact available -> the honest pre-status verdict, not a claim.
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'not-verified'))
    // ...and with the verdict indeterminate, the HEDGE is the honest copy:
    // claiming "is not authorized" here would assert a fact nobody holds.
    expect(within(card('notion')).getByText(/cannot see the authorization/)).toBeInTheDocument()
    expect(within(card('notion')).queryByText(/is not authorized/)).toBeNull()
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

// Connect opens the approval tab. The requirement is that one click lands the
// user on the provider's consent page; the "Re-open approval" link is the
// recovery path for a tab the browser refused, not the primary route. What makes
// it delicate is the ordering: POST /api/connections/mint answers BEFORE the URL
// exists, so the tab has to be opened by the click -- while the user activation
// is still current -- and filled when the poll produces a URL.
//
// `window.open` is swapped by hand rather than with vi.spyOn so the restore is
// guaranteed by try/finally even when an assertion throws: a leaked stub would
// silently change every test that ran after it.
describe('the approval tab', () => {
  type FakeTab = { closed: boolean; location: { href: string }; close: () => void }

  const fakeTab = () => {
    const body = { style: '', text: '', setAttribute: (_: string, v: string) => { body.style = v } }
    const tab = {
      closed: false,
      location: { href: '' },
      closeCalls: 0,
      // Only the surface the card touches: a body it can style and fill, and a
      // title. Deliberately not a real DOM -- the assertion is that the card
      // writes TEXT rather than markup, which a string field states plainly.
      document: {
        title: '',
        get body() {
          return {
            setAttribute: body.setAttribute,
            set textContent(v: string) { body.text = v },
            get textContent() { return body.text },
          }
        },
      },
      body,
      close() {
        tab.closeCalls += 1
        tab.closed = true
      },
    }
    return tab
  }

  const withOpen = async (
    tab: FakeTab | ReturnType<typeof fakeTab> | null,
    body: (calls: unknown[][]) => Promise<void>,
  ): Promise<void> => {
    const original = window.open
    const calls: unknown[][] = []
    window.open = ((...args: unknown[]) => {
      calls.push(args)
      return tab as unknown as Window
    }) as typeof window.open
    try {
      await body(calls)
    } finally {
      window.open = original
    }
  }

  const clickConnect = async () => {
    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' })))
  }

  it('opens the tab on the click, before any URL exists', async () => {
    // The mint stays in `minting`, so no URL is available at any point here: the
    // tab must still have been opened, which is the whole popup-blocker fix.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    await withOpen(fakeTab(), async calls => {
      mount()
      await clickConnect()

      await waitFor(() => expect(connectionsMint).toHaveBeenCalledWith('notion'))
      expect(calls).toEqual([['', '_blank']])
    })
  })

  it('sends the approval URL to the tab the click opened', async () => {
    const minted = 'https://mcp.notion.com/authorize?state=minted'
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: minted })
    const tab = fakeTab()
    await withOpen(tab, async () => {
      mount()
      await clickConnect()

      await waitFor(() => expect(tab.location.href).toBe(minted))
      // ...and the heading may now say the browser page exists, because it does.
      expect(within(card('notion')).getByText(/Finish approving in your browser/)).toBeInTheDocument()
    })
  })

  it('leaves the link as the way in when the browser refuses the tab', async () => {
    const minted = 'https://mcp.notion.com/authorize?state=blocked'
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: minted })
    // A blocked popup is a null handle, not a throw.
    await withOpen(null, async () => {
      mount()
      await clickConnect()

      const link = await waitFor(() =>
        within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
      )
      expect(link).toHaveAttribute('href', minted)
      // No tab was granted, so the heading must NOT claim a browser page is open.
      // Asserted as an absence on purpose: the neutral heading shares its wording
      // with the state badge, so a positive match would not tell them apart.
      expect(within(card('notion')).queryByText(/Finish approving in your browser/)).toBeNull()
    })
  })

  it('reclaims the blank tab when the attempt fails', async () => {
    connectionsMint.mockRejectedValue(new Error('mint refused'))
    const tab = fakeTab()
    await withOpen(tab, async () => {
      mount()
      await clickConnect()

      // The mint never starts, so no URL will ever arrive: leaving the blank tab
      // open would make the user close it by hand.
      await waitFor(() => expect(screen.getByText(/mint refused/)).toBeInTheDocument())
      await waitFor(() => expect(tab.closeCalls).toBe(1))
    })
  })

  it('drops a tab the user closed instead of reopening it', async () => {
    const minted = 'https://mcp.notion.com/authorize?state=closed'
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'waiting', oauth_url: minted })
    const tab = fakeTab()
    await withOpen(tab, async () => {
      mount()
      await clickConnect()
      // Simulate the user closing the placeholder before the URL landed. Racing
      // the poll would make this flaky, so close it and assert on the end state:
      // whatever the ordering, the card must never resurrect a closed window.
      tab.closed = true

      const link = await waitFor(() =>
        within(card('notion')).getByRole('link', { name: /Re-open approval/ }),
      )
      expect(link).toHaveAttribute('href', minted)
    })
  })

  it('tells the user what the blank tab is for while the mint polls', async () => {
    // The mint stays in `minting`, which is the whole poll window: a tab left on a
    // bare about:blank for those seconds reads as a failure of the click.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    const tab = fakeTab()
    await withOpen(tab, async () => {
      mount()
      await clickConnect()

      await waitFor(() => expect(tab.body.text).toBe('Connecting…'))
      expect(tab.document.title).toBe('Connecting…')
      // Written as text, never markup, so a translated string cannot become nodes.
      expect(tab.body.text).not.toMatch(/[<>]/)
      // And laid out without hardcoded colours, so it cannot clash with the theme.
      expect(tab.body.style).toContain('color-scheme:light dark')
    })
  })

  it('never claims an open browser page while the tab stands refused', async () => {
    // The refused-tab case during the POLL is the gap a boolean gated on
    // `oauth.minted` could not express: minted is still false here, so the card
    // used to tell a blocked-popup user to finish in a browser page they never
    // got -- the same false claim this change set out to remove.
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    await withOpen(null, async () => {
      mount()
      await clickConnect()

      await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
      expect(within(card('notion')).queryByText(/Finish approving in your browser/)).toBeNull()
    })
  })

  it('takes the blank tab back when the user cancels mid-mint', async () => {
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    const tab = fakeTab()
    await withOpen(tab, async () => {
      mount()
      await clickConnect()
      await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))

      fireEvent.click(within(card('notion')).getByRole('button', { name: /Cancel/ }))

      // Cancel ends the attempt, so no URL is ever coming. Leaving the tab open
      // also left the ref stale, and the NEXT Connect click then overwrote it and
      // orphaned this tab for good -- so the ref being cleared is the half that
      // matters beyond tidiness.
      await waitFor(() => expect(tab.closeCalls).toBe(1))
    })
  })

  it('opens the tab for Reconnect too, not just Connect', async () => {
    // Reconnect and Authorize run the IDENTICAL mint-and-poll path through
    // `onReconnect`. Wiring the tab to the Connect button alone left two of the
    // card's three mint-starting buttons opening nothing at all.
    mcpServers.mockResolvedValue([server({ status: 'error', error: 'invalid_grant' })])
    connectionsMintState.mockResolvedValue({ slug: 'notion', state: 'minting' })
    await withOpen(fakeTab(), async calls => {
      mount()
      await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'needs-attention'))

      fireEvent.click(within(card('notion')).getByRole('button', { name: /Reconnect/ }))

      await waitFor(() => expect(connectionsMint).toHaveBeenCalledWith('notion'))
      expect(calls).toEqual([['', '_blank']])
    })
  })

  // NOT pinned here, deliberately, and stated rather than faked: the stale-outcome
  // path (a granted tab whose attempt ended, leaving `clickTab` at `open` so a
  // LATER mint re-made the browser-page claim) is only observable when the card
  // re-enters `waiting-for-approval` WITHOUT a click, because a second click sets
  // the outcome explicitly and masks it. This harness could not produce that
  // second entry in the same mount: once a URL is published the card holds the
  // waiting state through `expired`, and a fresh mount resets the state under
  // test. Driving it would need a seeded gateway-published banner arriving after a
  // completed click attempt. The fix itself is a two-line reordering -- the reset
  // moved above the ref guard, since delivery nulls the ref -- reviewed against
  // the trace that found it.
})
