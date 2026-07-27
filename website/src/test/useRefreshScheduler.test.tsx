/**
 * Tests for useRefreshScheduler.
 *
 * Covers TR-F-01 through TR-F-09 from
 * KiroCrew/docs/token-refresh/TESTCASES.md.
 *
 * Strategy: vitest fake timers + global fetch mock + a probe component
 * that mounts the hook so we exercise its useEffect lifecycle. Each test
 * wraps the probe in a fresh QueryClientProvider so React Query state
 * doesn't leak between cases.
 *
 * NOTE on testing-library `waitFor`: it polls via real setTimeout which
 * deadlocks under fake timers. We instead use
 * `await vi.advanceTimersByTimeAsync(0)` to flush microtasks (which is
 * what vitest's fake-timer integration provides for async assertions).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRefreshScheduler, type UseRefreshSchedulerOptions } from '../hooks/useRefreshScheduler'
import { __resetRefreshOnceForTests } from '../api/refreshOnce'

const ProbeComponent = ({ opts }: { opts?: UseRefreshSchedulerOptions }): null => {
  useRefreshScheduler(opts)
  return null
}

// Fresh QueryClient per render so cache state is isolated between tests.
// Defaults disable retry and window-focus refetch so test behaviour is
// deterministic under fake timers.
const makeWrapper = (): {
  Wrapper: React.FC<{ children: React.ReactNode }>
  qc: QueryClient
} => {
  const qc = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        gcTime: Infinity,
        staleTime: Infinity,
      },
      mutations: { retry: false },
    },
  })
  const Wrapper = ({ children }: { children: React.ReactNode }): JSX.Element => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { Wrapper, qc }
}

const renderProbe = (opts?: UseRefreshSchedulerOptions): ReturnType<typeof render> => {
  const { Wrapper } = makeWrapper()
  return render(<ProbeComponent opts={opts} />, { wrapper: Wrapper })
}

const okJson = (body: unknown): Response =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
const status = (code: number, body: unknown = {}): Response =>
  new Response(JSON.stringify(body), { status: code, headers: { 'content-type': 'application/json' } })

const TWENTY_H = 20 * 3600
const ONE_HOUR_MS = 60 * 60 * 1000

const refreshCalls = (mock: ReturnType<typeof vi.fn>): unknown[][] =>
  mock.mock.calls.filter((c) => c[0] === '/api/auth/refresh')

describe('useRefreshScheduler', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let originalFetch: typeof fetch

  beforeEach(() => {
    __resetRefreshOnceForTests()
    vi.useFakeTimers()
    fetchMock = vi.fn()
    originalFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    globalThis.fetch = originalFetch
  })

  it('TR-F-01: schedules POST /api/auth/refresh at session_exp - 1h', async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: 0 }))

    renderProbe()

    // Flush the initial /api/auth/me microtask chain
    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/me', expect.any(Object))

    // Set up the upcoming /refresh and the re-scheduled /me (after invalidate)
    fetchMock.mockResolvedValueOnce(status(200, { refreshed_at: nowSec + 1, session_exp: nowSec + TWENTY_H + 1, refresh_exp: nowSec + 30 * 86400 }))
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H + 1, refresh_exp: 0 }))

    // Advance to T-1h (i.e. ~19h from now)
    await vi.advanceTimersByTimeAsync(19 * ONE_HOUR_MS + 1000)
    expect(refreshCalls(fetchMock).length).toBeGreaterThanOrEqual(1)
  })

  it('TR-F-04: backoff timer at +1m on first 5xx, then retries', async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: 0 }))

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    fetchMock.mockResolvedValueOnce(status(500))
    await vi.advanceTimersByTimeAsync(19 * ONE_HOUR_MS + 1000)
    expect(refreshCalls(fetchMock).length).toBe(1)

    // 30s into 60s backoff: no second call yet
    await vi.advanceTimersByTimeAsync(30 * 1000)
    expect(refreshCalls(fetchMock).length).toBe(1)

    // Past the 60s backoff window: second /refresh fires
    fetchMock.mockResolvedValueOnce(status(500))
    await vi.advanceTimersByTimeAsync(35 * 1000)
    expect(refreshCalls(fetchMock).length).toBeGreaterThanOrEqual(2)
  })

  it('TR-F-06: 401 refresh_chain_revoked invokes onChainRevoked and stops scheduling', async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: 0 }))

    const onChainRevoked = vi.fn()
    renderProbe({ onChainRevoked })
    await vi.advanceTimersByTimeAsync(0)

    fetchMock.mockResolvedValueOnce(status(401, { error: 'refresh_chain_revoked' }))
    await vi.advanceTimersByTimeAsync(19 * ONE_HOUR_MS + 1000)

    expect(onChainRevoked).toHaveBeenCalledTimes(1)

    const callsAfter = fetchMock.mock.calls.length
    await vi.advanceTimersByTimeAsync(120 * ONE_HOUR_MS)
    expect(fetchMock.mock.calls.length).toBe(callsAfter)
  })

  it('TR-F-07: cleanup clears pending timer on unmount', async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: 0 }))

    const { unmount } = renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    unmount()

    const callsBefore = fetchMock.mock.calls.length
    await vi.advanceTimersByTimeAsync(25 * ONE_HOUR_MS)
    expect(fetchMock.mock.calls.length).toBe(callsBefore)
  })

  it('TR-F-08: hidden tab defers refresh until visibility returns', async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: 0 }))

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    await vi.advanceTimersByTimeAsync(19 * ONE_HOUR_MS + 1000)

    expect(refreshCalls(fetchMock).length).toBe(0)

    // Restore visibility — refresh should fire
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    fetchMock.mockResolvedValueOnce(status(200, { refreshed_at: nowSec + 1, session_exp: nowSec + TWENTY_H + 1, refresh_exp: 0 }))
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H + 1, refresh_exp: 0 }))
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)

    expect(refreshCalls(fetchMock).length).toBeGreaterThanOrEqual(1)
  })

  it('older server: 404 on /api/auth/me stops scheduler gracefully', async () => {
    fetchMock.mockResolvedValueOnce(status(404))

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    const callsAfter = fetchMock.mock.calls.length
    await vi.advanceTimersByTimeAsync(120 * ONE_HOUR_MS)
    expect(fetchMock.mock.calls.length).toBe(callsAfter)
  })

  it('TR-F-08: 401 on /api/auth/me triggers proactive on-mount refresh (no banner flash)', async () => {
    // Scenario: user opens dashboard after access cookie expired.
    // /api/auth/me returns 401. We MUST NOT wait 60s on backoff —
    // we MUST proactively call /api/auth/refresh, then re-fetch /api/auth/me,
    // all silently within a few hundred ms. This is the "no banner flash"
    // guarantee Tony asked for after the live test caught the race on
    // 2026-06-18 when overnight cookies expired and the app showed the
    // 403 token-required UI before the hook could refresh.
    const nowSec = Math.floor(Date.now() / 1000)
    // 1st call: GET /api/auth/me -> 401 (access cookie expired)
    fetchMock.mockResolvedValueOnce(status(401, { error: 'session_expired' }))
    // 2nd call: POST /api/auth/refresh -> 200 (refresh cookie still valid)
    fetchMock.mockResolvedValueOnce(status(200, { refreshed_at: nowSec, session_exp: nowSec + TWENTY_H, refresh_exp: nowSec + 30 * 86400 }))
    // 3rd call: retry GET /api/auth/me -> 200 (new cookies attached by browser)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: nowSec + 30 * 86400 }))

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    // Verify the sequence: /me 401 -> /refresh 200 -> /me 200
    const calls = fetchMock.mock.calls
    expect(calls.length).toBeGreaterThanOrEqual(3)
    expect(calls[0][0]).toBe('/api/auth/me')
    expect(calls[1][0]).toBe('/api/auth/refresh')
    expect((calls[1][1] as RequestInit).method).toBe('POST')
    expect(calls[2][0]).toBe('/api/auth/me')

    // No backoff timer should be scheduled — the next event is the
    // normal 19h-from-now refresh, not a 60s retry.
    await vi.advanceTimersByTimeAsync(2 * 60 * 1000)  // skip 2 min
    expect(refreshCalls(fetchMock).length).toBe(1)  // only the proactive one
  })

  it('TR-F-09: proactive on-mount refresh does NOT loop if refresh succeeds but /me still 401', async () => {
    // Edge case: refresh returned 200 but /api/auth/me STILL returns 401
    // (e.g. clock skew, rare race). We must not infinitely retry.
    fetchMock.mockResolvedValueOnce(status(401, { error: 'session_expired' }))
    fetchMock.mockResolvedValueOnce(status(200, { refreshed_at: 0, session_exp: 0, refresh_exp: 0 }))
    fetchMock.mockResolvedValueOnce(status(401, { error: 'session_expired' }))  // 2nd /me still 401

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    const calls = fetchMock.mock.calls
    // Should be exactly 3 calls: /me, /refresh, /me — no further /refresh attempt.
    // The retry inside queryFn does NOT recurse (allowRefreshFallback semantics
    // are now: queryFn calls refresh only once, then a plain retry).
    expect(calls.length).toBe(3)
    expect(calls[0][0]).toBe('/api/auth/me')
    expect(calls[1][0]).toBe('/api/auth/refresh')
    expect(calls[2][0]).toBe('/api/auth/me')

    // After the 2nd /me 401, queryFn returns null. The mutation onSuccess
    // already invalidated auth-me, but with retry=false the second /me
    // returning null doesn't trigger a third refresh — backoff path takes
    // over via the scheduler effect.
    await vi.advanceTimersByTimeAsync(30 * 1000)  // 30s
    expect(calls.length).toBe(3)
  })

  it('TR-F-10: cache invalidation after successful refresh re-reads /api/auth/me', async () => {
    // NEW test for the React Query refactor: validates that mutation.onSuccess
    // calls invalidateQueries(['auth-me']) which causes the scheduler to pick
    // up the freshly-rotated session_exp instead of the cached pre-refresh one.
    const nowSec = Math.floor(Date.now() / 1000)
    // 1st call: /api/auth/me -> 200 with session_exp at +20h
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: 0 }))

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    // 2nd call: /api/auth/refresh -> 200
    fetchMock.mockResolvedValueOnce(status(200, { refreshed_at: nowSec + 1, session_exp: nowSec + TWENTY_H + 100, refresh_exp: 0 }))
    // 3rd call: /api/auth/me re-fired by invalidateQueries -> new session_exp
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H + 100, refresh_exp: 0 }))

    // Fire the proactive refresh via timer
    await vi.advanceTimersByTimeAsync(19 * ONE_HOUR_MS + 1000)

    // Verify /api/auth/me was re-fetched after the refresh (cache invalidation worked)
    const meCalls = fetchMock.mock.calls.filter((c) => c[0] === '/api/auth/me')
    expect(meCalls.length).toBeGreaterThanOrEqual(2)
  })

  it('invalidates Kiro prerequisite state after a successful auth refresh', async () => {
    const nowSec = Math.floor(Date.now() / 1000)
    fetchMock.mockResolvedValueOnce(okJson({
      user_id: 'alice',
      session_exp: nowSec + TWENTY_H,
      refresh_exp: 0,
    }))
    const { Wrapper, qc } = makeWrapper()
    qc.setQueryData(['kiro-prerequisite'], { ready: false })
    render(<ProbeComponent />, { wrapper: Wrapper })
    await vi.advanceTimersByTimeAsync(0)

    fetchMock.mockResolvedValueOnce(status(200, {
      refreshed_at: nowSec + 1,
      session_exp: nowSec + TWENTY_H + 100,
      refresh_exp: 0,
    }))
    fetchMock.mockResolvedValueOnce(okJson({
      user_id: 'alice',
      session_exp: nowSec + TWENTY_H + 100,
      refresh_exp: 0,
    }))
    await vi.advanceTimersByTimeAsync(19 * ONE_HOUR_MS + 1000)

    expect(qc.getQueryState(['kiro-prerequisite'])?.isInvalidated).toBe(true)
  })

  it('TR-F-11: 403 + X-Auth-Required on /api/auth/me triggers proactive refresh (cold-reopen banner fix)', async () => {
    // The auth middleware denies an expired/absent access cookie
    // with 403 + X-Auth-Required (KiroCrew token_auth._deny), NOT 401. The
    // recovery MUST fire on this signal too — otherwise a cold reopen shows
    // the red session-expired banner and never uses the still-valid 30-day
    // refresh cookie (the daily-banner bug).
    const nowSec = Math.floor(Date.now() / 1000)
    // 1st call: GET /api/auth/me -> 403 + X-Auth-Required (cold reopen)
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'Token required' }), {
        status: 403,
        headers: { 'content-type': 'application/json', 'X-Auth-Required': 'true' },
      }),
    )
    // 2nd call: POST /api/auth/refresh -> 200 (refresh cookie still valid)
    fetchMock.mockResolvedValueOnce(status(200, { refreshed_at: nowSec, session_exp: nowSec + TWENTY_H, refresh_exp: nowSec + 30 * 86400 }))
    // 3rd call: retry GET /api/auth/me -> 200 (new cookies attached by browser)
    fetchMock.mockResolvedValueOnce(okJson({ user_id: 'alice', session_exp: nowSec + TWENTY_H, refresh_exp: nowSec + 30 * 86400 }))

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    // Verify the sequence: /me 403 -> /refresh 200 -> /me 200
    const calls = fetchMock.mock.calls
    expect(calls.length).toBeGreaterThanOrEqual(3)
    expect(calls[0][0]).toBe('/api/auth/me')
    expect(calls[1][0]).toBe('/api/auth/refresh')
    expect((calls[1][1] as RequestInit).method).toBe('POST')
    expect(calls[2][0]).toBe('/api/auth/me')

    // No backoff timer — only the one proactive refresh.
    await vi.advanceTimersByTimeAsync(2 * 60 * 1000)
    expect(refreshCalls(fetchMock).length).toBe(1)
  })

  it('TR-F-12: plain 403 WITHOUT X-Auth-Required does NOT trigger a refresh', async () => {
    // Precision guard: a genuine authorization 403 (e.g. a disabled feature
    // endpoint) must NOT be mistaken for an expired-session signal — only
    // 403 + X-Auth-Required is the auth-middleware deny. No refresh fires.
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'forbidden' }), {
        status: 403,
        headers: { 'content-type': 'application/json' },
      }),
    )

    renderProbe()
    await vi.advanceTimersByTimeAsync(0)

    expect(refreshCalls(fetchMock).length).toBe(0)
  })
})
