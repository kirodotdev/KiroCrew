/**
 * InstancesViewport: a pane's load as the host sees it. The reactive token re-mint
 * budget that mc-auth-expired spends, the mc-switch-instance gate, the load watchdog's
 * timeout verdict and the Retry that force-reloads it, the iframe-mounted journal line
 * and which pane an mc-embedded-boot stage is journaled for. Readiness, the
 * tunnel-rebuild Retry, the script-error heal and the rest of the viewport are tested
 * in InstancesViewport.test.tsx.
 *
 * The mocks and the top-level beforeEach are copies of InstancesViewport.test.tsx's: a
 * vi.mock factory applies only to the file it is written in. A top-level hook puts the
 * real clock back after every test that faked it. The describe keeps the
 * title 'InstancesViewport', so every test keeps its full name.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { removeWarm, setActiveId, setWarm } from '../store/instancesSlice'
import {
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'
import { mockConnectedCd1 } from './InstancesViewportTestUtils'

// The postinstall patch (scripts/patch-happy-dom-iframe.mjs) makes happy-dom's
// disabled-iframe path dispatch 'load' instead of throwing DOMException when
// handleDisabledFileLoadingAsSuccess is true — no per-test workaround needed.

// The same Electron shell as InstancesViewport.test.tsx, so the overlays render as
// they do there. Only `isElectron` is overridden; the per-platform flags and
// caption widths keep their real values.
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

// Every test that fakes the clock gets the real one back, even if it fails or times out first.
afterEach(() => { vi.useRealTimers() })

describe('InstancesViewport', () => {
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
    expect(await screen.findByRole('group', { name: /Remote crews/i })).toBeInTheDocument()
  })

  it('Retry after a load timeout force-reloads the iframe even for an identical token', async () => {
    mockConnectedCd1()
    // connectInstance returns the SAME port+token as the preloaded warm entry —
    // the src is byte-identical, so only a keyed remount can reload a dead frame.
    vi.mocked(api.connectInstance).mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' })
    // flake-ok: the suite's top-level hook restores the real clock even when this test times out
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

  it('journals iframe-mounted once per real attach, not once per re-render (stable ref callback)', async () => {
    mockConnectedCd1()
    const info = vi.spyOn(console, 'info').mockImplementation(() => {})
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
      const paneLines = () => info.mock.calls.map(c => String(c[0])).filter(l => l.startsWith('[pane]'))
      const mounts = () => paneLines().filter(l => l.includes('iframe-mounted id=cd-1')).length
      const unmounts = () => paneLines().filter(l => l.includes('iframe-unmounted id=cd-1')).length
      expect(mounts()).toBe(1)
      expect(unmounts()).toBe(0)

      // Force several re-renders of the viewport WITHOUT touching the iframe:
      // a host-model input (unread count) and a same-port/token warm write are
      // both things the 10s poll and the broadcast effect do in production. An
      // inline `ref={el => ...}` would journal an unmount+mount pair for each.
      for (let i = 1; i <= 5; i++) {
        act(() => {
          window.dispatchEvent(new MessageEvent('message', {
            data: { type: 'mc-unread-slots', count: i },
            origin: 'http://127.0.0.1:7778',
          }))
        })
        act(() => { store.dispatch(setWarm({ id: 'cd-1', conn: { port: 7778, token: 'tok' } })) })
      }
      await waitFor(() => expect(store.getState().instances.unread['cd-1']).toBe(5))
      expect(mounts()).toBe(1)
      expect(unmounts()).toBe(0)

      // A real detach (the pane leaves the warm set) still journals exactly once.
      act(() => { store.dispatch(removeWarm('cd-1')) })
      await waitFor(() => expect(unmounts()).toBe(1))
      expect(mounts()).toBe(1)
    } finally {
      info.mockRestore()
    }
  })

  it('journals the pane bundle\'s mc-embedded-boot stages from its tunnel origin, and only from a mapped origin', async () => {
    mockConnectedCd1()
    const info = vi.spyOn(console, 'info').mockImplementation(() => {})
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()
      const bootLines = () => info.mock.calls.map(c => String(c[0])).filter(l => l.startsWith('[pane] boot '))

      for (const stage of ['entry', 'render', 'bridge']) {
        act(() => {
          window.dispatchEvent(new MessageEvent('message', {
            data: { type: 'mc-embedded-boot', v: 1, stage },
            origin: 'http://127.0.0.1:7778',
          }))
        })
      }
      expect(bootLines()).toEqual([
        '[pane] boot id=cd-1 stage=entry',
        '[pane] boot id=cd-1 stage=render',
        '[pane] boot id=cd-1 stage=bridge',
      ])
      // Boot is a journal line, never readiness: the overlay stays up.
      expect(store.getState().instances.ready['cd-1']).toBeUndefined()
      expect(screen.getByText(/Loading pane/i)).toBeInTheDocument()

      // A loopback origin the parent does not map is journaled as unattributed
      // (mirrors ready-unattributed) rather than dropped: "no boot line" must
      // keep meaning "no JavaScript of ours ran", not "the parent lost it".
      act(() => {
        window.dispatchEvent(new MessageEvent('message', {
          data: { type: 'mc-embedded-boot', v: 1, stage: 'entry' },
          origin: 'http://127.0.0.1:9999',
        }))
      })
      expect(bootLines().length).toBe(3)
      const unattributed = info.mock.calls.map(c => String(c[0])).filter(l => l.startsWith('[pane] boot-unattributed '))
      expect(unattributed).toEqual(['[pane] boot-unattributed origin=http://127.0.0.1:9999 stage=entry knownPorts=7778'])

      // A non-loopback origin stays silent.
      act(() => {
        window.dispatchEvent(new MessageEvent('message', {
          data: { type: 'mc-embedded-boot', v: 1, stage: 'entry' },
          origin: 'https://example.com',
        }))
      })
      expect(info.mock.calls.filter(c => String(c[0]).startsWith('[pane] boot')).length).toBe(4)
    } finally {
      info.mockRestore()
    }
  })

  it('attributes a boot from a pane warmed in the same tick, before the warm effect has flushed', async () => {
    mockConnectedCd1()
    const info = vi.spyOn(console, 'info').mockImplementation(() => {})
    try {
      const store = createTestStore({
        instances: { warm: { 'cd-1': { port: 7778, token: 'tok' } }, activeId: 'cd-1', mru: ['cd-1'], unread: {}, ready: {} },
      })
      renderWithProviders(<InstancesViewport />, { store })
      expect(await screen.findByText(/Loading pane/i)).toBeInTheDocument()

      // Warm a second pane and let its bundle announce boot inside the SAME
      // React flush. The render has already seen the new warm entry (warmRef
      // is assigned during render) but no passive effect keyed on `warm` has
      // run yet -- the exact window in which an effect-populated origin map
      // still lacks port 7790 and would drop this boot as unattributed.
      act(() => {
        store.dispatch(setWarm({ id: 'cd-2', conn: { port: 7790, token: 'tok2' } }))
        window.dispatchEvent(new MessageEvent('message', {
          data: { type: 'mc-embedded-boot', v: 1, stage: 'entry' },
          origin: 'http://127.0.0.1:7790',
        }))
      })
      const lines = info.mock.calls.map(c => String(c[0])).filter(l => l.startsWith('[pane] boot'))
      expect(lines).toContain('[pane] boot id=cd-2 stage=entry')
      expect(lines.some(l => l.startsWith('[pane] boot-unattributed'))).toBe(false)
    } finally {
      info.mockRestore()
    }
  })
})
