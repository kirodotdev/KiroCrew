// Render-level coverage of the Connections page's connect flow: installing a
// provider, the mint and its approval link, waiting for approval, relaying the
// loopback return address, the authorization status feed and the approval tab.
// ConnectionsPage.coverage.test.tsx covers the rest of the page, and its header
// explains the seams. The api mock, the McpTab stub and the beforeEach below are
// copied from it word for word; the shared fixtures are in
// ConnectionsPage.coverageTestUtils.tsx.
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

import {
  NOTION_URL,
  server,
  banner,
  mount,
  card,
  deferred,
} from './ConnectionsPage.coverageTestUtils'

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

  it('shows a proactive shared-grant note when the grant is shared', async () => {
    mcpServers.mockResolvedValue([server({ status: 'ok' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{
        slug: 'notion',
        status: 'connected',
        grantPresent: true,
        grantSharedWith: ['atlassian'],
      }],
    })
    mount()

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
    // The sharer fact and the disconnect/reconnect caveat BOTH ride in the one
    // Disconnect-side caveat tip (connection-card-caveat-tip), never an
    // always-visible band: nothing shared-grant renders inline until the tip
    // is opened.
    expect(within(card('notion')).queryByText(/shares its stored authorization/i)).not.toBeInTheDocument()
    const icon = await waitFor(() =>
      within(card('notion')).getByRole('button', { name: 'Notion prerequisites' }),
    )
    fireEvent.mouseEnter(icon)
    const bubble = screen.getByRole('tooltip')
    expect(within(bubble).getByText('Before you connect')).toBeInTheDocument()
    expect(bubble).toHaveTextContent(/shares its stored authorization with: atlassian/i)
    // and the caveat about what Disconnect does, in the same bubble
    expect(bubble).toHaveTextContent(/reconnecting reuses it/i)
  })

  it('hedges the shared-grant note when the census is incomplete', async () => {
    mcpServers.mockResolvedValue([server({ status: 'ok' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{
        slug: 'notion',
        status: 'connected',
        grantPresent: true,
        grantSharedUnconfirmed: true,
      }],
    })
    mount()

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
    const icon = await waitFor(() =>
      within(card('notion')).getByRole('button', { name: 'Notion prerequisites' }),
    )
    fireEvent.mouseEnter(icon)
    const bubble = screen.getByRole('tooltip')
    expect(bubble).toHaveTextContent(/could not be read to confirm/i)
  })

  it('shows no shared-grant tip for a solely-owned connected grant', async () => {
    mcpServers.mockResolvedValue([server({ status: 'ok' })])
    connectionsStatus.mockResolvedValue({
      schema_version: 1,
      connections: [{ slug: 'notion', status: 'connected', grantPresent: true }],
    })
    mount()

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'connected'))
    expect(within(card('notion')).queryByText(/shares its stored authorization/i)).not.toBeInTheDocument()
    // No sharer and no hedge: the Disconnect-side caveat triangle is absent too.
    expect(within(card('notion')).queryByRole('button', { name: 'Notion prerequisites' })).not.toBeInTheDocument()
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
    // flake-ok: pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
    // nosemgrep: semgrep.kirocrew.test-promise-sleep -- pre-existing sleep barrier, moved verbatim; tracked for a de-flake pass
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
