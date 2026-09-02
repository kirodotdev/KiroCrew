/**
 * discoverMcpTools contract: report a machine-readable CODE, never server prose,
 * and treat a 200 whose `status` is not "ok" as a failure rather than "no tools".
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../src/mochiApi'

function mockFetch(status: number, body: unknown) {
  const spy = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response)
  vi.stubGlobal('fetch', spy)
  return spy
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('discoverMcpTools', () => {
  it('calls Mochi\'s own route, not core\'s method-less path', async () => {
    const spy = mockFetch(200, { tools: [{ name: 'a' }], status: 'ok' })
    await api.discoverMcpTools?.('slack-mcp')
    // The original bug: core registers only PUT/DELETE on /api/mcp/servers/{name},
    // so a GET there 405s.
    expect(spy.mock.calls[0][0]).toBe('/api/apps/mochi/mcp-tools/slack-mcp')
  })

  it('probes with POST, so CSRF cannot be skipped', async () => {
    const spy = mockFetch(200, { tools: [], status: 'ok' })
    await api.discoverMcpTools?.('srv')
    // The route spawns a server process. The dashboard's CSRF middleware exempts
    // GET/HEAD/OPTIONS from the Origin check, so discovering over GET would let a
    // cross-site navigation start any configured MCP server. Locked here because
    // dropping `method` is a silent one-word regression that reopens that hole.
    expect((spy.mock.calls[0][1] as RequestInit).method).toBe('POST')
  })

  it('encodes the server name', async () => {
    const spy = mockFetch(200, { tools: [], status: 'ok' })
    await api.discoverMcpTools?.('a/b c')
    expect(spy.mock.calls[0][0]).toBe('/api/apps/mochi/mcp-tools/a%2Fb%20c')
  })

  it('returns the backend code, never its prose', async () => {
    mockFetch(409, { code: 'server_disabled', error: 'MCP server is disabled' })
    const r = await api.discoverMcpTools?.('srv')
    expect(r?.errorCode).toBe('server_disabled')
    expect(JSON.stringify(r)).not.toContain('MCP server is disabled')
  })

  it('synthesizes a code when the body carries none', async () => {
    mockFetch(405, {})
    const r = await api.discoverMcpTools?.('srv')
    // A FIXED token, not `http_405`: the panel maps codes to catalog keys, so an
    // arbitrary status number would be an unmappable member of a closed set. The
    // point this locks is that an unexpected HTTP failure still yields a code at
    // all -- the 405 that made this button inert used to return null.
    expect(r?.errorCode).toBe('http_error')
    expect(r?.tools).toEqual([])
  })

  it('treats a 200 with status != ok as a failure, not "no tools"', async () => {
    mockFetch(200, { tools: [], status: 'error' })
    const r = await api.discoverMcpTools?.('srv')
    expect(r?.errorCode).toBe('probe_failed')
  })

  it('reports success without an errorCode', async () => {
    mockFetch(200, { tools: [{ name: 'x' }], status: 'ok', cached: false })
    const r = await api.discoverMcpTools?.('srv')
    expect(r?.errorCode).toBeUndefined()
    expect(r?.tools).toEqual([{ name: 'x' }])
  })

  it('reports a network failure as a code, not an exception message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('boom at 10.0.0.1')))
    const r = await api.discoverMcpTools?.('srv')
    expect(r?.errorCode).toBe('network')
    expect(JSON.stringify(r)).not.toContain('10.0.0.1')
  })
})
