/**
 * knowledgeApi — the knowledge pages' thin fetch wrapper. Its whole job is
 * error shaping: a JSON body's `error` field wins over the status line, a
 * non-JSON body falls back to the status line, and a 2xx is parsed as JSON.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { knowledgeApi } from '../pages/knowledge/api'

afterEach(() => vi.unstubAllGlobals())

function stubFetch(impl: (url: string, opts?: RequestInit) => unknown) {
  const fetchMock = vi.fn((url: string, opts?: RequestInit) => Promise.resolve(impl(url, opts)))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('knowledgeApi', () => {
  it('prefixes /api/knowledge, forwards RequestInit, and returns the parsed body', async () => {
    const fetchMock = stubFetch(() => ({ ok: true, json: () => Promise.resolve({ zz: 7 }) }))
    const opts = { method: 'POST', body: '{}' }

    await expect(knowledgeApi<{ zz: number }>('/embedding/status', opts)).resolves.toEqual({ zz: 7 })
    expect(fetchMock).toHaveBeenCalledWith('/api/knowledge/embedding/status', opts)
  })

  it('throws the JSON body error field when the response is not ok', async () => {
    stubFetch(() => ({
      ok: false,
      status: 422,
      statusText: 'Unprocessable',
      json: () => Promise.resolve({ error: 'zzq namespace missing' }),
    }))

    await expect(knowledgeApi('/items')).rejects.toThrow('zzq namespace missing')
  })

  it('falls back to the status line when the error body has no error field', async () => {
    stubFetch(() => ({
      ok: false,
      status: 500,
      statusText: 'Server Error',
      json: () => Promise.resolve({ detail: 'zzq' }),
    }))

    await expect(knowledgeApi('/items')).rejects.toThrow('500 Server Error')
  })

  it('falls back to the status line when the error body is not JSON at all', async () => {
    stubFetch(() => ({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: () => Promise.reject(new Error('not json')),
    }))

    await expect(knowledgeApi('/items')).rejects.toThrow('502 Bad Gateway')
  })
})
