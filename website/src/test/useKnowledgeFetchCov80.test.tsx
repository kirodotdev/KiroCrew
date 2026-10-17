/**
 * useKnowledgeFetch — the composer's knowledge-fetch prefix intercept.
 *
 * The pure helpers own the prefix grammar and the LLM-facing block format; the
 * hook owns the per-slot pending-block ledger (save on the way out, restore on
 * the way in) and the search/inject/clear transitions.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const mocks = vi.hoisted(() => ({ knowledgeSearch: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import {
  extractKnowledgeQuery,
  expandKnowledgeBlock,
  useKnowledgeFetch,
  type KnowledgeResult,
} from '../pages/chat/useKnowledgeFetch'

const item = (over: Partial<KnowledgeResult> = {}): KnowledgeResult => ({
  id: 'zzq-1',
  title: 'zzq-title',
  source: null,
  match_type: 'zzq-match',
  tokens: 10,
  summary: 'zzq-summary',
  content: 'zzq-content',
  ...over,
})

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

afterEach(() => vi.clearAllMocks())

describe('extractKnowledgeQuery', () => {
  it('strips each supported prefix and trims the remainder', () => {
    expect(extractKnowledgeQuery('@knowledge  zzq topic ')).toBe('zzq topic')
    expect(extractKnowledgeQuery('@kb zzq topic')).toBe('zzq topic')
    expect(extractKnowledgeQuery('/kb zzq topic')).toBe('zzq topic')
  })

  it('matches the prefix case-insensitively but keeps the query casing', () => {
    expect(extractKnowledgeQuery('@KB ZzQ Topic')).toBe('ZzQ Topic')
  })

  it('returns null for a message with no prefix', () => {
    expect(extractKnowledgeQuery('zzq @kb not at the start')).toBeNull()
  })
})

describe('expandKnowledgeBlock', () => {
  it('annotates the source when one is present and omits it otherwise', () => {
    const text = expandKnowledgeBlock({
      items: [
        item({ title: 'zzq-a', source: 'zzq-src', content: 'body-a' }),
        item({ id: 'zzq-2', title: 'zzq-b', content: 'body-b' }),
      ],
      totalTokens: 20,
    })
    expect(text).toContain('## zzq-a (zzq-src)')
    expect(text).toContain('## zzq-b\nbody-b')
    expect(text.startsWith('[KNOWLEDGE CONTEXT')).toBe(true)
    expect(text).toContain('[END KNOWLEDGE CONTEXT]')
  })
})

describe('useKnowledgeFetch', () => {
  it('searches through the api and exposes the returned results', async () => {
    mocks.knowledgeSearch.mockResolvedValue({
      query: 'zzq', results: [item()], total_tokens: 10, max_tokens: 100,
    })
    const { result } = renderHook(() => useKnowledgeFetch('slot-a'), { wrapper })

    act(() => result.current.searchKnowledge('zzq'))
    expect(result.current.query).toBe('zzq')
    await waitFor(() => expect(result.current.results).toHaveLength(1))
    expect(mocks.knowledgeSearch).toHaveBeenCalledWith('zzq')
  })

  it('does not search until a query is set', () => {
    renderHook(() => useKnowledgeFetch('slot-a'), { wrapper })
    expect(mocks.knowledgeSearch).not.toHaveBeenCalled()
  })

  it('inject sums the selected tokens, clears the query, and clearPending drops the block', () => {
    const { result } = renderHook(() => useKnowledgeFetch('slot-a'), { wrapper })

    act(() => result.current.searchKnowledge('zzq'))
    act(() => result.current.inject([item({ tokens: 3 }), item({ id: 'zzq-2', tokens: 4 })]))
    expect(result.current.pendingKnowledge).toEqual({
      items: [expect.objectContaining({ tokens: 3 }), expect.objectContaining({ tokens: 4 })],
      totalTokens: 7,
    })
    expect(result.current.query).toBe('')

    act(() => result.current.clearPending())
    expect(result.current.pendingKnowledge).toBeNull()
  })

  it('injecting an empty selection leaves no pending block', () => {
    const { result } = renderHook(() => useKnowledgeFetch('slot-a'), { wrapper })
    act(() => result.current.inject([]))
    expect(result.current.pendingKnowledge).toBeNull()
  })

  it('clearResults drops the in-flight query', async () => {
    mocks.knowledgeSearch.mockResolvedValue({
      query: 'zzq', results: [item()], total_tokens: 10, max_tokens: 100,
    })
    const { result } = renderHook(() => useKnowledgeFetch('slot-a'), { wrapper })
    act(() => result.current.searchKnowledge('zzq'))
    await waitFor(() => expect(result.current.results).toHaveLength(1))

    act(() => result.current.clearResults())
    expect(result.current.query).toBe('')
    await waitFor(() => expect(result.current.results).toHaveLength(0))
  })

  it('stashes the pending block per slot and restores it when the slot comes back', () => {
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string | null }) => useKnowledgeFetch(slot),
      { wrapper, initialProps: { slot: 'slot-a' as string | null } },
    )

    act(() => result.current.inject([item({ tokens: 5 })]))
    expect(result.current.pendingKnowledge?.totalTokens).toBe(5)

    // Away to another slot: that slot has nothing stashed.
    rerender({ slot: 'slot-b' })
    expect(result.current.pendingKnowledge).toBeNull()

    // Back again: slot-a's block is restored.
    rerender({ slot: 'slot-a' })
    expect(result.current.pendingKnowledge?.totalTokens).toBe(5)
  })

  it('forgets a slot whose pending block was cleared before leaving it', () => {
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string | null }) => useKnowledgeFetch(slot),
      { wrapper, initialProps: { slot: 'slot-a' as string | null } },
    )

    act(() => result.current.inject([item({ tokens: 5 })]))
    act(() => result.current.clearPending())
    rerender({ slot: 'slot-b' })
    rerender({ slot: 'slot-a' })
    expect(result.current.pendingKnowledge).toBeNull()
  })

  it('clears the pending block when the slot goes away entirely', () => {
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string | null }) => useKnowledgeFetch(slot),
      { wrapper, initialProps: { slot: 'slot-a' as string | null } },
    )
    act(() => result.current.inject([item({ tokens: 5 })]))
    rerender({ slot: null })
    expect(result.current.pendingKnowledge).toBeNull()
  })
})
