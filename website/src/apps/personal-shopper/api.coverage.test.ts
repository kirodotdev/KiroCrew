/**
 * Personal Shopper API client: the fetch wrapper behind every request the app makes.
 *
 * fetch is the seam, so these tests run the real request builder and error mapping.
 * They assert the URL, method, headers and body sent, the parsed result, and the
 * error message thrown for each failure shape the backend can return.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { del, get, post, put } from './api'

const BASE = '/api/apps/personal-shopper'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('get', () => {
  it('requests the app path with same-origin credentials and returns the parsed body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [1, 2] }))

    await expect(get<{ items: number[] }>('/preferences')).resolves.toEqual({ items: [1, 2] })

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${BASE}/preferences`)
    expect(init).toEqual({ credentials: 'same-origin' })
  })

  it('returns undefined for a 204 response without reading a body', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await expect(get('/sites')).resolves.toBeUndefined()
  })
})

describe('error mapping', () => {
  it('throws the backend error code when the JSON body carries one', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ code: 'preferences_locked', error: 'Preferences are locked by an admin' }, 409),
    )

    await expect(get('/preferences')).rejects.toThrow('preferences_locked')
  })

  it('never surfaces the untranslated error prose when a code is present', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ code: 'quota_exceeded', error: 'raw english' }, 429))

    const failure = await get('/history').catch((err: unknown) => err)

    expect(failure).toBeInstanceOf(Error)
    expect((failure as Error).message).toBe('quota_exceeded')
    expect((failure as Error).message).not.toContain('raw english')
  })

  it('falls back to the HTTP status when the error body has no string code', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: 'only prose', code: 42 }, 500))

    await expect(get('/history')).rejects.toThrow('http_500')
  })

  it('falls back to the HTTP status when the error body is not JSON', async () => {
    fetchMock.mockResolvedValue(new Response('<html>gateway</html>', { status: 502 }))

    await expect(get('/history')).rejects.toThrow('http_502')
  })

  it('falls back to the HTTP status when the error body is JSON null', async () => {
    fetchMock.mockResolvedValue(jsonResponse(null, 404))

    await expect(get('/history/9')).rejects.toThrow('http_404')
  })
})

describe('post', () => {
  it('sends a JSON POST with the content type and serialised body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 'h1' }, 201))

    await expect(post<{ id: string }>('/history', { query: 'shoes' })).resolves.toEqual({ id: 'h1' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${BASE}/history`)
    expect(init).toEqual({
      credentials: 'same-origin',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: 'shoes' }),
    })
  })

  it('maps a backend error code on POST the same way as on GET', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ code: 'invalid_query' }, 400))

    await expect(post('/history', { query: '' })).rejects.toThrow('invalid_query')
  })
})

describe('put', () => {
  it('sends a JSON PUT with the content type and serialised body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ saved: true }))

    await expect(put<{ saved: boolean }>('/preferences', { size: 'M' })).resolves.toEqual({ saved: true })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${BASE}/preferences`)
    expect(init).toEqual({
      credentials: 'same-origin',
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ size: 'M' }),
    })
  })

  it('returns undefined for a 204 reply to a PUT', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await expect(put('/preferences', {})).resolves.toBeUndefined()
  })
})

describe('del', () => {
  it('sends a DELETE to the item path and returns the parsed body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ removed: 'site-7' }))

    await expect(del<{ removed: string }>('/sites/site-7')).resolves.toEqual({ removed: 'site-7' })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${BASE}/sites/site-7`)
    expect(init).toEqual({ credentials: 'same-origin', method: 'DELETE' })
  })

  it('throws the backend error code when a DELETE is refused', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ code: 'not_found' }, 404))

    await expect(del('/sites/missing')).rejects.toThrow('not_found')
  })
})
