import { afterEach, describe, expect, it, vi } from 'vitest'

import { designCritiqueApi } from '../apps/design-critique/api'

/**
 * The critic reasons over finished images and the gateway builds image blocks
 * only from a send's structured list: a screen named in the prompt text alone
 * is a mention and ships no picture. So `send` must carry the screens as
 * `meta.images`, the same list every other picture producer hands on.
 */
describe('design critique send carries its screens as the structured list', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('a send with screens POSTs meta.images beside the prompt', async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: JSON.parse(String(init?.body)) })
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const paths = ['/home/me/.kiro/crew/uploads/a.png', '/home/me/.kiro/crew/uploads/b.png']
    await designCritiqueApi.send('slot-1', 'critique these', paths)
    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe('/api/chat')
    expect(calls[0].body.message).toBe('critique these')
    expect(calls[0].body.slot).toBe('slot-1')
    expect(calls[0].body.meta).toEqual({ images: paths })
  })

  it('a text-only send carries no meta', async () => {
    const calls: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push(JSON.parse(String(init?.body)))
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    await designCritiqueApi.send('slot-1', 'what do you see?')
    expect(calls).toHaveLength(1)
    expect('meta' in calls[0]).toBe(false)
  })
})
