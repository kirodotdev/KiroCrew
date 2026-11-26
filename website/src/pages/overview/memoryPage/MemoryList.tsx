/**
 * The memory page's Memories tab for one store: one list of facts, lessons and
 * experiences with search, kind pills, sort, live facet filters, a detail panel,
 * multi-select edits.
 *
 * The server does every filter, sort, search and count and sends one page at a
 * time; the next page loads as the end of the list scrolls near.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ListChecks, Replace, Search, SlidersHorizontal, Trash2, X } from 'lucide-react'
import { api } from '../../../api/client'
import { Btn, Card, EmptyState, IconButton, Input, Skeleton } from '../../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { PanelLeftLight } from '../../../components/icons/panels'
import { savePrefExplicitly } from '../../../lib/uiPrefs'
import { safeGetItem } from '../../../utils/safeStorage'
import ErrorNotice from '../../../components/ErrorNotice'
import type { MemoryJump } from './memoryJump'
import { FilterRail, RAIL_MAX, RAIL_MIN } from './MemoryRail'
import { DateRangeButton } from './MemoryDateRange'
import { dayDate, rangeBounds, useLocalDay, type DateRange } from './memoryDateRanges'
import { MemoryDetailSlide } from './MemoryDetailSlide'
import Modal from '../../../components/Modal'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { fmtNumber } from '../../../i18n/format'
import { queryReadFailed } from '../../../utils/queryReadFailed'
import type { MemoryRecordFacetGroup, MemoryRecordQuery, MemoryRecordSort } from '../../../types/memoryEditing'
import { MEMORY_QUERY_PREFIXES, memoryQueryRetry } from '../MemoryStoreCard'
import { errorText, recordIdentity } from '../memoryRecordParts'
import { MANUAL_LIMIT, useMemoryRecordEdit, useMemoryRecordSelection } from '../useMemoryRecordEdit'
import { activeFilterCount, byMeaning, effectiveSort, filtersForKind, loadView, saveView, toggleFilter, type KindTab, type ListView, type SearchMode } from './memoryListView'
import { ActiveFilters, FilterPanel, KindPills, RecordRow, SavedTopics, SearchModeSwitch, SortControl } from './MemoryListParts'
import { useMemoryTopics } from './memoryTopics'
import { firstOfEach } from './memoryPaging'
import { MemoryRecordDetail } from './MemoryRecordDetail'

const PAGE_SIZE = 50
// The next page starts loading while the list's end is this far below the view.
const LOAD_AHEAD = '800px 0px'
/** '1' while the wide filter rail is hidden; absent (the default) shows it. */
export const FILTERS_COLLAPSED_KEY = 'kirocrew:memory-filters-collapsed'
export const FILTERS_WIDTH_KEY = 'kirocrew:memory-filters-width'
// The wide rail's width range, dragged at its edge and remembered per browser.
const RAIL_DEFAULT = 240
// Narrowest list column the rail may leave: a content area thinner than the rail
// plus this hides the rail, and its Filters button opens the filter sheet instead.
export const LIST_MIN = 480
// Narrowest toolbar that keeps the mode switch inside the search box (32rem).
export const SWITCH_INLINE_MIN = 512
const clampRail = (width: number) => Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(width)))
const SELECT_ALL_LIMIT = 10000

export default function MemoryList({ jump, store, botName, privateMemory, focusKey, focusLessons = false, selecting, onSelectingChange, onDirtyChange, onRecovery, onSaveAsLesson, emptyActions, storeName }: {
  /** Copy an experience into a new lesson, which never fades: the global page only. */
  onSaveAsLesson?: (rule: string) => void
  store: string; privateMemory: boolean
  /** The bot this store belongs to, named in the (?) tips; none on the global page. */
  botName?: string
  /** A saved lesson landing (`?lesson=`), searched by key without replacing the saved view. */
  focusKey?: string
  /** `?focus=lessons`: open on the Lessons pill. */
  focusLessons?: boolean
  selecting: boolean; onSelectingChange: (on: boolean) => void
  onDirtyChange?: (dirty: boolean) => void
  onRecovery?: () => void
  emptyActions?: ReactNode
  /** The Graph tab's hand-off: list exactly these records, and open one. */
  jump?: MemoryJump | null
  /** How the store is named in a sentence ("reviewer", "Global memory"). */
  storeName: string
}) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const phone = useIsMobile()
  const [saved] = useState(() => loadView(store))
  // A landing reads its target by words over every date, so a saved meaning search or
  // date range cannot hide it; the saved view is kept until the user changes this one.
  const [view, setView] = useState<ListView>(() => focusKey ? { ...saved, q: focusKey, kind: 'directive', filters: {}, mode: 'words', range: undefined } : focusLessons ? { ...saved, kind: 'directive', mode: 'words', range: undefined } : saved)
  const firstView = useRef(view).current
  const [input, setInput] = useState(view.q)
  const landing = (!!focusKey && view.q === focusKey && view.kind === 'directive') || (focusLessons && view === firstView)
  useEffect(() => { if (!landing) saveView(store, view) }, [store, view, landing])
  useEffect(() => {
    if (input.trim() === view.q) return
    const timer = setTimeout(() => setView(old => ({ ...old, q: input.trim(), sort: input.trim() && !old.q ? 'match' : old.sort })), 250)
    return () => clearTimeout(timer)
  }, [input, view.q])
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [railOpen, setRailOpenState] = useState(() => safeGetItem(FILTERS_COLLAPSED_KEY) !== '1')
  const setRailOpen = (open: boolean) => { setRailOpenState(open); savePrefExplicitly(FILTERS_COLLAPSED_KEY, open ? '0' : '1') }
  const [railWidth, setRailWidth] = useState(() => { const saved = Number(safeGetItem(FILTERS_WIDTH_KEY)); return saved ? clampRail(saved) : RAIL_DEFAULT })
  const resizeRail = (width: number, save = false) => { const next = clampRail(width); setRailWidth(next); if (save) savePrefExplicitly(FILTERS_WIDTH_KEY, String(next)) }
  // The content area's own width, not the window's: an open Settings rail narrows it.
  const rootRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(root)
    return () => observer.disconnect()
  }, [])
  const compact = !phone && width > 0 && width < railWidth + 24 + LIST_MIN
  // The mode switch sits inside the search box only while the toolbar is wide enough
  // for it beside the typed text; otherwise it takes a row of its own below the box.
  const toolbarRef = useRef<HTMLDivElement>(null)
  const [toolbarWidth, setToolbarWidth] = useState(0)
  useLayoutEffect(() => {
    const bar = toolbarRef.current
    if (!bar || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => setToolbarWidth(entry.contentRect.width))
    observer.observe(bar)
    return () => observer.disconnect()
  }, [selecting])
  const inlineSwitch = !phone && (toolbarWidth === 0 || toolbarWidth >= SWITCH_INLINE_MIN)
  // A side detail takes 384px including its gap; keep at least 320px for the list.
  const stacked = phone || (width > 0 && width - (!compact && railOpen ? railWidth + 24 : 0) < 320 + 384)
  const sheet = phone || compact || stacked
  const [detailKey, setDetailKey] = useState<string | null>(null)
  const picked = useMemoryRecordSelection()
  const { all, isSelected } = picked
  const sort = effectiveSort(view)
  const meaning = byMeaning(view)
  // Rows under the meaning cut-off stay counted, not listed, until "Show them".
  const [weak, setWeak] = useState(false)
  const topics = useMemoryTopics()
  // The range filters on the time the list is sorted by: created under that sort, else updated.
  const dateField = view.sort === 'created' ? 'created' : 'updated'
  // A quick range counts back from today, so it moves on at local midnight.
  const today = useLocalDay()
  const query = useMemo<MemoryRecordQuery>(() => {
    const bounds = rangeBounds(view.range, dayDate(today) ?? undefined)
    const filters = { ...filtersForKind(view.filters, view.kind), ...(bounds ? { ...bounds, ...(dateField === 'created' ? { date_field: 'created' as const } : {}) } : {}) }
    return activeFilterCount(filters) || bounds ? { q: view.q, kind: view.kind, filters } : { q: view.q, kind: view.kind }
  }, [view.q, view.kind, view.filters, view.range, dateField, today])
  const records = useInfiniteQuery({
    queryKey: ['memory-records', store, query, sort, view.order, meaning, meaning && weak],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.memoryRecords(store, query, pageParam, PAGE_SIZE, { sort, order: sort === 'match' ? 'desc' : view.order, ...(meaning ? { meaning, weak } : {}) }),
    getNextPageParam: (last, pages) => last.has_more ? pages.length * PAGE_SIZE : undefined,
    retry: memoryQueryRetry,
    placeholderData: previous => previous,
  })
  // The counts load on their own, so the first rows never wait for them. The
  // previous counts stay while new ones load, so the pills and filters do not jump.
  const counts = useQuery({
    queryKey: ['memory-records', store, query, 'facets', meaning, meaning && weak],
    queryFn: () => api.memoryRecords(store, query, 0, 1, { sort: meaning ? 'match' : 'updated', order: 'desc', facets: true, ...(meaning ? { meaning, weak } : {}) }),
    retry: memoryQueryRetry,
    placeholderData: previous => previous,
  })
  // A previous answer's rows belong to another view and are not shown.
  const searching = input.trim() !== view.q || records.isPending || records.isPlaceholderData
  const pages = records.data?.pages
  // A write between two page reads can shift a row onto the next page too; it is shown once.
  const rows = useMemo(() => searching || records.error ? [] : firstOfEach((pages || []).flatMap(page => page.entries), recordIdentity), [searching, records.error, pages])
  const facets = counts.data?.facets
  // Only a store whose recall fades experiences sends this.
  const storeDecay = counts.data?.decay
  const total = pages?.[0]?.total || 0
  const weaker = meaning && !weak ? pages?.[pages.length - 1]?.hidden || 0 : 0
  // A new search or list shows only the strong matches again.
  useEffect(() => { setWeak(false) }, [query, meaning])
  // The next page loads as the end of the list comes near the view.
  const end = useRef<HTMLDivElement>(null)
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = records
  useEffect(() => {
    const node = end.current
    if (!node || !hasNextPage || isFetchingNextPage || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) void fetchNextPage() }, { rootMargin: LOAD_AHEAD })
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, rows.length])
  const detail = rows.find(row => recordIdentity(row) === detailKey) ?? null
  const [jumpFocus, setJumpFocus] = useState<string | null>(null)
  // A selection from the graph replaces the search, kind and filters: it names exact
  // records, which any of those would hide. Filters picked afterwards narrow it.
  useEffect(() => {
    if (!jump) return
    setInput('')
    setView(old => ({ ...old, q: '', kind: 'all', mode: 'words' as const, filters: { ref: jump.refs }, range: undefined }))
    const open = jump.open ? recordIdentity(jump.open) : null
    setDetailKey(open); setJumpFocus(open); focusScrolled.current = false
  }, [jump])
  const focused = jumpFocus ? rows.find(row => recordIdentity(row) === jumpFocus) : landing ? rows.find(row => row.id === focusKey) : undefined
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
    onApplied: changed => { setFeedback(t('memoryEditing.applied', { count: fmtNumber(changed) })); onSelectingChange(false); refresh() },
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
  const change = (next: Partial<ListView>) => { if (locked) return; setView(old => ({ ...old, ...next })) }
  const onToggleFilter = (group: MemoryRecordFacetGroup, value: string) => change({ filters: toggleFilter(view.filters, group, value) })
  const onRange = (bound: 'conf_min' | 'conf_max', value: number | undefined) => setView(old => { if (locked) return old; const filters = { ...old.filters }; if (value === undefined) delete filters[bound]; else filters[bound] = value; return { ...old, filters } })
  const clearFilters = () => change({ filters: {}, range: undefined })
  const setRange = (range?: DateRange) => change({ range })
  const clearSearch = () => { setInput(''); change({ q: '', sort: view.sort === 'match' ? 'updated' : view.sort }) }
  const setMode = (mode: SearchMode) => change({ mode })
  // A saved topic runs at once, as a search by meaning.
  const runTopic = (topic: string) => { setInput(topic); change({ q: topic, mode: 'meaning' }) }
  const meaningUnavailable = (records.error as { status?: number } | null)?.status === 409
  const narrowed = !!view.q || view.kind !== 'all' || activeFilterCount(view.filters) > 0 || !!view.range
  const filterCount = activeFilterCount(filtersForKind(view.filters, view.kind))
  const filterPanel = <FilterPanel bot={botName} view={view} facets={facets} decay={storeDecay} onToggle={onToggleFilter} onRange={onRange} />

  // Under a score sort, rows with no score come last, newest first; a divider marks where.
  const scoreSort = sort === 'confidence' || sort === 'importance' || sort === 'strength'
  const firstUnscored = !scoreSort ? -1 : sort === 'strength' ? rows.findIndex(row => !row.decay)
    : rows.findIndex(row => typeof row.importance !== 'number' || (sort === 'confidence') === (row.kind === 'episode'))
  const DIVIDER_KEYS = { confidence: 'memoryPage.divider_no_confidence', importance: 'memoryPage.divider_no_importance', strength: 'memoryPage.divider_no_strength' } as const
  const toolbar = <div className="space-y-3">
    {selecting ? <div className="flex min-h-[52px] flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-border bg-card px-3 py-2" role="toolbar" aria-label={t('memoryPage.selection_toolbar')}>
      {rows.length > 0 && <label className="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]"><input type="checkbox" className="h-4 w-4 accent-accent" aria-label={t('memoryEditing.select_page')} checked={rows.every(isSelected)} disabled={!!all && all.exclude.size >= MANUAL_LIMIT && rows.every(isSelected)} onChange={() => picked.choosePage(rows)} />{t('memoryEditing.select_page')}</label>}
      <span className="text-[13px] font-medium tabular-nums text-text" role="status">{t('memoryEditing.selected', { count: fmtNumber(selectedCount) })}</span>
      {!all && !meaning && selectedCount > 0 && total > rows.length && <Btn className="min-h-11 border-transparent text-accent" disabled={searching || !!records.error || total > SELECT_ALL_LIMIT} onClick={() => picked.selectAll(query, total)}>{t('memoryEditing.select_all', { count: fmtNumber(total) })}</Btn>}
      {!all && <span className="text-[12px] text-muted">{t('memoryPage.select_limit_hint', { limit: fmtNumber(MANUAL_LIMIT) })}</span>}
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {/* Two controls a row: the bulk edits share one menu, so Forget is never one stray click away. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild><Btn className="min-h-11" disabled={!selectedCount}>{t('memoryV2.edit_action')}<ChevronDown className="lucide-inline text-muted" aria-hidden="true" /></Btn></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => edit.begin({ type: 'replace_text' })}><Replace className="lucide-inline text-muted" aria-hidden="true" />{t('memoryEditing.replace')}</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => edit.begin({ type: 'forget' })}><span className="contents text-danger-text"><Trash2 className="lucide-inline" aria-hidden="true" />{t('memoryEditing.forget_count', { count: fmtNumber(selectedCount) })}</span></DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Btn className="min-h-11" onClick={stopSelecting}>{t('pages.overview.memoryTab.cancel')}</Btn>
      </div>
      {all && <p className="basis-full text-[12px] text-muted">{t('memoryEditing.all_scope', { query: all.query.q || t('memoryV2.filter_all') })}</p>}
      {!all && selectedCount >= MANUAL_LIMIT && <p role="status" className="basis-full text-[12px] text-muted">{t('memoryEditing.selection_limit')}</p>}
      {total > SELECT_ALL_LIMIT && selectedCount > 0 && <p className="basis-full text-[12px] text-muted">{t('memoryEditing.narrow_selection')}</p>}
    </div> : <div ref={toolbarRef} className="@container min-w-0"><div className="flex min-w-0 flex-wrap gap-2" data-testid="memory-toolbar">
      {/* A phone gives the search box its own row; the mode switch, then the filter and sort buttons, wrap below it.
          Keyed on the phone layout, not on a width class: a 640-767 px phone would
          otherwise keep every control on one row and squeeze the search box. A wide
          screen keeps one row until the search box would drop below 22rem (its text
          beside the mode switch), then wraps the buttons instead of squeezing it. */}
      {/* A div, not a label: a label would also name the mode switch's buttons. */}
      <div className={`relative flex-1 ${phone ? 'min-w-0 basis-full' : 'min-w-0 basis-full @[40rem]:min-w-[22rem] @[40rem]:basis-0'}`}>
        <Search className="lucide-inline pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
        <Input className={`min-h-11 w-full pl-9 sm:min-h-10 ${inlineSwitch ? 'pr-[188px]' : 'pr-10'}`} aria-label={t('memoryV2.search')} placeholder={t(view.mode === 'meaning' ? 'memoryPage.meaning_placeholder' : 'memoryEditing.search_hint')} value={input} onChange={event => setInput(event.target.value)} />
        {/* Clear and the mode switch share one group: the switch sits flush at the box's end, clear 8 px before it. */}
        <div className="absolute right-1 top-1/2 flex -translate-y-1/2 items-center gap-2">
          {input && <IconButton aria-label={t('memoryV2.clear_search')} className="flex h-9 w-9 items-center justify-center sm:w-6" onClick={clearSearch}><X className="lucide-inline" aria-hidden="true" /></IconButton>}
          {inlineSwitch && <SearchModeSwitch mode={view.mode} onChange={setMode} />}
        </div>
      </div>
      {/* A phone puts the mode switch on its own row under the search box, above the filter and sort buttons. */}
      {/* So does a toolbar too narrow for the switch inside the box, so it never sits on the text. */}
      {!inlineSwitch && <div className="basis-full"><SearchModeSwitch wide={phone} mode={view.mode} onChange={setMode} /></div>}
      {/* A phone opens the filter sheet; a wide screen with the rail hidden shows it again. */}
      {(sheet || !railOpen) && <Btn aria-label={t('memoryPage.filters')} aria-expanded={sheet ? undefined : false} className="relative min-h-11 min-w-11 shrink-0 justify-center sm:min-h-10 sm:min-w-10" onClick={() => sheet ? setFiltersOpen(true) : setRailOpen(true)}><SlidersHorizontal className="lucide-inline" aria-hidden="true" />{filterCount > 0 && <span className="absolute -right-1.5 -top-1.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent px-1 text-[11px] text-accent-fg">{fmtNumber(filterCount)}</span>}</Btn>}
      {/* Picking another order leaves the search by meaning, which only ranks by match. */}
      <DateRangeButton range={view.range} onChange={setRange} />
      <SortControl view={view} decays={!!storeDecay} onChange={(next: MemoryRecordSort, order) => change({ sort: next, order, ...(meaning && next !== 'match' ? { mode: 'words' as const } : {}) })} />
      {!phone && <Btn className="min-h-10 shrink-0 whitespace-nowrap" onClick={() => onSelectingChange(true)}><ListChecks className="lucide-inline" />{t('memoryPage.select')}</Btn>}
    </div></div>}
    {!selecting && <SavedTopics topics={topics.topics} query={view.q} canSave={meaning} onRun={runTopic} onRemove={topics.remove} onSave={() => topics.save(view.q)} />}
    <KindPills bot={botName} wrap={!phone} kind={view.kind} counts={facets?.kind} disabled={locked} onChange={(kind: KindTab) => change({ kind })} />
    <ActiveFilters range={view.range} rangeField={dateField} onClearRange={() => setRange(undefined)} filters={filtersForKind(view.filters, view.kind)} onToggle={onToggleFilter} onRange={onRange} onClear={clearFilters} onClearRefs={() => { setJumpFocus(null); change({ filters: Object.fromEntries(Object.entries(view.filters).filter(([key]) => key !== 'ref')) }) }} />
  </div>

  const list = <div className="space-y-3">
    {feedback && <div role="status" className="flex items-center gap-2 rounded-lg bg-ok-subtle px-3 py-2 text-[13px] text-ok-text">{feedback}<IconButton aria-label={t('components.errorNotice.dismiss')} className="ml-auto flex h-8 w-8 items-center justify-center" onClick={() => setFeedback('')}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>}
    {/* No hand-off: cross-page selection and editing drafts must stay attached to this store. */}
    <ErrorNotice title={t(meaningUnavailable ? 'memoryPage.meaning_unavailable' : 'memoryPage.read_failed', { store: storeName })} message={errorText(records.error)} />
    {/* The list can stand without its counts: say they failed and offer to read them again. */}
    {!records.error && <ErrorNotice impact="partial" title={t('memoryPage.counts_failed')} message={errorText(counts.error)} askAgent={false}
      actions={queryReadFailed(counts) ? <button type="button" aria-disabled={counts.isFetching || undefined} onClick={() => { if (!counts.isFetching) void counts.refetch() }}>{t('memoryV2.retry_read')}</button> : undefined} />}
    {meaningUnavailable && <Btn className="min-h-11 w-fit" onClick={() => setMode('words')}>{t('memoryPage.search_words_instead')}</Btn>}
    {queryReadFailed(records) && <div className="flex flex-wrap gap-2"><Btn className="min-h-11 aria-disabled:cursor-default aria-disabled:opacity-30" aria-disabled={records.isFetching || undefined} onClick={() => { if (!records.isFetching) void records.refetch() }}>{t('memoryV2.retry_read')}</Btn>{onRecovery && <Btn className="min-h-11" onClick={onRecovery}>{t('memoryPage.open_recovery')}</Btn>}</div>}
    {searching && !records.error && <ul role="status" aria-label={t('pages.overview.memoryTab.loading')} className="m-0 list-none divide-y divide-border rounded-2xl border border-border bg-card p-0">{[0, 1, 2].map(index => <li key={index} className="space-y-2 px-4 py-4"><Skeleton className="h-3.5 w-4/5 motion-reduce:animate-none" /><Skeleton className="h-4 w-1/2 motion-reduce:animate-none" /></li>)}</ul>}
    {!searching && !records.error && !rows.length && !weaker && <Card className="mb-0 py-6">
      <EmptyState icon={<Search className="lucide-inline" />} title={narrowed ? t('memoryV2.no_matches') : t(privateMemory ? 'memoryV2.empty_title' : 'memoryV2.store_empty_title')} subtitle={narrowed ? t('memoryV2.no_matches_hint') : t(privateMemory ? 'memoryV2.empty_body' : 'memoryV2.store_empty_body')} />
      <div className="mt-3 flex flex-wrap justify-center gap-2">{narrowed ? <>{view.q && <Btn className="min-h-11" onClick={clearSearch}>{t('memoryV2.clear_search')}</Btn>}{(activeFilterCount(view.filters) > 0 || !!view.range) && <Btn className="min-h-11" onClick={clearFilters}>{t('memoryPage.clear_all')}</Btn>}</> : emptyActions}</div>
    </Card>}
    {(rows.length > 0 || weaker > 0) && <ul className="m-0 list-none overflow-hidden rounded-2xl border border-border bg-card p-0" aria-live="polite" aria-label={t('memoryV2.tab_memories')}>
      {rows.map((row, index) => [
        index === firstUnscored && index > 0 && view.kind === 'all' ? <li key="divider" className="flex h-9 items-center border-b border-border bg-bg-hover px-4 text-[12px] text-muted">{t(DIVIDER_KEYS[sort as keyof typeof DIVIDER_KEYS])}</li> : null,
        <RecordRow key={recordIdentity(row)} row={row} sort={sort} meaning={meaning} selecting={selecting} selected={isSelected(row)} chip={locked || selecting ? undefined : { filters: view.filters, onToggle: onToggleFilter }}
          selectDisabled={picked.lockedRow(row)}
          open={detailKey === recordIdentity(row)} focus={row === focused} saved={row === focused && !jumpFocus} focusRef={row === focused ? focusRow : undefined}
          onSelect={() => picked.toggle(row)} onOpen={() => setDetailKey(recordIdentity(row))}
          onEdit={() => edit.begin({ type: 'set', row })} onForget={() => edit.begin({ type: 'forget', row })} />,
      ])}
      {/* Shown after the last page of strong matches, where the weaker ones would follow. */}
      {weaker > 0 && !hasNextPage && <li className="flex min-h-[52px] items-center gap-2 bg-bg-hover px-4 text-[13px] text-muted sm:min-h-11">
        <span>{t('memoryPage.weaker_hidden', { count: fmtNumber(weaker) })}</span>
        <Btn className="ml-auto min-h-11 border-transparent text-accent sm:min-h-8" onClick={() => setWeak(true)}>{t('memoryPage.show_weaker')}</Btn>
      </li>}
    </ul>}
    {/* Watched for the next page; it sits under the last row. */}
    {hasNextPage && !searching && <div ref={end} aria-hidden="true" className="h-px" />}
    {isFetchingNextPage && <ul role="status" aria-label={t('pages.overview.memoryTab.loading')} className="m-0 list-none divide-y divide-border rounded-2xl border border-border bg-card p-0">{[0, 1].map(index => <li key={index} className="space-y-2 px-4 py-4"><Skeleton className="h-3.5 w-4/5 motion-reduce:animate-none" /><Skeleton className="h-4 w-1/2 motion-reduce:animate-none" /></li>)}</ul>}
    {!searching && !records.error && total > PAGE_SIZE && <p className="text-[12px] tabular-nums text-muted">{t('memoryEditing.page_range', { from: fmtNumber(1), to: fmtNumber(rows.length), total: fmtNumber(total) })}</p>}
  </div>

  const detailPanel = detail && <MemoryRecordDetail store={store} row={detail} phone={phone} fadeLine={storeDecay?.fade_line}
    onSaveAsLesson={onSaveAsLesson ? () => { setDetailKey(null); onSaveAsLesson(detail.text) } : undefined} chip={locked || selecting ? undefined : { filters: view.filters, onToggle: onToggleFilter }} onClose={() => setDetailKey(null)}
    onEdit={() => { setDetailKey(null); edit.begin({ type: 'set', row: detail }) }} onForget={() => { setDetailKey(null); edit.begin({ type: 'forget', row: detail }) }}
    onRestore={revisionId => { setDetailKey(null); edit.restore(detail, revisionId) }} onKeepCurrent={() => { setDetailKey(null); edit.keepCurrent(detail) }}
    onProposal={(proposal, operation) => { setDetailKey(null); edit.takeProposal(detail, proposal, operation) }} />

  return <div ref={rootRef} className="min-w-0" data-testid="memory-list">
    {stacked ? <div className="space-y-4">{toolbar}{list}{detailPanel}</div> : <div className="flex gap-6">
      {/* The rail stays in view while the list scrolls, as tall as the window, and scrolls on its own. */}
      {!compact && <FilterRail open={railOpen} width={railWidth} onResize={resizeRail}>
        <FilterPanel bot={botName} capped view={view} facets={facets} decay={storeDecay} onToggle={onToggleFilter} onRange={onRange}
          trailing={<IconButton aria-label={t('memoryPage.hide_filters')} title={t('memoryPage.hide_filters')} aria-expanded className="pi-morph -mr-2.5 flex h-9 w-9 shrink-0 items-center justify-center text-muted" onClick={() => setRailOpen(false)}><PanelLeftLight size={16} aria-hidden="true" /></IconButton>} />
      </FilterRail>}
      <div className="min-w-0 flex-1 space-y-4">{toolbar}{list}</div>
      <MemoryDetailSlide contentKey={detail ? detailKey : null}>{detailPanel}</MemoryDetailSlide>
    </div>}
    {sheet && filtersOpen && <Modal open title={t('memoryPage.filters')} onClose={() => setFiltersOpen(false)} headerActions={activeFilterCount(view.filters) > 0 ? <Btn className="min-h-11 border-transparent" onClick={clearFilters}>{t('memoryPage.clear_all')}</Btn> : undefined} footer={<Btn primary className="min-h-12 w-full justify-center" onClick={() => setFiltersOpen(false)}>{t('memoryPage.show_results')}</Btn>}>{filterPanel}</Modal>}
    {edit.dialog}
  </div>
}

