import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Brain, Check, AlertTriangle } from 'lucide-react'
import { api } from '../../api/client'
import { knowledgeApi } from './api'
import { apiErrorCode } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { reportForError } from '../../utils/errorReport'

import { i18nT } from '../../i18n/t'
interface KnowledgeEmbedStatus {
  enabled: boolean
  available: boolean
  model: string | null
  total_items: number
  embedded_items: number
}

/** The visible text for a failed Generate: localized for the route's codes, else the server's line. */
function generateErrorText(error: unknown): string {
  switch (apiErrorCode(error)) {
    case 'knowledge_embedding_disabled':
      return i18nT('pages.knowledge.embeddingStatus.generate_disabled')
    case 'knowledge_embedding_unavailable':
      return i18nT('pages.knowledge.embeddingStatus.generate_unavailable')
    default:
      return (error instanceof Error && error.message) || i18nT('pages.knowledge.embeddingStatus.generate_failed')
  }
}

export function EmbeddingStatus() {
  const queryClient = useQueryClient()
  const generateMutation = useMutation({
    // Through the shared client: a refusal arrives as an `ApiError` carrying the
    // route's `code`, already journaled with its endpoint for the agent hand-off.
    mutationFn: () => api.knowledgeEmbeddingGenerate(),
    // Settled, not just success: a 400/503 means the embedder's state moved, so the
    // counts query should re-read `available` and hide the button it can no longer serve.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['knowledge-embedding-status'] }),
  })

  // Use the vector memory status (same source of truth as Settings > Vector Memory)
  const { data: vectorStatus } = useQuery({
    queryKey: ['vector-embedding-status'],
    queryFn: () => api.vectorEmbeddingStatus() as Promise<{ provider?: string; server_healthy?: boolean; model_available?: boolean }>,
    refetchInterval: 30_000,
  })

  // Knowledge-specific counts (how many items have embeddings)
  const { data: knowledgeStatus } = useQuery({
    queryKey: ['knowledge-embedding-status'],
    queryFn: () => knowledgeApi<KnowledgeEmbedStatus>('/embedding/status'),
    refetchInterval: 30_000,
  })

  if (!vectorStatus) return null

  const active = vectorStatus.server_healthy || vectorStatus.model_available
  // Generate runs on the KNOWLEDGE embedder, not the vector-memory one: the route
  // refuses with 400 when it is not configured and 503 when it is not reachable.
  // Offer the button only when that same embedder says it can serve the call.
  const canGenerate = !!knowledgeStatus?.enabled && !!knowledgeStatus?.available
  const total = knowledgeStatus?.total_items ?? 0
  const embedded = knowledgeStatus?.embedded_items ?? 0
  const pct = total > 0 ? Math.round((embedded / total) * 100) : 0

  if (total === 0) return null

  // A request that failed, or one that ran and embedded none of the items it
  // found: both are a click that changed nothing, so both say so.
  const result = generateMutation.data
  const generateError = generateMutation.isError
    ? generateErrorText(generateMutation.error)
    : result && result.total > 0 && result.embedded === 0
      ? i18nT('pages.knowledge.embeddingStatus.generate_embedded_none')
      : ''

  return (
    <>
      <div className="flex items-center gap-2 px-3 py-2 mb-3 rounded-md border border-border bg-card text-[12px]">
        <Brain size={14} className={active ? 'text-ok' : 'text-muted'} />
        {active ? (
          embedded > 0 ? (
            <>
              <Check size={12} className="text-ok" />
              <span className="text-text">{i18nT('pages.knowledge.embeddingStatus.smart_search_active')}</span>
              <span className="text-muted">· {i18nT('pages.knowledge.embeddingStatus.embedded_pct', { embedded, total, pct })}</span>
            </>
          ) : canGenerate ? (
            <>
              <AlertTriangle size={12} className="text-warn" />
              <span className="text-muted">{i18nT('pages.knowledge.embeddingStatus.embedding_engine_ready_knowledge_items_need_embe')}</span>
              <button
                onClick={() => generateMutation.mutate()}
                disabled={generateMutation.isPending}
                className="text-accent underline bg-transparent border-none cursor-pointer text-[12px] p-0"
              >
                {generateMutation.isPending ? i18nT('pages.knowledge.embeddingStatus.generating') : i18nT('pages.knowledge.embeddingStatus.generate_now')}
              </button>
            </>
          ) : (
            // Vector memory is up but the knowledge embedder is not: say which way,
            // not the vector-memory "downloading" line, which is not what is happening.
            <>
              <AlertTriangle size={12} className="text-warn" />
              <span className="text-muted">
                {knowledgeStatus?.enabled
                  ? i18nT('pages.knowledge.embeddingStatus.knowledge_embedder_unreachable')
                  : i18nT('pages.knowledge.embeddingStatus.knowledge_embedder_off')}
              </span>
            </>
          )
        ) : (
          <>
            <AlertTriangle size={12} className="text-warn" />
            <span className="text-muted">{i18nT('pages.knowledge.embeddingStatus.smart_search_unavailable_embedding_model_is_down')}</span>
          </>
        )}
      </div>
      {/* Outside the status row on purpose: a refusal re-reads the status, which can
          swap the row to "unavailable", and the reason must stay visible through it.
          The banner holds no draft input, so the agent hand-off loses nothing. */}
      <ErrorNotice
        className="mb-3"
        testId="knowledge-embedding-generate-error"
        message={generateError}
        // The message may be our localized wording, not the journaled server line,
        // so the report rides on the error rather than being looked up by text.
        report={generateMutation.isError ? reportForError(generateMutation.error) : undefined}
        onDismiss={() => generateMutation.reset()}
        askAgent
      />
    </>
  )
}
