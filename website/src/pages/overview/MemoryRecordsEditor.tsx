import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { motion, useReducedMotion } from 'framer-motion'
import { Search, Mail, Pencil, Trash2, Check, X, ArrowUpRight, ChevronLeft, ChevronRight, History, ListFilter, Replace } from 'lucide-react'
import { api } from '../../api/client'
import { safeGetSessionItem, safeSetSessionItem } from '../../utils/safeStorage'
import { Badge, Btn, Card, EmptyState, Input, PanelSectionHeader, Skeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import SegmentedControl from '../../components/SegmentedControl'
import { fmtNumber } from '../../i18n/format'
import type { MemoryRecord, MemoryRecordQuery } from '../../types/memoryEditing'
import { MEMORY_QUERY_PREFIXES, memoryQueryRetry } from './MemoryStoreCard'
import { semanticWriteErrorMessage } from './embeddingStatusText'
import { queryReadFailed } from '../../utils/queryReadFailed'
import { usePagerFocus } from '../../hooks/usePagerFocus'
import { MEMORY_RECORD_ICONS, MEMORY_RECORD_LABELS, RecallEvidence, RecordHistory, RecordProvenance, RecordSource, errorText, memoryRecordBody, recordIdentity } from './memoryRecordParts'
import { MANUAL_LIMIT, useMemoryRecordEdit, useMemoryRecordSelection } from './useMemoryRecordEdit'

export { MEMORY_RECORD_LABELS, memoryRecordBody } from './memoryRecordParts'

const PAGE_SIZE = 50
const TINTS = { fact: 'bg-accent-subtle text-accent', directive: 'bg-info/10 text-info-text', episode: 'bg-warn-subtle text-warn-text' }

// The applied search and kind filter, kept per store for the browser session.
// Not the URL: the Settings rail reopens `/settings` with no query string, so a
// URL filter would not survive the leave-and-return trip it exists for.
const FILTER_KEY_PREFIX = 'kirocrew:memory-records-filter:'
function loadFilter(store: string): { q: string; kind: MemoryRecordQuery['kind'] } {
  try {
    const saved = JSON.parse(safeGetSessionItem(FILTER_KEY_PREFIX + store) || '{}')
    const kind = saved.kind === 'fact' || saved.kind === 'directive' || saved.kind === 'episode' ? saved.kind : 'all'
    return { q: typeof saved.q === 'string' ? saved.q : '', kind }
  } catch { return { q: '', kind: 'all' } }
}

export default function MemoryRecordsEditor({ store, privateMemory = false, onDirtyChange, emptyActions, onRecovery, focusKey }: {
  store: string; privateMemory?: boolean; onDirtyChange?: (dirty: boolean) => void; emptyActions?: ReactNode; onRecovery?: () => void
  /** A saved lesson landing, searched by key without replacing the stored filter. */
  focusKey?: string
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const reduceMotion = useReducedMotion()
  const [saved] = useState(() => loadFilter(store))
  const [input, setInput] = useState(focusKey ?? saved.q)
  const [search, setSearch] = useState(focusKey ?? saved.q)
  const [kind, setKind] = useState<MemoryRecordQuery['kind']>(focusKey ? 'directive' : saved.kind)
  useEffect(() => {
    if (focusKey && search === focusKey && kind === 'directive') return
    safeSetSessionItem(FILTER_KEY_PREFIX + store, JSON.stringify({ q: search, kind }))
  }, [store, search, kind, focusKey])
  const [page, setPage] = useState(0)
  const pager = usePagerFocus()
  const picked = useMemoryRecordSelection()
  const { all, isSelected, toggle } = picked
  const [detail, setDetail] = useState<MemoryRecord | null>(null)
  const [feedback, setFeedback] = useState('')
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  // The existing unscoped V1 writer must never become a private-store action.
  const canSetGlobalFact = !privateMemory && (!store || store === 'default')
  const [recallQuery, setRecallQuery] = useState('')
  useEffect(() => { if (input.trim() === search) return; const timer = setTimeout(() => { setSearch(input.trim()); setPage(0) }, 250); return () => clearTimeout(timer) }, [input, search])
  const query = useMemo<MemoryRecordQuery>(() => ({ q: search, kind }), [search, kind])
  const records = useQuery({ queryKey: ['memory-records', store, query, page], queryFn: () => api.memoryRecords(store, query, page * PAGE_SIZE, PAGE_SIZE), retry: memoryQueryRetry })
  const recall = useQuery({ queryKey: ['member-memory', store, 'recall', recallQuery], queryFn: () => api.memoryRecall(recallQuery, store), enabled: privateMemory && !!recallQuery, retry: memoryQueryRetry })
  const searching = input.trim() !== search || records.isPending
  const recallCurrent = !!recallQuery && recallQuery === search && input.trim() === search
  const rows = searching || records.error ? [] : records.data?.entries || []
  const focused = input === focusKey && search === focusKey && kind === 'directive' ? rows.find(row => row.id === focusKey) : undefined
  const focusRow = useRef<HTMLDivElement>(null)
  const focusScrolled = useRef(false)
  useEffect(() => {
    if (!focused || !focusRow.current || focusScrolled.current) return
    focusRow.current.scrollIntoView({ block: 'center' })
    focusScrolled.current = true
  }, [focused])
  const selectedCount = picked.count
  const clearSelection = picked.clear
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['memory-records', store] })
    void client.invalidateQueries({ queryKey: ['member-memory', store] })
    void client.invalidateQueries({ queryKey: ['memory-record-history', store] })
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
  }
  const edit = useMemoryRecordEdit({
    store, refresh, onBegin: () => setDetail(null),
    onApplied: changed => { setFeedback(t('memoryEditing.applied', { count: fmtNumber(changed) })); setPage(0); refresh() },
    selection: picked.forEdit,
  })
  const setGlobalFact = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => api.vectorSemanticWrite(key, value),
    onSuccess: () => {
      setNewKey(''); setNewValue(''); setFeedback(t('pages.overview.memoryTab.saved')); refresh()
    },
  })
  const dirty = !!edit.intent || !!all || selectedCount > 0 || (canSetGlobalFact && (!!newKey || !!newValue || setGlobalFact.isPending))
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
  const begin = edit.begin
  const choosePage = () => picked.choosePage(rows)
  const resetFilters = () => { setInput(''); setSearch(''); setKind('all'); setPage(0); setRecallQuery('') }
  const currentTotal = records.data?.total || 0
  useEffect(() => { if (records.data && page > 0 && page * PAGE_SIZE >= records.data.total) setPage(Math.max(0, Math.ceil(records.data.total / PAGE_SIZE) - 1)) }, [records.data, page])
  const editAction = t('memoryV2.edit_action')

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
      <ErrorNotice message={semanticWriteErrorMessage(setGlobalFact.error) || errorText(setGlobalFact.error)} />
    </Card>}
    <div className="space-y-3">
      <div className="flex min-w-0 items-center gap-2"><Search className="lucide-inline shrink-0 text-muted" /><Input className="min-h-11 min-w-0 flex-1" aria-label={t('memoryV2.search')} placeholder={t('memoryEditing.search_hint')} value={input} disabled={!!all} onChange={event => setInput(event.target.value)} />{(input || kind !== 'all') && <Btn aria-label={t('memoryV2.clear_search')} className="min-h-11 min-w-11 justify-center" disabled={!!all} onClick={resetFilters}><X className="lucide-inline" /></Btn>}</div>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <div className="min-w-0 max-w-full overflow-x-auto"><SegmentedControl collapse={false} layoutId={`memory-edit-kind-${store}`} value={kind} onChange={next => { if (!all) { setKind(next); setPage(0) } }} segments={[
          { key: 'all', label: t('memoryV2.filter_all'), icon: <ListFilter className="lucide-inline" />, disabled: !!all },
          { key: 'fact', label: t('memoryV2.facts_label'), disabled: !!all }, { key: 'directive', label: t('memoryV2.rules_label'), disabled: !!all }, { key: 'episode', label: t('memoryV2.experiences_label'), disabled: !!all },
        ]} /></div>
        {privateMemory && search && <Btn busy={recall.isFetching || searching} className="min-h-11" onClick={() => { setRecallQuery(search); if (recallQuery === search) void recall.refetch() }}><Search className="lucide-inline" />{t('memoryV2.recall')}</Btn>}
      </div>
      <p className="text-[12px] leading-relaxed text-muted">{t('memoryV2.memory_kinds_explanation')}</p>
    </div>
    {/* No hand-off: cross-page selection and editing drafts must stay attached to this store. */}
    <ErrorNotice message={errorText(records.error || (recallCurrent ? recall.error : null))} />
    {queryReadFailed(records) && <div className="flex flex-wrap gap-2"><Btn className="min-h-11 aria-disabled:opacity-30 aria-disabled:cursor-default" aria-disabled={records.isFetching || undefined} onClick={() => { if (!records.isFetching) void records.refetch() }}>{t('memoryV2.retry_read')}</Btn>{onRecovery && <Btn className="min-h-11" onClick={onRecovery}>{t('memoryV2.tab_recovery')}</Btn>}</div>}
    {recallCurrent && recall.data && <RecallEvidence data={recall.data} />}
    <motion.div layout={!reduceMotion} className="rounded-xl border border-border bg-bg-elevated p-3" aria-live="polite">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {rows.length > 0 && <div><label className="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]"><input aria-label={t('memoryEditing.select_page')} type="checkbox" className="h-4 w-4 accent-accent" checked={rows.every(isSelected)} disabled={!!all && all.exclude.size >= MANUAL_LIMIT && rows.every(isSelected)} onChange={choosePage} />{t('memoryEditing.select_page')}</label></div>}
        {selectedCount > 0 || all ? <><Badge variant="muted">{t('memoryEditing.selected', { count: fmtNumber(selectedCount) })}</Badge><Btn className="min-h-11" onClick={clearSelection}>{t('memoryEditing.clear_selection')}</Btn></> : !searching && !records.error && <span className="text-[12px] text-muted">{t('memoryEditing.matching', { count: fmtNumber(currentTotal) })}</span>}
      </div>
      {!all && selectedCount > 0 && currentTotal > rows.length && <Btn className="min-h-11 max-w-full whitespace-normal text-left" disabled={searching || !!records.error || currentTotal > 10000} onClick={() => picked.selectAll(query, currentTotal)}>{t('memoryEditing.select_all', { count: fmtNumber(currentTotal) })}</Btn>}
      {(selectedCount > 0 || all) && <div className="mt-2 flex flex-wrap gap-2"><Btn primary className="min-h-11" disabled={!selectedCount} onClick={() => begin({ type: 'replace_text' })}><Replace className="lucide-inline" />{t('memoryEditing.replace')}</Btn><Btn danger className="min-h-11" disabled={!selectedCount} onClick={() => begin({ type: 'forget' })}><Trash2 className="lucide-inline" />{t('memoryEditing.forget_count', { count: fmtNumber(selectedCount) })}</Btn></div>}
      {!selectedCount && !all && rows.length > 0 && <p className="text-[12px] text-muted">{t('memoryV2.selection_hint')}</p>}
      {all && <p className="text-[12px] text-muted">{t('memoryEditing.all_scope', { query: all.query.q || t('memoryV2.filter_all') })}</p>}
      {!all && selectedCount >= MANUAL_LIMIT && <p role="status" className="text-[12px] text-muted">{t('memoryEditing.selection_limit')}</p>}
      {currentTotal > 10000 && selectedCount > 0 && <p className="text-[12px] text-muted">{t('memoryEditing.narrow_selection')}</p>}
    </motion.div>
    {searching && <div role="status" aria-label={t('pages.overview.memoryTab.loading')} className="grid gap-3 lg:grid-cols-2">{[0, 1, 2, 3].map(index => <Skeleton key={index} className="h-36 rounded-xl motion-reduce:animate-none" />)}</div>}
    {!searching && !records.error && !rows.length && <Card className="mb-0 py-6"><EmptyState icon={<Search className="lucide-inline" />} title={search || kind !== 'all' ? t('memoryV2.no_matches') : t(privateMemory ? 'memoryV2.empty_title' : 'memoryV2.store_empty_title')} subtitle={search || kind !== 'all' ? t('memoryV2.no_matches_hint') : t(privateMemory ? 'memoryV2.empty_body' : 'memoryV2.store_empty_body')} />{!search && kind === 'all' && <div className="mt-3 flex flex-wrap justify-center gap-2">{emptyActions}</div>}</Card>}
    <div className="grid min-w-0 grid-cols-1 gap-3 lg:grid-cols-2">{rows.map(row => { const Icon = MEMORY_RECORD_ICONS[row.kind]; const chosen = isSelected(row); const isFocus = row === focused; return <motion.div layout={!reduceMotion} key={recordIdentity(row)} initial={false} className={`min-w-0${isFocus ? ' rounded-xl ring-2 ring-accent' : ''}`} ref={isFocus ? focusRow : undefined} data-memory-focus={isFocus || undefined}>
      <Card className={`mb-0 flex h-full min-w-0 flex-col py-4 transition-colors ${chosen ? 'border-accent bg-accent-subtle' : 'hover:border-border-strong'}`}>
        <div className="flex min-w-0 items-center gap-2.5"><input aria-label={t('memoryEditing.select_record', { text: memoryRecordBody(row) })} type="checkbox" className="h-4 w-4 shrink-0 accent-accent" checked={chosen} disabled={picked.lockedRow(row)} onChange={() => toggle(row)} /><span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-lg ${TINTS[row.kind]}`}><Icon className="lucide-inline" /></span><div className="min-w-0"><span className="block text-[12px] font-medium">{t(MEMORY_RECORD_LABELS[row.kind])}</span>{!privateMemory && row.key && <span className="block truncate text-[11px] text-muted" title={row.key}>{row.key}</span>}</div></div>
        <p className="my-3 line-clamp-3 whitespace-pre-wrap break-words text-sm leading-relaxed [overflow-wrap:anywhere]">{memoryRecordBody(row)}</p>
        {!!row.metadata?.email_addresses?.length && <div className="mb-3 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted">{row.metadata?.email_addresses.slice(0, 3).map(email => <span key={email} className="inline-flex min-w-0 max-w-full items-start gap-1"><Mail className="lucide-inline shrink-0" aria-hidden="true" /><span className="break-all">{email}</span></span>)}</div>}
        <RecordSource row={row} compact />{(row.metadata?.pending_conflicts || 0) > 0 && <Btn className="mt-2 min-h-11" onClick={() => setDetail(row)}><History className="lucide-inline" />{t('memoryEditing.review_proposals', { count: fmtNumber(row.metadata!.pending_conflicts!) })}</Btn>}
        <div className="mt-auto flex justify-between gap-2 border-t border-border pt-3"><Btn className="min-h-11" onClick={() => setDetail(row)}>{t('memoryV2.view_details')}<ArrowUpRight className="lucide-inline" /></Btn><Btn className="min-h-11" disabled={!row.revision} onClick={() => begin({ type: 'set', row })}><Pencil className="lucide-inline" />{editAction}</Btn></div>
      </Card>
    </motion.div> })}</div>
    {!searching && !records.error && currentTotal > 0 && <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[12px] text-muted">{t('memoryEditing.page_range', { from: fmtNumber(page * PAGE_SIZE + 1), to: fmtNumber(page * PAGE_SIZE + rows.length), total: fmtNumber(currentTotal) })}</span><div className="flex gap-2"><Btn ref={pager.prevRef} aria-label={t('memoryEditing.previous')} className="min-h-11 min-w-11 justify-center" disabled={page === 0} onClick={() => { setPage(value => value - 1); pager.pressedPrev() }}><ChevronLeft className="lucide-inline" /></Btn><Btn ref={pager.nextRef} aria-label={t('memoryEditing.next')} className="min-h-11 min-w-11 justify-center" disabled={!records.data?.has_more} onClick={() => { setPage(value => value + 1); pager.pressedNext() }}><ChevronRight className="lucide-inline" /></Btn></div></div>}
    {detail && <Modal open title={t('memoryV2.detail_title')} onClose={() => setDetail(null)} footer={<div className="flex flex-wrap gap-2"><Btn className="min-h-11" onClick={() => begin({ type: 'set', row: detail })}>{editAction}</Btn><Btn danger className="min-h-11" onClick={() => begin({ type: 'forget', row: detail })}>{t('memoryV2.forget')}</Btn></div>}><div className="space-y-4"><Badge variant="muted">{t(MEMORY_RECORD_LABELS[detail.kind])}</Badge><p className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">{memoryRecordBody(detail)}</p>{privateMemory && detail.key && <dl className="space-y-1 text-[12px]"><dt className="text-muted">{t('memoryV2.memory_key')}</dt><dd className="break-words [overflow-wrap:anywhere]"><code>{detail.key}</code></dd></dl>}<PanelSectionHeader label={t('memoryV2.provenance')} /><RecordProvenance row={detail} /><RecordHistory store={store} row={detail} onRestore={revisionId => edit.restore(detail, revisionId)} onKeepCurrent={() => edit.keepCurrent(detail)} onProposal={(proposal, operation) => edit.takeProposal(detail, proposal, operation)} /></div></Modal>}
    {edit.dialog}
  </div>
}
