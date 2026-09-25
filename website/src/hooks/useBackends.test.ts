import { describe, expect, it, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import { useBackends, backendRow, backendChipLabel, backendChoiceExists } from './useBackends'

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

  it('backendChipLabel never calls a PINNED chat "default" when the listing cannot name it', () => {
    const loaded = {
      backends: PAYLOAD.backends, invalid: [], unroutable: [], defaultId: '', isError: false, isLoading: false,
    }
    const errored = { ...loaded, backends: [], isError: true }
    const loading = { ...loaded, backends: [], isLoading: true }
    // Named by the listing: display label wins, for a pin and for the inherited default.
    expect(backendChipLabel(loaded, 'claude', 'Default backend')).toBe('Claude Code')
    expect(backendChipLabel(loaded, undefined, 'Default backend')).toBe('Kiro CLI')
    // Pinned but unnamed (errored / loading / dropped): the raw id, never "default".
    expect(backendChipLabel(errored, 'my-acp', 'Default backend')).toBe('my-acp')
    expect(backendChipLabel(loading, 'my-acp', 'Default backend')).toBe('my-acp')
    expect(backendChipLabel(loaded, 'ghost', 'Default backend')).toBe('ghost')
    // Unpinned with no listing is the only case that says "default".
    expect(backendChipLabel(errored, undefined, 'Default backend')).toBe('Default backend')
  })

  it('backendChoiceExists hides the picker and chip on a single-backend install unless the chat is pinned', () => {
    const single = {
      backends: [PAYLOAD.backends[0]], invalid: [], unroutable: [], defaultId: '', isError: false, isLoading: false,
    }
    const several = { ...single, backends: PAYLOAD.backends }
    expect(backendChoiceExists(single, undefined)).toBe(false)
    expect(backendChoiceExists(several, undefined)).toBe(true)
    // A pin is a decision whether or not the listing still offers alternatives.
    expect(backendChoiceExists(single, 'my-acp')).toBe(true)
    expect(backendChoiceExists({ ...single, backends: [], isError: true }, 'my-acp')).toBe(true)
  })

  it("treats '' as a pin to kiro-cli and null as inherit, in every helper", () => {
    // The default here is NOT kiro, so the two answers differ observably.
    const claudeDefault = {
      backends: [
        { id: '', label: 'Kiro CLI', is_global_default: false },
        { id: 'claude', label: 'Claude Code', is_global_default: true },
      ],
      invalid: [], unroutable: [], defaultId: 'claude', isError: false, isLoading: false,
    }
    // Inherit resolves to the default (claude); a '' pin resolves to kiro-cli.
    expect(backendRow(claudeDefault, null)?.id).toBe('claude')
    expect(backendRow(claudeDefault, undefined)?.id).toBe('claude')
    expect(backendRow(claudeDefault, '')?.id).toBe('')
    expect(backendChipLabel(claudeDefault, null, 'Default backend')).toBe('Claude Code')
    expect(backendChipLabel(claudeDefault, '', 'Default backend')).toBe('Kiro CLI')
    // A '' pin is a decision even on a single-backend install.
    const single = { ...claudeDefault, backends: [claudeDefault.backends[1]] }
    expect(backendChoiceExists(single, null)).toBe(false)
    expect(backendChoiceExists(single, '')).toBe(true)
  })
})
