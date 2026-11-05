/**
 * `api.projectTree` reads one level per request and folds the folders it did
 * not read into the set the tree lists when they are opened.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { api } from '../api/client'

function reply(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api.projectTree', () => {
  it('asks for one level and folds lazy folders into the open-to-load set', async () => {
    const fetchMock = vi.fn(async () => reply({
      root: '/p', paths: ['a.txt'], directories: ['src', 'vendor'], repo: true,
      nestedRepoDirectories: ['vendor'], lazyDirectories: ['src'],
    }))
    vi.stubGlobal('fetch', fetchMock)
    const tree = await api.projectTree('/p', 'x')
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toBe('/api/project/tree?shallow=1&path=%2Fp&dir=x')
    expect(tree.nestedRepoDirectories).toEqual(['src', 'vendor'])
  })

  it('leaves a payload without lazy folders as it came', async () => {
    const body = { root: '/p', paths: [], repo: false, nestedRepoDirectories: [] }
    vi.stubGlobal('fetch', vi.fn(async () => reply(body)))
    expect(await api.projectTree('/p')).toEqual(body)
  })
})

describe('api.sessionFiles', () => {
  it('reads the slot listing', async () => {
    const fetchMock = vi.fn(async () => reply({ files: [], truncated: false }))
    vi.stubGlobal('fetch', fetchMock)
    expect(await api.sessionFiles('chat 1')).toEqual({ files: [], truncated: false })
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toBe('/api/project/session-files?slot=chat%201')
  })
})

describe('api.sessionFileDiff', () => {
  it('asks for one file of the slot', async () => {
    const body = { path: '/p/a b.ts', available: true, before: 'a', after: 'b' }
    const fetchMock = vi.fn(async () => reply(body))
    vi.stubGlobal('fetch', fetchMock)
    expect(await api.sessionFileDiff('chat 1', '/p/a b.ts')).toEqual(body)
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toBe('/api/project/session-file-diff?slot=chat%201&path=%2Fp%2Fa%20b.ts')
  })
})
