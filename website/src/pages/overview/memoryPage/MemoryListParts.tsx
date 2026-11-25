/**
 * The memory list's controls and rows: the kind pills, the sort menu, the
 * filter panel with live counts, the active-filter chips and one record row.
 */
import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, ArrowDownUp, Check, ChevronDown, Folder, History, Mail, MoreHorizontal, Plus, Search, Sparkles, X } from 'lucide-react'
import { Btn, IconButton, Input, PanelSectionHeader, Slider } from '../../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { compareText, fmtDateTimeNumeric, fmtNumber, fmtDate } from '../../../i18n/format'
import type { MemoryRecord, MemoryRecordFacetGroup, MemoryRecordFacets, MemoryRecordFilters, MemoryRecordSort } from '../../../types/memoryEditing'
import { MEMORY_RECORD_ICONS, MEMORY_RECORD_LABELS, memoryRecordBody } from '../memoryRecordParts'
import { FACET_GROUPS, GROUP_KINDS, SCOPE_UNUSABLE, effectiveSort, facetLabel, sourceGroup, lessonFacts, recordTags, recordTitle, type KindTab, type ListView, type SearchMode } from './memoryListView'

const KINDS: [KindTab, string][] = [['all', 'memoryV2.filter_all'], ['fact', 'memoryV2.facts_label'], ['directive', 'memoryV2.rules_label'], ['episode', 'memoryV2.experiences_label']]
const SORT_KEYS: Record<MemoryRecordSort, string> = { updated: 'memoryPage.field_updated', created: 'memoryPage.field_created', confidence: 'memoryPage.facet_confidence', importance: 'memoryPage.facet_importance', key: 'memoryPage.sort_key', match: 'memoryPage.sort_match' }
const ORDER_KEYS: Record<MemoryRecordSort, [string, string]> = {
  updated: ['memoryPage.order_newest', 'memoryPage.order_oldest'], created: ['memoryPage.order_newest', 'memoryPage.order_oldest'],
  confidence: ['memoryPage.order_highest', 'memoryPage.order_lowest'], importance: ['memoryPage.order_highest', 'memoryPage.order_lowest'],
  key: ['memoryPage.order_z_a', 'memoryPage.order_a_z'], match: ['memoryPage.order_highest', 'memoryPage.order_lowest'],
}
const GROUP_KEYS: Record<MemoryRecordFacetGroup, string> = {
  category: 'pages.overview.memoryTab.category', applies: 'memoryPage.facet_applies', scope: 'pages.overview.memoryTab.scope', tag: 'memoryPage.facet_tags', source: 'pages.overview.memoryTab.source',
  surface: 'memoryV2.group_surface', crew: 'memoryV2.group_crew',
}
// A group heading names the kind it filters while the All pill shows every kind.
const GROUP_KIND_KEYS: Partial<Record<MemoryRecordFacetGroup, string>> = { category: 'memoryPage.for_lessons', applies: 'memoryPage.for_lessons', scope: 'memoryPage.for_lessons', tag: 'memoryPage.for_experiences' }
// The picked kind pill: filled with the text colour on both themes (mock S2).
export const PILL_ON = 'border-text bg-text font-medium text-bg'
// Each kind's icon colour, so facts, lessons and experiences read apart at a glance.
const KIND_TONE: Record<MemoryRecord['kind'], string> = { fact: 'text-info-text', directive: 'text-accent', episode: 'text-ok-text' }
// An open or picked row: an 8% accent tint (mock S2).
export const ROW_PICKED = 'bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]'
// The lesson a "Lesson saved" link lands on: the saved tint and a bar on its left edge.
export const ROW_SAVED = 'bg-[color-mix(in_srgb,var(--ok-subtle)_55%,transparent)] shadow-[inset_3px_0_0_var(--ok)]'
const CHIP = 'inline-flex h-[22px] max-w-full items-center gap-1 whitespace-nowrap rounded-full text-[12px] leading-none'
// Red only below the review line; every other score is a plain number.
const REVIEW_LINE = 0.5
/** A 0–1 score with two decimals, in the reader's locale. */
export const fmtScore = (value: number) => fmtNumber(value, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const SCORE_KEYS = { confidence: 'memoryPage.confidence_value', importance: 'memoryPage.importance_value', match: 'memoryPage.match_value' } as const

/** The kind pills, one row with live counts; a phone scrolls the row sideways. */
export function KindPills({ kind, counts, onChange, disabled, wrap = false }: { kind: KindTab; counts?: Record<string, number>; onChange: (kind: KindTab) => void; disabled?: boolean; wrap?: boolean }) {
  const { t } = useTranslation()
  const total = counts ? (counts.fact || 0) + (counts.directive || 0) + (counts.episode || 0) : undefined
  return <div role="radiogroup" aria-label={t('memoryV2.group_kind')} title={t('memoryV2.memory_kinds_explanation')} className={`-mx-1 flex min-w-0 gap-2 px-1 ${wrap ? 'flex-wrap' : 'overflow-x-auto [scrollbar-width:none]'}`}>
    {KINDS.map(([key, label]) => {
      const on = key === kind
      const count = key === 'all' ? total : counts ? counts[key] || 0 : undefined
      return <button key={key} type="button" role="radio" aria-checked={on} disabled={disabled} onClick={() => onChange(key)} className="flex min-h-11 shrink-0 items-center disabled:opacity-40 sm:min-h-8">
        <span className={`flex h-8 items-center gap-1.5 rounded-full border px-3 text-[13px] ${on ? PILL_ON : 'border-border text-text'}`}>{t(label)}{count !== undefined && <span className={`tabular-nums ${on ? 'opacity-70' : 'text-muted'}`}>{fmtNumber(count)}</span>}</span>
      </button>
    })}
  </div>
}

/** The sort menu with its direction, and a one-press Reverse beside it. */
export function SortControl({ view, onChange }: { view: ListView; onChange: (sort: MemoryRecordSort, order: 'asc' | 'desc') => void }) {
  const { t } = useTranslation()
  const sort = effectiveSort(view)
  const sorts: [MemoryRecordSort, string?][] = [
    ...(view.q ? [['match'] as [MemoryRecordSort]] : []),
    ['updated'], ['created'],
    ...(view.kind !== 'episode' ? [['confidence', view.kind === 'all' ? 'memoryPage.sort_hint_confidence' : undefined] as [MemoryRecordSort, string?]] : []),
    ...(view.kind === 'all' || view.kind === 'episode' ? [['importance', view.kind === 'all' ? 'memoryPage.sort_hint_importance' : undefined] as [MemoryRecordSort, string?]] : []),
    ['key'],
  ]
  const [desc, asc] = ORDER_KEYS[sort]
  const label = sort === 'match' ? t(SORT_KEYS.match) : `${t(SORT_KEYS[sort])} · ${t(view.order === 'desc' ? desc : asc)}`
  return <div className="flex min-h-11 shrink-0 overflow-hidden rounded-lg border border-border sm:min-h-10">
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" className="flex min-w-0 flex-1 items-center gap-1.5 whitespace-nowrap px-3 text-[13px] text-text focus-ring">
          <span className="truncate">{t('memoryPage.sort_by_value', { value: label })}</span><ChevronDown className="lucide-inline ml-auto text-muted" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuRadioGroup value={sort} onValueChange={value => onChange(value as MemoryRecordSort, value === 'key' ? 'asc' : view.order)}>
          {sorts.map(([key, hint]) => <DropdownMenuRadioItem key={key} value={key}><span className="flex-1">{t(SORT_KEYS[key])}</span>{hint && <span className="text-[12px] text-muted">{t(hint)}</span>}</DropdownMenuRadioItem>)}
        </DropdownMenuRadioGroup>
        {sort !== 'match' && <><DropdownMenuSeparator /><DropdownMenuRadioGroup value={view.order} onValueChange={value => onChange(sort, value as 'asc' | 'desc')}>
          <DropdownMenuRadioItem value="desc">{t(desc)}</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="asc">{t(asc)}</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup></>}
      </DropdownMenuContent>
    </DropdownMenu>
    {sort !== 'match' && <button type="button" aria-label={t('memoryPage.reverse_order')} title={t('memoryPage.reverse_order')} onClick={() => onChange(sort, view.order === 'desc' ? 'asc' : 'desc')} className="flex w-11 items-center justify-center border-l border-border text-muted focus-ring sm:w-9"><ArrowDownUp className="lucide-inline" aria-hidden="true" /></button>}
  </div>
}

function FacetCheck({ label, count, on, warn, onToggle }: { label: string; count: number; on: boolean; warn?: boolean; onToggle: () => void }) {
  return <label className="grid min-h-11 cursor-pointer items-center gap-2 text-[13px] text-text sm:min-h-8" style={{ gridTemplateColumns: '16px minmax(0,1fr) 48px' }}>
    <input type="checkbox" aria-label={label} className="h-4 w-4 accent-accent" checked={on} onChange={onToggle} />
    <span className={`truncate ${warn ? 'text-warn-text' : ''}`} title={label}>{label}</span>
    <span className="text-right text-[12px] tabular-nums text-muted">{fmtNumber(count)}</span>
  </label>
}

/** A rail group's values, at most 16 rows high; past that the group scrolls, with a
 *  fade and a hint at its foot until the last row is in view. */
function GroupScroll({ capped, children }: { capped: boolean; children: ReactNode }) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement>(null)
  const [more, setMore] = useState(false)
  const check = useCallback(() => {
    const box = ref.current
    setMore(!!box && box.scrollHeight - box.scrollTop - box.clientHeight > 1)
  }, [])
  // After every render: the values or their count may have changed the height.
  useLayoutEffect(check)
  if (!capped) return <>{children}</>
  // The fade sits over the last rows; the hint has a row of its own below them.
  return <div>
    <div className="relative">
      <div ref={ref} onScroll={check} data-scroll="y" className="max-h-[32rem] overflow-y-auto overscroll-contain" data-testid="facet-group-scroll">{children}</div>
      {more && <div aria-hidden="true" className="pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-bg to-transparent" />}
    </div>
    {more && <p aria-hidden="true" className="pt-0.5 text-right text-[11px] leading-4 text-muted" data-testid="facet-group-more">{t('memoryPage.scroll_more')}</p>}
  </div>
}

/** The filter groups with live counts from `facets=1`, and the confidence range.
 *  A box at the top narrows every group's values by label. */
export function FilterPanel({ view, facets, trailing, capped = false, onToggle, onRange }: {
  view: ListView; facets?: MemoryRecordFacets
  /** A control at the end of the search row (the wide rail's hide button). */
  trailing?: ReactNode
  /** The wide rail: each group scrolls inside 16 rows. */
  capped?: boolean
  onToggle: (group: MemoryRecordFacetGroup, value: string) => void
  onRange: (bound: 'conf_min' | 'conf_max', value: number | undefined) => void
}) {
  const { t } = useTranslation()
  const [find, setFind] = useState('')
  const needle = find.trim().toLocaleLowerCase()
  const groups = FACET_GROUPS.filter(group => view.kind === 'all' || GROUP_KINDS[group].includes(view.kind))
  const low = view.filters.conf_min ?? 0
  const high = view.filters.conf_max ?? 1
  const shown = groups.map(group => {
    const counts = facets?.[group] || {}
    const ticked = view.filters[group] || []
    // A ticked value stays listed even when the other filters leave it no rows.
    const values = [...new Set([...Object.keys(counts).filter(value => counts[value] > 0), ...ticked])]
      .filter(value => !needle || facetLabel(group, value).toLocaleLowerCase().includes(needle))
      .sort((a, b) => Number(ticked.includes(b)) - Number(ticked.includes(a)) || (counts[b] || 0) - (counts[a] || 0) || compareText(a, b))
    return { group, counts, ticked, values }
  }).filter(item => item.values.length)
  return <div className="space-y-5">
    <div className="flex items-center">
      <div className="relative min-w-0 flex-1">
        <Search className="lucide-inline pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
        <Input type="search" className="min-h-11 w-full pl-9 sm:min-h-9" aria-label={t('memoryPage.search_filters')} placeholder={t('memoryPage.search_filters')} value={find} onChange={event => setFind(event.target.value)} />
      </div>
      {trailing}
    </div>
    {view.kind !== 'episode' && !needle && <section aria-label={t('memoryPage.facet_confidence')}>
      <PanelSectionHeader label={view.kind === 'all' ? t('memoryPage.for_facts_lessons', { group: t('memoryPage.facet_confidence') }) : t('memoryPage.facet_confidence')} />
      <div className="space-y-3 pt-1">
        <Slider label={t('memoryPage.conf_lowest')} showValue min={0} max={1} step={0.05} value={low} formatValue={fmtScore} onChange={value => onRange('conf_min', value > 0 ? Math.min(value, high) : undefined)} />
        <Slider label={t('memoryPage.conf_highest')} showValue min={0} max={1} step={0.05} value={high} formatValue={fmtScore} onChange={value => onRange('conf_max', value < 1 ? Math.max(value, low) : undefined)} />
      </div>
    </section>}
    {needle && !shown.length && <p role="status" className="text-[13px] text-muted">{t('memoryV2.no_matches')}</p>}
    {shown.map(({ group, counts, ticked, values }) => {
      const heading = view.kind === 'all' && GROUP_KIND_KEYS[group] ? t(GROUP_KIND_KEYS[group]!, { group: t(GROUP_KEYS[group]) }) : t(GROUP_KEYS[group])
      return <section key={group} aria-label={heading}>
        <PanelSectionHeader label={heading} />
        <GroupScroll capped={capped}>
          {values.map(value => <FacetCheck key={value} label={facetLabel(group, value)} count={counts[value] || 0} on={ticked.includes(value)} warn={group === 'scope' && value === SCOPE_UNUSABLE} onToggle={() => onToggle(group, value)} />)}
        </GroupScroll>
      </section>
    })}
  </div>
}

/** One removable chip per ticked filter value, and Clear all. */
export function ActiveFilters({ filters, onToggle, onRange, onClear, onClearRefs }: { filters: MemoryRecordFilters; onClearRefs?: () => void; onToggle: (group: MemoryRecordFacetGroup, value: string) => void; onRange: (bound: 'conf_min' | 'conf_max', value: undefined) => void; onClear: () => void }) {
  const { t } = useTranslation()
  const chips = FACET_GROUPS.flatMap(group => (filters[group] || []).map(value => ({ key: `${group}:${value}`, label: facetLabel(group, value), remove: () => onToggle(group, value) })))
  // A graph selection is one chip: removing it brings the whole list back.
  if (filters.ref?.length && onClearRefs) chips.unshift({ key: 'ref', label: t('memoryPage.graph_selected', { count: filters.ref.length, formatted: fmtNumber(filters.ref.length) }), remove: onClearRefs })
  if (filters.conf_min !== undefined || filters.conf_max !== undefined) chips.push({ key: 'conf', label: t('memoryPage.confidence_range', { low: fmtScore(filters.conf_min ?? 0), high: fmtScore(filters.conf_max ?? 1) }), remove: () => { onRange('conf_min', undefined); onRange('conf_max', undefined) } })
  if (!chips.length) return null
  return <div className="flex flex-wrap items-center gap-x-1 gap-y-0">
    {chips.map(chip => <button key={chip.key} type="button" aria-label={t('memoryPage.remove_filter', { value: chip.label })} onClick={chip.remove} className="flex min-h-11 items-center sm:min-h-8">
      <span className="inline-flex h-8 max-w-[240px] items-center gap-1.5 rounded-full border border-border bg-card pl-3 pr-2 text-[13px] text-text"><span className="truncate">{chip.label}</span><X className="lucide-inline shrink-0 text-muted" aria-hidden="true" /></span>
    </button>)}
    <Btn className="min-h-11 border-transparent sm:min-h-8" onClick={onClear}>{t('memoryPage.clear_all')}</Btn>
  </div>
}

export function ScoreChip({ value, label }: { value: number; label: 'confidence' | 'importance' | 'match' }) {
  const { t } = useTranslation()
  const low = label === 'confidence' && value < REVIEW_LINE
  const text = t(SCORE_KEYS[label], { value: fmtScore(value) })
  return <span className={`${CHIP} px-2 ${low ? 'bg-danger-subtle text-danger-text' : 'bg-bg-hover text-muted'}`}>{low && <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />}<span className="tabular-nums">{text}</span></span>
}

/** The kind, and for a lesson its category: "Lesson · Tool". */
export function KindLabel({ row }: { row: MemoryRecord }) {
  const { t } = useTranslation()
  const Icon = MEMORY_RECORD_ICONS[row.kind]
  const kind = t(MEMORY_RECORD_LABELS[row.kind])
  return <><Icon className={`lucide-inline ${KIND_TONE[row.kind]}`} aria-hidden="true" />{row.kind === 'directive' ? t('memoryPage.lesson_category', { kind, category: facetLabel('category', lessonFacts(row).category) }) : kind}</>
}

export type ChipFilter = { filters: MemoryRecordFilters; onToggle: (group: MemoryRecordFacetGroup, value: string) => void }

/** A row chip that is also a filter: a press ticks its value in the matching
 *  group, a second press clears it, and the row behind it does not open. A touch
 *  screen gets a 44 px hit area around the 22 px chip. */
export function FilterChip({ group, value, tone, chip, title, children }: { group: MemoryRecordFacetGroup; value: string; tone: string; chip?: ChipFilter; title?: string; children: ReactNode }) {
  if (!chip) return <span className={`${CHIP} ${tone}`} title={title}>{children}</span>
  const on = !!chip.filters[group]?.includes(value)
  return <button type="button" aria-pressed={on} title={title} onClick={event => { event.stopPropagation(); chip.onToggle(group, value) }}
    className={`${CHIP} ${tone} relative cursor-pointer transition-colors hover:brightness-95 hover:ring-1 hover:ring-inset hover:ring-border-strong focus-ring pointer-coarse:before:absolute pointer-coarse:before:-inset-y-[11px] pointer-coarse:before:inset-x-0 pointer-coarse:before:content-[''] ${on ? 'ring-1 ring-inset ring-accent' : ''}`}>{children}</button>
}

/** The chips under a row's text: what it is, how sure, where from, when. */
export function RecordMeta({ row, sort, chip, meaning = false }: { row: MemoryRecord; sort: MemoryRecordSort; chip?: ChipFilter; meaning?: boolean }) {
  const { t } = useTranslation()
  const lesson = row.kind === 'directive' ? lessonFacts(row) : null
  const score = typeof row.importance === 'number' ? row.importance : null
  const created = sort === 'created'
  const when = created ? row.created_at : row.updated_at
  return <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5">
    {lesson ? <FilterChip group="category" value={lesson.category} tone="px-1 text-muted" chip={chip}><KindLabel row={row} /></FilterChip> : <span className={`${CHIP} text-muted`}><KindLabel row={row} /></span>}
    {lesson && lesson.applies !== 'unstated' && <FilterChip group="applies" value={lesson.applies} tone={`px-2 ${lesson.applies === 'always' ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`} chip={chip}>{facetLabel('applies', lesson.applies)}</FilterChip>}
    {lesson?.scope === SCOPE_UNUSABLE && <FilterChip group="scope" value={SCOPE_UNUSABLE} tone="bg-warn-subtle px-2 text-warn-text" chip={chip} title={t('pages.overview.memoryTab.scope_unusable_hint')}><AlertTriangle className="lucide-inline" aria-hidden="true" />{facetLabel('scope', SCOPE_UNUSABLE)}</FilterChip>}
    {lesson && lesson.scope && lesson.scope !== SCOPE_UNUSABLE && <FilterChip group="scope" value={lesson.scope} tone="bg-bg-hover px-2 text-muted" chip={chip}><Folder className="lucide-inline" aria-hidden="true" /><span className="truncate font-mono">{lesson.scope}</span></FilterChip>}
    {typeof row.match === 'number' && <span className={`${CHIP} px-2 ${meaning ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`} title={t(meaning ? 'memoryPage.meaning_match_hint' : 'memoryPage.match_hint')}><span className="tabular-nums">{t('memoryPage.match_value', { value: fmtScore(row.match) })}</span></span>}
    {row.kind === 'episode' && recordTags(row).map(tag => <FilterChip key={tag} group="tag" value={tag} tone="bg-bg-hover px-2 text-muted" chip={chip}>#{tag}</FilterChip>)}
    {score !== null && <ScoreChip value={score} label={row.kind === 'episode' ? 'importance' : 'confidence'} />}
    {/* The facet counts spell a blank source as `unknown` (memory_record_facets.SOURCE_UNKNOWN). */}
    <FilterChip group="source" value={sourceGroup(row.source)} tone="px-1 text-muted" chip={chip}><span className="truncate" title={row.source || undefined}>{facetLabel('source', sourceGroup(row.source))}</span></FilterChip>
    {when && <span className={`${CHIP} text-muted`} title={fmtDateTimeNumeric(when)}><time dateTime={when} className="tabular-nums" data-i18n-opaque>{t(created ? 'memoryPage.created_on' : 'memoryPage.updated_on', { date: fmtDate(when) })}</time></span>}
  </div>
}

/** One record in the list: text, chips, key, and its actions menu. */
export function RecordRow({ row, sort, meaning = false, selecting, selected, selectDisabled, open, focus, chip, onSelect, onOpen, onEdit, onForget, focusRef }: {
  row: MemoryRecord; sort: MemoryRecordSort; meaning?: boolean; selecting: boolean; selected: boolean; selectDisabled: boolean; open: boolean; focus?: boolean; chip?: ChipFilter
  onSelect: () => void; onOpen: () => void; onEdit: () => void; onForget: () => void
  focusRef?: React.Ref<HTMLLIElement>
}) {
  const { t } = useTranslation()
  const body = memoryRecordBody(row)
  const title = recordTitle(row, body)
  const negative = row.kind === 'directive' ? lessonFacts(row).negative : undefined
  const pending = row.metadata?.pending_conflicts || 0
  // The first and last rows take the list's inner corner radius (16 px less its
  // 1 px border), so the saved and open tints follow the rounded corners. An
  // off-screen row is neither laid out nor painted (`content-visibility`), so a
  // list scrolled thousands of rows deep stays as quick as its first page.
  return <li ref={focusRef} data-memory-focus={focus || undefined} className={`grid gap-x-4 border-b [contain-intrinsic-size:auto_112px] [content-visibility:auto] border-border px-4 py-3.5 first:rounded-t-[15px] last:rounded-b-[15px] last:border-b-0 ${focus ? ROW_SAVED : open || selected ? ROW_PICKED : ''}`} style={{ gridTemplateColumns: `${selecting ? '20px ' : ''}minmax(0,1fr) auto` }}>
    {selecting && <span className="pt-1"><input type="checkbox" className="h-4 w-4 accent-accent" aria-label={t('memoryEditing.select_record', { text: body })} checked={selected} disabled={selectDisabled} onChange={onSelect} /></span>}
    <div className="min-w-0 space-y-1.5">
      {focus && <p className="flex items-center gap-1 text-[12px] font-medium text-ok-text"><Check className="lucide-inline" aria-hidden="true" />{t('memoryPage.just_saved')}</p>}
      <button type="button" onClick={onOpen} className="block w-full text-left focus-ring" aria-label={t('memoryV2.view_details')} aria-expanded={open}>
        <p className="line-clamp-3 whitespace-pre-wrap break-words text-[15px] leading-[22px] text-text [overflow-wrap:anywhere]">{title}</p>
      </button>
      {negative && <p className="line-clamp-2 text-[13px] leading-5 text-muted">{t('memoryPage.lesson_dont', { text: negative })}</p>}
      {!!row.metadata?.email_addresses?.length && <p className="flex flex-wrap gap-x-3 text-[13px] leading-5 text-muted">{row.metadata.email_addresses.slice(0, 3).map(email => <span key={email} className="inline-flex min-w-0 items-start gap-1 break-all"><Mail className="lucide-inline shrink-0" aria-hidden="true" />{email}</span>)}</p>}
      <RecordMeta row={row} sort={sort} chip={chip} meaning={meaning} />
      {row.key && <p className="truncate font-mono text-[11px] text-muted" title={row.key}>{row.key}</p>}
      {pending > 0 && <Btn className="mt-1 min-h-11" onClick={onOpen}><History className="lucide-inline" />{t('memoryEditing.review_proposals', { count: fmtNumber(pending) })}</Btn>}
    </div>
    <DropdownMenu>
      <DropdownMenuTrigger asChild><IconButton aria-label={t('memoryPage.record_actions')} className="flex h-11 w-11 items-center justify-center self-start sm:h-8 sm:w-8"><MoreHorizontal className="lucide-inline" aria-hidden="true" /></IconButton></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={onOpen}>{t('memoryV2.view_details')}</DropdownMenuItem>
        <DropdownMenuItem disabled={!row.revision} onSelect={onEdit}>{t('memoryV2.edit_action')}</DropdownMenuItem>
        <DropdownMenuItem disabled={!row.revision} onSelect={onForget}>{t('memoryV2.forget')}</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  </li>
}


/** Words or Meaning: how the one search box reads what is typed in it. */
export function SearchModeSwitch({ mode, onChange, wide = false }: { mode: SearchMode; onChange: (mode: SearchMode) => void; wide?: boolean }) {
  const { t } = useTranslation()
  return <div role="radiogroup" aria-label={t('memoryPage.search_by')} className={`flex rounded-lg bg-bg-hover p-0.5 ${wide ? 'h-11 w-full' : 'h-8'}`}>
    {(['words', 'meaning'] as const).map(key => {
      const on = key === mode
      return <button key={key} type="button" role="radio" aria-checked={on} onClick={() => onChange(key)} className={`flex flex-1 items-center justify-center gap-1 whitespace-nowrap rounded-md px-2.5 text-[12px] focus-ring ${on ? 'bg-card text-text shadow-sm' : 'text-muted'}`}>
        {key === 'meaning' && <Sparkles className="lucide-inline" aria-hidden="true" />}{t(key === 'words' ? 'memoryPage.mode_words' : 'memoryPage.mode_meaning')}
      </button>
    })}
  </div>
}

/** Saved topics: one click runs that search by meaning; × removes it. Save shows in Meaning mode. */
export function SavedTopics({ topics, query, canSave, onRun, onRemove, onSave }: { topics: string[]; query: string; canSave: boolean; onRun: (topic: string) => void; onRemove: (topic: string) => void; onSave: () => void }) {
  const { t } = useTranslation()
  const showSave = canSave && !!query && !topics.includes(query)
  if (!topics.length && !showSave) return null
  return <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0" aria-label={t('memoryPage.topics')} role="group">
    {topics.length > 0 && <span className="mr-1 text-[12px] text-muted">{t('memoryPage.topics')}</span>}
    {topics.map(topic => <span key={topic} className="flex min-h-11 items-center sm:min-h-8"><span className="inline-flex h-8 max-w-[240px] items-center rounded-full border border-border bg-card text-[13px] text-text">
      <button type="button" className="truncate pl-3 pr-1.5 focus-ring" title={topic} onClick={() => onRun(topic)}>{topic}</button>
      <button type="button" aria-label={t('memoryPage.remove_topic', { topic })} className="flex h-full w-9 shrink-0 items-center justify-center text-muted focus-ring sm:w-7" onClick={() => onRemove(topic)}><X className="lucide-inline" aria-hidden="true" /></button>
    </span></span>)}
    {showSave && <Btn className="min-h-11 border-transparent text-accent sm:min-h-8" onClick={onSave}><Plus className="lucide-inline" aria-hidden="true" />{t('memoryPage.save_topic', { topic: query })}</Btn>}
  </div>
}
