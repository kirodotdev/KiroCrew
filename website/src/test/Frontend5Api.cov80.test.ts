/**
 * The two API-layer files whose behaviour is entirely in their error paths.
 *
 * `pins.ts` is three calls routed through the shared transport that each turn a
 * non-OK response into a thrown `ApiError` — the request shape (URL encoding,
 * method, the session-key header) and that throw are the whole contract, and
 * nothing else exercises either. Importing `../api/client` for its install
 * side-effect wires the blessed transport `pins.ts` now consumes.
 *
 * `apiTransport.ts` is the frozen downstream seam: every method is a wrapper
 * that resolves the installed helper at CALL time, plus a guard for the
 * before-install case. It is loaded through `vi.resetModules()` + dynamic import
 * so the module starts with nothing installed, which is the only way to reach
 * that guard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import '../api/client'
import { pinsApi, PIN_PREVIEW_INPUT_MAX_CHARS, type PinMessageBody } from '../api/pins'
import { ApiError } from '../api/apiError'

/** One recorded fetch call. */
interface Call {
  url: string
  init: RequestInit | undefined
}

let calls: Call[] = []

/** Queue one canned response per upcoming fetch. The shared transport's `j`
 *  reads `text()` on the error path and `json()` on success, so the stub
 *  answers both from the same canned body. */
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

const body: PinMessageBody = {
  slot_key: 'zzslot',
  mid: 'zzmid',
  message_ts: '2031-01-02T03:04:05Z',
  role: 'user',
  preview: 'zzpreview',
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api/pins', () => {
  it('bounds the preview input well beyond the stored preview', () => {
    expect(PIN_PREVIEW_INPUT_MAX_CHARS).toBeGreaterThan(200)
  })

  it('lists pins for a slot, encoding the slot key and sending the session key', async () => {
    stubFetch([{ ok: true, status: 200, body: { pins: [] } }])
    await expect(pinsApi.list('a/b c')).resolves.toEqual({ pins: [] })
    expect(calls[0].url).toBe('/api/chat/pins?slot=a%2Fb%20c')
    expect((calls[0].init?.headers as Record<string, string>)['X-Session-Key']).toBe('dashboard:ui')
  })

  it('throws an ApiError carrying the status and the backend body when the list request fails', async () => {
    stubFetch([{ ok: false, status: 503, body: { code: 'pins_unavailable' } }])
    const err = await pinsApi.list('zzslot').then(
      () => { throw new Error('expected rejection') },
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(503)
    // The raw body is retained so a consumer can parse the backend `code`.
    expect((err as ApiError).body).toContain('pins_unavailable')
  })

  it('posts the pin body as JSON', async () => {
    stubFetch([{ ok: true, status: 200, body: { id: 'zzid' } }])
    const created = await pinsApi.create(body)
    expect(created).toEqual({ id: 'zzid' })
    expect(calls[0].url).toBe('/api/chat/pins')
    expect(calls[0].init?.method).toBe('POST')
    expect((calls[0].init?.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(body)
  })

  it('throws an ApiError carrying the status and backend code when the create request fails', async () => {
    stubFetch([{ ok: false, status: 409, body: { code: 'pin_limit_reached' } }])
    const err = await pinsApi.create(body).then(
      () => { throw new Error('expected rejection') },
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).body).toContain('pin_limit_reached')
  })

  it('deletes by encoded id', async () => {
    stubFetch([{ ok: true, status: 200, body: { ok: true } }])
    await expect(pinsApi.remove('a/b')).resolves.toEqual({ ok: true })
    expect(calls[0].url).toBe('/api/chat/pins/a%2Fb')
    expect(calls[0].init?.method).toBe('DELETE')
  })

  it('throws an ApiError carrying the status when the delete request fails', async () => {
    stubFetch([{ ok: false, status: 404, body: { code: 'pin_not_found' } }])
    const err = await pinsApi.remove('zzid').then(
      () => { throw new Error('expected rejection') },
      (e: unknown) => e,
    )
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
  })
})

describe('api/apiTransport', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('throws a named error when a method is called before client.ts installs', async () => {
    const { apiTransport } = await import('../api/apiTransport')
    // Destructuring is safe by design; only calling before install may throw.
    const { get } = apiTransport
    expect(() => get('/zz')).toThrow(/before api\/client installed it/)
  })

  it('forwards every method to the installed helper at call time', async () => {
    const mod = await import('../api/apiTransport')
    const seen: Array<[string, unknown[]]> = []
    const spy = (name: string) => (...args: unknown[]) => {
      seen.push([name, args])
      return Promise.resolve(name as unknown as Response)
    }
    mod.installApiTransport({
      get: spy('get') as never,
      post: spy('post') as never,
      put: spy('put') as never,
      del: spy('del') as never,
      patch: spy('patch') as never,
      j: spy('j') as never,
      jNullable: spy('jNullable') as never,
    })

    const r = { status: 200 } as Response
    await expect(mod.apiTransport.get('/zz-get')).resolves.toBe('get')
    await expect(mod.apiTransport.post('/zz-post', { a: 1 })).resolves.toBe('post')
    await expect(mod.apiTransport.put('/zz-put', { b: 2 })).resolves.toBe('put')
    await expect(mod.apiTransport.del('/zz-del', { c: 3 })).resolves.toBe('del')
    await expect(mod.apiTransport.patch('/zz-patch', { d: 4 })).resolves.toBe('patch')
    await expect(mod.apiTransport.j(r)).resolves.toBe('j')
    await expect(mod.apiTransport.jNullable(r)).resolves.toBe('jNullable')

    expect(seen.map(([n]) => n)).toEqual(['get', 'post', 'put', 'del', 'patch', 'j', 'jNullable'])
    // The arguments must arrive unchanged — a wrapper that dropped the body
    // would silently turn an edition's PUT into an empty write.
    expect(seen[2][1]).toEqual(['/zz-put', { b: 2 }])
    expect(seen[5][1]).toEqual([r])
  })

  it('re-resolves per call, so a later install replaces the earlier one', async () => {
    const mod = await import('../api/apiTransport')
    mod.installApiTransport({ get: () => Promise.resolve('first' as unknown as Response) } as never)
    await expect(mod.apiTransport.get('/zz')).resolves.toBe('first')
    mod.installApiTransport({ get: () => Promise.resolve('second' as unknown as Response) } as never)
    await expect(mod.apiTransport.get('/zz')).resolves.toBe('second')
  })
})
