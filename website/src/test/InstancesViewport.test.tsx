import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
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

  it('renders the instance tab bar on the disconnect panel so the user can escape', async () => {
    // Regression: while a remote tab is active the local header (and its
    // InstanceTabBar) is hidden, and the embedded switcher lives inside the
    // dead iframe — without a strip on the panel the disconnect view was a
    // dead end with no way to reach Local or any other instance.
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

    expect(await screen.findByText(/Connection error/i)).toBeInTheDocument()
    // The full switcher renders atop the panel: Local + the instance tab.
    const bar = await screen.findByRole('tablist', { name: /Instances/i })
    expect(bar).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Local/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Cloud One/i })).toBeInTheDocument()

    // Clicking Local escapes the disconnect view.
    await u.click(screen.getByRole('tab', { name: /Local/i }))
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('insets the panel tab bar clear of the macOS traffic lights when macInset is set', async () => {
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
    renderWithProviders(<InstancesViewport macInset />, { store })

    const bar = await screen.findByRole('tablist', { name: /Instances/i })
    expect(bar.style.paddingLeft).toBe('84px')
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

  // Explicit connected-instance mock for the readiness/watchdog tests below —
  // earlier tests override listInstances and clearAllMocks() does NOT restore
  // implementations, so relying on the module-level default is order-dependent.
  const mockConnectedCd1 = () =>
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
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    })

  it('shows a loading overlay WITH the tab strip for an active warm pane that has not announced readiness', async () => {
    // Regression (strand bug): after Retry succeeds, setWarm mounts the iframe
    // and the error panel (with its escape-hatch tab strip) unmounted
    // immediately — leaving a black loading pane with NO tabs. The overlay must
    // keep the switcher reachable until the embedded SPA is actually up.
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })

    expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
    // The full switcher renders atop the overlay: the user can always escape.
    const bar = await screen.findByRole('tablist', { name: /Instances/i })
    expect(bar).toBeInTheDocument()
    // Not the error panel — no Retry while the load is still in flight.
    expect(screen.queryByText(/Connection error/i)).toBeNull()
  })

  it('dismisses the loading overlay when the pane posts mc-embedded-ready from its tunnel origin', async () => {
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()

    // The embedded SPA announces readiness from its validated loopback origin.
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'mc-embedded-ready', v: 1 },
        origin: 'http://127.0.0.1:7778',
      }),
    )
    await waitFor(() => expect(store.getState().instances.ready['cd-1']).toBe(true))
    await waitFor(() => expect(screen.queryByText(/Loading pane/i)).toBeNull())
  })

  it('ignores mc-embedded-ready from an unknown origin (no readiness, overlay stays)', async () => {
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()

    // Wrong port (no warm tunnel) and a non-loopback origin must both be dropped.
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'mc-embedded-ready', v: 1 },
        origin: 'http://127.0.0.1:9999',
      }),
    )
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'mc-embedded-ready', v: 1 },
        origin: 'https://evil.example.com',
      }),
    )
    expect(store.getState().instances.ready['cd-1']).toBeUndefined()
    expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()
  })

  it('surfaces the error panel with Retry when the pane never becomes ready (load watchdog)', async () => {
    mockConnectedCd1()
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()

      // 15s without mc-embedded-ready -> the silent black pane becomes an
      // actionable error panel (backend still says connected, so without the
      // watchdog nothing would ever surface it).
      await act(async () => {
        vi.advanceTimersByTime(15_000)
      })
      expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
    // The escape-hatch strip is on the panel (query resolves under real timers).
    expect(await screen.findByRole('tablist', { name: /Instances/i })).toBeInTheDocument()
  })

  it('Retry after a load timeout force-reloads the iframe even for an identical token', async () => {
    mockConnectedCd1()
    // connectInstance returns the SAME port+token as the preloaded warm entry —
    // the src is byte-identical, so only a keyed remount can reload a dead frame.
    vi.mocked(api.connectInstance).mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' })
    vi.useFakeTimers()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    await act(async () => {
      vi.advanceTimersByTime(15_000)
    })
    expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()
    const before = document.querySelector('iframe') as HTMLIFrameElement
    // Click under real timers — userEvent/waitFor deadlock with fake ones.
    vi.useRealTimers()

    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    // Back to the loading overlay (verdict cleared), not the error panel.
    await waitFor(() => expect(screen.queryByText(/Pane failed to load/i)).toBeNull())
    expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()
    // The iframe was remounted (new element) to force the reload.
    const after = document.querySelector('iframe') as HTMLIFrameElement
    expect(after).not.toBe(before)
  })

  it('a late mc-embedded-ready clears a timed-out verdict without Retry', async () => {
    mockConnectedCd1()
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      await act(async () => {
        vi.advanceTimersByTime(15_000)
      })
      expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()

      // The pane was just slow — a late readiness announcement restores it.
      await act(async () => {
        window.dispatchEvent(
          new MessageEvent('message', {
            data: { type: 'mc-embedded-ready', v: 1 },
            origin: 'http://127.0.0.1:7778',
          }),
        )
      })
      expect(store.getState().instances.ready['cd-1']).toBe(true)
      expect(screen.queryByText(/Pane failed to load/i)).toBeNull()
      expect(screen.queryByText(/Loading pane/i)).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })
})
