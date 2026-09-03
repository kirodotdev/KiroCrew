import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { removeWarm, setActiveId, setWarm } from '../store/instancesSlice'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'

// The postinstall patch (scripts/patch-happy-dom-iframe.mjs) makes happy-dom's
// disabled-iframe path dispatch 'load' instead of throwing DOMException when
// handleDisabledFileLoadingAsSuccess is true — no per-test workaround needed.

// The host drag strips only render under the Electron shell, so the focus-mode
// suppression test needs that to be true. Only `isElectron` is overridden; the
// per-platform flags and caption widths keep their real values.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  isElectron: true,
}))

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
  // Module-level state in the failure recorder: without this the de-dup from an
  // earlier test would suppress the report a later one asserts on.
  __resetInstanceFailuresForTests()
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
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

  it('delegates microphone and fullscreen to the cross-origin pane', async () => {
    // The pane is a cross-origin iframe (same host, different port), where
    // both features are denied unless the parent delegates them. Dropping
    // either regresses a user-visible capability: mic -> getUserMedia rejects
    // with NotAllowedError; fullscreen -> the fullscreen button on native
    // <video> controls inside the embedded chat renders disabled.
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    expect(frame.getAttribute('allow')).toBe('microphone; fullscreen')
    expect(frame.hasAttribute('allowfullscreen')).toBe(true)
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

  it('offers an agent hand-off carrying the diagnosis ladder, not just Retry', async () => {
    // Retry is right for a momentary drop, and useless for the failures a first
    // connect actually hits — SSH config, a remote gateway that is not running, a
    // wrong port. The panel's evidence arrives on a SUCCESSFUL poll, so this panel
    // has to journal it itself, and the report has to carry `probes`: ssh ok +
    // remote dashboard failed names a different repair than ssh failed does.
    __resetErrorJournalForTests()
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
          status: {
            instance_id: 'cd-1',
            state: 'error',
            error: 'ssh unreachable',
            remote_port: 7777,
            diagnosis: {
              code: 'remote_down',
              ok: false,
              reason: 'SSH works but the remote dashboard is not responding',
              probes: [
                { name: 'ssh', ok: true },
                { name: 'remote_dashboard', ok: false },
              ],
            },
          },
        },
      ],
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })

    expect(await screen.findByText(/Connection error/i)).toBeInTheDocument()
    // Present alongside Retry, not instead of it.
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()

    // Click it: the button resolves its own report by EXACT message match, so this
    // is the only assertion that catches a label re-derived locally from the
    // status — the journal would still be correct while the prompt lost the ladder.
    installSoftNavigate(() => {})
    await userEvent.click(await screen.findByRole('button', { name: /agent/i }))
    const prompt = consumeChatHandoff() ?? ''
    expect(prompt).toContain('Code: remote_down')
    expect(prompt).toContain('probes: ssh=ok -> remote_dashboard=FAILED')
    // And the hand-off must be VISIBLE. This panel lives inside the viewport's
    // opaque root overlay, which covers the local pane while a remote tab is
    // active, and the hand-off only soft-navigates the local SPA to /chat —
    // underneath it. Without returning to Local the user keeps staring at this same
    // error panel and reads the button as dead, while every further click stacks
    // another copy of the prompt onto the hand-off queue.
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('the Settings → Remote Instances link returns to Local before navigating (same overlay rule as the hand-off)', async () => {
    // The link soft-navigates the LOCAL SPA, which sits underneath this panel's
    // opaque root overlay while a remote tab is active. Without leaving the
    // remote tab the click looks dead. A modified click opens a new tab and
    // must leave this tab's panel alone.
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

    const link = screen.getByRole('link', { name: /Settings → Remote Instances/ }) as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/settings/instances')

    fireEvent.click(link, { metaKey: true })
    expect(store.getState().instances.activeId).toBe('cd-1')

    fireEvent.click(link)
    expect(store.getState().instances.activeId).toBeNull()
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
    // Regression: a warm+connected active tab whose
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
    const bar = await screen.findByRole('group', { name: /Remote instances/i })
    expect(bar).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /Switch instance/i }))
    expect(screen.getByRole('menuitemradio', { name: /Local/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitemradio', { name: /Cloud One/i })).toBeInTheDocument()

    // Clicking Local escapes the disconnect view.
    await u.click(screen.getByRole('menuitemradio', { name: /Local/i }))
    await waitFor(() => expect(store.getState().instances.activeId).toBeNull())
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

    const bar = await screen.findByRole('group', { name: /Remote instances/i })
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
    const bar = await screen.findByRole('group', { name: /Remote instances/i })
    expect(bar).toBeInTheDocument()
    // Not the error panel — no Retry while the load is still in flight.
    expect(screen.queryByText(/Connection error/i)).toBeNull()
  })

  it('suppresses the host drag strips in focus mode so the pane can peek its own chrome', async () => {
    // The strips are `-webkit-app-region: drag`, which the compositor resolves
    // BEFORE hit-testing. In focus mode the pane hides its own header to match the
    // host, so there is no header left to drag by — and leaving the strips up makes
    // the pane's top band answer neither hover nor clicks, so its chrome can never
    // be summoned back. This is the bug reported on the desktop app: switching to a
    // remote crew left the top region drag-only and dead.
    const { setFocusModeEnabled } = await import('../hooks/useFocusMode')
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: { 'cd-1': true } },
    })
    renderWithProviders(<InstancesViewport />, { store })

    // The pane reports the gaps in its own header that the host may drag by.
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-drag-gaps', v: 1, gaps: [{ x: 100, w: 300 }] },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    await waitFor(() => expect(document.querySelectorAll('.host-drag-strip').length).toBeGreaterThan(0))

    await act(async () => { setFocusModeEnabled(true) })
    // A pane that has never reported keeps VISIBLE chrome (it may be a
    // pre-focus-mode install rendering its full header), so the strips stay.
    await waitFor(() => expect(document.querySelectorAll('.host-drag-strip').length).toBeGreaterThan(0))
    // Its first report brings the focus-mode steady state: chrome hidden, no
    // strips left to swallow the top band's hover.
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-focus-chrome', v: 1, on: false },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    await waitFor(() => expect(document.querySelectorAll('.host-drag-strip').length).toBe(0))

    // While the pane's header is PEEKED the strips must come back: the pane's
    // own -webkit-app-region CSS is inert (draggable regions are only collected
    // from the host document, never a cross-origin iframe), so these strips are
    // the only thing that lets the peeked header move the window.
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-focus-chrome', v: 1, on: true },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    await waitFor(() => expect(document.querySelectorAll('.host-drag-strip').length).toBeGreaterThan(0))
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-focus-chrome', v: 1, on: false },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    await waitFor(() => expect(document.querySelectorAll('.host-drag-strip').length).toBe(0))

    await act(async () => { setFocusModeEnabled(false) })
    await waitFor(() => expect(document.querySelectorAll('.host-drag-strip').length).toBeGreaterThan(0))
  })

  it('adopts focus mode from a pane and shares it back across every pane', async () => {
    // Focus mode belongs to the WINDOW, so a toggle driven inside one pane has to
    // become the host's value too — that is what makes the top-bar icon agree and
    // what carries the state to the OTHER panes on the next model broadcast. The
    // reverse direction (host -> pane) rides `mc-host-model.focusMode`.
    const { focusModeEnabled, __resetFocusMode } = await import('../hooks/useFocusMode')
    __resetFocusMode()
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: { 'cd-1': true } },
    })
    renderWithProviders(<InstancesViewport />, { store })

    expect(focusModeEnabled()).toBe(false)
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-set-focus-mode', v: 1, on: true },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    await waitFor(() => expect(focusModeEnabled()).toBe(true))

    // A malformed payload from a pane must not flip window state.
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-set-focus-mode', v: 1, on: 'yes' },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    expect(focusModeEnabled()).toBe(true)
    __resetFocusMode()
  })

  it('takes chrome visibility from the ACTIVE pane only', async () => {
    // A pane's peeked header is the only thing on screen when that pane fills the
    // window, so the host has to act on its report — the traffic lights are AppKit
    // views on THIS window and the pane cannot touch them. But only the active
    // pane may speak: a background pane's peek must not summon the lights over a
    // different pane's content.
    const { focusChromeVisible, setFocusChromeVisible, __resetFocusMode } = await import('../hooks/useFocusMode')
    __resetFocusMode()
    setFocusChromeVisible(false)
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: { 'cd-1': true } },
    })
    renderWithProviders(<InstancesViewport />, { store })

    expect(focusChromeVisible()).toBe(false)
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-focus-chrome', v: 1, on: true },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    await waitFor(() => expect(focusChromeVisible()).toBe(true))

    // A report from a pane that is NOT active is ignored.
    setFocusChromeVisible(false)
    // Separate act: the component tracks the active id in a ref written during
    // render, so the switch has to COMMIT before the message is delivered —
    // otherwise the listener still sees cd-1 as active and the test would pass
    // for the wrong reason.
    await act(async () => { store.dispatch(setActiveId(null)) })
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'mc-focus-chrome', v: 1, on: true },
        origin: 'http://127.0.0.1:7778',
      }))
    })
    expect(focusChromeVisible()).toBe(false)
    __resetFocusMode()
  })

  it('applies the incoming pane\'s chrome state on switch instead of the outgoing one\'s', async () => {
    // A switch necessarily happens from a PEEKED header — the tab bar lives on
    // it — so the window store holds `true` at that moment. The incoming pane's
    // chrome state did not change, so it re-posts nothing; without a switch-time
    // apply, the traffic lights stay stranded over the new pane until its next
    // hover cycle. The store must flip to the incoming pane's last-known state;
    // a pane that has NEVER reported defaults to visible (it may be a
    // pre-focus-mode install whose header never hides).
    const { focusModeEnabled, focusChromeVisible, setFocusChromeVisible, setFocusModeEnabled, __resetFocusMode } = await import('../hooks/useFocusMode')
    __resetFocusMode()
    setFocusModeEnabled(true)
    mockConnectedCd1()
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 'tok' }, 'cd-2': { port: 7779, token: 'tok2' } },
        activeId: 'cd-1', mru: ['cd-1', 'cd-2'], unread: {}, ready: { 'cd-1': true, 'cd-2': true },
      },
    })
    renderWithProviders(<InstancesViewport />, { store })
    try {
      // cd-1 reports hidden (its focus-mode steady state), then the user drives
      // the switch from a re-peeked tab bar: the store holds `false` for cd-1.
      await act(async () => {
        window.dispatchEvent(new MessageEvent('message', {
          data: { type: 'mc-focus-chrome', v: 1, on: false },
          origin: 'http://127.0.0.1:7778',
        }))
      })
      await waitFor(() => expect(focusChromeVisible()).toBe(false))

      // Switch to cd-2, which has never reported: the store must rise to the
      // visible default rather than keep cd-1's `false` — a non-conforming
      // pane renders its full header, and hiding the lights under it strands
      // a header that cannot drag the window.
      await act(async () => { store.dispatch(setActiveId('cd-2')) })
      await waitFor(() => expect(focusChromeVisible()).toBe(true))

      // Switching BACK re-applies cd-1's remembered state — its stored report,
      // not the default. The report was recorded even while cd-1 was inactive.
      await act(async () => { store.dispatch(setActiveId('cd-1')) })
      await waitFor(() => expect(focusChromeVisible()).toBe(false))

      // Focus mode OFF: a switch must NOT touch chrome state (it is
      // unconditionally visible and owned by the surfaces themselves).
      setFocusModeEnabled(false)
      expect(focusModeEnabled()).toBe(false)
      setFocusChromeVisible(true)
      await act(async () => { store.dispatch(setActiveId('cd-2')) })
      expect(focusChromeVisible()).toBe(true)
    } finally {
      __resetFocusMode()
    }
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

  it('stops re-minting after MAX_REACTIVE_REMINTS unanswered mc-auth-expired asks', async () => {
    // A pane whose session a fresh token cannot repair posts mc-auth-expired on
    // EVERY 403 it sees, forever. Without a budget that is one SSH mint every
    // REFRESH_MIN_INTERVAL_MS for as long as the window stays open — and each
    // re-mint used to postpone the load watchdog, so the user saw only a
    // spinner. The reactive path must go quiet and let the verdict stand.
    mockConnectedCd1()
    // Retry reconnects, and the pane keeps its loopback port — so pin the
    // connect mock to 7778 like every other mock here. The module default
    // answers 7777, which would move the pane's expected origin mid-test and
    // make the posts below cross-origin (silently dropped) rather than capped.
    vi.mocked(api.connectInstance).mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' })
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })

      const expire = async () => {
        await act(async () => {
          window.dispatchEvent(
            new MessageEvent('message', {
              data: { type: 'mc-auth-expired' },
              origin: 'http://127.0.0.1:7778',
            }),
          )
        })
        // Past the per-instance rate guard, so the throttle is never what stops us.
        await act(async () => { vi.advanceTimersByTime(11_000) })
      }

      for (let i = 0; i < 6; i++) await expire()
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(3)

      // `mc-embedded-ready` must NOT re-open the budget. EmbeddedHostBridge
      // posts it from a mount effect, BEFORE the pane's first authenticated
      // request, so a pane whose shell mounts and whose API then 403s posts it
      // on every reload — crediting it would clear the count once per re-mint
      // and leave the loop this cap exists to bound running forever.
      const ready = async () => {
        await act(async () => {
          window.dispatchEvent(
            new MessageEvent('message', {
              data: { type: 'mc-embedded-ready', v: 1 },
              origin: 'http://127.0.0.1:7778',
            }),
          )
        })
      }
      await ready()
      await expire()
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(3)

      // Only Retry re-opens it, and it does.
      await act(async () => { screen.getByRole('button', { name: /retry/i }).click() })
      await expire()
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(4)
    } finally {
      vi.useRealTimers()
    }
  })

  it('surfaces the error panel when the reactive budget runs out on a READY pane', async () => {
    // The exhausted ask can land while the pane counts as ready: its shell
    // mounted (mc-embedded-ready) and only its API is 403ing. The load watchdog
    // skips a ready pane and the child has latched its own hand-off, so simply
    // dropping the ask would leave a live-looking pane on stale content with no
    // affordance at all. Exhaustion must retract readiness and show Retry.
    mockConnectedCd1()
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })

      const post = async (data: unknown) => {
        await act(async () => {
          window.dispatchEvent(new MessageEvent('message', { data, origin: 'http://127.0.0.1:7778' }))
        })
      }

      // Every cycle re-announces readiness, exactly as a real reload does.
      for (let i = 0; i < 4; i++) {
        await post({ type: 'mc-embedded-ready', v: 1 })
        await post({ type: 'mc-auth-expired' })
        await act(async () => { vi.advanceTimersByTime(11_000) })
      }

      // Capped at 3 mints even though the pane reported ready before every ask.
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(3)
      // And the pane is no longer passing for loaded — Retry is reachable.
      expect(store.getState().instances.ready['cd-1']).toBeUndefined()
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not spend the reactive budget on asks the rate guard drops', async () => {
    // The budget bounds MINTS, not asks. A pane can post several asks inside one
    // rate window — a 200 landing mid-reload re-arms the child's hand-off latch,
    // so a 403 from a poll that started before the reload posts again seconds
    // later — and refreshToken drops those. Charging for a mint that never
    // happened would fail the pane after one real retry instead of three.
    mockConnectedCd1()
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })

      const post = async (data: unknown) => {
        await act(async () => {
          window.dispatchEvent(new MessageEvent('message', { data, origin: 'http://127.0.0.1:7778' }))
        })
      }
      const nextWindow = async () => { await act(async () => { vi.advanceTimersByTime(11_000) }) }

      // Ready throughout, so the 15s load watchdog is never what shows the panel
      // — only the budget can. The mock re-mints the SAME token, so setWarm does
      // not clear readiness between asks.
      await post({ type: 'mc-embedded-ready', v: 1 })

      // Three asks inside ONE rate window: the first mints, the other two are
      // dropped by the guard and must cost nothing.
      await post({ type: 'mc-auth-expired' })
      await post({ type: 'mc-auth-expired' })
      await post({ type: 'mc-auth-expired' })
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(1)

      // Two more windows spend the rest of the budget. Six asks, three mints.
      await nextWindow()
      await post({ type: 'mc-auth-expired' })
      await nextWindow()
      await post({ type: 'mc-auth-expired' })
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(3)
      // Still alive: the dropped asks did not bring the panel forward.
      expect(store.getState().instances.ready['cd-1']).toBe(true)
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()

      // The next ask is the one that finds the budget spent.
      await nextWindow()
      await post({ type: 'mc-auth-expired' })
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(3)
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-warming a pane restores its budget instead of failing on the first ask', async () => {
    // Both the budget and the timed-out verdict describe ONE load of ONE
    // connection. Eviction and disconnect end that connection, and a re-warm is
    // a new load — but only Retry used to clear either, and a re-warm is exactly
    // the path that skips Retry. Left stale, a healthy re-warm renders "Pane
    // failed to load" before its fresh iframe has had a chance to load at all.
    mockConnectedCd1()
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })

      const post = async (data: unknown) => {
        await act(async () => {
          window.dispatchEvent(new MessageEvent('message', { data, origin: 'http://127.0.0.1:7778' }))
        })
      }
      await post({ type: 'mc-embedded-ready', v: 1 })
      for (let i = 0; i < 4; i++) {
        await post({ type: 'mc-auth-expired' })
        await act(async () => { vi.advanceTimersByTime(11_000) })
      }
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(3)
      expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()

      // The same teardown InstancesPanel and the K-cap eviction dispatch, then a
      // fresh warm for the same id.
      await act(async () => { store.dispatch(removeWarm('cd-1')) })
      await act(async () => { store.dispatch(setWarm({ id: 'cd-1', conn: { port: 7778, token: 'tok2' } })) })
      await act(async () => { store.dispatch(setActiveId('cd-1')) })

      // The stale verdict is gone: the new load gets its loading overlay, not the
      // error panel it never earned.
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
      expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()
      // And its first ask is answered with a mint rather than instant exhaustion.
      await post({ type: 'mc-auth-expired' })
      expect(vi.mocked(api.refreshInstanceToken).mock.calls.length).toBe(4)
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores mc-switch-instance to an unknown target id even from a trusted origin', async () => {
    // The inbound switcher validates the TARGET (known instance OR warm) after
    // resolving the SENDER origin. A trusted pane must NOT be able to flip the
    // active tab to an id the parent does not know (spoofed/unknown target).
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: null, mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    // Wait until the warm→port map is live so the origin resolves as trusted.
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'mc-switch-instance', id: 'ghost-instance' },
          origin: 'http://127.0.0.1:7778', // resolves to the warm cd-1 tunnel (trusted)
        }),
      )
    })
    // Unknown target -> no switch; activeId stays on Local (null).
    expect(store.getState().instances.activeId).toBeNull()
  })

  it('ignores mc-switch-instance to a valid target from an untrusted origin', async () => {
    // A valid target id delivered from an origin that does NOT resolve to any
    // warm tunnel must be dropped at the origin gate (resolveTunnelOrigin ->
    // null), before the target is ever inspected.
    mockConnectedCd1()
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: null, mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())

    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'mc-switch-instance', id: 'cd-1' }, // a real, known target
          origin: 'https://evil.example.com', // but an untrusted origin
        }),
      )
    })
    // Untrusted origin -> handler bails; activeId unchanged (still Local).
    expect(store.getState().instances.activeId).toBeNull()
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
    expect(await screen.findByRole('group', { name: /Remote instances/i })).toBeInTheDocument()
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

  it('still times out when a re-mint churns the token faster than the watchdog window', async () => {
    // Regression: the watchdog's countdown used to restart on every token change.
    // Re-mints are rate-limited to REFRESH_MIN_INTERVAL_MS (10s), which is SHORTER
    // than PANE_LOAD_TIMEOUT_MS (15s), so a pane stuck in an auth-expired -> re-mint
    // loop reset the clock before it could ever fire: the loading overlay spun
    // forever and Retry was unreachable. The deadline is now absolute per load.
    mockConnectedCd1()
    vi.useFakeTimers()
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok-0' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()

      // Two re-mints inside the 15s window, 10s apart — same port, new token each
      // time (exactly what the auth-expired path dispatches).
      await act(async () => {
        vi.advanceTimersByTime(10_000)
        store.dispatch(setWarm({ id: 'cd-1', conn: { port: 7778, token: 'tok-1' } }))
      })
      expect(screen.queryByText(/Pane failed to load/i)).toBeNull()
      await act(async () => {
        vi.advanceTimersByTime(4_000)
        store.dispatch(setWarm({ id: 'cd-1', conn: { port: 7778, token: 'tok-2' } }))
      })
      // 14s elapsed: still inside the ORIGINAL deadline, so not yet a failure.
      expect(screen.queryByText(/Pane failed to load/i)).toBeNull()

      // Crossing 15s of real elapsed time fires, despite the churn.
      await act(async () => {
        vi.advanceTimersByTime(1_000)
      })
      expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
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
