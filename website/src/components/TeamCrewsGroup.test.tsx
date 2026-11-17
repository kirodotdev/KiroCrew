/**
 * The desktop app's relay Crew list as the Crew switcher's Team Crews group:
 * hidden unless the desktop bridge exists, the crews route answers and it lists
 * a Crew or a refusal (sign-in and load failures stay on screen), offline
 * Crews shown greyed (not hidden), each typed refusal shown as its plain text,
 * and Open handing the shell the endpoint ID only.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, screen, waitFor, cleanup, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, ApiError, type InstanceView, type RelayCrewEntry, type SsoStatus } from '../api/client'
import TeamCrewsSwitcher from './TeamCrewsGroup'
import { setCrewPins, setStableOrder } from './InstanceTabBar'
import { RELAY_VIEWER_CREWS_KEY, relayRefusalText, relayRetryable } from '../hooks/useRelayViewerCrews'
import { timeAgo } from '../utils/timeAgo'
import { renderWithProviders, createTestStore } from '../test/helpers'

const crew = (over: Partial<RelayCrewEntry>): RelayCrewEntry => ({
  endpointId: 'ep-1',
  name: 'Build box',
  crewType: 'PERSONAL',
  presence: 'CONNECTED',
  relayConnectionStatus: 'CONNECTED',
  crewUrl: 'https://ep-1.relay.example',
  ...over,
})

const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const listResp = (instances: InstanceView[]) => ({ active: true, instances, warm_set_cap: 5, sso: okSso })
const sshCrew = (over: Partial<InstanceView> = {}): InstanceView => ({
  id: 'cd-1',
  name: 'Cloud One',
  ssh_host: 'cd-1-alias',
  remote_port: 7777,
  local_port: 7778,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
  ...over,
})

function mount(instances: InstanceView[] = [], unread: Record<string, number> = {}) {
  vi.spyOn(api, 'listInstances').mockResolvedValue(listResp(instances))
  const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread } })
  const r = renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
  return r.queryClient
}

function withBridge(open = vi.fn(async (_id: string) => ({ ok: true }))) {
  ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open }
  return open
}

/** Open the switcher's menu and return the Team Crews group. */
async function openGroup(u: ReturnType<typeof userEvent.setup>) {
  await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
  return screen.findByTestId('team-crews-group')
}

const row = (name: string) => screen.getByRole('menuitem', { name: `Open ${name}` })

beforeEach(() => {
  localStorage.clear()
  setCrewPins([])
  setStableOrder(false)
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
  vi.restoreAllMocks()
  delete (window as { relayCrewAPI?: unknown }).relayCrewAPI
})

describe('TeamCrewsGroup', () => {
  it('reads nothing and leaves the bar as today (no remote Crews: no bar) without the desktop bridge', async () => {
    const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('leaves the bar as today while the crews route answers 404', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(404, 'off', JSON.stringify({ error: 'off', code: 'RELAY_VIEWER_OFF' })),
    )
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
  })

  it('with the viewer off, a menu of Local and SSH Crews has no group names and no Team Crews', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(404, 'off', '{}'))
    mount([sshCrew({ was_connected: true })])
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
    await screen.findByRole('menuitemradio', { name: /Cloud One/ })
    expect(screen.queryByText('This computer')).toBeNull()
    expect(screen.queryByText('My Crews')).toBeNull()
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
  })

  it('leaves the bar as today when the viewer lists no Team Crews and there are no SSH Crews', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('success'))
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
  })

  it('with no Team Crews listed, a menu of Local and SSH Crews has no group names and no Team Crews', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    const client = mount([sshCrew({ was_connected: true })])
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('success'))
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
    await screen.findByRole('menuitemradio', { name: /Cloud One/ })
    expect(screen.queryByText('This computer')).toBeNull()
    expect(screen.queryByText('My Crews')).toBeNull()
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
    expect(screen.queryByText('No remote Crews to open.')).toBeNull()
  })

  it('shows the bar with no SSH Crews once the viewer lists Team Crews, under group names', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    const u = userEvent.setup()
    const group = await openGroup(u)
    expect(screen.getByText('This computer')).toBeTruthy()
    expect(within(group).getByText('Team Crews')).toBeTruthy()
    // The Local row keeps its name; "This computer" is the group's name only.
    expect(screen.getByRole('menuitemradio', { name: /Local/ })).toBeTruthy()
    // Team rows are plain items, not destinations: no radio, no pin.
    expect(within(group).queryAllByRole('menuitemradio')).toHaveLength(0)
    expect(within(group).queryAllByRole('menuitemcheckbox')).toHaveLength(0)
    expect(within(group).getByTitle('Opens in its own window')).toBeTruthy()
  })

  it('names My Crews only when SSH Crews are listed', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount([sshCrew({ was_connected: true })])
    const u = userEvent.setup()
    await openGroup(u)
    expect(screen.getByText('My Crews')).toBeTruthy()
  })

  it('shows the bar with the Team Crews group while the instances feature is off', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    vi.spyOn(api, 'listInstances').mockRejectedValue(
      new ApiError(403, 'Instances feature is disabled', JSON.stringify({ error: 'Instances feature is disabled', code: 'instances_disabled' })),
    )
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    const client = renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store }).queryClient
    await waitFor(() => expect(client.getQueryState(['instances'])?.status).toBe('error'))
    const u = userEvent.setup()
    const group = await openGroup(u)
    expect(within(group).getByRole('menuitem', { name: 'Open Build box' })).toBeTruthy()
  })

  it("leaves Team Crews out of the caret's unread count", async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })] })
    mount([sshCrew({ was_connected: true })], { 'cd-1': 2 })
    const u = userEvent.setup()
    // The Team rows must be on screen first, or the count could pass before they exist.
    const group = await openGroup(u)
    expect(within(group).getAllByRole('menuitem', { name: /^Open / })).toHaveLength(2)
    await u.keyboard('{Escape}')
    expect(await screen.findByRole('button', { name: 'Switch crew, 2 unread elsewhere' })).toBeTruthy()
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
    const group = await openGroup(userEvent.setup())
    const rows = within(group).getAllByTestId('relay-crew-row')
    expect(rows.map(r => r.dataset.online)).toEqual(['true', 'false'])
    expect(within(rows[1]).getByText(/Team · offline/)).toBeTruthy()
    expect(rows[1].className).toContain('opacity-40')
    expect(rows[0].className).not.toContain('opacity-40')
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
    const u = userEvent.setup()
    await openGroup(u)
    expect(row('Up').getAttribute('aria-disabled')).toBeNull()
    expect(row('Down').getAttribute('aria-disabled')).toBe('true')
    expect(row('Tunnel down').getAttribute('aria-disabled')).toBe('true')
    expect(row('Late').getAttribute('aria-disabled')).toBeNull()
    expect(row('Down').title).toBe('This Crew is offline.')
    await u.click(row('Tunnel down'))
    await u.click(row('Down'))
    expect(open).not.toHaveBeenCalled()
    // An offline row stays reachable, so it is said, not skipped.
    expect(screen.getByTestId('team-crews-group')).toBeTruthy()
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
    const group = await openGroup(userEvent.setup())
    const status = within(group).getAllByTestId('relay-crew-row')
      .map(r => r.querySelector('.text-\\[12px\\]')?.textContent)
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

  it('Open sends only the endpoint ID to the desktop bridge, then closes the menu', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' })] })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Lab'))
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    expect(open.mock.calls[0]).toEqual(['ep-9'])
    await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
  })

  it.each([
    ['CREW_ACCESS_NOT_GRANTED', 'You no longer have access to this Crew.'],
    ['ACCESS_DENIED', "Couldn't open this Crew."],
    ['REMOTE_ACCESS_DISABLED', 'Remote access is turned off for this account.'],
    ['CREW_OFFLINE', 'This Crew is offline.'],
    ['THROTTLED', 'Too many requests. Try again in a minute.'],
    ['SIGNED_OUT', 'Sign in to Kiro to see your Team Crews.'],
    ['NOT_SET_UP', "Team Crews over the relay aren't set up."],
    ['INVALID_REQUEST', "Couldn't open this Crew."],
    ['SERVICE_ERROR', "Couldn't open this Crew."],
    ['UNREACHABLE', "Couldn't open this Crew."],
    ['BAD_ANSWER', "Couldn't open this Crew."],
    ['INVALID_ENDPOINT', "Couldn't open this Crew."],
    ['UNAVAILABLE', "Couldn't open this Crew."],
  ])('keeps the menu open and shows the plain text for an Open refused with %s', async (code, text) => {
    withBridge(vi.fn(async () => ({ ok: false, error: code })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Build box'))
    expect((await screen.findByTestId('relay-crew-refusal')).textContent).toContain(text)
    expect(screen.getByTestId('team-crews-group')).toBeTruthy()
  })

  it('offers Ask the agent as a menu item tied to a row refusal, one per refused row', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'SERVICE_ERROR' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })],
    })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Build box'))
    await u.click(row('Two'))
    const items = await screen.findAllByRole('menuitem', { name: 'Ask the agent' })
    expect(items.map(i => i.getAttribute('aria-describedby'))).toEqual(['relay-crew-refusal-ep-1', 'relay-crew-refusal-ep-2'])
    for (const id of ['relay-crew-refusal-ep-1', 'relay-crew-refusal-ep-2']) {
      expect(document.getElementById(id)?.textContent).toContain("Couldn't open this Crew.")
    }
  })

  it('offers Ask the agent as a menu item tied to the list refusal', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    await openGroup(userEvent.setup())
    const item = await screen.findByRole('menuitem', { name: 'Ask the agent' })
    expect(item.getAttribute('aria-describedby')).toBe('relay-crews-list-refusal')
    expect(document.getElementById('relay-crews-list-refusal')?.textContent).toContain("Couldn't load your Team Crews.")
  })

  it('arrow keys skip the refusal notice under a row and reach its Ask the agent item', async () => {
    withBridge(vi.fn(async () => ({ ok: false, error: 'SERVICE_ERROR' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })],
    })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Build box'))
    const handoff = await screen.findByRole('menuitem', { name: 'Ask the agent' })
    expect(document.activeElement).toBe(row('Build box'))
    await u.keyboard('{ArrowDown}')
    expect(document.activeElement).toBe(handoff)
    await u.keyboard('{ArrowDown}')
    expect(document.activeElement).toBe(row('Two'))
    await u.keyboard('{ArrowUp}{ArrowUp}')
    expect(document.activeElement).toBe(row('Build box'))
  })

  it('shows a list-level refusal in place of the rows', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(401, 'signed out', JSON.stringify({ error: 'signed out', code: 'SIGNED_OUT' })),
    )
    mount()
    await openGroup(userEvent.setup())
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain('Sign in to Kiro to see your Team Crews.')
    expect(screen.queryByTestId('relay-crew-row')).toBeNull()
  })

  it('shows a load failure, not an Open failure, when the list read fails with no known code', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    await openGroup(userEvent.setup())
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain("Couldn't load your Team Crews.")
  })

  const refused = (status: number, body: Record<string, unknown>) =>
    new ApiError(status, 'refused', JSON.stringify({ error: 'refused', ...body }))

  it('shows the request ID and a Try again that reads the list again', async () => {
    withBridge()
    const spy = vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-0000-example' }))
      .mockResolvedValueOnce({ crews: [crew({})] })
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    const details = await screen.findByTestId('relay-crews-list-details')
    expect(details.textContent).toContain('Request ID: req-0000-example')
    await u.click(screen.getByRole('menuitem', { name: 'Try again' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('relay-crew-row')).toBeTruthy()
  })

  it('shows no request ID when the refusal has none', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(502, { code: 'SERVICE_ERROR' }))
    mount()
    await openGroup(userEvent.setup())
    await screen.findByTestId('relay-crews-list-refusal')
    expect(screen.queryByTestId('relay-crews-list-details')).toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Try again' })).toBeTruthy()
  })

  it.each(['CREW_ACCESS_NOT_GRANTED', 'REMOTE_ACCESS_DISABLED', 'ACCESS_DENIED', 'SIGNED_OUT', 'NOT_SET_UP', 'INVALID_REQUEST'])(
    'offers no Try again for %s, where the same request fails again',
    async code => {
      withBridge()
      vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(refused(403, { code, requestId: 'req-0000-example' }))
      mount()
      await openGroup(userEvent.setup())
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
    await openGroup(userEvent.setup({ advanceTimers: vi.advanceTimersByTime }))
    const retry = await screen.findByTestId('relay-crews-list-retry')
    expect(retry.getAttribute('aria-disabled')).toBe('true')
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(retry.getAttribute('aria-disabled')).toBeNull()
  })

  it('keeps Try again off while the list is being read again', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR' }))
      .mockReturnValueOnce(new Promise<never>(() => {}))
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    const retry = await screen.findByTestId('relay-crews-list-retry')
    expect(retry.getAttribute('aria-disabled')).toBeNull()
    await u.click(retry)
    await waitFor(() => expect(screen.getByTestId('relay-crews-list-retry').getAttribute('aria-disabled')).toBe('true'))
    // The refusal Try again was clicked from stays up during its own read.
    expect(screen.getByTestId('relay-crews-list-refusal').textContent).toContain("Couldn't load your Team Crews.")
  })

  it('does not bring back an older request ID on a later read once a retry failed again', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-old' }))
      .mockRejectedValueOnce(refused(502, { code: 'SERVICE_ERROR', requestId: 'req-new' }))
      .mockReturnValueOnce(new Promise<never>(() => {}))
    const client = mount()
    const u = userEvent.setup()
    await openGroup(u)
    expect((await screen.findByTestId('relay-crews-list-details')).textContent).toContain('req-old')
    await u.click(screen.getByTestId('relay-crews-list-retry'))
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
