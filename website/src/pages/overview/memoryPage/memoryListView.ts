/**
 * What the memory page's list shows: the search words, the kind pill, the sort
 * and the facet filters, kept per store for the browser session, and the labels
 * each facet value reads as.
 */
import { i18nT } from '../../../i18n/t'
import { safeGetSessionItem, safeSetSessionItem } from '../../../utils/safeStorage'
import type { MemoryRecord, MemoryRecordFacetGroup, MemoryRecordFilters, MemoryRecordKind, MemoryRecordQuery, MemoryRecordSort } from '../../../types/memoryEditing'
import { decodedRecordValue } from '../memoryRecordParts'

export type KindTab = MemoryRecordQuery['kind']
export type ListSort = Exclude<MemoryRecordSort, 'match'>
export type ListView = {
  q: string
  kind: KindTab
  /** The picked sort; a search ranks by Best match until one is picked while searching. */
  sort: MemoryRecordSort
  order: 'asc' | 'desc'
  filters: MemoryRecordFilters
  /** How the search words are read: as words, or as a topic ranked by meaning. */
  mode: SearchMode
}
export type SearchMode = 'words' | 'meaning'
export const DEFAULT_VIEW: ListView = { q: '', kind: 'all', sort: 'updated', order: 'desc', filters: {}, mode: 'words' }
export const FACET_GROUPS: MemoryRecordFacetGroup[] = ['category', 'applies', 'scope', 'tag', 'source', 'surface', 'crew']
const SORTS: MemoryRecordSort[] = ['updated', 'created', 'confidence', 'importance', 'key', 'match']

/** The sort a request sends: Best match needs search words, so it falls back to Updated without them. */
export const effectiveSort = (view: ListView): MemoryRecordSort => byMeaning(view) ? 'match' : view.sort === 'match' && !view.q ? 'updated' : view.sort
/** A search by meaning needs words to embed; with none the list is the plain one. */
export const byMeaning = (view: ListView) => view.mode === 'meaning' && !!view.q

/** Which kinds a facet group or a score belongs to. */
export const GROUP_KINDS: Record<MemoryRecordFacetGroup, MemoryRecordKind[]> = {
  category: ['directive'], applies: ['directive'], scope: ['directive'], tag: ['episode'],
  source: ['fact', 'directive', 'episode'], surface: ['fact', 'directive', 'episode'], crew: ['fact', 'directive', 'episode'],
}

// The applied view, kept per store for the browser session. Not the URL: the
// Settings rail reopens `/settings` with no query string, so a URL filter would
// not survive the leave-and-return trip it exists for.
const VIEW_KEY_PREFIX = 'kirocrew:memory-page-view:'
export function loadView(store: string): ListView {
  try {
    const saved = JSON.parse(safeGetSessionItem(VIEW_KEY_PREFIX + store) || '{}')
    const kind = saved.kind === 'fact' || saved.kind === 'directive' || saved.kind === 'episode' ? saved.kind : 'all'
    const filters: MemoryRecordFilters = {}
    for (const group of FACET_GROUPS) {
      const values = saved.filters?.[group]
      if (Array.isArray(values) && values.every(value => typeof value === 'string') && values.length) filters[group] = values
    }
    for (const bound of ['conf_min', 'conf_max'] as const) {
      const value = saved.filters?.[bound]
      if (typeof value === 'number' && value >= 0 && value <= 1) filters[bound] = value
    }
    return {
      q: typeof saved.q === 'string' ? saved.q : '',
      kind,
      sort: SORTS.includes(saved.sort) ? saved.sort : DEFAULT_VIEW.sort,
      order: saved.order === 'asc' ? 'asc' : 'desc',
      filters,
      mode: saved.mode === 'meaning' ? 'meaning' : 'words',
    }
  } catch { return DEFAULT_VIEW }
}
export const saveView = (store: string, view: ListView) => safeSetSessionItem(VIEW_KEY_PREFIX + store, JSON.stringify(view))

/** The filters that apply to a kind pill: a lesson facet means nothing on the Experiences pill. */
export function filtersForKind(filters: MemoryRecordFilters, kind: KindTab): MemoryRecordFilters {
  if (kind === 'all') return filters
  const out: MemoryRecordFilters = filters.ref?.length ? { ref: filters.ref } : {}
  for (const group of FACET_GROUPS) if (filters[group]?.length && GROUP_KINDS[group].includes(kind)) out[group] = filters[group]
  if (kind !== 'episode') { out.conf_min = filters.conf_min; out.conf_max = filters.conf_max }
  if (out.conf_min === undefined) delete out.conf_min
  if (out.conf_max === undefined) delete out.conf_max
  return out
}
export const activeFilterCount = (filters: MemoryRecordFilters) =>
  FACET_GROUPS.reduce((count, group) => count + (filters[group]?.length || 0), 0) + (filters.conf_min !== undefined || filters.conf_max !== undefined ? 1 : 0) + (filters.ref?.length ? 1 : 0)

export function toggleFilter(filters: MemoryRecordFilters, group: MemoryRecordFacetGroup, value: string): MemoryRecordFilters {
  const current = filters[group] || []
  const next = current.includes(value) ? current.filter(item => item !== value) : [...current, value]
  const out = { ...filters }
  if (next.length) out[group] = next
  else delete out[group]
  return out
}

// The same value as memory_record_facets.SCOPE_UNUSABLE, which the facet counts carry.
export const SCOPE_UNUSABLE = ':unusable'
const CATEGORY_KEYS: Record<string, string> = { knowledge: 'memoryPage.category_knowledge', tool: 'memoryPage.category_tool', preference: 'memoryPage.category_preference' }
const APPLIES_KEYS: Record<string, string> = { always: 'memoryPage.applies_always', on_topic: 'memoryPage.applies_on_topic', unstated: 'memoryPage.applies_unstated' }
const SOURCE_KEYS: Record<string, string> = {
  user_explicit: 'pages.overview.memoryTab.source_user_explicit',
  consolidation: 'pages.overview.memoryTab.source_consolidation',
  taskrunner: 'pages.overview.memoryTab.source_taskrunner',
  user_seed: 'memoryV2.source_copy',
  migration: 'memoryV2.source_migration',
  import: 'memoryV2.source_migration',
  unknown: 'memoryV2.source_unknown',
  '': 'memoryV2.source_unknown',
}

/** How one facet value reads. An unknown value (a raw source tag, a repo path, a tag) shows as stored. */
/** The source facet value for a write origin, as `memory_record_facets.source_group`
 *  makes it: `consolidation:dashboard:<session>` counts as `consolidation`. */
export function sourceGroup(source: string | undefined): string {
  const value = (source || '').trim()
  if (!value) return 'unknown'
  const colon = value.indexOf(':')
  return colon > 0 ? value.slice(0, colon) : value
}

export function facetLabel(group: MemoryRecordFacetGroup, value: string): string {
  if (group === 'category') return CATEGORY_KEYS[value] ? i18nT(CATEGORY_KEYS[value]) : value
  if (group === 'applies') return APPLIES_KEYS[value] ? i18nT(APPLIES_KEYS[value]) : value
  if (group === 'scope') return value === '' ? i18nT('memoryPage.scope_all_repos') : value === SCOPE_UNUSABLE ? i18nT('pages.overview.memoryTab.scope_unusable') : value
  if (group === 'source') return SOURCE_KEYS[value] !== undefined ? i18nT(SOURCE_KEYS[value]) : value
  // The channel and the crew are stamped on a bot's memory; an empty one was never recorded.
  if (group === 'surface' || group === 'crew') return value === '' ? i18nT('pages.overview.memoryCarveCard.not_recorded') : value
  return `#${value}`
}

/** The lesson fields a row shows, read with the same fallbacks the facets use. */
export function lessonFacts(row: MemoryRecord): { category: string; applies: string; scope: string; negative?: string } {
  const value = decodedRecordValue(row)
  const record = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
  // As `normalize_lesson_category(strict=False)`: a blank or non-string label is the default one.
  const category = typeof record.category === 'string' && record.category.trim() ? record.category.trim().toLowerCase() : 'knowledge'
  const applies = typeof record.applies === 'string' && APPLIES_KEYS[record.applies.trim().toLowerCase()] ? record.applies.trim().toLowerCase() : 'unstated'
  const raw = record.repo_scope
  const scope = 'repo_scope' in record && raw !== null && raw !== undefined && typeof raw !== 'string' ? SCOPE_UNUSABLE : typeof raw === 'string' ? raw.trim() : ''
  const negative = typeof record.negative === 'string' && record.negative.trim() ? record.negative.trim() : undefined
  return { category, applies, scope, negative }
}

/** An experience's tags from its stored JSON list. */
export function recordTags(row: MemoryRecord): string[] {
  try {
    const tags: unknown = JSON.parse(row.tags || '[]')
    return Array.isArray(tags) ? [...new Set(tags.filter((tag): tag is string => typeof tag === 'string' && !!tag.trim()).map(tag => tag.trim()))] : []
  } catch { return [] }
}

/** The one-line summary of a record's value; the detail panel shows it in full. */
export function recordTitle(row: MemoryRecord, body: string): string {
  if (row.kind !== 'fact') return body
  const value = decodedRecordValue(row)
  if (!value || typeof value !== 'object' || Array.isArray(value)) return body
  return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${key}: ${typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item)}`).join(' · ')
}
