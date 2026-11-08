/**
 * The desktop app's relay Crew list: hidden unless the desktop bridge exists and
 * the crews route answers, offline Crews shown greyed (not hidden), each typed
 * refusal shown as its plain text, and Open handing the shell the endpoint ID only.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiError, type RelayCrewEntry } from '../api/client'
import RelayCrewsMenu from './RelayCrewsMenu'
import { RELAY_VIEWER_CREWS_KEY, relayRefusalText, relayRetryable } from '../hooks/useRelayViewerCrews'
import { timeAgo } from '../utils/timeAgo'

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
  vi.useRealTimers()
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

  it('words a row that cannot open from its presence, with last seen, and a connected row as online', async () => {
    withBridge()
    // Only Date is faked, so react-query's timers stay real.
    vi.useFakeTimers({ toFake: ['Date'] })
    const pinned = new Date('2026-01-01T12:00:00Z')
    vi.setSystemTime(pinned)
    const seen = Math.floor(pinned.getTime() / 1000) - 300
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({ endpointId: 'ep-1', name: 'Up', lastSeenAt: seen }),
        crew({
          endpointId: 'ep-2', name: 'Late', presence: 'HEARTBEAT_LATE',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: seen,
        }),
        crew({ endpointId: 'ep-3', name: 'Tunnel down', relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: null }),
        crew({
          endpointId: 'ep-4', name: 'Down', presence: 'NOT_CONNECTED',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: seen,
        }),
        crew({ endpointId: 'ep-5', name: 'New', presence: 'NEVER_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-6', name: 'Odd', presence: 'UNKNOWN', relayConnectionStatus: 'UNKNOWN' }),
      ],
    })
    mount()
    const list = await openList()
    const status = within(list).getAllByTestId('relay-crew-row')
      .map(r => r.querySelector('.text-muted')?.textContent)
    const ago = `last seen ${timeAgo(seen)}`
    expect(status).toEqual([
      'Personal · online',
      `Personal · reconnecting · ${ago}`,
      'Personal · reconnecting',
      `Personal · offline · ${ago}`,
      'Personal · offline',
      'Personal · offline',
    ])
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

  const refused = (status: number, body: Record<string, unknown>) =>
    new ApiError(status, 'refused', JSON.stringify({ error: 'refused', ...body }))

  it('shows the request ID under Details and a Try again that reads the list again', async () => {
    withBridge()
    const spy = vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-0000-example' }))
      .mockResolvedValueOnce({ crews: [crew({})] })
    mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    const details = await screen.findByTestId('relay-crews-list-details')
    expect(details.textContent).toContain('Details')
    expect(details.textContent).toContain('Request ID: req-0000-example')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('list')).toBeTruthy()
  })

  it('shows no Details when the refusal has no request ID', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(502, { code: 'SERVICE_ERROR' }))
    mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    await screen.findByTestId('relay-crews-list-refusal')
    expect(screen.queryByTestId('relay-crews-list-details')).toBeNull()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it.each(['CREW_ACCESS_NOT_GRANTED', 'REMOTE_ACCESS_DISABLED', 'ACCESS_DENIED', 'SIGNED_OUT', 'NOT_SET_UP', 'INVALID_REQUEST'])(
    'offers no Try again for %s, where the same request fails again',
    async code => {
      withBridge()
      vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(403, { code, requestId: 'req-0000-example' }))
      mount()
      fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
      await screen.findByTestId('relay-crews-list-details')
      expect(screen.queryByTestId('relay-crews-list-retry')).toBeNull()
    },
  )

  it('keeps Try again off until a throttle\'s Retry-After passes', async () => {
    // A fake clock: the 30 s wait passes only when the test moves it, so a slow
    // runner cannot reach the deadline first. shouldAdvanceTime keeps findBy and
    // react-query moving between the steps.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(429, { code: 'THROTTLED', retryAfter: 30 }))
    mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    const retry = (await screen.findByTestId('relay-crews-list-retry')) as HTMLButtonElement
    expect(retry.disabled).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(retry.disabled).toBe(false)
  })

  it('keeps Try again off while the list is being read again', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR' }))
      .mockReturnValueOnce(new Promise<never>(() => {}))
    mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    const retry = (await screen.findByTestId('relay-crews-list-retry')) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    await waitFor(() => expect(retry.disabled).toBe(true))
    // The refusal Try again was clicked from stays up during its own read.
    expect(screen.getByTestId('relay-crews-list-refusal').textContent).toContain("Couldn't load your remote Crews.")
  })

  it('does not bring back an older request ID on a later read once a retry failed again', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-old' }))
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-new' }))
      .mockReturnValueOnce(new Promise<never>(() => {}))
    const client = mount()
    fireEvent.click(await screen.findByTestId('relay-crews-trigger'))
    expect((await screen.findByTestId('relay-crews-list-details')).textContent).toContain('req-old')
    fireEvent.click(screen.getByTestId('relay-crews-list-retry'))
    await waitFor(() => expect(screen.getByTestId('relay-crews-list-details').textContent).toContain('req-new'))
    // A later read with no list loaded (the 60 s re-read, here started by hand).
    act(() => { void client.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY }) })
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.fetchStatus).toBe('fetching'))
    expect(screen.queryByText(/req-old/)).toBeNull()
  })

  it('marks only the resend-safe codes retryable', () => {
    expect(['SERVICE_ERROR', 'THROTTLED', 'UNREACHABLE', 'BAD_ANSWER', 'CREW_OFFLINE', 'UNAVAILABLE', 'service_error']
      .every(relayRetryable)).toBe(true)
    expect(['CREW_ACCESS_NOT_GRANTED', 'SIGN_IN_CHANGED', 'INVALID_ENDPOINT', 'something_new', '']
      .some(c => relayRetryable(c))).toBe(false)
    expect(relayRetryable(undefined)).toBe(false)
  })

  it('maps an unknown code to the generic text', () => {
    expect(relayRefusalText('something_new')).toBe("Couldn't open this Crew.")
    expect(relayRefusalText(undefined)).toBe("Couldn't open this Crew.")
    expect(relayRefusalText('crew_offline')).toBe('This Crew is offline.')
  })
})
