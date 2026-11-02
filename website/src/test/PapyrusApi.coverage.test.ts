import { describe, it, expect, vi, afterEach } from 'vitest'

import { papyrusApi, pdfUrl, PapyrusApiError } from '../apps/papyrus/api'

interface Seen {
  url: string
  method: string | undefined
  body: unknown
}

/** Answer every request with `respond`, recording what the wrapper sent. */
function route(respond: (seen: Seen) => Response | object = () => ({})) {
  const seen: Seen[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const s: Seen = {
        url,
        method: init?.method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      }
      seen.push(s)
      const out = respond(s)
      return out instanceof Response ? out : new Response(JSON.stringify(out), { status: 200 })
    }),
  )
  return seen
}

const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status })

afterEach(() => vi.unstubAllGlobals())

describe('papyrusApi request shapes', () => {
  it('sends GET reads with their query string and no method override', async () => {
    const seen = route((s) => (s.url.includes('/health') ? { status: 'ok' } : { files: ['a.tex'] }))
    expect(await papyrusApi.health()).toEqual({ status: 'ok' })
    expect(await papyrusApi.listFiles('paper')).toEqual({ files: ['a.tex'] })
    expect(seen.map((s) => s.url)).toEqual([
      '/api/apps/papyrus/health',
      '/api/apps/papyrus/files?name=paper',
    ])
    expect(seen.every((s) => s.method === undefined)).toBe(true)
  })

  it('encodes parameters into the query string', async () => {
    const seen = route()
    await papyrusApi.getProject('my paper')
    await papyrusApi.readFile('paper', 'sec/intro.tex')
    expect(seen.map((s) => s.url)).toEqual([
      '/api/apps/papyrus/project?name=my+paper',
      '/api/apps/papyrus/file?name=paper&path=sec%2Fintro.tex',
    ])
  })

  it('sends each write with its method and JSON body', async () => {
    const seen = route()
    await papyrusApi.createProject('p')
    await papyrusApi.cloneProject('https://h/r.git')
    await papyrusApi.cloneProject('https://h/r.git', 'named')
    await papyrusApi.saveFile('p', 'a.tex', 'x')
    await papyrusApi.createFile('p', 'b.tex')
    await papyrusApi.setMainFile('p', 'a.tex')
    await papyrusApi.setTitle('p', 'Shown', 'gen-1')
    await papyrusApi.gitCommit('p', 'msg')
    await papyrusApi.gitPush('p')
    await papyrusApi.gitPull('p')
    await papyrusApi.provisionCompiler()
    expect(seen.map((s) => [s.method, s.url, s.body])).toEqual([
      ['POST', '/api/apps/papyrus/projects', { name: 'p' }],
      ['POST', '/api/apps/papyrus/projects/clone', { url: 'https://h/r.git', name: '' }],
      ['POST', '/api/apps/papyrus/projects/clone', { url: 'https://h/r.git', name: 'named' }],
      ['PUT', '/api/apps/papyrus/file', { name: 'p', path: 'a.tex', content: 'x' }],
      ['POST', '/api/apps/papyrus/file', { name: 'p', path: 'b.tex', content: '' }],
      ['PUT', '/api/apps/papyrus/main', { name: 'p', path: 'a.tex' }],
      ['PUT', '/api/apps/papyrus/title', { name: 'p', title: 'Shown', generation: 'gen-1' }],
      ['POST', '/api/apps/papyrus/git/commit', { name: 'p', message: 'msg' }],
      ['POST', '/api/apps/papyrus/git/push', { name: 'p' }],
      ['POST', '/api/apps/papyrus/git/pull', { name: 'p' }],
      ['POST', '/api/apps/papyrus/compiler/provision', {}],
    ])
  })

  it('sends deletes as DELETE with the target in the query string', async () => {
    const seen = route()
    await papyrusApi.deleteProject('p')
    await papyrusApi.deleteFile('p', 'a.tex')
    expect(seen.map((s) => [s.method, s.url])).toEqual([
      ['DELETE', '/api/apps/papyrus/project?name=p'],
      ['DELETE', '/api/apps/papyrus/file?name=p&path=a.tex'],
    ])
  })
})

describe('papyrusApi errors', () => {
  it('carries the backend message, output, code and offending character', async () => {
    route(() => json({ error: 'bad name', output: 'git: refused', code: 'invalid_project_name', char: '/' }, 400))
    const err = await papyrusApi.createProject('a/b').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(PapyrusApiError)
    expect(err).toMatchObject({ message: 'bad name', status: 400, output: 'git: refused', code: 'invalid_project_name', char: '/' })
  })

  it('falls back to the status text, then the bare status, when the body is not JSON', async () => {
    route(() => new Response('<html>', { status: 404, statusText: 'Not Found' }))
    await expect(papyrusApi.health()).rejects.toMatchObject({ message: 'Not Found', status: 404 })
    route(() => new Response('<html>', { status: 500 }))
    await expect(papyrusApi.health()).rejects.toMatchObject({ message: 'HTTP 500', status: 500, output: '', code: '' })
  })

  it('ignores a non-string code or char from the backend', async () => {
    route(() => json({ error: 'x', code: 5, char: null }, 409))
    await expect(papyrusApi.createProject('p')).rejects.toMatchObject({ code: '', char: '' })
  })
})

describe('papyrusApi.compile', () => {
  it('returns a compile payload as-is, including a failed compile with its log', async () => {
    const seen = route(() => ({ ok: false, log: 'l', errors: [{ level: 'error', message: 'm', line: 3, file: 'a.tex' }], duration_ms: 9 }))
    const out = await papyrusApi.compile('p')
    expect(seen[0]).toMatchObject({ method: 'POST', url: '/api/apps/papyrus/compile', body: { name: 'p' } })
    expect(out.errors).toEqual([{ level: 'error', message: 'm', line: 3, file: 'a.tex' }])
  })

  it('turns a transport failure with an error body into a failed compile', async () => {
    route(() => json({ error: 'compiler crashed' }, 500))
    expect(await papyrusApi.compile('p')).toEqual({ ok: false, log: 'compiler crashed', errors: [], duration_ms: 0 })
  })

  it('uses the status text, then the bare status, when there is no payload', async () => {
    route(() => new Response('', { status: 502, statusText: 'Bad Gateway' }))
    expect((await papyrusApi.compile('p')).log).toBe('Bad Gateway')
    route(() => new Response('', { status: 502 }))
    expect((await papyrusApi.compile('p')).log).toBe('HTTP 502')
  })
})

describe('pdfUrl', () => {
  it('builds a cache-busted PDF URL for the named paper', () => {
    expect(pdfUrl('my paper', 3)).toBe('/api/apps/papyrus/pdf?name=my+paper&v=3')
  })
})
