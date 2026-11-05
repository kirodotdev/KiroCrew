import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_BASE,
  PptxMakerError,
  artifactUrl,
  fetchArtifactJson,
  fetchArtifactText,
  pptxMakerApi,
} from './api'

type Call = { url: string; init: RequestInit | undefined }

let calls: Call[]
let nextResponse: () => Response

beforeEach(() => {
  calls = []
  nextResponse = () => new Response(JSON.stringify({}), { status: 200 })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return nextResponse()
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status })
}

describe('artifactUrl', () => {
  it('joins a relative artifact path onto the API base', () => {
    expect(artifactUrl('preview/slide-1.svg')).toBe(`${API_BASE}/preview/slide-1.svg`)
  })

  it('strips leading slashes from the artifact path', () => {
    expect(artifactUrl('///decks/a/defs.json')).toBe(`${API_BASE}/decks/a/defs.json`)
  })
})

describe('transport error handling', () => {
  it('surfaces the server error message from a JSON error body', async () => {
    nextResponse = () => json({ error: 'deck is locked' }, 409)
    const err = await pptxMakerApi.deck('d1').catch(e => e)
    expect(err).toBeInstanceOf(PptxMakerError)
    expect(err.message).toBe('deck is locked')
    expect(err.status).toBe(409)
    expect(err.name).toBe('PptxMakerError')
  })

  it('falls back to the status line when the error body is not JSON', async () => {
    nextResponse = () => new Response('<html>bad gateway</html>', { status: 502 })
    const err = await pptxMakerApi.engine().catch(e => e)
    expect(err.message).toBe('HTTP 502')
    expect(err.status).toBe(502)
  })

  it('keeps the status line when a JSON error body carries no error string', async () => {
    nextResponse = () => json({ detail: 'nope' }, 500)
    const err = await pptxMakerApi.engine().catch(e => e)
    expect(err.message).toBe('HTTP 500')
  })

  it('resolves to undefined for a 204 response with no body', async () => {
    nextResponse = () => new Response(null, { status: 204 })
    await expect(pptxMakerApi.deleteStyle('old')).resolves.toBeUndefined()
  })

  it('sends same-origin credentials on every request', async () => {
    await pptxMakerApi.engine()
    expect(calls[0].init).toMatchObject({ credentials: 'same-origin' })
  })
})

describe('artifact fetches', () => {
  it('returns artifact text on success', async () => {
    nextResponse = () => new Response('# Brief', { status: 200 })
    await expect(fetchArtifactText('decks/a/brief.md')).resolves.toBe('# Brief')
    expect(calls[0].url).toBe(`${API_BASE}/decks/a/brief.md`)
    expect(calls[0].init).toMatchObject({ credentials: 'same-origin' })
  })

  it('throws a PptxMakerError with the status when artifact text fails', async () => {
    nextResponse = () => new Response('missing', { status: 404 })
    const err = await fetchArtifactText('decks/a/none.md').catch(e => e)
    expect(err).toBeInstanceOf(PptxMakerError)
    expect(err.status).toBe(404)
    expect(err.message).toBe('HTTP 404')
  })

  it('parses artifact JSON on success', async () => {
    nextResponse = () => json({ defs: '<defs/>' })
    await expect(fetchArtifactJson<{ defs: string }>('decks/a/defs.json')).resolves.toEqual({ defs: '<defs/>' })
    expect(calls[0].init).toMatchObject({ credentials: 'same-origin' })
  })

  it('throws a PptxMakerError with the status when artifact JSON fails', async () => {
    nextResponse = () => new Response('', { status: 500 })
    const err = await fetchArtifactJson('decks/a/defs.json').catch(e => e)
    expect(err).toBeInstanceOf(PptxMakerError)
    expect(err.status).toBe(500)
  })
})

describe('engine and dependency endpoints', () => {
  it('reads engine status with a GET', async () => {
    nextResponse = () => json({ ready: true, clone: true, venv: true, pinnedTag: 'v1', provision: { state: 'idle', log: '', elapsed: 0 } })
    const status = await pptxMakerApi.engine()
    expect(status.ready).toBe(true)
    expect(calls[0].url).toBe(`${API_BASE}/engine`)
    expect(calls[0].init?.method).toBeUndefined()
  })

  it('starts engine provisioning with a POST', async () => {
    nextResponse = () => json({ state: 'running' })
    await expect(pptxMakerApi.provisionEngine()).resolves.toEqual({ state: 'running' })
    expect(calls[0]).toMatchObject({ url: `${API_BASE}/engine/provision`, init: { method: 'POST' } })
  })

  it('reads the dependency list', async () => {
    nextResponse = () => json({ labels: {}, present: {}, managed: {}, missing: ['libreoffice'], hints: {} })
    const deps = await pptxMakerApi.deps()
    expect(deps.missing).toEqual(['libreoffice'])
    expect(calls[0].url).toBe(`${API_BASE}/deps`)
  })

  it('reads asset status', async () => {
    nextResponse = () => json({ ready: false, state: 'idle', tag: 't', sources: [] })
    await expect(pptxMakerApi.assets()).resolves.toMatchObject({ ready: false, tag: 't' })
    expect(calls[0].url).toBe(`${API_BASE}/assets`)
  })

  it('provisions assets without force by default', async () => {
    nextResponse = () => json({ state: 'running' })
    await pptxMakerApi.provisionAssets()
    expect(calls[0]).toMatchObject({ url: `${API_BASE}/assets/provision`, init: { method: 'POST' } })
  })

  it('provisions assets with force when asked', async () => {
    nextResponse = () => json({ state: 'running' })
    await pptxMakerApi.provisionAssets(true)
    expect(calls[0].url).toBe(`${API_BASE}/assets/provision?force=true`)
  })
})

describe('config endpoints', () => {
  it('reads the deck root config', async () => {
    nextResponse = () => json({ deckRoot: '/decks', default: '/srv/default-decks' })
    await expect(pptxMakerApi.config()).resolves.toEqual({ deckRoot: '/decks', default: '/srv/default-decks' })
    expect(calls[0].url).toBe(`${API_BASE}/config`)
  })

  it('saves a new deck root with a PUT carrying the JSON body', async () => {
    nextResponse = () => json({ saved: true, deckRoot: '/new' })
    await pptxMakerApi.setDeckRoot('/new')
    expect(calls[0].init?.method).toBe('PUT')
    expect(calls[0].init?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ deckRoot: '/new' })
  })
})

describe('deck endpoints', () => {
  it('lists decks', async () => {
    nextResponse = () => json({ decks: [] })
    await expect(pptxMakerApi.decks()).resolves.toEqual({ decks: [] })
    expect(calls[0].url).toBe(`${API_BASE}/decks`)
  })

  it('reads one deck by URL-encoded id', async () => {
    nextResponse = () => json({ deckId: 'a b/c' })
    await pptxMakerApi.deck('a b/c')
    expect(calls[0].url).toBe(`${API_BASE}/deck?id=a%20b%2Fc`)
  })
})

describe('style endpoints', () => {
  it('lists styles', async () => {
    nextResponse = () => json({ styles: [{ name: 'Clean' }] })
    await expect(pptxMakerApi.styles()).resolves.toEqual({ styles: [{ name: 'Clean' }] })
    expect(calls[0].url).toBe(`${API_BASE}/styles`)
  })

  it('reads one style by encoded name', async () => {
    nextResponse = () => json({ name: 'Dark & Bold', fullHtml: '<html/>' })
    await pptxMakerApi.style('Dark & Bold')
    expect(calls[0].url).toBe(`${API_BASE}/style?name=Dark%20%26%20Bold`)
  })

  it('imports a style HTML body into the encoded name', async () => {
    nextResponse = () => json({ imported: 'Clean' })
    await expect(pptxMakerApi.importStyle('Clean', '<html/>')).resolves.toEqual({ imported: 'Clean' })
    expect(calls[0]).toMatchObject({
      url: `${API_BASE}/styles/import?name=Clean`,
      init: { method: 'POST', body: '<html/>' },
    })
  })

  it('renames a style with a JSON POST', async () => {
    nextResponse = () => json({ renamed: { from: 'a', to: 'b' } })
    await pptxMakerApi.renameStyle('a', 'b')
    expect(calls[0].url).toBe(`${API_BASE}/styles/rename`)
    expect(calls[0].init?.method).toBe('POST')
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ name: 'a', to: 'b' })
  })

  it('pins a style with a JSON POST carrying the flag', async () => {
    nextResponse = () => json({ pinnedStyles: ['Clean'] })
    await pptxMakerApi.pinStyle('Clean', true)
    expect(calls[0].url).toBe(`${API_BASE}/styles/pin`)
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ name: 'Clean', pinned: true })
  })

  it('deletes a style by encoded name', async () => {
    nextResponse = () => json({ deleted: 'Old one' })
    await pptxMakerApi.deleteStyle('Old one')
    expect(calls[0]).toMatchObject({ url: `${API_BASE}/styles?name=Old%20one`, init: { method: 'DELETE' } })
  })
})

describe('template endpoints', () => {
  it('lists templates', async () => {
    nextResponse = () => json({ templates: [{ name: 'Corp' }] })
    await expect(pptxMakerApi.templates()).resolves.toEqual({ templates: [{ name: 'Corp' }] })
    expect(calls[0].url).toBe(`${API_BASE}/templates`)
  })

  it('imports a template file with its encoded name and description', async () => {
    nextResponse = () => json({ imported: 'Corp', metadata: {} })
    const file = new Blob(['pptx-bytes'])
    await pptxMakerApi.importTemplate('Corp Deck', file, 'Quarterly & board')
    expect(calls[0].url).toBe(
      `${API_BASE}/templates/import?name=Corp%20Deck&description=Quarterly%20%26%20board`,
    )
    expect(calls[0].init?.method).toBe('POST')
    expect(calls[0].init?.body).toBe(file)
  })

  it('defaults the template description to empty', async () => {
    nextResponse = () => json({ imported: 'Corp', metadata: {} })
    await pptxMakerApi.importTemplate('Corp', new Blob([]))
    expect(calls[0].url).toBe(`${API_BASE}/templates/import?name=Corp&description=`)
  })

  it('renames a template with a JSON POST', async () => {
    nextResponse = () => json({ renamed: { from: 'x', to: 'y' } })
    await pptxMakerApi.renameTemplate('x', 'y')
    expect(calls[0].url).toBe(`${API_BASE}/templates/rename`)
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ name: 'x', to: 'y' })
  })

  it('deletes a template by encoded name', async () => {
    nextResponse = () => json({ deleted: 'Corp' })
    await pptxMakerApi.deleteTemplate('Corp')
    expect(calls[0]).toMatchObject({ url: `${API_BASE}/templates?name=Corp`, init: { method: 'DELETE' } })
  })
})
