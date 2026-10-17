/**
 * Wire contracts the domain split of `api/client.ts` must carry over unchanged.
 *
 * `ApiClient.coverage.test.tsx` pins the transport and the non-trivial URL/body
 * builders. This file pins what moving an endpoint between modules could
 * quietly change and the generic sweep there cannot see: which methods talk
 * raw `fetch` and therefore send NO session key, the exact request of methods
 * whose bodies are caller-owned, and that the endpoints issue the same requests
 * as the blessed `apiTransport`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, ApiError, __resetAuthRecoveryStateForTests } from '../api/client'
import { apiTransport } from '../api/apiTransport'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetArtifactWrites } from '../lib/artifactWrites'

function res(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    url: 'http://localhost:6776/api/probe',
    headers: { get: (k: string) => headers[k] ?? headers[k.toLowerCase()] ?? null },
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => text,
    clone: () => res(status, body, headers),
  } as unknown as Response
}

const fetchMock = vi.fn()

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockResolvedValue(res(200, { ok: true }))
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  __resetErrorJournalForTests()
  __resetArtifactWrites()
})

afterEach(() => {
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

const lastCall = () => fetchMock.mock.calls[fetchMock.mock.calls.length - 1] as [string, RequestInit | undefined]

describe('dynamic dashboard facade contracts', () => {
  it('reads a card with an encoded slot and bare fetch, retaining the response', async () => {
    const card = { card: { html: '<p>Done</p>', data: {} }, status: 'published', published_at: 123, content_event_at: 122, stale: false }
    fetchMock.mockResolvedValue(res(200, card))
    await expect(api.dashboardCard('slot a/b')).resolves.toEqual(card)
    expect(fetchMock.mock.calls).toEqual([['/api/chat/slots/slot%20a%2Fb/dashboard-card']])
  })

  it('reads accepted work through the shared keyed GET, retaining the projection', async () => {
    const projection = { value: { items: [{ id: 'work-1', state: 'accepted' }] }, seq: 7 }
    fetchMock.mockResolvedValue(res(200, projection))
    await expect(api.sessionWorkProjection('slot a/b')).resolves.toEqual(projection)
    expect(fetchMock.mock.calls).toEqual([['/api/sessions/slot%20a%2Fb/crew-log/projection/work', { headers: { 'X-Session-Key': 'dashboard:ui' } }]])
  })

  it('retains approval purpose and coordinator scope without changing legacy resolution', async () => {
    const approvals = [{ id: 'request/id', tool_purpose: 'Check changes', slot: 'slack:thread/id' }]
    fetchMock.mockResolvedValue(res(200, approvals))
    await expect(api.approvals()).resolves.toEqual(approvals)
    expect(fetchMock.mock.calls[0]).toEqual(['/api/approvals'])
    await api.resolveApproval('request/id', 'reject_once', { origin: 'coordinator', slot: 'slack:thread/id', instance: 'inst-1' })
    expect(lastCall()).toEqual(['/api/approvals/request%2Fid/reject_once?origin=coordinator&slot=slack%3Athread%2Fid&instance=inst-1', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' }, body: '{}',
    }])
    await api.resolveApproval('legacy', 'reject_once')
    expect(lastCall()[0]).toBe('/api/approvals/legacy/reject_once')
  })
})

describe('raw-fetch reads stay raw', () => {
  // These reads were written on bare `fetch`, so they carry no `X-Session-Key`
  // and no init at all. Routing one through `get` would ADD the header, which
  // is a wire change the server's session gate can observe.
  it.each([
    ['mcpGatewayStatus', () => api.mcpGatewayStatus(), '/api/mcp-gateway/status'],
    ['mcpGatewayMetrics', () => api.mcpGatewayMetrics(), '/api/mcp-gateway/metrics'],
    ['monitorsList', () => api.monitorsList(), '/api/monitors'],
    ['monitorForSlot', () => api.monitorForSlot('slot a/b'), '/api/monitors/slot/slot%20a%2Fb'],
    ['sessionsMemory', () => api.sessionsMemory(), '/api/sessions/memory'],
    ['status', () => api.status(), '/api/status'],
  ])('%s issues a bare fetch with no init', async (_name, call, url) => {
    await call()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]).toEqual([url])
  })

  it('a raw read that must carry the session key adds exactly the shared placeholder', async () => {
    await api.kirocrewAgents()
    const [url, init] = lastCall()
    expect(url).toBe('/api/agents')
    expect(init).toEqual({ headers: { 'X-Session-Key': 'dashboard:ui' } })
  })
})

describe('caller-owned bodies', () => {
  it('unlinkMirror without expected posts no body yet keeps the JSON type and session key', async () => {
    await api.unlinkMirror('s/1')
    const [url, init] = lastCall()
    expect(url).toBe('/api/chat/slots/s%2F1/mirror-unlink')
    expect(init).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: undefined,
    })
  })

  it('unlinkMirror with expected posts exactly that object', async () => {
    await api.unlinkMirror('s1', { channel_type: 'slack', binding: 'C1:1.2' })
    expect(lastCall()[1]?.body).toBe(JSON.stringify({ channel_type: 'slack', binding: 'C1:1.2' }))
  })

  it('cloudLaunch forwards the caller body verbatim, key order included', async () => {
    await api.cloudLaunch({ profile: 'p', region: 'r', size_key: 'm', subnet_id: 'subnet-1', login_target: { kind: 'builder_id' } as never })
    const [url, init] = lastCall()
    expect(url).toBe('/api/cloud/launch')
    expect(init?.body).toBe('{"profile":"p","region":"r","size_key":"m","subnet_id":"subnet-1","login_target":{"kind":"builder_id"}}')
  })

  it('kiroPrerequisite passes the bundled_cli boolean through untouched', async () => {
    fetchMock.mockResolvedValue(res(200, { ready: true, bundled_cli: false }))
    await expect(api.kiroPrerequisite('explicit')).resolves.toEqual({ ready: true, bundled_cli: false })
    expect(lastCall()[0]).toBe('/api/kiro-prerequisite?refresh=explicit')
  })
})

describe('owner-only refusals stay errors', () => {
  it.each([
    ['restartGateway', () => api.restartGateway()],
    ['applyUpdate', () => api.applyUpdate()],
    ['setUpdateChannel', () => api.setUpdateChannel('nightly')],
    ['setAutoUpdate', () => api.setAutoUpdate(true)],
  ])('%s rejects a 403 owner_only with an ApiError', async (_name, call) => {
    fetchMock.mockResolvedValue(res(403, { error: 'owner only', code: 'owner_only' }))
    const err = await call().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(403)
    expect((err as ApiError).body).toContain('owner_only')
  })
})

describe('the nested namespaces keep their wire', () => {
  // The whole-surface sweep in ApiClient.coverage probes top-level functions
  // only, so the two namespaces get their requests pinned here.
  it.each([
    ['teams.list', () => api.teams.list(), ['/api/teams']],
    ['teams.create', () => api.teams.create({ name: 'n', members: ['a'] }), ['/api/teams', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: '{"name":"n","members":["a"]}',
    }]],
    ['teams.update', () => api.teams.update('t/1', { add: ['b'] }), ['/api/teams/t%2F1', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: '{"add":["b"]}',
    }]],
    ['teams.remove', () => api.teams.remove('t/1'), ['/api/teams/t%2F1', {
      method: 'DELETE',
      headers: { 'X-Session-Key': 'dashboard:ui' },
      body: undefined,
    }]],
    ['appearances.list', () => api.appearances.list(), ['/api/appearances']],
    ['appearances.detail', () => api.appearances.detail('p/1'), ['/api/appearances/p%2F1']],
    ['appearances.importBundle', () => api.appearances.importBundle({ id: 'p' }), ['/api/appearances/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: '{"bundle":{"id":"p"}}',
    }]],
    ['appearances.remove', () => api.appearances.remove('p/1'), ['/api/appearances/p%2F1', {
      method: 'DELETE',
      headers: { 'X-Session-Key': 'dashboard:ui' },
      body: undefined,
    }]],
  ])('%s', async (_name, call, expected) => {
    await call()
    expect(fetchMock.mock.calls).toEqual([expected])
  })
})

describe('a method that resolves a sibling through api', () => {
  it('wakatimeExportDownload fetches whatever api.wakatimeExportUrl answers at call time', async () => {
    // A refusal keeps the download itself out of the test: only the URL matters.
    fetchMock.mockResolvedValue(res(502, 'upstream down'))
    const spy = vi.spyOn(api, 'wakatimeExportUrl').mockReturnValue('/api/wakatime/export?patched=1')
    try {
      await expect(api.wakatimeExportDownload('2026-09-01', '2026-09-02', 'csv')).rejects.toBeInstanceOf(ApiError)
    } finally {
      spy.mockRestore()
    }
    expect(lastCall()[0]).toBe('/api/wakatime/export?patched=1')
  })
})

describe('methods that read their own response', () => {
  it('publishArtifactToCoreProvider answers the parsed body on 2xx and on 409', async () => {
    fetchMock.mockResolvedValueOnce(res(200, { url: 'https://x' }))
    await expect(api.publishArtifactToCoreProvider('a b', 'core')).resolves.toEqual({ url: 'https://x' })
    expect(lastCall()).toEqual(['/api/artifacts/a%20b/publish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Key': 'dashboard:ui' },
      body: '{"visibility":"PUBLIC","shared_with":[],"provider":"core"}',
    }])
    fetchMock.mockResolvedValueOnce(res(409, { findings: [1] }))
    await expect(api.publishArtifactToCoreProvider('a', 'core')).resolves.toEqual({ findings: [1] })
  })

  it('publishArtifactToCoreProvider unwraps a JSON error and keeps a plain one verbatim', async () => {
    fetchMock.mockResolvedValueOnce(res(502, '{"error":"No AWS account is registered"}'))
    await expect(api.publishArtifactToCoreProvider('a', 'core')).resolves.toEqual({ error: 'No AWS account is registered' })
    fetchMock.mockResolvedValueOnce(res(502, '{"detail":"x"}'))
    await expect(api.publishArtifactToCoreProvider('a', 'core')).resolves.toEqual({ error: '{"detail":"x"}' })
    fetchMock.mockResolvedValueOnce(res(502, 'gateway down'))
    await expect(api.publishArtifactToCoreProvider('a', 'core')).resolves.toEqual({ error: 'gateway down' })
  })

  it('exportSession and invokeFileMenuItem raise an ApiError on refusal', async () => {
    fetchMock.mockResolvedValue(res(403, '{"error":"incognito"}'))
    await expect(api.exportSession('s1')).rejects.toBeInstanceOf(ApiError)
    await expect(api.invokeFileMenuItem(
      { id: 'send', endpoint: '/api/apps/doc-store/send' },
      { surface: 'file-overflow', path: '/tmp/a.txt', kind: 'file' },
    )).rejects.toBeInstanceOf(ApiError)
    expect(lastCall()[1]).toMatchObject({ method: 'POST', redirect: 'error' })
  })
})

describe('the endpoints and apiTransport issue identical requests', () => {
  it.each([
    ['get', () => apiTransport.get('/api/security/stats'), () => api.securityStats()],
    ['post', () => apiTransport.post('/api/screenshot'), () => api.screenshot()],
    ['put', () => apiTransport.put('/api/security/trusted-apps/allow-all', { value: true }), () => api.setTrustAllApps(true)],
    ['del', () => apiTransport.del('/api/secrets/a'), () => api.secretsDelete('a')],
    ['patch', () => apiTransport.patch('/api/security/denied-commands/user/r1', { enabled: false }), () => api.toggleUserDeniedCommand('r1', false)],
  ])('%s: apiTransport and the api method issue the identical request', async (_name, viaTransport, viaApi) => {
    await viaTransport()
    const a = lastCall()
    fetchMock.mockClear()
    await viaApi()
    expect(lastCall()).toEqual(a)
  })

  it('j and jNullable parse the same way through apiTransport and through the endpoints', async () => {
    fetchMock.mockResolvedValue(res(204, ''))
    await expect(api.tipsNext()).resolves.toBeNull()
    await expect(apiTransport.jNullable(res(204, ''))).resolves.toBeNull()
    const refusal = res(500, 'boom')
    await expect(apiTransport.j(refusal)).rejects.toBeInstanceOf(ApiError)
    fetchMock.mockResolvedValue(res(500, 'boom'))
    await expect(api.status()).rejects.toBeInstanceOf(ApiError)
  })
})

describe('chatSlotAgent body', () => {
  const sentBody = () => JSON.parse(String(lastCall()[1]?.body ?? '{}'))

  it('the picker shapes carry no announce flag', async () => {
    await api.chatSlotAgent('s', 'fable')
    expect(sentBody()).toEqual({ agent: 'fable' })
    await api.chatSlotAgent('s', 'fable', 'template')
    expect(sentBody()).toEqual({ agent: 'fable', agent_kind: 'template' })
  })

  it('the typed /agent shape asks for the transcript line', async () => {
    await api.chatSlotAgent('s', 'fable', 'template', { announce: true })
    expect(lastCall()[0]).toBe('/api/chat/slots/s/agent')
    expect(sentBody()).toEqual({ agent: 'fable', agent_kind: 'template', announce: true })
  })
})
