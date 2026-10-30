import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const downloadBlob = vi.fn()
const downloadUrl = vi.fn()
vi.mock('./download', () => ({
  downloadBlob: (...args: unknown[]) => downloadBlob(...args),
  downloadUrl: (...args: unknown[]) => downloadUrl(...args),
}))

import { downloadFileToDisk } from './fileReadUrl'

const BIG = 50 * 1024 * 1024 + 1

function answer(status: number, length: number, body = 'x'): Response {
  return new Response(body, { status, headers: { 'Content-Length': String(length) } })
}

describe('downloadFileToDisk', () => {
  beforeEach(() => {
    downloadBlob.mockReset()
    downloadUrl.mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('saves a small file from the fetched bytes', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => answer(200, 1)))
    const onError = vi.fn()
    await downloadFileToDisk('/home/u/notes.txt', onError)
    expect(downloadBlob).toHaveBeenCalledOnce()
    expect(downloadBlob.mock.calls[0][1]).toBe('notes.txt')
    expect(downloadUrl).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('hands a large file to the browser instead of buffering it', async () => {
    let signal: AbortSignal | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        signal = init?.signal ?? undefined
        return answer(200, BIG)
      }),
    )
    const onError = vi.fn()
    await downloadFileToDisk('/home/u/huge.iso', onError)
    expect(downloadUrl).toHaveBeenCalledWith('/api/file-download?path=%2Fhome%2Fu%2Fhuge.iso', 'huge.iso')
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(signal?.aborted).toBe(true)
    expect(onError).not.toHaveBeenCalled()
  })

  it('reports a failed answer instead of saving its body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => answer(404, BIG, '{"error":"nope"}')))
    const onError = vi.fn()
    await downloadFileToDisk('/home/u/gone.txt', onError)
    expect(onError).toHaveBeenCalledOnce()
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(downloadUrl).not.toHaveBeenCalled()
  })
})
