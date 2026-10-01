/**
 * Tests for the instances methods on the shared api client (src/api/client.ts).
 * Mocks global fetch to assert each method hits the right URL/method and that a
 * 403 surfaces as an ApiError (the "feature disabled" signal the page relies on).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import { recentErrors, __resetErrorJournalForTests } from '../utils/errorReport'

function okJson(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  __resetErrorJournalForTests()
})

describe('api instances methods', () => {
  it('listInstances GETs /api/instances and returns the payload', async () => {
    fetchMock.mockResolvedValue(okJson({ instances: [], warm_set_cap: 5 }))
    const res = await api.listInstances()
    expect(res.warm_set_cap).toBe(5)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/instances')
    expect(init?.headers?.['X-Session-Key']).toBe('dashboard:ui')
  })

  it('addInstance POSTs the body', async () => {
    fetchMock.mockResolvedValue(okJson({ id: 'cd-1' }))
    await api.addInstance({ name: 'CD', ssh_host: 'cd-1-alias' })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/instances')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(init?.body as string)).toMatchObject({ name: 'CD', ssh_host: 'cd-1-alias' })
  })

  it('update/remove/status/connect/disconnect hit the right endpoints', async () => {
    fetchMock.mockResolvedValue(okJson({}))
    await api.updateInstance('cd-1', { name: 'X' })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/instances/cd-1')
    expect(fetchMock.mock.calls[0][1].method).toBe('PATCH')

    await api.removeInstance('cd-1')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/instances/cd-1')
    expect(fetchMock.mock.calls[1][1].method).toBe('DELETE')

    await api.instanceStatus('cd-1')
    expect(fetchMock.mock.calls[2][0]).toBe('/api/instances/cd-1/status')

    await api.connectInstance('cd-1')
    expect(fetchMock.mock.calls[3][0]).toBe('/api/instances/cd-1/connect')
    expect(fetchMock.mock.calls[3][1].method).toBe('POST')

    await api.disconnectInstance('cd-1')
    expect(fetchMock.mock.calls[4][0]).toBe('/api/instances/cd-1/disconnect')
  })

  it('openInstancePane builds the connected-only query for the background issue mode', async () => {
    fetchMock.mockResolvedValue(okJson({ kind: 'same-origin-relay' }))
    // Default: no query.
    await api.openInstancePane('cd-1', 'same-origin-relay')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/instances/cd-1/pane')
    // onlyIfConnected -> ?only_if_connected=1 (auto-warm / renewal).
    await api.openInstancePane('cd-1', 'same-origin-relay', { onlyIfConnected: true })
    expect(fetchMock.mock.calls[1][0]).toBe('/api/instances/cd-1/pane?only_if_connected=1')
    // rebuild -> ?rebuild=1 (Retry); rebuild wins if both are somehow passed.
    await api.openInstancePane('cd-1', 'same-origin-relay', { rebuild: true })
    expect(fetchMock.mock.calls[2][0]).toBe('/api/instances/cd-1/pane?rebuild=1')
    await api.openInstancePane('cd-1', 'same-origin-relay', { rebuild: true, onlyIfConnected: true })
    expect(fetchMock.mock.calls[3][0]).toBe('/api/instances/cd-1/pane?rebuild=1')
    // The POST body always carries the access mode.
    expect(JSON.parse(fetchMock.mock.calls[1][1].body as string)).toEqual({ access: 'same-origin-relay' })
  })

  it('refreshInstanceToken POSTs /refresh-token and returns the new token', async () => {
    fetchMock.mockResolvedValue(okJson({ state: 'connected', local_port: 7778, token: 'fresh' }))
    const res = await api.refreshInstanceToken('cd-1')
    expect(res.token).toBe('fresh')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/instances/cd-1/refresh-token')
    expect(init?.method).toBe('POST')
  })

  it('encodes the id in the path', async () => {
    fetchMock.mockResolvedValue(okJson({}))
    await api.instanceStatus('a/b')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/instances/a%2Fb/status')
  })

  it('instanceChatSlots reads the hub route, NOT the peer through the proxy', async () => {
    // The URL is the contract here. Reading `/proxy/api/chat/slots` — which this
    // did first — returns the peer's list UNFILTERED, and the peer lists the slots
    // this hub drives for its own remote-EXECUTION bindings. The sidebar then
    // renders one conversation twice and cannot dedupe it, because the correlating
    // `remote_slot` is deliberately never projected to the browser. So the hub
    // route that applies that filter is the only correct one to call.
    fetchMock.mockResolvedValue(okJson([{ key: 'peer-1' }]))
    const rows = await api.instanceChatSlots('cd-1')
    expect(rows).toEqual([{ key: 'peer-1' }])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/instances/cd-1/chat-slots')
    expect(url).not.toContain('/proxy/')
    // GET by omission, as every read on this client is.
    expect(init?.method).toBeUndefined()
  })

  it('surfaces a 403 disabled response as ApiError', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      headers: { get: () => null },
      text: async () => 'instances feature is disabled',
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
  })

  it('does NOT journal the disabled-feature 403 (it is a designed, benign signal)', async () => {
    // The instances control plane is deny-by-default, so a 403 to its own list probe
    // is expected on most installs. The caller catches it and renders the enable
    // toggle; it must not surface as a spurious error report on whatever route
    // mounted the sidebar (e.g. /chat/new-session). Identified by the gateway's
    // `instances_disabled` code, NOT by the bare 403.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      url: '/api/instances',
      headers: { get: () => null },
      text: async () => JSON.stringify({
        error: 'instances feature is disabled (set instances.enabled=true)',
        code: 'instances_disabled',
      }),
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(0)
  })

  it('DOES journal the owner-only 403, which shares the status but is a real denial', async () => {
    // /api/instances answers 403 to an authenticated NON-OWNER caller too, and the
    // messaging transports mint valid dashboard tokens for non-owner subjects, so
    // this is a reachable principal rather than a theoretical one. Losing it would
    // strip the authorization failure from the error feed and the agent handoff,
    // which is why the opt-out is keyed on the code and not on the status.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      url: '/api/instances',
      headers: { get: () => null },
      text: async () => JSON.stringify({ error: 'owner authorization required', code: 'owner_only' }),
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({ source: 'api', status: 403, code: 'owner_only' })
  })

  it('DOES journal a 403 carrying no code at all, so an unlabelled denial is never swallowed', async () => {
    // The Slack-origin refusal on this route answers 403 with an `error` and no
    // `code`. A missing code must fail CLOSED (journal it), never match the benign
    // denial by default.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      url: '/api/instances',
      headers: { get: () => null },
      text: async () => JSON.stringify({ error: 'instances control plane is owner-only (not reachable via Slack)' }),
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({ source: 'api', status: 403 })
  })

  it('still journals an UNEXPECTED failure from listInstances (e.g. 500)', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      url: '/api/instances',
      headers: { get: () => null },
      text: async () => 'boom',
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({ source: 'api', status: 500 })
  })

  it('still journals an interposed gate 403, which arrives on the SAME expected status', async () => {
    // An edge proxy answering a lapsed session with its own HTML sign-in page is a
    // 403 too, so status alone cannot tell it from the disabled-feature denial. Two
    // independent things now stop it being swallowed: its body is markup and carries
    // no `code` of ours, so it cannot match the benign denial at all, and the guard
    // excludes edgeAuthExpired explicitly. This asserts the OUTCOME, so it holds
    // whichever of the two is reached first.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      url: '/api/instances',
      headers: { get: (h: string) => (h.toLowerCase() === 'content-type' ? 'text/html; charset=utf-8' : null) },
      text: async () => '<!doctype html><html><body>Sign in to continue</body></html>',
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0]).toMatchObject({ source: 'api', status: 403 })
  })
})
