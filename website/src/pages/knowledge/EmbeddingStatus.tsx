import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Brain, Check, AlertTriangle } from 'lucide-react'
import { api } from '../../api/client'
import { knowledgeApi } from './api'

import { i18nT } from '../../i18n/t'
interface KnowledgeEmbedStatus {
  enabled: boolean
  available: boolean
  model: string | null
  total_items: number
  embedded_items: number
}

export function EmbeddingStatus() {
  const queryClient = useQueryClient()
  const generateMutation = useMutation({
    mutationFn: () => knowledgeApi('/embedding/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['knowledge-embedding-status'] }),
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
  const total = knowledgeStatus?.total_items ?? 0
  const embedded = knowledgeStatus?.embedded_items ?? 0
  const pct = total > 0 ? Math.round((embedded / total) * 100) : 0

  // Don't show anything if there are no knowledge items
  if (total === 0) return null

  return (
    <div className="flex items-center gap-2 px-3 py-2 mb-3 rounded-md border border-border bg-card text-[12px]">
      <Brain size={14} className={active ? 'text-ok' : 'text-muted'} />
      {active ? (
        embedded > 0 ? (
          <>
            <Check size={12} className="text-ok" />
            <span className="text-text">{i18nT('pages.knowledge.embeddingStatus.smart_search_active')}</span>
            <span className="text-muted">· {i18nT('pages.knowledge.embeddingStatus.embedded_pct', { embedded, total, pct })}</span>
          </>
        ) : (
          <>
            <AlertTriangle size={12} className="text-warn" />
            <span className="text-muted">{i18nT('pages.knowledge.embeddingStatus.embedding_engine_ready_knowledge_items_need_embe')}</span>
            <button
              onClick={() => generateMutation.mutate()}
              disabled={generateMutation.isPending}
              className="text-accent underline bg-transparent border-none cursor-pointer text-[12px] p-0"
            >
              {generateMutation.isPending ? 'Generating…' : 'Generate now'}
            </button>
          </>
        )
      ) : (
        <>
          <AlertTriangle size={12} className="text-warn" />
          <span className="text-muted">{i18nT('pages.knowledge.embeddingStatus.smart_search_unavailable_embedding_model_is_down')}</span>
        </>
      )}
    </div>
  )
}
