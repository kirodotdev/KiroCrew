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

const KNOWLEDGE_OPEN = '[KNOWLEDGE CONTEXT — injected by user from knowledge library]'
const KNOWLEDGE_CLOSE = '[END KNOWLEDGE CONTEXT]'

/** Format knowledge block for LLM context (expanded at send time). */
export function expandKnowledgeBlock(block: KnowledgeBlock): string {
  return [
    KNOWLEDGE_OPEN + '\n',
    ...block.items.map(item =>
      `## ${item.title}${item.source ? ` (${item.source})` : ''}\n${item.content}\n`
    ),
    KNOWLEDGE_CLOSE + '\n',
  ].join('\n')
}

/** Split a wire text that OPENS with the knowledge envelope into the envelope
 *  (`head`, including the blank lines after its close marker) and what follows
 *  it (`rest`, the sender's own words). `null` when the text does not open with
 *  one. A close marker counts only when it sits on its own line, so a
 *  knowledge item that mentions the marker mid-line does not end the envelope.
 *
 *  The wire text records no boundary, and a knowledge item (or the typed text)
 *  can hold the close marker on a line of its own. With two such lines there
 *  is no way to tell which one ends the envelope, and picking the wrong one
 *  would show document text as the user's words and let an edit overwrite it.
 *  So the split happens only when there is exactly ONE close-marker line;
 *  otherwise this returns `null` and the caller keeps the text whole.
 *  `head + rest` is always the original text. */
export function splitKnowledgeEnvelope(text: string): { head: string; rest: string } | null {
  if (!text.startsWith(KNOWLEDGE_OPEN + '\n')) return null
  const needle = '\n' + KNOWLEDGE_CLOSE
  let close = -1
  for (let at = text.indexOf(needle); at >= 0; at = text.indexOf(needle, at + 1)) {
    const afterClose = at + needle.length
    if (afterClose < text.length && text[afterClose] !== '\n') continue
    if (close >= 0) return null
    close = afterClose
  }
  if (close < 0) return null
  let end = close
  while (text[end] === '\n') end++
  return { head: text.slice(0, end), rest: text.slice(end) }
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

  return { results, query, loading, error, retrySearch, pendingKnowledge, searchKnowledge, inject, clearPending, clearResults }
}
