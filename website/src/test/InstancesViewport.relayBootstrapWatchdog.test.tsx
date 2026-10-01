/**
 * Document-bootstrap watchdog (follow-up to the relay-pane readiness work).
 *
 * The confirmed gap: a relay pane that BOOTED and announced readiness keeps
 * `ready[id]` true. When that pane navigates to a NEW document under the same
 * capability, the new document has an empty `window.name`, so it hands the
 * parent a fresh MessageChannel port and asks to be reseeded. The parent adopts
 * the port and replies (authentication + port setup succeed) — but the initial
 * load watchdog is gated on `!ready` and so never arms for an already-ready
 * pane. If the new document then goes silent before announcing readiness, the
 * pane hangs invisibly, exactly the exhaustion the initial watchdog exists to
 * catch.
 *
 * The fix tracks each document generation independently of initial readiness:
 * accepting a fresh port (re)arms a bounded timer bound to that document's
 * nonce + channel; an actual `mc-embedded-ready` clears it; on timeout the
 * existing Retry panel surfaces even though `ready[id]` is still true, WITHOUT
 * clearing readiness (which would repopulate the iframe `name` seed and re-arm
 * the initial watchdog).
 *
 * Relay mode is forced by overriding resolvePaneMode. Attribution
 * (`resolvePaneBootstrapRequest` / `resolvePaneMessage`) is stubbed to reach the
 * handler branches under test — jsdom does not expose a usable iframe
 * `contentWindow`, and the exact-frame + capability binding is covered by the
 * paneChannel suite; this test targets the parent-side timer lifecycle the fix
 * added, not attribution.
 *
 * test-audit: observable behaviour (the recovery panel + Retry appear, and
 * readiness is preserved), credible regression (a lost timer arm or a readiness
 * clear reopens the confirmed invisible hang), no test-only seam (the real
 * viewport listener, store, and render drive the assertions).
 */
import { act, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { RELAY_BOOTSTRAP_REQUEST, RELAY_ENVELOPE_VERSION } from '../lib/relayPaneBootstrap'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

const CHANNEL = 'ch1'
const INSTANCE = 'cd-1'
const DOC_PATH = '/instance-pane/CAP1/'

vi.mock('../lib/paneChannel', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/paneChannel')>()
  return {
    ...real,
    resolvePaneMode: () => ({ kind: 'same-origin-relay', origin: 'https://hub.example' }),
    // Grant the reseed for our capability, echoing the child's per-document
    // nonce (the arming binds the watchdog to it).
    resolvePaneBootstrapRequest: (req: { documentPath?: unknown; nonce?: unknown }) =>
      req?.documentPath === DOC_PATH
        ? { id: INSTANCE, channel: CHANNEL, protocol: 1, nonce: String(req.nonce) }
        : null,
    // Attribute an mc-embedded-ready to the warm instance (the bootstrap request
    // is handled before this is consulted).
    resolvePaneMessage: (arg: { data?: { type?: unknown } }) =>
      arg?.data?.type === 'mc-embedded-ready' ? INSTANCE : null,
  }
})

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    openInstancePane: vi.fn(),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'x' }),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected' }),
  },
}))

// Far enough out that no lease-renewal timer fires during the test.
const LEASE_MS = 10 * 60 * 1000
// Mirrors PANE_LOAD_TIMEOUT_MS in the component (not exported); the watchdog
// reuses it. A browser boot delay well under this recovers; terminal silence
// past it fails visibly.
const BOOTSTRAP_TIMEOUT_MS = 15_000

function readyRelayWarm() {
  return {
    instances: {
      warm: {
        [INSTANCE]: {
          kind: 'same-origin-relay' as const,
          documentPath: DOC_PATH,
          channel: CHANNEL,
          protocol: 1,
          leaseExpiresAtEpochMs: LEASE_MS,
        },
      },
      activeId: INSTANCE,
      mru: [INSTANCE],
      unread: {},
      ready: { [INSTANCE]: true },
    },
  }
}

function captureInfo() {
  const lines: string[] = []
  vi.spyOn(console, 'info').mockImplementation((...args: unknown[]) => {
    lines.push(args.join(' '))
  })
  return lines
}

/** Dispatch a subsequent-document bootstrap request that transfers a fresh port. */
function postBootstrapRequest(nonce: string) {
  const mc = new MessageChannel()
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: RELAY_BOOTSTRAP_REQUEST, documentPath: DOC_PATH, nonce, v: RELAY_ENVELOPE_VERSION },
        origin: 'null',
        ports: [mc.port2],
      }),
    )
  })
  return mc
}

function postEmbeddedReady() {
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', { data: { type: 'mc-embedded-ready', v: 1, mcPaneChannel: CHANNEL }, origin: 'null' }),
    )
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.setSystemTime(0)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('InstancesViewport — document-bootstrap watchdog', () => {
  it('surfaces Retry for an already-ready pane whose new document authenticates its port but never announces readiness, without clearing readiness', () => {
    const lines = captureInfo()
    const store = createTestStore(readyRelayWarm())
    renderWithProviders(<InstancesViewport />, { store })

    // A ready pane shows no recovery panel before anything navigates.
    expect(screen.queryByTestId('instances-viewport-timeout-error')).toBeNull()

    // The navigated-to document hands over a fresh port and is reseeded
    // (authentication + port setup succeed)…
    postBootstrapRequest('nonce-A')
    expect(lines.some(l => l.includes('[pane] relay-bootstrap-reply id=cd-1'))).toBe(true)
    // …but the panel must NOT appear yet: this is the window in which a genuine
    // boot delay (e.g. 3.5s) is still allowed to recover.
    act(() => { vi.advanceTimersByTime(3_500) })
    expect(screen.queryByTestId('instances-viewport-timeout-error')).toBeNull()

    // Terminal silence past the bootstrap timeout: the pane fails visibly.
    act(() => { vi.advanceTimersByTime(BOOTSTRAP_TIMEOUT_MS) })
    expect(lines.some(l => l.includes('[pane] relay-bootstrap-timeout id=cd-1'))).toBe(true)
    expect(screen.getByTestId('instances-viewport-timeout-error')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    // Readiness was NOT cleared — the failure is modelled independently, so the
    // iframe `name` seed is not repopulated and the initial watchdog not re-armed.
    expect(store.getState().instances.ready[INSTANCE]).toBe(true)
  })

  it('recovers (no panel) when the new document announces readiness within the window', () => {
    const store = createTestStore(readyRelayWarm())
    renderWithProviders(<InstancesViewport />, { store })

    postBootstrapRequest('nonce-B')
    // A browser boot delay under the timeout, then the document goes ready.
    act(() => { vi.advanceTimersByTime(3_500) })
    postEmbeddedReady()

    // Past the original deadline the panel never appears — readiness cleared the
    // watchdog.
    act(() => { vi.advanceTimersByTime(BOOTSTRAP_TIMEOUT_MS) })
    expect(screen.queryByTestId('instances-viewport-timeout-error')).toBeNull()
    expect(store.getState().instances.ready[INSTANCE]).toBe(true)
  })

  it('does not let a superseded document\'s timer fail a newer generation', () => {
    const lines = captureInfo()
    const store = createTestStore(readyRelayWarm())
    renderWithProviders(<InstancesViewport />, { store })

    // First navigation arms a timer, then a second navigation supersedes it
    // before the first would fire. The first timer must be cancelled, so only
    // ~one timeout can ever be journaled for the surviving generation.
    postBootstrapRequest('nonce-1')
    act(() => { vi.advanceTimersByTime(5_000) })
    postBootstrapRequest('nonce-2')

    // Advance past the FIRST timer's original deadline: it must not fire.
    act(() => { vi.advanceTimersByTime(11_000) })
    expect(lines.filter(l => l.includes('[pane] relay-bootstrap-timeout')).length).toBe(0)

    // The second generation still fails on its own deadline (one timeout total).
    act(() => { vi.advanceTimersByTime(BOOTSTRAP_TIMEOUT_MS) })
    expect(lines.filter(l => l.includes('[pane] relay-bootstrap-timeout')).length).toBe(1)
    expect(screen.getByTestId('instances-viewport-timeout-error')).toBeInTheDocument()
  })
})
