/**
 * "1,123 of 1,284 embedded" under the memory page header, shown while vector
 * memory is on. The provider comes from the embedding-status read the vector
 * card already makes (same query key, so one request); the counts from the
 * store's stats.
 */
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { fmtNumber } from '../../../i18n/format'
import ErrorNotice from '../../../components/ErrorNotice'
import { errorText } from '../memoryRecordParts'

type Stats = { semantic_active?: number; episodic_active?: number; embedded_count?: number }
type Status = { provider?: string } | null

export function MemoryEmbeddedLine({ store }: { store: string }) {
  const { t } = useTranslation()
  const status = useQuery({ queryKey: ['member-memory', 'default', 'embedding-status'], queryFn: async () => (await api.vectorEmbeddingStatus()) as Status, retry: false })
  const stats = useQuery({ queryKey: ['member-memory', store || 'default', 'vector-stats'], queryFn: () => (store ? api.vectorStats(store) : api.vectorStats()) as Promise<Stats>, retry: false })
  const provider = status.data?.provider
  const data = stats.data
  const failed = status.error || stats.error
  // No hand-off: the page below can hold unsaved drafts and selections.
  if (failed) return <ErrorNotice impact="minor" title={t('memoryPage.embedded_read_failed')} message={errorText(failed)} />
  if (!provider || provider === 'none' || !data || typeof data.embedded_count !== 'number') return null
  const total = (data.semantic_active ?? 0) + (data.episodic_active ?? 0)
  return <p className="flex items-center gap-1.5 text-[12px] text-muted" data-testid="memory-embedded-line">
    <span className="h-2 w-2 rounded-full bg-ok" aria-hidden="true" />
    <span className="tabular-nums">{t('memoryPage.embedded_count', { embedded: fmtNumber(data.embedded_count), total: fmtNumber(total) })}</span>
  </p>
}
