/**
 * Whether the global store runs as vector memory, read from the same two
 * queries the Embedding Model card and its setup status use (shared cache keys),
 * so the Memory page needs no card to report it.
 *
 * `active` is true once the store holds vector rows or an embedding provider is
 * set, and false while setup is still running; `settled` is true once both reads
 * have answered, so "not active" can be told from "not known yet". A failed read
 * is "not known": the page keeps the list rather than hide it on an error.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'

export type VectorStats = { migrated?: boolean; semantic_active?: number; episodic_active?: number; embedded_count?: number; faiss_index_size?: number; has_legacy_memory?: boolean }
export const VECTOR_STATS_KEY = ['member-memory', 'default', 'vector-stats'] as const
export const EMBEDDING_STATUS_KEY = ['member-memory', 'default', 'embedding-status'] as const

/** A setup step that is neither finished nor failed. */
export const setupRunning = (step?: string) => !!step && step !== 'idle' && step !== 'done' && step !== 'error'

export function useVectorStats() {
  return useQuery({ queryKey: VECTOR_STATS_KEY, queryFn: () => api.vectorStats() as Promise<VectorStats>, staleTime: 0, retry: false })
}

export function useEmbeddingStatus(refetchInterval?: number | false) {
  return useQuery({
    queryKey: EMBEDDING_STATUS_KEY,
    queryFn: async () => {
      const status = await api.vectorEmbeddingStatus()
      if (!status) throw new Error(i18nT('pages.overview.vectorMemoryCard.unknown_error'))
      return status
    },
    staleTime: 0, retry: false, refetchInterval,
  })
}

export function useVectorMemoryState(): { active: boolean; migrated: boolean; settled: boolean } {
  const stats = useVectorStats()
  const embedding = useEmbeddingStatus()
  const data = stats.data
  const status = embedding.data as { provider?: string; setup_step?: string } | undefined
  const hasRows = !!data && ((data.semantic_active ?? 0) > 0 || (data.episodic_active ?? 0) > 0)
  const provider = !!status?.provider && status.provider !== 'none'
  return { active: hasRows || (!setupRunning(status?.setup_step) && provider), migrated: !!data?.migrated, settled: stats.isSuccess && embedding.isSuccess }
}
