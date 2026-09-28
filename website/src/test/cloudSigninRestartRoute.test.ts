/**
 * The wire contract of the sign-in restart call.
 *
 * `RemoteCrewPanelSigninRecovery.test.tsx` mocks the whole api module, so it can
 * prove the panel calls `cloudLaunchSigninRestart` but not what that function
 * requests — repointing it at `/cancel` left that suite green, which would have
 * shipped a "get a new code" button that destroys the launch instead.
 *
 * So this pins the route, the method, and that it is a DIFFERENT route from the
 * fetch-the-pending-prompt call it sits beside.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api } from '../api/client'

const ok = (body: unknown): Response =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

describe('cloudLaunchSigninRestart — the wire call', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let originalFetch: typeof fetch

  beforeEach(() => {
    // A FRESH Response per call: a body can only be read once, so one shared
    // instance makes the second call in a test throw instead of asserting.
    fetchMock = vi.fn(async () => ok({ id: 'j-1', status: 'running', steps: [] }))
    originalFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
  })
  afterEach(() => { globalThis.fetch = originalFetch })

  it('POSTs the launch job’s own signin/restart route', async () => {
    await api.cloudLaunchSigninRestart('j-unsigned')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/cloud/launch/j-unsigned/signin/restart')
    expect(init.method).toBe('POST')
  })

  it('escapes a job id rather than splicing it into the path', async () => {
    await api.cloudLaunchSigninRestart('a/b?c')
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('/api/cloud/launch/a%2Fb%3Fc/signin/restart')
  })

  it('is not the fetch-the-pending-prompt route, and not cancel', async () => {
    // Reusing either would answer "get a new code" by destroying the launch or
    // by re-reading the stale code the button exists to replace.
    await api.cloudLaunchSigninRestart('j-1')
    const restartUrl = (fetchMock.mock.calls[0] as [string])[0]
    fetchMock.mockClear()
    await api.cloudLaunchSignin('j-1')
    const fetchUrl = (fetchMock.mock.calls[0] as [string])[0]

    expect(restartUrl).not.toBe(fetchUrl)
    expect(restartUrl).not.toContain('/cancel')
    expect(restartUrl.startsWith(fetchUrl)).toBe(true)
  })
})
