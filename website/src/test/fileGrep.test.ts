/**
 * `api/fileGrep.ts` — the one wire call behind the Files rail's Content mode.
 *
 * The rail's own tests mock this module whole, so nothing there proves the
 * request is spelled the way `POST /api/file-grep` reads it. This does, through
 * the installed transport rather than a `fetch` spy: the root and the query
 * travel in the JSON body under the two keys the endpoint names -- never in the
 * URL, which proxies and access logs retain -- and the decoded body must come
 * back as-is.
 */
import { afterEach, describe, expect, it } from 'vitest'

import '../api/client'
import { apiTransport, installApiTransport } from '../api/apiTransport'
import { fileGrep, type FileGrepResponse } from '../api/fileGrep'

const real = { ...apiTransport }

afterEach(() => {
  installApiTransport(real)
})

describe('fileGrep', () => {
  it('POSTs the root and query in the body, never in the URL', async () => {
    const calls: Array<[string, unknown]> = []
    const body: FileGrepResponse = {
      results: [{ file: '/p/a.py', line: 2, preview: 'x = config(', label: '' }],
      truncated: false,
      engine: 'rg',
      skipped_docs: 0,
      root: '/p',
    }
    installApiTransport({
      ...real,
      post: (url: string, payload: unknown) => {
        calls.push([url, payload])
        return Promise.resolve({ status: 200 } as Response)
      },
      j: () => Promise.resolve(body),
    })

    const out = await fileGrep('/p/with space', 'sk-live-secret(&q=1')

    expect(calls).toHaveLength(1)
    const [url, payload] = calls[0]
    expect(url).toBe('/api/file-grep')
    expect(url).not.toContain('sk-live-secret')
    expect(payload).toEqual({ root: '/p/with space', q: 'sk-live-secret(&q=1' })
    expect(out).toBe(body)
  })

  it('lets a transport error surface rather than shaping an empty answer', async () => {
    installApiTransport({
      ...real,
      post: () => Promise.reject(new Error('503')),
    })
    await expect(fileGrep('/p', 'needle')).rejects.toThrow('503')
  })
})
