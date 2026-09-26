/**
 * netFetch — the transport's network-failure seam.
 *
 * What these tests lock in, one per defect class the wrapper exists to close:
 *
 * - a transient network failure on an idempotent request self-heals instead of
 *   surfacing (the SSH-tunnel-blip case);
 * - a persistent failure is journaled ONCE with `source: 'network'` and the
 *   request path — the context the old bare `TypeError` threw away — while the
 *   thrown error keeps the browser's own message so existing
 *   `setError(e.message)` call sites render unchanged text;
 * - a mutation is never replayed (a rejected POST may still have reached the
 *   server);
 * - an abort is not a network failure: rethrown untouched, no retry, no journal
 *   entry;
 * - an HTTP error response is not this module's business — it resolves through
 *   to `j` exactly as before.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import { netFetch, RETRY_DELAYS_MS } from '../api/netFetch'
import { recentErrors, __resetErrorJournalForTests } from '../utils/errorReport'

const NETWORK_FAILURE = () => new TypeError('Failed to fetch')

beforeEach(() => {
  __resetErrorJournalForTests()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

/** Run `p` while draining every pending retry backoff. */
async function withTimersDrained<T>(p: Promise<T>): Promise<T> {
  // Fold the outcome into a value BEFORE advancing timers: a rejection that
  // fires mid-drain is already captured here, so it is never unhandled.
  const settled = p.then(
    (v) => ({ ok: true as const, v }),
    (e: unknown) => ({ ok: false as const, e }),
  )
  await vi.runAllTimersAsync()
  const r = await settled
  if (r.ok) return r.v
  throw r.e
}

describe('netFetch', () => {
  it('retries an idempotent request past a transient network failure', async () => {
    const ok = new Response('{}', { status: 200 })
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(NETWORK_FAILURE())
      .mockResolvedValueOnce(ok)
    vi.stubGlobal('fetch', fetchMock)

    const res = await withTimersDrained(netFetch('/api/state', undefined, { retry: true }))

    expect(res.status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    // The blip self-healed: nothing to report, so nothing is journaled.
    expect(recentErrors()).toEqual([])
  })

  it('gives up after the retry budget and journals one network report with the endpoint', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.reject(NETWORK_FAILURE()))
    vi.stubGlobal('fetch', fetchMock)

    await expect(withTimersDrained(netFetch('/api/state?token=hunter2secret', undefined, { retry: true })))
      .rejects.toThrow('Failed to fetch')

    expect(fetchMock).toHaveBeenCalledTimes(1 + RETRY_DELAYS_MS.length)
    const reports = recentErrors()
    expect(reports).toHaveLength(1) // once per outage, not once per attempt
    expect(reports[0].source).toBe('network')
    expect(reports[0].message).toBe('Failed to fetch')
    // Path only — the query string (the dashboard's own auth hand-off shape)
    // must never reach the journal.
    expect(reports[0].endpoint).toBe('/api/state')
    expect(reports[0].status).toBeUndefined()
  })

  it('never replays a mutation, but still journals its failure', async () => {
    const fetchMock = vi.fn().mockRejectedValue(NETWORK_FAILURE())
    vi.stubGlobal('fetch', fetchMock)

    await expect(withTimersDrained(netFetch('/api/chat/send', { method: 'POST' })))
      .rejects.toThrow('Failed to fetch')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const reports = recentErrors()
    expect(reports).toHaveLength(1)
    expect(reports[0].source).toBe('network')
    expect(reports[0].endpoint).toBe('/api/chat/send')
  })

  it('rethrows an abort untouched — no retry, no journal entry', async () => {
    const abort = new DOMException('The user aborted a request.', 'AbortError')
    const fetchMock = vi.fn().mockRejectedValue(abort)
    vi.stubGlobal('fetch', fetchMock)

    await expect(withTimersDrained(netFetch('/api/state', undefined, { retry: true })))
      .rejects.toBe(abort)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(recentErrors()).toEqual([])
  })

  it('stops retrying when the signal aborts during backoff, reporting the failure in hand', async () => {
    const controller = new AbortController()
    const fetchMock = vi.fn().mockImplementation(() => {
      // Abort AFTER the first failure, so the wrapper is already in backoff.
      controller.abort()
      return Promise.reject(NETWORK_FAILURE())
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      withTimersDrained(netFetch('/api/state', { signal: controller.signal }, { retry: true })),
    ).rejects.toThrow('Failed to fetch')

    expect(fetchMock).toHaveBeenCalledTimes(1) // the backoff check saw the abort
    expect(recentErrors()).toHaveLength(1)
  })

  it('passes an HTTP error response through untouched — j owns that path', async () => {
    const res500 = new Response('boom', { status: 500 })
    const fetchMock = vi.fn().mockResolvedValue(res500)
    vi.stubGlobal('fetch', fetchMock)

    const res = await withTimersDrained(netFetch('/api/state', undefined, { retry: true }))

    expect(res.status).toBe(500)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(recentErrors()).toEqual([])
  })

  it('does not treat a plain Error rejection as a network failure', async () => {
    // Test doubles and non-network throws reject with plain Error — the
    // TypeError discriminator keeps the wrapper inert for those.
    const err = new Error('offline')
    const fetchMock = vi.fn().mockRejectedValue(err)
    vi.stubGlobal('fetch', fetchMock)

    await expect(withTimersDrained(netFetch('/api/state', undefined, { retry: true })))
      .rejects.toBe(err)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(recentErrors()).toEqual([])
  })
})
