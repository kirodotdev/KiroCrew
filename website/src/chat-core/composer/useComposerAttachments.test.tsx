/**
 * The chat-core Attach staging hook, driven with no Redux store, no app SDK
 * and no dashboard client: the host's `post` is the only transport.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useComposerAttachments, type ComposerAttachmentsOptions } from './useComposerAttachments'

const pdf = (name = 'spec.pdf') => new File(['x'], name, { type: 'application/pdf' })
const ok = (paths: string[]) => new Response(JSON.stringify({ paths }), { status: 200 })

function setup(over: Partial<ComposerAttachmentsOptions> = {}) {
  const post = vi.fn<ComposerAttachmentsOptions['post']>()
  const initial: ComposerAttachmentsOptions = {
    resetKey: 'a', granted: true, post, deniedNotice: () => 'not here', ...over,
  }
  const hook = renderHook((p: ComposerAttachmentsOptions) => useComposerAttachments(p), { initialProps: initial })
  return { post: over.post ? (over.post as typeof post) : post, hook, initial }
}

describe('useComposerAttachments', () => {
  it('stage adds gateway-side paths after the staged ones, deduped, with no upload or grant', () => {
    const { post, hook } = setup({ granted: false })
    act(() => { hook.result.current.stage(['/w/a.ts']) })
    act(() => { hook.result.current.stage(['/w/b.ts', '/w/a.ts']) })
    expect(hook.result.current.files).toEqual(['/w/a.ts', '/w/b.ts'])
    expect(post).not.toHaveBeenCalled()
    expect(hook.result.current.notice).toBe('')
  })

  it('without the grant posts nothing and shows the host notice', () => {
    const { post, hook } = setup({ granted: false })
    act(() => { hook.result.current.addFiles([pdf()]) })
    expect(post).not.toHaveBeenCalled()
    expect(hook.result.current.notice).toBe('not here')
    expect(hook.result.current.files).toEqual([])
  })

  it('uploads through the host post and stages the returned paths', async () => {
    const { post, hook } = setup()
    post.mockResolvedValueOnce(ok(['/up/spec.pdf']))
    act(() => { hook.result.current.addFiles([pdf()]) })
    expect(hook.result.current.uploading).toBe(true)
    expect(hook.result.current.uploadingNames).toEqual(['spec.pdf'])
    await waitFor(() => expect(hook.result.current.files).toEqual(['/up/spec.pdf']))
    expect(hook.result.current.uploading).toBe(false)
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toBeInstanceOf(FormData)
    expect(post.mock.calls[0][1]).toBeInstanceOf(AbortSignal)
  })

  it('a refused upload shows the server reason and stages nothing', async () => {
    const { post, hook } = setup()
    post.mockResolvedValueOnce(new Response(JSON.stringify({ error: 'too big' }), { status: 413 }))
    act(() => { hook.result.current.addFiles([pdf()]) })
    await waitFor(() => expect(hook.result.current.notice).toMatch(/too big/))
    expect(hook.result.current.files).toEqual([])
  })

  it('send-time recovery: clear, then restore puts the failed files first', async () => {
    const { post, hook } = setup()
    post.mockResolvedValueOnce(ok(['/up/a.pdf']))
    act(() => { hook.result.current.addFiles([pdf('a.pdf')]) })
    await waitFor(() => expect(hook.result.current.files).toEqual(['/up/a.pdf']))
    act(() => { hook.result.current.clear() })
    expect(hook.result.current.files).toEqual([])
    post.mockResolvedValueOnce(ok(['/up/b.pdf']))
    act(() => { hook.result.current.addFiles([pdf('b.pdf')]) })
    await waitFor(() => expect(hook.result.current.files).toEqual(['/up/b.pdf']))
    act(() => { hook.result.current.restore(['/up/a.pdf', '/up/b.pdf']) })
    expect(hook.result.current.files).toEqual(['/up/a.pdf', '/up/b.pdf'])
    act(() => { hook.result.current.removeFile('/up/a.pdf') })
    expect(hook.result.current.files).toEqual(['/up/b.pdf'])
  })

  it('a reset key change aborts the upload and drops its late result', async () => {
    let resolve!: (r: Response) => void
    const { post, hook, initial } = setup()
    post.mockImplementationOnce(() => new Promise<Response>(r => { resolve = r }))
    act(() => { hook.result.current.addFiles([pdf()]) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const signal = post.mock.calls[0][1]
    hook.rerender({ ...initial, resetKey: 'b' })
    expect(signal.aborted).toBe(true)
    expect(hook.result.current.uploading).toBe(false)
    await act(async () => { resolve(ok(['/up/late.pdf'])) })
    expect(hook.result.current.files).toEqual([])
    expect(hook.result.current.notice).toBe('')
  })
})
