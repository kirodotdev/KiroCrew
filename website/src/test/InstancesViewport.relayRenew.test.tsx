/**
 * Bounded relay-lease renewal (blocker 4): a transient issuer failure during the
 * renewal margin must NOT strand the pane on its old capability. The viewport
 * retries within the remaining lease deadline, cancels on endpoint/warm change,
 * and — when no attempt can still complete before the deadline — surfaces the
 * pane's timed-out state instead of a silent expiry. Renewal also uses the atomic
 * connected-only issue mode (blocker 3), so it can never reconnect after a
 * disconnect nor receive a remote token.
 *
 * Relay mode is forced by overriding resolvePaneMode; every other paneChannel
 * export stays real (relayRenewDelayMs, RELAY_LEASE_RENEW_RETRY_MS,
 * parsePaneEndpoint, paneEndpointSrc, …). The lease TTL is chosen so exactly one
 * retry fits before the deadline: first attempt at 60s (delay = ttl − ttl/3),
 * retry at 75s (+15s), and a would-be third at 90s == deadline is refused.
 */
import { act, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../lib/paneChannel', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/paneChannel')>()
  return { ...real, resolvePaneMode: () => ({ kind: 'same-origin-relay', origin: 'https://hub.example' }) }
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
import { api } from '../api/client'

const TTL = 90_000

function relayWarm(documentPath: string, channel: string, leaseExpiresAtEpochMs: number) {
  return {
    instances: {
      warm: {
        'cd-1': {
          kind: 'same-origin-relay' as const,
          documentPath,
          channel,
          protocol: 1,
          leaseExpiresAtEpochMs,
        },
      },
      activeId: null,
      mru: ['cd-1'],
      unread: {},
      ready: {},
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

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.setSystemTime(0)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('InstancesViewport relay-lease renewal', () => {
  it('retries a failed renewal within the deadline, then succeeds and rotates the endpoint', async () => {
    const lines = captureInfo()
    vi.mocked(api.openInstancePane)
      .mockRejectedValueOnce(new Error('gateway restarting'))
      .mockResolvedValue({
        kind: 'same-origin-relay',
        instance_id: 'cd-1',
        documentPath: '/instance-pane/CAP2/',
        channel: 'ch2',
        protocol: 1,
        leaseExpiresAtEpochMs: 75_000 + TTL,
      })
    const store = createTestStore(relayWarm('/instance-pane/CAP1/', 'ch1', TTL))
    renderWithProviders(<InstancesViewport />, { store })

    // First reissue fires at ttl - ttl/3 = 60s and FAILS.
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    expect(api.openInstancePane).toHaveBeenNthCalledWith(1, 'cd-1', 'same-origin-relay', { onlyIfConnected: true })
    expect(lines.some(l => l.includes('relay-renew-retry'))).toBe(true)

    // The bounded retry fires 15s later (75s < 90s deadline) and SUCCEEDS,
    // rotating the whole endpoint onto the fresh lease. Assert directly after the
    // act flush — waitFor polls on a timer, which deadlocks under fake timers.
    await act(async () => {
      vi.advanceTimersByTime(15_000)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(api.openInstancePane).toHaveBeenCalledTimes(2)
    expect(store.getState().instances.warm['cd-1']).toMatchObject({
      kind: 'same-origin-relay',
      documentPath: '/instance-pane/CAP2/',
      channel: 'ch2',
    })
    expect(lines.some(l => l.includes('[pane] relay-renew id=cd-1'))).toBe(true)
  })

  it('surfaces the timed-out pane when renewal can no longer succeed before the deadline', async () => {
    const lines = captureInfo()
    vi.mocked(api.openInstancePane).mockRejectedValue(new Error('still down'))
    const store = createTestStore(relayWarm('/instance-pane/CAP1/', 'ch1', TTL))
    renderWithProviders(<InstancesViewport />, { store })

    // 60s: first attempt fails, one retry fits (75s < 90s).
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    // 75s: retry fails; a further attempt at 90s == deadline does NOT fit, so the
    // pane is surfaced as timed out rather than left to silently 404 at expiry.
    await act(async () => {
      vi.advanceTimersByTime(15_000)
    })
    expect(api.openInstancePane).toHaveBeenCalledTimes(2)
    expect(lines.some(l => l.includes('relay-renew-timeout'))).toBe(true)

    // No further attempts are scheduled past the deadline.
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    expect(api.openInstancePane).toHaveBeenCalledTimes(2)
  })

  it('surfaces a Retry panel for an ACTIVE, READY pane whose renewal exhausts — without clearing readiness', async () => {
    // The bug the review confirmed: a pane that BOOTED and announced readiness,
    // then had its lease renewal exhaust, was marked timed out but kept `ready`,
    // and the old `timedOut && !ready` render could never surface it. The pane
    // stayed on screen while every future relayed request 404s at expiry. The fix
    // models terminal lease failure independently, so the recovery panel (and its
    // Retry) appears even for a ready pane, and readiness is NOT cleared (which
    // would repopulate the iframe `name` seed and re-arm the watchdog).
    vi.mocked(api.openInstancePane).mockRejectedValue(new Error('still down'))
    const store = createTestStore({
      instances: {
        warm: {
          'cd-1': {
            kind: 'same-origin-relay' as const,
            documentPath: '/instance-pane/CAP1/',
            channel: 'ch1',
            protocol: 1,
            leaseExpiresAtEpochMs: TTL,
          },
        },
        activeId: 'cd-1',
        mru: ['cd-1'],
        unread: {},
        ready: { 'cd-1': true },
      },
    })
    renderWithProviders(<InstancesViewport />, { store })

    // A ready pane shows NO recovery panel before renewal exhausts.
    expect(screen.queryByTestId('instances-viewport-timeout-error')).toBeNull()

    // 60s: first attempt fails; 75s: retry fails; a third at 90s == deadline does
    // not fit, so the lease failure becomes terminal.
    await act(async () => {
      vi.advanceTimersByTime(60_000)
    })
    await act(async () => {
      vi.advanceTimersByTime(15_000)
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(api.openInstancePane).toHaveBeenCalledTimes(2)
    // The recovery panel and its Retry control are now visible…
    expect(screen.getByTestId('instances-viewport-timeout-error')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
    // …and readiness was never cleared (the fix models the failure separately).
    expect(store.getState().instances.ready['cd-1']).toBe(true)
  })
})
