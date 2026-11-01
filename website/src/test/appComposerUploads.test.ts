import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const prepareHeicImage = vi.fn()
vi.mock('../utils/heicImage', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/heicImage')>()),
  prepareHeicImage: (...args: unknown[]) => prepareHeicImage(...args),
}))
const resizeImageForModel = vi.fn()
vi.mock('../utils/resizeImage', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/resizeImage')>()),
  resizeImageForModel: (...args: unknown[]) => resizeImageForModel(...args),
}))

const { designCritiqueApi } = await import('../apps/design-critique/api')
const { ingestFiles } = await import('../apps/mochi/panel/composerDrop')
const { HeicPrepareError } = await import('../utils/heicImage')

/** The Design Critique and Mochi composers upload through the same body as
 *  the main chat composer, so a HEIC photo is converted and an oversized image
 *  downscaled before the POST, not sent as-is. */

const heic = () => new File([new Uint8Array(4)], 'IMG_0001.HEIC', { type: 'image/heic' })
const jpeg = () => new File([new Uint8Array(2)], 'IMG_0001.jpg', { type: 'image/jpeg' })
const big = () => new File([new Uint8Array(8)], 'big.png', { type: 'image/png' })
const small = () => new File([new Uint8Array(3)], 'small.png', { type: 'image/png' })
const shrunk = () => new File([new Uint8Array(1)], 'big.jpg', { type: 'image/jpeg' })
const resizeInfo = { name: 'big.png', fromW: 6000, fromH: 4000, toW: 1568, toH: 1045, fromBytes: 8, toBytes: 1 }

type Upload = { name: string; type: string }
let sent: Upload[][]

function stubUpload(paths: string[]) {
  const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
    sent.push(((init.body as FormData).getAll('file') as File[]).map(f => ({ name: f.name, type: f.type })))
    return new Response(JSON.stringify({ paths }), { status: 200 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Each app's upload, read as the paths it got or the error it shows: Design
 *  Critique throws its error, Mochi returns it. */
type Outcome = { paths: string[]; error?: string }
const uploaders: Array<[string, (files: File[]) => Promise<Outcome>]> = [
  ['design critique', files => designCritiqueApi.uploadFiles(files).then(
    r => ({ paths: r.paths }), (e: Error) => ({ paths: [], error: e.message }))],
  ['mochi', async files => {
    const r = await ingestFiles(files)
    return { paths: [...r.images, ...r.files], ...(r.error !== undefined ? { error: r.error } : {}) }
  }],
]

describe.each(uploaders)('%s composer upload', (_app, upload) => {
  beforeEach(() => {
    sent = []
    prepareHeicImage.mockReset()
    resizeImageForModel.mockReset()
    prepareHeicImage.mockImplementation(async (f: File) => (f.type === 'image/heic' ? { file: jpeg(), info: null } : null))
    resizeImageForModel.mockImplementation(async (f: File) => (f.name === 'big.png' ? { file: shrunk(), info: resizeInfo } : { file: f, info: null }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('posts the converted JPEG in place of a HEIC photo', async () => {
    const fetchMock = stubUpload(['/u/a_IMG_0001.jpg'])
    expect(await upload([heic()])).toEqual({ paths: ['/u/a_IMG_0001.jpg'] })
    expect(fetchMock).toHaveBeenCalledWith('/api/upload/file', expect.objectContaining({ method: 'POST' }))
    expect(sent).toEqual([[{ name: 'IMG_0001.jpg', type: 'image/jpeg' }]])
  })

  it('posts the downscaled copy of an oversized image', async () => {
    stubUpload(['/u/a_big.jpg'])
    expect(await upload([big()])).toEqual({ paths: ['/u/a_big.jpg'] })
    expect(sent).toEqual([[{ name: 'big.jpg', type: 'image/jpeg' }]])
  })

  it('posts a small PNG unchanged', async () => {
    stubUpload(['/u/a_small.png'])
    expect(await upload([small()])).toEqual({ paths: ['/u/a_small.png'] })
    expect(sent).toEqual([[{ name: 'small.png', type: 'image/png' }]])
  })

  it('refuses a HEIC photo it cannot convert with that reason, before any POST', async () => {
    prepareHeicImage.mockImplementation(async () => { throw new HeicPrepareError('too_large', 'IMG_0001.HEIC', 'That photo is too large to convert.') })
    const fetchMock = stubUpload([])
    expect(await upload([heic()])).toEqual({ paths: [], error: 'That photo is too large to convert.' })
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('upload refusals keep each app\'s own message', () => {
  beforeEach(() => {
    prepareHeicImage.mockReset()
    resizeImageForModel.mockReset()
    prepareHeicImage.mockResolvedValue(null)
    resizeImageForModel.mockImplementation(async (f: File) => ({ file: f, info: null }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('design critique throws the server\'s error text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'File type not allowed' }), { status: 415 })))
    await expect(designCritiqueApi.uploadFiles([small()])).rejects.toThrow('File type not allowed')
  })

  it('mochi falls back to the status code when the body names no reason', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 500 })))
    expect((await ingestFiles([small()])).error).toBe('Upload failed (500)')
  })
})
