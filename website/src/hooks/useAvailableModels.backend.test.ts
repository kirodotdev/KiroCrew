import { describe, expect, it, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { useAvailableModelsQuery } from './useAvailableModels'

function wrapper(client: QueryClient) {
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
}

/** One `/api/models` row shape (the fields the adapter reads). */
const row = (name: string) => ({ model_name: name, description: name, context_window: 200000 })

describe('useAvailableModelsQuery backend re-key', () => {
  afterEach(() => vi.restoreAllMocks())

  it('passes the backend to the model fetch so the catalog is that harness\'s', async () => {
    const models = vi.spyOn(api, 'models').mockResolvedValue([row('opus')])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderHook(() => useAvailableModelsQuery({ backend: 'claude' }), {
      wrapper: wrapper(client),
    })
    await waitFor(() => expect(view.result.current.data.some(m => m.name === 'opus')).toBe(true))
    // The re-key threads the backend all the way to the wire.
    expect(models).toHaveBeenCalledWith('claude')
    view.unmount()
    client.clear()
  })

  it('keys the cache on the backend so two backends do not share one catalog', async () => {
    // Different catalogs per backend; a shared cache key would let one overwrite
    // the other for every reader.
    vi.spyOn(api, 'models').mockImplementation(async (backend?: string) =>
      backend === 'claude' ? [row('sonnet')] : [row('kiro-default')],
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const primary = renderHook(() => useAvailableModelsQuery({}), { wrapper: wrapper(client) })
    await waitFor(() => expect(primary.result.current.data.some(m => m.name === 'kiro-default')).toBe(true))

    const perChat = renderHook(() => useAvailableModelsQuery({ backend: 'claude' }), { wrapper: wrapper(client) })
    await waitFor(() => expect(perChat.result.current.data.some(m => m.name === 'sonnet')).toBe(true))

    // The two live under distinct cache keys — the backend-scoped list did not
    // clobber the primary one.
    expect(client.getQueryData(['available-models', 'acp'])).toBeTruthy()
    expect(client.getQueryData(['available-models', 'acp', 'claude'])).toBeTruthy()
    expect(primary.result.current.data.some(m => m.name === 'kiro-default')).toBe(true)

    primary.unmount()
    perChat.unmount()
    client.clear()
  })
})
