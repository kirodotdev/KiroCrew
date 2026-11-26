import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchFileRead, fileReadFailureMessage } from './fileReadQuery'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubFetch(body: string, status: number) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status, headers: { 'Content-Type': 'application/json' } })))
}

describe('fetchFileRead refusal code', () => {
  it('carries the code of a failed read', async () => {
    stubFetch(JSON.stringify({ error: 'x', code: 'unc_path_refused' }), 400)
    const r = await fetchFileRead('//wsl.localhost/Ubuntu/a.mjs')
    expect(r.ok).toBe(false)
    expect(r.code).toBe('unc_path_refused')
  })

  it('leaves the code unset for a body without one or a body that is not JSON', async () => {
    stubFetch(JSON.stringify({ error: 'invalid or forbidden path' }), 400)
    expect((await fetchFileRead('/a')).code).toBeUndefined()
    stubFetch('not json', 500)
    expect((await fetchFileRead('/a')).code).toBeUndefined()
  })

  it('never reads a code from a successful read', async () => {
    stubFetch(JSON.stringify({ code: 'unc_path_refused' }), 200)
    const r = await fetchFileRead('/a.json')
    expect(r.code).toBeUndefined()
    expect(r.text).toBe(JSON.stringify({ code: 'unc_path_refused' }))
  })
})

describe('fileReadFailureMessage', () => {
  it('says why a network path is refused and what to do', () => {
    const msg = fileReadFailureMessage('//wsl.localhost/Ubuntu/a.mjs', { status: 400, code: 'unc_path_refused' })
    expect(msg).toContain('//wsl.localhost/Ubuntu/a.mjs')
    expect(msg).toContain('does not open network paths')
    expect(msg).toContain('inside WSL')
    expect(msg).not.toContain('HTTP 400')
  })

  it('names the HTTP status for any other failure', () => {
    expect(fileReadFailureMessage('/a', { status: 400 })).toContain('HTTP 400')
    expect(fileReadFailureMessage('/a', { status: 500, code: 'read_failed' })).toContain('HTTP 500')
  })
})
