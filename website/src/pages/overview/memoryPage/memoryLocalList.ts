/**
 * The memory list computed in the browser once every record of a store is loaded.
 *
 * Each record carries the server's own summary (`summary=1`), so this mirrors
 * `memory_record_facets.matches`, `count_facets`, `order` and `match_score` and the
 * word filter in `memory_edit._rows` over those values: the same rows, order and
 * counts as the server path, with no request per filter, sort or search.
 */
import type { MemoryRecord, MemoryRecordFacetGroup, MemoryRecordFacets, MemoryRecordFilters, MemoryRecordQuery, MemoryRecordSort, MemoryRecordSummary } from '../../../types/memoryEditing'

const GROUPS: MemoryRecordFacetGroup[] = ['category', 'applies', 'scope', 'source', 'surface', 'crew', 'tag']

/** `memory_edit._normalized`: NFKC, then case folded. */
export const normalized = (value: string) => value.normalize('NFKC').toLowerCase().replace(/ß/g, 'ss')

function matches(summary: MemoryRecordSummary, filters: MemoryRecordFilters, skip?: MemoryRecordFacetGroup): boolean {
  const facets = summary.facets
  for (const group of GROUPS) {
    const wanted = filters[group]
    if (group === skip || !wanted?.length) continue
    if (group === 'tag') { if (!facets.tag.some(tag => wanted.includes(tag))) return false }
    else if (facets[group] === null || !wanted.includes(facets[group] as string)) return false
  }
  if (filters.ref?.length && !filters.ref.includes(summary.ref)) return false
  if (filters.conf_min !== undefined || filters.conf_max !== undefined) {
    if (summary.confidence === null) return false
    if (summary.confidence < (filters.conf_min ?? 0) || summary.confidence > (filters.conf_max ?? 1)) return false
  }
  return true
}

/** `memory_record_facets.match_score`. */
function matchScore(tokens: string[], key: string, text: string): number {
  if (!tokens.length) return 0
  let score = 0
  for (const token of tokens) score += key.includes(token) ? 2 : text.includes(token) ? 1 : 0
  if (tokens.length > 1 && text.includes(tokens.join(' '))) score += 1
  return Math.round(Math.min(1, score / (2 * tokens.length + (tokens.length > 1 ? 1 : 0))) * 100) / 100
}

const byText = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)

type Item = { record: MemoryRecord; summary: MemoryRecordSummary; match: number }

/** `memory_record_facets.order`: ties by kind and id; rows with no score go last, newest first. */
function order(items: Item[], sort: MemoryRecordSort, descending: boolean): Item[] {
  items.sort((a, b) => byText(a.summary.kind, b.summary.kind) || byText(a.summary.id || '', b.summary.id || ''))
  const sign = descending ? -1 : 1
  if (sort === 'confidence' || sort === 'importance') {
    const scored = items.filter(item => item.summary[sort] !== null)
    const unscored = items.filter(item => item.summary[sort] === null)
    unscored.sort((a, b) => -byText(a.summary.updated_at || '', b.summary.updated_at || ''))
    scored.sort((a, b) => sign * ((a.summary[sort] as number) - (b.summary[sort] as number)))
    return [...scored, ...unscored]
  }
  if (sort === 'match') return items.sort((a, b) => sign * (a.match - b.match))
  const field = sort === 'updated' ? 'updated_at' : sort === 'created' ? 'created_at' : 'key'
  return items.sort((a, b) => sign * byText(a.summary[field] || '', b.summary[field] || ''))
}

export type LocalPage = { entries: MemoryRecord[]; total: number; has_more: boolean; facets: MemoryRecordFacets }

/** One page of `records` for this query and sort, with the facet counts, as `memory_edit.list_records` builds it. */
export function localPage(records: MemoryRecord[], query: MemoryRecordQuery, offset: number, limit: number, sort: MemoryRecordSort, descending: boolean): LocalPage {
  const tokens = normalized(query.q).split(/\s+/).filter(Boolean)
  const filters = query.filters || {}
  const found: Item[] = []
  for (const record of records) {
    const summary = record.summary
    if (!summary || !tokens.every(token => summary.search.includes(token))) continue
    found.push({ record, summary, match: matchScore(tokens, normalized(summary.key), summary.search) })
  }
  const counts = Object.fromEntries(['kind', ...GROUPS].map(group => [group, {} as Record<string, number>])) as MemoryRecordFacets
  const bump = (group: keyof MemoryRecordFacets, value: string) => { counts[group][value] = (counts[group][value] || 0) + 1 }
  const listed: Item[] = []
  for (const item of found) {
    const { summary } = item
    const passes = matches(summary, filters)
    if (passes) bump('kind', summary.kind)
    if (query.kind !== 'all' && summary.kind !== query.kind) continue
    if (passes) listed.push(item)
    for (const group of GROUPS) {
      if (!matches(summary, filters, group)) continue
      const values = group === 'tag' ? summary.facets.tag : [summary.facets[group]]
      for (const value of values) if (value !== null) bump(group, value)
    }
  }
  const sorted = order(listed, sort, descending)
  const entries = sorted.slice(offset, offset + limit).map(item => sort === 'match' ? { ...item.record, match: item.match } : item.record)
  return { entries, total: sorted.length, has_more: sorted.length > offset + limit, facets: counts }
}
