/**
 * The desktop app's relay Crew list: hidden unless the desktop bridge exists and
 * the crews route answers, offline Crews shown greyed (not hidden), each typed
 * refusal shown as its plain text, and Open handing the shell the endpoint ID only.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiError, type RelayCrewEntry } from '../api/client'
import RelayCrewsMenu from './RelayCrewsMenu'
import { RELAY_VIEWER_CREWS_KEY, relayRefusalText } from '../hooks/useRelayViewerCrews'

const crew = (over: Partial<RelayCrewEntry>): RelayCrewEntry => ({
  endpointId: 'ep-1',
  name: 'Build box',
  crewType: 'PERSONAL',
  presence: 'CONNECTED',
  relayConnectionStatus: 'CONNECTED',
  crewUrl: 'https://ep-1.relay.example',
  ...over,
})

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <RelayCrewsMenu />
    </QueryClientProvider>,
  )
  return client
}

function withBridge(open = vi.fn(async (_id: string) => ({ ok: true }))) {
  ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open }
  return open
}

async function openList() {
  fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
  return screen.findByRole('list')
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
})

describe('RelayCrewsMenu', () => {
  it('renders nothing and reads nothing when the desktop bridge is missing', () => {
    const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    expect(screen.queryByTestId('relay-crews-trigger')).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('renders nothing while the crews route answers 404', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(404, 'off', JSON.stringify({ error: 'off', code: 'RELAY_VIEWER_OFF' })),
    )
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
    expect(screen.queryByTestId('relay-crews-trigger')).toBeNull()
  })

  it('shows offline Crews greyed with "offline" rather than hiding them', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({}),
        crew({
          endpointId: 'ep-2', name: 'Team box', crewType: 'TEAM',
          presence: 'NOT_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED',
        }),
      ],
    })
    mount()
    const list = await openList()
    const rows = within(list).getAllByTestId('relay-crew-row')
    expect(rows.map(r => r.dataset.online)).toEqual(['true', 'false'])
    expect(within(rows[1]).getByText(/Team · offline/)).toBeTruthy()
    expect(within(rows[1]).getByText('Team box').closest('.opacity-50')).not.toBeNull()
    expect(within(rows[0]).getByText('Build box').closest('.opacity-50')).toBeNull()
  })

  it("dims an offline row's text but not its Open button, which shows only its own disabled style", async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [crew({ endpointId: 'ep-2', name: 'Laptop', relayConnectionStatus: 'NOT_CONNECTED' })],
    })
    mount()
    await openList()
    const button = screen.getByRole('button', { name: 'Open Laptop' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(button.closest('.opacity-50')).toBeNull()
    expect(screen.getByText('Laptop').closest('.opacity-50')).not.toBeNull()
  })

  it('lets a Crew be opened only while its relay connection is CONNECTED, whatever its presence', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({ endpointId: 'ep-1', name: 'Up', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED' }),
        crew({ endpointId: 'ep-2', name: 'Down', presence: 'NOT_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-3', name: 'Tunnel down', presence: 'CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-4', name: 'Late', presence: 'HEARTBEAT_LATE', relayConnectionStatus: 'CONNECTED' }),
      ],
    })
    mount()
    await openList()
    const button = (name: string) => screen.getByRole('button', { name: `Open ${name}` }) as HTMLButtonElement
    expect(button('Up').disabled).toBe(false)
    expect(button('Down').disabled).toBe(true)
    expect(button('Tunnel down').disabled).toBe(true)
    expect(button('Late').disabled).toBe(false)
    expect(button('Down').title).toBe('This Crew is offline.')
    fireEvent.click(button('Tunnel down'))
    fireEvent.click(button('Down'))
    await new Promise(r => setTimeout(r, 0))
    expect(open).not.toHaveBeenCalled()
  })

  it('Open sends only the endpoint ID to the desktop bridge', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' })] })
    mount()
    await openList()
    fireEvent.click(screen.getByRole('button', { name: 'Open Lab' }))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    expect(open.mock.calls[0]).toEqual(['ep-9'])
  })

  it.each([
    ['CREW_ACCESS_NOT_GRANTED', 'You no longer have access to this Crew.'],
    ['ACCESS_DENIED', "Couldn't open this Crew."],
    ['REMOTE_ACCESS_DISABLED', 'Remote access is turned off for this account.'],
    ['CREW_OFFLINE', 'This Crew is offline.'],
    ['THROTTLED', 'Too many requests. Try again in a minute.'],
    ['SIGNED_OUT', 'Sign in to Kiro to see your remote Crews.'],
    ['NOT_SET_UP', "Remote Crews over the relay aren't set up."],
    ['INVALID_REQUEST', "Couldn't open this Crew."],
    ['SERVICE_ERROR', "Couldn't open this Crew."],
    ['UNREACHABLE', "Couldn't open this Crew."],
    ['BAD_ANSWER', "Couldn't open this Crew."],
    ['INVALID_ENDPOINT', "Couldn't open this Crew."],
    ['UNAVAILABLE', "Couldn't open this Crew."],
  ])('shows the plain text for an Open refused with %s', async (code, text) => {
    withBridge(vi.fn(async () => ({ ok: false, error: code })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    await openList()
    fireEvent.click(screen.getByRole('button', { name: 'Open Build box' }))
    expect((await screen.findByTestId('relay-crew-refusal')).textContent).toContain(text)
  })

  it('shows a list-level refusal in place of the list', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(401, 'signed out', JSON.stringify({ error: 'signed out', code: 'SIGNED_OUT' })),
    )
    mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain('Sign in to Kiro to see your remote Crews.')
    expect(screen.queryByRole('list')).toBeNull()
  })

  it('shows a load failure, not an Open failure, when the list read fails with no known code', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain("Couldn't load your remote Crews.")
  })

  it('maps an unknown code to the generic text', () => {
    expect(relayRefusalText('something_new')).toBe("Couldn't open this Crew.")
    expect(relayRefusalText(undefined)).toBe("Couldn't open this Crew.")
    expect(relayRefusalText('crew_offline')).toBe('This Crew is offline.')
  })
})
