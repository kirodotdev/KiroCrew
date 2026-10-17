/**
 * `api.agentCatalog` keeps every read independent so a refresh cannot join an
 * older in-flight request and publish stale roster data.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api } from '../api/client'

type Resolve = (body: unknown) => void

describe('api.agentCatalog independent reads', () => {
  let pending: Resolve[]
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    pending = []
    fetchMock = vi.fn(() => new Promise<Response>(resolve => {
      pending.push(body => resolve(new Response(JSON.stringify(body), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      })))
    }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  const sessionHeader = (call: number) =>
    (fetchMock.mock.calls[call][1] as RequestInit).headers as Record<string, string>

  it('keeps simultaneous unscoped reads independent', async () => {
    const stale = api.agentCatalog()
    const refreshed = api.agentCatalog()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    pending[1]({ agents: [{ name: 'new' }], default_agent: 'new' })
    expect((await refreshed).default_agent).toBe('new')
    pending[0]({ agents: [{ name: 'old' }], default_agent: 'old' })
    expect((await stale).default_agent).toBe('old')
  })

  it('does not share scoped reads when a session project changes in flight', async () => {
    const unscoped = api.agentCatalog()
    const oldProject = api.agentCatalog('chat-1')
    const newProject = api.agentCatalog('chat-1')
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(sessionHeader(0)['X-Session-Key']).toBe('dashboard:ui')
    expect(sessionHeader(1)['X-Session-Key']).toBe('chat-1')
    expect(sessionHeader(2)['X-Session-Key']).toBe('chat-1')
    pending[0]({ agents: [], default_agent: '' })
    pending[1]({ agents: [{ name: 'old-project' }], default_agent: 'old-project' })
    pending[2]({ agents: [{ name: 'new-project' }], default_agent: 'new-project' })
    const [, oldResult, newResult] = await Promise.all([unscoped, oldProject, newProject])
    expect(oldResult.default_agent).toBe('old-project')
    expect(newResult.default_agent).toBe('new-project')
  })

  it('reads the server again once the pending read has settled', async () => {
    const first = api.agentCatalog()
    pending[0]({ agents: [], default_agent: 'a' })
    await first
    const second = api.agentCatalog()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    pending[1]({ agents: [], default_agent: 'b' })
    expect((await second).default_agent).toBe('b')
  })

  it('does not hold on to a failed read', async () => {
    fetchMock.mockImplementationOnce(() => Promise.reject(new TypeError('network down')))
    await expect(api.agentCatalog()).rejects.toThrow('network down')
    const second = api.agentCatalog()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    pending[0]({ agents: [], default_agent: 'ok' })
    expect((await second).default_agent).toBe('ok')
  })
})
