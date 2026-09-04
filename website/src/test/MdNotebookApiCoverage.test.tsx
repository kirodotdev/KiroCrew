/**
 * Coverage for the Notes app's API client (`src/apps/md-notebook/api.ts`).
 *
 * Every existing Notes test MOCKS this module, so none of its real request
 * building, error translation or Knowledge-registration recovery paths ever
 * executed. These tests drive the real module with a stubbed `fetch` and assert
 * the wire shape (method, URL, body) plus each error branch.
 */
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  knowledgeRegister,
  knowledgeUnregister,
  notesApi,
} from '../apps/md-notebook/api'
import { API_BASE } from '../apps/md-notebook/constants'
import { i18nT } from '../i18n/t'
import type { Vault } from '../apps/md-notebook/types'

type StubReply = {
  ok?: boolean
  status?: number
  statusText?: string
  json?: unknown
  /** Make `res.json()` reject, the way a non-JSON body does. */
  jsonThrows?: boolean
}

let fetchMock: ReturnType<typeof vi.fn>

/** Queue one reply per upcoming request, in order. */
function reply(...replies: StubReply[]): void {
  for (const r of replies) {
    const status = r.status ?? 200
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve({
        ok: r.ok ?? status < 400,
        status,
        statusText: r.statusText ?? '',
        json: () =>
          r.jsonThrows ? Promise.reject(new Error('not json')) : Promise.resolve(r.json ?? {}),
      }),
    )
  }
}

/** The URL and init of the nth (0-based) request made. */
function callAt(i: number): { url: string; init: RequestInit } {
  const args = fetchMock.mock.calls[i]
  return { url: String(args[0]), init: (args[1] ?? {}) as RequestInit }
}

const VAULT: Vault = {
  id: 'v1',
  name: 'My Vault',
  repo: 'git@example.com:me/notes.git',
  branch: 'main',
  localPath: '/home/me/vaults/notes',
  readOnly: false,
  subfolder: 'docs',
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('mdnbCall request shape', () => {
  it('sends a GET with no body and returns the parsed payload', async () => {
    reply({ json: { ok: true, features: ['search'] } })
    const out = await notesApi.health()
    expect(out).toEqual({ ok: true, features: ['search'] })
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/health`)
    expect(init.method).toBe('GET')
    expect(init.body).toBeUndefined()
    expect(init.credentials).toBe('same-origin')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('serialises the body for a POST', async () => {
    reply({ json: { vault: VAULT } })
    await notesApi.cloneVault({ url: 'https://example.com/n.git', pat: 'p', knowledge: true })
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/vaults`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      url: 'https://example.com/n.git',
      pat: 'p',
      knowledge: true,
    })
  })
})

describe('mdnbCall error translation', () => {
  it('raises ApiError carrying the server error, status and body', async () => {
    reply({ status: 422, json: { error: 'bad path', detail: 'x' } })
    const err = await notesApi.health().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    const api = err as ApiError
    expect(api.name).toBe('ApiError')
    expect(api.message).toBe('bad path')
    expect(api.status).toBe(422)
    expect(api.body).toEqual({ error: 'bad path', detail: 'x' })
    expect(api.staleBackend).toBe(false)
  })

  it('falls back to statusText when the payload carries no error string', async () => {
    reply({ status: 500, statusText: 'Internal Server Error', json: { error: 7 } })
    const err = (await notesApi.health().catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('Internal Server Error')
    expect(err.status).toBe(500)
  })

  it('treats an unparseable body as empty', async () => {
    reply({ status: 502, statusText: 'Bad Gateway', jsonThrows: true })
    const err = (await notesApi.health().catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('Bad Gateway')
    expect(err.body).toEqual({})
  })

  it('returns an empty object when a successful reply is not JSON', async () => {
    reply({ status: 200, jsonThrows: true })
    await expect(notesApi.health()).resolves.toEqual({})
  })

  it('rewrites a "no route" reply into the stale-backend message', async () => {
    reply({ status: 404, json: { error: 'no route: POST /trash/open' } })
    const err = (await notesApi.openTrash(null).catch((e: unknown) => e)) as ApiError
    expect(err.staleBackend).toBe(true)
    expect(err.message).toBe(i18nT('apps.mdNotebook.banner.staleBackendRoute'))
    expect(err.message).not.toContain('no route')
    // The raw payload is still available to callers.
    expect(err.body).toEqual({ error: 'no route: POST /trash/open' })
  })

  it('does not treat a mid-string "no route" as stale — the marker is anchored', async () => {
    reply({ status: 400, json: { error: 'sync failed: no route: /x' } })
    const err = (await notesApi.health().catch((e: unknown) => e)) as ApiError
    expect(err.staleBackend).toBe(false)
    expect(err.message).toBe('sync failed: no route: /x')
  })
})

describe('vault query building', () => {
  it('leaves the path untouched when no vault is active', async () => {
    reply({ json: { notes: [] } })
    await notesApi.listNotes(null)
    expect(callAt(0).url).toBe(`${API_BASE}/notes`)
  })

  it('appends with ? on a bare path and encodes the id', async () => {
    reply({ json: { notes: [] } })
    await notesApi.listNotes('a b&c')
    expect(callAt(0).url).toBe(`${API_BASE}/notes?vault=a+b%26c`)
  })

  it('appends with & when the path already carries a query', async () => {
    reply({ json: { results: [] } })
    await notesApi.search('v1', 'hello world')
    expect(callAt(0).url).toBe(`${API_BASE}/search?q=hello%20world&vault=v1`)
  })
})

describe('vault endpoints', () => {
  it('lists vaults', async () => {
    reply({ json: { vaults: [VAULT], hasPat: true, hasGhAuth: false } })
    const out = await notesApi.listVaults()
    expect(out.vaults).toHaveLength(1)
    expect(out.hasPat).toBe(true)
    expect(callAt(0).url).toBe(`${API_BASE}/vaults`)
  })

  it('attaches a vault in place', async () => {
    reply({ json: { vault: VAULT } })
    await notesApi.attachVault({ path: '/tmp/n', subfolder: 'docs' })
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/vaults/attach`)
    expect(JSON.parse(String(init.body))).toEqual({ path: '/tmp/n', subfolder: 'docs' })
  })

  it('forgets a vault by encoded id', async () => {
    reply({ json: { ok: true, localPath: '/tmp/n' } })
    await notesApi.forgetVault('a/b c')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/vaults?vault=a%2Fb%20c`)
    expect(init.method).toBe('DELETE')
  })

  it('toggles the knowledge flag', async () => {
    reply({ json: { vault: VAULT } })
    await notesApi.setVaultKnowledge('v1', true, 'src-9')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/vaults/knowledge`)
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({
      vault: 'v1',
      knowledge: true,
      sourceId: 'src-9',
    })
  })

  it('stores a PAT', async () => {
    reply({ json: { hasPat: true, hasGhAuth: true } })
    const out = await notesApi.setPat('ghp_x')
    expect(out).toEqual({ hasPat: true, hasGhAuth: true })
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/pat`)
    expect(init.method).toBe('PUT')
  })

  it('opens the folder picker', async () => {
    reply({ json: { path: null, cancelled: true } })
    const out = await notesApi.pickFolder()
    expect(out.cancelled).toBe(true)
    expect(callAt(0).url).toBe(`${API_BASE}/pick-folder`)
  })
})

describe('note endpoints', () => {
  it('reads a note with both the path and the vault in the query', async () => {
    reply({ json: { path: 'a/b.md', content: '#', mtime: 5 } })
    await notesApi.readNote('v1', 'a/b c.md')
    expect(callAt(0).url).toBe(`${API_BASE}/note?path=a%2Fb%20c.md&vault=v1`)
  })

  it('saves a note with the mtime guard', async () => {
    reply({ json: { ok: true, mtime: 9 } })
    await notesApi.saveNote('v1', 'a.md', 'body', 8)
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/note?vault=v1`)
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({
      path: 'a.md',
      content: 'body',
      baseMtime: 8,
    })
  })

  it('omits baseMtime when the caller has no snapshot', async () => {
    reply({ json: { ok: true, mtime: 1 } })
    await notesApi.saveNote(null, 'a.md', 'body')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/note`)
    expect(JSON.parse(String(init.body))).toEqual({ path: 'a.md', content: 'body' })
  })

  it('deletes a note', async () => {
    reply({ json: { ok: true } })
    await notesApi.deleteNote('v1', 'a.md')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/note?path=a.md&vault=v1`)
    expect(init.method).toBe('DELETE')
  })

  it('creates a note in a folder', async () => {
    reply({ json: { path: 'f/Untitled.md' } })
    const out = await notesApi.newNote('v1', 'f')
    expect(out.path).toBe('f/Untitled.md')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/note/new?vault=v1`)
    expect(JSON.parse(String(init.body))).toEqual({ folder: 'f' })
  })

  it('duplicates a note', async () => {
    reply({ json: { path: 'a copy.md' } })
    await notesApi.duplicateNote('v1', 'a.md')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/note/duplicate?vault=v1`)
    expect(JSON.parse(String(init.body))).toEqual({ path: 'a.md' })
  })

  it('moves a note', async () => {
    reply({ json: { ok: true, path: 'b.md' } })
    await notesApi.moveNote('v1', 'a.md', 'b.md')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/note/move?vault=v1`)
    expect(JSON.parse(String(init.body))).toEqual({ from: 'a.md', to: 'b.md' })
  })

  it('polls for external changes since a revision', async () => {
    reply({ json: { rev: 4, changed: ['a.md'], watching: true } })
    const out = await notesApi.changes('v1', 3)
    expect(out.changed).toEqual(['a.md'])
    expect(callAt(0).url).toBe(`${API_BASE}/changes?since=3&vault=v1`)
  })
})

describe('git endpoints', () => {
  it('syncs', async () => {
    reply({ json: { result: { pushed: 1 } } })
    await notesApi.sync('v1')
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/sync?vault=v1`)
    expect(init.method).toBe('POST')
    expect(init.body).toBeUndefined()
  })

  it('commits locally', async () => {
    reply({ json: { result: { committed: 2 } } })
    await notesApi.commit('v1')
    expect(callAt(0).url).toBe(`${API_BASE}/commit?vault=v1`)
  })

  it('opens the trash folder without sending a path', async () => {
    reply({ json: { opened: false, empty: true, path: '' } })
    const out = await notesApi.openTrash('v1')
    expect(out.empty).toBe(true)
    const { url, init } = callAt(0)
    expect(url).toBe(`${API_BASE}/trash/open?vault=v1`)
    expect(init.body).toBeUndefined()
  })
})

describe('knowledgeRegister', () => {
  it('registers the vault content path and confirms the source', async () => {
    reply({ status: 201, json: { id: 'src-1', file_count: 12 } }, { status: 200 })
    const out = await knowledgeRegister(VAULT)
    expect(out).toEqual({ sourceId: 'src-1', fileCount: 12 })

    const add = callAt(0)
    expect(add.url).toBe('/api/knowledge/sources')
    expect(add.init.method).toBe('POST')
    expect(JSON.parse(String(add.init.body))).toEqual({
      name: 'My Vault',
      source_type: 'obsidian_vault',
      uri: '/home/me/vaults/notes/docs',
      properties: {},
    })

    const confirm = callAt(1)
    expect(confirm.url).toBe('/api/knowledge/sources/src-1/confirm')
    expect(confirm.init.method).toBe('POST')
  })

  it('omits the subfolder from the uri when the vault has none', async () => {
    reply({ status: 200, json: { id: 'src-2' } }, { status: 200 })
    const out = await knowledgeRegister({ ...VAULT, subfolder: undefined })
    expect(out).toEqual({ sourceId: 'src-2', fileCount: undefined })
    expect(JSON.parse(String(callAt(0).init.body)).uri).toBe('/home/me/vaults/notes')
  })

  it('adopts the existing source id on 409 instead of failing', async () => {
    reply({ status: 409, json: { id: 'src-old', error: 'already registered' } }, { status: 200 })
    const out = await knowledgeRegister(VAULT)
    expect(out.sourceId).toBe('src-old')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('surfaces the server error when registration fails', async () => {
    reply({ status: 500, json: { error: 'disk full' } })
    await expect(knowledgeRegister(VAULT)).rejects.toThrow('disk full')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('reports the status when a failed registration carries no error text', async () => {
    reply({ status: 503, jsonThrows: true })
    await expect(knowledgeRegister(VAULT)).rejects.toThrow('knowledge add failed (503)')
  })

  it('rejects a 2xx reply that carries no source id', async () => {
    reply({ status: 200, json: {} })
    await expect(knowledgeRegister(VAULT)).rejects.toThrow(
      'knowledge add returned no source id',
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('prefers the payload error over the generic no-id message on 409', async () => {
    reply({ status: 409, json: { error: 'conflict without id' } })
    await expect(knowledgeRegister(VAULT)).rejects.toThrow('conflict without id')
  })

  it('removes the orphan source when confirmation fails', async () => {
    reply(
      { status: 201, json: { id: 'src 3' } },
      { status: 500, json: { error: 'scan refused' } },
      { status: 200 },
    )
    await expect(knowledgeRegister(VAULT)).rejects.toThrow('scan refused')
    expect(fetchMock).toHaveBeenCalledTimes(3)
    const del = callAt(2)
    expect(del.url).toBe('/api/knowledge/sources/src%203')
    expect(del.init.method).toBe('DELETE')
  })

  it('still reports the confirm failure when the cleanup delete also fails', async () => {
    reply(
      { status: 201, json: { id: 'src-4' } },
      { status: 500, jsonThrows: true },
      { status: 500, json: { error: 'delete refused' } },
    )
    await expect(knowledgeRegister(VAULT)).rejects.toThrow('knowledge confirm failed (500)')
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })
})

describe('knowledgeUnregister', () => {
  it('does nothing without a source id', async () => {
    await knowledgeUnregister(undefined)
    await knowledgeUnregister(null)
    await knowledgeUnregister('')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('deletes the source by encoded id', async () => {
    reply({ status: 200 })
    await knowledgeUnregister('a/b')
    const { url, init } = callAt(0)
    expect(url).toBe('/api/knowledge/sources/a%2Fb')
    expect(init.method).toBe('DELETE')
    expect(init.credentials).toBe('same-origin')
  })

  it('treats an already-gone source as success', async () => {
    reply({ status: 404, json: { error: 'not found' } })
    await expect(knowledgeUnregister('src-9')).resolves.toBeUndefined()
  })

  it('raises the server error on any other failure', async () => {
    reply({ status: 500, json: { error: 'locked' } })
    await expect(knowledgeUnregister('src-9')).rejects.toThrow('locked')
  })

  it('reports the status when the failure carries no error text', async () => {
    reply({ status: 500, jsonThrows: true })
    await expect(knowledgeUnregister('src-9')).rejects.toThrow('knowledge remove failed (500)')
  })
})
