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
const connectionsStatus = vi.fn()
const connectionsCancel = vi.fn()

vi.mock('../api/client', () => ({
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
    connectionsStatus: (...a: unknown[]) => connectionsStatus(...a),
    connectionsCancel: (...a: unknown[]) => connectionsCancel(...a),
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
import { createTestStore, renderWithProviders } from './helpers'

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
  { servicesEnabled = true, chat = {} }: { servicesEnabled?: boolean; chat?: ChatSeed } = {},
) {
  const store = createTestStore({
    chat: {
      messages: chat.messages ?? [],
      slotMessages: chat.slotMessages ?? {},
    } as unknown as RootState['chat'],
  })
  return renderWithProviders(<ConnectionsPage servicesEnabled={servicesEnabled} />, { store })
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
  // Authorization axis: empty by default, so the reachability-derived card states
  // these tests assert on are unchanged by the status feed.
  connectionsStatus.mockReset().mockResolvedValue({ schema_version: 1, connections: [] })
  connectionsCancel.mockReset().mockResolvedValue({ ok: true, slug: 'notion', dropped: true })
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

describe('the provider gallery', () => {
  it('renders one card per launch-gated provider and withholds the rest', async () => {
    mount()

    await waitFor(() => expect(cards()).toHaveLength(CONNECTION_PROVIDERS.length))
    expect(screen.getByRole('heading', { name: 'Notion' })).toBeInTheDocument()
    // GitHub is in the registry but has not passed the launch gate.
    expect(screen.queryByRole('heading', { name: 'GitHub' })).not.toBeInTheDocument()
    expect(screen.getByText(`${CONNECTION_PROVIDERS.length} available`)).toBeInTheDocument()
  })

  it('shows an unconnected provider its docs link and a Connect button', async () => {
    mount()

    const notion = await waitFor(() => card('notion'))
    expect(notion).toHaveAttribute('data-state', 'not-connected')
    expect(within(notion).getByText('Search your Notion workspace and read pages and databases.')).toBeInTheDocument()
    expect(within(notion).getByRole('link', { name: /Documentation/ })).toHaveAttribute('target', '_blank')
    expect(within(notion).getByRole('button', { name: 'Connect' })).toBeEnabled()
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

  it('confirms a healthy probe as success feedback', async () => {
    mcpServers.mockResolvedValue(connected)
    mcpProbe.mockResolvedValue(connected)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    expect(await screen.findByText('Connection is healthy.')).toBeInTheDocument()
    expect(mcpProbe).toHaveBeenCalled()
  })

  it('surfaces a failing probe as an error on the card', async () => {
    mcpServers.mockResolvedValue(connected)
    mcpProbe.mockResolvedValue([server({ status: 'error' })])
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    const failure = await screen.findByRole('alert')
    expect(failure).toHaveTextContent('Action failed: The provider did not pass the connection test.')
  })

  it('shows the busy label while the probe is in flight', async () => {
    const pending = deferred<McpServer[]>()
    mcpServers.mockResolvedValue(connected)
    mcpProbe.mockReturnValue(pending.promise)
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: 'Test' })))

    const testing = await screen.findByRole('button', { name: 'Testing…' })
    expect(testing).toBeDisabled()
    expect(within(card('notion')).getByRole('button', { name: /Disconnect/ })).toBeDisabled()

    pending.resolve(connected)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled())
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
    expect(mcpApply).toHaveBeenCalledWith([{ name: 'notion', uninstall: true }])
  })

  it('reports a failed disconnect as an error instead of claiming success', async () => {
    mcpServers.mockResolvedValue(connected)
    mcpApply.mockRejectedValue(new Error('config is read-only'))
    mount()

    fireEvent.click(await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ })))

    expect(await screen.findByRole('alert')).toHaveTextContent('Action failed: config is read-only')
  })

  it('names an unknown thrown value rather than rendering "undefined"', async () => {
    mcpServers.mockResolvedValue(connected)
    mcpApply.mockRejectedValue('not an Error')
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
    expect(screen.getByText('Finish approving in your browser…')).toBeInTheDocument()
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
    expect(mcpApply).not.toHaveBeenCalledWith([{ name: 'notion', uninstall: true }])
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
    expect(mcpApply).not.toHaveBeenCalledWith([{ name: 'notion', uninstall: true }])
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
    expect(mcpApply).not.toHaveBeenCalledWith([{ name: 'notion', uninstall: true }])
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
    expect(mcpApply).not.toHaveBeenCalledWith([{ name: 'notion', uninstall: true }])
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
