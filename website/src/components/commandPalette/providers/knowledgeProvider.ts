import { createElement } from 'react'
import { useMemo } from 'react'
import { Brain } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import type { NavigateFunction } from 'react-router-dom'

import { api } from '../../../api/client'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import type { Result, ResourceProvider } from '../types'

/**
 * Knowledge provider for the Search Everywhere command palette.
 *
 * Backs the **Knowledge** tab. Wraps `api.knowledgeSearch(q)` (the existing
 * `/api/knowledge/search-for-context` full-text endpoint, the same one
 * `useKnowledgeFetch` / `KnowledgePicker` use) and maps each hit to a
 * {@link Result}:
 *  - `onActivate` (Enter) — open the entry. There is no per-entry deep link
 *    today, so this navigates to the Knowledge page (`/knowledge`).
 *  - `onCmdActivate` (⌘Enter) — attach the entry as context to the current
 *    chat. The attach implementation lives in the chat surface and is injected
 *    by the palette host; it is a **placeholder** here until the §2 Enter
 *    matrix step wires the real "insert into active chat / new session"
 *    behaviour. When the host does not supply it, ⌘Enter is unbound.
 *
 * The backend already does the relevance filtering, so the {@link fuzzyMatch}
 * pass here is only for **client-side highlight indices** on the returned
 * titles (per the §2 design + `frontend-security` lint rule — highlights render
 * as React `<mark>` nodes keyed off `indices`, never as HTML strings). Title
 * matches additionally bias the client-side ordering; non-title (body-only)
 * matches are kept with a neutral score so backend results are never dropped.
 */

const PROVIDER_ID = 'knowledge'
const PROVIDER_LABEL = 'Knowledge'

/** Where Enter ("open entry") navigates — no per-entry deep link exists yet. */
const KNOWLEDGE_ROUTE = '/knowledge'

/** Cache server responses briefly so retyping the same query is free. */
const KNOWLEDGE_STALE_MS = 30_000

/**
 * One knowledge hit as returned by `/api/knowledge/search-for-context`.
 * Mirrors `KnowledgeResult` in `pages/chat/useKnowledgeFetch.ts`; pinned here
 * so the provider does not depend on a chat-surface module.
 */
export interface KnowledgeSearchItem {
  id: string
  title: string
  source?: string | null
  match_type?: string
  tokens?: number
  summary?: string
  content?: string
}

/** Shape of the `/api/knowledge/search-for-context` response envelope. */
export interface KnowledgeSearchResponse {
  query?: string
  results?: KnowledgeSearchItem[]
  total_tokens?: number
  max_tokens?: number
}

/** A knowledge entry reference passed to the open / attach callbacks. */
export interface KnowledgeRef {
  id: string
  title: string
}

/**
 * Injectable dependencies for {@link createKnowledgeProvider}. Keeping the
 * concrete provider free of React hooks makes it unit-testable with a plain
 * mock fetch + callbacks; the {@link useKnowledgeProvider} hook wires the real
 * React-Query + router implementations.
 */
export interface KnowledgeProviderDeps {
  /** Fetch search hits for a query (React-Query-cached in the hook). */
  fetchKnowledge: (query: string) => Promise<KnowledgeSearchResponse>
  /** Open the entry (Enter). */
  openEntry: (ref: KnowledgeRef) => void
  /**
   * Attach the entry as context to the current chat (⌘Enter). Optional /
   * placeholder until the §2 Enter matrix step; when omitted, ⌘Enter is
   * unbound for knowledge rows.
   */
  attachAsContext?: (ref: KnowledgeRef) => void
}

function knowledgeIcon() {
  return createElement(Brain, { className: 'lucide-inline' })
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Build the Knowledge {@link ResourceProvider} from injected dependencies.
 * Pure (no hooks) so it can be exercised directly in tests.
 */
export function createKnowledgeProvider(deps: KnowledgeProviderDeps): ResourceProvider {
  const { fetchKnowledge, openEntry, attachAsContext } = deps

  return {
    id: PROVIDER_ID,
    label: PROVIDER_LABEL,
    icon: knowledgeIcon(),
    async search(query: string): Promise<Result[]> {
      const q = query.trim()
      const data = await fetchKnowledge(q)
      const items = data?.results ?? []

      const results: Result[] = items.map((k) => {
        const title = k.title || k.id
        // Highlight + client-side rank bias; never used to drop backend hits.
        const match = fuzzyMatch(q, title)
        const ref: KnowledgeRef = { id: k.id, title }
        return {
          id: `${PROVIDER_ID}:${k.id}`,
          providerId: PROVIDER_ID,
          title,
          subtitle: k.summary || k.source || undefined,
          icon: knowledgeIcon(),
          score: match ? match.score : 0,
          indices: match ? match.indices : [],
          // Declarative Enter contract (§2 / task 26). The central
          // `dispatchEnter` in CommandPalette routes on this: primary Enter
          // opens / navigates to the entry; ⌘Enter attaches it as context to
          // the active chat. The `entryId` is the knowledge entry id from this
          // result payload. The `on*Activate` closures below stay as the
          // payload-bound execution path (the `open-knowledge` branch invokes
          // them) and as the legacy/mouse fallback.
          enter: { kind: 'open-knowledge', entryId: k.id, title },
          onActivate: () => openEntry(ref),
          onCmdActivate: attachAsContext ? () => attachAsContext(ref) : undefined,
        }
      })

      // Title matches first, then deterministic name order. Skip the re-rank on
      // an empty query so the backend's ordering is preserved (a review finding).
      if (q.length > 0) {
        results.sort(compareResults)
      }
      return results
    },
  }
}

/**
 * React hook that returns a live Knowledge provider wired to React-Query and
 * the app router.
 *
 * Per the `use-react-query` lint rule the server fetch goes through
 * React-Query with the key `['palette', 'knowledge', q]` (mirrors the Sessions
 * provider's `['palette','sessions',q]` keying). `queryClient.fetchQuery` is
 * used rather than `useQuery` because a {@link ResourceProvider}'s `search` is
 * an imperative call from the palette, not a render-time subscription — the
 * cache (key + `staleTime`) is still shared with any
 * `useQuery(['palette','knowledge',q])`.
 *
 * @param opts.attachAsContext - Optional attach-as-context callback supplied by
 *   the palette host (placeholder until the §2 Enter matrix step); when
 *   omitted, ⌘Enter is unbound for knowledge rows.
 */
export function useKnowledgeProvider(opts?: {
  attachAsContext?: (ref: KnowledgeRef) => void
}): ResourceProvider {
  const queryClient = useQueryClient()
  const navigate: NavigateFunction = useNavigate()
  const attachAsContext = opts?.attachAsContext

  return useMemo(
    () =>
      createKnowledgeProvider({
        fetchKnowledge: (q) =>
          queryClient.fetchQuery<KnowledgeSearchResponse>({
            queryKey: ['palette', 'knowledge', q],
            queryFn: () => api.knowledgeSearch(q) as Promise<KnowledgeSearchResponse>,
            staleTime: KNOWLEDGE_STALE_MS,
          }),
        openEntry: () => {
          // No per-entry deep link exists yet; open the Knowledge page.
          navigate(KNOWLEDGE_ROUTE)
        },
        attachAsContext,
      }),
    [queryClient, navigate, attachAsContext],
  )
}
