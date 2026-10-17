import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fileExplorerApi } from '../apps/file-explorer/api'

/**
 * The File Explorer HTTP seam. Every path a user can type travels through these
 * query strings, so encoding is the whole job: a path with a space, a `#`, or a
 * `&` must survive as ONE parameter rather than truncating the request or
 * inventing a second one. The error path matters too — the backend's own message
 * is what tells the user "outside the allowed roots" instead of a bare 403.
 */
const BASE = '/apps/file-explorer/api'

let fetchMock: ReturnType<typeof vi.fn>

function ok(body: unknown = { zzz: true }): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

function fail(status: number, body: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({}),
    text: async () => body,
  } as unknown as Response
}

/** The URL the single call under test was made with. */
function calledUrl(): string {
  return String(fetchMock.mock.calls[0][0])
}

beforeEach(() => {
  fetchMock = vi.fn(async () => ok())
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fileExplorerApi requests', () => {
  it('sends same-origin credentials and parses the JSON body', async () => {
    fetchMock.mockResolvedValue(ok({ allowedRoots: ['/zzz'], home: '/zzz' }))
    await expect(fileExplorerApi.health()).resolves.toEqual({ allowedRoots: ['/zzz'], home: '/zzz' })
    expect(calledUrl()).toBe(`${BASE}/health`)
    expect(fetchMock.mock.calls[0][1]).toEqual({ credentials: 'same-origin' })
  })

  it('encodes the tree path and defaults the depth to 1', async () => {
    await fileExplorerApi.tree('/zzz dir/&sub')
    expect(calledUrl()).toBe(`${BASE}/tree?path=%2Fzzz%20dir%2F%26sub&depth=1`)
  })

  it('carries an explicit tree depth', async () => {
    await fileExplorerApi.tree('/zzz', 3)
    expect(calledUrl()).toBe(`${BASE}/tree?path=%2Fzzz&depth=3`)
  })

  it('omits max_bytes when not asked for, and sends it when it is', async () => {
    await fileExplorerApi.read('/zzz/file #1.txt')
    expect(calledUrl()).toContain('path=%2Fzzz%2Ffile+%231.txt')
    expect(calledUrl()).not.toContain('max_bytes')

    fetchMock.mockClear()
    await fileExplorerApi.read('/zzz/file.txt', 4096)
    expect(calledUrl()).toContain('max_bytes=4096')
  })

  it('treats a zero max_bytes as "no cap" rather than a zero-byte read', async () => {
    await fileExplorerApi.read('/zzz/file.txt', 0)
    expect(calledUrl()).not.toContain('max_bytes')
  })

  it('sends the search query, adding include/exclude only when non-empty', async () => {
    await fileExplorerApi.search('/zzz', 'needle & thread')
    expect(calledUrl()).toContain('q=needle+%26+thread')
    expect(calledUrl()).not.toContain('include=')
    expect(calledUrl()).not.toContain('exclude=')

    fetchMock.mockClear()
    await fileExplorerApi.search('/zzz', 'needle', '*.ts', 'node_modules')
    expect(calledUrl()).toContain('include=*.ts')
    expect(calledUrl()).toContain('exclude=node_modules')
  })

  it('encodes the path for git-status and resolve', async () => {
    await fileExplorerApi.gitStatus('/zzz repo')
    expect(calledUrl()).toBe(`${BASE}/git-status?path=%2Fzzz%20repo`)

    fetchMock.mockClear()
    await fileExplorerApi.resolve('/zzz?x')
    expect(calledUrl()).toBe(`${BASE}/resolve?path=%2Fzzz%3Fx`)
  })

  it('defaults completion to directories with a bounded limit', async () => {
    await fileExplorerApi.complete('/zzz/')
    expect(calledUrl()).toContain('kind=dir')
    expect(calledUrl()).toContain('limit=30')

    fetchMock.mockClear()
    await fileExplorerApi.complete('/zzz/', 'file', 5)
    expect(calledUrl()).toContain('kind=file')
    expect(calledUrl()).toContain('limit=5')
  })
})

describe('fileExplorerApi failures', () => {
  it("raises the backend's own message so the reason survives", async () => {
    fetchMock.mockResolvedValue(fail(403, 'zzz outside the allowed roots'))
    await expect(fileExplorerApi.tree('/zzz')).rejects.toThrow('zzz outside the allowed roots')
  })

  it('falls back to the status code when the body is empty', async () => {
    fetchMock.mockResolvedValue(fail(500, ''))
    await expect(fileExplorerApi.health()).rejects.toThrow('HTTP 500')
  })

  it('falls back to the status code when the body cannot even be read', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => { throw new Error('zzz stream broke') },
    } as unknown as Response)
    await expect(fileExplorerApi.resolve('/zzz')).rejects.toThrow('HTTP 502')
  })
})
