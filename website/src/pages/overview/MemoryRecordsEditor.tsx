import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { motion, useReducedMotion } from 'framer-motion'
import { Search, Mail, Pencil, Trash2, Check, X, ArrowUpRight, ChevronLeft, ChevronRight, Lightbulb, ShieldCheck, History, ListFilter, Replace } from 'lucide-react'
import { api } from '../../api/client'
import { Badge, Btn, Card, EmptyState, Input, PanelSectionHeader, Skeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import SegmentedControl from '../../components/SegmentedControl'
import { fmtDateTimeNumeric, fmtNumber } from '../../i18n/format'
import type { MemoryEditOperation, MemoryEditPreview, MemoryRecord, MemoryRecordQuery, MemoryRecordRevision, MemoryRecordSelection } from '../../types/memoryEditing'
import { MEMORY_QUERY_PREFIXES, MemoryStoreAvatar, memoryQueryRetry, useMemoryStores } from './MemoryStoreCard'

const PAGE_SIZE = 50
const MANUAL_LIMIT = 500
const ICONS = { fact: Lightbulb, directive: ShieldCheck, episode: History }
export const MEMORY_RECORD_LABELS = { fact: 'memoryV2.fact', directive: 'memoryV2.directive', episode: 'memoryV2.episode' } as const
const TINTS = { fact: 'bg-accent-subtle text-accent', directive: 'bg-info/10 text-info', episode: 'bg-warn-subtle text-warn' }
const identity = (row: { kind: string; id: string }) => `${row.kind}:${row.id}`
const errorText = (error: unknown) => error instanceof Error ? error.message : error ? String(error) : ''
const decoded = (row: MemoryRecord): unknown => {
  if (typeof row.value_json !== 'string') return row.value_json
  try { return JSON.parse(row.value_json) } catch { return row.value_json }
}
export const memoryRecordBody = (row: MemoryRecord) => {
  if (row.kind === 'episode') return row.text
  const value = decoded(row)
  if (row.kind === 'directive' && value && typeof value === 'object' && 'rule' in value) return String(value.rule)
  return value && typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? row.text ?? '')
}
const replacement = (row: MemoryRecord, draft: string): MemoryEditOperation => {
  if (row.kind === 'episode') return { type: 'set', text: draft }
  const value = decoded(row)
  if (row.kind === 'directive' && value && typeof value === 'object' && 'rule' in value) return { type: 'set', value: { ...value, rule: draft } }
  return { type: 'set', value: typeof value !== 'string' && value !== undefined ? JSON.parse(draft) : draft }
}
type Intent = { type: 'set'; row: MemoryRecord } | { type: 'replace_text' } | { type: 'forget'; row?: MemoryRecord }
type QuerySelection = { query: MemoryRecordQuery; count: number; exclude: Map<string, MemoryRecord> }

type RecordOrigin = Pick<MemoryRecord, 'source' | 'updated_at' | 'metadata'> & { derived_from?: string | Record<string, unknown> }
type RecallRecord = RecordOrigin & { id?: string; key?: string; snippet?: string; text?: string; retrieval?: { matched_terms?: string[] } }
type RecallResult = { semantic_context?: string; episodic_context?: string; lessons_context?: string; retrieval?: { facts?: RecallRecord[]; episodes?: RecallRecord[]; operating_point?: { qualification?: 'reference_identity' | 'custom_or_registered' | 'unknown' } } }

const sourceLabelKey = (source: string) => {
  switch (source) {
    case 'user_seed': return 'memoryV2.source_copy'
    case 'user_explicit': return 'memoryV2.source_user'
    case 'consolidation': return 'memoryV2.source_conversation'
    case 'migration':
    case 'import': return 'memoryV2.source_migration'
    default: return undefined
  }
}

function RecordSource({ row }: { row: RecordOrigin }) {
  const { t } = useTranslation()
  const source = row.source || ''
  const labelKey = sourceLabelKey(source)
  const label = labelKey ? t(labelKey) : source || t('memoryV2.source_unknown')
  return <div className="flex min-w-0 flex-wrap items-center gap-2 text-[12px] text-muted"><Badge variant="muted">{label}</Badge>{row.updated_at && <time dateTime={row.updated_at} data-i18n-opaque>{fmtDateTimeNumeric(row.updated_at)}</time>}</div>
}

function RecordProvenance({ row }: { row: RecordOrigin }) {
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
  return <div className="space-y-2"><RecordSource row={row} />{typeof label === 'string' && <div className="flex min-w-0 items-center gap-2 text-[13px]"><MemoryStoreAvatar summary={summary} size={22} /><span className="break-words">{t('memoryEditing.copy_origin', { source: label })}</span></div>}{typeof reference === 'string' && <p className="break-words text-[12px] text-muted [overflow-wrap:anywhere]">{t('memoryEditing.original_reference')}: {referenceLabelKey ? t(referenceLabelKey) : reference}</p>}</div>
}

function RecallEvidence({ data }: { data: RecallResult }) {
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
        const Icon = ICONS[group.kind]
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
        {item.retrieval?.matched_terms?.length ? <p>{t('memoryV2.matched_terms', { terms: item.retrieval.matched_terms.join(', ') })}</p> : null}
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

function RecordHistory({ store, row, onProposal, onKeepCurrent }: { store: string; row: MemoryRecord; onProposal: (proposal: MemoryRecord, operation: 'set' | 'forget') => void; onKeepCurrent: () => void }) {
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
    {!!history.error && <Btn className="min-h-11" disabled={history.isFetching} onClick={() => void (history.isFetchNextPageError ? history.fetchNextPage() : history.refetch())}>{t('memoryV2.retry_read')}</Btn>}
    {entries.map(item => { const proposal = proposed(item.after_json); const before = proposed(item.before_json); const removed = item.operation === 'forget'; const pending = item.status === 'conflict' && item.base_revision === currentRevision; return <details key={item.id} open={pending} className="rounded-lg border border-border p-3"><summary className="cursor-pointer text-[13px]"><Badge variant="muted">{t(item.status === 'conflict' ? 'memoryEditing.proposal' : 'memoryEditing.accepted')}</Badge><span className="ml-2 text-[12px] text-muted">{fmtDateTimeNumeric(item.created_at)}</span></summary><div className="mt-3 space-y-3"><RecordProvenance row={origin(item)} />{before && <div><PanelSectionHeader label={t('memoryEditing.before')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{memoryRecordBody(before)}</p></div>}{(proposal || removed) && <div><PanelSectionHeader label={t('memoryEditing.after')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{removed ? t('memoryEditing.removed') : memoryRecordBody(proposal!)}</p></div>}{pending && <div className="flex flex-wrap gap-2">{(proposal || removed) && <Btn className="min-h-11" onClick={() => onProposal(proposal || row, removed ? 'forget' : 'set')}>{t('memoryEditing.use_proposal')}</Btn>}<Btn className="min-h-11" onClick={onKeepCurrent}>{t('memoryEditing.keep_current')}</Btn></div>}</div></details> })}
    {history.data && !entries.length && <p className="text-[13px] text-muted">{t('memoryEditing.no_history')}</p>}
    {history.hasNextPage && !history.error && <Btn className="min-h-11" disabled={history.isFetching} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? t('pages.overview.memoryTab.loading') : t('memoryV2.show_more')}</Btn>}
  </div>
}

export default function MemoryRecordsEditor({ store, privateMemory = false, onDirtyChange, emptyActions, onRecovery }: {
  store: string; privateMemory?: boolean; onDirtyChange?: (dirty: boolean) => void; emptyActions?: ReactNode; onRecovery?: () => void
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const reduceMotion = useReducedMotion()
  const [input, setInput] = useState('')
  const [search, setSearch] = useState('')
  const [kind, setKind] = useState<MemoryRecordQuery['kind']>('all')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<Map<string, MemoryRecord>>(new Map())
  const [all, setAll] = useState<QuerySelection | null>(null)
  const [intent, setIntent] = useState<Intent | null>(null)
  const [detail, setDetail] = useState<MemoryRecord | null>(null)
  const [draft, setDraft] = useState('')
  const [find, setFind] = useState('')
  const [replaceWith, setReplaceWith] = useState('')
  const [matchCase, setMatchCase] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  // The existing unscoped V1 writer must never become a private-store action.
  const canSetGlobalFact = !privateMemory && (!store || store === 'default')
  const [selectionNotice, setSelectionNotice] = useState('')
  const [missingRecord, setMissingRecord] = useState(false)
  const [recallQuery, setRecallQuery] = useState('')
  const [reviewedPreview, setReviewedPreview] = useState<MemoryEditPreview | null>(null)
  const [previewPageOffset, setPreviewPageOffset] = useState(0)
  useEffect(() => { if (input.trim() === search) return; const timer = setTimeout(() => { setSearch(input.trim()); setPage(0) }, 250); return () => clearTimeout(timer) }, [input, search])
  const query = useMemo<MemoryRecordQuery>(() => ({ q: search, kind }), [search, kind])
  const records = useQuery({ queryKey: ['memory-records', store, query, page], queryFn: () => api.memoryRecords(store, query, page * PAGE_SIZE, PAGE_SIZE), retry: memoryQueryRetry })
  const recall = useQuery({ queryKey: ['member-memory', store, 'recall', recallQuery], queryFn: () => api.memoryRecall(recallQuery, store), enabled: privateMemory && !!recallQuery, retry: memoryQueryRetry })
  const searching = input.trim() !== search || records.isPending
  const recallCurrent = !!recallQuery && recallQuery === search && input.trim() === search
  const rows = searching || records.error ? [] : records.data?.entries || []
  const selectedCount = all ? all.count : selected.size
  const isSelected = (row: MemoryRecord) => all ? !all.exclude.has(identity(row)) : selected.has(identity(row))
  const clearSelection = () => { setSelected(new Map()); setAll(null) }
  const selection = (): MemoryRecordSelection => {
    const row = intent && 'row' in intent ? intent.row : undefined
    if (row) return { items: [{ kind: row.kind, id: row.id, revision: row.revision }] }
    return all ? { query: all.query, exclude: [...all.exclude.values()].map(({ kind, id }) => ({ kind, id })) }
      : { items: [...selected.values()].map(({ kind, id, revision }) => ({ kind, id, revision })) }
  }
  const previewPage = useMutation({ mutationFn: ({ previewId, offset }: { previewId: string; offset: number }) => api.memoryEditPreviewPage(store, previewId, offset), onSuccess: result => setReviewedPreview(result) })
  const preview = useMutation({ mutationFn: ({ row }: { row?: MemoryRecord }) => {
    let operation: MemoryEditOperation
    if (row) return api.memoryEditPreview(store, { items: [{ kind: row.kind, id: row.id, revision: row.revision }] }, replacement(row, memoryRecordBody(row)))
    if (intent?.type === 'set') {
      try { operation = replacement(intent.row, draft) } catch { throw new Error(t('memoryV2.structured_error')) }
    } else operation = intent?.type === 'forget' ? { type: 'forget' } : { type: 'replace_text', find, replacement: replaceWith, match_case: matchCase }
    return api.memoryEditPreview(store, selection(), operation)
  }, onSuccess: result => {
    previewPage.reset(); setPreviewPageOffset(0); setReviewedPreview(result)
    if (all && !(intent && 'row' in intent && intent.row)) setAll(old => old ? { ...old, count: result.matched_count } : old)
  } })
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['memory-records', store] })
    void client.invalidateQueries({ queryKey: ['member-memory', store] })
    void client.invalidateQueries({ queryKey: ['memory-record-history', store] })
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
  }
  const apply = useMutation({ mutationFn: () => api.memoryEditApply(store, preview.data!.preview_id), onSuccess: result => {
    setFeedback(t('memoryEditing.applied', { count: fmtNumber(result.changed_count) })); setIntent(null); clearSelection(); preview.reset(); previewPage.reset(); setReviewedPreview(null); setPage(0); refresh()
  } })
  const setGlobalFact = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => api.vectorSemanticWrite(key, value),
    onSuccess: () => {
      setNewKey(''); setNewValue(''); setFeedback(t('pages.overview.memoryTab.saved')); refresh()
    },
  })
  const refreshSelection = useMutation({ mutationFn: async () => {
    const current = selection()
    if ('query' in current) return api.memoryQuerySelectionRefresh(store, current)
    return api.memoryRecordsRefresh(store, current.items.map(({ kind, id }) => ({ kind, id })))
  }, onSuccess: result => {
    if ('matched_count' in result) {
      setAll(old => old ? { ...old, count: result.matched_count } : old)
    } else {
      if (intent && 'row' in intent && intent.row) {
        const current = result.entries.find(row => identity(row) === identity(intent.row!))
        setMissingRecord(!current)
        if (current) setIntent({ ...intent, row: current })
      } else setSelected(new Map(result.entries.map(row => [identity(row), row])))
    }
    preview.reset(); previewPage.reset(); setReviewedPreview(null); setPreviewPageOffset(0); apply.reset(); refresh()
    setSelectionNotice(t('missing' in result && result.missing.length ? 'memoryEditing.missing_selection' : 'memoryEditing.selection_refreshed'))
  } })
  const applyConflict = !!apply.error && typeof apply.error === 'object' && 'status' in apply.error && (apply.error.status === 409 || apply.error.status === 400)
  const previewPageConflict = !!previewPage.error && typeof previewPage.error === 'object' && 'status' in previewPage.error && previewPage.error.status === 409
  const busy = preview.isPending || previewPage.isPending || apply.isPending || refreshSelection.isPending
  const dirty = !!intent || !!all || selectedCount > 0 || (canSetGlobalFact && (!!newKey || !!newValue || setGlobalFact.isPending))
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
  const begin = (next: Intent) => { preview.reset(); previewPage.reset(); setReviewedPreview(null); setPreviewPageOffset(0); apply.reset(); refreshSelection.reset(); setSelectionNotice(''); setMissingRecord(false); setIntent(next); setDetail(null); if (next.type === 'set') setDraft(memoryRecordBody(next.row)) }
  const toggle = (row: MemoryRecord) => {
    if (all) { setAll(old => { const exclude = new Map(old!.exclude); if (exclude.has(identity(row))) exclude.delete(identity(row)); else if (exclude.size < MANUAL_LIMIT) exclude.set(identity(row), row); return { ...old!, count: Math.max(0, old!.count + old!.exclude.size - exclude.size), exclude } }); return }
    setSelected(old => { const next = new Map(old); if (next.has(identity(row))) next.delete(identity(row)); else if (next.size < MANUAL_LIMIT) next.set(identity(row), row); return next })
  }
  const choosePage = () => {
    if (all) { setAll(old => { const exclude = new Map(old!.exclude); const every = rows.every(row => !exclude.has(identity(row))); for (const row of rows) { if (every) { if (exclude.size < MANUAL_LIMIT) exclude.set(identity(row), row) } else exclude.delete(identity(row)) } return { ...old!, count: Math.max(0, old!.count + old!.exclude.size - exclude.size), exclude } }); return }
    setSelected(old => { const next = new Map(old); const every = rows.every(row => next.has(identity(row))); for (const row of rows) { if (every) next.delete(identity(row)); else if (next.size < MANUAL_LIMIT) next.set(identity(row), row) } return next })
  }
  const resetFilters = () => { setInput(''); setSearch(''); setKind('all'); setPage(0); setRecallQuery('') }
  const currentTotal = records.data?.total || 0
  useEffect(() => { if (records.data && page > 0 && page * PAGE_SIZE >= records.data.total) setPage(Math.max(0, Math.ceil(records.data.total / PAGE_SIZE) - 1)) }, [records.data, page])
  const editAction = t('memoryV2.edit_action')
  const intentCount = intent && 'row' in intent && intent.row ? 1 : selectedCount
  const title = intent?.type === 'set' ? t('memoryV2.edit_memory_title') : intent?.type === 'forget' ? 'row' in intent && intent.row ? t('memoryV2.forget') : t('memoryEditing.forget_count', { count: fmtNumber(intentCount) }) : t('memoryEditing.replace_title')

  return <div className="min-w-0 space-y-4" data-testid="memory-records-editor">
    {feedback && <div role="status" className="flex items-center gap-2 text-[13px] text-ok"><Check className="lucide-inline" />{feedback}<Btn aria-label={t('components.errorNotice.dismiss')} className="ml-auto min-h-11 min-w-11 justify-center" onClick={() => setFeedback('')}><X className="lucide-inline" /></Btn></div>}
    {canSetGlobalFact && <Card className="mb-0">
      <PanelSectionHeader label={t('pages.overview.vectorMemoryCard.semantic_memory')} />
      <form className="mt-3 flex min-w-0 flex-col gap-3 sm:flex-row sm:items-end" onSubmit={event => {
        event.preventDefault()
        if (newKey.trim() && newValue.trim() && !setGlobalFact.isPending) setGlobalFact.mutate({ key: newKey.trim(), value: newValue })
      }}>
        <Input className="min-h-11 min-w-0 flex-1" aria-label={t('pages.overview.vectorMemoryCard.key_e_g_pref_backend_framework')} placeholder={t('pages.overview.vectorMemoryCard.key_e_g_pref_backend_framework')} value={newKey} disabled={setGlobalFact.isPending} onChange={event => { setNewKey(event.target.value); setGlobalFact.reset() }} />
        <Input className="min-h-11 min-w-0 flex-1" aria-label={t('pages.overview.vectorMemoryCard.value')} placeholder={t('pages.overview.vectorMemoryCard.value')} value={newValue} disabled={setGlobalFact.isPending} onChange={event => { setNewValue(event.target.value); setGlobalFact.reset() }} />
        <Btn type="submit" primary className="min-h-11 justify-center" disabled={!newKey.trim() || !newValue.trim() || setGlobalFact.isPending}>{t('pages.overview.vectorMemoryCard.set')}</Btn>
      </form>
      {/* No hand-off: retain the fact key and value when a write fails. */}
      <ErrorNotice message={errorText(setGlobalFact.error)} />
    </Card>}
    <div className="space-y-3">
      <div className="flex min-w-0 items-center gap-2"><Search className="lucide-inline shrink-0 text-muted" /><Input className="min-h-11 min-w-0 flex-1" aria-label={t('memoryV2.search')} placeholder={t('memoryEditing.search_hint')} value={input} disabled={!!all} onChange={event => setInput(event.target.value)} />{(input || kind !== 'all') && <Btn aria-label={t('memoryV2.clear_search')} className="min-h-11 min-w-11 justify-center" disabled={!!all} onClick={resetFilters}><X className="lucide-inline" /></Btn>}</div>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <div className="min-w-0 max-w-full overflow-x-auto"><SegmentedControl collapse={false} layoutId={`memory-edit-kind-${store}`} value={kind} onChange={next => { if (!all) { setKind(next); setPage(0) } }} segments={[
          { key: 'all', label: t('memoryV2.filter_all'), icon: <ListFilter className="lucide-inline" />, disabled: !!all },
          { key: 'fact', label: t('memoryV2.facts_label'), disabled: !!all }, { key: 'directive', label: t('memoryV2.rules_label'), disabled: !!all }, { key: 'episode', label: t('memoryV2.experiences_label'), disabled: !!all },
        ]} /></div>
        {privateMemory && search && <Btn className="min-h-11" disabled={recall.isFetching || searching} onClick={() => { setRecallQuery(search); if (recallQuery === search) void recall.refetch() }}><Search className="lucide-inline" />{t('memoryV2.recall')}</Btn>}
      </div>
      <p className="text-[12px] leading-relaxed text-muted">{t('memoryV2.memory_kinds_explanation')}</p>
    </div>
    {/* No hand-off: cross-page selection and editing drafts must stay attached to this store. */}
    <ErrorNotice message={errorText(records.error || (recallCurrent ? recall.error : null))} />
    {records.error && <div className="flex flex-wrap gap-2"><Btn className="min-h-11" disabled={records.isFetching} onClick={() => void records.refetch()}>{t('memoryV2.retry_read')}</Btn>{onRecovery && <Btn className="min-h-11" onClick={onRecovery}>{t('memoryV2.tab_recovery')}</Btn>}</div>}
    {recallCurrent && recall.data && <RecallEvidence data={recall.data} />}
    <motion.div layout={!reduceMotion} className="rounded-xl border border-border bg-bg-elevated p-3" aria-live="polite">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {rows.length > 0 && <div><label className="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]"><input aria-label={t('memoryEditing.select_page')} type="checkbox" className="h-4 w-4 accent-accent" checked={rows.every(isSelected)} disabled={!!all && all.exclude.size >= MANUAL_LIMIT && rows.every(isSelected)} onChange={choosePage} />{t('memoryEditing.select_page')}</label></div>}
        {selectedCount > 0 || all ? <><Badge variant="muted">{t('memoryEditing.selected', { count: fmtNumber(selectedCount) })}</Badge><Btn className="min-h-11" onClick={clearSelection}>{t('memoryEditing.clear_selection')}</Btn></> : !searching && !records.error && <span className="text-[12px] text-muted">{t('memoryEditing.matching', { count: fmtNumber(currentTotal) })}</span>}
      </div>
      {!all && selectedCount > 0 && currentTotal > rows.length && <Btn className="min-h-11 max-w-full whitespace-normal text-left" disabled={searching || !!records.error || currentTotal > 10000} onClick={() => { setAll({ query, count: currentTotal, exclude: new Map() }); setSelected(new Map()) }}>{t('memoryEditing.select_all', { count: fmtNumber(currentTotal) })}</Btn>}
      {(selectedCount > 0 || all) && <div className="mt-2 flex flex-wrap gap-2"><Btn primary className="min-h-11" disabled={!selectedCount} onClick={() => begin({ type: 'replace_text' })}><Replace className="lucide-inline" />{t('memoryEditing.replace')}</Btn><Btn danger className="min-h-11" disabled={!selectedCount} onClick={() => begin({ type: 'forget' })}><Trash2 className="lucide-inline" />{t('memoryEditing.forget_count', { count: fmtNumber(selectedCount) })}</Btn></div>}
      {!selectedCount && !all && rows.length > 0 && <p className="text-[12px] text-muted">{t('memoryV2.selection_hint')}</p>}
      {all && <p className="text-[12px] text-muted">{t('memoryEditing.all_scope', { query: all.query.q || t('memoryV2.filter_all') })}</p>}
      {!all && selectedCount >= MANUAL_LIMIT && <p role="status" className="text-[12px] text-muted">{t('memoryEditing.selection_limit')}</p>}
      {currentTotal > 10000 && selectedCount > 0 && <p className="text-[12px] text-muted">{t('memoryEditing.narrow_selection')}</p>}
    </motion.div>
    {searching && <div role="status" aria-label={t('pages.overview.memoryTab.loading')} className="grid gap-3 lg:grid-cols-2">{[0, 1, 2, 3].map(index => <Skeleton key={index} className="h-36 rounded-xl motion-reduce:animate-none" />)}</div>}
    {!searching && !records.error && !rows.length && <Card className="mb-0 py-6"><EmptyState icon={<Search className="lucide-inline" />} title={search || kind !== 'all' ? t('memoryV2.no_matches') : t(privateMemory ? 'memoryV2.empty_title' : 'memoryV2.store_empty_title')} subtitle={search || kind !== 'all' ? t('memoryV2.no_matches_hint') : t(privateMemory ? 'memoryV2.empty_body' : 'memoryV2.store_empty_body')} />{!search && kind === 'all' && <div className="mt-3 flex flex-wrap justify-center gap-2">{emptyActions}</div>}</Card>}
    <div className="grid min-w-0 gap-3 lg:grid-cols-2">{rows.map(row => { const Icon = ICONS[row.kind]; const chosen = isSelected(row); return <motion.div layout={!reduceMotion} key={identity(row)} initial={false} whileHover={reduceMotion ? undefined : { y: -2 }}>
      <Card className={`mb-0 flex h-full min-w-0 flex-col py-4 transition-colors ${chosen ? 'border-accent bg-accent-subtle' : 'hover:border-accent/30'}`}>
        <div className="flex min-w-0 items-center gap-2.5"><input aria-label={t('memoryEditing.select_record', { text: memoryRecordBody(row) })} type="checkbox" className="h-4 w-4 shrink-0 accent-accent" checked={chosen} disabled={!row.revision || (!all && !chosen && selectedCount >= MANUAL_LIMIT) || (!!all && chosen && all.exclude.size >= MANUAL_LIMIT)} onChange={() => toggle(row)} /><span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-lg ${TINTS[row.kind]}`}><Icon className="lucide-inline" /></span><div className="min-w-0"><span className="block text-[12px] font-medium">{t(MEMORY_RECORD_LABELS[row.kind])}</span>{!privateMemory && row.key && <span className="block truncate text-[11px] text-muted" title={row.key}>{row.key}</span>}</div></div>
        <p className="my-3 line-clamp-3 whitespace-pre-wrap break-words text-sm leading-relaxed [overflow-wrap:anywhere]">{memoryRecordBody(row)}</p>
        {!!row.metadata?.email_addresses?.length && <div className="mb-3 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted">{row.metadata?.email_addresses.slice(0, 3).map(email => <span key={email} className="inline-flex min-w-0 max-w-full items-start gap-1"><Mail className="lucide-inline shrink-0" aria-hidden="true" /><span className="break-all">{email}</span></span>)}</div>}
        <RecordSource row={row} />{(row.metadata?.pending_conflicts || 0) > 0 && <Btn className="mt-2 min-h-11" onClick={() => setDetail(row)}><History className="lucide-inline" />{t('memoryEditing.review_proposals', { count: fmtNumber(row.metadata!.pending_conflicts!) })}</Btn>}
        <div className="mt-auto flex justify-between gap-2 border-t border-border pt-3"><Btn className="min-h-11" onClick={() => setDetail(row)}>{t('memoryV2.view_details')}<ArrowUpRight className="lucide-inline" /></Btn><Btn className="min-h-11" disabled={!row.revision} onClick={() => begin({ type: 'set', row })}><Pencil className="lucide-inline" />{editAction}</Btn></div>
      </Card>
    </motion.div> })}</div>
    {!searching && !records.error && currentTotal > 0 && <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[12px] text-muted">{t('memoryEditing.page_range', { from: fmtNumber(page * PAGE_SIZE + 1), to: fmtNumber(page * PAGE_SIZE + rows.length), total: fmtNumber(currentTotal) })}</span><div className="flex gap-2"><Btn aria-label={t('memoryEditing.previous')} className="min-h-11 min-w-11 justify-center" disabled={page === 0} onClick={() => setPage(value => value - 1)}><ChevronLeft className="lucide-inline" /></Btn><Btn aria-label={t('memoryEditing.next')} className="min-h-11 min-w-11 justify-center" disabled={!records.data?.has_more} onClick={() => setPage(value => value + 1)}><ChevronRight className="lucide-inline" /></Btn></div></div>}
    {detail && <Modal open title={t('memoryV2.detail_title')} onClose={() => setDetail(null)} footer={<div className="flex flex-wrap gap-2"><Btn className="min-h-11" onClick={() => begin({ type: 'set', row: detail })}>{editAction}</Btn><Btn danger className="min-h-11" onClick={() => begin({ type: 'forget', row: detail })}>{t('memoryV2.forget')}</Btn></div>}><div className="space-y-4"><Badge variant="muted">{t(MEMORY_RECORD_LABELS[detail.kind])}</Badge><p className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">{memoryRecordBody(detail)}</p>{privateMemory && detail.key && <dl className="space-y-1 text-[12px]"><dt className="text-muted">{t('memoryV2.memory_key')}</dt><dd className="break-words [overflow-wrap:anywhere]"><code>{detail.key}</code></dd></dl>}<PanelSectionHeader label={t('memoryV2.provenance')} /><RecordProvenance row={detail} /><RecordHistory store={store} row={detail} onKeepCurrent={() => { begin({ type: 'set', row: detail }); preview.mutate({ row: detail }) }} onProposal={(proposal, operation) => { begin(operation === 'forget' ? { type: 'forget', row: detail } : { type: 'set', row: detail }); if (operation === 'set') setDraft(memoryRecordBody(proposal)) }} /></div></Modal>}
    {intent && <Modal open title={title} guardAccidentalDismiss onClose={busy ? () => {} : () => setIntent(null)} footer={<div className="flex flex-wrap justify-end gap-2">{intent.type === 'forget' && <Btn className="min-h-11" disabled={busy} onClick={() => setIntent(null)}>{t('components.confirmDialog.cancel')}</Btn>}{reviewedPreview && intent.type !== 'forget' && <Btn className="min-h-11" disabled={busy} onClick={() => { preview.reset(); previewPage.reset(); setReviewedPreview(null); setPreviewPageOffset(0); apply.reset() }}>{t('memoryEditing.adjust')}</Btn>}{reviewedPreview && !applyConflict ? <Btn primary={intent.type !== 'forget'} danger={intent.type === 'forget'} className="min-h-11" disabled={busy || !!previewPage.error || reviewedPreview.changed_count === 0} onClick={() => apply.mutate()}>{apply.isPending ? t('memoryEditing.applying') : intent.type === 'forget' ? t('memoryEditing.forget_count', { count: fmtNumber(reviewedPreview.changed_count) }) : apply.error ? t('memoryEditing.retry_apply') : t('memoryEditing.apply', { count: fmtNumber(reviewedPreview.changed_count) })}</Btn> : <Btn primary className="min-h-11" disabled={busy || missingRecord || (!('row' in intent && intent.row) && selectedCount === 0) || (intent.type === 'set' && !draft.trim()) || (intent.type === 'replace_text' && !find)} onClick={() => { apply.reset(); refreshSelection.reset(); preview.mutate({}) }}>{preview.isPending ? t('memoryEditing.preparing') : t('memoryEditing.preview')}</Btn>}</div>}>
      <div className="space-y-4">
        {/* No hand-off: preserve the correction, selected records and reviewed preview. */}<ErrorNotice message={errorText(refreshSelection.error || preview.error || previewPage.error || apply.error)} />{selectionNotice && <p role="status" className="text-[13px] text-muted">{selectionNotice}</p>}{(applyConflict || preview.error || previewPageConflict || missingRecord) && <Btn className="min-h-11" disabled={busy} onClick={() => refreshSelection.mutate()}>{t('memoryEditing.refresh_selection')}</Btn>}{previewPage.error && !previewPageConflict && <Btn className="min-h-11" disabled={busy} onClick={() => previewPage.mutate({ previewId: preview.data!.preview_id, offset: previewPageOffset })}>{t('memoryEditing.retry_preview_page')}</Btn>}
        {intent.type === 'forget' && <p className="text-[13px]">{t('memoryV2.forget_explanation')}</p>}
        {apply.error && <p className="text-[13px] text-muted">{t(applyConflict ? 'memoryEditing.conflict_hint' : 'memoryEditing.unconfirmed_hint')}</p>}
        {!reviewedPreview && <>{intent.type === 'set' && <><RecordProvenance row={intent.row} /><textarea aria-label={t('memoryV2.memory_text')} className="min-h-40 w-full rounded-lg border border-border bg-bg-elevated p-3 text-sm focus-ring" disabled={busy} value={draft} onChange={event => setDraft(event.target.value)} /></>}{intent.type === 'replace_text' && <><label className="block space-y-1 text-[13px]"><span>{t('memoryEditing.find')}</span><Input className="min-h-11 w-full" value={find} disabled={busy} onChange={event => setFind(event.target.value)} /></label><label className="block space-y-1 text-[13px]"><span>{t('memoryEditing.replacement')}</span><Input className="min-h-11 w-full" value={replaceWith} disabled={busy} onChange={event => setReplaceWith(event.target.value)} /></label><label className="flex min-h-11 items-center gap-2 text-[13px]"><input aria-label={t('memoryEditing.match_case')} type="checkbox" className="h-4 w-4 accent-accent" disabled={busy} checked={matchCase} onChange={event => setMatchCase(event.target.checked)} />{t('memoryEditing.match_case')}</label><p className="text-[12px] text-muted">{t('memoryEditing.replace_hint')}</p></>}{intent.type === 'forget' && intent.row && <p className="whitespace-pre-wrap break-words text-sm">{memoryRecordBody(intent.row)}</p>}</>}
        {reviewedPreview && <><div role="status" className="flex flex-wrap gap-2"><Badge variant="muted">{t('memoryEditing.will_change', { count: fmtNumber(reviewedPreview.changed_count) })}</Badge><Badge variant="muted">{t('memoryEditing.unchanged', { count: fmtNumber(reviewedPreview.unchanged_count) })}</Badge></div><p className="text-[12px] text-muted">{t('memoryEditing.preview_expiry', { time: fmtDateTimeNumeric(reviewedPreview.expires_at) })}</p>{reviewedPreview.changed_count > 0 && <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[12px] text-muted">{t('memoryEditing.page_range', { from: fmtNumber(reviewedPreview.preview_offset + 1), to: fmtNumber(reviewedPreview.preview_offset + reviewedPreview.entries.length), total: fmtNumber(reviewedPreview.changed_count) })}</span><div className="flex gap-2"><Btn aria-label={t('memoryEditing.previous')} className="min-h-11 min-w-11 justify-center" disabled={busy || !!previewPage.error || reviewedPreview.preview_offset === 0} onClick={() => { const offset = Math.max(0, reviewedPreview.preview_offset - reviewedPreview.preview_limit); setPreviewPageOffset(offset); previewPage.mutate({ previewId: preview.data!.preview_id, offset }) }}><ChevronLeft className="lucide-inline" /></Btn><Btn aria-label={t('memoryEditing.next')} className="min-h-11 min-w-11 justify-center" disabled={busy || !!previewPage.error || !reviewedPreview.preview_has_more} onClick={() => { const offset = reviewedPreview.preview_offset + reviewedPreview.preview_limit; setPreviewPageOffset(offset); previewPage.mutate({ previewId: preview.data!.preview_id, offset }) }}><ChevronRight className="lucide-inline" /></Btn></div></div>}<div className="max-h-96 space-y-3 overflow-y-auto">{reviewedPreview.entries.map(({ before, after, operation }) => <div key={identity(before)} className="rounded-xl border border-border p-3">{operation === 'resolve' && <p className="mb-3 flex items-start gap-2 text-[13px] text-accent"><ShieldCheck className="lucide-inline mt-0.5 shrink-0" />{t('memoryEditing.resolve_explanation')}</p>}<Badge variant="muted">{t(MEMORY_RECORD_LABELS[before.kind])}</Badge><div className="mt-2 grid min-w-0 gap-3 sm:grid-cols-2"><div className="min-w-0"><PanelSectionHeader label={t('memoryEditing.before')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{memoryRecordBody(before)}</p></div><div className="min-w-0"><PanelSectionHeader label={t('memoryEditing.after')} /><p className="mt-2 whitespace-pre-wrap break-words text-[13px] [overflow-wrap:anywhere]">{after ? memoryRecordBody(after) : t('memoryEditing.removed')}</p></div></div></div>)}</div>{reviewedPreview.warnings.map((warning, index) => <p key={index} className="text-[13px] text-muted">{warning}</p>)}</>}
      </div>
    </Modal>}
  </div>
}
