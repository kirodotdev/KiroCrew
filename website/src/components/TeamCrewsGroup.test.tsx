/**
 * The desktop app's relay Crew list as the Crew switcher's Remote Crews group:
 * hidden unless the desktop bridge exists, the crews route answers and it lists
 * a Crew or a refusal (sign-in and load failures stay on screen), offline
 * Crews shown greyed (not hidden), each typed refusal shown as its plain text,
 * and Open handing the shell the endpoint ID only.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import * as React from 'react'
import { act, render, screen, waitFor, cleanup, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { Router, UNSAFE_createMemoryHistory as createMemoryHistory } from 'react-router-dom'
import { ThemeProvider } from '../hooks/useTheme'
import { api, ApiError, type InstanceView, type RelayCrewEntry, type SsoStatus } from '../api/client'
import TeamCrewsSwitcher from './TeamCrewsGroup'
import { setCrewPins, setStableOrder } from './InstanceTabBar'
import { RELAY_VIEWER_CREWS_KEY, openRelayCrewTab, relayRefusalText, relayRetryable } from '../hooks/useRelayViewerCrews'
import { relayPaneClosed, setActiveId } from '../store/instancesSlice'
import { timeAgo } from '../utils/timeAgo'
import { renderWithProviders, createTestStore } from '../test/helpers'

// A per-test stand-in for the hook's answer; null keeps the real hook's answer. The
// real hook always runs, so the other tests see exactly what they saw before.
const hookOverride = vi.hoisted(() => ({ value: null as null | Record<string, unknown> }))
vi.mock('../hooks/useRelayViewerCrews', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useRelayViewerCrews')>()
  return {
    ...actual,
    useRelayViewerCrews: () => {
      const real = actual.useRelayViewerCrews()
      return (hookOverride.value ?? real) as typeof real
    },
  }
})

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

const row = (name: string) => screen.getByRole('menuitem', { name: `Connect ${name}` })

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
  it('in a browser, Connect opens the Crew in a new tab cut from this page, at the URL the gateway gave', async () => {
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' })] })
    let answer: (v: { url: string }) => void = () => {}
    const browserOpen = vi.spyOn(api, 'relayViewerBrowserOpen').mockReturnValue(new Promise(r => { answer = r }))
    const tab = { opener: {} as unknown, close: vi.fn(), location: { replace: vi.fn() } }
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Lab'))
    // Opened inside the click, before the gateway answered.
    expect(open).toHaveBeenCalledWith('about:blank', '_blank')
    expect(tab.opener).toBeNull()
    expect(browserOpen).toHaveBeenCalledWith('ep-9')
    await act(async () => { answer({ url: 'http://kcr-abc.localhost:5476/' }) })
    expect(tab.location.replace).toHaveBeenCalledWith('http://kcr-abc.localhost:5476/')
    expect(tab.close).not.toHaveBeenCalled()
  })

  it('in a browser, a refused open closes its blank tab and says why under the row; a blocked pop-up says so', async () => {
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' })] })
    vi.spyOn(api, 'relayViewerBrowserOpen').mockRejectedValue(
      new ApiError(403, 'not local', JSON.stringify({ error: 'x', code: 'BROWSER_NOT_LOCAL' })),
    )
    const tab = { opener: {}, close: vi.fn(), location: { replace: vi.fn() } }
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    mount()
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Lab'))
    expect((await screen.findByTestId('relay-crew-refusal')).textContent).toContain('on the machine that runs this Crew')
    expect(tab.close).toHaveBeenCalled()
    expect(tab.location.replace).not.toHaveBeenCalled()
    open.mockReturnValue(null)
    await u.click(row('Lab'))
    await waitFor(() => expect(screen.getByTestId('relay-crew-refusal').textContent).toContain('blocked the new tab'))
  })

  it('in a browser, an answer that lands after a newer choice closes its tab and goes nowhere', async () => {
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    let answer: (v: { url: string }) => void = () => {}
    vi.spyOn(api, 'relayViewerBrowserOpen').mockReturnValue(new Promise(r => { answer = r }))
    const tab = { opener: {}, close: vi.fn(), location: { replace: vi.fn() } }
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    const opening = openRelayCrewTab('ep-9', 'Lab', store.dispatch)
    act(() => { store.dispatch(setActiveId(null)) })
    await act(async () => { answer({ url: 'http://kcr-abc.localhost:5476/' }) })
    expect(await opening).toEqual({ ok: true })
    expect(tab.close).toHaveBeenCalled()
    expect(tab.location.replace).not.toHaveBeenCalled()
  })

  it('in a browser, a second click while an open waits joins it: one tab, one mint', async () => {
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    let answer: (v: { url: string }) => void = () => {}
    const browserOpen = vi.spyOn(api, 'relayViewerBrowserOpen').mockReturnValue(new Promise(r => { answer = r }))
    const tab = { opener: {}, close: vi.fn(), location: { replace: vi.fn() } }
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window)
    const first = openRelayCrewTab('ep-7', 'Lab', store.dispatch)
    const second = openRelayCrewTab('ep-7', 'Lab', store.dispatch)
    await act(async () => { answer({ url: 'http://kcr-abc.localhost:5476/' }) })
    expect(await first).toEqual({ ok: true })
    expect(await second).toEqual({ ok: true })
    expect(open).toHaveBeenCalledTimes(1)
    expect(browserOpen).toHaveBeenCalledTimes(1)
    expect(tab.location.replace).toHaveBeenCalledTimes(1)
    expect(tab.close).not.toHaveBeenCalled()
  })

  it.each(['relay_local_credential', 'owner_only'])('hides the group on a page that cannot use it (403 %s)', async (code) => {
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(403, 'no', JSON.stringify({ error: 'no', code })))
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
    expect(screen.queryByRole('button', { name: /Switch crew/i })).toBeNull()
  })

  it('reads an answer with no list as an empty one, and the shell keeps running', async () => {
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({} as never)
    const client = mount()
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('success'))
    expect(client.getQueryData<{ crews: unknown[] }>(RELAY_VIEWER_CREWS_KEY)?.crews).toEqual([])
  })

  it('keeps an open Crew reachable and closable from the bar while the list read fails', async () => {
    const closePane = vi.fn(async () => ({ ok: true }))
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(), openPane: vi.fn(), closePane, onPaneEvent: () => () => {} }
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(502, 'down', JSON.stringify({ error: 'down', code: 'UNREACHABLE' })),
    )
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, relayTabs: [{ id: 'ep-9', name: 'Lab' }], relayLive: ['ep-9'], relayActive: null },
    })
    renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: /Switch crew/ }))
    expect(screen.getAllByRole('menuitemradio').some(r => r.textContent?.includes('Lab'))).toBe(true)
    await u.click(screen.getByTestId('crew-close-relay:ep-9'))
    expect(closePane).toHaveBeenCalledWith('ep-9')
  })

  it('brings back the tab of an open Crew that has since gone offline, from its row', async () => {
    const openPane = vi.fn(async () => ({ ok: true }))
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(), openPane, closePane: vi.fn(), onPaneEvent: () => () => {} }
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [crew({ endpointId: 'ep-9', name: 'Lab', relayConnectionStatus: 'DISCONNECTED' })],
    })
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({
      instances: { warm: {}, activeId: null, mru: [], unread: {}, relayTabs: [{ id: 'ep-9', name: 'Lab' }], relayLive: ['ep-9'], relayActive: null },
    })
    renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
    const u = userEvent.setup()
    await openGroup(u)
    expect(row('Lab').getAttribute('aria-disabled')).toBeNull()
    await u.click(row('Lab'))
    expect(store.getState().instances.relayActive).toBe('ep-9')
    expect(openPane).not.toHaveBeenCalled()
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
    expect(within(group).getByText('Remote Crews')).toBeTruthy()
    expect(within(group).queryByText('From your Kiro account')).toBeNull()
    expect(group.getAttribute('aria-label')).toBe('Remote Crews you can open through the relay')
    // The Local row keeps its name; "This computer" is the group's name only.
    expect(screen.getByRole('menuitemradio', { name: /Local/ })).toBeTruthy()
    // Team rows are plain items, not destinations: no radio, no pin.
    expect(within(group).queryAllByRole('menuitemradio')).toHaveLength(0)
    expect(within(group).queryAllByRole('menuitemcheckbox')).toHaveLength(0)
    // The row's one visible action is a small Connect, and only a click runs it.
    expect(within(group).getByTestId('relay-crew-connect').textContent).toBe('Connect')
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
    expect(within(group).getByRole('menuitem', { name: 'Connect Build box' })).toBeTruthy()
  })

  it("leaves Team Crews out of the caret's unread count", async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({}), crew({ endpointId: 'ep-2', name: 'Two' })] })
    mount([sshCrew({ was_connected: true })], { 'cd-1': 2 })
    const u = userEvent.setup()
    // The Team rows must be on screen first, or the count could pass before they exist.
    const group = await openGroup(u)
    expect(within(group).getAllByRole('menuitem', { name: /^Connect / })).toHaveLength(2)
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
    expect(within(rows[1]).getByText(/Shared · offline/)).toBeTruthy()
    expect(rows[1].className).toContain('opacity-40')
    expect(rows[0].className).not.toContain('opacity-40')
  })

  it('types each row Shared with a group icon or Private with a lock, and keeps the own-window arrow', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({ endpointId: 'ep-1', name: 'Payments', crewType: 'TEAM' }),
        crew({ endpointId: 'ep-2', name: 'My desk', crewType: 'PERSONAL' }),
        crew({ endpointId: 'ep-3', name: 'Odd', crewType: 'FUTURE' }),
      ],
    })
    mount()
    const group = await openGroup(userEvent.setup())
    const rows = within(group).getAllByTestId('relay-crew-row')
    const status = rows.map(r => r.querySelector('.text-\\[12px\\]')?.textContent)
    expect(status).toEqual(['Shared · online', 'Private · online', 'FUTURE · online'])
    expect(within(group).queryByText(/Personal|Team ·/)).toBeNull()
    const icon = (r: HTMLElement) => r.querySelector('[data-testid="relay-crew-type-icon"]')
    expect(icon(rows[0])?.getAttribute('class')).toContain('lucide-users')
    expect(icon(rows[1])?.getAttribute('class')).toContain('lucide-lock')
    // A type this build does not know keeps its own text and gets no icon.
    expect(icon(rows[2])).toBeNull()
    // The own-window arrow is unchanged on every row, and no row gains a pin.
    for (const r of rows) {
      const arrow = r.querySelector('[title="Opens in its own window"]')
      expect(arrow?.querySelector('svg')?.getAttribute('class')).toContain('lucide-external-link')
    }
    expect(within(group).queryAllByRole('menuitemcheckbox')).toHaveLength(0)
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
    const recent = Math.floor(pinned.getTime() / 1000) - 60
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({
      crews: [
        crew({ endpointId: 'ep-1', name: 'Up', lastSeenAt: seen }),
        crew({
          endpointId: 'ep-2', name: 'Late', presence: 'HEARTBEAT_LATE',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: recent,
        }),
        crew({ endpointId: 'ep-3', name: 'Tunnel down', relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: null }),
        crew({
          endpointId: 'ep-4', name: 'Down', presence: 'NOT_CONNECTED',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: seen,
        }),
        crew({ endpointId: 'ep-5', name: 'New', presence: 'NEVER_CONNECTED', relayConnectionStatus: 'NOT_CONNECTED' }),
        crew({ endpointId: 'ep-6', name: 'Odd', presence: 'UNKNOWN', relayConnectionStatus: 'UNKNOWN' }),
        // Presence still says late, but no renewal for five minutes: the host stopped.
        crew({
          endpointId: 'ep-7', name: 'Stopped', presence: 'HEARTBEAT_LATE',
          relayConnectionStatus: 'NOT_CONNECTED', lastSeenAt: seen,
        }),
      ],
    })
    mount()
    const group = await openGroup(userEvent.setup())
    const status = within(group).getAllByTestId('relay-crew-row')
      .map(r => r.querySelector('.text-\\[12px\\]')?.textContent)
    const ago = `last seen ${timeAgo(seen)}`
    expect(status).toEqual([
      'Private · online',
      `Private · reconnecting · last seen ${timeAgo(recent)}`,
      'Private · reconnecting',
      `Private · offline · ${ago}`,
      'Private · offline',
      'Private · offline',
      `Private · offline · ${ago}`,
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

  it('with the pane calls, Open puts the Crew in a tab over <main>; a refusal still stays in the menu', async () => {
    const open = vi.fn(async () => ({ ok: true }))
    const openPane = vi.fn(async () => ({ ok: true }))
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open, openPane, onPaneEvent: () => () => {} }
    const main = document.createElement('main')
    main.id = 'main-content'
    main.getBoundingClientRect = () => ({ x: 236, y: 42, width: 700, height: 500 }) as DOMRect
    document.body.appendChild(main)
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' }), crew({})] })
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(row('Lab'))
    await waitFor(() => expect(openPane).toHaveBeenCalledWith('ep-9', { x: 236, y: 42, width: 700, height: 500 }, { width: window.innerWidth, height: window.innerHeight }))
    expect(open).not.toHaveBeenCalled()
    await waitFor(() => expect(store.getState().instances.relayActive).toBe('ep-9'))
    expect(store.getState().instances.relayTabs).toEqual([{ id: 'ep-9', name: 'Lab' }])
    openPane.mockResolvedValueOnce({ ok: false, error: 'CREW_OFFLINE' })
    await openGroup(u)
    await u.click(row('Build box'))
    expect((await screen.findByTestId('relay-crew-refusal')).textContent).toContain('This Crew is offline.')
    main.remove()
  })

  it('shows a × on an open Crew\'s row only while it is open, and the × closes its pane', async () => {
    const openPane = vi.fn(async () => ({ ok: true }))
    const closePane = vi.fn(async () => ({ ok: true }))
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = { open: vi.fn(), openPane, closePane, onPaneEvent: () => () => {} }
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' }), crew({})] })
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
    const u = userEvent.setup()
    await openGroup(u)
    expect(screen.queryByTestId('relay-crew-close-ep-9')).toBeNull()
    await u.click(row('Lab'))
    await waitFor(() => expect(store.getState().instances.relayActive).toBe('ep-9'))
    await openGroup(u)
    const x = await screen.findByTestId('relay-crew-close-ep-9')
    expect(x.getAttribute('aria-label')).toBe('Close Lab')
    expect(screen.queryByTestId(`relay-crew-close-${crew({}).endpointId}`)).toBeNull()
    await u.click(x)
    expect(closePane).toHaveBeenCalledWith('ep-9')
    // The pane's `closed` event drops the tab, and the × with it.
    act(() => { store.dispatch(relayPaneClosed('ep-9')) })
    await waitFor(() => expect(screen.queryByTestId('relay-crew-close-ep-9')).toBeNull())
    expect(row('Lab')).toBeTruthy()
  })

  it('opens, as a tab in this window, a Crew another dashboard window handed to it, and says why one is refused', async () => {
    let handOff: (id: string) => void = () => {}
    const unsubscribe = vi.fn()
    const openPane = vi.fn(async (id: string) => (id === 'ep-9' ? { ok: true } : { ok: false, error: 'CREW_OFFLINE' }))
    ;(window as { relayCrewAPI?: unknown }).relayCrewAPI = {
      open: vi.fn(), openPane, onPaneEvent: () => () => {},
      onOpenRequest: (cb: (id: string) => void) => { handOff = cb; return unsubscribe },
    }
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({ endpointId: 'ep-9', name: 'Lab' }), crew({})] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    const { unmount } = renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store })
    await screen.findByRole('button', { name: /Switch crew/ })
    await act(async () => { handOff('ep-9') })
    await waitFor(() => expect(store.getState().instances.relayActive).toBe('ep-9'))
    expect(store.getState().instances.relayTabs).toEqual([{ id: 'ep-9', name: 'Lab' }])
    await act(async () => { handOff(crew({}).endpointId) })
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent).toContain("Couldn't open Build box. This Crew is offline.")
    unmount()
    expect(unsubscribe).toHaveBeenCalled()
  })

  it('never connects a listed Crew on its own', async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
    mount()
    await openGroup(userEvent.setup())
    expect(open).not.toHaveBeenCalled()
  })

  it.each([
    ['CREW_ACCESS_NOT_GRANTED', 'You no longer have access to this Crew.'],
    ['ACCESS_DENIED', "Couldn't open this Crew."],
    ['REMOTE_ACCESS_DISABLED', 'Remote access is turned off for this account.'],
    ['CREW_OFFLINE', 'This Crew is offline.'],
    ['THROTTLED', 'Too many requests. Try again in a minute.'],
    ['SIGNED_OUT', 'Sign in to Kiro to see your Shared Crews.'],
    ['NOT_SET_UP', "Shared Crews over the relay aren't set up."],
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
    expect(document.getElementById('relay-crews-list-refusal')?.textContent).toContain("Couldn't load your Shared Crews.")
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
      .toContain('Sign in to Kiro to see your Shared Crews.')
    expect(screen.queryByTestId('relay-crew-row')).toBeNull()
    // Signing in is the fix, so it comes first; the agent hand-off stays after it
    // for a backend that does not run on the Kiro sign-in.
    const items = screen.getAllByRole('menuitem').map(i => i.textContent ?? '')
    expect(screen.getByTestId('relay-crews-list-sign-in').textContent).toContain('Sign in to Kiro')
    expect(screen.getByTestId('relay-crews-list-sign-in').getAttribute('aria-describedby')).toBe('relay-crews-list-refusal')
    const signIn = items.findIndex(t => t.includes('Sign in to Kiro'))
    const handoff = items.findIndex(t => /ask the agent/i.test(t))
    expect(signIn).toBeGreaterThan(-1)
    expect(handoff).toBeGreaterThan(signIn)
  })

  it('keeps the agent hand-off for a refusal signing in does not fix', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(403, 'denied', JSON.stringify({ error: 'denied', code: 'ACCESS_DENIED' })),
    )
    mount()
    await openGroup(userEvent.setup())
    await screen.findByTestId('relay-crews-list-refusal')
    expect(screen.queryByTestId('relay-crews-list-sign-in')).toBeNull()
    expect(screen.getByRole('menuitem', { name: /ask the agent/i })).toBeTruthy()
  })

  it.each([
    ['a remote Crew pane', { activeId: 'remote-1', relayActive: null }],
    ['a relay Crew pane', { activeId: null, relayActive: 'relay-1' }],
  ] as const)('offers the way to Settings > Kiro account on a signed-out list refusal, over %s', async (_name, cover) => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(401, 'signed out', JSON.stringify({ error: 'signed out', code: 'SIGNED_OUT' })),
    )
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const history = createMemoryHistory({ initialEntries: ['/chat'] })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ instances: { warm: {}, mru: [], unread: {}, ...cover } })
    function Routed() {
      const [loc, setLoc] = React.useState(history.location)
      React.useEffect(() => history.listen(({ location }) => setLoc(location)), [])
      return <Router location={loc} navigator={history}><TeamCrewsSwitcher variant="inline" /></Router>
    }
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <ThemeProvider><Routed /></ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    const u = userEvent.setup()
    await openGroup(u)
    await u.click(await screen.findByRole('menuitem', { name: 'Sign in to Kiro' }))
    expect(history.location.pathname).toBe('/settings/kiro-account')
    // A remote pane covered the local Crew: it is brought forward, so the tab is seen.
    expect(store.getState().instances.activeId).toBeNull()
    expect(store.getState().instances.relayActive ?? null).toBeNull()
  })

  it('offers no Kiro account item on a list refusal that is not about sign-in', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    await openGroup(userEvent.setup())
    await screen.findByTestId('relay-crews-list-refusal')
    expect(screen.queryByTestId('relay-crews-list-sign-in')).toBeNull()
  })

  it('shows a load failure, not an Open failure, when the list read fails with no known code', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(503, 'down', JSON.stringify({ error: 'down', code: 'SERVICE_ERROR' })),
    )
    mount()
    await openGroup(userEvent.setup())
    expect((await screen.findByTestId('relay-crews-list-refusal')).textContent)
      .toContain("Couldn't load your Shared Crews.")
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
    expect(screen.getByTestId('relay-crews-list-refusal').textContent).toContain("Couldn't load your Shared Crews.")
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

  describe('re-reading the list', () => {
    // A fake clock moved by hand; shouldAdvanceTime keeps findBy and react-query moving.
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }) })
    const offline = crew({ endpointId: 'ep-2', name: 'Off box', relayConnectionStatus: 'DISCONNECTED' })

    it('reads the list again when the menu opens, unless the last read is under 5 s old', async () => {
      withBridge()
      const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
      mount()
      const u = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      await openGroup(u)
      // The read that showed the bar is seconds old, so this open reads nothing new.
      expect(spy).toHaveBeenCalledTimes(1)
      await u.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
      await act(async () => { await vi.advanceTimersByTimeAsync(6_000) })
      await openGroup(u)
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    })

    it('re-reads every 15 s while a listed Crew is not online', async () => {
      withBridge()
      const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({}), offline] })
      mount()
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    })

    it('re-reads every 60 s while every listed Crew is online', async () => {
      withBridge()
      const spy = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})] })
      mount()
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
      expect(spy).toHaveBeenCalledTimes(1)
      await act(async () => { await vi.advanceTimersByTimeAsync(45_000) })
      await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    })

    it('stops re-reading once the crews route answers 404', async () => {
      withBridge()
      const spy = vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(new ApiError(404, 'off', '{}'))
      const client = mount()
      await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('error'))
      await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
      expect(spy).toHaveBeenCalledTimes(1)
    })
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

describe('TeamCrewsGroup: a Team Crew the person has not seen yet', () => {
  const NEW_ID = 'n'.repeat(24)
  const fresh = () => crew({ endpointId: NEW_ID, name: 'Docs box', crewType: 'TEAM', unseen: true })

  function mountAt(route: string) {
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    return renderWithProviders(<TeamCrewsSwitcher variant="inline" />, { store, route })
  }

  it('dots the caret and the row, tells the gateway once the menu shows it, and drops the dot on close', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt('/')
    const caret = await screen.findByRole('button', { name: 'Switch crew, new Shared Crew' })
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
    const u = userEvent.setup()
    await u.click(caret)
    const group = await screen.findByTestId('team-crews-group')
    expect(within(group).getByTestId('relay-crew-new').textContent).toBe('New')
    await waitFor(() => expect(seen).toHaveBeenCalledWith([NEW_ID]))
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it("opens the Crew a note's Open action names, then clears its dot", async () => {
    const open = withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect(open).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(seen).toHaveBeenCalledWith([NEW_ID]))
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
  })

  it("sends a note's Open at once while the list reads signed out, and a later sign-in opens nothing", async () => {
    const open = withBridge(vi.fn(async () => ({ ok: false, error: 'SIGNED_OUT' })))
    vi.spyOn(api, 'relayViewerCrews')
      .mockRejectedValueOnce(new ApiError(401, 'signed out', JSON.stringify({ error: 'signed out', code: 'SIGNED_OUT' })))
      .mockResolvedValue({ crews: [fresh()] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    const { queryClient } = mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent).toContain('Sign in')
    await act(async () => { await queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY }) })
    await screen.findByTestId('crew-switcher-new-dot')
    expect(open).toHaveBeenCalledTimes(1)
  })

  it("opens a note's Crew through the bridge even when the cached list doesn't have it", async () => {
    const open = withBridge()
    const read = vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    await waitFor(() => expect(open).toHaveBeenCalledWith(NEW_ID))
    expect(open).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('relay-crew-open-refusal')).toBeNull()
  })

  it("does not open a note's Crew from a render that lags a navigation", async () => {
    const open = withBridge()
    let answer!: (v: { crews: RelayCrewEntry[] }) => void
    vi.spyOn(api, 'relayViewerCrews').mockReturnValueOnce(new Promise(r => { answer = r }))
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    vi.spyOn(api, 'listInstances').mockResolvedValue(listResp([]))
    const history = createMemoryHistory({ initialEntries: [`/chat?relay_crew=${NEW_ID}`] })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = createTestStore({ instances: { warm: {}, activeId: null, mru: [], unread: {} } })
    // The router keeps rendering the note's location, as BrowserRouter does while
    // a click's navigation is still in its transition.
    const at = history.location
    act(() => { history.push('/settings?tab=general') })
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <ThemeProvider>
            <Router location={at} navigator={history}><TeamCrewsSwitcher variant="inline" /></Router>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    await act(async () => { answer({ crews: [fresh()] }) })
    // The list has rendered (its new Crew dots the caret), so the effect has had its run.
    await screen.findByTestId('crew-switcher-new-dot')
    expect(history.location.pathname + history.location.search).toBe('/settings?tab=general')
    expect(history.index).toBe(1)
    expect(open).not.toHaveBeenCalled()
  })

  it('does not let a list read that started before a seen save bring the dot back', async () => {
    withBridge()
    let release: (v: { crews: RelayCrewEntry[] }) => void = () => {}
    const read = vi.spyOn(api, 'relayViewerCrews').mockResolvedValueOnce({ crews: [fresh()] })
    vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    const { queryClient } = mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Shared Crew' }))
    await screen.findByTestId('team-crews-group')
    // A poll goes out while the menu is open, answering with the pre-save list.
    read.mockReturnValueOnce(new Promise(r => { release = r }))
    void queryClient.refetchQueries({ queryKey: RELAY_VIEWER_CREWS_KEY })
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
    await act(async () => { release({ crews: [fresh()] }) })
    // The late answer has been taken in or dropped once the read is no longer running.
    await waitFor(() => expect(queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)?.fetchStatus).toBe('idle'))
    expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull()
  })

  it("says the gateway's refusal when a note's Crew isn't granted any more", async () => {
    const open = withBridge(vi.fn(async () => ({ ok: false, error: 'CREW_ACCESS_NOT_GRANTED' })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    mountAt(`/chat?relay_crew=${'z'.repeat(24)}`)
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent)
      .toContain('You no longer have access to this Crew.')
    expect(open).toHaveBeenCalledWith('z'.repeat(24))
  })

  it('keeps the dot when the gateway could not save that the Crew was seen', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockRejectedValue(new ApiError(503, 'not saved', '{}'))
    mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Shared Crew' }))
    await screen.findByTestId('team-crews-group')
    await waitFor(() => expect(seen).toHaveBeenCalled())
    expect((await screen.findByTestId('relay-crews-seen-failed')).textContent)
      .toContain("Couldn't save that you've seen this.")
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
  })

  it('clears the dot when the seen reply lands after the menu closed', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    let reply: (v: { ok: boolean }) => void = () => {}
    vi.spyOn(api, 'relayViewerCrewsSeen').mockReturnValue(new Promise(r => { reply = r }))
    mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Shared Crew' }))
    await screen.findByTestId('team-crews-group')
    await u.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('team-crews-group')).toBeNull())
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
    await act(async () => { reply({ ok: true }) })
    await waitFor(() => expect(screen.queryByTestId('crew-switcher-new-dot')).toBeNull())
  })

  it('sends a long unseen list in batches the route accepts', async () => {
    withBridge()
    const many = Array.from({ length: 450 }, (_, i) =>
      crew({ endpointId: `m${String(i).padStart(23, '0')}`, name: `Crew ${i}`, crewType: 'TEAM', unseen: true }))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: many })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt('/')
    const u = userEvent.setup()
    await u.click(await screen.findByRole('button', { name: 'Switch crew, new Shared Crew' }))
    await screen.findByTestId('team-crews-group')
    await waitFor(() => expect(seen).toHaveBeenCalledTimes(3))
    expect(seen.mock.calls.map(c => c[0].length)).toEqual([200, 200, 50])
  })

  it('says why beside the switcher when that open fails, naming the Crew once the list has it, and keeps the dot', async () => {
    // The refusal lands after the list read, as a mint through the gateway does.
    let refuse: (r: { ok: boolean; error?: string }) => void = () => {}
    withBridge(vi.fn(() => new Promise<{ ok: boolean; error?: string }>(r => { refuse = r })))
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [fresh()] })
    const seen = vi.spyOn(api, 'relayViewerCrewsSeen').mockResolvedValue({ ok: true })
    mountAt(`/chat?relay_crew=${NEW_ID}`)
    await screen.findByTestId('crew-switcher-new-dot')
    await act(async () => { refuse({ ok: false, error: 'CREW_OFFLINE' }) })
    expect((await screen.findByTestId('relay-crew-open-refusal')).textContent).toContain("Couldn't open Docs box. This Crew is offline.")
    expect(screen.getByTestId('crew-switcher-new-dot')).toBeTruthy()
    expect(seen).not.toHaveBeenCalled()
  })
})

describe('TeamCrewsGroup account line', () => {
  const PORTAL = 'https://example-portal.awsapps.example.test/start'

  async function accountLine(account: unknown) {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [crew({})], account } as never)
    mount()
    const group = await openGroup(userEvent.setup())
    await within(group).findByRole('menuitem', { name: 'Connect Build box' })
    return within(group).queryByTestId('team-crews-account')?.textContent ?? null
  }

  it.each([
    [{ accountType: 'BuilderId' }, 'Signed in with Builder ID'],
    [{ accountType: 'IamIdentityCenter', startUrl: PORTAL }, 'Signed in with IAM Identity Center · example-portal.awsapps.example.test'],
    [{ accountType: 'SocialGoogle' }, 'Signed in with Google'],
    [{ accountType: 'ExternalIdP' }, 'Signed in with Company SSO'],
  ])('names the account %j the list was read for', async (account, text) => {
    expect(await accountLine(account)).toBe(text)
  })

  it('leaves the line out when the gateway names no account', async () => {
    expect(await accountLine(undefined)).toBeNull()
  })

  it('leaves the line out for an account with no kind', async () => {
    expect(await accountLine({ accountType: '' })).toBeNull()
  })

  it('leaves the line out on a list refusal', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockRejectedValue(
      new ApiError(409, 'signed out', JSON.stringify({ error: 'signed out', code: 'SIGNED_OUT' })),
    )
    mount()
    const group = await openGroup(userEvent.setup())
    await within(group).findByTestId('relay-crews-list-refusal')
    expect(within(group).queryByTestId('team-crews-account')).toBeNull()
  })

  it('shows no e-mail even if one were sent', async () => {
    const text = await accountLine({ accountType: 'BuilderId', email: 'someone@example.test' })
    expect(text).toBe('Signed in with Builder ID')
  })

  it('wraps the line instead of cutting it, and titles it with the full text', async () => {
    await accountLine({ accountType: 'IamIdentityCenter', startUrl: PORTAL })
    const line = screen.getByTestId('team-crews-account')
    expect(line.className).not.toMatch(/\btruncate\b/)
    expect(line.getAttribute('title')).toBe('Signed in with IAM Identity Center · example-portal.awsapps.example.test')
  })
})

describe('TeamCrewsSwitcher with an empty list', () => {
  afterEach(() => { hookOverride.value = null })

  it('mounts the group with the account line above the empty text when an account is named', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [], account: { accountType: 'BuilderId' } } as never)
    mount()
    const group = await openGroup(userEvent.setup())
    const line = within(group).getByTestId('team-crews-account')
    expect(line.textContent).toBe('Signed in with Builder ID')
    const empty = within(group).getByText('No Shared Crews to open.')
    expect(line.compareDocumentPosition(empty) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('mounts nothing, as before, when no account is named', async () => {
    withBridge()
    vi.spyOn(api, 'relayViewerCrews').mockResolvedValue({ crews: [] })
    const client = mount([sshCrew({ was_connected: true })])
    await waitFor(() => expect(client.getQueryState(RELAY_VIEWER_CREWS_KEY)?.status).toBe('success'))
    await userEvent.setup().click(await screen.findByRole('button', { name: /Switch crew/i }))
    await screen.findByRole('menuitemradio', { name: /Cloud One/ })
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
  })

  it('mounts nothing while hidden, even with an account', async () => {
    // The real hook never pairs hidden with an account, so this pins the guard itself.
    hookOverride.value = { hidden: true, crews: [], account: { accountType: 'BuilderId' }, refusal: null, retry: () => {}, retrying: false }
    mount([sshCrew({ was_connected: true })])
    await userEvent.setup().click(await screen.findByRole('button', { name: /Switch crew/i }))
    await screen.findByRole('menuitemradio', { name: /Cloud One/ })
    expect(screen.queryByTestId('team-crews-group')).toBeNull()
  })
})
