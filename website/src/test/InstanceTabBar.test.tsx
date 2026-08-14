import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstanceTabBar from '../components/InstanceTabBar'
import type { InstanceView, SsoStatus } from '../api/client'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: {
      listInstances: vi.fn(),
      connectInstance: vi.fn(),
    },
  }
})
import { api } from '../api/client'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

const conn = (over: Partial<InstanceView> = {}): InstanceView => ({
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

const okSso: SsoStatus = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

/** Typed builder for the `api.listInstances` mock resolved value. */
const listResp = (instances: InstanceView[]) => ({ active: true, instances, warm_set_cap: 5, sso: okSso })

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
})

/** Open the crew dropdown and return the menu row for `name`. */
async function openSwitcher(u: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await u.click(await screen.findByRole('button', { name: /Switch crew/i }))
  return await screen.findByRole('menuitemradio', { name })
}

describe('InstanceTabBar', () => {
  it('renders nothing when embedded as an instance pane (no recursive nesting)', async () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    const { container } = renderWithProviders(<InstanceTabBar />, { store })
    // No switcher, and the instances poll is disabled while embedded.
    expect(container.querySelector('[aria-label^="Switch crew"]')).toBeNull()
    expect(api.listInstances).not.toHaveBeenCalled()
  })

  it('renders nothing when no instance is connected (single-instance experience)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([]))
    const { container } = renderWithProviders(<InstanceTabBar />)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(container.querySelector('[aria-label^="Switch crew"]')).toBeNull()
  })

  it('renders Local + a tab per connected instance and switches to Local', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />, { store })

    // The trigger names the crew on screen; the destinations live in its menu.
    const local = await openSwitcher(u, /Local/i)
    expect(local).toBeInTheDocument()
    expect(screen.getByRole('menuitemradio', { name: /Cloud One/i })).toBeInTheDocument()

    await u.click(local)
    await waitFor(() => expect(store.getState().instances.activeId).toBeNull())
  })

  it('connects a not-yet-warm instance when its tab is clicked', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: 'tok' })
    const u = userEvent.setup()
    const { store } = renderWithProviders(<InstanceTabBar />)

    await u.click(await openSwitcher(u, /Cloud One/i))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7778, token: 'tok' }))
    expect(store.getState().instances.activeId).toBe('cd-1')
  })

  it('reconnects a warm-but-disconnected tab on click (stale warm after a mid-session drop)', async () => {
    // A tunnel that dropped mid-session: status flips to error but the in-memory
    // `warm` entry lingers. Clicking the (red) tab must still fire a reconnect —
    // gating only on `!warm[id]` would skip it and nothing would happen.
    const down = conn({
      status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
      was_connected: true,
    })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([down]))
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: 'fresh' })
    const u = userEvent.setup()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'stale' } }, activeId: null, mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstanceTabBar />, { store })

    await u.click(await openSwitcher(u, /Cloud One/i))
    expect(store.getState().instances.activeId).toBe('cd-1')
    // Reconnect fires despite the lingering warm entry, and re-warms with a fresh token.
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7778, token: 'fresh' }))
  })

  it('does NOT reconnect a warm + connected tab on click (no needless re-mint)', async () => {
    // The healthy path: a live, warm tab just switches — clicking it must not
    // re-mint/reload an in-use pane.
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const u = userEvent.setup()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: null, mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstanceTabBar />, { store })

    await u.click(await openSwitcher(u, /Cloud One/i))
    expect(store.getState().instances.activeId).toBe('cd-1')
    // Give any stray async a tick; connect must NOT have been called.
    await new Promise(r => setTimeout(r, 0))
    expect(api.connectInstance).not.toHaveBeenCalled()
  })

  it('shows the active tunnel connection status with a token auto-refresh countdown', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([
      conn({ status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777, token_ttl_remaining: 72000 } }),
    ]))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 't' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstanceTabBar />, { store })
    // ttl 20h (72000s), 72000s remaining -> refresh fires at 80% elapsed (20% left),
    // so untilRefresh = 72000 - 14400 = 57600s ≈ 16h.
    expect(await screen.findByText(/connected · refresh/i)).toBeInTheDocument()
    expect(screen.getByTitle(/Tunnel connected.*auto-refresh in/i)).toBeInTheDocument()
  })

  it('keeps a sticky tab for a was_connected instance whose tunnel is down', async () => {
    // A tab exists for an instance the user intends to be connected
    // (was_connected) even when its live tunnel is down after a restart.
    const down = conn({
      status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
      was_connected: true,
    })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([down]))
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />)
    expect(await openSwitcher(u, /Cloud One/i)).toBeInTheDocument()
    // The error state reaches assistive tech through the row's accessible name,
    // not only through a red dot and a hover tooltip.
    expect(screen.getByRole('menuitemradio', { name: /tunnel error/i })).toBeInTheDocument()
    expect(screen.getByTitle(/— tunnel error/i)).toBeInTheDocument()
    // ...and it is on SCREEN, not only in the accessible name: the dot is the
    // other carrier of state and it is colour alone, so a colourblind user
    // scanning for the crew that failed would otherwise have to hover each row.
    const word = screen.getByText(/tunnel error/i, { selector: 'span' })
    expect(word).not.toHaveClass('sr-only')
  })

  it('shows no tab for an instance that was never connected and is down', async () => {
    const never = conn({
      status: { instance_id: 'cd-1', state: 'disconnected', remote_port: 7777 },
      was_connected: false,
    })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([never]))
    const { container } = renderWithProviders(<InstanceTabBar />)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(container.querySelector('[aria-label^="Switch crew"]')).toBeNull()
  })

  it('keeps the tab and activates it when a reconnect attempt fails', async () => {
    const down = conn({
      status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
      was_connected: true,
    })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([down]))
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('still unreachable'))
    const u = userEvent.setup()
    const { store } = renderWithProviders(<InstanceTabBar />)

    await u.click(await openSwitcher(u, /Cloud One/i))
    // Activated immediately (so the in-pane error panel shows) and a reconnect
    // was attempted...
    await waitFor(() => expect(store.getState().instances.activeId).toBe('cd-1'))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    // ...but the failed connect neither warms it nor removes the tab.
    expect(store.getState().instances.warm['cd-1']).toBeUndefined()
    expect(await openSwitcher(u, /Cloud One/i)).toBeInTheDocument()
  })

  it('carries unread counts for crews the dropdown is hiding', async () => {
    // Collapsing the strip into a menu would otherwise hide every unread badge
    // behind a closed menu: the trigger has to answer "is anything waiting?"
    // without being opened, and each row still owns its own count.
    const other = conn({ id: 'cd-2', name: 'Cloud Two', ssh_host: 'cd-2-alias', remote_port: 7779 })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn(), other]))
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 't' } },
        activeId: 'cd-1',
        mru: ['cd-1'],
        unread: { 'cd-1': 4, 'cd-2': 3 },
      },
    })
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />, { store })

    // Only the crews NOT on screen are counted, so the active pane's own stale
    // count never inflates the badge.
    // The trigger's own accessible name carries the count, so it is announced
    // even by screen readers that skip a button's child content.
    expect(await screen.findByRole('button', { name: /3 unread elsewhere/i })).toBeInTheDocument()

    await u.click(screen.getByRole('button', { name: /Switch crew/i }))
    expect(await screen.findByLabelText('3 unread')).toBeInTheDocument()
  })

  it('does not print the crew name twice when its host alias IS its name', async () => {
    // "clouddeskARM / clouddeskARM" reads as a rendering bug rather than detail.
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn({ name: 'clouddeskARM', ssh_host: 'clouddeskARM' })]))
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />)
    const row = await openSwitcher(u, /clouddeskARM/i)
    expect(row.textContent?.match(/clouddeskARM/g) ?? []).toHaveLength(1)
  })
})
