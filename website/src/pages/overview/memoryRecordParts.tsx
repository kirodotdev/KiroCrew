/**
 * The record pieces both memory lists share: the bot editor dialog's
 * `MemoryRecordsEditor` and the memory page's `MemoryList`. A record's body,
 * its source and provenance, its revision history and the recall evidence card.
 */
import { useInfiniteQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Lightbulb, ShieldCheck, History } from 'lucide-react'
import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { sliceCodePoints } from '../../utils/codePoints'
import { Badge, Btn, Card, PanelSectionHeader } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtDateTimeNumeric, fmtList } from '../../i18n/format'
import type { MemoryEditOperation, MemoryRecord, MemoryRecordRevision } from '../../types/memoryEditing'
import { MemoryStoreAvatar, memoryQueryRetry, useMemoryStores } from './MemoryStoreCard'
import { queryReadFailed } from '../../utils/queryReadFailed'

export const MEMORY_RECORD_ICONS = { fact: Lightbulb, directive: ShieldCheck, episode: History }
export const MEMORY_RECORD_LABELS = { fact: 'memoryV2.fact', directive: 'memoryV2.directive', episode: 'memoryV2.episode' } as const
export const recordIdentity = (row: { kind: string; id: string }) => `${row.kind}:${row.id}`
// A button names the value it brings back; a long value is cut so the label stays one line.
export const shortBody = (text: string) => { const flat = text.replace(/\s+/g, ' ').trim(); return flat.length > 40 ? `${sliceCodePoints(flat, 0, 39)}…` : flat }
export const errorText = (error: unknown) => apiErrorCode(error) === 'stale_memory_box' ? i18nT('memoryPage.graph_changed') : error instanceof Error ? error.message : error ? String(error) : ''
export const decodedRecordValue = (row: MemoryRecord): unknown => {
  if (typeof row.value_json !== 'string') return row.value_json
  try { return JSON.parse(row.value_json) } catch { return row.value_json }
}
export const memoryRecordBody = (row: MemoryRecord) => {
  if (row.kind === 'episode') return row.text
  const value = decodedRecordValue(row)
  if (row.kind === 'directive' && value && typeof value === 'object' && 'rule' in value) return String(value.rule)
  return value && typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? row.text ?? '')
}
export const recordReplacement = (row: MemoryRecord, draft: string): MemoryEditOperation => {
  if (row.kind === 'episode') return { type: 'set', text: draft }
  const value = decodedRecordValue(row)
  if (row.kind === 'directive' && value && typeof value === 'object' && 'rule' in value) return { type: 'set', value: { ...value, rule: draft } }
  return { type: 'set', value: typeof value !== 'string' && value !== undefined ? JSON.parse(draft) : draft }
}

export type RecordOrigin = Pick<MemoryRecord, 'source' | 'updated_at' | 'metadata'> & { derived_from?: string | Record<string, unknown> }
type RecallRecord = RecordOrigin & { id?: string; key?: string; snippet?: string; text?: string; retrieval?: { matched_terms?: string[] } }
export type RecallResult = { semantic_context?: string; episodic_context?: string; lessons_context?: string; retrieval?: { facts?: RecallRecord[]; episodes?: RecallRecord[]; operating_point?: { qualification?: 'reference_identity' | 'custom_or_registered' | 'unknown' } } }

export const sourceLabelKey = (source: string) => {
  switch (source) {
    case 'user_seed': return 'memoryV2.source_copy'
    case 'user_explicit': return 'memoryV2.source_user'
    case 'consolidation': return 'memoryV2.source_conversation'
    case 'migration':
    case 'import': return 'memoryV2.source_migration'
    default: return undefined
  }
}

export function RecordSource({ row, compact = false }: { row: RecordOrigin; compact?: boolean }) {
  const { t } = useTranslation()
  const source = row.source || ''
  const labelKey = sourceLabelKey(source)
  const label = labelKey ? t(labelKey) : source || t('memoryV2.source_unknown')
  // An unmapped source is a raw tag with no spaces, and a nowrap badge would
  // set the card's minimum width: the list cuts it to one line, details wrap it.
  const fit = compact ? 'block max-w-full truncate' : 'max-w-full whitespace-normal [overflow-wrap:anywhere]'
  return <div className="flex min-w-0 flex-wrap items-center gap-2 text-[12px] text-muted"><Badge variant="muted" className={fit} title={compact ? label : undefined}>{label}</Badge>{row.updated_at && <time dateTime={row.updated_at} data-i18n-opaque>{fmtDateTimeNumeric(row.updated_at)}</time>}</div>
}

/** Where a record came from. `source={false}` leaves out the source chip and the
 *  reference, for a place that already shows them (the record detail). */
export function RecordProvenance({ row, source: showSource = true }: { row: RecordOrigin; source?: boolean }) {
  const { t } = useTranslation()
  const stores = useMemoryStores()
  let origin: Record<string, unknown> | undefined
  try { const value = typeof row.derived_from === 'string' ? JSON.parse(row.derived_from) : row.derived_from; if (value && typeof value === 'object' && !Array.isArray(value)) origin = value } catch { /* Older rows carry a plain source reference. */ }
  const source = origin?.store || origin?.source_store
  const summary = stores.data?.stores.find(item => item.name === source)
  const label = source === 'default' ? t('pages.kiroCrewAgentsPage.global_memory_v1') : summary?.owner_member || source
  const reference = origin?.item_id || origin?.source_key || row.metadata?.source_ref || (!origin && row.derived_from)
  // A copied item's key is an identity, even when it resembles a source tag.
  // Only known bare fallback tags share the readable writer labels above.
  const referenceLabelKey = !origin?.item_id && !origin?.source_key && typeof reference === 'string' ? sourceLabelKey(reference) : undefined
  if (!showSource && typeof label !== 'string') return null
  return <div className="space-y-2">{showSource && <RecordSource row={row} />}{typeof label === 'string' && <div className="flex min-w-0 items-center gap-2 text-[13px]"><MemoryStoreAvatar summary={summary} size={22} /><span className="break-words">{t('memoryEditing.copy_origin', { source: label })}</span></div>}{showSource && typeof reference === 'string' && <p className="break-words text-[12px] text-muted [overflow-wrap:anywhere]">{t('memoryEditing.original_reference')}: {referenceLabelKey ? t(referenceLabelKey) : reference}</p>}</div>
}

export function RecallEvidence({ data }: { data: RecallResult }) {
  const { t } = useTranslation()
  const groups = [
    { kind: 'fact' as const, rows: data.retrieval?.facts || [], context: data.semantic_context, label: t('memoryV2.facts_label') },
    { kind: 'episode' as const, rows: data.retrieval?.episodes || [], context: data.episodic_context, label: t('memoryV2.experiences_label') },
  ]
  const hasEvidence = groups.some(group => group.rows.length || group.context) || !!data.lessons_context
  const qualification = data.retrieval?.operating_point?.qualification
  const unvalidatedModel = qualification === 'custom_or_registered' || qualification === 'unknown'
  return <Card className="mb-0" data-testid="memory-recall-evidence">
    <PanelSectionHeader label={t('memoryV2.recall_evidence')} />
    <p className="mt-2 text-[13px] text-muted">{t('memoryV2.recall_explanation')}</p>
    {unvalidatedModel && <p role="status" className="mt-2 text-[13px] text-muted">{t('memoryV2.recall_model_notice')}</p>}
    <div className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2">
      {groups.flatMap(group => group.rows.map((item, index) => {
        const Icon = MEMORY_RECORD_ICONS[group.kind]
        return <div key={`${group.kind}:${item.id || item.key || index}`} className="min-w-0 space-y-2 rounded-lg border border-border bg-bg-elevated p-3">
          <div className="flex items-center gap-2 text-[12px] text-muted"><Icon className="lucide-inline" />{group.label}</div>
          <p className="whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{item.snippet || item.text}</p>
        </div>
      }))}
    </div>
    {data.lessons_context && <p className="mt-3 text-[13px] text-muted">{t('memoryV2.recall_rules_details')}</p>}
    {!hasEvidence && <p className="mt-3 text-[13px] text-muted">{t('memoryV2.empty')}</p>}
    <details className="mt-3 text-[13px]">
      <summary className="min-h-11 cursor-pointer py-3 text-muted">{t('memoryV2.provenance')}</summary>
      <ul className="space-y-3">{groups.flatMap(group => group.rows.map((item, index) => <li key={`${group.kind}:${item.id || item.key || index}`} className="space-y-2 border-t border-border pt-3">
        {item.retrieval?.matched_terms?.length ? <p>{t('memoryV2.matched_terms', { terms: fmtList(item.retrieval.matched_terms, { style: 'narrow' }) })}</p> : null}
        <RecordProvenance row={item} />
      </li>))}</ul>
      {/* Exact model context, including rules, remains inspectable without becoming the main reading surface. */}
      {[...groups, { context: data.lessons_context, label: t('memoryV2.rules_label') }].filter(group => group.context).map(group => <div key={group.label} className="mt-3 space-y-2 border-t border-border pt-3">
        <PanelSectionHeader label={group.label} />
        <pre className="whitespace-pre-wrap break-words text-[12px] text-muted [overflow-wrap:anywhere]">{group.context}</pre>
      </div>)}
    </details>
  </Card>
}

export function RecordHistory({ store, row, onProposal, onKeepCurrent, onRestore }: { store: string; row: MemoryRecord; onProposal: (proposal: MemoryRecord, operation: 'set' | 'forget') => void; onKeepCurrent: () => void; onRestore: (revisionId: number) => void }) {
  const { t } = useTranslation()
  const history = useInfiniteQuery({
    queryKey: ['memory-record-history', store, row.kind, row.id], initialPageParam: 0,
    queryFn: ({ pageParam }) => api.memoryRecordHistory(store, row, 25, pageParam),
    getNextPageParam: (last, pages) => last.has_more ? pages.reduce((count, page) => count + page.entries.length, 0) : undefined,
    retry: memoryQueryRetry,
  })
  // Offset pages can overlap when another revision arrives during review.
  const entries = [...new Map(history.data?.pages.flatMap(page => page.entries).map(item => [item.id, item])).values()]
  const currentRevision = history.data?.pages.at(-1)?.current_revision
  const proposed = (json: string | null) => {
    try { const value = JSON.parse(json || ''); return value && typeof value === 'object' && !Array.isArray(value) ? { ...row, ...value } as MemoryRecord : null } catch { return null }
  }
  const origin = (item: MemoryRecordRevision): RecordOrigin => {
    // A proposal's source belongs to that revision, not to the current record.
    try {
      const metadata: unknown = JSON.parse(item.metadata_json)
      return { source: item.source, metadata: metadata && typeof metadata === 'object' && !Array.isArray(metadata) ? metadata as MemoryRecord['metadata'] : undefined }
    } catch { return { source: item.source } }
  }
  return <div className="space-y-3"><PanelSectionHeader label={t('memoryEditing.history')} />
    {/* No hand-off: keep the record detail and pending proposal in place. */}<ErrorNotice message={errorText(history.error)} />
    {history.isPending && <p role="status" className="text-[13px] text-muted">{t('pages.overview.memoryTab.loading')}</p>}
    {queryReadFailed(history) && <Btn className="min-h-11 aria-disabled:opacity-30 aria-disabled:cursor-default" aria-disabled={history.isFetching || undefined} onClick={() => { if (!history.isFetching) void (history.isFetchNextPageError ? history.fetchNextPage() : history.refetch()) }}>{t('memoryV2.retry_read')}</Btn>}
    {entries.map(item => { const proposal = proposed(item.after_json); const before = proposed(item.before_json); const removed = item.operation === 'forget'; const pending = item.status === 'conflict' && item.base_revision === currentRevision; const current = item.status === 'accepted' && item.revision === currentRevision; const restorable = item.status === 'accepted' && !!proposal && !removed && !current && memoryRecordBody(proposal) !== memoryRecordBody(row); return <details key={item.id} open={pending} className="rounded-lg border border-border p-3"><summary className="cursor-pointer text-[13px] text-text"><span>{t(item.status === 'conflict' ? 'memoryEditing.proposal' : item.operation === 'restore' ? 'memoryEditing.restored' : item.operation === 'create' ? 'memoryPage.field_created' : 'memoryEditing.edited')}</span><span className="text-muted"> · {fmtDateTimeNumeric(item.created_at)}</span>{current && <span className="ml-2 font-medium text-accent">{t('memoryEditing.current')}</span>}</summary><div className="mt-3 space-y-3"><RecordProvenance row={origin(item)} />{before && <div><PanelSectionHeader label={t('memoryEditing.before')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{memoryRecordBody(before)}</p></div>}{(proposal || removed) && <div><PanelSectionHeader label={t('memoryEditing.after')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{removed ? t('memoryEditing.removed') : memoryRecordBody(proposal!)}</p></div>}{pending && <div className="flex flex-wrap gap-2">{(proposal || removed) && <Btn className="min-h-11" onClick={() => onProposal(proposal || row, removed ? 'forget' : 'set')}>{t('memoryEditing.use_proposal')}</Btn>}<Btn className="min-h-11" onClick={onKeepCurrent}>{t('memoryEditing.keep_current')}</Btn></div>}{restorable && <Btn className="min-h-11" onClick={() => onRestore(item.id)}><History className="lucide-inline" />{t('memoryEditing.restore_version', { value: shortBody(memoryRecordBody(proposal!)) })}</Btn>}</div></details> })}
    {history.data && !entries.length && <p className="text-[13px] text-muted">{t('memoryEditing.no_history')}</p>}
    {history.hasNextPage && !history.error && <Btn className="min-h-11 aria-disabled:opacity-30 aria-disabled:cursor-default" aria-disabled={history.isFetching || undefined} onClick={() => { if (!history.isFetching) void history.fetchNextPage() }}>{history.isFetchingNextPage ? t('pages.overview.memoryTab.loading') : t('memoryV2.show_more')}</Btn>}
  </div>
}

/** A semantic value as text: a JSON string parsed, an object pretty-printed, a scalar as is. */
export function semanticValueText(e: { value_json?: unknown }): string {
  let val: unknown = e?.value_json
  if (typeof val === 'string') { try { val = JSON.parse(val) } catch { /* a raw string stays as it is */ } }
  return typeof val === 'object' && val !== null ? JSON.stringify(val, null, 2) : String(val ?? '')
}
