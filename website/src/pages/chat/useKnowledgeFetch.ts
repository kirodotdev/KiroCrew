import { useState, useCallback, useRef, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'

export interface KnowledgeResult {
  id: string
  title: string
  source: string | null
  match_type: string
  tokens: number
  summary: string
  content: string
}

export interface KnowledgeBlock {
  items: KnowledgeResult[]
  totalTokens: number
}

interface SearchResponse {
  query: string
  results: KnowledgeResult[]
  total_tokens: number
  max_tokens: number
}

const PREFIXES = ['@knowledge ', '@kb ', '/kb '] as const

/** Detect if message starts with a knowledge fetch prefix. Returns stripped query or null. */
export function extractKnowledgeQuery(text: string): string | null {
  const lower = text.toLowerCase()
  for (const p of PREFIXES) {
    if (lower.startsWith(p)) return text.slice(p.length).trim()
  }
  return null
}

/** Format knowledge block for LLM context (expanded at send time). */
export function expandKnowledgeBlock(block: KnowledgeBlock): string {
  return [
    '[KNOWLEDGE CONTEXT — injected by user from knowledge library]\n',
    ...block.items.map(item =>
      `## ${item.title}${item.source ? ` (${item.source})` : ''}\n${item.content}\n`
    ),
    '[END KNOWLEDGE CONTEXT]\n',
  ].join('\n')
}

/**
 * Hook for knowledge fetch (Option B: frontend-driven prefix intercept).
 *
 * Uses React Query useMutation for the imperative search operation.
 */
export function useKnowledgeFetch(slotKey?: string | null) {
  const [pendingKnowledge, setPendingKnowledge] = useState<KnowledgeBlock | null>(null)
  const [query, setQuery] = useState('')
  const slotMapRef = useRef<Map<string, KnowledgeBlock>>(new Map())

  // Save/restore pendingKnowledge per slot
  const prevSlotRef = useRef<string | null>(null)
  useEffect(() => {
    const prev = prevSlotRef.current
    const cur = slotKey ?? null
    if (prev === cur) return
    // Save outgoing slot's pending
    if (prev && pendingKnowledge) slotMapRef.current.set(prev, pendingKnowledge)
    else if (prev) slotMapRef.current.delete(prev)
    // Restore incoming slot's pending
    prevSlotRef.current = cur
    setPendingKnowledge(cur ? slotMapRef.current.get(cur) ?? null : null)
  }, [slotKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const [searchQuery, setSearchQuery] = useState<string | null>(null)

  const { data: searchData, isFetching: loading, error: searchError, refetch } = useQuery<SearchResponse>({
    queryKey: ['knowledge-search', searchQuery],
    queryFn: () => api.knowledgeSearch(searchQuery!) as Promise<SearchResponse>,
    enabled: !!searchQuery,
  })
  // A refused search used to fall through to the picker's "no knowledge found"
  // empty state, which is a claim the failed request cannot back. Exposed as the
  // backend's own message so the picker can render it through ErrorNotice.
  const error = searchQuery && searchError
    ? (searchError instanceof Error && searchError.message ? searchError.message : String(searchError))
    : null
  // Same key, so `searchKnowledge(query)` again would just re-read the cached
  // rejection; this is the real re-request the picker's Retry needs.
  const retrySearch = useCallback(() => { void refetch() }, [refetch])

  const searchKnowledge = useCallback((q: string) => {
    setQuery(q)
    setSearchQuery(q)
  }, [])

  const results = searchData?.results ?? []

  const inject = useCallback((selectedItems: KnowledgeResult[]) => {
    if (selectedItems.length > 0) {
      setPendingKnowledge({
        items: selectedItems,
        totalTokens: selectedItems.reduce((sum, r) => sum + r.tokens, 0),
      })
    }
    setSearchQuery(null)
    setQuery('')
  }, [])

  const clearPending = useCallback(() => setPendingKnowledge(null), [])
  const clearResults = useCallback(() => { setSearchQuery(null); setQuery('') }, [])

  /**
   * COPY a retiring slot's pending selection onto the slot replacing it.
   *
   * A mode switch replaces the slot and deletes the old one, and this selection is not a
   * draft bucket, so the migration that moves the drafts cannot see it. The map is a BANK,
   * written only when the slot-change effect retires a slot, and read back without being
   * cleared — so an entry outlives a later in-place change and goes stale. Live state
   * therefore WINS whenever it still owns `from`, including when it holds null because the
   * user cleared the selection; the bank answers only once React has switched away.
   *
   * The source is LEFT IN PLACE, matching `copySlotEntry`: the delete can be rejected, and
   * a slot that survives its own failed deletion must keep the selection it still owns.
   * `dropCarriedKnowledge` removes it once the deletion has actually succeeded.
   */
  const carryPendingKnowledge = useCallback((from: string, to: string): boolean => {
    if (!from || !to || from === to) return false
    const liveOwnsFrom = prevSlotRef.current === from
    const carried = liveOwnsFrom ? pendingKnowledge : slotMapRef.current.get(from) ?? null
    if (!carried) return false
    slotMapRef.current.set(to, carried)
    if ((slotKey ?? null) === to) setPendingKnowledge(carried)
    return true
  }, [pendingKnowledge, slotKey])

  /** Drop a retired slot's selection, once its deletion has actually succeeded. */
  const dropCarriedKnowledge = useCallback((slot: string): void => {
    if (!slot) return
    slotMapRef.current.delete(slot)
  }, [])

  return { results, query, loading, error, retrySearch, pendingKnowledge, searchKnowledge, inject, clearPending, clearResults, carryPendingKnowledge, dropCarriedKnowledge }
}
