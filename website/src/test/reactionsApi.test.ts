/**
 * `api/reactions.ts`: three calls routed through the shared transport. The
 * request shape (URL encoding, method, JSON body) and the throw on a non-OK
 * response are the whole contract. Importing `../api/client` for its install
 * side-effect wires the transport these calls consume, the same way
 * `Frontend5Api.cov80.test.ts` exercises `pins.ts`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import '../api/client'
import { reactionsApi, REACTION_EMOJI } from '../api/reactions'
import { ApiError } from '../api/apiError'

interface Call {
  url: string
  init: RequestInit | undefined
}

let calls: Call[] = []

/** Queue one canned response per upcoming fetch; `json()` serves success and
 *  `text()` the error path, both from the same body. */
function stubFetch(responses: Array<{ ok: boolean; status: number; body: unknown }>) {
  const queue = [...responses]
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      const next = queue.shift()
      if (!next) throw new Error('zzz-unexpected-extra-fetch')
      return Promise.resolve({
        ok: next.ok,
        status: next.status,
        url,
        headers: { get: () => null },
        json: () => Promise.resolve(next.body),
        text: () => Promise.resolve(next.body == null ? '' : JSON.stringify(next.body)),
      } as unknown as Response)
    }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api/reactions', () => {
  it('exposes the fixed four-emoji set', () => {
    expect(REACTION_EMOJI).toEqual(['👍', '✅', '👀', '⭐'])
  })

  it('lists reactions for a slot, encoding the slot key', async () => {
    stubFetch([{ ok: true, status: 200, body: { reactions: [] } }])
    await expect(reactionsApi.list('a/b c')).resolves.toEqual({ reactions: [] })
    expect(calls[0].url).toBe('/api/chat/reactions?slot=a%2Fb%20c')
  })

  it('creates a reaction with a JSON POST body', async () => {
    const record = { id: 'r1', slot_key: 's', mid: 'm', emoji: '👍', reacted_at: 'zz' }
    stubFetch([{ ok: true, status: 201, body: record }])
    await expect(reactionsApi.create({ slot_key: 's', mid: 'm', emoji: '👍' })).resolves.toEqual(record)
    expect(calls[0].url).toBe('/api/chat/reactions')
    expect(calls[0].init?.method).toBe('POST')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ slot_key: 's', mid: 'm', emoji: '👍' })
  })

  it('removes a reaction by slot, mid and emoji, encoding each', async () => {
    stubFetch([{ ok: true, status: 200, body: { ok: true } }])
    await expect(reactionsApi.remove('a/b', 'm 1', '👍')).resolves.toEqual({ ok: true })
    expect(calls[0].url).toBe(`/api/chat/reactions?slot=a%2Fb&mid=m%201&emoji=${encodeURIComponent('👍')}`)
    expect(calls[0].init?.method).toBe('DELETE')
  })

  it('throws an ApiError carrying the status on a non-OK response', async () => {
    stubFetch([{ ok: false, status: 409, body: { code: 'reaction_limit_reached' } }])
    const err = await reactionsApi.create({ slot_key: 's', mid: 'm', emoji: '👍' }).then(
      () => null,
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).body).toContain('reaction_limit_reached')
  })
})
