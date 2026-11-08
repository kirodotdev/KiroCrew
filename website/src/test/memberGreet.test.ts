/**
 * `memberGreet` -- a crewmate's first greeting has no user text to follow, so
 * the request carries the language this tab renders (`X-UI-Lang`), which is the
 * server's only language signal on a "follow the browser" install.
 */
import i18next from 'i18next'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

describe('memberGreet', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    vi.restoreAllMocks()
  })

  it('sends the rendered language as X-UI-Lang', async () => {
    vi.spyOn(i18next, 'resolvedLanguage', 'get').mockReturnValue('zh-CN')
    fetchSpy.mockResolvedValueOnce(json({ outcome: 'started' }))
    const out = await api.memberGreet('mate')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/members/mate/greet')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['X-UI-Lang']).toBe('zh-CN')
    expect(out.outcome).toBe('started')
  })
})
