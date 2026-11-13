/**
 * How `postComments` reads a failed answer. Only the gateway's own error body,
 * which names a `code`, means nothing reached the forge; any other failure,
 * including a proxy's 502 page, leaves delivery unknown.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CommentsRefusedError, issueRadarApi } from './api'

function answer(status: number, body: string) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status })))
}

const send = () => issueRadarApi.postComments({ owner: 'o', repo: 'r' }, 1, 'issue', [{ body: 'x', thread_id: null }])

describe('postComments failures', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('reads the gateway error body as a refusal, whatever its status', async () => {
    answer(502, JSON.stringify({ error: 'forge said no', code: 'provider_error' }))
    const err = await send().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(CommentsRefusedError)
    expect((err as Error).message).toBe('forge said no')
  })

  it('leaves a proxy error page as unknown delivery', async () => {
    answer(502, '<html>Bad Gateway</html>')
    const err = await send().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(Error)
    expect(err).not.toBeInstanceOf(CommentsRefusedError)
  })

  it('leaves a JSON error without a code as unknown delivery', async () => {
    answer(504, JSON.stringify({ message: 'timeout' }))
    const err = await send().catch((e: unknown) => e)
    expect(err).not.toBeInstanceOf(CommentsRefusedError)
  })
})
