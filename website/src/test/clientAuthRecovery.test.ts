/**
 * Tests for the client-side single-flight silent refresh that recovers an
 * expired access cookie on the background-poll (warm) path before the
 * session-expired banner shows. Companion to useRefreshScheduler.test.tsx,
 * which covers the cold-boot /api/auth/me path.
 *
 * Scenario: when the access cookie lapses mid-session the background polls
 * (instances/approvals/usage/agents) all 403 at once. checkSessionExpired
 * funnels that burst through attemptSilentRefresh, which must collapse to a
 * single POST /api/auth/refresh and only let the banner show if the refresh
 * genuinely cannot recover.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { attemptSilentRefresh, checkSessionExpired, removeAuthBanner, __resetAuthRecoveryStateForTests } from '../api/client'
import { queryClient } from '../api/queryClient'

const deny403 = (): Response =>
  new Response(JSON.stringify({ error: 'Token required' }), {
    status: 403,
    headers: { 'content-type': 'application/json', 'X-Auth-Required': 'true' },
  })
const flushMicrotasks = (): Promise<void> => new Promise((r) => setTimeout(r, 0))
const bannerEl = (): HTMLElement | null => document.getElementById('mc-session-expired')

describe('client silent-refresh recovery (warm / background-poll path)', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let originalFetch: typeof fetch

  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
    fetchMock = vi.fn()
    originalFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    __resetAuthRecoveryStateForTests()
  })

  it('single-flights a concurrent 403 burst into ONE POST /api/auth/refresh', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ refreshed_at: 1 }), { status: 200 }))

    // 4 background polls 403 at once -> 4 concurrent recovery attempts.
    const results = await Promise.all([
      attemptSilentRefresh(),
      attemptSilentRefresh(),
      attemptSilentRefresh(),
      attemptSilentRefresh(),
    ])

    const refreshCalls = fetchMock.mock.calls.filter((c) => c[0] === '/api/auth/refresh')
    expect(refreshCalls.length).toBe(1)
    expect((refreshCalls[0][1] as RequestInit).method).toBe('POST')
    expect(results).toEqual([true, true, true, true])
  })

  it('returns true when the 30-day refresh cookie mints fresh cookies (200)', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }))
    expect(await attemptSilentRefresh()).toBe(true)
  })

  it('invalidates the [auth-me] cache on a successful warm-path refresh (keeps scheduler in sync)', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }))
    const spy = vi.spyOn(queryClient, 'invalidateQueries')
    expect(await attemptSilentRefresh()).toBe(true)
    expect(spy).toHaveBeenCalledWith({ queryKey: ['auth-me'] })
    spy.mockRestore()
  })

  it('returns false on 401 (chain revoked / no refresh cookie) so the banner can show', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'refresh_chain_revoked' }), { status: 401 }),
    )
    expect(await attemptSilentRefresh()).toBe(false)
  })

  it('does NOT exhaust on a transient 5xx — a later poll retries with a fresh refresh', async () => {
    fetchMock.mockResolvedValueOnce(new Response('', { status: 503 }))
    expect(await attemptSilentRefresh()).toBe(false)

    // Single-flight cleared + not exhausted: the next burst attempts again.
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 200 }))
    expect(await attemptSilentRefresh()).toBe(true)
    expect(fetchMock.mock.calls.filter((c) => c[0] === '/api/auth/refresh').length).toBe(2)
  })

  it('checkSessionExpired: no banner while the refresh is in flight, and none on success', async () => {
    // Hold the refresh pending so we can assert the banner is deferred, then
    // resolve 200 → recovered silently, banner never shown.
    let resolveFetch: (r: Response) => void = () => {}
    fetchMock.mockImplementation(() => new Promise<Response>((res) => { resolveFetch = res }))

    checkSessionExpired(deny403())
    expect(bannerEl()).toBeNull()  // deferred while the silent refresh is pending

    resolveFetch(new Response('{}', { status: 200 }))
    await flushMicrotasks()
    expect(bannerEl()).toBeNull()  // recovered → banner never appears
  })

  it('checkSessionExpired: shows the banner only after a terminal 401 refresh', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'refresh_chain_revoked' }), { status: 401 }),
    )

    checkSessionExpired(deny403())
    expect(bannerEl()).toBeNull()       // deferred during the refresh attempt
    await flushMicrotasks()
    expect(bannerEl()).not.toBeNull()   // refresh exhausted → banner appears
  })

  it('self-heals: a 2xx clears the exhausted latch so a later lapse retries silently', async () => {
    // Terminal 401 latches "exhausted" and shows the banner.
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: 'refresh_chain_revoked' }), { status: 401 }),
    )
    checkSessionExpired(deny403())
    await flushMicrotasks()
    expect(bannerEl()).not.toBeNull()

    // Auth restored (e.g. re-mint): a 2xx clears the banner AND the latch.
    removeAuthBanner()
    expect(bannerEl()).toBeNull()

    // A later lapse must attempt a fresh silent refresh, not banner immediately.
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }))
    const before = fetchMock.mock.calls.filter((c) => c[0] === '/api/auth/refresh').length
    checkSessionExpired(deny403())
    await flushMicrotasks()
    const after = fetchMock.mock.calls.filter((c) => c[0] === '/api/auth/refresh').length
    expect(after).toBeGreaterThan(before)  // retried instead of latched-off
    expect(bannerEl()).toBeNull()          // recovered again, no banner
  })
})
