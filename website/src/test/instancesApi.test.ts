/**
 * Tests for the instances methods on the shared api client (src/api/client.ts).
 * Mocks global fetch to assert each method hits the right URL/method and that a
 * 403 surfaces as an ApiError (the "feature disabled" signal the page relies on).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { api, ApiError } from '../api/client'

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

  it('surfaces a 403 disabled response as ApiError', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 403,
      headers: { get: () => null },
      text: async () => 'instances feature is disabled',
    } as unknown as Response)
    await expect(api.listInstances()).rejects.toBeInstanceOf(ApiError)
  })
})
