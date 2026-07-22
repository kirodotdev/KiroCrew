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

describe('InstanceTabBar', () => {
  it('renders nothing when embedded as an instance pane (no recursive nesting)', async () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    const { container } = renderWithProviders(<InstanceTabBar />, { store })
    // No switcher, and the instances poll is disabled while embedded.
    expect(container.querySelector('[role="tablist"]')).toBeNull()
    expect(api.listInstances).not.toHaveBeenCalled()
  })

  it('renders nothing when no instance is connected (single-instance experience)', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([]))
    const { container } = renderWithProviders(<InstanceTabBar />)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(container.querySelector('[role="tablist"]')).toBeNull()
  })

  it('renders Local + a tab per connected instance and switches to Local', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<InstanceTabBar />, { store })

    expect(await screen.findByRole('tab', { name: /Local/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Cloud One/i })).toBeInTheDocument()

    await u.click(screen.getByRole('tab', { name: /Local/i }))
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('connects a not-yet-warm instance when its tab is clicked', async () => {
    vi.mocked(api.listInstances).mockResolvedValue(listResp([conn()]))
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'cd-1', state: 'connected', local_port: 7778, token: 'tok' })
    const u = userEvent.setup()
    const { store } = renderWithProviders(<InstanceTabBar />)

    await u.click(await screen.findByRole('tab', { name: /Cloud One/i }))
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

    await u.click(await screen.findByRole('tab', { name: /Cloud One/i }))
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

    await u.click(await screen.findByRole('tab', { name: /Cloud One/i }))
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
    // Core fix: a tab exists for an instance the user intends to be connected
    // (was_connected) even when its live tunnel is down after a restart.
    const down = conn({
      status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
      was_connected: true,
    })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([down]))
    renderWithProviders(<InstanceTabBar />)
    expect(await screen.findByRole('tab', { name: /Cloud One/i })).toBeInTheDocument()
    // The error state is surfaced in the tab tooltip.
    expect(screen.getByTitle(/— error/i)).toBeInTheDocument()
  })

  it('shows no tab for an instance that was never connected and is down', async () => {
    const never = conn({
      status: { instance_id: 'cd-1', state: 'disconnected', remote_port: 7777 },
      was_connected: false,
    })
    vi.mocked(api.listInstances).mockResolvedValue(listResp([never]))
    const { container } = renderWithProviders(<InstanceTabBar />)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    expect(container.querySelector('[role="tablist"]')).toBeNull()
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

    await u.click(await screen.findByRole('tab', { name: /Cloud One/i }))
    // Activated immediately (so the in-pane error panel shows) and a reconnect
    // was attempted...
    await waitFor(() => expect(store.getState().instances.activeId).toBe('cd-1'))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    // ...but the failed connect neither warms it nor removes the tab.
    expect(store.getState().instances.warm['cd-1']).toBeUndefined()
    expect(screen.getByRole('tab', { name: /Cloud One/i })).toBeInTheDocument()
  })
})
