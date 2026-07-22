import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
import { isEmbeddedPane } from '../lib/embedded'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 7778,
          ttl: '20h',
          remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7777, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
  },
}))
import { api } from '../api/client'

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
})

describe('InstancesViewport', () => {
  it('renders nothing when embedded (a pane never hosts nested panes)', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    const { container } = renderWithProviders(<InstancesViewport />, { store })
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('auto-warms connected instances on load but stays on the Local tab', async () => {
    // Default mock has cd-1 connected. On load we pre-mount its iframe (hidden,
    // since we land on Local) so it's instantly usable without a click.
    const { store } = renderWithProviders(<InstancesViewport />)
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toBeDefined())
    // Landed on Local (activeId null) -> the warmed iframe is mounted but hidden.
    expect(store.getState().instances.activeId).toBeNull()
    const frame = document.querySelector('iframe') as HTMLIFrameElement
    expect(frame).not.toBeNull()
    expect(frame.style.display).toBe('none')
  })

  it('does not auto-warm an instance whose tunnel is down', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 0,
          ttl: '20h',
          remote_bin: '',
          was_connected: true,
          status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    })
    const { store } = renderWithProviders(<InstancesViewport />)
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
    // A down instance is never auto-warmed; it stays a sticky tab to be clicked.
    expect(api.connectInstance).not.toHaveBeenCalled()
    expect(store.getState().instances.warm['cd-1']).toBeUndefined()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('keeps warm iframes mounted but hidden while on the Local tab', async () => {
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: null, mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    // Mounted (so switching back to it is instant) but hidden, and the whole
    // stack is hidden on Local so the native dashboard shows through.
    expect(frame.style.display).toBe('none')
    expect((frame.parentElement as HTMLElement).style.display).toBe('none')
  })

  it('renders the active instance iframe with the loopback token URL', async () => {
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    expect(frame.getAttribute('src')).toBe(`http://${window.location.hostname}:7778/?token=tok`)
    // Active frame is visible.
    expect(frame.style.display).toBe('block')
  })

  it('shows an in-pane error panel with Retry for an active non-warm instance', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 0,
          ttl: '20h',
          remote_bin: '',
          was_connected: true,
          status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })

    expect(await screen.findByText(/Connection error/i)).toBeInTheDocument()
    expect(screen.getByText('ssh unreachable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    // No iframe is mounted for a non-warm instance.
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('surfaces the error panel for an active warm-but-disconnected tab (stale warm)', async () => {
    // A tunnel dropped mid-session: status flips to error but the `warm` entry
    // lingers. The panel must show over the (now dead) iframe so the user gets
    // an error message + Retry instead of a silently-blank pane.
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 7778,
          ttl: '20h',
          remote_bin: '',
          was_connected: true,
          status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'stale' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })

    // Panel shows despite the lingering warm entry.
    expect(await screen.findByText(/Connection error/i)).toBeInTheDocument()
    expect(screen.getByText('ssh unreachable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
  })

  it('Retry re-mints a token and warms the instance', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Cloud One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 0,
          ttl: '20h',
          remote_bin: '',
          was_connected: true,
          status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    const u = userEvent.setup()
    renderWithProviders(<InstancesViewport />, { store })

    await u.click(await screen.findByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    await waitFor(() =>
      expect(store.getState().instances.warm['cd-1']).toEqual({ port: 7777, token: 'tok' }),
    )
  })

  it('does NOT flash the panel over a healthy warm iframe while the query has no entry yet (activeInst undefined)', async () => {
    // Regression (code review): a warm+connected active tab whose
    // instance is momentarily absent from the query results (initial load /
    // refetch) must keep showing its live iframe, NOT overlay the error panel.
    vi.mocked(api.listInstances).mockResolvedValue({ instances: [], warm_set_cap: 5 })
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    // Live iframe is shown; the error/connecting panel is NOT mounted.
    expect(frame.style.display).toBe('block')
    expect(screen.queryByText(/Connection error/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /Retry/i })).toBeNull()
  })

  it('K-cap eviction drops only the warm iframe, never disconnecting the tunnel', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ instances: [], warm_set_cap: 1 })
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 'a' }, 'cd-2': { port: 7779, token: 'b' } },
        activeId: 'cd-2',
        mru: ['cd-2', 'cd-1'],
        unread: {},
      },
    })
    renderWithProviders(<InstancesViewport />, { store })

    // cap=1 with 2 warm -> evict the LRU non-active one (cd-1) by dropping its
    // iframe only; the active one stays and the tunnel is NEVER disconnected.
    await waitFor(() => expect(store.getState().instances.warm['cd-1']).toBeUndefined())
    expect(store.getState().instances.warm['cd-2']).toBeDefined()
    expect(api.disconnectInstance).not.toHaveBeenCalled()
  })
})
