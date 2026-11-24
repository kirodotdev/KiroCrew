/**
 * The composer upload's XMLHttpRequest sender must not put a request on the wire
 * when the caller's signal is already aborted. It cancels by listening for the
 * signal's `abort` event, and that event never fires again for a signal that
 * aborted before the listener was added, so the send has to check first.
 *
 * `uploadComposerFiles` is replaced by a pass-through that hands the sender the
 * signal as given, so the check is pinned in the sender itself rather than in
 * whichever caller happens to stop an aborted upload earlier today.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { FakeXhr, stubXhr } from './fakeXhr'

vi.mock('../utils/composerUpload', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/composerUpload')>()
  return {
    ...actual,
    uploadComposerFiles: async (files: File[], post: (body: FormData) => Promise<Response>) => {
      const fd = new FormData()
      files.forEach(f => fd.append('file', f))
      await post(fd)
      return { paths: [], resized: [], resizedByPath: {} }
    },
  }
})

const { api } = await import('../api/client')

afterEach(() => { vi.unstubAllGlobals() })

describe('composer upload sender: a signal aborted before the send', () => {
  it('rejects with an AbortError and sends nothing', async () => {
    stubXhr()
    const controller = new AbortController()
    controller.abort()

    const upload = api.uploadFiles([new File(['abc'], 'a.txt', { type: 'text/plain' })], controller.signal)

    await expect(upload).rejects.toMatchObject({ name: 'AbortError' })
    expect(FakeXhr.sent).toHaveLength(0)
  })
})
