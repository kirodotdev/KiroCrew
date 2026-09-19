import { describe, expect, it, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { useBackends, backendRow } from './useBackends'

function wrapper(client: QueryClient) {
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client }, children)
}

const PAYLOAD = {
  backends: [
    { id: '', label: 'Kiro CLI', is_global_default: true },
    { id: 'claude', label: 'Claude Code', is_global_default: false },
  ],
  invalid: [{ id: 'bad', label: 'bad', reasons: ['missing executable'] }],
  unroutable: [{ id: 'no-route', label: 'No Route', reason: 'no recognized routing' }],
}

describe('useBackends', () => {
  afterEach(() => vi.restoreAllMocks())

  it('exposes the three arrays and derives the default from is_global_default', async () => {
    vi.spyOn(api, 'backends').mockResolvedValue(PAYLOAD)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderHook(() => useBackends(), { wrapper: wrapper(client) })
    await waitFor(() => expect(view.result.current.backends.length).toBe(2))
    const s = view.result.current
    expect(s.invalid).toEqual(PAYLOAD.invalid)
    expect(s.unroutable).toEqual(PAYLOAD.unroutable)
    // The default is DERIVED from the flagged row, never a separate field — and
    // kiro-cli's `''` is a real id, so the flag (not truthiness) selects it.
    expect(s.defaultId).toBe('')
    view.unmount()
    client.clear()
  })

  it('empties every array on a failed fetch rather than keeping a stale answer', async () => {
    vi.spyOn(api, 'backends').mockRejectedValue(new Error('offline'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderHook(() => useBackends(), { wrapper: wrapper(client) })
    await waitFor(() => expect(view.result.current.isError).toBe(true))
    const s = view.result.current
    expect(s.backends).toEqual([])
    expect(s.invalid).toEqual([])
    expect(s.unroutable).toEqual([])
    expect(s.defaultId).toBe('')
    view.unmount()
    client.clear()
  })

  it('backendRow resolves an empty selection to the default row (kiro-cli included)', () => {
    const state = {
      backends: PAYLOAD.backends,
      invalid: [],
      unroutable: [],
      defaultId: '',
      isError: false,
      isLoading: false,
    }
    // Empty id inherits the default, which is kiro-cli (`''`) — the row must be
    // found by matching the id, not by truthiness (which would skip `''`).
    expect(backendRow(state, '')?.label).toBe('Kiro CLI')
    expect(backendRow(state, 'claude')?.label).toBe('Claude Code')
    expect(backendRow(state, 'ghost')).toBeUndefined()
  })
})
