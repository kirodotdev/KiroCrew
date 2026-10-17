/**
 * A proxy's sign-in page on a background poll puts a notice on screen.
 *
 * The refusal was already worded on the failing request's `ApiError`, but a poll's
 * error is rendered by no card, so a tab left open past the proxy's session lifetime
 * failed every poll with nothing visible. These cases pin the notice: raised by the
 * proxy's page, never by the gateway's own JSON denial, and cleared by the first
 * request that gets through again.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, ApiError, __resetAuthRecoveryStateForTests, isAuthBannerShown } from '../api/client'

const CHALLENGE_PAGE = '<!DOCTYPE html><html><head><title>Access Required</title></head>'
  + '<body><h1>Access Required</h1></body></html>'

const respond = (body: string, status: number, headers: Record<string, string>) =>
  new Response(body, { status, headers })

const banner = (): HTMLElement | null => document.getElementById('mc-session-expired')

const settle = (call: Promise<unknown>): Promise<unknown> => call.then(() => null, (e) => e)

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
  vi.restoreAllMocks()
})

describe('the edge-challenge notice', () => {
  it('appears when a poll gets the proxy\'s HTML page instead of the gateway', async () => {
    fetchMock.mockResolvedValue(respond(CHALLENGE_PAGE, 403, { 'content-type': 'text/html; charset=UTF-8' }))
    const required = vi.fn()
    window.addEventListener('mc-auth-required', required)
    try {
      const err = await settle(api.status())
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).edgeChallenge).toBe(true)
    } finally {
      window.removeEventListener('mc-auth-required', required)
    }
    const el = banner()
    expect(el).not.toBeNull()
    expect(el?.dataset.variant).toBe('edge-challenge')
    expect(el?.textContent).toMatch(/access proxy/i)
    expect(el?.querySelector('input')).toBeNull()
    expect(isAuthBannerShown()).toBe(true)
    expect(required).toHaveBeenCalledTimes(1)
  })

  it('is raised once however many polls are refused', async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(respond(CHALLENGE_PAGE, 403, { 'content-type': 'text/html' })))
    await Promise.all([settle(api.status()), settle(api.status()), settle(api.status())])
    expect(document.querySelectorAll('#mc-session-expired')).toHaveLength(1)
  })

  it('reloads the document from its button, which lets a redirecting proxy sign in', async () => {
    fetchMock.mockResolvedValue(respond(CHALLENGE_PAGE, 401, { 'content-type': 'text/html' }))
    await settle(api.status())
    const reload = vi.fn()
    vi.spyOn(window, 'location', 'get').mockReturnValue({ ...window.location, reload } as Location)
    banner()?.querySelector('button')?.click()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('clears on the first request that gets a 2xx again', async () => {
    fetchMock.mockResolvedValueOnce(respond(CHALLENGE_PAGE, 403, { 'content-type': 'text/html' }))
    await settle(api.status())
    expect(banner()).not.toBeNull()
    fetchMock.mockResolvedValueOnce(respond('{}', 200, { 'content-type': 'application/json' }))
    await api.status()
    expect(banner()).toBeNull()
    expect(isAuthBannerShown()).toBe(false)
  })

  it('is not raised by the gateway\'s own JSON 401/403', async () => {
    fetchMock.mockResolvedValueOnce(respond('{"error":"forbidden","code":"not_owner"}', 403, { 'content-type': 'application/json' }))
    await settle(api.status())
    fetchMock.mockResolvedValueOnce(respond('{"error":"unauthenticated"}', 401, { 'content-type': 'application/json' }))
    await settle(api.status())
    expect(banner()).toBeNull()
  })

  it('is not raised inside an embedded pane, whose message already names where to sign in', async () => {
    vi.spyOn(window, 'top', 'get').mockReturnValue({} as Window)
    fetchMock.mockResolvedValue(respond(CHALLENGE_PAGE, 403, { 'content-type': 'text/html' }))
    const err = await settle(api.status())
    expect((err as ApiError).edgeChallenge).toBe(true)
    expect(banner()).toBeNull()
  })
})
