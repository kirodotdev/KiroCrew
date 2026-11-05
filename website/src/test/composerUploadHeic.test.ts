import { describe, it, expect, vi, beforeEach } from 'vitest'

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

const { uploadComposerFiles } = await import('../utils/composerUpload')
const { HeicPrepareError } = await import('../utils/heicImage')

/** The shared upload body every composer path (paste, picker, drop, ChatEmbed)
 *  goes through: the HEIC step runs before the POST and before the resize. */

const ok = (paths: string[]) => new Response(JSON.stringify({ paths }), { status: 200 })

describe('uploadComposerFiles with HEIC photos', () => {
  beforeEach(() => {
    prepareHeicImage.mockReset()
    resizeImageForModel.mockReset()
    resizeImageForModel.mockImplementation(async (file: File) => ({ file, info: null }))
  })

  it('posts the converted JPEG in place of the HEIC, keyed to its server path', async () => {
    const heic = new File([new Uint8Array(4)], 'IMG_0001.HEIC', { type: 'image/heic' })
    const jpeg = new File([new Uint8Array(2)], 'IMG_0001.jpg', { type: 'image/jpeg' })
    const info = { name: 'IMG_0001.HEIC', fromW: 4032, fromH: 3024, toW: 1568, toH: 1176, fromBytes: 4, toBytes: 2 }
    prepareHeicImage.mockImplementation(async (f: File) => (f === heic ? { file: jpeg, info } : null))
    const doc = new File(['x'], 'notes.md', { type: 'text/markdown' })
    let sent: File[] = []
    const res = await uploadComposerFiles([heic, doc], async body => {
      sent = body.getAll('file') as File[]
      return ok(['/u/a_IMG_0001.jpg', '/u/b_notes.md'])
    })
    expect(sent.map(f => [f.name, f.type])).toEqual([['IMG_0001.jpg', 'image/jpeg'], ['notes.md', 'text/markdown']])
    // The converted file is not resized a second time.
    expect(resizeImageForModel).toHaveBeenCalledTimes(1)
    expect(resizeImageForModel).toHaveBeenCalledWith(doc)
    expect(res.paths).toEqual(['/u/a_IMG_0001.jpg', '/u/b_notes.md'])
    expect(res.resizedByPath).toEqual({ '/u/a_IMG_0001.jpg': info })
  })

  it('returns a refused conversion as the batch error and posts nothing', async () => {
    prepareHeicImage.mockRejectedValue(new HeicPrepareError('unreadable', 'bad.heic', 'bad.heic could not be read'))
    const post = vi.fn()
    const res = await uploadComposerFiles([new File([new Uint8Array(1)], 'bad.heic', { type: 'image/heic' })], post)
    expect(post).not.toHaveBeenCalled()
    expect(res).toEqual({ paths: [], error: 'bad.heic could not be read', resized: [], resizedByPath: {} })
  })

  it('ends the upload with an AbortError the moment Cancel aborts a conversion in progress', async () => {
    // The conversion never settles on its own, as a long decode would not
    // within the test: only the abort can end the upload.
    prepareHeicImage.mockReturnValue(new Promise(() => {}))
    const controller = new AbortController()
    const post = vi.fn()
    const upload = uploadComposerFiles([new File([new Uint8Array(1)], 'big.heic', { type: 'image/heic' })], post, controller.signal)
    controller.abort()
    await expect(upload).rejects.toMatchObject({ name: 'AbortError' })
    expect(post).not.toHaveBeenCalled()
  })

  it('prepares nothing and posts nothing for an upload already cancelled', async () => {
    const controller = new AbortController()
    controller.abort()
    const post = vi.fn()
    await expect(uploadComposerFiles([new File(['x'], 'a.heic', { type: 'image/heic' })], post, controller.signal))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(prepareHeicImage).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
  })

  it('stops before the next file when Cancel lands between two preparations', async () => {
    const controller = new AbortController()
    const first = new File(['1'], 'one.heic', { type: 'image/heic' })
    const second = new File(['2'], 'two.heic', { type: 'image/heic' })
    prepareHeicImage.mockImplementation(async (f: File) => {
      if (f === first) controller.abort()
      return { file: f, info: null }
    })
    const post = vi.fn()
    await expect(uploadComposerFiles([first, second], post, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(prepareHeicImage).toHaveBeenCalledTimes(1)
    expect(post).not.toHaveBeenCalled()
  })

  it('lets an unexpected error propagate, as a transport error does', async () => {
    prepareHeicImage.mockRejectedValue(new RangeError('boom'))
    await expect(uploadComposerFiles([new File(['x'], 'a.heic')], vi.fn())).rejects.toThrow('boom')
  })
})
