// The write endpoints' wire contract. Each new mutation must carry the identity
// the CLIENT rendered (spec_dir + the per-creation slot_key) so the backend can
// refuse a stale tab, and a failure must carry the backend's machine-readable
// `code` — matching on translated prose to recognise a conflict would work in
// exactly one locale.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { specApi, SpecApiError } from '../apps/spec-builder/api'

const API = '/api/apps/spec-builder'
const ID = { spec_dir: '/w/.kiro/specs/thing', slot_key: 'spec-builder-thing-deadbeef' }

function mockFetch(status = 200, payload: unknown = { ok: true }) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  } as unknown as Response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const bodyOf = (m: ReturnType<typeof mockFetch>) => JSON.parse(m.mock.calls[0][1].body as string)

describe('spec write API', () => {
  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('sends the document, its compare-and-swap base and the identity', async () => {
    const f = mockFetch(200, { ok: true, hash: 'a'.repeat(64) })
    await specApi.saveDoc('thing', 'requirements.md', '# text', 'b'.repeat(64), ID)

    expect(f.mock.calls[0][0]).toBe(API + '/specs/thing/doc')
    expect(f.mock.calls[0][1].method).toBe('PUT')
    expect(bodyOf(f)).toEqual({
      file: 'requirements.md',
      content: '# text',
      base_hash: 'b'.repeat(64),
      ...ID,
    })
  })

  it('sends the approved phase together with the hash reviewed', async () => {
    const f = mockFetch()
    await specApi.approve('thing', 'requirements', 'c'.repeat(64), ID)
    expect(f.mock.calls[0][0]).toBe(API + '/specs/thing/approve')
    expect(bodyOf(f)).toEqual({ phase: 'requirements', hash: 'c'.repeat(64), ...ID })
  })

  it('sends both the task index and its text hash', async () => {
    const f = mockFetch()
    await specApi.runTask('thing', 2, 'd'.repeat(64), ID)
    expect(f.mock.calls[0][0]).toBe(API + '/specs/thing/task')
    expect(bodyOf(f)).toEqual({ index: 2, hash: 'd'.repeat(64), ...ID })
  })

  it('sends the label, the archive flag and the copy name', async () => {
    const t = mockFetch()
    await specApi.setTitle('thing', 'Checkout rewrite', ID)
    expect(bodyOf(t)).toEqual({ title: 'Checkout rewrite', ...ID })
    vi.unstubAllGlobals()

    const a = mockFetch()
    await specApi.setArchived('thing', true, ID)
    expect(bodyOf(a)).toEqual({ archived: true, ...ID })
    vi.unstubAllGlobals()

    const d = mockFetch(201, { name: 'thing-copy' })
    await specApi.duplicate('thing', 'thing-copy', ID)
    expect(bodyOf(d)).toEqual({ new_name: 'thing-copy', ...ID })
  })

  it('omits an identity field the client does not have rather than claiming ""', async () => {
    // An older tab predates these fields, and the backend treats "" as a CLAIM.
    const f = mockFetch()
    await specApi.setTitle('thing', 'x')
    expect(bodyOf(f)).toEqual({ title: 'x' })
  })

  it('surfaces a conflict as a code, not only as prose', async () => {
    mockFetch(409, { code: 'doc_conflict', error: 'this document changed', current_hash: 'e'.repeat(64) })

    await expect(
      specApi.saveDoc('thing', 'requirements.md', '# text', 'f'.repeat(64), ID),
    ).rejects.toMatchObject({ code: 'doc_conflict', message: 'this document changed' })
  })

  it('still throws a typed error when the body carries no code', async () => {
    mockFetch(500, { error: 'boom' })
    const err = await specApi.setArchived('thing', true, ID).catch((e) => e)
    expect(err).toBeInstanceOf(SpecApiError)
    expect(err.code).toBe('')
  })

  it('fetches the archived set only when asked', async () => {
    const plain = mockFetch(200, { specs: [] })
    await specApi.list()
    expect(plain.mock.calls[0][0]).toBe(API + '/specs')
    vi.unstubAllGlobals()

    const archived = mockFetch(200, { specs: [] })
    await specApi.list(undefined, true)
    expect(archived.mock.calls[0][0]).toBe(API + '/specs?archived=1')
  })
})
