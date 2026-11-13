import { afterEach, describe, expect, it, vi } from 'vitest'
import { writeFile } from './usePanelDocumentActions'

const path = '/repo/formal.md'
const revision = 'a'.repeat(64)

function readResponse(text: string, headers: Record<string, string> = {}) {
  return new Response(text, { headers: { 'X-Resource-Managed': 'true', 'X-File-Revision': revision, ...headers } })
}

afterEach(() => vi.unstubAllGlobals())

describe('owner resource save revisions', () => {
  it('posts the exact read revision only when its body still matches the buffer baseline', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(readResponse('baseline')).mockResolvedValueOnce(new Response('{"ok":true}'))
    vi.stubGlobal('fetch', fetch)
    await writeFile(path, 'edited', null, 'baseline')
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ path, content: 'edited', expectedRevision: revision })
  })

  it('refuses a changed baseline without posting even when the read has a fresh revision', async () => {
    const fetch = vi.fn().mockResolvedValue(readResponse('app changed it'))
    vi.stubGlobal('fetch', fetch)
    await expect(writeFile(path, 'edited', null, 'baseline')).rejects.toMatchObject({ status: 409, code: 'revision_conflict' })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][1]?.method).not.toBe('POST')
  })

  it.each([
    { 'X-File-Revision': '' },
    { 'X-Truncated': 'true' },
    { 'X-Redacted': 'true' },
    { 'X-Lossy-Decode': 'true' },
  ])('refuses a managed read lacking a complete revision: %j', async headers => {
    const fetch = vi.fn().mockResolvedValue(readResponse('baseline', headers))
    vi.stubGlobal('fetch', fetch)
    await expect(writeFile(path, 'edited', null, 'baseline')).rejects.toThrow()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('keeps the unmanaged POST payload unchanged', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response('ordinary')).mockResolvedValueOnce(new Response('{"ok":true}'))
    vi.stubGlobal('fetch', fetch)
    await writeFile(path, 'edited', null, 'ordinary')
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ path, content: 'edited' })
  })

  it('keeps the authoritative owner-gate response when the preparatory read is unauthorized', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response('', { status: 401 })).mockResolvedValueOnce(new Response('{}', { status: 401 }))
    vi.stubGlobal('fetch', fetch)
    await expect(writeFile(path, 'edited', null, 'baseline')).rejects.toThrow('Save failed: 401')
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('leaves unmanaged writes available when the extra read fails', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response('', { status: 500 })).mockResolvedValueOnce(new Response('{"ok":true}'))
    vi.stubGlobal('fetch', fetch)
    await writeFile(path, 'edited', null, 'baseline')
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ path, content: 'edited' })
  })
})
