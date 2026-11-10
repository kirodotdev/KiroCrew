/**
 * A read refused with a permission code is not asked again (#32031 and its
 * follow-up): the transport keeps a GET's `owner_only` or `relay_apps_teammate`
 * 403 by URL and hands a copy back without a request, until a token exchanged in
 * the auth banner may have changed the caller. Every other answer still reaches
 * the gateway on every ask.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, ApiError, checkSessionExpired, __resetAuthRecoveryStateForTests } from '../api/client'
import { isPermissionRefusal } from '../api/apiError'

const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>()

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } })
const refusal = (code: string) => async () => json(403, { error: 'refused', code })

const settle = (p: Promise<unknown>) => p.then((v) => v, (e: unknown) => e)

describe('the transport keeps a permission refusal', () => {
  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    __resetAuthRecoveryStateForTests()
    document.getElementById('mc-session-expired')?.remove()
  })

  it.each([
    ['relay_apps_teammate', () => api.listApps()],
    ['owner_only', () => api.sharedCrewPeople()],
    ['owner_only', () => api.members()],
  ])('asks once after a %s refusal and answers the rest with the same refusal', async (code, read) => {
    fetchMock.mockImplementation(refusal(code))
    for (let i = 0; i < 3; i++) {
      const err = await settle(read())
      expect(err).toBeInstanceOf(ApiError)
      expect(isPermissionRefusal(err)).toBe(true)
    }
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('asks again once a kept refusal is a minute old', async () => {
    // Only Date is faked: the fetch stub and the channel still settle on real time.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-08T12:00:00Z'))
    fetchMock.mockImplementation(refusal('owner_only'))
    await settle(api.sharedCrewPeople())
    vi.setSystemTime(new Date('2026-11-08T12:00:59Z'))
    await settle(api.sharedCrewPeople())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    vi.setSystemTime(new Date('2026-11-08T12:01:00Z'))
    await settle(api.sharedCrewPeople())
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('keeps a refusal for its own URL only', async () => {
    fetchMock.mockImplementation(refusal('owner_only'))
    await settle(api.sharedCrewSessions({ creator: 'me' }))
    await settle(api.sharedCrewSessions({ creator: 'all' }))
    await settle(api.sharedCrewSessions({ creator: 'me' }))
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['a 403 with another code', refusal('app_not_enabled')],
    ['instances_disabled', refusal('instances_disabled')],
    ['a session-expiry 403', async () => json(403, { error: 'expired', code: 'owner_only' }, { 'X-Auth-Required': 'true' })],
    ['a 401', async () => json(401, { error: 'unauthorized' })],
    ['a 500', async () => json(500, { error: 'boom', code: 'owner_only' })],
    ['a network failure', () => Promise.reject(new TypeError('Failed to fetch'))],
  ])('asks again after %s', async (_name, answer: () => Promise<Response>) => {
    // A 401 or session-expiry 403 starts the silent refresh; answer it with a 401.
    fetchMock.mockImplementation(async (url) => (url === '/api/auth/refresh' ? json(401, {}) : answer()))
    await settle(api.listApps())
    await settle(api.listApps())
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/apps')).toHaveLength(2)
  })

  it('asks the owner every time', async () => {
    fetchMock.mockImplementation(async () => json(200, { apps: [] }))
    await api.listApps()
    await api.listApps()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not keep a refused write', async () => {
    fetchMock.mockImplementation(refusal('owner_only'))
    await settle(api.chatMode('trust'))
    await settle(api.chatMode('trust'))
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('asks again once another tab of this origin loads', async () => {
    fetchMock.mockImplementation(refusal('owner_only'))
    await settle(api.members())
    await settle(api.members())
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // What a loading document posts: it may carry a new sign-in on the shared cookie.
    const otherTab = new BroadcastChannel('kirocrew-caller-changed')
    otherTab.postMessage(null)
    otherTab.close()
    // Still refused, so exactly one more request, after which it is kept again.
    await vi.waitFor(async () => {
      await settle(api.members())
      expect(fetchMock).toHaveBeenCalledTimes(2)
    })
  })

  it('does not keep a refusal that was in flight when the caller changed', async () => {
    fetchMock.mockImplementation(refusal('owner_only'))
    await settle(api.sharedCrewPeople())
    let release: (r: Response) => void = () => {}
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve }))
    const pending = settle(api.members())

    const otherTab = new BroadcastChannel('kirocrew-caller-changed')
    otherTab.postMessage(null)
    otherTab.close()
    // The kept People refusal is asked again once the message has landed.
    await vi.waitFor(async () => {
      await settle(api.sharedCrewPeople())
      expect(fetchMock.mock.calls.filter(([url]) => url === '/api/shared-crew/people')).toHaveLength(2)
    })

    release(json(403, { error: 'refused', code: 'owner_only' }))
    await pending
    await settle(api.members())
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/members')).toHaveLength(2)
  })

  it('asks again after a token is exchanged in the auth banner', async () => {
    fetchMock.mockImplementation(refusal('owner_only'))
    await settle(api.sharedCrewPeople())
    await settle(api.sharedCrewPeople())
    expect(fetchMock).toHaveBeenCalledTimes(1)

    fetchMock.mockImplementation(async (url) =>
      url === '/api/auth/refresh' ? json(401, {}) : json(200, { token_accepted: true, owner_ok: true }))
    checkSessionExpired(json(403, {}, { 'X-Auth-Required': 'true' }))
    await vi.waitFor(() => expect(document.getElementById('mc-session-expired')).not.toBeNull())
    const input = document.querySelector('#mc-session-expired input') as HTMLInputElement
    input.value = 'https://desk.example:6776/?token=owner'
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await vi.waitFor(() => expect(document.getElementById('mc-session-expired')).toBeNull())

    fetchMock.mockClear()
    fetchMock.mockImplementation(async () => json(200, { people: [] }))
    await expect(api.sharedCrewPeople()).resolves.toEqual({ people: [] })
    expect(fetchMock).toHaveBeenCalledWith('/api/shared-crew/people', expect.anything())
  })
})
