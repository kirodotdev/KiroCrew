/**
 * The memory page's Memories tab for one store: one list of facts, lessons and
 * experiences with search, kind pills, sort, live facet filters, a detail panel,
 * multi-select edits and Test recall.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ChevronLeft, ChevronRight, ListChecks, Replace, Search, SlidersHorizontal, Sparkles, Trash2, X } from 'lucide-react'
import { api } from '../../../api/client'
import { Btn, Card, EmptyState, IconButton, Input, Skeleton } from '../../../components/ui'
import { PanelLeftLight } from '../../../components/icons/panels'
import { savePrefExplicitly } from '../../../lib/uiPrefs'
import { safeGetItem } from '../../../utils/safeStorage'
import ErrorNotice from '../../../components/ErrorNotice'
import Modal from '../../../components/Modal'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { usePagerFocus } from '../../../hooks/usePagerFocus'
import { fmtNumber } from '../../../i18n/format'
import { queryReadFailed } from '../../../utils/queryReadFailed'
import type { MemoryRecordFacetGroup, MemoryRecordQuery, MemoryRecordSort } from '../../../types/memoryEditing'
import { MEMORY_QUERY_PREFIXES, memoryQueryRetry } from '../MemoryStoreCard'
import { errorText, recordIdentity } from '../memoryRecordParts'
import { MANUAL_LIMIT, useMemoryRecordEdit, useMemoryRecordSelection } from '../useMemoryRecordEdit'
import { activeFilterCount, effectiveSort, filtersForKind, loadView, saveView, toggleFilter, type KindTab, type ListView } from './memoryListView'
import { ActiveFilters, FilterPanel, KindPills, RecordRow, SortControl } from './MemoryListParts'
import { MemoryRecordDetail } from './MemoryRecordDetail'
import { MemoryRecallDialog } from './MemoryRecallDialog'

const PAGE_SIZE = 50
/** '1' while the wide filter rail is hidden; absent (the default) shows it. */
export const FILTERS_COLLAPSED_KEY = 'kirocrew:memory-filters-collapsed'
const SELECT_ALL_LIMIT = 10000

export default function MemoryList({ store, privateMemory, focusKey, focusLessons = false, selecting, onSelectingChange, recallOpen, onRecallOpenChange, onDirtyChange, onRecovery, emptyActions, storeName }: {
  store: string; privateMemory: boolean
  /** A saved lesson landing (`?lesson=`), searched by key without replacing the saved view. */
  focusKey?: string
  /** `?focus=lessons`: open on the Lessons pill. */
  focusLessons?: boolean
  selecting: boolean; onSelectingChange: (on: boolean) => void
  recallOpen: boolean; onRecallOpenChange: (open: boolean) => void
  onDirtyChange?: (dirty: boolean) => void
  onRecovery?: () => void
  emptyActions?: ReactNode
  /** How the store is named in a sentence ("reviewer", "Global memory"). */
  storeName: string
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const phone = useIsMobile()
  const [saved] = useState(() => loadView(store))
  const [view, setView] = useState<ListView>(() => focusKey ? { ...saved, q: focusKey, kind: 'directive', filters: {} } : focusLessons ? { ...saved, kind: 'directive' } : saved)
  const [input, setInput] = useState(view.q)
  const landing = !!focusKey && view.q === focusKey && view.kind === 'directive'
  useEffect(() => { if (!landing) saveView(store, view) }, [store, view, landing])
  useEffect(() => {
    if (input.trim() === view.q) return
    const timer = setTimeout(() => { setView(old => ({ ...old, q: input.trim(), sort: input.trim() && !old.q ? 'match' : old.sort })); setPage(0) }, 250)
    return () => clearTimeout(timer)
  }, [input, view.q])
  const [page, setPage] = useState(0)
  const pager = usePagerFocus()
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [railOpen, setRailOpenState] = useState(() => safeGetItem(FILTERS_COLLAPSED_KEY) !== '1')
  const setRailOpen = (open: boolean) => { setRailOpenState(open); savePrefExplicitly(FILTERS_COLLAPSED_KEY, open ? '0' : '1') }
  const [detailKey, setDetailKey] = useState<string | null>(null)
  const picked = useMemoryRecordSelection()
  const { all, isSelected } = picked
  const sort = effectiveSort(view)
  const query = useMemo<MemoryRecordQuery>(() => {
    const filters = filtersForKind(view.filters, view.kind)
    return activeFilterCount(filters) ? { q: view.q, kind: view.kind, filters } : { q: view.q, kind: view.kind }
  }, [view.q, view.kind, view.filters])
  const records = useQuery({
    queryKey: ['memory-records', store, query, page, sort, view.order],
    queryFn: () => api.memoryRecords(store, query, page * PAGE_SIZE, PAGE_SIZE, { sort, order: view.order, facets: true }),
    retry: memoryQueryRetry,
    placeholderData: previous => previous,
  })
  // The previous answer stays only for its facet counts, so the pills and filters do not
  // jump while a new view loads; its rows belong to another view and are not shown.
  const searching = input.trim() !== view.q || records.isPending || records.isPlaceholderData
  const rows = searching || records.error ? [] : records.data?.entries || []
  const facets = records.data?.facets
  const total = records.data?.total || 0
  useEffect(() => { if (records.data && page > 0 && page * PAGE_SIZE >= records.data.total) setPage(Math.max(0, Math.ceil(records.data.total / PAGE_SIZE) - 1)) }, [records.data, page])
  const detail = rows.find(row => recordIdentity(row) === detailKey) ?? null
  const focused = landing ? rows.find(row => row.id === focusKey) : undefined
  const focusRow = useRef<HTMLLIElement>(null)
  const focusScrolled = useRef(false)
  useEffect(() => {
    if (!focused || !focusRow.current || focusScrolled.current) return
    focusRow.current.scrollIntoView({ block: 'center' })
    focusScrolled.current = true
  }, [focused])

  const selectedCount = picked.count
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ['memory-records', store] })
    void client.invalidateQueries({ queryKey: ['member-memory', store] })
    void client.invalidateQueries({ queryKey: ['memory-record-history', store] })
    for (const prefix of MEMORY_QUERY_PREFIXES) void client.invalidateQueries({ queryKey: prefix })
  }
  const [feedback, setFeedback] = useState('')
  const edit = useMemoryRecordEdit({
    store, refresh,
    onApplied: changed => { setFeedback(t('memoryEditing.applied', { count: fmtNumber(changed) })); setPage(0); onSelectingChange(false); refresh() },
    selection: picked.forEdit,
  })
  const dirty = !!edit.intent || !!all || selectedCount > 0
  useEffect(() => { onDirtyChange?.(dirty); return () => onDirtyChange?.(false) }, [dirty, onDirtyChange])
  // Leaving select mode drops the picks.
  const stopSelecting = () => { picked.clear(); onSelectingChange(false) }
  // Hand-picked rows belong to the list they were picked in: a kind, filter or
  // search change drops them rather than acting on rows the new list hides.
  // ("Select all matching" locks those controls instead, below.)
  const clearPicks = picked.clear
  useEffect(() => { clearPicks() }, [query, clearPicks])

  // A filter, search or kind change while rows are picked would hide them; the
  // selection belongs to the list it was made in.
  const locked = !!all
  const change = (next: Partial<ListView>) => { if (locked) return; setView(old => ({ ...old, ...next })); setPage(0) }
  const onToggleFilter = (group: MemoryRecordFacetGroup, value: string) => change({ filters: toggleFilter(view.filters, group, value) })
  const onRange = (bound: 'conf_min' | 'conf_max', value: number | undefined) => setView(old => { if (locked) return old; const filters = { ...old.filters }; if (value === undefined) delete filters[bound]; else filters[bound] = value; return { ...old, filters } })
  const clearFilters = () => change({ filters: {} })
  const clearSearch = () => { setInput(''); change({ q: '', sort: view.sort === 'match' ? 'updated' : view.sort }) }
  const narrowed = !!view.q || view.kind !== 'all' || activeFilterCount(view.filters) > 0
  const filterCount = activeFilterCount(filtersForKind(view.filters, view.kind))
  const filterPanel = <FilterPanel view={view} facets={facets} onToggle={onToggleFilter} onRange={onRange} />

  // Under a score sort, rows with no score come last, newest first; a divider marks where.
  const scoreSort = sort === 'confidence' || sort === 'importance'
  const firstUnscored = scoreSort ? rows.findIndex(row => typeof row.importance !== 'number' || (sort === 'confidence') === (row.kind === 'episode')) : -1
  const toolbar = <div className="space-y-3">
    {selecting ? <div className="flex min-h-[52px] flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-card px-3 py-2" role="toolbar" aria-label={t('memoryPage.selection_toolbar')}>
      {rows.length > 0 && <label className="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]"><input type="checkbox" className="h-4 w-4 accent-accent" aria-label={t('memoryEditing.select_page')} checked={rows.every(isSelected)} disabled={!!all && all.exclude.size >= MANUAL_LIMIT && rows.every(isSelected)} onChange={() => picked.choosePage(rows)} />{t('memoryEditing.select_page')}</label>}
      <span className="text-[13px] font-medium tabular-nums text-text" role="status">{t('memoryEditing.selected', { count: fmtNumber(selectedCount) })}</span>
      {!all && selectedCount > 0 && total > rows.length && <Btn className="min-h-11 border-transparent text-accent" disabled={searching || !!records.error || total > SELECT_ALL_LIMIT} onClick={() => picked.selectAll(query, total)}>{t('memoryEditing.select_all', { count: fmtNumber(total) })}</Btn>}
      {!all && <span className="text-[12px] text-muted">{t('memoryPage.select_limit_hint', { limit: fmtNumber(MANUAL_LIMIT) })}</span>}
      <div className="ml-auto flex flex-wrap gap-2">
        <Btn className="min-h-11" disabled={!selectedCount} onClick={() => edit.begin({ type: 'replace_text' })}><Replace className="lucide-inline" />{t('memoryEditing.replace')}</Btn>
        <Btn danger className="min-h-11" disabled={!selectedCount} onClick={() => edit.begin({ type: 'forget' })}><Trash2 className="lucide-inline" />{t('memoryEditing.forget_count', { count: fmtNumber(selectedCount) })}</Btn>
        <Btn className="min-h-11" onClick={stopSelecting}>{t('pages.overview.memoryTab.cancel')}</Btn>
      </div>
      {all && <p className="basis-full text-[12px] text-muted">{t('memoryEditing.all_scope', { query: all.query.q || t('memoryV2.filter_all') })}</p>}
      {!all && selectedCount >= MANUAL_LIMIT && <p role="status" className="basis-full text-[12px] text-muted">{t('memoryEditing.selection_limit')}</p>}
      {total > SELECT_ALL_LIMIT && selectedCount > 0 && <p className="basis-full text-[12px] text-muted">{t('memoryEditing.narrow_selection')}</p>}
    </div> : <div className="flex min-w-0 flex-wrap gap-2" data-testid="memory-toolbar">
      {/* A phone gives the search box its own row; the filter and sort buttons wrap below it.
          Keyed on the phone layout, not on a width class: a 640-767 px phone would
          otherwise keep every control on one row and squeeze the search box. A wide
          screen keeps one row until the search box would drop below 20rem (an open
          detail panel), then wraps the buttons instead of squeezing it. */}
      <label className={`relative flex-1 ${phone ? 'min-w-0 basis-full' : 'min-w-[20rem]'}`}>
        <Search className="lucide-inline pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
        <Input className="min-h-11 w-full pl-9 pr-10 sm:min-h-10" aria-label={t('memoryV2.search')} placeholder={t('memoryEditing.search_hint')} value={input} onChange={event => setInput(event.target.value)} />
        {input && <IconButton aria-label={t('memoryV2.clear_search')} className="absolute right-1 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center" onClick={clearSearch}><X className="lucide-inline" aria-hidden="true" /></IconButton>}
      </label>
      {/* A phone opens the filter sheet; a wide screen with the rail hidden shows it again. */}
      {(phone || !railOpen) && <Btn aria-label={t('memoryPage.filters')} aria-expanded={phone ? undefined : false} className="relative min-h-11 min-w-11 shrink-0 justify-center sm:min-h-10 sm:min-w-10" onClick={() => phone ? setFiltersOpen(true) : setRailOpen(true)}><SlidersHorizontal className="lucide-inline" aria-hidden="true" />{filterCount > 0 && <span className="absolute -right-1.5 -top-1.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent px-1 text-[11px] text-accent-fg">{fmtNumber(filterCount)}</span>}</Btn>}
      <SortControl view={view} onChange={(next: MemoryRecordSort, order) => change({ sort: next, order })} />
      {!phone && <Btn className="min-h-10 shrink-0 whitespace-nowrap" onClick={() => onSelectingChange(true)}><ListChecks className="lucide-inline" />{t('memoryPage.select')}</Btn>}
      {!phone && privateMemory && <Btn className="min-h-10 shrink-0 whitespace-nowrap" onClick={() => onRecallOpenChange(true)}><Sparkles className="lucide-inline" />{t('memoryPage.test_recall')}</Btn>}
    </div>}
    <KindPills kind={view.kind} counts={facets?.kind} disabled={locked} onChange={(kind: KindTab) => change({ kind })} />
    <ActiveFilters filters={filtersForKind(view.filters, view.kind)} onToggle={onToggleFilter} onRange={onRange} onClear={clearFilters} />
  </div>

  const list = <div className="space-y-3">
    {feedback && <div role="status" className="flex items-center gap-2 rounded-lg bg-ok-subtle px-3 py-2 text-[13px] text-ok-text">{feedback}<IconButton aria-label={t('components.errorNotice.dismiss')} className="ml-auto flex h-8 w-8 items-center justify-center" onClick={() => setFeedback('')}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>}
    {/* No hand-off: cross-page selection and editing drafts must stay attached to this store. */}
    <ErrorNotice title={t('memoryPage.read_failed', { store: storeName })} message={errorText(records.error)} />
    {queryReadFailed(records) && <div className="flex flex-wrap gap-2"><Btn className="min-h-11 aria-disabled:cursor-default aria-disabled:opacity-30" aria-disabled={records.isFetching || undefined} onClick={() => { if (!records.isFetching) void records.refetch() }}>{t('memoryV2.retry_read')}</Btn>{onRecovery && <Btn className="min-h-11" onClick={onRecovery}>{t('memoryPage.open_recovery')}</Btn>}</div>}
    {searching && !records.error && <ul role="status" aria-label={t('pages.overview.memoryTab.loading')} className="m-0 list-none divide-y divide-border rounded-2xl border border-border bg-card p-0">{[0, 1, 2].map(index => <li key={index} className="space-y-2 px-4 py-4"><Skeleton className="h-3.5 w-4/5 motion-reduce:animate-none" /><Skeleton className="h-4 w-1/2 motion-reduce:animate-none" /></li>)}</ul>}
    {!searching && !records.error && !rows.length && <Card className="mb-0 py-6">
      <EmptyState icon={<Search className="lucide-inline" />} title={narrowed ? t('memoryV2.no_matches') : t(privateMemory ? 'memoryV2.empty_title' : 'memoryV2.store_empty_title')} subtitle={narrowed ? t('memoryV2.no_matches_hint') : t(privateMemory ? 'memoryV2.empty_body' : 'memoryV2.store_empty_body')} />
      <div className="mt-3 flex flex-wrap justify-center gap-2">{narrowed ? <>{view.q && <Btn className="min-h-11" onClick={clearSearch}>{t('memoryV2.clear_search')}</Btn>}{activeFilterCount(view.filters) > 0 && <Btn className="min-h-11" onClick={clearFilters}>{t('memoryPage.clear_all')}</Btn>}</> : emptyActions}</div>
    </Card>}
    {rows.length > 0 && <ul className="m-0 list-none overflow-hidden rounded-2xl border border-border bg-card p-0" aria-live="polite" aria-label={t('memoryV2.tab_memories')}>
      {rows.map((row, index) => [
        index === firstUnscored && index > 0 && view.kind === 'all' ? <li key="divider" className="flex h-9 items-center border-b border-border bg-bg-hover px-4 text-[12px] text-muted">{t(sort === 'confidence' ? 'memoryPage.divider_no_confidence' : 'memoryPage.divider_no_importance')}</li> : null,
        <RecordRow key={recordIdentity(row)} row={row} sort={sort} selecting={selecting} selected={isSelected(row)} chip={locked || selecting ? undefined : { filters: view.filters, onToggle: onToggleFilter }}
          selectDisabled={picked.lockedRow(row)}
          open={detailKey === recordIdentity(row)} focus={row === focused} focusRef={row === focused ? focusRow : undefined}
          onSelect={() => picked.toggle(row)} onOpen={() => setDetailKey(recordIdentity(row))}
          onEdit={() => edit.begin({ type: 'set', row })} onForget={() => edit.begin({ type: 'forget', row })} />,
      ])}
    </ul>}
    {!searching && !records.error && total > PAGE_SIZE && <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-[12px] tabular-nums text-muted">{t('memoryEditing.page_range', { from: fmtNumber(page * PAGE_SIZE + 1), to: fmtNumber(page * PAGE_SIZE + rows.length), total: fmtNumber(total) })}</span><div className="flex gap-2"><Btn ref={pager.prevRef} aria-label={t('memoryEditing.previous')} className="min-h-11 min-w-11 justify-center sm:min-h-8 sm:min-w-8" disabled={page === 0} onClick={() => { setPage(value => value - 1); pager.pressedPrev() }}><ChevronLeft className="lucide-inline" /></Btn><Btn ref={pager.nextRef} aria-label={t('memoryEditing.next')} className="min-h-11 min-w-11 justify-center sm:min-h-8 sm:min-w-8" disabled={!records.data?.has_more} onClick={() => { setPage(value => value + 1); pager.pressedNext() }}><ChevronRight className="lucide-inline" /></Btn></div></div>}
  </div>

  const detailPanel = detail && <MemoryRecordDetail store={store} row={detail} phone={phone} onClose={() => setDetailKey(null)}
    onEdit={() => { setDetailKey(null); edit.begin({ type: 'set', row: detail }) }} onForget={() => { setDetailKey(null); edit.begin({ type: 'forget', row: detail }) }}
    onRestore={revisionId => { setDetailKey(null); edit.restore(detail, revisionId) }} onKeepCurrent={() => { setDetailKey(null); edit.keepCurrent(detail) }}
    onProposal={(proposal, operation) => { setDetailKey(null); edit.takeProposal(detail, proposal, operation) }} />

  return <div className="min-w-0" data-testid="memory-list">
    {phone ? <div className="space-y-4">{toolbar}{list}{detailPanel}</div> : <div className="flex gap-6">
      {railOpen && <aside className="w-60 shrink-0 pt-1" aria-label={t('memoryPage.filters')}>
        <FilterPanel view={view} facets={facets} onToggle={onToggleFilter} onRange={onRange}
          trailing={<IconButton aria-label={t('memoryPage.hide_filters')} title={t('memoryPage.hide_filters')} aria-expanded className="pi-morph -mr-2.5 flex h-9 w-9 shrink-0 items-center justify-center text-muted" onClick={() => setRailOpen(false)}><PanelLeftLight size={16} aria-hidden="true" /></IconButton>} />
      </aside>}
      <div className="min-w-0 flex-1 space-y-4">{toolbar}{list}</div>
      {detailPanel}
    </div>}
    {phone && filtersOpen && <Modal open title={t('memoryPage.filters')} onClose={() => setFiltersOpen(false)} headerActions={activeFilterCount(view.filters) > 0 ? <Btn className="min-h-11 border-transparent" onClick={clearFilters}>{t('memoryPage.clear_all')}</Btn> : undefined} footer={<Btn primary className="min-h-12 w-full justify-center" onClick={() => setFiltersOpen(false)}>{t('memoryPage.show_results')}</Btn>}>{filterPanel}</Modal>}
    {recallOpen && <MemoryRecallDialog store={store} initialTask={input.trim()} onClose={() => onRecallOpenChange(false)} />}
    {edit.dialog}
  </div>
}
