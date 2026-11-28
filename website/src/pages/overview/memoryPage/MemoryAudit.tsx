/**
 * The memory page's Audit tab: every write the store made or refused
 * (`memory_events`), newest first, with the Event, Kind and Source rail, search,
 * sort, a date range and a detail panel showing the value before and after.
 * The server filters, counts and pages (`memory_audit.list_events`); the next
 * page loads as the end of the list scrolls near. Nothing here deletes a row.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowDownUp, History, Search, SlidersHorizontal, X } from 'lucide-react'
import { api } from '../../../api/client'
import { Btn, Card, EmptyState, IconButton, Input, PanelSectionHeader, Skeleton } from '../../../components/ui'
import { PanelLeftLight } from '../../../components/icons/panels'
import ErrorNotice from '../../../components/ErrorNotice'
import Modal from '../../../components/Modal'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { compareText, fmtDate, fmtDateTimeNumeric, fmtNumber } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { savePrefExplicitly } from '../../../lib/uiPrefs'
import { safeGetItem } from '../../../utils/safeStorage'
import { queryReadFailed } from '../../../utils/queryReadFailed'
import type { MemoryAuditEvent, MemoryAuditGroup, MemoryAuditQuery, MemoryRecordKind } from '../../../types/memoryEditing'
import { memoryQueryRetry } from '../MemoryStoreCard'
import { MEMORY_RECORD_LABELS, errorText } from '../memoryRecordParts'
import { FILTERS_COLLAPSED_KEY, FILTERS_WIDTH_KEY, LIST_MIN } from './MemoryList'
import { FacetCheck, GroupScroll, ROW_PICKED } from './MemoryListParts'
import { FilterRail, RAIL_MAX, RAIL_MIN } from './MemoryRail'
import { MemoryDetailSlide } from './MemoryDetailSlide'
import { DateRangeButton, rangeLabel } from './MemoryDateRange'
import { dayDate, rangeBounds, useLocalDay, type DateRange } from './memoryDateRanges'
import { facetLabel, sourceGroup } from './memoryListView'
import { eventTip, kindTip, sourceTip } from './memoryTypeTips'
import { firstOfEach } from './memoryPaging'
import { eventCodes, eventLabel, eventTone, eventWhy, notSaved, railEvents } from './memoryAuditLabels'

const PAGE_SIZE = 50
const LOAD_AHEAD = '800px 0px'
const RAIL_DEFAULT = 240
const GROUPS: MemoryAuditGroup[] = ['event', 'kind', 'source']
const GROUP_KEYS: Record<MemoryAuditGroup, string> = { event: 'memoryAudit.group_event', kind: 'memoryAudit.group_kind', source: 'pages.overview.memoryTab.source' }
const KIND_KEYS: Record<MemoryRecordKind, string> = { fact: 'memoryV2.facts_label', directive: 'memoryV2.rules_label', episode: 'memoryV2.experiences_label' }
const CHIP = 'inline-flex h-[22px] max-w-full items-center whitespace-nowrap rounded-full px-2 text-[12px] leading-none'
const clampRail = (width: number) => Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(width)))
type Picked = Partial<Record<MemoryAuditGroup, string[]>>

const valueLabel = (group: MemoryAuditGroup, value: string) =>
  group === 'event' ? eventLabel(value) : group === 'kind' ? (KIND_KEYS[value as MemoryRecordKind] ? i18nT(KIND_KEYS[value as MemoryRecordKind]) : value) : facetLabel('source', value)

const groupTip = (group: MemoryAuditGroup, value: string, bot?: string) =>
  group === 'event' ? eventTip(value, bot) : group === 'kind' ? kindTip(value, bot) : sourceTip(value, bot)

/** A stored value as the row and the detail read it: a JSON string unquoted. */
function shownValue(raw: string | null): string {
  if (raw === null || raw === undefined) return ''
  try { const value: unknown = JSON.parse(raw); return typeof value === 'string' ? value : JSON.stringify(value) } catch { return raw }
}

/** A lesson's rule and its "don't" text, read from the stored JSON as the Memories list shows them. */
function lessonText(event: MemoryAuditEvent): { rule: string; negative?: string } | null {
  if (!event.memory_key?.startsWith('lesson.')) return null
  try {
    const value: unknown = JSON.parse(event.new_value ?? event.old_value ?? '')
    const record = value && typeof value === 'object' ? value as Record<string, unknown> : {}
    if (typeof record.rule !== 'string' || !record.rule.trim()) return null
    return { rule: record.rule.trim(), negative: typeof record.negative === 'string' && record.negative.trim() ? record.negative.trim() : undefined }
  } catch { return null }
}

/** The row's one line: a lesson's rule, a fact's key and value, an experience's text. */
function eventTitle(event: MemoryAuditEvent): string {
  const lesson = lessonText(event)
  if (lesson) return lesson.rule
  const value = shownValue(event.new_value ?? event.old_value)
  if (event.memory_type === 'episodic' || !event.memory_key) return value
  return value ? `${event.memory_key} = ${value}` : event.memory_key
}

export default function MemoryAudit({ store, botName }: { store: string; botName?: string }) {
  const { t } = useTranslation()
  const phone = useIsMobile()
  const [input, setInput] = useState('')
  const [q, setQ] = useState('')
  useEffect(() => { const timer = setTimeout(() => setQ(input.trim()), 250); return () => clearTimeout(timer) }, [input])
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [picked, setPicked] = useState<Picked>({})
  const [range, setRange] = useState<DateRange>()
  const [openId, setOpenId] = useState<number | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [railOpen, setRailOpenState] = useState(() => safeGetItem(FILTERS_COLLAPSED_KEY) !== '1')
  const setRailOpen = (open: boolean) => { setRailOpenState(open); savePrefExplicitly(FILTERS_COLLAPSED_KEY, open ? '0' : '1') }
  const [railWidth, setRailWidth] = useState(() => { const saved = Number(safeGetItem(FILTERS_WIDTH_KEY)); return saved ? clampRail(saved) : RAIL_DEFAULT })
  const resizeRail = (width: number, save = false) => { const next = clampRail(width); setRailWidth(next); if (save) savePrefExplicitly(FILTERS_WIDTH_KEY, String(next)) }
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
  // A side detail takes 384px including its gap; keep at least 320px for the list.
  const stacked = phone || (width > 0 && width - (!compact && railOpen ? railWidth + 24 : 0) < 320 + 384)
  const sheet = phone || compact || stacked

  const today = useLocalDay()
  const query = useMemo<MemoryAuditQuery>(() => {
    const filters: MemoryAuditQuery['filters'] = {}
    for (const group of GROUPS) if (picked[group]?.length) filters[group] = group === 'event' ? eventCodes(picked[group]!) : picked[group]
    return { q, order, filters, ...rangeBounds(range, dayDate(today) ?? undefined) }
  }, [q, order, picked, range, today])
  const events = useInfiniteQuery({
    queryKey: ['memory-audit', store, query],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.memoryEvents(store, query, pageParam, PAGE_SIZE),
    getNextPageParam: (last, pages) => last.has_more ? pages.length * PAGE_SIZE : undefined,
    retry: memoryQueryRetry,
    placeholderData: previous => previous,
  })
  // The counts come with every page; the first page's are the ones shown, and they stay while the next view loads.
  const counts = useQuery({ queryKey: ['memory-audit', store, query, 'counts'], queryFn: () => api.memoryEvents(store, query, 0, 1), retry: memoryQueryRetry, placeholderData: previous => previous })
  const searching = input.trim() !== q || events.isPending || events.isPlaceholderData
  const pages = events.data?.pages
  // A write between two page reads shifts the log down a row; a repeated event is shown once.
  const rows = useMemo(() => searching || events.error ? [] : firstOfEach((pages || []).flatMap(page => page.events), event => String(event.id)), [searching, events.error, pages])
  const total = pages?.[0]?.total || 0
  const facets = counts.data?.facets
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = events
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = end.current
    if (!node || !hasNextPage || isFetchingNextPage || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) void fetchNextPage() }, { rootMargin: LOAD_AHEAD })
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, rows.length])
  const open = rows.find(row => row.id === openId) ?? null

  const toggle = (group: MemoryAuditGroup, value: string) => setPicked(old => {
    const current = old[group] || []
    const next = current.includes(value) ? current.filter(item => item !== value) : [...current, value]
    const out = { ...old }
    if (next.length) out[group] = next
    else delete out[group]
    return out
  })
  const clearAll = () => { setPicked({}); setRange(undefined) }
  const chipCount = GROUPS.reduce((n, group) => n + (picked[group]?.length || 0), 0) + (range ? 1 : 0)
  const narrowed = !!q || chipCount > 0

  const rail = (capped: boolean, trailing?: React.ReactNode) => <div className="space-y-5">
    {trailing && <div className="flex justify-end">{trailing}</div>}
    {GROUPS.map(group => {
      const raw = facets?.[group] || {}
      const groupCounts = group === 'event' ? railEvents(raw) : raw
      const ticked = picked[group] || []
      const values = [...new Set([...Object.keys(groupCounts).filter(value => groupCounts[value] > 0), ...ticked])]
        .sort((a, b) => (groupCounts[b] || 0) - (groupCounts[a] || 0) || compareText(a, b))
      if (!values.length) return null
      const heading = t(GROUP_KEYS[group])
      return <section key={group} aria-label={heading}>
        <PanelSectionHeader label={heading} />
        <GroupScroll capped={capped}>
          {values.map(value => <FacetCheck key={value} label={valueLabel(group, value)} hint={groupTip(group, value, botName)} count={groupCounts[value] || 0} on={ticked.includes(value)} onToggle={() => toggle(group, value)} />)}
        </GroupScroll>
      </section>
    })}
  </div>

  const chips = [
    ...(range ? [{ key: 'range', label: rangeLabel(range), remove: () => setRange(undefined) }] : []),
    ...GROUPS.flatMap(group => (picked[group] || []).map(value => ({ key: `${group}:${value}`, label: valueLabel(group, value), remove: () => toggle(group, value) }))),
  ]
  const toolbar = <div className="space-y-3">
    <div className="flex min-w-0 flex-wrap gap-2" data-testid="memory-audit-toolbar">
      <div className={`relative min-w-0 flex-1 ${phone ? 'basis-full' : 'min-w-[16rem]'}`}>
        <Search className="lucide-inline pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
        <Input className="min-h-11 w-full pl-9 pr-10 sm:min-h-10" aria-label={t('memoryAudit.search')} placeholder={t('memoryAudit.search')} value={input} onChange={event => setInput(event.target.value)} />
        {input && <IconButton aria-label={t('memoryV2.clear_search')} className="absolute right-1 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center sm:w-6" onClick={() => setInput('')}><X className="lucide-inline" aria-hidden="true" /></IconButton>}
      </div>
      {(sheet || !railOpen) && <Btn aria-label={t('memoryPage.filters')} aria-expanded={sheet ? undefined : false} className="relative min-h-11 min-w-11 shrink-0 justify-center sm:min-h-10 sm:min-w-10" onClick={() => sheet ? setSheetOpen(true) : setRailOpen(true)}><SlidersHorizontal className="lucide-inline" aria-hidden="true" />{chipCount > 0 && <span className="absolute -right-1.5 -top-1.5 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent px-1 text-[11px] text-accent-fg">{fmtNumber(chipCount)}</span>}</Btn>}
      <DateRangeButton range={range} onChange={setRange} />
      <button type="button" onClick={() => setOrder(order === 'desc' ? 'asc' : 'desc')} className="flex min-h-11 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-border px-3 text-[13px] text-text focus-ring sm:min-h-10">
        <ArrowDownUp className="lucide-inline text-muted" aria-hidden="true" />{t('memoryPage.sort_by_value', { value: t(order === 'desc' ? 'memoryPage.order_newest' : 'memoryPage.order_oldest') })}
      </button>
    </div>
    {chips.length > 0 && <div className="flex flex-wrap items-center gap-x-1 gap-y-0">
      {chips.map(chip => <button key={chip.key} type="button" aria-label={t('memoryPage.remove_filter', { value: chip.label })} onClick={chip.remove} className="flex min-h-11 items-center sm:min-h-8">
        <span className="inline-flex h-8 max-w-[240px] items-center gap-1.5 rounded-full border border-border bg-card pl-3 pr-2 text-[13px] text-text"><span className="truncate">{chip.label}</span><X className="lucide-inline shrink-0 text-muted" aria-hidden="true" /></span>
      </button>)}
      <Btn className="min-h-11 border-transparent sm:min-h-8" onClick={clearAll}>{t('memoryPage.clear_all')}</Btn>
    </div>}
  </div>

  const list = <div className="space-y-3">
    <ErrorNotice title={t('memoryAudit.read_failed')} message={errorText(events.error)} askAgent />
    {/* The log can stand without its counts: say they failed and offer to read them again. */}
    {!events.error && <ErrorNotice impact="partial" title={t('memoryPage.counts_failed')} message={errorText(counts.error)} askAgent
      actions={queryReadFailed(counts) ? <button type="button" aria-disabled={counts.isFetching || undefined} onClick={() => { if (!counts.isFetching) void counts.refetch() }}>{t('memoryV2.retry_read')}</button> : undefined} />}
    {queryReadFailed(events) && <Btn className="min-h-11 w-fit aria-disabled:cursor-default aria-disabled:opacity-30" aria-disabled={events.isFetching || undefined} onClick={() => { if (!events.isFetching) void events.refetch() }}>{t('memoryV2.retry_read')}</Btn>}
    {searching && !events.error && <ul role="status" aria-label={t('pages.overview.memoryTab.loading')} className="m-0 list-none divide-y divide-border rounded-2xl border border-border bg-card p-0">{[0, 1, 2].map(index => <li key={index} className="space-y-2 px-4 py-4"><Skeleton className="h-3.5 w-4/5 motion-reduce:animate-none" /><Skeleton className="h-4 w-1/2 motion-reduce:animate-none" /></li>)}</ul>}
    {!searching && !events.error && !rows.length && <Card className="mb-0 py-6">
      <EmptyState icon={<History className="lucide-inline" />} title={t(narrowed ? 'memoryAudit.no_matches' : 'memoryAudit.empty')} />
      {narrowed && <div className="mt-3 flex flex-wrap justify-center gap-2">{q && <Btn className="min-h-11" onClick={() => setInput('')}>{t('memoryV2.clear_search')}</Btn>}{chipCount > 0 && <Btn className="min-h-11" onClick={clearAll}>{t('memoryPage.clear_all')}</Btn>}</div>}
    </Card>}
    {rows.length > 0 && <ul className="m-0 list-none overflow-hidden rounded-2xl border border-border bg-card p-0" aria-label={t('memoryAudit.events')}>
      {rows.map(event => <AuditRow key={event.id} event={event} open={event.id === openId} onOpen={() => setOpenId(event.id)} />)}
    </ul>}
    {hasNextPage && !searching && <div ref={end} aria-hidden="true" className="h-px" />}
    {isFetchingNextPage && <ul role="status" aria-label={t('pages.overview.memoryTab.loading')} className="m-0 list-none rounded-2xl border border-border bg-card p-0"><li className="space-y-2 px-4 py-4"><Skeleton className="h-3.5 w-4/5 motion-reduce:animate-none" /><Skeleton className="h-4 w-1/2 motion-reduce:animate-none" /></li></ul>}
    {!searching && !events.error && rows.length > 0 && <p className="text-[12px] tabular-nums text-muted">{t('memoryAudit.shown_of', { shown: fmtNumber(rows.length), total: fmtNumber(total) })}</p>}
  </div>

  const detail = open && <AuditDetail event={open} phone={phone} onClose={() => setOpenId(null)} />
  return <div ref={rootRef} className="min-w-0" data-testid="memory-audit">
    {stacked ? <div className="space-y-4">{toolbar}{list}{detail}</div> : <div className="flex gap-6">
      {!compact && <FilterRail open={railOpen} width={railWidth} onResize={resizeRail}>
        {rail(true, <IconButton aria-label={t('memoryPage.hide_filters')} title={t('memoryPage.hide_filters')} aria-expanded className="pi-morph -mr-2.5 flex h-9 w-9 shrink-0 items-center justify-center text-muted" onClick={() => setRailOpen(false)}><PanelLeftLight size={16} aria-hidden="true" /></IconButton>)}
      </FilterRail>}
      <div className="min-w-0 flex-1 space-y-4">{toolbar}{list}</div>
      <MemoryDetailSlide contentKey={open ? String(open.id) : null}>{detail}</MemoryDetailSlide>
    </div>}
    {sheet && sheetOpen && <Modal open title={t('memoryPage.filters')} onClose={() => setSheetOpen(false)} headerActions={chipCount > 0 ? <Btn className="min-h-11 border-transparent" onClick={clearAll}>{t('memoryPage.clear_all')}</Btn> : undefined} footer={<Btn primary className="min-h-12 w-full justify-center" onClick={() => setSheetOpen(false)}>{t('memoryPage.show_results')}</Btn>}>{rail(false)}</Modal>}
  </div>
}

function EventChip({ code }: { code: string }) {
  return <span className={`${CHIP} ${eventTone(code)}`}>{eventLabel(code)}</span>
}

function AuditRow({ event, open, onOpen }: { event: MemoryAuditEvent; open: boolean; onOpen: () => void }) {
  const { t } = useTranslation()
  const source = sourceGroup(event.source || '')
  const negative = lessonText(event)?.negative
  return <li className={`border-b border-border px-4 py-3.5 [contain-intrinsic-size:auto_76px] [content-visibility:auto] first:rounded-t-[15px] last:rounded-b-[15px] last:border-b-0 ${open ? ROW_PICKED : ''}`}>
    <button type="button" onClick={onOpen} aria-expanded={open} className="block w-full space-y-1.5 text-left focus-ring">
      <p className="line-clamp-2 break-words text-[15px] leading-[22px] text-text [overflow-wrap:anywhere]">{eventTitle(event) || event.event_type}</p>
      {negative && <p className="line-clamp-1 text-[13px] leading-5 text-muted">{t('memoryPage.lesson_dont', { text: negative })}</p>}
      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5 text-[12px] text-muted">
        <EventChip code={event.event_type} />
        <span>{t(MEMORY_RECORD_LABELS[event.kind])}</span>
        <span className="truncate" title={event.source || undefined}>{facetLabel('source', source)}</span>
        <time dateTime={event.created_at} title={fmtDateTimeNumeric(event.created_at)} className="tabular-nums" data-i18n-opaque>{fmtDate(event.created_at)}</time>
      </span>
    </button>
  </li>
}

function AuditDetail({ event, phone, onClose }: { event: MemoryAuditEvent; phone: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const title = eventLabel(event.event_type)
  const why = eventWhy(event.event_type)
  const before = shownValue(event.old_value)
  const after = shownValue(event.new_value)
  const fields: [string, React.ReactNode][] = []
  if (event.memory_key && event.memory_type !== 'episodic') fields.push([t('memoryV2.memory_key'), <code className="break-all text-[12px]">{event.memory_key}</code>])
  fields.push([t('memoryAudit.group_kind'), t(MEMORY_RECORD_LABELS[event.kind])])
  fields.push([t('pages.overview.memoryTab.source'), <span title={event.source || undefined}>{facetLabel('source', sourceGroup(event.source || ''))}</span>])
  fields.push([t('memoryAudit.when'), <span className="tabular-nums">{fmtDateTimeNumeric(event.created_at)}</span>])
  const VALUE = 'whitespace-pre-wrap break-words [overflow-wrap:anywhere]'
  const body = <div className="space-y-4">
    <dl className="m-0 grid gap-x-3 gap-y-2.5 text-[13px] leading-5" style={{ gridTemplateColumns: '96px minmax(0,1fr)' }}>
      {fields.map(([label, value]) => [<dt key={`${label}:k`} className="text-muted">{label}</dt>, <dd key={label} className="m-0 min-w-0 text-text">{value}</dd>])}
    </dl>
    {/* Nothing was stored, so there is no before and after: the value shows once, in a neutral tone. */}
    {notSaved(event.event_type) ? <section aria-label={t('memoryAudit.not_saved')} className="pt-1">
      <PanelSectionHeader label={t('memoryAudit.not_saved')} />
      <div className={`rounded-lg bg-bg-hover px-3 py-2 font-mono text-[12px] leading-5 text-text ${VALUE}`}>{after || before || '—'}</div>
    </section> : <section aria-label={t('memoryAudit.change')} className="pt-1">
      <PanelSectionHeader label={t('memoryAudit.change')} />
      <div className="overflow-hidden rounded-lg font-mono text-[12px] leading-5">
        {/* The value reads in body text; the tint and the tone-mixed label carry which side it is. */}
        <div className="grid gap-3 bg-danger-subtle px-3 py-2" style={{ gridTemplateColumns: '56px minmax(0,1fr)' }}><span className="text-[var(--danger-body)]">{t('memoryAudit.before')}</span><span className={`${VALUE} text-text`}>{before || '—'}</span></div>
        <div className="grid gap-3 bg-ok-subtle px-3 py-2" style={{ gridTemplateColumns: '56px minmax(0,1fr)' }}><span className="text-[color-mix(in_srgb,var(--ok-text)_60%,var(--text))]">{t('memoryAudit.after')}</span><span className={`${VALUE} text-text`}>{after || '—'}</span></div>
      </div>
    </section>}
    {why && <section aria-label={t('memoryAudit.why')}><PanelSectionHeader label={t('memoryAudit.why')} /><p className="text-[13px] leading-5 text-text">{why}</p></section>}
  </div>
  if (phone) return <Modal open title={title} onClose={onClose}>{body}</Modal>
  return <aside aria-label={title} className="w-full space-y-4 rounded-2xl border border-border bg-card p-5" data-testid="memory-audit-detail">
    <div className="flex items-center justify-between gap-2"><h3 className="m-0 text-[15px] font-semibold text-text">{title}</h3><IconButton aria-label={t('components.modal.close')} className="flex h-8 w-8 items-center justify-center" onClick={onClose}><X className="lucide-inline" aria-hidden="true" /></IconButton></div>
    {body}
  </aside>
}
