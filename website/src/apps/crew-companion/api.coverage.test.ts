/**
 * Crew Companion same-origin fetch helpers.
 *
 * fetch is the seam. These tests run the real error mapping, so they assert what the
 * page receives: the status, the machine-readable code it switches on, and the message.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, apiGet, apiPost } from './api'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('ApiError', () => {
  it('carries the status, the code and the message, and names itself', () => {
    const err = new ApiError(409, 'app_disabled', 'Crew Companion is off')

    expect(err).toBeInstanceOf(Error)
    expect(err.name).toBe('ApiError')
    expect(err.status).toBe(409)
    expect(err.code).toBe('app_disabled')
    expect(err.message).toBe('Crew Companion is off')
  })
})

describe('apiGet', () => {
  it('requests the path with same-origin credentials and returns the parsed body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ pets: ['cat'] }))

    await expect(apiGet<{ pets: string[] }>('/api/crew/pets')).resolves.toEqual({ pets: ['cat'] })

    expect(fetchMock).toHaveBeenCalledWith('/api/crew/pets', { credentials: 'same-origin' })
  })

  it('maps a JSON error to an ApiError with its code and its error prose as the message', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ code: 'app_disabled', error: 'Disabled by admin' }, 409))

    const failure = await apiGet('/api/crew/pets').catch((err: unknown) => err)

    expect(failure).toBeInstanceOf(ApiError)
    expect(failure).toMatchObject({ status: 409, code: 'app_disabled', message: 'Disabled by admin' })
  })

  it('keeps a plain-text error body as the message, with no code', async () => {
    fetchMock.mockResolvedValue(new Response('upstream timeout', { status: 504 }))

    const failure = (await apiGet('/api/crew/pets').catch((err: unknown) => err)) as ApiError

    expect(failure.status).toBe(504)
    expect(failure.code).toBe('')
    expect(failure.message).toBe('upstream timeout')
  })

  it('uses the HTTP status line as the message when the error body is empty', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 503 }))

    const failure = (await apiGet('/api/crew/pets').catch((err: unknown) => err)) as ApiError

    expect(failure.status).toBe(503)
    expect(failure.code).toBe('')
    expect(failure.message).toBe('HTTP 503')
  })

  it('ignores non-string code and error fields and keeps the raw text as the message', async () => {
    const raw = JSON.stringify({ code: 7, error: { detail: 'nested' } })
    fetchMock.mockResolvedValue(new Response(raw, { status: 500 }))

    const failure = (await apiGet('/api/crew/pets').catch((err: unknown) => err)) as ApiError

    expect(failure.code).toBe('')
    expect(failure.message).toBe(raw)
  })

  it('treats an unreadable error body as empty and reports the HTTP status', async () => {
    const unreadable = {
      ok: false,
      status: 500,
      text: () => Promise.reject(new Error('stream closed')),
    } as unknown as Response
    fetchMock.mockResolvedValue(unreadable)

    const failure = (await apiGet('/api/crew/pets').catch((err: unknown) => err)) as ApiError

    expect(failure).toBeInstanceOf(ApiError)
    expect(failure.status).toBe(500)
    expect(failure.message).toBe('HTTP 500')
  })
})

describe('apiPost', () => {
  it('sends the JSON body with the content type and same-origin credentials', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ saved: true }))

    await expect(apiPost<{ saved: boolean }>('/api/crew/memory', { text: 'hi' })).resolves.toEqual({
      saved: true,
    })

    expect(fetchMock).toHaveBeenCalledWith('/api/crew/memory', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'hi' }),
    })
  })

  it('sends an empty object when no body is given', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))

    await apiPost('/api/crew/ping')

    const [, init] = fetchMock.mock.calls[0]
    expect((init as RequestInit).body).toBe('{}')
  })

  it('resolves to an empty object when the response has no body', async () => {
    fetchMock.mockResolvedValue(new Response('', { status: 200 }))

    await expect(apiPost('/api/crew/reset')).resolves.toEqual({})
  })

  it('throws an ApiError with the backend code on a non-2xx response', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ code: 'invalid_reminder', error: 'Bad time' }, 400))

    const failure = await apiPost('/api/crew/reminders', { at: 'soon' }).catch((err: unknown) => err)

    expect(failure).toBeInstanceOf(ApiError)
    expect(failure).toMatchObject({ status: 400, code: 'invalid_reminder', message: 'Bad time' })
  })
})
