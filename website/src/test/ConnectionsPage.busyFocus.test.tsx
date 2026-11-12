// Keyboard-focus retention for the Connections card action buttons that were
// still natively `disabled` while busy. A native `disabled` takes a control out
// of the tab order, so a real browser drops the focus of the keyboard user who
// just pressed it; jsdom keeps focus on a disabled button, so the tab-order half
// is pinned on the ATTRIBUTE (aria-disabled, not disabled) the way the Test
// button's coverage does. The buttons split two ways:
//
//   - Disconnect and the return-address relay keep their card mounted while they
//     run (a failed disconnect stays connected; the relay stays waiting), so they
//     stay focusable in place and refuse a second press.
//   - Connect/Reconnect UNMOUNT when the mint moves the card to
//     waiting-for-approval, so focus is handed to that state's Cancel.
//
// The api mock, the McpTab stub and the beforeEach are copied from
// ConnectionsPage.coverage.test.tsx, whose header explains the seams.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, waitFor, within, act } from '@testing-library/react'

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
  default: ({ onManagedProviderClick }: { onManagedProviderClick: (slug: string) => void }) => (
    <button type="button" onClick={() => onManagedProviderClick('stripe')}>deep link to stripe</button>
  ),
}))

import { server, mount, card, deferred } from './ConnectionsPage.coverageTestUtils'

const connected = [server({ accountLabel: 'ada@example.com' })]
// `status: 'unknown'` renders the waiting-for-approval state, where the
// return-address relay and its Complete button live.
const waiting = [server({ status: 'unknown' })]

beforeEach(() => {
  mcpServers.mockReset().mockResolvedValue([])
  mcpProbe.mockReset().mockResolvedValue([])
  mcpApply.mockReset().mockResolvedValue({ ok: true })
  mcpCustomAdd.mockReset().mockResolvedValue({ ok: true, added: [], enabled: true })
  mcpCustomGet.mockReset().mockResolvedValue({
    name: 'notion',
    spec: { url: 'https://old.example/mcp', scopes: ['read'], clientId: 'client-1' },
    enabled: true,
  })
  mcpCustomUpdate.mockReset().mockResolvedValue({ ok: true, name: 'notion' })
  mcpOAuthRelay.mockReset().mockResolvedValue({ ok: true })
  connectionsMint.mockReset().mockResolvedValue({ ok: true, slug: 'notion', state: 'minting', token: 'tok1' })
  connectionsMintState.mockReset().mockResolvedValue({ slug: 'notion', state: 'minting', token: 'tok1' })
  connectionsPremint.mockReset().mockResolvedValue({ ok: true, preminting: ['notion'] })
  connectionsStatus.mockReset().mockResolvedValue({ schema_version: 1, connections: [] })
  connectionsCancel.mockReset().mockResolvedValue({ ok: true, slug: 'notion', dropped: true })
  connectionsDisconnect.mockReset().mockResolvedValue({
    ok: true, grantRemoved: true, grantSurviving: [], entryRemoved: true, grantSharedWith: [],
  })
  connectionsTest.mockReset().mockResolvedValue({
    schema_version: 1, slug: 'notion', verdict: 'usable', code: 'tools_available', toolCount: 2,
  })
})

describe('Connections card buttons keep keyboard focus while busy', () => {
  it('keeps focus on the pressed Disconnect while it runs, and refuses a second press', async () => {
    const pending = deferred<unknown>()
    mcpServers.mockResolvedValue(connected)
    connectionsDisconnect.mockReturnValue(pending.promise)
    mount()

    const disconnect = await waitFor(() => within(card('notion')).getByRole('button', { name: /Disconnect/ }))
    disconnect.focus()
    fireEvent.click(disconnect)

    // aria-disabled, not disabled, so the button keeps its place in the tab
    // order; jsdom does not drop focus from a disabled button, so the tab-order
    // half is pinned on the attribute itself.
    await waitFor(() => expect(disconnect).toHaveAttribute('aria-disabled', 'true'))
    expect(disconnect).not.toBeDisabled()
    expect(disconnect).toHaveAttribute('aria-busy', 'true')
    expect(disconnect).toHaveFocus()

    // A second press while the first disconnect is in flight is refused.
    fireEvent.click(disconnect)
    expect(connectionsDisconnect).toHaveBeenCalledTimes(1)

    await act(async () => { pending.resolve({ ok: true, grantRemoved: true, grantSurviving: [], entryRemoved: true, grantSharedWith: [] }) })
  })

  it('keeps focus on the pressed Complete connection while the relay runs, and keeps the address field focusable', async () => {
    const pending = deferred<unknown>()
    mcpServers.mockResolvedValue(waiting)
    mcpOAuthRelay.mockReturnValue(pending.promise)
    mount()

    const input = await waitFor(() => within(card('notion')).getByLabelText('Return address'))
    fireEvent.change(input, { target: { value: 'http://127.0.0.1:4321/?code=one-time' } })
    const complete = within(card('notion')).getByRole('button', { name: 'Complete' })
    complete.focus()
    fireEvent.click(complete)

    await waitFor(() => expect(mcpOAuthRelay).toHaveBeenCalledTimes(1))
    const relaying = within(card('notion')).getByRole('button', { name: 'Sending…' })
    expect(relaying).toBe(complete)
    expect(relaying).not.toBeDisabled()
    expect(relaying).toHaveAttribute('aria-disabled', 'true')
    expect(relaying).toHaveAttribute('aria-busy', 'true')
    expect(relaying).toHaveFocus()
    // The address field stays focusable (readOnly, not disabled) so a keyboard
    // user who submitted it is not dropped either.
    expect(input).not.toBeDisabled()
    expect(input).toHaveAttribute('readonly')

    // A second submit while relaying is refused.
    fireEvent.click(relaying)
    expect(mcpOAuthRelay).toHaveBeenCalledTimes(1)

    await act(async () => { pending.resolve({ ok: true }) })
  })

  it('hands focus to Cancel when Connect unmounts into waiting-for-approval', async () => {
    mount()

    const connect = await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' }))
    connect.focus()
    fireEvent.click(connect)

    // The mint moves the card to waiting-for-approval, unmounting Connect. Focus
    // must land on that state's primary action rather than falling to the body.
    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    const cancel = within(card('notion')).getByRole('button', { name: /Cancel/ })
    await waitFor(() => expect(cancel).toHaveFocus())
  })

  it('does not consume the handoff while Cancel is still disabled by the in-flight connect, then lands it once busy clears', async () => {
    // The card renders waiting-for-approval the moment locallyWaiting is set,
    // which is BEFORE the connect's busy clears (busy clears only after the
    // awaited post-mint refetch). While busy==='connect', Cancel is natively
    // disabled, so focusing it then would be a no-op; the handoff must wait for
    // busy to clear rather than consuming itself on a disabled target. Holding
    // the post-mint mcp-servers refetch pending reproduces that window.
    const refetch = deferred<unknown>()
    let calls = 0
    mcpServers.mockImplementation(() => {
      calls += 1
      // First load resolves (empty gallery); the post-connect refetch is held so
      // busy stays 'connect' through the waiting-for-approval render.
      return calls <= 1 ? Promise.resolve([]) : refetch.promise
    })
    mount()

    const connect = await waitFor(() => within(card('notion')).getByRole('button', { name: 'Connect' }))
    connect.focus()
    fireEvent.click(connect)

    await waitFor(() => expect(card('notion')).toHaveAttribute('data-state', 'waiting-for-approval'))
    const cancel = within(card('notion')).getByRole('button', { name: /Cancel/ })
    // Still mid-connect: Cancel is disabled, so the handoff must NOT have fired
    // (focusing a disabled button does nothing, and the ref must survive to retry).
    expect(cancel).toBeDisabled()
    expect(cancel).not.toHaveFocus()

    // Release the refetch: busy clears, Cancel enables, and the pending handoff lands.
    await act(async () => { refetch.resolve([]) })
    await waitFor(() => expect(cancel).toBeEnabled())
    await waitFor(() => expect(cancel).toHaveFocus())
  })
})
