// The reactive token re-mint budget, the mc-switch-instance gate, the load-timeout
// verdict, the iframe-mounted journal and mc-embedded-boot attribution are tested in
// InstancesViewport.paneLoad.test.tsx; both suites share InstancesViewportTestUtils.tsx.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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
import { mockConnectedCd1 } from './InstancesViewportTestUtils'

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
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'added-1' }),
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
    // Auto-warm is connected-only: it pre-mounts a tunnel that is already up
    // and never asks the gateway to bring one up.
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1', { onlyIfConnected: true }))
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

  it('delegates microphone, fullscreen, clipboard-write and display-capture to the cross-origin pane', async () => {
    // The pane is a cross-origin iframe (same host, different port), where
    // these features are denied unless the parent delegates them. Dropping
    // any of them regresses a user-visible capability: mic -> getUserMedia
    // rejects with NotAllowedError; fullscreen -> the fullscreen button on
    // native <video> controls inside the embedded chat renders disabled;
    // clipboard-write -> navigator.clipboard.writeText() rejects in the pane,
    // so every copy affordance fails (CliPanel's selection copy surfaces
    // "Copy failed"; TerminalKeyBar and WebAppArtifactCard hit the same
    // rejection); display-capture -> getDisplayMedia() rejects in the pane,
    // so the snip affordances render (the presence gate,
    // isScreenSnipSupported, only checks the function EXISTS -- true inside
    // iframes) but every click dies with NotAllowedError (ChatPage's snip
    // flow, WebPreviewPanel's crop-to-chat, MochiSnipHost).
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const frame = await waitFor(() => {
      const f = document.querySelector('iframe')
      if (!f) throw new Error('no iframe yet')
      return f as HTMLIFrameElement
    })
    expect(frame.getAttribute('allow')).toBe('microphone; fullscreen; clipboard-write; display-capture')
    // Contract pin: whatever shape the delegation list takes in the future,
    // clipboard-write must survive it -- the pane's copy paths depend on it.
    expect(frame.getAttribute('allow')).toContain('clipboard-write')
    // Contract pin: display-capture must survive future list reshapes too --
    // the pane's snip paths depend on it, and delegating it only lets the pane
    // ASK: a browser runs its own source picker, and in the packaged app
    // capture-trust.js authorizes by identity, so a pane's request is refused
    // there whether it asks from this iframe or navigates the top frame
    // (electron/test/capture-trust.test.js pins both deny paths).
    expect(frame.getAttribute('allow')).toContain('display-capture')
    // Scope discipline: clipboard-read stays undelegated. Read is the more
    // sensitive grant class and exceeds this fix's clipboard-write scope, so
    // the pane's Paste key (TerminalKeyBar's readText) still fails inside
    // embedded panes, visibly, with its named paste_permission_needed status.
    // Delegating read is a maintainer decision; this pin makes a future grant
    // a deliberate act rather than a drive-by.
    expect(frame.getAttribute('allow')).not.toContain('clipboard-read')
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

  it('the Settings → Remote Crew link returns to Local before navigating (same overlay rule as the hand-off)', async () => {
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

    const link = screen.getByRole('link', { name: /Settings → Remote Crew/ }) as HTMLAnchorElement
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
    const bar = await screen.findByRole('group', { name: /Remote crews/i })
    expect(bar).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /Switch crew/i }))
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

    const bar = await screen.findByRole('group', { name: /Remote crews/i })
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
    const bar = await screen.findByRole('group', { name: /Remote crews/i })
    expect(bar).toBeInTheDocument()
    // Not the error panel — no Retry while the load is still in flight.
    expect(screen.queryByText(/Connection error/i)).toBeNull()
  })

  it('keeps a host drag strip over the loading and connection-error overlays so macOS can still drag the window', async () => {
    // Regression (macOS window-drag bug): on frameless macOS the window is
    // dragged SOLELY by `.host-drag-strip` divs, and the per-pane strips are
    // gated off once an overlay is up. Both the loading overlay and the
    // error/disconnected panel cover the top band with an opaque `bg-bg` layer
    // over a still-mounted iframe, so without an overlay strip the title bar
    // becomes un-draggable while a pane is connecting or has failed. Each
    // overlay must carry its own `overlay-drag-strip`.

    // Connecting/loading: warm + connected but not yet ready → loading overlay.
    mockConnectedCd1()
    const loadingStore = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    const { unmount } = renderWithProviders(<InstancesViewport />, { store: loadingStore })
    expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('overlay-drag-strip')).toHaveClass('host-drag-strip'))
    unmount()

    // Connection error: active, non-warm, status error → error panel overlay.
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
    const errorStore = createTestStore({
      instances: { warm: {}, activeId: 'cd-1', mru: ['cd-1'], unread: {} },
    })
    renderWithProviders(<InstancesViewport />, { store: errorStore })
    expect(await screen.findByText(/Connection error/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('overlay-drag-strip')).toHaveClass('host-drag-strip'))
    // Retry stays clickable under the strip (injected no-drag rule).
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
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

  describe('off-window cursor relay (mc-cursor-away-watch)', () => {
    // An embedded pane's focus-mode reveal is dismissed on the cursor's DISTANCE
    // from the window, which only the Electron main process can measure — and a
    // cross-origin pane has no preload to ask it. The host watches on its behalf.
    function installBridge() {
      const bridge = {
        watches: 0,
        stops: 0,
        reply: null as ((away: boolean) => void) | null,
      }
      ;(window as Window & { electronAPI?: unknown }).electronAPI = {
        watchCursorAway: (cb: (away: boolean) => void) => {
          bridge.watches += 1
          bridge.reply = cb
          return () => { bridge.stops += 1; bridge.reply = null }
        },
      }
      return bridge
    }
    afterEach(() => { delete (window as Window & { electronAPI?: unknown }).electronAPI })

    const frameFor = (port: number) =>
      [...document.querySelectorAll('iframe')].find(f => f.src.includes(`:${port}`)) as HTMLIFrameElement
    const send = (data: unknown, port: number, source: Window | null) =>
      act(async () => {
        window.dispatchEvent(new MessageEvent('message', {
          data, origin: `http://127.0.0.1:${port}`, source,
        }))
      })
    async function renderTwoPanes() {
      mockConnectedCd1()
      const store = createTestStore({
        instances: {
          warm: { 'cd-1': { port: 7778, token: 'tok' }, 'cd-2': { port: 7779, token: 'tok2' } },
          activeId: 'cd-1', mru: ['cd-1', 'cd-2'], unread: {}, ready: { 'cd-1': true, 'cd-2': true },
        },
      })
      const view = renderWithProviders(<InstancesViewport />, { store })
      await waitFor(() => expect(frameFor(7778)).toBeDefined())
      await waitFor(() => expect(frameFor(7779)).toBeDefined())
      // happy-dom never loads a cross-origin frame, so give each iframe a stand-in
      // window the host can address and a message can name as its source.
      const fakeWindow = (el: HTMLIFrameElement) => {
        const w = { postMessage: vi.fn() } as unknown as Window
        Object.defineProperty(el, 'contentWindow', { configurable: true, get: () => w })
        return w
      }
      const w1 = fakeWindow(frameFor(7778))
      const w2 = fakeWindow(frameFor(7779))
      const post1 = vi.mocked(w1.postMessage)
      const post2 = vi.mocked(w2.postMessage)
      return { store, view, w1, w2, post1, post2 }
    }
    const watch = (id: string) => ({ type: 'mc-cursor-away-watch', v: 1, id })

    it('watches for the active pane and forwards the one answer to its own origin', async () => {
      const bridge = installBridge()
      const { w1, post1 } = await renderTwoPanes()

      await send(watch('w-1'), 7778, w1)
      expect(bridge.watches).toBe(1)
      expect(post1).not.toHaveBeenCalled()

      act(() => { bridge.reply!(true) })
      expect(post1).toHaveBeenLastCalledWith(
        { v: 1, id: 'w-1', type: 'mc-cursor-away', away: true }, 'http://127.0.0.1:7778',
      )
      // Never broadcast: every post is addressed to the pane's exact origin.
      for (const call of post1.mock.calls) expect(call[1]).not.toBe('*')
    })

    it('ignores a background pane and a request from the wrong frame', async () => {
      const bridge = installBridge()
      const { w1, w2, post1, post2 } = await renderTwoPanes()

      // cd-2 is not the pane on screen: no watch, no answer.
      await send(watch('w-bg'), 7779, w2)
      // cd-1's origin, but the message came from another frame's window.
      await send(watch('w-spoof'), 7778, w2)
      // Right frame, unknown protocol version.
      await send({ ...watch('w-v2'), v: 2 }, 7778, w1)
      expect(bridge.watches).toBe(0)
      expect(post1).not.toHaveBeenCalled()
      expect(post2).not.toHaveBeenCalled()
    })

    it('disarms on the pane\'s cancel and on a pane switch', async () => {
      const bridge = installBridge()
      const { store, w1 } = await renderTwoPanes()

      await send(watch('w-1'), 7778, w1)
      // A cancel naming a different watch leaves the live one alone.
      await send({ type: 'mc-cursor-away-cancel', v: 1, id: 'other' }, 7778, w1)
      expect(bridge.stops).toBe(0)
      await send({ type: 'mc-cursor-away-cancel', v: 1, id: 'w-1' }, 7778, w1)
      expect(bridge.stops).toBe(1)

      await send(watch('w-2'), 7778, w1)
      expect(bridge.watches).toBe(2)
      await act(async () => { store.dispatch(setActiveId('cd-2')) })
      expect(bridge.stops).toBe(2)
    })

    it('stays silent without a native bridge', async () => {
      const { w1, post1 } = await renderTwoPanes()
      await send(watch('w-1'), 7778, w1)
      expect(post1).not.toHaveBeenCalled()
    })
  })

  describe('image copy for a pane (mc-clipboard-image, #14854)', () => {
    // The pane's own async Clipboard API is refused by this document's
    // `clipboard-write=(self)` header, and an image has no execCommand path, so
    // this frame writes on the pane's behalf -- but only for the pane's own
    // frame, only while it is on screen, and only right after a gesture INSIDE
    // it (live activation + the host's focus on that iframe).
    const nav = navigator as Navigator & { userActivation?: unknown }
    let savedActivation: PropertyDescriptor | undefined
    let write: ReturnType<typeof vi.fn>
    beforeEach(() => {
      savedActivation = Object.getOwnPropertyDescriptor(nav, 'userActivation')
      Object.defineProperty(nav, 'userActivation', { configurable: true, value: { isActive: true } })
      class FakeClipboardItem { constructor(public items: Record<string, unknown>) {} }
      vi.stubGlobal('ClipboardItem', FakeClipboardItem)
      write = vi.fn(async () => {})
      Object.defineProperty(nav, 'clipboard', { configurable: true, value: { write } })
    })
    afterEach(() => {
      if (savedActivation) Object.defineProperty(nav, 'userActivation', savedActivation)
      else delete (nav as { userActivation?: unknown }).userActivation
      delete (nav as { clipboard?: unknown }).clipboard
      delete (document as { activeElement?: unknown }).activeElement
      vi.unstubAllGlobals()
    })

    const frameFor = (port: number) =>
      [...document.querySelectorAll('iframe')].find(f => f.src.includes(`:${port}`)) as HTMLIFrameElement
    const send = (data: unknown, port: number, source: Window | null) =>
      act(async () => {
        window.dispatchEvent(new MessageEvent('message', {
          data, origin: `http://127.0.0.1:${port}`, source,
        }))
      })
    // happy-dom never loads iframes or moves focus into one, so the host's
    // focused element is set directly: under test is the gate, not focus.
    const focusOn = (el: Element | null) =>
      Object.defineProperty(document, 'activeElement', { configurable: true, get: () => el })
    async function renderTwoPanes() {
      mockConnectedCd1()
      const store = createTestStore({
        instances: {
          warm: { 'cd-1': { port: 7778, token: 'tok' }, 'cd-2': { port: 7779, token: 'tok2' } },
          activeId: 'cd-1', mru: ['cd-1', 'cd-2'], unread: {}, ready: { 'cd-1': true, 'cd-2': true },
        },
      })
      renderWithProviders(<InstancesViewport />, { store })
      await waitFor(() => expect(frameFor(7778)).toBeDefined())
      await waitFor(() => expect(frameFor(7779)).toBeDefined())
      const fakeWindow = (el: HTMLIFrameElement) => {
        const w = { postMessage: vi.fn() } as unknown as Window
        Object.defineProperty(el, 'contentWindow', { configurable: true, get: () => w })
        return w
      }
      const w1 = fakeWindow(frameFor(7778))
      const w2 = fakeWindow(frameFor(7779))
      return { store, w1, w2, post1: vi.mocked(w1.postMessage), post2: vi.mocked(w2.postMessage) }
    }
    const pngBlob = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })
    const req = (id: string, over: Record<string, unknown> = {}) =>
      ({ type: 'mc-clipboard-image', v: 1, id, png: pngBlob(), ...over })
    const result = (id: string, ok: boolean) => ({ type: 'mc-clipboard-image-result', v: 1, id, ok })

    it('advertises the relay in the model it sends each pane', async () => {
      const { w1, post1 } = await renderTwoPanes()
      await send({ type: 'mc-embedded-ready', v: 1 }, 7778, w1)
      expect(post1).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'mc-host-model', clipboardRelay: 1 }), 'http://localhost:7778',
      )
    })

    it('writes the image for a gesture in the active pane and answers at its exact origin', async () => {
      const { w1, post1 } = await renderTwoPanes()
      focusOn(frameFor(7778))
      await send(req('c-1', { text: 'caption' }), 7778, w1)
      await waitFor(() => expect(post1).toHaveBeenCalledWith(result('c-1', true), 'http://127.0.0.1:7778'))
      expect(write).toHaveBeenCalledOnce()
      const item = (write.mock.calls[0][0] as Array<{ items: Record<string, unknown> }>)[0].items
      expect(Object.keys(item).sort()).toEqual(['image/png', 'text/plain'])
      for (const call of post1.mock.calls) expect(call[1]).not.toBe('*')
    })

    it('refuses without a gesture inside that pane, says so, and journals which check refused', async () => {
      const info = vi.spyOn(console, 'info').mockImplementation(() => {})
      const { w1, post1 } = await renderTwoPanes()
      // Activation without focus on the pane: the click was in the host itself
      // (its composer, its tab bar), not in the pane asking.
      focusOn(document.body)
      await send(req('c-host-click'), 7778, w1)
      // Focus on the pane but no live activation: the pane posting on its own,
      // or an image render that outlived the click's activation window.
      focusOn(frameFor(7778))
      Object.defineProperty(nav, 'userActivation', { configurable: true, value: { isActive: false } })
      await send(req('c-no-gesture'), 7778, w1)
      expect(write).not.toHaveBeenCalled()
      expect(post1).toHaveBeenCalledWith(result('c-host-click', false), 'http://127.0.0.1:7778')
      expect(post1).toHaveBeenCalledWith(result('c-no-gesture', false), 'http://127.0.0.1:7778')
      const journal = info.mock.calls.map(c => String(c[0])).filter(l => l.includes('clipboard-relay-refused'))
      expect(journal).toEqual([
        '[pane] clipboard-relay-refused id=cd-1 activePane=true activation=true focusOnPane=false',
        '[pane] clipboard-relay-refused id=cd-1 activePane=true activation=false focusOnPane=true',
      ])
    })

    it('answers false and journals it when the host write itself is refused', async () => {
      const info = vi.spyOn(console, 'info').mockImplementation(() => {})
      write.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
      const { w1, post1 } = await renderTwoPanes()
      focusOn(frameFor(7778))
      await send(req('c-write-refused'), 7778, w1)
      await waitFor(() => expect(post1).toHaveBeenCalledWith(result('c-write-refused', false), 'http://127.0.0.1:7778'))
      expect(info.mock.calls.map(c => String(c[0]))).toContain('[pane] clipboard-relay-write-failed id=cd-1')
    })

    it('refuses a background pane even with focus on its frame', async () => {
      const { w2, post2 } = await renderTwoPanes()
      focusOn(frameFor(7779))
      await send(req('c-bg'), 7779, w2)
      expect(write).not.toHaveBeenCalled()
      expect(post2).toHaveBeenCalledWith(result('c-bg', false), 'http://127.0.0.1:7779')
    })

    it('ignores a request from the wrong frame, an unknown origin or a malformed payload', async () => {
      const { w1, w2, post1, post2 } = await renderTwoPanes()
      focusOn(frameFor(7778))
      // cd-1's origin, but posted from another frame's window.
      await send(req('c-spoof'), 7778, w2)
      // A loopback port this host has no warm pane on.
      await send(req('c-stranger'), 7790, w1)
      // Right frame, but not a PNG / unknown version.
      await send(req('c-text', { png: new Blob(['x'], { type: 'text/plain' }) }), 7778, w1)
      await send(req('c-v2', { v: 2 }), 7778, w1)
      expect(write).not.toHaveBeenCalled()
      expect(post1).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'mc-clipboard-image-result' }), expect.anything())
      expect(post2).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'mc-clipboard-image-result' }), expect.anything())
    })
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

  it('every auto-warm is connected-only, so one that fires after a disconnect is declined and warms nothing', async () => {
    // Two connected crews: cd-1 warms at once, cd-2 is scheduled 1.5 s later.
    const two = {
      instances: [
        { id: 'cd-1', name: 'One', ssh_host: 'a', remote_port: 7777, local_port: 7778, ttl: '20h', remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 } },
        { id: 'cd-2', name: 'Two', ssh_host: 'b', remote_port: 7777, local_port: 7779, ttl: '20h', remote_bin: '',
          status: { instance_id: 'cd-2', state: 'connected', local_port: 7779, remote_port: 7777 } },
      ],
      warm_set_cap: 5,
    }
    vi.mocked(api.listInstances).mockResolvedValue(two as never)
    // The gateway is the arbiter: cd-1 is up; cd-2 was disconnected before its
    // warm arrived, so the connected-only connect declines it (200, state
    // disconnected, no token) instead of re-opening the tunnel.
    vi.mocked(api.connectInstance).mockImplementation(async (id: string, opts?: { onlyIfConnected?: boolean }) => {
      expect(opts).toEqual({ onlyIfConnected: true })
      return id === 'cd-1'
        ? ({ state: 'connected', local_port: 7778, token: 'tok' } as never)
        : ({ state: 'disconnected', local_port: 0, remote_port: 7777, code: 'instance_not_connected' } as never)
    })
    const { store } = renderWithProviders(<InstancesViewport />)
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1', { onlyIfConnected: true }))
    expect(api.connectInstance).not.toHaveBeenCalledWith('cd-2', expect.anything())
    // The stagger timer was armed under real timers at render; let it fire.
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-2', { onlyIfConnected: true }), { timeout: 4_000 })
    await new Promise(r => setTimeout(r, 20))
    expect(store.getState().instances.warm['cd-1']).toBeDefined()
    expect(store.getState().instances.warm['cd-2']).toBeUndefined()
  })

  it('a staggered auto-warm skips a crew something else already warmed in the meantime', async () => {
    const two = {
      instances: [
        { id: 'cd-1', name: 'One', ssh_host: 'a', remote_port: 7777, local_port: 7778, ttl: '20h', remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 } },
        { id: 'cd-2', name: 'Two', ssh_host: 'b', remote_port: 7777, local_port: 7779, ttl: '20h', remote_bin: '',
          status: { instance_id: 'cd-2', state: 'connected', local_port: 7779, remote_port: 7777 } },
      ],
      warm_set_cap: 5,
    }
    vi.mocked(api.listInstances).mockResolvedValue(two as never)
    vi.mocked(api.connectInstance).mockImplementation(async (id: string) => ({
      state: 'connected', local_port: id === 'cd-1' ? 7778 : 7779, token: 'tok',
    }) as never)
    const { store } = renderWithProviders(<InstancesViewport />)
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1', { onlyIfConnected: true }))
    // A click (or the fan-out) warms cd-2 before its stagger slot.
    act(() => { store.dispatch(setWarm({ id: 'cd-2', conn: { port: 7779, token: 'tok' } })) })
    await new Promise(r => setTimeout(r, 1_800))
    expect(api.connectInstance).not.toHaveBeenCalledWith('cd-2', expect.anything())
  })

  it('Retry after a watchdog verdict on a navigated frame asks the gateway to rebuild the tunnel', async () => {
    mockConnectedCd1()
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
    vi.useRealTimers()
    // happy-dom's disabled-iframe load leaves the frame a same-origin blank
    // document; make it read as navigated (cross-origin), the one case rebuild
    // is meant for.
    const frame = document.querySelector('iframe') as HTMLIFrameElement
    Object.defineProperty(frame, 'contentWindow', {
      get: () => ({ get location() { throw new DOMException('blocked', 'SecurityError') } }),
    })

    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1', { rebuild: true }))
  })

  it("Retry's own failure surfaces on the active pane's panel (variables keyed by id)", async () => {
    // Stale warm: an iframe is mounted but the backend says the tunnel is down,
    // so the panel is up and shows the LIST's error. Retry then fails with a
    // NEWER reason; the mutation's error for THIS crew must win.
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [
        {
          id: 'cd-1', name: 'Cloud One', ssh_host: 'cd-1-alias', remote_port: 7777, local_port: 0, ttl: '20h', remote_bin: '',
          was_connected: true,
          status: { instance_id: 'cd-1', state: 'error', error: 'ssh unreachable', remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    })
    vi.mocked(api.connectInstance).mockRejectedValue(new Error('rebuild refused: EPERM'))
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
    })
    renderWithProviders(<InstancesViewport />, { store })
    expect(await screen.findByText(/ssh unreachable/i)).toBeInTheDocument()
    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
    expect(await screen.findByText(/rebuild refused: EPERM/i)).toBeInTheDocument()
    expect(screen.queryByText(/ssh unreachable/i)).toBeNull()
  })


  describe('script-error heal (poisoned HTTP cache)', () => {
    // The pane's index.html shell posts this when its entry <script type=module>
    // fires `error`: a chunk in the graph failed to fetch. On the desktop the one
    // cause a plain reload cannot fix is a 404 the shell's HTTP cache is
    // replaying under that origin, so the parent evicts the origin's cache and
    // reloads -- exactly once per Retry, so a 404 the gateway is really serving
    // cannot spin clear/reload forever.
    const scriptError = (origin = 'http://127.0.0.1:7778') =>
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'mc-embedded-boot', v: 1, stage: 'script-error', src: `${origin}/assets/main-krfrL6rl.js?token=x` },
          origin,
        }),
      )
    const withBridge = (impl: (origin: string) => Promise<boolean>) => {
      const clear = vi.fn(impl)
      ;(window as unknown as { electronAPI: unknown }).electronAPI = { clearPaneHttpCache: clear }
      return clear
    }
    afterEach(() => {
      delete (window as unknown as { electronAPI?: unknown }).electronAPI
    })

    it('evicts the pane origin cache and reloads the iframe once, journaling the src without its query', async () => {
      mockConnectedCd1()
      const clear = withBridge(async () => true)
      const info = vi.spyOn(console, 'info').mockImplementation(() => {})
      try {
        const store = createTestStore({
          instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
        })
        renderWithProviders(<InstancesViewport />, { store })
        expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
        const before = document.querySelector('iframe') as HTMLIFrameElement

        scriptError()
        await waitFor(() => expect(clear).toHaveBeenCalledWith('http://127.0.0.1:7778'))
        await waitFor(() => expect(document.querySelector('iframe')).not.toBe(before))
        const lines = info.mock.calls.map(c => String(c[0]))
        const boot = lines.find(l => l.includes('boot id=cd-1 stage=script-error'))
        expect(boot).toBeDefined()
        expect(boot).toContain('src=http://127.0.0.1:7778/assets/main-krfrL6rl.js?token=<redacted>')
        expect(boot).not.toContain('token=x')
        expect(lines.some(l => l.includes('script-error-heal id=cd-1 origin=http://127.0.0.1:7778'))).toBe(true)

        // A second script-error on the same load is NOT healed again: the
        // watchdog owns it from here, and only Retry re-opens the budget.
        const healed = document.querySelector('iframe') as HTMLIFrameElement
        scriptError()
        await new Promise(r => setTimeout(r, 20))
        expect(clear).toHaveBeenCalledTimes(1)
        expect(document.querySelector('iframe')).toBe(healed)
      } finally {
        info.mockRestore()
      }
    })

    it('retracts readiness on script-error so the watchdog can arm again for a pane that WAS ready', async () => {
      // A ready pane reloads itself (bundle change) and lands on a chunk 404: its
      // shell posts script-error while the store still says ready. Left alone,
      // the watchdog stays off (it skips ready panes) and a failed heal would be
      // a blank pane with no Retry.
      mockConnectedCd1()
      const clear = withBridge(async () => true)
      const store = createTestStore({
        instances: {
          warm: { 'cd-1': { port: 7778, token: 'tok' } },
          activeId: 'cd-1',
          mru: ['cd-1'],
          unread: {},
          ready: { 'cd-1': true },
        },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(screen.queryByText(/Loading pane/i)).toBeNull()
      scriptError()
      await waitFor(() => expect(store.getState().instances.ready['cd-1']).toBeUndefined())
      await waitFor(() => expect(clear).toHaveBeenCalledTimes(1))
      // Readiness retracted -> the loading overlay is back and the watchdog owns the pane.
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
      // The retraction is not gated on the heal budget: a second script-error
      // (budget spent, no reload) still leaves the pane un-ready.
      window.dispatchEvent(
        new MessageEvent('message', { data: { type: 'mc-embedded-ready', v: 1 }, origin: 'http://127.0.0.1:7778' }),
      )
      await waitFor(() => expect(store.getState().instances.ready['cd-1']).toBe(true))
      scriptError()
      await waitFor(() => expect(store.getState().instances.ready['cd-1']).toBeUndefined())
      expect(clear).toHaveBeenCalledTimes(1)
    })

    it('gives an evicted-then-re-warmed pane its one-shot heal back', async () => {
      // The budget is per load. Eviction (removeWarm) ends the load; a later
      // re-warm is a new one and must be able to heal a poisoned cache again
      // without a manual Retry.
      mockConnectedCd1()
      const clear = withBridge(async () => true)
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
      scriptError()
      await waitFor(() => expect(clear).toHaveBeenCalledTimes(1))
      // Budget spent for this load.
      scriptError()
      await new Promise(r => setTimeout(r, 20))
      expect(clear).toHaveBeenCalledTimes(1)

      await act(async () => { store.dispatch(removeWarm('cd-1')) })
      await waitFor(() => expect(document.querySelector('iframe')).toBeNull())
      await act(async () => { store.dispatch(setWarm({ id: 'cd-1', conn: { port: 7778, token: 'tok2' } })) })
      await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
      scriptError()
      await waitFor(() => expect(clear).toHaveBeenCalledTimes(2))
    })

    it('still reloads once when the bridge is absent or refuses (browser, non-Electron)', async () => {
      mockConnectedCd1()
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
      const before = document.querySelector('iframe') as HTMLIFrameElement
      scriptError()
      await waitFor(() => expect(document.querySelector('iframe')).not.toBe(before))
    })

    it('ignores a script-error from an origin that is not a warm pane', async () => {
      mockConnectedCd1()
      const clear = withBridge(async () => true)
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
      const before = document.querySelector('iframe') as HTMLIFrameElement
      scriptError('http://127.0.0.1:9999')
      scriptError('https://evil.example.com')
      await new Promise(r => setTimeout(r, 20))
      expect(clear).not.toHaveBeenCalled()
      expect(document.querySelector('iframe')).toBe(before)
    })

    it('Retry evicts the pane origin cache before reloading and re-opens the heal budget', async () => {
      mockConnectedCd1()
      vi.mocked(api.connectInstance).mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' })
      const clear = withBridge(async () => true)
      vi.useFakeTimers()
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      await act(async () => {
        vi.advanceTimersByTime(15_000)
      })
      expect(screen.getByText(/Pane failed to load/i)).toBeInTheDocument()
      vi.useRealTimers()

      const u = userEvent.setup()
      await u.click(screen.getByRole('button', { name: /Retry/i }))
      await waitFor(() => expect(clear).toHaveBeenCalledWith(`http://${window.location.hostname}:7778`))
      await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('cd-1'))
      // The evict resolved BEFORE the reconnect was issued.
      expect(clear.mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.connectInstance).mock.invocationCallOrder[0])
      await waitFor(() => expect(screen.queryByText(/Pane failed to load/i)).toBeNull())

      // Budget re-opened: a script-error after Retry is healed again.
      const before = document.querySelector('iframe') as HTMLIFrameElement
      scriptError()
      await waitFor(() => expect(clear).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(document.querySelector('iframe')).not.toBe(before))
    })
  })

  describe('a chained crew re-announcing on a NEW hop port', () => {
    it('rewrites the warm entry from the connect response instead of keeping the old port', async () => {
      // The defect this pins: the repoint PATCHed `via_remote_port`, which tears the
      // tunnel down, and then called the api directly and DISCARDED the status. So
      // `warm[id]` kept the old local port paired with the token minted for it, and
      // auto-warm skips any id already warm -- nothing healed it until Retry.
      //
      // Asserted on the entry's PORT, not on which function was called, so it fails
      // in both directions: with the response discarded the old port survives, and
      // with the response applied it cannot.
      vi.mocked(api.listInstances).mockResolvedValue({
        instances: [
          {
            id: 'cd-1',
            name: 'Parent',
            ssh_host: 'cd-1-alias',
            remote_port: 7777,
            local_port: 7778,
            ttl: '20h',
            remote_bin: '',
            status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
          },
          {
            id: 'cd-2',
            name: 'Chained',
            ssh_host: 'cd-2-alias',
            remote_port: 5476,
            local_port: 50001,
            ttl: '20h',
            remote_bin: '',
            via_instance_id: 'cd-1',
            via_remote_id: 'c-2',
            via_remote_port: 40001,
            status: { instance_id: 'cd-2', state: 'connected', local_port: 50001, remote_port: 5476 },
          },
        ],
        warm_set_cap: 5,
      } as unknown as Awaited<ReturnType<typeof api.listInstances>>)
      // The reconnect after the PATCH lands on a FRESH local port with its own token.
      vi.mocked(api.connectInstance).mockResolvedValue({
        state: 'connected',
        local_port: 50002,
        token: 'new-tok',
      } as unknown as Awaited<ReturnType<typeof api.connectInstance>>)

      const store = createTestStore({
        instances: {
          // cd-1 is the announcing pane, warm at 7778 -- that is what makes its
          // origin resolve. cd-2 carries the STALE pair the defect leaves behind.
          warm: { 'cd-1': { port: 7778, token: 'tok' }, 'cd-2': { port: 50001, token: 'old-tok' } },
          activeId: 'cd-1',
          mru: ['cd-1'],
          unread: {},
          ready: {},
        },
      })
      renderWithProviders(<InstancesViewport />, { store })
      await waitFor(() => expect(api.listInstances).toHaveBeenCalled())
      // Both panes mounted means the list reached the component, so the row the
      // announce has to find is in `instancesRef` before the message arrives.
      await waitFor(() => expect(document.querySelectorAll('iframe').length).toBe(2))

      // Announced from the parent pane's own loopback origin, on a new hop port.
      await act(async () => {
        window.dispatchEvent(
          new MessageEvent('message', {
            origin: 'http://127.0.0.1:7778',
            data: {
              type: 'mc-instance-ready',
              v: 1,
              id: 'c-2',
              name: 'Chained',
              sshHost: 'cd-2-alias',
              remotePort: 5476,
              port: 40002,
            },
          }),
        )
      })

      await waitFor(() => expect(api.updateInstance).toHaveBeenCalledWith('cd-2', { via_remote_port: 40002 }))
      await waitFor(() => expect(store.getState().instances.warm['cd-2']?.port).toBe(50002))
      expect(store.getState().instances.warm['cd-2']?.token).toBe('new-tok')
    })
  })
})

// #16009: a reconnect lands the pane on a new port, so its own origin-scoped
// storage is empty. The hub keeps the pane's last chat and hands it back.
describe('InstancesViewport last-chat hint (#16009)', () => {
  beforeEach(() => { localStorage.clear() })

  const paneSrc = () => (document.querySelector('iframe') as HTMLIFrameElement | null)?.getAttribute('src') ?? ''
  const post = (data: unknown, origin: string) =>
    act(async () => { window.dispatchEvent(new MessageEvent('message', { data, origin })) })
  const renderWarm = () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: [{
        id: 'cd-1', name: 'Cloud One', ssh_host: 'cd-1-alias', remote_port: 7777, local_port: 7778,
        ttl: '20h', remote_bin: '',
        status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
      }],
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: { 'cd-1': true } },
    })
    renderWithProviders(<InstancesViewport />, { store })
    return store
  }

  it('reopens the chat the pane last showed when the tunnel comes back on a new port', async () => {
    const store = renderWarm()
    await waitFor(() => expect(paneSrc()).toContain(':7778/'))
    const before = paneSrc()
    await post({ source: 'kirocrew', type: 'mc-pane-active-slot', key: 'chat-2' }, 'http://127.0.0.1:7778')
    // Remembering must not reload the pane it came from.
    expect(paneSrc()).toBe(before)
    expect(before).not.toContain('lastSid')

    await act(async () => { store.dispatch(setWarm({ id: 'cd-1', conn: { port: 50002, token: 'tok2' } })) })
    await waitFor(() => expect(paneSrc()).toContain(':50002/'))
    expect(paneSrc()).toContain('lastSid=chat-2')
  })

  it('keeps one last chat per crew when two panes report different chats', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      instances: ['cd-1', 'cd-2'].map((id, i) => ({
        id, name: id, ssh_host: `${id}-alias`, remote_port: 7777, local_port: 7778 + i,
        ttl: '20h', remote_bin: '',
        status: { instance_id: id, state: 'connected', local_port: 7778 + i, remote_port: 7777 },
      })),
      warm_set_cap: 5,
    })
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 'tok' }, 'cd-2': { port: 7779, token: 'tok' } },
        activeId: 'cd-1', mru: ['cd-1', 'cd-2'], unread: {}, ready: { 'cd-1': true, 'cd-2': true },
      },
    })
    renderWithProviders(<InstancesViewport />, { store })
    const srcs = () => [...document.querySelectorAll('iframe')].map(f => f.getAttribute('src') ?? '')
    await waitFor(() => expect(srcs().length).toBe(2))
    await post({ type: 'mc-pane-active-slot', key: 'chat-a' }, 'http://127.0.0.1:7778')
    await post({ type: 'mc-pane-active-slot', key: 'chat-b' }, 'http://127.0.0.1:7779')

    await act(async () => {
      store.dispatch(setWarm({ id: 'cd-1', conn: { port: 50001, token: 't1' } }))
      store.dispatch(setWarm({ id: 'cd-2', conn: { port: 50002, token: 't2' } }))
    })
    await waitFor(() => {
      const s = srcs()
      expect(s.find(x => x.includes(':50001/'))).toContain('lastSid=chat-a')
      expect(s.find(x => x.includes(':50002/'))).toContain('lastSid=chat-b')
    })
  })

  it('ignores the relay from an origin that is not a warm pane, and a malformed key', async () => {
    const store = renderWarm()
    await waitFor(() => expect(paneSrc()).toContain(':7778/'))
    await post({ type: 'mc-pane-active-slot', key: 'chat-2' }, 'http://127.0.0.1:9999')
    await post({ type: 'mc-pane-active-slot', key: 'chat-2' }, 'https://evil.example')
    await post({ type: 'mc-pane-active-slot', key: 'a&token=x' }, 'http://127.0.0.1:7778')
    await post({ type: 'mc-pane-active-slot', key: 42 }, 'http://127.0.0.1:7778')

    await act(async () => { store.dispatch(setWarm({ id: 'cd-1', conn: { port: 50002, token: 'tok2' } })) })
    await waitFor(() => expect(paneSrc()).toContain(':50002/'))
    expect(paneSrc()).not.toContain('lastSid')
  })
})
