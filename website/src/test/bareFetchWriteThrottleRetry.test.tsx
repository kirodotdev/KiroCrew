// A dashboard write that used to go out on the global `fetch` now goes through
// `apiTransport.request`, so a tunnel-edge 429 is re-sent like every other
// transport write (#40178). Pinned on one migrated site: removing a file from
// the knowledge library (`DELETE /api/knowledge/sources/<id>`).
import { describe, it, expect, vi, afterEach } from 'vitest'
import { createElement, type ReactNode } from 'react'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// Loading the client installs the transport `apiTransport` forwards to, as the app does at startup.
import '../api/client'
import { apiTransport } from '../api/apiTransport'
import { useFileKnowledgeState } from '../components/useMarkdownPanelFileState'

const EDGE_THROTTLE_BODY = '{"message":"Rate exceeded","throttlingReasons":null}'

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

afterEach(() => {
  vi.unstubAllGlobals()
})

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return createElement(QueryClientProvider, { client }, children)
}

describe('a migrated bare-fetch write (#40178)', () => {
  it('re-sends a knowledge-source DELETE the tunnel edge throttled', async () => {
    let deletes = 0
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      if (input === '/api/knowledge/config') return json(200, { enabled: true, supported_formats: ['md'] })
      if (input.startsWith('/api/knowledge/sources?uri=')) return json(200, [{ id: 'src-1', source_type: 'local_file' }])
      if (input === '/api/knowledge/sources/src-1' && init?.method === 'DELETE') {
        deletes += 1
        return deletes === 1 ? new Response(EDGE_THROTTLE_BODY, { status: 429 }) : new Response(null, { status: 204 })
      }
      return json(404, { error: 'unexpected ' + input })
    })
    vi.stubGlobal('fetch', fetchMock)
    const onError = vi.fn()

    const { result } = renderHook(() => useFileKnowledgeState('/notes/a.md', onError), { wrapper })
    await waitFor(() => expect(result.current.sourceId).toBe('src-1'))
    act(() => result.current.remove())

    // One backoff (1 s plus up to 0.5 s of jitter) sits between the two sends.
    await waitFor(() => expect(deletes).toBe(2), { timeout: 4_000 })
    await waitFor(() => expect(result.current.removing).toBe(false))
    expect(onError).not.toHaveBeenCalled()
    // The arguments reach the wire as the call site wrote them: no header is added.
    const deleteCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/knowledge/sources/src-1')
    expect(deleteCalls[1]).toEqual(['/api/knowledge/sources/src-1', { method: 'DELETE' }])
  })

  it('request sends on the global fetch, unchanged, before client.ts installs the transport', async () => {
    vi.resetModules()
    const fresh = await import('../api/apiTransport')
    const fetchMock = vi.fn(async () => new Response(EDGE_THROTTLE_BODY, { status: 429 }))
    vi.stubGlobal('fetch', fetchMock)
    const init = { method: 'DELETE' }
    const response = await fresh.apiTransport.request('/api/knowledge/sources/src-1', init)
    expect(response.status).toBe(429)
    expect(fetchMock.mock.calls).toEqual([['/api/knowledge/sources/src-1', init]])
  })

  it('request keeps a one-argument call one argument', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await apiTransport.request('/api/knowledge/config')
    expect(fetchMock.mock.calls[0]).toEqual(['/api/knowledge/config'])
  })
})
