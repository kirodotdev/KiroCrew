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

  const { data: searchData, isFetching: loading } = useQuery<SearchResponse>({
    queryKey: ['knowledge-search', searchQuery],
    queryFn: () => api.knowledgeSearch(searchQuery!) as Promise<SearchResponse>,
    enabled: !!searchQuery,
  })

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

  return { results, query, loading, pendingKnowledge, searchKnowledge, inject, clearPending, clearResults }
}
